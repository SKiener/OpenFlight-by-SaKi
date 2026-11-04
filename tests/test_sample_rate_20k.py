"""Rolling-buffer processing at OPS sample rates below 30 ksps (``--sample-rate 20``).

At 20 ksps complex I/Q is unambiguous only to 139.0 mph (AN-027): a driver ball
folds onto the inbound half. These tests pin the unfolding, the image rejection
it needs, the 30 ksps time grid of the FFT framing, and every place where the
buffer's time base used to be 30 ksps regardless of the configured rate.
"""

import json
from unittest.mock import Mock

import numpy as np
import pytest

from openflight.ops243 import OPS243Radar
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger, SpeedTriggeredCapture
from openflight.rolling_buffer.types import IQCapture, logged_sample_rate_hz
from openflight.session_logger import SessionLogger

FS20 = 20_000
MPH_PER_HZ = RollingBufferProcessor.WAVELENGTH_M / 2 * RollingBufferProcessor.MPS_TO_MPH


def _hz(speed_mph: float) -> float:
    """Doppler of a receding (positive) or approaching (negative) mover."""
    return speed_mph / MPH_PER_HZ


def _capture(
    tones: list[tuple[float, float, float]],
    *,
    sample_rate_hz: int = FS20,
    sample_count: int = 4096,
    image_db: float = -30.0,
    noise_std: float = 1.0,
    am: tuple[float, float] | None = None,
    trigger_offset_ms: float = 38.4,
) -> IQCapture:
    """OPS-like capture: (speed_mph, amplitude, onset_ms) tones plus their I/Q images.

    Each tone is sampled at ``sample_rate_hz`` from its true Doppler, so a tone
    beyond fs/2 folds as on the radar. The image (conjugate, ``image_db`` below)
    models the OPS I/Q imbalance; ``am`` = (rate_hz, depth) seam-modulates them.
    """
    time_s = np.arange(sample_count) / sample_rate_hz
    signal = np.zeros(sample_count, dtype=complex)
    for speed_mph, amplitude, onset_ms in tones:
        envelope = amplitude * (time_s >= onset_ms / 1000.0)
        if am is not None:
            envelope = envelope * (1.0 + am[1] * np.sin(2 * np.pi * am[0] * time_s))
        tone = envelope * np.exp(2j * np.pi * _hz(speed_mph) * time_s)
        signal += tone + 10 ** (image_db / 20) * np.conj(tone)
    noise = np.random.default_rng(3).normal(0.0, noise_std, (2, sample_count))
    i_samples = np.clip(np.rint(2048 + signal.real + noise[0]), 0, 4095).astype(int)
    q_samples = np.clip(np.rint(2048 + signal.imag + noise[1]), 0, 4095).astype(int)
    return IQCapture(
        sample_time=10.0,
        trigger_time=10.0 + trigger_offset_ms / 1000.0,
        i_samples=i_samples.tolist(),
        q_samples=q_samples.tolist(),
        sample_rate_hz=sample_rate_hz,
    )


class TestFraming:
    def test_30ksps_keeps_every_tuned_constant(self):
        processor = RollingBufferProcessor(sample_rate=30_000)

        assert processor.WINDOW_SIZE == 128
        assert processor.STEP_SIZE_STANDARD == 128
        assert processor.STEP_SIZE_OVERLAP == 32
        assert processor.DC_MASK_BINS == 150
        assert processor.MIN_PEAK_SEPARATION_BINS == 50
        assert processor.MAGNITUDE_THRESHOLD == 3
        assert processor.SPIN_MIN_SAMPLES == 600
        assert processor.SPIN_SIGNAL_LOSS_SMOOTH_SAMPLES == 90
        assert processor.SPIN_SIGNAL_LOSS_REF_SAMPLES == 450
        assert processor.SPIN_SIGNAL_LOSS_HOLD_SAMPLES == 150
        assert processor.doppler_unfolding is False
        assert processor.max_outbound_speed_mph == pytest.approx(208.5, abs=0.1)

    def test_20ksps_keeps_the_30ksps_time_grid_and_speed_zones(self):
        processor = RollingBufferProcessor(sample_rate=FS20)
        ms = 1000.0 / FS20

        assert processor.WINDOW_SIZE == 84  # 4.2 ms (4.27 ms at 30 ksps)
        assert processor.STEP_SIZE_OVERLAP == 21  # 1.05 ms (1.07 ms)
        assert processor.STEP_SIZE_STANDARD == processor.WINDOW_SIZE
        assert processor.WINDOW_SIZE % processor.STEP_SIZE_OVERLAP == 0
        assert processor.MAGNITUDE_THRESHOLD == pytest.approx(3 * np.sqrt(84 / 128))
        assert processor._speed_mph_for_bin(processor.DC_MASK_BINS) == pytest.approx(
            RollingBufferProcessor(30_000)._speed_mph_for_bin(150), abs=0.05
        )
        assert processor._speed_mph_for_bin(processor.MIN_PEAK_SEPARATION_BINS) == pytest.approx(
            5.1, abs=0.1
        )
        assert processor.SPIN_MIN_SAMPLES * ms == pytest.approx(20.0)
        assert processor.SPIN_SIGNAL_LOSS_SMOOTH_SAMPLES * ms == pytest.approx(3.0)
        assert processor.SPIN_SIGNAL_LOSS_REF_SAMPLES * ms == pytest.approx(15.0)
        assert processor.SPIN_SIGNAL_LOSS_HOLD_SAMPLES * ms == pytest.approx(5.0)
        assert processor.doppler_unfolding is True
        assert processor.max_outbound_speed_mph == pytest.approx(200.0, abs=0.1)


