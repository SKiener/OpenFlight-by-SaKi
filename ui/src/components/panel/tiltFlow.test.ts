import { describe, expect, it, vi } from 'vitest';
import type { MotionPermission, MotionSource } from '../../services/phoneMotion';
import type { CalibrateResult } from '../../services/phoneTiltApi';
import { tiltStatus } from '../../test-utils/tiltStatus';
import type { PhoneTiltRequest, Vec3 } from '../../types/tiltCalibration';
import type { MotionSample } from '../../utils/phoneTilt';
import {
  createTiltFlow,
  MEASURE_MS,
  MEASURE_TIMEOUT_MS,
  NO_MOTION_MS,
  SETTLE_MS,
  type TiltFlowState,
} from './tiltFlow';

const G = 9.80665;

function tilted(deg: number): Vec3 {
  const rad = (deg * Math.PI) / 180;
  return [0, G * Math.sin(rad), G * Math.cos(rad)];
}

function negated(vector: Vec3): Vec3 {
  return [-vector[0], -vector[1], -vector[2]];
}

class FakeMotion implements MotionSource {
  readonly kind = 'devicemotion' as const;
  listeners = new Set<(sample: MotionSample) => void>();
  permissionCalls = 0;
  t = 0;

  constructor(private readonly answer: MotionPermission = 'granted') {}

  requestPermission() {
    this.permissionCalls += 1;
    return Promise.resolve(this.answer);
  }

  subscribe(onSample: (sample: MotionSample) => void) {
    this.listeners.add(onSample);
    return () => this.listeners.delete(onSample);
  }

  /** `count` samples 20 ms apart (50 Hz). */
  emit(gravity: Vec3 | ((index: number) => Vec3), count = 1) {
    for (let index = 0; index < count; index += 1) {
      this.t += 20;
      const vector = typeof gravity === 'function' ? gravity(index) : gravity;
      for (const listener of [...this.listeners]) listener({ gravity: vector, t: this.t });
    }
  }

  /** Enough samples to settle and finish one 3 s position. */
  holdStill(gravity: Vec3) {
    this.emit(gravity, Math.ceil((SETTLE_MS + MEASURE_MS) / 20) + 2);
  }
}

class FakeTimers {
  pending = new Map<number, { callback: () => void; ms: number }>();
  next = 1;
  set = (callback: () => void, ms: number) => {
    const id = this.next++;
    this.pending.set(id, { callback, ms });
    return id;
  };
  clear = (id: unknown) => {
    this.pending.delete(id as number);
  };
  /** Fire the timers that would be due after `ms`. */
  elapse(ms: number) {
    for (const [id, timer] of [...this.pending]) {
      if (timer.ms <= ms) {
        this.pending.delete(id);
        timer.callback();
      }
    }
  }
}

function setup({
  permission = 'granted' as MotionPermission,
  result = { ok: true, status: tiltStatus() } as CalibrateResult,
} = {}) {
  const motion = new FakeMotion(permission);
  const timers = new FakeTimers();
  const states: TiltFlowState[] = [];
  const requests: PhoneTiltRequest[] = [];
  let resolveCalibrate: ((value: CalibrateResult) => void) | null = null;
  const calibrate = vi.fn((request: PhoneTiltRequest) => {
    requests.push(request);
    return new Promise<CalibrateResult>((resolve) => {
      resolveCalibrate = resolve;
    });
  });
  const onStatus = vi.fn();
  const flow = createTiltFlow({
    motion,
    calibrate,
    onStatus,
    onChange: (state) => states.push(state),
    clientInfo: () => ({ motion_source: 'devicemotion' }),
    nowIso: () => '2026-10-30T12:00:00.000Z',
    setTimer: timers.set,
    clearTimer: timers.clear,
  });
  const answer = async (value: CalibrateResult = result) => {
    resolveCalibrate?.(value);
    await flush();
  };
  return { motion, timers, states, requests, calibrate, onStatus, flow, answer };
}

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/** Intro → permission → live check with the given vector. */
async function toLive(env: ReturnType<typeof setup>, gravity: Vec3 = tilted(0)) {
  env.flow.begin();
  env.flow.next();
  await flush();
  env.motion.emit(gravity, 10);
}

