"""Regression guard: radar speed accuracy against committed TrackMan data.

``test_ballistics_trackman_regression.py`` feeds the ballistic model
TrackMan's own launch conditions, so nothing checks the radar's speeds against
real data. This replays every I/Q capture in the committed session through
``process_capture`` the way the monitor calls it, pairs the results with the
same session's TrackMan export via ``compare_trackman``, and asserts per-club
speed RMSE. Speeds are raw radial speeds, before the server's cosine
correction.

Ported from upstream PR #308. Two fork differences in the replay: the
processor runs at each capture's logged sample rate (these 2026-05-06 captures
predate ``sample_rate_hz`` and are 30 ksps; the fork runs the OPS at 20 ksps
live), and, as the monitor does on the sound trigger, the club speed is
anchored at the sound edge (``anchor_club_to_trigger``) and the ball is voted
only after impact.

Measured on all 24 shots, which pair within the tool's 5 mph tolerance (mph)::

                     ball speed        club speed
    club            rmse    bias      rmse    bias
    driver          3.19   -3.15      1.25   -0.61
    7-iron          2.73   -2.67      1.03   -0.37
    pw              2.62   -2.47      1.52   +0.85
    overall         2.86   -2.77      1.26   -0.09

Ball speed is identical to upstream's unanchored replay on the other 22 shots.
Club speed is far better than upstream's (2.43 / 3.90 / 8.41 / 5.47 RMSE on
those 22): the anchored read no longer takes shot 19's ball-contaminated 96.3
or shot 21's 65.2.

Shots 9 and 13 were unpaired until the ball vote required a track (two
readings at most ``BALL_TRACK_MAX_WINDOW_GAP`` standard windows apart). Shot 9
(7-iron) read two strays 21 ms apart, 122.8 against TrackMan 117.3; now 114.4.
Shot 13 (driver) read 54.2 against 162.2: its weak 156-158 mph ball shows in
three post-impact windows, three different 1-mph bins, and the bin vote took a
slower pair 30 ms apart (upstream's all-window vote: 65.0, which also capped
its club line). Now 157.8 mph ball and 106.8 mph club (TrackMan 109.0).

Budgets: club speed sits at the larger of measured + 30 % and measured +
0.5 mph. Ball speed keeps upstream's tighter budgets (measured + 0.25-0.4 mph
when they were set): a 30 % margin (about 0.9 mph) would let a real ball-speed
regression through. Bringing shots 9 and 13 back raised the measured RMSE
(their errors now count) but every budget still holds, so none was raised; the
driver ball budget is now only 0.06 mph above its measured RMSE. Lower the
budgets when the processor improves.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ANALYSIS = _REPO_ROOT / "scripts" / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

from compare_trackman import (  # noqa: E402  (path set up above)
    _rmse,
    load_openflight,
    load_trackman,
    pair_shots,
)
from replay_captures import load_captures  # noqa: E402

from openflight.clubs import ClubType  # noqa: E402
from openflight.rolling_buffer.monitor import (  # noqa: E402
    get_optimal_spin_for_ball_speed,
)
from openflight.rolling_buffer.processor import RollingBufferProcessor  # noqa: E402
from openflight.rolling_buffer.types import IQCapture, logged_sample_rate_hz  # noqa: E402

SESSION_LOG = _REPO_ROOT / "session_logs" / "session_20260506_152406_range.jsonl"
TRACKMAN_CSV = _REPO_ROOT / "session_logs" / "OpenFlight-Test.Normalized.csv"

EXPECTED_CAPTURE_COUNT = 24
EXPECTED_UNPAIRED_SHOTS: set[int] = set()

RMSE_BUDGET_MPH = {
    "ball_speed_mph": {"driver": 3.25, "7-iron": 3.0, "pw": 3.0, "overall": 3.0},
    "club_speed_mph": {"driver": 1.55, "7-iron": 1.6, "pw": 2.05, "overall": 1.75},
}


def _session_trigger_type(path: Path) -> str | None:
    """Trigger type recorded in the session's ``session_start`` entry."""
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry.get("type") == "session_start":
                return entry.get("trigger_type")
    return None


