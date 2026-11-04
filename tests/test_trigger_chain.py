"""Sound-trigger chain: OPS rejection releases TI early, camera and TI stay independent."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openflight.camera.capture_runtime import (
    CameraCaptureRuntime,
    CameraCaptureSettings,
    SavedCameraCapture,
)
from openflight.iwr6843.driver import DumpCancelled
from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.monitor import IWR6843Capture, IWR6843CaptureMonitor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.rolling_buffer.types import IQCapture
from openflight.session_logger import SessionLogger

FIRST_BYTE = 100.114


def _ops_radar(events: list, response: str = "complete dump", lost_bytes: int = 0):
    radar = Mock()
    radar.last_hardware_trigger_first_byte_timestamp = FIRST_BYTE
    radar.last_hardware_trigger_lost_capture_bytes = lost_bytes

    def wait_for_hardware_trigger(timeout, cancel_event=None, on_first_byte=None):
        if on_first_byte is not None:
            on_first_byte()
        return response

    radar.wait_for_hardware_trigger.side_effect = wait_for_hardware_trigger
    radar.rearm_rolling_buffer.side_effect = lambda *_args: events.append("rearm") or True
    radar.set_sample_rate.side_effect = lambda *_args: events.append("sample_rate")
    return radar


def _ops_processor(*, parsed=True, timing_ok=True, split_ok=True):
    processor = Mock()
    processor.SAMPLE_RATE = 30000
    processor.parse_capture.return_value = (
        IQCapture(100.0, 100.0 + 0.0341, [2048] * 4096, [2048] * 4096) if parsed else None
    )
    processor.has_consistent_capture_timing.return_value = timing_ok
    processor.has_expected_trigger_split.return_value = split_ok
    processor.process_standard.return_value.readings = []
    return processor


@pytest.mark.parametrize(
    ("reason", "radar_kwargs", "processor_kwargs"),
    [
        ("lost_capture", {"response": "", "lost_bytes": 30000}, {}),
        ("parse_failed", {}, {"parsed": False}),
        ("capture_timebase_mismatch", {}, {"timing_ok": False}),
        ("trigger_split_mismatch", {}, {"split_ok": False}),
        ("no_outbound_speed", {}, {}),
    ],
)
def test_rejection_callback_fires_before_rearm_for_every_reason(
    reason, radar_kwargs, processor_kwargs
):
    events = []
    radar = _ops_radar(events, **radar_kwargs)
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_rejection_callback(lambda ts, why: events.append(("reject", ts, why)))

    assert trigger.wait_for_trigger(radar, _ops_processor(**processor_kwargs), timeout=1) is None

    assert events[0] == ("reject", FIRST_BYTE, reason)
    assert events[-1] == "rearm"
    assert events.count(("reject", FIRST_BYTE, reason)) == 1
    assert trigger.drain_diagnostics()[0]["reason"] == reason


def test_timebase_rejection_releases_ti_before_the_sample_rate_command():
    events = []
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_rejection_callback(lambda ts, why: events.append("reject"))

    trigger.wait_for_trigger(_ops_radar(events), _ops_processor(timing_ok=False), timeout=1)

    assert events == ["reject", "sample_rate", "rearm"]


def test_failing_rejection_callback_never_blocks_the_rearm(caplog):
    events = []
    radar = _ops_radar(events)
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_rejection_callback(Mock(side_effect=RuntimeError("TI gone")))

    with caplog.at_level(logging.WARNING):
        assert trigger.wait_for_trigger(radar, _ops_processor(), timeout=1) is None

    assert events == ["rearm"]
    assert "rejection callback failed" in caplog.text


def test_accepted_dump_does_not_fire_the_rejection_callback():
    events = []
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_rejection_callback(lambda ts, why: events.append("reject"))
    trigger._summarize_capture_activity = Mock(
        return_value={
            "total_readings": 3,
            "outbound_readings": 3,
            "inbound_readings": 0,
            "peak_outbound_mph": 90.0,
            "peak_inbound_mph": 0.0,
            "all_outbound_speeds": [90.0],
            "all_inbound_speeds": [],
            "peak_outbound_magnitude": 1.0,
            "peak_inbound_magnitude": 0.0,
            "valid_outbound_count": 3,
            "valid_peak_outbound_mph": 90.0,
        }
    )
    trigger._select_clock_sync_for_capture = Mock()

    assert trigger.wait_for_trigger(_ops_radar(events), _ops_processor(), timeout=1) is not None
    assert events == ["rearm"]


def test_dump_lifecycle_callbacks_bracket_the_ops_dump():
    events = []
    radar = _ops_radar(events)
    trigger = SoundTrigger(pre_trigger_segments=8)
    trigger.set_dump_callbacks(
        started=lambda ts: events.append(("started", ts)),
        finished=lambda: events.append("finished"),
    )
    capture_started = Mock(side_effect=lambda: events.append("ui"))

    trigger.wait_for_trigger(
        radar, _ops_processor(), timeout=1, capture_started_callback=capture_started
    )

    assert events == [("started", FIRST_BYTE), "ui", "finished", "rearm"]


def test_rolling_buffer_monitor_plumbs_trigger_callbacks():
    from openflight.rolling_buffer import RollingBufferMonitor

    class IdleRadar:
        def wait_for_hardware_trigger(self, timeout, cancel_event=None, on_first_byte=None):
            cancel_event.wait(timeout)
            return ""

    rejected, started, finished = Mock(), Mock(), Mock()
    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    monitor.radar = IdleRadar()
    monitor.start(
        trigger_rejected_callback=rejected,
        ops_dump_started_callback=started,
        ops_dump_finished_callback=finished,
    )
    try:
        assert monitor.trigger._rejection_callback is rejected
        assert monitor.trigger._dump_started_callback is started
        assert monitor.trigger._dump_finished_callback is finished
    finally:
        monitor.stop()


def test_server_forwards_ops_rejection_to_ti_cancel(monkeypatch):
    from openflight import server as server_module

    capture_monitor = Mock()
    monkeypatch.setattr(
        server_module, "iwr6843_runtime", SimpleNamespace(capture_monitor=capture_monitor)
    )

    server_module._reject_ti_for_ops_rejection(FIRST_BYTE, "parse_failed")
    server_module._notify_ti_ops_dump_started(FIRST_BYTE)
    server_module._notify_ti_ops_dump_finished()

    capture_monitor.reject_trigger.assert_called_once_with(FIRST_BYTE)
    capture_monitor.notify_ops_dump_started.assert_called_once_with(FIRST_BYTE)
    capture_monitor.notify_ops_dump_finished.assert_called_once_with()

    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    server_module._reject_ti_for_ops_rejection(FIRST_BYTE, "parse_failed")
    server_module._notify_ti_ops_dump_started(FIRST_BYTE)
    server_module._notify_ti_ops_dump_finished()


def test_reject_trigger_is_idempotent_per_edge(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
        cancel_false_triggers=True,
    )
    monitor._running = monitor._armed = monitor._capture_active = True
    monitor._last_edge_timestamp = 100.0

    assert monitor.reject_trigger(FIRST_BYTE)
    first_cancel = monitor._cancel_dump
    assert not monitor.reject_trigger(FIRST_BYTE)
    assert monitor._cancel_dump is first_cancel and first_cancel.is_set()


class FakeButton:
    def __init__(self, pin, pull_up, bounce_time):
        self.when_pressed = None

    def close(self):
        pass


class BlockingRadar:
    """TI double whose dump streams until released or cancelled."""

    port = "/dev/fake-iwr6843"

    def __init__(self):
        self.raw = pack_dump(np.zeros((2, 4, 4, 8), dtype=complex), n_tx=2, version=3)
        self.reads = 0
        self.read_started = threading.Event()
        self.release = threading.Event()
        self.cancel_seen = threading.Event()

    def send_config(self, _path):
        pass

    def read_dump(self, cancel_event=None):
        self.reads += 1
        self.read_started.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_seen.set()
                raise DumpCancelled("OPS rejected acoustic trigger; TI re-armed")
            if self.release.is_set():
                return self.raw
            time.sleep(0.005)
        return self.raw

    def stop_sensor(self, *, retry=True):
        pass

    def close(self):
        pass


def _ti_monitor(tmp_path, radar, **kwargs):
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        cancel_false_triggers=True,
        **kwargs,
    )
    monitor.start()
    return monitor


def test_observers_fan_out_while_ti_dump_is_busy(tmp_path):
    observed = []
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar, trigger_observers=[observed.append])
    try:
        edge = time.time()
        assert monitor.notify_trigger(edge)
        assert radar.read_started.wait(1.0)
        assert monitor.dump_in_progress

        assert not monitor.notify_trigger(edge + 0.05)
        assert not monitor.notify_trigger(edge + 0.6)

        assert observed == [edge, edge + 0.6]
        assert radar.reads == 1
    finally:
        radar.release.set()
        monitor.stop()
    assert not monitor.dump_in_progress


def _camera_runtime(tmp_path, **settings):
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path / "camera",
        settings=CameraCaptureSettings(auto_exposure=False, **settings),
        use_gpio_trigger=False,
        vertical_offset_path=tmp_path / "offset",
    )
    saved_at = []

    def fake_save(sequence, trigger_epoch, _capture, *, auto_exposure=None):
        saved_at.append(time.monotonic())
        return SavedCameraCapture(
            sequence=sequence,
            trigger_timestamp=trigger_epoch,
            completed_timestamp=time.time(),
            path=tmp_path / f"clip{sequence}",
            metadata={},
        )

    runtime._save_capture = fake_save
    runtime._running = True
    worker = threading.Thread(target=runtime._save_loop, daemon=True)
    worker.start()
    return runtime, saved_at, worker


def _queue_clip(runtime, epoch):
    runtime._trigger_epochs.put(epoch)
    runtime._ready.put(object())


def test_camera_defers_clip_write_while_ti_dump_streams(tmp_path):
    dumping = threading.Event()
    dumping.set()
    runtime, saved_at, worker = _camera_runtime(tmp_path)
    runtime.set_write_deferral(dumping.is_set)
    try:
        _queue_clip(runtime, 100.0)
        time.sleep(0.2)
        assert saved_at == []

        released = time.monotonic()
        dumping.clear()
        deadline = time.monotonic() + 1.0
        while not saved_at and time.monotonic() < deadline:
            time.sleep(0.01)
        assert saved_at and saved_at[0] >= released
    finally:
        runtime._running = False
        runtime._ready.put(None)
        worker.join(1.0)


def test_camera_writes_immediately_when_deferral_queue_is_full(tmp_path):
    runtime, saved_at, worker = _camera_runtime(tmp_path)
    runtime.set_write_deferral(lambda: True)
    try:
        for index in range(CameraCaptureRuntime.MAX_DEFERRED_CLIPS + 1):
            _queue_clip(runtime, 100.0 + index)
        deadline = time.monotonic() + 1.0
        while not saved_at and time.monotonic() < deadline:
            time.sleep(0.01)
        assert saved_at
    finally:
        runtime._running = False
        runtime._ready.put(None)
        worker.join(1.0)


def test_waiting_shot_flushes_a_deferred_camera_clip(tmp_path):
    runtime, saved_at, worker = _camera_runtime(tmp_path)
    runtime.set_write_deferral(lambda: True)
    try:
        _queue_clip(runtime, 100.004)
        time.sleep(0.1)
        assert saved_at == []
        capture = runtime.capture_for_shot(100.0, timeout_s=1.0)
        assert capture is not None and capture.trigger_timestamp == 100.004
    finally:
        runtime._running = False
        runtime._ready.put(None)
        worker.join(1.0)


def _saved(sequence, epoch, tmp_path):
    return SavedCameraCapture(
        sequence=sequence,
        trigger_timestamp=epoch,
        completed_timestamp=epoch + 0.2,
        path=tmp_path / f"clip{sequence}",
        metadata={},
    )


def test_camera_picks_the_nearest_clip_in_the_window(tmp_path):
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path,
        settings=CameraCaptureSettings(auto_exposure=False),
        use_gpio_trigger=False,
        vertical_offset_path=tmp_path / "offset",
    )
    runtime._captures = [
        _saved(1, 99.97, tmp_path),
        _saved(2, 100.004, tmp_path),
        _saved(3, 100.2, tmp_path),
    ]

    assert runtime.settings.match_tolerance_s == pytest.approx(0.05)
    assert runtime.capture_for_shot(100.0, timeout_s=0.05).sequence == 2


def test_camera_does_not_match_a_clip_outside_the_window(tmp_path):
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path,
        settings=CameraCaptureSettings(auto_exposure=False),
        use_gpio_trigger=False,
        vertical_offset_path=tmp_path / "offset",
    )
    runtime._captures = [_saved(1, 100.397, tmp_path)]

    assert runtime.capture_for_shot(100.0, timeout_s=0.05) is None


def test_ti_edge_during_ops_dump_starts_no_l3dump(tmp_path, caplog):
    observed = []
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar, trigger_observers=[observed.append])
    monitor.enable_ops_dump_gating()
    try:
        now = time.time()
        monitor.notify_ops_dump_started(now - 0.3)
        with caplog.at_level(logging.INFO):
            assert not monitor.notify_trigger(now)
        time.sleep(0.05)
        assert radar.reads == 0
        assert observed == [now]
        assert "OPS dump in progress" in caplog.text

        monitor.notify_ops_dump_finished()
        assert monitor.notify_trigger(now + 0.5)
        monitor.notify_ops_dump_started(now + 0.614)
        assert radar.read_started.wait(1.0)
    finally:
        radar.release.set()
        monitor.stop()


def test_stale_ops_dump_flag_does_not_block_ti_forever(tmp_path):
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar)
    monitor.enable_ops_dump_gating()
    try:
        now = time.time()
        monitor.notify_ops_dump_started(now - IWR6843CaptureMonitor.OPS_DUMP_MAX_S - 1.0)
        assert monitor.notify_trigger(now)
        monitor.notify_ops_dump_started(now + 0.11)
    finally:
        radar.release.set()
        monitor.stop()


def test_ti_dump_without_ops_first_byte_is_cancelled(tmp_path, caplog):
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar)
    monitor.enable_ops_dump_gating(first_byte_timeout_s=0.05)
    try:
        edge = time.time()
        with caplog.at_level(logging.INFO):
            assert monitor.notify_trigger(edge)
            assert radar.cancel_seen.wait(1.0)
        assert "no OPS first byte" in caplog.text
    finally:
        monitor.stop()


def test_ops_first_byte_in_time_keeps_the_ti_dump(tmp_path):
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar)
    monitor.enable_ops_dump_gating(first_byte_timeout_s=0.05)
    try:
        edge = time.time()
        assert monitor.notify_trigger(edge)
        monitor.notify_ops_dump_started(edge + 0.02)
        time.sleep(0.15)
        assert not radar.cancel_seen.is_set()
        radar.release.set()
        capture = monitor.capture_for_shot(edge + 0.004, timeout_s=1.0)
        assert capture is not None and capture.valid
    finally:
        radar.release.set()
        monitor.stop()


def test_late_ops_first_byte_only_costs_the_ti_angle(tmp_path, caplog):
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar)
    monitor.enable_ops_dump_gating(first_byte_timeout_s=0.05)
    try:
        edge = time.time()
        assert monitor.notify_trigger(edge)
        assert radar.cancel_seen.wait(1.0)
        with caplog.at_level(logging.WARNING):
            monitor.notify_ops_dump_started(edge + 0.2)
        assert "TI angle lost" in caplog.text
        capture = monitor.capture_for_shot(edge + 0.004, timeout_s=1.0)
        assert capture is not None and not capture.valid
    finally:
        monitor.stop()


def test_gating_is_off_until_enabled(tmp_path):
    radar = BlockingRadar()
    monitor = _ti_monitor(tmp_path, radar)
    try:
        monitor.notify_ops_dump_started(time.time())
        assert monitor.notify_trigger(time.time())
        time.sleep(0.6)
        assert not radar.cancel_seen.is_set()
    finally:
        radar.release.set()
        monitor.stop()


def _idle_ti_monitor(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
    )
    monitor._running = monitor._armed = True
    return monitor


def _completed(sequence, edge, completed, error=None):
    return IWR6843Capture(
        sequence=sequence,
        trigger_timestamp=edge,
        completed_timestamp=completed,
        dump_duration_s=completed - edge,
        raw=None if error else b"raw",
        path=None,
        error=error,
    )


def test_late_ti_edge_is_consumed_as_out_of_window(tmp_path, caplog):
    impact = 1000.0
    monitor = _idle_ti_monitor(tmp_path)
    cancelled = _completed(1, impact - 2.391, impact + 0.309, error="OPS rejected")
    late = _completed(2, impact + 0.397, impact + 7.5)
    monitor._captures.extend([cancelled, late])
    monitor._dump_history.extend(
        [(c.trigger_timestamp, c.completed_timestamp) for c in (cancelled, late)]
    )

    assert monitor.match_tolerance_s == pytest.approx(0.05)
    with caplog.at_level(logging.WARNING):
        capture = monitor.capture_for_shot(impact, timeout_s=0.1)

    assert capture is not None and not capture.valid and capture.raw is None
    assert capture.error == "ti_edge_out_of_window"
    assert capture.nearest_trigger_delta_ms == pytest.approx(397.0)
    assert capture.ti_busy_at_impact is True
    assert capture.ti_ready_after_impact_ms == pytest.approx(309.0)
    assert not monitor._captures
    assert "Nearest TI edge +397 ms from impact (outside ±50 ms)" in caplog.text
    assert "TI busy at impact until +309 ms" in caplog.text


def test_in_window_capture_wins_over_a_nearer_out_of_window_one(tmp_path):
    impact = 1000.0
    monitor = _idle_ti_monitor(tmp_path)
    monitor._captures.extend(
        [_completed(1, impact + 0.008, impact + 7.1), _completed(2, impact + 0.3, impact + 7.4)]
    )

    capture = monitor.capture_for_shot(impact, timeout_s=0.1)

    assert capture.sequence == 1 and capture.valid


def test_active_out_of_window_dump_does_not_make_the_shot_wait(tmp_path):
    impact = time.time() - 2.0
    monitor = _idle_ti_monitor(tmp_path)
    monitor._capture_active = True
    monitor._sequence = 4
    monitor._last_edge_timestamp = impact + 0.574

    started = time.monotonic()
    capture = monitor.capture_for_shot(impact, timeout_s=5.0)

    assert time.monotonic() - started < 0.5
    assert capture.error == "ti_edge_out_of_window"
    assert capture.nearest_trigger_delta_ms == pytest.approx(574.0)
    assert capture.ti_busy_at_impact is False

    monitor._finish_capture(_completed(4, impact + 0.574, impact + 7.6))
    assert not monitor._captures


def test_edge_beyond_one_second_is_not_consumed(tmp_path):
    impact = 1000.0
    monitor = _idle_ti_monitor(tmp_path)
    monitor._captures.append(_completed(1, impact + 1.2, impact + 8.3))

    assert monitor.capture_for_shot(impact, timeout_s=0.05) is None
    assert len(monitor._captures) == 1


def test_out_of_window_record_reaches_the_session_jsonl(tmp_path, monkeypatch):
    from datetime import datetime

    from openflight import server as server_module
    from openflight.launch_monitor import ClubType, Shot

    capture = IWR6843Capture(
        sequence=7,
        trigger_timestamp=1000.397,
        completed_timestamp=1002.0,
        dump_duration_s=0.0,
        raw=None,
        path=None,
        error="ti_edge_out_of_window",
        nearest_trigger_delta_ms=397.0,
        ti_busy_at_impact=True,
        ti_ready_after_impact_ms=309.0,
    )
    runtime = SimpleNamespace(
        process_shot=lambda **_kwargs: SimpleNamespace(capture=capture, measurement=None)
    )
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
    monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
    monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)

    shot = Shot(
        ball_speed_mph=100.0,
        club_speed_mph=80.0,
        timestamp=datetime.now(),
        impact_timestamp=1000.0,
        club=ClubType.IRON_9,
    )
    server_module._process_iwr6843_angle(shot)
    session.end_session()

    records = [
        json.loads(line)
        for path in Path(tmp_path).glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    record = next(r for r in records if r.get("type") == "iwr6843_capture")
    assert record["capture_error"] == "ti_edge_out_of_window"
    assert record["nearest_trigger_delta_ms"] == pytest.approx(397.0)
    assert record["ti_busy_at_impact"] is True
    assert record["ti_ready_after_impact_ms"] == pytest.approx(309.0)
    assert record["measurement"] is None
    assert shot.launch_angle_vertical is None
