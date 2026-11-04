import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { makeShot, UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY, unmeasurableClubPageCopy } from '../../test-utils/shot';
import type { Shot } from '../../types/shot';
import { buildLiveMetrics, hasCalculatedMetric, NO_VALUE, type LiveMetric } from './liveMetrics';
import { buildClubMetrics, CALCULATED_CLUB_METRIC_IDS, CLUB_METRIC_IDS, clubDataSummaryParts } from './clubMetrics';

const byId = (metrics: LiveMetric[], id: string) => metrics.find((m) => m.id === id) as LiveMetric;
const noSwing = { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 };

/** An 8-iron with a camera path and attack angle, and everything else the server sends. */
const IRON: Partial<Shot> = {
  ...UNMEASURABLE_CLUB_DATA,
  club: '8-iron',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: -1.2,
  club_data_status: 'done',
  camera_club_path_deg: 6.9,
  camera_club_path_sigma_deg: 1.24,
  camera_attack_angle_deg: -10.4,
  camera_attack_angle_sigma_deg: 0.8,
};

const EMPTY: Partial<Shot> = {
  camera_club_path_deg: null,
  camera_club_path_sigma_deg: null,
  camera_attack_angle_deg: null,
  camera_attack_angle_sigma_deg: null,
  camera_low_point_cm: null,
  camera_low_point_sigma_cm: null,
  spin_axis_deg: null,
};

afterEach(() => setActiveLocale('en'));

describe('buildClubMetrics', () => {
  it('always returns club speed, path, attack angle, impact point and low point, in that order', () => {
    expect([...CLUB_METRIC_IDS]).toEqual(['club_speed', 'cd_path', 'cd_aoa', 'cd_impact', 'cd_low_point']);
    expect(buildClubMetrics(makeShot(IRON), 'metric').map((m) => m.id)).toEqual([...CLUB_METRIC_IDS]);
    expect(buildClubMetrics(makeShot({ club: '8-iron', ...EMPTY }), 'metric').map((m) => m.id)).toEqual([
      ...CLUB_METRIC_IDS,
    ]);
  });

  it.each([
    ['de', 'berechnet · unkalibriert', '±1,2° · berechnet · unkalibriert', '±0,8° · berechnet · unkalibriert'],
    ['en', 'calculated · uncalibrated', '±1.2° · calculated · uncalibrated', '±0.8° · calculated · uncalibrated'],
    ['es', 'calculado · sin calibrar', '±1,2° · calculado · sin calibrar', '±0,8° · calculado · sin calibrar'],
  ] as const)(
    '%s: path and attack angle carry ≈ and ±σ, both "calculated · uncalibrated"',
    (locale, uncalibrated, pathCaption, aoaCaption) => {
      setActiveLocale(locale);
      const metrics = buildClubMetrics(makeShot(IRON), 'metric');
      expect(byId(metrics, 'cd_path')).toMatchObject({
        estimated: true,
        estimatedLabel: uncalibrated,
        subtext: pathCaption,
      });
      expect(byId(metrics, 'cd_aoa')).toMatchObject({
        estimated: true,
        estimatedLabel: uncalibrated,
        subtext: aoaCaption,
      });
      // Measured club speed stays unmarked; smash stays on the ball page.
      expect(byId(metrics, 'club_speed').estimated).toBeUndefined();
      expect(metrics.some((m) => m.id === 'smash')).toBe(false);
    }
  );

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows a loft, the face, face to path, the spin axis or the strike',
    (locale) => {
      setActiveLocale(locale);
      const metrics = buildClubMetrics(makeShot(IRON), 'metric');
      // The low point has a card of its own (below); nothing else names it or its centimetres.
      const all = JSON.stringify(metrics.filter((metric) => metric.id !== 'cd_low_point'));
      // The impact point's own title is allowed here; its old field (impact_toe_mm) is never read.
      for (const word of unmeasurableClubPageCopy(locale)) expect(all, word).not.toContain(word);
      expect(byId(metrics, 'cd_impact').value).toBe(NO_VALUE);
      // Nor their numbers: loft 37.7/52.7, face -12.2, F2P -19.1, low point 12.3, impact -9 mm.
      expect(all).not.toMatch(/37[.,]7|52[.,]7|12[.,]2|19[.,]1|12[.,]3|"12"|cm|mm/);
    }
  );

  it('captions a path without a sigma just "calculated · uncalibrated"', () => {
    const shot = makeShot({ ...IRON, camera_club_path_sigma_deg: null });
    expect(byId(buildClubMetrics(shot, 'metric'), 'cd_path').subtext).toBe('calculated · uncalibrated');
  });

  it('leaves an empty path tile without ≈ or caption', () => {
    const path = byId(buildClubMetrics(makeShot({ ...IRON, ...EMPTY }), 'metric'), 'cd_path');
    expect(path).toMatchObject({ value: NO_VALUE, estimated: undefined, estimatedLabel: undefined, subtext: undefined });
  });

  it('captions empty tiles "calculating" while the club data is pending, and nothing once skipped', () => {
    setActiveLocale('de');
    const pending = makeShot({ ...IRON, ...EMPTY, club_data_status: 'pending' });
    const metrics = buildClubMetrics(pending, 'metric');
    for (const id of CALCULATED_CLUB_METRIC_IDS) {
      expect(byId(metrics, id), id).toMatchObject({ value: NO_VALUE, subtext: 'wird berechnet' });
    }
    expect(byId(metrics, 'club_speed').subtext).toBeUndefined();
    const skipped = makeShot({ ...IRON, ...EMPTY, club_data_status: 'skipped' });
    expect(byId(buildClubMetrics(skipped, 'metric'), 'cd_path')).toMatchObject({
      value: NO_VALUE,
      subtext: undefined,
    });
  });

  it('never captions the measured club speed "calculating" while the club data is pending', () => {
    setActiveLocale('de');
    const pending = makeShot({
      ...IRON,
      ...EMPTY,
      club_speed_mph: null,
      smash_factor: null,
      club_data_status: 'pending',
    });
    const metrics = buildClubMetrics(pending, 'metric');
    expect(byId(metrics, 'club_speed').value).toBe(NO_VALUE);
    expect(byId(metrics, 'club_speed').subtext).toBeUndefined();
  });
});

