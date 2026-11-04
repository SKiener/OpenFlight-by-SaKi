"""Mains-light flicker: detect it in a clip and remove it from the static scene.

Lamps on mains flicker at twice the mains frequency (100 Hz in Europe). The
camera's short exposure (1.25 ms on 2026-10-30, 0.35 ms on 2026-11-02) samples
that flicker at a different phase in every frame, so whatever the lamp lights
swings from frame to frame while the unlit sky stays constant: under floodlight
(2026-10-30) the mat and the ball by a factor of about 2.7, in daylight with a
mains lamp at the tee (2026-11-02) the white ball by +-35-40 % but the dark mat
next to it by only +-2-7 grey. The disappeared-ball locator measures its noise
as the temporal spread of the ball core over the reference frames; the flicker
inflated it (SNR 1.5-2.2 under floodlight, 0.5-1.7 by day, gate 8) and the
clips were rejected (analysis/lowlight-20261031/bericht.md,
analysis/range-20261102/kamera/bericht-kamera.md).

``compensate_flicker`` models every pixel as ``a + c * w_k``: ``w_k`` is one
global flicker waveform (the median over a set of lit pixels of frame k
divided by their reference mean; for the mat, every ``FLICKER_LEVEL_STRIDE``-th
row and column of its lit pixels), ``c`` a per-pixel gain fitted over the
reference frames (about 0 for the unlit sky, large for the lit ball and mat).
Each frame becomes ``frame_k - c * (w_k - mean w)``: what stands still stops
flickering, what moves (club, ball in flight) keeps its own brightness.

The flicker is measured twice:

* on the mat: the lit pixels below the hitting zone's top edge;
* at the ball: in the window around the resting ball (``flicker_window``: the
  ball mark's window, else the hitting zone), the ``FLICKER_BALL_FRACTION`` of
  its lit pixels that vary most over the reference frames. A flickering ball
  is what varies most there; a calm window leaves only noise, whose median
  cancels. Flicker changes from every frame to the next (it is sampled at a new
  phase each time), what the golfer moves through the window changes slowly, so
  the ball waveform also needs a frame-to-frame step of
  ``FLICKER_MIN_BALL_STEP_RATIO``.

A clip is compensated when either waveform swings by ``FLICKER_MIN_REL_STD`` or
more (std / mean over the reference frames). The compensation always uses the
mat's waveform: it is the median of tens of thousands of pixels, it stays valid
after the strike (the ball's pixels change when the ball leaves), and the mat
carries the lamp's phase even where it swings far less than the ball
(2026-11-02 #1-12: 0.019-0.043, 4.5x its calm-day maximum; compensated with it,
the replay located these balls). A lamp that lit only the ball and left the mat
entirely calm would be detected but not removed. Below the threshold the
frames come back unchanged (the same array object), so a calm clip runs
exactly as without this step.

``compensate_ball_flicker`` is that step as the ball path runs it, live
(``ball_flight.estimate_live_ball_flight``) and in the low-light shadow
(``camera.lowlight``). ``ball_flicker_levels`` measures the same way but only
returns the mat waveform of every frame, for the trigger check, which compares
each frame with reference frames of the same lamp level (``trigger_check``).

Assumes one common waveform. Lamps on different phases (three-phase supply)
could leave a residue; the locator's own noise gate still decides.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from openflight.camera.departed_ball import mark_window

# Frames the waveform's mean and the per-pixel gain are fitted over (pre-swing).
FLICKER_REF_FRAMES = 20
# Fewer reference frames than this cannot separate flicker from noise.
FLICKER_MIN_REF_FRAMES = 6
# A pixel lit at least this bright (reference mean, grey) joins a waveform.
FLICKER_LIT_MIN = 20.0
# Fewer lit pixels than this give no mat waveform (an almost black hitting area).
FLICKER_MIN_LIT_PIXELS = 200
# Compensate when the mat's or the ball's waveform swings by this much (std / mean
# over the reference frames). Measured per clip (analysis/range-20261102/kamera):
#   2026-10-25 daylight, no flicker (73 clips): mat <= 0.0042; ball <= 0.0097, or
#     0.019-0.119 where the golfer moved through the window (step ratio <= 0.06);
#   2026-11-02 daylight, mains lamp at the tee (64 clips): mat 0.019-0.043 on
#     #1-12 (missed: the ball itself swung by +-35-40 %), 0.051-0.162 on #13-65;
#     ball 0.126-0.365 (step ratio 0.76-1.66);
#   2026-10-30 floodlight (49 clips): mat 0.33-0.38, ball 0.42-0.47.
# The ball measure sits 5x above the calm daylight noise and 2.5x below the
# flickering minimum at this threshold. Replay through the shot path (runs/t1_*):
# 2026-11-02 session 1 compensates 64/64 clips with frames (11 found by the ball
# alone) and gets 31 camera start directions (was 0), camera - Toptracer MAE
# 1.81 deg on 30; 2026-10-25 compensates none, its daylight result is unchanged.
FLICKER_MIN_REL_STD = 0.05
# The ball waveform: this share of the ball window's lit pixels, the ones whose
# reference frames vary most (about the ball's area in a 129 px mark window), and
# at least FLICKER_MIN_BALL_PIXELS of them.
FLICKER_BALL_FRACTION = 0.01
FLICKER_MIN_BALL_PIXELS = 20
# The ball waveform's mean squared frame-to-frame step over twice its variance:
# 1 - cos(phase step) for flicker (1.57 at 288 fps, 0.74 at 479 fps for 100 Hz;
# about 1 for noise), 0.02-0.06 for a club or shadow moving slowly through the
# window (2026-10-25). Below it the ball waveform does not count as flicker.
FLICKER_MIN_BALL_STEP_RATIO = 0.3
# ``flicker_levels`` takes the mat waveform over every FLICKER_LEVEL_STRIDE-th
# row and column of the lit mat: a 64th of its pixels, ~1700 at 640x400 below the
# zone top, ~950 in the lower half at 640x200. On a 2026-11-02 clip the mat
# waveform's rel_std came out the same at strides 4, 6 and 8 (0.052); the step
# took 4.7, 2.6 and 1.9 ms on a Mac (~3.6x that on the Pi). ``compensate_flicker``
# takes its mat waveform on the same stride (the per-pixel gain stays full
# resolution): the full-resolution median over every lit mat pixel per frame was
# about 2/3 of the live step, 140-230 ms on a Mac for a compensated 2026-11-02
# clip (0.5-0.8 s on the Pi before the shot is shown).
FLICKER_LEVEL_STRIDE = 8


@dataclass(frozen=True)
class FlickerMeasurement:  # pylint: disable=too-many-instance-attributes
    """What the flicker step measured in one clip and whether it changed the frames.

    ``reason`` is ``compensated``, ``below_threshold`` (no flicker worth
    removing), ``too_few_frames``, ``no_lit_pixels`` or ``invalid_frames``;
    ``flicker_levels`` reports ``detected`` (flicker found, frames unchanged)
    in place of ``compensated``.
    ``rel_std`` is the mat waveform's std / mean over the reference frames
    (None when it could not be measured), ``lit_pixels`` its pixel count.
    ``ball_rel_std``, ``ball_step_ratio`` and ``ball_pixels`` are the ball
    waveform's, measured in ``window`` (x0, y0, x1, y1; all None/0 without a
    window or with too few lit pixels in it). ``detected_by`` names the
    measurement that crossed the threshold (``mat``, else ``ball``), None when
    neither did; the compensation itself always uses the mat's waveform.
    ``elapsed_ms`` is the step's wall time.
    """

    compensated: bool
    reason: str
    rel_std: float | None = None
    reference_frames: int = 0
    lit_pixels: int = 0
    roi_top: int | None = None
    window: tuple[int, int, int, int] | None = None
    ball_rel_std: float | None = None
    ball_step_ratio: float | None = None
    ball_pixels: int = 0
    detected_by: str | None = None
    elapsed_ms: float | None = None

    def to_dict(self) -> dict:
        """JSON-ready form for the session log."""
        return {
            "compensated": self.compensated,
            "reason": self.reason,
            "rel_std": _rounded(self.rel_std, 4),
            "reference_frames": self.reference_frames,
            "lit_pixels": self.lit_pixels,
            "roi_top": self.roi_top,
            "window": None if self.window is None else list(self.window),
            "ball_rel_std": _rounded(self.ball_rel_std, 4),
            "ball_step_ratio": _rounded(self.ball_step_ratio, 3),
            "ball_pixels": self.ball_pixels,
            "detected_by": self.detected_by,
            "elapsed_ms": _rounded(self.elapsed_ms, 1),
        }


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


class _Waveform(NamedTuple):
    """One pixel set's flicker waveform over the reference frames."""

    rows: slice
    cols: slice
    pixels: np.ndarray  # boolean mask inside rows x cols
    ref_pixels: np.ndarray  # their reference means
    reference: np.ndarray  # w_k of the reference frames
    rel_std: float
    step_ratio: float

    @property
    def count(self) -> int:
        """Pixels the waveform is the median over."""
        return int(self.pixels.sum())

    def level(self, frame: np.ndarray) -> float:
        """w_k of one frame: the median ratio of the pixels to their reference mean."""
        values = frame[self.rows, self.cols][self.pixels].astype(np.float32)
        return float(np.median(values / self.ref_pixels))


