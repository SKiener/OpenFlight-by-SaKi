import { expect, test, type Page } from '@playwright/test';
import {
  chooseMenuItem,
  clippedText,
  crowdedFooter,
  crowdedTileParts,
  mockSessionHistory,
  PHONE_VIEWPORTS,
  smallTargets,
} from './helpers';
import { openRange, phoneOrigin, routeApi, SHOT } from './phoneFixture';

/**
 * Phone screens other than the camera views, in German (the longest labels):
 * pairing, Diagnose (OPS recovery), the Live toolbar with long club names,
 * experimental TI club tiles and fingertip sizes in landscape.
 */
const PORTRAIT = [...PHONE_VIEWPORTS, { width: 320, height: 568 }, { width: 375, height: 667 }] as const;

async function openMenuItem(page: Page, name: string) {
  await page.getByRole('button', { name: 'Menü öffnen' }).click();
  await chooseMenuItem(page, name);
}

/** Diagnose with a real (not mock) sound-trigger radar and a recovery the test completes. */
async function openDiagnose(page: Page) {
  await openRange(page);
  await page.evaluate(async () => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    const { socketService } = await load('services/socketService');
    socketService.reinitializeRadar = () =>
      new Promise((resolve) => Object.assign(window, { finishRecovery: resolve }));
    (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true, mockMode: false });
    (await load('stores/useDebugStore')).useDebugStore.setState((state: { triggerStatus: object }) => ({
      triggerStatus: { ...state.triggerStatus, mode: 'rolling-buffer', trigger_type: 'sound', radar_connected: true },
    }));
  });
  await openMenuItem(page, 'Diagnose');
}

/** How much of `selector` shows inside its scroller and the window, 0..1 (vertically). */
function shownInScroller(page: Page, selector: string, scroller: string) {
  return page.evaluate(
    ({ selector, scroller }) => {
      const element = document.querySelector(selector)!.getBoundingClientRect();
      const box = document.querySelector(scroller)!.getBoundingClientRect();
      const top = Math.max(element.top, box.top, 0);
      const bottom = Math.min(element.bottom, box.bottom, innerHeight);
      return { share: Math.max(0, bottom - top) / element.height, topShown: element.top >= box.top - 0.5 };
    },
    { selector, scroller }
  );
}

/** "Schlag 124" stays clear of the toolbar buttons, whose labels keep inside their 44 px pills. */
async function expectToolbarReadable(page: Page, club: string) {
  const shots = Array.from({ length: 124 }, (_, index) => ({
    ...SHOT,
    camera_replay: null,
    timestamp: new Date(Date.parse(SHOT.timestamp) + index * 60_000).toISOString(),
  }));
  await openRange(page, { shots });
  await page.evaluate(async (club) => {
    const { useSystemStore } = await import(/* @vite-ignore */ '/src/stores/useSystemStore.ts');
    useSystemStore.setState({ serverClub: club });
  }, club);
  await expect(page.locator('.range-shot-heading h1')).toHaveText('Schlag 124');
  const crowded = () =>
    page.evaluate(() => {
      const problems: string[] = [];
      const heading = document.querySelector('.range-shot-heading h1');
      const actions = document.querySelector('.range-toolbar-actions');
      if (heading && actions) {
        const range = document.createRange();
        range.selectNodeContents(heading);
        const text = range.getBoundingClientRect();
        if (text.right > actions.getBoundingClientRect().left + 0.5) problems.push('heading under the buttons');
        const hit = document.elementFromPoint(text.right - 2, text.top + text.height / 2);
        if (!hit || !heading.contains(hit)) problems.push(`heading end covered by ${hit?.className}`);
        // Profile and time: under the heading, clear of the buttons (an ellipsis may shorten them).
        const line = heading.parentElement?.querySelector(':scope > span');
        const box = line?.getBoundingClientRect();
        if (line && box && box.width > 0) {
          if (box.right > actions.getBoundingClientRect().left + 0.5) problems.push('profile line under the buttons');
          if (box.top < heading.getBoundingClientRect().bottom - 1) problems.push('profile line beside the heading');
          const end = document.elementFromPoint(box.right - 2, box.top + box.height / 2);
          if (!end || !line.contains(end)) problems.push(`profile line end covered by ${end?.className}`);
          if (line.scrollWidth > line.clientWidth && getComputedStyle(line).textOverflow !== 'ellipsis')
            problems.push('profile line cut without an ellipsis');
        }
      }
      for (const button of document.querySelectorAll<HTMLElement>('.range-toolbar button')) {
        const box = button.getBoundingClientRect();
        if (box.width === 0) continue;
        if (button.scrollWidth > button.clientWidth + 1) problems.push(`${button.textContent?.trim()}: text overflows`);
        for (const part of button.querySelectorAll('*')) {
          const rect = part.getBoundingClientRect();
          if (rect.width && (rect.right > box.right + 1 || rect.left < box.left - 1))
            problems.push(`${button.textContent?.trim()}: ${part.nodeName} outside`);
        }
        if (box.height < 44) problems.push(`${button.textContent?.trim()}: lower than 44 px`);
      }
      if (document.documentElement.scrollWidth > document.documentElement.clientWidth)
        problems.push('page scrolls sideways');
      return problems;
    });
  expect(await crowded()).toEqual([]);
  await page.locator('.range-details-button').click();
  await expect(page.locator('.range-back')).toBeVisible();
  expect(await crowded()).toEqual([]);
}

