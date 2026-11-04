"""Replay the camera angle reference (``openflight.angle_reference``) over a range session.

Every OPS shot of the session log runs, in shot order, through the server's own
functions as the live pipeline calls them:

* ``server._process_iwr6843_angle``: the TI measurement recomputed from the saved
  L3 dump (the session's TI configuration and logged inclinometer tilt), with the
  vertical plausibility guard;
* ``server._fuse_camera_ball_flight``: the clip's ball flight with the session's
  mount geometry, lens model and hitting zone, one tracker and ground anchor for
  the whole session, skipped like live for clips the exposure check marked
  ineligible for ball analysis;
* ``server._apply_angle_reference`` (camera vertical wins, session TI offsets)
  and ``server._ensure_user_facing_launch_angles`` (the estimated fallback).

There is no tapped target in old sessions, so the start directions are those of
the session's geometry (``--camera-capture-yaw-from-ball``); the yaw part of the
target line needs a session with a tapped target.

Toptracer (``--pairs``: ``of_shot``, ``block``, ``tt_vla_deg``, ``tt_hla_deg``) is
read only for the final comparison; nothing is fitted to it. ``--not-strikes``
lists OPS shots that were not strikes; they are left out of the comparison.

Usage::

    uv run --frozen --extra camera python scripts/analysis/replay_angle_reference.py \\
        --session raw/session_20261019_181202_range.jsonl \\
        --camera-dir raw/range/camera --iwr-dir raw/iwr6843 \\
        --intrinsics camera-intrinsics.json --pairs pairs.csv --not-strikes 1,35,52,53

The raw clips stay outside the repository.

2026-10-25 (69 Toptracer-paired strikes, roll 0, no target; minus Toptracer, deg):

* vertical: TI raw bias +2.31, sd 0.96, rmse 2.50 (n 67); camera -1.11 / 0.45 / 1.20
  (n 56; PW -1.35, 8-iron -1.28, 7-wood -0.89, driver -0.90); shown -0.89 / 0.65 / 1.10
  (n 69: 56 camera, 12 radar_calibrated, 1 radar). TI minus camera was valid from OF14 on
  (and at OF8 and OF10; not at OF48), its median +2.9 to +3.8 (end +3.84, spread 0.73). On
  the 12 8-irons whose camera analysis the exposure check withheld (OF54-65),
  radar_calibrated read +0.11 / 0.47 / 0.46 against +3.03 / 0.47 / 3.06 for the raw TI.
  OF33 (driver, direction withheld by the line fit, TI 0.08, Toptracer 1.0) keeps the TI:
  corrected by +3.00 it would read -2.92, which fails the plausibility guard (bounds_0_45).
* horizontal: TI minus camera was valid only from OF42 to OF69 (the compressed TI keeps
  the spread above 1 deg otherwise). It is logged, never applied: on the 12 TI-only
  8-irons the fixed -7.7 deg reads -0.73 / 1.60 / 1.70, the logged +1.62 offset would
  read -2.34 / 1.60 / 2.80 (it was learned on drivers that start left). Shown, all
  69: +0.43 / 1.92 / 1.95. The target-line yaw needs a session with a tapped target.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from replay_camera_direction import (
    DEFAULT_CALIBRATION,
    IRON_BLOCKS,
    _Capture,
    load_session,
    ti_runtime,
    zone_for,
)

from openflight import server
from openflight.angle_reference import AngleReference
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.departed_ball import GroundBallAnchor
from openflight.camera.intrinsics import load_camera_intrinsics
from openflight.clubs.bag import club_category
from openflight.launch_monitor import Shot

GROUPS = {
    "PW": lambda block: block == "PW",
    "8i": lambda block: block in IRON_BLOCKS and block != "PW",
    "7w": lambda block: block == "7w",
    "driver": lambda block: block == "driver",
    "all": lambda block: True,
}


def _configure_server(session: dict, runtime, zone, intrinsics) -> None:
    """Point the server's module state at this session (no hardware, no socket, no log)."""
    server.iwr6843_runtime = runtime
    server.camera_capture_config = dict(session["config"]["camera_capture"])
    server.camera_capture_runtime = SimpleNamespace(active_tee_zone=lambda: zone)
    server.camera_intrinsics = intrinsics
    server.camera_ball_flight_reference_tracker = ReferenceBallTracker()
    server.camera_ball_flight_ground_anchor = GroundBallAnchor()
    server.camera_cv_worker = None
    server.angle_reference = AngleReference()
    server.get_session_logger = lambda: None
    server.socketio.emit = lambda *_args, **_kwargs: None


