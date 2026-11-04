import { test, type Page } from '@playwright/test';
import {
  chooseMenuItem,
  expect,
  LIVE_METRIC_CARDS,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  PHONE_VIEWPORTS,
  WIDEST_LIVE_METRIC_VALUE,
} from './helpers';
import { phoneOrigin, routeApi, SHOT } from './phoneFixture';

/** The Range layout's Live tiles: carry and ball speed, then three smaller ones (never a spin axis). */
const RANGE_TILES = '.range-tile .metric-card';

const LOCALE_STORAGE_KEY = 'openflight.locale:v1';

async function dismissPicker(page: Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

/** Shots, Stats and Profiles open from the menu's Analysis area; the footer holds Live, History, Align and the menu. */
async function openView(page: Page, name: 'Shots' | 'Stats' | 'Profiles') {
  await page.getByRole('button', { name: 'Open menu' }).click();
  await chooseMenuItem(page, name);
}

/** Column count of a grid, from its computed template. */
async function gridColumns(page: Page, selector: string): Promise<number> {
  return page
    .locator(selector)
    .first()
    .evaluate((node) => getComputedStyle(node).gridTemplateColumns.split(' ').filter(Boolean).length);
}

/** Elements under `selector` whose box sticks out of the viewport horizontally. */
async function horizontallyClipped(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((nodes) =>
    nodes
      .filter((node) => {
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && (rect.left < -1 || rect.right > window.innerWidth + 1);
      })
      .map((node) => node.textContent?.trim() || node.className)
  );
}

/** Tile parts (label, value, captions) that end below or right of their tile. */
async function partsOutsideTiles(page: Page, tiles: string): Promise<string[]> {
  return page.locator(tiles).evaluateAll((cards) =>
    cards.flatMap((card) => {
      const box = card.getBoundingClientRect();
      return [...card.querySelectorAll('*')]
        .filter((child) => {
          const rect = child.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && (rect.bottom > box.bottom + 1 || rect.right > box.right + 1);
        })
        .map((child) => `${card.querySelector('.metric-card__label')?.textContent?.trim()}: ${child.className}`);
    })
  );
}

/** Puts the widest value we format into every tile; values, units and captions must still fit. */
async function expectWidestValuesToFit(page: Page, tiles: string) {
  await page.locator(`${tiles} .metric-card__value`).evaluateAll((values, widest) => {
    for (const value of values) value.textContent = widest;
  }, WIDEST_LIVE_METRIC_VALUE);
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  expect(await overflowingLiveMetricValues(page, tiles)).toEqual([]);
  expect(await partsOutsideTiles(page, tiles)).toEqual([]);
}

/** Header, toolbar and footer buttons that are smaller than a fingertip or covered by another element. */
async function unreachableButtons(page: Page): Promise<string[]> {
  return page.locator('.range-header button, .range-toolbar button, .range-footer button').evaluateAll((buttons) =>
    buttons
      .filter((button) => button.getBoundingClientRect().width > 0)
      .filter((button) => {
        const rect = button.getBoundingClientRect();
        const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
        return rect.width < 44 || rect.height < 44 || hit?.closest('button') !== button;
      })
      .map((button) => button.getAttribute('aria-label') || button.textContent?.trim() || button.className)
  );
}

/**
 * The app as a paired phone sees it, with one driver shot (or `shots`) in an English session. A phone's
 * host is not the kiosk's 127.0.0.1 (whose own screen has the big kiosk Live layout), and the backend
 * admits no socket from it unpaired, so the stores are filled in the page.
 */
async function gotoPhoneApp(page: Page, shots: object[] = [{ ...SHOT, timestamp: new Date().toISOString() }]) {
  await routeApi(page, {});
  await page.goto(`${phoneOrigin(test.info().project.use.baseURL!)}/`);
  await page.locator('.range-app').waitFor();
  await page.evaluate(async (shots) => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    (await load('services/socketService')).socketService.disconnect();
    (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
      profiles: [{ id: 'phone', name: 'Golfer', settings: { unit_system: 'imperial' } }],
      active_profile_id: 'phone',
    });
    (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
    (await load('stores/useShotStore')).useShotStore.getState().setShots(shots);
    await document.fonts.ready;
  }, shots);
}

