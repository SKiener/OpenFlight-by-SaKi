import { test, expect, type Page } from '@playwright/test';
import { clippedText, KIOSK_VIEWPORTS, LIVE_METRIC_CARDS, openMenuArea, overflowingLiveMetricMetadata, overflowingLiveMetricValues, PHONE_VIEWPORTS } from './helpers';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY, unmeasurableClubPageCopy } from '../../src/test-utils/shot';
import { phoneOrigin } from './phoneFixture';

test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });

const SMALL_TILES = '.range-secondary-grid .metric-card';
const HERO_TILES = '.range-hero-grid .metric-card';

/**
 * The Range layout of every browser but the kiosk's (a tablet, a laptop, a phone): it loads from
 * a host that is not the Pi's own. The kiosk's big Live screen has its own spec (kiosk-live.spec.ts).
 */
async function showFixture(page: Page, locale = 'de', theme = 'dark', units = 'metric') {
  await page.route('**/api/**', route => route.fulfill({ json: {} }));
  await page.addInitScript(locale => localStorage.setItem('openflight.locale:v1', locale), locale);
  await page.goto(`${phoneOrigin(test.info().project.use.baseURL!)}/`);
  await page.locator('.range-app').waitFor();
  await page.evaluate(async ({ theme, units }) => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    (await load('services/socketService')).socketService.disconnect();
    (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
      profiles: [{ id: 'touch-test', name: 'Golfer', settings: { unit_system: units, club_ids: ['wedge-58', 'wedge-54', 'wedge-48', 'wedge-44', '9-iron', '8-iron', '7-iron', '6-iron', '5-iron', '4-iron', '7-wood', '3-wood', 'mini-driver', 'driver'] } }],
      active_profile_id: 'touch-test',
    });
    (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
    (await load('stores/useDebugStore')).useDebugStore.setState(state => ({
      triggerStatus: { ...state.triggerStatus, radar_connected: true },
    }));
    (await load('stores/useThemeStore')).useThemeStore.getState().setTheme(theme);
    (await load('stores/useShotStore')).useShotStore.getState().setShots([{
      ball_speed_mph: 103.4, club_speed_mph: 78, smash_factor: 1.33,
      estimated_carry_yards: 135, carry_range: [125, 145], club: '8iron',
      timestamp: '2026-10-01T12:00:00Z', peak_magnitude: 100,
      launch_angle_vertical: 26.2, launch_angle_horizontal: -1.2,
      launch_angle_confidence: .95, angle_source: 'radar', club_angle_deg: null,
      club_path_deg: null, spin_axis_deg: null, spin_rpm: 10000,
      spin_confidence: .59, spin_quality: 'experimental', spin_source: null,
      carry_spin_adjusted: 133, carry_spin_rpm: 10000, carry_spin_source: 'club_typical',
      profile_id: 'touch-test', profile_name: 'Golfer',
    }]);
    await document.fonts.ready;
  }, { theme, units });
  await page.locator('.picker-overlay__close').tap();
  // Launch angle, start direction and the typical spin; never a spin axis.
  await expect(page.locator(SMALL_TILES)).toHaveCount(3);
}

/** Replaces the fixture shot with a copy changed by `overrides`, e.g. a Total. */
async function updateFixtureShot(page: Page, overrides: Record<string, unknown>) {
  await page.evaluate(async (overrides) => {
    const path = '/src/stores/useShotStore.ts';
    const { useShotStore } = await import(/* @vite-ignore */ path);
    const shot = useShotStore.getState().shots[0];
    useShotStore.getState().setShots([{ ...shot, ...overrides }]);
  }, overrides);
}

interface TileBox { id: string | null; left: number; right: number; top: number; width: number }

/** The small tiles' boxes, measured from the edges of the row they share. */
function smallTiles(page: Page): Promise<{ height: number; tiles: TileBox[] }> {
  return page.locator('.range-secondary-grid').evaluate((grid) => {
    const row = grid.getBoundingClientRect();
    return { height: row.height, tiles: [...grid.children].map((tile) => {
      const box = tile.getBoundingClientRect();
      return { id: tile.getAttribute('data-metric'), left: box.left - row.left, right: row.right - box.right,
        top: box.top - row.top, width: box.width };
    }) };
  });
}

/** Tiles side by side in one band: same top, same width, flush with both ends of the row. */
function expectEvenRow(tiles: TileBox[]) {
  expect(tiles[0].left, JSON.stringify(tiles)).toBeLessThanOrEqual(1);
  expect(tiles.at(-1)!.right, JSON.stringify(tiles)).toBeLessThanOrEqual(1);
  for (const tile of tiles) {
    expect(Math.abs(tile.top - tiles[0].top), JSON.stringify(tiles)).toBeLessThanOrEqual(1);
    expect(Math.abs(tile.width - tiles[0].width), JSON.stringify(tiles)).toBeLessThanOrEqual(1);
  }
}

