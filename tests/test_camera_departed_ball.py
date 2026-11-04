"""Disappeared-ball locator, OPS motion gate and line-fit check of the camera ball flight.

Synthetic clips only: a mat (and sky above a horizon), a resting ball at the
geometric tee, optional rubber tee, static clutter and club head, and a ball
leaving at a known direction and speed. 288 fps, 45 frames before the trigger.
"""

import copy
import json
import math
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from test_camera_ball_flight import _project_world_point
from test_camera_cv_worker import _init_camera

import openflight.camera.ball_flight as ball_flight_module
from openflight import server
from openflight.camera import cv_worker
from openflight.camera.ball_flight import (
    LINE_FIT_MAX_RMS_MRAD,
    MPH_PER_MS,
    SPEED_OF_SOUND_M_S,
    BallCandidate,
    BallLocatorDiagnostics,
    CameraBallEstimate,
    CameraBallGeometry,
    _camera_model,
    _grey_difference_median,
    _line_fit,
    _median,
    _MotionGate,
    _sweep_candidates,
    estimate_camera_ball_flight,
)
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.club_motion import ReferenceBall, detect_reference_ball
from openflight.camera.departed_ball import (
    CONTRAST_FLOOR,
    CORE_RADIUS_FRAC,
    MARK_WINDOW_HALF_PX,
    MIN_CLIP_POST_FRAMES,
    MIN_POST_FRAMES,
    POST_WINDOW_FRAMES,
    SNR_MIN,
    TRIGGER_LAG_FRAMES,
    DepartedBall,
    GroundBallAnchor,
    departure_index,
    in_window,
    locate_departed_ball,
    mark_window,
    min_post_ms,
    search_box,
)
from openflight.camera.lowlight import estimate_lowlight_ball_flight
from openflight.camera.tee_zone import TeeZone
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger

PERIOD_NS = 3_474_000
FRAMES = 90
TRIGGER_INDEX = 45
IMPACT_INDEX = 43  # impact half a frame after this exposure; the trigger follows by sound
FOCAL_PX = 480.0
WIDTH, HEIGHT = 640, 400
SPEED_MS = 45.0
GEOMETRY = CameraBallGeometry(
    camera_height_m=0.20955,
    radar_height_m=0.15875,
    tee_range_m=1.524,
    ball_height_m=0.04,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)
TEE = np.array([0.0, GEOMETRY.ball_forward_m, GEOMETRY.ball_height_m])


def _pixel(point: np.ndarray) -> tuple[float, float, float]:
    x, y = _project_world_point(
        point,
        camera_height_m=GEOMETRY.camera_height_m,
        focal_px=FOCAL_PX,
        pitch_rad=0.0,
        width=WIDTH,
        height=HEIGHT,
    )
    distance = float(np.linalg.norm(point - GEOMETRY.camera_origin))
    return x, y, FOCAL_PX * GEOMETRY.ball_diameter_m / distance


REST_X, REST_Y, REST_D = _pixel(TEE)
ZONE = TeeZone(
    x0=round(REST_X) - 70,
    y0=round(REST_Y) - 40,
    x1=round(REST_X) + 70,
    y1=round(REST_Y) + 45,
    tee_x=REST_X,
    tee_y=REST_Y,
    diameter_px=REST_D,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)


def _velocity(horizontal_deg: float, vertical_deg: float, speed_ms: float = SPEED_MS):
    horizontal, vertical = math.radians(horizontal_deg), math.radians(vertical_deg)
    return speed_ms * np.array(
        [
            math.cos(vertical) * math.sin(horizontal),
            math.cos(vertical) * math.cos(horizontal),
            math.sin(vertical),
        ]
    )


def _times_ns() -> np.ndarray:
    return np.arange(FRAMES, dtype=np.int64) * PERIOD_NS


