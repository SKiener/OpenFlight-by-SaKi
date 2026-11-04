import { expect, test, type Page } from '@playwright/test';
import { showFullPicture } from './alignView';
import { clippedText, mockSessionHistory } from './helpers';
import { camera, openRange, REPLAY, routeApi, SHOT } from './phoneFixture';

/**
 * Every camera view shows the WHOLE sensor frame (contain, never crop) on
 * phones in portrait and landscape and on the kiosk, and every overlay drawn
 * on the picture maps to the picture's content box, not to its container.
 * The Align view opens on a close-up for tapping; its button shows the whole
 * frame, and the overlays map to the picture in both views.
 *
 * The frames are test cards with a coloured band on each edge, 640 x 400
 * (full mode) and 640 x 200 (strip mode). Geometry is measured from the
 * element box plus its computed object-fit, then clipped by the element itself,
 * by every ancestor that hides overflow and by the screen.
 */

interface Device {
  name: string;
  viewport: { width: number; height: number };
  mobile: boolean;
}

const PORTRAIT: Device[] = [
  { name: 'iPhone SE Safari', viewport: { width: 320, height: 460 }, mobile: true },
  { name: 'iPhone SE', viewport: { width: 320, height: 568 }, mobile: true },
  { name: 'iPhone SE 3 Safari', viewport: { width: 375, height: 548 }, mobile: true },
  { name: 'iPhone 15', viewport: { width: 393, height: 659 }, mobile: true },
  { name: 'iPhone 15 Pro Max', viewport: { width: 430, height: 739 }, mobile: true },
  { name: 'iPhone 15 full screen', viewport: { width: 393, height: 852 }, mobile: true },
];
const LANDSCAPE: Device[] = [
  { name: 'iPhone SE', viewport: { width: 568, height: 320 }, mobile: true },
  { name: 'iPhone SE 3', viewport: { width: 667, height: 375 }, mobile: true },
  { name: 'iPhone 15', viewport: { width: 734, height: 343 }, mobile: true },
  { name: 'iPhone 15 Pro Max', viewport: { width: 814, height: 380 }, mobile: true },
  { name: 'iPhone 15 full screen', viewport: { width: 852, height: 393 }, mobile: true },
  // Home-screen apps (viewport-fit=cover) on the big phones: 400 px high or more, but about 2.16:1.
  { name: 'iPhone 11 full screen', viewport: { width: 896, height: 414 }, mobile: true },
  { name: 'iPhone 15 Pro Max full screen', viewport: { width: 932, height: 430 }, mobile: true },
];
const KIOSK: Device[] = [
  { name: 'kiosk', viewport: { width: 1024, height: 600 }, mobile: false },
  { name: 'kiosk', viewport: { width: 800, height: 480 }, mobile: false },
  { name: 'kiosk', viewport: { width: 800, height: 400 }, mobile: false },
];
const ALL = [...PORTRAIT, ...LANDSCAPE, ...KIOSK];
/**
 * The least Align picture height per landscape screen height. A slimmer header and margins gave
 * 124, 147, 163, 186 and 197 px; the header and margins left 92, 122, 154, 158 and 167 px before.
 * The big phones as home-screen apps (414 and 430 high) had 185 and 180 px, now 217 and 214.
 */
const ALIGN_MIN_HEIGHT: Record<number, number> = {
  320: 120,
  343: 142,
  375: 158,
  380: 180,
  393: 190,
  414: 210,
  430: 205,
};
/**
 * The least strip-mode (640 x 200) Align picture height per landscape screen height: at least what
 * it had before the chip went beside the picture (88, 119, 109 and 153 px); the side strip had cut
 * it to 63, 95, 82 and 126 px.
 */
const ALIGN_STRIP_MIN_HEIGHT: Record<number, number> = { 320: 88, 343: 119, 375: 109, 393: 153 };

async function waitForVideo(page: Page, selector: string) {
  await page.waitForFunction(
    (sel) => {
      const video = document.querySelector(sel) as HTMLVideoElement | null;
      return !!video && video.videoWidth > 0 && video.readyState >= 1;
    },
    selector,
    { timeout: 15_000 }
  );
  await page.evaluate((sel) => (document.querySelector(sel) as HTMLVideoElement).pause(), selector);
  // Let the viewport take the frame's shape from the metadata.
  await page.waitForTimeout(100);
}

