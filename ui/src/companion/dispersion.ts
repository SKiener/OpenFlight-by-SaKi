import { carry, finite, summarize, type SavedSession, type SavedShot } from '../analysis/data';
import { isBallSpeedUncertain, isSwingSpeedShot, type Shot } from '../types/shot';

export type DispersionRange = 'session' | '7d' | '30d';
export const DISPERSION_RANGES: readonly DispersionRange[] = ['session', '7d', '30d'];
const RANGE_DAYS = { '7d': 7, '30d': 30 } as const;
const DAY_MS = 86_400_000;

/** Shortest to longest, grouped like the bag: wedges, irons, hybrids, woods. */
export const CLUB_LENGTH_ORDER: readonly string[] = [
  ...['lw', 'wedge-58', 'sw', 'wedge-54', 'gw', 'wedge-48', 'pw', 'wedge-44'],
  ...['9-iron', '8-iron', '7-iron', '6-iron', '5-iron', '4-iron', '3-iron', '2-iron'],
  ...['9-hybrid', '7-hybrid', '5-hybrid', '3-hybrid'],
  ...['7-wood', '5-wood', '3-wood', 'mini-driver', 'driver'],
];

export const CLUB_COLOR_COUNT = 8;
export const ELLIPSE_SIGMA = 2;
export const MIN_ELLIPSE_SHOTS = 5;
/** The Live tiles' "low" confidence tier. */
const LOW_DIRECTION_CONFIDENCE = 0.4;

function lengthRank(club: string): number {
  const index = CLUB_LENGTH_ORDER.indexOf(club);
  return index < 0 ? CLUB_LENGTH_ORDER.length : index;
}

export function compareClubsByLength(a: string, b: string): number {
  return lengthRank(a) - lengthRank(b) || a.localeCompare(b);
}

/** Bag neighbours land next to each other, so they take consecutive palette slots; null = not in the bag. */
export function clubColorSlot(club: string, bagClubIds: readonly string[]): number | null {
  const index = [...bagClubIds].sort(compareClubsByLength).indexOf(club);
  return index < 0 ? null : index % CLUB_COLOR_COUNT;
}

const average = (values: readonly number[]) => values.reduce((sum, value) => sum + value, 0) / values.length;

export function sampleCovariance(xs: readonly number[], ys: readonly number[]): number | null {
  if (xs.length < 2 || xs.length !== ys.length) return null;
  const meanX = average(xs);
  const meanY = average(ys);
  return xs.reduce((sum, x, index) => sum + (x - meanX) * (ys[index] - meanY), 0) / (xs.length - 1);
}

export interface Ellipse {
  cx: number;
  cy: number;
  semiMajor: number;
  semiMinor: number;
  /** Major axis, counter-clockwise from +x (right) in data coordinates. */
  angleRad: number;
}

export function ellipseFromCovariance(varX: number, varY: number, cov: number, sigma: number) {
  const middle = (varX + varY) / 2;
  const radius = Math.hypot((varX - varY) / 2, cov);
  return {
    semiMajor: sigma * Math.sqrt(middle + radius),
    semiMinor: sigma * Math.sqrt(Math.max(0, middle - radius)),
    angleRad: Math.atan2(2 * cov, varX - varY) / 2,
  };
}

export function covarianceEllipse(points: readonly { x: number; y: number }[], sigma = ELLIPSE_SIGMA): Ellipse | null {
  if (points.length < 2) return null;
  const xs = points.map((entry) => entry.x);
  const ys = points.map((entry) => entry.y);
  const axes = ellipseFromCovariance(
    sampleCovariance(xs, xs)!,
    sampleCovariance(ys, ys)!,
    sampleCovariance(xs, ys)!,
    sigma
  );
  return { cx: average(xs), cy: average(ys), ...axes };
}

export function ellipseCoverage(sigma: number): number {
  return 1 - Math.exp(-(sigma ** 2) / 2);
}

/** Exact while the spin axis is unmeasured (0): the flight then stays in its launch plane. */
export function derivedLateralYards(carryYards: number, horizontalDeg: number): number {
  return carryYards * Math.sin((horizontalDeg * Math.PI) / 180);
}

