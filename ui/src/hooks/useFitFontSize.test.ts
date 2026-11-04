import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  applyGridFitFontSize,
  applyLabelFitFontSize,
  applySharedFitFontSize,
  labelOverflowsBox,
  lineCount,
  rowOverflowsWidth,
} from './useFitFontSize';

function fakeRow(contentWidthAt16: number, boxWidth: number): HTMLElement {
  const row = {
    style: { fontSize: '16px' },
    children: [] as unknown as HTMLCollection,
    get clientWidth() {
      return boxWidth;
    },
    get scrollWidth() {
      const px = parseFloat(this.style.fontSize) || 16;
      return (contentWidthAt16 * px) / 16;
    },
    getBoundingClientRect() {
      return { left: 0, right: boxWidth, top: 0, bottom: 20, width: boxWidth, height: 20, x: 0, y: 0, toJSON() {} };
    },
  };
  return row as unknown as HTMLElement;
}

describe('applySharedFitFontSize', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses one size for every row, set by the widest value', () => {
    const short = fakeRow(50, 100);
    const wide = fakeRow(120, 100);
    vi.stubGlobal('getComputedStyle', (element: Element) => ({
      fontSize: (element as HTMLElement).style.fontSize || '16px',
    }));

    applySharedFitFontSize([short, wide]);

    expect(short.style.fontSize).toBe(wide.style.fontSize);
    expect(parseFloat(wide.style.fontSize)).toBeLessThan(16);
    expect(rowOverflowsWidth(wide)).toBe(false);
  });

  it('keeps the CSS size when every row already fits', () => {
    const a = fakeRow(40, 100);
    const b = fakeRow(60, 100);
    vi.stubGlobal('getComputedStyle', (element: Element) => ({
      fontSize: (element as HTMLElement).style.fontSize || '16px',
    }));

    applySharedFitFontSize([a, b]);

    expect(a.style.fontSize).toBe('');
    expect(b.style.fontSize).toBe('');
  });
});

describe('applyGridFitFontSize', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('fits numbers and class words in two groups, so a long word never shrinks the numbers', () => {
    const asRow = (row: HTMLElement, isClass: boolean) =>
      Object.assign(row, {
        closest: (selector: string) => (selector === '.metric-card--class' && isClass ? row : null),
      });
    const short = asRow(fakeRow(50, 100), false);
    const number = asRow(fakeRow(90, 100), false);
    const word = asRow(fakeRow(120, 100), true);
    const shortWord = asRow(fakeRow(40, 100), true);
    const grid = { querySelectorAll: () => [short, number, word, shortWord] } as unknown as ParentNode;
    vi.stubGlobal('getComputedStyle', (element: Element) => ({
      fontSize: (element as HTMLElement).style.fontSize || '16px',
    }));

    applyGridFitFontSize(grid);

    // The numbers fit at their ceiling and keep it.
    expect(short.style.fontSize).toBe('');
    expect(number.style.fontSize).toBe('');
    // The words share one smaller size of their own.
    expect(parseFloat(word.style.fontSize)).toBeLessThan(16);
    expect(shortWord.style.fontSize).toBe(word.style.fontSize);
    expect(rowOverflowsWidth(word)).toBe(false);
  });
});

describe('applySharedFitFontSize with an overflow predicate', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const stubFontSize = () =>
    vi.stubGlobal('getComputedStyle', (element: Element) => ({
      fontSize: (element as HTMLElement).style.fontSize || '16px',
    }));

  it('shrinks until the predicate passes and reports the fit', () => {
    stubFontSize();
    const row = fakeRow(10, 100);
    const tooBigAbove = (box: HTMLElement) => parseFloat(box.style.fontSize || '16') > 11;

    expect(applySharedFitFontSize([row], 8, tooBigAbove)).toBe(true);
    expect(parseFloat(row.style.fontSize)).toBeLessThanOrEqual(11);
    expect(parseFloat(row.style.fontSize)).toBeGreaterThan(10.9);
  });

  it('reports false, and leaves the floor applied, when nothing fits even at minPx', () => {
    stubFontSize();
    const row = fakeRow(10, 100);

    expect(applySharedFitFontSize([row], 9, () => true)).toBe(false);
    expect(row.style.fontSize).toBe('9px');
  });

  it('still uses the width test when no predicate is given', () => {
    stubFontSize();
    const wide = fakeRow(120, 100);

    expect(applySharedFitFontSize([wide])).toBe(true);
    expect(rowOverflowsWidth(wide)).toBe(false);
  });
});

