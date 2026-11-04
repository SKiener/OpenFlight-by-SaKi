import { getHtmlLang } from '../i18n';

export type UnitSystem = 'imperial' | 'metric' | 'mph-meters' | 'kmh-yards';

export function isUnitSystem(value: unknown): value is UnitSystem {
  return value === 'imperial' || value === 'metric' || value === 'mph-meters' || value === 'kmh-yards';
}

export function withSpeedUnit(current: UnitSystem, speed: 'mph' | 'km/h'): UnitSystem {
  return getDistanceUnit(current) === 'm'
    ? (speed === 'mph' ? 'mph-meters' : 'metric')
    : (speed === 'mph' ? 'imperial' : 'kmh-yards');
}

export function withDistanceUnit(current: UnitSystem, distance: 'm' | 'yds'): UnitSystem {
  return getSpeedUnit(current) === 'mph'
    ? (distance === 'm' ? 'mph-meters' : 'imperial')
    : (distance === 'm' ? 'metric' : 'kmh-yards');
}

export function formatNumber(value: number, digits: number): string {
  return value.toLocaleString(getHtmlLang(), {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    useGrouping: false,
  });
}

const MPH_TO_KMH = 1.60934;
const YARDS_TO_METERS = 0.9144;
const IMPERIAL_SPEED_UNIT = 'mph';
const METRIC_SPEED_UNIT = 'km/h';
const IMPERIAL_DISTANCE_UNIT = 'yds';
const METRIC_DISTANCE_UNIT = 'm';

export function convertSpeedFromMph(speedMph: number, unitSystem: UnitSystem): number {
  if (getSpeedUnit(unitSystem) === METRIC_SPEED_UNIT) {
    return speedMph * MPH_TO_KMH;
  }

  return speedMph;
}

export function convertDistanceFromYards(distanceYards: number, unitSystem: UnitSystem): number {
  if (getDistanceUnit(unitSystem) === METRIC_DISTANCE_UNIT) {
    return distanceYards * YARDS_TO_METERS;
  }

  return distanceYards;
}

export function formatSpeed(speedMph: number, unitSystem: UnitSystem, digits = 1): string {
  return formatNumber(convertSpeedFromMph(speedMph, unitSystem), digits);
}

export function formatDistance(distanceYards: number, unitSystem: UnitSystem, digits = 0): string {
  return formatNumber(convertDistanceFromYards(distanceYards, unitSystem), digits);
}

export function getSpeedUnit(unitSystem: UnitSystem): 'mph' | 'km/h' {
  return unitSystem === 'metric' || unitSystem === 'kmh-yards' ? METRIC_SPEED_UNIT : IMPERIAL_SPEED_UNIT;
}

export function getDistanceUnit(unitSystem: UnitSystem): 'yds' | 'm' {
  return unitSystem === 'metric' || unitSystem === 'mph-meters' ? METRIC_DISTANCE_UNIT : IMPERIAL_DISTANCE_UNIT;
}

const CM_PER_INCH = 2.54;

/** Short lengths (club data, tee height): cm with metres, inches with yards. */
export function getLengthUnit(unitSystem: UnitSystem): 'cm' | 'in' {
  return getDistanceUnit(unitSystem) === 'm' ? 'cm' : 'in';
}

/** A short length given in cm, one decimal in ``getLengthUnit`` (locale decimal separator). */
export function formatLengthCm(valueCm: number, unitSystem: UnitSystem): string {
  return formatNumber(getLengthUnit(unitSystem) === 'cm' ? valueCm : valueCm / CM_PER_INCH, 1);
}

export function getUnitsLabel(unitSystem: UnitSystem): string {
  return `${getSpeedUnit(unitSystem)} / ${getDistanceUnit(unitSystem)}`;
}

export function formatCarryRange(carryRange: [number, number], unitSystem: UnitSystem): string {
  const min = formatDistance(carryRange[0], unitSystem, 0);
  const max = formatDistance(carryRange[1], unitSystem, 0);
  return `${min}-${max} ${getDistanceUnit(unitSystem)}`;
}
