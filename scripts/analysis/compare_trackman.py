"""Pair OpenFlight shots with Trackman shots and emit a side-by-side
comparison CSV plus per-metric agreement statistics.

Usage::

    uv run python scripts/analysis/compare_trackman.py \\
        --openflight session_logs/session_20260506_*.jsonl \\
        --trackman ~/Downloads/TrackMan_2026-05-06.csv \\
        --output ~/openflight_sessions/comparison_20260506.csv \\
        --summary-json ~/openflight_sessions/comparison_20260506.json

Pairing strategy
----------------
Both systems number shots sequentially but their numbering doesn't
necessarily align (one of them may miss a shot, or you may have
hit a couple of "warm up" shots only one was watching). To be
robust:

1. Group both systems' shots by club.
2. Within each club, pair shots in chronological order.
3. Reject pairs whose ball-speed delta exceeds ``--ball-speed-tol``
   mph — flags missing/extra shots so you can fix the alignment by
   hand.

The output CSV has both systems' values + deltas + a
``match_quality`` column (``good``, ``ball_speed_mismatch``,
``unmatched_openflight``, ``unmatched_trackman``).

Trackman CSV handling
---------------------
Different Trackman exports (TPS, Range, TM4, iO / TrackMan Golf app and
web portal, the older "Normalized" export) use slightly different column
headers. We map known aliases to canonical names (an exact header match
wins over a substring match, and simulated / diff / index columns such as
``Spin Axis (Sim)`` or ``Ball Speed Diff`` are never picked); anything
unknown is preserved verbatim in the shot's ``raw`` dict.

Also handled: an optional ``sep=,`` / ``sep=;`` first line, doubled BOMs,
a units row under the header (``[mph]``, ``[deg]``, ``[yds]``, ``[ft]`` ...),
unit suffixes in the header (``Ball Speed (km/h)``), semicolon-separated
files with decimal commas (European locale), and direction strings such as
``2.3 R`` / ``L1.5`` / ``I-O 2.1``.

Units and sign conventions (normalised, right-handed golfer)
-------------------------------------------------------------
All values are compared in mph, yards, degrees and rpm; deltas are
OpenFlight minus Trackman.

* Launch direction (``launch_angle_horizontal``): + = right of the target line.
* Club path: + = in-to-out (right for a right-hander), - = out-to-in.
* Face angle (face to target): + = open (right of the target).
* Face to path: + = face open to the path.
* Spin axis: + = tilted right (fade/slice), - = left (draw/hook).
* Attack angle: + = up. Dynamic loft: + = loft.
* Carry side / curve: + = right.

These are TrackMan's published conventions for a right-handed golfer and
the conventions OpenFlight writes (``launch_monitor.Shot``). OpenFlight's
club data assumes a right-hander; left-handed sessions are not supported
by this tool. ``L``/``R`` suffixes map to -/+, ``O-I``/``I-O`` to -/+.

The Trackman curve class is derived from Trackman's spin axis with the
same thresholds OpenFlight uses (``openflight.dplane.curve_class``), so the
class comparison asks "does OpenFlight put the axis in the same bin".
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:  # The curve thresholds live in the package; keep the tool usable without it.
    from openflight.dplane import CURVE_CLASSES, curve_class as _of_curve_class
except ImportError:  # pragma: no cover - only outside the uv environment
    CURVE_CLASSES = ("hook", "draw", "straight", "fade", "slice")

    def _of_curve_class(axis: Optional[float]) -> Optional[str]:
        if axis is None or not math.isfinite(axis):
            return None
        if abs(axis) < 2.5:
            return "straight"
        if abs(axis) < 8.0:
            return "draw" if axis < 0 else "fade"
        return "hook" if axis < 0 else "slice"


AXIS_CLASSES = ("draw", "straight", "fade")
DIRECTION_CLASSES = ("left", "straight", "right")
_CURVE_TO_AXIS = {"hook": "draw", "draw": "draw", "straight": "straight",
                  "fade": "fade", "slice": "fade"}
_CURVE_TO_DIRECTION = {"hook": "left", "draw": "left", "straight": "straight",
                       "fade": "right", "slice": "right"}

# ---------------------------------------------------------------------------
# Trackman CSV column aliases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HeaderAlias:
    """Trackman header match rule."""

    text: str
    exact: bool = False


# Each canonical field maps to a list of header rules. Matching runs in two
# passes per field: first every alias as an exact (canonicalised) header,
# then every non-exact alias as a substring. Timestamp aliases intentionally
# include exact-only rules for generic names like "date" and "time" so
# "Last data Point - Time" does not steal the session timestamp from a real
# "Date" column.
_TM_ALIAS_CONFIG: Dict[str, List[str | HeaderAlias]] = {
    "ball_speed_mph":          ["ball speed", "ballspeed"],
    "club_speed_mph":          ["club speed", "clubspeed", "club head speed", "clubheadspeed"],
    "smash_factor":            ["smash factor", "smashfactor", HeaderAlias("smash", exact=True)],
    "launch_angle_vertical":   ["launch angle v", "launch angle (v)", "vertical launch",
                                "launch angle"],
    "launch_angle_horizontal": ["launch direction", "launch angle h", "horizontal launch",
                                "side angle", "azimuth", "launch dir"],
    "spin_rpm":                ["spin rate", "total spin", "spinrate"],
    "spin_axis_deg":           ["spin axis", "spinaxis", "spin tilt"],
    "club_path_deg":           ["club path", "clubpath", HeaderAlias("path", exact=True)],
    "attack_angle_deg":        ["attack angle", "angle of attack", "attackangle",
                                HeaderAlias("aoa", exact=True)],
    "face_angle_deg":          ["face angle", "face to target", "faceangle"],
    "face_to_path_deg":        ["face to path", "face-to-path", "facetopath"],
    "dynamic_loft_deg":        ["dynamic loft", "dyn. loft", "dyn loft", "dynloft"],
    "carry_yards":             ["carry flat - length", "carry distance", "carry"],
    "side_yards":              ["carry flat - side", "carry side", "carryside",
                                HeaderAlias("side", exact=True)],
    "curve_yards":             ["curve"],
    "club":                    ["club type", "club name", "club"],
    "shot_number":             ["shot number", "shotnumber", "shot no", "shot"],
    "timestamp":               [
        "date/time",
        "datetime",
        "timestamp",
        HeaderAlias("date", exact=True),
        HeaderAlias("time", exact=True),
    ],
}

# Substrings that disqualify a header in the substring pass, for every field
# ("Spin Axis (Sim)", "Ball Speed Diff", "Smash Index") and per field.
_TM_GLOBAL_EXCLUDE: Tuple[str, ...] = ("(sim)", " sim", "diff", "index")
_TM_FIELD_EXCLUDE: Dict[str, Tuple[str, ...]] = {
    "ball_speed_mph": ("carry", "land"),
    "spin_rpm": ("type",),
    "carry_yards": ("side", "angle", "time", "speed", "height", "total"),
    "side_yards": ("total", "est.", "max height", "last data", "angle"),
    "club": ("speed", "path", "plane", "loft", "face"),
    "launch_angle_vertical": ("land",),
    "shot_number": ("type", "tag"),
}


_TM_ALIASES: Dict[str, List[HeaderAlias]] = {
    field_name: [
        alias if isinstance(alias, HeaderAlias) else HeaderAlias(alias)
        for alias in aliases
    ]
    for field_name, aliases in _TM_ALIAS_CONFIG.items()
}

_UNIT_SUFFIX = re.compile(r"\s*[\[(]([^\])]*)[\])]\s*$")


def _canon_header(h: str) -> str:
    return h.strip().lower().replace("_", " ")


def _strip_unit_suffix(canon_h: str) -> str:
    """'ball speed (mph)' -> 'ball speed' (only a trailing unit token)."""
    m = _UNIT_SUFFIX.search(canon_h)
    if m and _normalize_unit(m.group(1)) is not None:
        return canon_h[:m.start()].strip()
    return canon_h


def _detect_units(headers: List[str]) -> Dict[str, str]:
    """Return a unit hint per canonical field by inspecting the header
    suffix. Used as a fallback for ball/club speed (mph vs km/h vs m/s)
    and carry (yards vs metres) when the export has no units row.
    """
    units: Dict[str, str] = {}
    for h in headers:
        ch = _canon_header(h)
        if "ball speed" in ch or "club speed" in ch or "club head speed" in ch:
            if "kph" in ch or "km/h" in ch or "kmh" in ch:
                units.setdefault("speed", "kph")
            elif "m/s" in ch:
                units.setdefault("speed", "mps")
            else:
                units.setdefault("speed", "mph")
        if "carry" in ch:
            if "metre" in ch or "meter" in ch or "(m)" in ch or " m " in ch:
                units.setdefault("carry", "m")
            else:
                units.setdefault("carry", "yards")
    return units


def _build_column_map(headers: List[str]) -> Dict[str, str]:
    """Map canonical-field-name → actual-header-string from the CSV.

    Exact matches (also after removing a trailing unit suffix such as
    ``(mph)``) win over substring matches; a header is assigned to at
    most one field.
    """
    col_map: Dict[str, str] = {}
    canon = [(_canon_header(h), h) for h in headers]
    used: set = set()

    def take(field_name: str, raw_h: str) -> None:
        col_map[field_name] = raw_h
        used.add(raw_h)

    # Pass 1: exact matches, for every field, before any substring match.
    for field_name, aliases in _TM_ALIASES.items():
        for alias in aliases:
            for canon_h, raw_h in canon:
                if raw_h in used:
                    continue
                if alias.text in (canon_h, _strip_unit_suffix(canon_h)):
                    take(field_name, raw_h)
                    break
            if field_name in col_map:
                break

    # Pass 2: substring matches for fields still missing.
    for field_name, aliases in _TM_ALIASES.items():
        if field_name in col_map:
            continue
        excludes = _TM_GLOBAL_EXCLUDE + _TM_FIELD_EXCLUDE.get(field_name, ())
        for alias in aliases:
            if alias.exact:
                continue
            for canon_h, raw_h in canon:
                if raw_h in used or alias.text not in canon_h:
                    continue
                if any(x in canon_h for x in excludes):
                    continue
                take(field_name, raw_h)
                break
            if field_name in col_map:
                break
    return col_map


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

_UNIT_ALIASES: Dict[str, str] = {
    "mph": "mph", "mi/h": "mph",
    "kph": "kph", "km/h": "kph", "kmh": "kph", "kmph": "kph",
    "m/s": "mps", "mps": "mps",
    "yds": "yds", "yd": "yds", "yards": "yds", "yard": "yds",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "ft": "ft", "feet": "ft", "foot": "ft",
    "in": "in", "inch": "in", "inches": "in",
    "cm": "cm", "mm": "mm",
    "deg": "deg", "°": "deg", "degrees": "deg", "degree": "deg",
    "rpm": "rpm",
    "s": "s", "sec": "s", "%": "%", "": "",
}

_SPEED_TO_MPH = {"mph": 1.0, "kph": 1.0 / 1.609344, "mps": 2.236936}
_DIST_TO_YARDS = {"yds": 1.0, "m": 1.093613, "ft": 1.0 / 3.0,
                  "in": 1.0 / 36.0, "cm": 0.01093613, "mm": 0.001093613}

# Canonical field -> unit kind it needs converted.
_FIELD_KIND = {
    "ball_speed_mph": "speed",
    "club_speed_mph": "speed",
    "carry_yards": "distance",
    "side_yards": "distance",
    "curve_yards": "distance",
}


def _normalize_unit(token: Optional[str]) -> Optional[str]:
    """'[mph]' / '(km/h)' / 'yds' -> canonical unit, None when unknown."""
    if token is None:
        return None
    t = str(token).strip().lower().strip("[]() ").strip()
    return _UNIT_ALIASES.get(t)


def _column_units(
    headers: List[str], units_row: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """Unit per header: the units row wins, then a header suffix."""
    units: Dict[str, str] = {}
    for h in headers:
        unit = None
        if units_row is not None:
            unit = _normalize_unit(units_row.get(h))
        if not unit:
            m = _UNIT_SUFFIX.search(_canon_header(h))
            unit = _normalize_unit(m.group(1)) if m else None
        if unit:
            units[h] = unit
    return units


# ---------------------------------------------------------------------------
# Club name normalization
# ---------------------------------------------------------------------------

# A loose normalizer: strip case/whitespace/dashes, accept "7i" / "7iron"
# / "7-iron" / "iron 7" etc. Both systems should agree after normalization.
_CLUB_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b(driver|drv|1w|1-wood)\b"),     "driver"),
    (re.compile(r"\b(\d)\s*-?\s*(wood|w)\b"),        r"\1-wood"),
    (re.compile(r"\b(\d)\s*-?\s*(hybrid|h|hy)\b"),   r"\1-hybrid"),
    (re.compile(r"\b(\d)\s*-?\s*(iron|i)\b"),        r"\1-iron"),
    (re.compile(r"\biron\s*-?\s*(\d)\b"),            r"\1-iron"),
    (re.compile(r"\bpitching\s*wedge\b|\bpw\b"),     "pw"),
    (re.compile(r"\bgap\s*wedge\b|\bgw\b"),          "gw"),
    (re.compile(r"\bsand\s*wedge\b|\bsw\b"),         "sw"),
    (re.compile(r"\blob\s*wedge\b|\blw\b"),          "lw"),
]


def normalize_club(raw: Optional[str]) -> str:
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    for pat, repl in _CLUB_PATTERNS:
        m = pat.search(s)
        if m:
            return pat.sub(repl, m.group(0)).strip()
    return s


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

@dataclass
class Shot:
    """Canonical per-shot record used by both sources (see the module
    docstring for units and sign conventions)."""
    source: str  # "of" or "tm"
    shot_number: Optional[int]
    timestamp: Optional[datetime]
    club: str
    ball_speed_mph: Optional[float] = None
    club_speed_mph: Optional[float] = None
    smash_factor: Optional[float] = None
    launch_angle_vertical: Optional[float] = None
    launch_angle_horizontal: Optional[float] = None
    launch_angle_horizontal_source: Optional[str] = None
    spin_rpm: Optional[float] = None
    spin_confidence: Optional[float] = None
    spin_quality: Optional[str] = None
    spin_snr: Optional[float] = None
    spin_candidate_rpm: Optional[float] = None
    spin_rejection_reason: Optional[str] = None
    carry_yards: Optional[float] = None
    side_yards: Optional[float] = None
    curve_yards: Optional[float] = None  # Trackman only (OpenFlight has no numeric curve)
    spin_axis_deg: Optional[float] = None
    spin_axis_source: Optional[str] = None
    club_path_deg: Optional[float] = None
    club_path_source: Optional[str] = None
    attack_angle_deg: Optional[float] = None
    attack_angle_source: Optional[str] = None
    face_angle_deg: Optional[float] = None
    face_to_path_deg: Optional[float] = None
    dynamic_loft_deg: Optional[float] = None
    curve_class: Optional[str] = None
    spin_axis_class: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


def _to_float(v: Any, decimal_comma: bool = False) -> Optional[float]:
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if math.isfinite(f) else None
    s = str(v).strip()
    if decimal_comma:
        s = s.replace(".", "").replace(",", ".") if "," in s else s
    else:
        s = s.replace(",", "")
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_NUM = r"([-+]?\d+(?:[.,]\d+)?)"
_DIR_PREFIX = re.compile(r"^\s*(L|R|I-O|O-I|IN-OUT|OUT-IN)\s*" + _NUM + r"\s*$", re.I)
_DIR_SUFFIX = re.compile(r"^\s*" + _NUM + r"\s*(L|R|I-O|O-I|IN-OUT|OUT-IN)\s*$", re.I)
_DIR_SIGN = {"l": -1.0, "r": 1.0, "o-i": -1.0, "out-in": -1.0,
             "i-o": 1.0, "in-out": 1.0}


def _to_signed(v: Any, decimal_comma: bool = False) -> Optional[float]:
    """Parse a signed angle/distance; accepts 'R 2.3', '1.5L', 'I-O 2.1'.

    L / O-I map to negative, R / I-O to positive (right-handed convention).
    """
    f = _to_float(v, decimal_comma)
    if f is not None or v is None:
        return f
    s = str(v)
    m = _DIR_PREFIX.match(s)
    if m:
        tag, num = m.group(1), m.group(2)
    else:
        m = _DIR_SUFFIX.match(s)
        if not m:
            return None
        num, tag = m.group(1), m.group(2)
    mag = _to_float(num.replace(",", ".") if not decimal_comma else num, decimal_comma)
    if mag is None:
        return None
    return _DIR_SIGN[tag.lower()] * abs(mag)


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _parse_timestamp(v: Any) -> Optional[datetime]:
    if v is None or v == "":
        return None
    s = str(v).strip()
    # Try ISO first (OpenFlight), then a few common Trackman formats.
    candidates = [
        None,  # let fromisoformat try
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%d/%m/%Y %H:%M:%S",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%Y-%m-%d %H:%M",
    ]
    for fmt in candidates:
        try:
            if fmt is None:
                ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                ts = datetime.strptime(s, fmt)
            # Compare naive local-ish times only; pairing just needs order.
            return ts.replace(tzinfo=None) if ts.tzinfo else ts
        except (ValueError, TypeError):
            continue
    return None


def _first_value(data: Dict[str, Any], keys: Iterable[Tuple[str, str]]
                 ) -> Tuple[Optional[float], Optional[str]]:
    """First finite value among ``(key, source_label)`` candidates."""
    for key, label in keys:
        val = _to_float(data.get(key))
        if val is not None:
            return val, label
    return None, None


def _class_or_none(value: Any, allowed: Iterable[str]) -> Optional[str]:
    return value if isinstance(value, str) and value in tuple(allowed) else None


def _capture_key(number: Any, timestamp: Any) -> Optional[Tuple[int, float]]:
    """Join key of a shot and its later ``club_data`` entry (as in
    ``openflight.session_history``): shot number plus impact timestamp."""
    n = _to_int(number)
    t = _to_float(timestamp)
    if n is None or n <= 0 or t is None or t <= 0:
        return None
    return n, t


# OpenFlight club-data fields merged from ``club_data`` entries.
_CLUB_DATA_NUMBERS = (
    "camera_club_path_deg", "camera_attack_angle_deg", "dynamic_loft_deg",
    "face_to_target_deg", "face_to_path_deg", "calculated_spin_axis_deg",
)


def _of_shot(data: Dict[str, Any], timestamp: Optional[datetime]) -> Shot:
    club_path, club_path_src = _first_value(data, (
        ("camera_club_path_deg", "camera"),
        ("experimental_fused_club_path_deg", "fused"),
    ))
    attack, attack_src = _first_value(data, (
        ("camera_attack_angle_deg", "camera"),
        ("experimental_fused_attack_angle_deg", "fused"),
    ))
    axis, axis_src = _first_value(data, (
        ("calculated_spin_axis_deg", "club_data"),
        ("spin_axis_deg", "dplane"),
    ))
    launch_h = _to_float(data.get("launch_angle_horizontal"))
    return Shot(
        source="of",
        shot_number=_to_int(data.get("shot_number")),
        timestamp=timestamp,
        club=normalize_club(data.get("club")),
        ball_speed_mph=_to_float(data.get("ball_speed_mph")),
        club_speed_mph=_to_float(data.get("club_speed_mph")),
        smash_factor=_to_float(data.get("smash_factor")),
        launch_angle_vertical=_to_float(data.get("launch_angle_vertical")),
        launch_angle_horizontal=launch_h,
        launch_angle_horizontal_source=(
            (data.get("launch_angle_horizontal_source") or "unknown")
            if launch_h is not None else None),
        spin_rpm=_to_float(data.get("spin_rpm")),
        spin_confidence=_to_float(data.get("spin_confidence")),
        spin_quality=data.get("spin_quality"),
        spin_snr=_to_float(data.get("spin_snr")),
        spin_candidate_rpm=_to_float(data.get("spin_candidate_rpm")),
        spin_rejection_reason=data.get("spin_rejection_reason"),
        carry_yards=_to_float(
            data.get("carry_spin_adjusted")
            or data.get("estimated_carry_yards")),
        side_yards=_to_float(data.get("carry_lateral_yards")),
        spin_axis_deg=axis,
        spin_axis_source=axis_src,
        club_path_deg=club_path,
        club_path_source=club_path_src,
        attack_angle_deg=attack,
        attack_angle_source=attack_src,
        face_angle_deg=_to_float(data.get("face_to_target_deg")),
        face_to_path_deg=_to_float(data.get("face_to_path_deg")),
        dynamic_loft_deg=_to_float(data.get("dynamic_loft_deg")),
        curve_class=_class_or_none(data.get("curve_class"), CURVE_CLASSES),
        spin_axis_class=_class_or_none(data.get("spin_axis_class"), AXIS_CLASSES),
        raw=data,
    )


def load_openflight(path: Path) -> List[Shot]:
    """Load ``shot_detected`` entries from an OpenFlight JSONL session log.

    The camera club data (path, attack angle, face, dynamic loft, calculated
    spin axis and its class) is computed after the shot is published and
    logged as a separate ``club_data`` entry; it is merged into its shot by
    shot number + impact timestamp. Ambiguous duplicate ``club_data``
    entries are ignored. Shots marked by a later ``shot_deleted`` entry and
    demo shots are dropped.
    """
    records: List[Tuple[Dict[str, Any], Optional[datetime]]] = []
    club_data: Dict[Tuple[int, float], Optional[Dict[str, Any]]] = {}
    deleted: set = set()
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if etype == "shot_deleted":
                deleted.add(entry.get("shot_timestamp"))
                continue
            if etype == "club_data":
                key = _capture_key(entry.get("shot_number"), entry.get("shot_timestamp"))
                fields = entry.get("fields")
                if key is not None and isinstance(fields, dict):
                    club_data[key] = None if key in club_data else {
                        **fields,
                        "club_data_status": entry.get("status"),
                        "club_data_mode": entry.get("mode", "show"),
                    }
                continue
            if etype != "shot_detected":
                continue
            data = dict(entry.get("data", entry))  # tolerate flat or nested
            if data.get("demo") is True:
                continue
            # OpenFlight uses ``ts`` at the entry level. Older logs
            # may use ``timestamp`` instead. Try both.
            ts = _parse_timestamp(
                entry.get("ts")
                or entry.get("timestamp")
                or data.get("timestamp"))
            records.append((data, ts))

    shots: List[Shot] = []
    for data, ts in records:
        if data.get("timestamp") in deleted and data.get("timestamp") is not None:
            continue
        key = _capture_key(data.get("shot_number"), data.get("impact_timestamp"))
        extra = club_data.get(key) if key is not None else None
        if extra:
            for name in _CLUB_DATA_NUMBERS:
                if _to_float(extra.get(name)) is not None:
                    data[name] = extra[name]
            if extra.get("spin_axis_class") is not None:
                data["spin_axis_class"] = extra["spin_axis_class"]
            data["club_data_status"] = extra.get("club_data_status")
            data["club_data_mode"] = extra.get("club_data_mode")
        shots.append(_of_shot(data, ts))
    return shots


def _looks_like_units_row(row: Dict[str, Any]) -> bool:
    """Trackman's "Normalized" exports include a row immediately under
    the header containing units in brackets, e.g. ``[mph]``, ``[deg]``,
    ``[rpm]``. The Date/Club/etc. cells in this row are blank. Detect
    by looking for any cell wrapped in ``[...]`` and no plausible
    numeric values."""
    bracketed = 0
    has_plausible_value = False
    for v in row.values():
        if v is None:
            continue
        if isinstance(v, list):  # DictReader's restkey overflow
            continue
        s = str(v).strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            bracketed += 1
        elif _to_float(s) is not None:
            # If anything looks like a real number, it's not a units row.
            has_plausible_value = True
    return bracketed >= 1 and not has_plausible_value


def _sniff_delimiter(line: str) -> str:
    return ";" if line.count(";") > line.count(",") else ","


@dataclass
class TrackmanLoadInfo:
    """What ``load_trackman`` decided about a file (printed by the CLI)."""
    delimiter: str = ","
    decimal_comma: bool = False
    column_map: Dict[str, str] = field(default_factory=dict)
    units: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


def load_trackman(path: Path, info: Optional[TrackmanLoadInfo] = None) -> List[Shot]:
    """Load shots from a Trackman CSV export. Tolerant of header
    variations (TPS / Range / TM4 / iO / Normalized export). Pass a
    ``TrackmanLoadInfo`` to get the column map and unit decisions back."""
    info = info if info is not None else TrackmanLoadInfo()
    shots: List[Shot] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        # Strip an optional ``sep=,`` Excel-hint preamble (Trackman
        # Normalized exports include one as the first line, which would
        # otherwise be parsed as the header row).
        # Note: utf-8-sig only strips ONE BOM and Trackman exports have
        # been seen with TWO, so strip any leading BOMs explicitly.
        first_pos = fh.tell()
        first_line = fh.readline().lstrip("﻿").lstrip()
        if first_line.lower().startswith("sep="):
            sep = first_line.strip()[4:5]
            delimiter = sep if sep else ","
        else:
            delimiter = _sniff_delimiter(first_line)
            fh.seek(first_pos)
        info.delimiter = delimiter
        info.decimal_comma = delimiter == ";"
        reader = csv.DictReader(fh, delimiter=delimiter)
        if reader.fieldnames is None:
            return shots
        # Defensively strip BOMs from header field names too.
        cleaned_headers = [h.lstrip("﻿").strip()
                           for h in reader.fieldnames]
        reader.fieldnames = cleaned_headers
        col_map = _build_column_map(cleaned_headers)
        info.column_map = dict(col_map)
        legacy_units = _detect_units(cleaned_headers)
        rows = list(reader)

    units_row = None
    if rows and _looks_like_units_row(rows[0]):
        units_row = rows[0]
    col_units = _column_units(cleaned_headers, units_row)
    dc = info.decimal_comma

    # Resolve one conversion factor per canonical field.
    factors: Dict[str, float] = {}
    for canon, kind in _FIELD_KIND.items():
        col = col_map.get(canon)
        if col is None:
            continue
        unit = col_units.get(col)
        if unit is None:
            if kind == "speed":
                unit = legacy_units.get("speed", "mph")
            elif canon == "carry_yards":
                unit = "m" if legacy_units.get("carry") == "m" else "yds"
            else:
                unit = "yds"
                info.warnings.append(
                    f"no unit for '{col}', assuming yards")
        table = _SPEED_TO_MPH if kind == "speed" else _DIST_TO_YARDS
        if unit not in table:
            info.warnings.append(
                f"unit '{unit}' of '{col}' is not a {kind} unit; values dropped")
            factors[canon] = float("nan")
        else:
            factors[canon] = table[unit]
        info.units[canon] = unit

    def _get(row: Dict[str, str], canon: str) -> Any:
        col = col_map.get(canon)
        return row.get(col) if col else None

    def num(row: Dict[str, str], canon: str, signed: bool = False) -> Optional[float]:
        raw_v = _get(row, canon)
        val = _to_signed(raw_v, dc) if signed else _to_float(raw_v, dc)
        if val is None:
            return None
        factor = factors.get(canon, 1.0)
        if math.isnan(factor):
            return None
        return val * factor

    for row in rows:
        # Skip Trackman's units row (e.g. row of "[mph]", "[deg]"
        # under the header in Normalized exports).
        if _looks_like_units_row(row):
            continue
        axis = num(row, "spin_axis_deg", signed=True)
        tm_curve = _of_curve_class(axis) if axis is not None else None
        shots.append(Shot(
            source="tm",
            shot_number=_to_int(_get(row, "shot_number")),
            timestamp=_parse_timestamp(_get(row, "timestamp")),
            club=normalize_club(_get(row, "club")),
            ball_speed_mph=num(row, "ball_speed_mph"),
            club_speed_mph=num(row, "club_speed_mph"),
            smash_factor=num(row, "smash_factor"),
            launch_angle_vertical=num(row, "launch_angle_vertical"),
            launch_angle_horizontal=num(row, "launch_angle_horizontal", signed=True),
            spin_rpm=num(row, "spin_rpm"),
            carry_yards=num(row, "carry_yards"),
            side_yards=num(row, "side_yards", signed=True),
            curve_yards=num(row, "curve_yards", signed=True),
            spin_axis_deg=axis,
            club_path_deg=num(row, "club_path_deg", signed=True),
            attack_angle_deg=num(row, "attack_angle_deg"),
            face_angle_deg=num(row, "face_angle_deg", signed=True),
            face_to_path_deg=num(row, "face_to_path_deg", signed=True),
            dynamic_loft_deg=num(row, "dynamic_loft_deg"),
            curve_class=tm_curve,
            spin_axis_class=_CURVE_TO_AXIS.get(tm_curve) if tm_curve else None,
            raw=dict(row),
        ))
    return shots


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    of: Optional[Shot]
    tm: Optional[Shot]
    match_quality: str  # "good" | "ball_speed_mismatch" | "unmatched_openflight" | "unmatched_trackman"
    notes: str = ""


def _sort_for_pairing(shots: Iterable[Shot]) -> List[Shot]:
    """Order shots for chronological pairing. Prefer timestamp; fall
    back to shot_number; preserve input order otherwise (stable)."""
    def key(s: Shot, idx: int) -> Tuple:
        ts = s.timestamp.timestamp() if s.timestamp else float("inf")
        sn = s.shot_number if s.shot_number is not None else float("inf")
        return (ts, sn, idx)
    indexed = list(enumerate(shots))
    indexed.sort(key=lambda item: key(item[1], item[0]))
    return [s for _, s in indexed]


def pair_shots(
    of_shots: List[Shot],
    tm_shots: List[Shot],
    ball_speed_tol_mph: float = 5.0,
    club_filter: Optional[List[str]] = None,
) -> List[Pair]:
    """Pair OpenFlight ↔ Trackman shots in chronological order within
    each club. Pairs whose ball-speed delta exceeds the tolerance are
    flagged ``ball_speed_mismatch`` (still emitted so you can fix
    alignment by hand).
    """
    pairs: List[Pair] = []

    of_by_club: Dict[str, List[Shot]] = {}
    tm_by_club: Dict[str, List[Shot]] = {}
    for s in of_shots:
        of_by_club.setdefault(s.club, []).append(s)
    for s in tm_shots:
        tm_by_club.setdefault(s.club, []).append(s)

    clubs = set(of_by_club) | set(tm_by_club)
    if club_filter:
        wanted = {normalize_club(c) for c in club_filter}
        clubs &= wanted

    # Stable, friendly ordering — driver first, then irons by number, then wedges.
    def club_sort_key(c: str) -> Tuple:
        if c == "driver":
            return (0, 0, c)
        m = re.match(r"(\d+)-(wood|hybrid|iron)", c)
        if m:
            ord_map = {"wood": 1, "hybrid": 2, "iron": 3}
            return (ord_map[m.group(2)], int(m.group(1)), c)
        if c in ("pw", "gw", "sw", "lw"):
            return (4, ["pw","gw","sw","lw"].index(c), c)
        return (5, 0, c)

    for club in sorted(clubs, key=club_sort_key):
        of_seq = _sort_for_pairing(of_by_club.get(club, []))
        tm_seq = _sort_for_pairing(tm_by_club.get(club, []))
        i = j = 0
        while i < len(of_seq) and j < len(tm_seq):
            of_s, tm_s = of_seq[i], tm_seq[j]
            mq = "good"
            notes = ""
            if (of_s.ball_speed_mph is not None
                    and tm_s.ball_speed_mph is not None):
                d = abs(of_s.ball_speed_mph - tm_s.ball_speed_mph)
                if d > ball_speed_tol_mph:
                    mq = "ball_speed_mismatch"
                    notes = (f"ball-speed delta {d:.1f} mph "
                             f"exceeds tol {ball_speed_tol_mph} mph; "
                             f"check pairing manually")
            pairs.append(Pair(of=of_s, tm=tm_s,
                              match_quality=mq, notes=notes))
            i += 1
            j += 1

        # Trailing unmatched shots from either side — keep so the user
        # can see where alignment ran off.
        for k in range(i, len(of_seq)):
            pairs.append(Pair(of=of_seq[k], tm=None,
                              match_quality="unmatched_openflight",
                              notes="no Trackman shot for this club position"))
        for k in range(j, len(tm_seq)):
            pairs.append(Pair(of=None, tm=tm_seq[k],
                              match_quality="unmatched_trackman",
                              notes="no OpenFlight shot for this club position"))

    return pairs


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    """A numeric metric both systems report (``Shot`` attribute ``key``)."""
    key: str
    column: str  # output CSV prefix: <column>_of / _tm / _delta
    label: str
    unit: str
    source_key: Optional[str] = None  # OpenFlight ``Shot`` attribute naming the source


NUMERIC_METRICS: Tuple[Metric, ...] = (
    Metric("ball_speed_mph", "ball_speed", "ball speed", "mph"),
    Metric("club_speed_mph", "club_speed", "club speed", "mph"),
    Metric("smash_factor", "smash", "smash", ""),
    Metric("launch_angle_vertical", "launch_v", "launch V", "deg"),
    Metric("launch_angle_horizontal", "launch_h", "launch dir", "deg",
           "launch_angle_horizontal_source"),
    Metric("spin_rpm", "spin", "spin rate", "rpm"),
    Metric("spin_axis_deg", "spin_axis", "spin axis", "deg", "spin_axis_source"),
    Metric("club_path_deg", "club_path", "club path", "deg", "club_path_source"),
    Metric("attack_angle_deg", "attack", "attack angle", "deg", "attack_angle_source"),
    Metric("face_angle_deg", "face_angle", "face angle", "deg"),
    Metric("face_to_path_deg", "face_to_path", "face to path", "deg"),
    Metric("dynamic_loft_deg", "dyn_loft", "dyn. loft", "deg"),
    Metric("carry_yards", "carry", "carry", "yds"),
    Metric("side_yards", "side", "carry side", "yds"),
)


@dataclass(frozen=True)
class ClassMetric:
    """A categorical comparison: OpenFlight label vs Trackman-derived label."""
    name: str
    labels: Tuple[str, ...]
    of_key: str
    tm_key: str
    collapse: Optional[Dict[str, str]] = None  # applied to both sides


CLASS_METRICS: Tuple[ClassMetric, ...] = (
    ClassMetric("curve_class", tuple(CURVE_CLASSES), "curve_class", "curve_class"),
    ClassMetric("curve_direction", DIRECTION_CLASSES, "curve_class", "curve_class",
                _CURVE_TO_DIRECTION),
    ClassMetric("spin_axis_class", AXIS_CLASSES, "spin_axis_class", "spin_axis_class"),
)


def _class_value(shot: Optional[Shot], cm: ClassMetric, key: str) -> Optional[str]:
    if shot is None:
        return None
    value = getattr(shot, key)
    if value is None:
        return None
    if cm.collapse is not None:
        value = cm.collapse.get(value)
    return value if value in cm.labels else None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _output_fields() -> List[str]:
    fields = ["shot_number_of", "shot_number_tm", "timestamp_of", "timestamp_tm", "club"]
    for m in NUMERIC_METRICS:
        fields += [f"{m.column}_of", f"{m.column}_tm", f"{m.column}_delta"]
        if m.source_key:
            fields.append(f"{m.column}_source_of")
    fields += ["curve_tm"]
    for cm in CLASS_METRICS:
        if cm.collapse is None:
            fields += [f"{cm.name}_of", f"{cm.name}_tm"]
    fields += ["spin_candidate_of", "spin_confidence_of", "spin_quality_of",
               "spin_snr_of", "spin_rejection_of", "club_data_status_of",
               "match_quality", "notes"]
    return fields


_OUTPUT_FIELDS = _output_fields()


def _delta(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(a - b, 3)


def _row(pair: Pair) -> Dict[str, Any]:
    of, tm = pair.of, pair.tm

    def f(val: Any) -> Any:
        if isinstance(val, float):
            return round(val, 3)
        return val

    def attr(shot: Optional[Shot], name: str) -> Any:
        return getattr(shot, name) if shot is not None else None

    row: Dict[str, Any] = {
        "shot_number_of": attr(of, "shot_number"),
        "shot_number_tm": attr(tm, "shot_number"),
        "timestamp_of": of.timestamp.isoformat() if of and of.timestamp else None,
        "timestamp_tm": tm.timestamp.isoformat() if tm and tm.timestamp else None,
        "club": (of or tm).club if (of or tm) else "",
    }
    for m in NUMERIC_METRICS:
        a, b = attr(of, m.key), attr(tm, m.key)
        row[f"{m.column}_of"] = f(a)
        row[f"{m.column}_tm"] = f(b)
        row[f"{m.column}_delta"] = _delta(a, b)
        if m.source_key:
            row[f"{m.column}_source_of"] = attr(of, m.source_key)
    row["curve_tm"] = f(attr(tm, "curve_yards"))
    for cm in CLASS_METRICS:
        if cm.collapse is None:
            row[f"{cm.name}_of"] = attr(of, cm.of_key)
            row[f"{cm.name}_tm"] = attr(tm, cm.tm_key)
    row.update({
        "spin_candidate_of": f(attr(of, "spin_candidate_rpm")),
        "spin_confidence_of": f(attr(of, "spin_confidence")),
        "spin_quality_of": attr(of, "spin_quality"),
        "spin_snr_of": f(attr(of, "spin_snr")),
        "spin_rejection_of": attr(of, "spin_rejection_reason"),
        "club_data_status_of": of.raw.get("club_data_status") if of else None,
        "match_quality": pair.match_quality,
        "notes": pair.notes,
    })
    return row


def write_comparison_csv(pairs: List[Pair], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_OUTPUT_FIELDS)
        writer.writeheader()
        for p in pairs:
            writer.writerow(_row(p))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

_DELTA_LABELS = [(f"{m.column}_delta", m.label, m.unit or "-") for m in NUMERIC_METRICS]


def _fit_linear(x: List[float], y: List[float]) -> Tuple[float, float, float]:
    """Least-squares fit y = slope*x + intercept. Returns
    (slope, intercept, residual_stddev). Uses pure stdlib so the
    comparison tool stays free of a numpy dependency."""
    n = len(x)
    if n < 2:
        return 1.0, 0.0, 0.0
    sx = sum(x)
    sy = sum(y)
    sxx = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 1.0, 0.0, 0.0
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    resid = [yi - (slope * xi + intercept) for xi, yi in zip(x, y)]
    mean_r = sum(resid) / n
    sd = (sum((r - mean_r) ** 2 for r in resid) / n) ** 0.5
    return slope, intercept, sd


def _fit_proportional(x: List[float], y: List[float]) -> Tuple[float, float]:
    """Least-squares fit y = slope*x (no intercept). Returns
    (slope, residual_stddev)."""
    n = len(x)
    if n < 1:
        return 1.0, 0.0
    sxx = sum(xi * xi for xi in x)
    sxy = sum(xi * yi for xi, yi in zip(x, y))
    if sxx == 0:
        return 1.0, 0.0
    slope = sxy / sxx
    resid = [yi - slope * xi for xi, yi in zip(x, y)]
    mean_r = sum(resid) / n
    sd = (sum((r - mean_r) ** 2 for r in resid) / n) ** 0.5
    return slope, sd


def _correlation(x: List[float], y: List[float]) -> float:
    """Return Pearson correlation, or 0 when either side is constant."""
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    numerator = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom_x = sum((xi - mx) ** 2 for xi in x)
    denom_y = sum((yi - my) ** 2 for yi in y)
    denom = (denom_x * denom_y) ** 0.5
    if denom == 0:
        return 0.0
    return numerator / denom


def _good(pairs: Iterable[Pair]) -> List[Pair]:
    return [p for p in pairs
            if p.match_quality == "good" and p.of is not None and p.tm is not None]


def _metric_pairs(
    pairs: List[Pair],
    field_name: str,
) -> Tuple[List[float], List[float]]:
    """Collect OpenFlight/Trackman values for one metric from good pairs."""
    of: List[float] = []
    tm: List[float] = []
    for p in _good(pairs):
        a = getattr(p.of, field_name)
        b = getattr(p.tm, field_name)
        if a is None or b is None:
            continue
        of.append(a)
        tm.append(b)
    return of, tm


def _rmse(values: List[float]) -> float:
    if not values:
        return 0.0
    return (sum(v * v for v in values) / len(values)) ** 0.5


def metric_stats(of: List[float], tm: List[float]) -> Dict[str, Any]:
    """Agreement of paired values; deltas are OF - TM.

    ``slope``/``intercept`` fit ``OF = slope * TM + intercept``: a slope
    below 1 means OpenFlight compresses the real spread. ``r`` is Pearson.
    Slope, intercept and r are None below 3 pairs or without TM spread.
    """
    n = len(of)
    out: Dict[str, Any] = {"n": n, "bias": None, "sd": None, "mae": None,
                           "rmse": None, "slope": None, "intercept": None,
                           "r": None}
    if n == 0:
        return out
    deltas = [a - b for a, b in zip(of, tm)]
    out["bias"] = statistics.fmean(deltas)
    out["sd"] = statistics.stdev(deltas) if n >= 2 else None
    out["mae"] = statistics.fmean(abs(d) for d in deltas)
    out["rmse"] = _rmse(deltas)
    if n >= 3 and len(set(tm)) > 1:
        slope, intercept, _ = _fit_linear(tm, of)
        out["slope"] = slope
        out["intercept"] = intercept
        out["r"] = _correlation(tm, of)
    return out


def compute_metric_stats(pairs: List[Pair]) -> Dict[str, Dict[str, Any]]:
    """Per numeric metric: overall, per club and (where OpenFlight names
    a source) per source statistics over the good pairs."""
    good = _good(pairs)
    result: Dict[str, Dict[str, Any]] = {}
    for m in NUMERIC_METRICS:
        entry: Dict[str, Any] = {"unit": m.unit, "label": m.label}
        entry["all"] = metric_stats(*_metric_pairs(good, m.key))
        by_club: Dict[str, Any] = {}
        for club in sorted({p.of.club for p in good}):
            sub = [p for p in good if p.of.club == club]
            st = metric_stats(*_metric_pairs(sub, m.key))
            if st["n"]:
                by_club[club] = st
        entry["by_club"] = by_club
        if m.source_key:
            by_src: Dict[str, Any] = {}
            for src in sorted({str(getattr(p.of, m.source_key)) for p in good
                               if getattr(p.of, m.source_key) is not None}):
                sub = [p for p in good if str(getattr(p.of, m.source_key)) == src]
                st = metric_stats(*_metric_pairs(sub, m.key))
                if st["n"]:
                    by_src[src] = st
            entry["by_source"] = by_src
        result[m.column] = entry
    return result


def class_agreement(of_labels: List[str], tm_labels: List[str],
                    labels: Iterable[str]) -> Dict[str, Any]:
    """Agreement of two label lists against two null models.

    * ``null_majority``: always say Trackman's most frequent class (the
      score a constant "fade" answer gets on a fade-heavy session).
    * ``null_chance``: expected agreement of two independent raters with
      these marginals (sum p_of(c) * p_tm(c)); ``kappa`` is Cohen's kappa
      against it.
    """
    labels = tuple(labels)
    n = len(of_labels)
    confusion = {a: {b: 0 for b in labels} for a in labels}  # [of][tm]
    for a, b in zip(of_labels, tm_labels):
        confusion[a][b] += 1
    out: Dict[str, Any] = {"n": n, "agreement": None, "null_majority": None,
                           "null_majority_class": None, "null_chance": None,
                           "kappa": None, "confusion_of_by_tm": confusion}
    if n == 0:
        return out
    agree = sum(1 for a, b in zip(of_labels, tm_labels) if a == b) / n
    tm_counts = Counter(tm_labels)
    of_counts = Counter(of_labels)
    major_cls, major_n = tm_counts.most_common(1)[0]
    chance = sum((of_counts[c] / n) * (tm_counts[c] / n) for c in labels)
    out.update({
        "agreement": agree,
        "null_majority": major_n / n,
        "null_majority_class": major_cls,
        "null_chance": chance,
        "kappa": (agree - chance) / (1 - chance) if chance < 1 else None,
    })
    return out


def compute_class_stats(pairs: List[Pair]) -> Dict[str, Dict[str, Any]]:
    good = _good(pairs)
    result: Dict[str, Dict[str, Any]] = {}
    for cm in CLASS_METRICS:
        of_l: List[str] = []
        tm_l: List[str] = []
        for p in good:
            a = _class_value(p.of, cm, cm.of_key)
            b = _class_value(p.tm, cm, cm.tm_key)
            if a is None or b is None:
                continue
            of_l.append(a)
            tm_l.append(b)
        result[cm.name] = {"labels": list(cm.labels),
                           **class_agreement(of_l, tm_l, cm.labels)}
    return result


def coverage(pairs: List[Pair]) -> Dict[str, Dict[str, int]]:
    """How many good pairs have a value on each side, per metric."""
    good = _good(pairs)
    out: Dict[str, Dict[str, int]] = {}
    for m in NUMERIC_METRICS:
        out[m.column] = {
            "pairs": len(good),
            "of": sum(1 for p in good if getattr(p.of, m.key) is not None),
            "tm": sum(1 for p in good if getattr(p.tm, m.key) is not None),
        }
    return out


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def print_ball_speed_calibration(pairs: List[Pair]) -> None:
    """Fit ball-speed-correction models from OF/TM pairs and print
    recommended calibration constants. Lets the user decide whether to
    wire them into the live processor.

    Two models, both reported:

    * Slope+offset: ``corrected_of = slope * raw_of + offset`` (best fit
      when there's both a multiplicative and additive systematic).
    * Slope-only:   ``corrected_of = slope * raw_of`` (one knob, simpler
      to maintain; gives up a small amount of residual fit quality).

    The least-squares regressors run on the existing good-pair set.
    For applying the correction, the inverse mapping is what matters:
    we want ``corrected_of`` to predict ``tm`` from ``raw_of``. So the
    regression is ``tm = slope*of + offset`` and the recommended live
    correction is exactly those constants.
    """
    of, tm = _metric_pairs(pairs, "ball_speed_mph")

    if len(of) < 3:
        print()
        print("  (not enough good ball-speed pairs to recommend a "
              f"calibration — need >=3, got {len(of)})")
        return

    slope_lin, offset_lin, sd_lin = _fit_linear(of, tm)
    slope_prop, sd_prop = _fit_proportional(of, tm)
    raw_resid_sd = (
        sum((b - a) ** 2 for a, b in zip(of, tm)) / len(of)
    ) ** 0.5

    print()
    print("=" * 72)
    print("  BALL-SPEED CALIBRATION RECOMMENDATION")
    print("=" * 72)
    print(f"  Fit on {len(of)} good ball-speed pairs.")
    print()
    print(f"  Uncorrected residual (TM - OF) stddev:    {raw_resid_sd:6.3f} mph")
    print()
    print( "  Two-parameter model (slope + offset):")
    print(f"    corrected_of_mph = {slope_lin:.5f} * raw_of_mph "
          f"{offset_lin:+.4f}")
    print(f"    residual stddev: {sd_lin:6.3f} mph")
    print()
    print( "  One-parameter model (slope only):")
    print(f"    corrected_of_mph = {slope_prop:.5f} * raw_of_mph")
    print(f"    residual stddev: {sd_prop:6.3f} mph")
    print()
    print("  Caveats:")
    print("    - Calibration depends on the radar unit and mounting")
    print("      geometry. Re-fit per setup, not per session.")
    print("    - One session of ~20 shots is a useful start but light")
    print("      on data — collect more before wiring this into the")
    print("      live processor as a tuned default.")
    print("    - Apply the correction at the FFT-peak-to-mph step in")
    print("      rolling_buffer/processor.py, not at the UI layer, so")
    print("      that downstream metrics (smash, carry, etc.) benefit.")
    print("=" * 72)


def print_launch_angle_calibration(pairs: List[Pair]) -> None:
    """Print Trackman-based diagnostics for vertical and horizontal launch.

    Angle calibration is less straightforward than ball speed. A useful
    report needs to show both the simple bias and whether OpenFlight has
    shot-to-shot correlation with Trackman. Low correlation means an offset
    may improve average error but should not be treated as a reliable
    measured-angle calibration.
    """
    axes = [
        ("Vertical", "launch_angle_vertical", "launch_v"),
        ("Horizontal", "launch_angle_horizontal", "launch_h"),
    ]

    print()
    print("=" * 72)
    print("  LAUNCH-ANGLE CALIBRATION DIAGNOSTICS")
    print("=" * 72)

    any_axis = False
    for label, field_name, short_name in axes:
        of, tm = _metric_pairs(pairs, field_name)
        if len(of) < 3:
            print(
                f"  {label}: not enough good paired values "
                f"(need >=3, got {len(of)})"
            )
            continue

        any_axis = True
        deltas = [a - b for a, b in zip(of, tm)]
        bias = statistics.fmean(deltas)
        mae = statistics.fmean(abs(d) for d in deltas)
        raw_rmse = _rmse(deltas)

        offset = -bias
        offset_resid = [b - (a + offset) for a, b in zip(of, tm)]
        offset_rmse = _rmse(offset_resid)

        slope, intercept, fit_sd = _fit_linear(of, tm)
        corr = _correlation(of, tm)

        print()
        print(f"  {label} launch ({short_name}) — {len(of)} good paired values")
        print(f"    raw OF-TM bias:         {bias:+6.2f} deg")
        print(f"    raw MAE / RMSE:         {mae:6.2f} / {raw_rmse:6.2f} deg")
        print(f"    offset-only correction: corrected = raw {offset:+.2f} deg")
        print(f"    offset-only RMSE:       {offset_rmse:6.2f} deg")
        print(
            f"    linear correction:      corrected = {slope:.4f} * raw "
            f"{intercept:+.3f}"
        )
        print(f"    linear residual stddev: {fit_sd:6.2f} deg")
        print(f"    OF↔TM correlation:      {corr:+6.2f}")
        if abs(corr) < 0.5:
            print(
                "    note: weak shot-to-shot correlation; prefer using this "
                "to tune gating/fallbacks before applying a live correction."
            )

    if not any_axis:
        print("  (no launch-angle calibration available from this comparison)")

    print("=" * 72)


def _fmt(v: Optional[float], spec: str) -> str:
    return format(v, spec) if v is not None else "-"


def _stats_line(label: str, unit: str, st: Dict[str, Any]) -> str:
    return (f"    {label:<16} {unit:<4} {st['n']:>4}  {_fmt(st['bias'], '+8.2f'):>8}  "
            f"{_fmt(st['mae'], '7.2f'):>7}  {_fmt(st['rmse'], '7.2f'):>7}  "
            f"{_fmt(st['sd'], '7.2f'):>7}  {_fmt(st['slope'], '6.2f'):>6}  "
            f"{_fmt(st['r'], '+6.2f'):>6}")


def print_conventions() -> None:
    print()
    print("  Conventions: Δ = OpenFlight − Trackman; mph / yds / deg / rpm;")
    print("  right-handed: launch dir, club path (in-to-out), face angle (open),")
    print("  face to path (open), spin axis (fade), side: + = right.")
    print("  slope/r: fit OF = slope · TM + b over the good pairs.")


def print_metric_agreement(pairs: List[Pair]) -> None:
    stats = compute_metric_stats(pairs)
    cov = coverage(pairs)
    header = (f"    {'metric':<16} {'unit':<4} {'n':>4}  {'bias':>8}  {'MAE':>7}  "
              f"{'RMSE':>7}  {'sd':>7}  {'slope':>6}  {'r':>6}")
    print()
    print("=" * 72)
    print("  METRIC AGREEMENT (good pairs)")
    print("=" * 72)
    print_conventions()
    print()
    print("  All clubs:")
    print(header)
    for m in NUMERIC_METRICS:
        st = stats[m.column]["all"]
        if st["n"]:
            print(_stats_line(m.label, m.unit, st))
    print()
    print("  Coverage (good pairs with a value, OF / TM):")
    for m in NUMERIC_METRICS:
        c = cov[m.column]
        print(f"    {m.label:<16} {c['of']:>4} / {c['tm']:<4} of {c['pairs']}")
    clubs = sorted({club for e in stats.values() for club in e["by_club"]})
    for club in clubs:
        print()
        print(f"  {club}:")
        print(header)
        for m in NUMERIC_METRICS:
            st = stats[m.column]["by_club"].get(club)
            if st:
                print(_stats_line(m.label, m.unit, st))
    for m in NUMERIC_METRICS:
        by_src = stats[m.column].get("by_source") or {}
        if len(by_src) < 1:
            continue
        print()
        print(f"  {m.label} by OpenFlight source:")
        print(header)
        for src, st in by_src.items():
            print(_stats_line(src, m.unit, st))
    print("=" * 72)


def print_class_agreement(pairs: List[Pair]) -> None:
    stats = compute_class_stats(pairs)
    print()
    print("=" * 72)
    print("  CLASS AGREEMENT (Trackman class from Trackman spin axis,")
    print("  same thresholds as OpenFlight's curve_class)")
    print("=" * 72)
    for name, st in stats.items():
        print()
        if not st["n"]:
            print(f"  {name}: no pairs with a class on both sides")
            continue
        print(f"  {name} — n={st['n']}")
        print(f"    agreement:        {st['agreement']:.2f}")
        print(f"    null (always '{st['null_majority_class']}'): "
              f"{st['null_majority']:.2f}")
        print(f"    null (chance):    {st['null_chance']:.2f}")
        print(f"    Cohen's kappa:    {_fmt(st['kappa'], '+.2f')}")
        labels = st["labels"]
        print("    confusion (rows OF, cols TM): " + " ".join(f"{lb:>8}" for lb in labels))
        for a in labels:
            row = st["confusion_of_by_tm"][a]
            print(f"    {a:>29}: " + " ".join(f"{row[b]:>8}" for b in labels))
    print("=" * 72)


def _format_metric_summary_line(label: str, vals: List[float],
                                unit: str) -> str:
    """Format a single per-metric summary row. With one sample we
    suppress the (meaningless) stddev rather than printing 0.00 next
    to a single value, which previously misled the user."""
    n = len(vals)
    if n == 1:
        return (f"    {label:<12}  {vals[0]:>+12.2f} {unit:<3}  "
                f"{'(n=1)':>9}  {vals[0]:>+7.2f}")
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    mx = max(vals, key=abs)
    return (f"    {label:<12}  {mean:>+12.2f} {unit:<3}  "
            f"{sd:>9.2f}  {mx:>+7.2f}")


def _format_pair_short(row: Dict[str, Any]) -> str:
    """One-line summary of a pair, used in the rejected/unmatched
    detail sections."""
    of_n = row.get("shot_number_of") or "-"
    tm_n = row.get("shot_number_tm") or "-"
    of_t = row.get("timestamp_of") or ""
    tm_t = row.get("timestamp_tm") or ""
    of_b = row.get("ball_speed_of")
    tm_b = row.get("ball_speed_tm")
    of_b_s = f"{of_b:.1f} mph" if of_b is not None else "(no OF)"
    tm_b_s = f"{tm_b:.1f} mph" if tm_b is not None else "(no TM)"
    notes = row.get("notes") or ""
    return (f"    OF #{of_n} ({of_t})  ↔  TM #{tm_n} ({tm_t})    "
            f"OF: {of_b_s}    TM: {tm_b_s}    {notes}")


def _pair_counts(pairs: List[Pair]) -> Dict[str, int]:
    counts = {"good": 0, "ball_speed_mismatch": 0,
              "unmatched_openflight": 0, "unmatched_trackman": 0}
    for p in pairs:
        counts[p.match_quality] = counts.get(p.match_quality, 0) + 1
    return counts


def print_summary(pairs: List[Pair]) -> None:
    rows = [_row(p) for p in pairs]
    by_club: Dict[str, List[Dict[str, Any]]] = {}
    counts = _pair_counts(pairs)
    for r in rows:
        by_club.setdefault(r["club"] or "(no club)", []).append(r)

    print()
    print("=" * 72)
    print("  COMPARISON SUMMARY")
    print("=" * 72)
    print(f"  Total pairs:           {len(rows)}")
    print(f"  Good:                  {counts['good']}")
    print(f"  Ball-speed mismatch:   {counts['ball_speed_mismatch']}")
    print(f"  Unmatched OpenFlight:  {counts['unmatched_openflight']}")
    print(f"  Unmatched Trackman:    {counts['unmatched_trackman']}")
    print()

    for club in sorted(by_club):
        good = [r for r in by_club[club] if r["match_quality"] == "good"]
        if not good:
            print(f"  {club}: no good pairs ({len(by_club[club])} total)")
            continue
        print(f"  {club} — {len(good)} good pair(s) "
              f"(of {len(by_club[club])} total)")
        print(f"    {'metric':<12}  {'mean Δ (of-tm)':>15}  "
              f"{'stddev':>10}  {'|max|':>8}")
        for key, label, unit in _DELTA_LABELS:
            vals = [r[key] for r in good if r[key] is not None]
            if not vals:
                continue
            print(_format_metric_summary_line(label, vals, unit))
        print()

    # Detail sections for non-good pairs so the user can see which
    # specific shots got rejected and decide whether to manually fix
    # the alignment or ignore them.
    rejected = [r for r in rows if r["match_quality"] == "ball_speed_mismatch"]
    if rejected:
        print(f"  Ball-speed-mismatch pairs ({len(rejected)}) "
              "— excluded from per-club stats:")
        for r in rejected:
            print(_format_pair_short(r))
        print()

    unmatched = [r for r in rows
                 if r["match_quality"].startswith("unmatched_")]
    if unmatched:
        print(f"  Unmatched pairs ({len(unmatched)}):")
        for r in unmatched:
            print(_format_pair_short(r))
        print()

    print("=" * 72)


def build_summary(pairs: List[Pair],
                  tm_info: Optional[TrackmanLoadInfo] = None) -> Dict[str, Any]:
    """Machine-readable summary (``--summary-json``)."""
    return {
        "conventions": {
            "delta": "openflight - trackman",
            "units": {m.column: m.unit for m in NUMERIC_METRICS},
            "slope": "OF = slope * TM + intercept",
            "signs": ("right-handed; + = right for launch direction, club path "
                      "(in-to-out), face angle (open), face to path (open), "
                      "spin axis (fade), side"),
        },
        "pairing": _pair_counts(pairs),
        "coverage": coverage(pairs),
        "metrics": compute_metric_stats(pairs),
        "classes": compute_class_stats(pairs),
        "trackman_file": None if tm_info is None else {
            "delimiter": tm_info.delimiter,
            "decimal_comma": tm_info.decimal_comma,
            "column_map": tm_info.column_map,
            "units": tm_info.units,
            "warnings": tm_info.warnings,
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pair OpenFlight ↔ Trackman shots and emit a "
                    "side-by-side comparison CSV.",
    )
    parser.add_argument("--openflight", required=True, type=Path,
                        help="OpenFlight session JSONL file")
    parser.add_argument("--trackman", required=True, type=Path,
                        help="Trackman CSV export")
    parser.add_argument("--output", required=True, type=Path,
                        help="Output comparison CSV")
    parser.add_argument("--summary-json", type=Path, default=None,
                        help="Also write per-metric / class statistics as JSON")
    parser.add_argument("--ball-speed-tol", type=float, default=5.0,
                        help="Max ball-speed delta (mph) before a pair "
                             "is flagged as a likely mis-pair (default 5.0)")
    parser.add_argument("--club-filter", default=None,
                        help="Comma-separated list of clubs to include "
                             "(e.g. '7-iron,driver'). Default: all.")
    args = parser.parse_args(argv)

    if not args.openflight.exists():
        print(f"OpenFlight log not found: {args.openflight}", file=sys.stderr)
        return 2
    if not args.trackman.exists():
        print(f"Trackman CSV not found: {args.trackman}", file=sys.stderr)
        return 2

    of_shots = load_openflight(args.openflight)
    tm_info = TrackmanLoadInfo()
    tm_shots = load_trackman(args.trackman, tm_info)
    print(f"Loaded {len(of_shots)} OpenFlight shots, "
          f"{len(tm_shots)} Trackman shots")
    print(f"Trackman columns: {tm_info.column_map}")
    print(f"Trackman units:   {tm_info.units}  "
          f"(delimiter '{tm_info.delimiter}', decimal comma {tm_info.decimal_comma})")
    for w in tm_info.warnings:
        print(f"  warning: {w}", file=sys.stderr)

    club_filter = (
        [c.strip() for c in args.club_filter.split(",") if c.strip()]
        if args.club_filter else None
    )
    pairs = pair_shots(of_shots, tm_shots,
                       ball_speed_tol_mph=args.ball_speed_tol,
                       club_filter=club_filter)
    write_comparison_csv(pairs, args.output)
    print(f"Wrote {len(pairs)} pair rows to {args.output}")
    print_summary(pairs)
    print_metric_agreement(pairs)
    print_class_agreement(pairs)
    print_ball_speed_calibration(pairs)
    print_launch_angle_calibration(pairs)
    if args.summary_json is not None:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        with open(args.summary_json, "w", encoding="utf-8") as fh:
            json.dump(build_summary(pairs, tm_info), fh, indent=2)
        print(f"Wrote summary JSON to {args.summary_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
