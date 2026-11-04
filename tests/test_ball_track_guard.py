"""Ball-track guard: the club head's trailing track is not the ball.

2026-11-02 session 2 shot 3 (driver, Toptracer 150.1 mph) read 60.76 mph: the
ball (148 mph) was the 4th-strongest peak in two of its three windows
(MAX_PEAKS_PER_DIRECTION = 3), so a single 148.1 mph reading survived without a
partner and the post-impact vote took the club's follow-through (60.8 -> 51.0 mph
in 12.6 ms, smash 0.59). With the club anchored to the sound trigger the guard
follows the voted ball forward; a track that loses >= 8 mph within 40 ms at
<= -0.30 mph/ms (or a non-wedge smash below 0.90) fires it. It then reads the
post-impact windows again without the peak cap and takes their fastest track as
the ball if its smash is plausible, else keeps the number and flags it
"uncertain".

The fixture (tests/fixtures/ball_speed/ops243_ball_track_guard_captures.npz)
holds that shot, the two swings flagged without a ball (2026-10-19 shot 1,
2026-10-09 B shot 2) and seven good 20 ksps controls, with each capture's sample
rate and sound-edge delay.
"""

from __future__ import annotations

import json
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from openflight.clubs import ClubType
from openflight.clubs.bag import club_category
from openflight.launch_monitor import Shot, summarize_shots
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.types import (
    BallSpeedGuard,
    BallTrackShape,
    IQCapture,
    ProcessedCapture,
    SpeedReading,
    SpeedTimeline,
)
from openflight.session_logger import SessionLogger

FIXTURES = Path(__file__).parent / "fixtures"
GUARD_FIXTURE = FIXTURES / "ball_speed" / "ops243_ball_track_guard_captures.npz"
BALL_FIXTURE = FIXTURES / "ball_speed" / "ops243_ball_speed_captures.npz"
CLUB_FIXTURE = FIXTURES / "club_speed" / "ops243_club_speed_captures.npz"

S2_3 = "range_20261102s2_shot3"
FLAGGED = ("range_20261019_shot1", "range_20261009b_shot2")
CONTROLS = (
    "range_20261102s2_shot2",
    "range_20261102s2_shot4",
    "range_20261102s2_shot5",
    "range_20261102_shot1",
    "range_20261102_shot16",
    "range_20261030_shot1",
    "range_20261030_shot44",
)
# The one capture of the older fixtures the guard touches: T03a#1, a 54-degree
# wedge whose post-impact track is the club's follow-through (Toptracer 75.4 mph).
OLD_FIXTURE_FLAGGED = "range_20261003_shot1"


def _load(path: Path) -> dict:
    with np.load(path) as data:
        return {key: data[key] for key in data.files}


@pytest.fixture(scope="module")
def guard_fixture() -> dict:
    return _load(GUARD_FIXTURE)


def _guard_capture(fixture: dict, index: int) -> IQCapture:
    """The capture as the live monitor saw it: its own rate and sound-edge delay."""
    capture = IQCapture(
        sample_time=float(fixture["sample_time"][index]),
        trigger_time=float(fixture["trigger_time"][index]),
        i_samples=fixture["i_samples"][index].astype(int).tolist(),
        q_samples=fixture["q_samples"][index].astype(int).tolist(),
        sample_rate_hz=int(fixture["sample_rate_hz"][index]),
    )
    capture.sound_edge_delay_ms = float(fixture["sound_edge_delay_ms"][index])
    return capture


def _process_guard(fixture: dict, label: str, *, anchor: bool = True) -> ProcessedCapture:
    index = list(fixture["label"]).index(label)
    capture = _guard_capture(fixture, index)
    return RollingBufferProcessor(sample_rate=capture.sample_rate_hz).process_capture(
        capture,
        club_type=club_category(str(fixture["club"][index])),
        anchor_club_to_trigger=anchor,
    )


def _old_capture(fixture: dict, index: int) -> IQCapture:
    """As tests/test_ball_speed_post_impact.py builds the older fixtures (30 ksps)."""
    if "sample_time" in fixture:
        sample_time = float(fixture["sample_time"][index])
        trigger_time = float(fixture["trigger_time"][index])
    else:
        sample_time = 0.0
        trigger_time = float(fixture["trigger_offset_ms"][index]) / 1000.0
    return IQCapture(
        sample_time=sample_time,
        trigger_time=trigger_time,
        i_samples=fixture["i_samples"][index].astype(int).tolist(),
        q_samples=fixture["q_samples"][index].astype(int).tolist(),
    )


def _no_guard(self, _capture, _post_impact, ball_speed_mph, *_args):
    """Stand-in for _guard_ball_speed that never fires: the pipeline before the guard."""
    return BallSpeedGuard(
        ball_speed_mph=ball_speed_mph,
        voted_ball_speed_mph=ball_speed_mph,
        track=BallTrackShape(links=0),
    )


