# LIS3DH Inclinometer Setup

OpenFlight can use an LIS3DH accelerometer mounted to the enclosure base to
measure whether the rig is level. The measured enclosure pitch is added to the
fixed IWR6843 antenna mounting angle for each shot.

This is an optional feature. It is disabled unless `--inclinometer` is passed.
The server flag requires `--iwr6843`; use the standalone tools when testing the
LIS3DH without the radar.

If the sensor is missing, moving, stale, or temporarily unreadable, OpenFlight
keeps the shot and uses the configured IWR6843 tilt without an enclosure
correction.

## What To Buy

The hardware used for the validated OpenFlight build is:

| Part | Product |
|------|---------|
| LIS3DH breakout | [Adafruit LIS3DH Triple-Axis Accelerometer, product 2809](https://www.adafruit.com/product/2809) |
| Solderless cable kit | [4-pin JST SH 1.0 mm STEMMA QT/Qwiic cable kit on Amazon](https://www.amazon.com/Connector-Compatible-Development-Sensors-Drivers/dp/B0GJPRX4YT) |
| Mounting | Thin double-sided mounting tape or nonconductive standoffs |

The Amazon cable kit is the exact cable product used in this build. It includes
JST-SH-to-female-Dupont cables, so the breakout can connect directly to the
Raspberry Pi GPIO header without soldering pins onto the LIS3DH.

Do not buy a bare LIS3DH chip. Use a breakout board with the regulator, I2C
pull-ups, and STEMMA QT/Qwiic connectors already installed.

## How Compensation Works

The LIS3DH is mounted flat in the enclosure, parallel to the case bottom and
the case top face, not on the tilted TI radar board. OpenFlight keeps the three
quantities separate:

```text
calibrated enclosure pitch = raw LIS3DH pitch + inclinometer zero offset
effective IWR tilt = configured IWR tilt + calibrated enclosure pitch
```

For this build (OpenFlightCaseV3, CAD rev L):

- The TI boresight sits `10.0` degrees above the case top face (CAD nominal;
  the antenna plane stands at 80 degrees to the top face and leans back). The
  top face, the case bottom and the LIS3DH board are parallel.
- The LIS3DH zero offset was measured at `+1.40` degrees on 2026-10-01
  (phone level at 0 degrees in both directions on the enclosure).

The `10.0` degrees is a CAD value; print and assembly tolerances are estimated
at 0.3 to 0.5 degrees. Measure each physical build, ideally with the
[phone tilt calibration](#calibrate-the-ti-tilt-with-a-phone).

## Power Down Before Wiring

Shut down the Pi and remove power before connecting or moving GPIO wires. A
misplaced 3.3 V lead can short the Pi, cause reboot loops or a black screen, and
potentially damage the Pi or sensor.

## Wiring

The two STEMMA QT connectors on the Adafruit LIS3DH are electrically identical.
Use whichever port gives the cable the cleanest route through the enclosure.

The purchased cable kit uses these colors:

| Cable color | Signal | Raspberry Pi physical pin | Pi signal |
|-------------|--------|---------------------------|-----------|
| Red | `VIN` / `3.3V` | **17** | 3.3 V power |
| Black | `GND` | **20** | Ground |
| Blue | `SDA` | **3** | GPIO2 / I2C SDA |
| Yellow | `SCL` | **5** | GPIO3 / I2C SCL |

```text
LIS3DH STEMMA QT                         Raspberry Pi GPIO header

Red     VIN / 3.3V  ------------------>  physical pin 17 (3.3V)
Black   GND         ------------------>  physical pin 20 (GND)
Blue    SDA         ------------------>  physical pin 3  (GPIO2/SDA)
Yellow  SCL         ------------------>  physical pin 5  (GPIO3/SCL)
```

> [!WARNING]
> Use physical pin numbers exactly as shown. GPIO/BCM numbers are a different
> numbering system. Do not connect the LIS3DH to a 5 V GPIO-header pin.

Cable colors are not a universal standard. If using a different cable, verify
each conductor against the breakout labels before powering the Pi.

The I2C bus can share Pi power and ground pins with other devices. It does not
need a dedicated 3.3 V or ground pin as long as the shared connection is secure.

## Mounting And Axis Direction

Mount the LIS3DH firmly and flat against the bottom of the enclosure. Avoid
thick foam tape that lets the board flex independently from the enclosure.

For the Adafruit board:

- X runs along the long direction between the two STEMMA QT connectors.
- Y runs across the short direction of the board.
- Z points perpendicular to the board.

This build mounts the board with its **long side (X) along the shot
direction**, so **X is the pitch axis**: OpenFlight computes
`pitch = atan2(-X, hypot(Y, Z))`, verified by a forward/backward tilt test on
2026-09-30. Negative X, and so positive pitch, means the enclosure tilts
backward (ball side up, boresight up). Y/roll is recorded but is not currently
applied to the launch-angle correction.

Before fixing the board permanently, run the readout script and gently tilt the
enclosure backward. Confirm that the reported pitch becomes more positive.

## Enable I2C On The Pi

Enable the Pi header I2C interface and reboot:

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

After reconnecting over SSH, verify that bus 1 exists:

```bash
ls -l /dev/i2c-1
```

To scan the bus, install `i2c-tools` if needed and run:

```bash
sudo apt-get install -y i2c-tools
i2cdetect -y 1
```

The default LIS3DH address is `0x18`, so the scan should contain `18`:

```text
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
10: -- -- -- -- -- -- -- -- 18 -- -- -- -- -- -- --
```

## Install Software

From the OpenFlight checkout:

```bash
uv sync
```

The Linux installation includes `smbus2`, which the LIS3DH driver uses to talk
to `/dev/i2c-1`.

## Verify Raw Readings

Run the standalone hardware readout before starting the full application:

```bash
uv run python scripts/hardware-test/read_lis3dh.py
```

Example output:

```text
LIS3DH detected on I2C-1 at 0x18
X=+0.024g  Y=+0.001g  Z=+0.974g  Raw=-1.41deg  Enclosure=-1.41deg
```

Useful options:

```bash
# Print 10 samples and exit
uv run python scripts/hardware-test/read_lis3dh.py --count 10

# Preview the calibrated enclosure and effective TI angles
uv run python scripts/hardware-test/read_lis3dh.py \
  --zero-offset-deg 1.4 \
  --iwr6843-tilt-deg 10

# Probe a non-default address while troubleshooting
uv run python scripts/hardware-test/read_lis3dh.py --address 0x19
```

## Calibrate The Zero Offset

The zero offset corrects only the LIS3DH's own mounting and sensor bias. It does
not include the TI board's fixed mounting angle.

1. Put the complete enclosure on a firm, stationary surface.
2. Measure that surface in the radar tilt direction with a phone level app.
3. Keep the enclosure still and run the calibration helper.
4. Pass the phone reading to `--reference-pitch`, including its sign.

For a surface measured at `+0.1` degrees:

```bash
uv run python scripts/hardware-test/calibrate_lis3dh.py --reference-pitch 0.1
```

The script averages 50 samples and prints a value such as:

```text
Raw pitch mean: -1.401deg (std dev 0.071deg)
Reference pitch: +0.100deg
Recommended flag: --inclinometer-zero-offset +1.501
```

Record that value in the launch command. The current implementation does not
write a calibration file.

## Measure The Fixed IWR6843 Tilt

`--iwr6843-tilt-deg` is the TI boresight angle relative to the enclosure
base, not relative to the floor under the rig. For this case the CAD value is
`10` degrees:

```bash
--iwr6843-tilt-deg 10
```

Do not add the current floor slope to this number. The LIS3DH supplies that
runtime correction. A saved phone calibration (next section) replaces this
value.

## Calibrate The TI Tilt With A Phone

The phone measures how the case top face is tilted; the server turns that into
the TI tilt and applies it at once, without a restart.

```text
case tilt theta = (pitch_A - pitch_B) / 2      (positive = ball side higher)
radar tilt      = 10.0 + theta
configured tilt = radar tilt - calibrated LIS3DH pitch at calibration time
```

Because OpenFlight adds the LIS3DH pitch to the configured tilt for every shot,
each shot then uses `radar tilt + (pitch at the shot - pitch at calibration)`:
the LIS3DH zero offset cancels out, and the sensor only tracks changes since
the calibration. Without the inclinometer the configured tilt is the radar tilt.

Procedure:

1. Set the rig up as for hitting (tripod, aim), with OpenFlight running. Level
   the side-to-side roll with the tripod head first.
2. Remove the phone case and wipe the case top face. Check that the front edge
   (display bezel) does not stand proud of the top face.
3. **Position A:** lay the phone flat, screen up, in the middle of the top face,
   long side along the shot direction, **top end toward the ball**. Do not press
   on it. Keep it still while the app averages at least 30 samples.
4. **Position B:** turn the phone **180 degrees** (top end away from the ball),
   same spot, and measure again.
5. The app sends both averaged gravity vectors. The server rejects the
   measurement and changes nothing when a position has fewer than 30 samples,
   a pitch standard deviation above 0.5 degrees, a gravity magnitude outside
   0.9-1.1 g, or the phone is not lying flat; when the case tilt is above
   8 degrees; when the phone's own bias, `(pitch_A + pitch_B) / 2`, is above
   3 degrees; or when the side rolls of A and B do not mirror each other
   (roll bias above 3 degrees). With the inclinometer on, it waits up to 2 s for
   a stable LIS3DH reading and refuses the calibration while the rig moves.

The phone's own bias cancels out between A and B. A forgotten turn cannot be
detected on a level case (both positions then read the same); on a tilted case
it shows up as a large phone bias.

The result is saved to `~/.config/openflight/iwr6843_phone_orientation.json`
(atomically) and logged as a `config_change` session-log line. Removing it
falls back to `--iwr6843-tilt-deg`, or to the calibration JSON, at once.

At startup the configured tilt comes from, in order:

1. the saved phone calibration,
2. `--iwr6843-tilt-deg`,
3. the calibration JSON.

The startup log prints the chosen source, and `session_start` records it as
`iwr6843.tilt_source` (`phone`, `cli` or `calibration_json`) together with the
calibration record. The value also survives the TI USB power-cycle recovery at
startup. Recalibrate after switching `--inclinometer` on or off: a calibration
taken with the LIS3DH excludes the case pitch, one taken without it includes
it. OpenFlight warns at startup when the two do not match.

The response also reports `inclinometer_minus_phone_deg`, the calibrated
LIS3DH pitch minus the phone's case tilt. It should be near 0; a larger value
points to a wrong LIS3DH zero offset.

## Start OpenFlight

Example production startup:

```bash
scripts/start-kiosk.sh \
  --iwr6843 \
  --iwr6843-tilt-deg 10 \
  --inclinometer \
  --inclinometer-zero-offset 1.4
```

At startup, OpenFlight prints the raw enclosure pitch, calibrated pitch,
configured IWR tilt, and effective IWR tilt. It then samples the LIS3DH at 10 Hz.

For each shot, OpenFlight:

1. Selects the newest stable LIS3DH snapshot timestamped before impact.
2. Rejects snapshots after motion, sensor errors, or more than two seconds old.
3. Adds calibrated enclosure pitch to the configured IWR tilt.
4. Uses a per-shot copy of the IWR calibration so shared calibration is never
   mutated.
5. Continues with the configured IWR tilt if correction cannot be applied.

## Session Logging

The `session_start` entry records:

- Whether the inclinometer initialized.
- I2C bus and address.
- Sampling rate and zero offset.
- Startup reading or initialization error.

Each `shot_detected` entry records:

- Raw and calibrated pitch.
- X, Y, Z, and gravity magnitude.
- Reading timestamp and age at impact.
- Stability/application status.
- Configured and effective IWR tilt.

Common statuses are `stable`, `moving`, `stale`, `sensor_error`, and
`no_stable_preimpact_reading`.

## Troubleshooting

### Green LED Is On, But `0x18` Is Missing

The LED confirms power only. It does not prove SDA/SCL communication.

1. Confirm `/dev/i2c-1` exists.
2. Confirm I2C is enabled, then reboot.
3. Recheck blue to physical pin 3 and yellow to physical pin 5.
4. Confirm black is on ground and red is on 3.3 V.
5. Reseat both ends of the JST-SH cable.
6. Try the other identical STEMMA QT port on the LIS3DH.
7. Run `i2cdetect -y 1` again.

If the scan shows `0x19`, the board's address-selection input is pulled high.
Restore the default `0x18` configuration or use `--address 0x19` only with the
standalone readout while diagnosing it. The OpenFlight runtime currently expects
`0x18`.

### `WHO_AM_I expected 0x33`

OpenFlight reached an I2C device at the selected address, but it did not identify
as an LIS3DH. Check for another device using `0x18`, verify the breakout model,
and inspect the bus with `i2cdetect -y 1`.

### Permission Denied On `/dev/i2c-1`

Add the OpenFlight user to the `i2c` group, then log out and back in or reboot:

```bash
sudo usermod -aG i2c "$USER"
sudo reboot
```

### Pi Reboots, Shows A Black Screen, Or Runs The Fan At Full Speed

Power down immediately and inspect the GPIO connections. This commonly points
to a shifted connector, a power-to-ground short, or a wire on the wrong physical
pin. Remove the LIS3DH, confirm the Pi boots normally, and reconnect one signal
at a time with power removed.

### Pitch Sign Is Backward

Tilt the enclosure backward while watching the readout. Pitch should become more
positive. If it becomes negative, rotate the LIS3DH mounting so that X runs
along the shot direction with negative X pointing as in the validated build. A zero offset corrects bias; it should not be used to
hide a reversed axis.

### Pitch Is Noisy Or Shots Report `moving`

- Tighten the sensor mounting.
- Replace flexible foam tape with thin rigid tape or standoffs.
- Keep the cable from pulling on the breakout.
- Wait about one second after repositioning the rig before taking a shot.
- Check that gravity magnitude is close to `1.0g` in the readout.

### Shots Report `stale`

OpenFlight has not received a recent stable reading. Check the I2C connection,
sensor mounting, and logs for sample errors. The shot is still processed using
the configured IWR tilt.

### Effective Tilt Looks Wrong

Confirm the inputs have distinct meanings:

- `--iwr6843-tilt-deg`: TI boresight angle relative to the enclosure (a saved
  phone calibration replaces it).
- `--inclinometer-zero-offset`: LIS3DH calibration bias.
- Runtime enclosure pitch: current enclosure angle relative to level.

Do not put the floor slope into `--iwr6843-tilt-deg`, and do not put the TI board
mounting angle into `--inclinometer-zero-offset`.

## Current Limitations

- Pitch compensation uses the LIS3DH X direction.
- Y/roll is logged but not yet applied to full 3D boresight compensation.
- Sensor bus, address, and calibration are command-line/runtime settings rather
  than a persisted rig configuration file.
