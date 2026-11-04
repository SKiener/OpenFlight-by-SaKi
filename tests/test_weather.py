"""Air density from range weather: CIPM-2007, report validation, freshness, persistence."""

import json
import re
from pathlib import Path

import pytest

from openflight.ballistics import TRACKMAN_FLAT_AIR_DENSITY
from openflight.client_clock import parse_client_clock
from openflight.weather import (
    ASSUMED_HUMIDITY_PCT,
    BAROMETER_FRESH_MAX_S,
    FETCH_FAILURE_MIN_INTERVAL_S,
    FETCH_FAILURE_REASONS,
    FETCH_INTERVAL_S,
    FRESH_MAX_S,
    FetchFailureRateGate,
    RangeLocation,
    WeatherRejected,
    WeatherStore,
    compressibility_factor,
    moist_air_density,
    parse_weather_fetch_failure,
    parse_weather_report,
    saturation_vapour_pressure_pa,
    water_vapour_mole_fraction,
)

# Real UTC on the phone, and a Pi clock 5 days behind it (as on 2026-10-25).
PHONE_EPOCH = 1_793_007_900.0
PI_EPOCH = PHONE_EPOCH - 5 * 86400

# Davis, "Equation for the determination of the density of moist air (1981/91)",
# Metrologia 29 (1992) 67-70, Table 2 (1981/91 column): p/Pa, t/°C, h, ρ/(kg/m³), p_sv/Pa, Z.
# x_v and Z are unchanged in CIPM-2007; its leading constant is 3.483740 instead of
# 3.48349 (Picard et al., Metrologia 45 (2008) 149, section 3: +72e-6 relative).
DAVIS_1992_TABLE_2 = [
    (100000.0, 20.0, 0.50, 1.183472, 2339.2, 0.999619),
    (110000.0, 20.0, 0.10, 1.306582, 2339.2, 0.999608),
    (100000.0, 15.0, 0.90, 1.202408, 1705.7, 0.999555),
    (60000.0, 25.0, 0.50, 0.694162, 3169.8, 0.999769),
]
CIPM_2007_OVER_81_91 = 3.483740 / 3.48349


class TestCipm2007:
    @pytest.mark.parametrize(("p", "t", "h", "rho_81_91", "psv", "z"), DAVIS_1992_TABLE_2)
    def test_matches_the_published_examples(self, p, t, h, rho_81_91, psv, z):
        assert saturation_vapour_pressure_pa(t) == pytest.approx(psv, abs=0.06)
        x_v = water_vapour_mole_fraction(t, p, h)
        assert compressibility_factor(t, p, x_v) == pytest.approx(z, abs=1e-6)
        expected = rho_81_91 * CIPM_2007_OVER_81_91
        assert moist_air_density(t, p / 100.0, h * 100.0) == pytest.approx(expected, rel=2e-6)

    def test_dry_isa_sea_level(self):
        # ICAO standard atmosphere: 15 °C, 1013.25 hPa, dry -> 1.2250 kg/m³ (an ideal gas
        # with M = 28.9644 g/mol); CIPM-2007's real-gas Z and argon value add ~0.04 %.
        assert moist_air_density(15.0, 1013.25, 0.0) == pytest.approx(1.2250, rel=6e-4)

    def test_trackman_flat_density(self):
        # 25 °C, 1013.25 hPa, dry is the 1.184 the flat validation runs at.
        assert moist_air_density(25.0, 1013.25, 0.0) == pytest.approx(
            TRACKMAN_FLAT_AIR_DENSITY, abs=1e-3
        )

    def test_humid_air_is_lighter(self):
        assert moist_air_density(20.0, 1013.25, 100.0) < moist_air_density(20.0, 1013.25, 0.0)


def _clock(epoch_s=PHONE_EPOCH, client="phone"):
    return parse_client_clock(
        {"epoch_ms": epoch_s * 1000, "tz_offset_min": 60},
        client=client,
        server_epoch=PI_EPOCH,
        server_tz_offset_min=120,
    )


LOCATION = RangeLocation(name="Golfpark", latitude=48.1, longitude=11.5)


