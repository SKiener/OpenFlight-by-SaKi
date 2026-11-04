"""The impact point measured across contact (method D) on synthetic rear-camera clips.

The clips run past contact: the head keeps moving (slower) after it, the ball leaves
at its launch speed and angle, and the clip lasts long enough for the late frames
(the mat without the ball) the measurement needs.
"""

import inspect
import math
from dataclasses import dataclass, replace

import numpy as np
import pytest
from test_club_head_path import (
    CAMERA_ORIGIN,
    CLUB_SPEED_MPH,
    DARK_ON_BRIGHT,
    FOCAL_PX,
    FRAME_S,
    GEOMETRY,
    HEIGHT,
    SPEED_MS,
    WIDTH,
    WOOD_SCENE,
    Camera,
    Scenario,
    _paint_disk,
    _paint_head,
    _paint_polygon,
)

import openflight.camera.club_head_path as chp
import openflight.camera.impact_point as ip
from openflight.camera.club_head_path import HEAD_BEHIND_BALL_M, estimate_club_head_path
from openflight.camera.club_motion import ReferenceBall
from openflight.clubs import ClubType

FRAMES = 100
CONTACT_FRAME = 44
BALL_SPEED_MPH = 100.0
LAUNCH_DEG = 18.0
TOE_MM = 12.0


@dataclass(frozen=True)
class Strike:
    """What a synthetic strike shows; offsets in metres, the head's sole centre at contact."""

    ball_lateral_m: float = 0.0
    ball_forward_m: float = GEOMETRY.ball_forward_m  # where the ball really is
    ball_height_m: float = GEOMETRY.ball_height_m  # higher for a teed ball
    head_behind_ball_m: float = HEAD_BEHIND_BALL_M  # the head plane behind the ball centre
    head_lateral_speed: float = 0.0  # m/s (= mm/ms) at contact
    head_lateral_curvature: float = 0.0  # m/s^2
    head_lateral_jerk: float = 0.0  # m/s^3
    contact_after_frame_s: float = 0.0015
    left_handed: bool = False
    head_after_contact: bool = True
    ball_leaves: bool = True
    wood: bool = False
    ball_size_scale: float = 1.0  # painted ball size against a regulation ball's
    seed: int = 5


def head_centre(tau_s: float, strike: Strike) -> np.ndarray:
    """The head's sole centre: club speed before contact, 0.78 of it after."""
    travel = SPEED_MS * tau_s if tau_s < 0.0 else 0.78 * SPEED_MS * tau_s
    lateral = (
        strike.head_lateral_speed * tau_s
        + 0.5 * strike.head_lateral_curvature * tau_s**2
        + strike.head_lateral_jerk * tau_s**3
    )
    return np.array(
        [
            lateral,
            strike.ball_forward_m - strike.head_behind_ball_m + travel,
            0.002 + 400.0 * tau_s**2,
        ]
    )


