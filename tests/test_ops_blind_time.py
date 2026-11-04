"""OPS blind time after a dump: re-arm timings, lost sound edges and late first bytes.

Range 2026-10-25: every empty false dump blinded the OPS for ~2.48 s (1.78 s
transfer + 0.59 s drain and re-arm); edges inside it left no trigger_event, and
one dump queued during the last re-arm settle was read 270 ms after its edge
and logged as ``unmatched`` (edge #111).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import serial

from openflight import server
from openflight.ops243 import OPS243Radar
from openflight.rolling_buffer import RollingBufferMonitor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.session_logger import SessionLogger
from tests.test_trigger_gate import EDGE_EPOCH, FakeClock, make_gate

POST_MS = 102.4  # S#8 at 30 ksps


class QuietSerial:
    """Port with nothing pending, so re-arm goes straight to its commands."""

    def __init__(self):
        self.is_open = True
        self.writes = []
        self.in_waiting = 0
        self.out_waiting = 0

    def read(self, _n):
        return b""

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass


class TimeoutOnWriteSerial(QuietSerial):
    """Radar that stopped servicing commands (mid-dump)."""

    def write(self, data):
        raise serial.SerialTimeoutException("Write timeout")


@pytest.fixture(name="sleeps")
def fixture_sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr("openflight.ops243.time.sleep", recorded.append)
    return recorded


def _radar(fast: bool, port=None) -> OPS243Radar:
    radar = OPS243Radar(fast_rearm=fast)
    radar.serial = port or QuietSerial()
    return radar


# --- 1. re-arm timings ------------------------------------------------------


def test_default_rearm_keeps_the_measured_timings(sleeps):
    radar = _radar(fast=False)
    assert radar.rearm_rolling_buffer(8) is True
    assert sleeps == [0.2, 0.1, 0.1, 0.15]
    assert radar.serial.writes == [b"PA", b"S#8\r", b"PA"]
    assert (
        OPS243Radar.REARM_DRAIN_QUIET_S,
        OPS243Radar.REARM_PA_SETTLE_S,
        OPS243Radar.REARM_SPLIT_SETTLE_S,
        OPS243Radar.REARM_FINAL_PA_SETTLE_S,
    ) == (0.2, 0.1, 0.1, 0.15)


def test_fast_rearm_uses_50_ms_gap_and_settles(sleeps):
    radar = _radar(fast=True)
    assert radar.rearm_rolling_buffer(8) is True
    assert sleeps == [0.05, 0.05, 0.05, 0.05]
    assert radar.serial.writes == [b"PA", b"S#8\r", b"PA"]
    assert radar.rearm_timing() == {
        "drain_quiet_s": 0.05,
        "pa_settle_s": 0.05,
        "split_settle_s": 0.05,
        "final_pa_settle_s": 0.05,
    }


def test_radar_built_without_init_rearms_with_the_default_timings(sleeps):
    radar = OPS243Radar.__new__(OPS243Radar)
    radar.serial = QuietSerial()
    assert radar.rearm_rolling_buffer(16) is True
    assert sleeps == [0.2, 0.1, 0.1, 0.15]


def test_rearm_stamps_drain_end_and_armed_time_only_when_armed(monkeypatch, sleeps):
    clock = SimpleNamespace(now=100.0)

    def tick():  # strictly increasing, however often time.time() is read
        clock.now += 0.25
        return clock.now

    monkeypatch.setattr("openflight.ops243.time.time", tick)
    radar = _radar(fast=True)
    assert radar.rearm_rolling_buffer(8) is True
    assert radar.last_rearm_drain_end_timestamp is not None
    assert radar.last_rearm_armed_timestamp > radar.last_rearm_drain_end_timestamp

    radar.serial = TimeoutOnWriteSerial()
    monkeypatch.setattr("openflight.ops243.time.time", lambda: 200.0)
    assert radar.rearm_rolling_buffer(8) is False
    assert radar.last_rearm_armed_timestamp is None


def test_monitor_passes_the_fast_rearm_flag_to_the_radar():
    assert RollingBufferMonitor(port=None).radar.fast_rearm is False
    assert RollingBufferMonitor(port=None, ops_fast_rearm=True).radar.fast_rearm is True


@pytest.mark.parametrize("fast, expected", [(False, [0.2, 0.1, 0.1, 0.15]), (True, [0.05] * 4)])
def test_manual_recovery_rearms_with_either_timing(sleeps, fast, expected):
    radar = _radar(fast=fast)
    radar.set_units = Mock()
    radar.set_transmit_power = Mock()
    radar.set_sample_rate = Mock()
    radar.prepare_persisted_rolling_buffer(8, 30, transmit_power=None, flush_timeout_s=1.0)
    assert sleeps == expected
    assert radar.serial.writes == [b"PA", b"S#8\r", b"PA"]


@pytest.mark.parametrize("fast, expected", [(False, [0.2, 0.1, 0.1, 0.15]), (True, [0.05] * 4)])
def test_startup_retry_rearms_with_either_timing(sleeps, fast, expected):
    monitor = RollingBufferMonitor(port=None, ops_fast_rearm=fast, pre_trigger_segments=8)
    radar = monitor.radar
    radar.connect = Mock()
    radar.set_units = Mock()
    radar.set_transmit_power = Mock()
    radar.set_sample_rate = Mock()
    radar.serial = TimeoutOnWriteSerial()
    assert monitor.connect() is True  # busy radar at boot: no failed start
    assert monitor.trigger._rearm_pending is True

    sleeps.clear()
    radar.serial = QuietSerial()
    monitor.trigger._rearm(radar)
    assert monitor.trigger._rearm_pending is False
    assert sleeps == expected


def _main_with(monkeypatch, tmp_path, *flags):
    for name in (
        "air_density_kg_m3",
        "typical_spin_scale",
        "ballistics_enabled",
        "battery_provider",
        "profile_store",
        "ball_speed_correction_enabled",
        "ball_speed_correction_distance_ft",
        "ball_speed_correction_ball_above_radar_ft",
        "calculated_spin_enabled",
        "_VERTICAL_RADAR_GATE_BYPASS",
        "demo_session",
    ):
        monkeypatch.setattr(server, name, getattr(server, name))
    argv = ["openflight-server", "--no-logging", "--mock", *flags]
    argv += ["--profiles-path", str(tmp_path / "profiles.json")]
    monkeypatch.setattr(sys, "argv", argv)
    started = {}
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "start_monitor", started.update)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    server.main()
    return started


def test_cli_flag_is_off_by_default_and_reaches_the_monitor(monkeypatch, tmp_path):
    assert _main_with(monkeypatch, tmp_path)["ops_fast_rearm"] is False
    assert _main_with(monkeypatch, tmp_path, "--ops-fast-rearm")["ops_fast_rearm"] is True


def test_session_config_records_the_rearm_timing(monkeypatch):
    monkeypatch.setattr(server, "monitor", SimpleNamespace(radar=OPS243Radar(fast_rearm=True)))
    assert server._session_start_config()["ops_rearm"] == {
        "fast": True,
        "drain_quiet_s": 0.05,
        "pa_settle_s": 0.05,
        "split_settle_s": 0.05,
        "final_pa_settle_s": 0.05,
    }
    monkeypatch.setattr(server, "monitor", None)
    assert "ops_rearm" not in server._session_start_config()


# --- 2. sound edges lost to OPS blind time ------------------------------------


def _edge(gate, clock: FakeClock, at_s: float) -> float:
    """A sound edge at EDGE_EPOCH + at_s; passthrough pulses at the same instant."""
    clock.at_ms(at_s * 1000.0)
    gate.on_edge(EDGE_EPOCH + at_s)
    gate.run_pending()
    return EDGE_EPOCH + at_s


def _first_byte(edge: float, lag_ms: float = 13.0) -> float:
    return edge + (POST_MS + lag_ms) / 1000.0


def test_pulses_during_dump_and_rearm_are_reported_with_their_busy_context(caplog):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        start = _edge(gate, clock, 0.0)
        gate.note_ops_dump_started(_first_byte(start), POST_MS)
        _edge(gate, clock, 1.0)  # during the 1.78 s transfer
        gate.note_ops_rearm_started(EDGE_EPOCH + 1.9)
        _edge(gate, clock, 2.0)  # re-arm drain
        _edge(gate, clock, 2.2)  # PA / S# / PA
        gate.note_ops_armed(EDGE_EPOCH + 2.4, EDGE_EPOCH + 2.1)
        _edge(gate, clock, 2.45)  # armed again: not lost

    assert [fields["gate_edge_sequence"] for fields in lost] == [2, 3, 4]
    assert [fields["ops_busy_phase"] for fields in lost] == [
        "dump",
        "rearm_drain",
        "rearm_commands",
    ]
    assert [round(fields["ops_busy_ms_left"]) for fields in lost] == [1400, 400, 200]
    assert [round(fields["ops_busy_elapsed_ms"]) for fields in lost] == [1000, 2000, 2200]
    for fields in lost:
        assert fields["ops_busy_start_edge_sequence"] == 1
        assert fields["ops_busy_start_edge_timestamp"] == start
        assert fields["ops_busy_ended_by"] == "armed"
        assert fields["gate_decision"] == "passthrough"
    assert (
        "edge #2 " in caplog.text
        and "lost to OPS busy time (dump, busy since edge #1" in caplog.text
    )
    assert "1400 ms left" in caplog.text

    summary = gate.summary()
    assert summary["ops_busy"] == 3
    assert summary["ops_busy_phases"] == {"dump": 1, "rearm_drain": 1, "rearm_commands": 1}
    assert gate.summary_text().endswith(
        "; 3 in OPS busy time (dump 1, rearm_drain 1, rearm_commands 1)"
    )


def test_bare_rearm_and_failed_rearm_keep_the_busy_period_open():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    # Stall or startup re-arm: no dump, the busy period starts with the re-arm.
    gate.note_ops_rearm_started(EDGE_EPOCH + 0.5)
    _edge(gate, clock, 0.6)
    # The re-arm write timed out: no armed note; the retry closes it.
    gate.note_ops_rearm_started(EDGE_EPOCH + 3.0)
    _edge(gate, clock, 3.1)
    gate.note_ops_armed(EDGE_EPOCH + 3.4, EDGE_EPOCH + 3.2)
    assert [fields["gate_edge_sequence"] for fields in lost] == [1, 2]
    assert all(fields["ops_busy_start_edge_sequence"] is None for fields in lost)
    assert [fields["ops_busy_phase"] for fields in lost] == ["rearm_drain", "rearm_drain"]


def test_session_end_reports_an_open_busy_period_without_time_left():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    start = _edge(gate, clock, 0.0)
    gate.note_ops_dump_started(_first_byte(start), POST_MS)
    _edge(gate, clock, 0.8)
    gate.stop()
    assert len(lost) == 1
    assert lost[0]["ops_busy_ms_left"] is None
    assert lost[0]["ops_busy_ended_by"] == "session_end"


def test_a_failing_listener_does_not_break_the_gate():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.set_ops_busy_listener(Mock(side_effect=RuntimeError("boom")))
    start = _edge(gate, clock, 0.0)
    gate.note_ops_dump_started(_first_byte(start), POST_MS)
    _edge(gate, clock, 0.5)
    gate.note_ops_armed(EDGE_EPOCH + 2.4)
    assert gate.summary()["ops_busy"] == 1


def test_pulses_after_the_dump_ended_fall_in_host_processing(caplog):
    # Range 2026-10-25, edges #180/#181: the accepted shot's dump had ended and
    # the clock sync ran (0.41-1.01 s) before the re-arm drain started.
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        start = _edge(gate, clock, 0.0)
        gate.note_ops_dump_started(_first_byte(start), POST_MS)
        _edge(gate, clock, 1.0)  # during the transfer
        gate.note_ops_dump_finished(EDGE_EPOCH + 1.9)
        _edge(gate, clock, 2.0)  # clock sync / parsing after the dump
        gate.note_ops_rearm_started(EDGE_EPOCH + 2.6)
        _edge(gate, clock, 2.7)  # re-arm drain
        gate.note_ops_armed(EDGE_EPOCH + 3.0, EDGE_EPOCH + 2.8)

    assert [fields["ops_busy_phase"] for fields in lost] == ["dump", "processing", "rearm_drain"]
    assert all(fields["ops_busy_dump_end_timestamp"] == EDGE_EPOCH + 1.9 for fields in lost)
    assert all(fields["ops_busy_started_by"] == "dump" for fields in lost)
    assert "edge #3 " in caplog.text and "(processing, busy since edge #1" in caplog.text
    assert gate.summary()["ops_busy_phases"] == {"dump": 1, "processing": 1, "rearm_drain": 1}


def test_dump_finished_only_marks_an_open_dump_busy_period():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    gate.note_ops_dump_finished(EDGE_EPOCH + 0.1)  # no busy period: ignored
    gate.note_ops_rearm_started(EDGE_EPOCH + 0.5)  # bare re-arm
    gate.note_ops_dump_finished(EDGE_EPOCH + 0.55)  # not a dump: ignored
    _edge(gate, clock, 0.6)
    gate.note_ops_armed(EDGE_EPOCH + 0.9, EDGE_EPOCH + 0.7)
    assert [fields["ops_busy_phase"] for fields in lost] == ["rearm_drain"]
    assert lost[0]["ops_busy_started_by"] == "rearm"
    assert lost[0]["ops_busy_dump_end_timestamp"] is None


def test_busy_period_of_an_unmatched_dump_says_so(caplog):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    _edge(gate, clock, 2.2)  # before the radar was armed: cannot match
    gate.note_ops_armed(EDGE_EPOCH + 2.4)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.note_ops_dump_started(EDGE_EPOCH + 2.6, POST_MS)
        _edge(gate, clock, 3.5)
        gate.note_ops_rearm_started(EDGE_EPOCH + 4.5)
        gate.note_ops_armed(EDGE_EPOCH + 4.9, EDGE_EPOCH + 4.7)
    assert [fields["gate_edge_sequence"] for fields in lost] == [2]
    assert lost[0]["ops_busy_start_edge_sequence"] is None
    assert lost[0]["ops_busy_started_by"] == "dump"
    assert "busy since an unmatched dump" in caplog.text
    assert "a re-arm" not in caplog.text


def _records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for log in path.glob("*.jsonl")
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


class FakeRadar:
    """Radar double that re-arms and stamps its re-arm times like OPS243Radar."""

    def __init__(self):
        self.last_hardware_trigger_first_byte_timestamp = None
        self.last_rearm_drain_end_timestamp = None
        self.last_rearm_armed_timestamp = None
        self.next_rearm = None
        self.result = True

    def rearm_rolling_buffer(self, _segments):
        if self.result:
            self.last_rearm_drain_end_timestamp, self.last_rearm_armed_timestamp = self.next_rearm
        return self.result


def test_sound_trigger_and_monitor_log_ops_busy_events(tmp_path, monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)
    monitor = RollingBufferMonitor(port=None, pre_trigger_segments=8)
    started = Mock()
    monitor.trigger.set_dump_callbacks(started=started)
    monitor.set_trigger_gate(gate)
    radar = FakeRadar()
    trigger = monitor.trigger

    start = _edge(gate, clock, 0.0)
    radar.last_hardware_trigger_first_byte_timestamp = _first_byte(start)
    trigger._first_byte_hook(radar, None)()
    started.assert_called_once_with(_first_byte(start))
    _edge(gate, clock, 1.0)
    monkeypatch.setattr(
        "openflight.rolling_buffer.trigger.time.time", lambda: EDGE_EPOCH + 1.9
    )  # re-arm start on the fake clock
    radar.result = False  # re-arm write timed out
    trigger._rearm(radar)
    _edge(gate, clock, 2.3)
    radar.result, radar.next_rearm = True, (EDGE_EPOCH + 2.6, EDGE_EPOCH + 2.8)
    trigger._rearm(radar)
    stats = session._stats.copy()
    session.end_session()

    events = [r for r in _records(tmp_path) if r.get("type") == "trigger_event"]
    assert [event["reason"] for event in events] == ["ops_busy", "ops_busy"]
    assert [event["gate_edge_sequence"] for event in events] == [2, 3]
    assert [event["ops_busy_phase"] for event in events] == ["dump", "rearm_drain"]
    assert events[1]["ops_busy_ms_left"] == pytest.approx(500.0, abs=0.01)
    assert all(event["accepted"] is False for event in events)
    assert all(event["ops_busy_start_edge_sequence"] == 1 for event in events)
    assert stats["triggers_ops_busy"] == 2
    assert stats.get("triggers_total", 0) == 0


class DumpRadar(FakeRadar):
    """Radar double whose trigger wait returns one dump."""

    def __init__(self, events):
        super().__init__()
        self.events = events
        self.next_rearm = (10.0, 10.2)
        self.last_hardware_trigger_lost_capture_bytes = 0

    def wait_for_hardware_trigger(self, **_kwargs):
        return "garbage"

    def rearm_rolling_buffer(self, segments):
        self.events.append("rearm")
        return super().rearm_rolling_buffer(segments)


def test_sound_trigger_reports_the_dump_end_before_processing_and_rearm(monkeypatch):
    events = []
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_busy_callbacks(
        dump_finished=lambda ts: events.append(("dump_finished", ts)),
        rearm_started=lambda ts: events.append(("rearm_started", ts)),
    )
    monkeypatch.setattr("openflight.rolling_buffer.trigger.time.time", lambda: 42.0)
    radar = DumpRadar(events)
    processor = Mock()
    processor.parse_capture.side_effect = lambda *_a, **_k: events.append("parse")

    assert trigger.wait_for_trigger(radar, processor, timeout=0.1) is None
    assert events == [("dump_finished", 42.0), "parse", ("rearm_started", 42.0), "rearm"]


def test_manual_recovery_closes_the_gate_busy_period(monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    monitor = RollingBufferMonitor(port=None, pre_trigger_segments=8)
    monitor.set_trigger_gate(gate)
    gate.set_ops_busy_listener(lost.append)
    start = _edge(gate, clock, 0.0)
    gate.note_ops_dump_started(_first_byte(start), POST_MS)
    _edge(gate, clock, 1.0)
    monitor.radar.last_rearm_armed_timestamp = EDGE_EPOCH + 5.0
    monitor._note_gate_armed_after_recovery()
    assert [fields["ops_busy_ended_by"] for fields in lost] == ["manual_recovery"]
    assert lost[0]["ops_busy_ms_left"] == pytest.approx(4000.0)


# --- 3. late first byte (edge #111) and gate fields of rejected dumps ---------


def test_dump_queued_during_the_last_settle_matches_its_pulse_late(caplog):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    start = _edge(gate, clock, 0.0)
    gate.note_ops_dump_started(_first_byte(start), POST_MS)
    gate.note_ops_rearm_started(EDGE_EPOCH + 1.9)
    _edge(gate, clock, 2.2)  # during the re-arm commands: no dump, lost
    gate.note_ops_armed(EDGE_EPOCH + 2.4, EDGE_EPOCH + 2.1)
    late_edge = _edge(gate, clock, 2.476)  # during the final PA settle
    first_byte = late_edge + 0.270  # read after the settle and shot processing

    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        fields = gate.fields_for_dump(first_byte, POST_MS)
    assert fields["gate_decision"] == "passthrough"
    assert fields["gate_first_byte_match"] == "late"
    assert fields["gate_edge_timestamp"] == late_edge
    assert fields["gate_first_byte_lag_ms"] == pytest.approx(270.0 - POST_MS, abs=0.01)
    assert "matched edge #3 late" in caplog.text
    assert [lost_fields["gate_edge_sequence"] for lost_fields in lost] == [2]

    # Its own busy period starts at the late pulse; the dump is not "lost".
    gate.note_ops_dump_started(first_byte, POST_MS)
    _edge(gate, clock, 3.0)
    gate.note_ops_armed(EDGE_EPOCH + 4.9)
    assert lost[-1]["ops_busy_start_edge_sequence"] == 3
    assert [lost_fields["gate_edge_sequence"] for lost_fields in lost] == [2, 4]
    # After its own re-arm the same dump still matches (floor before its first byte).
    assert gate.fields_for_dump(first_byte, POST_MS)["gate_edge_timestamp"] == late_edge


def test_late_match_ignores_pulses_before_the_radar_was_armed():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    _edge(gate, clock, 2.2)  # PA / S# / PA: the radar is not armed yet
    gate.note_ops_armed(EDGE_EPOCH + 2.4, EDGE_EPOCH + 2.1)
    first_byte = EDGE_EPOCH + 2.2 + 0.4
    fields = gate.fields_for_dump(first_byte, POST_MS)
    assert fields["gate_decision"] == "unmatched"
    assert fields["gate_first_byte_match"] == "none"
    assert fields["gate_nearest_edge_sequence"] == 1
    assert fields["gate_nearest_edge_timestamp"] == EDGE_EPOCH + 2.2
    assert fields["gate_nearest_first_byte_lag_ms"] == pytest.approx(400.0 - POST_MS, abs=0.01)


def test_late_match_without_a_rearm_takes_the_last_pulse_before_the_first_byte():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    _edge(gate, clock, 0.0)
    second = _edge(gate, clock, 0.3)
    first_byte = second + 0.250
    record = gate.record_for_capture(first_byte, POST_MS)
    assert record.sequence == 2 and record.first_byte_match == "late"


def test_late_match_is_bounded():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    edge = _edge(gate, clock, 0.0)
    assert gate.record_for_capture(edge + (POST_MS + 999.0) / 1000.0, POST_MS) is not None
    assert gate.record_for_capture(edge + (POST_MS + 1001.0) / 1000.0, POST_MS) is None


def test_rejected_late_dump_event_carries_the_gate_record(tmp_path, monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)
    monitor = RollingBufferMonitor(port=None, pre_trigger_segments=8)
    monitor.set_trigger_gate(gate)
    edge = _edge(gate, clock, 0.0)
    monitor._record_trigger_event(
        {"capture_first_byte_timestamp": edge + 0.270},
        accepted=False,
        reason="no_outbound_speed",
    )
    session.end_session()
    event = next(r for r in _records(tmp_path) if r.get("type") == "trigger_event")
    assert event["gate_decision"] == "passthrough"
    assert event["gate_first_byte_match"] == "late"
    assert event["gate_edge_timestamp"] == edge


def test_parse_failed_dump_keeps_its_first_byte_for_the_gate_fields():
    trigger = SoundTrigger(pre_trigger_segments=8)
    radar = FakeRadar()
    radar.next_rearm = (1.0, 2.0)
    radar.last_hardware_trigger_first_byte_timestamp = 123.5
    processor = Mock()
    processor.parse_capture.return_value = None
    assert trigger._capture_from_dump(radar, processor, "garbage") is None
    (diagnostic,) = trigger.drain_diagnostics()
    assert diagnostic["reason"] == "parse_failed"
    assert diagnostic["capture_first_byte_timestamp"] == 123.5


# --- 4. the OPS speed trigger (B2): the microphone is only a witness ------------------


def _speed_witness(gate, clock: FakeClock, sink: list):
    from openflight.ops_speed import OPS_STATE_ARMED, OPS_STATE_BUSY, OpsSpeedWitness

    return OpsSpeedWitness(
        sink.append,
        ops_state=lambda: OPS_STATE_BUSY if gate.ops_busy_now() else OPS_STATE_ARMED,
        wall_time=clock.wall,
        monotonic_ns=clock.monotonic_ns,
    )


def test_witness_microphone_edge_in_ops_busy_time_is_mic_without_pin_busy():
    clock = FakeClock()
    gate, output = make_gate(clock, edge_source="ops-speed")
    lost, witnessed = [], []
    gate.set_ops_busy_listener(lost.append)
    witness = _speed_witness(gate, clock, witnessed)

    trip = _edge(gate, clock, 0.0)  # the speed trip on pin 1
    gate.note_ops_dump_started(_first_byte(trip), POST_MS)
    assert gate.ops_busy_now()
    clock.at_ms(1500.0)  # a neighbour's impact while the dump streams
    witness.note_mic_edge(EDGE_EPOCH + 1.5)
    gate.note_ops_rearm_started(EDGE_EPOCH + 1.9)
    gate.note_ops_armed(EDGE_EPOCH + 2.4, EDGE_EPOCH + 2.1)
    witness.flush(everything=True)

    assert output.pulses == [] and lost == [], "no pulse, so nothing is lost to busy time"
    assert gate.summary()["ops_busy"] == 0
    mic = [fields for fields in witnessed if fields["kind"] == "mic_without_pin"]
    assert len(mic) == 1 and mic[0]["ops_state"] == "busy"
    assert not gate.ops_busy_now()


def test_speed_trip_opens_the_busy_period_like_a_pulse():
    clock = FakeClock()
    gate, _ = make_gate(clock, edge_source="ops-speed")
    lost = []
    gate.set_ops_busy_listener(lost.append)
    trip = _edge(gate, clock, 0.0)
    gate.note_ops_dump_started(_first_byte(trip), POST_MS)
    # A HOST_INT pulse in the shadow (or a second trip) inside it is lost as today.
    _edge(gate, clock, 1.0)
    gate.note_ops_armed(EDGE_EPOCH + 2.4)
    assert [fields["ops_busy_start_edge_sequence"] for fields in lost] == [1]
    assert lost[0]["gate_decision"] == "ops_speed"


def test_witness_microphone_edge_between_the_trip_and_its_first_byte_is_busy():
    clock = FakeClock()
    gate, _ = make_gate(clock, edge_source="ops-speed", ops_speed_post_trigger_ms=POST_MS)
    witnessed = []
    witness = _speed_witness(gate, clock, witnessed)

    trip = _edge(gate, clock, 0.0)
    clock.at_ms(100.0)  # a neighbour's impact; the trip's dump has not arrived yet
    witness.note_mic_edge(EDGE_EPOCH + 0.100)
    gate.note_ops_dump_started(_first_byte(trip), POST_MS)
    gate.note_ops_rearm_started(EDGE_EPOCH + 1.9)
    gate.note_ops_armed(EDGE_EPOCH + 2.4, EDGE_EPOCH + 2.1)
    clock.at_ms(2500.0)  # armed again
    witness.note_mic_edge(EDGE_EPOCH + 2.5)
    witness.flush(everything=True)

    mic = [fields for fields in witnessed if fields["kind"] == "mic_without_pin"]
    assert [fields["ops_state"] for fields in mic] == ["busy", "armed"]


def test_trip_whose_dump_never_came_stops_counting_as_busy_after_the_match_window():
    clock = FakeClock()
    gate, _ = make_gate(clock, edge_source="ops-speed", ops_speed_post_trigger_ms=POST_MS)
    trip = _edge(gate, clock, 0.0)
    horizon_s = (POST_MS + 60.0) / 1000.0  # post-trigger span + the first-byte window
    assert gate.ops_busy_now(trip + horizon_s - 0.001)
    assert not gate.ops_busy_now(trip + horizon_s + 0.001)
    assert not gate.ops_busy_now(trip), "a trip at the edge itself does not count"
    # Without the post-trigger span the busy period starts at the first byte, as in sound mode.
    plain, _ = make_gate(FakeClock(), edge_source="ops-speed")
    _edge(plain, clock, 0.0)
    assert not plain.ops_busy_now(trip + 0.1)


# --- 5. shadow: a speed trip pin 1 never showed (loose or unwired J3 pin 1) -------------

S2_POST_MS = 192.0  # S#2 at 20 ksps


def _unseen_trip(mic_after_ms: float, uart_lag_ms: float):
    """The OPS trips itself at EDGE_EPOCH, the Pi sees no pin edge; the microphone hears
    the swing ``mic_after_ms`` later and the shadow pulses HOST_INT (ignored by the busy
    board). Returns the gate and the dump's first byte."""
    clock = FakeClock()
    gate, _ = make_gate(clock, ops_speed_shadow=True)
    _edge(gate, clock, mic_after_ms / 1000.0)
    return gate, EDGE_EPOCH + (S2_POST_MS + uart_lag_ms) / 1000.0


