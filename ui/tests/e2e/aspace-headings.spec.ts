import { expect, test, type Page } from '@playwright/test';
import {
  clippedText,
  gotoApp,
  hitAShot,
  KIOSK_VIEWPORTS,
  resetSession,
  setClub,
  simulateShot,
  waitForEvent,
  withControlSocket,
} from './helpers';
import { openRange, SHOT } from './phoneFixture';

/*
 * Headings and summary numbers set in a•space, the wide display face: a shot heading never breaks between
 * "Schlag" and its number (a no-break space joins them; "Ansicht:" before them may take a line of its own),
 * and a summary number keeps its unit on its line, inside its card, at the widest values.
 */

const PHONES = [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
  { width: 390, height: 844 },
  { width: 568, height: 320 },
] as const;

/** Line boxes the text of `selector` takes, each element on its own. */
function lineCounts(page: Page, selector: string): Promise<{ text: string; lines: number }[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements.map((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const tops = new Set(
        [...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top))
      );
      return { text: element.textContent ?? '', lines: tops.size };
    })
  );
}

/** Elements under `selector` whose text runs past the content box of `box` (their closest such ancestor). */
function pastBox(page: Page, selector: string, box: string): Promise<string[]> {
  return page.locator(selector).evaluateAll(
    (elements, box) =>
      elements.flatMap((element) => {
        const container = element.closest(box)!;
        const style = getComputedStyle(container);
        const outer = container.getBoundingClientRect();
        const left = outer.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
        const right = outer.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
        const range = document.createRange();
        range.selectNodeContents(element);
        const ink = range.getBoundingClientRect();
        return ink.left < left - 1 || ink.right > right + 1
          ? [`${element.textContent}: ${(left - ink.left).toFixed(1)} / ${(ink.right - right).toFixed(1)} px`]
          : [];
      }),
    box
  );
}

async function expectOneLine(page: Page, selector: string, label: string) {
  const counts = await lineCounts(page, selector);
  expect(counts.length, `${label}: ${selector}`).toBeGreaterThan(0);
  for (const { text, lines } of counts) expect(lines, `${label}: "${text}" on one line`).toBe(1);
}

/** The line (its top, rounded) each visible character of the first element under `selector` sits on. */
function characterLines(page: Page, selector: string): Promise<{ char: string; top: number }[]> {
  return page.locator(selector).first().evaluate((element) => {
    const out: { char: string; top: number }[] = [];
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.textContent ?? '';
      for (let index = 0; index < text.length; index += 1) {
        const range = document.createRange();
        range.setStart(node, index);
        range.setEnd(node, index + 1);
        const rect = range.getBoundingClientRect();
        if (rect.width > 0) out.push({ char: text[index], top: Math.round(rect.top) });
      }
    }
    return out;
  });
}

/** "Schlag" and its number on one line; returns the lines before it, as text. */
async function expectShotOnOneLine(page: Page, selector: string, label: string): Promise<string[]> {
  const chars = await characterLines(page, selector);
  const text = chars.map(({ char }) => char).join('');
  const start = text.indexOf('Schlag');
  expect(start, `${label}: "Schlag" in "${text}"`).toBeGreaterThanOrEqual(0);
  const shot = chars.slice(start);
  expect(new Set(shot.map(({ top }) => top)).size, `${label}: "${text}" breaks inside "Schlag N"`).toBe(1);
  const before = chars.slice(0, start).filter(({ top }) => top !== shot[0].top);
  const lines = [...new Set(before.map(({ top }) => top))];
  return lines.map((top) => before.filter((char) => char.top === top).map(({ char }) => char).join('').trim());
}

/** Give the heading a three-digit shot number, keeping the app's own text and spacing. */
const shotNumber124 = (page: Page, selector: string) =>
  page.locator(selector).evaluate((element) => {
    element.textContent = (element.textContent ?? '').replace(/\d+$/, '124');
  });

test.use({ hasTouch: true });

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await resetSession(socket);
    await setClub(socket, 'pw');
  });
});

