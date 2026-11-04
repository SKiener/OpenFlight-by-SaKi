"""The sound trigger must leave the OPS243 armed on every path out of a wait."""

import threading
from unittest.mock import Mock

import pytest

from openflight.rolling_buffer import monitor as monitor_module
from openflight.rolling_buffer.monitor import RollingBufferMonitor
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.rolling_buffer.types import IQCapture


def _idle_radar():
    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = ""
    radar.last_hardware_trigger_first_byte_timestamp = None
    radar.last_hardware_trigger_lost_capture_bytes = 0
    return radar


def test_lost_capture_rearms_the_idle_radar():
    radar = _idle_radar()
    radar.last_hardware_trigger_lost_capture_bytes = 30000
    trigger = SoundTrigger(pre_trigger_segments=8)

    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1) is None

    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert trigger.drain_diagnostics()[0]["reason"] == "lost_capture"


def test_plain_idle_timeout_does_not_rearm():
    radar = _idle_radar()
    trigger = SoundTrigger(pre_trigger_segments=8)

    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1) is None

    radar.rearm_rolling_buffer.assert_not_called()
    assert not trigger.drain_diagnostics()


def test_failed_rearm_is_retried_before_the_next_wait():
    radar = _idle_radar()
    radar.wait_for_hardware_trigger.return_value = "unparseable dump"
    radar.rearm_rolling_buffer.return_value = False
    processor = RollingBufferProcessor()
    processor.parse_capture = Mock(return_value=None)
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.wait_for_trigger(radar, processor, timeout=1)

    radar.reset_mock()
    radar.wait_for_hardware_trigger.return_value = ""
    radar.rearm_rolling_buffer.return_value = True
    trigger.wait_for_trigger(radar, processor, timeout=1)

    called = [name for name, _args, _kwargs in radar.mock_calls]
    assert called[:2] == ["rearm_rolling_buffer", "wait_for_hardware_trigger"]

    radar.reset_mock()
    trigger.wait_for_trigger(radar, processor, timeout=1)
    radar.rearm_rolling_buffer.assert_not_called()


def _dump_radar():
    radar = _idle_radar()
    radar.wait_for_hardware_trigger.return_value = "complete dump"
    return radar


def _processor_with_capture():
    processor = RollingBufferProcessor()
    capture = IQCapture(100.0, 100.0 + 0.0341, [2048] * 4096, [2048] * 4096)
    processor.parse_capture = Mock(return_value=capture)
    return processor


def test_error_while_validating_a_dump_still_rearms_and_propagates():
    radar = _dump_radar()
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger._summarize_capture_activity = Mock(side_effect=RuntimeError("fft failed"))

    with pytest.raises(RuntimeError, match="fft failed"):
        trigger.wait_for_trigger(radar, _processor_with_capture(), timeout=1)

    radar.rearm_rolling_buffer.assert_called_once_with(8)


def test_rearm_failure_during_error_cleanup_keeps_the_original_error():
    radar = _dump_radar()
    radar.rearm_rolling_buffer.side_effect = OSError("port gone")
    processor = RollingBufferProcessor()
    processor.parse_capture = Mock(side_effect=ValueError("bad json"))
    trigger = SoundTrigger(pre_trigger_segments=8)

    with pytest.raises(ValueError, match="bad json"):
        trigger.wait_for_trigger(radar, processor, timeout=1)

    radar.rearm_rolling_buffer.assert_called_once_with(8)


def test_rejected_dump_is_rearmed_exactly_once():
    radar = _dump_radar()
    trigger = SoundTrigger(pre_trigger_segments=8)

    assert trigger.wait_for_trigger(radar, _processor_with_capture(), timeout=1) is None

    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert trigger.drain_diagnostics()[0]["reason"] == "no_outbound_speed"


