import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { makeShot } from '../../test-utils/shot';
import { impactFaceInput, impactZoneWord } from '../panel/impactPoint';
import { ImpactFace, type ImpactFaceProps } from './ImpactFace';
import {
  CENTRE_HALF_WIDTH_MM,
  IMPACT_DOT_RADIUS_MM,
  impactDotPosition,
  impactFaceKind,
  impactFaceShape,
} from './impactFaceGeometry';

const render = (props: Partial<ImpactFaceProps> = {}) =>
  renderToString(<ImpactFace club="7-iron" offsetMm={null} heightMm={null} zone={null} {...props} />);

/** The dot's (or ring's) centre in face mm (SVG y down, so y is negated back). */
function mark(html: string): { x: number; y: number } | null {
  const circle = html.match(/<circle class="impact-face__(?:dot|ring-edge)" cx="([-\d.]+)" cy="([-\d.]+)"/);
  return circle ? { x: Number(circle[1]), y: -Number(circle[2]) } : null;
}

/** Points along an SVG path of M, L, Q, C and Z (absolute, "x,y" pairs), in face mm (y up): L 8×, Q 16×, C 24×. */
function pathPoints(d: string): [number, number][] {
  const points: [number, number][] = [];
  let current: [number, number] = [0, 0];
  const lerp = (a: number[], b: number[], t: number) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  const curve = (controls: number[][], t: number): number[] =>
    controls.length === 1 ? controls[0] : curve(controls.slice(1).map((p, i) => lerp(controls[i], p, t)), t);
  for (const [, command, args] of d.matchAll(/([MLQCZ])([^MLQCZ]*)/g)) {
    const pairs = [...args.matchAll(/(-?[\d.]+),(-?[\d.]+)/g)].map((m): [number, number] => [Number(m[1]), -Number(m[2])]);
    if (command === 'M') {
      current = pairs[0];
      points.push(current);
      continue;
    }
    const steps = { L: 8, Q: 16, C: 24 }[command as 'L' | 'Q' | 'C'];
    if (!steps) continue;
    const controls = [current, ...pairs];
    for (let i = 1; i <= steps; i += 1) points.push(curve(controls, i / steps) as [number, number]);
    current = pairs[pairs.length - 1];
  }
  return points;
}