for (const viewport of PHONE_VIEWPORTS) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('shows the live tiles two across, with every value and caption inside its tile', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);

      await expect(page.locator(RANGE_TILES)).toHaveCount(5);
      expect(await gridColumns(page, '.range-hero-grid')).toBe(2);
      expect(await gridColumns(page, '.range-secondary-grid')).toBe(2);
      expect(await overflowingLiveMetricValues(page, RANGE_TILES)).toEqual([]);
      expect(await overflowingLiveMetricMetadata(page, RANGE_TILES)).toEqual([]);
      expect(await partsOutsideTiles(page, RANGE_TILES)).toEqual([]);
      await expectWidestValuesToFit(page, RANGE_TILES);
    });

    test('shows all metrics two across, with every value and caption inside its tile', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);
      await page.getByRole('button', { name: 'All metrics' }).click();

      await expect(page.locator(LIVE_METRIC_CARDS)).toHaveCount(11);
      expect(await gridColumns(page, '.live-panel__grid')).toBe(2);
      // Eleven tiles in a 2 × 6 grid: no tile spans a row (the last one keeps half the width).
      const last = (await page.locator(LIVE_METRIC_CARDS).last().boundingBox())!;
      const grid = (await page.locator('.live-panel__grid').boundingBox())!;
      expect(last.width).toBeLessThan(grid.width * 0.6);
      expect(await overflowingLiveMetricValues(page)).toEqual([]);
      expect(await overflowingLiveMetricMetadata(page)).toEqual([]);
      expect(await partsOutsideTiles(page, LIVE_METRIC_CARDS)).toEqual([]);
      await expectWidestValuesToFit(page, LIVE_METRIC_CARDS);
    });

    for (const locale of ['en', 'de']) {
      test(`keeps the header, footer and page inside the screen, every button reachable (${locale})`, async ({
        page,
      }) => {
        await page.addInitScript(
          ([key, value]) => window.localStorage.setItem(key, value),
          [LOCALE_STORAGE_KEY, locale]
        );
        await gotoPhoneApp(page);
        await page.locator('.picker-overlay__close').click();

        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
        expect(
          await horizontallyClipped(page, '.range-header *, .range-toolbar *, .range-footer *, .range-shot-heading')
        ).toEqual([]);
        const footer = await page.locator('.range-footer').boundingBox();
        expect(footer!.y + footer!.height).toBeLessThanOrEqual(viewport.height + 1);
        // Before the phone layout the Replay button covered the menu button.
        expect(await unreachableButtons(page)).toEqual([]);

        await page.locator('.range-footer [aria-expanded]').tap();
        await expect(page.locator('.menu-sheet')).toBeVisible();
      });
    }

    test('shows shot (over its club), ball and carry in the shot list, all on screen', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);
      await openView(page, 'Shots');

      await expect(page.locator('.shots-panel__row')).toHaveCount(1);
      // The list holds the active profile's shots only; the club takes the profile's place.
      const headings = await page.locator('.shots-panel__columns > span:visible').allTextContents();
      expect(headings.filter(Boolean)).toEqual(['Shot', 'Ball', 'Carry']);
      await expect(page.locator('.shots-panel__profile-club')).toHaveText('Driver');
      expect(await horizontallyClipped(page, '.shots-panel__row-main > *, .shots-panel__delete')).toEqual([]);
      const deleteFits = await page
        .locator('.shots-panel__delete')
        .evaluate((node) => node.scrollWidth <= node.clientWidth);
      expect(deleteFits).toBe(true);
      // The two header buttons move below the profile instead of squeezing it.
      await expect(page.locator('.panel-header__subtitle')).toHaveText(/recorded/);
      expect(
        await page.locator('.panel-header__subtitle').evaluate((node) => node.scrollWidth <= node.clientWidth)
      ).toBe(true);
    });

    test('shows the session stats two across without clipping a unit', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);
      await openView(page, 'Stats');

      expect(await gridColumns(page, '.stats-panel__grid')).toBe(2);
      const clipped = await page.locator('.stats-panel__grid .metric-card').evaluateAll((cards) =>
        cards
          .filter((card) => {
            const cardRect = card.getBoundingClientRect();
            return [...card.querySelectorAll('*')].some((child) => {
              const rect = child.getBoundingClientRect();
              return rect.width > 0 && (rect.right > cardRect.right + 1 || rect.bottom > cardRect.bottom + 1);
            });
          })
          .map((card) => card.textContent?.trim())
      );
      expect(clipped).toEqual([]);
    });

    test('shows one profile card per row and returns to Live with its button pressed', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);
      await openView(page, 'Profiles');

      expect(await gridColumns(page, '.profiles-panel__grid')).toBe(1);
      const name = page.locator('.profiles-panel__name').first();
      expect(await name.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);

      // Picking a profile switches to Live by itself; the footer follows.
      await page.locator('.profiles-panel__card').first().click();
      const live = page.locator('.range-footer nav [aria-pressed="true"]');
      await expect(live).toHaveText(/Live/);
      const strip = await page.locator('.range-footer nav').boundingBox();
      const tab = await live.boundingBox();
      expect(tab!.x).toBeGreaterThanOrEqual(strip!.x - 1);
      expect(tab!.x + tab!.width).toBeLessThanOrEqual(strip!.x + strip!.width + 1);
    });

    test('keeps the empty-session title on screen', async ({ page }) => {
      await gotoPhoneApp(page, []);
      await dismissPicker(page);

      const title = page.locator('.range-empty h2');
      await expect(title).toBeVisible();
      const ink = await title.evaluate((node) => {
        const range = document.createRange();
        range.selectNodeContents(node);
        const rect = range.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      });
      expect(ink.left).toBeGreaterThanOrEqual(0);
      expect(ink.right).toBeLessThanOrEqual(viewport.width);
    });
  });
}

