import { expect, test } from '@playwright/test';
import { gotoApp } from './helpers';

async function mockMediaPlayback(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    interface MediaState {
      paused: boolean;
      currentTime: number;
      playbackRate: number;
    }

    const mediaStates = new WeakMap<HTMLMediaElement, MediaState>();
    const stateFor = (media: HTMLMediaElement) => {
      let state = mediaStates.get(media);
      if (!state) {
        state = { paused: true, currentTime: 0, playbackRate: 1 };
        mediaStates.set(media, state);
      }
      return state;
    };

    Object.defineProperties(HTMLMediaElement.prototype, {
      paused: {
        configurable: true,
        get() {
          return stateFor(this as HTMLMediaElement).paused;
        },
      },
      currentTime: {
        configurable: true,
        get() {
          return stateFor(this as HTMLMediaElement).currentTime;
        },
        set(value: number) {
          stateFor(this as HTMLMediaElement).currentTime = Number(value);
          this.dispatchEvent(new Event('timeupdate'));
        },
      },
      playbackRate: {
        configurable: true,
        get() {
          return stateFor(this as HTMLMediaElement).playbackRate;
        },
        set(value: number) {
          stateFor(this as HTMLMediaElement).playbackRate = Number(value);
        },
      },
    });

    HTMLMediaElement.prototype.play = function play() {
      stateFor(this).paused = false;
      this.dispatchEvent(new Event('play'));
      return Promise.resolve();
    };
    HTMLMediaElement.prototype.pause = function pause() {
      stateFor(this).paused = true;
      this.dispatchEvent(new Event('pause'));
    };
  });
}

test('exercises replay controls without covering the video at a kiosk viewport', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  await mockMediaPlayback(page);
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html');

  const dialog = page.getByRole('dialog', { name: 'Shot replay' });
  const video = dialog.getByLabel('Strike replay');
  const viewport = dialog.locator('.camera-replay__viewport');
  const controls = dialog.locator('.camera-replay__controls');

  await expect(dialog.getByRole('button', { name: 'Play', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Play', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();

  await dialog.getByLabel('Scrub replay').fill('0.8');
  await expect(dialog.locator('.camera-replay__time')).toContainText('0:00.8 / 0:01.6');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBe(0.8);

  await dialog.getByRole('button', { name: '0.25×', exact: true }).click();
  await expect(dialog.getByRole('button', { name: '0.25×', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.playbackRate)).toBe(0.25);

  await dialog.getByRole('button', { name: 'Loop', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Loop', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.loop)).toBe(false);

  await dialog.getByRole('button', { name: 'Restart replay', exact: true }).click();
  await expect
    .poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime))
    .toBeCloseTo(73 / 60 - 0.375);

  await dialog.getByRole('button', { name: '0.1×', exact: true }).click();
  await expect
    .poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime))
    .toBeCloseTo(73 / 60 - 0.15);
  await video.dispatchEvent('ended');
  await expect
    .poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime))
    .toBeCloseTo(73 / 60 - 0.15);

  await dialog.getByLabel('Scrub replay').fill('0');
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBe(0);
  await dialog.getByRole('button', { name: '1×', exact: true }).click();
  await dialog.getByRole('button', { name: 'Restart replay', exact: true }).click();
  await expect.poll(() => video.evaluate((element: HTMLVideoElement) => element.currentTime)).toBe(0);
  await expect(dialog.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
  await dialog.getByRole('button', { name: 'Pause', exact: true }).click();
  await expect(dialog.getByRole('button', { name: 'Play', exact: true })).toBeVisible();

  const viewportBox = await viewport.boundingBox();
  const controlsBox = await controls.boundingBox();
  expect(viewportBox).not.toBeNull();
  expect(controlsBox).not.toBeNull();
  expect(viewportBox!.x + viewportBox!.width).toBeLessThanOrEqual(controlsBox!.x);
  await expect(video).toHaveCSS('transform', 'matrix(-1, 0, 0, 1, 0, 0)');
});

