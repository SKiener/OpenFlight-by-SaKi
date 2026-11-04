import { describe, expect, it } from 'vitest';
import type { Shot } from './shot';
import { makeShot } from '../test-utils/shot';
import { computeStats, filterShotsByProfile, excludeShotsByProfile, getDisplayedSpin, latestDeletableShot } from './shot';

describe('computeStats', () => {
  it('averages the carry the live tile shows: spin-adjusted, else the estimate', () => {
    const shots = [
      {
        ball_speed_mph: 105,
        club_speed_mph: 80,
        smash_factor: 1.31,
        estimated_carry_yards: 140,
        carry_spin_adjusted: 136,
      },
      {
        ball_speed_mph: 108,
        club_speed_mph: 81,
        smash_factor: 1.33,
        estimated_carry_yards: 150,
        carry_spin_adjusted: null,
      },
    ] as Shot[];

    expect(computeStats(shots).avg_carry_est).toBe(143);
  });

  it('counts an uncertain ball speed as a shot but leaves it out of the means and extremes', () => {
    const good = [
      makeShot({ ball_speed_mph: 140, club_speed_mph: 100, smash_factor: 1.4 }),
      makeShot({ ball_speed_mph: 144, club_speed_mph: 102, smash_factor: 1.41 }),
    ];
    const uncertain = makeShot({ ball_speed_mph: 43.5, club_speed_mph: 103, smash_factor: 0.42, ball_speed_status: 'uncertain' });

    const stats = computeStats([good[0], uncertain, good[1]]);

    expect(stats).toEqual({ ...computeStats(good), shot_count: 3 });
    expect(stats.min_ball_speed).toBe(140);
  });

  it('keeps a replaced ball speed in the means', () => {
    const replaced = makeShot({ ball_speed_mph: 148.3, ball_speed_status: null, ball_speed_status_reason: 'club_like_track' });

    expect(computeStats([replaced]).avg_ball_speed).toBe(148.3);
  });

  it('reads like an empty session with only uncertain shots, but counts them', () => {
    const stats = computeStats([makeShot({ ball_speed_status: 'uncertain' })]);

    expect(stats).toMatchObject({ shot_count: 1, avg_ball_speed: 0, max_ball_speed: 0, avg_club_speed: null });
  });
});

describe('getDisplayedSpin', () => {
  // The range's 8-iron: OPS read 1,538 rpm at 39 %, the carry flew 7,500.
  const eightIron = {
    spin_rpm: 1538,
    spin_confidence: 0.39,
    spin_quality: 'low',
    spin_source: null,
    carry_spin_rpm: 7500,
    carry_spin_source: 'club_typical',
  } as Shot;

  it('shows the measured spin when the carry used it', () => {
    expect(getDisplayedSpin({ spin_rpm: 2650, carry_spin_rpm: 2650, carry_spin_source: 'measured' })).toEqual({
      rpm: 2650,
      source: 'measured',
    });
  });

  it('shows the spin the carry used, not the raw radar spin, when the measurement was not trusted', () => {
    expect(getDisplayedSpin(eightIron)).toEqual({ rpm: 7500, source: 'typical' });
  });

  it.each(['club_typical', 'speed_model', null, undefined, ''])(
    'calls the carry spin typical for source %s',
    (carry_spin_source) => {
      expect(getDisplayedSpin({ spin_rpm: 1538, carry_spin_rpm: 4800, carry_spin_source })).toEqual({
        rpm: 4800,
        source: 'typical',
      });
    }
  );

  it('keeps the calculated spin the carry used apart from a typical one', () => {
    expect(getDisplayedSpin({ spin_rpm: 6234, carry_spin_rpm: 6234, carry_spin_source: 'calculated' })).toEqual({
      rpm: 6234,
      source: 'calculated',
    });
  });

  it('trusts the carry source, not a high radar confidence', () => {
    const shot = { ...eightIron, spin_rpm: 9100, spin_confidence: 0.9, spin_quality: 'high' } as Shot;

    expect(getDisplayedSpin(shot)).toEqual({ rpm: 7500, source: 'typical' });
  });

  it('never falls back to the raw spin without a carry spin (older saved shots, mock shots)', () => {
    expect(getDisplayedSpin({ spin_rpm: 6000 })).toBeNull();
    expect(getDisplayedSpin({ spin_rpm: 6000, carry_spin_rpm: null, carry_spin_source: null })).toBeNull();
    expect(getDisplayedSpin({ spin_rpm: 6000, carry_spin_rpm: null, carry_spin_source: 'club_typical' })).toBeNull();
  });

  it('has no spin for a swing-speed rep', () => {
    const rep = { mode: 'swing-speed', club: 'Swing Speed', spin_rpm: null } as Shot;

    expect(getDisplayedSpin(rep)).toBeNull();
    expect(getDisplayedSpin({ ...rep, spin_rpm: 2400 })).toBeNull();
  });

  it('shows the measured carry spin when the measured value itself is missing', () => {
    expect(getDisplayedSpin({ spin_rpm: null, carry_spin_rpm: 2650, carry_spin_source: 'measured' })).toEqual({
      rpm: 2650,
      source: 'measured',
    });
    expect(getDisplayedSpin({ spin_rpm: null, carry_spin_rpm: null, carry_spin_source: 'measured' })).toBeNull();
  });

  it('treats non-finite numbers as missing', () => {
    expect(
      getDisplayedSpin({ spin_rpm: Number.NaN, carry_spin_rpm: Number.NaN, carry_spin_source: 'measured' })
    ).toBeNull();
    expect(getDisplayedSpin({ spin_rpm: Number.NaN, carry_spin_rpm: 2650, carry_spin_source: 'measured' })).toEqual({
      rpm: 2650,
      source: 'measured',
    });
    expect(
      getDisplayedSpin({ spin_rpm: 1538, carry_spin_rpm: Number.POSITIVE_INFINITY, carry_spin_source: 'club_typical' })
    ).toBeNull();
  });
});