def render_strike(strike: Strike = Strike()):
    """Frames, timestamps (ns), contact time (s) and the resting ball of one strike."""
    camera = Camera()
    palette = WOOD_SCENE if strike.wood else DARK_ON_BRIGHT
    painter = Scenario(camera=camera, left_handed=strike.left_handed, two_tone=strike.wood)
    rng = np.random.default_rng(strike.seed)
    times = np.arange(FRAMES) * FRAME_S
    contact = float(times[CONTACT_FRAME] + strike.contact_after_frame_s)
    ball_centre = np.array([strike.ball_lateral_m, strike.ball_forward_m, strike.ball_height_m])
    ball_px = camera.project(ball_centre)[0]
    radius_px = (
        strike.ball_size_scale
        * FOCAL_PX
        * 0.04267
        / 2.0
        / float(np.linalg.norm(ball_centre - CAMERA_ORIGIN))
    )
    horizon_row = int(ball_px[1] - 3.0 * radius_px)
    empty = np.full((HEIGHT, WIDTH), palette["mat"])
    empty[:horizon_row] = palette["sky"]
    empty[horizon_row:] += rng.normal(0.0, 6.0, (HEIGHT, WIDTH))[horizon_row:]
    resting = empty.copy()
    _paint_disk(resting, ball_px, radius_px, palette["ball"])
    launch = math.radians(LAUNCH_DEG)
    speed = BALL_SPEED_MPH / 2.23694
    frames = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index, time_s in enumerate(times):
        tau = time_s - contact
        image = (resting if tau <= 0.0 or not strike.ball_leaves else empty).copy()
        leg_x = ball_px[0] - 165.0 + 0.6 * index
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
        if tau > 0.0 and strike.ball_leaves:
            flying = ball_centre + speed * tau * np.array([0.0, math.cos(launch), math.sin(launch)])
            centre_px = camera.project(flying)[0]
            radius = (
                strike.ball_size_scale
                * FOCAL_PX
                * 0.04267
                / 2.0
                / float(np.linalg.norm(flying - CAMERA_ORIGIN))
            )
            if 0.0 < centre_px[0] < WIDTH and 0.0 < centre_px[1] < HEIGHT:
                _paint_disk(image, centre_px, radius, palette["ball"])
        if -0.025 <= tau <= (0.010 if strike.head_after_contact else 0.0):
            _paint_head(image, head_centre(tau, strike), painter)
        image += rng.normal(0.0, 2.5, image.shape)
        frames[index] = np.clip(np.round(image), 0, 255).astype(np.uint8)
    ball = ReferenceBall(
        float(ball_px[0]), float(ball_px[1]), 2.0 * radius_px, int(math.pi * radius_px**2)
    )
    return frames, (times * 1e9).astype(np.int64), contact, ball


def run(clip, *, club=ClubType.IRON_8, **overrides):
    """estimate_club_head_path on a strike, against its resting ball as the departed one."""
    frames, timestamps, contact, ball = clip
    kwargs = {
        "contact_time_s": contact,
        "geometry": GEOMETRY,
        "club_speed_mph": CLUB_SPEED_MPH,
        "reference_ball": ball,
        "departed_ball": ball,
        "club": club,
        "attack": False,
        "ball_speed_mph": BALL_SPEED_MPH,
        "launch_vertical_deg": LAUNCH_DEG,
        "launch_horizontal_deg": 0.0,
    }
    kwargs.update(overrides)
    return estimate_club_head_path(frames, timestamps, **kwargs)


@pytest.fixture(name="toe_strike", scope="module")
def fixture_toe_strike():
    """A right-handed 8-iron strike with the ball 12 mm toe-ward of the head's midpoint."""
    return render_strike(Strike(ball_lateral_m=TOE_MM / 1e3))


@pytest.fixture(name="far_strike", scope="module")
def fixture_far_strike():
    """The ball 1.73 m from the camera while the geometry assumes 1.5 m (2026-10-25)."""
    return render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, ball_forward_m=1.73))


def _expected_ball_px(clip) -> float:
    """The scene's expected ball diameter: the regulation ball at the known range."""
    frames, timestamps, contact, ball = clip
    return chp._build_scene(
        frames.shape[1:],
        (timestamps / 1e9, contact),
        (ball, ball),
        GEOMETRY,
        CLUB_SPEED_MPH / chp.MPH_PER_MS,
    ).ball_px


# ----------------------------------------------------------------------------- silhouettes


def test_a_toe_strike_reads_toe_interpolated_across_contact(toe_strike):
    point = run(toe_strike).impact_point

    assert point.status == "accepted"
    assert point.method == "interpolated"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)
    assert point.post_status == {"1": "ok", "2": "ok"}
    assert point.ball_reference == "given"
    assert point.frame_l == CONTACT_FRAME
    assert point.tau_l_ms == pytest.approx(-1.5, abs=0.01)
    assert point.tau_p_ms == pytest.approx(FRAME_S * 1e3 - 1.5, abs=0.01)
    assert point.hosel_vote < 0.0  # the shaft leaves the head on its image-left side
    assert 60.0 <= point.head_length_mm <= 90.0  # the 74 mm synthetic head
    assert 1.5 <= point.sigma_mm <= 5.0
    assert set(point.sigma_parts) == set(ip.SIGMA_PARTS)
    assert point.sigma_mm == pytest.approx(
        math.sqrt(sum(value**2 for value in point.sigma_parts.values())), abs=0.05
    )


