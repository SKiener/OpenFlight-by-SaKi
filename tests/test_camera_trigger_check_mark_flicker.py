"""Trigger check with a tapped ball mark and under 100 Hz mains flicker (2026-11-02).

Synthetic clips at 288 fps: a hitting zone holding a faint teed ball and a
brighter static basket of spare balls, and a floodlit ball whose brightness
swings with a 100 Hz lamp sampled at a new phase in every frame.
"""

import math

import numpy as np
import pytest

from openflight.camera import trigger_check
from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.departed_ball import MARK_WINDOW_HALF_PX
from openflight.camera.flicker import FLICKER_MIN_REL_STD, flicker_levels
from openflight.camera.tee_anchor import CONFIRMATIONS_TO_BLOCK, TeeAnchor
from openflight.camera.tee_zone import TeeZone
from openflight.camera.trigger_check import (
    ANCHOR_SOURCES,
    MARK_MIN_CONTRAST,
    MIN_CONTRAST,
    UNLIT_STATE,
    check_ball_departure,
)

HEIGHT, WIDTH = 200, 320
FRAMES, PRE = 60, 45
FPS = 287.9
INTERVAL_NS = round(1e9 / FPS)
TIMESTAMPS = np.arange(FRAMES, dtype=np.int64) * INTERVAL_NS + 1_000_000_000
TRIGGER_NS = int(TIMESTAMPS[PRE - 1] + 2_000_000)
RADIUS = 7
DIAMETER = 2 * RADIUS + 1
BALL = (150.0, 140.0)
# The spare balls in the basket, inside the zone but 100 px from the tee.
BASKET = ((250.0, 120.0), (262.0, 121.0))
MARK = (154.0, 146.0)
EXPOSURE_GAIN = np.full(FRAMES, 2100.0)

ZONE = TeeZone(
    x0=60,
    y0=80,
    x1=300,
    y1=190,
    tee_x=150.0,
    tee_y=140.0,
    diameter_px=DIAMETER,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)


def _disk(position, radius=RADIUS):
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    return (xx - position[0]) ** 2 + (yy - position[1]) ** 2 <= (radius + 0.5) ** 2


def render_basket_scene(
    *, ball_contrast: float = 14.0, leaves: bool = True, ball=BALL, seed: int = 0
) -> np.ndarray:
    """Daylight at the tee (2026-11-02 S1): a faint ball, a brighter static basket.

    Mat ~50 with fixed texture; the teed ball stands ``ball_contrast`` grey above
    it (below the gate's MIN_CONTRAST), the spare balls +32. The ball leaves
    after the edge when ``leaves``.
    """
    rng = np.random.default_rng(seed)
    texture = rng.integers(-3, 4, size=(HEIGHT, WIDTH))
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(FRAMES):
        image = 50.0 + texture + rng.normal(0.0, 1.5, size=(HEIGHT, WIDTH))
        for spare in BASKET:
            image[_disk(spare)] = 82.0
        if not leaves or index < PRE:
            image[_disk(ball)] = 50.0 + ball_contrast
        clip[index] = np.clip(np.rint(image), 0, 255).astype(np.uint8)
    return clip


def lamp_wave(count: int = FRAMES, *, depth: float = 0.75, phase: float = 0.3) -> np.ndarray:
    """A 100 Hz floodlight sampled at 287.9 fps: 1 + depth * sin(...), never below 0."""
    times = np.arange(count) / FPS
    return 1.0 + depth * np.sin(2.0 * math.pi * 100.0 * times + phase)


