"""Tests for GPIO-triggered TI capture and OPS shot correlation."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest
from iwr6843_scripted_cli import PROMPT, scripted_radar

from openflight.iwr6843.driver import DumpCancelled
from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.health import HWA_STALL_SESSION_REASON
from openflight.iwr6843.monitor import (
    HWA_STALL_RECOVERING_REASON,
    TI_RECOVERY_FAILED_SESSION_REASON,
    TI_RESTARTING_REASON,
    IWR6843CaptureMonitor,
)


class FakeRadar:
    """Small transport double with a complete L3 dump."""

    port = "/dev/fake-iwr6843"

    def __init__(self, raw: bytes, error: Exception | None = None):
        self.raw = raw
        self.error = error
        self.configs = []
        self.closed = False
        self.read_started_at = None
        self.shutdown_events = []
        self.stop_retries = []

    def send_config(self, path: str):
        self.configs.append(path)

    def read_dump(self):
        self.read_started_at = time.monotonic()
        if self.error is not None:
            raise self.error
        return self.raw

    def close(self):
        self.shutdown_events.append("close")
        self.closed = True

    def stop_sensor(self, *, retry=True):
        self.stop_retries.append(retry)
        self.shutdown_events.append("sensorStop")


class FakeButton:
    """gpiozero-compatible button double."""

    def __init__(self, pin, pull_up, bounce_time):
        self.pin = pin
        self.pull_up = pull_up
        self.bounce_time = bounce_time
        self.when_pressed = None
        self.closed = False

    def close(self):
        self.closed = True


def _temperature_report() -> dict[str, int]:
    return {
        "device_time_ms": 123456,
        "rx0_c": 42,
        "rx1_c": 43,
        "rx2_c": 44,
        "rx3_c": 45,
        "tx0_c": 46,
        "tx1_c": 47,
        "tx2_c": 48,
        "pm_c": 49,
        "dig0_c": 50,
        "dig1_c": 51,
    }


def _raw_dump(temperature_report: dict[str, int] | None = None) -> bytes:
    cube = np.zeros((2, 4, 4, 8), dtype=complex)
    return pack_dump(
        cube,
        n_tx=2,
        version=5 if temperature_report is not None else 3,
        frame_period_us=6000,
        temperature_report=temperature_report,
    )


def test_capture_monitor_matches_gpio_edge_to_ops_impact(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    radar = FakeRadar(_raw_dump())
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        save_dumps=True,
    )
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge + 0.012, timeout_s=1.0)

    assert capture is not None and capture.valid
    assert capture.trigger_timestamp == edge
    assert capture.path is not None and capture.path.read_bytes() == _raw_dump()
    assert radar.configs == [str(config)]
    assert monitor._button.bounce_time is None  # pylint: disable=protected-access

    monitor.stop()
    assert radar.closed
    assert radar.stop_retries == [False]


def test_capture_monitor_stop_skips_resync_when_ti_is_silent(tmp_path, caplog):
    radar, serial, clock = scripted_radar()
    resyncs = []
    monitor = _monitor(tmp_path, radar)
    monitor.start()
    radar.resync = lambda **kwargs: resyncs.append(kwargs) or False
    serial.script.update({"sensorStop": [b""], "stats": [b""], "": [b""]})
    started = clock.now

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.monitor"):
        monitor.stop()

    assert clock.now - started < 6.0
    assert resyncs == []
    assert serial.sent("sensorStop") == 2
    assert serial.closed
    assert "Firmware did not stop cleanly" in caplog.text
    assert "TI stats after failed stop" in caplog.text


def test_capture_monitor_keeps_sensor_stop_retry_when_start_fails(tmp_path):
    class ArmFailingButton(FakeButton):
        def __init__(self, *args, **kwargs):
            raise OSError("GPIO busy")

    radar = FakeRadar(_raw_dump())
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=ArmFailingButton,
    )

    with pytest.raises(OSError, match="GPIO busy"):
        monitor.start()

    assert radar.stop_retries == [True]
    assert radar.closed


def test_capture_monitor_keeps_valid_raw_in_memory_without_writing_dump(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    raw = _raw_dump()
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(raw),
        button_factory=FakeButton,
    )
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=1.0)

    assert capture is not None and capture.valid
    assert capture.raw == raw
    assert capture.path is None
    assert not (tmp_path / "dumps").exists()
    monitor.stop()


def test_capture_monitor_notifies_trigger_observers(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    raw = _raw_dump()
    observed = []
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(raw),
        button_factory=FakeButton,
        trigger_observers=[observed.append],
    )
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=1.0)

    assert capture is not None and capture.valid
    assert observed == [edge]
    monitor.stop()


def test_capture_monitor_records_temperature_report_from_dump_header(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    report = _temperature_report()
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(_raw_dump(temperature_report=report)),
        button_factory=FakeButton,
    )
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=1.0)

    assert capture is not None and capture.valid
    assert capture.temperature_report == report
    monitor.stop()


def test_capture_monitor_can_configure_before_arming_gpio(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    radar = FakeRadar(_raw_dump())
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
    )
    monitor.start(armed=False)

    edge = time.time()
    assert not monitor.notify_trigger(edge)
    assert monitor._button.when_pressed is None  # pylint: disable=protected-access

    monitor.arm()
    assert monitor._button.when_pressed == monitor.notify_trigger  # pylint: disable=protected-access
    assert monitor.notify_trigger(edge)
    assert monitor.capture_for_shot(edge, timeout_s=1.0).valid
    monitor.stop()


def test_capture_monitor_finishes_active_dump_before_closing_serial(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")

    class BlockingRadar(FakeRadar):
        def __init__(self, raw):
            super().__init__(raw)
            self.read_started = threading.Event()
            self.release_read = threading.Event()
            self.closed_before_read_finished = False

        def read_dump(self):
            self.read_started.set()
            self.release_read.wait(timeout=1.0)
            return self.raw

        def close(self):
            self.closed_before_read_finished = not self.release_read.is_set()
            # Unblock the old close-before-join implementation so this test fails fast.
            self.release_read.set()
            super().close()

    radar = BlockingRadar(_raw_dump())
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
    )
    monitor.start()
    assert monitor.notify_trigger(time.time())
    assert radar.read_started.wait(timeout=0.5)

    stopper = threading.Thread(target=monitor.stop)
    stopper.start()
    time.sleep(0.05)
    radar.release_read.set()
    stopper.join(timeout=1.0)

    assert not stopper.is_alive()
    assert not radar.closed_before_read_finished
    assert radar.closed
    assert radar.shutdown_events == ["sensorStop", "close"]


def test_capture_monitor_closes_serial_when_sensor_stop_fails(tmp_path):
    """A failed firmware stop must not leak the host serial descriptor."""
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")

    class StopFailingRadar(FakeRadar):
        def stop_sensor(self, *, retry=True):
            self.shutdown_events.append("sensorStop")
            raise RuntimeError("firmware remained active")

    radar = StopFailingRadar(_raw_dump())
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
    )
    monitor.start()

    monitor.stop()

    assert radar.shutdown_events == ["sensorStop", "close"]
    assert radar.closed


def test_capture_monitor_discards_stale_false_trigger(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(_raw_dump()),
        button_factory=FakeButton,
        match_tolerance_s=0.1,
    )
    monitor.start()
    assert monitor.notify_trigger(100.0)

    assert monitor.capture_for_shot(101.0, timeout_s=0.1) is None
    monitor.stop()


def test_capture_monitor_returns_quickly_when_matching_trigger_is_absent(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(_raw_dump()),
        button_factory=FakeButton,
        match_tolerance_s=0.1,
    )
    monitor.start()

    start = time.monotonic()
    capture = monitor.capture_for_shot(time.time() - 1.0, timeout_s=1.0)

    assert capture is None
    assert time.monotonic() - start < 0.2
    monitor.stop()


def test_capture_monitor_surfaces_dump_failure_without_hanging(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(b"", error=OSError("serial disconnected")),
        button_factory=FakeButton,
    )
    monitor.start()
    edge = time.time()
    assert monitor.notify_trigger(edge)

    capture = monitor.capture_for_shot(edge, timeout_s=1.0)

    assert capture is not None
    assert not capture.valid
    assert capture.error == "serial disconnected"
    monitor.stop()


def test_capture_monitor_closes_serial_when_gpio_setup_fails(tmp_path):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    radar = FakeRadar(_raw_dump())

    def failing_button(*_args, **_kwargs):
        raise RuntimeError("GPIO unavailable")

    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=failing_button,
    )

    try:
        monitor.start()
    except RuntimeError as error:
        assert str(error) == "GPIO unavailable"
    else:
        raise AssertionError("expected GPIO setup to fail")
    assert radar.shutdown_events == ["sensorStop", "close"]
    assert radar.closed


def _monitor(tmp_path, radar, **kwargs):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    return IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        **kwargs,
    )


def test_capture_monitor_names_hwa_stall_after_two_class_a_failures(tmp_path, caplog):
    refusal = RuntimeError(
        "IWR6843 dump refused before ILD1: Error: HWA post-trigger frame freeze timed out"
    )
    monitor = _monitor(tmp_path, FakeRadar(b"", error=refusal))
    monitor.start()
    base = time.time()
    errors = []

    with caplog.at_level(logging.ERROR, logger="openflight.iwr6843.monitor"):
        for index in range(3):
            edge = base + index
            assert monitor.notify_trigger(edge)
            errors.append(monitor.capture_for_shot(edge, timeout_s=1.0).error)
    monitor.stop()

    assert errors[0] == str(refusal)
    assert errors[1:] == [HWA_STALL_SESSION_REASON, HWA_STALL_SESSION_REASON]
    stall_logs = [r for r in caplog.records if "stopped delivering data" in r.getMessage()]
    assert len(stall_logs) == 1
    assert stall_logs[0].levelno == logging.ERROR


def test_capture_monitor_resets_hwa_stall_count_after_a_good_dump(tmp_path):
    class AlternatingRadar(FakeRadar):
        def __init__(self, raw):
            super().__init__(raw)
            self.calls = 0

        def read_dump(self):
            self.calls += 1
            if self.calls == 2:
                return self.raw
            raise ValueError("bad magic b'l3du' (expected b'ILD1')")

    monitor = _monitor(tmp_path, AlternatingRadar(_raw_dump()))
    monitor.start()
    base = time.time()
    errors = []
    for index in range(3):
        edge = base + index
        assert monitor.notify_trigger(edge)
        errors.append(monitor.capture_for_shot(edge, timeout_s=1.0).error)
    monitor.stop()

    assert errors[1] is None
    assert HWA_STALL_SESSION_REASON not in errors


def test_capture_monitor_saves_short_dump_for_diagnosis(tmp_path):
    raw = _raw_dump()
    monitor = _monitor(tmp_path, FakeRadar(raw[:-64]), save_dumps=True)
    monitor.start()
    edge = time.time()
    assert monitor.notify_trigger(edge)

    capture = monitor.capture_for_shot(edge, timeout_s=1.0)
    monitor.stop()

    assert capture is not None and not capture.valid
    assert "short IWR6843 dump" in capture.error
    saved = list((tmp_path / "dumps").glob("*.short.l3dump"))
    assert len(saved) == 1
    assert saved[0].read_bytes() == raw[:-64]


def test_capture_monitor_stop_cancels_streaming_dump_before_sensor_stop(tmp_path):
    class StreamingRadar(FakeRadar):
        def __init__(self, raw):
            super().__init__(raw)
            self.read_started = threading.Event()

        def read_dump(self, cancel_event=None):
            self.read_started.set()
            assert cancel_event is not None and cancel_event.wait(timeout=1.0)
            self.shutdown_events.append("cancel")
            raise DumpCancelled("OPS rejected acoustic trigger; TI re-armed")

    radar = StreamingRadar(_raw_dump())
    monitor = _monitor(tmp_path, radar, cancel_false_triggers=True)
    monitor.start()
    assert monitor.notify_trigger(time.time())
    assert radar.read_started.wait(timeout=0.5)

    started = time.monotonic()
    monitor.stop()

    assert time.monotonic() - started < 0.5
    assert radar.shutdown_events == ["cancel", "sensorStop", "close"]


def test_capture_monitor_logs_stats_when_sensor_stop_fails(tmp_path, caplog):
    class WedgedRadar(FakeRadar):
        def stop_sensor(self, *, retry=True):
            raise RuntimeError("HWA shutdown boundary timed out")

        def stats(self):
            return "stats\nframes=9 active=1 hwa_out=0\nDone\n"

    radar = WedgedRadar(_raw_dump())
    monitor = _monitor(tmp_path, radar)
    monitor.start()

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.monitor"):
        monitor.stop()

    assert "TI stats after failed stop: stats frames=9 active=1 hwa_out=0 Done" in caplog.text
    assert radar.closed


# --- USB power-cycle recovery -------------------------------------------------

_CLASS_A_ERROR = "IWR6843 dump refused before ILD1: Error: HWA post-trigger frame freeze timed out"


class WedgingRadar(FakeRadar):
    """Fails dumps with a class-A error until a reconfigure after a reopen."""

    def __init__(self, raw: bytes, *, stays_wedged: bool = False, error: str = _CLASS_A_ERROR):
        super().__init__(raw)
        self.wedged = True
        self.stays_wedged = stays_wedged
        self.wedge_error = error
        self.reopened = 0
        self.dumps = 0

    def read_dump(self):
        self.dumps += 1
        if self.wedged:
            raise RuntimeError(self.wedge_error)
        return self.raw

    def reopen(self):
        self.reopened += 1
        self.shutdown_events.append("reopen")

    def send_config(self, path: str):
        super().send_config(path)
        if self.reopened and not self.stays_wedged:
            self.wedged = False


class FakeCycler:
    """UsbPowerCycler double; optionally blocks until released."""

    def __init__(self, *, error: Exception | None = None, block: bool = False):
        self.error = error
        self.block = block
        self.cycles = 0
        self.threads = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def cycle(self):
        self.cycles += 1
        self.threads.append(threading.current_thread().name)
        self.entered.set()
        if self.block:
            self.release.wait(5.0)
        if self.error is not None:
            raise self.error
        return 3.0


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def port_waits(monkeypatch):
    """Record re-enumeration waits instead of polling the filesystem."""
    from openflight.iwr6843 import monitor as monitor_module

    waits = []

    def present(path, *, present, timeout_s):
        waits.append((path, present, timeout_s))
        return True

    monkeypatch.setattr(monitor_module, "wait_for_path", present)
    return waits


def _fail_twice(monitor, base: float) -> list:
    errors = []
    for index in range(2):
        edge = base + index
        assert monitor.notify_trigger(edge)
        errors.append(monitor.capture_for_shot(edge, timeout_s=1.0).error)
    return errors


def _recovered(monitor, radar, configs: int = 2):
    # pylint: disable=protected-access
    return len(radar.configs) == configs and not monitor._recovering


def test_hwa_stall_triggers_one_usb_power_cycle_and_dumps_resume(tmp_path, port_waits, caplog):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler)
    monitor.start()
    base = time.time()

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        errors = _fail_twice(monitor, base)
        assert _wait_until(lambda: _recovered(monitor, radar))

        edge = base + 2
        assert monitor.notify_trigger(edge)
        capture = monitor.capture_for_shot(edge, timeout_s=1.0)
    monitor.stop()

    # pylint: disable=protected-access
    assert errors == [_CLASS_A_ERROR, HWA_STALL_RECOVERING_REASON]
    assert cycler.cycles == 1
    assert cycler.threads == ["iwr6843-capture"]
    assert radar.shutdown_events[:2] == ["close", "reopen"]
    assert port_waits == [("/dev/fake-iwr6843", True, 0.5)]
    assert radar.configs == [str(tmp_path / "radar.cfg")] * 2
    assert monitor._class_a_failures == 0
    assert monitor._hwa_stall_reported is False
    assert capture is not None and capture.valid
    assert "TI wedged (HWA chain stalled); power-cycling USB (attempt 1/2)" in caplog.text
    assert "TI recovered by USB power cycle in" in caplog.text
    # The pending recovery replaces the "shut the Pi down" operator error.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_single_class_a_failure_does_not_power_cycle(tmp_path, port_waits):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler)
    monitor.start()
    edge = time.time()

    assert monitor.notify_trigger(edge)
    monitor.capture_for_shot(edge, timeout_s=1.0)
    monitor.stop()

    assert cycler.cycles == 0
    assert port_waits == []


def test_edges_during_recovery_reach_observers_but_queue_no_dump(tmp_path, port_waits, caplog):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler(block=True)
    observed = []
    monitor = _monitor(
        tmp_path, radar, usb_power_cycler=cycler, trigger_observers=[observed.append]
    )
    monitor.start()
    base = time.time() - 10.0

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        _fail_twice(monitor, base)
        assert cycler.entered.wait(2.0)
        dumps_before = radar.dumps

        during = [base + 2, base + 3]
        queued = [monitor.notify_trigger(edge) for edge in during]
        busy = monitor.dump_in_progress
        started = time.monotonic()
        matched = monitor.capture_for_shot(during[0], timeout_s=5.0)
        waited_s = time.monotonic() - started

        cycler.release.set()
        assert _wait_until(lambda: _recovered(monitor, radar))
        after = base + 4
        assert monitor.notify_trigger(after)
        capture = monitor.capture_for_shot(after, timeout_s=1.0)
    monitor.stop()

    assert queued == [False, False]
    assert observed == [base, base + 1, *during, after]
    assert busy is False
    assert matched is not None and not matched.valid
    assert matched.error == TI_RESTARTING_REASON
    assert matched.trigger_timestamp == during[0]
    assert waited_s < 0.5
    assert radar.dumps == dumps_before + 1
    assert caplog.text.count("TI recovering; edges are only fanned out") == 1
    assert capture is not None and capture.valid


def test_failed_recovery_stops_ti_dumps_but_keeps_observers(tmp_path, port_waits, caplog):
    from openflight.iwr6843.usb_power import UsbPowerCycleError

    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler(error=UsbPowerCycleError("USB power cycle failed: 'sudo -n ...' exited 1"))
    observed = []
    monitor = _monitor(
        tmp_path, radar, usb_power_cycler=cycler, trigger_observers=[observed.append]
    )
    monitor.start()
    base = time.time() - 10.0

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        _fail_twice(monitor, base)
        # pylint: disable=protected-access
        assert _wait_until(lambda: monitor._ti_failed and not monitor._recovering)
        dumps_before = radar.dumps
        later = [base + 2, base + 3]
        queued = [monitor.notify_trigger(edge) for edge in later]
        busy = monitor.dump_in_progress
        shot = monitor.capture_for_shot(later[0], timeout_s=5.0)
        started = time.monotonic()
        unmatched = monitor.capture_for_shot(None, timeout_s=5.0)
        unmatched_wait_s = time.monotonic() - started
        monitor.stop()

    assert queued == [False, False]
    assert observed == [base, base + 1, *later]
    assert busy is False
    assert radar.dumps == dumps_before
    assert radar.reopened == 0
    assert shot is not None and not shot.valid
    assert shot.error == TI_RECOVERY_FAILED_SESSION_REASON
    assert shot.trigger_timestamp == later[0]
    assert unmatched is not None and unmatched.error == TI_RECOVERY_FAILED_SESSION_REASON
    assert unmatched_wait_s < 0.5
    failed = [r for r in caplog.records if "recovery by USB power cycle failed" in r.getMessage()]
    assert len(failed) == 1 and failed[0].levelno == logging.ERROR
    assert failed[0].exc_info is not None
    reason = [r for r in caplog.records if TI_RECOVERY_FAILED_SESSION_REASON in r.getMessage()]
    assert len(reason) == 1 and reason[0].levelno == logging.ERROR
    # The port was released by the recovery: stop does not talk to a closed port.
    assert radar.stop_retries == []
    assert "TI port already released by an interrupted recovery" in caplog.text


def test_recovery_fails_when_port_does_not_return(tmp_path, monkeypatch):
    from openflight.iwr6843 import monitor as monitor_module

    monkeypatch.setattr(monitor_module, "wait_for_path", lambda *_a, **_k: False)
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler)
    monitor.REENUMERATE_TIMEOUT_S = 0.0
    monitor.start()

    _fail_twice(monitor, time.time())
    # pylint: disable=protected-access
    assert _wait_until(lambda: monitor._ti_failed)
    monitor.stop()

    assert cycler.cycles == 1
    assert radar.reopened == 0


def test_recovery_skips_port_wait_for_a_non_path_port(tmp_path, port_waits):
    radar = WedgingRadar(_raw_dump())
    radar.port = "COM7"
    monitor = _monitor(tmp_path, radar, usb_power_cycler=FakeCycler())
    monitor.start()

    _fail_twice(monitor, time.time())
    assert _wait_until(lambda: _recovered(monitor, radar))
    monitor.stop()

    assert port_waits == []
    assert radar.reopened == 1


def test_recovery_failing_in_send_config_marks_ti_failed(tmp_path, port_waits):
    class ConfigFailingRadar(WedgingRadar):
        def send_config(self, path):
            if self.reopened:
                raise RuntimeError("IWR6843 config rejected")
            super().send_config(path)

    radar = ConfigFailingRadar(_raw_dump())
    monitor = _monitor(tmp_path, radar, usb_power_cycler=FakeCycler())
    monitor.start()

    _fail_twice(monitor, time.time())
    # pylint: disable=protected-access
    assert _wait_until(lambda: monitor._ti_failed)
    assert not monitor.notify_trigger(time.time() + 5)
    monitor.stop()

    # The port was reopened, so stop still stops the sensor and closes it.
    assert radar.stop_retries == [False]
    assert radar.shutdown_events[-1] == "close"


def test_max_recoveries_is_respected(tmp_path, port_waits, caplog):
    radar = WedgingRadar(_raw_dump(), stays_wedged=True)
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler, max_recoveries=1)
    monitor.start()
    base = time.time()

    with caplog.at_level(logging.ERROR, logger="openflight.iwr6843.monitor"):
        _fail_twice(monitor, base)
        assert _wait_until(lambda: _recovered(monitor, radar))
        errors = _fail_twice(monitor, base + 2)
        errors += _fail_twice(monitor, base + 4)
    monitor.stop()

    assert cycler.cycles == 1
    assert errors[1:] == [HWA_STALL_SESSION_REASON] * 3
    stall_logs = [r for r in caplog.records if HWA_STALL_SESSION_REASON in r.getMessage()]
    assert len(stall_logs) == 1


def test_without_cycler_hwa_stall_keeps_existing_behaviour(tmp_path, caplog):
    radar = WedgingRadar(_raw_dump())
    monitor = _monitor(tmp_path, radar)
    monitor.start()
    base = time.time()

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        errors = _fail_twice(monitor, base)
        assert monitor.notify_trigger(base + 2)
        errors.append(monitor.capture_for_shot(base + 2, timeout_s=1.0).error)
    monitor.stop()

    assert errors == [_CLASS_A_ERROR, HWA_STALL_SESSION_REASON, HWA_STALL_SESSION_REASON]
    assert radar.reopened == 0
    assert radar.shutdown_events == ["sensorStop", "close"]
    assert "power-cycling USB" not in caplog.text
    stall_logs = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and "stopped delivering data" in r.getMessage()
    ]
    assert len(stall_logs) == 1


def test_stop_during_port_wait_returns_promptly(tmp_path, monkeypatch, caplog):
    from openflight.iwr6843 import monitor as monitor_module

    waiting = threading.Event()

    def never_present(_path, *, present, timeout_s):  # pylint: disable=unused-argument
        waiting.set()
        time.sleep(0.01)
        return False

    monkeypatch.setattr(monitor_module, "wait_for_path", never_present)
    radar = WedgingRadar(_raw_dump())
    monitor = _monitor(tmp_path, radar, usb_power_cycler=FakeCycler())
    monitor.start()
    _fail_twice(monitor, time.time())
    assert waiting.wait(2.0)

    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        monitor.stop()

    assert time.monotonic() - started < 1.0
    assert radar.reopened == 0
    assert radar.stop_retries == []
    assert "TI recovery aborted" in caplog.text
    assert "Capture monitor stopped" in caplog.text


def test_stop_during_power_cycle_aborts_after_power_returns(tmp_path, port_waits):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler(block=True)
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler)
    monitor.start()
    _fail_twice(monitor, time.time())
    assert cycler.entered.wait(2.0)

    stopper = threading.Thread(target=monitor.stop)
    started = time.monotonic()
    stopper.start()
    time.sleep(0.05)
    cycler.release.set()
    stopper.join(3.0)

    assert not stopper.is_alive()
    assert time.monotonic() - started < 2.0
    assert cycler.cycles == 1
    assert port_waits == []
    assert radar.reopened == 0
    # pylint: disable=protected-access
    assert monitor._button is None


def test_edge_queued_before_recovery_is_dropped_but_stop_sentinel_kept(tmp_path):
    monitor = _monitor(tmp_path, FakeRadar(_raw_dump()))
    # pylint: disable=protected-access
    monitor._events.put_nowait(123.0)
    with monitor._condition:
        monitor._drop_queued_edge_locked()
    assert monitor._events.empty()

    monitor._events.put_nowait(None)
    with monitor._condition:
        monitor._drop_queued_edge_locked()
    assert monitor._events.get_nowait() is None


def _scripted_monitor(tmp_path, script, **kwargs):
    """Monitor on the real driver over a scripted serial, ready to dump."""
    from iwr6843_scripted_cli import ScriptedCli

    radar, serial, clock = scripted_radar()
    monitor = _monitor(tmp_path, radar, **kwargs)
    monitor.start()
    serial.script.update({key: list(value) for key, value in script.items()})
    reopened = []

    def reopen():
        # A power-cycled TI comes back with a healthy CLI on a fresh handle.
        radar.ser = ScriptedCli(clock)
        reopened.append(radar.ser)

    radar.reopen = reopen
    return monitor, radar, serial, reopened


_SILENT = {"l3dump": [b""], "": [b""]}
_ECHO_ONLY = {"l3dump": [b"l3dump\r\n"], "": [b""]}
_ECHO_PROMPT_LOST = {"l3dump": [b"l3dump\r\n" + PROMPT]}


@pytest.mark.parametrize(
    ("script", "error"),
    [
        (_SILENT, "short IWR6843 dump: 0 bytes"),
        (_ECHO_ONLY, "short IWR6843 dump: 8 bytes"),
    ],
    ids=["silent", "echo-only"],
)
def test_silent_cli_dump_recovers_after_one_failure(tmp_path, port_waits, script, error):
    cycler = FakeCycler()
    monitor, radar, serial, reopened = _scripted_monitor(tmp_path, script, usb_power_cycler=cycler)
    newlines_after_config = serial.sent("")
    edge = time.time()

    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    # pylint: disable=protected-access
    assert _wait_until(lambda: len(reopened) == 1 and not monitor._recovering)
    resyncs = serial.sent("") - newlines_after_config
    later = edge + 1
    queued = monitor.notify_trigger(later)
    monitor.stop()

    assert capture.error == error
    assert resyncs >= 1
    assert cycler.cycles == 1
    assert serial.closed
    assert reopened[0].sent("sensorStart") == 1
    assert monitor._ti_failed is False
    assert queued is True
    assert radar.ser is reopened[0]


def test_lost_l3dump_command_with_prompt_on_resync_does_not_recover(tmp_path, port_waits, caplog):
    cycler = FakeCycler()
    monitor, _radar, serial, reopened = _scripted_monitor(
        tmp_path, _ECHO_PROMPT_LOST, usb_power_cycler=cycler
    )
    newlines_after_config = serial.sent("")
    edge = time.time()

    with caplog.at_level(logging.INFO, logger="openflight.iwr6843.monitor"):
        assert monitor.notify_trigger(edge)
        capture = monitor.capture_for_shot(edge, timeout_s=2.0)
        assert _wait_until(lambda: serial.sent("") > newlines_after_config)
        # pylint: disable=protected-access
        assert _wait_until(lambda: not monitor._probing)
        queued = monitor.notify_trigger(edge + 1)
    monitor.stop()

    assert capture.error == "short IWR6843 dump: 19 bytes"
    assert cycler.cycles == 0
    assert reopened == []
    assert queued is True
    assert "the l3dump command was lost, no recovery" in caplog.text


def test_silent_cli_without_cycler_keeps_existing_behaviour(tmp_path):
    monitor, _radar, serial, reopened = _scripted_monitor(tmp_path, _SILENT)
    newlines_after_config = serial.sent("")
    edge = time.time()

    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    resyncs = serial.sent("") - newlines_after_config
    queued = monitor.notify_trigger(edge + 1)
    monitor.capture_for_shot(edge + 1, timeout_s=2.0)
    monitor.stop()

    assert capture.error == "short IWR6843 dump: 0 bytes"
    assert resyncs == 0
    assert reopened == []
    assert queued is True


def test_edges_during_cli_probe_fan_out_without_dumps(tmp_path, port_waits):
    class ProbedRadar(FakeRadar):
        def __init__(self):
            super().__init__(b"")
            self.probing = threading.Event()
            self.answer = threading.Event()
            self.dumps = 0

        def read_dump(self):
            self.dumps += 1
            return b""

        def resync(self):
            self.probing.set()
            self.answer.wait(5.0)
            return True

    radar = ProbedRadar()
    cycler = FakeCycler()
    observed = []
    monitor = _monitor(
        tmp_path, radar, usb_power_cycler=cycler, trigger_observers=[observed.append]
    )
    monitor.start()
    base = time.time() - 10.0

    assert monitor.notify_trigger(base)
    assert radar.probing.wait(2.0)
    queued = monitor.notify_trigger(base + 1)
    busy = monitor.dump_in_progress
    radar.answer.set()
    # pylint: disable=protected-access
    assert _wait_until(lambda: not monitor._probing)
    monitor.stop()

    assert queued is False
    assert busy is False
    assert observed == [base, base + 1]
    assert radar.dumps == 1
    assert cycler.cycles == 0


def test_recovery_retries_reopen_until_the_port_opens(tmp_path, port_waits):
    import serial as pyserial

    radar = WedgingRadar(_raw_dump())
    failures = [OSError("[Errno 2] could not open port"), pyserial.SerialException("busy")]
    original_reopen = radar.reopen

    def flaky_reopen():
        if failures:
            raise failures.pop(0)
        original_reopen()

    radar.reopen = flaky_reopen
    monitor = _monitor(tmp_path, radar, usb_power_cycler=FakeCycler())
    monitor.REOPEN_STEP_S = 0.0
    monitor.start()

    _fail_twice(monitor, time.time())
    assert _wait_until(lambda: _recovered(monitor, radar))
    monitor.stop()

    assert failures == []
    assert radar.reopened == 1
    # pylint: disable=protected-access
    assert monitor._ti_failed is False


def test_recovery_fails_when_reopen_keeps_failing(tmp_path, port_waits):
    radar = WedgingRadar(_raw_dump())
    attempts = []

    def broken_reopen():
        attempts.append(1)
        raise OSError("could not open port")

    radar.reopen = broken_reopen
    monitor = _monitor(tmp_path, radar, usb_power_cycler=FakeCycler())
    monitor.REOPEN_RETRY_S = 0.0
    monitor.start()

    _fail_twice(monitor, time.time())
    # pylint: disable=protected-access
    assert _wait_until(lambda: monitor._ti_failed)
    monitor.stop()

    assert len(attempts) == 1


@pytest.fixture
def live_ops(monkeypatch):
    """Point the server's recovery guard at a fake running OPS monitor."""
    from types import SimpleNamespace

    from openflight import server

    def connect(port):
        live = None if port is None else SimpleNamespace(radar=SimpleNamespace(port=port))
        monkeypatch.setattr(server, "monitor", live)
        monkeypatch.setattr(server, "mock_mode", False)
        return server._make_iwr6843_recovery_guard(kld7=False)  # pylint: disable=protected-access

    return connect


