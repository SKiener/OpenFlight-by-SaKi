"""
Physics-based golf ball flight simulator.

Integrates the drag + Magnus ODE using RK4 to produce a deterministic
trajectory from launch conditions. When measured spin is low-confidence
or missing, `resolve_launch` substitutes a club-typical spin value so the
output remains committable (no probabilistic range).

Coordinate system (world frame):
    x — downrange, target direction
    y — lateral, +right
    z — height, up

Aerodynamic model: Cd and Cl are functions of the spin parameter
Sp = r*omega / v. Fits are consistent with Bearman & Harvey (1976) and
Kensrud & Smith (2018) measurements for dimpled golf balls in the
post-drag-crisis regime (Re ~ 5e4 to 2e5). Spin decay follows
Kiratidis & Leinweber (2018) at ~4%/s.
"""

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Mapping, Optional

from .clubs import ClubType
from .clubs.physics import CLUB_PHYSICS
from .launch_monitor import Shot, trusted_spin_rpm

if TYPE_CHECKING:
    from .rollout import Rollout, Touchdown

MPH_TO_MPS = 0.44704
MPS_TO_MPH = 1.0 / MPH_TO_MPS
M_TO_YD = 1.09361

# USGA maximum-conforming ball (45.93 g, 42.7 mm diameter).
# Using the max — not an average — so carry estimates are upper-bounded by
# the rules rather than by a guess at the specific ball in play.
BALL_MASS_KG = 0.04593
BALL_RADIUS_M = 0.02135
BALL_AREA_M2 = math.pi * BALL_RADIUS_M ** 2
AIR_DENSITY_STD = 1.225  # kg/m³ at sea level, 15 °C ISA
# TrackMan "Flat" normalization (no wind, 0 ft altitude, 77 °F):
# ρ = P / (R_specific · T), P = 101325 Pa, T = 298.15 K, R = 287.05 J/(kg·K)
# → 1.1839 kg/m³. The Cd/Cl validation runs at this density.
TRACKMAN_FLAT_AIR_DENSITY = 1.184

# Cd = CD_POLY[0] + CD_POLY[1]*Sp + CD_POLY[2]*Sp^2
# Cl = CL_POLY[0] + CL_POLY[1]*Sp + CL_POLY[2]*Sp^2, clamped at >= 0
#   Second-order polynomials in the spin parameter Sp = r*omega/v, the form
#   Ferguson, McNally & McPhee (2022, ISEA 14, doi:10.5703/1288284317493)
#   fitted to N=1040 shots (GCQuad launch conditions, FlightScope X3
#   carry/apex/offline, Pro V1, Sp 0.02-0.75). The coefficients below are
#   their published values, not re-fitted here.
#   Lift peaks near Sp ~ 0.52 and turns over, as in Bearman & Harvey (1976)
#   and Lyu, Kensrud & Smith (2018). The previous Hill-type saturating form
#   was monotonic by construction, so no choice of its two constants could
#   represent that peak, and it sat ~4 yd low on apex across every club.
#   Beyond Sp = SP_FIT_MAX both curves are held at their end value rather
#   than extrapolating a parabola into a region the fit never saw.
#
# Validation, TrackMan measured launch conditions in, simulated flight out,
# Hill form (as re-fit in #229) -> this form:
#   committed 24-shot capture:            carry RMSE 3.97 -> 2.29 yd
#   independent 5-session 279-shot set:   carry RMSE 3.48 -> 1.98 yd,
#                                         apex RMSE 4.41 -> 0.97 yd
CD_POLY = (0.1304, 0.9287, -0.8259)
CL_POLY = (0.0504, 1.2031, -1.1490)
SP_FIT_MAX = 0.75

# Exponential spin decay: ω(t) = ω₀·exp(-rate·t).
# ~4%/s per Kiratidis & Leinweber (2018); small but matters over ~6 s flights.
SPIN_DECAY_RATE = 0.04

