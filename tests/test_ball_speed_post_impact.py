"""Sound-trigger ball speed is voted only over windows after impact.

At a 54-degree wedge's ~37 deg launch the OPS243 sees only 74-94 % of the ball
speed, so the club head approaching impact (or a slower pre-impact return) can
be the fastest repeated 1-mph bin of the whole capture. With the club anchored
to the sound trigger, votes and the reported +-2 mph maximum come only from
windows centred at or after the club-line end minus 1 ms (trigger - 7.4 ms
without a club line). A capture without any repeated post-impact speed holds no
ball and is rejected as "no_ball_track" instead of becoming a 20-60 mph shot.

The real-capture fixture (tests/fixtures/ball_speed/) holds raw I/Q from the
Toptracer range sessions of 2026-10-03/05/06 and the range session of
2026-10-09: the 8 wedge captures whose all-window vote took a pre-impact signal,
the 6 whose reported maximum came from a club frame, the 5 captures without a
ball, 15 normal captures across the bag, and May-6 TrackMan shots 9 and 13. It
also carries the TrackMan ball speed of all 24 May-6 shots (validation only);
the 22 good May-6 captures themselves live in the club-speed fixture.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from openflight.clubs.bag import club_category
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.types import IQCapture, SpeedTimeline

FIXTURES = Path(__file__).parent / "fixtures"
BALL_FIXTURE = FIXTURES / "ball_speed" / "ops243_ball_speed_captures.npz"
CLUB_FIXTURE = FIXTURES / "club_speed" / "ops243_club_speed_captures.npz"

# label -> (all-window vote = before / anchor_club_to_trigger=False, post-impact vote), mph
PRE_IMPACT_PICKS = {
    "range_20261003_shot4": (81.15, 72.91),
    "range_20261003_shot6": (81.77, 74.23),
    "range_20261005_shot2": (76.88, 70.26),
    "range_20261005_shot10": (81.05, 77.29),
    "range_20261006_shot2": (69.24, 63.13),
    "range_20261006_shot5": (71.07, 66.29),
    "range_20261006_shot7": (80.44, 67.51),
    # All-window 68.83 before the track vote: its 68 mph pre-impact pair is three
    # windows apart (BALL_TRACK_MAX_WINDOW_GAP).
    "range_20261006_shot8": (65.37, 64.25),
}
CLUB_FRAME_MAXIMA = {
    "range_20261005_shot1": (72.91, 72.30),
    "range_20261005_shot4": (79.73, 78.10),
    "range_20261005_shot11": (79.83, 78.30),
    "range_20261006_shot10": (70.77, 70.06),
    "range_20261006_shot15": (73.82, 72.30),
    "range_20261009_shot1": (75.76, 75.05),
}
# label -> all-window vote (the phantom ball reported before)
NO_BALL = {
    "range_20261003b_shot25": 17.51,
    "range_20261005_shot46": 33.50,
    "range_20261005_shot47": 22.20,
    "range_20261005b_shot1": 33.60,
    "range_20261009b_shot1": 63.95,
}
# label -> all-window vote. range_20261003_shot1 is the weak 54-degree wedge
# echo the rule keeps: its post-impact 53-54 mph return repeats.
NORMAL = {
    "range_20261003_shot1": 54.37,
    "range_20261003_shot2": 86.14,
    "range_20261003_shot7": 92.15,
    "range_20261003_shot12": 101.32,
    "range_20261003b_shot3": 103.66,
    "range_20261003b_shot9": 115.98,
    "range_20261003b_shot14": 123.21,
    "range_20261003b_shot20": 137.46,
    "range_20261003b_shot24": 133.09,
    "range_20261005_shot5": 68.94,
    "range_20261005_shot22": 104.37,
    "range_20261005_shot39": 146.02,
    "range_20261006_shot16": 92.56,  # no club line: vote starts at trigger - 7.4 ms
    "range_20261009_shot6": 61.30,
    "range_20261009_shot9": 81.56,
}
# May-6 shots the 1-mph bin vote read wrong (TrackMan 117.26 / 162.17 mph). Shot 9
# took two stray 122.6/122.8 mph readings 21 ms apart above its 14-window ball;
# shot 13 a slower 53.8/54.2 mph pair 30 ms apart (all-window: 63.0/63.4, one
# before and one after impact) over its 156.2/156.8 mph ball in adjacent windows.
# The track vote (two readings at most BALL_TRACK_MAX_WINDOW_GAP windows apart)
# reads both balls; before it they read 122.80/122.80 and 64.96/54.17 mph.
TRACKMAN_RECOVERED = {
    "trackman_20260506_shot9": (114.35, 114.35),
    "trackman_20260506_shot13": (157.83, 157.83),
}
# Per-club raw ball-speed RMSE vs TrackMan of the 22 good May-6 shots before this change.
TRACKMAN_RAW_RMSE_MPH = {"driver": 2.99, "7-iron": 2.71, "pw": 2.62}


def _load(path: Path) -> dict:
    with np.load(path) as data:
        return {key: data[key] for key in data.files}


@pytest.fixture(scope="module")
def ball_fixture() -> dict:
    return _load(BALL_FIXTURE)


def _capture(fixture: dict, index: int) -> IQCapture:
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


def _process(fixture: dict, label: str, *, anchor: bool):
    index = list(fixture["label"]).index(label)
    return RollingBufferProcessor().process_capture(
        _capture(fixture, index),
        club_type=club_category(str(fixture["club"][index])),
        anchor_club_to_trigger=anchor,
    )


def test_fixture_holds_every_pinned_capture(ball_fixture):
    pinned = (
        set(PRE_IMPACT_PICKS)
        | set(CLUB_FRAME_MAXIMA)
        | set(NO_BALL)
        | set(NORMAL)
        | set(TRACKMAN_RECOVERED)
    )
    assert set(ball_fixture["label"]) == pinned


@pytest.mark.parametrize("label", sorted(PRE_IMPACT_PICKS | CLUB_FRAME_MAXIMA))
def test_wedge_ball_speed_comes_from_post_impact_windows(ball_fixture, label):
    all_window_mph, post_impact_mph = (PRE_IMPACT_PICKS | CLUB_FRAME_MAXIMA)[label]

    processed = _process(ball_fixture, label, anchor=True)

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(post_impact_mph, abs=0.01)
    assert processed.ball_speed_mph < all_window_mph


@pytest.mark.parametrize("label", sorted(NORMAL))
def test_normal_captures_keep_their_ball_speed(ball_fixture, label):
    processed = _process(ball_fixture, label, anchor=True)

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(NORMAL[label], abs=0.25)


@pytest.mark.parametrize("label", sorted(NO_BALL))
def test_capture_without_post_impact_ball_is_rejected(ball_fixture, label):
    processed = _process(ball_fixture, label, anchor=True)

    assert processed.rejection_reason == "no_ball_track"
    assert processed.ball_speed_mph == pytest.approx(NO_BALL[label], abs=0.01)
    assert processed.spin is None
    assert processed.impact is None


@pytest.mark.parametrize("label", sorted(TRACKMAN_RECOVERED))
def test_trackman_shots_9_and_13_read_their_ball(ball_fixture, label):
    processed = _process(ball_fixture, label, anchor=True)
    truth = dict(zip(ball_fixture["trackman_label"], ball_fixture["trackman_ball_mph"]))

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(TRACKMAN_RECOVERED[label][1], abs=0.01)
    # A raw radial speed: below TrackMan's ball speed by the launch-angle cosine
    # (the 22 good May-6 shots read 0.8-3.7 mph low).
    assert float(truth[label]) - 5.0 < processed.ball_speed_mph < float(truth[label])
    # The club line is read again once the cap (1.25 x ball speed) admits it.
    assert processed.club_speed_mode == "anchored"


def test_without_trigger_anchor_ball_speed_is_unchanged(ball_fixture):
    before = {label: values[0] for label, values in (PRE_IMPACT_PICKS | CLUB_FRAME_MAXIMA).items()}
    before |= NO_BALL | NORMAL
    before |= {label: values[0] for label, values in TRACKMAN_RECOVERED.items()}

    for label, all_window_mph in before.items():
        processed = _process(ball_fixture, label, anchor=False)
        assert processed.rejection_reason is None, label
        assert processed.ball_speed_mph == pytest.approx(all_window_mph, abs=0.01), label


def test_trackman_raw_ball_speed_rmse_does_not_get_worse(ball_fixture):
    club_fixture = _load(CLUB_FIXTURE)
    truth = dict(zip(ball_fixture["trackman_label"], ball_fixture["trackman_ball_mph"]))
    errors: dict[str, list[float]] = {}
    for index, label in enumerate(club_fixture["label"]):
        if not str(label).startswith("trackman_"):
            continue
        club = str(club_fixture["club"][index])
        processed = RollingBufferProcessor().process_capture(
            _capture(club_fixture, index),
            club_type=club_category(club),
            anchor_club_to_trigger=True,
        )
        assert processed.rejection_reason is None, label
        errors.setdefault(club, []).append(processed.ball_speed_mph - float(truth[label]))

    assert {club: len(errs) for club, errs in errors.items()} == {
        "7-iron": 8,
        "driver": 7,
        "pw": 7,
    }
    for club, errs in errors.items():
        rmse = statistics.fmean(error * error for error in errs) ** 0.5
        assert rmse <= TRACKMAN_RAW_RMSE_MPH[club] + 0.005, club


class TestPostImpactVote:
    """Synthetic standard windows around a fixed impact."""

    TRIGGER_MS = 68.0
    CENTRE_MS = 128 / 30.0 / 2.0

    def _readings(self, points: list[tuple[float, float]]):
        from openflight.rolling_buffer.types import SpeedReading

        return [
            SpeedReading(speed, 10.0, centre - self.CENTRE_MS, "outbound")
            for centre, speed in points
        ]

    def test_vote_start_uses_club_line_end_or_merge_latency(self):
        from openflight.rolling_buffer.types import ClubSpeedEstimate

        processor = RollingBufferProcessor()

        with_line = processor._post_impact_vote_start_ms(
            ClubSpeedEstimate(speed_mph=80.0, line_end_ms=60.0, mode="anchored"), self.TRIGGER_MS
        )
        without_line = processor._post_impact_vote_start_ms(ClubSpeedEstimate(), self.TRIGGER_MS)

        assert with_line == pytest.approx(59.0)
        assert without_line == pytest.approx(self.TRIGGER_MS - 7.4)

    def test_repeated_bins_need_two_hits_and_are_fastest_first(self):
        readings = self._readings([(70.0, 81.2), (74.0, 72.6), (78.0, 72.9), (82.0, 30.1)])

        assert RollingBufferProcessor._repeated_speed_bins(readings) == [(73, 2)]
        assert RollingBufferProcessor._repeated_speed_bins(readings[:1]) == []

    @pytest.mark.parametrize(
        ("speeds", "expected"),
        [
            # May-6 shot 13's driver ball: three windows, three different 1-mph bins.
            ([156.2, 156.8, 157.8], True),
            ([72.4, 73.4], True),  # exactly 1 mph apart still repeats
            ([], False),
            ([33.5, 17.8, 15.5], False),  # post-impact readings of rejected captures
            ([20.2, 18.4, 22.2], False),
            ([81.2], False),
        ],
    )
    def test_ball_track_needs_two_readings_within_one_mph(self, speeds, expected):
        readings = self._readings([(70.0 + 4.0 * i, speed) for i, speed in enumerate(speeds)])

        assert RollingBufferProcessor._has_repeated_speed(readings) is expected


def test_driver_ball_spread_over_adjacent_bins_is_not_rejected(ball_fixture, monkeypatch):
    """May-6 shot 13 without its low-speed returns keeps its 156-158 mph ball.

    The driver ball shows in three post-impact standard windows at 156.2, 156.8
    and 157.8 mph: no 1-mph bin repeats, but it is a real ball track. (The
    unmodified capture reads it too since the track vote; see
    test_trackman_shots_9_and_13_read_their_ball.)
    """
    processor = RollingBufferProcessor()
    real = processor._process_overlapping_with_standard

    def ball_only_after_impact(capture):
        standard, timeline = real(capture)
        kept = [
            r
            for r in standard.readings
            if not r.is_outbound or processor._reading_center_ms(r) < 60.0 or r.speed_mph > 150.0
        ]
        return SpeedTimeline(kept, standard.sample_rate_hz, standard.capture), timeline

    monkeypatch.setattr(processor, "_process_overlapping_with_standard", ball_only_after_impact)
    index = list(ball_fixture["label"]).index("trackman_20260506_shot13")

    processed = processor.process_capture(
        _capture(ball_fixture, index),
        club_type=club_category(str(ball_fixture["club"][index])),
        anchor_club_to_trigger=True,
    )

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(157.8, abs=0.1)


def test_a_stray_reading_does_not_become_the_ball_without_a_repeated_bin(ball_fixture, monkeypatch):
    """Shot 13's bin-straddling ball plus one stray 171.4 mph reading reads the ball.

    No 1-mph bin repeats after impact, so the bin vote would fall back to
    max(post-impact) and report the stray. The fastest pair within 1 mph (the
    ball track that let the capture through, in adjacent windows) is voted
    instead.
    """
    from openflight.rolling_buffer.types import SpeedReading

    processor = RollingBufferProcessor()
    real = processor._process_overlapping_with_standard
    window_ms = processor.WINDOW_SIZE / processor.SAMPLE_RATE * 1000.0

    def ball_and_stray_after_impact(capture):
        standard, timeline = real(capture)
        kept = [
            r
            for r in standard.readings
            if not r.is_outbound or processor._reading_center_ms(r) < 60.0 or r.speed_mph > 150.0
        ]
        last_ball = max(
            (r for r in kept if r.is_outbound and r.speed_mph > 150.0),
            key=lambda r: r.timestamp_ms,
        )
        stray = SpeedReading(171.4, last_ball.magnitude, last_ball.timestamp_ms + window_ms)
        return SpeedTimeline(kept + [stray], standard.sample_rate_hz, standard.capture), timeline

    monkeypatch.setattr(
        processor, "_process_overlapping_with_standard", ball_and_stray_after_impact
    )
    index = list(ball_fixture["label"]).index("trackman_20260506_shot13")

    processed = processor.process_capture(
        _capture(ball_fixture, index),
        club_type=club_category(str(ball_fixture["club"][index])),
        anchor_club_to_trigger=True,
    )

    assert processed.rejection_reason is None
    assert processed.ball_speed_mph == pytest.approx(157.8, abs=0.1)


class TestCloseSpeedPairVote:
    """Post-impact vote without a repeated 1-mph bin."""

    def _readings(self, speeds: list[float]):
        from openflight.rolling_buffer.types import SpeedReading

        return [
            SpeedReading(speed, 10.0, 70.0 + 4.27 * i, "outbound") for i, speed in enumerate(speeds)
        ]

    @pytest.mark.parametrize(
        ("speeds", "expected"),
        [
            ([156.2, 156.81, 157.83], 156.81),
            ([156.2, 156.81, 157.83, 171.4], 156.81),
            ([72.4, 73.4, 90.0], 73.4),  # exactly 1 mph apart
            ([60.1, 60.9, 80.2, 80.7], 80.7),  # the fastest pair, not the first
            ([33.5, 17.8, 15.5], None),
            ([], None),
        ],
    )
    def test_fastest_close_pair_speed(self, speeds, expected):
        pair = RollingBufferProcessor._fastest_close_pair_speed(self._readings(speeds))

        assert pair == (None if expected is None else pytest.approx(expected))

    def test_a_repeated_bin_keeps_the_bin_vote(self):
        readings = self._readings([72.6, 72.9, 74.1, 90.3])

        assert RollingBufferProcessor._find_post_impact_ball_speed(readings) == pytest.approx(
            RollingBufferProcessor._find_consistent_ball_speed(readings)
        )

    def test_without_a_repeated_bin_the_close_pair_is_voted_not_the_maximum(self):
        readings = self._readings([156.2, 156.81, 157.83, 171.4])

        assert RollingBufferProcessor._find_post_impact_ball_speed(readings) == pytest.approx(
            157.83
        )


class TestBallTrackVote:
    """Repeated speeds count only from nearby standard windows (BALL_TRACK_MAX_WINDOW_GAP)."""

    @staticmethod
    def _readings(processor: RollingBufferProcessor, points: list[tuple[int, float]]):
        """(standard window index, speed) -> outbound readings on that processor's window grid."""
        from openflight.rolling_buffer.types import SpeedReading

        step_ms = processor.STEP_SIZE_STANDARD / processor.SAMPLE_RATE * 1000.0
        return [SpeedReading(speed, 10.0, window * step_ms, "outbound") for window, speed in points]

    def test_far_apart_repeated_bin_above_the_ball_is_not_a_track(self):
        """May-6 shot 9: two strays in bin 123 five windows apart above a dense ball."""
        processor = RollingBufferProcessor()
        ball = [(w, s) for w, s in enumerate([113.1, 112.7, 113.7, 113.6, 114.0, 113.4, 113.9])]
        readings = self._readings(processor, ball + [(1, 122.6), (6, 122.8)])

        assert processor._ball_track_bin(readings) == 114
        assert processor._vote_ball_speed(
            readings, processor._find_consistent_ball_speed
        ) == pytest.approx(114.0)
        # The any-time bin vote takes the strays.
        assert processor._find_consistent_ball_speed(readings) == pytest.approx(122.8)

    def test_adjacent_pair_straddling_bins_beats_a_far_apart_repeated_bin(self):
        """May-6 shot 13: a 156.2/156.81 ball in adjacent windows, 53.8/54.2 seven apart."""
        processor = RollingBufferProcessor()
        readings = self._readings(
            processor,
            [(0, 53.8), (1, 51.2), (2, 156.2), (3, 156.81), (7, 54.2), (7, 157.83), (8, 51.4)],
        )

        assert processor._ball_track_bin(readings) == 157
        assert processor._vote_ball_speed(
            readings, processor._find_post_impact_ball_speed
        ) == pytest.approx(157.83)
        assert processor._find_post_impact_ball_speed(readings) == pytest.approx(54.2)

    def test_a_repeated_bin_in_nearby_windows_keeps_precedence_over_a_faster_pair(self):
        """Bins first, then straddling pairs, as in the any-time vote."""
        processor = RollingBufferProcessor()
        readings = self._readings(processor, [(0, 134.9), (1, 135.0), (2, 139.4), (3, 140.32)])

        assert processor._ball_track_bin(readings) == 135

    @pytest.mark.parametrize(("gap", "expected"), [(1, 73), (2, 73), (3, None)])
    def test_one_missed_window_still_forms_a_track(self, gap, expected):
        processor = RollingBufferProcessor()
        readings = self._readings(processor, [(0, 72.6), (gap, 72.9)])

        assert processor._ball_track_bin(readings) == expected

    @pytest.mark.parametrize(("gap", "expected"), [(1, 156), (2, 156), (3, None)])
    def test_straddling_pair_obeys_the_same_window_gap(self, gap, expected):
        processor = RollingBufferProcessor()
        readings = self._readings(processor, [(0, 155.4), (gap, 156.3)])

        assert processor._ball_track_bin(readings) == expected

    @pytest.mark.parametrize(("gap", "expected"), [(2, 73), (3, None)])
    def test_window_gap_follows_the_sample_rate(self, gap, expected):
        """At 20 ksps a standard window steps 4.2 ms, not 4.27 ms."""
        processor = RollingBufferProcessor(sample_rate=20000)
        readings = self._readings(processor, [(0, 72.6), (gap, 72.9)])

        assert processor._ball_track_bin(readings) == expected

    def test_without_a_track_the_any_time_vote_is_kept(self):
        processor = RollingBufferProcessor()
        readings = self._readings(processor, [(0, 72.6), (1, 30.1), (4, 72.9), (6, 90.3)])

        assert processor._ball_track_bin(readings) is None
        assert processor._vote_ball_speed(
            readings, processor._find_consistent_ball_speed
        ) == pytest.approx(processor._find_consistent_ball_speed(readings))
        assert processor._vote_ball_speed([], processor._find_consistent_ball_speed) == 0.0


