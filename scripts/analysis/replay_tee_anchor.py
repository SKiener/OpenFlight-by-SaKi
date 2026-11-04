"""Replay the camera trigger gate and its tee anchor over a range session's clips.

Every clip of the camera directory (frames.npz + metadata.json) is replayed in
session order through the production code: the live gate
(``LiveDepartureCheck`` with the confirmed anchor, frames delivered at their
host times, ``--deadline-ms``), the observation check
(``check_ball_departure`` with the anchor hint and the hitting zone) and the
``TeeAnchor`` inside a ``CameraCaptureRuntime`` without a camera, fed through
the runtime's own calls: ``note_accepted_shot`` for the clips of OPS-accepted
shots (the session log's ``camera_capture`` entries), ``note_trigger_check``
for every clip, ``note_blocked_edge`` for every blocked edge, and the
session's camera restarts (``config_change``) through
``update_exposure_mode``. The gate anchor before and after each restart is
printed as found, not assumed.

``--mode camera`` replays ``--ops-trigger-mode camera``: a blocked shot never
reaches the OPS, so it teaches the anchor nothing, but a blocked clip whose
ball left away from the anchor contradicts it. ``--mode observation`` replays
the default passthrough session (nothing is blocked), where every OPS-accepted
shot teaches the anchor; its live gate columns are what a passthrough gate with
a camera logs as ``gate_shadow_*``.

Strikes are the OPS-accepted shots minus ``--not-strikes`` (practice swings and
phantoms, known from the range report; the tool cannot tell them apart). A
strike's ball counts as visible when the clip's own before/after difference
inside the zone sits on a ball-sized spot with at least ``MIN_CONTRAST``
(independent of the anchor).

Usage::

    uv run --extra camera python scripts/analysis/replay_tee_anchor.py \\
        --session session_20261019_181202_range.jsonl \\
        --camera-dir range/camera --intrinsics camera-intrinsics.json \\
        --not-strikes 1,35,52,53 --mode camera --csv replay.csv

The raw clips stay outside the repository.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, namedtuple
from pathlib import Path

import numpy as np

from openflight.camera.capture_runtime import (
    CameraCaptureRuntime,
    CameraCaptureSettings,
    SavedCameraCapture,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.intrinsics import load_camera_intrinsics
from openflight.camera.tee_anchor import TeeAnchor
from openflight.camera.tee_zone import TeeZone, tee_zone
from openflight.camera.trigger_check import (
    GONE_MIN_CHANGE,
    MIN_CONTRAST,
    PRESENT_MAX_CHANGE,
    REFERENCE_END_MS,
    REFERENCE_FRAMES,
    LiveDepartureCheck,
    check_ball_departure,
    exposure_timestamps_ns,
    find_balls,
    match_radius_px,
    spot_contrast,
)
from openflight.trigger_gate import (
    DECISION_MARGIN_MS,
    DEFAULT_DEADLINE_MS,
    REFERENCE_LOOKBACK_MS,
)

# The gate worker's first live_frames_after() call, after the GPIO callback.
FIRST_CALL_MS = 0.5
# Frames 30-60 ms after the edge show the spot the ball left.
AFTER_STRIKE_MS = (30.0, 60.0)
# Acceptance (task A, 2026-10-25): no strike blocked, and at least this share
# of the strikes with a visible ball judged departed by the live gate.
DEPARTED_TARGET = 0.9

Frame = namedtuple("Frame", "image host_timestamp_ns sensor_timestamp_ns exposure_us analogue_gain")


def _local_stamp(text: str) -> str:
    """'2026-10-19T18:12:02.69' or a clip name -> sortable 'YYYYMMDD_HHMMSS'."""
    if text.startswith("camera_"):
        return text[len("camera_") : len("camera_") + 15]
    return text[:19].replace("-", "").replace(":", "").replace("T", "_")


def load_session(path: Path) -> dict:
    """Start config, OPS-accepted shot clips and camera restarts of a session JSONL.

    Each restart is ``(stamp, camera config)``, the camera config being the
    start config with every change up to it applied.
    """
    session = {"config": {}, "start": "", "end": "", "shots": {}, "restarts": []}
    camera: dict = {}
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            kind = entry.get("type")
            if entry.get("ts"):
                session["end"] = max(session["end"], _local_stamp(entry["ts"]))
            if kind == "session_start":
                session["config"] = entry.get("config") or {}
                session["start"] = _local_stamp(entry.get("start_time") or entry["ts"])
                camera = dict(session["config"].get("camera_capture") or {})
            elif kind == "camera_capture" and entry.get("capture_path"):
                session["shots"][Path(entry["capture_path"]).name] = entry.get("shot_number")
            elif kind == "config_change" and "camera_capture" in (entry.get("config") or {}):
                camera = {**camera, **entry["config"]["camera_capture"]}
                session["restarts"].append((_local_stamp(entry["ts"]), camera))
    return session


class OfflineCameraRuntime(CameraCaptureRuntime):
    """The capture runtime without a camera: its restarts only flip ``running``."""

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False


def _settings(camera: dict) -> CameraCaptureSettings:
    """Capture settings of a session's camera config (defaults for missing keys)."""
    defaults = CameraCaptureSettings()
    crop = camera.get("scaler_crop")
    return CameraCaptureSettings(
        width=int(camera.get("width", defaults.width)),
        height=int(camera.get("height", defaults.height)),
        fps=float(camera.get("fps", defaults.fps)),
        exposure_us=int(camera.get("exposure_us", defaults.exposure_us)),
        gain=float(camera.get("gain", defaults.gain)),
        auto_exposure=bool(camera.get("auto_exposure_enabled", defaults.auto_exposure)),
        rotate_180=bool(camera.get("rotate_180", defaults.rotate_180)),
        mirror_horizontal=bool(camera.get("mirror_horizontal", defaults.mirror_horizontal)),
        scaler_crop=None if crop is None else tuple(int(value) for value in crop),
    )


