"""Carry span over 50-100 % of the club-typical spin, flown after the shot event."""

from dataclasses import replace
from datetime import datetime

import pytest

from openflight import ballistics, server
from openflight.ballistics import (
    CARRY_SPAN_SPIN_FRACTIONS,
    LaunchConditions,
    carry_span_yards,
    simulate,
)
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.weather import AirDensityChoice

pytestmark = pytest.mark.usefixtures("isolate_finalization")


def _typical(club=ClubType.IRON_7, ball_speed=115.0, launch=17.0, axis=0.0):
    shot = Shot(
        ball_speed_mph=ball_speed,
        timestamp=datetime.now(),
        club=club,
        launch_angle_vertical=launch,
        launch_angle_confidence=0.95,
        spin_rpm=4000.0,
        spin_confidence=0.59,
        spin_axis_deg=axis,
    )
    return shot


def _conditions(spin=7000.0, axis=0.0, source="club_typical", ball_speed=115.0, launch=17.0):
    return LaunchConditions(ball_speed, launch, 0.0, spin, axis, source)


def _carry(conditions, spin=None, axis=None):
    changed = replace(
        conditions,
        spin_rpm=conditions.spin_rpm if spin is None else spin,
        spin_axis_deg=conditions.spin_axis_deg if axis is None else axis,
    )
    return simulate(changed).carry_yards


# ---------------------------------------------------------------- ballistics


def test_span_fractions_are_the_decided_five():
    assert CARRY_SPAN_SPIN_FRACTIONS == (0.5, 0.625, 0.75, 0.875, 1.0)


def test_span_is_min_and_max_over_every_fraction():
    conditions = _conditions()
    carries = [_carry(conditions, spin=7000.0 * f, axis=0.0) for f in CARRY_SPAN_SPIN_FRACTIONS]

    span = carry_span_yards(conditions)

    assert span == pytest.approx((min(carries), max(carries)))


def test_measured_and_unknown_spin_get_no_span():
    assert carry_span_yards(_conditions(source="measured")) is None


def test_wedge_span_takes_the_inner_minimum_when_carry_is_not_monotonic():
    # A slow, high wedge: less spin first flies shorter, then longer (no lift vs. drag).
    conditions = _conditions(spin=10000.0, ball_speed=65.0, launch=32.0)
    carries = [_carry(conditions, spin=10000.0 * f, axis=0.0) for f in CARRY_SPAN_SPIN_FRACTIONS]

    span = carry_span_yards(conditions)

    assert span == pytest.approx((min(carries), max(carries)))
    assert span[0] <= carries[0] and span[0] <= carries[-1]
    assert span[1] >= carries[0] and span[1] >= carries[-1]


def test_full_spin_carry_stands_in_for_the_100_percent_flight(monkeypatch):
    calls = []
    original = ballistics.simulate
    monkeypatch.setattr(
        ballistics, "simulate", lambda c, **kw: calls.append(c.spin_rpm) or original(c, **kw)
    )
    conditions = _conditions()

    span = carry_span_yards(conditions, full_spin_carry_yards=999.0)

    assert calls == pytest.approx([3500.0, 4375.0, 5250.0, 6125.0])
    assert span[1] == 999.0


def test_a_curved_carry_is_flown_again_with_axis_zero(monkeypatch):
    seen = []
    original = ballistics.simulate
    monkeypatch.setattr(
        ballistics,
        "simulate",
        lambda c, **kw: seen.append((c.spin_rpm, c.spin_axis_deg)) or original(c, **kw),
    )
    conditions = _conditions(axis=12.0)

    span = carry_span_yards(conditions, full_spin_carry_yards=999.0)

    assert [axis for _, axis in seen] == [0.0] * 5
    assert seen[-1][0] == pytest.approx(7000.0)
    # The curved carry itself still bounds the span: min <= carry <= max.
    assert span[1] == 999.0


def test_a_curved_carry_below_every_level_flight_is_the_span_minimum():
    conditions = _conditions(axis=12.0)

    span = carry_span_yards(conditions, full_spin_carry_yards=1.0)

    assert span[0] == 1.0


