"""Test doubles for the OPS243-A firmware 1.3.3 speed trigger (B2).

``SimulatedOps133`` is a serial double on ``_UartRxBuffer`` (a real receive
queue: a flush drops unread bytes) that parses the command bytes the driver
writes, answers ``?V`` and ``??``, tracks ``PI``/``PA`` and trips its own
rolling buffer on ``fire(club_mph)``: pin 1 rises, the dump streams, pin 1
falls, and the board idles until the next ``PA``.

``FakePinButton`` is the gpiozero ``Button`` double for J3 pin 1 (both edges,
``active_state``, ``is_active``, kernel ticks in ``_last_changed``);
``FakePinFactory`` builds them and records how.
"""

from __future__ import annotations

import time

import numpy as np

from openflight.ops243 import OPS243Radar
from tests.test_iwr6843_monitor import FakeButton
from tests.test_ops243 import _UartRxBuffer
from tests.test_trigger_gate import CLUB_FIXTURE

# Commands that end at a CR rather than after two characters.
_CR_PREFIXES = ("ST", "SM", "SP", "S=", "S#", "R>", "R<", "M>", "M<", "O=", "X=")


class FakePinButton(FakeButton):
    """gpiozero ``Button`` double for pin 1: both edges, level and kernel ticks."""

    def __init__(  # pylint: disable=too-many-arguments
        self, pin, pull_up=False, active_state=None, bounce_time=None, *, idle_active=False
    ):
        super().__init__(pin, pull_up, bounce_time)
        self.active_state = active_state
        self.is_active = idle_active
        self.when_released = None
        self._last_changed = None
        self.presses = 0
        self.releases = 0

    def press(self, ticks: float | None = None) -> None:
        """Pin goes active; gpiozero stores the edge ticks before the callback."""
        self.is_active = True
        self.presses += 1
        self._last_changed = time.monotonic() if ticks is None else ticks
        if self.when_pressed is not None:
            self.when_pressed()

    def release(self, ticks: float | None = None) -> None:
        """Pin goes inactive."""
        self.is_active = False
        self.releases += 1
        self._last_changed = time.monotonic() if ticks is None else ticks
        if self.when_released is not None:
            self.when_released()


class FakePinFactory:
    """``button_factory`` double: records each construction's keyword arguments."""

    def __init__(self, *, idle_active: bool = False):
        self.idle_active = idle_active
        self.calls: list[tuple[int, dict]] = []
        self.buttons: list[FakePinButton] = []

    def __call__(self, pin, **kwargs) -> FakePinButton:
        self.calls.append((pin, dict(kwargs)))
        button = FakePinButton(
            pin,
            pull_up=kwargs.get("pull_up", False),
            active_state=kwargs.get("active_state"),
            bounce_time=kwargs.get("bounce_time"),
            idle_active=self.idle_active,
        )
        self.buttons.append(button)
        return button

    @property
    def button(self) -> FakePinButton:
        """The most recently built button."""
        return self.buttons[-1]


def dump_bytes(
    i_samples, q_samples, *, sample_time: float = 946.077, trigger_time: float = 946.145
) -> bytes:
    """One rolling-buffer dump in the radar's JSON-lines format."""
    return (
        f'{{"sample_time":"{sample_time:.3f}"}}\r\n'
        f'{{"trigger_time":"{trigger_time:.3f}"}}\r\n'
        f'{{"I":[{",".join(str(int(v)) for v in i_samples)}]}}\r\n'
        f'{{"Q":[{",".join(str(int(v)) for v in q_samples)}]}}\r\n'
    ).encode("ascii")


def fixture_dump(index: int = 0) -> bytes:
    """A real club-speed capture from the test fixtures, as dump bytes."""
    with np.load(CLUB_FIXTURE) as data:
        return dump_bytes(data["i_samples"][index], data["q_samples"][index])


SHORT_DUMP = dump_bytes([2168, 2187, 2155, 2154], [2048, 2050, 2047, 2049])


