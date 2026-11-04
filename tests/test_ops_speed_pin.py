"""J3 pin-1 input, microphone witness and helpers of the B2 speed trigger (``ops_speed``).

Fake GPIO only: ``FakePinFactory``/``FakePinButton`` stand in for gpiozero,
a fake clock and a manual scheduler replace wall time and the debounce timer.
"""

from __future__ import annotations

import argparse
import threading
import time
from types import SimpleNamespace

import pytest

from openflight import ops243
from openflight.edge_timing import EdgeTimingLog
from openflight.ops_speed import (
    DEFAULT_ANCHOR_MS,
    DEFAULT_TI_DELAY_MS,
    DEFAULT_WITNESS_WINDOW_MS,
    OPS_SPEED,
    OPS_SPEED_SHADOW,
    OPS_SPEED_SHOT_TRIGGERS,
    OpsSpeedConfig,
    OpsSpeedPinError,
    OpsSpeedWitness,
    PinEdgeInput,
    button_kwargs,
    make_pin_input,
    parse_window_ms,
    pin_button_factory,
    pin_idle_problem,
    practice_swing_verdict,
    resolve_pin_pull,
    speed_trigger_supported,
)
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.trigger_gate import (
    FULL_EDGE_HISTORY_MS,
    MIN_EDGE_HISTORY_MS,
    EdgeListener,
    ops_speed_split_check,
)
from tests.ops_speed_sim import FakePinFactory
from tests.test_trigger_gate import _fixture_capture

EPOCH = 1_790_000_000.0


class Clock:
    """Wall and monotonic clocks that move only when told."""

    def __init__(self):
        self.t = 0.0

    def wall(self) -> float:
        return EPOCH + self.t

    def mono_ns(self) -> int:
        return round(self.t * 1e9)

    def at_ms(self, ms: float) -> float:
        """Wall time of ``ms`` on this clock."""
        return EPOCH + ms / 1000.0

    def advance_ms(self, ms: float) -> None:
        self.t += ms / 1000.0

    def set_ms(self, ms: float) -> None:
        self.t = ms / 1000.0


class ManualScheduler:
    """Debounce timer double: callbacks run when the test says so."""

    def __init__(self):
        self.pending: list[list] = []

    def __call__(self, delay_s, callback):
        entry = [delay_s, callback, False]
        self.pending.append(entry)
        return SimpleNamespace(cancel=lambda: entry.__setitem__(2, True))

    def run(self) -> None:
        pending, self.pending = self.pending, []
        for _delay, callback, cancelled in pending:
            if not cancelled:
                callback()


def _pin(clock: Clock, *, debounce_ms=0.0, holdoff_ms=100.0, idle_active=False, **kwargs):
    factory = FakePinFactory(idle_active=idle_active)
    scheduler = ManualScheduler()
    device = factory(22, **button_kwargs("high"))
    pin_input = PinEdgeInput(
        device,
        pin=22,
        debounce_ms=debounce_ms,
        holdoff_ms=holdoff_ms,
        schedule=scheduler,
        wall_time=clock.wall,
        monotonic_ns=clock.mono_ns,
        **kwargs,
    )
    edges: list[tuple[float, object]] = []
    pin_input.when_pressed = lambda stamp: edges.append((stamp, pin_input._last_changed))
    return pin_input, device, scheduler, edges


# --- polarity and pull ------------------------------------------------------------


@pytest.mark.parametrize(
    ("active", "pull", "expected"),
    [
        ("high", None, {"pull_up": False, "bounce_time": None}),
        ("high", "down", {"pull_up": False, "bounce_time": None}),
        ("low", None, {"pull_up": True, "bounce_time": None}),
        ("low", "up", {"pull_up": True, "bounce_time": None}),
        ("high", "off", {"pull_up": None, "active_state": True, "bounce_time": None}),
        ("low", "off", {"pull_up": None, "active_state": False, "bounce_time": None}),
    ],
)
def test_polarity_and_pull_become_button_kwargs(active, pull, expected):
    factory = FakePinFactory()
    make_pin_input(22, active=active, pull=pull, factory=factory).close()
    assert factory.calls == [(22, expected)]