GRAVITY = 9.81
# 500 Hz integration. RK4 error is O(dt⁵) so this is effectively exact for
# the timescales involved; larger dt starts to visibly shorten long drives.
DT_SECONDS = 0.002
# Safety cap — real shots terminate in 5–9 s; anything longer implies the
# solver went unstable or physical inputs are pathological.
MAX_FLIGHT_SECONDS = 15.0
# Trajectory sample cadence for the returned point list. Integration still
# runs at DT_SECONDS; this only controls how many points the caller sees,
# keeping payload size reasonable for UI/log consumers.
SAMPLE_INTERVAL_S = 0.05

# Club-typical spin (RPM) from TrackMan PGA Tour averages.
# Used as fallback when measured spin is missing or low-confidence.
CLUB_TYPICAL_SPIN_RPM: dict[ClubType, float] = {
    club: physics.typical_spin_rpm for club, physics in CLUB_PHYSICS.items()
}

# Range-ball backspin (rpm) from this user's Toptracer history at the Munich range:
# TT sessions 2026-10-02 .. 2026-11-02, current TT processing (exports of 10-30/11-02).
# "measured" = TT median of the club (n >= 8 shots over >= 2 sessions); the other clubs
# are interpolated by nominal loft from the TT/tour ratio of the neighbouring measured
# clubs. Toptracer spin is itself an estimate from the ball flight, not a measurement.
# Source: analysis/flugmodell-20261103/spin (personal_table.py).
RANGE_BALL_SPIN_RPM: dict[ClubType, float] = {
    ClubType.DRIVER: 2250,  # measured, n=120, 6 sessions (tour 2700)
    ClubType.WOOD_3: 2660,  # measured, n=26, 3 sessions (tour 3500)
    ClubType.WOOD_5: 3330,  # interpolated 3-wood..7-wood (tour 4200)
    ClubType.WOOD_7: 3960,  # measured, n=24, 4 sessions (tour 4800)
    ClubType.HYBRID_3: 3540,  # interpolated 3-wood..7-wood (tour 4400)
    ClubType.HYBRID_5: 4270,  # interpolated 7-wood..5-iron (tour 4900)
    ClubType.HYBRID_7: 5350,  # interpolated 7-wood..5-iron (tour 5300)
    ClubType.HYBRID_9: 5720,  # interpolated 5-iron..6-iron (tour 5800)
    ClubType.IRON_2: 3170,  # interpolated 3-wood..7-wood (tour 4000)
    ClubType.IRON_3: 3710,  # interpolated, 7-wood ratio (tour 4500)
    ClubType.IRON_4: 4820,  # interpolated 7-wood..5-iron (tour 5000)
    ClubType.IRON_5: 5950,  # measured, n=18, 3 sessions (tour 5400)
    ClubType.IRON_6: 4190,  # measured, n=11, 2 sessions (tour 6000) - weakest measured value
    ClubType.IRON_7: 6480,  # measured, n=23, 3 sessions (tour 6500)
    ClubType.IRON_8: 4260,  # measured, n=81, 7 sessions (tour 7500)
    ClubType.IRON_9: 3500,  # interpolated 8-iron..PW (tour 8500)
    ClubType.PW: 2310,  # measured, n=20, 3 sessions (tour 9000)
    ClubType.GW: 2580,  # interpolated PW..SW (tour 9500)
    ClubType.SW: 2850,  # measured, n=31, 3 sessions (tour 10000)
    ClubType.LW: 3000,  # interpolated, SW ratio (tour 10500)
    ClubType.UNKNOWN: 5000,  # no club information: tour value kept
}

TYPICAL_SPIN_TABLES: dict[str, Mapping[ClubType, float]] = {
    "tour": CLUB_TYPICAL_SPIN_RPM,
    "range-ball": RANGE_BALL_SPIN_RPM,
}
DEFAULT_TYPICAL_SPIN_TABLE = "tour"

