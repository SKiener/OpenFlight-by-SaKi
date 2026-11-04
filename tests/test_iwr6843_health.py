"""Tests for TI CLI reply classification and capture-health checks."""

from __future__ import annotations

import pytest

from openflight.iwr6843.health import (
    BANNER,
    DONE,
    ECHO_ONLY,
    ERROR,
    HWA_STALL,
    HWA_TIMEOUT,
    NOT_RECOGNIZED,
    OK,
    OTHER,
    RF_STOPPED,
    SILENT,
    SILENT_CLI,
    CliReply,
    TiWedgedError,
    assess_capture,
    capture_warning,
    classify_reply,
    is_class_a_dump_error,
    parse_stats,
)

STATS_BEFORE = (
    "stats\r\nframes=1200 wraps=3 active=1 calib=0x1ffe rf_faults=0 hwa_frames=1200 "
    "hwa_out=1200 hwa_rearms=1200 hwa_rearm_err=0 hwa_missed=0 freeze_req=0 "
    "freeze_done=0 freeze_to=0 format=iq8 plan=48pre/16post loops=12 used=786432/786432\r\n"
    "iq8_packed=1200 iq8_overrun=0 iq8_clipped=0 pending=0 pre_seen=48 post_kept=0 "
    "post_seen=0 stride=1\r\nDone\r\nl3dump:/>"
)


def _stats(frames: int, hwa_out: int) -> str:
    return STATS_BEFORE.replace("frames=1200", f"frames={frames}", 1).replace(
        "hwa_out=1200", f"hwa_out={hwa_out}"
    )


@pytest.mark.parametrize(
    ("command", "raw", "kind"),
    [
        ("sensorStop", "\r\nOpenFlight hybrid-cadence L3 firmware (v6)\r\nl3dump:/>", BANNER),
        (
            "sensorStop",
            "ssensorStop\r\n'ssensorStop' is not recognized as a CLI command\r\nl3dump:/>",
            NOT_RECOGNIZED,
        ),
        (
            "sensorStop",
            "sensorStop\r\nError: HWA shutdown boundary timed out\r\nError -1\r\nl3dump:/>",
            ERROR,
        ),
        ("flushCfg", "flushCfg\r\nDone\r\nl3dump:/>", DONE),
        ("sensorStart", "sensorStart\r\n", ECHO_ONLY),
        ("sensorStart", "sensorStart\r\nl3dump:/>", ECHO_ONLY),
        ("sensorStop", "", SILENT),
        ("sensorStop", "\r\nl3dump:/>", SILENT),
        ("sensorStop", "sensorSt\x00garbage", OTHER),
    ],
)
def test_classify_reply_kinds(command, raw, kind):
    assert classify_reply(command, raw) == kind


def test_classify_reply_precedence_puts_banner_and_errors_first():
    banner_after_done = "flushCfg\r\nDone\r\nOpenFlight hybrid-cadence L3 firmware (v6)\r\n"
    assert classify_reply("flushCfg", banner_after_done) == BANNER
    assert classify_reply("x", "Error: bad\r\nDone") == ERROR


def test_cli_reply_marks_hwa_timeout_detail():
    raw = "sensorStop\r\nError: HWA shutdown boundary timed out\r\nError -1\r\n"
    reply = CliReply("sensorStop", raw, 0.3, classify_reply("sensorStop", raw))
    other = CliReply("flushCfg", "Error: geometry\r\n", 0.1, ERROR)

    assert reply.detail == HWA_TIMEOUT
    assert other.detail is None
    assert not reply.ok


def test_parse_stats_reads_integers_and_hex_and_skips_compound_values():
    stats = parse_stats(STATS_BEFORE)

    assert stats["frames"] == 1200
    assert stats["active"] == 1
    assert stats["calib"] == 0x1FFE
    assert stats["iq8_packed"] == 1200
    assert "plan" not in stats
    assert "used" not in stats
    assert "format" not in stats


@pytest.mark.parametrize(
    ("after", "verdict", "warning"),
    [
        (_stats(1283, 1283), OK, None),
        (_stats(1283, 1250), OK, None),
        (_stats(1283, 1230), OK, "only 30 new HWA outputs for 83 new RF frames"),
        (_stats(1205, 1205), OK, "only 5 new RF frames"),
        (_stats(1201, 1201), OK, "only 1 new RF frames"),
        (_stats(1283, 1200), HWA_STALL, "only 0 new HWA outputs for 83 new RF frames"),
        (_stats(1200, 1200), RF_STOPPED, "only 0 new RF frames"),
        (_stats(1200, 1283), RF_STOPPED, "only 0 new RF frames"),
    ],
)
def test_assess_capture_fails_only_on_frozen_counters(after, verdict, warning):
    before = parse_stats(STATS_BEFORE)

    assert assess_capture(before, parse_stats(after)) == verdict
    assert capture_warning(before, parse_stats(after)) == warning


def test_assess_capture_without_hwa_counter_judges_frames_only():
    before = parse_stats("frames=10 wraps=0 active=1 calib=0x0 rf_faults=0")
    slow = parse_stats("frames=12 wraps=0 active=1 calib=0x0 rf_faults=0")
    frozen = parse_stats("frames=10 wraps=0 active=1 calib=0x0 rf_faults=0")
    healthy = parse_stats("frames=90 wraps=0 active=1 calib=0x0 rf_faults=0")

    assert assess_capture(before, healthy) == OK
    assert capture_warning(before, healthy) is None
    assert assess_capture(before, slow) == OK
    assert capture_warning(before, slow) == "only 2 new RF frames"
    assert assess_capture(before, frozen) == RF_STOPPED


def test_ti_wedged_error_keeps_legacy_markers():
    raw = "sensorStop\r\nError: HWA shutdown boundary timed out\r\nError -1"
    reply = CliReply("sensorStop", raw, 0.3, ERROR)

    stall = TiWedgedError(HWA_STALL, "sensorStop", reply)
    silent = TiWedgedError(SILENT_CLI, "flushCfg", "")

    assert isinstance(stall, RuntimeError)
    assert "did not acknowledge 'sensorStop'" in str(stall)
    assert "the firmware may be wedged (press RESET and retry)" in str(stall)
    assert "HWA shutdown boundary timed out" in str(stall)
    assert "[TI wedge: HWA chain stalled]" in str(stall)
    assert "[TI wedge: no reply to commands]" in str(silent)
    assert "reply:" not in str(silent)


@pytest.mark.parametrize(
    ("error", "class_a"),
    [
        ("bad magic b'l3du' (expected b'ILD1')", True),
        ("IWR6843 dump refused before ILD1: Error: HWA post-trigger frame freeze timed out", True),
        ("short IWR6843 dump: 700000 bytes, expected 786500", False),
        ("OPS rejected acoustic trigger; TI re-armed", False),
        (None, False),
    ],
)
def test_class_a_dump_error_detection(error, class_a):
    assert is_class_a_dump_error(error) is class_a
