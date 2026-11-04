#!/usr/bin/env python3
"""
M1 bench test: OPS243-A firmware 1.3.3 speed trigger and J3 pin 1 on a Pi GPIO.

The launch-monitor code for the mic-free trigger (``--shot-trigger
ops-speed-shadow`` / ``ops-speed``) stays off until this test has answered
what no local document says: does the speed trigger work on our board (persisted
GC, 20 ksps, no GC at runtime), how long does pin 1 stay HIGH, what does the
board send when it trips, do ST/SM survive a dump, and does HOST_INT still work.

Before you start:
    * Stop the app (it owns the radar UART and the GPIOs).
    * Measure J3 pin 1 with a meter BEFORE wiring it to the Pi: idle level,
      level while tripped (3.3 V, not 5 V), push-pull or open drain. A wrong
      level can damage the Pi input.
    * Wiring: J3 pin 1 -> Pi GPIO22 (header pin 15), ground on pin 14.

What it does (never GC or GS; A! only with --save-to-flash):
    1. ?V and ?? -> the results file.
    2. PI, US, P3, S=<ksps>, SM<n>, ST-<mph>, S#<n>, ?? (the driver's
       prepare_internal_speed_trigger). --upstream-settings also sends what
       upstream restores after GC (buffer 128, X=2, R-, R><mph>), to see
       whether those settings shift the trip.
    3. Per try: re-arm (PA, S#n, PA), wave a hand or swing; records pin 1 rise
       and fall with kernel ticks, the bytes between rise and dump marker (the
       trip message), marker time, dump end, ?? after the dump, and where in the
       buffer the speed first passed ST compared with the trip point.
    4. Summary: trips with a pin edge, HIGH width against the post-trigger
       span (192 ms at S#2 / 20 ksps), rise -> first byte, settings after dumps.
    --host-int-test: pulse BCM27 (HOST_INT) for 20 ms, expect a dump, and
       report whether pin 1 toggled with it.

Restore: the speed-trigger off command once it is known
(ops243.SPEED_TRIGGER_OFF_COMMANDS); until then power-cycle the OPS, which
clears ST/SM because nothing was saved with A!.

Usage:
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20 --threshold-mph 5
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20 --upstream-settings
    uv run python scripts/hardware-test/test_ops_speed_pin.py --magnitude 20 --host-int-test
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# Add src to path so we can import openflight
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from openflight import ops243  # noqa: E402
from openflight.edge_timing import edge_ticks  # noqa: E402
from openflight.ops243 import (  # noqa: E402
    SPEED_TRIGGER_MARKERS,
    Direction,
    OPS243Radar,
    OPSFirmwareError,
)
from openflight.ops_speed import button_kwargs, pin_idle_message, resolve_pin_pull  # noqa: E402
from openflight.rolling_buffer.processor import RollingBufferProcessor  # noqa: E402
from openflight.trigger_gate import (  # noqa: E402
    OPS_BUFFER_SEGMENTS,
    OPS_SEGMENT_SAMPLES,
    pre_trigger_ms,
)

DEFAULT_PORT = "/dev/ttyAMA0"
DEFAULT_PIN = 22
DEFAULT_HOST_INT_PIN = 27
DEFAULT_HOST_INT_PULSE_MS = 20.0
# A hand wave is a few mph; the launch monitor uses 25 (--ops-speed-threshold-mph).
DEFAULT_THRESHOLD_MPH = 5
DEFAULT_PRE_TRIGGER = 2
DEFAULT_SAMPLE_RATE_KSPS = 20
DEFAULT_TRIES = 10
DEFAULT_TIMEOUT_S = 30.0


def expected_high_ms(pre_trigger_segments: int, sample_rate_ksps: float) -> float:
    """Post-trigger span of an ``S#n`` split: how long pin 1 should stay HIGH."""
    return (OPS_BUFFER_SEGMENTS - pre_trigger_segments) * OPS_SEGMENT_SAMPLES / sample_rate_ksps


