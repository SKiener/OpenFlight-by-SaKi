import { t } from '../i18n';
import type { WeatherFetchStatus } from '../stores/useWeatherStore';
import type { WeatherReadingState, WeatherState } from '../types/weather';
import { formatNumber } from './units';

/** The reading's age now: its age when the state arrived plus the time since (monotonic ms). */
export function readingAgeS(reading: WeatherReadingState, receivedAt: number, now: number): number {
  return reading.age_s + Math.max(0, now - receivedAt) / 1000;
}

/** The reading the shots fly with, or null when there is none or it is older than the server's limit. */
export function freshReading(state: WeatherState | null, receivedAt: number, now: number): WeatherReadingState | null {
  const reading = state?.reading;
  if (!state || !reading) return null;
  return readingAgeS(reading, receivedAt, now) <= state.fresh_max_s ? reading : null;
}

/**
 * Whether a failed fetch keeps the manual weather form open. It opens on the first failure while
 * no weather is fresh and stays open through the background retries ('fetching' between two
 * failures), so what was typed is not lost; fresh weather (typed or fetched) closes it.
 */
export function manualFormAutoOpen(
  wasOpen: boolean,
  fetchStatus: WeatherFetchStatus,
  hasFreshReading: boolean
): boolean {
  if (hasFreshReading) return false;
  return wasOpen || fetchStatus === 'failed';
}

export function formatWeatherAge(ageS: number): string {
  const minutes = Math.floor(ageS / 60);
  if (minutes < 1) return t('weather.ageJustNow');
  if (minutes < 60) return t('weather.ageMinutes', { minutes });
  return t('weather.ageHours', { hours: Math.floor(minutes / 60), minutes: minutes % 60 });
}

/**
 * "Weather: 12 °C · 1008 hPa · ρ 1.23 · 5 min ago · Open-Meteo", or the fixed density
 * ("Weather: standard 1.184") when there is no fresh reading.
 */
export function weatherStatusLine(state: WeatherState | null, receivedAt: number, now: number): string {
  const reading = freshReading(state, receivedAt, now);
  if (!reading) {
    return t('weather.standard', { density: formatNumber(state?.standard_density_kg_m3 ?? 1.184, 3) });
  }
  return t('weather.status', {
    temperature: formatNumber(reading.temperature_c, 0),
    pressure: formatNumber(reading.pressure_hpa, 0),
    density: formatNumber(reading.air_density_kg_m3, 2),
    age: formatWeatherAge(readingAgeS(reading, receivedAt, now)),
    source: weatherSourceLabel(reading),
  });
}

/** "Open-Meteo", "Open-Meteo (Mobilfunk) · Barometer iPhone" or "manuell". */
export function weatherSourceLabel(reading: WeatherReadingState): string {
  if (reading.source !== 'open-meteo') return t('weather.sourceManual');
  const parts = [t(reading.transport === 'cellular' ? 'weather.sourceOpenMeteoCellular' : 'weather.sourceOpenMeteo')];
  if (reading.pressure_source === 'phone-barometer') parts.push(t('weather.sourcePhoneBarometer'));
  return parts.join(' · ');
}

/** A typed number with either decimal separator ("12,5" or "12.5"); null when it is not one. */
export function parseDecimal(text: string): number | null {
  const cleaned = text.trim().replace(',', '.');
  if (!/^[-+−]?\d+(\.\d+)?$/.test(cleaned)) return null;
  return Number(cleaned.replace('−', '-'));
}

export function locationLabel(location: { name: string; admin1?: string; country?: string }): string {
  return [location.name, location.admin1, location.country].filter((part) => part).join(', ');
}
