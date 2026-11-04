import { describe, expect, it } from 'vitest';
import { makeShot } from '../test-utils/shot';
import type { FlightPathPoint, Shot } from '../types/shot';
import { positionAt, restPoint, sampleFlight, toRangeShot, toScenePoints, YARD_M, type ScenePoint } from './flightPath';

const LINE: FlightPathPoint[] = [
  [0, 0, 0, 0],
  [1, 100, 10, 20],
];

/** A server-style path: a point every 50 ms, then the landing at `flightS`. Lands 150 yd out, 2 yd right. */
function arcPath(flightS: number): FlightPathPoint[] {
  const times: number[] = [];
  for (let hundredths = 0; hundredths < Math.round(flightS * 100); hundredths += 5) times.push(hundredths / 100);
  times.push(flightS);
  return times.map((t): FlightPathPoint => {
    const u = t / flightS;
    return [t, 150 * u, 2 * u, 120 * u * (1 - u)];
  });
}

function rollPoints(flightS: number): FlightPathPoint[] {
  return [
    [flightS, 150, 2, 0],
    [flightS + 0.5, 156, 2.2, 0.4],
    [flightS + 1.25, 162, 2.4, 0],
  ];
}

function rollingShot(roll: FlightPathPoint[] | null, overrides: Partial<Shot> = {}): Shot {
  return makeShot({
    flight_path: { unit: 'yd', points: arcPath(5.2) },
    roll_path: roll && { unit: 'yd', points: roll },
    ...overrides,
  });
}

function flightOf(points: FlightPathPoint[]): ScenePoint[] {
  const flight = toRangeShot(makeShot({ flight_path: { unit: 'yd', points } }))?.flight;
  if (!flight) throw new Error('the fixture path was rejected');
  return flight;
}

function expectClose(actual: ScenePoint, expected: ScenePoint): void {
  expect(actual.t).toBeCloseTo(expected.t, 9);
  expect(actual.x).toBeCloseTo(expected.x, 9);
  expect(actual.y).toBeCloseTo(expected.y, 9);
  expect(actual.z).toBeCloseTo(expected.z, 9);
}

function expectNoNegativeZero(points: ScenePoint[]): void {
  for (const point of points) {
    for (const value of [point.t, point.x, point.y, point.z]) expect(Object.is(value, -0)).toBe(false);
  }
}

const GOOD_POINTS: FlightPathPoint[] = [
  [0, 0, 0, 0],
  [1, 40, 1, 5],
  [2, 80, 1, 0],
];

const withMiddle = (point: unknown): unknown[] => [GOOD_POINTS[0], point, GOOD_POINTS[2]];
const withLast = (point: unknown): unknown[] => [GOOD_POINTS[0], GOOD_POINTS[1], point];

const MALFORMED_PATHS: Record<string, unknown> = {
  'a unit other than yards': { unit: 'm', points: GOOD_POINTS },
  'no unit': { points: GOOD_POINTS },
  'a list of points instead of a path object': GOOD_POINTS,
  'a single point': { unit: 'yd', points: [GOOD_POINTS[0]] },
  'no points': { unit: 'yd', points: [] },
  'points that are not an array': { unit: 'yd', points: { 0: GOOD_POINTS[0], 1: GOOD_POINTS[1], length: 2 } },
  'missing points': { unit: 'yd' },
  'a NaN coordinate': { unit: 'yd', points: withMiddle([1, 40, 1, Number.NaN]) },
  'an infinite coordinate': { unit: 'yd', points: withMiddle([1, Number.POSITIVE_INFINITY, 1, 5]) },
  'a string coordinate': { unit: 'yd', points: withMiddle([1, '40', 1, 5]) },
  'a null coordinate': { unit: 'yd', points: withMiddle([1, 40, null, 5]) },
  'a point of 3 numbers': { unit: 'yd', points: withMiddle([1, 40, 1]) },
  'a point of 5 numbers': { unit: 'yd', points: withMiddle([1, 40, 1, 5, 9]) },
  'a non-array point': { unit: 'yd', points: withMiddle({ t: 1, x: 40, y: 1, z: 5 }) },
  'a 4-character string as a point': { unit: 'yd', points: withMiddle('1405') },
  'a NaN time': { unit: 'yd', points: withMiddle([Number.NaN, 40, 1, 5]) },
  // Infinity must be last: anywhere earlier, the order check would reject it even without the finite check.
  'an infinite last time': { unit: 'yd', points: withLast([Number.POSITIVE_INFINITY, 80, 1, 0]) },
  'a repeated first time': { unit: 'yd', points: withMiddle([0, 40, 1, 5]) },
  'a repeated last time': { unit: 'yd', points: withMiddle([2, 40, 1, 5]) },
  'a time that goes backwards': { unit: 'yd', points: withMiddle([3, 40, 1, 5]) },
};

