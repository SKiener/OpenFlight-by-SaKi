"""Observation-only camera check of one sound-trigger edge.

At the range most sound edges are neighbours' noise. For each edge this module
answers from the clip alone whether the teed ball left its spot just after the
edge. ``check_ball_departure`` never gates a trigger; its result is stored for
offline evaluation. ``LiveDepartureCheck`` applies the same per-frame states to
live frames for the trigger gate (``openflight.trigger_gate``).

The ball is located in the earliest pre-edge frames with the project's
``detect_reference_ball`` (around a hint first, which keeps it cheap). Its core
disk and a surrounding mat ring are then compared, frame by frame, with that
pre-edge appearance, in units of the ball's own contrast against the ring, so
front light (white ball on dark mat) and backlight (dark ball on a clipped
background) are handled alike. Per frame state:

* ``P`` present: the core disk still looks like the pre-edge ball.
* ``G`` gone: the disk changed completely while the ring stayed calm (at most
  a fifth of its pixels changed), so the ball left and nothing covers the spot.
  A ball that only rolled out of the disk also counts: it moved.
* ``O`` occluded: the disk changed and so did much of the ring (club head,
  golfer). Counting changed ring pixels, not their mean change, keeps a club
  darker or brighter than the mat by less than the ball's contrast visible.
* ``A`` ambiguous: the disk changed partly with a calm ring.
* ``-`` unlit (``UNLIT_STATE``, flickering clips only): the lamp's trough left
  the ball too faint in this frame to tell present from gone.

Mains flicker (a lamp at the tee, 100 Hz): ``flicker.flicker_levels`` measures
the lamp's level in every frame (the mat below the hitting zone's top; the
ball's window detects it too, as in ``flicker.compensate_ball_flicker``). When
it flickers, each frame is compared with the ``PHASE_MATCH_FRAMES`` reference
frames of the nearest lamp level instead of their median, and frames whose
matched contrast falls below ``GONE_MIN_CHANGE`` of the ball's are ``-``. On
2026-11-02 the ball vanished in every trough (``G P G G A G ...``) and 15
strikes were ``departed_before_edge``. The frames themselves are not
corrected: ``compensate_flicker`` cost 68 ms (Mac) on the frames the check
reads, the level measurement about 5 ms. Matching by frame count (every 3rd
frame at 288 fps) is not used: 3 frames are 1.042 lamp periods, so the phase
drifts by a full period over the ~70 frames between the reference and the
window end. A calm clip (``below_threshold``) is judged exactly as before.

Decision (``-`` frames skipped): ``departed`` once ``DEPARTURE_RUN``
consecutive post-edge frames are ``G`` and the ball is never present again;
``still_there`` when the window ends on present frames, even after the club
passed over the ball (a waggle or a practice swing must not count as a
departure); otherwise ``unclear``.

Where the ball is searched (``_locate_ball``): within ``match_radius_px`` of the
session tee anchor (``ball_source`` ``hint``); else, with a ball mark (the
resting ball tapped in the Align view, ``ball_mark_px``), only in the window
around it (``departed_ball.mark_window``, ``ball_mark``), where a hint counts
only inside the window too, as in the ball flight; else in the hitting zone
around the geometric tee (``zone``, ``openflight.camera.tee_zone``). Only
without a zone or a mark (no lens calibration or tee geometry) does the
legacy full-frame detector run (``full_frame``). These searches look for the
ball-sized spot whose core disk differs most from its mat ring, either sign,
at the expected diameter (the zone's, or with a mark and no fitting zone that
of the legacy detector's ball inside the window): a backlit ball is darker
than the mat by ~25-40 grey levels at AE 100 us/g2 (2026-10-25), which the
bright/dark thresholds of ``detect_reference_ball`` missed in 11 of 49 shots.
On 2026-11-02 the zone's strongest spot was the basket of spare balls, not
the ball (``zone_spot_static`` 46 times); the mark keeps it out. With a mark
the check judges spots down to ``MARK_MIN_CONTRAST`` (the daylight balls of
2026-11-02 stood 6-21 grey levels out); the live gate keeps ``MIN_CONTRAST``.

In the search area (the zone, or the mark's window) the spots whose core
changed most across the edge are classified as well: ``departure_*`` names a
spot that was seen leaving (the located ball or another area spot). Only these
spots may establish or move the tee anchor (``openflight.camera.tee_anchor``),
and only on OPS-accepted shots.

Rules that keep static spots from being judged as the ball:

* Near the anchor only true peaks count (``find_ball_near``): a spot must be
  the strongest within half a diameter, tested beyond the match circle, so
  the tail of a stronger spot outside the circle never stands in for the ball.
* Zone searches judge centres from ``RING_OUTER_FRAC`` diameters below the
  zone top (``_zone_box``), so the mat ring they compare stays inside the
  zone too, below the horizon and the tree line (2026-10-25: centres at rows
  209-217 had the tree line in their ring).
* A spot found by the zone or mark search is never ``still_there``: no shot
  has shown it to be the tee ball. If another area spot left, that spot is
  judged (``departed``), else the check is ``unclear``/``zone_spot_static``
  (``zone_spot_not_struck`` for a spot that left unlike a struck ball).
* An area spot counts as departed only when it left like a struck ball
  (``_struck``): present or covered by the club until ``EARLY_DEPARTURE_MS[1]``
  before the edge and gone within ``STRIKE_DEPARTURE_MS`` after it. A waggled
  club head next to the ball otherwise "departed" too (2026-10-25).
* The live gate judges a spot only when its signed contrast (core minus ring,
  scaled to ``NORMALISED_EXPOSURE_GAIN`` by ``normalised_contrast``) matches
  the confirmed anchor's (``AnchorBall.contrast``, ``spot_matches``); a mat
  mark or the horizon (+22..+39 at 100 us/g2) beside a dark ball (-25..-40 at
  100 us/g2) is ``unclear``/``spot_mismatch``, so the edge is pulsed. The
  scaling keeps the anchor valid across exposure changes: the 2026-10-25
  balls measured -26..-125 grey levels at 100 us/g2..197 us/g4, but
  -14..-20 normalised.
* When two true peaks within the match radius both match the anchor's
  contrast (a spare ball beside the teed one), the live gate judges neither:
  ``unclear``/``two_spots``; likewise when the judged peak lies more than
  half a diameter from the anchor and the spot at the anchor centre matches
  too (a stronger spare in front of the teed ball, which is then no peak).
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage, signal

from openflight.camera.club_delivery import BALL_PATCH_RADIUS_FRAC
from openflight.camera.club_motion import ReferenceBall, detect_reference_ball
from openflight.camera.departed_ball import in_window as centre_in_window, mark_window
from openflight.camera.flicker import ball_flicker_levels
from openflight.camera.tee_zone import TeeZone

SENSOR_MAX_LAG_NS = 50_000_000
REFERENCE_FRAMES = 20
REFERENCE_END_MS = -40.0
WINDOW_MS = (10.0, 50.0)
EARLY_DEPARTURE_MS = (-40.0, -15.0)
MIN_WINDOW_FRAMES = 3
MIN_CONTRAST = 20.0
MIN_DIAMETER_PX = 9.0
MAX_DIAMETER_PX = 30.0
RING_INNER_FRAC = 0.8
RING_OUTER_FRAC = 1.4
PRESENT_MAX_CHANGE = 0.35
GONE_MIN_CHANGE = 0.6
CALM_RING_MAX_CHANGED_FRAC = 0.2
RING_PIXEL_MIN_DELTA = 15.0
RING_PIXEL_CONTRAST_FRAC = 0.15
DEPARTURE_RUN = 2
CLIPPED_LEVEL = 250
DARK_LEVEL = 30.0
CLIPPED_SCENE_FRAC = 0.5
MIN_SEARCH_HALF_PX = 32
STILL_THERE_EVIDENCE_MS = 35.0
# A re-teed ball sits within this radius of the session anchor (2026-10-25:
# x 359-378 around the median, iron vs driver tee height 15 px). The gate and
# the check look for it only there, so a mat mark next to a missed ball cannot
# stand in for it.
ANCHOR_MATCH_DIAMETERS = 1.0
MIN_MATCH_RADIUS_PX = 10.0
# Spots weaker than this are mat texture; between it and MIN_CONTRAST the ball
# is found but too faint to judge (``low_contrast``).
CANDIDATE_MIN_CONTRAST = 10.0
# With a ball mark the check judges spots down to this contrast (the live gate
# keeps MIN_CONTRAST). The tap says where the ball lies, and the search stays in
# its window. 2026-11-02 daylight (350 us/g6, a ball lit from above on a dark
# mat): balls at -6..-14 and +11..+21 grey, the basket's spare balls at +32. At
# MIN_CONTRAST the check judged none of the session's first 50 balls.
MARK_MIN_CONTRAST = CANDIDATE_MIN_CONTRAST
# The departure scan ranks up to ZONE_SEARCH_LIMIT zone spots by how much their
# core changed across the edge and classifies the top ZONE_CANDIDATES. Ranking
# by strength alone missed the ball: the horizon band and the ball tray hold
# a dozen spots stronger than a backlit ball (2026-10-25).
ZONE_SEARCH_LIMIT = 64
ZONE_CANDIDATES = 4
# Around the spot the check judged, the scan also keeps spots one diameter
# from a stronger one (a ball leaving beside a static spare; see
# ``_departed_zone_spot``).
JUDGED_SEARCH_DIAMETERS = 1.5
# Within JUDGED_SEARCH_DIAMETERS of the judged spot the scan also ranks up to
# CHANGED_SPOT_LIMIT spots by how much their core changed across the edge
# (median window frame minus median reference frame), whatever their pre-edge
# peak: a ball leaving beside a static spot 1.5-2x as dark is no contrast
# peak at all (2026-10-26 review what-ifs). A change below
# CHANGED_SPOT_MIN_DELTA grey levels is sensor noise. The candidates' mat ring
# leaves out the judged spot's disk (JUDGED_CLEAR_FRAC diameters around it,
# the core disk of 0.4 d plus margin), which would otherwise sit in the ring of
# a ball 16-17 px away; a ring left with fewer than MIN_RING_PIXELS pixels
# is not judged. These values come from the reviewer's prototype and the
# synthetic scenes, not from Toptracer.
CHANGED_SPOT_LIMIT = 4
CHANGED_SPOT_MIN_DELTA = 5.0
JUDGED_CLEAR_FRAC = 0.65
MIN_RING_PIXELS = 20
ANCHOR_SOURCES = ("hint", "zone", "ball_mark")
# Under mains flicker (``flicker.flicker_levels`` detects it) each judged frame is
# compared with the PHASE_MATCH_FRAMES reference frames nearest to it in lamp
# level, not with the median of all of them (see ``_Spot.states``).
PHASE_MATCH_FRAMES = 3
# State of a frame whose lamp level leaves the ball less than GONE_MIN_CHANGE of
# its contrast (the lamp's trough): a ball gone there would not read ``G``, so the
# frame cannot tell; ``_decide`` and ``_struck`` skip it.
UNLIT_STATE = "-"
# The live gate judges a spot only when its signed contrast lies within this
# factor range of the confirmed anchor's (same sign). The 2026-10-25 balls
# spread -43..-112 over the session; the latest departures set the anchor's
# median, so an exposure change re-centres it within a few shots.
SPOT_CONTRAST_RATIO = (0.5, 2.0)
# Contrasts are compared at this exposure time x analogue gain (us): grey
# levels scale linearly with both on the raw stream.
NORMALISED_EXPOSURE_GAIN = 100.0
# A struck ball is gone this soon after its sound edge (range shots left the
# tee 8-30 ms after it; 2026-10-25: 6-14 ms). Club waggles that looked like a
# departing zone spot "left" 35-47 ms after the edge.
STRIKE_DEPARTURE_MS = 30.0
# The footprint profile reaches this many searched diameters out and takes the
# median of FOOTPRINT_SECTORS sectors per ring (``footprint_diameter_px``).
FOOTPRINT_REACH_DIAMETERS = 1.6
FOOTPRINT_SECTORS = 8
# The core disk may reach this factor beyond a ball's half-maximum footprint
# radius: ~70 % of it still covers the ball, so its median stays on it
# (2026-10-25 replay, footprint ~13.4 px: a 20 px disk departed 54 of 69
# strikes, 22 px 46, 24 px 20). Within it the nominal diameter is kept: the
# 2026-10-09 footprints (11.3-13.5 px) left the 16.5 px nominal unchanged.
FOOTPRINT_CORE_SLACK = 1.2


@dataclass(frozen=True)
class TriggerCheck:
    """What the camera saw around one trigger edge.

    ``first_departure_ms_after_edge`` is the exposure time of the frame that
    completed the departure evidence, i.e. the earliest moment a live gate
    could have decided (frame delivery adds a few ms on top). ``frame_states``
    holds one state letter per frame from ``EARLY_DEPARTURE_MS[0]`` to the
    window end, aligned with ``frame_offsets_ms``. ``locate_ms`` is the ball
    search, which a live gate could do before the edge; ``decide_ms`` is the
    part that has to run after it. ``departure_*`` is a spot seen leaving in
    this clip (the located ball or another hitting-zone spot), found only by
    the hint or zone search, never by the full-frame fallback;
    ``departure_contrast`` is its signed pre-edge contrast (core minus ring
    medians, negative for a ball darker than the mat) in grey levels, and
    ``exposure_gain`` the reference frames' exposure time x analogue gain (us)
    it was measured at (None when the frames carried no exposure).
    ``departure_footprint_px`` is the half-maximum diameter of the change the
    spot left (``footprint_diameter_px``) and ``departure_diameter_px`` the
    diameter the tee anchor and the live gate judge it at
    (``kernel_diameter_px``).
    """

    decision: str
    reason: str
    first_departure_ms_after_edge: float | None = None
    ball_x: float | None = None
    ball_y: float | None = None
    ball_diameter_px: float | None = None
    ball_source: str | None = None
    pre_contrast: float | None = None
    post_contrast: float | None = None
    clipped_fraction: float | None = None
    frames_used: int = 0
    frame_states: str = ""
    frame_offsets_ms: tuple[float, ...] = ()
    locate_ms: float = 0.0
    decide_ms: float = 0.0
    compute_ms: float = 0.0
    departure_x: float | None = None
    departure_y: float | None = None
    departure_diameter_px: float | None = None
    departure_source: str | None = None
    departure_contrast: float | None = None
    exposure_gain: float | None = None
    departure_footprint_px: float | None = None
    ball_mark: tuple[float, float] | None = None
    flicker: dict | None = None

    @property
    def ball(self) -> ReferenceBall | None:
        """The located ball."""
        return _ball_or_none(self.ball_x, self.ball_y, self.ball_diameter_px)

    @property
    def departure(self) -> ReferenceBall | None:
        """The spot seen leaving in this clip, if any."""
        return _ball_or_none(self.departure_x, self.departure_y, self.departure_diameter_px)

    def to_dict(self) -> dict:
        """JSON-ready form with rounded floats."""
        data = asdict(self)
        for key, value in data.items():
            if isinstance(value, float):
                data[key] = round(value, 3)
        data["frame_offsets_ms"] = [round(value, 2) for value in self.frame_offsets_ms]
        if self.ball_mark is not None:
            data["ball_mark"] = [round(value, 3) for value in self.ball_mark]
        return data


def _ball_or_none(x, y, diameter_px) -> ReferenceBall | None:
    if x is None or y is None or diameter_px is None:
        return None
    return ReferenceBall(x, y, diameter_px, round(math.pi * (diameter_px / 2.0) ** 2))


@dataclass(frozen=True)
class AnchorBall(ReferenceBall):
    """A session tee anchor plus the signed contrast its departing balls showed.

    ``contrast`` is the median core-minus-ring contrast (``_Spot``) of the
    departures that placed the anchor, normalised (``normalised_contrast``).
    ``LiveDepartureCheck`` judges only a spot that matches it
    (``spot_matches``); None (tests, tools) skips that.
    """

    contrast: float | None = None


def normalised_contrast(contrast: float, exposure_gain: float | None) -> float | None:
    """Grey-level contrast at ``exposure_gain`` (exposure us x gain) scaled to the norm."""
    if exposure_gain is None or not math.isfinite(exposure_gain) or exposure_gain <= 0.0:
        return None
    return contrast * NORMALISED_EXPOSURE_GAIN / exposure_gain


def spot_matches(contrast: float, anchor_contrast: float) -> bool:
    """Whether a spot's signed contrast fits the anchor ball's (``SPOT_CONTRAST_RATIO``)."""
    if contrast * anchor_contrast <= 0.0:
        return False
    ratio = abs(contrast) / abs(anchor_contrast)
    return SPOT_CONTRAST_RATIO[0] <= ratio <= SPOT_CONTRAST_RATIO[1]


def exposure_timestamps_ns(host_ns, sensor_ns=None) -> np.ndarray:
    """Per-frame exposure times on the trigger's monotonic clock.

    Same rule as ``server._camera_frame_timestamps``: host receive times lag
    exposure by ~4.6 ms, so sensor times are used when they rise strictly and
    trail the host times by 0..50 ms; otherwise the host times are kept.
    """
    host = np.asarray(host_ns, dtype=np.int64)
    if sensor_ns is None:
        return host
    sensor = np.asarray(sensor_ns, dtype=np.int64)
    return sensor if _sensor_clock_usable(host, sensor) else host


def _sensor_clock_usable(host: np.ndarray, sensor: np.ndarray) -> bool:
    if sensor.shape != host.shape or len(sensor) < 2 or np.any(np.diff(sensor) <= 0):
        return False
    return bool(0 <= np.median(host - sensor) <= SENSOR_MAX_LAG_NS)


def match_radius_px(diameter_px: float) -> float:
    """How far a re-teed ball may sit from an anchor of this diameter."""
    return max(MIN_MATCH_RADIUS_PX, ANCHOR_MATCH_DIAMETERS * diameter_px)


def kernel_diameter_px(nominal_px: float, footprint_px: float | None) -> float:
    """Diameter to judge a ball at: ``nominal_px``, shrunk when its core disk overhangs the ball.

    The core disk (``BALL_PATCH_RADIUS_FRAC`` of the diameter) may reach
    ``FOOTPRINT_CORE_SLACK`` times the measured footprint radius. A nominal
    diameter from a wrong mount or tee geometry that is too large puts mat
    into the core, the ball's contrast falls below ``MIN_CONTRAST`` and the
    gate goes blind (2026-10-25 replay at 24 px instead of 16.5 px: 20 of 69
    strikes departed instead of 57). The result never exceeds ``nominal_px``,
    so a footprint widened by a shadow or the tee cannot grow the disk or the
    anchor's match radius, and never falls below ``MIN_DIAMETER_PX``.
    """
    if footprint_px is None:
        return nominal_px
    fitted = FOOTPRINT_CORE_SLACK * footprint_px / (2.0 * BALL_PATCH_RADIUS_FRAC)
    return min(nominal_px, max(MIN_DIAMETER_PX, fitted))


def footprint_diameter_px(  # pylint: disable=too-many-locals
    frames: np.ndarray, reference_idx: np.ndarray, window_idx: np.ndarray, spot: ReferenceBall
) -> float | None:
    """Half-maximum diameter of the change ``spot`` left across the edge, None if unmeasurable.

    The change is |median(window) - median(reference)| around the spot. Its
    radial profile, centred on the change's centroid within half the spot's
    diameter, takes per 1-px ring the median of ``FOOTPRINT_SECTORS`` sector
    means, so a club, tee or shoe that changed on one side does not widen it.
    The footprint is twice the radius where the profile falls halfway from
    its core level to its level ``FOOTPRINT_REACH_DIAMETERS`` diameters out.
    None at the image edge or when the core changed less than
    ``CHANGED_SPOT_MIN_DELTA`` grey levels more than the surroundings.
    """
    diameter = spot.diameter_px
    reach = math.ceil(FOOTPRINT_REACH_DIAMETERS * diameter)
    cx, cy = round(spot.x), round(spot.y)
    height, width = frames.shape[1:]
    if cx - reach < 0 or cy - reach < 0 or cx + reach >= width or cy + reach >= height:
        return None
    crop = frames[:, cy - reach : cy + reach + 1, cx - reach : cx + reach + 1].astype(np.float32)
    change = np.abs(np.median(crop[window_idx], axis=0) - np.median(crop[reference_idx], axis=0))
    yy, xx = np.mgrid[-reach : reach + 1, -reach : reach + 1]
    weights = change * (xx * xx + yy * yy <= (diameter / 2.0) ** 2)
    total = float(weights.sum())
    if total <= 0.0:
        return None
    mx, my = float((weights * xx).sum()) / total, float((weights * yy).sum()) / total
    ring = np.hypot(xx - mx, yy - my).astype(int)
    angle = np.arctan2(yy - my, xx - mx) + math.pi
    sector = (angle / (2.0 * math.pi) * FOOTPRINT_SECTORS).astype(int) % FOOTPRINT_SECTORS
    profile = np.empty(reach)
    for radius in range(reach):
        in_ring = ring == radius
        means = [
            float(change[in_ring & (sector == index)].mean())
            for index in range(FOOTPRINT_SECTORS)
            if np.any(in_ring & (sector == index))
        ]
        profile[radius] = np.median(means)
    core = float(profile[: max(2, int(0.25 * diameter))].mean())
    outer = float(np.median(profile[int(0.85 * reach) :]))
    if core - outer < CHANGED_SPOT_MIN_DELTA:
        return None
    half = outer + 0.5 * (core - outer)
    below = np.flatnonzero(profile[1:] < half)
    if len(below) == 0:
        return None
    edge = int(below[0]) + 1
    step = max(1e-6, profile[edge - 1] - profile[edge])
    # Ring k holds radii [k, k + 1): its mean radius is k + 0.5.
    return float(2.0 * (edge - 0.5 + (profile[edge - 1] - half) / step))


def _plausible(ball: ReferenceBall) -> bool:
    return MIN_DIAMETER_PX <= ball.diameter_px <= MAX_DIAMETER_PX


def _contrast_kernel(diameter_px: float) -> np.ndarray:
    """Mean of the core disk minus mean of the mat ring, as a correlation kernel."""
    core = max(2.0, diameter_px * BALL_PATCH_RADIUS_FRAC)
    outer = RING_OUTER_FRAC * diameter_px
    half = math.ceil(outer)
    yy, xx = np.mgrid[-half : half + 1, -half : half + 1]
    distance_sq = xx * xx + yy * yy
    disk = distance_sq <= core * core
    ring = (distance_sq >= (RING_INNER_FRAC * diameter_px) ** 2) & (distance_sq <= outer**2)
    return (disk / disk.sum() - ring / ring.sum()).astype(np.float32)


def _stack_crop(reference, rows: slice, cols: slice) -> np.ndarray:
    """(n, h, w) crop of a frame stack or of a sequence of 2-D frames."""
    if isinstance(reference, np.ndarray):
        return reference[:, rows, cols]
    return np.stack([np.asarray(image)[rows, cols] for image in reference])


def find_balls(  # pylint: disable=too-many-arguments,too-many-locals
    reference,
    box: tuple[int, int, int, int],
    diameter_px: float,
    *,
    limit: int = 1,
    min_contrast: float = CANDIDATE_MIN_CONTRAST,
    within: tuple[float, float, float] | None = None,
    peaks_only: bool = False,
    suppress_px: int | None = None,
) -> list[tuple[ReferenceBall, float]]:
    """Ball-sized spots centred in ``box`` (x0, y0, x1, y1), strongest first.

    ``reference`` holds the pre-edge frames, (n, h, w) or a sequence of 2-D
    images; only the box (plus the ring) is medianed. A spot's strength is its
    core-disk minus mat-ring mean at ``diameter_px`` (the masks the check uses),
    returned signed; spots weaker than ``min_contrast`` are dropped, and spots
    closer than one diameter to a stronger one are suppressed. ``peaks_only``
    keeps only true local maxima: a centre counts only when nothing within
    half a diameter (where no second ball fits), inside or outside the box, is
    stronger, so a spot pinned to the box edge by a stronger neighbour outside
    it is no ball, while a ball beside a stronger club head still is. Peaks
    are suppressed only within that half diameter, so two balls side by side
    (centres one diameter apart or less) are both returned. ``suppress_px``
    overrides the suppression reach (a square of that half-width).
    ``within`` (x, y, radius) keeps only centres inside that circle. Centres
    keep the ring inside the image.
    """
    height, width = np.asarray(reference[0]).shape
    edge = math.ceil(RING_OUTER_FRAC * diameter_px) + 1
    x0, y0 = max(int(box[0]), edge), max(int(box[1]), edge)
    x1, y1 = min(int(box[2]), width - edge), min(int(box[3]), height - edge)
    if x1 <= x0 or y1 <= y0:
        return []
    # Strength map over the box grown by the peak test's reach (inside the image).
    pad = max(1, round(diameter_px / 2.0)) if peaks_only else 0
    if suppress_px is not None:
        suppress = max(1, int(suppress_px))
    else:
        suppress = pad if peaks_only else max(2, round(diameter_px))
    gx0, gy0 = max(x0 - pad, edge), max(y0 - pad, edge)
    gx1, gy1 = min(x1 + pad, width - edge), min(y1 + pad, height - edge)
    kernel = _contrast_kernel(diameter_px)
    half = kernel.shape[0] // 2
    background = np.median(
        _stack_crop(reference, slice(gy0 - half, gy1 + half), slice(gx0 - half, gx1 + half)),
        axis=0,
    ).astype(np.float32)
    grown = signal.fftconvolve(background, kernel, mode="valid")
    grown_strength = np.abs(grown)
    if peaks_only:
        neighbourhood = ndimage.maximum_filter(grown_strength, size=2 * pad + 1, mode="nearest")
        grown_strength[grown_strength < neighbourhood] = -1.0
    inner = (slice(y0 - gy0, y1 - gy0), slice(x0 - gx0, x1 - gx0))
    contrast, strength = grown[inner], grown_strength[inner]
    if within is not None:
        yy, xx = np.mgrid[y0:y1, x0:x1]
        strength[(xx - within[0]) ** 2 + (yy - within[1]) ** 2 > within[2] ** 2] = -1.0
    area = round(math.pi * (diameter_px / 2.0) ** 2)
    found: list[tuple[ReferenceBall, float]] = []
    while len(found) < limit:
        peak = np.unravel_index(int(np.argmax(strength)), strength.shape)
        row, col = int(peak[0]), int(peak[1])
        if strength[row, col] < min_contrast:
            break
        found.append(
            (
                ReferenceBall(float(x0 + col), float(y0 + row), float(diameter_px), area),
                float(contrast[row, col]),
            )
        )
        strength[
            max(0, row - suppress) : row + suppress + 1, max(0, col - suppress) : col + suppress + 1
        ] = -1.0
    return found


def balls_near(reference, anchor: ReferenceBall, *, limit: int = 1) -> list[ReferenceBall]:
    """Up to ``limit`` true ball-sized peaks within ``match_radius_px`` of ``anchor``.

    Strongest first. Only a centre that is the strongest within half a
    diameter counts (``find_balls(peaks_only=True)``, tested beyond the
    circle). Without that, the response of a mat mark, shoe or the horizon
    just outside the circle peaked on the circle's edge and was judged instead
    of the ball (2026-10-25, anchor 30 px beside the ball: 6 of 58 strikes
    blocked).
    """
    radius = match_radius_px(anchor.diameter_px)
    box = (
        math.floor(anchor.x - radius),
        math.floor(anchor.y - radius),
        math.ceil(anchor.x + radius) + 1,
        math.ceil(anchor.y + radius) + 1,
    )
    found = find_balls(
        reference,
        box,
        anchor.diameter_px,
        limit=limit,
        within=(anchor.x, anchor.y, radius),
        peaks_only=True,
    )
    return [ball for ball, _contrast in found]


def find_ball_near(reference, anchor: ReferenceBall) -> ReferenceBall | None:
    """The strongest true peak within ``match_radius_px`` of ``anchor`` (``balls_near``)."""
    found = balls_near(reference, anchor)
    return found[0] if found else None


def _zone_box(zone: TeeZone) -> tuple[int, int, int, int]:
    """Centres a zone search judges: the zone minus its top ``RING_OUTER_FRAC`` diameters.

    The zone bounds ball centres; the mat ring the check compares reaches
    ``RING_OUTER_FRAC`` diameters further, which put the tree line into the
    ring of centres at the 2026-10-25 zone top (row 209, horizon ~206). The
    inset keeps the ring inside the zone. The cost: a camera pitched down more
    than ~7 deg (instead of the zone's 8, ``tee_zone.PITCH_MARGIN_DEG``) puts
    the ball above the judged centres.
    """
    return _inset_top(
        (zone.x0, zone.y0, zone.x1, zone.y1),
        zone.diameter_px,
    )


def _inset_top(box: tuple[int, int, int, int], diameter_px: float) -> tuple[int, int, int, int]:
    """``box`` without its top ``RING_OUTER_FRAC`` diameters (``_zone_box``)."""
    return box[0], box[1] + math.ceil(RING_OUTER_FRAC * diameter_px), box[2], box[3]


@dataclass(frozen=True)
class _SearchArea:
    """Where the check looks for the ball without a hint, and for spots that left.

    ``box`` bounds the centres judged (x0, y0, x1, y1), ``diameter_px`` is the
    expected ball size and ``source`` the ``ball_source`` of a spot found
    there: ``zone`` (the hitting zone, ``_zone_box``) or ``ball_mark`` (the
    window around the tapped ball, ``_mark_area``). ``min_contrast`` is the
    weakest spot the check judges there.
    """

    box: tuple[int, int, int, int]
    diameter_px: float
    source: str
    min_contrast: float = MIN_CONTRAST

    @classmethod
    def of_zone(cls, zone: TeeZone) -> _SearchArea:
        """The hitting zone as a search area."""
        return cls(_zone_box(zone), float(zone.diameter_px), "zone")


def _zone_fits(zone: TeeZone | None, shape: tuple[int, int]) -> bool:
    return zone is not None and (zone.image_width_px, zone.image_height_px) == (shape[1], shape[0])


def _mark_area(
    reference: np.ndarray, window: tuple[int, int, int, int], zone: TeeZone | None
) -> _SearchArea | None:
    """The search area of a ball mark: its window, with the top inset as for the zone.

    The ball size is the hitting zone's when the zone fits the frames, else
    that of the legacy detector's ball inside the window (as the ball flight
    reads only the window then); None when it finds none. The window's top
    lies 64 px above the tap, about on the horizon at 640x400, so the inset
    keeps the ring of the centres judged below it, as ``_zone_box`` does.
    """
    if _zone_fits(zone, reference.shape[1:]):
        diameter = float(zone.diameter_px)
    else:
        try:
            ball = detect_reference_ball(reference, roi=window)
        except ValueError:
            return None
        if not _plausible(ball):
            return None
        diameter = float(ball.diameter_px)
    return _SearchArea(_inset_top(window, diameter), diameter, "ball_mark", MARK_MIN_CONTRAST)


def _locate_ball(
    reference: np.ndarray,
    hint: ReferenceBall | None,
    area: _SearchArea | None,
    window: tuple[int, int, int, int] | None = None,
) -> tuple[ReferenceBall | None, str | None]:
    """The ball and how it was found (``ball_source``), see the module docstring.

    With a ball mark (``window``) a ball found at the hint counts only inside
    the window, and without an area nothing is searched beyond the hint.
    """
    if hint is not None:
        ball = find_ball_near(reference, hint)
        if ball is not None and (window is None or centre_in_window(window, ball)):
            return ball, "hint"
    if area is not None:
        found = find_balls(reference, area.box, area.diameter_px, peaks_only=True)
        return (found[0][0], area.source) if found else (None, None)
    if window is not None:
        return None, None
    try:
        return detect_reference_ball(reference), "full_frame"
    except ValueError:
        return None, None


def _scene_reason(frame: np.ndarray) -> str:
    sample = frame[::4, ::4]
    if float(np.median(sample)) < DARK_LEVEL:
        return "too_dark"
    if float(np.mean(sample >= CLIPPED_LEVEL)) > CLIPPED_SCENE_FRAC:
        return "too_bright"
    return "ball_not_found"


def _masks(ball: ReferenceBall, shape: tuple[int, int]):
    """Crop box plus core-disk and mat-ring masks, or None at the image edge."""
    outer = RING_OUTER_FRAC * ball.diameter_px
    half = math.ceil(outer) + 1
    cx, cy = round(ball.x), round(ball.y)
    x0, y0 = cx - half, cy - half
    if x0 < 0 or y0 < 0 or cx + half + 1 > shape[1] or cy + half + 1 > shape[0]:
        return None
    yy, xx = np.mgrid[y0 : cy + half + 1, x0 : cx + half + 1]
    distance_sq = (xx - ball.x) ** 2 + (yy - ball.y) ** 2
    core = max(2.0, ball.diameter_px * BALL_PATCH_RADIUS_FRAC)
    disk = distance_sq <= core * core
    ring = (distance_sq >= (RING_INNER_FRAC * ball.diameter_px) ** 2) & (distance_sq <= outer**2)
    return (slice(y0, cy + half + 1), slice(x0, cx + half + 1)), disk, ring


@dataclass(frozen=True)
class _Spot:  # pylint: disable=too-many-instance-attributes
    """Pre-edge appearance of the ball's core disk and the mat ring around it.

    ``ref_disks``/``ref_rings`` (each reference frame's disk and ring pixels)
    and ``ref_levels`` (their lamp levels) are kept only for a flickering clip
    (``from_reference`` with ``levels``); ``states`` then matches phases.
    """

    disk: np.ndarray
    ring: np.ndarray
    disk_ref: np.ndarray
    ring_ref: np.ndarray
    signed_contrast: float
    clipped_fraction: float
    around_median: float
    ref_disks: np.ndarray | None = None
    ref_rings: np.ndarray | None = None
    ref_levels: np.ndarray | None = None

    @property
    def contrast(self) -> float:
        """How far the ball stands out from its ring, either sign."""
        return abs(self.signed_contrast)

    @classmethod
    def from_reference(
        cls, crops: np.ndarray, disk: np.ndarray, ring: np.ndarray, levels=None
    ) -> _Spot:
        """Model the spot from float crops (n, h, w) of the reference frames.

        ``levels`` holds the reference frames' lamp levels (``flicker_levels``)
        when the clip flickers, else None.
        """
        background = np.median(crops, axis=0)
        disk_ref, ring_ref = background[disk], background[ring]
        around = background[disk | ring]
        phase = {}
        if levels is not None:
            phase = {
                "ref_disks": crops[:, disk],
                "ref_rings": crops[:, ring],
                "ref_levels": np.asarray(levels, dtype=np.float64),
            }
        return cls(
            disk=disk,
            ring=ring,
            disk_ref=disk_ref,
            ring_ref=ring_ref,
            signed_contrast=float(np.median(disk_ref)) - float(np.median(ring_ref)),
            clipped_fraction=float(np.mean(around >= CLIPPED_LEVEL)),
            around_median=float(np.median(around)),
            **phase,
        )

    def _phase_references(self, levels: np.ndarray):
        """Per judged frame: disk and ring reference pixels and contrast at its lamp level.

        Each frame takes the per-pixel median of the ``PHASE_MATCH_FRAMES``
        reference frames whose lamp level lies nearest to its own. Under one
        common waveform a pixel's brightness depends on the level alone, rising
        or falling, so these frames show the ball as the lamp lit it in that
        frame. The contrast is theirs (core minus ring medians, unsigned).
        """
        distance = np.abs(np.asarray(levels, dtype=np.float64)[:, None] - self.ref_levels[None, :])
        nearest = np.argsort(distance, axis=1, kind="stable")[:, :PHASE_MATCH_FRAMES]
        disk_ref = np.median(self.ref_disks[nearest], axis=1)
        ring_ref = np.median(self.ref_rings[nearest], axis=1)
        contrast = np.abs(np.median(disk_ref, axis=1) - np.median(ring_ref, axis=1))
        return disk_ref, ring_ref, contrast

    def failure(self, min_contrast: float = MIN_CONTRAST) -> str | None:
        """Why the ball cannot be told from the mat at ``min_contrast``, or None when it can."""
        if self.contrast >= min_contrast:
            return None
        if self.clipped_fraction > CLIPPED_SCENE_FRAC:
            return "too_bright"
        if self.around_median < DARK_LEVEL:
            return "too_dark"
        return "low_contrast"

    def states(self, crops: np.ndarray, levels=None) -> str:
        """One state letter per float crop (n, h, w).

        ``levels`` holds the crops' lamp levels when the clip flickers: each
        crop is then compared with the reference frames of its own lamp level
        (``_phase_references``), so a ball that darkens in every trough of the
        lamp is not "gone" there. The change stays in units of the spot's own
        (median) contrast. A frame whose matched contrast falls below
        ``GONE_MIN_CHANGE`` of it is ``UNLIT_STATE``: in the lamp's trough the
        ball barely stands out, and a ball gone there would read present.
        Without levels (or a spot modelled without them) all crops are
        compared with the median reference.
        """
        disks, rings = crops[:, self.disk], crops[:, self.ring]
        unlit = None
        if levels is None or self.ref_levels is None:
            disk_ref, ring_ref = self.disk_ref, self.ring_ref
        else:
            disk_ref, ring_ref, contrast = self._phase_references(levels)
            unlit = contrast < GONE_MIN_CHANGE * self.contrast
        return _classify(
            np.mean(np.abs(disks - disk_ref), axis=1) / self.contrast,
            np.mean(
                np.abs(rings - ring_ref)
                > max(RING_PIXEL_MIN_DELTA, RING_PIXEL_CONTRAST_FRAC * self.contrast),
                axis=1,
            ),
            unlit,
        )


def _first_run_end(states: str, letter: str) -> int | None:
    run = 0
    for index, state in enumerate(states):
        run = run + 1 if state == letter else 0
        if run >= DEPARTURE_RUN:
            return index
    return None


def _classify(disk_change: np.ndarray, ring_changed: np.ndarray, unlit=None) -> str:
    """State letters from the disk change and the ring's changed share; ``unlit`` ones are ``-``."""
    states = []
    for index, (disk, ring) in enumerate(zip(disk_change, ring_changed)):
        if unlit is not None and unlit[index]:
            states.append(UNLIT_STATE)
        elif disk < PRESENT_MAX_CHANGE:
            states.append("P")
        elif ring > CALM_RING_MAX_CHANGED_FRAC:
            states.append("O")
        elif disk >= GONE_MIN_CHANGE:
            states.append("G")
        else:
            states.append("A")
    return "".join(states)


def _lit(states: str, offsets) -> tuple[str, list[float]]:
    """``states`` and their offsets without the ``UNLIT_STATE`` frames."""
    kept = [
        (state, float(offset)) for state, offset in zip(states, offsets) if state != UNLIT_STATE
    ]
    return "".join(state for state, _offset in kept), [offset for _state, offset in kept]


def _decide(  # pylint: disable=too-many-return-statements
    states: str, offsets: np.ndarray, window_ms: tuple[float, float]
):
    """(decision, reason, departure offset) from the per-frame states (unlit ones skipped)."""
    states, offsets = _lit(states, offsets)
    early = "".join(s for s, t in zip(states, offsets) if t <= EARLY_DEPARTURE_MS[1])
    post_offsets = [t for t in offsets if t >= 0.0]
    post = states[len(states) - len(post_offsets) :]
    window = "".join(s for s, t in zip(post, post_offsets) if t >= window_ms[0])
    if len(window) < MIN_WINDOW_FRAMES:
        return "unclear", "insufficient_post_frames", None
    if _first_run_end(early, "G") is not None:
        return "unclear", "departed_before_edge", None
    run_end = _first_run_end(post, "G")
    if run_end is not None:
        if "P" in post[run_end - DEPARTURE_RUN + 1 :]:
            return "still_there", "ball_reappeared", None
        return "departed", "ball_left_spot", float(post_offsets[run_end])
    if window.endswith("P" * DEPARTURE_RUN):
        reason = "ball_unchanged" if set(states) == {"P"} else "ball_visible_after_occlusion"
        return "still_there", reason, None
    if window.count("O") * 2 >= len(window):
        return "unclear", "occluded", None
    return "unclear", "ambiguous_change", None


def spot_contrast(reference: np.ndarray, ball: ReferenceBall) -> float | None:
    """The check's pre-edge contrast at ``ball`` (core-disk vs mat-ring medians).

    ``reference`` is the (n, h, w) pre-edge stack; None when the ring leaves
    the image.
    """
    masks = _masks(ball, reference.shape[1:])
    if masks is None:
        return None
    box, disk, ring = masks
    crop = reference[:, box[0], box[1]].astype(np.float32)
    return _Spot.from_reference(crop, disk, ring).contrast


def _at(levels: np.ndarray | None, index: np.ndarray) -> np.ndarray | None:
    """The lamp levels of the frames ``index``, None for a calm clip."""
    return None if levels is None else levels[index]


def _spot_at(frames: np.ndarray, ball: ReferenceBall, reference_idx: np.ndarray, levels=None):
    """(spot model, float crop of every frame, disk, ring) at ``ball``; None at the image edge.

    ``levels`` holds every frame's lamp level when the clip flickers.
    """
    masks = _masks(ball, frames.shape[1:])
    if masks is None:
        return None
    box, disk, ring = masks
    crop = frames[:, box[0], box[1]].astype(np.float32)
    spot = _Spot.from_reference(crop[reference_idx], disk, ring, _at(levels, reference_idx))
    return spot, crop, disk, ring


def _spot_beside(
    frames: np.ndarray,
    ball: ReferenceBall,
    reference_idx: np.ndarray,
    judged: ReferenceBall | None,
    levels=None,
):
    """``_spot_at`` with the mat ring cleared of the judged spot's disk.

    A static spot the check judged sits in the ring of a ball 12-17 px beside
    it; its contrast against the ring would hide the ball's. None at the image
    edge or when fewer than ``MIN_RING_PIXELS`` ring pixels are left.
    """
    masks = _masks(ball, frames.shape[1:])
    if masks is None:
        return None
    box, disk, ring = masks
    if judged is not None:
        yy, xx = np.mgrid[box[0], box[1]]
        clear = JUDGED_CLEAR_FRAC * judged.diameter_px
        ring = ring & (((xx - judged.x) ** 2 + (yy - judged.y) ** 2) > clear * clear)
        if np.count_nonzero(ring) < MIN_RING_PIXELS:
            return None
    crop = frames[:, box[0], box[1]].astype(np.float32)
    spot = _Spot.from_reference(crop[reference_idx], disk, ring, _at(levels, reference_idx))
    return spot, crop, disk, ring


def _changed_spots(  # pylint: disable=too-many-arguments,too-many-locals
    frames: np.ndarray,
    reference_idx: np.ndarray,
    window_idx: np.ndarray,
    *,
    box: tuple[int, int, int, int],
    diameter_px: float,
    judged: ReferenceBall,
    min_contrast: float = MIN_CONTRAST,
) -> list[tuple[ReferenceBall, float]]:
    """Up to ``CHANGED_SPOT_LIMIT`` spots near ``judged`` whose core changed most across the edge.

    Candidates lie between half a diameter and ``JUDGED_SEARCH_DIAMETERS``
    from ``judged`` and inside the zone ``box``; peaks of the core-disk mean
    of |median(window) - median(reference)| are suppressed within half a
    diameter of each other. Returns each spot with its signed pre-edge
    contrast against its ring cleared of ``judged`` (``_spot_beside``).
    """
    reach = JUDGED_SEARCH_DIAMETERS * diameter_px
    x0, y0 = max(box[0], math.floor(judged.x - reach)), max(box[1], math.floor(judged.y - reach))
    x1 = min(box[2], math.ceil(judged.x + reach) + 1)
    y1 = min(box[3], math.ceil(judged.y + reach) + 1)
    if x1 <= x0 or y1 <= y0:
        return []
    core = max(2.0, diameter_px * BALL_PATCH_RADIUS_FRAC)
    half = math.ceil(core)
    ky, kx = np.mgrid[-half : half + 1, -half : half + 1]
    kernel = (kx * kx + ky * ky <= core * core).astype(np.float32)
    # The core disks of centres at the edge of the centre box reach ``half``
    # pixels beyond it: convolve a crop that holds them, then keep the centres.
    height, width = frames.shape[1:]
    cx0, cy0 = max(0, x0 - half), max(0, y0 - half)
    crop = frames[:, cy0 : min(height, y1 + half), cx0 : min(width, x1 + half)]
    before = np.median(crop[reference_idx].astype(np.float32), axis=0)
    after = np.median(crop[window_idx].astype(np.float32), axis=0)
    response = signal.fftconvolve(np.abs(after - before), kernel / kernel.sum(), mode="same")
    response = response[y0 - cy0 : y1 - cy0, x0 - cx0 : x1 - cx0]
    yy, xx = np.mgrid[y0:y1, x0:x1]
    distance_sq = (xx - judged.x) ** 2 + (yy - judged.y) ** 2
    response[(distance_sq > reach * reach) | (distance_sq < (diameter_px / 2.0) ** 2)] = -1.0
    suppress = max(1, round(diameter_px / 2.0))
    area = round(math.pi * (diameter_px / 2.0) ** 2)
    spots = []
    for _ in range(CHANGED_SPOT_LIMIT):
        peak = np.unravel_index(int(np.argmax(response)), response.shape)
        row, col = int(peak[0]), int(peak[1])
        if response[row, col] < CHANGED_SPOT_MIN_DELTA:
            break
        response[
            max(0, row - suppress) : row + suppress + 1, max(0, col - suppress) : col + suppress + 1
        ] = -1.0
        spot = ReferenceBall(float(x0 + col), float(y0 + row), float(diameter_px), area)
        model = _spot_beside(frames, spot, reference_idx, judged)
        # Keep only spots the ranking loop can judge: a low-contrast spot on the
        # shoulder of the ball's own change ranks far above the ball (change over
        # a contrast of 1-12) and, skipped there, still used up one of the
        # ZONE_CANDIDATES slots (2026-10-26 review what-ifs: the ball ranked
        # fifth and the stale anchor blocked one more strike).
        if model is not None and model[0].failure(min_contrast) is None:
            spots.append((spot, model[0].signed_contrast))
    return spots


def _core_change(
    frames: np.ndarray, ball: ReferenceBall, reference_idx: np.ndarray, window_idx: np.ndarray
) -> float:
    """Mean change of the core disk between the reference and the decision window."""
    core = max(2.0, ball.diameter_px * BALL_PATCH_RADIUS_FRAC)
    half = math.ceil(core)
    cx, cy = round(ball.x), round(ball.y)
    patch = frames[:, cy - half : cy + half + 1, cx - half : cx + half + 1]
    yy, xx = np.mgrid[cy - half : cy + half + 1, cx - half : cx + half + 1]
    disk = (xx - ball.x) ** 2 + (yy - ball.y) ** 2 <= core * core
    before = np.median(patch[reference_idx][:, disk].astype(np.float32), axis=0)
    after = np.median(patch[window_idx][:, disk].astype(np.float32), axis=0)
    return float(np.mean(np.abs(after - before)))


def _departed_zone_spot(  # pylint: disable=too-many-arguments,too-many-locals
    frames: np.ndarray,
    reference_idx: np.ndarray,
    view: tuple[np.ndarray, np.ndarray],
    *,
    window_ms: tuple[float, float],
    zone: TeeZone | None = None,
    judged: ReferenceBall | None,
    judged_static: bool = False,
    area: _SearchArea | None = None,
    levels=None,
) -> tuple[ReferenceBall, float] | None:
    """The strongest search-area spot (other than ``judged``) that left in this clip.

    The area is ``area``, else the hitting ``zone`` (``_SearchArea.of_zone``);
    ``levels`` holds every frame's lamp level when the clip flickers.

    A spot within half a diameter of ``judged`` is ``judged`` itself; a ball
    that left beside it (a static ball copy 15 px away, 2026-10-25 what-if) is
    another spot. Within ``JUDGED_SEARCH_DIAMETERS`` of ``judged`` the scan
    also takes spots suppressed only within half a diameter of a stronger
    one: the zone-wide search suppresses a one-diameter square, so a ball
    leaving 12-17 px beside a stronger static spot was never a candidate,
    the block was never contradicted and every later strike was blocked
    (2026-10-25 what-ifs). Zone-wide, half-diameter suppression let spots
    beside stronger ones far from the tee "leave" in no-shot clips and
    contradict a good anchor (2026-10-25 range replay), so it stays local.
    A ball beside a static spot 1.5-2x as dark is no peak even then, and a
    ball 16-17 px away has the static spot in its mat ring: the scan also
    ranks the spots whose core changed most across the edge
    (``_changed_spots``), and models every candidate with its ring cleared of
    ``judged`` (``_spot_beside``; 2026-10-26 review what-ifs: a stale anchor
    blocked 4-12 strikes in a row without both). The changed-spot search
    runs only when ``judged_static`` (the check judged ``judged``
    still_there, as at a stale anchor): beside a spot too dark to judge, a
    shadow edge creeping across the mat "left like a struck ball" in a
    no-shot clip (2026-10-25 clip 184248_137_135, 9 px from the judged spot),
    which would have contradicted a good anchor had the gate blocked that edge.

    ``view`` holds the judged frame indices and their offsets from the edge.
    Returns the spot and its signed pre-edge contrast (``_Spot``).
    """
    view_idx, offsets = view
    window_idx = view_idx[(offsets >= window_ms[0]) & (offsets <= window_ms[1])]
    if len(window_idx) < MIN_WINDOW_FRAMES:
        return None
    if area is None:
        area = _SearchArea.of_zone(zone)
    reference = frames[reference_idx]
    box, diameter_px, floor = area.box, area.diameter_px, area.min_contrast
    candidates = find_balls(
        reference, box, diameter_px, limit=ZONE_SEARCH_LIMIT, min_contrast=floor
    )
    if judged is not None:
        reach = JUDGED_SEARCH_DIAMETERS * diameter_px
        seen = {(candidate.x, candidate.y) for candidate, _contrast in candidates}
        candidates += [
            (candidate, contrast)
            for candidate, contrast in find_balls(
                reference,
                (
                    max(box[0], math.floor(judged.x - reach)),
                    max(box[1], math.floor(judged.y - reach)),
                    min(box[2], math.ceil(judged.x + reach) + 1),
                    min(box[3], math.ceil(judged.y + reach) + 1),
                ),
                diameter_px,
                limit=ZONE_SEARCH_LIMIT,
                min_contrast=floor,
                within=(judged.x, judged.y, reach),
                suppress_px=max(1, round(diameter_px / 2.0)),
            )
            if (candidate.x, candidate.y) not in seen
        ]
    if judged is not None and judged_static:
        seen = {(candidate.x, candidate.y) for candidate, _contrast in candidates}
        candidates += [
            (candidate, contrast)
            for candidate, contrast in _changed_spots(
                frames,
                reference_idx,
                window_idx,
                box=box,
                diameter_px=diameter_px,
                judged=judged,
                min_contrast=floor,
            )
            if (candidate.x, candidate.y) not in seen
        ]
    ranked = []
    for candidate, contrast in candidates:
        if judged is not None and math.hypot(candidate.x - judged.x, candidate.y - judged.y) < (
            max(candidate.diameter_px, judged.diameter_px) / 2.0
        ):
            continue
        change = _core_change(frames, candidate, reference_idx, window_idx) / abs(contrast)
        ranked.append((change, abs(contrast), candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    best, best_strength = None, 0.0
    for change, strength, candidate in ranked[:ZONE_CANDIDATES]:
        if change < PRESENT_MAX_CHANGE:
            break
        model = _spot_beside(frames, candidate, reference_idx, judged, levels)
        if model is None or model[0].failure(floor) is not None:
            continue
        states = model[0].states(model[1][view_idx], _at(levels, view_idx))
        decision, _reason, departure_ms = _decide(states, offsets, window_ms)
        if (
            decision == "departed"
            and _struck(states, offsets, departure_ms)
            and strength > best_strength
        ):
            best, best_strength = (candidate, model[0].signed_contrast), strength
    return best


def _struck(states: str, offsets: np.ndarray, departure_ms: float | None) -> bool:
    """Whether a departed spot left like a struck ball, not a waggled club or a raked ball.

    Up to ``EARLY_DEPARTURE_MS[1]`` before the edge it was present or covered
    by the club (2026-10-25 strikes changed 16 ms before the edge at the
    earliest; a waggled club head next to the ball changed 22-78 ms before),
    and it was gone by ``STRIKE_DEPARTURE_MS`` after the edge. Unlit frames
    (``UNLIT_STATE``) are skipped.
    """
    states, offsets = _lit(states, offsets)
    before = {state for state, offset in zip(states, offsets) if offset <= EARLY_DEPARTURE_MS[1]}
    return before <= {"P", "O"} and departure_ms is not None and departure_ms <= STRIKE_DEPARTURE_MS


def _judge(
    model, view: tuple[np.ndarray, np.ndarray], window_ms: tuple[float, float], levels=None
) -> tuple[str, str, dict]:
    """(decision, reason, evidence fields) at a spot modelled by ``_spot_at`` (enough contrast)."""
    spot, crop, disk, ring = model
    view_idx, offsets = view
    disks, rings = crop[view_idx][:, disk], crop[view_idx][:, ring]
    states = spot.states(crop[view_idx], _at(levels, view_idx))
    decision, reason, departure_ms = _decide(states, offsets, window_ms)
    in_window = (offsets >= window_ms[0]) & (offsets <= window_ms[1])
    evidence = {
        "first_departure_ms_after_edge": departure_ms,
        "frames_used": int(np.count_nonzero(in_window)),
        "frame_states": states,
        "frame_offsets_ms": tuple(float(value) for value in offsets),
    }
    if np.any(in_window):
        evidence["post_contrast"] = float(
            np.mean(
                np.abs(np.median(disks[in_window], axis=1) - np.median(rings[in_window], axis=1))
            )
        )
    return decision, reason, evidence


def check_ball_departure(  # pylint: disable=too-many-locals,too-many-arguments,too-many-statements,too-many-return-statements
    frames: np.ndarray,
    timestamps_ns: np.ndarray,
    trigger_ns: int,
    ball_hint: ReferenceBall | None = None,
    *,
    zone: TeeZone | None = None,
    ball_mark_px: tuple[float, float] | None = None,
    window_ms: tuple[float, float] = WINDOW_MS,
    exposure_gain=None,
) -> TriggerCheck:
    """Decide whether the teed ball left its spot just after ``trigger_ns``.

    ``frames`` is the (n, height, width) clip, ``timestamps_ns`` the per-frame
    exposure times on the trigger's clock (see ``exposure_timestamps_ns``),
    ``ball_hint`` the session tee anchor to search around first, ``zone``
    the hitting zone and ``ball_mark_px`` the tapped resting ball in frame
    pixels (see the module docstring). ``exposure_gain`` holds the
    per-frame exposure time x analogue gain (us), which normalises the
    departure contrast for the tee anchor.
    """
    started = time.perf_counter()
    found: dict = {}
    # Set once the clip is usable: reference frame indices and the judged view;
    # then the located ball's signed contrast and the cached zone departure.
    clip: dict = {}

    def zone_departure(decision: str) -> tuple[ReferenceBall, float] | None:
        """The zone spot seen leaving, other than the located ball (scanned once).

        ``decision`` is the check's verdict at the located ball; the first
        call's verdict decides whether spots beside a static one are searched.
        """
        if "departure" not in clip:
            clip["departure"] = _departed_zone_spot(
                frames,
                clip["reference_idx"],
                clip["view"],
                window_ms=window_ms,
                area=clip["area"],
                judged=_ball_or_none(
                    found.get("ball_x"), found.get("ball_y"), found.get("ball_diameter_px")
                ),
                judged_static=decision == "still_there",
                levels=clip.get("levels"),
            )
        return clip["departure"]

    def result(decision: str, reason: str, **fields) -> TriggerCheck:
        checked = (time.perf_counter() - started) * 1000.0
        fields = {**found, **fields}
        departure, source, contrast = None, None, None
        if decision == "departed" and fields.get("ball_source") in ANCHOR_SOURCES:
            departure = _ball_or_none(
                fields.get("ball_x"), fields.get("ball_y"), fields.get("ball_diameter_px")
            )
            source, contrast = fields["ball_source"], clip.get("ball_contrast")
        elif clip.get("area") is not None:
            spot = zone_departure(decision)
            if spot is not None:
                (departure, contrast), source = spot, clip["area"].source
        if departure is not None:
            view_idx, offsets = clip["view"]
            footprint = footprint_diameter_px(
                frames,
                clip["reference_idx"],
                view_idx[(offsets >= window_ms[0]) & (offsets <= window_ms[1])],
                departure,
            )
            # Capped by the zone's geometric size (or the size the ball-mark
            # area expects), not by the hint's: a hint shrunk by earlier
            # footprints would otherwise ratchet down.
            area = clip.get("area")
            nominal = area.diameter_px if area is not None else departure.diameter_px
            fields.update(
                departure_x=departure.x,
                departure_y=departure.y,
                departure_diameter_px=kernel_diameter_px(nominal, footprint),
                departure_footprint_px=footprint,
                departure_source=source,
                departure_contrast=contrast,
            )
        # decide_ms leaves the departure scan out: a live gate never runs it.
        fields.setdefault("locate_ms", checked)
        fields.setdefault("decide_ms", checked - fields["locate_ms"])
        total = (time.perf_counter() - started) * 1000.0
        return TriggerCheck(decision, reason, compute_ms=total, **fields)

    frames = np.asarray(frames)
    offsets_all = (np.asarray(timestamps_ns, dtype=np.int64) - int(trigger_ns)) / 1e6
    if (
        frames.ndim != 3
        or len(frames) != len(offsets_all)
        or min(frames.shape[1:]) < 2 * MIN_SEARCH_HALF_PX
    ):
        return result("unclear", "invalid_frames")
    reference_idx = np.flatnonzero(offsets_all <= REFERENCE_END_MS)[:REFERENCE_FRAMES]
    if len(reference_idx) < 3:
        return result("unclear", "insufficient_pre_frames")
    view_idx = np.flatnonzero(
        (offsets_all >= EARLY_DEPARTURE_MS[0]) & (offsets_all <= window_ms[1])
    )
    clip.update(reference_idx=reference_idx, view=(view_idx, offsets_all[view_idx]))
    if exposure_gain is not None:
        gains = np.broadcast_to(np.asarray(exposure_gain, dtype=np.float64), offsets_all.shape)
        found["exposure_gain"] = float(np.median(gains[reference_idx]))

    reference = frames[reference_idx]
    window = None
    if ball_mark_px is not None:
        found["ball_mark"] = (float(ball_mark_px[0]), float(ball_mark_px[1]))
        window = mark_window(frames.shape[1:], found["ball_mark"])
        if window is None:
            return result("unclear", "ball_mark_off_frame")
        clip["area"] = _mark_area(reference, window, zone)
    elif zone is not None:
        clip["area"] = _SearchArea.of_zone(zone)
    ball, source = _locate_ball(reference, ball_hint, clip.get("area"), window)
    found["locate_ms"] = (time.perf_counter() - started) * 1000.0
    if ball is None:
        return result("unclear", _scene_reason(reference[0]))
    found.update(
        ball_x=ball.x, ball_y=ball.y, ball_diameter_px=ball.diameter_px, ball_source=source
    )
    # Mains flicker: each frame is judged against the reference frames of its
    # lamp level (``_Spot.states``). Only the frames up to the window end count.
    last = int(max(reference_idx.max(), view_idx.max() if len(view_idx) else 0))
    levels, flicker = ball_flicker_levels(
        frames[: last + 1], tee_zone=zone, ball_mark_px=found.get("ball_mark")
    )
    clip["levels"] = levels
    found["flicker"] = flicker.to_dict()
    model = _spot_at(frames, ball, reference_idx, levels) if _plausible(ball) else None
    if model is None:
        return result("unclear", "implausible_ball")

    found["pre_contrast"] = model[0].contrast
    found["clipped_fraction"] = model[0].clipped_fraction
    clip["ball_contrast"] = model[0].signed_contrast
    failure = model[0].failure(MIN_CONTRAST if window is None else MARK_MIN_CONTRAST)
    if failure is not None:
        return result("unclear", failure)

    decision, reason, evidence = _judge(model, clip["view"], window_ms, levels)
    struck = decision == "departed" and _struck(
        evidence["frame_states"], clip["view"][1], evidence["first_departure_ms_after_edge"]
    )
    if source in ("zone", "ball_mark") and decision in ("still_there", "departed") and not struck:
        # No shot has shown a zone spot to be the tee ball (2026-10-25: the
        # tree line, a spare ball, a waggled club head), nor the strongest
        # spot near a tap: judge the area spot that left like a struck ball,
        # if any.
        found["decide_ms"] = (time.perf_counter() - started) * 1000.0 - found["locate_ms"]
        left = zone_departure(decision)
        if left is None:
            reason = "zone_spot_static" if decision == "still_there" else "zone_spot_not_struck"
            decision = "unclear"
            evidence["first_departure_ms_after_edge"] = None
        else:
            # The scan modelled and judged this spot already, its ring cleared
            # of the located spot: it departed.
            moved = _spot_beside(frames, left[0], reference_idx, ball, levels)
            found.update(
                ball_x=left[0].x,
                ball_y=left[0].y,
                ball_diameter_px=left[0].diameter_px,
                pre_contrast=moved[0].contrast,
                clipped_fraction=moved[0].clipped_fraction,
            )
            clip["ball_contrast"] = moved[0].signed_contrast
            decision, reason, evidence = _judge(moved, clip["view"], window_ms, levels)
    return result(decision, reason, **evidence)


def _exposure_gain(frames) -> float | None:
    """Median exposure time x analogue gain (us) of live frames, None without metadata."""
    levels = [
        float(frame.exposure_us) * float(frame.analogue_gain)
        for frame in frames
        if getattr(frame, "exposure_us", None) is not None
        and getattr(frame, "analogue_gain", None) is not None
    ]
    return float(np.median(levels)) if levels else None


def _matching_spot(
    images: list[np.ndarray], ball: ReferenceBall, exposure_gain: float, expected: float
) -> bool:
    """Whether the pre-edge spot at ``ball`` also matches the anchor's contrast."""
    masks = _masks(ball, images[0].shape) if _plausible(ball) else None
    if masks is None:
        return False
    box, disk, ring = masks
    spot = _Spot.from_reference(
        np.stack([image[box] for image in images]).astype(np.float32), disk, ring
    )
    contrast = normalised_contrast(spot.signed_contrast, exposure_gain)
    return contrast is not None and spot_matches(contrast, expected)


@dataclass(frozen=True)
class LiveVerdict:
    """A live gate decision: ``departed``, ``still_there`` or ``unclear``."""

    decision: str
    reason: str
    first_departure_ms_after_edge: float | None = None


class LiveDepartureCheck:
    """``check_ball_departure`` fed frame by frame, for the live trigger gate.

    The ball is re-found within ``match_radius_px`` of the confirmed session
    tee anchor (``find_ball_near``, true peaks only) in the latest
    ``REFERENCE_FRAMES`` frames exposed at least ``-REFERENCE_END_MS`` ms
    before the edge; there is no wider search, so a mat mark beside a missed
    ball cannot stand in for it (no ball there: ``unclear``). With an
    ``AnchorBall`` contrast, a spot whose normalised contrast does not match it
    (``spot_matches``) is not judged either: ``unclear``/``spot_mismatch``
    (``spot_unverified`` when the frames carry no exposure and gain). When a
    second true peak inside the circle matches it as well, the gate cannot
    tell which spot is the tee ball and does not judge the stronger one:
    ``unclear``/``two_spots`` (a static ball copy 15 px beside the teed ball
    otherwise blocked every strike). The same holds when the judged peak lies
    more than half a diameter from the anchor and the spot at the anchor
    centre matches too: a stronger static ball one diameter in front of the
    teed ball outshines it within half a diameter, so the teed ball is no
    true peak (2026-10-25 what-if: strikes blocked five in a row).
    ``add`` returns a verdict
    as soon as one is certain: ``departed`` once ``DEPARTURE_RUN`` post-edge
    frames in a row are gone, ``unclear`` when the first frames delivered hold
    no usable reference. ``finish`` decides at the deadline: ``still_there``
    needs ``MIN_WINDOW_FRAMES`` frames from ``WINDOW_MS[0]`` on, post-edge
    frames that end on ``DEPARTURE_RUN`` present frames, and the last of them
    exposed ``STILL_THERE_EVIDENCE_MS`` or more after the edge (range shots
    left the tee 8-30 ms after it). Everything else is ``unclear``.

    Frames need ``image``, ``host_timestamp_ns`` and ``sensor_timestamp_ns``
    on the edge's monotonic clock. The sensor clock is used when the first
    frames show it consistent, as in ``exposure_timestamps_ns``.
    """

    def __init__(self, edge_ns: int, anchor: ReferenceBall):
        self.edge_ns = int(edge_ns)
        self.anchor = anchor
        self.ball: ReferenceBall | None = None
        # Signed pre-edge contrast of the spot found near the anchor: grey
        # levels, normalised (``normalised_contrast``) once compared with the anchor's.
        self.spot_contrast: float | None = None
        self.states = ""
        self.offsets_ms: list[float] = []
        self._use_sensor = False
        self._box: tuple[slice, slice] | None = None
        self._spot: _Spot | None = None
        self._verdict: LiveVerdict | None = None

    def _offset_ms(self, frame) -> float:
        stamp = frame.sensor_timestamp_ns if self._use_sensor else frame.host_timestamp_ns
        return (int(stamp) - self.edge_ns) / 1e6

    def _build_reference(self, frames) -> bool:
        """Model the spot from the first delivered frames; False with a verdict if impossible."""
        host = np.asarray([frame.host_timestamp_ns for frame in frames], dtype=np.int64)
        sensor = np.asarray([frame.sensor_timestamp_ns for frame in frames], dtype=np.int64)
        self._use_sensor = _sensor_clock_usable(host, sensor)
        offsets = np.asarray([self._offset_ms(frame) for frame in frames])
        reference_idx = np.flatnonzero(offsets <= REFERENCE_END_MS)[-REFERENCE_FRAMES:]
        if len(reference_idx) < 3:
            self._verdict = LiveVerdict("unclear", "insufficient_pre_frames")
            return False
        images = [np.asarray(frames[index].image) for index in reference_idx]
        shape = images[0].shape
        peaks = balls_near(images, self.anchor, limit=2) if len(shape) == 2 else []
        ball = peaks[0] if peaks else None
        masks = _masks(ball, shape) if ball is not None and _plausible(ball) else None
        if masks is None:
            reason = _scene_reason(images[0]) if ball is None else "implausible_ball"
            self._verdict = LiveVerdict("unclear", reason)
            return False
        box, disk, ring = masks
        spot = _Spot.from_reference(
            np.stack([image[box] for image in images]).astype(np.float32), disk, ring
        )
        self.spot_contrast = spot.signed_contrast
        failure = spot.failure()
        if failure is not None:
            self._verdict = LiveVerdict("unclear", failure)
            return False
        others = list(peaks[1:])
        if math.hypot(ball.x - self.anchor.x, ball.y - self.anchor.y) > ball.diameter_px / 2.0:
            # The teed ball need not be a true peak: a stronger static ball
            # one diameter in front of it outshines it within half a diameter.
            others.append(
                ReferenceBall(
                    float(self.anchor.x), float(self.anchor.y), ball.diameter_px, ball.area_px
                )
            )
        mismatch = self._mismatch(
            spot, images, others, _exposure_gain([frames[index] for index in reference_idx])
        )
        if mismatch is not None:
            self._verdict = LiveVerdict("unclear", mismatch)
            return False
        self.ball, self._box, self._spot = ball, box, spot
        return True

    def _mismatch(
        self, spot: _Spot, images: list[np.ndarray], others: list[ReferenceBall], exposure_gain
    ) -> str | None:
        """Why the spot is not judged against the anchor's contrast, None when it is.

        Sets ``spot_contrast`` to the normalised contrast. ``others`` are the
        further true peaks within the match radius.
        """
        expected = self.anchor.contrast if isinstance(self.anchor, AnchorBall) else None
        if expected is None:
            return None
        self.spot_contrast = normalised_contrast(spot.signed_contrast, exposure_gain)
        if self.spot_contrast is None:
            return "spot_unverified"
        if not spot_matches(self.spot_contrast, expected):
            return "spot_mismatch"
        if any(_matching_spot(images, other, exposure_gain, expected) for other in others):
            return "two_spots"
        return None

    def add(self, frames) -> LiveVerdict | None:
        """Classify newly delivered frames; returns a verdict once it is certain."""
        frames = list(frames)
        if self._verdict is not None or not frames:
            return self._verdict
        if self._spot is None and not self._build_reference(frames):
            return self._verdict
        for frame in frames:
            offset = self._offset_ms(frame)
            if offset < 0.0:
                continue
            crop = np.asarray(frame.image)[self._box].astype(np.float32)
            self.states += self._spot.states(crop[None])
            self.offsets_ms.append(offset)
        run_end = _first_run_end(self.states, "G")
        if run_end is not None:
            self._verdict = LiveVerdict("departed", "ball_left_spot", self.offsets_ms[run_end])
        return self._verdict

    def finish(self) -> LiveVerdict:
        """Decide at the deadline from the frames seen so far."""
        if self._verdict is not None:
            return self._verdict
        window = "".join(s for s, t in zip(self.states, self.offsets_ms) if t >= WINDOW_MS[0])
        if self._spot is None:
            reason = "insufficient_pre_frames"
        elif len(window) < MIN_WINDOW_FRAMES:
            reason = "insufficient_post_frames"
        elif not self.states.endswith("P" * DEPARTURE_RUN):
            reason = "occluded" if window.count("O") * 2 >= len(window) else "ambiguous_change"
        elif self.offsets_ms[-1] < STILL_THERE_EVIDENCE_MS:
            reason = "window_incomplete"
        elif set(self.states) == {"P"}:
            return LiveVerdict("still_there", "ball_unchanged")
        else:
            return LiveVerdict("still_there", "ball_visible_after_occlusion")
        return LiveVerdict("unclear", reason)
