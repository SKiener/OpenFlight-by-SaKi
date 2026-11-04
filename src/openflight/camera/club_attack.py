"""Arc-consistent attack angle and low point from the rear-camera club-head track.

Experimental, calculated ("berechnet") and pure: numpy/scipy, no I/O, never raises
on bad input. Port of the aoa-arc study (2026-10-25 range clips).

Model. The head's reference point (the club-path feature, face centre by default)
moves on a circle at the constant OPS club speed ``v``. With the arc length
s = v * tau (tau < 0 before contact)::

    P(tau) = P0 + sin(k s)/k * t + (1 - cos(k s))/k * c

P0 is the contact point: lateral x0 and height z0 are fitted, forward is fixed at
``HEAD_BEHIND_BALL_M`` behind the ball centre, as in ``club_head_path``. t is the
unit velocity at contact (path p, attack angle a), c the unit curvature direction
(perpendicular to t, ``plane`` above the horizontal golfer-side direction) and
k = 1/R. Unlike the quadratic fit of ``club_head_path`` the depth follows the
integrated fitted direction instead of the linear y0 + v * tau. The model is
projected through the same camera as the club path (yaw/pitch from the teed ball,
calibrated lens, roll) and fitted to the undistorted, derolled head positions with
a Gaussian prior on k (swing radius about 1.0 m) and a weak prior on the plane.

Low point. The fitted circle has its lowest point where dz/dphi = 0 with
phi = k s, z(phi) = z0 + R (t_z sin phi + c_z (1 - cos phi)). Writing
t_z = A sin(beta), c_z = A cos(beta) gives z = z0 + R c_z - R A cos(phi + beta),
so the minimum is at phi* = -beta = -atan2(t_z, c_z) (after contact for a
descending blow). The low point is

    D = R sin(phi*) t + R (1 - cos(phi*)) c

from the contact point; ``low_point_cm`` is D's horizontal component along the
target line (+ ahead of the ball, toward the target). Its sigma is propagated
like the attack angle's: Jacobian (inflated) or jackknife, the face-centre /
centroid disagreement, the contact time and the club speed.

Sign conventions as ``club_head_path``: path + in-to-out (target right), attack
+ upward. Left-handed golfers have the curvature on the other side: the plane
prior and range mirror to 180 - plane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple, Sequence

import numpy as np
from scipy.optimize import least_squares

from openflight.camera.ball_flight import (
    MPH_PER_MS,
    CameraBallGeometry,
    _apply_horizontal_offset,
    _normalized,
)
from openflight.camera.geometry import camera_to_world

if TYPE_CHECKING:
    from openflight.camera.club_head_path import HeadObservation

SWING_RADIUS_PRIOR_M = 1.0
CURVATURE_PRIOR_SIGMA_PER_M = 0.3  # k = 1/R ~ N(1.0, 0.3) 1/m: R 0.77-1.43 m at 1 sigma
PLANE_PRIOR_DEG = 60.0  # right-handed; left-handed 180 - 60 = 120
PLANE_PRIOR_SIGMA_DEG = 20.0
ARC_PIXEL_SIGMA_PX = 0.5
POSITION_NOISE_FLOOR_PX = 0.3
MAX_AOA_SIGMA_DEG = 1.5
AOA_RANGE_DEG = (-15.0, 12.0)
RADIUS_RANGE_M = (0.5, 3.0)
PLANE_RANGE_DEG = (20.0, 85.0)  # right-handed; left-handed mirrored to (95, 160)
# Fitted height of the face-centre point at contact: irons on the mat land at
# -3..+25 mm with a sane camera pitch; a pitch from a wrong ball anchor
# (2026-10-25 OF55-73, -1.26 deg) puts it at -10..-28 mm.
CONTACT_HEIGHT_RANGE_M = (-0.008, 0.050)
MAX_RMS_PX = 1.5
SPEED_SIGMA_MPH = 1.5
# Calibrated with white pixel noise on the exact model (aoa-arc sim_window): the
# Jacobian sigma understates the attack-angle scatter (pull sd 1.53 / 1.36 / 1.27 for
# 4 / 5 / 6 frames) and the leave-one-frame-out jackknife overstates it (median
# jackknife / empirical sd 1.58 / 1.39 for 5 / 6 frames).
JACOBIAN_INFLATION = {4: 1.5, 5: 1.35, 6: 1.25}
JACKKNIFE_WHITE_NOISE_RATIO = {5: 1.58, 6: 1.39}
CONTACT_GAIN_STEP_S = 0.5e-3
SPEED_GAIN_STEP_MPH = 2.0
ATTACK_METHOD = "arc_v1"
MIN_ARC_FRAMES = 4
# Diagnostic only: attack from the last WINDOW_FRAMES frames minus the full window.
WINDOW_FRAMES = 4

_PARAMETER_SCALE = np.array([0.01, 0.01, 0.02, 0.02, 0.2, 0.2])
_LOW_POINT_STEP = np.array([1e-4, 1e-4, 1e-4, 1e-4, 1e-3, 1e-3])
_FIT_ERRORS = (ValueError, ArithmeticError, np.linalg.LinAlgError)


@dataclass(frozen=True)
class ArcCamera:
    """What the arc fit needs of the club-path scene: pose, lens and geometry."""

    focal_px: float
    pitch_rad: float
    yaw_rad: float
    camera_origin: np.ndarray  # world (lateral, forward, height) of the optical centre
    contact_forward_m: float  # forward of the head reference point at contact
    # Added to the arc path and turns the low point onto the target line: the scene's
    # start-direction offset (horizontal offset, or minus the tapped target's azimuth).
    direction_offset_deg: float = 0.0

    @classmethod
    def from_scene(cls, scene) -> ArcCamera:
        """Camera of a ``club_head_path`` scene (its model, geometry and contact point)."""
        # Imported here: club_head_path calls into this module.
        from openflight.camera.club_head_path import HEAD_BEHIND_BALL_M

        return cls(
            focal_px=float(scene.model.focal_px),
            pitch_rad=float(scene.model.pitch_rad),
            yaw_rad=float(scene.model.yaw_rad),
            camera_origin=np.asarray(scene.geometry.camera_origin, dtype=float),
            contact_forward_m=float(scene.geometry.ball_forward_m - HEAD_BEHIND_BALL_M),
            direction_offset_deg=float(scene.model.direction_offset_deg),
        )


@dataclass(frozen=True)
class ArcFit:
    """Arc fit of one feature track. Angles in deg, lengths in m."""

    attack_deg: float
    path_deg: float
    radius_m: float
    plane_deg: float
    contact_height_m: float
    contact_lateral_m: float
    low_point_m: float
    rms_px: float
    stat_sigma_deg: float
    path_stat_sigma_deg: float
    low_point_stat_sigma_m: float
    residuals_px: np.ndarray
    success: bool


@dataclass(frozen=True)
class AttackAngleResult:
    """Camera attack angle and low point at contact, their sigmas and diagnostics."""

    status: str
    attack_deg: float | None = None
    sigma_deg: float | None = None
    method: str = ATTACK_METHOD
    n_frames: int = 0
    feature: str = "face_centre"
    radius_m: float | None = None
    plane_deg: float | None = None
    contact_height_mm: float | None = None
    rms_px: float | None = None
    arc_path_deg: float | None = None
    arc_path_sigma_deg: float | None = None
    # The arc path as a path measurement (shadow, ``ArcPath``): the fit gates without the
    # attack angle's, every sigma term. ``not_run`` when the fit did not run.
    arc_path_status: str = "not_run"
    arc_path_total_sigma_deg: float | None = None
    arc_path_stat_sigma_deg: float | None = None
    arc_path_jackknife_sigma_deg: float | None = None
    arc_path_feature_sigma_deg: float | None = None
    arc_path_contact_gain_deg_per_ms: float | None = None
    arc_path_speed_gain_deg_per_mph: float | None = None
    stat_sigma_deg: float | None = None
    jackknife_sigma_deg: float | None = None
    feature_sigma_deg: float | None = None
    contact_sigma_deg: float | None = None
    speed_sigma_deg: float | None = None
    contact_gain_deg_per_ms: float | None = None
    low_point_cm: float | None = None
    low_point_sigma_cm: float | None = None
    quadratic_attack_deg: float | None = None
    window_delta_deg: float | None = None
    feature_attacks_deg: dict[str, float] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        """Whether the attack angle passed every gate."""
        return self.status == "accepted"


@dataclass(frozen=True)
class ArcPath:
    """The arc fit's club path at contact with its own sigma; logged only.

    Sigma = quadrature of the same terms as the attack angle's, with the attack
    angle's inflation factors. ``status`` is ``accepted`` when the fit passed its gates
    (fit, residual, radius, plane, contact height); ``path_deg`` and ``sigma_deg`` are
    None otherwise. The path's own sigma and range gates are ``club_head_path``'s.
    """

    status: str
    path_deg: float | None = None
    sigma_deg: float | None = None
    stat_sigma_deg: float | None = None
    jackknife_sigma_deg: float | None = None
    feature_sigma_deg: float | None = None
    contact_gain_deg_per_ms: float | None = None
    speed_gain_deg_per_mph: float | None = None


class _ArcRefits(NamedTuple):
    """The fits of one shot's sigma terms; all but ``fit`` move one input."""

    fit: ArcFit
    alt: ArcFit
    jackknife: list[ArcFit]
    later: ArcFit
    earlier: ArcFit
    faster: ArcFit
    slower: ArcFit


