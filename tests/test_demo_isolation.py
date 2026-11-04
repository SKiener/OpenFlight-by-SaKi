"""A demo shot never reaches a log, the saved history, a simulator or the camera replay."""

import io
import json
import queue
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openflight import server, session_history
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger
from openflight.sim.types import ConnectionState

NOW = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)


def _shot(*, demo=False, replay=None):
    return Shot(
        ball_speed_mph=103.4,
        timestamp=NOW,
        club=ClubType.IRON_8,
        launch_angle_vertical=26.2,
        launch_angle_confidence=0.95,
        spin_rpm=4669.0,
        spin_confidence=0.9,
        camera_replay=replay,
        demo=demo,
    )


class _FakeConnector:
    name = "gspro"
    host = "127.0.0.1"
    port = 921
    state = ConnectionState.CONNECTED
    codec = SimpleNamespace(fields_for_target=lambda: ["ball_speed"])

    def __init__(self):
        self.sent = []

    def is_connected(self):
        return True

    def send_shot(self, resolved):
        self.sent.append(resolved)


@pytest.fixture(name="emitted")
def fixture_emitted(monkeypatch):
    events = []
    monkeypatch.setattr(server.socketio, "emit", lambda *args, **kwargs: events.append(args))
    return events


@pytest.fixture(name="connector")
def fixture_connector(monkeypatch):
    connector = _FakeConnector()
    monkeypatch.setattr(server, "sim_connectors", [connector])
    monkeypatch.setattr(server, "sim_player_state", server.SimPlayerState())
    return connector


@pytest.fixture(name="camera")
def fixture_camera(monkeypatch):
    camera = SimpleNamespace(
        registered=[],
        queued=queue.Queue(),
        capture=SimpleNamespace(valid=True, path=Path("/captures/1"), metadata={}),
    )

    def register(path, metadata):
        del metadata
        camera.registered.append(path)
        return {"id": "replay-1"}

    monkeypatch.setattr(server, "camera_capture_runtime", object())
    monkeypatch.setattr(server, "camera_replay_manager", SimpleNamespace(register=register))
    monkeypatch.setattr(server, "camera_replay_auto_prepare_queue", camera.queued)
    monkeypatch.setattr(server, "_camera_replay_auto_prepare_worker", None)
    monkeypatch.setattr(server, "_camera_replay_auto_prepare_loop", lambda: None)
    return camera


def _join_replay_worker():
    worker = server._camera_replay_auto_prepare_worker
    if worker is not None:
        worker.join(timeout=2)


def _session_entries(logger):
    return [json.loads(line) for line in logger.session_path.read_text().splitlines()]


def _write_session(root, name, *shots):
    entries = [{"type": "session_start", "start_time": NOW.isoformat()}, *shots]
    (root / f"{name}.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    return name


def test_shot_dict_carries_the_demo_flag():
    assert _shot().to_dict()["demo"] is False
    assert _shot(demo=True).to_dict()["demo"] is True


def test_session_logger_writes_no_demo_shot(tmp_path):
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="mock", trigger_type="manual")

    logger.log_shot(_shot(demo=True))
    logger.log_shot(_shot())
    logger.end_session()

    shots = [entry for entry in _session_entries(logger) if entry["type"] == "shot_detected"]
    assert [shot["demo"] for shot in shots] == [False]
    assert logger.stats["shots_detected"] == 1


def test_history_drops_a_logged_demo_shot(tmp_path):
    real = {
        "type": "shot_detected",
        "timestamp": NOW.isoformat(),
        "club": "7-iron",
        "ball_speed_mph": 103.4,
    }
    demo = {**real, "ball_speed_mph": 99.0, "demo": True}
    only_demo = _write_session(tmp_path, "session_20260930_200000_demo", demo)
    mixed = _write_session(tmp_path, "session_20260930_200100_mixed", demo, real)

    only_demo_session = session_history.get_session(tmp_path, only_demo, now=NOW)
    mixed_session = session_history.get_session(tmp_path, mixed, now=NOW)

    assert only_demo_session["shots"] == []
    assert only_demo_session["shot_count"] == 0
    assert [shot["ball_speed_mph"] for shot in mixed_session["shots"]] == [103.4]
    assert [s["id"] for s in session_history.list_sessions(tmp_path, now=NOW)] == [mixed]


def test_simulators_never_get_a_demo_shot(monkeypatch, emitted, connector):
    monkeypatch.setattr(server, "get_session_logger", lambda: None)

    server._forward_shot_to_simulators(_shot(demo=True))
    server._forward_shot_to_simulators(_shot())

    assert len(connector.sent) == 1
    assert server.sim_player_state.shot_counter == 1
    assert [event[0] for event in emitted] == ["sim_shot"]


def test_camera_replay_skips_a_demo_shot(camera):
    unregistered = _shot(demo=True)
    server._attach_camera_replay(unregistered, camera.capture)
    server._queue_camera_replay_auto_prepare(_shot(demo=True, replay={"id": "replay-1"}))
    _join_replay_worker()

    assert camera.registered == []
    assert unregistered.camera_replay is None
    assert camera.queued.empty()
    assert server._camera_replay_auto_prepare_worker is None


def test_camera_replay_still_serves_a_real_shot(camera):
    shot = _shot()
    server._attach_camera_replay(shot, camera.capture)
    server._queue_camera_replay_auto_prepare(shot)
    _join_replay_worker()

    assert camera.registered == [camera.capture.path]
    assert shot.camera_replay == {"id": "replay-1"}
    assert camera.queued.get_nowait() == (None, "replay-1", None)


@pytest.fixture(name="sinks")
def fixture_sinks(monkeypatch, tmp_path, emitted, connector, camera):
    """Everything _finalize_shot_detected can write to, with the hardware stubbed out."""
    for name in ("monitor", "kld7_vertical", "kld7_horizontal"):
        monkeypatch.setattr(server, name, None)
    for name in ("ball_speed_correction_enabled", "calculated_spin_enabled"):
        monkeypatch.setattr(server, name, False)
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="mock", trigger_type="manual")
    debug_log = io.StringIO()
    monkeypatch.setattr(server, "get_session_logger", lambda: logger)
    monkeypatch.setattr(server, "debug_mode", True)
    monkeypatch.setattr(server, "debug_log_file", debug_log)

    def reached():
        _join_replay_worker()
        logger.end_session()
        return {
            "session_log": sum(e["type"] == "shot_detected" for e in _session_entries(logger)),
            "simulator": len(connector.sent),
            "replay_queue": camera.queued.qsize(),
            "debug_log": len(debug_log.getvalue().splitlines()),
            "debug_event": sum(event[0] == "debug_shot" for event in emitted),
        }

    return SimpleNamespace(reached=reached)


def test_a_demo_shot_reaches_no_sink_even_through_the_live_finalizer(sinks):
    server._finalize_shot_detected(_shot(demo=True, replay={"id": "replay-1"}), emit_event="shot")

    assert sinks.reached() == {
        "session_log": 0,
        "simulator": 0,
        "replay_queue": 0,
        "debug_log": 0,
        "debug_event": 0,
    }


def test_a_real_shot_reaches_every_sink_through_the_live_finalizer(sinks):
    server._finalize_shot_detected(_shot(replay={"id": "replay-1"}), emit_event="shot")

    assert sinks.reached() == {
        "session_log": 1,
        "simulator": 1,
        "replay_queue": 1,
        "debug_log": 1,
        "debug_event": 1,
    }
