"""Bearing-to-launch-direction trajectory fit (default off, synthetic flights)."""

import json
import math

import numpy as np
import pytest
from iwr6843_three_tx_synth import ball_flight_dump

from openflight.iwr6843 import Calibration, doa, estimate_lcmf_v1, lcmf

PHASE_ZERO = -0.37
RADAR_HEIGHT_M = 0.15
BALL_HEIGHT_M = 0.02
SPEED_MPH = 45.0 * 2.23694
LEGACY_KEYS = {
    "estimator",
    "status",
    "launch_angle_deg",
    "raw_angle_deg",
    "angle_correction_deg",
    "components_deg",
    "n_snapshots",
    "n_frames",
    "component_std_deg",
    "channels_used",
    "single_channel",
    "tracker_quality",
    "track_speed_mph",
    "track_rms_bins",
    "track_inliers",
    "track_span_s",
    "impact_t_s",
    "tdm_sign_used",
    "horizontal_deg",
    "horizontal_raw_deg",
    "horizontal_confidence",
    "horizontal_status",
    "effective_tdm_tau_s",
    "effective_loop_period_s",
}


def _tee(lateral_m):
    return (1.5, lateral_m, BALL_HEIGHT_M - RADAR_HEIGHT_M)


def _cal(lateral_m):
    cal = Calibration.identity()
    cal.tilt_rad = math.radians(10.0)
    cal.tee_range_m = float(np.linalg.norm(_tee(lateral_m)))
    cal.tee_ball_height_m = BALL_HEIGHT_M
    cal.meta["radar_height_m"] = RADAR_HEIGHT_M
    return cal


def _estimate(flight_kwargs, *, lateral_m, **kwargs):
    flight = ball_flight_dump(tee_m=_tee(lateral_m), phase_zero_rad=PHASE_ZERO, **flight_kwargs)
    return estimate_lcmf_v1(
        flight.raw, _cal(lateral_m), ball_speed_mph=SPEED_MPH, club="7i", **kwargs
    )


def _frames(hla_deg, vla_deg, *, lateral_m, n=10, phase_noise=0.0, seed=0):
    """(range, TX2 phase, coherence) along a straight flight from the tee."""
    rng = np.random.default_rng(seed)
    distance = np.linspace(0.3, 2.4, n)
    hla, vla = math.radians(hla_deg), math.radians(vla_deg)
    x = 1.5 + distance * math.cos(vla) * math.cos(hla)
    y = lateral_m + distance * math.cos(vla) * math.sin(hla)
    z = BALL_HEIGHT_M - RADAR_HEIGHT_M + distance * math.sin(vla)
    ranges = np.sqrt(x * x + y * y + z * z)
    phases = doa.tx2_axis_angle_to_phase_rad(-np.arcsin(y / ranges)) + PHASE_ZERO
    phases = phases + rng.normal(0.0, phase_noise, n) if phase_noise else phases
    return [(float(r), float(p), 0.99) for r, p in zip(ranges, phases)]


def _fit(frames, *, lateral_m, vla_deg, reference=PHASE_ZERO):
    return lcmf.fit_horizontal_trajectory(
        frames,
        phase_reference_rad=reference,
        vertical_deg=vla_deg,
        tee_x_m=math.sqrt(float(np.linalg.norm(_tee(lateral_m))) ** 2 - 0.13**2 - lateral_m**2),
        tee_lateral_m=lateral_m,
        tee_height_m=BALL_HEIGHT_M - RADAR_HEIGHT_M,
    )


@pytest.mark.parametrize("hla_deg", [-9.0, -3.0, 0.0, 4.0, 10.0])
@pytest.mark.parametrize("lateral_m", [-0.072, 0.0, 0.05])
def test_fit_recovers_launch_direction_not_bearing(hla_deg, lateral_m):
    fit = _fit(_frames(hla_deg, 18.0, lateral_m=lateral_m), lateral_m=lateral_m, vla_deg=18.0)

    assert fit.status == "trajectory_fit_accepted"
    assert fit.angle_deg == pytest.approx(hla_deg, abs=0.05)
    assert fit.n_frames == 10
    assert fit.rms_m == pytest.approx(0.0, abs=1e-3)


