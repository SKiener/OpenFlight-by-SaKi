"""The demo mode on the server: the switch, the session routing, the ticker and real shots."""

import logging
import queue
import sys
import threading
import time
from datetime import datetime
from random import Random
from types import SimpleNamespace

import pytest

from openflight import server
from openflight.clubs import ClubType
from openflight.demo import DemoSession
from openflight.profiles import ProfileStore
from openflight.shot_generator import generate_shot
from openflight.sim.types import ConnectionState


class _Log:
    """A session log that records what the server hands to it."""

    def __init__(self):
        self.stats = {}
        self.shots = []
        self.sim_sends = []

    def log_shot(self, shot, pipeline_ms=None, log_only_fields=None):
        del pipeline_ms
        self.shots.append(shot)

    def log_sim_send(self, **entry):
        self.sim_sends.append(entry)


class _Simulator:
    """A connected simulator that keeps every shot it is sent."""

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


def _wait_for(condition, timeout_s=5.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _finalization_idle():
    with server._shot_finalization_condition:
        return not server._shot_finalization_order and not server._shot_finalization_running


@pytest.fixture(name="demo_server", autouse=True)
def fixture_demo_server(tmp_path, monkeypatch):
    """A mock monitor wired like the app, a fresh roster and a fresh, inactive demo."""
    monitor = server.MockLaunchMonitor()
    monitor.connect()
    monitor.start(shot_callback=server.on_shot_detected)
    log = _Log()
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server, "profile_store", store)
    monkeypatch.setattr(server, "get_session_logger", lambda: log)
    monkeypatch.setattr(server, "power_monitor", None)
    monkeypatch.setattr(server, "sim_connectors", [])
    monkeypatch.setattr(server, "mock_mode", True)
    monkeypatch.setattr(server, "debug_mode", False)
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "ball_speed_correction_enabled", False)
    monkeypatch.setattr(server, "calculated_spin_enabled", False)
    monkeypatch.setattr(server, "camera_replay_manager", None)
    monkeypatch.setattr(server, "_DEMO_TICK_S", 0.01)
    monkeypatch.setattr(
        server,
        "demo_session",
        DemoSession(rng=Random(2026), finalize=server._finalize_shot_metrics),
    )
    server._reset_shot_sequence()
    yield SimpleNamespace(monitor=monitor, log=log, store=store)
    server.demo_session.stop()
    for thread in threading.enumerate():
        if thread.name == "demo-ticker":
            thread.join(timeout=5)
            assert not thread.is_alive()
    _wait_for(_finalization_idle)


@pytest.fixture(name="connect")
def fixture_connect(monkeypatch):
    """Kiosk Socket.IO test clients; restores the server hooks the test client patches."""
    sio = server.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(sio, name, getattr(sio, name))
    monkeypatch.setattr(sio.eio, "async_handlers", sio.eio.async_handlers)
    clients = []

    def connect():
        client = server.socketio.test_client(server.app, flask_test_client=server.app.test_client())
        clients.append(client)
        return client

    yield connect
    for client in clients:
        if client.is_connected():
            client.disconnect()


@pytest.fixture(name="emits")
def fixture_emits(monkeypatch):
    events = []
    monkeypatch.setattr(
        server.socketio, "emit", lambda event, payload: events.append((event, payload))
    )
    return events


@pytest.fixture(name="simulator")
def fixture_simulator(monkeypatch):
    simulator = _Simulator()
    monkeypatch.setattr(server, "sim_connectors", [simulator])
    monkeypatch.setattr(server, "sim_player_state", server.SimPlayerState())
    return simulator


def _received(client):
    """What a test client got since the last call, as (event, payload) pairs."""
    return [(packet["name"], packet["args"][0]) for packet in client.get_received()]


def _names(events):
    return [event for event, _payload in events]


def _timestamps(shots):
    return [shot["timestamp"] for shot in shots]


def _real_shots(monitor, count):
    """Real shots through the whole pipeline, finalized and in the monitor's session."""
    shots = [monitor.simulate_shot() for _ in range(count)]
    _wait_for(_finalization_idle)
    return shots


