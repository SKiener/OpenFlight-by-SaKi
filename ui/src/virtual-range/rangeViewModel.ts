import type { MessageKey } from '../i18n';
import { filterShotsByProfile, type Shot } from '../types/shot';
import { NO_AIMS } from './aim';
import { shownTotalM, toRangeShot, type RangeShot } from './flightPath';
import type { FlightProgress } from './scene/createRangeScene';
import { directDistanceM, restOnRange, targetFor, type RestTarget } from './target';

// A saved session is identified by its source object (a new object opens it again); its shots are oldest first.
export type RangeSource = { kind: 'live' } | { kind: 'saved'; label: string; shots: Shot[] };

export const LIVE_SOURCE: RangeSource = { kind: 'live' };

export type RangeStatus = 'loading' | 'ready' | 'unavailable';

// The store swaps in a new object when a shot changes, so each shot object is converted once.
const rangeShots = new WeakMap<Shot, RangeShot | null>();

function rangeShotOf(shot: Shot): RangeShot | null {
  if (!rangeShots.has(shot)) rangeShots.set(shot, toRangeShot(shot));
  return rangeShots.get(shot) ?? null;
}

export function visibleRangeShots(source: RangeSource, shots: Shot[], profileId: string): RangeShot[] {
  const visible = source.kind === 'saved' ? source.shots : filterShotsByProfile(shots, profileId);
  return visible.flatMap((shot) => rangeShotOf(shot) ?? []);
}

// Only the newest shot flies or waits (`pending`), and a waiting one only while it is still in the list.
export function nextAnimation(
  previousIds: readonly string[],
  current: RangeShot[],
  sceneReady: boolean,
  pending: string | null
): { animateId: string | null; pending: string | null } {
  const newest = current.at(-1)?.id ?? null;
  const isNew = newest !== null && !previousIds.includes(newest);
  const waiting = pending !== null && current.some((shot) => shot.id === pending) ? pending : null;
  const target = isNew ? newest : waiting;
  return sceneReady ? { animateId: target, pending: null } : { animateId: null, pending: target };
}

/** The server sends a shot twice when hardware enriches it: first without a flight path, then updated with one. */
export function gainedFlight(previous: readonly RangeShot[], current: readonly RangeShot[]): string | null {
  const newest = current.at(-1);
  if (!newest?.flight) return null;
  const before = previous.find((shot) => shot.id === newest.id);
  return before !== undefined && before.flight === null ? newest.id : null;
}

/** A new origin (another profile or session) replaces the list; `seenOnReplace` are the ids that then count as seen. */
export interface ShotFeed {
  shots: RangeShot[];
  origin: string | RangeSource;
  seenOnReplace: string[];
  holding: boolean; // the newest live shot waits off the range for its final numbers
}

/** Live only. */
export interface LiveFeedOptions {
  seen?: ReadonlySet<string>; // instead of every shot: what counts as seen when today replaces a saved session
  session?: number;
}

// The Live view's provisional test. Such a shot waits off the range until its own update brings the final carry.
const isProvisional = (shot: Shot): boolean => shot.carry_spin_adjusted == null;

export function shotFeed(
  source: RangeSource,
  shots: Shot[],
  profileId: string,
  { seen, session = 0 }: LiveFeedOptions = {}
): ShotFeed {
  const visible = visibleRangeShots(source, shots, profileId);
  if (source.kind === 'saved') return { shots: visible, origin: source, seenOnReplace: [], holding: false };
  const final = shots.filter((shot) => !isProvisional(shot));
  const shown = visibleRangeShots(source, final, profileId);
  const ids = shown.map((shot) => shot.id);
  return {
    shots: shown,
    origin: `${session}/${profileId}`,
    seenOnReplace: seen ? ids.filter((id) => seen.has(id)) : ids,
    holding: visible.at(-1)?.id !== shown.at(-1)?.id,
  };
}

export interface KnownToday {
  source: RangeSource;
  profileId: string;
  session: number;
  ids: ReadonlySet<string>;
}

export function knownToday(
  previous: KnownToday | null,
  { source, profileId, session }: Omit<KnownToday, 'ids'>,
  todayIds: readonly string[]
): KnownToday {
  if (previous?.source === source && previous.profileId === profileId && previous.session === session) return previous;
  return { source, profileId, session, ids: new Set(todayIds) };
}

export function hitMeanwhile(known: KnownToday, todayIds: readonly string[]): boolean {
  return known.source.kind === 'saved' && todayIds.some((id) => !known.ids.has(id));
}

export interface RangeBar {
  club: string;
  carryM: number;
  ballSpeedMph: number;
  totalM?: number | null;
  targetM?: number | null;
  typicalSpin?: boolean;
  curveIgnored?: boolean;
}

export interface RangeAim {
  selected: number | null;
  aims: ReadonlyMap<string, number>;
  trainingTargetM?: number | null;
}

export const FREE_AIM: RangeAim = Object.freeze({ selected: null, aims: NO_AIMS });

const shownShot = (shots: readonly RangeShot[], flight: FlightProgress | null): RangeShot | undefined =>
  shots.find((candidate) => candidate.id === flight?.shotId) ?? shots.at(-1);

function targetOf(shot: RangeShot, { selected, aims, trainingTargetM = null }: RangeAim) {
  const rest = restOnRange(shot, aims.get(shot.id) ?? 0);
  return { rest, target: targetFor(rest, selected, trainingTargetM) };
}

export interface RangeReadout {
  bar: RangeBar | null;
  target: ({ shotId: string } & RestTarget) | null;
}

// Counts up while the shot flies; once landed it follows the shot, as the server may update its carry.
export function rangeReadout(
  shots: readonly RangeShot[],
  flight: FlightProgress | null,
  aim: RangeAim = FREE_AIM
): RangeReadout {
  const shot = shownShot(shots, flight);
  if (!shot) return { bar: null, target: null };
  const reported = flight?.shotId === shot.id;
  const counting = reported && !flight.landed;
  const resting = !reported || flight.rested === true;
  const { rest, target } = targetOf(shot, aim);
  return {
    bar: {
      club: shot.club,
      carryM: counting ? flight.carryM : shot.carryM,
      ballSpeedMph: shot.ballSpeedMph,
      totalM: resting ? shownTotalM(shot) : null,
      targetM: resting ? directDistanceM(rest, target) : null,
      ...(shot.typicalSpin ? { typicalSpin: true } : {}),
      ...(shot.curveIgnored ? { curveIgnored: true } : {}),
    },
    target: { shotId: shot.id, ...target },
  };
}

export function rangeNotice(status: RangeStatus): MessageKey | null {
  if (status === 'loading') return 'virtualRange.loading';
  return status === 'unavailable' ? 'virtualRange.noWebgl' : null;
}
