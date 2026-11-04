import { isWoodClub } from '../../data/clubs';

/**
 * The geometry of ImpactFace: a schematic club head seen from the golfer at address
 * (and in the camera video), hosel and heel on the left, toe on the right
 * (right-handed), so a + offset sits right of centre. Everything is in millimetres:
 * x maps 1:1 to the offset, x = 0 is the middle of the hitting area, and y grows up
 * from the leading edge (SVG y is its negative).
 */

export type ImpactFaceKind = 'driver' | 'fairway' | 'iron';

/** The dot: a ball mark of about 12 mm on the face, big enough to see from 2 m on the kiosk. */
export const IMPACT_DOT_RADIUS_MM = 6;
/** Room kept between the dot's edge and the outline when it is clamped. */
const DOT_CLEARANCE_MM = 1.5;

/** The backend's IMPACT_ZONE_CENTRE_HALF_WIDTH_MM (club_data.py): the centre band. */
export const CENTRE_HALF_WIDTH_MM: Record<'irons' | 'woods', number> = { irons: 6, woods: 8 };

/** The driver and the mini driver have the deep, teed face; 3-, 5- and 7-wood the shallow one. */
export function impactFaceKind(club: string | null | undefined): ImpactFaceKind {
  if (!club || !isWoodClub(club)) return 'iron';
  return club === 'driver' || club === 'mini-driver' ? 'driver' : 'fairway';
}

export interface ImpactFaceShape {
  kind: ImpactFaceKind;
  viewBox: string;
  /** The head's silhouette (SVG path, SVG y down), filled. */
  body: string;
  /** The head's outline, stroked: open at the iron's hosel top, where the shaft goes on; the woods' closed body. */
  edge: string;
  /** The hitting area (closed): clips the band and the grooves, and gives topAt / bottomAt. */
  face: string;
  /** The woods' face-plate contour inside the body; none on an iron, whose blade is the face. */
  plate: string | null;
  /** The woods' hosel, drawn behind the head: its fill and its two side lines. The iron's is part of its body. */
  hosel: { fill: string; edge: string } | null;
  /** The shaft stub above the hosel, fading out from `from` to `to` (SVG coordinates on the shaft axis). */
  shaft: { lines: string; fill: string; from: { x: number; y: number }; to: { x: number; y: number } };
  /** The score lines: one path of M…L… subpaths. */
  grooves: string;
  centreHalfWidth: number;
  /** The dot's centre stays within these x (face mm). */
  minX: number;
  maxX: number;
  /** Lower and upper edge of the face at x (face mm, y up). */
  bottomAt: (x: number) => number;
  topAt: (x: number) => number;
}

/** A coordinate to 0.1 mm, as drawn. */
export const roundMm = (value: number) => Math.round(value * 10) / 10;

type Point = readonly [number, number];
type Segment = { M: Point } | { L: Point } | { Q: readonly [Point, Point] } | { C: readonly [Point, Point, Point] };

const point = ([x, y]: Point) => `${roundMm(x)},${roundMm(-y)}`;
const add = (a: Point, b: Point): Point => [a[0] + b[0], a[1] + b[1]];
const scale = (a: Point, k: number): Point => [a[0] * k, a[1] * k];
const lerp = (a: Point, b: Point, t: number): Point => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
const quad = (a: Point, c: Point, b: Point, t: number) => lerp(lerp(a, c, t), lerp(c, b, t), t);
const cubic = (a: Point, c1: Point, c2: Point, b: Point, t: number) => quad(lerp(a, c1, t), lerp(c1, c2, t), lerp(c2, b, t), t);

/** A path from segments: its SVG string and its points sampled along the curves (L 8×, Q 16×, C 24×). */
function build(segments: readonly Segment[], close = true): { d: string; points: Point[] } {
  const parts: string[] = [];
  const points: Point[] = [];
  let current: Point = [0, 0];
  for (const segment of segments) {
    if ('M' in segment) {
      parts.push(`M${point(segment.M)}`);
      current = segment.M;
      points.push(current);
    } else if ('L' in segment) {
      parts.push(`L${point(segment.L)}`);
      for (let i = 1; i <= 8; i += 1) points.push(lerp(current, segment.L, i / 8));
      current = segment.L;
    } else if ('Q' in segment) {
      const [c, end] = segment.Q;
      parts.push(`Q${point(c)} ${point(end)}`);
      for (let i = 1; i <= 16; i += 1) points.push(quad(current, c, end, i / 16));
      current = end;
    } else {
      const [c1, c2, end] = segment.C;
      parts.push(`C${point(c1)} ${point(c2)} ${point(end)}`);
      for (let i = 1; i <= 24; i += 1) points.push(cubic(current, c1, c2, end, i / 24));
      current = end;
    }
  }
  if (close) parts.push('Z');
  return { d: parts.join(' '), points };
}

