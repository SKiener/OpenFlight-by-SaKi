"""IWR6843 self-trigger: notice parsing on the UART, geometry, anchor math, shadow pairing."""

from __future__ import annotations

import numpy as np
import pytest
from iwr6843_scripted_cli import PROMPT, done, scripted_radar

from openflight.iwr6843.dump import pack_dump
from openflight.iwr6843.health import SILENT_CLI, TiWedgedError
from openflight.iwr6843.self_trigger import (
    DEFAULT_FIRE_LATENCY_MS,
    DEFAULT_SELF_TRIGGER_OFFSET_M,
    NOTICE_LATENCY_MS,
    OFFSET_02_FIRE_LATENCY_MS,
    SOUND_EDGE_AFTER_IMPACT_MS,
    UPSTREAM_ARRAY_DEPTH_M,
    SelfTriggerConfig,
    TiAnchorModel,
    TiShadowMatcher,
    TriggerNotice,
    parse_fire_latency,
    parse_trigger_cause,
    self_trigger_bin,
    trigger_ball_bin,
)

CFG = (
    "profileCfg 0 60.0 7 3 38 0 0 100 1 128 4000 0 0 30\n"
    "frameCfg 0 2 12 0 3 1 0\n"
    "phaseCaptureCfg 20 53 9 32 53 7 47 53 47 8 1\n"
)
TRACK_REPLY = (
    "track active=0 points=7 speed=31.2\n"
    "delivery ok=1\n"
    " angle n=3 estimates=4\n"
    "range impact fired={fired} why=fired cause={cause} offsetms=1.25 t=123 pending=0 passed=0 "
    "fired_n=1 armed=1\n"
    "leave fired={leave} why=idle start=0 speed=0 t=0 fired_n=0\n"
    "impactfit ok=1\n"
)


def _cfg(tmp_path):
    path = tmp_path / "radar.cfg"
    path.write_text(CFG, encoding="utf-8")
    return path


def _stamped(radar, clock):
    """Stamp notices on the scripted CLI's fake clock."""
    radar._notice_monotonic_ns = lambda: round(clock.now * 1e9)  # pylint: disable=protected-access
    radar._notice_wall_time = lambda: 1_700_000_000.0 + clock.now  # pylint: disable=protected-access


# --- driver: notices on the UART ---------------------------------------------


def test_idle_notice_is_seen_and_stamped():
    radar, serial, clock = scripted_radar()
    _stamped(radar, clock)
    serial.queue(b"Triggered\n", delay_s=0.05)

    assert radar.poll_trigger_notice(block=False) == ()
    notices = radar.poll_trigger_notice(block=True)

    assert len(notices) == 1
    assert notices[0].monotonic_ns == round(100.05 * 1e9)
    assert not notices[0].in_reply and not notices[0].consumed
    assert radar.take_notices() == ()


def test_notice_split_across_reads_counts_once():
    radar, serial, clock = scripted_radar()
    _stamped(radar, clock)
    serial.queue(b"Trig")
    assert radar.poll_trigger_notice(block=False) == ()
    serial.queue(b"gered\r\nl3dump:/>")
    assert len(radar.poll_trigger_notice(block=False)) == 1
    serial.queue(b"Triggered\nTriggered\n")
    assert len(radar.poll_trigger_notice(block=False)) == 2


def test_notice_inside_a_reply_is_kept_and_stripped():
    reply = b"stats\r\nframes=10 active=1\r\nTriggered\nlatched=1\r\nDone" + PROMPT
    radar, _serial, clock = scripted_radar({"stats": [reply]})
    _stamped(radar, clock)

    answer = radar.command("stats", 2.0)

    assert answer.ok
    assert "Triggered" not in answer.raw
    (notice,) = radar.take_notices()
    assert notice.in_reply and not notice.consumed


def test_notice_in_residue_is_not_warned_as_stray_output(caplog):
    radar, serial, clock = scripted_radar()
    _stamped(radar, clock)
    serial.queue(b"Triggered\nl3dump:/>")

    radar.command("stats", 2.0)

    assert "Discarding unread TI output" not in caplog.text
    assert len(radar.take_notices()) == 1


