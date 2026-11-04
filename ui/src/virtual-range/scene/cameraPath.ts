import { positionAt, type ScenePoint } from '../flightPath';

export interface Vec3 {
  readonly x: number;
  readonly y: number;
  readonly z: number;
}

export interface CameraPose {
  readonly position: Vec3;
  readonly target: Vec3;
}

export const TEE_POSE: CameraPose = Object.freeze({
  position: Object.freeze({ x: 0, y: 11, z: 14 }),
  target: Object.freeze({ x: 0, y: 0, z: -92 }),
});

export const CAMERA_TIMING = Object.freeze({ preS: 0.5, landingMoveS: 1.2, landingHoldS: 1.5, returnS: 1.0 });

const { preS, landingMoveS, landingHoldS, returnS } = CAMERA_TIMING;

function phaseEnds(flightS: number, rollS: number) {
  const flightEnd = preS + flightS;
  const rollEnd = flightEnd + rollS;
  const moveEnd = rollEnd + landingMoveS;
  const holdEnd = moveEnd + landingHoldS;
  return { flightEnd, rollEnd, moveEnd, holdEnd, end: holdEnd + returnS };
}

export function flightSeconds(flight: readonly ScenePoint[]): number {
  if (flight.length < 2) throw new RangeError(`a flight needs at least 2 samples, got ${flight.length}`);
  return flight[flight.length - 1].t - flight[0].t;
}

export function rollSeconds(roll: readonly ScenePoint[] | null | undefined): number {
  return roll && roll.length >= 2 ? roll[roll.length - 1].t - roll[0].t : 0;
}

export const shotDuration = (flightS: number, rollS = 0): number => phaseEnds(flightS, rollS).end;

function smooth(edge0: number, edge1: number, x: number): number {
  const k = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return k * k * (3 - 2 * k);
}

function lerp(from: Vec3, to: Vec3, k: number): Vec3 {
  return { x: from.x + (to.x - from.x) * k, y: from.y + (to.y - from.y) * k, z: from.z + (to.z - from.z) * k };
}

function blend(from: CameraPose, to: CameraPose, k: number): CameraPose {
  return { position: lerp(from.position, to.position, k), target: lerp(from.target, to.target, k) };
}

const copyPose = ({ position, target }: CameraPose): CameraPose => ({
  position: { ...position },
  target: { ...target },
});
const freshTeePose = (): CameraPose => copyPose(TEE_POSE);

// Offsets and blend weights are the approved probe's poseCamera.
function chasePose(ball: Vec3, progress: number): CameraPose {
  return {
    position: lerp(
      TEE_POSE.position,
      { x: ball.x * 0.6, y: Math.max(10, ball.y + 7), z: ball.z + 46 },
      smooth(0.08, 1, progress) * 0.55
    ),
    target: lerp(TEE_POSE.target, { x: ball.x, y: ball.y * 0.55, z: ball.z - 15 }, smooth(0, 0.6, progress) * 0.85),
  };
}

function landingPose(landing: Vec3): CameraPose {
  return { position: { x: landing.x - 10, y: 24, z: landing.z + 36 }, target: { ...landing } };
}

export interface ViewBounds {
  readonly up: number;
  readonly down: number;
  readonly side: number;
}

const REST_MIN_DISTANCE_M = Math.hypot(10, 24, 36);
const REST_BACK = { x: -10 / REST_MIN_DISTANCE_M, y: 24 / REST_MIN_DISTANCE_M, z: 36 / REST_MIN_DISTANCE_M };
const REST_RIGHT_LENGTH = Math.hypot(REST_BACK.x, REST_BACK.z);
const REST_RIGHT = { x: REST_BACK.z / REST_RIGHT_LENGTH, y: 0, z: -REST_BACK.x / REST_RIGHT_LENGTH };
const REST_UP = {
  x: REST_BACK.y * REST_RIGHT.z - REST_BACK.z * REST_RIGHT.y,
  y: REST_BACK.z * REST_RIGHT.x - REST_BACK.x * REST_RIGHT.z,
  z: REST_BACK.x * REST_RIGHT.y - REST_BACK.y * REST_RIGHT.x,
};
const LANDING_FITS = 1.25;

