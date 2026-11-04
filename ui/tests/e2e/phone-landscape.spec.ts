import { expect, test, type Page } from '@playwright/test';
import {
  chooseMenuItem,
  clippedText,
  crowdedFooter,
  crowdedTileParts,
  overflowingLiveMetricValues,
  WIDEST_LIVE_METRIC_VALUE,
} from './helpers';
import { openRange, SHOT } from './phoneFixture';

/**
 * A phone turned sideways keeps the kiosk layout (phone.css is portrait
 * only); phoneLandscape.css fixes what breaks on 568-932 x 320-430. German,
 * the longest labels, with a 12-shot session.
 */
const LANDSCAPE = [
  { width: 568, height: 320 },
  { width: 667, height: 375 },
  { width: 734, height: 343 },
  { width: 814, height: 380 },
  { width: 852, height: 393 },
  // Home-screen apps (viewport-fit=cover) on the big phones: 400 px high or more, but about 2.16:1.
  { width: 896, height: 414 },
  { width: 932, height: 430 },
] as const;

const SHOTS = Array.from({ length: 12 }, (_, index) => ({
  ...SHOT,
  club: index % 2 ? 'pw' : 'driver',
  carry_spin_rpm: index % 2 ? 10_500 : 2650,
  carry_spin_source: index % 2 ? 'club_typical' : 'measured',
  timestamp: new Date(Date.parse(SHOT.timestamp) + index * 60_000).toISOString(),
}));

async function openMenuItem(page: Page, name: string) {
  await page.getByRole('button', { name: 'Menü öffnen' }).click();
  await chooseMenuItem(page, name);
}

/**
 * Tile labels whose visible text or ≈ mark leaves the tile (into the neighbour)
 * or breaks inside a word. The ≈ mark's screen-reader text is ignored.
 */
function crowdedLabels(page: Page, tiles: string): Promise<string[]> {
  return page.locator(tiles).evaluateAll((cards) =>
    cards.flatMap((card) => {
      const label = card.querySelector('.metric-card__label');
      if (!label) return [];
      const box = card.getBoundingClientRect();
      const name = label.firstChild?.textContent?.trim();
      const rects: DOMRect[] = [];
      const problems: string[] = [];
      for (const node of label.childNodes) {
        if (node.nodeType === Node.TEXT_NODE) {
          const range = document.createRange();
          range.selectNodeContents(node);
          rects.push(...range.getClientRects());
          // One word, one line. A soft hyphen (LivePanel's German compounds) is a proper break: "SCHLÄGER-TEMPO".
          // A word's first and last letter share a line. A letter right after a soft hyphen's
          // break also reports the generated hyphen on the line above: its own line is the lower one.
          const letterTop = (at: number) => {
            const letter = document.createRange();
            letter.setStart(node, at);
            letter.setEnd(node, at + 1);
            return Math.max(...[...letter.getClientRects()].map((rect) => Math.round(rect.top)));
          };
          for (const word of (node.textContent ?? '').matchAll(/[^\s\u00AD]+/g)) {
            if (letterTop(word.index!) !== letterTop(word.index! + word[0].length - 1)) problems.push(`${name}: "${word[0]}" broken`);
          }
        } else if (node instanceof Element) {
          const mark = node.querySelector('svg');
          if (mark) rects.push(mark.getBoundingClientRect());
        }
      }
      for (const rect of rects) {
        if (rect.width === 0) continue;
        if (rect.left < box.left - 0.5 || rect.right > box.right + 0.5) problems.push(`${name}: leaves its tile`);
        if (rect.top < box.top - 0.5 || rect.bottom > box.bottom + 0.5)
          problems.push(`${name}: above or below its tile`);
      }
      return problems;
    })
  );
}

