"""Pi-driven OPS243 hardware trigger, optionally gated by the camera.

After the microphone rework (analysis/range-20261011/umbau-mikrofon-gate.md)
the SEN-14262 GATE reaches only Pi BCM17, and a Pi output (BCM27) drives OPS
J3 pin 3 (HOST_INT) through 1 kOhm. Both lines are active HIGH: HOST_INT idles
LOW and the OPS dumps its rolling buffer on the rising edge. The pulse lasts
``pulse_ms`` (default 20 ms, like the tens-of-ms GATE pulse it replaces) and
must end before the next edge can pass the 100 ms ringing filter.

``TriggerGate.on_edge`` receives every BCM17 edge that passes the ringing
filter: from the TI monitor's observers, or from ``EdgeListener`` when no TI
monitor owns the pin. It runs on the GPIO callback thread and never blocks.

* ``passthrough``: HOST_INT goes HIGH at once, about 1 ms after the edge.
* ``camera``: a worker reads the live camera frames after the edge
  (``LiveDepartureCheck``). It pulses when the ball left the tee, when the
  answer is unclear and when anything fails. A ball seen still on the tee at
  the deadline blocks the pulse, and the TI dump that edge started is
  released. A watchdog pulses at the deadline whatever the worker is doing:
  a real strike is never lost to the gate.

With a camera, ``passthrough`` also runs the camera decision after each pulse
as a shadow (``GateRecord.shadow_*``, ``summary()["shadow"]``): what camera
mode would have done with the edge, logged but never acted on, so an
observation session measures the gate before it may block.

Every edge leaves a ``GateRecord``. An OPS capture finds its record through
the dump's first byte (``apply_to_capture``), which sets the capture's
``sound_edge_delay_ms``: the sound edge sits that long before HOST_INT in the
buffer, and club speed and impact anchor to it. A dump queued while the host
was still busy (pulse during the last re-arm settle or shot processing) is read
late; it falls back to the pulse after the radar was re-armed
(``LATE_FIRST_BYTE_MATCH_MS``).

The OPS is blind from the pulse that starts a dump until its re-arm is written
(``note_ops_dump_started`` / ``note_ops_dump_finished`` /
``note_ops_rearm_started`` / ``note_ops_armed``). Every pulse in that window is
lost: it is logged with its phase (dump transfer, host processing, re-arm drain
or commands), the edge that started the busy period and the time left, counted
in the summary and handed to the ``ops_busy`` listener (a ``trigger_event`` with
reason ``ops_busy``).

The OPS pre-trigger span must hold the club approach before the sound edge
even when the camera decides at the deadline (``camera_split_check``).

With ``edge_source="ti"`` (``--shot-trigger ti``, no microphone) the edges are
the IWR6843's self-trigger notices. Pulse, camera gate and shadow work as for
a sound edge; only the club-speed anchor moves: each record carries
``anchor_lead_ms`` (``TiAnchorModel``), how far the sound-edge-equivalent
anchor lies before the notice, first for an unknown rule and, once the TI
monitor read ``triggerLog track``, for the rule that fired
(``note_ti_cause``). The capture's ``sound_edge_delay_ms`` is then pulse delay
plus that lead (``ti_split_check`` sizes the pre-trigger span for it).

With the OPS243-A firmware 1.3.3 speed trigger (``openflight.ops_speed``) the
OPS trips its own rolling buffer and raises J3 pin 1; the gate never pulses
for such a trip. Each pin edge becomes a record with decision ``ops_speed``
whose "pulse" is the pin edge itself, so the first-byte match and the busy
tracking work unchanged, and whose club anchor lies ``ops_speed_anchor_ms``
after it (``sound_edge_delay_ms`` = -anchor):

* ``edge_source="ops-speed"`` (``--shot-trigger ops-speed``): ``on_edge``
  receives the pin edges; the microphone is only the witness's.
* ``ops_speed_shadow`` (``--shot-trigger ops-speed-shadow``, edge source
  ``sound``): ``note_ops_speed_edge`` records the pin edges; a microphone edge
  ``ops_speed_window_ms`` after one claims it (no pulse, the anchor is the
  microphone edge as today), any other microphone edge pulses HOST_INT as
  today (``ops_speed_fallback``).
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, replace
from typing import Callable

from openflight.camera.trigger_check import (
    STILL_THERE_EVIDENCE_MS,
    LiveDepartureCheck,
    LiveVerdict,
)
from openflight.edge_timing import EdgeTimingLog, edge_ticks
from openflight.gpio_factory import ensure_lgpio_pin_factory
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.iwr6843.self_trigger import TiAnchorModel
from openflight.ops_speed import (
    DEFAULT_ANCHOR_MS,
    DEFAULT_WITNESS_WINDOW_MS,
    OPS_SPEED,
    OPS_SPEED_SHADOW,
    parse_window_ms,
)
from openflight.rolling_buffer.processor import RollingBufferProcessor

logger = logging.getLogger(__name__)

GATE_MODES = ("passthrough", "camera")
EDGE_SOURCES = ("sound", "ti", "ops-speed")
# ti mode keeps more of the OPS buffer before HOST_INT than sound mode (S#6):
# at 20 ksps S#9 is 57.6 ms before / 147.2 ms after the pulse. With the of1
# firmware at offset 0 the slowest fire (+9.5 ms) needs 37.5 ms (S#6 would just
# hold it); S#9 also holds the +0.2 m fallback's slowest fire (+18.5 ms, 46.5 ms)
# without changing the split, and in passthrough leaves the full club history.
DEFAULT_TI_PRE_TRIGGER_SEGMENTS = 9
DEFAULT_PULSE_MS = 20.0
PULSE_MS_RANGE = (1.0, 30.0)
DEFAULT_DEADLINE_MS = 45.0
DEADLINE_MS_RANGE = (10.0, 60.0)
LATE_PULSE_MS = 5.0
DECISION_MARGIN_MS = 2.0
FRAME_DELIVERY_MS = 4.6
REFERENCE_LOOKBACK_MS = 150.0
FIRST_BYTE_LAG_MS = 17.0
FIRST_BYTE_MATCH_MS = (-10.0, 60.0)
# A dump queued while the host still slept after the final re-arm PA or
# processed a shot (range 2026-10-25, edge #111: first byte 270 ms after the
# edge, lag ~167 ms) is read late. Processing took up to ~575 ms there.
LATE_FIRST_BYTE_MATCH_MS = 1000.0
ARMED_HISTORY = 16
# "processing": the dump was read, the host parses it (and clock-syncs an
# accepted shot) before the re-arm starts.
OPS_BUSY_PHASES = ("dump", "processing", "rearm_drain", "rearm_commands")
SUMMARY_EVERY_EDGES = 20
RECORD_HISTORY = 64
# Shadow: a pin-1 edge this soon after a HOST_INT pulse is that pulse's own dump
# (if 1.3.3 raises pin 1 on HOST_INT dumps too, which M1 checks), not a speed trip.
HOST_INT_ECHO_MS = 5.0
# Shadow: the shortest first-byte lag a HOST_INT dump has shown (1.15 ms; 197 shots
# of 19.10., 30.10. and 02.11.). A fallback pulse matched below it came after the
# dump's trigger point, which a HOST_INT dump cannot do: the OPS tripped itself and
# pin 1 was not seen (loose or unwired J3 pin 1), so the busy board ignored the pulse.
HOST_INT_MIN_FIRST_BYTE_LAG_MS = 1.0

OPS_SEGMENT_SAMPLES = 128
OPS_BUFFER_SEGMENTS = 32
MIN_EDGE_HISTORY_MS = (
    RollingBufferProcessor.CLUB_ANCHOR_MAX_BACK_MS + RollingBufferProcessor.CLUB_ANCHOR_WINDOW_MS
)
FULL_EDGE_HISTORY_MS = (
    RollingBufferProcessor.CLUB_ANCHOR_MERGE_LATENCY_MS
    + RollingBufferProcessor.CLUB_ANCHOR_RISE_LOOKBACK_MS
)


def pre_trigger_ms(pre_trigger_segments: int, sample_rate_ksps: float = 30.0) -> float:
    """OPS buffer span before HOST_INT for an ``S#n`` split."""
    return pre_trigger_segments * OPS_SEGMENT_SAMPLES / sample_rate_ksps


def post_trigger_span_ms(pre_trigger_segments: int, sample_rate_ksps: float = 30.0) -> float:
    """OPS buffer span after HOST_INT (or a speed trip) for an ``S#n`` split."""
    return pre_trigger_ms(OPS_BUFFER_SEGMENTS - pre_trigger_segments, sample_rate_ksps)