class QuadraticAttack(NamedTuple):
    """Today's quadratic model: attack and path at contact, deg."""

    attack_deg: float
    path_deg: float


# ---------------------------------------------------------------- geometry


def direction(path_rad: float, attack_rad: float) -> np.ndarray:
    """Unit world vector (lateral, forward, up) of a path and attack angle."""
    return np.array(
        [
            math.sin(path_rad) * math.cos(attack_rad),
            math.cos(path_rad) * math.cos(attack_rad),
            math.sin(attack_rad),
        ]
    )


def curvature_direction(tangent: np.ndarray, plane_rad: float) -> np.ndarray:
    """Unit vector perpendicular to ``tangent``, ``plane_rad`` above the golfer-side
    (right-handed: target-left) horizontal."""
    horizontal = np.array([-tangent[1], tangent[0], 0.0])
    horizontal /= np.linalg.norm(horizontal)
    upward = np.cross(tangent, horizontal)
    return math.cos(plane_rad) * horizontal + math.sin(plane_rad) * upward


def arc_points(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    contact: np.ndarray,
    path_rad: float,
    attack_rad: float,
    plane_rad: float,
    curvature: float,
    arc_m: np.ndarray,
) -> np.ndarray:
    """World points (n, 3) on the arc at signed arc lengths ``arc_m`` from the contact."""
    tangent = direction(path_rad, attack_rad)
    centre_dir = curvature_direction(tangent, plane_rad)
    arc_m = np.asarray(arc_m, dtype=float)
    if abs(curvature) < 1e-6:
        along, across = arc_m, 0.5 * curvature * arc_m**2
    else:
        angle = curvature * arc_m
        along = np.sin(angle) / curvature
        across = (1.0 - np.cos(angle)) / curvature
    return (
        np.asarray(contact, dtype=float)[None, :]
        + along[:, None] * tangent[None, :]
        + across[:, None] * centre_dir[None, :]
    )