/*
 * A tiny layout model of a picker tile: a button (border 1, `padding` each
 * side) with a centred label whose words break only at spaces, lines at 1.05,
 * and a text ink 1.2 em tall per line box. Word widths are given at 16 px.
 */
class FakeElement {}

interface FakeTile {
  tile: HTMLElement;
  label: HTMLElement;
  /** Lines the label took at the size applied last. */
  lines: () => number;
}

function fakeTile(words: number[], { width = 200, height = 100, padding = 8, ceiling = 40 } = {}): FakeTile {
  const style = { fontSize: '' };
  const fontPx = () => parseFloat(style.fontSize) || ceiling;
  const layout = () => {
    const px = fontPx();
    const content = width - 2 * (padding + 1);
    const space = (4 * px) / 16;
    const widths: number[] = [];
    for (const word of words.map((w) => (w * px) / 16)) {
      const last = widths.length - 1;
      if (last >= 0 && widths[last] + space + word <= content) widths[last] += space + word;
      else widths.push(word);
    }
    const inkWidth = Math.max(...widths);
    const inkHeight = (widths.length - 1) * 1.05 * px + 1.2 * px;
    const left = (width - inkWidth) / 2;
    const top = (height - inkHeight) / 2;
    const rects = widths.map((w, index) => ({
      left: (width - w) / 2,
      right: (width + w) / 2,
      width: w,
      top: top + index * 1.05 * px,
    }));
    return {
      ink: { left, right: left + inkWidth, top, bottom: top + inkHeight, width: inkWidth, height: inkHeight },
      rects,
    };
  };
  const label = Object.assign(new FakeElement(), { kind: 'label', layout, fontPx }) as unknown as HTMLElement;
  const tile = Object.assign(new FakeElement(), {
    kind: 'tile',
    style,
    firstElementChild: label,
    clientWidth: width,
    fontPx,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: width, bottom: height, width, height }),
    padding,
  }) as unknown as HTMLElement;
  return { tile, label, lines: () => layout().rects.length };
}

function stubLayout() {
  vi.stubGlobal('HTMLElement', FakeElement);
  vi.stubGlobal('getComputedStyle', (element: { kind: string; fontPx: () => number; padding?: number }) => {
    const pad = `${element.padding ?? 0}px`;
    return {
      fontSize: `${element.fontPx()}px`,
      paddingLeft: pad,
      paddingRight: pad,
      paddingTop: '0px',
      paddingBottom: '0px',
      borderLeftWidth: '1px',
      borderRightWidth: '1px',
      borderTopWidth: '1px',
      borderBottomWidth: '1px',
    };
  });
  vi.stubGlobal('document', {
    createRange: () => {
      let node: { layout: () => { ink: object; rects: object[] } } | null = null;
      return {
        selectNodeContents: (target: { layout: () => { ink: object; rects: object[] } }) => {
          node = target;
        },
        getBoundingClientRect: () => node!.layout().ink,
        getClientRects: () => node!.layout().rects,
      };
    },
  });
}

describe('lineCount', () => {
  it('counts rects on one line once, and skips empty rects', () => {
    const rect = (top: number, width = 10) => ({ top, width }) as DOMRect;
    expect(lineCount([rect(0), rect(2), rect(21), rect(42), rect(80, 0)], 20)).toBe(3);
    expect(lineCount([], 20)).toBe(0);
  });
});

