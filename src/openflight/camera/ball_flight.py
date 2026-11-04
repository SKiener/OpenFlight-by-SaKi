"""Experimental rear-camera horizontal ball-flight reconstruction.

Camera centroids provide ball bearing. IWR6843 range is the preferred metric
depth source; apparent regulation-ball size provides a lower-confidence camera-
only fallback. OPS ball speed gates target identity. IWR horizontal remains an
independent comparison and fallback.

Two ball locators set the departure origin (``ball_locator``):

* ``disappeared`` (default): the resting ball is what disappeared at the strike
  (``openflight.camera.departed_ball``), inside the hitting zone. Its gates are
  relative to the clip's own noise, the camera pose comes from untee'd balls
  only (``GroundBallAnchor``), the tee column below a teed ball holds no flight
  candidates, and with an OPS ball speed every candidate must be reachable from
  the tee at that speed in a direction that agrees with its track
  (``_MotionGate``). Image work reads the hitting zone and the flight corridor
  above it only. When the locator finds no ball, the legacy locator runs as a
  fallback, unchanged. A track measured from the departed ball must also be
  explained by a straight line at the OPS speed from the resting ball within
  ``LINE_FIT_MAX_RMS_MRAD`` (independent of the IWR), or the direction is
  withheld (``rejected_line_fit_residual``).
* ``legacy``: ``detect_reference_ball`` plus the session ``ReferenceBallTracker``
  and the fixed 20 (contrast) / 30 (departure) grey-level thresholds, as before.

``CameraBallEstimate.locator`` reports which locator ran and what it measured.

A resting ball tapped in the Align view (``ball_mark_px``, see
``openflight.angle_reference.BallMark``) bounds both locators to the window
around it (``departed_ball.mark_window``): the disappeared-ball search box is
that window, the legacy detector reads only it, and a last ball, session tee
anchor or ground anchor (the pose) outside it never stands in for this shot's
ball. So a session anchor that drifted onto a tree crown (2026-10-25) cannot
place the ball. Inside the window the locators find each shot's ball as
without a mark, and the pose and the start direction are computed from it
exactly as without one; without a mark nothing changes.

A target tapped in the Align view (``CameraBallGeometry.target_px``, see
``openflight.angle_reference``) sets the line every start direction is measured
from: its azimuth under the shot's camera pose is subtracted (and reported as
``CameraBallEstimate.target_azimuth_deg``) instead of adding
``horizontal_offset_deg``.
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import asdict, dataclass, field, replace
from typing import NamedTuple

import numpy as np

from openflight.camera.club_delivery import _contact_time_s, _detect_impact_index
from openflight.camera.club_motion import (
    BALL_DIAMETER_MM,
    ReferenceBall,
    detect_reference_ball,
)
from openflight.camera.departed_ball import (
    DepartedBall,
    GroundBallAnchor,
    departure_index,
    in_window,
    locate_departed_ball,
    mark_window,
    search_box,
)
from openflight.camera.flicker import FLICKER_MIN_REL_STD, compensate_ball_flicker
from openflight.camera.geometry import (
    camera_to_world,
    normalized_image_offsets,
    reference_ball_pose,
    reference_focal_px,
)
from openflight.camera.intrinsics import CameraIntrinsics
from openflight.camera.tee_zone import TeeZone

MPH_PER_MS = 2.23694
BALL_LOCATORS = ("disappeared", "legacy")
DEFAULT_BALL_LOCATOR = "disappeared"
SWEEP_BRIGHT_THRESHOLDS = (100, 115, 130)
SWEEP_DIFFERENCE_THRESHOLDS = (12, 18, 24)
SWEEP_MIN_AREAS = (5, 10, 20)
PARAMETER_SWEEP_SIZE = 27
# The estimator's acceptance box for a track's direction.
HORIZONTAL_BOUNDS_DEG = (-30.0, 30.0)
VERTICAL_BOUNDS_DEG = (-5.0, 55.0)
# Flight candidates lie within this many pixels of the ball's column, between
# FLIGHT_TOP_PX and FLIGHT_BELOW_PX below the ball.
FLIGHT_HALF_WIDTH_PX = 160.0
FLIGHT_TOP_PX = 10.0
FLIGHT_BELOW_PX = 15.0
MAX_CANDIDATE_AREA_PX = 400
# The flight corridor crop reaches this far beyond the candidate box, so a
# component whose centroid lies inside it is read whole.
CORRIDOR_MARGIN_PX = 48
# Tracks start near the ball within the first frames of the flight window.
PATH_START_FRAMES = 5
PATH_START_RADIUS_PX = 70.0
# With the OPS motion gate a track may start later and farther out: the club
# head covers a low 7-wood ball for 6-8 frames (2026-10-25).
GATED_PATH_START_FRAMES = 10
GATED_PATH_START_RADIUS_PX = 160.0
MOTION_GATE_DIRECTION_TOL_DEG = 6.0
MOTION_GATE_RADII = 7
MOTION_GATE_MIN_RADIUS_M = 0.02
LINE_FIT_MAX_RMS_MRAD = 3.0
LINE_FIT_T0_BOUNDS_S = (-0.010, 0.010)
LINE_FIT_START_HORIZONTAL_DEG = (-10.0, 0.0, 10.0)
LINE_FIT_START_VERTICAL_DEG = (5.0, 15.0, 30.0)
LINE_FIT_BOUNDS_DEG = ((-45.0, -10.0), (45.0, 70.0))
LINE_FIT_LOSS_SCALE_RAD = 2e-3
FLIGHT_WINDOW_FRAMES = 15
MAX_IWR_FALLBACK_ABS_DEG = 20.0
# Dry air near 20 C; the microphone sits in the front panel beside the radar,
# so the radar-to-tee slant range is also the sound path.
SPEED_OF_SOUND_M_S = 343.0
DEPARTURE_FIT_POINTS = 6
DEPARTURE_FIT_MIN_POINTS = 4
DEPARTURE_FIT_MAX_RMS_PX = 0.6
DEPARTURE_FIT_MAX_SPAN_FRAMES = 4.5
# Gate and GPIO-callback latency let impact lead trigger-minus-sound-travel;
# only fit and sound-path errors let it lag.
DEPARTURE_MAX_LEAD_S = 5e-3
DEPARTURE_MAX_LAG_S = 1.5e-3
_ASYMPTOTE_LEADS_S = np.geomspace(2e-3, 5.0, 600)


@dataclass(frozen=True)
class BallCandidate:
    """One ball-like connected component in a camera frame."""

    x: float
    y: float
    area: int
    width: int
    height: int
    fill: float
    circularity: float
    mean_intensity: float
    local_contrast: float = 0.0


@dataclass(frozen=True)
class CameraBallGeometry:
    """Measured geometry shared by the rear camera and IWR6843."""

    camera_height_m: float
    radar_height_m: float
    tee_range_m: float
    # Ball-centre height above the floor (a teed ball's includes its tee).
    ball_height_m: float
    # Camera optical-center position relative to radar center. Positive is
    # target-right when viewed from behind the sensors looking downrange.
    camera_lateral_offset_m: float = 0.0
    # Positive when the camera sits downrange of the radar centre.
    camera_forward_offset_m: float = 0.0
    horizontal_offset_deg: float = 0.0
    # Convert saved-image horizontal pixels back to physical target direction.
    # Mirrored operator previews use -1; unmirrored captures use +1.
    horizontal_pixel_sign: float = 1.0
    roll_correction_deg: float = 0.0
    ball_diameter_m: float = BALL_DIAMETER_MM / 1000.0
    image_width_px: int = 640
    image_height_px: int = 400
    intrinsics: CameraIntrinsics | None = None
    # Derive camera yaw from the teed ball (ball on the radar centreline)
    # instead of assuming the optical axis is parallel to the target line.
    yaw_from_reference_ball: bool = False
    # The tapped target (``openflight.angle_reference``) in saved-frame pixels.
    # When set, start directions are reported relative to the line towards it,
    # in each shot's own camera pose; it replaces ``horizontal_offset_deg`` and
    # cancels the yaw from the reference ball. The target is assumed far enough
    # (> 30 m) for the ball-to-camera parallax to vanish.
    target_px: tuple[float, float] | None = None
    # Height of the ball's bottom above the floor (the tee; 0 on the mat), so
    # the ground plane stays at ``ball_height_m - ball_diameter_m / 2 - tee_height_m``.
    tee_height_m: float = 0.0

    @property
    def ball_forward_m(self) -> float:
        """Forward radar-to-ball distance derived from tee slant range."""
        vertical = self.ball_height_m - self.radar_height_m
        return math.sqrt(max(self.tee_range_m**2 - vertical**2, 1e-9))

    @property
    def camera_ball_forward_m(self) -> float:
        """Forward camera-to-ball distance."""
        return self.ball_forward_m - self.camera_forward_offset_m

    @property
    def camera_origin(self) -> np.ndarray:
        """Camera origin in the radar-centered world coordinate system."""
        return np.array(
            [self.camera_lateral_offset_m, self.camera_forward_offset_m, self.camera_height_m]
        )


@dataclass(frozen=True)
class DepartureFit:
    """Ball departure back-extrapolated from its image track (camera clock)."""

    time_s: float
    n_points: int
    rms_px: float
    span_frames: float


@dataclass(frozen=True)
class CameraTimeZero:
    """Camera time zero for the IWR range lookup; ``*_ms`` are ms from the trigger."""

    source: str
    time_zero_ms: float
    departure_fit_ms: float | None
    acoustic_ms: float
    # None without a departure frame (JSON has no NaN); both locators reject that case.
    tee_patch_ms: float | None
    iwr_impact_t_s: float | None
    departure_fit_points: int = 0
    departure_fit_rms_px: float | None = None
    departure_fit_span_frames: float | None = None
    departure_fit_support: int = 0
    selected_paths: int = 0


@dataclass(frozen=True)
class LineFit:
    """Straight line at the OPS speed from the resting ball through a track's rays.

    ``time_zero_ms`` is the fitted departure from the acoustic impact estimate;
    the residuals are the angular misses (mrad) of the track's camera rays.
    """

    horizontal_deg: float
    vertical_deg: float
    time_zero_ms: float
    rms_mrad: float
    max_mrad: float
    n_points: int


@dataclass(frozen=True)
class BallLocatorDiagnostics:
    """Which ball locator ran, what it measured and the TI-free quality checks.

    ``locator`` is ``disappeared``, ``legacy`` or ``legacy_fallback`` (the
    disappeared locator found no ball; ``reason`` says why). Ball fields are
    full-frame pixels and grey levels of the departed ball when one was found.
    ``motion_gate`` is ``applied``, ``no_ops_speed`` or None (legacy track
    search); ``gate_candidates``/``gate_kept`` count the sweep's flight
    candidates before and after it. ``line_fit_status`` is ``ok``,
    ``over_residual`` (direction withheld), ``no_ops_speed``, ``no_track`` or
    ``failed``; None when the legacy locator ran (legacy mode or fallback).
    ``ball_mark`` is the tapped resting ball the search was bounded by, if any
    (``search_box_source`` is then ``ball_mark``). ``teed``/``tee_px`` are the
    departed ball's own tee rows; ``ground_anchor_sample`` is whether the ball
    fed the ground anchor (an untee'd ball off a tee: neither its tee rows nor
    its geometry, a driver on its profile tee, put it on one). ``flicker`` is
    the live flicker step's ``FlickerMeasurement.to_dict()``
    (``estimate_live_ball_flight``), None where it did not run.
    """

    locator: str
    reason: str
    ball_x: float | None = None
    ball_y: float | None = None
    ball_diameter_px: float | None = None
    contrast: float | None = None
    noise: float | None = None
    snr: float | None = None
    polarity: int | None = None
    teed: bool | None = None
    tee_px: float | None = None
    search_box: tuple[int, int, int, int] | None = None
    search_box_source: str | None = None
    pose_source: str | None = None
    pose_x: float | None = None
    pose_y: float | None = None
    motion_gate: str | None = None
    gate_candidates: int | None = None
    gate_kept: int | None = None
    line_fit_status: str | None = None
    line_fit: LineFit | None = None
    ball_mark: tuple[float, float] | None = None
    ground_anchor_sample: bool | None = None
    # The flicker step's measurement; it holds a wall time, so not part of the identity.
    flicker: dict | None = field(default=None, compare=False)
    # Wall time of the whole estimate; not part of the result's identity.
    elapsed_ms: float | None = field(default=None, compare=False)

    def to_dict(self) -> dict:
        """JSON-ready form for the session log, floats rounded, without the wall time."""
        data = asdict(self)
        del data["elapsed_ms"]
        for key, value in data.items():
            if isinstance(value, float):
                data[key] = round(value, 3)
        if data["search_box"] is not None:
            data["search_box"] = list(data["search_box"])
        if data["ball_mark"] is not None:
            data["ball_mark"] = [round(float(value), 3) for value in data["ball_mark"]]
        if data["line_fit"] is not None:
            data["line_fit"] = {
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in data["line_fit"].items()
            }
        return data


@dataclass(frozen=True)
class CameraBallEstimate:
    """Consensus result from the camera/IWR/OPS ball-flight estimator."""

    status: str
    confidence_tier: str = "withheld"
    horizontal_deg: float | None = None
    vertical_deg: float | None = None
    support: int = 0
    support_pct: float = 0.0
    parameter_mad_deg: float | None = None
    window_mad_deg: float | None = None
    speed_mph: float | None = None
    speed_error_mph: float | None = None
    n_points: int = 0
    first_frame: int | None = None
    last_frame: int | None = None
    depth_source: str | None = None
    time_zero: CameraTimeZero | None = None
    locator: BallLocatorDiagnostics | None = None
    # The tapped target's azimuth in this shot's camera pose (the start direction
    # is reported relative to it); None without a target line.
    target_azimuth_deg: float | None = None


@dataclass(frozen=True)
class HorizontalFusionDecision:
    """Selected horizontal result plus independent sensor provenance."""

    selected_deg: float | None
    source: str | None
    confidence: float | None
    status: str
    iwr_horizontal_deg: float | None
    camera_horizontal_deg: float | None
    camera_iwr_delta_deg: float | None


@dataclass(frozen=True)
class _PathEstimate:
    horizontal_deg: float
    vertical_deg: float
    speed_mph: float
    speed_error_mph: float
    fit_median_m: float
    step_speed_mad_mph: float
    window_mad_deg: float
    n_points: int
    first_frame: int
    last_frame: int


def _normalized(
    x_px: float, y_px: float, *, focal_px: float, geometry: CameraBallGeometry
) -> tuple[float, float]:
    return normalized_image_offsets(
        x_px,
        y_px,
        focal_px=focal_px,
        image_width_px=geometry.image_width_px,
        image_height_px=geometry.image_height_px,
        horizontal_pixel_sign=geometry.horizontal_pixel_sign,
        roll_correction_deg=geometry.roll_correction_deg,
        intrinsics=geometry.intrinsics,
    )


class _CameraModel(NamedTuple):
    focal_px: float
    pitch_rad: float
    radar_from_camera: np.ndarray
    yaw_rad: float
    # Added to every start direction: geometry.horizontal_offset_deg, or minus
    # the tapped target's azimuth (target_azimuth_deg) when there is a target.
    direction_offset_deg: float = 0.0
    target_azimuth_deg: float | None = None


def _target_azimuth_deg(
    target_px: tuple[float, float],
    *,
    focal_px: float,
    pitch_rad: float,
    yaw_rad: float,
    geometry: CameraBallGeometry,
) -> float:
    """World azimuth of the ray through the tapped target, in the frame of the start directions."""
    image_x, image_z = _normalized(*target_px, focal_px=focal_px, geometry=geometry)
    lateral, forward, _vertical = camera_to_world(
        image_x, 1.0, image_z, pitch_rad=pitch_rad, yaw_rad=yaw_rad
    )
    return math.degrees(math.atan2(float(lateral), float(forward)))


def _camera_model(
    anchor: ReferenceBall,
    geometry: CameraBallGeometry,
) -> _CameraModel:
    """Infer focal scale and pose from the stationary regulation-size ball.

    With a tapped target the direction offset is minus its azimuth under this
    pose, so a start direction is measured from the target line (a constant
    camera yaw cancels).
    """
    camera_ball_range = math.sqrt(
        geometry.camera_lateral_offset_m**2
        + geometry.camera_ball_forward_m**2
        + (geometry.ball_height_m - geometry.camera_height_m) ** 2
    )
    focal_px = reference_focal_px(
        anchor.diameter_px, camera_ball_range, geometry.ball_diameter_m, geometry.intrinsics
    )
    ball_x, ball_z = _normalized(anchor.x, anchor.y, focal_px=focal_px, geometry=geometry)
    yaw, pitch = reference_ball_pose(
        ball_x,
        ball_z,
        camera_lateral_offset_m=geometry.camera_lateral_offset_m,
        camera_ball_forward_m=geometry.camera_ball_forward_m,
        height_delta_m=geometry.ball_height_m - geometry.camera_height_m,
    )
    radar_from_camera = geometry.camera_origin - np.array([0.0, 0.0, geometry.radar_height_m])
    yaw = yaw if geometry.yaw_from_reference_ball else 0.0
    target_azimuth = (
        None
        if geometry.target_px is None
        else _target_azimuth_deg(
            geometry.target_px, focal_px=focal_px, pitch_rad=pitch, yaw_rad=yaw, geometry=geometry
        )
    )
    return _CameraModel(
        focal_px,
        pitch,
        radar_from_camera,
        yaw,
        direction_offset_deg=(
            geometry.horizontal_offset_deg if target_azimuth is None else -target_azimuth
        ),
        target_azimuth_deg=target_azimuth,
    )


def _project(
    candidate: BallCandidate,
    radar_range_m: float,
    *,
    model: _CameraModel,
    geometry: CameraBallGeometry,
    ray: np.ndarray | None = None,
) -> np.ndarray | None:
    radar_from_camera = model.radar_from_camera
    if ray is None:
        ray = _camera_ray(candidate, model=model, geometry=geometry)
    ray_offset = float(ray @ radar_from_camera)
    discriminant = ray_offset**2 - (float(radar_from_camera @ radar_from_camera) - radar_range_m**2)
    if discriminant < 0.0:
        return None
    distance = -ray_offset + math.sqrt(discriminant)
    if distance <= 0.0:
        return None
    return geometry.camera_origin + distance * ray


def _camera_ray(
    candidate: BallCandidate,
    *,
    model: _CameraModel,
    geometry: CameraBallGeometry,
) -> np.ndarray:
    """Return the unit camera ray through a detected ball centroid."""
    image_x, image_z = _normalized(
        candidate.x, candidate.y, focal_px=model.focal_px, geometry=geometry
    )
    ray = np.array(
        camera_to_world(image_x, 1.0, image_z, pitch_rad=model.pitch_rad, yaw_rad=model.yaw_rad)
    )
    ray /= np.linalg.norm(ray)
    return ray


def _camera_rays(
    x_px: np.ndarray,
    y_px: np.ndarray,
    *,
    model: _CameraModel,
    geometry: CameraBallGeometry,
) -> np.ndarray:
    """Unit camera rays (n, 3) through many pixels at once (``_camera_ray`` vectorised)."""
    image_x, image_z = _normalized(
        np.asarray(x_px, dtype=float),
        np.asarray(y_px, dtype=float),
        focal_px=model.focal_px,
        geometry=geometry,
    )
    lateral, forward, vertical = camera_to_world(
        image_x, np.ones_like(image_x), image_z, pitch_rad=model.pitch_rad, yaw_rad=model.yaw_rad
    )
    rays = np.stack([lateral, forward, vertical], axis=-1)
    return rays / np.linalg.norm(rays, axis=-1, keepdims=True)


def _resting_ball_point(
    ball: ReferenceBall, *, model: _CameraModel, geometry: CameraBallGeometry
) -> np.ndarray:
    """The resting ball in world metres: its ray at the known camera-to-ball forward distance."""
    ray = _camera_rays(np.array([ball.x]), np.array([ball.y]), model=model, geometry=geometry)[0]
    return geometry.camera_origin + (geometry.camera_ball_forward_m / ray[1]) * ray


def _project_from_ball_size(
    candidate: BallCandidate,
    *,
    model: _CameraModel,
    geometry: CameraBallGeometry,
    ray: np.ndarray | None = None,
) -> np.ndarray | None:
    """Project a regulation ball using apparent diameter as camera depth."""
    measured_diameter_px = math.sqrt(4.0 * candidate.area / math.pi)
    if measured_diameter_px <= 0.0:
        return None
    camera_range_m = model.focal_px * geometry.ball_diameter_m / measured_diameter_px
    if not 0.25 <= camera_range_m <= 15.0:
        return None
    if ray is None:
        ray = _camera_ray(candidate, model=model, geometry=geometry)
    return geometry.camera_origin + camera_range_m * ray


def _local_contrast(frame: np.ndarray, left: int, top: int, mask: np.ndarray) -> float:
    height, width = mask.shape
    padding = max(2, round(max(width, height) / 2))
    x0, x1 = max(0, left - padding), min(frame.shape[1], left + width + padding)
    y0, y1 = max(0, top - padding), min(frame.shape[0], top + height + padding)
    region = frame[y0:y1, x0:x1]
    ring = np.ones(region.shape, dtype=bool)
    ring[top - y0 : top - y0 + height, left - x0 : left - x0 + width] = False
    pixels = frame[top : top + height, left : left + width][mask > 0]
    if not pixels.size or np.count_nonzero(ring) < 4:
        return 0.0
    return abs(float(np.median(pixels)) - float(np.median(region[ring])))


def _grey_difference_median(difference: np.ndarray) -> float:
    """``np.median`` of a difference of two uint8 images, from its histogram (exact)."""
    counts = np.cumsum(np.bincount((difference.astype(np.int32) + 255).ravel(), minlength=511))
    total = int(counts[-1])
    lower = int(np.searchsorted(counts, (total + 1) // 2))
    upper = int(np.searchsorted(counts, total // 2 + 1))
    return (lower + upper) / 2.0 - 255.0


def _sweep_candidates(  # pylint: disable=too-many-arguments
    frame: np.ndarray,
    background: np.ndarray,
    anchor: ReferenceBall,
    *,
    thresholds: tuple[tuple[int, int], ...],
    min_area: int,
    origin: tuple[int, int] = (0, 0),
    tee_column: bool = False,
) -> dict[tuple[int, int], list[BallCandidate]]:
    """Ball-like components per (bright, difference) threshold pair, in full-frame pixels.

    ``frame`` and ``background`` may be a crop whose top-left pixel sits at
    ``origin`` (x, y) in the full frame; the difference and its median are taken
    over what is given, once for every pair. ``min_area`` is the smallest area
    kept (a larger minimum filters the result). With ``tee_column`` the column
    below a teed ball holds no candidates: the rubber tee flexes after impact.
    """
    try:
        import cv2  # noqa: PLC0415  pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - optional hardware dependency
        raise RuntimeError("camera ball flight requires OpenCV") from exc

    difference = frame.astype(np.int16) - background.astype(np.int16)
    difference = difference - _grey_difference_median(difference)
    offset_x, offset_y = origin
    found: dict[tuple[int, int], list[BallCandidate]] = {}
    for bright_threshold, difference_threshold in thresholds:
        mask = (
            ((frame > bright_threshold) & (difference > difference_threshold))
            | ((frame < 255 - bright_threshold) & (difference < -difference_threshold))
        ).astype(np.uint8)
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
        candidates: list[BallCandidate] = []
        for label in range(1, count):
            _, _, width, height, area = stats[label]
            x, y = centroids[label]
            x, y = x + offset_x, y + offset_y
            aspect = width / max(height, 1)
            fill = area / max(width * height, 1)
            if not (
                min_area <= area <= MAX_CANDIDATE_AREA_PX
                and 0.35 <= aspect <= 2.8
                and fill >= 0.18
                and abs(x - anchor.x) < FLIGHT_HALF_WIDTH_PX
                and FLIGHT_TOP_PX < y < anchor.y + FLIGHT_BELOW_PX
                # Touching the resting ball: the club head at contact, not the ball in flight.
                and math.hypot(x - anchor.x, y - anchor.y) >= anchor.diameter_px
            ):
                continue
            if tee_column and abs(x - anchor.x) < anchor.diameter_px and y > anchor.y:
                continue
            left = int(stats[label][cv2.CC_STAT_LEFT])
            top = int(stats[label][cv2.CC_STAT_TOP])
            roi = (labels[top : top + height, left : left + width] == label).astype(np.uint8)
            contrast = _local_contrast(frame, left, top, roi)
            if contrast < difference_threshold:
                continue
            contours, _ = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            perimeter = sum(cv2.arcLength(contour, True) for contour in contours)
            circularity = 4.0 * math.pi * area / perimeter**2 if perimeter > 0.0 else 0.0
            pixels = frame[top : top + height, left : left + width][roi > 0]
            candidates.append(
                BallCandidate(
                    x=float(x),
                    y=float(y),
                    area=int(area),
                    width=int(width),
                    height=int(height),
                    fill=float(fill),
                    circularity=float(circularity),
                    mean_intensity=float(np.mean(pixels)),
                    local_contrast=contrast,
                )
            )
        found[(bright_threshold, difference_threshold)] = candidates
    return found


def _candidates(
    frame: np.ndarray,
    background: np.ndarray,
    anchor: ReferenceBall,
    *,
    bright_threshold: int,
    difference_threshold: int,
    min_area: int,
) -> list[BallCandidate]:
    pair = (bright_threshold, difference_threshold)
    return _sweep_candidates(frame, background, anchor, thresholds=(pair,), min_area=min_area)[pair]


def _sorted_median(values: list[float]) -> float:
    """Median of an already sorted list, as ``statistics.median`` computes it."""
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2.0


def _median(values) -> float:
    """``np.median`` of a short 1-D sequence without numpy's per-call overhead."""
    if isinstance(values, np.ndarray):
        values = values.tolist()
    return float(_sorted_median(sorted(values)))


