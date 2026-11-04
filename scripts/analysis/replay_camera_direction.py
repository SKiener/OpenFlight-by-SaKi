"""Replay the camera start direction (ball flight) over a range session's clips.

Every OPS-accepted shot of the session log is replayed in shot order through the
production code, as ``server._fuse_camera_ball_flight`` runs it:

* the IWR6843 measurement is recomputed from the saved L3 dump with
  ``IWR6843Runtime.process_shot`` and the session's TI configuration, for its
  ball range evidence (the camera's preferred depth);
* ``estimate_camera_ball_flight`` gets the clip's frames on the exposure clock,
  the session's mount geometry, the calibrated lens model, the hitting zone of
  ``tee_zone`` (as ``configure_camera_tee_zone`` computes it) and one
  ``ReferenceBallTracker`` and one ``GroundBallAnchor`` that live for the whole
  session, once per ``--locator``;
* ``select_camera_assisted_horizontal`` decides as live.

Clips the live run skipped for lighting (``rejected_lighting_quality``) never
updated the live session state, so they are estimated on copies of it.
``--false-triggers`` also runs every clip without an OPS-accepted shot (a sound
edge the OPS rejected) at ``--false-trigger-speed-mph`` on copies of the state;
live, these never reach the estimator, so this only checks that nothing there
looks like a departing ball (or finds strikes the OPS missed). Several
``--session`` logs share one camera directory: each runs with its own state,
and a clip without a shot counts for the session whose span holds it.

Toptracer (``--pairs``, the pairing CSV with ``of_shot``, ``block``,
``tt_hla_deg`` and ``tt_curve_m``) is read only for the final comparison;
nothing here is fitted to it. ``--not-strikes`` lists the OPS shots that were
not strikes; they must not produce a direction.

The estimate is timed on this machine around ``estimate_camera_ball_flight``
(after one untimed warm-up call); ``--csv`` writes one row per clip and locator.

Usage::

    uv run --frozen --extra camera python scripts/analysis/replay_camera_direction.py \\
        --session raw/session_20261019_181202_range.jsonl \\
        --camera-dir raw/range/camera --iwr-dir raw/iwr6843 \\
        --intrinsics camera-intrinsics.json --pairs pairs.csv \\
        --not-strikes 1,35,52,53 --locator both --false-triggers --csv replay.csv

The raw clips stay outside the repository.

2026-10-25 (69 Toptracer-paired strikes, 4 non-strikes, 148 clips without a
shot): ``disappeared`` measured driver 18/19 (OF33 withheld by the line fit,
4.5 mrad), 7-wood 9/9, irons/PW 41/41; ``legacy`` 0/19, 2/9, 19/41. On the 19
irons both measured, RMSE vs Toptracer 0.65 vs 0.66 deg. No direction for the
non-strikes; two clips without a shot gave one, both strikes the OPS missed
(Toptracer shots 28 and 41 fall in their intervals). Median 0.33 s, p90 0.42 s
per shot on a Mac. The 2026-10-11 clips end 15 frames after the trigger
(``--camera-capture-post-ms 50``), too early for the disappeared locator: all
16 fall back to the legacy one (11/16 with a direction either way).
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from openflight.camera.ball_flight import (
    BALL_LOCATORS,
    CameraBallEstimate,
    CameraBallGeometry,
    estimate_camera_ball_flight,
    select_camera_assisted_horizontal,
)
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.departed_ball import GroundBallAnchor
from openflight.camera.intrinsics import CameraIntrinsics, load_camera_intrinsics
from openflight.camera.tee_zone import TeeZone, tee_zone
from openflight.camera.trigger_check import exposure_timestamps_ns
from openflight.iwr6843 import Calibration
from openflight.iwr6843.runtime import IWR6843Runtime

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION = REPO / "config" / "iwr6843_calibration_reference.json"
IRON_BLOCKS = ("PW", "8i-1", "8i-2")
STRAIGHT_CURVE_M = 5.0


@dataclass
class _Capture:
    raw: bytes
    valid: bool = True


class _DumpMonitor:
    """Stands in for the TI capture monitor: hands out one saved dump per shot."""

    def __init__(self):
        self.next: _Capture | None = None

    def capture_for_shot(self, _impact_timestamp, timeout_s):  # noqa: ARG002
        del timeout_s
        return self.next


def load_session(path: Path) -> dict:
    """Start config, shots, TI captures and camera captures of a session JSONL."""
    session = {"config": {}, "shots": {}, "ti": {}, "camera": {}}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            kind = entry.get("type")
            if kind == "session_start":
                session["config"] = entry.get("config") or {}
            elif kind == "shot_detected":
                session["shots"][entry["shot_number"]] = entry
            elif kind == "iwr6843_capture" and entry.get("shot_number") is not None:
                session["ti"][entry["shot_number"]] = entry
            elif kind == "camera_capture" and entry.get("capture_path"):
                session["camera"][entry["shot_number"]] = entry
    return session


def ti_runtime(session: dict, calibration_path: Path) -> tuple[IWR6843Runtime, _DumpMonitor]:
    """The session's TI runtime, fed one saved dump per shot through the returned monitor."""
    config = session["config"].get("iwr6843") or {}
    calibration = Calibration.load(str(calibration_path))
    calibration.tee_range_m = config["tee_slant_range_m"]
    calibration.tee_ball_height_m = config["ball_height_m"]
    calibration.tilt_rad = math.radians(config["tilt_deg"])
    calibration.meta["radar_height_m"] = config["radar_height_m"]
    monitor = _DumpMonitor()
    runtime = IWR6843Runtime(
        capture_monitor=monitor,
        calibration=calibration,
        net_range_m=config["net_range_m"],
        tx_order=config["tx_order"],
        azimuth_offset_deg=config["azimuth_offset_deg"],
        horizontal_phase_reference_rad=config["horizontal_phase_reference_rad"],
        tdm_sign_policy="positive",
    )
    return runtime, monitor


