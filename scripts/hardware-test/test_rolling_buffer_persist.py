#!/usr/bin/env python3
"""
Test the OmniPreSense persistent rolling buffer workaround.

The OPS243-A has a bug where the HOST_INT pin mode switches unexpectedly
when transitioning from normal mode (GS) to rolling buffer mode (GC) at
runtime. The fix from OmniPreSense:

  1. Enter rolling buffer mode (GC)
  2. Save to persistent memory (A!)
  3. Power cycle the board
  4. Board starts in rolling buffer mode — HOST_INT works correctly
  5. After each capture, re-arm with PA or GC

This script tests two things:
  Phase 1: Configure and persist rolling buffer mode (one-time setup)
  Phase 2: Verify hardware trigger works after power cycle

Usage:
    # Phase 1: Configure and save (will prompt for power cycle)
    uv run python scripts/test_rolling_buffer_persist.py --setup

    # Phase 2: Test hardware trigger (run after power cycle)
    uv run python scripts/test_rolling_buffer_persist.py --test

    # Both phases interactively
    uv run python scripts/test_rolling_buffer_persist.py
"""

import argparse
import sys
import time

sys.path.insert(0, "src")

from openflight.ops243 import OPS243Radar, is_uart_port
from openflight.rolling_buffer.processor import RollingBufferProcessor


def _connect(port: str, baud: int) -> OPS243Radar:
    """Connect, reporting the transport so UART runs are unambiguous."""
    radar = OPS243Radar(port=port if port else None, **({"uart_baud": baud} if baud else {}))
    radar.connect()
    print(f"  Connected on: {radar.port}")
    if is_uart_port(radar.port):
        dump_s = radar.DUMP_BYTES / radar.bytes_per_second
        print(f"  Transport: UART at {radar.baud} baud (dump ~{dump_s:.1f}s)")
    else:
        print("  Transport: USB")

    info = radar.get_info()
    print(f"  Firmware: {info.get('Version', 'unknown')}")
    print()
    return radar


def _power_cycle_instructions(radar: OPS243Radar) -> str:
    """Power-cycle wording for the transport actually in use.

    On the GPIO header there is no USB cable to pull, and no reset line is
    wired, so the 5V jumper is the only way to restart the board.
    """
    if is_uart_port(radar.port):
        return (
            "  Disconnect 5V from OPS J3 pin 9, wait 3 seconds, reconnect it.\n"
            "  (No USB cable and no reset line on this wiring — the 5V jumper\n"
            "  is the power cycle. Leave TX/RX and ground connected.)"
        )
    return "  Unplug the USB cable, wait 3 seconds, plug it back in."


def phase_setup(port: str, pre_trigger: int, sample_rate: int, baud: int):
    """Phase 1: Configure rolling buffer mode and save to persistent memory."""
    print("=" * 60)
    print("  Phase 1: Configure & Persist Rolling Buffer Mode")
    print("=" * 60)
    print()

    print("Connecting to radar...")
    radar = _connect(port, baud)

    # The driver owns this sequence (GC/PA/S=/S#/PA then A!) so the saved
    # configuration cannot drift from what the server sends at runtime.
    # Over UART the negotiated baud is already active here, so A! captures
    # it too if the firmware persists baud.
    print(f"[1/2] Entering rolling buffer mode and saving (S#{pre_trigger}, S={sample_rate})...")
    radar.persist_rolling_buffer_mode(
        pre_trigger_segments=pre_trigger, sample_rate_ksps=sample_rate
    )
    print("  Done — settings saved")

    print("[2/2] Disconnecting...")
    cycle_text = _power_cycle_instructions(radar)
    radar.disconnect()
    print("  Done")

    print()
    print("=" * 60)
    print("  ACTION REQUIRED: Power cycle the radar board now!")
    print()
    print(cycle_text)
    print("  The LED should come on in rolling buffer mode.")
    print()
    print("  Then run:")
    print("    uv run python scripts/hardware-test/test_rolling_buffer_persist.py --test")
    print("=" * 60)
    print()


