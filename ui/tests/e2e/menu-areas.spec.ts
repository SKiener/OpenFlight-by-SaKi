import { test, expect, type Page } from '@playwright/test';
import { clippedText, gotoApp, KIOSK_VIEWPORTS, menuAreaRow, menuBack, openMenuArea, smallTargets } from './helpers';
import { phoneOrigin } from './phoneFixture';

/**
 * The device menu in two levels: a start page (Close, quick buttons, six area rows, Shut down)
 * and one page per area in the same sheet, behind a back button.
 */
const KIOSK_AREAS = ['Display', 'Live tiles', 'Callouts', 'Calibrate', 'Analysis', 'System'];

const sheet = (page: Page) => page.locator('.menu-sheet');

async function openMenu(page: Page) {
  await page.getByRole('button', { name: 'Open menu' }).click();
  await expect(sheet(page)).toBeVisible();
}

async function openKiosk(page: Page) {
  await gotoApp(page);
  await page.getByRole('button', { name: 'Close Select club' }).click();
  await openMenu(page);
}

/** Every button of the open sheet lower than `min` px. */
function lowButtons(page: Page, min: number): Promise<string[]> {
  return page.locator('.menu-sheet button, .menu-sheet select').evaluateAll(
    (elements, min) =>
      elements
        .filter((element) => {
          const box = element.getBoundingClientRect();
          return box.width > 0 && box.height < min - 0.5;
        })
        .map((element) => element.textContent?.trim() || element.getAttribute('aria-label') || element.tagName),
    min
  );
}

test.describe('menu areas on the kiosk', () => {
  test.use({ viewport: { width: 1024, height: 600 } });

  test('lists the six areas between the quick buttons and Shut down, which stays last', async ({ page }) => {
    await openKiosk(page);

    await expect(sheet(page).locator('.menu-sheet__area-name')).toHaveText(KIOSK_AREAS);
    await expect(sheet(page).getByRole('button', { name: 'Pair device' })).toBeVisible();
    const last = await sheet(page).evaluate((node) => node.lastElementChild?.className);
    expect(last).toBe('menu-sheet__shutdown');
    await expect(menuAreaRow(page, 'Callouts').locator('.menu-sheet__area-summary')).toHaveText(/^(On|Off)$/);
    await expect(menuAreaRow(page, 'Live tiles').locator('.menu-sheet__area-summary')).toHaveText(
      'Carry · Ball speed'
    );
  });

  test('opens an area in the same sheet and goes back to where the start page was scrolled', async ({ page }) => {
    await openKiosk(page);
    const dialog = page.getByRole('dialog', { name: 'Menu' });
    await dialog.evaluate((node) => node.scrollTo(0, node.scrollHeight));
    const scrolled = await dialog.evaluate((node) => node.scrollTop);
    expect(scrolled).toBeGreaterThan(0);

    await openMenuArea(page, 'System');
    await expect(page.getByRole('dialog')).toHaveCount(1);
    await expect(dialog.getByRole('button', { name: 'Demo mode' })).toBeVisible();
    await expect(dialog.locator('.menu-sheet__areas')).toHaveCount(0);
    await expect(dialog.locator('.menu-sheet__shutdown')).toHaveCount(0);
    expect(await dialog.evaluate((node) => node.scrollTop)).toBe(0);
    await expect(dialog.locator('.menu-sheet__back')).toBeFocused();

    await menuBack(page);
    await expect.poll(() => dialog.evaluate((node) => node.scrollTop)).toBeCloseTo(scrolled, 0);
    await expect(menuAreaRow(page, 'System')).toBeFocused();
  });

  test('starts on the start page after closing and reopening', async ({ page }) => {
    await openKiosk(page);
    await openMenuArea(page, 'Calibrate');
    await page.getByRole('button', { name: 'Close menu' }).click({ position: { x: 10, y: 300 } });
    await expect(sheet(page)).toHaveCount(0);

    await openMenu(page);
    await expect(sheet(page).locator('.menu-sheet__areas')).toBeVisible();
    await expect(sheet(page).locator('.menu-sheet__back')).toHaveCount(0);
  });

  test('keeps the view buttons closing the menu and switching the view', async ({ page }) => {
    await openKiosk(page);
    await openMenuArea(page, 'Analysis');
    await sheet(page).getByRole('button', { name: 'Stats', exact: true }).click();
    await expect(sheet(page)).toHaveCount(0);
    await expect(page.locator('.stats-panel')).toBeVisible();
  });

  test('drag-scrolls the start page and an area page without pressing what is under the finger', async ({
    page,
  }) => {
    await page.setViewportSize({ width: 800, height: 400 });
    await openKiosk(page);
    const dialog = page.getByRole('dialog', { name: 'Menu' });

    const drag = async (selector: string) => {
      await dialog.evaluate((node) => node.scrollTo(0, 0));
      expect(await dialog.evaluate((node) => node.scrollHeight > node.clientHeight), 'taller than the sheet').toBe(true);
      const box = (await page.locator(selector).first().boundingBox())!;
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width / 2, box.y - 100, { steps: 12 });
      await page.mouse.up();
      await expect.poll(() => dialog.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
    };

    await drag('.menu-sheet [data-menu-area="display"]');
    await expect(sheet(page).locator('.menu-sheet__back')).toHaveCount(0);

    await openMenuArea(page, 'System');
    await drag('.menu-sheet__area-page .menu-sheet__section');
    await expect(sheet(page).locator('.menu-sheet__area-title')).toHaveText('System');
    await expect(sheet(page).getByRole('button', { name: 'Demo mode' })).toHaveAttribute('aria-pressed', 'false');
  });

  for (const viewport of KIOSK_VIEWPORTS) {
    test(`${viewport.width}x${viewport.height}: 56 px targets and no clipped label on every page`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await openKiosk(page);
      expect(await lowButtons(page, 56)).toEqual([]);
      expect(await clippedText(page, '.menu-sheet button')).toEqual([]);
      for (const area of KIOSK_AREAS) {
        await openMenuArea(page, area);
        expect(await lowButtons(page, 56), area).toEqual([]);
        expect(await clippedText(page, '.menu-sheet__area-header *'), area).toEqual([]);
        await menuBack(page);
      }
    });
  }
});