# Rows, columns and a boolean mask inside them: the pixels one waveform is the median over.
_PixelSet = tuple[slice, slice, np.ndarray]


def _waveform(
    clip: np.ndarray, ref_mean: np.ndarray, pixel_set: _PixelSet, ref_n: int
) -> _Waveform | None:
    """The waveform of ``pixel_set`` over the reference frames, None without brightness."""
    rows, cols, pixels = pixel_set
    ref_pixels = ref_mean[rows, cols][pixels]
    probe = _Waveform(rows, cols, pixels, ref_pixels, np.empty(0), 0.0, 0.0)
    reference = np.array([probe.level(frame) for frame in clip[:ref_n]])
    mean = float(reference.mean())
    if not math.isfinite(mean) or mean <= 1e-9:
        return None
    variance = float(reference.var())
    steps = np.diff(reference)
    step_ratio = float((steps * steps).mean() / (2.0 * variance)) if variance > 1e-18 else 0.0
    return probe._replace(
        reference=reference, rel_std=math.sqrt(variance) / mean, step_ratio=step_ratio
    )


def _mat_pixels(ref_mean: np.ndarray, top: int, stride: int = 1) -> _PixelSet:
    """The lit pixels below ``top``, on every ``stride``-th row and column."""
    step = max(1, int(stride))
    rows, cols = slice(top, None, step), slice(None, None, step)
    return rows, cols, ref_mean[rows, cols] >= FLICKER_LIT_MIN


