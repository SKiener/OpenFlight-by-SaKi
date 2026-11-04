import { expect, test, type Page } from '@playwright/test';
import { KIOSK_VIEWPORTS, dismissSetupReminder, menuAreaRow, menuBack, openMenuArea } from './helpers';

/**
 * The Pi's own Live screen (KioskLiveOverview): one 56 px header row, two large and three
 * small configurable tiles whose values fill them, and the "No target" hint. Fixture data;
 * the page is the kiosk's (127.0.0.1).
 */

const SLOTS_KEY = 'openflight.kiosk-live-slots:v1';
const SHOT = {
  ball_speed_mph: 120.2, club_speed_mph: 86.4, smash_factor: 1.391,
  estimated_carry_yards: 165, carry_range: [155, 175], club: '7iron',
  timestamp: '2026-11-01T12:00:00Z', peak_magnitude: 100,
  launch_angle_vertical: 16.4, launch_angle_horizontal: 1.6,
  launch_angle_confidence: 0.9, angle_source: 'radar', launch_angle_horizontal_source: 'radar',
  club_angle_deg: -4.3, club_path_deg: 2.6,
  spin_axis_deg: -6, spin_rpm: 6512, spin_confidence: 0.9, spin_quality: 'high', spin_source: 'measured',
  carry_spin_adjusted: 166, carry_spin_rpm: 6512, carry_spin_source: 'measured', total_yards: 174, curve_class: 'draw',
  profile_id: 'kiosk', profile_name: 'Golfer',
};
const LONG = { ...SHOT, ball_speed_mph: 181.4, launch_angle_horizontal: -12.2, spin_rpm: 10000, carry_spin_rpm: 10000, curve_class: 'straight' };

// The camera's impact point on a 7-iron, toward the toe; IMPACT_OPEN's sigma leaves the zone open ("uncertain", the longest word).
const IMPACT = { ...SHOT, club: '7-iron', club_data_status: 'done', impact_offset_mm: 12, impact_sigma_mm: 3, impact_zone: 'toe', impact_method: 'D' };
const IMPACT_OPEN = { ...IMPACT, impact_offset_mm: 7, impact_sigma_mm: 6, impact_zone: null };

async function showKiosk(page: Page, shot: object, { slots, targetLine = true, locale = 'de', units = 'metric' }: { slots?: string[]; targetLine?: boolean; locale?: string; units?: string } = {}) {
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.addInitScript(({ slots, key, locale }) => {
    localStorage.setItem('openflight.locale:v1', locale);
    if (slots) localStorage.setItem(key, JSON.stringify(slots));
  }, { slots: slots ?? null, key: SLOTS_KEY, locale });
  await page.goto('/');
  await page.locator('.range-app').waitFor();
  await page.evaluate(async ({ shot, targetLine, units }) => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    (await load('services/socketService')).socketService.disconnect();
    (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
      profiles: [{ id: 'kiosk', name: 'Golfer', settings: { unit_system: units } }],
      active_profile_id: 'kiosk',
    });
    (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true, calibration: { horizontal_phase_reference: true } });
    (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings({
      available: true,
      target_line: targetLine ? { frame_x_px: 1, frame_y_px: 1, preview_x_pct: 50, preview_y_pct: 50, azimuth_deg: 0, elevation_deg: 0, set_at: 1 } : null,
    });
    (await load('stores/useShotStore')).useShotStore.getState().setShots([shot]);
    await document.fonts.ready;
  }, { shot, targetLine, units });
  await dismissSetupReminder(page);
  const close = page.locator('.picker-overlay__close');
  if (await close.isVisible().catch(() => false)) await close.tap();
}

const tileIds = (page: Page) => page.locator('.kiosk-tile').evaluateAll((tiles) => tiles.map((tile) => tile.getAttribute('data-metric')));

/**
 * Each value's size against its tile's free height, and any value whose glyphs leave its tile's
 * content box (sideways) or its border (up and down). The glyph height comes from the canvas, as a text
 * range only gives the font's line box.
 */