def test_recovery_runs_for_an_auto_detected_ops_on_the_gpio_uart(tmp_path, port_waits, live_ops):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    guard = live_ops("/dev/ttyAMA0")
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler, recovery_guard=guard)
    monitor.start()

    errors = _fail_twice(monitor, time.time())
    assert _wait_until(lambda: _recovered(monitor, radar))
    monitor.stop()

    assert cycler.cycles == 1
    assert errors[1] == HWA_STALL_RECOVERING_REASON


@pytest.mark.parametrize(
    ("ops_port", "reason"),
    [
        ("/dev/ttyACM0", "OPS radar on /dev/ttyACM0 is not a GPIO UART"),
        (None, "OPS radar not connected yet"),
    ],
    ids=["ops-usb", "ops-not-connected"],
)
def test_blocked_recovery_keeps_the_existing_hwa_stall_guidance(
    tmp_path, port_waits, live_ops, caplog, ops_port, reason
):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    guard = live_ops(ops_port)
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler, recovery_guard=guard)
    monitor.start()
    base = time.time()

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.monitor"):
        errors = _fail_twice(monitor, base)
        errors += _fail_twice(monitor, base + 2)
    monitor.stop()

    assert cycler.cycles == 0
    assert radar.reopened == 0
    assert errors == [_CLASS_A_ERROR] + [HWA_STALL_SESSION_REASON] * 3
    blocked = [r for r in caplog.records if "blocked" in r.getMessage()]
    assert len(blocked) == 1 and blocked[0].levelno == logging.WARNING
    assert blocked[0].getMessage() == (
        f"[IWR6843] TI recovery by USB power cycle blocked: {reason}"
    )
    stall = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and HWA_STALL_SESSION_REASON in r.getMessage()
    ]
    assert len(stall) == 1