def offline_runtime(config: dict, anchor: TeeAnchor, output_dir: Path) -> OfflineCameraRuntime:
    """A running camera-less runtime at the session's start settings around ``anchor``."""
    runtime = OfflineCameraRuntime(
        output_dir=output_dir,
        settings=_settings(config.get("camera_capture") or {}),
        use_gpio_trigger=False,
        tee_anchor=anchor,
    )
    runtime.start()
    return runtime


def _geometry(settings: CameraCaptureSettings) -> tuple:
    return (
        settings.width,
        settings.height,
        settings.scaler_crop,
        settings.rotate_180,
        settings.mirror_horizontal,
    )


def _anchor_text(anchor: ReferenceBall | None) -> str | None:
    return None if anchor is None else f"{anchor.x:.1f},{anchor.y:.1f}"


def apply_restart(runtime: CameraCaptureRuntime, camera: dict) -> dict:
    """Replay one camera config change through the runtime's restart; how the gate anchor fared."""
    before = runtime.trigger_check_anchor()
    proposed = _settings(camera)
    if _geometry(proposed) != _geometry(runtime.settings):
        outcome = "geometry change not replayed"
    else:
        if proposed.auto_exposure:
            runtime.update_exposure_mode(automatic=True)
        else:
            runtime.update_exposure_mode(
                automatic=False, exposure_us=proposed.exposure_us, gain=proposed.gain
            )
        after = runtime.trigger_check_anchor()
        if before is None and after is None:
            outcome = "no anchor"
        elif before == after:
            outcome = "kept"
        else:
            outcome = "reset" if after is None else "changed"
    return {
        "before": _anchor_text(before),
        "after": _anchor_text(runtime.trigger_check_anchor()),
        "outcome": outcome,
    }


