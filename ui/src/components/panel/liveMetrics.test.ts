import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../../test-utils/shot';
import type { Shot, SwingSpeedStats } from '../../types/shot';
import {
  buildLiveMetrics,
  carrySourceLabel,
  carrySubtext,
  formatSpinCell,
  hasCalculatedMetric,
  LIVE_METRIC_COUNT,
  NO_VALUE,
  pinSelectedMetric,
  SWING_METRIC_COUNT,
  type LiveMetric,
} from './liveMetrics';

const emptySwingStats: SwingSpeedStats = {
  count: 0,
  last_speed_mph: 0,
  best_speed_mph: 0,
  avg_speed_mph: 0,
};

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 92,
    club_speed_mph: 68,
    smash_factor: 1.35,
    estimated_carry_yards: 210,
    carry_range: [205, 215],
    club: 'driver',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13.4,
    launch_angle_horizontal: -1.2,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: 2.1,
    club_path_deg: -0.6,
    spin_axis_deg: 3.4,
    spin_rpm: 2650,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: 214,
    carry_spin_rpm: 2650,
    carry_spin_source: 'measured',
    ...overrides,
  };
}

/** The range's 8-iron: OPS read 1,538 rpm at 39 %, so the carry flew the club's typical 7,500. */
const TYPICAL_SPIN: Partial<Shot> = {
  spin_rpm: 1538,
  spin_confidence: 0.39,
  spin_quality: 'low',
  spin_source: null,
  carry_spin_rpm: 7500,
  carry_spin_source: 'club_typical',
};

function byId(metrics: LiveMetric[], id: string): LiveMetric {
  const metric = metrics.find((m) => m.id === id);
  if (!metric) throw new Error(`no metric ${id}`);
  return metric;
}

