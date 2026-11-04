import { test, expect } from '@playwright/test';
import {
  gotoApp,
  KIOSK_VIEWPORTS,
  liveMetricValueFontSizes,
  openMenuArea,
  overflowingLiveMetricValues,
  simulateShot,
  withControlSocket,
} from './helpers';

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`German interface at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    test('switches languages, persists German, and keeps all launch labels readable', async ({ page }, testInfo) => {
      await page.route('**/api/session-history', (route) => route.fulfill({ json: { sessions: [] } }));
      await withControlSocket(async (socket) => {
        await simulateShot(socket);
      });
      await gotoApp(page);
      await page.getByRole('button', { name: 'Close Select club' }).click();
      await page.getByRole('button', { name: 'Open menu' }).click();
      await openMenuArea(page, 'Display');
      await page.getByRole('combobox', { name: 'Language', exact: true }).selectOption('de');
      await expect(page.locator('html')).toHaveAttribute('lang', 'de-DE');
      await expect(page.getByRole('combobox', { name: 'Sprache', exact: true })).toHaveValue('de');
      await page.getByRole('button', { name: 'Menü schließen' }).click();
      await expect(page.getByRole('button', { name: /^Abflugwinkel / })).toBeVisible();
      await expect(page.getByRole('button', { name: /^Startrichtung / })).toBeVisible();
      const sizes = await liveMetricValueFontSizes(page);
      expect(new Set(sizes.map((size) => size.toFixed(2))).size).toBe(1);
      expect(Math.min(...sizes)).toBeGreaterThanOrEqual(viewport.width === 1024 ? 48 : 36);
      expect(await overflowingLiveMetricValues(page)).toEqual([]);
      const clippedLabels = await page.locator('.live-panel__grid .metric-card__label').evaluateAll((labels) =>
        labels.flatMap((label) => {
          const card = label.closest('.metric-card')!.getBoundingClientRect();
          const rects = [...label.childNodes]
            .filter((node) => node.nodeType === Node.TEXT_NODE)
            .map((node) => {
              const range = document.createRange();
              range.selectNodeContents(node);
              return range.getBoundingClientRect();
            });
          const icon = label.querySelector('svg');
          if (icon) rects.push(icon.getBoundingClientRect());
          return rects.some((rect) => rect.right > card.right - 2 || rect.left < card.left) ? [label.textContent] : [];
        })
      );
      expect(clippedLabels).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath('german-live.png') });

      await page.reload();
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).click();
      await expect(page.locator('html')).toHaveAttribute('lang', 'de-DE');
      await page.getByRole('button', { name: 'Menü öffnen' }).click();
      await page.getByRole('button', { name: 'Session-Verlauf', exact: true }).click();
      await expect(page.getByText('Keine gespeicherten Schläge in den letzten 30 Tagen.')).toBeVisible();
      await page.getByRole('button', { name: 'Schließen', exact: true }).click();
      await page.getByRole('button', { name: 'Menü öffnen' }).click();
      await openMenuArea(page, 'Anzeige');
      await page.getByRole('combobox', { name: 'Sprache', exact: true }).selectOption('en');
      await page.getByRole('button', { name: 'Close menu' }).click();
      await expect(page.getByRole('button', { name: /^Launch angle / })).toBeVisible();
      await expect(page.getByRole('button', { name: /^Start direction / })).toBeVisible();
    });
  });
}