test.afterAll(async () => {
  await withControlSocket((socket) => resetSession(socket));
});

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`the kiosk at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('"Schlag 124" over the Live tiles stays on one line', async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await gotoApp(page);
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
      await hitAShot(page);
      const heading = page.locator('.kiosk-live__header h1');
      await expect(heading).toHaveText('Schlag 1');
      await shotNumber124(page, '.kiosk-live__header h1');
      await expectOneLine(page, '.kiosk-live__header h1', 'kiosk Live');
      expect(await expectShotOnOneLine(page, '.kiosk-live__header h1', 'kiosk Live')).toEqual([]);
      expect(await clippedText(page, '.kiosk-live__header h1')).toEqual([]);
    });
  });
}

for (const viewport of PHONES) {
  test.describe(`a phone at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: true });

    test('"Schlag 124" over Live stays on one line', async ({ page }) => {
      const shots = Array.from({ length: 124 }, (_, index) => ({
        ...SHOT,
        camera_replay: null,
        timestamp: new Date(Date.parse(SHOT.timestamp) + index * 60_000).toISOString(),
      }));
      await openRange(page, { shots });
      await expect(page.locator('.range-shot-heading h1')).toHaveText('Schlag 124');
      await expectOneLine(page, '.range-shot-heading h1', 'phone Live');
      expect(await expectShotOnOneLine(page, '.range-shot-heading h1', 'phone Live')).toEqual([]);
      expect(await clippedText(page, '.range-shot-heading h1')).toEqual([]);
    });
  });
}

for (const viewport of [...PHONES, ...KIOSK_VIEWPORTS]) {
  test.describe(`the companion page at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('the shot heading keeps "Schlag 124" together at its size, beside the title', async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await gotoApp(page, '/companion');
      for (let shot = 0; shot < 2; shot += 1)
        await withControlSocket(async (socket) => {
          const arrived = waitForEvent(socket, 'shot');
          await simulateShot(socket);
          await arrived;
        });
      const heading = page.locator('.companion__shot-heading strong');
      await expect(heading).toHaveText('Schlag 2');
      await page.evaluate(() => document.fonts.ready);

      // The latest shot, then an earlier one ("Ansicht: Schlag N"), each with a three-digit number.
      for (const [select, text] of [
        [null, 'Schlag 124'],
        ['Schlag 1', 'Ansicht: Schlag 124'],
      ] as const) {
        if (select) {
          await page.locator('.companion__shot-row').filter({ hasText: '#1' }).click();
          await expect(heading).toHaveText(`Ansicht: ${select}`);
        }
        await shotNumber124(page, '.companion__shot-heading strong');
        await expect(heading).toHaveText(text);
        const before = await expectShotOnOneLine(page, '.companion__shot-heading strong', text);
        if (text === 'Schlag 124') expect(before, text).toEqual([]);
        // On the narrowest phone "Ansicht:" takes a line of its own above "Schlag 124".
        else if (viewport.width === 320) expect(before, text).toEqual(['Ansicht:']);
        // The heading keeps its size (1.25rem) rather than shrinking to fit one line.
        await expect(heading).toHaveCSS('font-size', '20px');
        expect(await pastBox(page, '.companion__shot-heading strong, .companion__header h1', '.companion__header'), text).toEqual([]);
        // Heading and title stay apart.
        const apart = await page.evaluate(() => {
          const ink = (selector: string) => {
            const range = document.createRange();
            range.selectNodeContents(document.querySelector(selector)!);
            return range.getBoundingClientRect();
          };
          return ink('.companion__shot-heading strong').left - ink('.companion__header h1').right;
        });
        expect(apart, `${text}: gap to the title`).toBeGreaterThanOrEqual(4);
      }
    });
  });
}

for (const viewport of [
  ...PHONES,
  { width: 751, height: 900 },
  ...KIOSK_VIEWPORTS,
]) {
  test.describe(`the analysis summary at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('each number keeps its unit on its line, inside its card, at the widest values', async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await page.goto('/?view=analysis');
      const numbers = page.locator('.analysis-kpis strong');
      await expect(numbers).toHaveCount(4, { timeout: 15_000 });
      await page.evaluate(() => document.fonts.ready);
      // Shots, mean carry, ball speed, spread; km/h is the widest speed unit, yds the widest distance unit.
      for (const widest of [
        ['1.244', '244,4 yds', '244,4 km/h', '44,4 yds'],
        ['1.244', '244,4 m', '244,4 mph', '44,4 m'],
      ]) {
        await numbers.evaluateAll(
          (elements, widest) => elements.forEach((element, index) => (element.textContent = widest[index])),
          widest
        );
        await expectOneLine(page, '.analysis-kpis strong', 'summary');
        expect(await pastBox(page, '.analysis-kpis strong', '.analysis-card')).toEqual([]);
      }
      // The narrowest phones: one column, so the numbers keep at least their 375 px size (17.3 px).
      if (viewport.width <= 359) {
        const lefts = await page
          .locator('.analysis-kpis > .analysis-card')
          .evaluateAll((cards) => cards.map((card) => Math.round(card.getBoundingClientRect().left)));
        expect(new Set(lefts).size, 'one column').toBe(1);
        const sizes = await numbers.evaluateAll((elements) =>
          elements.map((element) => parseFloat(getComputedStyle(element).fontSize))
        );
        for (const size of sizes) expect(size).toBeGreaterThanOrEqual(17.3);
      }
    });
  });
}