def _rough_path_score(path: list[tuple[int, BallCandidate]]) -> float:
    if len(path) < 3:
        return 20.0 * len(path)
    steps = [
        ((second.x - first.x) / (j - i), (second.y - first.y) / (j - i))
        for (i, first), (j, second) in zip(path, path[1:])
    ]
    median_x = _sorted_median(sorted(step[0] for step in steps))
    median_y = _sorted_median(sorted(step[1] for step in steps))
    dispersion = _sorted_median(
        sorted(math.hypot(step_x - median_x, step_y - median_y) for step_x, step_y in steps)
    )
    return 20.0 * len(path) - 2.0 * dispersion


# Admissible launch directions of one candidate: (horizontal, vertical) deg pairs.
_Directions = tuple[tuple[float, float], ...]
# A track under construction plus the sorted horizontal and vertical directions
# of its nodes (empty without the motion gate).
_Track = tuple[list[tuple[int, BallCandidate]], list[float], list[float]]


def _path_starts(
    nodes: list[list[BallCandidate]],
    anchor: ReferenceBall,
    directions: list[list[_Directions]] | None,
) -> list[_Track]:
    gated = directions is not None
    start_frames = GATED_PATH_START_FRAMES if gated else PATH_START_FRAMES
    start_radius = GATED_PATH_START_RADIUS_PX if gated else PATH_START_RADIUS_PX
    starts: list[_Track] = []
    for frame in range(min(start_frames, len(nodes))):
        for index, candidate in enumerate(nodes[frame]):
            options = directions[frame][index] if gated else ()
            if (options or not gated) and math.hypot(
                candidate.x - anchor.x, candidate.y - anchor.y
            ) <= start_radius:
                starts.append(
                    (
                        [(frame, candidate)],
                        sorted(h for h, _v in options),
                        sorted(v for _h, v in options),
                    )
                )
    return starts


