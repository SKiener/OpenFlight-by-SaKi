"""Read-only, 30-day view of saved shot logs; never imports them into a live session."""

import json
import math
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from .dplane import CURVE_CLASSES
from .launch_monitor import BALL_SPEED_UNCERTAIN, CLUB_DATA_SHOT_FIELDS
from .rollout import MAX_ROLL_POINTS

HISTORY_DAYS = 30
_SESSION_ID = re.compile(r"session_\d{8}_\d{6}_[A-Za-z0-9_-]{1,64}")
_ENTRY_TYPE = re.compile(
    r'"type"\s*:\s*"(session_start|shot_detected|shot_updated|camera_capture|shot_deleted'
    r'|club_data)"'
)
_NUMBERS = (
    "shot_number",
    "ball_speed_mph",
    "club_speed_mph",
    "estimated_carry_yards",
    "carry_spin_adjusted",
    "carry_spin_rpm",
    "carry_lateral_yards",
    "apex_yards",
    "roll_yards",
    "total_yards",
    "launch_angle_vertical",
    "launch_angle_horizontal",
    "launch_angle_horizontal_confidence",
    "spin_rpm",
    "spin_rpm_measured",
    "spin_confidence",
    "spin_axis_deg",
    "smash_factor",
)
_TEXT = (
    "timestamp",
    "club",
    "profile_id",
    "profile_name",
    "mode",
    "carry_spin_source",
    "carry_model",
    "spin_quality",
    "spin_source",
    "spin_axis_source",
    "angle_source",
    "launch_angle_vertical_source",
    "launch_angle_horizontal_source",
)
# A 15 s flight sampled every 50 ms is about 300 points; this only bounds a damaged file.
_MAX_PATH_POINTS = 400
# Calculated club data (``club_data`` entries, merged into their shot); the classes are
# the only text values.
_CLUB_DATA_CLASSES = {
    "face_to_path_class": ("closed", "neutral", "open"),
    "spin_axis_class": ("draw", "straight", "fade"),
    "impact_zone": ("heel", "center", "toe"),
    "impact_method": ("D", "E"),
}
_CLUB_DATA_STATUSES = ("pending", "done", "skipped")
# Rules of the OPS ball-track guard (RollingBufferProcessor.BALL_GUARD_CLUB_LIKE_TRACK and
# BALL_GUARD_SMASH_IMPLAUSIBLE); the reason is also set when the guard replaced the ball speed.
_BALL_SPEED_STATUS_REASONS = ("club_like_track", "smash_implausible")
# Class words of ``shot_detected`` itself; anything else reads None.
_SHOT_CLASSES = {
    "curve_class": CURVE_CLASSES,
    "ball_speed_status": (BALL_SPEED_UNCERTAIN,),
    "ball_speed_status_reason": _BALL_SPEED_STATUS_REASONS,
}
# What a ``shot_updated`` entry (the shot flown again with a later spin axis) replaces.
_UPDATED_KEYS = (
    "spin_axis_deg",
    "spin_axis_source",
    "curve_class",
    "estimated_carry_yards",
    "carry_spin_adjusted",
    "carry_spin_rpm",
    "carry_spin_source",
    "carry_model",
    "carry_lateral_yards",
    "apex_yards",
    "roll_yards",
    "total_yards",
    "_flight_path",
    "_roll_path",
)


