import { describe, expect, it } from 'vitest';
import type { SavedSession, SavedShot } from '../analysis/data';
import { ALL_CLUBS } from '../data/clubs';
import type { Shot } from '../types/shot';
import {
  classifyShot,
  CLUB_LENGTH_ORDER,
  clubColorSlot,
  collectShots,
  compareClubsByLength,
  covarianceEllipse,
  derivedLateralYards,
  directionDoubt,
  ellipseCoverage,
  ellipseFromCovariance,
  equalAspectViewport,
  niceStep,
  plotBounds,
  sampleCovariance,
  scaleEllipse,
  summarizeClubs,
  tickValues,
  type DispersionPoint,
} from './dispersion';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const daysAgo = (days: number) => new Date(NOW - days * 86_400_000).toISOString();

const shot = (overrides: Partial<Shot> = {}): Shot => ({
  ball_speed_mph: 120,
  club_speed_mph: 88,
  smash_factor: 1.36,
  estimated_carry_yards: 160,
  carry_range: [150, 170],
  club: '7-iron',
  timestamp: daysAgo(0),
  peak_magnitude: null,
  launch_angle_vertical: 18,
  launch_angle_horizontal: 2,
  launch_angle_confidence: 0.8,
  launch_angle_horizontal_confidence: 0.8,
  launch_angle_horizontal_source: 'radar',
  angle_source: 'radar',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 6500,
  spin_confidence: 0.9,
  spin_quality: 'high',
  spin_source: 'measured',
  carry_spin_adjusted: 158,
  carry_spin_source: 'measured',
  carry_lateral_yards: 5.5,
  profile_id: 'p1',
  ...overrides,
});

const saved = (overrides: Partial<SavedShot> = {}): SavedShot => ({
  shot_number: 1,
  timestamp: daysAgo(3),
  club: 'driver',
  profile_id: 'p1',
  profile_name: 'Golfer',
  ball_speed_mph: 150,
  club_speed_mph: 104,
  estimated_carry_yards: 240,
  carry_spin_adjusted: 236,
  carry_spin_source: 'club_typical',
  launch_angle_vertical: 12,
  launch_angle_horizontal: -1.5,
  launch_angle_horizontal_source: 'radar',
  spin_rpm: null,
  spin_source: '',
  ...overrides,
});

const point = (classified: ReturnType<typeof classifyShot>) => classified as DispersionPoint;

describe('dispersion statistics', () => {
  it('uses the sample (n − 1) covariance and refuses undersized or mismatched samples', () => {
    expect(sampleCovariance([1, 2, 3, 4], [2, 4, 6, 8])).toBeCloseTo(10 / 3, 10);
    expect(sampleCovariance([1, 2, 3, 4], [1, 2, 3, 4])).toBeCloseTo(5 / 3, 10);
    expect(sampleCovariance([5], [5])).toBeNull();
    expect(sampleCovariance([1, 2], [1])).toBeNull();
  });

  it('turns a covariance matrix into 2σ ellipse axes and the major-axis angle', () => {
    const aligned = ellipseFromCovariance(4, 1, 0, 2);
    expect(aligned.semiMajor).toBeCloseTo(4);
    expect(aligned.semiMinor).toBeCloseTo(2);
    expect(aligned.angleRad).toBeCloseTo(0);

    const diagonal = ellipseFromCovariance(2, 2, 1, 2);
    expect(diagonal.semiMajor).toBeCloseTo(2 * Math.sqrt(3));
    expect(diagonal.semiMinor).toBeCloseTo(2);
    expect(diagonal.angleRad).toBeCloseTo(Math.PI / 4);

    const tallAndNarrow = ellipseFromCovariance(0, 9, 0, 2);
    expect(tallAndNarrow.semiMajor).toBeCloseTo(6);
    expect(tallAndNarrow.semiMinor).toBe(0);
    expect(tallAndNarrow.angleRad).toBeCloseTo(Math.PI / 2);
  });

  it('centres the ellipse on the mean of the shots', () => {
    const ellipse = covarianceEllipse([
      { x: -2, y: 140 },
      { x: 2, y: 150 },
      { x: 4, y: 160 },
    ])!;
    expect(ellipse.cx).toBeCloseTo(4 / 3);
    expect(ellipse.cy).toBeCloseTo(150);
    expect(ellipse.angleRad).toBeGreaterThan(Math.PI / 4);
    expect(covarianceEllipse([{ x: 0, y: 100 }])).toBeNull();
  });

  it('states the share of a bivariate normal inside the 2σ ellipse', () => {
    expect(ellipseCoverage(2)).toBeCloseTo(0.8647, 4);
    expect(Math.round(ellipseCoverage(2) * 100)).toBe(86);
  });
});

