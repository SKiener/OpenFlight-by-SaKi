import type { MessageKey } from '../i18n';
import type { Shot } from '../types/shot';
import { getCarryYards, getDisplayedSpin, getSwingSpeedMph, isSwingSpeedShot } from '../types/shot';
import { convertDistanceFromYards, convertSpeedFromMph, getDistanceUnit, type UnitSystem } from '../utils/units';

/**
 * Spoken shot summary ("Carry 151 meters, Ball speed 193"). The phrases have
 * their own language list, separate from the UI catalogs: the voice is heard,
 * not read, so it follows the voice language chosen in the menu.
 *
 * Parts are joined with commas and there is no final period: in German a
 * whole number followed by a period is an ordinal ("194. Schläger" reads as
 * "194th club").
 */

/**
 * The spoken order. The ids are stored in the settings, so do not rename them
 * without a migration; they reuse the Live panel's metric ids for clarity.
 */
export const CALLOUT_METRICS = ['carry', 'ball_speed', 'club_speed', 'smash', 'launch_v', 'spin'] as const;
export type CalloutMetricId = (typeof CALLOUT_METRICS)[number];
export const DEFAULT_CALLOUT_METRICS: readonly CalloutMetricId[] = ['carry', 'ball_speed'];

/** Menu chip labels: the Live panel's label for the metric with the same id. */
export const CALLOUT_METRIC_LABEL_KEYS: Record<CalloutMetricId, MessageKey> = {
  carry: 'metric.carry',
  ball_speed: 'metric.ballSpeed',
  club_speed: 'metric.clubSpeed',
  smash: 'metric.smash',
  launch_v: 'metric.vLaunch',
  spin: 'metric.spin',
};

export type CalloutLanguage = 'de' | 'en' | 'es';

export interface CalloutLanguageOption {
  id: CalloutLanguage;
  /** Native name shown in the voice language dropdown. */
  nativeName: string;
  /** BCP 47 tag for the utterance and its number format. */
  speechLang: string;
}

export const CALLOUT_LANGUAGES: readonly CalloutLanguageOption[] = [
  { id: 'de', nativeName: 'Deutsch', speechLang: 'de-DE' },
  { id: 'en', nativeName: 'English', speechLang: 'en-US' },
  { id: 'es', nativeName: 'Español', speechLang: 'es-ES' },
];

interface CalloutPhrases {
  carry: string;
  ballSpeed: string;
  clubSpeed: string;
  smash: string;
  launch: string;
  spin: string;
  swingSpeed: string;
  meters: string;
  yards: string;
  degrees: string;
}

// Speeds are spoken without a unit: the number is what a golfer listens for,
// and "kilometers per hour" after every value doubles the callout.
const PHRASES: Record<CalloutLanguage, CalloutPhrases> = {
  de: {
    carry: 'Carry',
    ballSpeed: 'Ballgeschwindigkeit',
    clubSpeed: 'Schlägergeschwindigkeit',
    smash: 'Smash',
    launch: 'Abflugwinkel',
    spin: 'Spin',
    swingSpeed: 'Schwunggeschwindigkeit',
    meters: 'Meter',
    yards: 'Yards',
    degrees: 'Grad',
  },
  en: {
    carry: 'Carry',
    ballSpeed: 'Ball speed',
    clubSpeed: 'Club speed',
    smash: 'Smash',
    launch: 'Launch',
    spin: 'Spin',
    swingSpeed: 'Swing speed',
    meters: 'meters',
    yards: 'yards',
    degrees: 'degrees',
  },
  es: {
    carry: 'Carry',
    ballSpeed: 'Velocidad de bola',
    clubSpeed: 'Velocidad del palo',
    smash: 'Smash',
    launch: 'Salida',
    spin: 'Spin',
    swingSpeed: 'Velocidad de swing',
    meters: 'metros',
    yards: 'yardas',
    degrees: 'grados',
  },
};

const calloutLanguageIds = new Set<string>(CALLOUT_LANGUAGES.map((option) => option.id));

export function isCalloutLanguage(value: unknown): value is CalloutLanguage {
  return typeof value === 'string' && calloutLanguageIds.has(value);
}

export function isCalloutMetricId(value: unknown): value is CalloutMetricId {
  return typeof value === 'string' && (CALLOUT_METRICS as readonly string[]).includes(value);
}

export function getSpeechLang(language: CalloutLanguage): string {
  return CALLOUT_LANGUAGES.find((option) => option.id === language)?.speechLang ?? 'en-US';
}

