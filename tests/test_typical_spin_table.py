"""--typical-spin-table picks the club-typical carry spin: Tour averages or range-ball spin.

The default "tour" keeps CLUB_TYPICAL_SPIN_RPM exactly; "range-ball" is this user's
Toptracer range-ball spin (analysis/flugmodell-20261103/spin). Measured spin, the
simulator export spin and CLUB_PHYSICS are never touched.
"""

import argparse
import json
import sys
from datetime import datetime

import pytest

from openflight import server
from openflight.ballistics import (
    CLUB_TYPICAL_SPIN_RPM,
    DEFAULT_TYPICAL_SPIN_TABLE,
    RANGE_BALL_SPIN_RPM,
    TYPICAL_SPIN_SCALED_CLUBS,
    TYPICAL_SPIN_TABLES,
    club_typical_spin_rpm,
    resolve_launch,
    simulate,
)
from openflight.clubs import ClubType
from openflight.clubs.physics import CLUB_PHYSICS
from openflight.launch_monitor import SPIN_CONFIDENCE_HIGH, Shot
from openflight.sim.resolver import SPIN_MODEL_RPM, resolve_shot
from openflight.sim.types import PlayerState

pytestmark = pytest.mark.usefixtures("isolate_finalization")

# The values of analysis/flugmodell-20261103/spin/brief.md, section 1.
BRIEF_RANGE_BALL_SPIN_RPM = {
    ClubType.DRIVER: 2250,
    ClubType.WOOD_3: 2660,
    ClubType.WOOD_5: 3330,
    ClubType.WOOD_7: 3960,
    ClubType.HYBRID_3: 3540,
    ClubType.HYBRID_5: 4270,
    ClubType.HYBRID_7: 5350,
    ClubType.HYBRID_9: 5720,
    ClubType.IRON_2: 3170,
    ClubType.IRON_3: 3710,
    ClubType.IRON_4: 4820,
    ClubType.IRON_5: 5950,
    ClubType.IRON_6: 4190,
    ClubType.IRON_7: 6480,
    ClubType.IRON_8: 4260,
    ClubType.IRON_9: 3500,
    ClubType.PW: 2310,
    ClubType.GW: 2580,
    ClubType.SW: 2850,
    ClubType.LW: 3000,
    ClubType.UNKNOWN: 5000,
}


def _typical_shot(club=ClubType.IRON_7):
    # 0.59 is the OPS cap: never trusted, so carry runs on the club-typical spin.
    return Shot(
        ball_speed_mph=103.4,
        timestamp=datetime.now(),
        club=club,
        launch_angle_vertical=22.0,
        launch_angle_confidence=0.95,
        spin_rpm=4669.0,
        spin_confidence=0.59,
    )


def _measured_shot(club=ClubType.IRON_7):
    shot = _typical_shot(club)
    shot.spin_confidence = SPIN_CONFIDENCE_HIGH
    shot.spin_source = "measured"
    return shot


def _finalized(monkeypatch, shot, table, scale=1.0):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "typical_spin_scale", scale)
    monkeypatch.setattr(server, "typical_spin_table", table)
    server._finalize_shot_detected(shot, emit_event="shot")
    return shot


# ---------------------------------------------------------------- tables


def test_tables_are_tour_and_range_ball_with_tour_the_default():
    assert sorted(TYPICAL_SPIN_TABLES) == ["range-ball", "tour"]
    assert DEFAULT_TYPICAL_SPIN_TABLE == "tour"
    assert TYPICAL_SPIN_TABLES["tour"] is CLUB_TYPICAL_SPIN_RPM
    assert TYPICAL_SPIN_TABLES["range-ball"] is RANGE_BALL_SPIN_RPM


def test_range_ball_table_holds_exactly_the_studied_values():
    assert RANGE_BALL_SPIN_RPM == BRIEF_RANGE_BALL_SPIN_RPM
    # Whole rpm ints, like the tour table: the session log writes 2250, not 2250.0.
    assert all(type(value) is int for value in RANGE_BALL_SPIN_RPM.values())


def test_range_ball_table_has_every_club():
    assert set(RANGE_BALL_SPIN_RPM) == set(ClubType)