# Range-ball drag (--range-ball-drag-slope, 0.0 = off): above the Reynolds number
# RANGE_BALL_DRAG_RE_KNEE, Cd grows by slope * (Re - knee) / RANGE_BALL_DRAG_RE_SCALE, so
# fast drives brake harder while wedges (below the knee) keep today's drag. Candidate (c1)
# of analysis/flugmodell-20261103/aero (bericht.md section 6): it removed the wood carry
# excess against Toptracer but failed the pre-registered peak/hang-time criterion, so it
# is UNVALIDATED and off by default. Its all-session fit is slope 0.3546.
RANGE_BALL_DRAG_RE_KNEE = 1.2e5
RANGE_BALL_DRAG_RE_SCALE = 1e5
# Dynamic viscosity of air (Pa*s) for that Reynolds number, the study's constant.
AIR_DYNAMIC_VISCOSITY_PA_S = 1.81e-5
RANGE_BALL_DRAG_SLOPE_RANGE = (0.0, 1.0)

# Shadow flights of every ballistic carry for the next range validation against Toptracer
# (session log `carry_variants` only, never the shown carry): name -> (spin table, range-ball
# drag slope). 0.3546 is the study's fitted slope, 0.225 the largest slope that still met
# its criteria on all data without cross-validation (aero/07_feasibility.py).
CARRY_VARIANTS: dict[str, tuple[str, float]] = {
    "tour": ("tour", 0.0),
    "range_ball_spin": ("range-ball", 0.0),
    "range_ball_spin_drag": ("range-ball", 0.3546),
    "range_ball_spin_drag_half": ("range-ball", 0.225),
}
# The variants fly at the step the study validated against DT_SECONDS: at most 0.011 m
# carry difference (aero/02_verify_sim_log.txt) for a fifth of the integration steps.
CARRY_VARIANT_DT_SECONDS = 0.01


@dataclass
class LaunchConditions:
    """Deterministic launch parameters for the physics model or simulator export."""

    ball_speed_mph: float
    launch_angle_v: float
    launch_angle_h: float
    spin_rpm: float
    spin_axis_deg: float
    spin_source: Literal["measured", "club_typical"]


@dataclass
class TrajectoryPoint:
    t: float
    x: float
    y: float
    z: float
    speed_mph: float
    spin_rpm: float


@dataclass
class Trajectory:
    """
    A simulated flight from launch to touchdown.

    touchdown_velocity_mps is the velocity (vx, vy, vz) in m/s as the ball reaches the
    ground, and touchdown_backspin_rad_s its spin about the horizontal axis square to
    that heading (+ = backspin): the state the roll-out starts from. Both are None for a
    flight that never touched down.
    """

    points: list[TrajectoryPoint]
    carry_yards: float
    apex_yards: float
    lateral_yards: float
    flight_time_s: float
    landing_speed_mph: float
    landing_angle_deg: float
    touchdown_velocity_mps: Optional[tuple[float, float, float]] = None
    touchdown_backspin_rad_s: Optional[float] = None


# Operator scale for the club-typical spin (--typical-spin-scale). 1.0 keeps
# the TrackMan PGA Tour averages; the range allows turning them down (or
# slightly up) for players whose real spin sits below the Tour numbers.
TYPICAL_SPIN_SCALE_RANGE = (0.5, 1.2)
# The scale applies to irons and wedges only (the bag's wedge-* ids map to
# PW/GW/SW/LW). Driver, woods, hybrids and unknown clubs keep the table value.
TYPICAL_SPIN_SCALED_CLUBS = frozenset(
    {club for club in ClubType if club.name.startswith("IRON_")}
    | {ClubType.PW, ClubType.GW, ClubType.SW, ClubType.LW}
)


def club_typical_spin_rpm(
    club: Optional[ClubType],
    typical_spin_scale: float = 1.0,
    typical_spin_table: str = DEFAULT_TYPICAL_SPIN_TABLE,
) -> float:
    """
    The club-typical spin of `club` from `typical_spin_table` (`TYPICAL_SPIN_TABLES`).

    A club the table lacks gets its UNKNOWN value. `typical_spin_scale` multiplies
    only irons and wedges (`TYPICAL_SPIN_SCALED_CLUBS`).
    """
    table = TYPICAL_SPIN_TABLES[typical_spin_table]
    typical = table.get(club, table[ClubType.UNKNOWN])
    # The default and clubs other than irons and wedges keep the table
    # value itself (an int, so the session log stays byte-for-byte
    # unchanged); a scaled value is rounded to whole rpm so float noise
    # (4800 * 0.7 = 3359.999...) never reaches the log.
    if typical_spin_scale == 1.0 or club not in TYPICAL_SPIN_SCALED_CLUBS:
        return typical
    return round(typical * typical_spin_scale)