def _outputs(processed: ProcessedCapture) -> str:
    """Everything a ProcessedCapture reports except the guard's own decision.

    The capture objects are left out: IQCapture.timestamp is the wall clock of
    its construction.
    """
    values = {
        field.name: getattr(processed, field.name)
        for field in fields(processed)
        if field.name not in ("ball_guard", "capture", "timeline")
    }
    values["readings"] = processed.timeline.readings
    values["timeline_rate_hz"] = processed.timeline.sample_rate_hz
    return repr(values)


# --- real captures -----------------------------------------------------------


def test_fixture_holds_the_guard_cases(guard_fixture):
    assert set(guard_fixture["label"]) == {S2_3, *FLAGGED, *CONTROLS}
    # 8 of 10 captures were taken at 20 ksps.
    assert sorted(guard_fixture["sample_rate_hz"].tolist()).count(20000) == 8


def test_s2_3_club_trailing_track_is_replaced_by_the_ball(guard_fixture):
    processed = _process_guard(guard_fixture, S2_3)

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(148.33, abs=0.01)
    assert processed.ball_speed_status is None
    assert processed.ball_speed_status_reason == "club_like_track"
    assert processed.ball_speed_replaced_from_mph == pytest.approx(60.76, abs=0.01)
    assert processed.club_speed_mph == pytest.approx(103.39, abs=0.01)
    assert processed.smash_factor == pytest.approx(1.435, abs=0.001)
    processor = RollingBufferProcessor(sample_rate=20000)
    assert processed.ball_timestamp_ms == processor._ball_timestamp_ms(
        processed.timeline, processed.ball_speed_mph
    )
    guard = processed.ball_guard
    assert guard.fired
    assert guard.voted_ball_speed_mph == pytest.approx(60.76, abs=0.01)
    assert guard.track.links == 4
    assert guard.track.drop_mph == pytest.approx(9.71, abs=0.01)
    assert guard.track.slope_mph_per_ms == pytest.approx(-0.782, abs=0.001)
    assert guard.smash == pytest.approx(0.588, abs=0.001)
    assert guard.replacement_mph == pytest.approx(148.33, abs=0.01)
    assert guard.replacement_rejection is None


def test_s2_3_spin_prior_and_impact_read_the_replaced_ball(guard_fixture):
    seen = []

    def prior(ball_speed_mph):
        seen.append(ball_speed_mph)
        return 2700.0

    index = list(guard_fixture["label"]).index(S2_3)
    capture = _guard_capture(guard_fixture, index)
    processed = RollingBufferProcessor(sample_rate=20000).process_capture(
        capture,
        expected_spin_for_ball_speed=prior,
        club_type=ClubType.DRIVER,
        anchor_club_to_trigger=True,
    )

    assert seen == [processed.ball_speed_mph]
    assert processed.ball_speed_mph == pytest.approx(148.33, abs=0.01)


@pytest.mark.parametrize("label", FLAGGED)
def test_swing_without_a_ball_keeps_its_number_flagged_uncertain(guard_fixture, label):
    index = list(guard_fixture["label"]).index(label)

    processed = _process_guard(guard_fixture, label)

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == float(guard_fixture["head_ball_mph"][index])
    assert processed.ball_speed_status == "uncertain"
    assert processed.ball_speed_status_reason == "club_like_track"
    assert processed.ball_speed_replaced_from_mph is None


def test_flag_without_club_speed_never_searches_for_a_replacement(guard_fixture, monkeypatch):
    searched = []
    original = RollingBufferProcessor._uncapped_post_impact_readings
    monkeypatch.setattr(
        RollingBufferProcessor,
        "_uncapped_post_impact_readings",
        lambda self, *args: searched.append(args) or original(self, *args),
    )

    processed = _process_guard(guard_fixture, "range_20261009b_shot2")

    assert processed.club_speed_mph is None
    assert processed.ball_guard.replacement_rejection == "no_club_speed"
    assert searched == []


def test_flag_with_club_speed_finds_no_faster_track(guard_fixture):
    processed = _process_guard(guard_fixture, "range_20261019_shot1")

    assert processed.club_speed_mph == pytest.approx(54.58, abs=0.01)
    assert processed.ball_guard.replacement_rejection == "no_track"
    assert processed.ball_guard.replacement_mph is None


@pytest.mark.parametrize("label", CONTROLS)
def test_good_shots_keep_their_ball_speed_exactly(guard_fixture, label):
    index = list(guard_fixture["label"]).index(label)

    processed = _process_guard(guard_fixture, label)

    assert processed.ball_speed_mph == float(guard_fixture["head_ball_mph"][index])
    assert processed.ball_speed_status is None
    assert processed.ball_speed_status_reason is None
    assert processed.ball_speed_replaced_from_mph is None
    assert not processed.ball_guard.fired


def test_every_capture_matches_the_fixture_expectation(guard_fixture):
    for index, label in enumerate(guard_fixture["label"]):
        processed = _process_guard(guard_fixture, str(label))
        assert processed.ball_speed_mph == pytest.approx(
            float(guard_fixture["expected_ball_mph"][index]), abs=0.01
        ), label
        assert (processed.ball_speed_status or "") == guard_fixture["expected_status"][index]
        assert (processed.ball_speed_status_reason or "") == guard_fixture["expected_reason"][index]


