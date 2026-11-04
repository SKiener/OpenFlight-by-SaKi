"""B2 speed trigger end to end: a simulated OPS243-A 1.3.3, fake pin 1 and microphone inputs,
the real trigger gate, speed trigger, witness and rolling-buffer monitor (and the TI monitor).

``ops-speed`` (M4): pin 1 drives TI, camera and gate, HOST_INT is never pulsed, the club anchor
lies 28 ms after the trip. ``ops-speed-shadow`` (M3): a microphone edge 0-60 ms after a trip
claims it (no pulse, the anchor is the microphone edge), a microphone edge without a trip pulses
HOST_INT, a trip no microphone edge claimed is kept as ``ops_speed_unmatched`` with its I/Q.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from openflight.iwr6843.monitor import IWR6843CaptureMonitor
from openflight.ops243 import OPS243Radar
from openflight.ops_speed import (
    OPS_SPEED,
    OPS_SPEED_SHADOW,
    OPS_STATE_ARMED,
    OPS_STATE_BUSY,
    OpsSpeedConfig,
    OpsSpeedWitness,
    pin_button_factory,
)
from openflight.rolling_buffer import RollingBufferMonitor
from openflight.rolling_buffer.processor import RollingBufferProcessor
from openflight.rolling_buffer.types import IQCapture
from openflight.session_logger import SessionLogger
from openflight.trigger_gate import EdgeListener, TriggerGate
from tests.ops_speed_sim import FakePinFactory, SimulatedOps133, dump_bytes, sim_radar
from tests.test_iwr6843_monitor import FakeButton, FakeRadar, _raw_dump
from tests.test_trigger_gate import CLUB_FIXTURE, FakeClock, FakeOutput, _fixture_capture

KSPS = 30
SPLIT = 2
# S#2 at 30 ksps: the trip sits 8.53 ms into the 136.5 ms buffer, 128 ms before its end.
TRIP_OFFSET_MS = SPLIT * 128 / KSPS
POST_TRIGGER_S = (32 - SPLIT) * 128 / KSPS / 1000.0
# First byte = trip + post-trigger span + a UART lag inside the gate's match window.
DUMP_DELAY_S = POST_TRIGGER_S + 0.020
SHOT_CAPTURE = 1  # fixture: a shot (smash 1.06) at either anchor
PRACTICE_CAPTURE = 0  # fixture: the practice-swing rule calls it practice_swing (smash 1.03)
WAIT_S = 5.0


def _dump(index: int) -> bytes:
    """A fixture capture with its trip at the S#2 split (sound edge 25.5 ms after it)."""
    with np.load(CLUB_FIXTURE) as data:
        return dump_bytes(
            data["i_samples"][index],
            data["q_samples"][index],
            sample_time=100.0,
            trigger_time=100.0 + TRIP_OFFSET_MS / 1000.0,
        )


class DelayedDumpOps133(SimulatedOps133):
    """The simulated board, streaming each dump as late as a real one arrives.

    Pin 1 rises at the trip; the dump's first byte follows the post-trigger
    span plus a UART lag later (``dump_delay_s``), then pin 1 falls. With
    ``dump_delay_s`` None the dump waits for ``stream_at(wall_time)``.
    """

    def __init__(self, *, dump_delay_s: float | None = DUMP_DELAY_S, **kwargs):
        super().__init__(**kwargs)
        self.dump_delay_s = dump_delay_s
        self.streamed = threading.Event()
        self.first_byte_at: float | None = None
        self._held = None

    def _dump(self, *, with_pin: bool, trip_message: bytes) -> None:
        self.armed = False
        pin = self.pin if with_pin else None
        if pin is not None:
            pin.press()

        def stream() -> None:
            if trip_message:
                self.feed(trip_message)
            self.feed(self.dump)
            if pin is not None:
                pin.release()
            self.streamed.set()

        if self.dump_delay_s is None:
            self._held = stream
            return
        timer = threading.Timer(self.dump_delay_s, stream)
        timer.daemon = True
        timer.start()

    def stream_at(self, wall_time: float) -> None:
        """Stream the held dump so its first byte arrives at ``wall_time`` (``first_byte_at``)."""
        self.first_byte_at = wall_time
        stream, self._held = self._held, None
        timer = threading.Timer(max(0.0, wall_time - time.time()), stream)
        timer.daemon = True
        timer.start()


