"""Where the resting ball lay: the compact spot that disappeared at the strike.

The camera ball flight needs the ball's resting image for its departure origin,
its candidate exclusion and the camera pose. The legacy locator
(``club_motion.detect_reference_ball`` plus ``ReferenceBallTracker``) failed all
19 drivers of 2026-10-25: ball and rubber tee formed one ~15x27 px blob whose
centre sat 3 px low; the aspect ratio dropped it, the detector took the tree
line or the mat edge instead, and the tracker then anchored on the tee blob.
A 7-wood ball at 100 us / gain 2 differed from the mat by only 26-28 grey
levels, below the fixed 30 (departure) and 20 (contrast) grey thresholds.

The ball is the only compact thing that is there before the swing and gone once
the club has passed, so it is located in

    D = median(frames 0..PRE_FRAMES-1) - median(trigger + POST_WINDOW_FRAMES)

where the rubber tee (it stays in the mat) and static clutter (horizon, baskets,
mat corners) cancel. A disk matched filter over a few ball sizes finds the spot
inside the search box (the hitting zone of ``tee_zone`` when configured); a
circle fit to the radial edges of D over the upper ~280 deg (the bottom sector,
where the tee and the ball's shadow sit, is skipped) gives a sub-pixel centre
and diameter.

Gates are relative to the clip itself: the ball counts when its contrast is at
least ``SNR_MIN`` times the pixel noise of the pre-swing frames and at least
``CONTRAST_FLOOR`` grey levels. The contrast is the larger of the mean |D| of the
core's upper and lower half: a ball lit from above is brighter than the mat on
top and darker in its own shadow below (2026-11-02 at the daylight lock, +30 /
-20 grey), so the median |D| of the whole core sat on the zero crossing between
the halves (8-25 grey) and the ball failed the gate. "Still there"
for the tee-patch departure frame means a change below ``PRESENT_FRACTION`` of
that contrast. A ball sits on a tee when a narrow structure of the ball's
polarity continues below it in the late frames for at least ``TEED_FRACTION``
of its diameter.

The camera pose comes only from untee'd balls (``GroundBallAnchor``): a teed
ball's centre sits ~3 cm higher than the ball on the mat the pose assumes. A
ball on the driver's profile tee counts as teed whatever its tee rows say
(``ball_flight``): 6 of 19 drivers of 2026-11-02 showed only 3-6 tee rows.

Ball mark: when the owner tapped the resting ball in the Align view
(``angle_reference.BallMark``), the search box is the square of
``MARK_WINDOW_HALF_PX`` around the tap (``mark_window``), whatever the hitting
zone, and nothing outside it (a last ball, a session anchor, a ground anchor)
stands in for the ball (``openflight.camera.ball_flight``). The locator still
finds this shot's ball inside the window: every ball is placed by hand.
``MARK_WINDOW_HALF_PX`` = 64 adds up what lies between the tap and a later
ball: the tap itself (the ball is ~10 css px wide in the 1024x600 Align view,
1.66 frame px per css px; a fingertip lands within ~2 mm, 13 css px, so ~22
frame px; ~35 on a landscape phone) and the balls around the first one
(2026-10-25: up to 17 px, a driver teed 15 px above the irons; 2026-10-30
night: up to 36 px, the device turned by ~30 px after the 8th shot). The tree
crown the old anchor sat on that day (313.6/169.3, ~90 px above the balls)
stays out. A device turned further than that needs a new tap.

The 2026-10-25 range session (offline prototype, Toptracer as validation only)
located all 69 strikes; its 4 non-strikes stayed below SNR 1.3 (strikes: 18+).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import ndimage

from openflight.camera.club_delivery import BALL_PATCH_RADIUS_FRAC, _detect_impact_index
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.tee_zone import TeeZone

PRE_FRAMES = 20
# The reference frames end this many frames (~35 ms) before the trigger, before
# the club head reaches the hitting zone.
MIN_PRE_GAP_FRAMES = 10
# ~52-156 ms after the trigger at 288 fps: the club head has left the tee area.
POST_WINDOW_FRAMES = (15, 45)
MIN_POST_FRAMES = 8
# The trigger frame lands this many frames after the pre-trigger count at most
# (2026-10-25 range clips: +1 frame typical, up to +6), which shortens the
# clip past the trigger by as much.
TRIGGER_LAG_FRAMES = 6
# Post-trigger frames a clip must be captured with for the shortest usable
# late window, even when the trigger frame lands TRIGGER_LAG_FRAMES late.
MIN_CLIP_POST_FRAMES = POST_WINDOW_FRAMES[0] + MIN_POST_FRAMES + TRIGGER_LAG_FRAMES
MATCHED_FILTER_SCALES = (0.8, 0.9, 1.0, 1.15)
MATCHED_FILTER_RADIUS_FRAC = 0.4
EDGE_ANGLE_STEP_DEG = 10
EXCLUDE_BOTTOM_DEG = 40.0
EDGE_RADIUS_FRAC = (0.15, 1.0)
EDGE_RADIUS_STEP_PX = 0.25
EDGE_SMOOTHING_SIGMA_PX = 0.8
MIN_EDGE_POINTS = 8
EDGE_OUTLIER_FACTOR = 2.5
EDGE_OUTLIER_MIN_PX = 1.0
# Fitted diameter relative to the expected one.
SIZE_RANGE = (0.6, 1.5)
CORE_RADIUS_FRAC = 0.4
SNR_MIN = 8.0
CONTRAST_FLOOR = 8.0
NOISE_FLOOR = 0.5
PRESENT_FRACTION = 0.5
TEED_FRACTION = 0.5
TEE_MIN_STEP = 4.0
TEE_NOISE_STEPS = 3.0
# Without a hitting zone: a ~180x85 px box around the tee estimate.
DEFAULT_BOX_HALF_WIDTH_PX = 90
DEFAULT_BOX_ABOVE_PX = 45
DEFAULT_BOX_BELOW_PX = 40
GROUND_ANCHOR_SAMPLES = 15
GROUND_ANCHOR_MIN_SAMPLES = 3
# Half the side of the square searched around the tapped ball (see the module docstring).
MARK_WINDOW_HALF_PX = 64


@dataclass(frozen=True)
class DepartedBall:
    """The resting ball found as what disappeared (full-frame pixels, grey levels)."""

    x: float
    y: float
    diameter_px: float
    contrast: float  # the larger mean |pre - post| of the ball core's upper and lower half
    noise: float  # median per-pixel temporal noise of the core in the pre frames
    snr: float
    polarity: int  # -1: the ball is darker than what it hid; +1 brighter
    fit_rms_px: float
    n_edge: int
    tee_px: float  # rows of tee below the ball in the late frames

    @property
    def area_px(self) -> int:
        """Disk area of the fitted diameter."""
        return int(round(math.pi * (self.diameter_px / 2.0) ** 2))

    @property
    def teed(self) -> bool:
        """Whether a tee continues below the ball for half its diameter or more."""
        return self.tee_px >= TEED_FRACTION * self.diameter_px

    def reference(self) -> ReferenceBall:
        """The ball as the estimators' ``ReferenceBall``."""
        return ReferenceBall(self.x, self.y, self.diameter_px, self.area_px)

    def gate_failure(self) -> str | None:
        """None when the ball stands out of the clip's noise, else why it does not."""
        if self.contrast < CONTRAST_FLOOR:
            return "low_contrast"
        if self.snr < SNR_MIN:
            return "low_snr"
        return None


