"""Tests for the IWR6843 CLI and dump serial contract."""

from __future__ import annotations

import logging

import numpy as np
import pytest
from iwr6843_scripted_cli import PROMPT, done, scripted_radar, stats_line

from openflight.iwr6843 import driver as driver_module
from openflight.iwr6843.driver import IWR6843Radar
from openflight.iwr6843.dump import TEMP_REPORT_KEYS, pack_dump, parse_header
from openflight.iwr6843.health import (
    HWA_STALL,
    RF_STOPPED,
    SILENT_CLI,
    TiWedgedError,
)

HWA_STOP_REPLY = b"sensorStop\r\nError: HWA shutdown boundary timed out\r\nError -1" + PROMPT


def _config(tmp_path, text="dfeDataOutputMode 1\nsensorStart\n"):
    config = tmp_path / "radar.cfg"
    config.write_text(text, encoding="utf-8")
    return str(config)


def test_send_config_rejects_missing_cli_acknowledgement(tmp_path):
    """A wedged board must not be reported as configured and armed."""
    radar, serial, _clock = scripted_radar({"sensorStop": [b""], "": [b""]})

    with pytest.raises(TiWedgedError, match="did not acknowledge") as info:
        radar.send_config(_config(tmp_path))

    assert isinstance(info.value, RuntimeError)
    assert info.value.cause == SILENT_CLI
    assert "the firmware may be wedged (press RESET and retry)" in str(info.value)
    assert serial.sent("sensorStop") == 2


def test_send_config_logs_pi_power_state_on_failure(tmp_path, monkeypatch):
    radar, _serial, _clock = scripted_radar({"sensorStop": [HWA_STOP_REPLY]})
    logged = []
    monkeypatch.setattr(driver_module, "log_power_state", logged.append)

    with pytest.raises(TiWedgedError):
        radar.send_config(_config(tmp_path))

    assert logged == [driver_module.logger]


def test_open_failure_logs_pi_power_state(monkeypatch):
    logged = []
    monkeypatch.setattr(driver_module, "log_power_state", logged.append)

    def refuse(*_args, **_kwargs):
        raise OSError("could not open port")

    monkeypatch.setattr(driver_module, "open_port", refuse)

    with pytest.raises(OSError):
        IWR6843Radar(port="/dev/missing-iwr6843")

    assert logged == [driver_module.logger]


def test_send_config_hwa_timeout_is_a_stall_without_retry(tmp_path):
    radar, serial, _clock = scripted_radar({"sensorStop": [HWA_STOP_REPLY]})

    with pytest.raises(TiWedgedError, match="HWA shutdown boundary timed out") as info:
        radar.send_config(_config(tmp_path))

    assert info.value.cause == HWA_STALL
    assert "did not acknowledge" in str(info.value)
    assert serial.sent("sensorStop") == 1
    assert serial.sent("flushCfg") == 0


def test_send_config_resyncs_and_retries_a_garbled_sensor_stop(tmp_path):
    garbled = b"ssensorStop\r\n'ssensorStop' is not recognized as a CLI command" + PROMPT
    radar, serial, _clock = scripted_radar({"sensorStop": [garbled, done("sensorStop")]})

    radar.send_config(_config(tmp_path))

    assert serial.sent("sensorStop") == 2
    assert serial.sent("") == 2
    assert serial.sent("sensorStart") == 1


def test_send_config_accepts_a_late_sensor_start_done_with_warning(tmp_path, caplog):
    late = [(0.0, b"sensorStart\r\n"), (9.0, b"Done" + PROMPT)]
    radar, serial, _clock = scripted_radar({"sensorStart": [late]})

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        radar.send_config(_config(tmp_path))

    assert serial.sent("sensorStart") == 1
    assert "sensorStart acknowledged late" in caplog.text


def test_send_config_fails_when_sensor_start_never_acknowledges(tmp_path):
    never = [(0.0, b"sensorStart\r\n"), (30.0, b"Done" + PROMPT)]
    radar, serial, clock = scripted_radar({"sensorStart": [never]})
    started = clock.now

    with pytest.raises(TiWedgedError, match="did not acknowledge 'sensorStart'") as info:
        radar.send_config(_config(tmp_path))

    assert info.value.cause == SILENT_CLI
    assert serial.sent("sensorStart") == 1
    assert clock.now - started < 30.0


