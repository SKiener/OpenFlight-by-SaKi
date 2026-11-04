import { describe, expect, it } from 'vitest';
import { demoShots } from '../demoShots';
import { positionAt, restPoint, sampleFlight, type RangeShot, type ScenePoint } from '../flightPath';
import {
  CAMERA_TIMING,
  TEE_POSE,
  cameraPoseAt,
  flightSeconds,
  rollSeconds,
  restView,
  shotDuration,
  type CameraPose,
  type Vec3,
  type ViewBounds,
} from './cameraPath';

function demoFlight(id: string): ScenePoint[] {
  const flight = demoShots().find((shot) => shot.id === id)?.flight;
  if (!flight) throw new Error(`demo shot ${id} has no flight`);
  return flight;
}

const IRON = demoFlight('demo-iron8');
const DRIVER = demoFlight('demo-driver');
const WEDGE = demoFlight('demo-wedge');

const { preS, landingMoveS, landingHoldS, returnS } = CAMERA_TIMING;
const FLIGHTS: [string, ScenePoint[]][] = [
  ['IRON', IRON],
  ['DRIVER', DRIVER],
  ['WEDGE', WEDGE],
];

const shiftClock = (flight: ScenePoint[]): ScenePoint[] => flight.map((point) => ({ ...point, t: point.t + 10 }));

function phaseEnds(flightS: number, rollS = 0) {
  const flightEnd = preS + flightS;
  const rollEnd = flightEnd + rollS;
  const moveEnd = rollEnd + landingMoveS;
  const holdEnd = moveEnd + landingHoldS;
  return { flightEnd, rollEnd, moveEnd, holdEnd, end: holdEnd + returnS };
}

const smoothstep = (edge0: number, edge1: number, x: number): number => {
  const k = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return k * k * (3 - 2 * k);
};

const lerp = (from: Vec3, to: Vec3, k: number): Vec3 => ({
  x: from.x + (to.x - from.x) * k,
  y: from.y + (to.y - from.y) * k,
  z: from.z + (to.z - from.z) * k,
});

const blend = (from: CameraPose, to: CameraPose, k: number): CameraPose => ({
  position: lerp(from.position, to.position, k),
  target: lerp(from.target, to.target, k),
});

const distance = (a: Vec3, b: Vec3): number => Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z);

function expectVec(actual: Vec3, expected: Vec3): void {
  expect(actual.x).toBeCloseTo(expected.x, 9);
  expect(actual.y).toBeCloseTo(expected.y, 9);
  expect(actual.z).toBeCloseTo(expected.z, 9);
}

function expectPose(actual: CameraPose, expected: CameraPose): void {
  expectVec(actual.position, expected.position);
  expectVec(actual.target, expected.target);
}

const ARC_S = 8;
const arcFlight = (landingY = 0): ScenePoint[] => [
  { t: 0, x: 0, y: 0, z: 0 },
  { t: 4, x: 4, y: 30, z: -50 },
  { t: ARC_S, x: 8, y: landingY, z: -100 },
];
const ARC = arcFlight();
const ARC_ENDS = phaseEnds(ARC_S);
const ARC_LANDING_VIEW: CameraPose = { position: { x: -2, y: 24, z: -64 }, target: { x: 8, y: 0, z: -100 } };
const ARC_TIMES = [
  0.3,
  preS + 2,
  ARC_ENDS.flightEnd + 0.6,
  ARC_ENDS.moveEnd + 0.7,
  ARC_ENDS.holdEnd + 0.5,
  ARC_ENDS.end + 1,
];

function chaseOf(ball: Vec3, u: number): CameraPose {
  return {
    position: lerp(
      TEE_POSE.position,
      { x: ball.x * 0.6, y: Math.max(10, ball.y + 7), z: ball.z + 46 },
      smoothstep(0.08, 1, u) * 0.55
    ),
    target: lerp(TEE_POSE.target, { x: ball.x, y: ball.y * 0.55, z: ball.z - 15 }, smoothstep(0, 0.6, u) * 0.85),
  };
}

