# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **OPS243-A 1.3.3 speed trigger as shot trigger, off by default
  (`--shot-trigger ops-speed-shadow` / `ops-speed`).** Firmware 1.3.3 trips the
  OPS rolling buffer itself (`ST-25`, `SMnn`, `S#2`, no `GC`/`GS`/`A!`) and holds
  J3 pin 1 HIGH; pin 1 on GPIO22 (`--ops-speed-pin 22`) replaces the microphone
  edge. `ops-speed-shadow` (M3) keeps the microphone in charge: a dump counts
  only with a microphone edge 0–60 ms after its pin edge, others are logged as
  `ops_speed_unmatched` with their I/Q, and a microphone edge without a trip
  pulses HOST_INT as today (a trip pin 1 never showed is told apart by its
  dump's sub-millisecond first-byte lag, `self_trip`, and is no fallback
  shot). `ops-speed` (M4) drives camera, gate record and the
  TI `l3dump` (25 ms after the pin edge, `--ops-speed-ti-delay-ms`) from pin 1,
  never pulses HOST_INT and anchors club speed 28 ms after the trip
  (`--ops-speed-anchor-ms`); the microphone is a witness, and three microphone
  edges in a row without a trip re-arm a stuck OPS once (neighbours' impacts
  count too, so a busy range costs extra re-arms). Every pairing is an
  `ops_speed_trigger` session-log entry, with the microphone-minus-pin delay M3
  recalibrates both model timings from. The practice-swing rule is logged on
  every capture and rejects only with `--ops-speed-reject-practice-swings` (it
  drops 2 of 197 real shots). Below firmware 1.3.3, without a known off command
  or with pin 1 active at idle the server refuses the mode and stays on the
  microphone. Manual OPS recovery re-sends the speed-trigger setup. M1 bench
  test: `scripts/hardware-test/test_ops_speed_pin.py`. See
  [OPS speed trigger](build/ops-speed-trigger.md).
