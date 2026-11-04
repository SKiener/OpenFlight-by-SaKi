import { test } from '@playwright/test';
import {
  expect,
  gotoApp,
  KIOSK_VIEWPORTS,
  liveMetricValueFontSizes,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  resetUnits,
  simulateShot,
  WIDEST_LIVE_METRIC_VALUE,
  withControlSocket,
} from './helpers';

/** Dismiss the club picker that opens on every load, keeping the default club. */
async function dismissPicker(page: import('@playwright/test').Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

/** The Range overview shows only the hero tiles; the ten-tile grid lives behind "All metrics". */
async function openLiveDetails(page: import('@playwright/test').Page) {
  await dismissPicker(page);
  await page.locator('.range-details-button').tap();
  await expect(page.locator('.range-details')).toBeVisible();
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`live metrics at ${viewport.width}×${viewport.height}`, () => {
    test.use({ hasTouch: true, viewport });

    for (const unitSystem of ['metric', 'imperial']) {
      test(`keeps larger ${unitSystem} value text inside each live tile`, async ({ page }, testInfo) => {
        await page.addInitScript((units) => {
          localStorage.setItem('openflight.unit-system', units);
        }, unitSystem);
        const { shot } = await withControlSocket(async (socket) => {
          // A profile's own units win over the stored preference once the roster arrives.
          await resetUnits(socket, unitSystem);
          return simulateShot(socket) as Promise<{ shot: { apex_yards: number | null } }>;
        });

        await gotoApp(page);
        await openLiveDetails(page);

        await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(11);
        // The apex tile shows the shot's own apex, a whole number in m or yds (≈ in front when its carry flew typical spin).
        expect(shot.apex_yards, 'apex_yards').toBeGreaterThan(0);
        const apex = Math.round(unitSystem === 'metric' ? shot.apex_yards! * 0.9144 : shot.apex_yards!);
        const apexTile = page.locator('.live-panel__grid [data-metric="apex"]');
        await expect(apexTile.locator('.metric-card__value')).toHaveText(new RegExp(`^(≈\u00a0)?${apex}$`));
        await expect(apexTile.locator('.metric-card__unit')).toHaveText(unitSystem === 'metric' ? 'm' : 'yds');
        await expect(page.locator('.live-panel__grid [data-metric="ball_speed"] .metric-card__unit')).toHaveText(
          unitSystem === 'metric' ? 'km/h' : 'mph'
        );

        const sizes = await liveMetricValueFontSizes(page);
        expect(sizes.length).toBe(11);
        expect(new Set(sizes.map((size) => size.toFixed(2))).size).toBe(1);

        await expect(page.locator('.live-panel__grid .metric-card__value').first()).toHaveCSS('font-weight', '800');
        expect(Math.min(...sizes)).toBeGreaterThanOrEqual(viewport.width === 1024 ? 48 : 36);
        expect(await overflowingLiveMetricValues(page)).toEqual([]);
        await testInfo.attach('live-values', { body: JSON.stringify(sizes), contentType: 'application/json' });
        await page.screenshot({ path: testInfo.outputPath('live-readability.png') });

        await page.locator('.live-panel__grid .metric-card__value').evaluateAll((values, widest) => {
          for (const value of values) {
            value.textContent = widest;
          }
        }, WIDEST_LIVE_METRIC_VALUE);
        await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

        expect(await overflowingLiveMetricValues(page)).toEqual([]);
      });
    }

    test('keeps long metric subtitles readable inside each live tile', async ({ page }) => {
      // App bug: a wrapped long caption pushes the nowrap "experimental" pill below a details tile
      // that shows captions (9-13 px at 800x480 and 1024x600 in the old 5 × 2 grid). In the 4 × 3
      // grid of eleven tiles the kiosk tiles drop their captions under 96 px of content (panel.css):
      // always at 800x400 and 800x480, and at 1024x600 when the carry notes take the room under the
      // grid (the mock's random spin decides). The bug is expected only where captions show.
      await withControlSocket(async (socket) => {
        await simulateShot(socket);
      });

      await gotoApp(page);
      await openLiveDetails(page);
      await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(11);

      const subtitles = [
        'spin-adjusted',
        'camera assisted (experimental)',
        'camera fused (experimental)',
        'rejected: insufficient post trigger frames',
        'player + implement',
        '99 swings',
        '99 readings',
        '199.9 mph trigger',
        'straight',
        'candidate',
      ];
      await page.locator('.live-panel__grid .metric-card').evaluateAll((cards, text) => {
        cards.forEach((card, index) => {
          const meta = card.querySelector('.metric-card__meta');
          if (!(meta instanceof HTMLElement)) return;

          const subtitle = document.createElement('span');
          subtitle.className = 'metric-card__subtext metric-card__confidence-label';
          subtitle.textContent = text[index];
          meta.replaceChildren(subtitle);

          if (index === 2 || index === 3) {
            const confidence = document.createElement('div');
            confidence.className = 'metric-card__confidence metric-card__confidence--high';
            confidence.innerHTML =
              '<span class="metric-card__confidence-dots"><span class="dot filled"></span><span class="dot filled"></span><span class="dot filled"></span></span><span class="metric-card__confidence-label">experimental</span>';
            meta.append(confidence);
          }
        });
      }, subtitles);

      const captionsShown = await page
        .locator('.live-panel__grid .metric-card__subtext')
        .evaluateAll((captions) => captions.some((caption) => caption.getClientRects().length > 0));
      test.fail(captionsShown, 'experimental pill leaves short details tiles under a long caption');
      expect(await overflowingLiveMetricMetadata(page)).toEqual([]);
    });
  });
}
