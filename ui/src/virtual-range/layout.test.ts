import { describe, expect, it } from 'vitest';
import { GREENS } from './layout';

describe('GREENS', () => {
  it('lists the eight greens of the spec in order', () => {
    expect(GREENS.map((g) => [g.distanceM, g.sideM, g.flag])).toEqual([
      [50, -9, '#ff4b3e'],
      [75, 8, '#ffcf33'],
      [100, -5, '#3da5ff'],
      [125, 9, '#f4f4f4'],
      [150, -8, '#ff8a2a'],
      [175, 5, '#b56cff'],
      [200, -3, '#2fe0c8'],
      [230, 10, '#ff5fa8'],
    ]);
  });

  it('cannot be changed by callers', () => {
    expect(() => Object.assign(GREENS, { 0: GREENS[1] })).toThrow(TypeError);
    expect(() => Object.assign(GREENS, { [GREENS.length]: GREENS[0] })).toThrow(TypeError);
    expect(() => Object.assign(GREENS[0], { distanceM: 1 })).toThrow(TypeError);
  });
});