- **Ball type per profile (menu → Calibrate → Ball type).** Each profile picks
  "Standard ball" (TrackMan Tour spin table) or "Range ball" (the Toptracer
  range-ball table) for the club-typical carry spin, so the device is not tied
  to one range. `--typical-spin-table` stays the device default for profiles
  that never chose (nothing changes until one does); the switch applies from
  the next shot, trusted measured spin still wins, and `shot_detected` logs the
  table used as `typical_spin_table` (null when a trusted measured spin was
  flown). See
  [ballistics](how-it-works/ballistics.md#inputs-and-fallbacks).
- **Range-ball spin table (`--typical-spin-table range-ball`).** The ballistic
  carry can fly this user's Toptracer range-ball spin instead of the TrackMan
  PGA Tour averages when no trusted spin is measured (8-iron 4260 instead of
  7500 rpm, PW 2310 instead of 9000 rpm; leave-one-session-out on 308 OF/TT
  pairs: carry MAE 14.3 → 12.5 m). Default `tour`, unchanged. See
  [ballistics](how-it-works/ballistics.md#inputs-and-fallbacks).
- **Range-ball drag (`--range-ball-drag-slope`), unvalidated.** Raises the drag
  coefficient above Reynolds number 1.2×10⁵, so fast drives fly shorter
  (−10.7 m at the study's 0.3546) and wedges are unchanged. It failed the
  study's peak/hang-time check; default 0.0 = off, flights bit-identical. See
  [ballistics](how-it-works/ballistics.md#range-ball-drag-opt-in-unvalidated).
- **Carry variants in the session log.** Every ballistic carry gets a
  `carry_variants` entry after the shot is published: carry and apex with the
  tour table, the range-ball table and the range-ball table plus range-ball drag
  (0.3546 and 0.225), for the next range validation; the range session report
  shows them per club against Toptracer. See
  [session log](reference/session-log.md).
- **Ball and target in the camera Align view.** Two steps, **1 Ball** and
  **2 Ziel**: a tap marks the resting ball, then the target; the marks' status
  heads the settings panel (**Ball neu**, **Ziel neu**). The ball mark
  (`ball_tap` / `ball_mark`, saved with the target in a version 2
  `camera-target-line.json`) bounds the camera's resting-ball search to 64 px
  around it, so a session anchor on a tree crown can no longer place the ball;
  directions are computed as before. See
  [camera](camera/README.md#ball-and-target-line-by-tap).
- **IWR6843 self-trigger as shot trigger (`--shot-trigger`).** `ti` drives a
  shot from the TI board's own `Triggered` notice, without a microphone: the
  trigger gate pulses OPS HOST_INT at once, the camera ring freezes, the club
  speed anchors at the notice minus the firing rule's latency
  (`--ti-fire-latency-ms`, rule read from `triggerLog track`), the OPS split is
  `--ti-pre-trigger` (S#9), and `l3dump` reads the ring the board froze itself.
  `ti-shadow` keeps the microphone in charge and logs every notice against the
  microphone edges (`ti_self_trigger` entries). Needs firmware with the upstream
  `cb8fbff` l3dump fix (the fork image `openflight_iwr6843_v1.0.3-of1.bin`).
  The trigger bin defaults to offset 0 (2 bins short of the ball), which fired
  in the window far more often in that image's replay than upstream's +0.2 m;
  the fire latencies are that replay's medians. Upstream chose +0.2 m because
  backswings and waggles fired: if the range shadow test shows that, use
  `--iwr6843-self-trigger-offset-m 0.2 --ti-fire-latency-ms
  crossing=2.88,end=3.9,leave=1.19,unknown=2.91,worst=18.45`. See
  [CLI flags](reference/cli.md#shot-trigger-iwr6843-self-trigger).
- **L3 dump version 10.** Firmware v1.0.3 appends the board's clutter map to
  every dump; the parser now reads it (and the range window) and keeps v≤7.
- **Range session report.** `scripts/analysis/range_session_report.py` pairs a
  session log with a Toptracer SwingSync export by club, order and ball speed
  (never by the Pi clock) and writes a German markdown report: ball speed, VLA,
  start direction, carry, curve hit rate against two null models, inclinometer
  and trigger statistics. See
  [Range Session Report](development/range-session-report.md).
- **Optional low-battery shutdown for UPS builds.**
  `scripts/battery/install_battery_guard.sh` installs a systemd service that,
  when external power is offline and the battery is at or below 10% or 3.40 V
  for three readings 20 seconds apart, stops OpenFlight via `/api/shutdown`,
  waits for port 8080 to close and powers the Pi off. It never acts on
  external power or when the battery or charger state is unreadable. Limits
  live in `/etc/default/openflight-battery-guard`; see
  [Low-Battery Shutdown](build/battery.md#low-battery-shutdown).
- **Phone-measured TI radar tilt.** The phone lies flat on the case top in two
  positions turned 180 degrees apart; the server computes the case tilt from the
  two gravity vectors (the phone's own bias cancels), adds the CAD boresight
  angle of 10.0 degrees, subtracts the current LIS3DH pitch and applies the
  result at once. `GET`/`POST`/`DELETE /api/calibration/iwr6843/phone-tilt`
  (kiosk or paired device), socket event `iwr6843_tilt_calibration`. The value
  is saved to `~/.config/openflight/iwr6843_phone_orientation.json`, outranks
  `--iwr6843-tilt-deg` at startup and survives the TI USB power-cycle recovery.
  Adapted from upstream #298. See
  [Calibrate the TI tilt with a phone](build/inclinometer.md#calibrate-the-ti-tilt-with-a-phone).
- **Shots carry their simulated flight path.** A ballistic shot now includes
  `flight_path`, `{"unit": "yd", "points": [[t, x, y, z], ...]}`, with `t` in
  seconds since launch and `x` (downrange), `y` (right is positive) and `z`
  (height) in yards, ending at the touchdown. It is in the live `shot` payload
  and in the session log's `shot_detected` entries. The history detail endpoint
  returns it only on request, as `/api/session-history/<id>?flight_path=1` (so
  the default payload stays small), validated on read (unit `yd`, 2–400 points of
  four finite numbers) and `null` for anything else. Table carries, swing-speed
  reps and flights that end at launch or never touch down have none. It is the
  groundwork for the virtual range. See
  [Ballistics & Carry](how-it-works/ballistics.md).
- **Shots carry their roll and Total.** Every ballistic shot now bounces and
  rolls out from its touchdown on a normal fairway, after Penner's model (2002)
  with the paper's own values: `total_yards` is where the ball rests, measured
  like carry, `roll_yards` is Total minus carry (negative when the ball spins
  back), and `roll_path` holds the bounces and the roll in `flight_path`'s
  format, at most 25 points. They are in the live `shot` payload (Total and roll
  rounded to whole yards) and in the session log's `shot_detected` entries. Real,
  simulated and demo shots get them; table carries, shots without a flight path
  and touchdowns faster than 40 m/s, OpenFlight's limit for the model, have none.
  The history detail endpoint returns `roll_yards` and `total_yards` with every
  shot, and `roll_path` with `flight_path` on the same `?flight_path=1` request
  (validated on read, 2–25 points). See
  [Ballistics & Carry](how-it-works/ballistics.md#roll-out).
- **Virtual range on the kiosk.** A **Range** button in the footer flies every
  shot over a 3D driving range, with faint tracers of the two shots before it,
  landing dots and a carry sign where the ball lands. A bar shows club, carry
  and ball speed; a shot whose IWR or camera data is still being processed
  waits until its final numbers are in. **Show on range** in the history dialog
  opens a saved session there. Only pages served from `localhost`, `127.0.0.1`
  or `[::1]` (the Pi's own screen) get the Range. When frames run slow it drops
  anti-aliasing, then half the vegetation, for the running kiosk only.
  `?rangeDemo=1` flies three demo shots and logs frame times for render
  measurements. See the
  [UI README](https://github.com/jewbetcha/openflight/blob/main/ui/README.md).
- **Range stage 2: Total in the Live view.** Once a shot is final, the Carry
  tile shows its Total underneath ("Total —" when the shot has no roll).
- **Range stage 2: roll-out.** On the Range the ball bounces and rolls from its
  landing to its rest point, where the distance sign shows the Total.
- **Range stage 2: aiming.** Tapping a green turns it straight ahead of the mat;
  later shots fly towards it, and a second tap releases it.
- **Range target list.** A **Target** button in the Range's top bar lists the
  greens nearest first; picking one aims at it like a tap, and **No target**
  releases it.
- **Delete a shot, everywhere.** **Delete last shot** in the Live view and on the
  Range's shot bar, and a delete per row in the history and the shot list, each
  asks **Really delete?** first; the shot leaves the stats, the Range and training.
- **Range stage 2: distance to the target.** At rest a dashed line runs from the
  ball to the selected green, else the nearest flag, labelled with the distance.
- **Range stage 2: shot bar.** The bar shows Carry · Total · to target.
- **Range stage 2: distance training.** A **Training** mode sets a target
  distance with buttons, starting from the player's average with the club, and
  scores each shot at its rest point in zones of ±3, 6 and 10 %, with running
  figures over the last 10 shots.
- **Demo mode.** A **Demo mode** switch in the menu (ball mode only) shows every
  feature without hitting a ball: the kiosk and every paired phone switch to
  six starter shots at once, then get a new shot every 20 s, each flying on the
  range. A band marks the demo on every screen, with an **End** button on the
  kiosk and the phones. Real shots are ignored and leave one line in the app
  log. The demo ends with the switch, with **End**, after 15 minutes without a
  touch on the kiosk or a phone, or when the app restarts. It lives in memory
  only: demo shots are never written to the session log, saved to the session
  history, uploaded or sent to a simulator, and the real session comes back
  unchanged.

### Changed
- **The Range fills the screen.** The 3D scene takes the whole kiosk screen;
  the header, the demo band and the footer lie over it, dark and see-through.
  The range's own bars, hint and notice sit between them, the camera centres
  its view there, and distance signs keep off the bars.
- **The UI depends on `three` 0.186.1.** The range scene is bundled into its
  own chunk (about 575 KB) that loads only when the Range opens. `npm run build`
  now ends with `scripts/check-bundle.mjs`, which fails the build when three.js
  shows up in any other chunk.
- **Simulated shots now run the same ballistic carry as live shots.** Shot
  finalization skipped the carry step for `--mock` shots, so they never got a
  committed carry, landing offset or carry model. They now take the same path as
  live shots: the ballistic simulation, or the carry table when ballistics is
  off or the launch angle is missing.
- **Chromium fallback is reachable during Electron upgrades.** If `ui/dist`
  already exists, a missing Electron install no longer requires Node 22.12 and
  a successful `npm install` before the kiosk can start. Old Node or a failed
  install warns and continues to system Chromium. A missing UI still requires
  Node 22.12+ and a successful build.
- **First switch from Chromium to Electron resets browser-local UI state.**
  Electron persists its own session under `~/.config/openflight-ui` (Linux),
  not the system Chromium profile. Units, language, theme, pinned Live metric,
  and validation annotations in `localStorage` do not carry over. Export the
  Shots CSV on Chromium before switching. Profiles and shot logs are
  server-owned and unaffected. See
  [Electron Kiosk Shell](electron-kiosk-shell.md#browser-local-state-breaking-on-first-electron-launch).

### Fixed
- **The club's trailing track no longer becomes the ball speed.** A ball-track
  guard checks the voted ball's track and smash and reads the post-impact windows
  again when they look like the club head (S2#3 of 02.11.: 60.8 → 148.3 mph,
  Toptracer 150.1). Without a better track the shot is flagged `uncertain`: the
  screens mark ball speed, smash and carry with ≈, and it stays out of the
  averages. See [rolling buffer](how-it-works/rolling-buffer.md#ball-track-guard).
- **An implausible TI launch angle no longer reaches the carry.** The club/speed
  launch guardrail that already screened K-LD7 angles now also checks the
  IWR6843 (LCMF-v1) vertical angle, using trusted measured spin only. An angle
  at or below the expected launch must lie within 0–45°, one above it must stay
  within the club's allowed delta. A rejected angle is handled like a missing
  one: the launch angle is estimated (`launch_angle_vertical_source`
  `"estimated"`), the `[SERVER]` warning names the rule that fired, the Debug
  panel shows the angle with the reason, and the shot carries the new field
  `launch_angle_vertical_rejection_reason` (`"ti_launch_implausible"`, else
  `null`) in the live `shot` payload and the session log's `shot_detected`
  entries. On the 2026-10-25 range session this changes only one shot (7-wood,
  TI angle -0.47° with a channel stuck at the grid edge): launch 10.5°
  (estimated) instead of -0.47°, carry 198 yd instead of 0. The TX2 horizontal
  proxy keeps its own coherence gate. See
  [IWR6843 Troubleshooting](iwr6843/troubleshooting.md).
- **A damaged saved session can no longer break the whole session history.**
  An oversized number in one session log used to fail the session list, and
  that session's detail, with HTTP 500. Oversized numbers and `true`/`false`
  in a numeric field now read as missing; a shot whose ball speed is unusable
  is skipped.
- **A crash-looping boot service no longer kills the desktop kiosk.** Every
  launcher exit ran a `pkill` that matched the Electron binary path, so an
  `openflight.service` that failed at startup (for example because systemd's
  PATH hides `~/.local/bin/uv`) restarted every 5 s and killed whichever
  kiosk was on screen; Chromium then died with "GPU process isn't usable.
  Goodbye." `start-kiosk.sh` now launches the browser in its own process
  group and stops only that group (`scripts/kiosk-browser.sh`), refuses to
  start while another instance holds `/tmp/openflight-kiosk-<port>.lock`
  (exit 3, `OPENFLIGHT_KIOSK_LOCK_FILE` overrides the path), finds `uv` in
  `~/.local/bin` / `~/.cargo/bin` when PATH omits them, and prints the
  recovery hint to the terminal and journal. The unit file stops retrying
  after five failures in five minutes and never retries exit 3. Re-copy
  `scripts/setup/openflight.service` (or rerun `scripts/setup/setup.sh`) on
  existing Pis to pick up the unit changes.
- **Kiosk startup no longer rebuilds the UI after Electron has already launched.**
  `ensure_kiosk_ui` now runs before the splash browser. The helper is also
  stored with Unix line endings so a Windows checkout cannot make `ui/dist`
  look missing (a CR in the path) and run `npm install` over a live Electron
  GPU process.
- **On-screen keyboard for profile names.** Adding or renaming a profile on the
  Pi kiosk now shows a full-screen keyboard. Chromium in `--kiosk` mode does not
  surface a system keyboard, so the native text field was unusable on the
  touchscreen.
- **Clear-session confirmation is a modal again.** The overlay, scrim, and
  centered dialog styles were missing after the class-name rename, so at the
  800×480 kiosk size the prompt rendered as inline page content.
- **Attack angle no longer inflated by 1/cos(club path).** The camera club
  delivery divided vertical speed by the forward component alone instead of the
  full horizontal speed, overstating attack angle on any shot with club path.

### Added
- **Device pairing.** Every client except the Pi's own screen must now be
  paired: Menu → **Pair device** shows a QR code and a one-time code (five
  minutes, single use, rate limited) that a phone, tablet, TV browser, or
  laptop exchanges for a long-lived HttpOnly cookie. Unpaired devices see a
  code entry, the API answers 401, and live updates are refused. **Sign out all
  devices** rotates the secret and drops their connections. See
  [Pairing Devices](using/pairing.md).
- **Shot dispersion in the companion.** A "Streuung" tab next to Live plots
  carry over lateral landing offset per club (equal axes, mean, 2σ ellipse)
  with a per-club mean ± SD table for the current session or the last 7/30
  days. Derived or uncertain directions are drawn hollow, and uncalibrated aim,
  substitute-spin carry and shots without a direction are labelled. Tapping a
  shot opens its values and replay.
- **Lateral landing offset and aim-calibration status.** Ballistic shots now
  include `carry_lateral_yards` (simulated landing offset, positive = right of
  the target line; empty for the carry table or an unknown direction) in the
  live payload, session logs and the 30-day history, which also returns
  `launch_angle_horizontal_confidence`. `session_state` reports
  `calibration.horizontal_phase_reference` so clients can flag uncalibrated aim.
- **Phone/tablet companion view.** Open `/companion` on a phone or tablet on
  the same network for live shot values, the session's shots, and an inline
  slow-motion replay that starts when the server announces it. It is read-only
  and can be added to the iOS home screen.
- **Background camera replay preparation.** With camera capture enabled, the
  server encodes each finalized shot's replay MP4 in a low-priority worker once
  no shot is being captured or published, then emits `camera_replay_ready`
  (`shot_number`, `replay_id`, `video_url`). Manual replay requests reuse the
  same cache. Failures are logged only.
- **Electron kiosk shell.** `scripts/start-kiosk.sh` now opens the UI in a pinned
  Electron window (`electron@44`) instead of whichever system browser happens to
  be installed. Chromium remains a fallback if Electron is not installed (including
  when Node is older than 22.12 or `npm install` fails and `ui/dist` already
  exists). Installing Electron needs **Node.js 22.12 or newer**. See
  [Electron Kiosk Shell](electron-kiosk-shell.md).
- **PAR-TEE connector.** `"type": "partee"` in `config/sim.json` streams shots
  to the [PAR-TEE](https://playpartee.com) iPhone app over OpenConnect V1 on the
  phone's Wi-Fi address (port 921 by default). Same shared codec as GSPro and
  OpenGolfSim; the header pill and "Sent to" panel read PAR-TEE.
- **Profiles replace players.** Shots are now attributed to a server-owned profile
  (a person *or* a place) with a stable id, persisted to
  `~/.config/openflight/profiles.json` (override with `OPENFLIGHT_PROFILES_PATH`
  or `--profiles-path`). Profiles can be renamed without orphaning their shots.
  Removing a profile is refused while it still has session rows. The socket
  exposes a single authoritative `profiles` snapshot plus
  `set_active_profile` / `add_profile` / `rename_profile` / `remove_profile`.
  Breaking: `set_player` / `player_changed` are gone, `Shot.player_name` is replaced
  by `profile_id` + `profile_name`, and existing browser-local player rosters are
  discarded.
- **Automatic OV9281 exposure control.** High-speed camera capture now measures
  the impact area every five seconds, restores the last known-good setting at
  startup, and selects a shutter/gain combination that preserves club contrast
  without excessive clipping or motion blur. Large lighting changes re-enter
  fast convergence while smaller changes require confirmation. Camera-derived
  shot analysis is withheld when lighting is unsuitable, but radar processing
  and shot display continue normally with an operator-facing lighting warning.
- **Instrument-panel kiosk UI.** The dashboard is a tabbed shell (Live, Stats,
  Shots, Camera, Profiles, Debug) instead of the previous stacked shot and stats
  views. Tap a Live metric to pin it top-left while keeping all ten metrics
  visible. The footer logo opens units, dark/light theme, language, simulator,
  and ball-detection status; a persistent footer power button opens the shutdown
  confirmation. Club (or training implement) selection is a Live header action.
  See the [UI README](https://github.com/jewbetcha/openflight/blob/main/ui/README.md).
- **Kiosk languages.** English, German, and Spanish. Choice is
  stored in `localStorage` (`openflight.locale:v1`).
- **Dark and light themes.** Toggle in the footer menu; stored as
  `openflight.theme` (default dark).
- **Synchronized OV9281 high-speed camera capture.** OpenFlight can now retain
  pre- and post-impact camera frames from the shared sound trigger, align them
  with OPS243 and IWR6843 captures, and use camera-assisted or camera-only
  fallbacks for horizontal launch, club path, and angle of attack. The Camera
  tab adds live alignment, crop, orientation, and lighting controls while the
  rolling buffer remains armed. See [OV9281 Camera](camera/README.md).
- **On-demand camera shot replay.** Camera-backed shots can open a 60 FPS
  slow-motion impact player from Live or Shots, with touch controls, scrubbing,
  and a trigger-frame impact marker. MP4 conversion starts only after a manual
  Replay selection, caches the result beside the raw capture, and reports
  retryable preparation or playback failures without affecting shot results.
- **Battery and external-power status for Raspberry Pi UPS boards.** OpenFlight
  can now display charging state and battery percentage, issue dismissible 20%
  and 10% warnings while discharging, and record throttled power telemetry in
  session logs. Enable the initial Geekworm X1202/X1206 provider with
  `--battery geekworm`; monitoring remains disabled when no provider is
  selected. The accompanying Pi setup installs native Linux power-supply
  telemetry and optional taskbar capacity support without enabling automatic
  shutdown or charging control. See [Battery Monitoring](using/battery.md).
- **System Prerequisites:** Documented missing binary dependencies (`swig`, `liblgpio-dev`, `python3-dev`) required prior to executing `./scripts/setup/setup.sh`.
- **Environment Reload Guidance:** Added instructions for reloading terminal environment variables (`source ~/.bashrc`) when installed dependencies or scripts (`setup.sh`, `start-kiosk.sh`) are not recognized in the current terminal session.
- **Configurable IWR6843 capture compression.** One firmware image can now
  switch at runtime between the recommended 24-frame, 3 ms, 53-bin IQ16
  profile and an advanced 36-frame, 2 ms, 32-bin IQ8 profile. Both retain the
  same 72 ms shot movie and all 3 TX x 4 RX channels. Per-frame range windows,
  timing, and IQ8 scales are carried in the dump so offline and live processing
  use the actual capture geometry. The dense profile uses a sparse scale
  preview to sustain the 2 ms HWA rearm budget and exposes missed frames,
  overruns, and clipped components through firmware `stats`.
- **OPS243 over the Raspberry Pi GPIO UART.** The radar can now run on the J3
  header instead of USB, which frees the Pi's USB power budget for the TI angle
  radar. Baud is the real wire rate on that transport and the factory default of
  19,200 would stretch a 40.6KB dump to 21 seconds, so the driver probes for the
  rate the board is actually using and raises it to 230,400 (`I5`), bringing a
  dump down to ~1.8 seconds. Every dump timeout now scales with the negotiated
  rate, so a link that settles low runs slowly instead of truncating captures.
  Pass `--radar-port /dev/ttyAMA0` (and optionally `--ops-baud`); USB behaviour
  is unchanged. `diagnose.py --ops-port` adds a preflight for the three
  UART-only failures that all look like an unresponsive radar — missing device
  node, a login console holding the port, and the OPS USB cable still plugged in
  (which silences the UART). See
  [Moving the OPS243 from USB to the Pi GPIO UART](build/ops243-uart.md).
- **Flash IWR6843 firmware directly from a Raspberry Pi.** Contributors no
  longer need an Intel Mac, UniFlash, or TI Cloud Agent for routine firmware
  updates. The guided terminal workflow verifies the image hash, offers a
  non-destructive bootloader probe, erases the existing image, transfers the
  replacement in acknowledged chunks, and requires the radar's ROM bootloader
  to verify the completed image. The current IWR6843LEVM still requires its
  physical flash-mode switch and reset button.
- **Experimental three-transmitter capture for horizontal launch direction.**
  The TX2 firmware variant captures all three transmitters while retaining the
  TX1/TX3 vertical array, giving the offline and live pipelines the antenna
  diversity needed to begin measuring left/right start direction.
- **On-chip range snapshots for smaller IWR6843 shot captures.** The radar uses
  its hardware accelerator and EDMA to retain 53 selected complex range-FFT
  bins in moving early, middle, and late windows instead of every raw ADC
  sample. The production ring keeps 18 frames at 4 ms spacing in a 549,542-byte
  dump while preserving vertical and horizontal processing inputs.
- **`--kld7` now delivers the full launch-angle pipeline by default.** Enabling
  the K-LD7 radars turns on the **two-ray multipath vertical launch-angle
  estimator** (per-frame demodulation that separates the ball from its floor
  reflection to recover true elevation instead of averaging across the
  multipath) plus the **ball-speed cosine correction** (OPS radial → true
  speed). Each shot is graded into a tour-derived Tier-1/Tier-2 confidence with
  a tour-average boost for suppressed reads; measurements that clear the
  physics guard but trip a soft consistency guard are shown as **marginal
  (one-dot) confidence** rather than silently replaced by the club estimate.
  Far-net flights are de-aliased past the FSK range wrap (`--net-distance`).
- `--kld7-mount-tilt` is **required** with `--kld7` (measure with a phone
  inclinometer — no safe default). `--kld7-angle-offset` defaults to the
  calibrated `1.5`.
- `--calculated-spin` (opt-in, off by default): replaces radar spin with the
  kinematic estimate `170·v·sin(LA)^1.2`; the measured value is retained in
  `spin_rpm_measured` for scoring.
- `--kld7-vertical-raw` test mode surfaces the raw radar angle for every shot
  (all display guards bypassed).
- **Club path from the IWR6843's pre-impact frames.** `Shot.club_path_deg` has
  been wired end to end since the K-LD7 era but unpopulated since that radar
  was deprecated. It now comes from the six pre-impact frames the L3-dump
  firmware already retains. The estimator fits `x(t)` and `y(t)` in Cartesian
  coordinates and reports `path = atan2(v_y, v_x)`; absolute azimuth enters
  additively rather than cancelling out, so a constant per-element phase error
  from the shipped array calibration (measured on a different board) shifts
  the reported path by a constant rather than the estimator itself — which is
  what `--iwr6843-azimuth-offset-deg` is for. Measured on a first-principles
  fixture across ±12°, absolute error grows with angle (0.034° at 4° to
  0.303° at −12°, roughly symmetric), so it separates deliberate in-to-out
  from out-to-in swings but does not support degree-level claims. Ships
  experimental; validate with `scripts/iwr6843/club_path_report.py` before
  trusting it.

### Changed
- Club physics and simulation defaults now lives in one immutable registry
- Core server, session logging, kiosk startup, and UI code now share canonical
  shot/session helpers and omit redundant compatibility paths.
- Session JSONL format version is now 2 after consolidating trigger and shot
  entries and removing obsolete entry types.
- Display mode (`/display`) now uses the same metric cards and theme tokens as
  the kiosk Live view.
- The vertical estimator is now a fixed cascade (two_ray → geometry →
  single-frame geometry → naive); it is no longer user-selectable. Launch-angle
  source and confidence semantics changed accordingly.

### Removed
- Deprecated integrated-camera detection, obsolete K-LD7 experiment/replay
  tooling, unused session-log writers, and their redundant tests and controls.
- `--kld7-vertical-estimator` (estimator is a fixed cascade), `--kld7-geometry`
  (kiosk preset), and `--ball-speed-cosine-correction` (folded into `--kld7`).
  `--kld7-bypass-vertical-gate` renamed to `--kld7-vertical-raw`.

### Fixed
- Kiosk startup no longer exits when the optional Alloy service is unavailable,
  and it rebuilds the UI bundle before launching the server.
- **Graceful IWR6843 shutdown.** Kiosk shutdown now asks the server to finish
  hardware cleanup before escalating to process signals. An active TI dump is
  allowed to complete, capture firmware is stopped and verified inactive, and
  the serial port is then closed. This prevents an interrupted L3 transfer or
  failed GPIO setup from leaving the radar unresponsive on the next startup.
- **Raspberry Pi 5 & OS Compatibility:** Updated UART configuration documentation to support Debian Bookworm and Raspberry Pi 5 hardware using `dtparam=uart0=on` alongside legacy `enable_uart=1`.
- **UART Diagnostic Commands:** Simplified UART verification instructions in `docs/iwr6843/README.md` and `docs/ops243-uart-migration.md` using `grep -E` to check for both legacy and modern device-tree parameters simultaneously across config paths.
- Session logging: serialize all access to the session JSONL file with a
  lock. The `log_*` methods are called concurrently from the OPS243
  capture thread, the K-LD7 stream thread, and Flask-SocketIO handlers;
  without synchronization, large entries (e.g. `rolling_buffer_capture`
  with 2×4096 samples) could interleave and corrupt JSONL lines, and a
  write could race `end_session()` closing the file (`AttributeError` /
  `ValueError: I/O operation on closed file`). Corrupt lines break the
  offline replay tooling that depends on these logs.
- IWR6843 runtime: the ball-estimate call passed a hardcoded
  `tdm_sign_policy="positive"` instead of the runtime's configurable field
  (the club-path fallback already honored the field, and offline replay
  plumbs a caller-supplied policy end to end). Any non-default policy
  silently produced different live-vs-replay answers for the same capture.
  Live behavior with the default is unchanged.
- **GPIO startup on a Raspberry Pi 5.** Anything using the sound-trigger GPIO —
  the IWR6843 capture monitor and the GPIO sound trigger — died with
  `BadPinFactory: Unable to load any default pin factory!`. The cause is
  upstream: gpiozero 2.0.1.post2 (the latest release) calls `os.path.exists`
  in its lgpio chip auto-detection without importing `os`, and that code path
  only runs on a Pi 5, so gpiozero swallows the `NameError` and every remaining
  backend then fails for its own reason. OpenFlight now selects the lgpio
  factory itself with an explicit gpiochip, which skips the broken branch.
  `OPENFLIGHT_GPIO_CHIP` overrides the chip if a kernel update renumbers the
  header. Note that `GPIOZERO_PIN_FACTORY=lgpio` was never a workaround — it
  forces the same failing call.
- IWR6843 range-snapshot capture now freezes only at a completed ring boundary
  and correctly rearms the HWA/EDMA chain, preventing partial or one-shot-only
  captures during repeated shots.
- K-LD7 tracker: shots could silently lose their launch angle when the
  stream thread appended a frame while the shot path iterated the ring
  buffer (`snapshot_buffer` / `_radc_frames_for_extraction`). CPython
  raises `RuntimeError: deque mutated during iteration` for this, and
  the server's broad K-LD7 exception handler swallowed it, so the shot
  was reported without an angle and no error was visible. Buffer reads
  now copy under a lock; appends and resets take the same lock.
- **One collapsed channel no longer drags the launch angle down.** The vertical
  estimator averaged its two channel estimates unweighted. On a 0.229 m, 5.5°
  mount the `two8` channel collapsed to about 0° on five of six shots while
  `four4_path_tdm` read 15.3–22.0°, so five 7-irons that actually launched near
  17° were reported at 7–9°, each stamped with 0.95 confidence while
  `component_std_deg` sat at 8–10° in the log. Across a seven-shot session the
  plain mean read 10.9° with one channel collapsed; channel selection recovers
  18.3°. Channels that disagree beyond 8° now resolve to the better-supported
  one with reduced confidence; channels that agree are still averaged.
- **Launch-angle confidence is derived instead of hardcoded.** Both the vertical
  and horizontal angles reported a constant 0.95. The horizontal case computed
  HLCMF-v0 coherence, logged it, and then discarded it in favour of the
  constant, so five estimates whose own channels disagreed by 8–10° were
  presented as high confidence. Vertical confidence now follows channel
  agreement and corroboration, horizontal follows coherence, and `spin_axis_deg`
  gates on the horizontal leg rather than appearing the moment club path exists.
- **Track-span floor relaxed from 18 ms to 15 ms, and the span is now logged.**
  Recovers usable captures — one range session went from 6/7 accepted at a
  10.9° mean to 7/7 at 18.3°, and the 18 ms → 15 ms change is what recovered
  the seventh shot. The span is now recorded, since it was the gate rejecting
  most shots and was invisible without an offline replay.
- **A channel that measured nothing can no longer win the channel selection.**
  Objective curvature scored 0 both when a channel's minimum was genuinely
  flat and when its minimum sat on the edge of the −5° to 45° search grid —
  two different things, since an edge minimum means the true angle lies
  outside the searched range and a real launch above 45° pins a perfectly
  healthy channel there. Curvature now returns "no measurement" for an edge
  minimum, and such a channel takes no part in selection, in the spread
  comparison, or in the reported `component_std_deg`. When the channels
  disagree and none has positive curvature — all flat, all off-grid, or
  unscored — the shot is rejected as `rejected_no_conditioned_channel`
  instead of returning whichever channel came first in the dictionary, which
  was `two8`, the one that collapses.
- **The `fast_*` estimates no longer veto corroboration they cannot win.** All
  five components fed the agreement comparison while only the two channel
  models could be selected, so one `fast_*` outlier pushed the spread past
  the 8° gate and cut two channels agreeing to 0.4° down to a single channel
  flagged as uncorroborated and derated. They are diagnostic-only: still
  logged in `components_deg`, now excluded from the selection decision and
  from `component_std_deg`. Affects raw-ADC captures only — range-snapshot
  captures never computed the fast-time models.

### Known Limitations

Deferred pending a session paired with a reference instrument. See
[the IWR6843 operator guide](iwr6843/calibration.md#launch-angle-estimator-limitations).

- **The calibration tilt sweep cannot recommend a tilt.** It minimises
  `component_std_deg`, which is monotonic in tilt across the swept window, so
  its minimum lands on a window edge instead of the mount angle: on the
  2026-07-25 session, with the mount measured at 5.5°, a ±3° sweep returned
  2.5° on two shots and 8.5° on two others. Set tilt by physical measurement.
- **The curvature criterion is not scale-normalised.** `four4_path_tdm`'s
  objective range is 2–4× larger than `two8`'s, so most of the "3.7–10.7×
  sharper" margin is model scale — on one shot the true margin is 1.14×. It is
  validated as a degeneracy detector, not an accuracy ranker, and it is
  one-sided: a collapsed `four4_path_tdm` would likely still win.
- **Selecting is worse than averaging when both channels are healthy but
  disagree.** Monte Carlo at 6° of noise: 4.26° RMS averaging against 5.79°
  selecting, 7.93° on the disagreeing subset. The 8° gate's justification is a
  gap between one shot at 4.59° and six at 15.9–20.2°, from a single session,
  club, geometry and tilt.

### Changed
- Spin detection: drop the autocorrelation override branch. The autocorr
  peak inside the envelope search region often lands at minimum lag
  (~12000 RPM / upper rail) by spectral coincidence, which previously
  flipped legitimate mid-range FFT seam picks to the upper rail and got
  them rejected as bandpass-shoulder noise. The autocorr fallback still
  *confirms* the FFT pick when the two agree within 10%; disagreements
  are now logged for diagnostics but never replace the FFT result.
- Spin detection: lower `SPIN_SNR_MIN` from 3.0 → 2.5 so marginal but
  real seam tones are reported at low confidence instead of dropped.

### Added
- `scripts/analysis/replay_club_speed.py`: offline replay of a proposed
  MEDIAN club-speed picker against any session log. Builds the same
  candidate set the production picker uses, applies a 30 % magnitude
  floor, and reports the median speed for each `rolling_buffer_capture`
  alongside the originally logged (magnitude-pick) value, with smash
  factors as a physical sanity check. The script is exploratory and
  does not change production behaviour — it lets us inspect what a
  median-based picker would have produced before committing to a code
  change.
- `scripts/analysis/plot_spin_debug.py`: 4-panel diagnostic for a single
  `rolling_buffer_capture` (speed timeline, raw I/Q, bandpass envelope,
  envelope FFT spectrum) to inspect what the spin algorithm saw and why
  it accepted or rejected a shot.
- K-LD7 shot-correlation analysis workflow and theory writeup
  - `scripts/analyze_kld7.py --pair-shots` for offline club-to-ball pairing on `.pkl` captures
  - `docs/kld7-ball-detection-theory.md` with capture findings and detection rationale
- Persistent rolling buffer mode workaround for OPS243-A HOST_INT pin bug (per OmniPreSense)
  - `persist_rolling_buffer_mode()` method saves settings to flash memory
  - `test_rolling_buffer_persist.py` script for one-time radar setup and verification
  - Rolling buffer + sound trigger is now the default operating mode
- Grafana Alloy integration for shipping session logs to Grafana Cloud Loki
  - Setup script (`scripts/setup_alloy.sh`) and config (`config/alloy.alloy`)
  - Auto-starts with `start-kiosk.sh` when credentials are configured
  - Observability documentation with LogQL query examples
- Launch angle estimation from club type and ball speed (fallback when camera unavailable)
- Tunable Hough circle detection with all 5 parameters as CLI args (`--hough-param1`, `--hough-param2`, `--hough-min-radius`, `--hough-max-radius`, `--hough-min-dist`)
- Interactive `--tune` mode in `test_launch_angle.py` with live OpenCV trackbar sliders
- Mock mode now simulates realistic spin and launch angle data (TrackMan-based per-club averages)
- Sound trigger wiring guide with MOSFET circuit design (`docs/sound-trigger-wiring.md`)
- Camera integration with real-time ball detection in UI
- Ball detection indicator in header (shows detection status)
- Camera tab with live MJPEG stream and detection overlay
- Hough circle transform as default ball detector (replaces YOLO dependency)
- ByteTrack object tracking for persistent ball identification
- Club speed detection and smash factor calculation
- Rolling buffer mode for experimental spin rate detection
- Session logging to JSONL files (`~/openflight_sessions/`)
- I/Q streaming mode with FFT and 2D CFAR noise rejection
- `--mode rolling-buffer` flag for spin detection
- `--session-location` and `--log-dir` flags for session logging
- Roboflow API integration as optional detection backend
- YOLO performance tuning documentation for Raspberry Pi
- ONNX model export support for faster inference
- Threaded camera capture for improved FPS
- Rolling buffer spin detection documentation

### Changed
- K-LD7 launch-angle processing now uses OPS243 impact timestamps for live correlation
- K-LD7 ball-burst selection now prefers coherent far-target paths instead of averaging all far PDAT detections
- Live K-LD7 vertical launch angles now fall back to the existing club-and-speed estimate when the radar result is an obvious false positive
- Spin detection improved: Hann windowing, zero-padding to 256 points, band-limited search
- All shot metrics (spin, launch angle, club speed, carry) always shown in UI
- Shot logging unified — all metrics in single `shot_detected` entry
- Shot `mode` and `readings_data` are now proper dataclass fields (no more monkey-patching)
- Session logging enabled in mock mode for testing Alloy integration
- Default ball detection uses Hough circles instead of YOLO (no ML model required)
- Camera enabled by default in kiosk mode (use `--no-camera` to disable)
- Dropped Python 3.9 support (requires >=3.10)
- Updated Raspberry Pi setup guide with camera UI and observability instructions

## [0.2.0] - 2024-12-01

### Added
- Web UI with React frontend and Flask-SocketIO backend
- Real-time shot display with ball speed, carry distance, smash factor
- Session statistics view with per-club filtering
- Shot history with pagination
- Debug panel for radar tuning and raw readings
- Mock mode for development without hardware
- Kiosk mode script for Raspberry Pi deployment
- Systemd service for auto-start on boot
- Camera module for launch angle detection (experimental)
- Camera-based ball tracking for launch angle
- Club type selection (Driver through PW)

### Changed
- Migrated from CDM324/HB100 radar to OPS243-A
- Improved carry distance estimation model

## [0.1.0] - 2024-10-01

### Added
- Initial OPS243-A radar driver
- Basic launch monitor with shot detection
- CLI interface for monitoring shots
- Python API for integration
- Carry distance estimation based on ball speed

[Unreleased]: https://github.com/jewbetcha/openflight/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jewbetcha/openflight/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jewbetcha/openflight/releases/tag/v0.1.0
