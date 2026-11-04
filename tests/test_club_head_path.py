"""Tests for the camera club-head path estimator on synthetic rear-camera clips."""

import math
from dataclasses import dataclass, replace
from types import SimpleNamespace

import numpy as np
import pytest
from test_camera_ball_flight import render_departure_clip

import openflight.camera.club_head_path as club_head_path_module
from openflight.camera.ball_flight import (
    DEPARTURE_MAX_LAG_S,
    SPEED_OF_SOUND_M_S,
    CameraBallGeometry,
    CameraTimeZero,
    DepartureFit,
    _camera_model,
    _select_time_zero,
)
from openflight.camera.club_attack import ArcPath, AttackAngleResult, arc_points
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.club_head_path import (
    ACOUSTIC_STAMP_DELAY_MS,
    CLUB_BALL_MARK_MAX_SEARCH_RADII,
    CONTACT_SIGMA_S,
    HEAD_BEHIND_BALL_M,
    MAX_SIGMA_DEG,
    MIN_HEAD_CONTRAST,
    MIN_WOOD_HEAD_CONTRAST,
    WOOD_DIFFERENCE_LIMITS,
    WOODS_SEGMENTATION,
    AcousticDelayCalibrator,
    ClubBall,
    ClubHeadPathResult,
    ContactShadow,
    HeadObservation,
    HeadQuality,
    HeadQualityLimits,
    TeeControl,
    _difference_quality,
    _path_arc_fields,
    _PathRun,
    _woods_ensemble,
    _woods_mode,
    assess_head_quality,
    background_noise_sigma,
    estimate_club_data,
    estimate_club_head_path,
    estimate_departure_contact,
    expected_ball_diameter_px,
    locate_club_ball,
    mark_reference_ball,
    refine_search_radius_px,
    refine_teed_ball,
    resolve_club_ball,
    select_contact_time,
    shadow_contact_time,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.intrinsics import CameraIntrinsics
from openflight.camera.tee_zone import TeeZone
from openflight.clubs import ClubType

WIDTH, HEIGHT = 640, 400
FOCAL_PX = 576.0
FRAME_S = 1.0 / 288.0
FRAME_COUNT = 60
CONTACT_FRAME = 44
CONTACT_AFTER_FRAME_S = 0.0015
CLUB_SPEED_MPH = 60.0
SPEED_MS = CLUB_SPEED_MPH / 2.23694
HEAD_HALF_WIDTH_M = 0.037
HEAD_HEEL_HEIGHT_M = 0.03
HEAD_TOE_HEIGHT_M = 0.048
SUPERSAMPLE = 4

INTRINSICS = CameraIntrinsics(
    fx=FOCAL_PX,
    fy=FOCAL_PX,
    cx=WIDTH / 2.0,
    cy=HEIGHT / 2.0,
    dist_coeffs=(0.0, 0.0, 0.0, 0.0, 0.0),
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
    rotate_180=False,
    mirror_horizontal=False,
    scaler_crop=None,
    rms_reprojection_px=0.3,
)
GEOMETRY = CameraBallGeometry(
    camera_height_m=0.182,
    radar_height_m=0.132,
    tee_range_m=1.5,
    ball_height_m=0.02135,
    camera_lateral_offset_m=-0.025,
    camera_forward_offset_m=0.007,
    yaw_from_reference_ball=True,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
    intrinsics=INTRINSICS,
)
DARK_ON_BRIGHT = {"sky": 255.0, "mat": 105.0, "head": 25.0, "shaft": 30.0, "ball": 45.0}
BRIGHT_ON_DARK = {"sky": 20.0, "mat": 40.0, "head": 205.0, "shaft": 190.0, "ball": 230.0}
# A wood over the range mat: the head is painted relative to what it covers (lit
# crown +40, dark face -35, a band matching the background between them).
WOOD_SCENE = {"sky": 170.0, "mat": 110.0, "head": 60.0, "shaft": 40.0, "ball": 45.0}
TWO_TONE_DELTAS = ((0.0, 0.3, -35.0), (0.3, 0.7, 0.0), (0.7, 1.0, 40.0))


CAMERA_ORIGIN = np.array(
    [GEOMETRY.camera_lateral_offset_m, GEOMETRY.camera_forward_offset_m, GEOMETRY.camera_height_m]
)


@dataclass(frozen=True)
class Camera:
    """Pinhole camera with the module's world frame: lateral, forward, vertical."""

    yaw_deg: float = 1.5
    pitch_deg: float = 1.0

    def project(self, points: np.ndarray) -> np.ndarray:
        """World points (n, 3) -> pixel (n, 2)."""
        yaw, pitch = math.radians(self.yaw_deg), math.radians(self.pitch_deg)
        lateral, forward, vertical = (np.atleast_2d(points) - CAMERA_ORIGIN).T
        cam_lateral = math.cos(yaw) * lateral - math.sin(yaw) * forward
        level = math.sin(yaw) * lateral + math.cos(yaw) * forward
        depth = math.cos(pitch) * level + math.sin(pitch) * vertical
        cam_up = -math.sin(pitch) * level + math.cos(pitch) * vertical
        return np.column_stack(
            (
                INTRINSICS.cx + FOCAL_PX * cam_lateral / depth,
                INTRINSICS.cy - FOCAL_PX * cam_up / depth,
            )
        )


def _coverage(shape_test, x0: int, y0: int, x1: int, y1: int) -> np.ndarray:
    """Supersampled pixel coverage of a shape over the box [x0, x1) x [y0, y1)."""
    offsets = (np.arange(SUPERSAMPLE) + 0.5) / SUPERSAMPLE - 0.5
    ys, xs = np.meshgrid(
        np.arange(y0, y1, dtype=float), np.arange(x0, x1, dtype=float), indexing="ij"
    )
    total = np.zeros(ys.shape)
    for dy in offsets:
        for dx in offsets:
            total += shape_test(xs + dx, ys + dy)
    return total / SUPERSAMPLE**2


def _paint(image: np.ndarray, shape_test, bbox, value: float, *, relative: bool = False) -> None:
    x0, y0, x1, y1 = (
        max(0, int(math.floor(bbox[0])) - 1),
        max(0, int(math.floor(bbox[1])) - 1),
        min(WIDTH, int(math.ceil(bbox[2])) + 2),
        min(HEIGHT, int(math.ceil(bbox[3])) + 2),
    )
    if x0 >= x1 or y0 >= y1:
        return
    alpha = _coverage(shape_test, x0, y0, x1, y1)
    if relative:
        image[y0:y1, x0:x1] += value * alpha
    else:
        image[y0:y1, x0:x1] = image[y0:y1, x0:x1] * (1.0 - alpha) + value * alpha


def _paint_polygon(
    image: np.ndarray, corners: np.ndarray, value: float, *, relative: bool = False
) -> None:
    """Fill a convex polygon (pixel corners in order); ``relative`` adds ``value`` instead."""
    edges = np.roll(corners, -1, axis=0) - corners
    orientation = np.sign(edges[0, 0] * edges[1, 1] - edges[0, 1] * edges[1, 0])

    def inside(xs, ys):
        result = np.ones(xs.shape, dtype=bool)
        for start, edge in zip(corners, edges):
            cross = edge[0] * (ys - start[1]) - edge[1] * (xs - start[0])
            result &= orientation * cross >= 0.0
        return result

    _paint(image, inside, (*corners.min(axis=0), *corners.max(axis=0)), value, relative=relative)


def _paint_segment(image, start, end, half_width: float, value: float) -> None:
    direction = end - start
    length_sq = float(direction @ direction)

    def inside(xs, ys):
        along = np.clip(
            ((xs - start[0]) * direction[0] + (ys - start[1]) * direction[1]) / length_sq, 0, 1
        )
        return (
            np.hypot(xs - start[0] - along * direction[0], ys - start[1] - along * direction[1])
            <= half_width
        )

    low, high = np.minimum(start, end) - half_width, np.maximum(start, end) + half_width
    _paint(image, inside, (*low, *high), value)


def _paint_disk(image, centre, radius: float, value: float) -> None:
    def inside(xs, ys):
        return np.hypot(xs - centre[0], ys - centre[1]) <= radius

    _paint(image, inside, (*(centre - radius), *(centre + radius)), value)


def head_position(tau_s: float, path_deg: float, lateral_accel: float) -> np.ndarray:
    """Head reference point: kinematic forward, lateral path + curvature, descending."""
    forward = GEOMETRY.ball_forward_m - HEAD_BEHIND_BALL_M + SPEED_MS * tau_s
    lateral = math.tan(math.radians(path_deg)) * SPEED_MS * tau_s + 0.5 * lateral_accel * tau_s**2
    height = 0.002 + 0.5 * 800.0 * tau_s**2
    return np.array([lateral, forward, height])


def arc_head_position(tau_s: float, scenario) -> np.ndarray:
    """Head reference point on a swing arc at the club speed (``club_attack.arc_points``)."""
    contact = np.array([0.0, GEOMETRY.ball_forward_m - HEAD_BEHIND_BALL_M, 0.002])
    plane = scenario.plane_deg if not scenario.left_handed else 180.0 - scenario.plane_deg
    return arc_points(
        contact,
        math.radians(scenario.path_deg),
        math.radians(scenario.attack_deg),
        math.radians(plane),
        1.0 / scenario.radius_m,
        np.array([SPEED_MS * tau_s]),
    )[0]


@dataclass(frozen=True)
class Scenario:
    """What a synthetic clip shows; the head moves along ``head_position``."""

    path_deg: float = 0.0
    lateral_accel: float = 0.0
    bright_head: bool = False
    camera: Camera = Camera()
    with_head: bool = True
    with_shadow: bool = False
    jitter_m: float = 0.0
    seed: int = 3
    # Swing arc (club_attack model) instead of the parabola when set.
    attack_deg: float | None = None
    plane_deg: float = 60.0
    radius_m: float = 1.2
    # Mirrored golfer: hosel and shaft on the head's +lateral side, toe on its -lateral side.
    left_handed: bool = False
    ball_lateral_m: float = 0.0
    two_tone: bool = False
    # Frames after the default clip end, and whether the ball leaves at contact.
    extra_frames: int = 0
    ball_departs: bool = False

    @property
    def palette(self) -> dict:
        """Grey levels of the scene parts."""
        if self.two_tone:
            return WOOD_SCENE
        return BRIGHT_ON_DARK if self.bright_head else DARK_ON_BRIGHT


def _paint_head(image: np.ndarray, centre: np.ndarray, scenario: Scenario) -> None:
    """The head quadrilateral (and its shaft) with the reference point at its sole centre."""
    camera, palette = scenario.camera, scenario.palette
    side = -1.0 if scenario.left_handed else 1.0  # +1: heel on -lateral, toe on +lateral
    heel_x, toe_x = -side * HEAD_HALF_WIDTH_M, side * HEAD_HALF_WIDTH_M
    heel_top = centre + [heel_x, 0.0, HEAD_HEEL_HEIGHT_M]
    if scenario.two_tone:
        for low, high, delta in TWO_TONE_DELTAS:
            band = np.asarray(
                [
                    centre + [heel_x, 0.0, low * HEAD_HEEL_HEIGHT_M],
                    centre + [toe_x, 0.0, low * HEAD_TOE_HEIGHT_M],
                    centre + [toe_x, 0.0, high * HEAD_TOE_HEIGHT_M],
                    centre + [heel_x, 0.0, high * HEAD_HEEL_HEIGHT_M],
                ]
            )
            if delta:
                _paint_polygon(image, camera.project(band), delta, relative=True)
    else:
        head = np.asarray(
            [
                centre + [heel_x, 0.0, 0.0],
                centre + [toe_x, 0.0, 0.0],
                centre + [toe_x, 0.0, HEAD_TOE_HEIGHT_M],
                heel_top,
            ]
        )
        _paint_polygon(image, camera.project(head), palette["head"])
    grip = heel_top + [-side * 0.35, -0.2, 0.85]
    shaft = camera.project(np.asarray([heel_top, grip]))
    _paint_segment(image, shaft[0], shaft[1], 2.0, palette["shaft"])


def render_clip(scenario: Scenario = Scenario()):
    """Synthetic rear-camera clip around contact; returns frames, timestamps, contact, ball."""
    camera, palette = scenario.camera, scenario.palette
    rng = np.random.default_rng(scenario.seed)
    count = FRAME_COUNT + scenario.extra_frames
    times = np.arange(count) * FRAME_S
    contact = times[CONTACT_FRAME] + CONTACT_AFTER_FRAME_S
    ball_centre = np.array(
        [scenario.ball_lateral_m, GEOMETRY.ball_forward_m, GEOMETRY.ball_height_m]
    )
    ball_px = camera.project(ball_centre)[0]
    ball_radius_px = (
        FOCAL_PX * GEOMETRY.ball_diameter_m / 2.0 / np.linalg.norm(ball_centre - CAMERA_ORIGIN)
    )
    horizon_row = int(ball_px[1] - 1.5 * ball_radius_px * 2)
    background = np.full((HEIGHT, WIDTH), palette["mat"])
    background[:horizon_row] = palette["sky"]
    texture = rng.normal(0.0, 6.0, (HEIGHT, WIDTH))
    background[horizon_row:] += texture[horizon_row:]
    empty = background.copy()
    _paint_disk(background, ball_px, ball_radius_px, palette["ball"])

    frames = np.empty((count, HEIGHT, WIDTH), dtype=np.uint8)
    for index, time_s in enumerate(times):
        tau = time_s - contact
        image = (empty if scenario.ball_departs and tau > 0.0 else background).copy()
        leg_x = ball_px[0] - 165.0 + 0.6 * min(index, FRAME_COUNT)
        _paint_polygon(
            image,
            np.array(
                [
                    [leg_x, 0.0],
                    [leg_x + 38.0, 0.0],
                    [leg_x + 30.0, ball_px[1] - 15.0],
                    [leg_x - 6.0, ball_px[1] - 15.0],
                ]
            ),
            palette["head"],
        )
        if scenario.with_head and -0.025 <= tau <= 0.0:
            if scenario.attack_deg is None:
                centre = head_position(tau, scenario.path_deg, scenario.lateral_accel)
            else:
                centre = arc_head_position(tau, scenario)
            centre[0] += scenario.jitter_m * (1.0 if index % 2 else -1.0)
            if scenario.with_shadow:
                shadow = [
                    centre + [dx, -0.045, -centre[2]]
                    for dx in (-HEAD_HALF_WIDTH_M, HEAD_HALF_WIDTH_M)
                ]
                shadow += [corner + [0.0, 0.045, 0.0] for corner in shadow[::-1]]
                corners = camera.project(np.asarray(shadow))
                _paint_polygon(image, corners, 0.6 * palette["mat"])
            _paint_head(image, centre, scenario)
        image += rng.normal(0.0, 2.5, image.shape)
        frames[index] = np.clip(np.round(image), 0, 255).astype(np.uint8)
    ball = ReferenceBall(
        x=float(ball_px[0]),
        y=float(ball_px[1]),
        diameter_px=float(2.0 * ball_radius_px),
        area_px=int(math.pi * ball_radius_px**2),
    )
    return frames, (times * 1e9).astype(np.int64), float(contact), ball


def estimate(frames, timestamps, contact, ball, **overrides):
    """Run the estimator with the synthetic geometry and club speed."""
    kwargs = {
        "contact_time_s": contact,
        "geometry": GEOMETRY,
        "club_speed_mph": CLUB_SPEED_MPH,
        "reference_ball": ball,
    }
    kwargs.update(overrides)
    return estimate_club_head_path(frames, timestamps, **kwargs)


@pytest.fixture(name="straight_clips", scope="module")
def fixture_straight_clips():
    """One rendered clip per straight path, shared by the module."""
    return {path: render_clip(Scenario(path_deg=path)) for path in (-6.0, 0.0, 6.0)}


@pytest.mark.parametrize("path_deg", [-6.0, 0.0, 6.0])
def test_recovers_straight_paths_against_a_bright_clipped_background(straight_clips, path_deg):
    result = estimate(*straight_clips[path_deg])

    assert result.status == "accepted"
    assert result.polarity == "dark"
    assert result.n_frames >= 5
    assert result.path_deg == pytest.approx(path_deg, abs=0.6)
    assert 0.0 < result.sigma_deg <= 1.0


@pytest.mark.parametrize("bright_head", [False, True], ids=["dark", "bright"])
def test_recovers_a_curved_path_slope_at_contact_in_both_polarities(bright_head):
    """300 m/s^2 lateral curvature turns the path ~12 deg over the window."""
    frames, timestamps, contact, ball = render_clip(
        Scenario(path_deg=3.0, lateral_accel=-300.0, bright_head=bright_head)
    )

    result = estimate(frames, timestamps, contact, ball)

    assert result.status == "accepted"
    assert result.polarity == ("bright" if bright_head else "dark")
    assert result.path_deg == pytest.approx(3.0, abs=0.8)


def test_reference_point_stays_on_the_head_not_the_leg_or_shaft(straight_clips):
    frames, timestamps, contact, ball = straight_clips[0.0]
    camera = Camera()

    result = estimate(frames, timestamps, contact, ball)

    assert result.observations
    for observation in result.observations:
        tau = observation.t_rel_ms / 1000.0
        expected = camera.project(head_position(tau, 0.0, 0.0))[0]
        measured = observation.features_px["face_centre"]
        assert measured[0] == pytest.approx(expected[0], abs=1.0)
        assert abs(measured[0] - ball.x) < 120.0


def test_head_passing_over_a_backlit_ball_and_its_shadow_is_completed():
    frames, timestamps, contact, ball = render_clip(Scenario(path_deg=-4.0, with_shadow=True))

    result = estimate(frames, timestamps, contact, ball)

    assert result.status == "accepted"
    assert result.path_deg == pytest.approx(-4.0, abs=0.8)
    widths = [item.width_m for item in result.observations if item.quality.ok]
    assert max(widths) / min(widths) < 1.15


def test_path_is_in_the_ball_derived_yaw_frame():
    turned = Camera(yaw_deg=-3.0, pitch_deg=2.0)
    frames, timestamps, contact, ball = render_clip(Scenario(path_deg=2.0, camera=turned))

    result = estimate(frames, timestamps, contact, ball)

    assert result.status == "accepted"
    assert result.path_deg == pytest.approx(2.0, abs=0.6)


def test_horizontal_offset_shifts_path_like_the_launch_direction(straight_clips):
    shifted = CameraBallGeometry(**{**GEOMETRY.__dict__, "horizontal_offset_deg": 1.5})

    base = estimate(*straight_clips[0.0])
    offset = estimate(*straight_clips[0.0], geometry=shifted)

    assert offset.path_deg == pytest.approx(base.path_deg + 1.5, abs=0.01)


def test_tapped_target_shifts_path_like_the_start_direction(straight_clips):
    # With a tapped target the ball flight measures start directions from the target
    # line (direction offset = -target azimuth); the club path must use the same frame,
    # or face to path and face to target pick up -T/k and -(1-k)T/k.
    frames, timestamps, contact, ball = straight_clips[0.0]
    targeted = replace(
        GEOMETRY, horizontal_offset_deg=1.5, target_px=(INTRINSICS.cx + 25.0, INTRINSICS.cy - 10.0)
    )
    offset_deg = _camera_model(ball, targeted).direction_offset_deg
    assert abs(offset_deg) > 3.0  # the target replaces the horizontal offset

    base = estimate(frames, timestamps, contact, ball)
    aimed = estimate(frames, timestamps, contact, ball, geometry=targeted)

    assert aimed.status == base.status == "accepted"
    assert aimed.path_deg == pytest.approx(base.path_deg + offset_deg, abs=0.011)
    for name, path in aimed.feature_paths_deg.items():
        assert path == pytest.approx(base.feature_paths_deg[name] + offset_deg, abs=0.011)


def test_contact_time_uncertainty_widens_sigma_on_a_curved_arc():
    clip = render_clip(Scenario(lateral_accel=-300.0))

    sharp = estimate(*clip)
    vague = estimate(*clip, contact_sigma_s=0.002)

    assert sharp.contact_sigma_deg == 0.0
    assert vague.contact_sigma_deg > 1.0
    assert vague.sigma_deg > sharp.sigma_deg
    assert vague.path_deg == sharp.path_deg


def test_detects_the_ball_when_none_is_given(straight_clips):
    frames, timestamps, contact, _ball = straight_clips[6.0]

    result = estimate(frames, timestamps, contact, None)

    assert result.status == "accepted"
    assert result.path_deg == pytest.approx(6.0, abs=0.8)


def test_rejects_noisy_tracks_on_sigma():
    frames, timestamps, contact, ball = render_clip(Scenario(jitter_m=0.006))

    result = estimate(frames, timestamps, contact, ball)

    assert result.status == "rejected_sigma_too_large"
    assert result.sigma_deg > MAX_SIGMA_DEG
    assert result.path_deg is not None


def test_rejects_a_clip_without_a_club_head():
    frames, timestamps, contact, ball = render_clip(Scenario(with_head=False))

    result = estimate(frames, timestamps, contact, ball)

    assert result.status == "rejected_head_not_found"
    assert result.path_deg is None


def test_rejects_when_too_few_frames_precede_contact(straight_clips):
    frames, timestamps, _contact, ball = straight_clips[0.0]

    result = estimate(frames, timestamps, float(timestamps[2]) / 1e9 + 0.001, ball)

    assert result.status == "rejected_too_few_frames"


def test_rejects_a_clip_without_pre_swing_background(straight_clips):
    frames, timestamps, contact, ball = straight_clips[0.0]

    result = estimate(frames[38:], timestamps[38:], contact, ball)

    assert result.status == "rejected_no_background"


def test_rejects_a_short_track_when_the_head_appears_late():
    frames, timestamps, contact, ball = render_clip()
    blank = render_clip(Scenario(with_head=False))[0]
    frames[: CONTACT_FRAME - 2] = blank[: CONTACT_FRAME - 2]

    result = estimate(frames, timestamps, contact, ball)

    assert result.status in {"rejected_short_track", "rejected_head_quality"}
    assert result.path_deg is None


@pytest.mark.parametrize("speed", [None, 0.0, float("nan")])
def test_rejects_missing_club_speed(straight_clips, speed):
    result = estimate(*straight_clips[0.0], club_speed_mph=speed)

    assert result.status == "rejected_no_club_speed"


def test_rejects_mismatched_timestamps(straight_clips):
    frames, timestamps, contact, ball = straight_clips[0.0]

    result = estimate(frames, timestamps[:-1], contact, ball)

    assert result.status == "rejected_invalid_input"


def test_unknown_feature_is_a_programming_error(straight_clips):
    with pytest.raises(ValueError):
        estimate(*straight_clips[0.0], feature="hosel")


def test_every_feature_reports_a_path(straight_clips):
    result = estimate(*straight_clips[6.0], feature="centroid")

    assert set(result.feature_paths_deg) == {"face_centre", "centroid", "sole", "toe", "heel"}
    assert result.feature == "centroid"
    assert result.path_deg == result.feature_paths_deg["centroid"]
    assert result.feature_paths_deg["face_centre"] == pytest.approx(6.0, abs=0.6)


def _head_frame(contrast: float, blur: int = 0, size: int = 30, head_value: float = 40.0):
    frame = np.full((120, 160), head_value + contrast)
    mask = np.zeros(frame.shape, dtype=bool)
    mask[40 : 40 + size // 2, 50 : 50 + size] = True
    frame[mask] = head_value
    if blur:
        kernel = np.ones(blur) / blur
        frame = np.apply_along_axis(lambda row: np.convolve(row, kernel, mode="same"), 1, frame)
        frame = np.apply_along_axis(lambda col: np.convolve(col, kernel, mode="same"), 0, frame)
    return frame, mask


def test_head_quality_accepts_a_sharp_contrasted_head_on_a_clipped_sky():
    frame, mask = _head_frame(contrast=215.0)

    quality = assess_head_quality(frame, mask)

    assert quality.ok
    assert quality.contrast == pytest.approx(215.0, abs=1.0)
    assert quality.width_px == 30.0
    assert quality.edge_width_px < 3.0


@pytest.mark.parametrize(
    ("frame_kwargs", "status"),
    [
        ({"contrast": 8.0}, "low_contrast"),
        ({"contrast": 120.0, "size": 8}, "too_small"),
        ({"contrast": 120.0, "blur": 9}, "blurred"),
        ({"contrast": 0.0, "head_value": 255.0}, "low_contrast"),
    ],
)
def test_head_quality_rejects_poor_head_regions(frame_kwargs, status):
    frame, mask = _head_frame(**frame_kwargs)

    assert assess_head_quality(frame, mask).status == status


def test_head_quality_rejects_a_clipped_head():
    frame, mask = _head_frame(contrast=-200.0, head_value=255.0)

    quality = assess_head_quality(frame, mask)

    assert quality.status == "clipped"
    assert quality.clipped_pct == 100.0


def test_head_quality_without_a_mask_is_not_found():
    quality = assess_head_quality(np.zeros((10, 10)), np.zeros((10, 10), dtype=bool))

    assert quality.status == "not_found"
    assert not quality.ok


# Contact time for the club data: the ball estimator's time zero, with a departure
# fit after the sound bound refused; the fallback is the calibrated acoustic
# estimate, the stamp's sound bound minus ACOUSTIC_STAMP_DELAY_MS.
TRIGGER_NS = 5_000_000_000
TEE_RANGE_M = 1.5
SOUND_BOUND_MS = -1e3 * TEE_RANGE_M / SPEED_OF_SOUND_M_S
ACAL_MS = SOUND_BOUND_MS - ACOUSTIC_STAMP_DELAY_MS


def live_time_zero(departure_ms: float | None) -> CameraTimeZero:
    """The ball estimator's time zero for a departure fit (ms from the trigger), via its gate."""
    trigger_s = TRIGGER_NS / 1e9
    acoustic_s = trigger_s + SOUND_BOUND_MS / 1e3
    fit = (
        None
        if departure_ms is None
        else DepartureFit(
            time_s=trigger_s + departure_ms / 1e3, n_points=6, rms_px=0.2, span_frames=2.0
        )
    )
    zero_s, source = _select_time_zero(fit, trigger_s=trigger_s, acoustic_s=acoustic_s)
    return CameraTimeZero(
        source=source,
        time_zero_ms=1e3 * (zero_s - trigger_s),
        departure_fit_ms=departure_ms,
        acoustic_ms=SOUND_BOUND_MS,
        tee_patch_ms=-3.0,
        iwr_impact_t_s=None,
    )


def test_contact_time_uses_a_departure_fit_before_the_sound_left_the_tee():
    contact = select_contact_time(
        live_time_zero(-5.6), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M
    )

    assert contact.source == "departure_fit"
    assert contact.reason == "departure_fit"
    assert contact.t0_ms == pytest.approx(-5.6)
    assert contact.time_s == pytest.approx(TRIGGER_NS / 1e9 - 0.0056, abs=1e-9)
    assert contact.sigma_s == CONTACT_SIGMA_S["departure_fit"] == 0.5e-3
    assert contact.sound_bound_ms == pytest.approx(-4.373, abs=1e-3)


@pytest.mark.parametrize("late_ms", [0.05, 0.32, 1.09, 1.45])
def test_contact_time_refuses_a_departure_fit_after_the_sound_bound(late_ms):
    """The live window takes fits up to 1.5 ms after the bound (2026-10-09: fits at -4.05
    and -3.28 ms from the trigger, 0.32 and 1.09 ms after it at 1.5 m); impact cannot
    follow the sound it made."""
    departure_ms = SOUND_BOUND_MS + late_ms
    time_zero = live_time_zero(departure_ms)
    assert time_zero.source == "departure_fit"  # the ball flight still uses it

    contact = select_contact_time(time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert contact.source == "acoustic_calibrated"
    assert contact.reason == "departure_fit_after_sound_bound"
    assert contact.t0_ms == pytest.approx(ACAL_MS)
    assert contact.time_s == pytest.approx(TRIGGER_NS / 1e9 + ACAL_MS / 1e3, abs=1e-9)
    assert contact.sigma_s == CONTACT_SIGMA_S["acoustic"] == 2.0e-3
    assert contact.departure_fit_ms == pytest.approx(departure_ms)


def test_contact_time_takes_a_fit_exactly_at_the_sound_bound():
    time_zero = CameraTimeZero(
        source="departure_fit",
        time_zero_ms=SOUND_BOUND_MS,
        departure_fit_ms=SOUND_BOUND_MS,
        acoustic_ms=SOUND_BOUND_MS,
        tee_patch_ms=-3.0,
        iwr_impact_t_s=None,
    )

    contact = select_contact_time(time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert contact.source == "departure_fit"


def test_contact_time_keeps_the_live_window_for_fits_it_rejected():
    """A fit the live gate rejected (later than acoustic + 1.5 ms) stays rejected: no widening."""
    departure_ms = SOUND_BOUND_MS + 1e3 * DEPARTURE_MAX_LAG_S + 0.3
    time_zero = live_time_zero(departure_ms)
    assert time_zero.source == "acoustic"

    contact = select_contact_time(time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "departure_fit_outside_live_window",
    )
    assert contact.departure_fit_ms == pytest.approx(departure_ms)


@pytest.mark.parametrize(
    "time_zero,reason",
    [(live_time_zero(None), "no_departure_fit"), (None, "no_camera_time_zero")],
    ids=["no_fit", "no_time_zero"],
)
def test_contact_time_falls_back_to_the_calibrated_acoustic_estimate(time_zero, reason):
    contact = select_contact_time(time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert (contact.source, contact.reason) == ("acoustic_calibrated", reason)
    assert contact.t0_ms == pytest.approx(ACAL_MS) == pytest.approx(-5.573, abs=1e-3)
    assert contact.acoustic_ms == pytest.approx(ACAL_MS)
    assert (contact.acoustic_delay_ms, contact.acoustic_delay_source) == (1.20, "default")
    assert contact.acoustic_reference == "stamp"
    assert contact.departure_fit_ms is None


def test_contact_time_bound_follows_the_tee_range():
    """The bound is the geometry's tee range over the speed of sound, like the acoustic estimate."""
    departure_ms = -5.0
    near = select_contact_time(live_time_zero(departure_ms), trigger_ns=TRIGGER_NS, tee_range_m=1.5)
    far = select_contact_time(live_time_zero(departure_ms), trigger_ns=TRIGGER_NS, tee_range_m=1.8)

    assert near.source == "departure_fit"
    assert far.sound_bound_ms == pytest.approx(-1e3 * 1.8 / SPEED_OF_SOUND_M_S)
    assert (far.source, far.reason) == ("acoustic_calibrated", "departure_fit_after_sound_bound")


# The trigger stamp lags the sound edge (callback dispatch, the camera's exposure
# lock). With the edge's own stamp the bound at the edge is judged and logged; the
# bound at the trigger stamp, where the acoustic fallback lies, decides.


def test_contact_time_logs_a_fit_after_the_bound_at_the_sound_edge_and_keeps_it():
    """A fit 1 ms before the trigger-stamp bound with a 3 ms stamp lag is 2 ms after the edge
    bound. Refusing it would fall back to the stamp's acoustic estimate, 1 ms later still."""
    departure_ms = SOUND_BOUND_MS - 1.0
    time_zero = live_time_zero(departure_ms)
    assert time_zero.source == "departure_fit"

    contact = select_contact_time(
        time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, trigger_lag_ms=3.0
    )

    assert (contact.source, contact.reason) == ("departure_fit", "departure_fit")
    assert contact.t0_ms == pytest.approx(departure_ms)
    assert contact.sigma_s == CONTACT_SIGMA_S["departure_fit"]
    assert contact.edge_bound_verdict == "departure_fit_after_edge_sound_bound"
    assert contact.trigger_lag_ms == 3.0
    assert contact.edge_sound_bound_ms == pytest.approx(SOUND_BOUND_MS - 3.0)
    assert contact.departure_fit_ms == pytest.approx(departure_ms)


def test_contact_time_judges_a_fit_before_the_bound_at_the_sound_edge():
    contact = select_contact_time(
        live_time_zero(SOUND_BOUND_MS - 3.5),
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        trigger_lag_ms=3.0,
    )

    assert (contact.source, contact.reason) == ("departure_fit", "departure_fit")
    assert contact.t0_ms == pytest.approx(SOUND_BOUND_MS - 3.5)
    assert contact.edge_bound_verdict == "departure_fit_before_edge_sound_bound"


def test_a_fit_after_both_bounds_names_the_trigger_stamp_bound():
    """Unchanged reason for the fits refused before the edge stamp existed."""
    contact = select_contact_time(
        live_time_zero(SOUND_BOUND_MS + 0.3),
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        trigger_lag_ms=0.4,
    )

    assert contact.reason == "departure_fit_after_sound_bound"
    assert contact.edge_bound_verdict == "departure_fit_after_edge_sound_bound"


@pytest.mark.parametrize(
    "time_zero",
    [live_time_zero(None), live_time_zero(SOUND_BOUND_MS + 1e3 * DEPARTURE_MAX_LAG_S + 0.3), None],
    ids=["no_fit", "fit_outside_live_window", "no_time_zero"],
)
def test_without_a_fit_in_the_live_window_there_is_no_edge_verdict(time_zero):
    contact = select_contact_time(
        time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, trigger_lag_ms=3.0
    )

    assert contact.source == "acoustic_calibrated"
    assert contact.edge_sound_bound_ms == pytest.approx(SOUND_BOUND_MS - 3.0)
    assert contact.edge_bound_verdict is None


def test_without_an_edge_stamp_the_bound_stays_at_the_trigger_stamp():
    contact = select_contact_time(
        live_time_zero(SOUND_BOUND_MS - 1.0), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M
    )

    assert contact.source == "departure_fit"
    assert contact.trigger_lag_ms is None
    assert contact.edge_sound_bound_ms is None
    assert contact.edge_bound_verdict is None


@pytest.mark.parametrize("lag_ms", [None, 0.0, 0.4, 3.0, 8.0])
@pytest.mark.parametrize("offset_ms", [-5.0, -3.5, -1.0, -0.2, 0.0, 0.05, 0.32, 1.09, 1.45])
def test_a_refused_fit_is_never_replaced_by_a_later_contact(lag_ms, offset_ms):
    """Whatever the stamp lag: the contact is the fit, or no later than any fit it refused."""
    departure_ms = SOUND_BOUND_MS + offset_ms
    contact = select_contact_time(
        live_time_zero(departure_ms),
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        trigger_lag_ms=lag_ms,
    )

    if contact.source == "departure_fit":
        assert contact.t0_ms == pytest.approx(departure_ms)
    else:
        assert contact.t0_ms <= departure_ms


@pytest.mark.parametrize("lag_ms", [-0.1, float("nan"), float("inf")])
def test_contact_time_rejects_an_impossible_trigger_lag(lag_ms):
    with pytest.raises(ValueError, match="trigger_lag_ms"):
        select_contact_time(
            live_time_zero(-5.6),
            trigger_ns=TRIGGER_NS,
            tee_range_m=TEE_RANGE_M,
            trigger_lag_ms=lag_ms,
        )


# --- contact-time cascade: live fit, gate-free camera departure, calibrated acoustic ----


def test_a_live_departure_fit_wins_over_a_camera_departure():
    contact = select_contact_time(
        live_time_zero(-5.6),
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=ACAL_MS - 0.3,
    )

    assert (contact.source, contact.reason) == ("departure_fit", "departure_fit")
    assert contact.t0_ms == pytest.approx(-5.6)
    assert contact.sigma_s == 0.5e-3


def test_a_camera_departure_agreeing_with_the_acoustic_estimate_sets_the_contact():
    contact = select_contact_time(
        None,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=ACAL_MS - 0.8,
    )

    assert (contact.source, contact.reason) == ("camera_departure", "camera_departure")
    assert contact.t0_ms == pytest.approx(ACAL_MS - 0.8)
    assert contact.sigma_s == CONTACT_SIGMA_S["camera_departure"] == 0.5e-3
    assert contact.camera_departure_ms == pytest.approx(ACAL_MS - 0.8)
    assert contact.departure_agreement_ms == pytest.approx(-0.8)
    assert contact.acoustic_ms == pytest.approx(-5.573, abs=1e-3)


def test_a_camera_departure_in_conflict_widens_the_acoustic_sigma():
    contact = select_contact_time(
        None,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=ACAL_MS - 2.3,
    )

    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "camera_departure_conflict",
    )
    assert contact.t0_ms == pytest.approx(ACAL_MS)
    assert contact.sigma_s == pytest.approx(2.3e-3)


@pytest.mark.parametrize("teed, sigma_ms", [(True, 1.5), (False, 2.0)])
def test_a_camera_departure_after_the_sound_bound_gets_the_conflict_sigma(teed, sigma_ms):
    """0.3 ms after the bound lies 1.5 ms from the calibrated estimate: inside the agreement
    window, but impact cannot follow its sound."""
    contact = select_contact_time(
        None,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=SOUND_BOUND_MS + 0.3,
        teed=teed,
    )

    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "camera_departure_after_sound_bound",
    )
    assert contact.t0_ms == pytest.approx(ACAL_MS)
    assert contact.sigma_s == pytest.approx(sigma_ms * 1e-3)


@pytest.mark.parametrize("teed, sigma_s", [(True, 0.5e-3), (False, 2.0e-3)])
def test_without_any_departure_a_teed_ball_keeps_a_tight_acoustic_sigma(teed, sigma_s):
    contact = select_contact_time(None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, teed=teed)

    assert contact.source == "acoustic_calibrated"
    assert contact.t0_ms == pytest.approx(ACAL_MS)
    assert contact.sigma_s == sigma_s
    assert contact.camera_departure_ms is None
    assert contact.departure_agreement_ms is None


def test_a_session_delay_moves_the_acoustic_estimate():
    contact = select_contact_time(
        None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, acoustic_delay_ms=0.9
    )

    assert contact.t0_ms == pytest.approx(SOUND_BOUND_MS - 0.9)
    assert (contact.acoustic_delay_ms, contact.acoustic_delay_source) == (0.9, "session")


@pytest.mark.parametrize("lag_reference, logged", [("kernel", True), ("callback", False)])
def test_the_kernel_acoustic_estimate_is_logged_and_never_decides(lag_reference, logged):
    with_lag = select_contact_time(
        None,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        trigger_lag_ms=0.65,
        lag_reference=lag_reference,
    )
    without = select_contact_time(None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert with_lag.t0_ms == without.t0_ms == pytest.approx(ACAL_MS)
    if logged:
        assert with_lag.acoustic_kernel_ms == pytest.approx(-0.65 - 4.373 - 0.30, abs=1e-3)
    else:
        assert with_lag.acoustic_kernel_ms is None
    assert without.acoustic_kernel_ms is None


@pytest.mark.parametrize("delay", [float("nan"), float("inf")])
def test_contact_time_rejects_an_impossible_session_delay(delay):
    with pytest.raises(ValueError, match="acoustic_delay_ms"):
        select_contact_time(
            None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, acoustic_delay_ms=delay
        )


def test_the_calibrator_keeps_the_default_below_five_teed_samples():
    calibrator = AcousticDelayCalibrator()
    for delay in (1.0, 1.1, 1.3, 1.4):
        assert calibrator.add(SOUND_BOUND_MS, SOUND_BOUND_MS - delay, teed=True)

    assert calibrator.delay() == (ACOUSTIC_STAMP_DELAY_MS, "default", 4)
    assert calibrator.session_delay_ms() is None


def test_the_calibrator_takes_the_median_of_teed_samples_from_five_on():
    calibrator = AcousticDelayCalibrator()
    for delay in (0.9, 1.0, 1.1, 1.3, 1.4):
        calibrator.add(SOUND_BOUND_MS, SOUND_BOUND_MS - delay, teed=True)
    # Mat strikes, departures after the bound and far ones are no samples.
    assert not calibrator.add(SOUND_BOUND_MS, SOUND_BOUND_MS - 9.0, teed=False)
    assert not calibrator.add(SOUND_BOUND_MS, SOUND_BOUND_MS + 0.2, teed=True)
    assert not calibrator.add(SOUND_BOUND_MS, SOUND_BOUND_MS - 5.5, teed=True)
    assert not calibrator.add(SOUND_BOUND_MS, None, teed=True)

    delay_ms, source, count = calibrator.delay()
    assert (source, count) == ("session", 5)
    assert delay_ms == pytest.approx(1.1)
    assert calibrator.session_delay_ms() == pytest.approx(1.1)


def test_the_calibrator_learns_from_a_contacts_departure():
    calibrator = AcousticDelayCalibrator(min_samples=1)
    live = select_contact_time(live_time_zero(-5.6), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)
    camera = select_contact_time(
        None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, camera_departure_ms=ACAL_MS - 2.3
    )
    acoustic = select_contact_time(None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M)

    assert calibrator.add_contact(live, teed=True)
    assert calibrator.add_contact(camera, teed=True)  # a conflicting departure still counts
    assert not calibrator.add_contact(acoustic, teed=True)
    assert not calibrator.add_contact(None, teed=True)
    assert calibrator.delay()[2] == 2


# --- shadow contact time (study 60-kontaktzeit, proposal 2): logged, never decides -------


def _late_live_contact(after_bound_ms: float = 0.8, **kwargs):
    """A mat shot whose live fit lies ``after_bound_ms`` after the stamp sound bound."""
    time_zero = live_time_zero(SOUND_BOUND_MS + after_bound_ms)
    assert time_zero.source == "departure_fit"
    contact = select_contact_time(
        time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, **kwargs
    )
    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "departure_fit_after_sound_bound",
    )
    return contact, time_zero


def _late_camera_contact(after_bound_ms: float = 0.8, **kwargs):
    """A mat shot without a live fit whose camera departure lies after the sound bound."""
    contact = select_contact_time(
        None,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=SOUND_BOUND_MS + after_bound_ms,
        **kwargs,
    )
    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "camera_departure_after_sound_bound",
    )
    return contact


def test_the_shadow_takes_a_live_fit_after_the_sound_bound():
    contact, time_zero = _late_live_contact(0.8)

    shadow = shadow_contact_time(contact, time_zero, teed=False)

    assert shadow == ContactShadow(
        "accepted",
        "live_departure_fit",
        t0_ms=pytest.approx(SOUND_BOUND_MS + 0.8),
        sigma_s=0.5e-3,
        minus_t0_ms=pytest.approx(0.8 + ACOUSTIC_STAMP_DELAY_MS),
    )
    assert shadow.to_log() == {
        "t0_shadow_ms": shadow.t0_ms,
        "t0_shadow_source": "live_departure_fit",
        "t0_shadow_reason": "accepted",
        "t0_shadow_minus_t0_ms": shadow.minus_t0_ms,
    }
    # The contact itself is the acoustic estimate, as before.
    assert (contact.t0_ms, contact.sigma_s) == (pytest.approx(ACAL_MS), 2e-3)


def test_the_shadow_takes_a_camera_departure_after_the_sound_bound():
    contact = _late_camera_contact(1.1)

    shadow = shadow_contact_time(contact, None, teed=False)

    assert (shadow.reason, shadow.source) == ("accepted", "camera_departure")
    assert shadow.t0_ms == pytest.approx(SOUND_BOUND_MS + 1.1)
    assert shadow.minus_t0_ms == pytest.approx(1.1 + ACOUSTIC_STAMP_DELAY_MS)
    assert shadow.sigma_s == CONTACT_SIGMA_S["departure_fit"]


def test_a_teed_ball_gets_no_shadow():
    contact, time_zero = _late_live_contact(0.8, teed=True)

    assert shadow_contact_time(contact, time_zero, teed=True) == ContactShadow("teed")
    assert shadow_contact_time(_late_camera_contact(teed=True), None, teed=True) == ContactShadow(
        "teed"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time_zero": "live"},
        {"time_zero": None, "camera_departure_ms": ACAL_MS - 0.4},
    ],
    ids=["departure_fit", "camera_departure"],
)
def test_a_contact_from_a_departure_gets_no_shadow(kwargs):
    time_zero = live_time_zero(-5.6) if kwargs.pop("time_zero") == "live" else None
    contact = select_contact_time(
        time_zero, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, **kwargs
    )
    assert contact.source != "acoustic_calibrated"

    assert shadow_contact_time(contact, time_zero, teed=False) == ContactShadow("not_acoustic")


@pytest.mark.parametrize(
    "time_zero,camera_departure_ms,reason",
    [
        (None, None, "no_camera_time_zero"),
        (live_time_zero(None), None, "no_departure_fit"),
        # Later than the live window: the live fit is not the one the window took.
        (live_time_zero(SOUND_BOUND_MS + 1e3 * DEPARTURE_MAX_LAG_S + 0.3), None, None),
        (None, ACAL_MS - 2.3, "camera_departure_conflict"),
    ],
    ids=["no_time_zero", "no_fit", "outside_live_window", "conflict"],
)
def test_other_acoustic_reasons_have_no_shadow_candidate(time_zero, camera_departure_ms, reason):
    contact = select_contact_time(
        time_zero,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=camera_departure_ms,
    )
    assert contact.source == "acoustic_calibrated"
    assert contact.reason == (reason or "departure_fit_outside_live_window")

    assert shadow_contact_time(contact, time_zero, teed=False) == ContactShadow("no_candidate")


def test_a_live_fit_reason_without_its_time_zero_has_no_candidate():
    contact, _time_zero = _late_live_contact(0.8)

    assert shadow_contact_time(contact, None, teed=False) == ContactShadow("no_candidate")


@pytest.mark.parametrize("branch", ["live", "camera"])
def test_the_shadow_accepts_a_candidate_on_the_acoustic_estimate(branch):
    """The guard is acoustic <= candidate: a session delay of -1 ms puts the acoustic
    estimate 1 ms after the sound bound, onto the candidate."""
    if branch == "live":
        contact, time_zero = _late_live_contact(1.0, acoustic_delay_ms=-1.0)
    else:
        contact, time_zero = _late_camera_contact(1.0, acoustic_delay_ms=-1.0), None
    assert contact.acoustic_ms == pytest.approx(SOUND_BOUND_MS + 1.0)
    candidate = time_zero.time_zero_ms if time_zero is not None else contact.camera_departure_ms
    contact = replace(contact, acoustic_ms=candidate, t0_ms=candidate)

    shadow = shadow_contact_time(contact, time_zero, teed=False)

    assert (shadow.reason, shadow.t0_ms, shadow.minus_t0_ms) == ("accepted", candidate, 0.0)


@pytest.mark.parametrize("branch", ["live", "camera"])
def test_the_shadow_refuses_a_candidate_before_the_acoustic_estimate(branch):
    """One-sided (erratum E4): a mat strike only makes the sound edge early."""
    if branch == "live":
        contact, time_zero = _late_live_contact(0.5, acoustic_delay_ms=-1.0)
        source = "live_departure_fit"
    else:
        contact, time_zero = _late_camera_contact(0.5, acoustic_delay_ms=-1.0), None
        source = "camera_departure"

    shadow = shadow_contact_time(contact, time_zero, teed=False)

    assert shadow == ContactShadow("before_acoustic", source)


def test_the_shadow_refuses_a_live_fit_on_the_trigger_stamp():
    """0.3 m to the tee: the live window reaches the stamp, a fit on it is no contact."""
    trigger_s = TRIGGER_NS / 1e9
    sound_bound_ms = -1e3 * 0.3 / SPEED_OF_SOUND_M_S
    fit = DepartureFit(time_s=trigger_s, n_points=6, rms_px=0.2, span_frames=2.0)
    zero_s, source = _select_time_zero(
        fit, trigger_s=trigger_s, acoustic_s=trigger_s + sound_bound_ms / 1e3
    )
    assert source == "departure_fit"
    time_zero = CameraTimeZero(
        source=source,
        time_zero_ms=1e3 * (zero_s - trigger_s),
        departure_fit_ms=0.0,
        acoustic_ms=sound_bound_ms,
        tee_patch_ms=None,
        iwr_impact_t_s=None,
    )
    contact = select_contact_time(time_zero, trigger_ns=TRIGGER_NS, tee_range_m=0.3)
    assert contact.reason == "departure_fit_after_sound_bound"

    shadow = shadow_contact_time(contact, time_zero, teed=False)

    assert shadow == ContactShadow("not_before_stamp", "live_departure_fit")
    # Just before the stamp it is taken.
    earlier = replace(time_zero, time_zero_ms=-0.01)
    assert shadow_contact_time(contact, earlier, teed=False).reason == "accepted"


@pytest.mark.parametrize("departure_ms", [0.0, 0.4])
def test_the_shadow_refuses_a_camera_departure_on_or_after_the_trigger_stamp(departure_ms):
    contact = select_contact_time(
        None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M, camera_departure_ms=departure_ms
    )
    assert contact.reason == "camera_departure_after_sound_bound"

    shadow = shadow_contact_time(contact, None, teed=False)

    assert shadow == ContactShadow("not_before_stamp", "camera_departure")


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize("field_name", ["time_zero_ms", "camera_departure_ms", "acoustic_ms"])
def test_a_non_finite_candidate_or_acoustic_estimate_has_no_shadow(field_name, value):
    if field_name == "camera_departure_ms":
        contact, time_zero = _late_camera_contact(0.8), None
        contact = replace(contact, camera_departure_ms=value)
        source = "camera_departure"
    else:
        contact, time_zero = _late_live_contact(0.8)
        source = "live_departure_fit"
        if field_name == "time_zero_ms":
            time_zero = replace(time_zero, time_zero_ms=value)
        else:
            contact = replace(contact, acoustic_ms=value)

    assert shadow_contact_time(contact, time_zero, teed=False) == ContactShadow(
        "non_finite", source
    )


# --- tee control of the shadow (erratum E3) ---------------------------------------------


def test_the_tee_control_counts_teed_departures_late_after_their_sound_bound():
    control = TeeControl()
    on_time, _tz = _late_live_contact(0.29)  # within 0.3 ms after the bound: not late
    late, _tz = _late_live_contact(0.31)
    camera_late = _late_camera_contact(0.9)
    before = select_contact_time(
        live_time_zero(-5.6), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M
    )

    assert control.add_contact(on_time, teed=True)
    assert control.add_contact(late, teed=True)
    assert control.add_contact(camera_late, teed=True)  # no live fit: the camera departure
    assert control.add_contact(before, teed=True)
    # Mat shots, contacts without any departure, nothing: no count.
    assert not control.add_contact(late, teed=False)
    assert not control.add_contact(
        select_contact_time(None, trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M), teed=True
    )
    assert not control.add_contact(None, teed=True)
    assert not control.add_contact(replace(before, departure_fit_ms=float("nan")), teed=True)

    assert control.state() == (4, 2, False)


@pytest.mark.parametrize("fit_ms", [float("nan"), float("inf"), float("-inf")])
def test_the_tee_control_falls_back_to_the_camera_departure_for_a_non_finite_live_fit(fit_ms):
    control = TeeControl()
    contact = replace(_late_camera_contact(0.9), departure_fit_ms=fit_ms)

    assert control.add_contact(contact, teed=True)

    assert control.state() == (1, 1, False)
    # Without a camera departure there is nothing to count.
    assert not control.add_contact(replace(contact, camera_departure_ms=None), teed=True)
    assert control.state() == (1, 1, False)


def test_the_tee_control_takes_the_live_fit_before_the_camera_departure():
    """The live fit counts even outside the live window; the camera departure only without it."""
    control = TeeControl()
    outside = live_time_zero(SOUND_BOUND_MS + 1e3 * DEPARTURE_MAX_LAG_S + 0.3)
    contact = select_contact_time(
        outside,
        trigger_ns=TRIGGER_NS,
        tee_range_m=TEE_RANGE_M,
        camera_departure_ms=SOUND_BOUND_MS - 1.0,
        teed=True,
    )
    assert contact.source == "camera_departure"

    control.add_contact(contact, teed=True)

    assert (control.n_teed, control.n_late) == (1, 1)


@pytest.mark.parametrize(
    "n_on_time,n_late,passes",
    [
        (4, 0, False),
        (5, 0, True),
        (9, 1, True),
        (8, 1, False),
        (16, 2, False),
        (18, 2, True),
        (0, 0, False),
    ],
)
def test_the_tee_control_passes_from_five_teed_with_at_most_ten_percent_late(
    n_on_time, n_late, passes
):
    control = TeeControl()
    on_time = select_contact_time(
        live_time_zero(-5.6), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M
    )
    late, _tz = _late_live_contact(0.5)
    for _ in range(n_on_time):
        control.add_contact(on_time, teed=True)
    for _ in range(n_late):
        control.add_contact(late, teed=True)

    assert control.state() == (n_on_time + n_late, n_late, passes)
    assert control.passes() is passes


def test_the_calibrator_feeds_its_tee_control_without_changing_its_delay():
    calibrator = AcousticDelayCalibrator(min_samples=1)
    late, _tz = _late_live_contact(0.8)
    on_time = select_contact_time(
        live_time_zero(-5.6), trigger_ns=TRIGGER_NS, tee_range_m=TEE_RANGE_M
    )

    # A late teed fit is no delay sample but counts for the tee control.
    assert not calibrator.add_contact(late, teed=True)
    assert calibrator.add_contact(on_time, teed=True)
    assert not calibrator.add_contact(on_time, teed=False)

    assert calibrator.delay()[1:] == ("session", 1)
    assert calibrator.tee_control.state() == (2, 1, False)
    assert AcousticDelayCalibrator().tee_control.state() == (0, 0, False)


# --- gate-free camera departure as a contact time --------------------------------------


def _departure_trigger_ns(contact_s: float, stamp_lag_s: float = 0.0012) -> int:
    return int(round((contact_s + TEE_RANGE_M / SPEED_OF_SOUND_M_S + stamp_lag_s) * 1e9))


def test_the_camera_departure_finds_contact_behind_a_head_and_a_tee():
    frames, timestamps, contact_s, ball = render_departure_clip()

    fit = estimate_departure_contact(
        frames,
        timestamps,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        ball=ball,
    )

    assert fit is not None
    assert fit.time_s == pytest.approx(contact_s, abs=0.25e-3)


def test_the_camera_departure_runs_below_the_live_contrast_gate():
    frames, timestamps, contact_s, ball = render_departure_clip(ball_value=108.0, seed=11)

    fit = estimate_departure_contact(
        frames,
        timestamps,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        ball=ball,
    )

    assert fit is not None
    assert fit.time_s == pytest.approx(contact_s, abs=0.25e-3)


def test_the_camera_departure_needs_four_points():
    frames, timestamps, contact_s, ball = render_departure_clip()
    end = 44  # the flight window keeps 4 frames, the first ones still on the ball

    fit = estimate_departure_contact(
        frames[:end],
        timestamps[:end],
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        ball=ball,
    )

    assert fit is None


# --- teed-ball refinement ---------------------------------------------------------------


def _teed_ball_frames(centre, *, decoy_dx=None, stem=True, rim=True, flat=False, seed=1):
    """Pre-swing frames of a dark ball with a lit top rim on a 6 px dark tee stem."""
    rng = np.random.default_rng(seed)
    diameter = expected_ball_diameter_px(GEOMETRY, 16.4)
    image = np.full((HEIGHT, WIDTH), 110.0) + rng.normal(0.0, 3.0, (HEIGHT, WIDTH))
    cx, cy = centre
    if not flat:
        if stem:
            _paint_polygon(
                image,
                np.array(
                    [[cx - 3, cy], [cx + 3, cy], [cx + 3, cy + diameter], [cx - 3, cy + diameter]]
                ),
                40.0,
            )
        _paint_disk(image, np.array(centre), diameter / 2.0, 40.0)
        if rim:

            def lit(xs, ys):
                radius = np.hypot(xs - cx, ys - cy)
                return (
                    (radius <= diameter / 2.0)
                    & (radius >= diameter / 2.0 - 1.0)
                    & (ys < cy - 0.75 * diameter / 2.0)
                )

            _paint(image, lit, (cx - diameter, cy - diameter, cx + diameter, cy), 150.0)
        if decoy_dx is not None:
            _paint_disk(image, np.array([cx + decoy_dx, cy]), diameter / 2.0, 10.0)
    frames = [
        np.clip(np.round(image + rng.normal(0.0, 1.5, image.shape)), 0, 255).astype(np.uint8)
        for _ in range(20)
    ]
    return np.stack(frames), diameter


@pytest.mark.parametrize("centre", [(300.3, 250.6), (321.7, 260.2)])
def test_refine_finds_a_teed_ball_with_a_lit_rim_on_its_tee(centre):
    frames, diameter = _teed_ball_frames(centre)

    refined = refine_teed_ball(frames, ReferenceBall(*centre, 16.4, 211), GEOMETRY)

    assert refined is not None
    assert math.hypot(refined.x - centre[0], refined.y - centre[1]) <= 0.5
    assert refined.diameter_px == diameter


def test_refine_corrects_an_anchor_merged_with_the_tee():
    """A ball+tee blob centres about 2 px low and reads 26 px tall (2026-10-25 drivers)."""
    centre = (300.3, 250.6)
    frames, diameter = _teed_ball_frames(centre)

    refined = refine_teed_ball(frames, ReferenceBall(300.3, 252.6, 26.0, 300), GEOMETRY)

    assert math.hypot(refined.x - centre[0], refined.y - centre[1]) <= 0.5
    assert refined.diameter_px == diameter


def test_refine_ignores_a_stronger_decoy_beyond_its_search():
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre, decoy_dx=70.0)

    refined = refine_teed_ball(frames, ReferenceBall(300.3, 252.6, 16.4, 211), GEOMETRY)

    assert math.hypot(refined.x - centre[0], refined.y - centre[1]) <= 0.5


