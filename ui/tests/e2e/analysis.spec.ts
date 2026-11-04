import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { mockSessionHistory } from './helpers';

const replay = { id: 'recording-one', frame_count: 600, trigger_frame: 240, playback_fps: 60, capture_fps: 300, duration_seconds: 10, display_mirror_horizontal: false };
const sessions = [{ id: 'session_20261003_143038_range', started_at: '2026-10-03T14:30:38+02:00', shot_count: 3, clubs: ['8-iron', 'driver'] }];
const shots = [
  { shot_number: 1, timestamp: '2026-10-03T14:31:00+02:00', club: '8-iron', profile_name: 'Golfer', ball_speed_mph: 100, club_speed_mph: null, estimated_carry_yards: 150, carry_spin_adjusted: 140, carry_spin_source: 'club_typical', launch_angle_vertical: 20, spin_rpm: 5000, spin_quality: 'experimental', spin_confidence: 0.59, camera_replay: replay },
  { shot_number: 2, timestamp: '2026-10-03T14:32:00+02:00', club: '8-iron', profile_name: 'Golfer', ball_speed_mph: 110, club_speed_mph: null, estimated_carry_yards: 170, carry_spin_adjusted: 160, launch_angle_vertical: 22, spin_rpm: null },
  { shot_number: 3, timestamp: '2026-10-03T14:33:00+02:00', club: 'driver', profile_name: 'Other', ball_speed_mph: 150, club_speed_mph: null, estimated_carry_yards: 200, carry_spin_adjusted: null, launch_angle_vertical: null, spin_rpm: null },
];
const savedSessions = sessions.map((summary) => ({ summary, shots }));
async function openAnalysis(page: Page, theme = 'dark', locale = 'de') {
  await page.addInitScript(({ theme, locale }) => {
    localStorage.setItem('openflight.locale:v1', locale);
    localStorage.setItem('openflight.theme.v2', theme);
    localStorage.setItem('openflight.unit-system', 'mph-meters');
  }, { theme, locale });
  await mockSessionHistory(page, savedSessions);
  await page.goto('/?view=analysis');
  await expect(page.getByTestId('shot-count')).toHaveText('3');
}