def test_switching_on_swaps_every_client_to_six_demo_shots(connect):
    kiosk, phone = connect(), connect()
    _received(kiosk)
    _received(phone)

    kiosk.emit("set_demo_mode", {"enabled": True})

    for client in (kiosk, phone):
        events = _received(client)
        assert _names(events) == ["demo_state", "session_state"]
        assert events[0][1] == {"active": True}
        state = events[1][1]
        assert state["demo_active"] is True
        assert len(state["shots"]) == 6
        for shot in state["shots"]:
            assert shot["demo"] is True
            assert shot["carry_spin_adjusted"] is not None
            assert shot["flight_path"] is not None
            # The apex of that same flight travels with the carry.
            assert shot["apex_yards"] == pytest.approx(
                max(point[3] for point in shot["flight_path"]["points"]), abs=0.3
            )
        assert state["stats"]["shot_count"] == 6
        assert state["stats"]["mode"] == "demo"


def test_demo_shots_reach_the_screens_with_their_face_data(connect):
    kiosk = connect()
    _received(kiosk)

    kiosk.emit("set_demo_mode", {"enabled": True})

    shots = dict(_received(kiosk))["session_state"]["shots"]
    assert len(shots) == 6
    for shot in shots:
        assert shot["impact_method"] == "D"
        assert isinstance(shot["impact_offset_mm"], float)
        assert isinstance(shot["impact_sigma_mm"], float)
        assert shot["face_to_path_class"] in ("closed", "neutral", "open")
        assert isinstance(shot["face_to_path_deg"], float)


def test_the_demo_plays_the_active_profiles_bag(connect, demo_server):
    profile = demo_server.store.get_active()
    demo_server.store.set_clubs(profile.id, ["7-iron", "driver"])
    kiosk = connect()
    _received(kiosk)

    kiosk.emit("set_demo_mode", {"enabled": True})

    shots = _received(kiosk)[1][1]["shots"]
    assert [shot["club"] for shot in shots] == ["7-iron", "driver"] * 3
    assert {(shot["profile_id"], shot["profile_name"]) for shot in shots} == {
        (profile.id, profile.name)
    }


def test_switching_off_restores_the_real_session_exactly(connect, demo_server):
    real = _real_shots(demo_server.monitor, 3)
    kiosk = connect()
    before = dict(_received(kiosk))["session_state"]

    kiosk.emit("set_demo_mode", {"enabled": True})
    kiosk.emit("set_demo_mode", {"enabled": False})

    events = _received(kiosk)
    assert _names(events) == ["demo_state", "session_state", "demo_state", "session_state"]
    assert events[2][1] == {"active": False}
    after = events[3][1]
    assert after["demo_active"] is False
    assert after["shots"] == before["shots"]
    assert after["stats"] == before["stats"]
    assert _timestamps(after["shots"]) == [shot.timestamp.isoformat() for shot in real]
    assert demo_server.monitor.get_shots() == real


def test_clear_and_delete_touch_only_demo_shots(connect, demo_server):
    real = _real_shots(demo_server.monitor, 2)
    kiosk = connect()
    _received(kiosk)
    kiosk.emit("set_demo_mode", {"enabled": True})
    demo = _received(kiosk)[1][1]["shots"]

    kiosk.emit("delete_shot", {"timestamp": demo[0]["timestamp"]})
    kiosk.emit("delete_shot", {"timestamp": real[0].timestamp.isoformat()})
    kiosk.emit("clear_session", {})

    events = _received(kiosk)
    assert _names(events) == ["session_state", "delete_shot_error", "session_cleared"]
    assert _timestamps(events[0][1]["shots"]) == _timestamps(demo[1:])
    assert events[2][1]["shots"] == []
    assert demo_server.monitor.get_shots() == real
    kiosk.emit("set_demo_mode", {"enabled": False})
    after = _received(kiosk)[1][1]
    assert _timestamps(after["shots"]) == [shot.timestamp.isoformat() for shot in real]


def test_a_client_joining_mid_demo_gets_the_demo(connect):
    kiosk = connect()
    kiosk.emit("set_demo_mode", {"enabled": True})
    demo = dict(_received(kiosk))["session_state"]["shots"]

    phone = connect()

    events = dict(_received(phone))
    assert events["demo_state"] == {"active": True}
    assert events["session_state"]["demo_active"] is True
    assert _timestamps(events["session_state"]["shots"]) == _timestamps(demo)


