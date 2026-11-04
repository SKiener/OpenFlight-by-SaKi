import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchCurrentWeather, OPEN_METEO_TIMEOUT_MS, searchLocations } from './openMeteo';
import { weatherFailureReason } from './weatherFailure';

function fakeFetch(body: unknown, ok = true, status?: number) {
  const calls: string[] = [];
  const fetcher = async (url: string) => {
    calls.push(url);
    return { ok, status, json: async () => body };
  };
  return { fetcher, calls };
}

afterEach(() => vi.useRealTimers());

const LOCATION = { name: 'Golfpark', latitude: 48.1, longitude: 11.5 };

describe('Open-Meteo', () => {
  it('asks for the five current fields in m/s and UTC epoch seconds', async () => {
    const { fetcher, calls } = fakeFetch({
      latitude: 48.12,
      longitude: 11.49,
      current: {
        time: 1793007900,
        interval: 900,
        temperature_2m: 9.9,
        relative_humidity_2m: 65,
        surface_pressure: 960.2,
        wind_speed_10m: 3.28,
        wind_direction_10m: 258,
      },
    });
    const report = await fetchCurrentWeather(LOCATION, fetcher, () => new Date(1793008000000));

    const url = new URL(calls[0]);
    expect(url.origin + url.pathname).toBe('https://api.open-meteo.com/v1/forecast');
    expect(url.searchParams.get('current')).toBe(
      'temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m'
    );
    expect(url.searchParams.get('wind_speed_unit')).toBe('ms');
    expect(url.searchParams.get('timeformat')).toBe('unixtime');
    expect(report).toMatchObject({
      source: 'open-meteo',
      temperature_c: 9.9,
      humidity_pct: 65,
      pressure_hpa: 960.2,
      wind_speed_ms: 3.28,
      wind_direction_deg: 258,
      observed_epoch_s: 1793007900,
      // The requested coordinates, not the grid cell's: the server matches them to the stored location.
      latitude: 48.1,
      longitude: 11.5,
      epoch_ms: 1793008000000,
    });
  });

  it('refuses an answer without current weather', async () => {
    const { fetcher } = fakeFetch({ current: { time: 1, temperature_2m: 'n/a' } });
    await expect(fetchCurrentWeather(LOCATION, fetcher)).rejects.toThrow();
    await expect(fetchCurrentWeather(LOCATION, fakeFetch({}, false).fetcher)).rejects.toThrow();
  });

  it('says why a fetch failed', async () => {
    const reason = (promise: Promise<unknown>) => promise.then(() => 'resolved', weatherFailureReason);
    expect(await reason(fetchCurrentWeather(LOCATION, fakeFetch({}, false, 503).fetcher))).toBe('http_503');
    expect(await reason(fetchCurrentWeather(LOCATION, fakeFetch({}, false).fetcher))).toBe('unknown');
    expect(await reason(fetchCurrentWeather(LOCATION, fakeFetch({ current: {} }).fetcher))).toBe('invalid_response');
    const noRoute = async () => Promise.reject(new TypeError('Load failed'));
    expect(await reason(fetchCurrentWeather(LOCATION, noRoute))).toBe('offline');
    const notJson = async () => ({ ok: true, json: async () => Promise.reject(new SyntaxError('Unexpected token')) });
    expect(await reason(fetchCurrentWeather(LOCATION, notJson))).toBe('invalid_response');
  });

  it('gives up after its timeout', async () => {
    vi.useFakeTimers();
    const hanging = (_url: string, init?: { signal?: AbortSignal }) =>
      new Promise<never>((_resolve, reject) =>
        init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
      );
    const result = fetchCurrentWeather(LOCATION, hanging).then(() => 'resolved', weatherFailureReason);
    await vi.advanceTimersByTimeAsync(OPEN_METEO_TIMEOUT_MS);
    expect(await result).toBe('timeout');
  });

  it('searches places by name in the UI language', async () => {
    const { fetcher, calls } = fakeFetch({
      results: [
        {
          name: 'Otelfingen / Golfpark',
          latitude: 47.45376,
          longitude: 8.40201,
          country: 'Schweiz',
          admin1: 'Kanton Zürich',
        },
        { name: 'broken', latitude: 'x', longitude: 1 },
      ],
    });
    const found = await searchLocations(' Golfpark ', 'de', fetcher);
    const url = new URL(calls[0]);
    expect(url.searchParams.get('name')).toBe('Golfpark');
    expect(url.searchParams.get('language')).toBe('de');
    expect(found).toEqual([
      {
        name: 'Otelfingen / Golfpark',
        latitude: 47.45376,
        longitude: 8.40201,
        country: 'Schweiz',
        admin1: 'Kanton Zürich',
      },
    ]);
  });

  it('returns nothing when the search has no results', async () => {
    expect(await searchLocations('xyz', 'en', fakeFetch({ generationtime_ms: 0.1 }).fetcher)).toEqual([]);
  });
});
