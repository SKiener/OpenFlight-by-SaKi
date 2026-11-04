import { useLayoutEffect, useState, type RefObject } from 'react';
import { NO_INSETS, type RangeInsets } from '../virtual-range/insets';

/** Reports the heights of the bars lying over the top and the bottom edge, at once and whenever one changes. */
export function watchChromeInsets(top: Element, bottom: Element, onChange: (insets: RangeInsets) => void): () => void {
  let last: RangeInsets | null = null;
  const measure = (): void => {
    // Rounded up, so nothing placed below a bar of 75.6 px starts under its last row.
    const next = {
      top: Math.ceil(top.getBoundingClientRect().height),
      bottom: Math.ceil(bottom.getBoundingClientRect().height),
    };
    if (last && next.top === last.top && next.bottom === last.bottom) return;
    last = next;
    onChange(next);
  };
  const observer = new ResizeObserver(measure);
  observer.observe(top);
  observer.observe(bottom);
  measure();
  return () => observer.disconnect();
}

/** While `active`, the px the bars `top` and `bottom` cover; none otherwise. */
export function useChromeInsets(
  top: RefObject<HTMLElement | null>,
  bottom: RefObject<HTMLElement | null>,
  active: boolean
): RangeInsets {
  const [insets, setInsets] = useState(NO_INSETS);
  // A layout effect measures before the first paint, so nothing shows under a bar for a frame.
  useLayoutEffect(() => {
    if (!active || !top.current || !bottom.current) return;
    return watchChromeInsets(top.current, bottom.current, setInsets);
  }, [active, top, bottom]);
  return active ? insets : NO_INSETS;
}
