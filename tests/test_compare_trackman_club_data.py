"""Tests for the club-data / Trackman iO extension of compare_trackman.

Synthetic fixtures only: club path, attack angle, face, face to path,
dynamic loft, spin axis, carry side / curve, the curve classes, unit and
sign normalisation, the OpenFlight ``club_data`` merge and the agreement
statistics.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import compare_trackman as ct  # noqa: E402  pylint: disable=wrong-import-position

REPO = Path(__file__).parent.parent
NORMALIZED_FIXTURE = REPO / "session_logs" / "OpenFlight-Test.Normalized.csv"

# Header + units row of an older TrackMan "Normalized" export (subset, same
# order and spelling as the repo fixture), including the decoy columns.
NORMALIZED_HEADER = [
    "Date", "Player", "Club", "Club Speed", "Attack Angle", "Club Path",
    "Dyn. Loft", "Face Angle", "Face To Path", "Ball Speed", "Smash Factor",
    "Launch Angle", "Launch Direction", "Spin Rate", "Spin Rate Type",
    "Spin Axis", "Carry Flat - Length", "Carry Flat - Side",
    "Carry Flat - Ball Speed", "Curve", "Spin Axis (Sim)", "Curve (Sim)",
    "Carry Side (Sim)", "Ball Speed Diff", "Smash Index", "Spin Rate Diff",
]
NORMALIZED_UNITS = [
    "", "", "", "[mph]", "[deg]", "[deg]", "[deg]", "[deg]", "[deg]", "[mph]",
    "[]", "[deg]", "[deg]", "[rpm]", "[]", "[deg]", "[yds]", "[yds]", "[mph]",
    "[ft]", "[deg]", "[ft]", "[ft/in]", "[mph]", "[%]", "[rpm]",
]


def _normalized_row(**over):
    base = {
        "Date": "10/31/2026 3:00:00 PM", "Player": "owner", "Club": "7 Iron",
        "Club Speed": "80.0", "Attack Angle": "-4.0", "Club Path": "-3.0",
        "Dyn. Loft": "22.0", "Face Angle": "-0.5", "Face To Path": "2.5",
        "Ball Speed": "112.0", "Smash Factor": "1.40", "Launch Angle": "17.0",
        "Launch Direction": "-1.0", "Spin Rate": "6200", "Spin Rate Type": "Measured",
        "Spin Axis": "5.0", "Carry Flat - Length": "150.0", "Carry Flat - Side": "4.0",
        "Carry Flat - Ball Speed": "60.0", "Curve": "30.0", "Spin Axis (Sim)": "99",
        "Curve (Sim)": "99", "Carry Side (Sim)": "99", "Ball Speed Diff": "99",
        "Smash Index": "99", "Spin Rate Diff": "99",
    }
    base.update(over)
    return [base[h] for h in NORMALIZED_HEADER]


def _write_normalized(path: Path, rows) -> None:
    lines = ["﻿﻿sep=,"]
    lines.append(",".join(NORMALIZED_HEADER))
    lines.append(",".join(NORMALIZED_UNITS))
    for r in rows:
        lines.append(",".join(f'"{v}"' for v in r))
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")


def _write_jsonl(path: Path, entries) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


def _shot_entry(n, ts, impact, **data):
    """A flat shot_detected entry as SessionLogger writes it."""
    return {"ts": ts, "type": "shot_detected", "shot_number": n,
            "timestamp": ts, "impact_timestamp": impact, "club": "7-iron",
            "ball_speed_mph": 112.0, **data}


def _club_data_entry(n, impact, mode="show", **fields):
    return {"ts": "x", "type": "club_data", "shot_number": n,
            "shot_timestamp": impact, "status": "done", "reason": None,
            "mode": mode, "fields": fields}


# ---------------------------------------------------------------------------
# Header aliases
# ---------------------------------------------------------------------------

class TestClubDataAliases:
    def test_normalized_header_maps_every_club_metric(self):
        col_map = ct._build_column_map(NORMALIZED_HEADER)
        assert col_map["club_path_deg"] == "Club Path"
        assert col_map["attack_angle_deg"] == "Attack Angle"
        assert col_map["face_angle_deg"] == "Face Angle"
        assert col_map["face_to_path_deg"] == "Face To Path"
        assert col_map["dynamic_loft_deg"] == "Dyn. Loft"
        assert col_map["spin_axis_deg"] == "Spin Axis"
        assert col_map["spin_rpm"] == "Spin Rate"
        assert col_map["curve_yards"] == "Curve"
        assert col_map["side_yards"] == "Carry Flat - Side"
        assert col_map["carry_yards"] == "Carry Flat - Length"
        assert col_map["ball_speed_mph"] == "Ball Speed"
        assert col_map["smash_factor"] == "Smash Factor"
        assert col_map["club"] == "Club"

    def test_repo_fixture_header_maps(self):
        with open(NORMALIZED_FIXTURE, encoding="utf-8-sig") as fh:
            fh.readline()  # sep=,
            header = next(csv.reader(fh))
        col_map = ct._build_column_map([h.lstrip("﻿") for h in header])
        for key, col in {
            "club_path_deg": "Club Path", "face_angle_deg": "Face Angle",
            "face_to_path_deg": "Face To Path", "spin_axis_deg": "Spin Axis",
            "curve_yards": "Curve", "dynamic_loft_deg": "Dyn. Loft",
            "launch_angle_horizontal": "Launch Direction",
        }.items():
            assert col_map[key] == col

    @pytest.mark.parametrize("headers,field,expected", [
        (["Spin Rate Type", "Spin Rate"], "spin_rpm", "Spin Rate"),
        (["Spin Axis (Sim)", "Spin Axis"], "spin_axis_deg", "Spin Axis"),
        (["Curve (Sim)", "Curve"], "curve_yards", "Curve"),
        (["Ball Speed Diff", "Ball Speed"], "ball_speed_mph", "Ball Speed"),
        (["Club Speed", "Club"], "club", "Club"),
        (["Smash Index", "Smash Factor"], "smash_factor", "Smash Factor"),
    ])
    def test_exact_header_beats_decoy(self, headers, field, expected):
        assert ct._build_column_map(headers)[field] == expected

    def test_sim_only_columns_are_not_used(self):
        col_map = ct._build_column_map(["Spin Axis (Sim)", "Curve (Sim)", "Club Speed"])
        assert "spin_axis_deg" not in col_map
        assert "curve_yards" not in col_map
        assert "club" not in col_map  # "Club Speed" is not the club name

    def test_web_style_headers_with_units(self):
        headers = ["Club", "Club Path (deg)", "Face Angle (deg)", "Face To Path (deg)",
                   "Dynamic Loft (deg)", "Attack Angle (deg)", "Spin Axis (deg)",
                   "Curve (m)", "Carry Side (m)", "Launch Direction (deg)"]
        col_map = ct._build_column_map(headers)
        assert col_map["dynamic_loft_deg"] == "Dynamic Loft (deg)"
        assert col_map["side_yards"] == "Carry Side (m)"
        assert col_map["curve_yards"] == "Curve (m)"
        assert col_map["launch_angle_horizontal"] == "Launch Direction (deg)"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

class TestSignedValues:
    @pytest.mark.parametrize("raw,expected", [
        ("2.3", 2.3), ("-1.5", -1.5), ("2.3 R", 2.3), ("2.3R", 2.3),
        ("R 2.3", 2.3), ("L1.5", -1.5), ("1.5 L", -1.5), ("I-O 2.1", 2.1),
        ("O-I 2.1", -2.1), ("2.1 in-out", 2.1), ("out-in 4", -4.0),
    ])
    def test_direction_strings(self, raw, expected):
        assert ct._to_signed(raw) == pytest.approx(expected)

    def test_direction_with_decimal_comma(self):
        assert ct._to_signed("L 1,5", decimal_comma=True) == pytest.approx(-1.5)

    @pytest.mark.parametrize("raw", ["", None, "n/a", "R", "abc 2"])
    def test_unparseable_is_none(self, raw):
        assert ct._to_signed(raw) is None

    def test_decimal_comma_number(self):
        assert ct._to_float("1.234,5", decimal_comma=True) == pytest.approx(1234.5)
        assert ct._to_float("-5,2", decimal_comma=True) == pytest.approx(-5.2)
        assert ct._to_float("-5.2", decimal_comma=True) == pytest.approx(-5.2)

    def test_non_finite_is_none(self):
        assert ct._to_float("nan") is None
        assert ct._to_float(float("inf")) is None


# ---------------------------------------------------------------------------
# Trackman loading: units, signs, classes
# ---------------------------------------------------------------------------

class TestLoadTrackmanClubData:
    def test_normalized_export_values_and_units(self, tmp_path):
        path = tmp_path / "tm.csv"
        _write_normalized(path, [_normalized_row()])
        info = ct.TrackmanLoadInfo()
        shots = ct.load_trackman(path, info)
        assert len(shots) == 1
        s = shots[0]
        assert s.club == "7-iron"
        assert s.club_path_deg == pytest.approx(-3.0)
        assert s.attack_angle_deg == pytest.approx(-4.0)
        assert s.face_angle_deg == pytest.approx(-0.5)
        assert s.face_to_path_deg == pytest.approx(2.5)
        assert s.dynamic_loft_deg == pytest.approx(22.0)
        assert s.spin_axis_deg == pytest.approx(5.0)
        assert s.side_yards == pytest.approx(4.0)
        assert s.curve_yards == pytest.approx(10.0)  # 30 ft from the units row
        assert s.curve_class == "fade"
        assert s.spin_axis_class == "fade"
        assert info.units["curve_yards"] == "ft"
        assert info.warnings == []

    def test_repo_fixture_loads(self):
        shots = ct.load_trackman(NORMALIZED_FIXTURE)
        assert shots, "fixture should contain shots"
        first = shots[0]
        assert first.club == "7-iron"
        assert first.club_path_deg == pytest.approx(-6.0)
        assert first.face_to_path_deg == pytest.approx(5.884)
        assert first.spin_axis_deg == pytest.approx(11.506)
        assert first.curve_yards == pytest.approx(55.6557243357758 / 3.0)
        assert first.curve_class == "slice"

    @pytest.mark.parametrize("axis,curve", [
        ("-9.0", "hook"), ("-4.0", "draw"), ("1.0", "straight"),
        ("-2.4", "straight"), ("3.0", "fade"), ("8.0", "slice"),
    ])
    def test_trackman_curve_class_from_spin_axis(self, tmp_path, axis, curve):
        path = tmp_path / "tm.csv"
        _write_normalized(path, [_normalized_row(**{"Spin Axis": axis})])
        assert ct.load_trackman(path)[0].curve_class == curve

    def test_semicolon_decimal_comma_metric_export(self, tmp_path):
        path = tmp_path / "tm_eu.csv"
        content = (
            "sep=;\r\n"
            "Date;Club;Ball Speed (km/h);Club Path (deg);Launch Direction;"
            "Spin Axis;Carry (m);Carry Side (m);Curve (m)\r\n"
            "31.10.2026 15:00:00;7 Iron;180,0;-2,5;L 1,5;3,5 R;140,0;3,2 R;-2,0\r\n"
        )
        path.write_text(content, encoding="utf-8", newline="")
        info = ct.TrackmanLoadInfo()
        s = ct.load_trackman(path, info)[0]
        assert info.delimiter == ";" and info.decimal_comma
        assert s.timestamp == datetime(2026, 10, 31, 15, 0, 0)
        assert s.ball_speed_mph == pytest.approx(111.85, abs=0.01)
        assert s.club_path_deg == pytest.approx(-2.5)
        assert s.launch_angle_horizontal == pytest.approx(-1.5)
        assert s.spin_axis_deg == pytest.approx(3.5)
        assert s.carry_yards == pytest.approx(153.1, abs=0.1)
        assert s.side_yards == pytest.approx(3.5, abs=0.01)
        assert s.curve_yards == pytest.approx(-2.187, abs=0.01)

    def test_semicolon_sniffed_without_sep_line(self, tmp_path):
        path = tmp_path / "tm_eu.csv"
        path.write_text("Club;Ball Speed;Club Path\r\n7 Iron;112,5;-1,0\r\n",
                        encoding="utf-8", newline="")
        s = ct.load_trackman(path)[0]
        assert s.ball_speed_mph == pytest.approx(112.5)
        assert s.club_path_deg == pytest.approx(-1.0)

    def test_distance_without_unit_assumes_yards_and_warns(self, tmp_path):
        path = tmp_path / "tm.csv"
        path.write_text("Club,Ball Speed,Curve\r\n7 Iron,112,5\r\n",
                        encoding="utf-8", newline="")
        info = ct.TrackmanLoadInfo()
        s = ct.load_trackman(path, info)[0]
        assert s.curve_yards == pytest.approx(5.0)
        assert any("Curve" in w for w in info.warnings)

    def test_wrong_kind_unit_drops_values(self, tmp_path):
        path = tmp_path / "tm.csv"
        path.write_text("Club,Ball Speed,Curve\r\n,[mph],[deg]\r\n7 Iron,112,5\r\n",
                        encoding="utf-8", newline="")
        info = ct.TrackmanLoadInfo()
        s = ct.load_trackman(path, info)[0]
        assert s.curve_yards is None
        assert any("not a distance unit" in w for w in info.warnings)


# ---------------------------------------------------------------------------
# OpenFlight loading: club_data merge, sources, deletions
# ---------------------------------------------------------------------------

class TestLoadOpenflightClubData:
    def test_club_data_entry_merged_into_its_shot(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5,
                        launch_angle_horizontal=-1.2,
                        launch_angle_horizontal_source="camera",
                        curve_class="fade", camera_club_path_deg=None),
            _club_data_entry(1, 1000.5, camera_club_path_deg=-2.0,
                             camera_attack_angle_deg=-3.5, dynamic_loft_deg=21.0,
                             face_to_target_deg=-0.4, face_to_path_deg=1.6,
                             calculated_spin_axis_deg=4.0, spin_axis_class="fade"),
        ])
        s = ct.load_openflight(path)[0]
        assert s.club_path_deg == pytest.approx(-2.0)
        assert s.club_path_source == "camera"
        assert s.attack_angle_deg == pytest.approx(-3.5)
        assert s.attack_angle_source == "camera"
        assert s.dynamic_loft_deg == pytest.approx(21.0)
        assert s.face_angle_deg == pytest.approx(-0.4)
        assert s.face_to_path_deg == pytest.approx(1.6)
        assert s.spin_axis_deg == pytest.approx(4.0)
        assert s.spin_axis_source == "club_data"
        assert s.spin_axis_class == "fade"
        assert s.curve_class == "fade"
        assert s.launch_angle_horizontal_source == "camera"
        assert s.raw["club_data_status"] == "done"

    def test_club_data_of_another_shot_is_not_merged(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5),
            _club_data_entry(1, 2000.0, camera_club_path_deg=-2.0),
        ])
        assert ct.load_openflight(path)[0].club_path_deg is None

    def test_ambiguous_duplicate_club_data_ignored(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5),
            _club_data_entry(1, 1000.5, camera_club_path_deg=-2.0),
            _club_data_entry(1, 1000.5, camera_club_path_deg=5.0),
        ])
        assert ct.load_openflight(path)[0].club_path_deg is None

    def test_fallbacks_fused_path_and_dplane_axis(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5,
                        experimental_fused_club_path_deg=1.5,
                        experimental_fused_attack_angle_deg=-2.0,
                        spin_axis_deg=-3.0, launch_angle_horizontal=0.4),
        ])
        s = ct.load_openflight(path)[0]
        assert (s.club_path_deg, s.club_path_source) == (1.5, "fused")
        assert (s.attack_angle_deg, s.attack_angle_source) == (-2.0, "fused")
        assert (s.spin_axis_deg, s.spin_axis_source) == (-3.0, "dplane")
        assert s.launch_angle_horizontal_source == "unknown"

    def test_camera_path_preferred_over_fused(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5,
                        camera_club_path_deg=0.5,
                        experimental_fused_club_path_deg=1.5),
        ])
        s = ct.load_openflight(path)[0]
        assert (s.club_path_deg, s.club_path_source) == (0.5, "camera")

    def test_deleted_and_demo_shots_dropped(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5),
            _shot_entry(2, "2026-10-31T15:01:00", 1060.5),
            _shot_entry(3, "2026-10-31T15:02:00", 1120.5, demo=True),
            {"ts": "x", "type": "shot_deleted", "shot_timestamp": "2026-10-31T15:01:00"},
        ])
        shots = ct.load_openflight(path)
        assert [s.shot_number for s in shots] == [1]

    def test_invalid_class_words_ignored(self, tmp_path):
        path = tmp_path / "s.jsonl"
        _write_jsonl(path, [
            _shot_entry(1, "2026-10-31T15:00:00", 1000.5,
                        curve_class="banana", spin_axis_class=7),
        ])
        s = ct.load_openflight(path)[0]
        assert s.curve_class is None and s.spin_axis_class is None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

class TestMetricStats:
    def test_known_linear_relation(self):
        tm = [-4.0, -2.0, 0.0, 2.0, 4.0]
        of = [2 * t + 1 for t in tm]  # OF = 2*TM + 1
        st = ct.metric_stats(of, tm)
        assert st["n"] == 5
        assert st["slope"] == pytest.approx(2.0)
        assert st["intercept"] == pytest.approx(1.0)
        assert st["r"] == pytest.approx(1.0)
        deltas = [a - b for a, b in zip(of, tm)]  # = t + 1
        assert st["bias"] == pytest.approx(1.0)
        assert st["mae"] == pytest.approx(sum(abs(d) for d in deltas) / 5)
        assert st["rmse"] == pytest.approx((sum(d * d for d in deltas) / 5) ** 0.5)

    def test_constant_offset(self):
        st = ct.metric_stats([1.5, 2.5, 3.5], [1.0, 2.0, 3.0])
        assert st["bias"] == pytest.approx(0.5)
        assert st["mae"] == pytest.approx(0.5)
        assert st["rmse"] == pytest.approx(0.5)
        assert st["sd"] == pytest.approx(0.0)
        assert st["slope"] == pytest.approx(1.0)

    def test_empty_and_small_samples(self):
        assert ct.metric_stats([], [])["bias"] is None
        st = ct.metric_stats([1.0, 2.0], [0.0, 1.0])
        assert st["bias"] == pytest.approx(1.0)
        assert st["slope"] is None and st["r"] is None

    def test_no_trackman_spread_gives_no_slope(self):
        st = ct.metric_stats([1.0, 2.0, 3.0], [2.0, 2.0, 2.0])
        assert st["slope"] is None


class TestClassAgreement:
    def test_agreement_and_null_models(self):
        tm = ["fade"] * 8 + ["draw"] * 2
        of = ["fade"] * 7 + ["draw"] + ["draw", "fade"]
        st = ct.class_agreement(of, tm, ct.AXIS_CLASSES)
        assert st["n"] == 10
        assert st["agreement"] == pytest.approx(0.8)
        assert st["null_majority"] == pytest.approx(0.8)
        assert st["null_majority_class"] == "fade"
        # p_of(fade)=0.8, p_tm(fade)=0.8; p_of(draw)=0.2, p_tm(draw)=0.2
        assert st["null_chance"] == pytest.approx(0.68)
        assert st["kappa"] == pytest.approx((0.8 - 0.68) / 0.32)
        assert st["confusion_of_by_tm"]["fade"]["draw"] == 1
        assert st["confusion_of_by_tm"]["draw"]["fade"] == 1

    def test_empty(self):
        st = ct.class_agreement([], [], ct.AXIS_CLASSES)
        assert st["n"] == 0 and st["agreement"] is None

    def test_perfect_single_class_has_no_kappa(self):
        st = ct.class_agreement(["fade"] * 3, ["fade"] * 3, ct.AXIS_CLASSES)
        assert st["agreement"] == 1.0 and st["kappa"] is None


def _pair(of_kw, tm_kw, club="7-iron", ball=(112.0, 112.5)):
    ts = datetime(2026, 10, 31, 15, 0, 0)
    of = ct.Shot(source="of", shot_number=1, timestamp=ts, club=club,
                 ball_speed_mph=ball[0], **of_kw)
    tm = ct.Shot(source="tm", shot_number=1, timestamp=ts, club=club,
                 ball_speed_mph=ball[1], **tm_kw)
    return ct.Pair(of=of, tm=tm, match_quality="good")


class TestComputeStats:
    def test_by_source_and_by_club(self):
        pairs = [
            _pair({"club_path_deg": -2.0, "club_path_source": "camera"},
                  {"club_path_deg": -3.0}),
            _pair({"club_path_deg": 1.0, "club_path_source": "fused"},
                  {"club_path_deg": -1.0}),
            _pair({"club_path_deg": 0.0, "club_path_source": "camera"},
                  {"club_path_deg": -1.0}, club="pw"),
        ]
        stats = ct.compute_metric_stats(pairs)["club_path"]
        assert stats["all"]["n"] == 3
        assert stats["all"]["bias"] == pytest.approx((1 + 2 + 1) / 3)
        assert stats["by_source"]["camera"]["n"] == 2
        assert stats["by_source"]["camera"]["bias"] == pytest.approx(1.0)
        assert stats["by_source"]["fused"]["bias"] == pytest.approx(2.0)
        assert stats["by_club"]["pw"]["n"] == 1

    def test_mismatched_pairs_excluded(self):
        bad = _pair({"club_path_deg": 9.0}, {"club_path_deg": 0.0})
        bad.match_quality = "ball_speed_mismatch"
        good = _pair({"club_path_deg": 1.0}, {"club_path_deg": 0.0})
        stats = ct.compute_metric_stats([bad, good])["club_path"]["all"]
        assert stats["n"] == 1 and stats["bias"] == pytest.approx(1.0)

    def test_class_stats_collapse_to_direction(self):
        pairs = [
            _pair({"curve_class": "slice"}, {"curve_class": "fade"}),
            _pair({"curve_class": "draw"}, {"curve_class": "hook"}),
            _pair({"curve_class": "straight"}, {"curve_class": "fade"}),
        ]
        stats = ct.compute_class_stats(pairs)
        assert stats["curve_class"]["agreement"] == pytest.approx(0.0)
        assert stats["curve_direction"]["agreement"] == pytest.approx(2 / 3)

    def test_coverage(self):
        pairs = [_pair({"club_path_deg": 1.0}, {"club_path_deg": 0.0}),
                 _pair({}, {"club_path_deg": 0.0})]
        cov = ct.coverage(pairs)["club_path"]
        assert cov == {"pairs": 2, "of": 1, "tm": 2}


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

class TestMainClubData:
    def test_full_pipeline_with_club_data(self, tmp_path, capsys):
        of_path = tmp_path / "session.jsonl"
        tm_path = tmp_path / "tm.csv"
        out_path = tmp_path / "cmp.csv"
        json_path = tmp_path / "cmp.json"
        entries = []
        tm_rows = []
        # Five 7-iron shots; OF club path reads 1 deg more in-to-out than TM.
        for i, (path_tm, axis_tm, ld_tm) in enumerate(
                [(-3.0, 5.0, -1.0), (-1.0, 3.0, 0.5), (0.0, -1.0, 1.0),
                 (1.0, -4.0, 2.0), (-2.0, 9.0, -0.5)], start=1):
            impact = 1000.0 + i
            ts = f"2026-10-31T15:0{i}:00"
            entries.append(_shot_entry(
                i, ts, impact, launch_angle_horizontal=ld_tm + 0.2,
                launch_angle_horizontal_source="camera",
                curve_class=ct._of_curve_class(axis_tm)))
            entries.append(_club_data_entry(
                i, impact, camera_club_path_deg=path_tm + 1.0,
                calculated_spin_axis_deg=axis_tm - 0.5,
                face_to_path_deg=2.0, face_to_target_deg=0.0,
                camera_attack_angle_deg=-4.0, dynamic_loft_deg=22.0))
            tm_rows.append(_normalized_row(**{
                "Date": f"10/31/2026 3:0{i}:00 PM", "Club Path": str(path_tm),
                "Spin Axis": str(axis_tm), "Launch Direction": str(ld_tm),
                "Ball Speed": "112.5"}))
        _write_jsonl(of_path, entries)
        _write_normalized(tm_path, tm_rows)

        rc = ct.main(["--openflight", str(of_path), "--trackman", str(tm_path),
                      "--output", str(out_path), "--summary-json", str(json_path)])
        assert rc == 0

        with open(out_path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 5
        assert all(r["match_quality"] == "good" for r in rows)
        assert float(rows[0]["club_path_delta"]) == pytest.approx(1.0)
        assert rows[0]["club_path_source_of"] == "camera"
        assert float(rows[0]["curve_tm"]) == pytest.approx(10.0)
        assert rows[0]["curve_class_tm"] == "fade"

        summary = json.loads(json_path.read_text(encoding="utf-8"))
        club_path = summary["metrics"]["club_path"]["all"]
        assert club_path["n"] == 5
        assert club_path["bias"] == pytest.approx(1.0)
        assert club_path["slope"] == pytest.approx(1.0)
        assert summary["metrics"]["launch_h"]["all"]["bias"] == pytest.approx(0.2)
        assert summary["metrics"]["spin_axis"]["all"]["bias"] == pytest.approx(-0.5)
        assert summary["classes"]["curve_class"]["agreement"] == pytest.approx(1.0)
        assert summary["coverage"]["face_to_path"]["of"] == 5
        assert summary["trackman_file"]["units"]["curve_yards"] == "ft"
        assert summary["pairing"]["good"] == 5

        out = capsys.readouterr().out
        assert "METRIC AGREEMENT" in out
        assert "CLASS AGREEMENT" in out
        assert "club path" in out
