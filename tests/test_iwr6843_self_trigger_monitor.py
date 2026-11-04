"""IWR6843CaptureMonitor in the ti and ti-shadow shot-trigger modes, on a fake TI board.

The fake board models the firmware contract of upstream cb8fbff: a fire latches
and freezes the ring and writes ``Triggered``; ``l3dump`` and ``l3release``
take that freeze (no second freeze); without a latch ``l3dump`` freezes at the
next boundary (the sound path). No hardware is involved.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from openflight.iwr6843 import monitor as monitor_module
from openflight.iwr6843.dump import TEMP_REPORT_KEYS, pack_dump
from openflight.iwr6843.health import DONE, SILENT, SILENT_CLI, CliReply, TiWedgedError
from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.iwr6843.self_trigger import SelfTriggerConfig, TiShadowMatcher, TriggerNotice
from openflight.trigger_gate import TriggerGate

TRIGGER = SelfTriggerConfig(tee_bin=36, snr=1.0)


def _raw_dump() -> bytes:
    report = {key: index + 25 for index, key in enumerate(TEMP_REPORT_KEYS)}
    return pack_dump(
        np.ones((2, 4, 4, 8), dtype=complex), n_tx=2, version=5, temperature_report=report
    )


class FakeBoard:
    """TI CLI + ring with the self-trigger firmware's latch semantics."""

    def __init__(self, *, cause: str | None = "end"):
        self.port = "/dev/fake-ti"
        self.lock = threading.Lock()
        self.commands: list[str] = []
        self.scheduled: list[float] = []  # monotonic times of future fires
        self.latched = False
        self.enabled = False
        self.active = True
        self.frames = 0
        self.freezes: list[str] = []  # "self" or "sound" per freeze the board made
        self.dumps: list[str] = []  # whose freeze each l3dump streamed
        self.cause = cause
        self.dump_error: Exception | None = None
        self.release_error: Exception | None = None
        self.stats_reply: str | None = None
        self.resync_answers = True
        self.dump_seconds = 0.0
        self.notices: list[TriggerNotice] = []

    # --- test controls ---------------------------------------------------
    def fire_in(self, seconds: float) -> None:
        with self.lock:
            self.scheduled.append(time.monotonic() + seconds)
            self.scheduled.sort()

    def _fire_due(self) -> None:
        now = time.monotonic()
        while self.scheduled and self.scheduled[0] <= now:
            self.scheduled.pop(0)
            if self.enabled and self.active and not self.latched:
                self.latched = True
                self.freezes.append("self")
                self.notices.append(TriggerNotice(time.monotonic_ns(), time.time()))

    def sent(self, prefix: str) -> int:
        return sum(1 for command in self.commands if command.startswith(prefix))

    # --- radar API -----------------------------------------------------------
    def send_config(self, path: str) -> None:
        with self.lock:
            self.commands.append("config")
            self.enabled = False
            self.latched = False
            self.active = True

    def set_self_trigger(self, command: str) -> None:
        with self.lock:
            self.commands.append(command)
            self.enabled = True

    def poll_trigger_notice(self, *, block: bool):
        if block:
            time.sleep(0.002)
        with self.lock:
            self._fire_due()
        return self.take_notices()

    def take_notices(self):
        with self.lock:
            self._fire_due()
            notices, self.notices = tuple(self.notices), []
        return notices

    def trigger_cause(self):
        with self.lock:
            self.commands.append("triggerLog track")
        return self.cause

    def read_dump(self, cancel_event=None):
        with self.lock:
            self.commands.append("l3dump")
            self._fire_due()
            if self.latched:
                # cb8fbff: l3_awaitFrozenRing takes the fire's freeze.
                self.dumps.append("self")
                self.latched = False
            else:
                self.freezes.append("sound")
                self.dumps.append("sound")
            error = self.dump_error
        if self.dump_seconds:
            time.sleep(self.dump_seconds)
        if error is not None:
            raise error
        return _raw_dump()

    def release_frozen_ring(self) -> None:
        with self.lock:
            self.commands.append("l3release")
            error = self.release_error
            if error is None:
                self.latched = False
        if error is not None:
            raise error

    def command(self, line: str, window: float = 1.5) -> CliReply:
        with self.lock:
            self.commands.append(line)
            self._fire_due()
            self.frames += 100
            raw = (
                self.stats_reply
                if self.stats_reply is not None
                else (
                    f"frames={self.frames} active={int(self.active)} hwa_out={self.frames} "
                    f"latched={int(self.latched)} enabled={int(self.enabled)}\nDone"
                )
            )
        kind = DONE if "Done" in raw else SILENT
        return CliReply(command=line, raw=raw, elapsed_s=0.001, kind=kind)

    def resync(self) -> bool:
        self.commands.append("resync")
        return self.resync_answers

    def stop_sensor(self, *, retry=True) -> None:
        self.commands.append("sensorStop")
        self.latched = False

    def close(self) -> None:
        self.commands.append("close")

    def reopen(self) -> None:
        self.commands.append("reopen")


