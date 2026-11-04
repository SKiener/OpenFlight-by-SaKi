import { expect, test, type Page } from '@playwright/test';
import { fillTexts, spyOnFillText, targetLabels } from './helpers';
import type { RangeShot } from '../../src/virtual-range/flightPath';
import { flightSeconds, shotDuration } from '../../src/virtual-range/scene/cameraPath';
import type { FlightProgress, RangeScene } from '../../src/virtual-range/scene/createRangeScene';
import type { QualityLevel } from '../../src/virtual-range/scene/quality';

interface SceneCost {
  calls: number;
  triangles: number;
  programs: number;
}

interface RangeHarness {
  scene: RangeScene;
  host: HTMLElement;
  shots: RangeShot[];
  flights: (FlightProgress & { at: number; animating: boolean })[];
  qualities: QualityLevel[];
  fatal: { reason: string; at: number }[];
  peak: SceneCost;
  lose?: WEBGL_lose_context;
  landingSignSpots?: { x: number; z: number; at: number }[];
}

declare global {
  interface Window {
    __range: RangeHarness;
  }
}

const IRON8 = 'demo-iron8';
const DRIVER = 'demo-driver';
const WEDGE = 'demo-wedge';

const WEDGE_ROLL_M: [t: number, downrange: number, right: number, up: number][] = [
  [4.203, 74.153, 9.5, 0],
  [4.524, 74.823, 9.586, 0.504],
  [4.844, 75.492, 9.673, 0],
  [4.985, 75.801, 9.712, 0.097],
  [5.126, 76.109, 9.752, 0],
  [5.206, 76.279, 9.774, 0.031],
  [5.286, 76.449, 9.796, 0],
  [5.758, 77.4, 9.919, 0],
  [6.459, 78.519, 10.063, 0],
  [7.161, 79.19, 10.149, 0],
  [7.862, 79.414, 10.178, 0],
];

test.use({ viewport: { width: 1024, height: 600 } });

async function mountScene(page: Page, quality: QualityLevel = 0): Promise<void> {
  await page.evaluate(async (quality) => {
    const { createRangeScene } = (await import(
      /* @vite-ignore */ '/src/virtual-range/scene/createRangeScene.ts'
    )) as typeof import('../../src/virtual-range/scene/createRangeScene');
    const { demoShots } = (await import(
      /* @vite-ignore */ '/src/virtual-range/demoShots.ts'
    )) as typeof import('../../src/virtual-range/demoShots');
    const host = document.createElement('div');
    host.style.cssText = 'position: fixed; left: 0; top: 0; width: 1024px; height: 600px; z-index: 10000';
    document.body.append(host);
    const harness = {
      host,
      shots: demoShots().map((shot) => ({ ...shot, roll: null, totalM: null })),
      flights: [],
      qualities: [],
      fatal: [],
      peak: { calls: 0, triangles: 0, programs: 0 },
    } as unknown as RangeHarness;
    harness.scene = createRangeScene(host, {
      unitSystem: 'metric',
      quality,
      onQualityChange: (level) => harness.qualities.push(level),
      onFlight: (progress) =>
        harness.flights.push({ ...progress, at: performance.now(), animating: harness.scene.stats().animating }),
      onFatalError: (reason) =>
        harness.fatal.push({
          reason: reason instanceof Error ? reason.message : String(reason),
          at: performance.now(),
        }),
    });
    window.__range = harness;
  }, quality);
  await expect.poll(() => stats(page).then(({ frames }) => frames)).toBeGreaterThan(0);
}

function animate(page: Page, shotId: string) {
  return page.evaluate(async (shotId) => {
    const { CAMERA_TIMING, flightSeconds, rollSeconds, shotDuration } = (await import(
      /* @vite-ignore */ '/src/virtual-range/scene/cameraPath.ts'
    )) as typeof import('../../src/virtual-range/scene/cameraPath');
    const { scene, shots } = window.__range;
    const shot = shots.find((candidate) => candidate.id === shotId);
    if (!shot?.flight) throw new Error(`no demo flight ${shotId}`);
    scene.setShots(shots, shotId);
    const flightS = flightSeconds(shot.flight);
    const rollS = rollSeconds(shot.roll);
    return {
      startedAt: performance.now(),
      preS: CAMERA_TIMING.preS,
      flightS,
      rollS,
      endS: shotDuration(flightS, rollS),
    };
  }, shotId);
}

