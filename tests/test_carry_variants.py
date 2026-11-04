"""Shadow carry variants: four extra flights per ballistic carry, session log only.

Every live ballistic carry gets a ``carry_variants`` session-log entry, flown after the
shot event on the carry-span worker: the shot's launch with the tour table, the
range-ball table, and the range-ball table with the range-ball drag at 0.3546 and 0.225.
The shown carry, the shot event and the simulators never see them.
"""

import json
import math
from dataclasses import replace
from datetime import datetime

import pytest

from openflight import ballistics, server
from openflight.ballistics import (
    CARRY_VARIANT_DT_SECONDS,
    CARRY_VARIANTS,
    CLUB_TYPICAL_SPIN_RPM,
    M_TO_YD,
    RANGE_BALL_SPIN_RPM,
    LaunchConditions,
    carry_variants,
    resolve_launch,
    simulate,
)
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger
from openflight.weather import AirDensityChoice

pytestmark = pytest.mark.usefixtures("isolate_finalization")

VARIANT_NAMES = ["tour", "range_ball_spin", "range_ball_spin_drag", "range_ball_spin_drag_half"]


def _shot(club=ClubType.DRIVER, ball_speed=147.1, launch=9.4, axis=0.0, hla=None):
    return Shot(
        ball_speed_mph=ball_speed,
        timestamp=datetime.now(),
        club=club,
        launch_angle_vertical=launch,
        launch_angle_confidence=0.95,
        launch_angle_horizontal=hla,
        spin_rpm=2500.0,
        spin_confidence=0.59,
        spin_axis_deg=axis,
        shot_number=7,
        impact_timestamp=1793622589.5,
    )


def _driver(spin=2700, source="club_typical", axis=0.0):
    return LaunchConditions(147.139, 9.43, 0.0, spin, axis, source)


def _metres(trajectory):
    return trajectory.carry_yards / M_TO_YD, trajectory.apex_yards / M_TO_YD


# ---------------------------------------------------------------- ballistics


def test_variants_are_the_four_decided_combinations():
    assert CARRY_VARIANTS == {
        "tour": ("tour", 0.0),
        "range_ball_spin": ("range-ball", 0.0),
        "range_ball_spin_drag": ("range-ball", 0.3546),
        "range_ball_spin_drag_half": ("range-ball", 0.225),
    }
    assert CARRY_VARIANT_DT_SECONDS == 0.01


def test_each_variant_flies_its_table_and_slope():
    rho = 1.2013
    variants = carry_variants(_driver(), ClubType.DRIVER, rho)

    assert list(variants) == VARIANT_NAMES
    for name, (table, slope) in CARRY_VARIANTS.items():
        spin = (CLUB_TYPICAL_SPIN_RPM if table == "tour" else RANGE_BALL_SPIN_RPM)[ClubType.DRIVER]
        carry_m, apex_m = _metres(
            simulate(
                replace(_driver(), spin_rpm=spin),
                air_density=rho,
                dt=0.01,
                range_ball_drag_slope=slope,
            )
        )
        assert variants[name] == {
            "spin_rpm": spin,
            "carry_m": round(carry_m, 1),
            "apex_m": round(apex_m, 1),
        }


def test_variants_are_metres_with_one_decimal():
    for values in carry_variants(_driver(), ClubType.DRIVER, 1.2).values():
        for key in ("carry_m", "apex_m"):
            assert values[key] == round(values[key], 1)
    assert json.dumps(carry_variants(_driver(), ClubType.DRIVER, 1.2))  # plain JSON


