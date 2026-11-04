import { expect, test, type Page } from '@playwright/test';
import {
  canvases,
  clippedText,
  clothOf,
  covered,
  expectLandedAtLatestCarry,
  expectOverlaysInside,
  fillTexts,
  footerButton,
  freeArea,
  gotoApp,
  greenHighlighted,
  KIOSK_VIEWPORTS,
  latestCarryYards,
  mockSessionHistory,
  onScreen,
  openApp,
  openMenuArea,
  openRange,
  outsideScreen,
  overlapping,
  rangeButton,
  resetSession,
  resetUnits,
  savedShot,
  sceneAndCameraSeen,
  LANDED_TIMEOUT_MS,
  SCENE_TIMEOUT_MS,
  setClub,
  settled,
  showDemoBand,
  showSavedSession,
  signLabel,
  smallTargets,
  spyOnFillText,
  spyOnRangeWorld,
  storedTarget,
  targetLabels,
  spyOnRangeCamera,
  waitForEvent,
  withControlSocket,
  worldTurn,
  type MockedSession,
  type Point,
} from './helpers';
import { greenBearing, rotateAboutTee } from '../../src/virtual-range/aim';
import { GREENS } from '../../src/virtual-range/layout';

interface ShotPayload {
  shot: { ball_speed_mph: number };
}

interface RangeSpy {
  frames: number;
  contexts: WebGL2RenderingContext[];
  carry: number[];
}

/** The parts of three.js's Vector3 and PerspectiveCamera the framing checks use. */
interface SceneVector extends Point {
  add(other: Point): SceneVector;
  applyMatrix4(matrix: unknown): SceneVector;
  setFromMatrixPosition(matrix: unknown): SceneVector;
  clone(): SceneVector;
  project(camera: SceneCamera): SceneVector;
  set(x: number, y: number, z: number): SceneVector;
  distanceTo(other: Point): number;
}

interface SceneCamera {
  fov: number;
  position: SceneVector;
  clone(): SceneCamera;
  getWorldDirection(target: SceneVector): SceneVector;
  lookAt(x: number, y: number, z: number): void;
  updateMatrixWorld(): void;
}

declare global {
  interface Window {
    __rangeSpy: RangeSpy;
    __rangeCamera?: SceneCamera;
    __canvasPresses?: number;
  }
}

test.use({ hasTouch: true, viewport: { width: 1024, height: 600 } });

async function installSpy(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const spy: RangeSpy = { frames: 0, contexts: [], carry: [] };
    window.__rangeSpy = spy;

    const requestFrame = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (callback) =>
      requestFrame((time) => {
        spy.frames += 1;
        callback(time);
      });

    const { getContext } = HTMLCanvasElement.prototype;
    HTMLCanvasElement.prototype.getContext = function (
      this: HTMLCanvasElement,
      ...args: Parameters<typeof getContext>
    ) {
      const context = getContext.apply(this, args);
      if (args[0] === 'webgl2' && context) spy.contexts.push(context as WebGL2RenderingContext);
      return context;
    } as typeof getContext;
  });
}

const frames = (page: Page): Promise<number> => page.evaluate(() => window.__rangeSpy.frames);
const contextsLost = (page: Page): Promise<boolean[]> =>
  page.evaluate(() => window.__rangeSpy.contexts.map((context) => context.isContextLost()));

async function reloadApp(page: Page): Promise<void> {
  await page.reload();
  await page.getByRole('button', { name: 'Close Select club' }).tap();
}

function simulateShot(page: Page): Promise<ShotPayload['shot']> {
  return withControlSocket(async (socket) => {
    const arrived = waitForEvent<ShotPayload>(socket, 'shot');
    await page.getByRole('button', { name: 'Simulate shot' }).tap();
    return (await arrived).shot;
  });
}

function recordCarry(page: Page): Promise<void> {
  return page.evaluate(() => {
    window.__rangeSpy.carry.splice(0);
    new MutationObserver(() => {
      const text = document.querySelector('.virtual-range__stat--carry .virtual-range__value')?.textContent;
      const value = parseInt(text ?? '', 10);
      if (Number.isFinite(value)) window.__rangeSpy.carry.push(value);
    }).observe(document.querySelector('.virtual-range')!, { childList: true, characterData: true, subtree: true });
  });
}

const carrySeries = (page: Page): Promise<number[]> => page.evaluate(() => window.__rangeSpy.carry);

/**
 * The widest realistic figures, built from 4s: a•space's digits are proportional and "4" is the widest
 * (0.99 em; "8" 0.80 em). A typical-spin carry carries "≈ ", and the speed is in km/h or mph alike.
 */
