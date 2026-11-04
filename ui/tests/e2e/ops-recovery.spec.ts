import { expect, test } from '@playwright/test';

for (const theme of ['sun', 'dark']) {
  test(`OPS recovery stays readable and reports completion in German (${theme})`, async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 600 });
    await page.goto('/tests/e2e/fixtures/ops-recovery.html');
    await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
    const button = page.getByRole('button', { name: 'OPS-Radar wiederherstellen', exact: true });
    await expect(button).toBeInViewport();
    const box = (await button.boundingBox())!;
    expect(box.height).toBeGreaterThanOrEqual(48);
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(1024);
    await button.click();
    const pending = page.getByRole('button', { name: 'OPS-Radar wird wiederhergestellt…' });
    await expect(pending).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Einstellungen', exact: true })).toBeDisabled();
    await pending.dispatchEvent('click');
    expect(await page.evaluate(() => Reflect.get(window, 'recoveryTest').calls())).toBe(1);
    await page.evaluate(() => Reflect.get(window, 'recoveryTest').finish({ ok: true, code: 'commands_sent' }));
    await expect(button).toBeEnabled();
    await expect(page.getByRole('status')).toContainText('noch nicht bestätigt');
    await expect(page.getByRole('status')).toBeInViewport();
    await button.click();
    await page.evaluate(() => Reflect.get(window, 'recoveryTest').finish({ ok: false, code: 'failed' }));
    await expect(page.getByRole('status')).toContainText('fehlgeschlagen');
    await expect(button).toBeEnabled();
    await button.click();
    await page.evaluate(() => Reflect.get(window, 'recoveryTest').finish({ ok: false, code: 'timeout' }));
    await expect(page.getByRole('status')).toContainText('möglicherweise noch');
    await expect(button).toBeEnabled();
  });
}

test('a rejected slider change resets to the server value, even when that value has not changed', async ({ page }) => {
  await page.goto('/tests/e2e/fixtures/ops-recovery.html');
  await page.getByRole('button', { name: 'Einstellungen', exact: true }).click();
  const power = page.locator('.slider-control').filter({ hasText: 'Sendeleistung' }).getByRole('slider');
  await power.scrollIntoViewIfNeeded();
  const box = (await power.boundingBox())!;
  await power.click({ position: { x: box.width - 3, y: box.height / 2 } });
  await expect(page.getByRole('status')).toContainText('Änderung nicht übernommen');
  await expect(power).toHaveValue('3');
});
