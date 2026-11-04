"""Penner's bounce-and-roll model gives every landing a rest point."""

import math

import pytest

from openflight.rollout import (
    MAX_IMPACT_SPEED_MPS,
    Ground,
    OutOfRangeError,
    Touchdown,
    bounce,
    crater_angle_rad,
    restitution,
    simulate_rollout,
)

# Penner (2002) Figs. 4 and 7: (impact speed m/s, angle from the vertical deg, backspin rad/s).
DRIVE_SHALLOW = (26.8, 68.0, 224.0)
DRIVE_STEEP = (26.2, 46.2, 315.0)
NINE_IRON_809 = (23.8, 34.0, 809.0)
NINE_IRON_1200 = (23.8, 34.0, 1200.0)
PENNER_CASES = (DRIVE_SHALLOW, DRIVE_STEEP, NINE_IRON_809, NINE_IRON_1200)


def touchdown(v, phi_deg, omega, x_m=0.0, y_m=0.0):
    phi = math.radians(phi_deg)
    return Touchdown(
        x_m=x_m,
        y_m=y_m,
        vx=v * math.sin(phi),
        vy=0.0,
        vz=-v * math.cos(phi),
        backspin_rad_s=omega,
    )


def roll_m(v, phi_deg, omega):
    return simulate_rollout(touchdown(v, phi_deg, omega)).roll_m


def test_restitution_matches_penner():
    assert restitution(0.0) == pytest.approx(0.510)
    assert restitution(10.0) == pytest.approx(0.2253, abs=1e-4)
    assert restitution(25.0) == pytest.approx(0.120)


def test_crater_angle_matches_penner():
    angle = crater_angle_rad(18.6, math.radians(44.4))
    assert math.degrees(angle) == pytest.approx(15.4, abs=1e-9)


@pytest.mark.parametrize(
    ("u", "w", "omega", "expected"),
    [
        pytest.param(
            13.308791, -19.731095, 809.0, (-0.14862, 2.75827, -26.912), id="rolls-out-mu-ge-mu_c"
        ),
        pytest.param(
            6.8404029, -18.7938524, 1500.0, (-4.69157, 1.78913, 473.964), id="slides-mu-lt-mu_c"
        ),
    ],
)
def test_bounce_matches_the_worked_impacts(u, w, omega, expected):
    """Both impact branches against values worked by hand from eqs. (2)-(8)."""
    u_r, w_r, omega_r = bounce(u, w, omega)
    assert u_r == pytest.approx(expected[0], abs=2e-4)
    assert w_r == pytest.approx(expected[1], abs=2e-4)
    assert omega_r == pytest.approx(expected[2], abs=2e-2)


@pytest.mark.parametrize(
    ("omega", "omega_after"),
    [
        pytest.param(84.8, -53.96, id="backspin-keeps-its-sign-p938"),
        pytest.param(-84.8, 53.96, id="topspin-mirrors-physically"),
    ],
)
def test_a_ball_running_back_is_braked(omega, omega_after):
    """Worked by hand: running back, the ball meets the turf with backspin |omega| (p. 938)."""
    u_r, w_r, omega_r = bounce(-2.451, -2.137, omega)
    assert u_r == pytest.approx(-1.1002, abs=1e-3)
    assert w_r == pytest.approx(1.0302, abs=1e-3)
    assert omega_r == pytest.approx(omega_after, abs=5e-2)


@pytest.mark.parametrize(
    ("vx", "vz", "omega", "direction"),
    [
        pytest.param(2.0, -0.1, 0.0, 1.0, id="rolls-on"),
        pytest.param(0.05, -0.6, 1000.0, -1.0, id="rolls-back"),
    ],
)
def test_a_landing_below_the_critical_bounce_rolls_at_the_eq_9_deceleration(
    vx, vz, omega, direction
):
    u_r, w_r, _ = bounce(vx, vz, omega)
    assert math.copysign(1.0, u_r) == direction
    assert w_r**2 / (2 * 9.81) < 0.005
    rollout = simulate_rollout(Touchdown(0.0, 0.0, vx, 0.0, vz, omega))
    deceleration = 5 / 7 * 0.131 * 9.81
    assert rollout.roll_m == pytest.approx(direction * u_r**2 / (2 * deceleration))
    assert rollout.points[-1][0] == pytest.approx(abs(u_r) / deceleration)
    assert len(rollout.points) == 4


def test_a_landing_above_the_critical_bounce_hops_first():
    w_r = bounce(2.0, -0.4, 0.0)[1]
    assert w_r**2 / (2 * 9.81) > 0.005
    points = simulate_rollout(Touchdown(0.0, 0.0, 2.0, 0.0, -0.4, 0.0)).points
    assert points[1][3] == pytest.approx(w_r**2 / (2 * 9.81))


