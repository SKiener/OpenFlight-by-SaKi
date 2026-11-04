import { describe, expect, it } from 'vitest';
import { cssFrameAspect, DEFAULT_FRAME_ASPECT, frameAspect } from './frameAspect';

describe('frameAspect', () => {
  it('defaults to the 640 x 400 sensor frame', () => {
    expect(DEFAULT_FRAME_ASPECT).toBe(1.6);
    expect(frameAspect()).toBe(1.6);
    expect(frameAspect(null, undefined)).toBe(1.6);
  });

  it('prefers the first valid size (the loaded picture before the settings)', () => {
    expect(frameAspect([640, 200], [640, 400])).toBe(3.2);
    expect(frameAspect([0, 0], [640, 200])).toBe(3.2);
    expect(frameAspect([undefined, 400], [640, 400])).toBe(1.6);
  });

  it('ignores sizes that are missing, zero, negative or not finite', () => {
    expect(frameAspect([0, 400])).toBe(1.6);
    expect(frameAspect([640, -1])).toBe(1.6);
    expect(frameAspect([Number.NaN, 400])).toBe(1.6);
    expect(frameAspect([640, Number.POSITIVE_INFINITY])).toBe(1.6);
  });

  it('formats the aspect as a short CSS number', () => {
    expect(cssFrameAspect(1.6)).toBe('1.6');
    expect(cssFrameAspect(1920 / 1080)).toBe('1.7778');
    expect(cssFrameAspect(3.2)).toBe('3.2');
  });
});
