import { expect, test, type Page } from '@playwright/test';
import {
  alignControls,
  alignStep,
  chooseTargetStep,
  frameAreaOnScreen,
  framePctPerPixel,
  framePointOnScreen,
  pictureBox,
  sceneBox,
  showFullPicture,
  STREAM,
  tapFramePoint,
  viewToggle,
} from './alignView';
import { clippedText, labelsPastPadding } from './helpers';
import { camera, openRange, type Setup } from './phoneFixture';

/**
 * The two marks of the Align view: the step chips choose what a tap on the picture sets (the view
 * opens on the ball; a tap never changes the step), the tap is sent in percent of the camera frame
 * through the view shown (the close-up it opens with, or the whole picture), and the ball, the
 * target and the dashed line between them are drawn on the frame in either view, on the kiosk and
 * on phones in both orientations. The marks' status heads the settings panel.
 */

const DEVICES = [
  { name: 'kiosk 1024×600', viewport: { width: 1024, height: 600 }, mobile: false },
  { name: 'kiosk 800×480', viewport: { width: 800, height: 480 }, mobile: false },
  { name: 'kiosk 800×400', viewport: { width: 800, height: 400 }, mobile: false },
  { name: 'kiosk 720×1280', viewport: { width: 720, height: 1280 }, mobile: false },
  { name: 'iPhone 15 portrait', viewport: { width: 393, height: 659 }, mobile: true },
  { name: 'iPhone 15 landscape', viewport: { width: 734, height: 343 }, mobile: true },
];

const BALL = {
  frame_x_px: 191.5,
  frame_y_px: 279.5,
  preview_x_pct: 30,
  preview_y_pct: 70,
  set_at: 1_761_389_200,
};

const TARGET = {
  frame_x_px: 412.3,
  frame_y_px: 150.1,
  preview_x_pct: 64.5,
  preview_y_pct: 37.6,
  azimuth_deg: 2.34,
  elevation_deg: 1.1,
  set_at: 1_761_389_280,
};

interface Marks {
  ball_mark?: typeof BALL | null;
  target_line?: typeof TARGET | null;
  /** Other capture-status fields of the settings reply (a flat-frame snapshot). */
  frame_flat?: boolean;
  auto_exposure?: object;
}

/** Put marks into the camera store as the server's settings reply would. */
async function setMarks(page: Page, marks: Marks) {
  await page.evaluate(
    async ({ settings }) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings(settings);
    },
    { settings: { ...camera('640x400'), target_line_unavailable_reason: null, ...marks } }
  );
}

/** The footer's Align button per language (the Spanish catalog takes the English range labels). */
const ALIGN_BUTTON = { de: 'Ausrichten', en: 'Align', es: 'Align' } as const;

type Locale = NonNullable<Setup['locale']>;

async function openAlign(page: Page, marks: Marks, locale: Locale = 'de', beforeOpen?: (page: Page) => Promise<void>) {
  await openRange(page, { locale });
  await beforeOpen?.(page);
  await setMarks(page, marks);
  await page.evaluate(async () => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    // Record what the tab would send instead of a socket.
    const { socketService } = await load('services/socketService');
    (window as unknown as { __updates: unknown[] }).__updates = [];
    socketService.setCameraCaptureSettings = (update: unknown) =>
      (window as unknown as { __updates: unknown[] }).__updates.push(update);
  });
  await page.locator('.range-footer nav').getByRole('button', { name: ALIGN_BUTTON[locale] }).click();
  await page.waitForFunction(() => {
    const image = document.querySelector<HTMLImageElement>('.camera-feed__stream img');
    return Boolean(image?.complete && image.naturalWidth > 0);
  });
}

function updates(page: Page) {
  return page.evaluate(() => (window as unknown as { __updates: unknown[] }).__updates);
}

const KIOSK = DEVICES.filter((device) => !device.mobile && device.viewport.width > device.viewport.height);

/** The tap sent for a frame point, within a pixel and a half of the view shown (the tap's rounding). */
async function expectSentAt(page: Page, sent: { x_pct: number; y_pct: number }, xPct: number, yPct: number) {
  const perPixel = await framePctPerPixel(page);
  expect(Math.abs(sent.x_pct - xPct)).toBeLessThanOrEqual(perPixel.x * 1.5);
  expect(Math.abs(sent.y_pct - yPct)).toBeLessThanOrEqual(perPixel.y * 1.5);
}

