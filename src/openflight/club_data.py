"""Calculated club data ("berechnet") of one shot from the camera club-data job.

Pure. ``calculate_club_data`` turns the worker's ``ClubDataResult`` and the
shot's fused start direction and launch angles into the shot fields of
``CLUB_DATA_FIELDS``, each set only where its gate passes (plan section 4), so
a None means "not available" and the UI never re-derives a gate:

* camera club path: the path passed its gates (sigma <= 2 deg, |path| <= 20 deg);
* camera attack angle and the arc low point: the arc fit was accepted (woods
  are withheld there); the low point never for a wood;
* dynamic and spin loft: an accepted attack angle and a camera launch angle
  (``dplane.calculated_loft``: not for woods, spin loft within 5-70 deg);
* the D-plane face (``dplane.dplane_face``): an accepted path, a right-handed
  golfer (signs and labels assume one, plan D14) and a start direction. Its
  sigma comes from the horizontal fusion status, so a TI start direction gives
  neither a face to target nor a class. Face to target only within
  ``FACE_SHOW_MAX_SIGMA_DEG``; the face-to-path and spin-axis numbers feed the
  session means, their classes the shot; the face-to-path class for every
  club, the axis (number and class) never for ``WOOD_CATEGORIES``;
* the impact point (``_impact_fields``): the measurement across contact
  (method D, ``camera.impact_point``) when it passed its gates, else the older
  extrapolating estimator (method E, ``club_head_path._impact_location``) when it
  did, never a mean of both; ``impact_method`` says which. E's millimetres are
  brought to D's scale (``ImpactPoint.e_scale_ratio``, about 1: both use the
  known camera-ball range) and its sigma
  widened by ``IMPACT_E_FALLBACK_SIGMA_MM`` (its known heel offset and scatter
  against D). The face height only for a teed wood or driver; the zone
  (``impact_zone``) only where the sigma leaves it unambiguous. Logged and sent
  with the shot, never fed into the curve, the spin axis or the flight.

The server writes these fields; in ``--club-data show`` it also makes an
iron's ``calculated_spin_axis_deg`` the shot's one spin axis (``spin_axis_deg``,
``curve_class`` and the flight, ``server._publish_club_data``). Never
``club_path_deg``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Union

from .clubs import ClubType
from .dplane import (
    LAUNCH_VERTICAL_SIGMA_DEG,
    ROLL_SIGMA_DEG_CALIBRATED,
    ROLL_SIGMA_DEG_UNCALIBRATED,
    WOOD_CATEGORIES,
    CalculatedLoft,
    DPlaneFace,
    calculated_loft,
    dplane_face,
    launch_direction_sigma_deg,
)
from .launch_monitor import CLUB_DATA_SHOT_FIELDS

logger = logging.getLogger(__name__)

# The shot fields the club data sets; the status and reason are set by the server.
CLUB_DATA_FIELDS: tuple[str, ...] = tuple(
    name for name in CLUB_DATA_SHOT_FIELDS if name not in ("club_data_status", "club_data_reason")
)
# Clubs whose arc attack angle (and so low point) is withheld: the woods segmentation
# set of the club-head path (plan D12), hybrids included.
_ATTACK_WITHHELD_CLUBS = frozenset(
    {
        ClubType.DRIVER,
        ClubType.WOOD_3,
        ClubType.WOOD_5,
        ClubType.WOOD_7,
        ClubType.HYBRID_3,
        ClubType.HYBRID_5,
        ClubType.HYBRID_7,
        ClubType.HYBRID_9,
    }
)

# Impact zone: |offset| below the half width is "center" (mm). PRELIMINARY until the
# Trackman iO check: set from the 2026-10-25 session spread (study
# analysis/treffpunkt-20261101); the silhouette midpoint may sit a fixed few mm off
# the face centre per club, which no sigma here contains.
IMPACT_ZONE_CENTRE_HALF_WIDTH_MM = {"irons": 6.0, "woods": 8.0}
# Woods half width for these clubs (the club-head path's woods segmentation set);
# every other club, wedges and an unknown club included, takes the irons one.
IMPACT_ZONE_WOOD_CLUBS = _ATTACK_WITHHELD_CLUBS
# The zone is the class (heel / center / toe) holding the most probability under
# N(offset, sigma^2), None below 0.68 (the mass of a +/-1 sigma interval). On the
# 2026-10-25 replay (59 shown offsets) that names a zone for 47 (80 %), with an
# expected misclassification of 12 % among them from sigma alone; 0.5 names 58
# (18 %), 0.8 names 33 (5 %). A driver with sigma 5 mm (half width 8): center below
# about 5.6 mm, toe/heel beyond about 10.3 mm, None between.
IMPACT_ZONE_MIN_PROBABILITY = 0.68
IMPACT_ZONES = ("heel", "center", "toe")
IMPACT_METHODS = ("D", "E")
# E as the fallback, brought to D's scale (``ImpactPoint.e_scale_ratio``): on the 46
# shots both measured on the 2026-10-25 replay, D - e_scale_ratio * E had a median of
# +4.4 mm (E's heel offset from extrapolating the curved head path) and an SD of
# 2.4 mm (RMS 4.9); that RMS enters E's sigma in quadrature instead of a correction.
IMPACT_E_FALLBACK_SIGMA_MM = 4.9
# The face height counts only on a teed ball of these clubs.
IMPACT_HEIGHT_CLUBS = WOOD_CATEGORIES


@dataclass(frozen=True)
class CalculatedClubData:
    """The gated shot fields of one shot and what the session log needs besides them.

    ``fields`` holds every key of ``CLUB_DATA_FIELDS`` (None where its gate
    failed). ``face`` is None when the D-plane face was not computed, and
    ``dplane_reason`` then says why.
    """

    fields: dict = field(default_factory=lambda: dict.fromkeys(CLUB_DATA_FIELDS))
    loft: Optional[CalculatedLoft] = None
    face: Optional[DPlaneFace] = None
    dplane_reason: Optional[str] = None


def _tenth(value: Optional[float]) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), 1)


def _club_type(club: Union[ClubType, str, None]) -> Optional[ClubType]:
    if isinstance(club, ClubType) or club is None:
        return club
    try:
        return ClubType(club)
    except ValueError:
        return ClubType.UNKNOWN


def _finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def impact_zone(
    offset_mm: Optional[float], sigma_mm: Optional[float], club: Union[ClubType, str, None]
) -> Optional[str]:
    """``heel``, ``center`` or ``toe`` for a heel/toe offset (+ toe), None when ambiguous.

    Center is |offset| < ``IMPACT_ZONE_CENTRE_HALF_WIDTH_MM`` (irons and wedges
    6 mm, woods and the driver 8 mm; preliminary until the Trackman iO check).
    The zone is the class with the most probability under N(offset, sigma^2), and
    None when that is below ``IMPACT_ZONE_MIN_PROBABILITY`` or without a finite
    offset and a positive sigma.
    """
    if not (_finite(offset_mm) and _finite(sigma_mm) and sigma_mm > 0.0):
        return None
    group = "woods" if _club_type(club) in IMPACT_ZONE_WOOD_CLUBS else "irons"
    half = IMPACT_ZONE_CENTRE_HALF_WIDTH_MM[group]
    heel = _normal_cdf((-half - offset_mm) / sigma_mm)
    toe = 1.0 - _normal_cdf((half - offset_mm) / sigma_mm)
    probabilities = {"heel": heel, "center": max(0.0, 1.0 - heel - toe), "toe": toe}
    zone = max(IMPACT_ZONES, key=lambda name: probabilities[name])
    return zone if probabilities[zone] >= IMPACT_ZONE_MIN_PROBABILITY else None


def _impact_fields(fields: dict, path, club: Optional[ClubType], teed: bool) -> None:
    """The impact point: D when it passed its gates, else E (rescaled, sigma widened)."""
    point = getattr(path, "impact_point", None)
    impact = path.impact
    height = height_sigma = None
    if point is not None and point.accepted and _finite(point.offset_mm):
        method, offset, sigma = "D", point.offset_mm, point.sigma_mm
        scale = point.scale_mm_per_px
        if teed and club in IMPACT_HEIGHT_CLUBS and _finite(point.height_mm):
            height, height_sigma = point.height_mm, point.height_sigma_mm
    elif impact is not None and impact.accepted and _finite(impact.toe_mm):
        ratio = getattr(point, "e_scale_ratio", None)
        scale = point.scale_mm_per_px if _finite(ratio) else None
        ratio = ratio if _finite(ratio) else 1.0
        method, offset = "E", ratio * impact.toe_mm
        sigma = math.hypot(ratio * (impact.sigma_mm or 0.0), IMPACT_E_FALLBACK_SIGMA_MM)
    else:
        return
    fields["impact_offset_mm"] = _tenth(offset)
    fields["impact_sigma_mm"] = _tenth(sigma)
    # The older name of the same heel/toe value, kept for existing readers.
    fields["impact_toe_mm"] = fields["impact_offset_mm"]
    fields["impact_height_mm"] = _tenth(height)
    fields["impact_height_sigma_mm"] = _tenth(height_sigma)
    fields["impact_zone"] = impact_zone(offset, sigma, club)
    fields["impact_method"] = method
    fields["impact_scale_mm_per_px"] = round(float(scale), 2) if _finite(scale) else None


def calculate_club_data(  # pylint: disable=too-many-arguments,too-many-locals
    result,
    *,
    club: Union[ClubType, str, None],
    launch_direction_deg: Optional[float],
    launch_direction_status: Optional[str],
    launch_vertical_deg: Optional[float],
    camera_vertical_deg: Optional[float],
    roll_calibrated: bool = False,
    handedness: str = "right",
    teed: bool = False,
) -> CalculatedClubData:
    """The calculated club data of one shot (see the module docstring); never raises.

    ``result`` is ``camera.club_head_path.ClubDataResult`` (None when the job did
    not run). ``launch_direction_deg`` / ``launch_direction_status`` are the shot's
    fused start direction and its horizontal fusion status, ``launch_vertical_deg``
    its displayed launch angle and ``camera_vertical_deg`` the same clip's camera
    launch angle (None without an accepted camera estimate). ``roll_calibrated``
    stays False until the plumb boresight exists. ``teed`` is whether the ball sat
    on a tee (the impact height is shown only then, and only for a wood or driver).
    """
    try:
        return _calculate(
            result,
            club=_club_type(club),
            launch_direction_deg=launch_direction_deg,
            launch_direction_status=launch_direction_status,
            launch_vertical_deg=launch_vertical_deg,
            camera_vertical_deg=camera_vertical_deg,
            roll_calibrated=roll_calibrated,
            handedness=handedness,
            teed=teed,
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("[CLUB-DATA] calculation failed; no club data", exc_info=True)
        return CalculatedClubData(dplane_reason="error")


def _calculate(  # pylint: disable=too-many-arguments,too-many-locals
    result,
    *,
    club: Optional[ClubType],
    launch_direction_deg: Optional[float],
    launch_direction_status: Optional[str],
    launch_vertical_deg: Optional[float],
    camera_vertical_deg: Optional[float],
    roll_calibrated: bool,
    handedness: str,
    teed: bool,
) -> CalculatedClubData:
    fields = dict.fromkeys(CLUB_DATA_FIELDS)
    if result is None:
        return CalculatedClubData(fields=fields, dplane_reason="not_run")
    path = result.path
    path_accepted = bool(path.accepted and _finite(path.path_deg))
    if path_accepted:
        fields["camera_club_path_deg"] = _tenth(path.path_deg)
        fields["camera_club_path_sigma_deg"] = _tenth(path.sigma_deg)

    attack = path.attack
    attack_accepted = bool(attack is not None and attack.accepted and _finite(attack.attack_deg))
    loft = None
    if attack_accepted:
        fields["camera_attack_angle_deg"] = _tenth(attack.attack_deg)
        fields["camera_attack_angle_sigma_deg"] = _tenth(attack.sigma_deg)
        if club not in _ATTACK_WITHHELD_CLUBS and _finite(attack.low_point_cm):
            fields["camera_low_point_cm"] = _tenth(attack.low_point_cm)
            fields["camera_low_point_sigma_cm"] = _tenth(attack.low_point_sigma_cm)
        if _finite(camera_vertical_deg):
            loft = calculated_loft(
                club,
                camera_vertical_deg,
                attack.attack_deg,
                attack_sigma_deg=attack.sigma_deg,
                launch_source="camera",
            )
    if loft is not None:
        fields["dynamic_loft_deg"] = _tenth(loft.dynamic_loft_deg)
        fields["dynamic_loft_sigma_deg"] = _tenth(loft.dynamic_loft_sigma_deg)
        fields["spin_loft_deg"] = _tenth(loft.spin_loft_deg)
        fields["spin_loft_sigma_deg"] = _tenth(loft.spin_loft_sigma_deg)

    _impact_fields(fields, path, club, teed)

    face, dplane_reason = None, None
    if not path_accepted:
        dplane_reason = "path_not_accepted"
    elif handedness != "right":
        dplane_reason = "left_handed_unsupported"
    elif not _finite(launch_direction_deg):
        dplane_reason = "no_launch_direction"
    else:
        camera_la = _finite(camera_vertical_deg)
        face = dplane_face(
            club,
            launch_direction_deg=launch_direction_deg,
            launch_direction_sigma_deg=launch_direction_sigma_deg(launch_direction_status),
            club_path_deg=path.path_deg,
            club_path_sigma_deg=path.sigma_deg,
            launch_vertical_deg=camera_vertical_deg if camera_la else launch_vertical_deg,
            launch_vertical_sigma_deg=LAUNCH_VERTICAL_SIGMA_DEG["camera" if camera_la else "radar"],
            attack_angle_deg=attack.attack_deg if attack_accepted else None,
            attack_angle_sigma_deg=attack.sigma_deg if attack_accepted else None,
            roll_sigma_deg=(
                ROLL_SIGMA_DEG_CALIBRATED if roll_calibrated else ROLL_SIGMA_DEG_UNCALIBRATED
            ),
        )
        _face_fields(fields, face, club)
    return CalculatedClubData(fields=fields, loft=loft, face=face, dplane_reason=dplane_reason)


def _face_fields(fields: dict, face: DPlaneFace, club: Optional[ClubType]) -> None:
    """Face to target (within its sigma limit), face to path with its class; the axis for irons."""
    if not face.status.startswith("ok"):
        return
    if face.face_to_target_shown:
        fields["face_to_target_deg"] = _tenth(face.face_to_target_deg)
        fields["face_to_target_sigma_deg"] = _tenth(face.face_to_target_sigma_deg)
    fields["face_to_path_deg"] = _tenth(face.face_to_path_deg)
    fields["face_to_path_sigma_deg"] = _tenth(face.face_to_path_sigma_deg)
    fields["face_to_path_class"] = face.face_to_path_class
    if club not in WOOD_CATEGORIES:
        fields["calculated_spin_axis_deg"] = _tenth(face.spin_axis_deg)
        fields["calculated_spin_axis_sigma_deg"] = _tenth(face.spin_axis_sigma_deg)
        fields["spin_axis_class"] = face.spin_axis_class


__all__ = [
    "CLUB_DATA_FIELDS",
    "IMPACT_METHODS",
    "IMPACT_ZONES",
    "CalculatedClubData",
    "calculate_club_data",
    "impact_zone",
]
