import { rotateAboutTee, type GroundPoint } from './aim';
import { restingPoint, type RangeShot } from './flightPath';
import { GREENS } from './layout';

export type TargetKind = 'selected' | 'training' | 'nearest';

export interface RestTarget extends GroundPoint {
  kind: TargetKind;
}

export const directDistanceM = (a: GroundPoint, b: GroundPoint): number => Math.hypot(b.x - a.x, b.z - a.z);

const flagBase = (index: number): GroundPoint => ({ x: GREENS[index].sideM, z: -GREENS[index].distanceM });

export function targetFor(rest: GroundPoint, selected: number | null, trainingTargetM: number | null): RestTarget {
  if (trainingTargetM !== null) return { x: 0, z: -trainingTargetM, kind: 'training' };
  if (selected !== null && GREENS[selected] !== undefined) return { ...flagBase(selected), kind: 'selected' };
  let nearest = 0;
  for (let index = 1; index < GREENS.length; index++) {
    if (directDistanceM(rest, flagBase(index)) < directDistanceM(rest, flagBase(nearest))) nearest = index;
  }
  return { ...flagBase(nearest), kind: 'nearest' };
}

export const restOnRange = (shot: RangeShot, bearing: number): GroundPoint =>
  rotateAboutTee(restingPoint(shot), bearing);
