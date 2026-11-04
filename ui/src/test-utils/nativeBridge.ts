import { vi } from 'vitest';
import type { NativeSpeech, NativeWeatherReport, OpenFlightNative, SensorSnapshot } from '../native/openflightNative';

/** A phone clock at 2026-10-25 09:05 UTC (as in the server tests). */
export const PHONE_EPOCH_MS = 1_793_007_900_000;

export function sensorSnapshot(overrides: Partial<SensorSnapshot> = {}): SensorSnapshot {
  return {
    type: 'sensors',
    epoch_ms: PHONE_EPOCH_MS,
    tz_offset_min: 60,
    clock: { host_us: 812_345_700_000, epoch_ms: PHONE_EPOCH_MS, bracket_us: 2 },
    barometer: { authorization: 'authorized', pressure_hpa: 957.4, age_s: 0.7 },
    location: {
      authorization: 'when_in_use',
      accuracy_authorization: 'full',
      latitude: 48.1023456,
      longitude: 11.500118,
      horizontal_accuracy_m: 12,
      age_s: 4,
    },
    network: { wifi: true, cellular_available: true },
    audio: { state: 'running', capture_id: 'c1', last_seq: 3 },
    battery_pct: 81,
    ...overrides,
  };
}

export function nativeWeatherReport(overrides: Partial<NativeWeatherReport> = {}): NativeWeatherReport {
  return {
    source: 'open-meteo',
    latitude: 48.1,
    longitude: 11.5,
    temperature_c: 9.9,
    pressure_hpa: 960.2,
    humidity_pct: 65,
    wind_speed_ms: 3.28,
    wind_direction_deg: 258,
    observed_epoch_s: PHONE_EPOCH_MS / 1000 - 600,
    epoch_ms: PHONE_EPOCH_MS - 60_000,
    tz_offset_min: 60,
    transport: 'cellular',
    pressure_msl_hpa: 1013.1,
    fetch_ms: 412.5,
    ...overrides,
  };
}

/** A bridge rejection as openflight-native.js builds it: an `OpenFlightNativeError` with `.code`. */
export function nativeBridgeError(code: string, message = code): Error {
  return Object.assign(new Error(message), { name: 'OpenFlightNativeError', code });
}

/** The companion's native speech (`speech.*`), recording each call. */
export function fakeNativeSpeech() {
  return {
    speak: vi.fn<NativeSpeech['speak']>(async () => ({ speaking: true })),
    cancel: vi.fn<NativeSpeech['cancel']>(async () => ({ speaking: false })),
  };
}

/** A fake `window.openflightNative` whose calls the test can inspect. */
export function fakeNative(
  overrides: Partial<Omit<OpenFlightNative, 'capabilities'>> & {
    capabilities?: Partial<OpenFlightNative['capabilities']>;
  } = {}
) {
  const { capabilities, ...rest } = overrides;
  return {
    version: 1,
    app_version: '0.1.0',
    platform: 'ios' as const,
    capabilities: {
      barometer: true,
      absolute_altitude: false,
      location: true,
      weather: true,
      audio: true,
      keep_awake: true,
      debug_capture: true,
      ...capabilities,
    },
    getSensors: vi.fn<OpenFlightNative['getSensors']>(async () => sensorSnapshot()),
    fetchWeather: vi.fn<OpenFlightNative['fetchWeather']>(async () => nativeWeatherReport()),
    ...rest,
  };
}