for (const device of DEVICES) {
  test.describe(device.name, () => {
    test.use({ viewport: device.viewport, isMobile: device.mobile, hasTouch: true });

    test('a tap keeps the step: two ball taps move the ball, the target chip switches, target taps stay', async ({
      page,
    }) => {
      await openAlign(page, {});
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      await expect(alignStep(page, '2 Ziel')).toHaveAttribute('aria-pressed', 'false');
      await tapFramePoint(page, 30, 70);
      await setMarks(page, { ball_mark: BALL });
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      await tapFramePoint(page, 32, 72);
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');

      const balls = (await updates(page)) as { ball_tap: { x_pct: number; y_pct: number } }[];
      expect(balls.map((update) => Object.keys(update))).toEqual([['ball_tap'], ['ball_tap']]);
      await expectSentAt(page, balls[0].ball_tap, 30, 70);
      await expectSentAt(page, balls[1].ball_tap, 32, 72);

      await chooseTargetStep(page);
      await tapFramePoint(page, 64.5, 45);
      await setMarks(page, { ball_mark: BALL, target_line: TARGET });
      await expect(alignStep(page, '2 Ziel')).toHaveAttribute('aria-pressed', 'true');
      await tapFramePoint(page, 60, 50);
      await expect(alignStep(page, '2 Ziel')).toHaveAttribute('aria-pressed', 'true');

      const after = (await updates(page)).slice(2) as { target_tap: { x_pct: number; y_pct: number } }[];
      expect(after.map((update) => Object.keys(update))).toEqual([['target_tap'], ['target_tap']]);
      await expectSentAt(page, after[0].target_tap, 64.5, 45);
      await expectSentAt(page, after[1].target_tap, 60, 50);
    });

    test('a tap on the whole picture sends the same frame point as in the close-up', async ({ page }) => {
      await openAlign(page, {});
      await tapFramePoint(page, 40, 60);
      await showFullPicture(page);
      await tapFramePoint(page, 40, 60);
      const [cropped, full] = (await updates(page)) as { ball_tap: { x_pct: number; y_pct: number } }[];
      await expectSentAt(page, cropped.ball_tap, 40, 60);
      await expectSentAt(page, full.ball_tap, 40, 60);
    });

    test(`the step chips sit ${device.mobile ? 'in the marks section' : 'above the picture'}`, async ({ page }) => {
      await openAlign(page, {});
      const visible = page.locator('.camera-feed__steps:visible');
      await expect(visible).toHaveCount(1);
      const chips = (await visible.boundingBox())!;
      const box = await pictureBox(page);
      if (device.mobile) {
        // Beside (landscape) or under (portrait) the picture: never over it or above it.
        await expect(visible).toHaveClass(/camera-feed__steps--settings/);
        const besidePicture = chips.x >= box.left + box.width - 0.5;
        const underPicture = chips.y >= box.top + box.height - 0.5;
        expect(besidePicture || underPicture).toBe(true);
      } else {
        await expect(visible).toHaveClass(/camera-feed__steps--preview/);
        expect(chips.y + chips.height).toBeLessThanOrEqual(box.top + 0.5);
      }
      // The two chips and the view button.
      const buttons = await visible.getByRole('button').all();
      expect(buttons).toHaveLength(3);
      for (const button of buttons) {
        const size = (await button.boundingBox())!;
        expect(size.height).toBeGreaterThanOrEqual(44);
        expect(size.width).toBeGreaterThanOrEqual(44);
      }
      expect(await clippedText(page, '.camera-feed__steps:visible button')).toEqual([]);
    });

    test('opens on the ball with both marks stored, and a chip chooses the mark a tap moves', async ({ page }) => {
      await openAlign(page, { ball_mark: BALL, target_line: TARGET });
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      await chooseTargetStep(page);
      await alignStep(page, '1 Ball').tap();
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      await tapFramePoint(page, 40, 72);

      const sent = await updates(page);
      expect(sent).toHaveLength(1);
      expect(Object.keys(sent[0] as object)).toEqual(['ball_tap']);
    });

    for (const view of ['close-up', 'whole picture'] as const) {
      test(`the ball, the target and the line between them sit where they were tapped (${view})`, async ({ page }) => {
        // A target inside the close-up's window (y 38-88 % of the frame), so its ring is not clipped.
        const target = { ...TARGET, preview_y_pct: 45 };
        await openAlign(page, { ball_mark: BALL, target_line: target });
        if (view === 'whole picture') await showFullPicture(page);
        const picture = await pictureBox(page);
        const scene = await sceneBox(page);
        if (view === 'close-up') {
          // Twice the frame, the window x 25-75 % and y 38-88 % filling the view.
          expect(Math.abs(scene.width - 2 * picture.width)).toBeLessThanOrEqual(1);
          expect(Math.abs(scene.height - 2 * picture.height)).toBeLessThanOrEqual(1);
          expect(Math.abs(scene.left - (picture.left - 0.5 * picture.width))).toBeLessThanOrEqual(1);
          expect(Math.abs(scene.top - (picture.top - 0.76 * picture.height))).toBeLessThanOrEqual(1);
        } else {
          expect(scene).toEqual(picture);
        }
        const drawn = await page.locator(STREAM).evaluate((stream) => {
          const centre = (selector: string) => {
            const rect = stream.querySelector(selector)!.getBoundingClientRect();
            return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
          };
          const line = stream.querySelector('.camera-feed__target-line-stroke')!.getBoundingClientRect();
          return {
            ball: centre('.camera-feed__mark--ball'),
            target: centre('.camera-feed__mark--target'),
            line: { left: line.left, right: line.right, top: line.top, bottom: line.bottom },
          };
        });
        const ball = await framePointOnScreen(page, BALL.preview_x_pct, BALL.preview_y_pct);
        const aim = await framePointOnScreen(page, target.preview_x_pct, target.preview_y_pct);
        expect(Math.abs(drawn.ball.x - ball.x)).toBeLessThanOrEqual(1);
        expect(Math.abs(drawn.ball.y - ball.y)).toBeLessThanOrEqual(1);
        expect(Math.abs(drawn.target.x - aim.x)).toBeLessThanOrEqual(1);
        expect(Math.abs(drawn.target.y - aim.y)).toBeLessThanOrEqual(1);
        // The dashed line runs from the ball to the target.
        expect(Math.abs(drawn.line.left - ball.x)).toBeLessThanOrEqual(1.5);
        expect(Math.abs(drawn.line.right - aim.x)).toBeLessThanOrEqual(1.5);
        expect(Math.abs(drawn.line.top - aim.y)).toBeLessThanOrEqual(1.5);
        expect(Math.abs(drawn.line.bottom - ball.y)).toBeLessThanOrEqual(1.5);
        // The view clips the scene: nothing of the frame outside the window shows past the picture.
        const clip = await page
          .locator('.camera-feed__view')
          .evaluate((element) => [getComputedStyle(element).overflowX, getComputedStyle(element).overflowY]);
        expect(clip).toEqual(['hidden', 'hidden']);
      });
    }

    test('without a ball the target has no line', async ({ page }) => {
      await openAlign(page, { target_line: TARGET });
      await expect(page.locator('.camera-feed__mark--target')).toHaveCount(1);
      await expect(page.locator('.camera-feed__mark--ball')).toHaveCount(0);
      await expect(page.locator('.camera-feed__target-line')).toHaveCount(0);
    });

    test('the marks head the settings and are set anew one by one', async ({ page }) => {
      await openAlign(page, { ball_mark: BALL, target_line: TARGET });
      const section = page.locator('.camera-settings__section--target');
      await expect(section).toContainText('Ziel 2,3° rechts der Kameraachse');
      await expect(section.locator('.camera-settings__marks')).toContainText('Ballgesetzt');
      await expect(section.locator('.camera-settings__marks')).toContainText('Zielgesetzt');
      const sectionBox = (await section.boundingBox())!;
      const panel = (await page.locator('.camera-settings').boundingBox())!;
      const heading = (await page.locator('.camera-settings__heading').boundingBox())!;
      // First under the panel heading, before anything else in the panel.
      expect(Math.abs(sectionBox.y - (heading.y + heading.height))).toBeLessThanOrEqual(1);
      expect(sectionBox.y).toBeLessThan(panel.y + panel.height);

      // From the target step, so "Ball neu" has to switch back to the ball (Align opens on it).
      await chooseTargetStep(page);
      const ballAnew = section.getByRole('button', { name: 'Ball neu' });
      await ballAnew.scrollIntoViewIfNeeded();
      await ballAnew.tap();
      await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      await section.getByRole('button', { name: 'Ziel neu' }).tap();
      await expect(alignStep(page, '2 Ziel')).toHaveAttribute('aria-pressed', 'true');

      expect(await updates(page)).toEqual([{ ball_tap: null }, { target_tap: null }]);
    });
  });
}

