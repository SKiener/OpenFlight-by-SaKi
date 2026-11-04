"""Placement of the multitaper spin analysis window inside a capture."""

import pytest
from test_multitaper_spin import _modulated_capture, _with_tone

from openflight.rolling_buffer import RollingBufferProcessor


def _with_saturating_return(capture, start_ms, duration_ms):
    """Add a strong out-of-band club return that drives the ADC into its rails."""
    doppler_hz = (
        2.0 * 70.0 / RollingBufferProcessor.MPS_TO_MPH / RollingBufferProcessor.WAVELENGTH_M
    )
    return _with_tone(capture, doppler_hz, 2400.0, start_ms=start_ms, duration_ms=duration_ms)


def _record_spin_window_starts(processor, monkeypatch):
    """Collect the sample index at which each spin analysis window starts."""
    starts = []
    find_signal_end = processor._ball_signal_end_sample

    def record(envelope, start_sample):
        starts.append(start_sample)
        return find_signal_end(envelope, start_sample)

    monkeypatch.setattr(processor, "_ball_signal_end_sample", record)
    return starts


def test_spin_window_starts_at_ball_onset_not_at_strongest_frame(monkeypatch):
    """Rising multipath gain makes the last frames strongest, long after onset."""
    processor = RollingBufferProcessor()
    starts = _record_spin_window_starts(processor, monkeypatch)

    processed = processor.process_capture(
        _modulated_capture(spin_depth=0.03, noise_std=10.0, onset_ms=20.0, end_gain=2.0)
    )

    onset_sample = 20.0 * processor.SAMPLE_RATE / 1000.0
    assert abs(starts[0] - onset_sample) <= processor.WINDOW_SIZE / 2
    assert processed.spin.spin_rpm == pytest.approx(9000, abs=500)
    assert processed.spin.confidence < 0.7


def test_spin_window_starts_at_centre_of_first_ball_frame(monkeypatch):
    """A reading's timestamp is its FFT window start; onset is the window centre."""
    processor = RollingBufferProcessor()
    starts = _record_spin_window_starts(processor, monkeypatch)

    processor.process_capture(_modulated_capture(spin_depth=0.0, fade_depth=0.0, end_gain=0.25))

    assert starts == [processor.WINDOW_SIZE // 2]


def test_spin_window_starts_after_saturation_at_onset(monkeypatch):
    """Interpolated rail samples right after onset must not become spin evidence."""
    processor = RollingBufferProcessor()
    starts = _record_spin_window_starts(processor, monkeypatch)
    capture = _with_saturating_return(
        _modulated_capture(spin_depth=0.0, sample_count=1500, noise_std=10.0),
        start_ms=10.0,
        duration_ms=4.0,
    )

    result = processor.detect_spin_multitaper(capture, ball_speed_mph=100.0, ball_timestamp_ms=10.0)

    assert starts[0] >= 14.0 * processor.SAMPLE_RATE / 1000.0
    assert result.spin_rpm == 0
    assert result.rejection_reason.startswith("Spin/fade ambiguity")


def test_unclipped_capture_keeps_onset_as_window_start(monkeypatch):
    processor = RollingBufferProcessor()
    starts = _record_spin_window_starts(processor, monkeypatch)

    result = processor.detect_spin_multitaper(
        _modulated_capture(), ball_speed_mph=100.0, ball_timestamp_ms=5.0
    )

    assert starts == [150]
    assert result.spin_rpm == pytest.approx(9000, abs=500)


def test_saturation_inside_the_window_does_not_move_its_start(monkeypatch):
    processor = RollingBufferProcessor()
    starts = _record_spin_window_starts(processor, monkeypatch)
    capture = _with_saturating_return(_modulated_capture(), start_ms=20.0, duration_ms=2.0)

    processor.detect_spin_multitaper(capture, ball_speed_mph=100.0, ball_timestamp_ms=5.0)

    assert starts == [150]


@pytest.mark.parametrize("saturation_ms", [None, 4.0])
def test_ground_fade_without_spin_yields_no_candidate(saturation_ms):
    """A pure ground fade stays a non-measurement, with or without impact saturation."""
    capture = _modulated_capture(spin_depth=0.0, noise_std=10.0, onset_ms=20.0)
    if saturation_ms is not None:
        capture = _with_saturating_return(capture, start_ms=20.0, duration_ms=saturation_ms)

    processed = RollingBufferProcessor().process_capture(capture)

    assert processed.spin.spin_rpm == 0
    assert processed.spin.rejection_reason.startswith("Spin/fade ambiguity")
