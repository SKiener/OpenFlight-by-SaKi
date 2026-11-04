"""Air density for the ballistic carry from the weather at the range.

At the range the Pi runs its own Wi-Fi hotspot: it has no internet and its clock can be
days off. The paired phone fetches the current weather (Open-Meteo) for the range location
stored here, or the user types temperature and pressure, and sends it over the socket.
The server turns it into a moist-air density and flies the live ballistics with it while
the reading is fresh; otherwise the fixed fallback density (``--air-density``, by default
TrackMan's 1.184 kg/m³) stays in use. Wind is logged only; the flight model has no wind.

Timekeeping never relies on the Pi's wall clock. A reading's age at arrival is the phone's
own clock minus the observation time (both real UTC); after that it ages on the Pi's
monotonic clock. A reading from an earlier server run therefore has no trustworthy age and
is not kept: only the range location is persisted.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from .client_clock import ClientClockReading

logger = logging.getLogger(__name__)

DEFAULT_WEATHER_PATH = Path.home() / ".config" / "openflight" / "weather.json"
WEATHER_PATH_ENV = "OPENFLIGHT_WEATHER_PATH"

SOURCE_OPEN_METEO = "open-meteo"
SOURCE_MANUAL = "manual"
SOURCE_STANDARD = "standard"
WEATHER_SOURCES = (SOURCE_OPEN_METEO, SOURCE_MANUAL)
# Where an Open-Meteo report's pressure came from: Open-Meteo's surface pressure, or the
# station pressure the iOS companion's barometer measured at the phone (design §7.4, D3).
PRESSURE_SOURCE_OPEN_METEO = "open-meteo"
PRESSURE_SOURCE_PHONE_BAROMETER = "phone-barometer"
PRESSURE_SOURCES = (PRESSURE_SOURCE_OPEN_METEO, PRESSURE_SOURCE_PHONE_BAROMETER)
# How the phone reached Open-Meteo: the companion's cellular path, its default route,
# or the page's own browser fetch.
WEATHER_TRANSPORTS = ("cellular", "default", "browser")
# The web page uses the barometer only while its sample is at most this old.
BAROMETER_FRESH_MAX_S = 10 * 60.0

# Decided with the feature (not fitted to any shot data): a reading flies the ballistics
# for 3 h; the phone refreshes it once it is older than 30 min. Without an Open-Meteo
# reading (none yet, or only a manual entry) the phone keeps trying after each failure:
# 15 s after the first, doubling up to 5 min (weatherRetryPauseMs, ui/src/services/weatherSync.ts),
# each pause counted from the start of the try. After a success it waits at least 60 s, even
# while ``fetch_due`` stays true.
FRESH_MAX_S = 3 * 3600.0
FETCH_INTERVAL_S = 30 * 60.0
# Open-Meteo's `current.time` is the start of its 15-min interval (`current.interval` =
# 900 s), so a reading can carry a time up to that much before or after the phone's clock.
FUTURE_TOLERANCE_S = 15 * 60.0

# Plausibility guards: physically possible weather at a driving range, not tuned values.
# The pressure range is the stated validity range of CIPM-2007 (Picard et al. 2008, A.3).
TEMPERATURE_RANGE_C = (-30.0, 50.0)
PRESSURE_RANGE_HPA = (600.0, 1100.0)
HUMIDITY_RANGE_PCT = (0.0, 100.0)
WIND_SPEED_RANGE_MS = (0.0, 75.0)
WIND_DIRECTION_RANGE_DEG = (0.0, 360.0)
# Accepted air density, shared with --air-density (server.py).
AIR_DENSITY_RANGE_KG_M3 = (0.9, 1.4)
# A manual entry without humidity assumes the middle of 0-100 %: it bounds the error at
# about ±0.3 % of ρ at 15 °C (CIPM-2007 sensitivity -9e-3 per unit h, Picard 2008 eq. 6).
ASSUMED_HUMIDITY_PCT = 50.0
# CIPM-2007's reference CO2 mole fraction (Picard 2008, section 2.1); not measured here.
CO2_MOLE_FRACTION = 0.0004
MAX_LOCATION_NAME_LENGTH = 80
# The phone echoes the coordinates it asked Open-Meteo for; they must be the stored ones.
LOCATION_MATCH_DEG = 1e-4

_ZERO_CELSIUS_K = 273.15


def saturation_vapour_pressure_pa(temperature_c: float) -> float:
    """CIPM-2007 eq. (A1.1): p_sv = 1 Pa · exp(A·T² + B·T + C + D/T), T in K."""
    temperature_k = temperature_c + _ZERO_CELSIUS_K
    return math.exp(
        1.2378847e-5 * temperature_k**2
        - 1.9121316e-2 * temperature_k
        + 33.93711047
        - 6.3431645e3 / temperature_k
    )


def enhancement_factor(pressure_pa: float, temperature_c: float) -> float:
    """CIPM-2007 eq. (A1.2): f = α + β·p + γ·t², t in °C."""
    return 1.00062 + 3.14e-8 * pressure_pa + 5.6e-7 * temperature_c**2


def water_vapour_mole_fraction(
    temperature_c: float, pressure_pa: float, relative_humidity: float
) -> float:
    """CIPM-2007 eq. (A1.3): x_v = h · f(p, t) · p_sv(t) / p, with 0 <= h <= 1."""
    return (
        relative_humidity
        * enhancement_factor(pressure_pa, temperature_c)
        * saturation_vapour_pressure_pa(temperature_c)
        / pressure_pa
    )


def compressibility_factor(temperature_c: float, pressure_pa: float, x_v: float) -> float:
    """CIPM-2007 eq. (A1.4), the compressibility factor Z of moist air."""
    t = temperature_c
    temperature_k = t + _ZERO_CELSIUS_K
    p_over_t = pressure_pa / temperature_k
    return (
        1.0
        - p_over_t
        * (
            1.58123e-6
            - 2.9331e-8 * t
            + 1.1043e-10 * t * t
            + (5.707e-6 - 2.051e-8 * t) * x_v
            + (1.9898e-4 - 2.376e-6 * t) * x_v * x_v
        )
        + p_over_t * p_over_t * (1.83e-11 - 0.765e-8 * x_v * x_v)
    )


def moist_air_density(
    temperature_c: float,
    pressure_hpa: float,
    humidity_pct: float,
    co2_mole_fraction: float = CO2_MOLE_FRACTION,
) -> float:
    """Density of moist air in kg/m³, CIPM-2007 equation of state (eq. 5a).

    A. Picard, R. S. Davis, M. Gläser, K. Fujii, "Revised formula for the density of
    moist air (CIPM-2007)", Metrologia 45 (2008) 149-155, doi:10.1088/0026-1394/45/2/004:
    ρ = p·M_a / (Z·R·T) · [1 − x_v·(1 − M_v/M_a)], with
    M_a = [28.96546 + 12.011·(x_CO2 − 0.0004)] g/mol, M_v = 18.01528 g/mol,
    R = 8.314472 J/(mol·K). The paper states it for 600-1100 hPa and 15-27 °C; outside
    that temperature band it is an extrapolation of an ideal-gas law with small virial
    corrections, still far more accurate than the weather input itself.
    """
    pressure_pa = pressure_hpa * 100.0
    temperature_k = temperature_c + _ZERO_CELSIUS_K
    x_v = water_vapour_mole_fraction(temperature_c, pressure_pa, humidity_pct / 100.0)
    z = compressibility_factor(temperature_c, pressure_pa, x_v)
    molar_mass_air = (28.96546 + 12.011 * (co2_mole_fraction - 0.0004)) * 1e-3
    molar_mass_water = 18.01528e-3
    gas_constant = 8.314472
    return (
        pressure_pa
        * molar_mass_air
        / (z * gas_constant * temperature_k)
        * (1.0 - x_v * (1.0 - molar_mass_water / molar_mass_air))
    )


def _number(value: Any) -> Optional[float]:
    """A finite int or float (never a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _in_range(value: Optional[float], bounds: Tuple[float, float]) -> bool:
    return value is not None and bounds[0] <= value <= bounds[1]


def _clean_text(value: Any, limit: int) -> str:
    return str(value).strip()[:limit] if isinstance(value, str) else ""


@dataclass(frozen=True)
class RangeLocation:
    """Where the range is: what the user searched for (or typed) once, stored on the Pi."""

    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Wire, disk and log representation."""
        return {
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "country": self.country,
            "admin1": self.admin1,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["RangeLocation"]:
        """Parse a socket payload or stored record; None when unusable."""
        if not isinstance(raw, dict):
            return None
        latitude = _number(raw.get("latitude"))
        longitude = _number(raw.get("longitude"))
        if not _in_range(latitude, (-90.0, 90.0)) or not _in_range(longitude, (-180.0, 180.0)):
            return None
        name = _clean_text(raw.get("name"), MAX_LOCATION_NAME_LENGTH)
        if not name:
            name = f"{latitude:.4f}, {longitude:.4f}"
        return cls(
            name=name,
            latitude=round(latitude, 5),
            longitude=round(longitude, 5),
            country=_clean_text(raw.get("country"), MAX_LOCATION_NAME_LENGTH),
            admin1=_clean_text(raw.get("admin1"), MAX_LOCATION_NAME_LENGTH),
        )

    def matches(self, latitude: Optional[float], longitude: Optional[float]) -> bool:
        """Whether a report's echoed coordinates are this location's."""
        return (
            latitude is not None
            and longitude is not None
            and abs(latitude - self.latitude) <= LOCATION_MATCH_DEG
            and abs(longitude - self.longitude) <= LOCATION_MATCH_DEG
        )


@dataclass(frozen=True)
class WeatherReading:
    """One accepted weather update and the air density computed from it."""

    source: str
    temperature_c: float
    pressure_hpa: float
    humidity_pct: float
    humidity_assumed: bool
    wind_speed_ms: Optional[float]
    wind_direction_deg: Optional[float]
    air_density_kg_m3: float
    observed_epoch_s: float
    age_at_receipt_s: float
    clock: ClientClockReading
    location: Optional[RangeLocation]
    received_monotonic: float = field(compare=False)
    pressure_source: Optional[str] = None
    transport: Optional[str] = None
    open_meteo_pressure_hpa: Optional[float] = None
    phone_pressure_age_s: Optional[float] = None

    def age_s(self, now_monotonic: float) -> float:
        """Observation age now: age on arrival plus Pi-monotonic time since then."""
        return self.age_at_receipt_s + max(0.0, now_monotonic - self.received_monotonic)

    def to_log(self) -> Dict[str, Any]:
        """Fields of the session-log ``weather`` event."""
        return {
            "source": self.source,
            "temperature_c": self.temperature_c,
            "pressure_hpa": self.pressure_hpa,
            "humidity_pct": self.humidity_pct,
            "humidity_assumed": self.humidity_assumed,
            "wind_speed_ms": self.wind_speed_ms,
            "wind_direction_deg": self.wind_direction_deg,
            "air_density_kg_m3": round(self.air_density_kg_m3, 4),
            "observed_epoch_s": self.observed_epoch_s,
            # The observation on the Pi's clock, the time base of the session's `ts`.
            "observed_pi_epoch_s": round(self.observed_epoch_s - self.clock.offset_s, 3),
            "age_at_receipt_s": round(self.age_at_receipt_s, 1),
            "client": self.clock.client,
            "client_epoch_ms": self.clock.client_epoch_ms,
            "client_tz_offset_min": self.clock.client_tz_offset_min,
            "server_epoch": self.clock.server_epoch,
            "clock_offset_s": self.clock.offset_s,
            "location": self.location.to_dict() if self.location else None,
            # Open-Meteo only (else null): where pressure_hpa came from and how it was fetched.
            "pressure_source": self.pressure_source,
            "transport": self.transport,
            "open_meteo_pressure_hpa": self.open_meteo_pressure_hpa,
            "phone_pressure_age_s": self.phone_pressure_age_s,
        }


@dataclass(frozen=True)
class AirDensityChoice:
    """The density a shot flies with, and where it came from."""

    kg_m3: float
    source: str  # "open-meteo", "manual" or "standard"
    weather_age_s: Optional[float]


class WeatherRejected(ValueError):
    """A weather report failed validation; ``code`` says why (sent back to the UI)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _field_in_range(data: dict, key: str, bounds: Tuple[float, float]) -> float:
    """The payload's number at ``key``; out_of_range unless it is a finite number in bounds."""
    value = _number(data.get(key))
    if not _in_range(value, bounds):
        raise WeatherRejected("out_of_range", f"{key} {data.get(key)!r}")
    return value


def _pressure_origin(
    data: dict,
) -> Tuple[str, Optional[str], Optional[float], Optional[float]]:
    """``(pressure_source, transport, open_meteo_pressure_hpa, phone_pressure_age_s)``.

    Missing fields mean an Open-Meteo pressure fetched by the page itself: Safari's page
    fetch (no companion; its report carries no ``transport``) or an older page. ``browser``
    is the companion page's own fetch after the native one failed or was unavailable. A barometer
    pressure must say how old its sample was; one older than BAROMETER_FRESH_MAX_S is
    rejected (the page falls back to Open-Meteo's pressure before that).
    """
    pressure_source = data.get("pressure_source", PRESSURE_SOURCE_OPEN_METEO)
    if pressure_source not in PRESSURE_SOURCES:
        raise WeatherRejected("malformed", f"unknown pressure_source {pressure_source!r}")
    transport = data.get("transport")
    if transport is not None and transport not in WEATHER_TRANSPORTS:
        raise WeatherRejected("malformed", f"unknown transport {transport!r}")
    if pressure_source == PRESSURE_SOURCE_OPEN_METEO:
        return pressure_source, transport, None, None
    open_meteo_pressure = _field_in_range(data, "open_meteo_pressure_hpa", PRESSURE_RANGE_HPA)
    age = _number(data.get("phone_pressure_age_s"))
    if age is None or age < 0:
        raise WeatherRejected("malformed", "phone_pressure_age_s")
    if age > BAROMETER_FRESH_MAX_S:
        raise WeatherRejected("stale", f"barometer sample {age:.0f} s old")
    return pressure_source, transport, round(open_meteo_pressure, 2), round(age, 1)


def parse_weather_report(
    data: Any,
    *,
    clock: Optional[ClientClockReading],
    location: Optional[RangeLocation],
    received_monotonic: float,
) -> WeatherReading:
    """Validate a ``weather_report`` payload into a reading; raises WeatherRejected.

    ``clock`` is the payload's ``epoch_ms``/``tz_offset_min`` parsed by the existing
    client_clock handling (None when they are malformed).
    """
    if not isinstance(data, dict) or clock is None:
        raise WeatherRejected("malformed")
    source = data.get("source")
    if source not in WEATHER_SOURCES:
        raise WeatherRejected("malformed", f"unknown source {source!r}")

    temperature = _field_in_range(data, "temperature_c", TEMPERATURE_RANGE_C)
    pressure = _field_in_range(data, "pressure_hpa", PRESSURE_RANGE_HPA)
    humidity_assumed = data.get("humidity_pct") is None and source == SOURCE_MANUAL
    humidity = (
        ASSUMED_HUMIDITY_PCT
        if humidity_assumed
        else _field_in_range(data, "humidity_pct", HUMIDITY_RANGE_PCT)
    )

    wind_speed = wind_direction = None
    pressure_origin: Tuple[Optional[str], Optional[str], Optional[float], Optional[float]] = (
        None,
        None,
        None,
        None,
    )
    client_epoch_s = clock.client_epoch_ms / 1000.0
    if source == SOURCE_OPEN_METEO:
        if location is None:
            raise WeatherRejected("no_location")
        if not location.matches(_number(data.get("latitude")), _number(data.get("longitude"))):
            raise WeatherRejected("location_mismatch")
        wind_speed = _field_in_range(data, "wind_speed_ms", WIND_SPEED_RANGE_MS)
        wind_direction = _field_in_range(data, "wind_direction_deg", WIND_DIRECTION_RANGE_DEG)
        observed = _number(data.get("observed_epoch_s"))
        if observed is None or observed <= 0:
            raise WeatherRejected("malformed", "observed_epoch_s")
        pressure_origin = _pressure_origin(data)
    else:
        # Typed in now: observed when the phone (or kiosk) sent it.
        observed = client_epoch_s

    age_at_receipt = client_epoch_s - observed
    if age_at_receipt < -FUTURE_TOLERANCE_S:
        raise WeatherRejected("future")
    if age_at_receipt > FRESH_MAX_S:
        raise WeatherRejected("stale")

    density = moist_air_density(temperature, pressure, humidity)
    if not _in_range(density, AIR_DENSITY_RANGE_KG_M3):
        raise WeatherRejected("out_of_range", f"air density {density:.3f} kg/m³")

    return WeatherReading(
        source=source,
        temperature_c=round(temperature, 2),
        pressure_hpa=round(pressure, 2),
        humidity_pct=round(humidity, 1),
        humidity_assumed=humidity_assumed,
        wind_speed_ms=wind_speed,
        wind_direction_deg=wind_direction,
        air_density_kg_m3=density,
        observed_epoch_s=observed,
        age_at_receipt_s=max(0.0, age_at_receipt),
        clock=clock,
        location=location if source == SOURCE_OPEN_METEO else None,
        received_monotonic=received_monotonic,
        pressure_source=pressure_origin[0],
        transport=pressure_origin[1],
        open_meteo_pressure_hpa=pressure_origin[2],
        phone_pressure_age_s=pressure_origin[3],
    )


# The phone's ``weather_fetch_failed`` report (log only): which step of its weather upkeep
# failed and why, classified on the phone (ui/src/services/weatherFailure.ts). Closed sets
# only, so no error text, URL or coordinates reach the log.
FETCH_FAILURE_STAGES = ("location", "fetch", "send")
FETCH_FAILURE_REASONS = (
    "offline",
    "timeout",
    "invalid_response",
    "rejected",
    "no_permission",
    "no_fix",
    "save_failed",
    "unknown",
)
_HTTP_FAILURE_REASON = re.compile(r"http_[0-9]{3}")
FETCH_FAILURE_COUNT_RANGE = (0, 100_000)
# The phone retries at most every 15 s per count; this only keeps a broken page from
# flooding the log.
FETCH_FAILURE_MIN_INTERVAL_S = 5.0


def parse_weather_fetch_failure(data: Any) -> Optional[Dict[str, Any]]:
    """The ``weather_fetch_failed`` payload as a log entry, or None when it is malformed.

    ``stage`` and ``reason`` must come from the closed sets (``reason`` may also be
    ``http_`` plus a three-digit status); ``consecutive_failures`` and ``next_retry_s`` must be
    integers and are clamped to 0-100000. Every other field is dropped.
    """
    if not isinstance(data, dict):
        return None
    stage, reason = data.get("stage"), data.get("reason")
    if not isinstance(stage, str) or stage not in FETCH_FAILURE_STAGES:
        return None
    if not isinstance(reason, str) or not (
        reason in FETCH_FAILURE_REASONS or _HTTP_FAILURE_REASON.fullmatch(reason)
    ):
        return None
    entry: Dict[str, Any] = {"stage": stage, "reason": reason}
    low, high = FETCH_FAILURE_COUNT_RANGE
    for key in ("consecutive_failures", "next_retry_s"):
        value = data.get(key)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        entry[key] = min(max(value, low), high)
    return entry


class FetchFailureRateGate:  # pylint: disable=too-few-public-methods
    """Admits one ``weather_fetch_failed`` report per ``min_interval_s`` (monotonic clock)."""

    def __init__(
        self,
        min_interval_s: float = FETCH_FAILURE_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._last: Optional[float] = None
        self._lock = threading.Lock()

    def admit(self) -> bool:
        """True, restarting the interval, unless the last admitted report is younger than it."""
        with self._lock:
            now = self._clock()
            if self._last is not None and now - self._last < self._min_interval_s:
                return False
            self._last = now
            return True


def resolve_weather_path(path: Union[str, Path, None] = None) -> Path:
    """Constructor argument, then ``OPENFLIGHT_WEATHER_PATH``, then the user default."""
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = (os.environ.get(WEATHER_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_WEATHER_PATH


class WeatherStore:
    """The stored range location and the latest weather reading of this server run."""

    def __init__(
        self,
        path: Union[str, Path, None] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._path = resolve_weather_path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._location: Optional[RangeLocation] = None
        self._reading: Optional[WeatherReading] = None
        self._load()

    @property
    def location(self) -> Optional[RangeLocation]:
        """The stored range location, or None until the user set one."""
        with self._lock:
            return self._location

    @property
    def reading(self) -> Optional[WeatherReading]:
        """The latest accepted reading, or None."""
        with self._lock:
            return self._reading

    def now(self) -> float:
        """The monotonic time readings age on."""
        return self._clock()

    def set_location(self, raw: Any) -> Optional[RangeLocation]:
        """Validate and persist a new location; None (nothing changed) when rejected.

        A changed location drops the Open-Meteo reading of the old one; a manual entry,
        typed at the range itself, stays.
        """
        location = RangeLocation.from_dict(raw)
        if location is None:
            return None
        with self._lock:
            if not self._write({"location": location.to_dict()}):
                return None
            changed = self._location != location
            self._location = location
            if changed and self._reading is not None and self._reading.source == SOURCE_OPEN_METEO:
                self._reading = None
        return location

    def record(self, reading: WeatherReading) -> None:
        """Make an accepted reading the current one."""
        with self._lock:
            self._reading = reading

    def density(self, fallback_kg_m3: float) -> AirDensityChoice:
        """The fresh reading's density, else the fixed fallback."""
        with self._lock:
            reading = self._reading
        if reading is not None:
            age = reading.age_s(self._clock())
            if age <= FRESH_MAX_S:
                return AirDensityChoice(reading.air_density_kg_m3, reading.source, age)
        return AirDensityChoice(fallback_kg_m3, SOURCE_STANDARD, None)

    def fetch_due(self) -> bool:
        """Whether a phone should fetch: a location is set and no Open-Meteo reading is
        younger than FETCH_INTERVAL_S (a manual entry does not stop the retry)."""
        with self._lock:
            location, reading = self._location, self._reading
        if location is None:
            return False
        if reading is None or reading.source != SOURCE_OPEN_METEO:
            return True
        return reading.age_s(self._clock()) > FETCH_INTERVAL_S

    def snapshot(self, fallback_kg_m3: float) -> Dict[str, Any]:
        """The ``weather_state`` payload for the UI."""
        with self._lock:
            location, reading = self._location, self._reading
        now = self._clock()
        choice = self.density(fallback_kg_m3)
        reading_payload = None
        if reading is not None:
            reading_payload = {
                "source": reading.source,
                "temperature_c": reading.temperature_c,
                "pressure_hpa": reading.pressure_hpa,
                "humidity_pct": reading.humidity_pct,
                "humidity_assumed": reading.humidity_assumed,
                "wind_speed_ms": reading.wind_speed_ms,
                "wind_direction_deg": reading.wind_direction_deg,
                "air_density_kg_m3": round(reading.air_density_kg_m3, 4),
                "age_s": round(reading.age_s(now), 1),
                "pressure_source": reading.pressure_source,
                "transport": reading.transport,
            }
        return {
            "location": location.to_dict() if location else None,
            "reading": reading_payload,
            "density_kg_m3": round(choice.kg_m3, 4),
            "density_source": choice.source,
            "standard_density_kg_m3": round(fallback_kg_m3, 4),
            "fetch_due": self.fetch_due(),
            "fresh_max_s": FRESH_MAX_S,
            "fetch_interval_s": FETCH_INTERVAL_S,
        }

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[weather] could not read %s: %s", self._path, error)
            return
        self._location = RangeLocation.from_dict(
            raw.get("location") if isinstance(raw, dict) else None
        )

    def _write(self, payload: dict) -> bool:
        """Write atomically (temp file + rename). Never raises into a caller."""
        temp_path = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            return True
        except OSError as error:
            logger.error("[weather] could not save %s: %s", self._path, error)
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False
