"""Events from the native iOS companion, forwarded by the paired phone's web page (log only).

The companion app (``ios/``, design in ``docs/ios-companion/design.md``) detects club-ball
impacts with the phone's microphone and reads the phone's barometer and GPS. Its web page
forwards them over its Socket.IO connection:

- ``phone_audio_onset``: one detected impact onset on the phone's clock.
- ``phone_sensors``: a sensor snapshot (barometer, location, network, battery, audio state).
- ``phone_clock_probe``: an NTP-style two-way probe that ties the phone's wall clock to the
  Pi's more tightly than the one-way ``client_clock`` report at connect.

This module validates the payloads, maps an onset to Pi time with the best clock estimate
and matches it to the nearest sound-trigger edge. Everything it produces is written to the
session log only: nothing here feeds a shot, the carry, the club data or the trigger.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .client_clock import ClientClockReading, parse_client_clock
from .edge_timing import EdgeTiming

# An onset is matched to the nearest sound-trigger edge within this window (Pi wall time).
EDGE_MATCH_WINDOW_S = 0.100

# Clock probes: kept for this long, and the one with the smallest round trip wins.
PROBE_WINDOW_S = 10 * 60.0
PROBE_HISTORY = 64
# Probes the server answered, waiting for the client's report (per connection and number).
PENDING_PROBES = 64
MAX_PROBE_RTT_MS = 5000.0

# Payload bounds (shape, not tuned values): the companion sends small flat objects.
MAX_TEXT = 120
MAX_KEY = 48
MAX_KEYS = 48
MAX_LIST = 16
MAX_DEPTH = 4
MAX_SAFE_INTEGER = 2**53
# A forwarded onset happened at most this long before its event was built (a backlog
# after a reconnect), and never after it.
MAX_ONSET_AGE_MS = 3600 * 1000.0
ONSET_FUTURE_SLACK_MS = 1000.0
PRESSURE_RANGE_HPA = (300.0, 1100.0)

ONSET_NUMBER_FIELDS = (
    "onset_host_us_raw",
    "input_latency_us",
    "onset_sample",
    "onset_uncertainty_us",
    "sample_rate_hz",
    "sample_rate_eff_hz",
    "host_fit_rms_us",
)
ONSET_TREE_FIELDS = ("detection", "features", "fat_cue")
ONSET_TEXT_FIELDS = ("snippet_file", "detector_version")
# The documented snapshot keys (design §7.3); anything else is dropped so a page cannot
# set the logger's ``ts`` or a join key such as ``shot_id`` in the ``phone_sensors`` entry.
SENSOR_TREE_FIELDS = ("clock", "barometer", "absolute_altitude", "location", "network", "audio")
SENSOR_SCALAR_FIELDS = ("battery_pct", "battery_state", "keep_awake")


class PhoneEventRejected(ValueError):
    """A phone event failed validation; ``code`` goes back in the ack."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _number(value: Any) -> Optional[float]:
    """A finite int or float (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _whole(value: Any, minimum: int = 0) -> Optional[int]:
    """A finite whole number in [minimum, 2^53), else None (5.0 counts, 5.5 does not)."""
    number = _number(value)
    if number is None or number != int(number) or not minimum <= number < MAX_SAFE_INTEGER:
        return None
    return int(number)


def _clean_container(value: Any, depth: int) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if len(out) >= MAX_KEYS:
                break
            if isinstance(key, str) and key:
                out[key[:MAX_KEY]] = clean_tree(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [clean_tree(item, depth + 1) for item in value[:MAX_LIST]]
    return None


def clean_tree(value: Any, depth: int = 0) -> Any:
    """A JSON-safe, bounded copy: finite numbers, bools, short strings, small dicts/lists.

    Non-finite numbers become None; anything else unknown is dropped (None).
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return value[:MAX_TEXT]
    return _clean_container(value, depth) if depth < MAX_DEPTH else None