def _disk_kernel(radius: float) -> np.ndarray:
    extent = int(math.ceil(radius))
    yy, xx = np.mgrid[-extent : extent + 1, -extent : extent + 1]
    kernel = (xx * xx + yy * yy <= radius * radius).astype(np.float32)
    return kernel / kernel.sum()


def _circle_fit(px: np.ndarray, py: np.ndarray) -> tuple[float, float, float, float]:
    design = np.column_stack([px, py, np.ones_like(px)])
    solution, *_ = np.linalg.lstsq(design, px * px + py * py, rcond=None)
    cx, cy = solution[0] / 2.0, solution[1] / 2.0
    radius = math.sqrt(max(solution[2] + cx * cx + cy * cy, 1e-9))
    rms = float(np.sqrt(np.mean((np.hypot(px - cx, py - cy) - radius) ** 2)))
    return float(cx), float(cy), float(radius), rms


def _edge_points(smoothed: np.ndarray, x: float, y: float, diameter: float):
    """Outermost-falling |D| along rays over the upper sector (crop pixels)."""
    angles = np.arange(0, 360, EDGE_ANGLE_STEP_DEG, dtype=float)
    angles = angles[np.abs(angles - 180.0) >= EXCLUDE_BOTTOM_DEG]
    theta = np.radians(angles)
    dx, dy = np.sin(theta), -np.cos(theta)
    radii = np.arange(
        EDGE_RADIUS_FRAC[0] * diameter, EDGE_RADIUS_FRAC[1] * diameter, EDGE_RADIUS_STEP_PX
    )
    rows = y + radii[None, :] * dy[:, None]
    cols = x + radii[None, :] * dx[:, None]
    profiles = ndimage.map_coordinates(
        smoothed, [rows.ravel(), cols.ravel()], order=1, mode="nearest"
    ).reshape(rows.shape)
    falloff = -np.gradient(np.abs(profiles), axis=1)
    edge = np.argmax(falloff, axis=1)
    found = (falloff[np.arange(len(edge)), edge] > 0.0) & (edge > 0) & (edge < len(radii) - 1)
    return x + radii[edge[found]] * dx[found], y + radii[edge[found]] * dy[found]


