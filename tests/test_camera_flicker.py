"""Floodlight flicker compensation and the low-light shadow ball path (camera.flicker, .lowlight).

Synthetic clips only: a static scene whose lit part (mat, ball) is multiplied by a
100 Hz-like waveform sampled at a different phase in every frame, an unlit sky that
does not flicker, and (for the end-to-end checks) the departed-ball renderer of
``test_camera_departed_ball`` with a ball leaving at a known direction.
"""

import json

import numpy as np
import pytest
from test_camera_departed_ball import (
    GEOMETRY,
    MPH_PER_MS,
    SPEED_MS,
    ZONE,
    _estimate,
    _evidence,
    _render,
    _trigger_ns,
)

from openflight.camera import lowlight as lowlight_module
from openflight.camera.ball_flight import CameraBallEstimate, estimate_live_ball_flight
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.departed_ball import GroundBallAnchor, mark_window
from openflight.camera.flicker import (
    FLICKER_MIN_REL_STD,
    FLICKER_REF_FRAMES,
    FlickerMeasurement,
    compensate_ball_flicker,
    compensate_flicker,
    flicker_levels,
    flicker_window,
)
from openflight.camera.lowlight import (
    LowLightBallFlight,
    LowLightGate,
    estimate_lowlight_ball_flight,
    flat_frame,
    flicker_roi_top,
    known_controls,
    lowlight_ball_gate,
    nearly_black,
    relaxed_live_ball_gate,
)
from openflight.camera.tee_zone import TeeZone

# Brightness factor of each frame: three phases of the floodlight (2026-10-30 swung by
# a factor of about 2.7 between the brightest and the darkest frame).
WAVE = (1.0, 0.37, 0.92)


def _waveform(count: int, wave=WAVE) -> np.ndarray:
    return np.array([wave[index % len(wave)] for index in range(count)], dtype=np.float64)


def _flickering(frames: np.ndarray, *, lit_from_row: int = 0, wave=WAVE) -> np.ndarray:
    """``frames`` with the rows from ``lit_from_row`` down multiplied by the waveform."""
    out = frames.astype(np.float64)
    out[:, lit_from_row:, :] *= _waveform(len(frames), wave)[:, None, None]
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _static_scene(count: int = 60, *, height: int = 120, width: int = 160, seed: int = 3):
    """Unlit sky (rows < 40, grey 62), lit mat (150) and a dark resting ball (40)."""
    rng = np.random.default_rng(seed)
    frame = np.full((height, width), 150.0)
    frame[:40, :] = 62.0
    yy, xx = np.mgrid[:height, :width]
    ball = (yy - 80) ** 2 + (xx - 80) ** 2 <= 6**2
    frame[ball] = 40.0
    frames = np.repeat(frame[None], count, axis=0) + rng.normal(0, 1.0, (count, height, width))
    return np.clip(np.rint(frames), 0, 255).astype(np.uint8), ball


# --- compensate_flicker ----------------------------------------------------------------


def test_a_flickering_static_scene_stops_flickering():
    clean, ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40)
    before = frames[:FLICKER_REF_FRAMES, 70:90, 70:90].astype(float).std(axis=0).mean()

    out, measured = compensate_flicker(frames, roi_top=60)

    after = out[:FLICKER_REF_FRAMES, 70:90, 70:90].astype(float).std(axis=0).mean()
    assert measured.compensated and measured.reason == "compensated"
    assert measured.rel_std == pytest.approx(np.std(WAVE) / np.mean(WAVE), rel=0.05)
    assert before > 30.0
    assert after < 2.0
    # The ball keeps its contrast against the mat in every frame.
    assert np.all(out[:, 60:100, 100:140].mean(axis=(1, 2)) - out[:, ball].mean(axis=1) > 50)
    assert out.dtype == np.uint8 and out.shape == frames.shape


def test_the_unlit_sky_is_left_alone():
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40)

    out, measured = compensate_flicker(frames, roi_top=60)

    assert measured.compensated
    assert np.abs(out[:, :40].astype(int) - frames[:, :40].astype(int)).max() <= 3


