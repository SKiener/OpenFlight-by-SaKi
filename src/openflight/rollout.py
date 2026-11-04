"""
Bounce and roll of a golf ball after it lands on a normal fairway.

Model: Penner, A. R. (2002), "The run of a golf ball", Can. J. Phys. 80:
931-940, doi:10.1139/P02-035, with the paper's own values.

Every impact follows eqs. (2)-(8): the ball digs a crater whose front wall
tilts the contact plane by the crater angle, rebounds off that plane with the
turf's speed-dependent restitution, and friction either acts through the whole
impact (mu < mu_c) or brings the contact point to rest before the ball leaves.
Between impacts the ball flies under gravity alone with its spin unchanged.
Once a bounce would stay below the critical height, the ball rolls to rest at
the constant deceleration of eq. (9), a = -(5/7)·rho·g.

Touchdowns faster than MAX_IMPACT_SPEED_MPS get no roll: eq. (8) scales one
reference crater linearly, so its wall takes v / 53.6 m/s of the impact angle
(about three quarters at 40 m/s), and past the limit low liners turn around and
run back.

A ball that bounces back meets the turf as Penner computed it (Sect. 3.2,
p. 938, the 9-iron of Fig. 7): with omega > 0 the equations mirror only its
motion, so that spin strikes as backspin and brakes it; with omega <= 0 the
mirror is the physical one (-u, -omega). Kept on purpose, with its side effect:
the impact jumps at u = 0, and for wedges more spin can move the rest point up
to about half a metre forward instead of back.

The motion stays in the vertical plane of the touchdown heading: u is the speed
along the heading, w the vertical speed (up +) and omega the backspin as the
impact equations see it.
"""

import math
from dataclasses import astuple, dataclass

from .ballistics import BALL_RADIUS_M, GRAVITY


class OutOfRangeError(ValueError):
    """The touchdown or an impact lies outside the model's range."""


@dataclass(frozen=True)
class Ground:
    """How the turf answers a landing ball.

    mu is the kinetic friction ball-turf, rolling_friction the rho in a = -(5/7)·rho·g, and a
    bounce whose apex stays below critical_bounce_height_m ends in a roll.
    """

    mu: float
    rolling_friction: float
    critical_bounce_height_m: float


NORMAL_FAIRWAY = Ground(mu=0.40, rolling_friction=0.131, critical_bounce_height_m=0.005)
RESTITUTION_POLY = (0.510, -0.0375, 0.000903)
RESTITUTION_POLY_MAX_MPS = 20.0
RESTITUTION_FAST = 0.120
CRATER_ANGLE_REF_RAD = math.radians(15.4)
CRATER_SPEED_REF_MPS = 18.6
CRATER_IMPACT_ANGLE_REF_RAD = math.radians(44.4)
MAX_IMPACT_SPEED_MPS = 40.0
MAX_ROLL_POINTS = 25
MAX_IMPACTS = 100
_ROLL_FRACTIONS = (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0)

_PlanePoint = tuple[float, float, float]


@dataclass(frozen=True)
class Touchdown:
    """The ball as it reaches the ground, in the ballistics frame (x downrange, y right).

    Positions in metres, velocities in m/s with vz < 0; backspin in rad/s, + = backspin
    about the axis perpendicular to the horizontal heading.
    """

    x_m: float
    y_m: float
    vx: float
    vy: float
    vz: float
    backspin_rad_s: float


@dataclass(frozen=True)
class Rollout:
    """The run from the touchdown to the rest point.

    points are (t since touchdown in s, x_m, y_m, z_m) in the ballistics frame; the first
    is the touchdown, the last the rest point. roll_m is signed along the touchdown
    heading, < 0 when the ball spins back.
    """

    points: tuple[tuple[float, float, float, float], ...]
    rest_x_m: float
    rest_y_m: float
    roll_m: float


def restitution(normal_speed_mps: float) -> float:
    """Turf restitution: 0.510 - 0.0375·v + 0.000903·v² up to v = 20 m/s, 0.120 above."""
    speed = abs(normal_speed_mps)
    if speed > RESTITUTION_POLY_MAX_MPS:
        return RESTITUTION_FAST
    c0, c1, c2 = RESTITUTION_POLY
    return c0 + c1 * speed + c2 * speed * speed


def crater_angle_rad(impact_speed_mps: float, impact_angle_from_vertical_rad: float) -> float:
    """Crater wall tilt, eq. (8): 15.4° · (v / 18.6 m/s) · (angle from vertical / 44.4°)."""
    return (
        CRATER_ANGLE_REF_RAD
        * (impact_speed_mps / CRATER_SPEED_REF_MPS)
        * (impact_angle_from_vertical_rad / CRATER_IMPACT_ANGLE_REF_RAD)
    )


