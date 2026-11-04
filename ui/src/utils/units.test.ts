import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import {
  convertDistanceFromYards,
  convertSpeedFromMph,
  formatCarryRange,
  formatDistance,
  formatNumber,
  formatSpeed,
  getDistanceUnit,
  getSpeedUnit,
  getUnitsLabel,
  isUnitSystem,
  withSpeedUnit,
  withDistanceUnit,
} from './units';

describe('units helpers', () => {
  it('combines mph with meters without changing speed', () => {
    expect(formatSpeed(100, 'mph-meters')).toBe('100.0');
    expect(formatDistance(100, 'mph-meters')).toBe('91');
    expect(formatCarryRange([100, 200], 'mph-meters')).toBe('91-183 m');
    expect(getUnitsLabel('mph-meters')).toBe('mph / m');
  });
  it('also permits km/h with yards', () => {
    expect(formatSpeed(100, 'kmh-yards')).toBe('160.9');
    expect(formatDistance(100, 'kmh-yards')).toBe('100');
    expect(getUnitsLabel('kmh-yards')).toBe('km/h / yds');
  });
  it('changes each selector independently and accepts old saved settings', () => {
    for (const system of ['imperial', 'metric', 'mph-meters', 'kmh-yards'] as const) {
      expect(isUnitSystem(system)).toBe(true);
      expect(getDistanceUnit(withSpeedUnit(system, 'mph'))).toBe(getDistanceUnit(system));
      expect(getDistanceUnit(withSpeedUnit(system, 'km/h'))).toBe(getDistanceUnit(system));
      expect(getSpeedUnit(withDistanceUnit(system, 'm'))).toBe(getSpeedUnit(system));
      expect(getSpeedUnit(withDistanceUnit(system, 'yds'))).toBe(getSpeedUnit(system));
    }
    expect(withSpeedUnit('metric', 'mph')).toBe('mph-meters');
    expect(isUnitSystem(null)).toBe(false);
    expect(isUnitSystem('mph')).toBe(false);
  });
  afterEach(() => setActiveLocale('en'));

  it('uses German decimal commas without changing units or stored values', () => {
    setActiveLocale('de');
    expect(formatSpeed(100, 'metric', 1)).toBe('160,9');
    expect(formatSpeed(100, 'imperial', 1)).toBe('100,0');
    expect(formatDistance(100, 'metric', 1)).toBe('91,4');
    expect(formatNumber(1.45, 2)).toBe('1,45');
    expect(convertSpeedFromMph(100, 'metric')).toBeCloseTo(160.934, 3);
  });
  it('preserves imperial speed values', () => {
    expect(convertSpeedFromMph(150, 'imperial')).toBe(150);
    expect(formatSpeed(150, 'imperial', 1)).toBe('150.0');
    expect(getSpeedUnit('imperial')).toBe('mph');
  });

  it('converts mph to km/h with stable rounding', () => {
    expect(convertSpeedFromMph(100, 'metric')).toBeCloseTo(160.934, 3);
    expect(formatSpeed(100, 'metric', 1)).toBe('160.9');
    expect(getSpeedUnit('metric')).toBe('km/h');
  });

  it('preserves imperial distance values', () => {
    expect(convertDistanceFromYards(250, 'imperial')).toBe(250);
    expect(formatDistance(250, 'imperial', 0)).toBe('250');
    expect(getDistanceUnit('imperial')).toBe('yds');
  });

  it('converts yards to meters with stable rounding', () => {
    expect(convertDistanceFromYards(100, 'metric')).toBeCloseTo(91.44, 2);
    expect(formatDistance(100, 'metric', 0)).toBe('91');
    expect(getDistanceUnit('metric')).toBe('m');
  });

  it('joins speed and distance units for chrome labels', () => {
    expect(getUnitsLabel('imperial')).toBe('mph / yds');
    expect(getUnitsLabel('metric')).toBe('km/h / m');
  });

  it('formats carry ranges in the selected unit system', () => {
    expect(formatCarryRange([200, 220], 'imperial')).toBe('200-220 yds');
    expect(formatCarryRange([200, 220], 'metric')).toBe('183-201 m');
  });
});