def _tee_rows(
    post: np.ndarray, centre: tuple[float, float], radius: float, polarity: int, step: float
) -> float:
    """Rows below the ball where a narrow structure of the ball's polarity continues.

    ``step`` is how far (grey levels) the column must differ from its sides.
    """
    x, y = centre
    col = int(round(x))
    top, bottom = int(round(y + radius)), int(round(y + 4.0 * radius))
    width = post.shape[1]
    left = post[top:bottom, max(0, col - int(radius) - 4) : max(1, col - int(radius) - 1)]
    right = post[top:bottom, min(width, col + int(radius) + 1) : col + int(radius) + 4]
    centre = post[top:bottom, max(0, col - 1) : col + 2]
    if 0 in (left.size, right.size, centre.size):
        return 0.0
    side = 0.5 * (left.mean(axis=1) + right.mean(axis=1))
    middle = centre.mean(axis=1)
    rise = (side - middle) if polarity < 0 else (middle - side)
    # The first run of rows past the step, wherever below the ball it starts.
    run = 0
    for value in rise:
        if value > step:
            run += 1
        elif run:
            break
    return float(run)


def _matched_filter_peak(
    change: np.ndarray, box: tuple[int, int, int, int], expected_diameter_px: float
) -> tuple[float, int, int, float]:
    """Strongest disk response (either sign) inside ``box`` over the ball sizes tried.

    Returns the response, its column and row in ``change`` and the diameter;
    the first scale wins a tie.
    """
    import cv2  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    x0, y0, x1, y1 = box
    peaks = []
    for scale in MATCHED_FILTER_SCALES:
        diameter = expected_diameter_px * scale
        response = cv2.filter2D(  # pylint: disable=no-member
            change,
            -1,
            _disk_kernel(MATCHED_FILTER_RADIUS_FRAC * diameter),
            borderType=cv2.BORDER_REPLICATE,  # pylint: disable=no-member
        )[y0:y1, x0:x1]
        row, col = divmod(int(np.argmax(np.abs(response))), response.shape[1])
        peaks.append((float(response[row, col]), col + x0, row + y0, diameter))
    return max(peaks, key=lambda peak: abs(peak[0]))


