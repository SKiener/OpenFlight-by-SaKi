"""--range-ball-drag-slope: the unvalidated range-ball drag, off by default.

Candidate (c1) of analysis/flugmodell-20261103/aero: above Re 1.2e5 the drag coefficient
grows by slope * (Re - 1.2e5) / 1e5. At the default 0.0 every flight must stay exactly
today's.
"""

import argparse
import hashlib
import math
import sys
from dataclasses import astuple
from datetime import datetime

import pytest

from openflight import ballistics, server
from openflight.ballistics import (
    AIR_DYNAMIC_VISCOSITY_PA_S,
    BALL_RADIUS_M,
    M_TO_YD,
    RANGE_BALL_DRAG_RE_KNEE,
    RANGE_BALL_DRAG_SLOPE_RANGE,
    LaunchConditions,
    carry_span_yards,
    resolve_launch,
    simulate,
)
from openflight.clubs import ClubType
from openflight.clubs.physics import CLUB_PHYSICS
from openflight.launch_monitor import Shot
from openflight.weather import AirDensityChoice

pytestmark = pytest.mark.usefixtures("isolate_finalization")

STUDY_SLOPE = 0.3546
# 02.11. density of the study, mean of the two weather readings (aero/aerolib.py).
STUDY_DENSITY = (1.2006 + 1.202) / 2

# carry_yards, apex_yards of each club's TrackMan baseline launch (average ball speed,
# optimal launch, tour spin, straight) at 1.184 kg/m³, flown by the code before the
# range-ball drag existed (88563cc6). The default must keep them.
BASELINE_FLIGHTS_YD = {
    ClubType.DRIVER: (226.2987480673613, 22.014664064117685),
    ClubType.WOOD_3: (210.86676594142136, 25.194236789733807),
    ClubType.WOOD_5: (194.75926818011936, 26.984850658098136),
    ClubType.WOOD_7: (180.63708139079287, 27.926853607482713),
    ClubType.HYBRID_3: (182.62191791687096, 23.85764656353588),
    ClubType.HYBRID_5: (171.77836900277, 24.962248653772555),
    ClubType.HYBRID_7: (159.32137802683783, 24.763801662964713),
    ClubType.HYBRID_9: (146.74149485863495, 24.167636276688455),
    ClubType.IRON_2: (176.5897414808227, 20.486204749138505),
    ClubType.IRON_3: (172.98955773069116, 23.330698851830835),
    ClubType.IRON_4: (164.1789759666093, 24.650543462844468),
    ClubType.IRON_5: (155.62898846922903, 25.337653699452872),
    ClubType.IRON_6: (144.6069219545841, 25.171676227251027),
    ClubType.IRON_7: (134.4296807030171, 24.48213169899869),
    ClubType.IRON_8: (122.35988852901346, 23.976532789755957),
    ClubType.IRON_9: (112.053799879486, 22.793783423473805),
    ClubType.PW: (102.33879836834127, 21.47625878923935),
    ClubType.GW: (92.51573057097438, 19.57835433413383),
    ClubType.SW: (87.64932922015275, 19.415836370079298),
    ClubType.LW: (82.30126887722177, 19.971594369646706),
    ClubType.UNKNOWN: (176.7136476382571, 31.648388350829954),
}

# Point count and SHA-256 of repr(_trajectory_values(...)) -- every sampled point and every
# touchdown value -- of a curved launch (aim 3 deg right, spin axis -6 deg) at 1.2006 kg/m³,
# flown by 88563cc6 (CPython 3.11, macOS arm64).
BASELINE_TRAJECTORY_SHA256 = {
    ClubType.DRIVER: (111, "2e45b2433d4290c653bc2d6c5c307cfbe1cfb8f46b9b1b2fa38bc5425a9acc03"),
    ClubType.IRON_7: (104, "315ca81e2d2838614f48d0b444501c84e4b58f529a6ef7fa8dcd47ea296d58d9"),
    ClubType.SW: (85, "a2f6012ed2cdcf1c3f30d21dc20e70ef2f60a78b4705d03c7d4380a3aef1e578"),
}