describe('toRangeShot', () => {
  it('converts yards in the ballistics frame to metres in the scene frame', () => {
    const shot = makeShot({ flight_path: { unit: 'yd', points: LINE } });
    const p = toRangeShot(shot)!.flight![1];
    expect(p.t).toBe(1);
    expect(p.x).toBeCloseTo(9.144, 9);
    expect(p.y).toBeCloseTo(18.288, 9);
    expect(p.z).toBeCloseTo(-91.44, 9);
  });

  it('turns the origin into 0, not -0', () => {
    const [origin] = flightOf(LINE);
    expect(origin.x).toBe(0);
    expect(origin.y).toBe(0);
    expect(origin.z).toBe(0);
  });

  it('keeps the shot identity: timestamp, club and ball speed', () => {
    expect(toRangeShot(makeShot())).toMatchObject({ id: '2026-08-19T10:00:00Z', club: 'driver', ballSpeedMph: 92 });
  });

  it('places a shot without a flight path at its carry', () => {
    const r = toRangeShot(makeShot({ flight_path: null, carry_spin_adjusted: 150, carry_lateral_yards: -6 }))!;
    expect(r.flight).toBeNull();
    expect(r.carryM).toBeCloseTo(137.16, 9);
    expect(r.lateralM).toBeCloseTo(-5.4864, 9);
  });

  it('treats a shot without the flight_path key like one with null', () => {
    const shot = makeShot();
    expect('flight_path' in shot).toBe(false);
    expect('carry_lateral_yards' in shot).toBe(false);
    const r = toRangeShot(shot)!;
    expect(r.flight).toBeNull();
    expect(r.lateralM).toBe(0);
  });

  it('uses the spin-adjusted carry when there is one and the estimate otherwise', () => {
    const adjusted = toRangeShot(makeShot({ carry_spin_adjusted: 150 }))!;
    expect(adjusted.carryM).toBeCloseTo(137.16, 9);
    const estimated = toRangeShot(makeShot({ carry_spin_adjusted: null, estimated_carry_yards: 200 }))!;
    expect(estimated.carryM).toBeCloseTo(182.88, 9);
  });

  it('takes the lateral offset from the server, never from the path', () => {
    const flyingRight: FlightPathPoint[] = [
      [0, 0, 0, 0],
      [1, 100, 12, 0],
    ];
    const unknownDirection = makeShot({
      launch_angle_horizontal: null,
      carry_lateral_yards: null,
      flight_path: { unit: 'yd', points: flyingRight },
    });
    expect(toRangeShot(unknownDirection)!.lateralM).toBe(0);
    const measured = makeShot({ carry_lateral_yards: 4, flight_path: { unit: 'yd', points: flyingRight } });
    expect(toRangeShot(measured)!.lateralM).toBeCloseTo(3.6576, 9);
  });

  it.each(Object.entries(MALFORMED_PATHS))('ignores a malformed flight path with %s', (_name, path) => {
    const r = toRangeShot(makeShot({ flight_path: path as never, carry_spin_adjusted: 150 }))!;
    expect(r.flight).toBeNull();
    expect(r.carryM).toBeCloseTo(137.16, 9);
  });

  it('has no range shot for a swing-speed rep', () => {
    expect(toRangeShot(makeShot({ mode: 'swing-speed', club: 'Swing Speed' }))).toBeNull();
  });

  it.each<[string, Partial<Shot>]>([
    ['mode', { mode: 'swing-speed' }],
    ['club', { club: 'Swing Speed' }],
  ])('has no range shot when only the %s marks a swing-speed rep', (_flag, overrides) => {
    expect(toRangeShot(makeShot(overrides))).toBeNull();
  });
});

