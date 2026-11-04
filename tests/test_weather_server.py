"""Weather over the socket: location, phone reports, density per shot, session log."""

import json
from datetime import datetime

import pytest

from openflight import server as server_module
from openflight.ballistics import TRACKMAN_FLAT_AIR_DENSITY
from openflight.client_clock import ClientClockTracker
from openflight.clubs import ClubType
from openflight.gspro.codec import GSProCodec
from openflight.launch_monitor import Shot
from openflight.pairing import DeviceSecret, PairingCodes
from openflight.session_logger import SessionLogger
from openflight.sim import resolve_shot
from openflight.sim.types import PlayerState
from openflight.weather import FRESH_MAX_S, FetchFailureRateGate, WeatherStore, moist_air_density

PHONE_EPOCH = 1_793_007_900.0
PI_EPOCH = PHONE_EPOCH - 5 * 86400
LOCATION = {"name": "Golfpark", "latitude": 48.1, "longitude": 11.5, "country": "Deutschland"}


class FakeClock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


@pytest.fixture(name="clock")
def fixture_clock(tmp_path, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        server_module, "weather_store", WeatherStore(tmp_path / "weather.json", clock=clock)
    )
    return clock


@pytest.fixture(name="session_log")
def fixture_session_log(tmp_path, monkeypatch, clock):  # pylint: disable=unused-argument
    log = SessionLogger(log_dir=tmp_path)
    log.start_session(mode="rolling-buffer")
    monkeypatch.setattr(server_module, "get_session_logger", lambda: log)
    monkeypatch.setattr(server_module, "client_clocks", ClientClockTracker())
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


def open_meteo_report(**overrides):
    payload = {
        "source": "open-meteo",
        "temperature_c": 9.9,
        "pressure_hpa": 960.2,
        "humidity_pct": 65,
        "wind_speed_ms": 3.28,
        "wind_direction_deg": 258,
        "observed_epoch_s": PHONE_EPOCH - 600,
        "latitude": 48.1,
        "longitude": 11.5,
        "epoch_ms": PHONE_EPOCH * 1000,
        "tz_offset_min": 60,
    }
    payload.update(overrides)
    return payload


def log_events(log, entry_type):
    return [
        json.loads(line)
        for line in log.session_path.read_text().splitlines()
        if f'"type": "{entry_type}"' in line
    ]


def last_weather_state(client):
    states = [e for e in client.get_received() if e["name"] == "weather_state"]
    return states[-1]["args"][0]