def _baseline(club, hla=0.0, axis=0.0):
    physics = CLUB_PHYSICS[club]
    return LaunchConditions(
        physics.average_ball_speed_mph,
        physics.optimal_launch_deg,
        hla,
        physics.typical_spin_rpm,
        axis,
        "club_typical",
    )


def _trajectory_values(trajectory):
    return [astuple(point) for point in trajectory.points], (
        trajectory.carry_yards,
        trajectory.apex_yards,
        trajectory.lateral_yards,
        trajectory.flight_time_s,
        trajectory.landing_speed_mph,
        trajectory.landing_angle_deg,
        trajectory.touchdown_velocity_mps,
        trajectory.touchdown_backspin_rad_s,
    )


def _effect_m(conditions, slope, density=STUDY_DENSITY):
    base = simulate(conditions, air_density=density)
    braked = simulate(conditions, air_density=density, range_ball_drag_slope=slope)
    return (
        (braked.carry_yards - base.carry_yards) / M_TO_YD,
        (braked.apex_yards - base.apex_yards) / M_TO_YD,
    )


# ---------------------------------------------------------------- ballistics


def test_constants_are_the_studied_knee_and_viscosity():
    assert RANGE_BALL_DRAG_RE_KNEE == 1.2e5
    assert ballistics.RANGE_BALL_DRAG_RE_SCALE == 1e5
    assert AIR_DYNAMIC_VISCOSITY_PA_S == 1.81e-5
    assert RANGE_BALL_DRAG_SLOPE_RANGE == (0.0, 1.0)


@pytest.mark.parametrize("club", list(ClubType))
def test_default_keeps_the_flight_before_the_drag_existed(club):
    trajectory = simulate(_baseline(club), air_density=1.184)

    # Exact: the stored reprs round-trip, so any changed bit fails.
    assert (trajectory.carry_yards, trajectory.apex_yards) == BASELINE_FLIGHTS_YD[club]


@pytest.mark.parametrize("club", list(BASELINE_TRAJECTORY_SHA256))
@pytest.mark.parametrize("slope", [None, 0.0])
def test_default_keeps_the_whole_trajectory_before_the_drag_existed(club, slope):
    conditions = _baseline(club, hla=3.0, axis=-6.0)
    kwargs = {} if slope is None else {"range_ball_drag_slope": slope}
    points, touchdown = _trajectory_values(simulate(conditions, air_density=1.2006, **kwargs))

    digest = hashlib.sha256(repr((points, touchdown)).encode()).hexdigest()
    assert (len(points), digest) == BASELINE_TRAJECTORY_SHA256[club]


@pytest.mark.parametrize("club", list(ClubType))
@pytest.mark.parametrize("hla,axis,density", [(0.0, 0.0, 1.184), (3.0, -6.0, 1.2006)])
def test_slope_zero_is_bit_identical_to_the_default(club, hla, axis, density):
    conditions = _baseline(club, hla=hla, axis=axis)

    assert _trajectory_values(
        simulate(conditions, air_density=density, range_ball_drag_slope=0.0)
    ) == _trajectory_values(simulate(conditions, air_density=density))


@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.IRON_7, ClubType.SW])
def test_slope_zero_span_is_bit_identical_to_the_default(club):
    conditions = _baseline(club, axis=5.0)

    assert carry_span_yards(conditions, range_ball_drag_slope=0.0) == carry_span_yards(conditions)


def test_derivatives_follow_the_study_formula():
    """Above the knee the drag acceleration grows by s * (Re - 1.2e5) / 1e5; lift stays."""
    vx, vz = 60.0, 10.0
    state = (0.0, 0.0, 1.0, vx, 0.0, vz)
    omega, axis, density, slope = 250.0, (0.0, -1.0, 0.0), 1.2, 0.5
    v = math.hypot(vx, vz)
    re = density * v * 2 * BALL_RADIUS_M / 1.81e-5
    assert re > 1.2e5

    plain = ballistics._derivatives(state, omega, axis, density)
    braked = ballistics._derivatives(state, omega, axis, density, slope)

    cd = ballistics._cd(BALL_RADIUS_M * omega / v)
    q = 0.5 * density * v * v * ballistics.BALL_AREA_M2 / ballistics.BALL_MASS_KG
    extra_drag = cd * q * slope * (re - 1.2e5) / 1e5
    assert braked[:3] == plain[:3]
    assert braked[3] - plain[3] == pytest.approx(-extra_drag * vx / v)
    assert braked[4] == plain[4] == 0.0
    assert braked[5] - plain[5] == pytest.approx(-extra_drag * vz / v)


