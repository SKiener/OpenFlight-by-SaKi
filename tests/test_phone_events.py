"""iOS-companion events forwarded by the phone page: validation, clock probes, edge match, log."""

import json
import math

import pytest

from openflight import server as server_module
from openflight.client_clock import ClientClockTracker, parse_client_clock
from openflight.edge_timing import EdgeTiming, EdgeTimingLog
from openflight.pairing import DeviceSecret, PairingCodes
from openflight.phone_events import (
    EDGE_MATCH_WINDOW_S,
    MAX_KEYS,
    MAX_TEXT,
    PROBE_WINDOW_S,
    ClockEstimate,
    PhoneClockProbes,
    PhoneEventRejected,
    clean_tree,
    match_onset_to_edges,
    onset_log_entry,
    parse_audio_onset,
    parse_probe,
    parse_sensors,
)
from openflight.session_logger import SessionLogger

# Real UTC on the phone, and a Pi clock 5 days behind it (as on 2026-10-25).
PHONE_EPOCH = 1_793_007_900.0
OFFSET_S = 5 * 86400.0
PI_EPOCH = PHONE_EPOCH - OFFSET_S
ONSET_EPOCH_MS = PHONE_EPOCH * 1000 - 30.0  # the event is built ~30 ms after impact


def onset_payload(**overrides):
    payload = {
        "type": "audio_onset",
        "v": 1,
        "capture_id": "6f1c2a4e-0000-4000-8000-000000000001",
        "seq": 3,
        "onset_host_us": 812_345_678_901,
        "onset_host_us_raw": 812_345_684_101,
        "input_latency_us": 5200,
        "onset_epoch_ms": ONSET_EPOCH_MS,
        "onset_sample": 1_234_567.25,
        "onset_uncertainty_us": 40,
        "sample_rate_hz": 48000.0,
        "sample_rate_eff_hz": 47999.91,
        "host_fit_rms_us": 3.1,
        "clock": {"host_us": 812_345_700_000, "epoch_ms": ONSET_EPOCH_MS + 21.1, "bracket_us": 2},
        "epoch_ms": PHONE_EPOCH * 1000,
        "tz_offset_min": 60,
        "detection": {"snr_db": 31.5, "peak_dbfs": -6.2, "clipped": False, "rise_time_ms": 0.4},
        "features": {
            "band_db": {"low": -20.1, "mid": -14.0, "high": -18.3},
            "decay_ms_20db": 7.5,
            "second_transient": {"detected": False, "delay_ms": None, "rel_db": None},
        },
        "fat_cue": {"detected": True, "score": 0.71, "pre_low_db_1_10ms": -9.5},
        "snippet_file": None,
        "detector_version": "onset-v1",
    }
    payload.update(overrides)
    return payload


def sensors_payload(**overrides):
    payload = {
        "type": "sensors",
        "epoch_ms": PHONE_EPOCH * 1000,
        "tz_offset_min": 60,
        "clock": {"host_us": 812_345_700_000, "epoch_ms": PHONE_EPOCH * 1000, "bracket_us": 2},
        "barometer": {
            "authorization": "authorized",
            "pressure_hpa": 957.4,
            "relative_altitude_m": 0.2,
            "sample_host_us": 812_345_000_000,
            "age_s": 0.7,
        },
        "absolute_altitude": None,
        "location": {
            "authorization": "when_in_use",
            "accuracy_authorization": "full",
            "latitude": 48.1,
            "longitude": 11.5,
            "horizontal_accuracy_m": 12.0,
            "fix_epoch_ms": PHONE_EPOCH * 1000 - 5000,
            "age_s": 5.0,
        },
        "network": {"wifi": True, "cellular_available": True},
        "audio": {"state": "running", "capture_id": "c1", "last_seq": 3},
        "battery_pct": 81,
        "battery_state": "unplugged",
        "keep_awake": True,
    }
    payload.update(overrides)
    return payload