def _grown(track: _Track, node: tuple[int, BallCandidate], options: _Directions | None):
    """``track`` plus ``node``; None when the node's directions disagree with the track's."""
    path, path_h, path_v = track
    if options is None:
        return [*path, node], path_h, path_v
    median_h, median_v = _sorted_median(path_h), _sorted_median(path_v)
    if not any(
        abs(h - median_h) <= MOTION_GATE_DIRECTION_TOL_DEG
        and abs(v - median_v) <= MOTION_GATE_DIRECTION_TOL_DEG
        for h, v in options
    ):
        return None
    return (
        [*path, node],
        sorted(path_h + [h for h, _v in options]),
        sorted(path_v + [v for _h, v in options]),
    )


def _pixel_paths(
    nodes: list[list[BallCandidate]],
    anchor: ReferenceBall,
    directions: list[list[_Directions]] | None = None,
) -> list[list[tuple[int, BallCandidate]]]:
    """Candidate tracks receding from the ball, best rough score first.

    ``directions`` (the motion gate's admissible launch directions per
    candidate, aligned with ``nodes``) gate the search: a candidate without one
    is no node, a track may start in the first GATED_PATH_START_FRAMES frames
    within GATED_PATH_START_RADIUS_PX of the ball, and it grows only with a
    candidate that has a direction within MOTION_GATE_DIRECTION_TOL_DEG (both
    angles) of the median of all directions of its nodes so far.
    """
    frontier = _path_starts(nodes, anchor, directions)
    all_paths = [path for path, _h, _v in frontier]
    # Rough score of each path in all_paths, computed once.
    scored = [_rough_path_score(path) for path in all_paths]
    for _ in range(len(nodes)):
        extended: list[_Track] = []
        for track in frontier:
            previous_frame, previous = track[0][-1]
            for frame in range(previous_frame + 1, min(len(nodes), previous_frame + 3)):
                gap = frame - previous_frame
                for index, candidate in enumerate(nodes[frame]):
                    delta_x = candidate.x - previous.x
                    delta_y = candidate.y - previous.y
                    if not (abs(delta_x) <= 30.0 * gap and -38.0 * gap <= delta_y <= -0.5 * gap):
                        continue
                    grown = _grown(
                        track,
                        (frame, candidate),
                        None if directions is None else directions[frame][index],
                    )
                    if grown is not None:
                        extended.append(grown)
        if not extended:
            break
        scores = [_rough_path_score(path) for path, _h, _v in extended]
        order = sorted(range(len(extended)), key=scores.__getitem__, reverse=True)[:150]
        frontier = [extended[index] for index in order]
        all_paths.extend(extended[index][0] for index in order)
        scored.extend(scores[index] for index in order)
    viable = [index for index, path in enumerate(all_paths) if len(path) >= 4]
    viable.sort(key=scored.__getitem__, reverse=True)
    return [all_paths[index] for index in viable[:120]]