class EdgeRecorder:
    """Both edges of pin 1 with wall time, monotonic time and kernel ticks."""

    def __init__(self, device):
        self.device = device
        self._lock = threading.Lock()
        self.events: list[dict] = []
        device.when_pressed = lambda: self._record("rise")
        device.when_released = lambda: self._record("fall")

    def _record(self, kind: str) -> None:
        event = {
            "kind": kind,
            "wall": time.time(),
            "monotonic_ns": time.monotonic_ns(),
            "ticks": edge_ticks(self.device),
        }
        with self._lock:
            self.events.append(event)

    def since(self, monotonic_ns: int) -> list[dict]:
        """Edges recorded at or after ``monotonic_ns``."""
        with self._lock:
            return [event for event in self.events if event["monotonic_ns"] >= monotonic_ns]

    @property
    def is_active(self) -> bool:
        return bool(self.device.is_active)

    def close(self) -> None:
        self.device.when_pressed = None
        self.device.when_released = None
        self.device.close()


def open_recorder(pin: int, active: str, pull: Optional[str], factory=None) -> EdgeRecorder:
    """Claim the pin (no debounce, both edges); refuse a pin active at idle."""
    pull = resolve_pin_pull(active, pull)
    if factory is None:
        from openflight.gpio_factory import ensure_lgpio_pin_factory

        ensure_lgpio_pin_factory()
        from gpiozero import Button  # pylint: disable=import-error

        factory = Button
    recorder = EdgeRecorder(factory(pin, **button_kwargs(active, pull)))
    if recorder.is_active:
        recorder.close()
        raise SystemExit(f"ERROR: {pin_idle_message(pin, active, pull)}. Check the wiring.")
    return recorder


def analyze_capture(processor: RollingBufferProcessor, response: str, threshold_mph: float) -> dict:
    """Trip point and the first frame whose outbound speed reached ``threshold_mph``."""
    capture = processor.parse_capture(response)
    if capture is None:
        return {"parsed": False}
    timeline = processor.process_overlapping(capture)
    crossing = next(
        (
            reading
            for reading in timeline.readings
            if reading.is_outbound and reading.speed_mph >= threshold_mph
        ),
        None,
    )
    result = {
        "parsed": True,
        "trigger_offset_ms": round(capture.trigger_offset_ms, 2),
        "peak_outbound_mph": round(
            max((r.speed_mph for r in timeline.readings if r.is_outbound), default=0.0), 1
        ),
        "first_crossing_center_ms": None,
        "trip_shift_ms": None,
    }
    if crossing is not None:
        center = processor._reading_center_ms(crossing)  # pylint: disable=protected-access
        result["first_crossing_center_ms"] = round(center, 2)
        # Positive: the board tripped after the speed first passed ST in its own buffer.
        result["trip_shift_ms"] = round(capture.trigger_offset_ms - center, 2)
    return result


def _first(events: list[dict], kind: str, after_ns: int = 0) -> Optional[dict]:
    return next(
        (event for event in events if event["kind"] == kind and event["monotonic_ns"] >= after_ns),
        None,
    )


def run_try(  # pylint: disable=too-many-arguments
    radar: OPS243Radar,
    recorder: EdgeRecorder,
    processor: RollingBufferProcessor,
    *,
    index: int,
    pre_trigger_segments: int,
    threshold_mph: float,
    timeout_s: float,
    on_armed: Callable[[], None],
) -> dict:
    """Re-arm, wait for one trip, and measure it."""
    rearm_start = time.monotonic()
    rearmed = radar.rearm_rolling_buffer(pre_trigger_segments=pre_trigger_segments)
    record: dict = {
        "try": index,
        "rearm_ok": rearmed,
        "rearm_s": round(time.monotonic() - rearm_start, 3),
    }
    mark_ns = time.monotonic_ns()
    on_armed()
    response = radar.wait_for_hardware_trigger(
        timeout=timeout_s, capture_markers=SPEED_TRIGGER_MARKERS
    )
    dump_end = time.time()
    # A short pause so the fall after a dump that streams first is recorded too.
    time.sleep(0.05)
    events = recorder.since(mark_ns)
    rise = _first(events, "rise")
    fall = _first(events, "fall", rise["monotonic_ns"]) if rise else None
    first_byte = radar.last_hardware_trigger_first_byte_timestamp
    preamble = radar.last_hardware_trigger_preamble
    preamble_ts = radar.last_hardware_trigger_preamble_timestamp
    record.update(
        dumped=bool(response),
        dump_bytes=len(response),
        rise_wall=rise["wall"] if rise else None,
        rise_ticks=rise["ticks"] if rise else None,
        fall_ticks=fall["ticks"] if fall else None,
        high_ms=(
            round((fall["monotonic_ns"] - rise["monotonic_ns"]) / 1e6, 3) if rise and fall else None
        ),
        edges=len(events),
        marker_wall=first_byte,
        rise_to_first_byte_ms=(
            round((first_byte - rise["wall"]) * 1000.0, 3) if rise and first_byte else None
        ),
        # Everything read before the dump marker since the re-arm; negative
        # timing means bytes came before the rise (e.g. command replies).
        trip_message=preamble.decode("ascii", errors="replace") if preamble else "",
        rise_to_trip_message_ms=(
            round((preamble_ts - rise["wall"]) * 1000.0, 3) if rise and preamble_ts else None
        ),
        dump_s=round(dump_end - first_byte, 3) if first_byte else None,
    )
    if response:
        record["settings_after_dump"] = radar._send_command("??")  # pylint: disable=protected-access
        record.update(analyze_capture(processor, response, threshold_mph))
    return record