class FakeButton:
    def __init__(self, pin, pull_up, bounce_time):
        self.pin = pin
        self.when_pressed = None

    def close(self):
        pass


class FakeOutput:
    def __init__(self):
        self.ons = 0

    def factory(self, pin, *, active_high, initial_value):
        return self

    def on(self):
        self.ons += 1

    def off(self):
        pass

    def close(self):
        pass


class FakeCycler:
    def __init__(self):
        self.cycles = 0

    def cycle(self):
        self.cycles += 1


def _wait_until(predicate, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _monitor(tmp_path, board, *, mode="ti", **kwargs) -> IWR6843CaptureMonitor:
    cfg = tmp_path / "radar.cfg"
    cfg.write_text("sensorStart\n", encoding="utf-8")
    return IWR6843CaptureMonitor(
        config_path=cfg,
        output_dir=tmp_path,
        radar=board,
        button_factory=FakeButton,
        shot_trigger=mode,
        self_trigger=TRIGGER,
        cancel_false_triggers=True,
        **kwargs,
    )


@pytest.fixture(name="board")
def _board():
    return FakeBoard()


# --- ti: the notice is the shot edge -------------------------------------------


def test_ti_mode_needs_a_trigger_config_and_skips_the_microphone(tmp_path, board):
    with pytest.raises(ValueError, match="needs a self_trigger"):
        IWR6843CaptureMonitor(
            config_path=tmp_path / "x.cfg", output_dir=tmp_path, radar=board, shot_trigger="ti"
        )
    opened = []

    def factory(*args, **kwargs):
        opened.append(args)
        return FakeButton(*args, **kwargs)

    monitor = _monitor(tmp_path, board)
    monitor._button_factory = factory  # pylint: disable=protected-access
    monitor.start()
    try:
        assert opened == []
        assert board.commands[:2] == ["config", TRIGGER.command]
        assert not monitor.uses_gpio and monitor.self_trigger_active
    finally:
        monitor.stop()


def test_ti_notice_pulses_the_gate_then_dumps_the_frozen_ring_once(tmp_path, board):
    output = FakeOutput()
    gate = TriggerGate(pin=27, output_factory=output.factory, background=False, edge_source="ti")
    gate.start()
    fires: list[dict] = []
    camera = []
    monitor = _monitor(
        tmp_path,
        board,
        trigger_observers=[gate.on_edge, camera.append],
        fire_observers=[
            fires.append,
            lambda f: gate.note_ti_cause(f["edge_timestamp"], f["cause"]),
        ],
    )
    monitor.start()
    try:
        board.fire_in(0.0)
        capture = monitor.capture_for_shot(None, timeout_s=3.0)
    finally:
        monitor.stop()
        gate.stop()

    assert capture is not None and capture.valid
    assert output.ons == 1, "HOST_INT pulsed once at the notice"
    assert len(camera) == 1 and camera[0] == capture.trigger_timestamp
    # One freeze (the board's own), streamed by one l3dump; no release, no second freeze.
    assert board.freezes == ["self"]
    assert board.dumps == ["self"]
    assert board.sent("l3release") == 0
    sequence = [c for c in board.commands if c in ("triggerLog track", "l3dump", "l3release")]
    assert sequence == ["triggerLog track", "l3dump"]
    (fire,) = fires
    assert fire["cause"] == "end" and fire["edge_timestamp"] == capture.trigger_timestamp
    record = gate._records[-1]  # pylint: disable=protected-access
    assert record.edge_source == "ti" and record.ti_cause == "end"
    assert record.anchor_lead_ms == pytest.approx(gate.ti_anchor.lead_ms("end"))
    fields = record.log_fields()
    assert fields["sound_edge_delay_ms"] == pytest.approx(record.delay_ms + record.anchor_lead_ms)


def test_ti_notice_before_arming_is_released_without_fan_out(tmp_path, board):
    seen = []
    monitor = _monitor(tmp_path, board, trigger_observers=[seen.append])
    monitor.start(armed=False)
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: board.sent("l3release") == 1)
        assert seen == [] and board.sent("l3dump") == 0
        assert not board.latched
    finally:
        monitor.stop()


