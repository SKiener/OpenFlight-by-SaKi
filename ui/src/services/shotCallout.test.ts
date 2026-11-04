import { describe, expect, it } from 'vitest';
import { t } from '../i18n';
import { buildLiveMetrics } from '../components/panel/liveMetrics';
import type { Shot } from '../types/shot';
import {
  buildShotCallout,
  CALLOUT_LANGUAGES,
  CALLOUT_METRIC_LABEL_KEYS,
  CALLOUT_METRICS,
  getSpeechLang,
  isCalloutLanguage,
  isCalloutMetricId,
  pickCalloutLanguage,
  SAMPLE_CALLOUT_SHOT,
  testCalloutText,
  type CalloutConfig,
} from './shotCallout';

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 120.4,
    club_speed_mph: 87.2,
    smash_factor: 1.381,
    estimated_carry_yards: 165.4,
    carry_range: [158, 172],
    club: '7-iron',
    timestamp: '2026-10-05T12:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 16.54,
    launch_angle_horizontal: 1.2,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: 6234.6,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    carry_spin_adjusted: null,
    carry_spin_rpm: 6234.6,
    carry_spin_source: 'measured',
    ...overrides,
  };
}

const ALL: CalloutConfig = { metrics: CALLOUT_METRICS, language: 'en', unitSystem: 'imperial' };
const GERMAN: CalloutConfig = { ...ALL, language: 'de', unitSystem: 'metric' };

describe('buildShotCallout', () => {
  it('speaks every chosen value in the canonical order, in English and imperial units', () => {
    expect(buildShotCallout(makeShot(), ALL)).toBe(
      'Carry 165 yards, Ball speed 120, Club speed 87, Smash 1.38, Launch 16.5 degrees, Spin 6235'
    );
  });

  it('speaks German with metric units and decimal commas', () => {
    expect(buildShotCallout(makeShot(), GERMAN)).toBe(
      'Carry 151 Meter, Ballgeschwindigkeit 194, Schlägergeschwindigkeit 140, Smash 1,38, Abflugwinkel 16,5 Grad, Spin 6235'
    );
  });

  it('never puts a period after a whole number, which German reads as an ordinal', () => {
    for (const option of CALLOUT_LANGUAGES) {
      const text = buildShotCallout(makeShot({ smash_factor: 1.4 }), { ...GERMAN, language: option.id });
      expect(text, option.id).not.toMatch(/\d\.(\s|$)/);
    }
  });

  it('speaks the distance and speed units of a mixed choice', () => {
    const metrics: CalloutConfig['metrics'] = ['carry', 'ball_speed'];

    expect(buildShotCallout(makeShot(), { ...ALL, metrics, unitSystem: 'mph-meters' })).toBe(
      'Carry 151 meters, Ball speed 120'
    );
    expect(buildShotCallout(makeShot(), { ...ALL, metrics, unitSystem: 'kmh-yards' })).toBe(
      'Carry 165 yards, Ball speed 194'
    );
    expect(buildShotCallout(makeShot(), { ...GERMAN, metrics, unitSystem: 'mph-meters' })).toBe(
      'Carry 151 Meter, Ballgeschwindigkeit 120'
    );
  });

  it('defaults to carry and ball speed', () => {
    expect(buildShotCallout(makeShot(), { ...GERMAN, metrics: ['carry', 'ball_speed'] })).toBe(
      'Carry 151 Meter, Ballgeschwindigkeit 194'
    );
  });

  it('keeps the canonical order whatever order the metrics are stored in', () => {
    expect(buildShotCallout(makeShot(), { ...ALL, metrics: ['spin', 'carry'] })).toBe('Carry 165 yards, Spin 6235');
  });

  it('prefers the spin-adjusted carry, like the Live panel', () => {
    expect(buildShotCallout(makeShot({ carry_spin_adjusted: 171.6 }), { ...ALL, metrics: ['carry'] })).toBe(
      'Carry 172 yards'
    );
  });

  it('skips values the shot does not have', () => {
    const shot = makeShot({
      club_speed_mph: null,
      smash_factor: null,
      launch_angle_vertical: null,
      spin_rpm: null,
      carry_spin_rpm: null,
    });

    expect(buildShotCallout(shot, ALL)).toBe('Carry 165 yards, Ball speed 120');
  });

  it('returns null when none of the chosen values exist', () => {
    const shot = makeShot({ spin_rpm: null, carry_spin_rpm: null, launch_angle_vertical: null });

    expect(buildShotCallout(shot, { ...ALL, metrics: ['spin', 'launch_v'] })).toBeNull();
  });

  it('skips non-finite values instead of speaking "NaN"', () => {
    const shot = makeShot({
      spin_rpm: Number.NaN,
      carry_spin_rpm: Number.NaN,
      smash_factor: Number.POSITIVE_INFINITY,
    });

    expect(buildShotCallout(shot, { ...ALL, metrics: ['smash', 'spin'] })).toBeNull();
  });

  it.each([
    ['typical', { spin_rpm: 1538, spin_confidence: 0.39, carry_spin_rpm: 7500, carry_spin_source: 'club_typical' }],
    ['speed-model', { spin_rpm: 1538, carry_spin_rpm: 7200, carry_spin_source: 'speed_model' }],
    ['calculated', { spin_source: 'calculated', carry_spin_source: 'calculated' }],
    ['raw, without carry spin,', { spin_rpm: 6000, carry_spin_rpm: undefined, carry_spin_source: undefined }],
  ] as const)('says nothing about a %s spin, only about measured spin', (_kind, overrides) => {
    const shot = makeShot(overrides);

    expect(buildShotCallout(shot, { ...ALL, metrics: ['carry', 'spin'] })).toBe('Carry 165 yards');
    expect(buildShotCallout(shot, { ...ALL, metrics: ['spin'] })).toBeNull();
  });

  it('never groups thousands, so a spin of 2800 is not read as 2.8', () => {
    for (const option of CALLOUT_LANGUAGES) {
      const text = buildShotCallout(makeShot({ spin_rpm: 12345, carry_spin_rpm: 12345 }), {
        ...ALL,
        language: option.id,
        metrics: ['spin'],
      });
      expect(text, option.id).toMatch(/ 12345$/);
    }
  });

  it('speaks a negative launch angle with its sign', () => {
    expect(buildShotCallout(makeShot({ launch_angle_vertical: -2.04 }), { ...ALL, metrics: ['launch_v'] })).toBe(
      'Launch -2 degrees'
    );
  });

  it('speaks only the swing speed of a swing-speed rep', () => {
    const rep = makeShot({ club: 'Swing Speed', club_speed_mph: 101.6, ball_speed_mph: 0 });

    expect(buildShotCallout(rep, ALL)).toBe('Swing speed 102');
    expect(buildShotCallout(rep, GERMAN)).toBe('Schwunggeschwindigkeit 164');
  });

  it('has phrases for every language', () => {
    for (const option of CALLOUT_LANGUAGES) {
      const text = buildShotCallout(SAMPLE_CALLOUT_SHOT, { ...ALL, language: option.id });
      expect(text, option.id).toBeTruthy();
      expect(text, option.id).not.toContain('undefined');
    }
  });
});