TRIGGER_ISO_TIME = "2026-10-05T16:41:12.345678+00:00"


def test_monitor_turns_a_rejected_capture_into_a_trigger_event(monkeypatch, ball_fixture):
    """No shot, TI dump released, one trigger event with the processor's reason."""
    from openflight.rolling_buffer import RollingBufferMonitor, monitor as monitor_module

    processed = _process(ball_fixture, "range_20261005_shot46", anchor=True)
    capture = processed.capture
    capture.first_byte_timestamp = 1234.5
    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    shots, states, diagnostics, rejected = [], [], [], []

    class OneCaptureTrigger:
        calls = 0

        def wait_for_trigger(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                kwargs["capture_started_callback"]()
                return capture
            monitor._running = False
            return None

        @staticmethod
        def drain_diagnostics():
            return [
                {
                    "timestamp": TRIGGER_ISO_TIME,
                    "accepted": True,
                    "reason": "accepted",
                    "response_bytes": 32768,
                }
            ]

        @staticmethod
        def reset():
            return None

    session_logger = MagicMock()
    monkeypatch.setattr(monitor_module, "get_session_logger", lambda: session_logger)
    monitor.trigger = OneCaptureTrigger()
    monitor.processor = MagicMock(process_capture=MagicMock(return_value=processed))
    monitor._shot_callback = shots.append
    monitor._processing_callback = states.append
    monitor._diagnostic_callback = diagnostics.append
    monitor._trigger_rejected_callback = lambda first_byte, reason: rejected.append(
        (first_byte, reason)
    )
    monitor._running = True

    monitor._capture_loop()

    assert shots == []
    assert states == ["capturing", "calculating", "failed"]
    assert rejected == [(1234.5, "no_ball_track")]
    event = session_logger.log_trigger_event.call_args.kwargs
    assert (event["accepted"], event["reason"]) == (False, "no_ball_track")
    assert event["response_bytes"] == 32768
    assert event["ball_speed_mph"] == pytest.approx(33.5, abs=0.01)
    # The raw I/Q stays in the session, so the rejection can be replayed offline.
    raw = session_logger.log_rolling_buffer_capture.call_args.kwargs
    assert session_logger.log_rolling_buffer_capture.call_count == 1
    assert (raw["shot_number"], raw["rejection_reason"]) == (None, "no_ball_track")
    assert raw["i_samples"] is capture.i_samples and raw["q_samples"] is capture.q_samples
    assert len(raw["i_samples"]) == 4096
    assert (raw["sample_time"], raw["trigger_time"]) == (capture.sample_time, capture.trigger_time)
    assert raw["first_byte_timestamp"] == 1234.5
    assert raw["club_line_end_ms"] == processed.club_line_end_ms
    assert raw["gate_fields"] == {}
    # No shot: no shot speeds that could be read as one.
    assert "ball_speed_mph" not in raw and "club_speed_mph" not in raw
    assert diagnostics[0]["capture_first_byte_timestamp"] == 1234.5
    # The UI reads the wall-clock trigger time, not the radar clock (trigger_time).
    assert diagnostics[0]["timestamp"] == TRIGGER_ISO_TIME


def test_monitor_start_keeps_the_rejection_callback_for_processor_rejections(monkeypatch):
    from openflight.rolling_buffer import RollingBufferMonitor

    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    monkeypatch.setattr(monitor, "_start_capture_thread", lambda: None)

    def callback(_first_byte, _reason):
        return None

    monitor.start(trigger_rejected_callback=callback)

    assert monitor._trigger_rejected_callback is callback
