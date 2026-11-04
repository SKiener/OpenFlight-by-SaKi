"""Static-reflector TX2 phase-zero calibration tool (synthetic dumps only)."""

import json
import math
import sys
from pathlib import Path

import pytest
from iwr6843_three_tx_synth import ball_flight_dump, static_reflector_dump

from openflight.iwr6843 import doa, lcmf
from openflight.iwr6843.shot import ShotMeasurement

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "iwr6843"))

import horizontal_phase_cal as cal_tool  # noqa: E402

PHASE_ZERO = -0.37
LATERAL_M = -0.072
REFLECTOR_HEIGHT_COSINE = -0.05


def _reflector(distance_m, *, phase_zero=PHASE_ZERO, extra=(), **kwargs):
    targets = [(distance_m, REFLECTOR_HEIGHT_COSINE, LATERAL_M / distance_m, 3000.0), *extra]
    return static_reflector_dump(targets, phase_zero_rad=phase_zero, **kwargs)


def _run(placements):
    positions = []
    for distance_m, raw in placements:
        evidence = cal_tool.reflector_evidence(raw, distance_m=distance_m, range_bias_m=0.0)
        positions.append(cal_tool.evaluate_position(distance_m, LATERAL_M, [evidence]))
    return cal_tool.summarize(positions)


def test_recovers_injected_phase_zero_at_two_distances():
    report = _run([(2.5, _reflector(2.5, seed=1)), (3.2, _reflector(3.2, seed=2))])

    assert report["status"] == "ok"
    assert report["recommended_horizontal_phase_reference_rad"] == pytest.approx(
        PHASE_ZERO, abs=0.01
    )
    assert report["launcher_argument"].startswith("--iwr6843-horizontal-phase-reference-rad -0.37")
    assert report["angle_shift_deg"] == pytest.approx(
        math.degrees(math.asin(PHASE_ZERO / math.pi)), abs=0.2
    )
    assert [p["n_frames"] for p in report["positions"]] == [24, 24]


def test_recommended_reference_restores_sign_of_a_known_target():
    """Target at +4 deg with the same electrical zero must read +4 deg."""
    report = _run([(2.5, _reflector(2.5, seed=3)), (3.2, _reflector(3.2, seed=4))])
    reference = report["recommended_horizontal_phase_reference_rad"]
    target = static_reflector_dump(
        [(3.0, 0.0, math.sin(math.radians(4.0)), 3000.0)], phase_zero_rad=PHASE_ZERO
    )
    evidence = cal_tool.reflector_evidence(target, distance_m=3.0, range_bias_m=0.0)

    angle_deg, _coherence = lcmf._referenced_horizontal_mean(  # pylint: disable=protected-access
        evidence.frame_phases_rad,
        [1.0] * len(evidence.frame_phases_rad),
        phase_reference_rad=reference,
    )

    assert angle_deg == pytest.approx(4.0, abs=0.2)


@pytest.mark.parametrize("hla_deg", [-6.0, 6.0])
def test_recommended_reference_fixes_the_production_ball_bearing(hla_deg):
    """End to end: the production HLCMF proxy reports the true bearing sign."""
    report = _run([(2.5, _reflector(2.5, seed=5)), (3.2, _reflector(3.2, seed=6))])
    reference = report["recommended_horizontal_phase_reference_rad"]
    flight = ball_flight_dump(hla_deg=hla_deg, vla_deg=12.0, phase_zero_rad=PHASE_ZERO)
    shot = ShotMeasurement(geometry=None, ball_found=True, track=flight.track)

    uncalibrated, _, _ = lcmf._tx2_horizontal_proxy(  # pylint: disable=protected-access
        flight.raw, shot, tdm_sign=1
    )
    calibrated, _, status = lcmf._tx2_horizontal_proxy(  # pylint: disable=protected-access
        flight.raw, shot, tdm_sign=1, phase_reference_rad=reference
    )

    tail = flight.positions_m[-8:]
    bearing = sum(math.degrees(math.asin(p[1] / math.dist(p, (0, 0, 0)))) for p in tail) / 8
    assert status == "hlcmf_v1_accepted"
    assert calibrated == pytest.approx(bearing, abs=0.3)
    assert math.copysign(1.0, calibrated) == math.copysign(1.0, hla_deg)
    assert uncalibrated - calibrated == pytest.approx(-report["angle_shift_deg"], abs=0.3)


def test_refuses_when_reflector_is_not_the_dominant_return():
    competitor = (2.75, 0.0, 0.2, 2800.0)
    raw = _reflector(2.5, extra=(competitor,), seed=7)

    report = _run([(2.5, raw)])

    assert report["status"] == "refused"
    assert report["recommended_horizontal_phase_reference_rad"] is None
    assert report["positions"][0]["dumps"][0]["rejected"] == "reflector_not_dominant"


def test_refuses_high_phase_scatter():
    raw = _reflector(2.5, frame_phase_jitter_rad=0.4, seed=8)

    report = _run([(2.5, raw)])

    assert report["status"] == "refused"
    assert "2.50 m: phase_scatter_too_high" in report["reasons"]


def test_refuses_when_distances_disagree():
    report = _run(
        [
            (2.5, _reflector(2.5, seed=9)),
            (3.2, _reflector(3.2, phase_zero=PHASE_ZERO + 0.3, seed=10)),
        ]
    )

    assert report["status"] == "refused"
    assert report["position_spread_rad"] == pytest.approx(0.3, abs=0.02)
    assert report["recommended_horizontal_phase_reference_rad"] is None


def test_single_distance_is_a_warning_not_a_refusal():
    report = _run([(2.5, _reflector(2.5, seed=11))])

    assert report["status"] == "warning"
    assert report["recommended_horizontal_phase_reference_rad"] == pytest.approx(
        PHASE_ZERO, abs=0.01
    )


def test_distance_outside_capture_window_is_rejected_explicitly():
    with pytest.raises(ValueError, match="outside the captured range windows"):
        cal_tool.reflector_evidence(_reflector(2.5), distance_m=8.0, range_bias_m=0.0)


def test_far_reflector_only_uses_frames_whose_window_contains_it():
    raw = _reflector(4.2, seed=12)
    evidence = cal_tool.reflector_evidence(raw, distance_m=4.2, range_bias_m=0.0)

    assert len(evidence.frame_phases_rad) == 8


def test_expected_phase_follows_production_sign_convention():
    phase = cal_tool.expected_phase_rad(3.0, 0.3)

    assert -math.degrees(doa.tx2_phase_to_axis_angle_rad(phase)) == pytest.approx(
        math.degrees(math.asin(0.1))
    )


def test_cli_writes_report_and_signals_refusal(tmp_path):
    good = tmp_path / "good.l3dump"
    good.write_bytes(_reflector(2.5, seed=13))
    out = tmp_path / "report.json"
    args = ["--position", "2.5", str(good), "--reflector-lateral-m", str(LATERAL_M)]

    assert cal_tool.main([*args, "--output", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["recommended_horizontal_phase_reference_rad"] == pytest.approx(
        PHASE_ZERO, abs=0.02
    )

    noisy = tmp_path / "noisy.l3dump"
    noisy.write_bytes(_reflector(2.5, frame_phase_jitter_rad=0.5, seed=14))
    assert cal_tool.main(["--position", "2.5", str(noisy), "--reflector-lateral-m", "0"]) == 2