function valueFit(page: Page) {
  return page.locator('.kiosk-tile').evaluateAll((tiles) => tiles.map((tile) => {
    const body = tile.querySelector('.kiosk-tile__body')!.getBoundingClientRect();
    const row = tile.querySelector('.kiosk-tile__value-row')!.getBoundingClientRect();
    const value = tile.querySelector('.kiosk-tile__value')!;
    const range = document.createRange();
    range.selectNodeContents(value);
    const advance = range.getBoundingClientRect();
    const font = getComputedStyle(value);
    const context = document.createElement('canvas').getContext('2d')!;
    context.font = `${font.fontWeight} ${font.fontSize} ${font.fontFamily}`;
    const metrics = context.measureText(value.textContent ?? '');
    const baseline = row.top + (row.height - metrics.fontBoundingBoxAscent - metrics.fontBoundingBoxDescent) / 2 + metrics.fontBoundingBoxAscent;
    const ink = { top: baseline - metrics.actualBoundingBoxAscent, bottom: baseline + metrics.actualBoundingBoxDescent };
    const style = getComputedStyle(tile);
    const box = tile.getBoundingClientRect();
    const inner = { left: box.left + parseFloat(style.paddingLeft) - 1, right: box.right - parseFloat(style.paddingRight) + 1 };
    return {
      id: tile.getAttribute('data-metric'),
      text: value.textContent,
      fill: parseFloat(font.fontSize) / body.height,
      widthShare: advance.width / body.width,
      inkShare: (ink.bottom - ink.top) / body.height,
      clipped: advance.left < inner.left || advance.right > inner.right || ink.top < box.top + parseFloat(style.borderTopWidth) - 0.5 || ink.bottom > box.bottom - parseFloat(style.borderBottomWidth) + 0.5,
    };
  }));
}