def _onset(**overrides):
    return parse_audio_onset(onset_payload(**overrides), server_epoch=PI_EPOCH)


def _edge(pi_epoch_s, *, kernel=True, latency_ms=0.8, mono_ns=1_000_000_000_000):
    """An edge whose kernel time (or, without one, callback time) is ``pi_epoch_s``."""
    if not kernel:
        return EdgeTiming(pi_epoch_s, pi_epoch_s, mono_ns)
    callback = pi_epoch_s + latency_ms / 1000.0
    return EdgeTiming(
        callback,
        callback,
        mono_ns,
        kernel_timestamp=pi_epoch_s,
        kernel_clock="monotonic",
        latency_ms=latency_ms,
    )


ONSET_PI_S = ONSET_EPOCH_MS / 1000.0 - OFFSET_S


class TestCleanTree:
    def test_keeps_json_values_and_drops_the_rest(self):
        cleaned = clean_tree(
            {"a": 1, "b": True, "c": "x" * 500, "d": float("nan"), "e": object(), 5: "key"}
        )
        assert cleaned == {"a": 1.0, "b": True, "c": "x" * MAX_TEXT, "d": None, "e": None}

    def test_bounds_size_and_depth(self):
        assert len(clean_tree({f"k{i}": i for i in range(200)})) == MAX_KEYS
        deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        assert clean_tree(deep) == {"a": {"b": {"c": {"d": None}}}}
        assert clean_tree(list(range(40))) == [float(i) for i in range(16)]


class TestAudioOnsetValidation:
    def test_valid_onset(self):
        onset = _onset()
        assert onset.seq == 3
        assert onset.onset_host_us == 812_345_678_901
        assert onset.clock_pair["bracket_us"] == 2
        log = onset.to_log()
        assert log["fat_cue"]["detected"] is True
        assert log["features"]["band_db"]["mid"] == -14.0
        assert log["detector_version"] == "onset-v1"
        assert log["client_epoch_ms"] == int(PHONE_EPOCH * 1000)
        assert onset.client.client == "phone"
        assert onset.client.offset_s == pytest.approx(OFFSET_S)

    def test_optional_fields_may_be_missing(self):
        payload = onset_payload()
        for key in ("detection", "features", "fat_cue", "sample_rate_hz", "snippet_file"):
            del payload[key]
        log = parse_audio_onset(payload, server_epoch=PI_EPOCH).to_log()
        assert log["fat_cue"] is None
        assert log["sample_rate_hz"] is None
        assert log["snippet_file"] is None

    def test_non_dict_trees_and_non_finite_numbers_become_null(self):
        log = _onset(fat_cue="fat", sample_rate_hz=float("inf")).to_log()
        assert log["fat_cue"] is None
        assert log["sample_rate_hz"] is None

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"type": "audio_status"}, "malformed"),
            ({"epoch_ms": None}, "malformed"),
            ({"epoch_ms": True}, "malformed"),
            ({"capture_id": ""}, "malformed"),
            ({"capture_id": "x" * 65}, "malformed"),
            ({"capture_id": 7}, "malformed"),
            ({"seq": 0}, "malformed"),
            ({"seq": 2.5}, "malformed"),
            ({"seq": True}, "malformed"),
            ({"onset_host_us": -1}, "malformed"),
            ({"onset_host_us": 2.0**53}, "malformed"),
            ({"onset_epoch_ms": None}, "malformed"),
            ({"onset_epoch_ms": float("nan")}, "malformed"),
            ({"onset_epoch_ms": PHONE_EPOCH * 1000 + 5000}, "out_of_range"),
            ({"onset_epoch_ms": PHONE_EPOCH * 1000 - 3601 * 1000}, "out_of_range"),
            ({"clock": None}, "malformed"),
            ({"clock": {"host_us": 1, "epoch_ms": 1, "bracket_us": -1}}, "malformed"),
        ],
    )
    def test_rejected(self, overrides, code):
        with pytest.raises(PhoneEventRejected) as error:
            _onset(**overrides)
        assert error.value.code == code

    def test_not_a_dict(self):
        with pytest.raises(PhoneEventRejected):
            parse_audio_onset(["audio_onset"], server_epoch=PI_EPOCH)


