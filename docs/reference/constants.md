---
icon: lucide/sliders
---

# Constants

Values compiled into the code rather than passed as flags. Change them by
editing the module, not the command line.

## Capture and FFT

`src/openflight/rolling_buffer/processor.py`

| Constant | Value | Meaning |
| --- | --- | --- |
| `SAMPLE_RATE` | 30,000 | Samples per second |
| `WINDOW_SIZE` | 128 | Samples per FFT window |
| `FFT_SIZE` | 4,096 | Zero-padded FFT size |
| `STEP_SIZE_STANDARD` | 128 | Non-overlapping step |
| `STEP_SIZE_OVERLAP` | 32 | Overlapping step, high resolution |
| `DC_MASK_BINS` | 150 | ~15 mph exclusion zone around DC |
| `MAGNITUDE_THRESHOLD` | 3 | Minimum peak magnitude |
| `MIN_PEAK_SEPARATION_BINS` | 50 | ~5 mph; rejects sidelobe duplicates |
| `MAX_PEAKS_PER_DIRECTION` | 3 | |

`--sample-rate` overrides the rate at runtime. Lowering it lengthens the buffer
but reduces the maximum measurable speed: 25 ksps gives 174 mph over 164 ms,
27 ksps gives 187 mph over 152 ms.

## Ball-track guard