describe('buildLiveMetrics', () => {
  it('returns a fixed eleven metrics for a ball-strike shot, including the apex, the curve and the attack angle', () => {
    const metrics = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);

    expect(metrics).toHaveLength(LIVE_METRIC_COUNT);
    expect(new Set(metrics.map((m) => m.id)).size).toBe(LIVE_METRIC_COUNT);
    expect(metrics.map((m) => m.id)).toEqual([
      'ball_speed',
      'carry',
      'apex',
      'club_speed',
      'smash',
      'launch_v',
      'launch_h',
      'spin',
      'curve',
      'club_path',
      'club_aoa',
    ]);
    expect(LIVE_METRIC_COUNT).toBe(11);
    expect(byId(metrics, 'club_aoa')).toMatchObject({ value: '+2.1', unit: '°', label: 'Attack angle' });
  });

  it('names the attack angle in each language', () => {
    const label = (locale: 'de' | 'es') => {
      setActiveLocale(locale);
      try {
        return byId(buildLiveMetrics(makeShot(), 'imperial', emptySwingStats), 'club_aoa').label;
      } finally {
        setActiveLocale('en');
      }
    };

    // Short like "Salida V.": with the ≈ mark of a camera attack angle the long title
    // wrapped into the number on a 568x320 landscape phone.
    expect(label('es')).toBe('Áng. ataque');
    expect(label('de')).toBe('Eintreffwinkel');
  });

  it('keeps the same ten metrics in the same order when values are missing', () => {
    const full = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);
    const sparse = buildLiveMetrics(
      makeShot({
        club_speed_mph: null,
        smash_factor: null,
        launch_angle_vertical: null,
        launch_angle_horizontal: null,
        launch_angle_confidence: null,
        club_path_deg: null,
        spin_axis_deg: null,
        curve_class: null,
        spin_rpm: null,
        spin_quality: null,
        spin_source: null,
        carry_spin_adjusted: null,
        carry_spin_rpm: null,
        carry_spin_source: null,
      }),
      'imperial',
      emptySwingStats
    );

    // The grid must not reflow between shots.
    expect(sparse.map((m) => m.id)).toEqual(full.map((m) => m.id));
    expect(byId(sparse, 'club_speed').value).toBe(NO_VALUE);
    expect(byId(sparse, 'spin').value).toBe(NO_VALUE);
    expect(byId(sparse, 'spin').confidence).toBeNull();
    expect(byId(sparse, 'curve').value).toBe(NO_VALUE);
    // No stale unit hanging off a placeholder.
    expect(byId(sparse, 'club_speed').unit).toBeUndefined();
  });

  it('formats ball speed and carry in the selected unit system', () => {
    const imperial = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);
    const metric = buildLiveMetrics(makeShot(), 'metric', emptySwingStats);

    expect(byId(imperial, 'ball_speed')).toMatchObject({ value: '92.0', unit: 'mph' });
    expect(byId(metric, 'ball_speed')).toMatchObject({ value: '148.1', unit: 'km/h' });
    expect(byId(imperial, 'carry')).toMatchObject({ value: '214', unit: 'yds' });
    expect(byId(metric, 'carry')).toMatchObject({ value: '196', unit: 'm' });
  });

  it('marks a typical-spin carry with ≈ and names the typical spin, without the server span', () => {
    const shot = makeShot({
      club: '7-iron',
      carry_spin_source: 'club_typical',
      // The span the server still sends; no screen shows it.
      ...{ carry_span_min_yards: 198.4, carry_span_max_yards: 221.2 },
    });
    const carry = byId(buildLiveMetrics(shot, 'imperial', emptySwingStats), 'carry');
    expect(carry.value).toBe('≈\u00a0214');
    expect(carry.subtext).toBe('with typical spin');
    expect(carry.title).toBe('Estimated with typical spin');

    const metric = byId(buildLiveMetrics(shot, 'metric', emptySwingStats), 'carry');
    expect(metric).toMatchObject({
      value: '≈\u00a0196',
      unit: 'm',
      subtext: 'with typical spin',
      title: 'Estimated with typical spin',
    });
  });

  it('gives a measured carry no ≈, no span and no typical-spin note', () => {
    const carry = byId(
      buildLiveMetrics(makeShot({ carry_spin_source: 'measured', ...{ carry_span_min_yards: 100, carry_span_max_yards: 120 } }), 'imperial', emptySwingStats),
      'carry'
    );
    expect(carry.value).toBe('214');
    expect(carry.subtext).toBe('Spin-adjusted');
    expect(carry.title).toBeUndefined(); // the driver here was flown with its spin axis
  });

  it('prefers the spin-adjusted carry and marks model carry as estimated', () => {
    const adjusted = byId(buildLiveMetrics(makeShot(), 'imperial', emptySwingStats), 'carry');
    expect(adjusted.value).toBe('214');
    expect(adjusted.subtext).toBe('Spin-adjusted');
    expect(adjusted.estimated).toBeUndefined();

    const estimated = byId(
      buildLiveMetrics(makeShot({ carry_spin_adjusted: null }), 'imperial', emptySwingStats),
      'carry'
    );
    expect(estimated.value).toBe('210');
    expect(estimated.subtext).toBeUndefined();
    expect(estimated.estimated).toBe(true);
  });

  it('marks an uncertain ball speed, its smash and its carry as estimated', () => {
    const metrics = buildLiveMetrics(
      makeShot({ ball_speed_status: 'uncertain', ball_speed_status_reason: 'club_like_track' }),
      'imperial',
      emptySwingStats
    );
    for (const id of ['ball_speed', 'smash', 'carry']) {
      expect(byId(metrics, id).estimated, id).toBe(true);
      // The plain "estimated" mark: no "calculated" label.
      expect(byId(metrics, id).estimatedLabel, id).toBeUndefined();
    }
    // The number stays as measured; only the mark is added.
    expect(byId(metrics, 'ball_speed').value).toBe('92.0');
    expect(byId(metrics, 'carry').subtext).toBe('Spin-adjusted');
    expect(byId(metrics, 'club_speed').estimated).toBeUndefined();
    expect(hasCalculatedMetric(metrics)).toBe(false);
  });

  it('keeps the provisional-carry mark next to an uncertain ball speed (either one marks the carry)', () => {
    const provisional = (status: 'uncertain' | null) =>
      byId(
        buildLiveMetrics(makeShot({ carry_spin_adjusted: null, ball_speed_status: status }), 'imperial', emptySwingStats),
        'carry'
      ).estimated;
    expect(provisional(null)).toBe(true);
    expect(provisional('uncertain')).toBe(true);
  });

  it('gives an uncertain shot without a smash no smash mark', () => {
    const metrics = buildLiveMetrics(
      makeShot({ ball_speed_status: 'uncertain', club_speed_mph: null, smash_factor: null }),
      'imperial',
      emptySwingStats
    );
    expect(byId(metrics, 'smash')).toMatchObject({ value: NO_VALUE, estimated: undefined });
    expect(byId(metrics, 'ball_speed').estimated).toBe(true);
  });

  it.each([
    ['a measured shot', {}],
    ['a replaced ball speed', { ball_speed_status: null, ball_speed_status_reason: 'club_like_track' }],
    ['an older server without the field', { ball_speed_status: undefined }],
  ] as const)('marks no ball speed or smash for %s', (_name, overrides) => {
    const metrics = buildLiveMetrics(makeShot(overrides as Partial<Shot>), 'imperial', emptySwingStats);
    for (const id of ['ball_speed', 'smash', 'carry']) expect(byId(metrics, id).estimated, id).toBeUndefined();
  });

  it('signs the directional angles', () => {
    const metrics = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);

    expect(byId(metrics, 'launch_h').value).toBe('-1.2');
    expect(byId(metrics, 'club_path').value).toBe('-0.6');
    expect(byId(metrics, 'club_aoa').value).toBe('+2.1');

    const negativeAoa = buildLiveMetrics(makeShot({ club_angle_deg: -3.2 }), 'imperial', emptySwingStats);
    expect(byId(negativeAoa, 'club_aoa').value).toBe('-3.2');
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows the spin axis, a loft, the face, face to path, the strike or the low point',
    (locale) => {
      setActiveLocale(locale);
      try {
        for (const axis of [3.4, -4, 0.5]) {
          const metrics = buildLiveMetrics(
            makeShot({ ...UNMEASURABLE_CLUB_DATA, spin_axis_deg: axis }),
            'imperial',
            emptySwingStats
          );
          expect(metrics.map((m) => m.id)).not.toContain('spin_axis');
          const all = JSON.stringify(metrics);
          for (const word of [...UNMEASURABLE_COPY[locale], 'Fade', 'Neutral', 'Neutra']) {
            expect(all, word).not.toContain(word);
          }
          // The curve tile never turns the axis into a number or a class of its own.
          expect(byId(metrics, 'curve').value).toBe(NO_VALUE);
          expect(JSON.stringify(byId(metrics, 'curve'))).not.toMatch(/\d/);
        }
      } finally {
        setActiveLocale('en');
      }
    }
  );

  it('maps launch-angle confidence onto the dot scale', () => {
    const high = buildLiveMetrics(makeShot({ launch_angle_confidence: 0.9 }), 'imperial', emptySwingStats);
    const medium = buildLiveMetrics(makeShot({ launch_angle_confidence: 0.5 }), 'imperial', emptySwingStats);
    const low = buildLiveMetrics(makeShot({ launch_angle_confidence: 0.1 }), 'imperial', emptySwingStats);

    expect(byId(high, 'launch_v').confidence).toBe('high');
    expect(byId(medium, 'launch_v').confidence).toBe('medium');
    expect(byId(low, 'launch_v').confidence).toBe('low');
  });

  const CHAINED_DELIVERY: Partial<Shot> = {
    experimental_fused_attack_angle_deg: -4.2,
    experimental_fused_attack_angle_confidence: 'medium',
    experimental_fused_club_path_deg: -19.7,
    experimental_fused_club_path_confidence: 'high',
    experimental_fused_status: 'approach_mixed',
  };

  it('never shows the chained camera club delivery: the tiles keep their empty state', () => {
    const metrics = buildLiveMetrics(
      makeShot({ club_angle_deg: null, club_path_deg: null, ...CHAINED_DELIVERY }),
      'imperial',
      emptySwingStats
    );

    for (const id of ['club_path', 'club_aoa']) {
      expect(byId(metrics, id)).toMatchObject({
        value: NO_VALUE,
        unit: undefined,
        subtext: undefined,
        confidence: null,
      });
    }
  });

  it('shows no unaccepted TI club candidate, only a rejection status', () => {
    const metrics = buildLiveMetrics(
      makeShot({
        club_angle_deg: null,
        club_path_deg: null,
        experimental_attack_angle_deg: -33.9,
        experimental_attack_angle_status: 'candidate_out_of_bounds',
        experimental_club_path_deg: 30.0,
        experimental_club_path_status: 'rejected_azimuth_fit',
      }),
      'imperial',
      emptySwingStats
    );

    // A candidate caption under a withheld value would read as if a value existed.
    expect(byId(metrics, 'club_aoa')).toMatchObject({
      value: NO_VALUE,
      unit: undefined,
      subtext: undefined,
      confidence: null,
    });
    expect(byId(metrics, 'club_path')).toMatchObject({
      value: NO_VALUE,
      unit: undefined,
      subtext: 'rejected: azimuth fit',
      confidence: 'experimental',
    });
  });

  it.each([
    ['no status', 130.9, null],
    ['candidate_available', -33.9, 'candidate_available'],
    ['candidate_noisy_fit', 12.0, 'candidate_noisy_fit'],
    ['a status without a value', null, 'candidate_available'],
  ])('keeps the club tiles in their empty state for a TI candidate with %s', (_label, value, status) => {
    const metrics = buildLiveMetrics(
      makeShot({
        club_angle_deg: null,
        club_path_deg: null,
        experimental_club_path_deg: value,
        experimental_club_path_status: status,
        experimental_attack_angle_deg: value,
        experimental_attack_angle_status: status,
      }),
      'imperial',
      emptySwingStats
    );

    for (const id of ['club_path', 'club_aoa']) {
      expect(byId(metrics, id)).toMatchObject({ value: NO_VALUE, unit: undefined, subtext: undefined, confidence: null });
    }
  });

  it('keeps showing an accepted TI club path, also next to a chained camera estimate', () => {
    const metrics = buildLiveMetrics(
      makeShot({
        club_angle_deg: null,
        club_path_deg: null,
        experimental_club_path_deg: 2.4,
        experimental_club_path_status: 'accepted',
        ...CHAINED_DELIVERY,
      }),
      'imperial',
      emptySwingStats
    );

    // Measured: no ≈. Keeps its experimental dot; the caption is "not calibrated".
    expect(byId(metrics, 'club_path')).toMatchObject({
      value: '+2.4',
      unit: '°',
      subtext: 'not calibrated',
      confidence: 'experimental',
    });
    expect(byId(metrics, 'club_path').estimated).toBeUndefined();
    expect(byId(metrics, 'club_path').estimatedLabel).toBeUndefined();
    expect(byId(metrics, 'club_aoa').value).toBe(NO_VALUE);
  });

  it.each([
    ['de', '+1,5', '-2,0', 'nicht kalibriert'],
    ['en', '+1.5', '-2.0', 'not calibrated'],
    ['es', '+1,5', '-2,0', 'no calibrado'],
  ] as const)('%s: a radar club path and attack angle read "not calibrated", without ≈', (locale, path, aoa, caption) => {
    setActiveLocale(locale);
    try {
      const metrics = buildLiveMetrics(makeShot({ club_path_deg: 1.5, club_angle_deg: -2 }), 'imperial', emptySwingStats);
      for (const [id, value] of [['club_path', path], ['club_aoa', aoa]] as const) {
        expect(byId(metrics, id), id).toMatchObject({
          value,
          unit: '°',
          subtext: caption,
          confidence: null,
        });
        expect(byId(metrics, id).estimated, id).toBeUndefined();
        expect(byId(metrics, id).estimatedLabel, id).toBeUndefined();
      }
      // Measured values: they do not make the ball page name "≈ calculated".
      expect(hasCalculatedMetric(metrics.filter((m) => m.id === 'club_path' || m.id === 'club_aoa'))).toBe(false);
    } finally {
      setActiveLocale('en');
    }
  });

  it('gives an accepted experimental attack angle "not calibrated" and keeps its dot', () => {
    const metrics = buildLiveMetrics(
      makeShot({
        club_angle_deg: null,
        experimental_attack_angle_deg: -3.1,
        experimental_attack_angle_status: 'accepted_arc_fit',
      }),
      'imperial',
      emptySwingStats
    );
    expect(byId(metrics, 'club_aoa')).toMatchObject({
      value: '-3.1',
      subtext: 'not calibrated',
      confidence: 'experimental',
    });
    expect(byId(metrics, 'club_aoa').estimated).toBeUndefined();
  });

  it('keeps the rejection reason, not "not calibrated", on an experimental tile without a value', () => {
    const metrics = buildLiveMetrics(
      makeShot({
        club_angle_deg: null,
        experimental_attack_angle_deg: null,
        experimental_attack_angle_status: 'rejected_no_club_track',
      }),
      'imperial',
      emptySwingStats
    );
    expect(byId(metrics, 'club_aoa')).toMatchObject({
      value: NO_VALUE,
      subtext: 'rejected: no club track',
      confidence: 'experimental',
    });
    expect(byId(metrics, 'club_aoa').estimated).toBeUndefined();
  });

  it('leaves empty radar club tiles without ≈', () => {
    const metrics = buildLiveMetrics(makeShot({ club_path_deg: null, club_angle_deg: null }), 'imperial', emptySwingStats);
    for (const id of ['club_path', 'club_aoa']) {
      expect(byId(metrics, id), id).toMatchObject({ value: NO_VALUE, subtext: undefined });
      expect(byId(metrics, id).estimated, id).toBeUndefined();
    }
  });

  it('labels camera-assisted horizontal launch', () => {
    const metrics = buildLiveMetrics(
      makeShot({ launch_angle_horizontal_source: 'camera_assisted_experimental' }),
      'imperial',
      emptySwingStats
    );

    expect(byId(metrics, 'launch_h')).toMatchObject({
      subtext: 'camera assisted',
      confidenceLabel: 'experimental',
    });
  });

  // The range: TI read the vertical with both channels agreeing (0.95) while the
  // start direction tile borrowed that grade instead of showing its own.
  it("grades the start direction by its own confidence, not the vertical angle's", () => {
    const metrics = buildLiveMetrics(
      makeShot({
        launch_angle_confidence: 0.95,
        launch_angle_horizontal_confidence: 0.3,
        launch_angle_horizontal_source: 'radar',
      }),
      'imperial',
      emptySwingStats,
      { directionCalibrated: true }
    );

    expect(byId(metrics, 'launch_v').confidence).toBe('high');
    expect(byId(metrics, 'launch_h').confidence).toBe('low');
    expect(byId(metrics, 'launch_h').confidenceLabel).toBeUndefined();
  });

  it('calls a radar start direction not calibrated, without grading it, until the phase zero is set', () => {
    const shot = makeShot({ launch_angle_horizontal_confidence: 0.95, launch_angle_horizontal_source: 'radar' });

    for (const options of [undefined, { directionCalibrated: false }]) {
      const metric = byId(buildLiveMetrics(shot, 'imperial', emptySwingStats, options), 'launch_h');
      expect(metric).toMatchObject({
        label: 'Start direction',
        value: '-1.2',
        subtext: 'not calibrated',
        confidence: null,
      });
      expect(metric.confidenceLabel).toBeUndefined();
    }
  });

  it('drops the not-calibrated caption once the phase zero is set', () => {
    const metric = byId(
      buildLiveMetrics(
        makeShot({ launch_angle_horizontal_confidence: 0.95, launch_angle_horizontal_source: 'radar' }),
        'imperial',
        emptySwingStats,
        { directionCalibrated: true }
      ),
      'launch_h'
    );

    expect(metric).toMatchObject({ label: 'Start direction', confidence: 'high' });
    expect(metric.subtext).toBeUndefined();
  });

  it('treats a saved direction without a source as the radar bearing', () => {
    const metrics = buildLiveMetrics(
      makeShot({ launch_angle_horizontal_confidence: 0.95, launch_angle_horizontal_source: undefined }),
      'imperial',
      emptySwingStats
    );

    expect(byId(metrics, 'launch_h')).toMatchObject({ subtext: 'not calibrated', confidence: null });
  });

  it('leaves an estimated direction without a source to its estimated mark', () => {
    const metrics = buildLiveMetrics(
      makeShot({ angle_source: 'estimated', launch_angle_horizontal_source: undefined }),
      'imperial',
      emptySwingStats
    );

    expect(byId(metrics, 'launch_h')).toMatchObject({ confidence: null, estimated: true });
    expect(byId(metrics, 'launch_h').confidenceLabel).toBeUndefined();
    expect(byId(metrics, 'launch_h').subtext).toBeUndefined();
  });

  it('grades camera and estimated start directions by their own confidence, calibrated or not', () => {
    const camera = buildLiveMetrics(
      makeShot({
        launch_angle_horizontal_source: 'camera_assisted_experimental',
        launch_angle_horizontal_confidence: 0.5,
      }),
      'imperial',
      emptySwingStats
    );
    const estimated = buildLiveMetrics(
      makeShot({
        angle_source: 'estimated',
        launch_angle_horizontal_source: 'estimated',
        launch_angle_horizontal_confidence: 0.35,
      }),
      'imperial',
      emptySwingStats
    );

    expect(byId(camera, 'launch_h')).toMatchObject({
      subtext: 'camera assisted',
      confidence: 'medium',
      confidenceLabel: 'experimental',
    });
    expect(byId(estimated, 'launch_h')).toMatchObject({ confidence: 'low', estimated: true });
    expect(byId(estimated, 'launch_h').confidenceLabel).toBeUndefined();
  });

  it('shows no quality for a missing start direction', () => {
    const metrics = buildLiveMetrics(
      makeShot({ launch_angle_horizontal: null, launch_angle_horizontal_source: null }),
      'imperial',
      emptySwingStats
    );

    expect(byId(metrics, 'launch_h')).toMatchObject({ value: NO_VALUE, confidence: null });
    expect(byId(metrics, 'launch_h').confidenceLabel).toBeUndefined();
    expect(byId(metrics, 'launch_h').subtext).toBeUndefined();
  });

  it.each([
    ['de', 'Startrichtung', 'nicht kalibriert'],
    ['en', 'Start direction', 'not calibrated'],
    ['es', 'Salida H.', 'no calibrado'],
  ] as const)('keeps the title %s and says not calibrated in the caption', (locale, label, subtext) => {
    setActiveLocale(locale);
    try {
      const metric = byId(
        buildLiveMetrics(makeShot({ launch_angle_horizontal_source: 'radar' }), 'metric', emptySwingStats),
        'launch_h'
      );

      expect(metric).toMatchObject({ label, subtext, confidence: null });
      expect(metric.confidenceLabel).toBeUndefined();
    } finally {
      setActiveLocale('en');
    }
  });

  it('marks estimated launch and spin with a flag, not provenance subtext', () => {
    const measured = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);
    expect(byId(measured, 'launch_v').subtext).toBeUndefined();
    expect(byId(measured, 'launch_v').estimated).toBeUndefined();
    expect(byId(measured, 'launch_h').estimated).toBeUndefined();
    expect(byId(measured, 'spin').subtext).toBeUndefined();
    expect(byId(measured, 'spin').estimated).toBeUndefined();

    const estimated = buildLiveMetrics(
      makeShot({ angle_source: 'estimated', spin_source: 'calculated', carry_spin_source: 'calculated' }),
      'imperial',
      emptySwingStats
    );
    expect(byId(estimated, 'launch_v').estimated).toBe(true);
    expect(byId(estimated, 'launch_h').estimated).toBe(true);
    expect(byId(estimated, 'spin').estimated).toBe(true);
    expect(byId(estimated, 'launch_v').subtext).toBeUndefined();
    expect(byId(estimated, 'spin').subtext).toBeUndefined();
  });

  it('returns the swing-speed set, with ids that never collide with ball-strike ids', () => {
    const stats: SwingSpeedStats = { count: 12, last_speed_mph: 101, best_speed_mph: 108, avg_speed_mph: 99 };
    const swing = buildLiveMetrics(
      makeShot({ mode: 'swing-speed', training_implement_label: 'Stack 100g', swing_speed_reading_count: 7 }),
      'imperial',
      stats
    );
    const ballStrike = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);

    expect(swing).toHaveLength(SWING_METRIC_COUNT);
    expect(byId(swing, 'swing_best').value).toBe('108.0');
    expect(byId(swing, 'swing_implement').value).toBe('Stack 100g');

    const shared = swing.map((m) => m.id).filter((id) => ballStrike.some((m) => m.id === id));
    expect(shared).toEqual([]);
  });
});

