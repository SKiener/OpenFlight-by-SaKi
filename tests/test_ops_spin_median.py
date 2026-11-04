"""OPS spin session median per club: session log only, from the earlier shots."""

import json
from datetime import datetime, timedelta

import pytest

from openflight import server
from openflight.ballistics import CLUB_TYPICAL_SPIN_RPM
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.ops_spin_median import (
    MIN_EARLIER_SHOTS,
    OpsSpinSessionMedian,
    log_fields,
    ops_spin_rpm,
)
from openflight.session_logger import SessionLogger

_T0 = datetime(2026, 10, 25, 18, 0, 0)


def _shot(i, rpm, club=ClubType.IRON_7, profile="p1", **kwargs):
    return Shot(
        ball_speed_mph=110.0,
        timestamp=_T0 + timedelta(seconds=i),
        club=club,
        spin_rpm=rpm,
        spin_confidence=0.59,
        profile_id=profile,
        **kwargs,
    )


# ---------------------------------------------------------------- tracker


def test_min_earlier_shots_is_three():
    assert MIN_EARLIER_SHOTS == 3


def test_median_needs_three_earlier_shots_and_excludes_the_current_one():
    tracker = OpsSpinSessionMedian()
    results = [
        tracker.observe(_shot(i, rpm)) for i, rpm in enumerate([5000, 6000, 7000, 9000, 1000])
    ]
    assert results == [(None, 0), (None, 1), (None, 2), (6000, 3), (6500, 4)]


def test_median_is_per_club():
    tracker = OpsSpinSessionMedian()
    for i, rpm in enumerate([5000, 6000, 7000]):
        tracker.observe(_shot(i, rpm))
    assert tracker.observe(_shot(10, 2500, club=ClubType.DRIVER)) == (None, 0)
    assert tracker.observe(_shot(11, 4000)) == (6000, 3)


@pytest.mark.parametrize("rpm", [None, 0.0, -10.0, float("nan"), float("inf")])
def test_shots_without_a_positive_ops_spin_are_not_counted(rpm):
    tracker = OpsSpinSessionMedian()
    tracker.observe(_shot(0, rpm))
    assert tracker.observe(_shot(1, 5000)) == (None, 0)


def test_calculated_spin_uses_the_raw_ops_reading():
    shot = _shot(0, 9999.0, spin_rpm_measured=4321.0, spin_source="calculated")
    assert ops_spin_rpm(shot) == 4321.0
    assert ops_spin_rpm(_shot(1, 9999.0, spin_rpm_measured=None, spin_source="calculated")) is None
    assert ops_spin_rpm(_shot(2, 4500.0)) == 4500.0


def test_reset_and_forget():
    tracker = OpsSpinSessionMedian()
    for i, rpm in enumerate([5000, 6000, 7000]):
        tracker.observe(_shot(i, rpm))
    tracker.forget(_shot(1, 0).timestamp.isoformat())
    assert tracker.observe(_shot(5, 1)) == (None, 2)
    tracker.reset()
    assert tracker.observe(_shot(6, 1)) == (None, 0)


def test_another_profile_starts_over():
    tracker = OpsSpinSessionMedian()
    for i, rpm in enumerate([5000, 6000, 7000]):
        tracker.observe(_shot(i, rpm))
    assert tracker.observe(_shot(5, 5000, profile="p2")) == (None, 0)
    assert tracker.observe(_shot(6, 5000)) == (None, 0)


def test_the_same_shot_observed_again_counts_once():
    tracker = OpsSpinSessionMedian()
    for i, rpm in enumerate([5000, 6000, 7000]):
        tracker.observe(_shot(i, rpm))
    assert tracker.observe(_shot(2, 7000)) == (None, 2)


def test_log_fields():
    assert log_fields(6123.456, 4) == {
        "ops_spin_session_median_rpm": 6123.5,
        "ops_spin_session_median_n": 4,
    }
    assert log_fields(None, 2) == {
        "ops_spin_session_median_rpm": None,
        "ops_spin_session_median_n": 2,
    }


# ---------------------------------------------------------------- session log


