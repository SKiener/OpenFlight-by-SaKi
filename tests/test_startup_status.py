import json
import sys

import pytest

from openflight.startup_status import (
    StartupComponent,
    StartupStatusReporter,
    complete_startup_status,
    configured_startup_components,
    fail_startup_status,
    initialize_startup_status,
)


def test_reporter_writes_versioned_status_and_omits_disabled_components(tmp_path):
    status_path = tmp_path / "status.json"
    reporter = StartupStatusReporter(
        status_path,
        [
            StartupComponent("ops", "OPS radar"),
            StartupComponent("camera", "Camera"),
        ],
    )

    reporter.start("ops", "Connecting OPS radar")
    reporter.ready("ops", "OPS radar connected")

    assert json.loads(status_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "overall": "starting",
        "message": "OPS radar connected",
        "components": [
            {"id": "ops", "label": "OPS radar", "state": "ready"},
            {"id": "camera", "label": "Camera", "state": "waiting"},
        ],
    }


def test_reporter_rejects_unknown_components(tmp_path):
    reporter = StartupStatusReporter(
        tmp_path / "status.json", [StartupComponent("ops", "OPS radar")]
    )

    with pytest.raises(KeyError, match="camera"):
        reporter.start("camera")


def test_reporter_marks_overall_ready_after_components_are_initialized(tmp_path):
    status_path = tmp_path / "status.json"
    reporter = StartupStatusReporter(status_path, [StartupComponent("ops", "OPS radar")])

    reporter.start("ops")
    reporter.ready("ops")
    reporter.finish("OpenFlight is ready")

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["overall"] == "ready"
    assert payload["message"] == "OpenFlight is ready"


def test_reporter_replaces_status_atomically(tmp_path, monkeypatch):
    status_path = tmp_path / "status.json"
    replacements = []

    def record_replace(source, destination):
        replacements.append((source, destination, source.read_text(encoding="utf-8")))

    monkeypatch.setattr("openflight.startup_status.os.replace", record_replace)
    reporter = StartupStatusReporter(status_path, [StartupComponent("ops", "OPS radar")])

    reporter.start("ops")

    assert replacements
    source, destination, content = replacements[-1]
    assert source.parent == status_path.parent
    assert destination == status_path
    assert json.loads(content)["components"][0]["state"] == "starting"


def test_configured_components_hide_disabled_hardware():
    components = configured_startup_components(
        mock=False,
        camera=False,
        iwr6843=True,
        inclinometer=True,
        kld7=False,
        kld7_horizontal=False,
        battery=False,
        simulators=False,
    )

    assert [(item.component_id, item.label) for item in components] == [
        ("server", "OpenFlight server"),
        ("ops", "OPS radar"),
        ("ti", "TI radar"),
        ("inclinometer", "Inclinometer"),
    ]


def test_mock_mode_replaces_ops_with_shot_simulator():
    components = configured_startup_components(
        mock=True,
        camera=False,
        iwr6843=False,
        inclinometer=False,
        kld7=False,
        kld7_horizontal=False,
        battery=False,
        simulators=False,
    )

    assert [(item.component_id, item.label) for item in components] == [
        ("server", "OpenFlight server"),
        ("monitor", "Shot simulator"),
    ]


def test_initial_status_uses_final_component_order_and_starts_server(tmp_path):
    status_path = tmp_path / "status.json"

    initialize_startup_status(
        status_path,
        mock=False,
        camera=False,
        iwr6843=True,
        inclinometer=False,
        kld7=False,
        kld7_horizontal=False,
        battery=False,
        simulators=False,
    )

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["message"] == "Preparing OpenFlight server"
    assert payload["components"] == [
        {"id": "server", "label": "OpenFlight server", "state": "starting"},
        {"id": "ops", "label": "OPS radar", "state": "waiting"},
        {"id": "ti", "label": "TI radar", "state": "waiting"},
    ]


