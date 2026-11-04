"""The departure scan beside a static spot: which spots it judges and how it models them.

Synthetic scenes for the stale-anchor gap (2026-10-26 review): a ball leaving
beside a static spot 1.5-2x as dark, or one 16-17 px away in the ball's mat
ring, must still be seen leaving so the blocked edge contradicts the anchor.
"""

import math

import numpy as np
import pytest
from scipy import ndimage
from test_camera_trigger_check import (
    BALL,
    DIAMETER,
    EXPOSURE_GAIN,
    FRAMES,
    HEIGHT,
    RADIUS,
    TIMESTAMPS,
    TRIGGER_NS,
    WIDTH,
    ZONE,
    render_scene,
)

from openflight.camera import trigger_check
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.trigger_check import check_ball_departure


def judging_view() -> tuple[np.ndarray, tuple[np.ndarray, np.ndarray], np.ndarray]:
    """Reference indices, the judged view and the decision-window indices of the clips."""
    offsets = (TIMESTAMPS - TRIGGER_NS) / 1e6
    reference_idx = np.flatnonzero(offsets <= trigger_check.REFERENCE_END_MS)[
        : trigger_check.REFERENCE_FRAMES
    ]
    view_idx = np.flatnonzero(
        (offsets >= trigger_check.EARLY_DEPARTURE_MS[0]) & (offsets <= trigger_check.WINDOW_MS[1])
    )
    window = offsets[view_idx]
    window_idx = view_idx[
        (window >= trigger_check.WINDOW_MS[0]) & (window <= trigger_check.WINDOW_MS[1])
    ]
    return reference_idx, (view_idx, offsets[view_idx]), window_idx


def render_soft_ball_beside(darker: float, offset=(-12.0, -12.0)) -> tuple[np.ndarray, tuple]:
    """A dark ball (mat 110, ball 25) and a static spot ``darker`` x as dark, both blurred.

    The blur (sigma 1.5 px, like the real optics) spreads the ball's change
    across the edge beyond half a diameter: spots on its shoulder changed a
    lot but hold almost no contrast of their own.
    """
    static = (BALL[0] + offset[0], BALL[1] + offset[1], RADIUS + 0.5, 110.0 - darker * 85.0)
    clip = render_scene(mat=110.0, ball=25.0, patches=[static])
    blurred = ndimage.gaussian_filter(clip.astype(np.float32), sigma=(0.0, 1.5, 1.5))
    return np.clip(blurred, 0, 255).astype(np.uint8), static


@pytest.mark.parametrize("darker", [1.5, 2.0])
def test_changed_spots_holds_only_spots_the_departure_scan_can_judge(darker):
    """Shoulder spots of the ball's own change blob are too faint to judge.

    Their change over a contrast of 1-12 ranked them far above the ball;
    skipped in the ranking loop they still used up its ZONE_CANDIDATES slots.
    """
    clip, static = render_soft_ball_beside(darker)
    reference_idx, _view, window_idx = judging_view()
    judged = ReferenceBall(static[0], static[1], DIAMETER, 177)
    spots = trigger_check._changed_spots(  # pylint: disable=protected-access
        clip,
        reference_idx,
        window_idx,
        box=trigger_check._zone_box(ZONE),  # pylint: disable=protected-access
        diameter_px=ZONE.diameter_px,
        judged=judged,
    )
    assert any(math.hypot(spot.x - BALL[0], spot.y - BALL[1]) <= 2.0 for spot, _c in spots)
    for spot, _contrast in spots:
        model = trigger_check._spot_beside(  # pylint: disable=protected-access
            clip, spot, reference_idx, judged
        )
        assert model is not None and model[0].failure() is None


@pytest.mark.parametrize("darker", [1.5, 2.0])
def test_faint_changed_spots_never_crowd_the_ball_out_of_the_judged_slots(monkeypatch, darker):
    """The ball must stay among the judged candidates however many faint spots changed.

    On a 10-25 clip (183319_640_087, static copy 20 px beside, 1.5x as dark)
    four faint spots filled all four slots, the ball ranked fifth and the
    stale anchor blocked one more strike. The synthetic scene has three such
    spots, so the slots are narrowed to three here.
    """
    monkeypatch.setattr(trigger_check, "ZONE_CANDIDATES", 3)
    clip, static = render_soft_ball_beside(darker)
    reference_idx, view, _window_idx = judging_view()
    left = trigger_check._departed_zone_spot(  # pylint: disable=protected-access
        clip,
        reference_idx,
        view,
        window_ms=trigger_check.WINDOW_MS,
        zone=ZONE,
        judged=ReferenceBall(static[0], static[1], DIAMETER, 177),
        judged_static=True,
    )
    assert left is not None
    assert math.hypot(left[0].x - BALL[0], left[0].y - BALL[1]) <= 4.0