def test_blocked_guard_skips_the_silent_cli_probe(tmp_path, port_waits):
    cycler = FakeCycler()
    monitor, _radar, serial, reopened = _scripted_monitor(
        tmp_path,
        _SILENT,
        usb_power_cycler=cycler,
        recovery_guard=lambda: "OPS radar on /dev/ttyACM0 is not a GPIO UART",
    )
    newlines_after_config = serial.sent("")
    edge = time.time()

    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    monitor.stop()

    assert capture.error == "short IWR6843 dump: 0 bytes"
    assert serial.sent("") == newlines_after_config
    assert cycler.cycles == 0
    assert reopened == []


def test_recovery_guard_is_read_again_before_each_recovery(tmp_path, port_waits):
    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    reasons = ["OPS radar not connected yet"]
    monitor = _monitor(
        tmp_path,
        radar,
        usb_power_cycler=cycler,
        recovery_guard=lambda: reasons[0],
    )
    monitor.start()
    base = time.time()

    _fail_twice(monitor, base)
    blocked_cycles = cycler.cycles
    reasons[0] = None
    _fail_twice(monitor, base + 2)
    assert _wait_until(lambda: _recovered(monitor, radar))
    monitor.stop()

    assert blocked_cycles == 0
    assert cycler.cycles == 1