def test_shell_readiness_boundary_marks_server_and_overall_ready(tmp_path):
    status_path = tmp_path / "status.json"
    reporter = StartupStatusReporter(status_path, [StartupComponent("server", "OpenFlight server")])
    reporter.start("server", "Starting OpenFlight server")

    complete_startup_status(status_path)

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["overall"] == "ready"
    assert payload["message"] == "OpenFlight is ready"
    assert payload["components"] == [
        {"id": "server", "label": "OpenFlight server", "state": "ready"}
    ]


def test_failure_identifies_component_recovery_and_log_location(tmp_path):
    status_path = tmp_path / "status.json"
    reporter = StartupStatusReporter(
        status_path,
        [
            StartupComponent("ti", "TI radar"),
            StartupComponent("ops", "OPS radar"),
        ],
    )
    reporter.start("ti")

    fail_startup_status(
        status_path,
        component_id="ti",
        message="TI radar failed to initialize",
        recovery="Check the TI radar USB and power connections, then relaunch OpenFlight.",
        log_path="/home/pacinoj/openflight_sessions/terminal_logs/",
    )

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["overall"] == "error"
    assert payload["message"] == "TI radar failed to initialize"
    assert payload["components"][0]["state"] == "error"
    assert payload["error"] == {
        "recovery": "Check the TI radar USB and power connections, then relaunch OpenFlight.",
        "log_path": "/home/pacinoj/openflight_sessions/terminal_logs/",
    }


def test_generic_failure_preserves_an_existing_component_error(tmp_path):
    status_path = tmp_path / "status.json"
    StartupStatusReporter(status_path, [StartupComponent("ops", "OPS radar")])
    fail_startup_status(
        status_path,
        component_id="ops",
        message="OPS radar failed to initialize",
        recovery="Reconnect the OPS radar.",
        log_path="/logs",
    )

    changed = fail_startup_status(
        status_path,
        component_id=None,
        message="OpenFlight server exited during startup",
        recovery="Relaunch OpenFlight.",
        log_path="/logs",
        preserve_existing=True,
    )

    assert changed is False
    assert json.loads(status_path.read_text(encoding="utf-8"))["message"] == (
        "OPS radar failed to initialize"
    )


def test_reporter_can_mark_optional_component_skipped(tmp_path):
    status_path = tmp_path / "status.json"
    reporter = StartupStatusReporter(status_path, [StartupComponent("camera", "Camera")])

    reporter.skip("camera", "Camera unavailable; continuing")

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["components"][0]["state"] == "skipped"


def test_server_publishes_kld7_and_ops_success(tmp_path, monkeypatch):
    """The real sequential startup boundaries should publish their success events."""
    from openflight import server

    status_path = tmp_path / "status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--kld7",
            "--kld7-mount-tilt",
            "10",
            "--startup-status-file",
            str(status_path),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "init_kld7", lambda **_kwargs: True)
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)

    server.main()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    states = {component["id"]: component["state"] for component in payload["components"]}
    assert payload["overall"] == "starting"
    assert states == {
        "kld7_vertical": "ready",
        "ops": "ready",
        "server": "starting",
    }


@pytest.mark.parametrize(
    ("camera_available", "expected_state"),
    [(True, "ready"), (False, "skipped")],
)
def test_server_publishes_high_speed_camera_status_without_blocking_ops(
    tmp_path, monkeypatch, camera_available, expected_state
):
    """Optional OV9281 startup should report status and preserve the OPS path."""
    from openflight import server

    status_path = tmp_path / "status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--camera-capture",
            "--startup-status-file",
            str(status_path),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(
        server,
        "init_camera_capture",
        lambda **_kwargs: camera_available,
    )
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)

    server.main()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    states = {component["id"]: component["state"] for component in payload["components"]}
    assert states["camera"] == expected_state
    assert states["ops"] == "ready"


