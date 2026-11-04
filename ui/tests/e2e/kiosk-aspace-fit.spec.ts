import { expect, test, type Page } from '@playwright/test';
import {
  chooseMenuItem,
  clippedText,
  gotoApp,
  hitAShot,
  labelsPastPadding,
  KIOSK_VIEWPORTS,
  resetSession,
  resetUnits,
  setClub,
  withControlSocket,
} from './helpers';

/*
 * The kiosk's shot list and its All values toolbar in a•space, the wide typeface whose digits are
 * proportional (no tabular figures): "4" is its widest digit (0.99 em), "1" its narrowest (0.35 em).
 * Columns sized by CSS alone must hold the widest realistic value of their kind, built from 4s, not
 * the fixtures' values, whose narrow 1s hide an overflow.
 */

const SHOTS_PANEL = '.shots-panel';
/** The widest realistic value of each column, in the order ball, club, launch, spin, carry. */
const WIDEST = ['244,4', '144,4', '24,4', '≈12.444', '≈244'];

test.use({ hasTouch: true });

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await resetSession(socket);
    await resetUnits(socket);
    await setClub(socket, 'driver');
  });
});

test.afterAll(async () => {
  await withControlSocket((socket) => resetSession(socket));
});

async function openGerman(page: Page): Promise<void> {
  await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
  await gotoApp(page);
  await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
}

/** Ink of each cell of `rows` (a grid per row): left and right edge, in column order. */
function cellInks(page: Page, rows: string): Promise<{ text: string; left: number; right: number }[][]> {
  return page.locator(rows).evaluateAll((grids) =>
    grids.map((grid) =>
      [...grid.children].flatMap((cell) => {
        const range = document.createRange();
        range.selectNodeContents(cell);
        const ink = range.getBoundingClientRect();
        return ink.width > 0 ? [{ text: cell.textContent?.trim() ?? '', left: ink.left, right: ink.right }] : [];
      })
    )
  );
}

/** Neighbouring cells of a row whose text comes closer than `gap` px (or runs into each other). */
function touching(rows: { text: string; left: number; right: number }[][], gap = 4): string[] {
  return rows.flatMap((cells) =>
    cells.slice(1).flatMap((cell, index) => {
      const before = cells[index];
      return cell.left - before.right < gap
        ? [`${before.text} | ${cell.text}: ${(cell.left - before.right).toFixed(1)}`]
        : [];
    })
  );
}

for (const size of KIOSK_VIEWPORTS) {
  test.describe(`a•space on the kiosk at ${size.width}×${size.height} in German`, () => {
    test.use({ viewport: size });

    test('the shot list keeps its header, column headings, club and widest values whole and apart', async ({
      page,
    }) => {
      await openGerman(page);
      await hitAShot(page);
      await hitAShot(page);
      await page.getByRole('button', { name: 'Menü öffnen' }).tap();
      await chooseMenuItem(page, 'Schläge', { tap: true });
      await expect(page.locator('.shots-panel__row')).toHaveCount(2);

      // The panel header: title, counts and club whole, beside its two buttons.
      expect
        .soft(
          await clippedText(
            page,
            `${SHOTS_PANEL} .panel-header__title, ${SHOTS_PANEL} .panel-header__subtitle, ${SHOTS_PANEL} .panel-header__club, ${SHOTS_PANEL} .panel-header__actions button`
          ),
          'panel header'
        )
        .toEqual([]);
      expect
        .soft(await labelsPastPadding(page, `${SHOTS_PANEL} .panel-header__actions button`), 'header buttons')
        .toEqual([]);
      const header = await page
        .locator(`${SHOTS_PANEL} .panel-header`)
        .evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
      expect.soft(header.scroll, 'panel header scrollWidth').toBeLessThanOrEqual(header.client);

      // Every row with the widest value of each column.
      await page.locator('.shots-panel__row-main').evaluateAll((rows, widest) => {
        for (const row of rows)
          [...row.children].slice(2, 7).forEach((cell, index) => (cell.textContent = widest[index]));
      }, WIDEST);

      // The club under the profile and every value inside its own column; headings (at the 12 px floor,
      // "SCHLÄGER" may reach into the gap before its column) and values apart from their neighbours.
      expect
        .soft(
          await clippedText(page, '.shots-panel__profile-club, .shots-panel__row-main > .shots-panel__value'),
          'columns'
        )
        .toEqual([]);
      expect.soft(touching(await cellInks(page, '.shots-panel__columns')), 'column headings').toEqual([]);
      expect.soft(touching(await cellInks(page, '.shots-panel__row-main')), 'row values').toEqual([]);
    });

    test('All values keeps every toolbar button and the legend whole', async ({ page }) => {
      await openGerman(page);
      await hitAShot(page);
      await page.locator('.range-details-button').tap();
      const toolbar = page.locator('.range-details-toolbar');
      await expect(toolbar).toBeVisible();
      for (const name of ['Ball', 'Schläger']) {
        await toolbar.getByRole('button', { name, exact: true }).tap();
        expect
          .soft(await clippedText(page, '.range-details-toolbar button, .range-details-toolbar > span'), name)
          .toEqual([]);
        expect.soft(await labelsPastPadding(page, '.range-details-toolbar button'), name).toEqual([]);
        const bar = await toolbar.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
        expect.soft(bar.scroll, `${name}: toolbar scrollWidth`).toBeLessThanOrEqual(bar.client);
      }
    });
  });
}
