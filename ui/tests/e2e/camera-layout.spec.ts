import { expect, test } from '@playwright/test';
import { gotoApp } from './helpers';

test('loaded preview and scrollable settings stay above the kiosk footer', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  await gotoApp(page, '/tests/e2e/fixtures/camera-layout.html');
  await expect(page.getByRole('img', { name: 'Kameravorschau' })).toBeVisible();
  const settings = page.getByRole('complementary', { name: 'Kamera einstellen' });
  const settingsBox = (await settings.boundingBox())!;
  const footer = (await page.locator('.range-footer').boundingBox())!;
  const stream = (await page.locator('.camera-feed__stream').boundingBox())!;
  expect(settingsBox.y + settingsBox.height).toBeLessThan(footer.y);
  expect(stream.y + stream.height).toBeLessThan(footer.y);
  await expect(settings.getByRole('button', { name: 'Nach oben' })).toHaveCount(0);
  // Enough drags to reach the end. Each one scrolls the panel by its height less 60 px (238 px here); the
  // settings' notes wrap to more lines in a•space, about 45 % wider than Arial: 3,790 px of content (Arial
  // 3,080) take 15 drags where Arial took 12.
  for (let i = 0; i < 16; i += 1) {
    await page.mouse.move(settingsBox.x + 8, settingsBox.y + settingsBox.height - 30);
    await page.mouse.down();
    await page.mouse.move(settingsBox.x + 8, settingsBox.y + 30, { steps: 12 });
    await page.mouse.up();
  }
  const summary = (await page.locator('.camera-settings__section--summary').boundingBox())!;
  // Dragged to the end: the summary's end shows inside the panel, above the footer. With the
  // outdoor type (13 px captions) the summary is taller than the panel, so its top may sit above it.
  expect(summary.y + summary.height).toBeLessThanOrEqual(settingsBox.y + settingsBox.height + 1);
  expect(summary.y + summary.height).toBeGreaterThan(settingsBox.y);
  expect(summary.y + summary.height).toBeLessThan(footer.y);
});
