"""Tests for lazy, browser-friendly camera replay preparation."""

import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest

from openflight.camera.replay import (
    CameraReplayManager,
    ReplayNotFoundError,
    ReplayPreparationError,
)
from openflight.camera.triggered_buffer import unpack_r8_frame


def _write_capture(
    root: Path,
    *,
    name: str = "camera_20260825_120000_001",
    frame_count: int = 6,
    pre_trigger_count: int = 4,
) -> Path:
    capture_dir = root / name
    capture_dir.mkdir()
    frames = np.arange(frame_count * 4 * 6, dtype=np.uint8).reshape(frame_count, 4, 6)
    np.savez(
        capture_dir / "frames.npz",
        frames=frames,
        pre_trigger_count=np.int32(pre_trigger_count),
    )
    return capture_dir


def _successful_runner(calls: list[tuple[list[str], bytes]]):
    def run(command, *, input, check, capture_output, timeout):
        assert check is True
        assert capture_output is True
        assert timeout == 30.0
        calls.append((command, input))
        Path(command[-1]).write_bytes(b"fake mp4")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    return run


def test_registering_capture_does_not_build_video(tmp_path):
    """Shot processing may advertise replay, but must not perform transcoding."""
    capture_dir = _write_capture(tmp_path)
    calls = []
    manager = CameraReplayManager(tmp_path, runner=_successful_runner(calls))

    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4})

    assert calls == []
    assert not (capture_dir / "replay.mp4").exists()
    assert replay["frame_count"] == 6
    assert replay["trigger_frame"] == 3
    assert replay["playback_fps"] == 60
    assert replay["duration_seconds"] == pytest.approx(0.1)
    assert replay["display_mirror_horizontal"] is False


@pytest.mark.parametrize(
    ("timing", "expected"),
    [
        ({"delivered_fps": 287.88, "settings": {"fps": 300}}, 287.88),
        ({"settings": {"fps": 300}}, 300),
        ({"delivered_fps": 0, "settings": {"fps": 300}}, 300),
        ({"delivered_fps": float("nan")}, None),
        ({"delivered_fps": True}, None),
        ({}, None),
    ],
)
def test_replay_reports_capture_rate_without_changing_video_timing(tmp_path, timing, expected):
    capture_dir = _write_capture(tmp_path)
    manager = CameraReplayManager(tmp_path, runner=_successful_runner([]))
    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4, **timing})
    assert replay["capture_fps"] == expected
    assert replay["playback_fps"] == 60
    assert replay["duration_seconds"] == pytest.approx(0.1)


@pytest.mark.parametrize(
    ("saved_mirror_horizontal", "display_mirror_horizontal"),
    [(False, False), (True, True)],
)
def test_register_accounts_for_saved_frame_orientation(
    tmp_path,
    saved_mirror_horizontal,
    display_mirror_horizontal,
):
    capture_dir = _write_capture(tmp_path)
    manager = CameraReplayManager(tmp_path, runner=_successful_runner([]))

    replay = manager.register(
        capture_dir,
        {
            "frame_count": 6,
            "pre_trigger_frames": 4,
            "settings": {"mirror_horizontal": saved_mirror_horizontal},
        },
    )

    assert replay["display_mirror_horizontal"] is display_mirror_horizontal


def test_prepare_builds_and_caches_mp4_after_manual_request(tmp_path):
    capture_dir = _write_capture(tmp_path)
    calls = []
    manager = CameraReplayManager(tmp_path, runner=_successful_runner(calls))
    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4})

    prepared = manager.prepare(replay["id"])
    cached = manager.prepare(replay["id"])

    assert prepared.video_path == capture_dir / "replay.mp4"
    assert cached.video_path == prepared.video_path
    assert prepared.video_path.read_bytes() == b"fake mp4"
    assert len(calls) == 1
    command, raw_input = calls[0]
    assert command[:2] == ["ffmpeg", "-loglevel"]
    assert command[command.index("-framerate") + 1] == "60"
    assert command[command.index("-video_size") + 1] == "6x4"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-threads") + 1] == "2"
    assert command.index("-threads") > command.index("-i")
    assert len(raw_input) == 6 * 4 * 6