function rollOutTheWedge(page: Page): Promise<{ totalM: number; rest: { x: number; z: number } }> {
  return page.evaluate((points) => {
    const harness = window.__range;
    const roll = points.map(([t, downrange, right, up]) => ({ t, x: right, y: up, z: -downrange }));
    const { x, z } = roll[roll.length - 1];
    const totalM = Math.hypot(x, z);
    harness.shots = harness.shots.map((shot) => (shot.id === 'demo-wedge' ? { ...shot, roll, totalM } : shot));
    return { totalM, rest: { x, z } };
  }, WEDGE_ROLL_M);
}

function recordLandingSignSpots(page: Page): Promise<void> {
  return page.evaluate(async () => {
    const { createSign } = (await import(
      /* @vite-ignore */ '/src/virtual-range/scene/textures.ts'
    )) as typeof import('../../src/virtual-range/scene/textures');
    const vector = Object.getPrototypeOf(createSign(4, 4).sprite.position);
    const { project } = vector;
    const spots: { x: number; z: number; at: number }[] = [];
    window.__range.landingSignSpots = spots;
    vector.project = function (this: { x: number; y: number; z: number }, camera: unknown) {
      if (this.y === 1.6) spots.push({ x: this.x, z: this.z, at: performance.now() });
      return project.call(this, camera);
    };
  });
}

function watchPeakCost(page: Page): Promise<void> {
  return page.evaluate(() => {
    const harness = window.__range;
    window.setInterval(() => {
      const { calls, triangles, programs } = harness.scene.stats();
      const { peak } = harness;
      harness.peak = {
        calls: Math.max(peak.calls, calls),
        triangles: Math.max(peak.triangles, triangles),
        programs: Math.max(peak.programs, programs),
      };
    }, 10);
  });
}

async function expectPeakCostWithinBudget(page: Page): Promise<void> {
  const peak = await page.evaluate(() => window.__range.peak);
  console.log(`PEAK ${JSON.stringify(peak)}`);
  expect(peak.calls).toBeLessThanOrEqual(40);
  expect(peak.triangles).toBeLessThanOrEqual(80_000);
  expect(peak.programs).toBeLessThanOrEqual(20);
}

const stats = (page: Page) => page.evaluate(() => window.__range.scene.stats());
const flights = (page: Page) => page.evaluate(() => window.__range.flights);
const landings = (page: Page) =>
  page.evaluate(() =>
    window.__range.flights
      .filter((progress) => progress.landed)
      .map(({ shotId, carryM, landed }) => ({ shotId, carryM, landed }))
  );
const fatalReasons = (page: Page) => page.evaluate(() => window.__range.fatal.map(({ reason }) => reason));
const demoShot = (page: Page, shotId: string) =>
  page.evaluate((shotId) => window.__range.shots.find((shot) => shot.id === shotId)!, shotId);
const showShots = (page: Page, shots: RangeShot[], animateId: string | null) =>
  page.evaluate(
    async ({ shots, animateId }) => {
      const { scene } = window.__range;
      const before = scene.stats().frames;
      scene.setShots(shots, animateId);
      while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
      return scene.stats();
    },
    { shots, animateId }
  );
const restoreTimeoutMs = (page: Page) =>
  page.evaluate(
    async () =>
      (
        (await import(
          /* @vite-ignore */ '/src/virtual-range/scene/createRangeScene.ts'
        )) as typeof import('../../src/virtual-range/scene/createRangeScene')
      ).RESTORE_TIMEOUT_MS
  );

async function waitUntil(page: Page, pageTimeMs: number): Promise<void> {
  await page.waitForFunction((until) => performance.now() >= until, pageTimeMs, { polling: 100, timeout: 30_000 });
}

