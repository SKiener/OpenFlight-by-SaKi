"""Tests for deterministic high-speed camera auto exposure."""

import numpy as np
import pytest

from openflight.camera.auto_exposure import (
    EXPOSURE_STEPS,
    AutoExposurePolicy,
    ExposureObservation,
    exposure_steps_for_fps,
    measure_exposure,
    motion_blur_risk,
)


def observation(status, recommendation, *, median=50.0, p90=100.0, flight_clipped_pct=None):
    """Build one deterministic policy observation."""
    return ExposureObservation(
        sample_available=True,
        status=status,
        recommendation=recommendation,
        message=status,
        p10=10.0,
        median=median,
        p90=p90,
        contrast=p90 - 10.0,
        clipped_pct=0.0,
        dark_pct=0.0,
        flight_clipped_pct=flight_clipped_pct,
    )


def impact_zone_with_sky(sky: int) -> np.ndarray:
    """Good impact-zone contrast below, the sky the ball flies into above."""
    image = np.full((200, 320), 110, dtype=np.uint8)
    image[100:175, 90:230] = np.tile(np.linspace(45, 185, 140, dtype=np.uint8), (75, 1))
    image[:90] = sky
    return image


def step_index(exposure_us: int, gain: float) -> int:
    return next(
        index
        for index, step in enumerate(EXPOSURE_STEPS)
        if step.exposure_us == exposure_us and step.gain == gain
    )


def test_exposure_ladder_is_monotonic_and_respects_frame_period():
    assert [step.signal for step in EXPOSURE_STEPS] == sorted(
        step.signal for step in EXPOSURE_STEPS
    )
    assert exposure_steps_for_fps(488.0) == EXPOSURE_STEPS
    assert max(step.exposure_us for step in exposure_steps_for_fps(1000.0)) < 1000
    with pytest.raises(ValueError, match="positive"):
        exposure_steps_for_fps(0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(18, "too_dark"), (110, "good"), (252, "too_bright")],
)
def test_measure_exposure_classifies_impact_zone(value, expected):
    image = np.full((200, 320), value, dtype=np.uint8)
    # One pixel off the measured zones: a real frame is never one grey level (flat).
    image[0, 0] = 0 if value > 127 else 255
    if expected == "good":
        image[100:175, 90:230] = np.tile(
            np.linspace(45, 185, 140, dtype=np.uint8),
            (75, 1),
        )

    result = measure_exposure(image)

    assert result.status == expected
    assert result.sample_available is True


def test_measure_exposure_flags_a_clipped_ball_flight_area():
    # 2026-10-06 at the range: ball and mat fine, the sky above the tee white;
    # the ball vanished against it the moment it left the tee.
    result = measure_exposure(impact_zone_with_sky(255))

    assert result.status == "marginal"
    assert result.recommendation == "darker"
    assert result.flight_clipped_pct == pytest.approx(100.0)
    assert "flight" in result.message


def test_measure_exposure_keeps_a_good_impact_zone_under_an_unclipped_sky():
    result = measure_exposure(impact_zone_with_sky(200))

    assert result.status == "good"
    assert result.flight_clipped_pct == 0.0


def test_startup_darkens_until_the_ball_flight_area_stops_clipping():
    policy = AutoExposurePolicy(fps=300)

    first = policy.evaluate(
        observation("marginal", "darker", median=130.0, p90=190.0, flight_clipped_pct=100.0),
        exposure_us=250,
        gain=4.0,
    )
    second = policy.evaluate(
        observation("good", "hold", median=80.0, p90=116.0, flight_clipped_pct=0.0),
        exposure_us=first.target.exposure_us,
        gain=first.target.gain,
    )

    assert first.status == "adjusting"
    assert step_index(first.target.exposure_us, first.target.gain) == step_index(250, 4.0) - 1
    assert second.status == "ready"
    assert second.analysis_eligible is True


def test_startup_keeps_the_ball_area_usable_when_the_sky_cannot_be_unclipped():
    policy = AutoExposurePolicy(fps=300)
    clipped_sky = {"flight_clipped_pct": 100.0}
    exposure_us, gain = 250, 4.0
    seen = []
    for _ in range(len(EXPOSURE_STEPS)):
        index = step_index(exposure_us, gain)
        seen.append(index)
        if index <= step_index(100, 2.0):  # one step too dark for the ball area
            measured = observation("too_dark", "brighter", median=26.0, p90=38.0)
        else:
            measured = observation("marginal", "darker", median=80.0, p90=116.0, **clipped_sky)
        decision = policy.evaluate(measured, exposure_us=exposure_us, gain=gain)
        if not decision.should_apply:
            break
        exposure_us, gain = decision.target.exposure_us, decision.target.gain

    assert decision.status == "ready"
    assert decision.analysis_eligible is True
    assert (exposure_us, gain) == (150, 3.0)
    # Down to the step that lost the ball area, back once, then locked: no ping-pong.
    assert seen == [3, 2, 1, 0, 1]


