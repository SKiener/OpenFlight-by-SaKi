"""Host side of the IWR6843 firmware self-trigger (upstream ``feat/iwr-self-trigger``).

Firmware v1.0.3+ can freeze its own L3 ring when its club track reaches the
tee (``triggerCfg <bin> <snr> 1``). It reports the fire only as the CLI line
``Triggered`` on the same UART as the dump; there is no GPIO output. After a
self-fire the board stays latched until ``l3dump``, ``l3release``, ``l3sparse``
or ``l3track`` takes the freeze. Firmware built from ``cb8fbff`` or later routes
``l3dump`` through ``l3_awaitFrozenRing``: it streams the ring the fire froze
and requests no second freeze. Older images (v1.0.3 as released) request a
second freeze and leave the latch set; this host needs the fixed image.

Three shot-trigger modes (``--shot-trigger``):

* ``sound`` (default): the microphone edge on BCM17 drives OPS, camera and
  ``l3dump`` exactly as before; ``triggerCfg`` is never sent.
* ``ti``: no microphone. Each ``Triggered`` notice is the shot edge: the trigger
  gate pulses OPS HOST_INT, the camera ring freezes, and ``l3dump`` reads the
  ring the board froze itself.
* ``ti-shadow``: the microphone drives everything as in ``sound``; the board's
  self-trigger runs alongside and every notice is paired with the microphone
  edges for the session log (``ti_self_trigger`` entries).

Geometry: ``--iwr6843-tee-m`` stays the LCMF's antenna-to-ball slant range;
nothing in the LCMF changes. The trigger bin, however, follows the convention
the firmware evidence was replayed at (analysis iwr-self-trigger-20261031
``variant_sweep.py``/``replay_corpus.py``: "tape ball bin 33 (1.5 m + 30 mm)",
the same as upstream's front tape plus ``ARRAY_DEPTH_M``): ball bin =
``round((tee_m + 0.030) / 0.046875)``, so the replayed fire rates and latencies
apply to the bin the board watches. The radar's range coordinate is biased
anyway: an independent study (analysis ti-selbstneigung-20261031) sees the
resting ball at bins 34-35 (1.59-1.63 m) for the owner's 1.50 m tape, bin 33
by this convention. For 1.50 m the host sends ``triggerCfg 31 1 1`` (offset
0) or ``triggerCfg 35 1 1`` (+0.2 m).
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

TRIGGER_NOTICE = b"Triggered"
SHOT_TRIGGERS = ("sound", "ti", "ti-shadow")
SELF_TRIGGER_MODES = ("ti", "ti-shadow")

# Every cfg keeps a 6 m span over the range FFT (tracking.RANGE_SPAN_M).
RANGE_SPAN_M = 6.0
# Upstream calibration.ARRAY_DEPTH_M: enclosure front to antenna array. The
# trigger bin adds it to --iwr6843-tee-m, as the firmware replays did.
UPSTREAM_ARRAY_DEPTH_M = 0.030
# Upstream monitor.SELF_TRIGGER_TEE_LEAD_BINS: the watched bin sits this many
# bins short of the ball before the downrange offset.
SELF_TRIGGER_TEE_LEAD_BINS = 2
# Offset 0 ("lead2": the watched bin 2 bins short of the ball). In the replay of
# the fork's image openflight_iwr6843_v1.0.3-of1.bin (v1.0.3 + cb8fbff +
# candidate club-track acquisition) lead2 fired in the window on 44/46 drivers
# and 27/34 TrackMan swings; upstream's kiosk value +0.2 m ("off+4", 8f9667d7)
# on 41/46 and only 6/34 (many 3 frames late). Upstream moved its rig to +0.2 m
# because backswings and waggles fired at lead2; the range shadow test decides,
# and +0.2 m with the off+4 latencies (docs/reference/cli.md) is the fallback.
DEFAULT_SELF_TRIGGER_OFFSET_M = 0.0
MAX_SELF_TRIGGER_OFFSET_M = 1.0
# Upstream FIRMWARE_TRIGGER_DEFAULT_SNR; the firmware refuses snr < 1.
DEFAULT_SELF_TRIGGER_SNR = 1.0
SELF_TRIGGER_OFF_COMMAND = "triggerCfg 0 0 0"

# Board fire minus impact in ms per firing rule, the ~3 ms board decision
# included: medians of analysis/iwr-self-trigger-20261031/data/variants/
# E6_acq_conf02_step125.csv (the of1 image's replay), corpus owner, ball >= 60
# mph, shift lead2: crossing -1.70 (n 61), end +1.57 (n 36), leave -0.48
# (n 18). ``unknown`` (no readable ``triggerLog track``) is the pooled median,
# -0.57 (n 115). ``worst`` is the slowest fire seen there (leave, +9.52 ms; p90
# of leave +8.8): it only sizes the OPS pre-trigger span (``max_lead_ms``),
# never the anchor. scripts/analysis/iwr6843_fire_latency_defaults.py
# reproduces these values and the off+4 fallback set (offset +0.2 m).
DEFAULT_FIRE_LATENCY_MS = {
    "crossing": -1.70,
    "end": 1.57,
    "leave": -0.48,
    "unknown": -0.57,
    "worst": 9.52,
}
# The same medians for offset +0.2 m ("off+4"), for --ti-fire-latency-ms with
# --iwr6843-self-trigger-offset-m 0.2.
OFFSET_02_FIRE_LATENCY_MS = {
    "crossing": 2.88,
    "end": 3.90,
    "leave": 1.19,
    "unknown": 2.91,
    "worst": 18.45,
}
FIRE_CAUSES = ("crossing", "end", "leave")
# Fire to the host reading the notice: 10 bytes at 1.04 MBd, the notice task,
# CP2105/USB and the read (teil3: about 1 ms).
NOTICE_LATENCY_MS = 1.0
# The microphone edge lies this long after impact (teil3: 4.4 ms of sound
# travel); the OPS club-speed anchor constants are tuned to that edge.
SOUND_EDGE_AFTER_IMPACT_MS = 4.4

# Pairing window for ti-shadow, delta = notice - microphone edge.
DEFAULT_SHADOW_WINDOW_MS = (-150.0, 100.0)
# A notice whose fire froze the ring holds 9 pre / 15 post 3 ms frames, so a
# microphone edge up to this long after the notice still finds the impact in
# that ring (fire + 45 ms - 4.4 ms sound, rounded down). Later edges get a
# fresh sound freeze; the latched ring is released first.
SHADOW_CONSUME_AFTER_NOTICE_S = 0.040
SHADOW_HOLDOFF_MAX_MS = 40.0

_RANGE_LINE = re.compile(r"range impact fired=(\d+)\s+why=\S+\s+cause=(\w+)")
_LEAVE_LINE = re.compile(r"leave fired=(\d+)")


@dataclass(frozen=True)
class TriggerNotice:
    """One ``Triggered`` line, stamped when the host read it.

    ``consumed``: seen inside the reply of a command that takes a latched
    freeze (``l3dump``/``l3release``/``sensorStop``) before its verdict, so the
    board is no longer latched for it. ``in_reply``: seen while a command was
    running, so its stamp is late by up to that exchange.
    """

    monotonic_ns: int
    wall_time: float
    in_reply: bool = False
    consumed: bool = False


@dataclass(frozen=True)
class SelfTriggerConfig:
    """Firmware ``triggerCfg``: freeze when the club track reaches ``tee_bin``."""

    tee_bin: int
    snr: float = DEFAULT_SELF_TRIGGER_SNR

    def __post_init__(self) -> None:
        if self.tee_bin < 0:
            raise ValueError(f"self-trigger bin must be >= 0, got {self.tee_bin}")
        if not math.isfinite(self.snr) or self.snr < 1.0:
            raise ValueError(f"self-trigger snr must be >= 1, got {self.snr}")

    @property
    def command(self) -> str:
        """CLI line that arms this trigger: ``<bin> <snr> <on>``."""
        return f"triggerCfg {self.tee_bin} {self.snr:g} 1"


def bin_size_m(fft_size: int = 128) -> float:
    """Range per FFT bin."""
    return RANGE_SPAN_M / fft_size


def _first_window(config_path: str | Path) -> tuple[int, int]:
    """(start bin, bin count) of the cfg's first ``phaseCaptureCfg`` window."""
    with Path(config_path).open(encoding="utf-8") as handle:
        for raw_line in handle:
            fields = raw_line.split()
            if fields and fields[0] == "phaseCaptureCfg" and len(fields) >= 3:
                return int(fields[1]), int(fields[2])
    raise ValueError(f"{config_path} has no phaseCaptureCfg")


