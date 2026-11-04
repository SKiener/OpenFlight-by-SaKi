import { test, expect, type Page } from '@playwright/test';
import { brokenWords, clippedText, KIOSK_VIEWPORTS, PHONE_VIEWPORTS, smallTargets } from './helpers';
import { phoneOrigin } from './phoneFixture';

/**
 * The club and implement pickers, read from 1-2 m on the kiosk: every label of a tab
 * shares one size, as large as the tiles allow, never clipped and never broken inside
 * a word. Phones keep their own sizes as the ceiling and must not clip either.
 */

/** The 14-club bag of range-touch-layout.spec.ts: two rows of tiles (Eisen has six). */
const BAG_14 = ['wedge-58', 'wedge-54', 'wedge-48', 'wedge-44', '9-iron', '8-iron', '7-iron', '6-iron', '5-iron', '4-iron', '7-wood', '3-wood', 'mini-driver', 'driver'];

const OPTION = '.picker-overlay__option';
const TAB = '.picker-overlay__tabs .panel-action';

interface Setup {
  locale: 'de' | 'en';
  mode?: 'rolling-buffer' | 'swing-speed';
  /** Club ids of the bag; omitted: every club (Eisen has twelve, three rows). */
  bag?: string[];
  /** The kiosk's own host (127.0.0.1) or a phone's. */
  kiosk: boolean;
}

async function openPicker(page: Page, { locale, mode = 'rolling-buffer', bag, kiosk }: Setup) {
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.addInitScript((locale) => localStorage.setItem('openflight.locale:v1', locale), locale);
  const base = test.info().project.use.baseURL!;
  await page.goto(`${kiosk ? base : phoneOrigin(base)}/`);
  await page.locator('.range-app').waitFor();
  await page.evaluate(
    async ({ mode, bag }) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      (await load('services/socketService')).socketService.disconnect();
      (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
        profiles: [{ id: 'picker', name: 'Golfer', settings: { unit_system: 'metric', ...(bag ? { club_ids: bag } : {}) } }],
        active_profile_id: 'picker',
      });
      (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
      (await load('stores/useDebugStore')).useDebugStore.setState((state: { triggerStatus: object }) => ({
        triggerStatus: { ...state.triggerStatus, mode, radar_connected: true },
      }));
      await document.fonts.ready;
    },
    { mode, bag }
  );
  // The app opens on the club picker so the club is confirmed before the first shot.
  await expect(page.locator('.picker-overlay')).toBeVisible();
}

/** The labels' computed font-sizes on the open tab, once the fit has settled. */
async function labelSizes(page: Page): Promise<number[]> {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  return page.locator(OPTION).evaluateAll((options) => options.map((option) => parseFloat(getComputedStyle(option).fontSize)));
}

/** Opens each tab in turn and hands its name and label sizes to `check`. */
async function eachTab(page: Page, check: (tab: string, sizes: number[]) => Promise<void>) {
  const tabs = page.locator(TAB);
  for (let index = 0; index < (await tabs.count()); index += 1) {
    await tabs.nth(index).click();
    await expect(tabs.nth(index)).toHaveAttribute('aria-pressed', 'true');
    await check((await tabs.nth(index).textContent()) ?? '', await labelSizes(page));
  }
}

async function expectReadableTab(page: Page, tab: string, sizes: number[]) {
  expect(sizes.length, tab).toBeGreaterThan(0);
  expect(new Set(sizes).size, `${tab}: one size for every label, got ${sizes.join(', ')}`).toBe(1);
  expect(await clippedText(page, OPTION), tab).toEqual([]);
  expect(await brokenWords(page, '.picker-overlay__label'), tab).toEqual([]);
  expect(await clippedText(page, TAB), tab).toEqual([]);
  expect(await clippedText(page, '.picker-overlay__title'), tab).toEqual([]);
  expect(await smallTargets(page, `${OPTION}, ${TAB}, .picker-overlay__close`), tab).toEqual([]);
}