describe('the low point card (cd_low_point)', () => {
  const lowPoint = (overrides: Partial<Shot>, units: 'metric' | 'imperial' = 'metric') =>
    byId(buildClubMetrics(makeShot({ ...IRON, ...overrides }), units), 'cd_low_point');

  it.each([
    ['de', 'Tiefpunkt', 'vor dem Ball · ±2,1 cm · berechnet · unkalibriert', 'hinter dem Ball', 'am Ball', 'berechnet · unkalibriert'],
    ['en', 'Low point', 'ahead of the ball · ±2.1 cm · calculated · uncalibrated', 'behind the ball', 'at the ball', 'calculated · uncalibrated'],
    ['es', 'Punto bajo', 'delante de la bola · ±2,1 cm · calculado · sin calibrar', 'detrás de la bola', 'en la bola', 'calculado · sin calibrar'],
  ] as const)('%s: the distance without sign, its side, ±σ and the attack angle\'s caption', (locale, label, ahead, behind, at, uncalibrated) => {
    setActiveLocale(locale);
    const decimal = locale === 'en' ? '.' : ',';
    expect(lowPoint({ camera_low_point_cm: 12.3, camera_low_point_sigma_cm: 2.1 })).toEqual({
      id: 'cd_low_point',
      label,
      value: `12${decimal}3`,
      unit: 'cm',
      subtext: ahead,
      estimated: true,
      estimatedLabel: uncalibrated,
    });
    // Behind the ball (negative): the same distance, no minus sign.
    expect(lowPoint({ camera_low_point_cm: -4.06, camera_low_point_sigma_cm: 1 })).toMatchObject({
      value: `4${decimal}1`,
      subtext: `${behind} · ±1${decimal}0 cm · ${uncalibrated}`,
    });
    // Under half a centimetre either way: at the ball.
    for (const cm of [0.49, -0.49, 0]) {
      expect(lowPoint({ camera_low_point_cm: cm, camera_low_point_sigma_cm: null }).subtext, `${cm}`).toBe(`${at} · ${uncalibrated}`);
    }
    expect(lowPoint({ camera_low_point_cm: 0.5 }).subtext?.startsWith(ahead.split(' · ')[0])).toBe(true);
    expect(lowPoint({ camera_low_point_cm: -0.5 }).subtext?.startsWith(behind)).toBe(true);
  });

  it('shares the attack angle\'s ≈ label and caption ending', () => {
    const metrics = buildClubMetrics(makeShot({ ...IRON, camera_low_point_cm: 6, camera_low_point_sigma_cm: 1.5 }), 'metric');
    const aoa = byId(metrics, 'cd_aoa');
    const low = byId(metrics, 'cd_low_point');
    expect(low.estimatedLabel).toBe(aoa.estimatedLabel);
    expect(low.subtext?.endsWith(aoa.estimatedLabel!)).toBe(true);
    expect(hasCalculatedMetric([low])).toBe(true);
  });

  it('reads inches with yards, one decimal, sigma too', () => {
    // 12.3 cm = 4.84 in, 2.1 cm = 0.83 in.
    expect(lowPoint({ camera_low_point_cm: 12.3, camera_low_point_sigma_cm: 2.1 }, 'imperial')).toMatchObject({
      value: '4.8',
      unit: 'in',
      subtext: 'ahead of the ball · ±0.8 in · calculated · uncalibrated',
    });
    expect(byId(buildClubMetrics(makeShot({ ...IRON }), 'mph-meters'), 'cd_low_point').unit).toBe('cm');
    expect(byId(buildClubMetrics(makeShot({ ...IRON }), 'kmh-yards'), 'cd_low_point').unit).toBe('in');
  });

  it.each([null, undefined, Number.NaN])('reads "—" without a low point (%s): no unit, caption or ≈', (cm) => {
    expect(lowPoint({ camera_low_point_cm: cm as number | null | undefined })).toMatchObject({
      value: NO_VALUE,
      unit: undefined,
      subtext: undefined,
      estimated: undefined,
      estimatedLabel: undefined,
    });
  });

  it('reads "—" for a driver, whose low point the server withholds', () => {
    const driver = makeShot({ ...IRON, club: 'driver', camera_low_point_cm: null, camera_low_point_sigma_cm: null });
    expect(byId(buildClubMetrics(driver, 'metric'), 'cd_low_point').value).toBe(NO_VALUE);
  });
});

