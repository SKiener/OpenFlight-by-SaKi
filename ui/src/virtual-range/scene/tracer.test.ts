import { Scene, Vector3, type BufferGeometry, type Mesh } from 'three';
import { describe, expect, it } from 'vitest';
import { demoShots } from '../demoShots';
import { restPoint, sampleFlight, usableRoll, type RangeShot, type ScenePoint } from '../flightPath';
import { turfHeightAt } from './greens';
import { buildRollLine, buildTargetLine, landingOf, restOf, shotDrawKey } from './tracer';

const [demoIron, driver] = demoShots();
const iron: RangeShot = { ...demoIron, roll: undefined, totalM: undefined };
const landingPoint: RangeShot = {
  id: 'table',
  club: '7-iron',
  ballSpeedMph: 110,
  carryM: 140,
  lateralM: -4,
  flight: null,
};

const touchdown = iron.flight![iron.flight!.length - 1];
const rollFrom = (steps: [dt: number, down: number, up: number][]): ScenePoint[] =>
  steps.map(([dt, down, up]) => ({ t: touchdown.t + dt, x: touchdown.x + down * 0.05, y: up, z: touchdown.z - down }));
const ROLL = rollFrom([
  [0, 0, 0],
  [0.4, 3, 0.8],
  [0.8, 6, 0],
  [1.6, 9, 0],
  [2.4, 10.5, 0],
  [3.2, 11, 0],
]);
const SPIN_BACK = rollFrom([
  [0, 0, 0],
  [0.3, 0.8, 0.4],
  [0.6, 1.6, 0],
  [1.2, 0.2, 0],
  [1.8, -0.6, 0],
  [2.4, -0.9, 0],
]);
const rolled: RangeShot = { ...iron, roll: ROLL, totalM: 136 };

describe('shotDrawKey', () => {
  it('is the same for the same shot in other objects', () => {
    const copy = { ...iron, flight: iron.flight?.map((point) => ({ ...point })) ?? null };
    const rolledCopy = { ...rolled, roll: ROLL.map((point) => ({ ...point })) };

    expect(shotDrawKey(copy)).toBe(shotDrawKey(iron));
    expect(shotDrawKey(rolledCopy)).toBe(shotDrawKey(rolled));
  });

  it.each([
    ['id', { id: 'other' }],
    ['carry', { carryM: iron.carryM + 1 }],
    ['flight', { flight: driver.flight }],
    ['flight path, with the same carry', { flight: iron.flight?.slice(0, -1) ?? null }],
    ['flight path gone', { flight: null }],
  ])('changes with the %s', (_name, change) => {
    expect(shotDrawKey({ ...iron, ...change })).not.toBe(shotDrawKey(iron));
  });

  it.each([
    ['roll', { roll: ROLL.slice(0, -1) }],
    ['roll, with the same length', { roll: SPIN_BACK }],
    ['roll gone', { roll: null }],
    ['Total gone', { totalM: null }],
  ])('changes with the %s', (_name, change) => {
    expect(shotDrawKey({ ...rolled, ...change })).not.toBe(shotDrawKey(rolled));
  });

  it('redraws the sign when the Total changes', () => {
    expect(shotDrawKey({ ...rolled, totalM: 150 })).not.toBe(shotDrawKey({ ...rolled, totalM: 160 }));
  });

  it('takes a missing, null or empty roll and a missing or null Total alike', () => {
    const keys = [iron, { ...iron, roll: null }, { ...iron, roll: [] }, { ...iron, totalM: null }].map(shotDrawKey);

    expect(new Set(keys).size).toBe(1);
  });

  it('changes with the landing point of a shot without a flight', () => {
    expect(shotDrawKey({ ...landingPoint, lateralM: 2 })).not.toBe(shotDrawKey(landingPoint));
    expect(shotDrawKey({ ...landingPoint, carryM: 141 })).not.toBe(shotDrawKey(landingPoint));
  });

  it('ignores the lateral offset of a shot that has a flight, which ends where it ends', () => {
    expect(shotDrawKey({ ...iron, lateralM: iron.lateralM + 3 })).toBe(shotDrawKey(iron));
  });
});