def low_point_offset_m(
    path_rad: float, attack_rad: float, plane_rad: float, curvature: float
) -> np.ndarray:
    """World offset (lateral, forward, up) from the contact point to the arc's lowest point."""
    tangent = direction(path_rad, attack_rad)
    centre_dir = curvature_direction(tangent, plane_rad)
    angle = -math.atan2(tangent[2], centre_dir[2])
    return (math.sin(angle) * tangent + (1.0 - math.cos(angle)) * centre_dir) / curvature


def _target_component(offset: np.ndarray, horizontal_offset_deg: float) -> float:
    """Horizontal component along the target line (world angle -horizontal_offset)."""
    rad = math.radians(horizontal_offset_deg)
    return float(-offset[0] * math.sin(rad) + offset[1] * math.cos(rad))


def world_to_normalized(points: np.ndarray, camera: ArcCamera) -> tuple[np.ndarray, np.ndarray]:
    """World points -> derolled unit-plane offsets (target-right, up); inverse of
    ``geometry.camera_to_world``."""
    d = np.asarray(points, dtype=float) - camera.camera_origin[None, :]
    cos_yaw, sin_yaw = math.cos(camera.yaw_rad), math.sin(camera.yaw_rad)
    cos_pitch, sin_pitch = math.cos(camera.pitch_rad), math.sin(camera.pitch_rad)
    lateral = cos_yaw * d[:, 0] - sin_yaw * d[:, 1]
    level_forward = sin_yaw * d[:, 0] + cos_yaw * d[:, 1]
    forward = cos_pitch * level_forward + sin_pitch * d[:, 2]
    vertical = -sin_pitch * level_forward + cos_pitch * d[:, 2]
    return lateral / forward, vertical / forward


def plane_ray_points(
    image_x: np.ndarray, image_z: np.ndarray, forward_m: np.ndarray, camera: ArcCamera
) -> np.ndarray:
    """Rays through unit-plane offsets intersected with vertical planes forward = forward_m."""
    lateral, forward, vertical = camera_to_world(
        np.asarray(image_x, dtype=float),
        1.0,
        np.asarray(image_z, dtype=float),
        pitch_rad=camera.pitch_rad,
        yaw_rad=camera.yaw_rad,
    )
    scale = (np.asarray(forward_m, dtype=float) - camera.camera_origin[1]) / forward
    return np.column_stack(
        (
            camera.camera_origin[0] + scale * lateral,
            np.broadcast_to(forward_m, scale.shape),
            camera.camera_origin[2] + scale * vertical,
        )
    )


# ---------------------------------------------------------------- baseline


def _slope_operator(tau_ms: np.ndarray) -> np.ndarray:
    design = np.column_stack((np.ones_like(tau_ms), tau_ms, tau_ms**2))
    return np.linalg.pinv(design)[1]


def quadratic_attack_deg(
    tau_s: np.ndarray,
    image_x: np.ndarray,
    image_z: np.ndarray,
    *,
    speed_ms: float,
    camera: ArcCamera,
) -> QuadraticAttack:
    """Quadratic model (logged only): points on the planes forward = y0 + v * tau,
    quadratic in time, slopes at contact. Its path is ``club_head_path._fit_path``'s."""
    tau_s = np.asarray(tau_s, dtype=float)
    forward = camera.contact_forward_m + speed_ms * tau_s
    points = plane_ray_points(image_x, image_z, forward, camera)
    operator = _slope_operator(tau_s * 1000.0)
    rise = float(operator @ points[:, 2]) * 1000.0
    lateral = float(operator @ points[:, 0]) * 1000.0
    return QuadraticAttack(
        math.degrees(math.atan2(rise, speed_ms)), math.degrees(math.atan2(lateral, speed_ms))
    )