def test_release_reply_consumes_a_notice_before_its_verdict_only():
    before = b"l3release\r\nTriggered\nDone" + PROMPT
    radar, _serial, clock = scripted_radar({"l3release": [before]})
    _stamped(radar, clock)
    radar.release_frozen_ring()
    (notice,) = radar.take_notices()
    assert notice.consumed

    after = b"l3release\r\nDone\r\nTriggered\n" + PROMPT
    radar, _serial, clock = scripted_radar({"l3release": [after]})
    _stamped(radar, clock)
    radar.release_frozen_ring()
    (notice,) = radar.take_notices()
    assert not notice.consumed


def test_release_raises_on_silence_and_on_refusal():
    radar, _serial, _clock = scripted_radar({"l3release": [b""]})
    with pytest.raises(TiWedgedError) as info:
        radar.release_frozen_ring()
    assert info.value.cause == SILENT_CLI

    refusal = b"l3release\r\nError: self-trigger freeze timed out\r\nError -1" + PROMPT
    radar, _serial, _clock = scripted_radar({"l3release": [refusal]})
    with pytest.raises(RuntimeError, match="did not release"):
        radar.release_frozen_ring()


def test_dump_notices_before_magic_are_consumed_after_done_are_new():
    raw = pack_dump(np.ones((2, 6, 4, 7), dtype=complex), n_tx=3, version=3)
    reply = [(0.0, b"l3dump\r\nTriggered\n" + raw + b"Done\r\nTriggered\n" + PROMPT)]
    radar, _serial, clock = scripted_radar({"l3dump": [reply]})
    _stamped(radar, clock)

    assert radar.read_dump(timeout_s=5.0) == raw

    first, second = radar.take_notices()
    assert first.consumed and not second.consumed


def test_set_self_trigger_requires_done():
    radar, serial, _clock = scripted_radar()
    radar.set_self_trigger("triggerCfg 36 1 1")
    assert serial.sent("triggerCfg 36 1 1") == 1

    refused = b"triggerCfg 99 1 1\r\nError: trigger config\r\nError -1" + PROMPT
    radar, _serial, _clock = scripted_radar({"triggerCfg 99 1 1": [refused]})
    with pytest.raises(RuntimeError, match="config rejected"):
        radar.set_self_trigger("triggerCfg 99 1 1")

    unknown = b"triggerCfg 36 1 1\r\n'triggerCfg' is not recognized as a CLI command" + PROMPT
    radar, _serial, _clock = scripted_radar({"triggerCfg 36 1 1": [unknown]})
    with pytest.raises(RuntimeError, match="no triggerCfg"):
        radar.set_self_trigger("triggerCfg 36 1 1")


@pytest.mark.parametrize(
    ("fired", "cause", "leave", "expected"),
    [
        (1, "crossing", 0, "crossing"),
        (1, "end", 1, "end"),
        (0, "none", 1, "leave"),
        (0, "none", 0, None),
        (1, "none", 0, None),
    ],
)
def test_trigger_cause_from_trigger_log(fired, cause, leave, expected):
    text = TRACK_REPLY.format(fired=fired, cause=cause, leave=leave)
    assert parse_trigger_cause(text) == expected
    radar, _serial, _clock = scripted_radar({"triggerLog track": [done("triggerLog track", text)]})
    assert radar.trigger_cause() == expected


def test_trigger_cause_is_none_when_the_command_fails():
    radar, _serial, _clock = scripted_radar({"triggerLog track": [b""]})
    assert radar.trigger_cause() is None


# --- geometry -------------------------------------------------------------------


def test_owner_tee_gives_the_replayed_trigger_bins(tmp_path):
    """variant_sweep.py/replay_corpus.py: "tape ball bin 33 (1.5 m + 30 mm)", lead2 = 31."""
    cfg = _cfg(tmp_path)
    assert trigger_ball_bin(1.50) == 33
    assert self_trigger_bin(1.50, cfg) == 31
    assert SelfTriggerConfig(self_trigger_bin(1.50, cfg)).command == "triggerCfg 31 1 1"
    # The +0.2 m fallback ("off+4") = +4 bins.
    assert self_trigger_bin(1.50, cfg, offset_m=0.2) == 35
    assert self_trigger_bin(1.50, cfg, offset_m=-0.1) == 29