/** First of the browser's preferred languages that has phrases, else English. */
export function pickCalloutLanguage(preferred: readonly string[]): CalloutLanguage {
  for (const tag of preferred) {
    const primary = tag.split('-')[0]?.toLowerCase();
    if (isCalloutLanguage(primary)) return primary;
  }
  return 'en';
}

export interface CalloutConfig {
  metrics: readonly CalloutMetricId[];
  language: CalloutLanguage;
  unitSystem: UnitSystem;
}

function formatNumber(value: number, speechLang: string, digits: number): string {
  // No grouping: "2,800" or "2.800" can be read as a decimal.
  return new Intl.NumberFormat(speechLang, { maximumFractionDigits: digits, useGrouping: false }).format(value);
}

function part(label: string, value: string, unit?: string): string {
  return unit ? `${label} ${value} ${unit}` : `${label} ${value}`;
}

function finite(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function metricPart(
  metric: CalloutMetricId,
  shot: Shot,
  phrases: CalloutPhrases,
  speechLang: string,
  unitSystem: UnitSystem
): string | null {
  switch (metric) {
    case 'carry': {
      const carry = getCarryYards(shot);
      if (!finite(carry)) return null;
      const distance = formatNumber(convertDistanceFromYards(carry, unitSystem), speechLang, 0);
      return part(phrases.carry, distance, getDistanceUnit(unitSystem) === 'm' ? phrases.meters : phrases.yards);
    }
    case 'ball_speed':
      return finite(shot.ball_speed_mph)
        ? part(phrases.ballSpeed, formatNumber(convertSpeedFromMph(shot.ball_speed_mph, unitSystem), speechLang, 0))
        : null;
    case 'club_speed':
      return finite(shot.club_speed_mph)
        ? part(phrases.clubSpeed, formatNumber(convertSpeedFromMph(shot.club_speed_mph, unitSystem), speechLang, 0))
        : null;
    case 'smash':
      return finite(shot.smash_factor) ? part(phrases.smash, formatNumber(shot.smash_factor, speechLang, 2)) : null;
    case 'launch_v':
      return finite(shot.launch_angle_vertical)
        ? part(phrases.launch, formatNumber(shot.launch_angle_vertical, speechLang, 1), phrases.degrees)
        : null;
    case 'spin': {
      // Heard without the screen's "typical" caption, so only a measured spin is said.
      const spin = getDisplayedSpin(shot);
      return spin?.source === 'measured' ? part(phrases.spin, formatNumber(spin.rpm, speechLang, 0)) : null;
    }
  }
}

/**
 * The sentence to speak for a shot, or null when none of the chosen values
 * exist. A swing-speed rep only has its swing speed, whatever is chosen.
 */
export function buildShotCallout(shot: Shot, config: CalloutConfig): string | null {
  const phrases = PHRASES[config.language];
  const speechLang = getSpeechLang(config.language);

  if (isSwingSpeedShot(shot)) {
    const speed = getSwingSpeedMph(shot);
    if (!finite(speed)) return null;
    return part(phrases.swingSpeed, formatNumber(convertSpeedFromMph(speed, config.unitSystem), speechLang, 0));
  }

  const parts = CALLOUT_METRICS.filter((metric) => config.metrics.includes(metric))
    .map((metric) => metricPart(metric, shot, phrases, speechLang, config.unitSystem))
    .filter((text): text is string => text !== null);

  return parts.length > 0 ? parts.join(', ') : null;
}

/**
 * What the menu's test button says: the latest shot as it would be called
 * out, or the sample before the first shot (or when the latest shot has none
 * of the chosen values).
 */
export function testCalloutText(latest: Shot | null, config: CalloutConfig): string | null {
  return (latest && buildShotCallout(latest, config)) ?? buildShotCallout(SAMPLE_CALLOUT_SHOT, config);
}

/**
 * Spoken by the menu's test button before the first shot of a session. Its
 * spin is measured and used for the carry, so the sample can say it.
 */
export const SAMPLE_CALLOUT_SHOT: Shot = {
  mode: 'mock',
  ball_speed_mph: 120,
  club_speed_mph: 87,
  smash_factor: 1.38,
  estimated_carry_yards: 165,
  carry_range: [158, 172],
  club: '7-iron',
  timestamp: '1970-01-01T00:00:00Z',
  peak_magnitude: null,
  launch_angle_vertical: 16.5,
  launch_angle_horizontal: null,
  launch_angle_confidence: null,
  angle_source: null,
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 6200,
  spin_confidence: 0.9,
  spin_quality: 'high',
  spin_source: 'measured',
  carry_spin_adjusted: 165,
  carry_spin_rpm: 6200,
  carry_spin_source: 'measured',
};