def test_something_moving_after_the_reference_frames_keeps_its_brightness():
    clean, _ball = _static_scene()
    moving = clean.copy()
    for index in range(30, 60):
        x = 10 + 4 * (index - 30)
        moving[index, 50:56, x : x + 6] = 250  # a bright club passing the mat
    frames = _flickering(moving, lit_from_row=40)
    # Lit like the scene: the club shows its own brightness only in a bright frame.
    bright = [index for index in range(30, 60) if WAVE[index % 3] == 1.0]

    out, measured = compensate_flicker(frames, roi_top=60)

    assert measured.compensated
    for index in bright:
        x = 10 + 4 * (index - 30)
        club = out[index, 50:56, x : x + 6].astype(float).mean()
        assert club > out[index, 60:70, 100:140].astype(float).mean() + 60


def test_a_steady_scene_comes_back_unchanged_as_the_same_array():
    frames, _ball = _static_scene()

    out, measured = compensate_flicker(frames, roi_top=60)

    assert out is frames
    assert not measured.compensated
    assert measured.reason == "below_threshold"
    assert measured.rel_std is not None and measured.rel_std < 0.01


def test_a_weak_flicker_below_the_threshold_is_not_compensated():
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40, wave=(1.0, 0.97, 1.0))

    out, measured = compensate_flicker(frames, roi_top=60)

    assert out is frames
    assert measured.reason == "below_threshold"
    assert 0.0 < measured.rel_std < FLICKER_MIN_REL_STD


def test_the_threshold_is_a_parameter():
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40)

    out, measured = compensate_flicker(frames, roi_top=60, min_rel_std=0.9)

    assert out is frames and measured.reason == "below_threshold"


@pytest.mark.parametrize("count", [1, 5])
def test_too_few_frames_are_not_compensated(count):
    clean, _ball = _static_scene(count=count)
    frames = _flickering(clean, lit_from_row=40)

    out, measured = compensate_flicker(frames, roi_top=60)

    assert out is frames
    assert (measured.compensated, measured.reason) == (False, "too_few_frames")


def test_a_black_hitting_area_gives_no_waveform():
    frames = np.zeros((40, 50, 60), dtype=np.uint8)

    out, measured = compensate_flicker(frames, roi_top=10)

    assert out is frames
    assert measured.reason == "no_lit_pixels"
    assert measured.rel_std is None


@pytest.mark.parametrize(
    "frames",
    [np.zeros((10, 10), dtype=np.uint8), np.zeros((0, 10, 10), dtype=np.uint8), None],
)
def test_invalid_frames_never_raise(frames):
    out, measured = compensate_flicker(frames, roi_top=0)

    assert out is frames
    assert measured.reason == "invalid_frames"


@pytest.mark.parametrize(
    ("roi_top", "expected", "reason"),
    [
        (None, 60, "compensated"),
        (-5, 0, "compensated"),
        (60, 60, "compensated"),
        # The last row alone holds too few lit pixels for a waveform.
        (500, 119, "no_lit_pixels"),
    ],
)
def test_the_waveform_rows_default_to_the_lower_half_and_are_clamped(roi_top, expected, reason):
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=0)

    _out, measured = compensate_flicker(frames, roi_top=roi_top)

    assert (measured.roi_top, measured.reason) == (expected, reason)


def _full_frame_flicker(count: int = 40, seed: int = 11) -> np.ndarray:
    """A 640x400 clip: unlit sky above row 150, a noisy lit mat below it flickering at WAVE."""
    rng = np.random.default_rng(seed)
    frame = np.full((400, 640), 120.0)
    frame[:150, :] = 10.0
    frames = np.repeat(frame[None], count, axis=0) + rng.normal(0, 6.0, (count, 400, 640))
    clean = np.clip(np.rint(frames), 0, 255).astype(np.uint8)
    return _flickering(clean, lit_from_row=150)


