"""Heel/toe impact point (and face height) measured across contact: method "D".

Experimental, labelled "berechnet". ``club_head_path.estimate_club_head_path``
runs it in the camera CV worker's ``club_data`` job next to the older
extrapolating estimator (``club_head_path._impact_location``, method "E"); the
calculated club data (``openflight.club_data``) takes D as its primary value and
E only as the fallback. It follows the owner-approved feasibility study
``analysis/treffpunkt-20261101`` (2026-10-25 range, 56/69 real shots):

1. Frame L is the last frame before contact (0-3.5 ms at 288 fps), frame P the
   first after it. The head is segmented with the club-head path's own
   segmentation (background difference, polarity or a woods closing of 0.30
   balls, shaft opened away, the teed-ball zone completed from the previous
   head) over L-5 ... L, so the track ends at L instead of the path's 0.2 ms
   guard; P and P+1 are segmented too, with the resting-ball zone compared
   against the late frames (the mat or tee without the ball) and the departing
   ball (OPS speed, launch angles) blanked and completed the same way.
2. Per frame the feature is the midpoint between the 5 % and 95 % quantiles of
   the silhouette's lower half, the lower half starting at the crown edge on
   the toe side so the shaft and hosel do not count. It is back-projected at
   the head's own depth: the ball depth (step 5), minus the
   silhouette's depth behind the ball (driver 71, wood 56, iron 36 mm), plus
   club speed x time (about 25 % slower after contact).
3. The contact value interpolates L and P linearly to the contact time; without
   a usable P it is the quadratic through L-2 ... L (``accepted_pre_contact``,
   larger sigma). Offset = resting-ball centre minus that midpoint, + toward the
   toe (right-handed: target-right). It is the ball against the silhouette's
   heel-toe midpoint, not against the face centre or sweet spot: a fixed
   per-club offset of up to about +/-5 mm is still unknown (Trackman iO check).
4. The height is the ball centre above the head's lower edge (98 % quantile),
   interpolated the same way and taken in world vertical with the camera pitch;
   meaningful only for a teed ball (``openflight.club_data`` shows it for teed
   woods and the driver only).
5. The mm scale is always the geometry's: the regulation ball (42.67 mm) at the
   known camera-ball range seen through the lens model (``scale_source``
   ``geometry``, the club-head path scene's expected ball diameter). Owner
   decision 2026-11-03: range balls have the standard size, the owner keeps the
   ball at the measured 1.50 m with device and ball on one level, so the size is
   taken, not measured. The earlier image measurement of the resting ball's
   diameter (and its session median) read 12-15 px against the lens's 16.4 px
   (2026-10-25: 14.2 px, i.e. 1.73 m; 2026-11-02 S1 #6: 12.3 px) and is not
   trusted. Without a lens model the expected diameter falls back to the
   anchor ball's own size, as everywhere in the camera geometry
   (``geometry.reference_focal_px``; ``scale_source`` still says ``geometry``):
   on the Mac or mock, and on the Pi without ``--camera-intrinsics`` or for a
   clip whose capture mode the lens model does not cover
   (``server._capture_intrinsics`` None).

Sigma per shot (``SIGMA_PARTS``, study section 3.7) and the gates (head length
+/-25 % of the club's session median, L-to-P width +/-20 %, |offset| <= 50 mm,
sigma <= 10 mm) are the study's. Per-frame noise and silhouette depths are the
2026-10-25 calibration; nothing is fitted to a reference. The per-frame noise
(``ClubModel.frame_sigma_mm``) was read at the old measured-ball scale (mm per
px about 15 % above the geometry's) and is kept as it is: it errs large.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import NamedTuple

import numpy as np
from scipy import ndimage

from openflight.camera.ball_flight import MPH_PER_MS, _normalized
from openflight.camera.club_head_path import (
    BACKGROUND_FRAMES,
    BACKGROUND_LEAD_S,
    FEATURE_QUANTILE,
    IMPACT_HEAD_LENGTH_RANGE_MM,
    IMPACT_MAX_ABS_MM,
    IMPACT_MAX_SIGMA_MM,
    HeadTrack,
    _background_reference,
    _Blob,
    _candidates,
    _complete,
    _completed_track,
    _disk,
    _hosel_vote,
    _Scene,
    _segment,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.geometry import camera_to_world
from openflight.clubs import ClubType

# Head segmentation of a wood (single closing radius, the ensemble's middle one).
WOODS_CLOSE_RADIUS_BALLS = 0.30
# Frames of the head track before contact (L-5 ... L) and the frames used per shot.
TRACK_FRAMES = 6
FEATURE_FRAMES_BEFORE = 3  # features from L-3 on
# The frames after contact compare the resting-ball zone against the late frames
# (the ball gone): at least this many, this long after contact.
BALL_MIN_AFTER_FRAMES = 10
BALL_STILL_S = 0.06
# The impact point's scale is always the geometry's (module docstring step 5); the
# log keeps the key so old and new logs read alike.
SCALE_SOURCE = "geometry"
# Post-contact segmentation: candidate search and the departing-ball blank.
POST_MAX_DISTANCE_BALLS = 1.5
POST_WIDTH_RATIO_RANGE = (0.6, 1.4)
DEPARTING_BALL_MARGIN_PX = 2.0
DEFAULT_LAUNCH_VERTICAL_DEG = 15.0
# Sigma budget (study section 3.7).
SCALE_SIGMA = 0.05
FACE_ANGLE_SIGMA_DEG = 5.0
FEATURE_FALLBACK_SIGMA_MM = 3.0
HEIGHT_FRAME_SIGMA_MM = 1.03  # driver sole edge, third differences, 2026-10-25
HEIGHT_MODEL_SIGMA_MM = 2.0
HEIGHT_PITCH_SIGMA_DEG = 1.0
HEIGHT_PITCH_LEVER_M = 0.1
# Gates (study section 3.8).
HEAD_LENGTH_TOLERANCE = 0.25
WIDTH_LP_TOLERANCE = 0.20
MAX_ABS_OFFSET_MM = IMPACT_MAX_ABS_MM
MAX_SIGMA_MM = IMPACT_MAX_SIGMA_MM
HEAD_LENGTH_RANGE_MM = IMPACT_HEAD_LENGTH_RANGE_MM
METHODS = ("interpolated", "pre_contact")
SIGMA_PARTS = ("noise", "curvature", "contact_time", "depth", "face_angle", "feature", "scale")


class ClubModel(NamedTuple):
    """Per-club-group constants of the measurement (2026-10-25 calibration).

    ``silhouette_behind_ball_m``: depth of the silhouette's widest heel-toe
    section behind the ball centre at contact (ball radius + head depth);
    ``post_speed_ratio``: head speed after / before impact; ``frame_sigma_mm``:
    per-frame noise of the midpoint (third differences over L-2 ... P+1).
    """

    group: str
    silhouette_behind_ball_m: float
    silhouette_sigma_m: float
    post_speed_ratio: float
    frame_sigma_mm: float


_BALL_RADIUS_M = 0.0213
CLUB_MODELS = {
    "driver": ClubModel("driver", _BALL_RADIUS_M + 0.050, 0.025, 0.70, 1.79),
    # Fairway woods (7-wood measured); hybrids assumed alike (not measured).
    "wood": ClubModel("wood", _BALL_RADIUS_M + 0.035, 0.020, 0.72, 1.79),
    "iron": ClubModel("iron", _BALL_RADIUS_M + 0.015, 0.010, 0.78, 1.67),
    "wedge": ClubModel("wedge", _BALL_RADIUS_M + 0.015, 0.010, 0.80, 1.67),
}
_WOOD_CLUBS = frozenset(
    {
        ClubType.WOOD_3,
        ClubType.WOOD_5,
        ClubType.WOOD_7,
        ClubType.HYBRID_3,
        ClubType.HYBRID_5,
        ClubType.HYBRID_7,
        ClubType.HYBRID_9,
    }
)
_WEDGE_CLUBS = frozenset({ClubType.PW, ClubType.GW, ClubType.SW, ClubType.LW})


def club_model(club: ClubType | None) -> ClubModel:
    """The measurement constants of a club's group; an unknown club counts as an iron."""
    if club is ClubType.DRIVER:
        return CLUB_MODELS["driver"]
    if club in _WOOD_CLUBS:
        return CLUB_MODELS["wood"]
    if club in _WEDGE_CLUBS:
        return CLUB_MODELS["wedge"]
    return CLUB_MODELS["iron"]