def resolve_launch(
    shot: Shot,
    typical_spin_scale: float = 1.0,
    typical_spin_table: str = DEFAULT_TYPICAL_SPIN_TABLE,
) -> Optional[LaunchConditions]:
    """
    Produce committed launch conditions from a shot.

    Returns None if the vertical launch angle is unavailable (no physics
    simulation possible without it). Spin is taken from the measurement only
    when confidence >= SPIN_CONFIDENCE_HIGH; otherwise a club-typical value
    is substituted and `spin_source` is set to "club_typical".

    `typical_spin_table` picks that club-typical value (`TYPICAL_SPIN_TABLES`:
    "tour" = TrackMan PGA Tour averages, "range-ball" = this user's Toptracer
    range-ball spin). `typical_spin_scale` multiplies only the club-typical
    substitute of irons and wedges (`TYPICAL_SPIN_SCALED_CLUBS`) of either table;
    trusted measured spin and the typical spin of every other club are never scaled.
    """
    if shot.launch_angle_vertical is None:
        return None

    measured_spin = trusted_spin_rpm(shot)
    if measured_spin is not None:
        spin_rpm = measured_spin
        source: Literal["measured", "club_typical"] = "measured"
    else:
        spin_rpm = club_typical_spin_rpm(shot.club, typical_spin_scale, typical_spin_table)
        source = "club_typical"

    return LaunchConditions(
        ball_speed_mph=shot.ball_speed_mph,
        launch_angle_v=shot.launch_angle_vertical,
        launch_angle_h=shot.launch_angle_horizontal or 0.0,
        spin_rpm=spin_rpm,
        spin_axis_deg=shot.spin_axis_deg or 0.0,
        spin_source=source,
    )


def _poly(coeffs: tuple, sp: float) -> float:
    return coeffs[0] + coeffs[1] * sp + coeffs[2] * sp * sp


def _cd(sp: float) -> float:
    return _poly(CD_POLY, min(sp, SP_FIT_MAX))


def _cl(sp: float) -> float:
    if sp <= 0:
        return 0.0
    return max(0.0, _poly(CL_POLY, min(sp, SP_FIT_MAX)))


def _derivatives(
    state: tuple,
    omega: float,
    axis: tuple,
    air_density: float,
    drag_slope: float = 0.0,
) -> tuple:
    """d/dt of (x, y, z, vx, vy, vz) under gravity + drag + Magnus.

    `drag_slope` is the unvalidated range-ball drag (RANGE_BALL_DRAG_RE_KNEE); 0.0 skips it.
    """
    _, _, _, vx, vy, vz = state
    v = math.sqrt(vx * vx + vy * vy + vz * vz)
    # At v ≈ 0 both drag and Magnus vanish (F ∝ v²) and division below
    # would be unsafe. Should only happen at the instantaneous apex of a
    # pathological straight-up launch.
    if v < 1e-6:
        return (vx, vy, vz, 0.0, 0.0, -GRAVITY)

    # Spin parameter Sp drives both Cd and Cl. Note ω is tracked separately
    # (not in the state tuple) because its decay is exponential, not an ODE
    # we want to couple into RK4.
    sp = BALL_RADIUS_M * omega / v
    cd = _cd(sp)
    # Skipped entirely at 0.0, so the default flight stays bit-identical.
    if drag_slope:
        re = air_density * v * 2 * BALL_RADIUS_M / AIR_DYNAMIC_VISCOSITY_PA_S
        cd *= 1.0 + drag_slope * max(0.0, re - RANGE_BALL_DRAG_RE_KNEE) / RANGE_BALL_DRAG_RE_SCALE
    cl = _cl(sp)

    # q: dynamic-pressure × reference-area / mass = acceleration per unit C.
    # Pre-dividing by mass here lets us return accelerations directly.
    q = 0.5 * air_density * v * v * BALL_AREA_M2 / BALL_MASS_KG
    drag_a = cd * q
    lift_a = cl * q

    # Drag opposes velocity: -v̂ · |F_drag|.
    ax = -drag_a * vx / v
    ay = -drag_a * vy / v
    az = -drag_a * vz / v

    # Magnus direction is (ω̂ × v̂). We compute axis × velocity and normalize
    # by the cross-product magnitude rather than |ω|·|v|·sin(θ), which avoids
    # an extra sin() and gracefully degrades when axis ∥ v (spin does no work).
    ox, oy, oz = axis
    cx = oy * vz - oz * vy
    cy = oz * vx - ox * vz
    cz = ox * vy - oy * vx
    c_mag = math.sqrt(cx * cx + cy * cy + cz * cz)
    if c_mag > 1e-6:
        ax += lift_a * cx / c_mag
        ay += lift_a * cy / c_mag
        az += lift_a * cz / c_mag

    az -= GRAVITY

    return (vx, vy, vz, ax, ay, az)


