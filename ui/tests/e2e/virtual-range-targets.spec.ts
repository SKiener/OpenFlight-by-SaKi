import { expect, test, type Page } from '@playwright/test';
import {
  canvases,
  clippedText,
  gotoApp,
  greenHighlighted,
  KIOSK_VIEWPORTS,
  outsideScreen,
  overlapping,
  rangeButton,
  resetSession,
  resetUnits,
  sceneAndCameraSeen,
  SCENE_TIMEOUT_MS,
  setClub,
  settled,
  smallTargets,
  spyOnRangeCamera,
  spyOnRangeWorld,
  storedTarget,
  withControlSocket,
  worldTurn,
} from './helpers';
import { greenBearing } from '../../src/virtual-range/aim';
import { YARD_M } from '../../src/virtual-range/flightPath';
import { GREENS } from '../../src/virtual-range/layout';

const PICKED = 4;
const CONTROLS = '.virtual-range__target-button, .virtual-range__target-row';

test.use({ hasTouch: true });

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

const button = (page: Page) => page.locator('.virtual-range__target-button');
const list = (page: Page) => page.locator('.virtual-range__targets');
const row = (page: Page, name: string) => list(page).getByRole('button', { name, exact: true });

async function openGermanRange(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
  await gotoApp(page);
  await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
  await spyOnRangeCamera(page);
  await spyOnRangeWorld(page);
  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await setUnits(page, 'metric');
  await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);
}

function setUnits(page: Page, unitSystem: string): Promise<void> {
  return page.evaluate(async (unitSystem) => {
    const path = '/src/stores/useUnitPreferenceStore.ts';
    const { useUnitPreferenceStore } = await import(/* @vite-ignore */ path);
    useUnitPreferenceStore.getState().setUnitSystem(unitSystem);
  }, unitSystem);
}

async function expectEveryCellShown(page: Page): Promise<void> {
  await expect(list(page).getByRole('button')).toHaveCount(9);
  expect(await outsideScreen(page, '.virtual-range__targets, .virtual-range__target-row')).toEqual([]);
  expect(
    await overlapping(page, '.virtual-range__targets, .virtual-range__target-row', '.range-app__top, .range-footer')
  ).toEqual([]);
  expect(
    await list(page).evaluate((element) => element.scrollHeight <= element.clientHeight),
    'the panel does not scroll'
  ).toBe(true);
}

async function turnedTo(page: Page, index: number | null): Promise<void> {
  await expect.poll(() => storedTarget(page)).toBe(index);
  await expect.poll(() => worldTurn(page)).toBeCloseTo(index === null ? 0 : greenBearing(GREENS[index]), 12);
  await settled(page);
}

async function signsOnTheList(page: Page): Promise<string[]> {
  const signs = await page.evaluate(() => window.__rangeWorld!.signRects());
  const box = (await list(page).boundingBox())!;
  expect(signs.length, 'signs to check').toBeGreaterThan(0);
  return signs
    .filter(
      (sign) =>
        sign.left < box.x + box.width && sign.right > box.x && sign.top < box.y + box.height && sign.bottom > box.y
    )
    .map((sign) => `sign at ${sign.name}`);
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`the target list at ${viewport.width}×${viewport.height} in German`, () => {
    test.use({ viewport });

    test('lists the greens to pick from, keeps the signs off it, turns the Range and releases', async ({
      page,
    }, testInfo) => {
      await openGermanRange(page);
      await expect(button(page)).toHaveText('Ziel');
      await button(page).tap();
      await expect(list(page)).toBeVisible();
      await expect(list(page).getByRole('button')).toHaveText([
        ...GREENS.map(({ distanceM }) => `●${distanceM} m`),
        'Kein Ziel✓',
      ]);

      expect(await smallTargets(page, CONTROLS)).toEqual([]);
      expect(await clippedText(page, CONTROLS)).toEqual([]);
      await expectEveryCellShown(page);
      await settled(page);
      expect(await signsOnTheList(page), 'with the list open').toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`targets-${viewport.width}x${viewport.height}.png`) });

      await setUnits(page, 'imperial');
      await expect(list(page).getByRole('button')).toHaveText([
        ...GREENS.map(({ distanceM }) => `●${Math.round(distanceM / YARD_M)} yds`),
        'Kein Ziel✓',
      ]);
      expect(await smallTargets(page, CONTROLS), 'imperial').toEqual([]);
      expect(await clippedText(page, CONTROLS), 'imperial').toEqual([]);
      await expectEveryCellShown(page);
      if (viewport.height === 400) {
        await page.screenshot({
          path: testInfo.outputPath(`targets-${viewport.width}x${viewport.height}-imperial.png`),
        });
      }
      await setUnits(page, 'metric');

      await row(page, `${GREENS[PICKED].distanceM} m`).tap();
      await expect(list(page)).toHaveCount(0);
      await turnedTo(page, PICKED);
      expect(await greenHighlighted(page)).toBe(true);
      await expect(button(page)).toHaveText(`Ziel ${GREENS[PICKED].distanceM} m`);

      await button(page).tap();
      await expect(row(page, `${GREENS[PICKED].distanceM} m`)).toHaveAttribute('aria-pressed', 'true');
      await page.keyboard.press('Escape');
      await expect(list(page), 'Escape closes').toHaveCount(0);
      expect(await storedTarget(page), 'without a change').toBe(PICKED);

      await button(page).tap();
      await row(page, 'Kein Ziel').tap();
      await expect(list(page)).toHaveCount(0);
      await turnedTo(page, null);
      expect(await greenHighlighted(page)).toBe(false);
      await expect(button(page)).toHaveText('Ziel');
    });
  });
}
