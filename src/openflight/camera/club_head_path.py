"""Club path, attack angle and impact from the pre-contact club-head silhouette.

Experimental, labelled "berechnet". The server runs it in the camera CV worker
after the shot is published (job ``club_data``, ``estimate_club_data``) and puts
accepted values on the shot only as the calculated ``camera_*`` / face / impact
fields (``openflight.club_data``), never into ``club_path_deg``, the K-LD7 D-plane
spin axis, the carry or the simulator export. In the last frames before contact
the head is a compact blob that differs from the pre-swing background: dark
against a bright, partly clipped sky outdoors, possibly bright against a dark
hall indoors. The estimator:

1. takes the contact time as an input (``select_contact_time``: the live camera
   ball-departure fit when physically possible, i.e. not later than the
   trigger minus sound travel, else a gate-free camera departure that agrees
   with the calibrated acoustic estimate, else that acoustic estimate);
2. segments the head in a background-difference image in both polarities,
   opens the thin shaft away and drops pixels whose ray meets the ground
   clearly nearer the camera than the head (most of its backlit shadow); a
   wood's two-tone head (lit crown, dark face) is segmented on the absolute
   difference with a closing that bridges the band between, over three
   closing radii (``WOODS_CLOSE_RADII_BALLS``);
3. picks, among the head-sized candidates of the last frames, the longest
   then smoothest track (centroids close to a quadratic in time, consistent
   areas) that ends at the teed ball; a head must be 4-16 cm wide at its
   kinematic depth and keep that metric width from frame to frame;
4. completes each head where the difference image is blind: behind the teed
   ball (dark when backlit, so a dark head in front of it barely changes the
   image) and in the ground strip. The previous frame's head, scaled by the
   depth ratio and registered on the reliable pixels, fills those pixels;
5. reads a face-centre reference point: the midpoint between the heel and toe
   extents of the lower half of the blob, coverage-weighted for sub-pixel
   edges. It is the point club path refers to; on the 2026-10-09 range clips
   it agrees with the centroid within about 0.5 deg once completed, while
   toe, heel and sole points scatter 2-4x more;
6. back-projects that point with the calibrated lens and the ball-derived
   camera yaw/pitch of ``ball_flight`` (the frame of the camera launch
   direction, so a common aim error cancels in face-to-path) onto the
   vertical plane where OPS club-speed kinematics put the head, its reference
   point sitting ``HEAD_BEHIND_BALL_M`` behind the ball centre at contact;
7. fits lateral position quadratically in time over the last 4-6 frames and
   takes the slope at contact: the swing arc turns the path by roughly
   2 deg per frame, so a chord over the last frames reads about 3 deg off;
8. derives sigma from the fit (residuals with a pixel-noise floor, and a
   calibrated leave-one-frame-out jackknife), the face-centre/centroid
   disagreement and the contact-time uncertainty;
9. fits the swing arc to the same observations for the attack angle and low
   point (``club_attack``), and extrapolates the face centre to contact
   against the ball that left (``departed_ball``) for the heel/toe impact
   (method E, now the fallback); ``camera.impact_point`` measures the impact
   point across contact on the same segmentation (method D, the primary value).

``assess_head_quality`` rates one head region (contrast against its own
surroundings, size, edge width, clipping) so club analysis need not depend on
a global clipping gate that measures the bright background.

Sign: positive path is in-to-out (head moving target-right); positive impact
is toward the toe.
"""

from __future__ import annotations

import itertools
import math
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
from scipy import ndimage

from openflight.camera import departed_ball as departed_ball_module
from openflight.camera.ball_flight import (
    MPH_PER_MS,
    SPEED_OF_SOUND_M_S,
    CameraBallGeometry,
    CameraTimeZero,
    DepartureFit,
    _apply_horizontal_offset,
    _camera_model,
    _CameraModel,
    _locate_reference_ball,
    _normalized,
    fit_ball_departure,
)
from openflight.camera.club_attack import (
    ArcCamera,
    ArcPath,
    AttackAngleResult,
    arc_path_windows,
    estimate_arc_path,
    estimate_attack_angle,
)
from openflight.camera.club_motion import ReferenceBall, detect_reference_ball
from openflight.camera.geometry import camera_to_world, reference_focal_px
from openflight.camera.tee_zone import TeeZone
from openflight.clubs import ClubType
from openflight.dplane import WOOD_CATEGORIES

if TYPE_CHECKING:  # camera.impact_point imports this module's segmentation
    from openflight.camera.impact_point import ImpactPoint

HEAD_BEHIND_BALL_M = 0.035
GROUND_MARGIN_M = 0.02
BALL_ZONE_MARGIN_PX = 2.0
COMPLETION_SEARCH_BALLS = 0.6
COMPLETION_SLACK_PX = 3
FEATURES = ("face_centre", "centroid", "sole", "toe", "heel")
DEFAULT_FEATURE = "face_centre"
_ALTERNATIVE_FEATURE = {name: "centroid" for name in FEATURES} | {"centroid": "face_centre"}
MIN_FIT_FRAMES = 4
MAX_FIT_FRAMES = 6
MAX_SIGMA_DEG = 2.0
MAX_ABS_PATH_DEG = 20.0
CONTACT_GUARD_S = 0.0002
BACKGROUND_FRAMES = 15
BACKGROUND_MIN_FRAMES = 3
BACKGROUND_LEAD_S = 0.06
DIFF_FLOOR = 15.0
NOISE_SIGMAS = 5.0
ROI_HALF_WIDTH_BALLS = 11.0
ROI_UP_BALLS = 11.0
ROI_DOWN_BALLS = 2.0
OPEN_RADIUS_BALLS = 0.18
MIN_AREA_BALLS = 0.6
NEAR_BALL_BALLS = 4.0
MAX_CANDIDATES_PER_FRAME = 3
TRACK_RMS_BALLS = 0.25
AREA_RATIO_RANGE = (0.4, 2.5)
HEAD_WIDTH_RANGE_M = (0.04, 0.16)
WIDTH_CONSISTENCY = 0.2
FEATURE_QUANTILE = 0.05
POSITION_NOISE_FLOOR_PX = 0.3
MIN_HEAD_CONTRAST = 20.0
# A wood's head rated on |frame - background| (``_difference_quality``): under the
# range lamps (2026-11-02) the last frames before contact read about 16 grey
# levels over the dark mat, daylight (2026-10-25) 23-31. Limits 10, 12 and 15 gave
# a plateau of accepted 11-02 paths; 10-25 wood clips darkened to 11-02 contrast kept
# their daylight path within 0.9 deg MAE at 12 (Toptracer not involved). Hybrids,
# though segmented the same way, and every polarity rating keep MIN_HEAD_CONTRAST.
# The same rating picks the frames of the attack arc and of impact E (its last
# frames are the dim ones before contact), and the D-plane face takes the path.
# On the darkened clips, against their own daylight values: attack 0.8 deg MAE
# (mean +0.5), low point 3.7 cm, impact E 1.2-2.8 mm; impact D does not use it.
MIN_WOOD_HEAD_CONTRAST = 12.0
MIN_HEAD_WIDTH_PX = 10.0
MAX_EDGE_WIDTH_PX = 5.0
MAX_HEAD_CLIPPED_PCT = 20.0
CLIP_HIGH = 250
CLIP_LOW = 3
# Contact-time uncertainty per source, as assumed by the offline club-path-v2
# harness, not measured: the departure fits scatter 1.6 ms (sd) around the
# acoustic estimate and nothing yet tells fit error from trigger jitter. Keep
# them until an LED or ball-drop timing calibration exists; tightening them to
# pass the sigma gate would tune the gate to the data. A gate-free camera
# departure is a departure fit like the live one; the calibrated acoustic
# estimate of a teed driver (no mat strike, stamp-to-departure SD 0.34 ms on
# 2026-10-25, path-woods study) is as good as a fit, off the mat it keeps 2 ms.
CONTACT_SIGMA_S = {
    "departure_fit": 0.5e-3,
    "camera_departure": 0.5e-3,
    "acoustic_teed": 0.5e-3,
    "acoustic": 2.0e-3,
}
# A gate-free camera departure is taken when it lies within this of the
# calibrated acoustic estimate (path-woods study, 2026-10-25 drivers/7-woods).
CONTACT_AGREEMENT_MS = 1.5
# Default delay from the trigger stamp's sound bound back to ball contact: the
# median of the 2026-10-25 camera departures before the stamp bound (1.19 ms,
# path-woods study; Toptracer not involved). The session calibrator replaces it
# once ACOUSTIC_DELAY_MIN_SAMPLES teed shots agree.
ACOUSTIC_STAMP_DELAY_MS = 1.20
# The same delay referenced at the kernel edge stamp: provisional, shadow only
# (logged as acoustic_kernel_ms, never decides; plan D10).
ACOUSTIC_EDGE_DELAY_MS = 0.30
ACOUSTIC_DELAY_MIN_SAMPLES = 5
# A delay sample counts only within the live window's lead (DEPARTURE_MAX_LEAD_S).
ACOUSTIC_DELAY_MAX_SAMPLE_MS = 5.0
CONTACT_SOURCES = ("departure_fit", "camera_departure", "acoustic_calibrated")
# Shadow contact time (``shadow_contact_time``, contact-time study 60-kontaktzeit,
# proposal 2): logged only, it never decides. A mat strike sounds before the
# ball leaves, so on 2026-10-25 the acoustic estimate of 9 of 13 mat irons was a
# median 1.68 ms early. Which candidate a refusing reason of
# ``select_contact_time`` would take instead:
CONTACT_SHADOW_CANDIDATES = {
    "departure_fit_after_sound_bound": "live_departure_fit",
    "camera_departure_after_sound_bound": "camera_departure",
}
CONTACT_SHADOW_REASONS = (
    "accepted",
    "teed",
    "not_acoustic",
    "no_candidate",
    "before_acoustic",
    "not_before_stamp",
    "non_finite",
)
# Tee control of the shadow (study erratum E3): a teed ball cannot strike the mat,
# so its departure after the sound bound is a fit or clock error, not a mat strike.
# The shadow may only decide (later, after a range test) while at most
# TEE_CONTROL_MAX_LATE_SHARE of at least TEE_CONTROL_MIN_TEED teed departures lie
# more than TEE_CONTROL_LATE_MS after their sound bound (0.3 ms: the robust spread
# of the 2026-10-25 teed woods, 0.27 ms, rounded up).
TEE_CONTROL_LATE_MS = 0.3
TEE_CONTROL_MIN_TEED = 5
TEE_CONTROL_MAX_LATE_SHARE = 0.10