def _open_meteo(**overrides):
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
    }
    payload.update(overrides)
    return payload


def _parse(payload, clock=None, location=LOCATION, received=100.0):
    return parse_weather_report(
        payload,
        clock=clock if clock is not None else _clock(),
        location=location,
        received_monotonic=received,
    )


class TestReportValidation:
    def test_open_meteo_report(self):
        reading = _parse(_open_meteo())
        assert reading.source == "open-meteo"
        assert reading.air_density_kg_m3 == pytest.approx(moist_air_density(9.9, 960.2, 65))
        assert reading.age_at_receipt_s == pytest.approx(600)
        assert reading.wind_speed_ms == 3.28
        log = reading.to_log()
        assert log["clock_offset_s"] == pytest.approx(5 * 86400)
        # The observation on the Pi's (wrong) clock, where session `ts` values live.
        assert log["observed_pi_epoch_s"] == pytest.approx(PI_EPOCH - 600)
        assert log["location"]["name"] == "Golfpark"

    def test_manual_entry_without_humidity_assumes_the_middle(self):
        reading = _parse({"source": "manual", "temperature_c": 12, "pressure_hpa": 1008})
        assert reading.humidity_pct == ASSUMED_HUMIDITY_PCT
        assert reading.humidity_assumed is True
        assert reading.age_at_receipt_s == 0
        assert reading.wind_speed_ms is None
        assert reading.location is None

    def test_manual_entry_needs_no_location(self):
        reading = _parse(
            {"source": "manual", "temperature_c": 12, "pressure_hpa": 1008, "humidity_pct": 80},
            location=None,
        )
        assert reading.humidity_assumed is False

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"source": "guess"}, "malformed"),
            ({"temperature_c": "warm"}, "out_of_range"),
            ({"temperature_c": True}, "out_of_range"),
            ({"temperature_c": float("nan")}, "out_of_range"),
            ({"temperature_c": 61}, "out_of_range"),
            ({"temperature_c": -31}, "out_of_range"),
            ({"pressure_hpa": 1013.25 * 2}, "out_of_range"),
            ({"pressure_hpa": 599}, "out_of_range"),
            ({"humidity_pct": 101}, "out_of_range"),
            ({"humidity_pct": None}, "out_of_range"),
            ({"wind_speed_ms": -1}, "out_of_range"),
            ({"wind_direction_deg": 361}, "out_of_range"),
            ({"observed_epoch_s": None}, "malformed"),
            ({"observed_epoch_s": PHONE_EPOCH + 3600}, "future"),
            ({"observed_epoch_s": PHONE_EPOCH - FRESH_MAX_S - 1}, "stale"),
            ({"latitude": 48.2}, "location_mismatch"),
            ({"longitude": None}, "location_mismatch"),
            # 50 °C at 600 hPa is ρ ≈ 0.64: outside the accepted 0.9-1.4 kg/m³.
            ({"temperature_c": 50, "pressure_hpa": 600}, "out_of_range"),
        ],
    )
    def test_rejected(self, overrides, code):
        with pytest.raises(WeatherRejected) as error:
            _parse(_open_meteo(**overrides))
        assert error.value.code == code

    def test_open_meteo_needs_a_stored_location(self):
        with pytest.raises(WeatherRejected) as error:
            _parse(_open_meteo(), location=None)
        assert error.value.code == "no_location"

    def test_malformed_clock_is_rejected(self):
        with pytest.raises(WeatherRejected) as error:
            parse_weather_report(_open_meteo(), clock=None, location=LOCATION, received_monotonic=0)
        assert error.value.code == "malformed"

    def test_not_a_dict(self):
        with pytest.raises(WeatherRejected):
            _parse(["source", "manual"])

    def test_interval_start_slightly_after_the_phone_clock_is_accepted(self):
        reading = _parse(_open_meteo(observed_epoch_s=PHONE_EPOCH + 120))
        assert reading.age_at_receipt_s == 0


