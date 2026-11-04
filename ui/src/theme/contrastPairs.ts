import { contrast, themeTokens } from './contrast';

export interface ContrastPair {
  theme: string;
  pair: string;
  ratio: number;
  min: number;
}

function rootTokens(css: string): Record<string, string> {
  const block = css.match(/:root\s*\{([^}]+)\}/)?.[1] ?? '';
  return Object.fromEntries(
    [...block.matchAll(/(--color-[\w-]+):\s*(#[0-9a-fA-F]{3,6}|rgba?\([^)]*\))\s*;/g)].map((m) => [m[1], m[2]])
  );
}

/**
 * The main text/background pairs of both themes, as the app resolves them:
 * index.css, then range.css on top (the kiosk/phone app loads both; the
 * companion and pairing pages only index.css, listed as "dark (index.css)").
 * Values and units need 7:1 in sun, captions 4.5:1; 4.5:1 throughout in dark.
 */
export function contrastPairs(indexCss: string, rangeCss: string, virtualRangeCss: string): ContrastPair[] {
  const root = rootTokens(indexCss);
  const sun = { ...root, ...themeTokens(indexCss, 'sun') };
  const darkIndex = { ...root, ...themeTokens(indexCss, 'dark') };
  const dark = { ...darkIndex, ...themeTokens(rangeCss, 'dark') };
  const out: ContrastPair[] = [];
  const add = (theme: string, pair: string, fg: string, bg: string, min: number, base?: string) =>
    out.push({ theme, pair, ratio: contrast(fg, bg, base ?? bg), min });

  for (const [theme, t, body] of [
    ['sun', sun, 7],
    ['dark', dark, 4.5],
    ['dark (index.css)', darkIndex, 4.5],
  ] as const) {
    const bg = t['--color-surface'];
    add(theme, 'text on surface', t['--color-text'], bg, body);
    add(theme, 'text-muted on surface', t['--color-text-muted'], bg, body);
    add(theme, 'text-faint on surface (captions)', t['--color-text-faint'], bg, 4.5);
    add(theme, 'accent (text/lines) on surface', t['--color-accent'], bg, theme === 'sun' ? 7 : 4.5);
    add(
      theme,
      'accent-fg on accent-block',
      t['--color-accent-fg'],
      t['--color-accent-block'],
      theme === 'sun' ? 7 : 4.5
    );
    add(theme, 'danger on surface', t['--color-danger'], bg, 4.5);
    for (const status of ['success', 'error', 'info', 'warning']) {
      add(theme, `${status} on surface`, t[`--color-${status}`], bg, 4.5);
    }
    add(theme, 'black on warning-block', '#000000', t['--color-warning-block'], theme === 'sun' ? 7 : 4.5);
    add(theme, 'border on surface (non-text, 3:1)', t['--color-border'], bg, theme === 'sun' ? 3 : 1);
  }

  // The Range bars over the 3D scene: the scene's sky (#c8d9e2) and the worst case behind them.
  const block = (selector: string) => {
    const body = virtualRangeCss.match(new RegExp(`(?:^|\\n)${selector} \\{([^}]+)\\}`))?.[1] ?? '';
    return themeTokens(`[data-theme='x'] {${body}}`, 'x');
  };
  const vrDark = block('\\.virtual-range');
  const vrSun = block("\\[data-theme='sun'\\] \\.virtual-range");
  for (const [theme, t, worst] of [
    ['dark', vrDark, '#ffffff'],
    ['sun', vrSun, '#000000'],
  ] as const) {
    for (const scene of ['#c8d9e2', worst]) {
      add(
        theme,
        `range bar text over scene ${scene}`,
        t['--virtual-range-text'],
        t['--virtual-range-panel'],
        theme === 'sun' ? 7 : 4.5,
        scene
      );
      add(
        theme,
        `range bar label over scene ${scene}`,
        t['--virtual-range-text-muted'],
        t['--virtual-range-panel'],
        4.5,
        scene
      );
    }
  }
  return out;
}