@pytest.mark.parametrize("label", CONTROLS + FLAGGED)
def test_unfired_and_flagged_captures_match_the_pipeline_without_guard(
    guard_fixture, monkeypatch, label
):
    guarded = _process_guard(guard_fixture, label)
    monkeypatch.setattr(RollingBufferProcessor, "_guard_ball_speed", _no_guard)

    unguarded = _process_guard(guard_fixture, label)

    assert _outputs(guarded) == _outputs(unguarded)


@pytest.mark.parametrize("path", [BALL_FIXTURE, CLUB_FIXTURE], ids=["ball", "club"])
def test_older_fixtures_keep_every_ball_and_flag_only_t03a_1(path, monkeypatch):
    fixture = _load(path)
    processor = RollingBufferProcessor()

    def run(index):
        return processor.process_capture(
            _old_capture(fixture, index),
            club_type=club_category(str(fixture["club"][index])),
            anchor_club_to_trigger=True,
        )

    guarded = {str(label): run(index) for index, label in enumerate(fixture["label"])}
    monkeypatch.setattr(RollingBufferProcessor, "_guard_ball_speed", _no_guard)
    unguarded = {str(label): run(index) for index, label in enumerate(fixture["label"])}

    flagged = set()
    for label, processed in guarded.items():
        if processed.rejection_reason is not None:
            assert processed.ball_guard is None, label
            continue
        assert _outputs(processed) == _outputs(unguarded[label]), label
        assert processed.ball_speed_replaced_from_mph is None, label
        if processed.ball_speed_status is not None:
            flagged.add(label)
    if path == BALL_FIXTURE:
        assert flagged == {OLD_FIXTURE_FLAGGED}
        t03a_1 = guarded[OLD_FIXTURE_FLAGGED]
        assert t03a_1.ball_speed_mph == pytest.approx(54.37, abs=0.01)
        assert (t03a_1.ball_speed_status, t03a_1.ball_speed_status_reason) == (
            "uncertain",
            "club_like_track",
        )
    else:
        assert flagged == set()


def test_without_trigger_anchor_the_guard_does_not_run(guard_fixture, monkeypatch):
    calls = []
    monkeypatch.setattr(
        RollingBufferProcessor,
        "_guard_ball_speed",
        lambda *args: calls.append(args) or _no_guard(*args),
    )

    for label in guard_fixture["label"]:
        processed = _process_guard(guard_fixture, str(label), anchor=False)
        assert processed.ball_guard is None
        assert processed.ball_speed_status is None
        assert processed.ball_speed_status_reason is None
        assert processed.ball_speed_replaced_from_mph is None
    assert calls == []


def test_without_trigger_anchor_s2_3_keeps_the_all_window_vote(guard_fixture):
    processed = _process_guard(guard_fixture, S2_3, anchor=False)

    # Unchanged by the guard: the all-window vote also took the club's track.
    assert processed.ball_speed_mph < 100.0


def test_rejected_capture_carries_no_guard_decision():
    fixture = _load(BALL_FIXTURE)
    index = list(fixture["label"]).index("range_20261005_shot46")

    processed = RollingBufferProcessor().process_capture(
        _old_capture(fixture, index), anchor_club_to_trigger=True
    )

    assert processed.rejection_reason == "no_ball_track"
    assert processed.ball_guard is None


def test_guard_cost_per_capture_stays_small(guard_fixture):
    """The uncapped re-read runs only when the guard fires; it stays a few ms on a Mac."""
    index = list(guard_fixture["label"]).index(S2_3)
    capture = _guard_capture(guard_fixture, index)
    processor = RollingBufferProcessor(sample_rate=20000)
    processed = processor.process_capture(
        capture, club_type=ClubType.DRIVER, anchor_club_to_trigger=True
    )
    vote_start_ms = processed.club_line_end_ms - processor.BALL_VOTE_IMPACT_MARGIN_MS

    started = time.perf_counter()
    readings = processor._uncapped_post_impact_readings(capture, vote_start_ms)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert readings
    # Generous: a CI runner, not the Pi. Measured 1-2.5 ms on an M-series Mac.
    assert elapsed_ms < 250.0


# --- uncapped re-read --------------------------------------------------------


def test_uncapped_readings_hold_every_capped_post_impact_reading(guard_fixture):
    index = list(guard_fixture["label"]).index(S2_3)
    capture = _guard_capture(guard_fixture, index)
    processor = RollingBufferProcessor(sample_rate=20000)
    processed = processor.process_capture(
        capture, club_type=ClubType.DRIVER, anchor_club_to_trigger=True
    )
    vote_start_ms = processed.club_line_end_ms - processor.BALL_VOTE_IMPACT_MARGIN_MS
    standard = processor.process_standard(capture)
    capped = [
        reading
        for reading in standard.readings
        if reading.is_outbound and processor._reading_center_ms(reading) >= vote_start_ms
    ]

    uncapped = processor._uncapped_post_impact_readings(capture, vote_start_ms)

    def key(reading):
        return (reading.timestamp_ms, reading.speed_mph, reading.magnitude)

    assert {key(r) for r in capped} <= {key(r) for r in uncapped}
    assert len(uncapped) > len(capped)
    assert all(reading.is_outbound for reading in uncapped)
    assert all(processor._reading_center_ms(r) >= vote_start_ms for r in uncapped)
    per_window = {}
    for reading in uncapped:
        per_window[reading.timestamp_ms] = per_window.get(reading.timestamp_ms, 0) + 1
    assert max(per_window.values()) <= processor.BALL_GUARD_UNCAPPED_PEAKS
    # The ball of windows 8 and 10, the 4th-strongest peak there.
    assert any(abs(r.speed_mph - 148.33) < 0.01 for r in uncapped)
    assert not any(abs(r.speed_mph - 148.33) < 0.01 for r in capped)


