# OV9281 High-Speed Camera

OpenFlight can capture a short, synchronized camera movie around impact. The
camera does not replace the OPS243 or IWR6843: OPS anchors speed and trigger
timing, the IWR6843 measures radar angles, and the camera provides a visual
record of the clubhead and early ball flight.

Camera capture is experimental. Camera-assisted horizontal launch, club path,
and attack angle must remain quality-gated until they are validated against a
launch-monitor source of truth.

## Supported Hardware

The tested camera is an InnoMaker OV9281 monochrome global-shutter module on a
Raspberry Pi 5. Other OV9281 modules may expose different register programming
or lens geometry even when they use the same sensor.

Required hardware:

- Raspberry Pi 5 running Raspberry Pi OS.
- InnoMaker OV9281 monochrome global-shutter camera.
- Correct Pi 5 camera ribbon cable for the selected CAM/DISP connector.
- Rigid, focusable camera mount.
- Shared sound-trigger wiring on BCM17 when using OPS and IWR6843 capture.

Power the Pi off before connecting or disconnecting the ribbon cable. Confirm
that the cable contacts face the correct direction for both the Pi connector
and camera board before applying power.

## Mounting And Sensor Offset

Mount the camera rigidly and point it down the target line. The tested original
assembly placed the camera directly above the IWR6843 antenna center, about
`8.25 in` (`0.20955 m`) above the hitting surface. A laterally offset camera is
also supported when its position is measured and passed to OpenFlight.

Measure horizontally from the radar antenna center used for radar height to the
camera lens optical center. Pass that signed distance in meters:

```bash
scripts/start-kiosk.sh \
  --camera-capture \
  --camera-capture-lateral-offset-m 0.0762
```

The sign is defined while standing behind the unit and looking down the target
line:

- Positive: camera is to target-right of the radar center.
- Negative: camera is to target-left of the radar center.
- Zero: camera and radar optical centers share the same vertical centerline.

For reference, `3 in` is `0.0762 m`. Measure center-to-center rather than using
enclosure edges. OpenFlight records this value with the session and uses it in
camera-only ball depth, camera/IWR horizontal launch, Club Path, and Attack
Angle geometry.

This physical distance is different from
`--camera-capture-horizontal-offset-deg`. The meter value describes where the
camera is mounted; the degree value corrects residual camera yaw or target-line
alignment. Do not convert the physical separation into degrees, and do not use
the degree setting as a substitute for the measured distance.

The useful image does not need to contain the golfer or the full shaft. It must
contain:

- The stationary ball and impact point.
- The clubhead for the final frames before impact.
- The clubhead through impact.
- The first several milliseconds of ball flight.

TrackMan-aligned 7-iron and 9-iron captures showed that a `200`-row view was
needed to retain both the final clubhead approach and early ball flight. The
tested high-speed `320x200` mode (withdrawn since 2026-11-02, see Capture Modes) defaulted
to a vertically centered `(480,150)` crop.
A `150`-row crop was too tight for robust combined clubhead and ball-path
tracking. A raised `200`-row crop remains a future experiment, not part of the
checked-in driver.

This result has not yet been validated for wedges or driver. Use the alignment
preview before each new physical mounting configuration.

## Raspberry Pi Packages

Install the Raspberry Pi camera stack and preview dependency:

```bash
sudo apt update
sudo apt install -y rpicam-apps python3-picamera2 ffmpeg
```

The main `scripts/setup/setup.sh` installer also installs FFmpeg automatically
on Raspberry Pi systems when it is missing. Picamera2 remains an operating-
system package because it must match Raspberry Pi OS and its libcamera stack.

Install OpenFlight's optional camera image-processing dependency from the
repository root:

```bash
uv sync --extra camera
```

OpenCV is intentionally not installed for radar-only OpenFlight systems.

Reboot after enabling or changing camera hardware:

```bash
sudo reboot
```

Verify that the camera is detected:

```bash
rpicam-hello --list-cameras
```

The standard Raspberry Pi driver should list the stock OV9281 modes. The
OpenFlight high-speed driver additionally lists `640x200`, `640x100`, and
`320x200` raw modes.

## High-Speed Driver

The Raspberry Pi kernel's stock OV9281/OV9282 driver does not expose the
cropped high-speed modes used by OpenFlight. The repository includes a narrow
kernel patch and installer under:

```text
drivers/ov9281/
scripts/setup/install_ov9281_high_speed_driver.sh
```