class _MotionGate:  # pylint: disable=too-few-public-methods
    """Launch directions that put a candidate where it is seen, at the OPS ball speed.

    A ball leaving the resting spot at the OPS speed lies, in flight frame k,
    on a sphere of radius v * (t_k - t0) around it, for an impact t0 inside the
    departure fit's acoustic window (DEPARTURE_MAX_LAG_S after to
    DEPARTURE_MAX_LEAD_S before the acoustic estimate). A candidate's camera ray
    meets MOTION_GATE_RADII such spheres; each meeting point gives a launch
    direction, admissible inside the estimator's acceptance box. Geometry only,
    no fit; a candidate with no admissible direction cannot be the ball.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        ball: ReferenceBall,
        model: _CameraModel,
        geometry: CameraBallGeometry,
        frame_times_s: np.ndarray,
        acoustic_s: float,
        speed_mph: float,
    ):
        self._model, self._geometry = model, geometry
        self._origin = geometry.camera_origin
        self._start = _resting_ball_point(ball, model=model, geometry=geometry)
        speed_ms = speed_mph / MPH_PER_MS
        elapsed = np.asarray(frame_times_s, dtype=float) - acoustic_s
        low = np.maximum(0.0, speed_ms * (elapsed - DEPARTURE_MAX_LAG_S))
        high = np.maximum(0.0, speed_ms * (elapsed + DEPARTURE_MAX_LEAD_S))
        self._radii = np.linspace(low, high, MOTION_GATE_RADII, axis=1)

    def directions(self, frame: int, candidates: list[BallCandidate]) -> list[_Directions]:
        """Admissible (horizontal, vertical) deg pairs per candidate of flight frame ``frame``."""
        if not candidates:
            return []
        rays = _camera_rays(
            np.array([candidate.x for candidate in candidates]),
            np.array([candidate.y for candidate in candidates]),
            model=self._model,
            geometry=self._geometry,
        )
        offset = self._origin - self._start
        along = rays @ offset
        radii = self._radii[frame]
        discriminant = along[:, None] ** 2 - (float(offset @ offset) - radii[None, :] ** 2)
        distance = -along[:, None] + np.sqrt(np.maximum(discriminant, 0.0))
        points = offset + distance[..., None] * rays[:, None, :]
        horizontal = np.degrees(np.arctan2(points[..., 0], points[..., 1]))
        vertical = np.degrees(np.arctan2(points[..., 2], np.hypot(points[..., 0], points[..., 1])))
        valid = (
            (discriminant >= 0.0)
            & (distance > 0.0)
            & (radii[None, :] > MOTION_GATE_MIN_RADIUS_M)
            & (horizontal >= HORIZONTAL_BOUNDS_DEG[0])
            & (horizontal <= HORIZONTAL_BOUNDS_DEG[1])
            & (vertical >= VERTICAL_BOUNDS_DEG[0])
            & (vertical <= VERTICAL_BOUNDS_DEG[1])
        )
        return [
            tuple(zip(horizontal[row][valid[row]].tolist(), vertical[row][valid[row]].tolist()))
            for row in range(len(candidates))
        ]


def _robust_velocity(times: np.ndarray, positions: np.ndarray) -> tuple[np.ndarray, float]:
    first, second = np.triu_indices(len(times), 1)
    delta = times[second] - times[first]
    forward = delta > 0.0
    slopes = (positions[second[forward]] - positions[first[forward]]) / delta[forward][:, None]
    velocity = np.median(slopes, axis=0)
    intercept = np.median(positions - times[:, None] * velocity, axis=0)
    residual = np.linalg.norm(positions - (intercept + times[:, None] * velocity), axis=1)
    return velocity, _median(residual)


def _horizontal(velocity: np.ndarray) -> float:
    """Return motion direction relative to the camera optical target line."""
    angle = math.atan2(float(velocity[0]), float(velocity[1]))
    return (math.degrees(angle) + 180.0) % 360.0 - 180.0


def _apply_horizontal_offset(angle_deg: float, offset_deg: float) -> float:
    """Apply a setup yaw correction or target-line offset while preserving angle wrapping."""
    return (angle_deg + offset_deg + 180.0) % 360.0 - 180.0


def _path_estimate(
    *,
    path: list[tuple[int, BallCandidate]],
    frame_indices: list[int],
    timestamps_ns: np.ndarray,
    time_zero_s: float,
    range_evidence,
    ops_ball_speed_mph: float,
    iwr_vertical_deg: float | None,
    model: _CameraModel,
    geometry: CameraBallGeometry,
    thresholds: tuple[int, int, int],  # retained for replay/debug provenance
    rays: dict[BallCandidate, np.ndarray] | None = None,
) -> tuple[float, _PathEstimate] | None:
    """Score and direction of one track, None when it fails a plausibility gate.

    ``rays`` optionally holds each candidate's camera ray (``_camera_rays``
    under ``model``), so a candidate shared by many tracks is undistorted once.
    """
    del thresholds
    times: list[float] = []
    positions: list[np.ndarray] = []
    actual_frames: list[int] = []
    used_candidates: list[BallCandidate] = []
    for relative_frame, candidate in path:
        frame = frame_indices[relative_frame]
        relative_time = int(timestamps_ns[frame]) / 1e9 - time_zero_s
        ray = rays.get(candidate) if rays is not None else None
        if range_evidence is None:
            position = _project_from_ball_size(candidate, model=model, geometry=geometry, ray=ray)
        else:
            radar_range = float(
                range_evidence.track.range_at(
                    range_evidence.impact_t_s + relative_time,
                    range_evidence.geometry.range_res_m,
                )
            )
            position = _project(candidate, radar_range, model=model, geometry=geometry, ray=ray)
        if position is not None:
            times.append(relative_time)
            positions.append(position)
            actual_frames.append(frame)
            used_candidates.append(candidate)
    if len(positions) < 4:
        return None

    times_array = np.asarray(times)
    positions_array = np.stack(positions)
    velocity, fit_median = _robust_velocity(times_array, positions_array)
    horizontal = _apply_horizontal_offset(_horizontal(velocity), model.direction_offset_deg)
    vertical = math.degrees(
        math.atan2(float(velocity[2]), math.hypot(float(velocity[0]), float(velocity[1])))
    )
    speed = float(np.linalg.norm(velocity) * MPH_PER_MS)
    if not (
        HORIZONTAL_BOUNDS_DEG[0] <= horizontal <= HORIZONTAL_BOUNDS_DEG[1]
        and VERTICAL_BOUNDS_DEG[0] <= vertical <= VERTICAL_BOUNDS_DEG[1]
        and 0.5 * ops_ball_speed_mph <= speed <= 1.5 * ops_ball_speed_mph
    ):
        return None  # the same gate as below, before the costlier track statistics
    step_velocity = np.diff(positions_array, axis=0) / np.diff(times_array)[:, None]
    step_speeds = np.linalg.norm(step_velocity, axis=1) * MPH_PER_MS
    step_speed_mad = _median(np.abs(step_speeds - _median(step_speeds)))
    step_angles = np.asarray(
        [
            _apply_horizontal_offset(_horizontal(step), model.direction_offset_deg)
            for step in step_velocity
        ]
    )
    window_mad = _median(np.abs(step_angles - horizontal))
    shape = np.asarray(
        [abs(math.log(candidate.width / max(candidate.height, 1))) for candidate in used_candidates]
    )
    shape_median = _median(shape)
    fill_median = _median([candidate.fill for candidate in used_candidates])
    circularity_median = _median([candidate.circularity for candidate in used_candidates])
    contrast_median = _median([candidate.local_contrast for candidate in used_candidates])
    camera_origin = np.array([0.0, 0.0, geometry.camera_height_m])
    camera_ranges = np.linalg.norm(positions_array - camera_origin, axis=1)
    expected_diameter = model.focal_px * geometry.ball_diameter_m / camera_ranges
    measured_diameter = np.asarray(
        [math.sqrt(4.0 * candidate.area / math.pi) for candidate in used_candidates]
    )
    size_ratio = measured_diameter / expected_diameter
    size_ratio_median = _median(size_ratio)
    size_ratio_mad = _median(np.abs(size_ratio - size_ratio_median))
    if not (
        HORIZONTAL_BOUNDS_DEG[0] <= horizontal <= HORIZONTAL_BOUNDS_DEG[1]
        and VERTICAL_BOUNDS_DEG[0] <= vertical <= VERTICAL_BOUNDS_DEG[1]
        and 0.5 * ops_ball_speed_mph <= speed <= 1.5 * ops_ball_speed_mph
        and shape_median <= 0.55
        and fill_median >= 0.5
        and circularity_median >= 0.5
        and contrast_median >= 24.0
        and 0.45 <= size_ratio_median <= 2.5
        and size_ratio_mad <= 0.75
    ):
        return None

    vertical_prior = abs(vertical - iwr_vertical_deg) if iwr_vertical_deg is not None else 0.0
    score = (
        10.0 * len(positions_array)
        - 400.0 * fit_median
        - 0.45 * abs(speed - ops_ball_speed_mph)
        - 0.35 * step_speed_mad
        - 0.5 * vertical_prior
        - 6.0 * window_mad
        - 8.0 * shape_median
        + 6.0 * fill_median
        + 4.0 * circularity_median
        - 4.0 * size_ratio_mad
        - 2.0 * (actual_frames[0] - frame_indices[0])
    )
    return score, _PathEstimate(
        horizontal_deg=horizontal,
        vertical_deg=vertical,
        speed_mph=speed,
        speed_error_mph=speed - ops_ball_speed_mph,
        fit_median_m=fit_median,
        step_speed_mad_mph=step_speed_mad,
        window_mad_deg=window_mad,
        n_points=len(positions_array),
        first_frame=actual_frames[0],
        last_frame=actual_frames[-1],
    )


def _confidence_tier(support: int, parameter_mad: float, window_mad: float) -> str:
    """Map detector consensus and local trajectory coherence to a confidence tier."""
    stable_consensus = parameter_mad <= 1.0
    if support >= 9 and stable_consensus and window_mad <= 0.5:
        return "high"
    if support >= 2 and stable_consensus and window_mad <= 1.5:
        return "experimental"
    return "withheld"


def _fit_departure(
    path: list[tuple[int, BallCandidate]],
    frame_times_ns: np.ndarray,
    *,
    anchor: ReferenceBall,
    model: _CameraModel,
    geometry: CameraBallGeometry,
) -> DepartureFit | None:
    """Back-extrapolate a ball track to its resting image; None when unreliable.

    A receding ball appears s(t) = S - K / (t - t_inf) from its resting image.
    Points within one ball diameter are skipped (resting ball or club head).
    """
    anchor_x, anchor_z = _normalized(anchor.x, anchor.y, focal_px=model.focal_px, geometry=geometry)
    points = []
    for relative_frame, candidate in path:
        image_x, image_z = _normalized(
            candidate.x, candidate.y, focal_px=model.focal_px, geometry=geometry
        )
        distance_px = model.focal_px * math.hypot(image_x - anchor_x, image_z - anchor_z)
        if distance_px >= anchor.diameter_px:
            points.append((int(frame_times_ns[relative_frame]), distance_px))
    points = points[:DEPARTURE_FIT_POINTS]
    if len(points) < DEPARTURE_FIT_MIN_POINTS:
        return None
    first_ns = points[0][0]
    times = np.asarray([(time_ns - first_ns) / 1e9 for time_ns, _ in points])
    distances = np.asarray([distance for _, distance in points])
    # One linear least-squares fit of (S, K) per candidate t_inf = -lead.
    inverse = 1.0 / (times[:, None] + _ASYMPTOTE_LEADS_S)
    centred = inverse - inverse.mean(axis=0)
    scale = -(centred.T @ (distances - distances.mean())) / (centred**2).sum(axis=0)
    asymptote = distances.mean() + scale * inverse.mean(axis=0)
    residual = ((distances[:, None] - asymptote + scale * inverse) ** 2).sum(axis=0)
    residual[(scale <= 0.0) | (asymptote <= 0.0)] = np.inf
    best = int(np.argmin(residual))
    if not np.isfinite(residual[best]):
        return None
    departure_s = scale[best] / asymptote[best] - _ASYMPTOTE_LEADS_S[best]
    fit = DepartureFit(
        time_s=first_ns / 1e9 + departure_s,
        n_points=len(points),
        rms_px=math.sqrt(residual[best] / len(points)),
        span_frames=-departure_s / (float(np.median(np.diff(frame_times_ns))) / 1e9),
    )
    if fit.rms_px > DEPARTURE_FIT_MAX_RMS_PX or fit.span_frames > DEPARTURE_FIT_MAX_SPAN_FRAMES:
        return None
    return fit


def _median_departure(
    paths: list[list[tuple[int, BallCandidate]]],
    frame_times_ns: np.ndarray,
    *,
    anchor: ReferenceBall,
    model: _CameraModel,
    geometry: CameraBallGeometry,
) -> tuple[DepartureFit | None, int]:
    """Median reliable departure fit, when at least half the tracks yield one."""
    fits = []
    for path in paths:
        fit = _fit_departure(path, frame_times_ns, anchor=anchor, model=model, geometry=geometry)
        if fit is not None:
            fits.append(fit)
    fits.sort(key=lambda fit: fit.time_s)
    if not fits or 2 * len(fits) < len(paths):
        return None, len(fits)
    return fits[(len(fits) - 1) // 2], len(fits)


def fit_ball_departure(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    start_index: int,
    geometry: CameraBallGeometry,
    ball: ReferenceBall,
    n_frames: int = FLIGHT_WINDOW_FRAMES,
) -> DepartureFit | None:
    """Ball departure from the flight frames from ``start_index`` on, without the live gates.

    The departure part of ``estimate_camera_ball_flight`` alone, for the club
    data's contact time: the same candidates (pre-swing median of the first
    20 frames), the full 27-cell threshold sweep, ``_pixel_paths``,
    ``_fit_departure`` and ``_median_departure``. Each cell contributes its best
    ``_rough_path_score`` path, as there is no range evidence to rank tracks.
    Unlike the live estimator it applies neither the reference-ball contrast
    gate nor the tee-patch departure gate: a teed driver's ball merges with its
    tee and the head covers the tee patch, so the live estimator stops before
    its fit, yet the ball emerges above the head a frame or two after contact.
    ``ball`` is the resting ball, the origin of every track and, with
    ``geometry``, the focal scale of the fit. None without a reliable fit.
    """
    stack = np.asarray(frames)
    times_ns = np.asarray(timestamps_ns, dtype=np.int64)
    if stack.ndim != 3 or len(stack) != len(times_ns) or len(stack) == 0:
        return None
    first = max(0, int(start_index))
    frame_indices = list(range(first, min(len(times_ns), first + max(0, int(n_frames)))))
    if len(frame_indices) < DEPARTURE_FIT_MIN_POINTS:
        return None
    model = _camera_model(ball, geometry)
    background = np.median(stack[: min(20, len(stack))], axis=0).astype(np.uint8)
    pairs = tuple(itertools.product(SWEEP_BRIGHT_THRESHOLDS, SWEEP_DIFFERENCE_THRESHOLDS))
    # One candidate pass per frame for all nine threshold pairs; a larger
    # minimum area filters it, exactly as ``_candidates`` per cell would.
    sweep = [
        _sweep_candidates(
            stack[index], background, ball, thresholds=pairs, min_area=min(SWEEP_MIN_AREAS)
        )
        for index in frame_indices
    ]
    paths = []
    for bright, difference, min_area in itertools.product(
        SWEEP_BRIGHT_THRESHOLDS, SWEEP_DIFFERENCE_THRESHOLDS, SWEEP_MIN_AREAS
    ):
        nodes = [
            [candidate for candidate in found[(bright, difference)] if candidate.area >= min_area]
            for found in sweep
        ]
        found_paths = _pixel_paths(nodes, ball)
        if found_paths:
            paths.append(found_paths[0])
    if not paths:
        return None
    fit, _support = _median_departure(
        paths, times_ns[frame_indices], anchor=ball, model=model, geometry=geometry
    )
    return fit


def _select_time_zero(
    departure: DepartureFit | None, *, trigger_s: float, acoustic_s: float
) -> tuple[float, str]:
    """Fitted departure when acoustically plausible, else trigger minus sound travel."""
    latest_s = min(acoustic_s + DEPARTURE_MAX_LAG_S, trigger_s)
    if departure is not None and acoustic_s - DEPARTURE_MAX_LEAD_S <= departure.time_s <= latest_s:
        return departure.time_s, "departure_fit"
    return acoustic_s, "acoustic"


def _post_impact_frames(timestamps_ns: np.ndarray, acoustic_s: float) -> list[int]:
    """Up to FLIGHT_WINDOW_FRAMES frames exposed at or after the acoustic impact estimate.

    Impact cannot follow its own sound, so none of them precedes impact. The
    tee-patch departure is no bound: the club head still covers that patch for
    a frame or two after the ball has left it.
    """
    first = int(np.searchsorted(timestamps_ns / 1e9, acoustic_s))
    return list(range(first, min(len(timestamps_ns), first + FLIGHT_WINDOW_FRAMES)))


def _locate_reference_ball(
    detected: ReferenceBall, ball_tracker
) -> tuple[ReferenceBall, ReferenceBall]:
    """Where the ball lies in this capture, and the steadier ball the camera pose comes from.

    Every shot is a newly teed ball, often a few centimetres from the last. The
    tracker keeps a detection that agrees with the session and swaps a false
    blob for its anchor; contrast, departure and the flight search must look
    there, or they read empty mat (2026-10-06: an 8-iron teed 3 cm from fifteen
    wedges failed the contrast check seven times). Focal scale, pitch and yaw
    come from the rolling session anchor, which one re-teed ball barely moves.
    """
    if ball_tracker is None:
        return detected, detected
    ball, _source = ball_tracker.resolve(detected)
    fallback = getattr(ball_tracker, "fallback", None)
    session_anchor = fallback() if fallback is not None else None
    return ball, session_anchor if session_anchor is not None else ball


class _Shot(NamedTuple):
    """One clip and the measurements its ball flight is checked against."""

    frames: np.ndarray
    timestamps_ns: np.ndarray
    trigger_ns: int
    range_evidence: object
    geometry: CameraBallGeometry
    ops_ball_speed_mph: float | None
    iwr_vertical_deg: float | None

    @property
    def trigger_frame(self) -> int:
        """Frame nearest the trigger stamp."""
        return int(np.argmin(np.abs(self.timestamps_ns.astype(np.int64) - self.trigger_ns)))

    @property
    def trigger_s(self) -> float:
        """Trigger stamp in seconds on the frame clock."""
        return self.trigger_ns / 1e9

    @property
    def acoustic_s(self) -> float:
        """Impact estimate: the trigger minus the tee-to-microphone sound travel."""
        return self.trigger_s - self.geometry.tee_range_m / SPEED_OF_SOUND_M_S


class _Origin(NamedTuple):
    """Where the ball left from, the pose source and where its flight is searched."""

    ball: ReferenceBall
    pose_ball: ReferenceBall
    model: _CameraModel
    departure: int | None
    # (x0, y0, x1, y1) the flight search reads, and its pre-swing median (uint8).
    region: tuple[int, int, int, int]
    background: np.ndarray
    tee_column: bool = False
    gated: bool = False


class _Tracked(NamedTuple):
    """The consensus estimate plus what the quality checks need of it."""

    estimate: CameraBallEstimate
    path: list[tuple[int, BallCandidate]] | None = None
    frame_indices: list[int] | None = None
    model: _CameraModel | None = None
    gate_candidates: int | None = None
    gate_kept: int | None = None


def _detect_reference_ball(frames: np.ndarray, window) -> ReferenceBall:
    """``detect_reference_ball`` over the whole frame, or over the ball mark's ``window`` only."""
    if window is None:
        return detect_reference_ball(frames)
    return detect_reference_ball(frames, roi=window)


