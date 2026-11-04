"""Tests for experimental camera-assisted horizontal ball flight."""

import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

import openflight.camera.ball_flight as ball_flight_module
from openflight.camera.ball_flight import (
    BallCandidate,
    CameraBallEstimate,
    CameraBallGeometry,
    _camera_model,
    _confidence_tier,
    _path_estimate,
    _rough_path_score,
    estimate_camera_ball_flight,
    select_camera_assisted_horizontal,
)
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.club_motion import ReferenceBall
from openflight.iwr6843.lcmf import BallRangeEvidence, LCMFResult


def test_ball_flight_uses_established_anchor_when_detection_is_missing(monkeypatch):
    tracker = ReferenceBallTracker()
    for x in (159.0, 160.0, 161.0):
        tracker.resolve_stable(ReferenceBall(x, 100.0, 14.0, 140))
    monkeypatch.setattr(
        ball_flight_module,
        "detect_reference_ball",
        lambda _frames: (_ for _ in ()).throw(ValueError("not found")),
    )

    result = estimate_camera_ball_flight(
        np.zeros((20, 200, 320), dtype=np.uint8),
        np.arange(20, dtype=np.int64) * 2_000_000,
        trigger_ns=10_000_000,
        range_evidence=None,
        geometry=CameraBallGeometry(
            camera_height_m=0.2032,
            radar_height_m=0.1524,
            tee_range_m=1.524,
            ball_height_m=0.04,
            image_width_px=320,
            image_height_px=200,
        ),
        ops_ball_speed_mph=100.0,
        ball_tracker=tracker,
    )

    assert result.status != "rejected_reference_ball_not_found"


# Range session 2026-10-06: fifteen wedges teed at one spot, then the 8-iron
# teed about 3 cm away (11 px). The rolling anchor stayed on the wedge spot, so
# the contrast check read empty mat and rejected all seven 8-iron shots.
WEDGE_SPOT = (336.6, 265.2)
IRON_BALL = ReferenceBall(326.0, 261.0, 18.0, 250)
RANGE_GEOMETRY = CameraBallGeometry(
    camera_height_m=0.182,
    radar_height_m=0.132,
    tee_range_m=1.5,
    ball_height_m=0.02135,
    camera_lateral_offset_m=-0.025,
    camera_forward_offset_m=0.007,
    yaw_from_reference_ball=True,
    image_width_px=640,
    image_height_px=400,
)


def _tracker_after_wedges() -> ReferenceBallTracker:
    tracker = ReferenceBallTracker()
    for jitter in (-1.5, 0.5, -0.5, 1.0, 0.0) * 3:
        tracker.resolve(ReferenceBall(WEDGE_SPOT[0] + jitter, WEDGE_SPOT[1], 15.0, 180))
    return tracker


def _still_ball_capture(ball: ReferenceBall) -> tuple[np.ndarray, np.ndarray]:
    """Sunlit mat with the ball darker than its surroundings, as on 2026-10-06."""
    frames = np.full((60, 400, 640), 220, dtype=np.uint8)
    yy, xx = np.mgrid[:400, :640]
    frames[:, (xx - ball.x) ** 2 + (yy - ball.y) ** 2 <= (ball.diameter_px / 2) ** 2] = 110
    return frames, np.arange(60, dtype=np.int64) * 3_474_000


def _estimate_range_capture(frames, timestamps, tracker):
    return estimate_camera_ball_flight(
        frames,
        timestamps,
        trigger_ns=int(timestamps[45]),
        range_evidence=None,
        geometry=RANGE_GEOMETRY,
        ops_ball_speed_mph=105.0,
        ball_tracker=tracker,
    )


def test_a_re_teed_ball_is_read_where_it_lies_not_at_the_session_anchor(monkeypatch):
    frames, timestamps = _still_ball_capture(IRON_BALL)
    monkeypatch.setattr(ball_flight_module, "detect_reference_ball", lambda _frames: IRON_BALL)

    result = _estimate_range_capture(frames, timestamps, _tracker_after_wedges())

    # A still ball never departs: reaching that check means the contrast passed.
    assert result.status == "rejected_no_ball_departure"


