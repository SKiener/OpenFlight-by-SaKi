import { getServerOrigin } from '../../utils/serverOrigin';
import { restingPoint, YARD_M, type RangeShot } from '../flightPath';
import { readJson, writeJson, type JsonStorage } from '../storedJson';

export interface ClubAverages {
  club: string;
  total_yards: number | null;
  total_count: number;
  carry_yards: number | null;
  carry_count: number;
}

export type Zone = 'volltreffer' | 'gut' | 'getroffen' | 'outside';

export interface ZoneRule {
  zone: Exclude<Zone, 'outside'>;
  share: number;
  points: number;
}

export const ZONES: readonly ZoneRule[] = Object.freeze([
  { zone: 'volltreffer', share: 0.03, points: 10 },
  { zone: 'gut', share: 0.06, points: 5 },
  { zone: 'getroffen', share: 0.1, points: 2 },
]);

export const MIN_ZONE_HALF_M = 1;
export const MIN_TARGET_M = 10;
export const MAX_TARGET_M = 300;
export const DEFAULT_TARGET_M = 100;
const MIN_AVERAGED_SHOTS = 3;
const KEPT_SHOTS = 10;

export const TRAINING_KEY = 'openflight.virtual-range.training:v1';

export interface TrainingShot {
  deviationM: number;
  zone: Zone;
  shotId?: string;
}

export interface TrainingState {
  targetM: number;
  recent: TrainingShot[];
}

export type TrainingByPlayer = Readonly<Record<string, TrainingState>>;

export const NO_TRAINING: TrainingByPlayer = Object.freeze({});

export interface RunningFigures {
  meanDeviationM: number | null;
  shareInside: number | null;
}

export const zoneHalfWidthM = (share: number, targetM: number): number => Math.max(MIN_ZONE_HALF_M, share * targetM);

export function zoneFor(deviationM: number, targetM: number): Zone {
  const off = Math.abs(deviationM);
  return ZONES.find(({ share }) => off <= zoneHalfWidthM(share, targetM))?.zone ?? 'outside';
}

export const deviationAlongAim = (rest: { x: number; z: number }, targetM: number): number => -rest.z - targetM;

export const clampTarget = (m: number): number =>
  Number.isFinite(m) ? Math.min(MAX_TARGET_M, Math.max(MIN_TARGET_M, Math.round(m))) : DEFAULT_TARGET_M;

const averagedM = (yards: number | null, count: number): number | null =>
  yards !== null && Number.isFinite(yards) && count >= MIN_AVERAGED_SHOTS ? yards * YARD_M : null;

export function startTarget(avg: ClubAverages | null): number {
  const m = avg && (averagedM(avg.total_yards, avg.total_count) ?? averagedM(avg.carry_yards, avg.carry_count));
  return m == null ? DEFAULT_TARGET_M : clampTarget(m);
}

export const withTarget = (state: TrainingState, targetM: number): TrainingState => ({
  ...state,
  targetM: clampTarget(targetM),
});

export function recordShot(state: TrainingState, deviationM: number, shotId?: string): TrainingState {
  const shot: TrainingShot = {
    deviationM,
    zone: zoneFor(deviationM, state.targetM),
    ...(shotId === undefined ? {} : { shotId }),
  };
  return { targetM: state.targetM, recent: [...state.recent, shot].slice(-KEPT_SHOTS) };
}

export function runningFigures({ recent }: TrainingState): RunningFigures {
  if (recent.length === 0) return { meanDeviationM: null, shareInside: null };
  const sum = recent.reduce((total, { deviationM }) => total + deviationM, 0);
  const inside = recent.filter(({ zone }) => zone !== 'outside').length;
  return { meanDeviationM: sum / recent.length, shareInside: inside / recent.length };
}

export interface SeenShots {
  origin: unknown;
  ids: ReadonlySet<string>;
}

export interface FeedLike {
  shots: readonly { id: string }[];
  origin: unknown;
  seenOnReplace: readonly string[];
}

