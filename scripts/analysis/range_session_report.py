#!/usr/bin/env python3
"""Automatic range-session report: OpenFlight session log against a Toptracer export.

Usage::

    uv run python scripts/analysis/range_session_report.py \\
        session_20261030_142447_range.jsonl toptracer-swingsync-20261030.csv \\
        [--kiosk-log kiosk_20261030_142420.log] [--date 2026-10-30] \\
        [--club-map 7-wood="8 Iron"] [--pairs-csv pairs.csv] [--out report.md]

Writes a German markdown report (stdout without ``--out``).

Pairing
-------
The Pi clock is not trusted (no NTP on the range), so wall time is never used to
pair. Shots are pooled by Toptracer club label (an OpenFlight club maps to one
label, see ``of_club_to_tt``; ``--club-map`` overrides it, e.g. when the Toptracer
club was set wrongly). Inside a pool both sequences keep their shot order and an
order-preserving alignment (dynamic programming) picks the pairs: pairing costs
the ball-speed difference in mph and is only allowed within ``--max-dv`` (default
4 mph); leaving a shot unpaired costs ``--skip-cost`` (default 3 mph). A Toptracer
shot that stays unpaired was missed by OpenFlight; an OpenFlight shot that stays
unpaired is a phantom (or a shot Toptracer did not see). A shot whose ball speed is
``uncertain`` (``ball_speed_status``: a wrong start) pairs like any other; the OF - TT
statistics leave it out and each section says how many.

Inputs
------
* Toptracer SwingSync CSV: header row, unit row, then one row per shot. One
  export may hold many sessions; ``--date`` (UTC day of the ``Date`` column)
  selects one. Shots are ordered by ``Date`` then ``Shot number``. Speeds are kph,
  distances m; direction values carry an ``L``/``R`` suffix (L is negative).
* OpenFlight session JSONL: ``shot_detected`` lines (minus ``shot_deleted`` ones
  and demo shots), ``trigger_event`` lines and ``carry_variants`` lines (the shadow
  carries of the spin tables and the range-ball drag, joined to their shot by
  ``shot_number`` and ``shot_timestamp``; the last entry of a shot counts). A shot's
  ``shot_updated`` lines (re-flown after its spin axis changed) only give the live
  carry the carry variants are compared with.
* Optional kiosk log: ``[GATE] edge #N`` lines (edge count, camera shadow
  decisions) and ``[CAMERA] Trigger check`` lines.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics as st
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:  # keep the curve thresholds in sync with the live classifier
    from openflight.dplane import CURVE_STRAIGHT_MAX_DEG, CURVE_STRONG_MIN_DEG
except ImportError:  # pragma: no cover - script used outside the repo venv
    CURVE_STRAIGHT_MAX_DEG = 2.5
    CURVE_STRONG_MIN_DEG = 8.0
try:  # ball_speed_status of a ball speed the OPS ball-track guard kept but distrusts
    from openflight.launch_monitor import BALL_SPEED_UNCERTAIN as UNCERTAIN
except ImportError:  # pragma: no cover - script used outside the repo venv
    UNCERTAIN = "uncertain"

KPH_TO_MPH = 0.621371
YD_TO_M = 0.9144
DEFAULT_MAX_DV_MPH = 4.0
DEFAULT_SKIP_COST_MPH = 3.0
FALSE_TRIGGER_WINDOW_S = 3.0
CURVE_CLASSES = ("hook", "draw", "straight", "fade", "slice")
CLASS_DE = {"hook": "Hook", "draw": "Draw", "straight": "Straight", "fade": "Fade", "slice": "Slice"}
SIDE = {"hook": "left", "draw": "left", "straight": "straight", "fade": "right", "slice": "right"}
# The session log's carry_variants, in the order of openflight.ballistics.CARRY_VARIANTS.
CARRY_VARIANTS_DE = OrderedDict([
    ("tour", "Tour-Spin"),
    ("range_ball_spin", "Rangeball-Spin"),
    ("range_ball_spin_drag", "Rangeball-Spin + Drag 0,3546"),
    ("range_ball_spin_drag_half", "Rangeball-Spin + Drag 0,225"),
])

# OpenFlight wedges are named by loft; the owner's Toptracer labels for them.
WEDGE_TO_TT = {"wedge-44": "Pitching Wedge", "wedge-48": "Lob Wedge", "wedge-54": "Sand Wedge"}
NAMED_TO_TT = {"driver": "Driver", "mini-driver": "2 Wood", "putter": "Putter"}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def fnum(value) -> Optional[float]:
    """Finite float or None (bools are not numbers here)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_signed(text: Optional[str]) -> Optional[float]:
    """Toptracer number with optional L/R suffix: ``"4.300 L"`` -> -4.3, ``"504 R"`` -> 504."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    match = re.fullmatch(r"(-?[0-9]*\.?[0-9]+)\s*([LR])?", text)
    if not match:
        return None
    value = float(match.group(1))
    return -value if match.group(2) == "L" else value


def of_club_to_tt(club: Optional[str], overrides: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Toptracer club label for an OpenFlight club key (``8-iron`` -> ``8 Iron``)."""
    if not club:
        return None
    if overrides and club in overrides:
        return overrides[club]
    if club in NAMED_TO_TT:
        return NAMED_TO_TT[club]
    if club in WEDGE_TO_TT:
        return WEDGE_TO_TT[club]
    match = re.fullmatch(r"(\d+)-(iron|wood|hybrid)", club)
    if match:
        return f"{match.group(1)} {match.group(2).capitalize()}"
    return club


def is_iron(club: Optional[str]) -> bool:
    """Irons and wedges: the clubs the calculated curve is defined for."""
    return bool(club) and ("iron" in club or "wedge" in club)