def test_camera_pose_still_comes_from_the_session_anchor(monkeypatch):
    frames, timestamps = _still_ball_capture(IRON_BALL)
    monkeypatch.setattr(ball_flight_module, "detect_reference_ball", lambda _frames: IRON_BALL)
    pose_balls = []
    camera_model = ball_flight_module._camera_model
    monkeypatch.setattr(
        ball_flight_module,
        "_camera_model",
        lambda ball, geometry: pose_balls.append(ball) or camera_model(ball, geometry),
    )

    _estimate_range_capture(frames, timestamps, _tracker_after_wedges())

    # One re-teed ball must not swing the camera yaw; it is derived from the ball.
    assert len(pose_balls) == 1
    assert pose_balls[0].x == pytest.approx(WEDGE_SPOT[0], abs=1.0)
    assert pose_balls[0].y == pytest.approx(WEDGE_SPOT[1], abs=1.0)


def test_a_false_blob_away_from_the_tee_still_falls_back_to_the_session_anchor(monkeypatch):
    frames, timestamps = _still_ball_capture(ReferenceBall(*WEDGE_SPOT, 15.0, 180))
    false_blob = ReferenceBall(520.0, 120.0, 15.0, 180)
    monkeypatch.setattr(ball_flight_module, "detect_reference_ball", lambda _frames: false_blob)

    result = _estimate_range_capture(frames, timestamps, _tracker_after_wedges())

    assert result.status == "rejected_no_ball_departure"


def _project_world_point(
    point: np.ndarray,
    *,
    camera_height_m: float,
    camera_lateral_offset_m: float = 0.0,
    focal_px: float,
    pitch_rad: float,
    width: int,
    height: int,
) -> tuple[float, float]:
    vector = point - np.array([camera_lateral_offset_m, 0.0, camera_height_m])
    camera_forward = math.cos(pitch_rad) * vector[1] + math.sin(pitch_rad) * vector[2]
    camera_vertical = -math.sin(pitch_rad) * vector[1] + math.cos(pitch_rad) * vector[2]
    return (
        width / 2 + focal_px * vector[0] / camera_forward,
        height / 2 - focal_px * camera_vertical / camera_forward,
    )


