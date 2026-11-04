import { expect, test, type Page } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { KIOSK_VIEWPORTS, clippedText, footerButton, outsideScreen } from './helpers';
import { alignStep, viewToggle } from './alignView';
import { camera, openRange } from './phoneFixture';

/**
 * The kiosk's setup window at start: with a capture camera it lies above the club picker, shows the live
 * camera picture with the Align crosshair and the saved ball mark, the three steps and the marks' status.
 * "Fertig" leaves the club picker; "Ausrichten" opens the Align view on the ball step. Set
 * SETUP_REMINDER_SCREENSHOTS to a folder to save a picture of each size and theme.
 */

const BALL = { frame_x_px: 191.5, frame_y_px: 279.5, preview_x_pct: 30, preview_y_pct: 70, set_at: 1_761_389_200 };
const SCREENSHOTS = process.env.SETUP_REMINDER_SCREENSHOTS;

async function openKiosk(page: Page, baseURL: string, theme: string) {
  await page.addInitScript((value) => localStorage.setItem('openflight.theme.v2', value), theme);
  await openRange(page, { origin: baseURL, keepStartOverlays: true });
  await page.evaluate(
    async (settings) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings(settings);
    },
    { ...camera('640x400'), ball_mark: BALL, target_line: null }
  );
  await expect(page.locator('.setup-reminder')).toBeVisible();
}

const reminder = (page: Page) => page.getByRole('dialog', { name: 'Kamera auf den Ball einstellen' });

