import { isBallSpeedUncertain, type CameraReplay } from '../types/shot';
import { convertDistanceFromYards, convertSpeedFromMph, getDistanceUnit, getSpeedUnit, type UnitSystem } from '../utils/units';

export interface SavedShot {
  shot_number: number | null;
  timestamp: string;
  club: string;
  profile_id?: string;
  profile_name: string;
  ball_speed_mph: number;
  club_speed_mph: number | null;
  estimated_carry_yards: number | null;
  carry_spin_adjusted: number | null;
  carry_spin_source?: string;
  carry_spin_rpm?: number | null;
  carry_lateral_yards?: number | null;
  launch_angle_vertical: number | null;
  launch_angle_horizontal?: number | null;
  launch_angle_horizontal_confidence?: number | null;
  launch_angle_vertical_source?: string;
  launch_angle_horizontal_source?: string;
  mode?: string;
  spin_rpm: number | null;
  spin_rpm_measured?: number | null;
  spin_confidence?: number | null;
  spin_quality?: string;
  spin_source?: string;
  spin_axis_deg?: number | null;
  spin_axis_source?: string | null;
  curve_class?: string | null;
  ball_speed_status?: 'uncertain' | null;
  ball_speed_status_reason?: string | null;
  camera_replay?: CameraReplay;
}
export interface SavedSession {
  id: string;
  started_at: string;
  shot_count: number;
  clubs: string[];
  shots: SavedShot[];
}
export type AnalysisShot = SavedShot & { key: string; session_id: string };
export interface Filters { session: string; club: string; profile: string; from: string; to: string }
export const emptyFilters: Filters = { session: '', club: '', profile: '', from: '', to: '' };
export const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export const carry = (shot: Pick<SavedShot, 'carry_spin_adjusted' | 'estimated_carry_yards'>) => finite(shot.carry_spin_adjusted) ? shot.carry_spin_adjusted : finite(shot.estimated_carry_yards) ? shot.estimated_carry_yards : null;
export const confirmedSpin = (shot: SavedShot) => shot.spin_source !== 'calculated' && shot.spin_quality === 'high' && finite(shot.spin_rpm) && (shot.spin_confidence ?? 0) >= 0.7 ? shot.spin_rpm : null;
export const profileKey = (shot: SavedShot) => shot.profile_id || shot.profile_name || '';