def _replay(capture: dict, club: ClubType, trigger_type: str | None):
    """Run one logged capture through the processor the way the monitor does.

    The processor is built at the capture's logged sample rate (30 ksps for
    logs that predate ``sample_rate_hz``, as this one does), and the club speed
    is anchored at the sound edge when the session ran on the sound trigger.
    """
    rate_hz = logged_sample_rate_hz(capture)
    return RollingBufferProcessor(sample_rate=rate_hz).process_capture(
        IQCapture(
            sample_time=capture["sample_time"],
            trigger_time=capture["trigger_time"],
            i_samples=capture["i_samples"],
            q_samples=capture["q_samples"],
            sound_edge_delay_ms=float(capture.get("sound_edge_delay_ms") or 0.0),
            sample_rate_hz=rate_hz,
        ),
        expected_spin_for_ball_speed=lambda ball_speed_mph: get_optimal_spin_for_ball_speed(
            ball_speed_mph, club
        ),
        club_type=club,
        anchor_club_to_trigger=(trigger_type == "sound"),
    )


@pytest.fixture(scope="module")
def captures():
    if not SESSION_LOG.exists():
        pytest.skip(f"Radar reference session not present: {SESSION_LOG}")
    return {c["shot_number"]: c for c in load_captures(str(SESSION_LOG))}


@pytest.fixture(scope="module")
def pairs(captures):
    """Logged shots (for club and timestamp) with replayed speeds, paired with TrackMan."""
    if not TRACKMAN_CSV.exists():
        pytest.skip(f"TrackMan reference capture not present: {TRACKMAN_CSV}")
    shots = load_openflight(SESSION_LOG)
    trigger_type = _session_trigger_type(SESSION_LOG)
    for shot in shots:
        processed = _replay(captures[shot.shot_number], ClubType(shot.club), trigger_type)
        shot.ball_speed_mph = processed.ball_speed_mph if processed else None
        shot.club_speed_mph = processed.club_speed_mph if processed else None
    return pair_shots(shots, load_trackman(TRACKMAN_CSV))


@pytest.fixture(scope="module")
def good_pairs(pairs):
    """``pair_shots`` also calls a pair good when the replay produced no ball
    speed, so drop those here."""
    return [p for p in pairs if p.match_quality == "good" and p.of.ball_speed_mph is not None]


def test_reference_session_capture_count(captures):
    """Pin the fixture size so budget drift cannot hide behind a changed file."""
    assert len(captures) == EXPECTED_CAPTURE_COUNT, (
        f"Reference session has {len(captures)} I/Q captures, expected "
        f"{EXPECTED_CAPTURE_COUNT}. Re-derive the budgets if it changed on purpose."
    )


def test_unpaired_shots(captures, pairs, good_pairs):
    """A shot the processor stops reading correctly drops out of the pairing
    instead of failing a budget, so pin exactly which shots are out."""
    unpaired = set(captures) - {p.of.shot_number for p in good_pairs}
    readings = {
        p.of.shot_number: f"{p.of.ball_speed_mph and round(p.of.ball_speed_mph, 1)} vs "
        f"TrackMan {p.tm.ball_speed_mph:.1f}"
        for p in pairs
        if p.of and p.tm
    }
    changed = sorted(unpaired ^ EXPECTED_UNPAIRED_SHOTS)
    assert not changed, (
        f"Unpaired shots are {sorted(unpaired)}, expected {sorted(EXPECTED_UNPAIRED_SHOTS)}. "
        f"Ball speed (mph) for the shots that changed: "
        f"{', '.join(f'{n}: {readings.get(n)}' for n in changed)}."
    )


def test_every_good_pair_has_club_speed(good_pairs):
    """Pin coverage so the club-speed budgets cannot pass on a shrinking set."""
    missing = [p.of.shot_number for p in good_pairs if p.of.club_speed_mph is None]
    assert not missing, f"No club speed detected for shots {missing}."


@pytest.mark.parametrize(
    "field, club",
    [(field, club) for field, budgets in RMSE_BUDGET_MPH.items() for club in budgets],
)
def test_speed_rmse_within_budget(good_pairs, field, club):
    deltas = [
        getattr(p.of, field) - getattr(p.tm, field)
        for p in good_pairs
        if getattr(p.of, field) is not None and club in ("overall", p.of.club)
    ]
    assert deltas, f"No good pairs for club {club!r}"
    rmse = _rmse(deltas)
    budget = RMSE_BUDGET_MPH[field][club]
    assert rmse <= budget, (
        f"{club} {field}: RMSE {rmse:.2f} mph exceeds budget {budget} mph "
        f"(bias {sum(deltas) / len(deltas):+.2f}, n={len(deltas)})."
    )