class _Clock:
    """Monotonic clock that only moves when a test advances it."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _stall_trigger(clock):
    return SoundTrigger(pre_trigger_segments=8, clock=clock)


def _reasons(trigger):
    return [entry["reason"] for entry in trigger.drain_diagnostics()]


def _called(radar):
    return [name for name, _args, _kwargs in radar.mock_calls]


def test_long_stall_rearms_once_the_timeout_has_passed():
    clock = _Clock()
    radar = _idle_radar()
    trigger = _stall_trigger(clock)

    clock.now += SoundTrigger.STALL_REARM_S - 1
    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1) is None
    radar.rearm_rolling_buffer.assert_not_called()

    clock.now += 1
    assert trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1) is None
    assert _called(radar) == ["wait_for_hardware_trigger"] * 2 + ["rearm_rolling_buffer"]
    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["long_stall"]


def test_each_dump_restarts_the_stall_timer():
    clock = _Clock()
    radar = _dump_radar()
    trigger = _stall_trigger(clock)
    clock.now += SoundTrigger.STALL_REARM_S - 1
    trigger.wait_for_trigger(radar, _processor_with_capture(), timeout=1)
    radar.reset_mock()
    radar.wait_for_hardware_trigger.return_value = ""

    clock.now += SoundTrigger.STALL_REARM_S - 1
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)
    radar.rearm_rolling_buffer.assert_not_called()

    clock.now += 1
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)
    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["no_outbound_speed", "long_stall"]


def test_dump_arriving_after_a_long_stall_is_read_and_rearmed_once():
    clock = _Clock()
    radar = _dump_radar()
    trigger = _stall_trigger(clock)
    clock.now += SoundTrigger.STALL_REARM_S + 60

    trigger.wait_for_trigger(radar, _processor_with_capture(), timeout=1)

    assert _called(radar) == ["wait_for_hardware_trigger", "rearm_rolling_buffer"]
    assert _reasons(trigger) == ["no_outbound_speed"]


def test_rearm_by_another_path_restarts_the_stall_timer():
    clock = _Clock()
    radar = _idle_radar()
    trigger = _stall_trigger(clock)
    clock.now += SoundTrigger.STALL_REARM_S - 1
    radar.last_hardware_trigger_lost_capture_bytes = 30000
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)

    radar.last_hardware_trigger_lost_capture_bytes = 0
    clock.now += 2
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)

    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["lost_capture"]


def test_failed_stall_rearm_is_retried_once_per_wait_until_it_succeeds():
    clock = _Clock()
    radar = _idle_radar()
    radar.rearm_rolling_buffer.return_value = False
    trigger = _stall_trigger(clock)
    clock.now += SoundTrigger.STALL_REARM_S
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)
    assert _reasons(trigger) == ["long_stall"]

    for rearmed in (False, True):
        radar.reset_mock()
        radar.rearm_rolling_buffer.return_value = rearmed
        clock.now += 30
        trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)
        assert _called(radar) == ["rearm_rolling_buffer", "wait_for_hardware_trigger"]

    radar.reset_mock()
    clock.now += 30
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1)
    radar.rearm_rolling_buffer.assert_not_called()
    assert not trigger.drain_diagnostics()


def test_cancelled_wait_does_not_rearm_during_shutdown():
    clock = _Clock()
    radar = _idle_radar()
    trigger = _stall_trigger(clock)
    clock.now += 10
    trigger.notify_trigger_edge()
    clock.now += SoundTrigger.STALL_REARM_S
    stop = threading.Event()
    stop.set()

    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=1, cancel_event=stop)

    radar.rearm_rolling_buffer.assert_not_called()
    assert not trigger.drain_diagnostics()


def test_sound_edge_without_a_dump_rearms_after_the_wait():
    clock = _Clock()
    radar = _idle_radar()
    trigger = _stall_trigger(clock)

    def edge_but_no_dump(**_kwargs):
        clock.now += 10
        trigger.notify_trigger_edge()
        clock.now += SoundTrigger.EDGE_DUMP_GRACE_S
        return ""

    radar.wait_for_hardware_trigger.side_effect = edge_but_no_dump
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=30)

    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["trigger_without_dump"]


def test_sound_edge_answered_by_a_dump_needs_no_extra_rearm():
    clock = _Clock()
    radar = _dump_radar()
    trigger = _stall_trigger(clock)

    def edge_then_dump(**_kwargs):
        clock.now += 10
        trigger.notify_trigger_edge()
        clock.now += 2 * SoundTrigger.EDGE_DUMP_GRACE_S
        return "complete dump"

    radar.wait_for_hardware_trigger.side_effect = edge_then_dump
    trigger.wait_for_trigger(radar, _processor_with_capture(), timeout=30)
    radar.wait_for_hardware_trigger.side_effect = None
    radar.wait_for_hardware_trigger.return_value = ""
    clock.now += 30
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=30)

    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["no_outbound_speed"]


def test_recent_sound_edge_defers_any_rearm_while_its_dump_may_be_arriving():
    clock = _Clock()
    radar = _idle_radar()
    trigger = _stall_trigger(clock)
    clock.now += SoundTrigger.STALL_REARM_S
    trigger.notify_trigger_edge()
    clock.now += 0.1

    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=30)
    radar.rearm_rolling_buffer.assert_not_called()

    clock.now += 30
    trigger.wait_for_trigger(radar, RollingBufferProcessor(), timeout=30)
    radar.rearm_rolling_buffer.assert_called_once_with(8)
    assert _reasons(trigger) == ["trigger_without_dump"]


def test_stall_rearm_is_recorded_as_a_trigger_event(monkeypatch):
    clock = _Clock()
    monitor = RollingBufferMonitor(trigger_type="sound", pre_trigger_segments=8, clock=clock)
    monitor.radar = _idle_radar()
    stall_steps = iter([SoundTrigger.STALL_REARM_S, None])

    def quiet_wait(**_kwargs):
        step = next(stall_steps)
        if step is None:
            monitor._running = False
        else:
            clock.now += step
        return ""

    monitor.radar.wait_for_hardware_trigger.side_effect = quiet_wait
    session_logger = Mock()
    monkeypatch.setattr(monitor_module, "get_session_logger", lambda: session_logger)
    monitor._running = True

    monitor._capture_loop()

    monitor.radar.rearm_rolling_buffer.assert_called_once_with(8)
    event = session_logger.log_trigger_event.call_args.kwargs
    assert (event["accepted"], event["reason"]) == (False, "long_stall")