`src/openflight/rolling_buffer/processor.py`, sound and OPS speed trigger only. See
[rolling buffer](../how-it-works/rolling-buffer.md#ball-track-guard).

| Constant | Value | Meaning |
| --- | --- | --- |
| `BALL_GUARD_DROP_MPH` | 8.0 | Speed a club-like track loses ... |
| `BALL_GUARD_DROP_HORIZON_MS` | 40.0 | ... within this time of its first link |
| `BALL_GUARD_SLOPE_MPH_PER_MS` | −0.30 | And its least-squares slope is at or below this |
| `BALL_GUARD_LINK_MPH` | 6.0 | Link tolerance: max(this, ... |
| `BALL_GUARD_LINK_FRACTION` | 0.08 | ... this × the previous link's speed) |
| `BALL_GUARD_LINK_MAX_MISSES` | 2 | Windows in a row without a link that end the track |
| `BALL_GUARD_SMASH_FLOOR` | 0.90 | Smash below this fires the guard (not for PW/GW/SW/LW or an unknown club) |
| `BALL_GUARD_REPLACE_MIN_RATIO` | 1.10 | A replacement is at least this × the voted ball |
| `BALL_GUARD_REPLACE_SMASH` | 1.00–1.50 | Its smash range (inclusive; 1.50 is `MAX_SMASH_FACTOR`) |
| `BALL_GUARD_REPLACE_SMASH_WEDGE` | 0.75–1.45 | The same for PW/GW/SW/LW |
| `BALL_GUARD_UNCAPPED_PEAKS` | 12 | Peaks per window when the guard reads the windows again |

## Radar physics

| Constant | Value | Meaning |
| --- | --- | --- |
| `WAVELENGTH_M` | 0.01243 | 24.125 GHz |
| `MPS_TO_MPH` | 2.23694 | |
| `ADC_RANGE` | 4,096 | 12-bit ADC |
| `VOLTAGE_REF` | 3.3 | Reference voltage |

At 24.125 GHz, 1 mph produces roughly a 71.7 Hz Doppler shift.

## Club extraction

| Constant | Value |
| --- | --- |
| `CLUB_BRANCH_HISTORY_MS` | 30.0 |
| `CLUB_BALL_ONSET_SEARCH_MS` | 10.0 |
| `CLUB_BALL_MATCH_MIN_MPH` | 4.0 |
| `CLUB_BALL_MATCH_FRACTION` | 0.06 |
| `CLUB_PLATEAU_LOOKBACK_MS` | 18.0 |
| `CLUB_PLATEAU_GUARD_MS` | 4.0 |
| `CLUB_PLATEAU_QUANTILE` | 0.70 |
| `CLUB_BALL_CONTAMINATION_RATIO` | 0.95 |
| `CLUB_TERMINAL_START_MS` | −2.5 |
| `CLUB_TERMINAL_END_MS` | 1.0 |
| `CLUB_MAX_PLAUSIBLE_SPEED_MPH` | 150.0 |

## Spin detection

Amplitude-envelope demodulation. See
[rolling buffer & spin detection](../how-it-works/rolling-buffer.md).

| Constant | Value | Meaning |
| --- | --- | --- |
| `SPIN_BANDPASS_BW_HZ` | 700 | ±700 Hz around ball Doppler |
| `SPIN_BANDPASS_ORDER` | 4 | Butterworth order |
| `SPIN_ENVELOPE_FFT_SIZE` | 8,192 | Zero-padded envelope FFT |
| `SPIN_MIN_SEAM_HZ` | 33.0 | ~2,000 RPM floor |
| `SPIN_MAX_SEAM_HZ` | 200.0 | 12,000 RPM ceiling |
| `SPIN_MIN_SAMPLES` | 600 | ~20 ms minimum ball signal |
| `SPIN_SNR_HIGH` | 8.0 | High-confidence threshold |
| `SPIN_SNR_MEDIUM` | 5.0 | Medium-confidence threshold |
| `SPIN_SNR_MIN` | 2.5 | Minimum to report at all |
| `SPIN_AUTOCORR_MIN` | 0.3 | Minimum normalised correlation |
| `SPIN_MIN_CYCLES` | 2 | Minimum seam cycles |
| `SPIN_DC_LEAKAGE_BINS` | 1 | Low bins zeroed |
| `SPIN_LOW_BAND_SUSPECT_MAX_RPM` | 3,100.0 | |
| `SPIN_DETREND_POLY_ORDER` | 3 | |
| `SPIN_SIGNAL_LOSS_SMOOTH_SAMPLES` | 90 | ~3 ms moving average |

`SPIN_CONFIDENCE_HIGH` (0.7, in `launch_monitor.py`) is the threshold above
which measured spin is trusted for physics.

## Ballistics

`src/openflight/ballistics.py` — see
[ballistics & carry](../how-it-works/ballistics.md) for the model and its
references.

| Constant | Value |
| --- | --- |
| `BALL_MASS_KG` | 0.04593 |
| `BALL_RADIUS_M` | 0.02135 |
| `AIR_DENSITY_STD` | 1.225 |
| `CD_POLY` | (0.1304, 0.9287, -0.8259) |
| `CL_POLY` | (0.0504, 1.2031, -1.1490) |
| `SP_FIT_MAX` | 0.75 |
| `SPIN_DECAY_RATE` | 0.04 |
| `GRAVITY` | 9.81 |
| `DT_SECONDS` | 0.002 |
| `MAX_FLIGHT_SECONDS` | 15.0 |
| `SAMPLE_INTERVAL_S` | 0.05 |
| `RANGE_BALL_DRAG_RE_KNEE` | 1.2e5 |
| `RANGE_BALL_DRAG_RE_SCALE` | 1e5 |
| `AIR_DYNAMIC_VISCOSITY_PA_S` | 1.81e-5 |
| `CARRY_VARIANT_DT_SECONDS` | 0.01 |

## Roll-out

`src/openflight/rollout.py` — Penner (2002), "The run of a golf ball",
*Can. J. Phys.* 80: 931–940. See
[ballistics & carry](../how-it-works/ballistics.md#roll-out) for the model.

| Constant | Value | Source |
| --- | --- | --- |
| `NORMAL_FAIRWAY.mu` | 0.40 | Kinetic friction ball–turf; Penner (2002), after Daish |
| `NORMAL_FAIRWAY.rolling_friction` | 0.131 | $\rho$ in $a = -\tfrac{5}{7}\rho g$; Penner (2002), who matched drive runs with it (Fig. 5) |
| `NORMAL_FAIRWAY.critical_bounce_height_m` | 0.005 | A bounce below 5 mm ends in a roll; Penner (2002) |
| `RESTITUTION_POLY` | (0.510, −0.0375, 0.000903) | Restitution over the normal impact speed; Penner (2002) |
| `RESTITUTION_POLY_MAX_MPS` | 20.0 | End of the polynomial; Penner (2002) |
| `RESTITUTION_FAST` | 0.120 | Restitution above 20 m/s; Penner (2002) |
| `CRATER_ANGLE_REF_RAD` | 15.4° | Haake's reference crater, Penner's eq. (8) |
| `CRATER_SPEED_REF_MPS` | 18.6 | Its impact speed |
| `CRATER_IMPACT_ANGLE_REF_RAD` | 44.4° | Its impact angle from the vertical |
| `MAX_IMPACT_SPEED_MPS` | 40.0 | OpenFlight: past it the linear crater fit turns low liners around |
| `MAX_ROLL_POINTS` | 25 | OpenFlight: points in a shot's `roll_path`, which stays under 1 KB |
| `MAX_IMPACTS` | 100 | OpenFlight: safety cap on bounces; Penner's shallow drive takes 43 |

## Thresholds

| Value | Setting |
| --- | --- |
| Minimum ball speed | 15 mph (35 mph in speed-triggered mode) |
| Trigger latency, `sound` | ~10 µs |
| Trigger latency, `speed` | ~5–6 ms |
| Rolling-buffer dump | 40,556 bytes |
| K-LD7 OS-CFAR threshold factor | 8.0 *(deprecated hardware)* |

## Related

- [CLI flags](cli.md) — what can be changed at runtime
- [Configuration files](configuration.md) — settings held in files