def test_trigger_bin_adds_the_array_depth_like_upstream(tmp_path):
    """Tape + 30 mm, as upstream's self_trigger_bin, not the LCMF slant alone."""
    cfg = _cfg(tmp_path)
    size = 6.0 / 128
    for tee in (1.40, 1.50, 1.575, 1.62):
        ball = int(round((tee + UPSTREAM_ARRAY_DEPTH_M) / size))
        assert trigger_ball_bin(tee) == ball
        assert self_trigger_bin(tee, cfg, offset_m=0.2) == ball - 2 + int(round(0.2 / size))
    # 1.50 m alone would be bin 32: the convention moves the bin one later.
    assert int(round(1.50 / size)) == 32


def test_self_trigger_bin_refuses_bins_outside_the_first_window(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError, match="outside the first capture window"):
        self_trigger_bin(0.5, cfg)
    with pytest.raises(ValueError, match="offset must be within"):
        self_trigger_bin(1.575, cfg, offset_m=1.5)
    with pytest.raises(ValueError, match="positive"):
        self_trigger_bin(0.0, cfg)


def test_self_trigger_config_command_and_validation():
    assert SelfTriggerConfig(36, 1.0).command == "triggerCfg 36 1 1"
    assert SelfTriggerConfig(36, 2.5).command == "triggerCfg 36 2.5 1"
    with pytest.raises(ValueError):
        SelfTriggerConfig(36, 0.5)
    with pytest.raises(ValueError):
        SelfTriggerConfig(-1, 1.0)


# --- anchor model -----------------------------------------------------------------


def test_anchor_lead_per_rule():
    model = TiAnchorModel()
    for cause in ("crossing", "end", "leave"):
        expected = NOTICE_LATENCY_MS + DEFAULT_FIRE_LATENCY_MS[cause] - SOUND_EDGE_AFTER_IMPACT_MS
        assert model.lead_ms(cause) == pytest.approx(expected)
    assert model.lead_ms(None) == pytest.approx(1.0 - 0.57 - 4.4)
    assert model.lead_ms("bogus") == model.lead_ms(None)
    # Every median lead is negative (anchor after the notice); the split is
    # sized by the slowest fire instead.
    assert all(model.lead_ms(cause) < 0 for cause in ("crossing", "end", "leave", None))
    assert model.max_lead_ms == pytest.approx(1.0 + 9.52 - 4.4)
    assert model.as_dict()["lead_ms"]["end"] == pytest.approx(model.lead_ms("end"))
    assert model.as_dict()["max_lead_ms"] == pytest.approx(model.max_lead_ms)


def test_fire_latency_defaults_are_the_of1_replay_medians():
    """E6_acq_conf02_step125.csv, owner corpus, ball >= 60 mph (see the module comment)."""
    assert DEFAULT_FIRE_LATENCY_MS == {
        "crossing": -1.70,
        "end": 1.57,
        "leave": -0.48,
        "unknown": -0.57,
        "worst": 9.52,
    }
    assert DEFAULT_SELF_TRIGGER_OFFSET_M == 0.0
    fallback = TiAnchorModel(
        fire_latency_ms=parse_fire_latency(
            "crossing=2.88,end=3.9,leave=1.19,unknown=2.91,worst=18.45"
        )
    )
    assert fallback.fire_latency_ms == OFFSET_02_FIRE_LATENCY_MS
    assert fallback.max_lead_ms == pytest.approx(1.0 + 18.45 - 4.4)


def test_max_lead_takes_a_rule_lead_longer_than_worst():
    model = TiAnchorModel(fire_latency_ms=parse_fire_latency("leave=20,worst=5"))
    assert model.max_lead_ms == pytest.approx(model.lead_ms("leave"))


def test_anchor_reproduces_the_impact_sound_edge_offset():
    """Notice at impact + latency: the anchor lands where the mic edge would be."""
    model = TiAnchorModel(fire_latency_ms=parse_fire_latency("end=6"))  # +0.2 m-like
    impact_ms = 100.0
    notice_ms = impact_ms + 6.0 + NOTICE_LATENCY_MS
    anchor_ms = notice_ms - model.lead_ms("end")
    assert anchor_ms == pytest.approx(impact_ms + SOUND_EDGE_AFTER_IMPACT_MS)


