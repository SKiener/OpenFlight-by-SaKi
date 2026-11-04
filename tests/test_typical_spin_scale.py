"""--typical-spin-scale turns the club-typical carry spin of irons and wedges down (or up).

Nothing else: other clubs, measured spin and the simulator export keep their spin.
"""

import argparse
import json
import sys
from datetime import datetime

import pytest

from openflight import server
from openflight.ballistics import (
    CLUB_TYPICAL_SPIN_RPM,
    TYPICAL_SPIN_SCALED_CLUBS,
    resolve_launch,
    simulate,
)
from openflight.clubs import ClubType
from openflight.clubs.bag import CLUB_VARIANTS
from openflight.launch_monitor import Shot
from openflight.sim.resolver import SPIN_MODEL_RPM, resolve_shot
from openflight.sim.types import PlayerState

pytestmark = pytest.mark.usefixtures("isolate_finalization")


def _typical_shot():
    # 0.59 is the OPS cap: never trusted, so carry runs on the club-typical spin.
    return Shot(
        ball_speed_mph=103.4,
        timestamp=datetime.now(),
        club=ClubType.IRON_7,
        launch_angle_vertical=22.0,
        launch_angle_confidence=0.95,
        spin_rpm=4669.0,
        spin_confidence=0.59,
    )


def _measured_shot():
    return Shot(
        ball_speed_mph=103.4,
        timestamp=datetime.now(),
        club=ClubType.IRON_7,
        launch_angle_vertical=22.0,
        launch_angle_confidence=0.95,
        spin_rpm=4669.0,
        spin_confidence=0.9,
        spin_source="measured",
    )


def _finalized(monkeypatch, shot, scale):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "typical_spin_scale", scale)
    server._finalize_shot_detected(shot, emit_event="shot")
    return shot


def test_default_scale_is_one_and_leaves_typical_spin_exact(monkeypatch):
    assert server.typical_spin_scale == 1.0
    shot = _finalized(monkeypatch, _typical_shot(), 1.0)
    typical = CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_7]
    unscaled = resolve_launch(_typical_shot())

    assert unscaled.spin_rpm == typical
    assert shot.carry_spin_source == "club_typical"
    assert shot.carry_spin_rpm == typical
    # Byte-for-byte: the session log must keep writing the table int (6500, not 6500.0).
    assert type(shot.carry_spin_rpm) is type(typical)
    assert json.dumps(shot.to_dict()["carry_spin_rpm"]) == json.dumps(typical)
    assert (
        shot.carry_spin_adjusted
        == simulate(unscaled, air_density=server.air_density_kg_m3).carry_yards
    )


def test_scale_lowers_typical_spin_and_changes_carry(monkeypatch):
    base = _finalized(monkeypatch, _typical_shot(), 1.0)
    scaled = _finalized(monkeypatch, _typical_shot(), 0.8)
    typical = CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_7]

    assert scaled.carry_spin_source == "club_typical"
    assert scaled.carry_spin_rpm == round(0.8 * typical)
    assert isinstance(scaled.carry_spin_rpm, int)
    assert server.shot_to_dict(scaled)["carry_spin_rpm"] == round(0.8 * typical)
    expected = simulate(
        resolve_launch(_typical_shot(), typical_spin_scale=0.8),
        air_density=server.air_density_kg_m3,
    ).carry_yards
    assert scaled.carry_spin_adjusted == pytest.approx(expected)
    assert scaled.carry_spin_adjusted != pytest.approx(base.carry_spin_adjusted, abs=0.5)
    # The radar reading itself stays as it was measured.
    assert scaled.spin_rpm == 4669.0


def test_scale_never_touches_trusted_measured_spin(monkeypatch):
    base = _finalized(monkeypatch, _measured_shot(), 1.0)
    scaled = _finalized(monkeypatch, _measured_shot(), 0.8)

    assert scaled.carry_spin_source == "measured"
    assert scaled.carry_spin_rpm == base.carry_spin_rpm == 4669.0
    assert scaled.carry_spin_adjusted == base.carry_spin_adjusted


def test_simulator_export_spin_is_not_scaled(monkeypatch):
    shot = _finalized(monkeypatch, _typical_shot(), 0.8)

    resolved = resolve_shot(shot, PlayerState())

    assert resolved.total_spin_rpm == SPIN_MODEL_RPM[ClubType.IRON_7]
    assert resolved.provenance["total_spin"] == "estimated"