The experimental `320x200` register sequence is derived from InnoMaker's
[CAM-OV9281RAW-V2 reference repository](https://github.com/INNO-MAKER/CAM-OV9281RAW-V2)
and adapted to Raspberry Pi's upstream `ov9282` driver.

Install the driver on the Pi with:

```bash
cd ~/openflight
scripts/setup/install_ov9281_high_speed_driver.sh
sudo reboot
```

The installer resolves the stock driver source matching the running Raspberry
Pi kernel, builds only the `ov9282` module against the installed kernel headers,
backs up the stock module, installs the patched module, and runs `depmod`. It
also grants members of the `video` group access to the live vertical sensor
position control. Kernel upgrades require rebuilding the module for the new
kernel before the custom modes are available again.

Restore the stock module with:

```bash
cd ~/openflight
scripts/setup/install_ov9281_high_speed_driver.sh --restore
sudo reboot
```

Do not unload or replace the active camera module while the camera pipeline is
running. Stop OpenFlight and reboot after installing or restoring a driver.

## Capture Modes

Measured modes on the test Pi 5:

| Mode | Profile (request) | Purpose | Observed cadence |
|---|---|---|---:|
| `640x400` | `full-300` (`300 FPS`) | Full-field setup and baseline capture | about `288 FPS` (`287.9` on 2026-11-02) |
| `640x200` | `strip-500` (`500 FPS`) | Wide impact strip | about `479 FPS` (`479.1` on 2026-11-02) |
| `640x200` | `strip-560` (`560 FPS`) | Wide impact strip at the driver's top rate (about `589 FPS` nominal); offered when the R8 mode reports at least `560 FPS` | about `536 FPS` expected |
| `320x200` | none: `crop-500`/`crop-600` withdrawn | Flat frames on this Pi (see below) | — |
| `640x100` | none | Timing experiment only; vertically fragile | not recommended |

The Camera tab offers the three profiles (`capture_runtime.supported_capture_modes`), each
only when the installed driver's R8 mode reports at least its rate; the app starts in
`full-300` unless started with another mode. Every mode delivers about 4.2 % below its
request. The delivered cadence is recorded in each capture's `metadata.json`. The whole
exposure ladder (up to `1250 us`) fits the `1786 us` frame period of `strip-560`. Both
strips are the same `640x200` sensor window, so the lens model, the marks, the hitting zone
and the strip offset carry over between them unchanged.

**Withdrawn (2026-11-02):** the app no longer offers `crop-500` and `crop-600`
(`320x200`), which earlier builds offered as the production mode. On this Pi the
patched `320x200` register set delivers flat frames (every pixel 16) although timing
and metadata look normal. A stored `320x200` setting starts as `full-300`
(`640x400`), and a flat frame is reported as `frame_flat` instead of driving
auto exposure. See the range report
`analysis/range-20261102/kamera/bericht-kamera.md`, section 1.

## Alignment Preview

Stop OpenFlight before opening the camera preview:

```bash
cd ~/openflight
CAMERA_WIDTH=640 \
CAMERA_HEIGHT=400 \
CAMERA_FPS=120 \
CAMERA_EXPOSURE_US=500 \
CAMERA_GAIN=2 \
scripts/hardware-test/preview_camera_alignment.sh
```

Use the preview to place the ball near the horizontal center and ensure the
clubhead and expected launch corridor remain within the vertical window. The
preview is rotated for the tested inverted camera mount.

Small residual camera roll can be corrected without resampling the high-speed
capture. Pass a clockwise-positive correction measured from a stationary
full-field preview:

```bash
scripts/start-kiosk.sh \
  --camera-capture \
  --camera-capture-roll-deg 2.8
```

OpenFlight applies this value to the Camera-tab preview and to the pixel-ray
geometry used by horizontal launch, Club Path, and Attack Angle. Saved raw
frames remain unchanged so calibration can be revised during offline replay.
Physical leveling is still preferred when practical because it preserves the
entire usable area of a compact sensor crop.

## Camera Tab Controls

When `--camera-capture` is enabled, the Camera tab keeps the preview visible
beside an operator-control column. Exposure and analogue gain are selected
automatically. The tab shows the current shutter, gain, impact-area brightness,
contrast, motion-blur risk, and whether camera-assisted analysis is eligible.
There is no manual exposure override in the UI.

The offered modes have a fixed field of view: align the camera mechanically.
**View up** and **View down**, which moved the sensor window in 10-pixel steps,
belonged to the withdrawn `320x200` crop and no longer appear. A strip offset
left in the driver parameter (`strip_y_offset`) still moves the `640x200` strip;
the app reads it from sysfs and maps the strip with it.

The Camera tab switches between the offered profiles (resolution and requested
frame rate) with a short controlled camera restart; the radars keep running.
Pre/post-trigger timing is shown as read-only capture provenance.

## Camera As The Angle Reference

The camera is the reference for both launch angles (`src/openflight/angle_reference.py`).
Toptracer is never part of it; it stays a validation reference only.

- **Vertical launch:** an accepted camera ball flight sets the vertical launch angle
  (source `camera`). The TI vertical is the fallback. Every shot with both adds TI minus
  camera to a per-session running median (last 20 pairs). Once at least 5 pairs agree to
  a robust spread of 1.0 deg, a TI-only shot is corrected by it (source
  `radar_calibrated`), provided the corrected value still passes the club/speed
  plausibility guard the raw TI passed; otherwise the shot keeps the TI as measured
  (source `radar`) and logs the rejected value (`calibration_rejected`). The uncorrected
  TI value stays in `iwr6843_vertical_deg`. The TI offset is not stable between sessions
  (+0.7 deg on 2026-10-03, +2.5 deg on 2026-10-05, +2.3 deg on 2026-10-25), so it is
  never a fixed number.
- **Horizontal launch:** the same running median of TI azimuth minus camera start
  direction is logged as a diagnostic but never applied: a TI-only shot keeps the fixed
  `--iwr6843-azimuth-offset-deg` (source `radar`), the last-resort fallback. The TI
  horizontal is compressed (about 0.29 deg per degree on 2026-10-25), so TI minus camera
  depends on where the pairs started, not on the setup: on 2026-10-25 the median learned
  on drivers starting left made the twelve TI-only 8-irons worse (RMSE 2.80 deg against
  1.70 deg with the fixed offset).
- The offsets reset on session clear, a profile switch and a camera geometry change
  (frame size, sensor window; `strip-500` and `strip-560` share theirs); a restart starts
  a new session without them.

### Ball and target line by tap

The Align view sets two marks, chosen with the chips **1 Ball** and **2 Ziel** above the
picture (on phones at the top of the settings). It always opens on the ball, also with
stored marks; a tap sets or moves the active chip's mark and keeps the step, so a second
tap corrects the same mark. Only the chips and **Ball neu** / **Ziel neu** change the step.
The marks' status heads the settings panel, with **Ball neu** and **Ziel neu** to set
either again.

The picture opens as a 2× close-up for tapping: half the frame's width and height, centred
across on the ball guide (the crosshair) with the guide at 80 % of the window's height, kept
inside the frame (with the guide at 50 % / 78 %: x 25–75 %, y 38–88 %). The window is fixed
while the view is open, and the button beside the chips switches to the whole picture
(**Ganzes Bild** / **Ausschnitt**); each open starts on the close-up again. Taps are mapped
through the window, so the server gets the same frame percentages in either view
(`ui/src/utils/alignCrop.ts`). The kiosk's setup reminder keeps the whole picture.

**Ball.** Tap the resting ball as it lies for the shot. The camera ball flight (live and
the low-light shadow) then searches the resting ball only within 64 px of the tap
(`departed_ball.MARK_WINDOW_HALF_PX`), in place of the hitting zone; a last ball, the
session tee anchor or a ground anchor outside that window never stand in for it, so an anchor that
drifted onto a tree crown (2026-10-25) cannot place the ball. Inside the window each shot's
ball is still located on its own (every ball is placed by hand), and the pose and the
start direction are computed from it as without a mark. The ball flight needs no lens
calibration for the mark. A new or cleared mark restarts the ball flight's tee trackers and ground
anchors. The trigger gate (`camera.trigger_check`) searches the ball in the same window
(`ball_source` `ball_mark`; its session tee anchor counts only inside the window), and the
departures it sees there feed that tee anchor (`tee_anchor.ANCHOR_SOURCES`). The chained
club delivery does not read the mark. The club-data job (below) does: an iron or wedge
without a departed ball and without a plausible tracker ball anchors its resting ball on the
mark, refined around the tap (`ball_anchor` `mark`, 2026-11-02 S1: the tracker put the
irons' balls on the tree line). A club that may be teed (woods and hybrids, the driver
included) never anchors on it, since around a tap on the ground the refine can latch onto
the tee stem; for every club the mark bounds the tracker's ball (two refine search radii,
50 px at a 16.4 px ball). For this the job needs a lens model for the clip's capture mode
(`--camera-intrinsics`): the mark is a pixel and takes the regulation ball's size from the
lens; without one the job does not use the mark, and the hitting zone alone bounds the
tracker's ball.