for (const fps of [288, 576]) {
  test(`selects extra slow motion with native browser rates at ${fps} capture fps`, async ({ page }) => {
    await gotoApp(page, `/tests/e2e/fixtures/camera-replay.html?fps=${fps}`);
    const video = page.getByLabel('Strike replay');
    const controls = page.locator('.camera-replay__controls');
    for (const speed of [0.05, 0.025]) {
      const button = page.getByRole('button', { name: `${speed}×`, exact: true });
      await button.tap();
      await expect(button).toHaveAttribute('aria-pressed', 'true');
      await expect
        .poll(() => video.evaluate((element: HTMLVideoElement) => element.playbackRate))
        .toBeCloseTo((speed * fps) / 60);
      const box = await button.boundingBox();
      expect(box!.height).toBeGreaterThanOrEqual(44);
      expect(box!.y + box!.height).toBeLessThanOrEqual(600);
    }
    expect(await controls.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeLessThanOrEqual(1);
    await expect(page.getByRole('button', { name: 'Restart replay' })).toBeInViewport();
    await expect(page.getByRole('button', { name: 'Close replay' })).toBeInViewport();
  });
}

test('does not mirror a capture already flipped during persistence', async ({ page }) => {
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html?mirror=false');

  await expect(page.getByLabel('Strike replay')).toHaveCSS('transform', 'none');
});

test('retries a failed preparation from the player', async ({ page }) => {
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html?mode=error');

  await expect(page.getByText('Fixture preparation failed')).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByLabel('Strike replay')).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => (window as typeof window & { __replayRetryCount?: number }).__replayRetryCount))
    .toBe(1);
});

test('aborts an older preparation and ignores its stale response', async ({ page }) => {
  let releaseReplayA: (() => void) | undefined;
  const replayARelease = new Promise<void>((resolve) => {
    releaseReplayA = resolve;
  });

  await page.route('**/api/camera/replays/*/prepare', async (route) => {
    const replayId = decodeURIComponent(new URL(route.request().url()).pathname.split('/').at(-2) ?? '');
    if (replayId === 'replay-a') {
      await replayARelease;
    }
    await route
      .fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          id: replayId,
          frame_count: 99,
          trigger_frame: 73,
          playback_fps: 60,
          duration_seconds: 1.65,
          display_mirror_horizontal: true,
          video_url: `/videos/${replayId}.mp4`,
        }),
      })
      .catch(() => undefined);
  });

  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html?mode=controller');
  await page.getByRole('button', { name: 'Open replay A' }).click();
  await expect(page.getByText('Preparing replay')).toBeVisible();

  await page.getByRole('button', { name: 'Close replay' }).click();
  await page.getByRole('button', { name: 'Open replay B' }).click();
  const video = page.getByLabel('Strike replay');
  await expect(video).toHaveAttribute('src', /replay-b\.mp4$/);

  releaseReplayA?.();
  await page.waitForTimeout(50);
  await expect(video).toHaveAttribute('src', /replay-b\.mp4$/);
});

// iOS (Low Power Mode, thermal limits) refuses even muted autoplay; the autoplay attribute
// then fails silently and play() rejects with NotAllowedError until the user taps. The test
// sets __userTapped before its tap (Playwright's own input keeps userActivation active).
async function mockRefusedAutoplay(page: import('@playwright/test').Page, refuse: boolean) {
  await page.addInitScript((refuseAutoplay: boolean) => {
    const state = new WeakMap<HTMLMediaElement, boolean>();
    const calls = { total: 0, refused: 0 };
    (window as typeof window & { __playCalls?: typeof calls }).__playCalls = calls;
    Object.defineProperty(HTMLMediaElement.prototype, 'paused', {
      configurable: true,
      get() {
        return !state.get(this as HTMLMediaElement);
      },
    });
    HTMLMediaElement.prototype.play = function play() {
      calls.total += 1;
      if (refuseAutoplay && !(window as typeof window & { __userTapped?: boolean }).__userTapped) {
        calls.refused += 1;
        return Promise.reject(new DOMException('The request is not allowed', 'NotAllowedError'));
      }
      state.set(this, true);
      this.dispatchEvent(new Event('play'));
      return Promise.resolve();
    };
    HTMLMediaElement.prototype.pause = function pause() {
      state.set(this, false);
      this.dispatchEvent(new Event('pause'));
    };
  }, refuse);
}