def _impact_ns() -> int:
    return int(_times_ns()[IMPACT_INDEX] + PERIOD_NS // 2)


def _trigger_ns(latency_ms: float = 0.3) -> int:
    sound_ns = GEOMETRY.tee_range_m / SPEED_OF_SOUND_M_S * 1e9
    return round(_impact_ns() + sound_ns + latency_ms * 1e6)


def _disk(frame, x, y, diameter, value):
    cv2.circle(frame, (round(x), round(y)), max(1, round(diameter / 2)), value, -1)


def _render(  # noqa: PLR0913  pylint: disable=too-many-arguments
    *,
    mat: int = 150,
    sky: int | None = None,
    horizon_y: int = 0,
    ball: int = 40,
    flight: tuple[float, float] | None = (4.0, 20.0),
    tee: int | None = None,
    clutter: tuple = (),
    club_frames: int = 0,
    noise: float = 1.5,
    seed: int = 7,
):
    """A clip and its timestamps; ``flight`` None leaves the ball at rest, False removes it."""
    frames = np.full((FRAMES, HEIGHT, WIDTH), mat, np.float32)
    if sky is not None:
        frames[:, :horizon_y, :] = sky
    for x, y, diameter, value in clutter:
        for frame in frames:
            _disk(frame, x, y, diameter, value)
    if tee is not None:
        top = round(REST_Y + REST_D / 2) - 1
        for frame in frames:
            cv2.rectangle(frame, (round(REST_X) - 2, top), (round(REST_X) + 2, top + 14), tee, -1)
    times = _times_ns()
    velocity = None if not flight else _velocity(*flight)
    for index, frame in enumerate(frames):
        elapsed_s = (int(times[index]) - _impact_ns()) / 1e9
        if flight is False:
            continue
        if velocity is None or elapsed_s < 0.0:
            _disk(frame, REST_X, REST_Y, REST_D, ball)
            continue
        x, y, diameter = _pixel(TEE + velocity * elapsed_s)
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            _disk(frame, x, y, diameter, ball)
            if index - IMPACT_INDEX <= club_frames:
                # The club head right behind and around the low ball.
                cv2.ellipse(frame, (round(x), round(y) + 2), (22, 14), 0, 0, 360, ball, -1)
    rng = np.random.default_rng(seed)
    frames += rng.normal(0.0, noise, frames.shape).astype(np.float32)
    return np.clip(np.round(frames), 0, 255).astype(np.uint8), times


class _RangeTrack:
    def __init__(self, velocity):
        self.velocity = velocity

    def range_at(self, time_s, _range_resolution_m):
        radar = np.array([0.0, 0.0, GEOMETRY.radar_height_m])
        return float(np.linalg.norm(TEE + self.velocity * max(time_s, 0.0) - radar))


def _evidence(flight=(4.0, 20.0)):
    return SimpleNamespace(
        track=_RangeTrack(_velocity(*flight)),
        geometry=SimpleNamespace(range_res_m=0.046875),
        impact_t_s=0.0,
    )


def _estimate(frames, times, *, locator="disappeared", flight=(4.0, 20.0), **kwargs):
    defaults = {
        "trigger_ns": _trigger_ns(),
        "range_evidence": _evidence(flight),
        "geometry": GEOMETRY,
        "ops_ball_speed_mph": SPEED_MS * MPH_PER_MS,
        "ball_locator": locator,
        "tee_zone": ZONE,
    }
    return estimate_camera_ball_flight(frames, times, **{**defaults, **kwargs})


def _locate(frames):
    return locate_departed_ball(
        frames,
        TRIGGER_INDEX,
        expected_diameter_px=REST_D,
        box=(ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1),
    )


# --- the locator on its own ------------------------------------------------------------


def test_the_resting_ball_is_what_disappeared():
    frames, _times = _render()

    ball, reason = _locate(frames)

    assert reason == "located"
    assert ball.x == pytest.approx(REST_X, abs=0.7)
    assert ball.y == pytest.approx(REST_Y, abs=0.7)
    assert ball.diameter_px == pytest.approx(REST_D, rel=0.15)
    assert ball.polarity == -1
    assert ball.contrast == pytest.approx(110.0, abs=5.0)
    assert ball.gate_failure() is None
    assert not ball.teed


def test_a_teed_ball_is_found_apart_from_its_tee():
    """Ball and rubber tee merge into one blob before the strike; only the ball leaves."""
    frames, _times = _render(tee=40)

    ball, reason = _locate(frames)

    assert reason == "located"
    assert ball.x == pytest.approx(REST_X, abs=0.7)
    assert ball.y == pytest.approx(REST_Y, abs=0.7)
    assert ball.diameter_px == pytest.approx(REST_D, rel=0.15)
    assert ball.teed


def test_static_clutter_stronger_than_the_ball_cancels():
    clutter = (
        (REST_X - 45, REST_Y - 10, REST_D, 0),
        (REST_X + 40, REST_Y + 20, REST_D * 1.3, 255),
    )
    frames, _times = _render(ball=110, clutter=clutter)

    ball, reason = _locate(frames)

    assert reason == "located"
    assert ball.x == pytest.approx(REST_X, abs=0.7)
    assert ball.y == pytest.approx(REST_Y, abs=0.7)


def test_a_low_contrast_ball_passes_the_relative_gate_but_not_the_legacy_threshold():
    """A 7-wood ball 12 grey levels off the mat (2026-10-25: 26-28 at 100 us / gain 2)."""
    frames, _times = _render(mat=120, ball=132, noise=1.0)

    ball, reason = _locate(frames)

    assert reason == "located"
    assert ball.polarity == 1
    assert ball.contrast == pytest.approx(12.0, abs=2.0)
    assert ball.contrast >= CONTRAST_FLOOR and ball.snr >= SNR_MIN
    assert ball.gate_failure() is None


def test_nothing_departed_fails_the_noise_gate():
    clutter = ((REST_X - 45, REST_Y - 10, REST_D, 30),)
    frames, _times = _render(flight=False, clutter=clutter)

    ball, reason = _locate(frames)

    assert ball is None or ball.gate_failure() in ("low_contrast", "low_snr")
    assert reason in ("located", "no_ball_edge", "implausible_ball_size")


def test_a_short_clip_has_no_late_frames_to_compare():
    frames, _times = _render()

    ball, reason = _locate(frames[: TRIGGER_INDEX + 20])

    assert ball is None
    assert reason == "insufficient_post_frames"


def test_a_clip_starting_late_has_no_clean_reference_frames():
    frames, _times = _render()

    ball, reason = locate_departed_ball(
        frames[20:],
        TRIGGER_INDEX - 20,
        expected_diameter_px=REST_D,
        box=(ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1),
    )

    assert ball is None
    assert reason == "insufficient_pre_frames"


def test_departure_frame_uses_the_clip_contrast_not_thirty_grey_levels():
    frames, _times = _render(mat=120, ball=132, noise=1.0)
    ball, _reason = _locate(frames)

    # The core changes by only ~12 grey levels when the ball leaves.
    assert departure_index(frames, ball, TRIGGER_INDEX) == IMPACT_INDEX


def test_search_box_prefers_the_zone_and_else_centres_on_the_estimate():
    estimate = ReferenceBall(300.0, 250.0, 14.0, 154)

    assert search_box((HEIGHT, WIDTH), zone=ZONE, tee_estimate=estimate) == (
        (ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1),
        ZONE.diameter_px,
    )
    box, diameter = search_box((HEIGHT, WIDTH), zone=None, tee_estimate=estimate)
    assert box == (210, 205, 390, 290)
    assert diameter == 14.0
    assert search_box((HEIGHT, WIDTH), zone=None, tee_estimate=None) is None
    other_mode = TeeZone(**{**vars(ZONE), "image_width_px": 320, "image_height_px": 200})
    assert search_box((HEIGHT, WIDTH), zone=other_mode, tee_estimate=None) is None


# --- contrast of a ball lit from above (2026-11-02) ------------------------------------


def _shaded_ball_clip(  # noqa: PLR0913  pylint: disable=too-many-arguments
    *, upper=30.0, lower=-20.0, gradient=False, terminator=0.0, noise=1.5, mat=150.0
):
    """A resting ball lit from above on a mat: brighter than the mat on top, darker below.

    2026-11-02 at the daylight lock (350-450 us): the ball's upper half +30 grey, its
    self-shadowed lower half -20, so D = pre - post is positive above and negative below.
    ``gradient`` shades it smoothly from ``upper`` at the top to ``lower`` at the bottom
    (a sphere), else two halves. ``terminator`` is the share of the diameter, centred on
    the ball's middle row, where the two halves meet at the mat's grey (D = 0): the
    self-shadow line of the lit top. The ball leaves at the impact and does not reappear.
    """
    frames, times = _render(mat=int(mat), flight=False, noise=0.0)
    frames = frames.astype(np.float32)
    yy, xx = np.mgrid[:HEIGHT, :WIDTH]
    radius = REST_D / 2.0
    disk = (xx - REST_X) ** 2 + (yy - REST_Y) ** 2 <= radius * radius
    if gradient:
        share = np.clip((yy - (REST_Y - radius)) / (2.0 * radius), 0.0, 1.0)
        shade = upper + (lower - upper) * share
    else:
        band = terminator * radius
        shade = np.where(yy < REST_Y - band, upper, np.where(yy > REST_Y + band, lower, 0.0))
    frames[: IMPACT_INDEX + 1, disk] += shade[disk].astype(np.float32)
    frames += np.random.default_rng(7).normal(0.0, noise, frames.shape).astype(np.float32)
    return np.clip(np.round(frames), 0, 255).astype(np.uint8), times


def _core_median_change(frames, ball):
    """The pre-fix contrast: median |D| of the core (0.4 x the fitted diameter) at ``ball``."""
    change = np.median(frames[: IMPACT_INDEX + 1], axis=0) - np.median(
        frames[IMPACT_INDEX + 1 :], axis=0
    )
    yy, xx = np.mgrid[:HEIGHT, :WIDTH]
    core = np.hypot(xx - ball.x, yy - ball.y) <= CORE_RADIUS_FRAC * ball.diameter_px
    return float(np.median(np.abs(change[core])))


def test_a_two_tone_ball_passes_the_contrast_gate():
    """Upper half +30, lower half -20, meeting at a terminator band of the mat's grey.

    The core's median |D| lands on the band (~1 grey) and failed the gate before the fix;
    the better half's mean keeps the +30 of the lit top.
    """
    frames, _times = _shaded_ball_clip(terminator=0.3)

    ball, reason = _locate(frames)

    assert reason == "located"
    assert _core_median_change(frames, ball) < CONTRAST_FLOOR
    assert ball.gate_failure() is None
    assert ball.contrast == pytest.approx(30.0, abs=3.0)
    assert ball.x == pytest.approx(REST_X, abs=1.0)
    # The edge fit leans towards the lit top (1.3 px here, up to 1.9 px over other seeds).
    assert ball.y == pytest.approx(REST_Y, abs=2.0)


def test_a_ball_shaded_from_above_counts_its_better_half():
    """The median |D| of the whole core sat on the zero crossing between the halves (7.5)."""
    frames, _times = _shaded_ball_clip(gradient=True)

    ball, reason = _locate(frames)

    assert reason == "located"
    assert ball.gate_failure() is None
    assert ball.contrast > 12.0
    assert ball.snr >= SNR_MIN


def test_the_contrast_floor_and_snr_gate_are_unchanged():
    assert (CONTRAST_FLOOR, SNR_MIN) == (8.0, 8.0)
    frames, _times = _shaded_ball_clip(upper=6.0, lower=-6.0, noise=0.5)

    ball, _reason = _locate(frames)

    assert ball is None or ball.gate_failure() == "low_contrast"


# --- ground anchor (camera pose) -------------------------------------------------------


def _departed(x, y, tee_px=0.0):
    return DepartedBall(x, y, 13.5, 30.0, 1.0, 30.0, -1, 0.2, 20, tee_px)


def test_only_untee_d_balls_set_the_camera_pose():
    anchor = GroundBallAnchor()
    for x in (300.0, 301.0):
        anchor.observe(_departed(x, 260.0))
    anchor.observe(_departed(350.0, 245.0, tee_px=12.0))
    assert anchor.anchor() is None
    assert anchor.last.y == 245.0

    anchor.observe(_departed(302.0, 261.0))

    pose = anchor.anchor()
    assert (pose.x, pose.y) == (301.0, 260.0)
    assert anchor.samples == 3


def test_ground_anchor_adopts_a_worker_copy():
    anchor, copy_in_worker = GroundBallAnchor(), GroundBallAnchor()
    for x in (300.0, 301.0, 302.0):
        copy_in_worker.observe(_departed(x, 260.0))

    anchor.adopt(copy_in_worker)

    assert anchor.anchor() == copy_in_worker.anchor()
    assert anchor.last == copy_in_worker.last
    assert cv_worker.STATEFUL_ARGUMENTS == ("ball_tracker", "ground_anchor")
    assert cv_worker._stateful({"ground_anchor": anchor, "frames": None}) == {
        "ground_anchor": anchor
    }


# --- OPS motion gate and line fit ------------------------------------------------------


def _model():
    return _camera_model(ReferenceBall(REST_X, REST_Y, REST_D, 143), GEOMETRY)


def _candidate(x, y):
    return BallCandidate(x, y, 140, 13, 13, 0.8, 0.9, 40.0, 100.0)


def test_motion_gate_admits_the_ball_and_rejects_what_the_ops_speed_cannot_reach():
    model = _model()
    acoustic_s = _impact_ns() / 1e9
    frame_s = (_times_ns()[IMPACT_INDEX + 3]) / 1e9
    gate = _MotionGate(
        ball=ReferenceBall(REST_X, REST_Y, REST_D, 143),
        model=model,
        geometry=GEOMETRY,
        frame_times_s=np.array([frame_s]),
        acoustic_s=acoustic_s,
        speed_mph=SPEED_MS * MPH_PER_MS,
    )
    ball_x, ball_y, _diameter = _pixel(TEE + _velocity(4.0, 20.0) * (frame_s - acoustic_s))

    on_track, far_away = gate.directions(
        0, [_candidate(ball_x, ball_y), _candidate(REST_X + 150, 30.0)]
    )

    assert far_away == ()
    horizontal = [h for h, _v in on_track]
    vertical = [v for _h, v in on_track]
    assert min(horizontal) - 1.0 <= 4.0 <= max(horizontal) + 1.0
    assert min(vertical) - 1.0 <= 20.0 <= max(vertical) + 1.0


def _track(velocity, frames=8):
    times = _times_ns()
    path, elapsed = [], []
    for relative in range(frames):
        frame = IMPACT_INDEX + 1 + relative
        seconds = (int(times[frame]) - _impact_ns()) / 1e9
        x, y, _diameter = _pixel(TEE + velocity * seconds)
        path.append((relative, _candidate(x, y)))
        elapsed.append(seconds)
    return path, np.asarray(elapsed)


def test_line_fit_at_the_ops_speed_explains_a_straight_track():
    path, elapsed = _track(_velocity(-6.0, 12.0))

    fit = _line_fit(
        path,
        elapsed_s=elapsed,
        ball=ReferenceBall(REST_X, REST_Y, REST_D, 143),
        model=_model(),
        geometry=GEOMETRY,
        speed_mph=SPEED_MS * MPH_PER_MS,
    )

    assert fit.rms_mrad < 0.5
    assert fit.horizontal_deg == pytest.approx(-6.0, abs=0.2)
    assert fit.vertical_deg == pytest.approx(12.0, abs=0.3)
    assert fit.n_points == 8


def test_line_fit_misses_a_track_bent_off_its_line():
    path, elapsed = _track(_velocity(-6.0, 12.0))
    bent = [
        (relative, _candidate(candidate.x + (6.0 if relative >= 4 else 0.0), candidate.y))
        for relative, candidate in path
    ]

    fit = _line_fit(
        bent,
        elapsed_s=elapsed,
        ball=ReferenceBall(REST_X, REST_Y, REST_D, 143),
        model=_model(),
        geometry=GEOMETRY,
        speed_mph=SPEED_MS * MPH_PER_MS,
    )

    assert fit.rms_mrad > LINE_FIT_MAX_RMS_MRAD


def test_median_helpers_equal_numpy():
    rng = np.random.default_rng(3)
    for size in (1, 2, 5, 8, 1001):
        first = rng.integers(0, 256, size).astype(np.int16)
        second = rng.integers(0, 256, size).astype(np.int16)
        assert _grey_difference_median(first - second) == np.median(first - second)
        values = rng.normal(size=size)
        assert _median(values) == np.median(values)


# --- the estimator end to end ----------------------------------------------------------


def test_disappeared_locator_measures_the_flight():
    frames, times = _render()
    anchor = GroundBallAnchor()

    result = _estimate(frames, times, ground_anchor=anchor)

    assert result.confidence_tier != "withheld"
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert result.vertical_deg == pytest.approx(20.0, abs=0.7)
    locator = result.locator
    assert (locator.locator, locator.reason) == ("disappeared", "located")
    assert locator.pose_source == "this_ball"
    assert locator.motion_gate == "applied"
    assert 0 < locator.gate_kept <= locator.gate_candidates
    assert locator.line_fit_status == "ok"
    assert locator.line_fit.rms_mrad <= LINE_FIT_MAX_RMS_MRAD
    assert anchor.samples == 1


def test_a_tapped_target_sets_the_line_the_flight_is_measured_from():
    """The consensus and the TI-free line fit both report relative to the target."""
    frames, times = _render()
    target_x, target_y, _diameter = _pixel(
        np.array([100.0 * math.sin(math.radians(1.5)), 100.0 * math.cos(math.radians(1.5)), 0.0])
    )
    geometry = replace(GEOMETRY, target_px=(target_x, target_y))

    result = _estimate(frames, times, geometry=geometry, ground_anchor=GroundBallAnchor())
    plain = _estimate(frames, times, ground_anchor=GroundBallAnchor())

    assert result.confidence_tier != "withheld"
    # The pose (focal scale, pitch) comes from the rendered ball, so not exactly 1.5.
    assert result.target_azimuth_deg == pytest.approx(1.5, abs=0.05)
    assert plain.target_azimuth_deg is None
    shift = result.target_azimuth_deg
    assert result.horizontal_deg == pytest.approx(plain.horizontal_deg - shift, abs=1e-6)
    assert result.vertical_deg == pytest.approx(plain.vertical_deg, abs=1e-9)
    assert result.locator.line_fit.horizontal_deg == pytest.approx(
        plain.locator.line_fit.horizontal_deg - shift, abs=1e-3
    )


def test_a_driver_on_its_profile_tee_never_feeds_the_ground_anchor_whatever_its_tee_rows():
    """2026-11-02: 6 of 19 drivers showed only 3-6 tee rows (limit 0.5 d ~ 7) and fed the anchor."""
    frames, times = _render(flight=(-3.0, 10.0))  # no tee rows visible below the ball
    on_tee = replace(GEOMETRY, tee_height_m=0.04)  # server._shot_ball_height: driver, 4 cm
    anchor = GroundBallAnchor()

    result = _estimate(frames, times, flight=(-3.0, 10.0), geometry=on_tee, ground_anchor=anchor)

    assert result.locator.reason == "located"
    assert not result.locator.teed  # the tee rows alone say "on the mat"
    assert result.locator.ground_anchor_sample is False
    assert result.locator.pose_source == "teed_ball"
    assert result.horizontal_deg == pytest.approx(-3.0, abs=0.5)
    assert anchor.samples == 0
    assert anchor.last is not None


def test_other_clubs_keep_the_tee_row_detection_for_the_ground_anchor():
    anchor = GroundBallAnchor()

    plain = _estimate(*_render(flight=(-3.0, 10.0)), flight=(-3.0, 10.0), ground_anchor=anchor)
    assert plain.locator.ground_anchor_sample is True
    assert anchor.samples == 1

    teed = _estimate(
        *_render(tee=40, flight=(-3.0, 10.0)), flight=(-3.0, 10.0), ground_anchor=anchor
    )
    assert teed.locator.teed and teed.locator.ground_anchor_sample is False
    assert anchor.samples == 1


def test_the_ground_anchor_takes_the_caller_s_tee_verdict():
    anchor = GroundBallAnchor()

    anchor.observe(_departed(300.0, 260.0), teed=True)
    anchor.observe(_departed(301.0, 245.0, tee_px=12.0), teed=None)  # its own tee rows: teed
    anchor.observe(_departed(302.0, 261.0, tee_px=12.0), teed=False)

    assert anchor.samples == 1
    assert anchor.last.x == 302.0


def test_a_teed_driver_ball_is_measured_and_never_moves_the_pose():
    frames, times = _render(tee=40, flight=(-3.0, 10.0))
    anchor = GroundBallAnchor()

    result = _estimate(frames, times, flight=(-3.0, 10.0), ground_anchor=anchor)

    assert result.confidence_tier != "withheld"
    assert result.horizontal_deg == pytest.approx(-3.0, abs=0.5)
    assert result.locator.teed
    assert result.locator.pose_source == "teed_ball"
    assert anchor.samples == 0
    assert anchor.last is not None


def test_a_teed_ball_is_measured_with_the_ground_anchor_pose():
    """Untee'd balls of the session rest lower than the teed one; their median sets the pitch."""
    frames, times = _render(tee=40, flight=(-3.0, 10.0))
    own_pose = _estimate(frames, times, flight=(-3.0, 10.0), ground_anchor=GroundBallAnchor())
    anchor = GroundBallAnchor()
    for jitter in (-0.5, 0.0, 0.5):
        anchor.observe(_departed(REST_X + jitter, REST_Y + 6.0))
    ground = anchor.anchor()

    result = _estimate(frames, times, flight=(-3.0, 10.0), ground_anchor=anchor)

    locator = result.locator
    assert locator.teed
    assert locator.pose_source == "ground_anchor"
    assert (locator.pose_x, locator.pose_y) == (ground.x, ground.y)
    assert anchor.samples == 3
    assert result.status == "accepted"
    teed = ReferenceBall(
        own_pose.locator.ball_x, own_pose.locator.ball_y, own_pose.locator.ball_diameter_px, 143
    )
    pitch_shift_deg = math.degrees(
        _camera_model(ground, GEOMETRY).pitch_rad - _camera_model(teed, GEOMETRY).pitch_rad
    )
    assert pitch_shift_deg > 0.5
    # The flight is measured through the anchor's camera model, not the teed ball's.
    assert result.vertical_deg - own_pose.vertical_deg == pytest.approx(pitch_shift_deg, abs=0.05)
    assert result.horizontal_deg == pytest.approx(own_pose.horizontal_deg, abs=0.05)


def test_the_column_below_a_teed_ball_holds_no_flight_candidates():
    """The rubber tee flexes after impact; whatever moves right below the ball is not the ball."""
    background = np.full((HEIGHT, WIDTH), 150, np.uint8)
    frame = background.copy()
    ball = ReferenceBall(REST_X, REST_Y, REST_D, 143)
    _disk(frame, REST_X, REST_Y + 14.0, 6, 40)  # the tee head, in the column
    _disk(frame, REST_X + 30.0, REST_Y + 14.0, 6, 40)  # beside the column

    def found(tee_column):
        candidates = _sweep_candidates(
            frame, background, ball, thresholds=((100, 12),), min_area=5, tee_column=tee_column
        )[(100, 12)]
        return sorted(round(candidate.x - REST_X) for candidate in candidates)

    assert found(tee_column=False) == [0, 30]
    assert found(tee_column=True) == [30]


def test_the_estimator_excludes_the_tee_column_only_for_a_teed_ball(monkeypatch):
    seen = []

    def spy(*args, **kwargs):
        seen.append(kwargs["tee_column"])
        return _sweep_candidates(*args, **kwargs)

    monkeypatch.setattr(ball_flight_module, "_sweep_candidates", spy)

    _estimate(*_render(tee=40, flight=(-3.0, 10.0)), flight=(-3.0, 10.0))
    teed, seen[:] = set(seen), []
    _estimate(*_render())

    assert teed == {True}
    assert set(seen) == {False}


def test_a_decoy_off_the_ball_s_direction_never_extends_its_track():
    """The ball leaves the view; a ball-like spot at the OPS speed but 10 deg off appears.

    It shows up after the gated start window, so it can only be reached by
    growing the ball's track onto it, which the direction tolerance forbids.
    """
    decoy_from = IMPACT_INDEX + 11
    frames, times = _render()
    frames[decoy_from:] = _render(flight=False)[0][decoy_from:]
    velocity = _velocity(14.0, 20.0)
    for index in range(decoy_from, FRAMES):
        x, y, diameter = _pixel(TEE + velocity * (int(times[index]) - _impact_ns()) / 1e9)
        if 0 <= x < WIDTH and 0 <= y < HEIGHT:
            _disk(frames[index], x, y, diameter, 40)

    result = _estimate(frames, times)

    assert result.status == "accepted"
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert result.last_frame < decoy_from
    assert result.locator.line_fit_status == "ok"


def test_legacy_detector_takes_the_ball_and_tee_as_one_blob():
    """2026-10-25 drivers: the merged ~15x27 px blob sat 3 px low; the legacy run reads it."""
    frames, times = _render(tee=40, flight=(-3.0, 10.0))

    merged = detect_reference_ball(frames)
    ball, _reason = _locate(frames)
    legacy = _estimate(frames, times, locator="legacy", flight=(-3.0, 10.0))

    assert merged.diameter_px > 1.5 * REST_D
    assert merged.y - REST_Y > 2.0
    assert ball.y == pytest.approx(REST_Y, abs=0.7)
    assert legacy.locator.locator == "legacy"
    assert legacy.locator.line_fit_status is None


def test_low_contrast_seven_wood_ball_is_measured():
    """12 grey levels off the mat at rest; against the sky in flight."""
    frames, times = _render(mat=120, ball=132, sky=60, horizon_y=round(REST_Y) - 12, noise=1.0)

    result = _estimate(frames, times)
    legacy = _estimate(frames, times, locator="legacy")

    assert result.confidence_tier != "withheld"
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert legacy.horizontal_deg is None
    assert legacy.status in (
        "rejected_reference_ball_not_found",
        "rejected_reference_ball_contrast",
    )


def test_ball_hidden_by_the_club_head_for_seven_frames_is_still_tracked():
    frames, times = _render(flight=(2.0, 8.0), club_frames=7)

    result = _estimate(frames, times, flight=(2.0, 8.0))
    legacy = _estimate(frames, times, locator="legacy", flight=(2.0, 8.0))

    assert result.confidence_tier != "withheld"
    assert result.horizontal_deg == pytest.approx(2.0, abs=0.5)
    assert result.first_frame > IMPACT_INDEX + 7
    # Legacy tracks must start within five flight frames of the tee.
    assert legacy.status == "rejected_no_stable_path"


def test_no_ball_no_direction():
    clutter = ((REST_X - 45, REST_Y - 10, REST_D, 30),)
    frames, times = _render(flight=False, clutter=clutter)

    result = _estimate(frames, times)

    assert result.horizontal_deg is None
    assert result.confidence_tier == "withheld"
    assert result.locator.locator == "legacy_fallback"
    assert result.locator.reason in ("low_contrast", "low_snr", "no_ball_edge")


def test_a_ball_still_at_rest_gives_no_direction():
    frames, times = _render(flight=None)

    result = _estimate(frames, times)

    assert result.horizontal_deg is None
    assert result.locator.locator == "legacy_fallback"


def test_a_located_ball_without_a_departure_frame_is_rejected_json_safe():
    """A late trigger (or a slow scene change) leaves no departure in [trigger-8, trigger+10]."""
    frames, times = _render()
    anchor, tracker = GroundBallAnchor(), ReferenceBallTracker()

    result = _estimate(
        frames,
        times,
        trigger_ns=_trigger_ns(latency_ms=40),
        ground_anchor=anchor,
        ball_tracker=tracker,
    )

    assert result.status == "rejected_no_ball_departure"
    assert result.horizontal_deg is None
    assert result.time_zero is None
    assert (result.locator.locator, result.locator.reason) == ("disappeared", "no_ball_departure")
    assert result.locator.ball_x == pytest.approx(REST_X, abs=0.7)
    # Neither the pose nor the session tee anchor learns from it.
    assert anchor.samples == 0
    assert anchor.last is None
    assert tracker.fallback() is None
    shot = Shot(ball_speed_mph=95.0, timestamp=datetime.now())
    shot.experimental_camera_time_zero = server._camera_time_zero_diagnostics(result, shot)
    shot.experimental_camera_ball_locator = result.locator.to_dict()
    json.dumps(shot.to_dict(), allow_nan=False)
    json.dumps(server.shot_to_dict(shot), allow_nan=False)


def test_line_fit_over_its_limit_withholds_the_direction(monkeypatch):
    frames, times = _render()
    monkeypatch.setattr(ball_flight_module, "LINE_FIT_MAX_RMS_MRAD", 0.0)

    result = _estimate(frames, times)

    assert result.status == "rejected_line_fit_residual"
    assert result.confidence_tier == "withheld"
    assert result.horizontal_deg is None
    assert result.vertical_deg is not None
    assert result.locator.line_fit_status == "over_residual"


def test_without_an_ops_speed_there_is_no_gate_and_no_line_fit():
    frames, times = _render()

    result = _estimate(frames, times, ops_ball_speed_mph=0.0)

    assert result.locator.motion_gate == "no_ops_speed"
    assert result.locator.gate_candidates is None


def test_legacy_mode_never_runs_the_disappeared_locator(monkeypatch):
    frames, times = _render()
    monkeypatch.setattr(
        ball_flight_module,
        "locate_departed_ball",
        lambda *_args, **_kwargs: pytest.fail("legacy mode must not locate a departed ball"),
    )

    result = _estimate(frames, times, locator="legacy")

    assert result.locator == BallLocatorDiagnostics("legacy", "legacy")


def test_an_unknown_locator_is_refused():
    frames, times = _render()

    with pytest.raises(ValueError, match="ball_locator"):
        _estimate(frames, times, locator="magic")


def test_without_a_zone_the_search_centres_on_the_session_tee_anchor():
    frames, times = _render()
    tracker = ReferenceBallTracker()
    for jitter in (-1.0, 0.0, 1.0):
        tracker.resolve(ReferenceBall(REST_X + 8.0 + jitter, REST_Y - 3.0, 15.0, 177))

    result = _estimate(frames, times, tee_zone=None, ball_tracker=tracker)

    assert result.locator.search_box_source == "session_anchor"
    assert result.locator.ball_x == pytest.approx(REST_X, abs=0.7)
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)


