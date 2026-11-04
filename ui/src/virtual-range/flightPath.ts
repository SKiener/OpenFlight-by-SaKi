import { getCarryYards, isSwingSpeedShot, type FlightPath, type FlightPathPoint, type Shot } from '../types/shot';
import { isCurveIgnored, isTypicalSpinCarry } from '../utils/carryDisplay';

export const YARD_M = 0.9144;

/** A flight sample in meters in the three.js frame (x right, y up, z negative downrange); t in seconds. */
export interface ScenePoint {
  t: number;
  x: number;
  y: number;
  z: number;
}

/** `lateralM` is positive to the right of the target line, like x in `flight`. */
export interface RangeShot {
  id: string;
  club: string;
  ballSpeedMph: number;
  carryM: number;
  lateralM: number;
  flight: ScenePoint[] | null;
  /** From the touchdown to the rest point. Missing (an older shot), null or empty all mean no roll. */
  roll?: ScenePoint[] | null;
  /** For labels only (whole yards live, unrounded from history); geometry and scoring use `restPoint`. */
  totalM?: number | null;
  /** The carry was flown with the club-typical spin (labels only: the ≈ mark). */
  typicalSpin?: boolean;
  /** The offline comes from the start direction alone (labels only). */
  curveIgnored?: boolean;
}

type Position = Pick<ScenePoint, 'x' | 'y' | 'z'>;

/** A sample this close to the landing is the landing: keeping it would leave a zero-length last step. */
const TIME_EPSILON_S = 1e-9;

const MIN_KNOT_INTERVAL = 1e-4;

/** `+ 0` turns the -0 that negating 0 yards leaves into 0. */
const yardsToMeters = (yards: number): number => yards * YARD_M + 0;

function isPoint(entry: unknown): entry is FlightPathPoint {
  return Array.isArray(entry) && entry.length === 4 && entry.every((v) => typeof v === 'number' && Number.isFinite(v));
}

export function toScenePoints(path: FlightPath | null | undefined): ScenePoint[] | null {
  if (path?.unit !== 'yd' || !Array.isArray(path.points) || path.points.length < 2) return null;
  const flight: ScenePoint[] = [];
  for (const entry of path.points as unknown[]) {
    if (!isPoint(entry)) return null;
    const [t, downrange, right, up] = entry;
    if (flight.length > 0 && t <= flight[flight.length - 1].t) return null;
    flight.push({ t, x: yardsToMeters(right), y: yardsToMeters(up), z: yardsToMeters(-downrange) });
  }
  return flight;
}

/** The server starts the roll on the flight's last point; a roll that starts elsewhere would make the ball jump. */
function toRoll(flight: ScenePoint[] | null, path: FlightPath | null): ScenePoint[] | null {
  const roll = toScenePoints(path);
  const touchdown = flight?.at(-1);
  if (!roll || !touchdown) return null;
  const { t, x, y, z } = roll[0];
  return t === touchdown.t && x === touchdown.x && y === touchdown.y && z === touchdown.z ? roll : null;
}

const toTotalM = (yards: number | null): number | null =>
  typeof yards === 'number' && Number.isFinite(yards) ? yardsToMeters(yards) : null;

/** `lateralM` is the server's offset, never read off the path. */
export function toRangeShot(shot: Shot): RangeShot | null {
  if (isSwingSpeedShot(shot)) return null;
  const flight = toScenePoints(shot.flight_path);
  const rangeShot: RangeShot = {
    id: shot.timestamp,
    club: shot.club,
    ballSpeedMph: shot.ball_speed_mph,
    carryM: yardsToMeters(getCarryYards(shot)),
    lateralM: yardsToMeters(shot.carry_lateral_yards ?? 0),
    flight,
  };
  if (isTypicalSpinCarry(shot)) rangeShot.typicalSpin = true;
  if (isCurveIgnored(shot)) rangeShot.curveIgnored = true;
  if (shot.roll_path !== undefined) rangeShot.roll = toRoll(flight, shot.roll_path);
  if (shot.total_yards !== undefined) rangeShot.totalM = toTotalM(shot.total_yards);
  return rangeShot;
}

export function restPoint(shot: RangeShot): { x: number; z: number } {
  const end = shot.roll?.at(-1) ?? shot.flight?.at(-1);
  return end ? { x: end.x, z: end.z } : { x: shot.lateralM, z: -shot.carryM + 0 };
}

const lastsAWhile = (points: readonly ScenePoint[]): boolean => {
  const seconds = points[points.length - 1].t - points[0].t;
  return Number.isFinite(seconds) && seconds > 0;
};

export function usableFlight(shot: RangeShot): ScenePoint[] | null {
  const { flight } = shot;
  return flight && flight.length >= 2 && lastsAWhile(flight) ? flight : null;
}

export function usableRoll(shot: RangeShot): ScenePoint[] | null {
  const { roll } = shot;
  return roll && roll.length >= 2 && usableFlight(shot) && lastsAWhile(roll) ? roll : null;
}

export function landingPoint(shot: RangeShot): { x: number; z: number } {
  const last = usableFlight(shot)?.at(-1);
  return last ? { x: last.x, z: last.z } : { x: shot.lateralM, z: -shot.carryM };
}

