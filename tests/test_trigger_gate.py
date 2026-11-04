"""Pi-driven OPS HOST_INT trigger gate: passthrough, camera gating, timebase, wiring."""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from openflight import server as server_module
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.trigger_check import (
    LiveDepartureCheck,
    check_ball_departure,
)
from openflight.camera.triggered_buffer import CameraFrame, TriggeredFrameBuffer
from openflight.iwr6843 import Calibration
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.rolling_buffer import RollingBufferMonitor
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.trigger import SoundTrigger
from openflight.rolling_buffer.types import IQCapture, SpeedTimeline
from openflight.session_logger import SessionLogger
from openflight.trigger_gate import (
    FULL_EDGE_HISTORY_MS,
    MIN_EDGE_HISTORY_MS,
    EdgeListener,
    GateRecord,
    TriggerGate,
    camera_split_check,
    pre_trigger_ms,
    recommended_camera_split,
)
from tests.test_trigger_chain import BlockingRadar, FakeButton

EDGE_NS = 5_000_000_000
EDGE_EPOCH = 1_760_000_000.0
CALLBACK_LAG_NS = 300_000
FRAME_NS = 3_333_333
DELIVERY_NS = 4_600_000
HEIGHT, WIDTH = 200, 320
BALL = (160.0, 130.0)
RADIUS = 7
ANCHOR = ReferenceBall(BALL[0], BALL[1], 2 * RADIUS + 1, 177)
CLUB_FIXTURE = Path(__file__).parent / "fixtures" / "club_speed" / "ops243_club_speed_captures.npz"


class FakeClock:
    """Monotonic ns and wall-clock seconds that only move when told."""

    def __init__(self, ns: int = EDGE_NS + CALLBACK_LAG_NS):
        self.ns = ns

    def monotonic_ns(self) -> int:
        return self.ns

    def wall(self) -> float:
        return EDGE_EPOCH + (self.ns - EDGE_NS) / 1e9

    def advance_ms(self, ms: float) -> None:
        self.ns += round(ms * 1e6)

    def at_ms(self, ms: float) -> None:
        self.ns = EDGE_NS + round(ms * 1e6)


