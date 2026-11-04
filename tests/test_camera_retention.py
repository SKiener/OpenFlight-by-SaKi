"""Tests for the opt-in camera clip retention rule."""

from __future__ import annotations

import argparse
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from openflight.camera import retention

NOW_DT = datetime(2026, 10, 11, 12, 0, 0)
NOW = NOW_DT.timestamp()
PAYLOAD = {
    "frames.npz": 4000,
    "replay.mp4": 1000,
    "first.pgm": 100,
    "trigger.pgm": 100,
    "last.pgm": 100,
}
PAYLOAD_BYTES = sum(PAYLOAD.values())
HUGE_FREE = 10**15


def make_clip(root: Path, age: timedelta, sequence: int = 1, *, files=None) -> Path:
    """Create a clip directory whose name, metadata and file times say ``age`` old."""
    when = NOW_DT - age
    name = f"camera_{when.strftime('%Y%m%d_%H%M%S')}_{when.microsecond // 1000:03d}_{sequence:03d}"
    clip = root / name
    clip.mkdir(parents=True)
    for file_name, size in (PAYLOAD if files is None else files).items():
        (clip / file_name).write_bytes(b"x" * size)
    metadata = {
        "sequence": sequence,
        "trigger_timestamp": when.timestamp(),
        "capture_path": str(clip),
        "trigger_check": {"verdict": "false_trigger"},
    }
    (clip / "metadata.json").write_text(json.dumps(metadata))
    set_times(clip, when.timestamp())
    return clip


def set_times(clip: Path, epoch: float) -> None:
    for child in clip.iterdir():
        os.utime(child, (epoch, epoch), follow_symlinks=False)
    os.utime(clip, (epoch, epoch))


def write_session(sessions: Path, name: str, entries: list) -> Path:
    path = sessions / name
    lines = [entry if isinstance(entry, str) else json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n")
    return path


def clip_time(clip: Path) -> float:
    return json.loads((clip / "metadata.json").read_text())["trigger_timestamp"]


def has_payload(clip: Path) -> bool:
    return all((clip / name).exists() for name in PAYLOAD)


def run(root, sessions, **kwargs):
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("free_space", lambda _root: HUGE_FREE)
    return retention.run_retention(root, sessions, **kwargs)


@pytest.fixture(name="dirs")
def fixture_dirs(tmp_path):
    sessions = tmp_path / "sessions"
    root = sessions / "range" / "camera"
    root.mkdir(parents=True)
    write_session(sessions, "session_20261001_100000_range.jsonl", [{"type": "session_start"}])
    return root, sessions


def test_clip_referenced_by_capture_path_is_kept(dirs):
    root, sessions = dirs
    shot = make_clip(root, timedelta(days=30), 1)
    other = make_clip(root, timedelta(days=30, seconds=10), 2)
    write_session(
        sessions,
        "session_20260911_100000_range.jsonl",
        [
            {
                "type": "camera_capture",
                "shot_number": 1,
                "shot_timestamp": clip_time(shot),
                "capture_path": str(shot),
                "metadata": {},
            }
        ],
    )

    report = run(root, sessions)

    assert has_payload(shot)
    assert not has_payload(other)
    assert report.shot_clips == 1
    assert report.pruned_names == [other.name]


def test_unresolved_camera_replay_id_protects_clips_near_the_shot(dirs):
    root, sessions = dirs
    shot = make_clip(root, timedelta(days=30), 1)
    far = make_clip(root, timedelta(days=31), 2)
    write_session(
        sessions,
        "session_20260911_100000_range.jsonl",
        [
            {
                "type": "shot_detected",
                "shot_number": 4,
                "impact_timestamp": clip_time(shot) + 0.4,
                "camera_replay": {"id": "0f" * 16, "frame_count": 60},
            }
        ],
    )

    report = run(root, sessions)

    assert has_payload(shot)
    assert not has_payload(far)
    assert report.shot_clips == 1


def test_camera_replay_resolved_through_capture_line_protects_only_that_clip(dirs):
    root, sessions = dirs
    shot = make_clip(root, timedelta(days=30), 1)
    neighbour = make_clip(root, timedelta(days=30) - timedelta(seconds=5), 2)
    impact = clip_time(shot) + 0.2
    write_session(
        sessions,
        "session_20260911_100000_range.jsonl",
        [
            {
                "type": "camera_capture",
                "shot_number": 1,
                "shot_timestamp": impact,
                "capture_path": f"/home/pi/openflight_sessions/range/camera/{shot.name}",
            },
            {
                "type": "shot_detected",
                "shot_number": 1,
                "impact_timestamp": impact,
                "camera_replay": {"id": "ab" * 16},
            },
        ],
    )

    run(root, sessions)

    assert has_payload(shot)
    assert not has_payload(neighbour)


def test_camera_replay_without_clip_or_time_aborts(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    write_session(
        sessions,
        "session_20260911_100000_range.jsonl",
        [{"type": "shot_detected", "camera_replay": {"id": "cd" * 16}}],
    )

    report = run(root, sessions)

    assert report.aborted
    assert has_payload(clip)


def test_old_non_shot_clip_is_pruned_and_metadata_annotated(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=15))

    report = run(root, sessions, days=14)

    assert sorted(path.name for path in clip.iterdir()) == ["metadata.json"]
    metadata = json.loads((clip / "metadata.json").read_text())
    assert metadata["pruned_bytes"] == PAYLOAD_BYTES
    assert metadata["pruned_at"].startswith("2026-10-11T12:00:00")
    assert metadata["pruned_files"] == sorted(PAYLOAD)
    assert metadata["trigger_check"] == {"verdict": "false_trigger"}
    assert (report.pruned, report.bytes_freed) == (1, PAYLOAD_BYTES)


def test_second_run_does_not_prune_again(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=20))
    run(root, sessions)
    set_times(clip, NOW - 86400 * 20)

    report = run(root, sessions)

    assert report.pruned == 0
    assert json.loads((clip / "metadata.json").read_text())["pruned_bytes"] == PAYLOAD_BYTES