def recommended_camera_split(deadline_ms: float, sample_rate_ksps: float = 30.0) -> int:
    """Smallest ``S#n`` that keeps the full club history before a deadline-late sound edge."""
    needed_ms = deadline_ms + FULL_EDGE_HISTORY_MS
    return min(OPS_BUFFER_SEGMENTS, math.ceil(needed_ms * sample_rate_ksps / OPS_SEGMENT_SAMPLES))


def _split_problem(
    pre_trigger_segments: int, anchor_before_pulse_ms: float, sample_rate_ksps: float
) -> tuple[str, float, float, float] | None:
    """``(level, pre_ms, history_ms, needed_ms)`` when the anchor lacks club history."""
    pre_ms = pre_trigger_ms(pre_trigger_segments, sample_rate_ksps)
    history_ms = pre_ms - anchor_before_pulse_ms
    if history_ms >= FULL_EDGE_HISTORY_MS:
        return None
    level = "error" if history_ms < MIN_EDGE_HISTORY_MS else "warning"
    needed_ms = MIN_EDGE_HISTORY_MS if level == "error" else FULL_EDGE_HISTORY_MS
    return level, pre_ms, history_ms, needed_ms


def camera_split_check(
    pre_trigger_segments: int, deadline_ms: float, sample_rate_ksps: float = 30.0
) -> tuple[str, str] | None:
    """``("error" | "warning", message)`` when ``S#n`` is too short for camera mode.

    With the pulse ``deadline_ms`` late, the sound edge sits that much earlier
    in the OPS buffer. The anchored club speed reads up to
    ``MIN_EDGE_HISTORY_MS`` before the edge (error below it) and checks the
    club's rise up to ``FULL_EDGE_HISTORY_MS`` before it (warning below it).
    """
    problem = _split_problem(pre_trigger_segments, deadline_ms, sample_rate_ksps)
    if problem is None:
        return None
    level, pre_ms, history_ms, needed_ms = problem
    return level, (
        f"S#{pre_trigger_segments} keeps {pre_ms:.1f} ms before HOST_INT; a pulse "
        f"{deadline_ms:.0f} ms after the sound edge leaves {history_ms:.1f} ms of club "
        f"approach, the club-speed rule needs {needed_ms:.1f} ms. Use --buffer-split "
        f"{recommended_camera_split(deadline_ms, sample_rate_ksps)} for camera mode."
    )


def ti_split_check(
    pre_trigger_segments: int,
    *,
    max_lead_ms: float,
    deadline_ms: float = 0.0,
    sample_rate_ksps: float = 30.0,
) -> tuple[str, str] | None:
    """``("error" | "warning", message)`` when ``S#n`` is too short for ``--shot-trigger ti``.

    The anchor lies up to ``max_lead_ms`` (the slowest firing rule) before the
    notice, and in camera mode the pulse comes up to ``deadline_ms`` after it,
    so the anchor sits their sum before HOST_INT; the same club history as for
    a sound edge must fit before it.
    """
    late_ms = max(0.0, max_lead_ms) + deadline_ms
    problem = _split_problem(pre_trigger_segments, late_ms, sample_rate_ksps)
    if problem is None:
        return None
    level, pre_ms, history_ms, needed_ms = problem
    return level, (
        f"S#{pre_trigger_segments} keeps {pre_ms:.1f} ms before HOST_INT; the TI anchor "
        f"sits up to {late_ms:.1f} ms before the pulse, leaving {history_ms:.1f} ms of club "
        f"approach, the club-speed rule needs {needed_ms:.1f} ms. Use --ti-pre-trigger "
        f"{recommended_camera_split(late_ms, sample_rate_ksps)}."
    )


def ops_speed_split_check(
    pre_trigger_segments: int, anchor_ms: float, sample_rate_ksps: float = 30.0
) -> tuple[str, str] | None:
    """``("error" | "warning", message)`` when ``S#n`` is too short for the speed trigger.

    The firmware speed trigger trips the buffer itself, and the club-speed
    anchor lies ``anchor_ms`` after that trip, so the club history before the
    anchor is the pre-trigger span plus ``anchor_ms``: error below
    ``MIN_EDGE_HISTORY_MS``, warning below ``FULL_EDGE_HISTORY_MS``. ``S#0``
    is always an error (nothing of the swing before the trip).
    """
    needed_segments = max(
        1,
        min(
            OPS_BUFFER_SEGMENTS,
            math.ceil((FULL_EDGE_HISTORY_MS - anchor_ms) * sample_rate_ksps / OPS_SEGMENT_SAMPLES),
        ),
    )
    advice = f"Use --ops-speed-pre-trigger {needed_segments}."
    if pre_trigger_segments <= 0:
        return "error", (
            f"S#{pre_trigger_segments} keeps nothing before the speed trip; the club "
            f"approach before it is lost. {advice}"
        )
    problem = _split_problem(pre_trigger_segments, -anchor_ms, sample_rate_ksps)
    if problem is None:
        return None
    level, pre_ms, history_ms, needed_ms = problem
    return level, (
        f"S#{pre_trigger_segments} keeps {pre_ms:.1f} ms before the speed trip; with the "
        f"club-speed anchor {anchor_ms:g} ms after it that leaves {history_ms:.1f} ms of club "
        f"approach, the club-speed rule needs {needed_ms:.1f} ms. {advice}"
    )


@dataclass
class GateRecord:  # pylint: disable=too-many-instance-attributes
    """What the gate did with one sound edge; timestamps are host epoch seconds.

    ``decision`` is ``passthrough``, ``departed``, ``unclear`` (all pulsed) or
    ``still_there`` (blocked); ``pending`` until settled. ``shadow_*`` is the
    camera decision a passthrough gate with a camera computed after its pulse
    (``still_there`` = camera mode would have blocked), None otherwise.

    ``ops_speed`` is an OPS speed trip on J3 pin 1 (``ops_speed_mode`` set):
    ``pulse_timestamp`` is the pin edge, where the OPS triggered itself, and
    no HOST_INT pulse went out. A pin record keeps edge source ``ops-speed``;
    one a microphone edge claimed (shadow) takes that edge and source
    ``sound``. ``ops_speed_fallback`` marks a shadow-mode microphone edge with
    no trip before it, pulsed as in sound mode; ``ops_speed_echo_ms`` how long
    after its pulse pin 1 rose with that pulse's dump (None: it did not).
    """

    sequence: int
    mode: str
    edge_timestamp: float
    decision: str = "pending"
    reason: str | None = None
    pulse_timestamp: float | None = None
    decided_timestamp: float | None = None
    first_departure_ms: float | None = None
    frames_used: int = 0
    frame_states: str = ""
    ti_released: bool = False
    edge_source: str = "sound"
    ti_cause: str | None = None
    anchor_lead_ms: float = 0.0
    first_byte_lag_ms: float | None = None
    first_byte_match: str | None = None
    shadow_decision: str | None = None
    shadow_reason: str | None = None
    shadow_first_departure_ms: float | None = None
    shadow_frame_states: str = ""
    ops_speed_mode: str | None = None
    ops_speed_edge_timestamp: float | None = None
    ops_speed_fallback: bool = False
    ops_speed_echo_ms: float | None = None

    @property
    def pulsed(self) -> bool:
        """Whether HOST_INT was pulsed for this edge."""
        return self.pulse_timestamp is not None

    @property
    def delay_ms(self) -> float | None:
        """Sound edge to HOST_INT rising edge."""
        if self.pulse_timestamp is None:
            return None
        return (self.pulse_timestamp - self.edge_timestamp) * 1000.0

    @property
    def anchor_delay_ms(self) -> float | None:
        """Club-speed anchor to HOST_INT: the pulse delay plus the TI anchor lead."""
        if self.delay_ms is None:
            return None
        return self.delay_ms + self.anchor_lead_ms

    @property
    def ops_speed_self_trip(self) -> bool:
        """Shadow: this fallback pulse was matched to a dump that started before it.

        Only for a copy from ``record_for_capture`` (``first_byte_lag_ms`` set):
        a lag below ``HOST_INT_MIN_FIRST_BYTE_LAG_MS`` puts the dump's trigger
        point before the pulse, so the OPS tripped itself unseen. A pulse pin 1
        echoed (``ops_speed_echo_ms``) did start its dump.
        """
        return (
            self.ops_speed_fallback
            and self.ops_speed_echo_ms is None
            and self.first_byte_match == "window"
            and self.first_byte_lag_ms is not None
            and self.first_byte_lag_ms < HOST_INT_MIN_FIRST_BYTE_LAG_MS
        )

    @property
    def decision_latency_ms(self) -> float | None:
        """Sound edge to the settled decision."""
        if self.decided_timestamp is None:
            return None
        return (self.decided_timestamp - self.edge_timestamp) * 1000.0

    def log_fields(self) -> dict:
        """Fields merged into ``rolling_buffer_capture`` and ``trigger_event`` records.

        The ``ops_speed`` fields are only present with the speed trigger on.
        """
        fields = {
            "gate_mode": self.mode,
            "gate_decision": self.decision,
            "gate_reason": self.reason,
            "sound_edge_delay_ms": self.anchor_delay_ms,
            "gate_pulse_delay_ms": self.delay_ms,
            "gate_edge_source": self.edge_source,
            "ti_fire_cause": self.ti_cause,
            "ti_anchor_lead_ms": self.anchor_lead_ms if self.edge_source == "ti" else None,
            "gate_decision_latency_ms": self.decision_latency_ms,
            "gate_frames_used": self.frames_used,
            "gate_frame_states": self.frame_states,
            "gate_edge_timestamp": self.edge_timestamp,
            "gate_first_byte_lag_ms": self.first_byte_lag_ms,
            "gate_first_byte_match": self.first_byte_match,
            "gate_shadow_decision": self.shadow_decision,
            "gate_shadow_reason": self.shadow_reason,
            "gate_shadow_first_departure_ms": self.shadow_first_departure_ms,
            "gate_shadow_frame_states": self.shadow_frame_states,
        }
        if self.ops_speed_mode is not None:
            fields.update(
                gate_ops_speed_mode=self.ops_speed_mode,
                gate_ops_speed_edge_timestamp=self.ops_speed_edge_timestamp,
                ops_speed_fallback=self.ops_speed_fallback,
                ops_speed_host_int_echo_ms=self.ops_speed_echo_ms,
                ops_speed_anchor_ms=(
                    -self.anchor_lead_ms if self.edge_source == "ops-speed" else None
                ),
            )
        return fields