test.describe('kiosk Live', () => {
  test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });

  test('one 56 px header row: shot, delete, all values, club and power; no brand, theme or status word', async ({ page }) => {
    await showKiosk(page, SHOT);
    const header = page.locator('.kiosk-live__header');
    await expect(header.locator('h1')).toHaveText('Schlag 1');
    await expect(header.getByRole('button', { name: 'Letzten Schlag löschen' })).toHaveText('Löschen');
    await expect(header.getByRole('button', { name: 'Alle Werte' })).toBeVisible();
    await expect(header.getByRole('button', { name: 'Schläger wechseln' })).toBeVisible();
    await expect(header.locator('.range-power-button--kiosk')).toBeVisible();
    expect((await header.boundingBox())!.height).toBe(56);
    await expect(page.locator('.range-header')).toBeHidden();
    for (const gone of ['.range-brand', '.range-theme', '.range-connection']) await expect(page.locator(gone)).toHaveCount(0);
    // Labels are 22 px (buttons) and 28 px (shot number) at 1024 wide.
    const sizes = await header.evaluate((node) => ({
      h1: parseFloat(getComputedStyle(node.querySelector('h1')!).fontSize),
      buttons: [...node.querySelectorAll('button')].map((button) => parseFloat(getComputedStyle(button).fontSize)),
    }));
    expect(sizes.h1).toBeCloseTo(28, 0);
    for (const size of sizes.buttons) expect(size).toBeCloseTo(22, 0);
  });

  test('shows the default slots, their values filling the tiles', async ({ page }) => {
    await showKiosk(page, SHOT);
    expect(await tileIds(page)).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
    await expect(page.locator('.kiosk-tile__title')).toHaveText(['Flugweite m', 'Balltempo km/h', 'Abflug', 'Richtung', 'Kurve ≈']);
    await expect(page.locator('.kiosk-tile__value')).toHaveText(['152', '193', '16°', '2° R', 'Draw']);
    const fit = await valueFit(page);
    // Two- and three-digit values fill the tile in their limiting dimension. Height-limited: the font is
    // the free height, the digits about 70–80 % of it. a•space is about 45 % wider than Arial, so a value
    // such as "2° R" is width-limited (its font 0.90 of the height in a•space): it fills at least 90 % of
    // the body's width instead, its digits never taller than 82 % of the height.
    for (const tile of fit.filter((entry) => entry.id !== 'curve')) {
      if (tile.fill > 0.95) {
        expect(tile.inkShare, JSON.stringify(tile)).toBeGreaterThan(0.68);
      } else {
        expect(tile.widthShare, JSON.stringify(tile)).toBeGreaterThanOrEqual(0.9);
      }
      expect(tile.inkShare, JSON.stringify(tile)).toBeLessThan(0.82);
    }
    expect(fit.filter((tile) => tile.clipped)).toEqual([]);
  });

  test('the power button opens the power details', async ({ page }) => {
    await showKiosk(page, SHOT);
    await page.locator('.kiosk-live__header .range-power-button--kiosk').tap();
    await expect(page.locator('.range-power-dialog')).toBeVisible();
  });

  for (const viewport of KIOSK_VIEWPORTS) {
    test(`${viewport.width}x${viewport.height}: long values shrink to their width, never clip`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await showKiosk(page, LONG, { slots: ['carry', 'ball_speed', 'spin', 'launch_h', 'curve'] });
      await expect(page.locator('.kiosk-tile__value')).toHaveText(['152', '292', '10.000', '12° L', 'Gerade']);
      expect((await valueFit(page)).filter((tile) => tile.clipped)).toEqual([]);
      const footer = await page.locator('.range-footer button').evaluateAll((buttons) =>
        buttons.filter((button) => button.scrollWidth > button.clientWidth + 1).map((button) => button.textContent));
      expect(footer).toEqual([]);
    });
  }

  test('chooses a tile’s value in the menu, keeps it on the kiosk, and restores the default', async ({ page }) => {
    await showKiosk(page, SHOT);
    await page.locator('.range-footer button[aria-expanded]').tap();
    // The start-page row names the two large tiles; the tiles themselves are on the Live-Kacheln page.
    await expect(menuAreaRow(page, 'Live-Kacheln').locator('.menu-sheet__area-summary')).toHaveText('Flugweite · Balltempo');
    await openMenuArea(page, 'Live-Kacheln', { tap: true });
    const section = page.locator('.live-slots');
    await expect(section.locator('.live-slots__slot')).toHaveText(['Flugweite', 'Balltempo', 'Abflug', 'Richtung', 'Kurve']);
    await section.getByRole('button', { name: 'Kleines Feld 3: Kurve' }).tap();
    const options = section.locator('.live-slots__option');
    await expect(options).toHaveCount(13);
    // The apex comes right after the total.
    await expect(options.nth(2)).toHaveText('Apex');
    // The impact tile is offered last.
    await expect(options.last()).toHaveText('Treffpunkt');
    for (const box of await options.evaluateAll((rows) => rows.map((row) => row.getBoundingClientRect().height))) {
      expect(box).toBeGreaterThanOrEqual(56);
    }
    await section.getByRole('button', { name: 'Spin', exact: true }).tap();
    await expect(options).toHaveCount(0);
    await expect(section.locator('.live-slots__slot').nth(4)).toHaveText('Spin');
    expect(JSON.parse((await page.evaluate((key) => localStorage.getItem(key), SLOTS_KEY))!)).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'spin']);
    await menuBack(page, { tap: true });
    await page.locator('.menu-sheet').getByRole('button', { name: 'Schließen' }).first().tap();
    expect(await tileIds(page)).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'spin']);

    await page.locator('.range-footer button[aria-expanded]').tap();
    await openMenuArea(page, 'Live-Kacheln', { tap: true });
    await section.getByRole('button', { name: 'Standard' }).tap();
    await expect(section.locator('.live-slots__slot').nth(4)).toHaveText('Kurve');
    expect(JSON.parse((await page.evaluate((key) => localStorage.getItem(key), SLOTS_KEY))!)).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
  });

  for (const viewport of KIOSK_VIEWPORTS) {
    test(`${viewport.width}x${viewport.height}: an apex in a large and a small slot reads "Apex ≈ m", whole, inside its tile`, async ({ page }) => {
      await page.setViewportSize(viewport);
      // 31.6 yd = 28.9 m.
      const apexShot = { ...SHOT, apex_yards: 31.6 };
      for (const slots of [
        ['apex', 'ball_speed', 'launch_v', 'launch_h', 'curve'],
        ['carry', 'ball_speed', 'launch_v', 'launch_h', 'apex'],
      ]) {
        await showKiosk(page, apexShot, { slots });
        const tile = page.locator('.kiosk-tile--apex');
        await expect(tile.locator('.kiosk-tile__title')).toHaveText('Apex ≈ m');
        await expect(tile.locator('.kiosk-tile__value')).toHaveText('29');
        expect((await valueFit(page)).filter((entry) => entry.clipped), slots.join()).toEqual([]);
      }
      await showKiosk(page, apexShot, { slots: ['carry', 'apex', 'launch_v', 'launch_h', 'curve'], units: 'imperial', locale: 'en' });
      await expect(page.locator('.kiosk-tile--apex .kiosk-tile__title')).toHaveText('Apex ≈ yds');
      await expect(page.locator('.kiosk-tile--apex .kiosk-tile__value')).toHaveText('32');
      expect((await valueFit(page)).filter((entry) => entry.clipped)).toEqual([]);
    });
  }

  test('an uncertain ball speed: ≈ on its tile and in All values, and the note under the header', async ({ page }) => {
    // The OPS ball-track guard kept a number that reads like the club's trailing track.
    await showKiosk(page, { ...SHOT, ball_speed_status: 'uncertain', ball_speed_status_reason: 'club_like_track' });
    await expect(page.locator('.kiosk-tile--ball_speed .kiosk-tile__title')).toHaveText('Balltempo ≈ km/h');
    await expect(page.locator('.kiosk-tile--ball_speed .kiosk-tile__value')).toHaveText('193');
    const note = page.locator('.range-app__top .ball-speed-note');
    await expect(note).toHaveText('Ballgeschwindigkeit unsicher: Das Radar hat den Ball kaum gesehen.');
    await expect(note).toBeVisible();

    await page.locator('.kiosk-live__header').getByRole('button', { name: 'Alle Werte' }).tap();
    const ballSpeed = page.locator('.metric-card[data-metric="ball_speed"]');
    await expect(ballSpeed.locator('.metric-card__estimated')).toHaveCount(1);
    await expect(note).toBeVisible();
  });

  test('a measured ball speed has no ≈ and no note', async ({ page }) => {
    await showKiosk(page, SHOT);
    await expect(page.locator('.kiosk-tile--ball_speed .kiosk-tile__title')).toHaveText('Balltempo km/h');
    await expect(page.locator('.ball-speed-note')).toHaveCount(0);
  });

  test('a stored layout survives a restart', async ({ page }) => {
    await showKiosk(page, SHOT, { slots: ['club_speed', 'smash', 'spin', 'club_path', 'club_aoa'] });
    await expect(page.locator('.kiosk-tile__value')).toHaveText(['139', '1,39', '6.512', '+3°', '-4°']);
    expect((await valueFit(page)).filter((tile) => tile.clipped)).toEqual([]);
  });

  test('a camera start direction without a target says so, and a tap opens Align', async ({ page }) => {
    await showKiosk(page, { ...SHOT, launch_angle_horizontal_source: 'camera_assisted_experimental' }, { targetLine: false });
    const tile = page.locator('.kiosk-tile--launch_h');
    await expect(tile.locator('.kiosk-tile__badge')).toHaveText('Ziel fehlt');
    await tile.tap();
    await expect(page.locator('.kiosk-live')).toHaveCount(0);
    await expect(page.locator('.range-footer').getByRole('button', { name: 'Ausrichten' })).toHaveAttribute('aria-pressed', 'true');
  });

  test('no badge once a target line is set', async ({ page }) => {
    await showKiosk(page, { ...SHOT, launch_angle_horizontal_source: 'camera_assisted_experimental' });
    await expect(page.locator('.kiosk-tile__badge')).toHaveCount(0);
    await expect(page.locator('.kiosk-tile--launch_h')).not.toHaveAttribute('type', 'button');
  });
});