def zone_from_config(config: dict, intrinsics) -> TeeZone | None:
    """The hitting zone the server computes for this session's mount and TI geometry."""
    camera, ti = config.get("camera_capture") or {}, config.get("iwr6843") or {}
    if not camera or not ti:
        return None
    return tee_zone(
        intrinsics,
        camera_height_m=float(camera["mount_height_m"]),
        camera_lateral_offset_m=float(camera.get("lateral_offset_m", 0.0)),
        camera_forward_offset_m=float(camera.get("forward_offset_m", 0.0)),
        tee_range_m=float(ti["tee_slant_range_m"]),
        radar_height_m=float(ti["radar_height_m"]),
        ball_height_m=float(ti["ball_height_m"]),
        horizontal_pixel_sign=-1.0 if camera.get("mirror_horizontal") else 1.0,
    )


def session_clips(camera_dir: Path, start: str = "", end: str = "~") -> list[Path]:
    """Clip directories with frames, in time order, from session start to its last entry."""
    return sorted(
        path.parent
        for path in Path(camera_dir).glob("camera_*/frames.npz")
        if start <= _local_stamp(path.parent.name) <= end
    )


def load_clip(clip: Path):
    """(frames, host ns, sensor ns, edge ns) of one saved capture."""
    with np.load(clip / "frames.npz") as archive:
        return (
            archive["frames"],
            np.asarray(archive["host_timestamp_ns"], dtype=np.int64),
            np.asarray(archive["sensor_timestamp_ns"], dtype=np.int64),
            int(archive["trigger_host_timestamp_ns"]),
        )


def clip_extras(clip: Path, edge_ns: int) -> tuple[float, np.ndarray | None, np.ndarray | None]:
    """(edge on the wall clock, per-frame exposure us, per-frame gain) of a saved capture.

    The wall-clock edge is what the gate reports; clips without it use their
    monotonic edge. Exposure and gain are None when the clip lacks them.
    """
    with np.load(clip / "frames.npz") as archive:
        names = set(archive.files)
        epoch = (
            float(archive["trigger_epoch_timestamp"])
            if "trigger_epoch_timestamp" in names
            else edge_ns / 1e9
        )
        if {"exposure_us", "analogue_gain"} <= names:
            return epoch, np.asarray(archive["exposure_us"]), np.asarray(archive["analogue_gain"])
    return epoch, None, None


def simulate_live_gate(  # pylint: disable=too-many-arguments
    frames,
    host,
    sensor,
    edge_ns,
    anchor,
    *,
    deadline_ms=DEFAULT_DEADLINE_MS,
    exposure_us=None,
    gain=None,
) -> dict:
    """Feed the frames as ``TriggerGate._camera_verdict`` sees them (compute time ignored)."""
    if anchor is None:
        return {
            "decision": "unclear",
            "reason": "no_anchor",
            "departure_ms": None,
            "states": "",
            "spot_contrast": None,
        }
    check = LiveDepartureCheck(edge_ns, anchor)
    collect_until = edge_ns + round((deadline_ms - DECISION_MARGIN_MS) * 1e6)
    after = edge_ns - round(REFERENCE_LOOKBACK_MS * 1e6)
    first_call = edge_ns + round(FIRST_CALL_MS * 1e6)
    pending = [index for index in np.argsort(host) if host[index] > after]
    first = [index for index in pending if host[index] <= first_call]
    batches = ([first] if first else []) + [[i] for i in pending if host[i] > first_call]
    verdict = None
    for batch in batches:
        if host[batch[-1]] > collect_until:
            break
        verdict = check.add(
            [
                Frame(
                    frames[i],
                    int(host[i]),
                    int(sensor[i]),
                    None if exposure_us is None else int(exposure_us[i]),
                    None if gain is None else float(gain[i]),
                )
                for i in batch
            ]
        )
        if verdict is not None:
            break
    verdict = verdict or check.finish()
    return {
        "decision": verdict.decision,
        "reason": verdict.reason,
        "departure_ms": verdict.first_departure_ms_after_edge,
        "states": check.states,
        "spot_contrast": check.spot_contrast,
    }