async function expectSmallTilesReadable(page: Page) {
  expect(await overflowingLiveMetricValues(page, SMALL_TILES)).toEqual([]);
  expect(await overflowingLiveMetricMetadata(page, SMALL_TILES)).toEqual([]);
}

for (const locale of ['de', 'en']) {
  for (const theme of ['dark', 'sun']) {
    test(`${locale}/${theme}: provisional and spin-estimated carry stay readable`, async ({ page }) => {
      await showFixture(page, locale, theme);
      const carry = page.locator('[data-metric="carry"]');
      const label = carry.locator('.metric-card__subtext');
      const value = carry.locator('.metric-card__value');
      await page.evaluate(async () => {
        const path = '/src/stores/useShotStore.ts';
        const { useShotStore } = await import(/* @vite-ignore */ path);
        const shot = useShotStore.getState().shots[0];
        useShotStore.getState().setShots([{ ...shot, estimated_carry_yards: 155.3,
          carry_spin_adjusted: null, launch_angle_vertical: null, launch_angle_horizontal: null }]);
      });
      await expect(value).toHaveText('142');
      await expect(label).toHaveText(locale === 'de' ? 'Vorläufige Carry-Schätzung' : 'Provisional carry estimate');
      for (const stage of ['initial', 'final']) {
        if (stage === 'final') {
          await page.evaluate(async () => {
            const path = '/src/stores/useShotStore.ts';
            const { useShotStore } = await import(/* @vite-ignore */ path);
            const shot = useShotStore.getState().shots[0];
            useShotStore.getState().updateShot({ ...shot, carry_spin_adjusted: 146.4,
              carry_spin_source: 'club_typical', launch_angle_vertical: 25 });
          });
          await expect(value).toHaveText('≈ 134');
          await expect(label).toHaveText(locale === 'de' ? 'Total — · mit typischem Spin' : 'Total — · with typical spin');
        }
        const bounds = await label.evaluate(el => {
          const card = el.closest('.metric-card')!.getBoundingClientRect();
          const range = document.createRange();
          range.selectNodeContents(el);
          return { fits: [...range.getClientRects()].every(r => r.left >= card.left && r.right <= card.right && r.top >= card.top && r.bottom <= card.bottom),
            size: parseFloat(getComputedStyle(el).fontSize) };
        });
        expect(bounds.fits).toBe(true);
        expect(bounds.size).toBeGreaterThanOrEqual(18);
      }
    });
    test(`${locale}/${theme}: readable touch controls without clipping at 1024×600`, async ({ page }, testInfo) => {
      await showFixture(page, locale, theme);
      const typeSizes = await page.locator('.range-shot-heading h1, .range-shot-heading > span, .range-tile .metric-card__label, .range-tile .metric-card__confidence-label, .range-replay-button small').evaluateAll(elements => elements.map(element => ({
        text: element.textContent,
        size: parseFloat(getComputedStyle(element).fontSize),
        minimum: element.matches('h1') ? 32 : element.matches('.metric-card__label') ? 20 : 18,
        weight: getComputedStyle(element).fontWeight,
      })));
      for (const item of typeSizes) {
        expect(item.size, JSON.stringify(item)).toBeGreaterThanOrEqual(item.minimum);
        expect(Number(item.weight), JSON.stringify(item)).toBeGreaterThanOrEqual(600);
      }
      for (const unit of await page.locator('.range-tile .metric-card__unit').all()) {
        expect(await unit.evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(16);
      }
      const controls = page.locator('.range-header button, .range-toolbar button, .range-footer button');
      const measurements = await controls.evaluateAll(buttons => buttons.map(button => {
        const r = button.getBoundingClientRect();
        return { text: button.textContent, height: r.height, width: r.width, font: parseFloat(getComputedStyle(button).fontSize) };
      }));
      expect(measurements.length).toBeGreaterThanOrEqual(9);
      for (const control of measurements) {
        expect(control.height, JSON.stringify(control)).toBeGreaterThanOrEqual(56);
        expect(control.width, JSON.stringify(control)).toBeGreaterThanOrEqual(56);
        expect(control.font, JSON.stringify(control)).toBeGreaterThanOrEqual(20);
      }
      const overflow = await page.locator('.range-header button, .range-toolbar button, .range-footer button, .range-tile .metric-card').evaluateAll(elements => elements.flatMap(element => {
        const box = element.getBoundingClientRect();
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        const errors = [];
        let node;
        while ((node = walker.nextNode())) {
          if (!node.textContent?.trim()) continue;
          const range = document.createRange();
          range.selectNodeContents(node);
          if ([...range.getClientRects()].some(r => r.left < box.left - 1 || r.right > box.right + 1 || r.top < box.top - 1 || r.bottom > box.bottom + 1)) errors.push(node.textContent);
        }
        if (box.left < 0 || box.right > 1024 || box.top < 0 || box.bottom > 600) errors.push('outside screen');
        return errors;
      }));
      expect(overflow).toEqual([]);
      const layout = await page.evaluate(() => ({
        mainBottom: document.querySelector('main')!.getBoundingClientRect().bottom,
        footerTop: document.querySelector('.range-footer')!.getBoundingClientRect().top,
        width: document.documentElement.scrollWidth,
      }));
      expect(layout.mainBottom).toBeLessThanOrEqual(layout.footerTop);
      expect(layout.width).toBe(1024);
      await page.screenshot({ path: testInfo.outputPath(`${locale}-${theme}-touch.png`) });
    });
  }
}

test('four-digit shot count stays separate from touch actions', async ({ page }) => {
  await showFixture(page);
  await page.evaluate(async () => {
    const path = '/src/stores/useShotStore.ts';
    const { useShotStore } = await import(/* @vite-ignore */ path);
    const shot = useShotStore.getState().shots[0];
    useShotStore.getState().setShots(Array.from({ length: 1234 }, () => ({ ...shot })));
  });
  await expect(page.locator('.range-shot-heading h1')).toHaveText('Schlag 1234');
  const heading = await page.locator('.range-shot-heading').boundingBox();
  const actions = await page.locator('.range-toolbar-actions').boundingBox();
  expect(heading!.x + heading!.width).toBeLessThanOrEqual(actions!.x);
});

test('touch navigation and keyboard focus still work with enlarged controls', async ({ page }) => {
  await showFixture(page);
  await page.locator('.range-theme').tap();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'sun');
  await page.locator('.range-connection').tap();
  await expect(page.locator('.panel-header__status-menu')).toBeVisible();
  await page.locator('.panel-scrim').tap({ position: { x: 10, y: 400 } });
  await page.locator('.range-details-button').tap();
  await expect(page.locator('.range-details')).toBeVisible();
  await page.locator('.range-back').tap();
  await expect(page.locator('.range-hero-grid')).toBeVisible();
  await page.locator('.range-toolbar .panel-action').tap();
  await expect(page.locator('.picker-overlay')).toBeVisible();
  await page.locator('.picker-overlay__close').tap();
  await page.locator('.range-footer button[aria-expanded]').tap();
  await expect(page.locator('.menu-sheet')).toBeVisible();
  await page.locator('.panel-scrim').tap({ position: { x: 10, y: 400 } });
  await page.locator('.range-theme').focus();
  await page.keyboard.press('Tab');
  await expect(page.locator('.range-connection')).toBeFocused();
  await expect(page.locator('.range-connection')).toHaveCSS('outline-style', 'solid');
});