def test_a_large_lit_mat_is_measured_on_the_level_stride():
    # The live step's cost (2026-11-02 review): a full-resolution median over every lit
    # mat pixel per frame. Over a strided subset, as flicker_levels measures it, the mat
    # waveform is the same and the step several times cheaper.
    frames = _full_frame_flicker()

    out, measured = compensate_flicker(frames, roi_top=200)
    _levels, detected = flicker_levels(frames, roi_top=200)

    assert measured.reason == "compensated"
    assert measured.rel_std == detected.rel_std
    # lit_pixels still counts every lit mat pixel, as the session log always did.
    assert measured.lit_pixels == 200 * 640
    mat = out[:, 200:, :].astype(np.float64).mean(axis=(1, 2))
    assert mat.std() / mat.mean() < 0.01


def test_a_small_lit_mat_keeps_every_pixel_for_its_waveform():
    # Too few lit pixels on the stride for a waveform: the full set measures, as before.
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40)

    _out, measured = compensate_flicker(frames, roi_top=60)
    _levels, detected = flicker_levels(frames, roi_top=60)

    assert measured.reason == "compensated"
    assert detected.reason == "no_lit_pixels"


def test_the_measurement_is_json_ready():
    clean, _ball = _static_scene()

    _out, measured = compensate_flicker(
        _flickering(clean, lit_from_row=40), roi_top=60, window=(50, 50, 110, 110)
    )
    data = measured.to_dict()

    assert set(data) == {
        "compensated",
        "reason",
        "rel_std",
        "reference_frames",
        "lit_pixels",
        "roi_top",
        "window",
        "ball_rel_std",
        "ball_step_ratio",
        "ball_pixels",
        "detected_by",
        "elapsed_ms",
    }
    assert data["reference_frames"] == FLICKER_REF_FRAMES
    assert data["lit_pixels"] > 0
    assert data["window"] == [50, 50, 110, 110]
    assert data["ball_pixels"] > 0
    assert data["detected_by"] == "mat"
    json.dumps(data)
    empty = FlickerMeasurement(False, "invalid_frames").to_dict()
    assert empty["rel_std"] is None and empty["ball_rel_std"] is None and empty["window"] is None


# --- flicker at the ball on a calm mat (2026-11-02) ----------------------------------------

FPS = 288.0
WINDOW = (48, 48, 113, 113)  # around the ball at (80, 80), as a ball mark's window


def _mains_wave(count: int, *, depth: float = 0.35, hz: float = 100.0, fps: float = FPS):
    """The brightness factor of a lamp on mains, sampled by a short exposure every frame."""
    return 1.0 + depth * np.sin(2.0 * np.pi * hz * np.arange(count) / fps + 0.4)


def _flickering_ball_on_a_calm_mat(
    count: int = 60, *, depth: float = 0.35, mat_depth: float = 0.035, fps: float = FPS
):
    """A white ball lit by a mains lamp (+-35 %) on a dark mat the lamp barely reaches.

    2026-11-02 at 350 us: the ball swung by +-10-27 grey, the mat by +-2-7 (rel. std
    0.019-0.043, here 0.025), under the mat measurement's threshold.
    """
    rng = np.random.default_rng(5)
    height, width = 120, 160
    frame = np.full((height, width), 45.0)
    frame[:40, :] = 62.0
    yy, xx = np.mgrid[:height, :width]
    ball = (yy - 80) ** 2 + (xx - 80) ** 2 <= 7**2
    wave = _mains_wave(count, depth=depth, fps=fps)
    frames = np.repeat(frame[None], count, axis=0)
    frames[:, 40:, :] *= _mains_wave(count, depth=mat_depth, fps=fps)[:, None, None]
    frames[:, ball] = 70.0 * wave[:, None]
    frames += rng.normal(0, 1.5, frames.shape)
    return np.clip(np.rint(frames), 0, 255).astype(np.uint8), ball


