"""Synthetic-clip tests for the observation-only camera trigger check."""

import dataclasses
import json
import math
from collections import namedtuple

import numpy as np
import pytest
from scipy import ndimage

from openflight.camera import capture_runtime, trigger_check
from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings
from openflight.camera.club_motion import ReferenceBall, detect_reference_ball
from openflight.camera.tee_anchor import TeeAnchor
from openflight.camera.tee_zone import TeeZone
from openflight.camera.trigger_check import (
    AnchorBall,
    LiveDepartureCheck,
    TriggerCheck,
    check_ball_departure,
    exposure_timestamps_ns,
    find_ball_near,
    normalised_contrast,
    spot_matches,
)
from openflight.camera.triggered_buffer import CameraFrame, TriggeredCapture

HEIGHT, WIDTH = 200, 320
FRAMES, PRE = 60, 45
INTERVAL_NS = 3_472_000
BALL = (160.0, 130.0)
RADIUS = 7
TIMESTAMPS = np.arange(FRAMES, dtype=np.int64) * INTERVAL_NS + 1_000_000_000
TRIGGER_NS = int(TIMESTAMPS[PRE - 1] + 2_000_000)

SCENES = {
    "front_light": {"mat": 60, "ball": 220, "club": 25},
    "backlight": {"mat": 255, "ball": 40, "club": 15},
}


def ball_positions(motion: str) -> list:
    """Ball centre per frame, or None once it has left the image."""
    positions = []
    for index in range(FRAMES):
        step = index - (PRE - 1)
        if motion == "gone_early":
            positions.append(BALL if index < 34 else None)
        elif motion == "stays" or step <= 0:
            positions.append(BALL)
        elif motion == "fast":
            positions.append(None)
        elif motion == "chip":
            positions.append((BALL[0] + 3 * step, BALL[1] - 4 * step))
    return positions


def render_clip(scene: str, motion: str, *, club_over: bool = False, seed: int = 0) -> np.ndarray:
    """Clip with sensor noise, a teed ball and an optional club sweeping over it."""
    levels = SCENES[scene]
    rng = np.random.default_rng(seed)
    texture = rng.integers(-6, 7, size=(HEIGHT, WIDTH))
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index, position in enumerate(ball_positions(motion)):
        image = levels["mat"] + texture + rng.normal(0.0, 3.0, size=(HEIGHT, WIDTH))
        if position is not None:
            image[(xx - position[0]) ** 2 + (yy - position[1]) ** 2 <= RADIUS**2] = levels["ball"]
        if club_over and PRE - 3 <= index <= PRE + 1:
            left = int(BALL[0]) - 60 + 25 * (index - (PRE - 3))
            image[118:142, max(0, left) : left + 40] = levels["club"]
        clip[index] = np.clip(image, 0, 255).astype(np.uint8)
    return clip


def check(
    clip: np.ndarray, hint: ReferenceBall | None = None, zone: TeeZone | None = None
) -> TriggerCheck:
    return check_ball_departure(clip, TIMESTAMPS, TRIGGER_NS, hint, zone=zone)


# Hitting zone below the horizon band (rows < 95) and the far-off tree line.
ZONE = TeeZone(
    x0=110,
    y0=95,
    x1=230,
    y1=180,
    tee_x=165.0,
    tee_y=135.0,
    diameter_px=2 * RADIUS + 1,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)


