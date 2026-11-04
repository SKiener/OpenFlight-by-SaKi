"""Every ballistic shot carries its roll and Total."""

import json
import logging
import math
from datetime import datetime
from random import Random
from types import SimpleNamespace

import pytest

from openflight import ballistics, server
from openflight.ballistics import (
    M_TO_YD,
    MPH_TO_MPS,
    LaunchConditions,
    Trajectory,
    TrajectoryPoint,
    flight_path,
    roll_path,
    simulate,
    touchdown_of,
)
from openflight.clubs import ClubType
from openflight.demo import DemoSession
from openflight.launch_monitor import Shot
from openflight.rollout import MAX_IMPACT_SPEED_MPS, MAX_ROLL_POINTS, Rollout, simulate_rollout
from openflight.session_logger import SessionLogger

RPM_TO_RAD_S = 2 * math.pi / 60
ROLL_FIELDS = ("roll_path", "roll_yards", "total_yards")
ROLL_LOG_PREFIXES = ("[SERVER] No roll", "[SERVER] Roll-out failed")


pytestmark = pytest.mark.usefixtures("isolate_finalization")


@pytest.fixture(autouse=True)
def ballistics_on(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)


@pytest.fixture(name="emits")
def fixture_emits(monkeypatch):
    events = []
    monkeypatch.setattr(
        server.socketio, "emit", lambda event, payload: events.append((event, payload))
    )
    return events


def _shot(club=ClubType.IRON_8, ball_speed_mph=103.4, launch_angle_vertical=26.2, spin_rpm=4669.0):
    """A shot with measured launch and spin; the defaults are test_flight_path's 8-iron."""
    return Shot(
        ball_speed_mph=ball_speed_mph,
        timestamp=datetime.now(),
        club=club,
        launch_angle_vertical=launch_angle_vertical,
        launch_angle_confidence=0.95,
        launch_angle_horizontal=3.0,
        launch_angle_horizontal_confidence=0.8,
        launch_angle_horizontal_source="radar",
        spin_rpm=spin_rpm,
        spin_confidence=0.9,
        spin_source="measured",
    )


def _finalized(shot):
    server._finalize_shot_detected(shot, emit_event="shot")
    return shot


def _roll(shot):
    return tuple(getattr(shot, name) for name in ROLL_FIELDS)


def _flight(*points):
    """A trajectory that touches down at its last point; roll_path reads only points and time."""
    return Trajectory(
        points=[TrajectoryPoint(t, x, y, z, speed_mph=0.0, spin_rpm=0.0) for t, x, y, z in points],
        carry_yards=0.0,
        apex_yards=0.0,
        lateral_yards=0.0,
        flight_time_s=points[-1][0],
        landing_speed_mph=0.0,
        landing_angle_deg=0.0,
    )


def _rollout(*points_yd):
    """A Rollout from (t, x, y, z) points given in yards, as the model would return it in metres."""
    points = tuple((t, x / M_TO_YD, y / M_TO_YD, z / M_TO_YD) for t, x, y, z in points_yd)
    return Rollout(points=points, rest_x_m=points[-1][1], rest_y_m=points[-1][2], roll_m=0.0)


def test_simulate_keeps_the_touchdown_state():
    trajectory = simulate(LaunchConditions(120, 16.3, 0.0, 7097, 0.0, "measured"))
    vx, vy, vz = trajectory.touchdown_velocity_mps
    assert vz < 0
    assert vy == 0.0
    assert math.hypot(vx, vy, vz) == pytest.approx(trajectory.landing_speed_mph * MPH_TO_MPS)
    assert math.degrees(math.atan2(-vz, vx)) == pytest.approx(trajectory.landing_angle_deg)
    final_omega = trajectory.points[-1].spin_rpm * RPM_TO_RAD_S
    assert trajectory.touchdown_backspin_rad_s == pytest.approx(final_omega, rel=1e-12)


@pytest.mark.parametrize(
    "aim_deg, spin_axis_deg",
    [(8.0, 0.0), (-6.0, 0.0), (0.0, 20.0), (5.0, -15.0)],
    ids=["aimed-right", "aimed-left", "fade", "aimed-draw"],
)
def test_the_backspin_is_the_spin_about_the_axis_square_to_the_heading(aim_deg, spin_axis_deg):
    trajectory = simulate(LaunchConditions(120, 16.3, aim_deg, 7097, spin_axis_deg, "measured"))
    vx, vy, _ = trajectory.touchdown_velocity_mps
    turned = math.atan2(vy, vx) - math.radians(aim_deg)
    final_omega = trajectory.points[-1].spin_rpm * RPM_TO_RAD_S
    expected = final_omega * math.cos(math.radians(spin_axis_deg)) * math.cos(turned)
    assert trajectory.touchdown_backspin_rad_s == pytest.approx(expected, rel=1e-9)


