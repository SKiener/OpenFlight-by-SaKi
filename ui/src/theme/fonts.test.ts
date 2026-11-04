import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import {
  BODY_FONT_PROBES,
  BODY_FONT_STACK,
  DISPLAY_FONT_PROBES,
  DISPLAY_FONT_STACK,
  bodyFontPending,
  displayFontPending,
  fontPending,
} from './fonts';

function fontSet(loaded: boolean | ((probe: string) => boolean), load: (probe: string) => Promise<FontFace[]> = () => Promise.resolve([])) {
  const isLoaded = typeof loaded === 'function' ? loaded : () => loaded;
  return { check: vi.fn(isLoaded), load: vi.fn(load) };
}

describe('Live numbers in a•space', () => {
  const tracking = (path: string, selector: string) => {
    const css = readFileSync(new URL(path, import.meta.url), 'utf8');
    const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const value = css.match(new RegExp(`${escaped} \\{[^}]*letter-spacing: (-?[\\d.]+)em`))?.[1];
    expect(value, selector).toBeDefined();
    return Number(value);
  };

  // a•space's "1" has almost no left side bearing: at -0.03em a digit before it touches it (synthetic bold).
  it.each([
    ['../components/range/range.css', '.kiosk-tile__value'],
    ['../components/range/range.css', '.range-tile .metric-card__value'],
    ['../components/panel/panel.css', '.live-panel__grid .metric-card__value'],
  ])('keeps digits apart in %s %s', (path, selector) => {
    expect(tracking(path, selector)).toBeGreaterThanOrEqual(-0.02);
  });
});

describe('font stacks for canvases', () => {
  const css = readFileSync(new URL('../index.css', import.meta.url), 'utf8');

  it('mirrors --font-body (Nexa, then a•space for ≈) in index.css', () => {
    expect(css).toContain(`--font-body: ${BODY_FONT_STACK};`);
    expect(BODY_FONT_STACK.startsWith("'Nexa', 'A-Space',")).toBe(true);
  });

  it('mirrors --font-display (a•space) in index.css', () => {
    expect(css).toContain(`--font-display: ${DISPLAY_FONT_STACK};`);
    expect(DISPLAY_FONT_STACK.startsWith("'A-Space',")).toBe(true);
    expect(DISPLAY_FONT_STACK).not.toContain('Nexa');
  });

  it('asks about the faces themselves, not the fallback', () => {
    // Chart.js ticks are normal weight (Light), its tooltip titles bold (Bold).
    expect(BODY_FONT_PROBES).toEqual(["400 1em 'Nexa'", "700 1em 'Nexa'"]);
    expect(DISPLAY_FONT_PROBES).toEqual(["700 1em 'A-Space'"]);
  });
});

describe('headings in a•space', () => {
  const css = readFileSync(new URL('../index.css', import.meta.url), 'utf8');
  const displayRule = css.match(/([^{}]+)\{\s*font-family: var\(--font-display\);\s*\}/)?.[1] ?? '';
  const selectors = displayRule
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(',')
    .map((selector) => selector.trim());

  it.each([
    // The analysis page: brand and summary numbers beside its a•space titles.
    '.analysis-header strong',
    '.analysis-kpis strong',
    // The companion page's shot heading, like the kiosk and phone shot headings.
    '.companion__shot-heading strong',
    // Dialog titles: bag settings, power, power warning, pairing screen and dialog, camera alignment.
    '#bag-title',
    '#range-power-title',
    '#power-warning-title',
    '#pairing-title',
    '#pair-dialog-title',
    '.camera-feed__title',
    // The replay's error heading and the empty Live and Range page.
    '.camera-replay__status h2',
    '.range-empty h2',
  ])('sets %s in the display face', (selector) => {
    expect(selectors).toContain(selector);
  });
});

describe('fontPending', () => {
  it('is not pending once every face is loaded', () => {
    const fonts = fontSet(true);
    expect(fontPending(BODY_FONT_PROBES, fonts)).toBeNull();
    expect(fonts.check).toHaveBeenCalledWith(BODY_FONT_PROBES[0]);
    expect(fonts.check).toHaveBeenCalledWith(BODY_FONT_PROBES[1]);
    expect(fonts.load).not.toHaveBeenCalled();
  });

  it('loads only the faces that are not ready and settles when they are', async () => {
    let release: () => void = () => {};
    const fonts = fontSet(
      (probe) => probe === BODY_FONT_PROBES[0],
      () => new Promise((resolve) => (release = () => resolve([]))),
    );
    const pending = fontPending(BODY_FONT_PROBES, fonts);
    expect(pending).not.toBeNull();
    expect(fonts.load).toHaveBeenCalledTimes(1);
    expect(fonts.load).toHaveBeenCalledWith(BODY_FONT_PROBES[1]);
    let settled = false;
    void pending!.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);
    release();
    await expect(pending).resolves.toBeUndefined();
  });

  it('waits for every face even if one of them fails to load', async () => {
    let releaseBold: () => void = () => {};
    const fonts = fontSet(false, (probe) =>
      probe === BODY_FONT_PROBES[0]
        ? Promise.reject(new Error('offline'))
        : new Promise((resolve) => (releaseBold = () => resolve([]))),
    );
    const pending = fontPending(BODY_FONT_PROBES, fonts);
    let settled = false;
    void pending!.then(() => (settled = true));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(settled).toBe(false);
    releaseBold();
    await expect(pending).resolves.toBeUndefined();
  });

  it('settles even if loading fails, so a caller still draws with the fallback', async () => {
    const fonts = fontSet(false, () => Promise.reject(new Error('offline')));
    await expect(fontPending(DISPLAY_FONT_PROBES, fonts)).resolves.toBeUndefined();
  });

  it('is not pending without a font loading API', () => {
    expect(fontPending(BODY_FONT_PROBES, undefined)).toBeNull();
    const broken = {
      check: () => {
        throw new Error('SyntaxError');
      },
      load: vi.fn(),
    };
    expect(fontPending(BODY_FONT_PROBES, broken)).toBeNull();
  });
});

describe('each canvas waits for its own face', () => {
  it('bodyFontPending waits for Nexa (Chart.js)', () => {
    const fonts = fontSet(false);
    expect(bodyFontPending(fonts)).not.toBeNull();
    expect(fonts.load.mock.calls.map(([probe]) => probe)).toEqual([...BODY_FONT_PROBES]);
  });

  it('displayFontPending waits for a•space (range signs)', () => {
    const fonts = fontSet(false);
    expect(displayFontPending(fonts)).not.toBeNull();
    expect(fonts.load.mock.calls.map(([probe]) => probe)).toEqual([...DISPLAY_FONT_PROBES]);
  });

  it('neither waits for the other face', () => {
    const nexaOnly = fontSet((probe) => probe.includes("'Nexa'"));
    expect(bodyFontPending(nexaOnly)).toBeNull();
    expect(displayFontPending(nexaOnly)).not.toBeNull();
    const aspaceOnly = fontSet((probe) => probe.includes("'A-Space'"));
    expect(displayFontPending(aspaceOnly)).toBeNull();
    expect(bodyFontPending(aspaceOnly)).not.toBeNull();
  });
});