def test_session_logger_writes_log_only_fields(tmp_path):
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session()
    logger.log_shot(_shot(0, 5000), log_only_fields=log_fields(6000.0, 3))
    logger.end_session()

    entries = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    shot = next(entry for entry in entries if entry["type"] == "shot_detected")
    assert shot["ops_spin_session_median_rpm"] == 6000.0
    assert shot["ops_spin_session_median_n"] == 3


# ---------------------------------------------------------------- server


@pytest.fixture(name="logged")
def fixture_logged(monkeypatch):
    entries = []

    class _Log:
        def log_shot(self, shot, pipeline_ms=None, log_only_fields=None):
            entries.append((shot, log_only_fields))

        def log_shot_deleted(self, _timestamp):
            return None

    for name in ("monitor", "kld7_vertical", "kld7_horizontal", "camera_capture_runtime"):
        monkeypatch.setattr(server, name, None)
    for name in ("ball_speed_correction_enabled", "calculated_spin_enabled", "debug_mode"):
        monkeypatch.setattr(server, name, False)
    monkeypatch.setattr(server, "sim_connectors", [])
    monkeypatch.setattr(server, "get_session_logger", _Log)
    monkeypatch.setattr(server.socketio, "emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "ops_spin_session_median", OpsSpinSessionMedian())
    return entries


def test_finalized_shots_log_the_median_of_the_earlier_ones(logged):
    shots = [_shot(i, rpm) for i, rpm in enumerate([5000, 6000, 7000, 8000])]
    for shot in shots:
        server._finalize_shot_detected(shot, emit_event="shot")

    assert [fields for _, fields in logged] == [
        log_fields(None, 0),
        log_fields(None, 1),
        log_fields(None, 2),
        log_fields(6000.0, 3),
    ]


def test_the_median_is_not_in_the_shot_or_its_payload(logged):
    shot = _shot(0, 5000)
    server._finalize_shot_detected(shot, emit_event="shot")

    for data in (shot.to_dict(), server.shot_to_dict(shot)):
        assert "ops_spin_session_median_rpm" not in data
        assert "ops_spin_session_median_n" not in data


def test_the_median_never_changes_the_carry(logged):
    for i, rpm in enumerate([1000, 1000, 1000]):
        server._finalize_shot_detected(_shot(i, rpm, launch_angle_vertical=17.0), emit_event="shot")
    shot = _shot(5, 1000, launch_angle_vertical=17.0)
    server._finalize_shot_detected(shot, emit_event="shot")

    assert logged[-1][1]["ops_spin_session_median_rpm"] == 1000.0
    assert shot.carry_spin_source == "club_typical"
    assert shot.carry_spin_rpm == CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_7]


def test_demo_shots_are_not_counted(logged):
    for i in range(3):
        server._finalize_shot_detected(_shot(i, 5000, demo=True), emit_event="shot")
    assert server.ops_spin_session_median.observe(_shot(9, 5000)) == (None, 0)


def test_clear_session_resets_the_median(logged):
    for i in range(3):
        server._finalize_shot_detected(_shot(i, 5000), emit_event="shot")

    server.handle_clear_session({"profile_id": "p1"})

    assert server.ops_spin_session_median.observe(_shot(9, 5000)) == (None, 0)


def test_switching_the_active_profile_resets_the_median(logged):
    store = server.get_profile_store()
    first = store.get_active().id
    second = store.add("Second").id
    store.set_active(first)
    for i in range(3):
        server._finalize_shot_detected(_shot(i, 5000, profile=first), emit_event="shot")

    server.handle_set_active_profile({"profile_id": first})
    assert server.ops_spin_session_median.observe(_shot(8, 5000, profile=first)) == (5000, 3)

    server.handle_set_active_profile({"profile_id": second})
    assert server.ops_spin_session_median.observe(_shot(9, 5000, profile=first)) == (None, 0)


def test_deleting_a_shot_drops_it_from_the_median(logged, monkeypatch):
    shots = [_shot(i, rpm) for i, rpm in enumerate([5000, 6000, 7000])]
    for shot in shots:
        server._finalize_shot_detected(shot, emit_event="shot")
    monkeypatch.setattr(server, "_delete_session_row", lambda _timestamp: True)
    monkeypatch.setattr(server, "_session_state_payload", lambda: {})

    server.handle_delete_shot({"timestamp": shots[1].timestamp.isoformat()})

    assert server.ops_spin_session_median.observe(_shot(9, 1)) == (None, 2)
