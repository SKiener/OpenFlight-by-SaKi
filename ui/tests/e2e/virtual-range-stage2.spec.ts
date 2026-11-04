import { expect, test, type Page } from '@playwright/test';
import {
  activeProfileId,
  canvases,
  clippedText,
  clothOf,
  fillTexts,
  footerButton,
  gotoApp,
  hitAShot,
  KIOSK_VIEWPORTS,
  LANDED_TIMEOUT_MS,
  onScreen,
  rangeButton,
  resetSession,
  resetUnits,
  sceneAndCameraSeen,
  SCENE_TIMEOUT_MS,
  serveClubAverages,
  setClub,
  settled,
  signLabel,
  spyOnFillText,
  spyOnRangeCamera,
  spyOnRangeWorld,
  storedTarget,
  targetLabels,
  withControlSocket,
  withShotStore,
} from './helpers';
import { AIMS_KEY, greenBearing, rotateAboutTee } from '../../src/virtual-range/aim';
import { YARD_M } from '../../src/virtual-range/flightPath';
import { TRAINING_KEY } from '../../src/virtual-range/game/training';
import { GREENS } from '../../src/virtual-range/layout';
import type { Shot } from '../../src/types/shot';

type WirePoint = [t: number, downrange: number, right: number, up: number];

interface RolledShot {
  id: string;
  totalYards: number;
  landing: WirePoint;
  rest: WirePoint;
  rollS: number;
}

interface DrawCallSpy {
  calls: number;
  frames: { at: number; calls: number }[];
  restAt: number | null;
}

const SELECTED = 4;
const DRAW_CALL_BUDGET = 40;
const barValue = (page: Page, kind: string) => page.locator(`.virtual-range__stat--${kind} .virtual-range__value`);

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

async function openGermanRange(page: Page, path = '/'): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
  await gotoApp(page, path);
  if (path === '/') {
    await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
    await spyOnRangeCamera(page);
    await spyOnRangeWorld(page);
    await rangeButton(page).tap();
  }
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
}

async function rolledShot(page: Page): Promise<RolledShot> {
  const read = () =>
    withShotStore(page, (store) => {
      const shot = store.getState().shots.at(-1) as Shot | undefined;
      const flight = shot?.flight_path?.points as WirePoint[] | undefined;
      const roll = shot?.roll_path?.points as WirePoint[] | undefined;
      if (!shot || shot.carry_spin_adjusted == null || typeof shot.total_yards !== 'number' || !flight || !roll) {
        return null;
      }
      return {
        id: shot.timestamp,
        totalYards: shot.total_yards,
        landing: flight[flight.length - 1],
        rest: roll[roll.length - 1],
        rollS: roll[roll.length - 1][0] - roll[0][0],
      };
    });
  await expect.poll(read, { message: 'a final shot with a roll', timeout: LANDED_TIMEOUT_MS }).not.toBeNull();
  return (await read())!;
}

async function tapGreen(page: Page, index: number): Promise<void> {
  await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);
  const at = await onScreen(page, clothOf(index));
  await page.touchscreen.tap(at.x, at.y);
}

const storedAims = (page: Page): Promise<unknown> =>
  page.evaluate((key) => JSON.parse(sessionStorage.getItem(key) ?? 'null'), AIMS_KEY);

function yardsToFlag(rest: WirePoint, index: number): number {
  const turned = rotateAboutTee({ x: rest[2] * YARD_M, z: -rest[1] * YARD_M }, greenBearing(GREENS[index]));
  return Math.round(Math.hypot(GREENS[index].sideM - turned.x, -GREENS[index].distanceM - turned.z) / YARD_M);
}

function switchUnits(page: Page, unitSystem: string): Promise<void> {
  return page.evaluate(async (unitSystem) => {
    const path = '/src/stores/useUnitPreferenceStore.ts';
    const { useUnitPreferenceStore } = await import(/* @vite-ignore */ path);
    useUnitPreferenceStore.getState().setUnitSystem(unitSystem);
  }, unitSystem);
}