def bounce(
    u: float, w: float, omega: float, ground: Ground = NORMAL_FAIRWAY
) -> tuple[float, float, float]:
    """Rebound (u, w, omega) of one impact on the turf, in the plane of the heading.

    Raises OutOfRangeError when the impact leaves the crater fit's range.
    """
    if u < 0.0:
        # Penner (p. 938) mirrors only the motion of a ball running back with backspin, so that
        # spin brakes it (his Fig. 7); without backspin the mirror is the physical one.
        sign = 1.0 if omega > 0.0 else -1.0
        u_r, w_r, omega_r = bounce(-u, w, sign * omega, ground)
        return -u_r, w_r, sign * omega_r
    down = abs(w)
    crater = crater_angle_rad(math.hypot(u, down), math.atan2(u, down))
    cos_c, sin_c = math.cos(crater), math.sin(crater)
    along = u * cos_c - down * sin_c
    normal = u * sin_c + down * cos_c
    if normal <= 0.0:
        raise OutOfRangeError(f"impact leaves the crater model's range: u={u}, w={w}")
    e = restitution(normal)
    normal_impulse = normal * (1.0 + e)
    mu_critical = 2.0 * (along + BALL_RADIUS_M * omega) / (7.0 * normal_impulse)
    if ground.mu < mu_critical:
        along_r = along - ground.mu * normal_impulse
        omega_r = omega - 5.0 * ground.mu * normal_impulse / (2.0 * BALL_RADIUS_M)
    else:
        along_r = 5.0 / 7.0 * along - 2.0 / 7.0 * BALL_RADIUS_M * omega
        omega_r = -along_r / BALL_RADIUS_M
    normal_r = e * normal
    return along_r * cos_c - normal_r * sin_c, along_r * sin_c + normal_r * cos_c, omega_r


def simulate_rollout(touchdown: Touchdown, ground: Ground = NORMAL_FAIRWAY) -> Rollout:
    """Bounces and roll from the touchdown to rest.

    Raises OutOfRangeError (a ValueError) when the touchdown is faster than MAX_IMPACT_SPEED_MPS
    or an impact leaves the crater fit in bounce(), which huge touchdown topspin can still reach
    below the limit. Raises a plain ValueError when the touchdown is not finite or does not move
    down, when the ball neither bounces nor rolls, or when the result is not finite.
    """
    if not all(math.isfinite(value) for value in astuple(touchdown)):
        raise ValueError(f"touchdown is not finite: {touchdown}")
    if touchdown.vz >= 0.0:
        raise ValueError(f"touchdown does not move down: {touchdown}")
    impact_speed = math.hypot(touchdown.vx, touchdown.vy, touchdown.vz)
    if impact_speed > MAX_IMPACT_SPEED_MPS:
        raise OutOfRangeError(
            f"touchdown at {impact_speed:.2f} m/s: the crater fit leaves its range above "
            f"{MAX_IMPACT_SPEED_MPS:.0f} m/s"
        )
    speed = math.hypot(touchdown.vx, touchdown.vy)
    # A vertical landing has no heading of its own; downrange stands in for it.
    heading_x, heading_y = (
        (touchdown.vx / speed, touchdown.vy / speed) if speed > 0.0 else (1.0, 0.0)
    )
    hops, (t, s, u) = _bounces(speed, touchdown.vz, touchdown.backspin_rad_s, ground)
    path = _select_points(hops, _roll(t, s, u, ground))
    if len(path) < 2:
        raise ValueError(f"touchdown neither bounces nor rolls: {touchdown}")
    points = tuple(
        (t_p, touchdown.x_m + s_p * heading_x, touchdown.y_m + s_p * heading_y, z_p)
        for t_p, s_p, z_p in path
    )
    if not all(math.isfinite(value) for point in points for value in point):
        raise ValueError(f"rollout is not finite for {touchdown}")
    return Rollout(
        points=points, rest_x_m=points[-1][1], rest_y_m=points[-1][2], roll_m=path[-1][1]
    )


def _bounces(
    u: float, w: float, omega: float, ground: Ground
) -> tuple[list[tuple[_PlanePoint, _PlanePoint]], tuple[float, float, float]]:
    """Apex and landing (t, s, z) of every bounce, then (t, s, u) where the roll starts."""
    hops = []
    t = s = 0.0
    for impact in range(1, MAX_IMPACTS + 1):
        u, w, omega = bounce(u, w, omega, ground)
        apex = w * w / (2.0 * GRAVITY)
        # A rebound that does not point up keeps the ball on the turf.
        if w <= 0.0 or apex < ground.critical_bounce_height_m or impact == MAX_IMPACTS:
            break
        half = w / GRAVITY
        landing = (t + 2.0 * half, s + 2.0 * half * u, 0.0)
        hops.append(((t + half, s + half * u, apex), landing))
        t, s, _ = landing
        w = -w
    return hops, (t, s, u)


def _roll(t: float, s: float, u: float, ground: Ground) -> list[_PlanePoint]:
    """(t, s, z) at the start, 1/3, 2/3 and end of the roll."""
    deceleration = 5.0 / 7.0 * ground.rolling_friction * GRAVITY
    duration = abs(u) / deceleration
    points = []
    for fraction in _ROLL_FRACTIONS:
        dt = fraction * duration
        points.append((t + dt, s + u * dt - math.copysign(0.5 * deceleration * dt * dt, u), 0.0))
    return points


def _select_points(
    hops: list[tuple[_PlanePoint, _PlanePoint]], roll: list[_PlanePoint]
) -> list[_PlanePoint]:
    """Touchdown, the earliest bounces that fit, then the roll; the rest point comes last."""
    room = (MAX_ROLL_POINTS - 1 - len(roll)) // 2
    path = [(0.0, 0.0, 0.0)]
    for apex, landing in hops[:room]:
        path += [apex, landing]
    *moving, rest = roll
    for point in moving:
        # The roll starts on the last kept landing unless later bounces were dropped.
        if point[0] > path[-1][0]:
            path.append(point)
    if rest[0] > path[-1][0]:
        path.append(rest)
    else:
        # A roll too short to move the clock still has to end on the rest point.
        path[-1] = rest
    return path