def test_swing_speed_mode_refuses_the_demo(connect, monkeypatch):
    monkeypatch.setattr(server, "monitor", server.MockSwingSpeedMonitor())
    refused = ("demo_state", {"active": False, "unavailable": "swing-speed"})
    kiosk = connect()
    assert refused in _received(kiosk)

    kiosk.emit("set_demo_mode", {"enabled": True})

    assert _received(kiosk) == [refused]
    assert not server.demo_session.active


def test_switching_twice_is_harmless(connect):
    kiosk, phone = connect(), connect()
    _received(kiosk)
    _received(phone)

    kiosk.emit("set_demo_mode", {"enabled": True})
    phone.emit("set_demo_mode", {"enabled": True})

    assert _names(_received(phone)) == ["demo_state", "session_state", "demo_state"]
    assert len(server.demo_session.shots()) == 6

    kiosk.emit("set_demo_mode", {"enabled": False})
    phone.emit("set_demo_mode", {"enabled": False})

    events = _received(phone)
    assert _names(events) == ["demo_state", "session_state", "demo_state"]
    assert events[2][1] == {"active": False}


def test_two_screens_switching_at_once_start_one_demo(emits):
    barrier = threading.Barrier(4)

    def switch_on():
        barrier.wait()
        server.handle_set_demo_mode({"enabled": True})

    threads = [threading.Thread(target=switch_on) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert _names(emits).count("demo_state") == 4
    assert _names(emits).count("session_state") == 1
    assert len(server.demo_session.shots()) == 6


@pytest.mark.parametrize(
    "payload",
    [None, "on", {}, {"enabled": None}, {"enabled": "true"}, {"enabled": 1}, {"enabled": 0}],
    ids=["none", "text", "empty", "enabled-none", "enabled-text", "enabled-1", "enabled-0"],
)
def test_a_switch_without_a_bool_changes_nothing(emits, payload):
    server.handle_set_demo_mode(payload)

    assert emits == []
    assert not server.demo_session.active

    server.handle_set_demo_mode({"enabled": True})
    started = len(emits)
    server.handle_set_demo_mode(payload)

    assert emits[started:] == []
    assert server.demo_session.active


def test_demo_touch_reaches_the_session(monkeypatch, connect):
    touches = []
    monkeypatch.setattr(server.demo_session, "touch", touches.append)
    kiosk = connect()
    before = time.monotonic()

    kiosk.emit("demo_touch")

    assert len(touches) == 1
    assert before <= touches[0] <= time.monotonic()


def _run_main(monkeypatch, tmp_path, *arguments):
    """The server's main() on the mock backend, without a monitor, a log or a web server."""
    for name in (
        "air_density_kg_m3",
        "battery_provider",
        "ball_speed_correction_distance_ft",
        "ball_speed_correction_ball_above_radar_ft",
        "_VERTICAL_RADAR_GATE_BYPASS",
    ):
        monkeypatch.setattr(server, name, getattr(server, name))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--mock",
            "--profiles-path",
            str(tmp_path / "flag-profiles.json"),
            *arguments,
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    server.main()


def test_the_interval_flag_reaches_the_demo(monkeypatch, tmp_path):
    _run_main(monkeypatch, tmp_path, "--demo-interval-s", "2")
    session = server.demo_session
    session.start(now_s=0.0, wall=datetime.now(), bag_ids=None, profile_id="", profile_name="")

    assert session.due_shot(now_s=1.9, wall=datetime.now(), profile_id="", profile_name="") is None
    assert session.due_shot(now_s=2.0, wall=datetime.now(), profile_id="", profile_name="")


@pytest.mark.parametrize("seconds", ["0", "-1", "nan", "inf"])
def test_the_interval_flag_wants_a_positive_finite_number(monkeypatch, tmp_path, seconds):
    with pytest.raises(SystemExit) as exit_info:
        _run_main(monkeypatch, tmp_path, f"--demo-interval-s={seconds}")

    assert exit_info.value.code == 2


def test_the_interval_flag_is_not_in_the_help(monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _run_main(monkeypatch, tmp_path, "--help")

    assert exit_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--mock-swing-speed" in help_text
    assert "--demo-interval-s" not in help_text


def _same_shot_twice(club, seed):
    """A demo shot and its real twin with the same generated values."""
    demo, real = (generate_shot(club, Random(seed), mode="demo") for _ in range(2))
    demo.demo = True
    return demo, real


def test_demo_shots_skip_the_cosine_correction(monkeypatch):
    monkeypatch.setattr(server, "ball_speed_correction_enabled", True)
    demo, real = _same_shot_twice(ClubType.LW, 4)
    generated = demo.ball_speed_mph

    server._finalize_shot_metrics(demo)
    server._finalize_shot_metrics(real)

    assert demo.ball_speed_mph == generated
    assert demo.ball_speed_raw_mph is None
    assert real.ball_speed_raw_mph == generated
    assert real.ball_speed_mph > generated


@pytest.mark.parametrize(
    ("ballistics", "carry_line"),
    [(True, "[SERVER] Ballistic carry"), (False, "[SERVER] Table carry (ballistics disabled)")],
    ids=["ballistic", "table"],
)
def test_only_real_shots_reach_the_info_log(monkeypatch, caplog, emits, ballistics, carry_line):
    monkeypatch.setattr(server, "ballistics_enabled", ballistics)
    monkeypatch.setattr(server, "ball_speed_correction_enabled", True)
    caplog.set_level(logging.DEBUG, logger=server.logger.name)
    demo, real = _same_shot_twice(ClubType.IRON_7, 9)

    def levels(shot):
        caplog.clear()
        server._finalize_shot_detected(shot, emit_event="shot")
        return {
            record.getMessage().split(":")[0]: record.levelname
            for record in caplog.records
            if record.name == server.logger.name
        }

    assert levels(demo) == {carry_line: "DEBUG", "[SERVER] Shot": "DEBUG"}
    assert levels(real) == {
        "[SERVER] Ball speed cosine correction": "INFO",
        carry_line: "INFO",
        "[SERVER] Shot": "INFO",
    }


def _use_demo(monkeypatch, **timing):
    monkeypatch.setattr(
        server,
        "demo_session",
        DemoSession(rng=Random(7), finalize=server._finalize_shot_metrics, **timing),
    )


def _switch_on():
    """Switch the demo on and return its ticker before the ticker gets to tick."""
    with server._demo_lock:
        server.handle_set_demo_mode({"enabled": True})
        return server._demo_ticker


def _shot_events(events):
    """The events that put a shot, or a shot's progress, on a screen."""
    return [
        (event, payload)
        for event, payload in events
        if event in ("shot", "shot_update", "shot_processing")
    ]


def test_a_new_demo_shot_arrives_on_the_interval(monkeypatch, emits):
    _use_demo(monkeypatch, interval_s=0.2)
    server.handle_set_demo_mode({"enabled": True})

    _wait_for(lambda: "shot" in _names(emits))

    payload = next(payload for event, payload in emits if event == "shot")
    assert payload["shot"]["demo"] is True
    assert payload["shot"]["shot_number"] == 7
    assert payload["shot"]["carry_spin_adjusted"] is not None
    assert payload["shot"]["flight_path"] is not None
    assert payload["shot"]["apex_yards"] > 0
    assert payload["stats"]["shot_count"] == 7
    assert payload["stats"]["mode"] == "demo"


def test_the_demo_ends_itself_after_the_idle_timeout(monkeypatch, emits, demo_server):
    real = _real_shots(demo_server.monitor, 1)
    _use_demo(monkeypatch, idle_timeout_s=0.5)
    ticker = _switch_on()

    ticker.join(timeout=5)

    assert not ticker.is_alive()
    assert not server.demo_session.active
    assert _names(emits[-2:]) == ["demo_state", "session_state"]
    assert emits[-2][1] == {"active": False}
    state = emits[-1][1]
    assert state["demo_active"] is False
    assert _timestamps(state["shots"]) == [real[0].timestamp.isoformat()]


def test_the_ticker_runs_only_while_the_demo_does():
    ticker = _switch_on()
    with server._demo_lock:
        server.handle_set_demo_mode({"enabled": False})
        server.handle_set_demo_mode({"enabled": True})

    assert server._demo_ticker is ticker
    assert ticker.is_alive()

    server.handle_set_demo_mode({"enabled": False})
    ticker.join(timeout=5)

    assert not ticker.is_alive()
    assert server._demo_ticker is None


def test_a_failing_demo_shot_does_not_stop_the_ticker(monkeypatch, caplog, emits):
    def finalize(shot):
        if shot.shot_number == 7:
            raise RuntimeError("no flight")
        server._finalize_shot_metrics(shot)

    monkeypatch.setattr(
        server, "demo_session", DemoSession(rng=Random(7), finalize=finalize, interval_s=0.05)
    )
    server.handle_set_demo_mode({"enabled": True})

    _wait_for(lambda: "shot" in _names(emits))

    assert next(payload for event, payload in emits if event == "shot")["shot"]["shot_number"] == 8
    assert "[DEMO] Tick failed: no flight" in caplog.messages


def test_a_real_shot_during_the_demo_is_dropped_everywhere(
    monkeypatch, caplog, emits, demo_server, simulator
):
    monkeypatch.setattr(server, "_shot_processing_active", False)
    caplog.set_level(logging.INFO, logger=server.logger.name)
    server.handle_set_demo_mode({"enabled": True})

    server.on_shot_processing("calculating")
    shot = demo_server.monitor.simulate_shot(ball_speed=123.4)
    _wait_for(_finalization_idle)

    assert _shot_events(emits) == []
    assert caplog.messages.count("[DEMO] real shot ignored (123.4 mph)") == 1
    assert demo_server.monitor.get_shots() == []
    assert demo_server.log.shots == []
    assert simulator.sent == []
    assert shot.shot_number is None
    assert shot.profile_id == ""
    server.handle_set_demo_mode({"enabled": False})
    assert emits[-1][1]["shots"] == []
    assert not server._shot_capture_in_progress()


def _hold_enrichment(monkeypatch):
    """Optional hardware that keeps a real shot in processing until the test lets it finish."""
    workers = []
    monkeypatch.setattr(server, "camera_capture_runtime", object())
    monkeypatch.setattr(
        server,
        "shot_enrichment_queue",
        queue.Queue(maxsize=server._SHOT_ENRICHMENT_QUEUE_CAPACITY),
    )
    monkeypatch.setattr(server, "shot_enrichment_task", None)
    monkeypatch.setattr(
        server.socketio,
        "start_background_task",
        lambda target, *_args, **_kwargs: workers.append(target) or object(),
    )
    monkeypatch.setattr(
        server, "_enrich_shot_from_optional_hardware", lambda _shot: server._ShotEnrichmentResult()
    )
    return workers


def test_a_real_shot_in_processing_is_kept_but_not_shown(
    monkeypatch, emits, demo_server, simulator
):
    finish_enrichment = _hold_enrichment(monkeypatch)
    shot = generate_shot(ClubType.IRON_7, Random(3), mode="rolling-buffer")
    demo_server.monitor._shots.append(shot)
    server.on_shot_detected(shot)
    assert _names(_shot_events(emits)) == ["shot"]
    server.handle_set_demo_mode({"enabled": True})
    started = len(emits)

    finish_enrichment[0]()
    _wait_for(_finalization_idle)

    assert _shot_events(emits[started:]) == []
    assert demo_server.log.shots == [shot]
    assert len(simulator.sent) == 1
    server.handle_set_demo_mode({"enabled": False})
    after = emits[-1][1]["shots"]
    assert _timestamps(after) == [shot.timestamp.isoformat()]
    assert after[0]["carry_spin_adjusted"] is not None


def test_real_shot_progress_is_not_shown_during_the_demo(monkeypatch, emits):
    monkeypatch.setattr(server, "_shot_processing_active", False)
    shot = generate_shot(ClubType.DRIVER, Random(5), mode="rolling-buffer")
    server.handle_set_demo_mode({"enabled": True})
    started = len(emits)

    server.on_shot_processing("capturing")
    published = server._emit_initial_ops_shot(shot)
    server._emit_ops_enrichment_skipped(shot, reason="queue_full")
    server._emit_iwr6843_trigger_status(shot, state="error", reason="no capture")

    assert emits[started:] == []
    assert published is True
    assert server._shot_processing_active is True


def test_demo_shots_never_reach_logger_or_simulators(monkeypatch, emits, demo_server, simulator):
    _use_demo(monkeypatch, interval_s=0.05)
    server.handle_set_demo_mode({"enabled": True})

    _wait_for(lambda: _names(emits).count("shot") >= 2)
    server.handle_set_demo_mode({"enabled": False})

    assert demo_server.log.shots == []
    assert demo_server.log.sim_sends == []
    assert simulator.sent == []
    assert "sim_shot" not in _names(emits)
