import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { cliTiltStatus, phoneTiltRecord, TILT_NOW_MS, tiltStatus } from '../test-utils/tiltStatus';
import {
  formatRelativeTime,
  shownTiltDeg,
  tiltDetailLines,
  tiltErrorMessage,
  tiltResultLine,
  tiltStatusLine,
} from './tiltFormat';

describe('tilt status line', () => {
  afterEach(() => setActiveLocale('en'));

  it('names the phone measurement and how long ago, with the German decimal comma', () => {
    setActiveLocale('de');
    expect(tiltStatusLine(tiltStatus(), TILT_NOW_MS)).toBe('11,2° · per Handy gemessen vor 3 Tagen');
    setActiveLocale('en');
    expect(tiltStatusLine(tiltStatus(), TILT_NOW_MS)).toBe('11.2° · measured by phone 3 days ago');
  });

  it('names the start script and the calibration file', () => {
    setActiveLocale('de');
    expect(tiltStatusLine(cliTiltStatus(), TILT_NOW_MS)).toBe('10,4° · fest (Startskript)');
    expect(tiltStatusLine(cliTiltStatus({ tilt_source: 'calibration_json' }), TILT_NOW_MS)).toBe(
      '10,4° · Kalibrierdatei'
    );
  });

  it('says when the TI radar is off, and before the first status arrived', () => {
    setActiveLocale('de');
    expect(
      tiltStatusLine(tiltStatus({ enabled: false, tilt_source: null, configured_iwr_tilt_deg: null }), TILT_NOW_MS)
    ).toBe('TI nicht aktiv');
    expect(tiltStatusLine(null, TILT_NOW_MS)).toBe('Wird geladen…');
  });

  it('dates "just now", yesterday and minutes in the active language', () => {
    setActiveLocale('de');
    const now = Date.parse('2026-10-30T12:00:00Z');
    expect(formatRelativeTime('2026-10-30T11:59:30Z', now)).toBe('gerade eben');
    // A phone clock slightly ahead of this device is still "just now".
    expect(formatRelativeTime('2026-10-30T12:05:00Z', now)).toBe('gerade eben');
    expect(formatRelativeTime('2026-10-30T11:55:00Z', now)).toBe('vor 5 Minuten');
    expect(formatRelativeTime('2026-10-29T11:00:00Z', now)).toBe('gestern');
    expect(formatRelativeTime('not a date', now)).toBeNull();
    setActiveLocale('es');
    expect(formatRelativeTime('2026-10-27T11:00:00Z', now)).toBe('hace 3 días');
  });

  it('falls back to the Pi apply time when the phone time is unreadable', () => {
    const status = tiltStatus({
      calibration: phoneTiltRecord({ measured_at: '?', applied_at: '2026-10-29T12:00:00Z' }),
    });
    expect(tiltStatusLine(status, TILT_NOW_MS)).toBe('11.2° · measured by phone yesterday');
  });
});

describe('shown tilt', () => {
  it('is the configured tilt without the LIS3DH', () => {
    expect(
      shownTiltDeg(cliTiltStatus({ last_effective_iwr_tilt_deg: 12, last_effective_at: '2026-10-30T10:00:00Z' }))
    ).toBe(10.405);
  });

  it("is the last shot's effective tilt with the LIS3DH", () => {
    const status = tiltStatus({ last_effective_iwr_tilt_deg: 11.6, last_effective_at: '2026-10-28T10:00:00Z' });
    expect(shownTiltDeg(status)).toBe(11.6);
  });

  it('is the measured radar tilt while the last shot is older than the phone calibration', () => {
    const status = tiltStatus({ last_effective_iwr_tilt_deg: 10.1, last_effective_at: '2026-10-26T10:00:00Z' });
    expect(shownTiltDeg(status)).toBe(11.24);
    expect(shownTiltDeg(tiltStatus())).toBe(11.24);
  });
});

describe('tilt detail lines', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows what the phone measured and how far the tilt sensor differs', () => {
    setActiveLocale('de');
    expect(tiltDetailLines(tiltStatus())).toEqual([
      'Gerät gemessen: vorne 1,2° höher',
      'Neigungssensor weicht 0,2° ab',
    ]);
    const lower = tiltStatus({
      calibration: phoneTiltRecord({ case_tilt_deg: -0.66, inclinometer_minus_phone_deg: null }),
    });
    expect(tiltDetailLines(lower)).toEqual(['Gerät gemessen: vorne 0,7° tiefer']);
    const level = tiltStatus({
      calibration: phoneTiltRecord({ case_tilt_deg: 0.02, inclinometer_minus_phone_deg: 0 }),
    });
    expect(tiltDetailLines(level)).toEqual(['Gerät gemessen: waagerecht', 'Neigungssensor weicht 0,0° ab']);
  });

  it('shows nothing without a phone calibration in use', () => {
    expect(tiltDetailLines(null)).toEqual([]);
    expect(tiltDetailLines(cliTiltStatus())).toEqual([]);
    expect(tiltDetailLines(tiltStatus({ enabled: false }))).toEqual([]);
  });
});

describe('tilt result and errors', () => {
  afterEach(() => setActiveLocale('en'));

  it('states the measured case tilt and the radar tilt', () => {
    setActiveLocale('de');
    expect(tiltResultLine(1.24, 11.24)).toBe('Gerät vorne 1,2° höher → Radar-Neigung 11,2°');
    expect(tiltResultLine(-0.5, 9.5)).toBe('Gerät vorne 0,5° tiefer → Radar-Neigung 9,5°');
    expect(tiltResultLine(0, 10)).toBe('Gerät waagerecht → Radar-Neigung 10,0°');
  });

  it('has one plain sentence per server reason code', () => {
    setActiveLocale('de');
    expect(tiltErrorMessage('phone_bias_out_of_range')).toBe(
      'Die zwei Messungen passen nicht zusammen – Handy wirklich um 180° gedreht?'
    );
    const codes = [
      'too_few_samples',
      'phone_unstable',
      'gravity_magnitude_out_of_range',
      'phone_not_flat',
      'gravity_sign_inverted',
      'phone_bias_out_of_range',
      'positions_not_opposite',
      'case_tilt_out_of_range',
      'configured_tilt_out_of_range',
      'invalid_payload',
      'iwr6843_not_enabled',
      'inclinometer_unstable',
      'inclinometer_error',
      'pairing_required',
      'network',
    ];
    const messages = codes.map(tiltErrorMessage);
    expect(new Set(messages).size).toBe(codes.length);
    for (const message of messages) expect(message).not.toMatch(/tilt\.error|\{/);
    expect(tiltErrorMessage('save_failed')).toBe('Das Gerät hat die Messung nicht übernommen (save_failed).');
  });
});