def test_failing_recovery_guard_blocks(tmp_path, port_waits, caplog):
    def broken_guard():
        raise AttributeError("monitor half-built")

    radar = WedgingRadar(_raw_dump())
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, radar, usb_power_cycler=cycler, recovery_guard=broken_guard)
    monitor.start()

    with caplog.at_level(logging.WARNING, logger="openflight.iwr6843.monitor"):
        _fail_twice(monitor, time.time())
    monitor.stop()

    assert cycler.cycles == 0
    assert "TI recovery by USB power cycle blocked: recovery guard failed" in caplog.text


# --- l3dump delay (--shot-trigger ops-speed: the TI ring after the pin-1 edge) ---------


class CancellableRadar(FakeRadar):
    """Transport double that accepts the cancel event and notes when l3dump went out."""

    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.read_started_ns = None

    def read_dump(self, cancel_event=None):
        self.read_started_ns = time.monotonic_ns()
        if cancel_event is not None and cancel_event.is_set():
            raise DumpCancelled("cancelled")
        return super().read_dump()


def _delayed_monitor(tmp_path, radar, delay_s: float, **kwargs):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    return IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        l3dump_delay_s=delay_s,
        **kwargs,
    )


def test_l3dump_delay_waits_after_the_edge_and_records_the_timing(tmp_path):
    radar = CancellableRadar(_raw_dump())
    monitor = _delayed_monitor(tmp_path, radar, 0.025)
    monitor.start()

    edge_ns = time.monotonic_ns()
    edge = time.time()
    assert monitor.notify_trigger(edge, monotonic_ns=edge_ns)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    monitor.stop()

    assert capture is not None and capture.valid
    assert (radar.read_started_ns - edge_ns) / 1e6 >= 25.0
    assert capture.l3dump_target_ms == 25.0
    assert 25.0 <= capture.l3dump_actual_ms < 500.0