async function framesDuringASecondAfter(page: Page, pageTimeMs: number): Promise<number> {
  await waitUntil(page, pageTimeMs);
  const before = await stats(page);
  await page.waitForTimeout(1000);
  const after = await stats(page);
  expect(after.animating).toBe(false);
  return after.frames - before.frames;
}

function loseContext(page: Page): Promise<number> {
  return page.evaluate(async () => {
    const canvas = window.__range.host.querySelector('canvas')!;
    const lost = new Promise((resolve) => canvas.addEventListener('webglcontextlost', resolve, { once: true }));
    const lose = canvas.getContext('webgl2')!.getExtension('WEBGL_lose_context')!;
    window.__range.lose = lose;
    lose.loseContext();
    await lost;
    return performance.now();
  });
}

/** Starts a second scene in its own host while WebGL fails as named: what it threw and what it left behind. */
function startFailingScene(page: Page, failure: 'no-webgl2' | 'link-fails') {
  return page.evaluate(async (failure) => {
    const { createRangeScene } = (await import(
      /* @vite-ignore */ '/src/virtual-range/scene/createRangeScene.ts'
    )) as typeof import('../../src/virtual-range/scene/createRangeScene');
    const canvasProto = HTMLCanvasElement.prototype as unknown as {
      getContext(type: string, options?: unknown): unknown;
    };
    const glProto = WebGL2RenderingContext.prototype;
    const { getContext } = canvasProto;
    const { linkProgram } = glProto;
    const contexts: WebGL2RenderingContext[] = [];
    canvasProto.getContext = function (this: HTMLCanvasElement, type: string, options?: unknown) {
      if (type !== 'webgl2') return getContext.call(this, type, options);
      if (failure === 'no-webgl2') return null;
      const context = getContext.call(this, type, options) as WebGL2RenderingContext;
      contexts.push(context);
      return context;
    };
    if (failure === 'link-fails') {
      glProto.linkProgram = () => {
        throw new Error('link failed');
      };
    }
    const host = document.createElement('div');
    document.body.append(host);
    let error: string | null = null;
    try {
      createRangeScene(host, {
        unitSystem: 'metric',
        quality: 0,
        onQualityChange: () => {},
        onFlight: () => {},
        onFatalError: () => {},
      });
    } catch (thrown) {
      error = (thrown as Error).message;
    } finally {
      canvasProto.getContext = getContext;
      glProto.linkProgram = linkProgram;
    }
    return { error, children: host.childElementCount, lost: contexts.map((context) => context.isContextLost()) };
  }, failure);
}

async function flyIron8ToTheEnd(page: Page): Promise<void> {
  const { startedAt, endS } = await animate(page, IRON8);
  await expect.poll(() => landings(page), { timeout: 20_000 }).toHaveLength(1);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await mountScene(page);
});

test('flies a shot within budget and stops rendering afterwards', async ({ page }) => {
  await watchPeakCost(page);
  // The wedge is the last demo shot, so it flies with the faint tracers of the other two behind it.
  const { startedAt, preS, flightS, endS } = await animate(page, WEDGE);
  await expect.poll(() => landings(page), { timeout: 20_000 }).toHaveLength(1);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  await expectPeakCostWithinBudget(page);

  const progress = await flights(page);
  const landed = progress.filter(({ landed }) => landed);
  expect(landed.map(({ shotId }) => shotId)).toEqual([WEDGE]);
  expect(landed[0].rested, 'without a roll the ball rests where it lands').toBe(true);
  expect(progress.at(-1)?.landed).toBe(true);
  expect(landed[0].at - startedAt).toBeGreaterThanOrEqual((preS + flightS) * 1000 - 100);
  expect(landed[0].at - startedAt).toBeLessThan((preS + flightS) * 1000 + 2000);

  const inFlight = progress.filter(({ landed }) => !landed);
  expect(inFlight.length).toBeGreaterThan(0);
  expect(inFlight.length, 'at most 20 progress calls a second').toBeLessThanOrEqual(Math.ceil(flightS * 20) + 1);
  for (let i = 1; i < inFlight.length; i++) expect(inFlight[i].carryM).toBeGreaterThanOrEqual(inFlight[i - 1].carryM);
  expect(inFlight.at(-1)!.carryM).toBeGreaterThan((await demoShot(page, WEDGE)).carryM - 4);
});