class BallLaunch(NamedTuple):
    """The departing ball, to blank it in the frames after contact (shot values)."""

    speed_mph: float
    vertical_deg: float | None = None
    horizontal_deg: float | None = None


@dataclass(frozen=True)
class ImpactPoint:  # pylint: disable=too-many-instance-attributes
    """Method D: heel/toe offset at contact (mm, + toward the toe) and the face height.

    ``height_mm`` is the ball centre above the head's lower edge for every club;
    only a teed ball makes it a face height (``openflight.club_data`` gates it).
    ``scale_mm_per_px`` is the image scale at the ball (``ball_distance_m`` from
    ``ball_diameter_px``, the regulation ball's expected diameter at the known
    range; ``scale_source`` is always ``geometry``); ``e_scale_ratio`` turns
    the extrapolating estimator's millimetres (configured distance) into this
    scale. ``sigma_parts`` are the sigma terms, ``post_status`` says per frame
    after contact (``"1"`` = P, ``"2"`` = P+1) whether the head was found.
    """

    status: str
    offset_mm: float | None = None
    sigma_mm: float | None = None
    method: str | None = None
    height_mm: float | None = None
    height_sigma_mm: float | None = None
    head_height_mm: float | None = None
    head_length_mm: float | None = None
    width_ratio_lp: float | None = None
    pre_contact_mm: float | None = None
    alt_feature_mm: float | None = None
    scale_mm_per_px: float | None = None
    scale_source: str | None = None
    ball_diameter_px: float | None = None
    ball_distance_m: float | None = None
    e_scale_ratio: float | None = None
    ball_reference: str | None = None
    ball_px: tuple[float, float] | None = None
    frame_l: int | None = None
    tau_l_ms: float | None = None
    tau_p_ms: float | None = None
    polarity: str | None = None
    hosel_vote: float | None = None
    track_frames: tuple[int, ...] = ()
    post_status: dict[str, str] = field(default_factory=dict)
    sigma_parts: dict[str, float] = field(default_factory=dict)
    error: str | None = None  # the exception type when the measurement raised

    @property
    def accepted(self) -> bool:
        """Whether the offset passed every gate (``accepted`` or ``accepted_pre_contact``)."""
        return self.status.startswith("accepted")

    def to_dict(self) -> dict:
        """JSON-ready form for the session log."""
        return {
            "status": self.status,
            "offset_mm": self.offset_mm,
            "sigma_mm": self.sigma_mm,
            "method": self.method,
            "height_mm": self.height_mm,
            "height_sigma_mm": self.height_sigma_mm,
            "head_height_mm": self.head_height_mm,
            "head_length_mm": self.head_length_mm,
            "width_ratio_lp": self.width_ratio_lp,
            "pre_contact_mm": self.pre_contact_mm,
            "alt_feature_mm": self.alt_feature_mm,
            "scale_mm_per_px": self.scale_mm_per_px,
            "scale_source": self.scale_source,
            "ball_diameter_px": self.ball_diameter_px,
            "ball_distance_m": self.ball_distance_m,
            "e_scale_ratio": self.e_scale_ratio,
            "ball_reference": self.ball_reference,
            "ball_px": None if self.ball_px is None else list(self.ball_px),
            "frame_l": self.frame_l,
            "tau_l_ms": self.tau_l_ms,
            "tau_p_ms": self.tau_p_ms,
            "polarity": self.polarity,
            "hosel_vote": self.hosel_vote,
            "track_frames": list(self.track_frames),
            "post_status": dict(self.post_status),
            "sigma_parts": dict(self.sigma_parts),
            "error": self.error,
        }


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)


