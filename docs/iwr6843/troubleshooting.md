---
icon: lucide/wrench
---

# IWR6843 Troubleshooting

Start with the symptom shown in the terminal. Avoid changing estimator settings
until power, ports, firmware, config, and geometry are verified.

| Symptom | Likely cause | Action |
|---|---|---|
| `no IWR6843 CLI found` | Wrong USB interface, board still in flash mode, missing functional RESET, stale serial owner, or unstable power | Set functional switches, press RESET, verify interface `00`, stop serial processes, then retry with explicit `--iwr6843-port` |
| `GPIO busy` | Another kiosk, calibration, or shot-test process owns BCM17 | Stop the old process; use `pgrep -af` and `sudo fuser -v /dev/gpiochip*` to locate it |
| `captureFormat` or `phaseCaptureCfg` rejected | Older firmware is flashed | Flash the configurable release, reset in functional mode, and retry either supported profile |
| Bootloader probe returns no response | Wrong CP2105 port or RESET occurred before the script opened UART | Use Enhanced/UARTA, rerun the probe, type `READY`, then RESET only when prompted |
| Flash fails after `Erasing existing SFLASH` | Transfer was interrupted after the old image was erased | Leave the board in flash mode and rerun the complete flash; the ROM bootloader is still available |
| Server starts only after unplugging TI | Board was not reset cleanly, a prior dump was still streaming, or USB/power wedged | Stop the old process, press RESET in functional mode, wait for the port, then reconnect USB only if needed |
| `short IWR6843 dump` | Interrupted UART transfer, process shutdown during dump, or wrong firmware format | Let the active dump finish, restart, and confirm 732,812 bytes for wide or 549,764 bytes for dense |
| Dense `stats` accumulates `hwa_missed` or `iq8_overrun` | The 2 ms processing budget is not being sustained | Stop using the capture for measurements, reset, and return to the wide profile while investigating |
| Clap produces `rejected_by_ball_tracker` | A clap has no moving ball range track | Expected for trigger testing; confirm the dump completed, then hit a ball |
| `rejected_track_quality` | A ball-like track was found but it was too thin, noisy, inconsistent, or net-contaminated | Verify geometry and aim; inspect the debug dump before relaxing acceptance gates |
| `rejected_missing_tdm_sign` | The ball track was usable, but the TX timing evidence did not resolve a trustworthy correction sign | Keep the estimated UI angle, inspect the debug dump, and verify signal quality before changing gates |
| All UI angles are estimated | TI captures are absent, unmatched to OPS, or rejected by LCMF | Run with `--debug`, inspect `iwr6843_capture`, and check the reported rejection reason |
| `iwr6843_capture` says `accepted`, but the shot angle is estimated; Debug panel shows `TI REJECTED` with an angle | `ti_launch_implausible`: the LCMF vertical angle failed the club/speed launch guardrail (outside 0–45° at or below the expected launch, or too far above it). `iwr6843_capture` is logged before this check | Check `shot_detected.launch_angle_vertical_rejection_reason` and the `[SERVER] IWR6843 vertical angle … rejected` warning, which names the rule that fired; then inspect the TI channel components in the debug dump (a channel stuck at a grid edge is a typical cause) |
| OPS reports no data | Wrong OPS port, missing power, WiFi OPS connected through unsupported receive-only J3 UART, or non-WiFi UART wired incorrectly | For a WiFi OPS use the externally powered USB hub; otherwise verify `/dev/ttyAMA0`, power, shared ground, and crossed TX/RX |
| Either radar disconnects when both run | Insufficient USB power or unstable cabling | Use OPS GPIO power or a hub with its own external supply; verify the hub supply is connected and sized for both radars |
| Angles are consistently shifted | Tilt, antenna orientation, radar height, ball height, or tee distance is wrong | Re-measure all geometry from the antenna center and common floor reference |
| Dump file is missing from the session | OpenFlight was not launched with `--debug` | Re-run in debug mode when raw capture retention is required |