def test_server_publishes_ops_failure_and_cleans_up(tmp_path, monkeypatch):
    from openflight import server

    status_path = tmp_path / "status.json"
    cleanup_calls = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--startup-status-file",
            str(status_path),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)

    def fail_monitor(**_kwargs):
        raise RuntimeError("serial port unavailable")

    monkeypatch.setattr(server, "start_monitor", fail_monitor)
    monkeypatch.setattr(
        server, "_cleanup_hardware_for_shutdown", lambda: cleanup_calls.append("cleanup")
    )

    with pytest.raises(RuntimeError, match="serial port unavailable"):
        server.main()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    states = {component["id"]: component["state"] for component in payload["components"]}
    assert payload["overall"] == "error"
    assert payload["message"] == "OPS radar failed to initialize"
    assert states["ops"] == "error"
    assert cleanup_calls == ["cleanup"]


def _ti_wedge_recovery(cause: str) -> str:
    return (
        f"TI radar is not responding ({cause}). Shut the Pi down completely (not restart) "
        "and power it on again, or unplug the TI USB cable for 5 s. Then start OpenFlight."
    )


@pytest.mark.parametrize(
    ("initialization_error", "expected_recovery"),
    [
        (
            "IWR6843 did not acknowledge 'sensorStop'; the firmware may be wedged "
            "(press RESET and retry)",
            _ti_wedge_recovery("no reply to commands"),
        ),
        (
            "config rejected: 'sensorStop': sensorStop\n"
            "Error: HWA shutdown boundary timed out\nError -1\nl3dump:/>",
            _ti_wedge_recovery("HWA chain stalled"),
        ),
        (
            "IWR6843 did not acknowledge 'sensorStart'; the firmware may be wedged "
            "(press RESET and retry) [TI wedge: no RF frames]; reply: stats frames=0",
            _ti_wedge_recovery("no RF frames"),
        ),
        (
            "IWR6843 serial port is unavailable",
            "Check the TI radar USB and power connections, then relaunch OpenFlight.",
        ),
    ],
)
def test_server_publishes_specific_ti_recovery(
    tmp_path, monkeypatch, initialization_error, expected_recovery
):
    """A wedged radar needs different operator action than an unplugged radar."""
    from openflight import server

    status_path = tmp_path / "status.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "openflight-server",
            "--no-logging",
            "--iwr6843",
            "--startup-status-file",
            str(status_path),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)

    def fail_iwr6843(**_kwargs):
        server.iwr6843_runtime_config = {
            "enabled": False,
            "error": initialization_error,
        }
        return False

    monkeypatch.setattr(server, "init_iwr6843", fail_iwr6843)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)

    with pytest.raises(SystemExit) as exit_info:
        server.main()

    assert exit_info.value.code == 1
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["overall"] == "error"
    assert payload["message"] == "TI radar failed to initialize"
    assert payload["error"]["recovery"] == expected_recovery


def test_iwr6843_startup_recovery_texts_follow_ti_wedged_error_causes():
    from openflight import server
    from openflight.iwr6843.health import HWA_STALL, SILENT_CLI, TiWedgedError

    stall = TiWedgedError(HWA_STALL, "sensorStop", "Error: HWA shutdown boundary timed out")
    silent = TiWedgedError(SILENT_CLI, "flushCfg", "")

    assert server._iwr6843_startup_recovery(stall) == _ti_wedge_recovery("HWA chain stalled")
    assert server._iwr6843_startup_recovery(silent) == _ti_wedge_recovery("no reply to commands")
    assert server._iwr6843_startup_recovery(None) == (
        "Check the TI radar USB and power connections, then relaunch OpenFlight."
    )


# --- Opt-in TI recovery by USB power cycle at startup ---------------------------

_WEDGE_ERROR = (
    "IWR6843 did not acknowledge 'sensorStop'; the firmware may be wedged "
    "(press RESET and retry) [TI wedge: no reply to commands]"
)
_TI_PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2105_test-if00-port0"
_OPS_UART = "/dev/ttyAMA0"


class _InclinometerReached(Exception):
    """Raised by the step after TI init so a successful start stops there."""


