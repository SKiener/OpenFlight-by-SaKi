import { expect, test, type Page } from '@playwright/test';
import { clippedText, KIOSK_VIEWPORTS, mockSessionHistory, outsideScreen, smallTargets } from './helpers';

const sessions = Array.from({ length: 35 }, (_, index) => ({
  id: `session_20260930_1800${String(index).padStart(2, '0')}_range`,
  started_at: new Date(Date.UTC(2026, 8, 30, 18, 0, index)).toISOString(),
  shot_count: 40,
  clubs: ['7-iron'],
}));
// Spin as the carry used it: the first shot flew the club's typical spin, the
// last predates carry_spin_rpm (shown as —), the others their measured spin.
const carrySpin = (index: number) =>
  index === 0 ? { carry_spin_rpm: 6500, carry_spin_source: 'club_typical' }
    : index === 39 ? {} : { carry_spin_rpm: 6000, carry_spin_source: 'measured' };
const shots = Array.from({ length: 40 }, (_, index) => ({
  shot_number: index + 1, timestamp: sessions[0].started_at, club: '7-iron',
  profile_name: 'Golfer', ball_speed_mph: 100, club_speed_mph: null,
  estimated_carry_yards: 180, carry_spin_adjusted: 150, launch_angle_vertical: 18.5, spin_rpm: 6000,
  ...carrySpin(index),
}));
const savedSessions = sessions.map((summary) => ({ summary, shots }));
const spinCell = (page: Page, row: number) => page.locator('.session-history tbody tr').nth(row).locator('td').nth(6);

async function openHistory(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('openflight.unit-system', 'metric');
    localStorage.setItem('openflight.locale', 'en');
  });
  await mockSessionHistory(page, savedSessions);
  await page.goto('/');
  await page.locator('.picker-overlay__close').click();
  await page.getByRole('button', { name: 'Open menu' }).click();
  await page.getByRole('button', { name: 'Session history', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Session history' })).toBeVisible();
  await expect(page.locator('.session-history__session')).toHaveCount(35);
}

for (const size of [{ width: 1024, height: 600 }, { width: 800, height: 480 }, { width: 800, height: 400 }]) {
  test(`saved history fits ${size.width}x${size.height} and leaves live counters alone`, async ({ page }) => {
    await page.setViewportSize(size);
    await openHistory(page);
    await page.locator('.session-history__session').first().click();
    await expect(page.locator('.session-history tbody tr')).toHaveCount(40);
    await expect(page.locator('.session-history tbody tr').first()).toContainText('160.9');
    await expect(page.locator('.session-history tbody tr').first()).toContainText('137');
    await expect(page.locator('.session-history tbody tr').first()).toContainText('—');
    await expect(spinCell(page, 0)).toHaveText('≈6,500');
    await expect(spinCell(page, 1)).toHaveText('6,000');
    await expect(spinCell(page, 39)).toHaveText('—');
    const bounds = await page.locator('.session-history').boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(size.height);
    expect(await page.locator('.session-history').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    await page.screenshot({ path: `test-results/openflight-history-${size.width}x${size.height}.png` });
    await page.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(page.locator('.session-history')).toHaveCount(0);
    await expect(page.locator('.nav__badge')).toHaveCount(0);
    await page.getByRole('button', { name: 'Open menu' }).click();
    await page.getByRole('button', { name: 'Session history', exact: true }).click();
    await expect(page.locator('.session-history__session')).toHaveCount(35);
  });
}

const HEADER_BUTTONS = '.session-history__header button';
const LOCALES = [
  { id: 'en', history: 'History', showOnRange: 'Show on range' },
  { id: 'de', history: 'Verlauf', showOnRange: 'Auf der Range zeigen' },
];

for (const locale of LOCALES) {
  for (const size of KIOSK_VIEWPORTS) {
    test(`an open session keeps all five header buttons on screen at ${size.width}x${size.height} in ${locale.id}`, async ({ page }) => {
      await page.setViewportSize(size);
      await page.addInitScript((id) => localStorage.setItem('openflight.locale:v1', id), locale.id);
      await mockSessionHistory(page, savedSessions);
      await page.goto('/');
      await page.locator('.picker-overlay__close').click();
      await page.locator('.range-footer nav').getByRole('button', { name: locale.history, exact: true }).click();
      await page.locator('.session-history__session').first().click();
      await expect(page.locator('.session-history tbody tr')).toHaveCount(40);

      await expect(page.locator(HEADER_BUTTONS)).toHaveCount(5);
      await expect(page.getByRole('button', { name: locale.showOnRange, exact: true })).toBeVisible();
      expect(await outsideScreen(page, HEADER_BUTTONS)).toEqual([]);
      expect(await clippedText(page, HEADER_BUTTONS)).toEqual([]);
      expect(await clippedText(page, '.session-history__header h2, .session-history__header p')).toEqual([]);
      expect(await smallTargets(page, HEADER_BUTTONS)).toEqual([]);
      expect(await page.locator('.session-history').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      const overlapping = await page.locator(HEADER_BUTTONS).evaluateAll((buttons) => {
        const boxes = buttons.map((button) => ({ text: button.textContent, box: button.getBoundingClientRect() }));
        return boxes.flatMap((a, index) => boxes.slice(index + 1)
          .filter((b) => a.box.left < b.box.right && b.box.left < a.box.right && a.box.top < b.box.bottom && b.box.top < a.box.bottom)
          .map((b) => `${a.text} / ${b.text}`));
      });
      expect(overlapping).toEqual([]);
    });
  }
}

test('dragging a session row scrolls without opening it; failed requests can be retried', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 480 });
  await openHistory(page);
  const box = (await page.locator('.session-history__scroll').boundingBox())!;
  await page.mouse.move(box.x + 140, box.y + box.height - 25);
  await page.mouse.down();
  await page.mouse.move(box.x + 140, box.y + 30, { steps: 12 });
  await page.mouse.up();
  expect(await page.locator('.session-history__scroll').evaluate((el) => el.scrollTop)).toBeGreaterThan(50);
  await expect(page.locator('.session-history__session')).toHaveCount(35);
  await page.route('**/api/session-history/*', (route) => route.fulfill({ status: 404, json: { error: 'Missing' } }));
  await page.locator('.session-history__session').last().click();
  await expect(page.getByRole('alert')).toContainText('Could not load');
  await page.route('**/api/session-history/*', (route) => route.fulfill({ json: { ...sessions[0], shots } }));
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await expect(page.locator('.session-history tbody tr')).toHaveCount(40);
  await page.keyboard.press('Escape');
  await expect(page.locator('.session-history')).toHaveCount(0);
});

test('touch drag scrolls and a tap opens a saved session', async ({ browser }) => {
  const context = await browser.newContext({ hasTouch: true, viewport: { width: 800, height: 480 } });
  const page = await context.newPage();
  await openHistory(page);
  const cdp = await context.newCDPSession(page);
  const box = (await page.locator('.session-history__scroll').boundingBox())!;
  const x = box.x + 130;
  const y = box.y + box.height - 30;
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
  for (let step = 1; step <= 10; step++) {
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - step * 15 }] });
  }
  await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  expect(await page.locator('.session-history__scroll').evaluate((el) => el.scrollTop)).toBeGreaterThan(50);
  await expect(page.locator('.session-history__session')).toHaveCount(35);
  await page.locator('.session-history__session').nth(7).tap();
  await expect(page.locator('.session-history tbody tr')).toHaveCount(40);
  await context.close();
});