def _interp_quantile(values: np.ndarray, weights: np.ndarray, quantile: float) -> float:
    """Weighted quantile, each sample at the middle of its weight (the study's ``wq``).

    The calibration (frame noise, silhouette depths) was measured with it, so D
    keeps it rather than ``club_head_path._weighted_quantile``.
    """
    order = np.argsort(values, kind="stable")
    ordered, mass = values[order], weights[order]
    centres = np.cumsum(mass) - 0.5 * mass
    return float(np.interp(quantile * mass.sum(), centres, ordered))


# ----------------------------------------------------------------------------- frames


class _BallFrame(NamedTuple):
    """The resting ball in camera coordinates (lateral x signed target-right)."""

    lateral_m: float
    depth_m: float
    up_m: float  # world vertical relative to the camera
    image_z: float
    diameter_px: float


class _Clip(NamedTuple):
    """Per-clip state shared by the frames before and after contact."""

    stack: np.ndarray
    scene: _Scene
    crop: tuple[slice, slice]
    early: np.ndarray
    woods: bool
    toe_right: bool  # the toe lies toward image right (pixels)


def _ball_frame(scene: _Scene, ball: ReferenceBall, diameter_px: float) -> _BallFrame | None:
    geometry, focal = scene.geometry, scene.model.focal_px
    image_x, image_z = _normalized(ball.x, ball.y, focal_px=focal, geometry=geometry)
    left, _z = _normalized(ball.x - diameter_px / 2.0, ball.y, focal_px=focal, geometry=geometry)
    right, _z = _normalized(ball.x + diameter_px / 2.0, ball.y, focal_px=focal, geometry=geometry)
    width = abs(float(right) - float(left))
    if not width > 0.0:
        return None
    depth = geometry.ball_diameter_m / width
    _lateral, _forward, up = camera_to_world(
        image_x, 1.0, image_z, pitch_rad=scene.model.pitch_rad, yaw_rad=0.0
    )
    return _BallFrame(float(image_x) * depth, depth, float(up) * depth, float(image_z), diameter_px)


def _head_track(
    clip: _Clip, last: int, prior: HeadTrack | None = None
) -> tuple[str, dict[int, _Blob], list[_Blob]] | None:
    """(polarity, completed heads by frame, the raw track) over L-5 ... L, else None.

    ``prior`` is the club-head path's track of the same segmentation: when it
    covers exactly these frames it is the same result, and is reused.
    """
    pre = np.arange(max(0, last - TRACK_FRAMES + 1), last + 1)
    if prior is not None and prior.frames == tuple(int(index) for index in pre):
        if prior.polarity is None:
            return None
        return prior.polarity, {blob.index: blob for blob in prior.completed}, list(prior.track)
    early_frames = clip.stack[clip.early]
    if clip.woods:
        segmented = _segment(
            clip.stack,
            pre,
            early_frames,
            clip.scene,
            polarities=("abs",),
            close_radius_balls=WOODS_CLOSE_RADIUS_BALLS,
        )
    else:
        segmented = _segment(clip.stack, pre, early_frames, clip.scene)
    if segmented is None:
        return None
    polarity, track, signed = segmented
    completed = _completed_track(track, signed, clip.scene)
    return polarity, {blob.index: blob for blob in completed}, track


