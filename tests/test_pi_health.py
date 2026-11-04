"""CPU temperature and throttle flags ride along in every power_status log entry.

Range 2026-11-02: neither was logged anywhere and the Pi's journal does not
persist, so a hot or under-powered Pi could not be told apart afterwards.
"""

import subprocess

import pytest

from openflight import pi_health
from openflight.pi_health import parse_throttled, read_cpu_temp_c, read_throttled


def test_cpu_temperature_is_read_from_millidegrees(tmp_path):
    path = tmp_path / "temp"
    path.write_text("61324\n", encoding="ascii")

    assert read_cpu_temp_c(path) == 61.3


@pytest.mark.parametrize("content", [None, "", "warm\n"])
def test_a_missing_or_malformed_thermal_file_gives_none(tmp_path, content):
    path = tmp_path / "temp"
    if content is not None:
        path.write_text(content, encoding="ascii")

    assert read_cpu_temp_c(path) is None


def test_the_default_thermal_zone_is_zero():
    # thermal_zone1 is disabled on this Pi (dmesg).
    assert str(pi_health.CPU_TEMP_PATH) == "/sys/class/thermal/thermal_zone0/temp"


def test_no_throttling_decodes_to_all_false():
    assert parse_throttled("throttled=0x0\n") == {
        "raw": "0x0",
        "under_voltage_now": False,
        "freq_capped_now": False,
        "throttled_now": False,
        "under_voltage_ever": False,
        "freq_capped_ever": False,
        "throttled_ever": False,
    }


def test_throttle_bits_decode_now_and_ever():
    # 0x50005: under-voltage and throttled now, both also since boot.
    flags = parse_throttled("throttled=0x50005")

    assert flags["raw"] == "0x50005"
    assert flags["under_voltage_now"] and flags["throttled_now"]
    assert flags["under_voltage_ever"] and flags["throttled_ever"]
    assert not flags["freq_capped_now"] and not flags["freq_capped_ever"]


def test_past_only_flags_stay_in_the_ever_fields():
    flags = parse_throttled("throttled=0x20000")

    assert flags["freq_capped_ever"] is True
    assert flags["freq_capped_now"] is False


@pytest.mark.parametrize("output", ["", "throttled", "throttled=zz", "error"])
def test_malformed_vcgencmd_output_gives_none(output):
    assert parse_throttled(output) is None


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("vcgencmd"),
        PermissionError("vcgencmd"),
        subprocess.TimeoutExpired("vcgencmd", 1.0),
        subprocess.CalledProcessError(255, "vcgencmd"),
    ],
)
def test_a_missing_or_failing_vcgencmd_gives_none(error):
    def run():
        raise error

    assert read_throttled(run) is None


def test_vcgencmd_is_called_with_a_timeout(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="throttled=0x0\n", stderr="")

    monkeypatch.setattr(pi_health.subprocess, "run", fake_run)

    assert read_throttled()["raw"] == "0x0"
    ((args, kwargs),) = calls
    assert args == ("vcgencmd", "get_throttled")
    assert kwargs["timeout"] == pi_health.VCGENCMD_TIMEOUT_S
    assert kwargs["check"] is True


def test_pi_health_combines_both_reads(tmp_path):
    path = tmp_path / "temp"
    path.write_text("72000", encoding="ascii")

    health = pi_health.pi_health(temp_path=path, run_vcgencmd=lambda: "throttled=0x4")

    assert health["cpu_temp_c"] == 72.0
    assert health["throttle"]["throttled_now"] is True


def test_pi_health_never_raises(tmp_path, monkeypatch):
    def broken(_path):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(pi_health, "read_cpu_temp_c", broken)

    assert pi_health.pi_health(temp_path=tmp_path / "t", run_vcgencmd=lambda: "x") == {
        "cpu_temp_c": None,
        "throttle": None,
    }


def test_pi_health_off_a_pi_is_all_none(tmp_path):
    def missing():
        raise FileNotFoundError("vcgencmd")

    assert pi_health.pi_health(temp_path=tmp_path / "none", run_vcgencmd=missing) == {
        "cpu_temp_c": None,
        "throttle": None,
    }