/**
 * The impact tile's face (the drawn viewBox inside its svg, `meet`) and word: the face inside the
 * tile's content box and above the word, the word's glyphs inside the tile, and both big enough.
 */
function impactFit(page: Page) {
  return page.locator('.kiosk-tile--impact').evaluate((tile) => {
    const svg = tile.querySelector('svg.impact-face') as SVGSVGElement;
    const box = svg.getBoundingClientRect();
    const view = svg.viewBox.baseVal;
    const scale = Math.min(box.width / view.width, box.height / view.height);
    const face = {
      left: box.left + (box.width - view.width * scale) / 2,
      right: box.right - (box.width - view.width * scale) / 2,
      top: box.top + (box.height - view.height * scale) / 2,
      bottom: box.bottom - (box.height - view.height * scale) / 2,
    };
    const style = getComputedStyle(tile);
    const tileBox = tile.getBoundingClientRect();
    const inner = {
      left: tileBox.left + parseFloat(style.paddingLeft) - 1,
      right: tileBox.right - parseFloat(style.paddingRight) + 1,
      top: tile.querySelector('.kiosk-tile__title')!.getBoundingClientRect().bottom - 1,
      bottom: tileBox.bottom - parseFloat(style.borderBottomWidth) + 0.5,
    };
    const value = tile.querySelector('.kiosk-tile__value')!;
    const range = document.createRange();
    range.selectNodeContents(value);
    const word = range.getBoundingClientRect();
    const tileCentre = (inner.left + inner.right) / 2;
    return {
      faceHeight: face.bottom - face.top,
      faceInside: face.left >= inner.left && face.right <= inner.right && face.top >= inner.top && face.bottom <= inner.bottom,
      faceAboveWord: face.bottom <= word.top + 1,
      faceCentred: Math.abs((face.left + face.right) / 2 - tileCentre) <= 2,
      wordCentred: Math.abs((word.left + word.right) / 2 - tileCentre) <= 2,
      wordPx: parseFloat(getComputedStyle(value).fontSize),
      titlePx: parseFloat(getComputedStyle(tile.querySelector('.kiosk-tile__title')!).fontSize),
      mark: svg.getAttribute('data-mark'),
    };
  });
}