/** The lowest and highest crossing of the vertical at x with a closed polyline (x kept within its extent). */
function crossings(points: readonly Point[]): { bottomAt: (x: number) => number; topAt: (x: number) => number } {
  const xs = points.map((p) => p[0]);
  const [left, right] = [Math.min(...xs), Math.max(...xs)];
  const at = (rawX: number) => {
    const x = Math.max(left, Math.min(right, rawX));
    let low = Infinity;
    let high = -Infinity;
    for (let i = 0; i < points.length; i += 1) {
      const a = points[i];
      const b = points[(i + 1) % points.length];
      if ((a[0] - x) * (b[0] - x) > 0 || a[0] === b[0]) continue;
      const y = a[1] + ((x - a[0]) / (b[0] - a[0])) * (b[1] - a[1]);
      low = Math.min(low, y);
      high = Math.max(high, y);
    }
    return [low, high] as const;
  };
  return { bottomAt: (x) => at(x)[0], topAt: (x) => at(x)[1] };
}

/** A hosel / shaft axis: its point s mm up from the base, leaning from vertical toward the heel; side +1 is the toe side. */
function axis(base: Point, leanDeg: number) {
  const lean = (leanDeg * Math.PI) / 180;
  const along: Point = [-Math.sin(lean), Math.cos(lean)];
  const across: Point = [along[1], -along[0]];
  const at = (s: number) => add(base, scale(along, s));
  return { across, at, side: (s: number, halfWidth: number, k: 1 | -1) => add(at(s), scale(across, k * halfWidth)) };
}
type Axis = ReturnType<typeof axis>;

/** The shaft stub: 5 mm tapering to this half width, then straight; it fades out over its length. */
const SHAFT_HALF_WIDTH_MM = 4.2;
const SHAFT_TAPER_MM = 5;

interface HeadParts {
  kind: ImpactFaceKind;
  body: { d: string; points: Point[] };
  edge: string;
  face: { d: string; points: Point[] };
  plate: string | null;
  axis: Axis;
  /** Where the hosel ends and the shaft starts (mm along the axis), and the hosel's half width there. */
  hoselTop: number;
  hoselTopHalfWidth: number;
  /** The woods' separate hosel: from s mm along the axis (below the crown), this half width. */
  hosel: { from: number; halfWidth: number } | null;
  shaftLength: number;
  grooves: readonly (readonly [Point, Point])[];
  centreHalfWidth: number;
  minX: number;
  maxX: number;
}

/** The shaft, the woods' hosel and the viewBox around the sampled outline (3 mm margin), common to every head. */
function finish(head: HeadParts): ImpactFaceShape {
  const { axis: ax, hoselTop: top, hoselTopHalfWidth: topWidth } = head;
  const end = top + head.shaftLength;
  const sides = [-1, 1] as const;
  const lines = sides
    .map((k) => `M${point(ax.side(top, topWidth, k))} L${point(ax.side(top + SHAFT_TAPER_MM, SHAFT_HALF_WIDTH_MM, k))} L${point(ax.side(end, SHAFT_HALF_WIDTH_MM, k))}`)
    .join(' ');
  // The fill starts a touch inside the hosel, so no seam shows between the two.
  const fill = `M${point(ax.side(top - 0.2, topWidth, -1))} L${point(ax.side(top + SHAFT_TAPER_MM, SHAFT_HALF_WIDTH_MM, -1))} L${point(ax.side(end, SHAFT_HALF_WIDTH_MM, -1))} L${point(ax.side(end, SHAFT_HALF_WIDTH_MM, 1))} L${point(ax.side(top + SHAFT_TAPER_MM, SHAFT_HALF_WIDTH_MM, 1))} L${point(ax.side(top - 0.2, topWidth, 1))} Z`;
  const corners: Point[] = sides.flatMap((k) => [ax.side(top, topWidth, k), ax.side(top + SHAFT_TAPER_MM, SHAFT_HALF_WIDTH_MM, k), ax.side(end, SHAFT_HALF_WIDTH_MM, k)]);
  let hosel: ImpactFaceShape['hosel'] = null;
  if (head.hosel) {
    const { from, halfWidth } = head.hosel;
    const [a, b, c, d] = [ax.side(from, halfWidth, -1), ax.side(top, halfWidth, -1), ax.side(top, halfWidth, 1), ax.side(from, halfWidth, 1)];
    hosel = { fill: `M${point(a)} L${point(b)} L${point(c)} L${point(d)} Z`, edge: `M${point(a)} L${point(b)} M${point(d)} L${point(c)}` };
    corners.push(a, b, c, d);
  }
  // From the sampled curve, not its control points, which stand off the outline.
  const all = [...head.body.points, ...corners];
  const xs = all.map((p) => p[0]);
  const ys = all.map((p) => p[1]);
  const margin = 3;
  const left = Math.min(...xs) - margin;
  const right = Math.max(...xs) + margin;
  const bottom = Math.min(...ys) - margin;
  const ceiling = Math.max(...ys) + margin;
  const toSvg = (p: Point) => ({ x: roundMm(p[0]), y: roundMm(-p[1]) });
  return {
    kind: head.kind,
    viewBox: `${roundMm(left)} ${roundMm(-ceiling)} ${roundMm(right - left)} ${roundMm(ceiling - bottom)}`,
    body: head.body.d,
    edge: head.edge,
    face: head.face.d,
    plate: head.plate,
    hosel,
    shaft: { lines, fill, from: toSvg(ax.at(top)), to: toSvg(ax.at(end)) },
    grooves: head.grooves.map(([a, b]) => `M${point(a)} L${point(b)}`).join(' '),
    centreHalfWidth: head.centreHalfWidth,
    minX: head.minX,
    maxX: head.maxX,
    ...crossings(head.face.points),
  };
}