def test_a_ball_that_only_drops_still_lands():
    trajectory = simulate(LaunchConditions(0.0, 0.0, 0.0, 0.0, 0.0, "measured"))
    assert trajectory.touchdown_velocity_mps[:2] == (0.0, 0.0)
    assert trajectory.touchdown_backspin_rad_s == 0.0


def test_touchdown_of_takes_the_landing_in_metres():
    trajectory = simulate(LaunchConditions(120, 16.3, 4.0, 7097, 10.0, "measured"))
    touchdown = touchdown_of(trajectory)
    landing = trajectory.points[-1]
    assert (touchdown.x_m, touchdown.y_m) == pytest.approx(
        (landing.x / M_TO_YD, landing.y / M_TO_YD)
    )
    assert (touchdown.vx, touchdown.vy, touchdown.vz) == trajectory.touchdown_velocity_mps
    assert touchdown.backspin_rad_s == trajectory.touchdown_backspin_rad_s


def test_a_flight_that_never_landed_has_no_touchdown(monkeypatch):
    monkeypatch.setattr(ballistics, "MAX_FLIGHT_SECONDS", 2.0)
    trajectory = simulate(LaunchConditions(160, 14.0, 0.0, 2800, 0.0, "measured"))
    assert trajectory.touchdown_velocity_mps is None
    assert trajectory.touchdown_backspin_rad_s is None
    assert touchdown_of(trajectory) is None


def test_finalize_attaches_roll_and_total():
    shot = _finalized(_shot())
    trajectory = simulate(server.resolve_launch(shot), air_density=server.air_density_kg_m3)
    assert shot.roll_path == roll_path(trajectory, simulate_rollout(touchdown_of(trajectory)))
    assert shot.roll_path["unit"] == "yd"
    assert math.isfinite(shot.total_yards)
    assert shot.roll_yards == pytest.approx(shot.total_yards - shot.carry_spin_adjusted, abs=1e-9)
    assert shot.roll_yards > 0


def test_a_wedge_that_spins_back_has_a_negative_roll():
    shot = _shot(ClubType.LW, 90.0, 40.0, 12000.0)
    server._finalize_shot_metrics(shot)

    assert shot.roll_yards < 0
    assert shot.total_yards < shot.carry_spin_adjusted
    wire_roll = server.shot_to_dict(shot)["roll_yards"]
    assert isinstance(wire_roll, int)
    assert wire_roll == round(shot.roll_yards) < 0


def test_total_is_measured_at_the_rest_point():
    shot = _finalized(_shot())
    _, x, y, _ = shot.roll_path["points"][-1]
    assert shot.total_yards == pytest.approx(math.hypot(x, y), abs=0.01)


def test_roll_path_starts_where_the_flight_lands():
    shot = _finalized(_shot())
    assert shot.roll_path["points"][0] == shot.flight_path["points"][-1]


@pytest.mark.parametrize(
    "club, ball_speed_mph, launch_deg, spin_rpm",
    [
        pytest.param(ClubType.DRIVER, 167.0, 10.9, 2686.0, id="pga-driver"),
        pytest.param(ClubType.IRON_7, 120.0, 16.3, 7097.0, id="pga-7-iron"),
        pytest.param(ClubType.PW, 102.0, 24.2, 9304.0, id="pga-pw"),
        pytest.param(ClubType.SW, 75.0, 30.0, 11000.0, id="sw-11k-rpm"),
        pytest.param(ClubType.LW, 65.0, 34.0, 12000.0, id="lw-12k-rpm"),
        pytest.param(ClubType.WOOD_3, 150.0, 4.0, 3000.0, id="thin-3-wood-25-points"),
        pytest.param(ClubType.DRIVER, 160.0, 14.0, 2800.0, id="high-driver"),
    ],
)
def test_roll_path_contract(club, ball_speed_mph, launch_deg, spin_rpm):
    shot = _finalized(_shot(club, ball_speed_mph, launch_deg, spin_rpm))
    path = shot.roll_path
    points = path["points"]
    values = [value for point in points for value in point]
    assert path["unit"] == "yd"
    assert 2 <= len(points) <= MAX_ROLL_POINTS
    assert all(len(point) == 4 for point in points)
    assert all(math.isfinite(value) for value in values)
    assert all(b[0] > a[0] for a, b in zip(points, points[1:]))
    assert all(point[3] >= 0.0 for point in points)
    assert points[-1][3] == 0.0
    assert all(value == round(value, 2) for value in values)
    assert [v for v in values if v == 0.0 and math.copysign(1.0, v) < 0] == []
    assert len(json.dumps(path)) <= 1024


def test_a_long_run_fills_every_point():
    shot = _finalized(_shot(ClubType.WOOD_3, 150.0, 4.0, 3000.0))
    assert len(shot.roll_path["points"]) == MAX_ROLL_POINTS