def _clock(data: dict, *, server_epoch: float, server_tz_offset_min: Optional[int]):
    clock = parse_client_clock(
        data,
        client="phone",
        server_epoch=server_epoch,
        server_tz_offset_min=server_tz_offset_min,
    )
    if clock is None:
        raise PhoneEventRejected("malformed", "epoch_ms")
    return clock


def _clock_pair(raw: Any) -> Dict[str, float]:
    """The native clock pair ``{host_us, epoch_ms, bracket_us}`` (design §7.1)."""
    if not isinstance(raw, dict):
        raise PhoneEventRejected("malformed", "clock")
    host_us = _whole(raw.get("host_us"))
    epoch_ms = _number(raw.get("epoch_ms"))
    bracket_us = _number(raw.get("bracket_us"))
    if host_us is None or epoch_ms is None or epoch_ms <= 0 or bracket_us is None or bracket_us < 0:
        raise PhoneEventRejected("malformed", "clock")
    return {"host_us": host_us, "epoch_ms": epoch_ms, "bracket_us": bracket_us}


@dataclass(frozen=True)
class PhoneAudioOnset:
    """A validated ``phone_audio_onset``: the native ``audio_onset`` event (design §9.8)."""

    capture_id: str
    seq: int
    onset_host_us: int
    onset_epoch_ms: float
    clock_pair: Dict[str, float]
    fields: Dict[str, Any]
    client: ClientClockReading

    def to_log(self) -> Dict[str, Any]:
        """The payload's fields for the session log (match and clock fields added by the caller)."""
        return {
            "capture_id": self.capture_id,
            "seq": self.seq,
            "onset_host_us": self.onset_host_us,
            "onset_epoch_ms": self.onset_epoch_ms,
            "clock": self.clock_pair,
            **self.fields,
            "client_epoch_ms": self.client.client_epoch_ms,
            "client_tz_offset_min": self.client.client_tz_offset_min,
        }


def parse_audio_onset(
    data: Any, *, server_epoch: float, server_tz_offset_min: Optional[int] = None
) -> PhoneAudioOnset:
    """Validate a forwarded ``audio_onset`` event; raises PhoneEventRejected."""
    if not isinstance(data, dict) or data.get("type") != "audio_onset":
        raise PhoneEventRejected("malformed", "type")
    client = _clock(data, server_epoch=server_epoch, server_tz_offset_min=server_tz_offset_min)
    capture_id = data.get("capture_id")
    if not isinstance(capture_id, str) or not 0 < len(capture_id) <= 64:
        raise PhoneEventRejected("malformed", "capture_id")
    seq = _whole(data.get("seq"), minimum=1)
    onset_host_us = _whole(data.get("onset_host_us"))
    onset_epoch_ms = _number(data.get("onset_epoch_ms"))
    if seq is None or onset_host_us is None or onset_epoch_ms is None or onset_epoch_ms <= 0:
        raise PhoneEventRejected("malformed", "onset")
    built_ms = float(client.client_epoch_ms)
    if not built_ms - MAX_ONSET_AGE_MS <= onset_epoch_ms <= built_ms + ONSET_FUTURE_SLACK_MS:
        raise PhoneEventRejected("out_of_range", "onset_epoch_ms")
    fields: Dict[str, Any] = {}
    for key in ONSET_NUMBER_FIELDS:
        fields[key] = _number(data.get(key))
    for key in ONSET_TREE_FIELDS:
        tree = clean_tree(data.get(key))
        fields[key] = tree if isinstance(tree, dict) else None
    for key in ONSET_TEXT_FIELDS:
        text = data.get(key)
        fields[key] = text[:MAX_TEXT] if isinstance(text, str) else None
    return PhoneAudioOnset(
        capture_id=capture_id,
        seq=seq,
        onset_host_us=onset_host_us,
        onset_epoch_ms=onset_epoch_ms,
        clock_pair=_clock_pair(data.get("clock")),
        fields=fields,
        client=client,
    )