def test_parse_fire_latency():
    assert parse_fire_latency("") == DEFAULT_FIRE_LATENCY_MS
    parsed = parse_fire_latency("crossing=-1, unknown=2.5, worst=12")
    assert parsed["crossing"] == -1.0 and parsed["unknown"] == 2.5
    assert parsed["worst"] == 12.0
    assert parsed["end"] == DEFAULT_FIRE_LATENCY_MS["end"]
    for bad in ("crossing", "rule=3", "end=abc", "leave=99"):
        with pytest.raises(ValueError):
            parse_fire_latency(bad)


# --- shadow pairing ---------------------------------------------------------------


MS = 1_000_000


def _notice(ms: float) -> TriggerNotice:
    return TriggerNotice(monotonic_ns=round(ms * MS), wall_time=1000.0 + ms / 1000.0)


def _matcher():
    entries: list[dict] = []
    return TiShadowMatcher(entries.append, monotonic_ns=lambda: 0), entries


def test_shadow_matches_a_notice_to_its_edge():
    matcher, entries = _matcher()
    matcher.note_edge(1000.0, 0, ti_state="listening")
    notice = _notice(3.5)
    matcher.note_notice(notice)
    matcher.note_cause(notice, "end")
    matcher.note_disposition(notice, "dumped")

    matcher.flush(99 * MS)
    assert entries == []
    matcher.flush(100 * MS)

    (entry,) = entries
    assert entry["kind"] == "matched"
    assert entry["delta_ms"] == pytest.approx(3.5)
    assert entry["cause"] == "end" and entry["ring"] == "dumped"
    assert entry["ti_state"] == "listening"
    matcher.flush(10_000 * MS)
    assert len(entries) == 1
    assert matcher.summary() == {"matched": 1, "notice_without_edge": 0, "edge_without_notice": 0}


def test_shadow_notice_before_the_edge_matches_too():
    matcher, entries = _matcher()
    matcher.note_notice(_notice(-12.0))
    matcher.note_edge(1000.0, 0, ti_state="latched")
    matcher.flush(1000 * MS)
    assert [entry["kind"] for entry in entries] == ["matched"]
    assert entries[0]["delta_ms"] == pytest.approx(-12.0)


def test_shadow_reports_would_be_false_fires_and_misses():
    matcher, entries = _matcher()
    matcher.note_notice(_notice(0.0))  # no edge near it
    matcher.note_edge(1002.0, 2000 * MS, ti_state="listening")
    matcher.note_listen(1002.0, 0.4)
    matcher.note_edge(1010.0, 10_000 * MS, ti_state="dumping")

    matcher.flush(20_000 * MS)

    kinds = {entry["kind"]: entry for entry in entries}
    assert set(kinds) == {"notice_without_edge", "edge_without_notice"}
    false_fire = kinds["notice_without_edge"]
    assert false_fire["nearest_edge_delta_ms"] == pytest.approx(-2000.0)
    misses = [entry for entry in entries if entry["kind"] == "edge_without_notice"]
    assert [entry["ti_state"] for entry in misses] == ["listening", "dumping"]
    assert misses[0]["ti_listen_ms"] == 0.4
    assert misses[1]["ti_listen_ms"] is None


def test_shadow_window_edges_and_nearest_choice():
    matcher, entries = _matcher()
    matcher.note_edge(1000.0, 0, ti_state="listening")
    matcher.note_notice(_notice(101.0))  # just outside +100 ms
    matcher.note_notice(_notice(-150.0))  # on the window's start
    matcher.flush(10_000 * MS)
    by_kind = sorted(entry["kind"] for entry in entries)
    assert by_kind == ["matched", "notice_without_edge"]
    matched = next(entry for entry in entries if entry["kind"] == "matched")
    assert matched["delta_ms"] == pytest.approx(-150.0)

    matcher, entries = _matcher()
    matcher.note_edge(1000.0, 0, ti_state="listening")
    matcher.note_notice(_notice(-20.0))
    matcher.note_notice(_notice(4.0))
    matcher.flush(10_000 * MS)
    matched = next(entry for entry in entries if entry["kind"] == "matched")
    assert matched["delta_ms"] == pytest.approx(4.0)


def test_shadow_flush_everything_decides_open_items():
    matcher, entries = _matcher()
    matcher.note_edge(1000.0, 0, ti_state="listening")
    matcher.flush(1 * MS, everything=True)
    assert [entry["kind"] for entry in entries] == ["edge_without_notice"]
    with pytest.raises(ValueError):
        TiShadowMatcher(entries.append, window_ms=(10.0, 20.0))