def test_the_first_apex_follows_the_rebound():
    landing = touchdown(*NINE_IRON_809)
    u_1, w_1, _ = bounce(landing.vx, landing.vz, landing.backspin_rad_s)
    t_apex, x_apex, _, z_apex = simulate_rollout(landing).points[1]
    assert t_apex == pytest.approx(w_1 / 9.81)
    assert x_apex == pytest.approx(u_1 * w_1 / 9.81)
    assert z_apex == pytest.approx(w_1**2 / (2 * 9.81))


def test_drive_shallow_impact_runs_about_35_m():
    assert 28.0 <= roll_m(*DRIVE_SHALLOW) <= 42.0


def test_drive_steep_impact_runs_about_17_m():
    assert 12.0 <= roll_m(*DRIVE_STEEP) <= 22.0


def test_nine_iron_with_809_rad_s_stops_near_the_landing():
    assert -1.5 <= roll_m(*NINE_IRON_809) <= 1.0


def test_nine_iron_with_1200_rad_s_runs_back():
    assert -3.0 <= roll_m(*NINE_IRON_1200) <= -0.8


def test_more_backspin_rolls_less():
    rolls = [roll_m(24.0, 40.0, omega) for omega in range(200, 1201, 100)]
    assert all(later <= earlier for earlier, later in zip(rolls, rolls[1:])), rolls


def test_steeper_landing_rolls_less():
    rolls = [roll_m(25.0, phi, 300.0) for phi in range(70, 34, -5)]
    assert all(later <= earlier for earlier, later in zip(rolls, rolls[1:])), rolls


def test_spin_back_is_bounded():
    assert roll_m(20.0, 20.0, 1500.0) >= -5.0


@pytest.mark.parametrize("case", PENNER_CASES)
def test_rollout_path_shape(case):
    landing = touchdown(*case, x_m=142.5, y_m=-3.25)
    rollout = simulate_rollout(landing)
    points = rollout.points
    assert points[0] == (0.0, 142.5, -3.25, 0.0)
    assert all(later[0] > earlier[0] for earlier, later in zip(points, points[1:]))
    assert all(point[3] >= 0.0 for point in points)
    assert all(math.isfinite(value) for point in points for value in point)
    assert len(points) <= 25
    assert points[-1][1:3] == (rollout.rest_x_m, rollout.rest_y_m)
    assert points[-1][3] == 0.0


def test_long_runs_keep_the_earliest_bounces():
    points = simulate_rollout(touchdown(*DRIVE_SHALLOW)).points
    assert len(points) == 25
    assert points[1][3] == max(point[3] for point in points)
    assert points[2][0] == pytest.approx(2 * points[1][0])
    assert [point[3] for point in points[-4:]] == [0.0, 0.0, 0.0, 0.0]


def assert_finite_with_rising_time(points):
    assert all(math.isfinite(value) for point in points for value in point)
    assert all(later[0] > earlier[0] for earlier, later in zip(points, points[1:]))


def test_without_a_critical_height_the_impact_cap_ends_the_bounces():
    rollout = simulate_rollout(touchdown(*DRIVE_SHALLOW), Ground(0.40, 0.131, 0.0))
    assert_finite_with_rising_time(rollout.points)
    assert len(rollout.points) == 25


def test_a_rebound_into_the_turf_rolls_at_once():
    landing = touchdown(39.0, 45.0, 3000.0)
    assert bounce(landing.vx, landing.vz, landing.backspin_rad_s)[1] < 0.0
    rollout = simulate_rollout(landing, Ground(0.40, 0.131, 0.0))
    assert_finite_with_rising_time(rollout.points)
    assert len(rollout.points) == 4


def test_a_vertical_drop_bounces_in_place():
    rollout = simulate_rollout(
        Touchdown(x_m=120.0, y_m=-2.0, vx=0.0, vy=0.0, vz=-12.0, backspin_rad_s=0.0)
    )
    assert (rollout.rest_x_m, rollout.rest_y_m, rollout.roll_m) == (120.0, -2.0, 0.0)
    assert len(rollout.points) > 2
    assert all(later[0] > earlier[0] for earlier, later in zip(rollout.points, rollout.points[1:]))
    assert rollout.points[-1][1:] == (120.0, -2.0, 0.0)