def parse_sensors(
    data: Any, *, server_epoch: float, server_tz_offset_min: Optional[int] = None
) -> Tuple[Dict[str, Any], ClientClockReading]:
    """Validate a forwarded sensor snapshot (design §7.3); returns ``(fields, clock)``."""
    if not isinstance(data, dict) or data.get("type") != "sensors":
        raise PhoneEventRejected("malformed", "type")
    client = _clock(data, server_epoch=server_epoch, server_tz_offset_min=server_tz_offset_min)
    cleaned = clean_tree({key: data.get(key) for key in SENSOR_TREE_FIELDS + SENSOR_SCALAR_FIELDS})
    snapshot: Dict[str, Any] = {}
    for key in SENSOR_TREE_FIELDS:
        tree = cleaned.get(key)
        snapshot[key] = tree if isinstance(tree, dict) else None
    for key in SENSOR_SCALAR_FIELDS:
        value = cleaned.get(key)
        snapshot[key] = None if isinstance(value, (dict, list)) else value
    barometer = snapshot["barometer"]
    if barometer is not None:
        pressure = _number(barometer.get("pressure_hpa"))
        if pressure is not None and not PRESSURE_RANGE_HPA[0] <= pressure <= PRESSURE_RANGE_HPA[1]:
            raise PhoneEventRejected("out_of_range", "barometer.pressure_hpa")
    location = snapshot["location"]
    if location is not None:
        latitude = _number(location.get("latitude"))
        longitude = _number(location.get("longitude"))
        if (latitude is not None and abs(latitude) > 90.0) or (
            longitude is not None and abs(longitude) > 180.0
        ):
            raise PhoneEventRejected("out_of_range", "location")
    snapshot["client_epoch_ms"] = client.client_epoch_ms
    snapshot["client_tz_offset_min"] = client.client_tz_offset_min
    return snapshot, client


@dataclass(frozen=True)
class ClockProbeSample:
    """One completed two-way probe: phone wall minus Pi wall, bounded by half the round trip."""

    offset_s: float
    rtt_ms: float
    pi_epoch_s: float
    pi_monotonic_ns: int
    recorded_monotonic_s: float


@dataclass(frozen=True)
class ClockEstimate:
    """The phone-minus-Pi offset used to map phone wall time to Pi wall time."""

    offset_s: Optional[float]
    source: Optional[str]  # "probe", "client_clock" or None
    uncertainty_ms: Optional[float]
    probe_count: int

    def to_log(self) -> Dict[str, Any]:
        """Clock fields of the ``phone_*`` session-log entries."""
        return {
            "clock_offset_s": None if self.offset_s is None else round(self.offset_s, 6),
            "clock_source": self.source,
            "clock_uncertainty_ms": (
                None if self.uncertainty_ms is None else round(self.uncertainty_ms, 3)
            ),
            "clock_probe_n": self.probe_count,
        }


