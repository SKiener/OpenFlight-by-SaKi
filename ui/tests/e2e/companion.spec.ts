import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { gotoApp, resetSession, setClub, simulateShot, waitForEvent, withControlSocket } from './helpers';

const MUTATING_EVENTS = new Set([
  'shutdown',
  'delete_shot',
  'clear_session',
  'set_club',
  'set_training_implement',
  'set_active_profile',
  'add_profile',
  'rename_profile',
  'remove_profile',
  'set_profile_clubs',
  'set_profile_units',
  'simulate_shot',
  'toggle_debug',
  'set_radar_config',
  'set_camera_capture_settings',
  'upload_cloud',
]);

function recordSocketEvents(page: Page): string[] {
  const events: string[] = [];
  page.on('websocket', (socket) => {
    socket.on('framesent', ({ payload }) => {
      const match = /^4\d+?(\[.*)$/s.exec(String(payload));
      if (!match) return;
      try {
        const [event] = JSON.parse(match[1]) as [string];
        events.push(event);
      } catch {
        return;
      }
    });
  });
  return events;
}

async function simulateShotWithReplay() {
  return withControlSocket(async (socket) => {
    const ready = waitForEvent<{ replay_id: string }>(socket, 'camera_replay_ready', 8000);
    await simulateShot(socket);
    return ready;
  });
}

const video = (page: Page) => page.getByLabel('Strike replay');
const metricValue = (page: Page, id: string) =>
  page.locator(`.companion__live [data-metric="${id}"] .metric-card__value`);

test.beforeEach(async () => {
  await withControlSocket(resetSession);
});

test('shows live values and plays the announced replay slowly without mutating the session', async ({ page }) => {
  const sent = recordSocketEvents(page);
  const prepareRequests: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/prepare')) prepareRequests.push(request.url());
  });

  await gotoApp(page, '/companion');
  await expect(page.getByText('Waiting for the first shot')).toBeVisible();
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();

  await withControlSocket(async (socket) => {
    socket.emit('simulate_shot');
    await expect(page.getByText(/Impact detected|Shot captured/)).toBeVisible();
    await waitForEvent(socket, 'shot');
  });

  await expect(metricValue(page, 'carry')).not.toHaveText('—');
  await expect(metricValue(page, 'ball_speed')).toHaveText(/^\d+([.,]\d)?$/);
  await expect(page.locator('.companion__live [data-metric="ball_speed"] .metric-card__unit')).toHaveText('mph');
  for (const id of ['launch_v', 'launch_h', 'spin', 'club_speed', 'smash']) {
    await expect(page.locator(`.companion__live [data-metric="${id}"]`)).toBeVisible();
  }
  await expect(page.locator('.companion__live [data-metric="spin"]')).toContainText('Estimated');

  await expect(video(page)).toHaveAttribute('src', /\/api\/camera\/replays\/mock-[^/]+\/video$/);
  await expect.poll(() => video(page).evaluate((element: HTMLVideoElement) => element.playbackRate)).toBe(0.25);
  expect(
    await video(page).evaluate((element: HTMLVideoElement) => [element.muted, element.loop, element.playsInline])
  ).toEqual([true, true, true]);
  await expect(page.getByRole('button', { name: '0.25×' })).toHaveAttribute('aria-pressed', 'true');

  await page.getByRole('button', { name: '0.1×' }).tap();
  await expect.poll(() => video(page).evaluate((element: HTMLVideoElement) => element.playbackRate)).toBe(0.1);
  await expect(page.getByLabel('Scrub replay')).toBeVisible();

  expect(prepareRequests).toEqual([]);
  expect(sent.filter((event) => MUTATING_EVENTS.has(event))).toEqual([]);
  expect(sent).toContain('get_session');
});

test('prepares the replay itself when it connects after the ready event', async ({ page }) => {
  const { replay_id: replayId } = await simulateShotWithReplay();

  const prepare = page.waitForRequest((request) => request.url().includes(`/api/camera/replays/${replayId}/prepare`));
  await gotoApp(page, '/companion');

  expect((await prepare).method()).toBe('POST');
  await expect(video(page)).toHaveAttribute('src', new RegExp(`/api/camera/replays/${replayId}/video$`));
});

