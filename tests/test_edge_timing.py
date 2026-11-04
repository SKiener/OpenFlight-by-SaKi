"""Kernel sound-edge timestamps logged next to the callback stamps (shadow only)."""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from unittest.mock import Mock

import pytest

from openflight.edge_timing import (
    FIELD_NAMES,
    MAX_LATENCY_S,
    RECORD_HISTORY,
    EdgeTimingLog,
    edge_ticks,
    null_fields,
)
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.session_logger import SessionLogger
from openflight.trigger_gate import EdgeListener, TriggerGate
from tests.test_trigger_chain import BlockingRadar
from tests.test_trigger_gate import EDGE_EPOCH, EDGE_NS, FakeClock, make_gate

WALL = EDGE_EPOCH + 0.0004
MONO_NS = EDGE_NS + 400_000


class GpiozeroButton:
    """Button double that dispatches like gpiozero: ``_last_changed`` first, then the callback."""

    def __init__(self, pin, pull_up, bounce_time):
        self.pin = pin
        self.when_pressed = None
        self._last_changed = 0.0
        self.closed = False

    def fire(self, ticks):
        self._last_changed = ticks
        self.when_pressed()

    def close(self):
        self.closed = True


class StampFreeButton:
    """A pin without kernel ticks (mock mode, non-lgpio factory)."""

    def __init__(self, pin, pull_up, bounce_time):
        self.when_pressed = None

    def close(self):
        pass


def _record(log, ticks, *, edge=WALL, wall=WALL, mono=MONO_NS):
    return log.record(edge, callback_timestamp=wall, callback_monotonic_ns=mono, ticks=ticks)


# --- clock-domain decision -------------------------------------------------


def test_realtime_nanosecond_ticks():
    log = EdgeTimingLog()
    timing = _record(log, round((WALL - 0.00125) * 1e9))
    assert log.clock_decision == ("realtime", "ns")
    assert timing.kernel_clock == "realtime"
    assert timing.latency_ms == pytest.approx(1.25, abs=1e-3)
    assert timing.kernel_timestamp == pytest.approx(WALL - 0.00125, abs=1e-6)


def test_monotonic_second_ticks_as_gpiozero_hands_them_over():
    log = EdgeTimingLog()
    timing = _record(log, (MONO_NS - 800_000) / 1e9)
    assert log.clock_decision == ("monotonic", "s")
    assert timing.latency_ms == pytest.approx(0.8, abs=1e-6)
    # Converted to epoch through the callback's own wall/monotonic pair.
    assert timing.kernel_timestamp == pytest.approx(WALL - 0.0008, abs=1e-9)