class TestDopplerUnfolding:
    @pytest.mark.parametrize("ball_mph", [141.0, 147.0, 159.7, 185.0])
    def test_folded_driver_ball_reads_its_true_speed(self, ball_mph):
        """A receding ball above 139 mph aliases to an inbound mover at 20 ksps."""
        capture = _capture([(103.0, 300.0, 0.0), (ball_mph, 200.0, 40.0)])

        processed = RollingBufferProcessor(sample_rate=FS20).process_capture(capture)

        assert processed is not None
        assert processed.ball_speed_mph == pytest.approx(ball_mph, abs=0.6)

    def test_ball_just_below_the_fold_is_unchanged(self):
        capture = _capture([(98.0, 300.0, 0.0), (137.5, 200.0, 40.0)])

        processed = RollingBufferProcessor(sample_rate=FS20).process_capture(capture)

        assert processed.ball_speed_mph == pytest.approx(137.5, abs=0.6)

    def test_club_image_does_not_become_a_phantom_fast_ball(self):
        """A strong 103 mph club's -24 dB image would unfold to a 175 mph 'ball'."""
        capture = _capture([(103.0, 1200.0, 0.0)], image_db=-24.0)

        timeline = RollingBufferProcessor(sample_rate=FS20).process_standard(capture)
        outbound = [r.speed_mph for r in timeline.readings if r.is_outbound]

        assert outbound and max(outbound) < 110.0
        assert all(r.speed_mph < 110.0 for r in timeline.readings)

    def test_folded_ball_image_is_not_reported_as_a_slower_ball(self):
        """The folded ball's own image sits at fs - v (131 mph for 147 mph)."""
        capture = _capture([(147.0, 900.0, 0.0)], image_db=-20.0)

        timeline = RollingBufferProcessor(sample_rate=FS20).process_standard(capture)

        speeds = [r.speed_mph for r in timeline.readings]
        assert speeds and all(abs(s - 147.0) < 1.0 for s in speeds)

    def test_slow_inbound_mover_stays_inbound(self):
        capture = _capture([(-35.0, 300.0, 0.0)])

        timeline = RollingBufferProcessor(sample_rate=FS20).process_standard(capture)

        assert timeline.readings
        assert all(not r.is_outbound for r in timeline.readings)
        assert all(abs(r.speed_mph - 35.0) < 1.0 for r in timeline.readings)

    def test_30ksps_does_not_unfold_or_reject_images(self):
        """At 30 ksps an inbound 150 mph return stays inbound (no unfolding)."""
        capture = _capture([(-150.0, 300.0, 0.0)], sample_rate_hz=30_000, image_db=-60)

        timeline = RollingBufferProcessor(sample_rate=30_000).process_standard(capture)

        assert timeline.readings
        assert all(not r.is_outbound for r in timeline.readings)

    def test_spin_band_of_a_folded_ball_wraps_around_fs_half(self):
        """The ball band is mixed down at its unfolded Doppler, not clipped at fs/2."""
        processor = RollingBufferProcessor(sample_rate=FS20)
        capture = _capture([(147.0, 500.0, 0.0)], image_db=-90.0, noise_std=0.0)
        iq = (np.asarray(capture.i_samples) - 2048.0) + 1j * (
            np.asarray(capture.q_samples) - 2048.0
        )

        baseband = processor._ball_band_baseband(iq, 147.0)

        middle = slice(500, -500)
        assert np.mean(np.abs(baseband[middle]) ** 2) == pytest.approx(500.0**2, rel=0.05)

    def test_multitaper_spin_of_a_folded_ball(self):
        # 120 Hz seam tone (7200 rpm), outside the 25-90 Hz multipath-fade band.
        capture = _capture([(147.0, 500.0, 0.0)], image_db=-33.0, noise_std=0.5, am=(120.0, 0.03))

        spin = RollingBufferProcessor(sample_rate=FS20).detect_spin_multitaper(
            capture, ball_speed_mph=147.0, ball_timestamp_ms=5.0
        )

        assert spin.rejection_reason is None
        assert spin.spin_rpm == pytest.approx(7200.0, abs=150.0)

    def test_ball_beyond_the_unfolded_range_is_rejected_for_spin(self):
        capture = _capture([(147.0, 500.0, 0.0)])

        spin = RollingBufferProcessor(sample_rate=FS20).detect_spin_multitaper(
            capture, ball_speed_mph=230.0, ball_timestamp_ms=5.0
        )

        assert spin.spin_rpm == 0
        assert spin.rejection_reason.startswith("Ball Doppler outside filter range")


