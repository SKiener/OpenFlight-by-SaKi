"""D-plane spin-axis model: ball launch, club path and attack angle to spin axis.

Pure functions, no I/O. The ball does not leave along the face normal: launch
~ k * face + (1 - k) * club delivery, with k the face weight. Wood, Henrikson &
Broadie (2018), "The influence of face angle and club path on the resultant
launch angle of a golf ball", Proceedings 2(6):249, measured k ~ 0.61-0.83
depending on club and impact. One k serves both components: horizontally the
launch direction sits between face angle and club path, vertically the launch
angle sits between dynamic loft and attack angle.

Face-to-path F2P = (launch direction - path) / k. Spin loft = dynamic loft -
attack angle = (launch angle - attack angle) / k. Spin axis =
atan(sin(F2P) / tan(spin loft)) + SPIN_AXIS_OFFSET_DEG (a provisional calibration
against Toptracer curves). With the same k in numerator and denominator the axis
barely depends on k. Sign convention as Shot.spin_axis_deg and
ballistics: face open to the path (F2P > 0) gives a positive axis (fade),
closed gives a negative axis (draw).

Woods (driver and fairway woods) get no axis: without the impact location the
gear effect dominates their curve, and the D-plane axis does worse there than
an axis of 0.

Calculated ("berechnet") face data and lofts, from the camera club path and
attack angle (study "dplane-face", 2026-10-25):

- face to target FTT = (LD - (1 - k) * path) / k, + open (right);
- dynamic loft = AoA + (LA - AoA) / k, spin loft = (LA - AoA) / k;
- sigmas are first-order propagated from independent input sigmas (LD, path,
  LA, AoA, k, roll). A common yaw error of LD and path (one camera frame)
  moves FTT 1:1 and cancels in F2P and the axis; roll does not cancel and
  adds ``ld_per_roll_deg`` times its sigma to the start direction.

Display rules: FTT as a number only at sigma <= FACE_SHOW_MAX_SIGMA_DEG; F2P
and the axis only as a class (``classify``), none above CLASS_MAX_SIGMA_DEG.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional, Union

from .clubs import ClubType
from .clubs.bag import club_category
from .clubs.physics import SHOT_SIMULATION_DEFAULTS

FACE_WEIGHT_WOODS = 0.85
FACE_WEIGHT_IRONS = 0.75
MIN_SPIN_LOFT_DEG = 5.0
MAX_SPIN_LOFT_DEG = 70.0
# Placeholder attack angle for every club (-4 deg, the shot generator's mean),
# used until a measured camera attack angle exists.
MEAN_ATTACK_ANGLE_DEG = SHOT_SIMULATION_DEFAULTS.angle_of_attack_mean_deg

WOOD_CATEGORIES = frozenset({ClubType.DRIVER, ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7})

# k uncertainty: literature 0.61-0.83 (Wood et al. 2018), TrackMan k ~ 0.918 - 0.0039*SL
# gives 0.80-0.87; 0.05 (1 sigma) covers the gap between 0.75/0.85 and those.
FACE_WEIGHT_SIGMA = 0.05
FACE_SHOW_MAX_SIGMA_DEG = 1.5
CLASS_Z = 2.0
CLASS_MAX_SIGMA_DEG = 3.0
# Start-direction sigma per HorizontalFusionDecision.status, camera start directions only
# (camera against the Toptracer start direction 2026-10-25: sd 0.64 deg incl. Toptracer's
# own error). The TI fallback (camera_withheld_fallback_iwr, residual sd 2.16 deg against
# the camera) is deliberately absent: a TI start direction gives no face and no class.
# Any status not listed gives no D-plane face.
LAUNCH_DIRECTION_SIGMA_DEG = {
    "camera_assisted_high": 0.7,
    "camera_assisted_experimental_agreement": 1.0,
    "camera_experimental_disagreement": 1.3,
    "camera_experimental_no_iwr": 1.3,
    "camera_only_experimental": 1.5,
}
# Vertical launch sigma by source: the camera launch angle, or the TI radar one (which
# reads +2.1..2.5 deg above Toptracer on 2026-10-25; lofts are shown with the camera only).
LAUNCH_VERTICAL_SIGMA_DEG = {"camera": 0.5, "radar": 1.5}
# Placeholder attack angle: real per-shot scatter 2.2 deg plus the unknown per-club mean.
PLACEHOLDER_ATTACK_SIGMA_DEG = 3.0
# Roll: uncalibrated (horizon -1.48, ground vanishing line -1.20, compensation 0).
ROLL_SIGMA_DEG_UNCALIBRATED = 0.7
ROLL_SIGMA_DEG_CALIBRATED = 0.2
# Start direction per degree of roll = tan(LA) + ROLL_LD_OFFSET; confirmed on the
# 2026-10-25 replay chains: PW +0.649 (formula 0.64), 8i +0.475 (0.47), 7w +0.239 (0.25).
# The camera path barely moves with roll (PW -0.06, 8i 0.00, DR +0.05).
ROLL_LD_OFFSET = 0.106
F2P_CLASSES = ("closed", "neutral", "open")
AXIS_CLASSES = ("draw", "straight", "fade")
# Calculated axis read 3.5 deg too far to the draw side against the Toptracer curves of
# 27 camera-direction iron shots on 2026-10-26 (axis vs curve regression, intercept/slope;
# leave-one-out direction agreement 10/11 for |curve| >= 1 m vs 10/14 uncorrected).
# Provisional: re-check on the next Toptracer and Trackman sessions. Added to every
# calculated axis (``dplane_spin_axis`` and ``dplane_face``); the sigma is unchanged.
SPIN_AXIS_OFFSET_DEG = 3.5
# Curve shape of the drawn flight (``curve_class``), from the point estimate of the
# calculated D-plane axis (``dplane_spin_axis``), right-handed convention: a negative
# axis curves left (draw/hook), a positive one right (fade/slice). Provisional limits,
# to be checked against a Trackman iO reference session.
CURVE_CLASSES = ("hook", "draw", "straight", "fade", "slice")
CURVE_STRAIGHT_MAX_DEG = 2.5
CURVE_STRONG_MIN_DEG = 8.0


def _club_type(club: Union[ClubType, str, None]) -> ClubType:
    """Resolve a club category or bag label; anything unrecognised is UNKNOWN."""
    if isinstance(club, ClubType):
        return club
    try:
        return club_category(club)
    except (ValueError, TypeError):
        return ClubType.UNKNOWN


def face_weight_for_club(club: Union[ClubType, str, None]) -> float:
    """Face weight k: woods FACE_WEIGHT_WOODS, everything else FACE_WEIGHT_IRONS."""
    return FACE_WEIGHT_WOODS if _club_type(club) in WOOD_CATEGORIES else FACE_WEIGHT_IRONS


def face_to_path_deg(
    launch_direction_deg: float, club_path_deg: float, face_weight: float
) -> float:
    """Face angle relative to the club path, from launch direction and path."""
    return (launch_direction_deg - club_path_deg) / face_weight


def spin_loft_deg(launch_vertical_deg: float, attack_angle_deg: float, face_weight: float) -> float:
    """Spin loft (launch angle - attack angle) / k, clamped to a plausible range.

    face_weight is the same k as in face_to_path_deg.
    """
    spin_loft = (launch_vertical_deg - attack_angle_deg) / face_weight
    return min(max(spin_loft, MIN_SPIN_LOFT_DEG), MAX_SPIN_LOFT_DEG)


def spin_axis_deg(f2p_deg: float, spin_loft_angle_deg: float) -> float:
    """Spin-axis tilt in degrees from face-to-path and spin loft: +right (fade), -left (draw)."""
    return math.degrees(
        math.atan(math.sin(math.radians(f2p_deg)) / math.tan(math.radians(spin_loft_angle_deg)))
    )


def dplane_spin_axis(
    club: Union[ClubType, str, None],
    launch_direction_deg: Optional[float],
    club_path_deg: Optional[float],
    launch_vertical_deg: Optional[float],
    attack_angle_deg: Optional[float] = None,
) -> Optional[float]:
    """Calculated spin axis (0.1 deg) for a shot, or None.

    None when any of club, launch direction, club path or launch angle is missing, and
    for every club in WOOD_CATEGORIES. attack_angle_deg is a measured attack angle;
    without one the placeholder MEAN_ATTACK_ANGLE_DEG is used for every club. The
    calibration offset SPIN_AXIS_OFFSET_DEG is added before rounding.
    """
    if club is None or launch_direction_deg is None:
        return None
    if club_path_deg is None or launch_vertical_deg is None:
        return None
    if _club_type(club) in WOOD_CATEGORIES:
        return None
    attack = MEAN_ATTACK_ANGLE_DEG if attack_angle_deg is None else attack_angle_deg
    face_weight = face_weight_for_club(club)
    f2p = face_to_path_deg(launch_direction_deg, club_path_deg, face_weight)
    loft = spin_loft_deg(launch_vertical_deg, attack, face_weight)
    return round(spin_axis_deg(f2p, loft) + SPIN_AXIS_OFFSET_DEG, 1)


def curve_class(  # pylint: disable=redefined-outer-name
    spin_axis_deg: Optional[float],
) -> Optional[str]:
    """Curve class of a calculated spin axis (deg, + right), or None without a finite axis.

    |axis| < CURVE_STRAIGHT_MAX_DEG is "straight"; up to CURVE_STRONG_MIN_DEG (exclusive)
    "draw" (negative) or "fade" (positive); from CURVE_STRONG_MIN_DEG on "hook" or "slice".
    """
    if isinstance(spin_axis_deg, bool) or not isinstance(spin_axis_deg, (int, float)):
        return None
    axis = float(spin_axis_deg)
    if not math.isfinite(axis):
        return None
    magnitude = abs(axis)
    if magnitude < CURVE_STRAIGHT_MAX_DEG:
        return "straight"
    if magnitude < CURVE_STRONG_MIN_DEG:
        return "draw" if axis < 0 else "fade"
    return "hook" if axis < 0 else "slice"


def spin_loft_unclamped_deg(
    launch_vertical_deg: float, attack_angle_deg: float, face_weight: float
) -> float:
    """Spin loft (LA - AoA) / k without the clamp of ``spin_loft_deg``."""
    return (launch_vertical_deg - attack_angle_deg) / face_weight


def dynamic_loft_deg(
    launch_vertical_deg: float, attack_angle_deg: float, face_weight: float
) -> float:
    """Dynamic loft AoA + (LA - AoA) / k: the face's loft at contact."""
    return attack_angle_deg + spin_loft_unclamped_deg(
        launch_vertical_deg, attack_angle_deg, face_weight
    )