describe('tilt calibration flow', () => {
  it('walks intro, live check, both positions and the POST', async () => {
    const env = setup();
    const { flow, motion } = env;
    expect(flow.state).toEqual({ step: 'idle', notice: null });

    flow.begin();
    expect(flow.state).toEqual({ step: 'intro', requesting: false });
    flow.next();
    // The permission request runs inside the tap, before anything is awaited.
    expect(motion.permissionCalls).toBe(1);
    expect(flow.state).toEqual({ step: 'intro', requesting: true });
    await flush();
    expect(flow.state).toEqual({ step: 'live', convention: 'w3c', pitchDeg: null });

    motion.emit(tilted(2), 10);
    expect(flow.state.step).toBe('live');
    if (flow.state.step === 'live') expect(flow.state.pitchDeg).toBeCloseTo(2, 6);

    flow.confirm();
    expect(flow.state).toEqual({ step: 'ready', position: 'a' });
    flow.measure();
    expect(flow.state).toEqual({ step: 'measuring', position: 'a', remainingS: 3, unstable: false });
    motion.holdStill(tilted(1.5));
    expect(flow.state).toEqual({ step: 'ready', position: 'b' });

    flow.measure();
    motion.holdStill(tilted(-0.5));
    expect(flow.state).toEqual({ step: 'submitting' });
    expect(motion.listeners.size).toBe(0);

    expect(env.requests).toHaveLength(1);
    const request = env.requests[0];
    expect(request.convention).toBe('w3c');
    expect(request.measured_at).toBe('2026-10-30T12:00:00.000Z');
    expect(request.client).toEqual({ motion_source: 'devicemotion' });
    expect(request.position_a.sample_count).toBeGreaterThanOrEqual(30);
    expect(request.position_a.pitch_std_deg).toBeCloseTo(0, 6);
    expect(Math.atan2(request.position_a.gravity[1], request.position_a.gravity[2]) * (180 / Math.PI)).toBeCloseTo(
      1.5,
      6
    );
    expect(Math.atan2(request.position_b.gravity[1], request.position_b.gravity[2]) * (180 / Math.PI)).toBeCloseTo(
      -0.5,
      6
    );

    await env.answer();
    expect(env.onStatus).toHaveBeenCalledWith(tiltStatus());
    expect(flow.state).toEqual({ step: 'idle', notice: { kind: 'applied', caseTiltDeg: 1.24, radarTiltDeg: 11.24 } });
  });

  it('counts down the seconds left and drops the samples of the tap', async () => {
    const env = setup();
    await toLive(env);
    env.flow.confirm();
    env.flow.measure();
    // Settling: the finger is still on the phone; a jolt here is not measured.
    env.motion.emit(tilted(20), SETTLE_MS / 20 - 1);
    expect(env.flow.state).toMatchObject({ step: 'measuring', remainingS: 3 });
    env.motion.emit(tilted(0), 60);
    expect(env.flow.state).toMatchObject({ step: 'measuring', remainingS: 2 });
    env.motion.emit(tilted(0), 50);
    expect(env.flow.state).toMatchObject({ step: 'measuring', remainingS: 1 });
    env.motion.emit(tilted(0), 50);
    expect(env.flow.state).toEqual({ step: 'ready', position: 'b' });
  });

  it('restarts a position that moved more than 0.5° and says not to touch', async () => {
    const env = setup();
    await toLive(env);
    env.flow.confirm();
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    env.flow.measure();
    // Position 2 wobbles by ±1°.
    env.motion.emit((index) => tilted(index % 2 ? 1 : -1), Math.ceil((SETTLE_MS + MEASURE_MS) / 20) + 2);
    expect(env.flow.state).toMatchObject({ step: 'measuring', position: 'b', unstable: true });
    expect(env.calibrate).not.toHaveBeenCalled();
    env.motion.holdStill(tilted(0.2));
    expect(env.flow.state).toEqual({ step: 'submitting' });
    expect(env.requests[0].position_b.pitch_std_deg).toBeLessThan(0.5);
  });

  it('guesses "inverted" when a flat phone reports z strongly negative', async () => {
    const env = setup();
    await toLive(env, negated(tilted(2)));
    expect(env.flow.state).toMatchObject({ step: 'live', convention: 'inverted' });
    if (env.flow.state.step === 'live') expect(env.flow.state.pitchDeg).toBeCloseTo(2, 6);

    env.flow.confirm();
    env.flow.measure();
    env.motion.holdStill(negated(tilted(1)));
    env.flow.measure();
    env.motion.holdStill(negated(tilted(-1)));
    expect(env.requests[0].convention).toBe('inverted');
    // The vector goes out as the phone reported it; the server flips it.
    expect(env.requests[0].position_a.gravity[2]).toBeLessThan(0);
    expect(env.requests[0].position_a.pitch_std_deg).toBeCloseTo(0, 6);
  });

  it('flips the convention with "shows it the wrong way round" and keeps that choice', async () => {
    const env = setup();
    await toLive(env, tilted(2));
    if (env.flow.state.step === 'live') expect(env.flow.state.pitchDeg).toBeCloseTo(2, 6);
    env.flow.flip();
    expect(env.flow.state).toMatchObject({ step: 'live', convention: 'inverted' });
    if (env.flow.state.step === 'live') expect(Math.abs(env.flow.state.pitchDeg!)).toBeGreaterThan(170);
    env.flow.flip();
    expect(env.flow.state).toMatchObject({ step: 'live', convention: 'w3c' });
    env.flow.flip();
    // The automatic guess does not overrule a manual choice.
    env.motion.emit(tilted(0), 10);
    expect(env.flow.state).toMatchObject({ step: 'live', convention: 'inverted' });
  });

  it('cannot confirm before the first live value', async () => {
    const env = setup();
    env.flow.begin();
    env.flow.next();
    await flush();
    env.flow.confirm();
    expect(env.flow.state).toMatchObject({ step: 'live', pitchDeg: null });
  });

  it('ends with a notice when motion access is denied or the browser has none', async () => {
    const denied = setup({ permission: 'denied' });
    denied.flow.begin();
    denied.flow.next();
    await flush();
    expect(denied.flow.state).toEqual({ step: 'idle', notice: { kind: 'denied' } });
    expect(denied.motion.listeners.size).toBe(0);

    const unsupported = setup({ permission: 'unsupported' });
    unsupported.flow.begin();
    unsupported.flow.next();
    await flush();
    expect(unsupported.flow.state).toEqual({ step: 'idle', notice: { kind: 'unsupported' } });
  });

  it('treats a granted sensor that never sends as unsupported', async () => {
    const env = setup();
    env.flow.begin();
    env.flow.next();
    await flush();
    env.timers.elapse(NO_MOTION_MS);
    expect(env.flow.state).toEqual({ step: 'idle', notice: { kind: 'unsupported' } });
    expect(env.motion.listeners.size).toBe(0);
  });

  it('fails as too_few_samples when a position never completes', async () => {
    const env = setup();
    await toLive(env);
    env.flow.confirm();
    env.flow.measure();
    env.motion.emit(tilted(0), 20);
    env.timers.elapse(MEASURE_TIMEOUT_MS);
    expect(env.flow.state).toEqual({ step: 'failed', code: 'too_few_samples' });
    expect(env.motion.listeners.size).toBe(0);
  });

  it('shows the server reason and starts again at the live check with "again"', async () => {
    const env = setup();
    await toLive(env);
    env.flow.confirm();
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    await env.answer({ ok: false, error: 'phone_bias_out_of_range' });
    expect(env.flow.state).toEqual({ step: 'failed', code: 'phone_bias_out_of_range' });
    expect(env.onStatus).not.toHaveBeenCalled();

    env.flow.retry();
    expect(env.flow.state).toEqual({ step: 'live', convention: 'w3c', pitchDeg: null });
    expect(env.motion.listeners.size).toBe(1);
    env.motion.emit(tilted(0), 10);
    env.flow.confirm();
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    // Position 1 was measured again: no stale samples from the failed run.
    expect(env.flow.state).toEqual({ step: 'ready', position: 'b' });
  });

  it('stops the motion and ignores a late reply once disposed', async () => {
    const env = setup();
    await toLive(env);
    env.flow.confirm();
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    env.flow.measure();
    env.motion.holdStill(tilted(0));
    const changes = env.states.length;
    env.flow.dispose();
    await env.answer();
    expect(env.states).toHaveLength(changes);
    expect(env.onStatus).not.toHaveBeenCalled();

    const live = setup();
    await toLive(live);
    live.flow.dispose();
    expect(live.motion.listeners.size).toBe(0);
    expect(live.timers.pending.size).toBe(0);
  });

  it('ignores taps that do not belong to the current step', () => {
    const env = setup();
    env.flow.next();
    env.flow.confirm();
    env.flow.measure();
    env.flow.retry();
    env.flow.flip();
    expect(env.flow.state).toEqual({ step: 'idle', notice: null });
    expect(env.motion.permissionCalls).toBe(0);
  });
});