describe('lateral landing position', () => {
  it('derives a missing offset as carry × sin(HLA): right positive, left negative', () => {
    expect(derivedLateralYards(150, 0)).toBe(0);
    expect(derivedLateralYards(150, 30)).toBeCloseTo(75);
    expect(derivedLateralYards(150, -30)).toBeCloseTo(-75);
  });

  it('prefers the server offset and labels the fallback as derived', () => {
    const measured = point(classifyShot(shot()));
    expect(measured).toMatchObject({ lateralYards: 5.5, lateralDerived: false, carryYards: 158, club: '7-iron' });

    for (const missing of [undefined, null]) {
      const derived = point(classifyShot(shot({ carry_lateral_yards: missing, launch_angle_horizontal: -2 })));
      expect(derived.lateralDerived).toBe(true);
      expect(derived.lateralYards).toBeCloseTo(158 * Math.sin((-2 * Math.PI) / 180));
    }
  });

  it('excludes shots without a direction or a carry instead of inventing one', () => {
    expect(classifyShot(shot({ launch_angle_horizontal: null }))).toEqual({
      key: shot().timestamp,
      club: '7-iron',
      reason: 'no_direction',
    });
    expect(classifyShot(shot({ launch_angle_horizontal: Number.NaN }))).toMatchObject({ reason: 'no_direction' });
    expect(classifyShot(shot({ carry_spin_adjusted: null, estimated_carry_yards: Number.NaN }))).toMatchObject({
      reason: 'no_carry',
    });
    expect(point(classifyShot(shot({ carry_spin_adjusted: null }))).carryYards).toBe(160);
  });
});

describe('honest labelling', () => {
  it('doubts estimated, experimental, unknown and low-confidence directions', () => {
    const doubt = (source: string | null | undefined, confidence: number | null = 0.8) =>
      directionDoubt({ launch_angle_horizontal_source: source, launch_angle_horizontal_confidence: confidence });

    expect(doubt('radar')).toBeNull();
    expect(doubt('radar', null)).toBeNull();
    expect(doubt('radar', 0.39)).toBe('low_confidence');
    expect(doubt('radar', 0.4)).toBeNull();
    expect(doubt('estimated')).toBe('estimated');
    expect(doubt('mock')).toBe('estimated');
    expect(doubt('camera_assisted_experimental', 0.75)).toBe('experimental');
    expect(doubt('camera_only_experimental', 0.3)).toBe('experimental');
    expect(doubt(undefined)).toBe('unknown_source');
    expect(doubt(null)).toBe('unknown_source');
  });

  it('reads a session-calibrated TI direction like the TI itself', () => {
    const doubt = (confidence: number | null) =>
      directionDoubt({
        launch_angle_horizontal_source: 'radar_calibrated',
        launch_angle_horizontal_confidence: confidence,
      });

    expect(doubt(0.8)).toBeNull();
    expect(doubt(null)).toBeNull();
    expect(doubt(0.39)).toBe('low_confidence');
  });

  it('flags carry that did not use measured spin', () => {
    expect(point(classifyShot(shot())).substituteSpin).toBe(false);
    for (const source of ['club_typical', 'speed_model', 'calculated', null, undefined] as const) {
      expect(point(classifyShot(shot({ carry_spin_source: source }))).substituteSpin).toBe(true);
    }
  });
});