def test_uncapped_readings_after_the_last_window_are_empty(guard_fixture):
    capture = _guard_capture(guard_fixture, 0)
    processor = RollingBufferProcessor(sample_rate=20000)

    assert processor._uncapped_post_impact_readings(capture, 1.0e6) == []


def test_batched_block_magnitude_matches_one_window_at_a_time(guard_fixture):
    capture = _guard_capture(guard_fixture, 0)
    processor = RollingBufferProcessor(sample_rate=20000)
    i_data = np.asarray(capture.i_samples)
    q_data = np.asarray(capture.q_samples)
    size = processor.WINDOW_SIZE
    starts = list(range(0, len(i_data) - size + 1, processor.STEP_SIZE_STANDARD))

    windows = np.asarray(starts)[:, np.newaxis] + np.arange(size)
    batched = processor._block_magnitude(i_data[windows], q_data[windows])

    for row, start in zip(batched, starts):
        single = processor._block_magnitude(
            i_data[start : start + size], q_data[start : start + size]
        )
        assert np.array_equal(row, single)


def test_block_magnitude_of_one_window_is_the_original_formula(guard_fixture):
    """_process_block's FFT moved into _block_magnitude without changing a bit."""
    capture = _guard_capture(guard_fixture, 0)
    processor = RollingBufferProcessor(sample_rate=20000)
    size = processor.WINDOW_SIZE
    scale = processor.VOLTAGE_REF / processor.ADC_RANGE
    for start in range(0, 4096 - size + 1, processor.STEP_SIZE_OVERLAP):
        i_block = np.asarray(capture.i_samples[start : start + size])
        q_block = np.asarray(capture.q_samples[start : start + size])
        i_windowed = (i_block - np.mean(i_block)) * scale * processor.hanning_window
        q_windowed = (q_block - np.mean(q_block)) * scale * processor.hanning_window
        original = np.abs(np.fft.fft(i_windowed + 1j * q_windowed, processor.FFT_SIZE))

        assert np.array_equal(processor._block_magnitude(i_block, q_block), original)


def test_find_peaks_cap_defaults_to_max_peaks_per_direction(guard_fixture):
    capture = _guard_capture(guard_fixture, 0)
    processor = RollingBufferProcessor(sample_rate=20000)
    start = 8 * processor.STEP_SIZE_STANDARD
    size = processor.WINDOW_SIZE
    magnitude = processor._block_magnitude(
        np.asarray(capture.i_samples[start : start + size]),
        np.asarray(capture.q_samples[start : start + size]),
    )
    band = (magnitude, processor.DC_MASK_BINS, processor._outbound_end_bin)

    default = processor._find_peaks(*band)
    uncapped = processor._find_peaks(*band, max_peaks=12)

    assert len(default) == processor.MAX_PEAKS_PER_DIRECTION
    assert processor._find_peaks(*band, max_peaks=3) == default
    # The greedy pick takes peaks strongest first: a larger cap only appends.
    assert uncapped[: len(default)] == default
    assert len(uncapped) > len(default)


# --- synthetic tracks --------------------------------------------------------

PROCESSOR = RollingBufferProcessor()
STEP_MS = PROCESSOR.STEP_SIZE_STANDARD / PROCESSOR.SAMPLE_RATE * 1000.0


def _readings(*track: tuple[int, float]) -> list[SpeedReading]:
    """Outbound standard-window readings: (window index, speed mph)."""
    return [
        SpeedReading(speed_mph=speed, magnitude=10.0, timestamp_ms=window * STEP_MS)
        for window, speed in track
    ]


def _line(start_mph: float, mph_per_ms: float, windows: range) -> list[tuple[int, float]]:
    return [(window, start_mph + mph_per_ms * window * STEP_MS) for window in windows]


FOLLOW_THROUGH = _line(60.8, -0.8, range(0, 5))  # 60.8 -> 47.1 mph in 17 ms


def test_flat_ball_track_is_not_club_like():
    readings = _readings(
        *[(w, 150.0 - 0.05 * w) for w in range(10)],
        *[(w, 100.0 - w) for w in range(10)],  # the club, one per window
    )

    shape = PROCESSOR._ball_track_shape(readings, 150.0)

    assert shape.links == 10
    assert shape.drop_mph == pytest.approx(0.45)
    assert shape.slope_mph_per_ms == pytest.approx(-0.05 / STEP_MS)
    assert not PROCESSOR._is_club_like_track(shape)


