import { afterEach, describe, expect, it, vi } from 'vitest';
import { fakeNative, nativeBridgeError, nativeWeatherReport, sensorSnapshot } from '../test-utils/nativeBridge';
import { weatherFailureReason } from '../services/weatherFailure';
import type { RangeLocation, WeatherReport } from '../types/weather';
import {
  BAROMETER_FRESH_MAX_S,
  fetchRangeWeather,
  freshBarometerPressure,
  GPS_FIX_MAX_AGE_S,
  rangeLocationFromGps,
  weatherReportFromNative,
} from './nativeWeather';

const LOCATION: RangeLocation = { name: 'Golfpark', latitude: 48.1, longitude: 11.5 };
const SEND_TIME = new Date(1_793_007_960_000);

const BROWSER_REPORT: WeatherReport = {
  source: 'open-meteo',
  temperature_c: 10.1,
  humidity_pct: 66,
  pressure_hpa: 960.0,
  wind_speed_ms: 3,
  wind_direction_deg: 250,
  observed_epoch_s: 1_793_007_300,
  latitude: 48.1,
  longitude: 11.5,
  epoch_ms: 1,
  tz_offset_min: 60,
};

const browserFetch = () => vi.fn(async () => ({ ...BROWSER_REPORT }));

afterEach(() => vi.restoreAllMocks());

describe('freshBarometerPressure', () => {
  it('takes an authorized sample up to 10 min old', () => {
    expect(freshBarometerPressure(sensorSnapshot())).toEqual({ pressure_hpa: 957.4, age_s: 0.7 });
    const limit = sensorSnapshot({ barometer: { authorization: 'authorized', pressure_hpa: 957.4, age_s: 600 } });
    expect(freshBarometerPressure(limit)?.age_s).toBe(BAROMETER_FRESH_MAX_S);
  });

  it.each([
    ['too old', { authorization: 'authorized', pressure_hpa: 957.4, age_s: 600.1 }],
    ['denied', { authorization: 'denied', pressure_hpa: 957.4, age_s: 1 }],
    ['no sample yet', { authorization: 'authorized', pressure_hpa: null, age_s: null }],
    ['negative age', { authorization: 'authorized', pressure_hpa: 957.4, age_s: -1 }],
    ['no hardware', null],
  ] as const)('ignores a sample that is %s', (_why, barometer) => {
    expect(freshBarometerPressure(sensorSnapshot({ barometer }))).toBeNull();
  });

  it('ignores a missing snapshot', () => {
    expect(freshBarometerPressure(null)).toBeNull();
  });
});

describe('weatherReportFromNative', () => {
  it('keeps the weather_report fields, the stored coordinates and the transport', () => {
    const report = weatherReportFromNative(nativeWeatherReport({ latitude: 48.10001 }), LOCATION);
    expect(report).toMatchObject({
      source: 'open-meteo',
      temperature_c: 9.9,
      pressure_hpa: 960.2,
      humidity_pct: 65,
      latitude: 48.1,
      longitude: 11.5,
      transport: 'cellular',
    });
    expect(report).not.toHaveProperty('pressure_msl_hpa');
    expect(weatherReportFromNative(nativeWeatherReport({ transport: 'default' }), LOCATION).transport).toBe('default');
  });

  it('throws on a missing field', () => {
    expect(() => weatherReportFromNative(nativeWeatherReport({ temperature_c: Number.NaN }), LOCATION)).toThrow();
  });
});