# ---------------------------------------------------------------- arc fit


def _plane_bounds_rad(left_handed: bool) -> tuple[float, float]:
    low, high = math.radians(-10.0), math.radians(120.0)
    return (math.pi - high, math.pi - low) if left_handed else (low, high)


def _residuals(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    q: np.ndarray,
    tau_s: np.ndarray,
    measured: np.ndarray,
    speed_ms: float,
    camera: ArcCamera,
    sigma_px: float,
    prior: tuple[float, float, float, float],
) -> np.ndarray:
    x0, z0, path, attack, plane, curvature = q
    contact = np.array([x0, camera.contact_forward_m, z0])
    points = arc_points(contact, path, attack, plane, curvature, speed_ms * tau_s)
    model_x, model_z = world_to_normalized(points, camera)
    residuals = (
        np.concatenate((model_x - measured[:, 0], model_z - measured[:, 1]))
        * camera.focal_px
        / sigma_px
    )
    curvature_prior, curvature_sigma, plane_prior, plane_sigma = prior
    return np.concatenate(
        (
            residuals,
            [(curvature - curvature_prior) / curvature_sigma, (plane - plane_prior) / plane_sigma],
        )
    )


def _low_point_of(q: np.ndarray, horizontal_offset_deg: float) -> float:
    _x0, _z0, path, attack, plane, curvature = q
    return _target_component(
        low_point_offset_m(path, attack, plane, curvature), horizontal_offset_deg
    )


def _low_point_gradient(q: np.ndarray, horizontal_offset_deg: float) -> np.ndarray:
    gradient = np.zeros(len(q))
    for index, step in enumerate(_LOW_POINT_STEP):
        high, low = q.copy(), q.copy()
        high[index] += step
        low[index] -= step
        gradient[index] = (
            _low_point_of(high, horizontal_offset_deg) - _low_point_of(low, horizontal_offset_deg)
        ) / (2.0 * step)
    return gradient


def _start_values(  # pylint: disable=too-many-arguments
    tau_s: np.ndarray,
    image_x: np.ndarray,
    image_z: np.ndarray,
    *,
    speed_ms: float,
    camera: ArcCamera,
    prior: tuple[float, float, float, float],
) -> np.ndarray:
    quadratic = quadratic_attack_deg(tau_s, image_x, image_z, speed_ms=speed_ms, camera=camera)
    last = plane_ray_points(
        image_x[-1:],
        image_z[-1:],
        np.array([camera.contact_forward_m + speed_ms * tau_s[-1]]),
        camera,
    )[0]
    return np.array(
        [
            last[0],
            last[2],
            math.radians(float(np.clip(quadratic.path_deg, -20.0, 20.0))),
            math.radians(float(np.clip(quadratic.attack_deg * 0.5, -15.0, 10.0))),
            prior[2],
            prior[0],
        ]
    )


def fit_attack_arc(  # pylint: disable=too-many-arguments
    tau_s: np.ndarray,
    image_x: np.ndarray,
    image_z: np.ndarray,
    *,
    speed_ms: float,
    camera: ArcCamera,
    sigma_px: float = ARC_PIXEL_SIGMA_PX,
    radius_prior_m: float = SWING_RADIUS_PRIOR_M,
    left_handed: bool = False,
    horizontal_offset_deg: float = 0.0,
) -> ArcFit:
    """Least-squares arc fit (scipy trf with bounds); sigmas from the Jacobian with
    the residual scale, floored at POSITION_NOISE_FLOOR_PX.

    ``tau_s`` are frame times relative to contact; ``image_x``/``image_z`` the
    undistorted, derolled unit-plane offsets of the head feature, earliest first.
    ``horizontal_offset_deg`` only turns the low point onto the target line.
    """
    tau_s = np.asarray(tau_s, dtype=float)
    image_x = np.asarray(image_x, dtype=float)
    image_z = np.asarray(image_z, dtype=float)
    measured = np.column_stack((image_x, image_z))
    plane_prior_deg = 180.0 - PLANE_PRIOR_DEG if left_handed else PLANE_PRIOR_DEG
    prior = (
        1.0 / radius_prior_m,
        CURVATURE_PRIOR_SIGMA_PER_M,
        math.radians(plane_prior_deg),
        math.radians(PLANE_PRIOR_SIGMA_DEG),
    )
    plane_low, plane_high = _plane_bounds_rad(left_handed)
    lower = np.array([-1.0, -0.5, math.radians(-40.0), math.radians(-40.0), plane_low, 0.05])
    upper = np.array([1.0, 1.0, math.radians(40.0), math.radians(40.0), plane_high, 4.0])
    start = _start_values(tau_s, image_x, image_z, speed_ms=speed_ms, camera=camera, prior=prior)
    start = np.clip(start, lower + 1e-9, upper - 1e-9)
    solution = least_squares(
        _residuals,
        start,
        args=(tau_s, measured, speed_ms, camera, sigma_px, prior),
        bounds=(lower, upper),
        x_scale=_PARAMETER_SCALE,
        method="trf",
    )
    count = len(tau_s)
    pixels = solution.fun[: 2 * count] * sigma_px
    rms = float(math.sqrt(np.mean(pixels**2)))
    dof = max(2 * count - 4, 1)  # 6 parameters, 2 prior rows
    scale = max(math.sqrt(float(np.sum(pixels**2)) / dof), POSITION_NOISE_FLOOR_PX) / sigma_px
    try:
        covariance = np.linalg.inv(solution.jac.T @ solution.jac)
        stat_sigma = math.degrees(math.sqrt(max(covariance[3, 3], 0.0))) * scale
        path_sigma = math.degrees(math.sqrt(max(covariance[2, 2], 0.0))) * scale
        gradient = _low_point_gradient(solution.x, horizontal_offset_deg)
        low_point_sigma = math.sqrt(max(float(gradient @ covariance @ gradient), 0.0)) * scale
    except np.linalg.LinAlgError:
        stat_sigma = path_sigma = low_point_sigma = math.inf
    x0, z0, path, attack, plane, curvature = solution.x
    return ArcFit(
        attack_deg=math.degrees(attack),
        path_deg=math.degrees(path),
        radius_m=1.0 / curvature,
        plane_deg=math.degrees(plane),
        contact_height_m=float(z0),
        contact_lateral_m=float(x0),
        low_point_m=_low_point_of(solution.x, horizontal_offset_deg),
        rms_px=rms,
        stat_sigma_deg=stat_sigma,
        path_stat_sigma_deg=path_sigma,
        low_point_stat_sigma_m=low_point_sigma,
        residuals_px=pixels,
        success=bool(solution.success),
    )


