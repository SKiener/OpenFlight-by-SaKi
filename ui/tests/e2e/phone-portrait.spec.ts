import { test, expect, type Page } from '@playwright/test';
import {
  brokenWords,
  chooseMenuItem,
  clippedText,
  crowdedTileParts,
  menuBack,
  mockSessionHistory,
  openMenuArea,
  overflowingLiveMetricValues,
  PHONE_VIEWPORTS,
  smallTargets,
} from './helpers';
import { phoneOrigin } from './phoneFixture';

/**
 * Phone portrait in German (the longest labels) with fixture data: the Range
 * stores are filled in the page, so every check sees the same shots. Sizes are
 * Safari's visible area (helpers) plus a full iPhone screen (home-screen app).
 */
const PORTRAIT = [...PHONE_VIEWPORTS, { width: 390, height: 844 }] as const;
const SMALLEST = PHONE_VIEWPORTS[0];

const DRIVER = {
  ball_speed_mph: 168.4,
  club_speed_mph: 113.8,
  smash_factor: 1.48,
  estimated_carry_yards: 287,
  carry_range: [273, 301],
  club: 'driver',
  timestamp: '2026-10-07T09:12:00Z',
  peak_magnitude: 100,
  launch_angle_vertical: 10.4,
  launch_angle_horizontal: -8,
  launch_angle_confidence: 0.82,
  angle_source: 'estimated',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 2650,
  spin_confidence: 0.9,
  spin_quality: 'high',
  spin_source: 'measured',
  carry_spin_adjusted: 289,
  carry_spin_rpm: 2650,
  carry_spin_source: 'measured',
  profile_id: 'phone',
  profile_name: 'Golfer',
};

/** Long German club names, a typical wedge spin and a 3-digit shot count. */
const CLUBS = ['pw', '7-iron', 'lw', 'mini-driver', 'wedge-58', 'driver', 'sw', '3-hybrid'];
const MANY_SHOTS = Array.from({ length: 120 }, (_, index) => ({
  ...DRIVER,
  club: CLUBS[index % CLUBS.length],
  timestamp: new Date(Date.parse(DRIVER.timestamp) + index * 60_000).toISOString(),
  carry_spin_rpm: 10_500,
  carry_spin_source: 'club_typical',
}));

const swing = (label: string) => ({
  ...DRIVER,
  mode: 'swing-speed',
  club: 'Swing Speed',
  training_implement: 'rypstick-3w-cw',
  training_implement_label: label,
  club_speed_mph: 112.4,
  swing_speed_reading_count: 14,
  swing_speed_trigger_mph: 60,
  swing_speed_duration_ms: 182,
});

const CAMERA = {
  available: true,
  enabled: true,
  running: true,
  armed: true,
  width: 640,
  height: 400,
  fps: 300,
  capture_mode: 'full-300',
  capture_modes: [
    { id: 'full-300', width: 640, height: 400, fps: 300 },
    { id: 'strip-500', width: 640, height: 200, fps: 500 },
  ],
  exposure_us: 400,
  gain: 8,
  raw_crop_adjustable: false,
  pre_ms: 150,
  post_ms: 50,
  pre_frames: 45,
  post_frames: 15,
};
const PREVIEW_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400"><rect width="640" height="400" fill="#555"/></svg>';

/**
 * a•space's digits are proportional ("4" is the widest, "1" the narrowest): the widest values
 * a shot list shows are a ball of 244,4 km/h (244.4 mph) and a typical-spin carry of ≈244 m (yd).
 */
const widestShots = (ballMph: number, carryYards: number) =>
  MANY_SHOTS.map((shot) => ({
    ...shot,
    ball_speed_mph: ballMph,
    carry_spin_adjusted: carryYards,
    estimated_carry_yards: carryYards,
  }));
const WIDEST_SHOTS = {
  metric: widestShots(244.4 / 1.609344, 244 / 0.9144),
  imperial: widestShots(244.4, 244),
} as const;

interface Fixture {
  shots?: object[];
  mode?: string;
  voice?: boolean;
  units?: 'metric' | 'imperial';
}