describe('fetchRangeWeather', () => {
  it('in Safari fetches in the page exactly as before', async () => {
    const fetch = browserFetch();
    const report = await fetchRangeWeather(LOCATION, { native: null, browserFetch: fetch });
    expect(report).toEqual(BROWSER_REPORT);
    expect(fetch).toHaveBeenCalledWith(LOCATION);
  });

  it('in the companion prefers the native fetch and the fresh barometer', async () => {
    const native = fakeNative();
    const fetch = browserFetch();
    const report = await fetchRangeWeather(LOCATION, { native, browserFetch: fetch, now: () => SEND_TIME });
    expect(native.fetchWeather).toHaveBeenCalledWith({ latitude: 48.1, longitude: 11.5 });
    expect(fetch).not.toHaveBeenCalled();
    expect(report).toMatchObject({
      transport: 'cellular',
      temperature_c: 9.9,
      pressure_hpa: 957.4,
      pressure_source: 'phone-barometer',
      open_meteo_pressure_hpa: 960.2,
      phone_pressure_age_s: 0.7,
      epoch_ms: SEND_TIME.getTime(),
    });
  });

  it("keeps Open-Meteo's pressure when the barometer sample is stale or missing", async () => {
    const stale = fakeNative({
      getSensors: vi.fn(async () =>
        sensorSnapshot({ barometer: { authorization: 'authorized', pressure_hpa: 950, age_s: 601 } })
      ),
    });
    const report = await fetchRangeWeather(LOCATION, { native: stale, browserFetch: browserFetch() });
    expect(report.pressure_hpa).toBe(960.2);
    expect(report).not.toHaveProperty('pressure_source');

    const failing = fakeNative({ getSensors: vi.fn(async () => Promise.reject(new Error('busy'))) });
    expect((await fetchRangeWeather(LOCATION, { native: failing, browserFetch: browserFetch() })).pressure_hpa).toBe(
      960.2
    );

    const noBarometer = fakeNative({ capabilities: { barometer: false } });
    await fetchRangeWeather(LOCATION, { native: noBarometer, browserFetch: browserFetch() });
    expect(noBarometer.getSensors).not.toHaveBeenCalled();
  });

  it('falls back to the page fetch when the native fetch fails, still with the barometer', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const native = fakeNative({ fetchWeather: vi.fn(async () => Promise.reject(new Error('network'))) });
    const fetch = browserFetch();
    const report = await fetchRangeWeather(LOCATION, { native, browserFetch: fetch });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(report).toMatchObject({
      transport: 'browser',
      pressure_hpa: 957.4,
      pressure_source: 'phone-barometer',
      open_meteo_pressure_hpa: 960.0,
    });
  });

  it('uses the page fetch when the companion cannot fetch weather', async () => {
    const native = fakeNative({ capabilities: { weather: false, barometer: false } });
    const report = await fetchRangeWeather(LOCATION, { native, browserFetch: browserFetch() });
    expect(native.fetchWeather).not.toHaveBeenCalled();
    expect(report.transport).toBe('browser');
  });

  it("rejects with the native fetch's error when both fetches fail", async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const nativeError = nativeBridgeError('http_error', 'Open-Meteo HTTP 503 via cellular');
    const native = fakeNative({ fetchWeather: vi.fn(async () => Promise.reject(nativeError)) });
    const fetch = vi.fn(async () => Promise.reject(new Error('offline')));
    await expect(fetchRangeWeather(LOCATION, { native, browserFetch: fetch })).rejects.toBe(nativeError);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each(['unavailable', 'busy', 'internal', 'bad_request'])(
    "rejects with the page fetch's error when the native %s error has no known reason",
    async (code) => {
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      const native = fakeNative({ fetchWeather: vi.fn(async () => Promise.reject(nativeBridgeError(code))) });
      const pageError = new DOMException('signal timed out', 'TimeoutError');
      const fetch = vi.fn(async () => Promise.reject(pageError));
      const rejection = fetchRangeWeather(LOCATION, { native, browserFetch: fetch });
      await expect(rejection).rejects.toBe(pageError);
      expect(weatherFailureReason(await rejection.catch((error: unknown) => error))).toBe('timeout');
    }
  );

  it("keeps the native fetch's error when it has a reason, whatever the page's", async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const nativeError = nativeBridgeError('network', 'cellular and default route failed');
    const native = fakeNative({ fetchWeather: vi.fn(async () => Promise.reject(nativeError)) });
    const fetch = vi.fn(async () => Promise.reject(new DOMException('signal timed out', 'TimeoutError')));
    await expect(fetchRangeWeather(LOCATION, { native, browserFetch: fetch })).rejects.toBe(nativeError);
  });

  it("propagates the page fetch's error when there is no native one", async () => {
    const fetch = vi.fn(async () => Promise.reject(new Error('offline')));
    await expect(fetchRangeWeather(LOCATION, { native: null, browserFetch: fetch })).rejects.toThrow('offline');
    const noWeather = fakeNative({ capabilities: { weather: false } });
    await expect(fetchRangeWeather(LOCATION, { native: noWeather, browserFetch: fetch })).rejects.toThrow('offline');
  });

  it('classifies a native answer without current weather as invalid', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    expect(() => weatherReportFromNative(nativeWeatherReport({ temperature_c: Number.NaN }), LOCATION)).toThrow(
      expect.objectContaining({ reason: 'invalid_response' })
    );
    const native = fakeNative({ fetchWeather: vi.fn(async () => nativeWeatherReport({ source: 'manual' as never })) });
    const fetch = vi.fn(async () => Promise.reject(new TypeError('Load failed')));
    const error = await fetchRangeWeather(LOCATION, { native, browserFetch: fetch }).catch((e: unknown) => e);
    expect(weatherFailureReason(error)).toBe('invalid_response');
  });
});

describe('rangeLocationFromGps', () => {
  it('asks for a recent fix and rounds it to the stored precision', async () => {
    const native = fakeNative();
    expect(await rangeLocationFromGps(native)).toEqual({ name: '', latitude: 48.10235, longitude: 11.50012 });
    expect(native.getSensors).toHaveBeenCalledWith({ location_max_age_s: GPS_FIX_MAX_AGE_S, timeout_ms: 15_000 });
  });

  it.each([
    ['location services off', null, 'no_permission'],
    ['permission denied', { authorization: 'denied', latitude: null, longitude: null }, 'no_permission'],
    ['restricted', { authorization: 'restricted', latitude: null, longitude: null }, 'no_permission'],
    ['an unanswered prompt', { authorization: 'not_determined', latitude: null, longitude: null }, 'no_permission'],
    ['no fix in time', { authorization: 'when_in_use', latitude: null, longitude: null }, 'no_fix'],
    [
      'an old fix',
      { authorization: 'when_in_use', latitude: 48.1, longitude: 11.5, age_s: GPS_FIX_MAX_AGE_S + 1 },
      'no_fix',
    ],
    ['impossible coordinates', { authorization: 'when_in_use', latitude: 91, longitude: 11.5 }, 'no_fix'],
  ] as const)('rejects for %s with its reason', async (_why, location, reason) => {
    const native = fakeNative({ getSensors: vi.fn(async () => sensorSnapshot({ location })) });
    const error = await rangeLocationFromGps(native).catch((e: unknown) => e);
    expect(weatherFailureReason(error)).toBe(reason);
  });

  it('passes a native rejection on', async () => {
    const native = fakeNative({ getSensors: vi.fn(async () => Promise.reject(nativeBridgeError('timeout'))) });
    const error = await rangeLocationFromGps(native).catch((e: unknown) => e);
    expect(weatherFailureReason(error)).toBe('timeout');
  });

  it('never asks without the location capability', async () => {
    const native = fakeNative({ capabilities: { location: false } });
    const error = await rangeLocationFromGps(native).catch((e: unknown) => e);
    expect(weatherFailureReason(error)).toBe('unknown');
    expect(native.getSensors).not.toHaveBeenCalled();
  });
});