def phase_test(port: str, pre_trigger: int, timeout: float, baud: int):
    """Phase 2: Test hardware trigger after power cycle."""
    SEGMENT_DURATION_MS = 128 / 30000 * 1000  # ~4.27ms per segment at 30ksps
    pre_ms = pre_trigger * SEGMENT_DURATION_MS
    post_ms = (32 - pre_trigger) * SEGMENT_DURATION_MS

    print("=" * 60)
    print("  Phase 2: Test Hardware Trigger (Post Power Cycle)")
    print("=" * 60)
    print()
    print(f"  Pre-trigger:  S#{pre_trigger} ({pre_ms:.1f}ms)")
    print(f"  Post-trigger: {32 - pre_trigger} segments ({post_ms:.1f}ms)")
    print()

    print("Connecting to radar...")
    radar = _connect(port, baud)

    # Don't send GC/GS — the board should already be in rolling buffer mode
    # Activate sampling and set trigger split
    print(f"Activating sampling (PA, S#{pre_trigger}) — board should already be in rolling buffer mode...")
    radar.serial.reset_input_buffer()
    radar.serial.write(b"PA")
    time.sleep(0.1)
    radar.serial.write(f"S#{pre_trigger}\r".encode())
    radar.serial.flush()
    time.sleep(0.1)
    radar.serial.write(b"PA")
    time.sleep(0.2)
    radar.serial.reset_input_buffer()
    print("  Ready")
    print()

    processor = RollingBufferProcessor()

    print("-" * 60)
    print("Waiting for hardware trigger (HOST_INT)...")
    print("Make a sound near the sensor or trigger HOST_INT manually.")
    print("(Ctrl+C to quit)")
    print("-" * 60)
    print()

    trigger_count = 0
    successful_captures = 0

    try:
        while True:
            print(f"[{trigger_count + 1}] Waiting for trigger (timeout={timeout}s)...")

            response = radar.wait_for_hardware_trigger(timeout=timeout)
            # Transfer time is measured from the first byte, not from when we
            # started waiting, so it reflects wire rate rather than how long
            # it took you to make a noise.
            first_byte_ts = radar.last_hardware_trigger_first_byte_timestamp
            transfer_s = (time.time() - first_byte_ts) if first_byte_ts else None

            if not response:
                print("  Timeout — no trigger received")
                print("  If this keeps happening, the persist workaround may not have worked.")
                print("  Try: uv run python scripts/test_rolling_buffer_persist.py --setup")
                print()
                continue

            trigger_count += 1
            print(f"  TRIGGER RECEIVED! ({len(response)} bytes)")

            if response and '"I"' in response and '"Q"' in response:
                capture = processor.parse_capture(response)
                if capture:
                    print(f"  I/Q samples: {len(capture.i_samples)} I, {len(capture.q_samples)} Q")
                    if transfer_s:
                        kbps = len(response) / transfer_s / 1000.0
                        print(
                            f"  Dump transfer: {len(response)} bytes in "
                            f"{transfer_s:.2f}s ({kbps:.1f} KB/s)"
                        )

                    timeline = processor.process_standard(capture)
                    outbound = [r for r in timeline.readings if r.is_outbound]
                    outbound_fast = [r for r in outbound if r.speed_mph >= 15.0]

                    if outbound_fast:
                        peak = max(r.speed_mph for r in outbound_fast)
                        print(f"  SWING DETECTED: peak {peak:.1f} mph ({len(outbound_fast)} readings)")
                        successful_captures += 1
                    else:
                        print(f"  No swing (false trigger). {len(outbound)} outbound readings.")
                else:
                    print("  WARNING: Failed to parse I/Q data")
            else:
                print("  WARNING: Response missing I/Q data")
                print(f"  Response preview: {response[:200]}...")

            # Re-arm with trigger split setting
            print(f"  Re-arming (S#{pre_trigger})...")
            radar.rearm_rolling_buffer(pre_trigger_segments=pre_trigger)

            print(f"  Stats: {successful_captures}/{trigger_count} valid captures")
            print()

    except KeyboardInterrupt:
        print()
        print()
        print("=" * 60)
        print("  SESSION SUMMARY")
        print("=" * 60)
        print(f"  Total triggers received: {trigger_count}")
        print(f"  Successful captures: {successful_captures}")
        if trigger_count > 0:
            print(f"  Success rate: {successful_captures/trigger_count*100:.1f}%")

        if trigger_count == 0:
            print()
            print("  No triggers received. Check:")
            print("  1. Did you power cycle after --setup?")
            print("  2. Is the SEN-14262 GATE wired to HOST_INT?")
            print("  3. Is the sound sensor sensitivity adjusted?")
        print("=" * 60)

    finally:
        print()
        print("Disconnecting...")
        radar.disconnect()
        print("Done.")


def main():
    parser = argparse.ArgumentParser(
        description="Test OmniPreSense persistent rolling buffer workaround",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The OPS243-A HOST_INT pin has a bug when switching modes at runtime.
The workaround is to save rolling buffer mode to persistent memory and
power cycle. After that, hardware triggers work correctly.

Examples:
    # One-time setup (configure + save + power cycle)
    uv run python scripts/test_rolling_buffer_persist.py --setup

    # Test after power cycle
    uv run python scripts/test_rolling_buffer_persist.py --test

    # Interactive (both phases with prompts)
    uv run python scripts/test_rolling_buffer_persist.py
        """,
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="Phase 1 only: configure rolling buffer and save to persistent memory"
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Phase 2 only: test hardware trigger (run after power cycle)"
    )
    parser.add_argument(
        "--port",
        help="Serial port for radar (auto-detect USB if not specified; "
             "pass /dev/ttyAMA0 for the GPIO UART wiring)",
    )
    parser.add_argument(
        "--baud", type=int, default=None,
        help=f"Target UART baud (default {OPS243Radar.DEFAULT_UART_BAUD}). "
             "Ignored over USB.",
    )
    parser.add_argument(
        "--pre-trigger", "-p", type=int, default=12,
        help="Pre-trigger segments S#n, 0-32 (default: 12)"
    )
    parser.add_argument(
        "--sample-rate", "-s", type=int, default=30,
        help="Sample rate in ksps (default: 30)"
    )
    parser.add_argument(
        "--timeout", "-t", type=float, default=30.0,
        help="Timeout waiting for trigger in seconds (default: 30)"
    )

    args = parser.parse_args()

    if args.setup:
        phase_setup(args.port, args.pre_trigger, args.sample_rate, args.baud)
    elif args.test:
        phase_test(args.port, args.pre_trigger, args.timeout, args.baud)
    else:
        # Interactive: run both phases
        phase_setup(args.port, args.pre_trigger, args.sample_rate, args.baud)
        input("Press Enter after power cycling the radar...")
        print()
        phase_test(args.port, args.pre_trigger, args.timeout, args.baud)


if __name__ == "__main__":
    main()
