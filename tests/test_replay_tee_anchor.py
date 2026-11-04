"""Synthetic-session test of scripts/analysis/replay_tee_anchor.py."""

import csv
import importlib.util
import json
from pathlib import Path

import numpy as np

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analysis" / "replay_tee_anchor.py"
spec = importlib.util.spec_from_file_location("replay_tee_anchor", SCRIPT)
replay_tee_anchor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_tee_anchor)

WIDTH, HEIGHT = 240, 160
FRAMES, PRE = 61, 45
INTERVAL_NS = 3_472_000
DELIVERY_NS = 4_600_000
BALL = (126.0, 113.0)
# A second ball in the hitting zone, 3 diameters beside the tee.
OTHER = (156.0, 113.0)
RADIUS = 5
# 2026-10-25 mount and TI geometry; a 240x160 pinhole puts the tee near BALL.
CONFIG = {
    "camera_capture": {
        "mount_height_m": 0.182,
        "lateral_offset_m": -0.025,
        "forward_offset_m": 0.007,
        "mirror_horizontal": False,
    },
    "iwr6843": {"tee_slant_range_m": 1.5, "radar_height_m": 0.132, "ball_height_m": 0.02135},
}


def write_clip(directory: Path, motion: str, seed: int) -> None:
    """``fast``: the teed ball leaves; ``stays``; ``other_leaves``: it stays, OTHER leaves."""
    rng = np.random.default_rng(seed)
    texture = rng.integers(-6, 7, size=(HEIGHT, WIDTH))
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    frames = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(FRAMES):
        image = 60.0 + texture + rng.normal(0.0, 3.0, size=(HEIGHT, WIDTH))
        if motion in ("stays", "other_leaves") or index < PRE:
            image[(xx - BALL[0]) ** 2 + (yy - BALL[1]) ** 2 <= RADIUS**2] = 220.0
        if motion == "other_leaves" and index < PRE:
            image[(xx - OTHER[0]) ** 2 + (yy - OTHER[1]) ** 2 <= RADIUS**2] = 220.0
        frames[index] = np.clip(image, 0, 255).astype(np.uint8)
    sensor = np.arange(FRAMES, dtype=np.int64) * INTERVAL_NS + 10_000_000_000
    directory.mkdir(parents=True)
    np.savez(
        directory / "frames.npz",
        frames=frames,
        sensor_timestamp_ns=sensor,
        host_timestamp_ns=sensor + DELIVERY_NS,
        exposure_us=np.full(FRAMES, 100, dtype=np.int32),
        analogue_gain=np.full(FRAMES, 2.0, dtype=np.float32),
        trigger_host_timestamp_ns=np.int64(sensor[PRE - 1] + 2_000_000),
        trigger_epoch_timestamp=np.float64(1_760_000_000.0 + seed),
    )