describe('curve tile', () => {
  afterEach(() => setActiveLocale('en'));

  const iron = (overrides: Partial<Shot> = {}) => makeShot({ club: '7-iron', spin_axis_deg: 5.9, ...overrides });
  const curve = (shot: Shot) => byId(buildLiveMetrics(shot, 'imperial', emptySwingStats), 'curve');

  const WORDS = {
    en: { label: 'Curve', calculated: 'calculated', hook: 'Hook', draw: 'Draw', straight: 'Straight', fade: 'Fade', slice: 'Slice' },
    de: { label: 'Kurve', calculated: 'berechnet', hook: 'Hook', draw: 'Draw', straight: 'Gerade', fade: 'Fade', slice: 'Slice' },
    es: { label: 'Curva', calculated: 'calculado', hook: 'Hook', draw: 'Draw', straight: 'Recta', fade: 'Fade', slice: 'Slice' },
  } as const;
  const CLASSES = ['hook', 'draw', 'straight', 'fade', 'slice'] as const;

  it.each(['en', 'de', 'es'] as const)('%s: shows the class word, calculated, never a number', (locale) => {
    setActiveLocale(locale);
    for (const cls of CLASSES) {
      const metric = curve(iron({ curve_class: cls }));
      expect(metric).toMatchObject({
        id: 'curve',
        label: WORDS[locale].label,
        value: WORDS[locale][cls],
        kind: 'class',
        subtext: WORDS[locale].calculated,
        estimated: true,
        estimatedLabel: WORDS[locale].calculated,
      });
      expect(metric.unit).toBeUndefined();
      expect(JSON.stringify(metric)).not.toMatch(/\d/);
    }
  });

  it('sits where the spin axis tile was: after spin, before the club path', () => {
    const ids = buildLiveMetrics(iron({ curve_class: 'draw' }), 'imperial', emptySwingStats).map((m) => m.id);
    expect(ids.indexOf('curve')).toBe(ids.indexOf('spin') + 1);
    expect(ids.indexOf('club_path')).toBe(ids.indexOf('curve') + 1);
  });

  it.each([
    ['en', 'not for woods'],
    ['de', 'nicht bei Hölzern'],
    ['es', 'no en maderas'],
  ] as const)('%s: shows — for a wood, which has no axis by design', (locale, caption) => {
    setActiveLocale(locale);
    for (const club of ['driver', 'mini-driver', '3-wood', '5-wood', '7-wood']) {
      const metric = curve(makeShot({ club, spin_axis_deg: null, curve_class: null }));
      expect(metric, club).toMatchObject({ value: NO_VALUE, subtext: caption });
      expect(metric.estimated, club).toBeUndefined();
      expect(metric.kind, club).toBeUndefined();
    }
  });

  it('shows — without a caption when an iron has no class, whatever its spin axis', () => {
    for (const shot of [iron({ curve_class: null }), iron({ curve_class: undefined }), iron({ spin_axis_deg: -12, curve_class: null })]) {
      const metric = curve(shot);
      expect(metric.value).toBe(NO_VALUE);
      expect(metric.subtext).toBeUndefined();
      expect(metric.estimated).toBeUndefined();
    }
  });

  it('shows — for a class word the UI does not know', () => {
    const metric = curve(iron({ curve_class: 'banana' as unknown as Shot['curve_class'] }));
    expect(metric.value).toBe(NO_VALUE);
  });

  it('follows the server class, not the axis number', () => {
    // A slice axis with a straight class reads "Straight": the class is the server's.
    expect(curve(iron({ spin_axis_deg: 14.5, curve_class: 'straight' })).value).toBe('Straight');
  });
});

