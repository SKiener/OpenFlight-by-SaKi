import { test, expect, type Page } from '@playwright/test';
import {
  chooseMenuItem,
  clippedText,
  crowdedTileParts,
  KIOSK_VIEWPORTS,
  labelsPastPadding,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  PHONE_VIEWPORTS,
} from './helpers';
import { UNMEASURABLE_COPY, unmeasurableClubPageCopy } from '../../src/test-utils/shot';
import { phoneOrigin } from './phoneFixture';

/*
 * Calculated club data ("berechnet", plan T9 / A7): the "All metrics" club page,
 * the statistics club page and the shot list's club-data line in the 8 production
 * viewports and three landscape phones × de/en/es × 3 cases (iron worst case,
 * driver, pending). Nothing may leave its tile or the screen, "berechnet" stays
 * readable (caption or legend) wherever short tiles drop their captions, and only
 * club speed, path, attack angle, the impact point (a face and its zone word) and
 * the low point (club page only) are shown: the server still sends the lofts, the
 * face, face to path, the spin axis and the impact in mm, which this hardware
 * cannot measure, and no tile, caption or line may show them.
 */

const VIEWPORTS = [
  ...KIOSK_VIEWPORTS.map((viewport) => ({ viewport, phone: false })),
  { viewport: { width: 720, height: 1280 }, phone: false },
  ...PHONE_VIEWPORTS.map((viewport) => ({ viewport, phone: true })),
  // Phones on their side (phoneLandscape.css): the iPhone SE and the 15 / 15 Pro Max rotated.
  { viewport: { width: 568, height: 320 }, phone: true },
  { viewport: { width: 659, height: 393 }, phone: true },
  // 1.72 : 1 and 430 high: the app treats it like a kiosk (phoneLandscape.css stops at 399 px or 2.1 : 1).
  { viewport: { width: 739, height: 430 }, phone: false },
];
const LOCALES = ['de', 'en', 'es'] as const;
type Locale = (typeof LOCALES)[number];
type Case = 'iron' | 'driver' | 'pending';

const CLUB_CARDS = '.club-data__grid .metric-card';
const BALL_CARDS = '.live-panel__grid:not(.club-data__grid) .metric-card';
const STATS_CARDS = '.stats-panel__grid--club .metric-card';
const STATS_BALL_CARDS = '.stats-panel__grid:not(.stats-panel__grid--club) .metric-card';
const STATS_SWITCH = '.stats-panel__chips > .segmented-control';
const CLUB_IDS = ['club_speed', 'cd_path', 'cd_aoa', 'cd_impact', 'cd_low_point'];
const CALCULATED_IDS = ['cd_path', 'cd_aoa', 'cd_impact', 'cd_low_point'];
/** The low point card per case (the iron's 12.3 cm ahead of the ball; the driver's is withheld). */
const LOW_POINT: Record<Case, Record<Locale, { value: string; side?: string }>> = {
  iron: {
    de: { value: '12,3', side: 'vor dem Ball · ±2,1 cm · berechnet · unkalibriert' },
    en: { value: '12.3', side: 'ahead of the ball · ±2.1 cm · calculated · uncalibrated' },
    es: { value: '12,3', side: 'delante de la bola · ±2,1 cm · calculado · sin calibrar' },
  },
  driver: { de: { value: '—' }, en: { value: '—' }, es: { value: '—' } },
  pending: { de: { value: '—' }, en: { value: '—' }, es: { value: '—' } },
};
/** The newest shot's zone word on the club page, per case and locale ("uncertain": the longest). */
const IMPACT_WORDS: Record<Case, Record<Locale, string>> = {
  iron: { de: 'Spitze', en: 'Toe', es: 'Punta' },
  driver: { de: 'unsicher', en: 'uncertain', es: 'incierto' },
  pending: { de: '—', en: '—', es: '—' },
};
const STATS_IDS = ['stats_cd_path', 'stats_cd_aoa'];