test('rolls a shot out to its rest point within budget, shows its Total there and stops rendering', async ({
  page,
}) => {
  await spyOnFillText(page);
  await recordLandingSignSpots(page);
  await watchPeakCost(page);
  const { programs } = await stats(page);
  const { totalM, rest } = await rollOutTheWedge(page);
  const { startedAt, preS, flightS, rollS, endS } = await animate(page, WEDGE);
  expect(rollS).toBeCloseTo(7.862 - 4.203, 9);

  await waitUntil(page, startedAt + (preS + flightS + rollS / 2) * 1000);
  const rolling = await page.evaluate(() => ({
    animating: window.__range.scene.stats().animating,
    reports: window.__range.flights.map(({ landed, rested }) => ({ landed, rested })),
  }));
  expect(rolling.animating, 'animating while the ball rolls').toBe(true);
  expect(rolling.reports.at(-1), 'landed, not yet at rest').toEqual({ landed: true, rested: false });
  await expect.poll(() => flights(page).then((progress) => progress.at(-1)?.rested), { timeout: 30_000 }).toBe(true);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  await expectPeakCostWithinBudget(page);
  const peak = await page.evaluate(() => window.__range.peak);
  expect(peak.programs, 'the roll line and the landing mark need no shader program of their own').toBe(programs);

  const progress = await flights(page);
  const [touchdown, ...later] = progress.filter(({ landed }) => landed);
  const rested = progress.filter(({ rested }) => rested);
  expect(rested, 'one rest report').toHaveLength(1);
  expect(later, 'after the touchdown only the rest').toEqual(rested);
  expect(touchdown).toMatchObject({ shotId: WEDGE, landed: true, rested: false });
  expect(rested[0]).toMatchObject({ shotId: WEDGE, landed: true, rested: true, animating: true });
  expect(touchdown.at - startedAt).toBeGreaterThanOrEqual((preS + flightS) * 1000 - 100);
  expect(rested[0].at - touchdown.at).toBeGreaterThanOrEqual(rollS * 1000 - 100);
  expect(rested[0].at - touchdown.at).toBeLessThan(rollS * 1000 + 2000);
  expect((await fillTexts(page)).at(-1), 'the sign shows the Total').toBe(`${Math.round(totalM)} m`);

  const spots = await page.evaluate(() => window.__range.landingSignSpots!);
  expect(spots.length, 'the sign is shown').toBeGreaterThan(0);
  expect(spots[0].at, 'from the rest on, not from the touchdown').toBeGreaterThan(rested[0].at - 100);
  expect(new Set(spots.map(({ x, z }) => `${x},${z}`)), 'at the rest point').toEqual(new Set([`${rest.x},${rest.z}`]));
});

test('draws the line to the target and its distance at rest within budget, two more draw calls and no new program', async ({
  page,
}) => {
  await spyOnFillText(page);
  await watchPeakCost(page);
  const { programs } = await stats(page);
  const { rest } = await rollOutTheWedge(page);
  const flag = { x: 8, z: -75 };
  await page.evaluate((flag) => window.__range.scene.setRestTarget(flag), flag);
  const { startedAt, endS } = await animate(page, WEDGE);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  await expectPeakCostWithinBudget(page);
  expect((await page.evaluate(() => window.__range.peak)).programs, 'no shader program of its own').toBe(programs);
  expect(await targetLabels(page)).toEqual([`${Math.round(Math.hypot(flag.x - rest.x, flag.z - rest.z))} m`]);

  const redrawn = (target: { x: number; z: number } | null) =>
    page.evaluate(async (target) => {
      const { scene } = window.__range;
      const before = scene.stats().frames;
      scene.setRestTarget(target);
      while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
      return scene.stats();
    }, target);
  const withLine = await redrawn(flag);
  const without = await redrawn(null);
  expect(withLine.calls, 'the line and its label').toBe(without.calls + 2);
  expect(withLine.programs).toBe(without.programs);
});