function showWidestFigures(page: Page): Promise<void> {
  return page.evaluate(() => {
    const figure = (kind: string) =>
      document.querySelector(`.virtual-range__stat--${kind} .virtual-range__value`)!.firstChild!;
    figure('carry').textContent = '≈ 244';
    for (const kind of ['total', 'target']) figure(kind).textContent = '244';
    figure('speed').textContent = '244,4';
  });
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

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`Range area at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('the Range button opens the range and a simulated shot flies', async ({ page }) => {
      await installSpy(page);
      await openApp(page);
      await expect(page.locator('.range-footer nav button')).toHaveText([
        'Live',
        'Range',
        'History',
        'Align',
        'Device',
      ]);
      await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'false');

      await rangeButton(page).tap();
      await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
      await expect(footerButton(page, 'Live')).toHaveAttribute('aria-pressed', 'false');
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      await expect(page.getByText('Hit a shot – it flies here.')).toBeVisible();
      await recordCarry(page);

      const shot = await simulateShot(page);
      const bar = page.locator('.virtual-range__bar');
      await expect(bar.locator('.virtual-range__club')).toHaveText('Driver');
      const ballSpeed = await page.evaluate(async (speed) => {
        const units = '/src/utils/units.ts';
        const { formatSpeed } = await import(/* @vite-ignore */ units);
        return formatSpeed(speed, 'imperial');
      }, shot.ball_speed_mph);
      await expect(bar).toContainText(`${ballSpeed}mph`);
      await expectOverlaysInside(page);
      // The mock backend's button must not sit on the figures it is there to produce.
      expect(await overlapping(page, '.simulate-bubble', '.virtual-range__bar')).toEqual([]);

      const carry = await expectLandedAtLatestCarry(page);
      const series = await carrySeries(page);
      expect(series.at(-1), `${series}`).toBe(carry);
      expect(Math.max(...series), `${series}`).toBeLessThanOrEqual(carry);
      expect(
        series.some((value) => value > 0 && value < carry * 0.8),
        `a count-up to ${carry}: ${series}`
      ).toBe(true);
      await expectOverlaysInside(page);
      await showWidestFigures(page);
      await expectOverlaysInside(page);
    });

    test('the five footer buttons and the replay button fit in German, on Live and over the range', async ({
      page,
    }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await gotoApp(page);
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();

      await expect(page.locator('.range-footer nav button')).toHaveText([
        'Live',
        'Range',
        'Verlauf',
        'Ausrichten',
        'Gerät',
      ]);
      for (const area of ['Live', 'Range']) {
        if (area === 'Range') {
          await rangeButton(page).tap();
          await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
        }
        expect(await outsideScreen(page, '.range-footer button'), area).toEqual([]);
        expect(await clippedText(page, '.range-footer button', 0), area).toEqual([]);
        expect(await smallTargets(page, '.range-footer button'), area).toEqual([]);
        expect(await covered(page, '.range-footer button'), area).toEqual([]);
      }
    });
  });
}

/** A 3.5 s flight in the wire format: [t, downrange, right, up], yards. */
const wireFlight = (carryYards: number, rightYards: number): number[][] =>
  Array.from({ length: 15 }, (_, i) => {
    const u = i / 14;
    return [3.5 * u, carryYards * u, rightYards * u, 120 * u * (1 - u)];
  });

/** Feeds the app's socket handlers: 'shot' as an OPS emit with camera data pending, or its 'shot_update'. */
function deliver(page: Page, event: 'shot' | 'shot_update', shot: Record<string, unknown>): Promise<void> {
  return page.evaluate(
    async ({ event, shot }) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      const { handleShotMessage, handleShotUpdate } = await load('services/handleShotMessage');
      const { useProfileStore } = await load('stores/useProfileStore');
      const message = { shot: { ...shot, profile_id: useProfileStore.getState().activeProfileId }, stats: {} };
      if (event === 'shot') handleShotMessage({ ...message, pending: { camera: true } });
      else handleShotUpdate(message);
    },
    { event, shot }
  );
}

test('shots still being processed wait off the range, then each flies once with its final numbers', async ({
  page,
}) => {
  await installSpy(page);
  await openRange(page);
  await spyOnFillText(page);
  await recordCarry(page);
  const bar = page.locator('.virtual-range__bar');
  const opsShot = (timestamp: string, estimatedCarry: number) => ({
    timestamp,
    club: 'driver',
    ball_speed_mph: 141.3,
    estimated_carry_yards: estimatedCarry,
    carry_spin_adjusted: null,
    flight_path: null,
  });
  const first = opsShot('2026-10-08T10:00:00.000000', 180);
  const second = opsShot('2026-10-08T10:00:13.000000', 190);

  // On hardware the OPS numbers come first and the shot_update about 12 s later, in hit order: the next hit can
  // come in between.
  for (const shot of [first, second]) {
    await deliver(page, 'shot', shot);
    await expect(bar).toContainText('OPS metrics ready');
    await expect(bar).toContainText('Processing camera capture…');
    await expect(bar.locator('.virtual-range__stat')).toHaveCount(0);
    expect(await fillTexts(page), 'no landing sign at a provisional carry').toEqual([]);
  }
  await expectOverlaysInside(page);

  const update = (shot: typeof first, ballSpeed: number, carry: number) => ({
    ...shot,
    ball_speed_mph: ballSpeed,
    carry_spin_adjusted: carry,
    carry_lateral_yards: 3,
    flight_path: { unit: 'yd', points: wireFlight(carry, 3) },
  });
  await deliver(page, 'shot_update', update(first, 145.6, 152.4));
  await expect.poll(() => fillTexts(page), { message: 'the first shot flies' }).toEqual(['152 yds']);
  await expect(bar, 'the newest shot is still being processed').toContainText('Processing camera capture…');

  await deliver(page, 'shot_update', update(second, 151.2, 171.2));
  const carry = await expectLandedAtLatestCarry(page);
  expect(carry).toBe(171);
  await expect(bar).toContainText('151.2mph');
  const series = await carrySeries(page);
  expect(series, 'one count-up, from 0 to the final carry').toEqual([...series].sort((a, b) => a - b));
  expect(series.at(-1)).toBe(carry);
  expect(
    series.some((value) => value > 0 && value < carry * 0.8),
    `${series}`
  ).toBe(true);
  expect(series, 'no provisional carry ever showed').not.toContain(180);
  expect(series, 'no provisional carry ever showed').not.toContain(190);
  expect(await fillTexts(page)).toEqual(['152 yds', '171 yds']);
});

test('the app reopens the range after a reload', async ({ page }) => {
  await openRange(page);

  await reloadApp(page);
  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });

  await footerButton(page, 'Live').tap();
  await expect(page.locator('.virtual-range')).toHaveCount(0);
  await reloadApp(page);
  await expect(footerButton(page, 'Live')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.virtual-range')).toHaveCount(0);
});

test('a reload shows Live once another area was open after the range', async ({ page }) => {
  await openRange(page);

  await footerButton(page, 'Align').tap();
  await expect(footerButton(page, 'Align')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.virtual-range')).toHaveCount(0);

  await reloadApp(page);
  await expect(footerButton(page, 'Live')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.virtual-range')).toHaveCount(0);
});

test('a cleared session keeps the kiosk on the range, showing its empty hint', async ({ page }) => {
  await openRange(page);
  await simulateShot(page);
  await expectLandedAtLatestCarry(page);
  await expect(page.locator('.virtual-range__bar')).toBeVisible();

  await withControlSocket(resetSession);

  await expect(page.getByText('Hit a shot – it flies here.')).toBeVisible();
  await expect(page.locator('.virtual-range__bar')).toHaveCount(0);
  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
  await expect(canvases(page)).toHaveCount(1);
});

test('a cleared session still sends any other view back to Live', async ({ page }) => {
  await openApp(page);
  await footerButton(page, 'Align').tap();
  await expect(footerButton(page, 'Align')).toHaveAttribute('aria-pressed', 'true');

  await withControlSocket(resetSession);

  await expect(footerButton(page, 'Live')).toHaveAttribute('aria-pressed', 'true');
});

// The newest shot of each session is a different club from today's driver.
const SAVED_SESSIONS: MockedSession[] = [
  {
    summary: {
      id: 'session_20260930_180000_range',
      started_at: '2026-09-30T18:00:00+02:00',
      shot_count: 2,
      clubs: ['7-iron', '9-iron'],
    },
    shots: [
      savedShot('2026-09-30T18:00:10.000000', {
        shot_number: 1,
        club: '7-iron',
        ball_speed_mph: 105,
        estimated_carry_yards: 150,
        carry_spin_adjusted: 148,
      }),
      savedShot('2026-09-30T18:00:40.000000', {
        shot_number: 2,
        club: '9-iron',
        ball_speed_mph: 92,
        estimated_carry_yards: 125,
        carry_spin_adjusted: 121,
        carry_lateral_yards: -3,
        flight_path: {
          unit: 'yd',
          points: [
            [0, 0, 0, 0],
            [1, 40, -1, 25],
            [2, 80, -2, 35],
            [3, 110, -3, 15],
            [3.6, 121, -3, 0],
          ],
        },
      }),
    ],
  },
  {
    summary: {
      id: 'session_20260929_100000_range',
      started_at: '2026-09-29T10:00:00+02:00',
      shot_count: 1,
      clubs: ['wedge-54'],
    },
    shots: [
      savedShot('2026-09-29T10:00:05.000000', {
        shot_number: 1,
        club: 'wedge-54',
        ball_speed_mph: 70,
        estimated_carry_yards: 80,
        carry_spin_adjusted: 78,
      }),
    ],
  },
];

const pathOf = (url: string): string => {
  const { pathname, search } = new URL(url);
  return pathname + search;
};

test('a saved session opens on the range from the history and ends when the range is left', async ({ page }) => {
  const sessionUrls = await mockSessionHistory(page, SAVED_SESSIONS);
  await openApp(page);
  await simulateShot(page);
  const todayCarry = await latestCarryYards(page);

  const date = await showSavedSession(page, 0);

  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await expect(page.locator('.virtual-range__session')).toHaveText(`Saved session · ${date}`);
  await expect(page.locator('.virtual-range__club')).toHaveText('9 Iron');
  expect(sessionUrls.map(pathOf)).toEqual([`/api/session-history/${SAVED_SESSIONS[0].summary.id}?flight_path=1`]);

  await footerButton(page, 'Live').tap();
  await expect(page.locator('.virtual-range')).toHaveCount(0);
  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });

  await expect(page.locator('.virtual-range__session')).toHaveCount(0);
  await expect(page.locator('.virtual-range__club')).toHaveText('Driver');
  await expect(page.locator('.virtual-range__stat--carry .virtual-range__value')).toHaveText(`${todayCarry}yds`);
});

test('another saved session replaces the first on the range, and Back to today shows the live shots', async ({
  page,
}) => {
  await mockSessionHistory(page, SAVED_SESSIONS);
  await openRange(page);
  await simulateShot(page);

  const first = await showSavedSession(page, 0);
  await expect(page.locator('.virtual-range__club')).toHaveText('9 Iron');
  await expect(page.locator('.virtual-range__session')).toHaveText(`Saved session · ${first}`);

  const second = await showSavedSession(page, 1);
  expect(second).not.toBe(first);
  await expect(page.locator('.virtual-range__club')).toHaveText('Wedge 54°');
  await expect(page.locator('.virtual-range__session')).toHaveText(`Saved session · ${second}`);

  await page.getByRole('button', { name: 'Back to today' }).tap();
  await expect(page.locator('.virtual-range__session')).toHaveCount(0);
  await expect(page.locator('.virtual-range__club')).toHaveText('Driver');
  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
});

test.describe(`at ${KIOSK_VIEWPORTS[0].width}×${KIOSK_VIEWPORTS[0].height} in German`, () => {
  test.use({ viewport: KIOSK_VIEWPORTS[0] });

  test('the saved session and Back to today fit, clear of the flight bar, header, band and footer', async ({
    page,
  }) => {
    await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
    await mockSessionHistory(page, SAVED_SESSIONS);
    await gotoApp(page);
    await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
    await footerButton(page, 'Verlauf').tap();
    await page.locator('.session-history__session').first().tap();
    await page.getByRole('button', { name: 'Auf der Range zeigen', exact: true }).tap();
    await expect(page.locator('.virtual-range__session')).toContainText('Gespeicherte Session');
    await expect(page.locator('.virtual-range__club')).toHaveText('Eisen 9');
    await page.evaluate(() => document.fonts.ready);

    const topBar = '.virtual-range__session, .virtual-range__back';
    for (const band of [false, true]) {
      await showDemoBand(page, band);
      await expect(page.locator('.demo-band')).toHaveCount(band ? 1 : 0);
      if (band) await expect(page.locator('.demo-band')).toContainText('DEMO – echte Schläge werden ignoriert');
      await chromeMeasured(page);
      expect(await outsideScreen(page, topBar)).toEqual([]);
      expect(await overlapping(page, topBar, '.virtual-range__bar, .range-footer, .range-app__top')).toEqual([]);
      expect(await overlapping(page, '.demo-band', '.range-header, .virtual-range__bar, .range-footer')).toEqual([]);
      expect(
        await clippedText(
          page,
          `${topBar}, .virtual-range__club, .virtual-range__value, .demo-band__text, .demo-band__end`
        )
      ).toEqual([]);
      expect(await smallTargets(page, '.virtual-range__back, .demo-band__end, .range-footer button')).toEqual([]);
      await expectBetweenTheBars(page, '.virtual-range__top, .virtual-range__bar');
    }
  });
});

test('a shot hit while a saved session is shown takes the range back to today, where it flies', async ({ page }) => {
  await installSpy(page);
  await mockSessionHistory(page, SAVED_SESSIONS);
  await openRange(page);
  await showSavedSession(page, 0);
  await expect(page.locator('.virtual-range__club')).toHaveText('9 Iron');
  await recordCarry(page);

  await simulateShot(page);

  await expect(page.locator('.virtual-range__session')).toHaveCount(0);
  await expect(page.locator('.virtual-range__club')).toHaveText('Driver');
  const carry = await expectLandedAtLatestCarry(page);
  const series = await carrySeries(page);
  expect(
    series.some((value) => value > 0 && value < carry * 0.8),
    `a count-up to ${carry}: ${series}`
  ).toBe(true);
});

test('a browser that is not the kiosk has no Show on range, and asks for no flight paths', async ({ page }) => {
  const sessionUrls = await mockSessionHistory(page, SAVED_SESSIONS);
  // *.localhost reaches this machine but is not one of the kiosk hosts.
  await page.goto('http://phone.localhost:5173/');
  await page.getByRole('button', { name: 'Close Select club' }).tap();
  await expect(footerButton(page, 'Range')).toHaveCount(0);

  await footerButton(page, 'History').tap();
  await page.locator('.session-history__session').first().tap();

  await expect(page.locator('.session-history tbody tr')).toHaveCount(2);
  await expect(page.getByRole('button', { name: 'Back', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Show on range', exact: true })).toHaveCount(0);
  expect(sessionUrls.map(pathOf)).toEqual([`/api/session-history/${SAVED_SESSIONS[0].summary.id}`]);
});

test('an open session is not fetched again when the app re-renders', async ({ page }) => {
  const sessionUrls = await mockSessionHistory(page, SAVED_SESSIONS);
  await page.route('**/api/flush', (route) => route.fulfill({ body: '' }));
  await openApp(page);
  await footerButton(page, 'History').tap();
  await page.locator('.session-history__session').first().tap();
  await expect(page.getByRole('button', { name: 'Show on range', exact: true })).toBeVisible();

  // Route handlers run in request order: once this is answered, any request the re-render made has been counted.
  await page.evaluate(async () => {
    const store = '/src/stores/useHeroMetricStore.ts';
    const { useHeroMetricStore } = await import(/* @vite-ignore */ store);
    useHeroMetricStore.getState().setHeroMetricId('carry');
    await new Promise((resolve) => requestAnimationFrame(resolve));
    await fetch('/api/flush');
  });

  expect(sessionUrls).toHaveLength(1);
});

test('leaving mid-flight disposes the scene', async ({ page }) => {
  await installSpy(page);
  await openRange(page);
  await recordCarry(page);

  await simulateShot(page);
  const started = await frames(page);
  await expect.poll(() => frames(page)).toBeGreaterThan(started + 5);
  await footerButton(page, 'Live').tap();

  await expect(page.locator('canvas')).toHaveCount(0);
  await expect(page.locator('.virtual-range')).toHaveCount(0);
  expect(await contextsLost(page), 'the range made one WebGL context and lost it').toEqual([true]);
  const landed = await latestCarryYards(page);
  expect((await carrySeries(page)).at(-1), 'the flight had not landed yet').toBeLessThan(landed);
  await page.waitForTimeout(500); // the Live view fits its numbers once when it appears
  const idle = await frames(page);
  await page.waitForTimeout(2000);
  expect(await frames(page), 'animation frames in the 2 s after leaving').toBe(idle);

  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  expect(await contextsLost(page), 'a fresh scene on the way back').toEqual([true, false]);
  await recordCarry(page);
  await simulateShot(page);
  await expectLandedAtLatestCarry(page);
  expect((await carrySeries(page)).length, 'a count-up, not a jump').toBeGreaterThan(5);
});

test('the scene chunk loads only when the Range opens', async ({ page }) => {
  const scripts: string[] = [];
  page.on('request', (request) => {
    if (request.resourceType() === 'script') scripts.push(request.url());
  });
  await openApp(page);

  const sceneScripts = () => scripts.filter((url) => /createRangeScene|\/three[.\-/]/.test(url));
  expect(sceneScripts()).toEqual([]);

  await rangeButton(page).tap();
  await expect.poll(() => sceneScripts().length, { timeout: SCENE_TIMEOUT_MS }).toBeGreaterThan(0);
});

test('changing the distance unit with the range open rebuilds the scene in that unit', async ({ page }) => {
  await installSpy(page);
  await openApp(page);
  await spyOnFillText(page);
  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await expect.poll(() => fillTexts(page)).toContain('55 yds');
  expect((await fillTexts(page)).filter((text) => text.endsWith(' m'))).toEqual([]);

  await spyOnFillText(page);
  await page.getByRole('button', { name: 'Open menu' }).tap();
  await openMenuArea(page, 'Display', { tap: true });
  const meters = page.getByRole('group', { name: 'Distance' }).getByRole('button', { name: 'Meters', exact: true });
  await meters.tap();
  await expect(meters).toHaveAttribute('aria-pressed', 'true');

  await expect
    .poll(() => fillTexts(page), { timeout: SCENE_TIMEOUT_MS })
    .toEqual(expect.arrayContaining(['50 m', '230 m']));
  expect((await fillTexts(page)).filter((text) => text.endsWith(' yds'))).toEqual([]);
  await expect(canvases(page)).toHaveCount(1);
  expect(await contextsLost(page), 'the old scene released its context, the new one has its own').toEqual([
    true,
    false,
  ]);
});

test('a viewport resize follows into the canvas size', async ({ page }) => {
  await openRange(page);
  const sizes = () =>
    page.evaluate(() => {
      const canvas = document.querySelector<HTMLCanvasElement>('.virtual-range__host canvas')!;
      const host = document.querySelector<HTMLElement>('.virtual-range__host')!;
      return { canvas: [canvas.width, canvas.height], host: [host.clientWidth, host.clientHeight] };
    });

  const first = await sizes();
  expect(first.canvas).toEqual(first.host);
  let previous = first.host;
  for (const viewport of [KIOSK_VIEWPORTS[1], KIOSK_VIEWPORTS[0]]) {
    await page.setViewportSize(viewport);
    await expect
      .poll(async () => (await sizes()).canvas, { message: `canvas at ${viewport.width}×${viewport.height}` })
      .not.toEqual(previous);
    const now = await sizes();
    expect(now.canvas).toEqual(now.host);
    expect(now.host[0]).toBeLessThanOrEqual(viewport.width);
    expect(now.host[1]).toBeLessThanOrEqual(viewport.height);
    previous = now.host;
  }
});

test('a graphics step lasts for the running kiosk: read from sessionStorage, not localStorage', async ({ page }) => {
  const key = 'openflight.virtual-range.quality:v1';
  const antialiased = () =>
    page.evaluate(() => window.__rangeSpy.contexts.map((context) => context.getContextAttributes()?.antialias));
  await installSpy(page);
  await page.addInitScript((key) => localStorage.setItem(key, '1'), key);
  await openRange(page);
  expect(await antialiased(), 'a step kept in localStorage').toEqual([true]);

  await page.evaluate((key) => sessionStorage.setItem(key, '1'), key);
  await reloadApp(page);
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  expect(await antialiased(), 'the step of this session, after a reload').toEqual([false]);
});

test('?rangeDemo=1 opens the range on a fresh profile and flies the demo shots', async ({ page }) => {
  await installSpy(page);
  await gotoApp(page, '/?rangeDemo=1');

  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.picker-overlay')).toHaveCount(0);
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await expect(page.locator('.virtual-range__club')).toHaveText('8 Iron', { timeout: SCENE_TIMEOUT_MS });
  await expect.poll(() => frames(page)).toBeGreaterThan(30);

  expect(await page.evaluate(() => localStorage.getItem('openflight.last-area:v1'))).toBeNull();
  await openApp(page);
  await expect(footerButton(page, 'Live')).toHaveAttribute('aria-pressed', 'true');
});

/** Waits until the range has the chrome's current height: a band that shows takes a frame to measure. */
async function chromeMeasured(page: Page): Promise<void> {
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const app = document.querySelector<HTMLElement>('.range-app')!;
          const height = (selector: string) =>
            `${Math.ceil(document.querySelector(selector)!.getBoundingClientRect().height)}px`;
          return (
            app.style.getPropertyValue('--virtual-range-inset-top') === height('.range-app__top') &&
            app.style.getPropertyValue('--virtual-range-inset-bottom') === height('.range-footer')
          );
        }),
      { message: 'the range knows how much the chrome covers' }
    )
    .toBe(true);
}

/** Each element under `selector` lies in the free area between the top chrome and the footer, inside the screen. */
async function expectBetweenTheBars(page: Page, selector: string): Promise<void> {
  const free = await freeArea(page);
  const boxes = await page.locator(selector).evaluateAll((elements) =>
    elements.map((element) => {
      const { left, top, right, bottom } = element.getBoundingClientRect();
      return { name: element.className, left, top, right, bottom };
    })
  );
  expect(boxes.length, selector).toBeGreaterThan(0);
  for (const box of boxes) {
    expect(box.top, `${box.name} under the top chrome (${free.top})`).toBeGreaterThanOrEqual(free.top);
    expect(box.bottom, `${box.name} above the footer (${free.bottom})`).toBeLessThanOrEqual(free.bottom);
    expect(box.left, box.name).toBeGreaterThanOrEqual(0);
    expect(box.right, box.name).toBeLessThanOrEqual(free.width);
  }
}

/** Boxes of the screen's parts, null for a part that is not shown, and how the chrome looks over the canvas. */
function rangeScreen(page: Page) {
  return page.evaluate(() => {
    const box = (selector: string) => {
      const element = document.querySelector(selector);
      if (!element) return null;
      const { left, top, right, bottom } = element.getBoundingClientRect();
      return { left, top, right, bottom };
    };
    const look = (selector: string) => {
      const style = getComputedStyle(document.querySelector(selector)!);
      const [, , , alpha = 1] = style.backgroundColor.match(/[\d.]+/g)!.map(Number);
      return { alpha, backdropFilter: style.backdropFilter };
    };
    const hit = (x: number, y: number, selector: string) => Boolean(document.elementFromPoint(x, y)?.closest(selector));
    const { innerWidth: width, innerHeight: height } = window;
    return {
      full: document.querySelector('.range-app')!.classList.contains('range-app--full'),
      canvas: box('.virtual-range__host canvas'),
      top: box('.range-app__top'),
      header: box('.range-header'),
      footer: box('.range-footer'),
      main: box('.panel-app__main'),
      looks: [look('.range-app__top'), look('.range-footer')],
      chromeOverCanvas: hit(width / 2, 2, '.range-app__top') && hit(width / 2, height - 2, '.range-footer'),
    };
  });
}

/** Where the middle of the range camera's view lands on the screen, in px from the top. */
function opticalCentre(page: Page): Promise<number | null> {
  return page.evaluate(() => {
    const camera = window.__rangeCamera;
    if (!camera) return null;
    const ahead = camera.getWorldDirection(camera.position.clone()).add(camera.position).project(camera);
    const canvas = document.querySelector('.virtual-range__host canvas')!.getBoundingClientRect();
    return canvas.top + ((1 - ahead.y) / 2) * canvas.height;
  });
}

/** How far above the footer the element under `selector` ends, in whole px. */
function gapAboveFooter(page: Page, selector: string): Promise<number> {
  return page.evaluate(
    (selector) =>
      Math.round(
        document.querySelector('.range-footer')!.getBoundingClientRect().top -
          document.querySelector(selector)!.getBoundingClientRect().bottom
      ),
    selector
  );
}

/** The greens whose distance sign, in the view at rest, overlaps the box of `selector`, by distance in m. */
function signsUnder(page: Page, selector: string): Promise<number[]> {
  return page.evaluate(async (selector) => {
    const load = (path: string) => import(/* @vite-ignore */ path);
    const { TEE_POSE } = await load('/src/virtual-range/scene/cameraPath.ts');
    const { GREENS } = await load('/src/virtual-range/layout.ts');
    const canvas = document.querySelector('.virtual-range__host canvas')!.getBoundingClientRect();
    const box = document.querySelector(selector)!.getBoundingClientRect();
    const camera = window.__rangeCamera!.clone();
    const { position, target } = TEE_POSE;
    camera.position.set(position.x, position.y, position.z);
    camera.lookAt(target.x, target.y, target.z);
    camera.updateMatrixWorld();
    const Vector = camera.position.constructor as new (x: number, y: number, z: number) => SceneVector;
    // A green's sign (greens.ts, signs.ts): 0.068 of its distance wide, a 256 × 112 canvas on its foot 5 m up over
    // the flag, reaching away from the target line.
    const width = (0.068 * canvas.height) / 2 / Math.tan((camera.fov * Math.PI) / 360);
    const height = (width * 112) / 256;
    return GREENS.filter(({ distanceM, sideM }: { distanceM: number; sideM: number }) => {
      const ndc = new Vector(sideM, 5, -distanceM).project(camera);
      const x = canvas.left + ((ndc.x + 1) / 2) * canvas.width;
      const y = canvas.top + ((1 - ndc.y) / 2) * canvas.height;
      const centre = sideM < 0 ? 0.92 : 0.08;
      const [left, right] = [x - centre * width, x + (1 - centre) * width];
      return left < box.right && right > box.left && y - height < box.bottom && y > box.top;
    }).map(({ distanceM }: { distanceM: number }) => distanceM);
  }, selector);
}

interface FlightOnScreen {
  shot: string;
  carryM: number;
  /** Share of the flight still under the footer when the ball first comes up between the bars. */
  inFrom: number | null;
  ballTop: number;
  signTop: number;
  signBottom: number;
  restDot: number;
  outside: string[];
}

/**
 * Flies a 60 m wedge, the 8-iron and the driver of the demo with the camera the scene draws with (its size, its
 * view offset) along the scene's camera path, and says where ball, landing dot and landing sign come out on the
 * screen: the ball from the moment it comes up between the bars until it lands, dot and sign from the landing
 * until the camera is back at the tee (the sign hides half way back), the dot again at rest.
 */
function flightsOnScreen(page: Page): Promise<FlightOnScreen[]> {
  return page.evaluate(async () => {
    const load = (path: string) => import(/* @vite-ignore */ path);
    const { CAMERA_TIMING, TEE_POSE, cameraPoseAt, flightSeconds, shotDuration } = await load(
      '/src/virtual-range/scene/cameraPath.ts'
    );
    const { positionAt, sampleFlight } = await load('/src/virtual-range/flightPath.ts');
    const { demoShots } = await load('/src/virtual-range/demoShots.ts');
    const top = document.querySelector('.range-app__top')!.getBoundingClientRect().bottom;
    const bottom = document.querySelector('.range-footer')!.getBoundingClientRect().top;
    const canvas = document.querySelector('.virtual-range__host canvas')!.getBoundingClientRect();
    const camera = window.__rangeCamera!.clone();
    const Vector = camera.position.constructor as new (x: number, y: number, z: number) => SceneVector;
    const screen = (x: number, y: number, z: number) => {
      const ndc = new Vector(x, y, z).project(camera);
      return { x: canvas.left + ((ndc.x + 1) / 2) * canvas.width, y: canvas.top + ((1 - ndc.y) / 2) * canvas.height };
    };
    const place = ({ position: p, target: t }: { position: Point; target: Point }) => {
      camera.position.set(p.x, p.y, p.z);
      camera.lookAt(t.x, t.y, t.z);
      camera.updateMatrixWorld();
    };
    const between = (x: number, y: number) => y >= top && y <= bottom && x >= canvas.left && x <= canvas.right;
    // The landing sign: 0.17 of its distance wide (signs.ts), a 300 × 132 canvas on its foot (tracer.ts), 1.6 m up.
    const signWidth = (0.17 * canvas.height) / 2 / Math.tan((camera.fov * Math.PI) / 360);
    const signHeight = (signWidth * 132) / 300;
    const [iron8, driver, wedge] = demoShots();
    const toSixty = 60 / wedge.carryM;
    const wedge60 = {
      ...wedge,
      id: 'wedge-60m',
      flight: wedge.flight.map((point: Point) => ({ ...point, x: point.x * toSixty, z: point.z * toSixty })),
    };

    return [wedge60, iron8, driver].map((shot): FlightOnScreen => {
      const samples = sampleFlight(shot.flight, 0.025);
      const flightS = flightSeconds(samples);
      const last = samples.at(-1);
      const outside: string[] = [];
      let inFrom: number | null = null;
      let ballTop = Infinity;
      let signTop = Infinity;
      let signBottom = -Infinity;
      for (let t = 0; t <= shotDuration(flightS); t += 1 / 60) {
        place(cameraPoseAt(samples, t));
        const flownS = t - CAMERA_TIMING.preS;
        const at = `${shot.id} at ${t.toFixed(2)} s`;
        if (flownS > 0 && flownS < flightS) {
          const ball = positionAt(samples, samples[0].t + flownS);
          const { x, y } = screen(ball.x, ball.y, ball.z);
          if (inFrom === null && between(x, y)) inFrom = flownS / flightS;
          if (inFrom === null) continue;
          ballTop = Math.min(ballTop, y);
          if (!between(x, y)) outside.push(`${at}: ball ${Math.round(x)},${Math.round(y)}`);
        } else if (flownS >= flightS) {
          const dot = screen(last.x, 0, last.z);
          const foot = screen(last.x, 1.6, last.z);
          const sign = [foot.x - signWidth / 2, foot.y - signHeight, foot.x + signWidth / 2, foot.y];
          signTop = Math.min(signTop, sign[1]);
          signBottom = Math.max(signBottom, sign[3]);
          if (!between(dot.x, dot.y) || !between(sign[0], sign[1]) || !between(sign[2], sign[3]))
            outside.push(`${at}: dot ${Math.round(dot.y)}, sign ${sign.map(Math.round)}`);
        }
      }
      if (inFrom === null) outside.push(`${shot.id}: the ball never comes up between the bars`);
      place(TEE_POSE);
      const rest = screen(last.x, 0, last.z);
      if (!between(rest.x, rest.y)) outside.push(`${shot.id} at rest: dot ${Math.round(rest.x)},${Math.round(rest.y)}`);
      return {
        shot: shot.id,
        carryM: Math.round(Math.hypot(last.x, last.z)),
        inFrom: inFrom === null ? null : Math.round(inFrom * 1000) / 1000,
        ballTop: Math.round(ballTop),
        signTop: Math.round(signTop),
        signBottom: Math.round(signBottom),
        restDot: Math.round(rest.y),
        outside,
      };
    });
  });
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`the full-screen range at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('the scene fills the screen under see-through bars; notice, hint, its own bars and the band sit between them', async ({
      page,
    }) => {
      let releaseScene!: () => void;
      const sceneHeld = new Promise<void>((resolve) => (releaseScene = resolve));
      await page.route('**/virtual-range/scene/createRangeScene.ts*', async (route) => {
        await sceneHeld;
        await route.continue();
      });
      await mockSessionHistory(page, SAVED_SESSIONS);
      await openApp(page);
      await rangeButton(page).tap();
      await page.evaluate(() => document.fonts.ready);

      const notice = page.locator('.virtual-range__notice');
      await expect(notice).toHaveText('Loading the range…');
      await chromeMeasured(page);
      const free = await freeArea(page);
      const noticeBox = (await notice.boundingBox())!;
      // Within a px: the bars' heights reach the CSS rounded up to whole px.
      expect(
        Math.abs(noticeBox.y + noticeBox.height / 2 - (free.top + free.bottom) / 2),
        'the notice in the middle of the free area'
      ).toBeLessThanOrEqual(1);
      releaseScene();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });

      const screen = await rangeScreen(page);
      expect(screen.full).toBe(true);
      expect(screen.canvas, 'the canvas covers the screen').toEqual({
        left: 0,
        top: 0,
        right: viewport.width,
        bottom: viewport.height,
      });
      expect(screen.top).toMatchObject({ left: 0, top: 0, right: viewport.width });
      expect(screen.footer).toMatchObject({ left: 0, right: viewport.width, bottom: viewport.height });
      expect(screen.chromeOverCanvas, 'header and footer lie over the canvas').toBe(true);
      for (const { alpha, backdropFilter } of screen.looks) {
        expect(alpha, 'see-through').toBeGreaterThan(0);
        expect(alpha, 'see-through').toBeLessThan(1);
        expect(backdropFilter).toBe('none');
      }

      await expect(page.getByText('Hit a shot – it flies here.')).toBeVisible();
      const hintGaps: number[] = [];
      for (const band of [true, false]) {
        await showDemoBand(page, band);
        await expect(page.locator('.demo-band')).toHaveCount(band ? 1 : 0);
        await chromeMeasured(page);
        await expectBetweenTheBars(page, '.virtual-range__hint');
        hintGaps.push(await gapAboveFooter(page, '.virtual-range__hint'));
      }
      await expectOverlaysInside(page);
      expect(await covered(page, '.range-header button, .range-footer button')).toEqual([]);

      await showSavedSession(page, 0);
      await expect(page.locator('.virtual-range__club')).toHaveText('9 Iron');
      const barGap = await gapAboveFooter(page, '.virtual-range__bar');
      expect(hintGaps, 'the hint holds the place the bar takes, with and without the band').toEqual([barGap, barGap]);
      for (const band of [false, true]) {
        await showDemoBand(page, band);
        await expect(page.locator('.demo-band')).toHaveCount(band ? 1 : 0);
        await chromeMeasured(page);
        const shown = band ? 'with the band' : 'without the band';
        await expectBetweenTheBars(page, '.virtual-range__top, .virtual-range__bar');
        expect(await overlapping(page, '.virtual-range__top', '.virtual-range__bar'), shown).toEqual([]);
        expect(
          await overlapping(
            page,
            '.demo-band',
            '.range-header, .range-footer, .virtual-range__top, .virtual-range__bar'
          ),
          shown
        ).toEqual([]);
        await expectOverlaysInside(page);
        expect(
          await clippedText(page, '.virtual-range__session, .virtual-range__back, .demo-band__text'),
          shown
        ).toEqual([]);
        expect(await smallTargets(page, '.virtual-range__back, .demo-band__end'), shown).toEqual([]);
        expect(await covered(page, '.range-footer button, .virtual-range__back, .demo-band__end'), shown).toEqual([]);
      }
      await showDemoBand(page, false);

      await footerButton(page, 'Live').tap();
      await expect(page.locator('.virtual-range')).toHaveCount(0);
      const live = await rangeScreen(page);
      expect(live.full).toBe(false);
      expect(live.main!.top, 'Live starts under the header').toBeGreaterThanOrEqual(live.header!.bottom);
      expect(live.main!.bottom, 'Live ends above the footer').toBeLessThanOrEqual(live.footer!.top);
    });

    test('flights and their landings stay between the bars, the view centred there, and the hint covers no sign', async ({
      page,
    }) => {
      await openApp(page);
      await spyOnRangeCamera(page);
      await rangeButton(page).tap();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      await page.evaluate(() => document.fonts.ready);

      for (const band of [false, true]) {
        await showDemoBand(page, band);
        await expect(page.locator('.demo-band')).toHaveCount(band ? 1 : 0);
        const free = await freeArea(page);
        // Within a px: the scene gets the bars' heights rounded up to whole px.
        await expect
          .poll(async () => Math.abs(((await opticalCentre(page)) ?? Infinity) - (free.top + free.bottom) / 2), {
            message: 'the view is centred between the bars',
          })
          .toBeLessThanOrEqual(1);

        const flights = await flightsOnScreen(page);
        const size = `${viewport.width}x${viewport.height}${band ? ' band' : ''}`;
        const numbers = JSON.stringify(flights, (key, value) => (key === 'outside' ? undefined : value));
        console.log(`CAMERA ${size} free ${free.top}..${free.bottom} ${numbers}`);
        expect(flights.flatMap(({ outside }) => outside)).toEqual([]);
        await expect(page.getByText('Hit a shot – it flies here.')).toBeVisible();
        expect(await signsUnder(page, '.virtual-range__hint'), 'greens whose sign the hint covers').toEqual([]);
      }
    });
  });
}