@dataclass
class _OpsBusy:
    """One OPS blind period: from the pulse that started a dump (or a bare re-arm).

    ``started_by`` is ``"dump"`` or ``"rearm"``; a dump whose pulse was not
    found keeps ``start_sequence`` None.
    """

    start_timestamp: float
    start_sequence: int | None = None
    start_edge_timestamp: float | None = None
    rearm_start_timestamp: float | None = None
    started_by: str = "dump"
    dump_end_timestamp: float | None = None

    def phase_at(self, timestamp: float, drain_end_timestamp: float | None) -> str:
        """Which part of the busy period ``timestamp`` fell in."""
        if self.rearm_start_timestamp is None or timestamp < self.rearm_start_timestamp:
            if self.dump_end_timestamp is not None and timestamp >= self.dump_end_timestamp:
                return "processing"
            return "dump"
        if drain_end_timestamp is None or timestamp < drain_end_timestamp:
            return "rearm_drain"
        return "rearm_commands"


def _busy_origin(busy: _OpsBusy) -> str:
    """What started a busy period, for the log line."""
    if busy.start_sequence is not None:
        return f"edge #{busy.start_sequence}"
    return "an unmatched dump" if busy.started_by == "dump" else "a re-arm"


class _Scheduler:
    """Runs callbacks at monotonic deadlines on one thread: pulse ends and decision deadlines."""

    def __init__(self, monotonic_ns: Callable[[], int]):
        self._now = monotonic_ns
        self._heap: list[tuple[int, int, Callable[[], None]]] = []
        self._order = itertools.count()
        self._condition = threading.Condition()
        self._running = False
        self._thread: threading.Thread | None = None

    def call_at(self, due_ns: int, callback: Callable[[], None]) -> None:
        """Run ``callback`` once the monotonic clock reaches ``due_ns``."""
        with self._condition:
            heapq.heappush(self._heap, (int(due_ns), next(self._order), callback))
            self._condition.notify()

    def run_due(self, *, everything: bool = False) -> None:
        """Run the callbacks that are due (all of them with ``everything``)."""
        while True:
            with self._condition:
                if not self._heap or (not everything and self._heap[0][0] > self._now()):
                    return
                callback = heapq.heappop(self._heap)[2]
            try:
                callback()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[GATE] Scheduled callback failed", exc_info=True)

    def _loop(self) -> None:
        while True:
            self.run_due()
            with self._condition:
                if not self._running:
                    return
                timeout = max(0.0, (self._heap[0][0] - self._now()) / 1e9) if self._heap else None
                self._condition.wait(timeout)

    def start(self) -> None:
        """Start the scheduler thread."""
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="trigger-gate-timer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread, then run whatever is still queued (a pulse end must happen)."""
        with self._condition:
            self._running = False
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.run_due(everything=True)


