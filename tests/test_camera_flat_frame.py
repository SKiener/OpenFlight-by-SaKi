"""A flat frame (no picture) is not a dark one and must not drive the exposure ladder.

Range 2026-11-02: the 320x200 sensor mode (crop-500/crop-600) delivered every
pixel at 16. The startup calibration read it as "too dark", climbed to
1250 us / gain 16 and asked for more light, which no light could fix. Every
real clip of that day, the darkest included, spanned at least 173 grey levels.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openflight import server
from openflight.camera import capture_runtime
from openflight.camera.auto_exposure import AutoExposurePolicy, measure_exposure
from openflight.camera.capture_runtime import (
    CameraCaptureRuntime,
    CameraCaptureSettings,
    capture_exposure_status,
)
from openflight.camera.triggered_buffer import CameraFrame, TriggeredCapture

FLAT = np.full((200, 320), 16, np.uint8)


def dark_range_frame() -> np.ndarray:
    """A floodlit night frame: black surroundings, sensor noise, a lit ball and mat edge."""
    rng = np.random.default_rng(20261102)
    image = np.clip(rng.normal(9, 3, (400, 640)), 0, 255).astype(np.uint8)
    image[250:262, 300:312] = 182  # the ball under the floodlight
    image[300:310, 100:540] = 40  # mat edge
    return image


class FakeRing:
    def __init__(self, image):
        self.image = image
        self.capture_busy = False
        self.measured_fps = 600.0

    @property
    def latest_frame(self):
        return SimpleNamespace(image=self.image)


@pytest.fixture(name="runtime")
def fixture_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_runtime, "AUTO_EXPOSURE_STARTUP_SETTLE_S", 0.0)
    runtime = CameraCaptureRuntime(output_dir=tmp_path, settings=CameraCaptureSettings(fps=300))
    runtime._running = True
    runtime._camera = Mock()
    runtime._ring = FakeRing(FLAT)
    runtime._capture_modes = [{"id": "full-300", "width": 640, "height": 400, "fps": 300}]
    runtime.update_image_controls = Mock()
    yield runtime
    runtime._running = False
    runtime._auto_exposure_stop.set()


def test_a_uniform_frame_is_flat():
    result = measure_exposure(FLAT)

    assert result.status == "flat"
    assert result.flat is True
    assert result.frame_spread == 0
    assert result.recommendation == "hold"
    assert result.acceptable is False


def test_two_grey_levels_of_noise_are_still_flat_three_are_not():
    image = FLAT.copy()
    image[0, 0] = 18
    assert measure_exposure(image).status == "flat"
    image[0, 1] = 19
    assert measure_exposure(image).status != "flat"


def test_a_real_dark_frame_is_dark_not_flat():
    result = measure_exposure(dark_range_frame())

    assert result.status == "too_dark"
    assert result.flat is False
    assert result.frame_spread >= 170


def test_the_policy_takes_no_exposure_step_and_asks_for_no_light_on_a_flat_frame():
    policy = AutoExposurePolicy(fps=600)

    decision = policy.evaluate(measure_exposure(FLAT), exposure_us=350, gain=6.0)

    assert decision.status == "no_image"
    assert decision.analysis_eligible is False
    assert decision.target is None
    assert "light" not in decision.message
    # Locked: the same answer until the next startup, no step on a second look.
    assert policy.evaluate(measure_exposure(FLAT), exposure_us=350, gain=6.0) is decision


def test_startup_calibration_on_a_flat_frame_applies_no_step_and_warns(runtime, caplog):
    with caplog.at_level("WARNING", logger=capture_runtime.logger.name):
        runtime._start_auto_exposure()

    runtime.update_image_controls.assert_not_called()
    assert (runtime.settings.exposure_us, runtime.settings.gain) == (1000, 4.0)
    status = runtime.auto_exposure_status()
    assert status["status"] == "no_image"
    assert status["analysis_eligible"] is False
    assert status["frame_flat"] is True
    assert runtime.camera_analysis_eligible is False
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Flat frame in mode full-300" in warnings[0]
    assert "add or redirect light" not in caplog.text


def test_capture_mode_status_reports_the_flat_frame(runtime):
    runtime._start_auto_exposure()

    assert runtime.capture_mode_status()["frame_flat"] is True


def test_a_real_frame_reports_no_flat_frame(runtime):
    runtime._ring.image = dark_range_frame()
    runtime._start_auto_exposure()

    status = runtime.auto_exposure_status()
    assert status["frame_flat"] is False
    assert status["status"] == "lighting_required"
    assert runtime.capture_mode_status()["frame_flat"] is False


def test_manual_exposure_status_reports_the_flat_frame(tmp_path):
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path, settings=CameraCaptureSettings(auto_exposure=False)
    )
    runtime._ring = FakeRing(FLAT)

    status = runtime.auto_exposure_status()

    assert status["frame_flat"] is True
    assert status["status"] == "no_image"
    assert status["analysis_eligible"] is False


def test_a_flat_lock_is_retried_by_the_watch(runtime):
    runtime._start_auto_exposure()
    good = dark_range_frame()
    good[150:350, 100:540] = np.tile(np.linspace(45, 185, 440, dtype=np.uint8), (200, 1))
    runtime._ring.image = good

    assert runtime.retry_exposure_calibration() is True
    assert runtime.auto_exposure_status()["frame_flat"] is False


def test_a_saved_flat_capture_is_no_image_not_lighting():
    frames = tuple(
        CameraFrame(
            image=FLAT,
            sensor_timestamp_ns=index * 1_700_000,
            host_timestamp_ns=index * 1_700_000,
            exposure_us=1244,
            analogue_gain=15.94,
        )
        for index in range(10)
    )
    capture = TriggeredCapture(
        frames=frames, pre_trigger_count=5, trigger_host_timestamp_ns=5 * 1_700_000
    )

    result = capture_exposure_status(capture)

    assert result["status"] == "no_image"
    assert result["frame_flat"] is True
    assert result["analysis_eligible"] is False
    assert result["ball_analysis_eligible"] is False


def test_exposure_quality_api_reports_the_flat_frame(monkeypatch):
    runtime = SimpleNamespace(
        exposure_quality=lambda: measure_exposure(FLAT).to_dict(),
        auto_exposure_status=lambda: {"status": "no_image", "frame_flat": True},
    )
    monkeypatch.setattr(server, "camera_capture_runtime", runtime)

    with server.app.test_request_context():
        payload = server.camera_capture_exposure_quality().get_json()

    assert payload["status"] == "flat"
    assert payload["frame_flat"] is True


def test_exposure_quality_api_reports_no_flat_frame_for_a_real_image(monkeypatch):
    runtime = SimpleNamespace(
        exposure_quality=lambda: measure_exposure(dark_range_frame()).to_dict(),
        auto_exposure_status=lambda: {"status": "lighting_required", "frame_flat": False},
    )
    monkeypatch.setattr(server, "camera_capture_runtime", runtime)

    with server.app.test_request_context():
        payload = server.camera_capture_exposure_quality().get_json()

    assert payload["frame_flat"] is False


@pytest.mark.parametrize("level", [255, 235])
def test_a_clipped_uniform_frame_is_too_bright_not_flat(level):
    # Every pixel clipped (full-range 255, limited-range 235): an over-exposed scene,
    # not a sensor mode without a picture. It must step darker, not lock no_image.
    result = measure_exposure(np.full((400, 640), level, np.uint8))

    assert result.status == "too_bright"
    assert result.flat is False
    assert result.recommendation == "darker"


def test_the_policy_steps_darker_on_a_clipped_uniform_frame():
    policy = AutoExposurePolicy(fps=300)

    decision = policy.evaluate(
        measure_exposure(np.full((400, 640), 255, np.uint8)), exposure_us=1250, gain=16.0
    )

    assert decision.status == "adjusting"
    assert decision.target is not None
    assert decision.target.exposure_us * decision.target.gain < 1250 * 16.0


def test_startup_calibration_on_a_clipped_frame_steps_darker(runtime):
    runtime._ring.image = np.full((400, 640), 255, np.uint8)
    runtime._start_auto_exposure()

    runtime.update_image_controls.assert_called()
    status = runtime.auto_exposure_status()
    assert status["frame_flat"] is False
    assert status["status"] != "no_image"
