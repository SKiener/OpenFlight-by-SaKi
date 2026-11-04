import { useLayoutEffect, useRef, type RefObject } from 'react';

export function rowOverflowsWidth(row: HTMLElement, slop = 1): boolean {
  if (row.scrollWidth > row.clientWidth + slop) {
    return true;
  }
  const box = row.getBoundingClientRect();
  const children = [...row.children];
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index];
    if (!(child instanceof HTMLElement)) {
      continue;
    }
    const range = document.createRange();
    range.selectNodeContents(child);
    const ink = range.getBoundingClientRect();
    const childBox = child.getBoundingClientRect();
    const right = Math.max(childBox.right, ink.right || 0);
    if (right > box.right + slop) {
      return true;
    }
    const next = children[index + 1];
    if (next instanceof HTMLElement && ink.right > next.getBoundingClientRect().left + slop) {
      return true;
    }
  }
  return false;
}

/**
 * One size for every row: the CSS ceiling, or smaller if the widest row still overflows.
 * `overflows` decides what "overflows" means (by default: wider than the row,
 * {@link rowOverflowsWidth}). Returns false only when a row still overflows at `minPx`.
 */
export function applySharedFitFontSize(
  rows: HTMLElement[],
  minPx = 12,
  overflows: (row: HTMLElement) => boolean = rowOverflowsWidth
): boolean {
  if (rows.length === 0) {
    return true;
  }
  for (const row of rows) {
    row.style.fontSize = '';
  }
  const maxPx = Math.min(...rows.map((row) => parseFloat(getComputedStyle(row).fontSize)));
  if (!Number.isFinite(maxPx) || maxPx <= 0 || rows.some((row) => row.clientWidth <= 0)) {
    return true;
  }

  const apply = (px: number) => {
    for (const row of rows) {
      row.style.fontSize = `${px}px`;
    }
  };
  const fits = (px: number) => {
    apply(px);
    return rows.every((row) => !overflows(row));
  };

  if (fits(maxPx)) {
    for (const row of rows) {
      row.style.fontSize = '';
    }
    return true;
  }

  let low = minPx;
  let high = maxPx;
  let best = minPx;
  let fitted = false;
  for (let step = 0; step < 14; step += 1) {
    const mid = (low + high) / 2;
    if (fits(mid)) {
      best = mid;
      fitted = true;
      low = mid;
    } else {
      high = mid;
    }
  }
  if (fitted) {
    apply(best);
    return true;
  }
  // Nothing in the search fitted: the floor, as before, and whether it fits there.
  return fits(minPx);
}

/**
 * Fit a grid's value rows in two groups: numbers share one size, class words
 * (Draw, Gerade) share their own, so a long word never shrinks the numbers.
 */
export function applyGridFitFontSize(grid: ParentNode): void {
  const rows = [...grid.querySelectorAll<HTMLElement>('.metric-card__value-row')];
  const isClass = (row: HTMLElement) => row.closest('.metric-card--class') !== null;
  applySharedFitFontSize(rows.filter((row) => !isClass(row)));
  applySharedFitFontSize(rows.filter(isClass));
}

/**
 * Fit each value row of a grid on its own: the CSS size (the kiosk's tile height)
 * is the ceiling, and a row shrinks only as far as its own width demands, so
 * "10.000" or "Gerade" never shrinks the numbers next to it.
 */
export function applyEachFitFontSize(grid: ParentNode, selector: string): void {
  for (const row of grid.querySelectorAll<HTMLElement>(selector)) {
    applySharedFitFontSize([row]);
  }
}

/** How a grid's value rows are fitted: one shared size per group, or each row on its own. */
export type FitMode = 'shared' | 'each';

const EACH_ROW_SELECTOR = '.kiosk-tile__value-row';

/**
 * Run `fit` now and again whenever the layout under `root` may have changed:
 * the next two frames, 50 ms later (cqi/cqb settle), once webfonts are ready,
 * on resize, and when the text under `root` changes. Returns the cleanup.
 */
export function refitOnLayoutChange(root: HTMLElement, fit: () => void): () => void {
  let cancelled = false;
  const mutationObserver = new MutationObserver(() => {
    refit();
  });
  const refit = () => {
    if (cancelled) {
      return;
    }
    mutationObserver.disconnect();
    fit();
    if (!cancelled) {
      mutationObserver.observe(root, { subtree: true, characterData: true, childList: true });
    }
  };

  // cqi/cqb and webfonts can change size after the first layout; refit then.
  refit();
  const frame = requestAnimationFrame(() => {
    refit();
    requestAnimationFrame(refit);
  });
  const retry = window.setTimeout(refit, 50);
  void document.fonts.ready.then(refit);
  const resizeObserver = new ResizeObserver(refit);
  resizeObserver.observe(root);
  return () => {
    cancelled = true;
    cancelAnimationFrame(frame);
    window.clearTimeout(retry);
    resizeObserver.disconnect();
    mutationObserver.disconnect();
  };
}

/**
 * Fit every `.metric-card__value-row` in a grid to the same font-size so Live
 * numbers stay consistent (181 and 2,328 look the same size); class words fit
 * in a group of their own ({@link applyGridFitFontSize}). `each` fits every
 * `.kiosk-tile__value-row` on its own ({@link applyEachFitFontSize}).
 */
