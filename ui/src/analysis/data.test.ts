import { describe, expect, it } from 'vitest';
import { carry, clubStatistics, confirmedSpin, emptyFilters, filterShots, flattenSessions, shotsCsv, statistics, statsCsv, summarize, type AnalysisShot } from './data';

const shot: AnalysisShot = { key: 'one:0', session_id: 'one', shot_number: 1, timestamp: '2026-10-03T14:00:00+02:00', club: '8-iron', profile_name: 'Golfer',
  ball_speed_mph: 100, club_speed_mph: null, estimated_carry_yards: 150, carry_spin_adjusted: 140, spin_rpm: 5000, spin_quality: 'experimental', spin_confidence: 0.59, launch_angle_vertical: 20 };

describe('saved shot analysis', () => {
  it('uses final carry, keeps zero and never invents a missing value', () => {
    expect(carry(shot)).toBe(140);
    expect(carry({ ...shot, carry_spin_adjusted: 0 })).toBe(0);
    expect(carry({ ...shot, carry_spin_adjusted: null })).toBe(150);
    expect(carry({ ...shot, carry_spin_adjusted: null, estimated_carry_yards: null })).toBeNull();
  });
  it('counts valid measurements independently, including zero', () => {
    expect(summarize([null, undefined, NaN, 0, 10])).toEqual({ n: 2, mean: 5, median: 5, sd: Math.sqrt(50) });
    expect(summarize([3]).sd).toBeNull();
    expect(summarize([]).mean).toBeNull();
    expect(statistics([shot]).carry.mean).toBe(140);
  });
  it('excludes experimental, low confidence and legacy ambiguous spin from measured statistics', () => {
    expect(confirmedSpin({ ...shot, spin_rpm_measured: 5000 })).toBeNull();
    expect(confirmedSpin({ ...shot, spin_quality: 'high', spin_rpm_measured: 6000 })).toBeNull();
    expect(confirmedSpin({ ...shot, spin_quality: 'high', spin_confidence: 0.9, spin_source: 'calculated', spin_rpm_measured: 1234 })).toBeNull();
    expect(confirmedSpin({ ...shot, spin_quality: 'high', spin_confidence: 0.9, spin_source: 'measured', spin_rpm: 6000 })).toBe(6000);
    expect(confirmedSpin({ ...shot, spin_quality: undefined, spin_confidence: undefined })).toBeNull();
    expect(statistics([shot]).spin.n).toBe(0);
  });
  it('leaves an uncertain ball speed out of the ball-speed and carry means but counts the shot', () => {
    // S2#3 kept as voted: the club's trailing track as the ball, so a short carry too.
    const uncertain = { ...shot, key: 'one:1', ball_speed_mph: 60.8, carry_spin_adjusted: 40, launch_angle_vertical: 30,
      ball_speed_status: 'uncertain' as const, ball_speed_status_reason: 'club_like_track' };
    const replaced = { ...shot, key: 'one:2', ball_speed_mph: 120, carry_spin_adjusted: 160, ball_speed_status: null, ball_speed_status_reason: 'club_like_track' };
    const stats = statistics([shot, uncertain, replaced]);
    expect(stats.count).toBe(3);
    expect(stats.speed).toMatchObject({ n: 2, mean: 110 });
    expect(stats.carry).toMatchObject({ n: 2, mean: 150 });
    expect(stats.launch.n).toBe(3);
    expect(statistics([uncertain])).toMatchObject({ count: 1, speed: { n: 0, mean: null }, carry: { n: 0, mean: null } });
    const driver = { ...uncertain, key: 'one:3', club: 'driver' };
    expect(clubStatistics([shot, uncertain, driver]).map((group) => [group.club, group.count, group.speed.mean, group.carry.mean]))
      .toEqual([['driver', 1, null, null], ['8-iron', 2, 100, 140]]);
  });
  it('filters by session, date, club and profile without changing originals', () => {
    const other = { ...shot, key: 'two:0', session_id: 'two', club: 'driver', profile_name: 'Other' };
    expect(filterShots([shot, other], { ...emptyFilters, club: '8-iron', profile: 'Golfer', from: '2026-10-03', to: '2026-10-03' })).toEqual([shot]);
    expect(filterShots([shot, other], { ...emptyFilters, session: 'two' })).toEqual([other]);
    expect(filterShots([shot], { ...emptyFilters, from: '2026-10-04' })).toEqual([]);
    expect(filterShots([shot], { ...emptyFilters, from: '2026-10-04', to: '2026-10-02' })).toEqual([]);
  });
  it('keeps repeated shot numbers distinct across sessions', () => {
    const session = { id: 'one', started_at: shot.timestamp, shot_count: 1, clubs: ['8-iron'], shots: [shot] };
    const flat = flattenSessions([session, { ...session, id: 'two' }]);
    expect(new Set(flat.map((entry) => entry.key)).size).toBe(2);
  });
  it('exports mixed units, source flags, empty missing fields and safe spreadsheet strings', () => {
    const value = shotsCsv([{ ...shot, profile_name: '=BAD("x")', launch_angle_horizontal: -3 }], 'mph-meters');
    expect(value).toContain('"ball_speed_mph"');
    expect(value).toContain('"carry_m"');
    expect(value).toContain('"128.016"');
    expect(value).toContain('"\'=BAD(""x"")"');
    expect(value).toContain('"-3"');
    expect(value).toContain('"experimental"');
    expect(value).toContain('"final_estimate"');
    expect(value).not.toContain('NaN');
    const [header, row] = shotsCsv([{ ...shot, carry_lateral_yards: -10 }, shot], 'mph-meters').split('\r\n');
    const lateral = header.split(',').indexOf('"carry_lateral_m"');
    expect(lateral).toBeGreaterThan(-1);
    expect(row.split(',')[lateral]).toBe('"-9.144"');
    expect(value.split('\r\n')[1].split(',')[lateral]).toBe('');
    expect(statsCsv([shot], 'mph-meters')).toContain('"confirmed_spin_n"');
    expect(statsCsv([], 'metric').split('\r\n')).toHaveLength(2);
  });
  it('exports the provenance of a calculated D-plane spin axis next to the axis', () => {
    const [header, calculated, bare] = shotsCsv([{ ...shot, spin_axis_deg: 4.7, spin_axis_source: 'calculated' }, shot], 'mph-meters').split('\r\n');
    const columns = header.split(',');
    const axis = columns.indexOf('"spin_axis_deg"');
    expect(columns[axis + 1]).toBe('"spin_axis_source"');
    expect(calculated.split(',')[axis]).toBe('"4.7"');
    expect(calculated.split(',')[axis + 1]).toBe('"calculated"');
    expect(bare.split(',')[axis + 1]).toBe('');
  });
  it('exports the curve class next to the spin-axis provenance', () => {
    const [header, calculated, bare] = shotsCsv(
      [{ ...shot, spin_axis_deg: 4.7, spin_axis_source: 'calculated', curve_class: 'fade' }, shot],
      'mph-meters'
    ).split('\r\n');
    const columns = header.split(',');
    const cls = columns.indexOf('"curve_class"');
    expect(columns[cls - 1]).toBe('"spin_axis_source"');
    expect(calculated.split(',')[cls]).toBe('"fade"');
    expect(bare.split(',')[cls]).toBe('');
  });
});