describe('usableRoll and restOf', () => {
  it('rest a rolled shot on the turf at its last roll point', () => {
    const { x, z } = ROLL[ROLL.length - 1];

    expect(usableRoll(rolled)).toBe(ROLL);
    expect(restOf(rolled)).toEqual({ x, y: turfHeightAt(x, z), z });
    expect(restPoint(rolled)).toEqual({ x, z });
  });

  it('put a spin-back shot to rest behind its landing, towards the tee', () => {
    const spinBack = { ...rolled, roll: SPIN_BACK };

    expect(restOf(spinBack).z).toBeGreaterThan(landingOf(spinBack).z);
  });

  it.each([
    ['no roll', iron],
    ['a null roll', { ...iron, roll: null }],
    ['an empty roll', { ...iron, roll: [] }],
    ['a single roll point', { ...iron, roll: ROLL.slice(0, 1) }],
    ['a roll that goes back in time', { ...iron, roll: [ROLL[0], { ...ROLL[1], t: touchdown.t - 1 }] }],
    ['a roll after no flight', { ...landingPoint, roll: ROLL }],
  ])('rest a shot with %s at its landing', (_name, shot) => {
    expect(usableRoll(shot)).toBeNull();
    expect(restOf(shot)).toEqual(landingOf(shot));
  });
});

describe('buildRollLine', () => {
  const SAMPLES = sampleFlight(ROLL, 0.025);

  function lineIn(scene: Scene) {
    const line = buildRollLine(scene);
    expect(scene.children, 'one mesh, one draw call').toHaveLength(1);
    return { line, mesh: scene.children[0] as Mesh };
  }

  const vertices = (geometry: BufferGeometry): Vector3[] => {
    const position = geometry.getAttribute('position');
    return Array.from({ length: position.count }, (_, i) => new Vector3().fromBufferAttribute(position, i));
  };

  function faces(geometry: BufferGeometry): { normalY: number; area: number }[] {
    const points = vertices(geometry);
    const index = geometry.getIndex()!.array;
    const result = [];
    for (let i = 0; i < index.length; i += 3) {
      const [a, b, c] = [points[index[i]], points[index[i + 1]], points[index[i + 2]]];
      const normal = new Vector3().subVectors(b, a).cross(new Vector3().subVectors(c, a));
      result.push({ normalY: normal.y, area: normal.length() / 2 });
    }
    return result;
  }

  it('lies just above the turf as a thin strip along the roll', () => {
    const { line, mesh } = lineIn(new Scene());
    expect(mesh.visible).toBe(false);

    line.set(SAMPLES);
    line.show(Infinity);

    expect(mesh.visible).toBe(true);
    const points = vertices(mesh.geometry);
    expect(points).toHaveLength(SAMPLES.length * 2);
    SAMPLES.forEach(({ x, z }, i) => {
      const [left, right] = [points[2 * i], points[2 * i + 1]];
      const lift = (left.y + right.y) / 2 - turfHeightAt(x, z);
      expect(lift, `sample ${i}`).toBeGreaterThan(0.05);
      expect(lift, `sample ${i}`).toBeLessThan(0.2);
      expect(left.distanceTo(right), `width at sample ${i}`).toBeGreaterThan(0.1);
      expect(left.distanceTo(right), `width at sample ${i}`).toBeLessThan(0.4);
      expect((left.x + right.x) / 2).toBeCloseTo(x, 4);
      expect((left.z + right.z) / 2).toBeCloseTo(z, 4);
    });
  });

  it.each([
    ['rolls on', ROLL],
    ['spins back', SPIN_BACK],
  ])('faces up when the ball %s, so no part of it is culled', (_name, roll) => {
    const { line, mesh } = lineIn(new Scene());

    line.set(sampleFlight(roll, 0.025));

    const area = (triangles: { area: number }[]) => triangles.reduce((sum, triangle) => sum + triangle.area, 0);
    const triangles = faces(mesh.geometry);
    const down = triangles.filter(({ normalY }) => normalY <= 0);
    expect(area(triangles)).toBeGreaterThan(0.5);
    expect(area(down), 'area facing down, at most a sliver where the ball turns').toBeLessThan(0.02 * area(triangles));
  });

  it('has positions and up normals from the start, as three picks the shader program by them', () => {
    const { line, mesh } = lineIn(new Scene());
    const attributes = () => Object.keys(mesh.geometry.attributes).sort();
    expect(attributes()).toEqual(['normal', 'position']);

    line.set(SAMPLES);

    expect(attributes()).toEqual(['normal', 'position']);
    const normal = mesh.geometry.getAttribute('normal');
    for (let i = 0; i < normal.count; i++) expect([normal.getX(i), normal.getY(i), normal.getZ(i)]).toEqual([0, 1, 0]);
  });

  it('builds the strip once for the same samples, and hides without a roll', () => {
    const { line, mesh } = lineIn(new Scene());
    line.set(SAMPLES);
    line.show(Infinity);
    const shown = mesh.geometry;

    line.set(SAMPLES);
    expect(mesh.geometry).toBe(shown);

    line.set(null);
    line.show(Infinity);
    expect(mesh.visible).toBe(false);
    line.set(SAMPLES);
    line.show(Infinity);
    expect(mesh.visible).toBe(true);
    expect(vertices(mesh.geometry)).toEqual(vertices(shown));
  });

  it('builds the strip before the flight: the air and the touchdown only change what it draws', () => {
    const { line, mesh } = lineIn(new Scene());
    line.set(SAMPLES);
    const built = mesh.geometry;

    line.show(null);
    expect(mesh.visible).toBe(false);
    line.show(0.05);

    expect(mesh.visible).toBe(true);
    expect(mesh.geometry).toBe(built);
  });
});