def test_send_config_detects_hwa_stall_after_active(tmp_path, caplog):
    frozen = stats_line(frames=0, hwa_out=0)

    def frames_without_hwa(serial):
        return stats_line(frames=int(serial.clock.now * 333), hwa_out=0)

    radar, serial, _clock = scripted_radar({"stats": [frozen, frames_without_hwa]})

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.driver"):
        with pytest.raises(TiWedgedError) as info:
            radar.send_config(_config(tmp_path))

    assert info.value.cause == HWA_STALL
    assert "HWA chain stalled" in str(info.value)
    assert serial.sent("stats") == 5
    assert "TI stats: stats frames=" in caplog.text
    assert "at active=1, 1.00s earlier: stats frames=0 " in caplog.text


def test_send_config_detects_rf_frames_not_running(tmp_path, caplog):
    radar, _serial, _clock = scripted_radar({"stats": [stats_line(frames=4, hwa_out=4)]})

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.driver"):
        with pytest.raises(TiWedgedError) as info:
            radar.send_config(_config(tmp_path))

    assert info.value.cause == RF_STOPPED
    assert "TI stats: stats frames=4 " in caplog.text


def test_send_config_accepts_a_slow_but_moving_start_with_warning(tmp_path, caplog):
    statuses = [stats_line(frames=100, hwa_out=100), stats_line(frames=103, hwa_out=103)]
    radar, serial, _clock = scripted_radar({"stats": statuses})

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.driver"):
        radar.send_config(_config(tmp_path))

    assert serial.sent("stats") == 5
    assert "TI capture looks slow after sensorStart: only 3 new RF frames" in caplog.text
    assert "TI stats: stats frames=103 " in caplog.text
    assert "earlier: stats frames=100 " in caplog.text
    assert "TI configured in" in caplog.text


