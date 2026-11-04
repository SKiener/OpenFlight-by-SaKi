import { expect, test, type Page } from '@playwright/test';
import {
  activeProfileId,
  canvases,
  clippedText,
  clothOf,
  covered,
  footerButton,
  freeArea,
  gotoApp,
  greenHighlighted,
  hitAShot,
  KIOSK_VIEWPORTS,
  LANDED_TIMEOUT_MS,
  onScreen,
  overlapping,
  rangeButton,
  resetSession,
  resetUnits,
  sceneAndCameraSeen,
  SCENE_TIMEOUT_MS,
  serveClubAverages,
  setClub,
  setDemo,
  settled,
  simulateShot,
  smallTargets,
  spyOnRangeCamera,
  spyOnRangeWorld,
  withControlSocket,
  withShotStore,
  worldTurn,
} from './helpers';
import { greenBearing, TARGET_KEY } from '../../src/virtual-range/aim';
import { YARD_M } from '../../src/virtual-range/flightPath';
import { TRAINING_KEY } from '../../src/virtual-range/game/training';
import type { Shot } from '../../src/types/shot';
import { GREENS } from '../../src/virtual-range/layout';
import type { RangeScene } from '../../src/virtual-range/scene/createRangeScene';

interface Snapshot {
  total: string;
  result: string;
}

declare global {
  interface Window {
    __snapshots?: Snapshot[];
    __budget?: { scene: RangeScene };
  }
}

const SELECTED = 4;
const READY = 'Schlag aufs Ziel';
const AVERAGES = { club: 'driver', total_yards: 150, total_count: 5, carry_yards: 140, carry_count: 5 };
const START_M = Math.round(150 * YARD_M);
const PANEL = '.virtual-range__training';
const RESULT = '.virtual-range__training-result strong';
const FIGURES = '.virtual-range__training-result span';

test.use({ hasTouch: true, viewport: { width: 1024, height: 600 } });

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await resetSession(socket);
    await resetUnits(socket);
    await setClub(socket, 'driver');
  });
});

test.afterAll(async () => {
  await withControlSocket((socket) => resetUnits(socket));
});

const marks = (page: Page) => page.evaluate(() => window.__rangeWorld!.marks());
const stored = (page: Page, key: string, storage: 'local' | 'session' = 'local'): Promise<unknown> =>
  page.evaluate(
    ([key, storage]) => JSON.parse((storage === 'local' ? localStorage : sessionStorage).getItem(key) ?? 'null'),
    [key, storage] as const
  );

const storedTraining = async (page: Page): Promise<Record<string, { targetM: number; recent: unknown[] }>> =>
  (await stored(page, TRAINING_KEY)) as Record<string, { targetM: number; recent: unknown[] }>;

async function clothOnTheCanvas(page: Page): Promise<{ x: number; y: number }> {
  for (const index of GREENS.keys()) {
    if (index === SELECTED) continue;
    const cloth = await onScreen(page, clothOf(index));
    const tag = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.tagName, cloth);
    if (tag === 'CANVAS') return cloth;
  }
  throw new Error('no flag cloth to tap on the canvas');
}

async function openTrainingRange(page: Page): Promise<string[]> {
  const asked = await serveClubAverages(page, AVERAGES);
  await page.addInitScript(
    ([key, selected]) => {
      localStorage.setItem('openflight.locale:v1', 'de');
      if (sessionStorage.getItem(key) === null) sessionStorage.setItem(key, selected);
    },
    [TARGET_KEY, String(SELECTED)] as const
  );
  await gotoApp(page);
  await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
  await spyOnRangeCamera(page);
  await spyOnRangeWorld(page);
  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);
  await expect.poll(() => worldTurn(page)).toBeCloseTo(greenBearing(GREENS[SELECTED]), 12);
  return asked;
}

async function expectPanelFits(page: Page, label: string): Promise<void> {
  const free = await freeArea(page);
  const box = (await page.locator(PANEL).boundingBox())!;
  expect(box.y, label).toBeGreaterThanOrEqual(free.top);
  expect(box.y + box.height, label).toBeLessThanOrEqual(free.bottom);
  expect(box.x, label).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width, label).toBeLessThanOrEqual(free.width);
  expect(await smallTargets(page, `${PANEL} button`), label).toEqual([]);
  expect(await covered(page, `${PANEL} button`), label).toEqual([]);
  expect(
    await clippedText(
      page,
      `${RESULT}, ${FIGURES}, ${PANEL} .virtual-range__label, ${PANEL} button, .virtual-range__training-value`
    ),
    label
  ).toEqual([]);
  expect(
    await overlapping(
      page,
      PANEL,
      '.range-app__top, .range-footer, .virtual-range__bar, .virtual-range__hint, .simulate-bubble'
    ),
    label
  ).toEqual([]);
}