def _synthetic_path(
    horizontal_deg: float = 4.0,
    tee_x_m: float = 0.0,
    horizontal_offset_deg: float = 0.0,
    camera_lateral_offset_m: float = 0.0,
    mirror_horizontal: bool = False,
    target_azimuth_deg: float | None = None,
):
    """A synthetic track; ``target_azimuth_deg`` taps a target 100 m away on the ground."""
    geometry = CameraBallGeometry(
        camera_height_m=0.20955,
        radar_height_m=0.15875,
        tee_range_m=1.524,
        ball_height_m=0.04,
        camera_lateral_offset_m=camera_lateral_offset_m,
        horizontal_offset_deg=horizontal_offset_deg,
        horizontal_pixel_sign=-1.0 if mirror_horizontal else 1.0,
        image_width_px=640,
        image_height_px=400,
    )
    if target_azimuth_deg is not None:
        target = np.array(
            [
                camera_lateral_offset_m + 100.0 * math.sin(math.radians(target_azimuth_deg)),
                100.0 * math.cos(math.radians(target_azimuth_deg)),
                0.0,
            ]
        )
        target_x, target_y = _project_world_point(
            target,
            camera_height_m=geometry.camera_height_m,
            camera_lateral_offset_m=camera_lateral_offset_m,
            focal_px=480.0,
            pitch_rad=0.0,
            width=geometry.image_width_px,
            height=geometry.image_height_px,
        )
        if mirror_horizontal:
            target_x = geometry.image_width_px - target_x
        geometry = replace(geometry, target_px=(target_x, target_y))
    focal_px = 480.0
    pitch_rad = 0.0
    tee_forward_m = math.sqrt(
        geometry.tee_range_m**2
        - (geometry.ball_height_m - geometry.radar_height_m) ** 2
        - tee_x_m**2
    )
    tee = np.array([tee_x_m, tee_forward_m, geometry.ball_height_m])
    anchor_x, anchor_y = _project_world_point(
        tee,
        camera_height_m=geometry.camera_height_m,
        camera_lateral_offset_m=geometry.camera_lateral_offset_m,
        focal_px=focal_px,
        pitch_rad=pitch_rad,
        width=geometry.image_width_px,
        height=geometry.image_height_px,
    )
    if mirror_horizontal:
        anchor_x = geometry.image_width_px - anchor_x
    camera_origin = np.array([geometry.camera_lateral_offset_m, 0.0, geometry.camera_height_m])
    camera_ball_range = np.linalg.norm(tee - camera_origin)
    anchor = ReferenceBall(
        x=anchor_x,
        y=anchor_y,
        diameter_px=focal_px * geometry.ball_diameter_m / camera_ball_range,
        area_px=140,
    )
    model = _camera_model(anchor, geometry)

    speed_ms = 45.0
    horizontal = math.radians(horizontal_deg)
    vertical = math.radians(20.0)
    forward = speed_ms * math.cos(vertical) * math.cos(horizontal)
    velocity = np.array(
        [
            forward * math.tan(horizontal),
            forward,
            speed_ms * math.sin(vertical),
        ]
    )
    relative_times = np.arange(8, dtype=float) * 0.0035 + 0.007
    positions = np.asarray([tee + velocity * time_s for time_s in relative_times])
    timestamps_ns = np.asarray(relative_times * 1e9, dtype=np.int64)

    candidates = []
    for point in positions:
        x_px, y_px = _project_world_point(
            point,
            camera_height_m=geometry.camera_height_m,
            camera_lateral_offset_m=geometry.camera_lateral_offset_m,
            focal_px=focal_px,
            pitch_rad=pitch_rad,
            width=geometry.image_width_px,
            height=geometry.image_height_px,
        )
        if mirror_horizontal:
            x_px = geometry.image_width_px - x_px
        camera_range = np.linalg.norm(point - camera_origin)
        diameter = focal_px * geometry.ball_diameter_m / camera_range
        candidates.append(
            BallCandidate(
                x=x_px,
                y=y_px,
                area=max(5, round(math.pi * (diameter / 2) ** 2)),
                width=max(2, round(diameter)),
                height=max(2, round(diameter)),
                fill=0.75,
                circularity=0.9,
                mean_intensity=220.0,
                local_contrast=150.0,
            )
        )

    radar_origin = np.array([0.0, 0.0, geometry.radar_height_m])
    radar_ranges = np.linalg.norm(positions - radar_origin, axis=1)

    class SyntheticRangeTrack:
        def range_at(self, time_s, _range_resolution_m):
            return float(np.interp(time_s, relative_times, radar_ranges))

    evidence = SimpleNamespace(
        track=SyntheticRangeTrack(),
        geometry=SimpleNamespace(range_res_m=0.046875),
        impact_t_s=0.0,
    )
    return geometry, anchor, model, candidates, timestamps_ns, evidence, speed_ms


def test_path_estimate_recovers_known_horizontal_without_iwr_horizontal():
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path()

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(4.0, abs=0.15)
    assert estimate.vertical_deg == pytest.approx(20.0, abs=0.25)


def test_path_estimate_recovers_physical_sign_from_mirrored_capture():
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path(
        horizontal_deg=4.0,
        mirror_horizontal=True,
    )

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(4.0, abs=0.15)


def test_rough_path_scoring_avoids_numpy_in_beam_search(monkeypatch):
    path = [
        (0, BallCandidate(10, 20, 10, 3, 4, 0.7, 0.8, 220)),
        (1, BallCandidate(12, 17, 10, 3, 4, 0.7, 0.8, 220)),
        (2, BallCandidate(14, 14, 10, 3, 4, 0.7, 0.8, 220)),
    ]

    def reject_numpy_median(*_args, **_kwargs):
        raise AssertionError("beam-search scoring must remain scalar")

    monkeypatch.setattr(np, "median", reject_numpy_median)
    assert _rough_path_score(path) == pytest.approx(60.0)