def test_below_the_knee_the_drag_is_unchanged():
    state = (0.0, 0.0, 1.0, 20.0, 0.0, 5.0)  # ~20.6 m/s: Re ~ 5.9e4
    args = (state, 300.0, (0.0, -1.0, 0.0), 1.2)
    assert ballistics._derivatives(*args, 1.0) == ballistics._derivatives(*args)


def test_study_slope_shortens_the_typical_driver_as_in_the_study():
    """aero/08_effects_log.txt: 147.1 mph, 9.4 deg, table spin 2700 -> carry -10.7 m, apex -1.4 m."""
    carry_m, apex_m = _effect_m(LaunchConditions(147.139, 9.43, 0.0, 2700, 0.0, "club_typical"), 0.3546)

    assert carry_m == pytest.approx(-10.7, abs=0.5)
    assert apex_m == pytest.approx(-1.4, abs=0.3)


def test_study_slope_shortens_the_typical_3_wood_as_in_the_study():
    """aero/08_effects_log.txt: 140.0 mph, 9.3 deg, table spin 3500 -> carry -7.8 m."""
    carry_m, _ = _effect_m(LaunchConditions(139.969, 9.29, 0.0, 3500, 0.0, "club_typical"), 0.3546)

    assert carry_m == pytest.approx(-7.8, abs=0.5)


@pytest.mark.parametrize(
    "ball_speed,launch,spin",
    [
        (93.525, 29.045, 9000),  # PW, typical shot of the study
        (93.525, 29.045, 2310),  # the same with the range-ball spin
        (78.7625, 38.02, 10000),  # SW
        (83.2, 33.5, 10500),  # LW
    ],
)
def test_study_slope_leaves_wedges_unchanged(ball_speed, launch, spin):
    carry_m, apex_m = _effect_m(
        LaunchConditions(ball_speed, launch, 0.0, spin, 0.0, "club_typical"), STUDY_SLOPE
    )

    assert abs(carry_m) < 0.1
    assert abs(apex_m) < 0.1


def test_span_flies_with_the_given_slope():
    conditions = LaunchConditions(147.139, 9.43, 0.0, 2700, 0.0, "club_typical")
    braked = simulate(conditions, range_ball_drag_slope=STUDY_SLOPE).carry_yards

    low, high = carry_span_yards(
        conditions, full_spin_carry_yards=braked, range_ball_drag_slope=STUDY_SLOPE
    )

    assert low <= braked <= high
    assert high < carry_span_yards(conditions)[1]


# ---------------------------------------------------------------- server


def _driver_shot():
    return Shot(
        ball_speed_mph=147.1,
        timestamp=datetime.now(),
        club=ClubType.DRIVER,
        launch_angle_vertical=9.4,
        launch_angle_confidence=0.95,
        spin_rpm=2500.0,
        spin_confidence=0.59,
    )


def test_server_default_slope_is_off():
    assert server.range_ball_drag_slope == 0.0


def test_live_carry_flies_the_slope(monkeypatch, _hold_carry_span_jobs):
    monkeypatch.setattr(server, "range_ball_drag_slope", STUDY_SLOPE)
    shot = _driver_shot()

    server._finalize_shot_detected(shot, emit_event="shot")

    conditions = resolve_launch(_driver_shot())
    expected = simulate(
        conditions, air_density=server.air_density_kg_m3, range_ball_drag_slope=STUDY_SLOPE
    )
    assert shot.carry_spin_adjusted == expected.carry_yards
    assert shot.apex_yards == expected.apex_yards
    # The export carry flies the same model at the fixed density.
    assert shot.carry_standard_air_yards == expected.carry_yards
    plain = simulate(conditions, air_density=server.air_density_kg_m3).carry_yards
    assert shot.carry_spin_adjusted < plain - 5.0

    # The span is flown with the carry's slope, so it contains that carry.
    _hold_carry_span_jobs[0]()
    assert shot.carry_span_min_yards <= shot.carry_spin_adjusted <= shot.carry_span_max_yards


