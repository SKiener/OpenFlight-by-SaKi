import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeRangeScene } from '../test-utils/fakeRangeScene';
import { demoShots } from './demoShots';
import { DEMO_REPORT_CEILING_MS, demoFeed, startRangeDemo, summarizeFrames } from './rangeDemo';

describe('demoFeed', () => {
  it('holds the demo shots, all seen, so nothing flies until the demo starts one', () => {
    expect(demoFeed().shots).toBe(demoShots());
    expect(demoFeed().seenOnReplace).toEqual(demoShots().map((shot) => shot.id));
    expect(demoFeed()).toBe(demoFeed());
  });
});

describe('summarizeFrames', () => {
  it('picks the median and the 95th percentile of the sorted frame times', () => {
    expect(summarizeFrames([40, 10, 30, 20])).toEqual({ frames: 4, p50_ms: 20, p95_ms: 30 });
  });

  it('ignores a few slow frames in the percentile, but not many', () => {
    const smooth = Array<number>(2400).fill(16.7);

    expect(summarizeFrames([...smooth, 90, 90, 90]).p95_ms).toBe(16.7);
    expect(summarizeFrames([...smooth, ...Array<number>(300).fill(40)]).p95_ms).toBe(40);
  });

  it('rounds to hundredths of a millisecond', () => {
    expect(summarizeFrames([16.66666, 16.66666])).toMatchObject({ p50_ms: 16.67, p95_ms: 16.67 });
  });

  it('has no percentiles without frames', () => {
    expect(summarizeFrames([])).toEqual({ frames: 0, p50_ms: null, p95_ms: null });
  });
});

describe('startRangeDemo', () => {
  let frameCallbacks: Map<number, () => void>;
  let nextFrameId: number;

  const runFrame = (): void => {
    const callbacks = [...frameCallbacks.values()];
    frameCallbacks.clear();
    for (const callback of callbacks) callback();
  };

  beforeEach(() => {
    vi.useFakeTimers();
    frameCallbacks = new Map();
    nextFrameId = 1;
    vi.stubGlobal('requestAnimationFrame', (callback: () => void) => {
      frameCallbacks.set(nextFrameId, callback);
      return nextFrameId++;
    });
    vi.stubGlobal('cancelAnimationFrame', (id: number) => frameCallbacks.delete(id));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function start(mountedAt = 0, now = () => 0) {
    const scene = fakeRangeScene();
    const fly = vi.fn<(shotId: string) => void>(() => {
      scene.reading.animating = true;
    });
    const log = vi.fn<(line: string) => void>();
    const stop = startRangeDemo(scene, fly, { mountedAt, now, log });
    return { scene, fly, log, stop };
  }

  const flown = (fly: ReturnType<typeof start>['fly']) => fly.mock.calls.map(([id]) => id);

  const finishFlight = (scene: ReturnType<typeof fakeRangeScene>): void => {
    scene.reading.animating = false;
    vi.advanceTimersByTime(250 + 2000);
  };

  it('flies the first demo shot at once and the next one only after the first is over, and a pause', () => {
    const { scene, fly } = start();
    expect(flown(fly)).toEqual(['demo-iron8']);

    vi.advanceTimersByTime(5000);
    expect(flown(fly)).toEqual(['demo-iron8']);

    scene.reading.animating = false;
    vi.advanceTimersByTime(250 + 1999);
    expect(flown(fly)).toEqual(['demo-iron8']);
    vi.advanceTimersByTime(1);
    expect(flown(fly)).toEqual(['demo-iron8', 'demo-driver']);
  });

  it('loops through the demo shots in order, and round again', () => {
    const { scene, fly } = start();

    for (let shot = 0; shot < 4; shot++) finishFlight(scene);

    expect(flown(fly)).toEqual(['demo-iron8', 'demo-driver', 'demo-wedge', 'demo-iron8', 'demo-driver']);
  });

  it('logs one OF_RESULT line once the third flight is over and the pause after it, from the frame times and the scene stats', () => {
    const { scene, fly, log } = start();
    const flownAtReport: string[][] = [];
    log.mockImplementation(() => flownAtReport.push(flown(fly)));
    scene.frameLog.push(16.7, 16.7, 16.8, 30);
    scene.reading.frames = 2500;
    scene.reading.quality = 1;

    finishFlight(scene);
    finishFlight(scene);
    vi.advanceTimersByTime(60_000);
    expect(flown(fly)).toEqual(['demo-iron8', 'demo-driver', 'demo-wedge']);
    expect(log, 'the wedge still flies').not.toHaveBeenCalled();
    scene.reading.animating = false;
    vi.advanceTimersByTime(250 + 1999);
    expect(log).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);

    expect(log).toHaveBeenCalledOnce();
    expect(flownAtReport).toEqual([['demo-iron8', 'demo-driver', 'demo-wedge']]);
    const line = log.mock.calls[0][0];
    expect(line.startsWith('OF_RESULT {')).toBe(true);
    expect(line).not.toContain('\n');
    const result = JSON.parse(line.slice('OF_RESULT '.length));
    expect(Object.keys(result)).toEqual(['frames', 'p50_ms', 'p95_ms', 'open_ms', 'stats']);
    expect(result).toMatchObject({
      frames: 4,
      p50_ms: 16.7,
      p95_ms: 16.8,
      stats: { ...scene.reading, animating: false },
    });

    for (let shot = 0; shot < 4; shot++) finishFlight(scene);
    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS);
    expect(log).toHaveBeenCalledOnce();
  });

  it('reports after 90 s at the latest, once, when a flight never ends', () => {
    const { log } = start();

    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS - 1);
    expect(log).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(log).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS);
    expect(log).toHaveBeenCalledOnce();
  });

  it('measures the opening from the mount to the first frame the scene rendered', () => {
    let clock = 1100;
    const { scene, log } = start(1000, () => clock);

    runFrame();
    expect(frameCallbacks.size).toBe(1);

    clock = 1850;
    scene.reading.frames = 1;
    runFrame();
    expect(frameCallbacks.size).toBe(0);

    clock = 99_000;
    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS);
    expect(JSON.parse(log.mock.calls[0][0].slice('OF_RESULT '.length)).open_ms).toBe(850);
  });

  it('reports no opening time when the scene never rendered', () => {
    const { log } = start();

    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS);

    expect(JSON.parse(log.mock.calls[0][0].slice('OF_RESULT '.length)).open_ms).toBeNull();
  });

  it('stops flying, waiting for a frame and reporting once stopped', () => {
    const { scene, fly, log, stop } = start();

    stop();
    scene.reading.animating = false;
    vi.advanceTimersByTime(DEMO_REPORT_CEILING_MS * 2);

    expect(flown(fly)).toEqual(['demo-iron8']);
    expect(log).not.toHaveBeenCalled();
    expect(frameCallbacks.size).toBe(0);
  });
});