def test_diagnostics_serialise_for_the_session_log_without_the_wall_time():
    frames, times = _render()

    result = _estimate(frames, times)
    data = result.locator.to_dict()

    assert result.locator.elapsed_ms > 0.0
    assert "elapsed_ms" not in data
    assert data["search_box"] == [ZONE.x0, ZONE.y0, ZONE.x1, ZONE.y1]
    assert set(data["line_fit"]) == {
        "horizontal_deg",
        "vertical_deg",
        "time_zero_ms",
        "rms_mrad",
        "max_mrad",
        "n_points",
    }
    json.dumps(data)


# --- ball mark (the resting ball tapped in the Align view) ------------------------------

# 2026-10-25: the session anchor sat on a dark tree crown at 313.6/169.3 for 33
# minutes while the balls lay at x 358-378, y 251-269: ~55 px left, ~90 px up.
TREE_CROWN = (REST_X - 55.0, REST_Y - 90.0)


def _tree_crown_scene(flight=(4.0, 20.0)):
    """A clip with a static dark tree crown and a session tracker anchored on it."""
    frames, times = _render(flight=flight, clutter=((*TREE_CROWN, REST_D, 30),))
    tracker = ReferenceBallTracker()
    for jitter in (-0.5, 0.0, 0.5):
        tracker.resolve(ReferenceBall(TREE_CROWN[0] + jitter, TREE_CROWN[1], REST_D, 143))
    assert tracker.fallback() is not None
    return frames, times, tracker


