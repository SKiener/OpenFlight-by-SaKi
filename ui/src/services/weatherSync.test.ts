import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useWeatherStore } from '../stores/useWeatherStore';
import { nativeBridgeError } from '../test-utils/nativeBridge';
import type { RangeLocation, WeatherReport, WeatherState } from '../types/weather';
import {
  newGpsLocationAttempts,
  syncLocationFromGps,
  syncWeather,
  weatherFetchDue,
  weatherRetryPauseMs,
  WEATHER_CHECK_INTERVAL_MS,
  WEATHER_MIN_SUCCESS_INTERVAL_MS,
  WEATHER_RETRY_FIRST_PAUSE_MS,
  type GpsLocationDeps,
  type WeatherSyncDeps,
} from './weatherSync';
import { WeatherFailureError } from './weatherFailure';

function state(overrides: Partial<WeatherState> = {}): WeatherState {
  return {
    location: { name: 'Golfpark', latitude: 48.1, longitude: 11.5 },
    reading: null,
    density_kg_m3: 1.184,
    density_source: 'standard',
    standard_density_kg_m3: 1.184,
    fetch_due: true,
    fresh_max_s: 10800,
    fetch_interval_s: 1800,
    ...overrides,
  };
}

const OPEN_METEO_READING = {
  source: 'open-meteo' as const,
  temperature_c: 10,
  pressure_hpa: 960,
  humidity_pct: 70,
  humidity_assumed: false,
  wind_speed_ms: 1,
  wind_direction_deg: 1,
  air_density_kg_m3: 1.17,
  age_s: 2400,
};

const REPORT = {
  source: 'open-meteo',
  temperature_c: 10,
  pressure_hpa: 960,
  epoch_ms: 1,
  tz_offset_min: 0,
} as WeatherReport;

function deps(overrides: Partial<WeatherSyncDeps> = {}) {
  return {
    isKiosk: false,
    connected: true,
    now: () => 1000,
    fetchWeather: vi.fn(async () => REPORT),
    send: vi.fn(async () => ({ ok: true as const })),
    reportFailure: vi.fn(),
    ...overrides,
  };
}

describe('weatherFetchDue', () => {
  const reading = {
    source: 'open-meteo' as const,
    temperature_c: 10,
    pressure_hpa: 960,
    humidity_pct: 70,
    humidity_assumed: false,
    wind_speed_ms: 1,
    wind_direction_deg: 1,
    air_density_kg_m3: 1.17,
    age_s: 1700,
  };

  it('needs a stored location', () => {
    expect(weatherFetchDue(state({ location: null }), 0, 0)).toBe(false);
    expect(weatherFetchDue(null, 0, 0)).toBe(false);
  });

  it('follows the server and ages the reading on locally', () => {
    expect(weatherFetchDue(state(), 0, 0)).toBe(true);
    const recent = state({ fetch_due: false, reading });
    expect(weatherFetchDue(recent, 0, 100_000)).toBe(false);
    expect(weatherFetchDue(recent, 0, 100_001)).toBe(true);
  });

  it('keeps trying while only a manual entry is there', () => {
    expect(weatherFetchDue(state({ fetch_due: false, reading: { ...reading, source: 'manual' } }), 0, 0)).toBe(true);
  });
});

describe('weatherRetryPauseMs', () => {
  it('doubles from 15 s up to 5 min', () => {
    expect([1, 2, 3, 4, 5, 6, 7, 50].map((n) => weatherRetryPauseMs(n) / 1000)).toEqual([
      15, 30, 60, 120, 240, 300, 300, 300,
    ]);
    expect(weatherRetryPauseMs(0)).toBe(WEATHER_RETRY_FIRST_PAUSE_MS);
  });

  it('is checked every 15 s', () => {
    expect(WEATHER_CHECK_INTERVAL_MS).toBe(15_000);
  });
});

