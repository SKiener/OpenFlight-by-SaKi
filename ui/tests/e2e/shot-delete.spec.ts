import { expect, test, type Page } from '@playwright/test';
import {
  canvases,
  chooseMenuItem,
  clippedText,
  footerButton,
  gotoApp,
  hitAShot,
  KIOSK_VIEWPORTS,
  LANDED_TIMEOUT_MS,
  mockSessionHistory,
  rangeButton,
  resetSession,
  resetUnits,
  savedShot,
  SCENE_TIMEOUT_MS,
  setClub,
  smallTargets,
  withControlSocket,
  withShotStore,
} from './helpers';

const SHOTS = 'test-results/pw';
const SERVER = process.env.E2E_SOCKET_URL ?? 'http://127.0.0.1:8080';

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

const shotTimestamps = (page: Page): Promise<string[]> =>
  withShotStore(page, (store) => store.getState().shots.map((shot: { timestamp: string }) => shot.timestamp));

async function savedTimestampsToday(page: Page): Promise<string[]> {
  return page.evaluate(async (server) => {
    const today = new Date().toISOString().slice(0, 10);
    const list = await (await fetch(`${server}/api/session-history`, { cache: 'no-store' })).json();
    const ids: string[] = list.sessions
      .filter((session: { started_at: string }) => session.started_at.startsWith(today))
      .map((session: { id: string }) => session.id);
    const sessions = await Promise.all(
      ids.map(async (id) => (await fetch(`${server}/api/session-history/${encodeURIComponent(id)}`)).json())
    );
    return sessions.flatMap((session: { shots?: { timestamp: string }[] }) =>
      (session.shots ?? []).map(({ timestamp }) => timestamp)
    );
  }, SERVER);
}

function barOnOneLine(page: Page): Promise<boolean> {
  return page.locator('.virtual-range__bar > *').evaluateAll((elements) => {
    const middles = elements
      .filter((element) => element.getBoundingClientRect().width > 0)
      .map((element) => {
        const { top, bottom } = element.getBoundingClientRect();
        return (top + bottom) / 2;
      });
    return Math.max(...middles) - Math.min(...middles) <= 2;
  });
}

async function expectFingerFriendly(page: Page, selector: string): Promise<void> {
  expect(await smallTargets(page, selector)).toEqual([]);
  expect(await clippedText(page, selector)).toEqual([]);
}