test('highlights a green with one more draw call, turns to an aim and flies a turned shot within budget', async ({
  page,
}) => {
  await watchPeakCost(page);
  const resting = await stats(page);
  const highlighted = await page.evaluate(async () => {
    const { scene } = window.__range;
    const before = scene.stats().frames;
    scene.setHighlight(4);
    while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
    return scene.stats();
  });
  expect(highlighted.calls).toBe(resting.calls + 1);
  expect(highlighted.programs).toBe(resting.programs);

  const turnStartedAt = await page.evaluate(() => {
    window.__range.scene.setAim(Math.atan2(-8, 150));
    return performance.now();
  });
  expect((await stats(page)).animating, 'animating while the range turns').toBe(true);
  expect(await framesDuringASecondAfter(page, turnStartedAt + 700)).toBe(0);

  const startedAt = await page.evaluate(
    (bearing) => {
      const { scene, shots } = window.__range;
      scene.setShots(shots, 'demo-wedge', new Map(shots.map((shot) => [shot.id, bearing])));
      return performance.now();
    },
    Math.atan2(-8, 150)
  );
  await expect.poll(() => landings(page), { timeout: 20_000 }).toHaveLength(1);
  const endS = shotDuration(flightSeconds((await demoShot(page, WEDGE)).flight!));
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  await expectPeakCostWithinBudget(page);
  expect(await fatalReasons(page)).toEqual([]);
});

test('cuts to a new shot mid-flight and stops rendering afterwards', async ({ page }) => {
  await animate(page, IRON8);
  await page.waitForTimeout(1000);
  const { startedAt, preS, flightS, endS } = await animate(page, DRIVER);

  await expect.poll(() => landings(page), { timeout: 20_000 }).toHaveLength(1);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  const afterCut = (await flights(page)).filter(({ at }) => at > startedAt);
  expect(new Set(afterCut.map(({ shotId }) => shotId))).toEqual(new Set([DRIVER]));
  expect(afterCut[0].carryM, 'the driver starts from the tee').toBeLessThan(15);
  const landing = afterCut.find(({ landed }) => landed)!;
  expect(landing.at - startedAt).toBeGreaterThanOrEqual((preS + flightS) * 1000 - 100);
  expect((await landings(page)).map(({ shotId }) => shotId)).toEqual([DRIVER]);
});

test('keeps a flight going when the same shots are set again', async ({ page }) => {
  const { startedAt, endS } = await animate(page, IRON8);
  await page.waitForTimeout(1500);
  const setAgainAt = await page.evaluate(() => {
    window.__range.scene.setShots(window.__range.shots, null);
    return performance.now();
  });

  await expect.poll(() => landings(page), { timeout: 20_000 }).toHaveLength(1);
  expect(await framesDuringASecondAfter(page, startedAt + (endS + 1) * 1000)).toBe(0);
  const progress = await flights(page);
  expect(progress.filter(({ landed }) => landed).map(({ shotId }) => shotId)).toEqual([IRON8]);
  expect(progress.filter(({ at }) => at < setAgainAt).length).toBeGreaterThan(0);
  const carries = progress.filter(({ landed }) => !landed).map(({ carryM }) => carryM);
  for (let i = 1; i < carries.length; i++)
    expect(carries[i], 'no restart from the tee').toBeGreaterThanOrEqual(carries[i - 1]);
});

test('drops a flight whose shot is no longer shown', async ({ page }) => {
  await animate(page, IRON8);
  await page.waitForTimeout(500);
  const framesAtSwitch = await page.evaluate(() => {
    const { scene, shots } = window.__range;
    scene.setShots(
      shots.filter((shot) => shot.id !== 'demo-iron8'),
      null
    );
    return scene.stats().frames;
  });

  expect((await stats(page)).animating).toBe(false);
  await page.waitForTimeout(1000);
  expect((await stats(page)).frames - framesAtSwitch, 'one frame for the change, then none').toBe(1);
  expect(await landings(page)).toEqual([]);
});