export type DirectionDoubt = 'estimated' | 'experimental' | 'low_confidence' | 'unknown_source';

export function directionDoubt(
  shot: Pick<Shot, 'launch_angle_horizontal_source' | 'launch_angle_horizontal_confidence'>
): DirectionDoubt | null {
  const source = shot.launch_angle_horizontal_source;
  if (source === 'estimated' || source === 'mock') return 'estimated';
  if (source?.includes('experimental')) return 'experimental';
  // radar_calibrated: the TI with the session's camera-referenced offset.
  if (source !== 'radar' && source !== 'radar_calibrated') return 'unknown_source';
  const confidence = shot.launch_angle_horizontal_confidence;
  return finite(confidence) && confidence < LOW_DIRECTION_CONFIDENCE ? 'low_confidence' : null;
}

export interface DispersionPoint {
  key: string;
  shot: Shot;
  club: string;
  carryYards: number;
  lateralYards: number;
  lateralDerived: boolean;
  directionDoubt: DirectionDoubt | null;
  substituteSpin: boolean;
}

export interface Exclusion {
  key: string;
  club: string;
  reason: 'no_direction' | 'no_carry';
}

export const isPoint = (entry: DispersionPoint | Exclusion): entry is DispersionPoint => !('reason' in entry);
export const isHollow = (entry: DispersionPoint) => entry.lateralDerived || entry.directionDoubt !== null;

export function classifyShot(shot: Shot): DispersionPoint | Exclusion {
  const key = shot.timestamp;
  const horizontal = shot.launch_angle_horizontal;
  if (!finite(horizontal)) return { key, club: shot.club, reason: 'no_direction' };
  const carryYards = carry(shot);
  if (carryYards === null) return { key, club: shot.club, reason: 'no_carry' };
  const lateralDerived = !finite(shot.carry_lateral_yards);
  return {
    key,
    shot,
    club: shot.club,
    carryYards,
    lateralYards: lateralDerived ? derivedLateralYards(carryYards, horizontal) : shot.carry_lateral_yards!,
    lateralDerived,
    directionDoubt: directionDoubt(shot),
    substituteSpin: shot.carry_spin_source !== 'measured',
  };
}

/** The Live tiles need a full Shot; history omits fields and sends "" for missing text. */
export function savedShotToShot(saved: SavedShot): Shot {
  const present = Object.fromEntries(Object.entries(saved).map(([key, value]) => [key, value === '' ? null : value]));
  const estimated = saved.estimated_carry_yards ?? Number.NaN;
  return {
    peak_magnitude: null,
    launch_angle_confidence: null,
    angle_source: null,
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_confidence: null,
    spin_quality: null,
    spin_source: null,
    smash_factor: null,
    ...present,
    estimated_carry_yards: estimated,
    carry_range: [estimated, estimated],
  } as Shot;
}

interface CollectOptions {
  liveShots: readonly Shot[];
  sessions: readonly SavedSession[];
  profileId: string;
  range: DispersionRange;
  now: number;
}

/** Live shots of the active profile, plus saved ones for 7/30 days; the live copy of a logged shot wins. */
export function collectShots({ liveShots, sessions, profileId, range, now }: CollectOptions): Shot[] {
  const inRange = (timestamp: string) =>
    range === 'session' || Date.parse(timestamp) >= now - RANGE_DAYS[range] * DAY_MS;
  const byTimestamp = new Map<string, Shot>();
  if (range !== 'session') {
    for (const session of sessions) {
      for (const entry of session.shots) {
        if ((!profileId || entry.profile_id === profileId) && inRange(entry.timestamp)) {
          byTimestamp.set(entry.timestamp, savedShotToShot(entry));
        }
      }
    }
  }
  for (const entry of liveShots) if (inRange(entry.timestamp)) byTimestamp.set(entry.timestamp, entry);
  return [...byTimestamp.values()]
    .filter((entry) => !isSwingSpeedShot(entry))
    .sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp));
}

export interface ClubSummary {
  club: string;
  points: DispersionPoint[];
  excluded: number;
  carry: ReturnType<typeof summarize>;
  lateral: ReturnType<typeof summarize>;
  ballSpeed: ReturnType<typeof summarize>;
  vla: ReturnType<typeof summarize>;
  hla: ReturnType<typeof summarize>;
  ellipse: Ellipse | null;
}

