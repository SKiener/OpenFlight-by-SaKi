"""GPIO- or self-triggered IWR6843 L3 capture and OPS-shot correlation.

``shot_trigger`` (see :mod:`openflight.iwr6843.self_trigger`):

* ``sound``: the microphone edge on ``gpio_pin`` starts every ``l3dump``.
* ``ti``: no microphone pin. The capture worker listens on the TI CLI for the
  firmware's ``Triggered`` notice and treats it as the shot edge: the edge
  observers (trigger gate, camera) run at once, then ``triggerLog track`` names
  the firing rule and ``l3dump`` streams the ring the board froze itself (no
  second freeze). Notices the host does not accept are released (``l3release``).
* ``ti-shadow``: the microphone works as in ``sound``; the board self-triggers
  alongside, the notices are paired with the microphone edges
  (``TiShadowMatcher``), and a latched ring no edge claims is released.

``l3dump_delay_s`` (``--shot-trigger ops-speed``, default 0) sends each
``l3dump`` that long after its edge instead of at once: the edge is then the
OPS speed trip on J3 pin 1, which comes before the impact the microphone hears.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

import serial

from openflight.edge_timing import EdgeTimingLog, edge_ticks
from openflight.gpio_factory import ensure_lgpio_pin_factory
from openflight.iwr6843.driver import DumpCancelled, IWR6843Radar
from openflight.iwr6843.dump import HEADER, MAGIC, parse_header, payload_nbytes
from openflight.iwr6843.health import (
    CAUSE_LABELS,
    ECHO_ONLY,
    HWA_STALL,
    HWA_STALL_SESSION_REASON,
    OK,
    SILENT,
    SILENT_CLI,
    TiWedgedError,
    assess_capture,
    classify_reply,
    is_class_a_dump_error,
    parse_stats,
    wedge_label,
)
from openflight.iwr6843.self_trigger import (
    SELF_TRIGGER_MODES,
    SHADOW_CONSUME_AFTER_NOTICE_S,
    SHOT_TRIGGERS,
    SelfTriggerConfig,
    TiShadowMatcher,
    TriggerNotice,
)
from openflight.iwr6843.usb_power import (
    DEFAULT_REENUMERATE_TIMEOUT_S,
    UsbPowerCycleError,
    UsbPowerCycler,
    wait_for_path,
)

logger = logging.getLogger(__name__)

_GRACEFUL_DUMP_SHUTDOWN_S = 12.0
_OUT_OF_WINDOW_DISCARD_S = 1.0
_OUT_OF_WINDOW_ERROR = "ti_edge_out_of_window"
_HWA_STALL_CONSECUTIVE_FAILURES = 2
_PORT_WAIT_SLICE_S = 0.5
# ti-shadow: the worker waits this long on the edge queue between non-blocking
# notice polls, so a microphone edge still starts its l3dump at once.
_SHADOW_NOTICE_POLL_S = 0.001
_HOLDOFF_POLL_S = 0.0005
# Self-trigger modes: an idle board is asked for ``stats`` this often, so a lost
# notice (latched=1), a dropped triggerCfg (enabled=0) or a dead CLI is noticed.
_IDLE_PROBE_S = 10.0
_LISTENER_ERROR_BACKOFF_S = 0.5
# Consecutive serial errors of the idle listener that count as a dead TI port.
_LISTENER_FAILURES_FOR_RECOVERY = 3
TI_RECOVERY_FAILED_SESSION_REASON = (
    "TI radar stopped delivering data — shut the Pi down (not restart) after this session"
)
HWA_STALL_RECOVERING_REASON = "TI radar stalled (HWA) — restarting its USB power"
TI_RESTARTING_REASON = "TI radar restarting (USB power cycle)"


class _RecoveryAborted(Exception):
    """The monitor is stopping; a USB power-cycle recovery must not go on."""


def tx_order_from_config(config_path: str | Path) -> str:
    """Infer the vertical physical TX order from chirp masks in a cfg."""
    masks = []
    with Path(config_path).open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith("chirpCfg"):
                masks.append(line.rsplit(maxsplit=1)[-1])
    if masks == ["1", "4"]:
        return "normal"
    if masks == ["4", "1"]:
        return "reversed"
    if masks == ["1", "2", "4"]:
        return "normal"
    raise ValueError(f"IWR6843 config must contain chirp TX masks 1/4, 4/1, or 1/2/4, got {masks}")


@dataclass(frozen=True)
class IWR6843Capture:
    """One GPIO edge and its completed L3 dump."""

    sequence: int
    trigger_timestamp: float
    completed_timestamp: float
    dump_duration_s: float | None
    raw: bytes | None
    path: Path | None
    error: str | None = None
    temperature_report: dict[str, int] | None = None
    nearest_trigger_delta_ms: float | None = None
    ti_busy_at_impact: bool | None = None
    ti_ready_after_impact_ms: float | None = None
    # Dump version 10 names the HWA range window ("none" or "hann"); None before.
    range_window: str | None = None
    # l3dump_delay_s > 0: the planned and the actual edge-to-l3dump time (ms).
    l3dump_target_ms: float | None = None
    l3dump_actual_ms: float | None = None

    @property
    def valid(self) -> bool:
        """Whether a complete dump was captured."""
        return self.raw is not None and self.error is None


class IWR6843CaptureMonitor:
    """Capture TI rolling-buffer dumps on the same sound edge used by OPS.

    The GPIO callback only timestamps and queues the edge. Serial transfer is
    handled on a dedicated thread because one 768 KiB dump takes several
    seconds at the firmware UART rate.

    Trigger observers (the camera ring, the OPS stall watch) receive every
    edge that passes the ringing filter, whether or not the TI is busy. With
    OPS dump gating enabled, an edge that arrives while the OPS is streaming a
    dump starts no TI dump, and a TI dump whose OPS first byte does not follow
    within the timeout is cancelled: the OPS cannot report a shot for it.

    With a ``usb_power_cycler``, a wedged TI (HWA stall or a wedge-classified
    dump error) is recovered on the capture thread by cutting USB power and
    reconfiguring the radar, at most ``max_recoveries`` times per session.
    Edges keep reaching the observers during a recovery, but start no TI dump.
    """

    RING_FILTER_S = 0.1
    # Class default so a monitor built with __new__ (edge-timing tests) has no shadow.
    _shadow: TiShadowMatcher | None = None
    OPS_DUMP_MAX_S = 6.0
    LATE_FIRST_BYTE_S = 2.0
    REENUMERATE_TIMEOUT_S = DEFAULT_REENUMERATE_TIMEOUT_S
    REOPEN_RETRY_S = 3.0
    REOPEN_STEP_S = 0.25

    def __init__(
        self,
        *,
        config_path: str | Path,
        output_dir: str | Path,
        port: str | None = None,
        gpio_pin: int = 17,
        radar: IWR6843Radar | None = None,
        button_factory: Callable | None = None,
        match_tolerance_s: float = 0.05,
        save_dumps: bool = False,
        trigger_observers: list[Callable[[float], None]] | None = None,
        cancel_false_triggers: bool = False,
        usb_power_cycler: UsbPowerCycler | None = None,
        max_recoveries: int = 2,
        recovery_guard: Callable[[], str | None] | None = None,
        edge_timing: EdgeTimingLog | None = None,
        shot_trigger: str = "sound",
        self_trigger: SelfTriggerConfig | None = None,
        shadow_matcher: TiShadowMatcher | None = None,
        shadow_holdoff_s: float = 0.0,
        fire_observers: list[Callable[[dict], None]] | None = None,
        l3dump_delay_s: float = 0.0,
    ):
        if shot_trigger not in SHOT_TRIGGERS:
            raise ValueError(f"shot_trigger must be one of {SHOT_TRIGGERS}, got {shot_trigger!r}")
        if shot_trigger in SELF_TRIGGER_MODES and self_trigger is None:
            raise ValueError(f"shot_trigger {shot_trigger!r} needs a self_trigger config")
        if shadow_holdoff_s < 0.0:
            raise ValueError(f"shadow hold-off must be >= 0, got {shadow_holdoff_s}")
        if not 0.0 <= l3dump_delay_s <= 1.0:
            raise ValueError(f"l3dump delay must be 0-1 s, got {l3dump_delay_s}")
        self._l3dump_delay_s = float(l3dump_delay_s)
        self.shot_trigger = shot_trigger
        self.self_trigger = self_trigger if shot_trigger in SELF_TRIGGER_MODES else None
        self._shadow = shadow_matcher if shot_trigger == "ti-shadow" else None
        self._shadow_holdoff_s = shadow_holdoff_s if shot_trigger == "ti-shadow" else 0.0
        self._fire_observers = list(fire_observers or [])
        # The fire the board latched and no l3dump/l3release has taken yet.
        self._latched_notice: TriggerNotice | None = None
        # ti: the notice behind each queued edge (keyed by the edge timestamp).
        self._fire_notices: dict[float, TriggerNotice] = {}
        # ti-shadow and l3dump_delay_s: monotonic stamp of each queued edge.
        self._edge_monotonic: dict[float, int] = {}
        self._last_serial_s = time.monotonic()
        self._last_probe_stats: dict[str, int] | None = None
        self._release_retry_at = 0.0
        self._listener_failures = 0
        self.config_path = Path(config_path)
        self.output_dir = Path(output_dir).expanduser()
        self.gpio_pin = gpio_pin
        self.match_tolerance_s = match_tolerance_s
        self.save_dumps = save_dumps
        self.radar = radar or IWR6843Radar(port=port)
        self._button_factory = button_factory
        self._button = None
        self._running = False
        self._armed = False
        self._capture_active = False
        self._sequence = 0
        self._last_edge_timestamp = 0.0
        self._events: queue.Queue[float | None] = queue.Queue(maxsize=1)
        self._captures: deque[IWR6843Capture] = deque()
        self._condition = threading.Condition()
        self._worker: threading.Thread | None = None
        self._trigger_observers = list(trigger_observers or [])
        self._cancel_false_triggers = cancel_false_triggers
        self._cancel_dump = threading.Event()
        self._last_observed_edge_timestamp = 0.0
        self._rejected_edge_timestamp: float | None = None
        self._ops_gating = False
        self._ops_first_byte_timeout_s = 0.5
        self._ops_dump_started_at: float | None = None
        self._ops_last_first_byte: float | None = None
        self._orphan_cancelled_edge: float | None = None
        self._first_byte_timer: threading.Timer | None = None
        self._discard_edges: set[float] = set()
        self._dump_history: deque[tuple[float, float]] = deque(maxlen=32)
        self._class_a_failures = 0
        self._hwa_stall_reported = False
        self._usb_power_cycler = usb_power_cycler
        self._max_recoveries = max_recoveries
        self._recovery_guard = recovery_guard
        self._recovery_guard_warned = False
        self._recoveries = 0
        self._recovering = False
        self._probing = False
        self._recovery_edge_logged = False
        self._ti_failed = False
        self._radar_released = False
        self.edge_timing = edge_timing if edge_timing is not None else EdgeTimingLog()

    @property
    def port(self) -> str:
        """Connected TI serial port."""
        return self.radar.port

    @property
    def self_trigger_active(self) -> bool:
        """Whether the board's own trigger runs (``ti`` or ``ti-shadow``)."""
        return self.self_trigger is not None

    @property
    def uses_gpio(self) -> bool:
        """Whether the monitor owns the microphone input pin."""
        return self.shot_trigger != "ti"

    def add_fire_observer(self, observer: Callable[[dict], None]) -> None:
        """Call ``observer(fields)`` for every self-trigger fire the host accepted (``ti``)."""
        self._fire_observers.append(observer)

    @property
    def dump_in_progress(self) -> bool:
        """Whether a TI dump is queued or streaming over the UART."""
        with self._condition:
            return self._dump_busy_locked()

    def _dump_busy_locked(self) -> bool:
        # A recovery is not a dump: camera writes and shot matching must not
        # wait for a TI that is being power-cycled.
        if self._recovering or self._probing:
            return False
        return self._capture_active or (self._running and not self._events.empty())

    def _ti_state_locked(self) -> str:
        """Whether the board could self-fire right now, for the shadow log."""
        if self._ti_failed:
            return "failed"
        if self._recovering or self._probing:
            return "recovering"
        if self._capture_active or not self._events.empty():
            return "dumping"
        if self._latched_notice is not None:
            return "latched"
        return "listening"

    def enable_ops_dump_gating(self, first_byte_timeout_s: float = 0.5) -> None:
        """Couple TI dumps to the OPS dump lifecycle (sound trigger only)."""
        with self._condition:
            self._ops_gating = True
            self._ops_first_byte_timeout_s = first_byte_timeout_s

    def notify_ops_dump_started(self, first_byte_timestamp: float) -> None:
        """Record the first byte of an OPS dump (OPS capture thread)."""
        with self._condition:
            self._ops_dump_started_at = first_byte_timestamp
            self._ops_last_first_byte = first_byte_timestamp
            orphan_edge = self._orphan_cancelled_edge
            self._orphan_cancelled_edge = None
        if orphan_edge is not None and 0.0 <= first_byte_timestamp - orphan_edge <= (
            self.LATE_FIRST_BYTE_S
        ):
            logger.warning(
                "[IWR6843] OPS first byte arrived %.0f ms after TI edge %.3f, after its TI "
                "dump was cancelled: the shot keeps its OPS data, TI angle lost",
                (first_byte_timestamp - orphan_edge) * 1000.0,
                orphan_edge,
            )

    def notify_ops_dump_finished(self) -> None:
        """Record that the OPS dump is over and its re-arm begins."""
        with self._condition:
            self._ops_dump_started_at = None

    def _ops_dump_in_progress_locked(self, edge_timestamp: float) -> bool:
        started = self._ops_dump_started_at
        return (
            self._ops_gating
            and started is not None
            and 0.0 <= edge_timestamp - started <= self.OPS_DUMP_MAX_S
        )

    def start(self, *, armed: bool = True) -> None:
        """Configure the radar and GPIO, optionally arming trigger capture."""
        if self._running:
            return
        if not self.config_path.is_file():
            raise FileNotFoundError(f"IWR6843 config not found: {self.config_path}")
        if self.save_dumps:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        configured = False
        try:
            self.radar.send_config(str(self.config_path))
            configured = True
            self._apply_self_trigger()

            if self.uses_gpio:
                self._button = self._open_button()
            self._running = True
            self._worker = threading.Thread(
                target=self._capture_loop,
                name="iwr6843-capture",
                daemon=True,
            )
            self._worker.start()
            if armed:
                self.arm()
        except Exception:
            self._running = False
            if self._button is not None:
                self._button.close()
                self._button = None
            if configured:
                self._stop_sensor_and_close()
            else:
                self.radar.close()
            raise
        logger.info(
            "[IWR6843] Configured %s using %s (%s%s%s)",
            f"on BCM{self.gpio_pin}" if self.uses_gpio else "without a microphone pin",
            self.port,
            self.config_path.name,
            ", armed" if self._armed else ", waiting for OPS",
            (
                f", {self.shot_trigger} self-trigger {self.self_trigger.command!r}"
                if self.self_trigger is not None
                else ""
            ),
        )

    def _open_button(self):
        """The microphone input, from the injected factory or gpiozero."""
        button_factory = self._button_factory
        if button_factory is None:
            # Must precede the first gpiozero device: on a Pi 5 gpiozero's
            # own auto-detection fails outright. See gpio_factory.
            ensure_lgpio_pin_factory()

            from gpiozero import Button  # pylint: disable=import-error,import-outside-toplevel

            button_factory = Button
        # No gpiozero debounce: lgpio delays delivery by the debounce interval,
        # which previously cost the first 50 ms of ball flight.
        return button_factory(self.gpio_pin, pull_up=False, bounce_time=None)

    def _apply_self_trigger(self) -> None:
        """Send ``triggerCfg`` after every sensorStart (it clears the trigger)."""
        if self.self_trigger is None:
            return
        self.radar.set_self_trigger(self.self_trigger.command)
        with self._condition:
            self._latched_notice = None
        self._last_probe_stats = None
        logger.info("[IWR6843] Self-trigger armed: %s", self.self_trigger.command)

    def arm(self) -> None:
        """Accept edges after the OPS trigger path is fully initialized."""
        if not self._running:
            raise RuntimeError("cannot arm an IWR6843 monitor that is not running")
        if self._armed:
            return
        # Attach while logically disarmed so a line already high from OPS
        # startup cannot synchronously create a false capture. In ti mode the
        # worker listens for the firmware line instead; notices read while
        # disarmed are released.
        if self._button is not None:
            self._button.when_pressed = self.notify_trigger
        self._armed = True
        logger.info(
            "[IWR6843] Armed on %s",
            "the firmware self-trigger" if not self.uses_gpio else f"BCM{self.gpio_pin}",
        )

    def notify_trigger(
        self, timestamp: float | None = None, *, monotonic_ns: int | None = None
    ) -> bool:
        """Queue an edge without doing serial work in the callback.

        Returns True when the edge starts a TI dump. Observers are notified of
        every edge outside the ringing filter, also while the TI is busy. In
        ``ti`` mode the worker calls this for each accepted firmware notice,
        with the notice's stamps.
        """
        if not self._running or not self._armed:
            return False
        callback_wall = time.time()
        callback_monotonic_ns = time.monotonic_ns()
        ticks = edge_ticks(self._button)
        edge_timestamp = callback_wall if timestamp is None else float(timestamp)
        if monotonic_ns is not None:
            edge_monotonic_ns = int(monotonic_ns)
        else:
            edge_monotonic_ns = callback_monotonic_ns - round(
                (callback_wall - edge_timestamp) * 1e9
            )
        with self._condition:
            if edge_timestamp - self._last_observed_edge_timestamp < self.RING_FILTER_S:
                logger.debug("[IWR6843] Ignoring acoustic ringing edge")
                return False
            self._last_observed_edge_timestamp = edge_timestamp
            ti_state = self._ti_state_locked() if self._shadow is not None else None
            queued = self._queue_dump_locked(edge_timestamp)
            if queued and (self._shadow is not None or self._l3dump_delay_s > 0.0):
                self._edge_monotonic[edge_timestamp] = edge_monotonic_ns
        if self._shadow is not None:
            self._shadow.note_edge(edge_timestamp, edge_monotonic_ns, ti_state=ti_state)
        # Shadow timing only: a bare deque append before the observers, so a
        # session-log writer finds the edge even while an observer blocks.
        self.edge_timing.note(
            edge_timestamp,
            callback_timestamp=callback_wall,
            callback_monotonic_ns=callback_monotonic_ns,
            ticks=ticks,
        )
        for observer in self._trigger_observers:
            try:
                observer(edge_timestamp)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[IWR6843] Trigger observer failed", exc_info=True)
        # The clock resolution waits until the gate has pulsed.
        self.edge_timing.flush()
        return queued

    def _queue_dump_locked(self, edge_timestamp: float) -> bool:
        if self._ti_failed:
            logger.debug("[IWR6843] TI failed; edge %.3f only fanned out", edge_timestamp)
            return False
        if self._probing:
            logger.debug("[IWR6843] TI CLI being probed; edge %.3f only fanned out", edge_timestamp)
            return False
        if self._recovering:
            if not self._recovery_edge_logged:
                self._recovery_edge_logged = True
                logger.info("[IWR6843] TI recovering; edges are only fanned out")
            return False
        if self._dump_busy_locked():
            logger.debug("[IWR6843] TI dump busy; edge %.3f only fanned out", edge_timestamp)
            return False
        if self._ops_dump_in_progress_locked(edge_timestamp):
            logger.info(
                "[IWR6843] Edge %.3f arrived with an OPS dump in progress (first byte %.3f); "
                "no l3dump",
                edge_timestamp,
                self._ops_dump_started_at,
            )
            return False
        self._last_edge_timestamp = edge_timestamp
        self._cancel_dump = threading.Event()
        self._events.put_nowait(edge_timestamp)
        self._condition.notify_all()
        if self._ops_gating and self._cancel_false_triggers:
            self._start_first_byte_watchdog(edge_timestamp, self._cancel_dump)
        return True

    def _start_first_byte_watchdog(
        self, edge_timestamp: float, cancel_event: threading.Event
    ) -> None:
        if self._first_byte_timer is not None:
            self._first_byte_timer.cancel()
        timer = threading.Timer(
            self._ops_first_byte_timeout_s,
            self._cancel_without_ops_first_byte,
            args=(edge_timestamp, cancel_event),
        )
        timer.daemon = True
        self._first_byte_timer = timer
        timer.start()

    def _cancel_without_ops_first_byte(
        self, edge_timestamp: float, cancel_event: threading.Event
    ) -> None:
        """Cancel a TI dump the OPS never answered: it cannot become a shot."""
        with self._condition:
            first_byte = self._ops_last_first_byte
            dump_running = self._dump_busy_locked() and self._last_edge_timestamp == edge_timestamp
            if (
                not self._running
                or not dump_running
                or cancel_event.is_set()
                or (first_byte is not None and first_byte >= edge_timestamp - 0.05)
            ):
                return
            cancel_event.set()
            self._orphan_cancelled_edge = edge_timestamp
        logger.warning(
            "[IWR6843] Cancelling TI dump for edge %.3f: no OPS first byte within %.0f ms",
            edge_timestamp,
            self._ops_first_byte_timeout_s * 1000.0,
        )

    def reject_trigger(self, first_byte_timestamp: float | None) -> bool:
        """Cancel only the active TI edge associated with a rejected OPS dump."""
        if first_byte_timestamp is None or not math.isfinite(first_byte_timestamp):
            return False
        with self._condition:
            if (
                not self._cancel_false_triggers
                or not self._running
                or not self._armed
                or not self._dump_busy_locked()
                or not -0.05 <= first_byte_timestamp - self._last_edge_timestamp <= 0.5
            ):
                return False
            return self._cancel_active_edge_locked()

    def reject_edge(self, edge_timestamp: float) -> bool:
        """Cancel the TI dump started by ``edge_timestamp``; the OPS was never triggered.

        The trigger gate calls this when the camera saw the ball stay on the tee.
        """
        with self._condition:
            if (
                not self._cancel_false_triggers
                or not self._running
                or not self._armed
                or not self._dump_busy_locked()
                or self._last_edge_timestamp != edge_timestamp
            ):
                return False
            return self._cancel_active_edge_locked()

    def _cancel_active_edge_locked(self) -> bool:
        if self._rejected_edge_timestamp == self._last_edge_timestamp:
            return False
        self._rejected_edge_timestamp = self._last_edge_timestamp
        self._cancel_dump.set()
        return True

    def _validate_dump(self, raw: bytes) -> dict:
        if len(raw) < HEADER.size:
            raise ValueError(f"short IWR6843 dump: {len(raw)} bytes")
        metadata = parse_header(raw)
        expected = metadata["header_nbytes"] + payload_nbytes(metadata, raw)
        if len(raw) != expected:
            raise ValueError(f"short IWR6843 dump: {len(raw)} bytes, expected {expected}")
        return metadata

    def _capture_path(self, sequence: int, trigger_timestamp: float) -> Path:
        timestamp = datetime.fromtimestamp(trigger_timestamp).strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return self.output_dir / f"iwr6843_{timestamp}_{sequence:03d}.l3dump"

    def _capture_loop(self) -> None:
        while self._running:
            edge_timestamp = self._next_edge()
            if edge_timestamp is None or not self._running:
                break
            self._capture(edge_timestamp)

    def _next_edge(self) -> float | None:
        """The next queued edge, or None to stop; listens for notices meanwhile.

        ``ti`` blocks on the UART for up to the port timeout (a notice is seen
        about a millisecond after it arrives; nothing else is queued but the
        stop sentinel). ``ti-shadow`` polls the UART between short waits on the
        edge queue, so a microphone edge still starts its dump at once.
        """
        if self.self_trigger is None:
            return self._events.get()
        block = self.shot_trigger == "ti"
        while self._running:
            try:
                if block:
                    return self._events.get_nowait()
                return self._events.get(timeout=_SHADOW_NOTICE_POLL_S)
            except queue.Empty:
                pass
            self._service_self_trigger(block=block)
        return None

    def _service_self_trigger(self, *, block: bool) -> None:
        """One listener pass: read notices, release an unclaimed ring, probe an idle board."""
        if self._ti_failed:
            # Recovery gave up: the port is gone for this session; edges still fan out.
            time.sleep(_LISTENER_ERROR_BACKOFF_S)
            return
        try:
            self._dispatch_notices(self.radar.poll_trigger_notice(block=block))
            self._listener_failures = 0
            self._release_unclaimed_latch()
            self._probe_idle_board()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Keep the worker alive: it still owns captures and recovery.
            self._listener_failures += 1
            logger.warning("[IWR6843] Self-trigger listener error", exc_info=True)
            if (
                isinstance(exc, (OSError, serial.SerialException))
                and self._listener_failures >= _LISTENER_FAILURES_FOR_RECOVERY
                and self._recovery_available()
            ):
                self._listener_failures = 0
                self._recover_by_power_cycle(CAUSE_LABELS[SILENT_CLI])
            else:
                time.sleep(_LISTENER_ERROR_BACKOFF_S)
        if self._shadow is not None:
            self._shadow.flush()

    def _dispatch_notices(self, notices: tuple[TriggerNotice, ...] = ()) -> None:
        """Handle ``notices`` and every notice a command read meanwhile, oldest first."""
        pending = list(notices)
        while True:
            pending.extend(self.radar.take_notices())
            if not pending:
                return
            batch, pending = pending, []
            for notice in batch:
                self._on_notice(notice)

    def _on_notice(self, notice: TriggerNotice) -> None:
        self._last_serial_s = time.monotonic()
        if self._shadow is not None:
            self._shadow.note_notice(notice)
        if notice.consumed:
            # It fired as l3dump/l3release reached it: that command took the ring.
            logger.info("[IWR6843] Self-trigger fired as a CLI command took the ring")
            if self._shadow is not None:
                self._shadow.note_disposition(notice, "consumed")
            return
        if self.shot_trigger == "ti":
            self._accept_fire(notice)
        else:
            self._latch_shadow_notice(notice)

    def _accept_fire(self, notice: TriggerNotice) -> None:
        """``ti``: the notice is the shot edge; fan it out and queue its dump."""
        with self._condition:
            self._fire_notices[notice.wall_time] = notice
            self._latched_notice = notice
        if self.notify_trigger(notice.wall_time, monotonic_ns=notice.monotonic_ns):
            logger.info(
                "[IWR6843] Self-trigger fired%s; observers notified",
                " (notice read inside a CLI reply)" if notice.in_reply else "",
            )
            return
        with self._condition:
            self._fire_notices.pop(notice.wall_time, None)
            # A dump already queued or running takes the latch; a failed TI
            # gets no command.
            leave_it = self._ti_failed or self._dump_busy_locked()
        if leave_it:
            logger.info("[IWR6843] Self-trigger notice while a dump is pending; not released")
            return
        self._release_latch("not accepted: disarmed, ringing filter or OPS dump in progress")

    def _latch_shadow_notice(self, notice: TriggerNotice) -> None:
        """``ti-shadow``: keep the latched ring for a microphone edge, log the rule."""
        with self._condition:
            previous = self._latched_notice
            self._latched_notice = notice
        if previous is not None:
            logger.warning("[IWR6843] Self-trigger notice while an earlier one was still latched")
        cause = self._query_cause()
        if self._shadow is not None:
            self._shadow.note_cause(notice, cause)
        logger.info("[IWR6843] Shadow self-trigger fired (%s)", cause or "rule unknown")

    def _query_cause(self) -> str | None:
        """The rule behind the latched fire, best effort (``triggerLog track``)."""
        try:
            return self.radar.trigger_cause()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] triggerLog track failed", exc_info=True)
            return None
        finally:
            self._last_serial_s = time.monotonic()

    def _release_unclaimed_latch(self) -> None:
        """``ti-shadow``: release a ring no microphone edge claimed in time."""
        if self.shot_trigger != "ti-shadow":
            return
        with self._condition:
            notice = self._latched_notice
            if notice is None or not self._events.empty():
                return
        if time.monotonic_ns() - notice.monotonic_ns < SHADOW_CONSUME_AFTER_NOTICE_S * 1e9:
            return
        if time.monotonic() < self._release_retry_at:
            return
        self._release_latch("no microphone edge claimed it")

    def _release_latch(self, reason: str) -> bool:
        """``l3release`` the latched ring; on failure restart or recover the board."""
        with self._condition:
            notice = self._latched_notice
        try:
            self.radar.release_frozen_ring()
        except TiWedgedError as exc:
            self._last_serial_s = time.monotonic()
            logger.warning("[IWR6843] l3release got no verdict (%s): %s", reason, exc)
            self._release_retry_at = time.monotonic() + _LISTENER_ERROR_BACKOFF_S
            if self._recovery_available() and not self._cli_answers_resync() and self._running:
                self._recover_by_power_cycle(CAUSE_LABELS[SILENT_CLI])
            return False
        except RuntimeError as exc:
            self._last_serial_s = time.monotonic()
            logger.warning("[IWR6843] l3release refused (%s): %s", reason, exc)
            self._release_retry_at = time.monotonic() + _LISTENER_ERROR_BACKOFF_S
            self._restart_radar("l3release refused")
            return False
        self._last_serial_s = time.monotonic()
        with self._condition:
            if self._latched_notice is notice:
                self._latched_notice = None
        if self._shadow is not None and notice is not None:
            self._shadow.note_disposition(notice, "released")
        logger.info("[IWR6843] Released the self-triggered ring (%s)", reason)
        return True

    def _restart_radar(self, reason: str) -> None:
        """Configure the TI again (cfg, sensorStart, triggerCfg); recover a wedge."""
        logger.warning("[IWR6843] Restarting the TI capture (%s)", reason)
        try:
            self.radar.send_config(str(self.config_path))
            self._apply_self_trigger()
        except TiWedgedError as exc:
            logger.warning("[IWR6843] TI restart failed: %s", exc)
            if self._recovery_available():
                self._recover_by_power_cycle(exc.label)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] TI restart failed", exc_info=True)
        finally:
            self._last_serial_s = time.monotonic()

    def _probe_idle_board(self) -> None:
        """Every ``_IDLE_PROBE_S`` of silence, check the board still captures and is armed."""
        now = time.monotonic()
        if now - self._last_serial_s < _IDLE_PROBE_S:
            return
        with self._condition:
            if self._latched_notice is not None or not self._events.empty():
                return
        self._last_serial_s = now
        reply = self.radar.command("stats", 2.0)
        self._last_serial_s = time.monotonic()
        if not reply.ok:
            if reply.kind in (SILENT, ECHO_ONLY) and self._recovery_available():
                if not self._cli_answers_resync() and self._running:
                    self._recover_by_power_cycle(CAUSE_LABELS[SILENT_CLI])
            else:
                logger.warning("[IWR6843] Idle stats probe answered %s", reply.kind)
            return
        stats = parse_stats(reply.raw)
        previous, self._last_probe_stats = self._last_probe_stats, stats
        # A notice inside the stats reply is a fire the host can still take.
        self._dispatch_notices()
        with self._condition:
            claimed = self._latched_notice is not None or not self._events.empty()
        if stats.get("latched") == 1 and not claimed:
            logger.warning("[IWR6843] Board latched without a notice the host read; releasing")
            with self._condition:
                self._latched_notice = TriggerNotice(time.monotonic_ns(), time.time(), True)
            self._release_latch("lost notice")
            return
        if stats.get("active") == 0:
            self._restart_radar("capture inactive at the idle probe")
            return
        if stats.get("enabled") == 0:
            logger.warning("[IWR6843] Self-trigger found disabled at the idle probe; re-arming")
            self._apply_self_trigger()
            return
        verdict = OK if previous is None else assess_capture(previous, stats)
        if verdict != OK:
            label = CAUSE_LABELS.get(verdict, verdict)
            logger.warning("[IWR6843] Idle TI probe: %s", label)
            if self._recovery_available():
                self._recover_by_power_cycle(label)

    def _prepare_shadow_dump(self, edge_timestamp: float, edge_monotonic_ns: int | None) -> None:
        """``ti-shadow``: before a microphone edge's l3dump, settle the latched ring.

        A ring the board froze up to ``SHADOW_CONSUME_AFTER_NOTICE_S`` before
        the edge still holds the impact: l3dump streams it. An older one is
        released first, so the edge gets its own sound freeze. Without a latch
        the board may still fire for ``shadow_holdoff_s`` after the edge.
        """
        if edge_monotonic_ns is None:
            edge_monotonic_ns = time.monotonic_ns() - round((time.time() - edge_timestamp) * 1e9)
        self._dispatch_notices(self.radar.poll_trigger_notice(block=False))
        with self._condition:
            notice = self._latched_notice
        if notice is not None:
            if edge_monotonic_ns - notice.monotonic_ns <= SHADOW_CONSUME_AFTER_NOTICE_S * 1e9:
                self._take_latched_for_dump(notice)
                return
            self._release_latch("latched too long before the microphone edge")
        deadline_ns = edge_monotonic_ns + round(self._shadow_holdoff_s * 1e9)
        while self._running and time.monotonic_ns() < deadline_ns:
            self._dispatch_notices(self.radar.poll_trigger_notice(block=False))
            with self._condition:
                notice = self._latched_notice
            if notice is not None:
                self._take_latched_for_dump(notice)
                return
            time.sleep(_HOLDOFF_POLL_S)
        if self._shadow is not None:
            self._shadow.note_listen(
                edge_timestamp, round((time.monotonic_ns() - edge_monotonic_ns) / 1e6, 3)
            )

    def _take_latched_for_dump(self, notice: TriggerNotice) -> None:
        with self._condition:
            if self._latched_notice is notice:
                self._latched_notice = None
        if self._shadow is not None:
            self._shadow.note_disposition(notice, "dumped")

    def _notify_fire(
        self, edge_timestamp: float, notice: TriggerNotice, cause: str | None, sequence: int
    ) -> None:
        """Tell the fire observers (gate anchor, session log) which rule fired."""
        fields = {
            "kind": "fire",
            "mode": self.shot_trigger,
            "sequence": sequence,
            "edge_timestamp": edge_timestamp,
            "notice_timestamp": notice.wall_time,
            "notice_monotonic_ns": notice.monotonic_ns,
            "notice_in_reply": notice.in_reply,
            "cause": cause,
            "trigger_cfg": self.self_trigger.command if self.self_trigger else None,
        }
        for observer in self._fire_observers:
            try:
                observer(fields)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[IWR6843] Fire observer failed", exc_info=True)

    def _ensure_board_armed(self) -> None:
        """After a failed self-trigger dump: release a latch, else restart the capture."""
        try:
            reply = self.radar.command("stats", 2.0)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] stats failed after a failed dump", exc_info=True)
            reply = None
        self._last_serial_s = time.monotonic()
        if reply is not None and reply.ok:
            stats = parse_stats(reply.raw)
            if stats.get("active") == 1 and stats.get("latched", 0) == 0:
                return
            if stats.get("latched") == 1:
                with self._condition:
                    if self._latched_notice is None:
                        self._latched_notice = TriggerNotice(time.monotonic_ns(), time.time(), True)
                # A failed release restarts or recovers the board itself.
                self._release_latch("left latched by a failed dump")
                return
        self._restart_radar("not armed after a failed dump")

    def _capture(self, edge_timestamp: float) -> None:
        with self._condition:
            self._capture_active = True
            self._sequence += 1
            sequence = self._sequence
            fire_notice = self._fire_notices.pop(edge_timestamp, None)
            edge_monotonic_ns = self._edge_monotonic.pop(edge_timestamp, None)
        if fire_notice is not None:
            self._notify_fire(edge_timestamp, fire_notice, self._query_cause(), sequence)
        elif self.shot_trigger == "ti-shadow":
            self._prepare_shadow_dump(edge_timestamp, edge_monotonic_ns)
        start = time.time()
        raw = None
        failed_raw = None
        path = None
        error = None
        metadata = None
        cancelled = False
        l3dump_ms: tuple[float | None, float | None] = (None, None)
        try:
            if self._l3dump_delay_s > 0.0:
                l3dump_ms = self._wait_for_l3dump_delay(edge_timestamp, edge_monotonic_ns)
            logger.info(
                "[IWR6843] Trigger #%d: dumping firmware-frozen L3 ring",
                sequence,
            )
            if self._cancel_false_triggers:
                raw = self.radar.read_dump(cancel_event=self._cancel_dump)
            else:
                raw = self.radar.read_dump()
            metadata = self._validate_dump(raw)
            if self.save_dumps:
                path = self._capture_path(sequence, edge_timestamp)
                path.write_bytes(raw)
        except DumpCancelled as exc:
            cancelled = True
            error = str(exc)
            raw = None
            if self._running:
                logger.info("[IWR6843] False trigger #%d discarded; ready again", sequence)
            else:
                logger.info("[IWR6843] Dump #%d cancelled for shutdown", sequence)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            error = str(exc)
            if raw and metadata is None and self.save_dumps:
                self._save_short_dump(raw, sequence, edge_timestamp)
            failed_raw = raw
            raw = None
            logger.warning("[IWR6843] Capture #%d failed: %s", sequence, exc, exc_info=True)
        dump_error = error
        error = self._track_hwa_stall(error)
        completed = time.time()
        capture = IWR6843Capture(
            sequence=sequence,
            trigger_timestamp=edge_timestamp,
            completed_timestamp=completed,
            dump_duration_s=completed - start,
            raw=raw,
            path=path,
            error=error,
            temperature_report=(
                metadata.get("temperature_report") if metadata is not None else None
            ),
            range_window=metadata.get("range_window") if metadata is not None else None,
            l3dump_target_ms=l3dump_ms[0],
            l3dump_actual_ms=l3dump_ms[1],
        )
        self._finish_capture(capture)
        logger.info(
            "[IWR6843] Capture #%d complete: %s in %.2fs",
            sequence,
            f"{len(raw)} bytes" if raw is not None else error,
            capture.dump_duration_s,
        )
        if self.self_trigger is not None:
            self._last_serial_s = time.monotonic()
            with self._condition:
                # l3dump takes any latched freeze (l3_awaitFrozenRing), even when
                # it then fails; a fire after its Done is dispatched below.
                self._latched_notice = None
        label = self._recovery_label(dump_error, failed_raw)
        if label is not None:
            self._recover_by_power_cycle(label)
        elif self.self_trigger is not None and dump_error is not None and not cancelled:
            self._ensure_board_armed()
        if self.self_trigger is not None and self._running:
            # A notice after the dump's Done is a new fire.
            self._dispatch_notices()

    def _wait_for_l3dump_delay(
        self, edge_timestamp: float, edge_monotonic_ns: int | None
    ) -> tuple[float, float]:
        """Hold the ``l3dump`` until ``l3dump_delay_s`` after the edge.

        Returns the planned and the actual edge-to-l3dump time in ms. Raises
        DumpCancelled when the edge's dump is cancelled (or the monitor stops)
        during the wait: the ring was never asked for.
        """
        if edge_monotonic_ns is None:
            edge_monotonic_ns = time.monotonic_ns() - round((time.time() - edge_timestamp) * 1e9)
        target_ms = self._l3dump_delay_s * 1000.0
        with self._condition:
            cancel_event = self._cancel_dump
        remaining_s = (edge_monotonic_ns + round(target_ms * 1e6) - time.monotonic_ns()) / 1e9
        if (remaining_s > 0.0 and cancel_event.wait(remaining_s)) or cancel_event.is_set():
            raise DumpCancelled("TI dump cancelled during the l3dump delay")
        if not self._running:
            raise DumpCancelled("TI monitor stopping during the l3dump delay")
        actual_ms = round((time.monotonic_ns() - edge_monotonic_ns) / 1e6, 3)
        if actual_ms > target_ms + 5.0:
            logger.info(
                "[IWR6843] l3dump %.1f ms after the edge (planned %.0f ms)", actual_ms, target_ms
            )
        return target_ms, actual_ms

    def _save_short_dump(self, raw: bytes, sequence: int, trigger_timestamp: float) -> None:
        """Keep the bytes of a failed dump for offline diagnosis of the UART."""
        path = self._capture_path(sequence, trigger_timestamp).with_suffix(".short.l3dump")
        try:
            path.write_bytes(raw)
        except OSError as exc:
            logger.warning("[IWR6843] Could not save short dump %s: %s", path, exc)
            return
        logger.warning("[IWR6843] Saved short dump (%d bytes) to %s", len(raw), path)

    def _track_hwa_stall(self, error: str | None) -> str | None:
        """Name the HWA stall once consecutive dumps fail without an ILD1 payload."""
        if not is_class_a_dump_error(error):
            self._class_a_failures = 0
            return error
        self._class_a_failures += 1
        if self._class_a_failures < _HWA_STALL_CONSECUTIVE_FAILURES:
            return error
        # A pending USB power cycle usually saves the session; the "shut the
        # Pi down" text is reserved for when no recovery is left.
        if self._recovery_available():
            return HWA_STALL_RECOVERING_REASON
        if not self._hwa_stall_reported:
            self._hwa_stall_reported = True
            logger.error("[IWR6843] %s (last failure: %s)", HWA_STALL_SESSION_REASON, error)
        return HWA_STALL_SESSION_REASON

    def _recovery_available(self) -> bool:
        return (
            self._usb_power_cycler is not None
            and self._recoveries < self._max_recoveries
            and not self._ti_failed
            and self._running
            and self._recovery_guard_allows()
        )

    def _recovery_guard_allows(self) -> bool:
        """Ask the guard whether cutting USB power is safe right now.

        A blocking reason (e.g. the OPS is on USB too) makes the monitor
        behave as if it had no cycler; the reason is logged once a session.
        """
        if self._recovery_guard is None:
            return True
        try:
            reason = self._recovery_guard()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] TI recovery guard failed", exc_info=True)
            reason = "recovery guard failed"
        if reason is None:
            return True
        if not self._recovery_guard_warned:
            self._recovery_guard_warned = True
            logger.warning("[IWR6843] TI recovery by USB power cycle blocked: %s", reason)
        return False

    def _recovery_label(self, dump_error: str | None, failed_raw: bytes | None) -> str | None:
        """Wedge cause that warrants a USB power cycle now, else None.

        Class A needs consecutive stalled dumps. A dump that returned no
        payload and only silence or its own echo is probed with a resync: no
        prompt means a silent CLI (class B) after this one failure, a prompt
        means the command was merely lost.
        """
        if not self._recovery_available():
            return None
        if self._class_a_failures >= _HWA_STALL_CONSECUTIVE_FAILURES:
            return CAUSE_LABELS[HWA_STALL]
        label = wedge_label(dump_error)
        if label is not None:
            return label
        if self._looks_silent(failed_raw) and not self._cli_answers_resync():
            # The probe can take ~11 s on a silent CLI; never start a recovery
            # once stop() has begun.
            return CAUSE_LABELS[SILENT_CLI] if self._running else None
        return None

    @staticmethod
    def _looks_silent(failed_raw: bytes | None) -> bool:
        """Whether a failed dump's bytes are nothing but silence or the echo."""
        if failed_raw is None or MAGIC in failed_raw:
            return False
        kind = classify_reply("l3dump", failed_raw.decode(errors="replace"))
        return kind in (SILENT, ECHO_ONLY)

    def _cli_answers_resync(self) -> bool:
        """Probe the TI CLI for a prompt; edges only fan out meanwhile."""
        with self._condition:
            self._probing = True
        logger.warning("[IWR6843] TI sent no dump payload; checking whether its CLI answers")
        try:
            answered = bool(self.radar.resync())
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] TI resync failed", exc_info=True)
            answered = False
        finally:
            with self._condition:
                self._probing = False
                self._condition.notify_all()
        if answered:
            logger.info("[IWR6843] TI CLI answered; the l3dump command was lost, no recovery")
        return answered

    def _recover_by_power_cycle(self, label: str) -> None:
        """Power-cycle USB and reconfigure the TI; runs on the capture thread.

        Never raises. A failed recovery stops all further TI dumps for the
        session; edges still reach the trigger observers.
        """
        cycler = self._usb_power_cycler
        self._recoveries += 1
        started = time.monotonic()
        with self._condition:
            self._recovering = True
            self._recovery_edge_logged = False
            self._drop_queued_edge_locked()
            self._condition.notify_all()
        logger.warning(
            "[IWR6843] TI wedged (%s); power-cycling USB (attempt %d/%d)",
            label,
            self._recoveries,
            self._max_recoveries,
        )
        try:
            self._release_radar()
            self._raise_if_stopping()
            cycler.cycle()
            self._raise_if_stopping()
            if self.radar.port.startswith("/"):
                self._wait_for_port(self.radar.port)
            self._reopen_radar()
            self._radar_released = False
            self._raise_if_stopping()
            self.radar.send_config(str(self.config_path))
            # sensorStart cleared the board's trigger and any latch.
            with self._condition:
                self._fire_notices.clear()
                self._edge_monotonic.clear()
            self._apply_self_trigger()
        except _RecoveryAborted:
            self._end_recovery(failed=False)
            logger.info("[IWR6843] TI recovery aborted: the capture monitor is stopping")
            return
        except Exception:  # pylint: disable=broad-exception-caught
            if not self._running:
                self._end_recovery(failed=False)
                logger.info("[IWR6843] TI recovery ended by shutdown", exc_info=True)
                return
            logger.error("[IWR6843] TI recovery by USB power cycle failed", exc_info=True)
            self._end_recovery(failed=True)
            logger.error("[IWR6843] %s", TI_RECOVERY_FAILED_SESSION_REASON)
            return
        self._class_a_failures = 0
        self._hwa_stall_reported = False
        self._end_recovery(failed=False)
        logger.info(
            "[IWR6843] TI recovered by USB power cycle in %.1fs", time.monotonic() - started
        )

    def _drop_queued_edge_locked(self) -> None:
        """Drop an edge queued between the failed dump and the recovery start."""
        try:
            edge_timestamp = self._events.get_nowait()
        except queue.Empty:
            return
        if edge_timestamp is None:
            # The stop sentinel: keep it for the capture loop.
            self._events.put_nowait(None)
            return
        logger.info("[IWR6843] Edge %.3f dropped: TI is about to be power-cycled", edge_timestamp)

    def _release_radar(self) -> None:
        """Close the wedged port; the power cycle is the cure, so errors only warn."""
        self._radar_released = True
        try:
            self.radar.close()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] Closing the wedged TI port failed", exc_info=True)

    def _raise_if_stopping(self) -> None:
        if not self._running:
            raise _RecoveryAborted()

    def _wait_for_port(self, port: str) -> None:
        """Wait for the re-enumerated port, checking for shutdown between slices."""
        deadline = time.monotonic() + self.REENUMERATE_TIMEOUT_S
        while True:
            self._raise_if_stopping()
            remaining = max(0.0, deadline - time.monotonic())
            if wait_for_path(port, present=True, timeout_s=min(_PORT_WAIT_SLICE_S, remaining)):
                return
            if remaining <= 0.0:
                raise UsbPowerCycleError(
                    f"TI port {port} did not return within {self.REENUMERATE_TIMEOUT_S:.0f}s "
                    "after the USB power cycle"
                )

    def _reopen_radar(self) -> None:
        """Reopen the port, retrying while a fresh by-id link is not openable yet."""
        deadline = time.monotonic() + self.REOPEN_RETRY_S
        while True:
            self._raise_if_stopping()
            try:
                self.radar.reopen()
                return
            except (OSError, serial.SerialException) as exc:
                if time.monotonic() >= deadline:
                    raise
                logger.info("[IWR6843] TI port not openable yet (%s); retrying", exc)
            time.sleep(self.REOPEN_STEP_S)

    def _end_recovery(self, *, failed: bool) -> None:
        with self._condition:
            self._recovering = False
            if failed:
                self._ti_failed = True
            self._condition.notify_all()

    def _finish_capture(self, capture: IWR6843Capture) -> None:
        """Publish a completed dump unless a shot already discarded its edge."""
        with self._condition:
            self._capture_active = False
            self._dump_history.append((capture.trigger_timestamp, capture.completed_timestamp))
            if capture.trigger_timestamp in self._discard_edges:
                self._discard_edges.discard(capture.trigger_timestamp)
                logger.info(
                    "[IWR6843] Capture #%d dropped: its edge was outside a shot's match window",
                    capture.sequence,
                )
            else:
                self._captures.append(capture)
            self._condition.notify_all()

    def capture_for_shot(
        self,
        impact_timestamp: float | None,
        *,
        timeout_s: float = 12.0,
    ) -> IWR6843Capture | None:
        """Consume the capture nearest an OPS impact timestamp.

        Only an edge within ``match_tolerance_s`` of impact matches. When none
        does, the nearest edge within 1 s is consumed and returned as an
        invalid ``ti_edge_out_of_window`` capture that carries the match context.
        """
        deadline = time.monotonic() + timeout_s
        # impact_timestamp is wall-clock (OPS host time) and captures are
        # matched against it in that frame. The *wait* for the window to
        # close, though, is converted to the monotonic clock once here, so an
        # NTP step during the wait neither closes the window early nor
        # holds it open until the deadline.
        window_closes_mono = (
            float("inf")
            if impact_timestamp is None
            else time.monotonic() + (impact_timestamp + self.match_tolerance_s - time.time())
        )
        with self._condition:
            while True:
                if impact_timestamp is None and self._captures:
                    return self._captures.popleft()
                if impact_timestamp is None:
                    status = self._ti_status_capture_locked(time.time())
                    if status is not None:
                        return status

                if impact_timestamp is not None:
                    selected = self._nearest_capture_locked(
                        impact_timestamp, self.match_tolerance_s
                    )
                    if selected is not None:
                        self._captures.remove(selected)
                        self._discard_stale_locked(impact_timestamp)
                        return selected
                    if time.monotonic() > window_closes_mono and not (
                        self._matching_capture_active_locked(impact_timestamp)
                    ):
                        result = self._consume_out_of_window_locked(impact_timestamp)
                        self._discard_stale_locked(impact_timestamp)
                        if result is None:
                            result = self._ti_status_capture_locked(impact_timestamp)
                        return result

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _ti_status_capture_locked(self, timestamp: float) -> IWR6843Capture | None:
        """Invalid capture naming why the TI has no data (recovery only), else None."""
        if self._ti_failed:
            error = TI_RECOVERY_FAILED_SESSION_REASON
        elif self._recovering:
            error = TI_RESTARTING_REASON
        else:
            return None
        return IWR6843Capture(
            sequence=self._sequence,
            trigger_timestamp=timestamp,
            completed_timestamp=time.time(),
            dump_duration_s=None,
            raw=None,
            path=None,
            error=error,
        )

    def _nearest_capture_locked(
        self, impact_timestamp: float, limit_s: float
    ) -> IWR6843Capture | None:
        matches = [
            capture
            for capture in self._captures
            if abs(capture.trigger_timestamp - impact_timestamp) <= limit_s
        ]
        if not matches:
            return None
        return min(matches, key=lambda capture: abs(capture.trigger_timestamp - impact_timestamp))

    def _matching_capture_active_locked(self, impact_timestamp: float) -> bool:
        return (
            self._dump_busy_locked()
            and abs(self._last_edge_timestamp - impact_timestamp) <= self.match_tolerance_s
        )

    def _discard_stale_locked(self, impact_timestamp: float) -> None:
        cutoff = impact_timestamp - self.match_tolerance_s
        while self._captures and self._captures[0].trigger_timestamp < cutoff:
            stale = self._captures.popleft()
            logger.warning(
                "[IWR6843] Discarding unmatched capture #%d (edge %.3f, shot %.3f)",
                stale.sequence,
                stale.trigger_timestamp,
                impact_timestamp,
            )

    def _busy_at_locked(self, impact_timestamp: float) -> tuple[bool, float | None]:
        """Whether a TI dump was running at impact, and when the TI was ready (ms)."""
        for edge, completed in self._dump_history:
            if edge <= impact_timestamp < completed:
                return True, round((completed - impact_timestamp) * 1000.0, 1)
        if self._capture_active and self._last_edge_timestamp <= impact_timestamp:
            return True, None
        return False, None

    def _consume_out_of_window_locked(self, impact_timestamp: float) -> IWR6843Capture | None:
        candidates: list[tuple[float, IWR6843Capture | None]] = [
            (capture.trigger_timestamp, capture)
            for capture in self._captures
            if abs(capture.trigger_timestamp - impact_timestamp) < _OUT_OF_WINDOW_DISCARD_S
        ]
        if (
            self._dump_busy_locked()
            and abs(self._last_edge_timestamp - impact_timestamp) < _OUT_OF_WINDOW_DISCARD_S
        ):
            candidates.append((self._last_edge_timestamp, None))
        if not candidates:
            return None
        edge, capture = min(candidates, key=lambda item: abs(item[0] - impact_timestamp))
        busy, ready_ms = self._busy_at_locked(impact_timestamp)
        delta_ms = round((edge - impact_timestamp) * 1000.0, 1)
        logger.warning(
            "[IWR6843] Nearest TI edge %+.0f ms from impact (outside ±%.0f ms); %s",
            delta_ms,
            self.match_tolerance_s * 1000.0,
            (
                f"TI busy at impact until {ready_ms:+.0f} ms"
                if ready_ms is not None
                else ("TI busy at impact (dump still streaming)" if busy else "TI idle at impact")
            ),
        )
        context = {
            "raw": None,
            "error": _OUT_OF_WINDOW_ERROR,
            "temperature_report": None,
            "nearest_trigger_delta_ms": delta_ms,
            "ti_busy_at_impact": busy,
            "ti_ready_after_impact_ms": ready_ms,
        }
        if capture is not None:
            self._captures.remove(capture)
            return replace(capture, **context)
        self._discard_edges.add(edge)
        return IWR6843Capture(
            sequence=self._sequence + (0 if self._capture_active else 1),
            trigger_timestamp=edge,
            completed_timestamp=time.time(),
            dump_duration_s=None,
            path=None,
            **context,
        )

    def stop(self) -> None:
        """Drain active capture, stop firmware, then release host resources."""
        if not self._running:
            return
        self._armed = False
        self._running = False
        if self._first_byte_timer is not None:
            self._first_byte_timer.cancel()
        if self._button is not None:
            self._button.when_pressed = None
            self._button.close()
            self._button = None
        self.edge_timing.log_summary(
            f"BCM{self.gpio_pin} (TI monitor)" if self.uses_gpio else "TI self-trigger notices"
        )
        with self._condition:
            cancel_streaming_dump = self._capture_active and self._cancel_false_triggers
            if cancel_streaming_dump:
                self._cancel_dump.set()
        if cancel_streaming_dump:
            logger.info("[IWR6843] Cancelling the streaming TI dump before sensorStop")
        try:
            self._events.put_nowait(None)
        except queue.Full:
            pass
        if self._worker is not None:
            # Preserve a complete debug dump and its trailing CLI prompt before
            # issuing sensorStop. Closing early strands firmware mid-transfer.
            self._worker.join(timeout=_GRACEFUL_DUMP_SHUTDOWN_S)
            if self._worker.is_alive():
                logger.warning(
                    "[IWR6843] Active dump did not finish within %.1fs; "
                    "forcing serial close (board reset may be required)",
                    _GRACEFUL_DUMP_SHUTDOWN_S,
                )
                self.radar.close()
                self._worker.join(timeout=2.0)
            else:
                self._stop_sensor_and_close(retry=False)
            self._worker = None
        else:
            self._stop_sensor_and_close(retry=False)
        if self._shadow is not None:
            self._shadow.flush(everything=True)
            logger.info("[IWR6843] Self-trigger shadow summary: %s", self._shadow.summary())
        logger.info("[IWR6843] Capture monitor stopped")

    def _stop_sensor_and_close(self, *, retry: bool = True) -> None:
        """Best-effort firmware stop that never leaks the serial descriptor."""
        if self._radar_released:
            logger.info("[IWR6843] TI port already released by an interrupted recovery")
            return
        try:
            self.radar.stop_sensor(retry=retry)
            logger.info("[IWR6843] Firmware capture stopped and verified inactive")
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[IWR6843] Firmware did not stop cleanly; board reset may be required",
                exc_info=True,
            )
            self._log_firmware_stats()
        finally:
            self.radar.close()

    def _log_firmware_stats(self) -> None:
        """Best-effort ``stats`` snapshot after a failed firmware stop."""
        try:
            stats = self.radar.stats()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] TI stats unavailable after failed stop", exc_info=True)
            return
        logger.warning("[IWR6843] TI stats after failed stop: %s", " ".join(stats.split()))


__all__ = [
    "IWR6843Capture",
    "IWR6843CaptureMonitor",
    "tx_order_from_config",
]