**Target.** Tap the target you aim at in the live picture: a flag, post or sign
on the range, at least 30 m away. The server undoes the preview's roll correction, then
undistorts the tapped pixel with the active lens model (`--camera-intrinsics`, required)
and derolls it in normalized coordinates: that is the target's azimuth in the camera
frame, shown with the time it was set. From then on every camera start direction is
measured from the line to the target, in each shot's own camera pose. The target replaces
`--camera-capture-horizontal-offset-deg`, and the yaw from the teed ball
(`--camera-capture-yaw-from-ball`) cancels, so the camera's yaw against the target line
no longer matters. The calculated camera club path, the arc path and the low point use the
same frame as the start direction, so face to path and the spin-axis class do not change
when a target is tapped; face to target and the path are then measured from the target
line. The TI horizontal fallback is not moved onto the target line; its
logged TI-minus-camera median restarts when the target is set or cleared. A shot whose
ball flight was measured against a target that was replaced while the shot was being
processed adds no pair and logs the target it was measured against. Without a tapped
target nothing changes.

The picture shows a ring on the ball, a ring on the target and a dashed line between
them. Both marks are saved per device setup in
`~/.config/openflight/camera-target-line.json` (version 2: `target_line` and
`ball_mark`; the version 1 file of older builds, the target alone, still loads) and
survive a restart. They are stored in canonical `640x400` coordinates, so they carry over
between `640x400` and the `640x200` strips (`strip-500`, `strip-560`) and to a new strip
offset (`angle_reference.carry_camera_marks`):