async function showPhone(
  page: Page,
  { shots = [DRIVER], mode = 'rolling-buffer', voice = false, units = 'metric' }: Fixture = {}
) {
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/camera/preview.jpg*', (route) =>
    route.fulfill({ contentType: 'image/svg+xml', body: PREVIEW_SVG })
  );
  await page.addInitScript((voice) => {
    localStorage.setItem('openflight.locale:v1', 'de');
    if (voice) {
      const metrics = ['carry', 'ball_speed', 'club_speed', 'smash', 'launch_v', 'spin'];
      localStorage.setItem('openflight.voice-callouts:v1', JSON.stringify({ enabled: true, metrics, language: 'de' }));
    }
  }, voice);
  // A phone's host: 127.0.0.1 is the kiosk, with its own big Live layout.
  await page.goto(`${phoneOrigin(test.info().project.use.baseURL!)}/`);
  await page.locator('.range-app').waitFor();
  await page.evaluate(
    async ({ shots, mode, camera, units }) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      (await load('services/socketService')).socketService.disconnect();
      (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
        profiles: [{ id: 'phone', name: 'Golfer', settings: { unit_system: units } }],
        active_profile_id: 'phone',
      });
      (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
      (await load('stores/useDebugStore')).useDebugStore.setState((state: { triggerStatus: object }) => ({
        triggerStatus: { ...state.triggerStatus, mode, radar_connected: true },
      }));
      (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings(camera);
      (await load('stores/useShotStore')).useShotStore.getState().setShots(shots);
      await document.fonts.ready;
    },
    { shots, mode, camera: CAMERA, units }
  );
}

async function closePicker(page: Page) {
  await page.locator('.picker-overlay__close').tap();
  await expect(page.locator('.picker-overlay')).toHaveCount(0);
}

async function openMenuItem(page: Page, name: string) {
  await page.getByRole('button', { name: 'Menü öffnen' }).tap();
  await chooseMenuItem(page, name, { tap: true });
}

