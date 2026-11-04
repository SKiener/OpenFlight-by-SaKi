"""--shot-trigger: TI-anchored gate records, the ti OPS split, CLI parsing and server wiring."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openflight import server as server_module
from openflight.iwr6843 import Calibration
from openflight.iwr6843.self_trigger import TiAnchorModel, parse_fire_latency
from openflight.trigger_gate import (
    DEFAULT_TI_PRE_TRIGGER_SEGMENTS,
    FULL_EDGE_HISTORY_MS,
    TriggerGate,
    pre_trigger_ms,
    ti_split_check,
)
from tests.test_trigger_gate import EDGE_EPOCH, FakeClock, _fixture_capture, make_gate

# --- gate: TI edges carry the anchor lead -----------------------------------------


def test_ti_edge_record_starts_with_the_unknown_rule_lead_and_takes_the_cause():
    clock = FakeClock()
    gate, output = make_gate(clock, edge_source="ti")
    gate.on_edge(EDGE_EPOCH)
    record = gate._records[-1]  # pylint: disable=protected-access
    model = gate.ti_anchor

    assert output.pulses, "a TI edge pulses HOST_INT at once in passthrough"
    assert record.edge_source == "ti"
    assert record.anchor_lead_ms == pytest.approx(model.lead_ms(None))
    assert gate.note_ti_cause(EDGE_EPOCH, "leave")
    assert record.ti_cause == "leave"
    assert record.anchor_lead_ms == pytest.approx(model.lead_ms("leave"))
    fields = record.log_fields()
    assert fields["gate_edge_source"] == "ti" and fields["ti_fire_cause"] == "leave"
    assert fields["sound_edge_delay_ms"] == pytest.approx(record.delay_ms + model.lead_ms("leave"))
    assert fields["gate_pulse_delay_ms"] == pytest.approx(record.delay_ms)
    assert not gate.note_ti_cause(EDGE_EPOCH + 5.0, "end")


def test_ti_anchor_moves_the_capture_club_anchor():
    clock = FakeClock()
    gate, _ = make_gate(clock, edge_source="ti")
    gate.on_edge(EDGE_EPOCH)
    gate.note_ti_cause(EDGE_EPOCH, "end")
    record = gate._records[-1]  # pylint: disable=protected-access
    capture = _fixture_capture(2, correct=False)

    fields = gate.apply_to_capture(capture)

    lead = gate.ti_anchor.lead_ms("end")
    assert capture.sound_edge_delay_ms == pytest.approx(record.delay_ms + lead)
    assert fields["sound_edge_offset_ms"] == pytest.approx(
        capture.trigger_offset_ms - record.delay_ms - lead
    )


def test_sound_edges_keep_a_zero_lead():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    record = gate._records[-1]  # pylint: disable=protected-access
    assert gate.ti_anchor is None and record.anchor_lead_ms == 0.0
    assert record.log_fields()["sound_edge_delay_ms"] == record.delay_ms
    assert record.log_fields()["ti_anchor_lead_ms"] is None
    assert not gate.note_ti_cause(EDGE_EPOCH, "end")
    with pytest.raises(ValueError):
        TriggerGate(pin=27, edge_source="laser")


# --- the ti OPS split ----------------------------------------------------------------


def test_default_ti_split_holds_the_slowest_rule_at_20_ksps():
    model = TiAnchorModel()
    assert DEFAULT_TI_PRE_TRIGGER_SEGMENTS == 9
    assert pre_trigger_ms(9, 20.0) == pytest.approx(57.6)
    assert pre_trigger_ms(9, 20.0) - model.max_lead_ms >= FULL_EDGE_HISTORY_MS
    assert ti_split_check(9, max_lead_ms=model.max_lead_ms, sample_rate_ksps=20.0) is None


def test_ti_split_check_levels():
    model = TiAnchorModel()
    # Offset 0: the slowest fire needs 6.1 + 31.4 ms; S#6 (38.4 ms) holds it, S#5 does not.
    assert ti_split_check(6, max_lead_ms=model.max_lead_ms, sample_rate_ksps=20.0) is None
    level, message = ti_split_check(5, max_lead_ms=model.max_lead_ms, sample_rate_ksps=20.0)
    assert level == "warning" and "--ti-pre-trigger 6" in message
    # The +0.2 m fallback's slowest fire (+18.45 ms) still fits S#9, not S#6.
    fallback = TiAnchorModel(fire_latency_ms=parse_fire_latency("worst=18.45"))
    assert ti_split_check(9, max_lead_ms=fallback.max_lead_ms, sample_rate_ksps=20.0) is None
    level, _ = ti_split_check(6, max_lead_ms=fallback.max_lead_ms, sample_rate_ksps=20.0)
    assert level == "warning"
    level, _ = ti_split_check(
        9, max_lead_ms=model.max_lead_ms, deadline_ms=45.0, sample_rate_ksps=20.0
    )
    assert level == "error"
    assert (
        ti_split_check(14, max_lead_ms=model.max_lead_ms, deadline_ms=45.0, sample_rate_ksps=20.0)
        is None
    )


# --- CLI ---------------------------------------------------------------------------------


def _args(**overrides) -> SimpleNamespace:
    values = {
        "shot_trigger": "sound",
        "ti_pre_trigger": None,
        "ti_fire_latency_ms": None,
        "ti_shadow_holdoff_ms": None,
        "iwr6843_self_trigger_offset_m": None,
        "iwr6843_self_trigger_snr": None,
        "iwr6843_self_trigger_bin": None,
        "iwr6843": True,
        "mock": False,
        "swing_speed": False,
        "trigger": "sound",
        "ops_trigger_pin": 27,
        "ops_trigger_mode": "passthrough",
        "ops_trigger_deadline_ms": 45.0,
        "sound_pre_trigger": 6,
        "sample_rate": 20,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _validate(args):
    return server_module._validate_shot_trigger_args(argparse.ArgumentParser(), args)


def test_parser_defines_the_shot_trigger_flags():
    parser = argparse.ArgumentParser()
    server_module._add_shot_trigger_arguments(parser)
    defaults = parser.parse_args([])
    assert defaults.shot_trigger == "sound"
    assert defaults.ti_pre_trigger is None and defaults.ti_shadow_holdoff_ms is None
    parsed = parser.parse_args(
        [
            "--shot-trigger",
            "ti",
            "--ti-pre-trigger",
            "10",
            "--ti-fire-latency-ms",
            "crossing=-1",
            "--iwr6843-self-trigger-offset-m",
            "0.1",
            "--iwr6843-self-trigger-snr",
            "2",
            "--iwr6843-self-trigger-bin",
            "35",
        ]
    )
    assert parsed.shot_trigger == "ti" and parsed.ti_pre_trigger == 10
    assert parsed.iwr6843_self_trigger_bin == 35
    with pytest.raises(SystemExit):
        parser.parse_args(["--shot-trigger", "laser"])


def test_sound_mode_is_unchanged_and_refuses_ti_tuning():
    args = _args()
    assert _validate(args) is None
    assert args.sound_pre_trigger == 6
    for flag in (
        {"ti_pre_trigger": 9},
        {"ti_fire_latency_ms": "end=5"},
        {"ti_shadow_holdoff_ms": 10.0},
        {"iwr6843_self_trigger_offset_m": 0.2},
        {"iwr6843_self_trigger_snr": 2.0},
        {"iwr6843_self_trigger_bin": 36},
    ):
        with pytest.raises(SystemExit):
            _validate(_args(**flag))


def test_ti_mode_switches_the_split_and_builds_the_anchor(capsys):
    args = _args(shot_trigger="ti", ti_fire_latency_ms="end=6")
    assert _validate(args) is None
    assert args.sound_pre_trigger == 9 and args.ti_pre_trigger == 9
    assert args.ti_anchor.fire_latency("end") == 6.0
    assert "S#9 (--ti-pre-trigger) replaces --sound-pre-trigger 6" in capsys.readouterr().out


def test_ti_mode_warns_about_a_short_split():
    args = _args(shot_trigger="ti", ti_pre_trigger=5)
    warning = _validate(args)
    assert "--ti-pre-trigger 6" in warning
    assert args.sound_pre_trigger == 5
    fallback = _args(
        shot_trigger="ti",
        ti_pre_trigger=6,
        iwr6843_self_trigger_offset_m=0.2,
        ti_fire_latency_ms="crossing=2.88,end=3.9,leave=1.19,unknown=2.91,worst=18.45",
    )
    assert "--ti-pre-trigger 8" in _validate(fallback)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iwr6843": False},
        {"ops_trigger_pin": None},
        {"mock": True},
        {"trigger": "speed"},
        {"ti_fire_latency_ms": "late=3"},
        {"ti_pre_trigger": 40},
        {"ops_trigger_mode": "camera"},  # S#9 cannot hold a 45 ms deadline plus the lead
        {"iwr6843_self_trigger_snr": 0.5},
        {"iwr6843_self_trigger_offset_m": 2.0},
        {"iwr6843_self_trigger_bin": -1},
        {"ti_shadow_holdoff_ms": 5.0},
    ],
)
def test_ti_mode_refuses_unworkable_options(overrides):
    with pytest.raises(SystemExit):
        _validate(_args(shot_trigger="ti", **overrides))


def test_ti_camera_mode_with_a_long_split_passes():
    args = _args(shot_trigger="ti", ops_trigger_mode="camera", ti_pre_trigger=14)
    assert _validate(args) is None


def test_ti_shadow_keeps_the_microphone_setup():
    args = _args(shot_trigger="ti-shadow", ops_trigger_pin=None, ti_shadow_holdoff_ms=20.0)
    assert _validate(args) is None
    assert args.sound_pre_trigger == 6 and args.ti_shadow_holdoff_ms == 20.0
    default = _args(shot_trigger="ti-shadow")
    assert _validate(default) is None and default.ti_shadow_holdoff_ms == 0.0
    for overrides in (
        {"ti_shadow_holdoff_ms": 41.0},
        {"ti_pre_trigger": 9},
        {"iwr6843": False},
    ):
        with pytest.raises(SystemExit):
            _validate(_args(shot_trigger="ti-shadow", **overrides))


def test_gate_validation_leaves_the_ti_camera_split_to_the_ti_check():
    args = SimpleNamespace(
        ops_trigger_pin=27,
        ops_trigger_mode="camera",
        ops_trigger_pulse_ms=20.0,
        ops_trigger_deadline_ms=45.0,
        iwr6843_trigger_pin=17,
        mock=False,
        swing_speed=False,
        trigger="sound",
        camera_capture=True,
        sound_pre_trigger=6,
        sample_rate=20,
        shot_trigger="ti",
    )
    assert server_module._validate_trigger_gate_args(argparse.ArgumentParser(), args) is None
    args.shot_trigger = "sound"
    with pytest.raises(SystemExit):
        server_module._validate_trigger_gate_args(argparse.ArgumentParser(), args)


# --- server wiring -----------------------------------------------------------------------


CFG = "phaseCaptureCfg 20 53 9 32 53 7 47 53 47 8 1\nchirpCfg 0 0 0 0 0 0 0 1\n"


def _init(monkeypatch, tmp_path, *, tee_range_m=1.50, **kwargs) -> dict:
    captured = {}

    class FakeCaptureMonitor:
        def __init__(self, **monitor_kwargs):
            captured.update(monitor_kwargs)
            self.port = "/dev/ttyUSB0"

        def start(self, *, armed=True):
            return None

    cfg = tmp_path / "radar.cfg"
    cfg.write_text(CFG, encoding="utf-8")
    monkeypatch.setattr(Calibration, "load", lambda _path: Calibration.identity())
    monkeypatch.setattr("openflight.iwr6843.monitor.IWR6843CaptureMonitor", FakeCaptureMonitor)
    monkeypatch.setattr("openflight.iwr6843.monitor.tx_order_from_config", lambda _path: "normal")
    ok = server_module.init_iwr6843(
        port="/dev/ttyUSB0",
        config_path=str(cfg),
        calibration_path="cal.json",
        output_dir=tmp_path,
        trigger_pin=17,
        tee_range_m=tee_range_m,
        net_range_m=4.6,
        tx_order="auto",
        capture_timeout_s=12.0,
        **kwargs,
    )
    captured["ok"] = ok
    captured["config"] = dict(server_module.iwr6843_runtime_config)
    server_module.iwr6843_runtime = None
    return captured


def test_init_iwr6843_sound_mode_sends_no_trigger_config(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    captured = _init(monkeypatch, tmp_path)
    assert captured["ok"]
    assert captured["shot_trigger"] == "sound" and captured["self_trigger"] is None
    assert captured["config"]["self_trigger"] is None
    assert captured["config"]["trigger_pin_bcm"] == 17


def test_init_iwr6843_ti_mode_wires_the_trigger_and_fire_observer(monkeypatch, tmp_path):
    gate = SimpleNamespace(on_edge=Mock())
    camera = SimpleNamespace(notify_trigger=Mock(), set_write_deferral=Mock())
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    monkeypatch.setattr(server_module, "camera_capture_runtime", camera)
    monkeypatch.setattr(server_module, "camera_capture_config", {"enabled": True})
    captured = _init(monkeypatch, tmp_path, shot_trigger="ti")
    assert captured["ok"]
    # The owner's --iwr6843-tee-m 1.50: ball bin 33 (+30 mm), lead2 = 31.
    assert captured["self_trigger"].command == "triggerCfg 31 1 1"
    assert captured["trigger_observers"] == [gate.on_edge, camera.notify_trigger]
    assert captured["fire_observers"] == [server_module._on_ti_fire]
    assert captured["shadow_matcher"] is None
    config = captured["config"]
    assert config["shot_trigger"] == "ti" and config["trigger_pin_bcm"] is None
    assert config["self_trigger"]["ball_bin"] == 33
    assert config["self_trigger"]["tee_bin"] == 31
    fallback = _init(monkeypatch, tmp_path, shot_trigger="ti", self_trigger_offset_m=0.2)
    assert fallback["self_trigger"].command == "triggerCfg 35 1 1"
    assert server_module.camera_capture_config["trigger_source"] == "iwr6843_self_trigger"


def test_init_iwr6843_shadow_mode_logs_pairings(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    logged = []
    monkeypatch.setattr(server_module, "_log_ti_self_trigger", logged.append)
    captured = _init(
        monkeypatch, tmp_path, shot_trigger="ti-shadow", shadow_holdoff_ms=15.0, self_trigger_bin=30
    )
    assert captured["ok"]
    assert captured["self_trigger"].tee_bin == 30
    assert captured["shadow_holdoff_s"] == pytest.approx(0.015)
    assert captured["fire_observers"] is None
    matcher = captured["shadow_matcher"]
    matcher.note_edge(1.0, 0, ti_state="listening")
    matcher.flush(everything=True)
    assert logged and logged[0]["mode"] == "ti-shadow"
    assert logged[0]["kind"] == "edge_without_notice"


def test_init_iwr6843_refuses_a_bin_outside_the_window(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    captured = _init(monkeypatch, tmp_path, shot_trigger="ti", self_trigger_bin=99)
    assert not captured["ok"]
    assert "outside the first capture window" in captured["config"]["error"]


def test_fire_observer_anchors_the_gate_and_logs(monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock, edge_source="ti", ti_anchor=TiAnchorModel(parse_fire_latency("")))
    gate.on_edge(EDGE_EPOCH)
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    logged = []
    monkeypatch.setattr(server_module, "_log_ti_self_trigger", logged.append)

    server_module._on_ti_fire({"kind": "fire", "edge_timestamp": EDGE_EPOCH, "cause": "crossing"})

    assert gate._records[-1].ti_cause == "crossing"  # pylint: disable=protected-access
    assert logged[0]["anchor_lead_ms"] == pytest.approx(gate.ti_anchor.lead_ms("crossing"))


# --- OPS speed trigger (B2): CLI ---------------------------------------------------------

SOUND_GATE_FIELDS = {
    "gate_mode",
    "gate_decision",
    "gate_reason",
    "sound_edge_delay_ms",
    "gate_pulse_delay_ms",
    "gate_edge_source",
    "ti_fire_cause",
    "ti_anchor_lead_ms",
    "gate_decision_latency_ms",
    "gate_frames_used",
    "gate_frame_states",
    "gate_edge_timestamp",
    "gate_first_byte_lag_ms",
    "gate_first_byte_match",
    "gate_shadow_decision",
    "gate_shadow_reason",
    "gate_shadow_first_departure_ms",
    "gate_shadow_frame_states",
}

OPS_SPEED_ARG_NAMES = (
    "ops_speed_pin",
    "ops_speed_pin_active",
    "ops_speed_pin_pull",
    "ops_speed_debounce_ms",
    "ops_speed_holdoff_ms",
    "ops_speed_threshold_mph",
    "ops_speed_magnitude",
    "ops_speed_pre_trigger",
    "ops_speed_ti_delay_ms",
    "ops_speed_anchor_ms",
    "ops_speed_witness_window_ms",
    "ops_speed_stall_mic_edges",
)


def _speed_args(**overrides) -> SimpleNamespace:
    """Namespace as the parser builds it, in ops-speed mode with the required flags."""
    values = {
        **vars(_args(iwr6843=False)),
        **dict.fromkeys(OPS_SPEED_ARG_NAMES),
        "ops_speed_reject_practice_swings": False,
        "shot_trigger": "ops-speed",
        "ops_speed_pin": 22,
        "ops_speed_magnitude": 40,
        "iwr6843_trigger_pin": 17,
        "port": None,
        "ops_baud": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _validate_speed(args):
    return server_module._validate_ops_speed_args(argparse.ArgumentParser(), args)


def test_parser_offers_the_speed_trigger_modes_and_flags():
    parser = argparse.ArgumentParser()
    server_module._add_shot_trigger_arguments(parser)
    server_module._add_ops_speed_arguments(parser)
    defaults = parser.parse_args([])
    assert defaults.shot_trigger == "sound"
    assert all(getattr(defaults, name) is None for name in OPS_SPEED_ARG_NAMES)
    assert defaults.ops_speed_reject_practice_swings is False
    parsed = parser.parse_args(
        [
            "--shot-trigger",
            "ops-speed",
            "--ops-speed-pin",
            "22",
            "--ops-speed-pin-active",
            "low",
            "--ops-speed-pin-pull",
            "off",
            "--ops-speed-magnitude",
            "40",
            "--ops-speed-ti-delay-ms",
            "20",
            "--ops-speed-witness-window-ms",
            "0,50",
            "--ops-speed-reject-practice-swings",
        ]
    )
    assert parsed.shot_trigger == "ops-speed" and parsed.ops_speed_pin == 22
    assert parsed.ops_speed_pin_active == "low" and parsed.ops_speed_pin_pull == "off"
    assert parsed.ops_speed_ti_delay_ms == 20.0 and parsed.ops_speed_reject_practice_swings
    assert parser.parse_args(["--shot-trigger", "ops-speed-shadow"]).shot_trigger == (
        "ops-speed-shadow"
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["--ops-speed-pin-active", "rising"])


@pytest.mark.parametrize(
    "flag",
    [
        {"ops_speed_pin": 22},
        {"ops_speed_magnitude": 40},
        {"ops_speed_debounce_ms": 0.0},  # also a zero counts as given
        {"ops_speed_stall_mic_edges": 0},
        {"ops_speed_witness_window_ms": "0,60"},
        {"ops_speed_reject_practice_swings": True},
    ],
)
@pytest.mark.parametrize("mode", ["sound", "ti", "ti-shadow"])
def test_ops_speed_flags_need_a_speed_trigger_mode(mode, flag):
    args = _speed_args(
        shot_trigger=mode, **{"ops_speed_pin": None, "ops_speed_magnitude": None, **flag}
    )
    with pytest.raises(SystemExit):
        _validate_speed(args)


def test_sound_mode_without_speed_flags_is_untouched():
    args = _speed_args(shot_trigger="sound", ops_speed_pin=None, ops_speed_magnitude=None)
    before = dict(vars(args))
    assert _validate_speed(args) is None
    assert args.ops_speed_config is None
    after = dict(vars(args))
    after.pop("ops_speed_config")
    assert after == before


def test_speed_mode_builds_the_config_and_replaces_the_split(capsys):
    args = _speed_args()
    assert _validate_speed(args) is None
    config = args.ops_speed_config
    assert config.mode == "ops-speed" and config.pin == 22 and config.magnitude == 40
    assert config.pre_trigger_segments == 2 and config.anchor_ms == 28.0
    assert config.ti_delay_ms == 25.0 and config.witness_window_ms == (0.0, 60.0)
    assert config.stall_mic_edges == 3 and not config.reject_practice_swings
    assert args.sound_pre_trigger == 2 and args.ops_speed_sound_pre_trigger == 6
    assert "S#2 (--ops-speed-pre-trigger) replaces --sound-pre-trigger 6" in capsys.readouterr().out
    # --iwr6843 is not needed (unlike ti / ti-shadow).
    assert server_module._validate_shot_trigger_args(argparse.ArgumentParser(), args) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"ops_trigger_pin": None},
        {"ops_trigger_mode": "camera"},
        {"mock": True},
        {"swing_speed": True},
        {"trigger": "speed"},
        {"ops_speed_pin": None},
        {"ops_speed_magnitude": None},
        {"ops_speed_pin": 17},  # the microphone input
        {"ops_speed_pin": 27},  # HOST_INT
        {"ops_speed_pin": 28},
        {"ops_speed_magnitude": 0},
        {"ops_speed_threshold_mph": 0},
        {"ops_speed_ti_delay_ms": 41.0},
        {"ops_speed_debounce_ms": 6.0},
        {"ops_speed_pin_active": "high", "ops_speed_pin_pull": "up"},
        {"ops_speed_witness_window_ms": "60,0"},
        {"ops_speed_pre_trigger": 0},  # S#0 keeps nothing before the trip
    ],
)
def test_speed_mode_refuses_unworkable_options(overrides):
    with pytest.raises(SystemExit):
        _validate_speed(_speed_args(**overrides))


@pytest.mark.parametrize(
    "tuning",
    [
        {"iwr6843_self_trigger_offset_m": 0.2},
        {"iwr6843_self_trigger_snr": 2.0},
        {"iwr6843_self_trigger_bin": 31},
        {"ti_pre_trigger": 9},
        {"ti_shadow_holdoff_ms": 5.0},
    ],
)
@pytest.mark.parametrize("mode", ["ops-speed", "ops-speed-shadow"])
def test_speed_modes_refuse_the_ti_self_trigger_tuning(mode, tuning):
    with pytest.raises(SystemExit):
        _validate(_speed_args(shot_trigger=mode, **tuning))


@pytest.mark.parametrize(
    "flag", [{"ops_speed_ti_delay_ms": 20.0}, {"ops_speed_stall_mic_edges": 2}]
)
def test_shadow_refuses_the_ops_speed_only_flags(flag):
    with pytest.raises(SystemExit):
        _validate_speed(_speed_args(shot_trigger="ops-speed-shadow", **flag))
    assert _validate_speed(_speed_args(**flag)) is None


def test_speed_split_check_levels():
    # S#2 at 20 ksps: 12.8 ms + the 28 ms anchor = 40.8 ms of club approach (31.4 needed).
    assert _validate_speed(_speed_args()) is None
    # A 10 ms anchor leaves 22.8 ms: enough for the club speed, short of its rise check.
    warning = _validate_speed(_speed_args(ops_speed_anchor_ms=10.0))
    assert "--ops-speed-pre-trigger 4" in warning
    # A 5 ms anchor leaves 17.8 ms: below the club-speed rule's 20.4 ms.
    with pytest.raises(SystemExit):
        _validate_speed(_speed_args(ops_speed_anchor_ms=5.0))


def test_shadow_warns_that_fallback_dumps_keep_little_club_history():
    warning = _validate_speed(_speed_args(shot_trigger="ops-speed-shadow"))
    assert "fallback dump" in warning and "12.8 ms before HOST_INT" in warning
    assert (
        _validate_speed(_speed_args(shot_trigger="ops-speed-shadow", ops_speed_pre_trigger=5))
        is None
    )


def test_gate_validation_leaves_the_speed_camera_refusal_to_the_speed_check():
    args = _speed_args(
        ops_trigger_mode="camera",
        ops_trigger_pulse_ms=20.0,
        camera_capture=True,
        sound_pre_trigger=6,
    )
    assert server_module._validate_trigger_gate_args(argparse.ArgumentParser(), args) is None
    with pytest.raises(SystemExit):
        _validate_speed(args)


# --- OPS speed trigger: the preflight (firmware gate, pin idle level) ---------------------


class PreflightRadar:
    """OPS243Radar double answering ``?V`` with ``version`` (or failing to connect)."""

    created: list = []

    def __init__(self, version, error=None):
        self.version = version
        self.error = error
        self.firmware_version = None
        self.kwargs = None
        self.connect_kwargs = None
        self.disconnected = False

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.error is not None:
            raise self.error
        self.firmware_version = self.version
        return True

    def disconnect(self):
        self.disconnected = True


@pytest.fixture(name="speed_args")
def _validated_speed_args(monkeypatch):
    monkeypatch.setattr(server_module, "ops_speed_runtime_config", {"enabled": False})
    args = _speed_args(shot_trigger="ops-speed-shadow")
    _validate_speed(args)
    return args


@pytest.fixture(name="off_command")
def _known_off_command(monkeypatch):
    monkeypatch.setattr("openflight.ops243.SPEED_TRIGGER_OFF_COMMANDS", ("ST0",))


@pytest.mark.parametrize(
    ("version", "fragment"),
    [
        ("1.3.0", "below 1.3.3"),
        ("1.3.2", "below 1.3.3"),
        ("1.4.0", "outside the 1.3 release train"),
        ("garbage", "not major.minor.patch"),
        (None, "version unknown"),
    ],
)
def test_preflight_refuses_other_firmware_and_falls_back_to_sound(
    speed_args, off_command, caplog, capsys, version, fragment
):
    radar = PreflightRadar(version)
    pin_check = Mock(return_value=None)
    with caplog.at_level("ERROR", logger="openflight.server"):
        reason = server_module._ops_speed_preflight(
            speed_args, radar_factory=radar, pin_check=pin_check
        )

    assert fragment in reason
    assert speed_args.shot_trigger == "sound" and speed_args.sound_pre_trigger == 6
    assert speed_args.ops_speed_config is None
    assert radar.connect_kwargs == {"probe_firmware": True} and radar.disconnected
    pin_check.assert_not_called()
    line = "speed trigger refused, staying on the microphone (--shot-trigger sound)"
    assert line in caplog.text and line in capsys.readouterr().out
    config = server_module._session_start_config()["ops_speed"]
    assert config["enabled"] is False and fragment in config["refused"]
    assert config["firmware"] == version and config["mode"] == "ops-speed-shadow"


def test_preflight_refuses_while_the_off_command_is_unknown(speed_args):
    reason = server_module._ops_speed_preflight(
        speed_args, radar_factory=PreflightRadar("1.3.3"), pin_check=Mock(return_value=None)
    )
    assert "off command is unknown" in reason and speed_args.shot_trigger == "sound"


def test_preflight_refuses_a_pin_active_at_idle(speed_args, off_command):
    reason = server_module._ops_speed_preflight(
        speed_args,
        radar_factory=PreflightRadar("1.3.3"),
        pin_check=Mock(return_value="BCM22 (J3 pin 1) reads active at idle"),
    )
    assert "active at idle" in reason and speed_args.shot_trigger == "sound"
    assert server_module._session_start_config()["ops_speed"]["firmware"] == "1.3.3"


def test_preflight_refuses_when_the_ops_does_not_answer(speed_args, off_command):
    radar = PreflightRadar(None, error=ConnectionError("no reply at any baud"))
    reason = server_module._ops_speed_preflight(speed_args, radar_factory=radar)
    assert "firmware check failed (no reply at any baud)" in reason
    assert radar.disconnected and speed_args.shot_trigger == "sound"


def test_preflight_passes_firmware_133_with_a_quiet_pin(speed_args, off_command, monkeypatch):
    radar = PreflightRadar("1.3.3")
    pin_check = Mock(return_value=None)
    speed_args.ops_baud = 230400
    assert (
        server_module._ops_speed_preflight(speed_args, radar_factory=radar, pin_check=pin_check)
        is None
    )
    assert radar.kwargs == {"port": None, "uart_baud": 230400}
    pin_check.assert_called_once_with(22, active="high", pull="down")
    assert speed_args.shot_trigger == "ops-speed-shadow" and speed_args.sound_pre_trigger == 2
    monkeypatch.setattr(server_module, "monitor", None)
    config = server_module._session_start_config()["ops_speed"]
    assert config["enabled"] is True and "refused" not in config
    assert config["pin_idle"] == "inactive" and config["flags"]["pin"] == 22


def test_preflight_does_nothing_without_a_speed_mode(monkeypatch):
    monkeypatch.setattr(server_module, "ops_speed_runtime_config", {"enabled": False})
    args = _speed_args(shot_trigger="sound", ops_speed_pin=None, ops_speed_magnitude=None)
    _validate_speed(args)
    factory = Mock()
    assert server_module._ops_speed_preflight(args, radar_factory=factory) is None
    factory.assert_not_called()
    assert "ops_speed" not in server_module._session_start_config()


# --- OPS speed trigger: server wiring --------------------------------------------------------


def _speed_config(mode="ops-speed", **kwargs):
    from openflight.ops_speed import OpsSpeedConfig

    return OpsSpeedConfig(mode=mode, pin=22, magnitude=40, **kwargs)


def test_init_iwr6843_ops_speed_listens_on_pin_1_with_the_l3dump_delay(monkeypatch, tmp_path):
    gate = SimpleNamespace(on_edge=Mock())
    camera = SimpleNamespace(notify_trigger=Mock(), set_write_deferral=Mock())
    witness = SimpleNamespace(note_pin_edge=Mock(), note_pin_release=Mock(), note_pin_glitch=Mock())
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    monkeypatch.setattr(server_module, "camera_capture_runtime", camera)
    monkeypatch.setattr(server_module, "camera_capture_config", {"enabled": True})
    monkeypatch.setattr(server_module, "ops_speed_witness", witness)
    captured = _init(monkeypatch, tmp_path, shot_trigger="ops-speed", ops_speed=_speed_config())

    assert captured["ok"]
    # The TI runs as in sound: no self-trigger config for the speed-trigger modes.
    assert captured["shot_trigger"] == "sound" and captured["self_trigger"] is None
    assert captured["shadow_matcher"] is None and captured["fire_observers"] is None
    assert captured["gpio_pin"] == 22 and callable(captured["button_factory"])
    assert captured["l3dump_delay_s"] == pytest.approx(0.025)
    assert captured["trigger_observers"] == [
        gate.on_edge,
        camera.notify_trigger,
        witness.note_pin_edge,
    ]
    config = captured["config"]
    assert config["shot_trigger"] == "ops-speed" and config["self_trigger"] is None
    assert config["trigger_pin_bcm"] == 22 and config["freeze_delay_ms"] == pytest.approx(25.0)
    assert server_module.camera_capture_config["trigger_source"] == "ops_speed_pin"


def test_init_iwr6843_ops_speed_shadow_keeps_the_microphone(monkeypatch, tmp_path):
    gate = SimpleNamespace(on_edge=Mock())
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    monkeypatch.setattr(server_module, "ops_speed_witness", SimpleNamespace())
    captured = _init(
        monkeypatch,
        tmp_path,
        shot_trigger="ops-speed-shadow",
        ops_speed=_speed_config("ops-speed-shadow"),
    )
    assert captured["ok"] and captured["shot_trigger"] == "sound"
    assert captured["self_trigger"] is None
    assert captured["gpio_pin"] == 17 and captured["button_factory"] is None
    assert captured["l3dump_delay_s"] == 0.0
    assert captured["trigger_observers"] == [gate.on_edge, server_module._ops_speed_mic_edge]
    assert captured["config"]["freeze_delay_ms"] == 0.0


def test_init_iwr6843_sound_mode_kwargs_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    captured = _init(monkeypatch, tmp_path)
    assert captured["gpio_pin"] == 17 and captured["button_factory"] is None
    assert captured["l3dump_delay_s"] == 0.0 and captured["config"]["freeze_delay_ms"] == 0.0
    assert captured["trigger_observers"] == [server_module._notify_ops_trigger_edge]


def _fake_pin_factory(monkeypatch):
    from openflight.ops_speed import pin_button_factory
    from tests.ops_speed_sim import FakePinFactory

    pins = FakePinFactory()
    monkeypatch.setattr(
        server_module,
        "_ops_speed_pin_factory",
        lambda config: pin_button_factory(config.pin, factory=pins),
    )
    return pins


@pytest.fixture(name="gate_server")
def _gate_server(monkeypatch):
    from openflight.trigger_gate import EdgeListener
    from tests.test_trigger_chain import FakeButton
    from tests.test_trigger_gate import FakeOutput

    created: dict = {}
    output = FakeOutput(FakeClock())

    class FakeGate(TriggerGate):
        def __init__(self, **kwargs):
            created.update(kwargs)
            super().__init__(output_factory=output.factory, background=False, **kwargs)

    class FakeListener(EdgeListener):
        def __init__(self, **kwargs):
            if kwargs.get("button_factory") is None:
                kwargs["button_factory"] = FakeButton
            super().__init__(**kwargs)

    monkeypatch.setattr("openflight.trigger_gate.TriggerGate", FakeGate)
    monkeypatch.setattr("openflight.trigger_gate.EdgeListener", FakeListener)
    for name, value in (
        ("camera_capture_runtime", None),
        ("trigger_gate", None),
        ("trigger_gate_listener", None),
        ("trigger_gate_config", {"enabled": False}),
        ("ops_speed_listener", None),
        ("ops_speed_witness", None),
        ("ops_speed_settings", None),
    ):
        monkeypatch.setattr(server_module, name, value)
    yield created, output
    for name in ("trigger_gate_listener", "ops_speed_listener", "trigger_gate"):
        item = getattr(server_module, name)
        if item is not None:
            item.stop()


def test_init_trigger_gate_ops_speed_without_ti_owns_pin_1(gate_server, monkeypatch):
    created, output = gate_server
    pins = _fake_pin_factory(monkeypatch)
    config = _speed_config()
    witness = server_module.init_ops_speed_witness(config)
    assert server_module.init_trigger_gate(
        pin=27,
        mode="passthrough",
        pulse_ms=20.0,
        deadline_ms=45.0,
        edge_pin=17,
        own_edge_pin=True,
        ops_speed=config,
        sample_rate_ksps=20.0,
    )
    gate, listener = server_module.trigger_gate, server_module.trigger_gate_listener
    assert created["edge_source"] == "ops-speed" and created["ops_speed_anchor_ms"] == 28.0
    assert created["ops_speed_shadow"] is False
    # S#2 at 20 ksps: the OPS is blind 192 ms (+ the UART lag) after a trip.
    assert created["ops_speed_post_trigger_ms"] == pytest.approx(192.0)
    assert listener.pin == 22 and listener._observers == [gate.on_edge, witness.note_pin_edge]
    assert pins.calls == [(22, {"pull_up": False, "bounce_time": None})]
    gate_config = server_module.trigger_gate_config
    assert gate_config["edge_pin_bcm"] == 22
    assert gate_config["ops_speed"] == {
        "mode": "ops-speed",
        "anchor_ms": 28.0,
        "window_ms": [0.0, 60.0],
        "post_trigger_ms": pytest.approx(192.0),
    }

    # The microphone becomes the witness's own input, never the OPS stall watch.
    assert server_module.init_ops_speed_listener(config, sound_pin=17)
    mic = server_module.ops_speed_listener
    assert mic.pin == 17 and mic._observers == [server_module._ops_speed_mic_edge]

    listener.arm()
    pins.button.press()
    assert output.pulses == []
    assert gate._records[-1].decision == "ops_speed"


def test_init_ops_speed_listener_shadow_owns_pin_1(gate_server, monkeypatch):
    _fake_pin_factory(monkeypatch)
    config = _speed_config("ops-speed-shadow")
    witness = server_module.init_ops_speed_witness(config)
    camera = SimpleNamespace(notify_trigger=Mock())
    monkeypatch.setattr(server_module, "camera_capture_runtime", camera)
    assert server_module.init_trigger_gate(
        pin=27,
        mode="passthrough",
        pulse_ms=20.0,
        deadline_ms=45.0,
        edge_pin=17,
        own_edge_pin=True,
        ops_speed=config,
    )
    gate = server_module.trigger_gate
    assert server_module.trigger_gate_listener.pin == 17
    assert server_module.trigger_gate_listener._observers == [
        gate.on_edge,
        camera.notify_trigger,
        server_module._ops_speed_mic_edge,
    ]
    assert server_module.init_ops_speed_listener(config, sound_pin=17)
    pin = server_module.ops_speed_listener
    assert pin.pin == 22
    assert pin._observers == [server_module._ops_speed_shadow_pin_edge, camera.notify_trigger]
    # The shadow witness has no stall rule: HOST_INT fallback pulses feed the stall watch.
    assert witness._stall_mic_edges == 0


def test_shadow_pin_edge_right_after_a_fallback_pulse_is_its_own_dump(gate_server, monkeypatch):
    clock = FakeClock()
    gate, output = make_gate(clock, ops_speed_shadow=True)
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    witness = SimpleNamespace(note_pin_edge=Mock())
    monkeypatch.setattr(server_module, "ops_speed_witness", witness)

    gate.on_edge(EDGE_EPOCH)  # a microphone edge without a trip: fallback pulse
    pulse = gate._records[-1].pulse_timestamp
    server_module._ops_speed_shadow_pin_edge(pulse + 0.001)  # pin 1 rises with that dump

    record = gate._records[-1]
    assert len(gate._records) == 1 and output.pulses
    assert record.ops_speed_echo_ms == pytest.approx(1.0)
    assert record.log_fields()["ops_speed_host_int_echo_ms"] == pytest.approx(1.0)
    witness.note_pin_edge.assert_not_called()
    assert gate.summary()["ops_speed"]["host_int_echoes"] == 1
    # Its dump still counts as the microphone's fallback dump.
    first_byte = pulse + (102.4 + 17.0) / 1000.0
    assert gate.classify_ops_speed_dump(first_byte, 102.4) == "fallback"

    # A pin edge later than the echo window is a speed trip of its own.
    server_module._ops_speed_shadow_pin_edge(pulse + 0.5)
    assert gate._records[-1].decision == "ops_speed"
    witness.note_pin_edge.assert_called_once_with(pulse + 0.5)


def test_init_ops_speed_listener_failure_is_reported(gate_server, monkeypatch, caplog):
    from openflight.ops_speed import OpsSpeedPinError

    def broken(_config):
        def factory(_pin, **_kwargs):
            raise OpsSpeedPinError("BCM22 (J3 pin 1) reads active at idle")

        return factory

    monkeypatch.setattr(server_module, "_ops_speed_pin_factory", broken)
    monkeypatch.setattr(server_module, "log_session_error", Mock())
    config = _speed_config("ops-speed-shadow")
    server_module.init_ops_speed_witness(config)
    assert not server_module.init_ops_speed_listener(config, sound_pin=17)
    assert server_module.ops_speed_listener is None
    assert "reads active at idle" in caplog.text


def test_witness_microphone_edge_carries_the_shadow_fallback(gate_server, monkeypatch):
    clock = FakeClock()
    gate, output = make_gate(clock, ops_speed_shadow=True, ops_speed_post_trigger_ms=192.0)
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    witness = SimpleNamespace(note_mic_edge=Mock())
    monkeypatch.setattr(server_module, "ops_speed_witness", witness)
    gate.on_edge(EDGE_EPOCH)
    server_module._ops_speed_mic_edge(EDGE_EPOCH)
    # The edge's own fallback pulse (sent just before) does not make the OPS busy at the edge.
    witness.note_mic_edge.assert_called_once_with(
        EDGE_EPOCH, ops_state="armed", fallback_pulse=True
    )
    assert output.pulses and gate.ops_busy_now()


def test_witness_microphone_edge_after_a_pulse_sees_the_ops_blind(gate_server, monkeypatch):
    clock = FakeClock()
    gate, _ = make_gate(clock, ops_speed_shadow=True, ops_speed_post_trigger_ms=192.0)
    monkeypatch.setattr(server_module, "trigger_gate", gate)
    witness = SimpleNamespace(note_mic_edge=Mock())
    monkeypatch.setattr(server_module, "ops_speed_witness", witness)
    gate.on_edge(EDGE_EPOCH)  # a fallback pulse: the OPS dumps from here on
    clock.at_ms(150.0)
    gate.on_edge(EDGE_EPOCH + 0.150)  # a neighbour 150 ms later, before the first byte
    server_module._ops_speed_mic_edge(EDGE_EPOCH + 0.150)
    witness.note_mic_edge.assert_called_once_with(
        EDGE_EPOCH + 0.150, ops_state="busy", fallback_pulse=True
    )


def test_stall_callback_requests_one_rearm_and_logs_it(monkeypatch):
    trigger = SimpleNamespace(request_rearm=Mock())
    monkeypatch.setattr(server_module, "monitor", SimpleNamespace(trigger=trigger))
    logged = []
    monkeypatch.setattr(server_module, "_log_ops_speed_trigger", logged.append)
    server_module._on_ops_speed_stall({"kind": "stall", "mic_edges": 3})
    trigger.request_rearm.assert_called_once_with()
    assert logged == [{"kind": "stall_rearm", "mic_edges": 3, "rearm_requested": True}]


# --- the default path is unchanged ----------------------------------------------------------


def test_sound_gate_records_and_summary_keep_their_fields():
    clock = FakeClock()
    gate, _ = make_gate(clock)
    gate.on_edge(EDGE_EPOCH)
    gate.run_pending()
    record = gate._records[-1]  # pylint: disable=protected-access
    assert set(record.log_fields()) == SOUND_GATE_FIELDS
    assert set(gate.summary()) == {
        "mode",
        "edges",
        "pulsed",
        "blocked",
        "late",
        "decisions",
        "unclear_reasons",
        "ops_busy",
        "ops_busy_phases",
        "shadow",
    }
    assert "OPS speed" not in gate.summary_text()
    unmatched = gate.fields_for_dump(EDGE_EPOCH + 9.0, 102.4)
    assert unmatched["sound_edge_delay_ms"] == 0.0 and "gate_ops_speed_mode" not in unmatched
    assert gate.ops_speed_fallback_for(EDGE_EPOCH) is None


def test_sound_trigger_wait_and_connect_are_unchanged(caplog):
    from openflight.rolling_buffer import RollingBufferMonitor
    from openflight.rolling_buffer.trigger import SoundTrigger

    radar = Mock()
    radar.wait_for_hardware_trigger.return_value = ""
    radar.last_hardware_trigger_lost_capture_bytes = 0
    trigger = SoundTrigger(pre_trigger_segments=6)
    with caplog.at_level("INFO", logger="openflight.rolling_buffer.trigger"):
        assert trigger.wait_for_trigger(radar, Mock(), timeout=1.0) is None
    assert set(radar.wait_for_hardware_trigger.call_args.kwargs) == {
        "timeout",
        "cancel_event",
        "on_first_byte",
    }
    assert "Waiting for sound trigger (timeout=1s)" in caplog.text
    assert "Sound trigger timeout" in caplog.text

    monitor = RollingBufferMonitor(port=None, pre_trigger_segments=6, sample_rate_ksps=20)
    monitor.radar = Mock(fast_rearm=False)
    monitor.connect()
    monitor.radar.connect.assert_called_once_with()
    monitor.radar.prepare_persisted_rolling_buffer.assert_called_once_with(
        pre_trigger_segments=6, sample_rate_ksps=20
    )
    monitor.radar.prepare_internal_speed_trigger.assert_not_called()
    assert monitor._ops_speed_fields({"gate_ops_speed_edge_timestamp": 1.0}) == {}


def test_session_start_config_has_no_speed_trigger_entry_by_default(monkeypatch):
    monkeypatch.setattr(server_module, "ops_speed_runtime_config", {"enabled": False})
    assert "ops_speed" not in server_module._session_start_config()


def test_session_start_config_records_the_speed_trigger_setup(monkeypatch):
    from openflight.ops243 import OPS243Radar

    radar = OPS243Radar()
    radar.last_speed_trigger_setup = {"settings_reply": '{"SpeedTrigger":"-25"}'}
    monkeypatch.setattr(server_module, "monitor", SimpleNamespace(radar=radar))
    monkeypatch.setattr(
        server_module,
        "ops_speed_runtime_config",
        {"requested": True, "enabled": True, "mode": "ops-speed", "firmware": "1.3.3"},
    )
    config = server_module._session_start_config()["ops_speed"]
    assert config == {
        "enabled": True,
        "mode": "ops-speed",
        "firmware": "1.3.3",
        "speed_trigger_setup": {"settings_reply": '{"SpeedTrigger":"-25"}'},
    }


def test_session_end_logs_the_witness_summary(monkeypatch):
    config = _speed_config()
    logged = []
    session = SimpleNamespace(
        end_session=Mock(),
        log_ops_speed_trigger=logged.append,
    )
    monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
    monkeypatch.setattr(server_module, "_fire_cloud_push", lambda _session: None)
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "monitor", None)
    monkeypatch.setattr(server_module, "ops_speed_witness", None)
    monkeypatch.setattr(server_module, "ops_speed_settings", None)
    witness = server_module.init_ops_speed_witness(config)
    witness.note_mic_edge(EDGE_EPOCH, ops_state="armed")
    witness.flush(everything=True)
    server_module.stop_monitor()
    summary = logged[-1]
    assert summary["kind"] == "summary" and summary["mode"] == "ops-speed"
    assert summary["mic_without_pin"] == 1 and summary["mic_without_pin_armed"] == 1
    assert summary["gate"] is None and summary["stall_rearms_requested"] is None
    session.end_session.assert_called_once_with()
