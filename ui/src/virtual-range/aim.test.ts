import { describe, expect, it } from 'vitest';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { AIMS_KEY, greenBearing, loadAims, loadTarget, rotateAboutTee, saveAims, saveTarget, TARGET_KEY } from './aim';
import { GREENS } from './layout';

describe('aim', () => {
  it('gives each green its bearing from the tee', () => {
    expect(greenBearing(GREENS[0])).toBeCloseTo(Math.atan2(-9, 50), 12);
    for (const green of GREENS) {
      const { x, z } = rotateAboutTee({ x: green.sideM, z: -green.distanceM }, -greenBearing(green));
      expect(x).toBeCloseTo(0, 9);
      expect(z).toBeCloseTo(-Math.hypot(green.sideM, green.distanceM), 9);
    }
  });

  it('rotates about the tee and back', () => {
    const p = { x: 7.5, z: -143.2 };
    for (const bearing of [-0.4, -0.05, 0, 0.12, 1]) {
      const back = rotateAboutTee(rotateAboutTee(p, bearing), -bearing);
      expect(back.x).toBeCloseTo(p.x, 9);
      expect(back.z).toBeCloseTo(p.z, 9);
    }
    const ahead = rotateAboutTee({ x: 0, z: -100 }, 0.2);
    expect(ahead.x).toBeCloseTo(100 * Math.sin(0.2), 9);
    expect(ahead.z).toBeCloseTo(-100 * Math.cos(0.2), 9);
  });

  it('stores the target and the aims per session', () => {
    const storage = fakeStorage().localStorage;
    expect(loadTarget(storage)).toBeNull();
    saveTarget(storage, 4);
    expect(loadTarget(storage)).toBe(4);
    saveTarget(storage, null);
    expect(loadTarget(storage)).toBeNull();

    expect(loadAims(storage)).toEqual(new Map());
    const aims = new Map(Array.from({ length: 305 }, (_, i): [string, number] => [`shot-${i}`, i / 1000]));
    saveAims(storage, aims);
    const loaded = loadAims(storage);
    expect(loaded.size).toBe(300);
    expect(loaded.has('shot-4')).toBe(false);
    expect(loaded.get('shot-5')).toBe(0.005);
    expect(loaded.get('shot-304')).toBe(0.304);

    for (const corrupt of ['{', '"x"', '[1,2]', '{"a":"b"}']) {
      const broken = fakeStorage({ [TARGET_KEY]: corrupt, [AIMS_KEY]: corrupt }).localStorage;
      expect(loadTarget(broken)).toBeNull();
      expect(loadAims(broken)).toEqual(new Map());
    }
    for (const outside of ['-1', '8', '2.5']) {
      expect(loadTarget(fakeStorage({ [TARGET_KEY]: outside }).localStorage)).toBeNull();
    }
    expect(loadTarget(null)).toBeNull();
    expect(loadAims(null)).toEqual(new Map());
  });
});