def test_follow_through_track_is_club_like():
    shape = PROCESSOR._ball_track_shape(_readings(*FOLLOW_THROUGH), 60.8)

    assert shape.links == 5
    assert shape.drop_mph == pytest.approx(0.8 * 4 * STEP_MS)
    assert shape.slope_mph_per_ms == pytest.approx(-0.8)
    assert PROCESSOR._is_club_like_track(shape)


def test_drop_without_a_steep_slope_is_not_club_like():
    # Dips 8 mph (multipath fade), then holds again: no follow-through.
    speeds = [100.0, 97.0, 94.0, 92.0, 96.0] + [100.0] * 7
    shape = PROCESSOR._ball_track_shape(_readings(*enumerate(speeds)), 100.0)

    assert shape.links == 12
    assert shape.drop_mph == pytest.approx(8.0)
    assert shape.slope_mph_per_ms > PROCESSOR.BALL_GUARD_SLOPE_MPH_PER_MS
    assert not PROCESSOR._is_club_like_track(shape)


def test_steep_track_of_two_links_is_not_club_like():
    shape = PROCESSOR._ball_track_shape(_readings((0, 100.0), (1, 92.0)), 100.0)

    assert shape.links == 2
    assert shape.drop_mph == pytest.approx(8.0)
    assert shape.slope_mph_per_ms == pytest.approx(-8.0 / STEP_MS)
    assert not PROCESSOR._is_club_like_track(shape)


def test_drop_counts_only_links_within_40_ms():
    # 9 windows = 38.4 ms at 30 ksps; the slow tail after 40 ms does not count.
    speeds = [100.0] * 10 + [94.0, 89.0, 84.0]
    shape = PROCESSOR._ball_track_shape(_readings(*enumerate(speeds)), 100.0)

    assert shape.links == 13
    assert shape.drop_mph == pytest.approx(0.0)
    assert not PROCESSOR._is_club_like_track(shape)


def test_track_shape_without_readings():
    assert PROCESSOR._ball_track_shape([], 100.0) == BallTrackShape(links=0)
    single = PROCESSOR._ball_track_shape(_readings((3, 100.0)), 100.0)
    assert single == BallTrackShape(links=1, drop_mph=None, slope_mph_per_ms=None)


def test_track_starts_at_the_reading_nearest_the_voted_ball():
    readings = _readings((0, 140.0), (1, 60.8), (2, 57.0), (3, 53.0))

    track = PROCESSOR._forward_ball_track(readings, 61.0)

    assert [round(r.speed_mph, 1) for r in track] == [60.8, 57.0, 53.0]


def test_one_window_without_a_link_continues_two_end_the_track():
    one_miss = PROCESSOR._forward_ball_track(_readings((0, 100.0), (2, 99.5), (3, 99.0)), 100.0)
    two_misses = PROCESSOR._forward_ball_track(_readings((0, 100.0), (3, 99.5), (4, 99.0)), 100.0)
    # A window whose readings are all out of tolerance is a miss as well.
    off_track = PROCESSOR._forward_ball_track(
        _readings((0, 100.0), (1, 70.0), (2, 71.0), (3, 99.0)), 100.0
    )

    assert [r.speed_mph for r in one_miss] == [100.0, 99.5, 99.0]
    assert [r.speed_mph for r in two_misses] == [100.0]
    assert [r.speed_mph for r in off_track] == [100.0]


def test_track_links_the_reading_nearest_the_previous_link():
    readings = _readings((0, 100.0), (1, 104.0), (1, 99.0), (2, 98.5))

    track = PROCESSOR._forward_ball_track(readings, 100.0)

    assert [r.speed_mph for r in track] == [100.0, 99.0, 98.5]


def test_link_tolerance_is_6_mph_up_to_75_mph_and_8_percent_above():
    assert len(PROCESSOR._forward_ball_track(_readings((0, 60.0), (1, 65.9)), 60.0)) == 2
    assert len(PROCESSOR._forward_ball_track(_readings((0, 60.0), (1, 66.1)), 60.0)) == 1
    # 8 % of 100 mph is 8 mph.
    assert len(PROCESSOR._forward_ball_track(_readings((0, 100.0), (1, 107.9)), 100.0)) == 2
    assert len(PROCESSOR._forward_ball_track(_readings((0, 100.0), (1, 108.1)), 100.0)) == 1


# --- guard decisions (synthetic) ------------------------------------------------


def _guard(
    post_impact,
    ball_mph,
    club_mph,
    club_type=ClubType.DRIVER,
    uncapped=None,
    monkeypatch=None,
):
    processor = RollingBufferProcessor()
    if uncapped is not None:
        monkeypatch.setattr(
            processor, "_uncapped_post_impact_readings", lambda _capture, _start: uncapped
        )
    else:
        processor._uncapped_post_impact_readings = MagicMock(
            side_effect=AssertionError("searched for a replacement")
        )
    return processor._guard_ball_speed(None, post_impact, ball_mph, club_mph, club_type, 0.0)