for (const viewport of PORTRAIT) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de)`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('the pairing code entry fits the screen width', async ({ page }) => {
      await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await page.goto('/pair');
      await expect(page.locator('.pairing-screen__input')).toBeVisible();
      // innerWidth grows with the content in mobile emulation, so compare with the layout width.
      const widths = await page.evaluate(() => ({
        scroll: document.documentElement.scrollWidth,
        client: document.documentElement.clientWidth,
        input: document.querySelector('.pairing-screen__input')!.getBoundingClientRect().right,
        submit: document.querySelector('.pairing-screen__submit')!.getBoundingClientRect().right,
      }));
      expect(widths.client).toBe(viewport.width);
      expect(widths.scroll).toBeLessThanOrEqual(widths.client);
      expect(widths.input).toBeLessThanOrEqual(viewport.width);
      expect(widths.submit).toBeLessThanOrEqual(viewport.width);
    });

    test('the footer labels stay inside their buttons and the buttons inside the bar', async ({ page }) => {
      await openRange(page);
      expect(await crowdedFooter(page)).toEqual([]);
      expect(await clippedText(page, '.range-footer nav, .range-footer button')).toEqual([]);
      expect(await smallTargets(page, '.range-footer button')).toEqual([]);
    });

    test('Diagnose shows every tab and brings the recovery result into view', async ({ page }) => {
      await openDiagnose(page);
      const tabs = page.locator('.debug-tabs');
      expect(await tabs.evaluate((row) => row.scrollWidth - row.clientWidth)).toBeLessThanOrEqual(0);
      const button = page.getByRole('button', { name: 'OPS-Radar wiederherstellen', exact: true });
      await button.click();
      // The pending label stays inside the green button.
      const pending = page.getByRole('button', { name: 'OPS-Radar wird wiederhergestellt…' });
      expect(await pending.evaluate((element) => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(0);
      for (const code of ['commands_sent', 'failed', 'timeout']) {
        await page.evaluate(
          (code) => Reflect.get(window, 'finishRecovery')({ ok: code === 'commands_sent', code }),
          code
        );
        await expect(page.getByRole('status')).toBeVisible();
        await page.waitForTimeout(100);
        const shown = await shownInScroller(
          page,
          '.debug-panel__recovery [role="status"]',
          '.debug-panel__tab-content'
        );
        // A 124 px scroller (320 × 460) cannot hold the longest result: its start and most of it show.
        if (viewport.height >= 548) expect(shown.share, code).toBeGreaterThanOrEqual(0.99);
        else {
          expect(shown.topShown, code).toBe(true);
          expect(shown.share, code).toBeGreaterThan(0.5);
        }
        if (code !== 'timeout') await button.click();
      }
    });

    for (const club of ['pw', 'mini-driver'] as const) {
      test(`the Live toolbar keeps "Schlag 124" readable beside a long club (${club})`, async ({ page }) => {
        await expectToolbarReadable(page, club);
      });
    }
  });
}

for (const viewport of [
  { width: 568, height: 320 },
  { width: 667, height: 375 },
]) {
  test.describe(`phone in landscape ${viewport.width}×${viewport.height} (de), Live toolbar`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    // The extra "Letzten Schlag löschen" button shows here; Pitching Wedge is a default club.
    for (const club of ['driver', 'pw', 'mini-driver'] as const) {
      test(`the Live toolbar keeps "Schlag 124" readable beside the delete button (${club})`, async ({ page }) => {
        await expectToolbarReadable(page, club);
      });
    }
  });
}

for (const viewport of [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
  { width: 667, height: 375 },
  { width: 932, height: 430 },
]) {
  test.describe(`phone ${viewport.width}×${viewport.height} (de), profile name`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    async function renameGolfer(page: Page) {
      await openMenuItem(page, 'Profile');
      await page.getByRole('button', { name: 'Golfer umbenennen' }).tap();
      await expect(page.locator('.profile-name-modal')).toBeVisible();
    }

    test('a paired phone types the name on its own keyboard', async ({ page, baseURL }) => {
      await openRange(page, { origin: phoneOrigin(baseURL!) });
      await renameGolfer(page);
      const input = page.locator('.profile-name-modal__input');
      // A plain text field with focus: iOS raises its keyboard, and at 16 px or more Safari does not zoom in.
      await expect(input).toBeFocused();
      expect(await input.getAttribute('inputmode')).not.toBe('none');
      expect(await input.evaluate((field) => parseFloat(getComputedStyle(field).fontSize))).toBeGreaterThanOrEqual(16);
      await expect(page.locator('.profile-name-modal__keyboard')).toHaveCount(0);
      await input.press('End');
      await page.keyboard.type(' K.');
      await expect(input).toHaveValue('Golfer K.');
      // Confirm and Cancel stay on screen and fingertip-sized.
      expect(
        await smallTargets(page, '.profile-name-modal__actions .panel-action, .profile-name-modal__close')
      ).toEqual([]);
      await expect(page.locator('.profile-name-modal__actions')).toBeInViewport({ ratio: 1 });
    });

    test('the kiosk keeps its on-screen keyboard', async ({ page, baseURL }) => {
      await openRange(page, { origin: baseURL });
      await renameGolfer(page);
      await expect(page.locator('.profile-name-modal__keyboard')).toBeVisible();
      expect(await page.locator('.profile-name-modal__input').getAttribute('inputmode')).toBe('none');
    });
  });
}

const QR = `data:image/svg+xml;utf8,${encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 29 29"><rect width="29" height="29" fill="#fff"/><rect x="2" y="2" width="7" height="7"/></svg>'
)}`;

for (const viewport of [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
]) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de), pair dialog`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('every part of the pair dialog can be scrolled into the panel and stays on screen', async ({ page }) => {
      await openRange(page);
      await page.route('**/api/pairing/code', (route) =>
        route.fulfill({
          json: {
            code: 'ABCD2345',
            expires_in_s: 300,
            links: [
              { host: 'openflight.local:8080', url: 'http://openflight.local:8080/pair#code=ABCD2345', qr_svg: QR },
              { host: '192.168.178.123:8080', url: 'http://192.168.178.123:8080/pair#code=ABCD2345', qr_svg: QR },
            ],
            wifi: { ssid: 'OpenFlight-Range', password: 'e2e-dummy-pass-1234', qr_svg: QR },
          },
        })
      );
      // The kiosk itself (local access) offers pairing; /api is answered in the page here.
      await page.evaluate(async () => {
        const { usePairingStore } = await import(/* @vite-ignore */ '/src/stores/usePairingStore.ts');
        usePairingStore.getState().setAccess('local');
      });
      await openMenuItem(page, 'Gerät koppeln');
      const panel = page.locator('.pair-dialog__panel');
      await expect(panel.locator('.pair-dialog__code')).toBeVisible();
      const problems = await panel.evaluate((panel) => {
        const problems: string[] = [];
        const box = panel.getBoundingClientRect();
        if (box.left < -0.5 || box.top < -0.5 || box.right > innerWidth + 0.5 || box.bottom > innerHeight + 0.5)
          problems.push('panel off screen');
        if (panel.scrollWidth > panel.clientWidth) problems.push('panel scrolls sideways');
        const inner = {
          left: Math.max(0, box.left),
          top: Math.max(0, box.top),
          right: Math.min(innerWidth, box.right),
          bottom: Math.min(innerHeight, box.bottom),
        };
        for (const element of panel.querySelectorAll('*')) {
          element.scrollIntoView({ block: 'nearest', inline: 'nearest' });
          const rect = element.getBoundingClientRect();
          if (rect.width === 0 || rect.height === 0) continue;
          const label = `${element.className || element.tagName} "${(element.textContent ?? '').trim().slice(0, 20)}"`;
          if (rect.left < inner.left - 0.5 || rect.right > inner.right + 0.5) problems.push(`${label} sideways`);
          if (element.scrollWidth > element.clientWidth + 1) problems.push(`${label} text runs out`);
          if (
            rect.height <= inner.bottom - inner.top &&
            (rect.top < inner.top - 0.5 || rect.bottom > inner.bottom + 0.5)
          )
            problems.push(`${label} cannot be scrolled into the panel`);
        }
        panel.scrollTop = panel.scrollHeight;
        return problems;
      });
      expect(problems).toEqual([]);
      // At the end of the panel Close is on screen and tappable.
      const close = panel.locator('.pair-dialog__actions .panel-action').last();
      await expect(close).toBeInViewport({ ratio: 1 });
      expect((await close.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    });
  });
}