def test_mark_window_is_a_square_around_the_tap_clipped_to_the_frame():
    assert MARK_WINDOW_HALF_PX == 64
    assert mark_window((HEIGHT, WIDTH), (300.0, 250.0)) == (236, 186, 365, 315)
    assert mark_window((HEIGHT, WIDTH), (10.2, 395.0)) == (0, 331, 76, 400)
    assert mark_window((HEIGHT, WIDTH), (-80.0, 200.0)) is None
    assert in_window((236, 186, 365, 315), ReferenceBall(300.0, 250.0, 14.0, 154))
    assert not in_window((236, 186, 365, 315), ReferenceBall(365.0, 250.0, 14.0, 154))
    # The 2026-10-25 tree crown, ~55 px left of and ~90 px above the balls, stays out.
    assert not in_window(
        mark_window((HEIGHT, WIDTH), (368.0, 260.0)), ReferenceBall(313.6, 169.3, 17.0, 227)
    )


def test_search_box_with_a_mark_is_its_window_whatever_the_zone():
    window = mark_window((HEIGHT, WIDTH), (300.0, 250.0))
    estimate = ReferenceBall(300.0, 250.0, 14.0, 154)

    assert search_box((HEIGHT, WIDTH), zone=ZONE, tee_estimate=None, window=window) == (
        window,
        ZONE.diameter_px,
    )
    assert search_box((HEIGHT, WIDTH), zone=None, tee_estimate=estimate, window=window) == (
        window,
        14.0,
    )
    assert search_box((HEIGHT, WIDTH), zone=None, tee_estimate=None, window=window) is None