for (const automatic of [false, true]) {
  test(`offers a play control over the frame when the phone refuses ${automatic ? 'automatic' : 'manual'} autoplay`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.clock.install();
    await mockRefusedAutoplay(page, true);
    await gotoApp(page, `/tests/e2e/fixtures/camera-replay.html${automatic ? '?automatic=1' : ''}`);
    const dialog = page.getByRole('dialog', { name: 'Shot replay' });
    const video = dialog.getByLabel('Strike replay');
    const prompt = dialog.locator('.camera-replay__play-prompt');

    await expect(prompt).toHaveCount(0);
    await video.dispatchEvent('loadedmetadata');
    await expect(prompt.getByRole('button', { name: 'Play', exact: true })).toBeVisible();
    // Refused autoplay is not a playback failure.
    await expect(page.getByText('Could not play replay')).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as typeof window & { __playCalls?: { refused: number } }).__playCalls?.refused)
    ).toBe(1);

    const viewportBox = await dialog.locator('.camera-replay__viewport').boundingBox();
    const promptBox = await prompt.boundingBox();
    // The prompt covers the frame (within the viewport's border).
    expect(promptBox!.x).toBeGreaterThanOrEqual(viewportBox!.x);
    expect(promptBox!.y).toBeGreaterThanOrEqual(viewportBox!.y);
    expect(promptBox!.x + promptBox!.width).toBeLessThanOrEqual(viewportBox!.x + viewportBox!.width);
    expect(promptBox!.y + promptBox!.height).toBeLessThanOrEqual(viewportBox!.y + viewportBox!.height);
    expect(promptBox!.width).toBeGreaterThanOrEqual(viewportBox!.width - 2);
    expect(promptBox!.height).toBeGreaterThanOrEqual(viewportBox!.height - 2);

    await page.evaluate(() => {
      (window as typeof window & { __userTapped?: boolean }).__userTapped = true;
    });
    await prompt.getByRole('button', { name: 'Play', exact: true }).click();
    await expect(dialog.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
    await expect(prompt).toHaveCount(0);
    // A tap cancels the automatic return to Live; manual replays never close by themselves.
    await page.clock.runFor(20_000);
    expect(await replayCloseCount(page)).toBe(0);
  });
}

function replayCloseCount(page: import('@playwright/test').Page) {
  return page.evaluate(() => (window as typeof window & { __replayCloseCount?: number }).__replayCloseCount ?? 0);
}

test('returns a refused automatic replay to Live after 8 s without a tap', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  await mockRefusedAutoplay(page, true);
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html?automatic=1');
  const dialog = page.getByRole('dialog', { name: 'Shot replay' });

  await dialog.getByLabel('Strike replay').dispatchEvent('loadedmetadata');
  await expect(dialog.locator('.camera-replay__play-prompt').getByRole('button', { name: 'Play' })).toBeVisible();
  await page.clock.runFor(7_900);
  expect(await replayCloseCount(page)).toBe(0);
  await page.clock.runFor(200);
  await expect.poll(() => replayCloseCount(page)).toBe(1);
});

test('keeps a refused manual replay open until it is closed', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.clock.install();
  await mockRefusedAutoplay(page, true);
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html');
  const dialog = page.getByRole('dialog', { name: 'Shot replay' });

  await dialog.getByLabel('Strike replay').dispatchEvent('loadedmetadata');
  await expect(dialog.locator('.camera-replay__play-prompt').getByRole('button', { name: 'Play' })).toBeVisible();
  await page.clock.runFor(60_000);
  expect(await replayCloseCount(page)).toBe(0);
  await expect(dialog.locator('.camera-replay__play-prompt')).toBeVisible();
});

test('starts the replay by itself where autoplay is allowed, as on the kiosk', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 600 });
  await mockRefusedAutoplay(page, false);
  await gotoApp(page, '/tests/e2e/fixtures/camera-replay.html');
  const dialog = page.getByRole('dialog', { name: 'Shot replay' });

  await dialog.getByLabel('Strike replay').dispatchEvent('loadedmetadata');
  await expect(dialog.getByRole('button', { name: 'Pause', exact: true })).toBeVisible();
  await expect(dialog.locator('.camera-replay__play-prompt')).toHaveCount(0);
  expect(
    await page.evaluate(() => (window as typeof window & { __playCalls?: { total: number } }).__playCalls?.total)
  ).toBe(1);
});
