# Range Session Report

Reference for `scripts/analysis/range_session_report.py`: one command that
compares a range session with a Toptracer export and writes a German markdown
report. It replaces the throwaway pairing scripts written per session.

```bash
uv run python scripts/analysis/range_session_report.py \
    session_20261030_142447_range.jsonl toptracer-swingsync-20261030.csv \
    --kiosk-log kiosk_20261030_142420.log --date 2026-10-30 \
    --out report.md --pairs-csv pairs.csv
```

| Option | Meaning |
|---|---|
| `--date YYYY-MM-DD` | Toptracer day (UTC). One export can hold many sessions. Without it the tool takes the only day in the export, else the day in the session file name if the export has it, else it stops and lists the days. |
| `--kiosk-log` | Adds the edge count, the camera-shadow verdict per edge and the saved trigger checks. |
| `--club-map OF=TT` | Overrides the Toptracer club label of an OpenFlight club, repeatable. Use it when the Toptracer club was set wrongly, e.g. `--club-map '7-wood=8 Iron'` for 2026-10-25. The report warns when a club appears on one side only. |
| `--max-dv` / `--skip-cost` | Pairing limits in mph (defaults 4 and 3). |
| `--pairs-csv` | Writes the pairs, one row per pair. |
| `--out` | Report path; stdout without it. |

## Pairing

The Pi clock is wrong on the range (no NTP), so wall time is never used. Shots
are pooled by Toptracer club label. Inside a pool both shot sequences keep their
order and a dynamic-programming alignment picks the pairs: a pair costs the
ball-speed difference and is only allowed within `--max-dv`; an unpaired shot
costs `--skip-cost`. A missing shot in the middle (e.g. Toptracer driver #8 on
2026-10-30) is skipped instead of shifting every later pair. Unpaired Toptracer
shots are reported as missed, unpaired OpenFlight shots as phantoms.

Speed alone cannot always tell which of several similar driver shots was
missed; the timing-aware block search used for 2026-10-25 can pick a different
one. Totals stay the same.

## Report contents

Per club and overall: pairs, missed and phantom shots; ball-speed bias and MAE;
VLA bias and sd per source; horizontal start direction by source with slope and
offset of OF against Toptracer (left is negative, Toptracer `L` suffix); carry
bias and MAE in metres (`carry_spin_adjusted`, else `estimated_carry_yards`).

A pair whose OpenFlight ball speed has `ball_speed_status` `uncertain` (the OPS
ball-track guard kept a club-like speed, so the start is wrong) stays paired and
counts in the pair numbers and the pairs CSV, but every OF − TT statistic (ball
speed, VLA, start direction, carry, carry variants, curve) leaves it out; each of
those sections says in one line how many pairs it left out.

Curve, irons and wedges only: coverage (`curve_class`, else `spin_axis_class`),
hit rate against the Toptracer Classification mapped to
Hook/Draw/Straight/Fade/Slice (keyword `Hook` > `Slice` > `Draw` > `Fade`;
pure start-line names such as Pull, Push or Dead Straight are Straight; Top,
Shank and empty are not scored), and two null models: N1 always predicts the
session's most common Toptracer class, N2 classifies the OpenFlight start
direction with the `curve_class` limits.

Carry variants ("Carry-Varianten", only when the session log has
`carry_variants` entries): per club and overall, bias and MAE against the
Toptracer carry of the live carry and of each shadow variant (tour spin,
range-ball spin, range-ball spin with the range-ball drag at 0.3546 and 0.225),
over the pairs that have an entry. Entries join their shot by `shot_number` and
`shot_timestamp` (the shot's `impact_timestamp`); the last entry of a shot
counts (a null one: the shot has no variants). The live column compares the
same flight as the variants: for a shot flown again after its spin axis changed
(`shot_updated`) it is the re-flown carry, while the Carry section keeps the
`shot_detected` one. See [session log](../reference/session-log.md).

Also: mean inclinometer pitch and trigger statistics (trigger events, accepted,
false triggers per minute between the first and the last edge, rejection
reasons, real shots within 3 s after a false trigger, gate-shadow decisions for
real shots and false triggers).

Tests: `uv run pytest tests/test_range_session_report.py`.