export function seeShots(
  seen: SeenShots | null,
  feed: FeedLike,
  present: readonly string[] = feed.shots.map(({ id }) => id)
): { seen: SeenShots; fresh: string[] } {
  const ids = feed.shots.map(({ id }) => id);
  if (seen === null) return { seen: { origin: feed.origin, ids: new Set([...present, ...ids]) }, fresh: [] };
  if (seen.origin !== feed.origin) {
    const known = new Set(feed.seenOnReplace);
    return { seen: { origin: feed.origin, ids: new Set(ids) }, fresh: ids.filter((id) => !known.has(id)) };
  }
  const fresh = ids.filter((id) => !seen.ids.has(id));
  return fresh.length === 0
    ? { seen, fresh }
    : { seen: { origin: seen.origin, ids: new Set([...seen.ids, ...fresh]) }, fresh };
}

export function scoreShots(
  state: TrainingState,
  shots: readonly RangeShot[],
  freshIds: readonly string[]
): TrainingState {
  return freshIds.reduce((scored, id) => {
    const shot = shots.find((candidate) => candidate.id === id);
    return shot ? recordShot(scored, deviationAlongAim(restingPoint(shot), scored.targetM), id) : scored;
  }, state);
}

export function withoutShots(state: TrainingState, ids: ReadonlySet<string>): TrainingState {
  const recent = state.recent.filter(({ shotId }) => shotId === undefined || !ids.has(shotId));
  return recent.length === state.recent.length ? state : { ...state, recent };
}

export function vanishedShots(previous: FeedLike | null, next: FeedLike): string[] {
  if (previous === null || previous.origin !== next.origin) return [];
  const ids = new Set(next.shots.map(({ id }) => id));
  return previous.shots.map(({ id }) => id).filter((id) => !ids.has(id));
}

export const trainingOf = (byPlayer: TrainingByPlayer, profileId: string): TrainingState | null =>
  Object.hasOwn(byPlayer, profileId) ? byPlayer[profileId] : null;

export function withPlayer(
  byPlayer: TrainingByPlayer,
  profileId: string,
  state: TrainingState | null
): TrainingByPlayer {
  const others = Object.entries(byPlayer).filter(([id]) => id !== profileId);
  return Object.fromEntries(state === null ? others : [...others, [profileId, state]]);
}

const ZONE_NAMES: readonly Zone[] = ['volltreffer', 'gut', 'getroffen', 'outside'];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const isFiniteNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

const isTrainingShot = (entry: unknown): entry is TrainingShot =>
  isRecord(entry) && isFiniteNumber(entry.deviationM) && ZONE_NAMES.includes(entry.zone as Zone);

function parseTraining(stored: unknown): TrainingState | null {
  if (!isRecord(stored) || !isFiniteNumber(stored.targetM) || !Array.isArray(stored.recent)) return null;
  const recent = stored.recent
    .filter(isTrainingShot)
    .map(({ deviationM, zone, shotId }) => ({ deviationM, zone, ...(typeof shotId === 'string' ? { shotId } : {}) }));
  return { targetM: clampTarget(stored.targetM), recent: recent.slice(-KEPT_SHOTS) };
}

export function loadTraining(storage: JsonStorage): TrainingByPlayer {
  const stored = readJson(storage, TRAINING_KEY);
  if (!isRecord(stored)) return NO_TRAINING;
  return Object.fromEntries(
    Object.entries(stored).flatMap(([profileId, entry]) => {
      const state = parseTraining(entry);
      return state ? [[profileId, state]] : [];
    })
  );
}

export function saveTraining(storage: JsonStorage, byPlayer: TrainingByPlayer): void {
  writeJson(storage, TRAINING_KEY, byPlayer);
}

const isCount = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 0;
const isYards = (value: unknown): value is number | null => value === null || isFiniteNumber(value);

function isClubAverages(value: unknown): value is ClubAverages {
  return (
    isRecord(value) &&
    typeof value.club === 'string' &&
    isYards(value.total_yards) &&
    isCount(value.total_count) &&
    isYards(value.carry_yards) &&
    isCount(value.carry_count)
  );
}

export async function loadClubAverages(
  club: string,
  profileId: string,
  signal: AbortSignal
): Promise<ClubAverages | null> {
  try {
    const profile = profileId ? `&profile=${encodeURIComponent(profileId)}` : '';
    const url = `${getServerOrigin()}/api/club-averages?club=${encodeURIComponent(club)}${profile}`;
    const response = await fetch(url, { signal, cache: 'no-store' });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    return isClubAverages(body) ? body : null;
  } catch {
    return null;
  }
}
