/**
 * `window.openflightNative`: the bridge of the OpenFlight iOS companion app
 * (ios/, docs/ios-companion/design.md §7). It exists only when this page runs inside the
 * companion's web view: never in Safari, never on the kiosk.
 */

/** Phone host time and wall time read together (design §7.1). */
export interface NativeClockPair {
  host_us: number;
  epoch_ms: number;
  bracket_us: number;
}

export interface NativeBarometer {
  authorization: 'authorized' | 'denied' | 'restricted' | 'not_determined';
  pressure_hpa: number | null;
  relative_altitude_m?: number | null;
  sample_host_us?: number | null;
  age_s: number | null;
}

export interface NativeLocation {
  authorization: 'when_in_use' | 'denied' | 'restricted' | 'not_determined';
  accuracy_authorization?: 'full' | 'reduced';
  latitude: number | null;
  longitude: number | null;
  horizontal_accuracy_m?: number | null;
  fix_epoch_ms?: number | null;
  age_s?: number | null;
}

/** `getSensors()` (design §7.3); only the fields the page reads are typed, the rest is forwarded as is. */
export interface SensorSnapshot {
  type: 'sensors';
  epoch_ms: number;
  tz_offset_min: number;
  clock: NativeClockPair;
  barometer: NativeBarometer | null;
  location: NativeLocation | null;
  [key: string]: unknown;
}

/** `fetchWeather()` (design §7.4): a ready `weather_report` plus extras. */
export interface NativeWeatherReport {
  source: 'open-meteo';
  latitude: number;
  longitude: number;
  temperature_c: number;
  pressure_hpa: number;
  humidity_pct: number;
  wind_speed_ms: number;
  wind_direction_deg: number;
  observed_epoch_s: number;
  epoch_ms: number;
  tz_offset_min: number;
  transport: 'cellular' | 'default';
  [key: string]: unknown;
}

/** One accelerometer reading, already in the W3C `accelerationIncludingGravity` frame and m/s². */
export interface NativeMotionSample {
  seq: number;
  /** Milliseconds since the phone booted. */
  t_ms: number;
  gravity_mps2: [number, number, number];
}

/** `motion.*` (accelerometer for the TI tilt calibration): Device Motion needs a secure context, http is not one. */
export interface NativeMotion {
  start(): Promise<{ running: boolean; interval_ms: number }>;
  samples(req: { after_seq: number }): Promise<{ running: boolean; samples: NativeMotionSample[] }>;
  stop(): Promise<{ running: boolean }>;
}

/**
 * `speech.*`: spoken shot callouts through AVSpeechSynthesizer. Unlike Web Speech it needs no
 * priming tap and is heard with the ring/silent switch on and while the impact microphone runs.
 */
export interface NativeSpeech {
  /** Queues behind anything still speaking; `interrupt` cuts that off first. */
  speak(req: { text: string; lang: string; interrupt?: boolean }): Promise<{ speaking: boolean }>;
  cancel(): Promise<{ speaking: boolean }>;
}

export interface OpenFlightNative {
  readonly version: number;
  readonly app_version: string;
  readonly platform: 'ios';
  readonly capabilities: Readonly<{
    barometer: boolean;
    absolute_altitude: boolean;
    location: boolean;
    weather: boolean;
    audio: boolean;
    keep_awake: boolean;
    debug_capture: boolean;
    /** Absent in app builds from before the TI tilt calibration. */
    motion?: boolean;
    /** Absent in app builds from before native shot callouts. */
    speech?: boolean;
  }>;
  getSensors(opts?: { location_max_age_s?: number; timeout_ms?: number }): Promise<SensorSnapshot>;
  fetchWeather(req: { latitude: number; longitude: number; timeout_ms?: number }): Promise<NativeWeatherReport>;
  readonly motion?: NativeMotion;
  readonly speech?: NativeSpeech;
}

declare global {
  interface Window {
    openflightNative?: OpenFlightNative;
  }
}

/** The bridge protocol this page speaks. */
export const NATIVE_BRIDGE_VERSION = 1;
/** Native → web events: `window.dispatchEvent(new CustomEvent(NATIVE_EVENT, { detail }))`. */
export const NATIVE_EVENT = 'openflight-native';

/** The companion's bridge when this page runs inside it and speaks protocol 1, else null. */
export function getOpenflightNative(
  target: { openflightNative?: unknown } | undefined = globalThis.window
): OpenFlightNative | null {
  const native = target?.openflightNative as Partial<OpenFlightNative> | undefined;
  if (!native || typeof native !== 'object') return null;
  if (native.version !== NATIVE_BRIDGE_VERSION) return null;
  if (typeof native.getSensors !== 'function' || typeof native.fetchWeather !== 'function') return null;
  if (!native.capabilities || typeof native.capabilities !== 'object') return null;
  return native as OpenFlightNative;
}

export function finiteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}