describe('filterShotsByProfile', () => {
  const shotWith = (profileId: string | undefined): Shot => ({ profile_id: profileId, ball_speed_mph: 100 }) as Shot;

  it('keeps only shots stamped with the given profile id', () => {
    const shots = [shotWith('aaa'), shotWith('bbb'), shotWith('aaa')];

    expect(filterShotsByProfile(shots, 'aaa')).toHaveLength(2);
  });

  it('matches exactly, without folding case', () => {
    const shots = [shotWith('AAA'), shotWith('aaa')];

    expect(filterShotsByProfile(shots, 'aaa')).toEqual([shots[1]]);
  });

  it('excludes unstamped shots from every profile', () => {
    const shots = [shotWith(undefined), shotWith('')];

    expect(filterShotsByProfile(shots, 'aaa')).toEqual([]);
    expect(filterShotsByProfile(shots, 'bbb')).toEqual([]);
  });

  it('returns nothing for a blank profile id', () => {
    const shots = [shotWith('aaa'), shotWith(undefined)];

    expect(filterShotsByProfile(shots, '')).toEqual([]);
  });
});

describe('excludeShotsByProfile', () => {
  const shotWith = (profileId: string | undefined): Shot => ({ profile_id: profileId, ball_speed_mph: 100 }) as Shot;

  it('drops only the given profile and keeps unstamped shots', () => {
    const shots = [shotWith('aaa'), shotWith('bbb'), shotWith(undefined)];

    expect(excludeShotsByProfile(shots, 'aaa')).toEqual([shots[1], shots[2]]);
  });

  it('excludes nothing for a blank profile id', () => {
    const shots = [shotWith('aaa'), shotWith('bbb')];

    expect(excludeShotsByProfile(shots, '')).toEqual(shots);
  });
});

describe('latestDeletableShot', () => {
  it('picks the newest final shot of the profile', () => {
    const anna1 = makeShot({ timestamp: 'a1', profile_id: 'anna' });
    const anna2 = makeShot({ timestamp: 'a2', profile_id: 'anna' });
    const ben = makeShot({ timestamp: 'b1', profile_id: 'ben' });
    const provisional = makeShot({ timestamp: 'a3', profile_id: 'anna', carry_spin_adjusted: null });

    expect(latestDeletableShot([anna1, anna2, ben], 'anna')).toBe(anna2);
    expect(latestDeletableShot([anna1, ben], 'ben')).toBe(ben);
    expect(latestDeletableShot([anna1, anna2, provisional], 'anna'), 'the newest is still processed').toBeNull();
    expect(latestDeletableShot([anna1, provisional, ben], 'ben')).toBe(ben);
    expect(latestDeletableShot([ben], 'anna')).toBeNull();
    expect(latestDeletableShot([], 'anna')).toBeNull();
    expect(latestDeletableShot([anna1], '')).toBeNull();
  });
});