describe('toScenePoints', () => {
  it('converts any path in the flight path format, and nothing else', () => {
    expect(toScenePoints({ unit: 'yd', points: LINE })).toHaveLength(2);
    expect(toScenePoints(null)).toBeNull();
    expect(toScenePoints(undefined)).toBeNull();
    expect(toScenePoints({ unit: 'yd', points: [LINE[0]] })).toBeNull();
  });
});

describe('toRangeShot roll', () => {
  it('converts the roll path into scene points that continue the flight', () => {
    const { flight, roll } = toRangeShot(rollingShot(rollPoints(5.2)))!;

    expect(roll).toHaveLength(3);
    expect(roll![0]).toEqual(flight!.at(-1));
    roll!.slice(1).forEach((point, i) => expect(point.t).toBeGreaterThan(roll![i].t));
    expectClose(roll![1], { t: 5.7, x: 2.2 * YARD_M, y: 0.4 * YARD_M, z: -156 * YARD_M });
    expectClose(roll![2], { t: 6.45, x: 2.4 * YARD_M, y: 0, z: -162 * YARD_M });
  });

  it.each<[string, FlightPathPoint]>([
    ['0.1 m past it', [5.2, 150.11, 2, 0]],
    ['0.1 m above it', [5.2, 150, 2, 0.11]],
    ['0.1 m to the side of it', [5.2, 150, 2.11, 0]],
    ['earlier than it', [5.1, 150, 2, 0]],
    ['later than it', [5.3, 150, 2, 0]],
  ])('drops a roll that does not start at the touchdown: it starts %s', (_where, start) => {
    const r = toRangeShot(rollingShot([start, ...rollPoints(5.2).slice(1)]))!;

    expect(r.roll).toBeNull();
    expect(r.flight).not.toBeNull();
  });

  it('drops a roll when there is no flight for it to continue', () => {
    expect(toRangeShot(rollingShot(rollPoints(5.2), { flight_path: null }))!.roll).toBeNull();
  });

  const [touchdown, hop, rest] = rollPoints(5.2);
  it.each<[string, unknown]>([
    ['a unit other than yards', { unit: 'm', points: [touchdown, hop, rest] }],
    ['a NaN coordinate', { unit: 'yd', points: [touchdown, [5.7, 156, Number.NaN, 0.4], rest] }],
    ['a time that does not rise', { unit: 'yd', points: [touchdown, [5.2, 156, 2.2, 0.4], rest] }],
    ['a single point', { unit: 'yd', points: [touchdown] }],
  ])('drops a roll path with %s and keeps the flight', (_name, path) => {
    const r = toRangeShot(rollingShot(null, { roll_path: path as never }))!;

    expect(r.roll).toBeNull();
    expect(r.flight).not.toBeNull();
  });

  it('turns total_yards into metres', () => {
    expect(toRangeShot(rollingShot(rollPoints(5.2), { total_yards: 162 }))!.totalM).toBe(162 * YARD_M);
  });

  it.each([Number.NaN, Number.POSITIVE_INFINITY, '162'])('has no Total for a total_yards of %s', (total) => {
    expect(toRangeShot(rollingShot(null, { total_yards: total as never }))!.totalM).toBeNull();
  });

  it('flags a typical-spin carry and one whose curve the offline ignores', () => {
    expect(toRangeShot(makeShot({ carry_spin_source: 'club_typical', spin_axis_deg: null }))).toMatchObject({
      typicalSpin: true,
      curveIgnored: true,
    });
    const curved = toRangeShot(makeShot({ club: '7-iron', spin_axis_deg: 3, carry_spin_source: 'measured' }))!;
    expect('typicalSpin' in curved).toBe(false);
    expect('curveIgnored' in curved).toBe(false);
    // A driver flown with a spin axis has its curve in the offline too.
    expect('curveIgnored' in toRangeShot(makeShot({ club: 'driver', spin_axis_deg: -6.6 }))!).toBe(false);
  });

  it('leaves the roll fields out of a shot that has none, so it converts as it did before', () => {
    const r = toRangeShot(makeShot())!;

    expect(r).toEqual({
      id: '2026-08-19T10:00:00Z',
      club: 'driver',
      ballSpeedMph: 92,
      carryM: 214 * YARD_M,
      lateralM: 0,
      flight: null,
    });
    expect('roll' in r).toBe(false);
    expect('totalM' in r).toBe(false);
  });
});