describe('the impact point card (cd_impact)', () => {
  const impact = (overrides: Partial<Shot>) => byId(buildClubMetrics(makeShot({ ...IRON, ...overrides }), 'metric'), 'cd_impact');

  it.each([
    ['de', 'Treffpunkt', ['Ferse', 'Mitte', 'Spitze', 'unsicher'], 'berechnet · unkalibriert'],
    ['en', 'Impact', ['Heel', 'Center', 'Toe', 'uncertain'], 'calculated · uncalibrated'],
    ['es', 'Impacto', ['Talón', 'Centro', 'Punta', 'incierto'], 'calculado · sin calibrar'],
  ] as const)('%s: names the zone under "%s", with ≈ and "calculated · uncalibrated"', (locale, label, words, caption) => {
    setActiveLocale(locale);
    const cases = [
      [-14, 'heel'],
      [1.5, 'center'],
      [12, 'toe'],
      [5, null],
    ] as const;
    cases.forEach(([offset, zone], index) => {
      const metric = impact({ impact_offset_mm: offset, impact_sigma_mm: 3, impact_zone: zone, impact_method: 'D' });
      expect(metric, `${offset}`).toMatchObject({
        label,
        value: words[index],
        kind: 'class',
        estimated: true,
        estimatedLabel: caption,
        subtext: caption,
      });
      // No millimetres, no sigma.
      expect(JSON.stringify(metric)).not.toMatch(/mm|±|14|1[.,]5|12/);
    });
  });

  it('reads — without ≈ or caption when the camera gave no impact point', () => {
    expect(impact({ impact_offset_mm: null, impact_zone: null })).toMatchObject({
      value: NO_VALUE,
      estimated: undefined,
      subtext: undefined,
    });
    // A zone without an offset is no impact point either.
    expect(impact({ impact_offset_mm: undefined, impact_zone: 'toe' }).value).toBe(NO_VALUE);
    expect(impact({ impact_offset_mm: Number.NaN, impact_zone: 'toe' }).value).toBe(NO_VALUE);
  });

  it('reads "calculating" while the club data is pending, and nothing once done', () => {
    setActiveLocale('de');
    expect(impact({ impact_offset_mm: null, club_data_status: 'pending' })).toMatchObject({
      value: NO_VALUE,
      subtext: 'wird berechnet',
    });
    expect(impact({ impact_offset_mm: null, club_data_status: 'done' }).subtext).toBeUndefined();
    // A value already in keeps its caption.
    expect(impact({ impact_offset_mm: 3, impact_zone: 'center', club_data_status: 'pending' }).subtext).toBe(
      'berechnet · unkalibriert'
    );
  });

  it('is one of the calculated tiles, so the club page names ≈', () => {
    expect(CALCULATED_CLUB_METRIC_IDS).toContain('cd_impact');
    expect(hasCalculatedMetric([impact({ impact_offset_mm: 3, impact_zone: 'center' })])).toBe(true);
  });
});