test('club categories are large touch tabs ordered Wedges, Eisen, Hölzer', async ({ page }, testInfo) => {
  await showFixture(page);
  await page.locator('.range-toolbar .panel-action').tap();
  const tabs = page.locator('.picker-overlay__tabs button');
  await expect(tabs).toHaveText(['Wedges', 'Eisen', 'Hölzer']);
  for (const tab of await tabs.all()) {
    const size = await tab.boundingBox();
    expect(size!.height).toBeGreaterThanOrEqual(76);
    expect(size!.width).toBeGreaterThanOrEqual(300);
    // 1.35rem: 32 px at 1024 wide, read from 1-2 m.
    expect(await tab.evaluate(el => parseFloat(getComputedStyle(el).fontSize))).toBeGreaterThanOrEqual(32);
  }
  const closeSize = await page.locator('.picker-overlay__close').boundingBox();
  expect(closeSize!.height).toBeGreaterThanOrEqual(64);
  expect(closeSize!.width).toBeGreaterThanOrEqual(64);
  await tabs.nth(0).tap();
  await expect(tabs.nth(0)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.picker-overlay__option')).toHaveText(['58°', '54°', '48°', '44°']);
  await page.screenshot({ path: testInfo.outputPath('club-picker-touch.png') });
  await tabs.nth(1).tap();
  await expect(page.locator('.picker-overlay__option')).toHaveCount(6);
  await expect(page.locator('.picker-overlay__option').last()).toBeInViewport();
  await tabs.nth(2).tap();
  await expect(page.locator('.picker-overlay__option')).toHaveText(['DR', 'Mini DR', '3W', '7W']);
  await page.locator('.picker-overlay__option').last().tap();
  await expect(page.locator('.picker-overlay')).not.toBeVisible();
});

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`small tiles at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('three tiles share the row evenly, also once a shot has a spin axis', async ({ page }, testInfo) => {
      await showFixture(page);
      const three = await smallTiles(page);
      expect(three.tiles.map(tile => tile.id)).toEqual(['launch_v', 'launch_h', 'spin']);
      expectEvenRow(three.tiles);
      await expect(page.locator('[data-metric="spin"] .metric-card__value')).toHaveText('10.000');
      await expect(page.locator('[data-metric="spin"] .metric-card__subtext')).toHaveText('typisch');
      await expectSmallTilesReadable(page);
      await page.screenshot({ path: testInfo.outputPath('three-small-tiles.png') });

      // No screen shows the spin axis this hardware cannot measure.
      await updateFixtureShot(page, { spin_axis_deg: 3.4 });
      await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
      const after = await smallTiles(page);
      expect(after.tiles.map(tile => tile.id)).toEqual(['launch_v', 'launch_h', 'spin']);
      expectEvenRow(after.tiles);
      expect(Math.abs(after.height - three.height)).toBeLessThanOrEqual(1);
    });

    test('the shot list fits a typical wedge spin with its ≈ without shifting the columns', async ({ page }, testInfo) => {
      await showFixture(page, 'en');
      await page.evaluate(async () => {
        const path = '/src/stores/useShotStore.ts';
        const { useShotStore } = await import(/* @vite-ignore */ path);
        const [shot] = useShotStore.getState().shots;
        useShotStore.getState().setShots([
          { ...shot, timestamp: '2026-10-01T12:00:00Z', spin_rpm: 2650, carry_spin_rpm: 2650, carry_spin_source: 'measured' },
          { ...shot, timestamp: '2026-10-01T12:01:00Z', carry_spin_rpm: 10500 },
        ]);
      });
      await page.getByRole('button', { name: 'Open menu' }).tap();
      await openMenuArea(page, 'Analysis', { tap: true });
      await page.locator('.range-tools').getByRole('button', { name: 'Shots', exact: true }).tap();
      const spin = page.locator('.shots-panel__row-main > :nth-child(6)');
      await expect(spin).toHaveText(['≈10,500', '2,650']);
      // Every row is its own grid: a value wider than its column would widen it and shift that row.
      const edges = await page.evaluate(() => [document.querySelector('.shots-panel__columns')!,
        ...document.querySelectorAll('.shots-panel__row-main')].map(grid =>
        [...grid.children].slice(2, 7).map(cell => Math.round(cell.getBoundingClientRect().right))));
      for (const row of edges.slice(1)) expect(row, JSON.stringify(edges)).toEqual(edges[0]);
      const ink = await spin.first().evaluate(cell => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        const text = range.getBoundingClientRect();
        const box = cell.getBoundingClientRect();
        return text.left >= box.left - 1 && text.right <= box.right + 1;
      });
      expect(ink).toBe(true);
      await page.screenshot({ path: testInfo.outputPath('shot-list-spin.png') });
    });
  });
}

const TOTAL_CAPTIONS = [
  { locale: 'de', units: 'metric', caption: 'Total 156 m · mit typischem Spin' },
  { locale: 'en', units: 'metric', caption: 'Total 156 m · with typical spin' },
  { locale: 'de', units: 'imperial', caption: 'Total 171 yds · mit typischem Spin' },
];

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`carry tile with a Total at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    for (const { locale, units, caption } of TOTAL_CAPTIONS) {
      test(`${locale} ${units}: the Total and the spin note stay inside the hero tile`, async ({ page }) => {
        await showFixture(page, locale, 'dark', units);
        await updateFixtureShot(page, { total_yards: 171 });
        await expect(page.locator('[data-metric="carry"] .metric-card__subtext')).toHaveText(caption);
        expect(await overflowingLiveMetricValues(page, HERO_TILES)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, HERO_TILES)).toEqual([]);
        expect(await clippedText(page, `${HERO_TILES} .metric-card__subtext`)).toEqual([]);
      });
    }
  });
}