const LABELS: Record<Locale, { menu: string; stats: string; shots: string; club: string; ball: string }> = {
  de: { menu: 'Menü öffnen', stats: 'Statistik', shots: 'Schläge', club: 'Schläger', ball: 'Ball' },
  en: { menu: 'Open menu', stats: 'Stats', shots: 'Shots', club: 'Club', ball: 'Ball' },
  es: { menu: 'Abrir menú', stats: 'Estadísticas', shots: 'Golpes', club: 'Palo', ball: 'Bola' },
};

const BASE = {
  ball_speed_mph: 103.4,
  club_speed_mph: 125.5,
  smash_factor: 1.33,
  estimated_carry_yards: 135,
  carry_range: [125, 145],
  peak_magnitude: 100,
  launch_angle_vertical: 26.2,
  launch_angle_horizontal: -1.2,
  launch_angle_confidence: 0.95,
  angle_source: 'radar',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 10000,
  spin_confidence: 0.59,
  spin_quality: 'experimental',
  spin_source: null,
  carry_spin_adjusted: 133,
  carry_spin_rpm: 10000,
  carry_spin_source: 'club_typical',
  apex_yards: 30.6,
  profile_id: 'club-test',
  profile_name: 'Golfer',
};

/**
 * The widest values the gates let through: |path| near 20°, a steep attack; and
 * everything the server sends but no screen shows (lofts near 70°, the face, the
 * classes, the low point, a scattering impact).
 */
function ironShot(i: number) {
  return {
    ...BASE,
    club: '8-iron',
    club_data_status: 'done',
    camera_club_path_deg: -16.9 + (i % 2) * 0.4,
    camera_club_path_sigma_deg: 1.9,
    camera_attack_angle_deg: -14.9 + (i % 2) * 0.2,
    camera_attack_angle_sigma_deg: 1.5,
    camera_low_point_cm: 12.3,
    camera_low_point_sigma_cm: 2.1,
    dynamic_loft_deg: 69.9,
    dynamic_loft_sigma_deg: 1.4,
    spin_loft_deg: 69.9,
    spin_loft_sigma_deg: 1.5,
    face_to_target_deg: -12.2 - (i % 2) * 0.4,
    face_to_target_sigma_deg: 1.5,
    face_to_path_deg: -19.1 + (i % 3),
    face_to_path_sigma_deg: 2.9,
    face_to_path_class: 'closed',
    calculated_spin_axis_deg: -1.2 + (i % 2),
    calculated_spin_axis_sigma_deg: 2.9,
    spin_axis_class: 'straight',
    spin_axis_source: 'calculated',
    // Alternating ±30 mm: the pattern "scatters", the longest strike word.
    impact_toe_mm: i % 2 ? 30 : -30,
    impact_sigma_mm: 6,
    // The impact point the club page shows: past the toe (clamped onto the face) on the newest shot.
    impact_offset_mm: i % 2 ? -30 : 30,
    impact_zone: i % 2 ? 'heel' : 'toe',
    impact_method: 'D',
  };
}

function driverShot(i: number) {
  return {
    ...BASE,
    club: 'driver',
    club_data_status: 'done',
    camera_club_path_deg: -2.1 + (i % 2) * 0.3,
    camera_club_path_sigma_deg: 1.9,
    face_to_path_deg: 4.4,
    face_to_path_sigma_deg: 2.0,
    face_to_path_class: 'open',
    impact_toe_mm: -9.7 + i,
    impact_sigma_mm: 6,
    // A teed driver: offset and height, but the sigma leaves the zone open (a ring, "uncertain").
    impact_offset_mm: -9.7 + i,
    impact_height_mm: 30,
    impact_height_sigma_mm: 5,
    impact_zone: null,
    impact_method: 'E',
  };
}