class CountingCamera:
    """Camera ring double: counts the edges it was told about."""

    running = False

    def __init__(self):
        self.edges: list[float] = []

    def notify_trigger(self, timestamp=None) -> bool:
        self.edges.append(timestamp)
        return len(self.edges) == 1


class TimedTiRadar(FakeRadar):
    """TI transport double that notes when ``l3dump`` was sent (monotonic ns)."""

    def __init__(self):
        super().__init__(_raw_dump())
        self.read_started_ns: list[int] = []

    def read_dump(self):
        self.read_started_ns.append(time.monotonic_ns())
        return super().read_dump()


@dataclass
class Rig:  # pylint: disable=too-many-instance-attributes
    """One speed-trigger session: board, inputs, gate, witness, monitor, session log."""

    config: OpsSpeedConfig
    sim: DelayedDumpOps133
    gate: TriggerGate
    output: FakeOutput
    witness: OpsSpeedWitness
    monitor: RollingBufferMonitor
    session: SessionLogger
    log_dir: Path
    camera: CountingCamera
    mic: EdgeListener
    pin_factory: FakePinFactory
    stoppers: list = field(default_factory=list)
    events: list = field(default_factory=list)
    shots: list = field(default_factory=list)
    clock_syncs: list = field(default_factory=list)
    stalls: list = field(default_factory=list)
    ti: IWR6843CaptureMonitor | None = None
    ti_radar: TimedTiRadar | None = None
    _new_event: threading.Event = field(default_factory=threading.Event)

    def wait_event(self, count: int = 1) -> dict:
        deadline = time.monotonic() + WAIT_S
        while len(self.events) < count:
            self._new_event.clear()
            if len(self.events) >= count:
                break
            assert time.monotonic() < deadline, f"no trigger event {count} (got {self.events})"
            self._new_event.wait(0.05)
        return self.events[count - 1]

    def on_event(self, event: dict) -> None:
        self.events.append(event)
        self._new_event.set()

    def wait_armed(self) -> None:
        """Until the re-arm's final PA (PA / S#n / PA) armed the board and the gate knows it."""
        deadline = time.monotonic() + WAIT_S
        while not (
            self.sim.armed and self.sim.commands[-1] == "PA" and not self.gate.ops_busy_now()
        ):
            assert time.monotonic() < deadline, "the speed trigger never armed the board"
            threading.Event().wait(0.01)

    def pin_record(self):
        records = [record for record in self.gate._records if record.ops_speed_edge_timestamp]
        return records[-1]

    def swing(self, *, mic_after_ms: float | None = None, club_mph: float = 80.0) -> float:
        """A swing that trips the board; the microphone hears it ``mic_after_ms`` later."""
        self.wait_armed()
        self.sim.streamed.clear()
        assert self.sim.fire(club_mph)
        pin_timestamp = self.pin_record().ops_speed_edge_timestamp
        if mic_after_ms is not None:
            assert self.mic.notify_edge(pin_timestamp + mic_after_ms / 1000.0)
        return pin_timestamp

    def records(self, entry_type: str) -> list[dict]:
        self.close()
        return [
            entry
            for log in self.log_dir.glob("*.jsonl")
            for line in log.read_text(encoding="utf-8").splitlines()
            if (entry := json.loads(line))["type"] == entry_type
        ]

    def close(self) -> None:
        while self.stoppers:
            self.stoppers.pop()()


def _quick_replies(monkeypatch, rig_clock_syncs: list) -> None:
    """Read the simulator's synchronous replies at once; a canned, unusable clock sync."""

    def read_reply(radar, *_args, **_kwargs):
        waiting = radar.serial.in_waiting
        return radar.serial.read(waiting).decode("ascii", errors="ignore") if waiting else ""

    def read_clock_sync(_radar, **_kwargs):
        rig_clock_syncs.append(time.time())
        return {"usable_for_trigger_timestamps": False, "clock_sync_method": "test"}

    monkeypatch.setattr(OPS243Radar, "_read_reply", read_reply)
    monkeypatch.setattr(OPS243Radar, "read_clock_sync", read_clock_sync)