def test_session_start_config_logs_active_scale(monkeypatch):
    assert server._session_start_config()["typical_spin_scale"] == 1.0
    monkeypatch.setattr(server, "typical_spin_scale", 0.8)
    config = server._session_start_config()
    assert (config["typical_spin_scale"], config["typical_spin_scale_clubs"]) == (
        0.8,
        "irons_wedges",
    )


@pytest.mark.parametrize("club", [ClubType.IRON_7, ClubType.IRON_3, ClubType.PW, ClubType.LW])
def test_scale_applies_to_irons_and_wedges(club):
    """Irons and wedges get the scaled club-typical spin, rounded to whole rpm."""
    shot = _typical_shot()
    shot.club = club

    spin = resolve_launch(shot, typical_spin_scale=0.85).spin_rpm

    assert spin == round(CLUB_TYPICAL_SPIN_RPM[club] * 0.85)
    assert spin != CLUB_TYPICAL_SPIN_RPM[club]


@pytest.mark.parametrize(
    "club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.WOOD_7, ClubType.HYBRID_5, ClubType.UNKNOWN]
)
def test_scale_leaves_driver_woods_hybrids_and_unknown_unchanged(monkeypatch, club):
    """Byte-for-byte: the table int, as at the default scale."""
    shot = _typical_shot()
    shot.club = club
    typical = CLUB_TYPICAL_SPIN_RPM[club]

    spin = resolve_launch(shot, typical_spin_scale=0.85).spin_rpm
    assert spin == typical and type(spin) is type(typical)

    finalized = _finalized(monkeypatch, shot, 0.85)
    assert finalized.carry_spin_source == "club_typical"
    assert json.dumps(finalized.to_dict()["carry_spin_rpm"]) == json.dumps(typical)


def test_wedges_from_the_bag_are_scaled():
    """The bag's wedge-* ids map to PW/GW/SW/LW, all scaled."""
    wedges = [club for key, club in CLUB_VARIANTS.items() if key.startswith("wedge-")]
    assert wedges and set(wedges) <= TYPICAL_SPIN_SCALED_CLUBS


@pytest.mark.parametrize("club,scale", [(ClubType.IRON_5, 0.7), (ClubType.SW, 1.1)])
def test_scaled_typical_spin_is_whole_rpm(club, scale):
    shot = _typical_shot()
    shot.club = club

    spin = resolve_launch(shot, typical_spin_scale=scale).spin_rpm

    assert isinstance(spin, int)
    assert spin == round(CLUB_TYPICAL_SPIN_RPM[club] * scale)


def test_session_start_config_logs_no_scale_without_ballistics(monkeypatch):
    monkeypatch.setattr(server, "typical_spin_scale", 0.8)
    monkeypatch.setattr(server, "ballistics_enabled", False)

    assert server._session_start_config()["typical_spin_scale"] is None


def test_cli_flag_defaults_and_bounds():
    parser = argparse.ArgumentParser()
    server._add_typical_spin_scale_arguments(parser)

    assert parser.parse_args([]).typical_spin_scale == 1.0
    assert parser.parse_args(["--typical-spin-scale", "0.8"]).typical_spin_scale == 0.8
    assert parser.parse_args(["--typical-spin-scale", "0.5"]).typical_spin_scale == 0.5
    assert parser.parse_args(["--typical-spin-scale", "1.2"]).typical_spin_scale == 1.2


@pytest.mark.parametrize("value", ["0.49", "1.21", "0", "-1", "nan", "inf", "low"])
def test_cli_flag_rejects_out_of_range(value):
    parser = argparse.ArgumentParser()
    server._add_typical_spin_scale_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--typical-spin-scale", value])


def test_main_applies_cli_scale(monkeypatch, tmp_path):
    for name in (
        "air_density_kg_m3",
        "typical_spin_scale",
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
            "--typical-spin-scale",
            "0.8",
            "--profiles-path",
            str(tmp_path / "profiles.json"),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)

    server.main()

    assert server.typical_spin_scale == 0.8
    assert server._session_start_config()["typical_spin_scale"] == 0.8
