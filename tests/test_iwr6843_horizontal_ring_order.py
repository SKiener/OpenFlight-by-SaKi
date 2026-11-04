"""Horizontal tail selection must follow time, not physical ring-slot numbers."""

from types import SimpleNamespace

import numpy as np
import pytest

from openflight.iwr6843 import doa, lcmf


@pytest.mark.parametrize("trigger_slot", [0, 1, 3, 5, 9, 11])
@pytest.mark.parametrize("variable_cadence", [False, True])
def test_horizontal_tail_is_invariant_to_ring_storage_order(
    monkeypatch, trigger_slot, variable_cadence
):
    n_frames, loops = 12, 2
    metadata = {
        "n_frames": n_frames,
        "chirps_per_frame": loops * 3,
        "n_tx": 3,
        "n_rx": 4,
        "n_samples": 8,
        "trigger_frame": trigger_slot,
        "frame_period_us": 3000,
    }
    if variable_cadence:
        metadata["frame_time_offsets_us"] = tuple(
            [i * 2000 for i in range(6)] + [12000 + i * 4000 for i in range(6)]
        )
    cube = np.zeros((n_frames, loops * 3, 4, 8), dtype=np.complex128)
    prepared = SimpleNamespace(
        full_metadata=metadata,
        full_cube=cube,
        horizontal_mti=lambda: cube.reshape(n_frames, loops, 3, 4, 8),
    )
    phases = np.linspace(-0.2, 0.3, n_frames)

    def sample(_mti, frame, _loop, _bin, **_kwargs):
        chronological_index = (frame - trigger_slot) % n_frames
        return float(phases[chronological_index]), 1.0

    monkeypatch.setattr(doa, "tx2_phase_at", sample)
    track = SimpleNamespace(t_first=0.0, t_last=0.04, bin_at=lambda _time: 3.0)
    shot = SimpleNamespace(track=track)
    angle, coherence, status = lcmf._tx2_horizontal_proxy(
        b"", shot, tdm_sign=1, phase_velocity_ms=45.0, prepared=prepared
    )
    expected_phase = np.angle(np.mean(np.exp(1j * phases[-8:])))
    expected_angle = -np.degrees(np.arcsin(expected_phase / np.pi))
    assert status == "hlcmf_v1_accepted"
    assert coherence > 0.99
    assert angle == pytest.approx(expected_angle, abs=1e-10)
