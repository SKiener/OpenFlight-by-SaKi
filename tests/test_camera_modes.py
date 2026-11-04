"""Operator mode changes must preserve the buffer window and recover on failure."""

from dataclasses import replace
from unittest.mock import Mock

import numpy as np
import pytest

from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings
from openflight.camera.triggered_buffer import CameraFrame, TriggeredFrameBuffer


def runtime_for_test(tmp_path):
    runtime = CameraCaptureRuntime(output_dir=tmp_path, settings=CameraCaptureSettings())
    runtime._running = True
    runtime._capture_modes = [
        {"id": "full-300", "width": 640, "height": 400, "fps": 300},
        {"id": "strip-500", "width": 640, "height": 200, "fps": 500},
        {"id": "strip-560", "width": 640, "height": 200, "fps": 560},
    ]
    runtime.stop = Mock()
    runtime.start = Mock()
    return runtime


def test_mode_change_keeps_time_window_and_mount_orientation(tmp_path):
    runtime = runtime_for_test(tmp_path)
    result = runtime.update_capture_mode("strip-500")
    assert (runtime.settings.width, runtime.settings.height, runtime.settings.fps) == (
        640,
        200,
        500,
    )
    assert (runtime.settings.pre_frames, runtime.settings.post_frames) == (75, 25)
    assert (runtime.settings.pre_ms, runtime.settings.post_ms) == (150, 50)
    assert result["capture_mode"] == "strip-500"
    assert result["pre_frames"] == 75
    runtime.stop.assert_called_once()
    runtime.start.assert_called_once()


@pytest.mark.parametrize("mode", ["arbitrary", 600, None, True, []])
def test_unknown_or_malformed_mode_never_stops_capture(tmp_path, mode):
    runtime = runtime_for_test(tmp_path)
    with pytest.raises(ValueError, match="mode"):
        runtime.update_capture_mode(mode)
    runtime.stop.assert_not_called()


def test_failed_mode_restores_old_settings_and_policy(tmp_path):
    runtime = runtime_for_test(tmp_path)
    previous = runtime.settings
    runtime.start.side_effect = [RuntimeError("not supported"), None]
    with pytest.raises(RuntimeError, match="not supported"):
        runtime.update_capture_mode("strip-500")
    assert runtime.settings == previous
    assert runtime.start.call_count == 2
    assert runtime.stop.call_count == 2


