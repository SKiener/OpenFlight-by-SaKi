/**
 * Session statistics of the calculated club data (plan D6): computed in the UI
 * only, like every other StatsPanel statistic, from the gated per-shot values
 * the server sent. A null value is "not available" and never counts.
 */

/** Shots before a session mean (path, attack angle) is shown. */
export const MEAN_SEM_N_MIN = 3;

export interface MeanSem {
  n: number;
  mean: number;
  /** Standard error of the mean: sample SD / √n. */
  sem: number;
}

function finite(values: readonly (number | null | undefined)[]): number[] {
  return values.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
}

function mean(values: readonly number[]): number {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function sampleSd(values: readonly number[], centre: number): number {
  return Math.sqrt(values.reduce((sum, value) => sum + (value - centre) ** 2, 0) / (values.length - 1));
}

/** Mean ± SEM of the available values, or null below {@link MEAN_SEM_N_MIN}. */
export function meanSem(values: readonly (number | null | undefined)[]): MeanSem | null {
  const xs = finite(values);
  if (xs.length < MEAN_SEM_N_MIN) return null;
  const m = mean(xs);
  return { n: xs.length, mean: m, sem: sampleSd(xs, m) / Math.sqrt(xs.length) };
}
