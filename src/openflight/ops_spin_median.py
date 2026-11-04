"""Session median of the OPS spin per club, written to the session log only.

The OPS spin of a single shot is rarely trusted (its confidence stays below the 0.7
the carry needs), but the 2026-10-25 spin check found the per-club median of the
other shots' OPS spin (its "S3m") closer to the real spin of some clubs than the
club-typical value. This keeps that median live, from the shots before the current
one, so later sessions can be scored offline. It never reaches the shot payload, the
UI, the carry or the simulator export.
"""

import math
import statistics
import threading
from typing import Optional

from .clubs import ClubType
from .launch_monitor import Shot

# Fewer earlier shots of the club give no median; the spin check used the same floor.
MIN_EARLIER_SHOTS = 3


def ops_spin_rpm(shot: Shot) -> Optional[float]:
    """The OPS radar's own spin reading of a shot, at any confidence.

    That is `spin_rpm`, or `spin_rpm_measured` where --calculated-spin replaced
    `spin_rpm` with its model. None when the OPS gave no positive, finite spin.
    """
    value = shot.spin_rpm_measured if shot.spin_source == "calculated" else shot.spin_rpm
    if value is None or isinstance(value, bool):
        return None
    try:
        rpm = float(value)
    except (TypeError, ValueError):
        return None
    return rpm if math.isfinite(rpm) and rpm > 0.0 else None


class OpsSpinSessionMedian:
    """Per-club OPS spins of the session's earlier shots. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spins: dict[ClubType, list[tuple[str, float]]] = {}
        self._profile_id: Optional[str] = None

    def observe(self, shot: Shot) -> tuple[Optional[float], int]:
        """(median, n) of the earlier shots of this club, then remember this shot's spin.

        n counts the earlier shots of the club with an OPS spin; the median is None while
        n < MIN_EARLIER_SHOTS. The shot itself is never part of its own median. A shot
        observed again (same timestamp) replaces its earlier reading instead of counting
        twice. A shot of another profile than the previous one starts over, as a profile
        switch does.
        """
        timestamp = shot.timestamp.isoformat()
        rpm = ops_spin_rpm(shot)
        with self._lock:
            if shot.profile_id != self._profile_id:
                self._spins.clear()
                self._profile_id = shot.profile_id
            club_spins = self._spins.setdefault(shot.club, [])
            club_spins[:] = [entry for entry in club_spins if entry[0] != timestamp]
            earlier = [value for _, value in club_spins]
            if rpm is not None:
                club_spins.append((timestamp, rpm))
        n = len(earlier)
        median = statistics.median(earlier) if n >= MIN_EARLIER_SHOTS else None
        return median, n

    def forget(self, timestamp: str) -> None:
        """Drop a deleted shot, so it no longer counts for later shots."""
        with self._lock:
            for club_spins in self._spins.values():
                club_spins[:] = [entry for entry in club_spins if entry[0] != timestamp]

    def reset(self) -> None:
        """Start over: a cleared session, another profile or a new monitor session."""
        with self._lock:
            self._spins.clear()
            self._profile_id = None


def log_fields(median: Optional[float], n: int) -> dict:
    """The two session-log fields of a shot entry."""
    return {
        "ops_spin_session_median_rpm": round(median, 1) if median is not None else None,
        "ops_spin_session_median_n": n,
    }