class TestSensorsValidation:
    def test_valid_snapshot(self):
        snapshot, clock = parse_sensors(sensors_payload(), server_epoch=PI_EPOCH)
        assert snapshot["barometer"]["pressure_hpa"] == 957.4
        assert snapshot["location"]["latitude"] == 48.1
        assert snapshot["audio"]["state"] == "running"
        assert snapshot["battery_pct"] == 81
        assert snapshot["client_epoch_ms"] == int(PHONE_EPOCH * 1000)
        assert "type" not in snapshot and "epoch_ms" not in snapshot
        assert clock.offset_s == pytest.approx(OFFSET_S)

    def test_missing_sensors_are_null(self):
        payload = sensors_payload(barometer=None, location="off")
        del payload["network"]
        snapshot, _ = parse_sensors(payload, server_epoch=PI_EPOCH)
        assert snapshot["barometer"] is None
        assert snapshot["location"] is None
        assert snapshot["network"] is None

    def test_only_documented_keys_are_kept(self):
        """Unknown top-level keys, and keys the handler or logger set, are dropped."""
        payload = sensors_payload(
            ts="1999-01-01T00:00:00",
            shot_id=42,
            server_epoch=1.0,
            clock_source="probe",
            client_clock_offset_s=9.0,
            extra={"a": 1},
        )
        snapshot, _ = parse_sensors(payload, server_epoch=PI_EPOCH)
        assert set(snapshot) == {
            "clock",
            "barometer",
            "absolute_altitude",
            "location",
            "network",
            "audio",
            "battery_pct",
            "battery_state",
            "keep_awake",
            "client_epoch_ms",
            "client_tz_offset_min",
        }
        assert snapshot["clock"]["bracket_us"] == 2
        assert snapshot["keep_awake"] is True
        assert snapshot["battery_state"] == "unplugged"

    def test_missing_scalars_are_null(self):
        payload = sensors_payload()
        for key in ("clock", "battery_pct", "battery_state", "keep_awake"):
            del payload[key]
        snapshot, _ = parse_sensors(payload, server_epoch=PI_EPOCH)
        assert snapshot["clock"] is None
        assert snapshot["battery_pct"] is None
        assert snapshot["battery_state"] is None
        assert snapshot["keep_awake"] is None

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"type": "audio_onset"}, "malformed"),
            ({"epoch_ms": "now"}, "malformed"),
            ({"barometer": {"pressure_hpa": 1500}}, "out_of_range"),
            ({"location": {"latitude": 91, "longitude": 0}}, "out_of_range"),
            ({"location": {"latitude": 0, "longitude": -181}}, "out_of_range"),
        ],
    )
    def test_rejected(self, overrides, code):
        with pytest.raises(PhoneEventRejected) as error:
            parse_sensors(sensors_payload(**overrides), server_epoch=PI_EPOCH)
        assert error.value.code == code