def test_a_marked_ball_is_found_where_it_lies_inside_the_window():
    """The tap is 20 px off the ball: the locator still measures this shot's ball."""
    frames, times = _render()
    anchor = GroundBallAnchor()
    mark = (REST_X + 16.0, REST_Y - 12.0)

    result = _estimate(frames, times, ball_mark_px=mark, ground_anchor=anchor)

    assert result.locator.search_box_source == "ball_mark"
    assert result.locator.search_box == mark_window((HEIGHT, WIDTH), mark)
    assert result.locator.ball_mark == mark
    assert result.locator.ball_x == pytest.approx(REST_X, abs=0.7)
    assert result.locator.ball_y == pytest.approx(REST_Y, abs=0.7)
    assert anchor.last.x == pytest.approx(REST_X, abs=0.7)
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert result.locator.to_dict()["ball_mark"] == [round(mark[0], 3), round(mark[1], 3)]


def test_a_mark_changes_no_direction_the_zone_search_measured():
    frames, times = _render()

    zone_only = _estimate(frames, times)
    marked = _estimate(frames, times, ball_mark_px=(REST_X - 6.0, REST_Y + 4.0))

    assert marked.status == zone_only.status == "accepted"
    assert marked.locator.ball_x == pytest.approx(zone_only.locator.ball_x, abs=1e-6)
    assert marked.locator.pose_x == pytest.approx(zone_only.locator.pose_x, abs=1e-6)
    assert marked.horizontal_deg == pytest.approx(zone_only.horizontal_deg, abs=1e-6)
    assert marked.vertical_deg == pytest.approx(zone_only.vertical_deg, abs=1e-6)