for (const viewport of KIOSK_VIEWPORTS) {
  for (const theme of ['sun', 'dark']) {
    test.describe(`kiosk ${viewport.width}×${viewport.height} ${theme}`, () => {
      test.use({ viewport });

      test('opens above the club picker with the camera picture, the steps and the marks', async ({
        page,
        baseURL,
      }) => {
        await openKiosk(page, baseURL!, theme);
        const dialog = reminder(page);
        await expect(dialog).toBeVisible();
        // The picker is open under it; the reminder takes the taps.
        await expect(page.locator('.picker-overlay')).toHaveCount(1);
        const topmost = await page.evaluate(
          ({ width, height }) => document.elementFromPoint(width / 2, height / 2)?.closest('.setup-reminder') !== null,
          viewport
        );
        expect(topmost).toBe(true);
        // It covers the picker exactly, footer included.
        const box = (await dialog.boundingBox())!;
        expect(box).toEqual((await page.locator('.picker-overlay').boundingBox())!);
        expect(box.y + box.height).toBeLessThanOrEqual(viewport.height + 0.5);

        await expect(dialog.getByText('Setup', { exact: true })).toBeVisible();
        await expect(dialog.locator('.setup-reminder__steps li')).toHaveText([
          'Ball auf die Abschlagposition legen.',
          'Kamera einstellen, bis der Ball im Fadenkreuz liegt.',
          'Ball und Ziel unter „Ausrichten“ antippen.',
        ]);
        await expect(dialog.locator('.setup-reminder__marks')).toHaveText('Ballmarke: gesetzt · Ziel: fehlt');

        // The live picture, in its 16:10 frame, with the crosshair and the ball's ring on it.
        await page.waitForFunction(() => {
          const image = document.querySelector<HTMLImageElement>('.setup-reminder__image');
          return Boolean(image?.complete && image.naturalWidth > 0);
        });
        const frame = (await page.locator('.setup-reminder__frame').boundingBox())!;
        expect(frame.width / frame.height).toBeCloseTo(1.6, 1);
        const column = (await page.locator('.setup-reminder__picture').boundingBox())!;
        expect(frame.x).toBeGreaterThanOrEqual(column.x - 0.5);
        expect(frame.x + frame.width).toBeLessThanOrEqual(column.x + column.width + 0.5);
        expect(frame.y + frame.height).toBeLessThanOrEqual(column.y + column.height + 0.5);
        // As big as fits: it fills the column's width or its height.
        expect(Math.max(frame.width / column.width, frame.height / column.height)).toBeGreaterThan(0.99);
        await expect(page.locator('.setup-reminder__frame .camera-feed__hairline')).toHaveCount(2);
        // Placed in percent of the picture (inside the frame's 1 px border), as in the Align view.
        const picture = (await page.locator('.setup-reminder__image').boundingBox())!;
        const at = (box: { x: number; y: number; width: number; height: number }) => [
          box.x + box.width / 2,
          box.y + box.height / 2,
        ];
        const pct = (x: number, y: number) => [
          picture.x + (picture.width * x) / 100,
          picture.y + (picture.height * y) / 100,
        ];
        const near = (actual: number[], expected: number[]) =>
          expect(Math.hypot(actual[0] - expected[0], actual[1] - expected[1])).toBeLessThan(0.75);
        near(at((await page.locator('.setup-reminder__frame .camera-feed__center-cross').boundingBox())!), pct(50, 78));
        near(at((await page.locator('.setup-reminder__frame .camera-feed__mark--ball').boundingBox())!), pct(30, 70));

        // Nothing clipped or off screen; the steps in body type; kiosk-sized buttons.
        const text =
          '.setup-reminder__eyebrow, .setup-reminder__title, .setup-reminder__steps li, .setup-reminder__marks, .setup-reminder .panel-action';
        expect(await clippedText(page, text)).toEqual([]);
        expect(await outsideScreen(page, '.setup-reminder button, .setup-reminder__marks')).toEqual([]);
        const side = page.locator('.setup-reminder__side');
        expect(await side.evaluate((element) => element.scrollHeight <= element.clientHeight + 1)).toBe(true);
        const marks = (await dialog.locator('.setup-reminder__marks').boundingBox())!;
        const actions = (await dialog.locator('.setup-reminder__actions').boundingBox())!;
        expect(marks.y + marks.height).toBeLessThanOrEqual(actions.y);
        const rem = await page.evaluate(() => parseFloat(getComputedStyle(document.documentElement).fontSize));
        for (const size of await dialog
          .locator('.setup-reminder__steps li')
          .evaluateAll((items) => items.map((item) => parseFloat(getComputedStyle(item).fontSize))))
          expect(size).toBeGreaterThanOrEqual(rem - 0.01);
        for (const button of await dialog.getByRole('button').all()) {
          const size = (await button.boundingBox())!;
          expect(size.height, await button.textContent()).toBeGreaterThanOrEqual(56);
          expect(size.width, await button.textContent()).toBeGreaterThanOrEqual(56);
        }

        if (SCREENSHOTS) {
          mkdirSync(SCREENSHOTS, { recursive: true });
          await page.screenshot({ path: join(SCREENSHOTS, `setup-${viewport.width}x${viewport.height}-${theme}.png`) });
        }
      });

      test('"Fertig" leaves the club picker; "Ausrichten" opens the Align view on the ball step', async ({
        page,
        baseURL,
      }) => {
        await openKiosk(page, baseURL!, theme);
        await reminder(page).getByRole('button', { name: 'Fertig' }).click();
        await expect(page.locator('.setup-reminder')).toHaveCount(0);
        await expect(page.getByRole('dialog', { name: 'Schläger wählen' })).toBeVisible();

        await openKiosk(page, baseURL!, theme);
        await reminder(page).getByRole('button', { name: 'Ausrichten' }).click();
        await expect(page.locator('.setup-reminder')).toHaveCount(0);
        await expect(page.locator('.picker-overlay')).toHaveCount(0);
        await expect(footerButton(page, 'Ausrichten')).toHaveAttribute('aria-pressed', 'true');
        await expect(page.locator('.camera-feed')).toBeVisible();
        // On the ball step, also with the ball mark stored, and on the close-up.
        await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
        await expect(alignStep(page, '2 Ziel')).toHaveAttribute('aria-pressed', 'false');
        await expect(viewToggle(page)).toHaveText('Ganzes Bild');
      });
    });
  }
}

test.describe('kiosk 1024×600', () => {
  test.use({ viewport: { width: 1024, height: 600 } });

  test('the header close button is "Fertig", and the picture refreshes every second until it closes', async ({
    page,
    baseURL,
  }) => {
    let previews = 0;
    page.on('request', (request) => {
      if (request.url().includes('/api/camera/preview.jpg')) previews += 1;
    });
    await openKiosk(page, baseURL!, 'sun');
    await page.waitForTimeout(2_500);
    expect(previews).toBeGreaterThanOrEqual(3);

    await reminder(page).getByRole('button', { name: 'Kamera auf den Ball einstellen schließen' }).click();
    await expect(page.locator('.setup-reminder')).toHaveCount(0);
    await expect(page.locator('.picker-overlay')).toHaveCount(1);
    const closed = previews;
    await page.waitForTimeout(2_500);
    // The Live view under it does not poll the picture.
    expect(previews).toBe(closed);
  });

  test('the phone and a server without a capture camera never show it', async ({ page, baseURL }) => {
    await openRange(page, { keepStartOverlays: true });
    await expect(page.locator('.picker-overlay')).toHaveCount(1);
    await expect(page.locator('.setup-reminder')).toHaveCount(0);

    await page.goto(`${baseURL}/`);
    await page.locator('.picker-overlay').waitFor();
    await page.waitForTimeout(1_000);
    await expect(page.locator('.setup-reminder')).toHaveCount(0);
  });
});