def _core_contrast(magnitude: np.ndarray, core: np.ndarray, yy: np.ndarray) -> float:
    """The larger mean |D| of the core's upper and lower half (the centre row is in neither).

    ``magnitude`` is |D| on the grid around the fitted centre, ``core`` its core
    mask and ``yy`` the grid's row offsets. A core too small for two halves
    falls back to the median |D| of the core.
    """
    halves = [magnitude[core & (yy < 0)], magnitude[core & (yy > 0)]]
    means = [float(half.mean()) for half in halves if half.size]
    if not means:
        return float(np.median(magnitude[core]))
    return max(means)


def locate_departed_ball(
    frames: np.ndarray,
    trigger_index: int,
    *,
    expected_diameter_px: float,
    box: tuple[int, int, int, int],
) -> tuple[DepartedBall | None, str]:
    """The spot that disappeared inside ``box`` (x0, y0, x1, y1), and why not if None.

    Reasons: ``located``, ``insufficient_pre_frames``,
    ``insufficient_post_frames``, ``empty_search_box``, ``no_ball_edge``,
    ``implausible_ball_size``. Only the box plus a margin of
    the ball's reach is read. The contrast gate is ``DepartedBall.gate_failure``.
    """
    count, height, width = frames.shape
    start = trigger_index + POST_WINDOW_FRAMES[0]
    end = min(count, trigger_index + POST_WINDOW_FRAMES[1])
    if trigger_index < PRE_FRAMES + MIN_PRE_GAP_FRAMES:
        return None, "insufficient_pre_frames"
    if end - start < MIN_POST_FRAMES:
        return None, "insufficient_post_frames"
    bx0, by0 = max(0, int(box[0])), max(0, int(box[1]))
    bx1, by1 = min(width, int(box[2])), min(height, int(box[3]))
    if bx1 - bx0 < 2 or by1 - by0 < 2:
        return None, "empty_search_box"
    margin = int(math.ceil(2.0 * SIZE_RANGE[1] * expected_diameter_px)) + 4
    cx0, cy0 = max(0, bx0 - margin), max(0, by0 - margin)
    cx1, cy1 = min(width, bx1 + margin), min(height, by1 + margin)
    pre_stack = frames[:PRE_FRAMES, cy0:cy1, cx0:cx1]
    pre = np.median(pre_stack, axis=0)
    post = np.median(frames[start:end, cy0:cy1, cx0:cx1], axis=0)
    change = (pre - post).astype(np.float32)

    value, peak_x, peak_y, diameter = _matched_filter_peak(
        change, (bx0 - cx0, by0 - cy0, bx1 - cx0, by1 - cy0), expected_diameter_px
    )
    # D = pre - post: a ball darker than what it hid leaves D < 0.
    polarity = -1 if value < 0.0 else 1

    smoothed = ndimage.gaussian_filter(change, EDGE_SMOOTHING_SIGMA_PX)
    edge_x, edge_y = _edge_points(smoothed, float(peak_x), float(peak_y), diameter)
    if len(edge_x) < MIN_EDGE_POINTS:
        return None, "no_ball_edge"
    centre_x, centre_y, radius, rms = _circle_fit(edge_x, edge_y)
    residual = np.abs(np.hypot(edge_x - centre_x, edge_y - centre_y) - radius)
    keep = residual <= max(EDGE_OUTLIER_MIN_PX, EDGE_OUTLIER_FACTOR * float(np.median(residual)))
    if keep.sum() >= MIN_EDGE_POINTS:
        centre_x, centre_y, radius, rms = _circle_fit(edge_x[keep], edge_y[keep])
    fitted = 2.0 * radius
    if not SIZE_RANGE[0] * expected_diameter_px <= fitted <= SIZE_RANGE[1] * expected_diameter_px:
        return None, "implausible_ball_size"

    reach = int(math.ceil(fitted)) + 1
    yy, xx = np.mgrid[-reach : reach + 1, -reach : reach + 1]
    cols = np.clip(np.round(centre_x + xx).astype(int), 0, change.shape[1] - 1)
    rows = np.clip(np.round(centre_y + yy).astype(int), 0, change.shape[0] - 1)
    core = np.hypot(xx, yy) <= CORE_RADIUS_FRAC * fitted
    contrast = _core_contrast(np.abs(change[rows, cols]), core, yy)
    core_pixels = pre_stack[:, rows[core], cols[core]].astype(np.float32)
    noise = float(np.median(core_pixels.std(axis=0)))
    tee_px = _tee_rows(
        post,
        (centre_x, centre_y),
        radius,
        polarity,
        max(TEE_MIN_STEP, TEE_NOISE_STEPS * noise),
    )
    return (
        DepartedBall(
            x=centre_x + cx0,
            y=centre_y + cy0,
            diameter_px=fitted,
            contrast=contrast,
            noise=noise,
            snr=contrast / max(noise, NOISE_FLOOR),
            polarity=polarity,
            fit_rms_px=rms,
            n_edge=int(keep.sum()),
            tee_px=tee_px,
        ),
        "located",
    )


