# Rolling Buffer Capture and Spin Detection

OpenFlight uses the OPS243-A rolling buffer for production shot capture. Spin
estimation from that capture remains experimental.

## Capture pipeline

1. The SEN-14262 detects impact and triggers the OPS243-A through `HOST_INT`.
2. The radar freezes and returns 4,096 I samples and 4,096 Q samples.
3. `RollingBufferProcessor` extracts ball and club speed from short FFT windows.
4. An overlapping timeline separates the club, impact, and ball regions.
5. The [ball-track guard](#ball-track-guard) checks that the voted ball speed is
   the ball, not the club head's follow-through.
6. An ungated multitaper estimator records an experimental spin candidate.

The normal kiosk command uses this pipeline:

```bash
scripts/start-kiosk.sh
```

With `--shot-trigger ops-speed-shadow` or `ops-speed` (off by default) the
OPS243-A firmware 1.3.3 speed trigger starts step 2 instead: the radar trips its
own buffer when an outbound reading passes `ST` and raises J3 pin 1, which the
Pi reads on GPIO22. The pin edge takes the microphone edge's place for the
camera ring, the trigger gate record (no HOST_INT pulse) and, in `ops-speed`,
the TI `l3dump` (25 ms later). Club speed anchors 28 ms after the trip, or at a
microphone edge that came 0–60 ms after it in the shadow mode. Both timings are
model values to recalibrate in M3 from the logged microphone-minus-pin delay.
See [OPS speed trigger](../build/ops-speed-trigger.md) and the
[CLI flags](../reference/cli.md#shot-trigger-ops243-a-133-speed-trigger).

## One-time radar setup

The OPS243-A firmware changes the `HOST_INT` behavior when rolling-buffer mode
is entered at runtime, so the mode has to be saved to flash once and the radar
power-cycled — see **[Rolling buffer setup](../setup/rolling-buffer.md)**.

The runtime then starts in the persisted `GC` rolling-buffer mode without
re-entering it. See [Sound trigger wiring](../build/sound-trigger.md) for the
recommended direct hardware trigger.

## Current defaults

| Setting | Value |
|---|---:|
| OPS243-A mode | Persisted rolling buffer (`GC`) |
| Sample rate | 30,000 samples/s |
| Capture | 4,096 I + 4,096 Q samples |
| Capture duration | About 136.5 ms |
| Pre-trigger split | 16 of 32 blocks |
| Speed window | 128 samples |
| FFT size | 4,096 |
| Overlap step | 32 samples (937.5 windows/s) |
| Production trigger | SEN-14262 `GATE` to OPS243-A `HOST_INT` |

Use `--buffer-split post-heavy` when more post-impact data is needed, or
`--buffer-split pre-heavy` when the club approach matters more:

```bash
scripts/start-kiosk.sh --buffer-split post-heavy
```

## Ball-track guard

With the sound trigger (and the OPS speed trigger) the ball speed is voted only over
the windows after impact: the fastest speed repeated within 1 mph in windows one or
two apart. Each window keeps its 3 strongest peaks (`MAX_PEAKS_PER_DIRECTION`). When
the ball is weaker than three other returns in most windows, it can lose its partner,
and the vote takes the club head's follow-through instead. On 2026-11-02 a 150 mph
drive (Toptracer) read 60.8 mph that way: its ball survived in one window only.

A ball keeps its speed (drag takes about 1.7 mph in 40 ms from a 150 mph drive at
Munich's air density); a club head in the follow-through does not. So right after
the vote, before the impact estimate, the spin search and the TI see the ball
speed, the guard follows the voted ball forward window by window (each link within
max(6 mph, 8 %) of the previous one; two windows without a link end the track):

- **Club-like track:** at least 3 links, at least 8 mph lost within 40 ms and a
  least-squares slope at or below −0.30 mph/ms. Good shots lose at most 5.9 mph
  (slope ≥ −0.24 mph/ms); the club-like tracks of the corpus lose 9.7–18.4 mph.
- **Implausible smash:** with a club speed and not a wedge (PW/GW/SW/LW), a smash
  below 0.90. Good non-wedge shots read at least 1.01.

When a rule fires, the post-impact windows are read again with up to 12 peaks each.
Their fastest track at least 10 % above the voted ball replaces it if it is not
club-like itself and its smash lies in 1.00–1.50 (wedges 0.75–1.45). Without such a
track the number is kept and the shot is flagged `uncertain`: it stays out of the
session averages, the screens mark its ball speed, smash and carry with ≈ and say
"Ball speed uncertain: the radar barely saw the ball." Simulators still get it; the
shot is never rejected. A replaced ball speed gets no hint on the screens.

Over 345 replayed range and TrackMan captures the guard fires on four: the drive
above is replaced (148.3 mph), a 54° wedge without a ball echo and two swings
without a ball are flagged; none of the 321 good shots changes. The re-read costs
1–2.5 ms on an M-series Mac, only when a rule fires (under 0.1 ms otherwise). Every
decision is logged in `rolling_buffer_capture` (`ball_guard`, see the
[session log](../reference/session-log.md#ball-track-guard-fields)) so range sessions
can validate it.

## Experimental spin output

Every valid capture runs the ungated multitaper estimator. Its candidate:

- is not shown as the shot's spin in the UI, which shows the spin the carry
  used and marks it "typical" when it is the club-typical value; the raw
  candidate appears only in the debug tab, on the analysis page ("unconfirmed
  spin candidate") and in the CSV export;
- is logged as `spin_method: "multitaper_ungated"`;
- is constrained to 1,500–11,000 RPM; and
- while unconfirmed (confidence below 0.7), drives neither spin-adjusted carry
  nor the estimated launch angle.

Treat individual values as evaluation data, not ground truth. Multipath and
capture geometry can produce plausible but incorrect candidates, and current
blind validation is not accurate enough for production carry calculations.

For offline comparison and estimator work, use
[Spin Replay and Diagnostics](../development/spin-replay.md). Raw captures are stored
in the session JSONL logs described in the
[observability guide](../using/observability.md#session-log-format).

## Related guides

- [Raspberry Pi Setup](../setup/raspberry-pi.md)
- [Sound Trigger Wiring](../build/sound-trigger.md)
- [OPS speed trigger](../build/ops-speed-trigger.md)
- [Spin Replay and Diagnostics](../development/spin-replay.md)