def build_rig(  # pylint: disable=too-many-locals
    tmp_path: Path,
    monkeypatch,
    mode: str,
    *,
    with_ti: bool = False,
    capture: int = SHOT_CAPTURE,
    **config_kwargs,
) -> Rig:
    clock_syncs: list = []
    _quick_replies(monkeypatch, clock_syncs)
    config = OpsSpeedConfig(mode=mode, pin=22, magnitude=40, **config_kwargs)
    log_dir = tmp_path / "logs"
    session = SessionLogger(log_dir=log_dir)
    session.start_session()
    monkeypatch.setattr("openflight.rolling_buffer.monitor.get_session_logger", lambda: session)
    monkeypatch.setattr(
        "openflight.rolling_buffer.monitor.log_session_error", lambda *_a, **_k: None
    )

    output = FakeOutput(FakeClock())
    gate = TriggerGate(
        pin=27,
        output_factory=output.factory,
        background=False,
        edge_source="ops-speed" if mode == OPS_SPEED else "sound",
        ops_speed_shadow=mode == OPS_SPEED_SHADOW,
        ops_speed_anchor_ms=config.anchor_ms,
        ops_speed_window_ms=config.witness_window_ms,
        ops_speed_post_trigger_ms=POST_TRIGGER_S * 1000.0,
    )
    gate.start()
    stalls: list = []
    witness = OpsSpeedWitness(
        session.log_ops_speed_trigger,
        window_ms=config.witness_window_ms,
        ops_state=lambda: OPS_STATE_BUSY if gate.ops_busy_now() else OPS_STATE_ARMED,
        stall_mic_edges=config.stall_mic_edges if mode == OPS_SPEED else 0,
        on_stall=stalls.append,
    )
    camera = CountingCamera()
    pin_factory = FakePinFactory()
    button_factory = pin_button_factory(
        22,
        factory=pin_factory,
        on_release=witness.note_pin_release,
        on_glitch=witness.note_pin_glitch,
    )
    stoppers = [gate.stop]
    ti = ti_radar = None

    def mic_observer(edge_timestamp: float) -> None:
        # As server._ops_speed_mic_edge: the OPS state at the edge, before its own pulse.
        witness.note_mic_edge(
            edge_timestamp,
            ops_state=OPS_STATE_BUSY if gate.ops_busy_now(edge_timestamp) else OPS_STATE_ARMED,
            fallback_pulse=gate.ops_speed_fallback_for(edge_timestamp),
        )

    if mode == OPS_SPEED:
        observers = [gate.on_edge, camera.notify_trigger, witness.note_pin_edge]
        if with_ti:
            config_path = tmp_path / "radar.cfg"
            config_path.write_text("sensorStart\n", encoding="utf-8")
            ti_radar = TimedTiRadar()
            ti = IWR6843CaptureMonitor(
                config_path=config_path,
                output_dir=tmp_path / "dumps",
                radar=ti_radar,
                gpio_pin=22,
                button_factory=button_factory,
                trigger_observers=observers,
                l3dump_delay_s=config.ti_delay_ms / 1000.0,
            )
            ti.start()
            stoppers.append(ti.stop)
        else:
            pin = EdgeListener(pin=22, observers=observers, button_factory=button_factory)
            pin.start()
            pin.arm()
            stoppers.append(pin.stop)
        mic = EdgeListener(pin=17, observers=[mic_observer], button_factory=FakeButton)
    else:
        pin = EdgeListener(
            pin=22,
            observers=[gate.note_ops_speed_edge, camera.notify_trigger, witness.note_pin_edge],
            button_factory=button_factory,
        )
        pin.start()
        pin.arm()
        stoppers.append(pin.stop)
        mic = EdgeListener(
            pin=17,
            observers=[gate.on_edge, camera.notify_trigger, mic_observer],
            button_factory=FakeButton,
        )
    mic.start()
    mic.arm()
    stoppers.append(mic.stop)

    sim = DelayedDumpOps133(pin=pin_factory.button, dump=_dump(capture), settings_survive_dump=True)
    monitor = RollingBufferMonitor(
        port=None,
        trigger_type="ops-speed",
        sample_rate_ksps=KSPS,
        pre_trigger_segments=SPLIT,
        config=config,
    )
    radar = sim_radar(sim, firmware_version="1.3.3")
    radar.fast_rearm = True
    radar.connect = lambda **_kwargs: True
    monitor.radar = radar
    monitor.connect()
    monitor.set_trigger_gate(gate)
    monitor.set_ops_speed_witness(witness)
    rig = Rig(
        config=config,
        sim=sim,
        gate=gate,
        output=output,
        witness=witness,
        monitor=monitor,
        session=session,
        log_dir=log_dir,
        camera=camera,
        mic=mic,
        pin_factory=pin_factory,
        stoppers=stoppers,
        clock_syncs=clock_syncs,
        stalls=stalls,
        ti=ti,
        ti_radar=ti_radar,
    )
    monitor.start(shot_callback=rig.shots.append, diagnostic_callback=rig.on_event)
    stoppers.extend([session.end_session, witness.stop, monitor.stop])
    return rig