- the target keeps its direction (azimuth, elevation) and is re-projected into the new
  frame with that mode's lens model (`intrinsics.for_mode`);
- the ball mark keeps its spot; one that falls outside the strip is dropped alone
  (`ball_mark_outside_mode` in the session log);
- between the two strips nothing changes: they are the same frame.

Both marks are cleared only for a mount or orientation change (orientation, scaler crop,
roll correction, lens model or mount offsets) or for a mode without a known mapping (and
the target without a lens model for the new mode); tap them again after such a change.

Assumption: the target is far enough that the parallax between ball and camera does not
matter. The camera sits about 1.5 m behind the ball and a few centimetres to the side;
0.1 m of lateral offset is 0.19 deg at 30 m and 0.06 deg at 100 m.

### Roll together with the target

The plumb-line measurement of 2026-10-25 gave a camera roll of about +1.35 deg. Applied
alone (`--camera-capture-roll-deg 1.35`) it made the start directions agree worse with
Toptracer, because the camera yaw against the target line (a constant +1.0 to +1.4 deg)
was still uncorrected and had partly cancelled the roll error. Set the measured roll
together with a tapped target: restart with `--camera-capture-roll-deg 1.35` (the default
stays 0), open the Camera tab and tap the target. The roll change discards any target
saved under the old roll, so the target is always tapped in the derolled picture. Do not
change the roll mid-session without tapping the target again.

## Automatic Exposure

Lighting varies too much for one universal exposure. OpenFlight measures the
center-lower impact region and selects from camera settings validated to fit
inside the configured frame period. It also checks the area above it, where the
ball flies: when more than `10 %` of it is clipped white, a darker step is
tried as long as the impact region stays usable. A white sky hides the ball the
moment it leaves the tee; if the next darker step would leave the impact region
too dark, the controller goes back one step and keeps it.

At startup, the controller begins with the last good setting saved for the same
resolution and frame rate. If that setting is not usable, it can jump several
steps and recheck after `300 ms`, rather than waiting through one five-second
step at a time. It makes at most one adjustment per ladder step before shot
capture is armed. A usable setting is then locked, so consecutive replays use
the same image controls, and the lock is checked every `30 s` between shots: a
result that asks for light (**Lighting needed**) is re-measured at once, and a
usable setting is re-measured when the impact region looked too bright or too
dark at two checks in a row (the sun came out or went in). A single odd check,
such as someone passing the lens, changes nothing.

The last good setting is stored at:

```text
~/.config/openflight/camera-exposure.json
```

