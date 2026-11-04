---
icon: lucide/terminal
---

# CLI Flags

Every flag accepted by the server, grouped by subsystem.

`scripts/start-kiosk.sh` accepts most of these and forwards them. Use
`--dry-run` to print the exact command the script would run:

```bash
scripts/start-kiosk.sh --iwr6843 --dry-run
```

!!! note "Generated from the source"

    This table is derived from the argparse definitions in
    `src/openflight/server.py`. If a flag here disagrees with the code, the
    code is right — please open an issue.

## Server & web

Binding, ports, and debug output.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--mock`, `-m` | flag | Run in mock mode without radar |
| `--mock-swing-speed` | flag | Run swing speed training mode with simulated reps and no OPS radar |
| `--host` | default `0.0.0.0` | Host to bind to (default: 0.0.0.0) |
| `--web-port` | int; default `8080` | Web server port (default: 8080) |
| `--startup-status-file` | path | Write structured initialization progress for the optional kiosk splash |
| `--debug`, `-d` | flag | Enable verbose FFT/CFAR debug output |
| `--radar-log` | flag | Log raw radar data to console (Python logging) |
| `--show-raw` | flag | Show raw radar readings in console (signed values) |

## OPS243 radar & transport

Serial port, baud, and sample rate.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--port`, `-p` | — | Serial port for radar |
| `--ops-baud` | int | — |
| `--ops-fast-rearm` | flag | Re-arm the OPS after each dump with a 50 ms drain gap and 50 ms command settles instead of 200/100/100/150 ms (about 0.25 s less blind time per dump; the re-arm returns 0.35 s sooner, and the radar is armed from the final PA write). Off by default until validated on the hardware; the timings used are logged in the session-start config (`ops_rearm`). |
| `--sample-rate` | int; default `30` | Radar sample rate in ksps (default: 30). Lower = longer buffer but lower max speed. 25=174mph/164ms, 27=187mph/152ms |

## Trigger & capture