def test_path_estimate_recovers_horizontal_from_apparent_ball_size_without_iwr_range():
    geometry, _anchor, model, candidates, timestamps, _evidence, speed_ms = _synthetic_path()

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=None,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(4.0, abs=0.75)
    assert estimate.vertical_deg == pytest.approx(20.0, abs=1.5)


def test_path_estimate_does_not_treat_lateral_ball_position_as_target_yaw():
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path(
        horizontal_deg=0.0,
        tee_x_m=-0.03,
    )

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(0.0, abs=0.15)


def test_path_estimate_accounts_for_camera_lateral_translation():
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path(
        horizontal_deg=4.0,
        camera_lateral_offset_m=0.08,
    )

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(4.0, abs=0.15)
    assert estimate.vertical_deg == pytest.approx(20.0, abs=0.25)


def test_path_estimate_applies_setup_level_horizontal_offset():
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path(
        horizontal_deg=4.0,
        horizontal_offset_deg=-0.45,
    )

    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )

    assert result is not None
    _score, estimate = result
    assert estimate.horizontal_deg == pytest.approx(3.55, abs=0.15)


def _horizontal_of(path) -> float:
    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = path
    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(100, 12, 5),
    )
    assert result is not None
    return result[1].horizontal_deg


@pytest.mark.parametrize("mirror_horizontal", [False, True])
def test_a_ball_flying_at_the_tapped_target_starts_on_the_target_line(mirror_horizontal):
    path = _synthetic_path(
        horizontal_deg=4.0, target_azimuth_deg=4.0, mirror_horizontal=mirror_horizontal
    )

    assert path[2].target_azimuth_deg == pytest.approx(4.0, abs=0.01)
    assert path[2].direction_offset_deg == pytest.approx(-path[2].target_azimuth_deg)
    assert _horizontal_of(path) == pytest.approx(0.0, abs=0.15)


def test_the_target_line_replaces_the_setup_horizontal_offset():
    path = _synthetic_path(horizontal_deg=4.0, target_azimuth_deg=-1.5, horizontal_offset_deg=-0.45)

    assert _horizontal_of(path) == pytest.approx(5.5, abs=0.15)


def test_without_a_target_the_model_keeps_the_setup_horizontal_offset():
    model = _synthetic_path(horizontal_offset_deg=-0.45)[2]

    assert model.target_azimuth_deg is None
    assert model.direction_offset_deg == -0.45


def test_the_target_azimuth_carries_the_yaw_from_the_teed_ball():
    """A constant camera yaw is in both the start direction and the target, so it cancels."""
    geometry, anchor, model, *_rest = _synthetic_path(target_azimuth_deg=3.0)
    yawed_geometry = replace(geometry, yaw_from_reference_ball=True)
    shifted_anchor = ReferenceBall(anchor.x - 12.0, anchor.y, anchor.diameter_px, anchor.area_px)
    yawed = _camera_model(shifted_anchor, yawed_geometry)
    unyawed = _camera_model(shifted_anchor, geometry)

    assert yawed.yaw_rad != 0.0
    assert yawed.target_azimuth_deg - unyawed.target_azimuth_deg == pytest.approx(
        math.degrees(yawed.yaw_rad), abs=0.01
    )
    assert model.target_azimuth_deg == pytest.approx(3.0, abs=0.01)


def test_high_confidence_requires_tight_local_trajectory_stability():
    assert _confidence_tier(20, 0.2, 0.49) == "high"
    assert _confidence_tier(20, 0.2, 0.51) == "experimental"
    assert _confidence_tier(20, 1.1, 0.2) == "withheld"


def _estimate(tier: str, angle: float | None) -> CameraBallEstimate:
    return CameraBallEstimate(
        status="accepted" if angle is not None else "rejected_no_stable_path",
        confidence_tier=tier,
        horizontal_deg=angle,
    )