test.describe('menu areas in German', () => {
  test.use({ viewport: { width: 1024, height: 600 } });

  test('names the areas and Back in German', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
    await gotoApp(page);
    await page.locator('.picker-overlay__close').click();
    await page.getByRole('button', { name: 'Menü öffnen' }).click();

    await expect(sheet(page).locator('.menu-sheet__area-name')).toHaveText([
      'Anzeige',
      'Live-Kacheln',
      'Ansage',
      'Kalibrieren',
      'Auswertung',
      'System',
    ]);
    await openMenuArea(page, 'Kalibrieren');
    await expect(sheet(page).locator('.menu-sheet__back')).toHaveText('‹ Zurück');
    await expect(sheet(page).getByText('Radar-Neigung', { exact: true })).toBeVisible();
    const tee = sheet(page).getByRole('region', { name: 'Tee-Höhe Driver' });
    // The default 4 cm, in inches under the e2e profile's default yards.
    await expect(tee.getByTestId('driver-tee-value')).toHaveText('1,6 in');
    await expect(tee.getByRole('button', { name: 'Niedriger' })).toBeEnabled();
    await expect(tee.getByRole('button', { name: 'Höher' })).toBeEnabled();
  });
});

test.describe('menu areas on a phone', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('has the same areas without Live tiles, and taps through one and back', async ({ page }) => {
    await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
    await page.goto(`${phoneOrigin(test.info().project.use.baseURL!)}/`);
    await page.locator('.range-app').waitFor();
    const picker = page.locator('.picker-overlay__close');
    if (await picker.isVisible().catch(() => false)) await picker.tap();
    await page.getByRole('button', { name: 'Open menu' }).tap();

    await expect(sheet(page).locator('.menu-sheet__area-name')).toHaveText([
      'Display',
      'Callouts',
      'Calibrate',
      'Analysis',
      'System',
    ]);
    expect(await smallTargets(page, '.menu-sheet button')).toEqual([]);
    await openMenuArea(page, 'Display', { tap: true });
    await expect(sheet(page).getByRole('group', { name: 'Speed' })).toBeVisible();
    expect(await smallTargets(page, '.menu-sheet button')).toEqual([]);
    await menuBack(page, { tap: true });
    await expect(sheet(page).locator('.menu-sheet__shutdown')).toBeVisible();
  });
});