export function useSharedFitFontSize(enabled: boolean, token: string, mode: FitMode = 'shared'): RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const grid = ref.current;
    if (!enabled || !grid) {
      return undefined;
    }

    const stop = refitOnLayoutChange(grid, () => {
      if (mode === 'each') applyEachFitFontSize(grid, EACH_ROW_SELECTOR);
      else applyGridFitFontSize(grid);
    });
    return () => {
      stop();
      for (const row of grid.querySelectorAll<HTMLElement>(mode === 'each' ? EACH_ROW_SELECTOR : '.metric-card__value-row')) {
        row.style.fontSize = '';
      }
    };
  }, [enabled, token, mode]);

  return ref;
}

/** Lines a label's text takes: client rects whose tops lie within half a line share one line. */
export function lineCount(rects: ArrayLike<DOMRect>, linePx: number): number {
  const tops = Array.from(rects)
    .filter((rect) => rect.width > 0)
    .map((rect) => rect.top)
    .sort((a, b) => a - b);
  let lines = 0;
  let previous = Number.NEGATIVE_INFINITY;
  for (const top of tops) {
    if (top > previous + linePx / 2) {
      lines += 1;
      previous = top;
    }
  }
  return lines;
}

/**
 * Overflow test for a tile whose label is its first element child (or its own
 * text): the label's text must stay inside the tile's content box (border and
 * padding off) on all four sides and take at most `maxLines` lines. Labels
 * break only at spaces (CSS), so a word too wide for the tile shows up here as
 * text past the content box, and too many lines as text past its bottom.
 */
export function labelOverflowsBox(maxLines: number, slop = 1): (box: HTMLElement) => boolean {
  return (box) => {
    const label = box.firstElementChild instanceof HTMLElement ? box.firstElementChild : box;
    const style = getComputedStyle(box);
    const outer = box.getBoundingClientRect();
    const left = outer.left + (parseFloat(style.borderLeftWidth) || 0) + (parseFloat(style.paddingLeft) || 0);
    const right = outer.right - (parseFloat(style.borderRightWidth) || 0) - (parseFloat(style.paddingRight) || 0);
    const top = outer.top + (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.paddingTop) || 0);
    const bottom = outer.bottom - (parseFloat(style.borderBottomWidth) || 0) - (parseFloat(style.paddingBottom) || 0);

    const range = document.createRange();
    range.selectNodeContents(label);
    const ink = range.getBoundingClientRect();
    if (ink.width === 0 && ink.height === 0) {
      return false;
    }
    if (ink.left < left - slop || ink.right > right + slop || ink.top < top - slop || ink.bottom > bottom + slop) {
      return true;
    }
    const fontPx = parseFloat(getComputedStyle(label).fontSize) || 16;
    return lineCount(range.getClientRects(), fontPx) > maxLines;
  };
}

/** Labels may take two lines; three only once two no longer fit at the floor. */
export const LABEL_LINES: readonly number[] = [2, 3];
/** Below the floor only so text never clips (a three-line name in a 48 px tile). */
const LABEL_LAST_RESORT_PX = 12;

/**
 * One size for every tile label of a set (one picker tab): the CSS ceiling, or
 * the largest size at which every label fits its tile in `lines[0]` lines (two),
 * or else in the next count (three). Should even the last count not fit at
 * `minPx`, the labels go below it, down to 12 px, rather than clip.
 */
export function applyLabelFitFontSize(
  boxes: HTMLElement[],
  minPx: number,
  lines: readonly number[] = LABEL_LINES
): void {
  if (boxes.length === 0) {
    return;
  }
  for (const box of boxes) {
    box.style.fontSize = '';
  }
  const ceiling = Math.min(...boxes.map((box) => parseFloat(getComputedStyle(box).fontSize)));
  // A phone's ceiling can sit below the kiosk floor; the floor never lifts a label past its ceiling.
  const floor = Number.isFinite(ceiling) ? Math.min(minPx, ceiling) : minPx;
  for (const count of lines) {
    if (applySharedFitFontSize(boxes, floor, labelOverflowsBox(count))) {
      return;
    }
  }
  const mostLines = lines[lines.length - 1] ?? 1;
  applySharedFitFontSize(boxes, Math.min(LABEL_LAST_RESORT_PX, floor), labelOverflowsBox(mostLines));
}

/**
 * Fit the tiles under the returned ref (`selector`) with {@link applyLabelFitFontSize}:
 * on mount, whenever `token` changes (a new tab), on resize and once webfonts load.
 * `minRem` is the floor in rem, read against `html`'s fluid font-size at each fit;
 * `lines` the line counts to try in turn (one line for a row of tabs).
 */
export function useLabelFitFontSize<T extends HTMLElement>(
  token: string,
  selector: string,
  minRem: number,
  lines: readonly number[] = LABEL_LINES
): RefObject<T | null> {
  const ref = useRef<T>(null);

  useLayoutEffect(() => {
    const root = ref.current;
    if (!root) {
      return undefined;
    }
    const stop = refitOnLayoutChange(root, () => {
      const remPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
      applyLabelFitFontSize([...root.querySelectorAll<HTMLElement>(selector)], minRem * remPx, lines);
    });
    return () => {
      stop();
      for (const box of root.querySelectorAll<HTMLElement>(selector)) {
        box.style.fontSize = '';
      }
    };
  }, [token, selector, minRem, lines]);

  return ref;
}