async function waitForImage(page: Page, selector: string) {
  await page.waitForFunction(
    (sel) => {
      const img = document.querySelector(sel) as HTMLImageElement | null;
      return !!img && img.complete && img.naturalWidth > 0;
    },
    selector,
    { timeout: 15_000 }
  );
  await page.waitForTimeout(100);
}

interface FrameGeometry {
  /** The picture inside the element box, for the element's computed object-fit. */
  content: { left: number; top: number; right: number; bottom: number; width: number; height: number };
  /** The part of the screen that shows the element: ancestors that hide overflow, and the window. */
  visible: { left: number; top: number; right: number; bottom: number };
  /** Share of the picture's width and height on screen, 0..1. */
  shownX: number;
  shownY: number;
}

/** Where the whole picture of a video or img lies, and how much of it is on screen. */
function frameGeometry(page: Page, selector: string): Promise<FrameGeometry> {
  return page.locator(selector).evaluate((element: HTMLVideoElement | HTMLImageElement) => {
    const natural =
      element instanceof HTMLVideoElement
        ? { width: element.videoWidth, height: element.videoHeight }
        : { width: element.naturalWidth, height: element.naturalHeight };
    const box = element.getBoundingClientRect();
    // The picture box for the element's real object-fit (centred, the default object-position).
    const fit = getComputedStyle(element).objectFit;
    const contain = Math.min(box.width / natural.width, box.height / natural.height);
    const scaleX =
      fit === 'fill'
        ? box.width / natural.width
        : fit === 'cover'
          ? Math.max(box.width / natural.width, box.height / natural.height)
          : fit === 'none'
            ? 1
            : fit === 'scale-down'
              ? Math.min(1, contain)
              : contain;
    const scaleY = fit === 'fill' ? box.height / natural.height : scaleX;
    const width = natural.width * scaleX;
    const height = natural.height * scaleY;
    const left = box.left + (box.width - width) / 2;
    const top = box.top + (box.height - height) / 2;
    const content = { left, top, right: left + width, bottom: top + height, width, height };
    // The element clips its own picture (cover, none), then its ancestors and the window do.
    const visible = {
      left: Math.max(0, box.left),
      top: Math.max(0, box.top),
      right: Math.min(window.innerWidth, box.right),
      bottom: Math.min(window.innerHeight, box.bottom),
    };
    for (let node = element.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.overflowX === 'visible' && style.overflowY === 'visible') continue;
      const rect = node.getBoundingClientRect();
      const borderTop = parseFloat(style.borderTopWidth);
      const borderLeft = parseFloat(style.borderLeftWidth);
      visible.left = Math.max(visible.left, rect.left + borderLeft);
      visible.top = Math.max(visible.top, rect.top + borderTop);
      visible.right = Math.min(visible.right, rect.left + borderLeft + node.clientWidth);
      visible.bottom = Math.min(visible.bottom, rect.top + borderTop + node.clientHeight);
    }
    const shown = (from: number, to: number, min: number, max: number) =>
      Math.max(0, Math.min(to, max) - Math.max(from, min)) / (to - from);
    return {
      content,
      visible,
      shownX: shown(content.left, content.right, visible.left, visible.right),
      shownY: shown(content.top, content.bottom, visible.top, visible.bottom),
    };
  });
}

/** The whole frame is on screen (0.5 px rounding allowed) and not tiny. */
function expectWholeFrame(geometry: FrameGeometry, minHeight: number) {
  const { content } = geometry;
  expect(geometry.shownX, JSON.stringify(geometry)).toBeGreaterThanOrEqual(1 - 0.5 / content.width);
  expect(geometry.shownY, JSON.stringify(geometry)).toBeGreaterThanOrEqual(1 - 0.5 / content.height);
  expect(content.height, JSON.stringify(geometry)).toBeGreaterThanOrEqual(minHeight);
}