def test_roll_path_starts_exactly_on_the_flight_touchdown():
    """The model's own touchdown, back from metres, would round to 120.01 instead of 120.0."""
    flight = _flight((0.0, 0.0, 0.0, 0.0), (4.1234, 120.004999, -3.0, 0.0))
    rollout = _rollout(
        (0.0, 120.005001, -3.0, 0.0),
        (0.5, 121.0, -3.0, 0.1),
        (1.2, 125.0, -3.1, 0.0),
    )
    points = roll_path(flight, rollout)["points"]
    assert points[0] == flight_path(flight)["points"][-1] == [4.12, 120.0, -3.0, 0.0]
    assert points[1:] == [[4.62, 121.0, -3.0, 0.1], [5.32, 125.0, -3.1, 0.0]]


def test_roll_points_that_round_onto_the_time_before_are_dropped_but_the_rest_point_ends_it():
    """After apex and landing, 5.803 s rounds onto the landing's 5.80 s and is dropped; the
    rest at 5.809 s rounds onto 5.806 s's 5.81 s and replaces that point instead."""
    flight = _flight((0.0, 0.0, 0.0, 0.0), (5.0, 150.0, 0.0, 0.0))
    rollout = _rollout(
        (0.0, 150.0, 0.0, 0.0),
        (0.4, 152.0, 0.0, 0.2),
        (0.8, 154.0, 0.0, 0.0),
        (0.803, 154.01, 0.0, 0.0),
        (0.806, 154.02, 0.0, 0.0),
        (0.809, 154.03, 0.0, 0.0),
    )
    assert roll_path(flight, rollout)["points"] == [
        [5.0, 150.0, 0.0, 0.0],
        [5.4, 152.0, 0.0, 0.2],
        [5.8, 154.0, 0.0, 0.0],
        [5.81, 154.03, 0.0, 0.0],
    ]


@pytest.mark.parametrize(
    "landing, roll",
    [
        pytest.param(
            (5.0, 150.0, 0.0, 0.0),
            [(0.0, 150.0, 0.0, 0.0), (0.004, 150.001, 0.0, 0.0)],
            id="rests-within-the-touchdown-hundredth",
        ),
        pytest.param(
            (5.0, 150.0, 0.0, 0.0),
            [(0.0, 150.0, 0.0, 0.0), (0.4, math.nan, 0.0, 0.2), (0.9, 153.0, 0.0, 0.0)],
            id="nan-in-the-roll",
        ),
        pytest.param(
            (5.0, math.inf, 0.0, 0.0),
            [(0.0, 150.0, 0.0, 0.0), (0.9, 153.0, 0.0, 0.0)],
            id="inf-at-the-touchdown",
        ),
    ],
)
def test_roll_path_is_none_without_two_finite_points(landing, roll):
    flight = _flight((0.0, 0.0, 0.0, 0.0), landing)
    assert roll_path(flight, _rollout(*roll)) is None


def test_table_carry_has_no_roll(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _finalized(_shot())
    assert shot.carry_model == "table"
    assert _roll(shot) == (None, None, None)


def test_refinalizing_as_a_table_carry_drops_the_stale_roll(monkeypatch):
    shot = _finalized(_shot())
    assert shot.roll_path is not None

    monkeypatch.setattr(server, "ballistics_enabled", False)
    server._finalize_shot_detected(shot, emit_event="shot_update")

    assert shot.carry_model == "table"
    assert _roll(shot) == (None, None, None)


def test_a_flight_that_ends_at_launch_has_no_roll_and_logs_nothing_about_it(caplog):
    shot = _finalized(_shot())
    assert shot.roll_path is not None
    shot.launch_angle_vertical = -10.0
    caplog.set_level(logging.DEBUG, logger=server.logger.name)
    caplog.clear()

    server._finalize_shot_metrics(shot)

    assert shot.carry_model == "ballistic"
    assert shot.flight_path is None
    assert _roll(shot) == (None, None, None)
    assert [m for m in caplog.messages if m.startswith(ROLL_LOG_PREFIXES)] == []


def _raise_plain_value_error(_touchdown):
    raise ValueError("touchdown is not finite")


@pytest.mark.parametrize(
    "name, replacement, reason",
    [
        ("simulate_rollout", _raise_plain_value_error, "touchdown is not finite"),
        ("touchdown_of", lambda _trajectory: None, "the flight has no touchdown state"),
        ("roll_path", lambda _trajectory, _rollout: None, "the roll-out gives no roll path"),
    ],
    ids=["model-raises", "no-touchdown-state", "no-roll-path"],
)
@pytest.mark.parametrize("demo", [False, True], ids=["real", "demo"])
def test_failed_rollout_leaves_no_roll(monkeypatch, caplog, demo, name, replacement, reason):
    shot = _finalized(_shot())
    assert shot.roll_path is not None
    shot.demo = demo
    monkeypatch.setattr(server, name, replacement)
    caplog.set_level(logging.DEBUG, logger=server.logger.name)
    caplog.clear()

    server._finalize_shot_metrics(shot)

    assert shot.flight_path is not None
    assert _roll(shot) == (None, None, None)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage()) for r in warnings] == [
        (logging.WARNING, f"[SERVER] Roll-out failed: {reason}")
    ]
    assert [m for m in caplog.messages if m.startswith("[SERVER] No roll")] == []