def trigger_ball_bin(tee_m: float, fft_size: int = 128) -> int:
    """The ball's bin as the firmware replays placed it: tape plus 30 mm array depth.

    ``tee_m`` is ``--iwr6843-tee-m``; see the module docstring for why the
    trigger uses this convention rather than the LCMF slant semantics.
    """
    if not math.isfinite(tee_m) or tee_m <= 0:
        raise ValueError(f"tee distance must be positive, got {tee_m}")
    return int(round((tee_m + UPSTREAM_ARRAY_DEPTH_M) / bin_size_m(fft_size)))


def self_trigger_bin(
    tee_m: float,
    config_path: str | Path,
    *,
    offset_m: float = DEFAULT_SELF_TRIGGER_OFFSET_M,
    fft_size: int = 128,
) -> int:
    """``triggerCfg`` bin for ``--iwr6843-tee-m`` in the replay convention.

    The ball's bin (:func:`trigger_ball_bin`, tape + 30 mm, as upstream's
    ``self_trigger_bin``), ``SELF_TRIGGER_TEE_LEAD_BINS`` short of it, then
    ``offset_m`` downrange in whole bins: 1.50 m gives 31 at offset 0 and 35 at
    +0.2 m. Raises when the bin falls outside the cfg's first capture window,
    which the board would never watch.
    """
    if not math.isfinite(offset_m) or abs(offset_m) > MAX_SELF_TRIGGER_OFFSET_M:
        raise ValueError(
            f"self-trigger offset must be within +/-{MAX_SELF_TRIGGER_OFFSET_M:g} m, got {offset_m}"
        )
    size = bin_size_m(fft_size)
    ball = trigger_ball_bin(tee_m, fft_size)
    watched = ball - SELF_TRIGGER_TEE_LEAD_BINS + int(round(offset_m / size))
    return check_first_window_bin(
        watched, config_path, f"self-trigger bin {watched} ({offset_m:+.2f} m offset)"
    )


