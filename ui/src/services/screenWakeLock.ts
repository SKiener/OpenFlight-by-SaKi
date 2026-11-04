import { currentBrowserEnv, isCompanionApp } from '../pairing/companionHandoff';
import { isKioskPage } from '../utils/kioskHost';

/**
 * Keeps a phone's screen on while the page is visible, through the Screen Wake Lock API.
 *
 * Only where it can and should run:
 * - The API exists only in secure contexts (https, localhost). A phone opening the Pi over
 *   plain http has no `navigator.wakeLock`, so this is a no-op there; no hidden-video hack.
 * - The companion app keeps the screen on natively (ios KeepAwake), so the page leaves it alone.
 * - The Pi's own kiosk (localhost, a secure context in Electron) keeps its own display-sleep
 *   behaviour; the page does not change it.
 *
 * The browser drops the lock whenever the page is hidden, so it is requested again each time
 * the page becomes visible. A refused request (some browsers want a user gesture) is retried on
 * the next tap.
 */

export interface WakeLockSentinelLike {
  readonly released: boolean;
  release(): Promise<void>;
  addEventListener(type: 'release', listener: () => void): void;
}

export interface WakeLockLike {
  request(type: 'screen'): Promise<WakeLockSentinelLike>;
}

export interface ScreenWakeLockEnv {
  /** `navigator.wakeLock`, undefined on insecure origins and older browsers. */
  wakeLock: WakeLockLike | undefined;
  doc: Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'>;
  /** Inside the companion app (native keep-awake) or the Pi kiosk: do nothing. */
  skip: boolean;
}

export function defaultScreenWakeLockEnv(): ScreenWakeLockEnv {
  const nav = typeof navigator === 'undefined' ? undefined : navigator;
  return {
    wakeLock: (nav as { wakeLock?: WakeLockLike } | undefined)?.wakeLock,
    doc: document,
    skip: isKioskPage() || isCompanionApp(currentBrowserEnv()),
  };
}

/** Starts holding the screen wake lock; the returned function stops and releases it. */
export function startScreenWakeLock(env: ScreenWakeLockEnv = defaultScreenWakeLockEnv()): () => void {
  const { wakeLock, doc } = env;
  if (env.skip || !wakeLock || typeof wakeLock.request !== 'function') return () => {};

  let stopped = false;
  let sentinel: WakeLockSentinelLike | null = null;
  let pending = false;
  let retryOnTap = false;

  const held = () => sentinel !== null && !sentinel.released;

  const onTap = () => {
    retryOnTap = false;
    doc.removeEventListener('pointerdown', onTap);
    void acquire();
  };

  async function acquire(): Promise<void> {
    if (stopped || pending || held() || doc.visibilityState !== 'visible') return;
    pending = true;
    try {
      const lock = await wakeLock!.request('screen');
      if (stopped) {
        void lock.release().catch(() => {});
        return;
      }
      sentinel = lock;
    } catch {
      // Refused (no user activation, battery saver, permissions policy): try again on a tap.
      if (!stopped && !retryOnTap) {
        retryOnTap = true;
        doc.addEventListener('pointerdown', onTap);
      }
    } finally {
      pending = false;
    }
  }

  const onVisibility = () => {
    if (doc.visibilityState === 'visible') void acquire();
  };

  doc.addEventListener('visibilitychange', onVisibility);
  void acquire();

  return () => {
    stopped = true;
    doc.removeEventListener('visibilitychange', onVisibility);
    if (retryOnTap) doc.removeEventListener('pointerdown', onTap);
    retryOnTap = false;
    const lock = sentinel;
    sentinel = null;
    if (lock && !lock.released) void lock.release().catch(() => {});
  };
}