def _divide_by_zero(_touchdown):
    raise ZeroDivisionError("float division by zero")


def test_a_roll_out_bug_never_costs_the_shot(monkeypatch, caplog, emits):
    logged = []
    session_log = SimpleNamespace(
        log_shot=lambda shot, pipeline_ms, log_only_fields=None: logged.append(shot)
    )
    monkeypatch.setattr(server, "get_session_logger", lambda: session_log)
    monkeypatch.setattr(server, "simulate_rollout", _divide_by_zero)
    caplog.set_level(logging.DEBUG, logger=server.logger.name)

    shot = _finalized(_shot())

    assert shot.carry_model == "ballistic"
    assert _roll(shot) == (None, None, None)
    assert logged == [shot]
    assert [event for event, _ in emits] == ["shot"]
    emitted = emits[0][1]["shot"]
    assert emitted["carry_spin_adjusted"] == round(shot.carry_spin_adjusted)
    assert {name: emitted[name] for name in ROLL_FIELDS} == dict.fromkeys(ROLL_FIELDS)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [(r.levelno, r.getMessage()) for r in warnings] == [
        (logging.WARNING, "[SERVER] Roll-out failed: float division by zero")
    ]
    assert warnings[0].exc_info[0] is ZeroDivisionError


@pytest.mark.parametrize(
    "demo, level", [(False, logging.INFO), (True, logging.DEBUG)], ids=["real", "demo"]
)
def test_fast_landing_has_no_roll(caplog, demo, level):
    shot = _shot(ClubType.WOOD_3, ball_speed_mph=140.0, launch_angle_vertical=3.0, spin_rpm=1500.0)
    shot.demo = demo
    caplog.set_level(logging.DEBUG, logger=server.logger.name)

    server._finalize_shot_metrics(shot)

    trajectory = simulate(server.resolve_launch(shot), air_density=server.air_density_kg_m3)
    assert trajectory.landing_speed_mph * MPH_TO_MPS > MAX_IMPACT_SPEED_MPS
    assert shot.flight_path is not None
    assert _roll(shot) == (None, None, None)
    no_roll = [r for r in caplog.records if r.getMessage().startswith("[SERVER] No roll: ")]
    assert [r.levelno for r in no_roll] == [level]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_wire_rounds_total_like_carry():
    shot = _finalized(_shot())
    data = server.shot_to_dict(shot)
    assert isinstance(data["total_yards"], int)
    assert data["total_yards"] == round(shot.total_yards)
    assert isinstance(data["roll_yards"], int)
    assert data["roll_yards"] == round(shot.roll_yards)
    assert data["roll_path"] == shot.roll_path
    canonical = shot.to_dict()
    assert {name: canonical[name] for name in ROLL_FIELDS} == dict(zip(ROLL_FIELDS, _roll(shot)))


def test_demo_shots_roll():
    session = DemoSession(rng=Random(2026), finalize=server._finalize_shot_metrics)
    shots = session.start(
        now_s=100.0,
        wall=datetime(2026, 10, 8, 12, 0, 0),
        bag_ids=None,
        profile_id="p1",
        profile_name="Golfer",
    )
    assert len(shots) == 6
    for shot in shots:
        assert shot.roll_path is not None, shot.club
        assert math.isfinite(shot.total_yards), shot.club


def test_the_provisional_emit_has_no_roll(emits):
    assert server._emit_initial_ops_shot(_shot()) is True

    assert [event for event, _ in emits] == ["shot"]
    shot = emits[0][1]["shot"]
    assert {name: shot[name] for name in ROLL_FIELDS} == dict.fromkeys(ROLL_FIELDS)


def test_session_log_keeps_the_roll(tmp_path):
    shot = _finalized(_shot())
    session_log = SessionLogger(log_dir=tmp_path, enabled=True)
    session_log.start_session(mode="mock", trigger_type="manual")
    session_log.log_shot(shot)
    session_log.end_session()
    entries = [json.loads(line) for line in session_log.session_path.read_text().splitlines()]
    entry = next(e for e in entries if e["type"] == "shot_detected")
    assert entry["roll_path"] == shot.roll_path
    assert entry["total_yards"] == shot.total_yards
    assert entry["roll_yards"] == shot.roll_yards