describe('apex tile', () => {
  afterEach(() => setActiveLocale('en'));
  const apex = (overrides: Partial<Shot>, units: 'imperial' | 'metric' = 'imperial') =>
    byId(buildLiveMetrics(makeShot(overrides), units, emptySwingStats), 'apex');

  it('shows the apex in the distance unit, whole, ≈ "calculated" after a measured-spin carry', () => {
    expect(apex({ apex_yards: 31.6 })).toEqual({
      id: 'apex',
      label: 'Apex',
      value: '32',
      unit: 'yds',
      subtext: 'calculated',
      estimated: true,
      estimatedLabel: 'calculated',
    });
    // 31.6 yd = 28.9 m.
    expect(apex({ apex_yards: 31.6 }, 'metric')).toMatchObject({ value: '29', unit: 'm' });
    expect(byId(buildLiveMetrics(makeShot({ apex_yards: 31.6 }), 'mph-meters', emptySwingStats), 'apex').unit).toBe('m');
    expect(byId(buildLiveMetrics(makeShot({ apex_yards: 31.6 }), 'kmh-yards', emptySwingStats), 'apex').unit).toBe('yds');
  });

  it('reads like a typical-spin carry: ≈ in front of the number, "with typical spin"', () => {
    const metric = apex({ ...TYPICAL_SPIN, apex_yards: 27.2 });
    expect(metric).toMatchObject({ value: '≈\u00a027', unit: 'yds', subtext: 'with typical spin' });
    expect(metric.estimated).toBeUndefined();
    expect(byId(buildLiveMetrics(makeShot({ ...TYPICAL_SPIN, apex_yards: 27.2 }), 'imperial', emptySwingStats), 'carry').value).toMatch(/^≈\u00a0/);
  });

  it.each([null, undefined, Number.NaN])('reads "—" without an apex (%s): no unit, caption or mark', (value) => {
    expect(apex({ apex_yards: value as number | null | undefined })).toEqual({ id: 'apex', label: 'Apex', value: NO_VALUE });
  });

  it('is captioned in each language', () => {
    setActiveLocale('de');
    expect(apex({ apex_yards: 30 }, 'metric')).toMatchObject({ label: 'Apex', subtext: 'berechnet', value: '27' });
    expect(apex({ ...TYPICAL_SPIN, apex_yards: 30 }, 'metric').subtext).toBe('mit typischem Spin');
    setActiveLocale('es');
    expect(apex({ apex_yards: 30 }, 'metric')).toMatchObject({ label: 'Apex', subtext: 'calculado' });
  });

  it('marks the ball page as showing a calculated value', () => {
    expect(hasCalculatedMetric([apex({ apex_yards: 30 })])).toBe(true);
    expect(hasCalculatedMetric([apex({ ...TYPICAL_SPIN, apex_yards: 30 })])).toBe(false);
    expect(hasCalculatedMetric([apex({ apex_yards: null })])).toBe(false);
  });
});