def test_smash_floor_fires_for_a_driver_at_0_59(monkeypatch):
    flat = _readings(*[(w, 60.0) for w in range(6)])

    decision = _guard(flat, 60.0, 102.0, uncapped=[], monkeypatch=monkeypatch)

    assert decision.rule == "smash_implausible"
    assert decision.status == "uncertain"
    assert decision.ball_speed_mph == 60.0
    assert decision.replacement_rejection == "no_track"
    assert decision.smash == pytest.approx(60.0 / 102.0)


@pytest.mark.parametrize("club_type", [ClubType.SW, ClubType.PW, ClubType.GW, ClubType.LW])
def test_no_smash_floor_for_wedges(club_type):
    flat = _readings(*[(w, 60.0) for w in range(6)])

    decision = _guard(flat, 60.0, 77.0, club_type)  # smash 0.78

    assert not decision.fired
    assert decision.status is None
    assert decision.smash == pytest.approx(60.0 / 77.0)


def test_no_smash_floor_without_a_club_type_or_club_speed():
    flat = _readings(*[(w, 60.0) for w in range(6)])

    assert not _guard(flat, 60.0, 102.0, ClubType.UNKNOWN).fired
    assert not _guard(flat, 60.0, None).fired
    assert _guard(flat, 60.0, None).smash is None


def test_smash_floor_is_strict():
    flat = _readings(*[(w, 90.0) for w in range(6)])

    assert not _guard(flat, 90.0, 100.0).fired  # exactly 0.90


def test_good_shot_is_kept_without_searching():
    flat = _readings(*[(w, 140.0 - 0.1 * w) for w in range(8)])

    decision = _guard(flat, 140.0, 103.0)

    assert not decision.fired
    assert (decision.ball_speed_mph, decision.voted_ball_speed_mph) == (140.0, 140.0)
    assert decision.status is None
    assert decision.replaced_from_mph is None
    assert decision.replacement_mph is None
    assert decision.replacement_rejection is None
    assert decision.smash == pytest.approx(140.0 / 103.0)
    assert decision.track.links == 8


def test_club_like_track_without_club_speed_is_flagged():
    decision = _guard(_readings(*FOLLOW_THROUGH), 60.8, None)

    assert decision.rule == "club_like_track"
    assert decision.status == "uncertain"
    assert decision.replacement_rejection == "no_club_speed"
    assert decision.ball_speed_mph == 60.8


def test_replacement_is_the_fastest_uncapped_track(monkeypatch):
    uncapped = _readings(*FOLLOW_THROUGH, (1, 148.0), (2, 148.3), (3, 30.0))

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 103.4, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.ball_speed_mph == pytest.approx(148.3)
    assert decision.status is None
    assert decision.rule == "club_like_track"
    assert decision.replaced_from_mph == pytest.approx(60.8)
    assert decision.replacement_smash == pytest.approx(148.3 / 103.4)
    assert decision.replacement_rejection is None


def test_replacement_needs_two_readings(monkeypatch):
    uncapped = _readings(*FOLLOW_THROUGH, (1, 148.0))

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 103.4, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.status == "uncertain"
    assert decision.replacement_rejection == "no_track"
    assert decision.ball_speed_mph == 60.8


def test_replacement_must_be_10_percent_faster_than_the_voted_ball(monkeypatch):
    # A repeated 66 mph return (1.086 x the voted ball) is not searched.
    uncapped = _readings(*FOLLOW_THROUGH, (1, 66.0), (2, 66.2))

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 103.4, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.replacement_rejection == "no_track"


@pytest.mark.parametrize("replacement_mph", [157.0, 161.0])  # smash 1.518 and 1.557
def test_replacement_above_smash_1_50_is_rejected(monkeypatch, replacement_mph):
    uncapped = _readings(*FOLLOW_THROUGH, (1, replacement_mph - 0.2), (2, replacement_mph))

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 103.4, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.status == "uncertain"
    assert decision.replacement_rejection == "smash_out_of_range"
    assert decision.replacement_mph == pytest.approx(replacement_mph)
    assert decision.ball_speed_mph == 60.8


def test_replacement_smash_bounds_are_inclusive(monkeypatch):
    uncapped = _readings(*FOLLOW_THROUGH, (1, 149.9), (2, 150.0))

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 100.0, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.ball_speed_mph == 150.0  # smash exactly 1.50


def test_club_like_replacement_is_rejected(monkeypatch):
    # A second follow-through: 150 -> 132 mph in 12.8 ms.
    second = [(1, 150.0), (2, 150.2), (3, 141.0), (4, 132.0)]
    uncapped = _readings(*FOLLOW_THROUGH, *second)

    decision = _guard(
        _readings(*FOLLOW_THROUGH), 60.8, 103.4, uncapped=uncapped, monkeypatch=monkeypatch
    )

    assert decision.replacement_mph == pytest.approx(150.2)
    assert decision.replacement_rejection == "club_like_track"
    assert decision.status == "uncertain"