def test_young_non_shot_clip_is_kept(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=13))

    report = run(root, sessions, days=14)

    assert has_payload(clip)
    assert report.pruned == 0


def test_dry_run_deletes_nothing_and_reports(dirs, caplog):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=20))
    before = (clip / "metadata.json").read_text()

    with caplog.at_level(logging.INFO, logger=retention.__name__):
        report = run(root, sessions, dry_run=True)

    assert has_payload(clip)
    assert (clip / "metadata.json").read_text() == before
    assert (report.pruned, report.bytes_freed) == (1, PAYLOAD_BYTES)
    assert clip.name in caplog.text
    assert "dry run" in report.summary()


def _payload_on_disk(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob("*")
        if path.is_file() and path.name != "metadata.json"
    )


def test_low_disk_guard_prunes_oldest_non_shot_first(dirs):
    root, sessions = dirs
    shot = make_clip(root, timedelta(days=5), 1)
    oldest = make_clip(root, timedelta(days=4), 2)
    middle = make_clip(root, timedelta(days=3), 3)
    youngest = make_clip(root, timedelta(days=2), 4)
    write_session(
        sessions,
        "session_20261006_100000_range.jsonl",
        [{"type": "camera_capture", "capture_path": str(shot)}],
    )
    initial = _payload_on_disk(root)

    def free_space(_root):
        return 1000 + initial - _payload_on_disk(root)

    report = run(root, sessions, free_space=free_space, min_free_bytes=1000 + 2 * PAYLOAD_BYTES)

    assert has_payload(shot)
    assert not has_payload(oldest)
    assert not has_payload(middle)
    assert has_payload(youngest)
    assert report.pruned_names == [oldest.name, middle.name]
    assert json.loads((oldest / "metadata.json").read_text())["pruned_reason"] == "low disk space"


def test_low_disk_guard_warns_when_still_low_and_keeps_shot_clips(dirs, caplog):
    root, sessions = dirs
    shot = make_clip(root, timedelta(days=5), 1)
    other = make_clip(root, timedelta(days=2), 2)
    write_session(
        sessions,
        "session_20261006_100000_range.jsonl",
        [{"type": "camera_capture", "capture_path": str(shot)}],
    )

    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        report = run(root, sessions, free_space=lambda _root: 10, min_free_bytes=10**9)

    assert has_payload(shot)
    assert not has_payload(other)
    assert report.free_bytes == 10
    assert "free on the clip filesystem" in caplog.text


def test_low_disk_dry_run_counts_would_be_freed_space(dirs):
    root, sessions = dirs
    first = make_clip(root, timedelta(days=3), 1)
    second = make_clip(root, timedelta(days=2), 2)

    report = run(
        root,
        sessions,
        dry_run=True,
        free_space=lambda _root: 0,
        min_free_bytes=PAYLOAD_BYTES,
    )

    assert report.pruned_names == [first.name]
    assert has_payload(first) and has_payload(second)


