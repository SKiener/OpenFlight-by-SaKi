import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  defaultScreenWakeLockEnv,
  startScreenWakeLock,
  type ScreenWakeLockEnv,
  type WakeLockLike,
  type WakeLockSentinelLike,
} from './screenWakeLock';

class FakeSentinel implements WakeLockSentinelLike {
  released = false;
  private listeners: Array<() => void> = [];
  release = vi.fn(async () => {
    this.released = true;
    this.listeners.forEach((listener) => listener());
  });
  addEventListener(_type: 'release', listener: () => void) {
    this.listeners.push(listener);
  }
}

function fakeDoc(initial: DocumentVisibilityState = 'visible') {
  const target = new EventTarget();
  const doc = {
    visibilityState: initial,
    addEventListener: vi.fn((type: string, listener: EventListener) => target.addEventListener(type, listener)),
    removeEventListener: vi.fn((type: string, listener: EventListener) => target.removeEventListener(type, listener)),
    setVisibility(state: DocumentVisibilityState) {
      doc.visibilityState = state;
      target.dispatchEvent(new Event('visibilitychange'));
    },
    tap() {
      target.dispatchEvent(new Event('pointerdown'));
    },
  };
  return doc;
}

function setup(options: { skip?: boolean; visibility?: DocumentVisibilityState; wakeLock?: WakeLockLike | null } = {}) {
  const sentinels: FakeSentinel[] = [];
  const wakeLock: WakeLockLike =
    options.wakeLock ??
    ({
      request: vi.fn(async () => {
        const sentinel = new FakeSentinel();
        sentinels.push(sentinel);
        return sentinel;
      }),
    } as WakeLockLike);
  const doc = fakeDoc(options.visibility);
  const env: ScreenWakeLockEnv = {
    wakeLock: options.wakeLock === null ? undefined : wakeLock,
    doc: doc as unknown as ScreenWakeLockEnv['doc'],
    skip: options.skip ?? false,
  };
  return { env, doc, wakeLock, sentinels };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('startScreenWakeLock', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('requests a screen lock when the API exists and the page is visible', async () => {
    const { env, wakeLock, sentinels } = setup();
    startScreenWakeLock(env);
    await flush();
    expect(wakeLock.request).toHaveBeenCalledWith('screen');
    expect(sentinels).toHaveLength(1);
  });

  it('is a no-op without the API (plain http)', () => {
    const { env, doc } = setup({ wakeLock: null });
    const stop = startScreenWakeLock(env);
    expect(doc.addEventListener).not.toHaveBeenCalled();
    expect(() => stop()).not.toThrow();
  });

  it('is a no-op inside the companion app or the kiosk', async () => {
    const { env, wakeLock, doc } = setup({ skip: true });
    startScreenWakeLock(env);
    await flush();
    expect(wakeLock.request).not.toHaveBeenCalled();
    expect(doc.addEventListener).not.toHaveBeenCalled();
  });

  it('waits for the page to become visible', async () => {
    const { env, doc, wakeLock } = setup({ visibility: 'hidden' });
    startScreenWakeLock(env);
    await flush();
    expect(wakeLock.request).not.toHaveBeenCalled();
    doc.setVisibility('visible');
    await flush();
    expect(wakeLock.request).toHaveBeenCalledTimes(1);
  });

  it('re-acquires after the browser released the lock while hidden', async () => {
    const { env, doc, wakeLock, sentinels } = setup();
    startScreenWakeLock(env);
    await flush();
    // The browser releases the lock when the page is hidden.
    doc.setVisibility('hidden');
    await sentinels[0].release();
    await flush();
    expect(wakeLock.request).toHaveBeenCalledTimes(1);
    doc.setVisibility('visible');
    await flush();
    expect(wakeLock.request).toHaveBeenCalledTimes(2);
  });

  it('does not stack requests while a lock is held or pending', async () => {
    const { env, doc, wakeLock } = setup();
    startScreenWakeLock(env);
    doc.setVisibility('visible');
    await flush();
    doc.setVisibility('visible');
    await flush();
    expect(wakeLock.request).toHaveBeenCalledTimes(1);
  });

  it('retries a refused request on the next tap, once', async () => {
    const sentinel = new FakeSentinel();
    const request = vi
      .fn()
      .mockRejectedValueOnce(new DOMException('needs activation', 'NotAllowedError'))
      .mockResolvedValueOnce(sentinel);
    const { env, doc } = setup({ wakeLock: { request } });
    startScreenWakeLock(env);
    await flush();
    expect(request).toHaveBeenCalledTimes(1);
    doc.tap();
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
    doc.tap();
    await flush();
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('stop releases the lock and ignores later visibility changes', async () => {
    const { env, doc, wakeLock, sentinels } = setup();
    const stop = startScreenWakeLock(env);
    await flush();
    stop();
    expect(sentinels[0].release).toHaveBeenCalled();
    doc.setVisibility('hidden');
    doc.setVisibility('visible');
    await flush();
    expect(wakeLock.request).toHaveBeenCalledTimes(1);
  });

  it('releases a lock that arrives after stop', async () => {
    let resolve!: (sentinel: WakeLockSentinelLike) => void;
    const request = vi.fn(() => new Promise<WakeLockSentinelLike>((r) => (resolve = r)));
    const { env } = setup({ wakeLock: { request } });
    const stop = startScreenWakeLock(env);
    stop();
    const late = new FakeSentinel();
    resolve(late);
    await flush();
    expect(late.release).toHaveBeenCalled();
  });
});

describe('defaultScreenWakeLockEnv', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function browser(host: string, userAgent: string, wakeLock?: WakeLockLike, openflightNative?: unknown) {
    vi.stubGlobal('window', { location: { host }, openflightNative });
    vi.stubGlobal('navigator', { userAgent, platform: 'iPhone', maxTouchPoints: 5, wakeLock });
    vi.stubGlobal('document', { visibilityState: 'visible' });
  }

  const SAFARI = 'Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) Version/27.0 Mobile/15E148 Safari/604.1';
  const lock: WakeLockLike = { request: vi.fn() };

  it('skips on the Pi kiosk host', () => {
    browser('localhost:8080', 'Mozilla/5.0 (X11; Linux aarch64) Electron/33.0.0', lock);
    expect(defaultScreenWakeLockEnv().skip).toBe(true);
  });

  it('skips inside the companion app', () => {
    browser('openflight.local:8080', `${SAFARI} OpenFlightCompanion/0.1.0`, lock);
    expect(defaultScreenWakeLockEnv().skip).toBe(true);
    browser('openflight.local:8080', SAFARI, lock, { version: 1 });
    expect(defaultScreenWakeLockEnv().skip).toBe(true);
  });

  it('runs in a phone browser and picks up navigator.wakeLock', () => {
    browser('openflight.local:8080', SAFARI, lock);
    const env = defaultScreenWakeLockEnv();
    expect(env.skip).toBe(false);
    expect(env.wakeLock).toBe(lock);
  });

  it('has no wake lock over plain http (no navigator.wakeLock)', () => {
    browser('192.168.4.1:8080', SAFARI);
    const env = defaultScreenWakeLockEnv();
    expect(env.wakeLock).toBeUndefined();
    expect(() => startScreenWakeLock(env)()).not.toThrow();
  });
});