def _finite_number(value: object) -> bool:
    """A finite int or float; a bool (JSON true/false) is an int subclass but not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large to convert to a float
        return False


def _capture_key(number, timestamp):
    """Do not associate recordings by row position or a reused shot number alone."""
    if (
        isinstance(number, int)
        and not isinstance(number, bool)
        and number > 0
        and _finite_number(timestamp)
        and timestamp > 0
    ):
        return number, timestamp
    return None


def _club_data(entry: dict) -> dict | None:
    """The shot fields of a ``club_data`` entry the server showed, validated; else None.

    Numbers must be finite and classes one of their words; anything else reads None.
    An entry written in ``log`` mode never reached a shot and is not merged.
    """
    fields = entry.get("fields")
    status = entry.get("status")
    if entry.get("mode", "show") != "show" or not isinstance(fields, dict):
        return None
    merged = {"club_data_status": status if status in _CLUB_DATA_STATUSES else None}
    for key in CLUB_DATA_SHOT_FIELDS:
        if key in ("club_data_status", "club_data_reason"):
            continue
        value = fields.get(key)
        if key in _CLUB_DATA_CLASSES:
            merged[key] = value if value in _CLUB_DATA_CLASSES[key] else None
        else:
            merged[key] = value if _finite_number(value) else None
    return merged


def _path(value: object, max_points: int) -> dict | None:
    """Return a stored flight or roll path only if it has the shape the server writes, else None."""
    if not isinstance(value, dict) or value.get("unit") != "yd":
        return None
    points = value.get("points")
    if not isinstance(points, list) or not 2 <= len(points) <= max_points:
        return None
    for point in points:
        if not isinstance(point, list) or len(point) != 4 or not all(map(_finite_number, point)):
            return None
    return {"unit": "yd", "points": points}


def _path_text(value: object, max_points: int) -> str | None:
    """The valid path as compact JSON text, else None.

    The cache keeps text (about 3 KB per path); the parsed lists would be about 25 KiB.
    """
    path = _path(value, max_points)
    return None if path is None else json.dumps(path, separators=(",", ":"))


def _shot(entry: dict) -> dict | None:
    """The history fields of a ``shot_detected`` or ``shot_updated`` entry; None to skip it."""
    if (
        entry.get("demo") is True
        or not _finite_number(entry.get("ball_speed_mph"))
        or not isinstance(entry.get("timestamp"), str)
    ):
        return None
    shot = {
        key: entry.get(key, "")[:200] if isinstance(entry.get(key), str) else "" for key in _TEXT
    }
    for key in _NUMBERS:
        value = entry.get(key)
        shot[key] = value if _finite_number(value) else None
    for key, words in _SHOT_CLASSES.items():
        value = entry.get(key)
        shot[key] = value if value in words else None
    shot["_flight_path"] = _path_text(entry.get("flight_path"), _MAX_PATH_POINTS)
    shot["_roll_path"] = _path_text(entry.get("roll_path"), MAX_ROLL_POINTS)
    shot["_capture_key"] = _capture_key(entry.get("shot_number"), entry.get("impact_timestamp"))
    return shot


def _add_shot(entry: dict, shots: list, updates: dict) -> None:
    """Collect a ``shot_detected`` row, or keep a ``shot_updated`` one by its shot key."""
    shot = _shot(entry)
    if shot is None:
        return
    if entry["type"] == "shot_detected":
        shots.append(shot)
    else:
        # A later update replaces an earlier one: each is the whole shot then.
        updates[shot["_capture_key"]] = shot


def _merge_later_entries(shots: list, updates: dict, club_data: dict) -> None:
    """Lay each shot's ``shot_updated`` flight and its ``club_data`` fields over it."""
    for shot in shots:
        key = shot["_capture_key"]
        updated = updates.get(key) if key is not None else None
        if updated is not None and updated["timestamp"] == shot["timestamp"]:
            shot.update({name: updated[name] for name in _UPDATED_KEYS})
        calculated = club_data.get(key)
        if calculated is not None:
            shot.update(calculated)


@lru_cache(maxsize=64)
def _read_session(path: Path, mtime_ns: int, size: int) -> dict:
    del mtime_ns, size  # Cache identity includes the current file revision.
    started = None
    shots = []
    captures = {}
    club_data = {}
    updates = {}
    deleted = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not _ENTRY_TYPE.search(line):
                continue
            try:
                entry = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "session_start":
                started = entry.get("start_time")
            elif entry.get("type") == "shot_deleted":
                deleted.append(entry.get("shot_timestamp"))
            elif entry.get("type") == "camera_capture":
                key = _capture_key(entry.get("shot_number"), entry.get("shot_timestamp"))
                if key is not None:
                    # Ambiguous duplicate correlations must never play the wrong clip.
                    captures[key] = None if key in captures else entry
            elif entry.get("type") == "club_data":
                key = _capture_key(entry.get("shot_number"), entry.get("shot_timestamp"))
                if key is not None:
                    # Like a clip: an ambiguous duplicate never lends a shot the wrong values.
                    club_data[key] = None if key in club_data else _club_data(entry)
            elif entry.get("type") in ("shot_detected", "shot_updated"):
                _add_shot(entry, shots, updates)
    deleted = frozenset(stamp for stamp in deleted if isinstance(stamp, str))
    shots = [shot for shot in shots if shot["timestamp"] not in deleted]
    _merge_later_entries(shots, updates, club_data)
    try:
        start_time = datetime.fromisoformat(started).astimezone()
    except (ValueError, TypeError):
        start_time = datetime.strptime(path.stem[8:23], "%Y%m%d_%H%M%S").astimezone()
    return {
        "id": path.stem,
        "started_at": start_time.isoformat(),
        "shot_count": len(shots),
        "clubs": sorted({shot["club"] for shot in shots if shot["club"]}),
        "shots": shots,
        "_captures": captures,
        "_deleted": deleted,
    }


def _cached_session(log_dir: Path, session_id: str, *, now: datetime | None, days: int) -> dict:
    """The shared cached read of a regular session log started in the last ``days``.

    Callers must not mutate it or hand it out; FileNotFoundError when it is not available.
    """
    if not _SESSION_ID.fullmatch(session_id):
        raise FileNotFoundError(session_id)
    path = log_dir / f"{session_id}.jsonl"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(session_id)
    stat = path.stat()
    result = _read_session(path, stat.st_mtime_ns, stat.st_size)
    current = (now or datetime.now().astimezone()).astimezone()
    started = datetime.fromisoformat(result["started_at"])
    if not current - timedelta(days=days) <= started <= current:
        raise FileNotFoundError(session_id)
    return result