for (const viewport of PHONE_VIEWPORTS) {
  test.describe(`small tiles on a ${viewport.width}×${viewport.height} phone`, () => {
    test.use({ viewport, isMobile: true });

    test('launch and start direction share a row, spin spans the next, all inside the screen', async ({ page }, testInfo) => {
      await showFixture(page);
      const three = await smallTiles(page);
      const [launch, direction, spin] = three.tiles;
      expect(three.tiles.map(tile => tile.id)).toEqual(['launch_v', 'launch_h', 'spin']);
      expectEvenRow([launch, direction]);
      expect(spin.top).toBeGreaterThan(launch.top + 1);
      expect(spin.left).toBeLessThanOrEqual(1);
      expect(spin.right).toBeLessThanOrEqual(1);
      await expect(page.locator('[data-metric="spin"] .metric-card__subtext')).toHaveText('typisch');
      await expectSmallTilesReadable(page);
      const footer = (await page.locator('.range-footer').boundingBox())!;
      expect(footer.y + footer.height).toBeLessThanOrEqual(viewport.height + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
      await page.screenshot({ path: testInfo.outputPath('three-small-tiles-phone.png') });

      await updateFixtureShot(page, { spin_axis_deg: -4 });
      await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
      const after = await smallTiles(page);
      expect(after.tiles.map(tile => tile.id)).toEqual(['launch_v', 'launch_h', 'spin']);
      expect(Math.abs(after.height - three.height)).toBeLessThanOrEqual(1);
    });
  });
}

/**
 * Visible title text that leaves its tile, as `text: px past the edge`. Text in a
 * clipped screen-reader label is not painted and does not count.
 */
function titleOverflow(page: Page, tiles: string): Promise<string[]> {
  return page.locator(`${tiles} .metric-card__label`).evaluateAll((labels) =>
    labels.flatMap((label) => {
      const card = label.closest('.metric-card')!.getBoundingClientRect();
      const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
      const painted: string[] = [];
      let past = 0;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const hidden = node.parentElement?.closest('.metric-card__estimated-label');
        if (hidden && getComputedStyle(hidden).clip !== 'auto') continue;
        painted.push(node.textContent ?? '');
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (rect.width > 0) past = Math.max(past, card.left - rect.left, rect.right - card.right);
        }
      }
      return past > 1 ? [`${painted.join('').trim()}: ${Math.round(past)}`] : [];
    }));
}

