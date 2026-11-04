"""Manual camera controls must rearm safely without bypassing quality checks."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings


def runtime_for_test(tmp_path):
    runtime = CameraCaptureRuntime(output_dir=tmp_path, settings=CameraCaptureSettings(fps=300))
    runtime._running = True
    runtime.stop = Mock()
    runtime.start = Mock()
    return runtime


def test_manual_controls_rearm_and_return_to_automatic(tmp_path):
    runtime = runtime_for_test(tmp_path)
    result = runtime.update_exposure_mode(automatic=False, exposure_us=350, gain=4)
    assert result == {"auto_exposure_enabled": False, "exposure_us": 350, "gain": 4.0}
    runtime.stop.assert_called_once()
    runtime.start.assert_called_once()
    assert runtime.settings.auto_exposure is False
    runtime.update_exposure_mode(automatic=True)
    assert runtime.settings.auto_exposure is True
    assert runtime.start.call_count == 2


@pytest.mark.parametrize(
    "controls",
    [
        {"exposure_us": 0},
        {"exposure_us": 1251},
        {"exposure_us": True},
        {"gain": 0},
        {"gain": 21},
        {"gain": float("nan")},
        {"gain": True},
    ],
)
def test_invalid_manual_controls_do_not_stop_capture(tmp_path, controls):
    runtime = runtime_for_test(tmp_path)
    with pytest.raises(ValueError):
        runtime.update_exposure_mode(automatic=False, **controls)
    runtime.stop.assert_not_called()


def test_failed_restart_restores_previous_settings(tmp_path):
    runtime = runtime_for_test(tmp_path)
    original = runtime.settings
    runtime.start.side_effect = [RuntimeError("camera busy"), None]
    with pytest.raises(RuntimeError, match="camera busy"):
        runtime.update_exposure_mode(automatic=False, exposure_us=350, gain=4)
    assert runtime.settings == original
    assert runtime.start.call_count == 2


def test_manual_mode_does_not_enable_analysis_without_usable_frames(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime.settings = replace(runtime.settings, auto_exposure=False)
    assert runtime.auto_exposure_status()["enabled"] is False
    assert runtime.camera_analysis_eligible is False


def test_no_reconfiguration_during_a_shot(tmp_path):
    runtime = runtime_for_test(tmp_path)
    runtime._ring = Mock(capture_busy=True)
    with pytest.raises(RuntimeError, match="capturing a shot"):
        runtime.update_exposure_mode(automatic=False, exposure_us=350)
    runtime.stop.assert_not_called()


@pytest.mark.parametrize(
    "controls", [{"automatic": "false"}, {"automatic": True, "exposure_us": 350}]
)
def test_rejects_ambiguous_modes(tmp_path, controls):
    runtime = runtime_for_test(tmp_path)
    with pytest.raises(ValueError):
        runtime.update_exposure_mode(**controls)
    runtime.stop.assert_not_called()
