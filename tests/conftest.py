"""Reusable mock simulator TCP server for connector transport tests.

Protocol-agnostic: records bytes received and can send scripted JSON replies or
drop the connection. Used by both GSPro and OpenGolfSim connector tests.
"""

import json
import socket
import sys
import threading
from pathlib import Path
from typing import List, Optional

import pytest

# Add tests directory to sys.path for importing test utilities like spin_synth
sys.path.insert(0, str(Path(__file__).parent))


class MockSimServer:
    """Tiny TCP server that records bytes and can send scripted replies."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.host, self.port = self._sock.getsockname()
        self._client_sock: Optional[socket.socket] = None
        self.received: List[bytes] = []
        self.scripted_replies: List[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._sock.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    self._client_sock, _ = self._sock.accept()
                except socket.timeout:
                    continue
                self._client_sock.settimeout(0.2)
                # Send any scripted replies that were queued before connect
                for reply in list(self.scripted_replies):
                    try:
                        self._client_sock.sendall(reply)
                    except OSError:
                        break
                self.scripted_replies.clear()
                while not self._stop.is_set():
                    try:
                        chunk = self._client_sock.recv(4096)
                    except socket.timeout:
                        # Send any newly-queued replies
                        for reply in list(self.scripted_replies):
                            try:
                                self._client_sock.sendall(reply)
                            except OSError:
                                break
                        self.scripted_replies.clear()
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.received.append(chunk)
                conn = self._client_sock
                self._client_sock = None
                if conn is not None:
                    try:
                        conn.close()
                    except OSError:
                        pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass

    def queue_reply(self, obj: dict) -> None:
        self.scripted_replies.append(json.dumps(obj).encode("utf-8"))

    def queue_raw(self, data: bytes) -> None:
        self.scripted_replies.append(data)

    def disconnect_client(self) -> None:
        # Capture locally; the accept loop may null _client_sock concurrently.
        conn = self._client_sock
        self._client_sock = None
        if conn is not None:
            try:
                conn.shutdown(socket.SHUT_RDWR)
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self.disconnect_client()
        self._thread.join(timeout=2.0)


@pytest.fixture
def mock_sim():
    server = MockSimServer()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def _isolate_profile_store(tmp_path_factory, monkeypatch):
    """Keep ProfileStore() off ~/.config/openflight/profiles.json during tests."""
    roster = tmp_path_factory.mktemp("profiles") / "profiles.json"
    monkeypatch.setenv("OPENFLIGHT_PROFILES_PATH", str(roster))


@pytest.fixture(autouse=True)
def _isolate_pairing_secret(tmp_path_factory, monkeypatch):
    """Keep DeviceSecret() off ~/.config/openflight/pairing-secret during tests."""
    secret = tmp_path_factory.mktemp("pairing") / "pairing-secret"
    monkeypatch.setenv("OPENFLIGHT_PAIRING_SECRET_PATH", str(secret))


@pytest.fixture(autouse=True)
def _isolate_camera_angle_reference(tmp_path_factory, monkeypatch):
    """Keep the tapped target off ~/.config/openflight; each test starts without offsets."""
    target = tmp_path_factory.mktemp("target-line") / "camera-target-line.json"
    monkeypatch.setenv("OPENFLIGHT_CAMERA_TARGET_LINE_PATH", str(target))
    server = sys.modules.get("openflight.server")
    if server is not None:
        from openflight.angle_reference import AngleReference

        monkeypatch.setattr(server, "angle_reference", AngleReference())


@pytest.fixture(autouse=True)
def _isolate_weather_store(tmp_path_factory, monkeypatch):
    """Keep WeatherStore() off ~/.config/openflight/weather.json during tests."""
    path = tmp_path_factory.mktemp("weather") / "weather.json"
    monkeypatch.setenv("OPENFLIGHT_WEATHER_PATH", str(path))
    from openflight import server  # pylint: disable=import-outside-toplevel

    monkeypatch.setattr(server, "weather_store", None)


@pytest.fixture(autouse=True)
def _isolate_phone_tilt_calibration(tmp_path_factory, monkeypatch):
    """Keep the saved TI phone tilt off ~/.config/openflight during tests."""
    path = tmp_path_factory.mktemp("phone-tilt") / "iwr6843_phone_orientation.json"
    monkeypatch.setenv("OPENFLIGHT_IWR6843_PHONE_TILT_PATH", str(path))
    server = sys.modules.get("openflight.server")
    if server is not None:
        monkeypatch.setattr(server, "PHONE_TILT_CALIBRATION_PATH", None)
        monkeypatch.setattr(server, "_last_effective_iwr_tilt", None)


@pytest.fixture
def isolate_finalization(monkeypatch):
    from openflight import server

    for name in ("monitor", "kld7_vertical", "kld7_horizontal", "camera_capture_runtime"):
        monkeypatch.setattr(server, name, None)
    for name in ("ball_speed_correction_enabled", "calculated_spin_enabled", "debug_mode"):
        monkeypatch.setattr(server, name, False)
    monkeypatch.setattr(server, "sim_connectors", [])
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    monkeypatch.setattr(server.socketio, "emit", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def _hold_carry_span_jobs(monkeypatch):
    """Keep the background carry span out of tests that do not ask for it.

    The span runs on its own worker after the shot event and emits a late shot_update,
    which would race the event lists other tests assert. Collected jobs are run by the
    tests in test_carry_span.py.
    """
    server = sys.modules.get("openflight.server")
    jobs = []
    if server is not None:
        monkeypatch.setattr(server, "_submit_carry_span", jobs.append)
    return jobs


@pytest.fixture(autouse=True)
def _hold_carry_variant_jobs(monkeypatch):
    """Keep the background carry variants out of tests that do not ask for it.

    They run on the carry-span worker after the shot event and write a late
    carry_variants session-log entry, which would race the entries other tests read.
    Collected jobs are run by the tests in test_carry_variants.py.
    """
    server = sys.modules.get("openflight.server")
    jobs = []
    if server is not None:
        monkeypatch.setattr(server, "_submit_carry_variants", jobs.append)
    return jobs