class _PostContact:
    """Segments the head in the frames after contact (study ``Segmenter.post_contact``)."""

    def __init__(self, clip: _Clip, polarity: str, ball: _BallFrame, launch: BallLaunch):
        self.clip, self.polarity, self.ball, self.launch = clip, polarity, ball, launch
        scene = clip.scene
        reference, self.threshold = _background_reference(
            clip.stack[clip.early][(slice(None), *clip.crop)].astype(np.float32)
        )
        # The resting ball is gone after contact: inside its (dilated) zone the
        # background is the late median (mat or tee without the ball).
        late = np.nonzero(scene.times_s > scene.contact_time_s + BALL_STILL_S)[0]
        self.reference = reference
        if len(late) >= BALL_MIN_AFTER_FRAMES:
            zone = ndimage.binary_dilation(scene.ball_zone, _disk(2))
            rows, cols = np.nonzero(zone)
            box = (slice(rows.min(), rows.max() + 1), slice(cols.min(), cols.max() + 1))
            offset = (clip.crop[0].start, clip.crop[1].start)
            frame_box = (
                slice(box[0].start + offset[0], box[0].stop + offset[0]),
                slice(box[1].start + offset[1], box[1].stop + offset[1]),
            )
            late_box = np.median(
                clip.stack[late][(slice(None), *frame_box)].astype(np.float32), axis=0
            )
            reference_post = reference.copy()
            reference_post[box] = np.where(zone[box], late_box, reference[box])
            self.reference = reference_post
        self._grid: tuple[np.ndarray, np.ndarray] | None = None

    def _signed(self, index: int) -> np.ndarray:
        diff = self.clip.stack[(index, *self.clip.crop)].astype(np.float32) - self.reference
        if self.polarity == "abs":
            return np.abs(diff)
        return -diff if self.polarity == "dark" else diff

    def _normalized_grid(self) -> tuple[np.ndarray, np.ndarray]:
        if self._grid is None:
            scene = self.clip.scene
            height, width = scene.ground_forward_m.shape
            x0, y0 = scene.origin
            ys, xs = np.mgrid[y0 : y0 + height, x0 : x0 + width]
            gx, gz = _normalized(
                xs.ravel().astype(float),
                ys.ravel().astype(float),
                focal_px=scene.model.focal_px,
                geometry=scene.geometry,
            )
            self._grid = (np.reshape(gx, xs.shape), np.reshape(gz, xs.shape))
        return self._grid

    def departing_ball(self, tau_ms: float) -> np.ndarray:
        """The departing ball's disk (ROI mask) at ``tau_ms`` after contact.

        Camera pitch only (no yaw), as in the study: the blank carries a margin.
        """
        speed = self.launch.speed_mph / MPH_PER_MS
        vertical = math.radians(
            DEFAULT_LAUNCH_VERTICAL_DEG
            if self.launch.vertical_deg is None or not math.isfinite(self.launch.vertical_deg)
            else self.launch.vertical_deg
        )
        horizontal = math.radians(
            0.0
            if self.launch.horizontal_deg is None or not math.isfinite(self.launch.horizontal_deg)
            else self.launch.horizontal_deg
        )
        elapsed = tau_ms / 1e3
        forward = speed * math.cos(vertical) * elapsed
        up = speed * math.sin(vertical) * elapsed
        lateral = forward * math.sin(horizontal)
        pitch = self.clip.scene.model.pitch_rad
        # World (forward, up) displacement into the camera's (depth, up) axes.
        depth = self.ball.depth_m + math.cos(pitch) * forward + math.sin(pitch) * up
        cam_up = self.ball.image_z * self.ball.depth_m - math.sin(pitch) * forward
        cam_up += math.cos(pitch) * up
        centre_x = (self.ball.lateral_m + lateral) / depth
        centre_z = cam_up / depth
        focal = self.clip.scene.model.focal_px
        radius = (0.5 * self.ball.diameter_px * self.ball.depth_m / depth) + (
            DEPARTING_BALL_MARGIN_PX
        )
        gx, gz = self._normalized_grid()
        return (gx - centre_x) ** 2 + (gz - centre_z) ** 2 <= (radius / focal) ** 2

    def head(self, index: int, previous: _Blob, older: _Blob) -> tuple[_Blob | None, str]:
        """The head in frame ``index`` after contact, completed from ``previous``."""
        scene = self.clip.scene
        tau_ms = (scene.times_s[index] - scene.contact_time_s) * 1e3
        blind = self.departing_ball(tau_ms)
        blind_scene = replace(scene, ball_zone=blind)
        signed = np.where(blind, 0.0, self._signed(index))
        candidates = _candidates(
            signed,
            self.threshold,
            index=index,
            scene=blind_scene,
            close_radius_balls=WOODS_CLOSE_RADIUS_BALLS if self.clip.woods else 0.0,
        )
        if not candidates:
            return None, "no_candidate"
        times = scene.times_s
        ratio = (times[index] - times[previous.index]) / max(
            times[previous.index] - times[older.index], 1e-9
        )
        predicted = tuple((previous.centroid[a] - older.centroid[a]) * ratio for a in (0, 1))
        target = (previous.centroid[0] + predicted[0], previous.centroid[1] + predicted[1])
        best = min(
            candidates,
            key=lambda blob: math.hypot(blob.centroid[0] - target[0], blob.centroid[1] - target[1]),
        )
        distance = math.hypot(best.centroid[0] - target[0], best.centroid[1] - target[1])
        if distance > POST_MAX_DISTANCE_BALLS * scene.ball_px:
            return None, "candidate_too_far"
        low, high = POST_WIDTH_RATIO_RANGE
        if not low <= best.width_px / max(previous.width_px, 1.0) <= high:
            return None, "width_jump"
        return _complete(best, previous, predicted, signed, blind_scene), "ok"