describe('ImpactFace', () => {
  it('draws a + (toe) offset right of centre and a − (heel) offset left of it', () => {
    const toe = mark(render({ offsetMm: 12, zone: 'toe' }))!;
    const heel = mark(render({ offsetMm: -12, zone: 'heel' }))!;
    expect(toe.x).toBe(12);
    expect(heel.x).toBe(-12);
    expect(mark(render({ offsetMm: 0, zone: 'center' }))!.x).toBe(0);
  });

  it('puts the hosel and the shaft on the heel (left) side', () => {
    for (const club of ['7-iron', 'driver', '3-wood']) {
      const shape = impactFaceShape(impactFaceKind(club));
      // The iron's hosel: the part of its body off the face (left of it or over its top).
      const faceLeft = Math.min(...pathPoints(shape.face).map(([x]) => x));
      const ironHosel = shape.plate === null ? pathPoints(shape.body).filter(([x, y]) => x < faceLeft || y > shape.topAt(x) + 1) : [];
      const points = [...ironHosel, ...pathPoints(shape.shaft.fill), ...pathPoints(shape.shaft.lines), ...(shape.hosel ? pathPoints(shape.hosel.fill) : [])];
      expect(points.length, club).toBeGreaterThan(4);
      expect(Math.max(...points.map(([x]) => x)), club).toBeLessThan(-20);
    }
  });

  it('clamps an offset outside the face inside the outline', () => {
    for (const [club, offset] of [
      ['7-iron', 80],
      ['7-iron', -80],
      ['driver', 200],
      ['driver', -200],
      ['5-wood', 90],
    ] as const) {
      const shape = impactFaceShape(impactFaceKind(club));
      const dot = mark(render({ club, offsetMm: offset, zone: offset > 0 ? 'toe' : 'heel' }))!;
      expect(dot.x, `${club} ${offset}`).toBe(offset > 0 ? Math.round(shape.maxX * 10) / 10 : Math.round(shape.minX * 10) / 10);
      // The whole dot stays on the face, under and over the edges where it sits.
      for (const x of [dot.x - IMPACT_DOT_RADIUS_MM, dot.x, dot.x + IMPACT_DOT_RADIUS_MM]) {
        expect(dot.y + IMPACT_DOT_RADIUS_MM, `${club} ${offset} top`).toBeLessThan(shape.topAt(x) + 0.05);
        expect(dot.y - IMPACT_DOT_RADIUS_MM, `${club} ${offset} bottom`).toBeGreaterThan(shape.bottomAt(x) - 0.05);
      }
    }
  });

  it('places the dot at the measured height, clamped to the face, else at mid-height', () => {
    const driver = impactFaceShape('driver');
    expect(impactDotPosition(driver, 0, 30)).toEqual({ x: 0, y: 30 });
    expect(impactDotPosition(driver, 0, 200).y).toBeLessThanOrEqual(driver.topAt(0) - IMPACT_DOT_RADIUS_MM);
    expect(impactDotPosition(driver, 0, -50).y).toBeGreaterThanOrEqual(driver.bottomAt(0) + IMPACT_DOT_RADIUS_MM);
    // Without a height: half way between the edges at that x (an iron's toe is taller than its heel).
    const iron = impactFaceShape('iron');
    const heel = impactDotPosition(iron, -20, null);
    const toe = impactDotPosition(iron, 20, null);
    expect(heel.y).toBeGreaterThan(10);
    expect(toe.y).toBeGreaterThan(heel.y);
    expect(impactDotPosition(driver, 0, null).y).toBeCloseTo(28, 0);
  });

  it('draws a ring, no filled dot, when the zone is open', () => {
    const html = render({ offsetMm: 5, zone: null });
    expect(html).toContain('data-mark="ring"');
    expect(html).toContain('impact-face__ring-edge');
    expect(html).not.toContain('impact-face__dot');
    expect(mark(html)!.x).toBe(5);
  });

  it('draws no dot without an offset, only the face', () => {
    for (const zone of [null, 'toe'] as const) {
      const html = render({ offsetMm: null, zone });
      expect(html).toContain('data-mark="none"');
      expect(html).not.toContain('<circle');
      expect(html).toContain('impact-face__body');
      expect(html).toContain('impact-face__edge');
    }
  });

  it('takes the wood face for woods and the iron blade for every other club', () => {
    expect(impactFaceKind('driver')).toBe('driver');
    expect(impactFaceKind('mini-driver')).toBe('driver');
    for (const club of ['3-wood', '5-wood', '7-wood']) expect(impactFaceKind(club), club).toBe('fairway');
    for (const club of ['7-iron', 'pw', 'wedge-54', '3-hybrid', '7iron', '', null, undefined]) {
      expect(impactFaceKind(club), String(club)).toBe('iron');
    }
    expect(render({ club: 'driver' })).toContain('data-kind="driver"');
    expect(render({ club: '3-wood' })).toContain('data-kind="fairway"');
    expect(render({ club: '8-iron' })).toContain('data-kind="iron"');
  });

  it('sizes the heads as specified', () => {
    const extent = (d: string) => {
      const points = pathPoints(d);
      const xs = points.map(([x]) => x);
      const ys = points.map(([, y]) => y);
      return { left: Math.min(...xs), right: Math.max(...xs), width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) };
    };
    const iron = impactFaceShape('iron');
    // The fillet at the hosel's crotch lifts the top line's start (28 mm) a little.
    expect(iron.topAt(-37)).toBeCloseTo(30.2, 1);
    // The top line's end, where the toe's curve takes over.
    expect(iron.topAt(12)).toBeCloseTo(43, 0);
    // The toe's peak.
    expect(Math.max(...Array.from({ length: 61 }, (_, i) => iron.topAt(i * 0.5)))).toBeCloseTo(44, 0);
    // The blade, hosel cut off, from the heel corner to the toe.
    expect(extent(iron.face).left).toBeCloseTo(-41, 0);
    expect(extent(iron.face).right).toBeCloseTo(38, 0);
    const driver = impactFaceShape('driver');
    // The driver's plate: 107 mm wide, 57–58 mm high.
    expect(extent(driver.plate!).width).toBeCloseTo(107, 0);
    expect(extent(driver.plate!).height).toBeGreaterThan(57);
    expect(extent(driver.plate!).height).toBeLessThan(59);
    // Its crown is higher toward the toe than toward the heel.
    expect(driver.topAt(0)).toBeCloseTo(56.6, 0);
    expect(driver.topAt(-30)).toBeLessThan(driver.topAt(30) - 5);
    const fairway = impactFaceShape('fairway');
    // The fairway wood's shallow plate: 90 × 38 mm.
    expect(extent(fairway.plate!).width).toBeCloseTo(90, 0);
    expect(extent(fairway.plate!).height).toBeCloseTo(39, 0);
    // A plate only on the woods; the iron's blade is its face.
    expect(iron.plate).toBeNull();
    expect(iron.hosel).toBeNull();
    expect(driver.face).toBe(driver.plate);
  });

  it('draws 9 score lines on the iron and 7 on the woods, all inside the face', () => {
    for (const [kind, count] of [['iron', 9], ['driver', 7], ['fairway', 7]] as const) {
      const shape = impactFaceShape(kind);
      const ends = [...shape.grooves.matchAll(/[ML](-?[\d.]+),(-?[\d.]+)/g)].map((m) => [Number(m[1]), -Number(m[2])]);
      expect(shape.grooves.match(/M/g)?.length, kind).toBe(count);
      expect(ends.length, kind).toBe(2 * count);
      for (const [x, y] of ends) {
        expect(y, `${kind} ${x},${y}`).toBeGreaterThan(shape.bottomAt(x));
        expect(y, `${kind} ${x},${y}`).toBeLessThan(shape.topAt(x));
      }
    }
  });

  it('gives each graphic its own clip and gradient ids, and refers only to its own', () => {
    const html = renderToString(
      <>
        <ImpactFace club="driver" offsetMm={3} heightMm={null} zone="center" />
        <ImpactFace club="driver" offsetMm={3} heightMm={null} zone="center" />
      </>
    );
    const svgs = html.match(/<svg[\s\S]*?<\/svg>/g)!;
    expect(svgs).toHaveLength(2);
    const ids = svgs.map((svg) => [...svg.matchAll(/ id="([^"]+)"/g)].map((m) => m[1]));
    for (const [index, svg] of svgs.entries()) {
      // One clip and two gradients, every url(#…) naming one of them.
      expect(ids[index]).toHaveLength(3);
      const refs = [...svg.matchAll(/url\(#([^)]+)\)/g)].map((m) => m[1]);
      expect(refs.length).toBeGreaterThanOrEqual(4);
      for (const ref of refs) expect(ids[index]).toContain(ref);
    }
    expect(ids[0].filter((id) => ids[1].includes(id))).toEqual([]);
  });

  it('keeps every drawn part inside the viewBox, at least 2 mm in', () => {
    for (const kind of ['iron', 'driver', 'fairway'] as const) {
      const shape = impactFaceShape(kind);
      const [left, top, width, height] = shape.viewBox.split(' ').map(Number);
      const points = [shape.body, shape.shaft.fill, shape.shaft.lines, ...(shape.hosel ? [shape.hosel.fill] : [])].flatMap(pathPoints);
      for (const [x, y] of points) {
        expect(x, `${kind} x ${x}`).toBeGreaterThanOrEqual(left + 2);
        expect(x, `${kind} x ${x}`).toBeLessThanOrEqual(left + width - 2);
        expect(-y, `${kind} y ${y}`).toBeGreaterThanOrEqual(top + 2);
        expect(-y, `${kind} y ${y}`).toBeLessThanOrEqual(top + height - 2);
      }
    }
  });

  it('shades the centre band by the backend’s half widths', () => {
    expect(CENTRE_HALF_WIDTH_MM).toEqual({ irons: 6, woods: 8 });
    expect(render({ club: '7-iron' })).toMatch(/<rect class="impact-face__band" clip-path="url\(#impact-face-[\w-]+\)" x="-6" [^>]*width="12"/);
    expect(render({ club: 'driver' })).toMatch(/<rect class="impact-face__band" [^>]*x="-8" [^>]*width="16"/);
  });

  it('is decorative and draws no number or unit', () => {
    const html = render({ offsetMm: 12.3, heightMm: 30, zone: 'toe', club: 'driver' });
    expect(html).toMatch(/^<svg class="impact-face" [^>]*aria-hidden="true"/);
    expect(html).not.toContain('<text');
    expect(html).not.toMatch(/mm/);
  });
});

describe('impactPoint', () => {
  it('reads the offset, height, zone and club from the shot, never the old impact_toe_mm', () => {
    expect(
      impactFaceInput(makeShot({ club: 'driver', impact_offset_mm: 4, impact_height_mm: 31, impact_zone: 'center' }))
    ).toEqual({ club: 'driver', offsetMm: 4, heightMm: 31, zone: 'center' });
    const old = makeShot({ impact_toe_mm: -9 } as never);
    expect(impactFaceInput(old)).toMatchObject({ offsetMm: null, zone: null });
    expect(impactZoneWord(old)).toBe('—');
    expect(impactFaceInput(makeShot({ impact_offset_mm: 2, impact_zone: 'wobble' as never })).zone).toBeNull();
  });
});