@pytest.mark.parametrize(("active", "pull"), [("high", "up"), ("low", "down")])
def test_pull_towards_the_active_level_is_refused(active, pull):
    with pytest.raises(ValueError, match="active at idle"):
        resolve_pin_pull(active, pull)


@pytest.mark.parametrize(("active", "pull"), [("rising", None), ("high", "strong")])
def test_unknown_polarity_or_pull_is_refused(active, pull):
    with pytest.raises(ValueError):
        resolve_pin_pull(active, pull)


def test_pin_active_at_idle_is_refused_and_released():
    factory = FakePinFactory(idle_active=True)
    with pytest.raises(OpsSpeedPinError, match="active at idle"):
        make_pin_input(22, factory=factory)
    assert factory.button.closed


def test_pin_idle_problem_reports_reasons_without_raising():
    assert pin_idle_problem(22, factory=FakePinFactory()) is None
    assert "active at idle" in pin_idle_problem(22, factory=FakePinFactory(idle_active=True))

    def broken(_pin, **_kwargs):
        raise RuntimeError("GPIO busy")

    assert "GPIO busy" in pin_idle_problem(22, factory=broken)


def test_pin_out_of_range_is_refused():
    with pytest.raises(ValueError):
        make_pin_input(28, factory=FakePinFactory())


# --- edges: hold-off, debounce, release ------------------------------------------------


def test_edge_is_fanned_out_with_its_wall_stamp_and_ticks():
    clock = Clock()
    pin_input, device, _scheduler, edges = _pin(clock)
    clock.set_ms(1000.0)
    device.press(ticks=123.456)
    assert edges == [(clock.at_ms(1000.0), 123.456)]
    assert pin_input.stats()["accepted"] == 1


def test_holdoff_drops_edges_too_soon_after_one():
    clock = Clock()
    pin_input, device, _scheduler, edges = _pin(clock, holdoff_ms=100.0)
    device.press()
    clock.advance_ms(30.0)
    device.release()
    clock.advance_ms(40.0)  # 70 ms after the first edge
    device.press()
    clock.advance_ms(5.0)
    device.release()
    clock.advance_ms(30.0)  # 105 ms
    device.press()

    assert [stamp for stamp, _ in edges] == [clock.at_ms(0.0), clock.at_ms(105.0)]
    stats = pin_input.stats()
    assert stats["holdoff_dropped"] == 1
    assert stats["stray_releases"] == 1


def test_debounce_drops_a_glitch():
    clock = Clock()
    glitches = []
    pin_input, device, scheduler, edges = _pin(
        clock, debounce_ms=2.0, on_glitch=lambda *args: glitches.append(args)
    )
    device.press()
    clock.advance_ms(1.0)
    device.release()
    scheduler.run()

    assert edges == []
    assert glitches == [(clock.at_ms(0.0), clock.at_ms(1.0), pytest.approx(1.0))]
    assert pin_input.stats()["glitches"] == 1


def test_debounce_fans_out_a_held_edge_with_the_original_stamp():
    clock = Clock()
    pin_input, device, scheduler, edges = _pin(clock, debounce_ms=2.0)
    clock.set_ms(500.0)
    device.press(ticks=9.5)
    assert edges == []
    clock.advance_ms(2.0)
    scheduler.run()

    assert edges == [(clock.at_ms(500.0), 9.5)]
    assert scheduler.pending == []
    assert pin_input.stats()["accepted"] == 1


def test_debounce_that_finds_the_pin_inactive_counts_a_glitch():
    clock = Clock()
    glitches = []
    _pin_input, device, scheduler, edges = _pin(
        clock, debounce_ms=2.0, on_glitch=lambda *args: glitches.append(args)
    )
    device.press()
    device.is_active = False  # the release callback never arrived
    clock.advance_ms(2.0)
    scheduler.run()
    assert edges == []
    assert len(glitches) == 1


def test_release_reports_the_high_width():
    clock = Clock()
    releases = []
    _pin_input, device, _scheduler, _edges = _pin(
        clock, on_release=lambda *args: releases.append(args)
    )
    device.press()
    clock.advance_ms(192.0)
    device.release()
    assert releases == [(clock.at_ms(0.0), clock.at_ms(192.0), pytest.approx(192.0))]