def render_floodlit_ball(*, leaves: bool = True, depth: float = 0.75, seed: int = 0):
    """2026-11-02 evening: a mains lamp lights the tee (2026-10-30: factor ~2.7).

    Each pixel is ambient + lit x the lamp's level in that frame: the mat 30 +
    12, the white ball 30 + 130, so the ball swings far more than the mat. The
    ball leaves after the edge when ``leaves``.
    """
    rng = np.random.default_rng(seed)
    texture = rng.integers(-3, 4, size=(HEIGHT, WIDTH))
    wave = lamp_wave(depth=depth)
    ball = _disk(BALL)
    clip = np.empty((FRAMES, HEIGHT, WIDTH), dtype=np.uint8)
    for index in range(FRAMES):
        image = 30.0 + texture + 12.0 * wave[index]
        if not leaves or index < PRE:
            image[ball] = 30.0 + 130.0 * wave[index]
        image = image + rng.normal(0.0, 1.5, size=(HEIGHT, WIDTH))
        clip[index] = np.clip(np.rint(image), 0, 255).astype(np.uint8)
    return clip


def check(clip, hint=None, **kwargs):
    return check_ball_departure(
        clip, TIMESTAMPS, TRIGGER_NS, hint, exposure_gain=EXPOSURE_GAIN, **kwargs
    )


def near(x, y, point, tolerance=3.0) -> bool:
    return x is not None and math.hypot(x - point[0], y - point[1]) <= tolerance


# --- the ball mark bounds the search -------------------------------------------------


def test_without_a_mark_the_zone_search_judges_the_basket_not_the_faint_ball():
    """The 2026-11-02 bug: the zone's strongest spot is a spare ball, and the faint
    teed ball (14 grey, below MIN_CONTRAST) is never a departure candidate."""
    result = check(render_basket_scene(), zone=ZONE)
    assert (result.decision, result.reason) == ("unclear", "zone_spot_static")
    midpoint = np.mean(BASKET, axis=0)
    assert near(result.ball_x, result.ball_y, midpoint, tolerance=8.0)
    assert result.departure is None


def test_with_a_mark_the_ball_in_its_window_departs():
    result = check(render_basket_scene(), zone=ZONE, ball_mark_px=MARK)
    assert (result.decision, result.reason) == ("departed", "ball_left_spot")
    assert result.ball_source == "ball_mark"
    assert near(result.ball_x, result.ball_y, BALL)
    assert result.departure_source == "ball_mark"
    assert near(result.departure_x, result.departure_y, BALL)
    assert result.ball_mark == MARK
    assert result.to_dict()["ball_mark"] == [MARK[0], MARK[1]]


def test_a_marked_ball_that_stays_is_no_departure_and_no_evidence():
    result = check(render_basket_scene(leaves=False), zone=ZONE, ball_mark_px=MARK)
    assert result.decision == "unclear"
    assert result.reason == "zone_spot_static"
    assert result.departure is None


def test_the_mark_floor_sits_below_the_gate_s():
    assert MARK_MIN_CONTRAST < MIN_CONTRAST
    faint = check(render_basket_scene(ball_contrast=6.0), zone=ZONE, ball_mark_px=MARK)
    assert faint.decision == "unclear"
    assert faint.departure is None


def test_a_hint_outside_the_mark_window_cannot_stand_in_for_the_ball():
    """An anchor on a spare ball (as the 2026-10-25 tree crown) is outside the tap's window."""
    hint = ReferenceBall(*BASKET[0], DIAMETER, 177)
    assert math.hypot(BASKET[0][0] - MARK[0], BASKET[0][1] - MARK[1]) > MARK_WINDOW_HALF_PX
    result = check(render_basket_scene(), hint, zone=ZONE, ball_mark_px=MARK)
    assert result.ball_source == "ball_mark"
    assert near(result.ball_x, result.ball_y, BALL)
    assert result.decision == "departed"


def test_a_hint_inside_the_mark_window_is_used_first():
    hint = ReferenceBall(BALL[0] + 2.0, BALL[1] - 1.0, DIAMETER, 177)
    result = check(render_basket_scene(), hint, zone=ZONE, ball_mark_px=MARK)
    assert result.ball_source == "hint"
    assert result.decision == "departed"
    assert result.departure_source == "hint"