describe('testCalloutText', () => {
  it('speaks the sample before the first shot', () => {
    expect(testCalloutText(null, ALL)).toBe(
      'Carry 165 yards, Ball speed 120, Club speed 87, Smash 1.38, Launch 16.5 degrees, Spin 6200'
    );
  });

  it('speaks the latest shot as it would be called out', () => {
    expect(testCalloutText(makeShot({ estimated_carry_yards: 201 }), { ...ALL, metrics: ['carry'] })).toBe(
      'Carry 201 yards'
    );
  });

  it('falls back to the sample when the latest shot has none of the chosen values', () => {
    expect(testCalloutText(makeShot({ spin_rpm: null, carry_spin_rpm: null }), { ...ALL, metrics: ['spin'] })).toBe(
      'Spin 6200'
    );
    const typical = makeShot({ carry_spin_rpm: 7500, carry_spin_source: 'club_typical' });
    expect(testCalloutText(typical, { ...ALL, metrics: ['spin'] })).toBe('Spin 6200');
  });

  it('demonstrates spin with a measured sample, so the test button can say it', () => {
    expect(SAMPLE_CALLOUT_SHOT.carry_spin_source).toBe('measured');
    expect(buildShotCallout(SAMPLE_CALLOUT_SHOT, { ...ALL, metrics: ['spin'] })).toBe('Spin 6200');
  });
});

describe('callout metrics', () => {
  it('label each chip like the Live tile with the same id', () => {
    const live = buildLiveMetrics(makeShot(), 'imperial', {
      count: 0,
      last_speed_mph: 0,
      best_speed_mph: 0,
      avg_speed_mph: 0,
    });

    for (const metric of CALLOUT_METRICS) {
      const tile = live.find((entry) => entry.id === metric);
      expect(tile, metric).toBeDefined();
      expect(t(CALLOUT_METRIC_LABEL_KEYS[metric]), metric).toBe(tile?.label);
    }
  });
});

describe('callout languages', () => {
  it('offers German, English and Spanish', () => {
    expect(CALLOUT_LANGUAGES.map((option) => option.id)).toEqual(['de', 'en', 'es']);
  });

  it('maps each language to a speech tag', () => {
    expect(getSpeechLang('de')).toBe('de-DE');
    expect(getSpeechLang('es')).toBe('es-ES');
  });

  it('picks the first preferred language that has phrases', () => {
    expect(pickCalloutLanguage(['de-DE', 'en-US'])).toBe('de');
    expect(pickCalloutLanguage(['ja-JP', 'ES-mx'])).toBe('es');
    expect(pickCalloutLanguage(['es'])).toBe('es');
  });

  it('falls back to English', () => {
    expect(pickCalloutLanguage([])).toBe('en');
    expect(pickCalloutLanguage(['ja-JP', ''])).toBe('en');
  });

  it('no longer picks French or Portuguese, but still reaches a later language that has phrases', () => {
    expect(pickCalloutLanguage(['fr-FR'])).toBe('en');
    expect(pickCalloutLanguage(['pt-BR', 'fr-CA'])).toBe('en');
    expect(pickCalloutLanguage(['fr-FR', 'de-DE'])).toBe('de');
  });

  it('validates stored ids', () => {
    expect(isCalloutLanguage('de')).toBe(true);
    expect(isCalloutLanguage('it')).toBe(false);
    expect(isCalloutLanguage('fr')).toBe(false);
    expect(isCalloutLanguage('pt')).toBe(false);
    expect(isCalloutLanguage(3)).toBe(false);
    expect(isCalloutMetricId('carry')).toBe(true);
    expect(isCalloutMetricId('spin_axis')).toBe(false);
    expect(isCalloutMetricId(null)).toBe(false);
  });
});
