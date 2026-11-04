"""Tests for the optional low-battery shutdown service."""

import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD_SCRIPT = PROJECT_ROOT / "scripts" / "battery" / "battery_guard.sh"
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "battery" / "install_battery_guard.sh"


def _power_supplies(root: Path, *, online="0", capacity="50", voltage_now="3900000") -> Path:
    battery = root / "battery"
    battery.mkdir(parents=True)
    (battery / "type").write_text("Battery\n", encoding="ascii")
    if capacity is not None:
        (battery / "capacity").write_text(f"{capacity}\n", encoding="ascii")
    if voltage_now is not None:
        (battery / "voltage_now").write_text(f"{voltage_now}\n", encoding="ascii")
    charger = root / "charger"
    charger.mkdir()
    (charger / "type").write_text("Mains\n", encoding="ascii")
    if online is not None:
        (charger / "online").write_text(f"{online}\n", encoding="ascii")
    return root


def _run_guard(root: Path, max_polls: int = 5) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "OPENFLIGHT_BATTERY_GUARD_POWER_SUPPLY_ROOT": str(root),
        "OPENFLIGHT_BATTERY_GUARD_INTERVAL_S": "0",
        "OPENFLIGHT_BATTERY_GUARD_DRY_RUN": "1",
        "OPENFLIGHT_BATTERY_GUARD_MAX_POLLS": str(max_polls),
    }
    return subprocess.run(
        ["bash", str(GUARD_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_scripts_have_valid_bash_syntax():
    subprocess.run(["bash", "-n", GUARD_SCRIPT], check=True)
    subprocess.run(["bash", "-n", INSTALL_SCRIPT], check=True)


def test_low_percentage_on_battery_shuts_down_after_three_readings(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, capacity="10"))

    assert result.returncode == 0, result.stderr
    assert "(3/3)" in result.stdout
    assert "(4/3)" not in result.stdout
    assert "Dry run: would stop OpenFlight and power off." in result.stdout


def test_low_voltage_on_battery_shuts_down_even_when_percentage_looks_fine(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, capacity="40", voltage_now="3400000"))

    assert result.returncode == 0, result.stderr
    assert "would stop OpenFlight and power off" in result.stdout


def test_low_battery_on_external_power_does_not_shut_down(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, online="1", capacity="5", voltage_now="3300000"))

    assert result.returncode == 0, result.stderr
    assert "Low battery" not in result.stdout
    assert "would stop OpenFlight" not in result.stdout


def test_healthy_battery_does_not_shut_down(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, capacity="11", voltage_now="3410000"))

    assert result.returncode == 0, result.stderr
    assert "Low battery" not in result.stdout


def test_unknown_external_power_does_not_shut_down(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, online=None, capacity="5"))

    assert result.returncode == 0, result.stderr
    assert "External power state is unreadable" in result.stdout
    assert "Low battery" not in result.stdout


def test_unreadable_battery_does_not_shut_down(tmp_path):
    result = _run_guard(_power_supplies(tmp_path, capacity=None, voltage_now=None))

    assert result.returncode == 0, result.stderr
    assert "Battery level is unreadable" in result.stdout
    assert "Low battery" not in result.stdout


def test_installer_unit_runs_the_repository_guard_with_the_config_file():
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; render_unit', "install-test", str(INSTALL_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"ExecStart=/bin/bash {GUARD_SCRIPT.as_posix()}" in result.stdout
    assert "EnvironmentFile=-/etc/default/openflight-battery-guard" in result.stdout
    assert "Restart=always" in result.stdout


# --- Fork: Mains-only external power, as LinuxPowerReader reads it ---------


def test_only_mains_supplies_count_as_external_power(tmp_path):
    root = _power_supplies(tmp_path, capacity="5")
    usb = root / "usb"
    usb.mkdir()
    (usb / "type").write_text("USB\n", encoding="ascii")
    (usb / "online").write_text("1\n", encoding="ascii")

    result = _run_guard(root)

    assert result.returncode == 0, result.stderr
    assert "would stop OpenFlight and power off" in result.stdout


def test_non_mains_supply_alone_leaves_external_power_unreadable(tmp_path):
    root = _power_supplies(tmp_path, online=None, capacity="5")
    usb = root / "usb"
    usb.mkdir()
    (usb / "type").write_text("USB\n", encoding="ascii")
    (usb / "online").write_text("0\n", encoding="ascii")

    result = _run_guard(root)

    assert "External power state is unreadable" in result.stdout
    assert "Low battery" not in result.stdout