class PhoneClockProbes:
    """Two-way clock probes (``phone_clock_probe``) and the best phone-to-Pi offset.

    The phone emits a probe with its send time and gets the Pi's wall and monotonic time in
    the ack. It then reports the round trip it measured; the offset is computed here from
    the Pi's own stamp of that probe, so a report can only refer to a probe this server
    answered on the same connection.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: "OrderedDict[Tuple[str, int], Tuple[float, float, int, float]]" = (
            OrderedDict()
        )
        self._samples: deque[ClockProbeSample] = deque(maxlen=PROBE_HISTORY)

    def answer(
        self,
        sid: str,
        probe_seq: int,
        client_send_epoch_ms: float,
        *,
        pi_epoch_s: float,
        pi_monotonic_ns: int,
    ) -> Dict[str, Any]:
        """Remember a probe and return its ack payload."""
        with self._lock:
            key = (sid, probe_seq)
            self._pending.pop(key, None)
            self._pending[key] = (
                client_send_epoch_ms,
                pi_epoch_s,
                pi_monotonic_ns,
                pi_monotonic_ns / 1e9,
            )
            while len(self._pending) > PENDING_PROBES:
                self._pending.popitem(last=False)
        return {
            "ok": True,
            "probe_seq": probe_seq,
            "pi_epoch_s": pi_epoch_s,
            "pi_monotonic_ns": pi_monotonic_ns,
        }

    def report(
        self, sid: str, probe_seq: int, client_send_epoch_ms: float, rtt_ms: float
    ) -> Optional[ClockProbeSample]:
        """Turn the phone's round trip for an answered probe into a sample; None if unknown."""
        if not 0.0 <= rtt_ms <= MAX_PROBE_RTT_MS:
            return None
        with self._lock:
            key = (sid, probe_seq)
            pending = self._pending.get(key)
            if pending is None or pending[0] != client_send_epoch_ms:
                return None
            del self._pending[key]
            _, pi_epoch_s, pi_monotonic_ns, answered_s = pending
            sample = ClockProbeSample(
                offset_s=(client_send_epoch_ms + rtt_ms / 2.0) / 1000.0 - pi_epoch_s,
                rtt_ms=rtt_ms,
                pi_epoch_s=pi_epoch_s,
                pi_monotonic_ns=pi_monotonic_ns,
                recorded_monotonic_s=answered_s,
            )
            self._samples.append(sample)
        return sample

    def best(self, now_monotonic_s: float) -> Tuple[Optional[ClockProbeSample], int]:
        """The recent sample with the smallest round trip, and how many recent ones exist."""
        with self._lock:
            recent = [
                sample
                for sample in self._samples
                if now_monotonic_s - sample.recorded_monotonic_s <= PROBE_WINDOW_S
            ]
        if not recent:
            return None, 0
        return min(recent, key=lambda sample: sample.rtt_ms), len(recent)

    def estimate(
        self, now_monotonic_s: float, client_clock_offset_s: Optional[float]
    ) -> ClockEstimate:
        """Best probe, else the phone's connect-time ``client_clock`` offset, else nothing."""
        sample, count = self.best(now_monotonic_s)
        if sample is not None:
            return ClockEstimate(sample.offset_s, "probe", sample.rtt_ms / 2.0, count)
        if client_clock_offset_s is not None:
            return ClockEstimate(client_clock_offset_s, "client_clock", None, 0)
        return ClockEstimate(None, None, None, 0)

    def clear(self) -> None:
        """Forget all probes (tests)."""
        with self._lock:
            self._pending.clear()
            self._samples.clear()


def parse_probe(
    data: Any,
) -> Tuple[Optional[Tuple[int, float]], Optional[Tuple[int, float, float]]]:
    """``(probe, report)`` of a ``phone_clock_probe`` payload; either may be None.

    ``probe`` is ``(probe_seq, client_send_epoch_ms)``; ``report`` is
    ``(probe_seq, client_send_epoch_ms, rtt_ms)``. Raises when neither is valid.
    """
    if not isinstance(data, dict):
        raise PhoneEventRejected("malformed")
    probe = None
    seq = _whole(data.get("probe_seq"), minimum=1)
    send = _number(data.get("client_send_epoch_ms"))
    if seq is not None and send is not None and send > 0:
        probe = (seq, send)
    report = None
    raw = data.get("report")
    if isinstance(raw, dict):
        report_seq = _whole(raw.get("probe_seq"), minimum=1)
        report_send = _number(raw.get("client_send_epoch_ms"))
        rtt = _number(raw.get("rtt_ms"))
        if report_seq is not None and report_send is not None and rtt is not None:
            report = (report_seq, report_send, rtt)
    if probe is None and report is None:
        raise PhoneEventRejected("malformed")
    return probe, report


def _edge_time(timing: EdgeTiming) -> Tuple[float, str]:
    """The edge on the Pi's wall clock: the kernel line-event time when known."""
    if timing.kernel_timestamp is not None:
        return timing.kernel_timestamp, "kernel"
    return timing.callback_timestamp, "callback"


