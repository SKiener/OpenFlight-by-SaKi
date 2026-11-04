"""The field failure must never produce a speed using an impossible timebase."""

from unittest.mock import Mock

import pytest

from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.rolling_buffer.types import IQCapture


@pytest.mark.parametrize("offset_ms", [205, -10, float("nan"), float("inf")])
def test_inconsistent_capture_is_rejected_before_fft_and_rate_is_reasserted(offset_ms):
    processor = RollingBufferProcessor()
    capture = IQCapture(100.0, 100.0 + offset_ms / 1000, [2048] * 4096, [2048] * 4096)
    processor.parse_capture = Mock(return_value=capture)
    processor.process_standard = Mock()
    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = "field capture"
    radar.last_hardware_trigger_first_byte_timestamp = 101.0
    trigger = SoundTrigger(pre_trigger_segments=16)
    assert trigger.wait_for_trigger(radar, processor, timeout=1) is None
    processor.process_standard.assert_not_called()
    radar.set_sample_rate.assert_called_once_with(30000)
    radar.rearm_rolling_buffer.assert_called_once_with(16)
    assert trigger.drain_diagnostics()[0]["reason"] == "capture_timebase_mismatch"


@pytest.mark.parametrize(
    "rate,offset_ms,valid",
    [
        (30000, 68, True),
        (30000, 0, True),
        (30000, 137, True),
        (30000, 205, False),
        (10000, 205, True),
    ],
)
def test_timestamp_window_uses_processor_sample_rate(rate, offset_ms, valid):
    processor = RollingBufferProcessor(sample_rate=rate)
    capture = IQCapture(100.0, 100.0 + offset_ms / 1000, [2048] * 4096, [2048] * 4096)
    assert processor.has_consistent_capture_timing(capture) is valid


@pytest.mark.parametrize("segments,offset_ms", [(8, 4), (16, 4), (16, 34), (8, 68)])
def test_capture_with_wrong_trigger_split_is_rejected_and_split_resent(segments, offset_ms):
    processor = RollingBufferProcessor()
    capture = IQCapture(100.0, 100.0 + offset_ms / 1000, [2048] * 4096, [2048] * 4096)
    processor.parse_capture = Mock(return_value=capture)
    processor.process_standard = Mock()
    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = "field capture"
    radar.last_hardware_trigger_first_byte_timestamp = 101.0
    trigger = SoundTrigger(pre_trigger_segments=segments)

    assert trigger.wait_for_trigger(radar, processor, timeout=1) is None

    processor.process_standard.assert_not_called()
    radar.rearm_rolling_buffer.assert_called_once_with(segments)
    assert trigger.drain_diagnostics()[0]["reason"] == "trigger_split_mismatch"


@pytest.mark.parametrize(
    "rate,segments,offset_ms,expected",
    [
        (30000, 16, 68, True),
        (30000, 16, 70, True),
        (30000, 16, 67, True),
        (30000, 16, 71, False),
        (30000, 16, 4, False),
        (30000, 8, 34, True),
        (30000, 24, 102, True),
        (30000, 0, 0, True),
        (20000, 16, 102, True),
    ],
)
def test_trigger_split_check_uses_configured_segments(rate, segments, offset_ms, expected):
    processor = RollingBufferProcessor(sample_rate=rate)
    capture = IQCapture(100.0, 100.0 + offset_ms / 1000, [2048] * 4096, [2048] * 4096)
    assert processor.has_expected_trigger_split(capture, segments) is expected