@dataclass(frozen=True)
class CalculatedLoft:
    """Calculated (berechnet) dynamic loft and spin loft of one shot, deg."""

    dynamic_loft_deg: float
    dynamic_loft_sigma_deg: float
    spin_loft_deg: float
    spin_loft_sigma_deg: float
    face_weight: float
    launch_source: str


def _finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def calculated_loft(
    club: Union[ClubType, str, None],
    launch_vertical_deg: Optional[float],
    attack_angle_deg: Optional[float],
    *,
    attack_sigma_deg: Optional[float],
    launch_source: Optional[str],
) -> Optional[CalculatedLoft]:
    """Dynamic and spin loft with propagated sigmas, or None.

    None when the club, an angle or the attack sigma is missing, for every club in
    WOOD_CATEGORIES, for any launch source but "camera" (the TI launch angle reads
    about 2-2.5 deg high and that would move both lofts by about 3 deg), and when the
    unclamped spin loft lies outside [MIN_SPIN_LOFT_DEG, MAX_SPIN_LOFT_DEG]. In range
    it is the same value ``spin_loft_deg`` gives the D-plane axis. Sigmas, for
    independent launch and attack errors: sigma_SL = hypot(s_LA, s_a) / k and
    sigma_DL = hypot(s_LA / k, (1 - 1/k) * s_a).
    """
    if club is None or launch_source != "camera":
        return None
    if not (
        _finite(launch_vertical_deg) and _finite(attack_angle_deg) and _finite(attack_sigma_deg)
    ):
        return None
    if _club_type(club) in WOOD_CATEGORIES:
        return None
    k = face_weight_for_club(club)
    spin_loft = spin_loft_unclamped_deg(launch_vertical_deg, attack_angle_deg, k)
    if not MIN_SPIN_LOFT_DEG <= spin_loft <= MAX_SPIN_LOFT_DEG:
        return None
    sigma_launch = LAUNCH_VERTICAL_SIGMA_DEG[launch_source]
    sigma_attack = abs(attack_sigma_deg)
    return CalculatedLoft(
        dynamic_loft_deg=attack_angle_deg + spin_loft,
        dynamic_loft_sigma_deg=math.hypot(sigma_launch / k, (1.0 - 1.0 / k) * sigma_attack),
        spin_loft_deg=spin_loft,
        spin_loft_sigma_deg=math.hypot(sigma_launch, sigma_attack) / k,
        face_weight=k,
        launch_source=launch_source,
    )