@pytest.fixture(name="rigs")
def _rigs():
    built: list[Rig] = []
    yield built
    for rig in built:
        rig.close()


# --- ops-speed (M4) ------------------------------------------------------------------------------


def test_m4_pin_edge_drives_gate_camera_and_ti_without_a_pulse(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED, with_ti=True)
    rigs.append(rig)
    assert rig.sim.commands[:8] == ["PI", "US", "P3", "S=30", "SM40", "ST-25", "S#2", "??"]

    pin_timestamp = rig.swing(mic_after_ms=25.0)
    event = rig.wait_event()

    assert rig.output.pulses == [], "BCM27 must never be pulsed in ops-speed"
    assert rig.output.created_with == (27, True, False), "HOST_INT stays claimed LOW"
    assert rig.camera.edges == [pin_timestamp]
    # The TI held its l3dump the 25 ms delay after the pin edge.
    capture = rig.ti.capture_for_shot(pin_timestamp + 0.028, timeout_s=2.0)
    assert capture is not None and capture.valid
    assert capture.trigger_timestamp == pin_timestamp
    assert capture.l3dump_target_ms == 25.0 and capture.l3dump_actual_ms >= 25.0
    assert rig.ti_radar.read_started_ns

    assert event["accepted"] is True and rig.shots
    assert event["gate_decision"] == "ops_speed" and event["gate_edge_source"] == "ops-speed"
    assert event["sound_edge_delay_ms"] == pytest.approx(-28.0)
    assert event["ops_speed_dump"] == "pin"
    assert event["ops_speed_witness"] == "matched"
    assert event["ops_speed_mic_delta_ms"] == pytest.approx(25.0, abs=0.5)
    assert event["ops_speed_pin_high_ms"] is not None
    assert event["ops_speed_practice_swing"]["verdict"] == "shot"
    assert rig.clock_syncs, "an ops-speed shot gets its clock sync"

    captures = rig.records("rolling_buffer_capture")
    assert len(captures) == 1 and captures[0]["shot_number"] == 1
    assert captures[0]["sound_edge_delay_ms"] == pytest.approx(-28.0)
    assert captures[0]["sound_edge_offset_ms"] == pytest.approx(TRIP_OFFSET_MS + 28.0, abs=1.0)
    # The anchored club also runs the ball-track guard (a good shot: no rule fires).
    assert captures[0]["ball_guard"] is not None and captures[0]["ball_guard"]["fired"] is False
    assert captures[0]["ball_speed_status"] is None
    witness = [entry for entry in rig.records("ops_speed_trigger") if entry["kind"] == "matched"]
    assert len(witness) == 1 and witness[0]["delta_ms"] == pytest.approx(25.0, abs=0.5)


def test_m4_microphone_edge_alone_is_a_witness_record_not_a_gate_edge(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED)
    rigs.append(rig)
    rig.wait_armed()

    assert rig.mic.notify_edge(time.time())
    rig.witness.flush(everything=True)

    assert list(rig.gate._records) == [] and rig.output.pulses == []
    mic = [
        entry for entry in rig.records("ops_speed_trigger") if entry["kind"] == "mic_without_pin"
    ]
    assert len(mic) == 1 and mic[0]["ops_state"] == OPS_STATE_ARMED


def test_m4_three_armed_microphone_edges_rearm_the_stuck_board_once(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED)
    rigs.append(rig)
    rig.wait_armed()
    rig.sim.armed = False  # the board dropped out of its armed state
    now = time.time()
    for k in range(3):
        rig.mic.notify_edge(now - 1.0 + 0.2 * k)
    rig.witness.flush(everything=True)
    assert len(rig.stalls) == 1
    rig.monitor.trigger.request_rearm()

    rig.wait_armed()  # the capture thread re-armed it from its wait
    event = rig.wait_event()
    assert event["reason"] == "ops_speed_stall_rearm" and event["accepted"] is False