for (const viewport of [
  { width: 320, height: 568 },
  { width: 375, height: 667 },
  { width: 393, height: 659 },
]) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de), analysis table`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('the shot table fits its card and breaks no name, club or date inside a word', async ({ page }) => {
      await routeApi(page, {});
      await mockSessionHistory(page, [
        {
          summary: { id: 's1', started_at: '2026-10-07T09:00:00Z', shot_count: 2, clubs: ['driver', 'pw'] },
          shots: [
            { ...SHOT, shot_number: 1 },
            { ...SHOT, club: 'pw', shot_number: 2, camera_replay: null, carry_spin_adjusted: null },
          ],
        },
      ]);
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await page.goto('/?view=analysis');
      await expect(page.getByTestId('shot-count')).toHaveText('2');
      const problems = await page.locator('.analysis-shots').evaluate((card) => {
        const problems: string[] = [];
        const style = getComputedStyle(card);
        const right =
          card.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
        if (card.querySelector('table')!.getBoundingClientRect().right > right + 0.5)
          problems.push('table wider than its card');
        for (const cell of card.querySelectorAll('td')) {
          if (getComputedStyle(cell).overflowWrap !== 'normal')
            problems.push(`overflow-wrap ${getComputedStyle(cell).overflowWrap}`);
          // Each token of a name, club or date stays on one line.
          const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            const text = node.textContent ?? '';
            for (const match of text.matchAll(/Golfer|Driver|Pitching|Wedge|\d{2}\.\d{2}\.\d{2},|\d{2}:\d{2}/g)) {
              const range = document.createRange();
              range.setStart(node, match.index!);
              range.setEnd(node, match.index! + match[0].length);
              const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
              if (tops.size > 1) problems.push(`"${match[0]}" split over ${tops.size} lines`);
            }
          }
        }
        return problems;
      });
      expect(problems).toEqual([]);
      // Below 360 px the details button gives way: only ▷, still named "Schlagdetails" for touch and screen readers.
      const details = page.getByRole('button', { name: 'Schlagdetails #2', exact: true });
      await expect(details).toHaveAttribute('title', 'Schlagdetails');
      const shown = await details.evaluate((button) => {
        const range = document.createRange();
        range.selectNodeContents(button);
        return [...range.getClientRects()].length > 0 ? button.innerText.trim() : '';
      });
      expect(shown).toBe(viewport.width < 360 ? '▷' : 'Schlagdetails');
      expect(await smallTargets(page, '.analysis-shots td button')).toEqual([]);
    });

    // a•space digits are proportional: "244,4" is a third wider than "111,1". The widest realistic row
    // (carry, date; ball and club speed are hidden on phones), in m and in yds, keeps the table inside
    // its card and every value, name and date whole.
    for (const [units, wide] of [
      ['metric', { ball: 244.4 / 1.60934, club: 144.4 / 1.60934, carry: 244.4 / 0.9144, unit: 'm' }],
      ['imperial', { ball: 244.4, club: 144.4, carry: 244.4, unit: 'yds' }],
    ] as const) {
      test(`the shot table keeps the widest values (${units}) whole and inside its card`, async ({ page }) => {
        await routeApi(page, {});
        const shot = {
          ...SHOT,
          club: '3-hybrid',
          ball_speed_mph: wide.ball,
          club_speed_mph: wide.club,
          estimated_carry_yards: wide.carry,
          carry_spin_adjusted: wide.carry,
          launch_angle_vertical: 24.4,
          spin_rpm: 12_444,
          carry_spin_rpm: 12_444,
          carry_spin_source: 'club_typical',
          // "24.04.26," is the widest date of the year (4 is a•space's widest digit).
          timestamp: '2026-04-24T10:44:00Z',
        };
        await mockSessionHistory(page, [
          {
            summary: { id: 's1', started_at: '2026-04-24T10:00:00Z', shot_count: 2, clubs: ['3-hybrid', 'pw'] },
            shots: [
              { ...shot, shot_number: 1 },
              { ...shot, club: 'pw', shot_number: 2, camera_replay: null },
            ],
          },
        ]);
        await page.addInitScript((units) => {
          localStorage.setItem('openflight.locale:v1', 'de');
          localStorage.setItem('openflight.unit-system', units);
        }, units);
        await page.goto('/?view=analysis');
        await expect(page.getByTestId('shot-count')).toHaveText('2');
        await expect(page.locator('.analysis-shots th').nth(2)).toContainText(wide.unit);
        await expect(page.locator('.analysis-shots tbody tr').first().locator('td').nth(2)).toContainText(/≈\s244,4/);
        const problems = await page.locator('.analysis-shots').evaluate((card) => {
          const problems: string[] = [];
          const style = getComputedStyle(card);
          const right =
            card.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth);
          if (card.querySelector('table')!.getBoundingClientRect().right > right + 0.5)
            problems.push('table wider than its card');
          for (const cell of card.querySelectorAll('td')) {
            const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
            for (let node = walker.nextNode(); node; node = walker.nextNode()) {
              const text = node.textContent ?? '';
              // Values, names, clubs and dates each stay on one line.
              for (const match of text.matchAll(/(?:≈\u00a0?)?\d[\d.,:]*|Golfer|Hybrid|Pitching|Wedge/g)) {
                const range = document.createRange();
                range.setStart(node, match.index!);
                range.setEnd(node, match.index! + match[0].length);
                const tops = new Set([...range.getClientRects()].map((rect) => Math.round(rect.top)));
                if (tops.size > 1) problems.push(`"${match[0]}" split over ${tops.size} lines`);
              }
            }
          }
          return problems;
        });
        expect(problems).toEqual([]);
        expect(await clippedText(page, '.analysis-shots td')).toEqual([]);
      });
    }
  });
}

/** A TI-radar shot whose experimental club path was rejected and attack angle accepted (long real statuses). */
const TI_SHOT = {
  ...SHOT,
  camera_replay: null,
  experimental_club_path_deg: 2.4,
  experimental_club_path_status: 'rejected_club_speed_mismatch',
  experimental_attack_angle_deg: -2.2,
  experimental_attack_angle_status: 'accepted_track_speed_warning',
};

for (const viewport of [
  { width: 375, height: 667 },
  { width: 393, height: 659 },
  { width: 430, height: 739 },
  { width: 430, height: 880 },
]) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de), experimental tiles`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('experimental club tiles keep caption and pill out of the number and inside the tile', async ({ page }) => {
      await openRange(page, { shots: [TI_SHOT] });
      await page.locator('.range-details-button').click();
      await expect(page.locator('.metric-card__confidence--experimental').first()).toBeVisible();
      expect(await crowdedTileParts(page, '.live-panel__grid .metric-card')).toEqual([]);
    });
  });
}

test.describe('phone in landscape 568×320 (de), fingertip sizes', () => {
  test.use({ viewport: { width: 568, height: 320 }, isMobile: true, hasTouch: true });

  test('Align controls, Stats chips and club options are at least 44 px', async ({ page }) => {
    await openRange(page);
    await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).click();
    await expect(page.locator('.camera-settings')).toBeVisible();
    expect(
      await smallTargets(
        page,
        ".camera-settings input[type='range'], .camera-settings__mode-controls button, .camera-settings__view-controls button, .camera-settings__manual-controls button"
      )
    ).toEqual([]);
    await openMenuItem(page, 'Statistik');
    expect(await smallTargets(page, '.stats-panel__chips button')).toEqual([]);
    await page.locator('.range-footer nav').getByRole('button', { name: 'Live' }).click();
    await page.getByRole('button', { name: 'Schläger wechseln' }).first().click();
    await expect(page.locator('.picker-overlay')).toBeVisible();
    expect(await smallTargets(page, '.picker-overlay__option')).toEqual([]);
  });
});
