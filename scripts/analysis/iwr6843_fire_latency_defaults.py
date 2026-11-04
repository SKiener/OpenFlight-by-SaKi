"""Reproduce the ``--ti-fire-latency-ms`` defaults from a firmware-variant replay CSV.

The defaults in ``openflight.iwr6843.self_trigger.DEFAULT_FIRE_LATENCY_MS`` come
from ``analysis/iwr-self-trigger-20261031/data/variants/E6_acq_conf02_step125.csv``
(the replay of firmware v1.0.3 + cb8fbff + candidate club-track acquisition,
image ``openflight_iwr6843_v1.0.3-of1.bin``). Rows used: ``corpus`` owner,
``ball_mph`` >= 60, one ``shift`` (``lead2`` = watched bin 2 bins short of the
ball, offset 0 m; ``off+4`` = offset +0.2 m). Per firing rule (``how``) the
median of ``fire_minus_impact_ms`` (the ~3 ms board decision included);
``unknown`` is the pooled median of all fired rows of that shift.

    uv run python scripts/analysis/iwr6843_fire_latency_defaults.py path/to/E6_acq_conf02_step125.csv
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

RULES = ("crossing", "end", "leave")
SHIFTS = ("lead2", "off+4")
MIN_BALL_MPH = 60.0


def _percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile (numpy's default method)."""
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def fire_latencies(path: Path, shift: str) -> dict[str, dict[str, float]]:
    """Per rule (and ``unknown`` = pooled): n, median, p10, p90, max of fire minus impact."""
    by_rule: dict[str, list[float]] = {rule: [] for rule in RULES}
    with path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["corpus"] != "owner" or row["shift"] != shift:
                continue
            if not row["ball_mph"] or float(row["ball_mph"]) < MIN_BALL_MPH:
                continue
            if not row["fire_minus_impact_ms"] or row["how"] not in by_rule:
                continue
            by_rule[row["how"]].append(float(row["fire_minus_impact_ms"]))
    groups = {**by_rule, "unknown": [value for values in by_rule.values() for value in values]}
    return {
        rule: {
            "n": len(values),
            "median": round(statistics.median(values), 2),
            "p10": round(_percentile(values, 0.10), 2),
            "p90": round(_percentile(values, 0.90), 2),
            "max": round(max(values), 2),
        }
        for rule, values in groups.items()
        if values
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("csv", type=Path)
    args = parser.parse_args()
    for shift in SHIFTS:
        stats = fire_latencies(args.csv, shift)
        print(f"{shift}:")
        for rule, values in stats.items():
            print(
                f"  {rule:8s} n={values['n']:3.0f} median={values['median']:+6.2f} "
                f"p10={values['p10']:+6.2f} p90={values['p90']:+6.2f} max={values['max']:+6.2f}"
            )
        flag = ",".join(f"{rule}={stats[rule]['median']:g}" for rule in (*RULES, "unknown"))
        # worst: the slowest fire of the shift; it only sizes the OPS split check.
        print(f"  --ti-fire-latency-ms {flag},worst={stats['unknown']['max']:g}")


if __name__ == "__main__":
    main()
