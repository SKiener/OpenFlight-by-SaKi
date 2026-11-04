import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { makeShot } from '../../test-utils/shot';
import type { LiveSlotId } from '../../stores/useLiveSlotsStore';
import {
  buildKioskTiles,
  formatKioskAngle,
  formatKioskDirection,
  formatKioskSmash,
  formatKioskSpin,
  needsTargetLine,
} from './kioskLiveMetrics';

const ALL: LiveSlotId[] = [
  'carry',
  'total',
  'apex',
  'ball_speed',
  'club_speed',
  'smash',
  'launch_v',
  'launch_h',
  'curve',
  'spin',
  'club_path',
  'club_aoa',
];
const tiles = (shot = makeShot(), ids: LiveSlotId[] = ALL, options = { directionCalibrated: true }) =>
  Object.fromEntries(buildKioskTiles(shot, ids, 'metric', options).map((tile) => [tile.id, tile]));

afterEach(() => setActiveLocale('en'));

describe('kiosk formatters', () => {
  it('rounds angles to whole degrees with °, signed when asked, never "-0"', () => {
    expect(formatKioskAngle(16.4)).toBe('16°');
    expect(formatKioskAngle(16.5)).toBe('17°');
    expect(formatKioskAngle(-2.4)).toBe('-2°');
    expect(formatKioskAngle(-0.4)).toBe('0°');
    expect(formatKioskAngle(2.6, true)).toBe('+3°');
    expect(formatKioskAngle(-4.3, true)).toBe('-4°');
    expect(formatKioskAngle(0.2, true)).toBe('0°');
    expect(formatKioskAngle(null)).toBe('—');
    expect(formatKioskAngle(Number.NaN)).toBe('—');
  });

  it('names the start direction’s side: + is right', () => {
    setActiveLocale('de');
    expect(formatKioskDirection(1.6)).toBe('2° R');
    expect(formatKioskDirection(-3.2)).toBe('3° L');
    expect(formatKioskDirection(-12.2)).toBe('12° L');
    expect(formatKioskDirection(0.5)).toBe('1° R');
    expect(formatKioskDirection(-0.5)).toBe('1° L');
  });

  it('reads 0° under half a degree either way, and — without a direction', () => {
    expect(formatKioskDirection(0.49)).toBe('0°');
    expect(formatKioskDirection(-0.49)).toBe('0°');
    expect(formatKioskDirection(0)).toBe('0°');
    expect(formatKioskDirection(null)).toBe('—');
  });

  it('uses the language’s side letters', () => {
    setActiveLocale('es');
    expect(formatKioskDirection(2)).toBe('2° D');
    expect(formatKioskDirection(-2)).toBe('2° I');
    setActiveLocale('en');
    expect(formatKioskDirection(2)).toBe('2° R');
    expect(formatKioskDirection(-2)).toBe('2° L');
  });

  it('gives spin whole, with the language’s thousands separator', () => {
    expect(formatKioskSpin(10000)).toBe('10,000');
    expect(formatKioskSpin(6512.6)).toBe('6,513');
    setActiveLocale('de');
    expect(formatKioskSpin(10000)).toBe('10.000');
    expect(formatKioskSpin(null)).toBe('—');
  });

  it('keeps two decimals for smash', () => {
    expect(formatKioskSmash(1.448)).toBe('1.45');
    setActiveLocale('de');
    expect(formatKioskSmash(1.448)).toBe('1,45');
    expect(formatKioskSmash(1.5)).toBe('1,50');
    expect(formatKioskSmash(null)).toBe('—');
  });
});