def _mat_waveform_pixels(ref_mean: np.ndarray, top: int) -> _PixelSet:
    """The mat pixels the compensation's waveform is the median over.

    Every ``FLICKER_LEVEL_STRIDE``-th row and column of the lit mat, as
    ``flicker_levels`` measures it, when that leaves ``FLICKER_MIN_LIT_PIXELS``;
    else every lit pixel (a small lit area).
    """
    strided = _mat_pixels(ref_mean, top, FLICKER_LEVEL_STRIDE)
    if int(strided[2].sum()) >= FLICKER_MIN_LIT_PIXELS:
        return strided
    return _mat_pixels(ref_mean, top)


def _ball_pixels(
    reference: np.ndarray, ref_mean: np.ndarray, box: tuple[int, int, int, int]
) -> _PixelSet | None:
    """The ball window's most varying lit pixels, None with too few lit pixels."""
    x0, y0, x1, y1 = box
    rows, cols = slice(y0, y1), slice(x0, x1)
    lit = ref_mean[rows, cols] >= FLICKER_LIT_MIN
    lit_count = int(lit.sum())
    if lit_count < FLICKER_MIN_BALL_PIXELS:
        return None
    keep = min(lit_count, max(FLICKER_MIN_BALL_PIXELS, round(FLICKER_BALL_FRACTION * lit_count)))
    spread = reference[:, rows, cols].std(axis=0)[lit]
    chosen = np.argpartition(spread, lit_count - keep)[lit_count - keep :]
    pixels = np.zeros(lit.size, dtype=bool)
    pixels[np.flatnonzero(lit)[chosen]] = True
    return rows, cols, pixels.reshape(lit.shape)