def test_m4_anchor_moved_26_ms_earlier_gives_the_sound_mode_club_speed():
    """A trip 26 ms before the sound edge, anchored 26 ms after it, is the sound-mode anchor."""
    processor = RollingBufferProcessor()
    clock = FakeClock()
    gate = TriggerGate(
        pin=27,
        output_factory=FakeOutput(clock).factory,
        monotonic_ns=clock.monotonic_ns,
        wall_time=clock.wall,
        background=False,
        edge_source="ops-speed",
        ops_speed_anchor_ms=26.0,
    )
    gate.start()
    for index in (2, 5, 9):
        sound = processor.process_capture(_fixture_capture(index), anchor_club_to_trigger=True)
        with np.load(CLUB_FIXTURE) as data:
            edge_ms = float(data["trigger_offset_ms"][index])
            moved = IQCapture(
                sample_time=0.0,
                trigger_time=(edge_ms - 26.0) / 1000.0,
                i_samples=data["i_samples"][index].astype(int).tolist(),
                q_samples=data["q_samples"][index].astype(int).tolist(),
            )
        trip = 1_760_000_000.0 + index
        moved.first_byte_timestamp = trip + (moved.post_trigger_duration_ms + 17.0) / 1000.0
        gate.note_ops_speed_edge(trip)
        fields = gate.apply_to_capture(moved)
        speed = processor.process_capture(moved, anchor_club_to_trigger=True)

        assert fields["gate_decision"] == "ops_speed" and moved.sound_edge_delay_ms == -26.0
        assert moved.sound_edge_offset_ms == pytest.approx(edge_ms)
        assert speed.club_speed_mode == sound.club_speed_mode == "anchored"
        assert speed.club_speed_mph == sound.club_speed_mph
        assert speed.impact.timestamp_ms == pytest.approx(sound.impact.timestamp_ms)
    gate.stop()


# --- ops-speed-shadow (M3) -----------------------------------------------------------------------


def test_m3_microphone_after_the_trip_claims_it_without_a_pulse(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED_SHADOW)
    rigs.append(rig)

    pin_timestamp = rig.swing(mic_after_ms=25.5)
    event = rig.wait_event()

    assert rig.output.pulses == []
    record = rig.pin_record()
    assert record.edge_timestamp == pytest.approx(pin_timestamp + 0.0255)
    assert record.pulse_timestamp == pin_timestamp and record.reason == "sound_claimed"
    assert event["accepted"] is True and event["ops_speed_dump"] == "matched"
    # The anchor is the microphone edge, as in sound mode.
    assert event["sound_edge_delay_ms"] == pytest.approx(-25.5, abs=0.01)
    assert event["gate_edge_timestamp"] == pytest.approx(pin_timestamp + 0.0255)
    assert event["ops_speed_fallback"] is False
    assert event["ops_speed_witness"] == "matched"
    assert rig.camera.edges[0] == pin_timestamp  # the pin edge froze the ring first
    assert rig.gate.summary()["ops_speed"] == {
        "mode": OPS_SPEED_SHADOW,
        "trips": 0,
        "claimed": 0,
        "fallback": 0,
        "host_int_echoes": 0,
    }
    rig.gate.run_pending()
    assert rig.gate.summary()["ops_speed"]["trips"] == 1
    assert rig.gate.summary()["ops_speed"]["claimed"] == 1


def test_m3_microphone_without_a_trip_pulses_host_int(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED_SHADOW)
    rigs.append(rig)
    rig.wait_armed()

    edge = time.time()
    assert rig.mic.notify_edge(edge)
    assert rig.output.pulses, "a microphone edge without a speed trip pulses HOST_INT as today"
    assert rig.gate.ops_speed_fallback_for(edge) is True
    rig.witness.flush(everything=True)
    record = rig.gate._records[-1]
    assert record.ops_speed_fallback and record.decision == "passthrough"
    assert record.log_fields()["ops_speed_fallback"] is True
    mic = [
        entry for entry in rig.records("ops_speed_trigger") if entry["kind"] == "mic_without_pin"
    ]
    assert mic[0]["fallback_pulse"] is True