How a capture is initiated and framed.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--trigger` | choices: `sound`, `speed`; default `sound` | Trigger strategy |
| `--sound-pre-trigger` | int; default `16` | Pre-trigger segments S#n, 0-32 (default: 16 = 50/50 split, each segment ~4.27ms at 30ksps) |

## Shot trigger (IWR6843 self-trigger)

What starts a shot. The self-trigger needs IWR6843 firmware built from the
upstream `feat/iwr-self-trigger` sources with `cb8fbff` or later: the released
`v1.0.3` image requests a second freeze on `l3dump` after a self-fire and
leaves the board latched. The firmware reports a fire only as the CLI line
`Triggered` on the TI UART; the host reads it, stamps it on the monotonic and
wall clocks, and (in `ti`) treats it as the shot edge. See
`src/openflight/iwr6843/self_trigger.py`.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--shot-trigger` | choices: `sound`, `ti`, `ti-shadow`, `ops-speed-shadow`, `ops-speed`; default `sound` | The two `ops-speed*` modes use the OPS243-A firmware 1.3.3 speed trigger, see [below](#shot-trigger-ops243-a-133-speed-trigger). `sound`: the microphone edge on `--iwr6843-trigger-pin` drives OPS, camera and the TI dump, as before; no `triggerCfg` is sent. `ti`: no microphone. Each self-trigger notice is the shot edge: the trigger gate pulses OPS HOST_INT at once (same pulse, passthrough or camera gate as configured), the camera ring freezes in software, `triggerLog track` names the firing rule, and `l3dump` streams the ring the board froze itself (no second freeze); a notice the host does not accept (disarmed, OPS dump in progress, ringing filter) is released with `l3release`. Needs `--iwr6843` and `--ops-trigger-pin`. `ti-shadow`: the microphone drives everything exactly as in `sound`; the board's self-trigger runs alongside and every notice is paired with the microphone edges in `ti_self_trigger` session-log entries (matched, would-be false fire, would-be miss). A latched ring no edge claims within 40 ms is released |
| `--ti-pre-trigger` | int; default `9` | `ti` only: OPS pre-trigger segments S#n, replacing `--sound-pre-trigger` (S#9 at 20 ksps = 57.6 ms before / 147.2 ms after HOST_INT). Refused with an error (warning) when the slowest fire (`worst` in `--ti-fire-latency-ms`) or the longest anchor lead, plus the camera deadline in camera mode, leaves less than the minimum (full) club history. At offset 0 the slowest fire needs 37.5 ms (S#6 would just hold it); S#9 keeps a margin and also holds the +0.2 m fallback (46.5 ms) unchanged |
| `--ti-fire-latency-ms` | `RULE=MS,...`; default `crossing=-1.7,end=1.57,leave=-0.48,unknown=-0.57,worst=9.52` | `ti` only: board fire minus impact per firing rule, the ~3 ms board decision included. Defaults: medians of the replay of the fork image `openflight_iwr6843_v1.0.3-of1.bin` (v1.0.3 + cb8fbff + candidate club-track acquisition; `analysis/iwr-self-trigger-20261031/data/variants/E6_acq_conf02_step125.csv`, own dumps, ball ≥ 60 mph, offset 0: crossing n 61, end n 36, leave n 18; `unknown` = pooled median, n 115; `worst` = slowest fire, a leave at +9.52 ms). `scripts/analysis/iwr6843_fire_latency_defaults.py <csv>` reproduces them. The club-speed anchor goes where the microphone edge would have been: notice − 1 ms notice latency − fire latency + 4.4 ms sound delay (negative leads put it after the notice). `unknown` applies when `triggerLog track` gives no rule; `worst` only sizes the `--ti-pre-trigger` check |
| `--ti-shadow-holdoff-ms` | float 0-40; default `0` | `ti-shadow` only: after a microphone edge, let the board fire for up to this long before the sound-path `l3dump` freezes the ring. 0 keeps today's timing, but a fire after the edge is then pre-empted (logged as `edge_without_notice` with `ti_listen_ms` ≈ 0); a longer hold-off measures late fires but shifts the sound-path TI ring later by up to that much when the board does not fire |
| `--iwr6843-self-trigger-offset-m` | float; default `0` | Self-trigger: watched bin = ball bin − 2 + this offset in whole 46.9 mm bins ("lead2" at 0). Within ±1 m. See the offset note below the table |
| `--iwr6843-self-trigger-snr` | float; default `1` | Self-trigger: club-target threshold over the board's noise floor (minimum 1) |
| `--iwr6843-self-trigger-bin` | int | Self-trigger: explicit global range bin for `triggerCfg`, instead of the one computed from `--iwr6843-tee-m`. Must lie in the cfg's first capture window |

**Trigger bin.** `--iwr6843-tee-m` stays the LCMF's antenna-to-ball slant
range. The trigger bin, though, follows the convention the firmware evidence was
replayed at (the analysis scripts place the owner's ball at "tape ball bin 33
(1.5 m + 30 mm)", like upstream's front tape plus its 30 mm array depth): ball
bin = `round((tee + 0.030) / 0.046875)`, watched bin = ball bin − 2 + offset.
Only then do the replayed fire rates and latencies above apply. The radar's
range coordinate is biased anyway: an independent study
(`analysis/ti-selbstneigung-20261031`) sees the resting ball at bins 34–35
(1.59–1.63 m) for the 1.50 m tape. For the owner's `--iwr6843-tee-m 1.50` the
host sends **`triggerCfg 31 1 1`** (offset 0), or `triggerCfg 35 1 1` with
`--iwr6843-self-trigger-offset-m 0.2`. The session-start config records
`self_trigger.tee_bin` and `self_trigger.ball_bin`.

**Offset 0 against +0.2 m.** In the replay of the of1 image, offset 0 ("lead2", 2 bins short of the ball) fired in the window on 44 of 46 drivers and 27 of 34 TrackMan swings; +0.2 m ("off+4", 2 bins past the ball) on 41 of 46 and only 6 of 34, many 3 frames late. Upstream nevertheless moved its rig to +0.2 m because backswings and waggles fired at the shorter bin; replays of microphone-triggered dumps cannot show such fires. The range shadow test (`--shot-trigger ti-shadow`, `notice_without_edge` entries) decides. If waggles or backswings fire there, the fallback is

```bash
--iwr6843-self-trigger-offset-m 0.2 \
--ti-fire-latency-ms crossing=2.88,end=3.9,leave=1.19,unknown=2.91,worst=18.45
```

(the off+4 rows of the same CSV: crossing n 75, end n 24, leave n 13, pooled n 112, slowest a leave at +18.45 ms; `--ti-pre-trigger 9` still holds it).

## Shot trigger (OPS243-A 1.3.3 speed trigger)

Mic-free shot trigger "B2", off by default. OPS243-A firmware 1.3.3 trips its own
rolling buffer when an outbound reading passes `ST` (mph) with at least `SM`
magnitude, and holds J3 pin 1 HIGH until the sampling has completed. Pin 1 goes
to a Pi input (`--ops-speed-pin`, GPIO22 = header pin 15). The host sends
`PI`, `US`, `P3`, `S=<ksps>`, `SM<n>`, `ST-<mph>`, `S#<n>` and `??` (raw reply
logged) on the persisted rolling-buffer mode, never `GC`, `GS` or `A!`, and
arms the board with the first re-arm (`PA` / `S#n` / `PA`). Wiring and the
M1-M5 steps: [OPS speed trigger](../build/ops-speed-trigger.md). See
`src/openflight/ops_speed.py`.

| | `ops-speed-shadow` (M3) | `ops-speed` (M4) |
| --- | --- | --- |
| OPS dump | the board's speed trip; a HOST_INT pulse only for a microphone edge with no trip in the 60 ms before it (fallback, needs HOST_INT to work on 1.3.3) | the board's speed trip; BCM27 stays claimed LOW, never pulsed |
| Pin 1 | gate record, camera ring, witness (a pin edge within 5 ms after a fallback pulse is that pulse's own dump, in case 1.3.3 raises pin 1 on HOST_INT dumps too) | drives TI, camera ring and gate record |
| TI `l3dump` | at the microphone edge, as today | `--ops-speed-ti-delay-ms` after the pin edge |
| Club-speed anchor | the microphone edge that claimed the trip; a trip no edge claimed: trip + `--ops-speed-anchor-ms` | trip + `--ops-speed-anchor-ms` |
| Shot | only a dump whose trip a microphone edge claimed (0-60 ms after it), or a fallback dump; any other dump becomes `ops_speed_unmatched` (full I/Q logged, no shot, no clock sync). That includes a trip pin 1 never showed (loose pin 1): its dump matches the later fallback pulse with a first-byte lag below 1 ms, which no HOST_INT dump has (`ops_speed_dump` `self_trip`), and is anchored trip + `--ops-speed-anchor-ms`, not at the pulse | every dump; the microphone is a witness only |
| TI self-trigger | off | off |

**Refusal.** Before any GPIO or OPS setup the server connects the OPS once and
reads its firmware version. Below 1.3.3, outside the 1.3 train, an unparsable
or missing version, an unknown off command (`ops243.SPEED_TRIGGER_OFF_COMMANDS`
is empty until M1 finds it) or pin 1 reading active at idle refuse the speed
trigger: one ERROR line `[OPS-SPEED] <reason>: speed trigger refused, staying
on the microphone (--shot-trigger sound)`, the session runs as `--shot-trigger
sound` with `--sound-pre-trigger` restored, and `session_start`
`config.ops_speed` records `refused`. Until M1 fills in the off command the
modes therefore always refuse.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--ops-speed-pin` | int; required | BCM input wired to J3 pin 1, e.g. `22`. 0-27, different from `--iwr6843-trigger-pin` and `--ops-trigger-pin`. Measure the pin's level before wiring it |
| `--ops-speed-pin-active` | `high`/`low`; default `high` | Pin 1 level while the trigger holds it (the vendor README says high; M1 confirms) |
| `--ops-speed-pin-pull` | `down`/`up`/`off`; default derived | `down` for an active-high pin, `up` for active-low, `off` for a push-pull driver. A pull towards the active level is refused |
| `--ops-speed-debounce-ms` | float 0-5; default `0` | The pin must still be active this long before its edge is fanned out, with its original time. No gpiozero `bounce_time`: it delays delivery |
| `--ops-speed-holdoff-ms` | float; default `100` | Ignore pin edges this long after one (like the 100 ms ringing filter) |
| `--ops-speed-threshold-mph` | int 1-200; default `25` | Trip speed, sent as `ST-25` (outbound) after `US` |
| `--ops-speed-magnitude` | int 1-2000; required | `SMnn` magnitude gate. Not our FFT magnitude: only the device can tell a working value (M1/M3) |
| `--ops-speed-pre-trigger` | int; default `2` | OPS split S#n around the trip, replacing `--sound-pre-trigger` (S#2 at 20 ksps = 12.8 ms before / 192.0 ms after). Club history before the anchor = pre-trigger span + anchor: error below 20.4 ms, warning below 31.4 ms; `S#0` is an error. In the shadow a fallback dump keeps only the pre-trigger span before HOST_INT (12.8 ms at S#2), which the server warns about |
| `--ops-speed-ti-delay-ms` | float 0-40; default `25` | `ops-speed` only: TI `l3dump` this long after the pin edge (`iwr6843_capture` logs `l3dump_target_ms` / `l3dump_actual_ms`) |
| `--ops-speed-anchor-ms` | float; default `28` | Club-speed anchor this long after the trip |
| `--ops-speed-witness-window-ms` | `LOW,HIGH`; default `0,60` | Microphone minus pin edge that counts as the same swing |
| `--ops-speed-reject-practice-swings` | flag | Reject captures the practice-swing rule calls `practice_swing` (no club-to-ball jump in the OPS data and smash < 1.05). **Off by default:** on 197 real shots (19.10., 30.10., 02.11.) it already drops 2, against the M3 criterion of dropping none. The verdict is logged for every capture either way |
| `--ops-speed-stall-mic-edges` | int 0-50; default `3` | `ops-speed` only: after this many consecutive microphone edges without a trip while the OPS was armed (witness `mic_without_pin`, `ops_state` `armed`), re-arm the OPS once (PA path; `trigger_event` reason `ops_speed_stall_rearm`). 0 turns it off. Without it only the 300 s long-stall re-arm would catch a board stuck idle. **Neighbours count:** the microphone hears every impact on the range, and any N of them between two of your swings (while the OPS is armed) make a streak, so each streak re-arms a working board too (drain, `PA` / `S#n` / `PA`, about 0.1-0.6 s blind). Edges from the trip until its dump can arrive (post-trigger span + 60 ms) and during the dump and re-arm count as `busy` and break the streak. In M4 count the `ops_speed_trigger` `stall_rearm` entries against the `trigger_event` entries (each dump re-arms once anyway); if stall re-arms are frequent, raise N or set it to 0 |

**Timing defaults are model values.** The TI delay (25 ms) and the anchor
(28 ms) come from the 2026-10-03 shots at transmit power P0 and 30 ksps; B2 runs
at P3 and 20 ksps. Recalibrate both in M3 from the logged microphone-minus-pin
delay (`ops_speed_trigger` `matched` entries, `delta_ms`; per capture
`ops_speed_mic_delta_ms`): set the TI delay to its median and choose the anchor
by the same criterion as the model (impact inside the club-speed search span).

The speed-trigger modes need `--ops-trigger-pin` (BCM27 stays claimed LOW; the
shadow fallback pulses it) and `--ops-trigger-mode passthrough`, and refuse
`--mock`, `--swing-speed`, `--trigger speed` and the TI self-trigger flags. They
do not need `--iwr6843`. Every `--ops-speed-*` flag needs one of the two modes.
Manual OPS recovery works in both: it re-sends the speed-trigger setup and
re-arms.

## IWR6843 angle radar

The supported angle radar.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--iwr6843` | flag | Enable TI IWR6843 L3 capture and LCMF-v1 vertical launch angle |
| `--iwr6843-port` | — | TI serial port (auto-detect by default) |
| `--iwr6843-config` | default `config/iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg` | TI RF config matching the flashed L3 firmware |
| `--iwr6843-cal` | default `config/iwr6843_calibration_reference.json` | TI complex array/range calibration JSON |
| `--iwr6843-trigger-pin` | int; default `17` | BCM GPIO receiving the shared sound-trigger edge (default: 17) |
| `--iwr6843-tee-m` | float; default `1.575` | Antenna-center to tee slant range in metres (default: 1.575) |
| `--iwr6843-net-m` | float; default `4.6` | Antenna-center to net range in metres (default: 4.6) |
| `--iwr6843-tilt-deg` | float | Override mount tilt from the TI calibration JSON; a saved phone tilt calibration outranks it |
| `--iwr6843-radar-height-m` | float | Override antenna-center height from the TI calibration JSON |
| `--iwr6843-ball-height-m` | float; default `0.04` | Ball-center height above the floor/mat (default: 0.040) |
| `--iwr6843-tx-order` | choices: `auto`, `normal`, `reversed`; default `auto` | TI TDM chirp order; auto reads the chirp masks from the cfg |
| `--iwr6843-capture-timeout` | float; default `16.0` | Maximum seconds an OPS shot waits for its TI UART dump |
| `--iwr6843-output-dir` | — | Raw TI dump directory when --debug is enabled (default: <session-log-dir>/iwr6843) |
| `--iwr6843-azimuth-offset-deg` | float | Azimuth of the radar boresight relative to the target line, in degrees. Positive means boresight points right of the target line. Added to the measured club path; 0 reports club path relative to boresight. |
| `--iwr6843-horizontal-phase-reference-rad` | float | Static target-line phase measured by horizontal aim calibration. Subtracted from the TX2 horizontal proxy before angle conversion. |
| `--iwr6843-horizontal-trajectory-fit` | flag | Report horizontal launch from the lateral ball path anchored at the tee instead of the radar bearing. Default off; needs a calibrated `--iwr6843-horizontal-phase-reference-rad`. |
| `--iwr6843-tee-lateral-m` | float; default `-0.072` | Signed lateral ball position relative to the TI antenna centre, positive = right seen from behind the radar. Used only by the trajectory fit. |

## Inclinometer

LIS3DH enclosure tilt compensation.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--inclinometer` | flag | Enable LIS3DH enclosure pitch compensation for IWR6843 tilt |
| `--inclinometer-zero-offset` | float | Degrees added to raw LIS3DH pitch (default: 0) |

## Ballistics & spin

Carry model and spin handling.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--ballistics` | flag | Use the physics-based carry simulator (drag + Magnus, RK4). This is the default; shots without a vertical launch angle fall back to the legacy table estimator. |
| `--no-ballistics` | flag | Disable the physics simulator and use the legacy carry table for all shots. |
| `--typical-spin-scale` | float, default 1.0 (0.5-1.2) | Multiply the club-typical spin of irons and wedges (`IRON_*`, PW/GW/SW/LW) that the ballistic carry uses when no trusted spin was measured (and the typical spin the UI shows). Driver, woods, hybrids and unknown clubs keep the table spin; trusted measured spin, the simulator export spin and the table fallback's speed-based spin are unchanged. Logged in the session-start config (`typical_spin_scale`, `typical_spin_scale_clubs`). |
| `--typical-spin-table` | `tour` \| `range-ball`, default `tour` | Club-typical spin used for the ballistic carry when no trusted spin was measured: `tour` = TrackMan PGA Tour averages (`CLUB_PHYSICS`), `range-ball` = this user's Toptracer range-ball spin (`RANGE_BALL_SPIN_RPM`, TT medians of 2026-10-02 to 2026-11-02, the rest interpolated by loft). `--typical-spin-scale` applies to either table; trusted measured spin and the simulator export spin are unchanged. Logged in the session-start config (`typical_spin_table`). This is the device default: a profile's **Ball type** in the app (menu → Calibrate; `standard` = `tour`, `range` = `range-ball`) overrides it for that profile's shots, and a profile that never chose keeps this value. See [ballistics](../how-it-works/ballistics.md#inputs-and-fallbacks). |
| `--range-ball-drag-slope` | float, default 0.0 = off (0.0-1.0) | **Unvalidated.** Above Reynolds number 1.2e5 the drag coefficient of every ballistic flight (live carry, export carry, carry span) grows by the slope per 1e5, so fast drives fly shorter and wedges are unchanged. The Toptracer study's fit is 0.3546 (typical drive −10.7 m), which failed its peak/hang-time check. Logged in the session-start config (`range_ball_drag_slope`). See [ballistics](../how-it-works/ballistics.md#range-ball-drag-opt-in-unvalidated). |
| `--air-density` | float, default `1.184` (TrackMan 'Flat', sea level 77 °F) | Fallback site air density in kg/m³ for the ballistic carry. A phone (Open-Meteo) or manual weather reading from Settings → Weather replaces it while the reading is at most 3 h old, even when this flag is set; the simulator export (`CarryDistance`) always flies with this value. Logged per shot (`carry_air_density_*` in [session-log.md](session-log.md)). |
| `--calculated-spin` | flag | Replace radar-measured spin with the kinematic estimate (170*v*sin(LA)^1.2) when the launch angle was measured. The 24 GHz OPS return carries no usable spin line (see src/openflight/spin_estimate.py); the measured value is kept in spin_rpm_measured for offline scoring |

## Swing speed

Club-only training mode.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--swing-speed` | flag | Run club-only swing speed training mode (no impact or ball required) |
| `--swing-speed-threshold` | float; default `30.0` | Outbound speed threshold that starts a swing speed rep (default: 30 mph) |
| `--swing-speed-max` | float; default `130.0` | Maximum plausible swing speed accepted from OPS reports; use 0 to disable (default: 130 mph) |
| `--swing-speed-min-readings` | int; default `3` | Minimum qualifying radar readings required to count a swing speed rep (default: 3) |
| `--swing-speed-single-peak` | float; default `60.0` | Peak speed that can count as a swing from one radar reading (default: 60 mph) |
| `--swing-speed-num-reports` | int; default `8` | Number of OPS speed candidates to report per sample cycle (default: 8) |
| `--swing-speed-end-ms` | float; default `1000.0` | Milliseconds below threshold before ending a swing speed rep (default: 1000) |
| `--swing-speed-cooldown-ms` | float; default `750.0` | Cooldown after a swing speed rep before accepting another (default: 750) |
| `--swing-speed-rejected-cooldown-ms` | float; default `100.0` | Cooldown after an ignored short motion before re-arming (default: 100) |

## Logging & session data

Where session logs go and what they capture.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--session-location`, `-l` | default `range` | Location identifier for session logs (e.g., 'range', 'course', 'home') |
| `--log-dir` | — | Directory for session logs (default: ~/openflight_sessions) |
| `--profiles-path` | path | Profile store (default: `OPENFLIGHT_PROFILES_PATH` or `~/.config/openflight/profiles.json`) |
| `--weather-path` | path | Stored range location for the weather (default: `OPENFLIGHT_WEATHER_PATH` or `~/.config/openflight/weather.json`) |
| `--no-logging` | flag | Disable session logging |

## Simulators & power

Outbound connectors and battery status.

| Flag | Type / default | Description |
| --- | --- | --- |
| `--battery` | — | Show battery and external-power status using the selected provider |
| `--sim` | flag | Enable simulator connectors from config/sim.json (GSPro / OpenGolfSim / PAR-TEE). Off by default. |

## High-speed camera capture

Optional rolling-buffer capture and replay. See [camera setup](../camera/README.md).

| Flag | Type / default | Description |
| --- | --- | --- |
| `--camera-capture` | flag | Enable high-speed rolling-buffer capture and replay |
| `--camera-capture-width` | int; default `640` | Capture width |
| `--camera-capture-height` | int; default `400` | Capture height |
| `--camera-capture-fps` | float; default `300` | Capture frame rate |
| `--camera-capture-pre-ms` | float; default `150` | Milliseconds retained before the trigger |
| `--camera-capture-post-ms` | float; default `50` | Milliseconds retained after the trigger |
| `--camera-capture-exposure-us` | int; default `1000` | Exposure seed for startup calibration |
| `--camera-capture-gain` | float; default `4.0` | Analogue-gain seed for startup calibration |
| `--camera-capture-mount-height-m` | float; default `0.20955` | Camera optical-center height above the hitting surface |
| `--camera-capture-horizontal-offset-deg` | float; default `0` | Target-line correction added to horizontal launch angles; a target tapped in the Camera tab replaces it ([camera](../camera/README.md#target-line-by-tap)) |
| `--camera-capture-lateral-offset-m` | float; default `0` | Camera position relative to radar center; positive is target-right |
| `--camera-capture-roll-deg` | float; default `0` | Clockwise image-roll correction for preview and geometry. Set a measured roll (e.g. the plumb-line +1.35) together with a tapped target: roll alone leaves the camera yaw uncorrected, and a roll change discards the saved target ([camera](../camera/README.md#roll-together-with-the-target)) |
| `--camera-capture-stream` | `raw` or `main-y`; default `raw` | Camera stream to persist |
| `--camera-capture-scaler-crop` | `X,Y,W,H` | Optional Picamera2 scaler crop |
| `--camera-capture-rotate-180` | flag | Rotate saved frames 180 degrees |
| `--camera-capture-mirror-horizontal` | flag | Mirror saved frames left-to-right after rotation |
| `--camera-buffer-count` | int `4`-`32`; default `8` | Picamera2 request buffers (8 = 27.8 ms at 288 fps). Each 640x400 buffer takes about 0.9 MB of CMA memory (512 KB raw + 384 KB YUV420), so 32 need about 29 MB |
| `--camera-ball-locator` | `disappeared` or `legacy`; default `disappeared` | How the camera start direction finds the resting ball. `disappeared` takes the spot that vanished at the strike inside the hitting zone, with contrast gates relative to the clip's noise, the camera pose from untee'd balls only, the OPS motion gate on flight candidates and a straight-line check at the OPS speed that withholds a track missing it by more than 3 mrad. It needs clips reaching trigger + 23 frames, and the trigger frame lands up to 6 frames late (`--camera-capture-post-ms` of 101 or more at 288 fps; 178 or more gives its full 30-frame window); otherwise, and whenever it finds no ball, the legacy locator runs unchanged. `legacy` is the earlier blob detector and tracker (rollback). See [session log](session-log.md) |
| `--club-data` | `show` or `log`; default `show` | Calculated club data from the camera (path, attack angle, lofts, face, impact; "berechnet"). `show` puts the gated values on the shot and updates the screens, which show only the path and the attack angle (the lofts, face, face to path, spin-axis class, low point and impact stay in the shot and the session log: this hardware cannot measure them); `log` only writes them to the session log. Never part of the carry, the spin axis or the simulator export. See [camera](../camera/README.md) |
| `--golfer-handedness` | `right` or `left`; default `right` | Golfer handedness for the calculated club data; a simulator that reports one (GSPro) overrides it. Left-handed golfers get no calculated face yet |
| `--camera-cv-in-process` | flag | Run the per-shot camera computer vision in the server process, as before (rollback). By default it runs in one low-priority worker process |
| `--no-camera-lowlight-shadow` | flag; shadow on by default | Turn off the low-light camera start direction in shadow. By default every shot clip also runs, in the CV worker after the shot is published (and after the club-data job, never while another shot waits, within a 5 s budget), through the live mains-flicker compensation and a relaxed ball gate (motion-blur risk ignored, only a nearly black hitting area blocks, no legacy-locator fallback for clips the live gate blocks or admits only by its own relaxed dark rule). The result goes only to the session log (`camera_lowlight_ball_flight`); the shot, the screens and the simulators are unchanged. See [session log](session-log.md) |

## K-LD7 (deprecated)

Retained for existing builds only. See [Legacy (K-LD7)](../legacy/index.md).

| Flag | Type / default | Description |
| --- | --- | --- |
| `--kld7` | flag | [DEPRECATED] Enable K-LD7 vertical angle radar (launch angle) |
| `--kld7-port` | — | K-LD7 vertical serial port (auto-detect if not specified) |
| `--kld7-angle-offset` | float; default `1.5` | K-LD7 vertical boresight offset in degrees. Not user-measurable without a corner reflector; 1.5 is the calibrated default for the standard mount (default: 1.5) |
| `--kld7-mount-tilt` | float | K-LD7 vertical radar mount tilt in degrees. REQUIRED with --kld7 — measure it with a phone inclinometer against the radar face; there is no default because a wrong tilt silently corrupts the launch angle |
| `--kld7-ball-distance` | float; default `5.0` | Radar-to-tee distance in feet (default: 5.0) |
| `--net-distance` | float; default `10.0` | Ball-to-net/screen distance in feet (two_ray). For nets beyond the ~11ft FSK range wrap, far-flight frames are de-aliased and kept instead of dropped (default: 10.0; nets at/inside the wrap are unaffected). |
| `--kld7-radar-height-inches` | float; default `4.0` | K-LD7 radar height above the ball in inches, used by the ball-speed cosine correction geometry (default: 4.0) |
| `--kld7-vertical-raw` | flag | TEST MODE: show the raw vertical launch angle for every shot the estimator produces, bypassing all display guardrails (plausibility, soft-lane, estimator-agreement, confidence floor). Default off. |
| `--kld7-horizontal` | flag | [DEPRECATED] Enable K-LD7 horizontal angle radar (club path) |
| `--kld7-horizontal-port` | — | K-LD7 horizontal serial port |
| `--kld7-horizontal-offset` | float | K-LD7 horizontal angle offset in degrees (default: 0.0) |

## Wrapper-only flags

Handled by `scripts/start-kiosk.sh` itself rather than passed through.

| Flag | Description |
| --- | --- |
| `--dry-run` | Print the command that would run, then exit |
| `--startup-splash` | Show component progress while the kiosk starts |
| `--startup-splash-port` | Port used by the temporary splash server |
| `--port`, `--web-port` | Set the kiosk web port |
| `--radar-port`, `--ops-port` | Forward the OPS serial port as the server's `--port` |
| `--buffer-split` | Buffer split preset (`balanced`, `post-heavy`, `pre-heavy`) or raw segment count |

## Related

- [Running & modes](../using/running.md) — the common invocations
- [Configuration files](configuration.md) — settings that are not flags
- [Constants](constants.md) — values compiled in rather than passed
