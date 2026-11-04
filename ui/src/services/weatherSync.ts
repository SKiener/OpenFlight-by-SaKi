import { useWeatherStore } from '../stores/useWeatherStore';
import type { RangeLocation, WeatherAck, WeatherReport, WeatherState } from '../types/weather';
import { readingAgeS } from '../utils/weatherFormat';
import {
  sendFailureReason,
  WeatherFailureError,
  weatherFailureReason,
  type WeatherFailureReport,
  type WeatherFailureStage,
} from './weatherFailure';

/** After a first failed fetch (no cellular route, Open-Meteo down) the phone waits this long before trying again. */
export const WEATHER_RETRY_FIRST_PAUSE_MS = 15_000;
/** Each further failure in a row doubles the pause, up to this one. */
export const WEATHER_RETRY_MAX_PAUSE_MS = 5 * 60_000;
/**
 * After a successful fetch the phone waits at least this long before the next one, even while the
 * server still says `fetch_due` (phone clock ahead, Open-Meteo's `current.time` lagging).
 */
export const WEATHER_MIN_SUCCESS_INTERVAL_MS = 60_000;
/** How often a connected phone re-checks whether the weather is due. */
export const WEATHER_CHECK_INTERVAL_MS = 15_000;

/** The pause after `consecutiveFailures` failed tries in a row: 15, 30, 60, 120, 240, then 300 s. */
export function weatherRetryPauseMs(consecutiveFailures: number): number {
  const failures = Math.max(1, consecutiveFailures);
  return Math.min(WEATHER_RETRY_FIRST_PAUSE_MS * 2 ** (failures - 1), WEATHER_RETRY_MAX_PAUSE_MS);
}

/** The `weather_fetch_failed` payload for the `consecutiveFailures`-th failure in a row of a stage's count. */
export function weatherFailureReport(
  stage: WeatherFailureStage,
  error: unknown,
  consecutiveFailures: number
): WeatherFailureReport {
  return {
    stage,
    reason: weatherFailureReason(error),
    consecutive_failures: consecutiveFailures,
    next_retry_s: Math.round(weatherRetryPauseMs(consecutiveFailures) / 1000),
  };
}

/** A location is stored and no Open-Meteo reading is younger than the server's refresh interval. */
export function weatherFetchDue(state: WeatherState | null, receivedAt: number, now: number): boolean {
  if (!state?.location) return false;
  if (state.fetch_due) return true;
  const reading = state.reading;
  if (!reading || reading.source !== 'open-meteo') return true;
  return readingAgeS(reading, receivedAt, now) > state.fetch_interval_s;
}

export interface WeatherSyncDeps {
  /** The kiosk never fetches: the Pi has no internet. */
  isKiosk: boolean;
  connected: boolean;
  now: () => number;
  fetchWeather: (location: RangeLocation) => Promise<WeatherReport>;
  send: (report: WeatherReport) => Promise<WeatherAck>;
  /** Tells the Pi why a try failed (session log only). */
  reportFailure: (failure: WeatherFailureReport) => void;
}

/**
 * Fetch and send the weather when it is due; at most one fetch at a time, paused longer after each
 * failure and at least WEATHER_MIN_SUCCESS_INTERVAL_MS after a success. Both pauses count from the
 * start of the try, so a slow fetch does not push the next one a check later.
 */