@pytest.mark.parametrize("saved_mirror", [None, False, True])
@pytest.mark.parametrize("rotate_180", [False, True])
def test_replay_preserves_handedness_and_mount_rotation_after_restart(
    tmp_path, saved_mirror, rotate_180
):
    capture_dir = _write_capture(tmp_path)
    native_frame = np.arange(24, dtype=np.uint8).reshape(4, 6)
    upright_frame = unpack_r8_frame(native_frame, 6, 4, rotate_180)
    stored_frame = unpack_r8_frame(native_frame, 6, 4, rotate_180, bool(saved_mirror))
    stored_frames = np.repeat(stored_frame[np.newaxis], 6, axis=0)
    np.savez(capture_dir / "frames.npz", frames=stored_frames)
    archive_before = (capture_dir / "frames.npz").read_bytes()
    settings = {"rotate_180": rotate_180}
    if saved_mirror is not None:
        settings["mirror_horizontal"] = saved_mirror
    metadata = {"frame_count": 6, "pre_trigger_frames": 4, "settings": settings}
    calls = []

    for _restart in range(2):
        manager = CameraReplayManager(tmp_path, runner=_successful_runner(calls))
        replay = manager.register(capture_dir, metadata)
        prepared = manager.prepare(replay["id"])
        displayed = (
            stored_frames[:, :, ::-1] if replay["display_mirror_horizontal"] else stored_frames
        )
        np.testing.assert_array_equal(displayed[0], upright_frame)
        assert prepared.payload["display_mirror_horizontal"] is bool(saved_mirror)
        assert prepared.video_path.read_bytes() == b"fake mp4"
        assert (capture_dir / "frames.npz").read_bytes() == archive_before

    assert len(calls) == 1
    assert calls[0][1] == stored_frames.tobytes()


def test_concurrent_manual_requests_encode_only_once(tmp_path):
    capture_dir = _write_capture(tmp_path)
    calls = []

    def slow_runner(*args, **kwargs):
        time.sleep(0.03)
        return _successful_runner(calls)(*args, **kwargs)

    manager = CameraReplayManager(tmp_path, runner=slow_runner)
    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4})

    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = list(pool.map(lambda _index: manager.prepare(replay["id"]), range(2)))

    assert prepared[0].video_path == prepared[1].video_path
    assert len(calls) == 1


def test_different_replays_never_run_ffmpeg_concurrently(tmp_path):
    first_capture = _write_capture(tmp_path, name="camera_20260825_120000_001")
    second_capture = _write_capture(tmp_path, name="camera_20260825_120001_002")
    calls = []
    active_encoders = 0
    maximum_active_encoders = 0
    counter_lock = threading.Lock()

    def tracked_runner(*args, **kwargs):
        nonlocal active_encoders, maximum_active_encoders
        with counter_lock:
            active_encoders += 1
            maximum_active_encoders = max(maximum_active_encoders, active_encoders)
        try:
            time.sleep(0.05)
            return _successful_runner(calls)(*args, **kwargs)
        finally:
            with counter_lock:
                active_encoders -= 1

    manager = CameraReplayManager(tmp_path, runner=tracked_runner)
    replay_ids = [
        manager.register(first_capture, {"frame_count": 6, "pre_trigger_frames": 4})["id"],
        manager.register(second_capture, {"frame_count": 6, "pre_trigger_frames": 4})["id"],
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(manager.prepare, replay_ids))

    assert len(calls) == 2
    assert maximum_active_encoders == 1


def test_prepare_rejects_unknown_id(tmp_path):
    manager = CameraReplayManager(tmp_path, runner=_successful_runner([]))

    with pytest.raises(ReplayNotFoundError):
        manager.prepare("not-registered")


def test_register_rejects_capture_outside_configured_root(tmp_path):
    output_root = tmp_path / "camera"
    output_root.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()

    manager = CameraReplayManager(output_root, runner=_successful_runner([]))

    with pytest.raises(ValueError, match="outside camera output directory"):
        manager.register(outside, {"frame_count": 6, "pre_trigger_frames": 4})


def test_failed_encode_leaves_no_partial_replay_and_can_retry(tmp_path):
    capture_dir = _write_capture(tmp_path)
    attempts = 0

    def runner(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            Path(command[-1]).write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, command, stderr=b"encoder failed")
        return _successful_runner([])(command, **kwargs)

    manager = CameraReplayManager(tmp_path, runner=runner)
    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4})

    with pytest.raises(ReplayPreparationError, match="encoding failed"):
        manager.prepare(replay["id"])
    assert not (capture_dir / "replay.mp4").exists()

    prepared = manager.prepare(replay["id"])
    assert prepared.video_path.exists()
    assert attempts == 2


def test_unexpected_encoder_io_error_is_wrapped_and_retryable(tmp_path):
    capture_dir = _write_capture(tmp_path)

    def runner(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial")
        raise OSError("disk disappeared")

    manager = CameraReplayManager(tmp_path, runner=runner)
    replay = manager.register(capture_dir, {"frame_count": 6, "pre_trigger_frames": 4})

    with pytest.raises(ReplayPreparationError, match="could not be written"):
        manager.prepare(replay["id"])

    assert not (capture_dir / "replay.mp4").exists()
    assert list(capture_dir.glob(".replay-*.mp4")) == []