def replay_ti(session: dict, iwr_dir: Path, calibration_path: Path) -> dict:
    """Ball range evidence per shot from the saved L3 dumps (None where unavailable)."""
    runtime, monitor = ti_runtime(session, calibration_path)
    evidence = {}
    for number, shot in sorted(session["shots"].items()):
        record = session["ti"].get(number)
        capture_path = (record or {}).get("capture_path")
        dump = iwr_dir / Path(capture_path).name if capture_path else None
        if dump is None or not dump.exists():
            evidence[number] = None
            continue
        monitor.next = _Capture(dump.read_bytes())
        inclinometer = shot.get("inclinometer") or {}
        result = runtime.process_shot(
            impact_timestamp=shot["impact_timestamp"],
            ball_speed_mph=record["ball_speed_mph"],
            club=shot["club"],
            club_speed_mph=None,
            tilt_deg=(
                inclinometer.get("effective_iwr_tilt_deg") if inclinometer.get("applied") else None
            ),
        )
        measurement = result.measurement
        evidence[number] = getattr(measurement, "range_evidence", None)
    return evidence


def geometry_for(config: dict, settings: dict, intrinsics: CameraIntrinsics | None):
    """The server's CameraBallGeometry for this session and clip."""
    camera, ti = config["camera_capture"], config["iwr6843"]
    return CameraBallGeometry(
        camera_height_m=float(camera["mount_height_m"]),
        radar_height_m=ti["radar_height_m"],
        tee_range_m=float(ti["tee_slant_range_m"]),
        ball_height_m=ti["ball_height_m"],
        camera_lateral_offset_m=float(camera.get("lateral_offset_m", 0.0)),
        camera_forward_offset_m=float(camera.get("forward_offset_m", 0.0)),
        yaw_from_reference_ball=bool(camera.get("yaw_from_ball", False)),
        horizontal_offset_deg=float(camera.get("horizontal_offset_deg", 0.0)),
        roll_correction_deg=float(camera.get("roll_correction_deg", 0.0)),
        horizontal_pixel_sign=-1.0 if settings.get("mirror_horizontal") else 1.0,
        image_width_px=int(camera["width"]),
        image_height_px=int(camera["height"]),
        intrinsics=(
            intrinsics
            if intrinsics is not None and intrinsics.mode_mismatch(settings) is None
            else None
        ),
    )


def zone_for(config: dict, intrinsics: CameraIntrinsics | None) -> TeeZone | None:
    """The hitting zone as ``server.configure_camera_tee_zone`` computes it."""
    camera, ti = config["camera_capture"], config["iwr6843"]
    if intrinsics is None or intrinsics.mode_mismatch(camera) is not None:
        return None
    return tee_zone(
        intrinsics,
        camera_height_m=float(camera["mount_height_m"]),
        camera_lateral_offset_m=float(camera.get("lateral_offset_m", 0.0)),
        camera_forward_offset_m=float(camera.get("forward_offset_m", 0.0)),
        tee_range_m=float(ti["tee_slant_range_m"]),
        radar_height_m=ti["radar_height_m"],
        ball_height_m=ti["ball_height_m"],
        horizontal_pixel_sign=-1.0 if camera.get("mirror_horizontal") else 1.0,
    )


