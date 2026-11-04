import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { NATIVE_EVENT } from '../native/openflightNative';
import { useWeatherStore } from '../stores/useWeatherStore';
import { fakeNative, nativeBridgeError, sensorSnapshot } from '../test-utils/nativeBridge';
import type { WeatherState } from '../types/weather';
import { socketService } from './socketService';

const page = vi.hoisted(() => ({ kiosk: false }));
vi.mock('../utils/kioskHost', () => ({ isKioskPage: () => page.kiosk, isKioskHost: () => page.kiosk }));

function fakeSocket() {
  const handlers = new Map<string, (data?: unknown) => void>();
  const emitWithAck = vi.fn(async (event: string, payload: { probe_seq?: number }) =>
    event === 'phone_clock_probe'
      ? { ok: true, probe_seq: payload.probe_seq, pi_epoch_s: 1_792_575_900, pi_monotonic_ns: 1 }
      : { ok: true }
  );
  return {
    handlers,
    connected: true,
    on(event: string, handler: (data?: unknown) => void) {
      handlers.set(event, handler);
    },
    emit: vi.fn(),
    emitWithAck,
    timeout: () => ({ emitWithAck }),
    close: vi.fn(),
    receive(event: string, data?: unknown) {
      handlers.get(event)?.(data);
    },
  };
}

function weatherState(overrides: Partial<WeatherState> = {}): WeatherState {
  return {
    location: null,
    reading: null,
    density_kg_m3: 1.184,
    density_source: 'standard',
    standard_density_kg_m3: 1.184,
    fetch_due: false,
    fresh_max_s: 10800,
    fetch_interval_s: 1800,
    ...overrides,
  };
}

let socket: ReturnType<typeof fakeSocket>;
let win: EventTarget & { openflightNative?: unknown };

function open(native: unknown) {
  win = Object.assign(new EventTarget(), native ? { openflightNative: native } : {});
  vi.stubGlobal('window', win);
  socket = fakeSocket();
  socketService.connect(() => socket as unknown as Socket);
  socket.receive('connect');
}

const emitted = (event: string) => socket.emit.mock.calls.filter(([name]) => name === event).map(([, p]) => p);
const acked = (event: string) => socket.emitWithAck.mock.calls.filter(([name]) => name === event).map(([, p]) => p);

beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'debug').mockImplementation(() => {});
  page.kiosk = false;
  useWeatherStore.setState({ state: null, receivedAt: 0, fetchStatus: 'idle', failedAt: null, succeededAt: null, consecutiveFailures: 0 });
});