def test_span_uses_the_given_air_density():
    conditions = _conditions()
    assert carry_span_yards(conditions, air_density=1.0) != carry_span_yards(conditions)
    thin = [
        simulate(replace(conditions, spin_rpm=7000.0 * f), air_density=1.0).carry_yards
        for f in CARRY_SPAN_SPIN_FRACTIONS
    ]
    assert carry_span_yards(conditions, air_density=1.0) == pytest.approx((min(thin), max(thin)))


@pytest.mark.parametrize("axis", [0.0, 5.0, -5.0, 12.0, -12.0, 25.0, -25.0])
@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.IRON_7, ClubType.PW, ClubType.SW])
def test_the_typical_carry_lies_inside_its_span(club, axis):
    shot = _typical(club=club, axis=axis)
    conditions = ballistics.resolve_launch(shot)
    carry = simulate(conditions).carry_yards

    low, high = carry_span_yards(conditions, full_spin_carry_yards=carry)

    assert low <= carry <= high


# ---------------------------------------------------------------- server


def test_live_typical_shot_gets_its_span_after_the_shot_event(monkeypatch, _hold_carry_span_jobs):
    events = []
    monkeypatch.setattr(
        server.socketio, "emit", lambda event, payload: events.append((event, payload))
    )
    shot = _typical()

    server._finalize_shot_detected(shot, emit_event="shot")

    assert [event for event, _ in events] == ["shot"]
    assert events[0][1]["shot"]["carry_span_min_yards"] is None
    assert shot.carry_span_min_yards is None
    assert len(_hold_carry_span_jobs) == 1

    _hold_carry_span_jobs[0]()

    conditions = ballistics.resolve_launch(shot)
    expected = carry_span_yards(
        conditions,
        air_density=server.air_density_kg_m3,
        full_spin_carry_yards=shot.carry_spin_adjusted,
    )
    assert (shot.carry_span_min_yards, shot.carry_span_max_yards) == pytest.approx(expected)
    assert [event for event, _ in events] == ["shot", "shot_update"]
    payload = events[1][1]
    assert payload["shot"]["timestamp"] == shot.timestamp.isoformat()
    assert payload["shot"]["carry_span_min_yards"] == round(expected[0])
    assert payload["shot"]["carry_span_max_yards"] == round(expected[1])
    assert "pending" not in payload


def test_span_never_changes_the_carry(_hold_carry_span_jobs):
    shot = _typical()
    server._finalize_shot_detected(shot, emit_event="shot")
    carry = shot.carry_spin_adjusted

    _hold_carry_span_jobs[0]()

    assert shot.carry_spin_adjusted == carry
    assert shot.carry_spin_source == "club_typical"


# The weather the phone reports: thin, warm air, far from the fixed --air-density.
WEATHER_DENSITY = 1.05


def _fresh_weather(monkeypatch):
    """Let every shot fly with WEATHER_DENSITY, as with a fresh phone reading."""
    monkeypatch.setattr(
        server,
        "_air_density_for_shot",
        lambda: AirDensityChoice(kg_m3=WEATHER_DENSITY, source="manual", weather_age_s=60.0),
    )


@pytest.mark.parametrize("axis", [0.0, 12.0, -25.0])
def test_live_span_flies_with_the_weather_density_of_its_carry(
    monkeypatch, _hold_carry_span_jobs, axis
):
    """The span contains the weather carry it is shown with, so it flies in that air."""
    _fresh_weather(monkeypatch)
    shot = _typical(axis=axis)
    server._finalize_shot_detected(  # pylint: disable=protected-access
        shot, emit_event="shot"
    )
    assert shot.carry_air_density_kg_m3 == WEATHER_DENSITY

    _hold_carry_span_jobs[0]()

    expected = carry_span_yards(
        ballistics.resolve_launch(shot),
        air_density=WEATHER_DENSITY,
        full_spin_carry_yards=shot.carry_spin_adjusted,
    )
    assert (shot.carry_span_min_yards, shot.carry_span_max_yards) == pytest.approx(expected)
    assert shot.carry_span_min_yards <= shot.carry_spin_adjusted <= shot.carry_span_max_yards