for (const viewport of LANDSCAPE) {
  test.describe(`phone in landscape ${viewport.width}×${viewport.height} (de)`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('footer labels stay inside their buttons', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      expect(await crowdedFooter(page)).toEqual([]);
    });

    test('Live tile labels never run into their numbers', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      expect(await crowdedTileParts(page, '.range-hero-grid .metric-card')).toEqual([]);
      expect(await crowdedTileParts(page, '.range-secondary-grid .metric-card')).toEqual([]);
      // The widest value we format still fits beside its unit.
      await page.locator('.range-tile .metric-card__value').evaluateAll((values, widest) => {
        for (const value of values) value.textContent = widest;
      }, WIDEST_LIVE_METRIC_VALUE);
      await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
      expect(await overflowingLiveMetricValues(page, '.range-tile .metric-card')).toEqual([]);
      expect(await crowdedTileParts(page, '.range-tile .metric-card')).toEqual([]);
    });

    test('All metrics keeps every label beside its neighbours and every number in its tile', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      await page.locator('.range-details-button').click();
      await expect(page.locator('.live-panel__grid .metric-card')).toHaveCount(11);
      expect(await crowdedLabels(page, '.live-panel__grid .metric-card')).toEqual([]);
      expect(await overflowingLiveMetricValues(page)).toEqual([]);
      expect(await crowdedTileParts(page, '.live-panel__grid .metric-card')).toEqual([]);
    });

    // 568×320: three rows of a two-line German title ("Eintreffwinkel ≈") and a number need more
    // height than the panel has, so the tiles scroll inside it (ui/AGENTS.md "Overflow + tap").
    if (viewport.width === 568) {
      test('All metrics scrolls its tiles: each one whole once in view, drag scrolls, tap selects, page still', async ({ page }) => {
        await openRange(page, { shots: SHOTS });
        await page.locator('.range-details-button').click();
        const cards = page.locator('.live-panel__grid .metric-card');
        await expect(cards).toHaveCount(11);
        const scroller = page.locator('.range-details .live-panel__body');
        const size = await scroller.evaluate((node) => ({ scroll: node.scrollHeight, client: node.clientHeight }));
        expect(size.scroll, 'the tiles need more height than the panel').toBeGreaterThan(size.client);

        // Every tile is reachable: scrolled to it, it lies whole inside the scroller.
        const outside = await scroller.evaluate((node) =>
          [...node.querySelectorAll<HTMLElement>('.live-panel__grid .metric-card')].flatMap((card) => {
            node.scrollTop = card.offsetTop - node.clientHeight / 2 + card.offsetHeight / 2;
            const box = node.getBoundingClientRect();
            const rect = card.getBoundingClientRect();
            const inside = rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1;
            return inside ? [] : [`${card.getAttribute('data-metric')}: ${Math.round(rect.top)}-${Math.round(rect.bottom)} of ${Math.round(box.top)}-${Math.round(box.bottom)}`];
          })
        );
        expect(outside, 'tiles out of reach').toEqual([]);
        await scroller.evaluate((node) => (node.scrollTop = 0));

        // A drag on the tiles scrolls them and selects nothing; the page, toolbar and footer stay put.
        const fixed = async () => ({
          toolbar: await page.locator('.range-details-toolbar').boundingBox(),
          footer: await page.locator('.range-footer').boundingBox(),
          pageScroll: await page.evaluate(() => ({ y: window.scrollY, overflow: document.documentElement.scrollHeight - innerHeight })),
        });
        const before = await fixed();
        expect(before.pageScroll.overflow, 'page scroll height').toBeLessThanOrEqual(0);
        const selected = await page.locator('.live-panel__grid .metric-card--selected').getAttribute('data-metric');
        const box = (await scroller.boundingBox())!;
        await page.mouse.move(box.x + box.width / 2, box.y + box.height - 8);
        await page.mouse.down();
        await page.mouse.move(box.x + box.width / 2, box.y + 8, { steps: 12 });
        await page.mouse.up();
        await expect.poll(() => scroller.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
        await expect(page.locator('.live-panel__grid .metric-card--selected')).toHaveAttribute('data-metric', selected!);
        expect(await fixed()).toEqual(before);

        // A tap still selects a single tile (it moves to the top left).
        const target = page.locator('.live-panel__grid .metric-card[data-metric="club_aoa"]');
        await target.scrollIntoViewIfNeeded();
        await target.tap();
        await expect(page.locator('.live-panel__grid .metric-card--selected')).toHaveAttribute('data-metric', 'club_aoa');
        await expect(page.locator('.live-panel__grid .metric-card--selected')).toHaveCount(1);
        expect((await fixed()).pageScroll).toEqual(before.pageScroll);
      });
    }

    test('the shot list header keeps its count and club whole', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      await openMenuItem(page, 'Schläge');
      await expect(page.locator('.panel-header__subtitle')).toHaveText('12 erfasst · 0/12 geprüft');
      expect(
        await clippedText(
          page,
          '.panel-header__subtitle, .panel-header__club, .panel-header__title, .panel-header__actions .panel-action'
        )
      ).toEqual([]);
      // The identity keeps clear of the header buttons.
      const identity = (await page.locator('.panel-header__identity').boundingBox())!;
      const actions = (await page.locator('.panel-header__actions').boundingBox())!;
      expect(identity.x + identity.width).toBeLessThanOrEqual(actions.x + 0.5);
    });

    test('Stats numbers stay in their tiles and leave the footer tappable', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      await openMenuItem(page, 'Statistik');
      await expect(page.locator('.stats-panel__grid')).toBeVisible();
      expect(await crowdedTileParts(page, '.stats-panel__grid .metric-card')).toEqual([]);
      // Scrolled to the end too: nothing paints over the footer.
      await page.locator('.range-app .panel-app__main').evaluate((main) => main.scrollTo(0, main.scrollHeight));
      expect(await crowdedFooter(page)).toEqual([]);
    });

    test('shot list values do not run under the row buttons', async ({ page }) => {
      await openRange(page, { shots: SHOTS });
      await openMenuItem(page, 'Schläge');
      const overlaps = await page.locator('.shots-panel__row').evaluateAll((rows) =>
        rows.flatMap((row) => {
          const actions = row.querySelector('.shots-panel__actions')?.getBoundingClientRect();
          if (!actions) return [];
          return [...row.querySelectorAll('.shots-panel__row-main > *')].flatMap((cell) => {
            const range = document.createRange();
            range.selectNodeContents(cell);
            const text = range.getBoundingClientRect();
            return text.width > 0 && text.right > actions.left + 0.5 ? [`${cell.textContent?.trim()}`] : [];
          });
        })
      );
      expect(overlaps).toEqual([]);
    });

    // a•space's digits are proportional ("4" the widest): ball 244,4 km/h (244.4 mph) and carry ≈244 m (yd).
    if (viewport.width === 568 || viewport.width === 734) {
      for (const units of ['metric', 'imperial'] as const) {
        test(`the widest ball and carry (${units}) stay inside their columns`, async ({ page }) => {
          const [ballMph, carryYards] = units === 'metric' ? [244.4 / 1.609344, 244 / 0.9144] : [244.4, 244];
          const shots = SHOTS.map((shot, index) => ({
            ...shot,
            ball_speed_mph: ballMph,
            carry_spin_adjusted: carryYards,
            estimated_carry_yards: carryYards,
            carry_spin_rpm: 10_500 + index,
            carry_spin_source: 'club_typical',
          }));
          await openRange(page, { shots });
          await page.evaluate(async (units) => {
            const { useProfileStore } = await import(/* @vite-ignore */ '/src/stores/useProfileStore.ts');
            useProfileStore.getState().applySnapshot({
              profiles: [{ id: 'phone', name: 'Golfer', settings: { unit_system: units } }],
              active_profile_id: 'phone',
            });
          }, units);
          await openMenuItem(page, 'Schläge');

          await expect(page.locator('.shots-panel__value').first()).toHaveText('244,4');
          await expect(page.locator('.shots-panel__value--accent').first()).toHaveText('≈244');
          // Inside its own track is clear of the neighbouring column and of the row buttons.
          expect(await clippedText(page, '.shots-panel__row-main > span')).toEqual([]);
          expect(await clippedText(page, '.shots-panel__delete')).toEqual([]);
          const sizes = await page
            .locator('.shots-panel__value')
            .evaluateAll((values) => values.map((value) => parseFloat(getComputedStyle(value).fontSize)));
          expect(Math.min(...sizes)).toBeGreaterThanOrEqual(14.6);
        });
      }
    }
  });
}