test.describe('phone portrait 393×659, platform', () => {
  test.use({ viewport: { width: 393, height: 659 }, isMobile: true, hasTouch: true });

  test('uses the system font the Pi has too and loads nothing from the internet', async ({ page }) => {
    const external: string[] = [];
    page.on('request', (request) => {
      const { hostname } = new URL(request.url());
      if (!['127.0.0.1', 'localhost', 'phone.localhost'].includes(hostname) && !request.url().startsWith('data:')) {
        external.push(request.url());
      }
    });
    await gotoPhoneApp(page);
    await dismissPicker(page);
    await page.evaluate(() => document.fonts.ready);

    // Text in Nexa (a•space next for ≈), metric numbers in a•space.
    const family = (selector: string) => page.locator(selector).first().evaluate((element) => getComputedStyle(element).fontFamily);
    expect(await page.evaluate(() => getComputedStyle(document.body).fontFamily)).toMatch(/^"?Nexa"?, "?A-Space"?, Arial, "Liberation Sans"/);
    expect(await family('.range-hero-grid .metric-card__label')).toMatch(/^"?Nexa"?, "?A-Space"?, Arial/);
    expect(await family('.range-hero-grid .metric-card__value')).toMatch(/^"?A-Space"?, Arial, "Liberation Sans"/);
    expect(await page.evaluate(() => document.fonts.check('600 16px "Nexa"'))).toBe(true);
    expect(await page.evaluate(() => document.fonts.check('16px "A-Space"'))).toBe(true);
    expect(external).toEqual([]);
  });

  test('keeps the shot editor fields at 16 px or more, so iOS does not zoom in', async ({ page }) => {
    await gotoPhoneApp(page);
    await dismissPicker(page);
    await openView(page, 'Shots');
    await page.locator('.shots-panel__row-main').first().click();

    const sizes = await page
      .locator('.shots-panel__field input, .shots-panel__field select')
      .evaluateAll((fields) => fields.map((field) => parseFloat(getComputedStyle(field).fontSize)));
    expect(sizes.length).toBeGreaterThan(0);
    expect(Math.min(...sizes)).toBeGreaterThanOrEqual(16);
  });
});

for (const viewport of [
  { width: 852, height: 300 },
  { width: 568, height: 320 },
]) {
  test.describe(`phone in landscape ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('keeps the kiosk layout', async ({ page }) => {
      await gotoPhoneApp(page);
      await dismissPicker(page);

      expect(await gridColumns(page, '.range-hero-grid')).toBe(2);
      // Launch, start direction and spin; never a spin axis.
      expect(await gridColumns(page, '.range-secondary-grid')).toBe(3);
      await expect(page.locator('.range-header .range-theme')).toBeVisible();
      // Up to 760 wide the five footer labels only fit under their icons (phoneLandscape.css).
      expect(
        await page
          .locator('.range-footer nav button')
          .first()
          .evaluate((node) => getComputedStyle(node).flexDirection)
      ).toBe(viewport.width <= 760 ? 'column' : 'row');
    });
  });
}