def test_send_config_warns_when_hwa_output_lags_frames(tmp_path, caplog):
    def lagging_hwa(serial):
        frames = int(serial.clock.now * 333)
        return stats_line(frames=frames, hwa_out=frames // 4)

    radar, _serial, _clock = scripted_radar({"stats": [lagging_hwa]})

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        radar.send_config(_config(tmp_path))

    assert "TI capture looks slow after sensorStart: only" in caplog.text
    assert "new HWA outputs for" in caplog.text


def test_send_config_judges_firmware_without_hwa_counter_on_frames(tmp_path, caplog):
    def frames_only(serial):
        return done("stats", f"frames={int(serial.clock.now * 333)} active=1 calib=0x1ffe")

    radar, serial, _clock = scripted_radar({"stats": [frames_only]})

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        radar.send_config(_config(tmp_path))

    assert serial.sent("stats") == 2
    assert "looks slow" not in caplog.text

    frozen, _serial, _clock = scripted_radar({"stats": [done("stats", "frames=7 active=1")]})
    with pytest.raises(TiWedgedError) as info:
        frozen.send_config(_config(tmp_path))
    assert info.value.cause == RF_STOPPED


def test_send_config_logs_summary_and_stats(tmp_path, caplog):
    radar, _serial, _clock = scripted_radar()

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.driver"):
        radar.send_config(_config(tmp_path))

    assert "TI configured in" in caplog.text
    assert "TI stats: stats frames=" in caplog.text
    assert "looks slow" not in caplog.text


def test_command_reports_and_warns_about_leftover_bytes(caplog):
    radar, serial, _clock = scripted_radar()
    serial.queue(b"stale garbage" + PROMPT)

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        reply = radar.command("flushCfg")

    assert reply.ok
    assert reply.residue == "stale garbage"
    assert "Discarding unread TI output" in caplog.text


def test_close_warns_when_serial_close_is_slow(caplog):
    radar, serial, _clock = scripted_radar()
    serial.close_delay_s = 5.2

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        radar.close()

    assert serial.closed
    assert "Closing the TI serial port took 5.2s" in caplog.text


def test_stop_sensor_requires_acknowledgement_and_inactive_health():
    """Shutdown must leave firmware idle rather than merely close the host UART."""
    radar, serial, _clock = scripted_radar({"stats": [stats_line(frames=9, hwa_out=9, active=0)]})

    radar.stop_sensor()

    assert serial.writes == [b"sensorStop\n", b"stats\n"]


def test_stop_sensor_rejects_firmware_that_remains_active():
    radar, _serial, _clock = scripted_radar()

    with pytest.raises(RuntimeError, match="remained active"):
        radar.stop_sensor()


def test_stop_sensor_without_retry_spends_one_window_on_a_silent_cli():
    radar, serial, clock = scripted_radar({"sensorStop": [b""], "": [b""]})
    resyncs = []
    radar.resync = lambda **kwargs: resyncs.append(kwargs) or False
    started = clock.now

    with pytest.raises(TiWedgedError) as info:
        radar.stop_sensor(retry=False)

    assert info.value.cause == SILENT_CLI
    assert resyncs == []
    assert serial.writes == [b"sensorStop\n"]
    assert clock.now - started < 4.0


def test_stop_sensor_reports_hwa_stall():
    radar, _serial, _clock = scripted_radar({"sensorStop": [HWA_STOP_REPLY]})

    with pytest.raises(TiWedgedError, match="HWA shutdown boundary timed out"):
        radar.stop_sensor()


def test_send_config_flushes_previous_mmwave_profile_when_config_omits_flush(tmp_path):
    """Repeated startup must not exhaust the firmware's mmWave profile slots."""
    radar, serial, _clock = scripted_radar()

    radar.send_config(_config(tmp_path))

    commands = [write.decode().strip() for write in serial.writes]
    assert commands == [
        "",
        "sensorStop",
        "flushCfg",
        "dfeDataOutputMode 1",
        "sensorStart",
        "stats",
        "stats",
    ]


def test_send_config_waits_for_sensor_to_become_active(tmp_path):
    """sensorStart may acknowledge before RF calibration and HWA startup finish."""
    statuses = [
        done("stats", "frames=0 active=0 calib=0x0 hwa_frames=0 hwa_out=0"),
        done("stats", "frames=0 active=0 calib=0x1ffe hwa_frames=0 hwa_out=0"),
        stats_line(frames=2, hwa_out=2),
        stats_line(frames=90, hwa_out=89),
    ]
    radar, serial, _clock = scripted_radar({"stats": statuses})

    radar.send_config(_config(tmp_path, "sensorStart\n"))

    assert serial.sent("stats") == 4


def _dump_reply(*segments):
    return [[(delay, data) for delay, data in segments]]


def test_read_dump_fails_fast_on_cli_error_before_magic():
    refusal = b"l3dump\r\nError: HWA post-trigger frame freeze timed out\r\nError -1" + PROMPT
    radar, _serial, clock = scripted_radar({"l3dump": [refusal]})
    started = clock.now

    with pytest.raises(RuntimeError, match="before ILD1.*freeze timed out"):
        radar.read_dump()

    assert clock.now - started < 1.0


def test_read_dump_ignores_error_text_inside_binary_payload():
    raw = bytearray(pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3))
    offset = parse_header(bytes(raw))["header_nbytes"]
    raw[offset : offset + 5] = b"Error"
    reply = _dump_reply(
        (0.0, b"l3dump\r\n" + bytes(raw[: offset + 20])),
        (0.5, bytes(raw[offset + 20 :])),
        (0.5, b"Done" + PROMPT),
    )
    radar, _serial, _clock = scripted_radar({"l3dump": reply})

    assert radar.read_dump() == bytes(raw)


def test_read_dump_stops_at_trailer_of_short_dump(caplog):
    raw = pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3)
    short = raw[:-100]
    reply = _dump_reply((0.0, b"l3dump\r\n" + short), (0.0, b"Done\nl3dump:/>"))
    radar, _serial, clock = scripted_radar({"l3dump": reply})
    started = clock.now

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        dump = radar.read_dump()

    assert dump == short
    assert clock.now - started < 1.0
    assert "Dump read ended (trailer)" in caplog.text


class FakeSerial:
    """Serial double that exposes the in_waiting/read/write pieces read_dump uses."""

    def __init__(self, payload: bytes):
        self.payload = bytearray(payload)
        self.writes = []

    @property
    def in_waiting(self):
        return len(self.payload)

    def reset_input_buffer(self):
        pass

    def write(self, data: bytes):
        self.writes.append(data)

    def read(self, nbytes: int):
        nbytes = min(nbytes, len(self.payload))
        chunk = self.payload[:nbytes]
        del self.payload[:nbytes]
        return bytes(chunk)