def jackknife_fits(
    tau_s: np.ndarray, image_x: np.ndarray, image_z: np.ndarray, **fit_kwargs
) -> list[ArcFit]:
    """Leave-one-frame-out refits (empty below MIN_ARC_FRAMES + 1 frames)."""
    tau_s, image_x, image_z = (np.asarray(v, dtype=float) for v in (tau_s, image_x, image_z))
    count = len(tau_s)
    if count <= MIN_ARC_FRAMES:
        return []
    fits = []
    for drop in range(count):
        keep = np.arange(count) != drop
        fits.append(fit_attack_arc(tau_s[keep], image_x[keep], image_z[keep], **fit_kwargs))
    return fits


def _jackknife_spread(values: Sequence[float]) -> float:
    array = np.asarray(values, dtype=float)
    count = len(array)
    return float(math.sqrt((count - 1) / count * np.sum((array - array.mean()) ** 2)))


def statistical_sigma(fit_sigma: float, jackknife_sigma: float | None, n_frames: int) -> float:
    """Inflated Jacobian sigma, or the white-noise-rescaled jackknife if larger."""
    frames = min(n_frames, 6)
    sigma = JACOBIAN_INFLATION.get(frames, JACOBIAN_INFLATION[6]) * fit_sigma
    if jackknife_sigma is not None:
        ratio = JACKKNIFE_WHITE_NOISE_RATIO.get(frames, JACKKNIFE_WHITE_NOISE_RATIO[6])
        sigma = max(sigma, jackknife_sigma / ratio)
    return sigma


# ---------------------------------------------------------------- per-shot estimator


def _feature_offsets(
    usable: Sequence[HeadObservation], name: str, *, camera: ArcCamera, geometry: CameraBallGeometry
) -> tuple[np.ndarray, np.ndarray]:
    points = np.asarray([item.features_px[name] for item in usable], dtype=float)
    image_x, image_z = _normalized(
        points[:, 0], points[:, 1], focal_px=camera.focal_px, geometry=geometry
    )
    return np.asarray(image_x, dtype=float), np.asarray(image_z, dtype=float)


def _fit_failures(
    fit: ArcFit, angle_deg: float, *, wood: bool, left_handed: bool
) -> tuple[tuple[bool, str], ...]:
    """The gates on the fit itself, in their fixed order; ``angle_deg`` must be finite too."""
    plane_deg = 180.0 - fit.plane_deg if left_handed else fit.plane_deg
    fitted = (angle_deg, fit.radius_m, fit.plane_deg, fit.contact_height_m, fit.rms_px)
    return (
        (not fit.success or not all(map(math.isfinite, fitted)), "rejected_fit_failed"),
        (fit.rms_px > MAX_RMS_PX, "rejected_residual"),
        (not RADIUS_RANGE_M[0] <= fit.radius_m <= RADIUS_RANGE_M[1], "rejected_implausible_radius"),
        (not PLANE_RANGE_DEG[0] <= plane_deg <= PLANE_RANGE_DEG[1], "rejected_implausible_plane"),
        (
            not wood
            and not CONTACT_HEIGHT_RANGE_M[0] <= fit.contact_height_m <= CONTACT_HEIGHT_RANGE_M[1],
            "rejected_contact_height",
        ),
    )