@pytest.mark.parametrize(
    ("mic_after_ms", "uart_lag_ms", "kind"),
    [
        (26.0, 17.0, "self_trip"),  # first byte 9 ms "before" the pulse's span
        (26.0, 26.6, "self_trip"),  # the longest UART lag seen (26.63 ms, 30.10.)
        (26.0, 4.0, "no_record"),  # outside the match window: no record at all
    ],
)
def test_unseen_trip_is_never_a_fallback_shot_anchored_at_its_trigger_point(
    mic_after_ms, uart_lag_ms, kind
):
    gate, first_byte = _unseen_trip(mic_after_ms, uart_lag_ms)

    assert gate.classify_ops_speed_dump(first_byte, S2_POST_MS) == kind
    fields = gate.fields_for_dump(first_byte, S2_POST_MS)
    assert fields["gate_decision"] == "unmatched"
    # Anchored 28 ms after the buffer's trigger point, not at the fallback pulse (+0 ms).
    assert fields["sound_edge_delay_ms"] == -28.0
    if kind == "self_trip":
        assert fields["gate_first_byte_match"] == "self_trip"
        assert fields["gate_nearest_edge_sequence"] == 1
        assert fields["gate_nearest_first_byte_lag_ms"] == pytest.approx(
            uart_lag_ms - mic_after_ms, abs=0.01
        )