class TestPressureSource:
    """The iOS companion's barometer may replace Open-Meteo's surface pressure (log + label)."""

    @staticmethod
    def _barometer(**overrides):
        payload = _open_meteo(
            pressure_hpa=957.4,
            pressure_source="phone-barometer",
            transport="cellular",
            open_meteo_pressure_hpa=960.2,
            phone_pressure_age_s=42.0,
        )
        payload.update(overrides)
        return payload

    def test_an_older_page_reports_open_meteo_pressure(self):
        reading = _parse(_open_meteo())
        assert reading.pressure_source == "open-meteo"
        assert reading.transport is None
        log = reading.to_log()
        assert log["pressure_source"] == "open-meteo"
        assert log["open_meteo_pressure_hpa"] is None

    def test_barometer_pressure_flies_and_both_pressures_are_logged(self):
        reading = _parse(self._barometer())
        assert reading.pressure_hpa == 957.4
        assert reading.air_density_kg_m3 == pytest.approx(moist_air_density(9.9, 957.4, 65))
        log = reading.to_log()
        assert log["pressure_source"] == "phone-barometer"
        assert log["transport"] == "cellular"
        assert log["open_meteo_pressure_hpa"] == 960.2
        assert log["phone_pressure_age_s"] == 42.0

    def test_transport_alone_is_kept(self):
        reading = _parse(_open_meteo(transport="browser"))
        assert (reading.pressure_source, reading.transport) == ("open-meteo", "browser")

    def test_manual_entry_has_no_pressure_source(self):
        reading = _parse(
            {"source": "manual", "temperature_c": 12, "pressure_hpa": 1008, "transport": "x"}
        )
        assert reading.pressure_source is None
        assert reading.transport is None

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"pressure_source": "guess"}, "malformed"),
            ({"transport": "carrier-pigeon"}, "malformed"),
            ({"phone_pressure_age_s": None}, "malformed"),
            ({"phone_pressure_age_s": -1}, "malformed"),
            ({"phone_pressure_age_s": True}, "malformed"),
            ({"phone_pressure_age_s": BAROMETER_FRESH_MAX_S + 1}, "stale"),
            ({"open_meteo_pressure_hpa": None}, "out_of_range"),
            ({"pressure_hpa": 580}, "out_of_range"),
        ],
    )
    def test_rejected(self, overrides, code):
        with pytest.raises(WeatherRejected) as error:
            _parse(self._barometer(**overrides))
        assert error.value.code == code

    def test_barometer_sample_at_the_limit_is_accepted(self):
        reading = _parse(self._barometer(phone_pressure_age_s=BAROMETER_FRESH_MAX_S))
        assert reading.phone_pressure_age_s == BAROMETER_FRESH_MAX_S

    def test_snapshot_labels_the_reading(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        store.set_location(LOCATION.to_dict())
        store.record(_parse(self._barometer()))
        reading = store.snapshot(1.184)["reading"]
        assert reading["pressure_source"] == "phone-barometer"
        assert reading["transport"] == "cellular"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(name="store_path")
def fixture_store_path(tmp_path):
    return tmp_path / "weather.json"


class TestStore:
    def test_without_weather_the_fallback_flies(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        choice = store.density(1.184)
        assert (choice.kg_m3, choice.source, choice.weather_age_s) == (1.184, "standard", None)
        assert store.fetch_due() is False  # no location yet

    def test_fresh_reading_flies_until_three_hours(self, store_path):
        clock = FakeClock()
        store = WeatherStore(store_path, clock=clock)
        store.set_location(LOCATION.to_dict())
        reading = _parse(_open_meteo(), received=clock.now)
        store.record(reading)

        choice = store.density(1.184)
        assert choice.source == "open-meteo"
        assert choice.kg_m3 == pytest.approx(reading.air_density_kg_m3)
        assert choice.weather_age_s == pytest.approx(600)

        clock.now += FRESH_MAX_S - 600
        assert store.density(1.184).source == "open-meteo"
        clock.now += 1
        assert store.density(1.184).source == "standard"
        assert store.density(1.184).kg_m3 == 1.184

    def test_fetch_due_after_thirty_minutes(self, store_path):
        clock = FakeClock()
        store = WeatherStore(store_path, clock=clock)
        store.set_location(LOCATION.to_dict())
        assert store.fetch_due() is True
        store.record(_parse(_open_meteo(observed_epoch_s=PHONE_EPOCH), received=clock.now))
        assert store.fetch_due() is False
        clock.now += FETCH_INTERVAL_S + 1
        assert store.fetch_due() is True

    def test_manual_entry_flies_but_keeps_the_fetch_due(self, store_path):
        clock = FakeClock()
        store = WeatherStore(store_path, clock=clock)
        store.set_location(LOCATION.to_dict())
        store.record(_parse({"source": "manual", "temperature_c": 12, "pressure_hpa": 1008}))
        assert store.density(1.184).source == "manual"
        assert store.fetch_due() is True

    def test_snapshot(self, store_path):
        clock = FakeClock()
        store = WeatherStore(store_path, clock=clock)
        store.set_location(LOCATION.to_dict())
        store.record(_parse(_open_meteo(), received=clock.now))
        clock.now += 60
        snapshot = store.snapshot(1.184)
        assert snapshot["location"]["name"] == "Golfpark"
        assert snapshot["reading"]["age_s"] == pytest.approx(660)
        assert snapshot["density_source"] == "open-meteo"
        assert snapshot["standard_density_kg_m3"] == 1.184
        assert snapshot["fetch_due"] is False
        assert snapshot["fresh_max_s"] == FRESH_MAX_S

    def test_location_persists_but_weather_does_not(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        assert store.set_location(
            {"name": " Golfpark ", "latitude": 48.123456, "longitude": 11.5, "country": "DE"}
        )
        store.record(
            _parse(_open_meteo(latitude=48.12346, longitude=11.5), location=store.location)
        )
        assert json.loads(store_path.read_text())["location"]["name"] == "Golfpark"

        reloaded = WeatherStore(store_path, clock=FakeClock())
        assert reloaded.location == RangeLocation(
            name="Golfpark", latitude=48.12346, longitude=11.5, country="DE"
        )
        # Its age cannot be trusted across a restart (the Pi clock may have jumped).
        assert reloaded.reading is None
        assert reloaded.density(1.184).source == "standard"

    def test_coordinates_only_location_gets_a_name(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        location = store.set_location({"latitude": 48.1, "longitude": 11.5})
        assert location.name == "48.1000, 11.5000"

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            {"latitude": 91, "longitude": 0},
            {"latitude": 0, "longitude": 181},
            {"latitude": "48", "longitude": 11},
            {"latitude": True, "longitude": 11},
        ],
    )
    def test_bad_location_is_rejected_and_nothing_changes(self, store_path, raw):
        store = WeatherStore(store_path, clock=FakeClock())
        store.set_location(LOCATION.to_dict())
        assert store.set_location(raw) is None
        assert store.location == LOCATION

    def test_new_location_drops_the_old_open_meteo_reading_only(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        store.set_location(LOCATION.to_dict())
        store.record(_parse(_open_meteo()))
        store.set_location({"name": "Other", "latitude": 50.0, "longitude": 8.0})
        assert store.reading is None

        store.record(_parse({"source": "manual", "temperature_c": 12, "pressure_hpa": 1008}))
        store.set_location({"name": "Third", "latitude": 51.0, "longitude": 7.0})
        assert store.reading is not None

    def test_same_location_keeps_the_reading(self, store_path):
        store = WeatherStore(store_path, clock=FakeClock())
        store.set_location(LOCATION.to_dict())
        store.record(_parse(_open_meteo()))
        store.set_location(LOCATION.to_dict())
        assert store.reading is not None

    def test_unreadable_file_starts_empty(self, store_path):
        store_path.write_text("{not json")
        assert WeatherStore(store_path, clock=FakeClock()).location is None

    def test_failed_write_keeps_the_old_location(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        store = WeatherStore(blocker / "weather.json", clock=FakeClock())
        assert store.set_location(LOCATION.to_dict()) is None
        assert store.location is None

    def test_env_var_picks_the_path(self, tmp_path, monkeypatch):
        path = tmp_path / "env-weather.json"
        monkeypatch.setenv("OPENFLIGHT_WEATHER_PATH", str(path))
        WeatherStore(clock=FakeClock()).set_location(LOCATION.to_dict())
        assert path.exists()


def _failure(**overrides):
    payload = {"stage": "fetch", "reason": "offline", "consecutive_failures": 3, "next_retry_s": 60}
    payload.update(overrides)
    return payload


class TestFetchFailure:
    @pytest.mark.parametrize("stage", ["location", "fetch", "send"])
    @pytest.mark.parametrize(
        "reason",
        [
            "offline",
            "timeout",
            "invalid_response",
            "rejected",
            "no_permission",
            "no_fix",
            "save_failed",
            "unknown",
            "http_503",
            "http_429",
        ],
    )
    def test_closed_sets_are_accepted(self, stage, reason):
        assert parse_weather_fetch_failure(_failure(stage=stage, reason=reason)) == {
            "stage": stage,
            "reason": reason,
            "consecutive_failures": 3,
            "next_retry_s": 60,
        }

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            "offline",
            ["fetch", "offline"],
            _failure(stage="weather"),
            _failure(stage=None),
            _failure(stage=["fetch"]),
            _failure(reason="Open-Meteo HTTP 503 via cellular"),
            _failure(reason="https://api.open-meteo.com/v1/forecast?latitude=48.1"),
            _failure(reason="http_50"),
            _failure(reason="http_5030"),
            _failure(reason="http_abc"),
            _failure(reason="http_\u0665\u0660\u0663"),
            _failure(reason="OFFLINE"),
            _failure(reason=None),
            _failure(consecutive_failures="3"),
            _failure(consecutive_failures=True),
            _failure(consecutive_failures=1.5),
            _failure(consecutive_failures=float("nan")),
            _failure(next_retry_s=float("inf")),
            _failure(next_retry_s=None),
            {"stage": "fetch", "reason": "offline", "consecutive_failures": 1},
        ],
    )
    def test_anything_else_is_malformed(self, payload):
        assert parse_weather_fetch_failure(payload) is None

    def test_counts_are_clamped_and_extra_fields_dropped(self):
        entry = parse_weather_fetch_failure(
            _failure(
                consecutive_failures=-4,
                next_retry_s=10**9,
                latitude=48.1,
                longitude=11.5,
                message="Open-Meteo HTTP 503",
                url="https://api.open-meteo.com/",
            )
        )
        assert entry == {
            "stage": "fetch",
            "reason": "offline",
            "consecutive_failures": 0,
            "next_retry_s": 100_000,
        }
        assert parse_weather_fetch_failure(_failure(next_retry_s=15.0))["next_retry_s"] == 15
        assert isinstance(
            parse_weather_fetch_failure(_failure(next_retry_s=15.0))["next_retry_s"], int
        )

    def test_reasons_match_the_phones_closed_set(self):
        # A reason only the phone knows would be dropped here as malformed.
        source = (
            Path(__file__).resolve().parents[1] / "ui" / "src" / "services" / "weatherFailure.ts"
        ).read_text(encoding="utf-8")
        union = re.search(r"export\s+type\s+WeatherFailureReason\s*=([^;]*);", source)
        assert union is not None
        literals = re.findall(r"'([^']*)'|\"([^\"]*)\"|`([^`]*)`", union.group(1))
        names = {plain or double for plain, double, template in literals if not template}
        templates = [template for _, _, template in literals if template]
        assert names == set(FETCH_FAILURE_REASONS)
        assert templates == ["http_${number}"]
        assert parse_weather_fetch_failure(_failure(reason="http_503")) is not None

    def test_rate_gate_admits_one_report_per_interval(self):
        clock = FakeClock()
        gate = FetchFailureRateGate(clock=clock)
        assert gate.admit() is True
        clock.now += FETCH_FAILURE_MIN_INTERVAL_S - 0.01
        assert gate.admit() is False
        clock.now += 0.01
        assert gate.admit() is True
        # A refused report does not restart the interval.
        clock.now += 1.0
        assert gate.admit() is False
        clock.now += FETCH_FAILURE_MIN_INTERVAL_S - 1.0
        assert gate.admit() is True