def test_a_missed_release_never_silences_the_pin():
    clock = Clock()
    pin_input, device, _scheduler, edges = _pin(clock)
    device.press()
    clock.advance_ms(300.0)
    device.press()  # its release was lost on the way
    assert len(edges) == 2
    assert pin_input.stats()["missed_releases"] == 1


def test_close_releases_the_line_and_stops_edges():
    clock = Clock()
    pin_input, device, scheduler, edges = _pin(clock, debounce_ms=2.0)
    device.press()
    pin_input.close()
    pin_input.close()
    scheduler.run()
    assert device.closed and edges == []
    assert device.when_pressed is None and device.when_released is None


def test_real_timer_debounce_confirms_a_held_edge():
    factory = FakePinFactory()
    pin_input = make_pin_input(22, debounce_ms=1.0, factory=factory)
    delivered = threading.Event()
    stamps = []
    pin_input.when_pressed = lambda stamp: (stamps.append(stamp), delivered.set())
    before = time.time()
    factory.button.press()
    assert delivered.wait(1.0)
    assert before <= stamps[0] <= time.time()
    pin_input.close()


# --- consumers: EdgeListener (and the TI monitor) see the usual surface -----------------


def test_edge_listener_owns_the_pin_through_the_factory():
    factory = FakePinFactory()
    observed = []
    edge_timing = EdgeTimingLog()
    listener = EdgeListener(
        pin=22,
        observers=[observed.append],
        button_factory=pin_button_factory(22, factory=factory),
        edge_timing=edge_timing,
    )
    listener.start()
    listener.arm()
    before = time.time()
    factory.button.press()

    assert len(observed) == 1 and before <= observed[0] <= time.time()
    assert isinstance(listener._button, PinEdgeInput)
    assert edge_timing.summary()["edges"] == 1
    listener.stop()
    assert factory.button.closed


def test_pin_factory_refuses_another_pin():
    factory = pin_button_factory(22, factory=FakePinFactory())
    with pytest.raises(ValueError, match="BCM22"):
        factory(17, pull_up=False, bounce_time=None)


def test_pin_factory_builds_a_fresh_input_per_start():
    fake = FakePinFactory()
    factory = pin_button_factory(22, factory=fake, active="low")
    first = factory(22, pull_up=False, bounce_time=None)
    first.close()
    second = factory(22, pull_up=False, bounce_time=None)
    assert first is not second
    assert [kwargs for _pin_no, kwargs in fake.calls] == [
        {"pull_up": True, "bounce_time": None}
    ] * 2


def test_ti_monitor_takes_the_pin_input(tmp_path):
    from openflight.iwr6843.monitor import IWR6843CaptureMonitor
    from tests.test_iwr6843_monitor import FakeRadar, _raw_dump

    config = tmp_path / "radar.cfg"
    config.write_text("sensorStart\n", encoding="utf-8")
    factory = FakePinFactory()
    monitor = IWR6843CaptureMonitor(
        config_path=config,
        output_dir=tmp_path / "dumps",
        radar=FakeRadar(_raw_dump()),
        gpio_pin=22,
        button_factory=pin_button_factory(22, factory=factory),
    )
    monitor.start()
    before = time.time()
    factory.button.press()
    capture = monitor.capture_for_shot(before + 0.02, timeout_s=1.0)
    monitor.stop()

    assert capture is not None and capture.valid
    assert before <= capture.trigger_timestamp <= time.time()
    assert factory.button.closed


# --- witness -----------------------------------------------------------------------------


class _Witness:
    def __init__(self, **kwargs):
        self.clock = Clock()
        self.records: list[dict] = []
        self.stalls: list[dict] = []
        self.witness = OpsSpeedWitness(
            self.records.append,
            wall_time=self.clock.wall,
            monotonic_ns=self.clock.mono_ns,
            on_stall=self.stalls.append,
            **kwargs,
        )

    def pin(self, ms: float, high_ms: float | None = 192.0) -> float:
        self.clock.set_ms(ms)
        stamp = self.clock.wall()
        self.witness.note_pin_edge(stamp)
        if high_ms is not None:
            self.witness.note_pin_release(stamp, stamp + high_ms / 1000.0, high_ms)
        return stamp

    def mic(self, ms: float, **kwargs) -> float:
        self.clock.set_ms(ms)
        stamp = self.clock.wall()
        self.witness.note_mic_edge(stamp, **kwargs)
        return stamp

    def flush_at(self, ms: float, **kwargs) -> list[dict]:
        self.clock.set_ms(ms)
        self.witness.flush(**kwargs)
        return self.records

    def kinds(self) -> list[str]:
        return [record["kind"] for record in self.records]