/**
 * `points` keeps every plotted shot (its length is the shown count, as in computeStats); the
 * statistics and the ellipse leave out an uncertain ball speed, whose carry is wrong.
 */
export function summarizeClubs(entries: readonly (DispersionPoint | Exclusion)[]): ClubSummary[] {
  const clubs = [...new Set(entries.map((entry) => entry.club))].sort(compareClubsByLength);
  return clubs.map((club) => {
    const points = entries.filter((entry): entry is DispersionPoint => entry.club === club && isPoint(entry));
    const trusted = points.filter((entry) => !isBallSpeedUncertain(entry.shot));
    return {
      club,
      points,
      excluded: entries.filter((entry) => entry.club === club && !isPoint(entry)).length,
      carry: summarize(trusted.map((entry) => entry.carryYards)),
      lateral: summarize(trusted.map((entry) => entry.lateralYards)),
      ballSpeed: summarize(trusted.map((entry) => entry.shot.ball_speed_mph)),
      vla: summarize(trusted.map((entry) => entry.shot.launch_angle_vertical)),
      hla: summarize(trusted.map((entry) => entry.shot.launch_angle_horizontal)),
      ellipse:
        trusted.length >= MIN_ELLIPSE_SHOTS
          ? covarianceEllipse(trusted.map((entry) => ({ x: entry.lateralYards, y: entry.carryYards })))
          : null,
    };
  });
}

export interface Bounds {
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
}

export function scaleEllipse(ellipse: Ellipse, factor: number): Ellipse {
  const { cx, cy, semiMajor, semiMinor, angleRad } = ellipse;
  return { cx: cx * factor, cy: cy * factor, semiMajor: semiMajor * factor, semiMinor: semiMinor * factor, angleRad };
}

export function plotBounds(points: readonly { x: number; y: number }[], ellipses: readonly Ellipse[]): Bounds {
  const xs = points.map((entry) => entry.x);
  const ys = points.map((entry) => entry.y);
  for (const { cx, cy, semiMajor, semiMinor, angleRad } of ellipses) {
    const halfX = Math.hypot(semiMajor * Math.cos(angleRad), semiMinor * Math.sin(angleRad));
    const halfY = Math.hypot(semiMajor * Math.sin(angleRad), semiMinor * Math.cos(angleRad));
    xs.push(cx - halfX, cx + halfX);
    ys.push(cy - halfY, cy + halfY);
  }
  return { xMin: Math.min(...xs), xMax: Math.max(...xs), yMin: Math.min(...ys), yMax: Math.max(...ys) };
}

/** Same pixels per unit on both axes, target line centred, carry never below zero. */
export function equalAspectViewport(bounds: Bounds, widthPx: number, heightPx: number, minSpan: number) {
  const halfWidth = Math.max(Math.abs(bounds.xMin), Math.abs(bounds.xMax), minSpan / 2) * 1.15;
  const height = Math.max(bounds.yMax - bounds.yMin, minSpan) * 1.15;
  const pxPerUnit = Math.min(widthPx / (2 * halfWidth), heightPx / height);
  const span = { x: widthPx / pxPerUnit, y: heightPx / pxPerUnit };
  const yMin = Math.max(0, (bounds.yMin + bounds.yMax) / 2 - span.y / 2);
  return { xMin: -span.x / 2, xMax: span.x / 2, yMin, yMax: yMin + span.y, pxPerUnit };
}

/** The 1, 2 or 5 × 10ⁿ step closest to `raw` on a log scale. */
export function niceStep(raw: number): number {
  const base = 10 ** Math.floor(Math.log10(raw));
  const distance = (step: number) => Math.abs(Math.log(step / raw));
  return [1, 2, 5, 10]
    .map((factor) => factor * base)
    .reduce((best, step) => (distance(step) < distance(best) ? step : best));
}

export function tickValues(min: number, max: number, step: number): number[] {
  const values: number[] = [];
  for (let index = Math.ceil(min / step); index * step <= max + step * 1e-9; index += 1) values.push(index * step || 0);
  return values;
}