for (const device of KIOSK) {
  for (const theme of ['sun', 'dark'] as const) {
    test.describe(`${device.name} ${theme}`, () => {
      test.use({ viewport: device.viewport, hasTouch: true });

      test('Align opens on the close-up and the ball step; a second ball tap stays on the ball', async ({ page }) => {
        await openAlign(page, { ball_mark: BALL, target_line: TARGET });
        await page.evaluate((value) => {
          document.documentElement.dataset.theme = value;
        }, theme);
        await expect(page.locator(STREAM)).toHaveClass(/camera-feed__stream--cropped/);
        await expect(viewToggle(page)).toHaveText('Ganzes Bild');
        await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
        await expect(page.locator('.camera-settings__section--target .camera-settings__note')).toHaveText(
          'Nochmal tippen verschiebt die Ballmarke. Danach „2 Ziel“ wählen.'
        );

        // The restored ball mark moved: the server's reply carries a new set_at, as at the range.
        await tapFramePoint(page, 31, 71);
        await setMarks(page, {
          ball_mark: { ...BALL, preview_x_pct: 31, preview_y_pct: 71, set_at: BALL.set_at + 5 },
          target_line: TARGET,
        });
        await tapFramePoint(page, 30, 70);
        await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
        expect((await updates(page)).map((update) => Object.keys(update as object))).toEqual([
          ['ball_tap'],
          ['ball_tap'],
        ]);

        // Finger-sized chips and view button (48 px on the kiosk), whole words, above the picture, on screen.
        const controls = alignControls(page);
        const picture = await pictureBox(page);
        for (const button of await controls.getByRole('button').all()) {
          const size = (await button.boundingBox())!;
          expect(size.height, await button.textContent()).toBeGreaterThanOrEqual(48);
          expect(size.width, await button.textContent()).toBeGreaterThanOrEqual(48);
          expect(size.x).toBeGreaterThanOrEqual(0);
          expect(size.x + size.width).toBeLessThanOrEqual(device.viewport.width);
          expect(size.y + size.height).toBeLessThanOrEqual(picture.top + 0.5);
        }
        expect(await clippedText(page, '.camera-feed__steps:visible button')).toEqual([]);
        const column = (await page.locator('.camera-feed__preview-column').boundingBox())!;
        const row = (await controls.boundingBox())!;
        expect(row.x).toBeGreaterThanOrEqual(column.x - 0.5);
        expect(row.x + row.width).toBeLessThanOrEqual(column.x + column.width + 0.5);
        expect(row.y).toBeGreaterThanOrEqual(column.y - 0.5);
        // The picture stays inside its column and above the footer.
        const footer = (await page.locator('.range-footer').boundingBox())!;
        expect(picture.top + picture.height).toBeLessThanOrEqual(footer.y + 0.5);
        expect(picture.left).toBeGreaterThanOrEqual(column.x - 0.5);
        expect(picture.left + picture.width).toBeLessThanOrEqual(column.x + column.width + 0.5);
        expect(await clippedText(page, '.camera-settings__section--target .camera-settings__note')).toEqual([]);
      });

      test('the view button switches to the whole picture and back, and Align opens on the close-up again', async ({
        page,
      }) => {
        await openAlign(page, {});
        await showFullPicture(page);
        expect(await sceneBox(page)).toEqual(await pictureBox(page));
        await viewToggle(page).tap();
        await expect(viewToggle(page)).toHaveText('Ganzes Bild');
        await expect(page.locator(STREAM)).toHaveClass(/camera-feed__stream--cropped/);

        await showFullPicture(page);
        const footer = page.locator('.range-footer nav');
        await footer.getByRole('button', { name: 'Live' }).click();
        await footer.getByRole('button', { name: 'Ausrichten' }).click();
        await expect(page.locator(STREAM)).toHaveClass(/camera-feed__stream--cropped/);
        await expect(viewToggle(page)).toHaveText('Ganzes Bild');
        await expect(alignStep(page, '1 Ball')).toHaveAttribute('aria-pressed', 'true');
      });
    });
  }
}

