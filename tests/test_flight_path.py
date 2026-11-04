"""Every ballistic shot carries the simulated flight that produced its carry."""

import json
import math
from datetime import datetime

import pytest

from openflight import ballistics, server
from openflight.ballistics import (
    LaunchConditions,
    Trajectory,
    TrajectoryPoint,
    flight_path,
    simulate,
)
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger
from openflight.swing_speed import SwingSpeedEvent

pytestmark = pytest.mark.usefixtures("isolate_finalization")


def _iron_shot(launch_angle_horizontal):
    return Shot(
        ball_speed_mph=103.4,
        timestamp=datetime.now(),
        club=ClubType.IRON_8,
        launch_angle_vertical=26.2,
        launch_angle_confidence=0.95,
        launch_angle_horizontal=launch_angle_horizontal,
        launch_angle_horizontal_confidence=0.8,
        launch_angle_horizontal_source="radar",
        spin_rpm=4669.0,
        spin_confidence=0.9,
        spin_source="measured",
    )


def _point(t, x, y, z):
    return TrajectoryPoint(t, x, y, z, speed_mph=0.0, spin_rpm=0.0)


def _trajectory(*points):
    # flight_path reads only the points; the summary fields are filler.
    return Trajectory(
        points=list(points),
        carry_yards=0.0,
        apex_yards=0.0,
        lateral_yards=0.0,
        flight_time_s=0.0,
        landing_speed_mph=0.0,
        landing_angle_deg=0.0,
    )


def test_flight_path_rounds_points_and_ends_at_the_carry():
    trajectory = simulate(LaunchConditions(102, 19.0, 3.0, 6500, 0.0, "measured"))
    path = flight_path(trajectory)
    assert path["unit"] == "yd"
    assert path["points"][0] == [0.0, 0.0, 0.0, 0.0]
    assert all(value == round(value, 2) for point in path["points"] for value in point)
    t, x, y, z = path["points"][-1]
    assert t == pytest.approx(trajectory.flight_time_s, abs=0.01)
    assert math.hypot(x, y) == pytest.approx(trajectory.carry_yards, abs=0.02)
    assert z == 0.0


def test_flight_path_times_strictly_increase():
    collisions = 0
    for ball_speed in range(60, 181, 3):
        trajectory = simulate(LaunchConditions(ball_speed, 16.0, 2.0, 4000, 0.0, "measured"))
        # The landing is stamped at the exact touchdown time, so it can round to the same
        # hundredth as the last regular 50 ms sample before it.
        rounded_times = [round(point.t, 2) for point in trajectory.points]
        collides = rounded_times[-2] == rounded_times[-1]
        collisions += int(collides)

        points = flight_path(trajectory)["points"]
        assert all(b[0] > a[0] for a, b in zip(points, points[1:])), ball_speed
        # Only the colliding sample goes; every other point stays.
        assert len(points) == len(trajectory.points) - int(collides), ball_speed
        # The landing point survives, still exact.
        t, x, y, z = points[-1]
        assert t == pytest.approx(trajectory.flight_time_s, abs=0.01), ball_speed
        assert math.hypot(x, y) == pytest.approx(trajectory.carry_yards, abs=0.02), ball_speed
        assert z == 0.0, ball_speed

    assert collisions >= 1, "the sweep no longer holds a flight whose last two times round alike"


def test_landing_replaces_a_last_sample_that_rounds_to_its_time():
    trajectory = _trajectory(
        _point(0.0, 0.0, 0.0, 0.0),
        _point(3.62, 103.451, -9.678, 0.614),
        _point(3.67, 104.561, -9.822, 0.052),  # last regular 50 ms sample
        _point(3.6742, 104.642, -9.833, 0.0),  # touchdown, 4 ms later
    )
    assert flight_path(trajectory)["points"] == [
        [0.0, 0.0, 0.0, 0.0],
        [3.62, 103.45, -9.68, 0.61],
        [3.67, 104.64, -9.83, 0.0],
    ]