def test_mic_25_ms_after_the_pin_is_matched():
    w = _Witness()
    pin = w.pin(1000.0)
    w.mic(1025.0, ops_state="armed")
    w.flush_at(3000.0)

    assert w.kinds() == ["matched"]
    record = w.records[0]
    assert record["delta_ms"] == pytest.approx(25.0)
    assert record["pin_high_ms"] == pytest.approx(192.0)
    assert record["pin_timestamp"] == pin
    assert record["window_ms"] == [0.0, 60.0]
    assert w.witness.mic_delta_ms(pin) == pytest.approx(25.0)
    assert w.witness.pin_fields(pin) == {
        "ops_speed_witness": "matched",
        "ops_speed_mic_delta_ms": pytest.approx(25.0),
        "ops_speed_pin_high_ms": pytest.approx(192.0),
    }


@pytest.mark.parametrize(("delta_ms", "kind"), [(0.0, "matched"), (60.0, "matched")])
def test_window_ends_are_inclusive(delta_ms, kind):
    w = _Witness()
    w.pin(1000.0)
    w.mic(1000.0 + delta_ms)
    w.flush_at(3000.0)
    assert w.kinds() == [kind]


@pytest.mark.parametrize("delta_ms", [-0.5, 60.5])
def test_mic_outside_the_window_is_not_matched(delta_ms):
    w = _Witness()
    w.pin(1000.0)
    w.mic(1000.0 + delta_ms, ops_state="busy")
    w.flush_at(3000.0)
    assert sorted(w.kinds()) == ["mic_without_pin", "pin_without_mic"]


def test_pin_without_mic_waits_for_its_release():
    w = _Witness()
    stamp = w.pin(1000.0, high_ms=None)
    assert w.flush_at(1100.0) == []  # window closed, still HIGH
    assert w.witness.pin_fields(stamp)["ops_speed_witness"] == "pending"
    w.witness.note_pin_release(stamp, stamp + 0.192, 192.0)
    w.flush_at(1200.0)
    assert w.kinds() == ["pin_without_mic"]
    assert w.records[0]["pin_high_ms"] == pytest.approx(192.0)
    assert w.records[0]["pin_released"] is True


def test_pin_still_high_is_decided_after_the_release_wait():
    w = _Witness()
    w.pin(1000.0, high_ms=None)
    w.flush_at(1999.0)
    assert w.records == []
    w.flush_at(2000.0)
    assert w.kinds() == ["pin_without_mic"]
    assert w.records[0]["pin_high_ms"] is None
    assert w.records[0]["pin_released"] is False


@pytest.mark.parametrize("ops_state", ["armed", "busy"])
def test_mic_without_pin_carries_the_ops_state(ops_state):
    w = _Witness()
    w.mic(1000.0, ops_state=ops_state, fallback_pulse=True)
    w.flush_at(1005.0)
    assert w.records == []  # within the grace for a late pin edge
    w.flush_at(1010.0)
    assert w.kinds() == ["mic_without_pin"]
    assert w.records[0]["ops_state"] == ops_state
    assert w.records[0]["fallback_pulse"] is True
    assert w.witness.summary()[f"mic_without_pin_{ops_state}"] == 1


def test_ops_state_comes_from_the_provider_for_an_observer_call():
    states = iter(["busy"])
    w = _Witness(ops_state=lambda: next(states))
    w.mic(1000.0)
    w.flush_at(1100.0)
    assert w.records[0]["ops_state"] == "busy"


def test_fallback_pulse_can_be_noted_after_the_edge():
    w = _Witness()
    stamp = w.mic(1000.0, ops_state="armed")
    w.witness.note_fallback_pulse(stamp)
    w.flush_at(1100.0)
    assert w.records[0]["fallback_pulse"] is True