const dot = (a: Vec3, b: Vec3): number => a.x * b.x + a.y * b.y + a.z * b.z;

function restDistance(points: readonly Vec3[], centre: Vec3, { up, down, side }: ViewBounds): number {
  let distance = REST_MIN_DISTANCE_M;
  for (const point of points) {
    const offset = { x: point.x - centre.x, y: point.y - centre.y, z: point.z - centre.z };
    const nearer = dot(offset, REST_BACK);
    const across = dot(offset, REST_RIGHT);
    const height = dot(offset, REST_UP);
    const vertical = height > 0 ? height / up : -height / down;
    distance = Math.max(distance, Math.abs(across) / side + nearer, vertical + nearer);
  }
  return distance;
}

function middleOf(points: readonly Vec3[]): Vec3 {
  const axis = (pick: (point: Vec3) => number) => {
    const values = points.map(pick);
    return (Math.min(...values) + Math.max(...values)) / 2;
  };
  return { x: axis(({ x }) => x), y: axis(({ y }) => y), z: axis(({ z }) => z) };
}

function viewOf(points: readonly Vec3[], bounds: ViewBounds): { pose: CameraPose; distance: number } {
  const target = middleOf(points);
  const distance = restDistance(points, target, bounds);
  const position = {
    x: target.x + REST_BACK.x * distance,
    y: target.y + REST_BACK.y * distance,
    z: target.z + REST_BACK.z * distance,
  };
  return { pose: { position, target }, distance };
}

export function restView(points: readonly Vec3[], bounds: ViewBounds, ifItFits: readonly Vec3[] = []): CameraPose {
  const needed = viewOf(points, bounds);
  if (ifItFits.length === 0) return needed.pose;
  const all = viewOf([...points, ...ifItFits], bounds);
  return all.distance <= needed.distance * LANDING_FITS ? all.pose : needed.pose;
}

/** The camera `t` seconds into a shot (pre-roll included); the tee pose before the shot and from its end on. */
export function cameraPoseAt(
  flight: ScenePoint[],
  t: number,
  roll: ScenePoint[] | null = null,
  atRest: CameraPose | null = null
): CameraPose {
  if (!Number.isFinite(t)) throw new RangeError(`t must be a finite number of seconds, got ${t}`);
  const flightS = flightSeconds(flight);
  if (!Number.isFinite(flightS) || flightS <= 0) {
    throw new RangeError(`a flight must last a positive, finite time, got ${flightS} s`);
  }
  const rollS = rollSeconds(roll);
  if (!Number.isFinite(rollS) || rollS < 0) {
    throw new RangeError(`a roll must last a finite time of at least 0 s, got ${rollS} s`);
  }

  const first = flight[0];
  const rest = roll && rollS > 0 ? roll[roll.length - 1] : flight[flight.length - 1];
  const { flightEnd, rollEnd, moveEnd, holdEnd, end } = phaseEnds(flightS, rollS);
  if (t < preS || t >= end) return freshTeePose();
  if (t < flightEnd) {
    const elapsed = t - preS;
    return chasePose(positionAt(flight, first.t + elapsed), elapsed / flightS);
  }
  if (roll && t < rollEnd) return chasePose(positionAt(roll, roll[0].t + (t - flightEnd)), 1);

  const restPose = atRest ?? landingPose({ x: rest.x, y: 0, z: rest.z });
  if (t < moveEnd) return blend(chasePose(rest, 1), restPose, smooth(0, 1, (t - rollEnd) / landingMoveS));
  if (t < holdEnd) return atRest ? copyPose(atRest) : restPose;
  return blend(restPose, TEE_POSE, smooth(0, 1, (t - holdEnd) / returnS));
}