If the available lighting cannot produce a usable image, OpenFlight does not
stop the camera. Preview and raw frame capture continue, but camera-assisted
horizontal launch, Club Path, and Attack Angle are withheld for that shot.
Horizontal launch falls back to the IWR6843 path. The Camera tab reports
**Lighting needed** and **Radar fallback active** so the operator can add or
redirect light. No restart is needed: within about `30 s` of usable light the
camera re-measures, locks a new setting and analyses shots again. Shots taken
while it re-measures, and until the ring buffer holds only frames at the new
setting, stay on the radar fallback. At the range this matters because startup
only sees the first second after the app starts, often before the ball is in
place.

The standalone calibration sweep remains useful when diagnosing an unusual
lighting installation:

```bash
cd ~/openflight
uv run --no-project --python /usr/bin/python3 \
  python scripts/hardware-test/calibrate_camera_exposure.py \
  --rotate-180 \
  --fps 300 \
  --width 640 \
  --height 400 \
  --exposures-us 250,350,500,700,900,1100 \
  --gains 1,2,4 \
  --settle-ms 150
```

Prefer adding light to the hitting area over relying on long shutter times or
high gain. Both can reduce clubhead edge quality even when the preview appears
bright enough.

## Standalone Trigger Test

Use the clap-buffer test before starting OpenFlight:

```bash
cd ~/openflight
uv run --no-project --python /usr/bin/python3 \
  python scripts/hardware-test/test_camera_clap_buffer.py \
  --width 640 \
  --height 200 \
  --fps 560 \
  --pre-ms 150 \
  --post-ms 50 \
  --exposure-us 500 \
  --gain 2 \
  --rotate-180 \
  --captures 5
```

Check that every capture reports the expected pre/post frame counts, plausible
delivered FPS, and `gaps=0`.

## Running OpenFlight

Enable synchronized rolling capture with `--camera-capture`:

```bash
scripts/start-kiosk.sh \
  --debug \
  --iwr6843 \
  --camera-capture \
  --camera-capture-width 640 \
  --camera-capture-height 400 \
  --camera-capture-fps 300 \
  --camera-capture-pre-ms 150 \
  --camera-capture-post-ms 100 \
  --camera-capture-rotate-180 \
  --session-location home
```

The start-direction locator (`--camera-ball-locator disappeared`, the default) compares the
frames 15-45 after the trigger with the pre-swing frames, so the clip must run at least 23
frames past the trigger frame. That frame lands up to 6 frames after the pre-trigger count
(2026-10-25: +1 typical), so capture at least 29 post-trigger frames:
`--camera-capture-post-ms` of 101 at 288 fps or 52 at 560 fps (the server warns at startup
and after a camera-mode change when it is shorter; shots whose clip ends too early fall
back to the legacy locator). 51 frames (178 ms at 288 fps, 92 ms at 560 fps) give the
full window. The post-trigger milliseconds stay when the Camera tab switches the mode, so
the frame count follows the rate.

When capture is enabled, OpenFlight keeps a rolling pre-trigger frame buffer and freezes it
from the same sound-trigger event used by the radar pipeline.

## Shot Replay

A shot with a matched high-speed camera capture exposes **Replay** in the Live
header and a play control in its Shots row. Replay opens a full-screen,
touch-friendly 60 FPS slow-motion player with play/pause, restart, scrubbing,
looping, selectable `1x`, `0.5x`, `0.25x`, and `0.1x` playback speeds, and an
impact marker derived from the recorded trigger frame. Each replay carries its
saved-frame orientation, so the player applies the operator-facing left/right
correction only when needed and never flips a capture twice. Saved frames used
by camera geometry remain unchanged.

The browser-ready H.264 MP4 is intentionally lazy. Shot processing registers
only the existing `frames.npz`; OpenFlight does not run FFmpeg until the user
selects Replay. The first request creates `replay.mp4` atomically beside the raw
capture, and later requests reuse that file. Closing the player while it is
preparing does not affect the shot or raw frames. Preparation and playback
failures are shown as retryable player states, while server-side failures are
logged without interrupting launch-monitor operation. FFmpeg preparation is
serialized across replays to avoid simultaneous CPU and memory spikes on the
Raspberry Pi.

## Saved Artifacts

Camera captures are written under:

```text
~/openflight_sessions/<location>/camera/camera_<timestamp>_<sequence>/
```

Each capture contains:

- `frames.npz`: grayscale frames and per-frame timing/control metadata.
- `metadata.json`: delivered cadence, frame gaps, brightness, timing, settings,
  and the capture-time automatic-exposure decision.