def _ball_eligible(metadata: dict) -> bool:
    """As ``server._fuse_camera_measurements``: the clip's own exposure verdict."""
    exposure = metadata.get("auto_exposure")
    if not isinstance(exposure, dict):
        return True
    return bool(exposure.get("analysis_eligible")) or exposure.get("ball_analysis_eligible") is True


def replay_shot(number: int, logged: dict, session: dict, args, monitor) -> Shot:
    """One shot through the server's TI, camera and angle-reference stages."""
    selection = logged.get("club") or "driver"
    shot = Shot(
        ball_speed_mph=logged.get("ball_speed_raw_mph") or logged["ball_speed_mph"],
        timestamp=datetime.fromisoformat(logged["timestamp"]),
        shot_number=number,
        impact_timestamp=logged.get("impact_timestamp"),
        club_speed_mph=logged.get("club_speed_mph"),
        club=club_category(selection),
        club_selection=selection,
        inclinometer=logged.get("inclinometer"),
    )
    record = session["ti"].get(number) or {}
    dump = args.iwr_dir / Path(record["capture_path"]).name if record.get("capture_path") else None
    monitor.next = _Capture(dump.read_bytes()) if dump is not None and dump.exists() else None
    server._process_iwr6843_angle(shot)  # pylint: disable=protected-access
    camera = session["camera"].get(number)
    clip = args.camera_dir / Path(camera["capture_path"]).name if camera else None
    if clip is not None and (clip / "frames.npz").exists():
        metadata = json.loads((clip / "metadata.json").read_text(encoding="utf-8"))
        if _ball_eligible(metadata):
            capture = SimpleNamespace(valid=True, path=clip, metadata=metadata)
            server._fuse_camera_ball_flight(shot, capture)  # pylint: disable=protected-access
        else:
            shot.experimental_camera_horizontal_status = "rejected_lighting_quality"
    server._apply_angle_reference(shot)  # pylint: disable=protected-access
    server._ensure_user_facing_launch_angles(shot)  # pylint: disable=protected-access
    return shot