const chaseAt = (u: number): CameraPose => chaseOf(positionAt(ARC, u * ARC_S), u);

const ROLL_S = 3;
const ARC_ROLL: ScenePoint[] = [
  { t: ARC_S, x: 8, y: 0, z: -100 },
  { t: ARC_S + 0.5, x: 8.2, y: 1.5, z: -106 },
  { t: ARC_S + 1, x: 8.4, y: 0, z: -112 },
  { t: ARC_S + 2, x: 8.6, y: 0, z: -118 },
  { t: ARC_S + ROLL_S, x: 8.7, y: 0, z: -121 },
];
const ROLL_ENDS = phaseEnds(ARC_S, ROLL_S);
const ARC_REST_VIEW: CameraPose = { position: { x: -1.3, y: 24, z: -85 }, target: { x: 8.7, y: 0, z: -121 } };
const SPIN_BACK: ScenePoint[] = [
  { t: ARC_S, x: 8, y: 0, z: -100 },
  { t: ARC_S + 0.3, x: 8.02, y: 0.4, z: -100.8 },
  { t: ARC_S + 0.6, x: 8.04, y: 0, z: -101.6 },
  { t: ARC_S + 1.2, x: 8.02, y: 0, z: -100.2 },
  { t: ARC_S + 1.8, x: 8, y: 0, z: -99.4 },
  { t: ARC_S + 2.4, x: 7.99, y: 0, z: -99.1 },
];
const arcShot = (roll: ScenePoint[]): RangeShot => ({
  id: 'arc',
  club: '7-iron',
  ballSpeedMph: 120,
  carryM: 100.3,
  lateralM: 8,
  flight: ARC,
  roll,
});

describe('the tee view and the timing of a shot', () => {
  it('uses the tee pose and the timing of the spec', () => {
    expect(TEE_POSE).toEqual({ position: { x: 0, y: 11, z: 14 }, target: { x: 0, y: 0, z: -92 } });
    expect(CAMERA_TIMING).toEqual({ preS: 0.5, landingMoveS: 1.2, landingHoldS: 1.5, returnS: 1 });
  });

  it.each([0, 3.928, 5.244, 6.305])('lasts the flight plus 4.2 s (%s s flight)', (flightS) => {
    expect(shotDuration(flightS)).toBeCloseTo(flightS + 4.2, 9);
  });

  it('lasts flight plus roll plus the camera moves', () => {
    expect(shotDuration(2, 1.5)).toBe(0.5 + 2 + 1.5 + 1.2 + 1.5 + 1.0);
    expect(shotDuration(2, 0)).toBe(shotDuration(2));
  });

  it('cannot be changed by callers', () => {
    expect(() => Object.assign(TEE_POSE, { position: TEE_POSE.target })).toThrow(TypeError);
    expect(() => Object.assign(TEE_POSE.position, { y: 0 })).toThrow(TypeError);
    expect(() => Object.assign(TEE_POSE.target, { z: 0 })).toThrow(TypeError);
    expect(() => Object.assign(CAMERA_TIMING, { preS: 0 })).toThrow(TypeError);
  });
});

describe('flightSeconds', () => {
  it('measures from the first to the last sample, wherever the flight clock starts', () => {
    expect(flightSeconds(IRON)).toBeCloseTo(5.244, 9);
    expect(flightSeconds(shiftClock(IRON))).toBeCloseTo(5.244, 9);
  });

  it.each([0, 1])('rejects a flight of %s samples', (count) => {
    expect(() => flightSeconds(IRON.slice(0, count))).toThrow(RangeError);
  });
});

describe('rollSeconds', () => {
  it('measures the roll on its own clock, which goes on from the flight', () => {
    expect(rollSeconds(ARC_ROLL)).toBe(ROLL_S);
    expect(rollSeconds(SPIN_BACK)).toBeCloseTo(2.4, 9);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['empty', []],
    ['a single point', ARC_ROLL.slice(0, 1)],
  ])('is 0 without a roll (%s)', (_name, roll) => {
    expect(rollSeconds(roll)).toBe(0);
  });
});

