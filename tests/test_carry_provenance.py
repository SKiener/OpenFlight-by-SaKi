"""Keep the actual carry inputs separate from the displayed spin candidate."""

from datetime import datetime

import pytest

from openflight import server
from openflight.ballistics import TRACKMAN_FLAT_AIR_DENSITY
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot

pytestmark = pytest.mark.usefixtures("isolate_finalization")


@pytest.mark.parametrize(
    "ballistics,confidence,source,expected_source",
    [
        (True, 0.59, None, "club_typical"),
        (True, 0.7, "measured", "measured"),
        (True, 0.9, "calculated", "calculated"),
        (False, 0.59, None, "speed_model"),
        (False, 0.6, "measured", "measured"),
        (False, 0.9, "calculated", "calculated"),
    ],
)
def test_finalization_records_exact_carry_input(
    monkeypatch, ballistics, confidence, source, expected_source
):
    monkeypatch.setattr(server, "ballistics_enabled", ballistics)
    shot = Shot(
        ball_speed_mph=103.4, timestamp=datetime.now(), club=ClubType.IRON_8,
        launch_angle_vertical=26.2, launch_angle_confidence=0.95,
        spin_rpm=4669.0, spin_confidence=confidence, spin_source=source,
    )
    conditions = server.resolve_launch(shot)
    expected_rpm = conditions.spin_rpm if ballistics else (
        shot.spin_rpm if confidence >= 0.6
        else server.get_optimal_spin_for_ball_speed(shot.ball_speed_mph, shot.club)
    )
    expected_carry = (
        server.simulate(conditions, air_density=server.air_density_kg_m3).carry_yards
        if ballistics
        else server.estimate_carry_with_spin(shot.ball_speed_mph, expected_rpm, shot.club)
    )

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_spin_rpm == pytest.approx(expected_rpm)
    assert shot.carry_spin_source == expected_source
    assert shot.carry_model == ("ballistic" if ballistics else "table")
    assert shot.carry_spin_adjusted == pytest.approx(expected_carry)
    assert shot.spin_rpm == 4669.0
    assert shot.to_dict()["carry_spin_source"] == expected_source
    assert server.shot_to_dict(shot)["carry_spin_rpm"] == round(expected_rpm)


def test_unfinalized_shot_has_no_invented_carry_provenance():
    shot = Shot(ball_speed_mph=100, timestamp=datetime.now())
    data = server.shot_to_dict(shot)
    assert data["carry_spin_rpm"] is None
    assert data["carry_spin_source"] is None
    assert data["carry_model"] is None
    assert data["carry_lateral_yards"] is None
    assert data["apex_yards"] is None


def test_live_ballistic_carry_uses_validated_air_density():
    shot = Shot(
        ball_speed_mph=150.0,
        timestamp=datetime.now(),
        club=ClubType.DRIVER,
        launch_angle_vertical=12.0,
        launch_angle_confidence=0.95,
    )
    conditions = server.resolve_launch(shot)

    server._finalize_shot_detected(shot, emit_event="shot")

    expected = server.simulate(conditions, air_density=TRACKMAN_FLAT_AIR_DENSITY)
    assert shot.carry_spin_adjusted == pytest.approx(expected.carry_yards)
    assert server.air_density_kg_m3 == TRACKMAN_FLAT_AIR_DENSITY


def test_live_ballistic_carry_uses_configured_air_density(monkeypatch):
    monkeypatch.setattr(server, "air_density_kg_m3", 1.0)
    shot = Shot(
        ball_speed_mph=150.0,
        timestamp=datetime.now(),
        club=ClubType.DRIVER,
        launch_angle_vertical=12.0,
        launch_angle_confidence=0.95,
    )
    conditions = server.resolve_launch(shot)

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_spin_adjusted == pytest.approx(
        server.simulate(conditions, air_density=1.0).carry_yards
    )


def _iron_shot(launch_angle_horizontal):
    return Shot(
        ball_speed_mph=103.4, timestamp=datetime.now(), club=ClubType.IRON_8,
        launch_angle_vertical=26.2, launch_angle_confidence=0.95,
        launch_angle_horizontal=launch_angle_horizontal,
        launch_angle_horizontal_confidence=0.8, launch_angle_horizontal_source="radar",
        spin_rpm=4669.0, spin_confidence=0.9, spin_source="measured",
    )


@pytest.mark.parametrize("horizontal", [-4.0, 0.0, 3.5])
def test_ballistic_carry_records_the_simulated_landing_offset(monkeypatch, horizontal):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(horizontal)
    expected = server.simulate(
        server.resolve_launch(shot), air_density=server.air_density_kg_m3
    ).lateral_yards

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_lateral_yards == pytest.approx(expected)
    assert (shot.carry_lateral_yards > 1, shot.carry_lateral_yards < -1) == (
        horizontal > 0, horizontal < 0,
    )
    assert shot.to_dict()["carry_lateral_yards"] == pytest.approx(expected)
    assert server.shot_to_dict(shot)["carry_lateral_yards"] == round(expected)


def test_table_carry_has_no_landing_offset(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _iron_shot(-4.0)

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_model == "table"
    assert shot.carry_lateral_yards is None


def test_unknown_direction_does_not_become_a_landing_offset(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "camera_capture_config", {"enabled": True})
    shot = _iron_shot(None)

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_model == "ballistic"
    assert shot.launch_angle_horizontal is None
    assert shot.carry_lateral_yards is None
    assert server.shot_to_dict(shot)["carry_lateral_yards"] is None


@pytest.mark.parametrize("horizontal", [None, 2.0])
def test_ballistic_carry_records_the_apex_of_the_same_flight(monkeypatch, horizontal):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(horizontal)
    trajectory = server.simulate(server.resolve_launch(shot), air_density=server.air_density_kg_m3)

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_spin_adjusted == pytest.approx(trajectory.carry_yards)
    assert shot.apex_yards == pytest.approx(trajectory.apex_yards)
    # The apex is the top of the drawn flight.
    assert shot.apex_yards == pytest.approx(max(p[3] for p in shot.flight_path["points"]), abs=0.3)
    assert 15 < shot.apex_yards < 45  # an 8 iron peaks around 30 yd
    assert shot.to_dict()["apex_yards"] == pytest.approx(trajectory.apex_yards)
    assert server.shot_to_dict(shot)["apex_yards"] == round(trajectory.apex_yards, 1)


def test_table_carry_has_no_apex(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _iron_shot(-4.0)
    shot.apex_yards = 31.0  # left from an earlier flight

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_model == "table"
    assert shot.apex_yards is None
    assert server.shot_to_dict(shot)["apex_yards"] is None
