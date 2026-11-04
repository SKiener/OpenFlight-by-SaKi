"""OPS243-A firmware 1.3.3 speed trigger (B2): driver commands, firmware gate, dump markers.

The speed trigger is off by default. These tests pin both sides: what the new
driver calls write to the radar (``PI``, ``US``, power, ``S=``, ``SM``, ``ST``,
``S#``, ``??``; never ``GC``/``GS``/``A!``), and that the default paths
(connect, ``_send_command`` of every existing command, the re-arm, the dump
wait) write and return exactly what they did before.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest

from openflight import ops243
from openflight.ops243 import (
    HARDWARE_TRIGGER_MARKERS,
    HARDWARE_TRIGGER_PREAMBLE_BYTES,
    SPEED_TRIGGER_MARKERS,
    OPS243Radar,
    OPSFirmwareError,
    parse_firmware_version,
    speed_trigger_firmware_problem,
)
from tests.ops_speed_sim import (
    SHORT_DUMP,
    FakePinButton,
    SimulatedOps133,
    fast_replies,
    sim_radar,
)
from tests.test_ops243 import _ScheduledSerial, _UartRxBuffer
from tests.test_ops243_mode_commands import RecordingSerial
from tests.test_wall_clock_steps import _REAL_TIME as REAL_TIME, StepClock

SETUP = {
    "threshold_mph": 25,
    "magnitude": 40,
    "pre_trigger_segments": 2,
    "sample_rate_ksps": 20,
}


@pytest.fixture(name="radar")
def _recording_radar(monkeypatch):
    """A radar on a recording port with silent replies (writes only)."""
    monkeypatch.setattr("openflight.ops243.time.sleep", lambda _s: None)
    monkeypatch.setattr(OPS243Radar, "_read_reply", lambda _self, *_a, **_kw: "")
    instance = OPS243Radar.__new__(OPS243Radar)
    instance.serial = RecordingSerial()
    instance._unit = "mph"
    instance._json_mode = False
    instance._magnitude_enabled = False
    instance._speed_read_buffer = ""
    return instance


@pytest.fixture(name="sim")
def _sim(monkeypatch):
    fast_replies(monkeypatch)
    return SimulatedOps133(pin=FakePinButton(22), dump=SHORT_DUMP)


def _commands(writes) -> list[bytes]:
    """Writes with each trailing CR folded into its command."""
    commands: list[bytes] = []
    for data in writes:
        if data == b"\r" and commands:
            commands[-1] += data
        else:
            commands.append(data)
    return commands


# --- firmware version ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1.3.3", (1, 3, 3)),
        (" 1.3.10 ", (1, 3, 10)),
        ("1.3", None),
        ("v1.3.3", None),
        ("1.3.3-beta", None),
        ("", None),
        (None, None),
        (133, None),
    ],
)
def test_parse_firmware_version(text, expected):
    assert parse_firmware_version(text) == expected


@pytest.mark.parametrize(
    ("version", "fragment"),
    [
        (None, "unknown"),
        ("", "unknown"),
        ("garbage", "not major.minor.patch"),
        ("1.3.0", "below 1.3.3"),
        ("1.3.2", "below 1.3.3"),
        ("1.2.9", "below 1.3.3"),
        ("1.4.0", "outside the 1.3 release train"),
        ("2.0.0", "outside the 1.3 release train"),
    ],
)
def test_firmware_problem_names_the_version(version, fragment):
    problem = speed_trigger_firmware_problem(version)
    assert problem is not None and fragment in problem
    if version:
        assert version in problem


@pytest.mark.parametrize("version", ["1.3.3", "1.3.4", "1.3.12"])
def test_firmware_in_the_13_train_from_133_is_accepted(version):
    assert speed_trigger_firmware_problem(version) is None


def test_radar_built_without_init_has_no_firmware_version():
    radar = OPS243Radar.__new__(OPS243Radar)
    assert radar.firmware_version is None
    assert radar.firmware_tuple() is None
    radar.firmware_version = "1.3.3"
    assert radar.firmware_tuple() == (1, 3, 3)


def test_uart_negotiation_keeps_the_version_it_read():
    sim = SimulatedOps133(version="1.3.3")
    radar = OPS243Radar(port="/dev/ttyAMA0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        assert radar.negotiate_uart_baud() == 230400
    assert radar.firmware_version == "1.3.3"
    assert radar.firmware_tuple() == (1, 3, 3)


def test_default_usb_connect_writes_nothing_to_the_radar():
    """Byte identity: the default connect sends no command (no new ?V probe)."""
    sim = SimulatedOps133()
    radar = OPS243Radar(port="/dev/ttyACM0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        assert radar.connect() is True
    assert sim.writes == []
    assert radar.firmware_version is None


def test_usb_connect_with_probe_reads_the_version_once(monkeypatch):
    fast_replies(monkeypatch)
    sim = SimulatedOps133(version="1.3.3")
    radar = OPS243Radar(port="/dev/ttyACM0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        radar.connect(probe_firmware=True)
    assert sim.writes == [b"?V"]
    assert radar.firmware_version == "1.3.3"


def test_uart_connect_with_probe_reuses_the_negotiated_version(monkeypatch):
    fast_replies(monkeypatch)
    sim = SimulatedOps133(version="1.3.3")
    radar = OPS243Radar(port="/dev/ttyAMA0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        radar.connect(probe_firmware=True)
    assert sim.commands == ["?V"]
    assert radar.firmware_version == "1.3.3"


def test_reconnect_forgets_the_old_version(monkeypatch):
    fast_replies(monkeypatch)
    sim = SimulatedOps133(version="1.3.3")
    radar = OPS243Radar(port="/dev/ttyACM0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        radar.connect(probe_firmware=True)
        sim.version = "1.3.0"
        radar.connect()
    assert radar.firmware_version is None


def test_get_firmware_version_keeps_the_answer(monkeypatch):
    fast_replies(monkeypatch)
    radar = sim_radar(SimulatedOps133(version="1.3.3"))
    assert radar.get_firmware_version() == "1.3.3"
    assert radar.firmware_version == "1.3.3"


# --- CR rule -------------------------------------------------------------------


@pytest.mark.parametrize("command", ["ST-25", "SM25", "SP0", "ST-200"])
def test_speed_trigger_commands_end_with_cr(radar, command):
    radar._send_command(command)
    assert radar.serial.writes == [command.encode(), b"\r"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("S#2", [b"S#2", b"\r"]),
        ("S=20", [b"S=20", b"\r"]),
        ("R>20", [b"R>20", b"\r"]),
        ("R<200", [b"R<200", b"\r"]),
        ("X=2", [b"X=2", b"\r"]),
        ("O=12", [b"O=12", b"\r"]),
        ("PA", [b"PA"]),
        ("PI", [b"PI"]),
        ("US", [b"US"]),
        ("P3", [b"P3"]),
        ("GS", [b"GS"]),
        ("GC", [b"GC"]),
        ("S?", [b"S?"]),
        ("S(", [b"S("]),
        ("R-", [b"R-"]),
        ("??", [b"??"]),
        ("?V", [b"?V"]),
        ("I?", [b"I?"]),
        ("W0", [b"W0"]),
        ("OJ", [b"OJ"]),
    ],
)
def test_existing_commands_keep_their_bytes(radar, command, expected):
    """Byte identity for every command the default paths send."""
    radar._send_command(command)
    assert radar.serial.writes == expected


# --- prepare_internal_speed_trigger ------------------------------------------------


@pytest.mark.parametrize("version", ["1.3.0", "1.4.0", "garbage", None])
def test_prepare_refuses_other_firmware_and_writes_nothing(radar, version):
    radar.firmware_version = version
    with pytest.raises(OPSFirmwareError) as exc:
        radar.prepare_internal_speed_trigger(**SETUP)
    assert radar.serial.writes == []
    if version:
        assert version in str(exc.value)


def test_prepare_on_133_sends_the_sequence_with_cr(radar):
    radar.firmware_version = "1.3.3"
    radar.prepare_internal_speed_trigger(**SETUP)

    assert _commands(radar.serial.writes) == [
        b"PI",
        b"US",
        b"P3",
        b"S=20\r",
        b"SM40\r",
        b"ST-25\r",
        b"S#2\r",
        b"??",
    ]


def test_prepare_never_sends_gc_gs_a_bang_or_a_final_pa(radar):
    radar.firmware_version = "1.3.3"
    radar.prepare_internal_speed_trigger(**SETUP)
    commands = _commands(radar.serial.writes)
    for forbidden in (b"GC", b"GS", b"A!", b"PA"):
        assert forbidden not in commands
    assert b"".join(radar.serial.writes).count(b"PA") == 0


def test_prepare_keeps_the_raw_settings_reply_and_the_board_idle(sim):
    radar = sim_radar(sim, firmware_version="1.3.3")
    sim.armed = True  # booted armed in persisted GC

    summary = radar.prepare_internal_speed_trigger(**SETUP)

    assert sim.armed is False  # PI, and no PA afterwards
    assert '"SpeedTrigger":"-25"' in summary["settings_reply"]
    assert '"SpeedTriggerMagnitude":"40"' in summary["settings_reply"]
    assert summary["firmware"] == "1.3.3"
    assert summary["speed_trigger_commands"] == ["SM40", "ST-25"]
    assert summary["resend_on_rearm"] is False
    assert radar.last_speed_trigger_setup == summary
    assert radar.speed_trigger_commands == ("SM40", "ST-25")
    assert sim.settings == {
        "units": "mph",
        "power": "3",
        "S=": "20",
        "SM": "40",
        "ST": "-25",
        "S#": "2",
    }


def test_prepare_with_power_none_keeps_the_current_power(radar):
    radar.firmware_version = "1.3.3"
    radar.prepare_internal_speed_trigger(**SETUP, transmit_power=None)
    assert not any(cmd.startswith(b"P") and cmd != b"PI" for cmd in _commands(radar.serial.writes))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("threshold_mph", 0),
        ("threshold_mph", 201),
        ("threshold_mph", 25.5),
        ("threshold_mph", True),
        ("magnitude", 0),
        ("magnitude", 2001),
        ("magnitude", "40"),
        ("pre_trigger_segments", -1),
        ("pre_trigger_segments", 33),
        ("sample_rate_ksps", 0),
    ],
)
def test_prepare_rejects_bad_values_before_writing(radar, field, value):
    radar.firmware_version = "1.3.3"
    with pytest.raises(ValueError, match=field):
        radar.prepare_internal_speed_trigger(**{**SETUP, field: value})
    assert radar.serial.writes == []


def test_prepare_rejects_bad_power_before_writing(radar):
    radar.firmware_version = "1.3.3"
    with pytest.raises(ValueError, match="transmit_power"):
        radar.prepare_internal_speed_trigger(**SETUP, transmit_power=8)
    assert radar.serial.writes == []


def test_prepare_needs_a_connection():
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = None
    with pytest.raises(ConnectionError):
        radar.prepare_internal_speed_trigger(**SETUP)


# --- off command ---------------------------------------------------------------------


def test_disable_raises_while_the_off_command_is_unknown(radar):
    assert ops243.SPEED_TRIGGER_OFF_COMMANDS == ()
    with pytest.raises(OPSFirmwareError, match="power-cycle"):
        radar.disable_internal_speed_trigger()
    assert radar.serial.writes == []


def test_disable_sends_the_off_commands_once_known(radar, monkeypatch):
    monkeypatch.setattr(ops243, "SPEED_TRIGGER_OFF_COMMANDS", ("ST0", "SM0"))
    radar.speed_trigger_commands = ("SM40", "ST-25")
    radar.disable_internal_speed_trigger()
    assert _commands(radar.serial.writes) == [b"ST0\r", b"SM0\r"]
    assert radar.speed_trigger_commands == ()


# --- re-arm --------------------------------------------------------------------------


def test_default_rearm_writes_exactly_pa_split_pa(radar):
    """Byte identity: no SM/ST in the default re-arm, also after a speed setup."""
    radar.firmware_version = "1.3.3"
    radar.prepare_internal_speed_trigger(**SETUP)
    radar.serial.writes.clear()

    assert radar.rearm_rolling_buffer(2) is True
    assert radar.serial.writes == [b"PA", b"S#2\r", b"PA"]


def test_resend_on_rearm_writes_sm_and_st_before_the_last_pa(radar):
    radar.firmware_version = "1.3.3"
    radar.prepare_internal_speed_trigger(**SETUP)
    radar.speed_trigger_resend_on_rearm = True
    radar.serial.writes.clear()

    radar.rearm_rolling_buffer(2)

    assert radar.serial.writes == [b"PA", b"S#2\r", b"SM40\r", b"ST-25\r", b"PA"]


def test_resend_without_a_speed_setup_writes_nothing_extra(radar):
    radar.speed_trigger_resend_on_rearm = True
    radar.rearm_rolling_buffer(2)
    assert radar.serial.writes == [b"PA", b"S#2\r", b"PA"]


def test_simulated_board_loses_st_after_a_dump_until_resent(sim):
    radar = sim_radar(sim, firmware_version="1.3.3")
    radar.prepare_internal_speed_trigger(**SETUP)
    radar.rearm_rolling_buffer(2)
    assert sim.fire(30.0)
    radar.wait_for_hardware_trigger(timeout=0.5, capture_markers=SPEED_TRIGGER_MARKERS)

    radar.rearm_rolling_buffer(2)
    assert not sim.fire(30.0)  # ST/SM gone, as M1 may find

    radar.speed_trigger_resend_on_rearm = True
    radar.rearm_rolling_buffer(2)
    assert sim.fire(30.0)


# --- dump wait: markers and preamble -------------------------------------------------


def _wait_radar(serial_obj) -> OPS243Radar:
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = serial_obj
    return radar


TRIP = b'{"trigger_time":"946.150"}\r\n'


def test_speed_markers_skip_an_early_trigger_time_line():
    serial_obj = _UartRxBuffer()
    serial_obj.feed(TRIP + SHORT_DUMP)
    radar = _wait_radar(serial_obj)

    response = radar.wait_for_hardware_trigger(timeout=0.3, capture_markers=SPEED_TRIGGER_MARKERS)

    assert response == SHORT_DUMP.decode("ascii")
    assert radar.last_hardware_trigger_preamble == TRIP
    assert radar.last_hardware_trigger_preamble_timestamp is not None
    assert radar.last_hardware_trigger_first_byte_timestamp is not None


class _TripThenDumpSerial:
    """The trip message, a few empty polls, then the dump; reads no clock itself."""

    def __init__(self, idle_polls: int = 5):
        self.is_open = True
        self._chunks = [TRIP, SHORT_DUMP]
        self._idle_polls = idle_polls

    @property
    def in_waiting(self):
        if len(self._chunks) == 1 and self._idle_polls > 0:
            self._idle_polls -= 1
            return 0
        return len(self._chunks[0]) if self._chunks else 0

    def read(self, _n):
        return self._chunks.pop(0)


def test_preamble_time_comes_from_the_marker_stamp_without_a_second_wall_read(monkeypatch, caplog):
    """The first-byte stamp stays the wait's first wall-clock read (NTP steps).

    The wall clock jumps a day after its first read; the first-byte stamp must
    still be real time, and the preamble is dated against it. Logging is on, as
    it may be in the full suite (log records read the clock too).
    """
    caplog.set_level(logging.DEBUG, logger="ops243")
    monkeypatch.setattr(time, "time", StepClock())
    radar = _wait_radar(_TripThenDumpSerial())

    response = radar.wait_for_hardware_trigger(timeout=0.5, capture_markers=SPEED_TRIGGER_MARKERS)

    assert response == SHORT_DUMP.decode("ascii")
    assert radar.last_hardware_trigger_preamble == TRIP
    first_byte = radar.last_hardware_trigger_first_byte_timestamp
    assert abs(first_byte - REAL_TIME()) < 5.0
    lead_s = first_byte - radar.last_hardware_trigger_preamble_timestamp
    assert 0.02 <= lead_s <= 0.5


def test_default_markers_still_start_at_a_trigger_time_line():
    """Byte identity: a HOST_INT wait returns from the first marker of either kind."""
    serial_obj = _UartRxBuffer()
    serial_obj.feed(TRIP + SHORT_DUMP)
    radar = _wait_radar(serial_obj)

    response = radar.wait_for_hardware_trigger(timeout=0.3)

    assert response == (TRIP + SHORT_DUMP).decode("ascii")
    assert radar.last_hardware_trigger_preamble == b""
    assert radar.last_hardware_trigger_preamble_timestamp is None
    assert HARDWARE_TRIGGER_MARKERS == (b'{"sample_time"', b'{"trigger_time"')


def test_default_wait_returns_the_same_with_idle_noise():
    noise = b'\n\n{"Clock":1786805707}\r\n\n'
    serial_obj = _UartRxBuffer()
    serial_obj.feed(noise + SHORT_DUMP)
    radar = _wait_radar(serial_obj)

    assert radar.wait_for_hardware_trigger(timeout=0.3) == SHORT_DUMP.decode("ascii")
    assert radar.last_hardware_trigger_preamble == noise


def test_preamble_is_capped_to_its_last_bytes():
    filler = b"".join(b"%04d noise line\r\n" % index for index in range(60))
    assert len(filler) > HARDWARE_TRIGGER_PREAMBLE_BYTES
    # Two reads, so the cap holds across discarded chunks and the marker read.
    radar = _wait_radar(_ScheduledSerial([(0.0, filler[:400]), (0.03, filler[400:] + SHORT_DUMP)]))

    response = radar.wait_for_hardware_trigger(timeout=0.5)

    assert response == SHORT_DUMP.decode("ascii")
    assert len(radar.last_hardware_trigger_preamble) == HARDWARE_TRIGGER_PREAMBLE_BYTES
    assert radar.last_hardware_trigger_preamble == filler[-HARDWARE_TRIGGER_PREAMBLE_BYTES:]


def test_trip_message_without_a_dump_is_kept_after_a_timeout():
    serial_obj = _UartRxBuffer()
    serial_obj.feed(b"TRIP 12.5\r\n")
    radar = _wait_radar(serial_obj)

    assert radar.wait_for_hardware_trigger(timeout=0.1) == ""
    assert radar.last_hardware_trigger_preamble == b"TRIP 12.5\r\n"


def test_empty_marker_list_is_refused():
    radar = _wait_radar(_UartRxBuffer())
    with pytest.raises(ValueError, match="marker"):
        radar.wait_for_hardware_trigger(timeout=0.1, capture_markers=())


def test_lost_capture_detection_is_unchanged_with_speed_markers():
    serial_obj = _UartRxBuffer()
    serial_obj.feed(b",".join([b"2048"] * 600) + b']}\r\n{"Q":[2048,2047]}')
    radar = _wait_radar(serial_obj)

    assert radar.wait_for_hardware_trigger(timeout=2.0, capture_markers=SPEED_TRIGGER_MARKERS) == ""
    assert radar.last_hardware_trigger_lost_capture_bytes > 1024


# --- end to end with the simulator ---------------------------------------------------


def test_simulated_trip_reads_the_dump_with_its_trip_message(monkeypatch):
    fast_replies(monkeypatch)
    pin = FakePinButton(22)
    sim = SimulatedOps133(pin=pin, dump=SHORT_DUMP, trip_message=TRIP)
    radar = sim_radar(sim, firmware_version="1.3.3")
    radar.prepare_internal_speed_trigger(**SETUP)

    assert not sim.fire(40.0)  # idle until the first re-arm
    radar.rearm_rolling_buffer(2)
    assert not sim.fire(20.0)  # below ST-25
    assert sim.fire(40.0)
    assert (pin.presses, pin.releases) == (1, 1)

    response = radar.wait_for_hardware_trigger(timeout=0.5, capture_markers=SPEED_TRIGGER_MARKERS)
    assert response == SHORT_DUMP.decode("ascii")
    assert radar.last_hardware_trigger_preamble == TRIP
    assert sim.armed is False
    assert not sim.fire(40.0)  # idle until PA


def test_simulated_host_int_dump_and_pin(monkeypatch):
    fast_replies(monkeypatch)
    pin = FakePinButton(22)
    sim = SimulatedOps133(pin=pin, dump=SHORT_DUMP, pin_on_host_int=False)
    radar = sim_radar(sim, firmware_version="1.3.3")
    radar.rearm_rolling_buffer(2)

    assert sim.host_int()
    assert pin.presses == 0
    assert radar.wait_for_hardware_trigger(timeout=0.5) == SHORT_DUMP.decode("ascii")
