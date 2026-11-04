export interface LabelBox {
  id: string;
  priority: number;
  distance: number;
  behind: boolean;
  rect: [x0: number, y0: number, x1: number, y1: number];
}

type Rect = LabelBox['rect'];

export const LABEL_MARGIN_PX = 4;
export const SIGN_PRIORITY = Object.freeze({ landing: 0, target: 1, selectedGreen: 2, green: 3 });
export const TARGET_LABEL_CANVAS = Object.freeze({ width: 300, height: 120 });

// NaN never reads as overlap and breaks the sort order, so boxes with any non-finite number are dropped first.
const isUsable = ({ behind, priority, distance, rect }: LabelBox): boolean =>
  !behind && [priority, distance, ...rect].every(Number.isFinite) && rect[2] >= rect[0] && rect[3] >= rect[1];

// Ids compare by code unit, not localeCompare, so ties resolve the same on every browser and locale.
const compareIds = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

const byPrecedence = (a: LabelBox, b: LabelBox): number =>
  a.priority - b.priority || a.distance - b.distance || compareIds(a.id, b.id);

const gap = (aMin: number, aMax: number, bMin: number, bMax: number): number => Math.max(aMin - bMax, bMin - aMax);

const collide = (a: Rect, b: Rect, marginPx: number): boolean =>
  gap(a[0], a[2], b[0], b[2]) < marginPx && gap(a[1], a[3], b[1], b[3]) < marginPx;

/** `reserved` areas, such as bars lying over the canvas, keep every label off them as a shown label would. */
export function visibleLabels(
  boxes: readonly LabelBox[],
  marginPx = LABEL_MARGIN_PX,
  reserved: readonly Rect[] = []
): Set<string> {
  if (!Number.isFinite(marginPx) || marginPx < 0) {
    throw new RangeError(`marginPx must be a finite number of at least 0, got ${marginPx}`);
  }
  const shown = new Set<string>();
  const placed: Rect[] = [...reserved];
  for (const box of boxes.filter(isUsable).sort(byPrecedence)) {
    if (placed.some((rect) => collide(box.rect, rect, marginPx))) continue;
    placed.push(box.rect);
    shown.add(box.id);
  }
  return shown;
}