def test_rollout_follows_the_touchdown_heading():
    heading = math.radians(30.0)
    along_x = touchdown(*DRIVE_SHALLOW)
    turned = Touchdown(
        x_m=150.0,
        y_m=4.0,
        vx=along_x.vx * math.cos(heading),
        vy=along_x.vx * math.sin(heading),
        vz=along_x.vz,
        backspin_rad_s=along_x.backspin_rad_s,
    )
    rollout = simulate_rollout(turned)
    for _, x_m, y_m, _ in rollout.points:
        dx, dy = x_m - turned.x_m, y_m - turned.y_m
        assert abs(dx * math.sin(heading) - dy * math.cos(heading)) <= 1e-6 * math.hypot(dx, dy)
    dx, dy = rollout.rest_x_m - turned.x_m, rollout.rest_y_m - turned.y_m
    assert dx * math.cos(heading) + dy * math.sin(heading) > 0.0
    assert rollout.roll_m == pytest.approx(simulate_rollout(along_x).roll_m)


@pytest.mark.parametrize("field", ["x_m", "y_m", "vx", "vy", "vz", "backspin_rad_s"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_rejects_non_finite_input(field, value):
    values = {"x_m": 0.0, "y_m": 0.0, "vx": 20.0, "vy": 0.0, "vz": -15.0, "backspin_rad_s": 300.0}
    values[field] = value
    with pytest.raises(ValueError) as excinfo:
        simulate_rollout(Touchdown(**values))
    assert not isinstance(excinfo.value, OutOfRangeError)


@pytest.mark.parametrize("vz", [0.0, 2.0])
def test_rejects_a_touchdown_that_does_not_move_down(vz):
    with pytest.raises(ValueError) as excinfo:
        simulate_rollout(Touchdown(x_m=0.0, y_m=0.0, vx=20.0, vy=0.0, vz=vz, backspin_rad_s=300.0))
    assert not isinstance(excinfo.value, OutOfRangeError)


def test_rejects_a_soft_drop_that_neither_bounces_nor_rolls():
    with pytest.raises(ValueError) as excinfo:
        simulate_rollout(Touchdown(x_m=0.0, y_m=0.0, vx=0.0, vy=0.0, vz=-0.2, backspin_rad_s=0.0))
    assert not isinstance(excinfo.value, OutOfRangeError)


def test_the_model_takes_touchdowns_up_to_40_m_s():
    at_limit = Touchdown(x_m=0.0, y_m=0.0, vx=38.4, vy=0.0, vz=-11.2, backspin_rad_s=100.0)
    assert MAX_IMPACT_SPEED_MPS == 40.0
    assert math.hypot(at_limit.vx, at_limit.vy, at_limit.vz) == 40.0
    assert len(simulate_rollout(at_limit).points) >= 2
    beyond = Touchdown(
        x_m=0.0, y_m=0.0, vx=38.4 * 40.01 / 40, vy=0.0, vz=-11.2 * 40.01 / 40, backspin_rad_s=100.0
    )
    with pytest.raises(OutOfRangeError, match=r"crater fit.*40 m/s"):
        simulate_rollout(beyond)


def test_a_touchdown_too_fast_to_square_is_out_of_range():
    with pytest.raises(OutOfRangeError):
        simulate_rollout(Touchdown(x_m=0.0, y_m=0.0, vx=1e200, vy=0.0, vz=-1.0, backspin_rad_s=0.0))


def test_an_impact_beyond_the_crater_fit_is_out_of_range():
    with pytest.raises(OutOfRangeError, match="crater model's range"):
        bounce(120.0, -1.0, 0.0)


def test_huge_topspin_below_the_limit_still_reaches_the_crater_fit_limit():
    with pytest.raises(OutOfRangeError, match="crater model's range"):
        simulate_rollout(touchdown(0.5, 60.0, -17750.0))



def test_out_of_range_is_a_value_error():
    assert issubclass(OutOfRangeError, ValueError)


def test_an_absurd_topspin_that_overflows_the_rollout_is_a_plain_value_error():
    absurd = Touchdown(x_m=0.0, y_m=0.0, vx=20.0, vy=0.0, vz=-15.0, backspin_rad_s=-1e157)
    with pytest.raises(ValueError, match="rollout is not finite") as excinfo:
        simulate_rollout(absurd)
    assert not isinstance(excinfo.value, OutOfRangeError)

@pytest.mark.parametrize("omega", [50.0, 150.0, 260.0])
@pytest.mark.parametrize("phi_deg", [60.0, 70.0, 80.0, 85.0])
@pytest.mark.parametrize("v", [30.0, 35.0, 39.99])
def test_low_liners_up_to_the_limit_spin_back_at_most_a_few_metres(v, phi_deg, omega):
    assert roll_m(v, phi_deg, omega) > -3.0