def test_m3_trip_without_a_microphone_edge_is_unmatched_with_its_iq(tmp_path, monkeypatch, rigs):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED_SHADOW)
    rigs.append(rig)

    rig.swing(mic_after_ms=None)
    event = rig.wait_event()

    assert event["accepted"] is False and event["reason"] == "ops_speed_unmatched"
    assert event["ops_speed_dump"] == "unmatched"
    assert event["ops_speed_witness"] == "pin_without_mic"
    assert not rig.shots and not rig.clock_syncs, "an unmatched dump skips the clock sync"
    # Unmatched trips anchor 28 ms after the trip.
    assert event["sound_edge_delay_ms"] == pytest.approx(-28.0)
    captures = rig.records("rolling_buffer_capture")
    assert len(captures) == 1
    assert captures[0]["shot_number"] is None
    assert captures[0]["rejection_reason"] == "ops_speed_unmatched"
    assert len(captures[0]["i_samples"]) == 4096


def test_m3_trip_pin_1_never_showed_is_no_fallback_shot(tmp_path, monkeypatch, rigs, caplog):
    """Pin 1 loose: the OPS trips itself, the microphone 26 ms later pulses HOST_INT for nothing.

    The dump started at the trip, so it must not count as that pulse's fallback dump nor be
    anchored at the pulse (+0 ms, 26 ms too early); the pin-silent detector must see it.
    """
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED_SHADOW)
    rigs.append(rig)
    rig.sim.pin = None
    rig.sim.dump_delay_s = None  # streamed from the fallback pulse on, see below
    radar = rig.monitor.radar
    read_dump = radar.wait_for_hardware_trigger

    def wait_stamping_the_arrival(on_first_byte=None, **kwargs):
        # The idle reader polls every 20 ms and the stream timer runs a few ms late: stamp
        # the first byte when the board sent it, so the gate's dump classification and its
        # busy tracking both see the lag this test sets.
        def first_byte() -> None:
            radar.last_hardware_trigger_first_byte_timestamp = rig.sim.first_byte_at
            if on_first_byte is not None:
                on_first_byte()

        response = read_dump(on_first_byte=first_byte, **kwargs)
        if response:
            radar.last_hardware_trigger_first_byte_timestamp = rig.sim.first_byte_at
        return response

    monkeypatch.setattr(radar, "wait_for_hardware_trigger", wait_stamping_the_arrival)
    uart_lag_ms, mic_after_ms = 17.0, 26.0

    for count in (1, 2):
        rig.wait_armed()
        rig.sim.streamed.clear()
        assert rig.sim.fire(80.0)
        threading.Event().wait(mic_after_ms / 1000.0)
        assert rig.mic.notify_edge(time.time())
        pulse = rig.gate._records[-1].pulse_timestamp
        assert rig.output.pulses and rig.gate._records[-1].ops_speed_fallback
        # Trip = pulse - 26 ms; first byte = trip + post-trigger span + UART lag, i.e. 9 ms
        # before the span after the pulse ends: no HOST_INT dump arrives that early.
        rig.sim.stream_at(pulse - mic_after_ms / 1000.0 + POST_TRIGGER_S + uart_lag_ms / 1000.0)
        event = rig.wait_event(count)

        assert event["ops_speed_dump"] == "self_trip"
        assert event["accepted"] is False and event["reason"] == "ops_speed_unmatched"
        assert event["gate_first_byte_match"] == "self_trip"
        # (The dump's trigger_time keeps 1 ms digits: its post-trigger span reads 0.5 ms short.)
        assert event["gate_nearest_first_byte_lag_ms"] == pytest.approx(-9.0, abs=0.5)
        # Anchored 28 ms after the trip, never at the fallback pulse (+0 ms).
        assert event["sound_edge_delay_ms"] == pytest.approx(-28.0)
    assert not rig.shots and not rig.clock_syncs
    assert "Pin 1 silent: 2 OPS dumps in a row" in caplog.text
    silent = [entry for entry in rig.records("ops_speed_trigger") if entry["kind"] == "pin_silent"]
    assert len(silent) == 1 and silent[0]["mode"] == OPS_SPEED_SHADOW
    # Each fallback pulse fell into the blind time of a dump it did not start.
    lost = [entry for entry in rig.records("trigger_event") if entry["reason"] == "ops_busy"]
    assert [entry["gate_edge_sequence"] for entry in lost] == [1, 2]