describe('syncWeather', () => {
  beforeEach(() => {
    useWeatherStore.setState({
      state: state(),
      receivedAt: 0,
      fetchStatus: 'idle',
      failedAt: null,
      succeededAt: null,
      consecutiveFailures: 0,
    });
  });

  it('fetches for the stored location and sends the report', async () => {
    const d = deps();
    await syncWeather(d);
    expect(d.fetchWeather).toHaveBeenCalledWith({ name: 'Golfpark', latitude: 48.1, longitude: 11.5 });
    expect(d.send).toHaveBeenCalledWith(REPORT);
    expect(useWeatherStore.getState().fetchStatus).toBe('idle');
  });

  it('never fetches on the kiosk, offline, or when nothing is due', async () => {
    for (const d of [deps({ isKiosk: true }), deps({ connected: false })]) {
      await syncWeather(d);
      expect(d.fetchWeather).not.toHaveBeenCalled();
    }
    useWeatherStore.setState({ state: state({ location: null }) });
    const d = deps();
    await syncWeather(d);
    expect(d.fetchWeather).not.toHaveBeenCalled();
  });

  it('marks a failed fetch and pauses before the next try', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const failing = deps({ fetchWeather: vi.fn(async () => Promise.reject(new Error('offline'))) });
    await syncWeather(failing);
    expect(useWeatherStore.getState().fetchStatus).toBe('failed');
    expect(useWeatherStore.getState().failedAt).toBe(1000);
    expect(useWeatherStore.getState().consecutiveFailures).toBe(1);

    const soon = deps({ now: () => 1000 + WEATHER_RETRY_FIRST_PAUSE_MS - 1 });
    await syncWeather(soon);
    expect(soon.fetchWeather).not.toHaveBeenCalled();

    const later = deps({ now: () => 1000 + WEATHER_RETRY_FIRST_PAUSE_MS });
    await syncWeather(later);
    expect(later.fetchWeather).toHaveBeenCalledTimes(1);
    expect(useWeatherStore.getState().fetchStatus).toBe('idle');
    expect(useWeatherStore.getState().consecutiveFailures).toBe(0);
  });

  it('waits 15, 30, 60, 120, 240, then 300 s between failed tries, however long a fetch takes', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    let now = 0;
    const tries: number[] = [];
    // Each fetch takes time on the clock (20 ms, then 3 s) before it fails.
    const durations = [20, 3000];
    const failing = deps({
      now: () => now,
      fetchWeather: vi.fn(async () => {
        tries.push(now / 1000);
        now += durations[tries.length % 2];
        return Promise.reject(new Error('offline'));
      }),
    });
    // The phone checks every 15 s.
    for (let tick = 0; tick <= 1200_000; tick += WEATHER_CHECK_INTERVAL_MS) {
      now = tick;
      await syncWeather(failing);
    }
    expect(tries).toEqual([0, 15, 45, 105, 225, 465, 765, 1065]);
    const gaps = tries.slice(1).map((at, i) => at - tries[i]);
    expect(gaps).toEqual([15, 30, 60, 120, 240, 300, 300]);
    // Each report names the pause that really follows it.
    const announced = failing.reportFailure.mock.calls.map(([failure]) => failure.next_retry_s);
    expect(announced.slice(0, gaps.length)).toEqual(gaps);
    expect(useWeatherStore.getState().consecutiveFailures).toBe(8);
  });

  it('waits at least a minute after a success, even while the server still asks for a fetch', async () => {
    let now = 0;
    const tries: number[] = [];
    const fetching = deps({
      now: () => now,
      fetchWeather: vi.fn(async () => {
        tries.push(now / 1000);
        now += 2000;
        return REPORT;
      }),
      // The Pi stores the reading, but keeps calling it old.
      send: vi.fn(async () => {
        useWeatherStore.getState().applyState(state({ reading: OPEN_METEO_READING }), now);
        return { ok: true as const };
      }),
    });
    // The server's state keeps `fetch_due: true` (phone clock ahead, Open-Meteo lagging).
    for (let tick = 0; tick <= 180_000; tick += WEATHER_CHECK_INTERVAL_MS) {
      now = tick;
      await syncWeather(fetching);
    }
    expect(WEATHER_MIN_SUCCESS_INTERVAL_MS).toBe(60_000);
    expect(tries).toEqual([0, 60, 120, 180]);
    expect(useWeatherStore.getState().succeededAt).toBe(180_000);
    expect(fetching.reportFailure).not.toHaveBeenCalled();
  });

  it('fetches a new location at once, even right after a success', async () => {
    await syncWeather(deps({ now: () => 0 }));
    expect(useWeatherStore.getState().succeededAt).toBe(0);
    // With the reading in place the next try waits for the minute ...
    useWeatherStore.getState().applyState(state({ reading: OPEN_METEO_READING }), 0);
    const held = deps({ now: () => WEATHER_CHECK_INTERVAL_MS });
    await syncWeather(held);
    expect(held.fetchWeather).not.toHaveBeenCalled();
    // ... but a new location clears the Pi's reading, and its weather is fetched on the next check.
    useWeatherStore
      .getState()
      .applyState(
        state({ location: { name: 'Anderer Platz', latitude: 47.9, longitude: 11.2 }, reading: null }),
        WEATHER_CHECK_INTERVAL_MS
      );
    const moved = deps({ now: () => 2 * WEATHER_CHECK_INTERVAL_MS });
    await syncWeather(moved);
    expect(moved.fetchWeather).toHaveBeenCalledWith({ name: 'Anderer Platz', latitude: 47.9, longitude: 11.2 });
  });

  it('starts again at 15 s after a success', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    useWeatherStore.setState({ fetchStatus: 'failed', failedAt: 0, consecutiveFailures: 6 });
    const early = deps({ now: () => 299_999 });
    await syncWeather(early);
    expect(early.fetchWeather).not.toHaveBeenCalled();
    await syncWeather(deps({ now: () => 300_000 }));
    expect(useWeatherStore.getState().consecutiveFailures).toBe(0);

    await syncWeather(deps({ now: () => 400_000, fetchWeather: vi.fn(async () => Promise.reject(new Error('x'))) }));
    const retry = deps({ now: () => 400_000 + WEATHER_RETRY_FIRST_PAUSE_MS });
    await syncWeather(retry);
    expect(retry.fetchWeather).toHaveBeenCalledTimes(1);
  });

  it('counts a refused report as a failure', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const refused = deps({ send: vi.fn(async () => ({ ok: false as const, error: 'stale' })) });
    await syncWeather(refused);
    expect(useWeatherStore.getState().fetchStatus).toBe('failed');
  });

  it('reports each failed fetch with its reason, count and pause, nothing else', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const error = nativeBridgeError('http_error', 'Open-Meteo HTTP 503 via cellular (48.1, 11.5)');
    const failing = deps({ fetchWeather: vi.fn(async () => Promise.reject(error)) });
    await syncWeather(failing);
    expect(failing.reportFailure).toHaveBeenCalledTimes(1);
    expect(failing.reportFailure).toHaveBeenCalledWith({
      stage: 'fetch',
      reason: 'http_503',
      consecutive_failures: 1,
      next_retry_s: 15,
    });

    const again = deps({
      now: () => 1000 + WEATHER_RETRY_FIRST_PAUSE_MS,
      fetchWeather: vi.fn(async () => Promise.reject(new DOMException('aborted', 'AbortError'))),
    });
    await syncWeather(again);
    const [payload] = again.reportFailure.mock.calls[0];
    expect(payload).toEqual({ stage: 'fetch', reason: 'timeout', consecutive_failures: 2, next_retry_s: 30 });
    expect(Object.keys(payload).sort()).toEqual(['consecutive_failures', 'next_retry_s', 'reason', 'stage']);
    expect(JSON.stringify(payload)).not.toMatch(/48\.1|11\.5|Open-Meteo|http:|https:/);
  });

  it('reports a refused or unanswered report as a send failure', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const refused = deps({ send: vi.fn(async () => ({ ok: false as const, error: 'location_mismatch' })) });
    await syncWeather(refused);
    expect(refused.reportFailure).toHaveBeenCalledWith({
      stage: 'send',
      reason: 'rejected',
      consecutive_failures: 1,
      next_retry_s: 15,
    });

    const unanswered = deps({
      now: () => 1000 + WEATHER_RETRY_FIRST_PAUSE_MS,
      send: vi.fn(async () => ({ ok: false as const, error: 'timeout' })),
    });
    await syncWeather(unanswered);
    expect(unanswered.reportFailure).toHaveBeenCalledWith(
      expect.objectContaining({ stage: 'send', reason: 'timeout', consecutive_failures: 2 })
    );
  });

  it('reports nothing when the fetch succeeds or is not tried', async () => {
    const ok = deps();
    await syncWeather(ok);
    await syncWeather(deps({ isKiosk: true, fetchWeather: vi.fn(async () => Promise.reject(new Error('x'))) }));
    expect(ok.reportFailure).not.toHaveBeenCalled();
  });

  it('runs one fetch at a time', async () => {
    let finish: (report: WeatherReport) => void = () => {};
    const slow = deps({ fetchWeather: vi.fn(() => new Promise<WeatherReport>((resolve) => (finish = resolve))) });
    const first = syncWeather(slow);
    await syncWeather(slow);
    expect(slow.fetchWeather).toHaveBeenCalledTimes(1);
    finish(REPORT);
    await first;
  });
});