@pytest.mark.parametrize("lateral_mm", [-18.0, 0.0])
def test_heel_and_centre_strikes_read_their_offset(lateral_mm):
    point = run(render_strike(Strike(ball_lateral_m=lateral_mm / 1e3))).impact_point

    assert point.status == "accepted"
    assert point.offset_mm == pytest.approx(lateral_mm, abs=1.5)


def test_a_mirrored_left_handed_toe_strike_reads_toe():
    clip = render_strike(Strike(ball_lateral_m=-TOE_MM / 1e3, left_handed=True))

    point = run(clip, handedness="left").impact_point

    assert point.status == "accepted"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)
    assert point.hosel_vote > 0.0


def test_a_two_tone_wood_head_is_measured_in_woods_segmentation():
    driver = ip.CLUB_MODELS["driver"].silhouette_behind_ball_m
    clip = render_strike(Strike(ball_lateral_m=-TOE_MM / 1e3, wood=True, head_behind_ball_m=driver))

    result = run(clip, club=ClubType.DRIVER)

    assert result.segmentation == "woods"
    assert result.impact_point.polarity == "abs"
    assert result.impact_point.accepted
    assert result.impact_point.offset_mm == pytest.approx(-TOE_MM, abs=2.0)


def test_a_clip_without_a_head_is_rejected():
    frames, timestamps, contact, ball = render_strike()
    empty = np.repeat(frames[:1], len(frames), axis=0)

    point = run((empty, timestamps, contact, ball)).impact_point

    assert point.status == "rejected_head_not_found"
    assert point.offset_mm is None


# ----------------------------------------------------------------------------- across contact


@pytest.mark.parametrize("after_s", [0.0003, 0.0017, 0.0025])
def test_interpolation_follows_a_curving_head_wherever_contact_falls(after_s):
    """The head path bends hard at the low point; L and P bracket contact at any phase."""
    strike = Strike(
        ball_lateral_m=TOE_MM / 1e3,
        head_lateral_speed=1.9,
        head_lateral_curvature=600.0,
        head_lateral_jerk=60000.0,
        contact_after_frame_s=after_s,
    )

    point = run(render_strike(strike)).impact_point

    assert point.method == "interpolated"
    assert point.tau_l_ms == pytest.approx(-after_s * 1e3, abs=0.01)
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)
    assert point.sigma_parts["curvature"] > 0.0


def test_contact_value_interpolates_l_and_p_and_keeps_the_pre_contact_quadratic():
    tau = {40: -8.444, 41: -4.97, 42: -1.496, 43: 1.978, 44: 5.452}
    values = {k: 0.01 + 0.002 * t + 0.0003 * t**2 for k, t in tau.items()}

    out = ip._contact_value(values, tau, last=42)

    weight = 1.496 / (1.978 + 1.496)
    assert out["weight_p"] == pytest.approx(weight)
    assert out["int"] == pytest.approx(values[42] + weight * (values[43] - values[42]))
    assert out["pre_quad"] == pytest.approx(0.01, abs=1e-12)  # exact on a quadratic
    assert out["chord_speed"] == pytest.approx((values[43] - values[42]) / (1.978 + 1.496))
    assert out["raw_l"] == values[42]
    assert out["second_diff_l"] == pytest.approx(values[41] - 2 * values[42] + values[43])
    assert len(out["third_diffs"]) == 2


def test_contact_value_without_the_frame_after_contact_has_no_interpolation():
    tau = {40: -8.0, 41: -4.5, 42: -1.0}
    out = ip._contact_value({k: 0.002 * t for k, t in tau.items()}, tau, last=42)

    assert "int" not in out and "second_diff_l" not in out
    assert out["pre_quad"] == pytest.approx(0.0, abs=1e-12)
    assert out["pre_speed"] == pytest.approx(0.002)