# 536.5 fps: strip-560 delivers about 4.2 % below its request, like strip-500 (479).
@pytest.mark.parametrize("fps", [288.0, 479.0, 536.5, 560.0])
def test_a_ball_flickering_at_100_hz_on_a_calm_mat_is_detected_and_removed(fps):
    frames, ball = _flickering_ball_on_a_calm_mat(fps=fps)
    before = frames[:FLICKER_REF_FRAMES][:, ball].astype(float).std(axis=0).mean()

    out, measured = compensate_flicker(frames, roi_top=60, window=WINDOW)

    after = out[:FLICKER_REF_FRAMES][:, ball].astype(float).std(axis=0).mean()
    assert 0.01 < measured.rel_std < FLICKER_MIN_REL_STD  # the mat alone: "no flicker"
    assert measured.ball_rel_std > 0.2
    assert measured.ball_step_ratio > 0.5
    assert (measured.compensated, measured.detected_by) == (True, "ball")
    assert before > 15.0
    assert after < 2.5
    # The mat stops its own small swing too.
    mat_before = frames[:FLICKER_REF_FRAMES, 60:, 100:].astype(float).std(axis=0).mean()
    mat_after = out[:FLICKER_REF_FRAMES, 60:, 100:].astype(float).std(axis=0).mean()
    assert mat_after <= mat_before


def test_without_a_window_the_ball_flicker_is_not_measured():
    frames, _ball = _flickering_ball_on_a_calm_mat()

    out, measured = compensate_flicker(frames, roi_top=60)

    assert out is frames
    assert measured.reason == "below_threshold"
    assert measured.ball_rel_std is None and measured.ball_pixels == 0


def test_a_calm_scene_with_a_ball_window_comes_back_unchanged():
    frames, _ball = _flickering_ball_on_a_calm_mat(depth=0.0, mat_depth=0.0)

    out, measured = compensate_flicker(frames, roi_top=60, window=WINDOW)

    assert out is frames
    assert (measured.compensated, measured.reason, measured.detected_by) == (
        False,
        "below_threshold",
        None,
    )
    assert measured.ball_rel_std < 0.01


def test_something_moving_slowly_through_the_ball_window_is_not_flicker():
    """2026-10-25: a club or shadow drifting through the window swung its pixels by up to 12 %."""
    frames, _ball = _static_scene()
    moving = frames.astype(np.float64)
    for index in range(len(frames)):
        x = 55 + index  # a shadow edge one pixel per frame
        moving[index, 60:100, 55 : min(x, 105)] *= 0.6
    moving = np.clip(np.rint(moving), 0, 255).astype(np.uint8)

    out, measured = compensate_flicker(moving, roi_top=60, window=WINDOW)

    assert measured.ball_rel_std >= FLICKER_MIN_REL_STD
    assert measured.ball_step_ratio < 0.3
    assert out is moving and measured.reason == "below_threshold"


def test_a_flickering_mat_compensates_with_the_mat_s_waveform():
    clean, _ball = _static_scene()
    frames = _flickering(clean, lit_from_row=40)

    _out, measured = compensate_flicker(frames, roi_top=60, window=WINDOW)

    assert measured.compensated and measured.detected_by == "mat"
    assert measured.ball_rel_std > FLICKER_MIN_REL_STD


@pytest.mark.parametrize(
    ("window", "expected"),
    [((-10, -5, 30, 25), (0, 0, 30, 25)), ((150, 110, 400, 300), (150, 110, 160, 120))],
)
def test_the_ball_window_is_clamped_to_the_frame(window, expected):
    frames, _ball = _static_scene()

    _out, measured = compensate_flicker(frames, roi_top=60, window=window)

    assert measured.window == expected


def test_a_window_off_the_frame_or_too_dark_measures_no_ball():
    frames, _ball = _static_scene()

    _out, off = compensate_flicker(frames, roi_top=60, window=(500, 500, 600, 600))
    dark = frames.copy()
    dark[:, :40, :40] = 0
    _out, black = compensate_flicker(dark, roi_top=60, window=(0, 0, 40, 40))

    assert off.window is None and off.ball_rel_std is None
    assert black.window == (0, 0, 40, 40) and black.ball_rel_std is None