class TriggerGate:  # pylint: disable=too-many-instance-attributes
    """Drive OPS HOST_INT from the Pi on sound edges, optionally gated by the camera.

    ``camera`` must offer ``running``, ``trigger_check_anchor()`` and
    ``live_frames_after(after_ns, timeout_s)`` (``CameraCaptureRuntime``); its
    ``note_blocked_edge(edge_timestamp)``, when offered, runs after every block
    so the edge's clip can prove a stale tee anchor wrong.
    ``on_pulse`` runs after every pulse (the OPS stall watch), ``release_edge``
    with the edge timestamp after every block (the TI dump). With
    ``background=False`` no threads start; ``run_pending`` and
    ``run_due_timers`` then drive the gate (tests). ``edge_timing`` (the edge
    source's ``EdgeTimingLog``) adds the kernel edge stamps to the dump fields.

    The OPS speed trigger (``edge_source="ops-speed"`` or ``ops_speed_shadow``)
    needs ``passthrough``: the gate cannot block a dump the OPS started itself.
    ``on_pulse`` then also runs after every speed trip (a trip must bring a dump).
    ``ops_speed_post_trigger_ms`` (the dump's post-trigger span) lets
    ``ops_busy_now`` count the OPS blind from a trip or pulse on, before its
    dump's first byte arrives.
    """

    def __init__(  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        pin: int,
        mode: str = "passthrough",
        pulse_ms: float = DEFAULT_PULSE_MS,
        deadline_ms: float = DEFAULT_DEADLINE_MS,
        camera=None,
        on_pulse: Callable[[], None] | None = None,
        release_edge: Callable[[float], bool] | None = None,
        output_factory: Callable | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        wall_time: Callable[[], float] = time.time,
        background: bool = True,
        edge_timing: EdgeTimingLog | None = None,
        edge_source: str = "sound",
        ti_anchor: TiAnchorModel | None = None,
        ops_speed_shadow: bool = False,
        ops_speed_anchor_ms: float = DEFAULT_ANCHOR_MS,
        ops_speed_window_ms: tuple[float, float] = DEFAULT_WITNESS_WINDOW_MS,
        ops_speed_post_trigger_ms: float | None = None,
    ):
        if mode not in GATE_MODES:
            raise ValueError(f"trigger gate mode must be one of {GATE_MODES}, got {mode!r}")
        if not PULSE_MS_RANGE[0] <= pulse_ms <= PULSE_MS_RANGE[1]:
            raise ValueError(f"HOST_INT pulse must last {PULSE_MS_RANGE} ms, got {pulse_ms}")
        if not DEADLINE_MS_RANGE[0] <= deadline_ms <= DEADLINE_MS_RANGE[1]:
            raise ValueError(
                f"camera decision deadline must be {DEADLINE_MS_RANGE} ms, got {deadline_ms}"
            )
        if edge_source not in EDGE_SOURCES:
            raise ValueError(f"edge source must be one of {EDGE_SOURCES}, got {edge_source!r}")
        if ops_speed_shadow and edge_source != "sound":
            raise ValueError("the ops-speed shadow gate takes microphone edges (edge source sound)")
        if (edge_source == "ops-speed" or ops_speed_shadow) and mode != "passthrough":
            raise ValueError("the OPS speed trigger needs a passthrough gate")
        if not 0.0 <= ops_speed_anchor_ms <= 100.0:
            raise ValueError(f"OPS speed anchor must be 0-100 ms, got {ops_speed_anchor_ms}")
        self.pin = pin
        self.mode = mode
        self.edge_source = edge_source
        self.ti_anchor = (ti_anchor or TiAnchorModel()) if edge_source == "ti" else None
        self.ops_speed_shadow = bool(ops_speed_shadow)
        self.ops_speed_mode = (
            OPS_SPEED
            if edge_source == "ops-speed"
            else OPS_SPEED_SHADOW
            if ops_speed_shadow
            else None
        )
        self.ops_speed_anchor_ms = float(ops_speed_anchor_ms)
        self.ops_speed_window_ms = parse_window_ms(ops_speed_window_ms)
        self.ops_speed_post_trigger_ms = (
            None if ops_speed_post_trigger_ms is None else float(ops_speed_post_trigger_ms)
        )
        self.pulse_ms = pulse_ms
        self.deadline_ms = deadline_ms
        self._camera = camera
        self._on_pulse = on_pulse
        self._release_edge = release_edge
        self._output_factory = output_factory
        self._monotonic_ns = monotonic_ns
        self._wall_time = wall_time
        self._background = background
        self._edge_timing = edge_timing
        self._device = None
        self._running = False
        self._lock = threading.Lock()
        self._records: deque[GateRecord] = deque(maxlen=RECORD_HISTORY)
        self._sequence = 0
        self._pulse_until_ns = 0
        self._counts: Counter = Counter()
        self._unclear_reasons: Counter = Counter()
        self._shadow_decisions: Counter = Counter()
        self._shadow_unclear_reasons: Counter = Counter()
        self._ops_busy: _OpsBusy | None = None
        self._ops_busy_phases: Counter = Counter()
        self._ops_busy_listener: Callable[[dict], None] | None = None
        self._armed_floors: deque[float] = deque(maxlen=ARMED_HISTORY)
        self._jobs: queue.Queue = queue.Queue()
        self._scheduler = _Scheduler(monotonic_ns)
        self._worker: threading.Thread | None = None

    @property
    def can_block(self) -> bool:
        """Whether a still_there verdict is reachable before the deadline."""
        last_frame_ms = self.deadline_ms - DECISION_MARGIN_MS - FRAME_DELIVERY_MS
        return self.mode == "camera" and last_frame_ms >= STILL_THERE_EVIDENCE_MS

    def start(self) -> None:
        """Claim the HOST_INT output (LOW) and start the worker and timer threads."""
        if self._running:
            return
        factory = self._output_factory
        if factory is None:
            ensure_lgpio_pin_factory()
            from gpiozero import (  # pylint: disable=import-error,import-outside-toplevel
                DigitalOutputDevice,
            )

            factory = DigitalOutputDevice
        self._device = factory(self.pin, active_high=True, initial_value=False)
        self._running = True
        if self._background:
            self._scheduler.start()
            self._worker = threading.Thread(
                target=self._work_loop, name="trigger-gate", daemon=True
            )
            self._worker.start()
        if self.mode == "camera":
            extra = f", decision deadline {self.deadline_ms:.0f} ms"
        elif self._camera is not None:
            extra = f", camera shadow (deadline {self.deadline_ms:.0f} ms, never blocks)"
        else:
            extra = ""
        if self.ti_anchor is not None:
            extra += (
                "; edges are TI self-trigger notices (club anchor "
                f"{self.ti_anchor.lead_ms(None):+.1f} ms before the notice until the rule is known)"
            )
        if self.ops_speed_mode == OPS_SPEED:
            extra += (
                "; edges are OPS speed trips on J3 pin 1, never pulsed (HOST_INT stays LOW; "
                f"club anchor {self.ops_speed_anchor_ms:g} ms after the trip)"
            )
        elif self.ops_speed_mode == OPS_SPEED_SHADOW:
            low, high = self.ops_speed_window_ms
            extra += (
                f"; OPS speed shadow: a sound edge {low:g}-{high:g} ms after a speed trip claims "
                "it without a pulse, any other sound edge pulses"
            )
        logger.info(
            "[GATE] HOST_INT on BCM%d, %s mode, %.0f ms pulse%s",
            self.pin,
            self.mode,
            self.pulse_ms,
            extra,
        )
        if self.mode == "camera" and not self.can_block:
            logger.warning(
                "[GATE] A %.0f ms deadline sees no frame %.0f ms after the edge; "
                "the camera gate will pulse every edge",
                self.deadline_ms,
                STILL_THERE_EVIDENCE_MS,
            )

    def stop(self) -> None:
        """Stop the threads, end any pulse, release the output and log the summary."""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._jobs.put(None)
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None
        self._scheduler.stop()
        with self._lock:
            device, self._device = self._device, None
            busy, self._ops_busy = self._ops_busy, None
        device.off()
        device.close()
        if busy is not None:
            self._resolve_ops_busy(busy, None, None, "session_end")
        logger.info("[GATE] Session summary: %s", self.summary_text())

    def on_edge(self, edge_timestamp: float | None = None) -> None:
        """Sound-edge observer on the GPIO callback thread: pulse or queue, never block.

        With ``edge_source="ops-speed"`` the edges are OPS speed trips on pin 1
        (no pulse); in the ops-speed shadow a sound edge first tries to claim a trip.
        """
        if not self._running:
            return
        if self.ops_speed_mode == OPS_SPEED:
            self.note_ops_speed_edge(edge_timestamp)
            return
        wall = self._wall_time()
        now_ns = self._monotonic_ns()
        edge_timestamp = wall if edge_timestamp is None else float(edge_timestamp)
        edge_ns = now_ns - round((wall - edge_timestamp) * 1e9)
        with self._lock:
            claimed = (
                self._claim_ops_speed_locked(edge_timestamp) if self.ops_speed_shadow else None
            )
            if claimed is None:
                self._sequence += 1
                record = GateRecord(self._sequence, self.mode, edge_timestamp)
                if self.ti_anchor is not None:
                    record.edge_source = "ti"
                    record.anchor_lead_ms = self.ti_anchor.lead_ms(None)
                if self.ops_speed_shadow:
                    record.ops_speed_mode = OPS_SPEED_SHADOW
                    record.ops_speed_fallback = True
                    self._counts["ops_speed_fallback"] += 1
                self._records.append(record)
        if claimed is not None:
            self._jobs.put((claimed, edge_ns))
            return
        if self.mode == "passthrough":
            self._settle(record, edge_ns, LiveVerdict("passthrough", "passthrough"))
        else:
            self._scheduler.call_at(
                edge_ns + round(self.deadline_ms * 1e6),
                lambda: self._on_deadline(record, edge_ns),
            )
        self._jobs.put((record, edge_ns))

    def note_ops_speed_edge(self, edge_timestamp: float | None = None) -> bool:
        """An OPS speed trip on J3 pin 1 (GPIO callback thread): record it, never pulse.

        The OPS dumped by itself at the edge, so the record's pulse is the edge
        and its club anchor lies ``ops_speed_anchor_ms`` after it. In the shadow
        the record waits for a sound edge to claim it, and a pin edge within
        ``HOST_INT_ECHO_MS`` after a fallback pulse is that pulse's dump: it is
        noted on the pulse's record instead. True when a trip was recorded.
        """
        if not self._running or self.ops_speed_mode is None:
            return False
        wall = self._wall_time()
        now_ns = self._monotonic_ns()
        edge_timestamp = wall if edge_timestamp is None else float(edge_timestamp)
        edge_ns = now_ns - round((wall - edge_timestamp) * 1e9)
        with self._lock:
            echo = self._host_int_echo_locked(edge_timestamp) if self.ops_speed_shadow else None
            if echo is not None:
                self._counts["ops_speed_host_int_echoes"] += 1
        if echo is not None:
            logger.info(
                "[GATE] edge #%d: pin 1 rose %.1f ms after its HOST_INT pulse (the pulse's "
                "own dump, not a speed trip)",
                echo.sequence,
                echo.ops_speed_echo_ms,
            )
            return False
        with self._lock:
            self._sequence += 1
            record = GateRecord(
                self._sequence,
                self.mode,
                edge_timestamp,
                decision="ops_speed",
                reason="speed_trip",
                pulse_timestamp=edge_timestamp,
                decided_timestamp=edge_timestamp,
                edge_source="ops-speed",
                anchor_lead_ms=-self.ops_speed_anchor_ms,
                ops_speed_mode=self.ops_speed_mode,
                ops_speed_edge_timestamp=edge_timestamp,
            )
            self._records.append(record)
            # A copy: a sound edge may claim the record before the worker reports it.
            job = replace(record)
        self._jobs.put((job, edge_ns))
        return True

    def _host_int_echo_locked(self, edge_timestamp: float) -> GateRecord | None:
        """Shadow: the HOST_INT pulse a pin edge followed within ``HOST_INT_ECHO_MS``."""
        for record in reversed(self._records):
            if record.ops_speed_edge_timestamp is not None or record.pulse_timestamp is None:
                continue
            lag_ms = (edge_timestamp - record.pulse_timestamp) * 1000.0
            if 0.0 <= lag_ms <= HOST_INT_ECHO_MS:
                record.ops_speed_echo_ms = round(lag_ms, 3)
                return record
        return None

    def _claim_ops_speed_locked(self, edge_timestamp: float) -> GateRecord | None:
        """Shadow: the unclaimed speed trip ``ops_speed_window_ms`` before a sound edge.

        The claimed record takes the sound edge, so the club anchor is that
        edge as in sound mode, and keeps the trip as its pulse. Returns a copy
        of it for the worker's report; None when no trip qualifies (the sound
        edge then pulses).
        """
        low, high = self.ops_speed_window_ms
        candidates = [
            record
            for record in self._records
            if record.edge_source == "ops-speed"
            and record.ops_speed_edge_timestamp is not None
            and low <= (edge_timestamp - record.ops_speed_edge_timestamp) * 1000.0 <= high
        ]
        if not candidates:
            return None
        record = min(
            candidates, key=lambda item: abs(edge_timestamp - item.ops_speed_edge_timestamp)
        )
        record.edge_timestamp = edge_timestamp
        record.edge_source = "sound"
        record.anchor_lead_ms = 0.0
        record.reason = "sound_claimed"
        record.decided_timestamp = edge_timestamp
        return replace(record)

    def ops_speed_fallback_for(self, edge_timestamp: float) -> bool | None:
        """Shadow: whether the sound edge ``edge_timestamp`` was pulsed (no trip before it)."""
        if not self.ops_speed_shadow:
            return None
        with self._lock:
            record = next(
                (item for item in self._records if item.edge_timestamp == edge_timestamp), None
            )
        if record is None:
            return None
        return record.ops_speed_fallback

    def ops_busy_now(self, at: float | None = None) -> bool:
        """Whether the OPS was blind at ``at`` (host epoch s, default now).

        Blind from a dump's first byte (or a bare re-arm) until it is armed
        again. With ``ops_speed_post_trigger_ms`` also from the last trip or
        pulse before ``at`` until its dump can still arrive (post-trigger span
        plus ``FIRST_BYTE_MATCH_MS``), unless the OPS was re-armed after it:
        the board stops listening at its trigger, well before the first byte.
        With ``at`` given, a pulse at or after it (the shadow's fallback pulse
        for the microphone edge ``at`` itself) does not count.
        """
        when = self._wall_time() if at is None else float(at)
        with self._lock:
            if self._ops_busy is not None:
                return True
            if self.ops_speed_post_trigger_ms is None:
                return False
            last = max(
                (
                    record.pulse_timestamp
                    for record in self._records
                    if record.pulse_timestamp is not None
                    and (at is None or record.pulse_timestamp < when)
                ),
                default=None,
            )
            if last is None or (self._armed_floors and self._armed_floors[-1] >= last):
                return False
        horizon_ms = self.ops_speed_post_trigger_ms + FIRST_BYTE_MATCH_MS[1]
        return (when - last) * 1000.0 <= horizon_ms

    def classify_ops_speed_dump(
        self, first_byte_timestamp: float | None, post_trigger_ms: float | None
    ) -> str:
        """What started an OPS dump under the speed trigger.

        ``pin`` (``ops-speed``: a speed trip), ``matched`` (shadow: a trip a
        sound edge claimed), ``unmatched`` (shadow: a trip no sound edge
        claimed), ``fallback`` (shadow: a HOST_INT pulse for a sound edge
        without a trip), ``self_trip`` (shadow: the dump began before the
        fallback pulse it matched, so the OPS tripped itself and pin 1 was not
        seen) or ``no_record`` (no gate record: pin 1 silent?).
        """
        record = self.record_for_capture(first_byte_timestamp, post_trigger_ms)
        if record is None:
            return "no_record"
        if record.ops_speed_self_trip:
            return "self_trip"
        if record.ops_speed_edge_timestamp is None:
            return "fallback"
        if record.edge_source == "ops-speed":
            return "unmatched" if self.ops_speed_shadow else "pin"
        return "matched"

    def note_ti_cause(self, edge_timestamp: float, cause: str | None) -> bool:
        """Anchor the TI edge ``edge_timestamp`` by the rule that fired; False if unknown."""
        if self.ti_anchor is None:
            return False
        with self._lock:
            record = next(
                (item for item in self._records if item.edge_timestamp == edge_timestamp), None
            )
            if record is None:
                return False
            record.ti_cause = cause
            record.anchor_lead_ms = self.ti_anchor.lead_ms(cause)
            lead = record.anchor_lead_ms
        logger.info(
            "[GATE] edge #%d: TI fired by %s, club anchor %+.1f ms before the notice",
            record.sequence,
            cause or "an unknown rule",
            lead,
        )
        return True

    def _settle(
        self,
        record: GateRecord,
        edge_ns: int,
        verdict: LiveVerdict,
        check: LiveDepartureCheck | None = None,
    ) -> bool:
        """Settle ``record`` once, pulsing unless the ball is still there; True if settled here."""
        with self._lock:
            if record.decision != "pending" or not self._running:
                return False
            decided_ns = self._monotonic_ns()
            if verdict.decision != "still_there":
                self._device.on()
                decided_ns = self._monotonic_ns()
                self._pulse_until_ns = decided_ns + round(self.pulse_ms * 1e6)
                self._scheduler.call_at(self._pulse_until_ns, self._end_pulse)
                record.pulse_timestamp = record.edge_timestamp + (decided_ns - edge_ns) / 1e9
            record.decided_timestamp = record.edge_timestamp + (decided_ns - edge_ns) / 1e9
            record.decision, record.reason = verdict.decision, verdict.reason
            record.first_departure_ms = verdict.first_departure_ms_after_edge
            if check is not None:
                record.frames_used, record.frame_states = len(check.states), check.states
            return True

    def _end_pulse(self) -> None:
        with self._lock:
            if self._device is not None and self._monotonic_ns() >= self._pulse_until_ns:
                self._device.off()

    def _on_deadline(self, record: GateRecord, edge_ns: int) -> None:
        if self._settle(record, edge_ns, LiveVerdict("unclear", "timeout")):
            logger.warning(
                "[GATE] Edge #%d: no camera decision by the %.0f ms deadline",
                record.sequence,
                self.deadline_ms,
            )
            self._report(record)

    def _work_loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            self._handle(*job)

    def run_pending(self) -> None:
        """Handle the queued edges on the calling thread (``background=False``)."""
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None:
                self._handle(*job)

    def run_due_timers(self) -> None:
        """Run due pulse ends and deadlines on the calling thread (``background=False``)."""
        self._scheduler.run_due()

    def _handle(self, record: GateRecord, edge_ns: int) -> None:
        if record.decision == "ops_speed":
            self._report_ops_speed(record)
            return
        if record.mode == "passthrough":
            self._report(record)
            if self._camera is not None:
                self._shadow(record, edge_ns)
            return
        try:
            verdict, check = self._camera_verdict(record, edge_ns)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[GATE] Camera decision for edge #%d failed", record.sequence, exc_info=True
            )
            verdict, check = LiveVerdict("unclear", "error"), None
        if verdict is None or not self._settle(record, edge_ns, verdict, check):
            return
        if not record.pulsed and self._release_edge is not None:
            try:
                released = bool(self._release_edge(record.edge_timestamp))
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[GATE] Releasing the TI dump failed", exc_info=True)
                released = False
            with self._lock:
                record.ti_released = released
        if not record.pulsed:
            self._note_blocked_edge(record)
        self._report(record)

    def _note_blocked_edge(self, record: GateRecord) -> None:
        """Tell the camera which edge was blocked (its clip may prove the tee anchor stale)."""
        note = getattr(self._camera, "note_blocked_edge", None)
        if not callable(note):
            return
        try:
            note(record.edge_timestamp)  # pylint: disable=not-callable
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "[GATE] Reporting blocked edge #%d failed", record.sequence, exc_info=True
            )

    def _shadow(self, record: GateRecord, edge_ns: int) -> None:
        """Passthrough with a camera: record what camera mode would have decided.

        The pulse went out already; this only logs and counts. A decision
        later than the deadline counts as ``unclear``/``timeout``, as the
        watchdog would have pulsed by then.
        """
        deadline_ns = edge_ns + round(self.deadline_ms * 1e6)
        verdict, check = LiveVerdict("unclear", "timeout"), None
        if self._monotonic_ns() < deadline_ns:
            try:
                verdict, check = self._camera_verdict(record, edge_ns, shadow=True)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "[GATE] Camera shadow for edge #%d failed", record.sequence, exc_info=True
                )
                verdict = LiveVerdict("unclear", "error")
            if self._monotonic_ns() > deadline_ns:
                verdict = LiveVerdict("unclear", "timeout")
        with self._lock:
            record.shadow_decision, record.shadow_reason = verdict.decision, verdict.reason
            record.shadow_first_departure_ms = verdict.first_departure_ms_after_edge
            record.shadow_frame_states = "" if check is None else check.states
            self._shadow_decisions[verdict.decision] += 1
            if verdict.decision == "unclear":
                self._shadow_unclear_reasons[verdict.reason] += 1
        if verdict.decision == "still_there":
            outcome = f"would block: still_there ({verdict.reason})"
        elif verdict.decision == "departed":
            outcome = f"would pulse: departed at +{verdict.first_departure_ms_after_edge:.1f} ms"
        else:
            outcome = f"would pulse: unclear ({verdict.reason})"
        logger.info("[GATE] edge #%d camera shadow -> %s", record.sequence, outcome)

    def _camera_verdict(
        self, record: GateRecord, edge_ns: int, *, shadow: bool = False
    ) -> tuple[LiveVerdict | None, LiveDepartureCheck | None]:
        """Watch the live frames after the edge; None when the deadline settled first.

        A ``shadow`` run ignores the record's decision (passthrough settled it
        at the edge) and always decides.
        """
        camera = self._camera
        if camera is None or not camera.running:
            return LiveVerdict("unclear", "camera_off"), None
        anchor = camera.trigger_check_anchor()
        if anchor is None:
            return LiveVerdict("unclear", "no_anchor"), None
        check = LiveDepartureCheck(edge_ns, anchor)
        collect_until_ns = edge_ns + round((self.deadline_ms - DECISION_MARGIN_MS) * 1e6)
        after_ns = edge_ns - round(REFERENCE_LOOKBACK_MS * 1e6)
        while shadow or record.decision == "pending":
            timeout_s = max(0.0, (collect_until_ns - self._monotonic_ns()) / 1e9)
            frames = camera.live_frames_after(after_ns, timeout_s)
            if frames:
                after_ns = frames[-1].host_timestamp_ns
                verdict = check.add(frames)
                if verdict is not None:
                    return verdict, check
            elif not camera.running:
                return LiveVerdict("unclear", "camera_off"), check
            if self._monotonic_ns() >= collect_until_ns:
                return check.finish(), check
        return None, check

    def _report(self, record: GateRecord) -> None:
        """Log one edge, count it and notify the pulse listener (worker or timer thread)."""
        delay_ms = record.delay_ms
        if record.decision == "passthrough":
            outcome = f"pulse +{delay_ms:.1f} ms (passthrough)"
        elif record.decision == "departed":
            outcome = (
                f"pulse +{delay_ms:.1f} ms: departed at +{record.first_departure_ms:.1f} ms "
                f"({record.frames_used} frames)"
            )
        elif record.pulsed:
            outcome = f"pulse +{delay_ms:.1f} ms: unclear ({record.reason})"
        else:
            outcome = (
                f"blocked: still_there after {record.decision_latency_ms:.1f} ms "
                f"({record.reason}, {record.frames_used} frames"
                f"{', TI released' if record.ti_released else ''})"
            )
        logger.info("[GATE] edge #%d %.3f -> %s", record.sequence, record.edge_timestamp, outcome)
        budget_ms = LATE_PULSE_MS + (self.deadline_ms if self.mode == "camera" else 0.0)
        late = record.pulsed and delay_ms > budget_ms
        if late:
            logger.warning(
                "[GATE] Late pulse for edge #%d: +%.1f ms after the sound edge (budget %.0f ms)",
                record.sequence,
                delay_ms,
                budget_ms,
            )
        with self._lock:
            self._counts["edges"] += 1
            self._counts["pulsed" if record.pulsed else "blocked"] += 1
            self._counts[record.decision] += 1
            self._counts["late"] += int(late)
            if record.decision == "unclear":
                self._unclear_reasons[record.reason] += 1
            edges = self._counts["edges"]
        if record.pulsed and self._on_pulse is not None:
            try:
                self._on_pulse()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[GATE] Pulse listener failed", exc_info=True)
        if edges % SUMMARY_EVERY_EDGES == 0:
            logger.info("[GATE] Summary: %s", self.summary_text())

    def _report_ops_speed(self, record: GateRecord) -> None:
        """Log and count a speed trip or its claim (worker thread); a trip runs ``on_pulse``."""
        if record.edge_source == "ops-speed":
            logger.info(
                "[GATE] edge #%d %.3f -> OPS speed trip on pin 1, no pulse (club anchor +%.0f ms)",
                record.sequence,
                record.edge_timestamp,
                -record.anchor_lead_ms,
            )
            with self._lock:
                self._counts["edges"] += 1
                self._counts["ops_speed_trips"] += 1
                edges = self._counts["edges"]
            if self._on_pulse is not None:
                try:
                    self._on_pulse()
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("[GATE] Pulse listener failed", exc_info=True)
            if edges % SUMMARY_EVERY_EDGES == 0:
                logger.info("[GATE] Summary: %s", self.summary_text())
            return
        logger.info(
            "[GATE] edge #%d %.3f -> no pulse: sound edge %.1f ms after the OPS speed trip",
            record.sequence,
            record.edge_timestamp,
            -record.delay_ms,
        )
        with self._lock:
            self._counts["ops_speed_claimed"] += 1

    def summary(self) -> dict:
        """Per-session counters: edges, pulsed, blocked, late, per decision and unclear reason.

        ``ops_busy`` counts pulses lost to OPS blind time, ``ops_busy_phases``
        splits them by dump transfer / host processing after the dump / re-arm
        drain / re-arm commands. ``shadow`` counts the passthrough camera
        shadow's decisions and unclear reasons (empty without one).
        ``ops_speed`` (speed trigger only) counts the speed trips, the trips a
        sound edge claimed and the shadow's fallback pulses.
        """
        with self._lock:
            counts = dict(self._counts)
            reasons = dict(self._unclear_reasons)
            shadow = {
                "decisions": dict(self._shadow_decisions),
                "unclear_reasons": dict(self._shadow_unclear_reasons),
            }
            busy_phases = {
                phase: self._ops_busy_phases[phase]
                for phase in OPS_BUSY_PHASES
                if self._ops_busy_phases[phase]
            }
        decisions = {
            key: counts.get(key, 0)
            for key in ("passthrough", "departed", "unclear", "still_there")
            if counts.get(key)
        }
        summary = {
            "mode": self.mode,
            "edges": counts.get("edges", 0),
            "pulsed": counts.get("pulsed", 0),
            "blocked": counts.get("blocked", 0),
            "late": counts.get("late", 0),
            "decisions": decisions,
            "unclear_reasons": reasons,
            "ops_busy": counts.get("ops_busy", 0),
            "ops_busy_phases": busy_phases,
            "shadow": shadow,
        }
        if self.ops_speed_mode is not None:
            summary["ops_speed"] = {
                "mode": self.ops_speed_mode,
                "trips": counts.get("ops_speed_trips", 0),
                "claimed": counts.get("ops_speed_claimed", 0),
                "fallback": counts.get("ops_speed_fallback", 0),
                "host_int_echoes": counts.get("ops_speed_host_int_echoes", 0),
            }
        return summary

    def summary_text(self) -> str:
        """One-line form of ``summary``."""
        summary = self.summary()
        decisions = ", ".join(f"{key} {value}" for key, value in summary["decisions"].items())
        reasons = ", ".join(f"{key} {value}" for key, value in summary["unclear_reasons"].items())
        phases = ", ".join(f"{key} {value}" for key, value in summary["ops_busy_phases"].items())
        text = (
            f"{summary['edges']} edges, {summary['pulsed']} pulsed, {summary['blocked']} blocked, "
            f"{summary['late']} late ({decisions or 'none'}"
            f"{'; unclear: ' + reasons if reasons else ''}); "
            f"{summary['ops_busy']} in OPS busy time{' (' + phases + ')' if phases else ''}"
        )
        ops_speed = summary.get("ops_speed")
        if ops_speed is not None:
            text += (
                f"; OPS speed trips {ops_speed['trips']} ({ops_speed['claimed']} claimed by a "
                f"sound edge, {ops_speed['fallback']} fallback pulses)"
            )
        shadow = summary["shadow"]["decisions"]
        if not shadow:
            return text
        shadow_reasons = ", ".join(
            f"{key} {value}" for key, value in summary["shadow"]["unclear_reasons"].items()
        )
        return (
            f"{text}; camera shadow: would block {shadow.get('still_there', 0)} of "
            f"{sum(shadow.values())} (departed {shadow.get('departed', 0)}, unclear "
            f"{shadow.get('unclear', 0)}{': ' + shadow_reasons if shadow_reasons else ''})"
        )

    def set_ops_busy_listener(self, listener: Callable[[dict], None] | None) -> None:
        """Call ``listener(fields)`` for every pulse lost to OPS blind time."""
        self._ops_busy_listener = listener

    def note_ops_dump_started(
        self, first_byte_timestamp: float, post_trigger_ms: float | None
    ) -> None:
        """An OPS dump started arriving: the OPS is blind since the pulse behind it.

        A shadow fallback pulse that came after the dump's trigger point
        (``ops_speed_self_trip``) did not start it: the busy period then
        begins before that pulse, which counts as lost to it.
        """
        record = self.record_for_capture(first_byte_timestamp, post_trigger_ms)
        if record is not None and not record.ops_speed_self_trip:
            busy = _OpsBusy(record.pulse_timestamp, record.sequence, record.edge_timestamp)
        else:
            start = float(first_byte_timestamp) - float(post_trigger_ms or 0.0) / 1000.0
            if record is not None:
                # The trigger point lies at least the shortest HOST_INT lag
                # before that bound, so before the self trip's fallback pulse.
                start -= HOST_INT_MIN_FIRST_BYTE_LAG_MS / 1000.0
            busy = _OpsBusy(start)
        with self._lock:
            previous, self._ops_busy = self._ops_busy, busy
        if previous is not None:
            # A new dump while the last busy period was never closed (a re-arm
            # that failed, or one not reported): it ended when this one began.
            self._resolve_ops_busy(previous, busy.start_timestamp, None, "next_dump")

    def note_ops_dump_finished(self, timestamp: float | None = None) -> None:
        """The dump's last byte was read: later pulses fall in host ``processing``.

        The OPS stays blind until its re-arm (parsing, validation and, for an
        accepted shot, the clock sync run first), but a faster UART only
        shortens the ``dump`` phase.
        """
        timestamp = self._wall_time() if timestamp is None else float(timestamp)
        with self._lock:
            busy = self._ops_busy
            if (
                busy is not None
                and busy.started_by == "dump"
                and busy.rearm_start_timestamp is None
                and busy.dump_end_timestamp is None
            ):
                busy.dump_end_timestamp = timestamp

    def note_ops_rearm_started(self, timestamp: float | None = None) -> None:
        """The OPS re-arm (drain, then PA / S# / PA) starts; opens a busy period if none is."""
        timestamp = self._wall_time() if timestamp is None else float(timestamp)
        with self._lock:
            if self._ops_busy is None:
                self._ops_busy = _OpsBusy(
                    timestamp, rearm_start_timestamp=timestamp, started_by="rearm"
                )
            elif self._ops_busy.rearm_start_timestamp is None:
                self._ops_busy.rearm_start_timestamp = timestamp

    def note_ops_armed(
        self,
        armed_timestamp: float | None = None,
        drain_end_timestamp: float | None = None,
        ended_by: str = "armed",
    ) -> None:
        """The final re-arm PA was written: close the busy period and report its lost pulses."""
        armed_timestamp = self._wall_time() if armed_timestamp is None else float(armed_timestamp)
        with self._lock:
            busy, self._ops_busy = self._ops_busy, None
            # Pulses during the re-arm commands started no dump at the range
            # (2026-10-25: 2 of 2), so the radar counts as armed from here.
            self._armed_floors.append(armed_timestamp)
        if busy is not None:
            self._resolve_ops_busy(busy, armed_timestamp, drain_end_timestamp, ended_by)

    def _resolve_ops_busy(
        self,
        busy: _OpsBusy,
        end_timestamp: float | None,
        drain_end_timestamp: float | None,
        ended_by: str,
    ) -> None:
        """Log, count and report every pulse that fell inside ``busy``."""
        with self._lock:
            lost = [
                record
                for record in self._records
                if record.pulse_timestamp is not None
                and record.sequence != busy.start_sequence
                and record.pulse_timestamp > busy.start_timestamp
                and (end_timestamp is None or record.pulse_timestamp < end_timestamp)
            ]
        for record in lost:
            phase = busy.phase_at(record.pulse_timestamp, drain_end_timestamp)
            ms_left = (
                None if end_timestamp is None else (end_timestamp - record.pulse_timestamp) * 1000.0
            )
            fields = {
                **record.log_fields(),
                "gate_edge_sequence": record.sequence,
                "ops_busy_phase": phase,
                "ops_busy_start_edge_sequence": busy.start_sequence,
                "ops_busy_started_by": busy.started_by,
                "ops_busy_dump_end_timestamp": busy.dump_end_timestamp,
                "ops_busy_start_edge_timestamp": busy.start_edge_timestamp,
                "ops_busy_start_timestamp": busy.start_timestamp,
                "ops_busy_elapsed_ms": (record.pulse_timestamp - busy.start_timestamp) * 1000.0,
                "ops_busy_ms_left": ms_left,
                "ops_busy_end_timestamp": end_timestamp,
                "ops_busy_ended_by": ended_by,
            }
            logger.info(
                "[GATE] edge #%d %.3f lost to OPS busy time (%s, busy since %s, "
                "+%.0f ms in, %s left, ended by %s)",
                record.sequence,
                record.edge_timestamp,
                phase,
                _busy_origin(busy),
                fields["ops_busy_elapsed_ms"],
                "unknown" if ms_left is None else f"{ms_left:.0f} ms",
                ended_by,
            )
            with self._lock:
                self._counts["ops_busy"] += 1
                self._ops_busy_phases[phase] += 1
            listener = self._ops_busy_listener
            if listener is not None:
                try:
                    listener(fields)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning("[GATE] OPS-busy listener failed", exc_info=True)

    def fields_for_dump(
        self, first_byte_timestamp: float | None, post_trigger_ms: float | None
    ) -> dict:
        """Log fields of the pulse behind an OPS dump; ``unmatched`` when none fits.

        A shadow self trip (``GateRecord.ops_speed_self_trip``) is ``unmatched``
        too, with ``gate_first_byte_match`` ``self_trip`` and the fallback pulse
        it matched as the nearest pulse.
        """
        if first_byte_timestamp is None:
            return {}
        record = self.record_for_capture(first_byte_timestamp, post_trigger_ms)
        if record is None or record.ops_speed_self_trip:
            fields = {
                "gate_mode": self.mode,
                "gate_decision": "unmatched",
                "sound_edge_delay_ms": 0.0,
                "gate_first_byte_match": "none",
                **self._nearest_pulse_fields(first_byte_timestamp, post_trigger_ms),
            }
            if self.ops_speed_mode is not None:
                # The OPS may have tripped itself: anchor after its buffer trigger point.
                fields["sound_edge_delay_ms"] = -self.ops_speed_anchor_ms
                fields["gate_ops_speed_mode"] = self.ops_speed_mode
            if record is not None:
                fields.update(
                    gate_first_byte_match="self_trip",
                    gate_nearest_edge_sequence=record.sequence,
                    gate_nearest_edge_timestamp=record.edge_timestamp,
                    gate_nearest_first_byte_lag_ms=record.first_byte_lag_ms,
                )
            return fields
        if record.first_byte_match == "late":
            logger.info(
                "[GATE] OPS dump (first byte %.3f) matched edge #%d late: first byte "
                "%.0f ms after the pulse's usual window (dump queued while the host was busy)",
                first_byte_timestamp,
                record.sequence,
                record.first_byte_lag_ms - FIRST_BYTE_MATCH_MS[1],
            )
        fields = record.log_fields()
        if self._edge_timing is not None:
            fields.update(self._edge_timing.fields_for(record.edge_timestamp))
        return fields

    def apply_to_capture(self, capture) -> dict:
        """Set the capture's ``sound_edge_delay_ms`` from its pulse; return the log fields.

        An unmatched capture keeps delay 0: club speed and impact then anchor
        to HOST_INT, as without the gate. Under the speed trigger it anchors
        ``ops_speed_anchor_ms`` after the buffer's trigger point instead.
        """
        fields = self.fields_for_dump(
            capture.first_byte_timestamp, capture.post_trigger_duration_ms
        )
        if not fields:
            return {}
        capture.sound_edge_delay_ms = fields["sound_edge_delay_ms"]
        if fields["gate_first_byte_match"] == "self_trip":
            logger.warning(
                "[GATE] OPS dump (first byte %.3f) lags fallback pulse #%d by %.1f ms, less "
                "than any HOST_INT dump (%g ms): the OPS tripped itself and pin 1 was not "
                "seen; club speed and impact anchor %g ms after its trigger point",
                capture.first_byte_timestamp,
                fields["gate_nearest_edge_sequence"],
                fields["gate_nearest_first_byte_lag_ms"],
                HOST_INT_MIN_FIRST_BYTE_LAG_MS,
                self.ops_speed_anchor_ms,
            )
        elif fields["gate_decision"] == "unmatched" and self.ops_speed_mode is not None:
            logger.warning(
                "[GATE] OPS dump (first byte %.3f) matches no gate record; club speed and "
                "impact anchor %g ms after its trigger point",
                capture.first_byte_timestamp,
                self.ops_speed_anchor_ms,
            )
        elif fields["gate_decision"] == "unmatched":
            logger.warning(
                "[GATE] OPS dump (first byte %.3f) matches no gate pulse; "
                "club speed and impact stay anchored to HOST_INT",
                capture.first_byte_timestamp,
            )
        return {**fields, "sound_edge_offset_ms": capture.sound_edge_offset_ms}

    def record_for_capture(
        self, first_byte_timestamp: float | None, post_trigger_ms: float | None
    ) -> GateRecord | None:
        """The pulse that started an OPS dump, found from the dump's first byte.

        The first byte arrives the post-trigger span plus ``FIRST_BYTE_LAG_MS``
        (observed 4-30 ms, down to 1.15 ms on 2026-11-02, see
        ``HOST_INT_MIN_FIRST_BYTE_LAG_MS``) after HOST_INT. Pulses at least
        10 ms after that bound or more than 60 ms before it match as ``window``.

        Without such a pulse the dump may have been queued while the host was
        still busy and read late: the pulse up to ``LATE_FIRST_BYTE_MATCH_MS``
        before the bound matches as ``late``. Once a re-arm was reported
        (``note_ops_armed``) only pulses after the last re-arm before the first
        byte count, and the earliest wins (the radar dumps on its first pulse);
        without one the last pulse before the first byte wins.
        """
        if first_byte_timestamp is None or post_trigger_ms is None:
            return None
        first_byte = float(first_byte_timestamp)
        bound = first_byte - float(post_trigger_ms) / 1000.0
        with self._lock:
            lags = [
                ((bound - record.pulse_timestamp) * 1000.0, record)
                for record in self._records
                if record.pulse_timestamp is not None
            ]
            matches = [
                (lag, record)
                for lag, record in lags
                if FIRST_BYTE_MATCH_MS[0] <= lag <= FIRST_BYTE_MATCH_MS[1]
            ]
            if matches:
                lag, record = min(matches, key=lambda item: abs(item[0] - FIRST_BYTE_LAG_MS))
                return replace(record, first_byte_lag_ms=lag, first_byte_match="window")
            floors = [floor for floor in self._armed_floors if floor <= first_byte]
            floor = max(floors) if floors else None
            late = [
                (lag, record)
                for lag, record in lags
                if FIRST_BYTE_MATCH_MS[1] < lag <= LATE_FIRST_BYTE_MATCH_MS
                and (floor is None or record.pulse_timestamp >= floor)
            ]
            if not late:
                return None
            pick = max if floor is not None else min
            lag, record = pick(late, key=lambda item: item[0])
            return replace(record, first_byte_lag_ms=lag, first_byte_match="late")

    def _nearest_pulse_fields(
        self, first_byte_timestamp: float, post_trigger_ms: float | None
    ) -> dict:
        """Context for an unmatched dump: the last pulse before its first byte, if any."""
        bound = float(first_byte_timestamp) - float(post_trigger_ms or 0.0) / 1000.0
        with self._lock:
            before = [
                record
                for record in self._records
                if record.pulse_timestamp is not None and record.pulse_timestamp <= bound
            ]
        if not before:
            return {"gate_nearest_edge_sequence": None}
        record = max(before, key=lambda item: item.pulse_timestamp)
        return {
            "gate_nearest_edge_sequence": record.sequence,
            "gate_nearest_edge_timestamp": record.edge_timestamp,
            "gate_nearest_first_byte_lag_ms": (bound - record.pulse_timestamp) * 1000.0,
        }