class TestSocket:
    def test_connect_sends_the_weather_state(self, session_log, socket_client):
        kiosk = socket_client()
        state = last_weather_state(kiosk)
        assert state["location"] is None
        assert state["reading"] is None
        assert state["density_source"] == "standard"
        assert state["density_kg_m3"] == TRACKMAN_FLAT_AIR_DENSITY
        assert state["fetch_due"] is False

    def test_location_is_stored_logged_and_broadcast(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        kiosk = socket_client()

        assert phone.emit("set_weather_location", LOCATION, callback=True) == {"ok": True}

        assert last_weather_state(kiosk)["location"]["name"] == "Golfpark"
        assert last_weather_state(phone)["fetch_due"] is True
        assert log_events(session_log, "weather_location")[0]["latitude"] == 48.1
        assert server_module.get_weather_store().location.name == "Golfpark"

    def test_bad_location_is_refused(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        assert phone.emit(
            "set_weather_location", {"latitude": 123, "longitude": 0}, callback=True
        ) == {"ok": False, "error": "invalid_location"}
        assert log_events(session_log, "weather_location") == []

    def test_phone_report_flies_logs_and_broadcasts(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit("set_weather_location", LOCATION, callback=True)

        assert phone.emit("weather_report", open_meteo_report(), callback=True) == {"ok": True}

        state = last_weather_state(phone)
        assert state["density_source"] == "open-meteo"
        assert state["density_kg_m3"] == pytest.approx(moist_air_density(9.9, 960.2, 65), abs=1e-4)
        assert state["reading"]["age_s"] == pytest.approx(600)
        assert state["fetch_due"] is False
        [event] = log_events(session_log, "weather")
        assert event["client"] == "phone"
        assert event["clock_offset_s"] == pytest.approx(5 * 86400)
        assert event["observed_pi_epoch_s"] == pytest.approx(PI_EPOCH - 600)
        assert event["wind_speed_ms"] == 3.28
        assert event["air_density_kg_m3"] == round(moist_air_density(9.9, 960.2, 65), 4)

    def test_rejected_report_changes_nothing(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit("set_weather_location", LOCATION, callback=True)

        result = phone.emit("weather_report", open_meteo_report(pressure_hpa=2000), callback=True)

        assert result == {"ok": False, "error": "out_of_range"}
        assert server_module.get_weather_store().reading is None
        assert log_events(session_log, "weather") == []

    def test_report_without_clock_is_malformed(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit("set_weather_location", LOCATION, callback=True)
        report = open_meteo_report()
        del report["epoch_ms"]
        assert phone.emit("weather_report", report, callback=True) == {
            "ok": False,
            "error": "malformed",
        }

    def test_kiosk_manual_entry(self, session_log, socket_client):
        kiosk = socket_client()
        manual = {
            "source": "manual",
            "temperature_c": 12,
            "pressure_hpa": 1008,
            "epoch_ms": PI_EPOCH * 1000,
        }
        assert kiosk.emit("weather_report", manual, callback=True) == {"ok": True}
        state = last_weather_state(kiosk)
        assert state["density_source"] == "manual"
        assert state["reading"]["humidity_assumed"] is True
        assert log_events(session_log, "weather")[0]["client"] == "kiosk"


def fetch_failure(**overrides):
    payload = {
        "stage": "fetch",
        "reason": "http_503",
        "consecutive_failures": 2,
        "next_retry_s": 30,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(name="failure_clock")
def fixture_failure_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        server_module, "weather_fetch_failure_gate", FetchFailureRateGate(clock=clock)
    )
    return clock


@pytest.mark.usefixtures("failure_clock")
class TestFetchFailed:
    def test_failure_is_logged_once_and_not_broadcast(self, session_log, socket_client, caplog):
        phone = socket_client(paired_phone())
        kiosk = socket_client()
        phone.get_received()
        kiosk.get_received()

        with caplog.at_level("INFO", logger=server_module.logger.name):
            ack = phone.emit("weather_fetch_failed", fetch_failure(), callback=True)

        assert ack == {"ok": True}
        [event] = log_events(session_log, "weather_fetch_failed")
        assert {k: v for k, v in event.items() if k != "ts"} == {
            "type": "weather_fetch_failed",
            "stage": "fetch",
            "reason": "http_503",
            "consecutive_failures": 2,
            "next_retry_s": 30,
            "client": "phone",
        }
        assert kiosk.get_received() == []
        assert phone.get_received() == []
        lines = [r.getMessage() for r in caplog.records if "failed (http_503)" in r.getMessage()]
        assert lines == ["[WEATHER] phone: fetch failed (http_503), 2 in a row, next try in 30 s"]
        assert all(
            r.levelname == "INFO" for r in caplog.records if "failed (http_503)" in r.getMessage()
        )

    def test_location_and_free_text_never_reach_the_log(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit("set_weather_location", LOCATION, callback=True)
        payload = fetch_failure(
            stage="location",
            reason="no_permission",
            latitude=48.10235,
            longitude=11.50012,
            message="location access is denied for OpenFlight",
            url="https://api.open-meteo.com/v1/forecast?latitude=48.1",
        )
        assert phone.emit("weather_fetch_failed", payload, callback=True) == {"ok": True}

        [event] = log_events(session_log, "weather_fetch_failed")
        assert set(event) == {
            "ts",
            "type",
            "stage",
            "reason",
            "consecutive_failures",
            "next_retry_s",
            "client",
        }
        line = next(
            line
            for line in session_log.session_path.read_text().splitlines()
            if "weather_fetch_failed" in line
        )
        assert "48.1" not in line and "11.50" not in line and "open-meteo" not in line

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "offline",
            fetch_failure(stage="weather"),
            fetch_failure(reason="Open-Meteo HTTP 503 via cellular"),
            fetch_failure(reason="http_5xx"),
            fetch_failure(consecutive_failures="2"),
            fetch_failure(next_retry_s=None),
        ],
    )
    def test_invalid_reports_are_ignored(self, session_log, socket_client, payload):
        phone = socket_client(paired_phone())
        assert phone.emit("weather_fetch_failed", payload, callback=True) == {
            "ok": False,
            "error": "malformed",
        }
        assert log_events(session_log, "weather_fetch_failed") == []
        # An ignored report does not hold back the next valid one.
        assert phone.emit("weather_fetch_failed", fetch_failure(), callback=True) == {"ok": True}

    def test_counts_are_clamped(self, session_log, socket_client):
        phone = socket_client(paired_phone())
        phone.emit(
            "weather_fetch_failed",
            fetch_failure(consecutive_failures=-1, next_retry_s=10**12),
            callback=True,
        )
        [event] = log_events(session_log, "weather_fetch_failed")
        assert (event["consecutive_failures"], event["next_retry_s"]) == (0, 100_000)

    def test_more_than_one_report_per_5_s_is_ignored(
        self, session_log, socket_client, failure_clock
    ):
        phone = socket_client(paired_phone())
        assert phone.emit("weather_fetch_failed", fetch_failure(), callback=True) == {"ok": True}
        failure_clock.now += 4.9
        assert phone.emit(
            "weather_fetch_failed", fetch_failure(consecutive_failures=3), callback=True
        ) == {"ok": False, "error": "rate_limited"}
        failure_clock.now += 0.1
        assert phone.emit(
            "weather_fetch_failed", fetch_failure(consecutive_failures=4), callback=True
        ) == {"ok": True}

        events = log_events(session_log, "weather_fetch_failed")
        assert [e["consecutive_failures"] for e in events] == [2, 4]

    def test_without_a_session_log_nothing_is_written(
        self, session_log, socket_client, monkeypatch
    ):
        phone = socket_client(paired_phone())
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        assert phone.emit("weather_fetch_failed", fetch_failure(), callback=True) == {"ok": True}
        assert log_events(session_log, "weather_fetch_failed") == []


class _ConnectedSim:
    """A simulator connector stub that records what it is sent."""

    name = "gspro"

    class codec:  # pylint: disable=invalid-name,too-few-public-methods
        """The field list the sim_shot emit reads."""

        @staticmethod
        def fields_for_target():
            """The fields this stub reports."""
            return ["carry"]

    def __init__(self, connected=True):
        self._connected = connected
        self.sent = []

    def is_connected(self):
        """Whether the stub counts as connected."""
        return self._connected

    def send_shot(self, resolved):
        """Record the resolved shot."""
        self.sent.append(resolved)


def _driver_shot():
    return Shot(
        ball_speed_mph=150.0,
        timestamp=datetime.now(),
        club=ClubType.DRIVER,
        launch_angle_vertical=12.0,
        launch_angle_confidence=0.95,
    )


@pytest.mark.usefixtures("isolate_finalization")
class TestShotDensity:
    def test_without_weather_the_fixed_density_flies(self, clock):  # pylint: disable=unused-argument
        shot = _driver_shot()
        conditions = server_module.resolve_launch(shot)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        expected = server_module.simulate(conditions, air_density=TRACKMAN_FLAT_AIR_DENSITY)
        assert shot.carry_spin_adjusted == pytest.approx(expected.carry_yards)
        assert shot.carry_air_density_kg_m3 == TRACKMAN_FLAT_AIR_DENSITY
        assert shot.carry_air_density_source == "standard"
        assert shot.carry_weather_age_s is None
        logged = shot.to_dict()
        assert logged["carry_air_density_source"] == "standard"

    def _record_weather(self, **overrides):
        store = server_module.get_weather_store()
        store.set_location(LOCATION)
        reading = server_module.parse_weather_report(
            open_meteo_report(**overrides),
            clock=server_module.parse_client_clock(
                open_meteo_report(), client="phone", server_epoch=PI_EPOCH
            ),
            location=store.location,
            received_monotonic=store.now(),
        )
        store.record(reading)
        return reading

    def test_fresh_weather_flies_and_is_on_the_shot(self, clock):
        reading = self._record_weather()
        clock.now += 1200
        shot = _driver_shot()
        conditions = server_module.resolve_launch(shot)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        expected = server_module.simulate(conditions, air_density=reading.air_density_kg_m3)
        assert shot.carry_spin_adjusted == pytest.approx(expected.carry_yards)
        assert shot.carry_air_density_kg_m3 == pytest.approx(reading.air_density_kg_m3, abs=1e-4)
        assert shot.carry_air_density_source == "open-meteo"
        assert shot.carry_weather_age_s == pytest.approx(1800)
        # Thinner air than 1.184 flies further.
        flat = server_module.simulate(conditions, air_density=TRACKMAN_FLAT_AIR_DENSITY)
        assert reading.air_density_kg_m3 < TRACKMAN_FLAT_AIR_DENSITY
        assert shot.carry_spin_adjusted > flat.carry_yards

    def test_sim_export_carry_ignores_the_weather(self, clock, monkeypatch):  # pylint: disable=unused-argument
        """The GSPro/OpenConnect export stays at the fixed --air-density; only the
        live carry follows the weather."""
        monkeypatch.setattr(server_module, "sim_connectors", [_ConnectedSim()])

        def exported_carry(shot):
            """The CarryDistance the GSPro/OpenConnect export sends for ``shot``."""
            payload = json.loads(GSProCodec().build_shot(resolve_shot(shot, PlayerState())))
            return payload["BallData"]["CarryDistance"]

        without_weather = _driver_shot()
        server_module._finalize_shot_detected(  # pylint: disable=protected-access
            without_weather, emit_event="shot"
        )
        # Cold, high-pressure air: about 7.5 % denser than 1.184.
        reading = self._record_weather(temperature_c=5, pressure_hpa=1020, humidity_pct=85)
        with_weather = _driver_shot()
        server_module._finalize_shot_detected(  # pylint: disable=protected-access
            with_weather, emit_event="shot"
        )

        assert reading.air_density_kg_m3 > TRACKMAN_FLAT_AIR_DENSITY + 0.05
        assert with_weather.carry_spin_adjusted < without_weather.carry_spin_adjusted - 2
        assert exported_carry(with_weather) == exported_carry(without_weather)
        assert with_weather.carry_standard_air_yards == pytest.approx(
            without_weather.carry_spin_adjusted
        )
        assert with_weather.to_dict()["carry_standard_air_yards"] == pytest.approx(
            without_weather.carry_spin_adjusted
        )

    def test_no_simulator_flies_the_shot_once(self, clock, monkeypatch):  # pylint: disable=unused-argument
        """Without a connected simulator the fixed-density export carry is not flown."""
        self._record_weather(temperature_c=5, pressure_hpa=1020, humidity_pct=85)
        calls = []
        real_simulate = server_module.simulate

        def counting_simulate(conditions, **kwargs):
            """Record each simulated air density."""
            calls.append(kwargs.get("air_density"))
            return real_simulate(conditions, **kwargs)

        monkeypatch.setattr(server_module, "simulate", counting_simulate)
        monkeypatch.setattr(server_module, "sim_connectors", [_ConnectedSim(connected=False)])
        shot = _driver_shot()
        server_module._finalize_shot_detected(  # pylint: disable=protected-access
            shot, emit_event="shot"
        )

        assert len(calls) == 1
        assert calls[0] != TRACKMAN_FLAT_AIR_DENSITY
        assert shot.carry_standard_air_yards is None

    def test_simulator_connecting_after_finalization_gets_the_fixed_density(
        self, clock, monkeypatch
    ):  # pylint: disable=unused-argument
        """A simulator that connects between finalization and the export still gets
        the fixed-density carry, not the weather one."""
        without_weather = _driver_shot()
        server_module._finalize_shot_detected(  # pylint: disable=protected-access
            without_weather, emit_event="shot"
        )
        self._record_weather(temperature_c=5, pressure_hpa=1020, humidity_pct=85)
        with_weather = _driver_shot()
        server_module._finalize_shot_metrics(with_weather)  # pylint: disable=protected-access
        assert with_weather.carry_standard_air_yards is None

        sim = _ConnectedSim()
        monkeypatch.setattr(server_module, "sim_connectors", [sim])
        server_module._forward_shot_to_simulators(with_weather)  # pylint: disable=protected-access

        assert with_weather.carry_spin_adjusted < without_weather.carry_spin_adjusted - 2
        assert len(sim.sent) == 1
        assert sim.sent[0].carry_yards == pytest.approx(without_weather.carry_spin_adjusted)

    def test_stale_weather_falls_back(self, clock):
        self._record_weather()
        clock.now += FRESH_MAX_S
        shot = _driver_shot()

        server_module._finalize_shot_detected(shot, emit_event="shot")

        assert shot.carry_air_density_source == "standard"
        assert shot.carry_air_density_kg_m3 == TRACKMAN_FLAT_AIR_DENSITY

    def test_configured_fallback_density_is_the_standard(self, clock, monkeypatch):  # pylint: disable=unused-argument
        monkeypatch.setattr(server_module, "air_density_kg_m3", 1.1)
        shot = _driver_shot()
        server_module._finalize_shot_detected(shot, emit_event="shot")
        assert shot.carry_air_density_kg_m3 == 1.1

    def test_table_carry_has_no_density(self, clock, monkeypatch):  # pylint: disable=unused-argument
        monkeypatch.setattr(server_module, "ballistics_enabled", False)
        self._record_weather()
        shot = _driver_shot()
        server_module._finalize_shot_detected(shot, emit_event="shot")
        assert shot.carry_model == "table"
        assert shot.carry_air_density_kg_m3 is None
        assert shot.carry_air_density_source is None
        assert shot.carry_standard_air_yards is None
