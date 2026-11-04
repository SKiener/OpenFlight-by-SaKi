"""Client clock reports: offset parsing, session-log event, one warning per session."""

import logging
import time
from datetime import datetime, timedelta, timezone

import pytest

from openflight import server as server_module
from openflight.client_clock import (
    ClientClockTracker,
    format_clock_offset,
    local_utc_offset_min,
    parse_client_clock,
)
from openflight.pairing import DeviceSecret, PairingCodes
from openflight.session_logger import SessionLogger

# The range session of 2026-10-25: the Pi stamped 2026-10-19 18:12.
PI_EPOCH = 1_760_890_320.0
# The wall-clock gap of that day (5 d 17 h 29 min). Because summer time ended in
# between, the UTC epoch offset this feature measures was one hour more (see
# TestDstSafeCorrection); here it is just a convenient large offset.
RANGE_OFFSET_S = (5 * 24 + 17) * 3600 + 29 * 60

# Real range data: session_start ts (Pi local, CEST) and the host epoch logged with it.
RANGE_PI_TS = datetime(2026, 10, 19, 18, 12, 2, 690000)
RANGE_PI_EPOCH = 1_792_426_322.69
# The real time then: 2026-10-25 11:41:31.69 CET (UTC+1).
RANGE_REAL_UTC = datetime(2026, 10, 25, 10, 41, 31, 690000, tzinfo=timezone.utc)


@pytest.fixture
def berlin_tz(monkeypatch):
    """Run with the Pi's time zone (Europe/Berlin), restoring the old one afterwards."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


class TestParse:
    def test_offset_is_client_minus_pi(self):
        reading = parse_client_clock(
            {"epoch_ms": (PI_EPOCH + RANGE_OFFSET_S) * 1000, "tz_offset_min": -120},
            client="phone",
            server_epoch=PI_EPOCH,
        )

        assert reading.offset_s == pytest.approx(RANGE_OFFSET_S)
        assert reading.client == "phone"
        assert reading.client_tz_offset_min == -120
        assert reading.to_log() == {
            "client": "phone",
            "client_epoch_ms": int((PI_EPOCH + RANGE_OFFSET_S) * 1000),
            "client_tz_offset_min": -120,
            "server_epoch": PI_EPOCH,
            "offset_s": pytest.approx(RANGE_OFFSET_S),
            "server_tz_offset_min": None,
        }

    def test_server_tz_offset_is_carried_into_the_event(self):
        reading = parse_client_clock(
            {"epoch_ms": PI_EPOCH * 1000},
            client="kiosk",
            server_epoch=PI_EPOCH,
            server_tz_offset_min=120,
        )
        assert reading.to_log()["server_tz_offset_min"] == 120

    def test_missing_timezone_is_kept_as_none(self):
        reading = parse_client_clock(
            {"epoch_ms": PI_EPOCH * 1000, "tz_offset_min": "x"},
            client="kiosk",
            server_epoch=PI_EPOCH,
        )
        assert reading.client_tz_offset_min is None
        assert reading.offset_s == 0

    @pytest.mark.parametrize(
        "data",
        [
            None,
            [],
            {},
            {"epoch_ms": "1760890320000"},
            {"epoch_ms": True},
            {"epoch_ms": float("nan")},
            {"epoch_ms": float("inf")},
            {"epoch_ms": -5},
            {"epoch_ms": 0},
        ],
    )
    def test_malformed_payloads_are_ignored(self, data):
        assert parse_client_clock(data, client="phone", server_epoch=PI_EPOCH) is None

    def test_unknown_client_kind_is_ignored(self):
        assert parse_client_clock({"epoch_ms": 1}, client="tv", server_epoch=PI_EPOCH) is None


class TestDstSafeCorrection:
    """2026-10-25: summer time ended between the Pi's date and the real date."""

    def test_local_utc_offset_follows_dst(self, berlin_tz):
        assert local_utc_offset_min(RANGE_PI_EPOCH) == 120  # Pi date: CEST
        assert local_utc_offset_min(RANGE_REAL_UTC.timestamp()) == 60  # real date: CET

    def test_documented_recipe_recovers_the_real_time(self, berlin_tz):
        reading = parse_client_clock(
            {"epoch_ms": RANGE_REAL_UTC.timestamp() * 1000, "tz_offset_min": 60},
            client="phone",
            server_epoch=RANGE_PI_EPOCH,
            server_tz_offset_min=local_utc_offset_min(RANGE_PI_EPOCH),
        )
        log = reading.to_log()
        # The epoch offset is 5 d 18 h 29 min, an hour more than the wall-clock gap.
        assert format_clock_offset(log["offset_s"]) == "+5 d 18 h 29 min"

        # docs/reference/session-log.md: real_utc = (ts - Pi UTC offset) + offset_s,
        # phone local time = real_utc + client_tz_offset_min.
        real_utc = (
            RANGE_PI_TS
            - timedelta(minutes=log["server_tz_offset_min"])
            + timedelta(seconds=log["offset_s"])
        )
        phone_local = real_utc + timedelta(minutes=log["client_tz_offset_min"])
        assert real_utc == RANGE_REAL_UTC.replace(tzinfo=None)
        assert phone_local == datetime(2026, 10, 25, 11, 41, 31, 690000)
        # Adding offset_s straight to ts would be an hour late.
        naive = RANGE_PI_TS + timedelta(seconds=log["offset_s"])
        assert naive - phone_local == timedelta(hours=1)