def host_int_test(
    radar: OPS243Radar,
    recorder: EdgeRecorder,
    *,
    pre_trigger_segments: int,
    pulse: Callable[[], None],
    timeout_s: float = 5.0,
) -> dict:
    """Pulse HOST_INT once: did the board dump, and did pin 1 toggle with it?"""
    radar.rearm_rolling_buffer(pre_trigger_segments=pre_trigger_segments)
    mark_ns = time.monotonic_ns()
    pulse_wall = time.time()
    pulse()
    response = radar.wait_for_hardware_trigger(timeout=timeout_s)
    time.sleep(0.05)
    events = recorder.since(mark_ns)
    rise = _first(events, "rise")
    first_byte = radar.last_hardware_trigger_first_byte_timestamp
    return {
        "dumped": bool(response),
        "dump_bytes": len(response),
        "pin_toggled": rise is not None,
        "pulse_to_rise_ms": round((rise["wall"] - pulse_wall) * 1000.0, 3) if rise else None,
        "pulse_to_first_byte_ms": (
            round((first_byte - pulse_wall) * 1000.0, 3) if first_byte else None
        ),
        "edges": len(events),
    }


def _stats(values: list) -> Optional[dict]:
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {
        "n": len(values),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def summarize(tries: list[dict], *, expected_high: float) -> dict:
    """What M1 needs from the tries."""
    dumped = [record for record in tries if record.get("dumped")]
    high = _stats([record.get("high_ms") for record in tries])
    return {
        "tries": len(tries),
        "dumps": len(dumped),
        "dumps_with_pin_rise": sum(1 for record in dumped if record.get("rise_wall") is not None),
        "rises_without_dump": sum(
            1 for record in tries if record.get("rise_wall") is not None and not record["dumped"]
        ),
        "high_ms": high,
        "expected_high_ms": round(expected_high, 3),
        "high_minus_expected_ms": (
            round(high["median"] - expected_high, 3) if high is not None else None
        ),
        "rise_to_first_byte_ms": _stats([record.get("rise_to_first_byte_ms") for record in tries]),
        "trip_messages": sorted(
            {record["trip_message"] for record in dumped if record["trip_message"]}
        ),
        "settings_after_dump": sorted(
            {record["settings_after_dump"] for record in dumped if "settings_after_dump" in record}
        ),
        "trip_shift_ms": _stats([record.get("trip_shift_ms") for record in dumped]),
        "rearm_s": _stats([record.get("rearm_s") for record in tries]),
    }


def print_summary(summary: dict) -> None:
    print()
    print("=" * 64)
    print("  SUMMARY")
    print("=" * 64)
    print(f"  Dumps:                {summary['dumps']}/{summary['tries']} tries")
    print(f"  ... with a pin rise:  {summary['dumps_with_pin_rise']}/{summary['dumps']}")
    print(f"  Pin rises, no dump:   {summary['rises_without_dump']}")
    high = summary["high_ms"]
    if high:
        print(
            f"  HIGH width:           median {high['median']} ms (min {high['min']}, "
            f"max {high['max']}); expected {summary['expected_high_ms']} ms"
        )
    else:
        print("  HIGH width:           no complete pulse seen")
    lag = summary["rise_to_first_byte_ms"]
    if lag:
        print(
            f"  Rise -> first byte:   median {lag['median']} ms (min {lag['min']}, max {lag['max']})"
        )
    shift = summary["trip_shift_ms"]
    if shift:
        print(f"  Trip after crossing:  median {shift['median']} ms (in the board's own buffer)")
    print(f"  Trip messages:        {summary['trip_messages'] or 'none seen'}")
    print(f"  ?? after dumps:       {len(summary['settings_after_dump'])} distinct reply(ies)")
    print("=" * 64)


def apply_upstream_settings(radar: OPS243Radar, threshold_mph: int) -> list[str]:
    """What upstream PR #221 restores after GC: buffer 128, X=2, R-, R><mph>."""
    radar.set_buffer_size(128)
    radar.set_fft_size(2)
    radar.set_direction_filter(Direction.OUTBOUND)
    radar.set_min_speed_filter(threshold_mph)
    return ["S(", "X=2", "R-", f"R>{threshold_mph}"]


def restore(radar: OPS243Radar, *, saved: bool) -> str:
    """Idle the board and switch the speed trigger off, or say how to."""
    radar._send_command("PI")  # pylint: disable=protected-access
    if ops243.SPEED_TRIGGER_OFF_COMMANDS:
        radar.disable_internal_speed_trigger()
        return "Speed trigger switched off (" + " ".join(ops243.SPEED_TRIGGER_OFF_COMMANDS) + ")."
    if saved:
        return (
            "The speed trigger is now SAVED in flash (A!). The off command is unknown: restore "
            "the launch-monitor settings with test_rolling_buffer_persist.py --setup."
        )
    return "Power-cycle the OPS to clear ST/SM (nothing was saved with A!)."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="M1 bench test for the OPS243-A 1.3.3 speed trigger and J3 pin 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"Radar port (default {DEFAULT_PORT})")
    parser.add_argument("--pin", type=int, default=DEFAULT_PIN, help="BCM input for J3 pin 1 (22)")
    parser.add_argument("--active", choices=("high", "low"), default="high", help="Pin 1 polarity")
    parser.add_argument(
        "--pull", choices=("down", "up", "off"), help="Pull (default: down for high, up for low)"
    )
    parser.add_argument(
        "--magnitude", type=int, required=True, help="SMnn magnitude gate (1-2000; device units)"
    )
    parser.add_argument(
        "--threshold-mph",
        type=int,
        default=DEFAULT_THRESHOLD_MPH,
        help=f"Temporary ST-<mph> for hand waves (default {DEFAULT_THRESHOLD_MPH})",
    )
    parser.add_argument("--pre-trigger", type=int, default=DEFAULT_PRE_TRIGGER, help="S#n (2)")
    parser.add_argument(
        "--sample-rate-ksps", type=int, default=DEFAULT_SAMPLE_RATE_KSPS, help="S=nn (20)"
    )
    parser.add_argument("--power", type=int, default=3, help="Transmit power level 0-7 (3)")
    parser.add_argument("--tries", type=int, default=DEFAULT_TRIES, help="Hand waves (10)")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="Seconds to wait per try (30)"
    )
    parser.add_argument(
        "--upstream-settings",
        action="store_true",
        help="Also send buffer 128, X=2, R-, R><mph> (upstream's post-GC restore)",
    )
    parser.add_argument(
        "--resend-on-rearm",
        action="store_true",
        help="Re-send SM/ST before the final PA of each re-arm",
    )
    parser.add_argument(
        "--host-int-test", action="store_true", help="Pulse HOST_INT once at the end"
    )
    parser.add_argument("--host-int-pin", type=int, default=DEFAULT_HOST_INT_PIN, help="BCM27")
    parser.add_argument(
        "--host-int-pulse-ms", type=float, default=DEFAULT_HOST_INT_PULSE_MS, help="20 ms"
    )
    parser.add_argument("--out", type=Path, default=Path("."), help="Folder for the results file")
    parser.add_argument(
        "--save-to-flash",
        action="store_true",
        help="Send A! at the end (asks first). Persists ST/SM and every other setting.",
    )
    return parser