/**
 * Irons (and hybrids and wedges): one silhouette for blade and hosel. A straight leading
 * edge with a rounded heel corner, a slim hosel leaning 27° that tapers from 6.4 to 5 mm
 * half width and flows into the blade with a fillet, a top line rising toward the toe,
 * a rounded toe, nine score lines.
 */
function ironShape(): ImpactFaceShape {
  const lean = 27;
  const [bottomHalfWidth, topHalfWidth] = [6.4, 5.0];
  const hoselTop = 48;
  const heelCorner: Point = [-37, 0];
  // The axis sits half a hosel width toe-side of the heel corner, so the hosel's heel side starts at the corner.
  const base = add(heelCorner, scale(axis([0, 0], lean).across, bottomHalfWidth));
  const ax = axis(base, lean);
  const halfWidth = (s: number) => bottomHalfWidth + (topHalfWidth - bottomHalfWidth) * Math.min(1, s / hoselTop);
  const outer = (s: number) => ax.side(s, halfWidth(s), -1);
  const inner = (s: number) => ax.side(s, halfWidth(s), 1);
  const topLine = (x: number) => 28 + 0.306 * (x + 37);
  // Where the hosel's toe side meets the top line: the crotch.
  let crotch = 0;
  for (let s = 0; s < 80; s += 0.05) {
    const p = inner(s);
    if (p[1] >= topLine(p[0])) {
      crotch = s;
      break;
    }
  }
  const toeStart: Point = [12, topLine(12)];
  const heel: Segment[] = [{ L: [-31, 0] }, { Q: [heelCorner, outer(7.5)] }];
  // From the crotch's start over its fillet along the top line, round the toe back to the leading edge.
  const blade: Segment[] = [
    { Q: [inner(crotch), [-29.5, topLine(-29.5)]] },
    { L: toeStart },
    { C: [[24.4, 46.8], [38, 38], [38, 24]] },
    { C: [[38, 9], [34.5, 0], [25, 0]] },
  ];
  const back: Segment[] = [{ L: inner(crotch + 7) }, ...blade];
  const body = build([{ M: [25, 0] }, ...heel, { L: outer(hoselTop) }, { L: inner(hoselTop) }, ...back]);
  // Open at the hosel top: the shaft lines go on from there.
  const edge = build([{ M: inner(hoselTop) }, ...back, ...heel, { L: outer(hoselTop) }], false).d;
  // The hitting area: the blade with the hosel cut off straight from the heel corner's end to the crotch's start.
  const face = build([{ M: [25, 0] }, ...heel, ...back]);
  const grooves: [Point, Point][] = [];
  for (let k = 0; k < 9; k += 1) {
    const y = 3.6 + 3.4 * k;
    // They start a groove's pitch under the top line at the heel, at most 24 mm out.
    const from = Math.max(-24, -37 + (y + 3.6 - 28) / 0.306);
    if (22 - from > 8) grooves.push([[from, y], [22, y]]);
  }
  return finish({
    kind: 'iron',
    body,
    edge,
    face,
    plate: null,
    axis: ax,
    hoselTop,
    hoselTopHalfWidth: topHalfWidth,
    hosel: null,
    shaftLength: 10,
    grooves,
    centreHalfWidth: CENTRE_HALF_WIDTH_MM.irons,
    minX: -29,
    maxX: 26,
  });
}