def test_uses_the_same_power_supply_nodes_as_the_linux_reader(tmp_path):
    from openflight.power.providers.linux import (  # pylint: disable=import-outside-toplevel
        LinuxPowerReader,
    )

    root = _power_supplies(tmp_path, capacity="10", voltage_now="3600000")
    sample = LinuxPowerReader(power_supply_path=root).read()

    assert sample.external_power is False
    assert sample.battery_percent == 10.0
    result = _run_guard(root)
    assert "would stop OpenFlight and power off" in result.stdout


def test_ui_critical_threshold_matches_the_guard_default():
    # pylint: disable=import-outside-toplevel
    from openflight.power.models import PowerSample, PowerState
    from openflight.power.service import PowerMonitor

    def state(percent):
        return PowerMonitor._state_for(  # pylint: disable=protected-access
            PowerSample(battery_percent=percent, battery_voltage_v=3.7, external_power=False)
        )

    assert state(10.0) is PowerState.CRITICAL
    assert state(10.1) is not PowerState.CRITICAL
    config = subprocess.run(
        ["bash", "-c", 'source "$1"; render_config', "install-test", str(INSTALL_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "OPENFLIGHT_BATTERY_GUARD_MIN_PERCENT=10\n" in config


# --- Fork: clean stop via /api/shutdown, then poweroff ---------------------


def _fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "systemctl.log"
    script = tmp_path / "fake-systemctl"
    script.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> "{log}"\n', encoding="ascii")
    script.chmod(0o755)
    return script, log


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_shutdown(tmp_path: Path, port: int, stop_timeout_s: int = 10):
    systemctl, log = _fake_systemctl(tmp_path)
    root = _power_supplies(tmp_path / "sys", capacity="5")
    env = {
        "PATH": "/usr/bin:/bin",
        "OPENFLIGHT_BATTERY_GUARD_POWER_SUPPLY_ROOT": str(root),
        "OPENFLIGHT_BATTERY_GUARD_INTERVAL_S": "0",
        "OPENFLIGHT_BATTERY_GUARD_MAX_POLLS": "5",
        "OPENFLIGHT_BATTERY_GUARD_WEB_PORT": str(port),
        "OPENFLIGHT_BATTERY_GUARD_STOP_TIMEOUT_S": str(stop_timeout_s),
        "OPENFLIGHT_BATTERY_GUARD_SYSTEMCTL": str(systemctl),
        "OPENFLIGHT_BATTERY_GUARD_SERVER_PATTERN": "no-such-process-battery-guard-test",
    }
    result = subprocess.run(
        ["bash", str(GUARD_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    calls = log.read_text(encoding="ascii").splitlines() if log.exists() else []
    return result, calls


class _ShutdownHandler(BaseHTTPRequestHandler):
    """Mimics /api/shutdown: answer, then stop listening like the exiting server."""

    requests: list[str] = []

    def do_POST(self):  # pylint: disable=invalid-name
        """Record the request, reply, and close the listening socket."""
        type(self).requests.append(self.path)
        body = b'{"status": "shutting_down"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        server = self.server

        def stop():
            server.shutdown()
            server.server_close()

        threading.Thread(target=stop, daemon=True).start()

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        """Keep test output quiet."""


def test_shutdown_uses_the_api_waits_for_the_port_and_powers_off(tmp_path):
    _ShutdownHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _ShutdownHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    result, calls = _run_shutdown(tmp_path, port)

    assert result.returncode == 0, result.stderr
    assert _ShutdownHandler.requests == ["/api/shutdown"]
    assert "Requested OpenFlight shutdown via /api/shutdown." in result.stdout
    assert "OpenFlight stopped after" in result.stdout
    assert calls == ["stop --no-block openflight", "poweroff"]


def test_shutdown_still_powers_off_when_the_api_is_down(tmp_path):
    result, calls = _run_shutdown(tmp_path, _free_port())

    assert result.returncode == 0, result.stderr
    assert "OpenFlight API did not answer" in result.stdout
    assert calls == ["stop --no-block openflight", "poweroff"]


def test_shutdown_powers_off_after_the_timeout_when_the_port_stays_busy(tmp_path):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(16)
        port = busy.getsockname()[1]

        result, calls = _run_shutdown(tmp_path, port, stop_timeout_s=2)

    assert result.returncode == 0, result.stderr
    assert f"Port {port} still in use after 2s; powering off anyway." in result.stdout
    assert calls[-1] == "poweroff"