def test_the_ball_window_is_the_mark_s_else_the_fitting_hitting_zone():
    frames = np.zeros((3, 400, 640), dtype=np.uint8)
    other = TeeZone(**{**ZONE.__dict__, "image_width_px": 320, "image_height_px": 200})

    assert flicker_window(frames, tee_zone=ZONE, ball_mark_px=(300.0, 250.0)) == mark_window(
        (400, 640), (300.0, 250.0)
    )
    assert flicker_window(frames, tee_zone=ZONE) == (ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1)
    assert flicker_window(frames, tee_zone=other) is None
    assert flicker_window(frames) is None
    assert flicker_window(np.zeros((4, 4), np.uint8), ball_mark_px=(1.0, 1.0)) is None


def test_the_ball_path_step_measures_the_mat_below_the_zone_and_the_ball_in_its_window():
    frames, _times = _render(mat=46, ball=230)

    _out, measured = compensate_ball_flicker(frames, tee_zone=ZONE, ball_mark_px=(300.0, 250.0))
    _out, unmarked = compensate_ball_flicker(frames, tee_zone=ZONE)
    _out, no_zone = compensate_ball_flicker(frames)

    assert measured.roi_top == ZONE.y0
    assert measured.window == mark_window((400, 640), (300.0, 250.0))
    assert unmarked.window == (ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1)
    assert (no_zone.roi_top, no_zone.window) == (200, None)
    assert not measured.compensated


# --- the relaxed ball gate ---------------------------------------------------------------

# The live gate's verdict on a 2026-10-30 floodlight clip: blur risk high, too dark.
FLOODLIGHT = {
    "status": "lighting_required",
    "analysis_eligible": False,
    "motion_blur_risk": "high",
    "observation": {"status": "too_dark", "p90": 64.0, "median": 46.0, "dark_pct": 0.84},
    "ball_analysis_eligible": False,
}


def _with_observation(**observation):
    return {**FLOODLIGHT, "observation": {**FLOODLIGHT["observation"], **observation}}


def test_the_live_gate_decides_first():
    gate = lowlight_ball_gate(None, live_ball_eligible=True, controls_known=False)

    assert gate == LowLightGate(True, "live")
    assert not gate.relaxed_only


def test_a_dark_blurred_floodlight_clip_passes_only_the_relaxed_gate():
    gate = lowlight_ball_gate(FLOODLIGHT, live_ball_eligible=False, controls_known=True)

    assert gate == LowLightGate(True, "relaxed")
    assert gate.relaxed_only


@pytest.mark.parametrize(
    ("observation", "eligible"),
    [
        ({"p90": 12.0, "dark_pct": 90.0}, False),
        ({"p90": 5.0, "dark_pct": 99.0}, False),
        ({"p90": 12.0, "dark_pct": 89.9}, True),
        ({"p90": 12.1, "dark_pct": 99.0}, True),
        ({"p90": None, "dark_pct": 0.0}, False),
        ({"p90": 8.0, "dark_pct": None}, False),
        ({"p90": float("nan"), "dark_pct": 0.0}, False),
    ],
)
def test_only_a_nearly_black_clip_is_blocked(observation, eligible):
    gate = lowlight_ball_gate(
        _with_observation(**observation), live_ball_eligible=False, controls_known=True
    )

    assert gate.eligible is eligible
    assert gate.reason == ("relaxed" if eligible else "nearly_black")


@pytest.mark.parametrize("observation", [None, "x", {}])
def test_a_missing_observation_counts_as_nearly_black(observation):
    assert nearly_black(observation)
    gate = lowlight_ball_gate(
        {**FLOODLIGHT, "observation": observation}, live_ball_eligible=False, controls_known=True
    )
    assert gate == LowLightGate(False, "nearly_black")


# 2026-11-02, 320x200: every pixel 16. Not nearly black (p90 16 > 12), yet no picture.
FLAT_OBSERVATION = {"status": "flat", "p90": 16.0, "median": 16.0, "dark_pct": 0.0}