def _rk4_step(
    state: tuple,
    omega: float,
    axis: tuple,
    air_density: float,
    dt: float,
    drag_slope: float = 0.0,
) -> tuple:
    k1 = _derivatives(state, omega, axis, air_density, drag_slope)
    s2 = tuple(state[i] + 0.5 * dt * k1[i] for i in range(6))
    k2 = _derivatives(s2, omega, axis, air_density, drag_slope)
    s3 = tuple(state[i] + 0.5 * dt * k2[i] for i in range(6))
    k3 = _derivatives(s3, omega, axis, air_density, drag_slope)
    s4 = tuple(state[i] + dt * k3[i] for i in range(6))
    k4 = _derivatives(s4, omega, axis, air_density, drag_slope)
    return tuple(
        state[i] + (dt / 6.0) * (k1[i] + 2 * k2[i] + 2 * k3[i] + k4[i])
        for i in range(6)
    )


def _backspin_rad_s(omega: float, axis: tuple, vx: float, vy: float) -> float:
    """
    omega's component about b̂ = (h_y, -h_x, 0), the horizontal axis square to the
    heading ĥ: omega itself for pure backspin, whose axis is b̂. Without a horizontal
    heading, downrange stands in for it, as in the roll-out.
    """
    speed = math.hypot(vx, vy)
    heading_x, heading_y = (vx / speed, vy / speed) if speed > 0.0 else (1.0, 0.0)
    return omega * (axis[0] * heading_y - axis[1] * heading_x)


