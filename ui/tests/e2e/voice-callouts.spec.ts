import { test, type Page } from '@playwright/test';
import {
  expect,
  gotoApp,
  menuBack,
  openMenuArea,
  resetSession,
  resetUnits,
  setClub,
  simulateShot,
  withControlSocket,
} from './helpers';

interface SpokenUtterance {
  text: string;
  lang: string;
  volume: number;
}

interface ShotPayload {
  shot: { ball_speed_mph: number; estimated_carry_yards: number; carry_spin_adjusted: number | null };
}

declare global {
  interface Window {
    __spoken: SpokenUtterance[];
    __speechCancels: number;
  }
}

const VOICE_SETTINGS_KEY = 'openflight.voice-callouts:v1';

/**
 * Replace the browser's voice with a recorder so the test can read what was
 * said. Like WebKit dropping a call outside a gesture, it never confirms the
 * silent unlock utterance, so every tap primes again.
 */
async function installFakeSpeech(page: Page) {
  await page.addInitScript(() => {
    const spoken: SpokenUtterance[] = [];
    window.__spoken = spoken;
    window.__speechCancels = 0;

    class FakeUtterance {
      text: string;
      lang = '';
      volume = 1;
      onstart: (() => void) | null = null;
      onend: (() => void) | null = null;

      constructor(text: string) {
        this.text = text;
      }
    }

    const synth = {
      speaking: false,
      pending: false,
      paused: false,
      speak(utterance: FakeUtterance) {
        spoken.push({ text: utterance.text, lang: utterance.lang, volume: utterance.volume });
      },
      cancel() {
        window.__speechCancels += 1;
      },
      resume() {},
      getVoices() {
        return [];
      },
    };
    Object.defineProperty(window, 'speechSynthesis', { value: synth, configurable: true });
    Object.defineProperty(window, 'SpeechSynthesisUtterance', { value: FakeUtterance, configurable: true });
  });
}

/** Everything said out loud (the silent unlock utterance is left out). */
async function spokenAloud(page: Page): Promise<SpokenUtterance[]> {
  return page.evaluate(() => window.__spoken.filter((utterance) => utterance.volume > 0));
}

async function silentPrimes(page: Page): Promise<number> {
  return page.evaluate(() => window.__spoken.filter((utterance) => utterance.volume === 0).length);
}

async function dismissPicker(page: Page) {
  await page.getByRole('button', { name: 'Close Select club' }).click();
}

async function openMenu(page: Page) {
  await page.getByRole('button', { name: 'Open menu' }).click();
}

/** The sheet's own close button; on a 1280 px window the sheet covers the scrim's centre. */
async function closeMenu(page: Page) {
  await page.locator('.menu-sheet').getByRole('button', { name: 'Close', exact: true }).click();
}

/** Units are saved for the profile on the Pi, speed and distance each on their own (menu area Display). */
async function chooseMetricUnits(page: Page) {
  await menuBack(page);
  await openMenuArea(page, 'Display');
  for (const name of ['km/h', 'Meters']) {
    const option = page.getByRole('button', { name, exact: true });
    await option.click();
    await expect(option).toHaveAttribute('aria-pressed', 'true');
  }
}

async function turnOnVoice(page: Page, language: string) {
  await openMenu(page);
  await openMenuArea(page, 'Callouts');
  await page.getByRole('group', { name: 'Voice callouts' }).getByRole('button', { name: 'On' }).click();
  await page.getByRole('combobox', { name: 'Voice language' }).selectOption(language);
}

async function simulateShotPayload(): Promise<ShotPayload> {
  return withControlSocket((socket) => simulateShot(socket) as Promise<ShotPayload>);
}

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await resetSession(socket);
    await resetUnits(socket);
    await setClub(socket, 'driver');
  });
});

test.afterAll(async () => {
  await withControlSocket((socket) => resetUnits(socket));
});

test('announces a shot in German and metric units once callouts are on', async ({ page }) => {
  await installFakeSpeech(page);
  await gotoApp(page);
  await dismissPicker(page);

  await turnOnVoice(page, 'de');
  await chooseMetricUnits(page);
  await closeMenu(page);

  // Turning callouts on is a tap, so it also unlocked speech (iOS needs that).
  expect(await silentPrimes(page)).toBeGreaterThan(0);

  const { shot } = await simulateShotPayload();
  const carryMeters = Math.round((shot.carry_spin_adjusted ?? shot.estimated_carry_yards) * 0.9144);
  const ballKmh = Math.round(shot.ball_speed_mph * 1.60934);

  await expect
    .poll(() => spokenAloud(page))
    .toEqual([{ text: `Carry ${carryMeters} Meter, Ballgeschwindigkeit ${ballKmh}`, lang: 'de-DE', volume: 1 }]);
});

