"""
Trigger strategies for rolling buffer capture.

Defines different methods for determining when to capture the rolling buffer.
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, List, Optional

from ..ops243 import SPEED_TRIGGER_MARKERS
from .processor import RollingBufferProcessor
from .types import IQCapture

if TYPE_CHECKING:
    from ..ops243 import OPS243Radar
    from ..ops_speed import OpsSpeedConfig

# Incremented per capture for log sequencing (not shot number)

logger = logging.getLogger("openflight.rolling_buffer.trigger")


class TriggerStrategy(ABC):
    """
    Base class for trigger strategies.

    A trigger strategy determines when to capture the rolling buffer.
    Different strategies trade off between simplicity, reliability, and efficiency.
    """

    MIN_VALID_OUTBOUND_MPH = 15.0

    def __init__(self, pre_trigger_segments: int = 12):
        self._diagnostics: List[dict] = []
        self.pre_trigger_segments = pre_trigger_segments

    def drain_diagnostics(self) -> List[dict]:
        """Return and clear accumulated diagnostic entries.

        Diagnostics are accumulated during wait_for_trigger() calls.
        The monitor drains these after each call to log and emit them.
        """
        diagnostics = self._diagnostics
        self._diagnostics = []
        return diagnostics

    def _append_diagnostic(
        self,
        accepted: bool,
        reason: str,
        response_bytes: int = 0,
        total_readings: int = 0,
        outbound_readings: int = 0,
        inbound_readings: int = 0,
        peak_outbound_mph: float = 0.0,
        peak_inbound_mph: float = 0.0,
        all_outbound_speeds: Optional[List[float]] = None,
        all_inbound_speeds: Optional[List[float]] = None,
        peak_outbound_magnitude: float = 0.0,
        peak_inbound_magnitude: float = 0.0,
        trigger_latency_ms: Optional[float] = None,
        capture_first_byte_timestamp: Optional[float] = None,
    ):
        """Append a diagnostic entry for the current trigger event."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "accepted": accepted,
            "reason": reason,
            "response_bytes": response_bytes,
            "total_readings": total_readings,
            "outbound_readings": outbound_readings,
            "inbound_readings": inbound_readings,
            "peak_outbound_mph": peak_outbound_mph,
            "peak_inbound_mph": peak_inbound_mph,
            "all_outbound_speeds": all_outbound_speeds or [],
            "all_inbound_speeds": all_inbound_speeds or [],
            "peak_outbound_magnitude": peak_outbound_magnitude,
            "peak_inbound_magnitude": peak_inbound_magnitude,
        }
        if trigger_latency_ms is not None:
            entry["trigger_latency_ms"] = trigger_latency_ms
        if capture_first_byte_timestamp is not None:
            entry["capture_first_byte_timestamp"] = capture_first_byte_timestamp
        self._diagnostics.append(entry)

    def _summarize_capture_activity(
        self,
        processor: RollingBufferProcessor,
        capture: IQCapture,
    ) -> dict:
        """Summarize movement in a capture before accepting a sound trigger."""
        timeline = processor.process_standard(capture)
        all_readings = timeline.readings
        all_outbound = [r for r in all_readings if r.is_outbound]
        all_inbound = [r for r in all_readings if not r.is_outbound]
        outbound_speeds = [r.speed_mph for r in all_outbound]
        inbound_speeds = [r.speed_mph for r in all_inbound]
        valid_outbound = [r for r in all_outbound if r.speed_mph >= self.MIN_VALID_OUTBOUND_MPH]

        return {
            "total_readings": len(all_readings),
            "outbound_readings": len(all_outbound),
            "inbound_readings": len(all_inbound),
            "peak_outbound_mph": max(outbound_speeds, default=0),
            "peak_inbound_mph": max(inbound_speeds, default=0),
            "all_outbound_speeds": outbound_speeds,
            "all_inbound_speeds": inbound_speeds,
            "peak_outbound_magnitude": max((r.magnitude for r in all_outbound), default=0),
            "peak_inbound_magnitude": max((r.magnitude for r in all_inbound), default=0),
            "valid_outbound_count": len(valid_outbound),
            "valid_peak_outbound_mph": max(
                (r.speed_mph for r in valid_outbound),
                default=0,
            ),
        }

    def _append_activity_diagnostic(
        self,
        summary: dict,
        *,
        accepted: bool,
        reason: str,
        response_bytes: int,
        trigger_latency_ms: Optional[float] = None,
        capture_first_byte_timestamp: Optional[float] = None,
    ):
        """Append a diagnostic entry using capture-activity summary fields."""
        self._append_diagnostic(
            accepted=accepted,
            reason=reason,
            response_bytes=response_bytes,
            total_readings=summary["total_readings"],
            outbound_readings=summary["outbound_readings"],
            inbound_readings=summary["inbound_readings"],
            peak_outbound_mph=summary["peak_outbound_mph"],
            peak_inbound_mph=summary["peak_inbound_mph"],
            all_outbound_speeds=summary["all_outbound_speeds"],
            all_inbound_speeds=summary["all_inbound_speeds"],
            peak_outbound_magnitude=summary["peak_outbound_magnitude"],
            peak_inbound_magnitude=summary["peak_inbound_magnitude"],
            trigger_latency_ms=trigger_latency_ms,
            capture_first_byte_timestamp=capture_first_byte_timestamp,
        )

    @abstractmethod
    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for trigger condition and capture buffer.

        Args:
            radar: Connected OPS243Radar instance in rolling buffer mode
            processor: Processor for parsing capture response
            timeout: Maximum time to wait for trigger

        Returns:
            IQCapture if triggered and captured, None if timeout or error
        """
        pass

    @abstractmethod
    def reset(self):
        """Reset trigger state for next capture."""
        pass


class SpeedTriggeredCapture(TriggerStrategy):
    """
    Speed-triggered rolling buffer capture per OmniPreSense recommendation.

    This implements the manufacturer's recommended approach for golf:
    1. Run in fast speed detection mode (~150-200Hz report rate)
    2. When outbound speed >20mph detected, immediately switch to rolling buffer
    3. Capture ball impact and flight with S#0 (no pre-trigger history)

    Advantages over polling:
    - Much faster trigger response (~5-6ms vs 300ms+ polling)
    - Captures club speed in speed mode, ball in rolling buffer
    - Minimal data loss during mode switch

    Per manufacturer:
    "You'll lose a little data (~5-6ms) from the initial club speed detection
    time while the sensor determines there's probably a golf swing event to
    capture with the Rolling Buffer. But assuming the club speed detected to
    ball impact is around 20-40ms, that should be ok."
    """

    def __init__(
        self,
        min_trigger_speed_mph: float = 20.0,
        min_ball_speed_mph: float = 35.0,
        trigger_to_capture_delay_ms: float = 15.0,
        pre_trigger_segments: int = 0,  # Speed trigger defaults to 0 (no pre-trigger)
    ):
        """
        Initialize speed-triggered capture.

        Args:
            min_trigger_speed_mph: Minimum speed to trigger capture (default 20mph)
            min_ball_speed_mph: Minimum ball speed to consider valid shot (default 35mph)
            trigger_to_capture_delay_ms: Delay after trigger before capture (default 15ms)
                This allows ball impact to happen before we dump the buffer.
            pre_trigger_segments: Pre-trigger segments (default 0 for speed trigger)
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.min_trigger_speed_mph = min_trigger_speed_mph
        self.min_ball_speed_mph = min_ball_speed_mph
        self.trigger_to_capture_delay_ms = trigger_to_capture_delay_ms
        self._last_trigger_speed: float = 0
        self._needs_reconfigure = True

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for speed trigger, switch to rolling buffer, and capture.

        Flow:
        1. Configure radar for fast speed detection (if needed)
        2. Poll for speed readings at ~150-200Hz
        3. When speed >= threshold detected:
           a. Record club speed
           b. Switch to rolling buffer mode (GC + S#0)
           c. Wait for ball impact (~15-25ms)
           d. Trigger capture (S!)
        4. Return to speed detection mode for next shot
        """
        # Configure for speed trigger mode if needed
        if self._needs_reconfigure:
            radar.configure_for_speed_trigger()
            self._needs_reconfigure = False
            # Clear any buffered data from mode switch
            if radar.serial:
                radar.serial.reset_input_buffer()
            time.sleep(0.1)

        start_time = time.monotonic()
        logger.info(
            "[TRIGGER] Waiting for speed trigger >= %.1f mph...", self.min_trigger_speed_mph
        )

        while (time.monotonic() - start_time) < timeout:
            # Non-blocking speed read
            reading = radar.read_speed_nonblocking()

            if reading and reading.speed >= self.min_trigger_speed_mph:
                # Speed detected - this is likely the club
                self._last_trigger_speed = reading.speed
                trigger_time = time.perf_counter()

                logger.info(
                    "[TRIGGER] Speed trigger: %.1f mph %s detected, switching to rolling buffer...",
                    reading.speed,
                    reading.direction.value,
                )

                # Immediately switch to rolling buffer mode
                radar.switch_to_rolling_buffer(sample_rate=processor.SAMPLE_RATE)

                # Wait for ball impact
                # Club to ball is typically 20-40ms, we wait a portion of that
                delay_sec = self.trigger_to_capture_delay_ms / 1000.0
                time.sleep(delay_sec)

                # Capture the rolling buffer
                response = radar.trigger_capture(timeout=5.0)
                capture = processor.parse_capture(response)

                # Calculate timing
                capture_time = time.perf_counter()
                total_delay_ms = (capture_time - trigger_time) * 1000
                logger.info("[TRIGGER] Buffer captured %.1fms after trigger", total_delay_ms)

                if capture:
                    # Validate capture has ball speed
                    timeline = processor.process_standard(capture)
                    outbound = [
                        r
                        for r in timeline.readings
                        if r.is_outbound and r.speed_mph >= self.min_ball_speed_mph
                    ]

                    if outbound:
                        peak = max(r.speed_mph for r in outbound)
                        logger.info("[TRIGGER] Ball detected: %.1f mph in capture", peak)

                        # Mark for reconfigure on next call
                        self._needs_reconfigure = True
                        return capture
                    else:
                        logger.info(
                            "[TRIGGER] No speed >= %.1f mph in capture", self.min_ball_speed_mph
                        )

                # Reconfigure for speed mode and continue
                self._needs_reconfigure = True
                radar.configure_for_speed_trigger()
                if radar.serial:
                    radar.serial.reset_input_buffer()

            # Brief sleep to avoid busy-waiting but stay responsive
            time.sleep(0.002)  # 2ms = 500Hz poll rate

        logger.info("[TRIGGER] Speed trigger timeout")
        return None

    def reset(self):
        """Reset trigger state and mark for reconfiguration."""
        self._last_trigger_speed = 0
        self._needs_reconfigure = True

    @property
    def last_trigger_speed(self) -> float:
        """Get the speed that triggered the last capture (likely club speed)."""
        return self._last_trigger_speed


class SoundTrigger(TriggerStrategy):
    """
    Hardware sound trigger using SparkFun SEN-14262.

    IMPORTANT: Rolling buffer mode must be configured BEFORE using this trigger.
    Call radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode()
    before calling wait_for_trigger().

    Wiring: SEN-14262 GATE → OPS243-A J3 Pin 3 (HOST_INT)
    The GATE output goes HIGH on loud sound (club impact).
    OPS243-A uses rising edge detection on HOST_INT as trigger.

    No software trigger (S!) needed — the radar triggers itself
    via hardware. We just need to wait for data to appear on serial.

    """

    # Names the trigger in its log lines.
    LABEL = "Sound trigger"
    CLOCK_SYNC_SAMPLES = 36
    CLOCK_SYNC_MAX_ROLLOVER_UNCERTAINTY_MS = 40.0
    CLOCK_SYNC_MAX_TIMEOUT_READ_MS = 50.0
    CLOCK_SYNC_MAX_FALLBACK_AGE_S = 60.0
    # The radar can drop its armed state without any error (e.g. a brown-out or
    # firmware glitch). A re-arm costs ~0.55 s and the buffered history, so only
    # a radar that has sent no dump for this long gets one.
    STALL_REARM_S = 300.0
    # A dump starts within ~0.2 s of its sound edge; one not started by now was missed.
    EDGE_DUMP_GRACE_S = 2.0

    def __init__(
        self,
        pre_trigger_segments: int = 12,
        clock: Callable[[], float] = time.monotonic,
    ):
        """
        Initialize sound trigger.

        Args:
            pre_trigger_segments: Number of pre-trigger segments for S# command.
                Each segment = 128 samples = ~4.27ms at 30ksps.
                Default 12 gives ~51ms pre-trigger, ~85ms post-trigger.
                NOTE: This is passed to enter_rolling_buffer_mode() by the caller.
                The trigger does NOT configure rolling buffer mode itself.
            clock: Monotonic time source for the stall re-arm.
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self._rearm_pending = False
        self._clock = clock
        self._armed_at = clock()
        self._last_edge_at: Optional[float] = None
        self._stall_rearms = 0
        self._rejection_callback: Optional[Callable[[Optional[float], str], None]] = None
        self._dump_started_callback: Optional[Callable[[float], None]] = None
        self._dump_finished_callback: Optional[Callable[[], None]] = None
        self._busy_dump_started: Optional[Callable[[float], None]] = None
        self._busy_dump_finished: Optional[Callable[[float], None]] = None
        self._busy_rearm_started: Optional[Callable[[float], None]] = None
        self._busy_armed: Optional[Callable[[float, Optional[float]], None]] = None

    def set_rejection_callback(
        self, callback: Optional[Callable[[Optional[float], str], None]]
    ) -> None:
        """Call ``callback(first_byte_timestamp, reason)`` for every rejected dump.

        It runs before the radar is re-armed, so sensors frozen by the same
        sound edge (the TI ring) can be released while the OPS re-arms.
        """
        self._rejection_callback = callback

    def set_dump_callbacks(
        self,
        started: Optional[Callable[[float], None]] = None,
        finished: Optional[Callable[[], None]] = None,
    ) -> None:
        """Report the OPS dump lifecycle: first byte received, and re-arm starting."""
        self._dump_started_callback = started
        self._dump_finished_callback = finished

    def set_busy_callbacks(
        self,
        *,
        dump_started: Optional[Callable[[float], None]] = None,
        dump_finished: Optional[Callable[[float], None]] = None,
        rearm_started: Optional[Callable[[float], None]] = None,
        armed: Optional[Callable[[float, Optional[float]], None]] = None,
    ) -> None:
        """Report when the OPS is blind (the trigger gate's busy tracking).

        ``dump_started(first_byte_timestamp)`` on a dump's first byte,
        ``dump_finished(timestamp)`` once the whole dump was read (parsing and
        a shot's clock sync follow), ``rearm_started(timestamp)`` before each
        re-arm and
        ``armed(armed_timestamp, drain_end_timestamp)`` after a re-arm that
        went through (the final PA written; host epoch seconds).
        """
        self._busy_dump_started = dump_started
        self._busy_dump_finished = dump_finished
        self._busy_rearm_started = rearm_started
        self._busy_armed = armed

    @staticmethod
    def _radar_timestamp(radar: "OPS243Radar", name: str) -> Optional[float]:
        """A float timestamp attribute of the radar; None when absent (radar doubles)."""
        value = getattr(radar, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def _notify_busy(self, callback: Optional[Callable], *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[TRIGGER] OPS busy callback failed", exc_info=True)

    def _notify_rejected(self, radar: "OPS243Radar", reason: str) -> None:
        """Tell the rejection listener about a dump that produced no shot."""
        if self._rejection_callback is None:
            return
        first_byte_timestamp = getattr(radar, "last_hardware_trigger_first_byte_timestamp", None)
        try:
            self._rejection_callback(first_byte_timestamp, reason)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[TRIGGER] Trigger rejection callback failed", exc_info=True)

    def _notify_dump_finished(self) -> None:
        if self._dump_finished_callback is None:
            return
        try:
            self._dump_finished_callback()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[TRIGGER] Dump-finished callback failed", exc_info=True)

    def _first_byte_hook(
        self,
        radar: "OPS243Radar",
        capture_started_callback: Optional[Callable[[], None]],
    ) -> Optional[Callable[[], None]]:
        """Wrap the UI first-byte callback so the dump-start listener runs first."""
        started = self._dump_started_callback
        busy_started = self._busy_dump_started
        if started is None and busy_started is None:
            return capture_started_callback

        def on_first_byte() -> None:
            timestamp = getattr(radar, "last_hardware_trigger_first_byte_timestamp", None)
            first_byte = time.time() if timestamp is None else float(timestamp)
            self._notify_busy(busy_started, first_byte)
            if started is not None:
                try:
                    started(first_byte)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("[TRIGGER] Dump-started callback failed", exc_info=True)
            if capture_started_callback is not None:
                capture_started_callback()

        return on_first_byte

    def _rearm(self, radar: "OPS243Radar") -> None:
        """Re-arm the radar, remembering a failure so the next wait retries it."""
        self._notify_dump_finished()
        self._notify_busy(self._busy_rearm_started, time.time())
        self._rearm_pending = True
        self._rearm_pending = radar.rearm_rolling_buffer(self.pre_trigger_segments) is False
        if not self._rearm_pending:
            self._armed_at = self._clock()
            armed_timestamp = self._radar_timestamp(radar, "last_rearm_armed_timestamp")
            self._notify_busy(
                self._busy_armed,
                time.time() if armed_timestamp is None else armed_timestamp,
                self._radar_timestamp(radar, "last_rearm_drain_end_timestamp"),
            )

    def notify_trigger_edge(self) -> None:
        """Record a sound-gate edge seen by another sensor (any thread)."""
        self._last_edge_at = self._clock()

    def mark_rearmed(self) -> None:
        """Record a completed external re-arm without erasing diagnostics or callbacks."""
        self._rearm_pending = False
        self._armed_at = self._clock()

    def mark_rearm_pending(self) -> None:
        """Record an external re-arm that did not complete; the next wait retries it."""
        self._rearm_pending = True

    def _rearm_if_stalled(self, radar: "OPS243Radar") -> None:
        """Re-arm a radar that went quiet after a missed edge or a long stall."""
        now = self._clock()
        edge_at = self._last_edge_at
        if edge_at is not None and edge_at > self._armed_at:
            if now - edge_at < self.EDGE_DUMP_GRACE_S:
                return
            reason = "trigger_without_dump"
        elif now - self._armed_at >= self.STALL_REARM_S:
            reason = "long_stall"
        else:
            return
        self._stall_rearms += 1
        logger.warning(
            "[TRIGGER] Re-arming radar (%s, stall re-arm #%d): no dump in the %.0fs "
            "since it was last armed",
            reason,
            self._stall_rearms,
            now - self._armed_at,
        )
        self._append_diagnostic(accepted=False, reason=reason)
        self._rearm(radar)

    @staticmethod
    def _clock_sync_last_read_host_time(clock_sync: dict) -> Optional[float]:
        """Return the host time of the last C? read in a clock-sync summary."""
        reads = clock_sync.get("reads") or []
        if not reads or not isinstance(reads[-1], dict):
            return None
        return reads[-1].get("host_after") or reads[-1].get("host_mid")

    @classmethod
    def _clock_sync_age_s(cls, clock_sync: dict) -> Optional[float]:
        """Return age in seconds for a clock-sync summary."""
        last_host_time = cls._clock_sync_last_read_host_time(clock_sync)
        if last_host_time is None:
            return None
        try:
            return time.time() - float(last_host_time)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _clock_sync_quality(cls, clock_sync: object) -> tuple[bool, str]:
        """Return whether a clock sync is trustworthy enough for shot timing."""
        if not isinstance(clock_sync, dict):
            return False, "missing"

        if not clock_sync.get("usable_for_trigger_timestamps"):
            return False, f"unusable_method:{clock_sync.get('clock_sync_method', 'unknown')}"

        if clock_sync.get("best_offset_s") is None:
            return False, "missing_best_offset"

        reads = clock_sync.get("reads") or []
        slow_invalid_reads = [
            read
            for read in reads
            if isinstance(read, dict)
            and read.get("radar_clock_s") is None
            and (read.get("read_latency_ms") or 0.0) >= cls.CLOCK_SYNC_MAX_TIMEOUT_READ_MS
        ]
        if slow_invalid_reads:
            return False, f"timeout_reads:{len(slow_invalid_reads)}"

        method = clock_sync.get("clock_sync_method")
        if method == "integer_rollover":
            uncertainty = clock_sync.get("rollover_uncertainty_ms")
            if uncertainty is None:
                return False, "missing_rollover_uncertainty"
            try:
                if float(uncertainty) > cls.CLOCK_SYNC_MAX_ROLLOVER_UNCERTAINTY_MS:
                    return False, f"rollover_uncertainty:{float(uncertainty):.1f}ms"
            except (TypeError, ValueError):
                return False, "invalid_rollover_uncertainty"
            return True, "valid_integer_rollover"

        if method == "fractional_clock":
            return True, "valid_fractional_clock"

        return False, f"unsupported_method:{method or 'unknown'}"

    @classmethod
    def _clock_sync_summary_for_log(cls, clock_sync: object) -> Optional[dict]:
        """Return a compact JSONL-safe summary of a clock-sync candidate."""
        if not isinstance(clock_sync, dict):
            return None
        valid, reason = cls._clock_sync_quality(clock_sync)
        return {
            "valid": valid,
            "reason": reason,
            "source": clock_sync.get("source"),
            "samples": clock_sync.get("samples"),
            "valid_samples": clock_sync.get("valid_samples"),
            "clock_sync_method": clock_sync.get("clock_sync_method"),
            "best_offset_s": clock_sync.get("best_offset_s"),
            "raw_best_offset_s": clock_sync.get("raw_best_offset_s"),
            "best_read_latency_ms": clock_sync.get("best_read_latency_ms"),
            "offset_spread_ms": clock_sync.get("offset_spread_ms"),
            "rollover_uncertainty_ms": clock_sync.get("rollover_uncertainty_ms"),
            "age_s": (
                round(cls._clock_sync_age_s(clock_sync), 3)
                if cls._clock_sync_age_s(clock_sync) is not None
                else None
            ),
        }

    def _select_clock_sync_for_capture(
        self,
        radar: "OPS243Radar",
        capture: IQCapture,
    ) -> Optional[dict]:
        """Choose and apply the best OPS clock sync for this capture."""
        previous_sync = getattr(radar, "last_clock_sync", None)
        previous_valid, previous_reason = self._clock_sync_quality(previous_sync)
        previous_age_s = (
            self._clock_sync_age_s(previous_sync) if isinstance(previous_sync, dict) else None
        )

        fresh_sync = None
        fresh_error = None
        if hasattr(radar, "read_clock_sync"):
            try:
                fresh_sync = radar.read_clock_sync(
                    samples=self.CLOCK_SYNC_SAMPLES,
                    store=False,
                )
                if isinstance(fresh_sync, dict):
                    fresh_sync["source"] = "per_shot"
            except Exception as exc:  # pylint: disable=broad-except
                fresh_error = str(exc)
                logger.warning("[TRIGGER] Per-shot OPS clock sync failed: %s", exc, exc_info=True)

        fresh_valid, fresh_reason = self._clock_sync_quality(fresh_sync)

        selected_sync = None
        selected_source = "first_byte"
        selected_reason = "no_valid_clock_sync"

        if fresh_valid and isinstance(fresh_sync, dict):
            selected_sync = fresh_sync
            selected_source = "fresh"
            selected_reason = fresh_reason
            radar.last_clock_sync = fresh_sync
        elif (
            previous_valid
            and isinstance(previous_sync, dict)
            and previous_age_s is not None
            and previous_age_s <= self.CLOCK_SYNC_MAX_FALLBACK_AGE_S
        ):
            selected_sync = previous_sync
            selected_source = "previous"
            selected_reason = f"fresh_rejected:{fresh_reason};previous_age:{previous_age_s:.1f}s"
        elif previous_valid and previous_age_s is not None:
            selected_reason = (
                f"fresh_rejected:{fresh_reason};previous_too_old:{previous_age_s:.1f}s"
            )
        elif previous_reason != "missing":
            selected_reason = f"fresh_rejected:{fresh_reason};previous_rejected:{previous_reason}"
        elif fresh_error:
            selected_reason = f"fresh_error:{fresh_error}"
        else:
            selected_reason = f"fresh_rejected:{fresh_reason}"

        selected_offset_s = None
        selected_age_s = None
        if isinstance(selected_sync, dict):
            selected_offset_s = selected_sync.get("best_offset_s")
            selected_age_s = self._clock_sync_age_s(selected_sync)
            try:
                capture.apply_trigger_timestamp_from_clock_sync(float(selected_offset_s))
            except (TypeError, ValueError):
                logger.warning(
                    "[TRIGGER] Ignoring invalid selected OPS clock-sync offset: %r",
                    selected_offset_s,
                )
                selected_sync = None
                selected_source = "first_byte"
                selected_reason = "selected_offset_invalid"
                selected_offset_s = None

        previous_offset_s = (
            previous_sync.get("best_offset_s") if isinstance(previous_sync, dict) else None
        )
        fresh_offset_s = fresh_sync.get("best_offset_s") if isinstance(fresh_sync, dict) else None

        selection_log = {
            "selection": selected_source,
            "selection_reason": selected_reason,
            "selected_offset_s": selected_offset_s,
            "selected_age_s": round(selected_age_s, 3) if selected_age_s is not None else None,
            "fresh": self._clock_sync_summary_for_log(fresh_sync),
            "previous": self._clock_sync_summary_for_log(previous_sync),
            "fresh_error": fresh_error,
            "fresh_delta_from_previous_ms": (
                round((fresh_offset_s - previous_offset_s) * 1000.0, 3)
                if fresh_offset_s is not None and previous_offset_s is not None
                else None
            ),
        }

        if selected_sync is not None:
            logger.info(
                "[TRIGGER] OPS clock sync selected: %s offset=%.6fs age=%sms "
                "(fresh=%s, previous=%s, delta=%sms)",
                selected_source,
                selected_offset_s,
                "n/a" if selected_age_s is None else f"{selected_age_s:.1f}",
                fresh_reason,
                previous_reason,
                "n/a"
                if selection_log["fresh_delta_from_previous_ms"] is None
                else f"{selection_log['fresh_delta_from_previous_ms']:.1f}",
            )
        else:
            logger.info(
                "[TRIGGER] OPS clock sync fallback to first-byte timing: %s",
                selected_reason,
            )

        return selection_log

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
        cancel_event: Optional[threading.Event] = None,
        capture_started_callback: Optional[Callable[[], None]] = None,
    ) -> Optional[IQCapture]:
        """
        Wait for hardware sound trigger and capture buffer.

        PREREQUISITE: Rolling buffer mode must already be configured via
        radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode().

        Unlike other triggers, no S! command is sent. The radar's
        HOST_INT pin receives the trigger from the SEN-14262 GATE
        output, causing the radar to dump its rolling buffer automatically.
        We just block on serial read waiting for the I/Q data to arrive.
        """
        self._rearm_before_wait(radar)

        logger.info("[TRIGGER] Waiting for %s (timeout=%.0fs)...", self.LABEL.lower(), timeout)

        response = radar.wait_for_hardware_trigger(
            timeout=timeout,
            on_first_byte=self._first_byte_hook(radar, capture_started_callback),
            **self._hardware_wait_kwargs(cancel_event),
        )

        if not response:
            lost_bytes = getattr(radar, "last_hardware_trigger_lost_capture_bytes", 0)
            if lost_bytes:
                logger.warning("[TRIGGER] %s capture lost — re-arming radar", self.LABEL)
                self._notify_rejected(radar, "lost_capture")
                self._append_diagnostic(
                    accepted=False, reason="lost_capture", response_bytes=lost_bytes
                )
                self._rearm(radar)
                return None
            logger.info("[TRIGGER] %s timeout — no hardware trigger received", self.LABEL)
            cancelled = cancel_event is not None and cancel_event.is_set()
            if not cancelled and not self._rearm_pending:
                self._rearm_if_stalled(radar)
            return None

        # The dump leaves the radar idle; every exit from here must re-arm it.
        self._rearm_pending = True
        self._notify_busy(self._busy_dump_finished, time.time())
        try:
            return self._capture_from_dump(radar, processor, response)
        except Exception:
            if self._rearm_pending:
                try:
                    self._rearm(radar)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("[TRIGGER] Re-arm after capture error failed", exc_info=True)
            raise

    def _rearm_before_wait(self, radar: "OPS243Radar") -> None:
        """Retry a re-arm that did not complete before waiting again."""
        if self._rearm_pending:
            logger.warning("[TRIGGER] Retrying the radar re-arm that did not complete")
            self._rearm(radar)

    def _hardware_wait_kwargs(self, cancel_event: Optional[threading.Event]) -> dict:
        """Extra ``wait_for_hardware_trigger`` arguments: the cancel event."""
        return {"cancel_event": cancel_event}

    def _note_dump(self, radar: "OPS243Radar", first_byte_timestamp: Optional[float]) -> None:
        """A dump was read (before parsing): nothing to note for the sound trigger."""

    def _wants_clock_sync(self) -> bool:
        """Whether an accepted dump gets the per-shot clock sync before its re-arm."""
        return True

    def _capture_from_dump(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        response: str,
    ) -> Optional[IQCapture]:
        """Parse and validate a completed hardware dump, re-arming the radar."""
        response_len = len(response)
        logger.info("[TRIGGER] %s fired, %d bytes received", self.LABEL, response_len)
        first_byte_timestamp = getattr(
            radar,
            "last_hardware_trigger_first_byte_timestamp",
            None,
        )
        self._note_dump(radar, first_byte_timestamp)

        # ORDERING MATTERS: after its dump the radar sits idle, where
        # HOST_INT pulses are harmless. A real shot produces a second loud
        # sound ~1s later (ball hitting the net); if we re-arm first, that
        # sound starts a new dump exactly when the clock-sync exchange
        # writes to the port — a two-way serial deadlock observed in the
        # field (capture thread wedged in serial.write, radar frozen
        # mid-dump). So: do all wire-talk (clock sync) while idle, and
        # re-arm LAST, when we are about to go back to reading.

        capture = processor.parse_capture(
            response,
            first_byte_timestamp=first_byte_timestamp,
        )

        if not capture:
            self._notify_rejected(radar, "parse_failed")
            self._rearm(radar)
            logger.warning(
                "[TRIGGER] %s parse failed (%d bytes received)", self.LABEL, response_len
            )
            self._append_diagnostic(
                accepted=False,
                reason="parse_failed",
                response_bytes=response_len,
                capture_first_byte_timestamp=first_byte_timestamp,
            )
            return None

        if first_byte_timestamp is not None and capture.first_byte_timestamp is None:
            capture.first_byte_timestamp = float(first_byte_timestamp)

        if not processor.has_consistent_capture_timing(capture):
            logger.error(
                "[TRIGGER] Capture rejected: trigger offset %.1fms is incompatible "
                "with %d samples at %dHz. Reasserting sample rate before re-arming.",
                capture.trigger_offset_ms,
                capture.num_samples,
                processor.SAMPLE_RATE,
            )
            self._notify_rejected(radar, "capture_timebase_mismatch")
            self._append_diagnostic(
                accepted=False,
                reason="capture_timebase_mismatch",
                response_bytes=response_len,
                capture_first_byte_timestamp=first_byte_timestamp,
            )
            # The completed dump leaves OPS idle. Restore the intended rate
            # here, while no HOST_INT-triggered dump can race the command.
            # A following capture must independently pass this check; sending
            # the setting does not by itself prove that the radar applied it.
            radar.set_sample_rate(processor.SAMPLE_RATE)
            self._rearm(radar)
            return None

        if not processor.has_expected_trigger_split(capture, self.pre_trigger_segments):
            logger.error(
                "[TRIGGER] Capture rejected: trigger offset %.1fms does not match the "
                "S#%d pre-trigger split at %dHz. Re-sending rate and split while re-arming.",
                capture.trigger_offset_ms,
                self.pre_trigger_segments,
                processor.SAMPLE_RATE,
            )
            self._notify_rejected(radar, "trigger_split_mismatch")
            self._append_diagnostic(
                accepted=False,
                reason="trigger_split_mismatch",
                response_bytes=response_len,
                capture_first_byte_timestamp=first_byte_timestamp,
            )
            # A radar at another rate than configured mostly lands here, not in
            # the timebase check: a 30 ksps dump fits inside a 20 ksps buffer
            # span, but its S#n offset is 2/3 of the expected one. The re-arm
            # re-sends only S#n, so restore the rate too (idle radar, no race).
            radar.set_sample_rate(processor.SAMPLE_RATE)
            self._rearm(radar)
            return None

        # Quick validation: does the capture contain any real swing data?
        # At a driving range, a nearby player's impact sound can trip the
        # trigger even though nothing was moving in front of our radar.
        # Discard these false triggers immediately so we re-arm fast.
        summary = self._summarize_capture_activity(processor, capture)

        if not summary["valid_outbound_count"]:
            # False trigger: re-arm immediately (no clock sync) so the
            # next real swing isn't missed.
            self._notify_rejected(radar, "no_outbound_speed")
            self._rearm(radar)
            logger.info(
                "[TRIGGER] %s rejected — no outbound speed >= %.0f mph "
                "(peak=%.1f mph, %d readings)",
                self.LABEL,
                self.MIN_VALID_OUTBOUND_MPH,
                summary["peak_outbound_mph"],
                summary["total_readings"],
            )
            self._append_activity_diagnostic(
                summary,
                accepted=False,
                reason="no_outbound_speed",
                response_bytes=response_len,
                capture_first_byte_timestamp=first_byte_timestamp,
            )
            return None

        # Accepted: talk on the wire while the radar is still idle, then
        # re-arm as the last serial action before returning to the reader.
        if self._wants_clock_sync():
            self._select_clock_sync_for_capture(radar, capture)
        self._rearm(radar)

        if capture.first_byte_timestamp is not None and capture.trigger_timestamp is None:
            capture.apply_trigger_timestamp_from_first_byte()

        if capture.trigger_timestamp is not None and capture.first_byte_timestamp is not None:
            logger.info(
                "[TRIGGER] %s wall time %.3f (source=%s, first byte %.3f, post-trigger %.1fms)",
                self.LABEL,
                capture.trigger_timestamp,
                capture.trigger_timestamp_source or "unknown",
                capture.first_byte_timestamp,
                capture.post_trigger_duration_ms,
            )

        logger.info(
            "[TRIGGER] %s accepted — peak %.1f mph, %d outbound readings",
            self.LABEL,
            summary["valid_peak_outbound_mph"],
            summary["valid_outbound_count"],
        )
        self._append_activity_diagnostic(
            summary,
            accepted=True,
            reason="accepted",
            response_bytes=response_len,
        )

        return capture

    def reset(self):
        """Reset trigger state."""
        pass  # No state to reset


class _EitherEvent:  # pylint: disable=too-few-public-methods
    """``is_set`` of the monitor's stop event or the trigger's own wake-up."""

    def __init__(self, stop: Optional[threading.Event], wake: threading.Event):
        self._stop = stop
        self._wake = wake

    def is_set(self) -> bool:
        """True once either event is set."""
        return (self._stop is not None and self._stop.is_set()) or self._wake.is_set()


class OpsSpeedTrigger(SoundTrigger):
    """
    OPS243-A firmware 1.3.3 speed trigger (``--shot-trigger ops-speed*``).

    The OPS trips its own rolling buffer when an outbound reading passes
    ``ST`` and raises J3 pin 1; no HOST_INT pulse starts the dump. The wait,
    parse, validation, clock sync and re-arm-last flow is the sound trigger's,
    plus:

    - the board is left idle by ``prepare_internal_speed_trigger``; the first
      wait arms it (``mark_initial_arm_pending``, no "retrying" warning);
    - the wait looks for ``sample_time`` dump markers only
      (``SPEED_TRIGGER_MARKERS``: the trip message may be a ``trigger_time``
      line);
    - ``set_dump_classifier(fn)``: ``fn(first_byte_timestamp)`` names what
      started each dump (``TriggerGate.classify_ops_speed_dump``). In the
      shadow (``ops-speed-shadow``) only ``matched`` and ``fallback`` dumps
      count (``last_dump_counts``); the others skip the per-shot clock sync
      and the monitor rejects them as ``ops_speed_unmatched``;
    - pin-silent detector: ``PIN_SILENT_DUMPS`` dumps in a row without a pin
      edge (``no_record``, or a shadow ``self_trip``: a trip that only matched
      a later fallback pulse) log an error once (pin 1 not seen: wiring or
      polarity);
    - ``request_rearm`` (any thread) re-arms an OPS that is stuck idle, from
      the capture thread at its next wait (the witness's stall rule).

    ``last_dump_fields`` holds the per-dump log fields: the classification and
    the bytes read before the dump marker (the 1.3.3 trip message).
    """

    LABEL = "Speed trigger"
    PIN_SILENT_DUMPS = 2
    # Dumps that show the OPS tripped itself without a pin-1 edge reaching the gate.
    PIN_SILENT_DUMP_KINDS = ("no_record", "self_trip")
    STALL_REARM_REASON = "ops_speed_stall_rearm"
    COUNTED_SHADOW_DUMPS = ("matched", "fallback")

    def __init__(
        self,
        pre_trigger_segments: int = 2,
        clock: Callable[[], float] = time.monotonic,
        *,
        config: Optional["OpsSpeedConfig"] = None,
    ):
        """
        Initialize the speed trigger.

        Args:
            pre_trigger_segments: ``S#n`` around the speed trip
                (``--ops-speed-pre-trigger``, 2 = 12.8 / 192.0 ms at 20 ksps).
            clock: Monotonic time source for the stall re-arm.
            config: The ``--ops-speed-*`` settings (threshold, magnitude, mode).
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments, clock=clock)
        self.config = config
        self.shadow = bool(config is not None and config.shadow)
        self._initial_arm_pending = False
        self._classifier: Optional[Callable[[Optional[float]], Optional[str]]] = None
        self._pin_silent_callback: Optional[Callable[[int], None]] = None
        self._dumps_without_record = 0
        self._pin_silent_reported = False
        self._stall_reason: Optional[str] = None
        self._wake = threading.Event()
        self.stall_rearms_requested = 0
        self.last_dump_fields: dict = {}
        self.last_dump_counts = True

    def mark_initial_arm_pending(self) -> None:
        """The board was set up idle; the first wait arms it (an expected re-arm)."""
        self._initial_arm_pending = True
        self._rearm_pending = True

    def set_dump_classifier(self, classifier: Optional[Callable[[Optional[float]], str]]) -> None:
        """Name what started each dump from its first byte (the trigger gate)."""
        self._classifier = classifier

    def set_pin_silent_callback(self, callback: Optional[Callable[[int], None]]) -> None:
        """Call ``callback(dumps)`` once when pin 1 looks silent."""
        self._pin_silent_callback = callback

    def request_rearm(self, reason: str = STALL_REARM_REASON) -> None:
        """Re-arm the radar at the capture thread's next wait (any thread).

        Wakes an idle wait; a dump already arriving is read first, and its own
        re-arm then satisfies the request.
        """
        self._stall_reason = reason
        self.stall_rearms_requested += 1
        self._wake.set()

    def _rearm(self, radar: "OPS243Radar") -> None:
        # Any re-arm also answers a pending stall request.
        self._stall_reason = None
        self._wake.clear()
        super()._rearm(radar)

    def _serve_stall_request(self, radar: "OPS243Radar") -> bool:
        reason = self._stall_reason
        if reason is None:
            return False
        self._stall_rearms += 1
        logger.warning(
            "[TRIGGER] Re-arming radar (%s, stall re-arm #%d): microphone edges without "
            "a speed trip while the OPS was armed",
            reason,
            self._stall_rearms,
        )
        self._append_diagnostic(accepted=False, reason=reason)
        self._rearm(radar)
        return True

    def _rearm_before_wait(self, radar: "OPS243Radar") -> None:
        if self._rearm_pending and self._initial_arm_pending:
            self._initial_arm_pending = False
            logger.info("[TRIGGER] Arming the radar for the speed trigger")
            self._rearm(radar)
            return
        if not self._rearm_pending and self._serve_stall_request(radar):
            return
        super()._rearm_before_wait(radar)

    def _rearm_if_stalled(self, radar: "OPS243Radar") -> None:
        if self._serve_stall_request(radar):
            return
        super()._rearm_if_stalled(radar)

    def _hardware_wait_kwargs(self, cancel_event: Optional[threading.Event]) -> dict:
        return {
            "cancel_event": _EitherEvent(cancel_event, self._wake),
            "capture_markers": SPEED_TRIGGER_MARKERS,
        }

    def _classify(self, first_byte_timestamp: Optional[float]) -> Optional[str]:
        if self._classifier is None:
            return None
        try:
            return self._classifier(first_byte_timestamp)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[TRIGGER] Speed-trigger dump classifier failed", exc_info=True)
            return None

    def _note_dump(self, radar: "OPS243Radar", first_byte_timestamp: Optional[float]) -> None:
        """Classify the dump, keep its preamble and watch for a silent pin 1."""
        kind = self._classify(first_byte_timestamp)
        self.last_dump_counts = not self.shadow or kind in self.COUNTED_SHADOW_DUMPS
        preamble = getattr(radar, "last_hardware_trigger_preamble", b"")
        if not isinstance(preamble, (bytes, bytearray)):
            preamble = b""
        self.last_dump_fields = {
            "ops_speed_dump": kind,
            "ops_speed_preamble": (
                bytes(preamble).decode("ascii", errors="backslashreplace") if preamble else None
            ),
            "ops_speed_preamble_bytes": len(preamble),
            "ops_speed_preamble_timestamp": self._radar_timestamp(
                radar, "last_hardware_trigger_preamble_timestamp"
            ),
        }
        if preamble:
            logger.info(
                "[TRIGGER] %d bytes before the dump marker (trip message?): %r",
                len(preamble),
                bytes(preamble[:80]),
            )
        if kind == "self_trip":
            logger.info(
                "[TRIGGER] Speed-trigger dump self_trip: the OPS tripped before the fallback "
                "pulse it matched, pin 1 was not seen; not a shot (shadow)"
            )
        elif kind is not None and not self.last_dump_counts:
            logger.info(
                "[TRIGGER] Speed-trigger dump %s: no microphone edge claimed it, "
                "not a shot (shadow)",
                kind,
            )
        self._watch_pin(kind)

    def _watch_pin(self, kind: Optional[str]) -> None:
        if kind in self.PIN_SILENT_DUMP_KINDS:
            self._dumps_without_record += 1
            if self._dumps_without_record < self.PIN_SILENT_DUMPS or self._pin_silent_reported:
                return
            self._pin_silent_reported = True
            pin = getattr(self.config, "pin", None)
            logger.error(
                "[TRIGGER] Pin 1 silent: %d OPS dumps in a row without a pin-1 edge; "
                "check wiring/polarity (J3 pin 1 -> BCM%s)",
                self._dumps_without_record,
                "?" if pin is None else pin,
            )
            if self._pin_silent_callback is not None:
                try:
                    self._pin_silent_callback(self._dumps_without_record)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("[TRIGGER] Pin-silent callback failed", exc_info=True)
        elif kind in ("pin", "matched", "unmatched"):
            # A pin-1 edge reached the gate: the pin works.
            self._dumps_without_record = 0
            self._pin_silent_reported = False

    def _wants_clock_sync(self) -> bool:
        return self.last_dump_counts


def create_trigger(trigger_type: str = "sound", **kwargs) -> TriggerStrategy:
    """
    Factory function to create trigger strategy.

    Args:
        trigger_type: "sound" (production) or "speed" (fallback)
        **kwargs: Arguments passed to trigger constructor

    Returns:
        Configured TriggerStrategy instance

    Trigger types:
        - "sound": Hardware sound trigger via SparkFun SEN-14262 GATE → HOST_INT.
                   Requires GATE voltage to reach 3.3V threshold.
        - "speed": Fast speed detection triggers rolling buffer capture.
                   Recommended fallback by OmniPreSense. ~5-6ms response time.
        - "ops-speed": OPS243-A firmware 1.3.3 speed trigger: the radar trips
                   its own rolling buffer and raises J3 pin 1
                   (``--shot-trigger ops-speed``/``ops-speed-shadow``).
    """
    triggers = {
        "speed": SpeedTriggeredCapture,
        "sound": SoundTrigger,
        "ops-speed": OpsSpeedTrigger,
    }

    if trigger_type not in triggers:
        raise ValueError(
            f"Unknown trigger type: {trigger_type}. Available: {list(triggers.keys())}"
        )

    return triggers[trigger_type](**kwargs)