/**
 * Woods: an asymmetric head, higher at the toe than at the heel, with an inner face plate
 * (the hitting area) and a hosel leaning 28° behind the heel's crown. The driver is the deep
 * head, the fairway wood the shallow one; seven score lines each.
 */
function woodShape(kind: 'driver' | 'fairway'): ImpactFaceShape {
  const driver = kind === 'driver';
  const face = driver
    ? build([
        { M: [-36, 0] },
        { L: [30, 0] },
        { C: [[45, 0], [55, 8], [56, 22]] },
        { C: [[57, 38], [50, 52], [34, 57]] },
        { C: [[16, 61], [-12, 56], [-32, 47]] },
        { C: [[-44, 41], [-51, 32], [-51, 22]] },
        { C: [[-51, 10], [-46, 0.4], [-36, 0]] },
      ])
    : build([
        { M: [-30, 0] },
        { L: [26, 0] },
        { C: [[38, 0], [46, 5], [47, 15]] },
        { C: [[48, 27], [42, 35], [28, 38]] },
        { C: [[13, 41], [-10, 37.5], [-27, 31.5]] },
        { C: [[-37, 28], [-43, 21], [-43, 14]] },
        { C: [[-43, 6], [-39, 0.3], [-30, 0]] },
      ]);
  // The body: a rim around the plate, its sole a little under the leading edge.
  const body = driver
    ? build([
        { M: [-37, -2] },
        { C: [[-12, -3], [12, -3], [32, -2]] },
        { C: [[50, -1.5], [60.5, 8], [60.5, 23]] },
        { C: [[61, 42], [53, 56.5], [35, 61.5]] },
        { C: [[15, 66], [-14, 60.5], [-35, 51]] },
        { C: [[-48, 45], [-55.5, 35], [-55.5, 22]] },
        { C: [[-55.5, 8], [-49, -1], [-37, -2]] },
      ])
    : build([
        { M: [-31, -1.8] },
        { C: [[-10, -2.6], [10, -2.6], [28, -1.8]] },
        { C: [[43, -1.2], [51.5, 5], [51.5, 15.5]] },
        { C: [[52, 30], [45, 39.5], [29, 42.5]] },
        { C: [[12, 45.5], [-12, 41.5], [-30, 35.5]] },
        { C: [[-41, 31.5], [-47.5, 23], [-47.5, 14]] },
        { C: [[-47.5, 4], [-42, -1.2], [-31, -1.8]] },
      ]);
  const halfWidth = driver ? 6.4 : 6.0;
  const grooves: [Point, Point][] = [];
  for (let i = 0; i < 7; i += 1) {
    const [y, reach] = driver ? [11 + 5 * i, 22] : [6 + 3.6 * i, 20];
    grooves.push([[-reach, y], [reach, y]]);
  }
  return finish({
    kind,
    body,
    edge: body.d,
    face,
    plate: face.d,
    axis: axis(driver ? [-47, 33] : [-41, 22], 28),
    hoselTop: driver ? 20 : 14,
    hoselTopHalfWidth: halfWidth,
    // It starts 14 mm inside the head, so the body covers its foot.
    hosel: { from: -14, halfWidth },
    shaftLength: driver ? 9 : 8,
    grooves,
    centreHalfWidth: CENTRE_HALF_WIDTH_MM.woods,
    minX: driver ? -38 : -31,
    maxX: driver ? 42 : 34,
  });
}

export function impactFaceShape(kind: ImpactFaceKind): ImpactFaceShape {
  return kind === 'iron' ? ironShape() : woodShape(kind);
}

/**
 * The dot's centre on the face (face mm, y up): the offset clamped inside the outline,
 * at the measured height (clamped too) or half way up the face at that x.
 */
export function impactDotPosition(shape: ImpactFaceShape, offsetMm: number, heightMm: number | null): { x: number; y: number } {
  const x = Math.max(shape.minX, Math.min(shape.maxX, offsetMm));
  const r = IMPACT_DOT_RADIUS_MM;
  const keep = r + DOT_CLEARANCE_MM;
  // The tightest edge under and over the whole dot, not just its centre (the corners slope).
  const xs = [x - r, x, x + r];
  const floor = Math.max(...xs.map(shape.bottomAt));
  const ceiling = Math.min(...xs.map(shape.topAt));
  const low = floor + keep;
  const high = ceiling - keep;
  if (heightMm === null || low > high) return { x, y: (floor + ceiling) / 2 };
  return { x, y: Math.max(low, Math.min(high, heightMm)) };
}
