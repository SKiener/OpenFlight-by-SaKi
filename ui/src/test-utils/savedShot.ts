import type { SavedShot } from '../components/SessionHistoryDialog';

/** A saved 7-iron; its spin-adjusted carry (125 yd) differs from the estimate (140 yd). */
export function makeSavedShot(overrides: Partial<SavedShot> = {}): SavedShot {
  return {
    shot_number: 3,
    timestamp: '2026-10-07T18:12:04.512345',
    club: '7-iron',
    profile_id: 'p1',
    profile_name: 'Golfer',
    mode: 'rolling-buffer',
    ball_speed_mph: 112.4,
    club_speed_mph: 84.1,
    estimated_carry_yards: 140,
    carry_spin_adjusted: 125,
    launch_angle_vertical: 17.2,
    spin_rpm: 6200,
    ...overrides,
  };
}