def face_to_target_deg(
    launch_direction_deg: float, club_path_deg: float, face_weight: float
) -> float:
    """Face angle to the target line: (LD - (1 - k) * path) / k. + open (right)."""
    return (launch_direction_deg - (1.0 - face_weight) * club_path_deg) / face_weight


def ld_per_roll_deg(launch_vertical_deg: Optional[float]) -> float:
    """d(start direction)/d(roll) in deg/deg: tan(LA) + ROLL_LD_OFFSET.

    0 without a finite LA (None, NaN or +-inf count as missing).
    """
    if not _finite(launch_vertical_deg):
        return 0.0
    return math.tan(math.radians(launch_vertical_deg)) + ROLL_LD_OFFSET


def classify(
    value: Optional[float],
    sigma: Optional[float],
    labels: tuple[str, str, str],
    *,
    z: float = CLASS_Z,
    max_sigma: float = CLASS_MAX_SIGMA_DEG,
) -> Optional[str]:
    """labels = (negative, neutral, positive): |value| >= z * sigma names a side.

    None when the value or sigma is missing or not finite, or sigma > max_sigma.
    """
    if value is None or sigma is None or not math.isfinite(value) or not math.isfinite(sigma):
        return None
    if sigma > max_sigma:
        return None
    if value >= z * sigma:
        return labels[2]
    if value <= -z * sigma:
        return labels[0]
    return labels[1]