describe('restPoint', () => {
  it('is the last point of the roll', () => {
    const r = toRangeShot(rollingShot(rollPoints(5.2)))!;

    expect(restPoint(r)).toEqual({ x: r.roll!.at(-1)!.x, z: r.roll!.at(-1)!.z });
    expect(restPoint(r).z).toBeCloseTo(-162 * YARD_M, 9);
  });

  it('follows a spin-back roll towards the tee', () => {
    const back: FlightPathPoint[] = [
      [5.2, 150, 2, 0],
      [5.6, 147, 2, 0.3],
      [6.1, 145, 2, 0],
    ];
    const r = toRangeShot(rollingShot(back))!;

    expect(restPoint(r).z).toBeCloseTo(-145 * YARD_M, 9);
    expect(restPoint(r).z).toBeGreaterThan(r.flight!.at(-1)!.z);
  });

  it('a shot without roll_path rests at its landing', () => {
    const r = toRangeShot(rollingShot(null, { roll_yards: null, total_yards: null }))!;

    expect(r.roll).toBeNull();
    expect(r.totalM).toBeNull();
    expect(restPoint(r)).toEqual({ x: r.flight!.at(-1)!.x, z: r.flight!.at(-1)!.z });
  });

  it('is the carry and the lateral offset of a shot with no flight at all', () => {
    const r = toRangeShot(makeShot({ carry_spin_adjusted: 150, carry_lateral_yards: -6 }))!;

    expect(restPoint(r).x).toBeCloseTo(-6 * YARD_M, 9);
    expect(restPoint(r).z).toBeCloseTo(-150 * YARD_M, 9);
  });

  it('has no -0 for a shot that landed on the tee', () => {
    const { x, z } = restPoint(toRangeShot(makeShot({ carry_spin_adjusted: 0 }))!);

    expect(Object.is(x, -0)).toBe(false);
    expect(Object.is(z, -0)).toBe(false);
  });

  it('falls back to the flight when a range shot carries an empty roll', () => {
    const r = { ...toRangeShot(rollingShot(null))!, roll: [] };

    expect(restPoint(r)).toEqual({ x: r.flight!.at(-1)!.x, z: r.flight!.at(-1)!.z });
  });
});