def test_a_tree_crown_session_anchor_cannot_place_a_marked_ball():
    """Regression 2026-10-25: without a zone the search followed the tree-crown anchor."""
    frames, times, tracker = _tree_crown_scene()

    unmarked = _estimate(frames, times, tee_zone=None, ball_tracker=copy.deepcopy(tracker))
    marked = _estimate(
        frames, times, tee_zone=None, ball_tracker=tracker, ball_mark_px=(REST_X + 8, REST_Y)
    )

    # Without a mark the search box sat on the crown and the shot lost its direction.
    assert unmarked.locator.search_box_source == "session_anchor"
    assert unmarked.horizontal_deg is None
    assert marked.locator.search_box_source == "ball_mark"
    assert (marked.locator.locator, marked.locator.reason) == ("disappeared", "located")
    assert marked.locator.ball_x == pytest.approx(REST_X, abs=0.7)
    assert marked.locator.pose_x == pytest.approx(REST_X, abs=0.7)
    assert marked.horizontal_deg == pytest.approx(4.0, abs=0.5)


@pytest.mark.parametrize("locator", ["disappeared", "legacy"])
def test_the_legacy_locator_never_falls_back_to_a_tree_crown_outside_the_mark(locator):
    """No ball in the clip: the session anchor on the crown must not stand in for it."""
    frames, times, tracker = _tree_crown_scene(flight=False)

    unmarked = _estimate(
        frames, times, locator=locator, tee_zone=None, ball_tracker=copy.deepcopy(tracker)
    )
    marked = _estimate(
        frames,
        times,
        locator=locator,
        tee_zone=None,
        ball_tracker=tracker,
        ball_mark_px=(REST_X, REST_Y),
    )

    # Unmarked, the legacy path resolves the crown as the ball and only its
    # missing departure stops it.
    assert unmarked.status == "rejected_no_ball_departure"
    assert marked.status == "rejected_reference_ball_not_found"
    assert marked.horizontal_deg is None


