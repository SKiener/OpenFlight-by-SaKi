import type { GravityConvention, PhoneTiltPosition, Vec3 } from '../types/tiltCalibration';

/**
 * Pure math of the phone TI tilt calibration (server side: src/openflight/phone_tilt.py).
 * Vectors are `accelerationIncludingGravity` in m/s²: x right, y toward the phone top,
 * z out of the screen. A W3C phone lying face up reads about (0, 0, +9.81).
 */

export interface MotionSample {
  /** As the phone reported it (before any convention flip). */
  gravity: Vec3;
  /** Milliseconds on a monotonic clock. */
  t: number;
}

/** The server's minimum per position (`MIN_SAMPLE_COUNT`). */
export const MIN_SAMPLES = 30;
/** The server refuses a position whose pitch moved more than this (`MAX_PITCH_STD_DEG`). */
export const MAX_PITCH_STD_DEG = 0.5;
/** `z / |g|` at or below this: face down or inverted axes (the server's `MIN_FLAT_RATIO`). */
export const INVERTED_FLAT_RATIO = -0.9;
/** The live check averages this much of the latest motion. */
export const LIVE_WINDOW_MS = 500;
/** A live value that rounds to 0.0° reads "level". */
export const LEVEL_DEG = 0.05;

export type LiveLabel = 'higher' | 'lower' | 'level';

/** The vector in the W3C convention: an "inverted" phone reports every axis negated. */
export function applyConvention(gravity: Vec3, convention: GravityConvention): Vec3 {
  return convention === 'inverted' ? [-gravity[0], -gravity[1], -gravity[2]] : gravity;
}

/** Phone pitch in degrees from a W3C vector: positive when the top end is raised (`atan2(a_y, a_z)`). */
export function pitchDeg(gravity: Vec3): number {
  return (Math.atan2(gravity[1], gravity[2]) * 180) / Math.PI;
}

/** Component-wise mean; null for no vectors. */
export function meanVector(vectors: readonly Vec3[]): [number, number, number] | null {
  if (vectors.length === 0) return null;
  const sum = [0, 0, 0];
  for (const vector of vectors) {
    sum[0] += vector[0];
    sum[1] += vector[1];
    sum[2] += vector[2];
  }
  return [sum[0] / vectors.length, sum[1] / vectors.length, sum[2] / vectors.length];
}

/**
 * Population standard deviation of the per-sample pitch, in the given convention (computed
 * after the flip so a face-up "inverted" phone does not wrap around ±180°). 0 for fewer than two.
 */
export function pitchStdDeg(vectors: readonly Vec3[], convention: GravityConvention): number {
  if (vectors.length < 2) return 0;
  const pitches = vectors.map((vector) => pitchDeg(applyConvention(vector, convention)));
  const mean = pitches.reduce((sum, value) => sum + value, 0) / pitches.length;
  const variance = pitches.reduce((sum, value) => sum + (value - mean) ** 2, 0) / pitches.length;
  return Math.sqrt(variance);
}

/** The samples of the last `windowMs` before the newest one. */
export function recentSamples(samples: readonly MotionSample[], windowMs = LIVE_WINDOW_MS): MotionSample[] {
  if (samples.length === 0) return [];
  const newest = samples[samples.length - 1].t;
  return samples.filter((sample) => sample.t >= newest - windowMs);
}

/** Pitch of the mean of the last ~0.5 s in the given convention; null without samples. */
export function smoothedPitchDeg(
  samples: readonly MotionSample[],
  convention: GravityConvention,
  windowMs = LIVE_WINDOW_MS
): number | null {
  const mean = meanVector(recentSamples(samples, windowMs).map((sample) => sample.gravity));
  return mean ? pitchDeg(applyConvention(mean, convention)) : null;
}

/** "Ball side higher / lower / level" for a live pitch (phone top toward the ball). */
export function liveLabel(pitch: number): LiveLabel {
  if (Math.abs(pitch) < LEVEL_DEG) return 'level';
  return pitch > 0 ? 'higher' : 'lower';
}

/**
 * Whether a phone lying flat reports its axes negated: z strongly negative (as the server's
 * `gravity_sign_inverted`), judged on the vector exactly as reported.
 */
export function looksInverted(gravity: Vec3): boolean {
  const norm = Math.hypot(gravity[0], gravity[1], gravity[2]);
  return norm > 0 && gravity[2] / norm <= INVERTED_FLAT_RATIO;
}

/** One position for the POST: the reported mean (the server applies `convention`), pitch std, count. */
export function summarizePosition(
  samples: readonly MotionSample[],
  convention: GravityConvention
): PhoneTiltPosition | null {
  const vectors = samples.map((sample) => sample.gravity);
  const gravity = meanVector(vectors);
  if (!gravity) return null;
  return { gravity, pitch_std_deg: pitchStdDeg(vectors, convention), sample_count: vectors.length };
}

export function otherConvention(convention: GravityConvention): GravityConvention {
  return convention === 'w3c' ? 'inverted' : 'w3c';
}