def test_ti_notice_during_ops_dump_fans_out_and_releases(tmp_path, board):
    seen = []
    monitor = _monitor(tmp_path, board, trigger_observers=[seen.append])
    monitor.start()
    monitor.enable_ops_dump_gating()
    monitor.notify_ops_dump_started(time.time())
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: board.sent("l3release") == 1)
        assert len(seen) == 1, "the gate still sees the edge (OPS busy accounting)"
        assert board.sent("l3dump") == 0
    finally:
        monitor.stop()


def test_ti_second_fire_after_the_dump_is_a_new_shot(tmp_path, board):
    monitor = _monitor(tmp_path, board)
    monitor.start()
    try:
        board.fire_in(0.0)
        first = monitor.capture_for_shot(None, timeout_s=3.0)
        board.fire_in(0.15)
        second = monitor.capture_for_shot(None, timeout_s=3.0)
    finally:
        monitor.stop()
    assert first.valid and second.valid
    assert second.trigger_timestamp > first.trigger_timestamp
    assert board.dumps == ["self", "self"]
    assert board.sent("l3release") == 0


def test_ti_wedged_dump_power_cycles_and_rearms_the_trigger(tmp_path, board, monkeypatch):
    monkeypatch.setattr(monitor_module, "wait_for_path", lambda *a, **k: True)
    board.dump_error = TiWedgedError(SILENT_CLI, "l3dump")
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, board, usb_power_cycler=cycler)
    monitor.start()
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: cycler.cycles == 1)
        assert _wait_until(lambda: board.sent(TRIGGER.command) == 2)
        board.dump_error = None
        # Past the 100 ms ringing filter: the fake recovery takes milliseconds.
        board.fire_in(monitor.RING_FILTER_S + 0.05)
        capture = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            capture = monitor.capture_for_shot(None, timeout_s=0.5)
            if capture is not None and capture.valid:
                break
    finally:
        monitor.stop()
    assert capture is not None and capture.valid
    assert board.commands.count("config") == 2


def test_ti_failed_dump_leaves_the_board_armed(tmp_path, board):
    board.dump_error = RuntimeError("IWR6843 dump refused before ILD1: Error: something")
    monitor = _monitor(tmp_path, board)
    monitor.start()
    try:
        board.fire_in(0.0)
        capture = monitor.capture_for_shot(None, timeout_s=3.0)
        assert capture is not None and not capture.valid
        # The post-failure check reads stats; the board is active and unlatched.
        assert _wait_until(lambda: board.sent("stats") >= 1)
        assert board.sent("l3release") == 0 and board.commands.count("config") == 1
    finally:
        monitor.stop()