def test_a_mark_off_the_frame_is_unclear():
    result = check(render_basket_scene(), zone=ZONE, ball_mark_px=(WIDTH + 200.0, 10.0))
    assert (result.decision, result.reason) == ("unclear", "ball_mark_off_frame")


def test_without_a_fitting_zone_the_mark_window_is_searched_alone():
    """640x200 on 2026-11-02 had no zone: the full-frame search found background spots.

    The ball size then comes from the legacy detector inside the window, which
    needs a ball as bright as a white one in front light.
    """
    clip = render_basket_scene(ball_contrast=170.0)
    without = check(clip)
    assert without.ball_source in (None, "full_frame")
    assert without.departure is None
    result = check(clip, ball_mark_px=MARK)
    assert result.ball_source == "ball_mark"
    assert near(result.ball_x, result.ball_y, BALL)
    assert result.decision == "departed"


def test_the_tee_anchor_learns_from_ball_mark_departures():
    assert "ball_mark" in ANCHOR_SOURCES
    anchor = TeeAnchor()
    for shot in range(CONFIRMATIONS_TO_BLOCK):
        result = check(render_basket_scene(seed=shot), anchor.hint(), zone=ZONE, ball_mark_px=MARK)
        event = anchor.observe_shot(result, shot=shot + 1)
        assert event["kind"] == ("established" if shot == 0 else "confirmed")
    gate = anchor.gate_anchor()
    assert gate is not None and near(gate.x, gate.y, BALL)


# --- flicker-proof states ------------------------------------------------------------


def test_a_floodlit_ball_no_longer_departs_before_the_edge():
    """2026-11-02 #52-65: the ball 'left' in every lamp trough (G P G G A G ...)."""
    clip = render_floodlit_ball()
    result = check(clip, zone=ZONE)
    assert result.flicker["reason"] == "detected"
    assert (result.decision, result.reason) == ("departed", "ball_left_spot")
    assert near(result.ball_x, result.ball_y, BALL)
    early = [s for s, t in zip(result.frame_states, result.frame_offsets_ms) if t <= -15.0]
    assert "G" not in early
    assert UNLIT_STATE in result.frame_states
    assert result.departure is not None


def test_without_phase_matching_the_floodlit_ball_departs_before_the_edge(monkeypatch):
    """The same clip judged against the median reference: the bug this fixes."""
    real = trigger_check.ball_flicker_levels

    def unmatched(frames, **kwargs):
        return None, real(frames, **kwargs)[1]

    monkeypatch.setattr(trigger_check, "ball_flicker_levels", unmatched)
    result = check(render_floodlit_ball(), zone=ZONE)
    early = [s for s, t in zip(result.frame_states, result.frame_offsets_ms) if t <= -15.0]
    assert "G" in early
    assert result.decision == "unclear"
    assert result.departure is None


def test_a_floodlit_ball_that_stays_never_departs():
    result = check(render_floodlit_ball(leaves=False), zone=ZONE)
    assert result.decision != "departed"
    assert "G" not in result.frame_states
    assert result.departure is None


def test_a_floodlit_ball_departs_with_a_mark_too():
    result = check(render_floodlit_ball(), zone=ZONE, ball_mark_px=MARK)
    assert (result.decision, result.ball_source) == ("departed", "ball_mark")


def test_a_calm_clip_is_judged_exactly_as_before(monkeypatch):
    clip = render_basket_scene(ball_contrast=40.0)
    measured = check(clip, zone=ZONE)
    assert measured.flicker["reason"] == "below_threshold"
    real = trigger_check.ball_flicker_levels
    monkeypatch.setattr(
        trigger_check, "ball_flicker_levels", lambda frames, **kw: (None, real(frames, **kw)[1])
    )
    plain = check(clip, zone=ZONE)
    assert measured.frame_states == plain.frame_states
    assert (measured.decision, measured.reason) == (plain.decision, plain.reason)