describe('buildKioskTiles', () => {
  it('returns the tiles in slot order with short labels and units in the title', () => {
    setActiveLocale('de');
    const list = buildKioskTiles(makeShot(), ['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve'], 'metric');
    expect(list.map((tile) => [tile.label, tile.unit])).toEqual([
      ['Flugweite', 'm'],
      ['Balltempo', 'km/h'],
      ['Abflug', undefined],
      ['Richtung', undefined],
      ['Kurve', undefined],
    ]);
  });

  it('shows whole numbers: speeds, carry and total in the unit system', () => {
    const shot = makeShot({ ball_speed_mph: 120.2, club_speed_mph: 86.4, carry_spin_adjusted: 166, total_yards: 174 });
    const metric = tiles(shot);
    expect(metric.ball_speed.value).toBe('193');
    expect(metric.club_speed.value).toBe('139');
    expect(metric.carry.value).toBe('152');
    expect(metric.total.value).toBe('159');
    const imperial = Object.fromEntries(buildKioskTiles(shot, ALL, 'imperial').map((tile) => [tile.id, tile]));
    expect(imperial.ball_speed).toMatchObject({ value: '120', unit: 'mph' });
    expect(imperial.carry).toMatchObject({ value: '166', unit: 'yds' });
  });

  it('formats every other value for the kiosk', () => {
    setActiveLocale('de');
    const metric = tiles(
      makeShot({
        spin_rpm: 10000,
        carry_spin_rpm: 10000,
        launch_angle_horizontal: -12.2,
        curve_class: 'straight',
        smash_factor: 1.448,
      })
    );
    expect(metric.spin).toMatchObject({ value: '10.000', unit: 'rpm' });
    expect(metric.launch_h.value).toBe('12° L');
    expect(metric.launch_v.value).toBe('13°');
    expect(metric.smash.value).toBe('1,45');
    expect(metric.curve).toMatchObject({ value: 'Gerade', kind: 'class' });
    expect(metric.club_path.value).toBe('-1°');
    expect(metric.club_aoa.value).toBe('+2°');
  });

  it('names the curve class in the active language, — without one', () => {
    for (const [cls, en, de] of [
      ['hook', 'Hook', 'Hook'],
      ['draw', 'Draw', 'Draw'],
      ['straight', 'Straight', 'Gerade'],
      ['fade', 'Fade', 'Fade'],
      ['slice', 'Slice', 'Slice'],
    ] as const) {
      setActiveLocale('en');
      expect(tiles(makeShot({ curve_class: cls })).curve.value).toBe(en);
      setActiveLocale('de');
      expect(tiles(makeShot({ curve_class: cls })).curve.value).toBe(de);
    }
    expect(tiles(makeShot({ curve_class: null })).curve).toMatchObject({ value: '—', approx: false });
    expect(tiles(makeShot({ curve_class: 'wobble' as never })).curve.value).toBe('—');
  });

  it('shows — for missing values', () => {
    const metric = tiles(
      makeShot({
        club_speed_mph: null,
        smash_factor: null,
        launch_angle_vertical: null,
        launch_angle_horizontal: null,
        club_angle_deg: null,
        club_path_deg: null,
        carry_spin_rpm: null,
        spin_rpm: null,
      })
    );
    for (const id of ['club_speed', 'smash', 'launch_v', 'launch_h', 'club_path', 'club_aoa', 'spin'] as const) {
      expect(metric[id].value, id).toBe('—');
      expect(metric[id].approx, id).toBe(false);
    }
  });

  it('shows the total only once the final ballistics gave one', () => {
    expect(tiles(makeShot({ carry_spin_adjusted: null, total_yards: 174 })).total.value).toBe('—');
    expect(tiles(makeShot({ total_yards: null })).total.value).toBe('—');
    expect(tiles(makeShot({ total_yards: Number.NaN })).total.value).toBe('—');
  });

  it('takes the camera club path and attack angle when the radar has none, as the Live tile', () => {
    const metric = tiles(
      makeShot({ club_path_deg: null, club_angle_deg: null, camera_club_path_deg: 3.4, camera_attack_angle_deg: -8.6 })
    );
    expect(metric.club_path).toMatchObject({ value: '+3°', approx: true });
    expect(metric.club_aoa).toMatchObject({ value: '-9°', approx: true });
  });
});