- `first.pgm`: first buffered frame.
- `trigger.pgm`: frame nearest the hardware trigger.
- `last.pgm`: final post-trigger frame.
- `replay.mp4`: optional 60 FPS slow-motion video, created only after the first
  manual Replay selection and then cached.

The session JSONL contains a `camera_capture` entry linking the shot number to
the camera directory. Keep the JSONL, OPS capture, IWR6843 dump, and camera
directory together when copying a session for offline analysis.

## Troubleshooting

### No camera detected

1. Stop OpenFlight.
2. Power the Pi off.
3. Reseat both ends of the ribbon cable.
4. Boot and run `rpicam-hello --list-cameras`.

### `picamera2` import fails under `uv`

Raspberry Pi OS installs Picamera2 for the system Python. Run hardware scripts
with:

```bash
uv run --no-project --python /usr/bin/python3 python <script>
```

For OpenFlight itself, first confirm `python3-picamera2` is installed through
`apt`, then run `uv sync --extra camera`. The camera runtime exposes Raspberry
Pi OS's package directory to the OpenFlight environment automatically.

### Requested high-speed mode is missing

The custom module was not built for the running kernel, or a kernel update
restored the stock driver. Re-run the high-speed driver installer and reboot.

### Frames are dark

Verify that the lens cap is removed and that light reaches the clubhead path,
not just the stationary ball. If the Camera tab remains on **Lighting needed**
at the brightest validated setting, add or redirect light. Preview and raw
capture continue while radar-only measurements remain active.

### Highlights are solid white

Redirect intense light away from reflective surfaces and the ball. The
automatic controller will move darker, but clipped ball and club pixels cannot
provide reliable centroids even when the image looks bright enough to a person.

### Frame gaps increase

Stop preview applications, verify that only one process owns the camera, and
check the Pi for thermal throttling. Capture to RAM first; persistence happens
after the frame window freezes.

The per-shot camera computer vision (ball flight and club delivery, 8-10 s per
shot on a Pi 5) runs in one worker process at nice +10, so it no longer
competes with the frame callback for the server's GIL. `--camera-cv-in-process`
restores the old in-process behaviour for comparison or rollback. A worker that
crashes, fails to start or cannot pass a job's input or result is replaced and
that shot is computed in the server process, so no camera result is lost. An
exception the estimator itself raises is not a worker failure: it is logged as
the fusion error it was in-process, without recomputing the shot (it would only
raise again, holding the server's GIL for seconds), and the worker keeps
running. A shot's camera jobs share what is left of its
20 s enrichment deadline, counted from the shot, not from each job's start: a
worker still busy at that deadline is replaced, and once it has passed no
further camera job of that shot is sent to the worker or recomputed in-process
after a worker failure (club delivery: `rejected_shot_past_deadline`). That
shot was already published without camera data, so no next shot waits for work
whose result is dropped. With `--camera-cv-in-process` the jobs run to the end
as before.

The calculated club data ("berechnet", `camera/club_head_path.py` and
`club_data.py`) comes from the club-data job. For every clip eligible for ball
analysis it runs in the same worker once the shot has been handed to
publication, within a 6 s budget (the club-head path alone takes about 0.2-0.3 s
per clip on a Mac; the full job, with the contact time, the gate-free camera
departure, the path, the arc attack angle and the heel/toe impact, an estimated
5 s on a Pi 5; the per-stage timings are logged). Its result goes to the
session log as a `camera_club_head_path` entry (path, sigma, gate status,
contact time and its source, the camera departure, the resolved ball, the
attack angle, the impact and the D-plane face) and a `club_data` entry with the
gated shot fields.

`--club-data show` (default) also puts the gated values on the shot and sends a
`shot_update`: the shot is published with `club_data_status` `pending`, and the
job ends it as `done`, or `skipped` with `club_data_reason`, so no shot stays
pending. The screens show the path and the attack angle ("berechnet"; the
attack angle "unkalibriert") and the impact point (below) as a schematic face
(`ImpactFace`) over its zone word ("Ferse", "Mitte", "Spitze" or "unsicher", no
millimetres; the kiosk Live tile and the club card "Treffpunkt"): the dynamic and spin lofts,
the face, face to path, the spin-axis class and the low point stay in the shot
and the session log, since this hardware cannot measure them. No screen shows a spin
axis at all, whichever source it has. `--club-data log` only writes the session log; nothing reaches the shot
or the screens. `--golfer-handedness {right,left}` (default right; a simulator
that reports one, GSPro "Handed", overrides it) sets the toe side; a left-handed
golfer gets no calculated face yet. In either mode the club data never sets
`club_path_deg`, `spin_axis_deg` or `spin_axis_source`, and never reaches the
carry or the simulator export.

The contact time is the ball-flight time zero, except that a departure fit
later than the trigger minus the tee-to-microphone sound travel is physically
impossible; the entry then uses the acoustic estimate (trigger minus sound
travel, as the ball flight) and names the reason. The trigger stamp lags the
sound edge (callback dispatch, the exposure lock); where the edge log has the
kernel line-event time, or at least the callback's own stamp, the entry also
logs the lag, the same bound taken at the edge, which stamp gave it, and
whether the departure fit lies after that edge bound (`edge_bound_verdict`).
That verdict does not change the contact time: the edge stamps are still in
shadow, where the acoustic estimate should be referenced is still open, and
refusing a fit there would fall back to the trigger's acoustic estimate, later
than the refused fit.

A mat strike sounds before the ball leaves, so the acoustic estimate of an iron
off the mat can be early (2026-10-25: a median 1.68 ms on 9 of 13 such irons,
study `schlaegerdaten-replay/60-kontaktzeit`). The entry logs a shadow contact
time for that case and never uses it (`shadow_contact_time`, `t0_shadow_*`):
for a shot that is not a driver shot whose acoustic contact refused a departure
after the sound bound, that departure (the live fit, else the camera departure), when it lies
not before the acoustic estimate and before the trigger stamp. It may decide
only after a range test, and only while the session's tee control passes
(`tee_control_*`: of at least 5 driver departures at most 10 % more than 0.3 ms
after their sound bound; a teed ball cannot strike the mat). "Teed" in the
shadow and the tee control is the server's driver shot (the job's `teed`: the
club is the driver), not `ball_teed` (a tee the ball-flight locator saw): an iron
played off a tee gets a shadow and does not count for the tee control. The entry also
logs `path_last_frame_gap_ms`, the gap from the last usable path frame to
contact.