# Fixed mat texture (grey-level sigma 12, the same in every frame): the ring
# median then moves when a static spot covers part of the ring.
MAT_TEXTURE = 12.0 * np.random.default_rng(3).normal(0.0, 1.0, (HEIGHT, WIDTH))


def textured(clip: np.ndarray) -> np.ndarray:
    """``clip`` with the fixed mat texture added to every frame."""
    return np.clip(clip.astype(np.float32) + MAT_TEXTURE, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("offset", [(0.0, 17.0), (12.0, 12.0), (-17.0, 0.0)])
def test_the_departure_contrast_leaves_out_a_static_spot_in_the_balls_ring(offset):
    """A static spot 2x as dark 17 px from the ball lies in the ball's mat ring.

    Both the departure scan's ranking loop and the check's re-judge of a zone
    departure model the ball with its ring cleared of the judged spot
    (``_spot_beside``): the departure carries the ball's own contrast, which
    the tee anchor learns, not one lowered by the static spot. Without the
    clearing the contrast came out ~2.5 grey levels low here; on 10-25 clips
    the uncleared ring hid the departure and a stale anchor blocked up to 7
    strikes instead of 2.
    """
    static = (BALL[0] + offset[0], BALL[1] + offset[1], RADIUS + 0.5, 100.0 - 2.0 * 25.0)
    clip = textured(render_scene(patches=[static]))
    reference_idx, view, _window_idx = judging_view()
    alone = trigger_check._spot_at(  # pylint: disable=protected-access
        textured(render_scene()), ReferenceBall(*BALL, DIAMETER, 177), reference_idx
    )[0].signed_contrast

    left = trigger_check._departed_zone_spot(  # pylint: disable=protected-access
        clip,
        reference_idx,
        view,
        window_ms=trigger_check.WINDOW_MS,
        zone=ZONE,
        judged=ReferenceBall(static[0], static[1], DIAMETER, 177),
        judged_static=True,
    )
    assert left is not None and math.hypot(left[0].x - BALL[0], left[0].y - BALL[1]) <= 2.0
    assert left[1] == pytest.approx(alone, abs=1.0)

    # No hint: the zone search locates the static spot (the strongest), judges
    # it still_there and re-judges the zone spot that left.
    result = check_ball_departure(
        clip, TIMESTAMPS, TRIGGER_NS, zone=ZONE, exposure_gain=np.full(FRAMES, EXPOSURE_GAIN)
    )
    assert (result.ball_source, result.decision) == ("zone", "departed")
    assert math.hypot(result.ball_x - BALL[0], result.ball_y - BALL[1]) <= 2.0
    assert result.pre_contrast == pytest.approx(abs(alone), abs=1.0)
    assert result.departure_contrast == pytest.approx(alone, abs=1.0)


def test_spot_beside_clears_the_judged_disk_from_the_ring():
    """Ring pixels within JUDGED_CLEAR_FRAC diameters of the judged spot are left out."""
    clip = render_scene()
    reference_idx, _view, _window_idx = judging_view()
    ball = ReferenceBall(*BALL, DIAMETER, 177)
    judged = ReferenceBall(BALL[0], BALL[1] + 17.0, DIAMETER, 177)
    full = trigger_check._spot_at(clip, ball, reference_idx)  # pylint: disable=protected-access
    beside = trigger_check._spot_beside(  # pylint: disable=protected-access
        clip, ball, reference_idx, judged
    )
    assert beside is not None
    assert np.array_equal(beside[2], full[2])  # same core disk
    ring_full, ring_beside = full[3], beside[3]
    assert not np.any(ring_beside & ~ring_full)
    dropped = ring_full & ~ring_beside
    assert np.any(dropped)
    box = trigger_check._masks(ball, clip.shape[1:])[0]  # pylint: disable=protected-access
    yy, xx = np.mgrid[box[0], box[1]]
    distance = np.hypot(xx - judged.x, yy - judged.y)
    clear = trigger_check.JUDGED_CLEAR_FRAC * judged.diameter_px
    assert np.all(distance[dropped] <= clear)
    assert np.all(distance[ring_beside] > clear)
    assert (
        trigger_check._spot_beside(  # pylint: disable=protected-access
            clip, ball, reference_idx, None
        )[3].sum()
        == ring_full.sum()
    )
    # A judged spot that covers the whole ring leaves too few pixels to judge.
    huge = ReferenceBall(BALL[0], BALL[1], 4.0 * DIAMETER, 177)
    assert (
        trigger_check._spot_beside(  # pylint: disable=protected-access
            clip, ball, reference_idx, huge
        )
        is None
    )
