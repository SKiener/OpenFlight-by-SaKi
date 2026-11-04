"""Kernel time of each sound-trigger edge, logged next to the callback stamps (shadow).

Every consumer of the BCM17 sound edge (trigger gate, camera ring, OPS stall
watch, TI dump) takes its time from a Python stamp read inside the gpiozero
callback. That stamp lags the electrical edge by the dispatch latency: lgpio's
alert thread, the GIL and whatever the callback waits for first. This module
records, per edge, the kernel line-event time of the same edge next to the
callback stamps, so the latency can be measured. Nothing consumes the kernel
time yet: every timestamp handed to an observer is unchanged.

Where the kernel stamp comes from
---------------------------------
gpiozero's lgpio pin hands lgpio's alert timestamp (the kernel line event's
``timestamp_ns``) to the device as ``ticks``, and ``EventsMixin._fire_events``
stores it in ``device._last_changed`` before it fires ``when_activated``
(``when_pressed``). Read inside that callback, the attribute holds the ticks of
the edge being dispatched. ``edge_ticks`` is the only place that knows this;
a device without the attribute (mock mode, test doubles) gives ``None``.

Clock-domain rule
-----------------
The kernel stamps line events on CLOCK_MONOTONIC unless the line asks for the
realtime clock, and gpiozero converts lgpio's nanoseconds to seconds, so
neither the clock nor the unit is assumed. The raw ticks are read as s, ms, us
and ns against both the callback's ``time.time()`` (realtime) and its
``time.monotonic_ns()`` (monotonic). To decide, a reading fits when the
callback comes ``-NEGATIVE_SLACK_S`` to ``MAX_LATENCY_S`` after it; the fit
with the smallest latency wins (the two clocks differ by decades, so both
cannot fit). The first decision is logged once and reused. Once decided, an
edge on that clock is accepted with any latency up to ``MAX_DECIDED_LATENCY_S``,
so a stalled callback thread shows up with its real latency instead of being
dropped; only an edge outside that bound re-derives the decision (a real
clock or unit change lands decades away or negative). Either way the kernel
epoch is ``callback wall - latency``.

Ticks that fit nothing, or that repeat the previous edge's ticks (the device
did not update them), give null kernel fields; the callback fields remain.

Two phases per edge
-------------------
``note`` runs in the callback before the observers and only appends the raw
stamps to a deque (atomic, well under a microsecond), so the gate pulse waits
for nothing new. ``flush`` resolves the clock after the observers ran;
``fields_for`` and ``summary`` flush too, so a session-log writer finds the
edge even while a later observer still blocks the callback thread.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_LATENCY_S = 1.0
MAX_DECIDED_LATENCY_S = 60.0
NEGATIVE_SLACK_S = 0.001
TICK_UNITS = (("s", 1.0), ("ms", 1e-3), ("us", 1e-6), ("ns", 1e-9))
CLOCKS = ("realtime", "monotonic")
RECORD_HISTORY = 64
MATCH_TOLERANCE_S = 1e-6

FIELD_NAMES = (
    "edge_kernel_timestamp",
    "edge_kernel_clock",
    "edge_callback_timestamp",
    "edge_callback_monotonic_ns",
    "edge_callback_latency_ms",
)


def edge_ticks(device) -> float | None:
    """Raw ticks of the edge gpiozero is dispatching to ``device``'s callback, if any.

    Only meaningful when read inside ``when_pressed``; see the module docstring.
    """
    if device is None:
        return None
    ticks = getattr(device, "_last_changed", None)
    if isinstance(ticks, bool) or not isinstance(ticks, (int, float)):
        return None
    if isinstance(ticks, float) and not math.isfinite(ticks):
        return None
    return ticks


def null_fields() -> dict:
    """Edge-timing fields for an edge nothing is known about (mock mode, no match)."""
    return dict.fromkeys(FIELD_NAMES)


@dataclass(frozen=True)
class EdgeTiming:
    """One edge: the stamps read in the callback and, when known, the kernel time."""

    edge_timestamp: float
    callback_timestamp: float
    callback_monotonic_ns: int
    kernel_timestamp: float | None = None
    kernel_clock: str | None = None
    latency_ms: float | None = None

    def log_fields(self) -> dict:
        """Fields merged into session-log records next to the existing trigger data."""
        return {
            "edge_kernel_timestamp": self.kernel_timestamp,
            "edge_kernel_clock": self.kernel_clock,
            "edge_callback_timestamp": self.callback_timestamp,
            "edge_callback_monotonic_ns": self.callback_monotonic_ns,
            "edge_callback_latency_ms": self.latency_ms,
        }


def _latency_s(ticks: float, scale: float, clock: str, wall_s: float, monotonic_ns: int) -> float:
    reference_s = wall_s if clock == "realtime" else monotonic_ns / 1e9
    return reference_s - ticks * scale


def _fits(latency_s: float, max_latency_s: float = MAX_LATENCY_S) -> bool:
    return -NEGATIVE_SLACK_S <= latency_s <= max_latency_s


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of an ascending, non-empty list."""
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