def test_unlit_frames_are_skipped_by_the_decision():
    offsets = np.array([-30.0, -25.0, -20.0, -10.0, 0.5, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 30.0])
    states = "P-PP-G-GG-GG"
    decision, reason, departure_ms = trigger_check._decide(  # pylint: disable=protected-access
        states, offsets, trigger_check.WINDOW_MS
    )
    assert (decision, reason) == ("departed", "ball_left_spot")
    # The G frames at +4 and +12 ms are consecutive once the unlit +8 ms is skipped.
    assert departure_ms == 12.0
    assert trigger_check._struck(states, offsets, departure_ms)  # pylint: disable=protected-access


def test_only_unlit_frames_leave_too_few_post_frames():
    offsets = np.linspace(-30.0, 40.0, 12)
    decision, reason, _ = trigger_check._decide(  # pylint: disable=protected-access
        UNLIT_STATE * 12, offsets, trigger_check.WINDOW_MS
    )
    assert (decision, reason) == ("unclear", "insufficient_post_frames")


# --- flicker levels ------------------------------------------------------------------


def test_flicker_levels_follow_the_lamp():
    clip = render_floodlit_ball(leaves=False)
    levels, measured = flicker_levels(clip, roi_top=ZONE.y0)
    assert measured.reason == "detected" and measured.detected_by == "mat"
    assert not measured.compensated
    assert len(levels) == FRAMES
    wave = lamp_wave()
    assert np.corrcoef(levels, wave)[0, 1] > 0.99


def test_flicker_levels_are_none_for_a_calm_clip():
    levels, measured = flicker_levels(render_basket_scene(), roi_top=ZONE.y0)
    assert levels is None
    assert measured.reason == "below_threshold"
    assert measured.rel_std < FLICKER_MIN_REL_STD


@pytest.mark.parametrize(
    "frames", [np.zeros((0, 10, 10)), np.zeros((10, 10)), np.zeros((3, 50, 50))]
)
def test_flicker_levels_never_raise(frames):
    levels, measured = flicker_levels(frames, roi_top=None)
    assert levels is None
    assert measured.reason in ("invalid_frames", "too_few_frames")


# --- the capture runtime passes the server's mark --------------------------------------


def _runtime(tmp_path) -> CameraCaptureRuntime:
    return CameraCaptureRuntime(
        output_dir=tmp_path, settings=CameraCaptureSettings(width=WIDTH, height=HEIGHT)
    )


def _runtime_check(runtime: CameraCaptureRuntime, clip: np.ndarray) -> dict:
    return runtime._trigger_check(  # pylint: disable=protected-access
        1, clip, TIMESTAMPS, TIMESTAMPS, TRIGGER_NS, exposure_gain=EXPOSURE_GAIN
    )


def test_the_runtime_check_reads_the_mark_source(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.set_tee_zone(ZONE)
    clip = render_basket_scene()
    assert _runtime_check(runtime, clip)["reason"] == "zone_spot_static"
    runtime.set_ball_mark_source(lambda: MARK)
    stored = _runtime_check(runtime, clip)
    assert stored["ball_mark"] == [MARK[0], MARK[1]]
    assert (stored["decision"], stored["ball_source"]) == ("departed", "ball_mark")
    runtime.set_ball_mark_source(lambda: None)
    assert _runtime_check(runtime, clip)["ball_mark"] is None


def test_a_failing_mark_source_never_breaks_the_check(tmp_path, caplog):
    runtime = _runtime(tmp_path)
    runtime.set_tee_zone(ZONE)

    def broken():
        raise RuntimeError("no mark")

    runtime.set_ball_mark_source(broken)
    stored = _runtime_check(runtime, render_basket_scene())
    assert stored["ball_mark"] is None
    assert stored["reason"] == "zone_spot_static"
    assert "ball mark unavailable" in caplog.text
