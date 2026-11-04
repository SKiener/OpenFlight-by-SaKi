import { describe, expect, it } from 'vitest';
import { meanSem } from './clubDataStats';

describe('meanSem', () => {
  it('is the mean and SD/√n from 3 values, null below', () => {
    expect(meanSem([1, 2])).toBeNull();
    expect(meanSem([1, 2, null, undefined, Number.NaN])).toBeNull();
    const stat = meanSem([1, 2, 3, null])!;
    expect(stat).toEqual({ n: 3, mean: 2, sem: 1 / Math.sqrt(3) });
  });
});