The `path_arc_*` fields log the path of the arc fit beside `path_deg`
(`analysis/range-20261102/schlaegerdaten-replay/70-bogenpfad`). Nothing shown or
calculated reads them: the study did not prove the arc path better under its
criteria, and a range session decides. It uses the attack angle's fit (no attack
gates, its own sigma terms), the path's sigma and range gates and the woods
ensemble's rule; `path_arc_window_deg` tells how far it hangs on the frame window.

`path_noise_sigma` logs the background's grey-level noise in the same region as the
path's segmentation, without clipped pixels and without the MAD's grid steps
(`analysis/range-20261102/schlaegerdaten-replay/80-kontrast-rauschen`). It changes
no value: the contrast limits stay fixed. It is there to compare other sites and light.

#### Impact point

The impact point (`camera/impact_point.py`, study
`analysis/treffpunkt-20261101`) is measured in the two frames that bracket
contact: frame L (0-3.5 ms before it) and frame P (the first after it, with the
resting-ball zone compared against the late frames and the departing ball
blanked from the shot's ball speed and launch angles). The heel-toe midpoint of
the head's lower half is interpolated between L and P to the contact time and
subtracted from the resting ball's centre: `impact_offset_mm`, + toward the toe
(right-handed), with `impact_sigma_mm` (1 sigma per shot, 2026-10-25: irons
2.6-2.9 mm, 7-wood 3.8, driver 5.3). Without a usable P it is the quadratic
through L-2 ... L, with a larger sigma. The millimetres always come from the
geometry: the regulation ball (42.67 mm) at the known camera-ball range through
the lens model, `impact_scale_mm_per_px` (16.4 px at 1.5 m, about 2.6 mm/px;
`scale_source` `geometry` in the log). Owner decision 2026-11-03: range balls
have the standard size and the ball sits at the measured 1.50 m, device and ball
on one level, so the size is taken, not measured. The earlier measurement of the
resting ball's pixel diameter (and its session median) read 12-15 px against the
lens's 16.4 px (2026-10-25: 14.2 px, i.e. 1.73 m; 2026-11-02 S1 #6: 12.3 px) and
is not trusted; it is gone. Without a lens model the expected size falls back
to the anchor ball's own (`geometry.reference_focal_px`; the log still says
`scale_source` `geometry`): on the Mac or the mock, and on the Pi too when it runs
without `--camera-intrinsics` or for a clip whose capture mode the lens model does
not cover (`server._capture_intrinsics` gives None). For a teed wood or the driver (`teed`: the driver,
or a tee the ball-flight locator saw under the ball) `impact_height_mm` is the
ball centre above the head's lower edge (driver 2026-10-25: +/-2.8 mm; it follows
the launch angle, rho +0.72).

