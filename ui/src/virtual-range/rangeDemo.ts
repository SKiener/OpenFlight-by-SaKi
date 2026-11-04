import { demoShots } from './demoShots';
import type { ShotFeed } from './rangeViewModel';
import type { RangeScene } from './scene/createRangeScene';

export const DEMO_REPORT_CEILING_MS = 90_000;
const POLL_MS = 250;
const PAUSE_MS = 2000;

let feed: ShotFeed | null = null;

/** The demo shots, all seen, so nothing flies until the demo starts one. */
export function demoFeed(): ShotFeed {
  if (!feed) {
    const shots = demoShots();
    feed = { shots, origin: 'demo', seenOnReplace: shots.map((shot) => shot.id), holding: false };
  }
  return feed;
}

const round = (value: number): number => Math.round(value * 100) / 100;

export function summarizeFrames(frameTimes: readonly number[]): {
  frames: number;
  p50_ms: number | null;
  p95_ms: number | null;
} {
  const sorted = [...frameTimes].sort((a, b) => a - b);
  const pick = (q: number): number | null =>
    sorted.length === 0 ? null : round(sorted[Math.floor(q * (sorted.length - 1))]);
  return { frames: sorted.length, p50_ms: pick(0.5), p95_ms: pick(0.95) };
}

export interface RangeDemoOptions {
  mountedAt: number;
  now?: () => number;
  log?: (line: string) => void;
}

// `open_ms` runs from `mountedAt` to the scene's first rendered frame.
export function startRangeDemo(
  scene: RangeScene,
  fly: (shotId: string) => void,
  { mountedAt, now = () => performance.now(), log = (line) => console.log(line) }: RangeDemoOptions
): () => void {
  const shots = demoShots();
  let openMs: number | null = null;
  let frameRequest = 0;
  let next = 0;
  let pollTimer: ReturnType<typeof setTimeout> | undefined;
  let reported = false;

  function report(): void {
    if (reported) return;
    reported = true;
    clearTimeout(ceiling);
    log(
      `OF_RESULT ${JSON.stringify({ ...summarizeFrames(scene.frameTimes()), open_ms: openMs, stats: scene.stats() })}`
    );
  }

  const watchFirstFrame = (): void => {
    if (scene.stats().frames > 0) {
      openMs = Math.round(now() - mountedAt);
      return;
    }
    frameRequest = requestAnimationFrame(watchFirstFrame);
  };

  function flyNext(): void {
    fly(shots[next % shots.length].id);
    next += 1;
    waitForEnd();
  }

  // The pause leaves the scene idle between flights, so its idle load shows in the Pi's top log.
  function waitForEnd(): void {
    pollTimer = setTimeout(() => {
      if (scene.stats().animating) waitForEnd();
      else pollTimer = setTimeout(afterPause, PAUSE_MS);
    }, POLL_MS);
  }

  function afterPause(): void {
    if (next === shots.length) report();
    flyNext();
  }

  const ceiling = setTimeout(report, DEMO_REPORT_CEILING_MS);

  watchFirstFrame();
  flyNext();

  return () => {
    cancelAnimationFrame(frameRequest);
    clearTimeout(pollTimer);
    clearTimeout(ceiling);
  };
}
