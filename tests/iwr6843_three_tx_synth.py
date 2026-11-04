"""Three-TX IWR6843LEVM forward model for horizontal (TX2) tests.

Chip conventions follow ``test_iwr6843_pipeline.synth_shot``: the physical
eight-element vertical array is reversed into chip order, TX1 carries the
first four chip elements and TX3 the last four. TX2 sits at the TX1/TX3
vertical midpoint and ``doa.LEVM_TX2_AXIS_BASELINE_LAMBDA`` to the side, so
its residual phase is ``-pi * lateral_cosine`` in the production HLA sign
convention (positive = right of boresight seen from behind the radar).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from openflight.iwr6843 import doa
from openflight.iwr6843.dump import (
    SAMPLE_RANGE_FFT_IQ16,
    SAMPLE_RANGE_FFT_IQ16_WINDOWED,
    pack_dump,
)
from openflight.iwr6843.music import LAM, steer
from openflight.iwr6843.shot import TX2_LOOP_PERIOD_S
from openflight.iwr6843.tracking import RANGE_SPAN_M, BallTrack

RANGE_RES_M = RANGE_SPAN_M / 128
N_RX = 4
# Production wide profile: 9 pre, 7 impact and 8 ball frames of 53 bins.
WIDE_PROFILE_STARTS = (20,) * 9 + (32,) * 7 + (47,) * 8


def channel_matrix(
    vertical_cosine: float,
    lateral_cosine: float,
    *,
    phase_zero_rad: float = 0.0,
    tx_gains: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> np.ndarray:
    """Complex [TX1, TX2, TX3] x 4 RX response of one point target."""
    elevation = float(np.arcsin(vertical_cosine))
    chip = steer(elevation, 8)[::-1]
    middle = steer(elevation, 6)[::-1][:N_RX]
    horizontal = doa.tx2_axis_angle_to_phase_rad(-np.arcsin(lateral_cosine))
    tx2 = middle * np.exp(1j * (horizontal + phase_zero_rad))
    return np.stack((chip[:N_RX], tx2, chip[N_RX:])) * np.asarray(tx_gains)[:, None]


def static_reflector_dump(
    targets: list[tuple[float, float, float, float]],
    *,
    phase_zero_rad: float = 0.0,
    n_loops: int = 12,
    starts: tuple[int, ...] = WIDE_PROFILE_STARTS,
    n_bins: int = 53,
    noise: float = 2.0,
    frame_phase_jitter_rad: float = 0.0,
    seed: int = 0,
) -> bytes:
    """Windowed range-snapshot dump of static point targets.

    ``targets`` holds ``(range_m, vertical_cosine, lateral_cosine, amplitude)``.
    ``frame_phase_jitter_rad`` randomizes TX2 per frame to emulate an unstable
    measurement (people moving, vibrating mount).
    """
    rng = np.random.default_rng(seed)
    n_frames = len(starts)
    cube = np.zeros((n_frames, 3 * n_loops, N_RX, n_bins), dtype=complex)
    for frame, start in enumerate(starts):
        jitter = rng.normal(0.0, frame_phase_jitter_rad) if frame_phase_jitter_rad else 0.0
        for range_m, vertical, lateral, amplitude in targets:
            local = int(round(range_m / RANGE_RES_M)) - start
            if not 0 <= local < n_bins:
                continue
            response = amplitude * channel_matrix(
                vertical, lateral, phase_zero_rad=phase_zero_rad + jitter
            )
            for loop in range(n_loops):
                cube[frame, 3 * loop : 3 * loop + 3, :, local] += response
    cube += noise * (rng.standard_normal(cube.shape) + 1j * rng.standard_normal(cube.shape))
    return pack_dump(
        cube,
        n_tx=3,
        version=4,
        frame_period_us=3000,
        sample_fmt=SAMPLE_RANGE_FFT_IQ16_WINDOWED,
        range_bin_starts=starts,
    )


@dataclass(frozen=True)
class BallFlight:
    """A synthetic three-TX ball dump plus the truth used to build it."""

    raw: bytes
    track: BallTrack
    times_s: np.ndarray
    positions_m: np.ndarray  # [frames, (downrange, lateral, height above radar)]


def ball_flight_dump(  # pylint: disable=too-many-arguments,too-many-locals
    *,
    hla_deg: float,
    vla_deg: float,
    speed_ms: float = 45.0,
    tee_m: tuple[float, float, float] = (1.5, 0.0, -0.13),
    tilt_deg: float = 10.0,
    phase_zero_rad: float = 0.0,
    tx_gains: tuple[float, float, float] = (1.0, 1.0, 1.0),
    n_frames: int = 16,
    n_loops: int = 12,
    frame_period_us: int = 3000,
    amplitude: float = 2000.0,
    noise: float = 0.0,
    seed: int = 0,
) -> BallFlight:
    """Straight-line ball flight from ``tee_m`` (radar at the origin)."""
    rng = np.random.default_rng(seed)
    start_bin = 20
    n_bins = 80
    direction = np.array(
        [
            np.cos(np.radians(vla_deg)) * np.cos(np.radians(hla_deg)),
            np.cos(np.radians(vla_deg)) * np.sin(np.radians(hla_deg)),
            np.sin(np.radians(vla_deg)),
        ]
    )
    tee = np.asarray(tee_m, dtype=float)
    tilt = np.radians(tilt_deg)

    def position(t_s: float) -> np.ndarray:
        return tee + speed_ms * t_s * direction

    flight_s = n_frames * frame_period_us / 1e6
    range_0 = np.linalg.norm(position(0.0)) / RANGE_RES_M
    range_1 = np.linalg.norm(position(flight_s)) / RANGE_RES_M
    slope_bins = (range_1 - range_0) / flight_s
    track = BallTrack(
        speed_ms=slope_bins * RANGE_RES_M,
        slope_bins=slope_bins,
        intercept_bins=range_0,
        rms_bins=0.1,
        n_inliers=n_frames * n_loops,
        t_first=0.0,
        t_last=flight_s,
        low_confidence=False,
    )
    cube = np.zeros((n_frames, 3 * n_loops, N_RX, n_bins), dtype=complex)
    frame_times, frame_positions = [], []
    for frame in range(n_frames):
        frame_start = frame * frame_period_us / 1e6
        frame_times.append(frame_start)
        frame_positions.append(position(frame_start))
        for loop in range(n_loops):
            loop_s = frame_start + loop * TX2_LOOP_PERIOD_S
            local = int(round(track.bin_at(loop_s))) - start_bin
            if not 0 <= local < n_bins:
                continue
            downrange, lateral, height = position(loop_s)
            slant = float(np.linalg.norm((downrange, lateral, height)))
            vertical = (height * np.cos(tilt) - downrange * np.sin(tilt)) / slant
            response = amplitude * channel_matrix(
                vertical,
                lateral / slant,
                phase_zero_rad=phase_zero_rad,
                tx_gains=tx_gains,
            )
            for tx in range(3):
                chirp_s = loop_s + tx * doa.TDM_TAU_S
                doppler = np.exp(1j * 4.0 * np.pi * np.linalg.norm(position(chirp_s)) / LAM)
                cube[frame, 3 * loop + tx, :, local] = response[tx] * doppler
    if noise:
        cube += noise * (rng.standard_normal(cube.shape) + 1j * rng.standard_normal(cube.shape))
    raw = pack_dump(
        cube,
        n_tx=3,
        version=3,
        frame_period_us=frame_period_us,
        sample_fmt=SAMPLE_RANGE_FFT_IQ16,
        range_bin_start=start_bin,
    )
    return BallFlight(raw, track, np.asarray(frame_times), np.asarray(frame_positions))