function session(kind: Case | 'four-clubs') {
  const stamp = (i: number) => `2026-10-25T12:${String(i).padStart(2, '0')}:00Z`;
  if (kind === 'driver') return Array.from({ length: 5 }, (_, i) => ({ ...driverShot(i), timestamp: stamp(i) }));
  if (kind === 'four-clubs') {
    const clubs = ['driver', '7-wood', '8-iron', 'wedge-44'];
    return Array.from({ length: 8 }, (_, i) => ({ ...ironShot(i), club: clubs[i % 4], timestamp: stamp(i) }));
  }
  const irons = Array.from({ length: 9 }, (_, i) => ({ ...ironShot(i), timestamp: stamp(i) }));
  if (kind === 'iron') return irons;
  return [
    ...irons,
    {
      ...BASE,
      club: '8-iron',
      club_data_status: 'pending',
      timestamp: stamp(9),
    },
  ];
}

async function showSession(page: Page, locale: Locale, kind: Case | 'four-clubs') {
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.addInitScript((locale) => localStorage.setItem('openflight.locale:v1', locale), locale);
  // A phone-sized page is a paired phone's (127.0.0.1 is the kiosk, never phone-sized).
  const phone = (page.viewportSize()?.width ?? 1024) < 600;
  await page.goto(phone ? `${phoneOrigin(test.info().project.use.baseURL!)}/` : '/');
  await page.locator('.range-app').waitFor();
  await page.evaluate(async (shots) => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    (await load('services/socketService')).socketService.disconnect();
    (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
      profiles: [
        {
          id: 'club-test',
          name: 'Golfer',
          settings: { unit_system: 'metric', club_ids: ['wedge-44', '8-iron', '7-wood', 'driver'] },
        },
      ],
      active_profile_id: 'club-test',
    });
    (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
    (await load('stores/useDebugStore')).useDebugStore.setState((state: { triggerStatus: object }) => ({
      triggerStatus: { ...state.triggerStatus, radar_connected: true },
    }));
    (await load('stores/useShotStore')).useShotStore.getState().setShots(shots);
    await document.fonts.ready;
  }, session(kind));
  await page.locator('.picker-overlay__close').tap();
}

/** Visible title text that leaves its tile (the clipped screen-reader label of the ≈ mark does not count). */
function titleOverflow(page: Page, cards: string): Promise<string[]> {
  return page.locator(`${cards} .metric-card__label`).evaluateAll((labels) =>
    labels.flatMap((label) => {
      const card = label.closest('.metric-card')!.getBoundingClientRect();
      const walker = document.createTreeWalker(label, NodeFilter.SHOW_TEXT);
      let past = 0;
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const hidden = node.parentElement?.closest('.metric-card__estimated-label');
        if (hidden && getComputedStyle(hidden).clip !== 'auto') continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (rect.width > 0) past = Math.max(past, card.left - rect.left, rect.right - card.right);
        }
      }
      return past > 1 ? [`${label.textContent?.trim()}: ${Math.round(past)}`] : [];
    })
  );
}

/** Captions and quality pills in these tiles that are not displayed (a short tile drops its meta row). */
function hiddenMetaParts(page: Page, cards: string): Promise<string[]> {
  return page.locator(cards).evaluateAll((elements) =>
    elements.flatMap((card) =>
      [...card.querySelectorAll('.metric-card__meta > *')]
        .filter((part) => part.getClientRects().length === 0 || getComputedStyle(part).display === 'none')
        .map((part) => `${card.getAttribute('data-metric')}: ${part.textContent?.trim()}`)
    )
  );
}

function fontSizes(page: Page, selector: string): Promise<number[]> {
  return page
    .locator(selector)
    .evaluateAll((elements) =>
      elements
        .filter((element) => element.getBoundingClientRect().width > 0)
        .map((element) => parseFloat(getComputedStyle(element).fontSize))
    );
}