def _clamped_roi_top(roi_top: int | None, height: int) -> int:
    """The waveform's top row: the hitting zone's top edge, else the lower half."""
    if roi_top is None:
        return height // 2
    return int(min(max(int(roi_top), 0), height - 1))


def _clamped_window(window, height: int, width: int) -> tuple[int, int, int, int] | None:
    """``window`` (x0, y0, x1, y1) inside the frame, None without one or when it misses it."""
    if window is None:
        return None
    x0, y0 = max(0, int(window[0])), max(0, int(window[1]))
    x1, y1 = min(width, int(window[2])), min(height, int(window[3]))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return x0, y0, x1, y1


def _detected_by(mat: _Waveform | None, ball: _Waveform | None, min_rel_std: float) -> str | None:
    """Which measurement shows flicker worth removing: ``mat``, else ``ball``, else None."""
    if mat is not None and mat.rel_std >= min_rel_std:
        return "mat"
    if (
        ball is not None
        and ball.rel_std >= min_rel_std
        and ball.step_ratio >= FLICKER_MIN_BALL_STEP_RATIO
    ):
        return "ball"
    return None


def compensate_flicker(  # pylint: disable=too-many-locals
    frames: np.ndarray,
    *,
    roi_top: int | None,
    window: tuple[int, int, int, int] | None = None,
    min_rel_std: float = FLICKER_MIN_REL_STD,
    reference_frames: int = FLICKER_REF_FRAMES,
) -> tuple[np.ndarray, FlickerMeasurement]:
    """Remove a global flicker waveform from a grey clip ``(n, h, w)``; see the module docstring.

    ``roi_top`` is the first image row of the lit area the mat waveform is
    measured on (the hitting zone's top edge, ``TeeZone.y0``); None takes the
    lower half. ``window`` (x0, y0, x1, y1) is where the resting ball lies; the
    ball waveform is measured there, None skips it. ``min_rel_std`` applies to
    both; the mat's waveform compensates (``no_lit_pixels`` without one, even
    when the ball flickers). Returns the frames (uint8, a new array only when compensated;
    otherwise the input object unchanged) and the measurement. Never raises on
    bad input.
    """
    started = time.perf_counter()

    def done(out: np.ndarray, **fields) -> tuple[np.ndarray, FlickerMeasurement]:
        return out, FlickerMeasurement(elapsed_ms=1e3 * (time.perf_counter() - started), **fields)

    clip = np.asarray(frames)
    if clip.ndim != 3 or 0 in clip.shape:
        return done(frames, compensated=False, reason="invalid_frames")
    height, width = clip.shape[1:]
    top = _clamped_roi_top(roi_top, height)
    box = _clamped_window(window, height, width)
    ref_n = min(int(reference_frames), len(clip))
    if ref_n < FLICKER_MIN_REF_FRAMES:
        return done(
            frames,
            compensated=False,
            reason="too_few_frames",
            reference_frames=ref_n,
            roi_top=top,
            window=box,
        )
    reference = clip[:ref_n].astype(np.float32)
    ref_mean = reference.mean(axis=0)
    lit_count = int(_mat_pixels(ref_mean, top)[2].sum())
    mat = (
        _waveform(clip, ref_mean, _mat_waveform_pixels(ref_mean, top), ref_n)
        if lit_count >= FLICKER_MIN_LIT_PIXELS
        else None
    )
    ball_set = _ball_pixels(reference, ref_mean, box) if box is not None else None
    ball = _waveform(clip, ref_mean, ball_set, ref_n) if ball_set is not None else None
    common = {
        "reference_frames": ref_n,
        "lit_pixels": lit_count,
        "roi_top": top,
        "window": box,
        "rel_std": None if mat is None else mat.rel_std,
        "ball_rel_std": None if ball is None else ball.rel_std,
        "ball_step_ratio": None if ball is None else ball.step_ratio,
        "ball_pixels": 0 if ball is None else ball.count,
    }
    detected_by = _detected_by(mat, ball, min_rel_std)
    if mat is None:
        return done(
            frames, compensated=False, reason="no_lit_pixels", detected_by=detected_by, **common
        )
    if detected_by is None:
        return done(frames, compensated=False, reason="below_threshold", **common)
    wave = mat
    wave_mean = float(wave.reference.mean())
    deviation = wave.reference - wave_mean
    denominator = float((deviation * deviation).sum())
    if denominator <= 1e-9:
        return done(
            frames, compensated=False, reason="below_threshold", detected_by=detected_by, **common
        )
    # float32: 3x faster than float64 on a 120-frame clip; about 50 of its 30 M pixels
    # round the other way (1 grey).
    gain = (np.tensordot(deviation, reference - ref_mean, axes=(0, 0)) / denominator).astype(
        np.float32
    )
    waveform = np.concatenate([wave.reference, [wave.level(frame) for frame in clip[ref_n:]]])
    out = np.empty(clip.shape, dtype=np.uint8)
    for index, frame in enumerate(clip):
        corrected = frame.astype(np.float32) - gain * np.float32(waveform[index] - wave_mean)
        np.clip(np.rint(corrected), 0, 255, out=corrected)
        out[index] = corrected
    return done(out, compensated=True, reason="compensated", detected_by=detected_by, **common)