def test_self_trip_capture_anchors_after_its_trigger_point(caplog):
    gate, first_byte = _unseen_trip(26.0, 17.0)
    capture = SimpleNamespace(
        first_byte_timestamp=first_byte,
        post_trigger_duration_ms=S2_POST_MS,
        sound_edge_delay_ms=0.0,
        sound_edge_offset_ms=None,
    )
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate.apply_to_capture(capture)
    assert capture.sound_edge_delay_ms == -28.0
    assert "the OPS tripped itself and pin 1 was not seen" in caplog.text


def test_host_int_dump_with_the_shortest_lag_seen_stays_a_fallback_shot():
    # The OPS was armed and HOST_INT started the dump: 1.15 ms is the shortest lag of 197 shots.
    gate, first_byte = _unseen_trip(0.0, 1.15)
    assert gate.classify_ops_speed_dump(first_byte, S2_POST_MS) == "fallback"
    fields = gate.fields_for_dump(first_byte, S2_POST_MS)
    assert fields["gate_first_byte_match"] == "window"
    assert fields["sound_edge_delay_ms"] == pytest.approx(0.0)


def test_unseen_trip_with_a_uart_lag_above_the_mic_delay_still_reads_as_a_fallback():
    """Known limit: lag = UART lag - mic delay >= 1 ms looks like a HOST_INT dump."""
    gate, first_byte = _unseen_trip(20.0, 30.0)
    assert gate.classify_ops_speed_dump(first_byte, S2_POST_MS) == "fallback"


def test_fallback_pulse_echoed_by_pin_1_started_its_dump_whatever_the_lag():
    clock = FakeClock()
    gate, _ = make_gate(clock, ops_speed_shadow=True)
    pulse = _edge(gate, clock, 0.0)
    clock.at_ms(1.0)
    assert not gate.note_ops_speed_edge(pulse + 0.001)  # pin 1 rose with the pulse's dump
    first_byte = pulse + (S2_POST_MS + 0.5) / 1000.0
    assert gate.classify_ops_speed_dump(first_byte, S2_POST_MS) == "fallback"


def test_self_trip_fallback_pulse_counts_as_lost_to_the_busy_period():
    gate, first_byte = _unseen_trip(26.0, 17.0)
    lost = []
    gate.set_ops_busy_listener(lost.append)
    gate.note_ops_dump_started(first_byte, S2_POST_MS)
    gate.note_ops_armed(EDGE_EPOCH + 2.4)
    assert len(lost) == 1
    assert lost[0]["gate_edge_sequence"] == 1 and lost[0]["ops_speed_fallback"] is True
    assert lost[0]["ops_busy_start_edge_sequence"] is None