def mark_window(
    shape: tuple[int, int], mark_px: tuple[float, float]
) -> tuple[int, int, int, int] | None:
    """The square (x0, y0, x1, y1) searched around the tapped ball, None when it misses the frame."""
    height, width = shape
    x, y = mark_px
    box = (
        max(0, math.floor(x - MARK_WINDOW_HALF_PX)),
        max(0, math.floor(y - MARK_WINDOW_HALF_PX)),
        min(width, math.ceil(x + MARK_WINDOW_HALF_PX) + 1),
        min(height, math.ceil(y + MARK_WINDOW_HALF_PX) + 1),
    )
    if box[2] - box[0] < 2 or box[3] - box[1] < 2:
        return None
    return box


def in_window(window: tuple[int, int, int, int], ball: ReferenceBall) -> bool:
    """Whether the ball's centre lies inside ``window`` (x0, y0, x1, y1)."""
    x0, y0, x1, y1 = window
    return x0 <= ball.x < x1 and y0 <= ball.y < y1


def search_box(
    shape: tuple[int, int],
    *,
    zone: TeeZone | None,
    tee_estimate: ReferenceBall | None,
    window: tuple[int, int, int, int] | None = None,
) -> tuple[tuple[int, int, int, int], float] | None:
    """Where to look for the departed ball and its expected diameter, None without a hint.

    The window around the tapped ball (``mark_window``) when there is one, else
    the hitting zone when it matches the frame size, else a ~180x85 px box around
    ``tee_estimate`` (the last located ball or the session's tee anchor). The
    expected diameter is the zone's when it matches the frame, else the
    estimate's.
    """
    height, width = shape
    zone_fits = zone is not None and (zone.image_width_px, zone.image_height_px) == (width, height)
    if window is not None:
        if zone_fits:
            return window, float(zone.diameter_px)
        return (window, float(tee_estimate.diameter_px)) if tee_estimate is not None else None
    if zone_fits:
        return (zone.x0, zone.y0, zone.x1, zone.y1), float(zone.diameter_px)
    if tee_estimate is None:
        return None
    x, y = int(round(tee_estimate.x)), int(round(tee_estimate.y))
    box = (
        max(0, x - DEFAULT_BOX_HALF_WIDTH_PX),
        max(0, y - DEFAULT_BOX_ABOVE_PX),
        min(width, x + DEFAULT_BOX_HALF_WIDTH_PX),
        min(height, y + DEFAULT_BOX_BELOW_PX),
    )
    return box, float(tee_estimate.diameter_px)


