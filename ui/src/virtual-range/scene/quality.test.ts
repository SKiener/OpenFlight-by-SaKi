import { describe, expect, it } from 'vitest';
import { fakeStorage } from '../../test-utils/fakeBrowser';
import { QUALITY_STORAGE_KEY, QualityMonitor, loadQuality, saveQuality, type QualityLevel } from './quality';

type Result = QualityLevel | null;

const feed = (monitor: QualityMonitor, frameMs: number, count: number): Result[] =>
  Array.from({ length: count }, () => monitor.record(frameMs));

const stepsIn = (results: Result[]): [frame: number, level: QualityLevel][] =>
  results.flatMap((level, i): [number, QualityLevel][] => (level === null ? [] : [[i + 1, level]]));

const storedAs = (value: string) => fakeStorage({ [QUALITY_STORAGE_KEY]: value }).localStorage;

const failingStorage = (failure: unknown) => ({
  getItem: (): never => {
    throw failure;
  },
  setItem: (): never => {
    throw failure;
  },
});

const blockedStorage = failingStorage(new DOMException('blocked', 'SecurityError'));

const FAILURES: [string, unknown][] = [
  ['a DOMException', new DOMException('blocked', 'SecurityError')],
  ['an Error', new Error('blocked')],
  ['a TypeError', new TypeError('blocked')],
  ['a string', 'blocked'],
];

describe('QualityMonitor', () => {
  it('stays at full quality at 60 fps', () => {
    const monitor = new QualityMonitor(0);
    expect(stepsIn(feed(monitor, 16.7, 300))).toEqual([]);
    expect(monitor.level).toBe(0);
  });

  it('steps down after 2 s with a median above 25 ms, one level per window', () => {
    const monitor = new QualityMonitor(0);
    expect(stepsIn(feed(monitor, 30, 1000))).toEqual([
      [67, 1],
      [134, 2],
    ]);
    expect(monitor.level).toBe(2);
  });

  it('ignores a single hitch', () => {
    const monitor = new QualityMonitor(0);
    const results = [...feed(monitor, 16.7, 60), monitor.record(200), ...feed(monitor, 16.7, 240)];
    expect(stepsIn(results)).toEqual([]);
    expect(monitor.level).toBe(0);
  });

  it('closes a window once its frames add up to 2 s', () => {
    expect(stepsIn(feed(new QualityMonitor(0), 40, 200))).toEqual([
      [50, 1],
      [100, 2],
    ]);
  });

  it('keeps a window open until 2 s are covered', () => {
    const monitor = new QualityMonitor(0);
    expect(feed(monitor, 200, 9)).toEqual(Array(9).fill(null));
    expect(monitor.record(199.5)).toBeNull();
    expect(monitor.record(200)).toBe(1);
  });

  it('cannot close a window with a single hitch', () => {
    const monitor = new QualityMonitor(0);
    const firstTen = [monitor.record(5000), ...feed(monitor, 16.7, 8), monitor.record(30)];
    expect(firstTen).toEqual(Array(10).fill(null));
    expect(monitor.level).toBe(0);
    const nextWindow = feed(monitor, 30, 67);
    expect(stepsIn(nextWindow)).toEqual([[67, 1]]);
  });

  it('starts a new window after one that did not step', () => {
    const monitor = new QualityMonitor(0);
    expect(stepsIn(feed(monitor, 16.7, 120))).toEqual([]);
    expect(stepsIn(feed(monitor, 30, 67))).toEqual([[67, 1]]);
  });

  it('never steps back up', () => {
    const monitor = new QualityMonitor(0);
    feed(monitor, 30, 67);
    expect(stepsIn(feed(monitor, 16.7, 600))).toEqual([]);
    expect(monitor.level).toBe(1);
  });

  it('steps from the level it was given', () => {
    const monitor = new QualityMonitor(1);
    expect(monitor.level).toBe(1);
    expect(stepsIn(feed(monitor, 30, 200))).toEqual([[67, 2]]);
    expect(monitor.level).toBe(2);
  });

  it('does nothing at the lowest level', () => {
    const monitor = new QualityMonitor(2);
    expect(stepsIn(feed(monitor, 100, 500))).toEqual([]);
    expect(monitor.level).toBe(2);
  });

  it('cannot have its level set by callers', () => {
    const monitor = new QualityMonitor(0);
    expect(() => Object.assign(monitor, { level: 2 })).toThrow(TypeError);
    expect(monitor.level).toBe(0);
  });

  it.each([NaN, Infinity, -Infinity, 0, -0, -5])('counts a frame of %s ms for nothing', (invalid) => {
    const monitor = new QualityMonitor(0);
    for (let validFrames = 1; validFrames < 67; validFrames++) {
      expect(monitor.record(invalid)).toBeNull();
      expect(monitor.record(30)).toBeNull();
    }
    expect(monitor.record(invalid)).toBeNull();
    expect(monitor.record(30)).toBe(1);
  });

  describe('median', () => {
    const FAST = 9;
    const SLOW = 100;
    const HITCH = 5000;
    const evenWindow = (low: number, high: number) => [low, FAST, FAST, SLOW, SLOW, SLOW, FAST, FAST, high, HITCH];
    const oddWindow = (middle: number) => [SLOW, FAST, SLOW, FAST, middle, SLOW, FAST, SLOW, FAST, FAST, HITCH];

    it('does not step when the median is exactly 25 ms', () => {
      expect(stepsIn(feed(new QualityMonitor(0), 25, 400))).toEqual([]);
    });

    it('steps when the median is just above 25 ms', () => {
      expect(stepsIn(feed(new QualityMonitor(0), 25.1, 80))).toEqual([[80, 1]]);
    });

    it.each([
      [20, 29, false],
      [24, 26, false],
      [20, 30, false],
      [21, 30, true],
      [20, 31, true],
    ])('takes the mean of the two middle frames (%f and %f, steps down: %s)', (low, high, stepsDown) => {
      const monitor = new QualityMonitor(0);
      const results = evenWindow(low, high).map((frameMs) => monitor.record(frameMs));
      expect(stepsIn(results)).toEqual(stepsDown ? [[10, 1]] : []);
    });

    it.each([
      [24, false],
      [25, false],
      [26, true],
    ])('takes the middle frame of an odd count (%f, steps down: %s)', (middle, stepsDown) => {
      const monitor = new QualityMonitor(0);
      const results = oddWindow(middle).map((frameMs) => monitor.record(frameMs));
      expect(stepsIn(results)).toEqual(stepsDown ? [[11, 1]] : []);
    });
  });
});

