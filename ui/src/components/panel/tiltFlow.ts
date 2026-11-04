import type { MotionSource } from '../../services/phoneMotion';
import type { CalibrateResult } from '../../services/phoneTiltApi';
import type { GravityConvention, PhoneTiltRequest, TiltCalibrationStatus } from '../../types/tiltCalibration';
import {
  looksInverted,
  MAX_PITCH_STD_DEG,
  meanVector,
  MIN_SAMPLES,
  otherConvention,
  pitchStdDeg,
  recentSamples,
  smoothedPitchDeg,
  summarizePosition,
  type MotionSample,
} from '../../utils/phoneTilt';

/**
 * The phone's TI tilt calibration as a small state machine, kept out of React so it can be
 * driven with fake motion in tests. Steps: intro → live check → position 1 → position 2 →
 * submit. Sample times come from the motion source's own clock; wall timers only catch a
 * sensor that stops sending.
 */

export type TiltPosition = 'a' | 'b';

export type TiltNotice =
  { kind: 'denied' } | { kind: 'unsupported' } | { kind: 'applied'; caseTiltDeg: number; radarTiltDeg: number };

export type TiltFlowState =
  | { step: 'idle'; notice: TiltNotice | null }
  | { step: 'intro'; requesting: boolean }
  | { step: 'live'; convention: GravityConvention; pitchDeg: number | null }
  | { step: 'ready'; position: TiltPosition }
  | { step: 'measuring'; position: TiltPosition; remainingS: number; unstable: boolean }
  | { step: 'submitting' }
  | { step: 'failed'; code: string };

/** Each position averages this long once the phone has settled. */
export const MEASURE_MS = 3000;
/** Samples right after the tap on "measure" are dropped: the finger still moves the phone. */
export const SETTLE_MS = 500;
/** A measurement that has not finished by then (too few samples) fails as `too_few_samples`. */
export const MEASURE_TIMEOUT_MS = 10_000;
/** No sample at all for this long in the live check: the browser has no motion data. */
export const NO_MOTION_MS = 3000;
/** The live value is re-rendered at most this often (sample clock). */
export const LIVE_EMIT_MS = 100;
/** The automatic "inverted" guess waits for this many samples. */
const AUTO_CONVENTION_SAMPLES = 5;

export interface TiltFlowDeps {
  motion: MotionSource;
  calibrate: (request: PhoneTiltRequest) => Promise<CalibrateResult>;
  /** The server's new status after a successful calibration (for the store). */
  onStatus?: (status: TiltCalibrationStatus) => void;
  onChange: (state: TiltFlowState) => void;
  clientInfo?: () => PhoneTiltRequest['client'];
  /** ISO time for `measured_at`. */
  nowIso?: () => string;
  setTimer?: (callback: () => void, ms: number) => unknown;
  clearTimer?: (timer: unknown) => void;
}

export interface TiltFlow {
  readonly state: TiltFlowState;
  /** "Einmessen". */
  begin(): void;
  /** "Weiter": asks for motion access (call straight from the tap). */
  next(): void;
  /** "Stimmt". */
  confirm(): void;
  /** "Zeigt falsch herum": the other sign convention for this measurement. */
  flip(): void;
  /** "Position n messen". */
  measure(): void;
  /** "Nochmal" after a failed submit: back to the live check. */
  retry(): void;
  dispose(): void;
}

