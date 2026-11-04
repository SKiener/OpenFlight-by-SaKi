"""The M1 bench script for the OPS 1.3.3 speed trigger, against the simulated board."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from openflight.ops243 import OPS243Radar
from tests.ops_speed_sim import (
    FakePinFactory,
    SimulatedOps133,
    fast_replies,
    fixture_dump,
    sim_radar,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hardware-test" / "test_ops_speed_pin.py"
spec = importlib.util.spec_from_file_location("bench_ops_speed_pin", SCRIPT)
bench = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = bench
spec.loader.exec_module(bench)

TRIP = b'{"trigger_time":"946.150"}\r\n'
OPEN_RECORDER = bench.open_recorder


@pytest.mark.parametrize(
    ("segments", "ksps", "expected"), [(2, 20, 192.0), (2, 30, 128.0), (6, 20, 166.4)]
)
def test_expected_high_width_is_the_post_trigger_span(segments, ksps, expected):
    assert bench.expected_high_ms(segments, ksps) == pytest.approx(expected)


def test_magnitude_is_required_and_flash_is_opt_in():
    parser = bench.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    args = parser.parse_args(["--magnitude", "20"])
    assert args.save_to_flash is False
    assert (args.pin, args.pre_trigger, args.sample_rate_ksps) == (22, 2, 20)


def test_pin_active_at_idle_stops_the_script():
    with pytest.raises(SystemExit, match="active at idle"):
        bench.open_recorder(22, "high", None, factory=FakePinFactory(idle_active=True))


def _bench_board(monkeypatch, **sim_kwargs):
    fast_replies(monkeypatch)
    factory = FakePinFactory()
    recorder = bench.open_recorder(22, "high", None, factory=factory)
    sim = SimulatedOps133(pin=factory.button, dump=fixture_dump(), **sim_kwargs)
    radar = sim_radar(sim, firmware_version="1.3.3")
    radar.prepare_internal_speed_trigger(
        threshold_mph=5, magnitude=20, pre_trigger_segments=2, sample_rate_ksps=20
    )
    return sim, radar, recorder


def test_one_try_measures_pin_dump_and_trip_message(monkeypatch):
    sim, radar, recorder = _bench_board(monkeypatch, trip_message=TRIP)
    processor = bench.RollingBufferProcessor(sample_rate=20000)

    record = bench.run_try(
        radar,
        recorder,
        processor,
        index=1,
        pre_trigger_segments=2,
        threshold_mph=5,
        timeout_s=1.0,
        on_armed=lambda: sim.fire(30.0),
    )

    assert record["dumped"] and record["rearm_ok"]
    assert record["edges"] == 2 and record["high_ms"] is not None
    assert record["trip_message"] == TRIP.decode()
    assert record["rise_to_first_byte_ms"] is not None
    assert record["parsed"] is True
    assert "SpeedTrigger" in record["settings_after_dump"]
    assert "A!" not in sim.commands and "GC" not in sim.commands


def test_a_try_without_a_trip_times_out_cleanly(monkeypatch):
    _sim, radar, recorder = _bench_board(monkeypatch)
    record = bench.run_try(
        radar,
        recorder,
        bench.RollingBufferProcessor(sample_rate=20000),
        index=1,
        pre_trigger_segments=2,
        threshold_mph=5,
        timeout_s=0.05,
        on_armed=lambda: None,
    )
    assert record["dumped"] is False and record["rise_wall"] is None


def test_host_int_test_reports_whether_pin_1_toggled(monkeypatch):
    sim, radar, recorder = _bench_board(monkeypatch, pin_on_host_int=False)
    result = bench.host_int_test(radar, recorder, pre_trigger_segments=2, pulse=sim.host_int)
    assert result["dumped"] is True
    assert result["pin_toggled"] is False


def test_summary_counts_and_compares_the_width():
    tries = [
        {
            "dumped": True,
            "rise_wall": 1.0,
            "high_ms": 191.0,
            "rise_to_first_byte_ms": 205.0,
            "trip_message": "",
            "settings_after_dump": "A",
            "trip_shift_ms": 1.0,
            "rearm_s": 0.5,
        },
        {
            "dumped": True,
            "rise_wall": 2.0,
            "high_ms": 193.0,
            "rise_to_first_byte_ms": 207.0,
            "trip_message": "T",
            "settings_after_dump": "B",
            "trip_shift_ms": 3.0,
            "rearm_s": 0.6,
        },
        {
            "dumped": False,
            "rise_wall": 3.0,
            "high_ms": None,
            "rise_to_first_byte_ms": None,
            "trip_message": "",
            "rearm_s": 0.5,
        },
    ]
    summary = bench.summarize(tries, expected_high=192.0)
    assert summary["dumps"] == 2 and summary["dumps_with_pin_rise"] == 2
    assert summary["rises_without_dump"] == 1
    assert summary["high_ms"]["median"] == 192.0
    assert summary["high_minus_expected_ms"] == 0.0
    assert summary["trip_messages"] == ["T"]
    assert summary["settings_after_dump"] == ["A", "B"]


def test_restore_without_an_off_command_says_power_cycle(monkeypatch):
    _sim, radar, _recorder = _bench_board(monkeypatch)
    assert "Power-cycle" in bench.restore(radar, saved=False)
    assert "SAVED" in bench.restore(radar, saved=True)


def _run_main(monkeypatch, tmp_path, *extra, answer="no"):
    fast_replies(monkeypatch)
    factory = FakePinFactory()
    monkeypatch.setattr(
        bench,
        "open_recorder",
        lambda pin, active, pull: OPEN_RECORDER(pin, active, pull, factory=factory),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    sim = SimulatedOps133(dump=fixture_dump(), settings_survive_dump=True)
    done = threading.Event()

    def swing():
        while not done.is_set():
            if sim.pin is None and factory.buttons:
                sim.pin = factory.button  # main claimed the pin
            if sim.pin is not None and sim.armed:
                sim.fire(30.0)
            done.wait(0.005)  # time.sleep is patched out by fast_replies

    worker = threading.Thread(target=swing, daemon=True)
    worker.start()
    try:
        with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
            code = bench.main(
                [
                    "--magnitude",
                    "20",
                    "--tries",
                    "2",
                    "--timeout",
                    "2",
                    "--out",
                    str(tmp_path),
                    *extra,
                ]
            )
    finally:
        done.set()
        worker.join(1.0)
    results = json.loads(next(tmp_path.glob("ops_speed_pin_*.json")).read_text())
    return code, sim, results


def test_main_runs_the_tries_and_never_saves_by_default(monkeypatch, tmp_path):
    code, sim, results = _run_main(monkeypatch, tmp_path)
    assert code == 0
    assert results["firmware"] == "1.3.3"
    assert results["summary"]["dumps"] == 2
    assert results["saved_to_flash"] is False
    assert "Power-cycle" in results["restore"]
    assert "A!" not in sim.commands
    assert "GC" not in sim.commands and "GS" not in sim.commands


def test_main_sends_a_bang_only_after_the_flag_and_the_confirmation(monkeypatch, tmp_path):
    _code, sim, results = _run_main(monkeypatch, tmp_path, "--save-to-flash", answer="no")
    assert "A!" not in sim.commands and results["saved_to_flash"] is False

    _code, sim, results = _run_main(
        monkeypatch, tmp_path / "saved", "--save-to-flash", answer="SAVE"
    )
    assert "A!" in sim.commands and results["saved_to_flash"] is True


def test_main_refuses_old_firmware(monkeypatch, tmp_path):
    fast_replies(monkeypatch)
    monkeypatch.setattr(
        bench,
        "open_recorder",
        lambda pin, active, pull: bench.EdgeRecorder(FakePinFactory()(pin)),
    )
    sim = SimulatedOps133(version="1.3.0")
    with patch("openflight.ops243.serial.Serial", side_effect=sim.open_as_serial):
        code = bench.main(["--magnitude", "20", "--out", str(tmp_path)])
    assert code == 2
    assert not any(command.startswith(("ST", "SM")) for command in sim.commands)
    results = json.loads(next(tmp_path.glob("ops_speed_pin_*.json")).read_text())
    assert results["firmware"] == "1.3.0" and "restore" not in results


def test_upstream_settings_are_the_post_gc_restore(monkeypatch):
    sim, radar, _recorder = _bench_board(monkeypatch)
    sent = bench.apply_upstream_settings(radar, 5)
    assert sent == ["S(", "X=2", "R-", "R>5"]
    assert sim.commands[-4:] == sent
    assert isinstance(radar, OPS243Radar)