class TestClockProbes:
    SEND_MS = PHONE_EPOCH * 1000

    def _answer(self, probes, seq=1, *, sid="s1", send_ms=SEND_MS, pi_epoch_s=PI_EPOCH, now=100.0):
        return probes.answer(
            sid, seq, send_ms, pi_epoch_s=pi_epoch_s, pi_monotonic_ns=int(now * 1e9)
        )

    def test_ack_echoes_the_pi_clocks(self):
        ack = self._answer(PhoneClockProbes())
        assert ack == {
            "ok": True,
            "probe_seq": 1,
            "pi_epoch_s": PI_EPOCH,
            "pi_monotonic_ns": 100_000_000_000,
        }

    def test_report_gives_the_midpoint_offset(self):
        probes = PhoneClockProbes()
        # The Pi stamped the probe 4 ms after the phone sent it; the round trip took 8 ms.
        self._answer(probes, pi_epoch_s=PI_EPOCH + 0.004)
        sample = probes.report("s1", 1, self.SEND_MS, 8.0)
        assert sample.offset_s == pytest.approx(OFFSET_S, abs=1e-6)
        assert sample.rtt_ms == 8.0

    def test_report_must_match_an_answered_probe_of_the_same_connection(self):
        probes = PhoneClockProbes()
        self._answer(probes)
        assert probes.report("other-sid", 1, self.SEND_MS, 8.0) is None
        assert probes.report("s1", 2, self.SEND_MS, 8.0) is None
        assert probes.report("s1", 1, self.SEND_MS + 1, 8.0) is None
        assert probes.report("s1", 1, self.SEND_MS, -1.0) is None
        assert probes.report("s1", 1, self.SEND_MS, 8.0) is not None
        # A report is used once.
        assert probes.report("s1", 1, self.SEND_MS, 8.0) is None

    def test_best_is_the_smallest_round_trip_within_the_window(self):
        probes = PhoneClockProbes()
        for seq, rtt, offset_ms in ((1, 30.0, 9.0), (2, 6.0, 1.0), (3, 12.0, 3.0)):
            self._answer(probes, seq, pi_epoch_s=PI_EPOCH - offset_ms / 1000 + rtt / 2000)
            probes.report("s1", seq, self.SEND_MS, rtt)
        estimate = probes.estimate(100.0, client_clock_offset_s=OFFSET_S + 0.5)
        assert estimate.source == "probe"
        assert estimate.offset_s == pytest.approx(OFFSET_S + 0.001, abs=1e-6)
        assert estimate.uncertainty_ms == 3.0
        assert estimate.probe_count == 3
        # Once every probe is older than the window, the connect-time offset is used.
        later = probes.estimate(100.0 + PROBE_WINDOW_S + 1, client_clock_offset_s=OFFSET_S + 0.5)
        assert (later.source, later.offset_s, later.probe_count) == (
            "client_clock",
            OFFSET_S + 0.5,
            0,
        )
        assert probes.estimate(1e9, None) == ClockEstimate(None, None, None, 0)

    @pytest.mark.parametrize(
        ("payload", "probe", "report"),
        [
            ({"probe_seq": 1, "client_send_epoch_ms": 5.0}, (1, 5.0), None),
            (
                {"report": {"probe_seq": 1, "client_send_epoch_ms": 5.0, "rtt_ms": 3}},
                None,
                (1, 5.0, 3.0),
            ),
            (
                {
                    "probe_seq": 2,
                    "client_send_epoch_ms": 9.0,
                    "report": {"probe_seq": 1, "client_send_epoch_ms": 5.0, "rtt_ms": 3},
                },
                (2, 9.0),
                (1, 5.0, 3.0),
            ),
        ],
    )
    def test_parse_probe(self, payload, probe, report):
        assert parse_probe(payload) == (probe, report)

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {},
            {"probe_seq": 0, "client_send_epoch_ms": 5.0},
            {"probe_seq": 1, "client_send_epoch_ms": -5.0},
            {"probe_seq": True, "client_send_epoch_ms": 5.0},
            {"report": {"probe_seq": 1, "client_send_epoch_ms": 5.0, "rtt_ms": float("nan")}},
        ],
    )
    def test_parse_probe_rejects(self, payload):
        with pytest.raises(PhoneEventRejected):
            parse_probe(payload)