export function localDate(timestamp: string): string {
  const value = new Date(timestamp);
  if (!Number.isFinite(value.getTime())) return '';
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`;
}

export function flattenSessions(sessions: SavedSession[]): AnalysisShot[] {
  return sessions.flatMap((session) => session.shots.map((shot, index) => ({ ...shot, session_id: session.id, key: `${session.id}:${index}` })))
    .sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());
}

export function filterShots(shots: AnalysisShot[], filters: Filters): AnalysisShot[] {
  return shots.filter((shot) => (!filters.session || shot.session_id === filters.session)
    && (!filters.club || shot.club === filters.club)
    && (!filters.profile || profileKey(shot) === filters.profile)
    && (!filters.from || localDate(shot.timestamp) >= filters.from)
    && (!filters.to || localDate(shot.timestamp) <= filters.to));
}

export function summarize(values: (number | null | undefined)[]) {
  const numbers = values.filter(finite).sort((a, b) => a - b);
  const n = numbers.length;
  if (!n) return { n: 0, mean: null, median: null, sd: null };
  const mean = numbers.reduce((sum, value) => sum + value, 0) / n;
  return { n, mean, median: n % 2 ? numbers[(n - 1) / 2] : (numbers[n / 2 - 1] + numbers[n / 2]) / 2,
    sd: n < 2 ? null : Math.sqrt(numbers.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (n - 1)) };
}

/** Shot count of all shots; ball-speed and carry leave out an uncertain ball speed, as computeStats does. */
export function statistics(shots: SavedShot[]) {
  const trusted = shots.filter((shot) => !isBallSpeedUncertain(shot));
  return { count: shots.length, carry: summarize(trusted.map(carry)), speed: summarize(trusted.map((shot) => shot.ball_speed_mph)),
    launch: summarize(shots.map((shot) => shot.launch_angle_vertical)), spin: summarize(shots.map(confirmedSpin)),
    videos: shots.filter((shot) => shot.camera_replay).length };
}

function cell(value: unknown): string {
  if (value == null || typeof value === 'number' && !finite(value)) return '';
  let text = String(value);
  if (typeof value === 'string' && /^[\s]*[=+@-]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}
export const csv = (rows: unknown[][]) => '\ufeff' + rows.map((row) => row.map(cell).join(',')).join('\r\n') + '\r\n';

export function shotsCsv(shots: AnalysisShot[], units: UnitSystem) {
  const distance = (value: number | null | undefined) => finite(value) ? convertDistanceFromYards(value, units) : null;
  const speed = (value: number | null | undefined) => finite(value) ? convertSpeedFromMph(value, units) : null;
  return csv([['session', 'shot', 'timestamp', 'profile', 'club', `ball_speed_${getSpeedUnit(units)}`, `club_speed_${getSpeedUnit(units)}`,
    `carry_${getDistanceUnit(units)}`, `initial_carry_${getDistanceUnit(units)}`, 'carry_stage', 'carry_spin_source', 'carry_spin_rpm',
    `carry_lateral_${getDistanceUnit(units)}`, 'vertical_launch_deg', 'vertical_source', 'horizontal_launch_deg', 'horizontal_source', 'confirmed_spin_rpm', 'spin_candidate_rpm',
    'spin_quality', 'spin_confidence', 'spin_axis_deg', 'spin_axis_source', 'curve_class', 'replay_id', 'capture_fps'],
  ...shots.map((shot) => [shot.session_id, shot.shot_number, shot.timestamp, shot.profile_name, shot.club, speed(shot.ball_speed_mph), speed(shot.club_speed_mph),
    distance(carry(shot)), distance(shot.estimated_carry_yards), finite(shot.carry_spin_adjusted) ? 'final_estimate' : 'provisional', shot.carry_spin_source, shot.carry_spin_rpm,
    distance(shot.carry_lateral_yards), shot.launch_angle_vertical, shot.launch_angle_vertical_source, shot.launch_angle_horizontal, shot.launch_angle_horizontal_source,
    confirmedSpin(shot), shot.spin_rpm, shot.spin_quality, shot.spin_confidence, shot.spin_axis_deg, shot.spin_axis_source, shot.curve_class, shot.camera_replay?.id, shot.camera_replay?.capture_fps])]);
}

export function clubStatistics(shots: AnalysisShot[]) {
  return [...new Set(shots.map((shot) => shot.club))].map((club) => ({ club, ...statistics(shots.filter((shot) => shot.club === club)) }))
    .sort((a, b) => (a.carry.mean ?? 0) - (b.carry.mean ?? 0));
}

export function statsCsv(shots: AnalysisShot[], units: UnitSystem) {
  const distance = (value: number | null) => value == null ? null : convertDistanceFromYards(value, units);
  return csv([['club', 'shots', 'carry_n', `carry_mean_${getDistanceUnit(units)}`, `carry_median_${getDistanceUnit(units)}`, `carry_sample_sd_${getDistanceUnit(units)}`,
    'speed_n', `ball_speed_mean_${getSpeedUnit(units)}`, 'launch_n', 'vertical_launch_mean_deg', 'confirmed_spin_n', 'confirmed_spin_mean_rpm', 'videos'],
  ...clubStatistics(shots).map((group) => [group.club, group.count, group.carry.n, distance(group.carry.mean), distance(group.carry.median), distance(group.carry.sd),
    group.speed.n, group.speed.mean == null ? null : convertSpeedFromMph(group.speed.mean, units), group.launch.n, group.launch.mean, group.spin.n, group.spin.mean, group.videos])]);
}