def test_coarse_step_stays_within_the_validated_error():
    """dt 0.01 s instead of 0.002 s: at most ~0.011 m carry (aero/02_verify_sim_log.txt)."""
    for club, conditions in (
        (ClubType.DRIVER, _driver()),
        (ClubType.IRON_8, LaunchConditions(107.317, 21.56, 2.0, 4260, -4.0, "club_typical")),
        (ClubType.SW, LaunchConditions(78.7625, 38.02, 0.0, 2850, 0.0, "club_typical")),
    ):
        fine = _metres(simulate(conditions, air_density=1.2))
        coarse = _metres(simulate(conditions, air_density=1.2, dt=CARRY_VARIANT_DT_SECONDS))
        assert coarse == pytest.approx(fine, abs=0.02), club


def test_drag_variants_shorten_a_drive_and_leave_a_wedge_alone():
    drive = carry_variants(_driver(), ClubType.DRIVER, 1.2013)
    assert drive["range_ball_spin_drag"]["carry_m"] == pytest.approx(
        drive["range_ball_spin"]["carry_m"] - 10.9, abs=0.5
    )
    assert (
        drive["range_ball_spin_drag"]["carry_m"]
        < drive["range_ball_spin_drag_half"]["carry_m"]
        < drive["range_ball_spin"]["carry_m"]
    )

    wedge = carry_variants(
        LaunchConditions(93.5, 29.0, 0.0, 9000, 0.0, "club_typical"), ClubType.PW, 1.2013
    )
    assert wedge["range_ball_spin_drag"] == wedge["range_ball_spin"]
    assert wedge["range_ball_spin_drag_half"] == wedge["range_ball_spin"]
    # The range-ball wedge spin is far lower than the tour value: a longer PW.
    assert wedge["range_ball_spin"]["spin_rpm"] == 2310
    assert wedge["range_ball_spin"]["carry_m"] > wedge["tour"]["carry_m"]


def test_measured_spin_stays_measured_in_every_variant():
    variants = carry_variants(_driver(spin=3123.0, source="measured"), ClubType.DRIVER, 1.2)

    assert {values["spin_rpm"] for values in variants.values()} == {3123.0}
    assert variants["tour"] == variants["range_ball_spin"]


def test_variants_ignore_the_operator_scale_and_keep_the_spin_axis():
    curved = carry_variants(_driver(axis=15.0), ClubType.IRON_7, 1.2)
    straight = carry_variants(_driver(axis=0.0), ClubType.IRON_7, 1.2)

    assert curved["tour"]["spin_rpm"] == CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_7]
    assert curved["range_ball_spin"]["spin_rpm"] == RANGE_BALL_SPIN_RPM[ClubType.IRON_7]
    assert curved["tour"]["carry_m"] != straight["tour"]["carry_m"]


def test_unknown_club_flies_the_unknown_values():
    variants = carry_variants(_driver(), None, 1.2)

    assert variants["tour"]["spin_rpm"] == CLUB_TYPICAL_SPIN_RPM[ClubType.UNKNOWN]
    assert variants["range_ball_spin"]["spin_rpm"] == RANGE_BALL_SPIN_RPM[ClubType.UNKNOWN]


def test_a_flight_that_is_not_finite_raises():
    with pytest.raises(ValueError, match="not finite"):
        carry_variants(_driver(), ClubType.DRIVER, math.nan)


# ---------------------------------------------------------------- server


@pytest.fixture
def session_log(tmp_path, monkeypatch):
    log = SessionLogger(log_dir=tmp_path, enabled=True)
    log.start_session(mode="mock", trigger_type="manual")
    monkeypatch.setattr(server, "get_session_logger", lambda: log)
    yield log
    log.end_session()


@pytest.fixture
def timeline(monkeypatch, session_log):
    """One ordered list of what the shot path does: emits and session-log writes."""
    events = []
    monkeypatch.setattr(
        server.socketio, "emit", lambda event, payload: events.append(("emit", event, payload))
    )
    original = session_log._write_entry

    def write_entry(entry_type, data):
        events.append(("log", entry_type, data))
        original(entry_type, data)

    monkeypatch.setattr(session_log, "_write_entry", write_entry)
    return events