async function expectCardsFit(page: Page, cards: string) {
  expect(await titleOverflow(page, cards), 'titles').toEqual([]);
  expect(await overflowingLiveMetricValues(page, cards), 'values').toEqual([]);
  expect(await overflowingLiveMetricMetadata(page, cards), 'captions').toEqual([]);
  // A title (with its ≈ mark) never wraps into the number below it.
  const labels = (await crowdedTileParts(page, cards)).filter((problem) => problem.includes('metric-card__label'));
  expect(labels, 'titles crowd their numbers').toEqual([]);
}

/** What the ≈ legend says: the club page has calculated tiles only, the ball page a typical-spin carry too. */
const LEGENDS: Record<Locale, { club: string; ball: string }> = {
  de: { club: '≈ berechnet', ball: '≈ berechnet bzw. mit typischem Spin' },
  en: { club: '≈ calculated', ball: '≈ calculated or with typical spin' },
  es: { club: '≈ calculado', ball: '≈ calculado o con spin típico' },
};

/**
 * Under 420 px the ≈ legend sits under the grid (in a•space it squeezed the club
 * on 375 and 393 px phones); elsewhere whole, in the toolbar.
 * Its two-meaning form (`below`) sits under the grid on every portrait phone.
 * Under 360 px on the ball page it leads the carry notes' short line instead of
 * a footnote of its own ("≈ … · Kurve nicht berücksichtigt", one paragraph).
 */
async function expectDetailsLegend(page: Page, width: number, text: string, below = width < 420, ballPage = false) {
  const toolbarLegend = page.locator('.range-details-toolbar .range-details-legend');
  const footnote = page.locator('.range-details-footnote');
  const inNotes = ballPage && width < 360;
  const notesLegend = page.locator('.range-details-carry-notes .carry-notes__legend');
  const shown = inNotes ? notesLegend : below ? footnote : toolbarLegend;
  await expect(shown).toBeVisible();
  if (inNotes) await expect(shown).toHaveText(new RegExp(`^${text}( · )?$`));
  else await expect(shown).toHaveText(text);
  // The notes' legend is an inline run inside their paragraph: the paragraph (its line box) must be in view.
  await expect(inNotes ? page.locator('.range-details-carry-notes') : shown).toBeInViewport({ ratio: 1 });
  await expect(below ? toolbarLegend : footnote).toBeHidden();
  if (inNotes) await expect(footnote).toBeHidden();
  const legendSelector = inNotes
    ? '.range-details-carry-notes'
    : below
      ? '.range-details-footnote'
      : '.range-details-legend';
  expect(await clippedText(page, legendSelector), 'legend').toEqual([]);
}

/** No title, value or caption of these elements names a value this hardware cannot measure. */
async function expectNoUnmeasurable(
  page: Page,
  locale: Locale,
  selector: string,
  words: readonly string[] = UNMEASURABLE_COPY[locale]
) {
  const text = (await page.locator(selector).allInnerTexts()).join(' | ');
  for (const word of words) expect(text, `${selector}: ${word}`).not.toContain(word);
  expect(text, selector).not.toMatch(/69[.,]9|12[.,]2|19[.,]1|12[.,]3|\bcm\b|\bmm\b/);
}

/**
 * The impact card's face (its drawn viewBox, `meet`): inside the card, above the word (stacked, phones)
 * or left of it (the kiosk's two-column card), and not squeezed away.
 */
function impactCardFit(page: Page) {
  return page.locator('.club-data__grid .metric-card[data-metric="cd_impact"]').evaluate((card) => {
    const svg = card.querySelector('svg.impact-face') as SVGSVGElement;
    const box = svg.getBoundingClientRect();
    const view = svg.viewBox.baseVal;
    const scale = Math.min(box.width / view.width, box.height / view.height);
    const width = view.width * scale;
    const height = view.height * scale;
    const face = { left: box.left + (box.width - width) / 2, top: box.top + (box.height - height) / 2, width, height };
    const outer = card.getBoundingClientRect();
    const word = card.querySelector('.metric-card__value')!.getBoundingClientRect();
    return {
      inside:
        face.left >= outer.left - 0.5 &&
        face.left + width <= outer.right + 0.5 &&
        face.top >= outer.top - 0.5 &&
        face.top + height <= outer.bottom + 0.5,
      aboveWord: face.top + height <= word.top + 1,
      besideWord: face.left + width <= word.left + 1,
      // At least as tall as a line of the word (it gives way first in a short card).
      visible: height >= Math.min(24, word.height),
      height: Math.round(height),
      // Share of the card's height the drawn face takes.
      heightShare: height / outer.height,
      cardWidth: outer.width,
    };
  });
}