def _gate(
    fit: ArcFit, sigma: float, *, wood: bool, tee_height_known: bool, left_handed: bool
) -> str:
    """First failed gate in the fixed order, else withheld (woods) or accepted."""
    failures = (
        *_fit_failures(fit, fit.attack_deg, wood=wood, left_handed=left_handed),
        (not AOA_RANGE_DEG[0] <= fit.attack_deg <= AOA_RANGE_DEG[1], "rejected_implausible_attack"),
        (not sigma <= MAX_AOA_SIGMA_DEG, "rejected_sigma_too_large"),
        (wood and not tee_height_known, "withheld_tee_height_unknown"),
    )
    return next((status for failed, status in failures if failed), "accepted")


def _rounded(value: float | None, digits: int) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def estimate_attack_angle(  # pylint: disable=too-many-arguments
    observations: Sequence[HeadObservation],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    club_speed_mph: float | None,
    contact_sigma_s: float,
    feature: str = "face_centre",
    wood: bool = False,
    tee_height_known: bool = False,
    left_handed: bool = False,
) -> AttackAngleResult:
    """Arc attack angle and low point of one shot from the usable head observations.

    ``observations`` are ``club_head_path`` head observations (``t_rel_ms`` relative
    to contact, ``features_px`` on the saved frame); only those whose quality is ok
    count, earliest first. ``camera`` is ``ArcCamera.from_scene`` of the same clip and
    ``geometry`` the club path's (lens, roll); the arc path and low point use the
    camera's ``direction_offset_deg``, the frame of the ball's start direction.

    Sigma = quadrature of the statistical term (inflated Jacobian, or the rescaled
    jackknife from 5 frames), half the face-centre/centroid disagreement, the
    contact-time term |d attack / d t0| * sigma_t0 and the club-speed term
    |d attack / d v| * SPEED_SIGMA_MPH. Gates, first failure wins: no club speed,
    short track, fit failed, residual, radius, plane, contact height (not for
    woods), attack range, sigma, and woods without a known tee height are withheld.
    ``window_delta_deg`` and ``quadratic_attack_deg`` are diagnostics only.
    Never raises.
    """
    if club_speed_mph is None or not math.isfinite(club_speed_mph) or club_speed_mph <= 0.0:
        return AttackAngleResult("rejected_no_club_speed", feature=feature)
    usable = [item for item in observations if item.quality.ok]
    count = len(usable)
    if count < MIN_ARC_FRAMES:
        return AttackAngleResult("rejected_short_track", n_frames=count, feature=feature)
    try:
        return _estimate(
            usable,
            camera=camera,
            geometry=geometry,
            speed_mph=club_speed_mph,
            contact_sigma_s=contact_sigma_s,
            feature=feature,
            wood=wood,
            tee_height_known=tee_height_known,
            left_handed=left_handed,
        )
    except (*_FIT_ERRORS, KeyError, IndexError, TypeError):
        return AttackAngleResult("rejected_fit_failed", n_frames=count, feature=feature)


def _arc_refits(  # pylint: disable=too-many-arguments
    usable: Sequence[HeadObservation],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    speed_ms: float,
    feature: str,
    left_handed: bool,
) -> _ArcRefits:
    tau_s = np.asarray([item.t_rel_ms / 1000.0 for item in usable], dtype=float)
    image_x, image_z = _feature_offsets(usable, feature, camera=camera, geometry=geometry)
    alternative = "centroid" if feature != "centroid" else "face_centre"
    fit_kwargs = {
        "camera": camera,
        "left_handed": left_handed,
        "horizontal_offset_deg": camera.direction_offset_deg,
    }

    def refit(tau, ix=image_x, iz=image_z, speed=speed_ms) -> ArcFit:
        return fit_attack_arc(tau, ix, iz, speed_ms=speed, **fit_kwargs)

    # Contact later by CONTACT_GAIN_STEP_S -> every frame earlier relative to contact.
    speed_step = SPEED_GAIN_STEP_MPH / MPH_PER_MS
    return _ArcRefits(
        fit=refit(tau_s),
        alt=refit(tau_s, *_feature_offsets(usable, alternative, camera=camera, geometry=geometry)),
        jackknife=jackknife_fits(tau_s, image_x, image_z, speed_ms=speed_ms, **fit_kwargs),
        later=refit(tau_s - CONTACT_GAIN_STEP_S),
        earlier=refit(tau_s + CONTACT_GAIN_STEP_S),
        faster=refit(tau_s, speed=speed_ms + speed_step),
        slower=refit(tau_s, speed=speed_ms - speed_step),
    )