class FakeOutput:
    """gpiozero DigitalOutputDevice double logging on/off at the fake clock."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.events: list[tuple[str, float]] = []
        self.created_with = None
        self.closed = False

    def factory(self, pin, *, active_high, initial_value):
        self.created_with = (pin, active_high, initial_value)
        return self

    def on(self):
        self.events.append(("on", (self.clock.ns - EDGE_NS) / 1e6))

    def off(self):
        self.events.append(("off", (self.clock.ns - EDGE_NS) / 1e6))

    def close(self):
        self.closed = True

    @property
    def pulses(self) -> list[float]:
        return [at_ms for kind, at_ms in self.events if kind == "on"]


class FakeCamera:
    """Live ring double: frames become visible at their host time on the fake clock."""

    def __init__(self, clock: FakeClock, frames, anchor=ANCHOR, running=True):
        self.clock = clock
        self.frames = list(frames)
        self.anchor = anchor
        self.running = running
        self.calls = 0
        self.blocked_edges: list[float] = []

    def trigger_check_anchor(self):
        return self.anchor

    def note_blocked_edge(self, edge_timestamp):
        self.blocked_edges.append(edge_timestamp)

    def live_frames_after(self, after_ns, timeout_s=0.0):
        self.calls += 1
        if not any(after_ns < f.host_timestamp_ns <= self.clock.ns for f in self.frames):
            upcoming = [f.host_timestamp_ns for f in self.frames if f.host_timestamp_ns > after_ns]
            limit = self.clock.ns + round(timeout_s * 1e9)
            self.clock.ns = max(self.clock.ns, min(upcoming + [limit]) if upcoming else limit)
        return tuple(f for f in self.frames if after_ns < f.host_timestamp_ns <= self.clock.ns)


def render_frames(
    motion: str = "stays",
    *,
    scene: str = "front_light",
    club_over: bool = False,
    last_ms: float = 60.0,
    seed: int = 0,
) -> list[CameraFrame]:
    """300 fps frames from 150 ms before to ``last_ms`` after the edge, delivered 4.6 ms late."""
    levels = {
        "front_light": {"mat": 60, "ball": 220, "club": 25},
        "backlight": {"mat": 255, "ball": 40, "club": 15},
    }[scene]
    rng = np.random.default_rng(seed)
    texture = rng.integers(-6, 7, size=(HEIGHT, WIDTH))
    yy, xx = np.mgrid[0:HEIGHT, 0:WIDTH]
    frames = []
    for k in range(-45, math.floor(last_ms * 1e6 / FRAME_NS) + 1):
        offset_ms = k * FRAME_NS / 1e6
        image = levels["mat"] + texture + rng.normal(0.0, 3.0, size=(HEIGHT, WIDTH))
        if motion == "stays" or offset_ms < 8.0:
            image[(xx - BALL[0]) ** 2 + (yy - BALL[1]) ** 2 <= RADIUS**2] = levels["ball"]
        if club_over and 0.0 <= offset_ms <= 12.0:
            left = int(BALL[0]) - 50 + round(5.0 * offset_ms)
            image[118:142, left : left + 40] = levels["club"]
        sensor_ns = EDGE_NS + k * FRAME_NS
        frames.append(
            CameraFrame(
                image=np.clip(image, 0, 255).astype(np.uint8),
                sensor_timestamp_ns=sensor_ns,
                host_timestamp_ns=sensor_ns + DELIVERY_NS,
                exposure_us=100,
                analogue_gain=2.0,
            )
        )
    return frames


def make_gate(
    clock: FakeClock, mode: str = "passthrough", **kwargs
) -> tuple[TriggerGate, FakeOutput]:
    output = FakeOutput(clock)
    gate = TriggerGate(
        pin=27,
        mode=mode,
        output_factory=output.factory,
        monotonic_ns=clock.monotonic_ns,
        wall_time=clock.wall,
        background=False,
        **kwargs,
    )
    gate.start()
    return gate, output


def only_record(gate: TriggerGate) -> GateRecord:
    records = list(gate._records)
    assert len(records) == 1
    return records[0]


def test_passthrough_pulses_once_per_edge_within_budget(caplog):
    clock = FakeClock()
    pulses_seen = []
    gate, output = make_gate(clock, on_pulse=lambda: pulses_seen.append(clock.ns))
    assert output.created_with == (27, True, False)

    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        assert output.pulses == [pytest.approx(0.3)]
        record = only_record(gate)
        assert record.decision == "passthrough"
        assert record.delay_ms == pytest.approx(0.3, abs=0.01)
        assert record.delay_ms < 2.0
        gate.run_pending()

    assert pulses_seen and "pulse +0.3 ms (passthrough)" in caplog.text
    assert "Late pulse" not in caplog.text
    clock.at_ms(19.0)
    gate.run_due_timers()
    assert output.events[-1][0] == "on"
    clock.at_ms(20.4)
    gate.run_due_timers()
    assert output.events == [("on", pytest.approx(0.3)), ("off", pytest.approx(20.4))]


def test_passthrough_late_pulse_warns(caplog):
    clock = FakeClock(EDGE_NS + 7_200_000)
    gate, output = make_gate(clock)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        gate.run_pending()
    assert output.pulses == [pytest.approx(7.2)]
    assert "Late pulse for edge #1: +7.2 ms" in caplog.text
    assert gate.summary()["late"] == 1


def test_pulse_end_never_cuts_a_newer_pulse_short():
    clock = FakeClock()
    gate, output = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    clock.at_ms(15.0)
    gate.on_edge(EDGE_EPOCH + 0.0147)
    clock.at_ms(21.0)
    gate.run_due_timers()
    assert [kind for kind, _ in output.events] == ["on", "on"]
    clock.at_ms(35.0)
    gate.run_due_timers()
    assert [kind for kind, _ in output.events] == ["on", "on", "off"]


def test_stopped_gate_ignores_edges_and_releases_the_output():
    clock = FakeClock()
    gate, output = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    gate.stop()
    assert output.events[-1][0] == "off" and output.closed
    gate.on_edge(EDGE_EPOCH + 1.0)
    assert len(output.pulses) == 1


def test_edge_listener_filters_ringing_like_the_ti_monitor(tmp_path):
    edges = [EDGE_EPOCH, EDGE_EPOCH + 0.05, EDGE_EPOCH + 0.099, EDGE_EPOCH + 0.6]

    listened = []
    listener = EdgeListener(pin=17, observers=[listened.append], button_factory=FakeButton)
    listener.start()
    assert not listener.notify_edge(edges[0])
    listener.arm()
    for edge in edges:
        listener.notify_edge(edge)
    listener.stop()

    ti_seen = []
    radar = BlockingRadar()
    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=radar,
        button_factory=FakeButton,
        cancel_false_triggers=True,
        trigger_observers=[ti_seen.append],
    )
    monitor.start()
    try:
        for edge in edges:
            monitor.notify_trigger(edge)
    finally:
        radar.release.set()
        monitor.stop()

    assert listened == ti_seen == [edges[0], edges[3]]
    assert EdgeListener.RING_FILTER_S == IWR6843CaptureMonitor.RING_FILTER_S


def test_edge_listener_fans_out_to_gate_then_camera_and_survives_observer_errors():
    calls = []
    listener = EdgeListener(
        pin=17,
        observers=[
            lambda edge: calls.append(("gate", edge)),
            Mock(side_effect=RuntimeError("camera gone")),
            lambda edge: calls.append(("after", edge)),
        ],
        button_factory=FakeButton,
        wall_time=lambda: EDGE_EPOCH,
    )
    listener.start()
    listener.arm()
    assert listener._button.when_pressed == listener.notify_edge
    assert listener.notify_edge()
    assert calls == [("gate", EDGE_EPOCH), ("after", EDGE_EPOCH)]


def camera_gate(clock, frames, *, released=None, **camera_kwargs):
    camera = FakeCamera(clock, frames, **camera_kwargs)

    def release(edge):
        released.append(edge)
        return True

    gate, output = make_gate(
        clock,
        "camera",
        camera=camera,
        on_pulse=Mock(),
        release_edge=release if released is not None else None,
    )
    return gate, output, camera


@pytest.mark.parametrize("scene", ["front_light", "backlight"])
def test_camera_departed_pulses_as_soon_as_the_ball_is_gone(scene, caplog):
    clock = FakeClock()
    released = []
    gate, output, _ = camera_gate(clock, render_frames("fast", scene=scene), released=released)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        assert output.pulses == []
        gate.run_pending()

    record = only_record(gate)
    assert record.decision == "departed" and record.reason == "ball_left_spot"
    assert 8.0 <= record.first_departure_ms <= 15.0
    assert record.delay_ms == pytest.approx(record.first_departure_ms + 4.6, abs=0.01)
    assert record.delay_ms < gate.deadline_ms
    assert output.pulses == [pytest.approx(record.delay_ms, abs=0.01)]
    assert record.frames_used >= 2 and record.frame_states.endswith("GG")
    assert released == []
    gate._on_pulse.assert_called_once_with()
    assert "departed at +" in caplog.text


@pytest.mark.parametrize("club_over", [False, True])
def test_camera_still_there_blocks_and_releases_the_ti_dump(club_over, caplog):
    clock = FakeClock()
    released = []
    gate, output, camera = camera_gate(
        clock, render_frames("stays", club_over=club_over), released=released
    )
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        gate.run_pending()
        clock.at_ms(200.0)
        gate.run_due_timers()

    record = only_record(gate)
    assert record.decision == "still_there"
    assert record.reason == ("ball_visible_after_occlusion" if club_over else "ball_unchanged")
    assert output.pulses == []
    assert not record.pulsed and record.delay_ms is None
    assert 35.0 <= record.decision_latency_ms <= gate.deadline_ms
    assert released == [EDGE_EPOCH] and record.ti_released
    # The camera learns which edge was blocked (its clip may prove the anchor stale).
    assert camera.blocked_edges == [EDGE_EPOCH]
    gate._on_pulse.assert_not_called()
    assert "blocked: still_there after" in caplog.text and "TI released" in caplog.text
    assert gate.summary()["blocked"] == 1


def test_pulsed_edges_are_not_reported_as_blocked_and_a_failing_report_is_harmless(caplog):
    clock = FakeClock()
    gate, output, camera = camera_gate(clock, render_frames("fast"))
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    assert output.pulses and camera.blocked_edges == []

    clock = FakeClock()
    gate, output, camera = camera_gate(clock, render_frames("stays"))
    camera.note_blocked_edge = Mock(side_effect=RuntimeError("anchor gone"))
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        gate.run_pending()
    assert only_record(gate).decision == "still_there" and output.pulses == []
    assert "Reporting blocked edge #1 failed" in caplog.text
    assert gate.summary()["blocked"] == 1


@pytest.mark.parametrize(
    ("camera_kwargs", "frames", "reason"),
    [
        ({"anchor": None}, render_frames("stays"), "no_anchor"),
        ({"running": False}, render_frames("stays"), "camera_off"),
        ({}, render_frames("stays", last_ms=-30.0), "insufficient_post_frames"),
        ({}, render_frames("stays", last_ms=25.0), "window_incomplete"),
        ({}, render_frames("stays")[-12:], "insufficient_pre_frames"),
        (
            {"anchor": ReferenceBall(40.0, 40.0, 15.0, 177)},
            render_frames("stays"),
            "ball_not_found",
        ),
    ],
)
def test_camera_unclear_always_pulses(camera_kwargs, frames, reason):
    clock = FakeClock()
    released = []
    gate, output, _ = camera_gate(clock, frames, released=released, **camera_kwargs)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    clock.at_ms(200.0)
    gate.run_due_timers()

    record = only_record(gate)
    assert (record.decision, record.reason) == ("unclear", reason)
    assert len(output.pulses) == 1 and record.pulsed
    assert record.delay_ms <= gate.deadline_ms
    assert released == []
    assert gate.summary()["unclear_reasons"] == {reason: 1}


def shadow_gate(clock, frames, **camera_kwargs):
    """A passthrough gate with a live camera: it pulses at once and only shadows the camera."""
    camera = FakeCamera(clock, frames, **camera_kwargs)
    gate, output = make_gate(clock, "passthrough", camera=camera, on_pulse=Mock())
    return gate, output, camera


@pytest.mark.parametrize(
    ("motion", "decision", "reason"),
    [("fast", "departed", "ball_left_spot"), ("stays", "still_there", "ball_unchanged")],
)
def test_passthrough_shadow_logs_what_camera_mode_would_do(motion, decision, reason, caplog):
    clock = FakeClock()
    gate, output, camera = shadow_gate(clock, render_frames(motion))
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        # The pulse never waits for the camera.
        assert output.pulses == [pytest.approx(0.3)]
        gate.run_pending()
        clock.at_ms(200.0)
        gate.run_due_timers()

    record = only_record(gate)
    assert (record.decision, record.reason) == ("passthrough", "passthrough")
    assert output.pulses == [pytest.approx(0.3)]
    assert record.delay_ms == pytest.approx(0.3, abs=0.01)
    assert (record.shadow_decision, record.shadow_reason) == (decision, reason)
    assert camera.blocked_edges == []
    gate._on_pulse.assert_called_once_with()
    fields = record.log_fields()
    assert fields["gate_decision"] == "passthrough"
    assert fields["gate_shadow_decision"] == decision
    assert fields["gate_shadow_frame_states"] == record.shadow_frame_states != ""
    if decision == "departed":
        assert 8.0 <= fields["gate_shadow_first_departure_ms"] <= 15.0
        assert "camera shadow -> would pulse: departed at +" in caplog.text
    else:
        assert fields["gate_shadow_first_departure_ms"] is None
        assert "camera shadow -> would block: still_there (ball_unchanged)" in caplog.text
    summary = gate.summary()
    assert summary["blocked"] == 0 and summary["pulsed"] == 1
    assert summary["shadow"] == {"decisions": {decision: 1}, "unclear_reasons": {}}
    blocks = int(decision == "still_there")
    assert f"camera shadow: would block {blocks} of 1" in gate.summary_text()


@pytest.mark.parametrize(("scene", "motion"), [("front_light", "fast"), ("backlight", "stays")])
def test_passthrough_shadow_matches_camera_mode_on_the_same_frames(scene, motion):
    frames = render_frames(motion, scene=scene, club_over=True)
    clock = FakeClock()
    gate, _output, _camera = camera_gate(clock, frames)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    live = only_record(gate)
    clock = FakeClock()
    gate, _output, _camera = shadow_gate(clock, frames)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    shadow = only_record(gate)
    assert (shadow.shadow_decision, shadow.shadow_reason) == (live.decision, live.reason)
    assert shadow.shadow_frame_states == live.frame_states


@pytest.mark.parametrize(
    ("camera_kwargs", "reason"),
    [({"anchor": None}, "no_anchor"), ({"running": False}, "camera_off")],
)
def test_passthrough_shadow_unclear_is_counted_by_reason(camera_kwargs, reason):
    clock = FakeClock()
    gate, output, _ = shadow_gate(clock, render_frames("stays"), **camera_kwargs)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    record = only_record(gate)
    assert (record.shadow_decision, record.shadow_reason) == ("unclear", reason)
    assert output.pulses == [pytest.approx(0.3)]
    assert gate.summary()["shadow"]["unclear_reasons"] == {reason: 1}
    assert f"unclear 1: {reason} 1" in gate.summary_text()


def test_passthrough_shadow_failure_or_lateness_never_touches_the_pulse(caplog):
    clock = FakeClock()
    gate, output, camera = shadow_gate(clock, render_frames("stays"))
    camera.live_frames_after = Mock(side_effect=RuntimeError("ring gone"))
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        gate.run_pending()
    record = only_record(gate)
    assert (record.shadow_decision, record.shadow_reason) == ("unclear", "error")
    assert output.pulses == [pytest.approx(0.3)] and record.decision == "passthrough"
    assert "Camera shadow for edge #1 failed" in caplog.text

    # A worker that reaches the edge after the deadline: camera mode's watchdog
    # would have pulsed, so the shadow says timeout and reads no frames.
    clock = FakeClock()
    gate, output, camera = shadow_gate(clock, render_frames("stays"))
    gate.on_edge(EDGE_EPOCH)
    clock.at_ms(50.0)
    gate.run_pending()
    record = only_record(gate)
    assert (record.shadow_decision, record.shadow_reason) == ("unclear", "timeout")
    assert camera.calls == 0 and output.pulses == [pytest.approx(0.3)]


def test_camera_mode_has_no_shadow():
    clock = FakeClock()
    gate, _output, _camera = camera_gate(clock, render_frames("stays"))
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    record = only_record(gate)
    assert record.decision == "still_there" and record.shadow_decision is None
    assert gate.summary()["shadow"] == {"decisions": {}, "unclear_reasons": {}}


def test_camera_without_a_runtime_pulses_immediately():
    clock = FakeClock()
    gate, output = make_gate(clock, "camera")
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    record = only_record(gate)
    assert (record.decision, record.reason) == ("unclear", "camera_off")
    assert output.pulses == [pytest.approx(0.3)]


def test_camera_exception_pulses(caplog):
    clock = FakeClock()
    gate, output, camera = camera_gate(clock, render_frames("stays"))
    camera.live_frames_after = Mock(side_effect=RuntimeError("ring gone"))
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        gate.run_pending()
    record = only_record(gate)
    assert (record.decision, record.reason) == ("unclear", "error")
    assert len(output.pulses) == 1
    assert "Camera decision for edge #1 failed" in caplog.text


def test_deadline_watchdog_pulses_when_the_worker_is_late(caplog):
    clock = FakeClock()
    released = []
    gate, output, camera = camera_gate(clock, render_frames("stays"), released=released)
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate.on_edge(EDGE_EPOCH)
        clock.at_ms(45.01)
        gate.run_due_timers()
        gate.run_pending()
        clock.at_ms(200.0)
        gate.run_due_timers()

    record = only_record(gate)
    assert (record.decision, record.reason) == ("unclear", "timeout")
    assert output.pulses == [pytest.approx(45.01)]
    assert [kind for kind, _ in output.events] == ["on", "off"]
    assert released == [] and camera.calls == 0
    assert "no camera decision by the 45 ms deadline" in caplog.text


def test_deadline_after_a_decision_does_nothing():
    clock = FakeClock()
    gate, output, _ = camera_gate(clock, render_frames("fast"))
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    clock.at_ms(200.0)
    gate.run_due_timers()
    assert len(output.pulses) == 1
    assert only_record(gate).decision == "departed"


def test_short_deadline_warns_that_the_gate_cannot_block(caplog):
    clock = FakeClock()
    with caplog.at_level(logging.WARNING, logger="openflight.trigger_gate"):
        gate, _ = make_gate(clock, "camera", deadline_ms=30.0)
    assert not gate.can_block
    assert "will pulse every edge" in caplog.text
    assert make_gate(FakeClock(), "camera")[0].can_block


@pytest.mark.parametrize("scene", ["front_light", "backlight"])
@pytest.mark.parametrize(("motion", "decision"), [("fast", "departed"), ("stays", "still_there")])
def test_live_check_agrees_with_the_offline_check(scene, motion, decision):
    frames = render_frames(motion, scene=scene)
    clip = np.stack([frame.image for frame in frames])
    stamps = np.asarray([frame.sensor_timestamp_ns for frame in frames])
    offline = check_ball_departure(clip, stamps, EDGE_NS, ANCHOR)

    live = LiveDepartureCheck(EDGE_NS, ANCHOR)
    history = [frame for frame in frames if frame.host_timestamp_ns <= EDGE_NS]
    verdict = live.add(history)
    for frame in frames[len(history) :]:
        if verdict is not None or frame.host_timestamp_ns > EDGE_NS + 43_000_000:
            break
        verdict = live.add([frame])
    verdict = verdict or live.finish()

    assert offline.decision == verdict.decision == decision
    if decision == "departed":
        assert verdict.first_departure_ms_after_edge == pytest.approx(
            offline.first_departure_ms_after_edge
        )


class _RealClockOutput:
    """Output double for the threaded gate, stamping on/off with the real monotonic clock."""

    def __init__(self, *_args, **_kwargs):
        self.events: list[tuple[str, float]] = []
        self.changed = threading.Event()

    def on(self):
        self.events.append(("on", time.monotonic()))

    def off(self):
        self.events.append(("off", time.monotonic()))
        self.changed.set()

    def close(self):
        pass


def _threaded_gate(**kwargs) -> tuple[TriggerGate, _RealClockOutput]:
    output = _RealClockOutput()
    gate = TriggerGate(pin=27, output_factory=lambda *_a, **_k: output, **kwargs)
    gate.start()
    return gate, output


def test_threads_end_the_pulse_and_report_with_the_real_clock():
    reported = threading.Event()
    gate, output = _threaded_gate(on_pulse=reported.set)
    try:
        edge = time.time()
        started = time.monotonic()
        gate.on_edge(edge)
        assert output.events[0][0] == "on" and output.events[0][1] - started < 0.002
        assert output.changed.wait(1.0) and reported.wait(1.0)
    finally:
        gate.stop()
    width = output.events[1][1] - output.events[0][1]
    assert 0.019 <= width <= 0.2


def test_threads_watchdog_pulses_at_the_deadline_while_the_worker_hangs():
    stuck = threading.Event()
    camera = SimpleNamespace(
        running=True,
        trigger_check_anchor=lambda: ANCHOR,
        live_frames_after=lambda _after_ns, _timeout_s: stuck.wait(0.3) and (),
    )
    gate, output = _threaded_gate(mode="camera", camera=camera)
    try:
        started = time.monotonic()
        gate.on_edge(time.time())
        assert output.changed.wait(1.0)
    finally:
        gate.stop()
    assert 0.044 <= output.events[0][1] - started <= 0.2
    record = only_record(gate)
    assert (record.decision, record.reason) == ("unclear", "timeout")
    assert [kind for kind, _ in output.events].count("on") == 1


def test_invalid_gate_settings_are_refused():
    with pytest.raises(ValueError, match="mode"):
        TriggerGate(pin=27, mode="always")
    with pytest.raises(ValueError, match="pulse"):
        TriggerGate(pin=27, pulse_ms=80.0)
    with pytest.raises(ValueError, match="deadline"):
        TriggerGate(pin=27, mode="camera", deadline_ms=200.0)


def test_record_fields_and_session_summary():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    fields = only_record(gate).log_fields()
    assert fields["gate_mode"] == "passthrough"
    assert fields["gate_decision"] == "passthrough"
    assert fields["sound_edge_delay_ms"] == pytest.approx(0.3, abs=0.01)
    assert fields["gate_decision_latency_ms"] == pytest.approx(0.3, abs=0.01)
    assert fields["gate_edge_timestamp"] == EDGE_EPOCH
    assert set(fields) >= {"gate_reason", "gate_frames_used", "gate_first_byte_lag_ms"}
    assert gate.summary() == {
        "mode": "passthrough",
        "edges": 1,
        "pulsed": 1,
        "blocked": 0,
        "late": 0,
        "decisions": {"passthrough": 1},
        "unclear_reasons": {},
        "ops_busy": 0,
        "ops_busy_phases": {},
        "shadow": {"decisions": {}, "unclear_reasons": {}},
    }
    assert gate.summary_text() == (
        "1 edges, 1 pulsed, 0 blocked, 0 late (passthrough 1); 0 in OPS busy time"
    )
    # Without a camera there is no shadow.
    assert fields["gate_shadow_decision"] is None and fields["gate_shadow_frame_states"] == ""


def test_summary_is_logged_every_twenty_edges_and_at_stop(caplog):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    with caplog.at_level(logging.INFO, logger="openflight.trigger_gate"):
        for index in range(20):
            clock.at_ms(index * 200.0 + 0.3)
            gate.on_edge(EDGE_EPOCH + index * 0.2)
        gate.run_pending()
        gate.stop()
    assert "Summary: 20 edges, 20 pulsed" in caplog.text
    assert "Session summary: 20 edges" in caplog.text


def test_capture_matches_its_pulse_by_first_byte():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    pulse = only_record(gate).pulse_timestamp
    post_ms = 59.7

    matched = gate.record_for_capture(pulse + (post_ms + 17.0) / 1000.0, post_ms)
    assert matched is not None and matched.sequence == 1
    assert matched.first_byte_lag_ms == pytest.approx(17.0, abs=0.01)
    assert matched.first_byte_match == "window"
    # Past the window only the bounded late fallback matches (a queued dump).
    late = gate.record_for_capture(pulse + (post_ms + 70.0) / 1000.0, post_ms)
    assert late.sequence == 1 and late.first_byte_match == "late"
    assert gate.record_for_capture(pulse + (post_ms + 1010.0) / 1000.0, post_ms) is None
    assert gate.record_for_capture(pulse + (post_ms - 20.0) / 1000.0, post_ms) is None
    assert gate.record_for_capture(None, post_ms) is None


def test_capture_picks_the_pulse_nearest_the_usual_first_byte_lag():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    clock.at_ms(55.0)
    gate.on_edge(EDGE_EPOCH + 0.0547)
    first, second = list(gate._records)
    first_byte = first.pulse_timestamp + (68.3 + 20.0) / 1000.0
    assert gate.record_for_capture(first_byte, 68.3).sequence == 1
    assert gate.record_for_capture(second.pulse_timestamp + 0.0853, 68.3).sequence == 2


def test_blocked_edges_never_match_a_capture():
    clock = FakeClock()
    gate, _, _ = camera_gate(clock, render_frames("stays"))
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    assert gate.record_for_capture(EDGE_EPOCH + 0.1, 59.7) is None


def _fixture_capture(index: int, *, delay_ms: float = 0.0, correct: bool = True) -> IQCapture:
    with np.load(CLUB_FIXTURE) as data:
        edge_ms = float(data["trigger_offset_ms"][index])
        i_samples = data["i_samples"][index].astype(int).tolist()
        q_samples = data["q_samples"][index].astype(int).tolist()
    end_epoch = EDGE_EPOCH + (4096 / 30.0 - edge_ms) / 1000.0
    return IQCapture(
        sample_time=0.0,
        trigger_time=(edge_ms + delay_ms) / 1000.0,
        i_samples=i_samples,
        q_samples=q_samples,
        first_byte_timestamp=end_epoch,
        sound_edge_delay_ms=delay_ms if correct else 0.0,
    )


@pytest.mark.parametrize("index", [0, 1, 2, 5])
def test_gate_delay_keeps_club_speed_and_impact_on_the_sound_edge(index):
    processor = RollingBufferProcessor()
    base = processor.process_capture(_fixture_capture(index), anchor_club_to_trigger=True)
    delayed_capture = _fixture_capture(index, delay_ms=30.0)
    delayed = processor.process_capture(delayed_capture, anchor_club_to_trigger=True)
    uncorrected = processor.process_capture(
        _fixture_capture(index, delay_ms=30.0, correct=False), anchor_club_to_trigger=True
    )

    assert delayed_capture.trigger_offset_ms == pytest.approx(base.capture.trigger_offset_ms + 30)
    assert delayed_capture.sound_edge_offset_ms == pytest.approx(base.capture.trigger_offset_ms)
    assert base.club_speed_mode == delayed.club_speed_mode == "anchored"
    assert delayed.club_speed_mph == base.club_speed_mph
    assert delayed.club_line_end_ms == base.club_line_end_ms
    assert delayed.impact.timestamp_ms == pytest.approx(base.impact.timestamp_ms)
    assert delayed.impact.source == base.impact.source
    assert uncorrected.club_speed_mph != base.club_speed_mph

    shots = [RollingBufferMonitor(port=None)._create_shot(p) for p in (base, delayed)]
    assert shots[0].impact_timestamp == pytest.approx(EDGE_EPOCH, abs=1e-6)
    assert shots[1].impact_timestamp == pytest.approx(EDGE_EPOCH, abs=1e-6)
    assert shots[1].impact_timestamp_kld7 == pytest.approx(shots[0].impact_timestamp_kld7, abs=1e-6)


def test_impact_fallback_sits_on_the_sound_edge_only_with_the_delay():
    processor = RollingBufferProcessor()
    empty = SpeedTimeline(readings=[], sample_rate_hz=937.5)
    corrected = processor.estimate_impact(empty, 100.0, capture=_fixture_capture(2, delay_ms=30.0))
    uncorrected = processor.estimate_impact(
        empty, 100.0, capture=_fixture_capture(2, delay_ms=30.0, correct=False)
    )
    assert corrected.source == "sound_trigger"
    assert corrected.timestamp_ms == pytest.approx(68.0)
    assert uncorrected.timestamp_ms == pytest.approx(98.0)


def test_without_a_gate_nothing_changes():
    monitor = RollingBufferMonitor(port=None)
    capture = _fixture_capture(2)
    assert capture.sound_edge_delay_ms == 0.0
    assert capture.sound_edge_offset_ms == capture.trigger_offset_ms
    assert capture.sound_edge_timestamp == capture.trigger_timestamp
    assert monitor._gate_fields(EDGE_EPOCH) == {}


def test_gate_applies_the_matched_delay_to_a_capture(caplog):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    clock.at_ms(30.0)
    gate.on_edge(EDGE_EPOCH)
    capture = _fixture_capture(2, delay_ms=30.0, correct=False)
    fields = gate.apply_to_capture(capture)
    assert capture.sound_edge_delay_ms == pytest.approx(30.0, abs=0.01)
    assert fields["gate_decision"] == "passthrough"
    assert fields["sound_edge_offset_ms"] == pytest.approx(68.0, abs=0.01)

    stray = _fixture_capture(2)
    stray.first_byte_timestamp += 5.0
    with caplog.at_level(logging.WARNING):
        fields = gate.apply_to_capture(stray)
    assert fields["gate_decision"] == "unmatched" and stray.sound_edge_delay_ms == 0.0
    assert "matches no gate pulse" in caplog.text


def _session_records(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for log in path.glob("*.jsonl")
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


class _OneCaptureTrigger(SoundTrigger):
    """Sound trigger double handing the capture loop one capture, then idling."""

    def __init__(self, capture: IQCapture):
        super().__init__(pre_trigger_segments=16)
        self._capture = capture

    def wait_for_trigger(self, radar, processor, timeout=30.0, **kwargs):
        capture, self._capture = self._capture, None
        if capture is not None:
            self._append_diagnostic(accepted=True, reason="accepted")
            return capture
        kwargs["cancel_event"].wait(timeout)
        return None


def test_capture_loop_logs_gate_fields_and_anchors_the_shot(tmp_path, monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    clock.at_ms(30.0)
    gate.on_edge(EDGE_EPOCH)
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)

    shots = []
    shot_seen = threading.Event()
    monitor = RollingBufferMonitor(port=None)
    monitor.trigger = _OneCaptureTrigger(_fixture_capture(2, delay_ms=30.0, correct=False))
    monitor.set_trigger_gate(gate)
    monitor.start(shot_callback=lambda shot: (shots.append(shot), shot_seen.set()))
    try:
        assert shot_seen.wait(10.0)
    finally:
        monitor.stop()
    session.end_session()

    assert shots[0].impact_timestamp == pytest.approx(EDGE_EPOCH, abs=1e-4)
    records = _session_records(tmp_path)
    capture = next(r for r in records if r.get("type") == "rolling_buffer_capture")
    event = next(r for r in records if r.get("type") == "trigger_event")
    for record in (capture, event):
        assert record["gate_mode"] == "passthrough"
        assert record["gate_decision"] == "passthrough"
        assert record["sound_edge_delay_ms"] == pytest.approx(30.0, abs=0.01)
        assert record["gate_decision_latency_ms"] == pytest.approx(30.0, abs=0.01)
    assert capture["sound_edge_offset_ms"] == pytest.approx(68.0, abs=0.01)
    assert capture["trigger_offset_ms"] == pytest.approx(98.0)


def test_rejected_dump_events_carry_the_gate_record(tmp_path, monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)
    monitor = RollingBufferMonitor(port=None, pre_trigger_segments=16)
    monitor.set_trigger_gate(gate)
    first_byte = only_record(gate).pulse_timestamp + (68.27 + 17.0) / 1000.0

    monitor._record_trigger_event(
        {"capture_first_byte_timestamp": first_byte}, accepted=False, reason="no_outbound_speed"
    )
    session.end_session()

    event = next(r for r in _session_records(tmp_path) if r.get("type") == "trigger_event")
    assert event["gate_decision"] == "passthrough"
    assert event["gate_first_byte_lag_ms"] == pytest.approx(17.0, abs=0.05)


def test_events_without_a_gate_keep_their_old_shape(tmp_path, monkeypatch):
    session = SessionLogger(log_dir=tmp_path)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)
    RollingBufferMonitor(port=None)._record_trigger_event(
        {"capture_first_byte_timestamp": EDGE_EPOCH}, accepted=False, reason="parse_failed"
    )
    session.end_session()
    event = next(r for r in _session_records(tmp_path) if r.get("type") == "trigger_event")
    assert not any(key.startswith("gate_") or key.startswith("sound_edge") for key in event)


def test_camera_split_needs_the_club_history_before_a_late_edge():
    assert pre_trigger_ms(8) == pytest.approx(34.13, abs=0.01)
    assert MIN_EDGE_HISTORY_MS == pytest.approx(20.4)
    assert FULL_EDGE_HISTORY_MS == pytest.approx(31.4)
    assert recommended_camera_split(45.0) == 18
    assert camera_split_check(18, 45.0) is None
    level, message = camera_split_check(16, 45.0)
    assert level == "warning" and "--buffer-split 18" in message
    assert camera_split_check(8, 45.0)[0] == "error"
    assert camera_split_check(15, 45.0)[0] == "error"
    assert camera_split_check(13, 20.0) is None


def test_split_mismatch_check_accepts_the_camera_split():
    processor = RollingBufferProcessor()
    capture = _fixture_capture(2)
    capture.trigger_time = pre_trigger_ms(18) / 1000.0
    assert processor.has_expected_trigger_split(capture, 18)
    assert not processor.has_expected_trigger_split(capture, 8)


def _ring_frame(host_ns: int) -> CameraFrame:
    return CameraFrame(np.zeros((2, 2), np.uint8), host_ns - DELIVERY_NS, host_ns, 100, 2.0)


def test_ring_returns_frames_after_a_time_without_disturbing_capture():
    ring = TriggeredFrameBuffer(pre_trigger_frames=3, post_trigger_frames=2)
    for index in range(4):
        ring.add_frame(_ring_frame(1000 + index))
    assert ring.trigger(5000)
    ring.add_frame(_ring_frame(1004))

    newer = ring.frames_after(1001)
    assert [frame.host_timestamp_ns for frame in newer] == [1002, 1003, 1004]
    assert ring.frames_after(1004) == ()
    assert ring.capture_busy
    ring.add_frame(_ring_frame(1005))
    assert ring.pop_capture().pre_trigger_count == 3
    assert [f.host_timestamp_ns for f in ring.frames_after(0)] == [1001, 1002, 1003, 1004, 1005]


def test_ring_waits_for_the_next_frame_up_to_the_timeout():
    ring = TriggeredFrameBuffer(pre_trigger_frames=2, post_trigger_frames=2)
    ring.add_frame(_ring_frame(1000))
    timer = threading.Timer(0.05, lambda: ring.add_frame(_ring_frame(2000)))
    timer.start()
    started = time.monotonic()
    frames = ring.frames_after(1000, timeout_s=2.0)
    assert [frame.host_timestamp_ns for frame in frames] == [2000]
    assert time.monotonic() - started < 1.0
    started = time.monotonic()
    assert ring.frames_after(2000, timeout_s=0.05) == ()
    assert time.monotonic() - started >= 0.04


def test_camera_runtime_live_api(tmp_path):
    from openflight.camera.capture_runtime import CameraCaptureRuntime

    runtime = CameraCaptureRuntime(output_dir=tmp_path, use_gpio_trigger=False)
    assert not runtime.running
    assert runtime.live_frames_after(0) == ()
    assert runtime.trigger_check_anchor() is None
    runtime._ring.add_frame(_ring_frame(1000))
    runtime._running = True
    assert len(runtime.live_frames_after(0)) == 1


def test_reject_edge_cancels_only_the_dump_of_that_edge(tmp_path):
    monitor = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg",
        output_dir=tmp_path,
        radar=Mock(),
        cancel_false_triggers=True,
    )
    monitor._running = monitor._armed = monitor._capture_active = True
    monitor._last_edge_timestamp = EDGE_EPOCH

    assert not monitor.reject_edge(EDGE_EPOCH - 0.5)
    assert not monitor._cancel_dump.is_set()
    assert monitor.reject_edge(EDGE_EPOCH)
    assert monitor._cancel_dump.is_set()
    assert not monitor.reject_edge(EDGE_EPOCH)

    idle = IWR6843CaptureMonitor(
        config_path=tmp_path / "radar.cfg", output_dir=tmp_path, radar=Mock()
    )
    idle._running = idle._armed = idle._capture_active = True
    idle._last_edge_timestamp = EDGE_EPOCH
    assert not idle.reject_edge(EDGE_EPOCH)


def _gate_args(**overrides) -> SimpleNamespace:
    values = {
        "ops_trigger_pin": 27,
        "ops_trigger_mode": None,
        "ops_trigger_pulse_ms": 20.0,
        "ops_trigger_deadline_ms": 45.0,
        "iwr6843_trigger_pin": 17,
        "mock": False,
        "swing_speed": False,
        "trigger": "sound",
        "camera_capture": True,
        "sound_pre_trigger": 8,
        "sample_rate": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_server_gate_options_default_off():
    args = _gate_args(ops_trigger_pin=None)
    assert server_module._validate_trigger_gate_args(argparse.ArgumentParser(), args) is None
    assert args.ops_trigger_mode is None
    with pytest.raises(SystemExit):
        server_module._validate_trigger_gate_args(
            argparse.ArgumentParser(), _gate_args(ops_trigger_pin=None, ops_trigger_mode="camera")
        )


def test_server_gate_passthrough_runs_with_todays_split():
    args = _gate_args()
    assert server_module._validate_trigger_gate_args(argparse.ArgumentParser(), args) is None
    assert args.ops_trigger_mode == "passthrough"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ops_trigger_pin": 17},
        {"ops_trigger_pin": 40},
        {"mock": True},
        {"swing_speed": True},
        {"trigger": "speed"},
        {"ops_trigger_pulse_ms": 0.0},
        {"ops_trigger_deadline_ms": 120.0},
        {"ops_trigger_mode": "camera", "camera_capture": False, "sound_pre_trigger": 18},
        {"ops_trigger_mode": "camera", "sound_pre_trigger": 8},
    ],
)
def test_server_refuses_unworkable_gate_options(overrides):
    with pytest.raises(SystemExit):
        server_module._validate_trigger_gate_args(
            argparse.ArgumentParser(), _gate_args(**overrides)
        )


def test_server_warns_about_a_short_camera_split():
    warning = server_module._validate_trigger_gate_args(
        argparse.ArgumentParser(), _gate_args(ops_trigger_mode="camera", sound_pre_trigger=16)
    )
    assert "--buffer-split 18" in warning
    assert (
        server_module._validate_trigger_gate_args(
            argparse.ArgumentParser(), _gate_args(ops_trigger_mode="camera", sound_pre_trigger=18)
        )
        is None
    )


def _init_ti(monkeypatch, tmp_path) -> dict:
    captured = {}

    class FakeCaptureMonitor:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.port = "/dev/ttyUSB0"

        def start(self, *, armed=True):
            return None

    monkeypatch.setattr(Calibration, "load", lambda _path: Calibration.identity())
    monkeypatch.setattr("openflight.iwr6843.monitor.IWR6843CaptureMonitor", FakeCaptureMonitor)
    monkeypatch.setattr("openflight.iwr6843.monitor.tx_order_from_config", lambda _path: "normal")
    assert server_module.init_iwr6843(
        port="/dev/ttyUSB0",
        config_path="snapshot.cfg",
        calibration_path="cal.json",
        output_dir=tmp_path,
        trigger_pin=17,
        tee_range_m=1.575,
        net_range_m=4.6,
        tx_order="auto",
        capture_timeout_s=12.0,
    )
    server_module.iwr6843_runtime = None
    return captured


def test_ti_fans_out_to_the_gate_first_and_the_gate_owns_the_stall_watch(monkeypatch, tmp_path):
    camera = SimpleNamespace(notify_trigger=Mock(), set_write_deferral=Mock())
    monkeypatch.setattr(server_module, "camera_capture_runtime", camera)

    monkeypatch.setattr(server_module, "trigger_gate", None)
    observers = _init_ti(monkeypatch, tmp_path)["trigger_observers"]
    assert observers == [camera.notify_trigger, server_module._notify_ops_trigger_edge]

    gate = SimpleNamespace(on_edge=Mock())
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    captured = _init_ti(monkeypatch, tmp_path)
    assert captured["trigger_observers"] == [gate.on_edge, camera.notify_trigger]
    # The TI monitor fills the shared edge log the session-log writers read.
    assert captured["edge_timing"] is server_module.sound_edge_timing


@pytest.mark.parametrize("mode", ["camera", "passthrough"])
def test_init_trigger_gate_without_ti_owns_the_sound_input(monkeypatch, mode):
    clock = FakeClock()
    output = FakeOutput(clock)
    created = {}

    class FakeGate(TriggerGate):
        def __init__(self, **kwargs):
            created.update(kwargs)
            super().__init__(output_factory=output.factory, background=False, **kwargs)

    class FakeListener(EdgeListener):
        def __init__(self, **kwargs):
            super().__init__(button_factory=FakeButton, **kwargs)

    camera = SimpleNamespace(notify_trigger=Mock())
    monkeypatch.setattr("openflight.trigger_gate.TriggerGate", FakeGate)
    monkeypatch.setattr("openflight.trigger_gate.EdgeListener", FakeListener)
    monkeypatch.setattr(server_module, "camera_capture_runtime", camera)
    monkeypatch.setattr(server_module, "camera_capture_config", {"enabled": True})
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "trigger_gate_listener", None)
    monkeypatch.setattr(server_module, "trigger_gate_config", {"enabled": False})

    assert server_module.init_trigger_gate(
        pin=27,
        mode=mode,
        pulse_ms=20.0,
        deadline_ms=45.0,
        edge_pin=17,
        own_edge_pin=True,
        split_warning=None,
    )
    gate, listener = server_module.trigger_gate, server_module.trigger_gate_listener
    try:
        assert (
            created["camera"] is camera
            and created["release_edge"] is server_module._release_ti_edge
        )
        assert listener._observers == [gate.on_edge, camera.notify_trigger]
        # Listener and gate share the server's edge log: the gate dump fields
        # (trigger_event, rolling_buffer_capture) find the listener's edges.
        assert created["edge_timing"] is server_module.sound_edge_timing
        assert listener.edge_timing is server_module.sound_edge_timing
        assert server_module.camera_capture_config["trigger_source"] == "trigger_gate_fanout"
        config = server_module._session_start_config()["trigger_gate"]
        assert config["mode"] == mode and config["host_int_pin_bcm"] == 27
        # A passthrough gate with a camera shadows the camera decision on its deadline.
        assert config["deadline_ms"] == 45.0
        assert config["camera_shadow"] is (mode == "passthrough")
    finally:
        gate.stop()
        listener.stop()


def test_session_config_has_no_gate_entry_when_off(monkeypatch):
    monkeypatch.setattr(server_module, "trigger_gate_config", {"enabled": False})
    assert "trigger_gate" not in server_module._session_start_config()


def test_release_ti_edge_reaches_the_capture_monitor(monkeypatch):
    capture_monitor = Mock()
    capture_monitor.reject_edge.return_value = True
    monkeypatch.setattr(
        server_module, "iwr6843_runtime", SimpleNamespace(capture_monitor=capture_monitor)
    )
    assert server_module._release_ti_edge(EDGE_EPOCH)
    capture_monitor.reject_edge.assert_called_once_with(EDGE_EPOCH)
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    assert not server_module._release_ti_edge(EDGE_EPOCH)


def test_shot_impact_uses_the_sound_edge_for_ti_and_camera_matching():
    capture = _fixture_capture(2, delay_ms=30.0)
    processed = RollingBufferProcessor().process_capture(capture, anchor_club_to_trigger=True)
    shot = RollingBufferMonitor(port=None)._create_shot(processed)
    assert capture.trigger_timestamp == pytest.approx(EDGE_EPOCH + 0.030, abs=1e-6)
    assert shot.impact_timestamp == pytest.approx(EDGE_EPOCH, abs=1e-6)
    assert isinstance(shot.timestamp, datetime)