def test_wedge_replacement_smash_range(monkeypatch):
    wedge_track = _readings(*_line(55.0, -0.8, range(0, 5)))
    accepted = _readings(*_line(55.0, -0.8, range(0, 5)), (1, 109.8), (2, 110.0))
    too_fast = _readings(*_line(55.0, -0.8, range(0, 5)), (1, 117.8), (2, 118.0))

    sw_ok = _guard(wedge_track, 55.0, 80.0, ClubType.SW, uncapped=accepted, monkeypatch=monkeypatch)
    sw_fast = _guard(
        wedge_track, 55.0, 80.0, ClubType.SW, uncapped=too_fast, monkeypatch=monkeypatch
    )
    iron_fast = _guard(
        wedge_track, 55.0, 80.0, ClubType.IRON_9, uncapped=too_fast, monkeypatch=monkeypatch
    )

    assert sw_ok.ball_speed_mph == 110.0  # smash 1.375
    assert sw_fast.replacement_rejection == "smash_out_of_range"  # 1.475 > 1.45
    assert iron_fast.ball_speed_mph == 118.0  # 1.475 <= 1.50 for a non-wedge


def test_guard_to_dict_is_the_capture_log_record():
    decision = BallSpeedGuard(
        ball_speed_mph=148.3258,
        voted_ball_speed_mph=60.7559,
        track=BallTrackShape(links=4, drop_mph=9.7123, slope_mph_per_ms=-0.78234),
        smash=0.58766,
        rule="club_like_track",
        replaced_from_mph=60.7559,
        replacement_mph=148.3258,
        replacement_smash=1.43466,
    )

    assert decision.to_dict() == {
        "fired": True,
        "rule": "club_like_track",
        "status": None,
        "voted_ball_speed_mph": 60.76,
        "ball_speed_mph": 148.33,
        "track_links": 4,
        "track_drop_mph": 9.71,
        "track_slope_mph_per_ms": -0.782,
        "smash": 0.588,
        "replaced_from_mph": 60.76,
        "replacement_mph": 148.33,
        "replacement_smash": 1.435,
        "replacement_rejection": None,
    }
    assert BallSpeedGuard(1.0, 1.0, BallTrackShape(0)).to_dict()["fired"] is False


# --- shot, monitor and session log ---------------------------------------------


def _processed_with_guard(guard: BallSpeedGuard) -> ProcessedCapture:
    capture = IQCapture(
        sample_time=100.0,
        trigger_time=100.068,
        i_samples=[2048] * 16,
        q_samples=[2048] * 16,
        trigger_timestamp=1000.0,
    )
    return ProcessedCapture(
        timeline=SpeedTimeline(readings=[], sample_rate_hz=937.5, capture=capture),
        ball_speed_mph=guard.ball_speed_mph,
        ball_timestamp_ms=68.0,
        club_speed_mph=54.58,
        capture=capture,
        ball_guard=guard,
    )


UNCERTAIN = BallSpeedGuard(
    ball_speed_mph=55.19,
    voted_ball_speed_mph=55.19,
    track=BallTrackShape(links=11, drop_mph=15.07, slope_mph_per_ms=-0.462),
    smash=1.011,
    rule="club_like_track",
    status="uncertain",
    replacement_rejection="no_track",
)
REPLACED = BallSpeedGuard(
    ball_speed_mph=148.33,
    voted_ball_speed_mph=60.76,
    track=BallTrackShape(links=4, drop_mph=9.71, slope_mph_per_ms=-0.782),
    smash=0.588,
    rule="club_like_track",
    replaced_from_mph=60.76,
    replacement_mph=148.33,
    replacement_smash=1.435,
)


def test_processed_capture_without_guard_has_no_status():
    processed = ProcessedCapture(
        timeline=SpeedTimeline(readings=[], sample_rate_hz=937.5),
        ball_speed_mph=100.0,
        ball_timestamp_ms=68.0,
    )

    assert processed.ball_guard is None
    assert processed.ball_speed_status is None
    assert processed.ball_speed_status_reason is None
    assert processed.ball_speed_replaced_from_mph is None


@pytest.mark.parametrize(
    ("guard", "status", "reason"),
    [(UNCERTAIN, "uncertain", "club_like_track"), (REPLACED, None, "club_like_track")],
)
def test_create_shot_carries_the_status(guard, status, reason):
    from openflight.rolling_buffer import RollingBufferMonitor

    monitor = RollingBufferMonitor(port=None, trigger_type="sound")

    shot = monitor._create_shot(_processed_with_guard(guard))

    assert shot.ball_speed_mph == guard.ball_speed_mph
    assert (shot.ball_speed_status, shot.ball_speed_status_reason) == (status, reason)
    data = shot.to_dict()
    assert (data["ball_speed_status"], data["ball_speed_status_reason"]) == (status, reason)


def test_shot_without_guard_serializes_null_status():
    data = Shot(ball_speed_mph=140.0, timestamp=datetime.now()).to_dict()

    assert data["ball_speed_status"] is None
    assert data["ball_speed_status_reason"] is None