def _session_anchor(ball_tracker, window) -> ReferenceBall | None:
    """The tracker's established session tee anchor, None without one or outside ``window``."""
    fallback = getattr(ball_tracker, "fallback", None)
    anchor = fallback() if fallback is not None else None
    if anchor is None or (window is not None and not in_window(window, anchor)):
        return None
    return anchor


def _legacy_origin(shot: _Shot, ball_tracker, window=None) -> _Origin | CameraBallEstimate:
    """``detect_reference_ball`` plus the session tracker, fixed contrast and departure gates.

    With a ball mark the detector reads only its ``window``, and a tracker
    anchor outside it never replaces the detection or sets the pose.
    """
    frames = shot.frames
    try:
        detected = _detect_reference_ball(frames, window)
    except ValueError:
        ball = _session_anchor(ball_tracker, window)
        if ball is None:
            return CameraBallEstimate("rejected_reference_ball_not_found")
        pose_ball = ball
    else:
        ball, pose_ball = _locate_reference_ball(detected, ball_tracker)
        if window is not None:
            ball = ball if in_window(window, ball) else detected
            pose_ball = pose_ball if in_window(window, pose_ball) else ball
    if not 9.0 <= ball.diameter_px <= 30.0:
        return CameraBallEstimate("rejected_implausible_reference_ball")

    background = np.median(frames[: min(20, len(frames))], axis=0).astype(np.uint8)
    radius = max(2, round(ball.diameter_px * 0.35))
    left, top = round(ball.x) - radius, round(ball.y) - radius
    size = 2 * radius + 1
    if left < 0 or top < 0 or left + size > frames.shape[2] or top + size > frames.shape[1]:
        return CameraBallEstimate("rejected_implausible_reference_ball")
    yy, xx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    disk = (xx * xx + yy * yy <= radius * radius).astype(np.uint8)
    if _local_contrast(background, left, top, disk) < 20.0:
        return CameraBallEstimate("rejected_reference_ball_contrast")
    model = _camera_model(pose_ball, shot.geometry)
    departure = _detect_impact_index(frames, ball, trigger_index=shot.trigger_frame)
    if departure is None:
        return CameraBallEstimate("rejected_no_ball_departure")
    height, width = frames.shape[1:]
    return _Origin(ball, pose_ball, model, departure, (0, 0, width, height), background)


