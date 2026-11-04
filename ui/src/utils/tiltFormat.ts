import { getHtmlLang, t, type MessageKey } from '../i18n';
import type { TiltCalibrationStatus } from '../types/tiltCalibration';
import { LEVEL_DEG } from './phoneTilt';
import { formatNumber } from './units';

/** One decimal in the active locale ("10,4" in German). */
export function formatTiltDeg(value: number): string {
  return formatNumber(value, 1);
}

/**
 * The tilt the shots fly with. Without the LIS3DH it is the configured tilt. With it, the last
 * shot's effective tilt, unless a phone calibration was applied after that shot (then the
 * radar tilt it measured, which was effective at that moment).
 */
export function shownTiltDeg(status: TiltCalibrationStatus): number | null {
  const record = status.calibration;
  const last = status.last_effective_iwr_tilt_deg;
  if (status.inclinometer_enabled && last !== null && last !== undefined) {
    const lastAt = Date.parse(status.last_effective_at ?? '');
    const appliedAt = Date.parse(record?.applied_at ?? '');
    const staleAfterCalibration =
      status.tilt_source === 'phone' &&
      record &&
      Number.isFinite(appliedAt) &&
      (!Number.isFinite(lastAt) || lastAt < appliedAt);
    if (!staleAfterCalibration) return last;
  }
  if (status.tilt_source === 'phone' && record) return record.radar_tilt_deg;
  return status.configured_iwr_tilt_deg;
}

const RELATIVE_UNITS: ReadonlyArray<[Intl.RelativeTimeFormatUnit, number]> = [
  ['minute', 60],
  ['hour', 60 * 60],
  ['day', 24 * 60 * 60],
  ['month', 30 * 24 * 60 * 60],
  ['year', 365 * 24 * 60 * 60],
];

/** "vor 5 Minuten", "gestern", "vor 3 Tagen"; "gerade eben" under a minute or for a time ahead of `nowMs`. */
export function formatRelativeTime(iso: string, nowMs: number): string | null {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return null;
  const ageS = Math.max(0, (nowMs - at) / 1000);
  if (ageS < 60) return t('tilt.justNow');
  let unit = RELATIVE_UNITS[0];
  for (const candidate of RELATIVE_UNITS) {
    if (ageS >= candidate[1]) unit = candidate;
  }
  const format = new Intl.RelativeTimeFormat(getHtmlLang(), { numeric: 'auto' });
  return format.format(-Math.floor(ageS / unit[1]), unit[0]);
}

function sourceLabel(status: TiltCalibrationStatus, nowMs: number): string {
  if (status.tilt_source === 'phone') {
    const record = status.calibration;
    // measured_at is the phone's clock, which is usually right; applied_at is the Pi's.
    const when = record
      ? (formatRelativeTime(record.measured_at, nowMs) ?? formatRelativeTime(record.applied_at, nowMs))
      : null;
    return t('tilt.sourcePhone', { when: when ?? '' }).trim();
  }
  if (status.tilt_source === 'cli') return t('tilt.sourceCli');
  return t('tilt.sourceCalibrationJson');
}

/** "10,4° · per Handy gemessen vor 3 Tagen", "TI nicht aktiv", or the loading text before the first status. */
export function tiltStatusLine(status: TiltCalibrationStatus | null, nowMs: number): string {
  if (!status) return t('tilt.loading');
  if (!status.enabled) return t('tilt.notActive');
  const tilt = shownTiltDeg(status);
  if (tilt === null) return t('tilt.notActive');
  return t('tilt.status', { tilt: formatTiltDeg(tilt), source: sourceLabel(status, nowMs) });
}

/** "front 1,2° higher / lower" or "level" for a case tilt (positive: ball side higher). */
export function caseTiltKey(
  caseTiltDeg: number,
  keys: { higher: MessageKey; lower: MessageKey; level: MessageKey }
): MessageKey {
  if (Math.abs(caseTiltDeg) < LEVEL_DEG) return keys.level;
  return caseTiltDeg > 0 ? keys.higher : keys.lower;
}

/** The secondary lines under the status: what the phone measured, and the LIS3DH difference. */
export function tiltDetailLines(status: TiltCalibrationStatus | null): string[] {
  const record = status?.enabled && status.tilt_source === 'phone' ? status.calibration : null;
  if (!record) return [];
  const lines = [
    t(
      caseTiltKey(record.case_tilt_deg, {
        higher: 'tilt.caseHigher',
        lower: 'tilt.caseLower',
        level: 'tilt.caseLevel',
      }),
      {
        value: formatTiltDeg(Math.abs(record.case_tilt_deg)),
      }
    ),
  ];
  const diff = record.inclinometer_minus_phone_deg;
  if (diff !== null && diff !== undefined) lines.push(t('tilt.inclinometerDiff', { value: formatTiltDeg(diff) }));
  return lines;
}

/** "Gerät vorne 1,2° höher → Radar-Neigung 11,2°" after a successful measurement. */
export function tiltResultLine(caseTiltDeg: number, radarTiltDeg: number): string {
  return t(
    caseTiltKey(caseTiltDeg, { higher: 'tilt.resultHigher', lower: 'tilt.resultLower', level: 'tilt.resultLevel' }),
    {
      case: formatTiltDeg(Math.abs(caseTiltDeg)),
      radar: formatTiltDeg(radarTiltDeg),
    }
  );
}

const ERROR_CODES = new Set([
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
]);

/** One short sentence per server reason code; unknown codes name the code. */
export function tiltErrorMessage(code: string): string {
  return ERROR_CODES.has(code) ? t(`tilt.error.${code}` as MessageKey) : t('tilt.error.unknown', { code });
}