def _clip(camera_dir: Path, name: str):
    clip = camera_dir / name
    archive = dict(np.load(clip / "frames.npz"))
    metadata = json.loads((clip / "metadata.json").read_text(encoding="utf-8"))
    timestamps = exposure_timestamps_ns(
        archive["host_timestamp_ns"], archive.get("sensor_timestamp_ns")
    )
    return archive["frames"], timestamps, int(archive["trigger_host_timestamp_ns"]), metadata


class _Locator:
    """One locator's session state (tracker and ground anchor) across the replay."""

    def __init__(self, name: str, zone: TeeZone | None):
        self.name = name
        self.zone = zone
        self.tracker = ReferenceBallTracker()
        self.ground = GroundBallAnchor()

    def estimate(self, *, shadow: bool, **kwargs) -> tuple[CameraBallEstimate, float]:
        tracker = copy.deepcopy(self.tracker) if shadow else self.tracker
        ground = copy.deepcopy(self.ground) if shadow else self.ground
        started = time.perf_counter()
        estimate = estimate_camera_ball_flight(
            **kwargs,
            ball_tracker=tracker,
            ball_locator=self.name,
            ground_anchor=ground,
            tee_zone=self.zone,
        )
        return estimate, time.perf_counter() - started


def _stamp(path) -> str:
    """'YYYYMMDD_HHMMSS' of a session log or clip name."""
    return Path(path).name.split("_", 1)[1][:15]