def test_refine_finds_nothing_on_a_flat_image():
    frames, _diameter = _teed_ball_frames((300.3, 250.6), flat=True)

    assert refine_teed_ball(frames, ReferenceBall(300.3, 250.6, 16.4, 211), GEOMETRY) is None


def test_refine_returns_none_for_unusable_frames():
    ball = ReferenceBall(2.0, 2.0, 16.4, 211)

    assert refine_teed_ball(np.zeros((0, 4, 4), np.uint8), ball, GEOMETRY) is None
    assert refine_teed_ball(np.zeros((4, 4), np.uint8), ball, GEOMETRY) is None


def test_resolve_club_ball_refines_the_given_or_the_tracked_ball():
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    given = ReferenceBall(300.3, 252.6, 26.0, 300)
    tracker = ReferenceBallTracker()
    for _ in range(3):
        tracker.resolve_stable(given)

    refined, source = resolve_club_ball(frames, GEOMETRY, reference_ball=given)
    tracked, tracked_source = resolve_club_ball(frames, GEOMETRY, ball_tracker=tracker)
    flat, _ = _teed_ball_frames(centre, flat=True)
    unrefined = resolve_club_ball(flat, GEOMETRY, reference_ball=given)

    assert source == tracked_source == "refined"
    assert math.hypot(refined.x - centre[0], refined.y - centre[1]) <= 0.5
    assert (tracked.x, tracked.y) == pytest.approx((refined.x, refined.y))
    # The given ball is a tapped mark: without a ball around it there is none.
    assert unrefined == (None, None)
    assert resolve_club_ball(flat, GEOMETRY) == (None, None)