def render_backlit_range(
    motion: str, *, ball=BALL, marks=(), seed: int = 0, frames: int = FRAMES
) -> np.ndarray:
    """2026-10-25 range look at AE 100 us/g2: a backlit ball darker than the mat.

    Mat ~50, bright grass above row 110, a ball core of ~20 with a bright top
    rim (contrast ~30 against its ring); ``marks`` are static dark spots of
    ball size (a tree crown, mat marks).
    """
    rng = np.random.default_rng(seed)
    texture = rng.integers(-6, 7, size=(HEIGHT, WIDTH))
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    clip = np.empty((frames, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(frames):
        image = 50.0 + texture + rng.normal(0.0, 3.0, size=(HEIGHT, WIDTH))
        image[yy < 110] = 110.0 + rng.normal(0.0, 3.0, size=(HEIGHT, WIDTH))[yy < 110]
        for mark in marks:
            image[(xx - mark[0]) ** 2 + (yy - mark[1]) ** 2 <= RADIUS**2] = 15.0
        if motion == "stays" or index < PRE:
            disk = (xx - ball[0]) ** 2 + (yy - ball[1]) ** 2 <= (RADIUS + 0.5) ** 2
            image[disk] = 20.0
            image[disk & (yy <= ball[1] - 6)] = 90.0
        clip[index] = np.clip(image, 0, 255).astype(np.uint8)
    return clip


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_fast_ball_departs(scene):
    result = check(render_clip(scene, "fast"))
    assert result.decision == "departed"
    assert result.reason == "ball_left_spot"
    assert 0.0 <= result.first_departure_ms_after_edge <= 10.0
    assert abs(result.ball_x - BALL[0]) <= 1.5 and abs(result.ball_y - BALL[1]) <= 1.5
    assert result.pre_contrast >= 100.0
    assert result.frames_used >= 10


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_slow_chip_departs_once_it_leaves_its_spot(scene):
    """5 px per frame: the ball needs a few frames to clear its own core disk."""
    fast = check(render_clip(scene, "fast"))
    result = check(render_clip(scene, "chip"))
    assert result.decision == "departed"
    assert fast.first_departure_ms_after_edge < result.first_departure_ms_after_edge <= 20.0


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_ball_stays_with_noise(scene):
    result = check(render_clip(scene, "stays", seed=3))
    assert result.decision == "still_there"
    assert result.reason == "ball_unchanged"
    assert result.first_departure_ms_after_edge is None


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_club_passing_over_a_staying_ball_is_not_a_departure(scene):
    """A waggle or practice swing covers the ball; it is visible again after.

    In backlight the dark club over the dark ball keeps the core disk dark, so
    the ball reads as present throughout; in front light the frames under the
    club are occluded.
    """
    result = check(render_clip(scene, "stays", club_over=True))
    assert result.decision == "still_there"
    assert result.first_departure_ms_after_edge is None
    if scene == "front_light":
        assert result.reason == "ball_visible_after_occlusion"
        assert "O" in result.frame_states
        assert "G" not in result.frame_states


@pytest.mark.parametrize("scene", sorted(SCENES))
def test_club_over_a_departing_ball_still_departs(scene):
    assert check(render_clip(scene, "fast", club_over=True)).decision == "departed"


def test_backlight_ball_reports_clipped_surroundings():
    result = check(render_clip("backlight", "fast"))
    assert result.clipped_fraction > 0.5
    assert check(render_clip("front_light", "fast")).clipped_fraction == 0.0


def test_too_dark_is_unclear():
    clip = (render_clip("front_light", "fast").astype(np.float32) * 0.08).astype(np.uint8)
    result = check(clip)
    assert result.decision == "unclear"
    assert result.reason == "too_dark"


def test_fully_clipped_scene_is_unclear():
    result = check(np.full((FRAMES, HEIGHT, WIDTH), 255, dtype=np.uint8))
    assert (result.decision, result.reason) == ("unclear", "too_bright")


def test_ball_gone_before_the_edge_is_unclear():
    result = check(render_clip("front_light", "gone_early"))
    assert (result.decision, result.reason) == ("unclear", "departed_before_edge")


def test_too_few_post_edge_frames_is_unclear():
    clip = render_clip("front_light", "fast")
    result = check_ball_departure(clip[: PRE + 2], TIMESTAMPS[: PRE + 2], TRIGGER_NS)
    assert (result.decision, result.reason) == ("unclear", "insufficient_post_frames")


def test_tiny_or_malformed_frames_are_unclear():
    assert check_ball_departure(np.zeros((3, 2, 3)), TIMESTAMPS[:3], TRIGGER_NS).reason == (
        "invalid_frames"
    )
    assert check_ball_departure(np.zeros((60, 200, 320)), TIMESTAMPS[:10], TRIGGER_NS).reason == (
        "invalid_frames"
    )


def test_hint_finds_a_re_teed_ball_without_a_full_frame_search():
    hint = ReferenceBall(BALL[0] + 6, BALL[1] - 5, 2 * RADIUS + 1, 150)
    result = check(render_clip("front_light", "fast"), hint)
    assert result.ball_source == "hint"
    assert abs(result.ball_x - BALL[0]) <= 1.5
    assert result.decision == "departed"


def test_hint_far_from_the_ball_falls_back_to_full_frame():
    result = check(render_clip("front_light", "fast"), ReferenceBall(40.0, 40.0, 15.0, 170))
    assert result.ball_source == "full_frame"
    assert result.decision == "departed"


def test_result_serialises_to_json():
    data = check(render_clip("front_light", "fast")).to_dict()
    decoded = json.loads(json.dumps(data))
    assert decoded["decision"] == "departed"
    assert len(decoded["frame_offsets_ms"]) == len(decoded["frame_states"])


def test_exposure_timestamps_prefer_sensor_clock_only_when_consistent():
    sensor = TIMESTAMPS
    host = sensor + 4_600_000
    assert np.array_equal(exposure_timestamps_ns(host, sensor), sensor)
    assert np.array_equal(exposure_timestamps_ns(host, None), host)
    assert np.array_equal(exposure_timestamps_ns(host, sensor + 100_000_000), host)
    assert np.array_equal(exposure_timestamps_ns(host, sensor[::-1]), host)


def capture_from(clip: np.ndarray) -> TriggeredCapture:
    frames = tuple(
        CameraFrame(
            image=image,
            sensor_timestamp_ns=int(TIMESTAMPS[index]),
            host_timestamp_ns=int(TIMESTAMPS[index] + 4_600_000),
            exposure_us=100,
            analogue_gain=2.0,
        )
        for index, image in enumerate(clip)
    )
    return TriggeredCapture(
        frames=frames, pre_trigger_count=PRE, trigger_host_timestamp_ns=TRIGGER_NS
    )


def test_save_capture_stores_the_check_and_never_learns_from_it_without_an_ops_shot(tmp_path):
    runtime = CameraCaptureRuntime(output_dir=tmp_path)
    first = runtime._save_capture(1, 123.0, capture_from(render_clip("front_light", "fast")))
    second = runtime._save_capture(2, 124.0, capture_from(render_clip("front_light", "stays")))

    stored = json.loads((first.path / "metadata.json").read_text())["trigger_check"]
    assert stored["decision"] == "departed"
    assert first.metadata["trigger_check"] == stored
    assert stored["ball_source"] == "full_frame"
    assert second.metadata["trigger_check"]["decision"] == "still_there"
    # The first clip was never an OPS-accepted shot: no hint, no anchor.
    assert second.metadata["trigger_check"]["ball_source"] == "full_frame"
    # Without a hitting zone even an accepted shot's full-frame departure is no evidence.
    assert runtime.note_accepted_shot(first, shot_number=1)["kind"] == "no_departure"
    assert runtime.tee_anchor_status()["state"] == "none"


def test_ops_shot_departure_becomes_the_hint_of_later_checks(tmp_path):
    settings = CameraCaptureSettings(width=WIDTH, height=HEIGHT)
    runtime = CameraCaptureRuntime(output_dir=tmp_path, settings=settings)
    runtime.set_tee_zone(ZONE)
    first = runtime._save_capture(1, 123.0, capture_from(render_clip("front_light", "fast")))
    stray = runtime._save_capture(2, 124.0, capture_from(render_clip("front_light", "stays")))

    assert first.metadata["trigger_check"]["ball_source"] == "zone"
    assert first.metadata["trigger_check"]["departure_source"] == "zone"
    assert stray.metadata["trigger_check"]["ball_source"] == "zone"

    event = runtime.note_accepted_shot(first, shot_number=1)
    assert event["kind"] == "established" and first.metadata["tee_anchor"] == event
    third = runtime._save_capture(3, 125.0, capture_from(render_clip("front_light", "stays")))
    assert third.metadata["trigger_check"]["ball_source"] == "hint"
    assert runtime.trigger_check_anchor() is None


def test_note_accepted_shot_never_raises(tmp_path, caplog):
    runtime = CameraCaptureRuntime(output_dir=tmp_path)
    assert runtime.note_accepted_shot(None) is None
    broken = capture_runtime.SavedCameraCapture(
        sequence=1,
        trigger_timestamp=1.0,
        completed_timestamp=1.0,
        path=tmp_path,
        metadata={},
        error="save failed",
    )
    assert runtime.note_accepted_shot(broken) is None
    odd = capture_runtime.SavedCameraCapture(
        sequence=2,
        trigger_timestamp=1.0,
        completed_timestamp=1.0,
        path=tmp_path,
        metadata={
            "trigger_check": {
                "departure_source": "zone",
                "departure_x": "x",
                "departure_y": 1.0,
                "departure_diameter_px": 15.0,
            }
        },
    )
    assert runtime.note_accepted_shot(odd) is None
    assert "Tee anchor update failed" in caplog.text


def test_hint_search_finds_a_backlit_mid_grey_ball_the_legacy_detector_missed():
    """(f) AE 100 us/g2 at the range: the ball is ~30 grey levels darker than the mat."""
    clip = render_backlit_range("fast")
    anchor = ReferenceBall(BALL[0] + 3, BALL[1] - 3, 2 * RADIUS + 1, 177)
    with pytest.raises(ValueError):
        # The old hint window around the anchor, searched with the old detector.
        detect_reference_ball(clip[:20, 78:150, 125:201], roi=(0, 0, 76, 72))

    result = check(clip, anchor)
    assert result.ball_source == "hint"
    assert abs(result.ball_x - BALL[0]) <= 1.5 and abs(result.ball_y - BALL[1]) <= 1.5
    assert result.pre_contrast >= 20.0
    assert result.decision == "departed"
    ball = find_ball_near(clip[:20], anchor)
    assert ball is not None and abs(ball.x - BALL[0]) <= 1.5


def test_live_check_departs_on_a_backlit_ball():
    clip = render_backlit_range("fast")
    live = LiveDepartureCheck(TRIGGER_NS, ReferenceBall(BALL[0], BALL[1], 2 * RADIUS + 1, 177))
    frames = [
        CameraFrame(image, int(stamp), int(stamp) + 4_600_000, 100, 2.0)
        for image, stamp in zip(clip, TIMESTAMPS)
    ]
    verdict = live.add(frames[:PRE]) or live.add(frames[PRE:])
    assert verdict.decision == "departed"


def test_zone_search_ignores_a_dark_blob_on_the_horizon():
    """(c) The tree crown above the zone never becomes the ball."""
    clip = render_backlit_range("fast", marks=[(150.0, 60.0)])
    result = check(clip, zone=ZONE)
    assert result.ball_source == "zone"
    assert abs(result.ball_x - BALL[0]) <= 1.5 and abs(result.ball_y - BALL[1]) <= 1.5
    assert result.decision == "departed"
    assert result.departure_source == "zone"


def test_full_frame_fallback_never_yields_anchor_evidence():
    """(e) Without a zone the legacy search still judges, but its ball is no departure spot."""
    result = check(render_clip("front_light", "fast"))
    assert (result.ball_source, result.decision) == ("full_frame", "departed")
    assert result.departure is None and result.departure_source is None


def test_zone_scan_finds_the_ball_that_left_while_the_hint_spot_stayed():
    """A wrong anchor on a static mark: still_there there, the departure is reported elsewhere."""
    mark = (200.0, 150.0)
    clip = render_backlit_range("fast", marks=[mark])
    result = check(clip, ReferenceBall(*mark, 2 * RADIUS + 1, 177), ZONE)
    assert (result.ball_source, result.decision) == ("hint", "still_there")
    assert result.departure_source == "zone"
    assert abs(result.departure_x - BALL[0]) <= 1.5 and abs(result.departure_y - BALL[1]) <= 1.5


def test_a_departing_hint_ball_is_its_own_departure_spot():
    result = check(render_backlit_range("fast"), ReferenceBall(*BALL, 2 * RADIUS + 1, 177), ZONE)
    assert result.decision == "departed" and result.departure_source == "hint"
    assert (result.departure_x, result.departure_y) == (result.ball_x, result.ball_y)


def test_a_ball_outside_the_match_radius_cannot_stand_in_for_the_anchor():
    """A ball 25 px aside (the old 2.5-diameter window reached it) is not the tee ball."""
    clip = render_backlit_range("stays")
    anchor = ReferenceBall(BALL[0] + 25, BALL[1], 2 * RADIUS + 1, 177)
    live = LiveDepartureCheck(TRIGGER_NS, anchor)
    frames = [
        CameraFrame(image, int(stamp), int(stamp) + 4_600_000, 100, 2.0)
        for image, stamp in zip(clip, TIMESTAMPS)
    ]
    verdict = live.add(frames) or live.finish()
    assert verdict.decision == "unclear"
    assert verdict.reason in ("ball_not_found", "low_contrast")


def test_check_failure_never_breaks_saving(tmp_path, monkeypatch, caplog):
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(capture_runtime, "check_ball_departure", explode)
    runtime = CameraCaptureRuntime(output_dir=tmp_path)
    saved = runtime._save_capture(1, 123.0, capture_from(render_clip("front_light", "fast")))

    assert saved.valid
    with np.load(saved.path / "frames.npz") as archive:
        assert archive["frames"].shape == (FRAMES, HEIGHT, WIDTH)
    stored = json.loads((saved.path / "metadata.json").read_text())["trigger_check"]
    assert stored == {"decision": "unclear", "reason": "check_failed", "error": "boom"}
    assert "Trigger check #1 failed" in caplog.text


def test_save_capture_of_tiny_frames_records_unclear(tmp_path):
    runtime = CameraCaptureRuntime(output_dir=tmp_path)
    tiny = np.zeros((FRAMES, 2, 3), dtype=np.uint8)
    saved = runtime._save_capture(1, 123.0, capture_from(tiny))
    assert saved.metadata["trigger_check"]["decision"] == "unclear"
    assert saved.metadata["trigger_check"]["reason"] == "invalid_frames"


def test_ball_that_reappears_after_a_short_gap_is_still_there():
    clip = render_clip("front_light", "stays")
    mat = render_clip("front_light", "fast")
    clip[PRE + 1 : PRE + 3] = mat[PRE + 1 : PRE + 3]
    result = check(clip)
    assert (result.decision, result.reason) == ("still_there", "ball_reappeared")


def test_spot_covered_to_the_end_of_the_window_is_unclear():
    clip = render_clip("front_light", "stays")
    clip[PRE:, 100:170, 120:200] = SCENES["front_light"]["club"]
    result = check(clip)
    assert (result.decision, result.reason) == ("unclear", "occluded")


# --- static spots never judged as the ball (review round 2) ---------------------

DIAMETER = 2 * RADIUS + 1
# Every synthetic frame is exposed at 100 us and gain 2 (``capture_from``).
EXPOSURE_GAIN = 200.0


def live_frames(clip: np.ndarray, *, exposure_us: int = 100, gain: float = 2.0) -> list:
    return [
        CameraFrame(image, int(stamp), int(stamp) + 4_600_000, exposure_us, gain)
        for image, stamp in zip(clip, TIMESTAMPS)
    ]


def live_verdict(clip: np.ndarray, anchor: ReferenceBall, **exposure):
    live = LiveDepartureCheck(TRIGGER_NS, anchor)
    frames = live_frames(clip, **exposure)
    return live.add(frames[:PRE]) or live.add(frames[PRE:]) or live.finish(), live


def render_scene(  # pylint: disable=too-many-arguments
    *,
    mat: float = 100.0,
    ball: float | None = 75.0,
    ball_at=BALL,
    leaves: bool = True,
    patches=(),
    scale: float = 1.0,
    seed: int = 0,
) -> np.ndarray:
    """Flat mat with static ``patches`` ((x, y, radius, level)) and a ball that leaves.

    ``scale`` multiplies every level's distance from black, like a longer
    exposure or a higher gain on the raw stream.
    """
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    base = np.full((HEIGHT, WIDTH), mat)
    for x, y, radius, level in patches:
        base[(xx - x) ** 2 + (yy - y) ** 2 <= radius**2] = level
    with_ball = base.copy()
    if ball is not None:
        with_ball[(xx - ball_at[0]) ** 2 + (yy - ball_at[1]) ** 2 <= (RADIUS + 0.5) ** 2] = ball
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(FRAMES):
        image = with_ball if (index < PRE or not leaves) else base
        noisy = scale * image + rng.normal(0.0, 2.0, size=image.shape)
        clip[index] = np.clip(noisy, 0, 255).astype(np.uint8)
    return clip


# A bright static patch (white tee box, shoe toe) 1.7 diameters above the ball.
EDGE_PATCH = (BALL[0], BALL[1] - 25.0, 12.7, 220.0)


def anchor_ball(contrast: float | None, at=BALL) -> AnchorBall:
    """The confirmed anchor with its ball's normalised contrast (100 us x gain 2 frames)."""
    normalised = None if contrast is None else normalised_contrast(contrast, EXPOSURE_GAIN)
    return AnchorBall(at[0], at[1], DIAMETER, 177, contrast=normalised)


@pytest.mark.parametrize("contrast", [None, -25.0])
def test_a_static_spot_just_outside_the_circle_never_stands_in_for_the_ball(contrast):
    """Finding 1: a bright patch 1.7 diameters away pinned the old search to the circle edge.

    Its response at the circle's edge (15 px above the anchor) beat the dark
    ball at the anchor, the edge spot stayed, and the strike was blocked
    (still_there). Only true peaks count now, with or without the anchor's
    contrast.
    """
    clip = render_scene(patches=[EDGE_PATCH])
    ball = find_ball_near(clip[:20], anchor_ball(contrast))
    # The patch's glow in the ring nudges the peak by a pixel or two, no more.
    assert ball is not None and math.hypot(ball.x - BALL[0], ball.y - BALL[1]) <= 3.0

    verdict, _live = live_verdict(clip, anchor_ball(contrast))
    assert verdict.decision == "departed"


def test_a_circle_edge_without_a_peak_finds_no_ball():
    """No ball at the anchor, a strong patch outside: nothing inside the circle is a peak."""
    clip = render_scene(ball=None, patches=[EDGE_PATCH])
    assert find_ball_near(clip[:20], anchor_ball(None)) is None


def test_a_spot_unlike_the_confirmed_ball_is_not_judged():
    """Finding 1: a static bright mark (+40) where a dark ball (-25) was confirmed: pulse."""
    clip = render_scene(ball=140.0, leaves=False)
    verdict, live = live_verdict(clip, anchor_ball(-25.0))
    assert (verdict.decision, verdict.reason) == ("unclear", "spot_mismatch")
    assert live.spot_contrast == pytest.approx(normalised_contrast(40.0, EXPOSURE_GAIN), abs=2.0)

    # The same ball-like spot that stays is still judged: still_there blocks.
    verdict, _live = live_verdict(render_scene(leaves=False), anchor_ball(-25.0))
    assert verdict.decision == "still_there"


def test_the_spot_match_holds_across_exposure_changes():
    """Contrast scales with exposure x gain; the normalised anchor keeps matching (d)."""
    tripled = render_scene(leaves=False, scale=3.0, mat=60.0, ball=45.0)
    verdict, live = live_verdict(tripled, anchor_ball(-15.0), exposure_us=200, gain=3.0)
    assert verdict.decision == "still_there"
    assert live.spot_contrast == pytest.approx(normalised_contrast(-15.0, EXPOSURE_GAIN), abs=1.0)
    # Unscaled, three times the contrast would have looked like another spot.
    assert not spot_matches(-45.0, -15.0)


def test_frames_without_exposure_cannot_verify_the_spot():
    Frame = namedtuple("Frame", "image host_timestamp_ns sensor_timestamp_ns")
    live = LiveDepartureCheck(TRIGGER_NS, anchor_ball(-25.0))
    frames = [
        Frame(image, int(stamp), int(stamp) + 4_600_000)
        for image, stamp in zip(render_scene(leaves=False), TIMESTAMPS)
    ]
    verdict = live.add(frames) or live.finish()
    assert (verdict.decision, verdict.reason) == ("unclear", "spot_unverified")


def test_spot_matches_needs_the_same_sign_and_a_similar_size():
    assert spot_matches(-20.0, -15.0) and spot_matches(-8.0, -15.0) and spot_matches(-30.0, -15.0)
    assert not spot_matches(15.0, -15.0)
    assert not spot_matches(-7.0, -15.0) and not spot_matches(-31.0, -15.0)
    assert normalised_contrast(-30.0, 200.0) == pytest.approx(-15.0)
    assert normalised_contrast(-30.0, None) is None and normalised_contrast(-30.0, 0.0) is None


# Zone with a tree line just above its top edge, as on 2026-10-25 (zone top 209,
# horizon ~206): mat below row 100, a dark band at rows 80-97.
TOP_ZONE = TeeZone(
    x0=100,
    y0=100,
    x1=240,
    y1=190,
    tee_x=160.0,
    tee_y=150.0,
    diameter_px=DIAMETER,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)


def render_tree_line(*, ball_leaves: bool = True, spare=None) -> np.ndarray:
    """Sky above row 80, a dark tree band at rows 80-97, mat below with a backlit ball.

    A bright static mark sits at the zone's top edge (row 103); ``spare`` adds
    a bright static ball elsewhere in the zone. Both are stronger spots than
    the teed ball (mat 60, ball 25).
    """
    clip = render_scene(
        mat=60.0,
        ball=25.0,
        leaves=ball_leaves,
        patches=[] if spare is None else [(spare[0], spare[1], RADIUS, 240.0)],
    )
    clip[:, :80, :] = 200
    clip[:, 80:98, :] = 15
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    clip[:, (xx - 130) ** 2 + (yy - 103) ** 2 <= RADIUS**2] = 230
    return clip


def test_zone_search_without_a_hint_reports_the_ball_that_left_not_the_tree_line():
    """Finding 4: the zone's strongest static spot (tree line, top-edge mark) is no ball.

    The old check judged it still_there although the strike's ball left.
    """
    result = check(render_tree_line(), zone=TOP_ZONE)
    assert result.decision != "still_there"
    assert result.departure_source == "zone"
    assert abs(result.departure_x - BALL[0]) <= 1.5 and abs(result.departure_y - BALL[1]) <= 1.5
    # The judged centres keep the mat ring inside the zone (below its top edge).
    assert result.ball_y >= TOP_ZONE.y0 + math.ceil(1.4 * DIAMETER)


def test_a_static_zone_spot_is_never_still_there():
    """A spare ball brighter than the teed one: judged, but never still_there."""
    spare = (210.0, 170.0)
    left = check(render_tree_line(spare=spare), zone=TOP_ZONE)
    assert (left.decision, left.ball_source) == ("departed", "zone")
    assert abs(left.ball_x - BALL[0]) <= 1.5 and abs(left.ball_y - BALL[1]) <= 1.5

    stayed = check(render_tree_line(ball_leaves=False, spare=spare), zone=TOP_ZONE)
    assert (stayed.decision, stayed.reason) == ("unclear", "zone_spot_static")
    assert stayed.departure is None


def render_waggle(start_ms: float, *, step_px: float = 1.5) -> np.ndarray:
    """A ball-sized dark spot (a club head's toe) that starts sliding at ``start_ms``."""
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    offsets = (TIMESTAMPS - TRIGGER_NS) / 1e6
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index, offset in enumerate(offsets):
        shift = max(0.0, (offset - start_ms) / 3.472) * step_px
        image = np.full((HEIGHT, WIDTH), 60.0)
        image[(xx - (BALL[0] - shift)) ** 2 + (yy - BALL[1]) ** 2 <= (RADIUS + 0.5) ** 2] = 25.0
        clip[index] = np.clip(image + rng.normal(0.0, 2.0, image.shape), 0, 255).astype(np.uint8)
    return clip


@pytest.mark.parametrize(
    ("start_ms", "step_px"),
    [(-25.0, 1.5), (30.0, 6.0)],
    ids=["moving_before_the_edge", "leaving_too_late"],
)
def test_a_waggled_club_head_is_no_departing_ball(monkeypatch, start_ms, step_px):
    """2026-10-25 clips 065/165: a club head beside the ball 'departed' from the zone."""
    clip = render_waggle(start_ms, step_px=step_px)
    result = check(clip, zone=ZONE)
    assert result.decision != "departed" and result.departure is None

    # Without the struck-ball test the same spot counted as a departure.
    monkeypatch.setattr(trigger_check, "_struck", lambda *_args: True)
    assert check(clip, zone=ZONE).departure is not None


def test_departure_carries_its_signed_contrast_and_exposure():
    clip = render_backlit_range("fast")
    result = check_ball_departure(
        clip, TIMESTAMPS, TRIGGER_NS, zone=ZONE, exposure_gain=np.full(FRAMES, EXPOSURE_GAIN)
    )
    assert result.departure_source == "zone"
    assert result.departure_contrast < -20.0
    assert result.exposure_gain == EXPOSURE_GAIN
    stored = result.to_dict()
    assert stored["departure_contrast"] < 0 and stored["exposure_gain"] == EXPOSURE_GAIN


def test_a_saved_clip_of_a_blocked_edge_proves_a_stale_anchor_wrong(tmp_path):
    """Finding 2, end to end: anchor on a mat mark, the strike from the new spot was blocked.

    The clip's check sees the ball leave away from the anchor; the anchor loses
    its confirmations, so the next strike is pulsed instead of blocked too.
    """
    mark = (200.0, 150.0)
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path, settings=CameraCaptureSettings(width=WIDTH, height=HEIGHT)
    )
    runtime.set_tee_zone(ZONE)
    stale = {
        "departure_x": mark[0],
        "departure_y": mark[1],
        "departure_diameter_px": DIAMETER,
        "departure_source": "hint",
        "departure_contrast": -35.0,
        "exposure_gain": EXPOSURE_GAIN,
    }
    for shot in range(3):
        runtime.note_accepted_shot(
            capture_runtime.SavedCameraCapture(1, 1.0, 1.0, tmp_path, {"trigger_check": stale}),
            shot_number=shot,
        )
    assert runtime.trigger_check_anchor() is not None

    runtime.note_blocked_edge(123.0)
    saved = runtime._save_capture(
        1, 123.0, capture_from(render_backlit_range("fast", marks=[mark]))
    )
    stored = saved.metadata["trigger_check"]
    assert (stored["ball_source"], stored["decision"]) == ("hint", "still_there")
    assert abs(stored["departure_x"] - BALL[0]) <= 1.5
    assert saved.metadata["tee_anchor"]["kind"] == "blocked_departure"
    assert json.loads((saved.path / "metadata.json").read_text())["tee_anchor"]["contradicted"]
    assert runtime.trigger_check_anchor() is None


# --- two ball-like spots at the anchor (fixups, two-spot lockout) ---------------


def test_a_static_ball_copy_beside_the_teed_ball_never_locks_the_gate():
    """A spot 1.1x stronger than the ball, 15 px beside it, inside the match radius.

    The gate judged the static spot (still_there) and blocked every strike; the
    blocked edge's check also judged it and its departure, the teed ball, lay
    within the radius of the anchor, so the anchor was kept for good. Now the
    gate judges neither spot (``two_spots``), and a block of that kind
    contradicts the anchor.
    """
    learned = TeeAnchor()
    exposure = np.full(FRAMES, EXPOSURE_GAIN)
    for shot in range(3):
        clean = check_ball_departure(
            render_scene(seed=shot), TIMESTAMPS, TRIGGER_NS, zone=ZONE, exposure_gain=exposure
        )
        learned.observe_shot(clean, shot=shot + 1)
    anchor = learned.gate_anchor()
    assert anchor is not None and math.hypot(anchor.x - BALL[0], anchor.y - BALL[1]) <= 1.5

    static = (BALL[0] + 15.0, BALL[1], RADIUS + 0.5, 100.0 - 1.1 * 25.0)
    clip = render_scene(patches=[static])
    verdict, live = live_verdict(clip, anchor)
    assert verdict.decision != "still_there"
    assert (verdict.decision, verdict.reason) == ("unclear", "two_spots")
    assert live.ball is None

    blocked = check_ball_departure(
        clip, TIMESTAMPS, TRIGGER_NS, anchor, zone=ZONE, exposure_gain=exposure
    )
    assert (blocked.ball_source, blocked.decision) == ("hint", "still_there")
    assert abs(blocked.ball_x - static[0]) <= 2.0
    event = learned.observe_blocked_edge(blocked, edge=1.0)
    assert event is not None and event["contradicted"]
    assert learned.gate_anchor() is None


def test_a_single_ball_at_the_anchor_is_still_judged():
    """Only a second matching peak stops the gate: a ball alone departs or stays."""
    verdict, _live = live_verdict(render_scene(), anchor_ball(-25.0))
    assert verdict.decision == "departed"
    verdict, _live = live_verdict(render_scene(leaves=False), anchor_ball(-25.0))
    assert verdict.decision == "still_there"


def test_a_second_peak_unlike_the_ball_does_not_stop_the_gate():
    """A bright mark (+40) 15 px beside a dark ball (-25) is no second ball."""
    mark = (BALL[0] + 15.0, BALL[1], RADIUS + 0.5, 140.0)
    verdict, _live = live_verdict(render_scene(patches=[mark]), anchor_ball(-25.0))
    assert verdict.decision == "departed"


def learned_anchor() -> tuple[TeeAnchor, AnchorBall]:
    """A tee anchor confirmed by three clean shots of the synthetic scene."""
    learned = TeeAnchor()
    exposure = np.full(FRAMES, EXPOSURE_GAIN)
    for shot in range(3):
        clean = check_ball_departure(
            render_scene(seed=shot), TIMESTAMPS, TRIGGER_NS, zone=ZONE, exposure_gain=exposure
        )
        learned.observe_shot(clean, shot=shot + 1)
    anchor = learned.gate_anchor()
    assert anchor is not None and math.hypot(anchor.x - BALL[0], anchor.y - BALL[1]) <= 1.5
    return learned, anchor


def test_a_stronger_static_ball_just_in_front_of_the_teed_ball_never_locks_the_gate():
    """A static spot 1.5x as dark one diameter above (downrange of) the teed ball.

    Its kernel tail within half a diameter of the teed ball is stronger than
    the ball's own peak, so the teed ball is no true peak and the circle holds
    only the static spot: the gate judged it still_there and blocked every
    strike. The spot at the anchor centre matches the anchor too: two spots.
    """
    _learned, anchor = learned_anchor()
    static = (BALL[0], BALL[1] - DIAMETER, RADIUS + 0.5, 100.0 - 1.5 * 25.0)
    verdict, live = live_verdict(render_scene(patches=[static]), anchor)
    assert (verdict.decision, verdict.reason) == ("unclear", "two_spots")
    assert live.ball is None


@pytest.mark.parametrize(
    ("offset", "darker"),
    [
        ((11.0, -11.0), 1.1),
        ((-11.0, -11.0), 1.1),
        ((9.0, -9.0), 1.1),
        ((-10.0, 10.0), 1.1),
        # 2026-10-26 review what-ifs: a static spot 1.5-2x as dark is no
        # contrast peak beside the ball, and one 16-17 px away sits in the
        # ball's mat ring; a stale anchor blocked 4-12 strikes in a row.
        ((11.0, -11.0), 1.5),
        ((-11.0, -11.0), 1.5),
        ((0.0, -17.0), 1.5),
        ((-12.0, -12.0), 1.5),
        ((11.0, -11.0), 2.0),
        ((-11.0, -11.0), 2.0),
        ((0.0, -17.0), 2.0),
        ((-12.0, -12.0), 2.0),
    ],
)
def test_a_ball_leaving_beside_a_stronger_static_spot_contradicts_a_stale_anchor(offset, darker):
    """A stale anchor on a static spot 1.1-2x as dark; the ball is teed 13-17 px from it.

    The live gate judges the static spot still_there and blocks the strike;
    so does the check. The departure scan used to suppress every spot within
    one diameter (a square) of the stronger static one and to skip spots
    within one diameter of the judged spot; beside a spot 1.5-2x as dark the
    ball is no peak at all, and 16-17 px away the static spot darkens the
    ball's mat ring. Either way the departing ball was never a departure, the
    block never contradicted the anchor and every strike from that tee spot
    was blocked.
    """
    static = (BALL[0] + offset[0], BALL[1] + offset[1], RADIUS + 0.5, 100.0 - darker * 25.0)
    clip = render_scene(patches=[static])
    stale = AnchorBall(
        static[0], static[1], DIAMETER, 177, contrast=normalised_contrast(-25.0, EXPOSURE_GAIN)
    )
    verdict, _live = live_verdict(clip, stale)
    assert verdict.decision == "still_there"
    blocked = check_ball_departure(
        clip, TIMESTAMPS, TRIGGER_NS, stale, zone=ZONE, exposure_gain=np.full(FRAMES, EXPOSURE_GAIN)
    )
    assert (blocked.ball_source, blocked.decision) == ("hint", "still_there")
    assert math.hypot(blocked.ball_x - static[0], blocked.ball_y - static[1]) <= 3.0
    assert blocked.departure is not None
    assert math.hypot(blocked.departure.x - BALL[0], blocked.departure.y - BALL[1]) <= 2.0
    anchor = TeeAnchor()
    for shot in range(3):
        anchor.observe_shot(
            {
                "decision": "departed",
                "ball_source": "hint",
                "departure_x": stale.x,
                "departure_y": stale.y,
                "departure_diameter_px": DIAMETER,
                "departure_source": "hint",
                "departure_contrast": -25.0,
                "exposure_gain": EXPOSURE_GAIN,
            },
            shot=shot + 1,
        )
    assert anchor.gate_anchor() is not None
    event = anchor.observe_blocked_edge(blocked, edge=1.0)
    assert event is not None and event["contradicted"]
    assert anchor.gate_anchor() is None


@pytest.mark.parametrize("judged_static", [True, False])
def test_spots_beside_the_judged_one_are_searched_only_when_it_stayed(judged_static):
    """The changed-spot search runs only beside a spot the check judged still_there.

    A static spot 2x as dark 17 px from the ball hides the ball from the
    contrast-peak searches; only the changed-spot search finds it leaving.
    Beside a spot too dark to judge, a shadow edge creeping across the mat
    "left like a struck ball" in a 2026-10-25 no-shot clip, so the search
    stays off there.
    """
    static = (BALL[0], BALL[1] - 17.0, RADIUS + 0.5, 100.0 - 2.0 * 25.0)
    clip = render_scene(patches=[static])
    offsets = (TIMESTAMPS - TRIGGER_NS) / 1e6
    reference_idx = np.flatnonzero(offsets <= trigger_check.REFERENCE_END_MS)[
        : trigger_check.REFERENCE_FRAMES
    ]
    view_idx = np.flatnonzero(
        (offsets >= trigger_check.EARLY_DEPARTURE_MS[0]) & (offsets <= trigger_check.WINDOW_MS[1])
    )
    left = trigger_check._departed_zone_spot(  # pylint: disable=protected-access
        clip,
        reference_idx,
        (view_idx, offsets[view_idx]),
        window_ms=trigger_check.WINDOW_MS,
        zone=ZONE,
        judged=ReferenceBall(static[0], static[1], DIAMETER, 177),
        judged_static=judged_static,
    )
    if judged_static:
        assert left is not None
        assert math.hypot(left[0].x - BALL[0], left[0].y - BALL[1]) <= 2.0
        assert left[1] < 0.0
    else:
        assert left is None


# --- ball size: the footprint keeps an oversized zone diameter from blinding the gate ---


def render_soft_scene(ball: float, *, leaves: bool = True, seed: int = 0) -> np.ndarray:
    """``render_scene`` with the ball's edge blurred (sigma 2 px) like the range clips."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    base = np.full((HEIGHT, WIDTH), 100.0)
    with_ball = base.copy()
    with_ball[(xx - BALL[0]) ** 2 + (yy - BALL[1]) ** 2 <= (RADIUS + 0.5) ** 2] = ball
    with_ball = ndimage.gaussian_filter(with_ball, 2.0)
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(FRAMES):
        image = with_ball if (index < PRE or not leaves) else base
        noisy = image + rng.normal(0.0, 2.0, size=image.shape)
        clip[index] = np.clip(noisy, 0, 255).astype(np.uint8)
    return clip


# A zone whose geometric ball is 1.7x the real one (tee range set far too short),
# tall enough that the ball lies among its judged centres.
OVERSIZED_ZONE = dataclasses.replace(ZONE, y0=40, diameter_px=26.0)


def _reference_and_window(clip: np.ndarray):
    offsets = (TIMESTAMPS - TRIGGER_NS) / 1e6
    reference_idx = np.flatnonzero(offsets <= trigger_check.REFERENCE_END_MS)[
        : trigger_check.REFERENCE_FRAMES
    ]
    window_idx = np.flatnonzero(
        (offsets >= trigger_check.WINDOW_MS[0]) & (offsets <= trigger_check.WINDOW_MS[1])
    )
    return clip, reference_idx, window_idx


@pytest.mark.parametrize("searched", [12.0, 15.0, 24.0])
def test_footprint_measures_the_ball_whatever_diameter_was_searched(searched):
    """The 15 px ball's change is 15 px across, searched at 12, 15 or 24 px."""
    footprint = trigger_check.footprint_diameter_px(
        *_reference_and_window(render_scene()), ReferenceBall(*BALL, searched, 177)
    )
    assert footprint == pytest.approx(2 * RADIUS + 1, abs=1.0)


def test_footprint_ignores_a_club_changing_on_one_side():
    """A shaft lying beside the spot after the edge changes one sector, not the footprint."""
    clip = render_scene()
    row, col = int(BALL[1]), int(BALL[0])
    clip[PRE:, row - 3 : row + 4, col + 9 : col + 26] = 220
    footprint = trigger_check.footprint_diameter_px(
        *_reference_and_window(clip), ReferenceBall(*BALL, DIAMETER, 177)
    )
    assert footprint == pytest.approx(2 * RADIUS + 1, abs=1.0)


def test_footprint_is_none_without_a_change_or_at_the_image_edge():
    spot = ReferenceBall(*BALL, DIAMETER, 177)
    assert (
        trigger_check.footprint_diameter_px(
            *_reference_and_window(render_scene(leaves=False)), spot
        )
        is None
    )
    corner = ReferenceBall(10.0, 10.0, DIAMETER, 177)
    assert (
        trigger_check.footprint_diameter_px(*_reference_and_window(render_scene()), corner) is None
    )


@pytest.mark.parametrize(
    ("nominal", "footprint", "expected"),
    [
        (16.5, None, 16.5),
        (16.5, 11.3, 16.5),
        (24.0, 13.4, 20.1),
        (24.0, 40.0, 24.0),
        (24.0, 4.0, trigger_check.MIN_DIAMETER_PX),
    ],
)
def test_the_kernel_diameter_only_ever_shrinks_the_nominal(nominal, footprint, expected):
    assert trigger_check.kernel_diameter_px(nominal, footprint) == pytest.approx(expected)


def test_a_right_zone_diameter_is_kept_for_the_departure():
    result = check(render_scene(), zone=ZONE)
    assert result.departure_source == "zone"
    assert result.departure_diameter_px == DIAMETER
    assert result.departure_footprint_px == pytest.approx(DIAMETER, abs=1.0)


def test_a_hint_departure_is_sized_against_the_zone_not_the_shrunk_hint():
    """Capping against the hint's own diameter would ratchet the anchor down shot by shot."""
    result = check(render_scene(), ReferenceBall(*BALL, 12.0, 113), ZONE)
    assert (result.ball_source, result.decision) == ("hint", "departed")
    assert result.departure_diameter_px == DIAMETER


def test_an_oversized_zone_diameter_no_longer_blinds_the_gate():
    """Regression for the 20-26 px fragility of the 2026-10-25 report.

    The zone's geometric ball is 26 px, the real one 15 px with soft edges. At
    26 px the core disk takes in mat, and a ball 40 grey levels darker than the
    mat falls below ``MIN_CONTRAST``: the gate pulsed everything (replay at
    24 px: 20 of 69 strikes departed). The departures' footprints now size the
    anchor.
    """
    learned = TeeAnchor()
    exposure = np.full(FRAMES, EXPOSURE_GAIN)
    for shot in range(3):
        result = check_ball_departure(
            render_soft_scene(30.0, seed=shot),
            TIMESTAMPS,
            TRIGGER_NS,
            zone=OVERSIZED_ZONE,
            exposure_gain=exposure,
        )
        assert result.departure_footprint_px == pytest.approx(DIAMETER, abs=1.0)
        assert result.departure_diameter_px == pytest.approx(1.2 * DIAMETER / 0.8, abs=1.0)
        learned.observe_shot(result, shot=shot + 1)
    anchor = learned.gate_anchor()
    assert anchor is not None and anchor.diameter_px < OVERSIZED_ZONE.diameter_px - 2.0

    fainter_stays, fainter_leaves = render_soft_scene(60.0, leaves=False), render_soft_scene(60.0)
    at_zone_size = dataclasses.replace(anchor, diameter_px=OVERSIZED_ZONE.diameter_px)
    assert live_verdict(fainter_stays, at_zone_size)[0].reason == "low_contrast"
    assert live_verdict(fainter_stays, anchor)[0].decision == "still_there"
    assert live_verdict(fainter_leaves, anchor)[0].decision == "departed"