def _entries(session_log, entry_type):
    lines = session_log.session_path.read_text().strip().split("\n")
    return [entry for entry in map(json.loads, lines) if entry["type"] == entry_type]


def _record_submits(monkeypatch, timeline, span_jobs, variant_jobs):
    """Hold the worker jobs as the autouse fixtures do, and put each hand-over in the timeline."""

    def held(name, jobs):
        def submit(job):
            timeline.append(("submit", name, None))
            jobs.append(job)

        return submit

    monkeypatch.setattr(server, "_submit_carry_span", held("carry_span", span_jobs))
    monkeypatch.setattr(server, "_submit_carry_variants", held("carry_variants", variant_jobs))


def test_variants_are_logged_after_the_shot_event(
    monkeypatch, timeline, session_log, _hold_carry_variant_jobs, _hold_carry_span_jobs
):
    _record_submits(monkeypatch, timeline, _hold_carry_span_jobs, _hold_carry_variant_jobs)
    shot = _shot()

    server._finalize_shot_detected(shot, emit_event="shot")

    # The shot is logged and published before its variants reach the worker, after its span.
    assert [(kind, name) for kind, name, _ in timeline] == [
        ("log", "shot_detected"),
        ("emit", "shot"),
        ("submit", "carry_span"),
        ("submit", "carry_variants"),
    ]
    assert len(_hold_carry_variant_jobs) == 1
    shown = timeline[1][2]["shot"]

    # The span goes first on the shared worker, the variants after it.
    _hold_carry_span_jobs[0]()
    _hold_carry_variant_jobs[0]()

    assert [(kind, name) for kind, name, _ in timeline] == [
        ("log", "shot_detected"),
        ("emit", "shot"),
        ("submit", "carry_span"),
        ("submit", "carry_variants"),
        ("emit", "shot_update"),
        ("log", "carry_variants"),
    ]
    (entry,) = _entries(session_log, "carry_variants")
    assert entry["shot_number"] == 7
    assert entry["shot_timestamp"] == 1793622589.5
    assert entry["reason"] is None
    assert entry["air_density_kg_m3"] == round(server.air_density_kg_m3, 4)
    assert entry["elapsed_ms"] >= 0.0
    assert list(entry["carry_variants"]) == VARIANT_NAMES
    assert entry["carry_variants"] == carry_variants(
        resolve_launch(_shot()), ClubType.DRIVER, server.air_density_kg_m3
    )
    # The shown carry is the live one, unchanged by the variants.
    assert shown["carry_spin_adjusted"] == round(shot.carry_spin_adjusted)
    assert shot.carry_spin_adjusted == simulate(
        resolve_launch(_shot()), air_density=server.air_density_kg_m3
    ).carry_yards
    assert "carry_variants" not in _entries(session_log, "shot_detected")[0]


def test_tour_variant_matches_the_default_live_carry(session_log, _hold_carry_variant_jobs):
    shot = _shot(club=ClubType.IRON_7, ball_speed=112.0, launch=18.2)
    server._finalize_shot_detected(shot, emit_event="shot")

    _hold_carry_variant_jobs[0]()

    (entry,) = _entries(session_log, "carry_variants")
    assert entry["carry_variants"]["tour"]["carry_m"] == pytest.approx(
        shot.carry_spin_adjusted / M_TO_YD, abs=0.06
    )
    assert entry["carry_variants"]["tour"]["apex_m"] == pytest.approx(
        shot.apex_yards / M_TO_YD, abs=0.06
    )


def test_variants_never_change_the_shot(session_log, _hold_carry_variant_jobs):
    shot = _shot()
    server._finalize_shot_detected(shot, emit_event="shot")
    before = server.shot_to_dict(shot)

    _hold_carry_variant_jobs[0]()

    assert server.shot_to_dict(shot) == before