test('rebuilds once after a lost context and reports the second loss', async ({ page }) => {
  const timeoutMs = await restoreTimeoutMs(page);
  await page.evaluate(() => window.__range.scene.setShots(window.__range.shots, null));
  await expect.poll(() => stats(page).then(({ animating }) => animating)).toBe(false);
  const framesBeforeLoss = (await stats(page)).frames;

  await loseContext(page);
  await page.evaluate(async () => {
    const canvas = window.__range.host.querySelector('canvas')!;
    // Chrome allows a restore only once the lost event has been fully dispatched.
    await new Promise((resolve) => setTimeout(resolve));
    const restored = new Promise((resolve) => canvas.addEventListener('webglcontextrestored', resolve, { once: true }));
    window.__range.lose!.restoreContext();
    await restored;
  });
  await expect.poll(() => stats(page).then(({ frames }) => frames)).toBeGreaterThan(framesBeforeLoss);
  expect((await stats(page)).calls).toBeGreaterThan(0);
  await page.waitForTimeout(timeoutMs + 500);
  expect(await fatalReasons(page), 'the restore cancelled the timeout').toEqual([]);

  await loseContext(page);
  await expect.poll(() => fatalReasons(page)).toEqual(['context-lost']);
});

test('reports a lost context that never comes back', async ({ page }) => {
  const timeoutMs = await restoreTimeoutMs(page);
  const lostAt = await loseContext(page);
  await page.waitForTimeout(500);
  expect(await fatalReasons(page), 'not before the timeout').toEqual([]);

  await expect.poll(() => fatalReasons(page), { timeout: timeoutMs + 3000 }).toEqual(['context-lost']);
  const [{ at }] = await page.evaluate(() => window.__range.fatal);
  expect(at - lostAt).toBeGreaterThanOrEqual(timeoutMs - 50);
});

test('stays silent when disposed while the context is lost', async ({ page }) => {
  const timeoutMs = await restoreTimeoutMs(page);
  await loseContext(page);
  await page.evaluate(() => window.__range.scene.dispose());
  await page.waitForTimeout(timeoutMs + 500);
  expect(await fatalReasons(page)).toEqual([]);
});

test('logs no console errors', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.reload();
  await mountScene(page);

  await flyIron8ToTheEnd(page);
  expect(errors).toEqual([]);
});

test('shows a shot without a flight as a landing dot only', async ({ page }) => {
  const table: RangeShot = { id: 'table', club: '7-iron', ballSpeedMph: 110, carryM: 140, lateralM: -4, flight: null };
  const empty = await showShots(page, [], null);
  const dotOnly = await showShots(page, [table], 'table');

  await expect.poll(() => landings(page)).toEqual([{ shotId: 'table', carryM: 140, landed: true }]);
  expect((await flights(page)).at(-1)?.rested, 'it rests where it lands').toBe(true);
  await page.waitForTimeout(1000);
  const { frames, animating } = await stats(page);
  expect(animating).toBe(false);
  expect(frames - empty.frames, 'one frame for the change, no flight').toBe(1);

  const flight = (await demoShot(page, IRON8)).flight;
  const withFlight = await showShots(page, [{ ...table, flight }], null);
  expect(dotOnly.calls, 'the landing dot').toBe(empty.calls + 1);
  expect(dotOnly.triangles).toBeGreaterThan(empty.triangles);
  expect(withFlight.calls, 'a tracer (core and glow) only with a flight').toBe(dotOnly.calls + 2);
  expect(withFlight.triangles).toBeGreaterThan(dotOnly.triangles);
});