@pytest.mark.parametrize(
    ("ticks", "decision"),
    [
        ((MONO_NS - 2_000_000) // 1000, ("monotonic", "us")),
        ((MONO_NS - 2_000_000) // 1_000_000, ("monotonic", "ms")),
        (MONO_NS - 2_000_000, ("monotonic", "ns")),
        (WALL - 0.002, ("realtime", "s")),
    ],
)
def test_every_unit_and_clock_is_recognised(ticks, decision):
    log = EdgeTimingLog()
    timing = _record(log, ticks)
    assert log.clock_decision == decision
    assert 1.0 <= timing.latency_ms <= 3.0


def test_decision_is_logged_once_and_reused(caplog):
    log = EdgeTimingLog()
    with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
        for index in range(3):
            offset_s = index * 0.5
            _record(
                log,
                (MONO_NS - 500_000) / 1e9 + offset_s,
                edge=WALL + offset_s,
                wall=WALL + offset_s,
                mono=MONO_NS + round(offset_s * 1e9),
            )
    decisions = [r for r in caplog.records if "Kernel edge stamps" in r.getMessage()]
    assert len(decisions) == 1
    assert "CLOCK_MONOTONIC" in decisions[0].getMessage()
    assert log.summary()["with_kernel"] == 3


def test_decision_is_rederived_when_the_ticks_change_clock(caplog):
    log = EdgeTimingLog()
    _record(log, (MONO_NS - 500_000) / 1e9)
    with caplog.at_level(logging.WARNING, logger="openflight.edge_timing"):
        timing = _record(log, WALL + 0.5 - 0.0007, edge=WALL + 0.5, wall=WALL + 0.5)
    assert log.clock_decision == ("realtime", "s")
    assert timing.latency_ms == pytest.approx(0.7, abs=1e-3)
    assert any("moved from monotonic/s to realtime/s" in r.getMessage() for r in caplog.records)


# --- missing, stale and implausible stamps ---------------------------------


def test_missing_ticks_keep_the_callback_fields_and_null_kernel_fields(caplog):
    log = EdgeTimingLog()
    with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
        _record(log, None)
        _record(log, None, edge=WALL + 1.0, wall=WALL + 1.0)
    fields = log.fields_for(WALL)
    assert fields == {
        "edge_kernel_timestamp": None,
        "edge_kernel_clock": None,
        "edge_callback_timestamp": WALL,
        "edge_callback_monotonic_ns": MONO_NS,
        "edge_callback_latency_ms": None,
    }
    assert sum("No kernel edge stamp" in r.getMessage() for r in caplog.records) == 1
    summary = log.summary()
    assert (summary["edges"], summary["missing"], summary["with_kernel"]) == (2, 2, 0)
    assert summary["p50_ms"] is None and summary["clock"] is None


@pytest.mark.parametrize(
    "ticks",
    [
        WALL + 0.002,  # after the callback: impossible
        WALL - MAX_LATENCY_S - 0.1,  # older than any dispatch latency
        123.0e12,  # fits no unit on either clock
    ],
)
def test_implausible_ticks_give_null_kernel_fields(ticks, caplog):
    log = EdgeTimingLog()
    with caplog.at_level(logging.WARNING, logger="openflight.edge_timing"):
        timing = _record(log, ticks)
    assert timing.kernel_timestamp is None and timing.latency_ms is None
    assert log.summary()["rejected"] == 1
    assert any("fit neither clock" in r.getMessage() for r in caplog.records)


def test_ticks_repeating_the_previous_edge_are_stale():
    log = EdgeTimingLog()
    ticks = (MONO_NS - 500_000) / 1e9
    assert _record(log, ticks).latency_ms is not None
    later = _record(log, ticks, edge=WALL + 0.2, wall=WALL + 0.2, mono=MONO_NS + 200_000_000)
    assert later.latency_ms is None and later.kernel_timestamp is None
    assert log.summary()["rejected"] == 1


def test_small_negative_latency_from_clock_read_order_is_kept():
    log = EdgeTimingLog()
    timing = _record(log, WALL + 0.0002)
    assert timing.latency_ms == pytest.approx(-0.2, abs=1e-3)


@pytest.mark.parametrize("value", [None, True, "1.0", math.nan, math.inf])
def test_edge_ticks_ignores_non_numeric_stamps(value):
    button = StampFreeButton(17, False, None)
    button._last_changed = value
    assert edge_ticks(button) is None


def test_edge_ticks_reads_gpiozero_last_changed():
    button = GpiozeroButton(17, False, None)
    button._last_changed = 12.5
    assert edge_ticks(button) == 12.5
    assert edge_ticks(None) is None
    assert edge_ticks(StampFreeButton(17, False, None)) is None


# --- lookup and summary ----------------------------------------------------


def test_fields_for_unknown_or_missing_edges_are_null():
    log = EdgeTimingLog()
    _record(log, WALL - 0.001)
    assert log.fields_for(None) == null_fields()
    assert log.fields_for(WALL + 0.01) == null_fields()
    assert set(log.fields_for(WALL)) == set(FIELD_NAMES)


def test_summary_percentiles_and_log_line(caplog):
    log = EdgeTimingLog()
    latencies_ms = [0.2, 0.4, 0.6, 0.8, 5.0]
    for index, latency_ms in enumerate(latencies_ms):
        wall = WALL + index
        mono = MONO_NS + index * 1_000_000_000
        _record(log, (mono - round(latency_ms * 1e6)) / 1e9, edge=wall, wall=wall, mono=mono)
    _record(log, None, edge=WALL + 10, wall=WALL + 10)
    summary = log.summary()
    assert summary["edges"] == 6 and summary["with_kernel"] == 5 and summary["missing"] == 1
    assert summary["p50_ms"] == pytest.approx(0.6, abs=1e-4)
    assert summary["p95_ms"] == pytest.approx(0.8 + 0.8 * 4.2, abs=1e-3)
    assert summary["max_ms"] == pytest.approx(5.0, abs=1e-4)
    with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
        log.log_summary("BCM17 (test)")
    line = caplog.records[-1].getMessage()
    assert line.startswith("[EDGE] BCM17 (test) session summary: 6 edges, 5 with kernel time")
    assert "n=5 p50 0.600 ms" in line and "max 5.000 ms" in line and "CLOCK_MONOTONIC" in line


# --- edge sources ----------------------------------------------------------


def test_listener_records_after_the_gate_pulsed_and_keeps_the_edge_time(caplog):
    clock = FakeClock()
    gate, output = make_gate(clock)
    seen = []

    def pulse_watch(edge):
        # Ahead of the gate only the raw stamps were queued: nothing resolved.
        seen.append(("gate", len(listener.edge_timing._pending), listener.edge_timing._edges))
        gate.on_edge(edge)

    def camera(edge):
        # The gate pulsed already; the edge is findable while observers run.
        seen.append((edge, len(output.pulses), listener.edge_timing.fields_for(edge)))

    listener = EdgeListener(
        pin=17,
        observers=[pulse_watch, camera],
        button_factory=GpiozeroButton,
        wall_time=clock.wall,
        monotonic_ns=clock.monotonic_ns,
    )
    listener.start()
    listener.arm()
    kernel_mono_s = EDGE_NS / 1e9
    listener._button.fire(kernel_mono_s)

    callback_wall = clock.wall()
    fields = listener.edge_timing.fields_for(callback_wall)
    assert seen == [("gate", 1, 0), (callback_wall, 1, fields)]
    assert only_edge(gate) == callback_wall
    assert fields["edge_callback_timestamp"] == callback_wall
    assert fields["edge_callback_monotonic_ns"] == clock.ns
    assert fields["edge_kernel_clock"] == "monotonic"
    assert fields["edge_callback_latency_ms"] == pytest.approx(0.3, abs=1e-6)
    assert fields["edge_kernel_timestamp"] == pytest.approx(EDGE_EPOCH, abs=1e-6)

    with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
        listener.stop()
    gate.stop()
    assert any(
        "BCM17 (trigger gate input) session summary" in r.getMessage() for r in caplog.records
    )


def only_edge(gate: TriggerGate) -> float:
    records = list(gate._records)
    assert len(records) == 1
    return records[0].edge_timestamp


def test_listener_ringing_edges_are_not_recorded():
    clock = FakeClock()
    listener = EdgeListener(
        pin=17,
        observers=[],
        button_factory=GpiozeroButton,
        wall_time=clock.wall,
        monotonic_ns=clock.monotonic_ns,
    )
    listener.start()
    listener.arm()
    listener._button.fire(EDGE_NS / 1e9)
    clock.advance_ms(50.0)
    listener._button.fire(EDGE_NS / 1e9 + 0.0499)
    assert listener.edge_timing.summary()["edges"] == 1
    listener.stop()


def test_listener_without_stamps_logs_null_kernel_fields_in_the_gate_dump_fields():
    clock = FakeClock()
    timing = EdgeTimingLog()
    gate, output = make_gate(clock, edge_timing=timing)
    listener = EdgeListener(
        pin=17,
        observers=[gate.on_edge],
        button_factory=StampFreeButton,
        wall_time=clock.wall,
        monotonic_ns=clock.monotonic_ns,
        edge_timing=timing,
    )
    listener.start()
    listener.arm()
    assert listener.notify_edge()
    gate.run_pending()
    pulse = EDGE_EPOCH + output.pulses[0] / 1000.0
    fields = gate.fields_for_dump(pulse + (68.3 + 17.0) / 1000.0, 68.3)
    assert fields["gate_decision"] == "passthrough"
    assert fields["edge_kernel_timestamp"] is None
    assert fields["edge_callback_latency_ms"] is None
    assert fields["edge_callback_timestamp"] == clock.wall()
    listener.stop()
    gate.stop()


def test_gate_dump_fields_carry_the_kernel_stamp_and_keep_the_gate_fields():
    clock = FakeClock()
    timing = EdgeTimingLog()
    gate, output = make_gate(clock, edge_timing=timing)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    timing.record(
        EDGE_EPOCH,
        callback_timestamp=EDGE_EPOCH,
        callback_monotonic_ns=EDGE_NS,
        ticks=EDGE_EPOCH - 0.0011,
    )
    pulse = EDGE_EPOCH + output.pulses[0] / 1000.0
    fields = gate.fields_for_dump(pulse + (68.3 + 17.0) / 1000.0, 68.3)
    assert fields["gate_edge_timestamp"] == EDGE_EPOCH
    assert fields["sound_edge_delay_ms"] == pytest.approx(0.3, abs=0.01)
    assert fields["edge_kernel_clock"] == "realtime"
    assert fields["edge_callback_latency_ms"] == pytest.approx(1.1, abs=1e-3)
    gate.stop()


def test_gate_without_edge_timing_keeps_its_dump_fields_unchanged():
    clock = FakeClock()
    gate, output = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    pulse = EDGE_EPOCH + output.pulses[0] / 1000.0
    fields = gate.fields_for_dump(pulse + (68.3 + 17.0) / 1000.0, 68.3)
    assert not set(fields) & set(FIELD_NAMES)
    gate.stop()


def test_ti_monitor_records_kernel_latency_after_its_observers(tmp_path, caplog):
    radar = BlockingRadar()
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    seen = []
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=GpiozeroButton,
        cancel_false_triggers=True,
        trigger_observers=[lambda edge: seen.append((edge, monitor.edge_timing.fields_for(edge)))],
    )
    monitor.start()
    try:
        before = time.time()
        monitor._button.fire(time.monotonic() - 0.002)
        after = time.time()
    finally:
        radar.release.set()
        with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
            monitor.stop()

    ((edge, during),) = seen
    assert before <= edge <= after
    fields = monitor.edge_timing.fields_for(edge)
    assert during == fields
    assert fields["edge_callback_timestamp"] == edge
    assert fields["edge_kernel_clock"] == "monotonic"
    assert 2.0 <= fields["edge_callback_latency_ms"] < 100.0
    assert fields["edge_kernel_timestamp"] == pytest.approx(
        edge - fields["edge_callback_latency_ms"] / 1000.0, abs=1e-9
    )
    assert any("BCM17 (TI monitor) session summary" in r.getMessage() for r in caplog.records)


def test_ti_monitor_keeps_an_explicit_edge_timestamp():
    log = EdgeTimingLog()
    monitor = IWR6843CaptureMonitor.__new__(IWR6843CaptureMonitor)
    seen = []
    monitor._running = monitor._armed = True
    monitor._button = None
    monitor._last_observed_edge_timestamp = 0.0
    monitor._trigger_observers = [seen.append]
    monitor._queue_dump_locked = lambda _edge: False
    monitor._condition = threading.Condition()
    monitor.edge_timing = log
    monitor.notify_trigger(EDGE_EPOCH)
    assert seen == [EDGE_EPOCH]
    fields = log.fields_for(EDGE_EPOCH)
    assert fields["edge_kernel_timestamp"] is None
    assert fields["edge_callback_timestamp"] is not None


def test_camera_capture_entry_carries_the_edge_fields(tmp_path):
    session_logger = SessionLogger(log_dir=tmp_path, enabled=True)
    session_logger.start_session()
    fields = {**null_fields(), "edge_callback_timestamp": EDGE_EPOCH}
    session_logger.log_camera_capture(
        shot_number=1,
        shot_timestamp=EDGE_EPOCH,
        trigger_timestamp=EDGE_EPOCH,
        capture_path=None,
        edge_fields=fields,
    )
    session_logger.log_camera_capture(
        shot_number=2, shot_timestamp=None, trigger_timestamp=None, capture_path=None
    )
    session_logger.end_session()
    entries = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    captures = [entry for entry in entries if entry["type"] == "camera_capture"]
    assert captures[0]["edge_callback_timestamp"] == EDGE_EPOCH
    assert captures[0]["trigger_timestamp"] == EDGE_EPOCH
    assert "edge_callback_timestamp" not in captures[1]


# --- stalls after the decision (review fix) --------------------------------


def test_a_stalled_callback_after_the_decision_keeps_its_real_latency():
    log = EdgeTimingLog()
    _record(log, (MONO_NS - 500_000) / 1e9)
    # The GPIO thread was blocked (e.g. a camera reconfigure): 1.5 s late.
    later_s = 3.0
    stalled = _record(
        log,
        (MONO_NS + round(later_s * 1e9)) / 1e9 - 1.5,
        edge=WALL + later_s,
        wall=WALL + later_s,
        mono=MONO_NS + round(later_s * 1e9),
    )
    assert stalled.kernel_clock == "monotonic"
    assert stalled.latency_ms == pytest.approx(1500.0, abs=1e-3)
    assert stalled.kernel_timestamp == pytest.approx(WALL + later_s - 1.5, abs=1e-6)
    summary = log.summary()
    assert summary["rejected"] == 0
    assert summary["max_ms"] == pytest.approx(1500.0, abs=1e-3)
    assert "max 1500.000 ms" in log.summary_text()


def test_a_first_edge_needs_the_one_second_window_to_decide():
    log = EdgeTimingLog()
    timing = _record(log, (MONO_NS / 1e9) - 1.5)
    assert timing.latency_ms is None and log.clock_decision is None


def test_beyond_the_decided_bound_an_edge_is_rejected_and_warned_about(caplog):
    log = EdgeTimingLog()
    _record(log, (MONO_NS - 500_000) / 1e9)
    with caplog.at_level(logging.WARNING, logger="openflight.edge_timing"):
        timing = _record(
            log,
            (MONO_NS / 1e9) + 0.1 - 61.0,
            edge=WALL + 0.1,
            wall=WALL + 0.1,
            mono=MONO_NS + 10**8,
        )
    assert timing.latency_ms is None
    assert log.clock_decision == ("monotonic", "s")
    assert any("CLOCK_MONOTONIC/s within 60 s" in r.getMessage() for r in caplog.records)


def test_stale_and_no_fit_rejections_are_each_warned_once(caplog):
    log = EdgeTimingLog()
    ticks = (MONO_NS - 500_000) / 1e9
    with caplog.at_level(logging.WARNING, logger="openflight.edge_timing"):
        _record(log, ticks)
        for index in (1, 2):  # two stale repeats, one warning
            offset = index * 0.2
            _record(log, ticks, edge=WALL + offset, wall=WALL + offset, mono=MONO_NS)
        for index in (3, 4):  # two impossible stamps, one warning
            offset = index * 0.2
            _record(log, ticks + 10.0 * index, edge=WALL + offset, wall=WALL + offset)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "repeat the previous edge" in warnings[0]
    assert "fit neither" in warnings[1]
    assert log.summary()["rejected"] == 4


# --- an observer blocking the callback thread (review fix) -----------------


def test_dump_fields_find_the_edge_while_a_later_observer_blocks():
    clock = FakeClock()
    timing = EdgeTimingLog()
    gate, output = make_gate(clock, edge_timing=timing)
    blocked = threading.Event()
    release = threading.Event()

    def camera(_edge):
        blocked.set()
        assert release.wait(5.0)

    listener = EdgeListener(
        pin=17,
        observers=[gate.on_edge, camera],
        button_factory=GpiozeroButton,
        wall_time=clock.wall,
        monotonic_ns=clock.monotonic_ns,
        edge_timing=timing,
    )
    listener.start()
    listener.arm()
    callback = threading.Thread(target=listener._button.fire, args=(EDGE_NS / 1e9,))
    callback.start()
    try:
        assert blocked.wait(5.0)
        gate.run_pending()
        pulse = EDGE_EPOCH + output.pulses[0] / 1000.0
        fields = gate.fields_for_dump(pulse + (68.3 + 17.0) / 1000.0, 68.3)
        assert fields["edge_callback_timestamp"] == clock.wall()
        assert fields["edge_kernel_clock"] == "monotonic"
        assert fields["edge_callback_latency_ms"] == pytest.approx(0.3, abs=1e-6)
    finally:
        release.set()
        callback.join(5.0)
    # Resolved once: the flush after the observers finds nothing left to do.
    assert timing.summary()["edges"] == 1
    listener.stop()
    gate.stop()


def test_note_resolves_edges_in_arrival_order_once():
    log = EdgeTimingLog()
    first = (MONO_NS - 500_000) / 1e9
    log.note(WALL, callback_timestamp=WALL, callback_monotonic_ns=MONO_NS, ticks=first)
    log.note(WALL + 0.2, callback_timestamp=WALL + 0.2, callback_monotonic_ns=MONO_NS, ticks=first)
    assert log.fields_for(WALL)["edge_callback_latency_ms"] == pytest.approx(0.5, abs=1e-6)
    # The second edge repeats the first one's ticks: stale, in arrival order.
    assert log.fields_for(WALL + 0.2)["edge_kernel_timestamp"] is None
    log.flush()
    assert log.summary()["edges"] == 2 and log.summary()["rejected"] == 1


# --- camera-owned sound input (review fix) ---------------------------------


def _camera_runtime(tmp_path, edge_timing):
    from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings

    runtime = CameraCaptureRuntime(
        output_dir=tmp_path,
        settings=CameraCaptureSettings(auto_exposure=False),
        button_factory=GpiozeroButton,
        edge_timing=edge_timing,
    )
    runtime.notify_trigger = Mock(return_value=True)
    runtime._start_gpio_trigger()
    return runtime


def test_camera_gpio_trigger_records_the_edge_it_hands_to_notify_trigger(tmp_path, caplog):
    log = EdgeTimingLog()
    runtime = _camera_runtime(tmp_path, log)
    before = time.time()
    runtime._button.fire(time.monotonic() - 0.002)
    after = time.time()
    ((edge,), _kwargs) = runtime.notify_trigger.call_args
    assert before <= edge <= after
    fields = log.fields_for(edge)
    assert fields["edge_callback_timestamp"] == edge
    assert fields["edge_kernel_clock"] == "monotonic"
    assert 2.0 <= fields["edge_callback_latency_ms"] < 100.0
    with caplog.at_level(logging.INFO, logger="openflight.edge_timing"):
        runtime.stop()
    assert any("BCM17 (camera trigger) session summary" in r.getMessage() for r in caplog.records)


def test_camera_gpio_trigger_without_edge_timing_only_notifies(tmp_path):
    runtime = _camera_runtime(tmp_path, None)
    before = time.time()
    runtime._button.fire(123.0)
    ((edge,), _kwargs) = runtime.notify_trigger.call_args
    assert before <= edge <= time.time()
    runtime.stop()


def test_ti_capture_entry_carries_the_edge_fields(tmp_path):
    session_logger = SessionLogger(log_dir=tmp_path, enabled=True)
    session_logger.start_session()
    fields = {**null_fields(), "edge_callback_timestamp": EDGE_EPOCH}
    common = dict(
        capture_path=None,
        capture_bytes=0,
        dump_duration_s=None,
        capture_error=None,
        ball_speed_mph=150.0,
    )
    session_logger.log_iwr6843_capture(
        shot_number=1,
        shot_timestamp=EDGE_EPOCH,
        trigger_timestamp=EDGE_EPOCH,
        edge_fields=fields,
        **common,
    )
    session_logger.log_iwr6843_capture(
        shot_number=2, shot_timestamp=None, trigger_timestamp=None, **common
    )
    session_logger.end_session()
    entries = [
        json.loads(line)
        for path in tmp_path.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]
    captures = [entry for entry in entries if entry["type"] == "iwr6843_capture"]
    assert captures[0]["edge_callback_timestamp"] == EDGE_EPOCH
    assert captures[0]["trigger_timestamp"] == EDGE_EPOCH
    assert "edge_callback_timestamp" not in captures[1]


def test_history_numbers_every_edge_and_keeps_the_newest():
    log = EdgeTimingLog()
    for index in range(RECORD_HISTORY + 3):
        log.note(
            WALL + index,
            callback_timestamp=WALL + index,
            callback_monotonic_ns=MONO_NS + index,
            ticks=None,
        )
    history = log.history()  # flushes the noted edges
    assert len(history) == RECORD_HISTORY
    assert history[0][0] == 4
    assert history[-1][0] == RECORD_HISTORY + 3
    assert history[-1][1].edge_timestamp == WALL + RECORD_HISTORY + 2


# --- the gpiozero dispatch order, against the real library (Pi / Linux) ----


def test_real_gpiozero_sets_the_edge_ticks_before_when_pressed():
    """``edge_ticks`` read in ``when_pressed`` must be the ticks of that very edge."""
    gpiozero = pytest.importorskip("gpiozero")
    from gpiozero.pins.mock import MockFactory  # pylint: disable=import-error

    factory = MockFactory()
    button = gpiozero.Button(17, pull_up=False, bounce_time=None, pin_factory=factory)
    try:
        seen = []
        button.when_pressed = lambda: seen.append(edge_ticks(button))
        factory.ticks = lambda: 4242.5  # the next edge's ticks, distinct from any earlier
        factory.pin(17).drive_high()
        assert seen == [4242.5]
    finally:
        button.close()
        factory.reset()