def _flight_region(ball: ReferenceBall, shape: tuple[int, int]) -> tuple[int, int, int, int]:
    """The flight corridor above the ball that ``_sweep_candidates`` can accept, plus a margin."""
    height, width = shape
    return (
        max(0, math.floor(ball.x - FLIGHT_HALF_WIDTH_PX) - CORRIDOR_MARGIN_PX),
        max(0, math.floor(FLIGHT_TOP_PX) - CORRIDOR_MARGIN_PX),
        min(width, math.ceil(ball.x + FLIGHT_HALF_WIDTH_PX) + CORRIDOR_MARGIN_PX),
        min(height, math.ceil(ball.y + FLIGHT_BELOW_PX) + CORRIDOR_MARGIN_PX),
    )


def _tee_estimate(
    frames: np.ndarray, ground_anchor: GroundBallAnchor | None, ball_tracker, window=None
) -> tuple[ReferenceBall | None, str | None]:
    """Where to centre the search without a hitting zone, and where that came from.

    With a ball mark only a ball inside its ``window`` counts, and the detector
    reads only the window: the estimate then gives the expected diameter.
    """
    last = ground_anchor.last if ground_anchor is not None else None
    if last is not None and (window is None or in_window(window, last)):
        return last, "last_ball"
    session_anchor = _session_anchor(ball_tracker, window)
    if session_anchor is not None:
        return session_anchor, "session_anchor"
    try:
        return _detect_reference_ball(frames, window), "detected"
    except ValueError:
        return None, None


def _pose_geometry(geometry: CameraBallGeometry, pose_source: str) -> CameraBallGeometry:
    """The geometry of the ball the pose comes from.

    The ground anchor is a median of untee'd balls, so its centre sits at the mat
    height even on a teed shot; every other pose ball is this shot's own ball.
    """
    if pose_source != "ground_anchor" or not geometry.tee_height_m:
        return geometry
    return replace(
        geometry, ball_height_m=geometry.ball_height_m - geometry.tee_height_m, tee_height_m=0.0
    )


def _ball_teed(found: DepartedBall, geometry: CameraBallGeometry) -> bool:
    """Whether the departed ball sits on a tee, for the anchors.

    Its own tee rows (``DepartedBall.teed``), or a geometry that puts it on a
    tee (``tee_height_m`` > 0: the server sets it for the ``driver`` club only,
    from the profile's tee height, ``server._shot_ball_height``). A teed driver
    whose rubber tee shows only 3-6 rows (2026-11-02: 6 of 19, limit 0.5 d ~ 7)
    must not teach the ground anchor a ball 4 cm above the mat.
    """
    return found.teed or geometry.tee_height_m > 0.0


def _pose_ball(
    found: DepartedBall, ground_anchor: GroundBallAnchor | None, window, *, teed: bool
) -> tuple[ReferenceBall, str, bool | None]:
    """Teach the ground anchor this ball; the camera pose's ball, where it came from, and
    whether the ball fed the anchor (None without one).

    ``teed`` is ``_ball_teed``. A ground anchor learned outside the ball mark's
    ``window`` is not where this ball lies: the pose then comes from this ball.
    """
    sample = ground_anchor.observe(found, teed=teed) if ground_anchor is not None else None
    ground = ground_anchor.anchor() if ground_anchor is not None else None
    if ground is not None and (window is None or in_window(window, ground)):
        return ground, "ground_anchor", sample
    return found.reference(), "teed_ball" if teed else "this_ball", sample


def _departed_origin(
    shot: _Shot,
    *,
    zone: TeeZone | None,
    ground_anchor: GroundBallAnchor | None,
    ball_tracker,
    window=None,
) -> tuple[_Origin | CameraBallEstimate | None, BallLocatorDiagnostics]:
    """The disappeared-ball origin, a rejection, or None (legacy fallback), with the reason.

    ``window`` is the square around the tapped ball (``mark_window``), None without a mark.
    """
    frames = shot.frames
    shape = frames.shape[1:]
    if zone is not None and (zone.image_width_px, zone.image_height_px) == (shape[1], shape[0]):
        estimate, source = None, "tee_zone"
    else:
        estimate, source = _tee_estimate(frames, ground_anchor, ball_tracker, window)
    if window is not None:
        source = "ball_mark"
    searched = search_box(shape, zone=zone, tee_estimate=estimate, window=window)
    if searched is None:
        return None, BallLocatorDiagnostics("legacy_fallback", "no_search_box")
    box, expected_diameter = searched
    found, reason = locate_departed_ball(
        frames, shot.trigger_frame, expected_diameter_px=expected_diameter, box=box
    )
    diagnostics = BallLocatorDiagnostics(
        "disappeared", reason, search_box=box, search_box_source=source
    )
    if found is not None:
        diagnostics = _with_departed_ball(diagnostics, found)
        failure = found.gate_failure()
        if failure is not None:
            found, diagnostics = None, replace(diagnostics, reason=failure)
    if found is None:
        return None, replace(diagnostics, locator="legacy_fallback")
    departure = departure_index(frames, found, shot.trigger_frame)
    if departure is None:
        # A late trigger or a slow scene change: no time zero, and nothing the
        # pose or the session tee anchor should learn from (as in the legacy path).
        diagnostics = replace(diagnostics, reason="no_ball_departure")
        return CameraBallEstimate("rejected_no_ball_departure"), diagnostics

    ball = found.reference()
    pose_ball, pose_source, anchor_sample = _pose_ball(
        found, ground_anchor, window, teed=_ball_teed(found, shot.geometry)
    )
    if ball_tracker is not None:
        # The session tee anchor the shadow club-head path resolves its ball with.
        ball_tracker.resolve(ball)
    region = _flight_region(ball, shape)
    x0, y0, x1, y1 = region
    background = np.median(frames[: min(20, len(frames)), y0:y1, x0:x1], axis=0).astype(np.uint8)
    gated = bool(shot.ops_ball_speed_mph)
    origin = _Origin(
        ball,
        pose_ball,
        _camera_model(pose_ball, _pose_geometry(shot.geometry, pose_source)),
        departure,
        region,
        background,
        tee_column=found.teed,
        gated=gated,
    )
    return origin, replace(
        diagnostics,
        pose_source=pose_source,
        pose_x=pose_ball.x,
        pose_y=pose_ball.y,
        ground_anchor_sample=anchor_sample,
        motion_gate="applied" if gated else "no_ops_speed",
    )


def _with_departed_ball(
    diagnostics: BallLocatorDiagnostics, ball: DepartedBall
) -> BallLocatorDiagnostics:
    return replace(
        diagnostics,
        ball_x=ball.x,
        ball_y=ball.y,
        ball_diameter_px=ball.diameter_px,
        contrast=ball.contrast,
        noise=ball.noise,
        snr=ball.snr,
        polarity=ball.polarity,
        teed=ball.teed,
        tee_px=ball.tee_px,
    )


class _Sweep(NamedTuple):
    """Candidate tracks per threshold triple, their camera rays and the gate's counts."""

    paths: dict[tuple[int, int, int], list[list[tuple[int, BallCandidate]]]]
    rays: dict[BallCandidate, np.ndarray]
    gate_candidates: int | None
    gate_kept: int | None


def _sweep_tracks(shot: _Shot, origin: _Origin, frame_indices: list[int]) -> _Sweep:
    """Flight candidates and tracks for every threshold triple of the frozen sweep."""
    pairs = tuple(itertools.product(SWEEP_BRIGHT_THRESHOLDS, SWEEP_DIFFERENCE_THRESHOLDS))
    x0, y0, x1, y1 = origin.region
    sweep = [
        _sweep_candidates(
            shot.frames[frame, y0:y1, x0:x1],
            origin.background,
            origin.ball,
            thresholds=pairs,
            min_area=min(SWEEP_MIN_AREAS),
            origin=(x0, y0),
            tee_column=origin.tee_column,
        )
        for frame in frame_indices
    ]
    gate_directions = None
    gate_candidates = gate_kept = None
    if origin.gated:
        gate = _MotionGate(
            ball=origin.ball,
            model=origin.model,
            geometry=shot.geometry,
            frame_times_s=shot.timestamps_ns[frame_indices] / 1e9,
            acoustic_s=shot.acoustic_s,
            speed_mph=shot.ops_ball_speed_mph,
        )
        gate_directions = {
            pair: [gate.directions(relative, found[pair]) for relative, found in enumerate(sweep)]
            for pair in pairs
        }
        options = [
            option
            for per_frame in gate_directions.values()
            for frame_options in per_frame
            for option in frame_options
        ]
        gate_candidates = len(options)
        gate_kept = sum(1 for option in options if option)
    paths = {}
    for bright, difference, min_area in itertools.product(
        SWEEP_BRIGHT_THRESHOLDS, SWEEP_DIFFERENCE_THRESHOLDS, SWEEP_MIN_AREAS
    ):
        pair = (bright, difference)
        kept = [
            [index for index, candidate in enumerate(found[pair]) if candidate.area >= min_area]
            for found in sweep
        ]
        nodes = [[found[pair][index] for index in indexes] for found, indexes in zip(sweep, kept)]
        directions = (
            None
            if gate_directions is None
            else [
                [gate_directions[pair][relative][index] for index in indexes]
                for relative, indexes in enumerate(kept)
            ]
        )
        paths[(bright, difference, min_area)] = _pixel_paths(nodes, origin.ball, directions)

    rays: dict[BallCandidate, np.ndarray] = {}
    for found in sweep:
        unique = list(dict.fromkeys(candidate for pair in pairs for candidate in found[pair]))
        if unique:
            frame_rays = _camera_rays(
                np.array([candidate.x for candidate in unique]),
                np.array([candidate.y for candidate in unique]),
                model=origin.model,
                geometry=shot.geometry,
            )
            rays.update(zip(unique, frame_rays))
    return _Sweep(paths, rays, gate_candidates, gate_kept)