class SimulatedOps133(_UartRxBuffer):  # pylint: disable=too-many-instance-attributes
    """OPS243-A on firmware 1.3.3 (or ``version``) behind the J3 UART.

    Options: ``trip_message`` is sent when the trigger trips, before the dump;
    ``settings_survive_dump=False`` clears ST/SM after each dump (what M1
    checks with ``??``); ``pin_on_host_int`` makes HOST_INT dumps toggle pin 1
    too; ``release_before_dump`` drops pin 1 before the dump streams (the
    README's "until the sampling has completed") instead of after it.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        version: str = "1.3.3",
        pin: FakePinButton | None = None,
        dump: bytes | None = None,
        trip_message: bytes = b"",
        settings_survive_dump: bool = False,
        pin_on_host_int: bool = True,
        release_before_dump: bool = False,
    ):
        super().__init__(on_write=SimulatedOps133._on_write)
        self.version = version
        self.pin = pin
        self.dump = fixture_dump() if dump is None else dump
        self.trip_message = trip_message
        self.settings_survive_dump = settings_survive_dump
        self.pin_on_host_int = pin_on_host_int
        self.release_before_dump = release_before_dump
        self.timeout = 1.0
        self.armed = False
        self.settings: dict[str, str] = {}
        self.commands: list[str] = []
        self.trips = 0
        self.host_int_dumps = 0
        self._pending = b""

    # --- serial surface beyond _UartRxBuffer ---------------------------------

    def open_as_serial(self, *_args, **_kwargs) -> SimulatedOps133:
        """Stand-in for ``serial.Serial(...)``: the same board on every open."""
        self.is_open = True
        return self

    def close(self) -> None:
        self.is_open = False

    @property
    def out_waiting(self) -> int:
        return 0

    # --- command parsing ------------------------------------------------------

    def _on_write(self, data: bytes) -> None:
        self._pending += bytes(data)
        while self._pending:
            text = self._pending.decode("ascii", errors="replace")
            if text.startswith(("\r", "\n")):
                self._pending = self._pending[1:]
                continue
            if text.startswith(_CR_PREFIXES):
                end = self._pending.find(b"\r")
                if end < 0:
                    return
                command, self._pending = self._pending[:end], self._pending[end + 1 :]
            elif len(self._pending) >= 2:
                command, self._pending = self._pending[:2], self._pending[2:]
            else:
                return
            self._handle(command.decode("ascii", errors="replace"))

    def _handle(self, command: str) -> None:
        self.commands.append(command)
        if command == "?V":
            self.feed(f'{{"Version":"{self.version}"}}\r\n'.encode())
        elif command == "??":
            self.feed(self._settings_reply())
        elif command == "PI":
            self.armed = False
        elif command == "PA":
            self.armed = True
        elif command == "US":
            self.settings["units"] = "mph"
        elif len(command) == 2 and command[0] == "P" and command[1].isdigit():
            self.settings["power"] = command[1]
        elif command.startswith(("ST", "SM", "S#", "S=")):
            self.settings[command[:2]] = command[2:]

    def _settings_reply(self) -> bytes:
        lines = [
            '{"Product":"OPS243"}',
            f'{{"Version":"{self.version}"}}',
            f'{{"SamplingRate":"{self.settings.get("S=", "")}"}}',
            f'{{"TriggerSplit":"{self.settings.get("S#", "")}"}}',
            f'{{"SpeedTrigger":"{self.settings.get("ST", "")}"}}',
            f'{{"SpeedTriggerMagnitude":"{self.settings.get("SM", "")}"}}',
        ]
        return ("\r\n".join(lines) + "\r\n").encode()

    # --- the board's own trigger ---------------------------------------------

    @property
    def threshold_mph(self) -> float | None:
        """|ST| in mph, None while no speed trigger is set."""
        value = self.settings.get("ST")
        if not value or self.settings.get("units") != "mph":
            return None
        return abs(float(value))

    def fire(self, club_mph: float) -> bool:
        """A swing at ``club_mph``: True when the speed trigger tripped a dump."""
        threshold = self.threshold_mph
        if not self.armed or threshold is None or "SM" not in self.settings:
            return False
        if club_mph < threshold:
            return False
        self.trips += 1
        self._dump(with_pin=True, trip_message=self.trip_message)
        if not self.settings_survive_dump:
            self.settings.pop("ST", None)
            self.settings.pop("SM", None)
        return True

    def host_int(self) -> bool:
        """A HOST_INT pulse: True when it started a dump."""
        if not self.armed:
            return False
        self.host_int_dumps += 1
        self._dump(with_pin=self.pin_on_host_int, trip_message=b"")
        return True

    def _dump(self, *, with_pin: bool, trip_message: bytes) -> None:
        self.armed = False
        pin = self.pin if with_pin else None
        if pin is not None:
            pin.press()
        if trip_message:
            self.feed(trip_message)
        if pin is not None and self.release_before_dump:
            pin.release()
        self.feed(self.dump)
        if pin is not None and not self.release_before_dump:
            pin.release()


def fast_replies(monkeypatch) -> None:
    """Make ``_send_command`` read the simulator's synchronous reply at once."""

    def read_reply(radar, *_args, **_kwargs):
        waiting = radar.serial.in_waiting
        return radar.serial.read(waiting).decode("ascii", errors="ignore") if waiting else ""

    monkeypatch.setattr(OPS243Radar, "_read_reply", read_reply)
    monkeypatch.setattr("openflight.ops243.time.sleep", lambda _s: None)


def sim_radar(sim: SimulatedOps133, *, firmware_version: str | None = None) -> OPS243Radar:
    """A radar on ``sim`` without ``connect`` (``firmware_version`` as given)."""
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = sim
    radar.port = "/dev/ttyAMA0"
    radar.baud = 230400
    radar._unit = "mph"  # pylint: disable=protected-access
    radar._json_mode = False  # pylint: disable=protected-access
    radar._magnitude_enabled = False  # pylint: disable=protected-access
    radar._speed_read_buffer = ""  # pylint: disable=protected-access
    radar.last_hardware_trigger_first_byte_timestamp = None
    radar.firmware_version = firmware_version
    return radar
