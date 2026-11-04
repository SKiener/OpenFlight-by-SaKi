import type { RangeLocation, WeatherReport } from '../types/weather';
import { clientClockPayload } from '../utils/clockOffset';
import { finiteNumber, type NativeWeatherReport, type OpenFlightNative, type SensorSnapshot } from './openflightNative';
import { WeatherFailureError, weatherFailureReason } from '../services/weatherFailure';

/** The barometer replaces Open-Meteo's surface pressure only while its sample is at most this old (server: BAROMETER_FRESH_MAX_S). */
export const BAROMETER_FRESH_MAX_S = 10 * 60;
/** How old a GPS fix may be to store it as the range location; the phone asks for a new one otherwise. */
export const GPS_FIX_MAX_AGE_S = 10 * 60;
export const GPS_FIX_TIMEOUT_MS = 15_000;

/** The station pressure the phone's barometer measured, when its sample is fresh enough for the air density. */
export function freshBarometerPressure(
  snapshot: SensorSnapshot | null
): { pressure_hpa: number; age_s: number } | null {
  const barometer = snapshot?.barometer;
  if (!barometer || barometer.authorization !== 'authorized') return null;
  const { pressure_hpa: pressure, age_s: age } = barometer;
  if (!finiteNumber(pressure) || !finiteNumber(age) || age < 0 || age > BAROMETER_FRESH_MAX_S) return null;
  return { pressure_hpa: pressure, age_s: age };
}

/**
 * The native fetch as a `weather_report` for the stored location. The coordinates are the stored
 * ones (the app echoes the request), so the server matches them; throws when a field is missing.
 */
export function weatherReportFromNative(native: NativeWeatherReport, location: RangeLocation): WeatherReport {
  const fields = [
    native.temperature_c,
    native.pressure_hpa,
    native.humidity_pct,
    native.wind_speed_ms,
    native.wind_direction_deg,
    native.observed_epoch_s,
  ];
  if (native.source !== 'open-meteo' || !fields.every(finiteNumber)) {
    throw new WeatherFailureError('invalid_response', 'The companion answered without current weather');
  }
  return {
    source: 'open-meteo',
    temperature_c: native.temperature_c,
    humidity_pct: native.humidity_pct,
    pressure_hpa: native.pressure_hpa,
    wind_speed_ms: native.wind_speed_ms,
    wind_direction_deg: native.wind_direction_deg,
    observed_epoch_s: native.observed_epoch_s,
    latitude: location.latitude,
    longitude: location.longitude,
    transport: native.transport === 'cellular' ? 'cellular' : 'default',
    ...clientClockPayload(),
  };
}

export interface RangeWeatherDeps {
  /** The companion's bridge, or null in Safari (then the page's own fetch, exactly as before). */
  native: OpenFlightNative | null;
  browserFetch: (location: RangeLocation) => Promise<WeatherReport>;
  now?: () => Date;
}

/**
 * The current range weather as a `weather_report`. Inside the companion it prefers the native fetch
 * (cellular) over the page's own and the phone's barometer over Open-Meteo's surface pressure while
 * the barometer sample is fresh; both are labelled for the server (`transport`, `pressure_source`).
 * When both fetches fail it rejects with the native fetch's error: that one went out over cellular,
 * the page's usually only finds the Pi's hotspot without internet. Only a native error without a
 * known reason (`unavailable`, `busy`, `internal`, ...) gives way to the page's error.
 */
export async function fetchRangeWeather(location: RangeLocation, deps: RangeWeatherDeps): Promise<WeatherReport> {
  const { native } = deps;
  if (!native) return deps.browserFetch(location);

  let report: WeatherReport;
  if (native.capabilities.weather) {
    try {
      report = weatherReportFromNative(
        await native.fetchWeather({ latitude: location.latitude, longitude: location.longitude }),
        location
      );
    } catch (nativeError) {
      console.warn('Native weather fetch failed, using the page fetch:', nativeError);
      try {
        report = { ...(await deps.browserFetch(location)), transport: 'browser' };
      } catch (pageError) {
        console.warn('The page weather fetch failed too:', pageError);
        throw weatherFailureReason(nativeError) === 'unknown' ? pageError : nativeError;
      }
    }
  } else {
    report = { ...(await deps.browserFetch(location)), transport: 'browser' };
  }

  if (native.capabilities.barometer) {
    const barometer = freshBarometerPressure(await native.getSensors().catch(() => null));
    if (barometer) {
      report = {
        ...report,
        pressure_hpa: barometer.pressure_hpa,
        pressure_source: 'phone-barometer',
        open_meteo_pressure_hpa: report.pressure_hpa,
        phone_pressure_age_s: barometer.age_s,
      };
    }
  }
  // The awaits above took time: the clock fields must say when the report is sent.
  return { ...report, ...clientClockPayload(deps.now?.()) };
}

/** Location authorizations under which the app gets no fix (design §7.3). */
const NO_LOCATION_PERMISSION: ReadonlySet<string> = new Set(['denied', 'restricted', 'not_determined']);

/**
 * The phone's GPS fix as a range location (coordinates only; the server names it). Without one it
 * rejects with a WeatherFailureError saying why: `no_permission` (location services off, or the app
 * may not locate) or `no_fix` (no fix within the timeout, too old, impossible coordinates).
 */
export async function rangeLocationFromGps(native: OpenFlightNative): Promise<RangeLocation> {
  if (!native.capabilities.location) throw new WeatherFailureError('unknown', 'The companion cannot locate');
  const snapshot = await native.getSensors({ location_max_age_s: GPS_FIX_MAX_AGE_S, timeout_ms: GPS_FIX_TIMEOUT_MS });
  const location = snapshot?.location;
  if (!location) throw new WeatherFailureError('no_permission', 'Location services are off');
  const noFix = () => new WeatherFailureError('no_fix', 'No usable GPS fix');
  const { latitude, longitude } = location;
  if (!finiteNumber(latitude) || !finiteNumber(longitude)) {
    if (NO_LOCATION_PERMISSION.has(location.authorization)) {
      throw new WeatherFailureError('no_permission', 'The app may not locate');
    }
    throw noFix();
  }
  if (Math.abs(latitude) > 90 || Math.abs(longitude) > 180) throw noFix();
  if (finiteNumber(location.age_s) && location.age_s > GPS_FIX_MAX_AGE_S) throw noFix();
  return { name: '', latitude: Number(latitude.toFixed(5)), longitude: Number(longitude.toFixed(5)) };
}
