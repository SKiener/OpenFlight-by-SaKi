import { afterEach, describe, expect, it, vi } from 'vitest';
import type { NativeMotion, NativeMotionSample } from '../native/openflightNative';
import { deviceMotionSource, NATIVE_POLL_MS, nativeMotionSource, pickMotionSource } from './phoneMotion';
import type { MotionSample } from '../utils/phoneTilt';

type Listener = (event: DeviceMotionEvent) => void;

function fakeWindow(options: { secure?: boolean; ctor?: unknown } = {}) {
  const listeners = new Set<Listener>();
  return {
    listeners,
    isSecureContext: options.secure ?? true,
    // `ctor: undefined` is a browser without DeviceMotionEvent.
    DeviceMotionEvent: 'ctor' in options ? options.ctor : {},
    addEventListener: (_type: 'devicemotion', listener: Listener) => listeners.add(listener),
    removeEventListener: (_type: 'devicemotion', listener: Listener) => listeners.delete(listener),
    fire(acceleration: { x: number | null; y: number | null; z: number | null } | null) {
      for (const listener of listeners) listener({ accelerationIncludingGravity: acceleration } as DeviceMotionEvent);
    },
  };
}

function fakeNativeMotion(batches: NativeMotionSample[][] = []) {
  const calls: string[] = [];
  const motion: NativeMotion = {
    start: vi.fn(async () => {
      calls.push('start');
      return { running: true, interval_ms: 20 };
    }),
    samples: vi.fn(async ({ after_seq }: { after_seq: number }) => {
      calls.push(`samples:${after_seq}`);
      return { running: true, samples: batches.shift() ?? [] };
    }),
    stop: vi.fn(async () => {
      calls.push('stop');
      return { running: false };
    }),
  };
  return { motion, calls };
}

describe('device motion source', () => {
  it('is unsupported outside a secure context (the Pi serves plain http)', async () => {
    const source = deviceMotionSource(fakeWindow({ secure: false }));
    expect(await source.requestPermission()).toBe('unsupported');
    expect(await deviceMotionSource(fakeWindow({ ctor: undefined })).requestPermission()).toBe('unsupported');
  });

  it('asks iOS for permission and maps the answer', async () => {
    const granted = vi.fn(() => Promise.resolve('granted'));
    expect(await deviceMotionSource(fakeWindow({ ctor: { requestPermission: granted } })).requestPermission()).toBe(
      'granted'
    );
    expect(granted).toHaveBeenCalledTimes(1);
    const denied = () => Promise.resolve('denied');
    expect(await deviceMotionSource(fakeWindow({ ctor: { requestPermission: denied } })).requestPermission()).toBe(
      'denied'
    );
    // WebKit rejects a request outside a tap.
    const rejects = () => Promise.reject(new Error('NotAllowedError'));
    expect(await deviceMotionSource(fakeWindow({ ctor: { requestPermission: rejects } })).requestPermission()).toBe(
      'denied'
    );
  });

  it('needs no permission where the browser does not ask', async () => {
    expect(await deviceMotionSource(fakeWindow({ ctor: function DeviceMotionEvent() {} })).requestPermission()).toBe(
      'granted'
    );
  });

  it('delivers accelerationIncludingGravity with a monotonic time and skips empty readings', () => {
    const target = fakeWindow();
    let now = 100;
    const source = deviceMotionSource(target, () => (now += 20));
    const samples: MotionSample[] = [];
    const stop = source.subscribe((sample) => samples.push(sample));
    target.fire({ x: 0.1, y: 0.2, z: 9.8 });
    target.fire({ x: null, y: null, z: null });
    target.fire(null);
    expect(samples).toEqual([{ gravity: [0.1, 0.2, 9.8], t: 120 }]);
    stop();
    expect(target.listeners.size).toBe(0);
  });
});

describe('native motion source', () => {
  afterEach(() => vi.useRealTimers());

  it('starts the companion accelerometer and maps its errors', async () => {
    const { motion } = fakeNativeMotion();
    expect(await nativeMotionSource(motion).requestPermission()).toBe('granted');
    const denied = {
      ...motion,
      start: () => Promise.reject(Object.assign(new Error('no'), { code: 'permission_denied' })),
    };
    expect(await nativeMotionSource(denied).requestPermission()).toBe('denied');
    const missing = { ...motion, start: () => Promise.reject(Object.assign(new Error('no'), { code: 'unavailable' })) };
    expect(await nativeMotionSource(missing).requestPermission()).toBe('unsupported');
  });

  it('polls new samples after the last seq and stops the sensor on unsubscribe', async () => {
    vi.useFakeTimers();
    const { motion, calls } = fakeNativeMotion([
      [
        { seq: 1, t_ms: 1000, gravity_mps2: [0, 0.1, 9.8] },
        { seq: 2, t_ms: 1020, gravity_mps2: [0, 0.2, 9.8] },
      ],
      [{ seq: 3, t_ms: 1040, gravity_mps2: [0, Number.NaN, 9.8] }],
      [{ seq: 4, t_ms: 1060, gravity_mps2: [0, 0.3, 9.8] }],
    ]);
    const samples: MotionSample[] = [];
    const stop = nativeMotionSource(motion).subscribe((sample) => samples.push(sample));
    for (let poll = 0; poll < 3; poll += 1) await vi.advanceTimersByTimeAsync(NATIVE_POLL_MS);
    expect(samples).toEqual([
      { gravity: [0, 0.1, 9.8], t: 1000 },
      { gravity: [0, 0.2, 9.8], t: 1020 },
      { gravity: [0, 0.3, 9.8], t: 1060 },
    ]);
    // The broken sample is skipped, and not asked for again.
    expect(calls).toEqual(['start', 'samples:0', 'samples:2', 'samples:3']);
    stop();
    await vi.advanceTimersByTimeAsync(NATIVE_POLL_MS * 3);
    expect(calls.at(-1)).toBe('stop');
    expect(calls.filter((call) => call.startsWith('samples'))).toHaveLength(3);
  });
});

describe('pickMotionSource', () => {
  it('prefers the companion app when it offers motion', () => {
    const { motion } = fakeNativeMotion();
    const native = {
      version: 1,
      app_version: 'test',
      platform: 'ios',
      capabilities: { motion: true },
      getSensors: () => Promise.reject(new Error('unused')),
      fetchWeather: () => Promise.reject(new Error('unused')),
      motion,
    };
    expect(pickMotionSource({ ...fakeWindow(), openflightNative: native } as unknown as Window).kind).toBe('native');
    const older = { ...native, capabilities: {}, motion: undefined };
    expect(pickMotionSource({ ...fakeWindow(), openflightNative: older } as unknown as Window).kind).toBe(
      'devicemotion'
    );
    expect(pickMotionSource(fakeWindow() as unknown as Window).kind).toBe('devicemotion');
  });
});