async function expectNoPageScroll(page: Page, width: number) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
}

async function openMenuItem(page: Page, locale: Locale, name: string) {
  await page.getByRole('button', { name: LABELS[locale].menu }).tap();
  await chooseMenuItem(page, name, { tap: true });
}

for (const { viewport, phone } of VIEWPORTS) {
  const minButton = phone ? 44 : 56;
  const kiosk = KIOSK_VIEWPORTS.some(({ width, height }) => width === viewport.width && height === viewport.height);
  const landscape = viewport.width > viewport.height && viewport.height < 440;
  // 320×460: no room for the switch's own row above the Statistik chips.
  const shortPortrait = phone && !landscape && viewport.height < 500;

  test.describe(`club data at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, isMobile: phone || landscape, hasTouch: true });

    for (const locale of LOCALES) {
      for (const kind of ['iron', 'driver', 'pending'] as const) {
        test(`${locale} ${kind}: the club page, its statistics and the shot line fit`, async ({ page }) => {
          await showSession(page, locale, kind);
          const labels = LABELS[locale];

          // "All metrics": the ball page first, as before.
          await page.locator('.range-details-button').tap();
          await expect(page.locator(BALL_CARDS)).toHaveCount(11);
          await expectNoUnmeasurable(page, locale, BALL_CARDS);
          const ballNumbers = await fontSizes(page, `${BALL_CARDS} .metric-card__value`);
          // The camera path and attack angle are calculated: the ball page names ≈ too,
          // next to the typical-spin carry with both of its meanings.
          if (kind !== 'pending') {
            const portraitPhone = phone && viewport.width < viewport.height;
            await expectDetailsLegend(page, viewport.width, LEGENDS[locale].ball, portraitPhone, true);
            const ballBar = await page
              .locator('.range-details-toolbar')
              .evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
            expect(ballBar.scroll, 'ball toolbar scrollWidth').toBeLessThanOrEqual(ballBar.client);
            await expectNoPageScroll(page, viewport.width);
          }
          expect(
            await crowdedTileParts(page, BALL_CARDS).then((all) => all.filter((p) => p.includes('label'))),
            'ball titles'
          ).toEqual([]);
          // Portrait phones: no ball tile drops its caption or its experimental pill (the tiles
          // scroll inside the panel instead), and every part stays inside its tile.
          if (phone && !landscape) {
            expect(await hiddenMetaParts(page, BALL_CARDS), 'ball captions and pills shown').toEqual([]);
            expect(await crowdedTileParts(page, BALL_CARDS), 'ball tile parts').toEqual([]);
          }
          // In a•space a squeezed "← Übersicht" ran its label into its border on the kiosk.
          if (kiosk) {
            expect(await labelsPastPadding(page, '.range-details-toolbar button'), 'ball toolbar buttons').toEqual([]);
            expect(await clippedText(page, '.range-details-toolbar button, .range-details-toolbar > span')).toEqual([]);
          }
          // The legend and the carry notes under the grid take no room from the ball tiles.
          expect(await overflowingLiveMetricValues(page, BALL_CARDS), 'ball values').toEqual([]);
          expect(await overflowingLiveMetricMetadata(page, BALL_CARDS), 'ball captions').toEqual([]);

          const toolbar = page.locator('.range-details-toolbar');
          await toolbar.getByRole('button', { name: labels.club, exact: true }).tap();
          await expect(page.locator(CLUB_CARDS)).toHaveCount(5);
          expect(
            await page.locator(CLUB_CARDS).evaluateAll((cards) => cards.map((card) => card.getAttribute('data-metric')))
          ).toEqual(CLUB_IDS);
          // The low point has its own card; no other card names it or its centimetres.
          await expectNoUnmeasurable(
            page,
            locale,
            `${CLUB_CARDS}:not([data-metric="cd_low_point"])`,
            unmeasurableClubPageCopy(locale)
          );
          // The low point: its distance without sign, its side, ±σ and "calculated · uncalibrated"
          // (the caption is in the card even where a short tile hides it); "—" for a driver.
          const lowPoint = page.locator(`${CLUB_CARDS}[data-metric="cd_low_point"]`);
          await expect(lowPoint.locator('.metric-card__value')).toHaveText(LOW_POINT[kind][locale].value);
          if (kind === 'iron') {
            await expect(lowPoint.locator('.metric-card__unit')).toHaveText('cm');
            await expect(lowPoint.locator('.metric-card__subtext')).toHaveText(LOW_POINT[kind][locale].side!, {
              useInnerText: false,
            });
          } else {
            await expect(lowPoint.locator('.metric-card__unit')).toHaveCount(0);
            await expect(lowPoint.locator('.metric-card__estimated')).toHaveCount(0);
          }
          await page.evaluate(
            () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
          );

          await expectCardsFit(page, CLUB_CARDS);
          await expect(page.locator(`${CLUB_CARDS}[data-metric="cd_impact"] .metric-card__value`)).toHaveText(
            IMPACT_WORDS[kind][locale]
          );
          const impactFit = await impactCardFit(page);
          if (kiosk) {
            // Two columns beside the low point: the face left over most of the card, the word beside it.
            expect(impactFit, 'impact face').toMatchObject({ inside: true, besideWord: true, visible: true });
            expect(impactFit.heightShare, 'impact face height / card height').toBeGreaterThanOrEqual(0.55);
            const lowPointWidth = (await lowPoint.boundingBox())!.width;
            expect(Math.abs(impactFit.cardWidth - 2 * lowPointWidth), 'impact card ≈ two cells').toBeLessThanOrEqual(4);
          } else {
            expect(impactFit, 'impact face').toMatchObject({ inside: true, aboveWord: true, visible: true });
          }
          const bar = await toolbar.evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
          expect(bar.scroll, 'toolbar scrollWidth').toBeLessThanOrEqual(bar.client);
          for (const button of await toolbar.locator('button').all()) {
            const box = (await button.boundingBox())!;
            expect(box.height, await button.innerText()).toBeGreaterThanOrEqual(minButton - 0.5);
            expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 1);
          }
          await expectDetailsLegend(page, viewport.width, LEGENDS[locale].club);
          if (kiosk) {
            expect(await labelsPastPadding(page, '.range-details-toolbar button'), 'club toolbar buttons').toEqual([]);
            expect(await clippedText(page, '.range-details-toolbar button, .range-details-toolbar > span')).toEqual([]);
          }
          await expectNoPageScroll(page, viewport.width);

          if (kind === 'iron') {
            for (const id of CALCULATED_IDS) {
              await expect(page.locator(`${CLUB_CARDS}[data-metric="${id}"] .metric-card__estimated`), id).toHaveCount(
                1
              );
            }
          }
          // Numbers only: the impact card's value is a word, sized as one.
          const clubNumbers = await fontSizes(page, `${CLUB_CARDS}:not([data-metric="cd_impact"]) .metric-card__value`);
          expect(Math.min(...clubNumbers), 'club number px').toBeGreaterThanOrEqual(Math.min(...ballNumbers) - 1);

          // Statistics → club page of the session's club; the switch leads the chip row, outside the strip.
          await openMenuItem(page, locale, labels.stats);
          await expect(page.locator(STATS_BALL_CARDS)).toHaveCount(6);
          const statsBallNumbers = await fontSizes(page, `${STATS_BALL_CARDS} .metric-card__value`);
          const pageSwitch = page.locator(STATS_SWITCH);
          await pageSwitch.getByRole('button', { name: labels.club, exact: true }).tap();
          await page.locator('.stats-panel__chip-scroll .panel-chip').first().tap();
          await expect(page.locator(STATS_CARDS)).toHaveCount(2);
          expect(
            await page.locator(STATS_CARDS).evaluateAll((cards) => cards.map((card) => card.getAttribute('data-metric')))
          ).toEqual(STATS_IDS);
          await expectNoUnmeasurable(page, locale, STATS_CARDS);
          await page.evaluate(
            () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
          );
          await expectCardsFit(page, STATS_CARDS);
          // Short tiles drop their captions: the header names ≈.
          await expect(page.locator('.stats-panel__legend')).toBeVisible();
          expect(await clippedText(page, '.stats-panel__legend'), 'stats legend').toEqual([]);
          const statsClubNumbers = await fontSizes(page, `${STATS_CARDS} .metric-card__value`);
          // "-16,7 ± 0,1°" is about twice as wide as a ball-page number: where the
          // tile height allows, it still keeps more than half the ball-page size
          // (it sat at the 1rem floor, 24 of 66 px, on the 1024×600 kiosk).
          if (statsClubNumbers.length > 0) {
            const ratio = Math.min(...statsClubNumbers) / Math.min(...statsBallNumbers);
            expect(ratio, 'stats club / ball number px').toBeGreaterThanOrEqual(0.5);
          }
          const chipHeight = (await page.locator('.stats-panel__chips .panel-chip').first().boundingBox())!.height;
          for (const button of await pageSwitch.locator('.segmented-control__button').all()) {
            const box = (await button.boundingBox())!;
            // As tall as the club chips: 44 px on phones.
            expect(Math.abs(box.height - chipHeight), await button.innerText()).toBeLessThanOrEqual(1);
            if (phone) expect(box.height).toBeGreaterThanOrEqual(43.5);
          }
          expect(await clippedText(page, `${STATS_SWITCH} .segmented-control__button`)).toEqual([]);
          await expectNoPageScroll(page, viewport.width);

          // Shot list: the newest row's club-data line wraps inside the screen.
          await openMenuItem(page, locale, labels.shots);
          await page.locator('.shots-panel__row-main').first().tap();
          const line = page.locator('.shots-panel__club-data');
          if (kind === 'pending') {
            await expect(line).toHaveCount(0);
          } else {
            await expect(line).toBeVisible();
            await expectNoUnmeasurable(page, locale, '.shots-panel__club-data');
            expect(await clippedText(page, '.shots-panel__club-data')).toEqual([]);
            const box = (await line.boundingBox())!;
            expect(box.x).toBeGreaterThanOrEqual(-0.5);
            expect(box.x + box.width).toBeLessThanOrEqual(viewport.width + 0.5);
          }
          await expectNoPageScroll(page, viewport.width);
        });
      }
    }

    for (const locale of LOCALES) {
      test(`${locale} iron with typical spin: the ball page keeps its values in their tiles`, async ({ page }) => {
        await showSession(page, locale, 'iron');
        // The typical-spin carry (no spin axis) with the span the server sends, which no
        // screen shows: every carry note applies.
        await page.evaluate(async () => {
          const { useShotStore } = await import(/* @vite-ignore */ '/src/stores/useShotStore.ts');
          const shots = useShotStore.getState().shots;
          useShotStore
            .getState()
            .setShots(
              shots.map((shot: object) => ({ ...shot, carry_span_min_yards: 120.4, carry_span_max_yards: 139.6 }))
            );
        });
        await page.locator('.range-details-button').tap();
        await expect(page.locator(BALL_CARDS)).toHaveCount(11);
        await page.evaluate(
          () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
        );
        expect(await overflowingLiveMetricValues(page, BALL_CARDS), 'ball values').toEqual([]);
        expect(await overflowingLiveMetricMetadata(page, BALL_CARDS), 'ball captions').toEqual([]);
        await expectDetailsLegend(
          page,
          viewport.width,
          LEGENDS[locale].ball,
          phone && viewport.width < viewport.height,
          true
        );
        const notes = page.locator('.range-details-carry-notes');
        await expect(notes).toBeVisible();
        await expect(notes).toBeInViewport({ ratio: 1 });
        expect(await clippedText(page, '.range-details-carry-notes'), 'notes').toEqual([]);
        expect(
          await crowdedTileParts(page, BALL_CARDS).then((all) => all.filter((p) => p.includes('label'))),
          'ball titles'
        ).toEqual([]);
        await expectNoPageScroll(page, viewport.width);
      });
    }

    test('de four clubs: the Statistik switch stays in view and a picked chip scrolls into view', async ({ page }) => {
      await showSession(page, 'de', 'four-clubs');
      await openMenuItem(page, 'de', 'Statistik');
      const strip = page.locator('.stats-panel__chip-scroll');
      const pageSwitch = page.locator(STATS_SWITCH);
      const inView = async (selector: string) => {
        const outside = await page.locator(selector).evaluateAll((elements) =>
          elements.flatMap((element) => {
            const box = element.getBoundingClientRect();
            const clip = element.closest('.stats-panel__chip-scroll')?.getBoundingClientRect() ?? {
              left: 0,
              right: innerWidth,
            };
            return box.left < Math.max(0, clip.left) - 1 || box.right > Math.min(innerWidth, clip.right) + 1
              ? [element.textContent ?? '']
              : [];
          })
        );
        expect(outside, selector).toEqual([]);
      };
      await inView(`${STATS_SWITCH} .segmented-control__button`);
      // The first club chip is fully visible from the start (the switch has its own row on
      // portrait phones of 500 px and more; 320×460 keeps it beside "All").
      if (!shortPortrait) await inView('.stats-panel__chip-scroll .panel-chip >> nth=0');
      // There the compact switch still leaves the strip a drag target with part of the first club.
      else expect((await strip.boundingBox())!.width, 'strip width').toBeGreaterThanOrEqual(48);
      // The switch is no member of the club filter group.
      await expect(
        page
          .getByRole('group', { name: 'Nach Schläger filtern' })
          .getByRole('button', { name: 'Schläger', exact: true })
      ).toHaveCount(0);
      // Pick the last club after dragging there: it comes fully into view, the switch stays.
      await strip.evaluate((node) => node.scrollTo({ left: node.scrollWidth - node.clientWidth - 12 }));
      const last = strip.locator('.panel-chip').last();
      await last.tap();
      await expect(last).toHaveAttribute('aria-pressed', 'true');
      await page.waitForTimeout(400);
      if (!shortPortrait) {
        await inView('.stats-panel__chip-scroll .panel-chip[aria-pressed="true"]');
      } else {
        // 320×460: the strip is narrower than the chip; the chip then fills the strip.
        const shown = await strip.evaluate((node) => {
          const box = node.getBoundingClientRect();
          const chip = node.querySelector('[aria-pressed="true"]')!.getBoundingClientRect();
          return { overlap: Math.min(box.right, chip.right) - Math.max(box.left, chip.left), strip: box.width };
        });
        expect(shown.overlap, 'picked chip in the strip').toBeGreaterThanOrEqual(shown.strip - 1);
      }
      await inView(`${STATS_SWITCH} .segmented-control__button`);
      await pageSwitch.getByRole('button', { name: 'Schläger', exact: true }).tap();
      await expect(page.locator(STATS_CARDS)).toHaveCount(2);
      await pageSwitch.getByRole('button', { name: 'Ball', exact: true }).tap();
      await expect(page.locator(STATS_BALL_CARDS)).toHaveCount(6);
      await expectNoPageScroll(page, viewport.width);
    });
  });
}