def test_read_dump_waits_for_cli_ready_after_binary_payload():
    raw = pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3)

    class ChunkedSerial:
        def __init__(self):
            self.chunks = [bytearray(b"l3dump\r\n" + raw), bytearray(b"Done\r\nl3dump:/>")]
            self.writes = []
            self.delay_next_chunk = False

        @property
        def in_waiting(self):
            if self.delay_next_chunk:
                return 0
            return len(self.chunks[0]) if self.chunks else 0

        def reset_input_buffer(self):
            return None

        def write(self, value):
            self.writes.append(value)

        def read(self, count):
            if self.delay_next_chunk:
                self.delay_next_chunk = False
                return b""
            if not self.chunks:
                return b""
            chunk = self.chunks[0]
            data = bytes(chunk[:count])
            del chunk[:count]
            if not chunk:
                self.chunks.pop(0)
                if self.chunks:
                    self.delay_next_chunk = True
            return data

    radar = IWR6843Radar.__new__(IWR6843Radar)
    radar.ser = ChunkedSerial()

    assert radar.read_dump(timeout_s=0.1) == raw
    assert radar.ser.chunks == []
    assert radar.ser.writes == [b"l3dump\n"]


def test_read_dump_reports_firmware_restart_error_after_binary_payload():
    raw = pack_dump(np.ones((1, 3, 4, 4), dtype=complex), n_tx=3, version=3)
    radar = IWR6843Radar.__new__(IWR6843Radar)
    radar.ser = FakeSerial(b"l3dump\r\n" + raw + b"Error: RF restart failed\r\n")

    with pytest.raises(RuntimeError, match="RF restart failed"):
        radar.read_dump(timeout_s=0.1)


def test_read_dump_sizes_v5_header_extension():
    report = {key: index + 40 for index, key in enumerate(TEMP_REPORT_KEYS)}
    raw = pack_dump(
        np.zeros((2, 4, 4, 8), dtype=complex),
        n_tx=2,
        version=5,
        temperature_report=report,
    )
    serial = FakeSerial(b"cli echo\r\n" + raw + b"trailing cli noise")
    radar = IWR6843Radar.__new__(IWR6843Radar)
    radar.ser = serial

    dump = radar.read_dump(timeout_s=0.1)

    assert dump == raw
    assert serial.writes == [b"l3dump\n"]


class _StaleSerial:
    def __init__(self, close_error: Exception | None = None):
        self.close_error = close_error
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize("close_error", [None, OSError("device disconnected")])
def test_reopen_closes_the_old_handle_and_opens_the_same_port(monkeypatch, close_error):
    opened = []
    stale = _StaleSerial()
    fresh = object()
    handles = iter([stale, fresh])

    def fake_open(port, baud):
        opened.append((port, baud))
        return next(handles)

    monkeypatch.setattr(driver_module, "open_port", fake_open)
    radar = IWR6843Radar(port="/dev/serial/by-id/ti-port0", baud=921600)
    stale.close_error = close_error

    radar.reopen()

    assert stale.close_calls == 1
    assert opened == [("/dev/serial/by-id/ti-port0", 921600)] * 2
    assert radar.ser is fresh


def test_read_dump_ends_early_when_no_byte_arrives(caplog):
    """A silent CLI used to hold the capture thread for the full 40 s timeout."""
    radar, _serial, clock = scripted_radar({"l3dump": [b""]})
    started = clock.now

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        dump = radar.read_dump()

    assert dump == b""
    assert 4.0 <= clock.now - started < 5.0
    assert "Dump read ended (no first byte): 0/? bytes" in caplog.text


def test_read_dump_first_byte_window_is_configurable():
    radar, _serial, clock = scripted_radar({"l3dump": [b""]})
    started = clock.now

    assert radar.read_dump(first_byte_s=3.0) == b""
    assert 3.0 <= clock.now - started < 4.0


def test_read_dump_echo_only_ends_at_the_stall_tolerance(caplog):
    radar, _serial, clock = scripted_radar({"l3dump": [b"l3dump\r\n"]})
    started = clock.now

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.driver"):
        dump = radar.read_dump()

    assert dump == b"l3dump\r\n"
    assert clock.now - started < 5.0
    assert "Dump read ended (stall)" in caplog.text


def test_read_dump_waits_for_a_late_but_healthy_first_byte():
    raw = pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3)
    reply = [[(3.5, b"l3dump\r\n" + raw), (3.6, b"Done" + PROMPT)]]
    radar, _serial, _clock = scripted_radar({"l3dump": reply})

    assert radar.read_dump() == raw
