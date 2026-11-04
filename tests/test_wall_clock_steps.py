"""Timeouts and deadlines must survive a wall-clock step.

The Pi has no RTC: NTP (or the phone hotspot) steps ``time.time()`` by hours
shortly after boot. A deadline measured on the wall clock then fires at once
(forward step) or hangs for hours (backward step). Each test here steps the
wall clock by a day in the middle of a wait and checks the wait still runs
its real course.
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import Mock

import numpy as np

from openflight import server as server_module
from openflight.iwr6843.driver import IWR6843Radar
from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.ops243 import OPS243Radar
from openflight.rolling_buffer import RollingBufferMonitor
from openflight.rolling_buffer.trigger import SpeedTriggeredCapture
from openflight.sim.transport import TcpSimClient
from openflight.sim.types import ConnectionState

DAY_S = 86400.0
_REAL_TIME = time.time

_DUMP = (
    b'{"sample_time":946.077}\r\n{"trigger_time":946.145}\r\n'
    b'{"I":[2168,2187,2155,2154]}\r\n'
    b'{"Q":[2048,2050,2047,2049]}'
)


class StepClock:
    """``time.time`` that jumps by ``offset_s`` after ``real_calls`` reads.

    Only reads from the thread that built the clock are counted, so worker
    threads cannot move the step; they see the stepped clock once it stepped.
    """

    def __init__(self, offset_s: float = DAY_S, real_calls: int = 1):
        self.offset_s = offset_s
        self.real_calls = real_calls
        self.calls = 0
        self._owner = threading.get_ident()

    def __call__(self) -> float:
        if threading.get_ident() == self._owner:
            self.calls += 1
            if self.calls <= self.real_calls:
                return _REAL_TIME()
        elif self.calls <= self.real_calls:
            return _REAL_TIME()
        return _REAL_TIME() + self.offset_s


def _step_wall_clock(monkeypatch, **kwargs) -> StepClock:
    clock = StepClock(**kwargs)
    monkeypatch.setattr(time, "time", clock)
    return clock


# --- IWR6843 driver: detect_port -------------------------------------------


class _SlowCliSerial:
    """CLI that answers ``help`` only on the third read."""

    def __init__(self):
        self.reads = 0

    def reset_input_buffer(self):
        pass

    def write(self, _data):
        pass

    def read(self, _n):
        self.reads += 1
        return b"sensorStart\r\n" if self.reads >= 3 else b""

    def close(self):
        pass


def test_detect_port_survives_a_forward_wall_clock_step(monkeypatch):
    monkeypatch.setattr("openflight.iwr6843.driver._PORT_GLOBS", ("/dev/fake*",))
    monkeypatch.setattr("openflight.iwr6843.driver.glob.glob", lambda _p: ["/dev/fake"])
    monkeypatch.setattr("openflight.iwr6843.driver.open_port", lambda *_a: _SlowCliSerial())
    _step_wall_clock(monkeypatch)

    assert IWR6843Radar.detect_port() == "/dev/fake"


# --- OPS243: trigger_capture / wait_for_hardware_trigger / re-arm drain -----


class _PolledSerial:
    """Serial whose payload appears after ``idle_polls`` empty polls."""

    def __init__(self, payload: bytes, idle_polls: int = 3):
        self.is_open = True
        self.out_waiting = 0
        self._payload = payload
        self._idle_polls = idle_polls
        self._sent = False
        self.writes = []

    @property
    def in_waiting(self):
        if self._sent:
            return 0
        if self._idle_polls > 0:
            self._idle_polls -= 1
            return 0
        return len(self._payload)

    def read(self, _n):
        self._sent = True
        return self._payload

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


def _ops(serial_obj) -> OPS243Radar:
    radar = OPS243Radar()
    radar.serial = serial_obj
    return radar


def test_trigger_capture_survives_a_forward_wall_clock_step(monkeypatch):
    radar = _ops(_PolledSerial(_DUMP))
    _step_wall_clock(monkeypatch)

    assert radar.trigger_capture(timeout=2.0) == _DUMP.decode("ascii")


def test_hardware_trigger_wait_survives_a_forward_wall_clock_step(monkeypatch):
    radar = _ops(_PolledSerial(_DUMP))
    _step_wall_clock(monkeypatch)

    response = radar.wait_for_hardware_trigger(timeout=2.0, dump_grace=2.0)

    assert response == _DUMP.decode("ascii")
    # The first-byte stamp stays wall-clock (it is the loop's only wall
    # read): consumers compare it with gate edges and re-arm stamps.
    stamp = radar.last_hardware_trigger_first_byte_timestamp
    assert abs(stamp - _REAL_TIME()) < 5.0


class _StragglingDumpSerial:
    """Port still streaming the tail of a dump for a few reads, then quiet."""

    def __init__(self, chunks: int = 4):
        self.is_open = True
        self.out_waiting = 0
        self.writes = []
        self._chunks = chunks

    @property
    def in_waiting(self):
        return 64 if self._chunks > 0 else 0

    def read(self, _n):
        self._chunks -= 1
        return b"1" * 64

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


def test_rearm_drain_survives_a_forward_wall_clock_step(monkeypatch, caplog):
    monkeypatch.setattr("openflight.ops243.time.sleep", lambda _s: None)
    radar = _ops(_StragglingDumpSerial())
    _step_wall_clock(monkeypatch)

    with caplog.at_level(logging.INFO, logger="ops243"):
        assert radar.rearm_rolling_buffer(8) is True

    assert not any("drain timed out" in r.getMessage() for r in caplog.records)
    assert any("Re-arm drain: 256 bytes" in r.getMessage() for r in caplog.records)


# --- rolling buffer: speed trigger and wait_for_shot -----------------------


def test_speed_trigger_wait_survives_a_forward_wall_clock_step(monkeypatch):
    radar = Mock()
    radar.read_speed_nonblocking.return_value = None
    trigger = SpeedTriggeredCapture()
    trigger._needs_reconfigure = False  # pylint: disable=protected-access
    _step_wall_clock(monkeypatch)

    assert trigger.wait_for_trigger(radar, Mock(), timeout=0.1) is None

    # A wall-clock deadline ended after one poll; the real wait polls ~50x.
    assert radar.read_speed_nonblocking.call_count > 5


def test_wait_for_shot_survives_a_forward_wall_clock_step(monkeypatch):
    monitor = RollingBufferMonitor(port=None)
    shot = object()
    sleeps = []

    def fake_sleep(_s):
        sleeps.append(_s)
        if len(sleeps) == 3:
            monitor._shot_callback(shot)  # pylint: disable=protected-access

    monkeypatch.setattr("openflight.rolling_buffer.monitor.time.sleep", fake_sleep)
    _step_wall_clock(monkeypatch)

    assert monitor.wait_for_shot(timeout=5.0) is shot


# --- server: K-LD7 post-shot wait ------------------------------------------


def test_kld7_post_shot_wait_is_capped_after_a_backward_wall_clock_step(monkeypatch):
    sleeps = []
    monkeypatch.setattr(server_module.time, "time", lambda: 1000.0 - DAY_S)
    monkeypatch.setattr(server_module.time, "sleep", sleeps.append)

    server_module._maybe_wait_for_kld7_post_shot_frames(1000.0)

    assert sleeps == [server_module._KLD7_POST_SHOT_CAPTURE_DELAY_S]


# --- sim transport heartbeat -----------------------------------------------


class _HeartbeatCodec:
    def heartbeat_bytes(self):
        return b"HB"


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)


def test_heartbeat_survives_a_backward_wall_clock_step(monkeypatch):
    client = TcpSimClient("127.0.0.1", 1, _HeartbeatCodec(), heartbeat_interval_s=0.05)
    sock = _RecordingSocket()
    client._sock = sock  # pylint: disable=protected-access
    client._state = ConnectionState.CONNECTED  # pylint: disable=protected-access
    client.send_raw(b"shot")
    # Every later wall read is a day earlier than the send.
    monkeypatch.setattr(time, "time", lambda: _REAL_TIME() - DAY_S)

    loop = threading.Thread(target=client._heartbeat_loop, daemon=True)  # pylint: disable=protected-access
    loop.start()
    try:
        deadline = time.monotonic() + 2.0
        while b"HB" not in sock.sent and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        client._stop_event.set()  # pylint: disable=protected-access
        loop.join(timeout=1.0)

    assert b"HB" in sock.sent


# --- IWR6843 capture monitor: match window ---------------------------------


class _FakeTiRadar:
    port = "/dev/fake-iwr6843"

    def send_config(self, _path):
        pass

    def read_dump(self):
        return pack_dump(np.zeros((2, 4, 4, 8), dtype=complex), n_tx=2, frame_period_us=6000)

    def close(self):
        pass

    def stop_sensor(self, *, retry=True):  # pylint: disable=unused-argument
        pass


class _FakeButton:
    def __init__(self, pin, pull_up, bounce_time):
        self.pin, self.pull_up, self.bounce_time = pin, pull_up, bounce_time
        self.when_pressed = None

    def close(self):
        pass


def test_match_window_survives_a_forward_wall_clock_step(monkeypatch, tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=_FakeTiRadar(),
        button_factory=_FakeButton,
        match_tolerance_s=0.5,
    )
    monitor.start()
    edge = _REAL_TIME()

    def late_edge():
        time.sleep(0.05)
        with monitor._condition:  # pylint: disable=protected-access
            monitor._condition.notify_all()  # spurious wake-up first  # pylint: disable=protected-access
        time.sleep(0.05)
        monitor.notify_trigger(edge)

    worker = threading.Thread(target=late_edge, daemon=True)
    try:
        _step_wall_clock(monkeypatch)
        worker.start()
        capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    finally:
        worker.join(timeout=1.0)
        monitor.stop()

    assert capture is not None and capture.valid
    assert capture.trigger_timestamp == edge