def test_high_camera_estimate_replaces_iwr_but_preserves_both_values():
    decision = select_camera_assisted_horizontal(
        _estimate("high", 0.6),
        iwr_horizontal_deg=17.9,
        iwr_confidence=0.8,
    )

    assert decision.selected_deg == pytest.approx(0.6)
    assert decision.source == "camera_assisted_experimental"
    assert decision.iwr_horizontal_deg == pytest.approx(17.9)
    assert decision.camera_iwr_delta_deg == pytest.approx(-17.3)
    assert decision.status == "camera_assisted_high"


def test_experimental_camera_disagreement_keeps_measured_camera_trajectory():
    decision = select_camera_assisted_horizontal(
        _estimate("experimental", 5.0),
        iwr_horizontal_deg=-2.0,
        iwr_confidence=0.7,
    )

    assert decision.selected_deg == pytest.approx(5.0)
    assert decision.source == "camera_assisted_experimental"
    assert decision.confidence == pytest.approx(0.3)
    assert decision.camera_horizontal_deg == pytest.approx(5.0)
    assert decision.status == "camera_experimental_disagreement"


def test_experimental_camera_agreement_is_selected():
    decision = select_camera_assisted_horizontal(
        _estimate("experimental", 1.0),
        iwr_horizontal_deg=-1.0,
        iwr_confidence=0.7,
    )

    assert decision.selected_deg == pytest.approx(1.0)
    assert decision.source == "camera_assisted_experimental"
    assert decision.confidence == pytest.approx(0.45)
    assert decision.status == "camera_assisted_experimental_agreement"


def test_experimental_camera_is_retained_when_iwr_is_unavailable():
    decision = select_camera_assisted_horizontal(
        _estimate("experimental", 5.0),
        iwr_horizontal_deg=None,
        iwr_confidence=None,
    )

    assert decision.selected_deg == pytest.approx(5.0)
    assert decision.source == "camera_assisted_experimental"
    assert decision.confidence == pytest.approx(0.3)
    assert decision.status == "camera_experimental_no_iwr"


def test_camera_size_depth_is_labeled_as_camera_only_fallback():
    estimate = CameraBallEstimate(
        status="accepted_camera_only",
        confidence_tier="experimental",
        horizontal_deg=3.0,
        depth_source="camera_size",
    )

    decision = select_camera_assisted_horizontal(
        estimate,
        iwr_horizontal_deg=None,
        iwr_confidence=None,
    )

    assert decision.selected_deg == pytest.approx(3.0)
    assert decision.source == "camera_only_experimental"
    assert decision.confidence == pytest.approx(0.3)
    assert decision.status == "camera_only_experimental"


def test_camera_size_depth_remains_selected_when_iwr_is_available():
    estimate = CameraBallEstimate(
        status="accepted_camera_only",
        confidence_tier="experimental",
        horizontal_deg=3.0,
        depth_source="camera_size",
    )

    decision = select_camera_assisted_horizontal(
        estimate,
        iwr_horizontal_deg=-8.0,
        iwr_confidence=0.9,
    )

    assert decision.selected_deg == pytest.approx(3.0)
    assert decision.source == "camera_only_experimental"
    assert decision.confidence == pytest.approx(0.3)
    assert decision.status == "camera_only_experimental"


def test_withheld_camera_falls_back_to_unchanged_iwr():
    decision = select_camera_assisted_horizontal(
        _estimate("withheld", None),
        iwr_horizontal_deg=-2.0,
        iwr_confidence=0.7,
    )

    assert decision.selected_deg == pytest.approx(-2.0)
    assert decision.source == "radar"
    assert decision.confidence == pytest.approx(0.7)
    assert decision.status == "camera_withheld_fallback_iwr"


def test_withheld_camera_rejects_implausible_iwr_fallback():
    decision = select_camera_assisted_horizontal(
        _estimate("withheld", None),
        iwr_horizontal_deg=38.0,
        iwr_confidence=0.95,
    )

    assert decision.selected_deg is None
    assert decision.source is None
    assert decision.confidence is None
    assert decision.iwr_horizontal_deg == pytest.approx(38.0)
    assert decision.status == "camera_withheld_iwr_implausible"


