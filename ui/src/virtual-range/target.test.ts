import { describe, expect, it } from 'vitest';
import { rotateAboutTee } from './aim';
import type { RangeShot, ScenePoint } from './flightPath';
import { GREENS } from './layout';
import { distanceLabel } from './distance';
import { directDistanceM, restOnRange, targetFor } from './target';

const flagOf = (index: number) => ({ x: GREENS[index].sideM, z: -GREENS[index].distanceM });

describe('targetFor', () => {
  it('measures to the selected green', () => {
    const rest = { x: 3, z: -140 };
    const selected = GREENS.findIndex(({ distanceM }) => distanceM === 150);

    const target = targetFor(rest, selected, null);

    expect(target).toEqual({ ...flagOf(selected), kind: 'selected' });
    expect(directDistanceM(rest, target)).toBeCloseTo(Math.hypot(3 - GREENS[selected].sideM, 10), 12);
  });

  it('measures to the selected green even when another flag is nearer', () => {
    const near75 = { x: 7, z: -76 };

    expect(targetFor(near75, 7, null)).toEqual({ ...flagOf(7), kind: 'selected' });
  });

  it('falls back to the nearest flag', () => {
    expect(targetFor({ x: 6, z: -80 }, null, null)).toEqual({ ...flagOf(1), kind: 'nearest' });
    expect(targetFor({ x: 0, z: -121.6 }, null, null)).toEqual({ ...flagOf(3), kind: 'nearest' });
    expect(targetFor({ x: 0, z: -400 }, null, null)).toEqual({ ...flagOf(GREENS.length - 1), kind: 'nearest' });
    expect(targetFor({ x: 0, z: 0 }, null, null)).toEqual({ ...flagOf(0), kind: 'nearest' });
  });

  it.each([-1, GREENS.length, 1.5, Number.NaN])(
    'takes the nearest flag for a selection %s that names no green',
    (index) => {
      expect(targetFor({ x: 6, z: -80 }, index, null)).toEqual({ ...flagOf(1), kind: 'nearest' });
    }
  );

  it('measures to the training target', () => {
    const target = targetFor({ x: 4, z: -110 }, 3, 120);

    expect(target).toEqual({ x: 0, z: -120, kind: 'training' });
    expect(directDistanceM({ x: 4, z: -110 }, target)).toBeCloseTo(Math.hypot(4, 10), 12);
  });

  it('labels the distance to the nearest flag in metres or yards', () => {
    const rest = { x: 0, z: -121.6 };
    const distanceM = directDistanceM(rest, targetFor(rest, null, null));

    const metric = distanceLabel(distanceM, 'metric');
    const imperial = distanceLabel(distanceM, 'imperial');

    expect(distanceM).toBeCloseTo(Math.hypot(9, 3.4), 9);
    expect(metric).toBe('10 m');
    expect(imperial).toBe('11 yds');
    expect(directDistanceM(rest, targetFor(rest, null, null))).toBe(distanceM);
  });
});

describe('directDistanceM', () => {
  it('measures straight across the ground, either way round', () => {
    expect(directDistanceM({ x: 1, z: -2 }, { x: 4, z: -6 })).toBe(5);
    expect(directDistanceM({ x: 4, z: -6 }, { x: 1, z: -2 })).toBe(5);
    expect(directDistanceM({ x: 4, z: -6 }, { x: 4, z: -6 })).toBe(0);
  });
});

describe('restOnRange', () => {
  const flight: ScenePoint[] = [
    { t: 0, x: 0, y: 0, z: 0 },
    { t: 3, x: 2, y: 0, z: -100 },
  ];
  const roll: ScenePoint[] = [
    { t: 3, x: 2, y: 0, z: -100 },
    { t: 5, x: 3, y: 0, z: -112 },
  ];
  const shot: RangeShot = { id: 's', club: '7-iron', ballSpeedMph: 120, carryM: 100, lateralM: 2, flight, roll };

  it('turns the rest point of a rolled shot by the bearing it was hit with', () => {
    expect(restOnRange(shot, 0)).toEqual({ x: 3, z: -112 });
    expect(restOnRange(shot, 0.2)).toEqual(rotateAboutTee({ x: 3, z: -112 }, 0.2));
  });

  it('rests a shot whose roll is not drawn at its landing', () => {
    expect(restOnRange({ ...shot, roll: [roll[0]] }, 0)).toEqual({ x: 2, z: -100 });
    expect(restOnRange({ ...shot, flight: null }, 0)).toEqual({ x: 2, z: -100 });
  });
});