def _fnum(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _stats(errors: list[float]) -> str:
    if not errors:
        return "n= 0"
    values = np.asarray(errors)
    sd = values.std(ddof=1) if len(values) > 1 else 0.0
    return (
        f"n={len(values):2d} bias {values.mean():+5.2f} sd {sd:4.2f} "
        f"rmse {math.sqrt(float(np.mean(values**2))):4.2f} max|e| {np.abs(values).max():4.2f}"
    )


def _rows(shots: dict[int, Shot]) -> list[dict]:
    rows = []
    for number, shot in sorted(shots.items()):
        reference = shot.angle_reference or {}
        vertical = reference.get("vertical") or {}
        horizontal = reference.get("horizontal") or {}
        rows.append(
            {
                "shot": number,
                "club": shot.club_selection,
                "ti_v": shot.iwr6843_vertical_deg,
                "cam_v": shot.experimental_camera_vertical_deg,
                "final_v": shot.launch_angle_vertical,
                "v_source": shot.launch_angle_vertical_source,
                "v_offset": (vertical.get("offset") or {}).get("offset_deg"),
                "v_valid": (vertical.get("offset") or {}).get("valid"),
                "ti_h": shot.iwr6843_horizontal_deg,
                "cam_h": horizontal.get("camera_deg"),
                "final_h": shot.launch_angle_horizontal,
                "h_source": shot.launch_angle_horizontal_source,
                "h_offset": (horizontal.get("offset") or {}).get("offset_deg"),
                "h_valid": (horizontal.get("offset") or {}).get("valid"),
                # The logged (never applied) horizontal offset on a TI-only shot.
                "h_shadow": (
                    shot.iwr6843_horizontal_deg - horizontal["offset"]["offset_deg"]
                    if shot.launch_angle_horizontal_source == "radar"
                    and shot.iwr6843_horizontal_deg is not None
                    and (horizontal.get("offset") or {}).get("valid")
                    else None
                ),
                "v_rejected": (vertical.get("calibration_rejected") or {}).get("rule"),
                "cam_status": shot.experimental_camera_horizontal_status,
            }
        )
    return rows


def _print_rows(rows: list[dict]) -> None:
    def cell(value, spec="+6.2f"):
        return "   -  " if value is None else format(value, spec)

    print(
        "shot club       TI_v  cam_v final_v source            off_v ok | "
        "TI_h  cam_h final_h source              off_h ok  rejected"
    )
    for row in rows:
        print(
            f"OF{row['shot']:<3d}{row['club'] or '':10s}{cell(row['ti_v'])} {cell(row['cam_v'])} "
            f"{cell(row['final_v'])} {row['v_source'] or '-':17s}{cell(row['v_offset'])} "
            f"{'y' if row['v_valid'] else '-'}  | {cell(row['ti_h'])} {cell(row['cam_h'])} "
            f"{cell(row['final_h'])} {row['h_source'] or '-':20s}{cell(row['h_offset'])} "
            f"{'y' if row['h_valid'] else '-'}  {row['v_rejected'] or ''}"
        )


def _source_is(key: str, source: str):
    """A row filter: the shown angle came from ``source``."""
    return lambda row: row[key] == source


def _has_shadow(row: dict) -> bool:
    """A TI-only direction while the logged horizontal offset was valid."""
    return row["h_shadow"] is not None


def _has_both(row: dict) -> bool:
    return row["ti_v"] is not None and row["cam_v"] is not None


def compare(rows: list[dict], pairs: dict, not_strikes: set[int]) -> None:
    """Vertical and horizontal launch against Toptracer (validation only)."""
    by = {
        row["shot"]: row for row in rows if row["shot"] in pairs and row["shot"] not in not_strikes
    }

    def errors(member, value_key, truth_key, condition=lambda _row: True):
        result = []
        for number, row in by.items():
            truth = _fnum(pairs[number].get(truth_key))
            if (
                truth is None
                or row[value_key] is None
                or not member(pairs[number]["block"])
                or not condition(row)
            ):
                continue
            result.append(row[value_key] - truth)
        return result

    print("\n=== vertical launch - Toptracer (deg)")
    for name, member in GROUPS.items():
        print(f"-- {name}")
        print(f"   TI raw              {_stats(errors(member, 'ti_v', 'tt_vla_deg'))}")
        print(f"   camera              {_stats(errors(member, 'cam_v', 'tt_vla_deg'))}")
        print(f"   final shown         {_stats(errors(member, 'final_v', 'tt_vla_deg'))}")
        for source in ("camera", "radar", "radar_calibrated", "estimated"):
            picked = _source_is("v_source", source)
            print(
                f"     final {source:13s} {_stats(errors(member, 'final_v', 'tt_vla_deg', picked))}"
            )
        calibrated = _source_is("v_source", "radar_calibrated")
        print(f"     same shots, TI raw {_stats(errors(member, 'ti_v', 'tt_vla_deg', calibrated))}")
        print(f"   shots with both: TI {_stats(errors(member, 'ti_v', 'tt_vla_deg', _has_both))}")
        print(f"                camera {_stats(errors(member, 'cam_v', 'tt_vla_deg', _has_both))}")

    print("\n=== horizontal launch - Toptracer (deg); no tapped target in this session")
    for name, member in GROUPS.items():
        print(f"-- {name}")
        print(f"   TI (fixed offset)   {_stats(errors(member, 'ti_h', 'tt_hla_deg'))}")
        print(f"   camera              {_stats(errors(member, 'cam_h', 'tt_hla_deg'))}")
        print(f"   final shown         {_stats(errors(member, 'final_h', 'tt_hla_deg'))}")
        radar = errors(member, "final_h", "tt_hla_deg", _source_is("h_source", "radar"))
        print(f"     final radar (fixed)    {_stats(radar)}")
        shown = errors(member, "final_h", "tt_hla_deg", _has_shadow)
        shadow = errors(member, "h_shadow", "tt_hla_deg", _has_shadow)
        print(f"     TI-only, offset valid  {_stats(shown)}")
        print(f"     same, logged offset    {_stats(shadow)} (not applied)")


def main(argv: list[str] | None = None) -> int:
    """Replay the session, print every shot and, with ``--pairs``, the Toptracer comparison."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--camera-dir", type=Path, required=True)
    parser.add_argument("--iwr-dir", type=Path, required=True)
    parser.add_argument("--intrinsics", type=Path, default=None)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--pairs", type=Path, default=None)
    parser.add_argument("--not-strikes", default="")
    parser.add_argument("--csv", type=Path, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)

    session = load_session(args.session)
    intrinsics = load_camera_intrinsics(args.intrinsics) if args.intrinsics else None
    runtime, monitor = ti_runtime(session, args.calibration)
    _configure_server(session, runtime, zone_for(session["config"], intrinsics), intrinsics)
    shots = {
        number: replay_shot(number, logged, session, args, monitor)
        for number, logged in sorted(session["shots"].items())
    }
    rows = _rows(shots)
    _print_rows(rows)
    state = server.angle_reference.state()
    print(
        f"\n# end of session: vertical {state['vertical']}\n#                 horizontal "
        f"{state['horizontal']}"
    )
    not_strikes = {int(item) for item in args.not_strikes.split(",") if item.strip()}
    if args.pairs is not None:
        with args.pairs.open(encoding="utf-8") as handle:
            pairs = {int(row["of_shot"]): row for row in csv.DictReader(handle)}
        compare(rows, pairs, not_strikes)
    if args.csv is not None and rows:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