def test_a_head_missing_after_contact_falls_back_to_the_frames_before(toe_strike):
    clip = render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, head_after_contact=False))

    point = run(clip).impact_point
    interpolated = run(toe_strike).impact_point

    assert point.post_status["1"] != "ok"
    assert point.status == "accepted_pre_contact"
    assert point.method == "pre_contact"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=2.0)
    assert point.sigma_parts["noise"] > interpolated.sigma_parts["noise"]


def test_a_frame_just_after_contact_whose_blank_covers_the_toe_side_is_not_used():
    """At 0.3 ms after contact the departing ball's blank still hides the toe half of the
    head, so P keeps only its heel half; its width jump keeps it out (as in the study)."""
    clip = render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, contact_after_frame_s=0.0032))

    point = run(clip).impact_point

    assert point.post_status["1"] == "width_jump"
    assert point.method == "pre_contact"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=2.0)


def test_without_a_ball_speed_only_the_frames_before_contact_count(toe_strike):
    point = run(toe_strike, ball_speed_mph=None).impact_point

    assert point.post_status == {"1": "no_ball_launch", "2": "no_ball_launch"}
    assert point.method == "pre_contact"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=2.0)


def test_the_departing_ball_disk_follows_its_launch(toe_strike):
    frames, timestamps, contact, ball = toe_strike
    scene = chp._build_scene(
        frames.shape[1:],
        (timestamps / 1e9, contact),
        (ball, ball),
        GEOMETRY,
        CLUB_SPEED_MPH / chp.MPH_PER_MS,
    )
    height, width = scene.ground_forward_m.shape
    x0, y0 = scene.origin
    clip = ip._Clip(
        frames,
        scene,
        (slice(y0, y0 + height), slice(x0, x0 + width)),
        np.arange(10),
        False,
        True,
    )
    ball_frame = ip._ball_frame(scene, ball, ball.diameter_px)
    blank = ip._PostContact(clip, "dark", ball_frame, ip.BallLaunch(BALL_SPEED_MPH, LAUNCH_DEG))

    at_rest, later = blank.departing_ball(0.0), blank.departing_ball(3.0)

    rows, cols = np.nonzero(at_rest)
    assert cols.mean() + x0 == pytest.approx(ball.x, abs=1.0)
    assert rows.mean() + y0 == pytest.approx(ball.y, abs=1.0)
    assert np.nonzero(later)[0].mean() + y0 < ball.y - 3.0  # flown up the image
    assert later.sum() < at_rest.sum()  # and away from the camera


# ----------------------------------------------------------------------------- scale


def _configured_range_m() -> float:
    return math.sqrt(
        GEOMETRY.camera_lateral_offset_m**2
        + GEOMETRY.camera_ball_forward_m**2
        + (GEOMETRY.ball_height_m - GEOMETRY.camera_height_m) ** 2
    )


@pytest.mark.parametrize("ball_size_scale", [0.75, 1.25])
def test_the_scale_is_the_geometry_s_whatever_size_the_ball_shows(ball_size_scale):
    """A crisp ball painted 25 % small (the 12-15 px reads) or large sets no scale."""
    clip = render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, ball_size_scale=ball_size_scale))
    regulation = run(render_strike(Strike(ball_lateral_m=TOE_MM / 1e3))).impact_point
    expected = FOCAL_PX * 0.04267 / _configured_range_m()
    assert clip[3].diameter_px == pytest.approx(ball_size_scale * expected, rel=0.02)

    point = run(clip).impact_point

    assert point.scale_source == ip.SCALE_SOURCE == "geometry"
    assert point.ball_diameter_px == pytest.approx(_expected_ball_px(clip), abs=0.01)
    assert point.ball_diameter_px == pytest.approx(expected, rel=0.01)
    assert point.ball_distance_m == pytest.approx(_configured_range_m(), rel=0.02)
    assert point.e_scale_ratio == pytest.approx(1.0, abs=0.02)
    assert point.scale_mm_per_px == pytest.approx(regulation.scale_mm_per_px, abs=0.001)
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)