def check_first_window_bin(global_bin: int, config_path: str | Path, what: str = "") -> int:
    """``global_bin`` when the cfg's first capture window holds it, else ValueError."""
    start, count = _first_window(config_path)
    if not start <= global_bin < start + count:
        raise ValueError(
            f"{what or f'bin {global_bin}'} is outside the first capture window, "
            f"bins {start}-{start + count - 1}"
        )
    return global_bin


def parse_trigger_cause(text: str) -> str | None:
    """Which rule fired, from a ``triggerLog track`` reply: crossing, end, leave or None.

    The firmware fires on ``ranged || left`` and dates the impact by the range
    rule when both hold (``l3_considerSelfTrigger``), so a fired range line wins.
    """
    match = _RANGE_LINE.search(text)
    if match is not None and match.group(1) == "1" and match.group(2) in ("crossing", "end"):
        return match.group(2)
    leave = _LEAVE_LINE.search(text)
    if leave is not None and leave.group(1) == "1":
        return "leave"
    return None


def parse_fire_latency(text: str) -> dict[str, float]:
    """``crossing=-1.7,end=1.6,leave=-0.5,unknown=-0.6,worst=9.5`` -> latencies.

    Missing keys keep the defaults (``DEFAULT_FIRE_LATENCY_MS``); ``worst`` only
    sizes the OPS split (``TiAnchorModel.max_lead_ms``).
    """
    latencies = dict(DEFAULT_FIRE_LATENCY_MS)
    if not text.strip():
        return latencies
    for item in text.split(","):
        key, sep, value = item.partition("=")
        key = key.strip()
        if not sep or key not in latencies:
            raise ValueError(
                f"fire latency entry {item!r}: expected RULE=MS with RULE one of "
                f"{', '.join(latencies)}"
            )
        try:
            latency = float(value)
        except ValueError as exc:
            raise ValueError(f"fire latency for {key} is not a number: {value!r}") from exc
        if not math.isfinite(latency) or not -30.0 <= latency <= 50.0:
            raise ValueError(f"fire latency for {key} must be -30..50 ms, got {value}")
        latencies[key] = latency
    return latencies