def launch_direction_sigma_deg(fusion_status: Optional[str]) -> Optional[float]:
    """Sigma of the shot's start direction from its horizontal fusion status.

    The status is "<HorizontalFusionDecision.status>" or "<status>:<camera reason>";
    only the part before the colon counts. None (no D-plane face) for a status not in
    LAUNCH_DIRECTION_SIGMA_DEG, which includes the TI fallback.
    """
    if not fusion_status:
        return None
    return LAUNCH_DIRECTION_SIGMA_DEG.get(fusion_status.split(":", 1)[0])


def _partial(fn: Callable[..., float], x: dict, name: str, step: float) -> float:
    high = dict(x)
    low = dict(x)
    high[name] += step
    low[name] -= step
    return (fn(**high) - fn(**low)) / (2.0 * step)


def _propagated_sigma(fn: Callable[..., float], x: dict, sigmas: dict) -> float:
    """First-order sigma of fn(**x) for independent inputs (central differences)."""
    total = 0.0
    for name, sigma in sigmas.items():
        if not sigma:
            continue
        step = 1e-3 if name == "k" else 1e-2
        total += (_partial(fn, x, name, step) * sigma) ** 2
    return math.sqrt(total)


def _ftt(  # pylint: disable=too-many-arguments
    *, ld, path, k, roll=0.0, ld_roll=0.0, path_roll=0.0
):
    return face_to_target_deg(ld + ld_roll * roll, path + path_roll * roll, k)


def _f2p(  # pylint: disable=too-many-arguments
    *, ld, path, k, roll=0.0, ld_roll=0.0, path_roll=0.0
):
    return face_to_path_deg(ld + ld_roll * roll, path + path_roll * roll, k)


def _axis(  # pylint: disable=too-many-arguments
    *, ld, path, la, aoa, k, roll=0.0, ld_roll=0.0, path_roll=0.0
):
    f2p = face_to_path_deg(ld + ld_roll * roll, path + path_roll * roll, k)
    return spin_axis_deg(f2p, spin_loft_deg(la, aoa, k))