def tt_class(classification: Optional[str]) -> Optional[str]:
    """Toptracer Classification -> hook/draw/straight/fade/slice (None: not a flight shape).

    Hook > Slice > Draw > Fade by keyword ("Pull Hook" -> hook, "Straight Fade" -> fade,
    "Underfade" -> fade); pure start-line names (Pull, Push, Dead Straight, Slight Pull,
    Huge Pull ...) -> straight; Top, Shank and empty -> None.
    """
    text = (classification or "").strip().lower()
    if not text or text in ("top", "shank"):
        return None
    for key in ("hook", "slice", "draw", "fade"):
        if key in text:
            return key
    return "straight"


def class_from_axis(axis_deg: Optional[float]) -> Optional[str]:
    """Same limits as ``openflight.dplane.curve_class`` (deg, + right)."""
    axis = fnum(axis_deg)
    if axis is None:
        return None
    if abs(axis) < CURVE_STRAIGHT_MAX_DEG:
        return "straight"
    if abs(axis) < CURVE_STRONG_MIN_DEG:
        return "draw" if axis < 0 else "fade"
    return "hook" if axis < 0 else "slice"


@dataclass
class TTShot:
    shot: int
    date: str
    club: str
    classification: str
    ball_mph: Optional[float]
    club_mph: Optional[float]
    vla: Optional[float]
    hla: Optional[float]
    carry_m: Optional[float]
    total_m: Optional[float]
    backspin: Optional[float]
    sidespin: Optional[float]
    curve_m: Optional[float]

    @property
    def curve(self) -> Optional[str]:
        return tt_class(self.classification)


def load_tt(path: Path) -> List[TTShot]:
    """All shots of a SwingSync CSV (header row, unit row, data rows)."""
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        return []
    header = [h.strip() for h in rows[0]]
    col = {name: i for i, name in enumerate(header)}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    shots = []
    for row in rows[1:]:
        number = fnum(get(row, "Shot number"))
        if number is None:  # the unit row and blank lines
            continue
        kph = fnum(get(row, "Ball speed"))
        club_kph = fnum(get(row, "Club speed"))
        shots.append(TTShot(
            shot=int(number), date=(get(row, "Date") or "").strip(),
            club=(get(row, "Club type") or "").strip(),
            classification=(get(row, "Classification") or "").strip(),
            ball_mph=kph * KPH_TO_MPH if kph is not None else None,
            club_mph=club_kph * KPH_TO_MPH if club_kph is not None else None,
            vla=fnum(get(row, "Vertical Launch angle")),
            hla=parse_signed(get(row, "Horizontal Launch Angle")),
            carry_m=fnum(get(row, "Carry")), total_m=fnum(get(row, "Total")),
            backspin=fnum(get(row, "Backspin")), sidespin=parse_signed(get(row, "Sidespin")),
            curve_m=parse_signed(get(row, "Curve")),
        ))
    return shots


def select_tt_day(shots: Sequence[TTShot], day: Optional[str], session_path: Optional[Path] = None
                  ) -> Tuple[str, List[TTShot]]:
    """Shots of one UTC day, ordered by Date then Shot number.

    Without ``day``: the only day in the export, else the day in the session file
    name if the export has it, else an error listing the days.
    """
    days = sorted({s.date[:10] for s in shots if s.date})
    if day is None:
        if len(days) == 1:
            day = days[0]
        else:
            guess = None
            if session_path is not None:
                match = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", session_path.name)
                if match:
                    guess = "-".join(match.groups())
            if guess in days:
                day = guess
            else:
                raise SystemExit(f"Toptracer export holds several days, pass --date: {', '.join(days)}")
    picked = [s for s in shots if s.date[:10] == day]
    picked.sort(key=lambda s: (s.date, s.shot))
    return day, picked


@dataclass
class OFShot:
    index: int
    shot: Optional[int]
    timestamp: Optional[str]
    club: Optional[str]
    ball_mph: Optional[float]
    club_mph: Optional[float]
    vla: Optional[float]
    vla_source: Optional[str]
    hla: Optional[float]
    hla_source: Optional[str]
    hla_confidence: Optional[float]
    carry_yd: Optional[float]
    carry_field: Optional[str]
    curve_class: Optional[str]
    spin_axis_deg: Optional[float]
    spin_axis_class: Optional[str]
    club_path_deg: Optional[float]
    pitch_deg: Optional[float]
    # carry_variants entry of the shot: variant name -> carry in m (None: no entry).
    variant_carry_m: Optional[Dict[str, Optional[float]]] = None
    # Carry of the shot's last flight, the one its last carry_variants entry shadows: the
    # last shot_updated entry's (re-flown), else carry_yd.
    final_carry_yd: Optional[float] = None
    # "uncertain" when the OPS ball-track guard kept a club-like ball speed (wrong launch).
    ball_speed_status: Optional[str] = None

    @property
    def curve(self) -> Optional[str]:
        """OpenFlight curve: curve_class, else the 3-way spin_axis_class."""
        for value in (self.curve_class, self.spin_axis_class):
            if isinstance(value, str) and value.lower() in CURVE_CLASSES:
                return value.lower()
        return None


@dataclass
class Session:
    shots: List[OFShot]
    triggers: List[dict]
    deleted: int
    demo: int


def _carry(entry: dict) -> Tuple[Optional[float], Optional[str]]:
    for name in ("carry_spin_adjusted", "estimated_carry_yards"):
        value = fnum(entry.get(name))
        if value is not None:
            return value, name
    return None, None