test('draws the landing sign and the tracer when a shot gets its flight and a new carry', async ({ page }) => {
  await spyOnFillText(page);
  const { carryM, lateralM, flight } = await demoShot(page, IRON8);
  const estimate: RangeShot = {
    id: 'enriched',
    club: '7-iron',
    ballSpeedMph: 110,
    carryM: 130,
    lateralM: -4,
    flight: null,
  };
  const enriched: RangeShot = { ...estimate, carryM, lateralM, flight };

  await showShots(page, [estimate], 'enriched');
  const flying = await showShots(page, [enriched], 'enriched');
  const labels = await fillTexts(page);
  const fresh = await showShots(page, [{ ...enriched, id: 'fresh' }], 'fresh');

  expect(labels).toEqual(['130 m', `${Math.round(carryM)} m`]);
  expect(flying.triangles, 'the tracer is the one a new shot would get').toBe(fresh.triangles);
});

test('draws the new flight of a shot whose flight changes, not the one it replaces', async ({ page }) => {
  const iron = await demoShot(page, IRON8);
  const driver = await demoShot(page, DRIVER);
  const shown: RangeShot = { ...iron, id: 'enriched' };
  const changed: RangeShot = { ...driver, id: 'enriched' };

  const before = await showShots(page, [shown], null);
  const after = await showShots(page, [changed], null);
  const fresh = await showShots(page, [{ ...changed, id: 'fresh' }], null);

  expect(after.triangles, 'the driver flight is the longer tracer').toBeGreaterThan(before.triangles);
  expect(after.triangles, 'the tracer is the one a new shot would get').toBe(fresh.triangles);
});

test('keeps the sign and the tracer of a shot that is set again unchanged', async ({ page }) => {
  await spyOnFillText(page);
  const shot: RangeShot = { ...(await demoShot(page, IRON8)), id: 'again' };

  const first = await showShots(page, [shot], null);
  const again = await showShots(page, [shot], null);

  expect(await fillTexts(page)).toEqual([`${Math.round(shot.carryM)} m`]);
  expect(again.triangles).toBe(first.triangles);
});

test('disposes everything mid-flight and ignores later calls', async ({ page }) => {
  await animate(page, IRON8);
  await page.waitForTimeout(500);

  const result = await page.evaluate(async () => {
    const { scene, host, shots } = window.__range;
    const gl = host.querySelector('canvas')!.getContext('webgl2')!;
    scene.dispose();
    scene.dispose();
    scene.setShots(shots, 'demo-driver');
    scene.resize();
    scene.setInsets({ top: 76, bottom: 90 });
    const frames = scene.stats().frames;
    await new Promise((resolve) => setTimeout(resolve, 500));
    return {
      canvases: host.querySelectorAll('canvas').length,
      contextLost: gl.isContextLost(),
      newFrames: scene.stats().frames - frames,
      animating: scene.stats().animating,
    };
  });
  expect(result).toEqual({ canvases: 0, contextLost: true, newFrames: 0, animating: false });
});

test('keeps the distance signs off the chrome: with bars over the whole canvas it draws none', async ({ page }) => {
  // Bars of equal height leave the view where it is, so only the signs can change the draw calls.
  const callsUnder = (insets: { top: number; bottom: number }) =>
    page.evaluate(async (insets) => {
      const { scene } = window.__range;
      const before = scene.stats().frames;
      scene.setInsets(insets);
      while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
      return scene.stats().calls;
    }, insets);

  const open = await callsUnder({ top: 0, bottom: 0 });
  const covered = await callsUnder({ top: 300, bottom: 300 });
  const openAgain = await callsUnder({ top: 0, bottom: 0 });

  expect(covered, 'no sign under the bars').toBeLessThan(open);
  expect(openAgain).toBe(open);
});

test('refuses to start without WebGL2', async ({ page }) => {
  expect(await startFailingScene(page, 'no-webgl2')).toEqual({ error: 'webgl-unavailable', children: 0, lost: [] });
});

test('releases its context and takes its canvas out again when the first compile throws', async ({ page }) => {
  expect(await startFailingScene(page, 'link-fails')).toEqual({ error: 'link failed', children: 0, lost: [true] });
});