export function restingPoint(shot: RangeShot): { x: number; z: number } {
  return usableRoll(shot) ? restPoint(shot) : landingPoint(shot);
}

export function shownTotalM(shot: RangeShot): number | null {
  const { totalM } = shot;
  return usableRoll(shot) && typeof totalM === 'number' && Number.isFinite(totalM) ? totalM : null;
}

// Plain-math twin of three.js's CatmullRomCurve3(points, false, 'centripetal'), guards included. No three import here.

function knotInterval(a: Position, b: Position): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  const dz = a.z - b.z;
  return (dx * dx + dy * dy + dz * dz) ** 0.25;
}

interface IntervalsAround {
  dt0: number;
  dt1: number;
  dt2: number;
}

function intervalsAround(p0: Position, p1: Position, p2: Position, p3: Position): IntervalsAround {
  let dt1 = knotInterval(p1, p2);
  let dt0 = knotInterval(p0, p1);
  let dt2 = knotInterval(p2, p3);
  if (dt1 < MIN_KNOT_INTERVAL) dt1 = 1;
  if (dt0 < MIN_KNOT_INTERVAL) dt0 = dt1;
  if (dt2 < MIN_KNOT_INTERVAL) dt2 = dt1;
  return { dt0, dt1, dt2 };
}

function extendPast(end: Position, inner: Position): Position {
  return { x: end.x - inner.x + end.x, y: end.y - inner.y + end.y, z: end.z - inner.z + end.z };
}

function catmullRom(x0: number, x1: number, x2: number, x3: number, intervals: IntervalsAround, w: number): number {
  const { dt0, dt1, dt2 } = intervals;
  const m1 = ((x1 - x0) / dt0 - (x2 - x0) / (dt0 + dt1) + (x2 - x1) / dt1) * dt1;
  const m2 = ((x2 - x1) / dt1 - (x3 - x1) / (dt1 + dt2) + (x3 - x2) / dt2) * dt1;
  const c2 = -3 * x1 + 3 * x2 - 2 * m1 - m2;
  const c3 = 2 * x1 - 2 * x2 + m1 + m2;
  const w2 = w * w;
  return x1 + m1 * w + c2 * w2 + c3 * w2 * w;
}

function splinePoint(p0: Position, p1: Position, p2: Position, p3: Position, w: number): Position {
  const intervals = intervalsAround(p0, p1, p2, p3);
  return {
    x: catmullRom(p0.x, p1.x, p2.x, p3.x, intervals, w),
    y: catmullRom(p0.y, p1.y, p2.y, p3.y, intervals, w),
    z: catmullRom(p0.z, p1.z, p2.z, p3.z, intervals, w),
  };
}

const onOrAboveGround = (point: ScenePoint): ScenePoint => ({ ...point, y: Math.max(point.y, 0) });

/** Resamples a flight (2+ points, rising t) every `stepS` seconds on a spline; the first and last points stay exact. */
export function sampleFlight(flight: ScenePoint[], stepS: number): ScenePoint[] {
  if (!Number.isFinite(stepS) || stepS <= 0) throw new RangeError(`stepS must be a positive number, got ${stepS}`);
  if (flight.length < 2) throw new RangeError(`a flight needs at least 2 points, got ${flight.length}`);
  const lastIndex = flight.length - 1;
  const first = flight[0];
  const last = flight[lastIndex];
  // The sampling loop runs from the first time to the last: an infinite end would never stop it.
  if (!Number.isFinite(first.t) || !Number.isFinite(last.t)) {
    throw new RangeError(`a flight needs a finite start and end time, got ${first.t} and ${last.t}`);
  }
  const beforeFirst = extendPast(first, flight[1]);
  const afterLast = extendPast(last, flight[lastIndex - 1]);

  const samples: ScenePoint[] = [first];
  let k = 0;
  for (let i = 1; ; i++) {
    const t = first.t + i * stepS;
    const beforeLanding = t < last.t - TIME_EPSILON_S;
    if (!beforeLanding) break;
    while (flight[k + 1].t <= t) k++;
    const w = (t - flight[k].t) / (flight[k + 1].t - flight[k].t);
    const p0 = k > 0 ? flight[k - 1] : beforeFirst;
    const p3 = k + 1 < lastIndex ? flight[k + 2] : afterLast;
    samples.push({ t, ...splinePoint(p0, flight[k], flight[k + 1], p3, w) });
  }
  samples.push(last);
  return samples.map(onOrAboveGround);
}

export function positionAt(samples: ScenePoint[], t: number): ScenePoint {
  if (samples.length === 0) throw new RangeError('positionAt needs at least one sample');
  const first = samples[0];
  const last = samples[samples.length - 1];
  if (t <= first.t) return { ...first };
  if (t >= last.t) return { ...last };

  // samples[lo].t <= t < samples[hi].t throughout, so the step between them is never zero-length.
  let lo = 0;
  let hi = samples.length - 1;
  while (hi - lo > 1) {
    const mid = Math.floor((lo + hi) / 2);
    if (samples[mid].t <= t) lo = mid;
    else hi = mid;
  }
  const a = samples[lo];
  const b = samples[hi];
  const w = (t - a.t) / (b.t - a.t);
  return { t, x: a.x + (b.x - a.x) * w, y: a.y + (b.y - a.y) * w, z: a.z + (b.z - a.z) * w };
}