def test_capture_loop_logs_the_guard_decision(monkeypatch):
    from openflight.rolling_buffer import RollingBufferMonitor, monitor as monitor_module

    raw_captures = []
    session_log = MagicMock()
    session_log.log_rolling_buffer_capture.side_effect = lambda **row: raw_captures.append(row)
    monkeypatch.setattr(monitor_module, "get_session_logger", lambda: session_log)
    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    events = []
    monitor._diagnostic_callback = events.append
    shots = []
    monitor._shot_callback = shots.append
    processed = _processed_with_guard(UNCERTAIN)

    class OneCaptureTrigger:
        calls = 0

        def wait_for_trigger(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return processed.capture
            monitor._running = False
            return None

        @staticmethod
        def drain_diagnostics():
            return []

        @staticmethod
        def reset():
            return None

    monitor.trigger = OneCaptureTrigger()
    monitor.processor = MagicMock(process_capture=MagicMock(return_value=processed))
    monitor._running = True
    monitor._capture_loop()

    assert [shot.ball_speed_status for shot in shots] == ["uncertain"]
    (row,) = raw_captures
    assert row["ball_speed_status"] == "uncertain"
    assert row["ball_speed_status_reason"] == "club_like_track"
    assert row["ball_speed_replaced_from_mph"] is None
    assert row["ball_guard"] == UNCERTAIN.to_dict()
    accepted = [event for event in events if event["accepted"]]
    assert [(e["ball_speed_status"], e["ball_speed_status_reason"]) for e in accepted] == [
        ("uncertain", "club_like_track")
    ]


def test_session_log_writes_the_guard_fields(tmp_path):
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="rolling-buffer", trigger_type="sound")

    logger.log_rolling_buffer_capture(
        shot_number=3,
        sample_time=100.0,
        trigger_time=100.068,
        i_samples=[2048] * 4,
        q_samples=[2048] * 4,
        ball_speed_mph=148.33,
        ball_speed_status=None,
        ball_speed_status_reason="club_like_track",
        ball_speed_replaced_from_mph=60.76,
        ball_guard=REPLACED.to_dict(),
    )
    shot = Shot(
        ball_speed_mph=55.19,
        timestamp=datetime.now(),
        ball_speed_status="uncertain",
        ball_speed_status_reason="club_like_track",
    )
    logger.log_shot(shot)

    entries = [json.loads(line) for line in logger.session_path.read_text().splitlines()]
    capture = next(e for e in entries if e["type"] == "rolling_buffer_capture")
    assert capture["ball_speed_status"] is None
    assert capture["ball_speed_status_reason"] == "club_like_track"
    assert capture["ball_speed_replaced_from_mph"] == 60.76
    assert capture["ball_guard"]["replacement_mph"] == 148.33
    assert capture["ball_guard"]["track_slope_mph_per_ms"] == -0.782
    detected = next(e for e in entries if e["type"] == "shot_detected")
    assert detected["ball_speed_status"] == "uncertain"
    assert detected["ball_speed_status_reason"] == "club_like_track"


def test_session_log_capture_without_guard_logs_nulls(tmp_path):
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="rolling-buffer", trigger_type="sound")
    logger.log_rolling_buffer_capture(
        shot_number=1,
        sample_time=100.0,
        trigger_time=100.068,
        i_samples=[2048] * 4,
        q_samples=[2048] * 4,
    )

    entry = json.loads(logger.session_path.read_text().splitlines()[-1])
    assert entry["ball_guard"] is None
    assert entry["ball_speed_status"] is None
    assert entry["ball_speed_replaced_from_mph"] is None


# --- session summary -------------------------------------------------------------


def _shot(ball, club=None, status=None, spin=None):
    return Shot(
        ball_speed_mph=ball,
        timestamp=datetime.now(),
        club_speed_mph=club,
        ball_speed_status=status,
        spin_rpm=spin,
        club=ClubType.DRIVER,
    )


def test_session_averages_leave_out_uncertain_shots():
    good = [_shot(140.0, 100.0, spin=2500.0), _shot(144.0, 102.0)]
    uncertain = _shot(43.5, 103.0, status="uncertain", spin=9000.0)

    summary = summarize_shots([good[0], uncertain, good[1]], mode="rolling-buffer")
    reference = summarize_shots(good, mode="rolling-buffer")

    assert summary["shot_count"] == 3
    assert {k: v for k, v in summary.items() if k != "shot_count"} == {
        k: v for k, v in reference.items() if k != "shot_count"
    }
    assert summary["avg_ball_speed"] == pytest.approx(142.0)
    assert summary["min_ball_speed"] == 140.0
    assert summary["spin_detection_rate"] == pytest.approx(0.5)


def test_session_of_only_uncertain_shots_has_empty_averages():
    summary = summarize_shots([_shot(43.5, status="uncertain")], mode="rolling-buffer")

    assert summary["shot_count"] == 1
    assert summary["avg_ball_speed"] == 0
    assert summary["avg_club_speed"] is None
    assert summary["spin_detection_rate"] == 0


def test_replaced_ball_speed_counts_in_the_averages():
    replaced = _shot(148.3, 103.4)
    replaced.ball_speed_status_reason = "club_like_track"

    summary = summarize_shots([replaced], mode="rolling-buffer")

    assert summary["avg_ball_speed"] == pytest.approx(148.3)