describe('the kiosk ≈ rule', () => {
  it('marks nothing on a measured, final, calibrated shot but the calculated curve and the uncalibrated club values', () => {
    const metric = tiles(makeShot({ curve_class: 'draw' }));
    const marked = Object.values(metric)
      .filter((tile) => tile.approx)
      .map((tile) => tile.id);
    expect(marked).toEqual(['curve', 'club_path', 'club_aoa']);
  });

  it('marks the radar club path and attack angle ≈ ("not calibrated", as the start direction), an empty one not at all', () => {
    const radar = tiles(makeShot({ club_path_deg: 2.6, club_angle_deg: -4.4 }));
    expect(radar.club_path).toMatchObject({ value: '+3°', approx: true });
    expect(radar.club_aoa).toMatchObject({ value: '-4°', approx: true });
    const experimental = tiles(
      makeShot({
        club_path_deg: null,
        club_angle_deg: null,
        experimental_club_path_deg: 1.2,
        experimental_club_path_status: 'accepted',
      })
    );
    expect(experimental.club_path).toMatchObject({ value: '+1°', approx: true });
    expect(experimental.club_aoa).toMatchObject({ value: '—', approx: false });
  });

  it('marks a provisional carry and a typical-spin carry and total', () => {
    expect(tiles(makeShot({ carry_spin_adjusted: null })).carry.approx).toBe(true);
    const typical = tiles(makeShot({ carry_spin_source: 'club_typical', carry_spin_rpm: 7500, total_yards: 230 }));
    expect(typical.carry.approx).toBe(true);
    expect(typical.total.approx).toBe(true);
    // The value itself carries no ≈ (it moves to the title).
    expect(typical.carry.value).not.toContain('≈');
  });

  it('marks a spin that was not measured', () => {
    expect(tiles(makeShot({ carry_spin_source: 'club_typical', carry_spin_rpm: 7500 })).spin.approx).toBe(true);
    expect(tiles(makeShot({ carry_spin_source: 'calculated', carry_spin_rpm: 7500 })).spin.approx).toBe(true);
    expect(tiles(makeShot()).spin.approx).toBe(false);
  });

  it('marks estimated launch angles', () => {
    const metric = tiles(makeShot({ angle_source: 'estimated' }));
    expect(metric.launch_v.approx).toBe(true);
    expect(metric.launch_h.approx).toBe(true);
  });

  it('marks an uncalibrated radar start direction, not a camera one', () => {
    const uncalibrated = { directionCalibrated: false };
    expect(tiles(makeShot({ launch_angle_horizontal_source: 'radar' }), ALL, uncalibrated).launch_h.approx).toBe(true);
    expect(tiles(makeShot({ launch_angle_horizontal_source: 'radar' })).launch_h.approx).toBe(false);
    expect(
      tiles(makeShot({ launch_angle_horizontal_source: 'camera_assisted_experimental' }), ALL, uncalibrated).launch_h
        .approx
    ).toBe(false);
  });

  it('marks no ball speed, club speed or smash for estimated angles or a provisional carry', () => {
    const metric = tiles(makeShot({ angle_source: 'estimated', carry_spin_adjusted: null }));
    expect([metric.ball_speed.approx, metric.club_speed.approx, metric.smash.approx]).toEqual([false, false, false]);
  });

  it('marks an uncertain ball speed, its smash and carry, as the Live tiles; never the club speed', () => {
    const metric = tiles(makeShot({ ball_speed_status: 'uncertain', ball_speed_status_reason: 'club_like_track' }));
    expect([metric.ball_speed.approx, metric.smash.approx, metric.carry.approx]).toEqual([true, true, true]);
    expect(metric.club_speed.approx).toBe(false);
    // The value itself stays a plain number: the ≈ goes to the title.
    expect(metric.ball_speed.value).toBe('148');
  });

  it('gives a replaced ball speed no mark (the guard found the ball)', () => {
    const metric = tiles(makeShot({ ball_speed_status: null, ball_speed_status_reason: 'club_like_track' }));
    expect([metric.ball_speed.approx, metric.smash.approx, metric.carry.approx]).toEqual([false, false, false]);
  });
});

describe('the apex tile', () => {
  const apex = (overrides = {}, units: 'metric' | 'imperial' = 'metric') =>
    buildKioskTiles(makeShot(overrides), ['apex'], units)[0];

  it('reads like the others: "Apex m", a whole number', () => {
    for (const locale of ['de', 'en', 'es'] as const) {
      setActiveLocale(locale);
      // 31.6 yd = 28.9 m.
      expect(apex({ apex_yards: 31.6 }), locale).toEqual({ id: 'apex', label: 'Apex', unit: 'm', value: '29', approx: true });
    }
    expect(apex({ apex_yards: 31.6 }, 'imperial')).toMatchObject({ value: '32', unit: 'yds' });
  });

  it('is ≈ as on its All values tile: a model value, after a measured or a typical spin', () => {
    expect(apex({ apex_yards: 30 }).approx).toBe(true);
    const typical = apex({ apex_yards: 30, carry_spin_source: 'club_typical', carry_spin_rpm: 7500 });
    expect(typical).toMatchObject({ value: '27', approx: true });
    // The value itself carries no ≈ (it moves to the title).
    expect(typical.value).not.toContain('≈');
  });

  it('shows — without an apex, and no ≈', () => {
    for (const apexYards of [null, undefined, Number.NaN]) {
      expect(apex({ apex_yards: apexYards }), String(apexYards)).toMatchObject({ value: '—', approx: false });
    }
  });
});

