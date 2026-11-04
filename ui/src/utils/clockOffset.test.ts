import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { clientClockPayload, formatClockOffset, isClockOffsetShown } from './clockOffset';

// The wall-clock gap of the 2026-10-25 range session (the UTC epoch offset was 1 h more, DST ended in between).
const RANGE_OFFSET_S = (5 * 24 + 17) * 3600 + 29 * 60;

afterEach(() => setActiveLocale('en'));

describe('clock offset', () => {
  it('formats days, hours and minutes with the locale day unit', () => {
    expect(formatClockOffset(RANGE_OFFSET_S)).toBe('+5 d 17 h 29 min');
    setActiveLocale('de');
    expect(formatClockOffset(RANGE_OFFSET_S)).toBe('+5 T 17 h 29 min');
    expect(formatClockOffset(-RANGE_OFFSET_S)).toBe('-5 T 17 h 29 min');
  });

  it('drops leading zero units and rounds half minutes up like the server', () => {
    expect(formatClockOffset(3 * 3600 + 5 * 60)).toBe('+3 h 5 min');
    expect(formatClockOffset(-150)).toBe('-3 min');
    expect(formatClockOffset(24 * 3600)).toBe('+1 d 0 h 0 min');
  });

  it('is shown only beyond two minutes', () => {
    expect(isClockOffsetShown(null)).toBe(false);
    expect(isClockOffsetShown(undefined)).toBe(false);
    expect(isClockOffsetShown(Number.NaN)).toBe(false);
    expect(isClockOffsetShown(120)).toBe(false);
    expect(isClockOffsetShown(-119.9)).toBe(false);
    expect(isClockOffsetShown(121)).toBe(true);
    expect(isClockOffsetShown(-RANGE_OFFSET_S)).toBe(true);
  });

  it('sends epoch milliseconds and local-minus-UTC minutes', () => {
    const now = new Date(1_761_384_000_000);
    expect(clientClockPayload(now)).toEqual({
      epoch_ms: 1_761_384_000_000,
      tz_offset_min: -now.getTimezoneOffset(),
    });
  });
});
