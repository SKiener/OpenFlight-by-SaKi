import { describe, expect, it } from 'vitest';
import { isRangeDemo } from './rangeDemoSwitch';

describe('isRangeDemo', () => {
  it('is on only for ?rangeDemo=1', () => {
    expect(isRangeDemo('?rangeDemo=1')).toBe(true);
    expect(isRangeDemo('?view=x&rangeDemo=1')).toBe(true);
    expect(isRangeDemo('')).toBe(false);
    expect(isRangeDemo('?rangeDemo=0')).toBe(false);
    expect(isRangeDemo('?rangeDemo')).toBe(false);
    expect(isRangeDemo('?demo=1')).toBe(false);
  });
});
