"""A deleted shot leaves a shot_deleted line in its session log and leaves the history."""

import json
from datetime import datetime, timedelta
from random import Random
from urllib.parse import quote

import pytest

from openflight import server, session_history
from openflight.demo import DemoSession
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger


@pytest.fixture(name="live")
def fixture_live(tmp_path, monkeypatch):
    """A mock monitor and a real session logger writing into tmp_path."""
    monitor = server.MockLaunchMonitor()
    log = SessionLogger(log_dir=tmp_path)
    log.start_session(mode="mock")
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server, "get_session_logger", lambda: log)
    monkeypatch.setattr(server, "camera_replay_manager", None)
    monkeypatch.setattr(
        server,
        "demo_session",
        DemoSession(rng=Random(2026), finalize=server._finalize_shot_metrics),
    )
    yield monitor, log
    server.demo_session.stop()
    log.end_session()


@pytest.fixture(name="emits")
def fixture_emits(monkeypatch):
    events = []
    monkeypatch.setattr(
        server.socketio, "emit", lambda event, payload=None, **_: events.append((event, payload))
    )
    return events


def _record(monitor, log, seconds):
    shot = Shot(
        ball_speed_mph=110.0 + seconds,
        timestamp=datetime.now() + timedelta(seconds=seconds),
        shot_number=seconds + 1,
    )
    monitor._shots.append(shot)
    log.log_shot(shot)
    return shot


def _tombstones(path):
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    return [line["shot_timestamp"] for line in lines if line["type"] == "shot_deleted"]


def _history(log):
    return session_history.get_session(log.log_dir, log.session_path.stem)


def test_socket_delete_hides_the_shot_from_the_history(live, monkeypatch):
    monitor, log = live
    sio = server.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(sio, name, getattr(sio, name))
    monkeypatch.setattr(sio.eio, "async_handlers", sio.eio.async_handlers)
    gone, kept = _record(monitor, log, 0), _record(monitor, log, 1)
    logged = json.loads(log.session_path.read_text().splitlines()[1])["timestamp"]
    client = server.socketio.test_client(server.app, flask_test_client=server.app.test_client())
    try:
        client.emit("delete_shot", {"timestamp": gone.timestamp.isoformat()})
    finally:
        client.disconnect()
    assert _tombstones(log.session_path) == [logged]
    assert [shot["timestamp"] for shot in _history(log)["shots"]] == [kept.timestamp.isoformat()]
    assert monitor.get_shots() == [kept]


def test_deleting_a_current_session_shot_from_the_history_goes_through_the_logger(
    live, emits, monkeypatch
):
    monitor, log = live
    gone, kept = _record(monitor, log, 0), _record(monitor, log, 1)
    calls = []
    original = log.log_shot_deleted
    monkeypatch.setattr(
        log, "log_shot_deleted", lambda stamp: (calls.append(stamp), original(stamp))
    )
    stamp = gone.timestamp.isoformat()
    response = server.app.test_client().delete(
        f"/api/session-history/{log.session_path.stem}/shots/{quote(stamp, safe='')}"
    )
    assert response.status_code == 204
    assert calls == [stamp]
    assert _tombstones(log.session_path) == [stamp]
    assert monitor.get_shots() == [kept]
    states = [payload for event, payload in emits if event == "session_state"]
    assert [shot["timestamp"] for shot in states[-1]["shots"]] == [kept.timestamp.isoformat()]


def test_demo_delete_writes_nothing(live, emits):
    _monitor, log = live
    server.demo_session.start(
        now_s=0.0, wall=datetime.now(), bag_ids=None, profile_id="p", profile_name="P"
    )
    stamp = server.demo_session.shots()[0].timestamp.isoformat()
    server.handle_delete_shot({"timestamp": stamp})
    assert [event for event, _payload in emits] == ["session_state"]
    assert _tombstones(log.session_path) == []
