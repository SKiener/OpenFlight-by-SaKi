import { expect, it, vi } from 'vitest';
import '../App';
import { demoShots } from './demoShots';
import { landingPoint, restingPoint, sampleFlight, shownTotalM, usableRoll } from './flightPath';
import { demoFeed } from './rangeDemo';

vi.mock('./flightPath', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./flightPath')>();
  return { ...actual, sampleFlight: vi.fn(actual.sampleFlight) };
});

it('samples the demo flights on first use, once, not when the app loads', () => {
  expect(sampleFlight).not.toHaveBeenCalled();

  const shots = demoShots();

  expect(sampleFlight).toHaveBeenCalledTimes(shots.length);
  expect(demoShots()).toBe(shots);
  expect(demoFeed().shots).toBe(shots);
  expect(sampleFlight).toHaveBeenCalledTimes(shots.length);
});

it('rolls every demo shot out from its landing: the wedge back a little, the others forward', () => {
  const rolls = Object.fromEntries(
    demoShots().map((shot) => {
      const roll = usableRoll(shot);
      expect(roll, shot.id).not.toBeNull();
      expect(roll![0], shot.id).toEqual(shot.flight!.at(-1));
      const landing = landingPoint(shot);
      const rest = restingPoint(shot);
      expect(shownTotalM(shot), shot.id).toBeCloseTo(Math.hypot(rest.x, rest.z), 6);
      return [shot.id, Math.hypot(rest.x, rest.z) - Math.hypot(landing.x, landing.z)];
    })
  );

  expect(rolls['demo-wedge']).toBeLessThan(-0.2);
  expect(rolls['demo-wedge']).toBeGreaterThan(-2);
  expect(rolls['demo-iron8']).toBeGreaterThan(5);
  expect(rolls['demo-driver']).toBeGreaterThan(rolls['demo-iron8']);
});