def _track_flight(shot: _Shot, origin: _Origin) -> _Tracked:
    """The frozen detector-consensus sweep over the flight frames after the departure."""
    geometry, timestamps_ns = shot.geometry, shot.timestamps_ns
    ball, model = origin.ball, origin.model
    trigger_s, acoustic_s = shot.trigger_s, shot.acoustic_s
    frame_indices = _post_impact_frames(timestamps_ns, acoustic_s)
    if len(frame_indices) < 4:
        return _Tracked(CameraBallEstimate("rejected_insufficient_post_trigger_frames"))
    swept = _sweep_tracks(shot, origin, frame_indices)
    paths = swept.paths

    def evaluate(path, depth_evidence, zero_s: float, thresholds: tuple[int, int, int]):
        return _path_estimate(
            path=path,
            frame_indices=frame_indices,
            timestamps_ns=timestamps_ns,
            time_zero_s=zero_s,
            range_evidence=depth_evidence,
            ops_ball_speed_mph=shot.ops_ball_speed_mph,
            iwr_vertical_deg=shot.iwr_vertical_deg,
            model=model,
            geometry=geometry,
            thresholds=thresholds,
            rays=swept.rays,
        )

    def collect(depth_evidence, zero_s: float) -> list[tuple[_PathEstimate, list, tuple]]:
        # Thresholds share candidates, so many of their tracks are the same.
        results: dict[tuple, tuple[float, _PathEstimate] | None] = {}
        found: list[tuple[_PathEstimate, list, tuple]] = []
        for thresholds, candidate_paths in paths.items():
            options = []
            for path in candidate_paths:
                key = tuple((frame, id(candidate)) for frame, candidate in path)
                if key not in results:
                    results[key] = evaluate(path, depth_evidence, zero_s, thresholds)
                if results[key] is not None:
                    options.append((results[key], path))
            if options:
                (_score, estimate), path = max(options, key=lambda item: item[0][0])
                found.append((estimate, path, thresholds))
        return found

    # The IWR track is indexed from its own impact. Tracks are selected at the
    # acoustic estimate, then their own back-extrapolated departure sets the
    # camera time zero when it is acoustically plausible.
    selected = collect(shot.range_evidence, acoustic_s)
    fitted, support = _median_departure(
        [path for _estimate, path, _thresholds in selected],
        timestamps_ns[frame_indices],
        anchor=ball,
        model=model,
        geometry=geometry,
    )
    time_zero_s, time_zero_source = _select_time_zero(
        fitted, trigger_s=trigger_s, acoustic_s=acoustic_s
    )
    time_zero = CameraTimeZero(
        source=time_zero_source,
        time_zero_ms=1e3 * (time_zero_s - trigger_s),
        departure_fit_ms=None if fitted is None else 1e3 * (fitted.time_s - trigger_s),
        acoustic_ms=1e3 * (acoustic_s - trigger_s),
        tee_patch_ms=(
            None
            if origin.departure is None
            else 1e3 * (_contact_time_s(timestamps_ns, origin.departure) - trigger_s)
        ),
        iwr_impact_t_s=None if shot.range_evidence is None else shot.range_evidence.impact_t_s,
        departure_fit_points=0 if fitted is None else fitted.n_points,
        departure_fit_rms_px=None if fitted is None else fitted.rms_px,
        departure_fit_span_frames=None if fitted is None else fitted.span_frames,
        departure_fit_support=support,
        selected_paths=len(selected),
    )
    if shot.range_evidence is not None and time_zero_s != acoustic_s:
        selected = [
            (result[1], path, thresholds)
            for _estimate, path, thresholds in selected
            if (result := evaluate(path, shot.range_evidence, time_zero_s, thresholds)) is not None
        ]

    depth_source = "iwr_range" if shot.range_evidence is not None else "camera_size"
    if shot.range_evidence is not None:
        primary_tier = "withheld"
        if selected:
            primary_horizontal = np.asarray([item[0].horizontal_deg for item in selected])
            primary_median = float(np.median(primary_horizontal))
            primary_tier = _confidence_tier(
                len(selected),
                float(np.median(np.abs(primary_horizontal - primary_median))),
                float(np.median([item[0].window_mad_deg for item in selected])),
            )
        if primary_tier == "withheld":
            camera_only = collect(None, time_zero_s)
            if camera_only:
                camera_horizontal = np.asarray([item[0].horizontal_deg for item in camera_only])
                camera_median = float(np.median(camera_horizontal))
                camera_tier = _confidence_tier(
                    len(camera_only),
                    float(np.median(np.abs(camera_horizontal - camera_median))),
                    float(np.median([item[0].window_mad_deg for item in camera_only])),
                )
                if camera_tier != "withheld":
                    depth_source = "camera_size"
                    selected = camera_only

    tracked = {
        "frame_indices": frame_indices,
        "model": model,
        "gate_candidates": swept.gate_candidates,
        "gate_kept": swept.gate_kept,
    }
    if not selected:
        return _Tracked(
            CameraBallEstimate("rejected_no_stable_path", time_zero=time_zero), **tracked
        )
    estimates = [estimate for estimate, _path, _thresholds in selected]
    horizontal = np.asarray([estimate.horizontal_deg for estimate in estimates])
    median_horizontal = float(np.median(horizontal))
    parameter_mad = float(np.median(np.abs(horizontal - median_horizontal)))
    window_mad = float(np.median([estimate.window_mad_deg for estimate in estimates]))
    tier = _confidence_tier(len(estimates), parameter_mad, window_mad)
    if depth_source == "camera_size" and tier == "high":
        tier = "experimental"
    representative, representative_path, _thresholds = min(
        selected, key=lambda item: abs(item[0].horizontal_deg - median_horizontal)
    )
    estimate = CameraBallEstimate(
        status=(
            "accepted_camera_only"
            if tier != "withheld" and depth_source == "camera_size"
            else "accepted"
            if tier != "withheld"
            else "rejected_unstable_consensus"
        ),
        confidence_tier=tier,
        horizontal_deg=median_horizontal if tier != "withheld" else None,
        vertical_deg=float(np.median([estimate.vertical_deg for estimate in estimates])),
        support=len(estimates),
        support_pct=100.0 * len(estimates) / PARAMETER_SWEEP_SIZE,
        parameter_mad_deg=parameter_mad,
        window_mad_deg=window_mad,
        speed_mph=float(np.median([estimate.speed_mph for estimate in estimates])),
        speed_error_mph=float(np.median([estimate.speed_error_mph for estimate in estimates])),
        n_points=representative.n_points,
        first_frame=representative.first_frame,
        last_frame=representative.last_frame,
        depth_source=depth_source,
        time_zero=time_zero,
        target_azimuth_deg=model.target_azimuth_deg,
    )
    return _Tracked(estimate, representative_path, **tracked)


def _unit_direction(horizontal_deg: float, vertical_deg: float) -> np.ndarray:
    horizontal, vertical = math.radians(horizontal_deg), math.radians(vertical_deg)
    return np.array(
        [
            math.cos(vertical) * math.sin(horizontal),
            math.cos(vertical) * math.cos(horizontal),
            math.sin(vertical),
        ]
    )


def _line_fit(  # pylint: disable=too-many-arguments
    path: list[tuple[int, BallCandidate]],
    *,
    elapsed_s: np.ndarray,
    ball: ReferenceBall,
    model: _CameraModel,
    geometry: CameraBallGeometry,
    speed_mph: float,
) -> LineFit:
    """Fit P(t) = ball + v_ops (t - t0) u(h, v) to a track's camera rays; no IWR range.

    ``elapsed_s`` holds each track point's frame time from the acoustic impact
    estimate; t0 may move LINE_FIT_T0_BOUNDS_S around it. The residual of a
    point is the angle between its ray and the model point's (mrad).
    """
    from scipy.optimize import least_squares  # noqa: PLC0415

    rays = _camera_rays(
        np.array([candidate.x for _frame, candidate in path]),
        np.array([candidate.y for _frame, candidate in path]),
        model=model,
        geometry=geometry,
    )
    start = _resting_ball_point(ball, model=model, geometry=geometry) - geometry.camera_origin
    speed_ms = speed_mph / MPH_PER_MS

    def residuals(parameters: np.ndarray) -> np.ndarray:
        horizontal, vertical, lead = parameters
        points = start + speed_ms * (elapsed_s - lead)[:, None] * _unit_direction(
            horizontal, vertical
        )
        points /= np.linalg.norm(points, axis=1, keepdims=True)
        return np.cross(points, rays).ravel()

    lower = [LINE_FIT_BOUNDS_DEG[0][0], LINE_FIT_BOUNDS_DEG[0][1], LINE_FIT_T0_BOUNDS_S[0]]
    upper = [LINE_FIT_BOUNDS_DEG[1][0], LINE_FIT_BOUNDS_DEG[1][1], LINE_FIT_T0_BOUNDS_S[1]]
    best = None
    for horizontal, vertical in itertools.product(
        LINE_FIT_START_HORIZONTAL_DEG, LINE_FIT_START_VERTICAL_DEG
    ):
        result = least_squares(
            residuals,
            [horizontal, vertical, 0.0],
            bounds=(lower, upper),
            loss="soft_l1",
            f_scale=LINE_FIT_LOSS_SCALE_RAD,
        )
        if best is None or result.cost < best.cost:
            best = result
    misses = np.linalg.norm(residuals(best.x).reshape(-1, 3), axis=1)
    horizontal, vertical, lead = (float(value) for value in best.x)
    return LineFit(
        horizontal_deg=_apply_horizontal_offset(horizontal, model.direction_offset_deg),
        vertical_deg=vertical,
        time_zero_ms=1e3 * lead,
        rms_mrad=1e3 * float(np.sqrt(np.mean(misses**2))),
        max_mrad=1e3 * float(np.max(misses)),
        n_points=len(path),
    )