def find_saved_shot(
    log_dir: Path, session_id: str, shot_timestamp: str, *, now: datetime | None = None
) -> tuple[Path, bool]:
    """The session log holding this shot and whether the shot is already deleted.

    ValueError for a malformed id or an empty timestamp; FileNotFoundError when the session
    is not in the history or the shot is not in it.
    """
    if not _SESSION_ID.fullmatch(session_id) or not shot_timestamp:
        raise ValueError(session_id)
    session = _cached_session(log_dir, session_id, now=now, days=HISTORY_DAYS)
    path = log_dir / f"{session_id}.jsonl"
    if shot_timestamp in session["_deleted"]:
        return path, True
    if any(shot["timestamp"] == shot_timestamp for shot in session["shots"]):
        return path, False
    raise FileNotFoundError(shot_timestamp)


def append_shot_deleted(path: Path, shot_timestamp: str) -> None:
    """Append a shot_deleted line; the shot's own line stays in the file."""
    entry = {
        "ts": datetime.now().isoformat(),
        "type": "shot_deleted",
        "shot_timestamp": shot_timestamp,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _window_stems(log_dir: Path, *, now: datetime | None, days: int) -> list[str]:
    """Session logs whose file name (else mtime) puts their start within ``days`` plus a day."""
    current = (now or datetime.now().astimezone()).astimezone()
    cutoff = current - timedelta(days=days + 1)
    stems = []
    for path in log_dir.glob("session_*.jsonl"):
        try:
            started = datetime.strptime(path.stem[8:23], "%Y%m%d_%H%M%S").astimezone()
        except ValueError:
            try:
                started = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
            except OSError:
                continue
        if started >= cutoff:
            stems.append(path.stem)
    return stems


def get_session(
    log_dir: Path,
    session_id: str,
    *,
    now: datetime | None = None,
    replay_manager=None,
    flight_paths: bool = False,
) -> dict:
    """Look up only regular session logs within the visible retention window.

    A flight path is about 3 KB per shot and few readers want it, so paths are opt-in: shots
    carry ``flight_path`` and ``roll_path`` (the path, or None) only with ``flight_paths=True``.
    """
    result = _cached_session(log_dir, session_id, now=now, days=HISTORY_DAYS)
    # Never mutate cached metrics or expose internal file paths to the browser.
    public = {key: value for key, value in result.items() if not key.startswith("_")}
    public["shots"] = []
    for saved in result["shots"]:
        shot = {key: value for key, value in saved.items() if not key.startswith("_")}
        if flight_paths:
            # Parsed per call, so every caller owns its copy and the cache keeps only text.
            for key in ("flight_path", "roll_path"):
                text = saved[f"_{key}"]
                shot[key] = None if text is None else json.loads(text)
        capture = result["_captures"].get(saved["_capture_key"])
        if (
            replay_manager is not None
            and isinstance(capture, dict)
            and not capture.get("capture_error")
        ):
            capture_path = capture.get("capture_path")
            metadata = capture.get("metadata")
            if isinstance(capture_path, str) and isinstance(metadata, dict):
                try:
                    directory = Path(capture_path).resolve()
                    frames = directory / "frames.npz"
                    if not frames.is_symlink() and frames.resolve().is_relative_to(directory):
                        shot["camera_replay"] = replay_manager.register(directory, metadata)
                except (OSError, ValueError, TypeError, OverflowError):
                    # Missing/expired recordings leave the measured shot available.
                    pass
        public["shots"].append(shot)
    return public


def list_sessions(log_dir: Path, *, now: datetime | None = None) -> list[dict]:
    """Return newest sessions first without exposing raw captures or file paths."""
    sessions = []
    for stem in _window_stems(log_dir, now=now, days=HISTORY_DAYS):
        try:
            session = get_session(log_dir, stem, now=now)
        except (OSError, ValueError):
            continue
        if session["shot_count"]:
            sessions.append({key: value for key, value in session.items() if key != "shots"})
    return sorted(
        sessions,
        key=lambda item: datetime.fromisoformat(item["started_at"]).timestamp(),
        reverse=True,
    )


def club_averages(
    log_dir: Path,
    club: str,
    *,
    profile_id: str | None = None,
    days: int = HISTORY_DAYS,
    now: datetime | None = None,
) -> dict:
    """Average Total and carry of a club's non-demo shots (one profile's if given), last ``days``

    A shot whose ball speed the OPS ball-track guard marked uncertain has a wrong carry, so it
    stays out of the averages and out of their counts.
    """
    totals = []
    carries = []
    for stem in _window_stems(log_dir, now=now, days=days):
        try:
            session = _cached_session(log_dir, stem, now=now, days=days)
        except (OSError, ValueError):
            continue
        for shot in session["shots"]:
            if shot["club"] != club:
                continue
            if profile_id is not None and shot.get("profile_id") != profile_id:
                continue
            if shot.get("ball_speed_status") == BALL_SPEED_UNCERTAIN:
                continue
            if shot["total_yards"] is not None:
                totals.append(shot["total_yards"])
            if shot["carry_spin_adjusted"] is not None:
                carries.append(shot["carry_spin_adjusted"])
    return {
        "club": club,
        "total_yards": sum(totals) / len(totals) if totals else None,
        "total_count": len(totals),
        "carry_yards": sum(carries) / len(carries) if carries else None,
        "carry_count": len(carries),
    }