def _features(blob: _Blob, ball_row: float, ball_d_px: float, toe_right: bool) -> dict:
    """Heel-toe extents (lower half from the toe crown; band at ball height), sole, crown."""
    xs, ys, weights = blob.xs, blob.ys, blob.weights
    x_lo = _interp_quantile(xs, weights, 0.01)
    x_hi = _interp_quantile(xs, weights, 0.99)
    toe_side = xs >= x_lo + 0.6 * (x_hi - x_lo) if toe_right else xs <= x_lo + 0.4 * (x_hi - x_lo)
    sole_y = _interp_quantile(ys, weights, 0.98)
    crown_y = (
        _interp_quantile(ys[toe_side], weights[toe_side], 0.02)
        if np.count_nonzero(toe_side) >= 6
        else _interp_quantile(ys, weights, 0.02)
    )
    out: dict = {"sole_y": sole_y, "crown_y": crown_y}
    selections = {
        "lower": ys >= sole_y - 0.5 * (sole_y - crown_y),
        "band": np.abs(ys - ball_row) <= 0.5 * ball_d_px,
    }
    for name, selected in selections.items():
        if np.count_nonzero(selected) < 6:
            continue
        out[name] = (
            _interp_quantile(xs[selected], weights[selected], FEATURE_QUANTILE),
            _interp_quantile(xs[selected], weights[selected], 1.0 - FEATURE_QUANTILE),
        )
    bottom = ys >= sole_y - 2.0
    out["sole_x"] = float(np.average(xs[bottom], weights=weights[bottom]))
    return out


def _frame_values(  # pylint: disable=too-many-arguments
    clip: _Clip,
    blobs: dict[int, _Blob],
    *,
    ball: ReferenceBall,
    ball_frame: _BallFrame,
    model: ClubModel,
    window: tuple[int, int],
) -> dict[int, dict]:
    """Per frame in ``window``: tau, the features' midpoints/lengths (m) and sole/crown heights."""
    scene = clip.scene
    focal, geometry = scene.model.focal_px, scene.geometry
    pitch = scene.model.pitch_rad
    speed = scene.forward_speed_ms
    out = {}
    for index in sorted(blobs):
        if not window[0] <= index <= window[1]:
            continue
        feats = _features(blobs[index], ball.y, ball_frame.diameter_px, clip.toe_right)
        tau = (scene.times_s[index] - scene.contact_time_s) * 1e3
        travel = speed * tau if tau < 0.0 else speed * model.post_speed_ratio * tau
        depth = ball_frame.depth_m - model.silhouette_behind_ball_m + travel / 1e3
        entry = {"tau": tau}
        for name in ("lower", "band"):
            if name not in feats:
                continue
            edges = np.asarray(feats[name], dtype=float)
            image_x, _z = _normalized(edges, np.full(2, ball.y), focal_px=focal, geometry=geometry)
            first, second = float(image_x[0]) * depth, float(image_x[1]) * depth
            entry[name] = {"mid_m": 0.5 * (first + second), "len_m": abs(second - first)}
        image_x, image_z = _normalized(
            np.full(2, feats["sole_x"]),
            np.asarray([feats["sole_y"], feats["crown_y"]]),
            focal_px=focal,
            geometry=geometry,
        )
        _lateral, _forward, up = camera_to_world(
            image_x, np.ones(2), image_z, pitch_rad=pitch, yaw_rad=0.0
        )
        entry["sole_up_m"], entry["crown_up_m"] = float(up[0]) * depth, float(up[1]) * depth
        out[index] = entry
    return out


