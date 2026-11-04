import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

function palette(path: string, theme: string) {
  const css = readFileSync(new URL(path, import.meta.url), 'utf8');
  return css.match(new RegExp(`\\[data-theme='${theme}'\\]\\s*\\{([^}]+)\\}`))?.[1] ?? '';
}

describe('kiosk palette', () => {
  it.each(['../index.css', '../components/range/range.css'])('uses black backgrounds and surfaces in %s', (path) => {
    const dark = palette(path, 'dark');
    expect(dark).toMatch(/--color-bg:\s*#000000\s*;/);
    expect(dark).toMatch(/--color-surface:\s*#000000\s*;/);
  });

  it('keeps the night look of the Range layout', () => {
    expect(palette('../components/range/range.css', 'dark')).toContain('--color-text: #fff;');
    expect(palette('../components/range/range.css', 'dark')).toContain('--color-accent: #00ff78;');
  });

  it('uses neon green, not RAL 6038, as the night accent in index.css', () => {
    const dark = palette('../index.css', 'dark');
    expect(dark).toMatch(/--color-accent:\s*#00ff78;/);
    expect(dark).toMatch(/--color-accent-block:\s*#00ff78;/);
  });

  it('has no RAL 6038 green (#00bb2d) left', () => {
    for (const path of [
      '../index.css',
      '../components/range/range.css',
      '../virtual-range/virtualRange.css',
      '../analysis/analysis.css',
      '../analysis/chartConfig.ts',
      '../virtual-range/scene/trainingTarget.ts',
    ]) {
      expect(readFileSync(new URL(path, import.meta.url), 'utf8').toLowerCase(), path).not.toContain('#00bb2d');
    }
  });

  it('has no light theme left anywhere', () => {
    for (const path of ['../index.css', '../components/range/range.css', '../companion/DispersionView.css']) {
      expect(readFileSync(new URL(path, import.meta.url), 'utf8'), path).not.toContain("data-theme='light'");
    }
  });

  it('defines the sun palette once, in index.css: black on white, neon green only as a block', () => {
    const sun = palette('../index.css', 'sun');
    expect(sun).toMatch(/--color-bg:\s*#ffffff;/);
    expect(sun).toMatch(/--color-surface:\s*#ffffff;/);
    expect(sun).toMatch(/--color-text:\s*#000000;/);
    expect(sun).toMatch(/--color-accent:\s*#000000;/);
    expect(sun).toMatch(/--color-accent-block:\s*#00ff78;/);
    expect(sun).toMatch(/--color-accent-fg:\s*#000000;/);
    // range.css must not override it (it loads after index.css).
    expect(palette('../components/range/range.css', 'sun')).toBe('');
  });
});