describe('labelOverflowsBox', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('passes a label inside the content box within the line limit', () => {
    stubLayout();
    const { tile } = fakeTile([40], { ceiling: 20 });
    expect(labelOverflowsBox(1)(tile)).toBe(false);
  });

  it('fails a word wider than the content box (padding counts)', () => {
    stubLayout();
    // 190 px of word in a 200 px tile with 8 px padding: 182 px of content.
    const { tile } = fakeTile([152], { ceiling: 20 });
    expect(labelOverflowsBox(3)(tile)).toBe(true);
  });

  it('fails a label that takes more lines than allowed, or runs past the bottom', () => {
    stubLayout();
    const { tile, lines } = fakeTile([100, 100], { ceiling: 20 });
    expect(lines()).toBe(2);
    expect(labelOverflowsBox(1)(tile)).toBe(true);
    expect(labelOverflowsBox(2)(tile)).toBe(false);
    const { tile: low } = fakeTile([100, 100], { ceiling: 20, height: 40 });
    expect(labelOverflowsBox(2)(low)).toBe(true);
  });
});

describe('applyLabelFitFontSize', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps the CSS ceiling when every label fits there', () => {
    stubLayout();
    const tiles = [fakeTile([20]), fakeTile([24])];
    applyLabelFitFontSize(
      tiles.map((t) => t.tile),
      14
    );
    expect(tiles.map((t) => t.tile.style.fontSize)).toEqual(['', '']);
  });

  it('gives every label of the set the one size the widest needs, in two lines', () => {
    stubLayout();
    // "Mini DR" next to "DR": the long label decides for both.
    const short = fakeTile([20]);
    // At the 40 px ceiling its first word alone is 200 px, past 182 px of content.
    const long = fakeTile([80, 30]);
    applyLabelFitFontSize([short.tile, long.tile], 14);
    expect(short.tile.style.fontSize).toBe(long.tile.style.fontSize);
    const px = parseFloat(long.tile.style.fontSize);
    expect(px).toBeLessThan(40);
    expect(px).toBeGreaterThanOrEqual(14);
    expect(long.lines()).toBeLessThanOrEqual(2);
    expect(labelOverflowsBox(2)(long.tile)).toBe(false);
  });

  it('allows a third line only once two lines no longer fit at the floor', () => {
    stubLayout();
    // Two of these words share a line only below 23.5 px, under the 30 px floor; three lines fit up to 36.4 px (118 px of content, 1 px slop each way).
    const three = fakeTile([60, 60, 60], { height: 120 });
    applyLabelFitFontSize([three.tile], 30);
    expect(three.lines()).toBe(3);
    expect(parseFloat(three.tile.style.fontSize)).toBeGreaterThanOrEqual(30);
    expect(parseFloat(three.tile.style.fontSize)).toBeCloseTo(36.4, 0);
    expect(labelOverflowsBox(3)(three.tile)).toBe(false);
  });

  it('goes below the floor, never under 12 px, rather than clip one long word', () => {
    stubLayout();
    // A 240-px word (at 16 px) fits 182 px of content only at 12.1 px: below the 16 px floor.
    const word = fakeTile([240]);
    applyLabelFitFontSize([word.tile], 16);
    expect(parseFloat(word.tile.style.fontSize)).toBeLessThan(16);
    expect(parseFloat(word.tile.style.fontSize)).toBeGreaterThanOrEqual(12);
    expect(labelOverflowsBox(3)(word.tile)).toBe(false);
  });

  it('never lifts a label above its CSS ceiling when the floor is higher (phones)', () => {
    stubLayout();
    const label = fakeTile([60, 60], { ceiling: 13 });
    applyLabelFitFontSize([label.tile], 16);
    expect(label.tile.style.fontSize).toBe('');
  });

  it('tries the given line counts (one line for tabs)', () => {
    stubLayout();
    const tab = fakeTile([100, 100], { ceiling: 20 });
    applyLabelFitFontSize([tab.tile], 8, [1]);
    expect(tab.lines()).toBe(1);
    expect(parseFloat(tab.tile.style.fontSize)).toBeLessThan(20);
  });
});