describe('ball page with camera club data', () => {
  it('shows the camera path and attack angle as calculated with ±σ, and no spin axis', () => {
    setActiveLocale('de');
    const metrics = buildLiveMetrics(makeShot(IRON), 'metric', noSwing);
    expect(byId(metrics, 'club_path')).toMatchObject({
      value: '+6,9',
      subtext: '±1,2° · berechnet · unkalibriert',
      estimated: true,
      estimatedLabel: 'berechnet · unkalibriert',
    });
    // No "experimental" pill under the caption: a wrapped caption pushed it 9-13 px out (3c5cd1e).
    expect(byId(metrics, 'club_path').confidence).toBeUndefined();
    // Neither has ground truth yet: "uncalibrated" in the caption and the ≈ label.
    expect(byId(metrics, 'club_aoa')).toMatchObject({
      value: '-10,4',
      subtext: '±0,8° · berechnet · unkalibriert',
      estimatedLabel: 'berechnet · unkalibriert',
    });
    expect(hasCalculatedMetric([byId(metrics, 'club_aoa')])).toBe(true);
    expect(metrics.some((m) => m.id === 'spin_axis')).toBe(false);
  });

  it('keeps a legacy measured path and attack angle before the camera ones', () => {
    const metrics = buildLiveMetrics(
      makeShot({ ...IRON, club_path_deg: 1.5, club_angle_deg: -2 }),
      'imperial',
      noSwing
    );
    // The radar's own values are measured, so no ≈, but have no ground truth either: "not calibrated".
    for (const [id, value] of [['club_path', '+1.5'], ['club_aoa', '-2.0']] as const) {
      expect(byId(metrics, id), id).toMatchObject({
        value,
        unit: '°',
        subtext: 'not calibrated',
        confidence: null,
      });
      expect(byId(metrics, id).estimated, id).toBeUndefined();
      expect(byId(metrics, id).estimatedLabel, id).toBeUndefined();
    }
  });

  it('falls back to the accepted TI experimental values without camera ones', () => {
    const metrics = buildLiveMetrics(
      makeShot({
        ...EMPTY,
        club_path_deg: null,
        club_angle_deg: null,
        experimental_club_path_deg: 2.2,
        experimental_club_path_status: 'accepted',
      }),
      'imperial',
      noSwing
    );
    expect(byId(metrics, 'club_path')).toMatchObject({
      value: '+2.2',
      confidence: 'experimental',
      subtext: 'not calibrated',
    });
    expect(byId(metrics, 'club_path').estimated).toBeUndefined();
  });
});

describe('clubDataSummaryParts (ShotsPanel line)', () => {
  it('de: path and attack only, never the face, face to path, the axis or the impact', () => {
    setActiveLocale('de');
    const parts = clubDataSummaryParts(makeShot(IRON));
    // One "(unkalibriert)" covers both values.
    expect(parts).toEqual(['Pfad +6,9°', 'Eintreffw. -10,4° (unkalibriert)']);
    const line = parts.join(' · ');
    for (const word of UNMEASURABLE_COPY.de) expect(line, word).not.toContain(word);
    expect(line).not.toMatch(/Fläche|12[.,]2|19[.,]1|mm/);
  });

  it('leaves out what the shot does not have, and is empty with only unmeasurable data', () => {
    expect(clubDataSummaryParts(makeShot({ ...EMPTY }))).toEqual([]);
    expect(clubDataSummaryParts(makeShot({ ...EMPTY, ...UNMEASURABLE_CLUB_DATA }))).toEqual([]);
    expect(clubDataSummaryParts(makeShot({ ...EMPTY, camera_club_path_deg: -2.1 }))).toEqual([
      'Path -2.1° (uncalibrated)',
    ]);
    expect(clubDataSummaryParts(makeShot({ ...EMPTY, camera_attack_angle_deg: 3 }))).toEqual([
      'Attack +3.0° (uncalibrated)',
    ]);
  });

  it.each([
    ['en', ['Path +6.9°', 'Attack -10.4° (uncalibrated)']],
    ['es', ['Path +6,9°', 'Ataque -10,4° (sin calibrar)']],
  ] as const)('%s: one "uncalibrated" after the last value covers both', (locale, expected) => {
    setActiveLocale(locale);
    expect(clubDataSummaryParts(makeShot(IRON))).toEqual(expected);
  });
});
