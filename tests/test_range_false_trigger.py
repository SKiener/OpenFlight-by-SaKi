"""A rejected OPS capture must release TI without cancelling the next shot."""

import threading
from unittest.mock import Mock

import numpy as np
import pytest

from openflight.iwr6843 import driver
from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.monitor import IWR6843CaptureMonitor


class CancelSerial:
    def __init__(self, acknowledge=True):
        self.raw = pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3)
        self.chunks = [self.raw[:128]]
        self.writes = []
        self.acknowledge = acknowledge
        self.cancel_marker = b"ILDCANCEL"

    def reset_input_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, count):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]

    def write(self, value):
        self.writes.append(value)
        if value == b"\x18":
            if self.acknowledge:
                self.chunks.extend(
                    [self.cancel_marker[:6], self.cancel_marker[6:] + b"Done\nl3dump:/>"]
                )
            else:
                self.chunks.append(self.raw[128:] + b"Done\r\nl3dump:/>")


@pytest.mark.parametrize("marker", [b"ILDCANCEL", b"ILDCANCE"])
def test_cancelled_dump_waits_for_firmware_ack_and_cli_ready(marker):
    radar = driver.IWR6843Radar.__new__(driver.IWR6843Radar)
    radar.ser = CancelSerial()
    radar.ser.cancel_marker = marker
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(driver.DumpCancelled):
        radar.read_dump(cancel_event=cancel, timeout_s=0.1)
    assert radar.ser.writes == [b"l3dump\n", b"\x18"]
    assert not radar.ser.chunks


def test_firmware_without_cancel_support_finishes_normal_dump():
    radar = driver.IWR6843Radar.__new__(driver.IWR6843Radar)
    radar.ser = CancelSerial(acknowledge=False)
    cancel = threading.Event()
    cancel.set()
    assert radar.read_dump(cancel_event=cancel, timeout_s=0.1) == radar.ser.raw
    assert radar.ser.writes == [b"l3dump\n", b"\x18"]


@pytest.mark.parametrize("timestamp", [None, float("nan"), 98.0, 101.0])
def test_unrelated_rejection_does_not_cancel_active_ti_capture(tmp_path, timestamp):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
        cancel_false_triggers=True,
    )
    monitor._running = monitor._armed = monitor._capture_active = True
    monitor._last_edge_timestamp = 100.0
    assert not monitor.reject_trigger(timestamp)
    assert not monitor._cancel_dump.is_set()


def test_matching_rejection_cancels_only_current_capture(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
        cancel_false_triggers=True,
    )
    monitor._running = monitor._armed = monitor._capture_active = True
    monitor._last_edge_timestamp = 100.0
    assert monitor.reject_trigger(100.08)
    assert monitor._cancel_dump.is_set()
    monitor._capture_active = False
    assert monitor.notify_trigger(102.0)
    assert not monitor._cancel_dump.is_set()


def test_rejection_does_not_cancel_when_monitor_is_stopped(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
        cancel_false_triggers=True,
    )
    monitor._last_edge_timestamp = 100.0
    assert not monitor.reject_trigger(100.08)


def test_no_motion_diagnostic_carries_actual_first_byte_time():
    from openflight.rolling_buffer.trigger import SoundTrigger
    from openflight.rolling_buffer.types import IQCapture

    trigger = SoundTrigger()
    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = "capture"
    radar.last_hardware_trigger_first_byte_timestamp = 100.08
    processor = Mock()
    processor.parse_capture.return_value = IQCapture(0, 0.068, [2048] * 4096, [2048] * 4096)
    processor.process_standard.return_value.readings = []
    assert trigger.wait_for_trigger(radar, processor, timeout=1) is None
    diagnostic = trigger.drain_diagnostics()[0]
    assert diagnostic["reason"] == "no_outbound_speed"
    assert diagnostic["capture_first_byte_timestamp"] == 100.08
    radar.rearm_rolling_buffer.assert_called_once_with(12)


def test_missing_restart_ack_is_not_reported_as_successful_cancellation():
    class FailedRestartSerial(CancelSerial):
        def write(self, value):
            super().write(value)
            if value == b"\x18":
                self.chunks = [b"ILDCANCEL\r\nError: restart failed\r\n"]

    radar = driver.IWR6843Radar.__new__(driver.IWR6843Radar)
    radar.ser = FailedRestartSerial()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="did not confirm"):
        radar.read_dump(cancel_event=cancel, timeout_s=0.1)


def test_already_completed_payload_is_not_cancelled():
    radar = driver.IWR6843Radar.__new__(driver.IWR6843Radar)
    radar.ser = CancelSerial()
    radar.ser.chunks = [radar.ser.raw + b"Done\r\nl3dump:/>"]
    cancel = threading.Event()
    cancel.set()
    assert radar.read_dump(cancel_event=cancel, timeout_s=0.1) == radar.ser.raw
    assert radar.ser.writes == [b"l3dump\n"]