/** The labels of the Align view's controls per language. */
const CONTROLS = {
  de: { ball: '1 Ball', target: '2 Ziel', full: 'Ganzes Bild', crop: 'Ausschnitt' },
  en: { ball: '1 Ball', target: '2 Target', full: 'Full picture', crop: 'Close-up' },
  es: { ball: '1 Bola', target: '2 Objetivo', full: 'Imagen completa', crop: 'Primer plano' },
} as const;

for (const device of KIOSK) {
  for (const locale of ['de', 'en', 'es'] as const) {
    test.describe(`${device.name} ${locale}`, () => {
      test.use({ viewport: device.viewport, hasTouch: true });

      test('the chips and the view button keep one line and their places, and the picture its size', async ({
        page,
      }) => {
        const labels = CONTROLS[locale];
        await openAlign(page, { ball_mark: BALL, target_line: TARGET }, locale);
        const controls = alignControls(page);
        const layout = async () => {
          const boxes = [];
          for (const button of await controls.getByRole('button').all()) boxes.push((await button.boundingBox())!);
          return { boxes, picture: await pictureBox(page) };
        };
        const expectWholeLabels = async () => {
          expect(await clippedText(page, '.camera-feed__steps:visible button')).toEqual([]);
          expect(await labelsPastPadding(page, '.camera-feed__steps:visible button')).toEqual([]);
        };

        await expect(viewToggle(page)).toHaveText(labels.full);
        await expect(alignStep(page, labels.ball)).toHaveAttribute('aria-pressed', 'true');
        const closeUp = await layout();
        // One line: the chips and the button share their top (a wrapped button took the picture's height).
        expect(closeUp.boxes).toHaveLength(3);
        for (const box of closeUp.boxes) expect(Math.abs(box.y - closeUp.boxes[0].y)).toBeLessThanOrEqual(0.5);
        await expectWholeLabels();

        const toggle = viewToggle(page);
        await toggle.tap();
        await expect(toggle).toHaveText(labels.crop);
        await expect(page.locator(STREAM)).not.toHaveClass(/camera-feed__stream--cropped/);
        // Nothing moved under the finger: same buttons in the same places, the picture as large.
        expect(await layout()).toEqual(closeUp);
        await expectWholeLabels();

        await alignStep(page, labels.target).tap();
        await expect(alignStep(page, labels.target)).toHaveAttribute('aria-pressed', 'true');
        expect(await layout()).toEqual(closeUp);
      });
    });
  }
}