def test_l3dump_delay_counts_from_the_edge_not_from_the_callback(tmp_path):
    radar = CancellableRadar(_raw_dump())
    monitor = _delayed_monitor(tmp_path, radar, 0.025)
    monitor.start()

    # An edge delivered 30 ms late (debounce, slow callback): no extra wait.
    late_ns = time.monotonic_ns() - 30_000_000
    edge = time.time() - 0.030
    assert monitor.notify_trigger(edge, monotonic_ns=late_ns)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    monitor.stop()

    assert capture is not None and capture.valid
    assert 30.0 <= capture.l3dump_actual_ms < 100.0


def test_cancel_during_the_l3dump_delay_skips_the_dump(tmp_path):
    radar = CancellableRadar(_raw_dump())
    monitor = _delayed_monitor(tmp_path, radar, 0.2, cancel_false_triggers=True)
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    # The OPS rejected the dump (or the camera gate blocked the edge) during the wait.
    assert monitor.reject_edge(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    monitor.stop()

    assert radar.read_started_ns is None, "no l3dump for a cancelled edge"
    assert capture is not None and not capture.valid
    assert "l3dump delay" in capture.error
    assert capture.l3dump_actual_ms is None


def test_zero_l3dump_delay_is_unchanged(tmp_path):
    radar = CancellableRadar(_raw_dump())
    monitor = _delayed_monitor(tmp_path, radar, 0.0)
    monitor.start()

    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=1.0)
    monitor.stop()

    assert capture is not None and capture.valid
    assert capture.l3dump_target_ms is None and capture.l3dump_actual_ms is None
    assert monitor._edge_monotonic == {}  # pylint: disable=protected-access