/** Buttons and inputs under `selector` that are off screen or covered at their centre. */
function unreachableControls(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements.flatMap((element) => {
      element.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const rect = element.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const hit = document.elementFromPoint(x, y);
      const label = (element.getAttribute('aria-label') ?? element.textContent ?? '').trim();
      if (rect.width === 0 || rect.height === 0) return [`${label}: no size`];
      if (rect.left < -0.5 || rect.top < -0.5 || rect.right > innerWidth + 0.5 || rect.bottom > innerHeight + 0.5)
        return [`${label}: off screen`];
      if (!hit || !(hit === element || element.contains(hit))) return [`${label}: covered by ${hit?.className}`];
      return [];
    })
  );
}

/** Text that runs out of its own box sideways (the replay time, the button labels). */
function overflowingText(page: Page, selector: string): Promise<string[]> {
  return page
    .locator(selector)
    .evaluateAll((elements) =>
      elements.flatMap((element) =>
        element.scrollWidth > element.clientWidth + 1
          ? [`${element.textContent?.trim()} ${element.scrollWidth}>${element.clientWidth}`]
          : []
      )
    );
}

const sizeLabel = (device: Device) =>
  `${device.name} ${device.viewport.width}×${device.viewport.height}${
    device.viewport.width > device.viewport.height ? ' landscape' : ' portrait'
  }`;