def test_resolve_club_ball_refines_the_departed_ball():
    """The disappeared-ball locator's circle sits high and small on a tee (10-25 drivers)."""
    centre = (300.3, 250.6)
    frames, diameter = _teed_ball_frames(centre)
    departed = ReferenceBall(300.9, 248.6, 13.2, 137)
    flat, _ = _teed_ball_frames(centre, flat=True)

    refined, source = resolve_club_ball(
        frames,
        GEOMETRY,
        reference_ball=ReferenceBall(330.0, 262.6, 26.0, 300),
        departed_ball=departed,
    )

    assert source == "refined"
    assert math.hypot(refined.x - centre[0], refined.y - centre[1]) <= 0.5
    assert refined.diameter_px == diameter
    assert resolve_club_ball(flat, GEOMETRY, departed_ball=departed) == (departed, "departed")


def _zone(x0, y0, x1, y1):
    """A hitting zone [x0, x1) x [y0, y1) of the test frame."""
    return TeeZone(x0, y0, x1, y1, (x0 + x1) / 2.0, (y0 + y1) / 2.0, 16.4, WIDTH, HEIGHT)


def test_the_mark_is_a_resting_ball_of_the_lens_size():
    diameter = expected_ball_diameter_px(GEOMETRY, math.nan)

    mark = mark_reference_ball((310.0, 262.0), GEOMETRY)

    assert mark == ReferenceBall(310.0, 262.0, diameter, round(math.pi * (diameter / 2.0) ** 2))
    assert mark_reference_ball(None, GEOMETRY) is None
    assert mark_reference_ball((310.0, 262.0), replace(GEOMETRY, intrinsics=None)) is None
    assert mark_reference_ball((math.nan, 262.0), GEOMETRY) is None


