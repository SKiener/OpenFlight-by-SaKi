#!/usr/bin/env python3
"""Measure the IWR6843LEVM TX2 horizontal phase zero with a static reflector.

Input: three-TX L3 dumps of a corner reflector standing still at a known
distance and lateral position, e.g. clap-triggered captures saved by
``scripts/iwr6843/calibrate.py --debug``. Phases come from the raw range data
because the production horizontal path removes static returns (per-frame mean
over loops), which would erase the reflector.

The result is a candidate value for ``--iwr6843-horizontal-phase-reference-rad``.
It is a static RF measurement, not a golf-ball validation.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from openflight.iwr6843 import doa
from openflight.iwr6843.calibration import DEFAULT_CAL_PATH, Calibration
from openflight.iwr6843.dump import parse_dump, range_data
from openflight.iwr6843.shot import TX2_LOOP_PERIOD_S, geometry_from_header

SEARCH_HALF_WIDTH_M = 0.30
MIN_DOMINANCE_DB = 10.0
MIN_FLOOR_DB = 20.0
MIN_STATIC_TO_MOTION_DB = 15.0
MAX_PHASE_STD_RAD = 0.10
MAX_POSITION_SPREAD_RAD = 0.10
MIN_FRAMES = 6


@dataclass
class DumpEvidence:
    """Reflector detection and per-frame TX2 phases from one dump."""

    path: str
    range_bin: int
    measured_range_m: float
    dominance_db: float | None
    floor_db: float
    static_to_motion_db: float
    frame_phases_rad: list[float] = field(default_factory=list)
    rejected: str | None = None


@dataclass
class PositionResult:
    """Phase-zero estimate for one reflector distance."""

    distance_m: float
    lateral_m: float
    expected_phase_rad: float
    dumps: list[DumpEvidence]
    n_frames: int = 0
    phase_zero_rad: float | None = None
    circular_std_rad: float | None = None
    rejected: str | None = None


def wrap(phase_rad: float | np.ndarray) -> float | np.ndarray:
    """Wrap a phase to (-pi, pi]."""
    return np.angle(np.exp(1j * np.asarray(phase_rad)))


def circular_stats(phases_rad: list[float]) -> tuple[float, float]:
    """Circular mean and circular standard deviation."""
    mean_vector = np.mean(np.exp(1j * np.asarray(phases_rad)))
    resultant = min(float(abs(mean_vector)), 1.0)
    std = math.sqrt(-2.0 * math.log(resultant)) if resultant > 0 else math.inf
    return float(np.angle(mean_vector)), std


def _db(numerator: float, denominator: float) -> float:
    return 10.0 * math.log10(max(numerator, 1e-30) / max(denominator, 1e-30))


def reflector_evidence(
    raw: bytes, *, distance_m: float, range_bias_m: float, path: str = ""
) -> DumpEvidence:
    """Locate the static reflector and measure TX2 phase per frame."""
    meta, cube = parse_dump(raw)
    if meta["n_tx"] != 3:
        raise ValueError(f"{path}: needs the three-TX capture profile, got {meta['n_tx']} TX")
    geometry = geometry_from_header(meta, loop_period_s=TX2_LOOP_PERIOD_S)
    n_frames = cube.shape[0]
    tdm = range_data(meta, cube).reshape(n_frames, geometry.n_loops, 3, geometry.n_rx, -1)
    static = tdm.mean(axis=1)
    static_power = np.sum(np.abs(static) ** 2, axis=(1, 2))
    motion_power = np.mean(np.sum(np.abs(tdm - static[:, None]) ** 2, axis=(2, 3)), axis=1)

    max_bin = max(
        geometry.frame_bin_start(f) + geometry.frame_bin_count(f) for f in range(n_frames)
    )
    totals = np.zeros((3, max_bin))
    for frame in range(n_frames):
        start = geometry.frame_bin_start(frame)
        count = geometry.frame_bin_count(frame)
        totals[0, start : start + count] += static_power[frame, :count]
        totals[1, start : start + count] += motion_power[frame, :count]
        totals[2, start : start + count] += 1
    observed = np.nonzero(totals[2])[0]
    static_mean = totals[0, observed] / totals[2, observed]
    motion_mean = totals[1, observed] / totals[2, observed]

    res = geometry.range_res_m
    expected_bin = (distance_m + range_bias_m) / res
    in_search = np.abs(observed - expected_bin) <= SEARCH_HALF_WIDTH_M / res
    if not np.any(in_search):
        low, high = observed.min() * res - range_bias_m, observed.max() * res - range_bias_m
        raise ValueError(
            f"{path}: {distance_m:.2f} m lies outside the captured range windows "
            f"({low:.2f}-{high:.2f} m)"
        )
    search_index = np.nonzero(in_search)[0]
    peak_index = int(search_index[np.argmax(static_mean[search_index])])
    peak_bin = int(observed[peak_index])
    competitors = [i for i in search_index if abs(int(observed[i]) - peak_bin) > 2]
    evidence = DumpEvidence(
        path=path,
        range_bin=peak_bin,
        measured_range_m=peak_bin * res - range_bias_m,
        dominance_db=(
            _db(static_mean[peak_index], max(static_mean[i] for i in competitors))
            if competitors
            else None
        ),
        floor_db=_db(static_mean[peak_index], float(np.median(static_mean))),
        static_to_motion_db=_db(static_mean[peak_index], motion_mean[peak_index]),
    )
    if evidence.dominance_db is not None and evidence.dominance_db < MIN_DOMINANCE_DB:
        evidence.rejected = "reflector_not_dominant"
    elif evidence.floor_db < MIN_FLOOR_DB:
        evidence.rejected = "reflector_too_weak"
    if evidence.rejected:
        return evidence

    for frame in range(n_frames):
        if not geometry.contains_bin(peak_bin, frame=frame):
            continue
        local_bin = geometry.local_bin(peak_bin, frame)
        samples = [
            doa.tx2_phase_at(
                tdm, frame, loop, local_bin, velocity_ms=0.0, tdm_sign=1, n_rx=geometry.n_rx
            )
            for loop in range(geometry.n_loops)
        ]
        samples = [sample for sample in samples if sample is not None]
        if samples:
            phases, weights = zip(*samples)
            mean_vector = np.sum(np.asarray(weights) * np.exp(1j * np.asarray(phases)))
            evidence.frame_phases_rad.append(float(np.angle(mean_vector)))
    return evidence


def expected_phase_rad(distance_m: float, lateral_m: float) -> float:
    """TX2 phase of a target at this position after an ideal zero calibration."""
    if abs(lateral_m) >= distance_m:
        raise ValueError("lateral offset must be smaller than the reflector distance")
    return float(doa.tx2_axis_angle_to_phase_rad(-math.asin(lateral_m / distance_m)))


def evaluate_position(
    distance_m: float, lateral_m: float, evidence: list[DumpEvidence]
) -> PositionResult:
    """Pool usable dumps of one placement into a phase-zero estimate."""
    expected = expected_phase_rad(distance_m, lateral_m)
    result = PositionResult(distance_m, lateral_m, expected, evidence)
    zeros = [
        float(wrap(phase - expected))
        for item in evidence
        if item.rejected is None
        for phase in item.frame_phases_rad
    ]
    result.n_frames = len(zeros)
    if result.n_frames < MIN_FRAMES:
        result.rejected = "too_few_usable_frames"
        return result
    result.phase_zero_rad, result.circular_std_rad = circular_stats(zeros)
    if result.circular_std_rad > MAX_PHASE_STD_RAD:
        result.rejected = "phase_scatter_too_high"
    return result


def angle_shift_deg(reference_rad: float) -> float:
    """Change of every reported HLA when the reference is applied (small angles)."""
    return -math.degrees(doa.tx2_phase_to_axis_angle_rad(-reference_rad))


def summarize(positions: list[PositionResult]) -> dict:
    """Combine placements into a recommendation, or refuse."""
    reasons = [f"{p.distance_m:.2f} m: {p.rejected}" for p in positions if p.rejected is not None]
    usable = [p for p in positions if p.rejected is None]
    warnings: list[str] = []
    for position in positions:
        for item in position.dumps:
            if item.rejected:
                warnings.append(f"{item.path}: {item.rejected}")
            elif item.static_to_motion_db < MIN_STATIC_TO_MOTION_DB:
                warnings.append(f"{item.path}: motion near the reflector range")
    if len(usable) == 1:
        warnings.append("only one distance; distance consistency is unchecked")
    report: dict = {
        "tool": "iwr6843_horizontal_phase_cal",
        "status": "refused",
        "reasons": reasons,
        "warnings": warnings,
        "recommended_horizontal_phase_reference_rad": None,
        "launcher_argument": None,
        "angle_shift_deg": None,
        "position_spread_rad": None,
        "positions": [asdict(p) for p in positions],
        "thresholds": {
            "min_dominance_db": MIN_DOMINANCE_DB,
            "min_floor_db": MIN_FLOOR_DB,
            "min_static_to_motion_db": MIN_STATIC_TO_MOTION_DB,
            "max_phase_std_rad": MAX_PHASE_STD_RAD,
            "max_position_spread_rad": MAX_POSITION_SPREAD_RAD,
            "min_frames": MIN_FRAMES,
        },
    }
    if reasons or not usable:
        if not usable:
            reasons.append("no usable reflector position")
        return report
    zeros = [p.phase_zero_rad for p in usable]
    reference, _std = circular_stats(zeros)
    spread = max(abs(float(wrap(a - b))) for a in zeros for b in zeros)
    report["position_spread_rad"] = spread
    if spread > MAX_POSITION_SPREAD_RAD:
        reasons.append("distances disagree; check placement and multipath")
        return report
    report.update(
        status="warning" if warnings else "ok",
        recommended_horizontal_phase_reference_rad=round(reference, 4),
        launcher_argument=f"--iwr6843-horizontal-phase-reference-rad {reference:.4f}",
        angle_shift_deg=round(angle_shift_deg(reference), 3),
    )
    return report


def run(
    positions: list[tuple[float, list[Path]]], *, lateral_m: float, range_bias_m: float
) -> dict:
    """Evaluate every placement and summarize."""
    results = []
    for distance_m, paths in positions:
        evidence = [
            reflector_evidence(
                path.read_bytes(),
                distance_m=distance_m,
                range_bias_m=range_bias_m,
                path=str(path),
            )
            for path in paths
        ]
        results.append(evaluate_position(distance_m, lateral_m, evidence))
    return summarize(results)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--position",
        nargs="+",
        action="append",
        required=True,
        metavar=("DISTANCE_M", "DUMP"),
        help="Reflector slant distance from the TI antenna centre, then its .l3dump files",
    )
    parser.add_argument(
        "--reflector-lateral-m",
        type=float,
        required=True,
        help=(
            "Signed lateral offset of the reflector from the TI antenna centre; "
            "positive = right, seen from behind the radar looking downrange"
        ),
    )
    parser.add_argument("--cal", default=DEFAULT_CAL_PATH, help="Calibration JSON (range bias)")
    parser.add_argument("--output", type=Path, default=None, help="Write the JSON report here")
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; exit status 2 means no value is recommended."""
    args = _build_parser().parse_args(argv)
    positions = []
    for group in args.position:
        if len(group) < 2:
            raise SystemExit("--position needs a distance and at least one dump")
        positions.append((float(group[0]), [Path(p) for p in group[1:]]))
    report = run(
        positions,
        lateral_m=args.reflector_lateral_m,
        range_bias_m=Calibration.load(args.cal).range_bias_m,
    )
    text = json.dumps(report, indent=2)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 2 if report["status"] == "refused" else 0


if __name__ == "__main__":
    sys.exit(main())
