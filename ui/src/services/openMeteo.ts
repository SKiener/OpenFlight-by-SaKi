import type { RangeLocation, WeatherReport } from '../types/weather';
import { clientClockPayload } from '../utils/clockOffset';
import { httpFailureReason, WeatherFailureError } from './weatherFailure';

/**
 * Open-Meteo (open-meteo.com): free, no key, CORS open. Only the paired phone calls it; the
 * Pi's hotspot has no internet, so the request only gets out when iOS sends it over cellular.
 */
export const GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search';
export const FORECAST_URL = 'https://api.open-meteo.com/v1/forecast';
export const OPEN_METEO_TIMEOUT_MS = 10_000;
const CURRENT_FIELDS = 'temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m';

type FetchLike = (
  url: string,
  init?: { signal?: AbortSignal }
) => Promise<{ ok: boolean; status?: number; json: () => Promise<unknown> }>;

/** Throws a WeatherFailureError that says why (weather_fetch_failed), with the original error as its cause. */
async function getJson(url: string, fetcher: FetchLike): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), OPEN_METEO_TIMEOUT_MS);
  const timedOut = () => controller.signal.aborted;
  try {
    let response: Awaited<ReturnType<FetchLike>>;
    try {
      response = await fetcher(url, { signal: controller.signal });
    } catch (cause) {
      // fetch() rejects with a TypeError when there is no route (the Pi's hotspot has no internet).
      throw new WeatherFailureError(timedOut() ? 'timeout' : 'offline', 'Open-Meteo is unreachable', { cause });
    }
    if (!response.ok)
      throw new WeatherFailureError(httpFailureReason(response.status), 'Open-Meteo answered with an error');
    try {
      return await response.json();
    } catch (cause) {
      throw new WeatherFailureError(timedOut() ? 'timeout' : 'invalid_response', 'Open-Meteo answered without JSON', {
        cause,
      });
    }
  } finally {
    clearTimeout(timer);
  }
}

function finite(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Up to five places for a name; [] when nothing matches. Throws when Open-Meteo is unreachable. */
export async function searchLocations(
  name: string,
  language: string,
  fetcher: FetchLike = fetch
): Promise<RangeLocation[]> {
  const query = new URLSearchParams({ name: name.trim(), count: '5', language, format: 'json' });
  const body = (await getJson(`${GEOCODING_URL}?${query}`, fetcher)) as { results?: unknown };
  const results = Array.isArray(body?.results) ? body.results : [];
  return results.flatMap((raw): RangeLocation[] => {
    const entry = raw as Record<string, unknown>;
    if (!finite(entry.latitude) || !finite(entry.longitude) || !text(entry.name)) return [];
    return [
      {
        name: text(entry.name),
        latitude: entry.latitude,
        longitude: entry.longitude,
        country: text(entry.country),
        admin1: text(entry.admin1),
      },
    ];
  });
}

/**
 * Current weather at the stored location as a `weather_report`. The coordinates sent back are the
 * requested ones (Open-Meteo answers with its grid cell's), so the server can match them to the
 * stored location. Wind is in m/s; `current.time` is UTC epoch seconds.
 */
export async function fetchCurrentWeather(
  location: RangeLocation,
  fetcher: FetchLike = fetch,
  now: () => Date = () => new Date()
): Promise<WeatherReport> {
  const query = new URLSearchParams({
    latitude: String(location.latitude),
    longitude: String(location.longitude),
    current: CURRENT_FIELDS,
    wind_speed_unit: 'ms',
    timeformat: 'unixtime',
    timezone: 'GMT',
  });
  const body = (await getJson(`${FORECAST_URL}?${query}`, fetcher)) as { current?: Record<string, unknown> };
  const current = body?.current ?? {};
  const fields = [
    current.time,
    current.temperature_2m,
    current.relative_humidity_2m,
    current.surface_pressure,
    current.wind_speed_10m,
    current.wind_direction_10m,
  ];
  if (!fields.every(finite))
    throw new WeatherFailureError('invalid_response', 'Open-Meteo answered without current weather');
  return {
    source: 'open-meteo',
    temperature_c: current.temperature_2m as number,
    humidity_pct: current.relative_humidity_2m as number,
    pressure_hpa: current.surface_pressure as number,
    wind_speed_ms: current.wind_speed_10m as number,
    wind_direction_deg: current.wind_direction_10m as number,
    observed_epoch_s: current.time as number,
    latitude: location.latitude,
    longitude: location.longitude,
    ...clientClockPayload(now()),
  };
}