class TestEdgeMatch:
    def test_nearest_edge_in_the_window_is_matched_with_its_residual(self):
        edges = [(7, _edge(ONSET_PI_S - 0.250)), (8, _edge(ONSET_PI_S - 0.0123, mono_ns=5_000_000))]
        match = match_onset_to_edges(ONSET_PI_S, edges)
        assert match["edge_match"] == "matched"
        assert match["edge_candidates"] == 1
        assert match["edge_sequence"] == 8
        assert match["edge_time_source"] == "kernel"
        assert match["edge_residual_ms"] == pytest.approx(12.3, abs=1e-3)
        assert match["edge_kernel_timestamp"] == pytest.approx(ONSET_PI_S - 0.0123)
        # Kernel time on the monotonic clock: callback stamp minus the callback latency.
        assert match["edge_monotonic_ns"] == 5_000_000 - 800_000
        assert match["edge_next_residual_ms"] is None

    def test_onset_before_the_edge_gives_a_negative_residual(self):
        match = match_onset_to_edges(ONSET_PI_S, [(1, _edge(ONSET_PI_S + 0.020))])
        assert match["edge_residual_ms"] == pytest.approx(-20.0, abs=1e-3)

    def test_without_kernel_time_the_callback_time_is_used(self):
        match = match_onset_to_edges(ONSET_PI_S, [(1, _edge(ONSET_PI_S - 0.005, kernel=False))])
        assert match["edge_time_source"] == "callback"
        assert match["edge_kernel_timestamp"] is None
        assert match["edge_monotonic_ns"] == 1_000_000_000_000

    def test_window_edges(self):
        inside = [(1, _edge(ONSET_PI_S - EDGE_MATCH_WINDOW_S + 1e-4))]
        outside = [(1, _edge(ONSET_PI_S - EDGE_MATCH_WINDOW_S - 1e-4))]
        assert match_onset_to_edges(ONSET_PI_S, inside)["edge_match"] == "matched"
        assert match_onset_to_edges(ONSET_PI_S, outside)["edge_match"] == "none"

    def test_several_edges_in_the_window_are_ambiguous(self):
        edges = [(1, _edge(ONSET_PI_S + 0.060)), (2, _edge(ONSET_PI_S - 0.010))]
        match = match_onset_to_edges(ONSET_PI_S, edges)
        assert match["edge_match"] == "ambiguous"
        assert match["edge_candidates"] == 2
        assert match["edge_sequence"] == 2
        assert match["edge_next_residual_ms"] == pytest.approx(-60.0, abs=1e-3)

    def test_no_edges_and_no_clock(self):
        assert match_onset_to_edges(ONSET_PI_S, [])["edge_match"] == "none"
        no_clock = match_onset_to_edges(None, [(1, _edge(ONSET_PI_S))])
        assert no_clock["edge_match"] == "no_clock"
        assert no_clock["edge_sequence"] is None

    def test_log_entry_maps_the_onset_with_the_estimate(self):
        estimate = ClockEstimate(OFFSET_S, "probe", 2.5, 4)
        entry = onset_log_entry(
            _onset(),
            estimate=estimate,
            edges=[(9, _edge(ONSET_PI_S - 0.002))],
            server_monotonic_ns=7,
        )
        assert entry["onset_pi_epoch_s"] == pytest.approx(ONSET_PI_S, abs=1e-6)
        assert entry["clock_source"] == "probe"
        assert entry["clock_uncertainty_ms"] == 2.5
        assert entry["clock_probe_n"] == 4
        assert entry["edge_match"] == "matched"
        assert entry["edge_residual_ms"] == pytest.approx(2.0, abs=1e-3)
        assert entry["server_monotonic_ns"] == 7
        assert entry["fat_cue"]["score"] == 0.71
        assert json.dumps(entry)  # JSON-safe for the session log
        assert all(not isinstance(v, float) or math.isfinite(v) for v in entry.values())


# --- over the socket ------------------------------------------------------


