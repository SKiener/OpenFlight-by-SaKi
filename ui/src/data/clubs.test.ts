import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { ALL_CLUBS, CLUBS_BY_TYPE, getClubName } from './clubs';

describe('clubs data', () => {
  it('flattens every grouped club into ALL_CLUBS', () => {
    const grouped = Object.values(CLUBS_BY_TYPE).flat();
    expect(ALL_CLUBS).toHaveLength(grouped.length);
    expect(ALL_CLUBS).toEqual(grouped);
  });

  it('includes the driver default with a DR tile and Driver name', () => {
    const driver = ALL_CLUBS.find((c) => c.id === 'driver');
    expect(driver).toBeDefined();
    expect(driver?.label).toBe('DR');
    expect(driver?.name).toBe('Driver');
  });

  it('has unique club ids', () => {
    const ids = ALL_CLUBS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('getClubName', () => {
  afterEach(() => setActiveLocale('en'));

  it('translates built-in German club names without changing identifiers', () => {
    setActiveLocale('de');
    expect(getClubName('7-iron')).toBe('Eisen 7');
    expect(getClubName('7-wood')).toBe('Holz 7');
    expect(getClubName('5-hybrid')).toBe('Hybrid 5');
    expect(getClubName('driver')).toBe('Driver');
    expect(getClubName('my-club')).toBe('my-club');
    expect(ALL_CLUBS.find((club) => club.id === '7-iron')?.name).toBe('7 Iron');
  });
  it('returns the prose name used in the header', () => {
    expect(getClubName('driver')).toBe('Driver');
    expect(getClubName('7-iron')).toBe('7 Iron');
  });

  it('falls back to the id when the club is unknown', () => {
    expect(getClubName('not-a-club')).toBe('not-a-club');
  });
});