describe('club order and colours', () => {
  const bag = ['driver', '7-iron', 'wedge-58', '3-wood', 'mini-driver', '4-iron', 'wedge-44', '9-iron'];

  it('sorts by length within the bag groups and keeps unknown clubs last', () => {
    const owner = ['wedge-58', 'wedge-54', 'wedge-48', 'wedge-44', '9-iron', '8-iron', '7-iron', '6-iron'];
    const longer = ['5-iron', '4-iron', '7-wood', '3-wood', 'mini-driver', 'driver'];
    expect([...longer, 'unknown', '7iron', ...owner].reverse().sort(compareClubsByLength)).toEqual([
      ...owner,
      ...longer,
      '7iron',
      'unknown',
    ]);
    expect([...CLUB_LENGTH_ORDER].sort()).toEqual(ALL_CLUBS.map((club) => club.id).sort());
  });

  it('gives bag neighbours consecutive palette slots, independent of the stored bag order', () => {
    const slots = [...bag].sort(compareClubsByLength).map((club) => clubColorSlot(club, bag));
    expect(slots).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(clubColorSlot('driver', [...bag].reverse())).toBe(7);
    expect(clubColorSlot('5-wood', bag)).toBeNull();
    const fullBag = ALL_CLUBS.map((club) => club.id);
    expect(clubColorSlot('driver', fullBag)).toBe((CLUB_LENGTH_ORDER.length - 1) % 8);
  });
});

describe('collecting shots for a time range', () => {
  const sessions: SavedSession[] = [
    {
      id: 'session_old',
      started_at: daysAgo(12),
      shot_count: 2,
      clubs: ['driver'],
      shots: [saved({ timestamp: daysAgo(12) }), saved({ timestamp: 'not a date' })],
    },
    {
      id: 'session_recent',
      started_at: daysAgo(3),
      shot_count: 4,
      clubs: ['driver', '7-iron'],
      shots: [
        saved(),
        saved({ timestamp: daysAgo(2), profile_id: 'someone-else' }),
        saved({ timestamp: daysAgo(2), mode: 'swing-speed' }),
        saved({ timestamp: daysAgo(0), club: '7-iron', carry_spin_adjusted: 140, estimated_carry_yards: null }),
      ],
    },
  ];
  const live = [
    shot({ timestamp: daysAgo(0), carry_spin_adjusted: 162 }),
    shot({ timestamp: daysAgo(0.01), mode: 'swing-speed' }),
  ];
  const collect = (range: '7d' | '30d' | 'session') =>
    collectShots({ liveShots: live, sessions, profileId: 'p1', range, now: NOW });

  it('uses only the live session for the current session', () => {
    expect(collect('session').map((entry) => entry.carry_spin_adjusted)).toEqual([162]);
  });

  it('merges history of the active profile, newest live data winning, without swing-speed reps', () => {
    const week = collect('7d');
    expect(week.map((entry) => entry.timestamp)).toEqual([daysAgo(3), daysAgo(0)]);
    expect(week[1].carry_spin_adjusted).toBe(162);
    expect(week[0].spin_source).toBeNull();
    expect(collect('30d').map((entry) => entry.timestamp)).toEqual([daysAgo(12), daysAgo(3), daysAgo(0)]);
  });

  it('keeps a saved shot without any carry missing', () => {
    const noCarry: SavedSession = {
      ...sessions[1],
      shots: [saved({ carry_spin_adjusted: null, estimated_carry_yards: null })],
    };
    const [entry] = collectShots({ liveShots: [], sessions: [noCarry], profileId: 'p1', range: '7d', now: NOW });
    expect(classifyShot(entry)).toMatchObject({ reason: 'no_carry' });
  });
});