def departure_index(frames: np.ndarray, ball: DepartedBall, trigger_index: int) -> int | None:
    """Last frame the ball's core is still there: changed by under PRESENT_FRACTION x contrast.

    ``club_delivery._detect_impact_index`` on a crop around the ball, with the
    clip's own contrast in place of its fixed 30 grey levels.
    """
    radius = max(3, int(round(ball.diameter_px * BALL_PATCH_RADIUS_FRAC))) + 1
    height, width = frames.shape[1:]
    x0, y0 = max(0, int(ball.x) - radius), max(0, int(ball.y) - radius)
    x1, y1 = min(width, int(ball.x) + radius + 2), min(height, int(ball.y) + radius + 2)
    local = ReferenceBall(ball.x - x0, ball.y - y0, ball.diameter_px, ball.area_px)
    return _detect_impact_index(
        frames[:, y0:y1, x0:x1],
        local,
        trigger_index=trigger_index,
        present_delta=PRESENT_FRACTION * ball.contrast,
    )


def min_post_ms(fps: float) -> int:
    """Shortest post-trigger capture (ms, rounded up) that holds MIN_CLIP_POST_FRAMES at ``fps``.

    Shorter clips make the locator fall back to legacy (``insufficient_post_frames``)
    whenever the trigger frame lands late in the clip (``TRIGGER_LAG_FRAMES``).
    """
    return math.ceil(1000.0 * MIN_CLIP_POST_FRAMES / fps)


def _median_ball(samples: list[ReferenceBall]) -> ReferenceBall:
    return ReferenceBall(
        x=float(np.median([ball.x for ball in samples])),
        y=float(np.median([ball.y for ball in samples])),
        diameter_px=float(np.median([ball.diameter_px for ball in samples])),
        area_px=int(round(np.median([ball.area_px for ball in samples]))),
    )


class GroundBallAnchor:
    """Session pose source: rolling median of untee'd departed balls.

    The camera pose assumes the ball on the mat; a teed ball (driver, woods)
    sits ~3 cm higher and never moves it. ``last`` is the latest departed ball,
    teed or not, where the search box centres without a hitting zone.
    """

    def __init__(
        self,
        max_samples: int = GROUND_ANCHOR_SAMPLES,
        min_samples: int = GROUND_ANCHOR_MIN_SAMPLES,
    ):
        self.max_samples = max_samples
        self.min_samples = min_samples
        self._samples: list[ReferenceBall] = []
        self._last: ReferenceBall | None = None

    def observe(self, ball: DepartedBall, *, teed: bool | None = None) -> bool:
        """Learn from one departed ball; only untee'd balls feed the pose.

        ``teed`` is whether the ball sits on a tee; None reads its own tee rows
        (``DepartedBall.teed``). Returns whether the ball fed the pose median.
        """
        reference = ball.reference()
        self._last = reference
        if ball.teed if teed is None else teed:
            return False
        self._samples = (self._samples + [reference])[-self.max_samples :]
        return True

    def anchor(self) -> ReferenceBall | None:
        """Median untee'd ball once ``min_samples`` were seen, else None."""
        if len(self._samples) < self.min_samples:
            return None
        return _median_ball(self._samples)

    @property
    def last(self) -> ReferenceBall | None:
        """The latest departed ball, teed or not."""
        return self._last

    @property
    def samples(self) -> int:
        """Untee'd balls currently in the median."""
        return len(self._samples)

    def adopt(self, other: GroundBallAnchor) -> None:
        """Take over another anchor's samples, e.g. a copy a CV worker updated."""
        self._samples = list(other._samples)  # pylint: disable=protected-access
        self._last = other._last  # pylint: disable=protected-access


__all__ = [
    "MARK_WINDOW_HALF_PX",
    "DepartedBall",
    "GroundBallAnchor",
    "departure_index",
    "in_window",
    "locate_departed_ball",
    "mark_window",
    "min_post_ms",
    "search_box",
]
