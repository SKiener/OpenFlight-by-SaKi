"""A camera must follow the light: recover from a bad start and re-measure when it changes.

At the range the startup calibration ran in the first second after app start
and locked "lighting_required" for the whole session (2026-10-03 too bright,
2026-10-05 too dark), so not one shot was analysed although the light was fine
later. On 2026-10-06 it locked "ready" in mild light; ten minutes later the sun
had the impact area clipping and the sky above the tee white, and nothing
re-measured. The runtime now keeps watching between shots.
"""

import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openflight.camera import capture_runtime
from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings

DARK = np.full((200, 320), 18, dtype=np.uint8)
BRIGHT = np.full((200, 320), 252, dtype=np.uint8)
# One pixel off the measured zones: a real frame is never one grey level (flat).
DARK[0, 0] = 255
BRIGHT[0, 0] = 0
GOOD = np.full((200, 320), 110, dtype=np.uint8)
GOOD[100:175, 90:230] = np.tile(np.linspace(45, 185, 140, dtype=np.uint8), (75, 1))


class FakeRing:
    """Latest frame plus a capture-busy flag that can stay set for a few checks."""

    def __init__(self, image):
        self.image = image
        self.busy_checks = 0

    @property
    def latest_frame(self):
        return SimpleNamespace(image=self.image)

    @property
    def capture_busy(self):
        if self.busy_checks > 0:
            self.busy_checks -= 1
            return True
        return False


@pytest.fixture(name="ring")
def fixture_ring():
    return FakeRing(DARK)


@pytest.fixture(name="runtime")
def fixture_runtime(tmp_path, ring, monkeypatch):
    monkeypatch.setattr(capture_runtime, "AUTO_EXPOSURE_STARTUP_SETTLE_S", 0.0)
    runtime = CameraCaptureRuntime(output_dir=tmp_path, settings=CameraCaptureSettings(fps=300))
    runtime._running = True
    runtime._camera = Mock()
    runtime._ring = ring
    yield runtime
    runtime._running = False
    runtime._auto_exposure_stop.set()
    if runtime._exposure_watch_stop is not None:
        runtime._exposure_watch_stop.set()


def lock_in_the_dark(runtime, ring):
    ring.image = DARK
    runtime._start_auto_exposure()
    assert runtime.auto_exposure_status()["status"] == "lighting_required"
    assert runtime.camera_analysis_eligible is False


def test_lighting_lock_is_remeasured_once_the_light_is_good(runtime, ring):
    lock_in_the_dark(runtime, ring)
    ring.image = GOOD

    assert runtime.retry_exposure_calibration() is True

    assert runtime.camera_analysis_eligible is True
    status = runtime.auto_exposure_status()
    assert status["status"] == "ready"
    assert status["analysis_eligible"] is True
    assert status["recalibrating"] is False


def test_lighting_lock_stays_while_the_light_is_still_bad(runtime, ring):
    lock_in_the_dark(runtime, ring)

    assert runtime.retry_exposure_calibration() is False

    assert runtime.camera_analysis_eligible is False
    assert runtime.auto_exposure_status()["status"] == "lighting_required"


def test_ready_camera_is_left_alone(runtime, ring):
    ring.image = GOOD
    runtime._start_auto_exposure()
    controls_before = runtime._camera.set_controls.call_count

    assert runtime.retry_exposure_calibration() is False

    assert runtime._camera.set_controls.call_count == controls_before
    assert runtime.camera_analysis_eligible is True


def test_shots_stay_ineligible_until_the_ring_holds_only_new_frames(runtime, ring, monkeypatch):
    lock_in_the_dark(runtime, ring)
    ring.image = GOOD
    seen = []

    def record_eligibility(_stop):
        seen.append(
            (runtime.camera_analysis_eligible, runtime.auto_exposure_status()["analysis_eligible"])
        )

    monkeypatch.setattr(runtime, "_wait_for_fresh_frames", record_eligibility)

    assert runtime.retry_exposure_calibration() is True

    assert seen == [(False, False)]
    assert runtime.camera_analysis_eligible is True


def test_remeasuring_never_changes_controls_during_a_shot(runtime, ring):
    lock_in_the_dark(runtime, ring)
    ring.image = BRIGHT  # the policy now has to step darker
    ring.busy_checks = 3
    changed_while_busy = []
    runtime._camera.set_controls.side_effect = lambda _controls: changed_while_busy.append(
        ring.busy_checks > 0
    )

    runtime.retry_exposure_calibration()

    assert changed_while_busy, "the retry never got to adjust the controls"
    assert not any(changed_while_busy)


