import { describe, expect, it } from 'vitest';
import { SIGN_PRIORITY, visibleLabels, type LabelBox } from './labelLayout';

type Rect = LabelBox['rect'];

function box(id: string, rect: Rect, overrides: Partial<LabelBox> = {}): LabelBox {
  return { id, priority: 2, distance: 100, behind: false, rect, ...overrides };
}

const ids = (...names: string[]) => new Set(names);

const sideBySide = (gap: number) => [
  box('a', [0, 0, 100, 20], { priority: 0 }),
  box('b', [100 + gap, 0, 200 + gap, 20]),
];

function permutations<T>(items: T[]): T[][] {
  if (items.length <= 1) return [items];
  return items.flatMap((item, i) =>
    permutations([...items.slice(0, i), ...items.slice(i + 1)]).map((rest) => [item, ...rest])
  );
}

const ANCHOR: Rect = [100, 100, 180, 120];
const NEIGHBOURS: [string, (gap: number) => Rect][] = [
  ['right', (gap) => [180 + gap, 100, 260 + gap, 120]],
  ['left', (gap) => [20 - gap, 100, 100 - gap, 120]],
  ['below', (gap) => [100, 120 + gap, 180, 140 + gap]],
  ['above', (gap) => [100, 80 - gap, 180, 100 - gap]],
];