describe('the roll line while the ball rolls', () => {
  const SAMPLES = sampleFlight(ROLL, 0.025);
  const rollS = SAMPLES[SAMPLES.length - 1].t - SAMPLES[0].t;

  function drawn(rolledS: number): { count: number; geometry: BufferGeometry } {
    const scene = new Scene();
    const line = buildRollLine(scene);
    line.set(SAMPLES);
    line.show(rolledS);
    const { geometry } = scene.children[0] as Mesh;
    return { count: Math.min(geometry.drawRange.count, geometry.getIndex()!.count), geometry };
  }

  it('grows behind the ball and is whole at rest', () => {
    const counts = [0, 0.25, 0.5, 0.75, 1].map((k) => drawn(k * rollS).count);

    expect(counts[0]).toBe(0);
    for (let i = 1; i < counts.length; i++) expect(counts[i]).toBeGreaterThan(counts[i - 1]);
    expect(counts[4]).toBe(drawn(Infinity).geometry.getIndex()!.count);
    expect(drawn(rollS / 2).count % 6, 'whole quads').toBe(0);
  });

  it('ends at the last sample the ball has passed', () => {
    const k = 40;
    const { count, geometry } = drawn(SAMPLES[k].t - SAMPLES[0].t + 0.001);
    const index = geometry.getIndex()!.array;
    const reached = Math.max(...Array.from(index.slice(0, count)));

    expect(reached).toBe(2 * k + 1);
  });

  it('keeps one strip while it grows: no new geometry per frame', () => {
    const scene = new Scene();
    const line = buildRollLine(scene);
    line.set(SAMPLES);
    line.show(0.1);
    const { geometry } = scene.children[0] as Mesh;

    for (let s = 0.1; s < rollS; s += 1 / 60) line.show(s);

    expect((scene.children[0] as Mesh).geometry).toBe(geometry);
  });
});

