"""scripts/analysis/iwr6843_fire_latency_defaults.py on a synthetic variant CSV."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import iwr6843_fire_latency_defaults as fld  # noqa: E402  pylint: disable=wrong-import-position

FIELDS = ["corpus", "dump", "shift", "group", "ball_mph", "how", "fire_minus_impact_ms"]


def _write(path: Path, rows: list[tuple]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    return path


def test_medians_per_rule_and_pooled(tmp_path):
    rows = [
        ("owner", "a", "lead2", "iron", 100, "crossing", -2.0),
        ("owner", "b", "lead2", "iron", 100, "crossing", -1.0),
        ("owner", "c", "lead2", "iron", 100, "crossing", 0.0),
        ("owner", "d", "lead2", "driver", 140, "end", 2.0),
        ("owner", "e", "lead2", "driver", 140, "leave", 9.0),
        # filtered out: slow ball, other corpus, other shift, unfired
        ("owner", "f", "lead2", "wedge", 50, "crossing", 30.0),
        ("branch", "g", "lead2", "iron", 100, "crossing", 30.0),
        ("owner", "h", "off+4", "iron", 100, "crossing", 3.0),
        ("owner", "i", "lead2", "iron", 100, "", ""),
    ]
    stats = fld.fire_latencies(_write(tmp_path / "v.csv", rows), "lead2")
    assert stats["crossing"]["n"] == 3 and stats["crossing"]["median"] == -1.0
    assert stats["end"]["median"] == 2.0 and stats["leave"]["max"] == 9.0
    assert stats["unknown"]["n"] == 5 and stats["unknown"]["median"] == 0.0
    assert stats["unknown"]["max"] == 9.0
    assert fld.fire_latencies(tmp_path / "v.csv", "off+4")["crossing"]["median"] == 3.0


def test_percentile_matches_linear_interpolation():
    assert fld._percentile([0.0, 10.0], 0.9) == pytest.approx(9.0)  # pylint: disable=protected-access