def test_a_departed_ball_wins_over_the_mark_and_is_never_checked():
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    departed = ReferenceBall(300.9, 248.6, 13.2, 137)

    found = locate_club_ball(
        frames,
        GEOMETRY,
        reference_ball=mark_reference_ball((420.0, 330.0), GEOMETRY),
        departed_ball=departed,
        tee_zone=_zone(400, 300, 460, 360),
    )

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "departed",
        True,
        (),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5


def test_a_tracker_ball_far_from_the_mark_gives_way_to_the_mark():
    """2026-11-02 S1 irons: the tracker sat on the tree line; the tap ~14 px off the ball."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    flat, _ = _teed_ball_frames(centre, flat=True)
    mark = mark_reference_ball((310.0, 261.0), GEOMETRY)
    tracker = ReferenceBallTracker()
    for _ in range(3):
        tracker.resolve_stable(ReferenceBall(300.0, 120.0, 16.4, 211))  # a tree-line blob
    zone = _zone(200, 100, 450, 350)  # the blob lies inside: the mark alone rules it out

    found = locate_club_ball(
        frames, GEOMETRY, reference_ball=mark, ball_tracker=tracker, tee_zone=zone
    )
    unrefined = locate_club_ball(flat, GEOMETRY, reference_ball=mark, tee_zone=zone)

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "mark",
        True,
        ("tracker_anchor_far_from_mark",),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5
    assert found.to_log() == {
        "ball_anchor": "mark",
        "ball_refined": True,
        "ball_rejected": ["tracker_anchor_far_from_mark"],
    }
    # The bare tap is not this clip's ball.
    assert unrefined == ClubBall(None, None, "mark", rejected=("mark_refined_not_found",))


def test_a_teed_ball_above_the_mark_stays_the_trackers():
    """2026-11-02 S1 drivers: teed 16-30 px above the irons' tap, beyond the refine around it."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((centre[0], centre[1] + 34.0), GEOMETRY)
    # Around the tap the refine latches onto the tee stem, not the ball.
    off = refine_teed_ball(frames, mark, GEOMETRY)
    assert off is not None and math.hypot(off.x - centre[0], off.y - centre[1]) > 10.0

    found = locate_club_ball(
        frames, GEOMETRY, reference_ball=mark, tee_zone=_zone(200, 200, 450, 350)
    )

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "tracker",
        True,
        (),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5


