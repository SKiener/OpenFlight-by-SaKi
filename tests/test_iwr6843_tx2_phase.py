"""The TX2 phase helper is shared by the ball's horizontal proxy and club path.

Extracting it must not move the horizontal number: this test pins the helper's
contract, and test_iwr6843_pipeline.py pins the proxy's output.
"""

import numpy as np
import pytest
from iwr6843_three_tx_synth import ball_flight_dump, channel_matrix

from openflight.iwr6843 import doa, lcmf
from openflight.iwr6843.shot import ShotMeasurement


def _tdm(phase_offset_rad, *, n_frames=2, loops=2, n_rx=4, n_samples=8, n_tx=3):
    """TDM-split cube where TX2 leads the TX1/TX3 reference by a known phase."""
    tdm = np.zeros((n_frames, loops, n_tx, n_rx, n_samples), dtype=complex)
    tdm[:, :, 0, :, :] = 1.0 + 0j
    tdm[:, :, 2, :, :] = 1.0 + 0j
    tdm[:, :, 1, :, :] = np.exp(1j * phase_offset_rad)
    return tdm


def test_recovers_a_known_tx2_phase_offset():
    tdm = _tdm(0.4)
    result = doa.tx2_phase_at(tdm, 0, 0, 3, velocity_ms=0.0, tdm_sign=1, n_rx=4)
    assert result is not None
    phase, weight = result
    np.testing.assert_allclose(phase, 0.4, atol=1e-6)
    assert weight > 0.0


def test_zero_amplitude_returns_none():
    tdm = np.zeros((1, 1, 3, 4, 8), dtype=complex)
    assert doa.tx2_phase_at(tdm, 0, 0, 3, velocity_ms=0.0, tdm_sign=1, n_rx=4) is None


def test_circular_median_wraps():
    values = [3.0, -3.0, 3.1]
    median = doa.circular_median(values)
    assert abs(abs(median) - 3.05) < 0.2, "median must wrap, not average through zero"


def test_circular_median_does_not_degrade_to_a_linear_median():
    """Pins the wrap handling against a regression to plain ``np.median``.

    The test above uses [3.0, -3.0, 3.1], whose *linear* median is 3.0 -- well
    inside its 0.2 tolerance of 3.05. So replacing the whole implementation
    with ``np.median`` passes it, and the only real property being asserted
    goes unguarded.

    These values separate the two: they straddle +/-pi evenly, so the linear
    median collapses to ~0.0 -- diametrically opposite the actual cluster --
    while a circular median stays on the cluster near +/-pi.
    """
    values = [3.10, 3.13, -3.13, -3.10]
    assert abs(float(np.median(values))) < 0.1, "fixture must separate the two definitions"
    median = doa.circular_median(values)
    assert abs(median) > 3.0, "collapsed toward zero, i.e. averaged through the wrap"
    assert median in values, "a median returns one of its inputs, never a synthesised value"


def _phase_of(matrix):
    tdm = matrix[None, None, :, :, None]
    phase, _weight = doa.tx2_phase_at(tdm, 0, 0, 0, velocity_ms=0.0, tdm_sign=1, n_rx=4)
    return phase


@pytest.mark.parametrize("vertical_cosine", [-0.45, -0.3, 0.27, 0.35, 0.45])
def test_reference_survives_high_elevation_branch_jump(vertical_cosine):
    """TX1/TX3 are 2 lambda apart: |sin(el)| > 0.25 used to flip the midpoint by pi."""
    lateral = 0.1
    phase = _phase_of(channel_matrix(vertical_cosine, lateral, phase_zero_rad=-0.37))

    assert phase == pytest.approx(-np.pi * lateral - 0.37, abs=1e-9)


@pytest.mark.parametrize("vertical_cosine", [-0.2, 0.15])
def test_reference_ignores_unequal_tx1_tx3_gains(vertical_cosine):
    matrix = channel_matrix(vertical_cosine, 0.05, tx_gains=(1.25, 1.0, 0.8))

    assert _phase_of(matrix) == pytest.approx(-np.pi * 0.05, abs=1e-9)


@pytest.mark.parametrize("vertical_cosine", [-0.2, -0.1, 0.0, 0.1, 0.2])
def test_low_elevation_equal_gain_targets_are_unchanged(vertical_cosine):
    """Below the branch limit the old complex midpoint and the new reference agree."""
    matrix = channel_matrix(vertical_cosine, -0.08, phase_zero_rad=0.3)
    midpoint = 0.5 * (matrix[0] + matrix[2])
    legacy = doa.circular_median(list(np.angle(np.conj(midpoint) * matrix[1])))

    assert _phase_of(matrix) == pytest.approx(legacy, abs=1e-12)


def test_high_launch_ball_bearing_has_no_pi_jump():
    flight = ball_flight_dump(hla_deg=5.0, vla_deg=45.0, tilt_deg=0.0, tee_m=(1.5, 0.0, 0.0))
    shot = ShotMeasurement(geometry=None, ball_found=True, track=flight.track)
    tail = flight.positions_m[-8:]
    assert np.min(tail[:, 2] / np.linalg.norm(tail, axis=1)) > 0.25, "fixture must jump"

    angle_deg, coherence, status = lcmf._tx2_horizontal_proxy(  # pylint: disable=protected-access
        flight.raw, shot, tdm_sign=1
    )

    bearing = np.degrees(np.mean(np.arcsin(tail[:, 1] / np.linalg.norm(tail, axis=1))))
    assert status == "hlcmf_v1_accepted"
    assert coherence > 0.99
    assert angle_deg == pytest.approx(bearing, abs=0.3)
