import { expect, test, type Page } from '@playwright/test';
import { openRange } from './phoneFixture';

/**
 * An iPhone with viewport-fit=cover: the notch, the rounded corners and the
 * home indicator take safe-area insets, which Chrome emulates through CDP
 * (Emulation.setSafeAreaInsetsOverride). Footer, menu sheet, replay dialog,
 * session history and club picker stay inside the safe rectangle.
 */

interface Insets {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

const CASES: { name: string; viewport: { width: number; height: number }; insets: Insets }[] = [
  // Home Screen web app, portrait: status bar and home indicator.
  {
    name: 'iPhone 15 web app portrait',
    viewport: { width: 393, height: 852 },
    insets: { top: 59, right: 0, bottom: 34, left: 0 },
  },
  {
    name: 'iPhone 15 Safari portrait',
    viewport: { width: 393, height: 812 },
    insets: { top: 0, right: 0, bottom: 34, left: 0 },
  },
  // Landscape: the notch side and the rounded corners, home indicator below.
  {
    name: 'iPhone 15 web app landscape',
    viewport: { width: 852, height: 393 },
    insets: { top: 0, right: 59, bottom: 21, left: 59 },
  },
  {
    name: 'iPhone 15 Safari landscape',
    viewport: { width: 852, height: 343 },
    insets: { top: 0, right: 59, bottom: 21, left: 59 },
  },
];

async function setInsets(page: Page, insets: Insets) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Emulation.setSafeAreaInsetsOverride' as never, { insets } as never);
}

/**
 * Boxes of `selector` (visible ones) that reach past the safe rectangle, 0.5 px rounding allowed.
 * With `scroll`, each one is first scrolled into view inside its scrollers (controls in a scrolling dialog).
 */
function outsideSafeArea(page: Page, selector: string, insets: Insets, scroll = false): Promise<string[]> {
  return page.locator(selector).evaluateAll(
    (elements, { insets, scroll }) =>
      elements.flatMap((element) => {
        if (scroll) element.scrollIntoView({ block: 'nearest', inline: 'nearest' });
        const rect = element.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) return [];
        const label = `${element.className || element.tagName} "${(element.textContent ?? '').trim().slice(0, 20)}"`;
        const problems: string[] = [];
        if (rect.top < insets.top - 0.5) problems.push(`${label}: top ${rect.top.toFixed(1)}`);
        if (rect.left < insets.left - 0.5) problems.push(`${label}: left ${rect.left.toFixed(1)}`);
        if (rect.right > innerWidth - insets.right + 0.5) problems.push(`${label}: right ${rect.right.toFixed(1)}`);
        if (rect.bottom > innerHeight - insets.bottom + 0.5)
          problems.push(`${label}: bottom ${rect.bottom.toFixed(1)}`);
        return problems;
      }),
    { insets, scroll }
  );
}

for (const { name, viewport, insets } of CASES) {
  test.describe(`safe area, ${name} ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test.beforeEach(async ({ page }) => {
      await setInsets(page, insets);
      await openRange(page);
      // env(safe-area-inset-*) reports the emulated insets (viewport-fit=cover in index.html).
      const reported = await page.evaluate(() => {
        const probe = document.createElement('div');
        probe.style.cssText =
          'position:fixed;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)';
        document.body.append(probe);
        const style = getComputedStyle(probe);
        const value = [style.paddingTop, style.paddingRight, style.paddingBottom, style.paddingLeft].map(parseFloat);
        probe.remove();
        return value;
      });
      expect(reported).toEqual([insets.top, insets.right, insets.bottom, insets.left]);
    });

    test('footer buttons and the menu sheet stay inside the safe area', async ({ page }) => {
      expect(await outsideSafeArea(page, '.range-footer nav button', insets)).toEqual([]);
      await page.getByRole('button', { name: 'Menü öffnen' }).click();
      const sheet = page.locator('.menu-sheet');
      await expect(sheet).toBeVisible();
      expect(await outsideSafeArea(page, '.menu-sheet', insets)).toEqual([]);
      // Its first item (Schließen) is on screen without scrolling the sheet.
      expect(await outsideSafeArea(page, '.menu-sheet button >> nth=0', insets)).toEqual([]);
    });

    test('the replay dialog and the session history stay inside the safe area', async ({ page }) => {
      await page.locator('.range-replay-button').click();
      await expect(page.locator('.camera-replay__video')).toBeVisible();
      expect(await outsideSafeArea(page, '.camera-replay__dialog', insets)).toEqual([]);
      expect(await outsideSafeArea(page, '.camera-replay button', insets, true)).toEqual([]);
      await page.getByRole('button', { name: 'Zeitlupe schließen' }).click();
      await expect(page.locator('.camera-replay__dialog')).toBeHidden();

      await page.locator('.range-footer nav').getByRole('button', { name: 'Verlauf' }).click();
      await expect(page.locator('.session-history')).toBeVisible();
      expect(await outsideSafeArea(page, '.session-history', insets)).toEqual([]);
    });

    test('the club picker stays inside the safe area', async ({ page }) => {
      await page.getByRole('button', { name: 'Schläger wechseln' }).first().click();
      await expect(page.locator('.picker-overlay')).toBeVisible();
      expect(await outsideSafeArea(page, '.picker-overlay', insets)).toEqual([]);
    });
  });
}