@dataclass(frozen=True)
class TiAnchorModel:
    """Where the OPS club-speed anchor lies for a TI-triggered shot.

    The OPS anchor constants are tuned to the microphone edge, which lies
    ``SOUND_EDGE_AFTER_IMPACT_MS`` after impact. Without a microphone the
    anchor is put where that edge would have been::

        impact = notice - notice_latency - fire_latency[cause]
        anchor = impact + sound_edge_after_impact

    :meth:`lead_ms` is how far the anchor lies before the notice (negative:
    after it).
    """

    fire_latency_ms: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_FIRE_LATENCY_MS))
    notice_latency_ms: float = NOTICE_LATENCY_MS
    sound_edge_after_impact_ms: float = SOUND_EDGE_AFTER_IMPACT_MS

    def fire_latency(self, cause: str | None) -> float:
        """Fire minus impact for ``cause`` (``unknown`` when None or unlisted)."""
        key = cause if cause in FIRE_CAUSES else "unknown"
        return self.fire_latency_ms[key]

    def lead_ms(self, cause: str | None) -> float:
        """Notice minus anchor, in ms."""
        return self.notice_latency_ms + self.fire_latency(cause) - self.sound_edge_after_impact_ms

    @property
    def max_lead_ms(self) -> float:
        """How far before the notice the impact can lie, sound-edge equivalent.

        What the OPS pre-trigger span must hold beyond the club history: the
        longest anchor lead of any rule, or the slowest fire (``worst``) when
        it is later; a fire in the tail of its rule still needs its club
        history in the buffer.
        """
        leads = [self.lead_ms(cause) for cause in (*FIRE_CAUSES, None)]
        worst = self.fire_latency_ms.get("worst")
        if worst is not None:
            leads.append(self.notice_latency_ms + worst - self.sound_edge_after_impact_ms)
        return max(leads)

    def as_dict(self) -> dict:
        """Session-start config form."""
        return {
            "fire_latency_ms": dict(self.fire_latency_ms),
            "notice_latency_ms": self.notice_latency_ms,
            "sound_edge_after_impact_ms": self.sound_edge_after_impact_ms,
            "lead_ms": {cause: self.lead_ms(cause) for cause in (*FIRE_CAUSES, None)},
            "max_lead_ms": self.max_lead_ms,
        }


@dataclass
class _ShadowNotice:
    notice: TriggerNotice
    cause: str | None = None
    matched: bool = False
    disposition: str | None = None


@dataclass
class _ShadowEdge:
    wall_time: float
    monotonic_ns: int
    ti_state: str
    listen_ms: float | None = None
    done: bool = False