export async function syncWeather(deps: WeatherSyncDeps): Promise<void> {
  if (deps.isKiosk || !deps.connected) return;
  const store = useWeatherStore.getState();
  const now = deps.now();
  if (store.fetchStatus === 'fetching') return;
  if (!weatherFetchDue(store.state, store.receivedAt, now)) return;
  if (
    store.fetchStatus === 'failed' &&
    store.failedAt !== null &&
    now - store.failedAt < weatherRetryPauseMs(store.consecutiveFailures)
  )
    return;
  // Only while the Pi still holds an Open-Meteo reading: a new location clears it, and then the
  // new place's weather should not wait.
  if (
    store.succeededAt !== null &&
    store.state?.reading?.source === 'open-meteo' &&
    now - store.succeededAt < WEATHER_MIN_SUCCESS_INTERVAL_MS
  )
    return;
  const location = store.state?.location;
  if (!location) return;

  store.setFetchStatus('fetching', now);
  let stage: WeatherFailureStage = 'fetch';
  try {
    const report = await deps.fetchWeather(location);
    stage = 'send';
    const ack = await deps.send(report);
    if (!ack.ok) throw new WeatherFailureError(sendFailureReason(ack.error), ack.error);
    useWeatherStore.getState().setFetchStatus('idle', now);
  } catch (error) {
    console.warn('Weather fetch failed:', error);
    useWeatherStore.getState().setFetchStatus('failed', now);
    deps.reportFailure(weatherFailureReport(stage, error, useWeatherStore.getState().consecutiveFailures));
  }
}

/** One page's attempts to store the phone's GPS fix as the range location. */
export interface GpsLocationAttempts {
  inFlight: boolean;
  /** performance.now() when the last failed or empty attempt started, for the retry pause. */
  failedAt: number | null;
  /** Failed or empty attempts in a row (the weather fetch counts its own). */
  consecutiveFailures: number;
  done: boolean;
}

export const newGpsLocationAttempts = (): GpsLocationAttempts => ({
  inFlight: false,
  failedAt: null,
  consecutiveFailures: 0,
  done: false,
});

export interface GpsLocationDeps {
  isKiosk: boolean;
  connected: boolean;
  now: () => number;
  attempts: GpsLocationAttempts;
  /** The iOS companion's GPS fix; rejects (rangeLocationFromGps: with the reason) or gives null without one. */
  locate: () => Promise<RangeLocation | null>;
  save: (location: RangeLocation) => Promise<boolean>;
  /** Tells the Pi why an attempt failed (session log only). */
  reportFailure: (failure: WeatherFailureReport) => void;
}

/**
 * Inside the iOS companion: store the phone's GPS fix as the range location while the Pi has
 * none. Only once the server's weather state arrived (so a stored location is never overwritten),
 * one attempt at a time, paused like the weather fetch after a failure (with its own count).
 */
export async function syncLocationFromGps(deps: GpsLocationDeps): Promise<void> {
  const { attempts } = deps;
  if (deps.isKiosk || !deps.connected || attempts.inFlight || attempts.done) return;
  const store = useWeatherStore.getState();
  if (!store.state || store.state.location) return;
  const now = deps.now();
  if (attempts.failedAt !== null && now - attempts.failedAt < weatherRetryPauseMs(attempts.consecutiveFailures)) return;

  const locationStored = () => Boolean(useWeatherStore.getState().state?.location);
  attempts.inFlight = true;
  let failure: unknown = null;
  try {
    const location = await deps.locate();
    if (!location) {
      failure = new WeatherFailureError('no_fix', 'No GPS fix');
    } else if (!locationStored() && !(await deps.save(location))) {
      // (A location the user set while the fix was pending is never overwritten.)
      failure = new WeatherFailureError('save_failed', 'The Pi did not store the GPS location');
    }
  } catch (error) {
    // A rejection without a reason still counts as a failure.
    failure = error ?? new WeatherFailureError('unknown', 'GPS range location failed');
  } finally {
    attempts.inFlight = false;
  }
  // Stored (by this attempt, or by the user meanwhile): nothing left to do.
  if (failure === null || locationStored()) {
    attempts.done = true;
    attempts.consecutiveFailures = 0;
    return;
  }
  console.warn('GPS range location failed:', failure);
  attempts.failedAt = now;
  attempts.consecutiveFailures += 1;
  deps.reportFailure(weatherFailureReport('location', failure, attempts.consecutiveFailures));
}