@pytest.mark.parametrize(
    "bad_line",
    [
        '{"type": "camera_capture", "capture_path": "/home/pi/openflight_sess',
        b'{"type": "camera_capture", "note": "\xff\xfe"}',
    ],
)
def test_unparseable_session_line_prunes_nothing(dirs, bad_line):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    path = sessions / "session_20260911_100000_range.jsonl"
    data = bad_line if isinstance(bad_line, bytes) else bad_line.encode()
    path.write_bytes(b'{"type": "session_start"}\n' + data + b"\n")

    report = run(root, sessions, free_space=lambda _root: 0)

    assert has_payload(clip)
    assert report.pruned == 0
    assert "does not parse" in report.aborted


def test_truncated_line_without_camera_text_is_ignored(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    path = sessions / "session_20260911_100000_range.jsonl"
    path.write_text('{"type": "session_start"}\n{"type": "iq_blocks", "data": [1, 2')

    report = run(root, sessions)

    assert report.aborted is None
    assert not has_payload(clip)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_unreadable_session_file_prunes_nothing(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    path = write_session(sessions, "session_20260911_100000_range.jsonl", [{"type": "x"}])
    path.chmod(0)
    try:
        report = run(root, sessions)
    finally:
        path.chmod(0o600)

    assert has_payload(clip)
    assert "cannot be read" in report.aborted


def test_missing_session_directory_prunes_nothing(tmp_path):
    root = tmp_path / "camera"
    root.mkdir()
    clip = make_clip(root, timedelta(days=30))

    report = run(root, tmp_path / "missing")

    assert has_payload(clip)
    assert report.aborted


def test_session_files_in_subdirectories_are_scanned(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    nested = sessions / "archive"
    nested.mkdir()
    write_session(
        nested,
        "session_20260911_100000_range.jsonl",
        [{"type": "camera_capture", "capture_path": str(clip)}],
    )

    run(root, sessions)

    assert has_payload(clip)


def test_symlinks_and_foreign_directories_are_untouched(dirs, tmp_path):
    root, sessions = dirs
    outside = make_clip(tmp_path / "outside", timedelta(days=40), 7)
    linked = root / "camera_20260801_100000_000_009"
    linked.symlink_to(outside, target_is_directory=True)
    foreign = root / "notes"
    foreign.mkdir()
    (foreign / "frames.npz").write_bytes(b"x" * 10)
    with_link = make_clip(root, timedelta(days=40), 1)
    (with_link / "trigger.pgm").unlink()
    (with_link / "trigger.pgm").symlink_to(outside / "trigger.pgm")
    unknown = make_clip(root, timedelta(days=40), 2)
    (unknown / "notes.txt").write_text("keep me")
    for clip in (with_link, unknown):
        set_times(clip, NOW - 40 * 86400)

    report = run(root, sessions, free_space=lambda _root: 0)

    assert has_payload(outside)
    assert (foreign / "frames.npz").exists()
    assert (with_link / "frames.npz").exists() and (outside / "trigger.pgm").exists()
    assert (unknown / "frames.npz").exists()
    assert report.pruned == 0


@pytest.mark.parametrize("age", [timedelta(minutes=30), timedelta(minutes=59)])
def test_clip_younger_than_one_hour_is_never_touched(dirs, age):
    root, sessions = dirs
    clip = make_clip(root, age)

    report = run(root, sessions, free_space=lambda _root: 0, days=1)

    assert has_payload(clip)
    assert report.pruned == 0


def test_recently_written_old_clip_is_untouched(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    os.utime(clip / "metadata.json", (NOW - 60, NOW - 60))

    run(root, sessions)

    assert has_payload(clip)


def test_clip_whose_replay_is_being_encoded_is_untouched(dirs):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))
    (clip / f".replay-{'a' * 32}.mp4").write_bytes(b"x")
    set_times(clip, NOW - 30 * 86400)

    run(root, sessions)

    assert has_payload(clip)


def test_rejects_less_than_one_day():
    with pytest.raises(ValueError):
        retention.run_retention("/nonexistent", "/nonexistent", days=0)


def test_server_options_default_off_and_parse():
    parser = argparse.ArgumentParser()
    retention.add_retention_arguments(parser)

    defaults = parser.parse_args([])
    enabled = parser.parse_args(["--camera-retention-days", "7", "--camera-retention-dry-run"])

    assert defaults.camera_retention_days is None
    assert defaults.camera_retention_dry_run is False
    assert (enabled.camera_retention_days, enabled.camera_retention_dry_run) == (7, True)
    assert retention.start_retention_from_args(defaults, "/x", "/y") is None
    for bad in ("0", "-3", "two"):
        with pytest.raises(SystemExit):
            parser.parse_args(["--camera-retention-days", bad])


class _FakeTimeline:
    """Injected clock and wait for the retention thread; stops after ``waits`` waits."""

    def __init__(self, waits: int):
        self.now = 0.0
        self.waits_left = waits
        self.delays: list[float] = []
        self.runs: list[tuple[float, dict]] = []

    def wait(self, seconds: float) -> bool:
        self.delays.append(seconds)
        self.waits_left -= 1
        if self.waits_left < 0:
            return True
        self.now += seconds
        return False

    def run(self, *args, **kwargs):
        self.runs.append((self.now, {"args": args, **kwargs}))
        return retention.RetentionReport(dry_run=kwargs["dry_run"])


def _run_thread(monkeypatch, timeline, **kwargs):
    monkeypatch.setattr(retention, "run_retention", timeline.run)
    thread = retention.start_retention_thread(
        "/clips", "/sessions", days=9, dry_run=True, wait=timeline.wait, **kwargs
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert thread.daemon


def test_background_thread_delays_first_run_then_runs_daily(monkeypatch):
    timeline = _FakeTimeline(waits=3)

    _run_thread(monkeypatch, timeline)

    assert timeline.delays == [900.0, 86400.0, 86400.0, 86400.0]
    assert [when for when, _call in timeline.runs] == [900.0, 87300.0, 173700.0]
    assert timeline.runs[0][1] == {"args": ("/clips", "/sessions"), "days": 9, "dry_run": True}


def test_background_thread_postpones_a_run_while_busy(monkeypatch):
    timeline = _FakeTimeline(waits=5)
    answers = iter([True, True, True, False, False])

    _run_thread(monkeypatch, timeline, busy=lambda: next(answers))

    assert timeline.delays == [900.0, 60.0, 60.0, 60.0, 86400.0, 86400.0]
    assert [when for when, _call in timeline.runs] == [1080.0, 87480.0]


def test_background_thread_runs_when_busy_check_fails(monkeypatch, caplog):
    timeline = _FakeTimeline(waits=1)

    def broken():
        raise RuntimeError("no state")

    with caplog.at_level(logging.WARNING, logger=retention.__name__):
        _run_thread(monkeypatch, timeline, busy=broken)

    assert [when for when, _call in timeline.runs] == [900.0]
    assert "Busy check failed; running anyway" in caplog.text


def test_background_thread_stop_event_ends_the_first_delay(monkeypatch):
    calls = []
    monkeypatch.setattr(retention, "run_retention", lambda *a, **k: calls.append((a, k)))
    stop = threading.Event()
    thread = retention.start_retention_thread(
        "/clips", "/sessions", days=9, first_delay_s=3600, stop_event=stop
    )
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls == []


def test_start_from_args_passes_the_busy_predicate(monkeypatch):
    started = []
    monkeypatch.setattr(
        retention, "start_retention_thread", lambda *a, **k: started.append((a, k)) or "thread"
    )
    args = argparse.Namespace(camera_retention_days=7, camera_retention_dry_run=True)

    def busy():
        return False

    assert retention.start_retention_from_args(args, "/clips", "/sessions", busy=busy) == "thread"
    assert started == [(("/clips", "/sessions"), {"days": 7, "dry_run": True, "busy": busy})]


def test_dry_run_logs_at_most_ten_clip_names_at_info(dirs, caplog):
    root, sessions = dirs
    clips = [make_clip(root, timedelta(days=20, minutes=index), index) for index in range(12)]

    with caplog.at_level(logging.DEBUG, logger=retention.__name__):
        report = run(root, sessions, dry_run=True)

    would = [record for record in caplog.records if "Would prune" in record.getMessage()]
    assert report.pruned == 12
    assert [record.levelno for record in would] == [logging.INFO] * 10 + [logging.DEBUG] * 2
    assert all(has_payload(clip) for clip in clips)


def test_cli_dry_run(dirs, capsys):
    root, sessions = dirs
    clip = make_clip(root, timedelta(days=30))

    code = retention.main(
        ["--root", str(root), "--sessions", str(sessions), "--days", "14", "--dry-run"]
    )

    assert code == 0
    assert has_payload(clip)
    assert "would prune 1" in capsys.readouterr().out