class TestFormat:
    @pytest.mark.parametrize(
        ("offset_s", "text"),
        [
            (RANGE_OFFSET_S, "+5 d 17 h 29 min"),
            (-RANGE_OFFSET_S, "-5 d 17 h 29 min"),
            (3 * 3600 + 5 * 60, "+3 h 5 min"),
            (-150, "-3 min"),
            (24 * 3600, "+1 d 0 h 0 min"),
            (0, "+0 min"),
        ],
    )
    def test_days_hours_minutes(self, offset_s, text):
        assert format_clock_offset(offset_s) == text


def reading(offset_s, client="phone"):
    return parse_client_clock(
        {"epoch_ms": (PI_EPOCH + offset_s) * 1000}, client=client, server_epoch=PI_EPOCH
    )


class TestTracker:
    def test_status_keeps_the_latest_offset_per_client(self):
        tracker = ClientClockTracker()
        assert tracker.status() == {"kiosk": None, "phone": None}

        tracker.record(reading(RANGE_OFFSET_S), "s1")
        tracker.record(reading(3, client="kiosk"), "s1")
        tracker.record(reading(RANGE_OFFSET_S + 60), "s1")

        assert tracker.status() == {"kiosk": 3, "phone": RANGE_OFFSET_S + 60}

    def test_warns_once_per_session_and_only_above_two_minutes(self):
        tracker = ClientClockTracker()

        assert not tracker.record(reading(120), "s1")
        assert not tracker.record(reading(-119), "s1")
        assert tracker.record(reading(121), "s1")
        assert not tracker.record(reading(RANGE_OFFSET_S), "s1")
        assert not tracker.record(reading(-RANGE_OFFSET_S, client="kiosk"), "s1")
        assert tracker.record(reading(RANGE_OFFSET_S), "s2")


@pytest.fixture(name="session_log")
def fixture_session_log(tmp_path, monkeypatch):
    log = SessionLogger(log_dir=tmp_path)
    log.start_session(mode="rolling-buffer")
    monkeypatch.setattr(server_module, "get_session_logger", lambda: log)
    monkeypatch.setattr(server_module, "client_clocks", ClientClockTracker())
    monkeypatch.setattr(server_module, "device_secret", DeviceSecret(tmp_path / "secret"))
    monkeypatch.setattr(server_module, "pairing_codes", PairingCodes())
    monkeypatch.setattr(server_module, "_paired_socket_sids", set())
    monkeypatch.setattr(server_module, "camera_replay_manager", None)
    monkeypatch.setattr(server_module.time, "time", lambda: PI_EPOCH)
    yield log
    log.end_session()


@pytest.fixture(name="socket_client")
def fixture_socket_client(monkeypatch):
    server = server_module.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(server, name, getattr(server, name))
    monkeypatch.setattr(server.eio, "async_handlers", server.eio.async_handlers)
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


def clock_events(log):
    lines = log.session_path.read_text().splitlines()
    return [line for line in lines if '"type": "client_clock"' in line]


def last_trigger_status(client):
    statuses = [e for e in client.get_received() if e["name"] == "trigger_status"]
    return statuses[-1]["args"][0]


class TestSocketHandler:
    def test_phone_report_is_logged_warned_once_and_shown_in_status(
        self, session_log, socket_client, caplog
    ):
        phone = socket_client(paired_phone())
        payload = {"epoch_ms": (PI_EPOCH + RANGE_OFFSET_S) * 1000, "tz_offset_min": -120}

        with caplog.at_level(logging.WARNING, logger="openflight.server"):
            phone.emit("client_clock", payload)
            phone.emit("client_clock", payload)

        events = clock_events(session_log)
        assert len(events) == 2
        assert '"client": "phone"' in events[0]
        assert f'"offset_s": {float(RANGE_OFFSET_S)}' in events[0]
        assert '"server_tz_offset_min": ' in events[0]
        assert '"server_tz_offset_min": null' not in events[0]
        warnings = [r.getMessage() for r in caplog.records if "[CLOCK]" in r.getMessage()]
        assert warnings == [
            "[CLOCK] Pi clock (UTC) differs from phone by +5 d 17 h 29 min (more than 120 s); "
            "correct session timestamps as described in docs/reference/session-log.md "
            "(client_clock)"
        ]
        assert last_trigger_status(phone)["clock_offset_s"] == {
            "kiosk": None,
            "phone": RANGE_OFFSET_S,
        }

    def test_kiosk_report_in_sync_is_logged_without_warning(
        self, session_log, socket_client, caplog
    ):
        kiosk = socket_client()

        with caplog.at_level(logging.WARNING, logger="openflight.server"):
            kiosk.emit("client_clock", {"epoch_ms": PI_EPOCH * 1000 + 400, "tz_offset_min": 0})

        events = clock_events(session_log)
        assert len(events) == 1
        assert '"client": "kiosk"' in events[0]
        assert not [r for r in caplog.records if "[CLOCK]" in r.getMessage()]
        assert last_trigger_status(kiosk)["clock_offset_s"]["kiosk"] == pytest.approx(0.4)

    def test_malformed_report_is_ignored(self, session_log, socket_client):
        kiosk = socket_client()
        kiosk.get_received()

        kiosk.emit("client_clock", {"epoch_ms": "soon"})

        assert clock_events(session_log) == []
        assert not [e for e in kiosk.get_received() if e["name"] == "trigger_status"]

    def test_without_a_session_logger_the_offset_is_still_tracked(
        self, session_log, socket_client, monkeypatch
    ):
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        kiosk = socket_client()

        kiosk.emit("client_clock", {"epoch_ms": (PI_EPOCH - 600) * 1000})

        assert server_module.client_clocks.status()["kiosk"] == -600