describe('cameraPoseAt', () => {
  const ironEnd = shotDuration(flightSeconds(IRON));

  it('starts and ends at the tee pose', () => {
    expectPose(cameraPoseAt(IRON, 0), TEE_POSE);
    expectPose(cameraPoseAt(IRON, ironEnd), TEE_POSE);
  });

  it('is back at the tee at shotDuration(flightSeconds(flight)), wherever the flight clock starts', () => {
    const flight = shiftClock(IRON);
    const end = shotDuration(flightSeconds(flight));
    expect(end).toBeCloseTo(5.244 + 4.2, 9);
    expectPose(cameraPoseAt(flight, end), TEE_POSE);
    expect(distance(cameraPoseAt(flight, end - 0.1).position, TEE_POSE.position)).toBeGreaterThan(1);
  });

  it.each([-100, -0.001, 0.25, preS - 1e-9, ironEnd + 0.001, ironEnd + 100])(
    'shows the tee pose before the ball moves and after the shot (t = %s)',
    (t) => {
      expectPose(cameraPoseAt(IRON, t), TEE_POSE);
    }
  );

  describe('the phases of a shot', () => {
    it.each([0, 0.05, 0.25, 0.5, 0.75, 1])('chases the ball at flight fraction %s', (u) => {
      expectPose(cameraPoseAt(ARC, preS + u * ARC_S), chaseAt(u));
    });

    it.each([0.25, 0.5, 0.75])('swings to the landing view, %s of the way through the landing move', (k) => {
      const pose = cameraPoseAt(ARC, ARC_ENDS.flightEnd + k * landingMoveS);
      expectPose(pose, blend(chaseAt(1), ARC_LANDING_VIEW, smoothstep(0, 1, k)));
    });

    it.each([0, 0.5, 1])('holds the landing view, %s of the way through the hold', (k) => {
      expectPose(cameraPoseAt(ARC, ARC_ENDS.moveEnd + k * landingHoldS), ARC_LANDING_VIEW);
    });

    it.each([0.25, 0.5, 0.75])('returns to the tee, %s of the way through the return', (k) => {
      const pose = cameraPoseAt(ARC, ARC_ENDS.holdEnd + k * returnS);
      expectPose(pose, blend(ARC_LANDING_VIEW, TEE_POSE, smoothstep(0, 1, k)));
    });

    it.each(ARC_TIMES)('counts from the start of the shot, wherever the flight clock starts (t = %s)', (t) => {
      expectPose(cameraPoseAt(shiftClock(ARC), t), cameraPoseAt(ARC, t));
    });

    it('aims at the ground under the last sample', () => {
      expect(cameraPoseAt(arcFlight(3), ARC_ENDS.moveEnd + 0.7).target).toEqual({ x: 8, y: 0, z: -100 });
    });

    it('leaves the flight untouched in every phase', () => {
      const flight = arcFlight(3);
      for (const t of ARC_TIMES) cameraPoseAt(flight, t);
      expect(flight).toEqual(arcFlight(3));
    });
  });

  it.each(FLIGHTS)('looks at the landing point while holding (%s)', (_name, flight) => {
    const landing = flight[flight.length - 1];
    const t = preS + flightSeconds(flight) + landingMoveS + 0.5 * landingHoldS;
    const { target } = cameraPoseAt(flight, t);
    expect(Math.hypot(target.x - landing.x, target.y, target.z - landing.z)).toBeLessThan(0.5);
  });

  describe('over a whole shot', () => {
    const PHASES = ['preRoll', 'flight', 'landingMove', 'hold', 'return'] as const;
    type Phase = (typeof PHASES)[number];
    type Steps = Record<Phase, { position: number[]; target: number[] }>;
    const noSteps = () => ({ position: [] as number[], target: [] as number[] });

    /** Steps of 1/60 s plus one at each phase end, so none spans two phases; each belongs to the phase it ends in. */
    function stepsByPhase(flight: ScenePoint[]): Steps {
      const { flightEnd, moveEnd, holdEnd, end } = phaseEnds(flightSeconds(flight));
      const phaseEndTimes = [preS, flightEnd, moveEnd, holdEnd, end];
      const frames = Array.from({ length: Math.ceil(end * 60) }, (_, frame) => frame / 60);
      const times = [...new Set([...frames, ...phaseEndTimes])].sort((a, b) => a - b);
      const poses = times.map((t) => cameraPoseAt(flight, t));
      const steps: Steps = {
        preRoll: noSteps(),
        flight: noSteps(),
        landingMove: noSteps(),
        hold: noSteps(),
        return: noSteps(),
      };
      for (let i = 1; i < times.length; i++) {
        const phase = PHASES[phaseEndTimes.findIndex((phaseEnd) => times[i] <= phaseEnd)];
        steps[phase].position.push(distance(poses[i - 1].position, poses[i].position));
        steps[phase].target.push(distance(poses[i - 1].target, poses[i].target));
      }
      return steps;
    }

    const largest = (steps: number[]): number => Math.max(...steps);
    const mean = (steps: number[]): number => steps.reduce((sum, step) => sum + step, 0) / steps.length;

    it.each(FLIGHTS)('never jumps between frames (%s)', (name, flight) => {
      const steps = stepsByPhase(flight);
      for (const phase of PHASES) expect(steps[phase].position.length, `${name} ${phase} steps`).toBeGreaterThan(0);
      for (const part of ['position', 'target'] as const) {
        const label = (phase: Phase) => `${name} ${phase} ${part}`;
        expect(largest(steps.preRoll[part]), label('preRoll')).toBeLessThan(1.5);
        expect(largest(steps.flight[part]), label('flight')).toBeLessThan(1.5);
        expect(largest(steps.hold[part]), label('hold')).toBe(0);
        for (const phase of ['landingMove', 'return'] as const) {
          const limit = Math.max(1.6 * mean(steps[phase][part]), 0.01);
          expect(largest(steps[phase][part]), label(phase)).toBeLessThanOrEqual(limit);
        }
      }
    });
  });

  describe('with a roll', () => {
    it('chases the ball through the roll and holds on the rest point', () => {
      for (const k of [0, 0.25, 0.5, 0.75]) {
        const ball = positionAt(ARC_ROLL, ARC_S + k * ROLL_S);
        expectPose(cameraPoseAt(ARC, ROLL_ENDS.flightEnd + k * ROLL_S, ARC_ROLL), chaseOf(ball, 1));
      }
      const rest = restPoint(arcShot(ARC_ROLL));
      for (const k of [0, 0.5, 1]) {
        const pose = cameraPoseAt(ARC, ROLL_ENDS.moveEnd + k * landingHoldS, ARC_ROLL);
        expectPose(pose, ARC_REST_VIEW);
        expect(pose.target).toEqual({ x: rest.x, y: 0, z: rest.z });
      }
    });

    it.each([0.25, 0.5, 0.75])('swings from the ball at rest to the rest view, %s of the way', (k) => {
      const pose = cameraPoseAt(ARC, ROLL_ENDS.rollEnd + k * landingMoveS, ARC_ROLL);
      expectPose(pose, blend(chaseOf(ARC_ROLL[ARC_ROLL.length - 1], 1), ARC_REST_VIEW, smoothstep(0, 1, k)));
    });

    it.each([0.25, 0.5, 0.75])('returns from the rest view to the tee, %s of the way', (k) => {
      const pose = cameraPoseAt(ARC, ROLL_ENDS.holdEnd + k * returnS, ARC_ROLL);
      expectPose(pose, blend(ARC_REST_VIEW, TEE_POSE, smoothstep(0, 1, k)));
    });

    it('is back at the tee once flight, roll and camera moves are over', () => {
      const end = shotDuration(ARC_S, ROLL_S);
      expect(end).toBe(ROLL_ENDS.end);
      expectPose(cameraPoseAt(ARC, end, ARC_ROLL), TEE_POSE);
      expect(distance(cameraPoseAt(ARC, end - 0.1, ARC_ROLL).position, TEE_POSE.position)).toBeGreaterThan(1);
    });

    it('flies as without a roll until the touchdown', () => {
      for (const t of [0.3, preS + 2, ROLL_ENDS.flightEnd - 0.01]) {
        expectPose(cameraPoseAt(ARC, t, ARC_ROLL), cameraPoseAt(ARC, t));
      }
    });

    it.each([
      ['the roll points', ARC_ROLL],
      ['the sampled roll', sampleFlight(ARC_ROLL, 0.025)],
    ])('has no jumps across touchdown and roll (%s)', (_name, roll) => {
      const from = ROLL_ENDS.flightEnd - 0.1;
      const steps = Math.ceil((ROLL_ENDS.rollEnd + 0.1 - from) * 240);
      const poses = Array.from({ length: steps + 1 }, (_, i) => cameraPoseAt(ARC, from + i / 240, roll));
      for (const part of ['position', 'target'] as const) {
        const largestStep = Math.max(...poses.slice(1).map((pose, i) => distance(poses[i][part], pose[part])));
        expect(largestStep, part).toBeLessThan(0.25);
      }
    });

    it.each([
      ['the roll points', SPIN_BACK],
      ['the drawn roll', sampleFlight(SPIN_BACK, 0.025)],
    ])('a spin-back roll moves the ball and the chase towards the tee (%s)', (_name, roll) => {
      const touchdown = ARC[ARC.length - 1];
      const rest = restPoint(arcShot(SPIN_BACK));
      const { flightEnd, rollEnd, moveEnd } = phaseEnds(ARC_S, rollSeconds(SPIN_BACK));
      const farthest = roll.reduce((far, point) => (point.z < far.z ? point : far));
      const backFrom = flightEnd + farthest.t - roll[0].t;
      const steps = Array.from({ length: 41 }, (_, i) => backFrom + ((rollEnd - backFrom) * i) / 41);
      const balls = steps.map((t) => positionAt(roll, roll[0].t + t - flightEnd).z);
      const chases = steps.map((t) => cameraPoseAt(ARC, t, roll).target.z);

      expect(rest.z).toBeGreaterThan(touchdown.z);
      for (const [name, track] of [
        ['ball', balls],
        ['chase target', chases],
      ] as const) {
        track.slice(1).forEach((z, i) => expect(z, `${name} at step ${i + 1}`).toBeGreaterThanOrEqual(track[i]));
        expect(track.at(-1)! - track[0], name).toBeGreaterThan(1.5);
      }
      expect(balls.at(-1)).toBeGreaterThan(touchdown.z);
      expect(chases.at(-1)).toBeGreaterThan(cameraPoseAt(ARC, flightEnd, roll).target.z);
      expect(cameraPoseAt(ARC, moveEnd + 0.5 * landingHoldS, roll).target).toEqual({ x: rest.x, y: 0, z: rest.z });
    });

    it('leaves the roll untouched', () => {
      const roll = ARC_ROLL.map((point) => ({ ...point }));
      for (const t of [ROLL_ENDS.flightEnd + 1, ROLL_ENDS.rollEnd + 0.5, ROLL_ENDS.moveEnd + 0.5]) {
        cameraPoseAt(ARC, t, roll);
      }
      expect(roll).toEqual(ARC_ROLL);
    });
  });

  it.each(ARC_TIMES)('returns new objects on every call, so callers cannot disturb the next frame (t = %s)', (t) => {
    const a = cameraPoseAt(ARC, t);
    const b = cameraPoseAt(ARC, t);
    const objects = [a, a.position, a.target, b, b.position, b.target, TEE_POSE, TEE_POSE.position, TEE_POSE.target];
    expect(new Set(objects).size).toBe(objects.length);
    expectPose(a, b);
  });

  describe('checks its input', () => {
    it('accepts the shortest flight, two samples', () => {
      const straight: ScenePoint[] = [
        { t: 0, x: 0, y: 0, z: 0 },
        { t: 2, x: 0, y: 0, z: -40 },
      ];
      const { moveEnd, end } = phaseEnds(2);
      expect(cameraPoseAt(straight, moveEnd + 0.5).target).toEqual({ x: 0, y: 0, z: -40 });
      expectPose(cameraPoseAt(straight, end), TEE_POSE);
    });

    it.each([Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])('rejects the non-finite time %s', (t) => {
      expect(() => cameraPoseAt(IRON, t)).toThrow(RangeError);
    });

    it.each([0, 1])('rejects a flight of %s samples, even at the tee', (count) => {
      expect(() => cameraPoseAt(IRON.slice(0, count), 0)).toThrow(RangeError);
    });

    it.each([0, -1, Number.NaN, Number.POSITIVE_INFINITY])('rejects a flight that lasts %s s', (flightS) => {
      const flight = [ARC[0], { t: flightS, x: 5, y: 0, z: -50 }];
      expect(() => cameraPoseAt(flight, 1)).toThrow(RangeError);
    });

    it.each([-1, Number.NaN, Number.POSITIVE_INFINITY])('rejects a roll that lasts %s s', (rollS) => {
      const roll = [ARC_ROLL[0], { ...ARC_ROLL[1], t: ARC_S + rollS }];
      expect(() => cameraPoseAt(ARC, 1, roll)).toThrow(RangeError);
    });
  });
});