def _variant_carries(
    entries: Sequence[dict],
) -> Dict[Tuple[object, object], Optional[Dict[str, Optional[float]]]]:
    """carry_variants entries by (shot_number, shot_timestamp); the last one wins.

    The last entry shadows the shot's last flight: when its flights failed (null), the
    shot has no variants rather than an earlier flight's.
    """
    out = {}
    for entry in entries:
        if entry.get("type") != "carry_variants":
            continue
        variants = entry.get("carry_variants")
        carries = None
        if isinstance(variants, dict):
            carries = {
                name: fnum(values.get("carry_m")) if isinstance(values, dict) else None
                for name, values in variants.items()
            }
        out[(entry.get("shot_number"), entry.get("shot_timestamp"))] = carries
    return out


def _updated_carries(entries: Sequence[dict]) -> Dict[Tuple[object, object], Optional[float]]:
    """Carry (yd) of each shot's last shot_updated entry, by (shot_number, impact_timestamp)."""
    return {
        (entry.get("shot_number"), entry.get("impact_timestamp")): _carry(entry)[0]
        for entry in entries
        if entry.get("type") == "shot_updated"
    }


def load_session(path: Path) -> Session:
    """shot_detected (minus deleted and demo shots), trigger_event and carry_variants lines."""
    entries = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    deleted_ts = {e.get("shot_timestamp") for e in entries if e.get("type") == "shot_deleted"}
    variants = _variant_carries(entries)
    updated = _updated_carries(entries)
    shots, triggers, n_deleted, n_demo = [], [], 0, 0
    for entry in entries:
        kind = entry.get("type")
        if kind == "trigger_event":
            triggers.append(entry)
            continue
        if kind != "shot_detected":
            continue
        if entry.get("timestamp") in deleted_ts:
            n_deleted += 1
            continue
        if entry.get("demo") is True:
            n_demo += 1
            continue
        carry, carry_field = _carry(entry)
        key = (entry.get("shot_number"), entry.get("impact_timestamp"))
        inclinometer = entry.get("inclinometer") if isinstance(entry.get("inclinometer"), dict) else {}
        shots.append(OFShot(
            index=len(shots), shot=entry.get("shot_number"), timestamp=entry.get("timestamp"),
            club=entry.get("club"), ball_mph=fnum(entry.get("ball_speed_mph")),
            club_mph=fnum(entry.get("club_speed_mph")),
            vla=fnum(entry.get("launch_angle_vertical")),
            vla_source=entry.get("launch_angle_vertical_source"),
            hla=fnum(entry.get("launch_angle_horizontal")),
            hla_source=entry.get("launch_angle_horizontal_source"),
            hla_confidence=fnum(entry.get("launch_angle_horizontal_confidence")),
            carry_yd=carry, carry_field=carry_field,
            curve_class=entry.get("curve_class"),
            spin_axis_deg=fnum(entry.get("calculated_spin_axis_deg")),
            spin_axis_class=entry.get("spin_axis_class"),
            club_path_deg=fnum(entry.get("camera_club_path_deg")),
            pitch_deg=fnum(inclinometer.get("calibrated_pitch_deg")),
            variant_carry_m=variants.get(key),
            final_carry_yd=updated[key] if key in updated else carry,
            ball_speed_status=entry.get("ball_speed_status"),
        ))
    return Session(shots=shots, triggers=triggers, deleted=n_deleted, demo=n_demo)


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def align(of_speeds: Sequence[Optional[float]], tt_speeds: Sequence[Optional[float]],
          max_dv: float = DEFAULT_MAX_DV_MPH, skip_cost: float = DEFAULT_SKIP_COST_MPH
          ) -> List[Tuple[int, int]]:
    """Order-preserving alignment of two speed sequences; returns (of_index, tt_index) pairs.

    Pairing costs |dv| and is allowed only for |dv| <= max_dv (both speeds known);
    skipping a shot on either side costs skip_cost. Ties prefer pairing.
    """
    n, m = len(of_speeds), len(tt_speeds)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: List[List[Optional[Tuple[int, int, bool]]]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < n and j < m and of_speeds[i] is not None and tt_speeds[j] is not None:
                dv = abs(of_speeds[i] - tt_speeds[j])
                if dv <= max_dv and here + dv <= cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], back[i + 1][j + 1] = here + dv, (i, j, True)
            if i < n and here + skip_cost < cost[i + 1][j]:
                cost[i + 1][j], back[i + 1][j] = here + skip_cost, (i, j, False)
            if j < m and here + skip_cost < cost[i][j + 1]:
                cost[i][j + 1], back[i][j + 1] = here + skip_cost, (i, j, False)
    pairs, i, j = [], n, m
    while (i, j) != (0, 0):
        pi, pj, paired = back[i][j]
        if paired:
            pairs.append((pi, pj))
        i, j = pi, pj
    return pairs[::-1]


@dataclass
class Pairing:
    pairs: List[Tuple[OFShot, TTShot]]
    phantoms: List[OFShot]
    missed: List[TTShot]
    pools: "OrderedDict[str, List[str]]" = field(default_factory=OrderedDict)