export function createTiltFlow(deps: TiltFlowDeps): TiltFlow {
  const setTimer = deps.setTimer ?? ((callback, ms) => setTimeout(callback, ms));
  const clearTimer = deps.clearTimer ?? ((timer) => clearTimeout(timer as ReturnType<typeof setTimeout>));
  const nowIso = deps.nowIso ?? (() => new Date().toISOString());

  let state: TiltFlowState = { step: 'idle', notice: null };
  let disposed = false;
  let convention: GravityConvention = 'w3c';
  let conventionDecided = false;
  let unsubscribe: (() => void) | null = null;
  let timer: unknown = null;
  /** Each flow run counts up so a late permission answer or POST reply of an old run is ignored. */
  let run = 0;

  let liveSamples: MotionSample[] = [];
  let lastLiveEmitT = -Infinity;
  let measureStartT: number | null = null;
  let measureSamples: MotionSample[] = [];
  let unstable = false;
  const positions: Partial<Record<TiltPosition, MotionSample[]>> = {};

  const set = (next: TiltFlowState) => {
    if (disposed) return;
    state = next;
    deps.onChange(next);
  };

  const stopTimer = () => {
    if (timer !== null) clearTimer(timer);
    timer = null;
  };
  const startTimer = (ms: number, onFire: () => void) => {
    stopTimer();
    timer = setTimer(() => {
      timer = null;
      onFire();
    }, ms);
  };

  const stopMotion = () => {
    unsubscribe?.();
    unsubscribe = null;
  };

  const finish = (next: TiltFlowState) => {
    stopTimer();
    stopMotion();
    set(next);
  };

  const onSample = (sample: MotionSample) => {
    if (disposed) return;
    if (state.step === 'live') {
      liveSamples.push(sample);
      liveSamples = recentSamples(liveSamples);
      stopTimer();
      if (!conventionDecided && liveSamples.length >= AUTO_CONVENTION_SAMPLES) {
        conventionDecided = true;
        const mean = meanVector(liveSamples.map((entry) => entry.gravity));
        if (mean && looksInverted(mean)) convention = 'inverted';
      }
      if (sample.t - lastLiveEmitT >= LIVE_EMIT_MS || state.pitchDeg === null || state.convention !== convention) {
        lastLiveEmitT = sample.t;
        set({ step: 'live', convention, pitchDeg: smoothedPitchDeg(liveSamples, convention) });
      }
      return;
    }
    if (state.step === 'measuring') collect(state.position, sample);
  };

  const ensureMotion = () => {
    if (!unsubscribe) unsubscribe = deps.motion.subscribe(onSample);
  };

  const showLive = () => {
    liveSamples = [];
    lastLiveEmitT = -Infinity;
    ensureMotion();
    set({ step: 'live', convention, pitchDeg: null });
    startTimer(NO_MOTION_MS, () => {
      if (state.step === 'live' && state.pitchDeg === null) finish({ step: 'idle', notice: { kind: 'unsupported' } });
    });
  };

  const startMeasuring = (position: TiltPosition, wasUnstable: boolean) => {
    measureStartT = null;
    measureSamples = [];
    unstable = wasUnstable;
    set({ step: 'measuring', position, remainingS: Math.ceil(MEASURE_MS / 1000), unstable });
    startTimer(MEASURE_TIMEOUT_MS, () => {
      if (state.step === 'measuring') finish({ step: 'failed', code: 'too_few_samples' });
    });
  };

  const collect = (position: TiltPosition, sample: MotionSample) => {
    if (measureStartT === null) measureStartT = sample.t + SETTLE_MS;
    if (sample.t < measureStartT) return;
    measureSamples.push(sample);
    const elapsed = sample.t - measureStartT;
    if (elapsed < MEASURE_MS || measureSamples.length < MIN_SAMPLES) {
      const remainingS = Math.max(1, Math.ceil((MEASURE_MS - elapsed) / 1000));
      if (state.step === 'measuring' && state.remainingS !== remainingS) set({ ...state, remainingS });
      return;
    }
    const vectors = measureSamples.map((entry) => entry.gravity);
    if (pitchStdDeg(vectors, convention) > MAX_PITCH_STD_DEG) {
      startMeasuring(position, true);
      return;
    }
    positions[position] = measureSamples;
    stopTimer();
    if (position === 'a') {
      set({ step: 'ready', position: 'b' });
    } else {
      void submit();
    }
  };

  const submit = async () => {
    const a = positions.a ? summarizePosition(positions.a, convention) : null;
    const b = positions.b ? summarizePosition(positions.b, convention) : null;
    stopMotion();
    if (!a || !b) {
      set({ step: 'failed', code: 'too_few_samples' });
      return;
    }
    const thisRun = run;
    set({ step: 'submitting' });
    const result = await deps.calibrate({
      position_a: a,
      position_b: b,
      measured_at: nowIso(),
      convention,
      client: deps.clientInfo?.() ?? {},
    });
    if (disposed || thisRun !== run || state.step !== 'submitting') return;
    if (!result.ok) {
      set({ step: 'failed', code: result.error });
      return;
    }
    deps.onStatus?.(result.status);
    const record = result.status.calibration;
    set({
      step: 'idle',
      notice: record
        ? { kind: 'applied', caseTiltDeg: record.case_tilt_deg, radarTiltDeg: record.radar_tilt_deg }
        : null,
    });
  };

  return {
    get state() {
      return state;
    },
    begin() {
      if (state.step !== 'idle') return;
      run += 1;
      convention = 'w3c';
      conventionDecided = false;
      delete positions.a;
      delete positions.b;
      set({ step: 'intro', requesting: false });
    },
    next() {
      if (state.step !== 'intro' || state.requesting) return;
      const thisRun = run;
      // No await before this call: iOS grants Device Motion only inside the tap.
      const permission = deps.motion.requestPermission();
      set({ step: 'intro', requesting: true });
      void permission.then((answer) => {
        if (disposed || thisRun !== run || state.step !== 'intro') return;
        if (answer === 'granted') showLive();
        else finish({ step: 'idle', notice: { kind: answer } });
      });
    },
    confirm() {
      if (state.step !== 'live' || state.pitchDeg === null) return;
      stopTimer();
      set({ step: 'ready', position: 'a' });
    },
    flip() {
      if (state.step !== 'live') return;
      convention = otherConvention(convention);
      conventionDecided = true;
      set({ step: 'live', convention, pitchDeg: smoothedPitchDeg(liveSamples, convention) });
    },
    measure() {
      if (state.step !== 'ready') return;
      ensureMotion();
      startMeasuring(state.position, false);
    },
    retry() {
      if (state.step !== 'failed') return;
      delete positions.a;
      delete positions.b;
      showLive();
    },
    dispose() {
      stopTimer();
      stopMotion();
      disposed = true;
    },
  };
}