describe('sampleFlight', () => {
  it.each<[number, number, number]>([
    [5.2, 209, 0.025],
    [5.21, 210, 0.01],
  ])('samples uniformly in time and keeps the landing point (%s s flight)', (flightS, count, lastStepS) => {
    const flight = flightOf(arcPath(flightS));
    const samples = sampleFlight(flight, 0.025);
    expect(samples).toHaveLength(count);
    expect(samples[0].t).toBe(0);
    expect(samples[0]).toEqual(flight[0]);
    expect(samples.at(-1)).toEqual(flight.at(-1));
    const steps = samples.slice(1).map((sample, i) => sample.t - samples[i].t);
    for (const step of steps.slice(0, -1)) expect(step).toBeCloseTo(0.025, 9);
    expect(steps.at(-1)).toBeCloseTo(lastStepS, 9);
  });

  it.each<[number, string, number]>([
    [0.14, '0.02', 0.02],
    [2.24, '0.02', 0.02],
    [4.15, '1/60', 1 / 60],
    [1.85, '1/60', 1 / 60],
    [0.9, '0.3', 0.3],
    [2.1, '0.3', 0.3],
  ])('never repeats the landing time: a %s s flight sampled every %s s', (flightS, _stepLabel, stepS) => {
    const flight = flightOf(arcPath(flightS));
    const samples = sampleFlight(flight, stepS);
    for (let i = 1; i < samples.length; i++) expect(samples[i].t - samples[i - 1].t).toBeGreaterThan(1e-6);
    expect(samples.at(-1)).toEqual(flight.at(-1));
  });

  // Mid-segment points of three.js r185 CatmullRomCurve3(points, false, 'centripetal'), taken at the point index.
  const SPLINE_FLIGHT: ScenePoint[] = [
    { t: 0, x: 0, y: 0, z: 0 },
    { t: 1, x: 1, y: 3, z: -10 },
    { t: 2, x: 2.5, y: 5, z: -22 },
    { t: 3, x: 3, y: 4.5, z: -33 },
    { t: 4, x: 3.2, y: 0, z: -40 },
  ];
  const THREE_MIDPOINTS: ScenePoint[] = [
    { t: 0.5, x: 0.476724218353, y: 1.569074187063, z: -4.933924021933 },
    { t: 1.5, x: 1.785206640962, y: 4.24287499757, z: -15.948449018675 },
    { t: 2.5, x: 2.824169677199, y: 5.20869844468, z: -27.720038585013 },
    { t: 3.5, x: 3.113629263746, y: 2.486284959622, z: -36.648732876653 },
  ];

  it('passes through the source points and follows the centripetal Catmull-Rom curve three.js draws', () => {
    const samples = sampleFlight(SPLINE_FLIGHT, 0.5);
    expect(samples).toHaveLength(9);
    SPLINE_FLIGHT.forEach((point, i) => expectClose(samples[2 * i], point));
    THREE_MIDPOINTS.forEach((point, i) => expectClose(samples[2 * i + 1], point));
  });

  it('weights each step by the source times, not by the point index', () => {
    const times = [0, 1, 2.5, 3, 4];
    const uneven = SPLINE_FLIGHT.map((point, i) => ({ ...point, t: times[i] }));
    const samples = sampleFlight(uneven, 0.5);
    expectClose(samples[3], { t: 1.5, x: 1.504742958828, y: 3.858431679917, z: -13.923688667618 });
    expectClose(samples[4], { t: 2, x: 2.057846625104, y: 4.573346093541, z: -17.98466514336 });
  });

  it('counts time from the first point, not from zero', () => {
    const unshifted = sampleFlight(SPLINE_FLIGHT, 0.5);
    const shifted = sampleFlight(
      SPLINE_FLIGHT.map((point) => ({ ...point, t: point.t + 10 })),
      0.5
    );
    shifted.forEach((sample, i) => expectClose(sample, { ...unshifted[i], t: unshifted[i].t + 10 }));
  });

  it('lands on the source points when the step is a multiple of their spacing', () => {
    const flight = flightOf(arcPath(3));
    const samples = sampleFlight(flight, 0.2);
    expect(samples).toHaveLength(16);
    samples.forEach((sample, i) => expectClose(sample, flight[4 * i]));
  });

  it('uses the three.js stand-in knot intervals across a repeated point', () => {
    const atRest: ScenePoint[] = [
      { t: 0, x: 0, y: 0, z: 0 },
      { t: 1, x: 2, y: 3, z: -10 },
      { t: 2, x: 2, y: 3, z: -10 },
      { t: 3, x: 4, y: 0, z: -20 },
    ];
    const samples = sampleFlight(atRest, 0.5);
    expectClose(samples[1], { t: 0.5, x: 1.125, y: 1.6875, z: -5.625 });
    expectClose(samples[3], { t: 1.5, x: 2, y: 3.053993595814, z: -10 });
    expectClose(samples[5], { t: 2.5, x: 2.875, y: 1.6875, z: -14.375 });
  });

  it('draws a straight line through a two-point flight, with no -0', () => {
    const samples = sampleFlight(
      [
        { t: 0, x: 0, y: 0, z: 0 },
        { t: 1, x: 2, y: 4, z: -10 },
      ],
      0.25
    );
    expect(samples).toHaveLength(5);
    samples.forEach((sample, i) => expectClose(sample, { t: 0.25 * i, x: 0.5 * i, y: i, z: -2.5 * i }));
    expectNoNegativeZero(samples);
  });

  it('has no -0 along a flight that stays on the target line', () => {
    const straight = flightOf([
      [0, 0, 0, 0],
      [1, 40, 0, 12],
      [2, 80, 0, 0],
    ]);
    expectNoNegativeZero(sampleFlight(straight, 0.25));
  });

  it('keeps every sample on or above the ground when the spline undershoots touchdown', () => {
    const plunge = flightOf([
      [0, 0, 0, 0],
      [1, 20, 0, 12],
      [2, 40, 0, 12],
      [3, 60, 0, 6],
      [3.5, 61, 0, 0.1],
      [4, 62, 0, 0],
    ]);
    const samples = sampleFlight(plunge, 0.025);
    for (const sample of samples) expect(sample.y).toBeGreaterThanOrEqual(0);
    expect(samples.slice(1, -1).some((sample) => sample.y === 0)).toBe(true);
  });

  it.each([0, -0.025, Number.NaN, Number.POSITIVE_INFINITY])('rejects a step of %s s', (stepS) => {
    expect(() => sampleFlight(flightOf(LINE), stepS)).toThrow(RangeError);
  });

  it('rejects a flight with fewer than two points', () => {
    expect(() => sampleFlight([], 0.025)).toThrow(RangeError);
    expect(() => sampleFlight([{ t: 0, x: 0, y: 0, z: 0 }], 0.025)).toThrow(RangeError);
  });

  it.each<[string, number, number]>([
    ['first', Number.NaN, 0],
    ['first', Number.NEGATIVE_INFINITY, 0],
    ['last', Number.NaN, 2],
    ['last', Number.POSITIVE_INFINITY, 2],
  ])('rejects a flight whose %s time is %s', (_end, t, index) => {
    const flight = flightOf(GOOD_POINTS);
    flight[index] = { ...flight[index], t };

    expect(() => sampleFlight(flight, 0.025)).toThrow(RangeError);
  });
});