class _FakeStartupCycler:
    def __init__(self, *, status_path=None, error=None, permitted=True):
        self.cycles = 0
        self.error = error
        self.allowed = permitted
        self.status_path = status_path
        self.status_during_cycle = None

    def permitted(self):
        return self.allowed

    def cycle(self):
        self.cycles += 1
        if self.status_path is not None:
            self.status_during_cycle = json.loads(self.status_path.read_text(encoding="utf-8"))
        if self.error is not None:
            raise self.error
        return 3.0


def _run_ti_startup(
    tmp_path,
    monkeypatch,
    *,
    init_results,
    flag=True,
    cycler_error=None,
    port_returns=True,
    ti_port=_TI_PORT,
    ops_port=_OPS_UART,
    permitted=True,
    usb_storage=(),
):
    """Drive ``server.main`` through the TI block with scripted init results."""
    from types import SimpleNamespace

    from openflight import server
    from openflight.iwr6843 import usb_power

    status_path = tmp_path / "status.json"
    argv = [
        "openflight-server",
        "--no-logging",
        "--iwr6843",
        "--inclinometer",
        "--startup-status-file",
        str(status_path),
    ]
    if ti_port is not None:
        argv += ["--iwr6843-port", ti_port]
    if ops_port is not None:
        argv += ["--port", ops_port]
    if flag:
        argv.append("--iwr6843-usb-power-cycle")
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    for name in (
        "iwr6843_runtime",
        "iwr6843_runtime_config",
        "ball_speed_correction_enabled",
        "ball_speed_correction_distance_ft",
        "ball_speed_correction_ball_above_radar_ft",
    ):
        monkeypatch.setattr(server, name, getattr(server, name))

    cyclers = []

    def make_cycler(**_kwargs):
        cycler = _FakeStartupCycler(
            status_path=status_path, error=cycler_error, permitted=permitted
        )
        cyclers.append(cycler)
        return cycler

    monkeypatch.setattr(usb_power, "UsbPowerCycler", make_cycler)
    monkeypatch.setattr(usb_power, "usb_mounted_block_devices", lambda: list(usb_storage))
    waits = []

    def fake_wait(port, **_kwargs):
        waits.append(port)
        return port_returns

    monkeypatch.setattr(usb_power, "wait_for_tty", fake_wait)

    results = iter(init_results)
    init_calls = []

    def fake_init(**kwargs):
        init_calls.append(kwargs)
        result = next(results)
        if result is True:
            server.iwr6843_runtime = SimpleNamespace(
                calibration=SimpleNamespace(tee_ball_height_m=0.04, radar_height_m=0.3),
                tx_order="normal",
            )
            server.iwr6843_runtime_config = {"enabled": True}
            return True
        server.iwr6843_runtime_config = {"enabled": False, "error": result}
        return False

    monkeypatch.setattr(server, "init_iwr6843", fake_init)

    def reach_inclinometer(**_kwargs):
        raise _InclinometerReached

    monkeypatch.setattr(server, "init_inclinometer", reach_inclinometer)

    outcome = None
    try:
        server.main()
    except _InclinometerReached:
        outcome = "continued"
    except SystemExit as exit_info:
        outcome = exit_info.code
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    return SimpleNamespace(
        outcome=outcome,
        payload=payload,
        states={item["id"]: item["state"] for item in payload["components"]},
        cyclers=cyclers,
        waits=waits,
        init_calls=init_calls,
    )


def test_usb_power_cycle_flag_defaults_off_and_passes_no_cycler(tmp_path, monkeypatch):
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[True], flag=False)

    assert run.outcome == "continued"
    assert run.init_calls[0]["usb_power_cycler"] is None
    assert run.cyclers == []


def test_usb_power_cycle_flag_passes_one_cycler_to_init(tmp_path, monkeypatch):
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[True])

    assert run.outcome == "continued"
    assert len(run.cyclers) == 1
    assert run.init_calls[0]["usb_power_cycler"] is run.cyclers[0]
    assert run.init_calls[0]["port"] == _TI_PORT
    assert run.cyclers[0].cycles == 0