function recordSnapshots(page: Page): Promise<void> {
  return page.evaluate(
    ([result]) => {
      const snapshots: Snapshot[] = [];
      window.__snapshots = snapshots;
      const take = () =>
        snapshots.push({
          total: document.querySelector('.virtual-range__stat--total .virtual-range__value')?.textContent ?? '',
          result: document.querySelector(result)?.textContent ?? '',
        });
      new MutationObserver(take).observe(document.querySelector('.virtual-range')!, {
        childList: true,
        characterData: true,
        subtree: true,
      });
    },
    [RESULT] as const
  );
}

function expectedResult(page: Page, targetM: number): Promise<string> {
  return withShotStore(
    page,
    (store, targetM) => {
      const shot = store.getState().shots.at(-1)!;
      const points = (shot.roll_path?.points?.length ? shot.roll_path : shot.flight_path)!.points;
      const downrangeM = points[points.length - 1][1] * 0.9144;
      const deviation = (downrangeM - targetM) / 0.9144;
      const distance = Math.abs(deviation).toLocaleString('de-DE', {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1,
        useGrouping: false,
      });
      return `${distance} yds ${deviation < 0 ? 'kurz' : 'lang'}`;
    },
    targetM
  );
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`distance training at ${viewport.width}×${viewport.height} in German`, () => {
    test.use({ viewport });

    test('sets the target with the buttons, scores a shot at rest and ends back in free play', async ({
      page,
    }, testInfo) => {
      const asked = await openTrainingRange(page);
      expect(await greenHighlighted(page)).toBe(true);
      expect(await marks(page)).toEqual({ flag: null, zones: false });

      await page.getByRole('button', { name: 'Training', exact: true }).tap();
      await expect(page.locator(PANEL)).toBeVisible();
      const profileId = await activeProfileId(page);
      expect(profileId).not.toBe('');
      expect(asked).toEqual([`driver/${profileId}`]);
      await expect(page.locator('.virtual-range__training-value')).toHaveText(`${Math.round(START_M / YARD_M)}yds`);
      await expect(page.locator(RESULT)).toHaveText(READY);
      await expect(page.locator(FIGURES)).toHaveCount(0);

      await page.getByRole('button', { name: 'Ziel 10 m weiter' }).tap();
      await page.getByRole('button', { name: 'Ziel 1 m weiter' }).tap();
      await page.getByRole('button', { name: 'Ziel 1 m weiter' }).tap();
      await page.getByRole('button', { name: 'Ziel 1 m näher' }).tap();
      const targetM = START_M + 11;
      await expect(page.locator('.virtual-range__training-value')).toHaveText(`${Math.round(targetM / YARD_M)}yds`);
      await expect.poll(() => storedTraining(page)).toEqual({ [profileId]: { targetM, recent: [] } });

      await expect.poll(() => worldTurn(page), { message: 'training aims straight ahead' }).toBe(0);
      expect(await greenHighlighted(page)).toBe(false);
      await expect.poll(() => marks(page)).toMatchObject({ flag: { x: 0, z: -targetM }, zones: true });
      await settled(page);
      const free = await freeArea(page);
      const flag = await onScreen(page, { x: 0, y: 0, z: -targetM });
      expect(Math.abs(flag.x - free.width / 2), `flag at ${flag.x}`).toBeLessThanOrEqual(3);
      expect(flag.y).toBeGreaterThan(free.top);
      expect(flag.y).toBeLessThan(free.bottom);
      if (viewport.width === 1024) await page.screenshot({ path: testInfo.outputPath('t9-training-1024x600.png') });

      const cloth = await clothOnTheCanvas(page);
      await page.touchscreen.tap(cloth.x, cloth.y);
      await page.waitForTimeout(700);
      expect(await stored(page, TARGET_KEY, 'session'), 'a green tap selects nothing in training').toBe(SELECTED);
      expect(await worldTurn(page)).toBe(0);
      await expectPanelFits(page, 'before the shot');

      await recordSnapshots(page);
      await hitAShot(page);
      await expect(page.locator(RESULT)).toHaveText(/^\d+,\d yds (kurz|lang) · (Volltreffer|Gut|Getroffen|Daneben)$/, {
        timeout: LANDED_TIMEOUT_MS,
      });
      await expect(page.locator(RESULT)).toContainText(await expectedResult(page, targetM));
      await expect(page.locator(FIGURES)).toHaveText(/^Ø \d+,\d yds (kurz|lang) · (0|100)\s%\sgetroffen$/);
      const snapshots = await page.evaluate(() => window.__snapshots!);
      expect(
        snapshots.filter(({ total, result }) => result !== READY && total === '—'),
        'the result waits until the ball rests'
      ).toEqual([]);
      expect(snapshots.some(({ result }) => result === READY)).toBe(true);
      expect((await storedTraining(page))[profileId].recent).toHaveLength(1);
      await expectPanelFits(page, 'after the shot');
      if (viewport.width === 800) {
        await page.screenshot({ path: testInfo.outputPath(`t9-training-${viewport.width}x${viewport.height}.png`) });
      }

      await page.evaluate(
        ([result, figures]) => {
          document.querySelector(result)!.textContent = '164,0 yds kurz · Daneben';
          document.querySelector(figures)!.textContent = 'Ø 109,4 yds kurz · 90 % getroffen';
          document.querySelector('.virtual-range__training-value')!.firstChild!.textContent = '328';
        },
        [RESULT, FIGURES] as const
      );
      await expectPanelFits(page, 'with the widest texts');

      await page.getByRole('button', { name: 'Beenden', exact: true }).tap();
      await expect(page.locator(PANEL)).toHaveCount(0);
      await expect(page.getByRole('button', { name: 'Training', exact: true })).toBeVisible();
      expect(await smallTargets(page, '.virtual-range__mode')).toEqual([]);
      expect(await storedTraining(page), "ends only this player's training").toEqual({});
      await expect.poll(() => worldTurn(page)).toBeCloseTo(greenBearing(GREENS[SELECTED]), 12);
      expect(await greenHighlighted(page)).toBe(true);
      expect(await marks(page)).toEqual({ flag: null, zones: false });
    });
  });
}