def test_mode_switch_rejected_during_capture(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime._ring = Mock(capture_busy=True)
    with pytest.raises(RuntimeError, match="capturing"):
        runtime.update_capture_mode("strip-500")
    runtime.stop.assert_not_called()


def test_mode_switch_rejected_while_frames_are_waiting_to_be_saved(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime._ready.put(object())
    with pytest.raises(RuntimeError, match="saving"):
        runtime.update_capture_mode("strip-500")
    runtime.stop.assert_not_called()


def test_current_mode_does_not_restart_camera(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime.update_capture_mode("full-300")
    runtime.stop.assert_not_called()


def test_sensor_modes_filter_unsupported_profiles():
    from openflight.camera.capture_runtime import supported_capture_modes

    modes = supported_capture_modes(
        [
            {"size": (640, 400), "format": "R8", "fps": 309.79},
            {"size": (320, 200), "format": "R8", "fps": 618.81},
            {"size": (640, 200), "format": "R10_CSI2P", "fps": 471.25},
        ]
    )
    assert [mode["id"] for mode in modes] == ["full-300"]


def test_the_flat_320x200_crop_modes_are_no_longer_offered():
    """Range 2026-11-02: the patched 320x200 register set delivers flat frames on the Pi."""
    from openflight.camera.capture_runtime import supported_capture_modes

    modes = supported_capture_modes(
        [
            {"size": (640, 400), "format": "R8", "fps": 309.79},
            {"size": (640, 200), "format": "R8", "fps": 589.0},
            {"size": (320, 200), "format": "R8", "fps": 618.81},
        ]
    )
    assert [mode["id"] for mode in modes] == ["full-300", "strip-500", "strip-560"]
    assert not [mode for mode in modes if (mode["width"], mode["height"]) == (320, 200)]


@pytest.mark.parametrize(
    ("strip_fps", "offered"),
    [
        (589.0, ["full-300", "strip-500", "strip-560"]),
        (560.0, ["full-300", "strip-500", "strip-560"]),
        (540.0, ["full-300", "strip-500"]),
    ],
)
def test_strip_560_is_offered_only_when_the_strip_reaches_560_fps(strip_fps, offered):
    """The patched driver's 640x200 R8 mode reports about 589 fps (VTS/HTS)."""
    from openflight.camera.capture_runtime import supported_capture_modes

    modes = supported_capture_modes(
        [
            {"size": (640, 400), "format": "R8", "fps": 309.79},
            {"size": (640, 200), "format": "R8", "fps": strip_fps},
        ]
    )
    assert [mode["id"] for mode in modes] == offered
    if "strip-560" in offered:
        # The request is the profile's rate, as for the other profiles.
        assert modes[-1] == {"id": "strip-560", "width": 640, "height": 200, "fps": 560}


def test_strip_560_keeps_the_time_window_in_frames(tmp_path):
    runtime = runtime_for_test(tmp_path)

    result = runtime.update_capture_mode("strip-560")

    assert (runtime.settings.width, runtime.settings.height, runtime.settings.fps) == (
        640,
        200,
        560,
    )
    # ceil(150 ms * 560 fps) and ceil(50 ms * 560 fps).
    assert (runtime.settings.pre_frames, runtime.settings.post_frames) == (84, 28)
    assert (runtime.settings.pre_ms, runtime.settings.post_ms) == (150, 50)
    assert result["capture_mode"] == "strip-560"
    assert (result["pre_frames"], result["post_frames"]) == (84, 28)
    assert replace(runtime.settings, post_ms=100.0).post_frames == 56


@pytest.mark.parametrize("fps", [560.0, 536.5])
def test_every_exposure_step_fits_the_strip_560_frame_period(fps):
    """1786 us at 560 fps (1864 us delivered): the whole ladder up to 1250 us fits."""
    from openflight.camera.auto_exposure import EXPOSURE_STEPS, exposure_steps_for_fps

    assert exposure_steps_for_fps(fps) == EXPOSURE_STEPS
    assert max(step.exposure_us for step in EXPOSURE_STEPS) < round(1_000_000 / fps)


def test_strip_560_takes_the_longest_exposure(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime.update_capture_mode("strip-560")
    runtime._camera = Mock()

    assert runtime.update_image_controls(exposure_us=1250, gain=16.0) == {
        "exposure_us": 1250,
        "gain": 16.0,
    }
    with pytest.raises(ValueError, match="1786us frame period"):
        runtime.update_image_controls(exposure_us=1786, gain=16.0)


def test_strip_560_clips_hold_the_locator_window_like_the_other_modes():
    """The disappeared-ball locator needs 29 post frames: 52 ms at 560 fps."""
    from openflight.camera.departed_ball import MIN_CLIP_POST_FRAMES, min_post_ms

    assert min_post_ms(560.0) == 52
    settings = CameraCaptureSettings(width=640, height=200, fps=560.0, post_ms=52.0)
    assert settings.post_frames >= MIN_CLIP_POST_FRAMES


@pytest.mark.parametrize("strip_offset", [0, 30, -100, 140])
@pytest.mark.parametrize("rotate_180", [False, True])
def test_strip_560_maps_into_the_full_frame_like_strip_500(strip_offset, rotate_180):
    """Every geometry-keyed mapping covers both 640x200 profiles alike."""
    from openflight.camera.capture_runtime import (
        CAPTURE_MODE_FRAME_OFFSETS,
        DRIVER_STRIP_OFFSET_LIMITS,
        canonical_frame_offset,
        driver_strip_offset,
        mode_frame_shift,
        supported_capture_modes,
    )
    from openflight.camera.intrinsics import CameraIntrinsics, for_mode

    modes = {
        mode["id"]: mode
        for mode in supported_capture_modes(
            [
                {"size": (640, 400), "format": "R8", "fps": 309.79},
                {"size": (640, 200), "format": "R8", "fps": 589.0},
            ]
        )
    }
    strip_500, strip_560 = modes["strip-500"], modes["strip-560"]
    size_500 = (strip_500["width"], strip_500["height"])
    size_560 = (strip_560["width"], strip_560["height"])
    assert CAPTURE_MODE_FRAME_OFFSETS[size_560] == CAPTURE_MODE_FRAME_OFFSETS[size_500]
    assert DRIVER_STRIP_OFFSET_LIMITS[size_560] == DRIVER_STRIP_OFFSET_LIMITS[size_500]
    applied = driver_strip_offset(*size_560, strip_offset)
    assert applied == driver_strip_offset(*size_500, strip_offset)
    assert applied == max(-100, min(100, strip_offset))
    offset = canonical_frame_offset(*size_560, applied, rotate_180=rotate_180)
    assert offset == canonical_frame_offset(*size_500, applied, rotate_180=rotate_180)
    assert offset == ((0, 100 - applied) if rotate_180 else (0, 100 + applied))
    # Strip to strip at the same window: the same frame.
    assert mode_frame_shift((*size_500, applied), (*size_560, applied), rotate_180=rotate_180) == (
        0,
        0,
    )
    base = CameraIntrinsics(
        575.7, 577.3, 347.8, 246.9, (0.0,) * 5, 640, 400, rotate_180, False, None, 0.4
    )
    lens_560 = for_mode(base, *size_560, applied)
    assert lens_560 == for_mode(base, *size_500, applied)
    assert lens_560.cy == pytest.approx(246.9 - offset[1])


@pytest.mark.parametrize("fps", [500.0, 600.0])
def test_a_stored_crop_mode_falls_back_to_full_300_with_a_log_line(fps, caplog):
    from openflight.camera.capture_runtime import without_withdrawn_capture_mode

    stored = CameraCaptureSettings(width=320, height=200, fps=fps, exposure_us=350, gain=6.0)
    with caplog.at_level("WARNING", logger="openflight.camera.capture_runtime"):
        started = without_withdrawn_capture_mode(stored)

    assert (started.width, started.height, started.fps) == (640, 400, 300.0)
    assert (started.exposure_us, started.gain, started.pre_ms) == (350, 6.0, stored.pre_ms)
    assert "withdrawn" in caplog.text and "full-300" in caplog.text


@pytest.mark.parametrize(
    ("width", "height", "fps"), [(640, 400, 300.0), (640, 200, 500.0), (640, 200, 560.0)]
)
def test_offered_modes_start_unchanged_and_silently(width, height, fps, caplog):
    from openflight.camera.capture_runtime import without_withdrawn_capture_mode

    stored = CameraCaptureSettings(width=width, height=height, fps=fps)
    with caplog.at_level("WARNING", logger="openflight.camera.capture_runtime"):
        assert without_withdrawn_capture_mode(stored) is stored
    assert not caplog.records


def test_server_start_turns_a_stored_crop_mode_into_full_300(tmp_path, monkeypatch):
    from openflight import server
    from openflight.camera import capture_runtime

    created = []

    class FakeRuntime:
        def __init__(self, *, settings, **_kwargs):
            self.settings = settings
            created.append(settings)

        def start(self):
            pass

        def set_ball_mark_source(self, _provider):
            pass

    monkeypatch.setattr(capture_runtime, "CameraCaptureRuntime", FakeRuntime)
    for name in (
        "camera_capture_runtime",
        "camera_capture_config",
        "camera_replay_manager",
        "camera_reference_ball_tracker",
        "camera_ball_flight_reference_tracker",
        "camera_ball_flight_ground_anchor",
        "camera_lowlight_reference_tracker",
        "camera_lowlight_ground_anchor",
        "camera_cv_worker",
    ):
        monkeypatch.setattr(server, name, getattr(server, name))
    assert server.init_camera_capture(
        output_dir=tmp_path,
        gpio_pin=17,
        width=320,
        height=200,
        fps=600.0,
        pre_ms=150.0,
        post_ms=100.0,
        exposure_us=350,
        gain=6.0,
        stream="raw",
        rotate_180=False,
        mirror_horizontal=False,
        roll_correction_deg=0.0,
        scaler_crop=None,
        mount_height_m=0.3,
        lateral_offset_m=0.0,
        horizontal_offset_deg=0.0,
        use_gpio_trigger=False,
        cv_in_process=True,
    )
    assert (created[0].width, created[0].height, created[0].fps) == (640, 400, 300.0)
    assert server.camera_capture_config["width"] == 640


def test_reported_framerate_uses_sensor_time_not_requested_rate():
    ring = TriggeredFrameBuffer(30, 10)
    assert ring.measured_fps is None
    for index in range(30):
        ring.add_frame(CameraFrame(
            image=np.zeros((2, 2), dtype=np.uint8),
            sensor_timestamp_ns=1_000_000_000 + index * 2_000_000,
            host_timestamp_ns=1_010_000_000 + index * 2_000_000,
            exposure_us=300, analogue_gain=4,
        ))
    assert ring.measured_fps == 500
    assert ring.buffered_frames == 30
