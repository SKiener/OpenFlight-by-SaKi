#!/usr/bin/env python3
"""Club path directional separation report.

Club path has no ground truth at a home range, so acceptance is separation
between deliberate swing groups rather than an accuracy figure. Run three
sessions -- in-to-out, square, out-to-in -- and pass their JSONLs here.

Usage:
    uv run python scripts/iwr6843/club_path_report.py \
        --out-to-in session_A.jsonl \
        --square     session_B.jsonl \
        --in-to-out  session_C.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

# Below this many shots, a group's sample stdev is too noisy to trust as a
# spread estimate -- with n=3-4 it can land 2-3x under the true spread by
# chance, which would admit a gap that isn't real rather than removing the
# small-sample pathology. Validation sessions are 10-shot blocks, so 5 is
# already a lenient floor while giving the stdev enough support to mean
# something.
CLUB_PATH_MIN_GROUP_N = 5

# The estimator's own measured residual, from a first-principles fixture
# across +/-12 deg: 0.034 deg at 4 deg, 0.092 deg at 8 deg, 0.297 deg
# (max) at 12 deg. No sample size can fix this -- a gap below the
# instrument's own precision is physically meaningless regardless of n.
CLUB_PATH_MIN_SEPARATION_DEG = 0.3


def _iter_club_path_entries(path: str | Path):
    """Yield the raw ``club_path`` value of every ``iwr6843_capture`` line.

    ``None`` (not ``{}``) is yielded when the capture never attempted an
    estimate at all -- runtime.py records ``club_path=None`` when there was
    no OPS club speed to gate against -- so callers can tell "skipped" apart
    from "estimator ran and rejected".
    """
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "iwr6843_capture":
                continue
            yield entry.get("club_path")


def _is_accepted(club_path: dict | None) -> bool:
    """Whether a club-path entry counts as accepted.

    Matches ``ClubPathResult.accepted`` (club.py): a bare ``"accepted"``
    status is the common case only when the ball's LCMF estimate produced a
    measured TDM sign; runtime.py otherwise falls back to the configured
    sign policy and records ``"accepted_tdm_sign_fallback"``, which is still
    a real, published estimate -- the UI displays it -- so it must count.
    """
    if not club_path:
        return False
    return str(club_path.get("status") or "").startswith("accepted")


def load_group(path: str | Path) -> list[float]:
    """Accepted club-path values from one session JSONL, in shot order."""
    values: list[float] = []
    for club_path in _iter_club_path_entries(path):
        if _is_accepted(club_path):
            value = club_path.get("path_deg")
            if value is not None:
                values.append(float(value))
    return values


def group_coverage(path: str | Path) -> dict:
    """Accepted/rejected/skipped/total capture counts for one session.

    A session with many rejections is informative, not empty -- surfacing the
    rejection count keeps a low-coverage session visible instead of it
    silently producing statistics over a handful of shots. "Skipped" is kept
    separate from "rejected": a null ``club_path`` means there was no OPS
    club speed to gate against (runtime.py:68), so the estimator never ran --
    that is not a failure of the estimator and should not read as one.
    """
    total = 0
    accepted = 0
    skipped = 0
    for club_path in _iter_club_path_entries(path):
        total += 1
        if club_path is None:
            skipped += 1
        elif _is_accepted(club_path):
            accepted += 1
    return {
        "total": total,
        "accepted": accepted,
        "rejected": total - accepted - skipped,
        "skipped": skipped,
    }


def separation(groups: dict[str, list[float]]) -> dict:
    """Group statistics plus ordering and separation verdicts.

    Ordered: means increase in the given key order. Separated requires
    ordering, every group meeting CLUB_PATH_MIN_GROUP_N, and every adjacent
    gap exceeding both the larger neighbouring stdev and
    CLUB_PATH_MIN_SEPARATION_DEG. Those two guards fail differently: a
    falsely-small stdev from too few shots is one failure mode, a real but
    physically meaningless gap is another, and neither one covers the other
    -- so both are checked independently and both reasons are reported when
    both fail, rather than only ever surfacing one per adjacent pair.

    ``reasons`` names which specific condition failed (ordering, a group
    below the sample-size floor, a gap under the measurement floor, or a gap
    within the neighbouring spread) so a failing report says why rather than
    leaving the operator to read the code.
    """
    stats = {
        name: {
            "n": len(values),
            "mean": statistics.mean(values) if values else None,
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "insufficient_n": len(values) < CLUB_PATH_MIN_GROUP_N,
        }
        for name, values in groups.items()
    }
    names = list(groups)
    means = [stats[name]["mean"] for name in names]
    ordered = all(
        a is not None and b is not None and a < b for a, b in zip(means, means[1:])
    )

    reasons: list[str] = []
    if not ordered:
        reasons.append("group means are not in the expected increasing order")
    for name in names:
        if stats[name]["insufficient_n"]:
            reasons.append(
                f"{name}: n={stats[name]['n']} < {CLUB_PATH_MIN_GROUP_N} minimum "
                "-- stdev not trustworthy"
            )
    sufficient_n = not any(stats[name]["insufficient_n"] for name in names)

    separated = ordered and sufficient_n
    for first, second in zip(names, names[1:]):
        if stats[first]["mean"] is None or stats[second]["mean"] is None:
            continue
        gap = abs(stats[second]["mean"] - stats[first]["mean"])
        stdev_floor = max(stats[first]["stdev"], stats[second]["stdev"])
        if gap <= CLUB_PATH_MIN_SEPARATION_DEG:
            separated = False
            reasons.append(
                f"{first}->{second}: gap {gap:.2f} deg is below the "
                f"{CLUB_PATH_MIN_SEPARATION_DEG} deg measurement floor"
            )
        if gap <= stdev_floor:
            separated = False
            reasons.append(
                f"{first}->{second}: gap {gap:.2f} deg does not exceed the "
                f"within-group spread ({stdev_floor:.2f} deg stdev)"
            )
    return {"groups": stats, "ordered": ordered, "separated": separated, "reasons": reasons}


def main(argv: list[str] | None = None) -> int:
    """Print the separation report; exit non-zero when it does not separate."""
    parser = argparse.ArgumentParser(description="Club path separation report")
    parser.add_argument("--out-to-in", required=True, help="Session JSONL, out-to-in block")
    parser.add_argument("--square", required=True, help="Session JSONL, square block")
    parser.add_argument("--in-to-out", required=True, help="Session JSONL, in-to-out block")
    args = parser.parse_args(argv)

    sessions = {
        "out-to-in": args.out_to_in,
        "square": args.square,
        "in-to-out": args.in_to_out,
    }
    groups = {name: load_group(path) for name, path in sessions.items()}
    coverage = {name: group_coverage(path) for name, path in sessions.items()}
    report = separation(groups)

    print(f"{'group':>12} {'n':>4} {'rejected':>9} {'skipped':>8} {'mean':>8} {'stdev':>8}")
    for name, group_stats in report["groups"].items():
        mean = "n/a" if group_stats["mean"] is None else f"{group_stats['mean']:8.2f}"
        flag = (
            f"  (n < {CLUB_PATH_MIN_GROUP_N}, unreliable)"
            if group_stats["insufficient_n"]
            else ""
        )
        print(
            f"{name:>12} {group_stats['n']:>4} {coverage[name]['rejected']:>9} "
            f"{coverage[name]['skipped']:>8} {mean:>8} {group_stats['stdev']:>8.2f}{flag}"
        )
    print()
    print(f"ordering correct: {report['ordered']}")
    print(f"groups separate:  {report['separated']}")
    if report["reasons"]:
        print("reasons:")
        for reason in report["reasons"]:
            print(f"  - {reason}")
    return 0 if report["separated"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