describe('pinSelectedMetric', () => {
  const metrics = buildLiveMetrics(makeShot(), 'imperial', emptySwingStats);

  it('moves the selected metric to the front and keeps the rest in order', () => {
    const pinned = pinSelectedMetric(metrics, 'spin');

    expect(pinned.map((m) => m.id)).toEqual([
      'spin',
      'ball_speed',
      'carry',
      'apex',
      'club_speed',
      'smash',
      'launch_v',
      'launch_h',
      'curve',
      'club_path',
      'club_aoa',
    ]);
  });

  it('falls back to the first metric when the stored id is absent', () => {
    expect(pinSelectedMetric(metrics, 'swing_best')[0]?.id).toBe('ball_speed');
    expect(pinSelectedMetric(metrics, null)[0]?.id).toBe('ball_speed');
    expect(pinSelectedMetric(metrics, 'nonsense')).toHaveLength(LIVE_METRIC_COUNT);
  });

  it('handles an empty metric list', () => {
    expect(pinSelectedMetric([], 'ball_speed')).toEqual([]);
  });
});

describe('spin tile', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  const spin = (overrides: Partial<Shot>) =>
    byId(buildLiveMetrics(makeShot(overrides), 'imperial', emptySwingStats), 'spin');

  it('shows a measured spin as today: no mark, with its quality', () => {
    expect(spin({})).toMatchObject({ value: '2,650', unit: 'rpm', confidence: 'high' });
    expect(spin({}).estimated).toBeUndefined();
    expect(spin({}).subtext).toBeUndefined();
    expect(spin({ spin_quality: 'experimental' })).toMatchObject({
      confidence: 'experimental',
      confidenceLabel: 'Unconfirmed',
    });
  });

  it('shows the typical spin the carry used, marked typical, instead of an unreliable reading', () => {
    const typical = spin(TYPICAL_SPIN);

    expect(typical).toMatchObject({ value: '7,500', unit: 'rpm', estimated: true, subtext: 'typical' });
    expect(typical.confidence).toBeNull();
    expect(typical.confidenceLabel).toBeUndefined();
    expect(spin({ ...TYPICAL_SPIN, carry_spin_source: 'speed_model' })).toMatchObject({
      value: '7,500',
      subtext: 'typical',
    });
  });

  it('does not let an experimental reading badge the typical spin', () => {
    const typical = spin({ ...TYPICAL_SPIN, spin_quality: 'experimental' });

    expect(typical.confidence).toBeNull();
    expect(typical.confidenceLabel).toBeUndefined();
  });

  it('marks typical spin in German', () => {
    setActiveLocale('de');

    expect(spin(TYPICAL_SPIN)).toMatchObject({ value: '7.500', subtext: 'typisch', estimated: true });
  });

  it('keeps calculated spin marked estimated, as before, without calling it typical', () => {
    const calculated = spin({
      spin_rpm: 6234,
      spin_source: 'calculated',
      carry_spin_rpm: 6234,
      carry_spin_source: 'calculated',
    });

    expect(calculated).toMatchObject({ value: '6,234', unit: 'rpm', estimated: true, confidence: 'high' });
    expect(calculated.subtext).toBeUndefined();
  });

  it('shows the no-value dash, never the raw spin, when the shot has no carry spin', () => {
    const missing = spin({ spin_rpm: 6000, carry_spin_rpm: undefined, carry_spin_source: undefined });

    expect(missing.value).toBe(NO_VALUE);
    expect(missing.unit).toBeUndefined();
    expect(missing.confidence).toBeNull();
    expect(missing.estimated).toBeUndefined();
    expect(missing.subtext).toBeUndefined();
  });
});