def simulate(
    conditions: LaunchConditions,
    air_density: float = AIR_DENSITY_STD,
    dt: float = DT_SECONDS,
    range_ball_drag_slope: float = 0.0,
) -> Trajectory:
    """
    Integrate flight from launch to first ground contact (z = 0).

    `range_ball_drag_slope` adds the unvalidated range-ball drag above
    RANGE_BALL_DRAG_RE_KNEE (--range-ball-drag-slope); the default 0.0 flies today's model.
    """
    v0 = conditions.ball_speed_mph * MPH_TO_MPS
    la_v = math.radians(conditions.launch_angle_v)
    la_h = math.radians(conditions.launch_angle_h)

    # Initial velocity in world frame (x downrange, y right, z up).
    vx = v0 * math.cos(la_v) * math.cos(la_h)
    vy = v0 * math.cos(la_v) * math.sin(la_h)
    vz = v0 * math.sin(la_v)

    # Spin axis convention:
    #   spin_axis_deg = 0  → pure backspin, axis = -y
    #     (ω × v with v ≈ +x gives Magnus = +z, i.e. lift up — correct)
    #   spin_axis_deg > 0  → top of axis tilts toward +y (right);
    #     Magnus gains a +y component → fade/slice.
    #   spin_axis_deg < 0  → draw/hook.
    # Rotate the horizontal spin axis with launch direction: changing aim
    # alone cannot change the flight shape in still air.
    axis_rad = math.radians(conditions.spin_axis_deg)
    axis = (
        math.cos(axis_rad) * math.sin(la_h),
        -math.cos(axis_rad) * math.cos(la_h),
        math.sin(axis_rad),
    )

    # rpm → rad/s. Decays exponentially each step; see loop below.
    omega = conditions.spin_rpm * 2 * math.pi / 60.0

    points: list[TrajectoryPoint] = [
        TrajectoryPoint(0.0, 0.0, 0.0, 0.0, conditions.ball_speed_mph, conditions.spin_rpm)
    ]

    state = (0.0, 0.0, 0.0, vx, vy, vz)
    t = 0.0
    max_z = 0.0
    last_sample_t = 0.0

    while t < MAX_FLIGHT_SECONDS:
        new_state = _rk4_step(state, omega, axis, air_density, dt, range_ball_drag_slope)
        t += dt
        omega *= math.exp(-SPIN_DECAY_RATE * dt)

        _, _, z, _, _, _ = new_state
        if z > max_z:
            max_z = z

        # Ground contact. Linear interpolation between prev and new state
        # is sufficient because dt (2 ms) is tiny relative to the timescale
        # of the descent — the state is effectively linear across one step
        # even though the full trajectory is not.
        if z <= 0.0 and t > dt:
            prev_z = state[2]
            denom = prev_z - z
            frac = prev_z / denom if abs(denom) > 1e-9 else 0.0
            t_hit = (t - dt) + frac * dt
            final = tuple(state[i] + frac * (new_state[i] - state[i]) for i in range(6))
            fx, fy, fz, fvx, fvy, fvz = final
            v_final = math.sqrt(fvx * fvx + fvy * fvy + fvz * fvz)
            landing_angle = math.degrees(
                math.atan2(-fvz, math.sqrt(fvx * fvx + fvy * fvy))
            )
            points.append(TrajectoryPoint(
                t_hit,
                fx * M_TO_YD, fy * M_TO_YD, max(fz, 0.0) * M_TO_YD,
                v_final * MPS_TO_MPH,
                omega * 60 / (2 * math.pi),
            ))
            return Trajectory(
                points=points,
                carry_yards=math.hypot(fx, fy) * M_TO_YD,
                apex_yards=max_z * M_TO_YD,
                lateral_yards=fy * M_TO_YD,
                flight_time_s=t_hit,
                landing_speed_mph=v_final * MPS_TO_MPH,
                landing_angle_deg=landing_angle,
                touchdown_velocity_mps=(fvx, fvy, fvz),
                touchdown_backspin_rad_s=_backspin_rad_s(omega, axis, fvx, fvy),
            )

        state = new_state

        if t - last_sample_t >= SAMPLE_INTERVAL_S:
            sx_, sy_, sz_, svx, svy, svz = state
            v = math.sqrt(svx * svx + svy * svy + svz * svz)
            points.append(TrajectoryPoint(
                t,
                sx_ * M_TO_YD, sy_ * M_TO_YD, sz_ * M_TO_YD,
                v * MPS_TO_MPH,
                omega * 60 / (2 * math.pi),
            ))
            last_sample_t = t

    # Flight did not terminate — return current state as best-effort
    fx, fy, fz, fvx, fvy, fvz = state
    v_final = math.sqrt(fvx * fvx + fvy * fvy + fvz * fvz)
    landing_angle = math.degrees(math.atan2(-fvz, math.sqrt(fvx * fvx + fvy * fvy)))
    return Trajectory(
        points=points,
        carry_yards=math.hypot(fx, fy) * M_TO_YD,
        apex_yards=max_z * M_TO_YD,
        lateral_yards=fy * M_TO_YD,
        flight_time_s=t,
        landing_speed_mph=v_final * MPS_TO_MPH,
        landing_angle_deg=landing_angle,
    )


# Spin fractions of the club-typical value that bound the carry span.
CARRY_SPAN_SPIN_FRACTIONS = (0.5, 0.625, 0.75, 0.875, 1.0)


