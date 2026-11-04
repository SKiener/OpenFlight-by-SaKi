import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { contrast, parseColor } from './contrast';
import { contrastPairs } from './contrastPairs';

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

describe('contrast helpers', () => {
  it('parses hex and rgba', () => {
    expect(parseColor('#fff')).toEqual([255, 255, 255, 1]);
    expect(parseColor('rgba(0, 0, 0, 0.5)')).toEqual([0, 0, 0, 0.5]);
  });

  it('matches the WCAG reference values', () => {
    expect(contrast('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrast('#777777', '#ffffff')).toBeCloseTo(4.48, 2);
  });
});

describe('theme contrast', () => {
  const pairs = contrastPairs(
    read('../index.css'),
    read('../components/range/range.css'),
    read('../virtual-range/virtualRange.css')
  );

  it('checks both themes and the Range bars', () => {
    expect(pairs.filter((p) => p.theme === 'sun').length).toBeGreaterThan(10);
    expect(pairs.filter((p) => p.theme === 'dark').length).toBeGreaterThan(10);
    expect(pairs.some((p) => p.pair.startsWith('range bar'))).toBe(true);
  });

  it.each(pairs.map((p) => [`${p.theme}: ${p.pair}`, p] as const))('%s', (_, pair) => {
    expect(pair.ratio, `${pair.ratio.toFixed(2)} < ${pair.min}`).toBeGreaterThanOrEqual(pair.min);
  });
});