# --- practice swings and the pin-silent detector ------------------------------------------------


@pytest.mark.parametrize("reject", [False, True])
def test_practice_swing_is_logged_and_only_rejected_with_the_flag(
    tmp_path, monkeypatch, rigs, reject
):
    rig = build_rig(
        tmp_path,
        monkeypatch,
        OPS_SPEED,
        capture=PRACTICE_CAPTURE,
        reject_practice_swings=reject,
    )
    rigs.append(rig)

    rig.swing(mic_after_ms=None)
    event = rig.wait_event()

    verdict = event["ops_speed_practice_swing"]
    assert verdict["verdict"] == "practice_swing"
    assert verdict["impact_source"] != "ops_transition" and verdict["smash_factor"] < 1.05
    if reject:
        assert event["accepted"] is False and event["reason"] == "ops_speed_practice_swing"
        assert not rig.shots
    else:
        assert event["accepted"] is True and rig.shots


def test_pin_silent_detector_fires_after_two_dumps_without_a_pin_edge(
    tmp_path, monkeypatch, rigs, caplog
):
    rig = build_rig(tmp_path, monkeypatch, OPS_SPEED)
    rigs.append(rig)
    rig.sim.pin = None  # pin 1 not wired: the board trips, the Pi sees nothing

    for count in (1, 2):
        rig.wait_armed()
        assert rig.sim.fire(80.0)
        event = rig.wait_event(count)
        assert event["ops_speed_dump"] == "no_record"
        # Without a gate record the anchor still lies 28 ms after the trip.
        assert event["sound_edge_delay_ms"] == pytest.approx(-28.0)

    assert "Pin 1 silent: 2 OPS dumps in a row" in caplog.text
    silent = [entry for entry in rig.records("ops_speed_trigger") if entry["kind"] == "pin_silent"]
    assert silent == [{**silent[0], "mode": OPS_SPEED, "dumps_without_pin_edge": 2, "pin_bcm": 22}]


# --- OpsSpeedTrigger on its own ---------------------------------------------------------------


def _wait_radar(response: str = ""):
    from unittest.mock import Mock

    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = response
    radar.last_hardware_trigger_lost_capture_bytes = 0
    radar.rearm_rolling_buffer.return_value = True
    radar.last_rearm_armed_timestamp = None
    radar.last_rearm_drain_end_timestamp = None
    return radar


def _speed_trigger(mode=OPS_SPEED):
    from openflight.rolling_buffer.trigger import OpsSpeedTrigger

    return OpsSpeedTrigger(
        pre_trigger_segments=2, config=OpsSpeedConfig(mode=mode, pin=22, magnitude=40)
    )


def test_first_wait_arms_the_idle_board_without_the_retry_warning(caplog):
    from openflight.ops243 import SPEED_TRIGGER_MARKERS

    trigger = _speed_trigger()
    trigger.mark_initial_arm_pending()
    radar = _wait_radar()
    with caplog.at_level("INFO", logger="openflight.rolling_buffer.trigger"):
        assert trigger.wait_for_trigger(radar, None, timeout=0.1) is None
    radar.rearm_rolling_buffer.assert_called_once_with(2)
    assert "Arming the radar for the speed trigger" in caplog.text
    assert "Retrying the radar re-arm" not in caplog.text
    assert "Waiting for speed trigger" in caplog.text
    kwargs = radar.wait_for_hardware_trigger.call_args.kwargs
    assert kwargs["capture_markers"] == SPEED_TRIGGER_MARKERS
    # A later failed re-arm is retried with the usual warning.
    trigger.mark_rearm_pending()
    with caplog.at_level("WARNING", logger="openflight.rolling_buffer.trigger"):
        trigger.wait_for_trigger(radar, None, timeout=0.1)
    assert "Retrying the radar re-arm" in caplog.text


def test_request_rearm_wakes_an_idle_wait_and_rearms_once():
    trigger = _speed_trigger()
    radar = _wait_radar()
    seen = []

    def wait(**kwargs):
        seen.append(kwargs["cancel_event"].is_set())
        return ""

    radar.wait_for_hardware_trigger.side_effect = wait
    trigger.request_rearm()
    assert trigger.wait_for_trigger(radar, None, timeout=0.1) is None
    # Served before the wait; the next wait is not woken again.
    radar.rearm_rolling_buffer.assert_called_once_with(2)
    assert seen == [False]
    assert trigger.drain_diagnostics()[0]["reason"] == "ops_speed_stall_rearm"
    trigger.wait_for_trigger(radar, None, timeout=0.1)
    assert radar.rearm_rolling_buffer.call_count == 1