for (const device of ALL) {
  test.describe(`camera views, ${sizeLabel(device)}`, () => {
    test.use({ viewport: device.viewport, isMobile: device.mobile, hasTouch: true });

    for (const frame of ['640x400', '640x200'] as const) {
      test(`shot replay shows the whole ${frame} frame and every control`, async ({ page }) => {
        await openRange(page, { frame });
        await page.locator('.range-replay-button').click();
        await waitForVideo(page, '.camera-replay__video');

        const geometry = await frameGeometry(page, '.camera-replay__video');
        // Strip clips are wide and flat; a full frame should get real height.
        expectWholeFrame(geometry, frame === '640x400' ? 90 : 45);
        // The video box is the picture box: contain letterboxes by at most a pixel.
        const videoBox = (await page.locator('.camera-replay__video').boundingBox())!;
        expect(Math.abs(videoBox.width - geometry.content.width)).toBeLessThanOrEqual(1.5);
        expect(Math.abs(videoBox.height - geometry.content.height)).toBeLessThanOrEqual(1.5);

        expect(await overflowingText(page, '.camera-replay__controls, .camera-replay__button')).toEqual([]);
        expect(await unreachableControls(page, '.camera-replay button, .camera-replay input')).toEqual([]);
      });
    }

    test('the Align picture shows the whole frame and the cross sits on the ball target', async ({ page }) => {
      await openRange(page);
      await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
      await waitForImage(page, '.camera-feed__stream img');
      await expectAlignOverlayOnPicture(page);
    });

    if (device.mobile) {
      for (const frame of ['640x400', '640x200'] as const) {
        test(`the Align chip stays off the ${frame} picture and the settings, its words whole`, async ({ page }) => {
          await openRange(page, { frame });
          await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
          await waitForImage(page, '.camera-feed__stream img');
          await showFullPicture(page);
          const { content } = await frameGeometry(page, '.camera-feed__stream img');
          const chip = (await page.locator('.camera-feed__status').boundingBox())!;
          const overlapX = Math.min(chip.x + chip.width, content.right) - Math.max(chip.x, content.left);
          const overlapY = Math.min(chip.y + chip.height, content.bottom) - Math.max(chip.y, content.top);
          expect(overlapX > 0.5 && overlapY > 0.5, `chip ${JSON.stringify(chip)} on ${JSON.stringify(content)}`).toBe(
            false
          );
          // Beside or below the picture, inside the preview column, never over the settings.
          const column = (await page.locator('.camera-feed__preview-column').boundingBox())!;
          expect(chip.x).toBeGreaterThanOrEqual(column.x - 0.5);
          expect(chip.x + chip.width).toBeLessThanOrEqual(column.x + column.width + 0.5);
          expect(chip.y).toBeGreaterThanOrEqual(column.y - 0.5);
          expect(chip.y + chip.height).toBeLessThanOrEqual(column.y + column.height + 0.5);
          const settings = (await page.locator('.camera-settings').boundingBox())!;
          const besideSettings = chip.x + chip.width <= settings.x + 0.5;
          const aboveSettings = chip.y + chip.height <= settings.y + 0.5;
          expect(besideSettings || aboveSettings, `chip ${JSON.stringify(chip)} on ${JSON.stringify(settings)}`).toBe(
            true
          );
          expect(await clippedText(page, '.camera-feed__status')).toEqual([]);
          // A strip frame is limited by the column's width: the chip goes under it, never beside it.
          if (frame === '640x200') {
            expect(chip.y).toBeGreaterThanOrEqual(content.bottom - 0.5);
            expect(content.height).toBeGreaterThanOrEqual(ALIGN_STRIP_MIN_HEIGHT[device.viewport.height] ?? 0);
          }
        });
      }
    }

    if (device.mobile && device.viewport.width > device.viewport.height) {
      test('the Align heading clears the exposure chip and the picture is tall', async ({ page }) => {
        await openRange(page);
        await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
        await waitForImage(page, '.camera-feed__stream img');
        await showFullPicture(page);
        const { content } = await frameGeometry(page, '.camera-feed__stream img');
        expect(await clippedText(page, '.camera-feed__header span, .camera-feed__title')).toEqual([]);
        // "HOCHGESCHWINDIGKEITSAUFNAHME" and the title end before the exposure chip starts.
        const heading = (await page.locator('.camera-feed__header > div:first-child').boundingBox())!;
        const status = (await page.locator('.camera-feed__header-status').boundingBox())!;
        expect(heading.x + heading.width).toBeLessThanOrEqual(status.x + 0.5);
        const eyebrowEnd = await page.locator('.camera-feed__eyebrow').evaluate((eyebrow) => {
          const range = document.createRange();
          range.selectNodeContents(eyebrow);
          return range.getBoundingClientRect().right;
        });
        expect(eyebrowEnd).toBeLessThanOrEqual(status.x - 4);
        // The picture takes the height the screen leaves it (the iPhone SE had a 92 px picture).
        expect(content.height).toBeGreaterThanOrEqual(ALIGN_MIN_HEIGHT[device.viewport.height] ?? 0);
      });
    }

    test('the strip-mode Align picture keeps the cross on the ball target', async ({ page }) => {
      await openRange(page, { frame: '640x200' });
      await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
      await waitForImage(page, '.camera-feed__stream img');
      await expectAlignOverlayOnPicture(page);
    });

    // Just after a mode switch the preview frame and the capture settings disagree; the picture follows the frame.
    for (const [frame, settings] of [
      ['640x200', '640x400'],
      ['640x400', '640x200'],
    ] as const) {
      test(`the Align picture keeps the cross on the ball target with a ${frame} frame and ${settings} settings`, async ({
        page,
      }) => {
        await openRange(page, { frame, settings });
        await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
        await waitForImage(page, '.camera-feed__stream img');
        await expectAlignOverlayOnPicture(page);
      });
    }

    test('/display shows the whole camera frame', async ({ page }) => {
      await routeApi(page, {});
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await page.goto('/display');
      await page.locator('.display-mode').waitFor();
      await page.evaluate(async (settings) => {
        const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
        (await load('services/socketService')).socketService.disconnect();
        (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings(settings);
      }, camera('640x400'));
      await waitForImage(page, '.display-mode__camera-image');
      await page.locator('.display-mode__camera-image').evaluate((img) => img.scrollIntoView({ block: 'start' }));
      expectWholeFrame(await frameGeometry(page, '.display-mode__camera-image'), 100);
      // The status chips sit under the picture, not over the hitting zone.
      if (device.viewport.width <= 1100) {
        const picture = (await page.locator('.display-mode__camera-image').boundingBox())!;
        const chips = (await page.locator('.display-mode__status-row').boundingBox())!;
        expect(chips.y).toBeGreaterThanOrEqual(picture.y + picture.height - 0.5);
      }
    });
  });
}

/**
 * The overlays map to the picture in the close-up (the frame twice as large, clipped by the view),
 * then, in the whole picture, the img fills its stream box exactly and the whole frame is on screen.
 */
async function expectAlignOverlayOnPicture(page: Page) {
  await expectAlignOverlayOnFrame(page);
  const view = (await page.locator('.camera-feed__stream').boundingBox())!;
  const closeUp = await frameGeometry(page, '.camera-feed__stream img');
  // Half the frame across and down shows, filling the picture.
  expect(closeUp.shownX).toBeCloseTo(0.5, 2);
  expect(closeUp.shownY).toBeCloseTo(0.5, 2);
  expect(Math.abs(closeUp.content.width - 2 * view.width)).toBeLessThanOrEqual(1);
  await showFullPicture(page);
  expectWholeFrame(await expectAlignOverlayOnFrame(page), 40);
}

/** The hairlines' box is the picture's content box and the cross sits on the ball guide, in either view. */
async function expectAlignOverlayOnFrame(page: Page): Promise<FrameGeometry> {
  const geometry = await frameGeometry(page, '.camera-feed__stream img');
  const overlay = await page.locator('.camera-feed__stream').evaluate((stream) => {
    const svg = stream.querySelector('.camera-feed__crosshair')!.getBoundingClientRect();
    const cross = stream.querySelector('.camera-feed__center-cross')!.getBoundingClientRect();
    return {
      svg: { left: svg.left, top: svg.top, width: svg.width, height: svg.height },
      cross: { x: cross.left + cross.width / 2, y: cross.top + cross.height / 2 },
    };
  });
  const { content } = geometry;
  expect(Math.abs(overlay.svg.left - content.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(overlay.svg.top - content.top)).toBeLessThanOrEqual(1);
  expect(Math.abs(overlay.svg.width - content.width)).toBeLessThanOrEqual(1);
  expect(Math.abs(overlay.svg.height - content.height)).toBeLessThanOrEqual(1);
  expect(Math.abs(overlay.cross.x - (content.left + 0.5 * content.width))).toBeLessThanOrEqual(1);
  expect(Math.abs(overlay.cross.y - (content.top + 0.78 * content.height))).toBeLessThanOrEqual(1);
  return geometry;
}

test.describe('camera views, automatic and history replay', () => {
  for (const device of [PORTRAIT[0], PORTRAIT[1], PORTRAIT[3], LANDSCAPE[0], LANDSCAPE[2], KIOSK[0]]) {
    test.describe(sizeLabel(device), () => {
      test.use({ viewport: device.viewport, isMobile: device.mobile, hasTouch: true });

      test('the automatic replay after a shot shows the whole frame and every control', async ({ page }) => {
        await openRange(page);
        await page.evaluate(async (shot) => {
          const { useShotStore } = await import(/* @vite-ignore */ '/src/stores/useShotStore.ts');
          useShotStore.getState().addShot({ ...shot, timestamp: '2026-10-07T09:13:00Z' });
        }, SHOT);
        await waitForVideo(page, '.camera-replay__video');
        await expect(page.locator('.camera-replay__playback-row [role="status"]')).toBeVisible();
        expectWholeFrame(await frameGeometry(page, '.camera-replay__video'), 100);
        expect(await overflowingText(page, '.camera-replay__controls, .camera-replay__button')).toEqual([]);
        expect(await unreachableControls(page, '.camera-replay button, .camera-replay input')).toEqual([]);
      });

      test('a saved shot replay from the session history shows the whole frame', async ({ page }) => {
        await openRange(page);
        await page.locator('.range-footer nav').getByRole('button', { name: 'Verlauf' }).click();
        await page.locator('.session-history__session').first().click();
        // Every header button can be reached, and the sticky table head leaves the replay button free.
        expect(await unreachableControls(page, '.session-history__actions .panel-action')).toEqual([]);
        expect(await unreachableControls(page, '.session-history__replay')).toEqual([]);
        await page.locator('.session-history__replay').first().click();
        await waitForVideo(page, '.camera-replay__video');
        expectWholeFrame(await frameGeometry(page, '.camera-replay__video'), 100);
      });
    });
  }
});

/** Rate buttons whose boxes overlap the next one or leave the dialog. */
function crowdedRates(page: Page): Promise<string[]> {
  return page.locator('.camera-replay__dialog').evaluate((dialog) => {
    const bounds = dialog.getBoundingClientRect();
    const rates = [...dialog.querySelectorAll('.camera-replay__rate')].map((rate) => ({
      label: rate.textContent?.trim(),
      box: rate.getBoundingClientRect(),
    }));
    const problems: string[] = [];
    rates.forEach(({ label, box }, index) => {
      if (box.left < bounds.left - 0.5 || box.right > bounds.right + 0.5) problems.push(`${label} outside the dialog`);
      for (const other of rates.slice(index + 1)) {
        const overlapX = Math.min(box.right, other.box.right) - Math.max(box.left, other.box.left);
        const overlapY = Math.min(box.bottom, other.box.bottom) - Math.max(box.top, other.box.top);
        if (overlapX > 0.5 && overlapY > 0.5)
          problems.push(`${label} overlaps ${other.label} by ${overlapX.toFixed(1)}`);
      }
    });
    return problems;
  });
}

test.describe('camera views, replay from the analysis page', () => {
  // A real replay: 60 fps playback of a 300 fps capture shows all six rates.
  const replay = {
    ...REPLAY,
    frame_count: 600,
    trigger_frame: 240,
    playback_fps: 60,
    capture_fps: 300,
    duration_seconds: 10,
  };
  for (const device of [PORTRAIT[0], PORTRAIT[1], PORTRAIT[2], PORTRAIT[3], LANDSCAPE[0]]) {
    test.describe(sizeLabel(device), () => {
      test.use({ viewport: device.viewport, isMobile: device.mobile, hasTouch: true });

      test('shows the whole frame, every control and six separate rate buttons', async ({ page }) => {
        await routeApi(page, {});
        await page.route('**/api/camera/replays/*/prepare', (route) =>
          route.fulfill({ json: { ...replay, video_url: `/api/camera/replays/${replay.id}/video` } })
        );
        await mockSessionHistory(page, [
          {
            summary: { id: 's1', started_at: '2026-10-04T10:00:00Z', shot_count: 1, clubs: ['driver'] },
            shots: [{ ...SHOT, shot_number: 1, camera_replay: replay }],
          },
        ]);
        await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
        await page.goto('/?view=analysis');
        await expect(page.getByTestId('shot-count')).toHaveText('1');
        await page.getByRole('button', { name: 'Schlagdetails #1', exact: true }).click();
        await page
          .getByRole('region', { name: 'Schlagdetails' })
          .getByRole('button', { name: /Zeitlupe/ })
          .click();
        await waitForVideo(page, '.camera-replay__video');

        await expect(page.locator('.camera-replay__rate')).toHaveCount(6);
        expectWholeFrame(await frameGeometry(page, '.camera-replay__video'), 90);
        expect(await crowdedRates(page)).toEqual([]);
        expect(await overflowingText(page, '.camera-replay__controls, .camera-replay__button')).toEqual([]);
        expect(await unreachableControls(page, '.camera-replay button, .camera-replay input')).toEqual([]);
      });
    });
  }
});

test.describe('companion replay', () => {
  for (const device of [...PORTRAIT, ...LANDSCAPE]) {
    test.describe(sizeLabel(device), () => {
      test.use({ viewport: device.viewport, isMobile: device.mobile, hasTouch: true });

      for (const frame of ['640x400', '640x200'] as const) {
        test(`fits the whole ${frame} frame on screen at once`, async ({ page }) => {
          await page.goto(`/tests/e2e/fixtures/camera-frame.html?size=${frame}`);
          await waitForVideo(page, '.companion-replay__video');
          await page.locator('.companion-replay__video').evaluate((video) => video.scrollIntoView({ block: 'center' }));
          expectWholeFrame(await frameGeometry(page, '.companion-replay__video'), frame === '640x400' ? 100 : 50);
        });
      }
    });
  }
});