for (const viewport of KIOSK_VIEWPORTS) {
  for (const locale of ['de', 'en'] as const) {
    test.describe(`kiosk picker ${viewport.width}×${viewport.height} (${locale})`, () => {
      test.use({ viewport, hasTouch: true });

      test('club labels fill their tiles, one size per tab, never clipped or broken', async ({ page }) => {
        await openPicker(page, { locale, bag: BAG_14, kiosk: true });
        await expect(page.locator('.picker-overlay')).toHaveAttribute('aria-label', locale === 'de' ? 'Schläger wählen' : 'Select club');
        await eachTab(page, async (tab, sizes) => {
          await expectReadableTab(page, tab, sizes);
          // 1024×600: the ceiling, 0.42 of a 195 px tile (82 px); 800×480 and 800×400: 57 and 40 px.
          const least = viewport.width === 1024 ? 78 : viewport.height === 480 ? 54 : 38;
          expect(sizes[0], tab).toBeGreaterThanOrEqual(least);
        });
      });

      test('every club (three rows of irons) stays readable', async ({ page }) => {
        await openPicker(page, { locale, kiosk: true });
        await eachTab(page, async (tab, sizes) => {
          await expectReadableTab(page, tab, sizes);
          // 1024×600: 0.42 of a 127 px tile (53 px).
          if (viewport.width === 1024) expect(sizes[0], tab).toBeGreaterThanOrEqual(50);
        });
      });

      test('implement names wrap between words and never clip', async ({ page }) => {
        await openPicker(page, { locale, mode: 'swing-speed', kiosk: true });
        await expect(page.locator('.picker-overlay')).toHaveAttribute('aria-label', locale === 'de' ? 'Trainingsgerät wählen' : 'Select implement');
        await eachTab(page, (tab, sizes) => expectReadableTab(page, tab, sizes));
        await expect(page.locator(OPTION).last()).toHaveText('Rypstick 3 Weights + Counterweight');
        // "Rypstick 3 Weights + Counterweight" takes three lines at most.
        const lines = await page.locator('.picker-overlay__label').last().evaluate((label) => {
          const range = document.createRange();
          range.selectNodeContents(label);
          return new Set([...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))).size;
        });
        expect(lines).toBeLessThanOrEqual(3);
      });
    });
  }

  test(`kiosk picker chrome at ${viewport.width}×${viewport.height}: title like the Live header, large tabs and Close`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openPicker(page, { locale: 'de', bag: BAG_14, kiosk: true });
    const rem = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
    const title = await page.locator('.picker-overlay__title').evaluate((el) => {
      const style = getComputedStyle(el);
      return { size: parseFloat(style.fontSize), weight: style.fontWeight, transform: style.textTransform, spacing: style.letterSpacing };
    });
    expect(title).toEqual({ size: expect.closeTo(1.4 * rem, 1), weight: '800', transform: 'none', spacing: 'normal' });
    for (const tab of await page.locator(TAB).all()) {
      expect((await tab.boundingBox())!.height).toBeGreaterThanOrEqual(76);
      expect(await tab.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBeCloseTo(1.35 * rem, 1);
    }
    const close = page.locator('.picker-overlay__close');
    expect(await close.boundingBox()).toMatchObject({ width: 64, height: 64 });
    expect(await close.evaluate((el) => parseFloat(getComputedStyle(el).fontSize))).toBeCloseTo(1.6 * rem, 1);
    // The header keeps its 76 px (and its hairline), so the tiles keep their height.
    expect((await page.locator('.picker-overlay__header').boundingBox())!.height).toBeLessThanOrEqual(77);
  });
}

const PHONES = [...PHONE_VIEWPORTS, { width: 390, height: 844 }, { width: 844, height: 390 }, { width: 667, height: 375 }];

for (const viewport of PHONES) {
  test.describe(`phone picker ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('club labels keep the phone size and never clip', async ({ page }) => {
      await openPicker(page, { locale: 'de', kiosk: false });
      // A phone in the hand keeps its small uppercase title.
      await expect(page.locator('.picker-overlay__title')).toHaveCSS('text-transform', 'uppercase');
      await eachTab(page, async (tab) => {
        expect(await clippedText(page, OPTION), tab).toEqual([]);
        expect(await clippedText(page, TAB), tab).toEqual([]);
        // Short club names fit at the phone's CSS size: the fit leaves them alone.
        expect(await page.locator(OPTION).evaluateAll((options) => options.filter((option) => option.style.fontSize).length), tab).toBe(0);
      });
    });

    test('implement names never clip', async ({ page }) => {
      await openPicker(page, { locale: 'de', mode: 'swing-speed', kiosk: false });
      await eachTab(page, async (tab, sizes) => {
        expect(new Set(sizes).size, tab).toBe(1);
        expect(await clippedText(page, OPTION), tab).toEqual([]);
        expect(await brokenWords(page, '.picker-overlay__label'), tab).toEqual([]);
        expect(await clippedText(page, TAB), tab).toEqual([]);
      });
    });
  });
}