describe('the impact tile', () => {
  const impact = (overrides = {}) => buildKioskTiles(makeShot(overrides), ['impact'], 'metric')[0];

  it('is titled Treffpunkt / Impact / Impacto, always with ≈, and has no unit', () => {
    for (const [locale, label] of [
      ['de', 'Treffpunkt'],
      ['en', 'Impact'],
      ['es', 'Impacto'],
    ] as const) {
      setActiveLocale(locale);
      expect(impact({ impact_offset_mm: 3, impact_zone: 'center' }), locale).toMatchObject({
        id: 'impact',
        label,
        kind: 'impact',
        approx: true,
      });
      expect(impact({ impact_offset_mm: 3 }).unit, locale).toBeUndefined();
      // ≈ even without a value: the tile shows a calculated, uncalibrated camera value.
      expect(impact({ impact_offset_mm: null }).approx, locale).toBe(true);
    }
  });

  it('names the zone, "uncertain" when it is open and — without an offset; never millimetres', () => {
    setActiveLocale('de');
    expect(impact({ impact_offset_mm: 14.2, impact_zone: 'toe' }).value).toBe('Spitze');
    expect(impact({ impact_offset_mm: -1, impact_zone: 'center' }).value).toBe('Mitte');
    expect(impact({ impact_offset_mm: -13, impact_zone: 'heel' }).value).toBe('Ferse');
    expect(impact({ impact_offset_mm: 6.5, impact_zone: null }).value).toBe('unsicher');
    expect(impact({ impact_offset_mm: null, impact_zone: null }).value).toBe('—');
    setActiveLocale('en');
    expect(impact({ impact_offset_mm: 6.5, impact_zone: null }).value).toBe('uncertain');
    setActiveLocale('es');
    expect(impact({ impact_offset_mm: 14.2, impact_zone: 'toe' }).value).toBe('Punta');
    expect(impact({ impact_offset_mm: 6.5, impact_zone: null }).value).toBe('incierto');
    expect(JSON.stringify(impact({ impact_offset_mm: 14.2, impact_zone: 'toe' })).match(/mm/)).toBeNull();
  });

  it('carries what the face draws: club, offset, height and zone', () => {
    expect(
      impact({ club: 'driver', impact_offset_mm: -4, impact_height_mm: 33, impact_zone: 'center' }).face
    ).toEqual({ club: 'driver', offsetMm: -4, heightMm: 33, zone: 'center' });
    expect(impact({ impact_offset_mm: null }).face).toMatchObject({ offsetMm: null });
  });
});

describe('needsTargetLine ("No target")', () => {
  const camera = makeShot({ launch_angle_horizontal_source: 'camera_assisted_experimental' });
  it('asks for a target when the camera measured the direction and none is set', () => {
    expect(needsTargetLine(camera, { target_line: null })).toBe(true);
    expect(
      needsTargetLine(makeShot({ launch_angle_horizontal_source: 'camera_only_experimental' }), { target_line: null })
    ).toBe(true);
  });

  it('stays quiet with a target, for a radar direction, and before the camera settings arrive', () => {
    const target = {
      frame_x_px: 1,
      frame_y_px: 1,
      preview_x_pct: 50,
      preview_y_pct: 50,
      azimuth_deg: 0,
      elevation_deg: 0,
      set_at: 1,
    };
    expect(needsTargetLine(camera, { target_line: target })).toBe(false);
    expect(needsTargetLine(makeShot({ launch_angle_horizontal_source: 'radar' }), { target_line: null })).toBe(false);
    expect(needsTargetLine(makeShot({ launch_angle_horizontal_source: null }), { target_line: null })).toBe(false);
    expect(needsTargetLine(camera, {})).toBe(false);
  });
});