class EdgeListener:
    """Owns the sound-gate input when no TI monitor does and fans its edges out.

    Applies the TI monitor's ringing filter so observers see the same edges
    either way. Edges count only after ``arm``, once the OPS path runs. Each
    fanned-out edge is noted in ``edge_timing`` before the observers and
    resolved after they ran.
    """

    RING_FILTER_S = IWR6843CaptureMonitor.RING_FILTER_S

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        pin: int,
        observers: list[Callable[[float], None]],
        button_factory: Callable | None = None,
        wall_time: Callable[[], float] = time.time,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        edge_timing: EdgeTimingLog | None = None,
    ):
        self.pin = pin
        self._observers = list(observers)
        self._button_factory = button_factory
        self._wall_time = wall_time
        self._monotonic_ns = monotonic_ns
        self.edge_timing = edge_timing if edge_timing is not None else EdgeTimingLog()
        self._button = None
        self._armed = False
        self._last_edge = -math.inf
        self._lock = threading.Lock()

    def start(self) -> None:
        """Claim the input pin (pulled down, no debounce)."""
        factory = self._button_factory
        if factory is None:
            ensure_lgpio_pin_factory()
            from gpiozero import Button  # pylint: disable=import-error,import-outside-toplevel

            factory = Button
        self._button = factory(self.pin, pull_up=False, bounce_time=None)
        logger.info("[GATE] Listening for sound edges on BCM%d", self.pin)

    def arm(self) -> None:
        """Start accepting edges."""
        if self._button is None:
            raise RuntimeError("cannot arm an edge listener that is not started")
        self._button.when_pressed = self.notify_edge
        self._armed = True

    def notify_edge(self, timestamp: float | None = None) -> bool:
        """Fan one edge out unless it is acoustic ringing; True when fanned out."""
        if not self._armed:
            return False
        callback_wall = self._wall_time()
        callback_monotonic_ns = self._monotonic_ns()
        ticks = edge_ticks(self._button)
        edge = callback_wall if timestamp is None else float(timestamp)
        with self._lock:
            if edge - self._last_edge < self.RING_FILTER_S:
                return False
            self._last_edge = edge
        # Shadow timing only: a bare deque append before the observers, so a
        # session-log writer finds the edge even while an observer blocks.
        self.edge_timing.note(
            edge,
            callback_timestamp=callback_wall,
            callback_monotonic_ns=callback_monotonic_ns,
            ticks=ticks,
        )
        for observer in self._observers:
            try:
                observer(edge)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[GATE] Edge observer failed", exc_info=True)
        # The clock resolution waits until the gate has pulsed.
        self.edge_timing.flush()
        return True

    def stop(self) -> None:
        """Release the input pin."""
        self._armed = False
        if self._button is not None:
            self._button.when_pressed = None
            self._button.close()
            self._button = None
            self.edge_timing.log_summary(f"BCM{self.pin} (trigger gate input)")


__all__ = [
    "DEFAULT_DEADLINE_MS",
    "DEFAULT_TI_PRE_TRIGGER_SEGMENTS",
    "EDGE_SOURCES",
    "LATE_FIRST_BYTE_MATCH_MS",
    "DEFAULT_PULSE_MS",
    "EdgeListener",
    "GATE_MODES",
    "GateRecord",
    "TriggerGate",
    "camera_split_check",
    "ops_speed_split_check",
    "post_trigger_span_ms",
    "pre_trigger_ms",
    "recommended_camera_split",
    "ti_split_check",
]
