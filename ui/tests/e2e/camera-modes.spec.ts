import { expect, test } from '@playwright/test';
import { clippedText } from './helpers';

test('mode buttons are touch-sized, selectable and do not hide the footer', async ({ page }) => {
  await page.goto('/tests/e2e/fixtures/camera-layout.html');
  const group = page.getByRole('group', { name: 'Auflösung und Bildrate' });
  const button = group.getByRole('button', { name: '640 × 200 560 fps' });
  await button.scrollIntoViewIfNeeded();
  const box = (await button.boundingBox())!;
  expect(box.height).toBeGreaterThanOrEqual(48);
  expect(box.width).toBeGreaterThanOrEqual(70);
  await button.tap();
  await expect(button).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Nach oben' })).toBeVisible();
  const settings = page.getByRole('complementary', { name: 'Kamera einstellen' });
  const footer = (await page.locator('.range-footer').boundingBox())!;
  const settingsBox = (await settings.boundingBox())!;
  expect(settingsBox.y + settingsBox.height).toBeLessThan(footer.y);
  await page.screenshot({ path: 'test-results/camera-modes-1024x600.png' });
  const previousSelection = await button.getAttribute('aria-pressed');
  const scrollBefore = await settings.evaluate((element) => element.scrollTop);
  await page.mouse.move(box.x + 20, box.y + box.height - 5);
  await page.mouse.down();
  await page.mouse.move(box.x + 20, box.y - 80, { steps: 15 });
  await page.mouse.up();
  await expect(button).toHaveAttribute('aria-pressed', previousSelection!);
  expect(await settings.evaluate((element) => element.scrollTop)).toBeGreaterThan(scrollBefore);
});

const KIOSK = [
  { width: 1024, height: 600 },
  { width: 800, height: 480 },
  { width: 800, height: 400 },
];

for (const viewport of KIOSK) {
  test(`the server's modes show with their labels at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await page.goto('/tests/e2e/fixtures/camera-layout.html');
    const group = page.getByRole('group', { name: 'Auflösung und Bildrate' });
    const buttons = group.getByRole('button');
    await expect(buttons).toHaveText([/640 × 400\s*300 fps/, /640 × 200\s*500 fps/, /640 × 200\s*560 fps/]);
    const strip560 = group.getByRole('button', { name: '640 × 200 560 fps' });
    await strip560.scrollIntoViewIfNeeded();
    const settings = (await page.getByRole('complementary', { name: 'Kamera einstellen' }).boundingBox())!;
    for (const button of await buttons.all()) {
      const box = (await button.boundingBox())!;
      // 2.4rem: about 45 px on the 800-wide kiosks (112.5 % root), 48+ at 1024 wide.
      expect(box.height, await button.textContent()).toBeGreaterThanOrEqual(44);
      expect(box.width, await button.textContent()).toBeGreaterThanOrEqual(70);
      expect(box.x).toBeGreaterThanOrEqual(settings.x - 0.5);
      expect(box.x + box.width).toBeLessThanOrEqual(settings.x + settings.width + 0.5);
    }
    expect(await clippedText(page, '.camera-settings__mode-controls button')).toEqual([]);
    await strip560.click();
    await expect(strip560).toHaveAttribute('aria-pressed', 'true');
    await expect(group.getByRole('button', { name: '640 × 200 500 fps' })).toHaveAttribute('aria-pressed', 'false');
  });
}