const TARGET = 4;
const baseOf = (index: number): Point => ({ x: GREENS[index].sideM, y: 0, z: -GREENS[index].distanceM });

async function redraw(page: Page): Promise<void> {
  const size = page.viewportSize()!;
  await page.setViewportSize({ ...size, height: size.height - 1 });
  await expect.poll(() => page.evaluate(() => Boolean(window.__rangeWorld!.scene))).toBe(true);
  await page.setViewportSize(size);
  await settled(page);
}

async function openAimedRange(page: Page): Promise<void> {
  await openApp(page);
  await spyOnRangeCamera(page);
  await spyOnRangeWorld(page);
  await rangeButton(page).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);
}

const dotsInWorld = (page: Page) => page.evaluate(() => window.__rangeWorld!.dots());

async function tapFlag(page: Page, index: number): Promise<void> {
  const cloth = await onScreen(page, clothOf(index));
  await page.touchscreen.tap(cloth.x, cloth.y);
}

async function turnedTo(page: Page, index: number | null): Promise<void> {
  await expect.poll(() => storedTarget(page)).toBe(index);
  await expect.poll(() => worldTurn(page)).toBeCloseTo(index === null ? 0 : greenBearing(GREENS[index]), 12);
  await settled(page);
}

const signText = (page: Page, index: number): Promise<string> => signLabel(page, GREENS[index].distanceM);