afterEach(() => {
  socketService.disconnect();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('socketService inside the iOS companion', () => {
  it('forwards native onsets, sensor snapshots and clock probes over its socket', async () => {
    const native = fakeNative();
    open(native);
    await vi.waitFor(() => expect(emitted('phone_sensors')).toHaveLength(1));
    await vi.waitFor(() => expect(acked('phone_clock_probe').length).toBeGreaterThan(0));
    await vi.waitFor(() => expect(emitted('phone_clock_probe').length).toBeGreaterThan(0));

    win.dispatchEvent(new CustomEvent(NATIVE_EVENT, { detail: { type: 'audio_onset', seq: 1, capture_id: 'c1' } }));
    expect(emitted('phone_audio_onset')).toEqual([expect.objectContaining({ seq: 1, capture_id: 'c1' })]);
  });

  it('fills the range location from GPS, then fetches natively with the barometer pressure', async () => {
    const native = fakeNative();
    open(native);
    socket.receive('weather_state', weatherState());
    await vi.waitFor(() =>
      expect(acked('set_weather_location')).toEqual([{ name: '', latitude: 48.10235, longitude: 11.50012 }])
    );
    expect(native.getSensors).toHaveBeenCalledWith({ location_max_age_s: 600, timeout_ms: 15_000 });

    const location = { name: '48.1024, 11.5001', latitude: 48.10235, longitude: 11.50012 };
    socket.receive('weather_state', weatherState({ location, fetch_due: true }));
    await vi.waitFor(() => expect(acked('weather_report')).toHaveLength(1));
    expect(native.fetchWeather).toHaveBeenCalledWith({ latitude: 48.10235, longitude: 11.50012 });
    expect(acked('weather_report')[0]).toMatchObject({
      source: 'open-meteo',
      latitude: 48.10235,
      pressure_hpa: 957.4,
      pressure_source: 'phone-barometer',
      open_meteo_pressure_hpa: 960.2,
      transport: 'cellular',
    });
    // A stored location is never replaced by a later GPS fix.
    expect(acked('set_weather_location')).toHaveLength(1);
    expect(emitted('weather_fetch_failed')).toEqual([]);
  });

  it('tells the Pi why the weather fetch failed, without coordinates or error text', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('Load failed')))
    );
    const native = fakeNative({
      fetchWeather: vi.fn(async () =>
        Promise.reject(nativeBridgeError('http_error', 'Open-Meteo HTTP 503 via cellular'))
      ),
    });
    open(native);
    const location = { name: 'Golfpark', latitude: 48.10235, longitude: 11.50012 };
    socket.receive('weather_state', weatherState({ location, fetch_due: true }));

    await vi.waitFor(() => expect(emitted('weather_fetch_failed')).toHaveLength(1));
    expect(emitted('weather_fetch_failed')).toEqual([
      { stage: 'fetch', reason: 'http_503', consecutive_failures: 1, next_retry_s: 15 },
    ]);
    expect(JSON.stringify(emitted('weather_fetch_failed'))).not.toMatch(/48\.10|11\.50|Golfpark|Open-Meteo|HTTP/);
    expect(acked('weather_report')).toEqual([]);
  });

  it('tells the Pi when the phone may not give its GPS location', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const native = fakeNative({
      getSensors: vi.fn(async () =>
        sensorSnapshot({ location: { authorization: 'denied', latitude: null, longitude: null } })
      ),
    });
    open(native);
    socket.receive('weather_state', weatherState());

    await vi.waitFor(() =>
      expect(emitted('weather_fetch_failed')).toEqual([
        { stage: 'location', reason: 'no_permission', consecutive_failures: 1, next_retry_s: 15 },
      ])
    );
    expect(acked('set_weather_location')).toEqual([]);
  });

  it('drops a failure report once the socket is down', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    let fail: (error: Error) => void = () => {};
    const native = fakeNative({
      fetchWeather: vi.fn(() => new Promise<never>((_resolve, reject) => (fail = reject))),
      capabilities: { barometer: false },
    });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('Load failed')))
    );
    open(native);
    socket.receive(
      'weather_state',
      weatherState({ location: { name: 'x', latitude: 1, longitude: 2 }, fetch_due: true })
    );
    await vi.waitFor(() => expect(native.fetchWeather).toHaveBeenCalledTimes(1));

    socket.connected = false;
    fail(nativeBridgeError('network'));
    await vi.waitFor(() => expect(useWeatherStore.getState().fetchStatus).toBe('failed'));
    expect(emitted('weather_fetch_failed')).toEqual([]);
  });
});

describe('socketService without the companion', () => {
  it('on the kiosk ignores even a bridge object and sends no phone events', async () => {
    page.kiosk = true;
    const native = fakeNative();
    open(native);
    socket.receive('weather_state', weatherState());
    win.dispatchEvent(new CustomEvent(NATIVE_EVENT, { detail: { type: 'audio_onset', seq: 1 } }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(native.getSensors).not.toHaveBeenCalled();
    expect(socket.emit.mock.calls.map(([name]) => name).filter((name) => String(name).startsWith('phone_'))).toEqual(
      []
    );
    expect(socket.emitWithAck).not.toHaveBeenCalled();
    expect(emitted('weather_fetch_failed')).toEqual([]);
  });

  it('in Safari sends no phone events and stores no GPS location', async () => {
    open(null);
    socket.receive('weather_state', weatherState());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(socket.emit.mock.calls.map(([name]) => name).filter((name) => String(name).startsWith('phone_'))).toEqual(
      []
    );
    expect(acked('set_weather_location')).toEqual([]);
    expect(acked('phone_clock_probe')).toEqual([]);
  });
});