def test_lcmf_ball_range_evidence_is_transient():
    evidence = BallRangeEvidence(
        track=SimpleNamespace(name="ball-track"),
        geometry=SimpleNamespace(range_res_m=0.046875),
        impact_t_s=0.012,
    )
    result = LCMFResult(
        status="accepted",
        angle_deg=20.0,
        range_evidence=evidence,
    )

    assert result.range_evidence is evidence
    assert "range_evidence" not in result.to_dict()


# --- gate-free ball departure (fit_ball_departure) ---------------------------

DEPARTURE_FRAME_COUNT = 60
DEPARTURE_FRAME_S = 1.0 / 288.0
DEPARTURE_LAST_REST_FRAME = 40
DEPARTURE_AFTER_FRAME_S = 0.0012


def render_departure_clip(
    *,
    ball_value: float = 170.0,
    mat_value: float = 90.0,
    head_cover: bool = True,
    tee: bool = True,
    speed_ms: float = 40.0,
    launch_deg: float = 14.0,
    horizontal_deg: float = 2.0,
    seed: int = 5,
):
    """A teed ball receding on a straight line at constant speed from contact on.

    Its image distance from the resting ball is then exactly s(t) = S - K / (t - t_inf).
    The first frame after contact has a dark head blob over the ball's left half; a
    low-contrast tee stays under the resting spot. Returns frames, timestamps (ns),
    the true contact (s) and the resting ball.
    """
    from test_club_head_path import (  # pylint: disable=import-outside-toplevel
        CAMERA_ORIGIN,
        FOCAL_PX,
        GEOMETRY,
        HEIGHT,
        WIDTH,
        Camera,
        _paint_disk,
        _paint_polygon,
    )

    camera = Camera()
    rng = np.random.default_rng(seed)
    times = np.arange(DEPARTURE_FRAME_COUNT) * DEPARTURE_FRAME_S
    contact = times[DEPARTURE_LAST_REST_FRAME] + DEPARTURE_AFTER_FRAME_S
    rest = np.array([0.0, GEOMETRY.ball_forward_m, GEOMETRY.ball_height_m])
    vertical, horizontal = math.radians(launch_deg), math.radians(horizontal_deg)
    direction = np.array(
        [
            math.sin(horizontal) * math.cos(vertical),
            math.cos(horizontal) * math.cos(vertical),
            math.sin(vertical),
        ]
    )

    def radius_px(point):
        return FOCAL_PX * GEOMETRY.ball_diameter_m / 2.0 / np.linalg.norm(point - CAMERA_ORIGIN)

    rest_px = camera.project(rest)[0]
    rest_radius = radius_px(rest)
    background = np.full((HEIGHT, WIDTH), mat_value) + rng.normal(0.0, 3.0, (HEIGHT, WIDTH))
    if tee:
        stem = rest_px + [0.0, rest_radius]
        _paint_polygon(
            background,
            np.array(
                [stem + [-3.0, 0.0], stem + [3.0, 0.0], stem + [3.0, 14.0], stem + [-3.0, 14.0]]
            ),
            mat_value - 8.0,
        )
    frames = np.empty((DEPARTURE_FRAME_COUNT, HEIGHT, WIDTH), dtype=np.uint8)
    first_after = DEPARTURE_LAST_REST_FRAME + 1
    for index, time_s in enumerate(times):
        image = background.copy()
        elapsed = max(0.0, time_s - contact)
        centre = rest + speed_ms * elapsed * direction
        centre_px = camera.project(centre)[0]
        _paint_disk(image, centre_px, radius_px(centre), ball_value)
        if head_cover and index == first_after:
            radius = radius_px(centre)
            _paint_polygon(
                image,
                np.array(
                    [
                        centre_px + [-3.0 * radius, -1.2 * radius],
                        centre_px + [0.0, -1.2 * radius],
                        centre_px + [0.0, 1.5 * radius],
                        centre_px + [-3.0 * radius, 1.5 * radius],
                    ]
                ),
                30.0,
            )
        image += rng.normal(0.0, 1.5, image.shape)
        frames[index] = np.clip(np.round(image), 0, 255).astype(np.uint8)
    ball = ReferenceBall(
        x=float(rest_px[0]),
        y=float(rest_px[1]),
        diameter_px=float(2.0 * rest_radius),
        area_px=int(math.pi * rest_radius**2),
    )
    return frames, (times * 1e9).astype(np.int64), float(contact), ball