# Clubs whose head is segmented on |frame - background| (plan D12): a wood's lit
# crown is brighter than the mat, its face darker, and the band between them
# matches the background, so one polarity cuts the head apart (2026-10-25).
# The same clubs are the ones that may be teed: their club ball never anchors
# on the tapped mark (``locate_club_ball``).
WOODS_SEGMENTATION = frozenset(
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
# Woods-mode closing radii (balls), the ensemble of the path-woods study.
WOODS_CLOSE_RADII_BALLS = (0.25, 0.30, 0.35)
SEGMENTATIONS = ("polarity", "woods")
HANDEDNESS = ("right", "left")

# Teed-ball refinement on the pre-swing background (path-woods study ball_fix).
REFINE_BACKGROUND_FRAMES = 20
REFINE_SEARCH_MIN_PX = 25.0
REFINE_SEARCH_DIAMETERS = 1.5
REFINE_RING_PX = (1.0, 4.0)
# The ring counts only above this fraction of the radius below the centre: the
# tee and the ball's shadow hang below.
REFINE_RING_BELOW_RADII = 0.3
REFINE_MIN_CONTRAST = 15.0
# The edge-circle centre may sit this far (diameters) from the template peak.
REFINE_MAX_EDGE_SHIFT_DIAMETERS = 0.25
# Without a departed ball the session tracker's ball stands only within this many
# refine search radii (``refine_search_radius_px``: max(25 px, 1.5 ball diameters),
# 25 px at the range's 16.4 px ball, so 50 px) of the tapped ball mark: one radius for
# the tap itself (a fingertip lands within ~22 frame px of the ball, ``departed_ball``)
# and one for a ball teed above a tap on the ground (the highest profile tee,
# ``profiles.MAX_DRIVER_TEE_HEIGHT_M`` = 8 cm, is ~31 px at 0.38 px/mm). The tree-line
# blob and the ball tray a detection found instead of the irons' ball (2026-11-02 S1)
# lie ~85-115 px off. A refine started at the mark
# never leaves the bound (its search box plus the edge shift, ~44 px), so it bounds
# only the tracker's ball and that ball's refine.
CLUB_BALL_MARK_MAX_SEARCH_RADII = 2.0

# Heel/toe impact (impact study, 2026-10-25 range).
IMPACT_FEATURE = "face_centre"
IMPACT_MAX_GAP_MS = 4.0
IMPACT_MAX_ABS_MM = 50.0
# Absolute plausible visible head length, heel to toe. The upper bound is the
# physical limit of a driver head (USGA: at most 5 in = 127 mm); with woods
# segmentation a driver reads ~91-104 mm, so the old 95 mm cap rejected half of
# them. Once the club has a session median (server, n >= 5), the +/-20 % band
# around it decides inside this range; the median learns from every head length
# inside it (``head_length_plausible``), not only from accepted impacts.
IMPACT_HEAD_LENGTH_RANGE_MM = (50.0, 127.0)
IMPACT_HEAD_LENGTH_TOLERANCE = 0.20
# Per-shot edge noise of the extrapolated face centre: half the SD of the head
# length at contact (impact study budget, 2.4-3.4 mm), rounded up.
IMPACT_EDGE_SIGMA_MM = 3.0
IMPACT_BALL_SIGMA_MM = 0.5
IMPACT_MAX_SIGMA_MM = 10.0
IMPACT_FIT_FRAMES = 6
# Search window (px) around each reference for the ball that left: half width,
# above, below (impact study ball_verify).
DEPARTED_BALL_WINDOW_PX = (90, 90, 40)


@dataclass(frozen=True)
class ContactTime:  # pylint: disable=too-many-instance-attributes
    """Club contact time on the frame clock, and why it came from its source.

    ``t0_ms``, ``departure_fit_ms``, ``sound_bound_ms``, ``edge_sound_bound_ms``,
    ``camera_departure_ms``, ``acoustic_ms`` and ``acoustic_kernel_ms`` are ms from
    the trigger stamp. ``sound_bound_ms`` is the trigger stamp minus the
    tee-to-microphone sound travel. The stamp lags the sound edge by
    ``trigger_lag_ms`` (callback dispatch, waits for locks); where that lag is
    known, ``edge_sound_bound_ms`` is the same bound taken at the edge, the latest
    moment impact can physically have happened if the edge stamp is right; both
    are None when it is not. ``edge_bound_verdict`` says whether a departure fit
    the live window took lies after that edge bound; it is logged for offline
    analysis and does not decide the source (see ``select_contact_time``).

    ``acoustic_ms`` is the calibrated acoustic estimate, ``sound_bound_ms`` minus
    ``acoustic_delay_ms`` (``acoustic_delay_source``: ``default`` or the
    ``session`` calibration); it is referenced at the stamp
    (``acoustic_reference``). ``acoustic_kernel_ms`` is the same estimate
    referenced at the kernel edge stamp, shadow only. ``camera_departure_ms`` is
    the gate-free camera departure (``estimate_departure_contact``) and
    ``departure_agreement_ms`` its offset from ``acoustic_ms``.
    """

    time_s: float
    source: str
    sigma_s: float
    t0_ms: float
    reason: str
    departure_fit_ms: float | None
    sound_bound_ms: float
    trigger_lag_ms: float | None = None
    edge_sound_bound_ms: float | None = None
    edge_bound_verdict: str | None = None
    camera_departure_ms: float | None = None
    departure_agreement_ms: float | None = None
    acoustic_ms: float | None = None
    acoustic_reference: str = "stamp"
    acoustic_delay_ms: float = ACOUSTIC_STAMP_DELAY_MS
    acoustic_delay_source: str = "default"
    acoustic_kernel_ms: float | None = None


def _live_reason(time_zero: CameraTimeZero | None, sound_bound_ms: float) -> str:
    """``departure_fit`` when the live fit is usable, else why it is not."""
    if time_zero is None:
        return "no_camera_time_zero"
    if time_zero.source != "departure_fit":
        return (
            "no_departure_fit"
            if time_zero.departure_fit_ms is None
            else "departure_fit_outside_live_window"
        )
    if time_zero.time_zero_ms > sound_bound_ms:
        return "departure_fit_after_sound_bound"
    return "departure_fit"


def select_contact_time(  # pylint: disable=too-many-arguments,too-many-locals
    time_zero: CameraTimeZero | None,
    *,
    trigger_ns: int,
    tee_range_m: float,
    trigger_lag_ms: float | None = None,
    lag_reference: str | None = None,
    camera_departure_ms: float | None = None,
    teed: bool = False,
    acoustic_delay_ms: float | None = None,
) -> ContactTime:
    """Contact time from the live departure fit, a camera departure or the acoustic estimate.

    ``time_zero`` is the ball estimator's selection (``CameraBallEstimate.time_zero``,
    None when it stopped before one): the departure fit when it lies within
    [acoustic - 5 ms, min(acoustic + 1.5 ms, trigger)], else the acoustic
    estimate, trigger minus sound travel. That window stays as it is. Impact
    cannot follow the moment its sound left the tee, though: a departure fit
    later than the trigger minus tee range / speed of sound is a bad fit or a
    trigger from an earlier sound. The cascade:

    1. the live departure fit, when the live window took it and it is not after
       that stamp sound bound (``departure_fit``, sigma 0.5 ms);
    2. else ``camera_departure_ms``, the gate-free camera departure, when it is
       not after the sound bound and within ``CONTACT_AGREEMENT_MS`` of the
       calibrated acoustic estimate (``camera_departure``, sigma 0.5 ms);
    3. else the calibrated acoustic estimate, the sound bound minus
       ``acoustic_delay_ms`` (``ACOUSTIC_STAMP_DELAY_MS`` without a session
       calibration): ``acoustic_calibrated``, sigma 0.5 ms for a ``teed`` ball,
       else 2 ms, widened to the camera departure's disagreement when one was
       refused (``camera_departure_conflict``,
       ``camera_departure_after_sound_bound``).

    Without a camera departure the reason says why the live fit was not used.
    The bound that decides is taken at the trigger stamp, so a refused fit is
    never replaced by a later contact. When ``trigger_lag_ms`` (how long the
    stamp followed the edge, >= 0) is known, the same bound taken at the edge is
    only judged and logged (``edge_bound_verdict``); with ``lag_reference``
    ``kernel`` the acoustic estimate at the edge is logged as
    ``acoustic_kernel_ms`` (edge bound minus ``ACOUSTIC_EDGE_DELAY_MS``). Neither
    decides: the edge stamps are in shadow (plan D10).
    """
    sound_bound_ms = -1e3 * tee_range_m / SPEED_OF_SOUND_M_S
    if trigger_lag_ms is not None and not (math.isfinite(trigger_lag_ms) and trigger_lag_ms >= 0):
        raise ValueError(f"trigger_lag_ms must be finite and >= 0, got {trigger_lag_ms!r}")
    if acoustic_delay_ms is not None and not math.isfinite(acoustic_delay_ms):
        raise ValueError(f"acoustic_delay_ms must be finite, got {acoustic_delay_ms!r}")
    if camera_departure_ms is not None and not math.isfinite(camera_departure_ms):
        camera_departure_ms = None
    edge_sound_bound_ms = None if trigger_lag_ms is None else sound_bound_ms - trigger_lag_ms
    departure_fit_ms = None if time_zero is None else time_zero.departure_fit_ms
    delay_ms, delay_source = (
        (ACOUSTIC_STAMP_DELAY_MS, "default")
        if acoustic_delay_ms is None
        else (float(acoustic_delay_ms), "session")
    )
    acoustic_ms = sound_bound_ms - delay_ms
    reason = _live_reason(time_zero, sound_bound_ms)
    edge_bound_verdict = None
    if (
        edge_sound_bound_ms is not None
        and time_zero is not None
        and time_zero.source == "departure_fit"
    ):
        edge_bound_verdict = (
            "departure_fit_after_edge_sound_bound"
            if time_zero.time_zero_ms > edge_sound_bound_ms
            else "departure_fit_before_edge_sound_bound"
        )
    agreement_ms = None if camera_departure_ms is None else camera_departure_ms - acoustic_ms
    acoustic_sigma_s = CONTACT_SIGMA_S["acoustic_teed" if teed else "acoustic"]
    if reason == "departure_fit":
        source, t0_ms, sigma_s = "departure_fit", time_zero.time_zero_ms, CONTACT_SIGMA_S[reason]
    elif agreement_ms is None:
        source, t0_ms, sigma_s = "acoustic_calibrated", acoustic_ms, acoustic_sigma_s
    elif camera_departure_ms <= sound_bound_ms and abs(agreement_ms) <= CONTACT_AGREEMENT_MS:
        source, t0_ms = "camera_departure", camera_departure_ms
        sigma_s, reason = CONTACT_SIGMA_S["camera_departure"], "camera_departure"
    else:
        reason = (
            "camera_departure_after_sound_bound"
            if camera_departure_ms > sound_bound_ms
            else "camera_departure_conflict"
        )
        source, t0_ms = "acoustic_calibrated", acoustic_ms
        sigma_s = max(acoustic_sigma_s, abs(agreement_ms) / 1e3)
    return ContactTime(
        time_s=trigger_ns / 1e9 + t0_ms / 1e3,
        source=source,
        sigma_s=sigma_s,
        t0_ms=t0_ms,
        reason=reason,
        departure_fit_ms=departure_fit_ms,
        sound_bound_ms=sound_bound_ms,
        trigger_lag_ms=trigger_lag_ms,
        edge_sound_bound_ms=edge_sound_bound_ms,
        edge_bound_verdict=edge_bound_verdict,
        camera_departure_ms=camera_departure_ms,
        departure_agreement_ms=agreement_ms,
        acoustic_ms=acoustic_ms,
        acoustic_delay_ms=delay_ms,
        acoustic_delay_source=delay_source,
        acoustic_kernel_ms=(
            edge_sound_bound_ms - ACOUSTIC_EDGE_DELAY_MS
            if lag_reference == "kernel" and edge_sound_bound_ms is not None
            else None
        ),
    )


@dataclass(frozen=True)
class ContactShadow:
    """The shadow contact time of one clip (``shadow_contact_time``): logged, never used.

    ``reason`` is one of ``CONTACT_SHADOW_REASONS``. ``source`` names the
    candidate that was judged (``live_departure_fit`` or ``camera_departure``),
    also when the guard refused it; None when there was none to judge
    (``teed``, ``not_acoustic``, ``no_candidate``). ``t0_ms`` (ms from the
    trigger stamp), ``sigma_s`` and ``minus_t0_ms`` (``t0_ms`` minus the contact
    the job used) are set only when ``accepted``.
    """

    reason: str
    source: str | None = None
    t0_ms: float | None = None
    sigma_s: float | None = None
    minus_t0_ms: float | None = None

    def to_log(self) -> dict:
        """The ``t0_shadow_*`` fields of the ``camera_club_head_path`` entry."""
        return {
            "t0_shadow_ms": self.t0_ms,
            "t0_shadow_source": self.source,
            "t0_shadow_reason": self.reason,
            "t0_shadow_minus_t0_ms": self.minus_t0_ms,
        }


def shadow_contact_time(  # pylint: disable=too-many-return-statements
    contact: ContactTime, time_zero: CameraTimeZero | None, *, teed: bool
) -> ContactShadow:
    """What the mat-strike rule (study 60-kontaktzeit, proposal 2) would take as contact.

    ``contact`` is ``select_contact_time``'s result as it returned it (before
    ``estimate_club_data`` renames a reason to ``no_camera_departure``). Only a
    shot that is not ``teed`` (the server's driver shot, the job's ``teed``, not
    ``ball_teed``) whose contact is ``acoustic_calibrated`` because
    its departure lay after the sound bound gets a candidate
    (``CONTACT_SHADOW_CANDIDATES``): reason ``departure_fit_after_sound_bound``
    takes the live fit (``time_zero.time_zero_ms``),
    ``camera_departure_after_sound_bound`` the camera departure. The guard is
    one-sided (study erratum E4: a mat strike only makes the sound edge early):
    accepted only when ``acoustic_ms <= candidate < 0`` (not before the acoustic
    estimate, before the stamp), with the departure fit's sigma (0.5 ms).

    Shadow only: the contact the job uses never changes. Whether it ever may
    decide waits for a range test and the session's tee control
    (``TeeControl``).
    """
    if teed:
        return ContactShadow("teed")
    if contact.source != "acoustic_calibrated":
        return ContactShadow("not_acoustic")
    source = CONTACT_SHADOW_CANDIDATES.get(contact.reason)
    if source == "live_departure_fit":
        candidate_ms = None if time_zero is None else time_zero.time_zero_ms
    elif source == "camera_departure":
        candidate_ms = contact.camera_departure_ms
    else:
        return ContactShadow("no_candidate")
    if candidate_ms is None:
        return ContactShadow("no_candidate")
    # An acoustic contact's t0 is its acoustic estimate.
    acoustic_ms = contact.acoustic_ms
    candidate_ms = float(candidate_ms)
    if acoustic_ms is None or not (math.isfinite(candidate_ms) and math.isfinite(acoustic_ms)):
        return ContactShadow("non_finite", source)
    if candidate_ms < acoustic_ms:
        return ContactShadow("before_acoustic", source)
    if candidate_ms >= 0.0:
        return ContactShadow("not_before_stamp", source)
    return ContactShadow(
        "accepted",
        source,
        t0_ms=candidate_ms,
        sigma_s=CONTACT_SIGMA_S["departure_fit"],
        minus_t0_ms=candidate_ms - contact.t0_ms,
    )


class TeeControl:
    """Session count of teed departures and how many lie late after their sound bound.

    The shadow contact time's precondition (study erratum E3, ``K-T``): a teed
    ball cannot strike the mat, so in a session where teed balls leave after
    their own sound bound a late departure is no sign of a mat strike
    (2026-11-02: 6 of 10 teed drivers more than 0.3 ms late; 2026-10-25: 0 of
    18). Teed is the server's driver shot (the job's ``teed``), not ``ball_teed``.
    A teed shot counts with its live departure fit (``departure_fit_ms``,
    inside the live window or not, when finite), else its camera departure; late
    is more than ``TEE_CONTROL_LATE_MS`` after the sound bound. Fed with every
    contact the session's ``AcousticDelayCalibrator`` sees, and reset with it.
    """

    def __init__(self) -> None:
        self.n_teed = 0
        self.n_late = 0

    def add_contact(self, contact: ContactTime | None, *, teed: bool) -> bool:
        """Count one contact's departure; returns whether it counted."""
        if not teed or contact is None:
            return False
        departure_ms = (
            contact.departure_fit_ms
            if contact.departure_fit_ms is not None and math.isfinite(contact.departure_fit_ms)
            else contact.camera_departure_ms
        )
        if departure_ms is None:
            return False
        late_ms = departure_ms - contact.sound_bound_ms
        if not math.isfinite(late_ms):
            return False
        self.n_teed += 1
        if late_ms > TEE_CONTROL_LATE_MS:
            self.n_late += 1
        return True

    def passes(self) -> bool:
        """At least ``TEE_CONTROL_MIN_TEED`` teed departures, at most 10 % of them late."""
        return (
            self.n_teed >= TEE_CONTROL_MIN_TEED
            and self.n_late / self.n_teed <= TEE_CONTROL_MAX_LATE_SHARE
        )

    def state(self) -> tuple[int, int, bool]:
        """(teed departures, late ones, whether the control passes)."""
        return self.n_teed, self.n_late, self.passes()


class AcousticDelayCalibrator:
    """Session median of the delay from the stamp sound bound back to ball contact.

    The trigger handling (gate, OPS re-arm) can move the delay between the sound
    edge and its stamp, so the session measures it on its own teed shots: each
    adds sound bound minus a live or camera departure that is not after the
    bound and at most ``ACOUSTIC_DELAY_MAX_SAMPLE_MS`` before it. Teed shots
    only: a mat strike sounds before the ball leaves. One instance per session.
    Its ``tee_control`` (``TeeControl``) counts the same contacts for the shadow
    contact time; it never changes the delay.
    """

    def __init__(self, min_samples: int = ACOUSTIC_DELAY_MIN_SAMPLES):
        self.min_samples = min_samples
        self._delays_ms: list[float] = []
        self.tee_control = TeeControl()

    def add(self, sound_bound_ms: float, departure_ms: float | None, *, teed: bool) -> bool:
        """Learn one shot's delay; returns whether it counted."""
        if not teed or departure_ms is None:
            return False
        delay = sound_bound_ms - departure_ms
        if not math.isfinite(delay) or not 0.0 <= delay <= ACOUSTIC_DELAY_MAX_SAMPLE_MS:
            return False
        self._delays_ms.append(float(delay))
        return True

    def add_contact(self, contact: ContactTime | None, *, teed: bool) -> bool:
        """Learn from a contact time: its live departure fit, else its camera departure.

        The tee control counts the contact too; the return value is only
        whether it became a delay sample.
        """
        self.tee_control.add_contact(contact, teed=teed)
        if contact is None:
            return False
        departure = (
            contact.t0_ms if contact.source == "departure_fit" else contact.camera_departure_ms
        )
        return self.add(contact.sound_bound_ms, departure, teed=teed)

    def delay(self) -> tuple[float, str, int]:
        """(delay ms, ``default`` or ``session``, samples): the median from min_samples on."""
        count = len(self._delays_ms)
        if count < self.min_samples:
            return ACOUSTIC_STAMP_DELAY_MS, "default", count
        return float(statistics.median(self._delays_ms)), "session", count

    def session_delay_ms(self) -> float | None:
        """The calibrated delay for ``select_contact_time``, None while it is the default."""
        delay_ms, source, _count = self.delay()
        return delay_ms if source == "session" else None


@dataclass(frozen=True)
class HeadQuality:
    """Head-referenced image quality of one frame's club-head region.

    ``edge_width_px`` is contrast over the 90th-percentile central-difference
    gradient on the head outline: about 2 for a sharp edge, larger with blur.
    """

    status: str
    contrast: float
    area_px: int
    width_px: float
    edge_width_px: float
    clipped_pct: float

    @property
    def ok(self) -> bool:
        """Whether the head region is good enough for a position measurement."""
        return self.status == "ok"


@dataclass(frozen=True)
class HeadObservation:
    """One tracked head silhouette and its back-projection."""

    frame_index: int
    t_rel_ms: float
    features_px: dict[str, tuple[float, float]]
    quality: HeadQuality
    width_m: float | None = None
    lateral_m: float | None = None
    forward_m: float | None = None
    residual_mm: float | None = None


@dataclass(frozen=True)
class ClubHeadPathResult:
    """Club path at contact, its uncertainty and per-frame diagnostics."""

    status: str
    path_deg: float | None = None
    sigma_deg: float | None = None
    n_frames: int = 0
    feature: str = DEFAULT_FEATURE
    polarity: str | None = None
    fit_sigma_deg: float | None = None
    jackknife_sigma_deg: float | None = None
    feature_sigma_deg: float | None = None
    contact_sigma_deg: float | None = None
    feature_paths_deg: dict[str, float] = field(default_factory=dict)
    observations: tuple[HeadObservation, ...] = ()
    segmentation: str = "polarity"
    segmentation_sigma_deg: float | None = None
    segmentation_paths_deg: dict[str, float] = field(default_factory=dict)
    ball_source: str | None = None
    ball_px: tuple[float, float, float] | None = None
    attack: AttackAngleResult | None = None
    impact: ImpactLocation | None = None
    # The impact point measured across contact (method D, ``camera.impact_point``).
    impact_point: ImpactPoint | None = None
    # The path from the arc fit, logged only: nothing shown or calculated reads it
    # (70-bogenpfad). Same gates and woods ensemble as ``path_deg``; the radii and the
    # windows (without the last / first usable frame) are its diagnostics.
    path_arc_status: str = "not_run"
    path_arc_deg: float | None = None
    path_arc_sigma_deg: float | None = None
    path_arc_radii_deg: dict[str, float] = field(default_factory=dict)
    path_arc_window_deg: dict[str, float] = field(default_factory=dict)
    # The background's grey-level noise in the path's ROI, logged only (``background_noise_sigma``).
    noise_sigma: float | None = None

    @property
    def accepted(self) -> bool:
        """Whether the path passed every gate."""
        return self.status == "accepted"

    @property
    def last_frame_gap_ms(self) -> float | None:
        """ms from the last usable (``ok``) path frame to contact, None without one.

        Logged only (``path_last_frame_gap_ms``): the iron-path study
        (50-eisen-pfad-kontrast) asks for it to tell a long extrapolation from
        the phase (contact late between two frames) from one from lost frames.
        """
        usable = [item for item in self.observations if item.quality.ok]
        return round(-float(usable[-1].t_rel_ms), 3) if usable else None


@dataclass(frozen=True)
class ConfirmedBall:
    """The ball seen leaving (``departed_ball``) near one of the impact's references.

    ``reference_index`` is that reference's position in the references searched
    and ``shift_px`` how far the ball lay from it.
    """

    x: float
    y: float
    diameter_px: float
    reference_index: int
    shift_px: float
    snr: float | None = None
    teed: bool | None = None


@dataclass(frozen=True)
class ImpactLocation:  # pylint: disable=too-many-instance-attributes
    """Heel/toe impact offset along the face at contact (mm, + toward the toe).

    ``sigma_mm`` is the quadrature of the fit (larger of residual and calibrated
    jackknife sigma of the intercept), the edge noise, the contact-time term
    |lateral speed| * sigma_t0 and the ball position. ``ball_reference`` names
    the ball the offset is measured against: ``given`` (the ball flight's
    departed ball), ``ball`` (found leaving near the club ball) or ``tee`` (near
    the session's confirmed tee). ``ball_shift_mm`` is that ball's lateral
    offset from the club ball, toe-signed. ``hosel_vote`` is the shaft's side
    vote (image x, + right); it only checks the handedness, which sets the toe
    side. ``variants_mm`` are diagnostics only.
    """

    status: str
    toe_mm: float | None = None
    sigma_mm: float | None = None
    fit_sigma_mm: float | None = None
    edge_sigma_mm: float = IMPACT_EDGE_SIGMA_MM
    contact_sigma_mm: float | None = None
    ball_sigma_mm: float = IMPACT_BALL_SIGMA_MM
    feature: str = IMPACT_FEATURE
    head_length_mm: float | None = None
    extrapolation_gap_ms: float | None = None
    lateral_speed_mm_per_ms: float | None = None
    ball_reference: str | None = None
    ball_px: tuple[float, float, float] | None = None
    ball_shift_mm: float | None = None
    handedness: str = "right"
    hosel_vote: float | None = None
    variants_mm: dict[str, float] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        """Whether the impact passed every gate."""
        return self.status == "accepted"

    def to_dict(self) -> dict:
        """JSON-ready form for the session log."""
        return {
            "status": self.status,
            "toe_mm": self.toe_mm,
            "sigma_mm": self.sigma_mm,
            "fit_sigma_mm": self.fit_sigma_mm,
            "edge_sigma_mm": self.edge_sigma_mm,
            "contact_sigma_mm": self.contact_sigma_mm,
            "ball_sigma_mm": self.ball_sigma_mm,
            "feature": self.feature,
            "head_length_mm": self.head_length_mm,
            "extrapolation_gap_ms": self.extrapolation_gap_ms,
            "lateral_speed_mm_per_ms": self.lateral_speed_mm_per_ms,
            "ball_reference": self.ball_reference,
            "ball_px": None if self.ball_px is None else list(self.ball_px),
            "ball_shift_mm": self.ball_shift_mm,
            "handedness": self.handedness,
            "hosel_vote": self.hosel_vote,
            "variants_mm": dict(self.variants_mm),
        }


@dataclass(frozen=True)
class _Blob:
    index: int
    xs: np.ndarray
    ys: np.ndarray
    weights: np.ndarray
    centroid: tuple[float, float]
    area: int
    width_px: float
    head_mask: np.ndarray
    thin_mask: np.ndarray
    origin: tuple[int, int]


@dataclass(frozen=True)
class _Scene:
    """Per-clip constants shared by segmentation, tracking and back-projection."""

    times_s: np.ndarray
    contact_time_s: float
    origin: tuple[int, int]
    ball: ReferenceBall
    ball_px: float
    model: _CameraModel
    geometry: CameraBallGeometry
    forward_speed_ms: float
    ground_forward_m: np.ndarray
    ball_zone: np.ndarray

    def head_forward_m(self, index: int) -> float:
        """Kinematic forward position of the head's reference point in a frame."""
        elapsed = self.times_s[index] - self.contact_time_s
        return self.geometry.ball_forward_m - HEAD_BEHIND_BALL_M + self.forward_speed_ms * elapsed

    def metres_per_px(self, index: int) -> float:
        """Image scale at the head's kinematic depth."""
        depth = self.head_forward_m(index) - self.geometry.camera_forward_offset_m
        return depth / self.model.focal_px

    def below_ground(self, index: int) -> np.ndarray:
        """ROI pixels whose ray meets the ground nearer the camera than the head can be."""
        return self.ground_forward_m < self.head_forward_m(index) - GROUND_MARGIN_M


@dataclass(frozen=True)
class _Fit:
    path_deg: float
    sigma_deg: float
    fit_sigma_deg: float
    jackknife_sigma_deg: float
    contact_gain_deg_per_s: float
    residuals_m: np.ndarray


def _disk(radius: int) -> np.ndarray:
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    return xx * xx + yy * yy <= radius * radius


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    """Quantile of integer pixel coordinates, each pixel spread over its own width.

    Treating a pixel as a box makes the quantile continuous, so the extents
    move sub-pixel with the soft edge weights instead of snapping to columns.
    """
    unique, inverse = np.unique(values, return_inverse=True)
    mass = np.bincount(inverse, weights=weights)
    cumulative = np.cumsum(mass)
    target = quantile * cumulative[-1]
    index = min(int(np.searchsorted(cumulative, target)), len(unique) - 1)
    below = cumulative[index] - mass[index]
    fraction = (target - below) / mass[index] if mass[index] > 0.0 else 0.5
    return float(unique[index] - 0.5 + fraction)


@dataclass(frozen=True)
class HeadQualityLimits:
    """Thresholds of ``assess_head_quality``."""

    min_contrast: float = MIN_HEAD_CONTRAST
    min_width_px: float = MIN_HEAD_WIDTH_PX
    max_edge_width_px: float = MAX_EDGE_WIDTH_PX
    max_clipped_pct: float = MAX_HEAD_CLIPPED_PCT


WOOD_DIFFERENCE_LIMITS = HeadQualityLimits(min_contrast=MIN_WOOD_HEAD_CONTRAST)


def assess_head_quality(
    frame: np.ndarray, head_mask: np.ndarray, limits: HeadQualityLimits = HeadQualityLimits()
) -> HeadQuality:
    """Rate the head region itself: contrast to its surroundings, size, blur, clipping.

    ``head_mask`` is a boolean mask the shape of ``frame`` (or of the same crop).
    A white sky behind a dark head clips the background, not the head, so this
    check stays valid where a frame-wide clipping gate rejects the clip.
    """
    image = np.asarray(frame, dtype=np.float32)
    mask = np.asarray(head_mask, dtype=bool)
    area = int(np.count_nonzero(mask))
    if image.shape != mask.shape or area == 0:
        return HeadQuality("not_found", 0.0, area, 0.0, math.inf, 0.0)
    ring = ndimage.binary_dilation(mask, _disk(4)) & ~ndimage.binary_dilation(mask, _disk(1))
    outline = ndimage.binary_dilation(mask, _disk(1)) & ~ndimage.binary_erosion(mask, _disk(1))
    head_pixels = image[mask]
    contrast = (
        abs(float(np.median(head_pixels)) - float(np.median(image[ring]))) if ring.any() else 0.0
    )
    columns = np.nonzero(mask.any(axis=0))[0]
    width = float(columns[-1] - columns[0] + 1)
    grad_y, grad_x = np.gradient(image)
    gradient = np.hypot(grad_x, grad_y)[outline]
    steepest = float(np.percentile(gradient, 90)) if gradient.size else 0.0
    edge_width = contrast / steepest if steepest > 0.0 else math.inf
    clipped = float(np.mean((head_pixels >= CLIP_HIGH) | (head_pixels <= CLIP_LOW)) * 100.0)
    if contrast < limits.min_contrast:
        status = "low_contrast"
    elif width < limits.min_width_px:
        status = "too_small"
    elif edge_width > limits.max_edge_width_px:
        status = "blurred"
    elif clipped > limits.max_clipped_pct:
        status = "clipped"
    else:
        status = "ok"
    return HeadQuality(
        status=status,
        contrast=round(contrast, 1),
        area_px=area,
        width_px=width,
        edge_width_px=round(edge_width, 2),
        clipped_pct=round(clipped, 1),
    )


def _roi(ball: ReferenceBall, ball_px: float, shape: tuple[int, int]) -> tuple[int, int, int, int]:
    height, width = shape
    return (
        max(0, int(round(ball.x - ROI_HALF_WIDTH_BALLS * ball_px))),
        max(0, int(round(ball.y - ROI_UP_BALLS * ball_px))),
        min(width, int(round(ball.x + ROI_HALF_WIDTH_BALLS * ball_px)) + 1),
        min(height, int(round(ball.y + ROI_DOWN_BALLS * ball_px)) + 1),
    )


def _ground_forward(
    roi: tuple[int, int, int, int], model: _CameraModel, geometry: CameraBallGeometry
) -> np.ndarray:
    """Forward distance where each ROI pixel's ray meets the ground (inf above the horizon)."""
    x0, y0, x1, y1 = roi
    ys, xs = np.mgrid[y0:y1, x0:x1]
    image_x, image_z = _normalized(
        xs.ravel().astype(float),
        ys.ravel().astype(float),
        focal_px=model.focal_px,
        geometry=geometry,
    )
    _lateral, forward, vertical = camera_to_world(
        image_x, 1.0, image_z, pitch_rad=model.pitch_rad, yaw_rad=model.yaw_rad
    )
    # The floor, not the ball's bottom: a teed ball sits ``tee_height_m`` above it.
    floor_m = geometry.ball_height_m - geometry.ball_diameter_m / 2.0 - geometry.tee_height_m
    drop = floor_m - geometry.camera_height_m
    descending = vertical < 0.0
    scale = np.divide(drop, vertical, out=np.full(vertical.shape, np.inf), where=descending)
    ground = np.where(descending, geometry.camera_forward_offset_m + scale * forward, np.inf)
    return ground.reshape(ys.shape)


def _candidates(
    signed: np.ndarray,
    threshold: float,
    *,
    index: int,
    scene: _Scene,
    close_radius_balls: float = 0.0,
) -> list[_Blob]:
    """Head-sized components of one frame's change mask, shaft and shadow removed.

    A head in front of a ball of its own brightness leaves a ball-shaped gap,
    so the ball zone connects components without joining them (completion
    later fills those pixels). With ``close_radius_balls`` (woods mode) the mask
    is closed and filled first: a two-tone head (lit crown brighter, face darker
    than the mat) crosses the background level in between.
    """
    below_ground = scene.below_ground(index)
    raw = (signed > threshold) & ~below_ground
    if close_radius_balls > 0.0:
        closed = ndimage.binary_closing(
            raw, structure=_disk(max(1, round(close_radius_balls * scene.ball_px)))
        )
        raw = ndimage.binary_fill_holes(closed) & ~below_ground
    opened = ndimage.binary_opening(
        raw, structure=_disk(max(2, round(OPEN_RADIUS_BALLS * scene.ball_px)))
    )
    thin = raw & ~opened
    labels, _count = ndimage.label(opened | scene.ball_zone)
    min_area = MIN_AREA_BALLS * math.pi * (scene.ball_px / 2.0) ** 2
    width_range_px = [limit / scene.metres_per_px(index) for limit in HEAD_WIDTH_RANGE_M]
    height, width = signed.shape
    blobs = []
    for label, region in enumerate(ndimage.find_objects(labels), start=1):
        rows, cols = region
        if rows.start == 0 or cols.start == 0 or rows.stop == height or cols.stop == width:
            continue
        piece = (labels[region] == label) & opened[region]
        if not piece.any():
            continue
        component = ndimage.binary_fill_holes(piece)
        area = int(np.count_nonzero(component))
        blob_width = float(cols.stop - cols.start)
        if area < min_area or not width_range_px[0] <= blob_width <= width_range_px[1]:
            continue
        head = np.zeros_like(raw)
        head[region] = component
        soft = np.clip(signed / max(float(np.median(signed[head])), 1e-6), 0.0, 1.0)
        blobs.append(_make_blob(index, soft, head, thin, scene.origin))
    return blobs


def _make_blob(
    index: int, soft: np.ndarray, head: np.ndarray, thin: np.ndarray, origin: tuple[int, int]
) -> _Blob:
    """Blob with full weight inside ``head`` and soft coverage on a one-pixel rim."""
    edge = ndimage.binary_dilation(head, _disk(1)) & ~head
    ys, xs = np.nonzero(head | edge)
    weights = np.where(head[ys, xs], 1.0, soft[ys, xs])
    keep = weights > 0.0
    xs = (xs[keep] + origin[0]).astype(float)
    ys = (ys[keep] + origin[1]).astype(float)
    weights = weights[keep]
    columns = np.nonzero(head.any(axis=0))[0]
    return _Blob(
        index=index,
        xs=xs,
        ys=ys,
        weights=weights,
        centroid=(float(np.average(xs, weights=weights)), float(np.average(ys, weights=weights))),
        area=int(np.count_nonzero(head)),
        width_px=float(columns[-1] - columns[0] + 1),
        head_mask=head,
        thin_mask=thin,
        origin=origin,
    )


def _nearest_pixel_distance(blob: _Blob, point: tuple[float, float]) -> float:
    return float(np.min(np.hypot(blob.xs - point[0], blob.ys - point[1])))


def _smoothness_px(track: list[_Blob], times_s: np.ndarray) -> float:
    """RMS distance (px) of the track's centroids from a quadratic in time."""
    if len(track) <= 3:
        return 0.0
    elapsed = np.asarray([times_s[blob.index] for blob in track]) * 1000.0
    elapsed -= elapsed[-1]
    design = np.column_stack((np.ones_like(elapsed), elapsed, elapsed**2))
    centroids = np.asarray([blob.centroid for blob in track])
    residuals = centroids - design @ (np.linalg.pinv(design) @ centroids)
    return math.sqrt(float(np.mean(np.sum(residuals**2, axis=1))))


def _track(
    signed_frames: dict[int, np.ndarray],
    threshold: float,
    *,
    scene: _Scene,
    close_radius_balls: float = 0.0,
) -> list[_Blob]:
    """The longest, then smoothest, head track that ends near the teed ball.

    Each frame offers its candidates nearest the ball or none. A track must end
    within ``NEAR_BALL_BALLS`` of the ball, keep consecutive areas within
    ``AREA_RATIO_RANGE`` and keep its centroids within ``TRACK_RMS_BALLS`` of a
    quadratic in time, so a shadow, the leg or the hands cannot stand in for
    the head in one frame.
    """
    ball_xy = (scene.ball.x, scene.ball.y)
    options = []
    for index in sorted(signed_frames):
        candidates = sorted(
            _candidates(
                signed_frames[index],
                threshold,
                index=index,
                scene=scene,
                close_radius_balls=close_radius_balls,
            ),
            key=lambda blob: _nearest_pixel_distance(blob, ball_xy),
        )
        options.append([None, *candidates[:MAX_CANDIDATES_PER_FRAME]])
    best: list[_Blob] = []
    best_score = (0, -math.inf)
    for combination in itertools.product(*options):
        track = [blob for blob in combination if blob is not None]
        if len(track) < max(1, best_score[0]):
            continue
        if _nearest_pixel_distance(track[-1], ball_xy) > NEAR_BALL_BALLS * scene.ball_px:
            continue
        if not all(
            AREA_RATIO_RANGE[0] <= later.area / earlier.area <= AREA_RATIO_RANGE[1]
            for earlier, later in zip(track, track[1:])
        ):
            continue
        roughness = _smoothness_px(track, scene.times_s)
        score = (len(track), -roughness)
        if roughness <= TRACK_RMS_BALLS * scene.ball_px and score > best_score:
            best, best_score = track, score
    return best


def _hosel_side(track: list[_Blob]) -> float:
    """+1 when the shaft joins the head on its right (image x), -1 on its left.

    Thin (opened-away) pixels in a band above each head vote; without any the
    unmirrored right-handed view (hosel on the left) is assumed.
    """
    return 1.0 if _hosel_vote(track) > 0.0 else -1.0


def _hosel_vote(track: list[_Blob]) -> float:
    """Sum of the per-frame shaft-side votes (+1 right, -1 left); 0 without a shaft."""
    votes = 0.0
    for blob in track:
        head = blob.head_mask
        rows = np.nonzero(head.any(axis=1))[0]
        cols = np.nonzero(head.any(axis=0))[0]
        span = rows[-1] - rows[0] + 1
        band = np.zeros_like(head)
        band[max(0, rows[0] - span) : rows[0] + span // 2, cols[0] : cols[-1] + 1] = True
        shaft_xs = np.nonzero(blob.thin_mask & band)[1]
        if len(shaft_xs):
            votes += 1.0 if shaft_xs.mean() + blob.origin[0] > blob.centroid[0] else -1.0
    return votes


def _head_features(blob: _Blob, hosel_side: float) -> dict[str, tuple[float, float]]:
    """Centroid, sole, heel, toe and face-centre points of one head silhouette."""
    xs, ys, weights = blob.xs, blob.ys, blob.weights
    lower_share = np.clip(ys - _weighted_quantile(ys, weights, 0.5) + 0.5, 0.0, 1.0)
    lower = lower_share > 0.0
    lower_x, lower_y = xs[lower], ys[lower]
    lower_w = weights[lower] * lower_share[lower]
    left_x = _weighted_quantile(lower_x, lower_w, FEATURE_QUANTILE)
    right_x = _weighted_quantile(lower_x, lower_w, 1.0 - FEATURE_QUANTILE)
    near_left = lower_x <= left_x + 1.0
    near_right = lower_x >= right_x - 1.0
    left = (left_x, float(np.average(lower_y[near_left], weights=lower_w[near_left])))
    right = (right_x, float(np.average(lower_y[near_right], weights=lower_w[near_right])))
    bottom = ys >= ys.max() - 2.0
    heel, toe = (right, left) if hosel_side > 0 else (left, right)
    return {
        "face_centre": (
            0.5 * (left_x + right_x),
            float(np.average(lower_y, weights=lower_w)),
        ),
        "centroid": blob.centroid,
        "sole": (
            float(np.average(xs[bottom], weights=weights[bottom])),
            float(np.average(ys[bottom], weights=weights[bottom])),
        ),
        "toe": toe,
        "heel": heel,
    }


def _lateral_on_plane(
    points_px: np.ndarray,
    forward_m: np.ndarray,
    *,
    model: _CameraModel,
    geometry: CameraBallGeometry,
) -> np.ndarray:
    """Pixel rays intersected with the vertical planes ``forward = forward_m``."""
    image_x, image_z = _normalized(
        points_px[:, 0], points_px[:, 1], focal_px=model.focal_px, geometry=geometry
    )
    lateral, forward, _vertical = camera_to_world(
        image_x, 1.0, image_z, pitch_rad=model.pitch_rad, yaw_rad=model.yaw_rad
    )
    scale = (forward_m - geometry.camera_forward_offset_m) / forward
    return geometry.camera_lateral_offset_m + scale * lateral


def _slope_operator(tau_ms: np.ndarray) -> np.ndarray:
    """Weights ``w`` with quadratic-fit slope at tau = 0 (m/ms) equal to ``w @ lateral``."""
    design = np.column_stack((np.ones_like(tau_ms), tau_ms, tau_ms**2))
    return np.linalg.pinv(design)[1]


def _fit_path(
    tau_s: np.ndarray, lateral_m: np.ndarray, *, forward_speed_ms: float, noise_floor_m: float
) -> _Fit:
    """Quadratic lateral fit; path from the slope at contact (tau = 0).

    The residual sigma has a pixel-noise floor. The leave-one-frame-out
    jackknife catches a single bad frame, but for a slope extrapolated past
    the last frame it overstates white-noise variance 1.5-3x (that frame has
    high leverage), so it is rescaled by its own expectation under white
    noise; both then agree for clean data.
    """
    tau_ms = tau_s * 1000.0
    count = len(tau_ms)
    design = np.column_stack((np.ones_like(tau_ms), tau_ms, tau_ms**2))
    coefficients = np.linalg.pinv(design) @ lateral_m
    residuals = lateral_m - design @ coefficients
    operator = _slope_operator(tau_ms)
    slope = float(operator @ lateral_m) * 1000.0
    variance = max(float(residuals @ residuals) / max(count - 3, 1), noise_floor_m**2)
    slope_sigma = math.sqrt(variance * float(operator @ operator)) * 1000.0
    leave_one_out = np.asarray(
        [np.insert(_slope_operator(np.delete(tau_ms, drop)), drop, 0.0) for drop in range(count)]
    )
    centred = leave_one_out - leave_one_out.mean(axis=0)
    white_noise_expectation = float(np.sum(centred**2))
    jackknife = (
        math.sqrt(
            float(np.sum((centred @ lateral_m) ** 2))
            * float(operator @ operator)
            / white_noise_expectation
        )
        * 1000.0
    )
    gain = forward_speed_ms / (forward_speed_ms**2 + slope**2)
    fit_sigma = math.degrees(slope_sigma * gain)
    jackknife_sigma = math.degrees(jackknife * gain)
    return _Fit(
        path_deg=math.degrees(math.atan2(slope, forward_speed_ms)),
        sigma_deg=max(fit_sigma, jackknife_sigma),
        fit_sigma_deg=fit_sigma,
        jackknife_sigma_deg=jackknife_sigma,
        contact_gain_deg_per_s=math.degrees(2.0 * abs(float(coefficients[2])) * 1e6 * gain),
        residuals_m=residuals,
    )


def _resolve_ball(
    frames: np.ndarray, reference_ball: ReferenceBall | None, ball_tracker
) -> tuple[ReferenceBall, ReferenceBall] | None:
    if reference_ball is not None:
        return reference_ball, reference_ball
    try:
        detected = detect_reference_ball(frames)
    except ValueError:
        fallback = getattr(ball_tracker, "fallback", None)
        ball = fallback() if fallback is not None else None
        return (ball, ball) if ball is not None else None
    return _locate_reference_ball(detected, ball_tracker)


def _build_scene(
    frame_shape: tuple[int, int],
    clock: tuple[np.ndarray, float],
    balls: tuple[ReferenceBall, ReferenceBall],
    geometry: CameraBallGeometry,
    forward_speed_ms: float,
) -> _Scene:
    """Camera model, ROI and ground map; ``clock`` is (frame times, contact time) in s."""
    ball, pose_ball = balls
    model = _camera_model(pose_ball, geometry)
    camera_ball_range = math.sqrt(
        geometry.camera_lateral_offset_m**2
        + geometry.camera_ball_forward_m**2
        + (geometry.ball_height_m - geometry.camera_height_m) ** 2
    )
    # The regulation ball at the known range, as ``expected_ball_diameter_px``: it
    # also sets the impact point's mm scale (never a measured size). Without a
    # lens model (the Mac or mock, and the Pi without --camera-intrinsics or for a
    # clip whose capture mode it does not cover, ``server._capture_intrinsics``
    # None) the focal comes from the pose ball's own size.
    ball_px = model.focal_px * geometry.ball_diameter_m / camera_ball_range
    roi = _roi(ball, ball_px, frame_shape)
    rows, cols = np.mgrid[roi[1] : roi[3], roi[0] : roi[2]]
    zone_radius = max(ball.diameter_px, ball_px) / 2.0 + BALL_ZONE_MARGIN_PX
    return _Scene(
        times_s=clock[0],
        contact_time_s=clock[1],
        origin=(roi[0], roi[1]),
        ball=ball,
        ball_px=ball_px,
        model=model,
        geometry=geometry,
        forward_speed_ms=forward_speed_ms,
        ground_forward_m=_ground_forward(roi, model, geometry),
        ball_zone=(cols - ball.x) ** 2 + (rows - ball.y) ** 2 <= zone_radius**2,
    )


def _background_reference(early_crop: np.ndarray) -> tuple[np.ndarray, float]:
    """The pre-swing background (median) of the ROI frames and the change threshold."""
    reference = np.median(early_crop, axis=0)
    noise = 1.4826 * float(np.median(np.abs(early_crop - reference)))
    return reference, max(DIFF_FLOOR, NOISE_SIGMAS * noise)


def _interpolated_median(deviations: np.ndarray) -> float:
    """Median of absolute deviations on a whole or half grey-level grid, spread over each step.

    A value m on grid q stands for [m - q/2, m + q/2], so the median moves continuously
    instead of jumping between the grid's steps (a MAD of 0, 1, 2 ...).
    """
    values = np.abs(np.asarray(deviations, dtype=np.float64).ravel())
    if not np.all(np.abs(values * 2.0 - np.rint(values * 2.0)) < 1e-3):
        return float(np.median(values))
    step = 0.5 if np.any(np.abs(values - np.rint(values)) > 1e-3) else 1.0
    counts = np.bincount(np.rint(values / step).astype(np.int64)).astype(np.float64)
    cumulative = np.cumsum(counts)
    target = 0.5 * cumulative[-1]
    index = int(np.searchsorted(cumulative, target))
    share = (target - (cumulative[index] - counts[index])) / counts[index]
    low, width = (0.0, step / 2.0) if index == 0 else ((index - 0.5) * step, step)
    return float(low + share * width)


def background_noise_sigma(early_crop: np.ndarray) -> float | None:
    """Grey-level noise of the pre-swing ROI frames, None without an unclipped pixel; logged only.

    Unlike the threshold's MAD in ``_background_reference`` it leaves out clipped pixels
    and does not jump with the grid, so it tells sites and light apart below 1.5 grey
    levels (80-kontrast-rauschen).
    """
    early = np.asarray(early_crop, dtype=np.float32)
    reference = np.median(early, axis=0)
    unclipped = (reference > CLIP_LOW) & (reference < CLIP_HIGH)
    if early.size == 0 or not unclipped.any():
        return None
    return round(1.4826 * _interpolated_median(np.abs(early - reference)[:, unclipped]), 3)


def _segment(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    pre_contact: np.ndarray,
    early: np.ndarray,
    scene: _Scene,
    *,
    polarities: tuple[str, ...] = ("dark", "bright"),
    close_radius_balls: float = 0.0,
) -> tuple[str, list[_Blob], dict[int, np.ndarray]] | None:
    """Track the head per polarity; keep the longer, then stronger, track.

    ``abs`` segments |frame - background|: near the ball a wood's head is darker
    than the grass above the mat edge but brighter than the dark ball and tee it
    covers, so a single polarity cuts the heel off (2026-10-25 drivers, 7-wood).
    """
    height, width = scene.ground_forward_m.shape
    x0, y0 = scene.origin
    crop = (slice(y0, y0 + height), slice(x0, x0 + width))
    reference, threshold = _background_reference(early[(slice(None), *crop)].astype(np.float32))
    found = []
    for polarity in polarities:
        if polarity == "abs":
            signed = {
                int(index): np.abs(frames[(int(index), *crop)].astype(np.float32) - reference)
                for index in pre_contact
            }
        else:
            sign = -1.0 if polarity == "dark" else 1.0
            signed = {
                int(index): sign * (frames[(int(index), *crop)].astype(np.float32) - reference)
                for index in pre_contact
            }
        track = _track(signed, threshold, scene=scene, close_radius_balls=close_radius_balls)
        if track:
            strength = float(
                np.mean([np.median(signed[blob.index][blob.head_mask]) for blob in track])
            )
            found.append(((len(track), strength), polarity, track, signed))
    if not found:
        return None
    _score, polarity, track, signed = max(found, key=lambda item: item[0])
    return polarity, track, signed


def _register(
    template: np.ndarray,
    measured: np.ndarray,
    reliable: np.ndarray,
    predicted: tuple[float, float],
    radius: int,
) -> tuple[float, float] | None:
    """Sub-pixel (dx, dy) laying ``template`` onto ``measured`` over reliable pixels.

    Integer search within ``radius`` of the prediction, then a parabola per
    axis; None when the best shift sits on the search border.
    """
    window = np.nonzero(
        ndimage.binary_dilation((template > 0.0) | (measured > 0.0), _disk(2)) & reliable
    )
    rows, cols = window[0], window[1]
    if rows.size == 0:
        return None
    pad = radius + int(max(abs(predicted[0]), abs(predicted[1]))) + 2
    padded = np.pad(template, pad)
    centre_x, centre_y = round(predicted[0]), round(predicted[1])
    target = measured[rows, cols]
    costs = np.empty((2 * radius + 1, 2 * radius + 1))
    for step_y in range(-radius, radius + 1):
        for step_x in range(-radius, radius + 1):
            source = padded[rows + pad - centre_y - step_y, cols + pad - centre_x - step_x]
            costs[step_y + radius, step_x + radius] = float(np.sum((source - target) ** 2))
    best = np.unravel_index(int(np.argmin(costs)), costs.shape)
    best_y, best_x = int(best[0]), int(best[1])
    if best_y in (0, 2 * radius) or best_x in (0, 2 * radius):
        return None

    def vertex(low: float, mid: float, high: float) -> float:
        curvature = low - 2.0 * mid + high
        return 0.5 * (low - high) / curvature if curvature > 0.0 else 0.0

    return (
        centre_x + best_x - radius + vertex(*costs[best_y, best_x - 1 : best_x + 2]),
        centre_y + best_y - radius + vertex(*costs[best_y - 1 : best_y + 2, best_x]),
    )


def _complete(
    blob: _Blob,
    previous: _Blob,
    predicted_shift: tuple[float, float],
    signed: np.ndarray,
    scene: _Scene,
) -> _Blob:
    """Fill the ball zone and the ground strip from the previous, registered head.

    Behind the teed ball (dark when backlit) or over its own shadow the head
    leaves no usable difference signal. The previous frame's head, scaled by
    the kinematic depth ratio and registered on the reliable pixels, supplies
    those pixels; measured pixels count only within ``COMPLETION_SLACK_PX`` of
    the registered head, which drops a shadow joined on through the ball zone.
    Falls back to the measured blob when the registration is ambiguous.
    """
    unreliable = scene.ball_zone | scene.below_ground(blob.index)
    soft = np.clip(signed / max(float(np.median(signed[blob.head_mask])), 1e-6), 0.0, 1.0)
    measured = np.where(ndimage.binary_dilation(blob.head_mask, _disk(1)), soft, 0.0)
    if not (ndimage.binary_dilation(measured > 0.0, _disk(3)) & unreliable).any():
        return blob
    template = np.zeros_like(measured)
    template[
        previous.ys.astype(int) - scene.origin[1], previous.xs.astype(int) - scene.origin[0]
    ] = previous.weights
    scale = scene.metres_per_px(previous.index) / scene.metres_per_px(blob.index)
    centre = np.asarray(
        (previous.centroid[1] - scene.origin[1], previous.centroid[0] - scene.origin[0])
    )
    template = ndimage.affine_transform(
        template, np.eye(2) / scale, offset=centre - centre / scale, order=1
    )
    shift = _register(
        template,
        measured,
        ~unreliable,
        predicted_shift,
        max(2, round(COMPLETION_SEARCH_BALLS * scene.ball_px)),
    )
    if shift is None:
        return blob
    placed = ndimage.shift(template, (shift[1], shift[0]), order=1)
    near_head = ndimage.binary_dilation(placed >= 0.5, _disk(COMPLETION_SLACK_PX))
    completed = np.where(unreliable, placed, np.where(near_head, measured, 0.0))
    head = ndimage.binary_fill_holes(completed >= 0.5)
    if not head.any():
        return blob
    return _make_blob(blob.index, completed, head, blob.thin_mask, scene.origin)


def _completed_track(
    track: list[_Blob], signed: dict[int, np.ndarray], scene: _Scene
) -> list[_Blob]:
    """Earliest to latest, each head completed from its predecessor where occluded."""
    completed = [track[0]]
    for number, blob in enumerate(track[1:], start=1):
        previous = completed[-1]
        if number >= 2:
            older = completed[-2]
            ratio = (scene.times_s[blob.index] - scene.times_s[previous.index]) / (
                scene.times_s[previous.index] - scene.times_s[older.index]
            )
            predicted = tuple(
                (previous.centroid[axis] - older.centroid[axis]) * ratio for axis in (0, 1)
            )
        else:
            predicted = tuple(blob.centroid[axis] - previous.centroid[axis] for axis in (0, 1))
        completed.append(_complete(blob, previous, predicted, signed[blob.index], scene))
    return completed


def _difference_quality(
    raw: np.ndarray,
    signed: np.ndarray,
    head_mask: np.ndarray,
    limits: HeadQualityLimits,
) -> HeadQuality:
    """Head quality rated on |frame - background|, its clipping read from the raw frame.

    A two-tone wood head over the mat has a raw median equal to the mat's,
    although every pixel of it differs from the background. ``limits`` has no
    default: a hybrid is rated the same way but against ``MIN_HEAD_CONTRAST``,
    so each caller names its club's limits (``_woods_mode``).
    """
    quality = assess_head_quality(np.abs(signed), head_mask, limits)
    pixels = raw[head_mask]
    clipped = (
        float(np.mean((pixels >= CLIP_HIGH) | (pixels <= CLIP_LOW)) * 100.0) if pixels.size else 0.0
    )
    status = quality.status
    if status == "ok" and clipped > MAX_HEAD_CLIPPED_PCT:
        status = "clipped"
    elif status == "clipped" and clipped <= MAX_HEAD_CLIPPED_PCT:
        status = "ok"
    return replace(quality, status=status, clipped_pct=round(clipped, 1))


def _observe(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    track: list[_Blob],
    signed: dict[int, np.ndarray],
    scene: _Scene,
    *,
    quality_on_difference: HeadQualityLimits | None = None,
    completed: list[_Blob] | None = None,
) -> list[HeadObservation]:
    """Features and head quality per frame; a width off the track's metric width fails.

    ``quality_on_difference`` (woods mode) holds the limits the head is rated
    against on the background difference (``_difference_quality``); None rates
    it on the raw frame. ``completed`` is the track as ``_completed_track``
    completes it, when the caller has it.
    """
    height, width = scene.ground_forward_m.shape
    x0, y0 = scene.origin
    hosel_side = _hosel_side(track)
    observations = []
    if completed is None:
        completed = _completed_track(track, signed, scene)
    for measured, blob in zip(track, completed):
        raw = frames[blob.index, y0 : y0 + height, x0 : x0 + width]
        quality = (
            _difference_quality(raw, signed[blob.index], measured.head_mask, quality_on_difference)
            if quality_on_difference is not None
            else assess_head_quality(raw, measured.head_mask)
        )
        observations.append(
            HeadObservation(
                frame_index=blob.index,
                t_rel_ms=(scene.times_s[blob.index] - scene.contact_time_s) * 1000.0,
                features_px=_head_features(blob, hosel_side),
                quality=quality,
                width_m=blob.width_px * scene.metres_per_px(blob.index),
            )
        )
    typical = float(np.median([item.width_m for item in observations]))
    return [
        replace(item, quality=replace(item.quality, status="inconsistent_size"))
        if item.quality.ok and abs(item.width_m / typical - 1.0) > WIDTH_CONSISTENCY
        else item
        for item in observations
    ]


def _measure(
    observations: list[HeadObservation], feature: str, scene: _Scene
) -> tuple[dict[str, _Fit], list[HeadObservation]]:
    """Fit every feature; attach the chosen feature's positions and residuals."""
    usable = [item for item in observations if item.quality.ok]
    tau_s = np.asarray([item.t_rel_ms / 1000.0 for item in usable])
    forward_m = np.asarray([scene.head_forward_m(item.frame_index) for item in usable])
    noise_floor_m = POSITION_NOISE_FLOOR_PX * float(
        np.median([scene.metres_per_px(item.frame_index) for item in usable])
    )
    fits, laterals = {}, {}
    for name in FEATURES:
        points = np.asarray([item.features_px[name] for item in usable])
        laterals[name] = _lateral_on_plane(
            points, forward_m, model=scene.model, geometry=scene.geometry
        )
        fits[name] = _fit_path(
            tau_s,
            laterals[name],
            forward_speed_ms=scene.forward_speed_ms,
            noise_floor_m=noise_floor_m,
        )
    position = {item.frame_index: number for number, item in enumerate(usable)}
    annotated = [
        replace(
            item,
            lateral_m=float(laterals[feature][position[item.frame_index]]),
            forward_m=float(forward_m[position[item.frame_index]]),
            residual_mm=float(fits[feature].residuals_m[position[item.frame_index]] * 1e3),
        )
        if item.frame_index in position
        else item
        for item in observations
    ]
    return fits, annotated


def _input_gate(
    stack: np.ndarray, times_s: np.ndarray, contact_time_s: float, club_speed_mph: float | None
) -> str | None:
    """First failed input check, or None."""
    if stack.ndim != 3 or len(stack) != len(times_s) or not math.isfinite(contact_time_s):
        return "rejected_invalid_input"
    if club_speed_mph is None or not math.isfinite(club_speed_mph) or club_speed_mph <= 0.0:
        return "rejected_no_club_speed"
    if np.count_nonzero(times_s <= contact_time_s - CONTACT_GUARD_S) < MIN_FIT_FRAMES:
        return "rejected_too_few_frames"
    if np.count_nonzero(times_s < contact_time_s - BACKGROUND_LEAD_S) < BACKGROUND_MIN_FRAMES:
        return "rejected_no_background"
    return None


def _ball_gate(balls: tuple[ReferenceBall, ReferenceBall] | None) -> str | None:
    if balls is None:
        return "rejected_reference_ball_not_found"
    if not 9.0 <= balls[0].diameter_px <= 30.0:
        return "rejected_implausible_reference_ball"
    return None


class _SegmentationMode(NamedTuple):
    """How one run segments the head; ``quality_on_difference`` as in ``_observe``."""

    polarities: tuple[str, ...]
    close_radius_balls: float = 0.0
    quality_on_difference: HeadQualityLimits | None = None


_POLARITY_MODE = _SegmentationMode(("dark", "bright"))


def _woods_mode(close_radius_balls: float, club: ClubType | None) -> _SegmentationMode:
    """Woods segmentation; only a wood's head is rated against ``MIN_WOOD_HEAD_CONTRAST``."""
    limits = WOOD_DIFFERENCE_LIMITS if club in WOOD_CATEGORIES else HeadQualityLimits()
    return _SegmentationMode(("abs",), close_radius_balls, limits)


class HeadTrack(NamedTuple):
    """One segmentation's head track over ``frames``: measured and completed heads.

    ``polarity`` is None when that segmentation found no head. The impact point
    (``camera.impact_point``) reuses it when its own window is the same frames.
    """

    frames: tuple[int, ...]
    polarity: str | None
    track: tuple[_Blob, ...] = ()
    completed: tuple[_Blob, ...] = ()


class _PathRun(NamedTuple):
    """One segmentation run: its path result, the measured and the whole head track."""

    result: ClubHeadPathResult
    track: tuple[_Blob, ...] = ()
    head_track: HeadTrack | None = None


def _run_path(  # pylint: disable=too-many-arguments,too-many-locals
    stack: np.ndarray,
    pre_contact: np.ndarray,
    early: np.ndarray,
    scene: _Scene,
    *,
    feature: str,
    contact_sigma_s: float,
    mode: _SegmentationMode,
) -> _PathRun:
    """Segment, track, observe and fit one segmentation mode."""
    segmented = _segment(
        stack,
        pre_contact,
        stack[early],
        scene,
        polarities=mode.polarities,
        close_radius_balls=mode.close_radius_balls,
    )
    frames = tuple(int(index) for index in pre_contact)
    if segmented is None:
        return _PathRun(
            ClubHeadPathResult("rejected_head_not_found", feature=feature),
            head_track=HeadTrack(frames, None),
        )
    polarity, track, signed = segmented
    completed = _completed_track(track, signed, scene)
    head_track = HeadTrack(frames, polarity, tuple(track), tuple(completed))
    observations = _observe(
        stack,
        track,
        signed,
        scene,
        quality_on_difference=mode.quality_on_difference,
        completed=completed,
    )
    usable = sum(item.quality.ok for item in observations)
    if len(track) < MIN_FIT_FRAMES or usable < MIN_FIT_FRAMES:
        return _PathRun(
            ClubHeadPathResult(
                "rejected_short_track" if len(track) < MIN_FIT_FRAMES else "rejected_head_quality",
                n_frames=usable,
                feature=feature,
                polarity=polarity,
                observations=tuple(observations),
            ),
            tuple(track),
            head_track,
        )

    fits, observations = _measure(observations, feature, scene)
    chosen = fits[feature]
    feature_sigma = 0.5 * abs(chosen.path_deg - fits[_ALTERNATIVE_FEATURE[feature]].path_deg)
    contact_sigma = chosen.contact_gain_deg_per_s * max(contact_sigma_s, 0.0)
    sigma = math.sqrt(chosen.sigma_deg**2 + feature_sigma**2 + contact_sigma**2)
    # Same frame as the start direction: the horizontal offset, or the tapped target line.
    path_deg = _apply_horizontal_offset(chosen.path_deg, scene.model.direction_offset_deg)
    if sigma > MAX_SIGMA_DEG:
        status = "rejected_sigma_too_large"
    elif abs(path_deg) > MAX_ABS_PATH_DEG:
        status = "rejected_implausible_path"
    else:
        status = "accepted"
    return _PathRun(
        ClubHeadPathResult(
            status=status,
            path_deg=round(path_deg, 2),
            sigma_deg=round(sigma, 2),
            n_frames=usable,
            feature=feature,
            polarity=polarity,
            fit_sigma_deg=round(chosen.fit_sigma_deg, 2),
            jackknife_sigma_deg=round(chosen.jackknife_sigma_deg, 2),
            feature_sigma_deg=round(feature_sigma, 2),
            contact_sigma_deg=round(contact_sigma, 2),
            feature_paths_deg={
                name: round(
                    _apply_horizontal_offset(fit.path_deg, scene.model.direction_offset_deg), 2
                )
                for name, fit in fits.items()
            },
            observations=tuple(observations),
        ),
        tuple(track),
        head_track,
    )


def _radius_key(radius: float) -> str:
    return f"{radius:.2f}"


def _woods_ensemble(runs: dict[float, _PathRun]) -> _PathRun:
    """Median path over the closing radii, widened by half their range.

    Every radius must give a path, else ``rejected_segmentation_unstable`` (or,
    when none does, the middle radius's own rejection). The result carries the
    diagnostics and observations of the radius that gives the median path
    (plan D11); sigma = sqrt(median sigma^2 + (range / 2)^2) passes the path's
    usual gates.
    """
    middle = runs[WOODS_CLOSE_RADII_BALLS[len(WOODS_CLOSE_RADII_BALLS) // 2]]
    paths = {_radius_key(radius): run.result.path_deg for radius, run in runs.items()}
    given = {key: value for key, value in paths.items() if value is not None}
    if len(given) < len(paths):
        status = middle.result.status if not given else "rejected_segmentation_unstable"
        return middle._replace(
            result=replace(
                middle.result,
                status=status,
                path_deg=None,
                sigma_deg=None,
                segmentation="woods",
                segmentation_paths_deg=given,
            )
        )
    median_path = statistics.median(given.values())
    chosen = next(run for run in runs.values() if run.result.path_deg == median_path)
    spread = 0.5 * (max(given.values()) - min(given.values()))
    sigma = math.sqrt(
        statistics.median(run.result.sigma_deg for run in runs.values()) ** 2 + spread**2
    )
    if sigma > MAX_SIGMA_DEG:
        status = "rejected_sigma_too_large"
    elif abs(median_path) > MAX_ABS_PATH_DEG:
        status = "rejected_implausible_path"
    else:
        status = "accepted"
    return chosen._replace(
        result=replace(
            chosen.result,
            status=status,
            path_deg=round(median_path, 2),
            sigma_deg=round(sigma, 2),
            segmentation="woods",
            segmentation_sigma_deg=round(spread, 2),
            segmentation_paths_deg=given,
        )
    )


def _arc_path_of_attack(attack: AttackAngleResult | None) -> ArcPath | None:
    """The ``ArcPath`` the attack-angle fit already holds, None when it did not fit."""
    if attack is None or attack.arc_path_status == "not_run":
        return None
    accepted = attack.arc_path_status == "accepted"
    return ArcPath(
        attack.arc_path_status,
        path_deg=attack.arc_path_deg if accepted else None,
        sigma_deg=attack.arc_path_total_sigma_deg if accepted else None,
    )


def _arc_path_gate(path_deg: float, sigma_deg: float) -> str:
    if sigma_deg > MAX_SIGMA_DEG:
        return "rejected_sigma_too_large"
    if abs(path_deg) > MAX_ABS_PATH_DEG:
        return "rejected_implausible_path"
    return "accepted"


def _path_arc_fields(  # pylint: disable=too-many-arguments
    runs: dict[float, _PathRun],
    attack: AttackAngleResult,
    attack_observations: tuple[HeadObservation, ...],
    *,
    camera: ArcCamera,
    geometry: CameraBallGeometry,
    club_speed_mph: float,
    contact_sigma_s: float,
    feature: str,
    wood: bool,
    left_handed: bool,
) -> dict:
    """The ``path_arc_*`` fields of one clip; the attack fit's run costs no new fit.

    Several runs (woods) follow ``_woods_ensemble``'s rule on their arc paths: every
    radius must give one, the median counts and half the range widens sigma.
    """
    reused = _arc_path_of_attack(attack)
    arcs = {
        radius: (
            reused
            if reused is not None and run.result.observations is attack_observations
            else estimate_arc_path(
                run.result.observations,
                camera=camera,
                geometry=geometry,
                club_speed_mph=club_speed_mph,
                contact_sigma_s=contact_sigma_s,
                feature=feature,
                wood=wood,
                left_handed=left_handed,
            )
        )
        for radius, run in runs.items()
    }
    fields: dict = {}
    if len(arcs) > 1:
        given = {r: arc for r, arc in arcs.items() if arc.path_deg is not None}
        fields["path_arc_radii_deg"] = {_radius_key(r): arc.path_deg for r, arc in given.items()}
        if len(given) < len(arcs):
            middle = arcs[WOODS_CLOSE_RADII_BALLS[len(WOODS_CLOSE_RADII_BALLS) // 2]]
            status = "rejected_segmentation_unstable" if given else middle.status
            return {**fields, "path_arc_status": status}
        paths = [arc.path_deg for arc in given.values()]
        median_path = statistics.median(paths)
        chosen = next(r for r, arc in given.items() if arc.path_deg == median_path)
        spread = 0.5 * (max(paths) - min(paths))
        path_deg = round(median_path, 2)
        sigma_deg = round(
            math.sqrt(statistics.median(arc.sigma_deg for arc in given.values()) ** 2 + spread**2),
            2,
        )
    else:
        chosen, only = next(iter(arcs.items()))
        if only.path_deg is None:
            return {"path_arc_status": only.status}
        path_deg, sigma_deg = only.path_deg, only.sigma_deg
    return {
        **fields,
        "path_arc_status": _arc_path_gate(path_deg, sigma_deg),
        "path_arc_deg": path_deg,
        "path_arc_sigma_deg": sigma_deg,
        "path_arc_window_deg": arc_path_windows(
            runs[chosen].result.observations,
            camera=camera,
            geometry=geometry,
            club_speed_mph=club_speed_mph,
            feature=feature,
            left_handed=left_handed,
        ),
    }


def _poly_intercept(
    tau_ms: np.ndarray, values: np.ndarray, degree: int, noise_floor: float
) -> tuple[float, float, float]:
    """Value and slope (per ms) at tau = 0 of a polynomial fit, and the value's sigma.

    The sigma is the larger of the residual sigma (with a noise floor) and the
    leave-one-out jackknife rescaled by its white-noise expectation, as in
    ``_fit_path``.
    """
    count = len(tau_ms)
    design = np.column_stack([tau_ms**power for power in range(degree + 1)])
    pinv = np.linalg.pinv(design)
    coefficients = pinv @ values
    residuals = values - design @ coefficients
    dof = count - (degree + 1)
    variance = max(float(residuals @ residuals) / dof if dof > 0 else 0.0, noise_floor**2)
    operator = pinv[0]
    sigma = math.sqrt(variance * float(operator @ operator))
    if count >= degree + 2:
        leave_one_out = []
        for drop in range(count):
            keep = np.delete(np.arange(count), drop)
            reduced = np.column_stack([tau_ms[keep] ** power for power in range(degree + 1)])
            leave_one_out.append(np.insert(np.linalg.pinv(reduced)[0], drop, 0.0))
        centred = np.asarray(leave_one_out) - np.mean(leave_one_out, axis=0)
        expectation = float(np.sum(centred**2))
        if expectation > 0.0:
            sigma = max(
                sigma,
                math.sqrt(
                    float(np.sum((centred @ values) ** 2))
                    * float(operator @ operator)
                    / expectation
                ),
            )
    slope = float(coefficients[1]) if degree >= 1 else 0.0
    return float(coefficients[0]), slope, sigma


def head_length_plausible(head_length_mm: float | None) -> bool:
    """Whether a head length lies in the absolute range (the session median learns from it)."""
    if head_length_mm is None or not math.isfinite(head_length_mm):
        return False
    length_low, length_high = IMPACT_HEAD_LENGTH_RANGE_MM
    return length_low <= head_length_mm <= length_high


def _head_length_passes(head_length_mm: float, head_length_median_mm: float | None) -> bool:
    """The head-length gate: the absolute range, then the band around the club's median."""
    if not head_length_plausible(head_length_mm):
        return False
    if not head_length_median_mm:
        return True
    return abs(head_length_mm / head_length_median_mm - 1.0) <= IMPACT_HEAD_LENGTH_TOLERANCE


def _impact_location(  # pylint: disable=too-many-arguments,too-many-locals,too-many-return-statements
    run: _PathRun,
    scene: _Scene,
    *,
    club_speed_mph: float | None,
    contact_sigma_s: float,
    handedness: str,
    ball: tuple[ReferenceBall, str] | None,
    head_length_median_mm: float | None,
) -> ImpactLocation:
    """Heel/toe offset of the ball from the face centre extrapolated to contact.

    The face centre's lateral position on its kinematic plane is fitted
    quadratically in time over the last <= ``IMPACT_FIT_FRAMES`` usable frames
    and taken at tau = 0; the ball's on its own plane. The toe side comes from
    ``handedness`` (right-handed: toe toward +lateral); the shaft's side vote
    only checks it. Gates, first failure wins: no club speed, short track,
    extrapolation gap, no confirmed ball, hosel side, head length, offset,
    sigma. Independent of the path's status.
    """
    base = {"handedness": handedness}
    if club_speed_mph is None or not math.isfinite(club_speed_mph) or club_speed_mph <= 0.0:
        return ImpactLocation("rejected_no_club_speed", **base)
    usable = [item for item in run.result.observations if item.quality.ok][-IMPACT_FIT_FRAMES:]
    if len(usable) < MIN_FIT_FRAMES:
        return ImpactLocation("rejected_short_track", **base)
    geometry = scene.geometry
    toe_sign = 1.0 if handedness == "right" else -1.0
    tau_ms = np.asarray([item.t_rel_ms for item in usable])
    forward_m = np.asarray([scene.head_forward_m(item.frame_index) for item in usable])
    noise_floor = POSITION_NOISE_FLOOR_PX * float(
        np.median([scene.metres_per_px(item.frame_index) for item in usable])
    )

    def lateral(name: str) -> np.ndarray:
        points = np.asarray([item.features_px[name] for item in usable])
        return _lateral_on_plane(points, forward_m, model=scene.model, geometry=geometry)

    face = lateral(IMPACT_FEATURE)
    head_lateral, slope_m_per_ms, fit_sigma = _poly_intercept(tau_ms, face, 2, noise_floor)
    toe_end, _slope, _sigma = _poly_intercept(tau_ms, lateral("toe"), 2, noise_floor)
    heel_end, _slope, _sigma = _poly_intercept(tau_ms, lateral("heel"), 2, noise_floor)
    head_length_mm = abs(toe_end - heel_end) * 1e3
    gap_ms = -float(tau_ms[-1])
    vote = _hosel_vote(list(run.track))
    contact_sigma_mm = abs(slope_m_per_ms) * 1e3 * max(contact_sigma_s, 0.0) * 1e3
    fit_sigma_mm = fit_sigma * 1e3
    sigma_mm = math.sqrt(
        fit_sigma_mm**2 + IMPACT_EDGE_SIGMA_MM**2 + contact_sigma_mm**2 + IMPACT_BALL_SIGMA_MM**2
    )
    measured = replace(
        ImpactLocation("pending", **base),
        fit_sigma_mm=round(fit_sigma_mm, 2),
        contact_sigma_mm=round(contact_sigma_mm, 2),
        sigma_mm=round(sigma_mm, 2),
        head_length_mm=round(head_length_mm, 1),
        extrapolation_gap_ms=round(gap_ms, 3),
        lateral_speed_mm_per_ms=round(slope_m_per_ms * 1e3, 3),
        hosel_vote=vote,
    )
    if gap_ms > IMPACT_MAX_GAP_MS:
        return replace(measured, status="rejected_extrapolation_gap")
    if ball is None:
        return replace(measured, status="rejected_ball_not_confirmed")

    confirmed, reference = ball

    def ball_lateral(spot: ReferenceBall) -> float:
        return float(
            _lateral_on_plane(
                np.asarray([[spot.x, spot.y]]),
                np.asarray([geometry.ball_forward_m]),
                model=scene.model,
                geometry=geometry,
            )[0]
        )

    confirmed_lateral = ball_lateral(confirmed)
    toe_mm = toe_sign * (confirmed_lateral - head_lateral) * 1e3
    last3 = slice(-3, None)
    linear, _slope, _sigma = _poly_intercept(tau_ms[last3], face[last3], 1, noise_floor)
    centroid, _slope, _sigma = _poly_intercept(tau_ms, lateral("centroid"), 2, noise_floor)
    measured = replace(
        measured,
        toe_mm=round(toe_mm, 2),
        ball_reference=reference,
        ball_px=(
            round(float(confirmed.x), 2),
            round(float(confirmed.y), 2),
            round(float(confirmed.diameter_px), 2),
        ),
        ball_shift_mm=round(toe_sign * (confirmed_lateral - ball_lateral(scene.ball)) * 1e3, 2),
        variants_mm={
            "linear_last3": round(toe_sign * (confirmed_lateral - linear) * 1e3, 2),
            "centroid_quadratic": round(toe_sign * (confirmed_lateral - centroid) * 1e3, 2),
        },
    )
    expected_hosel_side = -toe_sign * geometry.horizontal_pixel_sign
    if vote != 0.0 and math.copysign(1.0, vote) != expected_hosel_side:
        return replace(measured, status="rejected_hosel_side_disagrees")
    if not _head_length_passes(head_length_mm, head_length_median_mm):
        return replace(measured, status="rejected_head_length")
    if abs(toe_mm) > IMPACT_MAX_ABS_MM:
        return replace(measured, status="rejected_implausible_offset")
    if sigma_mm > IMPACT_MAX_SIGMA_MM:
        return replace(measured, status="rejected_sigma_too_large")
    return replace(measured, status="accepted")


def _impact_ball(
    stack: np.ndarray,
    times_s: np.ndarray,
    scene: _Scene,
    *,
    departed_ball: ReferenceBall | None,
    confirmed_tee: ReferenceBall | None,
) -> tuple[ReferenceBall, str] | None:
    """The ball the impact refers to: the given departed ball, else one found leaving."""
    if departed_ball is not None:
        return departed_ball, "given"
    references = (scene.ball,) if confirmed_tee is None else (scene.ball, confirmed_tee)
    found = locate_departed_ball(
        stack,
        np.round(times_s * 1e9).astype(np.int64),
        contact_time_s=scene.contact_time_s,
        references=references,
        expected_diameter_px=scene.ball_px,
    )
    if found is None:
        return None
    spot = ReferenceBall(
        found.x, found.y, found.diameter_px, int(round(math.pi * (found.diameter_px / 2.0) ** 2))
    )
    return spot, ("ball", "tee")[found.reference_index]


def locate_departed_ball(
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    contact_time_s: float,
    references: tuple[ReferenceBall | None, ...],
    expected_diameter_px: float,
) -> ConfirmedBall | None:
    """The ball seen leaving near one of ``references``, by ``departed_ball``; never raises.

    Each reference opens a ``DEPARTED_BALL_WINDOW_PX`` search box (it may sit on a
    decoy: a static spot never "disappears"); ``departed_ball.locate_departed_ball``
    finds what disappeared after ``contact_time_s`` and its gates
    (``DepartedBall.gate_failure``) must pass. The strongest (highest SNR) ball
    wins. None without one.
    """
    try:
        stack = np.asarray(frames)
        times_ns = np.asarray(timestamps_ns, dtype=np.int64)
        valid_clip = stack.ndim == 3 and 0 < len(stack) == len(times_ns)
        if not (
            valid_clip
            and math.isfinite(contact_time_s)
            and math.isfinite(expected_diameter_px)
            and expected_diameter_px > 0.0
        ):
            return None
        contact_index = int(np.searchsorted(times_ns / 1e9, contact_time_s))
        half_width, above, below = DEPARTED_BALL_WINDOW_PX
        candidates: list[tuple[float, int, departed_ball_module.DepartedBall, ReferenceBall]] = []
        for number, reference in enumerate(references):
            if reference is None:
                continue
            box = (
                int(round(reference.x - half_width)),
                int(round(reference.y - above)),
                int(round(reference.x + half_width)),
                int(round(reference.y + below)),
            )
            found, _reason = departed_ball_module.locate_departed_ball(
                stack, contact_index, expected_diameter_px=expected_diameter_px, box=box
            )
            if found is not None and found.gate_failure() is None:
                # Highest SNR first; on a tie the earlier reference.
                candidates.append((-found.snr, number, found, reference))
    except (ValueError, IndexError, ArithmeticError, np.linalg.LinAlgError):
        return None
    if not candidates:
        return None
    _rank, number, found, reference = min(candidates, key=lambda item: item[:2])
    return ConfirmedBall(
        x=float(found.x),
        y=float(found.y),
        diameter_px=float(found.diameter_px),
        reference_index=number,
        shift_px=math.hypot(found.x - reference.x, found.y - reference.y),
        snr=float(found.snr),
        teed=bool(found.teed),
    )


def _early_result(
    status: str, *, feature: str, attack: bool, handedness: str, ball_source: str | None
) -> ClubHeadPathResult:
    """A rejection before segmentation: the attack and impact carry the same status."""
    return ClubHeadPathResult(
        status,
        feature=feature,
        ball_source=ball_source,
        attack=AttackAngleResult(status, feature=feature) if attack else None,
        impact=ImpactLocation(status, handedness=handedness),
    )


def estimate_club_head_path(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    contact_time_s: float,
    geometry: CameraBallGeometry,
    club_speed_mph: float | None,
    reference_ball: ReferenceBall | None = None,
    ball_tracker=None,
    contact_sigma_s: float = 0.0,
    feature: str = DEFAULT_FEATURE,
    max_frames: int = MAX_FIT_FRAMES,
    club: ClubType | None = None,
    segmentation: str | None = None,
    handedness: str = "right",
    departed_ball: ReferenceBall | None = None,
    confirmed_tee: ReferenceBall | None = None,
    head_length_median_mm: float | None = None,
    ball_source: str = "given",
    attack: bool = True,
    timings: dict[str, float] | None = None,
    impact_point: bool = True,
    ball_speed_mph: float | None = None,
    launch_vertical_deg: float | None = None,
    launch_horizontal_deg: float | None = None,
    impact_point_head_length_median_mm: float | None = None,
) -> ClubHeadPathResult:
    """Club path (deg, + in-to-out) at contact from the head silhouette before contact.

    ``contact_time_s`` is on the frame clock (``select_contact_time``);
    ``contact_sigma_s`` is its uncertainty, which the arc's curvature turns into
    path uncertainty. ``geometry`` must be the one used for the camera launch
    direction so path and launch share one yaw frame. The ball comes from
    ``reference_ball`` or, like the launch direction, from detection plus the
    session ``ball_tracker``; ``ball_source`` records where the caller got it.

    ``segmentation`` is ``polarity`` (dark or bright head) or ``woods`` (the
    three-radius ensemble on the absolute difference); by default ``woods`` for
    a club in ``WOODS_SEGMENTATION``, else ``polarity``. Woods mode rates the
    head of a wood (``WOOD_CATEGORIES``) against ``MIN_WOOD_HEAD_CONTRAST``, of
    any other club against ``MIN_HEAD_CONTRAST``.

    ``sigma_deg`` combines in quadrature the statistical sigma (larger of fit
    residual and calibrated jackknife), half the disagreement between the
    face-centre and centroid paths, the contact-time term and, in woods mode,
    half the closing-radius spread. With ``attack`` the arc attack angle and low
    point (``club_attack.estimate_attack_angle``) are fitted to the same
    observations, also when the path fails its sigma gate; a rejection before
    the fit is copied into ``attack.status``. ``impact`` is the heel/toe offset
    against ``departed_ball`` (the ball flight's departed ball), else against a
    ball found leaving near the club ball or ``confirmed_tee``
    (``locate_departed_ball``); ``handedness`` sets its toe side.

    With ``impact_point`` the impact point across contact (method D,
    ``camera.impact_point.measure_impact_point``) is measured against the same
    ball (the resting ball when none is confirmed), on its own head track ending
    in the last frame before contact; ``ball_speed_mph`` and the launch angles
    blank the departing ball in the frames after it (without a ball speed only
    the frames before contact are used). ``impact_point_head_length_median_mm``
    is the session median of its head lengths; its mm scale is always the
    scene's expected ball diameter (the regulation ball at the known range).

    ``timings``, when given, receives the seconds spent in ``path``, ``attack``,
    ``impact`` and ``impact_point``. Never raises on bad clips: ``status`` names
    the first failed gate.
    """
    if feature not in FEATURES:
        raise ValueError(f"feature must be one of {FEATURES}")
    if segmentation is None:
        segmentation = "woods" if club in WOODS_SEGMENTATION else "polarity"
    if segmentation not in SEGMENTATIONS:
        raise ValueError(f"segmentation must be one of {SEGMENTATIONS}")
    if handedness not in HANDEDNESS:
        raise ValueError(f"handedness must be one of {HANDEDNESS}")
    stage = {"path": 0.0, "attack": 0.0, "impact": 0.0, "impact_point": 0.0}
    if timings is not None:
        timings.update(stage)
    started = time.perf_counter()
    stack = np.asarray(frames)
    times_s = np.asarray(timestamps_ns, dtype=np.int64) / 1e9
    rejected = _input_gate(stack, times_s, contact_time_s, club_speed_mph)
    balls = None
    if rejected is None:
        balls = _resolve_ball(stack, reference_ball, ball_tracker)
        rejected = _ball_gate(balls)
    if rejected is not None:
        if timings is not None:
            timings["path"] = time.perf_counter() - started
        return _early_result(
            rejected,
            feature=feature,
            attack=attack,
            handedness=handedness,
            ball_source=ball_source,
        )

    pre_contact = np.nonzero(times_s <= contact_time_s - CONTACT_GUARD_S)[0]
    pre_contact = pre_contact[-max(max_frames, MIN_FIT_FRAMES) :]
    early = np.nonzero(times_s < contact_time_s - BACKGROUND_LEAD_S)[0][:BACKGROUND_FRAMES]
    scene = _build_scene(
        stack.shape[1:], (times_s, contact_time_s), balls, geometry, club_speed_mph / MPH_PER_MS
    )
    height, width = scene.ground_forward_m.shape
    noise_sigma = background_noise_sigma(
        stack[early][
            :,
            scene.origin[1] : scene.origin[1] + height,
            scene.origin[0] : scene.origin[0] + width,
        ]
    )
    run_kwargs = {"feature": feature, "contact_sigma_s": contact_sigma_s}
    if segmentation == "woods":
        runs = {
            radius: _run_path(
                stack, pre_contact, early, scene, mode=_woods_mode(radius, club), **run_kwargs
            )
            for radius in WOODS_CLOSE_RADII_BALLS
        }
        run = _woods_ensemble(runs)
    else:
        runs = {0.0: _run_path(stack, pre_contact, early, scene, mode=_POLARITY_MODE, **run_kwargs)}
        run = runs[0.0]
    stage["path"] = time.perf_counter() - started
    path_result = replace(
        run.result,
        noise_sigma=noise_sigma,
        ball_source=ball_source,
        ball_px=(
            round(float(scene.ball.x), 2),
            round(float(scene.ball.y), 2),
            round(float(scene.ball.diameter_px), 2),
        ),
    )

    attack_result = None
    path_arc: dict = {}
    if attack:
        attack_started = time.perf_counter()
        if not run.track:
            attack_result = AttackAngleResult(path_result.status, feature=feature)
        elif path_result.status in ("rejected_short_track", "rejected_head_quality"):
            attack_result = AttackAngleResult(
                path_result.status, n_frames=path_result.n_frames, feature=feature
            )
        else:
            attack_result = estimate_attack_angle(
                path_result.observations,
                camera=ArcCamera.from_scene(scene),
                geometry=geometry,
                club_speed_mph=club_speed_mph,
                contact_sigma_s=contact_sigma_s,
                feature=feature,
                wood=club in WOODS_SEGMENTATION,
                tee_height_known=False,
                left_handed=handedness == "left",
            )
        try:
            path_arc = _path_arc_fields(
                runs,
                attack_result,
                path_result.observations,
                camera=ArcCamera.from_scene(scene),
                geometry=geometry,
                club_speed_mph=club_speed_mph,
                contact_sigma_s=contact_sigma_s,
                feature=feature,
                wood=club in WOODS_SEGMENTATION,
                left_handed=handedness == "left",
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The shadow value must never cost the shot its path and attack angle.
            path_arc = {"path_arc_status": f"error:{type(error).__name__}"}
        stage["attack"] = time.perf_counter() - attack_started

    impact_started = time.perf_counter()
    impact_ball = (
        _impact_ball(
            stack, times_s, scene, departed_ball=departed_ball, confirmed_tee=confirmed_tee
        )
        if run.track or impact_point
        else None
    )
    if run.track:
        impact = _impact_location(
            run,
            scene,
            club_speed_mph=club_speed_mph,
            contact_sigma_s=contact_sigma_s,
            handedness=handedness,
            ball=impact_ball,
            head_length_median_mm=head_length_median_mm,
        )
    else:
        impact = ImpactLocation(path_result.status, handedness=handedness)
    stage["impact"] = time.perf_counter() - impact_started

    point = None
    if impact_point:
        point_started = time.perf_counter()
        # Imported here: camera.impact_point builds on this module's segmentation.
        from openflight.camera import impact_point as impact_point_module  # noqa: PLC0415

        # The segmentation D uses (polarity, or woods at its one closing radius) ran
        # here already; D reuses its head track when it ends in the same frame.
        prior = runs.get(
            impact_point_module.WOODS_CLOSE_RADIUS_BALLS if segmentation == "woods" else 0.0
        )
        try:
            point = impact_point_module.measure_impact_point(
                stack,
                scene,
                club=club,
                woods=segmentation == "woods",
                contact_sigma_s=contact_sigma_s,
                ball=impact_ball if impact_ball is not None else (scene.ball, "resting"),
                launch=(
                    None
                    if ball_speed_mph is None
                    else impact_point_module.BallLaunch(
                        float(ball_speed_mph), launch_vertical_deg, launch_horizontal_deg
                    )
                ),
                handedness=handedness,
                head_length_median_mm=impact_point_head_length_median_mm,
                prior_track=None if prior is None else prior.head_track,
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The newer measurement must never cost the shot its path, attack and E.
            point = impact_point_module.ImpactPoint("error", error=type(error).__name__)
        stage["impact_point"] = time.perf_counter() - point_started
    if timings is not None:
        timings.update(stage)
    return replace(path_result, attack=attack_result, impact=impact, impact_point=point, **path_arc)


def expected_ball_diameter_px(geometry: CameraBallGeometry, anchor_diameter_px: float) -> float:
    """The resting ball's diameter at its known range (the anchor's own without a lens model).

    The regulation ball (42.67 mm) through the lens model; the anchor fallback
    serves a geometry without intrinsics: the Mac or mock, and the Pi without
    ``--camera-intrinsics`` or for a clip whose capture mode the lens model does
    not cover (``server._capture_intrinsics`` None).
    """
    camera_ball_range = math.sqrt(
        geometry.camera_lateral_offset_m**2
        + geometry.camera_ball_forward_m**2
        + (geometry.ball_height_m - geometry.camera_height_m) ** 2
    )
    focal_px = reference_focal_px(
        anchor_diameter_px, camera_ball_range, geometry.ball_diameter_m, geometry.intrinsics
    )
    return focal_px * geometry.ball_diameter_m / camera_ball_range


def _ball_edge_centre(
    change: np.ndarray, centre: int, diameter: float, contrast: float
) -> tuple[float, float]:
    """Sub-pixel ball centre in a window around the template peak at (``centre``, ``centre``).

    ``change`` is the window minus the ring level and ``contrast`` the template's
    disk-minus-ring response. The centre of a circle fitted to the ball's radial
    edges over its upper ~280 deg (``departed_ball``'s edge fit: the tee and the
    shadow below are skipped), outliers dropped as there. |change| is clipped at
    half the contrast first, so a lit rim of a dark ball (or a shaded rim of a
    bright one) reads as ball and the edge lies outside it. Falls back to the
    contrast-weighted centroid inside the disk when the fit fails, its size is
    off (``departed_ball.SIZE_RANGE``) or it moved more than
    ``REFINE_MAX_EDGE_SHIFT_DIAMETERS`` from the peak.
    """
    level = np.minimum(np.abs(change.astype(np.float32)), 0.5 * abs(contrast))
    smoothed = ndimage.gaussian_filter(level, departed_ball_module.EDGE_SMOOTHING_SIGMA_PX)
    # pylint: disable=protected-access
    edge_x, edge_y = departed_ball_module._edge_points(
        smoothed, float(centre), float(centre), diameter
    )
    if len(edge_x) >= departed_ball_module.MIN_EDGE_POINTS:
        fit_x, fit_y, radius, _rms = departed_ball_module._circle_fit(edge_x, edge_y)
        residual = np.abs(np.hypot(edge_x - fit_x, edge_y - fit_y) - radius)
        keep = residual <= max(
            departed_ball_module.EDGE_OUTLIER_MIN_PX,
            departed_ball_module.EDGE_OUTLIER_FACTOR * float(np.median(residual)),
        )
        if keep.sum() >= departed_ball_module.MIN_EDGE_POINTS:
            fit_x, fit_y, radius, _rms = departed_ball_module._circle_fit(
                edge_x[keep], edge_y[keep]
            )
        low, high = departed_ball_module.SIZE_RANGE
        if (
            low * diameter <= 2.0 * radius <= high * diameter
            and math.hypot(fit_x - centre, fit_y - centre)
            <= REFINE_MAX_EDGE_SHIFT_DIAMETERS * diameter
        ):
            return float(fit_x), float(fit_y)
    # pylint: enable=protected-access
    size = change.shape[0]
    yy, xx = np.mgrid[:size, :size]
    disk = np.hypot(xx - centre, yy - centre) <= diameter / 2.0
    weights = np.abs(change) * disk
    total = float(weights.sum())
    if total <= 0.0:
        return float(centre), float(centre)
    return float((weights * xx).sum() / total), float((weights * yy).sum() / total)


def refine_search_radius_px(diameter_px: float) -> float:
    """How far from its anchor ``refine_teed_ball`` looks for a ball of ``diameter_px``."""
    return max(REFINE_SEARCH_MIN_PX, REFINE_SEARCH_DIAMETERS * diameter_px)


def mark_reference_ball(
    mark_px: tuple[float, float] | None, geometry: CameraBallGeometry
) -> ReferenceBall | None:
    """The tapped ball mark (saved-frame px) as a resting ball of the lens's size at the tee.

    None without a mark or without a lens model (``geometry.intrinsics``): the
    mark is a pixel and carries no ball size of its own.
    """
    if mark_px is None or geometry.intrinsics is None:
        return None
    x, y = float(mark_px[0]), float(mark_px[1])
    # With a lens model the expected diameter needs no anchor size.
    diameter = expected_ball_diameter_px(geometry, math.nan)
    if not (math.isfinite(x + y) and math.isfinite(diameter) and diameter > 0.0):
        return None
    return ReferenceBall(x, y, diameter, int(round(math.pi * (diameter / 2.0) ** 2)))


def refine_teed_ball(
    frames: np.ndarray, anchor: ReferenceBall, geometry: CameraBallGeometry
) -> ReferenceBall | None:
    """The resting ball near ``anchor`` by disk-template matching on the pre-swing background.

    A disk of the expected diameter d (lens focal length x ball diameter / range)
    weighs +1, a ring 1-4 px outside it -1, only above ``REFINE_RING_BELOW_RADII``
    of the radius below the centre (the tee and the shadow hang below). The best
    match of either polarity within max(25 px, 1.5 d) of ``anchor`` on the median
    of the first 20 frames, refined to a contrast-weighted centroid inside the
    disk, is the ball, with diameter d. None below ``REFINE_MIN_CONTRAST`` grey
    levels of disk-minus-ring contrast or for unusable input. It fixes anchors
    that slid onto a ball+tee blob or a nearby false blob (path-woods study).
    """
    import cv2  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    stack = np.asarray(frames)
    if stack.ndim != 3 or len(stack) == 0:
        return None
    diameter = expected_ball_diameter_px(geometry, anchor.diameter_px)
    if not (math.isfinite(diameter) and diameter > 0.0 and math.isfinite(anchor.x + anchor.y)):
        return None
    radius = diameter / 2.0
    size = int(math.ceil(2.0 * radius + 10.0)) | 1
    centre = size // 2
    yy, xx = np.mgrid[:size, :size] - centre
    distance = np.hypot(xx, yy)
    disk = distance <= radius
    ring = (
        (distance > radius + REFINE_RING_PX[0])
        & (distance <= radius + REFINE_RING_PX[1])
        & (yy < radius * REFINE_RING_BELOW_RADII)
    )
    template = np.zeros((size, size), np.float32)
    template[disk] = 1.0 / disk.sum()
    template[ring] = -1.0 / ring.sum()
    background = np.median(stack[:REFINE_BACKGROUND_FRAMES], axis=0).astype(np.float32)
    height, width = background.shape
    reach = refine_search_radius_px(diameter)
    x0 = max(0, int(math.floor(anchor.x - reach)))
    x1 = min(width - 1, int(math.ceil(anchor.x + reach)))
    y0 = max(0, int(math.floor(anchor.y - reach)))
    y1 = min(height - 1, int(math.ceil(anchor.y + reach)))
    if x1 < x0 or y1 < y0:
        return None
    pad = centre + 2
    padded = np.pad(background, pad, mode="edge")
    region = padded[y0 : y1 + 2 * pad + 1, x0 : x1 + 2 * pad + 1]
    best: tuple[float, int, int, float] = (-math.inf, 0, 0, 1.0)
    for sign in (1.0, -1.0):
        response = cv2.filter2D(  # pylint: disable=no-member
            sign * region,
            -1,
            template,
            borderType=cv2.BORDER_REPLICATE,  # pylint: disable=no-member
        )[pad : pad + y1 - y0 + 1, pad : pad + x1 - x0 + 1]
        row, col = divmod(int(np.argmax(response)), response.shape[1])
        value = float(response[row, col])
        if value > best[0]:
            best = (value, col + x0, row + y0, sign)
    value, peak_x, peak_y, sign = best
    if not value >= REFINE_MIN_CONTRAST:
        return None
    window = padded[
        peak_y + pad - centre : peak_y + pad + centre + 1,
        peak_x + pad - centre : peak_x + pad + centre + 1,
    ]
    ring_level = float(np.median(window[ring]))
    centre_x, centre_y = _ball_edge_centre(window - ring_level, centre, diameter, value)
    centre_x, centre_y = centre_x + peak_x - centre, centre_y + peak_y - centre
    return ReferenceBall(
        x=centre_x,
        y=centre_y,
        diameter_px=float(diameter),
        area_px=int(round(math.pi * radius**2)),
    )


@dataclass(frozen=True)
class ClubBall:
    """The club data's resting ball, where it came from, and why anchors were dropped.

    ``source`` is the path's ``ball_source`` (``departed``, ``tracker`` or
    ``refined``); ``anchor`` where the anchor came from (``departed``, the
    session ``tracker`` or the tapped ``mark``; without a ball the last one
    tried), ``refined`` whether ``refine_teed_ball`` found a ball around the
    last anchor tried (also when that ball was then dropped). ``rejected``
    names, in order, why an implausible anchor or refined ball was dropped:
    ``tracker_`` or ``mark_``, then ``anchor_`` or ``refined_``, then
    ``outside_hitting_zone`` or ``far_from_mark``, or ``mark_refined_not_found``
    (the refine found no ball around the mark), or ``mark_anchor_club_may_be_teed``
    (a club that may be teed never anchors on the mark). A dropped tracker ball
    can leave the mark's ball standing; with no ball ``ball`` and ``source`` are
    None.
    """

    ball: ReferenceBall | None
    source: str | None
    anchor: str | None = None
    refined: bool = False
    rejected: tuple[str, ...] = ()

    def to_log(self) -> dict:
        """The session log's ``ball_anchor``, ``ball_refined`` and ``ball_rejected``."""
        return {
            "ball_anchor": self.anchor,
            "ball_refined": self.refined,
            "ball_rejected": list(self.rejected) or None,
        }


def _implausible_club_ball(
    ball: ReferenceBall, tee_zone: TeeZone | None, mark: ReferenceBall | None
) -> str | None:
    """Why ``ball`` cannot be this clip's resting ball, None when it can.

    Outside the capture mode's hitting zone (``tee_zone``), or farther than
    ``CLUB_BALL_MARK_MAX_SEARCH_RADII`` refine search radii from the tapped
    ``mark``. The geometry has no horizon of its own (the camera pitch comes
    from the ball): the zone's top edge already lies below the horizon of a
    camera pitched down by more than ~3.8 deg (``camera.tee_zone``).
    """
    if tee_zone is not None and not tee_zone.contains(ball.x, ball.y):
        return "outside_hitting_zone"
    if mark is not None:
        # The mark carries the lens's ball size, the one the refine searches with.
        bound = CLUB_BALL_MARK_MAX_SEARCH_RADII * refine_search_radius_px(mark.diameter_px)
        if math.hypot(ball.x - mark.x, ball.y - mark.y) > bound:
            return "far_from_mark"
    return None


def _club_ball_from(  # pylint: disable=too-many-arguments
    stack: np.ndarray,
    geometry: CameraBallGeometry,
    anchor: ReferenceBall,
    origin: str,
    *,
    tee_zone: TeeZone | None = None,
    mark: ReferenceBall | None = None,
) -> ClubBall:
    """``anchor`` (from ``origin``) refined; checked unless it is the departed ball."""
    checked = origin != "departed"
    if checked:
        rejected = _implausible_club_ball(anchor, tee_zone, mark)
        if rejected is not None:
            return ClubBall(None, None, origin, rejected=(f"{origin}_anchor_{rejected}",))
    refined = refine_teed_ball(stack, anchor, geometry)
    if refined is None:
        if origin == "mark":
            # The tap is where a ball lay when it was tapped, not this clip's ball:
            # a driver teed above it sits up to two diameters higher.
            return ClubBall(None, None, origin, rejected=("mark_refined_not_found",))
        return ClubBall(anchor, origin, origin)
    if checked:
        rejected = _implausible_club_ball(refined, tee_zone, mark)
        if rejected is not None:
            return ClubBall(
                None, None, origin, refined=True, rejected=(f"{origin}_refined_{rejected}",)
            )
    return ClubBall(refined, "refined", origin, refined=True)


def locate_club_ball(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    geometry: CameraBallGeometry,
    *,
    reference_ball: ReferenceBall | None = None,
    ball_tracker=None,
    departed_ball: ReferenceBall | None = None,
    tee_zone: TeeZone | None = None,
    teed: bool = False,
    club: ClubType | None = None,
) -> ClubBall:
    """The club data's resting ball (``resolve_club_ball``) with its anchor and checks.

    The anchor is ``departed_ball`` (this clip's ball as the ball flight's
    disappeared-ball locator found it: ``departed``), else detection plus
    ``ball_tracker`` (``tracker``), else ``reference_ball`` (the tapped ball
    mark, ``mark_reference_ball``: ``mark``). ``refine_teed_ball`` refines it
    for every club (``refined``); a departed or tracker anchor stays as it is
    when that finds nothing, the mark is then no ball. The locator's edge
    circle sits high and small on a teed ball (2026-10-25 drivers: d 13.1-13.5
    px against the lens's 16.4 px), so the departure and the path need the
    refined disk; the impact keeps ``departed_ball`` itself.

    A departed ball is this clip's ball and is never checked. The tracker's
    ball, and its refined ball, is dropped when implausible
    (``_implausible_club_ball``: outside ``tee_zone``, or farther than
    ``CLUB_BALL_MARK_MAX_SEARCH_RADII`` from the mark); the mark's anchor and
    refined ball when outside ``tee_zone``. A dropped tracker ball leaves the
    mark; nothing else stands in. 2026-11-02 S1: without a departed ball the
    tracker picked a blob on the tree line, ~100 px above the irons' ball, and
    the refine kept it; the mark's refine finds it. The drivers' teed balls sat
    16-30 px above the same mark: around the tap the refine stopped at the top
    row of its window (``refine_search_radius_px``) or found nothing, the
    tracker had them. So a club that may be teed (``club`` in
    ``WOODS_SEGMENTATION``: woods and hybrids, the driver included) never
    anchors on the mark, nor does a ``teed`` shot (the server's driver): the
    server calls only the driver teed, and a teed fairway wood or hybrid sits
    as far above a tap on the ground, where the refine latches onto the tee
    stem (``test_a_teed_ball_above_the_mark_stays_the_trackers``). For them the
    mark only bounds the tracker's ball; without a plausible tracker ball they
    have no ball (``mark_anchor_club_may_be_teed``). Irons and wedges keep the
    mark's anchor (2026-11-02 S1 #6/#7/#9).
    """
    stack = np.asarray(frames)
    if departed_ball is not None:
        return _club_ball_from(stack, geometry, departed_ball, "departed")
    dropped: tuple[str, ...] = ()
    if stack.ndim == 3 and len(stack) > 0:
        balls = _resolve_ball(stack, None, ball_tracker)
        if balls is not None:
            found = _club_ball_from(
                stack, geometry, balls[0], "tracker", tee_zone=tee_zone, mark=reference_ball
            )
            if found.ball is not None or reference_ball is None:
                return found
            dropped = found.rejected
    if reference_ball is None:
        return ClubBall(None, None)
    if teed or club in WOODS_SEGMENTATION:
        return ClubBall(None, None, "mark", rejected=dropped + ("mark_anchor_club_may_be_teed",))
    # A refine started at the mark stays within the mark bound by construction.
    found = _club_ball_from(stack, geometry, reference_ball, "mark", tee_zone=tee_zone)
    return replace(found, rejected=dropped + found.rejected)


def resolve_club_ball(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    geometry: CameraBallGeometry,
    *,
    reference_ball: ReferenceBall | None = None,
    ball_tracker=None,
    departed_ball: ReferenceBall | None = None,
    tee_zone: TeeZone | None = None,
    teed: bool = False,
    club: ClubType | None = None,
) -> tuple[ReferenceBall | None, str | None]:
    """The club data's resting ball and where it came from (``locate_club_ball``).

    (None, None) without any ball or when the anchor was dropped.
    """
    found = locate_club_ball(
        frames,
        geometry,
        reference_ball=reference_ball,
        ball_tracker=ball_tracker,
        departed_ball=departed_ball,
        tee_zone=tee_zone,
        teed=teed,
        club=club,
    )
    return found.ball, found.source


def estimate_departure_contact(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    trigger_ns: int,
    tee_range_m: float,
    geometry: CameraBallGeometry,
    ball: ReferenceBall,
) -> DepartureFit | None:
    """Gate-free camera ball departure (``ball_flight.fit_ball_departure``) for the contact time.

    The flight frames start one frame before the stamp's acoustic estimate
    (trigger minus sound travel): a wood's ball can show in the frame
    straddling it.
    """
    times_ns = np.asarray(timestamps_ns, dtype=np.int64)
    acoustic_s = trigger_ns / 1e9 - tee_range_m / SPEED_OF_SOUND_M_S
    start = max(0, int(np.searchsorted(times_ns / 1e9, acoustic_s)) - 1)
    return fit_ball_departure(frames, times_ns, start_index=start, geometry=geometry, ball=ball)


@dataclass(frozen=True)
class ClubDataResult:
    """One clip's club data: the contact time, the camera departure and the head path.

    ``stage_elapsed_s`` holds the seconds spent in ``departure``, ``path``,
    ``attack``, ``impact`` (E) and ``impact_point`` (D); ``ball`` is how the
    resting ball was resolved (``locate_club_ball``); ``contact_shadow`` the
    shadow contact time (``shadow_contact_time``), logged only.
    """

    contact: ContactTime
    camera_departure: DepartureFit | None
    path: ClubHeadPathResult
    stage_elapsed_s: dict[str, float] = field(default_factory=dict)
    ball: ClubBall | None = None
    contact_shadow: ContactShadow | None = None


def estimate_club_data(  # pylint: disable=too-many-arguments,too-many-locals
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    time_zero: CameraTimeZero | None,
    trigger_ns: int,
    tee_range_m: float,
    geometry: CameraBallGeometry,
    club_speed_mph: float | None,
    trigger_lag_ms: float | None = None,
    lag_reference: str | None = None,
    teed: bool = False,
    acoustic_delay_ms: float | None = None,
    club: ClubType | None = None,
    handedness: str = "right",
    reference_ball: ReferenceBall | None = None,
    ball_tracker=None,
    departed_ball: ReferenceBall | None = None,
    confirmed_tee: ReferenceBall | None = None,
    head_length_median_mm: float | None = None,
    ball_speed_mph: float | None = None,
    launch_vertical_deg: float | None = None,
    launch_horizontal_deg: float | None = None,
    impact_point_head_length_median_mm: float | None = None,
    tee_zone: TeeZone | None = None,
) -> ClubDataResult:
    """Contact time, path, attack angle and impact of one clip (the worker's ``club_data`` job).

    1. resolves the ball once (``locate_club_ball``): the ball flight's
       ``departed_ball``, else the tracker's ball or ``reference_ball`` (the
       tapped ball mark), refined (the impact keeps ``departed_ball`` as it
       is); a tracker ball outside ``tee_zone`` or far from the mark gives way
       to the mark, a mark outside ``tee_zone``, without a refined ball, on a
       ``teed`` shot or for a ``club`` that may be teed (woods and hybrids, the
       driver included) is no ball (``rejected_reference_ball_not_found``);
    2. runs the gate-free camera departure (``estimate_departure_contact``) only
       when the live time zero is not a departure fit;
    3. selects the contact time (``select_contact_time``; reason
       ``no_camera_departure`` when the departure ran and found none) and
       judges its shadow (``shadow_contact_time``, logged only);
    4. runs ``estimate_club_head_path`` with that ball and contact, the impact
       point across contact (method D) included: the shot's ``ball_speed_mph``
       and launch angles blank the departing ball, the session median
       ``impact_point_head_length_median_mm`` gates its head length (its scale
       is the regulation ball at the known range, never measured).
    """
    stage = {"departure": 0.0, "path": 0.0, "attack": 0.0, "impact": 0.0, "impact_point": 0.0}
    started = time.perf_counter()
    stack = np.asarray(frames)
    times_ns = np.asarray(timestamps_ns, dtype=np.int64)
    club_ball = locate_club_ball(
        stack,
        geometry,
        reference_ball=reference_ball,
        ball_tracker=ball_tracker,
        departed_ball=departed_ball,
        tee_zone=tee_zone,
        teed=teed,
        club=club,
    )
    ball, ball_source = club_ball.ball, club_ball.source
    stage["path"] = time.perf_counter() - started
    camera_departure = None
    searched = False
    if ball is not None and (time_zero is None or time_zero.source != "departure_fit"):
        departure_started = time.perf_counter()
        camera_departure = estimate_departure_contact(
            stack,
            times_ns,
            trigger_ns=trigger_ns,
            tee_range_m=tee_range_m,
            geometry=geometry,
            ball=ball,
        )
        searched = True
        stage["departure"] = time.perf_counter() - departure_started
    contact = select_contact_time(
        time_zero,
        trigger_ns=trigger_ns,
        tee_range_m=tee_range_m,
        trigger_lag_ms=trigger_lag_ms,
        lag_reference=lag_reference,
        camera_departure_ms=(
            None if camera_departure is None else 1e3 * (camera_departure.time_s - trigger_ns / 1e9)
        ),
        teed=teed,
        acoustic_delay_ms=acoustic_delay_ms,
    )
    # Judged on the reason select_contact_time gave, before the rename below; the
    # contact itself stays as selected.
    contact_shadow = shadow_contact_time(contact, time_zero, teed=teed)
    if searched and camera_departure is None:
        contact = replace(contact, reason="no_camera_departure")
    if ball is None and club_ball.rejected:
        # A dropped anchor is no ball: the path must not detect one of its own.
        path = _early_result(
            _input_gate(stack, times_ns / 1e9, contact.time_s, club_speed_mph)
            or "rejected_reference_ball_not_found",
            feature=DEFAULT_FEATURE,
            attack=True,
            handedness=handedness,
            ball_source=None,
        )
        return ClubDataResult(contact, camera_departure, path, stage, club_ball, contact_shadow)
    timings: dict[str, float] = {}
    path = estimate_club_head_path(
        stack,
        times_ns,
        contact_time_s=contact.time_s,
        geometry=geometry,
        club_speed_mph=club_speed_mph,
        reference_ball=ball,
        ball_tracker=None if ball is not None else ball_tracker,
        contact_sigma_s=contact.sigma_s,
        club=club,
        handedness=handedness,
        departed_ball=departed_ball,
        confirmed_tee=confirmed_tee,
        head_length_median_mm=head_length_median_mm,
        ball_source=ball_source,
        timings=timings,
        ball_speed_mph=ball_speed_mph,
        launch_vertical_deg=launch_vertical_deg,
        launch_horizontal_deg=launch_horizontal_deg,
        impact_point_head_length_median_mm=impact_point_head_length_median_mm,
    )
    stage["path"] += timings.get("path", 0.0)
    for name in ("attack", "impact", "impact_point"):
        stage[name] = timings.get(name, 0.0)
    return ClubDataResult(contact, camera_departure, path, stage, club_ball, contact_shadow)