/* The server still sends a spin axis (the K-LD7 D-plane spin_axis_deg and the
 * camera D-plane class) and the other club values this hardware cannot measure:
 * neither the Range row nor the details pages show any of them, in any layout. */
const UNMEASURABLE_SHOT = {
  ...UNMEASURABLE_CLUB_DATA,
  spin_axis_deg: 3.4,
  spin_axis_source: 'calculated',
  spin_axis_class: 'straight',
  club_data_status: 'done',
};

const UNMEASURABLE_VIEWPORTS = [
  ...KIOSK_VIEWPORTS,
  { width: 720, height: 1280 },
  ...PHONE_VIEWPORTS.slice(0, 2),
  { width: 568, height: 320 },
  { width: 659, height: 393 },
];

for (const viewport of UNMEASURABLE_VIEWPORTS) {
  test.describe(`unmeasurable values at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: viewport.width < 700 || viewport.height < 440 });

    for (const locale of ['de', 'en', 'es'] as const) {
      test(`${locale}: no spin axis, loft, face, strike or low point on the Range row or the details pages`, async ({ page }) => {
        await showFixture(page, locale);
        await updateFixtureShot(page, UNMEASURABLE_SHOT);
        const words = [...UNMEASURABLE_COPY[locale], 'Fade', 'Neutral', 'Neutra'];
        const noneIn = async (selector: string, banned: readonly string[] = words) => {
          const text = (await page.locator(selector).allInnerTexts()).join(' | ');
          for (const word of banned) expect(text, `${selector}: ${word}`).not.toContain(word);
        };
        await expect(page.locator(SMALL_TILES)).toHaveCount(3);
        await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
        await noneIn('.range-live');
        await expectSmallTilesReadable(page);

        await page.locator('.range-details-button').tap();
        await expect(page.locator(LIVE_METRIC_CARDS)).toHaveCount(11);
        await noneIn(LIVE_METRIC_CARDS);
        expect(await titleOverflow(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, LIVE_METRIC_CARDS)).toEqual([]);
        // Eleven tiles fill every row but the last (kiosk 4 × 3, landscape phone 5 × 3, phone 2 × 6),
        // and no tile spans the empty cells there: every tile is as wide as the first.
        const grid = await page.locator('.live-panel__grid').evaluate((el) => {
          const box = el.getBoundingClientRect();
          const cards = [...el.children].map((child) => child.getBoundingClientRect());
          const rows = [...new Set(cards.map((card) => Math.round(card.top)))];
          return {
            gaps: rows.slice(0, -1).map((top) => {
              const row = cards.filter((card) => Math.round(card.top) === top);
              return Math.round(row.reduce((sum, card) => sum + card.width, 0) + row.length - 1 - box.width);
            }),
            widths: cards.map((card) => Math.round(card.width - cards[0].width)),
          };
        });
        for (const gap of grid.gaps) expect(Math.abs(gap), JSON.stringify(grid)).toBeLessThanOrEqual(2);
        for (const width of grid.widths) expect(Math.abs(width), JSON.stringify(grid)).toBeLessThanOrEqual(1);

        const clubTab = { de: 'Schläger', en: 'Club', es: 'Palo' }[locale];
        await page.locator('.range-details-toolbar').getByRole('button', { name: clubTab, exact: true }).tap();
        // The 4th card is the impact point: its title is allowed here, the old impact_toe_mm draws nothing.
        // The 5th is the low point, whose title the club page may show too.
        await expect(page.locator('.club-data__grid .metric-card')).toHaveCount(5);
        await noneIn('.club-data__grid .metric-card', [...unmeasurableClubPageCopy(locale), 'Fade', 'Neutral', 'Neutra']);
        await expect(page.locator('.club-data__grid [data-metric="cd_impact"] .metric-card__value')).toHaveText('—');
      });
    }
  });
}

const UNCALIBRATED_CAPTIONS = [
  { locale: 'de', title: 'Startrichtung', caption: 'nicht kalibriert' },
  { locale: 'en', title: 'Start direction', caption: 'not calibrated' },
  { locale: 'es', title: 'Salida H.', caption: 'no calibrado' },
];

/* "nicht kalibriert" is a caption, not a nowrap pill: as a pill it ran 10-22 px
 * past the start direction's details tile at 800x480, 1024x600 and 720x1280.
 * Tiles too short for a caption (panel.css, 96 px on landscape screens;
 * phone.css, 64 px) drop every details caption instead, so the Range row alone
 * carries the word there. With eleven tiles (the apex) that is every kiosk
 * (4 x 3: 55-112 px tiles). Portrait phones keep every caption: their 2 x 6
 * tiles scroll inside the panel instead (phone.css). */
const UNCALIBRATED_VIEWPORTS = [
  ...KIOSK_VIEWPORTS.map((viewport) => ({ viewport, detailsCaption: 'hidden' as const })),
  { viewport: { width: 720, height: 1280 }, detailsCaption: 'visible' as const },
  ...PHONE_VIEWPORTS.map((viewport) => ({ viewport, detailsCaption: 'visible' as const })),
];

for (const { viewport, detailsCaption } of UNCALIBRATED_VIEWPORTS) {
  test.describe(`uncalibrated start direction at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: viewport.width < 700 });

    for (const { locale, title, caption } of UNCALIBRATED_CAPTIONS) {
      test(`${locale}: "${caption}" is a caption and no title or caption leaves its tile`, async ({ page }) => {
        await showFixture(page, locale);
        // Every caption at once: a Total on the carry, a radar direction without a phase zero (and a calculated
        // spin axis the server sends, which no tile shows).
        await updateFixtureShot(page, { total_yards: 171, spin_axis_deg: 3.4, spin_axis_source: 'calculated' });
        const range = '.range-secondary-grid [data-metric="launch_h"]';
        await expect(page.locator(`${range} .metric-card__label`)).toHaveText(title);
        await expect(page.locator(`${range} .metric-card__subtext`)).toHaveText(caption);
        await expect(page.locator(`${range} .metric-card__subtext`)).toBeVisible();
        await expect(page.locator(`${range} .metric-card__confidence`)).toHaveCount(0);
        for (const tiles of [HERO_TILES, SMALL_TILES]) {
          expect(await titleOverflow(page, tiles)).toEqual([]);
          expect(await overflowingLiveMetricMetadata(page, tiles)).toEqual([]);
        }

        await page.locator('.range-details-button').tap();
        const details = '.live-panel__grid .metric-card[data-metric="launch_h"]';
        await expect(page.locator(`${details} .metric-card__label`)).toHaveText(title);
        await expect(page.locator(`${details} .metric-card__subtext`)).toHaveText(caption);
        await expect(page.locator(`${details} .metric-card__confidence`)).toHaveCount(0);
        expect(await titleOverflow(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, LIVE_METRIC_CARDS)).toEqual([]);
        if (detailsCaption === 'hidden') {
          await expect(page.locator(`${details} .metric-card__subtext`)).toBeHidden();
        } else {
          await expect(page.locator(`${details} .metric-card__subtext`)).toBeVisible();
        }
      });
    }
  });
}