def test_variants_fly_with_the_density_of_the_carry(
    monkeypatch, session_log, _hold_carry_variant_jobs
):
    monkeypatch.setattr(
        server,
        "_air_density_for_shot",
        lambda: AirDensityChoice(kg_m3=1.2006, source="open-meteo", weather_age_s=60.0),
    )
    server._finalize_shot_detected(_shot(), emit_event="shot")

    _hold_carry_variant_jobs[0]()

    (entry,) = _entries(session_log, "carry_variants")
    assert entry["air_density_kg_m3"] == 1.2006
    assert entry["carry_variants"] == carry_variants(
        resolve_launch(_shot()), ClubType.DRIVER, 1.2006
    )


def test_variants_ignore_the_live_table_and_slope(
    monkeypatch, session_log, _hold_carry_variant_jobs
):
    """The live carry may run any table and slope; the variants stay the fixed four."""
    monkeypatch.setattr(server, "typical_spin_table", "range-ball")
    monkeypatch.setattr(server, "typical_spin_scale", 0.8)
    monkeypatch.setattr(server, "range_ball_drag_slope", 0.5)
    shot = _shot(club=ClubType.IRON_8, ball_speed=107.3, launch=21.6)
    server._finalize_shot_detected(shot, emit_event="shot")

    _hold_carry_variant_jobs[0]()

    (entry,) = _entries(session_log, "carry_variants")
    assert shot.carry_spin_rpm == round(4260 * 0.8)
    assert entry["carry_variants"]["tour"]["spin_rpm"] == 7500
    assert entry["carry_variants"]["range_ball_spin"]["spin_rpm"] == 4260


def test_measured_spin_shot_gets_variants_with_its_spin(session_log, _hold_carry_variant_jobs):
    shot = replace(_shot(), spin_confidence=0.9, spin_source="measured")
    server._finalize_shot_detected(shot, emit_event="shot")

    _hold_carry_variant_jobs[0]()

    (entry,) = _entries(session_log, "carry_variants")
    assert {v["spin_rpm"] for v in entry["carry_variants"].values()} == {2500.0}


def test_a_variant_failure_logs_null_with_a_reason_and_spares_the_shot(
    monkeypatch, timeline, session_log, _hold_carry_variant_jobs
):
    def broken(*_args, **_kwargs):
        raise ZeroDivisionError("float division by zero")

    monkeypatch.setattr(server, "carry_variants", broken)
    shot = _shot()
    server._finalize_shot_detected(shot, emit_event="shot")
    before = server.shot_to_dict(shot)

    _hold_carry_variant_jobs[0]()  # never raises

    (entry,) = _entries(session_log, "carry_variants")
    assert entry["carry_variants"] is None
    assert entry["reason"] == "failed: ZeroDivisionError: float division by zero"
    assert server.shot_to_dict(shot) == before
    assert [name for kind, name, _ in timeline if kind == "emit"] == ["shot"]


def test_a_variant_that_cannot_start_is_logged_as_not_started(
    monkeypatch, session_log, _hold_carry_variant_jobs
):
    def refuse(_job):
        raise RuntimeError("cannot schedule new futures after shutdown")

    monkeypatch.setattr(server, "_submit_carry_variants", refuse)
    shot = _shot()

    server._finalize_shot_detected(shot, emit_event="shot")

    (entry,) = _entries(session_log, "carry_variants")
    assert entry["carry_variants"] is None
    assert entry["reason"].startswith("not_started: RuntimeError")
    assert shot.carry_model == "ballistic"


def test_a_failing_log_write_never_raises(monkeypatch, session_log, _hold_carry_variant_jobs):
    server._finalize_shot_detected(_shot(), emit_event="shot")

    def broken(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session_log, "log_carry_variants", broken)
    _hold_carry_variant_jobs[0]()


def test_an_entry_of_an_ended_or_new_session_is_dropped(
    tmp_path, session_log, _hold_carry_variant_jobs
):
    server._finalize_shot_detected(_shot(), emit_event="shot")
    first_path = session_log.session_path
    session_log.end_session()
    session_log._session_id = "20991231_235959"  # a new session started meanwhile

    _hold_carry_variant_jobs[0]()

    assert "carry_variants" not in first_path.read_text()


