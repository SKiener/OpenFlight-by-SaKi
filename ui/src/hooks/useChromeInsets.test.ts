import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RangeInsets } from '../virtual-range/insets';
import { watchChromeInsets } from './useChromeInsets';

class FakeResizeObserver {
  static last: FakeResizeObserver | null = null;
  readonly callback: () => void;
  observed: Element[] = [];
  disconnected = false;

  constructor(callback: () => void) {
    this.callback = callback;
    FakeResizeObserver.last = this;
  }

  observe(element: Element): void {
    this.observed.push(element);
  }

  disconnect(): void {
    this.disconnected = true;
  }

  /** What the browser does after a layout in which an observed element changed size. */
  fire(): void {
    if (!this.disconnected) this.callback();
  }
}

function bar(height: number) {
  return {
    height,
    getBoundingClientRect() {
      return { height: this.height } as DOMRect;
    },
  };
}

function watch() {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  const header = bar(76);
  const footer = bar(89.6);
  const reports: RangeInsets[] = [];
  const stop = watchChromeInsets(header as unknown as Element, footer as unknown as Element, (insets) =>
    reports.push(insets)
  );
  return { header, footer, reports, stop, observer: FakeResizeObserver.last! };
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeResizeObserver.last = null;
});

describe('watchChromeInsets', () => {
  it('reports the heights of both bars at once, in whole px rounded up', () => {
    const { reports, observer, header, footer } = watch();

    expect(reports).toEqual([{ top: 76, bottom: 90 }]);
    expect(observer.observed).toEqual([header, footer]);
  });

  it('reports again when a bar changes height, and only then', () => {
    const { reports, observer, header } = watch();

    observer.fire();
    header.height = 122.2;
    observer.fire();
    observer.fire();

    expect(reports).toEqual([
      { top: 76, bottom: 90 },
      { top: 123, bottom: 90 },
    ]);
  });

  it('stops watching when told', () => {
    const { reports, observer, footer, stop } = watch();

    stop();
    footer.height = 72;
    observer.fire();

    expect(observer.disconnected).toBe(true);
    expect(reports).toHaveLength(1);
  });
});