def test_flight_that_ends_within_5_ms_has_no_flight_path():
    # Launched into the ground: touchdown rounds onto the launch instant, so there is no
    # path to draw. None, not a single point or a zero-length segment.
    trajectory = simulate(LaunchConditions(60.0, -2.0, 0.0, 3000, 0.0, "measured"))
    assert [round(point.t, 2) for point in trajectory.points] == [0.0, 0.0]
    assert flight_path(trajectory) is None


@pytest.mark.parametrize(
    "points",
    [
        pytest.param([], id="no-points"),
        pytest.param([_point(0.0, 0.0, 0.0, 0.0)], id="launch-only"),
        pytest.param(
            [_point(0.0, 0.0, 0.0, 0.0), _point(0.0013, 0.04, 0.0, 0.0)],
            id="lands-in-the-launch-hundredth",
        ),
        # simulate() extrapolates the landing of a ball launched below the ground backwards.
        pytest.param(
            [_point(0.0, 0.0, 0.0, 0.0), _point(-0.0053, 0.0, 0.0, 0.0)],
            id="lands-before-launch",
        ),
    ],
)
def test_flight_path_is_none_without_two_points_in_time_order(points):
    assert flight_path(_trajectory(*points)) is None


def test_two_points_in_time_order_are_a_flight_path():
    trajectory = _trajectory(_point(0.0, 0.0, 0.0, 0.0), _point(0.0078, 0.31, 0.0, 0.0))
    assert flight_path(trajectory) == {
        "unit": "yd",
        "points": [[0.0, 0.0, 0.0, 0.0], [0.01, 0.31, 0.0, 0.0]],
    }


def test_flight_path_has_no_negative_zero():
    trajectory = _trajectory(
        _point(0.0, 0.0, 0.0, 0.0),
        _point(0.05, 3.76, -0.003, 0.95),  # y rounds to -0.0
        _point(0.1, 7.47, -0.012, 1.89),  # a real, if small, negative
        _point(0.14, 10.52, -0.002, 0.0),  # touchdown, y rounds to -0.0 too
    )
    points = flight_path(trajectory)["points"]
    values = [value for point in points for value in point]
    # The sign bit is what reaches the wire as "-0.0", and == cannot see it.
    negative_zeros = [v for v in values if v == 0.0 and math.copysign(1.0, v) < 0]
    assert negative_zeros == []
    assert points[2][2] == -0.01  # a real negative keeps its sign


def test_flight_path_is_none_when_the_flight_never_touched_down():
    trajectory = _trajectory(
        _point(0.0, 0.0, 0.0, 0.0),
        _point(0.05, 3.76, 0.0, 0.95),
        _point(0.1, 7.47, 0.0, 1.89),  # still in the air: there is no landing point
    )
    assert flight_path(trajectory) is None


def test_a_flight_cut_off_by_the_time_cap_has_no_flight_path(monkeypatch):
    monkeypatch.setattr(ballistics, "MAX_FLIGHT_SECONDS", 2.0)
    trajectory = simulate(LaunchConditions(160, 14.0, 0.0, 2800, 0.0, "measured"))
    assert trajectory.points[-1].z > 1.0  # simulate gave up with the ball still in the air
    assert flight_path(trajectory) is None


def test_a_landing_with_a_float_residual_in_z_is_still_a_landing():
    # simulate() interpolates the touchdown, so a real landing can leave z at about 1e-18 yd
    # instead of exactly 0.0 (about 4% of sampled landings did).
    trajectory = _trajectory(
        _point(0.0, 0.0, 0.0, 0.0),
        _point(0.05, 3.76, 0.0, 0.95),
        _point(0.0998, 7.41, 0.0, 7.6e-18),
    )
    assert flight_path(trajectory)["points"][-1] == [0.1, 7.41, 0.0, 0.0]