def _arc_path(  # pylint: disable=too-many-arguments
    refits: _ArcRefits,
    *,
    n_frames: int,
    offset_deg: float,
    contact_sigma_s: float,
    wood: bool,
    left_handed: bool,
) -> ArcPath:
    """The path of ``refits`` as ``ArcPath``. Never raises: it must not cost the attack angle."""
    fit = refits.fit
    try:
        jackknife = (
            _jackknife_spread([item.path_deg for item in refits.jackknife])
            if refits.jackknife
            else None
        )
        stat = statistical_sigma(fit.path_stat_sigma_deg, jackknife, n_frames)
        feature_sigma = 0.5 * abs(fit.path_deg - refits.alt.path_deg)
        contact_gain = (refits.later.path_deg - refits.earlier.path_deg) / (
            2.0 * CONTACT_GAIN_STEP_S * 1e3
        )
        speed_gain = (refits.faster.path_deg - refits.slower.path_deg) / (2.0 * SPEED_GAIN_STEP_MPH)
        sigma = math.sqrt(
            stat**2
            + feature_sigma**2
            + (contact_gain * max(contact_sigma_s, 0.0) * 1e3) ** 2
            + (speed_gain * SPEED_SIGMA_MPH) ** 2
        )
    except _FIT_ERRORS:
        return ArcPath("rejected_fit_failed")
    if not math.isfinite(sigma):
        return ArcPath("rejected_fit_failed")
    failures = _fit_failures(fit, fit.path_deg, wood=wood, left_handed=left_handed)
    status = next((name for failed, name in failures if failed), "accepted")
    return ArcPath(
        status=status,
        path_deg=(
            _rounded(_apply_horizontal_offset(fit.path_deg, offset_deg), 2)
            if status == "accepted"
            else None
        ),
        sigma_deg=_rounded(sigma, 2) if status == "accepted" else None,
        stat_sigma_deg=_rounded(stat, 2),
        jackknife_sigma_deg=_rounded(jackknife, 2),
        feature_sigma_deg=_rounded(feature_sigma, 2),
        contact_gain_deg_per_ms=_rounded(contact_gain, 3),
        speed_gain_deg_per_mph=_rounded(speed_gain, 3),
    )


def estimate_arc_path(  # pylint: disable=too-many-arguments
    observations: Sequence[HeadObservation],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    club_speed_mph: float,
    contact_sigma_s: float,
    feature: str = "face_centre",
    wood: bool = False,
    left_handed: bool = False,
) -> ArcPath:
    """``ArcPath`` of a run without an attack angle (the woods' other radii). Never raises."""
    usable = [item for item in observations if item.quality.ok]
    if len(usable) < MIN_ARC_FRAMES:
        return ArcPath("rejected_short_track")
    try:
        refits = _arc_refits(
            usable,
            camera=camera,
            geometry=geometry,
            speed_ms=club_speed_mph / MPH_PER_MS,
            feature=feature,
            left_handed=left_handed,
        )
    except (*_FIT_ERRORS, KeyError, IndexError, TypeError):
        return ArcPath("rejected_fit_failed")
    return _arc_path(
        refits,
        n_frames=len(usable),
        offset_deg=camera.direction_offset_deg,
        contact_sigma_s=contact_sigma_s,
        wood=wood,
        left_handed=left_handed,
    )


def arc_path_windows(  # pylint: disable=too-many-arguments
    observations: Sequence[HeadObservation],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    club_speed_mph: float,
    feature: str = "face_centre",
    left_handed: bool = False,
) -> dict[str, float]:
    """The arc path without the last and without the first usable frame, from 5 frames.

    One plain fit each, without gates: how far the path hangs on the window of frames
    (70-bogenpfad). A fit that fails has no entry. Never raises.
    """
    usable = [item for item in observations if item.quality.ok]
    if len(usable) <= MIN_ARC_FRAMES:
        return {}
    windows = {}
    try:
        tau_s = np.asarray([item.t_rel_ms / 1000.0 for item in usable], dtype=float)
        image_x, image_z = _feature_offsets(usable, feature, camera=camera, geometry=geometry)
        for name, keep in (("drop_last", slice(None, -1)), ("drop_first", slice(1, None))):
            fit = fit_attack_arc(
                tau_s[keep],
                image_x[keep],
                image_z[keep],
                speed_ms=club_speed_mph / MPH_PER_MS,
                camera=camera,
                left_handed=left_handed,
                horizontal_offset_deg=camera.direction_offset_deg,
            )
            value = _rounded(_apply_horizontal_offset(fit.path_deg, camera.direction_offset_deg), 2)
            if fit.success and value is not None:
                windows[name] = value
    except (*_FIT_ERRORS, KeyError, IndexError, TypeError):
        return windows
    return windows