for (const size of KIOSK_VIEWPORTS) {
  test.describe(`deleting a shot at ${size.width}×${size.height} in German`, () => {
    test.use({ viewport: size });

    test('the kiosk Live view deletes the last shot after a confirm', async ({ page }) => {
      await openGerman(page);
      await hitAShot(page);
      await hitAShot(page);
      const heading = page.locator('.kiosk-live__header h1');
      await expect(heading).toHaveText('Schlag 2');
      const [, last] = await shotTimestamps(page);

      const button = page.locator('.range-delete-last');
      await expect(button).toBeEnabled();
      await expect(button).toHaveText('Löschen');
      await expectFingerFriendly(page, '.kiosk-live__actions button');
      expect(await clippedText(page, '.kiosk-live__header > *')).toEqual([]);
      await button.tap();
      await expect(button).toHaveText('Wirklich löschen?');
      await expect(button).toHaveClass(/confirm-delete--confirming/);
      expect(await shotTimestamps(page), 'nothing goes before the second tap').toHaveLength(2);
      await expectFingerFriendly(page, '.kiosk-live__actions button');
      if (size.width === 800 && size.height === 400)
        await page.screenshot({ path: `${SHOTS}/delete-live-confirm-800x400.png` });

      await button.tap();
      await expect(heading).toHaveText('Schlag 1');
      expect(await shotTimestamps(page)).not.toContain(last);
      await expect(button).toHaveText('Löschen');
      expect(await savedTimestampsToday(page)).not.toContain(last);
    });

    test('the Range bar deletes the shot it shows and shows the previous one at rest', async ({ page }) => {
      test.setTimeout(120_000);
      await openGerman(page);
      await rangeButton(page).tap();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      const bar = page.locator('.virtual-range__bar');
      const carry = bar.locator('.virtual-range__stat--carry .virtual-range__value');
      const target = bar.locator('.virtual-range__stat--target .virtual-range__value');

      await hitAShot(page);
      await expect(target).not.toHaveText('—', { timeout: LANDED_TIMEOUT_MS });
      const firstCarry = await carry.innerText();
      await hitAShot(page);
      await expect.poll(() => shotTimestamps(page)).toHaveLength(2);
      await expect(target).not.toHaveText('—', { timeout: LANDED_TIMEOUT_MS });
      const [, second] = await shotTimestamps(page);

      const button = bar.locator('.virtual-range__delete');
      await expect(button).toHaveText('Löschen');
      await expect(button).toHaveAttribute('aria-label', 'Letzten Schlag löschen');
      await expect(bar.locator('.virtual-range__stat--speed')).toBeVisible();
      await expectFingerFriendly(page, '.virtual-range__delete');
      expect(await clippedText(page, '.virtual-range__bar > *, .virtual-range__bar .virtual-range__value')).toEqual([]);
      if (size.height !== 480)
        await page.screenshot({ path: `${SHOTS}/delete-range-${size.width}x${size.height}.png` });
      await button.tap();
      await expect(button).toHaveText('Wirklich löschen?');
      await expectFingerFriendly(page, '.virtual-range__delete');
      expect(await barOnOneLine(page), 'the bar stays on one line while the delete asks to confirm').toBe(true);
      expect(await clippedText(page, '.virtual-range__bar > *, .virtual-range__bar .virtual-range__value')).toEqual([]);
      await expect(bar.locator('.virtual-range__stat--speed')).toBeVisible();
      if (size.width === 800 && size.height === 400)
        await page.screenshot({ path: `${SHOTS}/delete-range-confirm-800x400.png` });
      await button.tap();

      await expect.poll(() => shotTimestamps(page)).not.toContain(second);
      await expect(carry).toHaveText(firstCarry);
      await expect(target, 'the previous shot rests, it does not fly again').not.toHaveText('—');
      await expect(button).toHaveText('Löschen');
    });

    test('the history dialog deletes a row, shows the session without it and keeps its place', async ({ page }) => {
      const id = 'session_20261008_181500_range';
      let shots = Array.from({ length: 30 }, (_, index) => index + 1).map((n) =>
        savedShot(`2026-10-08T18:${9 + n}:00+02:00`, {
          shot_number: n,
          club: '7-iron',
          ball_speed_mph: 100 + n,
          estimated_carry_yards: 150,
          carry_spin_adjusted: 150 + n,
        })
      );
      const deleted: string[] = [];
      await mockSessionHistory(page, [
        { summary: { id, started_at: '2026-10-08T16:15:00.000Z', shot_count: 30, clubs: ['7-iron'] }, shots },
      ]);
      await page.route(`**/api/session-history/${id}*`, (route) =>
        route.fulfill({
          json: { id, started_at: '2026-10-08T16:15:00.000Z', shot_count: shots.length, clubs: ['7-iron'], shots },
        })
      );
      await page.route(`**/api/session-history/${id}/shots/*`, (route) => {
        const timestamp = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop()!);
        expect(route.request().method()).toBe('DELETE');
        deleted.push(timestamp);
        shots = shots.filter((shot) => shot.timestamp !== timestamp);
        return route.fulfill({ status: 204 });
      });

      await openGerman(page);
      await footerButton(page, 'Verlauf').tap();
      await page.locator('.session-history__session').first().tap();
      const rows = page.locator('.session-history tbody tr');
      await expect(rows).toHaveCount(30);
      const scroller = page.locator('.session-history__scroll');
      await rows.nth(20).scrollIntoViewIfNeeded();
      const scrolled = await scroller.evaluate((el) => el.scrollTop);
      expect(scrolled).toBeGreaterThan(0);
      const button = rows.nth(20).locator('.session-history__delete');
      await expect(button).toHaveText('Löschen');
      await expectFingerFriendly(page, '.session-history tbody button');
      await button.tap();
      await expect(button).toHaveText('Wirklich löschen?');
      await expectFingerFriendly(page, '.session-history tbody button');
      expect(await page.locator('.session-history').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
      if (size.width === 1024) await page.screenshot({ path: `${SHOTS}/delete-history-1024x600.png` });
      await button.tap();

      await expect(rows).toHaveCount(29);
      expect(deleted).toEqual(['2026-10-08T18:30:00+02:00']);
      await expect(rows.nth(19)).toContainText('120');
      await expect(rows.nth(20)).toContainText('122');
      expect(await scroller.evaluate((el) => el.scrollTop), 'the list keeps its place').toBeCloseTo(scrolled, 0);
    });

    test('the shot list deletes its row after a confirm', async ({ page }) => {
      await openGerman(page);
      await hitAShot(page);
      await hitAShot(page);
      const [first, second] = await shotTimestamps(page);
      await page.getByRole('button', { name: 'Menü öffnen' }).tap();
      await chooseMenuItem(page, 'Schläge', { tap: true });

      const button = page.getByRole('button', { name: 'Schlag 1 löschen', exact: true });
      await expect(button).toHaveText('Löschen');
      await expectFingerFriendly(page, '.shots-panel__actions button');
      await button.tap();
      const confirming = page.locator('.shots-panel__delete.confirm-delete--confirming');
      await expect(confirming).toHaveText('Wirklich löschen?');
      await expectFingerFriendly(page, '.shots-panel__actions button');
      if (size.width === 800 && size.height === 400)
        await page.screenshot({ path: `${SHOTS}/delete-list-800x400.png` });
      await confirming.tap();

      await expect(page.locator('.shots-panel__row')).toHaveCount(1);
      expect(await shotTimestamps(page)).toEqual([second]);
      expect(first).not.toBe(second);
    });
  });
}
