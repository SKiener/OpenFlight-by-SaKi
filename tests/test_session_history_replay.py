"""Historical replays are matched by impact identity and never expose paths."""
import json
from datetime import datetime, timezone

import pytest

from openflight.camera.replay import CameraReplayManager
from openflight.session_history import get_session, list_sessions

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)
SESSION = "session_20261001_000000_range"


def history(root, *, frame_path=None, capture_error=None, duplicates=False, shot_impact=100.0):
    capture = root / "camera" / "clip"
    capture.mkdir(parents=True)
    if frame_path is None:
        (capture / "frames.npz").write_bytes(b"not opened until replay requested")
    else:
        (capture / "frames.npz").symlink_to(frame_path)
    camera = {"type": "camera_capture", "shot_number": 1, "shot_timestamp": 100.0,
              "capture_path": str(capture), "capture_error": capture_error,
              "metadata": {"frame_count": 60, "pre_trigger_frames": 45,
                           "settings": {"mirror_horizontal": True}}}
    shot = {"type": "shot_detected", "shot_number": 1, "impact_timestamp": shot_impact,
            "timestamp": NOW.isoformat(), "ball_speed_mph": 103}
    entries = [{"type": "session_start", "start_time": NOW.isoformat()}, camera]
    if duplicates:
        entries.append(camera)
    entries.append(shot)
    (root / f"{SESSION}.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    return capture


def test_history_reregisters_after_restart_without_encoding_or_mutating_logs(tmp_path):
    capture = history(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    manager = CameraReplayManager(tmp_path / "camera")
    saved = get_session(tmp_path, SESSION, now=NOW, replay_manager=manager)
    replay = saved["shots"][0]["camera_replay"]
    assert replay["frame_count"] == 60
    assert replay["trigger_frame"] == 44
    assert replay["display_mirror_horizontal"] is True
    assert "capture_path" not in json.dumps(saved)
    assert "_capture" not in json.dumps(saved)
    assert not (capture / "replay.mp4").exists()
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    again = get_session(tmp_path, SESSION, now=NOW, replay_manager=manager)
    assert again["shots"][0]["camera_replay"]["id"] == replay["id"]
    restarted = get_session(tmp_path, SESSION, now=NOW, replay_manager=CameraReplayManager(tmp_path / "camera"))
    assert restarted["shots"][0]["camera_replay"]["id"] != replay["id"]
    assert restarted["shots"][0]["camera_replay"]["display_mirror_horizontal"] is True
    assert "camera_replay" not in get_session(tmp_path, SESSION, now=NOW)["shots"][0]
    assert len(list_sessions(tmp_path, now=NOW)) == 1


@pytest.mark.parametrize("kwargs", [{"capture_error": "incomplete"}, {"duplicates": True}, {"shot_impact": 101.0}])
def test_missing_or_ambiguous_correlations_never_play_another_shot(tmp_path, kwargs):
    history(tmp_path, **kwargs)
    saved = get_session(tmp_path, SESSION, now=NOW, replay_manager=CameraReplayManager(tmp_path))
    assert saved["shot_count"] == 1
    assert "camera_replay" not in saved["shots"][0]


def test_deleted_archive_is_unavailable_without_losing_metrics(tmp_path):
    capture = history(tmp_path)
    (capture / "frames.npz").unlink()
    saved = get_session(tmp_path, SESSION, now=NOW, replay_manager=CameraReplayManager(tmp_path))
    assert saved["shots"][0]["ball_speed_mph"] == 103
    assert "camera_replay" not in saved["shots"][0]


def test_paths_outside_the_camera_root_and_frame_symlinks_are_rejected(tmp_path):
    capture = history(tmp_path)
    manager = CameraReplayManager(tmp_path / "other")
    assert "camera_replay" not in get_session(tmp_path, SESSION, now=NOW, replay_manager=manager)["shots"][0]
    (capture / "frames.npz").unlink()
    outside = tmp_path / "outside.npz"
    outside.write_bytes(b"private")
    (capture / "frames.npz").symlink_to(outside)
    manager = CameraReplayManager(tmp_path)
    assert "camera_replay" not in get_session(tmp_path, SESSION, now=NOW, replay_manager=manager)["shots"][0]