describe('syncLocationFromGps', () => {
  const GPS: RangeLocation = { name: '', latitude: 48.10234, longitude: 11.50012 };

  function gpsDeps(overrides: Partial<GpsLocationDeps> = {}) {
    return {
      isKiosk: false,
      connected: true,
      now: () => 1000,
      attempts: newGpsLocationAttempts(),
      locate: vi.fn(async (): Promise<RangeLocation | null> => GPS),
      save: vi.fn(async () => true),
      reportFailure: vi.fn(),
      ...overrides,
    };
  }

  beforeEach(() => {
    useWeatherStore.setState({
      state: state({ location: null }),
      receivedAt: 0,
      fetchStatus: 'idle',
      failedAt: null,
      succeededAt: null,
      consecutiveFailures: 0,
    });
  });

  it('stores the GPS fix while the Pi has no location, once', async () => {
    const d = gpsDeps();
    await syncLocationFromGps(d);
    expect(d.save).toHaveBeenCalledWith(GPS);
    expect(d.attempts.done).toBe(true);
    await syncLocationFromGps(d);
    expect(d.locate).toHaveBeenCalledTimes(1);
  });

  it('never overwrites a stored location, and waits for the weather state', async () => {
    useWeatherStore.setState({ state: state() });
    const stored = gpsDeps();
    await syncLocationFromGps(stored);
    useWeatherStore.setState({ state: null });
    const unknown = gpsDeps();
    await syncLocationFromGps(unknown);
    expect(stored.locate).not.toHaveBeenCalled();
    expect(unknown.locate).not.toHaveBeenCalled();
  });

  it('does nothing on the kiosk or offline', async () => {
    for (const d of [gpsDeps({ isKiosk: true }), gpsDeps({ connected: false })]) {
      await syncLocationFromGps(d);
      expect(d.locate).not.toHaveBeenCalled();
    }
  });

  it('drops the fix when a location was set while it was pending', async () => {
    const d = gpsDeps({
      locate: vi.fn(async () => {
        useWeatherStore.setState({ state: state() });
        return GPS;
      }),
    });
    await syncLocationFromGps(d);
    expect(d.save).not.toHaveBeenCalled();
    expect(d.attempts.done).toBe(true);
  });

  it('pauses after no fix, a refused save or an error, longer each time, then tries again', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const attempts = newGpsLocationAttempts();
    const noFix = gpsDeps({ attempts, locate: vi.fn(async () => null) });
    await syncLocationFromGps(noFix);
    expect(attempts.failedAt).toBe(1000);
    expect(attempts.consecutiveFailures).toBe(1);

    const soon = gpsDeps({ attempts, now: () => 1000 + 15_000 - 1 });
    await syncLocationFromGps(soon);
    expect(soon.locate).not.toHaveBeenCalled();

    const refused = gpsDeps({ attempts, now: () => 16_000, save: vi.fn(async () => false) });
    await syncLocationFromGps(refused);
    expect(refused.save).toHaveBeenCalledTimes(1);
    expect(attempts.done).toBe(false);
    expect(attempts.consecutiveFailures).toBe(2);

    // The second failure in a row pauses 30 s.
    const tooSoon = gpsDeps({ attempts, now: () => 16_000 + 30_000 - 1 });
    await syncLocationFromGps(tooSoon);
    expect(tooSoon.locate).not.toHaveBeenCalled();

    const failing = gpsDeps({
      attempts,
      now: () => 46_000,
      locate: vi.fn(async () => Promise.reject(new Error('denied'))),
    });
    await syncLocationFromGps(failing);
    expect(failing.locate).toHaveBeenCalledTimes(1);
    expect(attempts.failedAt).toBe(46_000);
    expect(attempts.consecutiveFailures).toBe(3);
    expect(attempts.inFlight).toBe(false);

    const fix = gpsDeps({ attempts, now: () => 46_000 + 60_000 });
    await syncLocationFromGps(fix);
    expect(fix.save).toHaveBeenCalledTimes(1);
    expect(attempts.done).toBe(true);
    expect(attempts.consecutiveFailures).toBe(0);
  });

  it('measures the pause from the start of an attempt, however long the fix takes', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    let now = 0;
    const tries: number[] = [];
    const d = gpsDeps({
      now: () => now,
      // No fix after a ~15 s wait for one.
      locate: vi.fn(async () => {
        tries.push(now / 1000);
        now += 14_500;
        return null;
      }),
    });
    for (let tick = 0; tick <= 1200_000; tick += WEATHER_CHECK_INTERVAL_MS) {
      now = tick;
      await syncLocationFromGps(d);
    }
    expect(tries).toEqual([0, 15, 45, 105, 225, 465, 765, 1065]);
    const gaps = tries.slice(1).map((at, i) => at - tries[i]);
    expect(gaps).toEqual([15, 30, 60, 120, 240, 300, 300]);
    const announced = d.reportFailure.mock.calls.map(([failure]) => failure.next_retry_s);
    expect(announced.slice(0, gaps.length)).toEqual(gaps);
  });

  it('reports why an attempt failed, with its own count', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const attempts = newGpsLocationAttempts();
    const denied = gpsDeps({
      attempts,
      locate: vi.fn(async () => Promise.reject(new WeatherFailureError('no_permission', 'The app may not locate'))),
    });
    await syncLocationFromGps(denied);
    expect(denied.reportFailure).toHaveBeenCalledWith({
      stage: 'location',
      reason: 'no_permission',
      consecutive_failures: 1,
      next_retry_s: 15,
    });

    const noFix = gpsDeps({ attempts, now: () => 16_000, locate: vi.fn(async () => null) });
    await syncLocationFromGps(noFix);
    expect(noFix.reportFailure).toHaveBeenCalledWith({
      stage: 'location',
      reason: 'no_fix',
      consecutive_failures: 2,
      next_retry_s: 30,
    });

    const refused = gpsDeps({ attempts, now: () => 46_000, save: vi.fn(async () => false) });
    await syncLocationFromGps(refused);
    const [payload] = refused.reportFailure.mock.calls[0];
    expect(payload).toEqual({ stage: 'location', reason: 'save_failed', consecutive_failures: 3, next_retry_s: 60 });
    // The fix itself never leaves in the report.
    expect(JSON.stringify(payload)).not.toMatch(/48\.10234|11\.50012|latitude|longitude/);

    const native = gpsDeps({
      attempts,
      now: () => 106_000,
      locate: vi.fn(async () => Promise.reject(nativeBridgeError('timeout'))),
    });
    await syncLocationFromGps(native);
    expect(native.reportFailure).toHaveBeenCalledWith(
      expect.objectContaining({ reason: 'timeout', consecutive_failures: 4, next_retry_s: 120 })
    );
  });

  it('reports nothing when a location was set while the attempt failed', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const d = gpsDeps({
      locate: vi.fn(async () => {
        useWeatherStore.setState({ state: state() });
        return Promise.reject(new WeatherFailureError('no_fix', 'No usable GPS fix'));
      }),
    });
    await syncLocationFromGps(d);
    expect(d.reportFailure).not.toHaveBeenCalled();
    expect(d.attempts.done).toBe(true);
    expect(d.attempts.consecutiveFailures).toBe(0);
  });

  it('counts its failures apart from the weather fetch', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    useWeatherStore.setState({ consecutiveFailures: 5 });
    const attempts = newGpsLocationAttempts();
    await syncLocationFromGps(gpsDeps({ attempts, locate: vi.fn(async () => null) }));
    expect(attempts.consecutiveFailures).toBe(1);
    expect(useWeatherStore.getState().consecutiveFailures).toBe(5);
    const retry = gpsDeps({ attempts, now: () => 1000 + WEATHER_RETRY_FIRST_PAUSE_MS });
    await syncLocationFromGps(retry);
    expect(retry.locate).toHaveBeenCalledTimes(1);
  });

  it('runs one attempt at a time', async () => {
    let finish: (location: RangeLocation | null) => void = () => {};
    const slow = gpsDeps({ locate: vi.fn(() => new Promise<RangeLocation | null>((resolve) => (finish = resolve))) });
    const first = syncLocationFromGps(slow);
    await syncLocationFromGps(slow);
    expect(slow.locate).toHaveBeenCalledTimes(1);
    finish(GPS);
    await first;
    expect(slow.save).toHaveBeenCalledTimes(1);
  });
});