def carry_span_yards(
    conditions: LaunchConditions,
    air_density: float = AIR_DENSITY_STD,
    full_spin_carry_yards: Optional[float] = None,
    range_ball_drag_slope: float = 0.0,
) -> Optional[tuple[float, float]]:
    """
    The (min, max) carry in yards over a band of spin below the club-typical value.

    Range balls and amateur strikes spin below the TrackMan PGA Tour averages the
    typical spin comes from: the 2026-10-25 spin check put the implied spin of the
    range shots at 35-100 % of the typical value. A carry flown with typical spin
    therefore carries that uncertainty. This flies the same launch (ball speed and
    both launch angles) with the spin axis at 0 and the spin at each of
    CARRY_SPAN_SPIN_FRACTIONS of `conditions.spin_rpm` (the club-typical spin the
    carry was flown with) and returns the shortest and the longest carry. Min and
    max are taken over all flights, not the two ends, because wedge carry is not
    monotonic in spin (a SW can fly shorter with less spin). The carry itself is
    not changed; the span only shows its spin uncertainty.

    None unless the spin source is "club_typical": a measured spin gets no span.

    `full_spin_carry_yards` is the carry already flown with these conditions (100 %
    of the spin, at their own spin axis). It always counts towards min and max, so
    the span contains the carry it is shown with. At spin axis 0 it is the 100 %
    flight itself, so the common case costs 4 extra flights rather than 5. A curved
    carry loses lift and can land below every level flight; the 100 % flight is
    then flown at axis 0 too and the curved carry becomes the span minimum.

    `range_ball_drag_slope` is the one the carry was flown with, for the same reason.
    """
    if conditions.spin_source != "club_typical":
        return None
    level = replace(conditions, spin_axis_deg=0.0)
    carries = [] if full_spin_carry_yards is None else [full_spin_carry_yards]
    for fraction in CARRY_SPAN_SPIN_FRACTIONS:
        if (
            fraction == 1.0
            and full_spin_carry_yards is not None
            and conditions.spin_axis_deg == 0.0
        ):
            continue
        flight = simulate(
            replace(level, spin_rpm=conditions.spin_rpm * fraction),
            air_density=air_density,
            range_ball_drag_slope=range_ball_drag_slope,
        )
        carries.append(flight.carry_yards)
    return min(carries), max(carries)


def carry_variants(
    conditions: LaunchConditions,
    club: Optional[ClubType],
    air_density: float = AIR_DENSITY_STD,
    dt: float = CARRY_VARIANT_DT_SECONDS,
) -> dict[str, dict[str, float]]:
    """
    The shadow flights of one carry: `{name: {spin_rpm, carry_m, apex_m}}` per CARRY_VARIANTS.

    Each flies `conditions` (ball speed, both launch angles, spin axis) at `air_density`
    with its variant's range-ball drag slope and the club-typical spin of its table, unscaled
    (no --typical-spin-scale): fixed definitions, whatever the live carry used. A measured
    spin (`spin_source` "measured") stays measured in every variant. Carry and apex are in
    metres, rounded to 0.1. `dt` defaults to CARRY_VARIANT_DT_SECONDS, coarser than the live
    carry's. Raises ValueError for a flight that is not finite.
    """
    variants: dict[str, dict[str, float]] = {}
    for name, (table, drag_slope) in CARRY_VARIANTS.items():
        spin_rpm = (
            conditions.spin_rpm
            if conditions.spin_source == "measured"
            else club_typical_spin_rpm(club, typical_spin_table=table)
        )
        flight = simulate(
            replace(conditions, spin_rpm=spin_rpm),
            air_density=air_density,
            dt=dt,
            range_ball_drag_slope=drag_slope,
        )
        carry_m, apex_m = flight.carry_yards / M_TO_YD, flight.apex_yards / M_TO_YD
        if not (math.isfinite(carry_m) and math.isfinite(apex_m)):
            raise ValueError(f"{name}: flight is not finite")
        variants[name] = {
            "spin_rpm": spin_rpm,
            "carry_m": round(carry_m, 1),
            "apex_m": round(apex_m, 1),
        }
    return variants