/** Where the targets of 2026-11-02 were, in percent of the 640x400 frame: the zone ahead of the ball. */
const TARGET_ZONE = { x0: 54, y0: 42, x1: 69, y1: 54 };

/** Visible boxes laid over the picture: everything in the preview frame that is not the frame itself. */
function overlayBoxes(page: Page) {
  return page.locator('.camera-feed__preview-frame').evaluate((frame) =>
    Array.from(frame.querySelectorAll<HTMLElement>('*'))
      .filter((element) => !element.closest('.camera-feed__scene'))
      .filter((element) => !element.matches('.camera-feed__stream, .camera-feed__view'))
      .filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) > 0;
      })
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { name: element.className, left: rect.left, top: rect.top, width: rect.width, height: rect.height };
      })
      .filter((box) => box.width > 0 && box.height > 0)
  );
}

for (const device of KIOSK) {
  test.describe(`${device.name} status chip`, () => {
    test.use({ viewport: device.viewport, hasTouch: true });

    test('the close-up leaves the target zone free; the whole picture shows the status chip', async ({ page }) => {
      await openAlign(page, { ball_mark: BALL, target_line: TARGET });
      await expect(page.locator(STREAM)).toHaveClass(/camera-feed__stream--cropped/);
      const zone = await frameAreaOnScreen(page, TARGET_ZONE);
      expect(zone.width * zone.height, 'the target zone lies in the close-up').toBeGreaterThan(0);
      await expect(page.locator('.camera-feed__status')).toHaveCount(0);
      for (const box of await overlayBoxes(page)) {
        const overlapX = Math.min(box.left + box.width, zone.left + zone.width) - Math.max(box.left, zone.left);
        const overlapY = Math.min(box.top + box.height, zone.top + zone.height) - Math.max(box.top, zone.top);
        expect(overlapX > 0.5 && overlapY > 0.5, `${box.name} ${JSON.stringify(box)} on ${JSON.stringify(zone)}`).toBe(
          false
        );
      }
      // The exposure state stays in the header badge.
      await expect(page.locator('.camera-feed__exposure-quality')).toBeVisible();

      await showFullPicture(page);
      const chip = page.locator('.camera-feed__status');
      await expect(chip).toBeVisible();
      await expect(chip).toContainText('Aufnahme bleibt');
      expect(await clippedText(page, '.camera-feed__status')).toEqual([]);

      await viewToggle(page).tap();
      await expect(page.locator(STREAM)).toHaveClass(/camera-feed__stream--cropped/);
      await expect(chip).toHaveCount(0);
    });
  });
}