def _host_int_pulse(pin: int, pulse_ms: float) -> Callable[[], None]:
    from gpiozero import DigitalOutputDevice  # pylint: disable=import-error

    device = DigitalOutputDevice(pin, initial_value=False)

    def pulse() -> None:
        try:
            device.on()
            time.sleep(pulse_ms / 1000.0)
            device.off()
        finally:
            device.close()

    return pulse


def _confirm_save() -> bool:
    print()
    print("A! writes the CURRENT settings (ST/SM included) to the OPS flash.")
    answer = input("Type SAVE to send A!, anything else to skip: ")
    return answer.strip() == "SAVE"


def main(argv: Optional[list[str]] = None) -> int:  # pylint: disable=too-many-branches
    args = build_parser().parse_args(argv)
    results: dict = {
        "started": datetime.now().isoformat(),
        "args": vars(args) | {"out": str(args.out)},
    }
    out_path = args.out / f"ops_speed_pin_{datetime.now():%Y%m%d_%H%M%S}.json"

    recorder = open_recorder(args.pin, args.active, args.pull)
    radar = OPS243Radar(port=args.port)
    saved = False
    try:
        radar.connect(probe_firmware=True)
        results["firmware"] = radar.firmware_version
        results["settings_before"] = radar._send_command("??")  # pylint: disable=protected-access
        print(f"Firmware: {radar.firmware_version}")
        try:
            results["setup"] = radar.prepare_internal_speed_trigger(
                threshold_mph=args.threshold_mph,
                magnitude=args.magnitude,
                pre_trigger_segments=args.pre_trigger,
                sample_rate_ksps=args.sample_rate_ksps,
                transmit_power=args.power,
            )
        except OPSFirmwareError as exc:
            print(f"ERROR: {exc}")
            return 2
        if args.upstream_settings:
            results["upstream_settings"] = apply_upstream_settings(radar, args.threshold_mph)
            results["settings_after_upstream"] = radar._send_command("??")  # pylint: disable=protected-access
        radar.speed_trigger_resend_on_rearm = args.resend_on_rearm

        processor = RollingBufferProcessor(sample_rate=args.sample_rate_ksps * 1000)
        expected = expected_high_ms(args.pre_trigger, args.sample_rate_ksps)
        print(
            f"S#{args.pre_trigger} at {args.sample_rate_ksps} ksps: "
            f"{pre_trigger_ms(args.pre_trigger, args.sample_rate_ksps):.1f} ms before / "
            f"{expected:.1f} ms after the trip (expected HIGH width)"
        )
        tries = []
        for index in range(1, args.tries + 1):
            record = run_try(
                radar,
                recorder,
                processor,
                index=index,
                pre_trigger_segments=args.pre_trigger,
                threshold_mph=args.threshold_mph,
                timeout_s=args.timeout,
                on_armed=lambda i=index: print(
                    f"\nTry {i}/{args.tries}: wave a hand in front of the radar "
                    f"(> {args.threshold_mph} mph, {args.timeout:.0f} s)..."
                ),
            )
            tries.append(record)
            print(
                f"  dump={record['dumped']} high={record['high_ms']} ms "
                f"rise->first byte={record['rise_to_first_byte_ms']} ms "
                f"trip message={record['trip_message']!r}"
            )
        results["tries"] = tries
        results["summary"] = summarize(tries, expected_high=expected)
        print_summary(results["summary"])

        if args.host_int_test:
            print("\nHOST_INT test: keep still, pulsing BCM%d..." % args.host_int_pin)
            results["host_int"] = host_int_test(
                radar,
                recorder,
                pre_trigger_segments=args.pre_trigger,
                pulse=_host_int_pulse(args.host_int_pin, args.host_int_pulse_ms),
            )
            print(f"  {results['host_int']}")

        if args.save_to_flash and _confirm_save():
            radar.save_config()
            saved = True
        results["saved_to_flash"] = saved
    finally:
        try:
            # Only a board that got ST/SM needs restoring.
            if radar.serial is not None and "setup" in results:
                results["restore"] = restore(radar, saved=saved)
                print(f"\n{results['restore']}")
        finally:
            recorder.close()
            radar.disconnect()
            args.out.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
            print(f"Results: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