describe('formatSpinCell', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('puts ≈ in front of spin that was not measured and a dash where there is none', () => {
    expect(formatSpinCell(makeShot())).toBe('2,650');
    expect(formatSpinCell(makeShot(TYPICAL_SPIN))).toBe('≈7,500');
    expect(formatSpinCell(makeShot({ carry_spin_rpm: 6234, carry_spin_source: 'calculated' }))).toBe('≈6,234');
    expect(formatSpinCell({ spin_rpm: 6000 })).toBe(NO_VALUE);
  });

  it('formats the number in the active language', () => {
    setActiveLocale('de');

    expect(formatSpinCell(makeShot(TYPICAL_SPIN))).toBe('≈7.500');
  });
});

describe('carrySourceLabel', () => {
  const base = { carry_spin_adjusted: 150, carry_spin_source: 'measured' } as Shot;

  it('labels provisional and spin-estimated carry, and leaves measured carry alone', () => {
    expect(carrySourceLabel({ ...base, carry_spin_adjusted: null })).toBe('Provisional carry estimate');
    expect(carrySourceLabel({ ...base, carry_spin_source: 'club_typical' })).toBe('with typical spin');
    expect(carrySourceLabel({ ...base, carry_spin_source: 'speed_model' })).toBe('Estimated · spin unconfirmed');
    expect(carrySourceLabel({ ...base, carry_spin_source: 'calculated' })).toBe('Estimated · spin unconfirmed');
    expect(carrySourceLabel(base)).toBeUndefined();
  });
});