def _edge_monotonic_ns(timing: EdgeTiming) -> int:
    """The edge on the Pi's monotonic clock (as ``server._trigger_stamp_lag`` computes it)."""
    latency_ms = timing.latency_ms or 0.0
    return int(round(timing.callback_monotonic_ns - latency_ms * 1e6))


def match_onset_to_edges(
    onset_pi_epoch_s: Optional[float],
    edges: Iterable[Tuple[int, EdgeTiming]],
    window_s: float = EDGE_MATCH_WINDOW_S,
) -> Dict[str, Any]:
    """Match an onset (Pi wall time) to the nearest sound-trigger edge within ``window_s``.

    ``edge_match`` is ``matched`` (one edge in the window), ``ambiguous`` (several; the
    nearest is reported), ``none`` (no edge in the window) or ``no_clock`` (no clock
    estimate to map the onset with). ``edge_residual_ms`` is onset minus edge.
    """
    result: Dict[str, Any] = {
        "edge_match": "no_clock" if onset_pi_epoch_s is None else "none",
        "edge_candidates": 0,
        "edge_sequence": None,
        "edge_timestamp": None,
        "edge_time_source": None,
        "edge_kernel_timestamp": None,
        "edge_callback_timestamp": None,
        "edge_monotonic_ns": None,
        "edge_residual_ms": None,
        "edge_next_residual_ms": None,
    }
    if onset_pi_epoch_s is None:
        return result
    candidates: List[Tuple[float, int, EdgeTiming, float, str]] = []
    for sequence, timing in edges:
        edge_s, source = _edge_time(timing)
        residual_s = onset_pi_epoch_s - edge_s
        if abs(residual_s) <= window_s:
            candidates.append((abs(residual_s), sequence, timing, residual_s, source))
    if not candidates:
        return result
    candidates.sort(key=lambda item: item[0])
    _, sequence, timing, residual_s, source = candidates[0]
    result.update(
        edge_match="matched" if len(candidates) == 1 else "ambiguous",
        edge_candidates=len(candidates),
        edge_sequence=sequence,
        edge_timestamp=_edge_time(timing)[0],
        edge_time_source=source,
        edge_kernel_timestamp=timing.kernel_timestamp,
        edge_callback_timestamp=timing.callback_timestamp,
        edge_monotonic_ns=_edge_monotonic_ns(timing),
        edge_residual_ms=round(residual_s * 1000.0, 3),
        edge_next_residual_ms=(
            round(candidates[1][3] * 1000.0, 3) if len(candidates) > 1 else None
        ),
    )
    return result


def onset_log_entry(
    onset: PhoneAudioOnset,
    *,
    estimate: ClockEstimate,
    edges: Iterable[Tuple[int, EdgeTiming]],
    server_monotonic_ns: int,
) -> Dict[str, Any]:
    """The ``phone_audio_onset`` session-log entry: payload, clock mapping and edge match."""
    onset_pi_epoch_s = (
        None if estimate.offset_s is None else onset.onset_epoch_ms / 1000.0 - estimate.offset_s
    )
    return {
        **onset.to_log(),
        "server_epoch": onset.client.server_epoch,
        "server_monotonic_ns": server_monotonic_ns,
        **estimate.to_log(),
        "onset_pi_epoch_s": None if onset_pi_epoch_s is None else round(onset_pi_epoch_s, 6),
        **match_onset_to_edges(onset_pi_epoch_s, edges),
    }


__all__ = [
    "EDGE_MATCH_WINDOW_S",
    "ClockEstimate",
    "ClockProbeSample",
    "PhoneAudioOnset",
    "PhoneClockProbes",
    "PhoneEventRejected",
    "clean_tree",
    "match_onset_to_edges",
    "onset_log_entry",
    "parse_audio_onset",
    "parse_probe",
    "parse_sensors",
]
