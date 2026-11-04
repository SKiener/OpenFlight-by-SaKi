"""The spin envelope must come from the ball's own Doppler band, not its mirror."""

import pytest
from scipy.signal import butter, sosfiltfilt
from test_multitaper_spin import _modulated_capture, _with_tone

from openflight.rolling_buffer import RollingBufferProcessor

BALL_DOPPLER_HZ = (
    2.0 * 100.0 / RollingBufferProcessor.MPS_TO_MPH / RollingBufferProcessor.WAVELENGTH_M
)
DETECTORS = ["detect_spin_multitaper", "detect_spin"]


def _real_coefficient_bandpass(processor, iq, ball_speed_mph):
    """The previous design: a real Butterworth bandpass applied to complex I/Q."""
    nyquist = processor.SAMPLE_RATE / 2
    doppler_hz = 2.0 * ball_speed_mph / processor.MPS_TO_MPH / processor.WAVELENGTH_M
    band = [
        (doppler_hz - processor.SPIN_BANDPASS_BW_HZ) / nyquist,
        (doppler_hz + processor.SPIN_BANDPASS_BW_HZ) / nyquist,
    ]
    sos = butter(processor.SPIN_BANDPASS_ORDER, band, btype="band", output="sos")
    return sosfiltfilt(sos, iq)


@pytest.mark.parametrize("detector", DETECTORS)
@pytest.mark.parametrize(("offset_hz", "am_hz"), [(0.0, 0.0), (300.0, 0.0), (300.0, 125.0)])
def test_mirror_band_return_leaves_the_ball_envelope_unchanged(detector, offset_hz, am_hz):
    """An inbound return at -f_d (+offset, inside the old mirror passband) is rejected."""
    processor = RollingBufferProcessor()
    clean = _modulated_capture(spin_depth=0.03, noise_std=10.0)
    mirrored = _with_tone(clean, -BALL_DOPPLER_HZ + offset_hz, 400.0, am_hz=am_hz, am_depth=0.3)

    expected = getattr(processor, detector)(clean, ball_speed_mph=100.0, ball_timestamp_ms=5.0)
    result = getattr(processor, detector)(mirrored, ball_speed_mph=100.0, ball_timestamp_ms=5.0)

    assert result.spin_rpm == pytest.approx(expected.spin_rpm, abs=60)
    assert result.modulation_depth == pytest.approx(expected.modulation_depth, rel=0.02)


@pytest.mark.parametrize("detector", DETECTORS)
def test_clean_ball_band_matches_the_previous_bandpass(monkeypatch, detector):
    """Without a mirror-band return the passband edges and results stay as before."""
    processor = RollingBufferProcessor()
    capture = _modulated_capture(spin_depth=0.03)

    result = getattr(processor, detector)(capture, ball_speed_mph=100.0, ball_timestamp_ms=5.0)
    monkeypatch.setattr(
        processor,
        "_ball_band_baseband",
        lambda iq, speed: _real_coefficient_bandpass(processor, iq, speed),
    )
    previous = getattr(processor, detector)(capture, ball_speed_mph=100.0, ball_timestamp_ms=5.0)

    assert result.spin_rpm == previous.spin_rpm
    assert result.modulation_depth == pytest.approx(previous.modulation_depth, rel=0.005)


@pytest.mark.parametrize("detector", DETECTORS)
@pytest.mark.parametrize(
    ("ball_speed_mph", "sample_count", "reason"),
    [
        (230.0, 4096, "Ball Doppler outside filter range"),
        (100.0, 10, "Bandpass filter failed"),
    ],
)
def test_unfilterable_capture_is_rejected(detector, ball_speed_mph, sample_count, reason):
    """Doppler beyond Nyquist, or a truncated dump, yields a reasoned rejection."""
    result = getattr(RollingBufferProcessor(), detector)(
        _modulated_capture(sample_count=sample_count),
        ball_speed_mph=ball_speed_mph,
        ball_timestamp_ms=5.0,
    )

    assert result.spin_rpm == 0
    assert result.rejection_reason.startswith(reason)


def test_ground_fade_with_modulated_mirror_mover_yields_no_candidate():
    """A fading ball with no spin stays a non-measurement next to an inbound mover."""
    capture = _with_tone(
        _modulated_capture(spin_depth=0.0, sample_count=1800, noise_std=10.0),
        -BALL_DOPPLER_HZ + 300.0,
        400.0,
        am_hz=125.0,
        am_depth=0.3,
    )

    result = RollingBufferProcessor().detect_spin_multitaper(
        capture, ball_speed_mph=100.0, ball_timestamp_ms=10.0
    )

    assert result.spin_rpm == 0
    assert result.rejection_reason.startswith("Spin/fade ambiguity")