def test_a_ball_farther_than_configured_is_still_read_at_the_known_range(far_strike):
    """The owner keeps the ball at the measured 1.50 m: a farther ball is not re-ranged.

    Its apparent size (14.2 px, as 2026-10-25 read) is ignored; D then reads its
    millimetres at the configured distance, like E, and both short by 1.5/1.73.
    """
    result = run(far_strike)
    point = result.impact_point

    assert point.scale_source == "geometry"
    assert point.ball_diameter_px == pytest.approx(_expected_ball_px(far_strike), abs=0.01)
    assert point.ball_distance_m == pytest.approx(_configured_range_m(), rel=0.02)
    assert point.e_scale_ratio == pytest.approx(1.0, abs=0.02)
    assert point.offset_mm == pytest.approx(TOE_MM * 1.5 / 1.73, abs=1.5)
    assert result.impact.toe_mm == pytest.approx(point.offset_mm, abs=1.5)


def test_a_ball_that_never_leaves_has_the_same_geometric_scale(toe_strike):
    clip = render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, ball_leaves=False))

    point = run(clip).impact_point
    left = run(toe_strike).impact_point

    assert point.scale_source == "geometry"
    assert point.ball_diameter_px == pytest.approx(left.ball_diameter_px, abs=0.01)
    assert point.scale_mm_per_px == pytest.approx(left.scale_mm_per_px, abs=0.001)


def test_no_ball_size_is_measured_or_taken_from_the_session(toe_strike):
    for name in (
        "measure_ball_diameter",
        "ball_diameter_plausible",
        "resolve_ball_diameter",
        "BALL_DIAMETER_RANGE",
        "BALL_DIAMETER_MEDIAN_TOLERANCE",
        "BALL_SCALE_SOURCES",
    ):
        assert not hasattr(ip, name), name
    for function in (ip.measure_impact_point, estimate_club_head_path, chp.estimate_club_data):
        assert "ball_diameter_median_px" not in inspect.signature(function).parameters
    with pytest.raises(TypeError):
        run(toe_strike, ball_diameter_median_px=15.5)


def test_the_log_says_geometry_and_has_no_measured_diameter(toe_strike):
    logged = run(toe_strike).impact_point.to_dict()

    assert logged["scale_source"] == "geometry"
    assert "ball_diameter_measured_px" not in logged
    assert logged["ball_diameter_px"] == pytest.approx(_expected_ball_px(toe_strike), abs=0.01)


# ----------------------------------------------------------------------------- height


@pytest.mark.parametrize("ball_height_m", [GEOMETRY.ball_height_m, 0.045])
def test_the_height_is_the_ball_centre_above_the_head_sole(ball_height_m):
    """A ball on the mat sits a radius above the sole, a teed one higher."""
    point = run(
        render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, ball_height_m=ball_height_m))
    ).impact_point

    assert point.accepted
    assert point.height_mm == pytest.approx((ball_height_m - 0.002) * 1e3, abs=3.0)
    assert point.head_height_mm == pytest.approx(48.0, abs=6.0)  # the toe-side crown
    assert point.height_sigma_mm == pytest.approx(
        math.sqrt((1.03 * 0.75) ** 2 + 2.0**2 + (100.0 * math.sin(math.radians(1.0))) ** 2),
        abs=0.01,
    )


# ----------------------------------------------------------------------------- gates


def test_a_head_length_off_the_session_median_is_rejected(toe_strike):
    point = run(toe_strike, impact_point_head_length_median_mm=120.0).impact_point

    assert point.status == "rejected_head_length"
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)  # measured, only gated


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({}, None),
        ({"head_length_mm": 40.0}, "rejected_head_length"),
        ({"head_length_mm": 130.0}, "rejected_head_length"),
        ({"head_length_median_mm": 107.0}, "rejected_head_length"),  # 80 is -25.2 %
        ({"width_ratio": 1.25}, "rejected_width_jump"),
        ({"offset_mm": -50.5}, "rejected_implausible_offset"),
        ({"sigma_mm": 10.5}, "rejected_sigma_too_large"),
        ({"offset_mm": math.nan}, "rejected_implausible_offset"),
        ({"sigma_mm": math.nan}, "rejected_sigma_too_large"),
    ],
)
def test_the_gates(overrides, status):
    kwargs = {
        "offset_mm": 12.0,
        "sigma_mm": 3.0,
        "head_length_mm": 80.0,
        "width_ratio": 1.05,
        "head_length_median_mm": 79.0,
    }
    kwargs.update(overrides)

    assert ip._gate(**kwargs) == status


