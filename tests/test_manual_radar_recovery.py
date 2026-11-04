"""Manual OPS recovery keeps the customized monitor and its wiring alive."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openflight import server
from openflight.ops243 import OPS243Radar
from openflight.rolling_buffer.monitor import RollingBufferMonitor


@pytest.fixture(name="running_monitor")
def fixture_running_monitor():
    monitor = RollingBufferMonitor(pre_trigger_segments=8, sample_rate_ksps=20)
    radar = Mock()
    entered = threading.Event()

    def idle_reader():
        entered.set()
        monitor._stop_event.wait(5)

    monitor.radar = radar
    monitor._capture_loop = idle_reader
    callbacks = [Mock() for _ in range(7)]
    monitor.set_trigger_gate(object())
    monitor.start(
        *callbacks[:4],
        trigger_rejected_callback=callbacks[4],
        ops_dump_started_callback=callbacks[5],
        ops_dump_finished_callback=callbacks[6],
    )
    assert entered.wait(1)
    yield monitor, radar, callbacks
    monitor.stop()


def test_recovery_keeps_all_callbacks_gate_history_and_club(running_monitor):
    monitor, radar, callbacks = running_monitor
    gate = monitor._trigger_gate
    shot = object()
    monitor._shots.append(shot)
    monitor._shot_sequence_number = 17
    monitor._current_club_selection = "wedge_58"
    old_thread = monitor._capture_thread

    monitor.reinitialize_radar()

    assert not old_thread.is_alive()
    assert monitor._running and monitor._capture_thread.is_alive()
    assert monitor._capture_thread is not old_thread
    radar.prepare_persisted_rolling_buffer.assert_called_once_with(
        pre_trigger_segments=8, sample_rate_ksps=20, transmit_power=None, flush_timeout_s=1.0
    )
    assert [
        monitor._shot_callback,
        monitor._live_callback,
        monitor._diagnostic_callback,
        monitor._processing_callback,
    ] == callbacks[:4]
    assert monitor.trigger._rejection_callback is callbacks[4]
    assert monitor.trigger._dump_started_callback is callbacks[5]
    assert monitor.trigger._dump_finished_callback is callbacks[6]
    assert monitor._trigger_gate is gate
    assert monitor._shots == [shot] and monitor._shot_sequence_number == 17
    assert monitor._current_club_selection == "wedge_58"
    radar.disconnect.assert_not_called()


@pytest.mark.parametrize("condition", ["speed", "stopped"])
def test_unsupported_or_stopped_monitor_is_not_touched(running_monitor, condition):
    monitor, radar, _ = running_monitor
    if condition == "speed":
        monitor.trigger_type = "speed"
    else:
        monitor.stop()
    with pytest.raises(RuntimeError):
        monitor.reinitialize_radar()
    radar.prepare_persisted_rolling_buffer.assert_not_called()


def test_recovery_does_not_write_while_old_reader_is_alive(running_monitor):
    monitor, radar, _ = running_monitor
    monitor.stop()
    monitor._running = True
    monitor._capture_thread = Mock(is_alive=Mock(return_value=True))
    with pytest.raises(RuntimeError, match="still active"):
        monitor.reinitialize_radar()
    radar.prepare_persisted_rolling_buffer.assert_not_called()
    monitor._capture_thread = None


def test_failed_preparation_stays_stopped_and_can_be_retried(running_monitor):
    monitor, radar, _ = running_monitor
    radar.prepare_persisted_rolling_buffer.side_effect = OSError("write timed out")
    with pytest.raises(OSError):
        monitor.reinitialize_radar()
    assert not monitor._running and monitor._capture_thread is None
    radar.prepare_persisted_rolling_buffer.side_effect = None
    monitor.reinitialize_radar()
    assert monitor._running


def test_prepare_preserves_transmit_power_and_reports_rearm_failure():
    radar = OPS243Radar()
    radar.serial = Mock(is_open=True)
    radar.set_units = Mock()
    radar.set_transmit_power = Mock()
    radar.set_sample_rate = Mock()
    radar.rearm_rolling_buffer = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="re-arm"):
        radar.prepare_persisted_rolling_buffer(8, 20, transmit_power=None)
    radar.set_transmit_power.assert_not_called()
    radar.set_sample_rate.assert_called_once_with(20000)
    radar.rearm_rolling_buffer.assert_called_once_with(pre_trigger_segments=8)
    radar.serial.write.assert_not_called()


def test_recovery_reports_a_failed_rearm_and_stays_stopped(running_monitor):
    monitor, radar, _ = running_monitor
    real = OPS243Radar()
    real.serial = Mock(is_open=True)
    real.set_units = Mock()
    real.set_sample_rate = Mock()
    real.rearm_rolling_buffer = Mock(return_value=False)
    radar.prepare_persisted_rolling_buffer.side_effect = real.prepare_persisted_rolling_buffer
    with pytest.raises(RuntimeError, match="re-arm"):
        monitor.reinitialize_radar()
    assert not monitor._running and monitor._capture_thread is None


@pytest.fixture(name="recovery_server")
def fixture_recovery_server(monkeypatch, running_monitor):
    monitor, radar, _ = running_monitor
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server, "mock_mode", False)
    monkeypatch.setattr(server, "demo_session", SimpleNamespace(active=False))
    monkeypatch.setattr(server, "shutdown_cleanup_started", False)
    monkeypatch.setattr(server, "_shot_capture_in_progress", lambda: False)
    return monitor, radar


@pytest.mark.parametrize(
    "guard,code",
    [
        ("mock", "unsupported"),
        ("demo", "demo"),
        ("shutdown", "shutdown"),
        ("capture", "busy"),
        ("no_monitor", "unsupported"),
        ("speed", "unsupported"),
    ],
)
def test_server_rejects_unsafe_recovery_without_serial_writes(
    recovery_server, monkeypatch, guard, code
):
    monitor, radar = recovery_server
    if guard == "mock":
        monkeypatch.setattr(server, "mock_mode", True)
    elif guard == "demo":
        server.demo_session.active = True
    elif guard == "shutdown":
        monkeypatch.setattr(server, "shutdown_cleanup_started", True)
    elif guard == "capture":
        monkeypatch.setattr(server, "_shot_capture_in_progress", lambda: True)
    elif guard == "no_monitor":
        monkeypatch.setattr(server, "monitor", None)
    else:
        monitor.trigger_type = "speed"
    assert server.handle_reinitialize_radar() == {"ok": False, "code": code}
    radar.prepare_persisted_rolling_buffer.assert_not_called()


def test_server_reports_commands_only_and_releases_lock_after_failure(recovery_server):
    _, radar = recovery_server
    radar.prepare_persisted_rolling_buffer.side_effect = OSError("unplugged")
    assert server.handle_reinitialize_radar() == {"ok": False, "code": "failed"}
    radar.prepare_persisted_rolling_buffer.side_effect = None
    assert server.handle_reinitialize_radar() == {"ok": True, "code": "commands_sent"}


def test_concurrent_recovery_is_rejected(recovery_server):
    _, radar = recovery_server
    with server._ops_control_lock:
        assert server.handle_reinitialize_radar() == {"ok": False, "code": "busy"}
    radar.prepare_persisted_rolling_buffer.assert_not_called()


def test_capture_arriving_during_stop_does_not_leave_recovery_busy(monkeypatch):
    monitor = RollingBufferMonitor()
    monitor.radar = Mock()
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server, "mock_mode", False)
    monkeypatch.setattr(server, "demo_session", SimpleNamespace(active=False))
    monkeypatch.setattr(server, "shutdown_cleanup_started", False)
    monkeypatch.setattr(server, "_shot_processing_active", False)
    monkeypatch.setattr(server, "_shot_finalization_order", [])
    monkeypatch.setattr(server, "_shot_finalization_running", False)
    monkeypatch.setattr(server, "iwr6843_runtime", None)
    monkeypatch.setattr(server, "_emit_real_shot_event", Mock())
    entered = threading.Event()

    def racing_wait(**kwargs):
        entered.set()
        kwargs["cancel_event"].wait(5)
        kwargs["capture_started_callback"]()
        return None

    monitor.trigger.wait_for_trigger = racing_wait
    monitor.start(processing_callback=server.on_shot_processing)
    assert entered.wait(1)
    try:
        monitor.radar.prepare_persisted_rolling_buffer.side_effect = OSError("port busy")
        assert server.handle_reinitialize_radar() == {"ok": False, "code": "failed"}
        assert not server._shot_processing_active
        monitor.radar.prepare_persisted_rolling_buffer.side_effect = None
        assert server.handle_reinitialize_radar() == {"ok": True, "code": "commands_sent"}
    finally:
        monitor.stop()


def test_successful_recovery_clears_pending_rearm_without_losing_diagnostics(running_monitor):
    monitor, radar, callbacks = running_monitor
    monitor.trigger._rearm_pending = True
    monitor.trigger._armed_at = 0
    monitor.trigger._clock = lambda: 100
    monitor.trigger._diagnostics.append({"reason": "long_stall"})
    monitor.reinitialize_radar()
    assert monitor.trigger._rearm_pending is False
    assert monitor.trigger._armed_at == 100
    assert monitor.trigger.drain_diagnostics() == [{"reason": "long_stall"}]
    assert monitor.trigger._dump_finished_callback is callbacks[6]
    radar.rearm_rolling_buffer.assert_not_called()


def test_recovery_flush_is_bounded_and_never_calls_tcdrain(monkeypatch):
    radar = OPS243Radar()
    radar.serial = Mock(is_open=True, in_waiting=0, out_waiting=1)
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("openflight.ops243.time.monotonic", lambda: clock.now)
    monkeypatch.setattr(
        "openflight.ops243.time.sleep", lambda delay: setattr(clock, "now", clock.now + delay)
    )
    assert radar.rearm_rolling_buffer(12, flush_timeout_s=0.1) is False
    assert clock.now < 1
    radar.serial.flush.assert_not_called()
    assert radar.serial.write.call_count == 1


def test_tuning_during_recovery_returns_authoritative_config(recovery_server, monkeypatch):
    _, radar = recovery_server
    emit = Mock()
    monkeypatch.setattr(server.socketio, "emit", emit)
    with server.app.test_request_context(), server._ops_control_lock:
        server.request.sid = "requester"
        server.handle_set_radar_config({"transmit_power": 7})
    radar.set_transmit_power.assert_not_called()
    assert emit.call_args_list[0].args == ("radar_config", server.radar_config)
    assert emit.call_args_list[1].args == ("radar_config_error", {"code": "busy"})
    assert all(call.kwargs == {"to": "requester"} for call in emit.call_args_list)


def test_recovery_byte_sequence_never_switches_mode_or_writes_flash(monkeypatch):
    radar = OPS243Radar()
    radar.serial = Mock(is_open=True, in_waiting=0, out_waiting=0)
    radar._read_reply = Mock(return_value="")
    monkeypatch.setattr("openflight.ops243.time.sleep", lambda _delay: None)
    radar.prepare_persisted_rolling_buffer(8, 30, transmit_power=None, flush_timeout_s=1)
    written = b"".join(call.args[0] for call in radar.serial.write.call_args_list)
    assert b"S=30\r" in written and b"S#8\r" in written and written.endswith(b"PA")
    for command in (b"GC", b"GS", b"PI", b"A!", b"PT"):
        assert command not in written
    radar.serial.flush.assert_not_called()


@pytest.fixture(name="connect_recovery_client")
def fixture_connect_recovery_client(recovery_server, monkeypatch):
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(server.socketio.server, name, getattr(server.socketio.server, name))
    monkeypatch.setattr(
        server.socketio.server.eio, "async_handlers", server.socketio.server.eio.async_handlers
    )
    monkeypatch.setattr(server, "_demo_state_payload", lambda: {"active": False})
    monkeypatch.setattr(server, "_session_state_payload", lambda **_kw: {})
    monkeypatch.setattr(server, "_get_trigger_status", lambda: {})
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    monkeypatch.setattr(server, "_emit_profiles", lambda: None)
    monkeypatch.setattr(server, "_reconcile_bag_club", lambda: None)
    clients = []

    def connect():
        client = server.socketio.test_client(server.app, flask_test_client=server.app.test_client())
        assert client.is_connected()
        clients.append(client)
        return client

    yield connect
    for client in clients:
        if client.is_connected():
            client.disconnect()


def test_socket_event_returns_completion_ack(connect_recovery_client):
    client = connect_recovery_client()
    assert client.emit("reinitialize_radar", callback=True) == {"ok": True, "code": "commands_sent"}


def test_tuning_rejection_only_reaches_the_client_that_requested_it(connect_recovery_client):
    requester, observer = connect_recovery_client(), connect_recovery_client()
    requester.get_received()
    observer.get_received()
    with server._ops_control_lock:
        requester.emit("set_radar_config", {"transmit_power": 7})
    assert [event["name"] for event in requester.get_received()] == [
        "radar_config",
        "radar_config_error",
    ]
    assert observer.get_received() == []


# --- OPS 1.3.3 speed trigger (--shot-trigger ops-speed*) ---------------------------------


def _speed_monitor():
    from openflight.ops_speed import OpsSpeedConfig

    config = OpsSpeedConfig(mode="ops-speed", pin=22, magnitude=40, threshold_mph=30)
    return RollingBufferMonitor(
        trigger_type="ops-speed", pre_trigger_segments=2, sample_rate_ksps=20, config=config
    )


@pytest.fixture(name="speed_monitor")
def fixture_speed_monitor():
    monitor = _speed_monitor()
    radar = Mock()
    radar.rearm_rolling_buffer.return_value = True
    entered = threading.Event()

    def idle_reader():
        entered.set()
        monitor._stop_event.wait(5)

    monitor.radar = radar
    monitor._capture_loop = idle_reader
    monitor.start()
    assert entered.wait(1)
    yield monitor, radar
    monitor.stop()


def test_speed_trigger_connect_probes_the_firmware_and_leaves_the_board_idle():
    monitor = _speed_monitor()
    monitor.radar = Mock(fast_rearm=False)
    monitor.connect()
    monitor.radar.connect.assert_called_once_with(probe_firmware=True)
    monitor.radar.prepare_internal_speed_trigger.assert_called_once_with(
        threshold_mph=30,
        magnitude=40,
        pre_trigger_segments=2,
        sample_rate_ksps=20,
        transmit_power=3,
    )
    monitor.radar.prepare_persisted_rolling_buffer.assert_not_called()
    monitor.radar.rearm_rolling_buffer.assert_not_called()
    # The first trigger wait arms it, without the "retrying" warning.
    assert monitor.trigger._rearm_pending and monitor.trigger._initial_arm_pending


def test_speed_trigger_connect_refuses_old_firmware_as_a_second_line_of_defence():
    from openflight.ops243 import OPSFirmwareError

    monitor = _speed_monitor()
    monitor.radar = Mock(fast_rearm=False)
    monitor.radar.prepare_internal_speed_trigger.side_effect = OPSFirmwareError("1.3.0")
    with pytest.raises(OPSFirmwareError):
        monitor.connect()


def test_speed_trigger_recovery_resends_the_speed_setup_and_rearms(speed_monitor):
    monitor, radar = speed_monitor
    monitor.trigger._rearm_pending = True

    monitor.reinitialize_radar()

    radar.prepare_internal_speed_trigger.assert_called_once_with(
        threshold_mph=30,
        magnitude=40,
        pre_trigger_segments=2,
        sample_rate_ksps=20,
        transmit_power=None,
    )
    radar.rearm_rolling_buffer.assert_called_once_with(pre_trigger_segments=2, flush_timeout_s=1.0)
    radar.prepare_persisted_rolling_buffer.assert_not_called()
    assert monitor._running and monitor.trigger._rearm_pending is False


def test_speed_trigger_recovery_reports_a_failed_rearm(speed_monitor):
    from openflight.ops243 import OPSRearmError

    monitor, radar = speed_monitor
    radar.rearm_rolling_buffer.return_value = False
    with pytest.raises(OPSRearmError):
        monitor.reinitialize_radar()
    assert not monitor._running


def test_server_offers_manual_recovery_in_the_speed_trigger_modes(speed_monitor, monkeypatch):
    monitor, radar = speed_monitor
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server, "mock_mode", False)
    monkeypatch.setattr(server, "demo_session", SimpleNamespace(active=False))
    monkeypatch.setattr(server, "shutdown_cleanup_started", False)
    monkeypatch.setattr(server, "_shot_capture_in_progress", lambda: False)
    assert server.handle_reinitialize_radar() == {"ok": True, "code": "commands_sent"}
    radar.prepare_internal_speed_trigger.assert_called_once()