def test_the_legacy_locator_measures_the_marked_ball_not_the_tree_crown():
    frames, times, tracker = _tree_crown_scene()

    unmarked = _estimate(
        frames, times, locator="legacy", tee_zone=None, ball_tracker=copy.deepcopy(tracker)
    )
    marked = _estimate(
        frames,
        times,
        locator="legacy",
        tee_zone=None,
        ball_tracker=tracker,
        ball_mark_px=(REST_X, REST_Y),
    )

    assert unmarked.horizontal_deg is None
    assert marked.horizontal_deg == pytest.approx(4.0, abs=0.5)


def test_a_ground_anchor_learned_away_from_the_mark_never_sets_the_pose():
    frames, times = _render()
    anchor = GroundBallAnchor()
    for jitter in (-1.0, 0.0, 1.0):
        anchor.observe(_departed(TREE_CROWN[0] + jitter, TREE_CROWN[1]))

    unmarked = _estimate(frames, times, ground_anchor=copy.deepcopy(anchor))
    marked = _estimate(frames, times, ground_anchor=anchor, ball_mark_px=(REST_X, REST_Y))

    assert unmarked.locator.pose_source == "ground_anchor"
    assert unmarked.locator.pose_x == pytest.approx(TREE_CROWN[0], abs=1.5)
    assert marked.locator.pose_source == "this_ball"
    assert marked.locator.pose_x == pytest.approx(REST_X, abs=0.7)
    assert marked.horizontal_deg == pytest.approx(4.0, abs=0.5)