@pytest.mark.parametrize(
    "first_error",
    [_WEDGE_ERROR, "no IWR6843 CLI found — board on, flashed, single-port fw?"],
    ids=["wedge", "no-cli"],
)
def test_startup_recovers_wedged_ti_by_usb_power_cycle(tmp_path, monkeypatch, caplog, first_error):
    import logging

    with caplog.at_level(logging.INFO, logger="openflight.server"):
        run = _run_ti_startup(tmp_path, monkeypatch, init_results=[first_error, True])

    assert run.outcome == "continued"
    assert run.states["ti"] == "ready"
    assert [cycler.cycles for cycler in run.cyclers] == [1]
    assert len(run.init_calls) == 2
    assert run.init_calls[1] == run.init_calls[0]
    assert run.waits == [_TI_PORT]
    during = run.cyclers[0].status_during_cycle
    assert during["message"] == "TI radar not responding – restarting its USB power"
    assert "[SERVER] TI recovered by USB power cycle at startup" in caplog.text


def test_startup_recovery_without_ti_port_waits_for_any_tty(tmp_path, monkeypatch):
    run = _run_ti_startup(
        tmp_path,
        monkeypatch,
        ti_port=None,
        init_results=["no IWR6843 CLI found — board on, flashed, single-port fw?", True],
    )

    assert run.outcome == "continued"
    assert run.waits == [None]
    assert run.init_calls[1]["port"] is None


def test_startup_power_cycle_failure_keeps_existing_guidance(tmp_path, monkeypatch):
    from openflight.iwr6843.usb_power import UsbPowerCycleError

    run = _run_ti_startup(
        tmp_path,
        monkeypatch,
        init_results=[_WEDGE_ERROR],
        cycler_error=UsbPowerCycleError("USB power cycle failed: sudo exited 1"),
    )

    assert run.outcome == 1
    assert len(run.init_calls) == 1
    assert run.payload["message"] == "TI radar failed to initialize"
    assert run.payload["error"]["recovery"] == _ti_wedge_recovery("no reply to commands")


def test_startup_port_timeout_after_power_cycle_keeps_existing_guidance(tmp_path, monkeypatch):
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[_WEDGE_ERROR], port_returns=False)

    assert run.outcome == 1
    assert len(run.init_calls) == 1
    assert run.payload["error"]["recovery"] == _ti_wedge_recovery("no reply to commands")


def test_startup_second_init_failure_keeps_existing_guidance(tmp_path, monkeypatch):
    stall = (
        "config rejected: 'sensorStop': sensorStop\n"
        "Error: HWA shutdown boundary timed out\nError -1\nl3dump:/>"
    )
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[_WEDGE_ERROR, stall])

    assert run.outcome == 1
    assert len(run.init_calls) == 2
    assert run.cyclers[0].cycles == 1
    assert run.states["ti"] == "error"
    assert run.payload["error"]["recovery"] == _ti_wedge_recovery("HWA chain stalled")


def test_startup_non_wedge_error_does_not_power_cycle(tmp_path, monkeypatch):
    run = _run_ti_startup(
        tmp_path, monkeypatch, init_results=["IWR6843 config not found: missing.cfg"]
    )

    assert run.outcome == 1
    assert run.cyclers[0].cycles == 0
    assert len(run.init_calls) == 1
    assert run.payload["error"]["recovery"] == (
        "Check the TI radar USB and power connections, then relaunch OpenFlight."
    )


def test_startup_without_flag_never_power_cycles_a_wedge(tmp_path, monkeypatch):
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[_WEDGE_ERROR], flag=False)

    assert run.outcome == 1
    assert run.cyclers == []
    assert len(run.init_calls) == 1
    assert run.payload["error"]["recovery"] == _ti_wedge_recovery("no reply to commands")