@pytest.mark.parametrize("table", sorted(TYPICAL_SPIN_TABLES))
def test_every_table_value_is_a_plausible_spin(table):
    assert all(1000 <= value <= 12000 for value in TYPICAL_SPIN_TABLES[table].values())


def test_tour_table_and_club_physics_are_unchanged():
    """CLUB_PHYSICS stays the TrackMan canonical default (and validation baseline)."""
    assert CLUB_TYPICAL_SPIN_RPM == {
        club: physics.typical_spin_rpm for club, physics in CLUB_PHYSICS.items()
    }
    assert CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_8] == 7500
    assert CLUB_TYPICAL_SPIN_RPM[ClubType.DRIVER] == 2700


# ---------------------------------------------------------------- resolve_launch


@pytest.mark.parametrize("club", list(ClubType))
def test_default_tour_resolves_every_club_to_the_tour_value_exactly(club):
    shot = _typical_shot(club)
    typical = CLUB_TYPICAL_SPIN_RPM[club]

    for conditions in (resolve_launch(shot), resolve_launch(shot, typical_spin_table="tour")):
        assert conditions.spin_rpm == typical
        assert type(conditions.spin_rpm) is type(typical)
        assert conditions.spin_source == "club_typical"


@pytest.mark.parametrize("club", list(ClubType))
def test_range_ball_resolves_every_club_to_its_value(club):
    conditions = resolve_launch(_typical_shot(club), typical_spin_table="range-ball")

    assert conditions.spin_rpm == RANGE_BALL_SPIN_RPM[club]
    assert type(conditions.spin_rpm) is int
    assert conditions.spin_source == "club_typical"


def test_range_ball_unknown_club_falls_back_to_its_unknown_value():
    shot = _typical_shot()
    shot.club = None

    assert resolve_launch(shot, typical_spin_table="range-ball").spin_rpm == (
        RANGE_BALL_SPIN_RPM[ClubType.UNKNOWN]
    )
    assert club_typical_spin_rpm(None, typical_spin_table="range-ball") == 5000


def test_unknown_table_name_raises():
    with pytest.raises(KeyError):
        resolve_launch(_typical_shot(), typical_spin_table="pro-v1")


@pytest.mark.parametrize("table", sorted(TYPICAL_SPIN_TABLES))
def test_trusted_measured_spin_ignores_the_table(table):
    conditions = resolve_launch(_measured_shot(), typical_spin_table=table)

    assert conditions.spin_source == "measured"
    assert conditions.spin_rpm == 4669.0


@pytest.mark.parametrize("club", list(ClubType))
def test_scale_applies_to_irons_and_wedges_of_the_range_ball_table(club):
    spin = resolve_launch(
        _typical_shot(club), typical_spin_scale=0.8, typical_spin_table="range-ball"
    ).spin_rpm

    if club in TYPICAL_SPIN_SCALED_CLUBS:
        assert spin == round(RANGE_BALL_SPIN_RPM[club] * 0.8)
        assert isinstance(spin, int)
    else:
        assert spin == RANGE_BALL_SPIN_RPM[club]
        assert type(spin) is int


# ---------------------------------------------------------------- server


def test_server_default_table_is_tour():
    assert server.typical_spin_table == "tour"


def test_live_carry_flies_the_selected_table(monkeypatch):
    tour = _finalized(monkeypatch, _typical_shot(ClubType.IRON_8), "tour")
    range_ball = _finalized(monkeypatch, _typical_shot(ClubType.IRON_8), "range-ball")

    assert tour.carry_spin_rpm == 7500
    assert range_ball.carry_spin_rpm == 4260
    assert range_ball.carry_spin_source == "club_typical"
    assert json.dumps(range_ball.to_dict()["carry_spin_rpm"]) == "4260"
    expected = simulate(
        resolve_launch(_typical_shot(ClubType.IRON_8), typical_spin_table="range-ball"),
        air_density=server.air_density_kg_m3,
    ).carry_yards
    assert range_ball.carry_spin_adjusted == expected
    # Less spin, a longer 8-iron: the reason for the table.
    assert range_ball.carry_spin_adjusted > tour.carry_spin_adjusted + 5.0
    # The radar reading itself stays as it was measured.
    assert range_ball.spin_rpm == 4669.0