/** The carry span the server still sends for a typical-spin carry (110–128 m); no screen shows it. */
const SERVER_CARRY_SPAN = { carry_span_min_yards: 120.4, carry_span_max_yards: 139.6 };
const SPAN_TEXT = /110–128|50–100|Spanne|Span |Rango/;

const TYPICAL_CARRY_CAPTIONS = [
  { locale: 'de', hero: 'Total 156 m · mit typischem Spin', details: 'mit typischem Spin',
    tooltip: 'Geschätzt mit typischem Spin · Kurve nicht berücksichtigt' },
  { locale: 'en', hero: 'Total 156 m · with typical spin', details: 'with typical spin',
    tooltip: 'Estimated with typical spin · Curve not considered' },
  { locale: 'es', hero: 'Total 156 m · con spin típico', details: 'con spin típico',
    tooltip: 'Estimado con spin típico · Curva no considerada' },
];

/* A carry flown with the club-typical spin: ≈ on every carry, "with typical spin" in the
 * captions. The span the server sends with it shows nowhere: not in All metrics, not in
 * the tooltip on the short Range hero tile. */
for (const { viewport, detailsCaption } of UNCALIBRATED_VIEWPORTS) {
  test.describe(`typical-spin carry without a span at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: viewport.width < 700 });

    for (const { locale, hero, details, tooltip } of TYPICAL_CARRY_CAPTIONS) {
      test(`${locale}: ≈ and caption stay inside the carry tiles, no span anywhere`, async ({ page }) => {
        await showFixture(page, locale);
        await updateFixtureShot(page, { total_yards: 171, ...SERVER_CARRY_SPAN });
        const heroCarry = page.locator('.range-hero-grid [data-metric="carry"] .metric-card');
        await expect(heroCarry.locator('.metric-card__value')).toHaveText('≈ 122');
        await expect(heroCarry.locator('.metric-card__subtext')).toHaveText(hero);
        await expect(heroCarry).toHaveAttribute('title', tooltip);
        await expect(page.locator('.range-hero-grid')).not.toContainText(SPAN_TEXT);
        expect(await overflowingLiveMetricValues(page, HERO_TILES)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, HERO_TILES)).toEqual([]);
        expect(await clippedText(page, `${HERO_TILES} .metric-card__subtext`)).toEqual([]);

        await page.locator('.range-details-button').tap();
        const carry = page.locator('.live-panel__grid .metric-card[data-metric="carry"]');
        await expect(carry.locator('.metric-card__value')).toHaveText('≈ 122');
        await expect(carry.locator('.metric-card__subtext')).toHaveText(details);
        await expect(carry).toHaveAttribute('title', tooltip);
        await expect(page.locator('.range-details')).not.toContainText(SPAN_TEXT);
        expect(await titleOverflow(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricValues(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, LIVE_METRIC_CARDS)).toEqual([]);
        if (detailsCaption === 'hidden') {
          await expect(carry.locator('.metric-card__subtext')).toBeHidden();
        } else {
          await expect(carry.locator('.metric-card__subtext')).toBeVisible();
        }
      });
    }
  });
}

const DETAILS_CARRY_NOTES = [
  { locale: 'de', long: TYPICAL_CARRY_CAPTIONS[0].tooltip, short: '≈ mit typischem Spin · Kurve nicht berücksichtigt', typical: 'mit typischem Spin' },
  { locale: 'en', long: TYPICAL_CARRY_CAPTIONS[1].tooltip, short: '≈ with typical spin · Curve not considered', typical: 'with typical spin' },
  { locale: 'es', long: TYPICAL_CARRY_CAPTIONS[2].tooltip, short: '≈ con spin típico · Curva no considerada', typical: 'con spin típico' },
];

/* A touch screen never shows the carry tile's tooltip: the details sheet prints
 * its notes as a text line under the grid, in the short form on short phones
 * (320×460 portrait, landscape phones), where the sentence took the tiles' room.
 * The fixture has no calculated tile, so no ≈ legend elsewhere: the short form
 * leads with "≈ mit typischem Spin", as the tiles there drop their captions. */
for (const viewport of [...KIOSK_VIEWPORTS, { width: 720, height: 1280 }, ...PHONE_VIEWPORTS, { width: 568, height: 320 }, { width: 659, height: 393 }]) {
  const short = viewport.width < 360 || viewport.height < 400;
  test.describe(`details carry notes at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: viewport.width < 700 || viewport.height < 440 });

    for (const { locale, long, short: shortText, typical } of DETAILS_CARRY_NOTES) {
      test(`${locale}: the typical spin and the missing curve are text, ${short ? 'short' : 'in full'}`, async ({ page }) => {
        await showFixture(page, locale);
        await updateFixtureShot(page, { total_yards: 171, ...SERVER_CARRY_SPAN });
        await page.locator('.range-details-button').tap();
        const shown = page.locator(`.range-details-carry-notes .carry-notes__${short ? 'short' : 'long'}`);
        await expect(shown).toHaveText(short ? shortText : long);
        await expect(shown).toBeVisible();
        await expect(page.locator('.range-details-carry-notes')).not.toContainText(SPAN_TEXT);
        // The ≈ on the carry is explained in visible text, never only in a tooltip.
        await expect(page.locator('.range-details').getByText(typical).filter({ visible: true })).not.toHaveCount(0);
        await expect(page.locator('.range-details-footnote')).toHaveCount(0);
        await expect(page.locator(`.range-details-carry-notes .carry-notes__${short ? 'long' : 'short'}`)).toBeHidden();
        await expect(shown).toBeInViewport({ ratio: 1 });
        expect(await clippedText(page, '.range-details-carry-notes')).toEqual([]);
        expect(await titleOverflow(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricValues(page, LIVE_METRIC_CARDS)).toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, LIVE_METRIC_CARDS)).toEqual([]);
        // The club page shows no carry, so no notes.
        await page.locator('.range-details-toolbar .segmented-control__button').nth(1).tap();
        await expect(page.locator('.range-details-carry-notes')).toHaveCount(0);
      });
    }
  });
}