@pytest.mark.parametrize(
    "auto_exposure",
    [
        {**FLOODLIGHT, "status": "no_image", "frame_flat": True, "observation": FLAT_OBSERVATION},
        # The flat observation alone, without the frame_flat flag.
        {**FLOODLIGHT, "observation": FLAT_OBSERVATION},
        # The flag alone, with a dark-looking observation.
        {**FLOODLIGHT, "frame_flat": True},
    ],
    ids=["flag_and_observation", "observation_only", "flag_only"],
)
def test_a_flat_clip_never_passes_the_relaxed_gate(auto_exposure):
    assert not nearly_black(auto_exposure["observation"])  # why the flat check must exist

    gate = lowlight_ball_gate(auto_exposure, live_ball_eligible=False, controls_known=True)

    assert gate == LowLightGate(False, "flat_frame")
    assert flat_frame(auto_exposure)


@pytest.mark.parametrize(
    "auto_exposure", [None, "x", FLOODLIGHT, {**FLOODLIGHT, "frame_flat": False}]
)
def test_a_real_or_missing_exposure_status_is_not_flat(auto_exposure):
    assert not flat_frame(auto_exposure)


def test_unknown_controls_and_missing_metadata_block():
    assert lowlight_ball_gate(
        FLOODLIGHT, live_ball_eligible=False, controls_known=False
    ) == LowLightGate(False, "unknown_controls")
    assert lowlight_ball_gate(None, live_ball_eligible=False, controls_known=True) == LowLightGate(
        False, "no_exposure_metadata"
    )


def test_the_relaxed_gate_never_changes_the_saved_status():
    saved = {**FLOODLIGHT, "observation": dict(FLOODLIGHT["observation"])}

    lowlight_ball_gate(saved, live_ball_eligible=False, controls_known=True)

    assert saved == FLOODLIGHT


def _archive(count=4, exposure=1250, gain=16.0):
    return {
        "frames": np.zeros((count, 4, 4), dtype=np.uint8),
        "exposure_us": np.full(count, exposure, dtype=np.int32),
        "analogue_gain": np.full(count, gain, dtype=np.float32),
    }


@pytest.mark.parametrize(
    ("archive", "known"),
    [
        (_archive(), True),
        (_archive(exposure=0), False),
        (_archive(gain=0.0), False),
        (_archive(gain=float("nan")), False),
        ({**_archive(), "exposure_us": np.full(3, 1250)}, False),
        ({"frames": np.zeros((4, 4, 4), dtype=np.uint8)}, False),
        ({**_archive(count=0)}, False),
    ],
)
def test_known_controls_matches_the_capture_rule(archive, known):
    assert known_controls(archive) is known


def test_the_flicker_rows_come_from_a_fitting_hitting_zone():
    frames = np.zeros((3, 400, 640), dtype=np.uint8)

    assert flicker_roi_top(frames, ZONE) == ZONE.y0
    other = TeeZone(**{**ZONE.__dict__, "image_width_px": 320, "image_height_px": 200})
    assert flicker_roi_top(frames, other) is None
    assert flicker_roi_top(frames, None) is None


# --- the shadow ball path end to end -----------------------------------------------------


def _floodlit_clip(flight=(4.0, 20.0)):
    """A bright ball leaving a dark mat (as 2026-10-30: mat 46, ball core 223) under flicker."""
    frames, times = _render(mat=46, ball=230, flight=flight)
    return _flickering(frames, lit_from_row=0), times


def _lowlight(frames, times, *, relaxed_only=True, flight=(4.0, 20.0), **kwargs):
    defaults = {
        "timestamps_ns": times,
        "trigger_ns": _trigger_ns(),
        "range_evidence": _evidence(flight),
        "geometry": GEOMETRY,
        "ops_ball_speed_mph": SPEED_MS * MPH_PER_MS,
        "ball_locator": "disappeared",
        "tee_zone": ZONE,
    }
    return estimate_lowlight_ball_flight(
        frames=frames, relaxed_only=relaxed_only, **{**defaults, **kwargs}
    )