def strike_ball(frames, timestamps, edge_ns, zone: TeeZone) -> dict:
    """The strike's ball, found from the clip alone (no anchor): a reference for the replay.

    The most ball-like compact change in the zone between the reference frames
    and 30-60 ms after the edge (the contrast kernel run over the change image,
    so a moving shoe or club, larger than a ball, does not win). The ball is
    ``visible`` when the spot there has the check's ``MIN_CONTRAST``, sat still
    through the reference frames (first vs. last third differ by less than
    ``PRESENT_MAX_CHANGE`` of that contrast; legs walking past do not) and its
    core changed by at least ``GONE_MIN_CHANGE`` of that contrast (it left).
    """
    offsets = (np.asarray(timestamps) - edge_ns) / 1e6
    reference = frames[np.flatnonzero(offsets <= REFERENCE_END_MS)[:REFERENCE_FRAMES]]
    after = frames[
        np.flatnonzero((offsets >= AFTER_STRIKE_MS[0]) & (offsets <= AFTER_STRIKE_MS[1]))
    ]
    nothing = {"x": None, "y": None, "contrast": None, "visible": False}
    if len(reference) < 3 or len(after) < 3:
        return nothing
    change = np.abs(
        np.median(reference, axis=0).astype(np.float32)
        - np.median(after, axis=0).astype(np.float32)
    )
    box = (zone.x0, zone.y0, zone.x1, zone.y1)
    moved = find_balls(change[None], box, zone.diameter_px, min_contrast=0.0)
    if not moved:
        return nothing
    spot, compact_change = moved[0]
    radius = match_radius_px(zone.diameter_px) / 2.0
    found = find_balls(
        reference,
        (
            math.floor(spot.x - radius),
            math.floor(spot.y - radius),
            math.ceil(spot.x + radius) + 1,
            math.ceil(spot.y + radius) + 1,
        ),
        zone.diameter_px,
        min_contrast=0.0,
        within=(spot.x, spot.y, radius),
    )
    ball = found[0][0] if found else spot
    contrast = spot_contrast(reference, ball)
    third = max(1, len(reference) // 3)
    early, late = spot_contrast(reference[:third], ball), spot_contrast(reference[-third:], ball)
    still = (
        contrast is not None
        and early is not None
        and late is not None
        and abs(early - late) < PRESENT_MAX_CHANGE * contrast
    )
    return {
        "x": round(ball.x),
        "y": round(ball.y),
        "contrast": contrast,
        "visible": still
        and contrast >= MIN_CONTRAST
        and abs(compact_change) >= GONE_MIN_CHANGE * contrast,
    }


def _rounded(value, digits=1):
    return None if value is None else round(float(value), digits)


def replay(  # pylint: disable=too-many-locals,too-many-arguments
    clips: list[Path],
    shots: dict,
    zone: TeeZone | None,
    *,
    mode: str = "camera",
    not_strikes: frozenset = frozenset(),
    deadline_ms: float = DEFAULT_DEADLINE_MS,
    config: dict | None = None,
    restarts: list = (),
) -> tuple[list[dict], TeeAnchor, list[dict]]:
    """One row per clip, replayed in order through gate, check and anchor; plus the restarts."""
    anchor = TeeAnchor()
    runtime = offline_runtime(config or {}, anchor, clips[0].parent if clips else Path("."))
    pending = sorted(restarts, key=lambda item: item[0])
    restart_log: list[dict] = []
    rows = []
    for sequence, clip in enumerate(clips, start=1):
        while pending and pending[0][0] <= _local_stamp(clip.name):
            stamp, camera = pending.pop(0)
            restart_log.append(
                {"stamp": stamp, "clip": clip.name, **apply_restart(runtime, camera)}
            )
        frames, host, sensor, edge_ns = load_clip(clip)
        epoch, exposure_us, gain = clip_extras(clip, edge_ns)
        timestamps = exposure_timestamps_ns(host, sensor)
        gate_anchor = runtime.trigger_check_anchor()
        live = simulate_live_gate(
            frames,
            host,
            sensor,
            edge_ns,
            gate_anchor,
            deadline_ms=deadline_ms,
            exposure_us=exposure_us,
            gain=gain,
        )
        check = check_ball_departure(
            frames,
            timestamps,
            edge_ns,
            anchor.hint(),
            zone=zone,
            exposure_gain=None if exposure_us is None else exposure_us * gain.astype(np.float64),
        )
        shot = shots.get(clip.name)
        role = "no_shot" if shot is None else ("not_strike" if shot in not_strikes else "strike")
        blocked = live["decision"] == "still_there"
        # Runtime order: the gate settles within its deadline, the clip is saved later.
        if mode == "camera" and blocked:
            runtime.note_blocked_edge(epoch)
        event = runtime.note_trigger_check(epoch, check.to_dict())
        if shot is not None and not (mode == "camera" and blocked):
            event = runtime.note_accepted_shot(
                SavedCameraCapture(
                    sequence=sequence,
                    trigger_timestamp=epoch,
                    completed_timestamp=epoch,
                    path=clip,
                    metadata={"trigger_check": check.to_dict()},
                ),
                shot_number=shot,
            )
        truth = strike_ball(frames, timestamps, edge_ns, zone) if role == "strike" and zone else {}
        departure = check.departure
        rows.append(
            {
                "clip": clip.name,
                "shot": shot,
                "role": role,
                "gate_anchor": _anchor_text(gate_anchor),
                "gate_contrast": _rounded(getattr(gate_anchor, "contrast", None)),
                "live_decision": live["decision"],
                "live_reason": live["reason"],
                "live_departure_ms": _rounded(live["departure_ms"]),
                "live_spot_contrast": _rounded(live["spot_contrast"]),
                "blocked": blocked,
                "check_decision": check.decision,
                "check_reason": check.reason,
                "check_ball": None
                if check.ball is None
                else f"{check.ball_x:.0f},{check.ball_y:.0f}",
                "check_source": check.ball_source,
                "check_contrast": _rounded(check.pre_contrast),
                "check_ms": _rounded(check.compute_ms, 2),
                "departure": None if departure is None else f"{departure.x:.0f},{departure.y:.0f}",
                "departure_source": check.departure_source,
                "departure_contrast": _rounded(check.departure_contrast),
                "event": None if event is None else event["kind"],
                "contradicted": bool(event and event["contradicted"]),
                "confirmations": anchor.diagnostics()["confirmations"],
                "truth_ball": None
                if not truth or truth["x"] is None
                else f"{truth['x']},{truth['y']}",
                "truth_contrast": _rounded(truth.get("contrast")),
                "visible": truth.get("visible"),
            }
        )
    for stamp, camera in pending:
        restart_log.append({"stamp": stamp, "clip": None, **apply_restart(runtime, camera)})
    return rows, anchor, restart_log


def _pct(part: int, whole: int) -> str:
    return f"{part}/{whole}" + (f" ({100.0 * part / whole:.1f} %)" if whole else "")


def _live_tally(rows: list[dict]) -> str:
    counts = Counter(f"{row['live_decision']}/{row['live_reason']}" for row in rows)
    return ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))