class EdgeTimingLog:  # pylint: disable=too-many-instance-attributes
    """Per-session record of sound-edge timing, shared by the edge source and the loggers.

    ``note`` runs on the GPIO callback thread before the observers and only
    appends the raw stamps. ``flush`` (after the observers), ``fields_for`` and
    ``summary`` resolve pending edges in arrival order: plain arithmetic under a
    short lock, no I/O apart from the one-time messages, which are logged after
    the lock is released. ``fields_for`` serves the session-log writers on
    other threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[tuple[float, float, int, float | None]] = deque()
        self._records: deque[EdgeTiming] = deque(maxlen=RECORD_HISTORY)
        # 1-based arrival number of each kept record (``history``), in step with ``_records``.
        self._sequences: deque[int] = deque(maxlen=RECORD_HISTORY)
        self._latencies_ms: list[float] = []
        self._edges = 0
        self._missing = 0
        self._rejected = 0
        self._decision: tuple[str, str, float] | None = None
        self._last_ticks: float | None = None
        self._missing_reported = False
        self._stale_reported = False
        self._nofit_reported = False

    @property
    def clock_decision(self) -> tuple[str, str] | None:
        """``(clock, unit)`` the kernel ticks were found on, once decided."""
        self.flush()
        with self._lock:
            return None if self._decision is None else self._decision[:2]

    def note(
        self,
        edge_timestamp: float,
        *,
        callback_timestamp: float,
        callback_monotonic_ns: int,
        ticks: float | None,
    ) -> None:
        """Queue one fanned-out edge's raw stamps; cheap enough for before the observers.

        ``ticks`` as ``edge_ticks`` read them in the callback. No lock: a
        deque append is atomic, and ``flush`` drains under the lock.
        """
        self._pending.append((edge_timestamp, callback_timestamp, callback_monotonic_ns, ticks))

    def flush(self) -> None:
        """Resolve every noted edge (clock decision, latency) in arrival order."""
        if not self._pending:
            return
        with self._lock:
            messages = self._drain_locked()
        for level, text, args in messages:
            logger.log(level, text, *args)

    def record(
        self,
        edge_timestamp: float,
        *,
        callback_timestamp: float,
        callback_monotonic_ns: int,
        ticks: float | None,
    ) -> EdgeTiming:
        """``note`` and ``flush`` one edge at once; returns its resolved timing."""
        self.note(
            edge_timestamp,
            callback_timestamp=callback_timestamp,
            callback_monotonic_ns=callback_monotonic_ns,
            ticks=ticks,
        )
        self.flush()
        with self._lock:
            timing = self._find_locked(float(edge_timestamp))
        if timing is None:  # pragma: no cover - only if RECORD_HISTORY edges arrived meanwhile
            return EdgeTiming(
                float(edge_timestamp), float(callback_timestamp), int(callback_monotonic_ns)
            )
        return timing

    def _drain_locked(self) -> list[tuple]:
        messages = []
        while self._pending:
            edge_timestamp, wall_s, monotonic_ns, ticks = self._pending.popleft()
            self._edges += 1
            timing, message = self._resolve_locked(
                float(edge_timestamp), float(wall_s), int(monotonic_ns), ticks
            )
            self._records.append(timing)
            self._sequences.append(self._edges)
            if timing.latency_ms is not None:
                self._latencies_ms.append(timing.latency_ms)
            if message is not None:
                messages.append(message)
        return messages

    def _find_locked(self, edge_timestamp: float) -> EdgeTiming | None:
        for timing in reversed(self._records):
            if abs(timing.edge_timestamp - edge_timestamp) <= MATCH_TOLERANCE_S:
                return timing
        return None

    def _resolve_locked(
        self, edge_timestamp: float, wall_s: float, monotonic_ns: int, ticks: float | None
    ) -> tuple[EdgeTiming, tuple | None]:
        bare = EdgeTiming(edge_timestamp, wall_s, monotonic_ns)
        if ticks is None:
            self._missing += 1
            if self._missing_reported:
                return bare, None
            self._missing_reported = True
            return bare, (
                logging.INFO,
                "[EDGE] No kernel edge stamp from this input (mock or non-lgpio pin); "
                "kernel edge fields stay null",
                (),
            )
        stale = ticks == self._last_ticks
        self._last_ticks = ticks
        decision = None if stale else self._decide_locked(ticks, wall_s, monotonic_ns)
        if decision is None:
            return bare, self._reject_locked(ticks, stale)
        (clock, _unit, scale), message = decision
        latency_s = _latency_s(ticks, scale, clock, wall_s, monotonic_ns)
        timing = EdgeTiming(
            edge_timestamp,
            wall_s,
            monotonic_ns,
            kernel_timestamp=wall_s - latency_s,
            kernel_clock=clock,
            latency_ms=latency_s * 1000.0,
        )
        return timing, message

    def _reject_locked(self, ticks: float, stale: bool) -> tuple | None:
        """Count a rejected edge; the first of each kind (stale, no fit) gets a WARNING."""
        self._rejected += 1
        if stale:
            if self._stale_reported:
                return None
            self._stale_reported = True
            why = "repeat the previous edge"
        else:
            if self._nofit_reported:
                return None
            self._nofit_reported = True
            if self._decision is None:
                why = "fit neither clock with a callback 0-1 s after the edge"
            else:
                why = (
                    f"fit neither CLOCK_{self._decision[0].upper()}/{self._decision[1]} "
                    f"within {MAX_DECIDED_LATENCY_S:.0f} s nor any clock within 1 s"
                )
        return (
            logging.WARNING,
            "[EDGE] Kernel ticks %r %s; kernel edge time not logged for such edges",
            (ticks, why),
        )

    def _decide_locked(
        self, ticks: float, wall_s: float, monotonic_ns: int
    ) -> tuple[tuple[str, str, float], tuple | None] | None:
        current = self._decision
        if current is not None and _fits(
            _latency_s(ticks, current[2], current[0], wall_s, monotonic_ns),
            MAX_DECIDED_LATENCY_S,
        ):
            return current, None
        fits = []
        for clock in CLOCKS:
            for unit, scale in TICK_UNITS:
                latency_s = _latency_s(ticks, scale, clock, wall_s, monotonic_ns)
                if _fits(latency_s):
                    fits.append((abs(latency_s), (clock, unit, scale), latency_s))
        if not fits:
            return None
        _, chosen, latency_s = min(fits, key=lambda item: item[0])
        self._decision = chosen
        if current is None:
            message = (
                logging.INFO,
                "[EDGE] Kernel edge stamps: ticks in %s on CLOCK_%s "
                "(first callback %.3f ms after the edge)",
                (chosen[1], chosen[0].upper(), latency_s * 1000.0),
            )
        else:
            message = (
                logging.WARNING,
                "[EDGE] Kernel edge ticks moved from %s/%s to %s/%s",
                (current[0], current[1], chosen[0], chosen[1]),
            )
        return chosen, message

    def fields_for(self, edge_timestamp: float | None) -> dict:
        """Edge-timing fields of the edge handed to observers as ``edge_timestamp``."""
        if edge_timestamp is None:
            return null_fields()
        self.flush()
        with self._lock:
            timing = self._find_locked(float(edge_timestamp))
        return null_fields() if timing is None else timing.log_fields()

    def history(self) -> list[tuple[int, EdgeTiming]]:
        """The kept edges (up to ``RECORD_HISTORY``) as ``(sequence, timing)``, oldest first.

        ``sequence`` counts every edge noted since this log was created, from 1.
        Read by the phone-onset matcher (``phone_events.py``); log-only.
        """
        self.flush()
        with self._lock:
            return list(zip(self._sequences, self._records))

    def summary(self) -> dict:
        """Edges seen, how many carry a kernel time, and their callback latency in ms."""
        self.flush()
        with self._lock:
            latencies = sorted(self._latencies_ms)
            decision = self._decision
            summary = {
                "edges": self._edges,
                "with_kernel": len(latencies),
                "missing": self._missing,
                "rejected": self._rejected,
                "clock": None if decision is None else decision[0],
                "unit": None if decision is None else decision[1],
            }
        if latencies:
            summary.update(
                p50_ms=_percentile(latencies, 0.5),
                p95_ms=_percentile(latencies, 0.95),
                max_ms=latencies[-1],
            )
        else:
            summary.update(p50_ms=None, p95_ms=None, max_ms=None)
        return summary

    def summary_text(self) -> str:
        """One-line form of ``summary``."""
        summary = self.summary()
        text = (
            f"{summary['edges']} edges, {summary['with_kernel']} with kernel time "
            f"({summary['missing']} without stamp, {summary['rejected']} rejected)"
        )
        if summary["with_kernel"]:
            text += (
                f"; callback latency n={summary['with_kernel']} p50 {summary['p50_ms']:.3f} ms, "
                f"p95 {summary['p95_ms']:.3f} ms, max {summary['max_ms']:.3f} ms "
                f"(CLOCK_{summary['clock'].upper()}, ticks in {summary['unit']})"
            )
        return text

    def log_summary(self, source: str) -> None:
        """Log the session's edge-latency summary at INFO (called when the source stops)."""
        logger.info("[EDGE] %s session summary: %s", source, self.summary_text())


__all__ = [
    "EdgeTiming",
    "EdgeTimingLog",
    "FIELD_NAMES",
    "edge_ticks",
    "null_fields",
]