@pytest.fixture(name="session_log")
def fixture_session_log(tmp_path, monkeypatch):
    log = SessionLogger(log_dir=tmp_path)
    log.start_session(mode="rolling-buffer")
    monkeypatch.setattr(server_module, "get_session_logger", lambda: log)
    monkeypatch.setattr(server_module, "client_clocks", ClientClockTracker())
    monkeypatch.setattr(server_module, "phone_clock_probes", PhoneClockProbes())
    monkeypatch.setattr(server_module, "sound_edge_timing", EdgeTimingLog())
    monkeypatch.setattr(server_module, "device_secret", DeviceSecret(tmp_path / "secret"))
    monkeypatch.setattr(server_module, "pairing_codes", PairingCodes())
    monkeypatch.setattr(server_module, "_paired_socket_sids", set())
    monkeypatch.setattr(server_module, "camera_replay_manager", None)
    monkeypatch.setattr(server_module, "monitor", None)
    monkeypatch.setattr(server_module.time, "time", lambda: PI_EPOCH)
    yield log
    log.end_session()


@pytest.fixture(name="socket_client")
def fixture_socket_client(monkeypatch):
    sio = server_module.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(sio, name, getattr(sio, name))
    monkeypatch.setattr(sio.eio, "async_handlers", sio.eio.async_handlers)
    clients = []

    def connect(http_client=None):
        client = server_module.socketio.test_client(
            server_module.app, flask_test_client=http_client or server_module.app.test_client()
        )
        clients.append(client)
        return client

    yield connect
    for client in clients:
        if client.is_connected():
            client.disconnect()


def paired_phone():
    code = server_module.app.test_client().post("/api/pairing/code").get_json()["code"]
    phone = server_module.app.test_client()
    phone.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    assert phone.post("/api/pairing/exchange", json={"code": code}).status_code == 200
    return phone


def log_events(log, entry_type):
    return [
        json.loads(line)
        for line in log.session_path.read_text().splitlines()
        if f'"type": "{entry_type}"' in line
    ]


def _note_edge(pi_epoch_s, mono_ns=2_000_000_000):
    server_module.sound_edge_timing.record(
        pi_epoch_s, callback_timestamp=pi_epoch_s, callback_monotonic_ns=mono_ns, ticks=None
    )