for (const viewport of PORTRAIT) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de)`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true, timezoneId: 'Europe/Berlin' });

    test('Range tiles keep label, number and caption apart, and the heading keeps profile and time', async ({
      page,
    }) => {
      await showPhone(page);
      await closePicker(page);

      await expect(page.locator('.range-secondary-grid .metric-card')).toHaveCount(3);
      expect(await crowdedTileParts(page, '.range-tile .metric-card')).toEqual([]);
      for (const tile of await page.locator('.range-secondary-grid .range-tile').all()) {
        expect((await tile.boundingBox())!.height).toBeGreaterThanOrEqual(70);
      }
      await expect(page.locator('.range-shot-heading > span')).toHaveText('Golfer · 11:12');
      expect(await clippedText(page, '.range-shot-heading > span, .range-shot-heading h1')).toEqual([]);

      await page.evaluate(async () => {
        const path = '/src/stores/useShotStore.ts';
        const { useShotStore } = await import(/* @vite-ignore */ path);
        const shot = useShotStore.getState().shots[0];
        useShotStore.getState().setShots([{ ...shot, spin_axis_deg: -12.5, spin_axis_class: 'draw' }]);
      });
      // A shot with a spin axis keeps the same three tiles: no screen shows the axis.
      await expect(page.locator('.range-secondary-grid .metric-card')).toHaveCount(3);
      await expect(page.locator('[data-metric="spin_axis"]')).toHaveCount(0);
      await expect(page.locator('.range-secondary-grid')).not.toContainText(/Spinachse|Draw/);
      expect(await crowdedTileParts(page, '.range-tile .metric-card')).toEqual([]);
    });

    test('an uncalibrated radar start direction says so and still fits its tile', async ({ page }) => {
      await showPhone(page);
      await closePicker(page);
      await page.evaluate(async () => {
        const path = '/src/stores/useShotStore.ts';
        const { useShotStore } = await import(/* @vite-ignore */ path);
        const shot = useShotStore.getState().shots[0];
        useShotStore.getState().setShots([
          {
            ...shot,
            angle_source: 'radar',
            launch_angle_horizontal_source: 'radar',
            launch_angle_horizontal_confidence: 0.95,
          },
        ]);
      });

      await expect(page.locator('.range-tile--launch_h .metric-card__confidence-label')).toHaveText('nicht kalibriert');
      expect(await crowdedTileParts(page, '.range-tile .metric-card')).toEqual([]);
    });

    test('the shot list shows number, club, ball, carry and a delete label that fits', async ({ page }) => {
      await showPhone(page, { shots: MANY_SHOTS });
      await closePicker(page);
      await openMenuItem(page, 'Schläge');

      await expect(page.locator('.shots-panel__row')).toHaveCount(MANY_SHOTS.length);
      // "LÖSCHEN" ran over the button's border.
      await expect(page.locator('.shots-panel__delete').first()).toHaveText('Löschen');
      const rows = '.shots-panel__row-group:nth-child(-n + 8)';
      expect(await clippedText(page, `${rows} .shots-panel__delete`)).toEqual([]);
      const headings = await page.locator('.shots-panel__columns > span:visible').allTextContents();
      // "Flugweite" carries a soft hyphen (labelBreaks.ts), invisible unless it breaks there.
      expect(headings.filter(Boolean).map((text) => text.replaceAll('­', ''))).toEqual(['Schlag', 'Ball', 'Flugweite']);
      expect(await clippedText(page, '.shots-panel__columns > span')).toEqual([]);
      await expect(page.locator('.shots-panel__index').first()).toHaveText('120');
      await expect(page.locator('.shots-panel__profile-club').first()).toHaveText('Hybrid 3');
      expect(await clippedText(page, `${rows} .shots-panel__row-main > span`)).toEqual([]);
      // Club names wrap between words only: "Pitching / Wedge", never "Eisen / 7".
      expect(await brokenWords(page, `${rows} .shots-panel__profile-club`)).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);

      await page.locator('.shots-panel__row-main').first().tap();
      expect(await smallTargets(page, '.shots-panel__field input, .shots-panel__field select')).toEqual([]);
    });

    for (const units of ['metric', 'imperial'] as const) {
      test(`the widest ball and carry (${units}) stay inside their columns beside the club and Löschen`, async ({
        page,
      }) => {
        await showPhone(page, { shots: WIDEST_SHOTS[units], units });
        await closePicker(page);
        await openMenuItem(page, 'Schläge');

        const rows = '.shots-panel__row-group:nth-child(-n + 8)';
        await expect(page.locator(`${rows} .shots-panel__value`).first()).toHaveText('244,4');
        await expect(page.locator(`${rows} .shots-panel__value--accent`).first()).toHaveText('≈244');
        // Inside its own track is clear of the neighbouring column (and of the row buttons).
        expect(await clippedText(page, `${rows} .shots-panel__row-main > span`)).toEqual([]);
        expect(await brokenWords(page, `${rows} .shots-panel__profile-club`)).toEqual([]);
        expect(await clippedText(page, `${rows} .shots-panel__delete`)).toEqual([]);
        const sizes = await page
          .locator(`${rows} .shots-panel__value`)
          .evaluateAll((values) => values.map((value) => parseFloat(getComputedStyle(value).fontSize)));
        // Never below 0.8125rem (1rem is 18 px on a phone).
        expect(Math.min(...sizes)).toBeGreaterThanOrEqual(14.6);
      });

      test(`the session stats keep "Ø Schlägertempo" and an average of 244,4 (${units}) inside their tiles`, async ({
        page,
      }) => {
        await showPhone(page, { shots: WIDEST_SHOTS[units], units });
        await closePicker(page);
        await openMenuItem(page, 'Statistik');

        const cards = '.stats-panel__grid .metric-card';
        await expect(page.locator(`${cards} .metric-card__value`).nth(1)).toHaveText('244,4');
        expect(await clippedText(page, `${cards} .metric-card__label`)).toEqual([]);
        expect(await crowdedTileParts(page, cards)).toEqual([]);
        expect(await overflowingLiveMetricValues(page, cards)).toEqual([]);
      });
    }

    test('training implements keep their names inside fingertip-sized tiles', async ({ page }) => {
      await showPhone(page, { mode: 'swing-speed', shots: [] });

      await expect(page.locator('.picker-overlay')).toHaveAttribute('aria-label', 'Trainingsgerät wählen');
      for (const tab of ['TheStack', 'Rypstick']) {
        await page.locator('.picker-overlay__tabs .panel-action', { hasText: new RegExp(`^${tab}$`, 'i') }).tap();
        expect(await clippedText(page, '.picker-overlay__option')).toEqual([]);
        expect(await clippedText(page, '.picker-overlay__tabs .panel-action')).toEqual([]);
        expect(await smallTargets(page, '.picker-overlay__option')).toEqual([]);
      }
      await expect(page.locator('.picker-overlay__option').last()).toHaveText('Rypstick 3 Weights + Counterweight');
    });
  });
}

for (const viewport of [SMALLEST, PHONE_VIEWPORTS[2], { width: 390, height: 844 }]) {
  test.describe(`phone portrait ${viewport.width}×${viewport.height} (de), more views`, () => {
    test.use({ viewport, isMobile: true, hasTouch: true });

    test('a long implement name does not shrink the swing-speed numbers', async ({ page }) => {
      await showPhone(page, { mode: 'swing-speed', shots: [swing('Stack')] });
      await closePicker(page);
      const tiles = page.locator('.live-panel__grid .metric-card');
      await expect(tiles).toHaveCount(5);
      const lastSwing = tiles.first().locator('.metric-card__value');
      const size = () => lastSwing.evaluate((node) => parseFloat(getComputedStyle(node).fontSize));
      const short = await size();

      await page.evaluate(async (shot) => {
        const path = '/src/stores/useShotStore.ts';
        const { useShotStore } = await import(/* @vite-ignore */ path);
        useShotStore.getState().setShots([shot]);
      }, swing('Rypstick 3 Weights + Counterweight'));
      await expect(tiles.last().locator('.metric-card__value')).toHaveText('Rypstick 3 Weights + Counterweight');
      await expect.poll(size).toBeGreaterThanOrEqual(short - 0.5);
      expect(await crowdedTileParts(page, '.live-panel__grid .metric-card')).toEqual([]);
      expect(await clippedText(page, '.live-panel__grid .metric-card__value')).toEqual([]);
    });

    test('session history keeps every button on screen and the table inside the dialog', async ({ page }) => {
      await showPhone(page);
      const session = { id: 's1', started_at: '2026-10-04T10:00:00Z', shot_count: 3, clubs: ['7-iron', 'pw'] };
      const saved = ['7-iron', 'pw', 'mini-driver'].map((club, index) => ({
        shot_number: index + 1,
        timestamp: session.started_at,
        club,
        profile_name: 'Golfer',
        ball_speed_mph: 112.4,
        club_speed_mph: 84,
        estimated_carry_yards: 160,
        carry_spin_adjusted: 158,
        launch_angle_vertical: 18.5,
        spin_rpm: 6500,
        carry_spin_rpm: 6500,
        carry_spin_source: 'measured',
        camera_replay: { id: `r${index}`, frame_count: 99, trigger_frame: 73, playback_fps: 60, duration_seconds: 1.6 },
      }));
      await mockSessionHistory(page, [{ summary: session, shots: saved }]);
      await closePicker(page);
      await page.locator('.range-footer nav').getByRole('button', { name: 'Verlauf' }).tap();
      const dialog = page.getByRole('dialog', { name: 'Session-Verlauf' });
      await dialog.locator('.session-history__session').tap();
      await expect(dialog.locator('tbody tr')).toHaveCount(3);

      const unreachable = await dialog.locator('.session-history__header button').evaluateAll((buttons) =>
        buttons
          .filter((button) => {
            const box = button.getBoundingClientRect();
            const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
            return box.left < 0 || box.right > window.innerWidth || !button.contains(hit);
          })
          .map((button) => button.textContent)
      );
      expect(unreachable).toEqual([]);
      expect(await clippedText(page, '.session-history__header button')).toEqual([]);
      const scroller = dialog.locator('.session-history__scroll');
      expect(await scroller.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
      // "Eisen 7" and "Mini Driver" wrap between words only ("Eise / n 7" before).
      expect(await brokenWords(page, '.session-history tbody td:nth-child(2)')).toEqual([]);
      expect(await clippedText(page, '.session-history tbody td')).toEqual([]);
      await expect(dialog.locator('.session-history__replay').first()).toBeInViewport({ ratio: 1 });

      await dialog.getByRole('button', { name: 'Schließen', exact: true }).tap();
      await expect(page.locator('.session-history')).toHaveCount(0);
    });

    // a•space digits are proportional: "244,4" is a third wider than "111,1". The widest realistic row,
    // in km/h and m and in mph and yds, keeps the table inside the dialog and every value whole.
    for (const [units, wide] of [
      ['metric', { ball: 244.4 / 1.60934, club: 144.4 / 1.60934, carry: 244.4 / 0.9144 }],
      ['imperial', { ball: 244.4, club: 144.4, carry: 244.4 }],
    ] as const) {
      test(`session history keeps the widest values (${units}) whole and inside the dialog`, async ({ page }) => {
        await showPhone(page);
        await page.evaluate(async (units) => {
          const path = '/src/stores/useUnitPreferenceStore.ts';
          const { useUnitPreferenceStore } = await import(/* @vite-ignore */ path);
          useUnitPreferenceStore.getState().setUnitSystem(units);
        }, units);
        const session = { id: 's1', started_at: '2026-10-04T10:00:00Z', shot_count: 2, clubs: ['3-hybrid', 'pw'] };
        const saved = ['3-hybrid', 'pw'].map((club, index) => ({
          shot_number: index + 1,
          timestamp: session.started_at,
          club,
          profile_name: 'Golfer',
          ball_speed_mph: wide.ball,
          club_speed_mph: wide.club,
          estimated_carry_yards: wide.carry,
          carry_spin_adjusted: wide.carry,
          launch_angle_vertical: 24.4,
          spin_rpm: 12_444,
          carry_spin_rpm: 12_444,
          carry_spin_source: 'club_typical',
          camera_replay: { id: `r${index}`, frame_count: 99, trigger_frame: 73, playback_fps: 60, duration_seconds: 1.6 },
        }));
        await mockSessionHistory(page, [{ summary: session, shots: saved }]);
        await closePicker(page);
        await page.locator('.range-footer nav').getByRole('button', { name: 'Verlauf' }).tap();
        const dialog = page.getByRole('dialog', { name: 'Session-Verlauf' });
        await dialog.locator('.session-history__session').tap();
        await expect(dialog.locator('tbody tr')).toHaveCount(2);
        // Ball 244,4 and carry ≈244 in either unit: the fixture is the widest row, not a rounding accident.
        await expect(dialog.locator('tbody tr').first().locator('td').nth(2)).toHaveText('244,4');
        await expect(dialog.locator('tbody tr').first().locator('td').nth(4)).toHaveText('≈244');

        const scroller = dialog.locator('.session-history__scroll');
        expect(await scroller.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
        expect(await brokenWords(page, '.session-history tbody td')).toEqual([]);
        expect(await clippedText(page, '.session-history tbody td')).toEqual([]);
        await expect(dialog.locator('.session-history__replay').first()).toBeInViewport({ ratio: 1 });
      });
    }

    test('the TI radar alert stays a compact band and the numbers keep their tiles', async ({ page }) => {
      await showPhone(page);
      await closePicker(page);
      await page.evaluate(async () => {
        const path = '/src/stores/useDebugStore.ts';
        const { useDebugStore } = await import(/* @vite-ignore */ path);
        const reason = 'Radar-Rohdaten unvollständig (UART-Zeitüberschreitung nach 180 ms)';
        useDebugStore.setState({ iwr6843Alert: { state: 'error', reason } });
      });
      const alert = page.locator('.iwr-alert');
      await expect(alert).toContainText('TI-Radaraufnahme fehlgeschlagen');
      expect((await alert.boundingBox())!.height).toBeLessThanOrEqual(96);
      expect(await crowdedTileParts(page, '.range-tile .metric-card')).toEqual([]);
      expect(await smallTargets(page, '.iwr-alert button')).toEqual([]);

      await page.getByRole('button', { name: 'TI-Radarhinweis ausblenden' }).tap();
      await expect(alert).toHaveCount(0);
    });

    test('My clubs keeps every club name inside a fingertip-sized button', async ({ page }) => {
      await showPhone(page);
      await closePicker(page);
      await openMenuItem(page, 'Meine Schläger');
      await expect(page.locator('.bag-settings__grid button')).toHaveCount(25);
      expect(await clippedText(page, '.bag-settings__grid button')).toEqual([]);
      expect(await smallTargets(page, '.bag-settings__grid button')).toEqual([]);
      await expect(page.getByRole('button', { name: 'Auswahl speichern' })).toBeInViewport({ ratio: 1 });
    });

    test('Align shows the camera picture, and a finger on the settings scrolls them', async ({ page, context }) => {
      await showPhone(page);
      await closePicker(page);
      await page.locator('.range-footer nav').getByRole('button', { name: 'Ausrichten' }).tap();
      await expect(page.locator('.camera-feed__stream img')).toBeVisible();

      const stream = (await page.locator('.camera-feed__stream').boundingBox())!;
      expect(stream.height).toBeGreaterThan(100);
      expect(stream.x).toBeGreaterThanOrEqual(0);
      expect(stream.x + stream.width).toBeLessThanOrEqual(viewport.width);
      expect(await clippedText(page, '.camera-feed__eyebrow, .camera-feed__title')).toEqual([]);
      const chip = (await page.locator('.camera-feed__exposure-quality').boundingBox())!;
      const eyebrow = (await page.locator('.camera-feed__eyebrow').boundingBox())!;
      expect(chip.y).toBeGreaterThanOrEqual(eyebrow.y + eyebrow.height - 1);
      expect(await smallTargets(page, '.camera-settings input[type="range"], .camera-settings button:visible')).toEqual(
        []
      );

      const workspace = page.locator('.camera-feed__workspace');
      const settings = (await page.locator('.camera-settings').boundingBox())!;
      const footer = (await page.locator('.range-footer').boundingBox())!;
      const cdp = await context.newCDPSession(page);
      const x = settings.x + settings.width / 2;
      const y = Math.min(settings.y + settings.height, footer.y) - 30;
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
      for (let step = 1; step <= 10; step++) {
        await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x, y: y - step * 15 }] });
      }
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
      await expect.poll(() => workspace.evaluate((node) => node.scrollTop)).toBeGreaterThan(50);
    });

    test('clearing the session keeps both labels inside, and the club chips are fingertip-sized', async ({ page }) => {
      await showPhone(page, { shots: MANY_SHOTS.slice(0, 3) });
      await closePicker(page);
      await openMenuItem(page, 'Statistik');
      await page.getByRole('button', { name: 'Session leeren' }).tap();
      await expect(page.locator('.clear-session-modal')).toBeVisible();
      expect(await clippedText(page, '.clear-session-modal__actions .panel-action')).toEqual([]);

      await page.getByRole('button', { name: 'Abbrechen' }).tap();
      expect(await smallTargets(page, '.stats-panel__chips .panel-chip')).toEqual([]);
    });

    test('the profile name dialog keeps its labels inside and phone-sized keys', async ({ page }) => {
      await showPhone(page);
      await closePicker(page);
      await openMenuItem(page, 'Profile');
      await page.getByRole('button', { name: 'Golfer umbenennen' }).tap();
      await expect(page.locator('.profile-name-modal')).toBeVisible();

      expect(await clippedText(page, '.profile-name-modal__actions .panel-action')).toEqual([]);
      expect(await smallTargets(page, '.profile-name-modal__close')).toEqual([]);
      // Key rows sit at the bottom like a phone keyboard instead of stretching over the screen.
      for (const row of await page.locator('.profile-name-modal__key-row').all()) {
        expect((await row.boundingBox())!.height).toBeLessThanOrEqual(56);
      }
      const actions = (await page.locator('.profile-name-modal__actions').boundingBox())!;
      expect(actions.y + actions.height).toBeLessThanOrEqual(viewport.height);
    });

    test('the shutdown question stays clear of the screen edges', async ({ page }) => {
      await showPhone(page);
      await closePicker(page);
      await page.getByRole('button', { name: 'Menü öffnen' }).tap();
      await page.locator('.menu-sheet__shutdown').tap();

      const dialog = (await page.locator('.shutdown-dialog').boundingBox())!;
      expect(dialog.x).toBeGreaterThanOrEqual(8);
      expect(dialog.x + dialog.width).toBeLessThanOrEqual(viewport.width - 8);
      expect(await clippedText(page, '.shutdown-dialog button')).toEqual([]);
    });
  });
}

test.describe('phone portrait menu (de)', () => {
  test.use({ viewport: SMALLEST, isMobile: true, hasTouch: true });

  test('shows no section heading without content, and every button fits, on each menu page', async ({ page }) => {
    await showPhone(page, { voice: true });
    await closePicker(page);
    await page.getByRole('button', { name: 'Menü öffnen' }).tap();

    const fits = async (where: string) => {
      const emptySections = await page.locator('.menu-sheet__section').evaluateAll((sections) =>
        sections
          .filter((section) => section.getBoundingClientRect().height > 0)
          .filter((section) => [...section.children].every((child) => child.matches('.menu-sheet__section-title')))
          .map((section) => section.textContent)
      );
      expect(emptySections, where).toEqual([]);
      expect(await clippedText(page, '.menu-sheet button'), where).toEqual([]);
      expect(await smallTargets(page, '.menu-sheet button, .menu-sheet select'), where).toEqual([]);
    };

    // The phone has no Live tiles to configure.
    const areas = await page.locator('.menu-sheet__area-name').allTextContents();
    expect(areas).toEqual(['Anzeige', 'Ansage', 'Kalibrieren', 'Auswertung', 'System']);
    await fits('start page');
    for (const area of areas) {
      await openMenuArea(page, area, { tap: true });
      if (area === 'Ansage') await expect(page.locator('.menu-sheet__select')).toBeVisible();
      await fits(area);
      await menuBack(page, { tap: true });
    }
  });
});