def pair_session(of_shots: Sequence[OFShot], tt_shots: Sequence[TTShot],
                 club_map: Optional[Dict[str, str]] = None,
                 max_dv: float = DEFAULT_MAX_DV_MPH, skip_cost: float = DEFAULT_SKIP_COST_MPH) -> Pairing:
    """Pool by Toptracer club label, align each pool by order + ball speed."""
    pools: "OrderedDict[str, List[str]]" = OrderedDict()
    for shot in of_shots:
        label = of_club_to_tt(shot.club, club_map) or "?"
        pools.setdefault(label, [])
        if shot.club not in pools[label]:
            pools[label].append(shot.club)
    for shot in tt_shots:
        pools.setdefault(shot.club, [])
    pairs, phantoms, missed = [], [], []
    for label in pools:
        o = [s for s in of_shots if (of_club_to_tt(s.club, club_map) or "?") == label]
        t = [s for s in tt_shots if s.club == label]
        matched = align([s.ball_mph for s in o], [s.ball_mph for s in t], max_dv, skip_cost)
        used_o = {i for i, _ in matched}
        used_t = {j for _, j in matched}
        pairs += [(o[i], t[j]) for i, j in matched]
        phantoms += [s for i, s in enumerate(o) if i not in used_o]
        missed += [s for j, s in enumerate(t) if j not in used_t]
    pairs.sort(key=lambda p: p[0].index)
    phantoms.sort(key=lambda s: s.index)
    missed.sort(key=lambda s: (s.date, s.shot))
    return Pairing(pairs=pairs, phantoms=phantoms, missed=missed, pools=pools)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def mean(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return st.fmean(vals) if vals else None


def sdev(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return st.stdev(vals) if len(vals) > 1 else None


def mae(values: Iterable[Optional[float]]) -> Optional[float]:
    return mean(abs(v) for v in values if v is not None)


def linfit(x: Sequence[float], y: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    """y = offset + slope * x; returns (slope, offset, r) or None (n < 3 or no spread)."""
    if len(x) < 3:
        return None
    mx, my = st.fmean(x), st.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0:
        return None
    slope = sum((a - mx) * (b - my) for a, b in zip(x, y)) / sxx
    r = slope * math.sqrt(sxx / syy) if syy > 0 else float("nan")
    return slope, my - slope * mx, r


def certain(pairs) -> list:
    """The pairs whose OF ball speed is not ``uncertain``: an uncertain one (the OPS ball-track
    guard kept a club-like speed) is a wrong start and would distort every OF − TT comparison.
    Pairing keeps them; only the statistics leave them out."""
    return [(o, t) for o, t in pairs if o.ball_speed_status != UNCERTAIN]


def left_out_text(n: int) -> str:
    """The report line that says how many pairs a section left out (see ``certain``)."""
    return (f"- {n} Paare mit unsicherer Ballgeschwindigkeit"
            " (`ball_speed_status` `uncertain`, falscher Start) nicht gewertet.")


def diffs(pairs, of_get, tt_get) -> List[float]:
    out = []
    for o, t in pairs:
        a, b = of_get(o), tt_get(t)
        if a is not None and b is not None:
            out.append(a - b)
    return out


def ball_d(pairs):
    return diffs(pairs, lambda o: o.ball_mph, lambda t: t.ball_mph)


def vla_d(pairs):
    return diffs(pairs, lambda o: o.vla, lambda t: t.vla)


def carry_d(pairs):
    return diffs(pairs, lambda o: o.carry_yd * YD_TO_M if o.carry_yd is not None else None,
                 lambda t: t.carry_m)


def final_carry_d(pairs) -> List[float]:
    """Carry of the shot's last flight (re-flown or not) minus TT carry (m)."""
    return diffs(pairs, lambda o: o.final_carry_yd * YD_TO_M if o.final_carry_yd is not None else None,
                 lambda t: t.carry_m)


def variant_d(pairs, name: str) -> List[float]:
    """Carry of one carry variant minus TT carry (m), over the pairs that have it."""
    return diffs(pairs, lambda o: (o.variant_carry_m or {}).get(name), lambda t: t.carry_m)


def hla_fit(pairs):
    pts = [(t.hla, o.hla) for o, t in pairs if o.hla is not None and t.hla is not None]
    if not pts:
        return None, 0
    return linfit([p[0] for p in pts], [p[1] for p in pts]), len(pts)


@dataclass
class CurveStats:
    irons: int
    covered: int
    scored: int  # covered irons with a Toptracer class
    hits5: int
    hits3: int
    n_null: int  # irons with a Toptracer class
    n1_class: Optional[str]
    n1_hits: int
    n2_hits: int
    n2_scored: int
    confusion: Counter


def curve_stats(pairs) -> CurveStats:
    irons = [(o, t) for o, t in pairs if is_iron(o.club)]
    covered = [(o, t) for o, t in irons if o.curve is not None]
    scored = [(o, t) for o, t in covered if t.curve is not None]
    hits5 = sum(1 for o, t in scored if o.curve == t.curve)
    hits3 = sum(1 for o, t in scored if SIDE[o.curve] == SIDE[t.curve])
    with_tt = [(o, t) for o, t in irons if t.curve is not None]
    counts = Counter(t.curve for _, t in with_tt)
    n1_class = counts.most_common(1)[0][0] if counts else None
    n1_hits = sum(1 for _, t in with_tt if t.curve == n1_class)
    n2 = [(class_from_axis(o.hla), t.curve) for o, t in with_tt if o.hla is not None]
    n2_hits = sum(1 for a, b in n2 if a == b)
    confusion = Counter((o.curve, t.curve) for o, t in scored)
    return CurveStats(irons=len(irons), covered=len(covered), scored=len(scored), hits5=hits5,
                      hits3=hits3, n_null=len(with_tt), n1_class=n1_class, n1_hits=n1_hits,
                      n2_hits=n2_hits, n2_scored=len(n2), confusion=confusion)


def edge_time(event: dict) -> Optional[float]:
    """Intra-session edge time (s): the GPIO edge stamp, else the log timestamp."""
    for name in ("gate_edge_timestamp", "edge_kernel_timestamp"):
        value = fnum(event.get(name))
        if value is not None:
            return value
    try:
        return datetime.fromisoformat(event.get("ts")).timestamp()
    except (TypeError, ValueError):
        return None


@dataclass
class TriggerStats:
    events: int
    accepted: int
    rejected: int
    reasons: Counter
    duration_min: Optional[float]
    real_after_false: int
    shadow_real: Counter
    shadow_false: Counter
    shadow_reason_real: Counter
    shadow_reason_false: Counter

    @property
    def false_per_min(self) -> Optional[float]:
        if not self.duration_min:
            return None
        return self.rejected / self.duration_min


def trigger_stats(triggers: Sequence[dict], window_s: float = FALSE_TRIGGER_WINDOW_S) -> TriggerStats:
    timed = [(edge_time(e), e) for e in triggers]
    timed = [(t, e) for t, e in timed if t is not None]
    timed.sort(key=lambda te: te[0])
    times = [t for t, _ in timed]
    duration = (times[-1] - times[0]) / 60.0 if len(times) > 1 else None
    accepted = [e for e in triggers if e.get("accepted") is True]
    rejected = [e for e in triggers if e.get("accepted") is not True]
    last_false = None
    real_after_false = 0
    for t, e in timed:
        if e.get("accepted") is True:
            if last_false is not None and 0 <= t - last_false <= window_s:
                real_after_false += 1
        else:
            last_false = t
    return TriggerStats(
        events=len(triggers), accepted=len(accepted), rejected=len(rejected),
        reasons=Counter(e.get("reason") or "?" for e in rejected), duration_min=duration,
        real_after_false=real_after_false,
        shadow_real=Counter(e.get("gate_shadow_decision") or "—" for e in accepted),
        shadow_false=Counter(e.get("gate_shadow_decision") or "—" for e in rejected),
        shadow_reason_real=Counter(e.get("gate_shadow_reason") or "—" for e in accepted
                                   if e.get("gate_shadow_decision") not in (None, "departed")),
        shadow_reason_false=Counter(e.get("gate_shadow_reason") or "—" for e in rejected
                                    if e.get("gate_shadow_decision") not in (None, "departed")),
    )


_GATE_EDGE = re.compile(r"\[GATE\] edge #(\d+)")
_GATE_SHADOW = re.compile(r"\[GATE\] edge #\d+ camera shadow -> would \w+: (departed|still_there|unclear)"
                          r"(?: \(([^)]*)\))?")
_TRIGGER_CHECK = re.compile(r"\[CAMERA\] Trigger check #\d+: (departed|still_there|unclear)(?: \(([^)]*)\))?")


@dataclass
class KioskStats:
    edges: int
    shadow: Counter
    shadow_reasons: Counter
    checks: Counter


def kiosk_stats(path: Path) -> KioskStats:
    edges = set()
    shadow, shadow_reasons, checks = Counter(), Counter(), Counter()
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _GATE_EDGE.search(line)
            if match:
                edges.add(int(match.group(1)))
            match = _GATE_SHADOW.search(line)
            if match:
                shadow[match.group(1)] += 1
                if match.group(2):
                    shadow_reasons[match.group(2)] += 1
            match = _TRIGGER_CHECK.search(line)
            if match:
                checks[match.group(1)] += 1
    return KioskStats(edges=len(edges), shadow=shadow, shadow_reasons=shadow_reasons, checks=checks)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def num(value: Optional[float], digits: int = 1, sign: bool = False) -> str:
    """German number: comma decimals, real minus sign, '–' for missing."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    text = f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"
    return text.replace("-", "−").replace(".", ",")


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({num(100.0 * part / whole, 0)} %)" if whole else f"{part}/0"


def counter_text(counter: Counter) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common()) or "–"


def fit_text(fit, n: int) -> str:
    if fit is None:
        return f"– (n={n})"
    slope, offset, r = fit
    return f"{num(slope, 2)} / {num(offset, 1, True)}° (r={num(r, 2)}, n={n})"


def _club_groups(pairing: Pairing, of_shots: Sequence[OFShot], tt_shots: Sequence[TTShot],
                 club_map) -> "OrderedDict[str, dict]":
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for shot in of_shots:
        groups.setdefault(shot.club or "?", {"pairs": [], "phantoms": [], "missed": []})
    for o, t in pairing.pairs:
        groups[o.club or "?"]["pairs"].append((o, t))
    for o in pairing.phantoms:
        groups[o.club or "?"]["phantoms"].append(o)
    label_to_club = {}
    for club in groups:
        label_to_club.setdefault(of_club_to_tt(club, club_map), club)
    for t in pairing.missed:
        key = label_to_club.get(t.club, f"(TT) {t.club}")
        groups.setdefault(key, {"pairs": [], "phantoms": [], "missed": []})
        groups[key]["missed"].append(t)
    return groups


def build_report(session_path: Path, tt_path: Path, day: str, session: Session,
                 tt_shots: Sequence[TTShot], pairing: Pairing, club_map: Dict[str, str],
                 kiosk: Optional[KioskStats] = None, kiosk_path: Optional[Path] = None,
                 max_dv: float = DEFAULT_MAX_DV_MPH) -> str:
    of_shots = session.shots
    pairs = pairing.pairs
    # Every OF − TT statistic below works on these; pair counts stay those of the pairing.
    scored = certain(pairs)
    n_uncertain = len(pairs) - len(scored)
    groups = _club_groups(pairing, of_shots, tt_shots, club_map)
    lines: List[str] = []
    add = lines.append

    add(f"# Range-Session {day} — OpenFlight gegen Toptracer")
    add("")
    add("Automatisch erzeugt von `scripts/analysis/range_session_report.py`.")
    add("")
    add("## Datenbasis")
    add("")
    add(f"- **OpenFlight:** `{session_path.name}` — {len(of_shots)} Schüsse"
        f" ({session.deleted} gelöschte, {session.demo} Demo-Schüsse nicht gezählt).")
    stamps = sorted({t.date for t in tt_shots})
    add(f"- **Toptracer:** `{tt_path.name}`, Tag {day} (UTC) — {len(tt_shots)} Schläge"
        f" (Zeitstempel im Export: {', '.join(stamps) or '–'}).")
    if kiosk_path is not None:
        add(f"- **Kiosk-Log:** `{kiosk_path.name}`.")
    add(f"- **Paarung:** je Toptracer-Schläger in Schussreihenfolge, Ausrichtung per dynamischer"
        f" Programmierung über die Ballgeschwindigkeit (Paar nur bei |Δv| ≤ {num(max_dv, 1)} mph)."
        f" Die Pi-Uhr wird nie benutzt.")
    mapped = []
    for label, clubs in pairing.pools.items():
        if clubs:
            mapped.append(f"{' + '.join(c or '?' for c in clubs)} → {label}")
    add(f"- **Schläger-Zuordnung:** {'; '.join(mapped) or '–'}"
        + (f" (überschrieben: {', '.join(f'{k}={v}' for k, v in club_map.items())})" if club_map else "")
        + ".")
    tt_labels = {t.club for t in tt_shots}
    lonely_of = [label for label, clubs in pairing.pools.items() if clubs and label not in tt_labels]
    lonely_tt = [label for label, clubs in pairing.pools.items() if not clubs]
    if lonely_of or lonely_tt:
        add(f"- **Hinweis:** Schläger nur auf einer Seite — nur OF: {', '.join(lonely_of) or '–'};"
            f" nur TT: {', '.join(lonely_tt) or '–'}. War der Toptracer-Schläger falsch eingestellt,"
            f" mit `--club-map OF=\"TT-Name\"` neu zuordnen.")
    add(f"- **Ergebnis:** {len(pairs)} Paare, {len(pairing.missed)} TT-Schläge ohne OF-Schuss (verpasst),"
        f" {len(pairing.phantoms)} OF-Schüsse ohne TT-Schlag (Phantome).")
    add("")

    add("## Übersicht pro Schläger")
    add("")
    add("| Schläger | n | verpasst | Phantome | Ballspeed Bias / MAE (mph) | VLA Bias / sd (°) |"
        " Carry Bias / MAE (m) | Startrichtung Steigung / Offset |")
    add("|---|---|---|---|---|---|---|---|")

    def row(name, prs, n_missed, n_phantom):
        sure = certain(prs)
        b, v, c = ball_d(sure), vla_d(sure), carry_d(sure)
        fit, n_fit = hla_fit(sure)
        add(f"| {name} | {len(prs)} | {n_missed} | {n_phantom} | {num(mean(b), 1, True)} / {num(mae(b))} |"
            f" {num(mean(v), 1, True)} / {num(sdev(v))} | {num(mean(c), 1, True)} / {num(mae(c))} |"
            f" {fit_text(fit, n_fit)} |")

    for club, g in groups.items():
        row(club, g["pairs"], len(g["missed"]), len(g["phantoms"]))
    row("**Gesamt**", pairs, len(pairing.missed), len(pairing.phantoms))
    add("")
    add(left_out_text(n_uncertain))
    add("")
    add("Bias = OF − TT. Startrichtung: lineare Regression OF = Offset + Steigung · TT (alle Quellen)."
        " n = alle Paare, auch die nicht gewerteten.")
    add("")

    add("## Ballgeschwindigkeit")
    add("")
    for club, g in groups.items():
        b = ball_d(certain(g["pairs"]))
        if b:
            add(f"- **{club}:** Bias {num(mean(b), 1, True)} mph, MAE {num(mae(b))}, sd {num(sdev(b))},"
                f" max |Δ| {num(max(abs(x) for x in b))} (n={len(b)}).")
    b = ball_d(scored)
    add(f"- **Gesamt:** Bias {num(mean(b), 1, True)} mph, MAE {num(mae(b))} (n={len(b)}).")
    add(left_out_text(n_uncertain))
    add("")

    add("## Vertikaler Abflugwinkel (VLA)")
    add("")
    add("| Quelle | Schläger | n | Bias (°) | sd (°) |")
    add("|---|---|---|---|---|")
    sources = Counter(o.vla_source or "—" for o, _ in scored)
    for source, _ in sources.most_common():
        sub = [(o, t) for o, t in scored if (o.vla_source or "—") == source]
        for club, g in groups.items():
            prs = [(o, t) for o, t in certain(g["pairs"]) if (o.vla_source or "—") == source]
            v = vla_d(prs)
            if v:
                add(f"| {source} | {club} | {len(v)} | {num(mean(v), 1, True)} | {num(sdev(v))} |")
        v = vla_d(sub)
        add(f"| {source} | **alle** | {len(v)} | {num(mean(v), 1, True)} | {num(sdev(v))} |")
    v = vla_d(scored)
    add(f"| **alle** | **alle** | {len(v)} | {num(mean(v), 1, True)} | {num(sdev(v))} |")
    add("")
    add(left_out_text(n_uncertain))
    add("")

    add("## Horizontale Startrichtung")
    add("")
    src = Counter(o.hla_source or "keine" for o, _ in scored)
    add(f"- **Quellen (gepaarte Schüsse):** {counter_text(src)}.")
    add("- Vorzeichen: links negativ (TT-Suffix L → negativ). Regression OF = Offset + Steigung · TT;"
        " ideal Steigung 1, Offset 0.")
    add(left_out_text(n_uncertain))
    add("")
    add("| Quelle | Schläger | Steigung / Offset | MAE (°) |")
    add("|---|---|---|---|")
    for source, _ in src.most_common():
        if source == "keine":
            continue
        for club, g in list(groups.items()) + [("**alle**", {"pairs": pairs})]:
            prs = [(o, t) for o, t in certain(g["pairs"]) if o.hla_source == source]
            fit, n_fit = hla_fit(prs)
            d = diffs(prs, lambda o: o.hla, lambda t: t.hla)
            if n_fit:
                add(f"| {source} | {club} | {fit_text(fit, n_fit)} | {num(mae(d))} |")
    fit, n_fit = hla_fit(scored)
    d = diffs(scored, lambda o: o.hla, lambda t: t.hla)
    add(f"| **alle** | **alle** | {fit_text(fit, n_fit)} | {num(mae(d))} |")
    add("")

    add("## Carry")
    add("")
    fields = Counter(o.carry_field or "keins" for o, _ in scored)
    add(f"- OF-Carry aus {counter_text(fields)} (Yards → m), gegen TT Carry.")
    for club, g in groups.items():
        c = carry_d(certain(g["pairs"]))
        if c:
            add(f"- **{club}:** Bias {num(mean(c), 1, True)} m, MAE {num(mae(c))} (n={len(c)}).")
    c = carry_d(scored)
    add(f"- **Gesamt:** Bias {num(mean(c), 1, True)} m, MAE {num(mae(c))} (n={len(c)}).")
    add(left_out_text(n_uncertain))
    add("")

    any_variants = [(o, t) for o, t in pairs if o.variant_carry_m]
    with_variants = certain(any_variants)
    if any_variants:
        add("## Carry-Varianten")
        add("")
        add(f"- Schatten-Carrys aus `carry_variants` (nur im Log, nicht angezeigt):"
            f" {pct(len(any_variants), len(pairs))} Paare haben einen Eintrag."
            " Gleicher Start (Ballspeed, Winkel, Spinachse, Luftdichte) wie der Live-Carry;"
            " Tour- bzw. Rangeball-Spin-Tabelle, Drag = Rangeball-Widerstand (unvalidiert).")
        add(left_out_text(len(any_variants) - len(with_variants)))
        add("")
        add("| Schläger | n | Live | " + " | ".join(CARRY_VARIANTS_DE.values()) + " |")
        add("|---|---|---|" + "---|" * len(CARRY_VARIANTS_DE))

        def variant_row(name, prs):
            live = final_carry_d(prs)
            cells = [f"{num(mean(live), 1, True)} / {num(mae(live))}"]
            for variant in CARRY_VARIANTS_DE:
                d = variant_d(prs, variant)
                cells.append(f"{num(mean(d), 1, True)} / {num(mae(d))}")
            add(f"| {name} | {len(prs)} | " + " | ".join(cells) + " |")

        for club, g in groups.items():
            prs = [(o, t) for o, t in certain(g["pairs"]) if o.variant_carry_m]
            if prs:
                variant_row(club, prs)
        variant_row("**Gesamt**", with_variants)
        add("")
        add("Bias / MAE in m, OF − TT, nur Paare mit Varianten. Live = der angezeigte Carry dieser"
            " Paare; bei einem neu geflogenen Schlag (`shot_updated`, Kamera-Spinachse) der neue,"
            " wie bei den Varianten.")
        add("")

    cs = curve_stats(scored)
    add("## Kurve (nur Eisen und Wedges)")
    add("")
    add("- **TT-Zuordnung:** Classification enthält „Hook“ → Hook, „Slice“ → Slice, „Draw“ → Draw,"
        " „Fade“ → Fade (in dieser Reihenfolge: „Pull Hook“ → Hook, „Straight Fade“ → Fade,"
        " „Underfade“ → Fade); reine Startlinien (Pull, Push, Dead Straight, Slight Pull, Huge Pull …)"
        " → Straight; Top, Shank und leer → nicht gewertet.")
    add("- **OF-Klasse:** `curve_class`, sonst `spin_axis_class` (draw/straight/fade).")
    add(f"- **Abdeckung:** {pct(cs.covered, cs.irons)} gepaarte Eisen mit OF-Kurvenklasse.")
    if cs.scored:
        add(f"- **Trefferquote OF:** 5 Klassen {pct(cs.hits5, cs.scored)}, Seite (links/gerade/rechts)"
            f" {pct(cs.hits3, cs.scored)}.")
        add("- **Verwechslungen (OF → TT):** "
            + ", ".join(f"{CLASS_DE[a]}→{CLASS_DE[b]} {n}" for (a, b), n in cs.confusion.most_common()) + ".")
    else:
        add("- **Trefferquote OF:** nicht bewertbar (keine gepaarten Eisen mit OF- und TT-Klasse).")
    n1 = CLASS_DE.get(cs.n1_class, "–") if cs.n1_class else "–"
    add(f"- **Nullmodell N1** (immer die häufigste TT-Klasse der Session, {n1}):"
        f" {pct(cs.n1_hits, cs.n_null)}.")
    add(f"- **Nullmodell N2** (Klasse nur aus der OF-Startrichtung, Grenzen wie `curve_class`:"
        f" |h| < {num(CURVE_STRAIGHT_MAX_DEG, 1)}° Straight, < {num(CURVE_STRONG_MIN_DEG, 1)}° Draw/Fade,"
        f" sonst Hook/Slice): {pct(cs.n2_hits, cs.n2_scored)}.")
    add(f"- TT-Klassen der gepaarten Eisen: "
        f"{counter_text(Counter(CLASS_DE[t.curve] for o, t in scored if is_iron(o.club) and t.curve))}.")
    add(left_out_text(sum(1 for o, _ in pairs if is_iron(o.club)) - cs.irons))
    add("")

    add("## Inklinometer")
    add("")
    pitch = [o.pitch_deg for o in of_shots if o.pitch_deg is not None]
    if pitch:
        add(f"- Mittlere kalibrierte Neigung {num(mean(pitch), 2, True)}° (sd {num(sdev(pitch), 2)},"
            f" min {num(min(pitch), 2, True)}, max {num(max(pitch), 2, True)}, n={len(pitch)}).")
    else:
        add("- Keine Inklinometer-Werte im Log.")
    add("")

    ts = trigger_stats(session.triggers)
    add("## Auslöser")
    add("")
    add("| | Wert |")
    add("|---|---|")
    if kiosk is not None:
        add(f"| Kanten (Kiosk-Log) | {kiosk.edges} |")
    add(f"| Trigger-Events (OPS-Dumps) | {ts.events} |")
    add(f"| akzeptiert | {ts.accepted} |")
    add(f"| Fehlauslöser (verworfen) | {ts.rejected} |")
    add(f"| Gründe | {counter_text(ts.reasons)} |")
    add(f"| Dauer erste → letzte Kante | {num(ts.duration_min, 1)} min |")
    add(f"| Fehlauslöser pro Minute | {num(ts.false_per_min, 2)} |")
    if kiosk is not None and ts.duration_min:
        add(f"| Kanten ohne Schuss pro Minute (Kiosk-Kanten − akzeptiert) |"
            f" {num((kiosk.edges - ts.accepted) / ts.duration_min, 2)} |")
    add(f"| echte Schüsse ≤ {num(FALSE_TRIGGER_WINDOW_S, 0)} s nach einem Fehlauslöser | {ts.real_after_false} |")
    add(f"| Torwächter-Schatten, echte Schüsse | {counter_text(ts.shadow_real)} |")
    add(f"| Torwächter-Schatten, Fehlauslöser | {counter_text(ts.shadow_false)} |")
    add(f"| Gründe nicht-departed, echte Schüsse | {counter_text(ts.shadow_reason_real)} |")
    add(f"| Gründe nicht-departed, Fehlauslöser | {counter_text(ts.shadow_reason_false)} |")
    if kiosk is not None:
        add(f"| Kiosk: Schatten-Urteile je Kante | {counter_text(kiosk.shadow)} |")
        add(f"| Kiosk: Gründe der Schatten-Urteile | {counter_text(kiosk.shadow_reasons)} |")
        add(f"| Kiosk: gespeicherte Trigger-Prüfung | {counter_text(kiosk.checks)} |")
    add("")
    add("„—“ = Feld nicht im Log (ältere Firmware ohne Torwächter-Schatten)."
        " Echte Schüsse = akzeptierte Trigger-Events; die Zeitabstände kommen aus den Kanten-Zeitstempeln"
        " derselben Pi-Uhr (nur relativ benutzt).")
    add("")

    add("## Nicht gepaart")
    add("")
    if pairing.missed:
        add("- **Verpasst (TT ohne OF):** " + "; ".join(
            f"TT#{t.shot} {t.club} {num(t.ball_mph)} mph" for t in pairing.missed) + ".")
    else:
        add("- **Verpasst (TT ohne OF):** keine.")
    if pairing.phantoms:
        add("- **Phantome (OF ohne TT):** " + "; ".join(
            f"OF#{o.shot} {o.club} {num(o.ball_mph)} mph" for o in pairing.phantoms) + ".")
    else:
        add("- **Phantome (OF ohne TT):** keine.")
    add("")
    return "\n".join(lines)


def write_pairs_csv(path: Path, pairing: Pairing) -> None:
    keys = ["of_shot", "of_club", "tt_shot", "tt_club", "of_ball_mph", "tt_ball_mph", "d_ball_mph",
            "vla_source", "of_vla", "tt_vla", "hla_source", "of_hla", "tt_hla", "of_carry_m", "tt_carry_m",
            "of_curve", "tt_classification", "tt_curve", "pitch_deg"]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(keys)
        for o, t in pairing.pairs:
            writer.writerow([
                o.shot, o.club, t.shot, t.club, _r(o.ball_mph), _r(t.ball_mph),
                _r(o.ball_mph - t.ball_mph if o.ball_mph is not None and t.ball_mph is not None else None),
                o.vla_source, _r(o.vla), _r(t.vla), o.hla_source, _r(o.hla), _r(t.hla),
                _r(o.carry_yd * YD_TO_M if o.carry_yd is not None else None), _r(t.carry_m),
                o.curve, t.classification, t.curve, _r(o.pitch_deg),
            ])


def _r(value: Optional[float], digits: int = 3):
    return "" if value is None else round(value, digits)


def parse_club_map(items: Sequence[str]) -> Dict[str, str]:
    out = {}
    for item in items or ():
        if "=" not in item:
            raise SystemExit(f"--club-map expects OF=TT, got {item!r}")
        key, value = item.split("=", 1)
        out[key.strip()] = value.strip().strip('"')
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("session", type=Path, help="OpenFlight session JSONL")
    parser.add_argument("toptracer", type=Path, help="Toptracer SwingSync CSV")
    parser.add_argument("--kiosk-log", type=Path, help="kiosk log of the same session")
    parser.add_argument("--date", help="Toptracer day (UTC, YYYY-MM-DD)")
    parser.add_argument("--club-map", action="append", default=[], metavar="OF=TT",
                        help='override the Toptracer label of an OpenFlight club, e.g. 7-wood="8 Iron"')
    parser.add_argument("--max-dv", type=float, default=DEFAULT_MAX_DV_MPH,
                        help="largest ball-speed difference of a pair, mph (default 4)")
    parser.add_argument("--skip-cost", type=float, default=DEFAULT_SKIP_COST_MPH,
                        help="alignment cost of an unpaired shot, mph (default 3)")
    parser.add_argument("--pairs-csv", type=Path, help="also write the pairs as CSV")
    parser.add_argument("--out", type=Path, help="report path (default stdout)")
    args = parser.parse_args(argv)

    club_map = parse_club_map(args.club_map)
    session = load_session(args.session)
    day, tt_shots = select_tt_day(load_tt(args.toptracer), args.date, args.session)
    pairing = pair_session(session.shots, tt_shots, club_map, args.max_dv, args.skip_cost)
    kiosk = kiosk_stats(args.kiosk_log) if args.kiosk_log else None
    report = build_report(args.session, args.toptracer, day, session, tt_shots, pairing, club_map,
                          kiosk, args.kiosk_log, args.max_dv)
    if args.pairs_csv:
        write_pairs_csv(args.pairs_csv, pairing)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    else:
        sys.stdout.write(report + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
