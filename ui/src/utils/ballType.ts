import type { Profile } from '../types/profile';

/** Mirrors ``openflight.profiles.BALL_TYPE_SPIN_TABLES``: the ball whose club-typical spin the carry flies. */
export const BALL_TYPES = ['standard', 'range'] as const;
export type BallType = (typeof BALL_TYPES)[number];

export function isBallType(value: unknown): value is BallType {
  return typeof value === 'string' && (BALL_TYPES as readonly string[]).includes(value);
}

/**
 * The active profile's effective ball type: the server's ``ball_type`` (its setting, else the
 * launcher's --typical-spin-table), then its stored setting, then "standard".
 */
export function activeBallType(roster: { profiles: Profile[]; activeProfileId: string }): BallType {
  const profile = roster.profiles.find((entry) => entry.id === roster.activeProfileId);
  if (isBallType(profile?.ball_type)) return profile.ball_type;
  const stored = profile?.settings?.ball_type;
  return isBallType(stored) ? stored : 'standard';
}