def _contact_value(values: dict[int, float], tau: dict[int, float], last: int) -> dict:
    """Interpolation L-P at contact, the pre-contact quadratic and the frame differences."""
    first_after = last + 1
    out: dict = {}
    if last in values and first_after in values:
        t_l, t_p = tau[last], tau[first_after]
        weight = -t_l / (t_p - t_l)
        out["int"] = values[last] + weight * (values[first_after] - values[last])
        out["weight_p"] = weight
        out["chord_speed"] = (values[first_after] - values[last]) / (t_p - t_l)
    pre = [index for index in sorted(values) if last - 2 <= index <= last]
    times = np.asarray([tau[index] for index in pre])
    points = np.asarray([values[index] for index in pre])
    if len(pre) >= 3:
        out["pre_quad"] = float(np.polyval(np.polyfit(times, points, 2), 0.0))
    if len(pre) >= 2:
        slope, intercept = np.polyfit(times[-2:], points[-2:], 1)
        out["pre_lin"] = float(intercept)
        out["pre_speed"] = float(slope)
    if last in values:
        out["raw_l"] = values[last]
    sequence = list(range(last - 2, first_after + 2))
    if all(index in values for index in sequence):
        series = np.asarray([values[index] for index in sequence])
        out["second_diff_l"] = float(series[1] - 2.0 * series[2] + series[3])
        out["third_diffs"] = [float(value) for value in np.diff(series, 3)]
    return out


# ----------------------------------------------------------------------------- measurement


def _frame_interval_ms(times_s: np.ndarray, last: int) -> float:
    lo, hi = max(0, last - 5), min(len(times_s), last + 6)
    steps = np.diff(times_s[lo:hi])
    steps = steps[steps > 0.0]
    return float(np.median(steps)) * 1e3 if len(steps) else math.nan


def _sigma_parts(  # pylint: disable=too-many-arguments
    lateral: dict,
    *,
    method: str,
    value: float,
    alt_value: float | None,
    tau: tuple[float, float],
    frame_ms: float,
    contact_sigma_ms: float,
    ball_image_x: float,
    model: ClubModel,
) -> dict[str, float]:
    """The study's per-shot sigma terms (mm)."""
    tau_l, tau_p = tau
    sigma_frame = model.frame_sigma_mm
    if method == "interpolated":
        weight = lateral["weight_p"]
        noise = sigma_frame * math.hypot(1.0 - weight, weight)
        accel = (lateral.get("second_diff_l") or 0.0) * 1e3 / frame_ms**2  # mm/ms^2
        curvature = abs(accel) * abs(tau_l) * tau_p / 2.0
        speed = abs(lateral["chord_speed"]) * 1e3
    else:
        g = abs(tau_l) / frame_ms
        noise = sigma_frame * math.sqrt(
            ((1 + g) * (2 + g) / 2) ** 2 + (g * (2 + g)) ** 2 + (g * (1 + g) / 2) ** 2
        )
        curvature = 0.0
        speed = abs(lateral.get("pre_speed") or 0.0) * 1e3
    return {
        "noise": noise,
        "curvature": curvature,
        "contact_time": speed * contact_sigma_ms,
        "depth": abs(ball_image_x) * model.silhouette_sigma_m * 1e3,
        "face_angle": (model.silhouette_behind_ball_m - _BALL_RADIUS_M)
        * math.sin(math.radians(FACE_ANGLE_SIGMA_DEG))
        * 1e3,
        "feature": (
            0.5 * abs(value - alt_value) if alt_value is not None else FEATURE_FALLBACK_SIGMA_MM
        ),
        "scale": SCALE_SIGMA * abs(value),
    }


def _gate(
    *,
    offset_mm: float,
    sigma_mm: float,
    head_length_mm: float | None,
    width_ratio: float | None,
    head_length_median_mm: float | None,
) -> str | None:
    """The first failed gate, or None: head length, L-to-P width, offset, sigma."""
    low, high = HEAD_LENGTH_RANGE_MM
    if head_length_mm is None or not low <= head_length_mm <= high:
        return "rejected_head_length"
    if (
        head_length_median_mm
        and abs(head_length_mm / head_length_median_mm - 1.0) > HEAD_LENGTH_TOLERANCE
    ):
        return "rejected_head_length"
    if width_ratio is not None and abs(width_ratio - 1.0) > WIDTH_LP_TOLERANCE:
        return "rejected_width_jump"
    if not (math.isfinite(offset_mm) and abs(offset_mm) <= MAX_ABS_OFFSET_MM):
        return "rejected_implausible_offset"
    if not (math.isfinite(sigma_mm) and sigma_mm <= MAX_SIGMA_MM):
        return "rejected_sigma_too_large"
    return None


def head_length_plausible(head_length_mm: float | None) -> bool:
    """Whether a D head length lies in the absolute range (the session median learns from it)."""
    if head_length_mm is None or not math.isfinite(head_length_mm):
        return False
    low, high = HEAD_LENGTH_RANGE_MM
    return low <= head_length_mm <= high