test.describe('kiosk Live impact tile', () => {
  test.use({ hasTouch: true });

  for (const viewport of KIOSK_VIEWPORTS) {
    for (const [size, slots] of [
      ['large', ['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve']],
      ['small', ['carry', 'ball_speed', 'launch_v', 'launch_h', 'impact']],
    ] as const) {
      test(`${viewport.width}x${viewport.height} ${size} slot: the face over the zone word, inside the tile`, async ({ page }) => {
        await page.setViewportSize(viewport);
        for (const [shot, locale, word, mark] of [
          [IMPACT, 'de', 'Spitze', 'dot'],
          [IMPACT_OPEN, 'en', 'uncertain', 'ring'],
          [{ ...IMPACT, club: 'driver', impact_offset_mm: -10, impact_height_mm: 24, impact_zone: 'heel' }, 'es', 'Talón', 'dot'],
        ] as const) {
          await showKiosk(page, shot, { slots: [...slots], locale });
          const tile = page.locator('.kiosk-tile--impact');
          await expect(tile).toHaveClass(new RegExp(`kiosk-tile--${size}`));
          await expect(tile.locator('.kiosk-tile__approx')).toHaveText('≈');
          await expect(tile.locator('.kiosk-tile__value')).toHaveText(word);
          await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          const fit = await impactFit(page);
          expect(fit.mark, word).toBe(mark);
          expect(fit, word).toMatchObject({ faceInside: true, faceAboveWord: true, faceCentred: true, wordCentred: true });
          // Read from 2 m: the word at least the tile title's size, the face at least as tall as the word.
          expect(fit.wordPx, word).toBeGreaterThanOrEqual(fit.titlePx - 0.5);
          if (size === 'large') expect(fit.wordPx, word).toBeGreaterThan(fit.titlePx * 1.15);
          expect(fit.faceHeight, word).toBeGreaterThanOrEqual(fit.wordPx);
          expect((await valueFit(page)).filter((entry) => entry.clipped)).toEqual([]);
        }
      });
    }
  }

  test('no impact point: the empty face and —', async ({ page }) => {
    await page.setViewportSize({ width: 1024, height: 600 });
    await showKiosk(page, { ...IMPACT, impact_offset_mm: null, impact_zone: null }, { slots: ['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve'] });
    await expect(page.locator('.kiosk-tile--impact .kiosk-tile__value')).toHaveText('—');
    expect((await impactFit(page)).mark).toBe('none');
    await expect(page.locator('.kiosk-tile--impact .kiosk-tile__title')).toHaveText('Treffpunkt ≈');
  });
});