def test_darkest_step_accepts_a_clipped_ball_flight_area():
    policy = AutoExposurePolicy(fps=300)

    decision = policy.evaluate(
        observation("marginal", "darker", median=80.0, p90=116.0, flight_clipped_pct=100.0),
        exposure_us=100,
        gain=2.0,
    )

    assert decision.status == "ready"
    assert not decision.should_apply


def test_measure_exposure_flags_nearly_black_view():
    image = np.zeros((200, 320), dtype=np.uint8)
    image[0, :4] = (3, 5, 2, 4)  # sensor noise: dark, not flat
    result = measure_exposure(image)

    assert result.status == "too_dark"
    assert "lens cover" in result.message


def test_startup_dark_scene_jumps_more_than_one_step():
    policy = AutoExposurePolicy(fps=488.0)

    decision = policy.evaluate(
        observation("too_dark", "brighter", median=18.0, p90=40.0),
        exposure_us=250,
        gain=4.0,
    )

    assert decision.status == "adjusting"
    assert decision.target is not None
    assert decision.target.signal > EXPOSURE_STEPS[4].signal
    assert decision.analysis_eligible is False


def test_startup_bright_scene_jumps_darker():
    policy = AutoExposurePolicy(fps=488.0)

    decision = policy.evaluate(
        observation("too_bright", "darker", median=240.0, p90=255.0),
        exposure_us=800,
        gain=18.0,
    )

    assert decision.target is not None
    assert decision.target.signal < 800 * 18


def test_acceptable_startup_observation_marks_analysis_ready():
    policy = AutoExposurePolicy(fps=488.0)

    decision = policy.evaluate(
        observation("good", "hold", median=100.0),
        exposure_us=500,
        gain=12.0,
    )

    assert decision.status == "ready"
    assert decision.analysis_eligible is True
    assert not decision.should_apply


def test_ready_setting_stays_locked_until_the_next_startup():
    policy = AutoExposurePolicy(fps=488.0)
    ready = policy.evaluate(
        observation("good", "hold"),
        exposure_us=500,
        gain=12.0,
    )

    changed_scene = policy.evaluate(
        observation("too_dark", "brighter", median=18.0, p90=40.0),
        exposure_us=500,
        gain=12.0,
    )
    policy.reset()
    next_startup = policy.evaluate(
        observation("too_dark", "brighter", median=18.0, p90=40.0),
        exposure_us=500,
        gain=12.0,
    )

    assert changed_scene is ready
    assert changed_scene.status == "ready"
    assert not changed_scene.should_apply
    assert next_startup.status == "adjusting"
    assert next_startup.should_apply
    assert policy.startup is True


def test_ladder_limit_stays_locked_until_the_next_startup():
    policy = AutoExposurePolicy(fps=488.0)
    darkest = EXPOSURE_STEPS[-1]

    failed = policy.evaluate(
        observation("too_dark", "brighter", median=10.0, p90=20.0),
        exposure_us=darkest.exposure_us,
        gain=darkest.gain,
    )
    locked = policy.evaluate(
        observation("marginal", "brighter", median=40.0, p90=95.0),
        exposure_us=darkest.exposure_us,
        gain=darkest.gain,
    )
    policy.reset()
    recovered = policy.evaluate(
        observation("marginal", "brighter", median=40.0, p90=95.0),
        exposure_us=darkest.exposure_us,
        gain=darkest.gain,
    )

    assert failed.status == "lighting_required"
    assert failed.analysis_eligible is False
    assert "add or redirect light" in failed.message
    assert locked is failed
    assert recovered.status == "ready"
    assert recovered.analysis_eligible is True


def test_startup_attempt_limit_requires_lighting_change():
    policy = AutoExposurePolicy(fps=488.0, startup_max_adjustments=1)
    dark = observation("too_dark", "brighter", median=18.0, p90=40.0)
    first = policy.evaluate(dark, exposure_us=250, gain=4.0)

    failed = policy.evaluate(
        dark,
        exposure_us=first.target.exposure_us,
        gain=first.target.gain,
    )

    assert failed.status == "lighting_required"
    assert not failed.should_apply


def test_default_startup_reaches_usable_low_setting_before_giving_up():
    policy = AutoExposurePolicy(fps=300)
    exposure_us, gain = 300, 5.0
    for _ in range(len(EXPOSURE_STEPS) + 1):
        usable = exposure_us * gain <= 200
        measured = observation(
            "marginal" if usable else "too_bright",
            "hold" if usable else "darker",
            median=71,
            p90=141 if usable else 255,
        )
        decision = policy.evaluate(measured, exposure_us=exposure_us, gain=gain)
        if not decision.should_apply:
            break
        exposure_us, gain = decision.target.exposure_us, decision.target.gain
    assert decision.status == "ready"
    assert decision.analysis_eligible
    assert exposure_us * gain <= 200


@pytest.mark.parametrize(
    ("exposure_us", "expected"),
    [(500, "low"), (650, "elevated"), (1000, "high")],
)
def test_motion_blur_risk(exposure_us, expected):
    assert motion_blur_risk(exposure_us) == expected