def test_missing_sudo_rights_disable_every_power_cycle(tmp_path, monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="openflight.server"):
        run = _run_ti_startup(tmp_path, monkeypatch, init_results=[_WEDGE_ERROR], permitted=False)

    assert run.outcome == 1
    assert run.cyclers[0].cycles == 0
    assert run.init_calls[0]["usb_power_cycler"] is None
    assert len(run.init_calls) == 1
    assert (
        "--iwr6843-usb-power-cycle is set but sudo does not allow uhubctl without a password; "
        "see docs/iwr6843/troubleshooting.md"
    ) in caplog.text


def test_auto_detected_ops_keeps_the_cycler_and_a_live_recovery_guard(tmp_path, monkeypatch):
    """The Pi launcher passes no --port: the OPS is judged at recovery time."""
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[_WEDGE_ERROR, True], ops_port=None)

    assert run.outcome == "continued"
    assert run.cyclers[0].cycles == 1
    assert [call["usb_power_cycler"] for call in run.init_calls] == [run.cyclers[0]] * 2
    assert callable(run.init_calls[0]["recovery_guard"])
    assert run.init_calls[1]["recovery_guard"] is run.init_calls[0]["recovery_guard"]


def test_flag_off_passes_no_recovery_guard(tmp_path, monkeypatch):
    run = _run_ti_startup(tmp_path, monkeypatch, init_results=[True], flag=False)

    assert run.init_calls[0]["recovery_guard"] is None


def test_mounted_usb_storage_disables_every_power_cycle(tmp_path, monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="openflight.server"):
        run = _run_ti_startup(
            tmp_path, monkeypatch, init_results=[_WEDGE_ERROR], usb_storage=["/dev/sda1"]
        )

    assert run.outcome == 1
    assert run.cyclers[0].cycles == 0
    assert run.init_calls[0]["usb_power_cycler"] is None
    assert "USB storage is mounted (/dev/sda1)" in caplog.text


@pytest.mark.parametrize(
    ("ops", "mock", "kld7", "expected"),
    [
        ("/dev/ttyAMA0", False, False, None),
        ("/dev/serial0", False, False, None),
        ("/dev/ttyS0", False, False, None),
        ("/dev/ttyACM0", False, False, "OPS radar on /dev/ttyACM0 is not a GPIO UART"),
        ("not-connected", False, False, "OPS radar not connected yet"),
        (None, False, False, "OPS radar not connected yet"),
        ("no-radar", True, False, None),
        ("/dev/ttyAMA0", False, True, "K-LD7 radars are on USB"),
    ],
    ids=["ama0", "serial0", "ttyS0", "acm0", "port-pending", "no-monitor", "mock", "kld7"],
)
def test_iwr6843_recovery_guard_reads_the_live_ops_port(monkeypatch, ops, mock, kld7, expected):
    from types import SimpleNamespace

    from openflight import server

    if ops is None:
        live = None
    elif ops == "not-connected":
        live = SimpleNamespace(radar=SimpleNamespace(port=None))
    elif ops == "no-radar":
        live = SimpleNamespace()
    else:
        live = SimpleNamespace(radar=SimpleNamespace(port=ops))
    monkeypatch.setattr(server, "monitor", live)
    monkeypatch.setattr(server, "mock_mode", mock)

    assert server._make_iwr6843_recovery_guard(kld7=kld7)() == expected


def test_startup_recovery_helper_waits_for_the_given_port(monkeypatch):
    from openflight import server
    from openflight.iwr6843 import usb_power

    waits = []
    monkeypatch.setattr(usb_power, "wait_for_tty", lambda port, **_k: waits.append(port) or True)
    monkeypatch.setattr(server, "init_iwr6843", lambda **_kwargs: True)
    cycler = _FakeStartupCycler()

    assert server._recover_iwr6843_at_startup(cycler, None, {})
    assert cycler.cycles == 1
    assert waits == [None]