def _height(
    frames: dict[int, dict], last: int, ball_frame: _BallFrame, method: str
) -> tuple[float | None, float | None, float | None]:
    """(ball centre above the head's lower edge, its sigma, the visible head height), mm."""
    tau = {index: entry["tau"] for index, entry in frames.items()}
    sole = _contact_value({k: e["sole_up_m"] for k, e in frames.items()}, tau, last)
    crown = _contact_value({k: e["crown_up_m"] for k, e in frames.items()}, tau, last)
    key = "int" if method == "interpolated" else "pre_quad"
    if key not in sole:
        return None, None, None
    height = (ball_frame.up_m - sole[key]) * 1e3
    if "int" in sole and "int" in crown:
        head_height = (crown["int"] - sole["int"]) * 1e3
    elif "raw_l" in sole and "raw_l" in crown:
        head_height = (crown["raw_l"] - sole["raw_l"]) * 1e3
    else:
        head_height = None
    factor = 0.75 if method == "interpolated" else 2.5
    sigma = math.sqrt(
        (HEIGHT_FRAME_SIGMA_MM * factor) ** 2
        + HEIGHT_MODEL_SIGMA_MM**2
        + (HEIGHT_PITCH_LEVER_M * 1e3 * math.sin(math.radians(HEIGHT_PITCH_SIGMA_DEG))) ** 2
    )
    return height, sigma, head_height