def _fnum(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _row(kind: str, key, locator: str, estimate: CameraBallEstimate, elapsed: float, extra):
    diag = estimate.locator
    fit = diag.line_fit if diag is not None else None
    return {
        "kind": kind,
        "shot": key,
        "locator": locator,
        "ran": diag.locator if diag is not None else None,
        "reason": diag.reason if diag is not None else None,
        "status": estimate.status,
        "tier": estimate.confidence_tier,
        "h_deg": estimate.horizontal_deg,
        "v_deg": estimate.vertical_deg,
        "support": estimate.support,
        "n_points": estimate.n_points,
        "depth": estimate.depth_source,
        "time_zero": estimate.time_zero.source if estimate.time_zero else None,
        "ball_x": diag.ball_x if diag else None,
        "ball_y": diag.ball_y if diag else None,
        "ball_d": diag.ball_diameter_px if diag else None,
        "contrast": diag.contrast if diag else None,
        "snr": diag.snr if diag else None,
        "teed": diag.teed if diag else None,
        "pose": diag.pose_source if diag else None,
        "gate": f"{diag.gate_kept}/{diag.gate_candidates}" if diag and diag.gate_kept else None,
        "line_fit": diag.line_fit_status if diag else None,
        "line_rms_mrad": fit.rms_mrad if fit else None,
        "line_h_deg": fit.horizontal_deg if fit else None,
        "elapsed_s": round(elapsed, 4),
        **extra,
    }


def _fmt(value, spec="+6.2f", empty="   -  "):
    return empty if value is None else format(value, spec)


def _stats(errors: list[float]) -> str:
    if not errors:
        return "n= 0"
    values = np.asarray(errors)
    sd = values.std(ddof=1) if len(values) > 1 else 0.0
    return (
        f"n={len(values):2d} bias {values.mean():+5.2f} sd {sd:4.2f} "
        f"rmse {math.sqrt(float(np.mean(values**2))):4.2f} max|e| {np.abs(values).max():4.2f}"
    )


def compare(rows: list[dict], pairs: dict, not_strikes: set[int], locators: list[str]) -> None:
    """Coverage and accuracy against Toptracer (validation only)."""
    by = {(row["locator"], row["shot"]): row for row in rows if row["kind"] == "shot"}
    paired = sorted(
        number
        for number, pair in pairs.items()
        if _fnum(pair.get("tt_hla_deg")) is not None and number not in not_strikes
    )
    toptracer = {number: _fnum(pairs[number]["tt_hla_deg"]) for number in paired}
    curve = {number: _fnum(pairs[number].get("tt_curve_m")) for number in paired}
    groups = {
        "driver": lambda block: block == "driver",
        "7w": lambda block: block == "7w",
        "irons/PW": lambda block: block in IRON_BLOCKS,
        "all": lambda block: True,
    }
    print("\n=== coverage: real paired shots with a camera direction")
    for locator in locators:
        cells = []
        for name, member in groups.items():
            shots = [n for n in paired if member(pairs[n]["block"])]
            got = [n for n in shots if by.get((locator, n), {}).get("h_deg") is not None]
            cells.append(f"{name} {len(got)}/{len(shots)}")
        print(f"  {locator:12s} " + "  ".join(cells))
    print("\n=== camera - Toptracer horizontal (deg), each locator on the shots it measured")
    for locator in locators:
        print(f"-- {locator}")
        for name, member in groups.items():
            for label, straight in (("all", False), (f"|curve|<{STRAIGHT_CURVE_M:g}m", True)):
                shots = [
                    n
                    for n in paired
                    if member(pairs[n]["block"])
                    and by.get((locator, n), {}).get("h_deg") is not None
                    and (
                        not straight or (curve[n] is not None and abs(curve[n]) < STRAIGHT_CURVE_M)
                    )
                ]
                errors = [by[(locator, n)]["h_deg"] - toptracer[n] for n in shots]
                print(f"   {name:8s} {label:12s} {_stats(errors)}")
    if "legacy" in locators and len(locators) > 1:
        legacy_irons = [
            n
            for n in paired
            if pairs[n]["block"] in IRON_BLOCKS
            and by.get(("legacy", n), {}).get("h_deg") is not None
        ]
        print(f"\n=== irons the legacy locator measured ({len(legacy_irons)} shots)")
        for locator in locators:
            shots = [n for n in legacy_irons if by.get((locator, n), {}).get("h_deg") is not None]
            errors = [by[(locator, n)]["h_deg"] - toptracer[n] for n in shots]
            print(f"   {locator:12s} {_stats(errors)}")
    print("\n=== not strikes (must not produce a direction)")
    for locator in locators:
        cells = [
            f"OF{n} {by.get((locator, n), {}).get('status')}"
            for n in sorted(not_strikes)
            if (locator, n) in by
        ]
        print(f"  {locator:12s} " + "; ".join(cells))


def _timing(rows: list[dict], locators: list[str]) -> None:
    print("\n=== estimate time per OPS shot (s, this machine)")
    for locator in locators:
        times = [
            row["elapsed_s"] for row in rows if row["locator"] == locator and row["kind"] == "shot"
        ]
        if not times:
            continue
        p90 = float(np.percentile(times, 90))
        print(
            f"  {locator:12s} n={len(times)} median {statistics.median(times):.3f} "
            f"p90 {p90:.3f} max {max(times):.3f}"
        )


def main(argv: list[str] | None = None) -> int:  # pylint: disable=too-many-locals
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--session", type=Path, required=True, action="append")
    parser.add_argument("--camera-dir", type=Path, required=True)
    parser.add_argument("--iwr-dir", type=Path, required=True)
    parser.add_argument("--intrinsics", type=Path, default=None)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--pairs", type=Path, default=None)
    parser.add_argument("--not-strikes", default="")
    parser.add_argument("--locator", choices=(*BALL_LOCATORS, "both"), default="both")
    parser.add_argument("--shots", default="", help="Comma-separated shot numbers only")
    parser.add_argument("--false-triggers", action="store_true")
    parser.add_argument("--false-trigger-speed-mph", type=float, default=100.0)
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args(argv)

    locators = list(BALL_LOCATORS) if args.locator == "both" else [args.locator]
    not_strikes = {int(item) for item in args.not_strikes.split(",") if item.strip()}
    only = {int(item) for item in args.shots.split(",") if item.strip()}
    intrinsics = load_camera_intrinsics(args.intrinsics) if args.intrinsics else None
    rows: list[dict] = []
    warmed = False
    sessions = sorted(
        ((path, load_session(path)) for path in args.session), key=lambda item: _stamp(item[0])
    )
    shot_clips = {
        Path(camera["capture_path"]).name
        for _path, session in sessions
        for camera in session["camera"].values()
    }
    starts = [_stamp(path) for path, _session in sessions]
    for position, (session_path, session) in enumerate(sessions):
        config = session["config"]
        evidence = replay_ti(session, args.iwr_dir, args.calibration)
        zone = zone_for(config, intrinsics)
        print(f"# {session_path.name}: {len(session['shots'])} shots, zone {zone}")
        states = {name: _Locator(name, zone) for name in locators}
        for number, shot in sorted(session["shots"].items()):
            if only and number not in only:
                continue
            camera = session["camera"].get(number)
            clip_name = Path(camera["capture_path"]).name if camera else None
            if clip_name is None or not (args.camera_dir / clip_name).exists():
                print(f"OF{number:2d} no clip")
                continue
            frames, timestamps, trigger_ns, metadata = _clip(args.camera_dir, clip_name)
            settings = metadata.get("settings") or {}
            kwargs = {
                "frames": frames,
                "timestamps_ns": timestamps,
                "trigger_ns": trigger_ns,
                "range_evidence": evidence.get(number),
                "geometry": geometry_for(config, settings, intrinsics),
                "ops_ball_speed_mph": shot.get("ball_speed_raw_mph") or shot["ball_speed_mph"],
                "iwr_vertical_deg": shot.get("launch_angle_vertical"),
            }
            if not warmed:
                _Locator(locators[0], zone).estimate(shadow=True, **kwargs)
                warmed = True
            shadow = (
                shot.get("experimental_camera_horizontal_status") == "rejected_lighting_quality"
            )
            for name, state in states.items():
                estimate, elapsed = state.estimate(shadow=shadow, **kwargs)
                decision = select_camera_assisted_horizontal(
                    estimate,
                    iwr_horizontal_deg=shot.get("iwr6843_horizontal_deg"),
                    iwr_confidence=shot.get("iwr6843_horizontal_confidence"),
                )
                row = _row(
                    "shot",
                    number,
                    name,
                    estimate,
                    elapsed,
                    {"club": shot.get("club"), "decision": decision.status, "shadow": shadow},
                )
                rows.append(row)
                print(
                    f"{name:11s} OF{number:2d} {'S' if shadow else ' '} {shot.get('club', ''):9s} "
                    f"{row['ran']:15s} {estimate.status:30s} {estimate.confidence_tier:12s} "
                    f"h={_fmt(estimate.horizontal_deg)} v={_fmt(estimate.vertical_deg, '+5.1f')} "
                    f"C={_fmt(row['contrast'], '5.1f', '  -  ')} "
                    f"SNR={_fmt(row['snr'], '5.1f', '  -  ')} teed={row['teed']} "
                    f"pose={row['pose']} line={row['line_fit']}"
                    f"({_fmt(row['line_rms_mrad'], '.2f', '-')}) {elapsed:.2f}s",
                    flush=True,
                )
        if args.false_triggers and not only:
            # Clips without an OPS shot from this session's time span (clips
            # before the first session count for the first).
            end = starts[position + 1] if position + 1 < len(starts) else "~"
            begin = starts[position] if position else ""
            for clip in sorted(path.name for path in args.camera_dir.glob("camera_*")):
                if (
                    clip in shot_clips
                    or not begin <= _stamp(clip) < end
                    or not (args.camera_dir / clip / "frames.npz").exists()
                ):
                    continue
                frames, timestamps, trigger_ns, metadata = _clip(args.camera_dir, clip)
                settings = metadata.get("settings") or {}
                kwargs = {
                    "frames": frames,
                    "timestamps_ns": timestamps,
                    "trigger_ns": trigger_ns,
                    "range_evidence": None,
                    "geometry": geometry_for(config, settings, intrinsics),
                    "ops_ball_speed_mph": args.false_trigger_speed_mph,
                    "iwr_vertical_deg": None,
                }
                for name, state in states.items():
                    estimate, elapsed = state.estimate(shadow=True, **kwargs)
                    rows.append(_row("false_trigger", clip, name, estimate, elapsed, {}))
            for name in locators:
                found = [
                    row
                    for row in rows
                    if row["kind"] == "false_trigger"
                    and row["locator"] == name
                    and begin <= _stamp(row["shot"]) < end
                ]
                directions = [row for row in found if row["h_deg"] is not None]
                print(
                    f"# false triggers ({name}): {len(found)} clips, "
                    f"{len(directions)} with a direction "
                    + " ".join(f"{row['shot']}:{row['h_deg']:+.1f}" for row in directions)
                )

    _timing(rows, locators)
    if args.pairs is not None:
        with args.pairs.open(encoding="utf-8") as handle:
            pairs = {int(row["of_shot"]): row for row in csv.DictReader(handle)}
        compare(rows, pairs, not_strikes, locators)
    else:
        print("\n=== coverage (OPS shots with a camera direction)")
        for name in locators:
            shots = [row for row in rows if row["kind"] == "shot" and row["locator"] == name]
            strikes = [row for row in shots if row["shot"] not in not_strikes]
            got = [row for row in strikes if row["h_deg"] is not None]
            print(f"  {name:12s} {len(got)}/{len(strikes)}")
    if args.csv is not None and rows:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key) for key in rows[0]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
