import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { gotoApp, resetSession, withControlSocket } from './helpers';

const MOCK_URL = 'http://127.0.0.1:8080';
const UI_URL = 'http://127.0.0.1:5173';

async function requirePairing(request: APIRequestContext, required: boolean) {
  expect((await request.post(`${MOCK_URL}/mock/pairing`, { data: { required } })).ok()).toBe(true);
}

/** What the kiosk's pairing dialog would show. */
async function issueCode(request: APIRequestContext): Promise<{ code: string; links: Array<{ url: string }> }> {
  return (await request.post(`${MOCK_URL}/api/pairing/code`, { headers: { Origin: UI_URL } })).json();
}

const pairingHeading = (page: Page) => page.getByRole('heading', { name: 'Pair this device' });
const connected = (page: Page) => page.getByText('Connected', { exact: true });
// The app opens with the club picker; a phone in portrait gets its own layout.
const appClubPicker = (page: Page) => page.getByRole('dialog', { name: 'Select club' });

test.beforeEach(async ({ request }) => {
  await withControlSocket(resetSession);
  await requirePairing(request, true);
});

test.afterEach(async ({ request }) => {
  await requirePairing(request, false);
});

test('an unpaired phone gets the code entry instead of the companion', async ({ page }) => {
  await gotoApp(page, '/companion');

  await expect(pairingHeading(page)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Pair' })).toBeDisabled();
  await expect(page.getByText('Waiting for the first shot')).toHaveCount(0);
  await expect(connected(page)).toHaveCount(0);
});

test('the QR link pairs without typing and opens the app', async ({ page, request }) => {
  const { links } = await issueCode(request);

  await page.goto(links[0].url);

  await expect(page).toHaveURL(`${UI_URL}/`);
  await expect(appClubPicker(page)).toBeVisible();
  await expect(pairingHeading(page)).toHaveCount(0);
});

test('a typed code pairs in place after a wrong attempt', async ({ page, request }) => {
  await gotoApp(page, '/companion');
  const input = page.getByLabel('Pairing code');
  const pair = page.getByRole('button', { name: 'Pair' });

  await input.fill('wrong-234');
  await pair.tap();
  await expect(page.getByRole('alert')).toContainText('wrong or has expired');

  const { code } = await issueCode(request);
  await input.fill(`${code.slice(0, 4).toLowerCase()}-${code.slice(4)}`);
  await pair.tap();

  await expect(connected(page)).toBeVisible();
  await expect(page).toHaveURL(`${UI_URL}/companion`);
});

test('signing out all devices sends a paired phone back to pairing', async ({ page, request }) => {
  await page.goto((await issueCode(request)).links[0].url);
  await expect(appClubPicker(page)).toBeVisible();

  expect((await request.post(`${MOCK_URL}/api/pairing/revoke`)).ok()).toBe(true);

  await expect(pairingHeading(page)).toBeVisible();
  await page.reload();
  await expect(pairingHeading(page)).toBeVisible();
});

test('the pairing screen fits the viewport on a pure black page', async ({ page }, testInfo) => {
  await gotoApp(page, '/companion');
  await expect(pairingHeading(page)).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(0, 0, 0)');
  for (const control of [page.getByLabel('Pairing code'), page.getByRole('button', { name: 'Pair' })]) {
    expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  mkdirSync('test-results/companion-screens', { recursive: true });
  await page.screenshot({ path: `test-results/companion-screens/pairing-${testInfo.project.name}.png` });
});

test.describe('in German', () => {
  test.use({ locale: 'de-DE' });

  test('follows the device language before pairing', async ({ page }) => {
    await gotoApp(page, '/companion');

    await expect(page.getByRole('heading', { name: 'Gerät koppeln' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Koppeln' })).toBeVisible();
  });
});