function countDrawCalls(page: Page): Promise<void> {
  return page.addInitScript(() => {
    const spy: DrawCallSpy = { calls: 0, frames: [], restAt: null };
    (window as unknown as { __drawCalls: DrawCallSpy }).__drawCalls = spy;
    const prototype = WebGL2RenderingContext.prototype as unknown as Record<string, (...args: unknown[]) => unknown>;
    for (const name of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
      const draw = prototype[name];
      prototype[name] = function (this: unknown, ...args: unknown[]) {
        spy.calls += 1;
        return draw.apply(this, args);
      };
    }
    const request = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (callback) =>
      request((time) => {
        spy.calls = 0;
        callback(time);
        if (spy.calls > 0) spy.frames.push({ at: performance.now(), calls: spy.calls });
      });
    new MutationObserver(() => {
      const total = document.querySelector('.virtual-range__stat--total .virtual-range__value')?.textContent ?? '';
      if (spy.restAt === null && /\d/.test(total)) spy.restAt = performance.now();
    }).observe(document, { childList: true, characterData: true, subtree: true });
  });
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`range stage 2 at ${viewport.width}×${viewport.height} in German`, () => {
    test.use({ viewport });

    test('a shot rolls out past or short of its landing; the sign, the bar and the Live Total tile show its Total', async ({
      page,
    }) => {
      // The kiosk's Live tiles with the Total among them (its notes, "Total … yds" under the carry, live in All values).
      await page.addInitScript(() =>
        localStorage.setItem('openflight.kiosk-live-slots:v1', JSON.stringify(['carry', 'total', 'launch_v', 'launch_h', 'curve']))
      );
      await openGermanRange(page);
      await spyOnFillText(page);
      await hitAShot(page);
      const shot = await rolledShot(page);
      const total = Math.round(shot.totalYards);

      await expect(barValue(page, 'total')).toHaveText(`${total}yds`, { timeout: LANDED_TIMEOUT_MS });
      const rolledM = (shot.rest[1] - shot.landing[1]) * YARD_M;
      expect(Math.abs(rolledM), 'the rest point is not the landing').toBeGreaterThan(0.1);
      expect(Math.sign(rolledM), 'the roll goes the way the Total says').toBe(
        Math.sign(Math.hypot(shot.rest[1], shot.rest[2]) - Math.hypot(shot.landing[1], shot.landing[2]))
      );
      expect(await fillTexts(page), 'the sign at the rest point').toContain(
        await signLabel(page, shot.totalYards * YARD_M)
      );

      await footerButton(page, 'Live').tap();
      await expect(page.locator('[data-metric="total"] .kiosk-tile__value')).toHaveText(String(total));
      await expect(page.locator('[data-metric="total"] .kiosk-tile__title')).toHaveText(/^Total (≈ )?yds$/);
      expect(await clippedText(page, '.kiosk-tile__value-row, .kiosk-tile__title')).toEqual([]);
    });

    test('a shot at a tapped green draws its line label at rest; the bar shows Total and to target, also after a reload', async ({
      page,
    }) => {
      await openGermanRange(page);
      await tapGreen(page, SELECTED);
      await expect.poll(() => storedTarget(page)).toBe(SELECTED);
      await spyOnFillText(page);
      await hitAShot(page);
      const shot = await rolledShot(page);
      const total = `${Math.round(shot.totalYards)}yds`;
      const toTarget = yardsToFlag(shot.rest, SELECTED);

      await expect(barValue(page, 'total')).toHaveText(total, { timeout: LANDED_TIMEOUT_MS });
      await expect(barValue(page, 'target')).toHaveText(`${toTarget}yds`);
      await expect
        .poll(() => targetLabels(page), { message: 'the label of the line to the flag' })
        .toContain(`${toTarget} yds`);
      expect(await clippedText(page, '.virtual-range__value')).toEqual([]);
      const aims = (await storedAims(page)) as [string, number][];
      expect(aims.map(([id]) => id)).toEqual([shot.id]);
      expect(aims[0][1], 'the shot keeps the bearing of the green').toBeCloseTo(greenBearing(GREENS[SELECTED]), 12);

      await page.reload();
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      expect(await storedTarget(page), 'the selection survives the reload').toBe(SELECTED);
      expect(await storedAims(page), 'and so does the bearing of the earlier shot').toEqual(aims);
      await expect(barValue(page, 'total')).toHaveText(total);
      await expect(barValue(page, 'target'), 'the earlier rest point, turned as before').toHaveText(`${toTarget}yds`);
    });

    test('a demo shot lands, rolls out and rests at its Total', async ({ page }) => {
      await openGermanRange(page, '/?rangeDemo=1');
      const { carryM, totalM, rollS } = await page.evaluate(async () => {
        const path = '/src/virtual-range/demoShots.ts';
        const { demoShots } = await import(/* @vite-ignore */ path);
        const [iron] = demoShots();
        return {
          carryM: iron.carryM as number,
          totalM: iron.totalM as number,
          rollS: iron.roll.at(-1).t - iron.roll[0].t,
        };
      });
      const carry = `${Math.round(carryM / YARD_M)}yds`;
      const total = `${Math.round(totalM / YARD_M)}yds`;
      expect(Math.round(totalM / YARD_M), 'the iron rolls on').toBeGreaterThan(Math.round(carryM / YARD_M));
      await page.evaluate(
        ([carry, total]) => {
          const times: { landedAt?: number; restedAt?: number } = {};
          (window as unknown as { __demoTimes: typeof times }).__demoTimes = times;
          const text = (kind: string) =>
            document.querySelector(`.virtual-range__stat--${kind} .virtual-range__value`)?.textContent ?? '';
          new MutationObserver(() => {
            if (times.landedAt === undefined && text('carry') === carry) times.landedAt = performance.now();
            if (times.restedAt === undefined && text('total') === total) times.restedAt = performance.now();
          }).observe(document.querySelector('.virtual-range')!, {
            childList: true,
            characterData: true,
            subtree: true,
          });
        },
        [carry, total] as const
      );
      await expect(page.locator('.virtual-range__club')).toHaveText('Eisen 8');

      await expect(barValue(page, 'total')).toHaveText(total, { timeout: LANDED_TIMEOUT_MS });
      await expect(barValue(page, 'carry')).toHaveText(carry);
      const { landedAt, restedAt } = await page.evaluate(
        () => (window as unknown as { __demoTimes: { landedAt?: number; restedAt?: number } }).__demoTimes
      );
      expect(landedAt, 'the carry counted up to the landing').toBeDefined();
      expect(restedAt! - landedAt!, 'the Total waits for the roll').toBeGreaterThan(rollS * 1000 - 300);
    });

    test('the roll-out stays within the draw-call budget', async ({ page }) => {
      await countDrawCalls(page);
      await openGermanRange(page);
      await hitAShot(page);
      const shot = await rolledShot(page);
      await expect(barValue(page, 'total')).toHaveText(`${Math.round(shot.totalYards)}yds`, {
        timeout: LANDED_TIMEOUT_MS,
      });

      const { frames, restAt } = await page.evaluate(
        () => (window as unknown as { __drawCalls: DrawCallSpy }).__drawCalls
      );
      expect(restAt).not.toBeNull();
      const rolling = frames.filter(({ at }) => at > restAt! - shot.rollS * 1000 && at <= restAt!);
      expect(rolling.length, 'frames drawn while the ball rolls').toBeGreaterThan(5);
      expect(Math.max(...rolling.map(({ calls }) => calls)), 'draw calls in a rolling frame').toBeLessThanOrEqual(
        DRAW_CALL_BUDGET
      );
      expect(Math.max(...frames.map(({ calls }) => calls)), 'draw calls in any frame').toBeLessThanOrEqual(
        DRAW_CALL_BUDGET
      );
    });
  });
}