def test_a_teed_shot_never_anchors_on_the_mark():
    """Without a plausible tracker ball a teed ball has no ball, not the tee stem by the tap."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((centre[0], centre[1] + 34.0), GEOMETRY)
    blob = ReferenceBall(300.0, 120.0, 16.4, 211)  # a tree-line blob, far from the mark
    tracker = SimpleNamespace(resolve=lambda _detected: (blob, "anchor"))
    zone = _zone(200, 100, 450, 350)

    def locate(teed: bool) -> ClubBall:
        return locate_club_ball(
            frames, GEOMETRY, reference_ball=mark, ball_tracker=tracker, tee_zone=zone, teed=teed
        )

    found = locate(True)
    # Not teed, the mark's refine latches onto the tee stem below the ball.
    unteed = locate(False)

    assert found == ClubBall(
        None,
        None,
        "mark",
        rejected=("tracker_anchor_far_from_mark", "mark_anchor_club_may_be_teed"),
    )
    assert found.to_log() == {
        "ball_anchor": "mark",
        "ball_refined": False,
        "ball_rejected": ["tracker_anchor_far_from_mark", "mark_anchor_club_may_be_teed"],
    }
    assert (unteed.source, unteed.anchor) == ("refined", "mark")
    assert math.hypot(unteed.ball.x - centre[0], unteed.ball.y - centre[1]) > 10.0
    assert resolve_club_ball(
        frames, GEOMETRY, reference_ball=mark, ball_tracker=tracker, tee_zone=zone, teed=True
    ) == (None, None)
    # Without a mark a teed shot keeps the tracker's checks alone.
    assert locate_club_ball(frames, GEOMETRY, ball_tracker=tracker, teed=True).anchor == "tracker"


def test_a_teed_shot_keeps_a_plausible_tracker_ball_beside_the_mark():
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((centre[0], centre[1] + 34.0), GEOMETRY)

    found = locate_club_ball(
        frames, GEOMETRY, reference_ball=mark, tee_zone=_zone(200, 200, 450, 350), teed=True
    )

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "tracker",
        True,
        (),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5


IRONS_AND_WEDGES = tuple(
    club for club in ClubType if club not in WOODS_SEGMENTATION and club is not ClubType.UNKNOWN
)


def _tree_line_tracker() -> SimpleNamespace:
    """A tracker whose ball is a tree-line blob, far from the irons' tap (2026-11-02 S1)."""
    blob = ReferenceBall(300.0, 120.0, 16.4, 211)
    return SimpleNamespace(resolve=lambda _detected: (blob, "anchor"))


