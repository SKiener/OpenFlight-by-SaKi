"""Do not turn a tone erased by the fade fit into a spurious spin result."""

import numpy as np
import pytest
from test_multitaper_spin import _modulated_capture

from openflight.rolling_buffer import RollingBufferProcessor
from openflight.rolling_buffer.multitaper import estimate_multitaper_spin


@pytest.mark.parametrize("rpm", [2000, 3000, 4500, 5400])
def test_single_tone_inside_fade_band_is_ambiguous(rpm):
    t = np.arange(1200) / 30000
    envelope = 500 * (1 + 0.03 * np.sin(2 * np.pi * rpm / 60 * t + 0.7))
    result = estimate_multitaper_spin(envelope, 30000)
    assert result.residual_energy_fraction < 1e-4


def test_erased_tone_is_not_reported_as_a_different_spin():
    result = RollingBufferProcessor().detect_spin_multitaper(
        _modulated_capture(spin_rpm=4500, fade_depth=0, spin_depth=0.03, sample_count=1800),
        ball_speed_mph=100,
        ball_timestamp_ms=10,
    )
    assert result.spin_rpm == 0
    assert result.confidence == 0
    assert result.rejection_reason.startswith("Spin/fade ambiguity")
    assert result.candidates
    assert all(not c.selected for c in result.candidates)


def test_distinct_weak_tone_above_fade_is_preserved():
    result = RollingBufferProcessor().detect_spin_multitaper(
        _modulated_capture(),
        ball_speed_mph=100,
        ball_timestamp_ms=5,
    )
    assert result.spin_rpm == pytest.approx(9000, abs=500)
    assert result.rejection_reason is None
    assert result.quality == "experimental"
    assert result.confidence < 0.7


@pytest.mark.parametrize(
    ("spin_rpm", "spin_depth", "fade_depth"),
    [(3000, 0.03, 0.0), (4500, 0.03, 0.0), (0, 0.0, 0.03)],
)
def test_noisy_tone_inside_fade_band_is_not_replaced_by_a_noise_peak(
    spin_rpm, spin_depth, fade_depth
):
    """A lone in-band tone fits spin and fade equally; noise must not decide."""
    result = RollingBufferProcessor().detect_spin_multitaper(
        _modulated_capture(
            spin_rpm=spin_rpm,
            spin_depth=spin_depth,
            fade_depth=fade_depth,
            sample_count=1800,
            noise_std=10.0,
        ),
        ball_speed_mph=100,
        ball_timestamp_ms=10,
    )
    assert result.spin_rpm == 0
    assert result.confidence == 0
    assert result.rejection_reason.startswith("Spin/fade ambiguity")


@pytest.mark.parametrize(("spin_rpm", "fade_hz"), [(9000, 48.0), (4500, 48.0), (9000, 30.0)])
def test_spin_beside_a_real_fade_survives_noise(spin_rpm, fade_hz):
    """Fade removal must still expose a distinct tone beside a genuine in-band fade."""
    result = RollingBufferProcessor().detect_spin_multitaper(
        _modulated_capture(
            spin_rpm=spin_rpm,
            fade_hz=fade_hz,
            spin_depth=0.03,
            sample_count=1800,
            noise_std=10.0,
        ),
        ball_speed_mph=100,
        ball_timestamp_ms=10,
    )
    assert result.spin_rpm == pytest.approx(spin_rpm, abs=500)
    assert result.multipath_fade_hz is not None
    assert result.confidence < 0.7


@pytest.mark.parametrize("rpm", [5600, 5800])
def test_fade_fit_stuck_on_band_edge_is_not_removed(rpm):
    """No in-band fade exists, so the edge fit must not eat the tone just above it."""
    rng = np.random.default_rng(3)
    t = np.arange(1500) / 30000
    envelope = 500 * (1 + 0.03 * np.sin(2 * np.pi * rpm / 60 * t + 0.7))
    result = estimate_multitaper_spin(envelope + rng.normal(0, 3, len(t)), 30000)
    assert result.fade_hz is None
    assert result.residual_energy_fraction == pytest.approx(1.0)
    assert result.spin_rpm == pytest.approx(rpm, abs=250)