def test_live_default_carry_is_unchanged(monkeypatch):
    shot = _finalized(monkeypatch, _typical_shot(ClubType.IRON_8), "tour")

    assert shot.carry_spin_adjusted == simulate(
        resolve_launch(_typical_shot(ClubType.IRON_8)), air_density=server.air_density_kg_m3
    ).carry_yards
    assert json.dumps(shot.to_dict()["carry_spin_rpm"]) == "7500"


def test_live_measured_spin_ignores_the_table(monkeypatch):
    tour = _finalized(monkeypatch, _measured_shot(), "tour")
    range_ball = _finalized(monkeypatch, _measured_shot(), "range-ball")

    assert range_ball.carry_spin_source == "measured"
    assert range_ball.carry_spin_rpm == tour.carry_spin_rpm == 4669.0
    assert range_ball.carry_spin_adjusted == tour.carry_spin_adjusted


def test_standard_air_carry_flies_the_selected_table(monkeypatch):
    """The fixed-density export carry uses the same spin as the live carry."""
    monkeypatch.setattr(server, "typical_spin_table", "range-ball")
    shot = _typical_shot(ClubType.PW)
    shot.carry_model = "ballistic"
    shot.carry_standard_air_yards = None

    server._ensure_standard_air_carry(shot)

    assert shot.carry_standard_air_yards == simulate(
        resolve_launch(_typical_shot(ClubType.PW), typical_spin_table="range-ball"),
        air_density=server.air_density_kg_m3,
    ).carry_yards


def test_simulator_export_spin_keeps_its_own_model(monkeypatch):
    shot = _finalized(monkeypatch, _typical_shot(), "range-ball")

    resolved = resolve_shot(shot, PlayerState())

    assert resolved.total_spin_rpm == SPIN_MODEL_RPM[ClubType.IRON_7]
    assert resolved.provenance["total_spin"] == "estimated"


def test_session_start_config_records_the_table(monkeypatch):
    assert server._session_start_config()["typical_spin_table"] == "tour"
    monkeypatch.setattr(server, "typical_spin_table", "range-ball")
    assert server._session_start_config()["typical_spin_table"] == "range-ball"


def test_session_start_config_records_no_table_without_ballistics(monkeypatch):
    monkeypatch.setattr(server, "typical_spin_table", "range-ball")
    monkeypatch.setattr(server, "ballistics_enabled", False)

    assert server._session_start_config()["typical_spin_table"] is None


def test_cli_flag_defaults_to_tour_and_accepts_range_ball():
    parser = argparse.ArgumentParser()
    server._add_typical_spin_scale_arguments(parser)

    assert parser.parse_args([]).typical_spin_table == "tour"
    assert parser.parse_args(["--typical-spin-table", "tour"]).typical_spin_table == "tour"
    assert (
        parser.parse_args(["--typical-spin-table", "range-ball"]).typical_spin_table
        == "range-ball"
    )


def test_cli_flag_help_names_the_per_profile_ball_type_override():
    parser = argparse.ArgumentParser()
    server._add_typical_spin_scale_arguments(parser)
    action = next(a for a in parser._actions if "--typical-spin-table" in a.option_strings)

    assert "device default" in action.help
    assert "profile's ball type set in the app" in action.help
    assert "overrides it for that profile's shots" in action.help


@pytest.mark.parametrize("value", ["range", "Range-Ball", "trackman", "", "range_ball"])
def test_cli_flag_rejects_other_tables(value):
    parser = argparse.ArgumentParser()
    server._add_typical_spin_scale_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--typical-spin-table", value])


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


def test_main_applies_the_table_and_prints_it(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, "--typical-spin-table", "range-ball")

    assert server.typical_spin_table == "range-ball"
    assert server._session_start_config()["typical_spin_table"] == "range-ball"
    assert "typical spin table range-ball" in capsys.readouterr().out


def test_main_prints_the_default_tour_table(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path)

    assert server.typical_spin_table == "tour"
    assert "typical spin table tour" in capsys.readouterr().out


def test_main_warns_that_the_table_is_ignored_without_ballistics(monkeypatch, tmp_path, capsys):
    _run_main(monkeypatch, tmp_path, "--no-ballistics", "--typical-spin-table", "range-ball")

    out = capsys.readouterr().out
    assert "Ballistic carry model: DISABLED" in out
    assert "WARNING: typical spin table range-ball ignored" in out