describe('positionAt', () => {
  const samples = sampleFlight(flightOf(arcPath(5.21)), 0.025);

  it('clamps positions to the flight', () => {
    expect(positionAt(samples, -1)).toEqual(samples[0]);
    expect(positionAt(samples, 99)).toEqual(samples.at(-1));
  });

  it('returns each sample at its own time and the mean of two neighbours halfway between them', () => {
    samples.forEach((sample, i) => {
      expect(positionAt(samples, sample.t)).toEqual(sample);
      const next = samples[i + 1];
      if (!next) return;
      const halfway = positionAt(samples, (sample.t + next.t) / 2);
      expectClose(halfway, {
        t: (sample.t + next.t) / 2,
        x: (sample.x + next.x) / 2,
        y: (sample.y + next.y) / 2,
        z: (sample.z + next.z) / 2,
      });
    });
  });

  it('interpolates linearly by the samples own times, so a shorter last step still lands exactly', () => {
    const shortLastStep: ScenePoint[] = [
      { t: 0, x: 0, y: 0, z: 0 },
      { t: 1, x: 2, y: 4, z: -10 },
      { t: 1.5, x: 3, y: 2, z: -16 },
    ];
    expect(positionAt(shortLastStep, 0.25)).toEqual({ t: 0.25, x: 0.5, y: 1, z: -2.5 });
    expect(positionAt(shortLastStep, 1.25)).toEqual({ t: 1.25, x: 2.5, y: 3, z: -13 });
    expect(positionAt(shortLastStep, 1.5)).toEqual({ t: 1.5, x: 3, y: 2, z: -16 });
  });

  it('returns a new object, never a stored sample', () => {
    expect(positionAt(samples, -1)).not.toBe(samples[0]);
    expect(positionAt(samples, 99)).not.toBe(samples.at(-1));
    expect(positionAt(samples, samples[7].t)).not.toBe(samples[7]);
  });

  it('gives the only sample of a one-sample list at any time', () => {
    const only: ScenePoint = { t: 2, x: 1, y: 0, z: -5 };
    expect(positionAt([only], 0)).toEqual(only);
    expect(positionAt([only], 9)).toEqual(only);
  });

  it('stays finite around a repeated sample time', () => {
    const stalled: ScenePoint[] = [
      { t: 0, x: 0, y: 0, z: 0 },
      { t: 1, x: 2, y: 4, z: -10 },
      { t: 1, x: 2, y: 4, z: -10 },
      { t: 2, x: 4, y: 0, z: -20 },
    ];
    for (const t of [0.5, 0.999999, 1.000001, 1.5]) {
      const { x, y, z } = positionAt(stalled, t);
      expect([x, y, z].every(Number.isFinite)).toBe(true);
    }
    expect(positionAt(stalled, 1)).toEqual({ t: 1, x: 2, y: 4, z: -10 });
  });

  it('throws when there are no samples', () => {
    expect(() => positionAt([], 0)).toThrow(RangeError);
  });
});