def test_live_default_carry_is_unchanged():
    shot = _driver_shot()

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_spin_adjusted == simulate(
        resolve_launch(_driver_shot()), air_density=server.air_density_kg_m3
    ).carry_yards


def test_weather_carry_and_standard_air_carry_both_fly_the_slope(monkeypatch):
    monkeypatch.setattr(server, "range_ball_drag_slope", STUDY_SLOPE)
    monkeypatch.setattr(
        server,
        "_air_density_for_shot",
        lambda: AirDensityChoice(kg_m3=1.2006, source="open-meteo", weather_age_s=60.0),
    )
    shot = _driver_shot()
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.carry_standard_air_yards is None  # no simulator connected

    server._ensure_standard_air_carry(shot)

    conditions = resolve_launch(_driver_shot())
    assert shot.carry_spin_adjusted == simulate(
        conditions, air_density=1.2006, range_ball_drag_slope=STUDY_SLOPE
    ).carry_yards
    assert shot.carry_standard_air_yards == simulate(
        conditions, air_density=server.air_density_kg_m3, range_ball_drag_slope=STUDY_SLOPE
    ).carry_yards


def test_session_start_config_records_the_slope(monkeypatch):
    assert server._session_start_config()["range_ball_drag_slope"] == 0.0
    monkeypatch.setattr(server, "range_ball_drag_slope", STUDY_SLOPE)
    assert server._session_start_config()["range_ball_drag_slope"] == STUDY_SLOPE


def test_session_start_config_records_no_slope_without_ballistics(monkeypatch):
    monkeypatch.setattr(server, "range_ball_drag_slope", STUDY_SLOPE)
    monkeypatch.setattr(server, "ballistics_enabled", False)

    assert server._session_start_config()["range_ball_drag_slope"] is None


def test_cli_flag_defaults_to_off_and_accepts_the_range():
    parser = argparse.ArgumentParser()
    server._add_range_ball_drag_arguments(parser)

    assert parser.parse_args([]).range_ball_drag_slope == 0.0
    for value, expected in (("0", 0.0), ("0.3546", 0.3546), ("0.225", 0.225), ("1.0", 1.0)):
        args = parser.parse_args(["--range-ball-drag-slope", value])
        assert args.range_ball_drag_slope == expected


@pytest.mark.parametrize("value", ["-0.1", "1.01", "2", "nan", "inf", "-inf", "steep"])
def test_cli_flag_rejects_out_of_range(value):
    parser = argparse.ArgumentParser()
    server._add_range_ball_drag_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--range-ball-drag-slope", value])


def _run_main(monkeypatch, tmp_path, *flags):
    for name in (
        "air_density_kg_m3",
        "typical_spin_scale",
        "typical_spin_table",
        "range_ball_drag_slope",
        "ballistics_enabled",
        "battery_provider",
        "profile_store",
        "ball_speed_correction_enabled",
        "ball_speed_correction_distance_ft",
        "ball_speed_correction_ball_above_radar_ft",
        "calculated_spin_enabled",
        "_VERTICAL_RADAR_GATE_BYPASS",
        "demo_session",
    ):
        monkeypatch.setattr(server, name, getattr(server, name))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--mock",
            *flags,
            "--profiles-path",
            str(tmp_path / "profiles.json"),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    server.main()


def test_main_applies_the_slope_and_prints_it(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, "--range-ball-drag-slope", "0.3546")

    assert server.range_ball_drag_slope == 0.3546
    assert server._session_start_config()["range_ball_drag_slope"] == 0.3546
    assert "range-ball drag slope 0.3546 (UNVALIDATED)" in capsys.readouterr().out


def test_main_prints_the_drag_off_by_default(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path)

    assert server.range_ball_drag_slope == 0.0
    assert "range-ball drag off" in capsys.readouterr().out


def test_main_warns_that_the_slope_is_ignored_without_ballistics(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, "--no-ballistics", "--range-ball-drag-slope", "0.2")

    assert "WARNING: range-ball drag slope 0.2 ignored" in capsys.readouterr().out