def test_flicker_hides_the_ball_from_the_live_locator_and_compensation_recovers_it():
    frames, times = _floodlit_clip()

    live = _estimate(frames, times)
    shadow = _lowlight(frames, times)

    assert live.horizontal_deg is None
    assert live.locator.reason == "low_snr"
    assert live.locator.snr < 4.0
    assert isinstance(shadow, LowLightBallFlight)
    assert shadow.flicker.compensated
    assert shadow.estimate.locator.locator == "disappeared"
    assert shadow.estimate.locator.reason == "located"
    assert shadow.estimate.locator.snr > 20.0
    assert shadow.estimate.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert (shadow.ball_locator, shadow.legacy_fallback) == ("disappeared", False)


def test_a_daylight_clip_runs_exactly_as_the_live_estimator():
    frames, times = _render()

    live = _estimate(frames, times)
    shadow = _lowlight(frames, times, relaxed_only=False)

    assert not shadow.flicker.compensated
    assert shadow.estimate == live
    assert shadow.legacy_fallback is True


def test_a_relaxed_only_clip_never_falls_back_to_the_legacy_locator():
    frames, times = _render(flight=False)

    fallback = _estimate(frames, times)
    shadow = _lowlight(frames, times, relaxed_only=True)

    assert fallback.locator.locator == "legacy_fallback"
    assert shadow.estimate.status == "rejected_no_departed_ball"
    assert shadow.estimate.horizontal_deg is None
    assert shadow.estimate.locator.locator == "disappeared"
    assert shadow.estimate.locator.reason == fallback.locator.reason


def test_a_relaxed_only_clip_ignores_a_legacy_locator_config():
    frames, times = _render()

    shadow = _lowlight(frames, times, relaxed_only=True, ball_locator="legacy")

    assert shadow.ball_locator == "disappeared"
    assert shadow.estimate.locator.locator == "disappeared"


def test_a_live_eligible_clip_keeps_the_configured_locator_and_fallback(monkeypatch):
    seen = {}

    def fake(frames, **kwargs):
        seen.update(kwargs, frames=frames)
        return "estimate"

    monkeypatch.setattr("openflight.camera.ball_flight.estimate_camera_ball_flight", fake)
    frames, times = _render(flight=False)

    result = _lowlight(frames, times, relaxed_only=False, ball_locator="legacy")

    assert result.estimate == "estimate"
    assert (seen["ball_locator"], seen["legacy_fallback"]) == ("legacy", True)
    assert seen["frames"] is frames
    assert seen["tee_zone"] is ZONE


def test_the_shadow_updates_only_the_state_it_is_given():
    frames, times = _floodlit_clip()
    tracker, anchor = ReferenceBallTracker(), GroundBallAnchor()

    _lowlight(frames, times, ball_tracker=tracker, ground_anchor=anchor)

    assert anchor.samples == 1


def test_the_legacy_fallback_default_is_unchanged():
    frames, times = _render(flight=False)

    default = _estimate(frames, times)
    explicit = _estimate(frames, times, legacy_fallback=True)

    assert default == explicit
    assert default.locator.locator == "legacy_fallback"


def test_the_worker_lists_the_lowlight_job():
    from openflight.camera import cv_worker  # noqa: PLC0415

    assert "lowlight_ball_flight" in cv_worker.JOBS
    assert lowlight_module.__all__


def test_a_clip_the_live_gate_admits_by_its_relaxed_dark_rule_stays_relaxed_in_the_shadow():
    live_relaxed = {**FLOODLIGHT, "motion_blur_risk": "low", "ball_analysis_eligible": True}
    live_relaxed["ball_analysis_relaxed"] = True

    gate = lowlight_ball_gate(live_relaxed, live_ball_eligible=True, controls_known=True)
    full = lowlight_ball_gate(
        {**live_relaxed, "ball_analysis_relaxed": False},
        live_ball_eligible=True,
        controls_known=True,
    )

    assert gate == LowLightGate(True, "relaxed") and gate.relaxed_only
    assert full == LowLightGate(True, "live")
    assert relaxed_live_ball_gate(live_relaxed)
    assert not relaxed_live_ball_gate(None)


