import { expect, request as apiRequest, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { gotoApp, KIOSK_VIEWPORTS, openMenuArea } from './helpers';

const SERVER_URL = 'http://127.0.0.1:8080';
const CODE_PATTERN = /^[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$/;
/** Loopback through a proxy counts as remote, so this stands in for a phone on the LAN. */
const AS_PHONE = { 'X-Forwarded-For': '192.168.1.50' };

async function openKioskMenu(page: Page) {
  await gotoApp(page);
  await page.getByRole('button', { name: 'Close Select club' }).click();
  await page.getByRole('button', { name: 'Open menu' }).click();
}

/** A phone with its own cookie jar. */
function newPhone(): Promise<APIRequestContext> {
  return apiRequest.newContext({ baseURL: SERVER_URL, extraHTTPHeaders: AS_PHONE });
}

/** Every element of the dialog panel stays inside the panel and the viewport, and the panel does not scroll. */
async function expectDialogFits(dialog: Locator) {
  const fit = await dialog.evaluate((panel) => {
    const box = panel.getBoundingClientRect();
    const outside = [...panel.querySelectorAll('*')]
      .map((element) => ({ element, rect: element.getBoundingClientRect() }))
      .filter(({ rect }) => rect.width > 0 && rect.height > 0)
      .filter(
        ({ rect }) =>
          rect.left < Math.max(0, box.left) - 0.5 ||
          rect.top < Math.max(0, box.top) - 0.5 ||
          rect.right > Math.min(window.innerWidth, box.right) + 0.5 ||
          rect.bottom > Math.min(window.innerHeight, box.bottom) + 0.5
      )
      .map(({ element }) => element.className || element.tagName);
    return {
      outside,
      scrolls: panel.scrollHeight > panel.clientHeight || panel.scrollWidth > panel.clientWidth,
      panelInViewport:
        box.top >= 0 && box.left >= 0 && box.bottom <= window.innerHeight && box.right <= window.innerWidth,
    };
  });
  expect(fit).toEqual({ outside: [], scrolls: false, panelInViewport: true });
}

async function access(phone: APIRequestContext) {
  const status = await phone.get('/api/pairing/status');
  const history = await phone.get('/api/session-history');
  return { state: (await status.json()).state, history: history.status() };
}

for (const viewport of KIOSK_VIEWPORTS) {
  test(`kiosk shows a scannable pairing code at ${viewport.width}×${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await openKioskMenu(page);
    await page.getByRole('button', { name: 'Pair device' }).click();

    const dialog = page.getByRole('dialog', { name: 'Pair device' });
    const qr = dialog.getByRole('img', { name: /^QR code to pair via / });
    const code = dialog.getByLabel('Pairing code');
    await expect(qr).toHaveAttribute('src', /^data:image\/svg\+xml/);
    await expect(code).toHaveText(CODE_PATTERN);
    await expect(dialog.getByText(/^Valid for [45]:\d\d$/)).toBeVisible();
    await expect(dialog.getByText(/:8080\/pair and enter the code/)).toBeVisible();

    const qrMode = dialog.getByRole('group', { name: 'QR code' });
    const [pairMode, wifiMode] = [
      qrMode.getByRole('button', { name: 'Pair', exact: true }),
      qrMode.getByRole('button', { name: 'Wi-Fi', exact: true }),
    ];
    await expect(pairMode).toHaveAttribute('aria-pressed', 'true');
    await expect(dialog.getByText('e2e-dummy-pass-1234')).toBeVisible();
    mkdirSync('test-results/pairing-screens', { recursive: true });

    for (const mode of ['pair', 'wifi'] as const) {
      if (mode === 'wifi') {
        await wifiMode.click();
        await expect(wifiMode).toHaveAttribute('aria-pressed', 'true');
        await expect(dialog.getByRole('img', { name: 'QR code to join the TestNet Wi-Fi' })).toHaveAttribute(
          'src',
          /^data:image\/svg\+xml/
        );
        await expect(dialog.getByText('Scan with the phone camera to join the "TestNet" Wi-Fi.')).toBeVisible();
        await expect(dialog.getByText(/:8080\/pair and enter the code/)).toHaveCount(0);
        await expect(dialog.getByRole('group', { name: 'Address' })).toHaveCount(0);
      }
      await expect(code).toHaveText(CODE_PATTERN);
      await expect(dialog.getByText(/^Valid for [45]:\d\d$/)).toBeVisible();
      await expect(dialog.getByText('TestNet', { exact: true })).toBeVisible();
      await expectDialogFits(dialog);
      expect((await dialog.getByRole('img').boundingBox())!.width).toBeGreaterThanOrEqual(180);
      for (const [button, minHeight] of [
        [dialog.getByRole('button', { name: 'New code', exact: true }), 44],
        [dialog.getByRole('button', { name: 'Close', exact: true }), 44],
        [pairMode, 38],
        [wifiMode, 38],
      ] as const) {
        expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(minHeight);
      }
      await page.screenshot({
        path: `test-results/pairing-screens/kiosk-${mode}-${viewport.width}x${viewport.height}.png`,
      });
    }

    await dialog.getByRole('button', { name: 'New code', exact: true }).click();
    await expect(pairMode).toHaveAttribute('aria-pressed', 'true');
    await expect(qr).toHaveAttribute('src', /^data:image\/svg\+xml/);

    const phone = await newPhone();
    const exchange = await phone.post('/api/pairing/exchange', { data: { code: await code.innerText() } });
    expect(exchange.status()).toBe(200);
    expect(exchange.headers()['set-cookie']).toMatch(/^openflight_device=[\w-]{40,};.*HttpOnly.*SameSite=Strict/i);
    expect(await access(phone)).toEqual({ state: 'paired', history: 200 });
    await phone.dispose();
    await expect(dialog.getByText('Device paired')).toBeVisible();

    await dialog.getByRole('button', { name: 'Close', exact: true }).click();
    await expect(dialog).toBeHidden();
  });
}

test('kiosk signs out every paired device after confirmation', async ({ page, request }) => {
  const [phone, stranger] = [await newPhone(), await newPhone()];
  const { code } = await (await request.post(`${SERVER_URL}/api/pairing/code`)).json();
  expect((await phone.post('/api/pairing/exchange', { data: { code } })).status()).toBe(200);
  expect(await access(stranger)).toEqual({ state: 'unpaired', history: 401 });
  expect(await access(phone)).toEqual({ state: 'paired', history: 200 });

  await page.setViewportSize({ width: 1024, height: 600 });
  await openKioskMenu(page);
  await openMenuArea(page, 'System');
  await page.getByRole('button', { name: 'Sign out all devices' }).click();
  const dialog = page.getByRole('dialog', { name: 'Sign out all devices?' });
  await expect(dialog).toContainText('This display stays signed in.');
  await dialog.getByRole('button', { name: 'Sign out all', exact: true }).click();

  await expect(page.getByRole('dialog', { name: 'All devices are signed out.' })).toBeVisible();
  expect(await access(phone)).toEqual({ state: 'unpaired', history: 401 });
  expect((await request.get(`${SERVER_URL}/api/session-history`)).status()).toBe(200);
  await Promise.all([phone.dispose(), stranger.dispose()]);
});