class TestSocket:
    def test_events_are_registered_to_their_handlers(self):
        handlers = server_module.socketio.server.handlers["/"]
        for event in ("phone_audio_onset", "phone_sensors", "phone_clock_probe"):
            registered = getattr(handlers[event], "__wrapped__", handlers[event])
            assert registered is getattr(server_module, f"handle_{event}")

    @pytest.mark.parametrize(
        ("event", "payload"),
        [
            ("phone_audio_onset", onset_payload()),
            ("phone_sensors", sensors_payload()),
            ("phone_clock_probe", {"probe_seq": 1, "client_send_epoch_ms": 5.0}),
        ],
    )
    def test_the_kiosk_is_refused(self, session_log, socket_client, event, payload):
        kiosk = socket_client()
        assert kiosk.emit(event, payload, callback=True) == {"ok": False, "error": "phone_only"}
        assert log_events(session_log, event) == []

    def test_onset_with_only_the_connect_clock_is_matched_and_logged(
        self, session_log, socket_client
    ):
        phone = socket_client(paired_phone())
        phone.emit("client_clock", {"epoch_ms": PHONE_EPOCH * 1000, "tz_offset_min": 60})
        _note_edge(ONSET_PI_S - 0.250)
        _note_edge(ONSET_PI_S - 0.004)

        ack = phone.emit("phone_audio_onset", onset_payload(), callback=True)

        assert ack["ok"] is True
        assert ack["edge_match"] == "matched"
        assert ack["clock_source"] == "client_clock"
        assert ack["edge_residual_ms"] == pytest.approx(4.0, abs=0.01)
        (entry,) = log_events(session_log, "phone_audio_onset")
        assert entry["edge_sequence"] == 2
        assert entry["edge_time_source"] == "callback"
        assert entry["clock_offset_s"] == pytest.approx(OFFSET_S)
        assert entry["fat_cue"]["detected"] is True
        assert entry["features"]["decay_ms_20db"] == 7.5
        assert entry["capture_id"] == onset_payload()["capture_id"]
        assert entry["server_epoch"] == PI_EPOCH

    def test_clock_probe_round_trip_tightens_the_mapping(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        # The connect-time offset is 40 ms off (one-way delay); the probe is not.
        phone.emit("client_clock", {"epoch_ms": PHONE_EPOCH * 1000 + 40, "tz_offset_min": 60})
        send_ms = PHONE_EPOCH * 1000 - 3.0
        ack = phone.emit(
            "phone_clock_probe",
            {"probe_seq": 1, "client_send_epoch_ms": send_ms, "epoch_ms": send_ms},
            callback=True,
        )
        assert ack["ok"] is True
        assert ack["probe_seq"] == 1
        assert ack["pi_epoch_s"] == PI_EPOCH
        assert isinstance(ack["pi_monotonic_ns"], int)
        report = {"probe_seq": 1, "client_send_epoch_ms": send_ms, "rtt_ms": 6.0}
        assert phone.emit("phone_clock_probe", {"report": report}, callback=True) == {"ok": True}
        _note_edge(ONSET_PI_S - 0.0015)

        ack = phone.emit("phone_audio_onset", onset_payload(), callback=True)

        assert ack["clock_source"] == "probe"
        assert ack["edge_residual_ms"] == pytest.approx(1.5, abs=0.01)
        (entry,) = log_events(session_log, "phone_audio_onset")
        assert entry["clock_uncertainty_ms"] == 3.0
        assert entry["clock_probe_n"] == 1

    def test_onset_far_from_every_edge_is_logged_unmatched(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit("client_clock", {"epoch_ms": PHONE_EPOCH * 1000, "tz_offset_min": 60})
        _note_edge(ONSET_PI_S - 2.0)
        ack = phone.emit("phone_audio_onset", onset_payload(), callback=True)
        assert ack["edge_match"] == "none"
        assert log_events(session_log, "phone_audio_onset")[0]["edge_sequence"] is None

    def test_bad_onset_is_refused_and_not_logged(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        ack = phone.emit("phone_audio_onset", onset_payload(seq=-1), callback=True)
        assert ack == {"ok": False, "error": "malformed"}
        assert log_events(session_log, "phone_audio_onset") == []

    def test_sensors_are_logged_with_the_clock(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        assert phone.emit("phone_sensors", sensors_payload(), callback=True) == {"ok": True}
        (entry,) = log_events(session_log, "phone_sensors")
        assert entry["barometer"]["pressure_hpa"] == 957.4
        assert entry["battery_pct"] == 81
        assert entry["client_clock_offset_s"] == pytest.approx(OFFSET_S)
        assert entry["server_epoch"] == PI_EPOCH
        assert entry["clock_source"] is None  # no client_clock or probe yet

    def test_sensors_cannot_replace_the_log_timestamp(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        payload = {**sensors_payload(), "ts": "1999-01-01T00:00:00", "shot_id": 42}
        assert phone.emit("phone_sensors", payload, callback=True) == {"ok": True}
        (entry,) = log_events(session_log, "phone_sensors")
        assert entry["ts"] != "1999-01-01T00:00:00"
        assert not entry["ts"].startswith("1999")
        assert "shot_id" not in entry
        assert entry["server_epoch"] == PI_EPOCH

    def test_phone_events_change_no_shot_state(self, session_log, socket_client):
        """Log only: the weather, the trigger status and the shots stay as they were."""
        phone = socket_client(paired_phone())
        phone.get_received()
        before = server_module.get_weather_store().snapshot(1.184)
        phone.emit("phone_sensors", sensors_payload(), callback=True)
        phone.emit("phone_audio_onset", onset_payload(), callback=True)
        assert server_module.get_weather_store().snapshot(1.184) == before
        assert phone.get_received() == []


def test_parse_client_clock_is_reused_for_the_phone_clock():
    clock = parse_client_clock(onset_payload(), client="phone", server_epoch=PI_EPOCH)
    assert clock.offset_s == pytest.approx(OFFSET_S)