def test_the_clubs_that_may_be_teed_are_the_woods_and_hybrids():
    assert WOODS_SEGMENTATION == {
        ClubType.DRIVER,
        ClubType.WOOD_3,
        ClubType.WOOD_5,
        ClubType.WOOD_7,
        ClubType.HYBRID_3,
        ClubType.HYBRID_5,
        ClubType.HYBRID_7,
        ClubType.HYBRID_9,
    }
    assert len(IRONS_AND_WEDGES) == 12


@pytest.mark.parametrize("club", sorted(WOODS_SEGMENTATION, key=lambda club: club.value))
def test_a_club_that_may_be_teed_never_anchors_on_the_mark(club, monkeypatch):
    """A teed fairway wood or hybrid is not ``teed`` for the server, yet sits above a ground tap."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((centre[0], centre[1] + 34.0), GEOMETRY)
    zone = _zone(200, 100, 450, 350)
    monkeypatch.setattr(
        club_head_path_module,
        "refine_teed_ball",
        lambda *_a, **_k: pytest.fail("a club that may be teed never refines around the mark"),
    )

    found = locate_club_ball(
        frames,
        GEOMETRY,
        reference_ball=mark,
        ball_tracker=_tree_line_tracker(),
        tee_zone=zone,
        teed=False,
        club=club,
    )

    # The mark still bounds the tracker's ball: the tree-line blob is dropped.
    assert found == ClubBall(
        None,
        None,
        "mark",
        rejected=("tracker_anchor_far_from_mark", "mark_anchor_club_may_be_teed"),
    )
    assert found.to_log() == {
        "ball_anchor": "mark",
        "ball_refined": False,
        "ball_rejected": ["tracker_anchor_far_from_mark", "mark_anchor_club_may_be_teed"],
    }
    assert resolve_club_ball(
        frames,
        GEOMETRY,
        reference_ball=mark,
        ball_tracker=_tree_line_tracker(),
        tee_zone=zone,
        club=club,
    ) == (None, None)
    # Without a tracker ball the mark is refused alone.
    assert locate_club_ball(
        np.zeros((0, 4, 4), dtype=np.uint8), GEOMETRY, reference_ball=mark, club=club
    ) == ClubBall(None, None, "mark", rejected=("mark_anchor_club_may_be_teed",))


@pytest.mark.parametrize("club", [ClubType.WOOD_3, ClubType.HYBRID_5])
def test_a_club_that_may_be_teed_keeps_a_plausible_tracker_ball_beside_the_mark(club):
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((centre[0], centre[1] + 34.0), GEOMETRY)

    found = locate_club_ball(
        frames, GEOMETRY, reference_ball=mark, tee_zone=_zone(200, 200, 450, 350), club=club
    )

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "tracker",
        True,
        (),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5


@pytest.mark.parametrize("club", IRONS_AND_WEDGES)
def test_irons_and_wedges_keep_the_mark_anchor(club):
    """2026-11-02 S1 #6/#7/#9: without a plausible tracker ball the irons' ball is the mark's."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    mark = mark_reference_ball((310.0, 261.0), GEOMETRY)

    found = locate_club_ball(
        frames,
        GEOMETRY,
        reference_ball=mark,
        ball_tracker=_tree_line_tracker(),
        tee_zone=_zone(200, 100, 450, 350),
        club=club,
    )

    assert (found.source, found.anchor, found.refined, found.rejected) == (
        "refined",
        "mark",
        True,
        ("tracker_anchor_far_from_mark",),
    )
    assert math.hypot(found.ball.x - centre[0], found.ball.y - centre[1]) <= 0.5


def test_a_tracker_ball_outside_the_hitting_zone_is_no_ball():
    """2026-11-02 S1 #9: the tracker's ball sat on the tree line, above the zone."""
    frames, _diameter = _teed_ball_frames((300.3, 250.6))
    zone = _zone(200, 280, 450, 380)

    found = locate_club_ball(frames, GEOMETRY, tee_zone=zone)

    assert found == ClubBall(
        None, None, "tracker", rejected=("tracker_anchor_outside_hitting_zone",)
    )
    assert found.to_log() == {
        "ball_anchor": "tracker",
        "ball_refined": False,
        "ball_rejected": ["tracker_anchor_outside_hitting_zone"],
    }
    assert resolve_club_ball(frames, GEOMETRY, tee_zone=zone) == (None, None)
    # Inside the zone the same ball stands.
    assert locate_club_ball(frames, GEOMETRY, tee_zone=_zone(200, 200, 450, 350)).source == (
        "refined"
    )


def test_a_refined_ball_outside_the_hitting_zone_is_no_ball():
    frames, _diameter = _teed_ball_frames((300.3, 250.6))
    mark = mark_reference_ball((300.3, 262.0), GEOMETRY)

    found = locate_club_ball(
        frames, GEOMETRY, reference_ball=mark, tee_zone=_zone(200, 258, 450, 350)
    )

    # The tracker's ball lies above the zone, the mark's refine finds the same ball.
    assert found == ClubBall(
        None,
        None,
        "mark",
        refined=True,
        rejected=("tracker_anchor_outside_hitting_zone", "mark_refined_outside_hitting_zone"),
    )


def test_a_refined_tracker_ball_far_from_the_mark_is_no_ball():
    """The tracker's blob lies inside the bound, but its refine moves past it."""
    centre = (300.3, 250.6)
    frames, _diameter = _teed_ball_frames(centre)
    bound = CLUB_BALL_MARK_MAX_SEARCH_RADII * refine_search_radius_px(
        mark_reference_ball(centre, GEOMETRY).diameter_px
    )
    assert bound == pytest.approx(50.0)  # two search radii at the range's ball
    # 45 px below the tracker's blob, 60 px below the ball; blank around the tap.
    mark = mark_reference_ball((centre[0], centre[1] + 60.0), GEOMETRY)
    blob = ReferenceBall(centre[0], centre[1] + 15.0, 16.4, 211)
    tracker = SimpleNamespace(resolve=lambda _detected: (blob, "anchor"))

    found = locate_club_ball(frames, GEOMETRY, reference_ball=mark, ball_tracker=tracker)

    assert found == ClubBall(
        None,
        None,
        "mark",
        rejected=("tracker_refined_far_from_mark", "mark_refined_not_found"),
    )
    assert found.to_log()["ball_rejected"] == [
        "tracker_refined_far_from_mark",
        "mark_refined_not_found",
    ]


def test_without_a_mark_or_a_plausible_ball_there_is_no_ball():
    flat, _ = _teed_ball_frames((300.3, 250.6), flat=True)

    found = locate_club_ball(flat, GEOMETRY, tee_zone=_zone(200, 200, 450, 350))

    assert found == ClubBall(None, None)
    assert found.to_log() == {"ball_anchor": None, "ball_refined": False, "ball_rejected": None}


# --- woods segmentation -----------------------------------------------------------------


@pytest.fixture(name="two_tone_clip", scope="module")
def fixture_two_tone_clip():
    """A wood's head (crown +40, face -35, a band at the background between) on a -2 deg path."""
    return render_clip(Scenario(two_tone=True, path_deg=-2.0))


def test_one_polarity_cuts_a_two_tone_head_apart(two_tone_clip):
    result = estimate(*two_tone_clip, segmentation="polarity")

    assert result.status in ("rejected_short_track", "rejected_head_quality")
    assert result.segmentation == "polarity"


def test_woods_mode_tracks_a_two_tone_head(two_tone_clip):
    result = estimate(*two_tone_clip, segmentation="woods")

    assert result.status == "accepted"
    assert (result.segmentation, result.polarity) == ("woods", "abs")
    assert result.n_frames >= 4
    assert all(item.quality.ok for item in result.observations)
    assert result.path_deg == pytest.approx(-2.0, abs=0.3)
    assert set(result.segmentation_paths_deg) == {"0.25", "0.30", "0.35"}
    assert result.segmentation_sigma_deg == pytest.approx(
        0.5
        * (
            max(result.segmentation_paths_deg.values())
            - min(result.segmentation_paths_deg.values())
        ),
        abs=0.01,
    )


def test_difference_quality_rates_a_two_tone_head_on_its_change():
    """Half the head +40, half -40 around the background: its raw median equals the ring's."""
    rng = np.random.default_rng(2)
    background = np.full((60, 60), 110.0) + rng.normal(0.0, 1.0, (60, 60))
    frame = background.copy()
    mask = np.zeros((60, 60), bool)
    mask[20:40, 15:45] = True
    frame[20:30, 15:45] += 40.0
    frame[30:40, 15:45] -= 40.0
    signed = frame - background

    raw = assess_head_quality(frame, mask)
    on_difference = _difference_quality(frame, signed, mask, HeadQualityLimits())

    assert raw.status == "low_contrast"
    assert on_difference.status == "ok"
    assert on_difference.contrast >= 35.0


def test_difference_quality_reads_clipping_from_the_raw_frame():
    background = np.full((60, 60), 215.0)
    frame = background.copy()
    mask = np.zeros((60, 60), bool)
    mask[20:40, 15:45] = True
    frame[20:40, 15:45] = 255.0  # +40 over the background, but saturated

    quality = _difference_quality(frame, frame - background, mask, HeadQualityLimits())

    assert quality.status == "clipped"
    assert quality.clipped_pct == 100.0


def _changed_head(change: float):
    """A sharp head ``change`` grey levels over a flat background: frame, signed difference, mask."""
    background = np.full((60, 60), 110.0)
    frame = background.copy()
    mask = np.zeros((60, 60), bool)
    mask[20:40, 15:45] = True
    frame[mask] += change
    return frame, frame - background, mask


def test_the_wood_contrast_limit_sits_below_the_polarity_limit():
    assert (MIN_WOOD_HEAD_CONTRAST, MIN_HEAD_CONTRAST) == (12.0, 20.0)


@pytest.mark.parametrize(("change", "status"), [(11.0, "low_contrast"), (12.0, "ok"), (14.0, "ok")])
def test_a_wood_rated_on_its_change_needs_12_grey_levels(change, status):
    frame, signed, mask = _changed_head(change)

    quality = _difference_quality(frame, signed, mask, WOOD_DIFFERENCE_LIMITS)

    assert quality.contrast == change
    assert quality.status == status


def test_a_14_grey_level_head_stays_low_contrast_outside_the_wood_rating():
    frame, signed, mask = _changed_head(14.0)

    # The polarity rating of an iron, and the difference rating at a hybrid's limits.
    assert assess_head_quality(frame, mask).status == "low_contrast"
    assert _difference_quality(frame, signed, mask, HeadQualityLimits()).status == "low_contrast"


@pytest.mark.parametrize(
    ("club", "min_contrast"),
    [
        (ClubType.DRIVER, MIN_WOOD_HEAD_CONTRAST),
        (ClubType.WOOD_3, MIN_WOOD_HEAD_CONTRAST),
        (ClubType.WOOD_7, MIN_WOOD_HEAD_CONTRAST),
        (ClubType.HYBRID_5, MIN_HEAD_CONTRAST),
        (ClubType.IRON_7, MIN_HEAD_CONTRAST),
        (None, MIN_HEAD_CONTRAST),
    ],
)
def test_woods_mode_rates_only_a_wood_against_the_wood_contrast_limit(club, min_contrast):
    mode = _woods_mode(0.30, club)

    assert mode.polarities == ("abs",)
    assert mode.quality_on_difference == HeadQualityLimits(min_contrast=min_contrast)


def _run(path_deg, sigma_deg, status="accepted"):
    return _PathRun(ClubHeadPathResult(status, path_deg=path_deg, sigma_deg=sigma_deg, n_frames=5))


def test_the_woods_ensemble_takes_the_median_and_widens_sigma_by_half_the_spread():
    runs = {0.25: _run(-2.4, 0.8), 0.30: _run(-2.0, 0.6), 0.35: _run(-1.4, 0.7)}

    combined = _woods_ensemble(runs)

    assert combined.result.status == "accepted"
    assert combined.result.path_deg == -2.0
    assert combined.result.segmentation_sigma_deg == pytest.approx(0.5)
    assert combined.result.sigma_deg == pytest.approx(math.hypot(0.7, 0.5), abs=0.01)
    assert combined is not runs[0.30]
    assert combined.result.n_frames == runs[0.30].result.n_frames