def test_ti_release_without_reply_recovers_by_power_cycle(tmp_path, board, monkeypatch):
    monkeypatch.setattr(monitor_module, "wait_for_path", lambda *a, **k: True)
    board.release_error = TiWedgedError(SILENT_CLI, "l3release")
    board.resync_answers = False
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, board, usb_power_cycler=cycler)
    monitor.start(armed=False)
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: cycler.cycles == 1)
        assert _wait_until(lambda: board.sent(TRIGGER.command) == 2)
    finally:
        board.release_error = None
        monitor.stop()


def test_idle_probe_releases_a_latch_whose_notice_was_lost(tmp_path, board, monkeypatch):
    monkeypatch.setattr(monitor_module, "_IDLE_PROBE_S", 0.05)
    monitor = _monitor(tmp_path, board)
    monitor.start(armed=False)
    try:
        with board.lock:
            board.latched = True  # fired, but the notice never reached the host
        assert _wait_until(lambda: board.sent("l3release") == 1)
        assert not board.latched
    finally:
        monitor.stop()


def test_idle_probe_rearms_a_dropped_trigger_config(tmp_path, board, monkeypatch):
    monkeypatch.setattr(monitor_module, "_IDLE_PROBE_S", 0.05)
    monitor = _monitor(tmp_path, board)
    monitor.start()
    try:
        with board.lock:
            board.enabled = False
        assert _wait_until(lambda: board.sent(TRIGGER.command) == 2)
    finally:
        monitor.stop()


def test_idle_probe_power_cycles_a_silent_cli(tmp_path, board, monkeypatch):
    monkeypatch.setattr(monitor_module, "_IDLE_PROBE_S", 0.05)
    monkeypatch.setattr(monitor_module, "wait_for_path", lambda *a, **k: True)
    cycler = FakeCycler()
    monitor = _monitor(tmp_path, board, usb_power_cycler=cycler)
    monitor.start()
    try:
        board.stats_reply = ""
        board.resync_answers = False
        assert _wait_until(lambda: cycler.cycles == 1)
    finally:
        board.stats_reply = None
        monitor.stop()


def test_sound_mode_never_touches_the_self_trigger(tmp_path, board):
    cfg = tmp_path / "radar.cfg"
    cfg.write_text("sensorStart\n", encoding="utf-8")
    monitor = IWR6843CaptureMonitor(
        config_path=cfg, output_dir=tmp_path, radar=board, button_factory=FakeButton
    )
    monitor.start()
    try:
        assert monitor.notify_trigger()
        assert monitor.capture_for_shot(None, timeout_s=2.0).valid
    finally:
        monitor.stop()
    assert board.sent("triggerCfg") == 0
    assert board.sent("triggerLog") == 0 and board.sent("l3release") == 0
    assert board.dumps == ["sound"]


# --- ti-shadow: microphone drives, the TI is logged -----------------------------


def _shadow(tmp_path, board, *, holdoff_s=0.0):
    entries: list[dict] = []
    matcher = TiShadowMatcher(entries.append)
    monitor = _monitor(
        tmp_path, board, mode="ti-shadow", shadow_matcher=matcher, shadow_holdoff_s=holdoff_s
    )
    return monitor, matcher, entries


def test_shadow_fire_with_a_mic_edge_is_matched_and_dumped_once(tmp_path, board):
    monitor, _matcher, entries = _shadow(tmp_path, board, holdoff_s=0.05)
    monitor.start()
    try:
        assert monitor.notify_trigger()  # the microphone edge
        board.fire_in(0.01)  # the board fires 10 ms later, inside the hold-off
        capture = monitor.capture_for_shot(None, timeout_s=3.0)
        assert _wait_until(lambda: any(e["kind"] == "matched" for e in entries), 2.0)
    finally:
        monitor.stop()
    assert capture.valid
    assert board.dumps == ["self"] and board.freezes == ["self"]
    assert board.sent("l3release") == 0
    (entry,) = [e for e in entries if e["kind"] == "matched"]
    assert 5.0 <= entry["delta_ms"] <= 60.0
    assert entry["cause"] == "end" and entry["ring"] == "dumped"
    assert entry["ti_state"] == "listening"