def _estimate(  # pylint: disable=too-many-arguments
    usable: Sequence[HeadObservation],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    speed_mph: float,
    contact_sigma_s: float,
    feature: str,
    wood: bool,
    tee_height_known: bool,
    left_handed: bool,
) -> AttackAngleResult:
    count = len(usable)
    speed_ms = speed_mph / MPH_PER_MS
    tau_s = np.asarray([item.t_rel_ms / 1000.0 for item in usable], dtype=float)
    image_x, image_z = _feature_offsets(usable, feature, camera=camera, geometry=geometry)
    alternative = "centroid" if feature != "centroid" else "face_centre"
    refits = _arc_refits(
        usable,
        camera=camera,
        geometry=geometry,
        speed_ms=speed_ms,
        feature=feature,
        left_handed=left_handed,
    )
    fit, alt, jackknife, later, earlier, faster, slower = refits
    quadratic = quadratic_attack_deg(tau_s, image_x, image_z, speed_ms=speed_ms, camera=camera)

    jack_attack = _jackknife_spread([f.attack_deg for f in jackknife]) if jackknife else None
    jack_low = _jackknife_spread([f.low_point_m for f in jackknife]) if jackknife else None
    stat = statistical_sigma(fit.stat_sigma_deg, jack_attack, count)
    stat_low = statistical_sigma(fit.low_point_stat_sigma_m, jack_low, count)

    contact_gain = (later.attack_deg - earlier.attack_deg) / (2.0 * CONTACT_GAIN_STEP_S * 1e3)
    contact_gain_low = (later.low_point_m - earlier.low_point_m) / (2.0 * CONTACT_GAIN_STEP_S)
    speed_gain = (faster.attack_deg - slower.attack_deg) / (2.0 * SPEED_GAIN_STEP_MPH)
    speed_gain_low = (faster.low_point_m - slower.low_point_m) / (2.0 * SPEED_GAIN_STEP_MPH)

    contact_sigma_s = max(contact_sigma_s, 0.0)
    feature_sigma = 0.5 * abs(fit.attack_deg - alt.attack_deg)
    contact_sigma = abs(contact_gain) * contact_sigma_s * 1e3
    speed_sigma = abs(speed_gain) * SPEED_SIGMA_MPH
    sigma = math.sqrt(stat**2 + feature_sigma**2 + contact_sigma**2 + speed_sigma**2)
    low_point_sigma = math.sqrt(
        stat_low**2
        + (0.5 * (fit.low_point_m - alt.low_point_m)) ** 2
        + (contact_gain_low * contact_sigma_s) ** 2
        + (speed_gain_low * SPEED_SIGMA_MPH) ** 2
    )

    window_delta = None
    if count > WINDOW_FRAMES:
        window = fit_attack_arc(
            tau_s[-WINDOW_FRAMES:],
            image_x[-WINDOW_FRAMES:],
            image_z[-WINDOW_FRAMES:],
            speed_ms=speed_ms,
            camera=camera,
            left_handed=left_handed,
            horizontal_offset_deg=camera.direction_offset_deg,
        )
        window_delta = window.attack_deg - fit.attack_deg

    arc_path = _arc_path(
        refits,
        n_frames=count,
        offset_deg=camera.direction_offset_deg,
        contact_sigma_s=contact_sigma_s,
        wood=wood,
        left_handed=left_handed,
    )
    frames = min(count, 6)
    return AttackAngleResult(
        status=_gate(
            fit, sigma, wood=wood, tee_height_known=tee_height_known, left_handed=left_handed
        ),
        attack_deg=_rounded(fit.attack_deg, 2),
        sigma_deg=_rounded(sigma, 2),
        n_frames=count,
        feature=feature,
        radius_m=_rounded(fit.radius_m, 3),
        plane_deg=_rounded(fit.plane_deg, 1),
        contact_height_mm=_rounded(fit.contact_height_m * 1000.0, 1),
        rms_px=_rounded(fit.rms_px, 3),
        arc_path_deg=_rounded(
            _apply_horizontal_offset(fit.path_deg, camera.direction_offset_deg), 2
        ),
        arc_path_sigma_deg=_rounded(
            JACOBIAN_INFLATION.get(frames, JACOBIAN_INFLATION[6]) * fit.path_stat_sigma_deg, 2
        ),
        arc_path_status=arc_path.status,
        arc_path_total_sigma_deg=arc_path.sigma_deg,
        arc_path_stat_sigma_deg=arc_path.stat_sigma_deg,
        arc_path_jackknife_sigma_deg=arc_path.jackknife_sigma_deg,
        arc_path_feature_sigma_deg=arc_path.feature_sigma_deg,
        arc_path_contact_gain_deg_per_ms=arc_path.contact_gain_deg_per_ms,
        arc_path_speed_gain_deg_per_mph=arc_path.speed_gain_deg_per_mph,
        stat_sigma_deg=_rounded(stat, 2),
        jackknife_sigma_deg=_rounded(jack_attack, 2),
        feature_sigma_deg=_rounded(feature_sigma, 2),
        contact_sigma_deg=_rounded(contact_sigma, 2),
        speed_sigma_deg=_rounded(speed_sigma, 2),
        contact_gain_deg_per_ms=_rounded(contact_gain, 3),
        low_point_cm=_rounded(fit.low_point_m * 100.0, 2),
        low_point_sigma_cm=_rounded(low_point_sigma * 100.0, 2),
        quadratic_attack_deg=_rounded(quadratic.attack_deg, 2),
        window_delta_deg=_rounded(window_delta, 2),
        feature_attacks_deg={
            feature: _rounded(fit.attack_deg, 2),
            alternative: _rounded(alt.attack_deg, 2),
        },
    )
