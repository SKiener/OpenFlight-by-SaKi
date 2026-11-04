import { finiteNumber, getOpenflightNative, type NativeMotion } from '../native/openflightNative';
import type { Vec3 } from '../types/tiltCalibration';
import type { MotionSample } from '../utils/phoneTilt';

export type MotionPermission = 'granted' | 'denied' | 'unsupported';

/**
 * Where the TI tilt calibration reads gravity from. Injected so tests can feed samples.
 *
 * `requestPermission` must be called straight from a tap: iOS only asks for Device Motion
 * from a user gesture, so callers do not await anything before it.
 */
export interface MotionSource {
  readonly kind: 'native' | 'devicemotion';
  requestPermission(): Promise<MotionPermission>;
  /** Starts delivering samples; the returned function stops them. */
  subscribe(onSample: (sample: MotionSample) => void): () => void;
}

/** How often the page polls the companion app for new accelerometer samples. */
export const NATIVE_POLL_MS = 100;

function finiteVector(values: unknown): Vec3 | null {
  if (!Array.isArray(values) || values.length !== 3 || !values.every(finiteNumber)) return null;
  return [values[0], values[1], values[2]];
}

/** The companion app's accelerometer (`window.openflightNative.motion`). */
export function nativeMotionSource(motion: NativeMotion): MotionSource {
  return {
    kind: 'native',
    async requestPermission() {
      try {
        return (await motion.start()).running ? 'granted' : 'unsupported';
      } catch (error) {
        return (error as { code?: unknown })?.code === 'permission_denied' ? 'denied' : 'unsupported';
      }
    },
    subscribe(onSample) {
      let afterSeq = 0;
      let busy = false;
      let stopped = false;
      // start is idempotent: a retry after a failed submit subscribes again without a new permission tap.
      void motion.start().catch(() => undefined);
      const poll = async () => {
        if (busy || stopped) return;
        busy = true;
        try {
          const reply = await motion.samples({ after_seq: afterSeq });
          for (const sample of reply.samples ?? []) {
            if (finiteNumber(sample.seq)) afterSeq = Math.max(afterSeq, sample.seq);
            const gravity = finiteVector(sample.gravity_mps2);
            if (stopped || !gravity || !finiteNumber(sample.t_ms)) continue;
            onSample({ gravity, t: sample.t_ms });
          }
        } catch {
          // A missed poll only delays samples; the flow's own timeouts handle a dead sensor.
        } finally {
          busy = false;
        }
      };
      const timer = setInterval(() => void poll(), NATIVE_POLL_MS);
      return () => {
        stopped = true;
        clearInterval(timer);
        void motion.stop().catch(() => undefined);
      };
    },
  };
}

interface DeviceMotionTarget {
  isSecureContext?: boolean;
  DeviceMotionEvent?: unknown;
  addEventListener(type: 'devicemotion', listener: (event: DeviceMotionEvent) => void): void;
  removeEventListener(type: 'devicemotion', listener: (event: DeviceMotionEvent) => void): void;
}

/** The browser's Device Motion events (secure contexts only, so not over the Pi's plain http). */
export function deviceMotionSource(
  target: DeviceMotionTarget = window,
  now: () => number = () => performance.now()
): MotionSource {
  return {
    kind: 'devicemotion',
    async requestPermission() {
      if (!target.isSecureContext || typeof target.DeviceMotionEvent === 'undefined') return 'unsupported';
      const ctor = target.DeviceMotionEvent as { requestPermission?: () => Promise<string> };
      if (typeof ctor.requestPermission !== 'function') return 'granted';
      try {
        return (await ctor.requestPermission()) === 'granted' ? 'granted' : 'denied';
      } catch {
        return 'denied';
      }
    },
    subscribe(onSample) {
      const listener = (event: DeviceMotionEvent) => {
        const a = event.accelerationIncludingGravity;
        const gravity = finiteVector([a?.x, a?.y, a?.z]);
        if (gravity) onSample({ gravity, t: now() });
      };
      target.addEventListener('devicemotion', listener);
      return () => target.removeEventListener('devicemotion', listener);
    },
  };
}

/** The companion app's accelerometer when it offers one, else the browser's Device Motion. */
export function pickMotionSource(target: Window = window): MotionSource {
  const native = getOpenflightNative(target);
  const motion = native?.capabilities.motion ? native.motion : undefined;
  if (
    motion &&
    typeof motion.start === 'function' &&
    typeof motion.samples === 'function' &&
    typeof motion.stop === 'function'
  ) {
    return nativeMotionSource(motion);
  }
  return deviceMotionSource(target);
}