def summarize(rows: list[dict], restarts: list[dict] = ()) -> list[str]:
    """Human-readable summary lines, with the task-A acceptance verdicts."""
    strikes = [row for row in rows if row["role"] == "strike"]
    visible = [row for row in strikes if row["visible"]]
    others = [row for row in rows if row["role"] == "not_strike"]
    no_shot = [row for row in rows if row["role"] == "no_shot"]
    armed = [row for row in no_shot if row["gate_anchor"] is not None]
    blocked = sum(row["blocked"] for row in strikes)
    departed = sum(r["live_decision"] == "departed" for r in visible)
    if not visible:
        departed_verdict = "n/a (no visible strike)"
    else:
        departed_verdict = "PASS" if departed >= DEPARTED_TARGET * len(visible) else "FAIL"
    lines = [
        f"clips {len(rows)}: strikes {len(strikes)}, other OPS shots {len(others)}, "
        f"no OPS shot {len(no_shot)}",
        f"strikes blocked by the live gate: {_pct(blocked, len(strikes))}",
        f"strikes, live gate: {_live_tally(strikes)}",
        "strikes with a visible ball departed (live gate): " + _pct(departed, len(visible)),
        f"acceptance: no strike blocked: {'PASS' if blocked == 0 else 'FAIL'}; "
        f">= {100 * DEPARTED_TARGET:.0f} % of visible strikes departed (live gate): "
        f"{departed_verdict}",
        "strikes with a visible ball departed (observation check): "
        + _pct(sum(r["check_decision"] == "departed" for r in visible), len(visible)),
        "strikes seen leaving (departure spot, observation check): "
        + _pct(sum(r["departure"] is not None for r in strikes), len(strikes)),
        "no-shot clips blocked: "
        + _pct(sum(r["blocked"] for r in no_shot), len(no_shot))
        + "; while the gate had a confirmed anchor: "
        + _pct(sum(r["blocked"] for r in armed), len(armed)),
        f"no-shot clips with a confirmed anchor, live gate: {_live_tally(armed)}",
    ]
    for row in others:
        lines.append(
            f"OPS shot {row['shot']} (not a strike): live {row['live_decision']}/"
            f"{row['live_reason']}, check {row['check_decision']}/{row['check_reason']}"
            f"{', contradicted the anchor' if row['contradicted'] else ''}"
        )
    events = Counter(row["event"] for row in rows if row["event"])
    lines.append(
        "anchor events: "
        + ", ".join(f"{k} {v}" for k, v in sorted(events.items()))
        + f"; contradictions {sum(row['contradicted'] for row in rows)}"
    )
    first_armed = next((row for row in rows if row["gate_anchor"] is not None), None)
    if first_armed is not None:
        lines.append(
            f"gate could first block at clip {first_armed['clip']} "
            f"(anchor {first_armed['gate_anchor']})"
        )
    for restart in restarts:
        lines.append(
            f"camera restart {restart['stamp']} (before clip {restart['clip']}): gate anchor "
            f"{restart['before']} -> {restart['after']} ({restart['outcome']})"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    parser.add_argument("--session", type=Path, required=True, help="session JSONL")
    parser.add_argument("--camera-dir", type=Path, required=True, help="clip directories")
    parser.add_argument("--intrinsics", type=Path, required=True, help="lens calibration JSON")
    parser.add_argument("--mode", choices=("camera", "observation"), default="camera")
    parser.add_argument(
        "--not-strikes", default="", help="comma-separated OPS shot numbers without a strike"
    )
    parser.add_argument("--deadline-ms", type=float, default=DEFAULT_DEADLINE_MS)
    parser.add_argument("--csv", type=Path, help="write one row per clip")
    args = parser.parse_args(argv)

    session = load_session(args.session)
    zone = zone_from_config(session["config"], load_camera_intrinsics(args.intrinsics))
    print(f"zone: {zone.to_dict() if zone else None}")
    not_strikes = frozenset(int(item) for item in args.not_strikes.split(",") if item.strip())
    rows, anchor, restarts = replay(
        session_clips(args.camera_dir, session["start"], session["end"] or "~"),
        session["shots"],
        zone,
        mode=args.mode,
        not_strikes=not_strikes,
        deadline_ms=args.deadline_ms,
        config=session["config"],
        restarts=session["restarts"],
    )
    print(f"mode: {args.mode}")
    for line in summarize(rows, restarts):
        print(line)
    print(f"final anchor: {json.dumps(anchor.diagnostics())}")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["clip"])
            writer.writeheader()
            writer.writerows(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
