#!/usr/bin/env python3
"""Checkerboard lens calibration for the OV9281 capture mode OpenFlight uses.

Two steps, both run on the Pi while OpenFlight is running with --camera-capture:

  grab   Save checkerboard views from the live /api/camera/preview.jpg frame,
         which has the saved-capture orientation (rotate/mirror already applied).
         The preview is roll-warped when --camera-capture-roll-deg is set, so
         grab only with roll 0.
  solve  Run OpenCV calibration on those views (or on capture folders /
         frames.npz files / PGM/PNG images) and write the JSON that
         ``openflight.server --camera-intrinsics`` loads.

The calibration is only valid for the capture mode recorded in the output
(size, rotate/mirror, scaler crop). Image files carry no mode, so ``solve``
needs ``--capture-metadata`` pointing to the metadata.json of any capture
taken in the same mode; capture folders supply their own metadata.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

IMAGE_SUFFIXES = {".pgm", ".png", ".jpg", ".jpeg", ".bmp"}
MODE_KEYS = ("width", "height", "rotate_180", "mirror_horizontal", "scaler_crop", "stream", "fps")
DEFAULT_PREVIEW_URL = "http://127.0.0.1:8080/api/camera/preview.jpg"
HARD_MIN_VIEWS = 10
WARN_RMS_PX = 0.5
COVERAGE_GRID = 4
FOCAL_GUESSES = (0.6, 0.9, 1.3, 1.9)  # x image width: roughly 80 down to 30 deg wide
MAX_FOCAL_UNCERTAINTY_PCT = 1.0
MAX_LEAVE_ONE_OUT_PCT = 2.0
TILTED_DEG = 30.0


def _cv2():
    try:
        import cv2  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - operator environment issue
        raise SystemExit("OpenCV missing: uv sync --extra camera") from exc
    return cv2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    board = argparse.ArgumentParser(add_help=False)
    board.add_argument("--cols", type=int, default=9, help="Inner corners per row (default 9)")
    board.add_argument("--rows", type=int, default=6, help="Inner corners per column (default 6)")
    board.add_argument("--square-mm", type=float, default=40.0, help="Printed square size")
    commands = parser.add_subparsers(dest="command", required=True)

    grab = commands.add_parser("grab", parents=[board], help="Save views from the live preview")
    grab.add_argument("--url", default=DEFAULT_PREVIEW_URL)
    grab.add_argument("--count", type=int, default=25, help="Views with a detected board")
    grab.add_argument("--interval-s", type=float, default=3.0, help="Pause to move the board")
    grab.add_argument("--max-attempts", type=int, default=200)
    grab.add_argument("--out", type=Path, required=True)

    solve = commands.add_parser("solve", parents=[board], help="Calibrate from saved views")
    solve.add_argument("inputs", nargs="+", type=Path, help="Images, capture dirs or frames.npz")
    solve.add_argument("--capture-metadata", type=Path, help="metadata.json of a same-mode capture")
    solve.add_argument("--out", type=Path, required=True, help="Calibration JSON to write")
    solve.add_argument("--min-views", type=int, default=15, help="Warn below this many views")
    solve.add_argument("--max-rms", type=float, default=1.0, help="Refuse above this RMS (px)")
    solve.add_argument("--free-k3", action="store_true", help="Also fit radial k3")
    solve.add_argument("--force", action="store_true", help="Write despite failed quality gates")

    args = parser.parse_args(argv)
    if args.cols < 3 or args.rows < 3 or args.square_mm <= 0:
        parser.error("board needs >=3x3 inner corners and a positive square size")
    return args


def find_corners(image: np.ndarray, pattern: tuple[int, int]) -> np.ndarray | None:
    """Subpixel inner-corner positions, or None when the full board is not seen."""
    cv2 = _cv2()
    found, corners = cv2.findChessboardCornersSB(image, pattern, flags=cv2.CALIB_CB_EXHAUSTIVE)
    if not found:
        found, corners = cv2.findChessboardCorners(image, pattern)
        if not found:
            return None
        corners = cv2.cornerSubPix(
            image,
            corners,
            (5, 5),
            (-1, -1),
            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3),
        )
    return np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2)


def _read_metadata_settings(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    settings = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings, dict):
        raise ValueError(f"{path} has no capture settings")
    return {key: settings.get(key) for key in MODE_KEYS}


def _npz_view(path: Path) -> tuple[str, np.ndarray, dict | None]:
    """Trigger frame of a saved capture plus its recorded mode, if any."""
    with np.load(path) as archive:
        frames = archive["frames"]
        index = int(archive["pre_trigger_count"]) - 1 if "pre_trigger_count" in archive else 0
    metadata = path.parent / "metadata.json"
    mode = _read_metadata_settings(metadata) if metadata.exists() else None
    return str(path.parent), np.ascontiguousarray(frames[index]), mode


def load_views(inputs: list[Path], capture_metadata: Path | None) -> tuple[list, dict]:
    """Return (name, grayscale image) views and the single capture mode they share."""
    cv2 = _cv2()
    views: list[tuple[str, np.ndarray]] = []
    modes: list[dict] = []
    needs_mode = False
    for path in inputs:
        if path.is_dir() and (path / "frames.npz").exists():
            path = path / "frames.npz"
        if path.name == "frames.npz":
            name, image, mode = _npz_view(path)
            views.append((name, image))
            if mode is None:
                needs_mode = True
            else:
                modes.append(mode)
            continue
        files = sorted(path.iterdir()) if path.is_dir() else [path]
        for file in files:
            if file.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            image = cv2.imread(str(file), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise ValueError(f"cannot read image {file}")
            views.append((str(file), image))
            needs_mode = True
    if needs_mode:
        if capture_metadata is None:
            raise ValueError("image inputs need --capture-metadata to record the capture mode")
        modes.append(_read_metadata_settings(capture_metadata))
    if not views:
        raise ValueError("no input views found")
    if any(mode != modes[0] for mode in modes):
        raise ValueError("inputs were captured in different camera modes")
    mode = modes[0]
    sizes = {image.shape for _name, image in views}
    if sizes != {(mode["height"], mode["width"])}:
        raise ValueError(f"image sizes {sorted(sizes)} differ from capture mode {mode}")
    return views, mode


def _fit_lowest_rms(objects: list, image_points: list, size: tuple[int, int], flags: int) -> tuple:
    """Lowest-RMS calibrateCameraExtended fit over OpenCV's own start and focal guesses.

    OpenCV's closed-form start degrades with near-frontal views, and
    Levenberg-Marquardt can then stop in a local minimum where large distortion
    terms stand in for a wrong focal length (first Pi calibration: fx 1079 at
    RMS 0.39 px, against fx 726 at 0.26 px for the same views).
    """
    cv2 = _cv2()
    width, height = size
    starts = [(None, None, flags)]
    for factor in FOCAL_GUESSES:
        focal = factor * width
        matrix = np.array(
            [[focal, 0.0, (width - 1) / 2], [0.0, focal, (height - 1) / 2], [0.0, 0.0, 1.0]]
        )
        starts.append((matrix, np.zeros(5), flags | cv2.CALIB_USE_INTRINSIC_GUESS))
    fits = []
    for matrix, dist, start_flags in starts:
        try:
            fits.append(
                cv2.calibrateCameraExtended(
                    objects, image_points, size, matrix, dist, flags=start_flags
                )
            )
        except cv2.error:  # a diverging start is simply skipped
            continue
    if not fits:
        raise ValueError("calibration did not converge from any start")
    return min(fits, key=lambda fit: fit[0])


def _leave_one_out_fx_shift(
    objects: list, image_points: list, size: tuple[int, int], flags: int, matrix, dist
) -> float:
    """Largest relative fx change when any single view is left out."""
    cv2 = _cv2()
    shift = 0.0
    for skip in range(len(image_points)):
        rest = image_points[:skip] + image_points[skip + 1 :]
        _rms, refit, *_ = cv2.calibrateCamera(
            objects[: len(rest)],
            rest,
            size,
            matrix.copy(),
            dist.copy(),
            flags=flags | cv2.CALIB_USE_INTRINSIC_GUESS,
        )
        shift = max(shift, abs(refit[0, 0] / matrix[0, 0] - 1.0))
    return shift


def _tilt_deg(rvec: np.ndarray) -> float:
    """Angle between the board and the image plane.

    A board parallel to the image plane maps by a similarity transform, which
    cannot separate focal length from distance wherever it sits in the image.
    """
    rotation, _ = _cv2().Rodrigues(rvec)
    return float(np.degrees(np.arccos(min(1.0, abs(rotation[2, 2])))))


def calibrate(views: list, mode: dict, args: argparse.Namespace) -> tuple[dict, list[str]]:
    """Run calibration and return the JSON payload plus quality problems."""
    cv2 = _cv2()
    pattern = (args.cols, args.rows)
    board = np.zeros((args.cols * args.rows, 3), np.float32)
    board[:, :2] = np.mgrid[0 : args.cols, 0 : args.rows].T.reshape(-1, 2) * args.square_mm / 1000
    used, image_points, skipped = [], [], []
    for name, image in views:
        corners = find_corners(image, pattern)
        if corners is None:
            skipped.append(name)
        else:
            used.append(name)
            image_points.append(corners)
    problems: list[str] = []
    if len(used) < HARD_MIN_VIEWS:
        raise ValueError(f"only {len(used)} views show the whole board; need >= {HARD_MIN_VIEWS}")
    if len(used) < args.min_views:
        problems.append(f"only {len(used)} usable views (recommend >= {args.min_views})")

    size = (mode["width"], mode["height"])
    flags = 0 if args.free_k3 else cv2.CALIB_FIX_K3
    objects = [board] * len(used)
    rms, matrix, dist, rvecs, tvecs, std_intrinsics, *_ = _fit_lowest_rms(
        objects, image_points, size, flags
    )
    per_view = []
    for corners, rvec, tvec in zip(image_points, rvecs, tvecs, strict=True):
        projected, _ = cv2.projectPoints(board, rvec, tvec, matrix, dist)
        per_view.append(float(np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=2)))))
    if rms > args.max_rms:
        problems.append(f"RMS reprojection error {rms:.3f} px exceeds {args.max_rms} px")
    elif rms > WARN_RMS_PX:
        print(f"WARNING: RMS {rms:.3f} px is high; re-check board flatness/focus", file=sys.stderr)

    # A low RMS does not mean a determined lens: near-frontal views fit just as
    # well with a wrong focal length.
    focal_uncertainty_pct = 100.0 * float(std_intrinsics.ravel()[0]) / float(matrix[0, 0])
    loo_pct = 100.0 * _leave_one_out_fx_shift(objects, image_points, size, flags, matrix, dist)
    tilted = sum(_tilt_deg(rvec) >= TILTED_DEG for rvec in rvecs)
    if focal_uncertainty_pct > MAX_FOCAL_UNCERTAINTY_PCT or loo_pct > MAX_LEAVE_ONE_OUT_PCT:
        problems.append(
            f"focal length poorly determined (+-{focal_uncertainty_pct:.1f}%, one view shifts it "
            f"{loo_pct:.1f}%); {tilted} of {len(used)} views tilted >= {TILTED_DEG:.0f} deg - "
            "add views tilted 30-45 deg in different directions"
        )

    all_corners = np.concatenate(image_points).reshape(-1, 2)
    cells = {
        (int(x * COVERAGE_GRID / size[0]), int(y * COVERAGE_GRID / size[1])) for x, y in all_corners
    }
    coverage_pct = 100.0 * len(cells) / COVERAGE_GRID**2
    if coverage_pct < 75.0:
        problems.append(f"corners cover only {coverage_pct:.0f}% of the image; add edge views")

    payload = {
        "version": 1,
        "fx": float(matrix[0, 0]),
        "fy": float(matrix[1, 1]),
        "cx": float(matrix[0, 2]),
        "cy": float(matrix[1, 2]),
        "dist_coeffs": [float(value) for value in np.ravel(dist)[:5]],
        "rms_reprojection_px": float(rms),
        "mode": mode,
        "board": {"inner_cols": args.cols, "inner_rows": args.rows, "square_mm": args.square_mm},
        "views_used": len(used),
        "views_skipped": skipped,
        "per_view_rms_px": dict(zip(used, per_view, strict=True)),
        "coverage_pct": coverage_pct,
        "focal_uncertainty_pct": focal_uncertainty_pct,
        "leave_one_out_fx_shift_pct": loo_pct,
        "views_tilted_30deg": tilted,
        "quality_problems": problems,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return payload, problems


def solve(args: argparse.Namespace) -> int:
    """Calibrate and write the intrinsics JSON."""
    try:
        views, mode = load_views(args.inputs, args.capture_metadata)
        payload, problems = calibrate(views, mode, args)
    except ValueError as exc:
        print(f"Calibration refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"{payload['views_used']} views, RMS {payload['rms_reprojection_px']:.3f} px, "
        f"fx={payload['fx']:.1f} fy={payload['fy']:.1f} "
        f"cx={payload['cx']:.1f} cy={payload['cy']:.1f}, coverage {payload['coverage_pct']:.0f}%"
    )
    print(
        f"fx uncertainty +-{payload['focal_uncertainty_pct']:.2f}%, largest leave-one-out shift "
        f"{payload['leave_one_out_fx_shift_pct']:.2f}%, "
        f"{payload['views_tilted_30deg']} views tilted >= {TILTED_DEG:.0f} deg"
    )
    for problem in problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if problems and not args.force:
        print("Not written; fix the views or pass --force.", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return response.read()


def grab(args: argparse.Namespace, fetch=None, sleep=time.sleep) -> int:
    """Save preview frames that show the whole board."""
    cv2 = _cv2()
    fetch = fetch or _fetch
    args.out.mkdir(parents=True, exist_ok=True)
    saved = 0
    for _attempt in range(args.max_attempts):
        if saved >= args.count:
            break
        image = cv2.imdecode(np.frombuffer(fetch(args.url), np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None or find_corners(image, (args.cols, args.rows)) is None:
            print("board not fully visible; keep it still and inside the frame")
            sleep(0.5)
            continue
        saved += 1
        path = args.out / f"view_{saved:03d}.png"
        cv2.imwrite(str(path), image)
        print(f"saved {path} ({saved}/{args.count}); move the board to the next pose")
        sleep(args.interval_s)
    if saved < args.count:
        print(f"Only {saved} views saved", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    return grab(args) if args.command == "grab" else solve(args)


if __name__ == "__main__":
    sys.exit(main())