@pytest.fixture(name="departure_clip", scope="module")
def fixture_departure_clip():
    return render_departure_clip()


def _fit(frames, timestamps, ball, **overrides):
    from test_club_head_path import GEOMETRY  # pylint: disable=import-outside-toplevel

    kwargs = {"start_index": DEPARTURE_LAST_REST_FRAME, "geometry": GEOMETRY, "ball": ball}
    kwargs.update(overrides)
    return ball_flight_module.fit_ball_departure(frames, timestamps, **kwargs)


def test_gate_free_departure_recovers_the_contact_time(departure_clip):
    frames, timestamps, contact, ball = departure_clip

    fit = _fit(frames, timestamps, ball)

    assert fit is not None
    assert fit.n_points >= 4
    assert fit.rms_px <= ball_flight_module.DEPARTURE_FIT_MAX_RMS_PX
    assert fit.time_s == pytest.approx(contact, abs=0.25e-3)


def test_gate_free_departure_runs_below_the_live_contrast_gate():
    """A 7-wood ball 18 grey levels off the mat fails the live 20-level gate, not this fit."""
    frames, timestamps, contact, ball = render_departure_clip(ball_value=108.0, seed=11)

    fit = _fit(frames, timestamps, ball)

    assert fit is not None
    assert fit.time_s == pytest.approx(contact, abs=0.25e-3)


def test_gate_free_departure_needs_four_points(departure_clip):
    frames, timestamps, _contact, ball = departure_clip

    assert _fit(frames, timestamps, ball, n_frames=3) is None
    assert _fit(frames, timestamps, ball, start_index=len(frames) - 3) is None


@pytest.mark.parametrize(
    "frames, timestamps",
    [
        (np.zeros((0, 4, 4), np.uint8), np.zeros(0, np.int64)),
        (np.zeros((5, 4, 4), np.uint8), np.zeros(4, np.int64)),
        (np.zeros((4, 4), np.uint8), np.zeros(4, np.int64)),
    ],
)
def test_gate_free_departure_returns_none_for_invalid_input(frames, timestamps):
    ball = ReferenceBall(2.0, 2.0, 16.0, 200)

    assert _fit(frames, timestamps, ball, start_index=0) is None


def test_gate_free_departure_without_a_departing_ball_returns_none(departure_clip):
    frames, timestamps, _contact, ball = departure_clip
    still = np.repeat(frames[:1], len(frames), axis=0)

    assert _fit(still, timestamps, ball) is None


def test_gate_free_departure_leaves_the_live_ball_flight_bit_identical(departure_clip):
    """The new fit shares no state with the live estimator: its result and tracker stay the same."""
    from test_club_head_path import GEOMETRY  # pylint: disable=import-outside-toplevel

    frames, timestamps, contact, ball = departure_clip
    trigger_ns = int(round((contact + GEOMETRY.tee_range_m / 343.0 + 0.0012) * 1e9))
    results = {}
    for locator in ball_flight_module.BALL_LOCATORS:
        runs = []
        for with_fit in (False, True):
            tracker = ReferenceBallTracker()
            for _ in range(3):
                tracker.resolve_stable(ball)
            if with_fit:
                _fit(frames, timestamps, ball)
            estimate = estimate_camera_ball_flight(
                frames,
                timestamps,
                trigger_ns=trigger_ns,
                range_evidence=None,
                geometry=GEOMETRY,
                ops_ball_speed_mph=89.5,
                ball_tracker=tracker,
                ball_locator=locator,
            )
            runs.append((estimate, tracker.fallback()))
        results[locator] = runs
    for runs in results.values():
        assert runs[0] == runs[1]
    assert any(runs[0][0].time_zero is not None for runs in results.values())