@dataclass(frozen=True)
class DPlaneFace:
    """Calculated (berechnet) face data of one shot. Angles in deg, + right / open / fade."""

    status: str
    club_category: str
    face_weight: Optional[float] = None
    face_to_target_deg: Optional[float] = None
    face_to_target_sigma_deg: Optional[float] = None
    face_to_target_shown: bool = False
    face_to_path_deg: Optional[float] = None
    face_to_path_sigma_deg: Optional[float] = None
    face_to_path_class: Optional[str] = None
    spin_loft_deg: Optional[float] = None
    spin_axis_deg: Optional[float] = None
    spin_axis_sigma_deg: Optional[float] = None
    spin_axis_class: Optional[str] = None
    inputs: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-serialisable payload for the session log."""
        return asdict(self)


def _rounded(value: Optional[float], digits: int = 1) -> Optional[float]:
    """Rounded value; None for a missing or non-finite one (keeps the payload strict JSON)."""
    return round(value, digits) if _finite(value) else None


def dplane_face(  # pylint: disable=too-many-arguments,redefined-outer-name
    club: Union[ClubType, str, None],
    *,
    launch_direction_deg: Optional[float],
    launch_direction_sigma_deg: Optional[float],
    club_path_deg: Optional[float],
    club_path_sigma_deg: Optional[float],
    launch_vertical_deg: Optional[float] = None,
    launch_vertical_sigma_deg: float = LAUNCH_VERTICAL_SIGMA_DEG["radar"],
    attack_angle_deg: Optional[float] = None,
    attack_angle_sigma_deg: Optional[float] = None,
    roll_sigma_deg: float = ROLL_SIGMA_DEG_UNCALIBRATED,
    face_weight_sigma: float = FACE_WEIGHT_SIGMA,
) -> DPlaneFace:
    """Face to target, face to path and spin-axis class with propagated sigmas.

    status: "ok", "ok_no_axis_wood", "ok_no_axis_no_launch_vertical",
    "no_launch_direction", "no_club_path" or "no_club". Without a measured attack
    angle the placeholder MEAN_ATTACK_ANGLE_DEG with PLACEHOLDER_ATTACK_SIGMA_DEG is
    used. The axis uses the clamped ``spin_loft_deg``, like ``dplane_spin_axis``.
    Never raises for missing inputs.
    """
    category = _club_type(club)
    measured = _finite(attack_angle_deg)
    inputs = {
        "launch_direction_deg": _rounded(launch_direction_deg, 2),
        "launch_direction_sigma_deg": _rounded(launch_direction_sigma_deg, 2),
        "club_path_deg": _rounded(club_path_deg, 2),
        "club_path_sigma_deg": _rounded(club_path_sigma_deg, 2),
        "launch_vertical_deg": _rounded(launch_vertical_deg, 2),
        "attack_angle_deg": _rounded(attack_angle_deg if measured else MEAN_ATTACK_ANGLE_DEG, 2),
        "attack_angle_source": "measured" if measured else "placeholder",
        "roll_sigma_deg": roll_sigma_deg,
    }
    if club is None:
        return DPlaneFace("no_club", category.value, inputs=inputs)
    if not (_finite(launch_direction_deg) and _finite(launch_direction_sigma_deg)):
        return DPlaneFace("no_launch_direction", category.value, inputs=inputs)
    if not (_finite(club_path_deg) and _finite(club_path_sigma_deg)):
        return DPlaneFace("no_club_path", category.value, inputs=inputs)

    k = face_weight_for_club(category)
    x = {
        "ld": launch_direction_deg,
        "path": club_path_deg,
        "k": k,
        "roll": 0.0,
        "ld_roll": ld_per_roll_deg(launch_vertical_deg),
        "path_roll": 0.0,
    }
    sigmas = {
        "ld": launch_direction_sigma_deg,
        "path": club_path_sigma_deg,
        "k": face_weight_sigma,
        "roll": roll_sigma_deg,
    }
    ftt = _ftt(**x)
    ftt_sigma = _propagated_sigma(_ftt, x, sigmas)
    f2p = _f2p(**x)
    f2p_sigma = _propagated_sigma(_f2p, x, sigmas)

    axis = axis_sigma = axis_class = loft = None
    status = "ok"
    if category in WOOD_CATEGORIES:
        status = "ok_no_axis_wood"
    elif not _finite(launch_vertical_deg):
        status = "ok_no_axis_no_launch_vertical"
    else:
        aoa = attack_angle_deg if measured else MEAN_ATTACK_ANGLE_DEG
        aoa_sigma = (
            attack_angle_sigma_deg
            if measured and _finite(attack_angle_sigma_deg) and attack_angle_sigma_deg
            else PLACEHOLDER_ATTACK_SIGMA_DEG
        )
        x_axis = dict(x, la=launch_vertical_deg, aoa=aoa)
        sigmas_axis = dict(sigmas, la=launch_vertical_sigma_deg, aoa=aoa_sigma)
        loft = spin_loft_deg(launch_vertical_deg, aoa, k)
        # The offset is a constant: it moves the axis, not its propagated sigma.
        axis = _axis(**x_axis) + SPIN_AXIS_OFFSET_DEG
        axis_sigma = _propagated_sigma(_axis, x_axis, sigmas_axis)
        axis_class = classify(axis, axis_sigma, AXIS_CLASSES)

    return DPlaneFace(
        status=status,
        club_category=category.value,
        face_weight=k,
        face_to_target_deg=_rounded(ftt),
        face_to_target_sigma_deg=_rounded(ftt_sigma, 2),
        face_to_target_shown=ftt_sigma <= FACE_SHOW_MAX_SIGMA_DEG,
        face_to_path_deg=_rounded(f2p),
        face_to_path_sigma_deg=_rounded(f2p_sigma, 2),
        face_to_path_class=classify(f2p, f2p_sigma, F2P_CLASSES),
        spin_loft_deg=_rounded(loft),
        spin_axis_deg=_rounded(axis),
        spin_axis_sigma_deg=_rounded(axis_sigma, 2),
        spin_axis_class=axis_class,
        inputs=inputs,
    )