def test_shadow_fire_without_an_edge_is_released_and_logged(tmp_path, board):
    monitor, _matcher, entries = _shadow(tmp_path, board)
    monitor.start()
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: board.sent("l3release") == 1)
        assert _wait_until(lambda: any(e["kind"] == "notice_without_edge" for e in entries), 2.0)
    finally:
        monitor.stop()
    (entry,) = entries
    assert entry["ring"] == "released" and entry["cause"] == "end"
    assert board.sent("l3dump") == 0


def test_shadow_edge_without_a_fire_uses_the_sound_freeze(tmp_path, board):
    monitor, matcher, entries = _shadow(tmp_path, board)
    monitor.start()
    try:
        assert monitor.notify_trigger()
        capture = monitor.capture_for_shot(None, timeout_s=3.0)
    finally:
        monitor.stop()
    assert capture.valid and board.dumps == ["sound"]
    (entry,) = entries
    assert entry["kind"] == "edge_without_notice"
    assert entry["ti_state"] == "listening"
    assert entry["ti_listen_ms"] is not None and entry["ti_listen_ms"] < 20.0
    assert matcher.summary()["edge_without_notice"] == 1


def test_shadow_stale_latch_is_released_before_the_sound_dump(tmp_path, board, monkeypatch):
    # Keep the latch past the consume window: the release pass is blocked until the edge.
    monkeypatch.setattr(IWR6843CaptureMonitor, "_release_unclaimed_latch", lambda self: None)
    monitor, _matcher, entries = _shadow(tmp_path, board)
    monitor.start()
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: monitor._latched_notice is not None)  # pylint: disable=protected-access
        time.sleep(0.08)
        assert monitor.notify_trigger()
        capture = monitor.capture_for_shot(None, timeout_s=3.0)
    finally:
        monitor.stop()
    assert capture.valid
    sequence = [c for c in board.commands if c in ("l3release", "l3dump")]
    assert sequence == ["l3release", "l3dump"]
    assert board.dumps == ["sound"]
    kinds = sorted(e["kind"] for e in entries)
    assert kinds == ["matched"] or kinds == ["edge_without_notice", "notice_without_edge"]


def test_shadow_edge_while_dumping_is_marked(tmp_path, board):
    board.dump_seconds = 0.3
    monitor, _matcher, entries = _shadow(tmp_path, board)
    monitor.start()
    try:
        assert monitor.notify_trigger(time.time())
        assert _wait_until(lambda: board.sent("l3dump") == 1)
        time.sleep(0.12)
        assert not monitor.notify_trigger(time.time())
        assert monitor.capture_for_shot(None, timeout_s=3.0).valid
    finally:
        monitor.stop()
    states = [e["ti_state"] for e in entries if e["kind"] == "edge_without_notice"]
    assert states == ["listening", "dumping"]


def test_shadow_microphone_still_drives_observers(tmp_path, board):
    seen = []
    entries: list[dict] = []
    monitor = _monitor(
        tmp_path,
        board,
        mode="ti-shadow",
        shadow_matcher=TiShadowMatcher(entries.append),
        trigger_observers=[seen.append],
    )
    monitor.start()
    try:
        board.fire_in(0.0)
        assert _wait_until(lambda: board.sent("l3release") == 1)
        assert seen == [], "a TI notice never reaches the gate or camera in shadow mode"
        assert monitor.notify_trigger()
        assert len(seen) == 1
    finally:
        monitor.stop()


def test_monitor_refuses_bad_modes(tmp_path, board):
    with pytest.raises(ValueError):
        IWR6843CaptureMonitor(
            config_path=tmp_path / "x", output_dir=tmp_path, radar=board, shot_trigger="mic"
        )
    with pytest.raises(ValueError):
        IWR6843CaptureMonitor(
            config_path=tmp_path / "x",
            output_dir=tmp_path,
            radar=board,
            shot_trigger="ti-shadow",
            self_trigger=TRIGGER,
            shadow_holdoff_s=-1.0,
        )