describe('loadQuality', () => {
  it('reads a stored level and falls back to full quality', () => {
    expect(loadQuality(storedAs('1'))).toBe(1);
    expect(loadQuality(storedAs('7'))).toBe(0);
    expect(loadQuality(storedAs('x'))).toBe(0);
    expect(loadQuality(null)).toBe(0);
    expect(loadQuality(blockedStorage)).toBe(0);
  });

  it.each([
    ['0', 0],
    ['1', 1],
    ['2', 2],
    ['3', 0],
    ['7', 0],
    ['-1', 0],
    ['01', 0],
    [' 1', 0],
    ['1 ', 0],
    ['1.0', 0],
    ['', 0],
    ['x', 0],
    ['NaN', 0],
    ['constructor', 0],
    ['__proto__', 0],
  ])('reads %j as level %i', (stored, level) => {
    expect(loadQuality(storedAs(stored))).toBe(level);
  });

  it('falls back to full quality when nothing is stored', () => {
    expect(loadQuality(fakeStorage().localStorage)).toBe(0);
  });

  it.each(FAILURES)('falls back to full quality when getItem throws %s', (_kind, failure) => {
    expect(loadQuality(failingStorage(failure))).toBe(0);
  });

  it('asks for the versioned storage key', () => {
    const asked: string[] = [];
    const getItem = (key: string) => {
      asked.push(key);
      return null;
    };
    loadQuality({ getItem });
    expect(asked).toEqual(['openflight.virtual-range.quality:v1']);
  });
});

describe('saveQuality', () => {
  it('stores the level as text under the storage key', () => {
    const { store, localStorage } = fakeStorage();
    saveQuality(localStorage, 2);
    expect(store).toEqual({ 'openflight.virtual-range.quality:v1': '2' });
  });

  it.each<QualityLevel>([0, 1, 2])('writes level %i over a different stored level and reads it back', (level) => {
    const { store, localStorage } = fakeStorage({ [QUALITY_STORAGE_KEY]: String((level + 1) % 3) });
    saveQuality(localStorage, level);
    expect(store[QUALITY_STORAGE_KEY]).toBe(String(level));
    expect(loadQuality(localStorage)).toBe(level);
  });

  it('does nothing without storage', () => {
    expect(() => saveQuality(null, 1)).not.toThrow();
  });

  it.each(FAILURES)('shrugs off a storage that throws %s on the write', (_kind, failure) => {
    expect(() => saveQuality(failingStorage(failure), 1)).not.toThrow();
  });
});
