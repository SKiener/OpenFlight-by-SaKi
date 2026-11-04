import { GREENS, type Green } from './layout';
import { readJson, writeJson, type JsonStorage } from './storedJson';

export interface GroundPoint {
  x: number;
  z: number;
}

export const TARGET_KEY = 'openflight.virtual-range.target:v1';
export const AIMS_KEY = 'openflight.virtual-range.aims:v1';
const KEPT_AIMS = 300;

export const NO_AIMS: ReadonlyMap<string, number> = new Map();

export const greenBearing = ({ distanceM, sideM }: Green): number => Math.atan2(sideM, distanceM);

export function rotateAboutTee({ x, z }: GroundPoint, bearing: number): GroundPoint {
  const cos = Math.cos(bearing);
  const sin = Math.sin(bearing);
  return { x: x * cos - z * sin, z: x * sin + z * cos };
}

export function loadTarget(storage: JsonStorage): number | null {
  const index = readJson(storage, TARGET_KEY);
  return typeof index === 'number' && Number.isInteger(index) && index >= 0 && index < GREENS.length ? index : null;
}

export function saveTarget(storage: JsonStorage, index: number | null): void {
  writeJson(storage, TARGET_KEY, index);
}

const isAim = (entry: unknown): entry is [string, number] =>
  Array.isArray(entry) &&
  entry.length === 2 &&
  typeof entry[0] === 'string' &&
  typeof entry[1] === 'number' &&
  Number.isFinite(entry[1]);

export function loadAims(storage: JsonStorage): Map<string, number> {
  const stored = readJson(storage, AIMS_KEY);
  return new Map(Array.isArray(stored) ? stored.filter(isAim) : []);
}

export function saveAims(storage: JsonStorage, aims: ReadonlyMap<string, number>): void {
  writeJson(storage, AIMS_KEY, [...aims].slice(-KEPT_AIMS));
}