def test_fit_tolerates_noisy_phases():
    errors = [
        _fit(
            _frames(6.0, 15.0, lateral_m=-0.072, phase_noise=0.05, seed=seed),
            lateral_m=-0.072,
            vla_deg=15.0,
        ).angle_deg
        - 6.0
        for seed in range(20)
    ]

    assert abs(np.mean(errors)) < 0.5
    assert np.std(errors) < 1.5


def test_too_few_frames_falls_back_with_reason():
    fit = _fit(_frames(5.0, 15.0, lateral_m=0.0, n=3), lateral_m=0.0, vla_deg=15.0)

    assert fit.angle_deg is None
    assert fit.status == "trajectory_fallback_too_few_frames"


def test_incoherent_frames_do_not_count():
    frames = [(r, p, 0.5) for r, p, _c in _frames(5.0, 15.0, lateral_m=0.0)]

    assert _fit(frames, lateral_m=0.0, vla_deg=15.0).status == (
        "trajectory_fallback_too_few_frames"
    )


def test_short_downrange_span_falls_back_with_reason():
    frames = _frames(5.0, 15.0, lateral_m=0.0)[:5]
    frames = [(1.52 + 0.01 * i, p, c) for i, (_r, p, c) in enumerate(frames)]

    assert _fit(frames, lateral_m=0.0, vla_deg=15.0).status == ("trajectory_fallback_short_span")


@pytest.mark.parametrize("hla_deg", [-7.0, 7.0])
def test_flag_reports_launch_direction_end_to_end(hla_deg):
    lateral_m = -0.072
    result = _estimate(
        {"hla_deg": hla_deg, "vla_deg": 18.0, "noise": 3.0, "n_frames": 24},
        lateral_m=lateral_m,
        horizontal_phase_reference_rad=PHASE_ZERO,
        horizontal_trajectory_fit=True,
        tee_lateral_m=lateral_m,
    )

    data = result.to_dict()
    assert result.accepted
    assert data["horizontal_source"] == lcmf.TRAJECTORY_SOURCE
    assert data["horizontal_trajectory_status"] == "trajectory_fit_accepted"
    assert data["horizontal_deg"] == pytest.approx(hla_deg, abs=1.0)
    assert abs(data["horizontal_bearing_deg"] - hla_deg) > 1.5
    assert data["horizontal_trajectory_frames"] >= 4


def test_flag_falls_back_to_bearing_with_lower_confidence(monkeypatch):
    monkeypatch.setattr(lcmf, "TRAJECTORY_MIN_FRAMES", 100)
    kwargs = {"hla_deg": 5.0, "vla_deg": 18.0, "noise": 3.0, "n_frames": 24}
    bearing = _estimate(kwargs, lateral_m=-0.072, horizontal_phase_reference_rad=PHASE_ZERO)
    fallback = _estimate(
        kwargs,
        lateral_m=-0.072,
        horizontal_phase_reference_rad=PHASE_ZERO,
        horizontal_trajectory_fit=True,
        tee_lateral_m=-0.072,
    )

    assert fallback.horizontal_deg == bearing.horizontal_deg
    assert fallback.horizontal_source == lcmf.BEARING_SOURCE
    assert fallback.horizontal_trajectory_status == "trajectory_fallback_too_few_frames"
    assert fallback.horizontal_confidence == pytest.approx(
        bearing.horizontal_confidence * lcmf.TRAJECTORY_FALLBACK_CONFIDENCE_FACTOR
    )


def test_default_output_is_unchanged():
    kwargs = {"hla_deg": 5.0, "vla_deg": 18.0, "noise": 3.0, "n_frames": 24}
    default = _estimate(kwargs, lateral_m=-0.072)
    explicit_off = _estimate(kwargs, lateral_m=-0.072, horizontal_trajectory_fit=False)

    assert set(default.to_dict()) == LEGACY_KEYS
    assert json.dumps(default.to_dict()) == json.dumps(explicit_off.to_dict())
    assert default.horizontal_source is None