def test_background_watch_turns_ready_and_keeps_watching(runtime, ring, monkeypatch):
    monkeypatch.setattr(capture_runtime, "AUTO_EXPOSURE_RETRY_S", 0.01)
    lock_in_the_dark(runtime, ring)
    runtime._start_exposure_watch()
    thread = runtime._exposure_watch_thread
    ring.image = GOOD

    deadline = time.monotonic() + 3.0
    while not runtime.camera_analysis_eligible and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.camera_analysis_eligible is True
    controls_when_ready = runtime._camera.set_controls.call_count
    time.sleep(0.1)

    # Ready is not the end: the watch stays for the next change of light,
    # and steady light leaves the controls alone.
    assert thread.is_alive()
    assert runtime._camera.set_controls.call_count == controls_when_ready


class LitScene:
    """The view at the runtime's current controls, scaled by exposure x gain like the sensor.

    Below: mat and ball with good contrast. Above: the sky the ball flies into.
    """

    def __init__(self, runtime, *, sky: float, light: float = 1.0):
        self.runtime = runtime
        self.light = light
        self.busy = False
        self.radiance = np.full((200, 320), 130.0)
        self.radiance[100:175, 90:230] = np.tile(np.linspace(60, 220, 140), (75, 1))
        self.radiance[:90] = sky

    @property
    def latest_frame(self):
        signal = self.runtime.settings.exposure_us * self.runtime.settings.gain / 1000.0
        image = np.clip(self.radiance * signal * self.light, 0, 255).astype(np.uint8)
        return SimpleNamespace(image=image)

    @property
    def capture_busy(self):
        return self.busy


def lit(runtime, *, sky: float) -> LitScene:
    """Start from the 250 us outdoor step under the given sky."""
    runtime.settings = replace(runtime.settings, exposure_us=250, gain=4.0)
    scene = LitScene(runtime, sky=sky)
    runtime._ring = scene
    return scene


def controls(runtime) -> tuple[int, float]:
    return runtime.settings.exposure_us, runtime.settings.gain


def test_startup_darkens_for_a_clipped_sky_above_the_tee(runtime):
    lit(runtime, sky=330)

    runtime._start_auto_exposure()

    assert controls(runtime) == (200, 3.0)
    assert runtime.auto_exposure_status()["status"] == "ready"
    assert runtime.camera_analysis_eligible is True


def test_ready_lock_is_remeasured_when_the_sun_comes_out(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_wait_for_fresh_frames", lambda _stop: None)
    scene = lit(runtime, sky=200)
    runtime._start_auto_exposure()
    assert controls(runtime) == (250, 4.0)
    scene.light = 2.0
    stop = threading.Event()

    first = runtime._check_exposure(stop, 0)
    assert first == 1
    assert controls(runtime) == (250, 4.0)

    assert runtime._check_exposure(stop, first) == 0
    assert controls(runtime) == (150, 3.0)
    status = runtime.auto_exposure_status()
    assert status["status"] == "ready"
    assert status["analysis_eligible"] is True
    assert status["recalibrating"] is False


def test_one_bright_check_does_not_remeasure(runtime):
    scene = lit(runtime, sky=200)
    runtime._start_auto_exposure()
    calls = runtime._camera.set_controls.call_count
    stop = threading.Event()

    scene.light = 2.0  # a white shirt passing the lens
    count = runtime._check_exposure(stop, 0)
    scene.light = 1.0
    count = runtime._check_exposure(stop, count)

    assert count == 0
    assert controls(runtime) == (250, 4.0)
    assert runtime._camera.set_controls.call_count == calls


def test_checks_pause_while_a_shot_is_captured(runtime):
    scene = lit(runtime, sky=200)
    runtime._start_auto_exposure()
    scene.light = 2.0
    scene.busy = True

    assert runtime._check_exposure(threading.Event(), 1) == 1
    assert controls(runtime) == (250, 4.0)


def test_manual_exposure_is_never_remeasured(runtime):
    scene = lit(runtime, sky=200)
    runtime._start_auto_exposure()
    runtime.settings = replace(runtime.settings, auto_exposure=False)
    scene.light = 2.0
    calls = runtime._camera.set_controls.call_count

    assert runtime._check_exposure(threading.Event(), 5) == 0
    assert runtime._camera.set_controls.call_count == calls


def test_stop_ends_the_retry_without_waiting_for_it(runtime, ring, monkeypatch):
    # _reconfigure_capture calls stop() while it holds the trigger lock; stop()
    # must not wait for a retry that is itself queued on that lock.
    monkeypatch.setattr(capture_runtime, "AUTO_EXPOSURE_RETRY_S", 0.01)
    lock_in_the_dark(runtime, ring)
    runtime._start_exposure_watch()
    thread = runtime._exposure_watch_thread

    stopped = threading.Event()
    with runtime._trigger_exposure_lock:
        threading.Thread(target=lambda: (runtime.stop(), stopped.set()), daemon=True).start()
        assert stopped.wait(timeout=1.0), "stop() blocked on the retry thread"

    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert runtime._camera is None