def _wire_point(t: float, x: float, y: float, z: float) -> list[float]:
    """
    [t, x, y, z] rounded to 2 decimals for the wire and the session log. `+ 0.0`
    turns the -0.0 that rounding a tiny negative leaves into 0.0.
    """
    return [round(value, 2) + 0.0 for value in (t, x, y, z)]


def flight_path(trajectory: Trajectory) -> Optional[dict]:
    """
    Compact, drawable form of a simulated flight: `[t, x, y, z]` per sample.

    Time is in seconds and x (downrange), y (lateral, +right) and z (height)
    are in yards, all rounded to 2 decimals (and never -0.0) so a long drive
    stays around 3 KB of JSON. The last sample is the landing point, so the path
    ends exactly on the carry the same simulation reported.

    Time strictly increases along the path. The landing point is stamped at the
    exact touchdown time, which can fall within 10 ms of the last regular 50 ms
    sample and round to the same hundredth; the landing point wins and that
    sample is dropped.

    There is no path to draw, and the result is None (never a single point or a
    path that stops in mid-air), for a flight that ends within 5 ms of launch (a
    ball launched into the ground, whose landing `simulate` may even extrapolate
    to before the launch instant), for one that never touched down (`simulate`
    stops at MAX_FLIGHT_SECONDS with the ball still airborne and appends no
    landing point) and for one with any value that is not finite. The result is
    therefore None or a path of at least two finite points that ends on the
    ground.
    """
    points = [_wire_point(point.t, point.x, point.y, point.z) for point in trajectory.points]
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    if len(points) > 1 and points[-1][0] <= points[-2][0]:
        del points[-2]
    # Rounded z on purpose: simulate() interpolates the touchdown, which can leave
    # z at about 1e-18 yd rather than exactly 0.0.
    if len(points) < 2 or points[-1][3] > 0.0:
        return None
    return {"unit": "yd", "points": points}


def touchdown_of(trajectory: Trajectory) -> Optional["Touchdown"]:
    """The flight's touchdown in metres, as the roll-out takes it; None if it never landed."""
    from .rollout import Touchdown  # pylint: disable=cyclic-import

    if trajectory.touchdown_velocity_mps is None or trajectory.touchdown_backspin_rad_s is None:
        return None
    landing = trajectory.points[-1]
    vx, vy, vz = trajectory.touchdown_velocity_mps
    return Touchdown(
        x_m=landing.x / M_TO_YD,
        y_m=landing.y / M_TO_YD,
        vx=vx,
        vy=vy,
        vz=vz,
        backspin_rad_s=trajectory.touchdown_backspin_rad_s,
    )


def roll_path(trajectory: Trajectory, rollout: "Rollout") -> Optional[dict]:
    """
    The roll-out from touchdown to rest in flight_path's format and frame.

    `[t, x, y, z]` per point, with `t` continuing from the flight (flight_time_s plus
    the roll-out's time) and x, y, z in yards, rounded like flight_path. The first
    point is the flight's touchdown exactly as flight_path ends, not the roll-out's own
    copy of it, which can round differently after the trip through metres.

    Time strictly increases: a point whose rounded time does not rise is dropped,
    except the rest point, which replaces the point before it instead, so the path
    always ends where the ball rests. None for fewer than two points or any value
    that is not finite.
    """
    landing = trajectory.points[-1]
    points = [_wire_point(landing.t, landing.x, landing.y, landing.z)] + [
        _wire_point(trajectory.flight_time_s + t, x * M_TO_YD, y * M_TO_YD, z * M_TO_YD)
        for t, x, y, z in rollout.points[1:]
    ]
    if not all(math.isfinite(value) for point in points for value in point):
        return None
    path = points[:1]
    for point in points[1:-1]:
        if point[0] > path[-1][0]:
            path.append(point)
    rest = points[-1]
    if rest[0] > path[-1][0]:
        path.append(rest)
    elif len(path) > 1:
        path[-1] = rest
    if len(path) < 2:
        return None
    return {"unit": "yd", "points": path}