def test_table_carry_gets_no_variants(monkeypatch, session_log, _hold_carry_variant_jobs):
    monkeypatch.setattr(server, "ballistics_enabled", False)

    server._finalize_shot_detected(_shot(), emit_event="shot")

    assert _hold_carry_variant_jobs == []


def test_demo_shot_gets_no_variants(session_log, _hold_carry_variant_jobs):
    server._finalize_shot_detected(replace(_shot(), demo=True), emit_event="shot")
    server._finalize_demo_shot_metrics(replace(_shot(), demo=True))

    assert _hold_carry_variant_jobs == []


def test_no_session_log_no_variants(_hold_carry_variant_jobs):
    server._finalize_shot_detected(_shot(), emit_event="shot")

    assert _hold_carry_variant_jobs == []


def test_disabled_session_log_no_variants(tmp_path, monkeypatch, _hold_carry_variant_jobs):
    log = SessionLogger(log_dir=tmp_path, enabled=False)
    monkeypatch.setattr(server, "get_session_logger", lambda: log)

    server._finalize_shot_detected(_shot(), emit_event="shot")

    assert _hold_carry_variant_jobs == []


def test_a_reflown_shot_gets_a_second_entry(
    monkeypatch, session_log, _hold_carry_variant_jobs
):
    shot = _shot(hla=1.5)
    server._finalize_shot_detected(shot, emit_event="shot")
    shot.spin_axis_deg = 12.0

    server._refly_published_shot(shot)

    assert len(_hold_carry_variant_jobs) == 2
    for job in _hold_carry_variant_jobs:
        job()
    first, second = _entries(session_log, "carry_variants")
    assert first["shot_number"] == second["shot_number"] == 7
    assert second["carry_variants"] == carry_variants(
        resolve_launch(shot), ClubType.DRIVER, server.air_density_kg_m3
    )
    assert second["carry_variants"]["tour"]["carry_m"] != first["carry_variants"]["tour"]["carry_m"]
    # The second entry follows the shot_updated entry of the re-flown shot.
    lines = session_log.session_path.read_text().strip().split("\n")
    types = [json.loads(line)["type"] for line in lines]
    last_variants = max(i for i, kind in enumerate(types) if kind == "carry_variants")
    assert types.index("shot_updated") < last_variants


def test_session_logger_writes_the_entry(tmp_path):
    log = SessionLogger(log_dir=tmp_path, enabled=True)
    log.start_session(mode="mock", trigger_type="manual")
    variants = carry_variants(_driver(), ClubType.DRIVER, 1.2)

    log.log_carry_variants(
        shot_number=3,
        shot_timestamp=12.5,
        variants=variants,
        air_density_kg_m3=1.2,
        elapsed_ms=4.2,
    )
    log.log_carry_variants(shot_number=4, shot_timestamp=None, variants=None, reason="failed: x")
    log.end_session()

    first, second = _entries(log, "carry_variants")
    assert first["carry_variants"] == variants
    assert (first["shot_number"], first["shot_timestamp"], first["reason"]) == (3, 12.5, None)
    assert (first["air_density_kg_m3"], first["elapsed_ms"]) == (1.2, 4.2)
    assert second["carry_variants"] is None and second["reason"] == "failed: x"
    assert log.stats["shots_detected"] == 0  # not a shot


def test_disabled_session_logger_writes_nothing(tmp_path):
    log = SessionLogger(log_dir=tmp_path, enabled=False)
    log.log_carry_variants(shot_number=1, shot_timestamp=None, variants=None, reason="x")
    assert log.session_path is None


def test_ballistics_module_exposes_the_variant_flight():
    assert server.carry_variants is ballistics.carry_variants