function countCanvasPresses(page: Page): Promise<void> {
  return page.evaluate(() => {
    window.__canvasPresses = 0;
    document
      .querySelector('.virtual-range__host canvas')!
      .addEventListener('pointerdown', () => (window.__canvasPresses = (window.__canvasPresses ?? 0) + 1));
  });
}

function bareSpots(page: Page, selector: string): Promise<{ name: string; x: number; y: number }[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements.flatMap((element) => {
      const box = element.getBoundingClientRect();
      for (let fy = 0.5; fy > 0; fy -= 0.2) {
        // From 1 %: the footer's buttons leave bare room only in its margins.
        for (let fx = 0.01; fx < 1; fx += 0.05) {
          const x = box.left + fx * box.width;
          const y = box.top + fy * box.height;
          // Bare 4 px to either side too: in a•space a sample fell 0.9 px beside the Live button, in the
          // footer's 4 px gap between two buttons, and the touch tap was adjusted onto Live.
          const bare = [-4, 0, 4].every((dx) => {
            const hit = document.elementFromPoint(x + dx, y);
            return hit && element.contains(hit) && !hit.closest('button, a, input, select, textarea');
          });
          // Touch adjustment moves a tap onto a control up to about 9 px away (in Nexa a footer sample
          // 5.8 px above Live landed on Live), so keep 12 px from every control.
          const nearControl = [...document.querySelectorAll('button, a, input, select, textarea')].some((control) => {
            const r = control.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            return Math.hypot(Math.max(r.left - x, 0, x - r.right), Math.max(r.top - y, 0, y - r.bottom)) < 12;
          });
          if (bare && !nearControl) {
            return [{ name: element.className, x, y }];
          }
        }
      }
      return [];
    })
  );
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`aiming at a green at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport });

    test('a tapped flag turns its green straight ahead and a second tap turns back; drags and the chrome select nothing', async ({
      page,
    }) => {
      await openAimedRange(page);
      const free = await freeArea(page);
      const cloth = await onScreen(page, clothOf(TARGET));
      await page.mouse.move(cloth.x, cloth.y);
      await page.mouse.down();
      await page.mouse.move(cloth.x + 40, cloth.y + 6, { steps: 5 });
      await page.mouse.up();

      await countCanvasPresses(page);
      const spots = await bareSpots(page, '.range-app__top, .range-footer, .virtual-range__hint');
      expect(spots.map(({ name }) => name).length, JSON.stringify(spots)).toBe(3);
      for (const { x, y } of spots) await page.touchscreen.tap(x, y);
      await page.waitForTimeout(700);
      expect(await page.evaluate(() => window.__canvasPresses)).toBe(0);
      expect(await storedTarget(page)).toBeNull();
      expect(await worldTurn(page)).toBe(0);
      expect(await greenHighlighted(page)).toBe(false);

      await spyOnFillText(page);
      await tapFlag(page, TARGET);
      await turnedTo(page, TARGET);
      const flag = await onScreen(page, baseOf(TARGET));
      expect(Math.abs(flag.x - free.width / 2), `flag at ${flag.x}`).toBeLessThanOrEqual(3);
      expect(flag.y).toBeGreaterThan(free.top);
      expect(flag.y).toBeLessThan(free.bottom);
      expect(await greenHighlighted(page)).toBe(true);
      expect(await fillTexts(page)).toContain(await signText(page, TARGET));
      expect(await page.evaluate((point) => window.__rangeWorld!.signShown(point), baseOf(TARGET))).toBe(true);
      await settled(page);

      await tapFlag(page, TARGET);
      await turnedTo(page, null);
      expect(await greenHighlighted(page)).toBe(false);
      const unturned = await onScreen(page, baseOf(TARGET));
      expect(unturned.x).toBeLessThan(free.width / 2 - 3);
    });
  });
}

test.describe(`taps near far flags at ${KIOSK_VIEWPORTS[0].width}×${KIOSK_VIEWPORTS[0].height}`, () => {
  test.use({ viewport: KIOSK_VIEWPORTS[0] });

  test('a tap beside the 230 m flag selects it; open fairway and a drag select nothing', async ({ page }) => {
    await openAimedRange(page);
    const far = GREENS.findIndex(({ distanceM }) => distanceM === 230);
    const stick = await onScreen(page, { ...baseOf(far), y: 2 });
    const beside = { x: stick.x + 20, y: stick.y };
    expect(await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.tagName, beside)).toBe('CANVAS');

    const flags = await Promise.all(GREENS.map((_, index) => onScreen(page, baseOf(index))));
    const open = await page.evaluate((flags) => {
      const canvas = document.querySelector('.virtual-range__host canvas');
      const top = document.querySelector('.range-app__top')!.getBoundingClientRect().bottom;
      const bottom = document.querySelector('.range-footer')!.getBoundingClientRect().top;
      let best = { x: 0, y: 0, clear: 0 };
      for (let y = top + 10; y < bottom - 10; y += 10) {
        for (let x = 10; x < window.innerWidth - 10; x += 10) {
          if (document.elementFromPoint(x, y) !== canvas) continue;
          const clear = Math.min(...flags.map((flag) => Math.hypot(flag.x - x, flag.y - y)));
          if (clear > best.clear) best = { x, y, clear };
        }
      }
      return best;
    }, flags);
    expect(open.clear).toBeGreaterThanOrEqual(80);
    await page.touchscreen.tap(open.x, open.y);

    await page.mouse.move(beside.x, beside.y);
    await page.mouse.down();
    await page.mouse.move(beside.x + 40, beside.y + 6, { steps: 5 });
    await page.mouse.up();
    await page.waitForTimeout(700);
    expect(await storedTarget(page)).toBeNull();
    expect(await worldTurn(page)).toBe(0);

    await page.touchscreen.tap(beside.x, beside.y);
    await turnedTo(page, far);
  });
});

test('defers the turn until the ball rests', async ({ page }) => {
  await openAimedRange(page);
  await simulateShot(page);
  await expect.poll(() => page.evaluate(() => window.__rangeWorld!.frames.some(({ ball }) => ball))).toBe(true);

  const tapped = await page.evaluate(
    (cloths) => {
      const canvas = document.querySelector('.virtual-range__host canvas')!;
      const top = document.querySelector('.range-app__top')!.getBoundingClientRect().bottom;
      const bottom = document.querySelector('.range-footer')!.getBoundingClientRect().top;
      const index = cloths.findIndex((cloth) => {
        const { x, y } = window.__rangeWorld!.onScreen(cloth);
        return y > top && y < bottom && x > 0 && x < window.innerWidth && document.elementFromPoint(x, y) === canvas;
      });
      if (index < 0) return { index, at: performance.now() };
      const { x, y } = window.__rangeWorld!.onScreen(cloths[index]);
      const press = { pointerId: 7, isPrimary: true, button: 0, clientX: x, clientY: y, bubbles: true };
      canvas.dispatchEvent(new PointerEvent('pointerdown', press));
      canvas.dispatchEvent(new PointerEvent('pointerup', press));
      return { index, at: performance.now() };
    },
    GREENS.map((_, index) => clothOf(index))
  );
  expect(tapped.index, 'a flag in view mid-flight').toBeGreaterThanOrEqual(0);
  await expect.poll(() => storedTarget(page)).toBe(tapped.index);

  await expect.poll(() => worldTurn(page), { timeout: 30_000 }).toBeCloseTo(greenBearing(GREENS[tapped.index]), 12);
  await settled(page);
  const frames = await page.evaluate(() => window.__rangeWorld!.frames);
  const lastBall = frames.findLastIndex(({ ball }) => ball);
  expect(frames[lastBall].at, 'the ball still shows after the tap').toBeGreaterThan(tapped.at);
  expect(frames.filter(({ ball }) => ball).every((frame) => frame.turn === 0)).toBe(true);
  expect(frames.findIndex((frame) => frame.turn !== 0)).toBeGreaterThan(lastBall);
});

test("keeps each shot's bearing across a reload", async ({ page }) => {
  await openAimedRange(page);
  await tapFlag(page, 1);
  await turnedTo(page, 1);
  await simulateShot(page);
  await expectLandedAtLatestCarry(page);
  await settled(page);
  const aimed = await dotsInWorld(page);
  expect(aimed).toHaveLength(1);

  await tapFlag(page, 1);
  await turnedTo(page, null);
  await settled(page);
  const before = await dotsInWorld(page);
  const bearing = greenBearing(GREENS[1]);
  const expected = rotateAboutTee(aimed[0], bearing);
  expect(before[0].x).toBeCloseTo(expected.x, 3);
  expect(before[0].z).toBeCloseTo(expected.z, 3);
  expect(Math.abs(before[0].x - aimed[0].x)).toBeGreaterThan(1);

  await page.reload();
  await page.getByRole('button', { name: 'Close Select club' }).tap();
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
  await spyOnRangeCamera(page);
  await spyOnRangeWorld(page);
  await redraw(page);
  expect(await worldTurn(page)).toBe(0);
  expect(await dotsInWorld(page)).toEqual(before);
});

test('draws saved sessions unturned and ignores green taps there', async ({ page }) => {
  await mockSessionHistory(page, SAVED_SESSIONS);
  await openAimedRange(page);
  await tapFlag(page, TARGET);
  await turnedTo(page, TARGET);

  const openedAt = await page.evaluate(() => performance.now());
  await showSavedSession(page, 0);
  await expect(page.locator('.virtual-range__club')).toHaveText('9 Iron');
  await expect
    .poll(() =>
      page.evaluate((after) => window.__rangeWorld!.frames.some(({ at, ball }) => ball && at > after), openedAt)
    )
    .toBe(true);
  await settled(page);
  expect(await worldTurn(page)).toBe(0);
  expect(await greenHighlighted(page)).toBe(false);
  const yard = 0.9144;
  const dots = await dotsInWorld(page);
  expect(dots).toHaveLength(2);
  expect(dots[0].x).toBeCloseTo(0, 3);
  expect(dots[0].z).toBeCloseTo(-148 * yard, 3);
  expect(dots[1].x).toBeCloseTo(-3 * yard, 3);
  expect(dots[1].z).toBeCloseTo(-121 * yard, 3);

  await tapFlag(page, 1);
  await page.waitForTimeout(700);
  expect(await storedTarget(page)).toBe(TARGET);
  expect(await worldTurn(page)).toBe(0);

  await page.getByRole('button', { name: 'Back to today' }).tap();
  await turnedTo(page, TARGET);
  expect(await greenHighlighted(page)).toBe(true);
});

const RESTING_SHOT = {
  timestamp: '2026-10-09T10:00:00.000000',
  club: 'driver',
  ball_speed_mph: 120.4,
  carry_spin_adjusted: 150,
  carry_lateral_yards: 5,
  flight_path: { unit: 'yd', points: wireFlight(150, 5) },
  roll_path: {
    unit: 'yd',
    points: [
      [3.5, 150, 5, 0],
      [4.5, 157, 5.4, 0],
      [5.5, 160, 5.5, 0],
    ],
  },
  total_yards: 160,
};
const RESTING_SHOT_REST_S = 0.5 + 3.5 + 2;

function deliverFinal(page: Page, shot: Record<string, unknown>): Promise<number> {
  return page.evaluate(async (shot) => {
    const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
    const { handleShotMessage } = await load('services/handleShotMessage');
    const { useProfileStore } = await load('stores/useProfileStore');
    handleShotMessage({ shot: { ...shot, profile_id: useProfileStore.getState().activeProfileId }, stats: {} });
    return performance.now();
  }, shot);
}

function onOneLine(page: Page): Promise<boolean> {
  return page.locator('.virtual-range__bar > *').evaluateAll((elements) => {
    const middles = elements
      .filter((element) => element.getBoundingClientRect().width > 0)
      .map((element) => {
        const { top, bottom } = element.getBoundingClientRect();
        return (top + bottom) / 2;
      });
    return Math.max(...middles) - Math.min(...middles) <= 2;
  });
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`the ball at rest at ${viewport.width}×${viewport.height} in German`, () => {
    test.use({ viewport });

    test('a line with its distance runs to the nearest flag, in view between the bars, and the bar holds it on one line', async ({
      page,
    }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await gotoApp(page);
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
      await spyOnRangeCamera(page);
      await spyOnRangeWorld(page);
      await rangeButton(page).tap();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);
      await spyOnFillText(page);

      const at = await deliverFinal(page, RESTING_SHOT);
      const value = (kind: string) => page.locator(`.virtual-range__stat--${kind} .virtual-range__value`);
      await expect(value('total'), 'no Total while the ball flies').toHaveText('—');
      await expect(value('target')).toHaveText('—');
      expect(await page.evaluate(() => window.__rangeWorld!.target())).toEqual({ line: false, label: false });

      await expect(value('total')).toHaveText('160yds', { timeout: LANDED_TIMEOUT_MS });
      const yard = 0.9144;
      const rest = { x: 5.5 * yard, y: 0, z: -160 * yard };
      const flag = GREENS.map((_, index) => baseOf(index)).reduce((best, base) =>
        Math.hypot(base.x - rest.x, base.z - rest.z) < Math.hypot(best.x - rest.x, best.z - rest.z) ? base : best
      );
      const yards = Math.round(Math.hypot(flag.x - rest.x, flag.z - rest.z) / yard);
      await expect(value('target')).toHaveText(`${yards}yds`);
      expect(await targetLabels(page)).toContain(`${yards} yds`);
      expect(await fillTexts(page), 'the sign at the rest point shows the Total').toContain('160 yds');

      await page.waitForFunction((until) => performance.now() >= until, at + (RESTING_SHOT_REST_S + 1.8) * 1000);
      expect(await page.evaluate(() => window.__rangeWorld!.target())).toEqual({ line: true, label: true });
      const top = await page.evaluate(() => document.querySelector('.range-app__top')!.getBoundingClientRect().bottom);
      const barTop = await page.evaluate(
        () => document.querySelector('.virtual-range__bar')!.getBoundingClientRect().top
      );
      const middle = { x: (rest.x + flag.x) / 2, y: 0, z: (rest.z + flag.z) / 2 };
      for (const [name, point] of Object.entries({
        rest,
        flag,
        middle,
        landing: { x: 5 * yard, y: 0, z: -150 * yard },
      })) {
        const { x, y } = await onScreen(page, point);
        expect(y, `${name} under the top chrome`).toBeGreaterThan(top);
        expect(y, `${name} above the bar`).toBeLessThan(barTop);
        expect(x, name).toBeGreaterThan(0);
        expect(x, name).toBeLessThan(viewport.width);
      }
      const pxPerUnit = await page.evaluate(
        () => window.innerHeight / 2 / Math.tan((window.__rangeCamera!.fov * Math.PI) / 360)
      );
      const totalSignFoot = await onScreen(page, { ...rest, y: 1.6 });
      expect(totalSignFoot.y - 0.17 * (132 / 300) * pxPerUnit, 'the Total sign under the top chrome').toBeGreaterThan(
        top
      );
      const labelTop = await onScreen(page, middle);
      expect(labelTop.y + 0.12 * (120 / 300) * pxPerUnit, 'the distance label above the bar').toBeLessThan(barTop);

      expect(await onOneLine(page), `the figures of the bar on one line`).toBe(true);
      await expectOverlaysInside(page);
      const speed = page.locator('.virtual-range__stat--speed');
      await expect(speed, 'ball speed stays in the bar on every kiosk screen').toBeVisible();
      await showWidestFigures(page);
      await page.evaluate(() => (document.querySelector('.virtual-range__club')!.textContent = 'Pitching Wedge'));
      expect(await onOneLine(page), `the widest figures on one line`).toBe(true);
      await expectOverlaysInside(page);
      await page.locator('.virtual-range__delete').tap();
      await expect(page.locator('.virtual-range__delete')).toHaveText('Wirklich löschen?');
      expect(await onOneLine(page), `the widest figures on one line while the delete asks to confirm`).toBe(true);
      await expectOverlaysInside(page);
      await expect(speed).toBeVisible();

      await page.setViewportSize({ width: 640, height: viewport.height });
      await expect(speed, 'where the bar is short of room, ball speed leaves it first').toBeHidden();
      expect(await onOneLine(page), `the widest figures on one line`).toBe(true);
      expect(await clippedText(page, '.virtual-range__club, .virtual-range__value')).toEqual([]);
    });
  });
}

async function signsOnTheBars(page: Page): Promise<string[]> {
  const signs = await page.evaluate(() => window.__rangeWorld!.signRects());
  const bars = await page.locator('.virtual-range__bar, .virtual-range__top').evaluateAll((elements) =>
    elements.map((element) => {
      const { left, top, right, bottom } = element.getBoundingClientRect();
      return { name: element.className, left, top, right, bottom };
    })
  );
  expect(bars, 'the shot bar and the bar of the saved session').toHaveLength(2);
  expect(signs.length, 'signs to check').toBeGreaterThan(0);
  return signs.flatMap((sign) =>
    bars
      .filter((bar) => sign.left < bar.right && sign.right > bar.left && sign.top < bar.bottom && sign.bottom > bar.top)
      .map((bar) => `sign at ${sign.name} × ${bar.name}`)
  );
}

for (const viewport of KIOSK_VIEWPORTS) {
  test.describe(`signs and the range's own bars at ${viewport.width}×${viewport.height} in German`, () => {
    test.use({ viewport });

    test('no sign draws on the shot bar or the bar of a saved session, at rest or back at the tee', async ({
      page,
    }) => {
      await mockSessionHistory(page, SAVED_SESSIONS);
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await gotoApp(page);
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();
      await spyOnRangeCamera(page);
      await spyOnRangeWorld(page);
      await rangeButton(page).tap();
      await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
      await expect.poll(() => sceneAndCameraSeen(page)).toBe(true);

      await footerButton(page, 'Verlauf').tap();
      await page.locator('.session-history__session').nth(0).tap();
      const showOnRange = page.getByRole('button', { name: 'Auf der Range zeigen', exact: true });
      await expect(showOnRange).toBeVisible();
      const at = await page.evaluate(() => performance.now());
      await showOnRange.tap();
      await expect(page.locator('.virtual-range__session')).toBeVisible();
      await expect(page.locator('.virtual-range__club')).toHaveText('Eisen 9');

      await page.waitForFunction((until) => performance.now() >= until, at + (0.5 + 3.6 + 1.2 + 0.7) * 1000);
      expect(await signsOnTheBars(page), 'at rest').toEqual([]);
      await settled(page);
      expect(await signsOnTheBars(page), 'back at the tee').toEqual([]);
    });
  });
}