def test_glitch_is_reported_at_the_next_flush():
    w = _Witness()
    stamp = w.clock.at_ms(500.0)
    w.witness.note_pin_glitch(stamp, stamp + 0.001, 1.0)
    w.flush_at(500.0)
    assert w.kinds() == ["glitch"]
    assert w.records[0]["pin_high_ms"] == pytest.approx(1.0)


def test_each_pin_pairs_with_one_mic_only():
    w = _Witness()
    w.pin(1000.0)
    w.mic(1020.0, ops_state="busy")
    w.mic(1050.0, ops_state="busy")
    w.flush_at(3000.0)
    assert sorted(w.kinds()) == ["matched", "mic_without_pin"]
    matched = next(record for record in w.records if record["kind"] == "matched")
    assert matched["delta_ms"] == pytest.approx(20.0)


def test_a_slow_pin_callback_still_pairs_within_the_grace():
    w = _Witness()
    w.mic(1025.0, ops_state="armed")
    assert w.flush_at(1030.0) == []  # the mic edge waits out its grace
    w.witness.note_pin_edge(w.clock.at_ms(1000.0))  # delivered after the debounce
    w.witness.note_pin_release(w.clock.at_ms(1000.0), w.clock.at_ms(1192.0), 192.0)
    w.flush_at(3000.0)
    assert w.kinds() == ["matched"]


def test_flush_everything_decides_pending_edges():
    w = _Witness()
    w.pin(1000.0, high_ms=None)
    w.mic(1500.0, ops_state="armed")
    w.flush_at(1500.0, everything=True)
    assert sorted(w.kinds()) == ["mic_without_pin", "pin_without_mic"]
    assert w.witness.summary()["pending_pins"] == 0


def test_summary_collects_the_delays_for_recalibration():
    w = _Witness()
    for index, delta in enumerate((24.0, 26.0, 25.0)):
        start = 1000.0 + index * 3000.0
        w.pin(start, high_ms=190.0 + index)
        w.mic(start + delta, ops_state="busy")
    w.flush_at(20000.0)
    summary = w.witness.summary()
    assert summary["matched"] == 3
    assert summary["delta_ms"]["n"] == 3
    assert summary["delta_ms"]["median"] == pytest.approx(25.0)
    assert summary["pin_high_ms"]["median"] == pytest.approx(191.0)
    assert summary["window_ms"] == [0.0, 60.0]


def test_a_failing_sink_does_not_break_the_witness(caplog):
    def sink(_fields):
        raise RuntimeError("disk full")

    witness = OpsSpeedWitness(sink)
    witness.note_mic_edge(time.time() - 1.0, ops_state="armed")
    witness.flush()
    assert witness.summary()["mic_without_pin"] == 1
    assert "Witness sink failed" in caplog.text


def test_start_and_stop_flush_on_their_own():
    records = []
    witness = OpsSpeedWitness(records.append)
    witness.start(interval_s=0.01)
    witness.note_mic_edge(time.time(), ops_state="busy")
    deadline = time.monotonic() + 1.0
    while not records and time.monotonic() < deadline:
        time.sleep(0.01)
    witness.note_pin_edge(time.time())
    witness.stop()
    assert [record["kind"] for record in records] == ["mic_without_pin", "pin_without_mic"]


def test_lookups_for_an_unknown_pin_edge():
    w = _Witness()
    assert w.witness.mic_delta_ms(EPOCH) is None
    assert w.witness.pin_fields(EPOCH)["ops_speed_witness"] is None


# --- stall rule (ruling 4) ----------------------------------------------------------------


def test_three_armed_mic_edges_without_pin_ask_for_one_rearm():
    w = _Witness(stall_mic_edges=3)
    for ms in (1000.0, 2000.0, 3000.0):
        w.mic(ms, ops_state="armed")
        w.flush_at(ms + 20.0)
    assert len(w.stalls) == 1
    assert w.stalls[0]["mic_edges"] == 3
    assert w.stalls[0]["mic_timestamp"] == w.clock.at_ms(3000.0)
    assert w.witness.summary()["stall"] == 1

    # It counts again from zero: two more do not re-arm again.
    for ms in (4000.0, 5000.0):
        w.mic(ms, ops_state="armed")
        w.flush_at(ms + 20.0)
    assert len(w.stalls) == 1