def test_the_shadow_runs_the_live_flicker_step_with_the_ball_mark(monkeypatch):
    seen = {}

    def spy(frames, **kwargs):
        seen.update(kwargs)
        return frames, FlickerMeasurement(False, "below_threshold")

    monkeypatch.setattr(lowlight_module, "compensate_ball_flicker", spy)
    frames, times = _render()

    _lowlight(frames, times, relaxed_only=False, ball_mark_px=(300.0, 250.0))

    assert seen["tee_zone"] is ZONE
    assert seen["ball_mark_px"] == (300.0, 250.0)
    assert seen["min_rel_std"] == FLICKER_MIN_REL_STD


# --- the live ball flight compensates the flicker first --------------------------------------


def _live(frames, times, *, flight=(4.0, 20.0), **kwargs):
    defaults = {
        "trigger_ns": _trigger_ns(),
        "range_evidence": _evidence(flight),
        "geometry": GEOMETRY,
        "ops_ball_speed_mph": SPEED_MS * MPH_PER_MS,
        "ball_locator": "disappeared",
        "tee_zone": ZONE,
    }
    return estimate_live_ball_flight(frames, times, **{**defaults, **kwargs})


def test_the_live_ball_flight_recovers_a_flickering_ball_and_leaves_the_saved_frames_alone():
    frames, times = _floodlit_clip()
    saved = frames.copy()

    raw = _estimate(frames, times)
    live = _live(frames, times)

    assert raw.horizontal_deg is None and raw.locator.reason == "low_snr"
    assert live.locator.reason == "located"
    assert live.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert live.locator.flicker["compensated"] is True
    assert live.locator.flicker["detected_by"] == "mat"
    assert np.array_equal(frames, saved)


def test_the_live_ball_flight_of_a_calm_clip_is_the_estimator_s_own():
    frames, times = _render()

    plain = _estimate(frames, times)
    live = _live(frames, times)

    assert live == plain
    assert live.locator.flicker["reason"] == "below_threshold"
    assert plain.locator.flicker is None
    assert json.dumps(live.locator.to_dict())


def test_the_live_ball_flight_passes_the_estimator_its_arguments_and_the_ball_window(
    monkeypatch,
):
    seen = {}

    def fake(frames, timestamps_ns, **kwargs):
        seen.update(kwargs, frames=frames, timestamps_ns=timestamps_ns)
        return CameraBallEstimate("rejected_no_departed_ball")

    monkeypatch.setattr("openflight.camera.ball_flight.estimate_camera_ball_flight", fake)
    frames, times = _render()

    result = _live(frames, times, ball_mark_px=(300.0, 250.0), legacy_fallback=False)

    assert result == CameraBallEstimate("rejected_no_departed_ball")
    assert seen["frames"] is frames  # calm: the same array
    assert seen["timestamps_ns"] is times
    assert (seen["ball_mark_px"], seen["legacy_fallback"]) == ((300.0, 250.0), False)


def test_the_worker_s_ball_flight_job_is_the_live_one():
    from openflight.camera import cv_worker  # noqa: PLC0415

    frames, times = _floodlit_clip()
    kwargs = {
        "frames": frames,
        "timestamps_ns": times,
        "trigger_ns": _trigger_ns(),
        "range_evidence": _evidence(),
        "geometry": GEOMETRY,
        "ops_ball_speed_mph": SPEED_MS * MPH_PER_MS,
        "ball_locator": "disappeared",
        "tee_zone": ZONE,
        "ground_anchor": GroundBallAnchor(),
    }

    result, state = cv_worker.run_job("ball_flight", kwargs)

    assert result.locator.flicker["compensated"] is True
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert state["ground_anchor"].samples == 1


def test_without_a_mat_waveform_a_flickering_ball_is_reported_but_not_compensated():
    frames, _ball = _flickering_ball_on_a_calm_mat()
    dark_mat = frames.copy()
    dark_mat[:, 60:, :] = np.where(dark_mat[:, 60:, :] > 55, dark_mat[:, 60:, :], 0)

    out, measured = compensate_flicker(dark_mat, roi_top=60, window=WINDOW)

    assert out is dark_mat
    assert (measured.reason, measured.detected_by) == ("no_lit_pixels", "ball")