def _zone_fits(frames: np.ndarray, tee_zone) -> bool:
    shape = np.shape(frames)
    return (
        tee_zone is not None
        and len(shape) == 3
        and (tee_zone.image_width_px, tee_zone.image_height_px) == (shape[2], shape[1])
    )


def flicker_roi_top(frames: np.ndarray, tee_zone) -> int | None:
    """The hitting zone's top row when the zone fits these frames, else None (lower half)."""
    return int(tee_zone.y0) if _zone_fits(frames, tee_zone) else None


def flicker_window(
    frames: np.ndarray, *, tee_zone=None, ball_mark_px: tuple[float, float] | None = None
) -> tuple[int, int, int, int] | None:
    """Where the resting ball lies, for the ball waveform.

    The ball mark's window (``departed_ball.mark_window``) when there is a mark,
    else the hitting zone when it fits these frames (the disappeared-ball search
    box in both cases), else None: without either the search box centres on
    session state (the last ball, the session anchor) this step does not read.
    """
    shape = np.shape(frames)
    if len(shape) != 3:
        return None
    if ball_mark_px is not None:
        return mark_window(shape[1:], (float(ball_mark_px[0]), float(ball_mark_px[1])))
    if _zone_fits(frames, tee_zone):
        return (int(tee_zone.x0), int(tee_zone.y0), int(tee_zone.x1), int(tee_zone.y1))
    return None


def compensate_ball_flicker(
    frames: np.ndarray,
    *,
    tee_zone=None,
    ball_mark_px: tuple[float, float] | None = None,
    min_rel_std: float = FLICKER_MIN_REL_STD,
) -> tuple[np.ndarray, FlickerMeasurement]:
    """The ball path's flicker step: ``compensate_flicker`` on the mat and at the ball.

    The mat waveform comes from below the hitting zone's top edge (the lower
    half without a fitting zone, ``flicker_roi_top``), the ball's from
    ``flicker_window``. Shared by the live ball flight and the low-light shadow.
    """
    return compensate_flicker(
        frames,
        roi_top=flicker_roi_top(frames, tee_zone),
        window=flicker_window(frames, tee_zone=tee_zone, ball_mark_px=ball_mark_px),
        min_rel_std=min_rel_std,
    )