class TiShadowMatcher:
    """Pair firmware notices with microphone edges on the monotonic clock.

    ``sink(fields)`` receives one dict per outcome, ``kind``:

    * ``matched``: a notice within ``window_ms`` (notice minus edge) of an edge;
    * ``notice_without_edge``: a would-be false fire;
    * ``edge_without_notice``: a would-be miss. ``ti_state`` says whether the
      board could fire at all (``listening``) or was dumping, recovering, etc.;
      ``ti_listen_ms`` is how long after the edge it could still fire before
      the sound path froze the ring (the ``--ti-shadow-holdoff-ms``).

    An edge is decided once ``window_ms[1]`` has passed after it, a notice once
    the whole window has (an edge may still come and pair with it). Thread
    safe: edges arrive on the GPIO thread, notices on the TI worker.
    """

    def __init__(
        self,
        sink: Callable[[dict], None],
        *,
        window_ms: tuple[float, float] = DEFAULT_SHADOW_WINDOW_MS,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ):
        low, high = window_ms
        if not low <= 0.0 <= high:
            raise ValueError(f"shadow window must contain 0, got {window_ms}")
        self._sink = sink
        self._low_ns = round(low * 1e6)
        self._high_ns = round(high * 1e6)
        self._now = monotonic_ns
        self._lock = threading.Lock()
        self._notices: list[_ShadowNotice] = []
        self._edges: list[_ShadowEdge] = []
        self._counts = {"matched": 0, "notice_without_edge": 0, "edge_without_notice": 0}
        self.window_ms = (low, high)

    def note_notice(self, notice: TriggerNotice) -> None:
        """A firmware fire was read."""
        with self._lock:
            self._notices.append(_ShadowNotice(notice))

    def note_cause(self, notice: TriggerNotice, cause: str | None) -> None:
        """The rule that fired, once ``triggerLog track`` answered."""
        with self._lock:
            for item in self._notices:
                if item.notice == notice:
                    item.cause = cause

    def note_disposition(self, notice: TriggerNotice, disposition: str) -> None:
        """What the host did with the latched ring (``dumped``/``released``/...)."""
        with self._lock:
            for item in self._notices:
                if item.notice == notice:
                    item.disposition = disposition

    def note_edge(self, wall_time: float, monotonic_ns: int, *, ti_state: str) -> None:
        """A microphone edge passed the ringing filter."""
        with self._lock:
            self._edges.append(_ShadowEdge(wall_time, monotonic_ns, ti_state))

    def note_listen(self, wall_time: float, listen_ms: float) -> None:
        """The sound-path ``l3dump`` for ``wall_time``'s edge left the board ``listen_ms``."""
        with self._lock:
            for edge in self._edges:
                if edge.wall_time == wall_time:
                    edge.listen_ms = listen_ms

    def flush(self, now_ns: int | None = None, *, everything: bool = False) -> None:
        """Decide every edge and notice whose window has closed."""
        now = self._now() if now_ns is None else now_ns
        emitted: list[dict] = []
        with self._lock:
            for edge in self._edges:
                if edge.done or (not everything and now < edge.monotonic_ns + self._high_ns):
                    continue
                edge.done = True
                emitted.append(self._decide_edge_locked(edge))
            span_ns = self._high_ns - self._low_ns
            keep: list[_ShadowNotice] = []
            for item in self._notices:
                if item.matched:
                    continue
                if not everything and now < item.notice.monotonic_ns + span_ns:
                    keep.append(item)
                    continue
                emitted.append(self._notice_fields_locked(item, "notice_without_edge", None))
            self._notices = keep
            self._edges = [
                edge
                for edge in self._edges
                if not edge.done or now - edge.monotonic_ns < 2 * span_ns
            ]
            for fields in emitted:
                self._counts[fields["kind"]] += 1
        for fields in emitted:
            self._sink(fields)

    def _decide_edge_locked(self, edge: _ShadowEdge) -> dict:
        candidates = [
            item
            for item in self._notices
            if not item.matched
            and self._low_ns <= item.notice.monotonic_ns - edge.monotonic_ns <= self._high_ns
        ]
        if not candidates:
            nearest = self._nearest_notice_ms_locked(edge.monotonic_ns)
            return {
                "kind": "edge_without_notice",
                "edge_timestamp": edge.wall_time,
                "edge_monotonic_ns": edge.monotonic_ns,
                "ti_state": edge.ti_state,
                "ti_listen_ms": edge.listen_ms,
                "nearest_notice_delta_ms": nearest,
                **self._window_fields(),
            }
        best = min(candidates, key=lambda item: abs(item.notice.monotonic_ns - edge.monotonic_ns))
        best.matched = True
        return self._notice_fields_locked(best, "matched", edge)

    def _nearest_notice_ms_locked(self, edge_ns: int) -> float | None:
        if not self._notices:
            return None
        nearest = min(self._notices, key=lambda item: abs(item.notice.monotonic_ns - edge_ns))
        return round((nearest.notice.monotonic_ns - edge_ns) / 1e6, 3)

    def _nearest_edge_ms_locked(self, notice_ns: int) -> float | None:
        if not self._edges:
            return None
        nearest = min(self._edges, key=lambda edge: abs(notice_ns - edge.monotonic_ns))
        return round((notice_ns - nearest.monotonic_ns) / 1e6, 3)

    def _window_fields(self) -> dict:
        return {"window_ms": list(self.window_ms)}

    def _notice_fields_locked(
        self, item: _ShadowNotice, kind: str, edge: _ShadowEdge | None
    ) -> dict:
        notice = item.notice
        fields = {
            "kind": kind,
            "notice_timestamp": notice.wall_time,
            "notice_monotonic_ns": notice.monotonic_ns,
            "notice_in_reply": notice.in_reply,
            "cause": item.cause,
            "ring": item.disposition,
            **self._window_fields(),
        }
        if edge is not None:
            fields.update(
                edge_timestamp=edge.wall_time,
                edge_monotonic_ns=edge.monotonic_ns,
                delta_ms=round((notice.monotonic_ns - edge.monotonic_ns) / 1e6, 3),
                ti_state=edge.ti_state,
            )
        else:
            fields["nearest_edge_delta_ms"] = self._nearest_edge_ms_locked(notice.monotonic_ns)
        return fields

    def summary(self) -> dict:
        """Counts per outcome so far."""
        with self._lock:
            return dict(self._counts)


__all__ = [
    "DEFAULT_FIRE_LATENCY_MS",
    "DEFAULT_SELF_TRIGGER_OFFSET_M",
    "DEFAULT_SELF_TRIGGER_SNR",
    "DEFAULT_SHADOW_WINDOW_MS",
    "FIRE_CAUSES",
    "SELF_TRIGGER_MODES",
    "SELF_TRIGGER_OFF_COMMAND",
    "SHOT_TRIGGERS",
    "SelfTriggerConfig",
    "TRIGGER_NOTICE",
    "TiAnchorModel",
    "TiShadowMatcher",
    "TriggerNotice",
    "parse_fire_latency",
    "parse_trigger_cause",
    "self_trigger_bin",
    "trigger_ball_bin",
]