/** A flat frame as the server reports it: the auto-exposure status, or only the frame_flat flag. */
const FLAT_FRAME_REPORTS = {
  'auto exposure no_image': {
    sample_available: true,
    status: 'flat',
    frame_flat: true,
    auto_exposure: { enabled: true, status: 'no_image', analysis_eligible: false, frame_flat: true, message: '' },
  },
  'frame_flat only': {
    sample_available: true,
    status: 'unavailable',
    frame_flat: true,
    auto_exposure: { enabled: true, status: 'adjusting', analysis_eligible: false, message: '' },
  },
} as const;

for (const device of KIOSK) {
  for (const [report, quality] of Object.entries(FLAT_FRAME_REPORTS)) {
    test.describe(`${device.name} flat frame (${report})`, () => {
      test.use({ viewport: device.viewport, hasTouch: true });

      test('the Align header says the sensor delivers no picture, in the lighting warning', async ({ page }) => {
        await openAlign(page, {}, 'de', (opened) =>
          opened.route('**/api/camera/exposure-quality*', (route) => route.fulfill({ json: quality }))
        );
        const badge = page.locator('.camera-feed__exposure-quality');
        await expect(badge).toContainText('Kein Bild vom Sensor');
        await expect(badge).not.toContainText('Warten');
        // As rendered (innerText applies text-transform), not only as written in the DOM.
        expect(await badge.evaluate((element) => (element as HTMLElement).innerText)).toBe(
          'Belichtung: Kein Bild vom Sensor'
        );
        await expect(badge).toHaveClass(/camera-feed__exposure-quality--no_image/);
        // The red of a lighting_required badge (too dark / too bright), not the amber of "checking".
        const color = await badge.evaluate((element) => getComputedStyle(element).color);
        const danger = await page.evaluate(() => {
          const probe = document.createElement('span');
          probe.className = 'camera-feed__exposure-quality camera-feed__exposure-quality--too_dark';
          document.body.append(probe);
          const value = getComputedStyle(probe).color;
          probe.remove();
          return value;
        });
        expect(color).toBe(danger);
        expect(await clippedText(page, '.camera-feed__exposure-quality')).toEqual([]);
        await expect(page.locator('.camera-settings__auto-status strong')).toHaveText('Kein Bild vom Sensor');
      });
    });
  }
}

for (const device of KIOSK) {
  test.describe(`${device.name} flat frame recovered`, () => {
    test.use({ viewport: device.viewport, hasTouch: true });

    test('a fresh exposure poll that sees a picture clears a flat capture-status snapshot', async ({ page }) => {
      // The capture settings came while the lock was no_image; the backend's retry has since
      // recovered it, which only the exposure-quality poll reports.
      const recovered = {
        sample_available: true,
        status: 'good',
        frame_flat: false,
        auto_exposure: { enabled: true, status: 'ready', analysis_eligible: true, frame_flat: false, message: '' },
      };
      await openAlign(
        page,
        {
          frame_flat: true,
          auto_exposure: { enabled: true, status: 'no_image', analysis_eligible: false, frame_flat: true, message: '' },
        },
        'de',
        (opened) => opened.route('**/api/camera/exposure-quality*', (route) => route.fulfill({ json: recovered }))
      );
      const badge = page.locator('.camera-feed__exposure-quality');
      await expect(badge).toContainText('Bereit');
      await expect(badge).not.toContainText('Kein Bild vom Sensor');
      await expect(badge).not.toHaveClass(/camera-feed__exposure-quality--no_image/);
      await expect(page.locator('.camera-settings__auto-status strong')).toHaveText('Bereit');
    });
  });
}