def test_club_models_group_the_clubs():
    assert ip.club_model(ClubType.DRIVER).group == "driver"
    assert ip.club_model(ClubType.WOOD_7).group == "wood"
    assert ip.club_model(ClubType.HYBRID_5).group == "wood"
    assert ip.club_model(ClubType.PW).group == "wedge"
    assert ip.club_model(ClubType.IRON_8).group == "iron"
    assert ip.club_model(None).group == "iron"
    assert ip.CLUB_MODELS["driver"].silhouette_behind_ball_m == pytest.approx(0.0713)


def test_a_failing_measurement_leaves_the_path_and_e_intact(toe_strike, monkeypatch):
    def broken(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(ip, "measure_impact_point", broken)

    result = run(toe_strike)

    assert result.impact_point.status == "error"
    assert result.impact_point.error == "ValueError"
    assert result.impact.status == "accepted"
    assert result.path_deg is not None


def test_the_impact_point_can_be_left_out(toe_strike):
    timings = {}

    result = run(toe_strike, impact_point=False, timings=timings)

    assert result.impact_point is None
    assert timings["impact_point"] == 0.0
    assert result.impact.status == "accepted"


def test_the_log_form_is_json_ready(toe_strike):
    import json  # noqa: PLC0415

    point = run(toe_strike).impact_point
    logged = point.to_dict()

    assert json.loads(json.dumps(logged)) == logged
    assert logged["status"] == "accepted" and logged["ball_px"] == list(point.ball_px)
    assert replace(point, status="x").to_dict()["status"] == "x"


def test_the_path_s_head_track_is_reused_when_it_ends_in_frame_l(toe_strike, monkeypatch):
    frames, timestamps, contact, ball = toe_strike
    fresh = run(toe_strike).impact_point
    scene = chp._build_scene(
        frames.shape[1:],
        (timestamps / 1e9, contact),
        (ball, ball),
        GEOMETRY,
        CLUB_SPEED_MPH / chp.MPH_PER_MS,
    )
    alone = ip.measure_impact_point(
        frames,
        scene,
        club=ClubType.IRON_8,
        woods=False,
        contact_sigma_s=0.0,
        ball=(ball, "given"),
        launch=ip.BallLaunch(BALL_SPEED_MPH, LAUNCH_DEG, 0.0),
    )
    assert alone == fresh  # reusing the path's track changes nothing

    def no_segmentation(*_args, **_kwargs):
        raise AssertionError("segmented again")

    monkeypatch.setattr(ip, "_segment", no_segmentation)
    assert run(toe_strike).impact_point == fresh


def test_a_contact_inside_the_path_s_guard_segments_its_own_track(monkeypatch):
    """0.1 ms after frame 44 the path stops at 43 (0.2 ms guard); D needs 44."""
    clip = render_strike(Strike(ball_lateral_m=TOE_MM / 1e3, contact_after_frame_s=0.0001))
    calls = []
    segment = ip._segment

    def counting(*args, **kwargs):
        calls.append(tuple(int(index) for index in args[1]))
        return segment(*args, **kwargs)

    monkeypatch.setattr(ip, "_segment", counting)

    point = run(clip).impact_point

    assert calls == [tuple(range(CONTACT_FRAME - 5, CONTACT_FRAME + 1))]
    assert point.frame_l == CONTACT_FRAME and point.accepted
    assert point.offset_mm == pytest.approx(TOE_MM, abs=1.5)