def test_a_ball_outside_the_window_is_not_searched():
    frames, times = _render()

    result = _estimate(frames, times, ball_mark_px=(REST_X + 120.0, REST_Y))

    assert result.horizontal_deg is None
    assert result.locator.search_box_source == "ball_mark"


def test_a_mark_off_the_frame_is_refused():
    frames, times = _render()

    result = _estimate(frames, times, ball_mark_px=(-200.0, -200.0))

    assert result.status == "rejected_ball_mark_off_frame"
    assert result.locator.reason == "ball_mark_off_frame"
    json.dumps(result.locator.to_dict(), allow_nan=False)


def test_the_low_light_path_bounds_its_search_by_the_mark():
    frames, times = _render()
    mark = (REST_X + 5.0, REST_Y)

    result = estimate_lowlight_ball_flight(
        frames=frames,
        timestamps_ns=times,
        relaxed_only=True,
        ball_locator="disappeared",
        tee_zone=ZONE,
        trigger_ns=_trigger_ns(),
        range_evidence=_evidence(),
        geometry=GEOMETRY,
        ops_ball_speed_mph=SPEED_MS * MPH_PER_MS,
        ball_mark_px=mark,
    )

    assert result.estimate.locator.search_box_source == "ball_mark"
    assert result.estimate.locator.ball_mark == mark
    assert result.estimate.horizontal_deg == pytest.approx(4.0, abs=0.5)


# --- server wiring ---------------------------------------------------------------------


def test_server_passes_the_locator_state_and_logs_its_diagnostics(monkeypatch, tmp_path):
    calls = []
    diagnostics = BallLocatorDiagnostics(
        "disappeared", "located", ball_x=360.0, ball_y=252.0, contrast=31.5, snr=40.2
    )
    monkeypatch.setattr(
        ball_flight_module,
        "estimate_camera_ball_flight",
        lambda *_args, **kwargs: (
            calls.append(kwargs)
            or CameraBallEstimate(
                status="accepted", confidence_tier="high", horizontal_deg=-2.5, locator=diagnostics
            )
        ),
    )
    calibration = SimpleNamespace(tee_range_m=1.5, radar_height_m=0.15, tee_ball_height_m=0.02)
    monkeypatch.setattr(server, "iwr6843_runtime", SimpleNamespace(calibration=calibration))
    monkeypatch.setattr(
        server,
        "camera_capture_config",
        {"mount_height_m": 0.21, "width": 640, "height": 400, "ball_locator": "legacy"},
    )
    anchor = GroundBallAnchor()
    monkeypatch.setattr(server, "camera_ball_flight_ground_anchor", anchor)
    monkeypatch.setattr(
        server, "camera_capture_runtime", SimpleNamespace(active_tee_zone=lambda: ZONE)
    )
    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=np.arange(8, dtype=np.int64),
        trigger_host_timestamp_ns=np.int64(3),
    )
    shot = Shot(ball_speed_mph=86.0, timestamp=datetime.now())

    server._fuse_camera_ball_flight(shot, SimpleNamespace(valid=True, path=tmp_path))
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session()
    logger.log_shot(shot)
    entry = json.loads(logger.session_path.read_text().strip().split("\n")[-1])

    assert calls[0]["ball_locator"] == "legacy"
    assert calls[0]["ground_anchor"] is anchor
    assert calls[0]["tee_zone"] == ZONE
    assert calls[0]["ball_mark_px"] is None
    assert calls[0]["legacy_fallback"] is True
    logged = entry["experimental_camera_ball_locator"]
    # The live job ran the flicker step first (a 4x4 black clip: nothing to measure).
    assert logged.pop("flicker")["reason"] == "no_lit_pixels"
    assert {**logged, "flicker": None} == diagnostics.to_dict()
    assert logged["snr"] == 40.2


@pytest.mark.parametrize(
    ("post_ms", "locator", "warned"),
    [
        (50.0, "disappeared", True),
        (79.0, "disappeared", True),
        (101.0, "disappeared", False),
        (50.0, "legacy", False),
    ],
)
def test_camera_init_warns_when_clips_end_before_the_locator_s_late_window(
    monkeypatch, tmp_path, caplog, post_ms, locator, warned
):
    """288 fps: 50 ms (the CLI default) is 15 post frames, the locator needs 29 (101 ms).

    79 ms (23 frames) held the late window only when the trigger frame landed
    right after the pre-trigger count; 2026-10-25 it landed up to 6 frames later.
    """
    with caplog.at_level("WARNING", logger=server.logger.name):
        _init_camera(monkeypatch, tmp_path, post_ms=post_ms, ball_locator=locator)

    warnings = [record.getMessage() for record in caplog.records if "[CAMERA]" in record.message]
    assert bool(warnings) == warned
    if warned:
        assert "--camera-capture-post-ms 101 " in warnings[0]
        assert f"{math.ceil(post_ms * 288.0 / 1000.0)} post-trigger frames" in warnings[0]
    assert MIN_CLIP_POST_FRAMES == POST_WINDOW_FRAMES[0] + MIN_POST_FRAMES + TRIGGER_LAG_FRAMES
    assert min_post_ms(288.0) == 101
    assert math.ceil(min_post_ms(288.0) * 288.0 / 1000.0) >= MIN_CLIP_POST_FRAMES