If the firmware itself must be rebuilt rather than flashed from the checked-in
binary, continue with the [firmware developer guide](../development/firmware.md).

## Automatic recovery by USB power cycle (Pi 5, opt-in)

A wedged TI firmware (HWA stall, or no reply to CLI commands) only recovers when
the radar loses power; a Pi restart keeps USB powered. On a Raspberry Pi 5,
OpenFlight can cut USB power itself with `uhubctl` when started with
`--iwr6843-usb-power-cycle` (off by default):

- **At startup**, a TI initialization that fails with a wedge (or finds no TI CLI)
  power-cycles USB once and retries. If that fails too, the splash screen shows
  the usual "shut the Pi down" guidance.
- **Mid-session**, two consecutive HWA-stall dumps, or one dump that gets no
  payload and whose CLI then stays silent on a resync, power-cycle USB and
  reconfigure the radar, at most twice per session. Sound-trigger edges keep
  reaching the OPS and camera during the recovery; those shots keep their OPS
  data and record "TI radar restarting (USB power cycle)" as their TI error. If
  the recovery fails, the session goes on without TI data and every shot records
  the "shut the Pi down" guidance as its TI error.

The Pi 5 USB ports are ganged, so **all four ports lose power together: the USB
touchscreen restarts too** (about 6 s of black screen). Use the stable by-id
path for `--iwr6843-port` (for example
`/dev/serial/by-id/usb-Silicon_Labs_CP2105_Dual_USB_to_UART_Bridge_Controller_<serial>-if00-port0`);
OpenFlight waits up to 15 s for it to reappear (without `--iwr6843-port`, for
any `/dev/ttyUSB*` plus 1 s to settle).

### What else loses power

A power cycle drops every USB device, not only the TI. OpenFlight therefore
limits the feature by setup:

| Setup | Startup retry | Mid-session recovery |
|---|---|---|
| OPS connected on the GPIO UART (`/dev/ttyAMA*`, `/dev/serial0`, `/dev/ttyS*`), auto-detected or via `--port`, no K-LD7, no USB storage | yes | yes |
| OPS connected over USB (for example `/dev/ttyACM0`) | yes (the OPS connects after the TI) | no |
| OPS not connected yet | yes | no |
| K-LD7 radars (`--kld7`, `--kld7-horizontal`) | yes (they connect after the TI) | no |
| A mounted USB storage device (USB root disk or stick) | no | no |
| sudo does not allow the `uhubctl` commands | no | no |

The mid-session check reads the port of the OPS that is actually connected,
right before each recovery, so the auto-detected GPIO UART of the standard Pi
build qualifies. When it blocks a recovery, OpenFlight logs one WARNING per
session (`TI recovery by USB power cycle blocked: <reason>`) and keeps the
usual "shut the Pi down" guidance. `--mock` (no OPS hardware) counts like a UART
OPS. The sudo and USB-storage checks run once at startup.

OpenFlight runs as a normal user and calls `sudo -n /usr/sbin/uhubctl`, which
never prompts. Allow exactly the four commands with a sudoers drop-in
`/etc/sudoers.d/openflight-usb-power` (mode `0440`) containing:

```text
pi ALL=(root) NOPASSWD: /usr/sbin/uhubctl -l 2 -a 0, /usr/sbin/uhubctl -l 4 -a 0, /usr/sbin/uhubctl -l 2 -a 1, /usr/sbin/uhubctl -l 4 -a 1
```

Install and validate it:

```bash
sudo install -m 0440 openflight-usb-power /etc/sudoers.d/openflight-usb-power
sudo visudo -cf /etc/sudoers.d/openflight-usb-power
```

OpenFlight checks the rules at startup with `sudo -n -l` for each of the four
commands. Without the drop-in it logs `--iwr6843-usb-power-cycle is set but sudo
does not allow uhubctl without a password` and never cuts USB power in that
session, so a wedged TI falls back to the manual guidance.