def measure_impact_point(  # pylint: disable=too-many-arguments,too-many-return-statements
    frames: np.ndarray,
    scene: _Scene,
    *,
    club: ClubType | None,
    woods: bool,
    contact_sigma_s: float,
    ball: tuple[ReferenceBall, str],
    launch: BallLaunch | None,
    handedness: str = "right",
    head_length_median_mm: float | None = None,
    prior_track: HeadTrack | None = None,
) -> ImpactPoint:
    """Method D on one clip (see the module docstring); never raises on a bad clip.

    ``scene`` is the club-head path's scene (resting ball, camera model, club
    speed, contact time); ``ball`` the ball the impact refers to and its
    reference name (``club_head_path._impact_ball``, else the resting ball);
    ``launch`` the shot's ball speed and launch angles (None: no frame after
    contact is used). ``head_length_median_mm`` is the session median of this
    club's D head lengths (None until the server has enough). The mm scale is
    the scene's expected ball diameter (``scene.ball_px``: the regulation ball
    at the known range through the lens model), never a measured one.
    ``prior_track`` is the club-head path's run of the same segmentation,
    reused when it ends in L.
    """
    stack = np.asarray(frames)
    times_s = scene.times_s
    model = club_model(club)
    spot, reference = ball
    toe_sign = 1.0 if handedness == "right" else -1.0
    tau_all = (times_s - scene.contact_time_s) * 1e3
    before = np.nonzero(tau_all < 0.0)[0]
    base = {
        "ball_reference": reference,
        "ball_px": (round(float(spot.x), 2), round(float(spot.y), 2)),
    }
    if before.size == 0:
        return ImpactPoint("rejected_no_frame_before_contact", **base)
    last = int(before[-1])
    base.update(
        frame_l=last,
        tau_l_ms=_round(float(tau_all[last]), 3),
        tau_p_ms=_round(float(tau_all[last + 1]), 3) if last + 1 < len(times_s) else None,
    )
    # The regulation ball at the known range (lens model; without one, on the
    # Mac or mock or a Pi clip without intrinsics, the anchor's own size, see
    # ``club_head_path._build_scene``).
    diameter = float(scene.ball_px)
    ball_frame = _ball_frame(scene, spot, diameter)
    if ball_frame is None:
        return ImpactPoint("rejected_no_ball_scale", **base)
    geometry = scene.geometry
    configured_range = math.sqrt(
        geometry.camera_lateral_offset_m**2
        + geometry.camera_ball_forward_m**2
        + (geometry.ball_height_m - geometry.camera_height_m) ** 2
    )
    base.update(
        scale_mm_per_px=_round(ball_frame.depth_m / scene.model.focal_px * 1e3, 3),
        scale_source=SCALE_SOURCE,
        ball_diameter_px=_round(diameter),
        ball_distance_m=_round(ball_frame.depth_m, 4),
        e_scale_ratio=_round(ball_frame.depth_m / configured_range, 4),
    )
    height, width = scene.ground_forward_m.shape
    x0, y0 = scene.origin
    clip = _Clip(
        stack=stack,
        scene=scene,
        crop=(slice(y0, y0 + height), slice(x0, x0 + width)),
        early=np.nonzero(times_s < scene.contact_time_s - BACKGROUND_LEAD_S)[0][:BACKGROUND_FRAMES],
        woods=woods,
        toe_right=toe_sign * geometry.horizontal_pixel_sign > 0.0,
    )
    tracked = _head_track(clip, last, prior_track)
    if tracked is None:
        return ImpactPoint("rejected_head_not_found", **base)
    polarity, blobs, track = tracked
    base.update(
        polarity=polarity,
        hosel_vote=_hosel_vote(track),
        track_frames=tuple(sorted(blobs)),
    )
    if last not in blobs:
        return ImpactPoint("rejected_no_head_before_contact", **base)
    base["post_status"] = _post_contact(
        clip, polarity, blobs, ball_frame=ball_frame, launch=launch, last=last
    )
    frames_used = _frame_values(
        clip,
        blobs,
        ball=spot,
        ball_frame=ball_frame,
        model=model,
        window=(last - FEATURE_FRAMES_BEFORE, last + 2),
    )
    tau = {index: entry["tau"] for index, entry in frames_used.items()}
    lateral = _contact_value(
        {k: e["lower"]["mid_m"] for k, e in frames_used.items() if "lower" in e}, tau, last
    )
    if "int" in lateral:
        method, mid = "interpolated", lateral["int"]
    elif "pre_quad" in lateral:
        method, mid = "pre_contact", lateral["pre_quad"]
    else:
        return ImpactPoint("rejected_no_contact_value", **base)
    offset = toe_sign * (ball_frame.lateral_m - mid) * 1e3
    band = _contact_value(
        {k: e["band"]["mid_m"] for k, e in frames_used.items() if "band" in e}, tau, last
    )
    band_mid = band.get("int", band.get("pre_quad"))
    alt = None if band_mid is None else toe_sign * (ball_frame.lateral_m - band_mid) * 1e3
    lengths = [
        frames_used[index]["lower"]["len_m"]
        for index in (last, last + 1)
        if index in frames_used and "lower" in frames_used[index]
    ]
    head_length = float(np.mean(lengths)) * 1e3 if lengths else None
    width_ratio = lengths[1] / lengths[0] if len(lengths) == 2 and lengths[0] > 0 else None
    parts = _sigma_parts(
        lateral,
        method=method,
        value=offset,
        alt_value=alt,
        tau=(float(tau_all[last]), float(tau_all[last + 1]) if last + 1 < len(tau_all) else 0.0),
        frame_ms=_frame_interval_ms(times_s, last),
        contact_sigma_ms=max(contact_sigma_s, 0.0) * 1e3,
        ball_image_x=ball_frame.lateral_m / ball_frame.depth_m,
        model=model,
    )
    sigma = math.sqrt(sum(value**2 for value in parts.values()))
    pre_quad = lateral.get("pre_quad")
    height_mm, height_sigma, head_height = _height(frames_used, last, ball_frame, method)
    result = ImpactPoint(
        "pending",
        offset_mm=_round(offset),
        sigma_mm=_round(sigma),
        method=method,
        height_mm=_round(height_mm),
        height_sigma_mm=_round(height_sigma),
        head_height_mm=_round(head_height, 1),
        head_length_mm=_round(head_length, 1),
        width_ratio_lp=_round(width_ratio, 3),
        pre_contact_mm=(
            None if pre_quad is None else _round(toe_sign * (ball_frame.lateral_m - pre_quad) * 1e3)
        ),
        alt_feature_mm=_round(alt),
        sigma_parts={name: round(float(parts[name]), 2) for name in SIGMA_PARTS},
        **base,
    )
    failed = _gate(
        offset_mm=offset,
        sigma_mm=sigma,
        head_length_mm=head_length,
        width_ratio=width_ratio,
        head_length_median_mm=head_length_median_mm,
    )
    if failed is not None:
        return replace(result, status=failed)
    return replace(
        result, status="accepted" if method == "interpolated" else "accepted_pre_contact"
    )


def _post_contact(  # pylint: disable=too-many-arguments
    clip: _Clip,
    polarity: str,
    blobs: dict[int, _Blob],
    *,
    ball_frame: _BallFrame,
    launch: BallLaunch | None,
    last: int,
) -> dict[str, str]:
    """Add the heads of P and P+1 to ``blobs``; the status per frame after contact."""
    status: dict[str, str] = {}
    if launch is None or not (math.isfinite(launch.speed_mph) and launch.speed_mph > 0.0):
        return {"1": "no_ball_launch", "2": "no_ball_launch"}
    segmenter = _PostContact(clip, polarity, ball_frame, launch)
    for index in (last + 1, last + 2):
        previous, older = blobs.get(index - 1), blobs.get(index - 2)
        if previous is None or older is None or index >= len(clip.stack):
            status[str(index - last)] = "predecessor_missing"
            continue
        blob, why = segmenter.head(index, previous, older)
        status[str(index - last)] = why
        if blob is not None:
            blobs[index] = blob
    return status


__all__ = [
    "CLUB_MODELS",
    "METHODS",
    "SCALE_SOURCE",
    "BallLaunch",
    "ClubModel",
    "ImpactPoint",
    "club_model",
    "head_length_plausible",
    "measure_impact_point",
]