test('gives up once when a frame throws, and draws no frame after that', async ({ page }) => {
  await animate(page, IRON8);
  await expect.poll(() => flights(page).then((progress) => progress.length)).toBeGreaterThan(0);

  const framesAtGiveUp = await page.evaluate(async () => {
    const { scene, shots, fatal } = window.__range;
    const glProto = WebGL2RenderingContext.prototype;
    const { clear } = glProto;
    glProto.clear = () => {
      throw new Error('draw failed');
    };
    const deadline = performance.now() + 5000;
    try {
      while (fatal.length === 0 && performance.now() < deadline)
        await new Promise((resolve) => setTimeout(resolve, 20));
    } finally {
      glProto.clear = clear;
    }
    // Drawing works again: a scene that kept its loop would render these.
    scene.setShots(shots, 'demo-driver');
    scene.resize();
    return scene.stats().frames;
  });
  await page.waitForTimeout(1000);

  expect(await fatalReasons(page)).toEqual(['draw failed']);
  expect((await stats(page)).frames, 'frames after giving up').toBe(framesAtGiveUp);
});

test('steps down on slow frames: antialias off, then half the vegetation', async ({ page }) => {
  test.setTimeout(120_000);
  const staticTriangles = () =>
    page.evaluate(async () => {
      const { scene } = window.__range;
      const before = scene.stats().frames;
      scene.setShots([], null);
      while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
      return scene.stats().triangles;
    });
  const fullTriangles = await staticTriangles();
  await page.evaluate(() => window.__range.scene.dispose());
  await mountScene(page, 2);
  const halvedTriangles = await staticTriangles();
  expect(halvedTriangles).toBeLessThan(fullTriangles);

  // Every frame now reports 30 ms (33 fps), so each 2 s window of rendered time has a median above 25 ms.
  await page.addInitScript(() => {
    const requestFrame = window.requestAnimationFrame.bind(window);
    let now = 0;
    window.requestAnimationFrame = (callback) => requestFrame(() => callback((now += 30)));
  });
  await page.reload();
  // The fake clock gives every requestAnimationFrame callback on the page its own 30 ms, so the
  // app's one-time refits once its web font (a•space) has loaded must be over before the flight.
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(500);
  await mountScene(page);
  const view = () =>
    page.evaluate(() => {
      const { host, qualities, scene } = window.__range;
      return {
        qualities: [...qualities],
        animating: scene.stats().animating,
        children: [...host.children].map(({ tagName }) => tagName),
        antialias: [...host.querySelectorAll('canvas')].map(
          (canvas) => canvas.getContext('webgl2')!.getContextAttributes()!.antialias
        ),
      };
    });
  await page.evaluate(() => window.__range.host.append(document.createElement('div')));
  await animate(page, IRON8);

  await expect.poll(() => stats(page).then(({ frames }) => frames), { timeout: 60_000 }).toBeGreaterThan(150);
  expect(await view(), 'decided after 2 s of frames, applied only once the flight is over').toEqual({
    qualities: [],
    animating: true,
    children: ['CANVAS', 'DIV'],
    antialias: [true],
  });
  await expect.poll(() => stats(page).then(({ animating }) => animating), { timeout: 60_000 }).toBe(false);
  expect(await view(), 'one step per flight, the canvas swapped in place').toEqual({
    qualities: [1],
    animating: false,
    children: ['CANVAS', 'DIV'],
    antialias: [false],
  });

  await animate(page, IRON8);
  await expect.poll(() => page.evaluate(() => window.__range.qualities), { timeout: 60_000 }).toEqual([1, 2]);
  expect((await view()).animating).toBe(false);
  expect((await stats(page)).quality).toBe(2);
  expect(await staticTriangles()).toBe(halvedTriangles);
  const frameTimes = await page.evaluate(() => window.__range.scene.frameTimes());
  expect(frameTimes.length).toBeGreaterThanOrEqual(600);
  expect(new Set(frameTimes)).toEqual(new Set([30]));
});