class TestTimeBase:
    def test_capture_span_and_post_trigger_follow_the_sample_rate(self):
        capture = IQCapture(10.0, 10.0384, [2048] * 4096, [2048] * 4096, sample_rate_hz=FS20)

        assert capture.duration_ms == pytest.approx(204.8)
        assert capture.post_trigger_duration_ms == pytest.approx(166.4)

    def test_first_byte_trigger_time_uses_the_20ksps_post_trigger_span(self):
        """The gate match and the first-byte fallback both rely on this span."""
        response = "\n".join(
            [
                json.dumps({"sample_time": "10.000"}),
                json.dumps({"trigger_time": "10.0384"}),
                json.dumps({"I": [2048] * 4096}),
                json.dumps({"Q": [2048] * 4096}),
            ]
        )

        capture = RollingBufferProcessor(sample_rate=FS20).parse_capture(
            response, first_byte_timestamp=1000.0
        )

        assert capture.sample_rate_hz == FS20
        assert capture.trigger_timestamp == pytest.approx(1000.0 - 0.1664)

    def test_processor_refuses_a_capture_of_another_rate(self):
        capture = _capture([(100.0, 300.0, 0.0)], sample_rate_hz=30_000)

        with pytest.raises(ValueError, match="30000 Hz"):
            RollingBufferProcessor(sample_rate=FS20).process_capture(capture)

    def test_split_mismatch_restores_the_sample_rate(self):
        """A radar left at 30 ksps passes the timebase check but not the S#6 split."""
        processor = RollingBufferProcessor(sample_rate=FS20)
        # S#6 at 30 ksps: 25.6 ms instead of 38.4 ms, well inside a 204.8 ms span.
        capture = IQCapture(100.0, 100.0256, [2048] * 4096, [2048] * 4096, sample_rate_hz=FS20)
        processor.parse_capture = Mock(return_value=capture)
        radar = Mock()
        radar.wait_for_hardware_trigger.return_value = "field capture"
        radar.last_hardware_trigger_first_byte_timestamp = 101.0
        trigger = SoundTrigger(pre_trigger_segments=6)

        assert trigger.wait_for_trigger(radar, processor, timeout=1) is None

        radar.set_sample_rate.assert_called_once_with(FS20)
        radar.rearm_rolling_buffer.assert_called_once_with(6)
        assert trigger.drain_diagnostics()[0]["reason"] == "trigger_split_mismatch"


class TestLoggedSampleRate:
    def test_capture_entry_records_its_sample_rate(self, tmp_path):
        logger = SessionLogger(log_dir=tmp_path, enabled=True)
        logger.start_session(mode="rolling-buffer", trigger_type="sound")

        logger.log_rolling_buffer_capture(
            shot_number=1,
            sample_time=10.0,
            trigger_time=10.0384,
            i_samples=[2048] * 4,
            q_samples=[2048] * 4,
            sample_rate_hz=FS20,
        )

        entry = json.loads(logger.session_path.read_text().strip().split("\n")[-1])
        assert entry["sample_rate_hz"] == FS20
        assert logged_sample_rate_hz(entry) == FS20

    def test_entries_without_a_rate_are_30ksps(self):
        assert logged_sample_rate_hz({"type": "rolling_buffer_capture"}) == 30_000
        assert logged_sample_rate_hz({"sample_rate_hz": None}) == 30_000


class TestRadarCommands:
    @staticmethod
    def _written(radar) -> bytes:
        return b"".join(call.args[0] for call in radar.serial.write.call_args_list)

    @pytest.fixture(name="radar")
    def fixture_radar(self, monkeypatch):
        radar = OPS243Radar()
        radar.serial = Mock(is_open=True, in_waiting=0, out_waiting=0)
        radar._read_reply = Mock(return_value="")
        monkeypatch.setattr("openflight.ops243.time.sleep", lambda _delay: None)
        return radar

    def test_persisted_rolling_buffer_sets_20ksps_with_s_equals(self, radar):
        """S=20, the configurable form S=30 already runs with, not the fixed-rate S2."""
        radar.prepare_persisted_rolling_buffer(6, 20, transmit_power=None, flush_timeout_s=1)

        written = self._written(radar)
        assert b"S=20\r" in written and b"S#6\r" in written
        assert b"S2" not in written

    def test_speed_trigger_capture_uses_the_processor_rate(self, radar):
        radar.switch_to_rolling_buffer(sample_rate=FS20)

        assert b"S=20\r" in self._written(radar)

    def test_speed_trigger_hands_the_processor_rate_to_the_radar(self):
        radar = Mock()
        radar.serial = None
        radar.read_speed_nonblocking.return_value = Mock(speed=40.0)
        radar.trigger_capture.return_value = ""
        processor = RollingBufferProcessor(sample_rate=FS20)
        processor.parse_capture = Mock(return_value=None)
        trigger = SpeedTriggeredCapture(trigger_to_capture_delay_ms=0.0)

        trigger.wait_for_trigger(radar, processor, timeout=0.01)

        radar.switch_to_rolling_buffer.assert_called_with(sample_rate=FS20)