def test_request_during_the_wait_is_served_when_the_wait_wakes():
    stop = threading.Event()
    trigger = _speed_trigger()
    radar = _wait_radar()

    def wait(**kwargs):
        trigger.request_rearm()  # from the witness thread while the radar idles
        assert kwargs["cancel_event"].is_set() and not stop.is_set()
        return ""

    radar.wait_for_hardware_trigger.side_effect = wait
    assert trigger.wait_for_trigger(radar, None, timeout=0.1, cancel_event=stop) is None
    radar.rearm_rolling_buffer.assert_called_once_with(2)
    assert trigger.stall_rearms_requested == 1


def test_a_dump_rearm_answers_a_pending_stall_request():
    trigger = _speed_trigger()
    radar = _wait_radar()
    trigger.request_rearm()
    trigger._rearm(radar)  # the dump of a real swing re-armed the board meanwhile
    radar.rearm_rolling_buffer.reset_mock()
    trigger.wait_for_trigger(radar, None, timeout=0.1)
    radar.rearm_rolling_buffer.assert_not_called()


@pytest.mark.parametrize(
    ("mode", "kind", "counts"),
    [
        (OPS_SPEED, "pin", True),
        (OPS_SPEED, "no_record", True),
        (OPS_SPEED_SHADOW, "matched", True),
        (OPS_SPEED_SHADOW, "fallback", True),
        (OPS_SPEED_SHADOW, "unmatched", False),
        (OPS_SPEED_SHADOW, "self_trip", False),
        (OPS_SPEED_SHADOW, "no_record", False),
    ],
)
def test_dump_classification_decides_what_counts_and_the_clock_sync(mode, kind, counts):
    trigger = _speed_trigger(mode)
    trigger.set_dump_classifier(lambda _first_byte: kind)
    radar = _wait_radar()
    radar.last_hardware_trigger_preamble = b'{"Speed":"-26"}\r\n'
    radar.last_hardware_trigger_preamble_timestamp = 99.5
    trigger._note_dump(radar, 100.0)
    assert trigger.last_dump_counts is counts and trigger._wants_clock_sync() is counts
    assert trigger.last_dump_fields == {
        "ops_speed_dump": kind,
        "ops_speed_preamble": '{"Speed":"-26"}\r\n',
        "ops_speed_preamble_bytes": 17,
        "ops_speed_preamble_timestamp": 99.5,
    }


def test_failing_classifier_leaves_the_dump_unclassified(caplog):
    trigger = _speed_trigger(OPS_SPEED_SHADOW)
    trigger.set_dump_classifier(lambda _first_byte: 1 / 0)
    trigger._note_dump(_wait_radar(), 100.0)
    assert trigger.last_dump_fields["ops_speed_dump"] is None
    assert trigger.last_dump_counts is False  # the shadow cannot tell: no shot
    assert "classifier failed" in caplog.text


def test_pin_silent_detector_resets_on_a_pin_edge():
    trigger = _speed_trigger()
    silent = []
    trigger.set_pin_silent_callback(silent.append)
    kinds = iter(["no_record", "pin", "no_record", "no_record", "no_record", "pin", "no_record"])
    trigger.set_dump_classifier(lambda _first_byte: next(kinds))
    for _ in range(7):
        trigger._note_dump(_wait_radar(), 100.0)
    # Two in a row once (reported once per streak), then a pin edge resets it.
    assert silent == [2]


def test_pin_silent_detector_counts_self_trips_and_skips_fallback_dumps():
    """A shadow self trip is a dump without a pin edge; a fallback dump says nothing about pin 1."""
    trigger = _speed_trigger(OPS_SPEED_SHADOW)
    silent = []
    trigger.set_pin_silent_callback(silent.append)
    kinds = iter(["self_trip", "fallback", "no_record"])
    trigger.set_dump_classifier(lambda _first_byte: next(kinds))
    for _ in range(3):
        trigger._note_dump(_wait_radar(), 100.0)
    assert silent == [2]
