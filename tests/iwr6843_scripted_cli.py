"""Serial double for the IWR6843 CLI with scripted replies on a fake clock.

Each written line looks up its next scripted reply; a reply is ``bytes``
(delivered at once), a list of ``(delay_s, bytes)`` segments, or a callable
taking the fake serial and returning either. The last scripted reply of a
command repeats. Reads advance the fake clock like a blocking read would, so
driver timeouts of many seconds run instantly.
"""

from __future__ import annotations

import math

from openflight.iwr6843.driver import IWR6843Radar

PROMPT = b"\r\nl3dump:/>"


class FakeClock:
    """Monotonic clock the tests advance explicitly or through reads."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        """Advance instead of sleeping."""
        self.now += seconds


def done(command: str, body: str = "") -> bytes:
    """Echo, optional body, Done and the prompt."""
    text = f"{command}\r\n" + (f"{body}\r\n" if body else "") + "Done"
    return text.encode() + PROMPT


def stats_line(*, frames: int, hwa_out: int, active: int = 1) -> bytes:
    """A real-format HWA-chained ``stats`` reply (first line of the iq8 build)."""
    return done(
        "stats",
        f"frames={frames} wraps=0 active={active} calib=0x1ffe rf_faults=0 "
        f"hwa_frames={frames} hwa_out={hwa_out} hwa_rearms={frames} hwa_rearm_err=0 "
        "hwa_missed=0 freeze_req=0 freeze_done=0 freeze_to=0 "
        "format=iq8 plan=48pre/16post loops=12 used=786432/786432",
    )


def running_stats(serial: "ScriptedCli") -> bytes:
    """Stats of healthy capture: ~333 frames/s with one HWA output each."""
    frames = int(serial.clock.now * 333)
    return stats_line(frames=frames, hwa_out=frames)


class ScriptedCli:
    """pyserial stand-in implementing in_waiting/read/write/reset/close."""

    def __init__(self, clock: FakeClock, script: dict | None = None, read_timeout: float = 0.3):
        self.clock = clock
        self.script = {key: list(value) for key, value in (script or {}).items()}
        self.read_timeout = read_timeout
        self.pending: list[tuple[float, bytes]] = []
        self.writes: list[bytes] = []
        self.close_delay_s = 0.0
        self.closed = False

    def sent(self, line: str) -> int:
        """How often ``line`` was written."""
        return self.writes.count((line + "\n").encode())

    def queue(self, data: bytes, delay_s: float = 0.0) -> None:
        """Make bytes arrive ``delay_s`` from now."""
        self.pending.append((self.clock.now + delay_s, data))
        self.pending.sort(key=lambda item: item[0])

    def _reply_for(self, line: str):
        replies = self.script.get(line)
        if replies:
            return replies.pop(0) if len(replies) > 1 else replies[0]
        if line == "":
            return PROMPT
        if line == "stats":
            return running_stats
        return done(line)

    @property
    def in_waiting(self) -> int:
        return sum(len(chunk) for due, chunk in self.pending if due <= self.clock.now)

    def read(self, size: int = 1) -> bytes:
        if not self.in_waiting:
            upcoming = min((due for due, _ in self.pending), default=math.inf)
            self.clock.now = min(upcoming, self.clock.now + self.read_timeout)
        out = bytearray()
        while self.pending and self.pending[0][0] <= self.clock.now and len(out) < size:
            due, chunk = self.pending[0]
            take = chunk[: size - len(out)]
            out.extend(take)
            if len(take) == len(chunk):
                self.pending.pop(0)
            else:
                self.pending[0] = (due, chunk[len(take) :])
        return bytes(out)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if data == b"\x18":
            return 1
        reply = self._reply_for(data.decode().strip())
        if callable(reply):
            reply = reply(self)
        if isinstance(reply, bytes):
            reply = [(0.0, reply)]
        for delay_s, chunk in reply:
            self.queue(chunk, delay_s)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.pending = [(due, chunk) for due, chunk in self.pending if due > self.clock.now]

    def close(self) -> None:
        self.clock.now += self.close_delay_s
        self.closed = True


def scripted_radar(script: dict | None = None) -> tuple[IWR6843Radar, ScriptedCli, FakeClock]:
    """An IWR6843Radar wired to a ScriptedCli and a fake clock."""
    clock = FakeClock(100.0)
    serial = ScriptedCli(clock, script)
    radar = IWR6843Radar.__new__(IWR6843Radar)
    radar.port = "/dev/fake-iwr6843"
    radar.ser = serial
    radar._clock = clock  # pylint: disable=protected-access
    radar._sleep = clock.sleep  # pylint: disable=protected-access
    return radar, serial, clock