def test_a_radius_without_a_track_makes_the_woods_path_unstable():
    runs = {
        0.25: _run(-2.4, 0.8),
        0.30: _run(None, None, "rejected_short_track"),
        0.35: _run(-1.4, 0.7),
    }

    combined = _woods_ensemble(runs)

    assert combined.result.status == "rejected_segmentation_unstable"
    assert combined.result.path_deg is None
    assert combined.result.segmentation_paths_deg == {"0.25": -2.4, "0.35": -1.4}


def test_no_woods_radius_with_a_track_keeps_the_middle_rejection():
    runs = {radius: _run(None, None, "rejected_head_quality") for radius in (0.25, 0.30, 0.35)}

    assert _woods_ensemble(runs).result.status == "rejected_head_quality"


def test_a_four_degree_spread_fails_the_two_degree_gate():
    runs = {0.25: _run(-4.0, 0.5), 0.30: _run(-2.0, 0.5), 0.35: _run(0.0, 0.5)}

    combined = _woods_ensemble(runs)

    assert combined.result.status == "rejected_sigma_too_large"
    assert combined.result.sigma_deg == pytest.approx(math.hypot(0.5, 2.0), abs=0.01)


# --- club gating ------------------------------------------------------------------------

_PATH_FIELDS = (
    "status",
    "path_deg",
    "sigma_deg",
    "n_frames",
    "feature",
    "polarity",
    "fit_sigma_deg",
    "jackknife_sigma_deg",
    "feature_sigma_deg",
    "contact_sigma_deg",
    "feature_paths_deg",
    "observations",
)


@pytest.mark.parametrize("path_deg", [-6.0, 0.0, 6.0])
def test_an_iron_is_bit_identical_to_the_polarity_estimator(straight_clips, path_deg):
    clip = straight_clips[path_deg]
    unset = estimate(*clip, attack=False)
    iron = estimate(*clip, club=ClubType.IRON_8)
    polarity = estimate(*clip, segmentation="polarity", attack=False)

    for name in _PATH_FIELDS:
        assert getattr(iron, name) == getattr(unset, name) == getattr(polarity, name)
    assert iron.segmentation == unset.segmentation == "polarity"
    assert unset.attack is None


@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_7, ClubType.HYBRID_5])
def test_woods_and_hybrids_run_woods_segmentation(two_tone_clip, club):
    result = estimate(*two_tone_clip, club=club)

    assert result.segmentation == "woods"
    assert result.polarity == "abs"


@pytest.mark.parametrize(
    ("club", "min_contrast"),
    [(ClubType.DRIVER, MIN_WOOD_HEAD_CONTRAST), (ClubType.HYBRID_5, MIN_HEAD_CONTRAST)],
)
def test_the_club_sets_the_contrast_limit_of_every_woods_rating(
    monkeypatch, two_tone_clip, club, min_contrast
):
    seen = set()

    def spy(raw, signed, head_mask, limits):
        seen.add(limits.min_contrast)
        return _difference_quality(raw, signed, head_mask, limits)

    monkeypatch.setattr(club_head_path_module, "_difference_quality", spy)

    estimate(*two_tone_clip, club=club, attack=False)

    assert seen == {min_contrast}


@pytest.mark.parametrize(("min_contrast", "impact_status"), [(12.0, "ok"), (29.0, "gap")])
def test_the_wood_contrast_limit_also_picks_the_attack_and_impact_frames(
    monkeypatch, two_tone_clip, min_contrast, impact_status
):
    # The head reads 28 grey levels in the last frame before contact: one rating
    # feeds the path, the attack arc and impact E's last frames alike.
    monkeypatch.setattr(
        club_head_path_module,
        "WOOD_DIFFERENCE_LIMITS",
        HeadQualityLimits(min_contrast=min_contrast),
    )
    attack_frames = []
    real_attack = club_head_path_module.estimate_attack_angle

    def spy(observations, **kwargs):
        attack_frames.extend(item.frame_index for item in observations if item.quality.ok)
        return real_attack(observations, **kwargs)

    monkeypatch.setattr(club_head_path_module, "estimate_attack_angle", spy)

    result = estimate(*two_tone_clip, club=ClubType.DRIVER)

    rated = [item.frame_index for item in result.observations if item.quality.ok]
    assert result.observations[-1].quality.contrast == 28.0
    assert (result.observations[-1].frame_index in rated) == (min_contrast == 12.0)
    assert attack_frames == rated
    assert (result.impact.status == "rejected_extrapolation_gap") == (impact_status == "gap")


def test_an_unknown_segmentation_or_handedness_is_a_programming_error(straight_clips):
    with pytest.raises(ValueError, match="segmentation"):
        estimate(*straight_clips[0.0], segmentation="sideways")
    with pytest.raises(ValueError, match="handedness"):
        estimate(*straight_clips[0.0], handedness="both")


# --- attack-angle hook ------------------------------------------------------------------


@pytest.mark.parametrize("attack_deg, path_deg", [(-4.0, 2.0), (-8.0, -3.0)])
def test_the_path_carries_the_arc_attack_angle(attack_deg, path_deg):
    clip = render_clip(Scenario(attack_deg=attack_deg, path_deg=path_deg))

    result = estimate(*clip)

    assert result.status == "accepted"
    assert result.attack.status == "accepted"
    assert result.attack.attack_deg == pytest.approx(attack_deg, abs=0.5)
    assert result.attack.low_point_cm is not None


def test_a_path_rejected_only_by_its_sigma_still_carries_an_attack():
    clip = render_clip(Scenario(attack_deg=-5.0, path_deg=1.0))

    result = estimate(*clip, contact_sigma_s=8e-3)

    unblurred = estimate(*clip)

    assert result.status == "rejected_sigma_too_large"
    assert unblurred.status == "accepted"
    assert result.attack.n_frames == unblurred.attack.n_frames >= 4
    assert result.attack.attack_deg == unblurred.attack.attack_deg
    assert result.attack.sigma_deg > unblurred.attack.sigma_deg


@pytest.mark.parametrize(
    "overrides, status",
    [({"club_speed_mph": None}, "rejected_no_club_speed"), ({}, "rejected_head_not_found")],
    ids=["no_club_speed", "no_head"],
)
def test_an_early_path_rejection_is_mirrored_into_the_attack(overrides, status):
    clip = render_clip(Scenario(with_head=False))

    result = estimate(*clip, **overrides)

    assert result.status == status
    assert result.attack.status == status
    assert result.impact.status == status


def _path_arc_kwargs():
    return {
        "camera": SimpleNamespace(direction_offset_deg=0.0),
        "geometry": None,
        "club_speed_mph": 90.0,
        "contact_sigma_s": 5e-4,
        "feature": "face_centre",
        "wood": True,
        "left_handed": False,
    }


def _arc_fields(monkeypatch, arcs):
    """``_path_arc_fields`` on runs whose arc path is given; returns the windows' observations."""
    runs = {
        radius: _PathRun(ClubHeadPathResult("accepted", observations=(radius,))) for radius in arcs
    }
    window_runs = []

    def windows(observations, **_):
        window_runs.append(observations)
        return {"drop_last": 1.0, "drop_first": 2.0}

    monkeypatch.setattr(
        club_head_path_module, "estimate_arc_path", lambda observations, **_: arcs[observations[0]]
    )
    monkeypatch.setattr(club_head_path_module, "arc_path_windows", windows)
    fields = _path_arc_fields(
        runs, AttackAngleResult("rejected_short_track"), (), **_path_arc_kwargs()
    )
    return fields, window_runs


def test_the_arc_path_of_the_woods_takes_the_median_and_widens_sigma_by_half_the_spread(
    monkeypatch,
):
    fields, window_runs = _arc_fields(
        monkeypatch,
        {
            0.25: ArcPath("accepted", -2.4, 0.8),
            0.30: ArcPath("accepted", -2.0, 0.6),
            0.35: ArcPath("accepted", -1.4, 0.7),
        },
    )

    assert fields["path_arc_status"] == "accepted"
    assert fields["path_arc_deg"] == -2.0
    assert fields["path_arc_sigma_deg"] == pytest.approx(math.hypot(0.7, 0.5), abs=0.01)
    assert fields["path_arc_radii_deg"] == {"0.25": -2.4, "0.30": -2.0, "0.35": -1.4}
    assert fields["path_arc_window_deg"] == {"drop_last": 1.0, "drop_first": 2.0}
    # The windows are those of the radius that gives the median.
    assert window_runs == [(0.30,)]


def test_a_radius_without_an_arc_path_leaves_the_woods_unstable(monkeypatch):
    fields, _ = _arc_fields(
        monkeypatch,
        {
            0.25: ArcPath("accepted", -2.4, 0.8),
            0.30: ArcPath("rejected_residual"),
            0.35: ArcPath("accepted", -1.4, 0.7),
        },
    )

    assert fields == {
        "path_arc_status": "rejected_segmentation_unstable",
        "path_arc_radii_deg": {"0.25": -2.4, "0.35": -1.4},
    }


def test_no_radius_with_an_arc_path_keeps_the_middle_rejection(monkeypatch):
    fields, _ = _arc_fields(
        monkeypatch,
        {
            0.25: ArcPath("rejected_short_track"),
            0.30: ArcPath("rejected_residual"),
            0.35: ArcPath("rejected_short_track"),
        },
    )

    assert fields == {"path_arc_status": "rejected_residual", "path_arc_radii_deg": {}}


@pytest.mark.parametrize(
    ("arc", "status"),
    (
        (ArcPath("accepted", 3.0, 2.5), "rejected_sigma_too_large"),
        (ArcPath("accepted", 21.0, 1.0), "rejected_implausible_path"),
        (ArcPath("accepted", 3.0, 1.0), "accepted"),
    ),
)
def test_the_arc_path_passes_the_paths_gates(monkeypatch, arc, status):
    fields, _ = _arc_fields(monkeypatch, {0.0: arc})

    assert fields["path_arc_status"] == status
    assert (fields["path_arc_deg"], fields["path_arc_sigma_deg"]) == (arc.path_deg, arc.sigma_deg)
    assert "path_arc_radii_deg" not in fields


def test_a_run_the_attack_angle_fitted_costs_no_new_arc_fit(monkeypatch):
    observations = (0.0,)
    attack = AttackAngleResult(
        "withheld_tee_height_unknown",
        arc_path_status="accepted",
        arc_path_deg=1.5,
        arc_path_total_sigma_deg=0.9,
    )
    run = _PathRun(ClubHeadPathResult("accepted", observations=observations))
    monkeypatch.setattr(club_head_path_module, "estimate_arc_path", None)  # would raise
    monkeypatch.setattr(club_head_path_module, "arc_path_windows", lambda *_, **__: {})

    fields = _path_arc_fields({0.0: run}, attack, observations, **_path_arc_kwargs())

    assert (fields["path_arc_deg"], fields["path_arc_sigma_deg"]) == (1.5, 0.9)


def test_the_arc_path_shadow_changes_nothing_else(monkeypatch, two_tone_clip):
    """With the shadow failing, path, attack and impact stay bit for bit."""
    result = estimate(*two_tone_clip, club=ClubType.DRIVER)

    def broken_fields(*_, **__):
        raise ValueError("broken")

    monkeypatch.setattr(club_head_path_module, "_path_arc_fields", broken_fields)
    broken = estimate(*two_tone_clip, club=ClubType.DRIVER)

    assert broken.path_arc_status == "error:ValueError"
    assert result.path_arc_status != "not_run"
    assert (
        replace(
            broken,
            path_arc_status=result.path_arc_status,
            path_arc_deg=result.path_arc_deg,
            path_arc_sigma_deg=result.path_arc_sigma_deg,
            path_arc_radii_deg=result.path_arc_radii_deg,
            path_arc_window_deg=result.path_arc_window_deg,
        )
        == result
    )


@pytest.mark.parametrize("sigma", (0.8, 1.0, 2.0, 4.0))
def test_the_background_noise_finds_the_sigma_of_quantised_gaussian_noise(sigma):
    rng = np.random.default_rng(int(sigma * 10))
    level = np.broadcast_to(rng.integers(60, 120, (120, 160)), (15, 120, 160))
    early = np.rint(level + rng.normal(0.0, sigma, level.shape)).astype(np.uint8)

    assert background_noise_sigma(early) == pytest.approx(sigma, rel=0.1)


def test_the_background_noise_ignores_clipped_pixels():
    rng = np.random.default_rng(5)
    early = np.rint(90.0 + rng.normal(0.0, 2.0, (15, 100, 100))).astype(np.uint8)
    clipped = early.copy()
    clipped[:, :, :40] = 255

    assert background_noise_sigma(clipped) == pytest.approx(2.0, rel=0.1)
    assert background_noise_sigma(np.full((15, 20, 20), 255, dtype=np.uint8)) is None


