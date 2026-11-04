"""Saved sessions survive restart without becoming the active session."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from openflight import ballistics, rollout, session_history

NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
SHOT = {
    "type": "shot_detected",
    "shot_number": 1,
    "timestamp": NOW.isoformat(),
    "club": "7iron",
    "ball_speed_mph": 103,
    "estimated_carry_yards": 140,
    "readings": [123],
    "camera_replay": {"id": "expired"},
}
PATH = {
    "unit": "yd",
    "points": [[0.0, 0.0, 0.0, 0.0], [0.05, 2.41, 0.13, 0.86], [5.2, 135.6, 7.66, 0.0]],
}
ROLL = {
    "unit": "yd",
    "points": [[5.2, 135.6, 7.66, 0.0], [5.5, 137.4, 7.78, 0.21], [6.3, 139.2, 7.9, 0.0]],
}
ROLLED = {
    **SHOT,
    "flight_path": PATH,
    "roll_path": ROLL,
    "roll_yards": 3.61,
    "total_yards": 139.21,
}


def write_session(root, age=0, name=None, shots=None):
    start = NOW - timedelta(days=age)
    name = name or f"session_{start:%Y%m%d_%H%M%S}_range"
    entries = [{"type": "session_start", "start_time": start.isoformat()}]
    entries += shots if shots is not None else [{**SHOT, "timestamp": start.isoformat()}]
    path = root / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return name


def test_last_30_days_are_read_only_and_newest_first(tmp_path):
    today = write_session(tmp_path)
    old = write_session(tmp_path, age=31)
    edge = write_session(tmp_path, age=30)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    sessions = session_history.list_sessions(tmp_path, now=NOW)
    assert [s["id"] for s in sessions] == [today, edge]
    assert sessions[0]["shot_count"] == 1
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    with pytest.raises(FileNotFoundError):
        session_history.get_session(tmp_path, old, now=NOW)


def test_detail_is_compact_and_keeps_final_measurements(tmp_path):
    name = write_session(tmp_path)
    result = session_history.get_session(tmp_path, name, now=NOW)
    assert result["shots"][0]["ball_speed_mph"] == 103
    assert "readings" not in result["shots"][0]
    assert "camera_replay" not in result["shots"][0]


def test_partial_or_damaged_lines_do_not_hide_saved_shots(tmp_path):
    name = write_session(tmp_path)
    path = tmp_path / f"{name}.jsonl"
    with path.open("a") as handle:
        handle.write('invalid\n[]\n{"type": "shot_detected", "ball_speed_mph": NaN}\n{"type":')
    result = session_history.get_session(tmp_path, name, now=NOW)
    assert result["shot_count"] == 1
    assert len(result["shots"]) == 1


def test_empty_sessions_are_hidden_and_changed_files_refresh(tmp_path):
    name = write_session(tmp_path, shots=[])
    assert session_history.list_sessions(tmp_path, now=NOW) == []
    write_session(tmp_path, name=name)
    assert len(session_history.list_sessions(tmp_path, now=NOW)) == 1


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "radar_raw_20260930"])
def test_untrusted_session_ids_cannot_read_other_files(tmp_path, name):
    with pytest.raises(FileNotFoundError):
        session_history.get_session(tmp_path, name, now=NOW)


def test_symlinks_are_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    name = write_session(outside)
    (tmp_path / f"{name}.jsonl").symlink_to(outside / f"{name}.jsonl")
    assert session_history.list_sessions(tmp_path, now=NOW) == []
    with pytest.raises(FileNotFoundError):
        session_history.get_session(tmp_path, name, now=NOW)


def test_directory_without_logs_is_empty(tmp_path):
    assert session_history.list_sessions(tmp_path / "missing", now=NOW) == []


def test_history_preserves_actual_carry_inputs_without_backfilling_old_shots(tmp_path):
    name = write_session(
        tmp_path,
        shots=[
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "spin_rpm": 4669,
                "carry_spin_rpm": 7500,
                "carry_spin_source": "club_typical",
                "carry_model": "ballistic",
            },
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
            },
        ],
    )
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert shots[0]["carry_spin_rpm"] == 7500
    assert shots[0]["carry_spin_source"] == "club_typical"
    assert shots[0]["carry_model"] == "ballistic"
    assert not shots[1].get("carry_spin_source")
    assert shots[1].get("carry_spin_rpm") is None


def test_history_keeps_landing_offset_and_direction_confidence(tmp_path):
    name = write_session(
        tmp_path,
        shots=[
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "carry_spin_adjusted": 140,
                "carry_lateral_yards": -6.4,
                "apex_yards": 29.6,
                "launch_angle_horizontal": -2.6,
                "launch_angle_horizontal_confidence": 0.31,
            },
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "carry_lateral_yards": "left",
                "apex_yards": float("inf"),
                "launch_angle_horizontal_confidence": float("nan"),
            },
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
            },
        ],
    )
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert shots[0]["carry_lateral_yards"] == -6.4
    assert shots[0]["apex_yards"] == 29.6
    assert shots[0]["launch_angle_horizontal_confidence"] == 0.31
    for older in shots[1:]:
        assert older["carry_lateral_yards"] is None
        assert older["apex_yards"] is None
        assert older["launch_angle_horizontal_confidence"] is None


def test_analysis_history_preserves_quality_without_promoting_experimental_spin(tmp_path):
    name = write_session(
        tmp_path,
        shots=[
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "spin_rpm": 4669,
                "spin_rpm_measured": None,
                "spin_quality": "experimental",
                "spin_source": None,
                "spin_confidence": 0.59,
                "spin_axis_deg": None,
                "launch_angle_vertical_source": "radar",
                "launch_angle_horizontal_source": "estimated",
            },
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "spin_rpm": 6000,
                "spin_rpm_measured": 6000,
                "spin_quality": "high",
                "spin_source": "measured",
                "spin_confidence": 0.9,
                "spin_axis_deg": -2,
            },
        ],
    )
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert shots[0]["spin_quality"] == "experimental"
    assert shots[0]["spin_rpm_measured"] is None
    assert shots[0]["spin_confidence"] == 0.59
    assert shots[0]["launch_angle_horizontal_source"] == "estimated"
    assert shots[1]["spin_rpm_measured"] == 6000
    assert shots[1]["spin_axis_deg"] == -2


def test_history_keeps_calculated_spin_axis_provenance(tmp_path):
    name = write_session(
        tmp_path,
        shots=[
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                "spin_axis_deg": 11.0,
                "spin_axis_source": "calculated",
            },
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
            },
        ],
    )
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert shots[0]["spin_axis_deg"] == 11.0
    assert shots[0]["spin_axis_source"] == "calculated"
    assert not shots[1].get("spin_axis_source")


def test_history_keeps_only_valid_curve_class_words(tmp_path):
    rows = [{"curve_class": word} for word in ("hook", "draw", "straight", "fade", "slice")] + [
        {"curve_class": "banana"},
        {"curve_class": 3},
        {"curve_class": ["draw"]},
        {},
    ]
    name = write_session(
        tmp_path,
        shots=[
            {
                "type": "shot_detected",
                "timestamp": NOW.isoformat(),
                "ball_speed_mph": 103,
                **row,
            }
            for row in rows
        ],
    )
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert [shot["curve_class"] for shot in shots] == [
        "hook",
        "draw",
        "straight",
        "fade",
        "slice",
        None,
        None,
        None,
        None,
    ]


def test_a_reloaded_session_keeps_the_uncertain_ball_speed_flag(tmp_path):
    # The guard flag leaves a shot out of the ball-speed, smash and carry means; a reloaded
    # session must keep it, and an unknown word must not pass for it.
    rows = [
        {"ball_speed_status": "uncertain", "ball_speed_status_reason": "club_like_track"},
        {"ball_speed_status": None, "ball_speed_status_reason": "smash_implausible"},
        {"ball_speed_status": "replaced", "ball_speed_status_reason": "banana"},
        {"ball_speed_status": ["uncertain"], "ball_speed_status_reason": 3},
        {},
    ]
    name = write_session(tmp_path, shots=[{**SHOT, **row} for row in rows])
    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]
    assert [(s["ball_speed_status"], s["ball_speed_status_reason"]) for s in shots] == [
        ("uncertain", "club_like_track"),
        (None, "smash_implausible"),
        (None, None),
        (None, None),
        (None, None),
    ]


def test_history_reason_words_are_the_guard_rules():
    from openflight.rolling_buffer.processor import RollingBufferProcessor as P

    assert set(session_history._BALL_SPEED_STATUS_REASONS) == {
        P.BALL_GUARD_CLUB_LIKE_TRACK,
        P.BALL_GUARD_SMASH_IMPLAUSIBLE,
    }
    assert session_history._SHOT_CLASSES["ball_speed_status"] == (P.BALL_GUARD_UNCERTAIN,)


def test_an_oversized_int_in_a_numeric_field_reads_as_missing(tmp_path):
    # math.isfinite raises OverflowError on an int beyond float range instead of False. One such
    # number in a damaged log must not turn the whole listing into a server error.
    name = write_session(
        tmp_path, shots=[{**SHOT, "spin_rpm": 10**400, "smash_factor": -(10**400)}]
    )
    shot = session_history.get_session(tmp_path, name, now=NOW)["shots"][0]
    assert shot["spin_rpm"] is None
    assert shot["smash_factor"] is None
    assert shot["ball_speed_mph"] == 103
    assert [s["id"] for s in session_history.list_sessions(tmp_path, now=NOW)] == [name]


@pytest.mark.parametrize(
    "speed", [pytest.param(10**400, id="oversized-int"), pytest.param(True, id="bool")]
)
def test_an_unusable_ball_speed_skips_the_shot(tmp_path, speed):
    name = write_session(tmp_path, shots=[{**SHOT, "ball_speed_mph": speed}, SHOT])
    result = session_history.get_session(tmp_path, name, now=NOW)
    assert result["shot_count"] == 1
    assert [shot["ball_speed_mph"] for shot in result["shots"]] == [103]


def test_an_oversized_capture_timestamp_does_not_raise(tmp_path):
    camera = {"type": "camera_capture", "shot_number": 1, "shot_timestamp": 10**400}
    name = write_session(tmp_path, shots=[camera, {**SHOT, "impact_timestamp": 10**400}])
    assert session_history.get_session(tmp_path, name, now=NOW)["shot_count"] == 1


def test_a_bool_in_a_numeric_field_reads_as_missing(tmp_path):
    name = write_session(tmp_path, shots=[{**SHOT, "spin_rpm": True, "smash_factor": False}])
    shot = session_history.get_session(tmp_path, name, now=NOW)["shots"][0]
    assert shot["spin_rpm"] is None
    assert shot["smash_factor"] is None


def test_history_leaves_flight_paths_out_unless_asked(tmp_path):
    name = write_session(tmp_path, shots=[{**SHOT, "flight_path": PATH}])
    shot = session_history.get_session(tmp_path, name, now=NOW)["shots"][0]
    assert "flight_path" not in shot
    assert not any(key.startswith("_") for key in shot)


def test_listing_sessions_never_asks_for_flight_paths(tmp_path, monkeypatch):
    # The listing reads every session of the last 30 days. Decoding their paths would cost
    # time for nothing, because it drops the shots.
    write_session(tmp_path, shots=[{**SHOT, "flight_path": PATH}])
    get_session = session_history.get_session
    asked = []

    def spy(*args, **kwargs):
        asked.append(kwargs.get("flight_paths", False))
        return get_session(*args, **kwargs)

    monkeypatch.setattr(session_history, "get_session", spy)
    assert len(session_history.list_sessions(tmp_path, now=NOW)) == 1
    assert asked == [False]


def test_history_returns_the_flight_path(tmp_path):
    name = write_session(tmp_path, shots=[{**SHOT, "flight_path": PATH}])
    shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
    assert shots[0]["flight_path"] == PATH
    assert not any(key.startswith("_") for key in shots[0])


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        {"unit": "m", "points": PATH["points"]},
        {"unit": "yd", "points": "x"},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0]]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0]]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, float("nan")], [1.0, 1.0, 1.0, 1.0]]},
        # JSON true/false parse to bool, an int subclass that passes math.isfinite.
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, True]]},
        # Each of these is wrong in one way only (the 3-coordinate case above is also a
        # single point, so the point count rejects it first), and none may raise.
        {"unit": "yd"},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], 7]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0, 1.0]]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, "1.0"]]},
        # math.isfinite raises OverflowError on an int beyond float range instead of False.
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 10**400]]},
        {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0]] * 401},
    ],
    ids=[
        "none",
        "not-a-dict",
        "wrong-unit",
        "points-not-a-list",
        "one-point-of-3-coordinates",
        "one-point",
        "nan-coordinate",
        "bool-coordinate",
        "no-points",
        "point-not-a-list",
        "point-of-3-coordinates",
        "point-of-5-coordinates",
        "string-coordinate",
        "oversized-int-coordinate",
        "401-points",
    ],
)
def test_history_drops_a_malformed_flight_path(tmp_path, bad):
    name = write_session(tmp_path, shots=[{**SHOT, "flight_path": bad}])
    shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
    assert shots[0]["flight_path"] is None


@pytest.mark.parametrize("count", [2, 400])
def test_history_keeps_flight_paths_of_2_and_400_points(tmp_path, count):
    path = {"unit": "yd", "points": [[float(i), 0.0, 0.0, 0.0] for i in range(count)]}
    name = write_session(tmp_path, shots=[{**SHOT, "flight_path": path}])
    shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
    assert shots[0]["flight_path"] == path


def test_the_point_cap_covers_the_longest_flight_the_simulator_can_write():
    # The cap follows the writer only through these constants: the launch point, one point per
    # sample interval up to the flight time cap, and the landing point. A longer or finer
    # simulation would otherwise make the history drop its own valid paths.
    longest = ballistics.MAX_FLIGHT_SECONDS / ballistics.SAMPLE_INTERVAL_S + 2
    assert longest <= session_history._MAX_PATH_POINTS


FLIGHT_AND_ROLL = pytest.mark.parametrize(
    "key, path", [("flight_path", PATH), ("roll_path", ROLL)], ids=["flight", "roll"]
)


@FLIGHT_AND_ROLL
def test_the_cache_holds_a_path_as_compact_text(tmp_path, key, path):
    # Parsed lists are about 25 KiB of Python objects per path, their text about 3 KB, and
    # the cache keeps up to 64 whole sessions.
    name = write_session(tmp_path, shots=[{**SHOT, key: path}, {**SHOT, key: "x"}])
    log = tmp_path / f"{name}.jsonl"
    stat = log.stat()
    kept, dropped = session_history._read_session(log, stat.st_mtime_ns, stat.st_size)["shots"]
    assert isinstance(kept[f"_{key}"], str)
    assert kept[f"_{key}"] == json.dumps(path, separators=(",", ":"))
    assert dropped[f"_{key}"] is None


@FLIGHT_AND_ROLL
def test_every_history_call_gets_its_own_path(tmp_path, key, path):
    name = write_session(tmp_path, shots=[{**SHOT, key: path}])

    def saved_path():
        shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
        return shots[0][key]

    first = saved_path()
    assert first == saved_path() == path
    first["unit"] = "m"
    first["points"].append([9.0, 9.0, 9.0, 9.0])
    first["points"][0][0] = 99.0
    assert saved_path() == path


def test_history_returns_the_roll_with_the_flight_path(tmp_path):
    back = {"unit": "yd", "points": [[5.2, 135.6, 7.66, 0.0], [5.9, 133.0, 7.5, 0.0]]}
    spin_back = {**ROLLED, "roll_path": back, "roll_yards": -2.4, "total_yards": 133.2}
    name = write_session(tmp_path, shots=[ROLLED, spin_back])
    shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
    assert [shot["flight_path"] for shot in shots] == [PATH, PATH]
    assert [shot["roll_path"] for shot in shots] == [ROLL, back]
    assert [(shot["roll_yards"], shot["total_yards"]) for shot in shots] == [
        (3.61, 139.21),
        (-2.4, 133.2),
    ]
    assert not any(key.startswith("_") for key in shots[0])


def test_history_omits_the_roll_without_the_opt_in(tmp_path):
    name = write_session(tmp_path, shots=[ROLLED])
    shot = session_history.get_session(tmp_path, name, now=NOW)["shots"][0]
    assert not {"flight_path", "roll_path"} & shot.keys()
    assert (shot["roll_yards"], shot["total_yards"]) == (3.61, 139.21)
    assert not any(key.startswith("_") for key in shot)


@pytest.mark.parametrize(
    "bad",
    [
        {"unit": "yd", "points": [[5.2, 0.0, 0.0, 0.0]] * (rollout.MAX_ROLL_POINTS + 1)},
        {"unit": "yd", "points": [[5.2, 135.6, 7.66, 0.0], [5.5, 137.4, float("nan"), 0.21]]},
        {"unit": "m", "points": ROLL["points"]},
        {"unit": "yd", "points": ROLL["points"][:1]},
        "x",
    ],
    ids=["one-over-the-cap", "nan-coordinate", "wrong-unit", "one-point", "not-a-dict"],
)
def test_history_drops_a_malformed_roll_path(tmp_path, bad):
    name = write_session(tmp_path, shots=[{**ROLLED, "roll_path": bad}])
    shot = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"][0]
    assert shot["roll_path"] is None
    assert shot["flight_path"] == PATH
    assert (shot["roll_yards"], shot["total_yards"]) == (3.61, 139.21)


@pytest.mark.parametrize("count", [2, rollout.MAX_ROLL_POINTS])
def test_history_keeps_roll_paths_from_2_points_to_the_cap(tmp_path, count):
    path = {"unit": "yd", "points": [[5.2 + i, 0.0, 0.0, 0.0] for i in range(count)]}
    name = write_session(tmp_path, shots=[{**ROLLED, "roll_path": path}])
    shot = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"][0]
    assert shot["roll_path"] == path


def test_history_keeps_old_shots_without_roll(tmp_path):
    name = write_session(tmp_path, shots=[{**SHOT, "flight_path": PATH}, SHOT])
    shots = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)["shots"]
    assert [shot["flight_path"] for shot in shots] == [PATH, None]
    for old in shots:
        assert old["roll_path"] is None
        assert old["roll_yards"] is None
        assert old["total_yards"] is None


def club_shot(club, carry, total=None, **extra):
    return {**SHOT, "club": club, "carry_spin_adjusted": carry, "total_yards": total, **extra}


def test_club_averages_prefer_logged_totals(tmp_path):
    write_session(
        tmp_path,
        shots=[
            club_shot("7iron", 140, 150.5),
            club_shot("7iron", 145, 152.0),
            club_shot("7iron", 150, 157.5),
        ],
    )
    result = session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert result == {
        "club": "7iron",
        "total_yards": pytest.approx(153.333, abs=1e-3),
        "total_count": 3,
        "carry_yards": 145,
        "carry_count": 3,
    }


def test_club_averages_skip_demo_and_old_sessions(tmp_path):
    write_session(
        tmp_path,
        shots=[
            club_shot("7iron", 140, 150),
            club_shot("7iron", 200, 210, demo=True),
            club_shot("driver", 250, 270),
        ],
    )
    write_session(tmp_path, age=31, shots=[club_shot("7iron", 100, 110)])
    result = session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert (result["total_yards"], result["total_count"]) == (150, 1)
    assert (result["carry_yards"], result["carry_count"]) == (140, 1)


def test_club_averages_without_a_finite_total_keep_the_carry(tmp_path):
    write_session(tmp_path, shots=[club_shot("pw", 110), club_shot("pw", 120)])
    result = session_history.club_averages(tmp_path, "pw", now=NOW)
    assert (result["total_yards"], result["total_count"]) == (None, 0)
    assert (result["carry_yards"], result["carry_count"]) == (115, 2)


def test_club_averages_follow_one_profile(tmp_path):
    write_session(
        tmp_path,
        shots=[
            club_shot("7iron", 140, 150, profile_id="anna"),
            club_shot("7iron", 150, 160, profile_id="anna"),
            club_shot("7iron", 120, 125, profile_id="ben"),
            club_shot("7iron", 130),
        ],
    )
    anna = session_history.club_averages(tmp_path, "7iron", profile_id="anna", now=NOW)
    ben = session_history.club_averages(tmp_path, "7iron", profile_id="ben", now=NOW)
    everyone = session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert (anna["total_yards"], anna["total_count"]) == (155, 2)
    assert (anna["carry_yards"], anna["carry_count"]) == (145, 2)
    assert (ben["total_yards"], ben["total_count"]) == (125, 1)
    assert (ben["carry_yards"], ben["carry_count"]) == (120, 1)
    assert (everyone["total_count"], everyone["carry_count"]) == (3, 4)
    nobody = session_history.club_averages(tmp_path, "7iron", profile_id="cleo", now=NOW)
    assert (nobody["total_yards"], nobody["carry_yards"], nobody["carry_count"]) == (None, None, 0)


def test_club_averages_leave_out_an_uncertain_ball_speed(tmp_path):
    # A guard-flagged shot's ball speed, and so its carry, is wrong: it must not pull the
    # training start target down nor count toward the averaged shots.
    uncertain = {"ball_speed_status": "uncertain", "ball_speed_status_reason": "club_like_track"}
    write_session(
        tmp_path,
        shots=[
            club_shot("driver", 240, 255, ball_speed_mph=150),
            club_shot("driver", 245, 262, ball_speed_mph=152),
            club_shot("driver", 70, 78, ball_speed_mph=60.8, **uncertain),
        ],
    )
    result = session_history.club_averages(tmp_path, "driver", now=NOW)
    assert result == {
        "club": "driver",
        "total_yards": pytest.approx(258.5),
        "total_count": 2,
        "carry_yards": pytest.approx(242.5),
        "carry_count": 2,
    }


def spy_reads(monkeypatch):
    read = session_history._read_session
    names = []

    def spy(path, *args):
        names.append(path.name)
        return read(path, *args)

    monkeypatch.setattr(session_history, "_read_session", spy)
    return names


def test_history_never_reads_a_session_file_outside_the_window(tmp_path, monkeypatch):
    today = write_session(tmp_path, shots=[club_shot("7iron", 140, 150)])
    edge = write_session(tmp_path, age=30, shots=[club_shot("7iron", 130, 140)])
    old = write_session(tmp_path, age=32, shots=[club_shot("7iron", 100, 110)])
    names = spy_reads(monkeypatch)
    sessions = session_history.list_sessions(tmp_path, now=NOW)
    averages = session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert [s["id"] for s in sessions] == [today, edge]
    assert (averages["total_yards"], averages["total_count"]) == (145, 2)
    assert f"{old}.jsonl" not in names
    assert {f"{today}.jsonl", f"{edge}.jsonl"} <= set(names)


def test_an_unparsable_name_falls_back_to_the_file_mtime(tmp_path, monkeypatch):
    stale = write_session(tmp_path, name="session_99999999_999999_stale")
    fresh = write_session(tmp_path, name="session_99999999_999999_fresh")
    old = (NOW - timedelta(days=32)).timestamp()
    os.utime(tmp_path / f"{stale}.jsonl", (old, old))
    recent = NOW.timestamp()
    os.utime(tmp_path / f"{fresh}.jsonl", (recent, recent))
    names = spy_reads(monkeypatch)
    session_history.list_sessions(tmp_path, now=NOW)
    session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert f"{stale}.jsonl" not in names
    assert f"{fresh}.jsonl" in names


def tombstone(timestamp):
    return {"type": "shot_deleted", "ts": NOW.isoformat(), "shot_timestamp": timestamp}


def stamped(seconds, **extra):
    return {**SHOT, "timestamp": (NOW + timedelta(seconds=seconds)).isoformat(), **extra}


def test_history_drops_a_deleted_shot(tmp_path):
    first, second = stamped(1, shot_number=1), stamped(2, shot_number=2)
    name = write_session(tmp_path, shots=[first, second, tombstone(first["timestamp"])])
    result = session_history.get_session(tmp_path, name, now=NOW)
    assert [shot["timestamp"] for shot in result["shots"]] == [second["timestamp"]]
    assert result["shot_count"] == 1


def test_list_hides_a_session_whose_shots_are_all_deleted(tmp_path):
    kept = write_session(tmp_path)
    shot = stamped(5)
    write_session(tmp_path, age=1, shots=[tombstone(shot["timestamp"]), shot])
    assert [s["id"] for s in session_history.list_sessions(tmp_path, now=NOW)] == [kept]


def test_club_averages_skip_deleted_shots(tmp_path):
    shots = [
        stamped(1, club="7iron", carry_spin_adjusted=140, total_yards=150),
        stamped(2, club="7iron", carry_spin_adjusted=145, total_yards=155),
        stamped(3, club="7iron", carry_spin_adjusted=300, total_yards=320),
    ]
    write_session(tmp_path, shots=[*shots, tombstone(shots[2]["timestamp"])])
    result = session_history.club_averages(tmp_path, "7iron", now=NOW)
    assert (result["total_yards"], result["total_count"]) == (152.5, 2)
    assert (result["carry_yards"], result["carry_count"]) == (142.5, 2)


def test_find_saved_shot(tmp_path):
    first, second = stamped(1), stamped(2)
    name = write_session(tmp_path, shots=[first, second])
    path = tmp_path / f"{name}.jsonl"
    assert session_history.find_saved_shot(tmp_path, name, first["timestamp"], now=NOW) == (
        path,
        False,
    )
    session_history.append_shot_deleted(path, first["timestamp"])
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[-1]["type"] == "shot_deleted"
    assert lines[-1]["shot_timestamp"] == first["timestamp"]
    assert isinstance(lines[-1]["ts"], str)
    assert len(lines) == 4
    assert session_history.find_saved_shot(tmp_path, name, first["timestamp"], now=NOW) == (
        path,
        True,
    )
    assert session_history.find_saved_shot(tmp_path, name, second["timestamp"], now=NOW) == (
        path,
        False,
    )
    with pytest.raises(FileNotFoundError):
        session_history.find_saved_shot(tmp_path, name, stamped(9)["timestamp"], now=NOW)
    with pytest.raises(FileNotFoundError):
        session_history.find_saved_shot(tmp_path, "session_20260930_200000_none", "x", now=NOW)
    with pytest.raises(ValueError):
        session_history.find_saved_shot(tmp_path, "../x", first["timestamp"], now=NOW)
    with pytest.raises(ValueError):
        session_history.find_saved_shot(tmp_path, name, "", now=NOW)


def test_find_saved_shot_refuses_symlinks_and_old_sessions(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = write_session(outside)
    (tmp_path / f"{linked}.jsonl").symlink_to(outside / f"{linked}.jsonl")
    old_shot = {**SHOT, "timestamp": (NOW - timedelta(days=31)).isoformat()}
    old = write_session(tmp_path, age=31, shots=[old_shot])
    with pytest.raises(FileNotFoundError):
        session_history.find_saved_shot(tmp_path, linked, NOW.isoformat(), now=NOW)
    with pytest.raises(FileNotFoundError):
        session_history.find_saved_shot(tmp_path, old, old_shot["timestamp"], now=NOW)


# --- calculated club data merged into its shot ------------------------------------------


def club_data_entry(shot_number, shot_timestamp, **fields):
    return {
        "type": "club_data",
        "ts": NOW.isoformat(),
        "shot_number": shot_number,
        "shot_timestamp": shot_timestamp,
        "status": "done",
        "reason": "accepted",
        "mode": "show",
        "fields": {
            "camera_club_path_deg": -2.9,
            "face_to_path_class": "closed",
            "spin_axis_class": "draw",
            "impact_toe_mm": 11.2,
            **fields,
        },
    }


def test_history_merges_a_shots_club_data(tmp_path):
    first = stamped(1, shot_number=1, impact_timestamp=1001.5)
    second = stamped(2, shot_number=2, impact_timestamp=1002.5)
    name = write_session(tmp_path, shots=[first, second, club_data_entry(2, 1002.5)])

    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert "club_data_status" not in shots[0]
    assert shots[1]["club_data_status"] == "done"
    assert shots[1]["camera_club_path_deg"] == -2.9
    assert (shots[1]["face_to_path_class"], shots[1]["spin_axis_class"]) == ("closed", "draw")
    assert shots[1]["impact_toe_mm"] == 11.2
    assert shots[1]["camera_attack_angle_deg"] is None
    assert "club_data_reason" not in shots[1]


def test_history_validates_club_data_values(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    bad = club_data_entry(
        1,
        1001.5,
        camera_club_path_deg="-2.9",
        face_to_path_class="square",
        impact_toe_mm=True,
        camera_attack_angle_deg=float("nan"),
    )
    bad["status"] = "weird"
    name = write_session(tmp_path, shots=[shot, bad])

    (saved,) = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert saved["club_data_status"] is None
    assert saved["camera_club_path_deg"] is None
    assert saved["face_to_path_class"] is None
    assert saved["impact_toe_mm"] is None
    assert saved["camera_attack_angle_deg"] is None
    assert saved["spin_axis_class"] == "draw"


def test_history_merges_the_impact_point_and_validates_its_words(tmp_path):
    first = stamped(1, shot_number=1, impact_timestamp=1001.5)
    second = stamped(2, shot_number=2, impact_timestamp=1002.5)
    impact = {
        "impact_offset_mm": -18.3,
        "impact_sigma_mm": 6.4,
        "impact_height_mm": 29.2,
        "impact_height_sigma_mm": 2.8,
        "impact_scale_mm_per_px": 3.0,
    }
    name = write_session(
        tmp_path,
        shots=[
            first,
            second,
            club_data_entry(1, 1001.5, **impact, impact_zone="heel", impact_method="D"),
            club_data_entry(2, 1002.5, **impact, impact_zone="extreme", impact_method="mean"),
        ],
    )

    good, bad = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert {key: good[key] for key in impact} == impact
    assert (good["impact_zone"], good["impact_method"]) == ("heel", "D")
    assert (bad["impact_zone"], bad["impact_method"]) == (None, None)
    assert bad["impact_offset_mm"] == -18.3


def test_history_ignores_duplicate_club_data_for_one_shot(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    name = write_session(
        tmp_path,
        shots=[shot, club_data_entry(1, 1001.5), club_data_entry(1, 1001.5, impact_toe_mm=-4.0)],
    )

    (saved,) = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert "club_data_status" not in saved
    assert "impact_toe_mm" not in saved


def test_history_skips_club_data_logged_in_log_mode(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    shadow = {**club_data_entry(1, 1001.5), "mode": "log"}
    name = write_session(tmp_path, shots=[shot, shadow])

    (saved,) = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert "camera_club_path_deg" not in saved


def test_history_drops_the_club_data_of_a_deleted_shot(tmp_path):
    first = stamped(1, shot_number=1, impact_timestamp=1001.5)
    second = stamped(2, shot_number=2, impact_timestamp=1002.5)
    name = write_session(
        tmp_path,
        shots=[
            first,
            second,
            club_data_entry(1, 1001.5),
            club_data_entry(2, 1002.5),
            tombstone(first["timestamp"]),
        ],
    )

    shots = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert [shot["shot_number"] for shot in shots] == [2]
    assert shots[0]["club_data_status"] == "done"


# --- a shot flown again with a later spin axis (``shot_updated``) ---------------------

CURVED = {
    "unit": "yd",
    "points": [[0.0, 0.0, 0.0, 0.0], [0.05, 2.41, 0.13, 0.86], [5.2, 134.9, 1.2, 0.0]],
}


def updated(shot, **extra):
    return {**shot, "type": "shot_updated", **extra}


def test_history_lays_a_shots_update_over_it_and_keeps_one_row(tmp_path):
    first = stamped(1, shot_number=1, impact_timestamp=1001.5, flight_path=PATH)
    second = stamped(
        2,
        shot_number=2,
        impact_timestamp=1002.5,
        flight_path=PATH,
        carry_spin_adjusted=140.0,
        carry_lateral_yards=7.66,
        apex_yards=30.2,
        total_yards=150.0,
    )
    update = updated(
        second,
        spin_axis_deg=-4.2,
        spin_axis_source="calculated",
        curve_class="draw",
        carry_spin_adjusted=138.5,
        carry_lateral_yards=1.2,
        apex_yards=29.8,
        total_yards=147.0,
        flight_path=CURVED,
        ball_speed_mph=999,  # not something an update replaces
    )
    name = write_session(tmp_path, shots=[first, second, update])

    result = session_history.get_session(tmp_path, name, now=NOW, flight_paths=True)

    assert result["shot_count"] == 2
    one, two = result["shots"]
    assert one["curve_class"] is None and one["flight_path"] == PATH
    assert (two["spin_axis_deg"], two["spin_axis_source"], two["curve_class"]) == (
        -4.2,
        "calculated",
        "draw",
    )
    assert (two["carry_spin_adjusted"], two["carry_lateral_yards"], two["total_yards"]) == (
        138.5,
        1.2,
        147.0,
    )
    assert two["apex_yards"] == 29.8
    assert two["flight_path"] == CURVED
    assert two["ball_speed_mph"] == 103


def test_history_takes_the_last_update_and_ignores_a_foreign_or_deleted_one(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    gone = stamped(2, shot_number=2, impact_timestamp=1002.5)
    other_shot = stamped(3, shot_number=1, impact_timestamp=1001.5)  # same key, other row
    name = write_session(
        tmp_path,
        shots=[
            shot,
            gone,
            updated(shot, curve_class="fade"),
            updated(shot, curve_class="slice"),
            updated(gone, curve_class="hook"),
            tombstone(gone["timestamp"]),
            updated(other_shot, curve_class="hook", timestamp=other_shot["timestamp"]),
        ],
    )

    (saved,) = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    # The last update for the key belongs to another timestamp: it lends nothing.
    assert saved["curve_class"] is None


def test_history_takes_the_last_update_of_a_shot(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    name = write_session(
        tmp_path,
        shots=[shot, updated(shot, curve_class="fade"), updated(shot, curve_class="slice")],
    )

    (saved,) = session_history.get_session(tmp_path, name, now=NOW)["shots"]

    assert saved["curve_class"] == "slice"


def test_history_never_counts_an_update_without_its_shot(tmp_path):
    shot = stamped(1, shot_number=1, impact_timestamp=1001.5)
    lone = stamped(2, shot_number=2, impact_timestamp=1002.5)
    name = write_session(tmp_path, shots=[shot, updated(lone, curve_class="draw")])

    result = session_history.get_session(tmp_path, name, now=NOW)

    assert result["shot_count"] == 1
    assert result["shots"][0]["curve_class"] is None