describe('per-club summary', () => {
  it('summarises plotted shots per club in bag order with exclusions and an ellipse from five shots', () => {
    const irons = [140, 150, 160, 150, 150].map((carry, index) =>
      shot({ timestamp: daysAgo(index), carry_spin_adjusted: carry, carry_lateral_yards: index - 2 })
    );
    const wedges = [80, 84].map((carry, index) =>
      shot({ timestamp: daysAgo(10 + index), club: 'wedge-54', carry_spin_adjusted: carry, carry_lateral_yards: 1 })
    );
    const classified = [...irons, ...wedges, shot({ timestamp: daysAgo(20), launch_angle_horizontal: null })].map(
      classifyShot
    );
    const [wedge, iron] = summarizeClubs(classified);

    expect([wedge.club, iron.club]).toEqual(['wedge-54', '7-iron']);
    expect(iron).toMatchObject({ excluded: 1 });
    expect(iron.points).toHaveLength(5);
    expect(iron.carry.mean).toBeCloseTo(150);
    expect(iron.carry.sd).toBeCloseTo(Math.sqrt(50));
    expect(iron.lateral.mean).toBeCloseTo(0);
    expect(iron.hla.mean).toBeCloseTo(2);
    expect(iron.ballSpeed.n).toBe(5);
    expect(iron.ellipse?.cx).toBeCloseTo(0);
    expect(wedge.ellipse).toBeNull();
    expect(wedge.carry.sd).toBeCloseTo(Math.sqrt(8));
  });

  it('keeps an uncertain ball speed plotted and counted but out of the statistics and the ellipse', () => {
    const trusted = [140, 150, 160, 150, 150].map((carry, index) =>
      shot({ timestamp: daysAgo(index), carry_spin_adjusted: carry, carry_lateral_yards: index - 2 })
    );
    const uncertain = shot({
      timestamp: daysAgo(5),
      ball_speed_mph: 60.8,
      carry_spin_adjusted: 70,
      carry_lateral_yards: 20,
      launch_angle_vertical: 40,
      launch_angle_horizontal: 12,
      ball_speed_status: 'uncertain',
      ball_speed_status_reason: 'club_like_track',
    });
    const [iron] = summarizeClubs([...trusted, uncertain].map(classifyShot));
    const [reference] = summarizeClubs(trusted.map(classifyShot));

    expect(iron.points).toHaveLength(6);
    expect(iron.points.map((entry) => entry.shot)).toContain(uncertain);
    for (const key of ['carry', 'lateral', 'ballSpeed', 'vla', 'hla'] as const) {
      expect(iron[key]).toEqual(reference[key]);
    }
    expect(iron.carry).toMatchObject({ n: 5, mean: 150 });
    expect(iron.ballSpeed.mean).toBeCloseTo(120);
    expect(iron.ellipse).toEqual(reference.ellipse);
  });

  it('draws no ellipse when the trusted shots fall short of five', () => {
    const shots = [140, 150, 160, 150, 150].map((carry, index) =>
      shot({
        timestamp: daysAgo(index),
        carry_spin_adjusted: carry,
        carry_lateral_yards: index - 2,
        ball_speed_status: index === 0 ? 'uncertain' : null,
      })
    );
    const [iron] = summarizeClubs(shots.map(classifyShot));
    expect(iron.points).toHaveLength(5);
    expect(iron.carry.n).toBe(4);
    expect(iron.ellipse).toBeNull();
  });
});

describe('equal-aspect plot geometry', () => {
  const bounds = { xMin: -8, xMax: 3, yMin: 120, yMax: 180 };

  it('uses one scale for both axes, centres the target line and keeps every shot visible', () => {
    for (const [width, height] of [
      [340, 420],
      [480, 360],
    ]) {
      const view = equalAspectViewport(bounds, width, height, 20);
      expect((view.xMax - view.xMin) * view.pxPerUnit).toBeCloseTo(width);
      expect((view.yMax - view.yMin) * view.pxPerUnit).toBeCloseTo(height);
      expect(view.xMin).toBeCloseTo(-view.xMax);
      expect(view.xMin).toBeLessThan(-8);
      expect(view.yMin).toBeLessThan(120);
      expect(view.yMax).toBeGreaterThan(180);
    }
  });

  it('never shows negative carry and keeps a minimum span for a single shot', () => {
    const view = equalAspectViewport({ xMin: 0, xMax: 0, yMin: 10, yMax: 10 }, 300, 600, 20);
    expect(view.yMin).toBe(0);
    expect(view.xMax - view.xMin).toBeGreaterThanOrEqual(20);
  });

  it('includes the full extent of rotated ellipses and converts them to display units', () => {
    const ellipse = { cx: 0, cy: 150, semiMajor: 10, semiMinor: 2, angleRad: Math.PI / 2 };
    expect(plotBounds([{ x: 1, y: 140 }], [ellipse])).toEqual({ xMin: -2, xMax: 2, yMin: 140, yMax: 160 });
    const meters = scaleEllipse(ellipse, 0.9144);
    expect(meters).toMatchObject({ cx: 0, angleRad: Math.PI / 2 });
    expect(meters.cy).toBeCloseTo(137.16);
    expect(meters.semiMajor).toBeCloseTo(9.144);
  });

  it('picks round grid steps shared by both axes', () => {
    expect([0.7, 3.2, 12, 26, 45, 5].map(niceStep)).toEqual([0.5, 5, 10, 20, 50, 5]);
    expect(tickValues(-12, 31, 10)).toEqual([-10, 0, 10, 20, 30]);
    expect(tickValues(-0.5, 0.5, 1)).toEqual([0]);
  });
});