def flicker_levels(  # pylint: disable=too-many-locals,too-many-arguments
    frames: np.ndarray,
    *,
    roi_top: int | None,
    window: tuple[int, int, int, int] | None = None,
    min_rel_std: float = FLICKER_MIN_REL_STD,
    reference_frames: int = FLICKER_REF_FRAMES,
    stride: int = FLICKER_LEVEL_STRIDE,
) -> tuple[np.ndarray | None, FlickerMeasurement]:
    """The mat waveform ``w_k`` of every frame when the clip flickers, else None.

    The detection of ``compensate_flicker`` (the mat below ``roi_top`` and the
    ball in ``window``, the same thresholds) without touching the frames: the
    trigger check compares each frame with the reference frames of the same
    lamp level instead (``trigger_check``). The mat waveform is the median over
    every ``stride``-th row and column of its lit pixels, which cuts the
    measurement's cost by ``stride`` squared; the ball waveform keeps every pixel
    of its window. The levels come back only when a waveform crosses
    ``min_rel_std`` (reason ``detected``); the measurement's other reasons are
    those of ``compensate_flicker``. Never raises on bad input.
    """
    started = time.perf_counter()

    def done(levels: np.ndarray | None, **fields) -> tuple[np.ndarray | None, FlickerMeasurement]:
        measured = FlickerMeasurement(
            compensated=False, elapsed_ms=1e3 * (time.perf_counter() - started), **fields
        )
        return levels, measured

    clip = np.asarray(frames)
    if clip.ndim != 3 or 0 in clip.shape:
        return done(None, reason="invalid_frames")
    height, width = clip.shape[1:]
    top = _clamped_roi_top(roi_top, height)
    box = _clamped_window(window, height, width)
    ref_n = min(int(reference_frames), len(clip))
    if ref_n < FLICKER_MIN_REF_FRAMES:
        return done(None, reason="too_few_frames", reference_frames=ref_n, roi_top=top, window=box)
    step = max(1, int(stride))
    mat_clip = clip[:, top::step, ::step]
    mat_mean = mat_clip[:ref_n].astype(np.float32).mean(axis=0)
    mat_set = _mat_pixels(mat_mean, 0)
    lit_count = int(mat_set[2].sum())
    mat = (
        _waveform(mat_clip, mat_mean, mat_set, ref_n)
        if lit_count >= FLICKER_MIN_LIT_PIXELS
        else None
    )
    ball = None
    if box is not None:
        ball_clip = clip[:, box[1] : box[3], box[0] : box[2]]
        ball_reference = ball_clip[:ref_n].astype(np.float32)
        ball_mean = ball_reference.mean(axis=0)
        ball_set = _ball_pixels(ball_reference, ball_mean, (0, 0, box[2] - box[0], box[3] - box[1]))
        ball = _waveform(ball_clip, ball_mean, ball_set, ref_n) if ball_set is not None else None
    common = {
        "reference_frames": ref_n,
        "lit_pixels": lit_count,
        "roi_top": top,
        "window": box,
        "rel_std": None if mat is None else mat.rel_std,
        "ball_rel_std": None if ball is None else ball.rel_std,
        "ball_step_ratio": None if ball is None else ball.step_ratio,
        "ball_pixels": 0 if ball is None else ball.count,
    }
    detected_by = _detected_by(mat, ball, min_rel_std)
    if mat is None:
        return done(None, reason="no_lit_pixels", detected_by=detected_by, **common)
    if detected_by is None:
        return done(None, reason="below_threshold", **common)
    levels = np.concatenate([mat.reference, [mat.level(frame) for frame in mat_clip[ref_n:]]])
    return done(levels, reason="detected", detected_by=detected_by, **common)


def ball_flicker_levels(
    frames: np.ndarray,
    *,
    tee_zone=None,
    ball_mark_px: tuple[float, float] | None = None,
    min_rel_std: float = FLICKER_MIN_REL_STD,
) -> tuple[np.ndarray | None, FlickerMeasurement]:
    """``flicker_levels`` measured where ``compensate_ball_flicker`` measures."""
    return flicker_levels(
        frames,
        roi_top=flicker_roi_top(frames, tee_zone),
        window=flicker_window(frames, tee_zone=tee_zone, ball_mark_px=ball_mark_px),
        min_rel_std=min_rel_std,
    )


__all__ = [
    "FLICKER_LEVEL_STRIDE",
    "FLICKER_MIN_REL_STD",
    "FLICKER_REF_FRAMES",
    "FlickerMeasurement",
    "ball_flicker_levels",
    "compensate_ball_flicker",
    "compensate_flicker",
    "flicker_levels",
    "flicker_roi_top",
    "flicker_window",
]