@pytest.mark.parametrize("delay_s", [-0.001, 1.5])
def test_l3dump_delay_out_of_range_is_refused(tmp_path, delay_s):
    with pytest.raises(ValueError, match="l3dump delay"):
        _delayed_monitor(tmp_path, FakeRadar(_raw_dump()), delay_s)


@pytest.mark.parametrize("delay_s", [0.025, 0.0], ids=["ops-speed", "sound"])
def test_l3dump_timing_reaches_the_iwr6843_capture_entry(tmp_path, monkeypatch, delay_s):
    """``iwr6843_capture`` carries the edge-to-l3dump time in ops-speed, nothing new in sound."""
    import json
    from datetime import datetime
    from types import SimpleNamespace

    from openflight import server as server_module
    from openflight.launch_monitor import ClubType, Shot
    from openflight.session_logger import SessionLogger

    radar = CancellableRadar(_raw_dump())
    monitor = _delayed_monitor(tmp_path, radar, delay_s)
    monitor.start()
    edge = time.time()
    assert monitor.notify_trigger(edge)
    capture = monitor.capture_for_shot(edge, timeout_s=2.0)
    monitor.stop()
    assert capture is not None and capture.valid

    runtime = SimpleNamespace(
        process_shot=lambda **_kwargs: SimpleNamespace(capture=capture, measurement=None)
    )
    session = SessionLogger(log_dir=tmp_path / "logs")
    session.start_session()
    monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)
    shot = Shot(
        ball_speed_mph=100.0,
        club_speed_mph=80.0,
        timestamp=datetime.now(),
        impact_timestamp=edge + 0.028,
        club=ClubType.IRON_9,
    )
    server_module._process_iwr6843_angle(shot)  # pylint: disable=protected-access
    session.end_session()

    records = [
        json.loads(line)
        for path in (tmp_path / "logs").glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    record = next(entry for entry in records if entry.get("type") == "iwr6843_capture")
    if delay_s:
        assert record["l3dump_target_ms"] == 25.0
        assert record["l3dump_actual_ms"] == pytest.approx(capture.l3dump_actual_ms)
        assert record["l3dump_actual_ms"] >= 25.0
    else:
        assert "l3dump_target_ms" not in record and "l3dump_actual_ms" not in record