for (const theme of ['dark', 'sun']) for (const locale of ['de', 'en']) {
  test(`${locale}/${theme}: readable analysis, filters and isolated read-only page`, async ({ page }) => {
    const requests: string[] = [];
    page.on('request', (request) => { if (request.url().includes('/api/') || request.url().includes('/socket.io/')) requests.push(`${request.method()} ${request.url()}`); });
    await openAnalysis(page, theme, locale);
    await expect(page.locator('canvas')).toHaveCount(2);
    await expect(page.getByTestId('mean-carry')).toContainText(locale === 'de' ? '152,4 m' : '152.4 m');
    await page.getByRole('combobox', { name: locale === 'de' ? 'Schläger' : 'Club', exact: true }).selectOption('8-iron');
    await expect(page.getByTestId('shot-count')).toHaveText('2');
    await expect(page.getByTestId('mean-carry')).toContainText(locale === 'de' ? '137,2 m' : '137.2 m');
    const controls = await page.locator('.analysis-page button, .analysis-page select, .analysis-page input, .analysis-page nav a').evaluateAll((elements) => elements.map((el) => ({ text: el.textContent, height: el.getBoundingClientRect().height, font: parseFloat(getComputedStyle(el).fontSize) })));
    for (const control of controls) { expect(control.height, control.text ?? '').toBeGreaterThanOrEqual(55); expect(control.font).toBeGreaterThanOrEqual(18); }
    expect(await page.locator('.analysis-scroll').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    const labelsFit = await page.locator('.analysis-kpis h2').evaluateAll((elements) => elements.every((el) => {
      const box = el.closest('.analysis-card')!.getBoundingClientRect();
      const range = document.createRange(); range.selectNodeContents(el);
      return [...range.getClientRects()].every((rect) => rect.right <= box.right && rect.left >= box.left);
    }));
    expect(labelsFit).toBe(true);
    expect(requests.every((request) => request.startsWith('GET ') && /\/api\/(session-history|pairing\/status)/.test(request))).toBe(true);
    await page.getByTestId('mean-carry').scrollIntoViewIfNeeded();
    await page.screenshot({ path: `test-results/openflight-analysis-${locale}-${theme}.png` });
  });
}

test('filtered CSV and JSON contain the same selection, units and provenance', async ({ page }) => {
  await openAnalysis(page);
  await page.getByRole('combobox', { name: 'Schläger', exact: true }).selectOption('8-iron');
  for (const [button, extension] of [['Schläge · CSV', 'csv'], ['Schlägerstatistik · CSV', 'csv'], ['Daten & Metadaten · JSON', 'json']]) {
    const pending = page.waitForEvent('download');
    await page.getByRole('button', { name: button, exact: true }).click();
    const downloaded = await pending;
    expect(downloaded.suggestedFilename()).toContain(`.${extension}`);
    const text = await readFile((await downloaded.path())!, 'utf8');
    expect(text).not.toContain('"driver"');
    if (extension === 'json') { const data = JSON.parse(text); expect(data.shots).toHaveLength(2); expect(data.display_units).toBe('mph-meters'); expect(data.statistics_by_club[0].spin.n).toBe(0); }
    else { expect(text).toContain('8-iron'); expect(text).toContain('mph'); expect(text).toContain('carry_'); }
  }
});

test('shot details expose replay and MP4 download, missing clips stay unavailable', async ({ page }) => {
  await openAnalysis(page);
  let prepares = 0;
  await page.route('**/api/camera/replays/*/prepare', (route) => { prepares++; return route.fulfill({ json: { ...replay, video_url: '/api/camera/replays/recording-one/video' } }); });
  await page.route('**/api/camera/replays/*/video', (route) => route.fulfill({ contentType: 'video/mp4', body: Buffer.from('test-mp4-download') }));
  await page.getByRole('button', { name: 'Schlagdetails #1', exact: true }).tap();
  const detail = page.getByRole('region', { name: 'Schlagdetails' });
  await expect(detail).toContainText('Kein bestätigter Spin');
  await expect(detail).toContainText('experimental · 59%');
  await expect(detail).toContainText('Carry nutzt geschätzten Spin');
  const pending = page.waitForEvent('download');
  await detail.getByRole('button', { name: 'MP4 herunterladen' }).click();
  expect((await pending).suggestedFilename()).toContain('shot-1.mp4');
  expect(prepares).toBe(1);
  await page.route('**/api/camera/replays/*/prepare', (route) => route.fulfill({ status: 503, json: { error: 'Replay unavailable' } }));
  await detail.getByRole('button', { name: /Replay|Zeitlupe/ }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByRole('dialog')).toContainText('Replay unavailable');
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button', { name: 'Schlagdetails #2', exact: true }).tap();
  await expect(detail).toContainText('Kein gespeichertes Video');
  await expect(detail.getByRole('button', { name: 'MP4 herunterladen' })).toHaveCount(0);
});

test('failed session is warned about and refresh recovers, empty filter is explicit', async ({ page }) => {
  await openAnalysis(page);
  await page.route('**/api/session-history/*', (route) => route.fulfill({ status: 404, json: {} }));
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('1 Sitzungen');
  await expect(page.getByTestId('shot-count')).toHaveText('0');
  await page.route('**/api/session-history/*', (route) => route.fulfill({ json: { ...sessions[0], shots } }));
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click();
  await expect(page.getByTestId('shot-count')).toHaveText('3');
  await page.getByLabel('Von', { exact: true }).fill('2026-10-04');
  await expect(page.getByRole('status')).toContainText('Keine Schläge');
  await expect(page.getByRole('button', { name: 'Schläge · CSV' })).toBeDisabled();
});

test('dragging scrolls the touchscreen page without activating details', async ({ page }) => {
  await openAnalysis(page);
  const scroller = page.locator('.analysis-scroll');
  const box = (await scroller.boundingBox())!;
  await page.mouse.move(box.x + 10, box.y + box.height - 20);
  await page.mouse.down();
  await page.mouse.move(box.x + 10, box.y + 20, { steps: 10 });
  await page.mouse.up();
  expect(await scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(100);
  await expect(page.getByRole('region', { name: 'Schlagdetails' })).toHaveCount(0);
});

test('long sessions paginate and sorting keeps shot identity', async ({ page }) => {
  await openAnalysis(page);
  const many = Array.from({ length: 123 }, (_, index) => ({ ...shots[0], shot_number: index + 1, timestamp: new Date(Date.UTC(2026, 9, 3, 12, index)).toISOString(), ball_speed_mph: 100 + index, camera_replay: undefined }));
  await page.route('**/api/session-history/*', (route) => route.fulfill({ json: { ...sessions[0], shots: many } }));
  await page.getByRole('button', { name: 'Aktualisieren', exact: true }).click();
  await expect(page.getByTestId('shot-count')).toHaveText('123');
  await expect(page.locator('tbody tr')).toHaveCount(50);
  await page.getByRole('button', { name: 'Weiter', exact: true }).click();
  await expect(page.locator('tbody tr').first()).toContainText('#51');
  await page.getByRole('combobox', { name: 'Sortierung', exact: true }).selectOption('fastest');
  await expect(page.locator('tbody tr').first()).toContainText('#123');
});

test('session history offers the analysis page without clipping the kiosk header', async ({ page }) => {
  await openAnalysis(page);
  await page.goto('/');
  await page.locator('.picker-overlay__close').click();
  await page.getByRole('button', { name: 'Menü öffnen' }).click();
  await page.getByRole('button', { name: 'Session-Verlauf', exact: true }).click();
  await expect(page.locator('.session-history__session')).toHaveCount(1);
  await page.locator('.session-history__session').first().click();
  await expect(page.locator('.session-history tbody tr')).toHaveCount(3);
  expect(await page.locator('.session-history').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  const link = page.getByRole('button', { name: 'Auswertung & Export', exact: true });
  await expect(link).toBeVisible();
  await link.click();
  await expect(page.getByTestId('shot-count')).toHaveText('3');
});

for (const theme of ['dark', 'sun']) test(`${theme}: saved replay exposes slow-motion rates with readable controls`, async ({ page }) => {
  await page.addInitScript(() => { HTMLMediaElement.prototype.play = () => Promise.resolve(); });
  await openAnalysis(page, theme);
  await page.route('**/api/camera/replays/*/prepare', (route) => route.fulfill({ json: { ...replay, video_url: '/api/camera/replays/recording-one/video' } }));
  await page.route('**/api/camera/replays/*/video', () => new Promise(() => {}));
  await page.getByRole('button', { name: 'Schlagdetails #1', exact: true }).click();
  await page.getByRole('region', { name: 'Schlagdetails' }).getByRole('button', { name: /Replay|Zeitlupe/ }).click();
  const dialog = page.getByRole('dialog');
  for (const rate of ['0.1×', '0.05×', '0.025×']) {
    const button = dialog.getByRole('button', { name: rate, exact: true });
    await button.click();
    await expect(button).toHaveAttribute('aria-pressed', 'true');
  }
  const bounds = await dialog.boundingBox();
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(600);
  expect(await dialog.evaluate((el) => getComputedStyle(el).color)).toBe('rgb(255, 255, 255)');
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth && el.scrollHeight <= el.clientHeight)).toBe(true);
});

test('interrupting video play with another control is not a corrupt-video error', async ({ page }) => {
  await page.addInitScript(() => { HTMLMediaElement.prototype.play = () => Promise.reject(new DOMException('Play interrupted by pause', 'AbortError')); });
  await openAnalysis(page);
  await page.route('**/api/camera/replays/*/prepare', (route) => route.fulfill({ json: { ...replay, video_url: '/api/camera/replays/recording-one/video' } }));
  await page.route('**/api/camera/replays/*/video', () => new Promise(() => {}));
  await page.getByRole('button', { name: 'Schlagdetails #1', exact: true }).click();
  await page.getByRole('region', { name: 'Schlagdetails' }).getByRole('button', { name: /Replay|Zeitlupe/ }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Abspielen', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Video konnte nicht abgespielt werden', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('dialog').getByRole('button', { name: '0.1×', exact: true })).toBeVisible();
});

test('genuine playback rejection still reports an error', async ({ page }) => {
  await page.addInitScript(() => { HTMLMediaElement.prototype.play = () => Promise.reject(new DOMException('Unsupported video', 'NotSupportedError')); });
  await openAnalysis(page);
  await page.route('**/api/camera/replays/*/prepare', (route) => route.fulfill({ json: { ...replay, video_url: '/api/camera/replays/recording-one/video' } }));
  await page.route('**/api/camera/replays/*/video', () => new Promise(() => {}));
  await page.getByRole('button', { name: 'Schlagdetails #1', exact: true }).click();
  await page.getByRole('region', { name: 'Schlagdetails' }).getByRole('button', { name: /Replay|Zeitlupe/ }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Abspielen', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('Video konnte nicht abgespielt werden', { exact: true })).toBeVisible();
});
