"""Checkerboard calibration script on synthetic OpenCV-rendered views."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from openflight.camera.intrinsics import load_camera_intrinsics  # noqa: E402

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "hardware-test"
    / "calibrate_camera_intrinsics.py"
)
spec = importlib.util.spec_from_file_location("calibrate_camera_intrinsics", SCRIPT)
calibration = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = calibration
spec.loader.exec_module(calibration)

WIDTH, HEIGHT = 640, 400
MATRIX = np.array([[470.0, 0.0, 327.0], [0.0, 468.0, 204.0], [0.0, 0.0, 1.0]])
DIST = np.array([-0.18, 0.05, 0.0, 0.0, 0.0])
COLS, ROWS, SQUARE_M = 9, 6, 0.04
MODE = {
    "width": WIDTH,
    "height": HEIGHT,
    "fps": 300.0,
    "stream": "raw",
    "rotate_180": False,
    "mirror_horizontal": True,
    "scaler_crop": None,
}


def _rotation(rx_deg: float, ry_deg: float, rz_deg: float) -> np.ndarray:
    rotation, _ = cv2.Rodrigues(np.radians([rx_deg, ry_deg, rz_deg]))
    return rotation


def _render_board(
    rotation: np.ndarray,
    translation: np.ndarray,
    matrix: np.ndarray = MATRIX,
    dist: np.ndarray = DIST,
    square_m: float = SQUARE_M,
) -> np.ndarray:
    """Ray-trace a 10x7 checkerboard (one white square border) through the lens."""
    grid = np.stack(np.meshgrid(np.arange(WIDTH), np.arange(HEIGHT)), axis=-1).reshape(-1, 1, 2)
    rays = cv2.undistortPoints(grid.astype(np.float64), matrix, dist).reshape(-1, 2)
    rays = np.column_stack((rays, np.ones(len(rays))))
    normal = rotation[:, 2]
    scale = (normal @ translation) / (rays @ normal)
    board = (rays * scale[:, None] - translation) @ rotation
    squares = np.floor(board[:, :2] / square_m).astype(int) + 1
    inside = (
        (squares[:, 0] >= 0)
        & (squares[:, 0] <= COLS)
        & (squares[:, 1] >= 0)
        & (squares[:, 1] <= ROWS)
        & (scale > 0)
    )
    image = np.full(len(rays), 90, np.uint8)
    margin = (board[:, 0] > -2 * square_m) & (board[:, 0] < (COLS + 2) * square_m)
    margin &= (board[:, 1] > -2 * square_m) & (board[:, 1] < (ROWS + 2) * square_m)
    image[margin & (scale > 0)] = 230
    dark = inside & ((squares[:, 0] + squares[:, 1]) % 2 == 1)
    image[dark] = 25
    return cv2.GaussianBlur(image.reshape(HEIGHT, WIDTH), (3, 3), 0.7)


def _poses():
    centre = np.array([COLS * SQUARE_M / 2, ROWS * SQUARE_M / 2, 0.0])
    for index in range(18):
        rotation = _rotation(
            25 * math.sin(index * 1.3), 25 * math.cos(index * 0.9), 10 * math.sin(index)
        )
        target = np.array(
            [0.25 * math.cos(index * 2.1), 0.14 * math.sin(index * 1.7), 0.85 + 0.1 * (index % 3)]
        )
        yield rotation, target - rotation @ centre


def _write_views(directory: Path, count: int = 18) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, (rotation, translation) in enumerate(_poses()):
        if index >= count:
            break
        path = directory / f"view_{index:03d}.png"
        cv2.imwrite(str(path), _render_board(rotation, translation))
        paths.append(path)
    return paths


def _write_frontal_views(directory: Path, count: int = 18) -> None:
    """Board always within 3 deg of the image plane, so the focal length is unobservable."""
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    centre = np.array([COLS * SQUARE_M / 2, ROWS * SQUARE_M / 2, 0.0])
    for index in range(count):
        rotation = _rotation(rng.uniform(-3, 3), rng.uniform(-3, 3), rng.uniform(-10, 10))
        target = np.array(
            [0.25 * math.cos(index * 2.1), 0.14 * math.sin(index * 1.7), 0.85 + 0.1 * (index % 3)]
        )
        image = _render_board(rotation, target - rotation @ centre)
        cv2.imwrite(str(directory / f"view_{index:03d}.png"), image)


# Lens and views like the first calibration on the Pi (2026-10-04): A4 board with
# 25 mm squares at 0.9-1.5 m, mostly near-frontal, strong barrel distortion.
PI_MATRIX = np.array([[726.0, 0.0, 348.0], [0.0, 724.0, 271.0], [0.0, 0.0, 1.0]])
PI_DIST = np.array([-0.70, 0.49, -0.034, -0.003, 0.0])
A4_SQUARE_M = 0.025


def _write_pi_like_views(directory: Path) -> None:
    """22 noisy views: 4 tilted 15-30 deg, the rest within 10 deg of frontal."""
    directory.mkdir(parents=True, exist_ok=True)
    poses, noise = np.random.default_rng(1), np.random.default_rng(101)
    centre = np.array([4 * A4_SQUARE_M, 2.5 * A4_SQUARE_M, 0.0])
    for index in range(22):
        tilt = poses.uniform(15, 30) if index < 4 else poses.uniform(0, 10)
        axis = poses.normal(size=2)
        axis /= np.linalg.norm(axis)
        spin = np.radians(poses.uniform(-8, 8))
        rotation, _ = cv2.Rodrigues(np.array([*(np.radians(tilt) * axis), spin]))
        depth = poses.uniform(0.9, 1.5)
        u, v = poses.uniform(80, 560), poses.uniform(60, 330)
        target = np.array([(u - 348) / 726 * depth, (v - 271) / 724 * depth, depth])
        image = _render_board(rotation, target - rotation @ centre, PI_MATRIX, PI_DIST, A4_SQUARE_M)
        noisy = np.clip(image + noise.normal(0, 4, image.shape), 0, 255).astype(np.uint8)
        cv2.imwrite(str(directory / f"view_{index:03d}.png"), noisy)


def _metadata(path: Path) -> Path:
    path.write_text(json.dumps({"settings": MODE}))
    return path


@pytest.fixture(scope="module")
def views_dir(tmp_path_factory) -> Path:
    directory = tmp_path_factory.mktemp("views")
    _write_views(directory)
    return directory


def test_solve_recovers_known_lens_and_writes_loadable_json(views_dir, tmp_path):
    out = tmp_path / "intrinsics.json"

    code = calibration.main(
        [
            "solve",
            str(views_dir),
            "--capture-metadata",
            str(_metadata(tmp_path / "metadata.json")),
            "--out",
            str(out),
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["rms_reprojection_px"] < 0.3
    assert payload["views_used"] == 18
    assert payload["mode"] == MODE
    assert payload["board"] == {"inner_cols": 9, "inner_rows": 6, "square_mm": 40.0}
    loaded = load_camera_intrinsics(out)
    assert loaded.fx == pytest.approx(470.0, rel=0.01)
    assert loaded.fy == pytest.approx(468.0, rel=0.01)
    assert loaded.cx == pytest.approx(327.0, abs=3.0)
    assert loaded.cy == pytest.approx(204.0, abs=3.0)
    assert loaded.dist_coeffs[0] == pytest.approx(-0.18, abs=0.03)
    assert loaded.mirror_horizontal is True
    assert payload["focal_uncertainty_pct"] < 1.0
    assert payload["leave_one_out_fx_shift_pct"] < 2.0
    assert payload["views_tilted_30deg"] >= 0
    assert payload["quality_problems"] == []


def test_solve_escapes_the_local_minimum_of_opencvs_own_start(tmp_path):
    # OpenCV's closed-form start ends at fx ~3900 (RMS 0.34 px) for these views,
    # with huge distortion terms standing in for the focal length.
    _write_pi_like_views(tmp_path / "views")
    out = tmp_path / "intrinsics.json"
    metadata = _metadata(tmp_path / "metadata.json")

    code = calibration.main(
        [
            "solve",
            str(tmp_path / "views"),
            "--square-mm",
            "25",
            "--capture-metadata",
            str(metadata),
            "--out",
            str(out),
            "--force",
        ]
    )

    assert code == 0
    payload = json.loads(out.read_text())
    assert payload["fx"] == pytest.approx(726.0, rel=0.03)
    assert payload["rms_reprojection_px"] < 0.25


def test_solve_refuses_near_frontal_views_with_an_unobservable_focal_length(tmp_path, capsys):
    # RMS, view count and coverage all pass, yet fx comes out ~17% too long.
    _write_frontal_views(tmp_path / "frontal")
    out = tmp_path / "intrinsics.json"
    metadata = _metadata(tmp_path / "metadata.json")
    args = ["solve", str(tmp_path / "frontal"), "--capture-metadata", str(metadata)]

    assert calibration.main([*args, "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "focal length poorly determined" in err
    assert "tilted 30-45 deg" in err
    assert not out.exists()
    assert calibration.main([*args, "--out", str(out), "--force"]) == 0
    payload = json.loads(out.read_text())
    assert payload["focal_uncertainty_pct"] > 1.0
    assert payload["views_tilted_30deg"] == 0


def test_solve_reads_capture_folders_with_their_own_metadata(tmp_path):
    for index, (rotation, translation) in enumerate(list(_poses())[:12]):
        capture = tmp_path / f"camera_{index:03d}"
        capture.mkdir()
        frame = _render_board(rotation, translation)
        np.savez(
            capture / "frames.npz",
            frames=np.stack([np.zeros_like(frame), frame, np.zeros_like(frame)]),
            pre_trigger_count=np.int32(2),
        )
        _metadata(capture / "metadata.json")
    out = tmp_path / "intrinsics.json"

    code = calibration.main(
        ["solve", *map(str, sorted(tmp_path.glob("camera_*"))), "--out", str(out), "--force"]
    )

    assert code == 0
    assert json.loads(out.read_text())["views_used"] == 12


def test_solve_refuses_images_without_capture_mode(views_dir, tmp_path, capsys):
    out = tmp_path / "intrinsics.json"

    assert calibration.main(["solve", str(views_dir), "--out", str(out)]) == 2
    assert "--capture-metadata" in capsys.readouterr().err
    assert not out.exists()


def test_solve_refuses_too_few_views(tmp_path, capsys):
    _write_views(tmp_path / "few", count=6)
    out = tmp_path / "intrinsics.json"
    metadata = _metadata(tmp_path / "metadata.json")

    code = calibration.main(
        ["solve", str(tmp_path / "few"), "--capture-metadata", str(metadata), "--out", str(out)]
    )

    assert code == 2
    assert "need >= 10" in capsys.readouterr().err
    assert not out.exists()


def test_solve_refuses_poor_rms_unless_forced(views_dir, tmp_path, capsys):
    metadata = _metadata(tmp_path / "metadata.json")
    out = tmp_path / "intrinsics.json"
    args = ["solve", str(views_dir), "--capture-metadata", str(metadata), "--out", str(out)]

    assert calibration.main([*args, "--max-rms", "0.0001"]) == 2
    assert "RMS reprojection error" in capsys.readouterr().err
    assert not out.exists()
    assert calibration.main([*args, "--max-rms", "0.0001", "--force"]) == 0
    assert json.loads(out.read_text())["quality_problems"]


def test_solve_refuses_mode_mismatch(views_dir, tmp_path, capsys):
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps({"settings": {**MODE, "width": 320, "height": 200}}))

    code = calibration.main(
        ["solve", str(views_dir), "--capture-metadata", str(metadata), "--out", str(tmp_path / "x")]
    )

    assert code == 2
    assert "differ from capture mode" in capsys.readouterr().err


def test_grab_keeps_only_views_with_the_whole_board(tmp_path):
    rotation, translation = next(_poses())
    _ok, board_jpeg = cv2.imencode(".jpg", _render_board(rotation, translation))
    _ok, empty_jpeg = cv2.imencode(".jpg", np.full((HEIGHT, WIDTH), 90, np.uint8))
    responses = [empty_jpeg.tobytes(), board_jpeg.tobytes(), board_jpeg.tobytes()]
    args = calibration.parse_args(["grab", "--count", "2", "--out", str(tmp_path / "grab")])

    code = calibration.grab(args, fetch=lambda _url: responses.pop(0), sleep=lambda _s: None)

    assert code == 0
    assert sorted(path.name for path in (tmp_path / "grab").iterdir()) == [
        "view_001.png",
        "view_002.png",
    ]


@pytest.mark.parametrize(
    "argv",
    [["solve", "x", "--out", "y", "--cols", "2"], ["solve", "x"], ["grab"], []],
)
def test_cli_rejects_invalid_arguments(argv):
    with pytest.raises(SystemExit):
        calibration.parse_args(argv)