test('tap selects a session shot; dragging the list scrolls without selecting', async ({ page }) => {
  for (let index = 0; index < 3; index += 1) await simulateShotWithReplay();
  await gotoApp(page, '/companion');
  const list = page.locator('.companion__shot-list');
  await expect(list.getByRole('button')).toHaveCount(3);
  await page.addStyleTag({ content: '.companion__shot-list { max-height: 6.5rem; }' });
  await expect(page.getByRole('button', { name: 'Show shot 3' })).toHaveAttribute('aria-pressed', 'true');

  await list.scrollIntoViewIfNeeded();
  const box = (await list.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height - 10);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2, box.y + 5, { steps: 8 });
  await page.mouse.up();
  await expect.poll(() => list.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect(page.getByRole('button', { name: 'Show shot 3' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText(/Viewing shot/)).toHaveCount(0);

  const first = page.getByRole('button', { name: 'Show shot 1' });
  await first.scrollIntoViewIfNeeded();
  const firstCarry = (await first.locator('span').nth(2).innerText()).split(' ')[0];
  await first.tap();
  await expect(page.getByText('Viewing shot 1')).toBeVisible();
  await expect(first).toHaveAttribute('aria-pressed', 'true');
  await expect(metricValue(page, 'carry')).toHaveText(firstCarry);
  await expect(video(page)).toHaveAttribute('src', /\/video$/);

  await page.getByRole('button', { name: 'Back to live' }).tap();
  await expect(page.getByText('Shot 3', { exact: true })).toBeVisible();
});

test('is installable to the home screen, scoped to the companion', async ({ page, request }) => {
  await gotoApp(page, '/companion');

  const manifestHref = await page.locator('link[rel="manifest"]').getAttribute('href');
  expect(manifestHref).toBe('/companion-manifest.json');
  await expect(page.locator('meta[name="apple-mobile-web-app-capable"]')).toHaveAttribute('content', 'yes');
  await expect(page.locator('link[rel="apple-touch-icon"]')).toHaveAttribute('href', /\.png$/);
  await expect(page).toHaveTitle('OpenFlight Companion');

  const manifest = await (await request.get(manifestHref!)).json();
  expect(manifest).toMatchObject({ start_url: '/companion', scope: '/companion', display: 'standalone' });
  expect((await request.get(manifest.icons[0].src)).ok()).toBe(true);
});

test('fits the viewport on a pure black page', async ({ page }, testInfo) => {
  await simulateShotWithReplay();
  await gotoApp(page, '/companion');
  await expect(video(page)).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(0, 0, 0)');
  for (const id of ['carry', 'ball_speed']) {
    const tile = (await page.locator(`.companion__hero [data-metric="${id}"]`).boundingBox())!;
    const value = (await page.locator(`.companion__hero [data-metric="${id}"] .metric-card__unit`).boundingBox())!;
    expect(value.x + value.width).toBeLessThanOrEqual(tile.x + tile.width + 1);
  }
  mkdirSync('test-results/companion-screens', { recursive: true });
  await page.screenshot({ path: `test-results/companion-screens/${testInfo.project.name}.png`, fullPage: true });
});

test.describe('in German', () => {
  test.use({ locale: 'de-DE' });

  test('follows the device language', async ({ page }) => {
    await gotoApp(page, '/companion');
    await expect(page.getByText('Warte auf den ersten Schlag')).toBeVisible();
    await expect(page.getByText('Nur Anzeige. Bedienung am Gerät.')).toBeVisible();
  });

  test('keeps every German tile label inside its tile', async ({ page }) => {
    await gotoApp(page, '/companion');
    await withControlSocket(async (socket) => simulateShot(socket));
    await expect(page.locator('.companion__tile').first()).toBeVisible();
    const overflowing = await page.locator('.companion__tile').evaluateAll((tiles) =>
      tiles.flatMap((tile) => {
        const label = tile.querySelector('.metric-card__label');
        if (!label) return [];
        const box = tile.getBoundingClientRect();
        const text = label.getBoundingClientRect();
        return text.left < box.left - 1 || text.right > box.right + 1 ? [label.textContent ?? ''] : [];
      })
    );
    expect(overflowing).toEqual([]);
  });
});

const dispersionPoints = (page: Page) => page.locator('.dispersion-plot__point');

async function simulateClubShots(plan: [club: string, count: number][]) {
  await withControlSocket(async (socket) => {
    for (const [club, count] of plan) {
      await setClub(socket, club);
      for (let index = 0; index < count; index += 1) await simulateShot(socket);
    }
  });
}

async function openDispersion(page: Page) {
  await gotoApp(page, '/companion');
  await page.getByRole('button', { name: 'Streuung', exact: true }).tap();
  await expect(page.getByRole('region', { name: 'Streuung' })).toBeVisible();
}

test.describe('Streuung', () => {
  test.use({ locale: 'de-DE' });

  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('openflight.unit-system', 'mph-meters'));
  });

  test('plots session and saved shots honestly with per-club stats on equal axes', async ({ page }, testInfo) => {
    const sent = recordSocketEvents(page);
    await simulateClubShots([
      ['7-iron', 2],
      ['driver', 1],
    ]);
    await openDispersion(page);

    await expect(dispersionPoints(page)).toHaveCount(3);
    await expect(page.locator('[data-hollow="true"]')).toHaveCount(3);
    await expect(
      page.getByText('Seitenwinkel nicht kalibriert – die seitliche Lage kann systematisch verschoben sein')
    ).toBeVisible();

    await page.getByRole('button', { name: '30 Tage' }).tap();
    await expect(dispersionPoints(page)).toHaveCount(17);
    await expect(page.locator('[data-hollow="true"]')).toHaveCount(4);
    await expect(page.getByText('1 ohne Richtung (nicht dargestellt)')).toBeVisible();
    await expect(page.getByText('Carry mit Ersatzspin berechnet (5 von 17 Schlägen)')).toBeVisible();
    await expect(page.locator('.dispersion-plot__ellipse')).toHaveCount(2);
    const clubs = page.locator('.dispersion-table tbody');
    expect(await clubs.evaluateAll((rows) => rows.map((row) => row.getAttribute('data-club')))).toEqual([
      'wedge-54',
      '7-iron',
      'driver',
    ]);
    await expect(clubs.nth(1)).toContainText('+1 nicht dargestellt');

    await page.getByRole('button', { name: '7 Tage' }).tap();
    await expect(dispersionPoints(page)).toHaveCount(11);
    await expect(page.locator('.dispersion-plot__ellipse')).toHaveCount(1);

    const gridSpacing = (axis: 'x' | 'y') =>
      page.locator(`.dispersion-plot__grid-${axis} line`).evaluateAll((lines, attribute) => {
        const [first, second] = lines.map((line) => Number(line.getAttribute(attribute)));
        return Math.abs(second - first);
      }, `${axis}1`);
    expect(await gridSpacing('x')).toBeCloseTo(await gridSpacing('y'), 3);
    const svg = page.locator('.dispersion-plot svg');
    const svgBox = (await svg.boundingBox())!;
    expect(svgBox.width).toBeCloseTo(Number(await svg.getAttribute('width')), 0);
    expect(svgBox.height).toBeCloseTo(Number(await svg.getAttribute('height')), 0);

    const plot = (await page.locator('.dispersion-plot').boundingBox())!;
    const table = (await page.locator('.dispersion-table').boundingBox())!;
    if (testInfo.project.name === 'ipad-landscape') expect(plot.x + plot.width).toBeLessThanOrEqual(table.x);
    else expect(plot.y + plot.height).toBeLessThanOrEqual(table.y);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);

    mkdirSync('test-results/companion-screens', { recursive: true });
    await page.screenshot({
      path: `test-results/companion-screens/dispersion-${testInfo.project.name}.png`,
      fullPage: true,
    });
    expect(sent.filter((event) => MUTATING_EVENTS.has(event))).toEqual([]);
  });

  test('club chips filter with a pinned Alle; dragging the chips scrolls without selecting', async ({ page }) => {
    await simulateClubShots([
      ['7-iron', 2],
      ['driver', 1],
    ]);
    await openDispersion(page);
    await page.getByRole('button', { name: '30 Tage' }).tap();
    await expect(dispersionPoints(page)).toHaveCount(17);
    await page.addStyleTag({ content: '.dispersion__chip-scroll { flex: 0 0 7rem; }' });

    const all = page.getByRole('button', { name: 'Alle (18)' });
    const chips = page.locator('.dispersion__chip-scroll');
    const box = (await chips.boundingBox())!;
    await page.mouse.move(box.x + box.width - 8, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 4, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
    await expect.poll(() => chips.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
    await expect(all).toHaveAttribute('aria-pressed', 'true');

    const iron = page.getByRole('button', { name: 'Eisen 7 (9)' });
    await iron.tap();
    await expect(iron).toHaveAttribute('aria-pressed', 'true');
    await expect(dispersionPoints(page)).toHaveCount(8);
    await expect(page.locator('.dispersion-table tbody')).toHaveCount(1);

    await all.tap();
    await expect(all).toHaveAttribute('aria-pressed', 'true');
    await expect(dispersionPoints(page)).toHaveCount(17);
  });

  test('a tapped shot opens its details and replay; an expired saved replay says so', async ({ page }, testInfo) => {
    await simulateClubShots([['7-iron', 1]]);
    await openDispersion(page);

    await dispersionPoints(page).first().tap();
    const dialog = page.getByRole('dialog', { name: 'Schlagdetails' });
    await expect(
      dialog.getByText(/^Seitlich: .+ · abgeleitet aus Carry × sin HLA · Herkunft der Richtung/)
    ).toBeVisible();
    await expect(dialog.locator('[data-metric="carry"] .metric-card__unit')).toHaveText('m');
    await expect(dialog.getByLabel('Schlagaufnahme')).toHaveAttribute(
      'src',
      /\/api\/camera\/replays\/mock-[^/]+\/video$/
    );
    await page.screenshot({ path: `test-results/companion-screens/dispersion-detail-${testInfo.project.name}.png` });
    await dialog.getByRole('button', { name: 'Schließen' }).tap();
    await expect(dialog).toBeHidden();

    await page.getByRole('button', { name: '30 Tage' }).tap();
    await page.getByRole('button', { name: 'Driver: Carry 229 m, 36 m links' }).tap();
    await expect(dialog.getByText('Diese Aufnahme ist nicht mehr verfügbar')).toBeVisible();
    await expect(dialog.getByText(/^Seitlich: 36 m links$/)).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
  });

  test('drops the aim warning once the server reports a horizontal phase reference', async ({ page }) => {
    await page.routeWebSocket(/socket\.io/, (ws) => {
      const server = ws.connectToServer();
      server.onMessage((message) =>
        ws.send(String(message).replace('"horizontal_phase_reference":false', '"horizontal_phase_reference":true'))
      );
    });
    await simulateClubShots([['7-iron', 1]]);
    await openDispersion(page);

    await expect(dispersionPoints(page)).toHaveCount(1);
    await expect(page.getByText(/Seitenwinkel nicht kalibriert/)).toHaveCount(0);
  });

  test('reports a failed history load and retries it', async ({ page }) => {
    await simulateClubShots([['7-iron', 1]]);
    await page.route('**/api/session-history', (route) => route.fulfill({ status: 503, body: '' }));
    await openDispersion(page);

    await page.getByRole('button', { name: '7 Tage' }).tap();
    await expect(page.getByRole('alert')).toContainText('Gespeicherte Sessions konnten nicht geladen werden');
    await expect(dispersionPoints(page)).toHaveCount(1);

    await page.unroute('**/api/session-history');
    await page.getByRole('button', { name: 'Aktualisieren' }).tap();
    await expect(dispersionPoints(page)).toHaveCount(9);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('uses the sun palette by default, also where the old theme key says dark', async ({ page }, testInfo) => {
    await page.addInitScript(() => localStorage.setItem('openflight.theme', 'dark'));
    await simulateClubShots([['7-iron', 2]]);
    await openDispersion(page);
    await page.getByRole('button', { name: '30 Tage' }).tap();

    await expect(dispersionPoints(page)).toHaveCount(16);
    await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(page.getByRole('button', { name: /^Eisen 7: / }).first()).toHaveCSS('color', 'rgb(27, 175, 122)');
    await page.screenshot({
      path: `test-results/companion-screens/dispersion-sun-${testInfo.project.name}.png`,
      fullPage: true,
    });
  });
});
