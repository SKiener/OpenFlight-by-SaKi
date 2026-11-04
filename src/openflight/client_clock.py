"""Compare the Pi's clock with the clocks of the kiosk and phone browsers.

At the range the Pi has no NTP, so its clock can be days off. Every Socket.IO client
reports its own time once per connection; the difference tells how far the session
timestamps are off. The Pi's system time is never changed here.
"""

from __future__ import annotations

import math
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Optional

# Offsets below this are network delay or ordinary clock drift, not a wrong clock.
CLOCK_OFFSET_WARN_S = 120.0

CLIENT_KINDS = ("kiosk", "phone")


@dataclass(frozen=True)
class ClientClockReading:
    """One client's clock report, compared with the Pi's clock when it arrived.

    ``offset_s`` is client minus Pi as a UTC epoch difference: positive means the Pi
    clock is behind. Session ``ts`` values are the Pi's naive local time, so correct
    them via UTC with ``server_tz_offset_min`` (the Pi's UTC offset when the report
    arrived); adding ``offset_s`` straight to ``ts`` is an hour off when a DST change
    lies between the Pi's date and the real date.
    """

    client: str
    client_epoch_ms: int
    client_tz_offset_min: Optional[int]
    server_epoch: float
    offset_s: float
    server_tz_offset_min: Optional[int] = None

    def to_log(self) -> Dict[str, Any]:
        """Fields of the session-log ``client_clock`` event."""
        return asdict(self)


def local_utc_offset_min(epoch: float) -> Optional[int]:
    """The Pi's local-time-minus-UTC in minutes at ``epoch`` (e.g. 120 for CEST)."""
    try:
        offset = datetime.fromtimestamp(epoch).astimezone().utcoffset()
    except (OverflowError, OSError, ValueError):
        return None
    return None if offset is None else int(offset.total_seconds() // 60)


def parse_client_clock(
    data: Any,
    *,
    client: str,
    server_epoch: float,
    server_tz_offset_min: Optional[int] = None,
) -> Optional[ClientClockReading]:
    """Build a reading from the UI's ``client_clock`` payload, or None if it is malformed."""
    if not isinstance(data, dict) or client not in CLIENT_KINDS:
        return None
    epoch_ms = data.get("epoch_ms")
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, (int, float)):
        return None
    if not math.isfinite(epoch_ms) or epoch_ms <= 0:
        return None
    tz_offset = data.get("tz_offset_min")
    if isinstance(tz_offset, bool) or not isinstance(tz_offset, (int, float)):
        tz_offset = None
    elif not math.isfinite(tz_offset):
        tz_offset = None
    return ClientClockReading(
        client=client,
        client_epoch_ms=int(epoch_ms),
        client_tz_offset_min=None if tz_offset is None else int(tz_offset),
        server_epoch=round(server_epoch, 3),
        offset_s=round(epoch_ms / 1000.0 - server_epoch, 3),
        server_tz_offset_min=server_tz_offset_min,
    )


def format_clock_offset(offset_s: float) -> str:
    """Signed days/hours/minutes, e.g. ``+5 d 17 h 29 min``."""
    sign = "-" if offset_s < 0 else "+"
    total_min = int(abs(offset_s) / 60.0 + 0.5)  # half up, not to even
    days, rest = divmod(total_min, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} d")
    if days or hours:
        parts.append(f"{hours} h")
    parts.append(f"{minutes} min")
    return sign + " ".join(parts)


class ClientClockTracker:
    """Latest offset per client kind, and which sessions were already warned about."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: Dict[str, ClientClockReading] = {}
        self._warned_sessions: set = set()

    def record(self, reading: ClientClockReading, session_id: Optional[str]) -> bool:
        """Store the reading; True when this session should get its one clock warning."""
        with self._lock:
            self._latest[reading.client] = reading
            if abs(reading.offset_s) <= CLOCK_OFFSET_WARN_S:
                return False
            if session_id in self._warned_sessions:
                return False
            self._warned_sessions.add(session_id)
            return True

    def status(self) -> Dict[str, Optional[float]]:
        """Latest offset in seconds for each client kind (None until one reported)."""
        with self._lock:
            return {
                kind: (self._latest[kind].offset_s if kind in self._latest else None)
                for kind in CLIENT_KINDS
            }

    def clear(self) -> None:
        """Forget all readings and warnings (tests)."""
        with self._lock:
            self._latest.clear()
            self._warned_sessions.clear()