describe('visibleLabels', () => {
  it('keeps every label that has room', () => {
    const boxes = [box('near', [0, 0, 100, 20]), box('far', [300, 200, 400, 220])];
    expect(visibleLabels(boxes)).toEqual(ids('near', 'far'));
  });

  it('lets the more important label win an overlap', () => {
    const boxes = [
      box('minor', [0, 0, 100, 20], { priority: 2, distance: 50 }),
      box('major', [50, 5, 150, 25], { priority: 0, distance: 200 }),
    ];
    expect(visibleLabels(boxes)).toEqual(ids('major'));
  });

  it("lets the selected green's sign win over nearer green signs, below the landing sign and a target label", () => {
    const selected = box('green-4', [50, 5, 150, 25], { priority: SIGN_PRIORITY.selectedGreen, distance: 165 });
    const nearer = box('green-2', [0, 0, 100, 20], { priority: SIGN_PRIORITY.green, distance: 100 });
    expect(visibleLabels([nearer, selected])).toEqual(ids('green-4'));
    const landing = box('landing', [60, 10, 160, 30], { priority: SIGN_PRIORITY.landing, distance: 200 });
    expect(visibleLabels([nearer, selected, landing])).toEqual(ids('landing'));
    const target = box('target', [60, 10, 160, 30], { priority: 1, distance: 200 });
    expect(visibleLabels([nearer, selected, target])).toEqual(ids('target'));
  });

  it('lets the nearer label win among equals', () => {
    const boxes = [box('a-far', [0, 0, 100, 20], { distance: 120 }), box('z-near', [50, 5, 150, 25], { distance: 80 })];
    expect(visibleLabels(boxes)).toEqual(ids('z-near'));
  });

  it('hides labels behind the camera', () => {
    expect(visibleLabels([box('behind', [0, 0, 100, 20], { behind: true })])).toEqual(ids());
  });

  it('counts the margin as overlap', () => {
    expect(visibleLabels(sideBySide(3), 4)).toEqual(ids('a'));
    expect(visibleLabels(sideBySide(5), 4)).toEqual(ids('a', 'b'));
  });

  it('returns nothing for no labels', () => {
    expect(visibleLabels([])).toEqual(ids());
  });

  describe('margin', () => {
    it.each(NEIGHBOURS)('counts a gap below the margin as overlap, %s of the other label', (_side, place) => {
      const anchor = box('anchor', ANCHOR, { priority: 1 });
      const neighbour = (gap: number, priority: number) => box('neighbour', place(gap), { priority });
      expect(visibleLabels([anchor, neighbour(3, 2)], 4)).toEqual(ids('anchor'));
      expect(visibleLabels([anchor, neighbour(3, 0)], 4)).toEqual(ids('neighbour'));
    });

    it.each(NEIGHBOURS)('lets a gap equal to the margin stand, %s of the other label', (_side, place) => {
      const anchor = box('anchor', ANCHOR, { priority: 1 });
      const neighbour = (priority: number) => box('neighbour', place(4), { priority });
      expect(visibleLabels([anchor, neighbour(2)], 4)).toEqual(ids('anchor', 'neighbour'));
      expect(visibleLabels([anchor, neighbour(0)], 4)).toEqual(ids('anchor', 'neighbour'));
    });

    it.each([
      [3, 3, true],
      [3, 4, false],
      [4, 3, false],
      [3, -10, true],
      [-10, 3, true],
      [4, -10, false],
      [-10, 4, false],
    ])('needs a gap below the margin on both axes (x gap %i px, y gap %i px, overlap: %s)', (gapX, gapY, overlap) => {
      const other: Rect = [180 + gapX, 120 + gapY, 260 + gapX, 140 + gapY];
      const boxes = [box('anchor', ANCHOR, { priority: 1 }), box('other', other, { priority: 2 })];
      expect(visibleLabels(boxes, 4)).toEqual(overlap ? ids('anchor') : ids('anchor', 'other'));
    });

    it('uses 4 px when no margin is given', () => {
      expect(visibleLabels(sideBySide(3))).toEqual(ids('a'));
      expect(visibleLabels(sideBySide(4))).toEqual(ids('a', 'b'));
    });

    it('lets boxes touch at margin 0 but not overlap', () => {
      expect(visibleLabels(sideBySide(0), 0)).toEqual(ids('a', 'b'));
      expect(visibleLabels(sideBySide(-1), 0)).toEqual(ids('a'));
    });

    it.each([-1, -0.5, NaN, Infinity, -Infinity])('rejects a margin of %s', (margin) => {
      expect(() => visibleLabels([], margin)).toThrow(RangeError);
    });
  });

  describe('order', () => {
    it('breaks a full tie by id, whatever the input order', () => {
      const tied = ['c', 'a', 'b'].map((id) => box(id, [0, 0, 100, 20]));
      for (const order of permutations(tied)) {
        expect(visibleLabels(order)).toEqual(ids('a'));
      }
    });

    const ID_ORDER: [string[], string][] = [
      [['a', 'B'], 'B'],
      [['é', 'f'], 'f'],
      [['9', '10'], '10'],
    ];

    it.each(ID_ORDER)('compares ids by code unit, so of %j the winner is %s', (tiedIds, winner) => {
      const tied = tiedIds.map((id) => box(id, [0, 0, 100, 20]));
      expect(visibleLabels(tied)).toEqual(ids(winner));
      expect(visibleLabels([...tied].reverse())).toEqual(ids(winner));
    });

    it('does not let a hidden label block the next one', () => {
      const boxes = [
        box('c', [140, 0, 240, 20], { priority: 2 }),
        box('b', [70, 0, 170, 20], { priority: 1 }),
        box('a', [0, 0, 100, 20], { priority: 0 }),
      ];
      expect(visibleLabels(boxes)).toEqual(ids('a', 'c'));
    });

    it('hides a label that overlaps only one of the labels already shown', () => {
      const withPriorities = (a: number, b: number) => [
        box('a', [0, 0, 100, 20], { priority: a }),
        box('b', [200, 0, 300, 20], { priority: b }),
        box('c', [250, 0, 350, 20], { priority: 2 }),
      ];
      expect(visibleLabels(withPriorities(0, 1))).toEqual(ids('a', 'b'));
      expect(visibleLabels(withPriorities(1, 0))).toEqual(ids('a', 'b'));
    });

    it('leaves the boxes and their order alone', () => {
      const boxes = [
        box('late', [0, 0, 100, 20], { priority: 2, distance: 90 }),
        box('early', [50, 0, 150, 20], { priority: 0, distance: 10 }),
        box('middle', [400, 0, 500, 20], { priority: 1, distance: 40 }),
      ];
      const before = structuredClone(boxes);
      expect(visibleLabels(Object.freeze(boxes))).toEqual(ids('early', 'middle'));
      expect(boxes).toEqual(before);
    });
  });

  describe('reserved areas', () => {
    const BAR: Rect = [0, 0, 800, 60];

    it('hides a label under a reserved area or within the margin of one, whatever its priority', () => {
      const at = (top: number) => [box('landing', [100, top, 200, top + 20], { priority: 0 })];
      expect(visibleLabels(at(50), 4, [BAR])).toEqual(ids());
      expect(visibleLabels(at(63), 4, [BAR])).toEqual(ids());
      expect(visibleLabels(at(64), 4, [BAR])).toEqual(ids('landing'));
    });

    it('keeps a label clear of every reserved area, and never returns an area itself', () => {
      const footer: Rect = [0, 340, 800, 400];
      const boxes = [box('middle', [100, 100, 200, 120]), box('low', [300, 330, 400, 350])];
      expect(visibleLabels(boxes, 4, [BAR, footer])).toEqual(ids('middle'));
    });

    it('lets a weaker label show where a reserved area hid the stronger one it overlaps', () => {
      const boxes = [
        box('strong', [100, 40, 200, 70], { priority: 0 }),
        box('weak', [100, 66, 200, 86], { priority: 2 }),
      ];
      expect(visibleLabels(boxes, 4)).toEqual(ids('strong'));
      expect(visibleLabels(boxes, 4, [BAR])).toEqual(ids('weak'));
    });

    it('reserves nothing unless given areas', () => {
      const boxes = [box('top', [100, 0, 200, 20])];
      expect(visibleLabels(boxes)).toEqual(ids('top'));
      expect(visibleLabels(boxes, 4, [])).toEqual(ids('top'));
    });
  });

  describe('hidden labels', () => {
    const HIDDEN: [string, Partial<LabelBox>][] = [
      ['is behind the camera', { behind: true }],
      ['has a NaN left edge', { rect: [NaN, 0, 100, 20] }],
      ['has a NaN top edge', { rect: [0, NaN, 100, 20] }],
      ['has a NaN right edge', { rect: [0, 0, NaN, 20] }],
      ['has a NaN bottom edge', { rect: [0, 0, 100, NaN] }],
      ['reaches +Infinity to the right', { rect: [0, 0, Infinity, 20] }],
      ['reaches -Infinity to the left', { rect: [-Infinity, 0, 100, 20] }],
      ['reaches +Infinity down', { rect: [0, 0, 100, Infinity] }],
      ['reaches -Infinity up', { rect: [0, -Infinity, 100, 20] }],
      ['is inverted in x', { rect: [100, 0, 0, 20] }],
      ['is inverted in y', { rect: [0, 20, 100, 0] }],
      ['has a NaN priority', { priority: NaN }],
      ['has a priority of +Infinity', { priority: Infinity }],
      ['has a priority of -Infinity', { priority: -Infinity }],
      ['has a NaN distance', { distance: NaN }],
      ['has a distance of +Infinity', { distance: Infinity }],
      ['has a distance of -Infinity', { distance: -Infinity }],
    ];

    it.each(HIDDEN)('hides a label that %s and does not let it block another', (_what, overrides) => {
      const hidden = box('hidden', [0, 0, 100, 20], { priority: 0, ...overrides });
      expect(visibleLabels([hidden])).toEqual(ids());
      expect(visibleLabels([hidden, box('other', [10, 5, 90, 15])])).toEqual(ids('other'));
    });

    it('hides labels with a NaN priority or a NaN distance and keeps the one they overlap', () => {
      const boxes = [
        box('nan-priority', [0, 0, 100, 20], { priority: NaN }),
        box('nan-distance', [20, 0, 120, 20], { priority: 0, distance: NaN }),
        box('normal', [10, 5, 90, 15]),
      ];
      expect(visibleLabels(boxes)).toEqual(ids('normal'));
    });

    it('keeps a label that is a single point or a line', () => {
      expect(visibleLabels([box('dot', [5, 5, 5, 5]), box('line', [50, 5, 50, 25])])).toEqual(ids('dot', 'line'));
    });
  });

  describe('on random scenes', () => {
    function seededRandom(seed: number): () => number {
      let state = seed;
      return () => {
        state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
        return state / 2 ** 32;
      };
    }

    function shuffled<T>(items: T[], random: () => number): T[] {
      const copy = [...items];
      for (let i = copy.length - 1; i > 0; i--) {
        const j = Math.floor(random() * (i + 1));
        [copy[i], copy[j]] = [copy[j], copy[i]];
      }
      return copy;
    }

    const precedes = (a: LabelBox, b: LabelBox) =>
      a.priority !== b.priority
        ? a.priority < b.priority
        : a.distance !== b.distance
          ? a.distance < b.distance
          : a.id < b.id;

    const overlaps = (a: Rect, b: Rect, margin: number) =>
      b[0] < a[2] + margin && b[2] > a[0] - margin && b[1] < a[3] + margin && b[3] > a[1] - margin;

    function randomScene(random: () => number): LabelBox[] {
      const int = (below: number) => Math.floor(random() * below);
      const sometimesBroken = (value: number) => (int(25) === 0 ? [NaN, Infinity, -Infinity][int(3)] : value);
      return Array.from({ length: 4 + int(12) }, (_unused, i) => {
        const x0 = int(160);
        const y0 = int(50);
        const rect: Rect = [x0, y0, x0 + 10 + int(60), y0 + 8 + int(16)];
        if (int(20) === 0) rect[int(4)] = NaN;
        return box(`label-${i}`, rect, {
          priority: sometimesBroken(int(3)),
          distance: sometimesBroken(50 * (1 + int(3))),
          behind: int(10) === 0,
        });
      });
    }

    it('shows labels that never overlap and hides only labels that lose to a shown one', () => {
      const random = seededRandom(20261007);
      let shownCount = 0;
      let lostCount = 0;
      for (let scene = 0; scene < 300; scene++) {
        const boxes = randomScene(random);
        const margin = [0, 3, 4][scene % 3];
        const shown = visibleLabels(boxes, margin);
        const usable = boxes.filter((b) => !b.behind && [b.priority, b.distance, ...b.rect].every(Number.isFinite));
        const visible = usable.filter((b) => shown.has(b.id));
        const lost = usable.filter((b) => !shown.has(b.id));

        expect(shown.size).toBe(visible.length);
        for (const a of visible) {
          for (const b of visible) {
            if (a !== b) expect(overlaps(a.rect, b.rect, margin)).toBe(false);
          }
        }
        for (const loser of lost) {
          expect(visible.some((winner) => precedes(winner, loser) && overlaps(winner.rect, loser.rect, margin))).toBe(
            true
          );
        }
        shownCount += visible.length;
        lostCount += lost.length;
      }
      expect(shownCount).toBeGreaterThan(300);
      expect(lostCount).toBeGreaterThan(300);
    });

    it('gives the same labels for any input order', () => {
      const random = seededRandom(7);
      for (let scene = 0; scene < 100; scene++) {
        const boxes = randomScene(random);
        expect(visibleLabels(shuffled(boxes, random))).toEqual(visibleLabels(boxes));
      }
    });

    it('keeps every shown label clear of the reserved areas, hiding only labels that lose to one or to a label', () => {
      const random = seededRandom(20261008);
      const reserved: Rect[] = [
        [0, 0, 240, 12],
        [0, 60, 240, 74],
      ];
      let hiddenByArea = 0;
      for (let scene = 0; scene < 300; scene++) {
        const boxes = randomScene(random);
        const margin = [0, 3, 4][scene % 3];
        const shown = visibleLabels(boxes, margin, reserved);
        const usable = boxes.filter((b) => !b.behind && [b.priority, b.distance, ...b.rect].every(Number.isFinite));
        const visible = usable.filter((b) => shown.has(b.id));

        for (const label of visible) {
          for (const area of reserved) expect(overlaps(label.rect, area, margin)).toBe(false);
        }
        for (const loser of usable.filter((b) => !shown.has(b.id))) {
          const underArea = reserved.some((area) => overlaps(loser.rect, area, margin));
          if (underArea) hiddenByArea += 1;
          expect(
            underArea || visible.some((winner) => precedes(winner, loser) && overlaps(winner.rect, loser.rect, margin))
          ).toBe(true);
        }
      }
      expect(hiddenByArea).toBeGreaterThan(300);
    });
  });
});
