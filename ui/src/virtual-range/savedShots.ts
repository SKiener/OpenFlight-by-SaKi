import type { SavedShot } from '../components/SessionHistoryDialog';
import type { Shot } from '../types/shot';

/** What the history hands the range: the session's date label and its shots, oldest first. */
export interface SavedRangeSession {
  label: string;
  shots: Shot[];
}

const MODES: readonly NonNullable<Shot['mode']>[] = ['rolling-buffer', 'mock', 'swing-speed'];
const SPIN_SOURCES: readonly NonNullable<Shot['carry_spin_source']>[] = [
  'measured',
  'calculated',
  'club_typical',
  'speed_model',
];

/** The history sends text fields as plain strings, "" for a missing one. */
function oneOf<T extends string>(allowed: readonly T[], value: string | null | undefined): T | undefined {
  return allowed.find((candidate) => candidate === value);
}

export function savedShotToShot(saved: SavedShot): Shot {
  const estimated = saved.estimated_carry_yards ?? 0;
  return {
    shot_number: saved.shot_number,
    timestamp: saved.timestamp,
    club: saved.club,
    profile_id: saved.profile_id || undefined,
    profile_name: saved.profile_name,
    mode: oneOf(MODES, saved.mode),
    ball_speed_mph: saved.ball_speed_mph,
    club_speed_mph: saved.club_speed_mph,
    estimated_carry_yards: estimated,
    carry_range: [estimated, estimated],
    carry_spin_adjusted: saved.carry_spin_adjusted,
    carry_spin_rpm: saved.carry_spin_rpm,
    carry_spin_source: oneOf(SPIN_SOURCES, saved.carry_spin_source) ?? null,
    carry_lateral_yards: saved.carry_lateral_yards,
    apex_yards: saved.apex_yards,
    flight_path: saved.flight_path,
    roll_path: saved.roll_path,
    roll_yards: saved.roll_yards,
    total_yards: saved.total_yards,
    launch_angle_vertical: saved.launch_angle_vertical,
    spin_rpm: saved.spin_rpm,
    camera_replay: saved.camera_replay,
    smash_factor: null,
    peak_magnitude: null,
    launch_angle_horizontal: null,
    launch_angle_confidence: null,
    angle_source: null,
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_confidence: null,
    spin_quality: null,
    spin_source: null,
  };
}

const canLand = (saved: SavedShot): boolean => (saved.carry_spin_adjusted ?? saved.estimated_carry_yards) != null;

/** Keeps the history order, oldest first. Drops a shot with no carry: savedShotToShot alone would land it at 0 yd. */
export function savedSessionShots(saved: readonly SavedShot[]): Shot[] {
  return saved.filter(canLand).map(savedShotToShot);
}
