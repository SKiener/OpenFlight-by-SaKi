"""Aiming the same shot sideways must rotate, not deform, its flight."""

import math
from dataclasses import replace

import pytest

from openflight.ballistics import LaunchConditions, simulate


@pytest.mark.parametrize("yaw", [-25, -10, 10, 25])
@pytest.mark.parametrize("axis", [-15, 0, 15])
def test_aim_rotation_preserves_trajectory_and_flat_carry(yaw, axis):
    launch = LaunchConditions(120, 18, 0, 6000, axis, "measured")
    straight = simulate(launch)
    rotated = simulate(replace(launch, launch_angle_h=yaw))
    angle = math.radians(yaw)
    c, s = math.cos(angle), math.sin(angle)
    a, b = straight.points[-1], rotated.points[-1]
    assert b.x == pytest.approx(c * a.x - s * a.y, abs=1e-6)
    assert b.y == pytest.approx(s * a.x + c * a.y, abs=1e-6)
    assert rotated.apex_yards == pytest.approx(straight.apex_yards, abs=1e-6)
    assert rotated.flight_time_s == pytest.approx(straight.flight_time_s, abs=1e-6)
    assert rotated.carry_yards == pytest.approx(straight.carry_yards, abs=1e-6)


def test_flat_carry_is_launch_to_landing_distance_not_target_projection():
    result = simulate(LaunchConditions(120, 18, 12, 6000, 10, "measured"))
    end = result.points[-1]
    assert result.carry_yards == pytest.approx(math.hypot(end.x, end.y))
    assert result.lateral_yards == pytest.approx(end.y)
