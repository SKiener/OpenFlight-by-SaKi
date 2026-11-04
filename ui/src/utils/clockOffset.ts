import { t } from '../i18n';

/** Offsets up to this are network delay or ordinary drift, not a wrong Pi clock (matches the server). */
export const CLOCK_OFFSET_WARN_S = 120;

/** The client time the UI sends once per Socket.IO connection (`client_clock`). */
export function clientClockPayload(now: Date = new Date()): { epoch_ms: number; tz_offset_min: number } {
  // tz_offset_min is local time minus UTC, e.g. +120 for CEST.
  return { epoch_ms: now.getTime(), tz_offset_min: -now.getTimezoneOffset() };
}

export function isClockOffsetShown(offsetS: number | null | undefined): offsetS is number {
  return typeof offsetS === 'number' && Number.isFinite(offsetS) && Math.abs(offsetS) > CLOCK_OFFSET_WARN_S;
}

/** Signed days/hours/minutes, e.g. `+5 T 17 h 29 min` in German. */
export function formatClockOffset(offsetS: number): string {
  const sign = offsetS < 0 ? '-' : '+';
  const totalMin = Math.floor(Math.abs(offsetS) / 60 + 0.5);
  const days = Math.floor(totalMin / (24 * 60));
  const hours = Math.floor((totalMin % (24 * 60)) / 60);
  const minutes = totalMin % 60;
  const parts: string[] = [];
  if (days) parts.push(`${days} ${t('debug.clockOffset.dayUnit')}`);
  if (days || hours) parts.push(`${hours} h`);
  parts.push(`${minutes} min`);
  return sign + parts.join(' ');
}