@pytest.mark.parametrize("breaker", ["pin", "busy"])
def test_a_pin_edge_or_a_busy_ops_resets_the_stall_count(breaker):
    w = _Witness(stall_mic_edges=3)
    for ms in (1000.0, 2000.0):
        w.mic(ms, ops_state="armed")
        w.flush_at(ms + 20.0)
    if breaker == "pin":
        w.pin(2500.0)
    else:
        w.mic(2500.0, ops_state="busy")
    w.flush_at(2520.0)
    w.mic(3000.0, ops_state="armed")
    w.flush_at(3020.0)
    assert w.stalls == []


def test_stall_rule_can_be_turned_off():
    w = _Witness(stall_mic_edges=0)
    for ms in (1000.0, 2000.0, 3000.0, 4000.0):
        w.mic(ms, ops_state="armed")
    w.flush_at(5000.0)
    assert w.stalls == []


def test_a_failing_stall_callback_is_logged(caplog):
    def on_stall(_fields):
        raise RuntimeError("re-arm failed")

    witness = OpsSpeedWitness(lambda _f: None, stall_mic_edges=1, on_stall=on_stall)
    witness.note_mic_edge(time.time() - 1.0, ops_state="armed")
    witness.flush()
    assert "OPS stuck idle" in caplog.text
    assert "Witness sink failed" in caplog.text


# --- firmware gate (ruling 7) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "fragment"),
    [
        ("1.3.0", "below 1.3.3"),
        ("1.4.0", "outside the 1.3 release train"),
        (None, "unknown"),
        ("abc", "not major.minor.patch"),
    ],
)
def test_firmware_gate_refuses_with_a_reason(version, fragment):
    supported, reason = speed_trigger_supported(version, off_commands=("XX",))
    assert supported is False and fragment in reason


def test_firmware_gate_refuses_without_an_off_command():
    assert ops243.SPEED_TRIGGER_OFF_COMMANDS == ()
    supported, reason = speed_trigger_supported("1.3.3")
    assert supported is False and "off command is unknown" in reason


def test_firmware_gate_reads_the_off_command_at_call_time(monkeypatch):
    monkeypatch.setattr(ops243, "SPEED_TRIGGER_OFF_COMMANDS", ("XX",))
    assert speed_trigger_supported("1.3.3") == (True, None)


# --- config ------------------------------------------------------------------------------------


def test_config_defaults_match_the_brief():
    config = OpsSpeedConfig(mode=OPS_SPEED, pin=22, magnitude=40)
    assert config.as_dict() == {
        "mode": "ops-speed",
        "pin": 22,
        "magnitude": 40,
        "pin_active": "high",
        "pin_pull": "down",
        "debounce_ms": 0.0,
        "holdoff_ms": 100.0,
        "threshold_mph": 25,
        "pre_trigger_segments": 2,
        "ti_delay_ms": 25.0,
        "anchor_ms": 28.0,
        "witness_window_ms": [0.0, 60.0],
        "reject_practice_swings": False,
        "stall_mic_edges": 3,
        "shadow": False,
    }
    assert (DEFAULT_TI_DELAY_MS, DEFAULT_ANCHOR_MS) == (25.0, 28.0)
    assert DEFAULT_WITNESS_WINDOW_MS == (0.0, 60.0)
    assert OPS_SPEED_SHOT_TRIGGERS == ("ops-speed-shadow", "ops-speed")