def build_session(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A camera restart, three strikes, a false trigger, a camera restart, a strike,
    a practice swing, and a false trigger while a second ball left beside the tee."""
    camera = tmp_path / "camera"
    plan = [
        ("120001", "fast", 1),
        ("120002", "fast", 2),
        ("120003", "fast", 3),
        ("120004", "stays", None),
        ("120006", "fast", 4),
        ("120007", "stays", 5),
        ("120008", "other_leaves", None),
    ]
    entries = [
        {
            "ts": "2026-10-19T12:00:00.0",
            "type": "session_start",
            "start_time": "2026-10-19T12:00:00.0",
            "config": CONFIG,
        },
        {
            "ts": "2026-10-19T12:00:00.5",
            "type": "config_change",
            "config": {"camera_capture": {"exposure_us": 100, "auto_exposure_enabled": False}},
        },
    ]
    for index, (stamp, motion, shot) in enumerate(plan):
        name = f"camera_20261019_{stamp}_000_{index + 1:03d}"
        write_clip(camera / name, motion, seed=index)
        if shot is not None:
            entries.append(
                {
                    "ts": f"2026-10-19T12:00:0{index}.5",
                    "type": "camera_capture",
                    "shot_number": shot,
                    "capture_path": f"/pi/camera/{name}",
                }
            )
        if stamp == "120004":
            entries.append(
                {
                    "ts": "2026-10-19T12:00:05.0",
                    "type": "config_change",
                    "config": {"camera_capture": {"exposure_us": 100}},
                }
            )
    entries.append({"ts": "2026-10-19T12:00:09.0", "type": "power_status"})
    session = tmp_path / "session.jsonl"
    session.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    intrinsics = tmp_path / "intrinsics.json"
    intrinsics.write_text(
        json.dumps(
            {
                "version": 1,
                "fx": 330.0,
                "fy": 330.0,
                "cx": 120.0,
                "cy": 80.0,
                "dist_coeffs": [0.0, 0.0, 0.0, 0.0, 0.0],
                "rms_reprojection_px": 0.1,
                "mode": {
                    "width": WIDTH,
                    "height": HEIGHT,
                    "rotate_180": False,
                    "mirror_horizontal": False,
                    "scaler_crop": None,
                },
            }
        ),
        encoding="utf-8",
    )
    return session, camera, intrinsics


def run(tmp_path, capsys, mode: str) -> tuple[str, list[dict]]:
    session, camera, intrinsics = build_session(tmp_path)
    out = tmp_path / f"{mode}.csv"
    assert (
        replay_tee_anchor.main(
            [
                "--session",
                str(session),
                "--camera-dir",
                str(camera),
                "--intrinsics",
                str(intrinsics),
                "--not-strikes",
                "5",
                "--mode",
                mode,
                "--csv",
                str(out),
            ]
        )
        == 0
    )
    with out.open(encoding="utf-8") as handle:
        return capsys.readouterr().out, list(csv.DictReader(handle))


def restart_line(text: str, stamp: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"camera restart {stamp}"))


def test_camera_mode_replay_blocks_no_strike_and_the_waggle(tmp_path, capsys):
    text, rows = run(tmp_path, capsys, "camera")
    assert [row["role"] for row in rows] == [
        "strike",
        "strike",
        "strike",
        "no_shot",
        "strike",
        "not_strike",
        "no_shot",
    ]
    assert [row["live_reason"] for row in rows[:3]] == ["no_anchor"] * 3
    assert rows[0]["event"] == "established" and rows[2]["confirmations"] == "3"
    assert rows[3]["blocked"] == "True" and rows[3]["live_decision"] == "still_there"
    assert rows[4]["live_decision"] == "departed"
    assert float(rows[4]["gate_contrast"]) > 0 and float(rows[4]["departure_contrast"]) > 0
    # Camera mode: the blocked practice swing never reaches the OPS, so it teaches nothing.
    assert rows[5]["blocked"] == "True" and rows[5]["event"] == ""
    # A blocked edge whose clip shows a ball leaving beside the tee contradicts the anchor.
    assert rows[6]["blocked"] == "True" and rows[6]["event"] == "blocked_departure"
    assert rows[6]["contradicted"] == "True" and rows[6]["confirmations"] == "0"
    assert "strikes blocked by the live gate: 0/4 (0.0 %)" in text
    assert "no-shot clips blocked: 2/2 (100.0 %)" in text
    assert "acceptance: no strike blocked: PASS" in text
    assert "visible strikes departed (live gate): FAIL" in text  # 1 of 4: anchor forms late
    # Restarts go through the runtime; the label is what the anchor did, not assumed.
    assert restart_line(text, "20261019_120000").endswith("None -> None (no anchor)")
    assert restart_line(text, "20261019_120005").endswith("(kept)")
    assert "None" not in restart_line(text, "20261019_120005")
    assert all(row["visible"] == "True" for row in rows if row["role"] == "strike")


def test_observation_mode_replay_counts_the_practice_swing_as_a_contradiction(tmp_path, capsys):
    text, rows = run(tmp_path, capsys, "observation")
    assert rows[5]["contradicted"] == "True"
    # Nothing is blocked in a passthrough session: the second ball teaches nothing.
    assert rows[6]["event"] == "" and rows[6]["contradicted"] == "False"
    assert "contradictions 1" in text
    assert "OPS shot 5 (not a strike): live still_there/ball_unchanged" in text


def test_pct_keeps_a_decimal_so_a_near_miss_shows():
    """52 of 58 is 89.7 %, not '90 %'."""
    assert replay_tee_anchor._pct(52, 58) == "52/58 (89.7 %)"
    assert replay_tee_anchor._pct(0, 0) == "0/0"