const SHOT_DETAIL_NOTES = [
  { locale: 'de', menu: 'Menü öffnen', area: 'Auswertung', shots: 'Schläge', notes: TYPICAL_CARRY_CAPTIONS[0].tooltip },
  { locale: 'en', menu: 'Open menu', area: 'Analysis', shots: 'Shots', notes: TYPICAL_CARRY_CAPTIONS[1].tooltip },
  { locale: 'es', menu: 'Abrir menú', area: 'Análisis', shots: 'Golpes', notes: TYPICAL_CARRY_CAPTIONS[2].tooltip },
];

/* A touch screen never shows the carry cell's tooltip: the opened shot prints it as a text line. */
for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`shot detail carry notes at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    for (const { locale, menu, area, shots, notes } of SHOT_DETAIL_NOTES) {
      test(`${locale}: the opened shot names the typical spin and the missing curve as text, no span`, async ({ page }) => {
        await showFixture(page, locale);
        await updateFixtureShot(page, SERVER_CARRY_SPAN);
        await page.getByRole('button', { name: menu }).tap();
        await openMenuArea(page, area, { tap: true });
        await page.locator('.range-tools').getByRole('button', { name: shots, exact: true }).tap();
        const line = page.locator('.shots-panel__carry-notes');
        await expect(line).toHaveCount(0);
        await page.locator('.shots-panel__row-main').first().tap();
        await expect(line).toHaveText(notes);
        await expect(page.locator('.shots-panel')).not.toContainText(SPAN_TEXT);
        await expect(line).toBeInViewport({ ratio: 1 });
        expect(await clippedText(page, '.shots-panel__carry-notes')).toEqual([]);
      });
    }
  });
}

/* The camera club data arrives a few seconds after the shot. No screen shows its
 * spin axis, so the row keeps the same three small tiles with the same geometry
 * whether a shot has an axis, still waits for one, or is a driver. */
for (const viewport of [...KIOSK_VIEWPORTS, { width: 720, height: 1280 }, ...PHONE_VIEWPORTS]) {
  test.describe(`small tiles across club-data states at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: viewport.width < 700 });

    test('an iron with an axis, a pending iron and a driver keep three tiles with identical geometry', async ({ page }) => {
      await showFixture(page);
      await updateFixtureShot(page, { club: '8-iron', spin_axis_class: 'draw', club_data_status: 'done' });
      await expect(page.locator(SMALL_TILES)).toHaveCount(3);
      await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
      const before = await smallTiles(page);

      const next = async (overrides: Record<string, unknown>) =>
        page.evaluate(async (overrides) => {
          const path = '/src/stores/useShotStore.ts';
          const { useShotStore } = await import(/* @vite-ignore */ path);
          const shots = useShotStore.getState().shots;
          const last = shots.at(-1);
          useShotStore.getState().setShots([...shots, { ...last, timestamp: `2026-10-01T12:0${shots.length}:00Z`,
            spin_axis_class: null, club_data_status: 'pending', ...overrides }]);
        }, overrides);

      for (const [n, club] of [[2, '8-iron'], [3, 'driver']] as const) {
        await next({ club });
        await expect(page.locator('.range-shot-heading h1')).toContainText(String(n));
        await expect(page.locator(SMALL_TILES)).toHaveCount(3);
        await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
        const now = await smallTiles(page);
        expect(Math.abs(now.height - before.height)).toBeLessThanOrEqual(1);
        for (const [index, tile] of now.tiles.entries()) {
          const was = before.tiles[index];
          expect(tile.id).toBe(was.id);
          for (const key of ['left', 'right', 'top', 'width'] as const) {
            expect(Math.abs(tile[key] - was[key]), `${club} ${tile.id} ${key}`).toBeLessThanOrEqual(1);
          }
        }
        await expectSmallTilesReadable(page);
      }
    });
  });
}