def test_config_from_args_keeps_defaults_for_unset_flags():
    args = argparse.Namespace(
        shot_trigger=OPS_SPEED_SHADOW,
        ops_speed_pin=22,
        ops_speed_magnitude=40,
        ops_speed_pin_active="low",
        ops_speed_pin_pull=None,
        ops_speed_witness_window_ms="5,50",
        ops_speed_reject_practice_swings=False,
    )
    config = OpsSpeedConfig.from_args(args)
    assert config.shadow
    assert config.pin_pull == "up"
    assert config.witness_window_ms == (5.0, 50.0)
    assert config.threshold_mph == 25


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "sound"),
        ("pin", 28),
        ("pin", -1),
        ("magnitude", 0),
        ("threshold_mph", 0),
        ("pre_trigger_segments", 33),
        ("debounce_ms", 5.5),
        ("holdoff_ms", -1.0),
        ("ti_delay_ms", 41.0),
        ("anchor_ms", float("nan")),
        ("witness_window_ms", (60.0, 0.0)),
        ("pin_pull", "up"),
        ("stall_mic_edges", -1),
    ],
)
def test_config_rejects_out_of_range_values(field, value):
    with pytest.raises(ValueError):
        OpsSpeedConfig(**{"mode": OPS_SPEED, "pin": 22, "magnitude": 40, field: value})


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0,60", (0.0, 60.0)), (" 5 , 50 ", (5.0, 50.0)), ((0, 60), (0.0, 60.0))],
)
def test_parse_window(text, expected):
    assert parse_window_ms(text) == expected


@pytest.mark.parametrize("text", ["60", "0,60,90", "a,b", "60,0", "0,500"])
def test_parse_window_rejects(text):
    with pytest.raises(ValueError):
        parse_window_ms(text)


# --- split check ---------------------------------------------------------------------------------


def test_split_check_s2_at_20_ksps_with_the_28_ms_anchor_is_fine():
    assert ops_speed_split_check(2, 28.0, 20.0) is None  # 12.8 + 28 = 40.8 ms of history


def test_split_check_levels():
    # S#1 at 20 ksps keeps 6.4 ms; + 20 ms anchor = 26.4 ms: above MIN, below FULL.
    level, message = ops_speed_split_check(1, 20.0, 20.0)
    assert level == "warning" and "--ops-speed-pre-trigger 2" in message
    assert MIN_EDGE_HISTORY_MS <= 26.4 < FULL_EDGE_HISTORY_MS
    level, _message = ops_speed_split_check(1, 5.0, 20.0)  # 11.4 ms < MIN
    assert level == "error"


def test_split_check_refuses_s0_whatever_the_anchor():
    level, message = ops_speed_split_check(0, 60.0, 20.0)
    assert level == "error" and "S#0" in message


# --- practice-swing verdict ------------------------------------------------------------------------


def _processed(*, source="sound_trigger", ball=60.0, club=58.0, smash=None, rejection=None):
    smash = ball / club if smash is None and club else smash
    return SimpleNamespace(
        impact=SimpleNamespace(source=source, speed_delta_mph=2.0),
        club_speed_mph=club,
        ball_speed_mph=ball,
        smash_factor=smash,
        capture=None,
        timeline=None,
        rejection_reason=rejection,
    )


@pytest.mark.parametrize(
    ("kwargs", "verdict"),
    [
        ({"source": "sound_trigger", "ball": 60.0, "club": 58.0}, "practice_swing"),
        ({"source": "ops_transition", "ball": 60.0, "club": 58.0}, "shot"),
        ({"source": "sound_trigger", "ball": 70.0, "club": 50.0}, "shot"),
        ({"source": "sound_trigger", "ball": 61.0, "club": 58.0}, "shot"),  # 1.052
        ({"source": "sound_trigger", "ball": 60.0, "club": None}, "unknown"),
        (
            {"source": None, "ball": 60.0, "club": 58.0, "rejection": "no_ball_track"},
            "practice_swing",
        ),
    ],
)
def test_practice_swing_rule(kwargs, verdict):
    result = practice_swing_verdict(_processed(**kwargs))
    assert result["verdict"] == verdict
    assert result["max_smash"] == 1.05


def test_practice_swing_without_a_processed_capture_is_unknown():
    assert practice_swing_verdict(None)["verdict"] == "unknown"


def test_practice_swing_features_on_a_real_capture():
    processed = RollingBufferProcessor().process_capture(
        _fixture_capture(0), anchor_club_to_trigger=True
    )
    result = practice_swing_verdict(processed)
    assert result["verdict"] in ("practice_swing", "shot", "unknown")
    assert result["impact_source"] == processed.impact_source
    assert isinstance(result["post_anchor_repeat_count"], int)
    assert result["post_anchor_repeat_count"] > 0
