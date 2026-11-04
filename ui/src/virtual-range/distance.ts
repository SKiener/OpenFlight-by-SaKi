import { formatDistance, getDistanceUnit, type UnitSystem } from '../utils/units';
import { YARD_M } from './flightPath';

export const distanceValue = (distanceM: number, unitSystem: UnitSystem, digits = 0): string =>
  formatDistance(distanceM / YARD_M, unitSystem, digits);

export const distanceLabel = (distanceM: number, unitSystem: UnitSystem, digits = 0): string =>
  `${distanceValue(distanceM, unitSystem, digits)} ${getDistanceUnit(unitSystem)}`;
