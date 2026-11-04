"""A shot's metrics can be finalized without logging, emitting or forwarding it."""

import math
from random import Random

import pytest

from openflight import server
from openflight.clubs import ClubType
from openflight.shot_generator import generate_shot


@pytest.fixture(name="touched")
def fixture_touched(monkeypatch):
    touched = []
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "ball_speed_correction_enabled", False)
    monkeypatch.setattr(server, "calculated_spin_enabled", False)
    monkeypatch.setattr(server, "get_session_logger", lambda: touched.append("session logger"))
    monkeypatch.setattr(server.socketio, "emit", lambda *args, **kwargs: touched.append("emit"))
    monkeypatch.setattr(
        server, "_forward_shot_to_simulators", lambda shot: touched.append("simulators")
    )
    return touched


def _assert_flies_to_its_carry(shot):
    assert shot.carry_spin_adjusted is not None
    assert shot.carry_spin_adjusted > 0
    assert shot.flight_path is not None
    x, y = shot.flight_path["points"][-1][1:3]
    assert math.hypot(x, y) == pytest.approx(shot.carry_spin_adjusted, abs=0.02)


def test_shot_metrics_have_no_side_effects(touched):
    shot = generate_shot(ClubType.IRON_7, Random(7), mode="demo")

    server._finalize_shot_metrics(shot)

    _assert_flies_to_its_carry(shot)
    assert touched == []


@pytest.mark.parametrize(
    "club", [club for club in ClubType if club is not ClubType.UNKNOWN], ids=lambda c: c.value
)
def test_every_club_gets_a_carry_and_a_flight_path(touched, club):
    shot = generate_shot(club, Random(2026), mode="demo")

    server._finalize_shot_metrics(shot)

    _assert_flies_to_its_carry(shot)
    assert touched == []
