import type { Profile } from '../types/profile';
import { formatLengthCm, getLengthUnit, type UnitSystem } from './units';

/** Mirrors ``openflight.profiles``: the ball's bottom above the mat on the driver's tee, in metres. */
export const DEFAULT_DRIVER_TEE_HEIGHT_M = 0.04;
export const MAX_DRIVER_TEE_HEIGHT_M = 0.08;
/** One − / + tap: 0.5 cm, i.e. 200 steps per metre (dividing keeps 0.08 exactly 0.08). */
const STEPS_PER_M = 200;

function isValidTeeHeight(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= MAX_DRIVER_TEE_HEIGHT_M;
}

/** A profile's stored tee height; the server's default when unset or invalid. */
export function driverTeeHeightM(settings: Record<string, unknown> | undefined): number {
  const stored = settings?.driver_tee_height_m;
  return isValidTeeHeight(stored) ? stored : DEFAULT_DRIVER_TEE_HEIGHT_M;
}

/** The active profile's driver tee height in metres (the default when it stores none). */
export function activeDriverTeeHeightM(roster: { profiles: Profile[]; activeProfileId: string }): number {
  return driverTeeHeightM(roster.profiles.find((profile) => profile.id === roster.activeProfileId)?.settings);
}

/** One step lower (-1) or higher (+1), on the 0.5 cm grid and clamped to 0–8 cm. */
export function stepDriverTee(heightM: number, direction: -1 | 1): number {
  const steps = Math.round(heightM * STEPS_PER_M) + direction;
  const maxSteps = Math.round(MAX_DRIVER_TEE_HEIGHT_M * STEPS_PER_M);
  return Math.min(maxSteps, Math.max(0, steps)) / STEPS_PER_M;
}

/** "4,0 cm" (locale decimal), or inches with yards like the low-point card. */
export function formatDriverTee(heightM: number, unitSystem: UnitSystem): string {
  return `${formatLengthCm(heightM * 100, unitSystem)} ${getLengthUnit(unitSystem)}`;
}
