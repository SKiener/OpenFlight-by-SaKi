"""Tests for scripts/analysis/range_session_report.py (synthetic fixtures)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import range_session_report as rsr  # noqa: E402  pylint: disable=wrong-import-position

MPH_TO_KPH = 1.0 / rsr.KPH_TO_MPH
TT_HEADER = [
    "Shot number", "Date", "Classification", "Club type", "Club name", "Carry", "Total",
    "Club speed", "Ball speed", "Horizontal Launch Angle", "Vertical Launch angle",
    "Backspin", "Sidespin", "Curve",
]
TT_UNITS = ["", "", "", "", "", "[m]", "[m]", "[kph]", "[kph]", "[°]", "[°]", "[rpm]", "[rpm]", "[m]"]


def tt_row(shot, date, club, ball_mph, *, cls="Baby Fade", vla=20.0, hla="2.000 L", carry=130.0):
    return [str(shot), date, cls, club, "Default", f"{carry:.3f}", f"{carry + 3:.3f}", "120.0",
            f"{ball_mph * MPH_TO_KPH:.3f}", hla, f"{vla:.3f}", "5000", "200 R", "3.000 R"]


def write_tt(path: Path, rows) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(TT_HEADER)
        writer.writerow(TT_UNITS)
        writer.writerows(rows)
    return path


def of_shot(n, club, ball_mph, *, vla=24.0, hla=-3.0, carry_yd=140.0, ts=None, **extra):
    entry = {
        "type": "shot_detected", "shot_number": n, "club": club, "ball_speed_mph": ball_mph,
        "timestamp": ts or f"2026-01-01T10:{n:02d}:00", "launch_angle_vertical": vla,
        "launch_angle_vertical_source": "radar", "launch_angle_horizontal": hla,
        "launch_angle_horizontal_source": "radar", "carry_spin_adjusted": carry_yd,
        "inclinometer": {"calibrated_pitch_deg": 1.0},
    }
    entry.update(extra)
    return entry


def trigger(edge_s, accepted, reason=None, shadow="unclear", shadow_reason="too_dark"):
    return {"type": "trigger_event", "ts": "2026-01-01T10:00:00", "accepted": accepted,
            "reason": "accepted" if accepted else reason, "gate_edge_timestamp": edge_s,
            "gate_shadow_decision": shadow, "gate_shadow_reason": shadow_reason}


def write_jsonl(path: Path, entries) -> Path:
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
        handle.write("not json\n")
    return path


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("4.300 L", -4.3), ("504 R", 504.0), ("0", 0.0), ("-1.858", -1.858), ("", None), (None, None),
    ("3.75° closed", None),
])
def test_parse_signed(text, expected):
    assert rsr.parse_signed(text) == expected


@pytest.mark.parametrize("club, expected", [
    ("8-iron", "8 Iron"), ("7-wood", "7 Wood"), ("4-hybrid", "4 Hybrid"), ("driver", "Driver"),
    ("mini-driver", "2 Wood"), ("wedge-44", "Pitching Wedge"), ("wedge-54", "Sand Wedge"), (None, None),
])
def test_of_club_to_tt(club, expected):
    assert rsr.of_club_to_tt(club) == expected


def test_of_club_to_tt_override():
    assert rsr.of_club_to_tt("7-wood", {"7-wood": "8 Iron"}) == "8 Iron"


@pytest.mark.parametrize("cls, expected", [
    ("Baby Fade", "fade"), ("Huge Pull Hook", "hook"), ("Pull Hook", "hook"), ("Straight Fade", "fade"),
    ("Underfade", "fade"), ("Overfade", "fade"), ("Power Draw", "draw"), ("Underdraw", "draw"),
    ("Push Slice", "slice"), ("Straight Slice", "slice"), ("Pull", "straight"), ("Dead Straight", "straight"),
    ("Huge Pull", "straight"), ("Top", None), ("Shank", None), ("", None),
])
def test_tt_class_mapping(cls, expected):
    assert rsr.tt_class(cls) == expected


@pytest.mark.parametrize("axis, expected", [
    (0.0, "straight"), (-2.4, "straight"), (2.5, "fade"), (-7.9, "draw"), (8.0, "slice"), (-12, "hook"),
    (None, None), (float("nan"), None),
])
def test_class_from_axis_matches_curve_class_limits(axis, expected):
    assert rsr.class_from_axis(axis) == expected


def test_load_tt_skips_unit_row_and_orders_by_shot(tmp_path):
    path = write_tt(tmp_path / "tt.csv", [
        tt_row(2, "2026-10-30T16:55:27.000Z", "8 Iron", 110),
        tt_row(1, "2026-10-30T16:55:27.000Z", "8 Iron", 108, hla="4.300 L"),
        tt_row(1, "2026-10-25T11:29:08.000Z", "Driver", 150),
    ])
    shots = rsr.load_tt(path)
    assert len(shots) == 3
    day, picked = rsr.select_tt_day(shots, "2026-10-30")
    assert day == "2026-10-30"
    assert [s.shot for s in picked] == [1, 2]
    assert picked[0].hla == pytest.approx(-4.3)
    assert picked[0].ball_mph == pytest.approx(108, abs=1e-3)


def test_select_tt_day_guesses_from_session_name_or_fails(tmp_path):
    shots = rsr.load_tt(write_tt(tmp_path / "tt.csv", [
        tt_row(1, "2026-10-30T16:55:27.000Z", "8 Iron", 108),
        tt_row(1, "2026-10-25T11:29:08.000Z", "Driver", 150),
    ]))
    day, picked = rsr.select_tt_day(shots, None, Path("session_20261025_101010_range.jsonl"))
    assert day == "2026-10-25" and len(picked) == 1
    with pytest.raises(SystemExit):
        rsr.select_tt_day(shots, None, Path("session_20261019_181202_range.jsonl"))


def test_load_session_skips_deleted_and_demo(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        of_shot(1, "8-iron", 108, ts="2026-01-01T10:01:00"),
        of_shot(2, "8-iron", 109, ts="2026-01-01T10:02:00"),
        of_shot(3, "8-iron", 110, demo=True),
        {"type": "shot_deleted", "shot_timestamp": "2026-01-01T10:02:00"},
        trigger(100.0, True),
    ])
    session = rsr.load_session(path)
    assert [s.shot for s in session.shots] == [1]
    assert session.deleted == 1 and session.demo == 1
    assert len(session.triggers) == 1


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


def test_align_handles_missing_shot_in_the_middle():
    tt = [108.0, 110.0, 154.0, 105.0, 112.0]
    of = [107.8, 109.5, 105.2, 111.6]  # the 154 mph shot is missing
    assert rsr.align(of, tt) == [(0, 0), (1, 1), (2, 3), (3, 4)]


def test_align_handles_phantom_and_rejects_far_pairs():
    tt = [108.0, 110.0]
    of = [30.0, 108.2, 125.0, 110.3]  # a 30 mph phantom and one far off
    assert rsr.align(of, tt) == [(1, 0), (3, 1)]
    assert rsr.align([100.0], [105.0]) == []  # 5 mph > max_dv


def test_align_unknown_speed_never_pairs():
    assert rsr.align([None, 108.0], [108.0]) == [(1, 0)]


def _shots(entries):
    return [
        rsr.OFShot(index=i, shot=e["shot_number"], timestamp=None, club=e["club"], ball_mph=e["ball_speed_mph"],
                   club_mph=None, vla=e["launch_angle_vertical"], vla_source="radar",
                   hla=e["launch_angle_horizontal"], hla_source="radar", hla_confidence=None,
                   carry_yd=e["carry_spin_adjusted"], carry_field="carry_spin_adjusted",
                   curve_class=e.get("curve_class"), spin_axis_deg=None, spin_axis_class=e.get("spin_axis_class"),
                   club_path_deg=None, pitch_deg=1.0)
        for i, e in enumerate(entries)
    ]


def test_pair_session_pools_by_club_and_reports_missed_and_phantoms(tmp_path):
    tt = rsr.load_tt(write_tt(tmp_path / "tt.csv", [
        tt_row(1, "D", "8 Iron", 108), tt_row(2, "D", "8 Iron", 110),
        tt_row(3, "D", "Driver", 150), tt_row(4, "D", "Driver", 154), tt_row(5, "D", "Driver", 147),
    ]))
    of = _shots([of_shot(1, "8-iron", 107.5), of_shot(2, "8-iron", 109.4), of_shot(3, "driver", 148.2),
                 of_shot(4, "driver", 30.0), of_shot(5, "driver", 145.3)])
    pairing = rsr.pair_session(of, tt)
    assert [(o.shot, t.shot) for o, t in pairing.pairs] == [(1, 1), (2, 2), (3, 3), (5, 5)]
    assert [t.shot for t in pairing.missed] == [4]
    assert [o.shot for o in pairing.phantoms] == [4]


def test_pair_session_club_map_merges_mislabelled_club(tmp_path):
    tt = rsr.load_tt(write_tt(tmp_path / "tt.csv", [
        tt_row(1, "D", "8 Iron", 108), tt_row(2, "D", "8 Iron", 135), tt_row(3, "D", "8 Iron", 109),
    ]))
    of = _shots([of_shot(1, "8-iron", 108.1), of_shot(2, "7-wood", 134.5), of_shot(3, "8-iron", 109.2)])
    assert len(rsr.pair_session(of, tt).pairs) == 2
    pairing = rsr.pair_session(of, tt, {"7-wood": "8 Iron"})
    assert [(o.shot, t.shot) for o, t in pairing.pairs] == [(1, 1), (2, 2), (3, 3)]


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_linfit_exact_line():
    slope, offset, r = rsr.linfit([0.0, 1.0, 2.0, 3.0], [1.0, 1.5, 2.0, 2.5])
    assert slope == pytest.approx(0.5) and offset == pytest.approx(1.0) and r == pytest.approx(1.0)
    assert rsr.linfit([1.0, 2.0], [1.0, 2.0]) is None
    assert rsr.linfit([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None


def test_curve_stats_hit_rates_and_null_models(tmp_path):
    tt = rsr.load_tt(write_tt(tmp_path / "tt.csv", [
        tt_row(1, "D", "8 Iron", 108, cls="Baby Fade"), tt_row(2, "D", "8 Iron", 110, cls="Pull"),
        tt_row(3, "D", "8 Iron", 112, cls="Pull Hook"), tt_row(4, "D", "8 Iron", 109, cls="Underfade"),
        tt_row(5, "D", "Driver", 150, cls="Slice"),
    ]))
    of = _shots([
        of_shot(1, "8-iron", 108, hla=3.0, curve_class="fade"),       # hit; N2 fade hit
        of_shot(2, "8-iron", 110, hla=0.0, spin_axis_class="draw"),   # miss (side miss too); N2 straight hit
        of_shot(3, "8-iron", 112, hla=-9.0, curve_class="draw"),      # 5-class miss, side hit; N2 hook hit
        of_shot(4, "8-iron", 109, hla=-3.0),                          # not covered; N2 draw miss
        of_shot(5, "driver", 150, curve_class="slice"),               # woods are not counted
    ])
    cs = rsr.curve_stats(rsr.pair_session(of, tt).pairs)
    assert (cs.irons, cs.covered, cs.scored) == (4, 3, 3)
    assert (cs.hits5, cs.hits3) == (1, 2)
    assert cs.n1_class == "fade" and (cs.n1_hits, cs.n_null) == (2, 4)
    assert (cs.n2_hits, cs.n2_scored) == (3, 4)


def test_trigger_stats_rate_window_and_shadow():
    events = [
        trigger(0.0, False, "no_outbound_speed", shadow_reason="no_anchor"),
        trigger(2.0, True, shadow="departed", shadow_reason="ball_left_spot"),  # 2 s after a false one
        trigger(30.0, False, "ops_busy"),
        trigger(40.0, True),                                                    # 10 s after
        trigger(120.0, False, "no_outbound_speed"),
    ]
    ts = rsr.trigger_stats(events)
    assert (ts.events, ts.accepted, ts.rejected) == (5, 2, 3)
    assert ts.duration_min == pytest.approx(2.0)
    assert ts.false_per_min == pytest.approx(1.5)
    assert ts.real_after_false == 1
    assert ts.reasons == {"no_outbound_speed": 2, "ops_busy": 1}
    assert ts.shadow_real == {"departed": 1, "unclear": 1}
    assert ts.shadow_false == {"unclear": 3}
    assert ts.shadow_reason_false["no_anchor"] == 1


def test_kiosk_stats(tmp_path):
    log = tmp_path / "kiosk.log"
    log.write_text(
        "x - [GATE] edge #1 camera shadow -> would pulse: unclear (no_anchor)\n"
        "x - [GATE] edge #2 camera shadow -> would pulse: departed at +5.6 ms\n"
        "x - [CAMERA] Trigger check #2: departed at +17 ms (contrast 134)\n"
        "x - [CAMERA] Trigger check #1: unclear (occluded) (contrast 190)\n"
        "noise\n", encoding="utf-8")
    ks = rsr.kiosk_stats(log)
    assert ks.edges == 2
    assert ks.shadow == {"unclear": 1, "departed": 1}
    assert ks.shadow_reasons == {"no_anchor": 1}
    assert ks.checks == {"departed": 1, "unclear": 1}


@pytest.mark.parametrize("value, digits, sign, expected", [
    (-2.123, 1, True, "−2,1"), (4.2, 1, True, "+4,2"), (1.432, 2, False, "1,43"), (None, 1, False, "–"),
])
def test_num_german_format(value, digits, sign, expected):
    assert rsr.num(value, digits, sign) == expected


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_cli_end_to_end(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [
        tt_row(1, day, "8 Iron", 108, vla=20.0, carry=130.0),
        tt_row(2, day, "8 Iron", 110, vla=21.0, carry=135.0),
        tt_row(3, day, "Driver", 150, vla=10.0, carry=200.0, cls="Huge Pull Hook"),
        tt_row(4, day, "Driver", 154, vla=10.0, carry=210.0),
        tt_row(5, day, "Driver", 148, vla=10.0, carry=195.0),
        tt_row(1, "2026-10-25T11:29:08.000Z", "Driver", 140),  # another session in the export
    ])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        trigger(1000.0, False, "no_outbound_speed"),
        of_shot(1, "8-iron", 107.5, vla=24.0, carry_yd=140.0), trigger(1060.0, True),
        of_shot(2, "8-iron", 109.5, vla=25.0, carry_yd=145.0), trigger(1120.0, True),
        of_shot(3, "driver", 148.0, vla=14.0, carry_yd=250.0), trigger(1180.0, True),
        of_shot(4, "driver", 146.0, vla=14.0, carry_yd=245.0), trigger(1240.0, True),
        trigger(1300.0, False, "ops_busy"),
    ])
    out = tmp_path / "report.md"
    pairs_csv = tmp_path / "pairs.csv"
    assert rsr.main([str(session), str(tt), "--out", str(out), "--pairs-csv", str(pairs_csv)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "# Range-Session 2026-10-30" in text
    assert "4 Paare, 1 TT-Schläge ohne OF-Schuss (verpasst), 0 OF-Schüsse ohne TT-Schlag" in text
    assert "TT#4 Driver 154,0 mph" in text
    assert "| driver | 2 | 1 | 0 | −2,0 / 2,0 | +4,0 / 0,0 |" in text
    assert "| Fehlauslöser pro Minute | 0,40 |" in text  # 2 false in 5 min
    assert "Abdeckung:** 0/2 (0 %)" in text
    with open(pairs_csv, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["tt_shot"] for r in rows] == ["1", "2", "3", "5"]


# ---------------------------------------------------------------------------
# Carry variants
# ---------------------------------------------------------------------------


def variants_entry(n, impact, carries, *, null=False):
    names = ["tour", "range_ball_spin", "range_ball_spin_drag", "range_ball_spin_drag_half"]
    return {
        "type": "carry_variants", "shot_number": n, "shot_timestamp": impact,
        "carry_variants": None if null else {
            name: {"spin_rpm": 4000, "carry_m": carry, "apex_m": 20.0} for name, carry in zip(names, carries)
        },
        "reason": "failed: x" if null else None,
    }


def test_load_session_joins_carry_variants_by_number_and_impact(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        of_shot(1, "8-iron", 108, impact_timestamp=100.5),
        variants_entry(1, 100.5, [120.0, 130.0, 129.5, 129.8]),
        of_shot(2, "8-iron", 109, impact_timestamp=160.5),
        variants_entry(2, 999.0, [1.0, 1.0, 1.0, 1.0]),  # another shot's timestamp
        variants_entry(2, 160.5, [121.0, 131.0, 130.0, 130.5]),
        variants_entry(2, 160.5, [122.0, 132.0, 131.0, 131.5]),  # re-flown: the last one counts
        of_shot(3, "8-iron", 110, impact_timestamp=220.5),
        variants_entry(3, 220.5, [], null=True),
        of_shot(4, "8-iron", 111),
    ])
    shots = rsr.load_session(path).shots
    assert shots[0].variant_carry_m == {
        "tour": 120.0, "range_ball_spin": 130.0,
        "range_ball_spin_drag": 129.5, "range_ball_spin_drag_half": 129.8,
    }
    assert shots[1].variant_carry_m["tour"] == 122.0
    assert shots[2].variant_carry_m is None
    assert shots[3].variant_carry_m is None


def test_a_failed_last_variants_entry_drops_the_earlier_flights_values(tmp_path):
    # The last entry shadows the re-flown shot; an earlier one belongs to the first flight.
    path = write_jsonl(tmp_path / "s.jsonl", [
        of_shot(1, "8-iron", 108, impact_timestamp=100.5),
        variants_entry(1, 100.5, [120.0, 130.0, 129.5, 129.8]),
        {"type": "shot_updated", "shot_number": 1, "impact_timestamp": 100.5, "carry_spin_adjusted": 150.0},
        variants_entry(1, 100.5, [], null=True),
    ])
    (shot,) = rsr.load_session(path).shots
    assert shot.variant_carry_m is None


def test_live_carry_of_the_variants_is_the_re_flown_one(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        of_shot(1, "driver", 148, carry_yd=230.0, impact_timestamp=100.5),
        variants_entry(1, 100.5, [210.0, 205.0, 195.0, 199.0]),
        {"type": "shot_updated", "shot_number": 1, "impact_timestamp": 999.0, "carry_spin_adjusted": 1.0},
        {"type": "shot_updated", "shot_number": 1, "impact_timestamp": 100.5, "carry_spin_adjusted": 226.0},
        {"type": "shot_updated", "shot_number": 1, "impact_timestamp": 100.5, "carry_spin_adjusted": 224.0},
        variants_entry(1, 100.5, [209.0, 204.0, 194.0, 198.0]),
        of_shot(2, "driver", 149, carry_yd=232.0, impact_timestamp=160.5),
        variants_entry(2, 160.5, [211.0, 206.0, 196.0, 200.0]),
    ])
    first, second = rsr.load_session(path).shots
    # The Carry section keeps the shot_detected carry; the variants' live carry is the last flight's.
    assert (first.carry_yd, first.final_carry_yd) == (230.0, 224.0)
    assert first.variant_carry_m["tour"] == 209.0
    assert (second.carry_yd, second.final_carry_yd) == (232.0, 232.0)


def test_report_shows_carry_variants_per_club(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [
        tt_row(1, day, "8 Iron", 108, carry=130.0),
        tt_row(2, day, "8 Iron", 110, carry=134.0),
        tt_row(3, day, "Driver", 150, vla=10.0, carry=200.0),
    ])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "8-iron", 107.5, carry_yd=120.0 / rsr.YD_TO_M, impact_timestamp=10.0),
        variants_entry(1, 10.0, [120.0, 132.0, 131.0, 131.5]),
        of_shot(2, "8-iron", 109.5, carry_yd=124.0 / rsr.YD_TO_M, impact_timestamp=20.0),
        variants_entry(2, 20.0, [124.0, 132.0, 133.0, 133.5]),
        of_shot(3, "driver", 148.0, carry_yd=230.0 / rsr.YD_TO_M, impact_timestamp=30.0),
        variants_entry(3, 30.0, [230.0, 226.0, 210.0, 214.0]),
    ])
    out = tmp_path / "report.md"
    assert rsr.main([str(session), str(tt), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")

    assert "## Carry-Varianten" in text
    assert "3/3 (100 %) Paare haben einen Eintrag" in text
    assert "- 0 Paare mit unsicherer Ballgeschwindigkeit" in text
    assert ("| Schläger | n | Live | Tour-Spin | Rangeball-Spin | Rangeball-Spin + Drag 0,3546 |"
            " Rangeball-Spin + Drag 0,225 |") in text
    # 8-iron: live/tour −10,0 MAE 10,0; range-ball +0,0 MAE 2,0; drag +0,0 / 1,0; half +0,5 / 1,0.
    assert "| 8-iron | 2 | −10,0 / 10,0 | −10,0 / 10,0 | +0,0 / 2,0 | +0,0 / 1,0 | +0,5 / 1,0 |" in text
    assert "| driver | 1 | +30,0 / 30,0 | +30,0 / 30,0 | +26,0 / 26,0 | +10,0 / 10,0 | +14,0 / 14,0 |" in text
    assert "| **Gesamt** | 3 |" in text


def test_report_leaves_uncertain_ball_speeds_out_of_the_carry_variants(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [
        tt_row(1, day, "8 Iron", 108, carry=130.0),
        tt_row(2, day, "8 Iron", 110, carry=134.0),
    ])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "8-iron", 107.5, carry_yd=120.0 / rsr.YD_TO_M, impact_timestamp=10.0),
        variants_entry(1, 10.0, [120.0, 132.0, 131.0, 131.5]),
        of_shot(2, "8-iron", 109.5, carry_yd=40.0 / rsr.YD_TO_M, impact_timestamp=20.0,
                ball_speed_status="uncertain", ball_speed_status_reason="club_like_track"),
        variants_entry(2, 20.0, [40.0, 41.0, 41.0, 41.0]),
    ])
    assert [s.ball_speed_status for s in rsr.load_session(session).shots] == [None, "uncertain"]
    out = tmp_path / "report.md"
    assert rsr.main([str(session), str(tt), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")

    assert "2/2 (100 %) Paare haben einen Eintrag" in text
    assert "- 1 Paare mit unsicherer Ballgeschwindigkeit" in text
    assert "| 8-iron | 1 | −10,0 / 10,0 | −10,0 / 10,0 | +2,0 / 2,0 | +1,0 / 1,0 | +1,5 / 1,5 |" in text
    assert "| **Gesamt** | 1 |" in text
    # The Carry section leaves it out too.
    assert "- **8-iron:** Bias −10,0 m, MAE 10,0 (n=1)." in text


def test_report_leaves_uncertain_ball_speeds_out_of_every_of_tt_statistic(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [
        tt_row(1, day, "8 Iron", 108, vla=20.0, hla="2.000 L", carry=130.0),
        tt_row(2, day, "8 Iron", 110, vla=21.0, carry=134.0),
        tt_row(3, day, "8 Iron", 112, vla=22.0, hla="1.000 R", carry=138.0),
        tt_row(4, day, "Driver", 150, vla=10.0, carry=200.0),
        tt_row(5, day, "Driver", 152, vla=10.0, hla="3.000 L", carry=205.0),
    ])
    wrong_start = {"ball_speed_status": "uncertain", "ball_speed_status_reason": "club_like_track"}
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "8-iron", 107.5, vla=24.0, hla=-3.0, carry_yd=128.0 / rsr.YD_TO_M, curve_class="fade"),
        of_shot(2, "8-iron", 109.5, vla=45.0, hla=25.0, carry_yd=40.0 / rsr.YD_TO_M, curve_class="hook",
                **wrong_start),
        of_shot(3, "8-iron", 111.5, vla=24.0, hla=3.0, carry_yd=136.0 / rsr.YD_TO_M, curve_class="fade"),
        of_shot(4, "driver", 148.0, vla=30.0, hla=20.0, carry_yd=60.0 / rsr.YD_TO_M, **wrong_start),
        of_shot(5, "driver", 151.0, vla=12.0, hla=-5.0, carry_yd=210.0 / rsr.YD_TO_M),
    ])
    out = tmp_path / "report.md"
    pairs_csv = tmp_path / "pairs.csv"
    assert rsr.main([str(session), str(tt), "--out", str(out), "--pairs-csv", str(pairs_csv)]) == 0
    text = out.read_text(encoding="utf-8")

    # Pairing is unchanged: the uncertain shots still pair and count as pairs.
    assert "5 Paare, 0 TT-Schläge ohne OF-Schuss (verpasst), 0 OF-Schüsse ohne TT-Schlag" in text
    with open(pairs_csv, encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 5
    # One line per section: overview, ball speed, VLA, start direction, carry (2 pairs) and curve
    # (irons only: 1).
    assert text.count("- 2 Paare mit unsicherer Ballgeschwindigkeit (`ball_speed_status` `uncertain`,"
                      " falscher Start) nicht gewertet.") == 5
    assert text.count("- 1 Paare mit unsicherer Ballgeschwindigkeit") == 1
    sections = text.split("\n## ")
    for title in ("Übersicht pro Schläger", "Ballgeschwindigkeit", "Vertikaler Abflugwinkel (VLA)",
                  "Horizontale Startrichtung", "Carry", "Kurve (nur Eisen und Wedges)"):
        [section] = [part for part in sections if part.startswith(title + "\n")]
        assert section.count("Paare mit unsicherer Ballgeschwindigkeit") == 1, title

    # Overview: n stays the pair count, the statistics use the certain pairs only.
    assert "| 8-iron | 3 | 0 | 0 | −0,5 / 0,5 | +3,0 / 1,4 | −2,0 / 2,0 | – (n=2) |" in text
    assert ("| **Gesamt** | 5 | 0 | 0 | −0,7 / 0,7 | +2,7 / 1,2 | +0,3 / 3,0 |"
            " 2,00 / +1,0° (r=1,00, n=3) |") in text
    assert "- **8-iron:** Bias −0,5 mph, MAE 0,5, sd 0,0, max |Δ| 0,5 (n=2)." in text
    assert "- **Gesamt:** Bias −0,7 mph, MAE 0,7 (n=3)." in text
    assert "| radar | 8-iron | 2 | +3,0 | 1,4 |" in text
    assert "| **alle** | **alle** | 3 | +2,7 | 1,2 |" in text
    # With the two wrong starts (25° and 20° against −2°) there would be no clean line.
    assert "| **alle** | **alle** | 2,00 / +1,0° (r=1,00, n=3) | 1,7 |" in text
    assert "- **8-iron:** Bias −2,0 m, MAE 2,0 (n=2)." in text
    assert "- **Gesamt:** Bias +0,3 m, MAE 3,0 (n=3)." in text
    assert "- **Abdeckung:** 2/2 (100 %) gepaarte Eisen mit OF-Kurvenklasse." in text
    assert "5 Klassen 2/2 (100 %)" in text


def test_report_without_uncertain_ball_speeds_counts_every_pair(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [tt_row(1, day, "8 Iron", 108, carry=130.0)])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "8-iron", 107.5, carry_yd=128.0 / rsr.YD_TO_M, ball_speed_status="ok"),
    ])
    out = tmp_path / "report.md"
    assert rsr.main([str(session), str(tt), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.count("- 0 Paare mit unsicherer Ballgeschwindigkeit") == 6
    assert "- **Gesamt:** Bias −2,0 m, MAE 2,0 (n=1)." in text


def test_report_compares_a_re_flown_shot_with_its_re_flown_variants(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [tt_row(1, day, "Driver", 150, vla=10.0, carry=200.0)])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "driver", 148.0, carry_yd=230.0 / rsr.YD_TO_M, impact_timestamp=30.0),
        variants_entry(1, 30.0, [230.0, 226.0, 210.0, 214.0]),
        {"type": "shot_updated", "shot_number": 1, "impact_timestamp": 30.0,
         "carry_spin_adjusted": 222.0 / rsr.YD_TO_M},
        variants_entry(1, 30.0, [222.0, 218.0, 203.0, 207.0]),
    ])
    out = tmp_path / "report.md"
    assert rsr.main([str(session), str(tt), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")

    # Carry section: the shot_detected carry. Variants: the re-flown carry and variants.
    assert "- **driver:** Bias +30,0 m, MAE 30,0 (n=1)." in text
    assert "| driver | 1 | +22,0 / 22,0 | +22,0 / 22,0 | +18,0 / 18,0 | +3,0 / 3,0 | +7,0 / 7,0 |" in text


def test_report_without_carry_variants_has_no_section(tmp_path):
    day = "2026-10-30T16:55:27.000Z"
    tt = write_tt(tmp_path / "tt.csv", [tt_row(1, day, "8 Iron", 108, carry=130.0)])
    session = write_jsonl(tmp_path / "session_20261030_142447_range.jsonl", [
        of_shot(1, "8-iron", 107.5, impact_timestamp=10.0),
        variants_entry(1, 10.0, [], null=True),
    ])
    out = tmp_path / "report.md"
    assert rsr.main([str(session), str(tt), "--out", str(out)]) == 0
    assert "Carry-Varianten" not in out.read_text(encoding="utf-8")