describe('restView', () => {
  const BOUNDS: ViewBounds = { up: 0.08, down: 0.12, side: 0.75 };
  const REST: Vec3 = { x: 8.7, y: 0, z: -121 };
  const FLAG: Vec3 = { x: -5, y: 0, z: -100 };
  const FLAG_TOP: Vec3 = { ...FLAG, y: 4.2 };
  const LANDING: Vec3 = { x: 8, y: 0, z: -100 };

  function seen({ position, target }: CameraPose, point: Vec3) {
    const length = distance(position, target);
    const f = {
      x: (target.x - position.x) / length,
      y: (target.y - position.y) / length,
      z: (target.z - position.z) / length,
    };
    const sideLength = Math.hypot(f.z, f.x);
    const r = { x: -f.z / sideLength, y: 0, z: f.x / sideLength };
    const u = { x: r.y * f.z - r.z * f.y, y: r.z * f.x - r.x * f.z, z: r.x * f.y - r.y * f.x };
    const rel = { x: point.x - position.x, y: point.y - position.y, z: point.z - position.z };
    const dot = (a: Vec3, b: Vec3) => a.x * b.x + a.y * b.y + a.z * b.z;
    const depth = dot(rel, f);
    return { right: dot(rel, r) / depth, up: dot(rel, u) / depth, depth };
  }

  function expectInside(pose: CameraPose, points: Vec3[], bounds = BOUNDS): void {
    for (const point of points) {
      const { right, up, depth } = seen(pose, point);
      const name = JSON.stringify(point);
      expect(depth, name).toBeGreaterThan(0);
      expect(Math.abs(right), name).toBeLessThanOrEqual(bounds.side + 1e-9);
      expect(up, name).toBeLessThanOrEqual(bounds.up + 1e-9);
      expect(-up, name).toBeLessThanOrEqual(bounds.down + 1e-9);
    }
  }

  const fills = (pose: CameraPose, points: Vec3[], bounds = BOUNDS): boolean =>
    points.some((point) => {
      const { right, up } = seen(pose, point);
      const near = (a: number, b: number) => Math.abs(a - b) < 1e-6;
      return near(Math.abs(right), bounds.side) || near(up, bounds.up) || near(-up, bounds.down);
    });

  it('frames the rest point and the flag, and comes no closer than it must', () => {
    const points = [REST, FLAG, FLAG_TOP];

    const pose = restView(points, BOUNDS);

    expectInside(pose, points);
    expect(fills(pose, points), 'one point on an edge of the view').toBe(true);
  });

  it('looks from behind the ball and from above, as the landing view did', () => {
    const { position, target } = restView([REST, FLAG], BOUNDS);

    expect(position.z).toBeGreaterThan(target.z);
    expect(position.y).toBeGreaterThan(target.y + 10);
    expect(position.x).toBeLessThan(target.x);
  });

  it('keeps its distance for a flag right beside the ball', () => {
    const near = { ...REST, x: REST.x + 0.5 };
    const pose = restView([REST, near], BOUNDS);

    expect(distance(pose.position, pose.target)).toBeCloseTo(Math.hypot(10, 24, 36), 9);
  });

  it.each([
    ['the flag far behind the ball', { x: -3, y: 0, z: -200 }],
    ['the flag back towards the tee', { x: 9, y: 0, z: -50 }],
    ['the flag far to the side', { x: -60, y: 0, z: -125 }],
  ])('frames %s', (_name, flag) => {
    const points = [REST, flag, { ...flag, y: 4.2 }];

    expectInside(restView(points, BOUNDS), points);
  });

  it('takes the landing mark in where it fits', () => {
    const pose = restView([REST, FLAG], BOUNDS, [LANDING]);

    expectInside(pose, [REST, FLAG, LANDING]);
  });

  it('leaves out a landing mark that would push the camera far back', () => {
    const farLanding = { x: 8, y: 0, z: 40 };
    const without = restView([REST, FLAG], BOUNDS);

    expect(restView([REST, FLAG], BOUNDS, [farLanding])).toEqual(without);
  });

  it('frames a narrower view from farther away', () => {
    const narrow = { ...BOUNDS, up: 0.04, down: 0.05 };
    const wide = restView([REST, FLAG], BOUNDS);
    const tight = restView([REST, FLAG], narrow);

    expectInside(tight, [REST, FLAG], narrow);
    expect(distance(tight.position, tight.target)).toBeGreaterThan(distance(wide.position, wide.target));
  });

  describe('as the view at rest of a shot', () => {
    const view = restView([REST, FLAG, FLAG_TOP], BOUNDS, [LANDING]);

    it('holds it, then returns from it to the tee', () => {
      for (const k of [0, 0.5, 1])
        expectPose(cameraPoseAt(ARC, ROLL_ENDS.moveEnd + k * landingHoldS, ARC_ROLL, view), view);
      const back = cameraPoseAt(ARC, ROLL_ENDS.holdEnd + 0.5 * returnS, ARC_ROLL, view);
      expectPose(back, blend(view, TEE_POSE, smoothstep(0, 1, 0.5)));
      expectPose(cameraPoseAt(ARC, ROLL_ENDS.end, ARC_ROLL, view), TEE_POSE);
    });

    it('flies and rolls as without it', () => {
      for (const t of [0.3, preS + 2, ROLL_ENDS.flightEnd + 1, ROLL_ENDS.rollEnd]) {
        expectPose(cameraPoseAt(ARC, t, ARC_ROLL, view), cameraPoseAt(ARC, t, ARC_ROLL));
      }
    });

    it.each([
      ['with a roll', ARC_ROLL],
      ['without a roll', null],
    ])('never jumps on the way there and back (%s)', (_name, roll) => {
      const ends = phaseEnds(ARC_S, rollSeconds(roll));
      const from = ends.flightEnd - 0.1;
      const steps = Math.ceil((ends.end + 0.1 - from) * 240);
      const poses = Array.from({ length: steps + 1 }, (_, i) => cameraPoseAt(ARC, from + i / 240, roll, view));
      const atRest = cameraPoseAt(ARC, ends.rollEnd, roll);
      for (const part of ['position', 'target'] as const) {
        const span = Math.max(distance(atRest[part], view[part]), distance(view[part], TEE_POSE[part]));
        const smoothstepLimit = (1.5 * span) / Math.min(landingMoveS, returnS) / 240 + 0.25;
        const largestStep = Math.max(...poses.slice(1).map((pose, i) => distance(poses[i][part], pose[part])));
        expect(largestStep, part).toBeLessThan(smoothstepLimit);
      }
    });
  });
});