test('a shot hit while the range is closed never counts; the next one on the range does', async ({ page }) => {
  await openTrainingRange(page);
  await page.getByRole('button', { name: 'Training', exact: true }).tap();
  await expect(page.locator(PANEL)).toBeVisible();
  const profileId = await activeProfileId(page);

  await footerButton(page, 'Live').tap();
  await expect(page.locator('.virtual-range')).toHaveCount(0);
  await withControlSocket((socket) => simulateShot(socket));
  await expect
    .poll(() =>
      withShotStore(
        page,
        (store) => store.getState().shots.filter((shot: Shot) => shot.carry_spin_adjusted != null).length
      )
    )
    .toBe(1);
  await rangeButton(page).tap();
  await expect(page.locator(PANEL)).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(page.locator(RESULT)).toHaveText(READY);
  expect(await storedTraining(page)).toEqual({ [profileId]: { targetM: START_M, recent: [] } });

  await hitAShot(page);
  await expect(page.locator(RESULT)).not.toHaveText(READY, { timeout: LANDED_TIMEOUT_MS });
  expect((await storedTraining(page))[profileId].recent).toHaveLength(1);
});

test('the server demo scores in memory only; once it ends, the training before it is back', async ({ page }) => {
  test.setTimeout(90_000);
  await openTrainingRange(page);
  await page.getByRole('button', { name: 'Training', exact: true }).tap();
  await expect(page.locator(PANEL)).toBeVisible();
  const before = { [await activeProfileId(page)]: { targetM: START_M, recent: [] } };
  await expect.poll(() => storedTraining(page)).toEqual(before);

  try {
    await withControlSocket((socket) => setDemo(socket, true));
    await expect(page.locator(RESULT), 'a demo shot is scored').not.toHaveText(READY, { timeout: LANDED_TIMEOUT_MS });
    await expect(page.locator(FIGURES)).toHaveCount(1);
    expect(await storedTraining(page), 'but not stored').toEqual(before);
  } finally {
    await withControlSocket((socket) => setDemo(socket, false));
  }

  await expect(page.locator(RESULT)).toHaveText(READY);
  await expect(page.locator(FIGURES)).toHaveCount(0);
  expect(await storedTraining(page)).toEqual(before);
});

test('the training flag and the zone bands stay within the draw-call and program budget', async ({ page }) => {
  await page.goto('/');
  const cost = await page.evaluate(async () => {
    const scenePath = '/src/virtual-range/scene/createRangeScene.ts';
    const demoPath = '/src/virtual-range/demoShots.ts';
    const { createRangeScene } = (await import(
      scenePath
    )) as typeof import('../../src/virtual-range/scene/createRangeScene');
    const { demoShots } = (await import(demoPath)) as typeof import('../../src/virtual-range/demoShots');
    const host = document.createElement('div');
    host.style.cssText = 'position: fixed; left: 0; top: 0; width: 1024px; height: 600px; z-index: 10000';
    document.body.append(host);
    const scene = createRangeScene(host, {
      unitSystem: 'metric',
      quality: 0,
      onQualityChange: () => {},
      onFlight: () => {},
      onFatalError: () => {},
    });
    window.__budget = { scene };
    const frame = async () => {
      const before = scene.stats().frames;
      while (scene.stats().frames === before) await new Promise((resolve) => setTimeout(resolve, 20));
      return scene.stats();
    };
    await frame();
    const shots = demoShots();
    scene.setRestTarget({ x: 0, z: -150 });
    scene.setShots(shots, null);
    const free = await frame();
    scene.setTrainingTarget(150);
    const training = await frame();
    scene.setTrainingTarget(300);
    const far = await frame();
    return { free, training, far };
  });
  console.log(`TRAINING COST ${JSON.stringify(cost)}`);
  expect(cost.training.calls - cost.free.calls).toBeGreaterThanOrEqual(2);
  expect(cost.training.calls - cost.free.calls).toBeLessThanOrEqual(3);
  expect(cost.training.programs).toBe(cost.free.programs);
  for (const { calls, triangles, programs } of [cost.training, cost.far]) {
    expect(calls).toBeLessThanOrEqual(40);
    expect(triangles).toBeLessThanOrEqual(80_000);
    expect(programs).toBeLessThanOrEqual(20);
  }
});