`impact_method` says which estimator gave the value: `D` (the measurement across
contact) whenever it passed its gates (head length within 25 % of the club's
session median, L-to-P width within 20 %, |offset| <= 50 mm, sigma <= 10 mm),
else `E`, the older extrapolating estimator (`club_head_path._impact_location`),
brought to D's scale (`e_scale_ratio`, about 1 now that both use the known
range) and with its sigma widened by 4.9 mm (its
heel offset and scatter against D); never a mean of both. `E` rescues extreme
hits at the head's edge, where D's sigma is too large. `impact_toe_mm` is the
older name of the same offset.

`impact_zone` is `heel`, `center` or `toe`: center is |offset| < 6 mm for irons
and wedges, < 8 mm for woods, hybrids and the driver. These widths are
preliminary until a Trackman iO comparison: the silhouette midpoint is not the
face centre, and a fixed per-club offset of up to about +/-5 mm is still unknown.
The zone is the class with the most probability under N(offset, sigma^2) and
stays null below 0.68: on the 2026-10-25 replay that named a zone for 47 of 59
shown offsets (expected misclassification 12 % among them, from sigma alone).
Nothing feeds the impact point into the curve, the spin axis or the flight yet;
the session log keeps D's diagnostics (`impact_point` in `camera_club_head_path`).
It reuses the club-head path's head track when that ends in frame L and takes
about 0.06 s per clip on a Mac (median, p90 0.09 s; stage `impact_point`, about
a fifth of the job), inside the club-data budget; on a Pi it is not measured yet.

It runs on the shot-enrichment thread, after this shot's hand-off, so it never
delays its own shot. To stay out of the next shot's way it does not start while
another shot waits (`skipped_next_shot_queued`) or for a shot whose enrichment
missed its deadline (`skipped_shot_past_deadline`). A shot that arrives while it
runs still waits for it: up to the 6 s budget plus the cut-off of a hung worker
(up to 2 x 2 s). A job past its budget is cut off and logged as `timeout`; the
worker is then started again right away while no shot waits, so the next ball
flight does not pay the start; a lost worker (`worker_error`) is restarted the
same way. The restart runs in the background (`worker_restart_started`; how it
went goes to the server log), so it never holds the enrichment thread: a shot
that needs the worker while it starts waits only for the rest of the start, and
that wait counts toward its own deadline, a shot that does not need it does not wait. An exception of the estimator itself is logged as `job_error` and
leaves the worker running. An eligible clip the path cannot run on (no
frames, no IWR runtime or tee geometry, a ball flight that timed out or raised)
gets an entry with a `skipped_*` reason and no contact time, so the log has one
entry per eligible clip. With `--camera-cv-in-process` the shadow path does not
run (`skipped_cv_in_process`).

`--camera-buffer-count` (default 8, range 4-32) sets the Picamera2 request
buffers. Each 640x400 buffer takes about 0.9 MB of CMA memory (512 KB raw plus
384 KB YUV420), so 24 buffers need about 21.5 MB and 32 about 29 MB; check
`CmaFree` in `/proc/meminfo` before raising it. More buffers ride out longer
host stalls but only postpone drops when the host falls behind for longer, and
they lengthen the worst-case frame delivery the live camera trigger gate sees.

### Camera works once and then remains busy

Stop OpenFlight cleanly and look for stale `rpicam-*`, `ffplay`, or Python
processes. Reboot rather than hot-unloading the camera kernel module.

## Current Limitations

- The `200`-row view was only evaluated with 7-iron and 9-iron TrackMan shots (in the
  withdrawn `320x200` crop).
- Camera pose is not yet a complete metric calibration.
- Camera-assisted club path and attack angle remain experimental. The Club
  Path and Attack Angle tiles show only accepted values: unaccepted IWR6843
  candidates and the chained camera estimate (about -20 deg on every backlit
  range shot) stay in the session log. An empty tile keeps an IWR6843
  rejection reason as its caption, never a candidate status.
- A kernel update requires rebuilding the custom module.
- The tested down-the-line view cannot independently measure downrange speed;
  OPS remains necessary for converting image-plane motion into delivery angles.