@pytest.mark.parametrize("value", [math.nan, math.inf])
@pytest.mark.parametrize("field", ["t", "x", "y", "z"])
def test_a_non_finite_value_in_the_flight_means_no_flight_path(monkeypatch, field, value):
    simulate_cleanly = server.simulate

    def simulate_with_a_bad_sample(*args, **kwargs):
        trajectory = simulate_cleanly(*args, **kwargs)
        setattr(trajectory.points[1], field, value)
        return trajectory

    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "simulate", simulate_with_a_bad_sample)
    shot = _iron_shot(3.0)
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.carry_model == "ballistic"
    assert shot.flight_path is None


def test_live_ballistic_shot_carries_its_flight_path(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(3.0)
    expected = flight_path(
        server.simulate(server.resolve_launch(shot), air_density=server.air_density_kg_m3)
    )
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.flight_path == expected
    assert shot.to_dict()["flight_path"] == expected
    assert server.shot_to_dict(shot)["flight_path"] == expected
    x, y = expected["points"][-1][1:3]
    assert math.hypot(x, y) == pytest.approx(shot.carry_spin_adjusted, abs=0.02)


def test_table_carry_has_no_flight_path(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _iron_shot(3.0)
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.flight_path is None
    assert server.shot_to_dict(shot)["flight_path"] is None


def test_refinalizing_as_a_table_carry_drops_the_stale_flight_path(monkeypatch):
    shot = _iron_shot(3.0)
    monkeypatch.setattr(server, "ballistics_enabled", True)
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.flight_path is not None

    monkeypatch.setattr(server, "ballistics_enabled", False)
    server._finalize_shot_detected(shot, emit_event="shot_update")

    assert shot.carry_model == "table"
    assert shot.flight_path is None


def test_flight_that_ends_at_launch_keeps_its_carry_but_has_no_flight_path(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(3.0)
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.flight_path is not None

    shot.launch_angle_vertical = -10.0  # driven into the ground
    server._finalize_shot_detected(shot, emit_event="shot_update")

    assert shot.carry_model == "ballistic"
    assert shot.carry_spin_adjusted == pytest.approx(0.0, abs=0.5)
    assert shot.flight_path is None
    assert shot.to_dict()["flight_path"] is None
    assert server.shot_to_dict(shot)["flight_path"] is None


def test_simulated_shot_flies_the_same_simulation(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(-2.0)
    shot.mode = "mock"
    server._finalize_shot_detected(shot, emit_event="shot")
    assert shot.carry_model == "ballistic"
    x, y = shot.flight_path["points"][-1][1:3]
    assert math.hypot(x, y) == pytest.approx(shot.carry_spin_adjusted, abs=0.02)


def test_swing_speed_rep_has_no_flight_path():
    event = SwingSpeedEvent(
        peak_speed_mph=92.0,
        timestamp=datetime.now(),
        duration_ms=120.0,
        reading_count=7,
        trigger_speed_mph=60.0,
    )
    assert server.swing_speed_to_shot_dict(event).get("flight_path") is None
    assert server.swing_speed_to_shot_dict(event)["apex_yards"] is None


def test_flight_path_stays_under_4_kb_for_a_high_driver():
    path = flight_path(simulate(LaunchConditions(160, 14.0, 0.0, 2800, 0.0, "measured")))
    assert len(json.dumps(path)) < 4096  # the session log's separators, wider than the socket's


def test_session_log_stores_the_flight_path(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    shot = _iron_shot(3.0)
    server._finalize_shot_detected(shot, emit_event="shot")
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="mock", trigger_type="manual")
    logger.log_shot(shot)
    logger.end_session()
    entries = [json.loads(line) for line in logger.session_path.read_text().splitlines()]
    entry = next(e for e in entries if e["type"] == "shot_detected")
    assert entry["flight_path"] == shot.flight_path
