"""Re-arm must not hang when the radar streams bytes continuously.

Reproduces the field failure where the OPS243 never presents a 200 ms
quiet gap after a capture (continuous dumps or streaming-mode fallback):
rearm_rolling_buffer's drain loop had no timeout, so the monitor thread
hung silently and the radar was never re-armed (blue dump light stays on).
"""

import threading
import time

import serial

from openflight.ops243 import OPS243Radar


class ChattySerial:
    """Fake serial port that always has bytes waiting, forever."""

    def __init__(self):
        self.is_open = True
        self.writes = []

    @property
    def in_waiting(self):
        return 512

    def read(self, n):
        return b'{"speed": 12.3}\r\n' * 28

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


def test_rearm_bails_out_when_bytes_never_stop():
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = ChattySerial()

    done = threading.Event()

    def rearm():
        radar.rearm_rolling_buffer(16)
        done.set()

    worker = threading.Thread(target=rearm, daemon=True)
    start = time.time()
    worker.start()
    # Drain budget is 5s; allow generous scheduling slack.
    finished = done.wait(timeout=10.0)
    elapsed = time.time() - start

    assert finished, (
        f"rearm_rolling_buffer still hung after {elapsed:.1f}s with a "
        "continuously-chatty serial port — drain loop has no timeout"
    )
    # Re-arm commands must still have been attempted (best-effort re-arm).
    assert any(b"PA" in w for w in radar.serial.writes)


class QuietSerial(ChattySerial):
    """Port with nothing pending, so re-arm goes straight to its commands."""

    @property
    def in_waiting(self):
        return 0


class TimeoutOnWriteSerial(QuietSerial):
    """Radar that stopped servicing commands: every write times out."""

    def write(self, data):
        raise serial.SerialTimeoutException("Write timeout")


def test_rearm_reports_whether_the_radar_was_armed():
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = TimeoutOnWriteSerial()
    assert radar.rearm_rolling_buffer(16) is False

    radar.serial = QuietSerial()
    assert radar.rearm_rolling_buffer(16) is True