test('stays silent while callouts are off', async ({ page }) => {
  await installFakeSpeech(page);
  await gotoApp(page);
  await dismissPicker(page);

  await withControlSocket(async (socket) => {
    await simulateShot(socket);
  });
  await openMenu(page);
  await openMenuArea(page, 'Analysis');
  await page.locator('.range-tools').getByRole('button', { name: 'Shots', exact: true }).click();
  await expect(page.locator('.shots-panel__row')).toHaveCount(1);

  expect(await spokenAloud(page)).toEqual([]);
  expect(await silentPrimes(page)).toBe(0);
});

test('after a reload with callouts on, the first tap unlocks speech again', async ({ page }) => {
  await installFakeSpeech(page);
  await page.addInitScript(
    ([key, value]) => window.localStorage.setItem(key, value),
    [VOICE_SETTINGS_KEY, JSON.stringify({ enabled: true, metrics: ['carry'], language: 'en' })]
  );
  await gotoApp(page);
  await expect(page.getByRole('dialog', { name: 'Select club' })).toBeVisible();
  expect(await silentPrimes(page)).toBe(0);

  await dismissPicker(page);
  expect(await silentPrimes(page)).toBeGreaterThan(0);

  const { shot } = await simulateShotPayload();
  const carryYards = Math.round(shot.carry_spin_adjusted ?? shot.estimated_carry_yards);
  await expect.poll(() => spokenAloud(page)).toEqual([{ text: `Carry ${carryYards} yards`, lang: 'en-US', volume: 1 }]);
});

test('speaks a sample with the test button, keeps the choice after a reload, and Off stops speech', async ({
  page,
}) => {
  await installFakeSpeech(page);
  await gotoApp(page);
  await dismissPicker(page);

  await turnOnVoice(page, 'en');
  await page.getByRole('button', { name: 'Spin', exact: true }).click();
  await page.getByRole('button', { name: 'Test voice' }).click();

  await expect
    .poll(() => spokenAloud(page))
    .toEqual([{ text: 'Carry 165 yards, Ball speed 120, Spin 6200', lang: 'en-US', volume: 1 }]);

  await page.reload();
  await dismissPicker(page);
  await openMenu(page);
  await openMenuArea(page, 'Callouts');
  const voice = page.getByRole('group', { name: 'Voice callouts' });
  await expect(voice.getByRole('button', { name: 'On' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: 'Spin', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('combobox', { name: 'Voice language' })).toHaveValue('en');

  const cancelsBefore = await page.evaluate(() => window.__speechCancels);
  await voice.getByRole('button', { name: 'Off' }).click();
  expect(await page.evaluate(() => window.__speechCancels)).toBeGreaterThan(cancelsBefore);
  await expect(page.getByRole('button', { name: 'Test voice' })).toHaveCount(0);
});

test('clears queued speech when the page is hidden and when it comes back', async ({ page }) => {
  await installFakeSpeech(page);
  await gotoApp(page);
  await dismissPicker(page);

  const cancels = () => page.evaluate(() => window.__speechCancels);
  const setVisibility = (state: 'hidden' | 'visible') =>
    page.evaluate((value) => {
      Object.defineProperty(document, 'visibilityState', { value, configurable: true });
      document.dispatchEvent(new Event('visibilitychange'));
    }, state);

  const before = await cancels();
  await setVisibility('hidden');
  expect(await cancels()).toBe(before + 1);
  await setVisibility('visible');
  expect(await cancels()).toBe(before + 2);
});

test('scrolls the taller menu by dragging without pressing the button under the finger', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 400 });
  await installFakeSpeech(page);
  await gotoApp(page);
  await dismissPicker(page);
  await turnOnVoice(page, 'de');

  const sheet = page.getByRole('dialog', { name: 'Menu' });
  await sheet.evaluate((node) => {
    node.scrollTop = 0;
  });
  // The Callouts page with its value chips outgrows the sheet at 800x400.
  const smashChip = page.getByRole('button', { name: 'Smash', exact: true });
  await expect(smashChip).toHaveAttribute('aria-pressed', 'false');
  const box = await smashChip.boundingBox();
  expect(box).toBeTruthy();

  await page.mouse.move(box!.x + box!.width / 2, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.move(box!.x + box!.width / 2, box!.y - 100, { steps: 12 });
  await page.mouse.up();

  await expect.poll(() => sheet.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
  await expect(smashChip).toHaveAttribute('aria-pressed', 'false');
  expect(await page.evaluate(() => window.getSelection()?.toString() ?? '')).toBe('');
});

test.describe('voice callouts on a touchscreen', () => {
  test.use({ hasTouch: true, viewport: { width: 1024, height: 600 } });

  test('taps a value chip to add it', async ({ page }) => {
    await installFakeSpeech(page);
    await gotoApp(page);
    await dismissPicker(page);
    await turnOnVoice(page, 'de');

    const smash = page.getByRole('button', { name: 'Smash', exact: true });
    await expect(smash).toHaveAttribute('aria-pressed', 'false');
    await smash.tap();
    await expect(smash).toHaveAttribute('aria-pressed', 'true');
  });
});