def _capture_with_window(window_ms, **kwargs):
    """A noisy capture whose spin analysis window, from onset at 10 ms, lasts window_ms."""
    samples_per_ms = RollingBufferProcessor.SAMPLE_RATE // 1000
    transient = int(RollingBufferProcessor.SAMPLE_RATE / RollingBufferProcessor.SPIN_BANDPASS_BW_HZ)
    return _modulated_capture(
        sample_count=(10 + window_ms) * samples_per_ms + 2 * transient,
        noise_std=10.0,
        **kwargs,
    )


@pytest.mark.parametrize("window_ms", [54, 74, 94, 114, 134])
def test_fade_only_yields_no_candidate_at_any_window_length(window_ms):
    """The 0.25 Hz fade-fit grid leaves a leftover that grows with the window: not spin."""
    processor = RollingBufferProcessor()
    candidates = []
    for fade_hz in (48.0, 70.0):
        for seed in range(20):
            result = processor.detect_spin_multitaper(
                _capture_with_window(window_ms, spin_depth=0.0, fade_hz=fade_hz, seed=seed),
                ball_speed_mph=100,
                ball_timestamp_ms=10,
            )
            if result.spin_rpm or not result.rejection_reason.startswith("Spin/fade ambiguity"):
                candidates.append((fade_hz, seed, result.spin_rpm))
    assert candidates == []


@pytest.mark.parametrize("window_ms", [54, 74, 94, 114, 134])
@pytest.mark.parametrize(("spin_rpm", "fade_hz"), [(9000, 48.0), (4500, 48.0), (9000, 30.0)])
def test_spin_beside_a_fade_is_recovered_at_any_window_length(spin_rpm, fade_hz, window_ms):
    result = RollingBufferProcessor().detect_spin_multitaper(
        _capture_with_window(window_ms, spin_rpm=spin_rpm, fade_hz=fade_hz, spin_depth=0.03),
        ball_speed_mph=100,
        ball_timestamp_ms=10,
    )
    assert result.spin_rpm == pytest.approx(spin_rpm, abs=500)
    assert result.multipath_fade_hz is not None
    assert result.confidence < 0.7


@pytest.mark.parametrize("window_ms", [94, 114, 134])
def test_long_window_keeps_its_resolution_beside_the_fade_harmonic(window_ms):
    """6000 rpm lies 4 Hz from the 96 Hz fade harmonic; only a long window recovers it."""
    result = RollingBufferProcessor().detect_spin_multitaper(
        _capture_with_window(window_ms, spin_rpm=6000, fade_hz=48.0, spin_depth=0.03),
        ball_speed_mph=100,
        ball_timestamp_ms=10,
    )
    assert result.spin_rpm == pytest.approx(6000, abs=500)
    assert result.confidence < 0.7


@pytest.mark.parametrize("sample_rate", [30_000, 20_000])
def test_long_window_is_also_judged_on_its_first_75_ms(monkeypatch, sample_rate):
    """The guard window is a duration, so it holds at every OPS sample rate."""
    envelope_lengths = []

    def record(envelope, *args, **kwargs):
        envelope_lengths.append(len(envelope))
        return estimate_multitaper_spin(envelope, *args, **kwargs)

    monkeypatch.setattr("openflight.rolling_buffer.processor.estimate_multitaper_spin", record)
    RollingBufferProcessor(sample_rate=sample_rate).detect_spin_multitaper(
        _modulated_capture(sample_rate_hz=sample_rate), ball_speed_mph=100, ball_timestamp_ms=10
    )

    guard_samples = 75 * sample_rate // 1000
    assert envelope_lengths[0] > guard_samples
    assert envelope_lengths[1:] == [guard_samples]