describe('buildTargetLine', () => {
  function lineIn(scene: Scene) {
    const line = buildTargetLine(scene);
    expect(scene.children, 'one mesh, one draw call').toHaveLength(1);
    return { line, mesh: scene.children[0] as Mesh };
  }

  function dashes(mesh: Mesh): { from: Vector3; to: Vector3 }[] {
    const points = vertices(mesh.geometry);
    const middle = (a: number) => new Vector3().addVectors(points[a], points[a + 1]).multiplyScalar(0.5);
    return Array.from({ length: mesh.geometry.drawRange.count / 6 }, (_, d) => ({
      from: middle(4 * d),
      to: middle(4 * d + 2),
    }));
  }

  const vertices = (geometry: BufferGeometry): Vector3[] => {
    const position = geometry.getAttribute('position');
    return Array.from({ length: position.count }, (_, i) => new Vector3().fromBufferAttribute(position, i));
  };

  it('runs dashed on the turf from the ball to the flag, hidden until shown', () => {
    const { line, mesh } = lineIn(new Scene());
    expect(mesh.visible).toBe(false);

    line.place({ x: 2, z: -140 }, { x: -8, z: -150 });
    line.show(true);

    expect(mesh.visible).toBe(true);
    const drawn = dashes(mesh);
    const length = Math.hypot(10, 10);
    expect(drawn).toHaveLength(Math.round((length + 1.5) / 3.5));
    expect(drawn[0].from.x).toBeCloseTo(2, 4);
    expect(drawn[0].from.z).toBeCloseTo(-140, 4);
    expect(drawn.at(-1)!.to.x).toBeCloseTo(-8, 4);
    expect(drawn.at(-1)!.to.z).toBeCloseTo(-150, 4);
    const lift = drawn.at(-1)!.to.y - turfHeightAt(-8, -150);
    expect(lift, 'over the dome of the green').toBeGreaterThan(0.05);
    expect(lift).toBeLessThan(0.2);
  });

  const across = (a: Vector3, b: Vector3): number => Math.hypot(b.x - a.x, b.z - a.z);

  it('ends on dashes at the ball and the flag, and draws one short dash for a flag right by the ball', () => {
    const { line, mesh } = lineIn(new Scene());

    line.place({ x: 0, z: -100 }, { x: 0, z: -100.6 });
    const [only] = dashes(mesh);
    expect(dashes(mesh)).toHaveLength(1);
    expect(across(only.from, only.to)).toBeCloseTo(0.6, 4);

    line.place({ x: 0, z: -100 }, { x: 0, z: -100 });
    expect(dashes(mesh).every(({ from, to }) => [from.x, from.z, to.x, to.z].every(Number.isFinite))).toBe(true);
  });

  it('has gaps between its dashes', () => {
    const { line, mesh } = lineIn(new Scene());
    line.place({ x: 0, z: -100 }, { x: 0, z: -130 });

    const drawn = dashes(mesh);
    expect(drawn.length).toBeGreaterThan(5);
    for (let d = 0; d < drawn.length - 1; d++) {
      expect(across(drawn[d].from, drawn[d].to), `dash ${d}`).toBeCloseTo(2, 3);
      expect(across(drawn[d].to, drawn[d + 1].from), `gap after dash ${d}`).toBeCloseTo(1.5, 3);
    }
  });

  it('moves its one mesh to a new target without building another', () => {
    const { line, mesh } = lineIn(new Scene());
    line.place({ x: 2, z: -140 }, { x: -8, z: -150 });
    const { geometry } = mesh;
    const position = geometry.getAttribute('position');
    const version = position.version;

    line.place({ x: 2, z: -140 }, { x: 10, z: -230 });

    expect(mesh.geometry).toBe(geometry);
    expect(geometry.getAttribute('position')).toBe(position);
    expect(position.version).toBeGreaterThan(version);
    expect(dashes(mesh).at(-1)!.to.z).toBeCloseTo(-230, 4);
    line.show(false);
    expect(mesh.visible).toBe(false);
  });

  it('has positions and up normals from the start, as the roll line, so both share one shader program', () => {
    const { mesh } = lineIn(new Scene());

    expect(Object.keys(mesh.geometry.attributes).sort()).toEqual(['normal', 'position']);
  });
});