def test_demo_span_flies_with_the_weather_density_of_its_carry(monkeypatch):
    """A demo shot's span is flown in the same air as its carry too."""
    _fresh_weather(monkeypatch)
    shot = replace(_typical(), demo=True)

    server._finalize_demo_shot_metrics(shot)  # pylint: disable=protected-access

    expected = carry_span_yards(
        ballistics.resolve_launch(shot),
        air_density=WEATHER_DENSITY,
        full_spin_carry_yards=shot.carry_spin_adjusted,
    )
    assert (shot.carry_span_min_yards, shot.carry_span_max_yards) == pytest.approx(expected)


def test_measured_spin_gets_no_span(_hold_carry_span_jobs):
    shot = replace(_typical(), spin_confidence=0.9, spin_source="measured")

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_spin_source == "measured"
    assert _hold_carry_span_jobs == []
    assert shot.carry_span_min_yards is None and shot.carry_span_max_yards is None


def test_table_carry_gets_no_span(monkeypatch, _hold_carry_span_jobs):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _typical()

    server._finalize_shot_detected(shot, emit_event="shot")

    assert shot.carry_model == "table"
    assert _hold_carry_span_jobs == []


def test_a_carry_changed_meanwhile_keeps_no_span(monkeypatch, _hold_carry_span_jobs):
    events = []
    monkeypatch.setattr(server.socketio, "emit", lambda event, payload: events.append(event))
    shot = _typical()
    server._finalize_shot_detected(shot, emit_event="shot")
    shot.carry_spin_adjusted += 3.0

    _hold_carry_span_jobs[0]()

    assert shot.carry_span_min_yards is None
    assert events == ["shot"]


def test_a_span_failure_costs_only_the_span(monkeypatch, _hold_carry_span_jobs):
    events = []
    monkeypatch.setattr(server.socketio, "emit", lambda event, payload: events.append(event))

    def broken(*_args, **_kwargs):
        raise ZeroDivisionError("float division by zero")

    monkeypatch.setattr(server, "carry_span_yards", broken)
    shot = _typical()
    server._finalize_shot_detected(shot, emit_event="shot")

    _hold_carry_span_jobs[0]()

    assert shot.carry_span_min_yards is None
    assert events == ["shot"]


def test_refinalizing_clears_a_stale_span():
    shot = _typical()
    shot.carry_span_min_yards, shot.carry_span_max_yards = 1.0, 2.0

    server._finalize_shot_metrics(shot)

    assert shot.carry_span_min_yards is None and shot.carry_span_max_yards is None


@pytest.mark.parametrize("axis", [0.0, 5.0, -5.0, 12.0, -12.0, 25.0, -25.0])
def test_demo_shot_gets_its_span_before_it_is_shown(_hold_carry_span_jobs, axis):
    shot = replace(_typical(axis=axis), demo=True)

    server._finalize_demo_shot_metrics(shot)

    assert shot.carry_span_min_yards is not None
    assert shot.carry_span_min_yards <= shot.carry_spin_adjusted <= shot.carry_span_max_yards
    data = server.shot_to_dict(shot)
    assert (
        data["carry_span_min_yards"] <= data["carry_spin_adjusted"] <= data["carry_span_max_yards"]
    )
    assert _hold_carry_span_jobs == []


def test_demo_shot_in_the_live_path_starts_no_span_job(_hold_carry_span_jobs):
    server._finalize_shot_detected(replace(_typical(), demo=True), emit_event="shot")
    assert _hold_carry_span_jobs == []


def test_span_is_a_ui_field_only():
    shot = _typical()
    shot.carry_span_min_yards, shot.carry_span_max_yards = 101.6, 115.4

    assert "carry_span_min_yards" not in shot.to_dict()
    assert "carry_span_max_yards" not in shot.to_dict()
    data = server.shot_to_dict(shot)
    assert (data["carry_span_min_yards"], data["carry_span_max_yards"]) == (102, 115)


def test_span_worker_runs_jobs_in_order(monkeypatch):
    monkeypatch.setattr(server, "_carry_span_executor", None)
    worker = server._carry_span_worker()
    done = []
    try:
        assert server._carry_span_worker() is worker
        worker.submit(lambda: done.append(1))
        worker.submit(lambda: done.append(2))
    finally:
        worker.shutdown(wait=True)
    assert done == [1, 2]
