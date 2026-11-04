import { expect, type Locator, type Page } from '@playwright/test';

/**
 * The Align view's picture controls, shared by the specs: the step chips, the view button
 * (close-up / whole picture) and taps on a frame point through whichever view is shown.
 *
 * The picture is the stream box (`.camera-feed__stream`, the view); the whole frame's box is the
 * scene inside it (`.camera-feed__scene`), twice as large in the close-up and shifted so the window
 * fills the view. Frame percent is laid out on the scene, so a frame point's place on screen is
 * read from the scene's box: no window numbers are copied here.
 */

export const STREAM = '.camera-feed__stream';
export const SCENE = '.camera-feed__scene';

/** The visible row of step chips and the view button (above the picture on the kiosk, in the marks on phones). */
export const alignControls = (page: Page): Locator => page.locator('.camera-feed__steps:visible');

/** A step chip by its label ("1 Ball", "2 Ziel"). */
export const alignStep = (page: Page, name: string): Locator =>
  alignControls(page).getByRole('button', { name, exact: true });

/** The button that switches between the close-up and the whole picture. */
export const viewToggle = (page: Page): Locator => alignControls(page).locator('.camera-feed__view-toggle');

/** Tap the target chip ("2 Ziel") and wait until it is the active step. */
export async function chooseTargetStep(page: Page): Promise<void> {
  const chip = alignStep(page, '2 Ziel');
  await chip.tap();
  await expect(chip).toHaveAttribute('aria-pressed', 'true');
}

/** Switch the picture to the whole frame (the view opens on the close-up). */
export async function showFullPicture(page: Page): Promise<void> {
  const toggle = viewToggle(page);
  await expect(toggle).toHaveText('Ganzes Bild');
  await toggle.tap();
  await expect(toggle).toHaveText('Ausschnitt');
  await expect(page.locator(STREAM)).not.toHaveClass(/camera-feed__stream--cropped/);
  // On a portrait phone the button sits in the marks section under the picture, and tapping it
  // scrolls the picture away: bring it back for what is measured next.
  await page.locator(STREAM).scrollIntoViewIfNeeded();
}

export interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

const boxOf = (page: Page, selector: string): Promise<Box> =>
  page.locator(selector).evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
  });

/** The picture's box on screen (the view). */
export const pictureBox = (page: Page): Promise<Box> => boxOf(page, STREAM);

/** The whole frame's box on screen: the view itself in the whole picture, twice as large in the close-up. */
export const sceneBox = (page: Page): Promise<Box> => boxOf(page, SCENE);

/** Where a frame point (percent of the frame) lies on screen in the view shown. */
export async function framePointOnScreen(page: Page, xPct: number, yPct: number): Promise<{ x: number; y: number }> {
  const scene = await sceneBox(page);
  return { x: scene.left + (xPct / 100) * scene.width, y: scene.top + (yPct / 100) * scene.height };
}

/** Where a frame area (percent of the frame) lies on screen in the view shown, clipped to the view. */
export async function frameAreaOnScreen(
  page: Page,
  area: { x0: number; y0: number; x1: number; y1: number }
): Promise<Box> {
  const topLeft = await framePointOnScreen(page, area.x0, area.y0);
  const bottomRight = await framePointOnScreen(page, area.x1, area.y1);
  const view = await pictureBox(page);
  const left = Math.max(topLeft.x, view.left);
  const top = Math.max(topLeft.y, view.top);
  const right = Math.min(bottomRight.x, view.left + view.width);
  const bottom = Math.min(bottomRight.y, view.top + view.height);
  return { left, top, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
}

/** Tap a frame point (percent of the frame) through the view shown; it must lie in the window. */
export async function tapFramePoint(page: Page, xPct: number, yPct: number): Promise<void> {
  const point = await framePointOnScreen(page, xPct, yPct);
  const view = await pictureBox(page);
  const x = point.x - view.left;
  const y = point.y - view.top;
  expect(
    x >= 0 && x <= view.width && y >= 0 && y <= view.height,
    `frame ${xPct} %, ${yPct} % is outside the view`
  ).toBe(true);
  await page.locator(STREAM).tap({ position: { x, y } });
}

/** How far one screen pixel in the view shown is in frame percent, across and down (a tap's rounding). */
export async function framePctPerPixel(page: Page): Promise<{ x: number; y: number }> {
  const scene = await sceneBox(page);
  return { x: 100 / scene.width, y: 100 / scene.height };
}