describe('carrySubtext', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('shows the Total under the carry once the shot is final', () => {
    expect(carrySubtext(makeShot({ total_yards: 171 }), 'imperial')).toBe('Total 171 yds');
  });

  it('keeps the spin note next to the Total', () => {
    expect(carrySubtext(makeShot({ ...TYPICAL_SPIN, total_yards: 171 }), 'imperial')).toBe(
      'Total 171 yds · with typical spin'
    );
  });

  it('shows no Total for a provisional shot', () => {
    expect(carrySubtext(makeShot({ carry_spin_adjusted: null, total_yards: 171 }), 'imperial')).toBe(
      'Provisional carry estimate'
    );
  });

  it('shows Total — for a final shot without a Total', () => {
    expect(carrySubtext(makeShot(), 'imperial')).toBe('Total —');
  });

  it('keeps the spin note next to Total — when the shot has no Total', () => {
    expect(carrySubtext(makeShot(TYPICAL_SPIN), 'imperial')).toBe('Total — · with typical spin');
  });

  it.each([null, Number.NaN, Number.POSITIVE_INFINITY])('shows Total — for %s', (total_yards) => {
    expect(carrySubtext(makeShot({ total_yards }), 'imperial')).toBe('Total —');
  });

  it('shows a Total of zero like any other', () => {
    expect(carrySubtext(makeShot({ carry_spin_adjusted: 0, total_yards: 0 }), 'imperial')).toBe('Total 0 yds');
  });

  it('gives the Total in the chosen distance unit', () => {
    expect(carrySubtext(makeShot({ total_yards: 171 }), 'metric')).toBe('Total 156 m');
    expect(carrySubtext(makeShot({ total_yards: 171 }), 'kmh-yards')).toBe('Total 171 yds');
  });

  it('is worded in German', () => {
    setActiveLocale('de');

    expect(carrySubtext(makeShot({ total_yards: 171 }), 'mph-meters')).toBe('Total 156 m');
    expect(carrySubtext(makeShot({ ...TYPICAL_SPIN, total_yards: 171 }), 'imperial')).toBe(
      'Total 171 yds · mit typischem Spin'
    );
    expect(carrySubtext(makeShot(), 'imperial')).toBe('Total —');
  });
});