def test_the_clip_logs_its_background_noise_and_keeps_its_path(monkeypatch, two_tone_clip):
    result = estimate(*two_tone_clip, club=ClubType.DRIVER)
    monkeypatch.setattr(club_head_path_module, "background_noise_sigma", lambda _early: None)
    without = estimate(*two_tone_clip, club=ClubType.DRIVER)

    assert result.noise_sigma is not None
    assert without.noise_sigma is None
    assert replace(without, noise_sigma=result.noise_sigma) == result


def test_woods_withhold_the_attack_angle_without_a_tee_height(two_tone_clip):
    result = estimate(*two_tone_clip, club=ClubType.DRIVER)

    assert result.attack.status != "accepted"


# --- the club-data orchestrator ---------------------------------------------------------


def _live_departure_time_zero(trigger_ns: int, contact_s: float) -> CameraTimeZero:
    departure_ms = 1e3 * (contact_s - trigger_ns / 1e9)
    return CameraTimeZero(
        source="departure_fit",
        time_zero_ms=departure_ms,
        departure_fit_ms=departure_ms,
        acoustic_ms=SOUND_BOUND_MS,
        tee_patch_ms=None,
        iwr_impact_t_s=None,
    )


@pytest.mark.parametrize("live_fit", [True, False], ids=["live_fit", "no_live_fit"])
def test_club_data_runs_the_camera_departure_only_without_a_live_fit(monkeypatch, live_fit):
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0, path_deg=2.0))
    trigger_ns = _departure_trigger_ns(contact_s)
    calls = []

    def fake_departure(*_args, **kwargs):
        calls.append(kwargs)
        return DepartureFit(time_s=contact_s, n_points=6, rms_px=0.1, span_frames=2.0)

    monkeypatch.setattr(club_head_path_module, "estimate_departure_contact", fake_departure)
    # render_clip's sky starts 1.5 ball diameters above the ball: a stronger edge
    # than the ball for the refine template. Keep the clip's exact ball.
    monkeypatch.setattr(club_head_path_module, "refine_teed_ball", lambda *_a, **_k: None)
    result = estimate_club_data(
        frames,
        timestamps,
        time_zero=_live_departure_time_zero(trigger_ns, contact_s) if live_fit else None,
        trigger_ns=trigger_ns,
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        club=ClubType.IRON_8,
        reference_ball=ball,
        departed_ball=ball,
    )

    assert set(result.stage_elapsed_s) == {"departure", "path", "attack", "impact", "impact_point"}
    assert all(value >= 0.0 for value in result.stage_elapsed_s.values())
    if live_fit:
        assert calls == []
        assert result.camera_departure is None
        assert result.contact.source == "departure_fit"
        assert result.stage_elapsed_s["departure"] == 0.0
    else:
        assert len(calls) == 1
        assert result.contact.source == "camera_departure"
        assert result.camera_departure is not None
    assert result.contact.t0_ms == pytest.approx(1e3 * (contact_s - trigger_ns / 1e9))
    assert result.path.status == "accepted"
    assert result.path.ball_source == "departed"
    assert result.path.ball_px == pytest.approx((ball.x, ball.y, ball.diameter_px), abs=0.01)
    assert result.path.attack.status == "accepted"
    assert result.path.impact.status == "accepted"


def test_club_data_departs_from_the_refined_ball_and_impacts_on_the_departed_one(monkeypatch):
    """A teed ball whose locator circle sits 3 px high and 13 px wide still times the contact.

    Departing from that circle puts the fit 0.56 ms late; the refined disk is on the ball.
    """
    frames, timestamps, contact_s, ball = render_departure_clip()
    trigger_ns = _departure_trigger_ns(contact_s)
    departed = ReferenceBall(ball.x + 0.4, ball.y - 3.0, 13.2, 137)
    path_calls = []
    real_path = club_head_path_module.estimate_club_head_path

    def spy_path(*args, **kwargs):
        path_calls.append(kwargs)
        return real_path(*args, **kwargs)

    monkeypatch.setattr(club_head_path_module, "estimate_club_head_path", spy_path)
    result = estimate_club_data(
        frames,
        timestamps,
        time_zero=None,
        trigger_ns=trigger_ns,
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        teed=True,
        club=ClubType.DRIVER,
        departed_ball=departed,
    )

    assert result.camera_departure is not None
    assert result.contact.source == "camera_departure"
    assert result.contact.time_s == pytest.approx(contact_s, abs=0.25e-3)
    assert path_calls[0]["departed_ball"] is departed
    used = path_calls[0]["reference_ball"]
    assert math.hypot(used.x - ball.x, used.y - ball.y) <= 0.5
    assert path_calls[0]["ball_source"] == "refined"


def test_club_data_names_a_camera_departure_it_searched_and_did_not_find(monkeypatch):
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0))
    monkeypatch.setattr(club_head_path_module, "estimate_departure_contact", lambda *_a, **_k: None)

    result = estimate_club_data(
        frames,
        timestamps,
        time_zero=None,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        teed=True,
        reference_ball=ball,
    )

    assert (result.contact.source, result.contact.reason) == (
        "acoustic_calibrated",
        "no_camera_departure",
    )
    assert result.contact.sigma_s == 0.5e-3


def _late_fit_club_data(monkeypatch, *, teed=False, shadow=None):
    """A mat iron whose live fit, at true contact, lies 1 ms after the stamp sound bound."""
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0, path_deg=2.0))
    trigger_ns = _departure_trigger_ns(contact_s, stamp_lag_s=-0.001)
    monkeypatch.setattr(club_head_path_module, "refine_teed_ball", lambda *_a, **_k: None)
    if shadow is not None:
        monkeypatch.setattr(club_head_path_module, "shadow_contact_time", shadow)
    time_zero = _live_departure_time_zero(trigger_ns, contact_s)
    kwargs = {
        "time_zero": time_zero,
        "trigger_ns": trigger_ns,
        "tee_range_m": TEE_RANGE_M,
        "teed": teed,
    }
    result = estimate_club_data(
        frames,
        timestamps,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        club=ClubType.IRON_8,
        reference_ball=ball,
        departed_ball=ball,
        **kwargs,
    )
    return result, select_contact_time(**kwargs), 1e3 * (contact_s - trigger_ns / 1e9)


def test_club_data_logs_the_shadow_contact_and_keeps_the_acoustic_one(monkeypatch):
    result, selected, true_ms = _late_fit_club_data(monkeypatch)

    assert result.contact == selected
    assert (result.contact.source, result.contact.reason) == (
        "acoustic_calibrated",
        "departure_fit_after_sound_bound",
    )
    shadow = result.contact_shadow
    assert (shadow.reason, shadow.source) == ("accepted", "live_departure_fit")
    assert shadow.t0_ms == pytest.approx(true_ms) == pytest.approx(SOUND_BOUND_MS + 1.0)
    assert shadow.minus_t0_ms == pytest.approx(1.0 + ACOUSTIC_STAMP_DELAY_MS)


def test_the_shadow_changes_nothing_the_job_computes(monkeypatch):
    """The whole result, path, attack and impact included, is the one without a shadow."""
    with_shadow, _selected, _true_ms = _late_fit_club_data(monkeypatch)
    assert with_shadow.contact_shadow.reason == "accepted"
    without, _selected, _true_ms = _late_fit_club_data(
        monkeypatch, shadow=lambda *_a, **_k: ContactShadow("no_candidate")
    )

    assert with_shadow.path.status == "accepted"
    assert replace(with_shadow, stage_elapsed_s={}, contact_shadow=None) == replace(
        without, stage_elapsed_s={}, contact_shadow=None
    )


def test_club_data_gives_a_teed_shot_no_shadow(monkeypatch):
    result, selected, _true_ms = _late_fit_club_data(monkeypatch, teed=True)

    assert result.contact == selected
    assert result.contact_shadow == ContactShadow("teed")


def test_club_data_judges_the_shadow_on_the_selected_reason(monkeypatch):
    """A camera departure after the bound is the candidate; a departure searched and not
    found renames the reason to no_camera_departure only after the shadow was judged."""
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0))
    trigger_ns = _departure_trigger_ns(contact_s, stamp_lag_s=-0.0005)
    late = DepartureFit(time_s=contact_s, n_points=6, rms_px=0.1, span_frames=2.0)
    monkeypatch.setattr(club_head_path_module, "estimate_departure_contact", lambda *_a, **_k: late)
    kwargs = {
        "time_zero": None,
        "trigger_ns": trigger_ns,
        "tee_range_m": TEE_RANGE_M,
        "geometry": GEOMETRY,
        "club_speed_mph": CLUB_SPEED_MPH,
        "reference_ball": ball,
    }

    found = estimate_club_data(frames, timestamps, **kwargs)
    monkeypatch.setattr(club_head_path_module, "estimate_departure_contact", lambda *_a, **_k: None)
    missing = estimate_club_data(frames, timestamps, **kwargs)

    assert (found.contact.source, found.contact.reason) == (
        "acoustic_calibrated",
        "camera_departure_after_sound_bound",
    )
    assert (found.contact_shadow.reason, found.contact_shadow.source) == (
        "accepted",
        "camera_departure",
    )
    assert found.contact_shadow.t0_ms == pytest.approx(1e3 * (contact_s - trigger_ns / 1e9))
    assert missing.contact.reason == "no_camera_departure"
    assert missing.contact_shadow == ContactShadow("no_candidate")


def _observation(t_rel_ms: float, status: str = "ok") -> HeadObservation:
    quality = HeadQuality(status, 40.0, 300, 20.0, 2.0, 0.0)
    return HeadObservation(frame_index=0, t_rel_ms=t_rel_ms, features_px={}, quality=quality)


def test_the_path_gap_runs_from_the_last_usable_frame_to_contact():
    path = ClubHeadPathResult(
        "accepted",
        observations=(
            _observation(-9.03),
            _observation(-5.56),
            _observation(-2.0874),
            _observation(1.38, "low_contrast"),
        ),
    )

    assert path.last_frame_gap_ms == 2.087
    assert replace(path, observations=path.observations[:2]).last_frame_gap_ms == 5.56
    assert (
        replace(path, observations=(_observation(-1.0, "low_contrast"),)).last_frame_gap_ms is None
    )
    assert ClubHeadPathResult("rejected_head_not_found").last_frame_gap_ms is None


def test_club_data_with_a_dropped_anchor_finds_no_ball_of_its_own(monkeypatch):
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0))
    for name in ("estimate_departure_contact", "estimate_club_head_path"):
        monkeypatch.setattr(
            club_head_path_module,
            name,
            lambda *_a, **_k: pytest.fail("a dropped anchor is no ball"),
        )

    result = estimate_club_data(
        frames,
        timestamps,
        time_zero=None,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        club=ClubType.IRON_8,
        reference_ball=ball,
        tee_zone=_zone(0, int(ball.y) + 30, WIDTH, HEIGHT),
    )

    assert result.camera_departure is None
    assert (result.path.status, result.path.ball_source) == (
        "rejected_reference_ball_not_found",
        None,
    )
    assert result.path.attack.status == "rejected_reference_ball_not_found"
    assert result.path.impact.status == "rejected_reference_ball_not_found"
    assert result.ball == ClubBall(
        None,
        None,
        "mark",
        rejected=("tracker_anchor_outside_hitting_zone", "mark_anchor_outside_hitting_zone"),
    )


@pytest.mark.parametrize(
    ("club", "teed"),
    [(ClubType.DRIVER, True), (ClubType.WOOD_3, False), (ClubType.HYBRID_5, False)],
)
def test_teed_club_data_drops_the_mark_for_no_ball(club, teed, monkeypatch):
    """The driver (``teed``) and a fairway wood or hybrid the server does not call teed."""
    frames, timestamps, contact_s, ball = render_clip(Scenario(attack_deg=-4.0))
    monkeypatch.setattr(
        club_head_path_module,
        "refine_teed_ball",
        lambda *_a, **_k: pytest.fail("a club that may be teed never refines around the mark"),
    )

    result = estimate_club_data(
        frames,
        timestamps,
        time_zero=None,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        club=club,
        teed=teed,
        reference_ball=ball,
        tee_zone=_zone(0, int(ball.y) + 30, WIDTH, HEIGHT),
    )

    assert (result.path.status, result.path.ball_source) == (
        "rejected_reference_ball_not_found",
        None,
    )
    assert result.ball == ClubBall(
        None,
        None,
        "mark",
        rejected=("tracker_anchor_outside_hitting_zone", "mark_anchor_club_may_be_teed"),
    )


def test_club_data_without_a_ball_skips_the_departure(monkeypatch):
    frames, timestamps, contact_s, _ball = render_clip(Scenario())
    monkeypatch.setattr(
        club_head_path_module,
        "estimate_departure_contact",
        lambda *_a, **_k: pytest.fail("no ball, no departure"),
    )
    blank = np.full_like(frames, 120)

    result = estimate_club_data(
        blank,
        timestamps,
        time_zero=None,
        trigger_ns=_departure_trigger_ns(contact_s),
        tee_range_m=TEE_RANGE_M,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
    )

    assert result.camera_departure is None
    assert result.path.status == "rejected_reference_ball_not_found"
    assert result.path.attack.status == "rejected_reference_ball_not_found"
