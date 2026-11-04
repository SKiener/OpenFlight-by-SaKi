import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import {
  DEFAULT_DRIVER_TEE_HEIGHT_M,
  MAX_DRIVER_TEE_HEIGHT_M,
  driverTeeHeightM,
  formatDriverTee,
  stepDriverTee,
} from './driverTee';

describe('driverTeeHeightM', () => {
  it('defaults to 4 cm when unset', () => {
    expect(DEFAULT_DRIVER_TEE_HEIGHT_M).toBe(0.04);
    expect(driverTeeHeightM(undefined)).toBe(0.04);
    expect(driverTeeHeightM({})).toBe(0.04);
  });

  it('reads a stored value', () => {
    expect(driverTeeHeightM({ driver_tee_height_m: 0.055 })).toBe(0.055);
    expect(driverTeeHeightM({ driver_tee_height_m: 0 })).toBe(0);
  });

  it.each([[-0.01], [0.09], [Number.NaN], ['0.05'], [null], [true]])('falls back to the default for %s', (value) => {
    expect(driverTeeHeightM({ driver_tee_height_m: value })).toBe(0.04);
  });
});

describe('stepDriverTee', () => {
  it('steps by 0.5 cm', () => {
    expect(stepDriverTee(0.04, 1)).toBe(0.045);
    expect(stepDriverTee(0.04, -1)).toBe(0.035);
  });

  it('clamps at 0 and 8 cm', () => {
    expect(stepDriverTee(0, -1)).toBe(0);
    expect(stepDriverTee(0.005, -1)).toBe(0);
    expect(stepDriverTee(0.075, 1)).toBe(MAX_DRIVER_TEE_HEIGHT_M);
    expect(stepDriverTee(0.08, 1)).toBe(0.08);
  });

  it('snaps an off-grid value onto the 0.5 cm grid', () => {
    expect(stepDriverTee(0.0412, 1)).toBe(0.045);
  });

  it('walks the whole range without float drift', () => {
    let value = 0;
    for (let index = 0; index < 20; index += 1) value = stepDriverTee(value, 1);
    expect(value).toBe(0.08);
    for (let index = 0; index < 20; index += 1) value = stepDriverTee(value, -1);
    expect(value).toBe(0);
  });
});

describe('formatDriverTee', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows cm with the locale decimal', () => {
    setActiveLocale('de');
    expect(formatDriverTee(0.04, 'metric')).toBe('4,0 cm');
    expect(formatDriverTee(0.045, 'mph-meters')).toBe('4,5 cm');
    setActiveLocale('en');
    expect(formatDriverTee(0.04, 'metric')).toBe('4.0 cm');
  });

  it('shows inches with yards, like the low-point card', () => {
    expect(formatDriverTee(0.0508, 'imperial')).toBe('2.0 in');
    expect(formatDriverTee(0.04, 'kmh-yards')).toBe('1.6 in');
  });
});
