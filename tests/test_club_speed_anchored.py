"""Club speed read at the end of the club line, anchored at the sound trigger.

The legacy rule finds ball onset by speed alone. On high-loft wedges the rising
club line crosses the (cosine-compressed) radial ball speed 8-12 ms before
impact, so it reads the club 10-18 ms early at ~0.76 of its impact value. The
anchored rule takes the post-impact level just before the trigger, walks back
to where the fastest line leaves it, and reads the club there.

The real-capture fixture holds two 48-degree wedge shots from the 2026-10-09
range session (A8, A11) and the 22 good TrackMan pairs of 2026-05-06; only I/Q,
the trigger offset, the club label and the TrackMan club speed are stored.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np
import pytest

from openflight.clubs import ClubType
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.types import IQCapture, SpeedReading, SpeedTimeline

FIXTURE = Path(__file__).parent / "fixtures" / "club_speed" / "ops243_club_speed_captures.npz"

STEP_MS = 32 / 30.0
CENTRE_MS = 128 / 30.0 / 2.0
N_FRAMES = 124
TRIGGER_MS = 70.0

Line = Callable[[float], Optional[tuple[float, float]]]


def _ramp(t0: float, v0: float, t1: float, v1: float, t: float) -> float:
    return v0 + (v1 - v0) * min(max((t - t0) / (t1 - t0), 0.0), 1.0)


def _timeline(
    lines: Iterable[Line],
    clipped_samples: Optional[slice] = None,
) -> SpeedTimeline:
    """Overlapping-frame timeline; each line maps a frame centre (ms) to (mph, magnitude)."""
    lines = list(lines)
    readings = []
    for k in range(N_FRAMES):
        start_ms = k * STEP_MS
        for line in lines:
            value = line(start_ms + CENTRE_MS)
            if value is not None:
                readings.append(SpeedReading(value[0], value[1], start_ms, "outbound"))
    i = np.full(4096, 2048, dtype=int)
    q = np.full(4096, 2048, dtype=int)
    if clipped_samples is not None:
        i[clipped_samples] = 4095
    capture = IQCapture(
        sample_time=0.0,
        trigger_time=TRIGGER_MS / 1000.0,
        i_samples=i.tolist(),
        q_samples=q.tolist(),
    )
    return SpeedTimeline(readings=readings, sample_rate_hz=937.5, capture=capture)


def _head(peak: float, end_ms: float, mag: float = 1.0, start_ms: float = 25.0) -> Line:
    """Club head accelerating to ``peak`` 10 ms before it ends at ``end_ms``."""

    def line(t: float):
        if t < start_ms or t > end_ms:
            return None
        return _ramp(start_ms, 0.5 * peak, end_ms - 10.0, peak, t), mag

    return line


def _ball(start_mph: float, end_mph: float, start_ms: float, mag: float = 3.0) -> Line:
    """Ball line from ``start_ms`` that settles at ``end_mph`` 4 ms later."""

    def line(t: float):
        if t < start_ms:
            return None
        return _ramp(start_ms, start_mph, start_ms + 4.0, end_mph, t), mag

    return line


def _constant(mph: float, mag: float, start_ms: float = 0.0, end_ms: float = 1e9) -> Line:
    def line(t: float):
        return (mph, mag) if start_ms <= t <= end_ms else None

    return line


def _last_centre_before(t: float) -> float:
    return np.floor((t - CENTRE_MS) / STEP_MS) * STEP_MS + CENTRE_MS


def _fixture() -> dict:
    with np.load(FIXTURE) as data:
        return {key: data[key] for key in data.files}


def _process(fixture: dict, index: int, processor: RollingBufferProcessor):
    trigger_ms = float(fixture["trigger_offset_ms"][index])
    capture = IQCapture(
        sample_time=0.0,
        trigger_time=trigger_ms / 1000.0,
        i_samples=fixture["i_samples"][index].astype(int).tolist(),
        q_samples=fixture["q_samples"][index].astype(int).tolist(),
    )
    return processor.process_capture(capture, anchor_club_to_trigger=True)


class TestAnchoredClubSpeed:
    """Club speed from the end of the club line, anchored at the sound trigger."""

    @pytest.fixture
    def processor(self) -> RollingBufferProcessor:
        return RollingBufferProcessor()

    def test_wedge_merge_reads_head_at_line_end(self, processor):
        line_end = TRIGGER_MS - 6.0
        timeline = _timeline([_head(80.0, line_end), _ball(66.0, 75.0, line_end)])

        club, _ = processor.find_club_speed(
            timeline, 75.0, line_end + 10.0, trigger_offset_ms=TRIGGER_MS
        )
        legacy, _ = processor.find_club_speed(timeline, 75.0, line_end + 10.0)

        assert club == pytest.approx(80.0, abs=1.0)
        assert legacy is None or abs(legacy - 80.0) > 5.0

    def test_iron_head_wins_over_stronger_hosel_line(self, processor):
        line_end = TRIGGER_MS - 6.0
        timeline = _timeline(
            [
                _head(86.0, line_end, mag=0.3),
                _head(78.0, line_end, mag=1.0),
                _ball(120.0, 120.0, line_end + 1.0),
            ]
        )

        club, club_ms = processor.find_club_speed(
            timeline, 120.0, line_end + 10.0, trigger_offset_ms=TRIGGER_MS
        )

        assert club == pytest.approx(86.0, abs=1.0)
        assert club_ms + CENTRE_MS <= line_end + 1e-6

    def test_late_upper_head_line_wins(self, processor):
        line_end = TRIGGER_MS - 6.0
        timeline = _timeline(
            [
                _head(78.0, line_end, mag=1.0),
                _constant(86.0, 0.4, start_ms=line_end - 10.0, end_ms=line_end),
                _ball(120.0, 120.0, line_end + 1.0),
            ]
        )

        club, _ = processor.find_club_speed(
            timeline, 120.0, line_end + 10.0, trigger_offset_ms=TRIGGER_MS
        )

        assert club == pytest.approx(86.0, abs=1.0)

    def test_two_spikes_in_read_window_are_ignored(self, processor):
        line_end = TRIGGER_MS - 6.0
        last_centre = _last_centre_before(line_end)
        spike_centres = {last_centre - 2 * STEP_MS, last_centre - 4 * STEP_MS}

        def spikes(t: float):
            return (110.0, 5.0) if any(abs(t - c) < 1e-6 for c in spike_centres) else None

        timeline = _timeline([_head(86.0, line_end), _ball(120.0, 120.0, line_end + 1.0), spikes])

        club, _ = processor.find_club_speed(
            timeline, 120.0, line_end + 10.0, trigger_offset_ms=TRIGGER_MS
        )

        assert club == pytest.approx(86.0, abs=1.0)

    def test_ghost_line_in_clipped_windows_is_ignored(self, processor):
        line_end = TRIGGER_MS - 6.0
        burst_ms = line_end - 5.0
        burst = int(round(burst_ms * 30.0))
        clipped = slice(burst, burst + 8)

        def ghost(t: float):
            start = int(round((t - CENTRE_MS) * 30.0))
            overlap = min(start + 128, burst + 8) - max(start, burst)
            return (1.24 * 120.0, 2.0) if overlap / 128 > 0.05 else None

        lines = [_head(86.0, line_end), _ball(120.0, 120.0, line_end + 1.0), ghost]

        club, _ = processor.find_club_speed(
            _timeline(lines, clipped_samples=clipped),
            120.0,
            line_end + 10.0,
            trigger_offset_ms=TRIGGER_MS,
        )
        ghosted, _ = processor.find_club_speed(
            _timeline(lines), 120.0, line_end + 10.0, trigger_offset_ms=TRIGGER_MS
        )

        assert club == pytest.approx(86.0, abs=1.0)
        assert ghosted == pytest.approx(1.24 * 120.0)

    def test_flat_line_returns_none_without_magnitude_fallback(self, processor):
        timeline = _timeline([_constant(53.0, 3.0), _constant(40.0, 1.0)])

        anchored = processor.find_club_speed(timeline, 53.0, 40.0, trigger_offset_ms=TRIGGER_MS)
        legacy, _ = processor.find_club_speed(timeline, 53.0, 40.0)

        assert anchored == (None, None)
        assert legacy == pytest.approx(40.0)

    def test_smooth_merge_reads_window_ending_before_trigger(self, processor):
        def merge_speed(t: float) -> float:
            if t < TRIGGER_MS - 14.0:
                return _ramp(25.0, 40.0, TRIGGER_MS - 14.0, 76.0, t)
            return _ramp(TRIGGER_MS - 14.0, 76.0, TRIGGER_MS - 6.0, 80.0, t)

        timeline = _timeline([lambda t: (merge_speed(t), 2.0)])

        estimate = processor._find_club_speed_anchored(timeline, 80.0, TRIGGER_MS)

        line_end = TRIGGER_MS - processor.CLUB_ANCHOR_MERGE_LATENCY_MS
        in_window = sorted(
            merge_speed(k * STEP_MS + CENTRE_MS)
            for k in range(N_FRAMES)
            if line_end - processor.CLUB_ANCHOR_WINDOW_MS <= k * STEP_MS + CENTRE_MS <= line_end
        )
        assert estimate.mode == "anchored"
        assert estimate.line_end_ms == pytest.approx(TRIGGER_MS - 6.4)
        assert estimate.speed_mph == pytest.approx(in_window[-processor.CLUB_ANCHOR_RANK])

    @pytest.mark.parametrize("club_type", [ClubType.UNKNOWN, ClubType.SW, ClubType.IRON_7])
    def test_without_trigger_offset_behaviour_is_unchanged(self, processor, club_type):
        line_end = TRIGGER_MS - 6.0
        timeline = _timeline([_head(80.0, line_end), _ball(66.0, 75.0, line_end)])

        default = processor.find_club_speed(timeline, 75.0, line_end + 10.0, club_type=club_type)
        explicit = processor.find_club_speed(
            timeline, 75.0, line_end + 10.0, club_type=club_type, trigger_offset_ms=None
        )

        assert explicit == default

    def test_process_capture_records_mode_and_line_end(self, processor):
        fixture = _fixture()
        trigger_ms = float(fixture["trigger_offset_ms"][0])
        capture = IQCapture(
            sample_time=0.0,
            trigger_time=trigger_ms / 1000.0,
            i_samples=fixture["i_samples"][0].astype(int).tolist(),
            q_samples=fixture["q_samples"][0].astype(int).tolist(),
        )

        anchored = processor.process_capture(capture, anchor_club_to_trigger=True)
        legacy = processor.process_capture(capture)

        assert anchored.club_speed_mode == "anchored"
        assert trigger_ms - 14.0 <= anchored.club_line_end_ms <= trigger_ms
        assert legacy.club_speed_mode == "legacy"
        assert legacy.club_line_end_ms is None
        assert legacy.club_speed_mph != anchored.club_speed_mph

    @pytest.mark.parametrize("label", ["range_20261009_shot8", "range_20261009_shot11"])
    def test_real_full_wedges_read_impact_club_speed(self, processor, label):
        fixture = _fixture()
        index = list(fixture["label"]).index(label)

        processed = _process(fixture, index, processor)

        assert processed.club_speed_mode == "anchored"
        assert 76.0 <= processed.club_speed_mph <= 83.0

    def test_trackman_club_speed_budget(self, processor):
        fixture = _fixture()
        errors = []
        for index, truth in enumerate(fixture["trackman_club_mph"]):
            if np.isnan(truth):
                continue
            processed = _process(fixture, index, processor)
            assert processed.club_speed_mph is not None, fixture["label"][index]
            errors.append(processed.club_speed_mph - float(truth))

        assert len(errors) == 22
        assert statistics.mean(abs(e) for e in errors) <= 1.5
        assert max(abs(e) for e in errors) <= 4.0
        assert abs(statistics.mean(errors)) <= 1.0