test('a unit switch at rest relabels the line to the training target, which stays the same in metres', async ({
  page,
}) => {
  await serveClubAverages(page, { club: 'driver', total_yards: 150, total_count: 5, carry_yards: 0, carry_count: 0 });
  await openGermanRange(page);
  await page.getByRole('button', { name: 'Training', exact: true }).tap();
  const profileId = await activeProfileId(page);
  const targetM = Math.round(150 * YARD_M);
  const storedTargetM = () =>
    page.evaluate(([key, profileId]) => JSON.parse(localStorage.getItem(key) ?? '{}')[profileId]?.targetM ?? null, [
      TRAINING_KEY,
      profileId,
    ] as const);
  await expect.poll(storedTargetM).toBe(targetM);
  await spyOnFillText(page);
  await hitAShot(page);
  const shot = await rolledShot(page);
  await expect(barValue(page, 'total')).toHaveText(`${Math.round(shot.totalYards)}yds`, {
    timeout: LANDED_TIMEOUT_MS,
  });
  await settled(page);
  const toTargetM = Math.hypot(shot.rest[2] * YARD_M, targetM - shot.rest[1] * YARD_M);
  const [yards, metres] = [await signLabel(page, toTargetM), await signLabel(page, toTargetM, 'metric')];
  expect(yards).toMatch(/ yds$/);
  expect(metres).toMatch(/ m$/);
  await expect.poll(() => targetLabels(page)).toContain(yards);
  await expect(page.locator('.virtual-range__training-value')).toHaveText(`${Math.round(targetM / YARD_M)}yds`);

  await spyOnFillText(page);
  await switchUnits(page, 'metric');

  await expect
    .poll(() => targetLabels(page), { message: 'the label of the line to the training flag' })
    .toContain(metres);
  expect((await targetLabels(page)).filter((label) => !label.endsWith(' m'))).toEqual([]);
  await expect(barValue(page, 'target')).toHaveText(metres.replace(' ', ''));
  await expect(page.locator('.virtual-range__training-value')).toHaveText(`${targetM}m`);
  expect(await storedTargetM(), 'the training target stays in metres').toBe(targetM);
});