def _check_line_fit(
    shot: _Shot,
    origin: _Origin,
    tracked: _Tracked,
    diagnostics: BallLocatorDiagnostics,
) -> tuple[CameraBallEstimate, BallLocatorDiagnostics]:
    """Withhold a direction no straight line at the OPS speed explains (TI-free check)."""
    estimate = tracked.estimate
    if tracked.path is None:
        return estimate, replace(diagnostics, line_fit_status="no_track")
    if not shot.ops_ball_speed_mph:
        return estimate, replace(diagnostics, line_fit_status="no_ops_speed")
    elapsed = np.asarray(
        [
            int(shot.timestamps_ns[tracked.frame_indices[relative]]) / 1e9 - shot.acoustic_s
            for relative, _candidate in tracked.path
        ]
    )
    try:
        fit = _line_fit(
            tracked.path,
            elapsed_s=elapsed,
            ball=origin.ball,
            model=tracked.model,
            geometry=shot.geometry,
            speed_mph=shot.ops_ball_speed_mph,
        )
    except (ValueError, np.linalg.LinAlgError):
        return estimate, replace(diagnostics, line_fit_status="failed")
    if fit.rms_mrad <= LINE_FIT_MAX_RMS_MRAD:
        return estimate, replace(diagnostics, line_fit_status="ok", line_fit=fit)
    if estimate.confidence_tier != "withheld":
        estimate = replace(
            estimate,
            status="rejected_line_fit_residual",
            confidence_tier="withheld",
            horizontal_deg=None,
        )
    return estimate, replace(diagnostics, line_fit_status="over_residual", line_fit=fit)


def estimate_camera_ball_flight(  # pylint: disable=too-many-arguments
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    trigger_ns: int,
    range_evidence,
    geometry: CameraBallGeometry,
    ops_ball_speed_mph: float,
    iwr_vertical_deg: float | None = None,
    ball_tracker=None,
    ball_locator: str = DEFAULT_BALL_LOCATOR,
    ground_anchor: GroundBallAnchor | None = None,
    tee_zone: TeeZone | None = None,
    legacy_fallback: bool = True,
    ball_mark_px: tuple[float, float] | None = None,
) -> CameraBallEstimate:
    """Estimate horizontal flight with a frozen detector-consensus sweep.

    ``ball_locator`` picks the departure origin (see the module docstring).
    ``ground_anchor`` (the session's untee'd balls, the camera pose source) and
    ``ball_tracker`` (the session tee anchor) are updated in place;
    ``tee_zone`` bounds the disappeared-ball search. ``legacy_fallback=False``
    (the low-light shadow path, ``camera.lowlight``) returns
    ``rejected_no_departed_ball`` where the disappeared-ball locator found no
    ball, instead of running the legacy locator. ``ball_mark_px`` (the tapped
    resting ball, saved-frame pixels) bounds every ball search to the window
    around it; a mark whose window misses the frame is
    ``rejected_ball_mark_off_frame``.
    """
    if ball_locator not in BALL_LOCATORS:
        raise ValueError(f"ball_locator must be one of {BALL_LOCATORS}")
    started = time.perf_counter()
    if frames.ndim != 3 or len(frames) < 4 or len(timestamps_ns) != len(frames):
        return CameraBallEstimate(
            "rejected_invalid_camera_frames",
            locator=BallLocatorDiagnostics(ball_locator, "invalid_camera_frames"),
        )
    mark = None if ball_mark_px is None else (float(ball_mark_px[0]), float(ball_mark_px[1]))
    window = None if mark is None else mark_window(frames.shape[1:], mark)
    if mark is not None and window is None:
        return CameraBallEstimate(
            "rejected_ball_mark_off_frame",
            locator=BallLocatorDiagnostics(ball_locator, "ball_mark_off_frame", ball_mark=mark),
        )
    shot = _Shot(
        frames,
        timestamps_ns,
        trigger_ns,
        range_evidence,
        geometry,
        ops_ball_speed_mph,
        iwr_vertical_deg,
    )
    origin, diagnostics = None, BallLocatorDiagnostics("legacy", "legacy")
    if ball_locator == "disappeared":
        origin, diagnostics = _departed_origin(
            shot,
            zone=tee_zone,
            ground_anchor=ground_anchor,
            ball_tracker=ball_tracker,
            window=window,
        )
    diagnostics = replace(diagnostics, ball_mark=mark)
    if origin is None and ball_locator == "disappeared" and not legacy_fallback:
        diagnostics = replace(diagnostics, locator="disappeared")
        origin = CameraBallEstimate("rejected_no_departed_ball")
    if origin is None:
        origin = _legacy_origin(shot, ball_tracker, window)
    if isinstance(origin, CameraBallEstimate):
        estimate = origin
    else:
        tracked = _track_flight(shot, origin)
        estimate = tracked.estimate
        diagnostics = replace(
            diagnostics, gate_candidates=tracked.gate_candidates, gate_kept=tracked.gate_kept
        )
        if diagnostics.locator == "disappeared":
            estimate, diagnostics = _check_line_fit(shot, origin, tracked, diagnostics)
    diagnostics = replace(diagnostics, elapsed_ms=1e3 * (time.perf_counter() - started))
    return replace(estimate, locator=diagnostics)


def estimate_live_ball_flight(
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    *,
    flicker_min_rel_std: float = FLICKER_MIN_REL_STD,
    **kwargs,
) -> CameraBallEstimate:
    """The shot path's ball flight: mains-flicker compensation, then the estimator.

    ``flicker.compensate_ball_flicker`` (the step the low-light shadow runs
    too) measures the flicker on the mat below the hitting zone's top and at
    the ball (the window around ``ball_mark_px``, else the hitting zone, both
    from ``kwargs``) and removes it from the frames the locator and the flight
    search read; a calm clip passes unchanged. The caller's frames (the saved
    clip) are never modified. ``kwargs`` go to ``estimate_camera_ball_flight``
    unchanged; the measurement is the estimate's ``locator.flicker`` (without its
    wall time, which joins ``locator.elapsed_ms``).
    2026-11-02: a mains lamp at the tee made the ball flicker by +-35-40 % in
    daylight and the live locator rejected every clip (SNR 0.5-1.7).
    """
    compensated, flicker = compensate_ball_flicker(
        frames,
        tee_zone=kwargs.get("tee_zone"),
        ball_mark_px=kwargs.get("ball_mark_px"),
        min_rel_std=flicker_min_rel_std,
    )
    estimate = estimate_camera_ball_flight(compensated, timestamps_ns, **kwargs)
    if not isinstance(estimate, CameraBallEstimate) or estimate.locator is None:
        return estimate
    measured = flicker.to_dict()
    del measured["elapsed_ms"]
    elapsed = estimate.locator.elapsed_ms
    return replace(
        estimate,
        locator=replace(
            estimate.locator,
            flicker=measured,
            elapsed_ms=None if elapsed is None else elapsed + (flicker.elapsed_ms or 0.0),
        ),
    )


def _angle_delta(first_deg: float, second_deg: float) -> float:
    return (first_deg - second_deg + 180.0) % 360.0 - 180.0


def select_camera_assisted_horizontal(
    estimate: CameraBallEstimate,
    *,
    iwr_horizontal_deg: float | None,
    iwr_confidence: float | None,
) -> HorizontalFusionDecision:
    """Select accepted camera output while keeping IWR as an honest fallback."""
    camera_deg = estimate.horizontal_deg
    delta = (
        _angle_delta(camera_deg, iwr_horizontal_deg)
        if camera_deg is not None and iwr_horizontal_deg is not None
        else None
    )
    if estimate.depth_source == "camera_size" and camera_deg is not None:
        return HorizontalFusionDecision(
            camera_deg,
            "camera_only_experimental",
            0.30,
            "camera_only_experimental",
            iwr_horizontal_deg,
            camera_deg,
            delta,
        )
    if estimate.confidence_tier == "high" and camera_deg is not None:
        return HorizontalFusionDecision(
            camera_deg,
            "camera_assisted_experimental",
            0.75,
            "camera_assisted_high",
            iwr_horizontal_deg,
            camera_deg,
            delta,
        )
    if estimate.confidence_tier == "experimental" and camera_deg is not None:
        agreement = delta is not None and abs(delta) <= 3.0
        return HorizontalFusionDecision(
            camera_deg,
            "camera_assisted_experimental",
            0.45 if agreement else 0.30,
            (
                "camera_assisted_experimental_agreement"
                if agreement
                else "camera_experimental_disagreement"
                if iwr_horizontal_deg is not None
                else "camera_experimental_no_iwr"
            ),
            iwr_horizontal_deg,
            camera_deg,
            delta,
        )
    if iwr_horizontal_deg is not None and abs(iwr_horizontal_deg) > MAX_IWR_FALLBACK_ABS_DEG:
        return HorizontalFusionDecision(
            None,
            None,
            None,
            "camera_withheld_iwr_implausible",
            iwr_horizontal_deg,
            camera_deg,
            delta,
        )
    return HorizontalFusionDecision(
        iwr_horizontal_deg,
        "radar" if iwr_horizontal_deg is not None else None,
        iwr_confidence if iwr_horizontal_deg is not None else None,
        "camera_withheld_fallback_iwr",
        iwr_horizontal_deg,
        camera_deg,
        delta,
    )


__all__ = [
    "BALL_LOCATORS",
    "DEFAULT_BALL_LOCATOR",
    "BallCandidate",
    "BallLocatorDiagnostics",
    "CameraBallEstimate",
    "CameraBallGeometry",
    "HorizontalFusionDecision",
    "LineFit",
    "estimate_camera_ball_flight",
    "estimate_live_ball_flight",
    "select_camera_assisted_horizontal",
]
