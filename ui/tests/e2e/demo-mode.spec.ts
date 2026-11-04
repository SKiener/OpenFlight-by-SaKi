import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  canvases,
  clippedText,
  covered,
  menuBack,
  openMenuArea,
  expectLandedAtLatestCarry,
  expectOverlaysInside,
  fillTexts,
  gotoApp,
  KIOSK_VIEWPORTS,
  mockSessionHistory,
  openApp,
  openRange,
  outsideScreen,
  overflowingLiveMetricMetadata,
  overflowingLiveMetricValues,
  overlapping,
  PHONE_VIEWPORTS,
  rangeButton,
  resetSession,
  resetUnits,
  savedShot,
  SCENE_TIMEOUT_MS,
  setClub,
  setDemo,
  showSavedSession,
  simulateShot,
  smallTargets,
  spyOnFillText,
  waitForEvent,
  WIDEST_LIVE_METRIC_VALUE,
  withControlSocket,
  withShotStore,
  type MockedSession,
} from './helpers';

// playwright.config.ts starts the backend with --demo-interval-s 2: after the six starters a demo shot comes every 2 s.
const STARTERS = 6;
const BAND = '.demo-band';
const BAND_TEXT = 'DEMO – real shots are ignored';
const TILES = '.range-tile .metric-card';
/** The kiosk's own Live tiles (127.0.0.1 is the Pi's screen: KioskLiveOverview). */
const KIOSK_TILES = '.kiosk-tile';
const SHOT_WAIT_MS = 10_000;
const STARTER_NUMBERS = Array.from({ length: STARTERS }, (_, i) => i + 1);

interface AppShot {
  club: string;
  demo?: boolean;
  shot_number: number | null;
  timestamp: string;
  ball_speed_mph: number;
  carry_spin_adjusted: number | null;
}

interface BarEntry {
  club: string | null;
  carry: number | null;
  speed: string | null;
  saved: boolean;
}

interface BarShot {
  club: string;
  speed: string;
  carry: number;
  sign: string;
}

interface SessionSwap {
  demo: boolean | null;
  numbers: Array<number | null>;
  demoShots: number;
}

declare global {
  interface Window {
    __bar?: BarEntry[];
    __barObserver?: MutationObserver;
    __sessionSwaps?: SessionSwap[];
    __sessionMessages?: number;
  }
}

const SAVED: MockedSession = {
  summary: {
    id: 'session_20260930_180000_range',
    started_at: '2026-09-30T18:00:00+02:00',
    shot_count: 1,
    clubs: ['9-iron'],
  },
  shots: [
    savedShot('2026-09-30T18:00:40.000000', {
      shot_number: 1,
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
};

const menu = (page: Page) => page.locator('.menu-sheet');
const band = (page: Page) => page.locator(BAND);
const endButton = (page: Page, name = 'End') => band(page).getByRole('button', { name, exact: true });
const exportButton = (page: Page) => page.getByRole('button', { name: 'Export CSV', exact: true });

const tap = (target: Locator) => target.tap();
const click = (target: Locator) => target.click();

async function inMenu(page: Page, act: (demoSwitch: Locator) => Promise<void>, press: typeof tap = tap): Promise<void> {
  await press(page.getByRole('button', { name: 'Open menu' }));
  await expect(menu(page)).toBeVisible();
  // The demo switch lives in the menu's System area; Close is on the start page.
  await openMenuArea(page, 'System', { tap: press === tap });
  await act(menu(page).getByRole('button', { name: 'Demo mode', exact: true }));
  await menuBack(page, { tap: press === tap });
  await press(menu(page).getByRole('button', { name: 'Close', exact: true }));
  await expect(menu(page)).toHaveCount(0);
}

async function openPanel(page: Page, name: 'Shots' | 'Stats'): Promise<void> {
  await page.getByRole('button', { name: 'Open menu' }).tap();
  await openMenuArea(page, 'Analysis', { tap: true });
  await menu(page).locator('.range-tools').getByRole('button', { name, exact: true }).tap();
}

const appShots = (page: Page): Promise<AppShot[]> => withShotStore(page, (store) => store.getState().shots);

const appShotCount = async (page: Page): Promise<number> => (await appShots(page)).length;

async function startDemo(page: Page): Promise<void> {
  await inMenu(page, async (demoSwitch) => {
    await demoSwitch.tap();
    await expect(demoSwitch).toHaveAttribute('aria-pressed', 'true');
  });
  await expect(band(page)).toBeVisible();
  await expect.poll(() => appShotCount(page), { timeout: SHOT_WAIT_MS }).toBeGreaterThanOrEqual(STARTERS);
}

async function recordSessionSwaps(page: Page): Promise<void> {
  await expect.poll(() => withShotStore(page, (store) => store.getState().sessionGeneration)).toBeGreaterThan(0);
  await withShotStore(page, (store) => {
    const swaps: SessionSwap[] = [];
    window.__sessionSwaps = swaps;
    let seen = store.getState().sessionGeneration;
    store.subscribe((state: { sessionGeneration: number; sessionDemo: boolean | null; shots: AppShot[] }) => {
      if (state.sessionGeneration === seen) return;
      seen = state.sessionGeneration;
      swaps.push({
        demo: state.sessionDemo,
        numbers: state.shots.map((shot) => shot.shot_number),
        demoShots: state.shots.filter((shot) => shot.demo === true).length,
      });
    });
  });
}

const sessionSwaps = (page: Page): Promise<SessionSwap[]> => page.evaluate(() => window.__sessionSwaps ?? []);

function countSessionMessages(page: Page): Promise<void> {
  return withShotStore(page, (store) => {
    window.__sessionMessages = 0;
    const receive = store.getState().receiveSession;
    store.setState({
      receiveSession: (...args: unknown[]) => {
        window.__sessionMessages = (window.__sessionMessages ?? 0) + 1;
        receive(...args);
      },
    });
  });
}

const sessionMessages = (page: Page): Promise<number> => page.evaluate(() => window.__sessionMessages ?? 0);

function recordBar(page: Page): Promise<void> {
  return page.evaluate(() => {
    window.__barObserver?.disconnect();
    const entries: BarEntry[] = [];
    window.__bar = entries;
    const text = (selector: string) => document.querySelector(selector)?.textContent ?? null;
    const read = (): BarEntry => {
      const carry = parseInt(text('.virtual-range__stat--carry .virtual-range__value') ?? '', 10);
      return {
        club: text('.virtual-range__club'),
        carry: Number.isFinite(carry) ? carry : null,
        speed: text('.virtual-range__stat--speed .virtual-range__value'),
        saved: document.querySelector('.virtual-range__session') !== null,
      };
    };
    const observer = new MutationObserver(() => entries.push(read()));
    observer.observe(document.querySelector('.virtual-range')!, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    window.__barObserver = observer;
  });
}

const barTimeline = (page: Page): Promise<BarEntry[]> => page.evaluate(() => window.__bar ?? []);

function barShot(page: Page, index: number): Promise<BarShot> {
  return withShotStore(
    page,
    async (store, index) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      const { getCarryYards } = await load('types/shot');
      const { getClubName } = await load('data/clubs');
      const { formatDistance, formatSpeed, getDistanceUnit } = await load('utils/units');
      const { shownTotalM, toRangeShot, YARD_M } = await load('virtual-range/flightPath');
      const shot = store.getState().shots.at(index);
      const rangeShot = toRangeShot(shot);
      const signM = (rangeShot && shownTotalM(rangeShot)) ?? getCarryYards(shot) * YARD_M;
      return {
        club: getClubName(shot.club),
        speed: `${formatSpeed(shot.ball_speed_mph, 'imperial')}mph`,
        carry: Math.round(getCarryYards(shot)),
        sign: `${formatDistance(signM / YARD_M, 'imperial')} ${getDistanceUnit('imperial')}`,
      };
    },
    index
  );
}

const barOf = (timeline: BarEntry[], shot: BarShot): BarEntry[] =>
  timeline.filter((entry) => entry.club === shot.club && entry.speed === shot.speed);

const carriesOf = (entries: BarEntry[]): number[] => entries.flatMap((entry) => entry.carry ?? []);

const isAscending = (values: number[]): boolean => values.every((value, i) => i === 0 || value >= values[i - 1]);

async function firstDemoShot(page: Page): Promise<BarShot> {
  await expect.poll(() => appShotCount(page), { timeout: SHOT_WAIT_MS }).toBeGreaterThan(STARTERS);
  return barShot(page, STARTERS);
}

async function expectFlying(page: Page, shot: BarShot): Promise<void> {
  await expect
    .poll(async () => carriesOf(barOf(await barTimeline(page), shot)).some((carry) => carry > 0), {
      message: `${shot.club} flies on the range`,
      timeout: SHOT_WAIT_MS,
    })
    .toBe(true);
  const flight = carriesOf(barOf(await barTimeline(page), shot));
  expect(flight[0], `a flight from the start: ${flight}`).toBeLessThan(shot.carry * 0.2);
  expect(isAscending(flight), `one count-up: ${flight}`).toBe(true);
}

function countSentEvents(page: Page, event: string): () => number {
  let count = 0;
  page.on('websocket', (socket) => {
    socket.on('framesent', ({ payload }) => {
      if (String(payload).startsWith(`42["${event}"`)) count += 1;
    });
  });
  return () => count;
}

async function expectBandInside(page: Page): Promise<void> {
  expect(await outsideScreen(page, `${BAND}, ${BAND} button`)).toEqual([]);
  expect(await clippedText(page, `${BAND} .demo-band__text, ${BAND} button`)).toEqual([]);
  expect(await smallTargets(page, `${BAND} button`)).toEqual([]);
}

async function expectBandFits(page: Page, text = BAND_TEXT, end = 'End'): Promise<void> {
  await expect(band(page)).toContainText(text);
  await expect(endButton(page, end)).toBeVisible();
  await expectBandInside(page);
  expect(await overlapping(page, BAND, `.range-header, .range-footer, .kiosk-live__header, ${TILES}, ${KIOSK_TILES}`)).toEqual([]);
  expect(await covered(page, '.range-footer button')).toEqual([]);
}

function freezeTilesOnWidestShot(page: Page): Promise<void> {
  return withShotStore(
    page,
    (store, spin) => {
      const { shots, addShot } = store.getState();
      store.setState({ addShot: () => undefined });
      addShot({
        ...shots.at(-1),
        timestamp: new Date().toISOString(),
        ball_speed_mph: 199.9,
        carry_spin_adjusted: 349,
        spin_rpm: spin,
        carry_spin_rpm: spin,
      });
    },
    Number(WIDEST_LIVE_METRIC_VALUE.replace(/\D/g, ''))
  );
}

async function expectLiveTilesFit(page: Page): Promise<void> {
  await expect.poll(() => page.locator(`${TILES}, ${KIOSK_TILES}`).count()).toBeGreaterThanOrEqual(5);
  if ((await page.locator(KIOSK_TILES).count()) > 0) {
    await expect.poll(() => clippedText(page, '.kiosk-tile__value-row')).toEqual([]);
    await expect.poll(() => clippedText(page, '.kiosk-tile__title')).toEqual([]);
    return;
  }
  await expect.poll(() => overflowingLiveMetricValues(page, TILES)).toEqual([]);
  await expect.poll(() => overflowingLiveMetricMetadata(page, TILES)).toEqual([]);
  await expect.poll(() => clippedText(page, `${TILES} .metric-card__value`)).toEqual([]);
}

test.beforeEach(async () => {
  await withControlSocket(async (socket) => {
    await setDemo(socket, false);
    await resetSession(socket);
    await resetUnits(socket);
    await setClub(socket, 'driver');
  });
});

// A demo left running would swallow the real shots of every spec after this one.
test.afterEach(async () => {
  await withControlSocket((socket) => setDemo(socket, false));
});

for (const viewport of [...KIOSK_VIEWPORTS, PHONE_VIEWPORTS[0]]) {
  const phone = viewport.width < 600;

  test.describe(`demo mode at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, hasTouch: true, isMobile: phone });

    test('the menu starts the demo with six shots and the band', async ({ page }) => {
      await openApp(page);
      await expect(band(page)).toHaveCount(0);
      await recordSessionSwaps(page);

      await startDemo(page);

      await expectBandFits(page);
      await expectLiveTilesFit(page);
      expect(await sessionSwaps(page)).toEqual([{ demo: true, numbers: STARTER_NUMBERS, demoShots: STARTERS }]);

      await openPanel(page, 'Shots');
      const listedMinusHeld = () =>
        withShotStore(
          page,
          (store) => document.querySelectorAll('.shots-panel__row').length - store.getState().shots.length
        );
      await expect.poll(listedMinusHeld).toBe(0);
      expect(await page.locator('.shots-panel__row').count()).toBeGreaterThanOrEqual(STARTERS);

      await openPanel(page, 'Stats');
      const all = page.getByRole('button', { name: /^All \(\d+\)$/ });
      await all.tap();
      const countedByChipAndTile = () =>
        page.evaluate(() => {
          const chip = [...document.querySelectorAll('.stats-panel__chips button')].find((button) =>
            /^All/.test(button.textContent ?? '')
          );
          const tile = document.querySelector('.stats-panel__grid .metric-card__value');
          return [Number(chip?.textContent?.match(/\d+/)?.[0]), Number(tile?.textContent)];
        });
      await expect.poll(async () => new Set(await countedByChipAndTile()).size).toBe(1);
      expect((await countedByChipAndTile())[0]).toBeGreaterThanOrEqual(STARTERS);
      await expect(band(page)).toBeVisible();
    });

    if (!phone) {
      test('a demo shot flies on the range', async ({ page }) => {
        await openRange(page);
        await spyOnFillText(page);
        await recordBar(page);

        await startDemo(page);

        const starter = await barShot(page, STARTERS - 1);
        const next = await firstDemoShot(page);
        await expectFlying(page, next);
        expect(next.club, 'a new club').not.toBe(starter.club);
        expect(next.speed, 'a new ball speed').not.toBe(starter.speed);
        await expect(canvases(page)).toHaveCount(1);
        expect(await fillTexts(page)).toContain(next.sign);

        await expectBandFits(page);
        await expectOverlaysInside(page);
        expect(await overlapping(page, BAND, '.virtual-range__bar, .virtual-range__hint, .simulate-bubble')).toEqual(
          []
        );
      });
    }

    test('a real shot during the demo is ignored', async ({ page }) => {
      await openApp(page);
      await startDemo(page);

      // The next shot the server sends is a demo shot; it also comes after the hit has been dealt with.
      const afterTheHit = await withControlSocket(async (socket) => {
        const next = waitForEvent<{ shot: AppShot }>(socket, 'shot', SHOT_WAIT_MS);
        socket.emit('simulate_shot');
        return (await next).shot;
      });
      expect(afterTheHit.demo, 'the hit made no shot of its own').toBe(true);
      await expect
        .poll(async () => (await appShots(page)).some((shot) => shot.timestamp === afterTheHit.timestamp))
        .toBe(true);

      const shots = await appShots(page);
      expect(shots.every((shot) => shot.demo === true)).toBe(true);
      expect(shots.map((shot) => shot.shot_number)).toEqual(shots.map((_, i) => i + 1));
      await expect(band(page)).toBeVisible();

      await endButton(page).tap();

      await expect(band(page)).toHaveCount(0);
      await expect.poll(() => appShotCount(page)).toBe(0);
      await openPanel(page, 'Shots');
      await expect(page.locator('.shots-panel__row')).toHaveCount(0);
    });

    test('ending the demo brings the real shots back', async ({ page }) => {
      await withControlSocket(async (socket) => {
        await simulateShot(socket);
      });
      await openApp(page);
      await expect.poll(() => appShotCount(page)).toBe(1);
      const [real] = await appShots(page);
      expect(real.demo).toBeFalsy();

      await startDemo(page);
      await expectBandFits(page);
      await openPanel(page, 'Shots');
      await expect(exportButton(page), 'no demo row is exported').toBeDisabled();
      await page.locator('.shots-panel__row-main').first().tap();
      const locked = page.locator('.shots-panel__validation').locator('input:disabled, select:disabled');
      await expect(locked, 'or kept as a note').toHaveCount(3);

      await endButton(page).tap();

      await expect(band(page)).toHaveCount(0);
      await expect.poll(() => appShots(page)).toEqual([expect.objectContaining({ timestamp: real.timestamp })]);
      const [back] = await appShots(page);
      expect(back).toMatchObject({
        club: real.club,
        ball_speed_mph: real.ball_speed_mph,
        shot_number: real.shot_number,
        carry_spin_adjusted: real.carry_spin_adjusted,
      });
      expect(back.demo).toBeFalsy();
      await openPanel(page, 'Shots');
      await expect(page.locator('.shots-panel__row')).toHaveCount(1);
      await expect(exportButton(page)).toBeEnabled();
    });
  });
}

for (const viewport of [PHONE_VIEWPORTS[0], KIOSK_VIEWPORTS[0]]) {
  test.describe(`in German at ${viewport.width}×${viewport.height}`, () => {
    test.use({ viewport, hasTouch: true, isMobile: viewport.width < 600 });

    test('the band, its sentence and Beenden fit', async ({ page }) => {
      await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
      await withControlSocket((socket) => setDemo(socket, true));
      await gotoApp(page);
      await page.getByRole('button', { name: 'Schläger wählen schließen' }).tap();

      await expectBandFits(page, 'DEMO – echte Schläge werden ignoriert', 'Beenden');
    });
  });
}

test.describe('on the range', () => {
  test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });
  test.describe.configure({ timeout: 90_000 });

  test('switching the demo on and off animates nothing; the next demo shot flies once', async ({ page }) => {
    await withControlSocket((socket) => setClub(socket, '7-iron'));
    await openRange(page);
    await page.getByRole('button', { name: 'Simulate shot' }).tap();
    await expectLandedAtLatestCarry(page);
    const real = await barShot(page, -1);

    await spyOnFillText(page);
    await recordBar(page);
    await startDemo(page);
    const starter = await barShot(page, STARTERS - 1);
    const next = await firstDemoShot(page);
    expect(new Set([real.club, starter.club, next.club]).size, 'shots told apart by their club').toBe(3);
    await expectFlying(page, next);

    const timeline = await barTimeline(page);
    const atRest = carriesOf(barOf(timeline, starter));
    expect(atRest.length, 'the starters were on the range').toBeGreaterThan(0);
    expect(atRest, 'the starters stand at their carry, no count-up').toEqual(atRest.map(() => starter.carry));
    expect((await fillTexts(page)).slice(0, 2), 'a sign for the starters at rest, then for the flying shot').toEqual([
      starter.sign,
      next.sign,
    ]);

    await endButton(page).tap();
    await expect(band(page)).toHaveCount(0);
    await expect(page.locator('.virtual-range__club')).toHaveText(real.club);
    await expect(page.locator('.virtual-range__stat--carry .virtual-range__value')).toHaveText(`${real.carry}yds`);
    const backAtRest = carriesOf(barOf(await barTimeline(page), real));
    expect(backAtRest.length, 'the real shot is back on the range').toBeGreaterThan(0);
    expect(backAtRest, 'at rest, it does not fly again').toEqual(backAtRest.map(() => real.carry));
    expect((await fillTexts(page)).at(-1)).toBe(real.sign);
  });

  test('a saved session stays when the demo starts; the first demo shot takes the range back to today', async ({
    page,
  }) => {
    await mockSessionHistory(page, [SAVED]);
    await openRange(page);
    await showSavedSession(page, 0);
    await expect(page.locator('.virtual-range__session')).toBeVisible();
    await recordBar(page);

    await startDemo(page);
    const starter = await barShot(page, STARTERS - 1);
    const next = await firstDemoShot(page);
    await expectFlying(page, next);

    const timeline = await barTimeline(page);
    expect(barOf(timeline, starter), 'the starters never showed on the range').toEqual([]);
    const takenBack = timeline.findIndex((entry) => !entry.saved);
    expect(takenBack, 'the saved session went').toBeGreaterThan(-1);
    expect(timeline.slice(0, takenBack).every((entry) => entry.club === '9 Iron')).toBe(true);
    expect(timeline[takenBack], 'with the first demo shot').toMatchObject({ club: next.club, speed: next.speed });
    await expect(page.locator('.virtual-range__session')).toHaveCount(0);
  });

  test('a screen joining while a shot flies leaves the flight alone', async ({ page, context }) => {
    await openRange(page);
    const joiner = await context.newPage();
    await joiner.goto('/companion');
    await expect(joiner.locator('.companion__connection.is-connected')).toBeVisible();

    await spyOnFillText(page);
    await recordBar(page);
    await recordSessionSwaps(page);
    await countSessionMessages(page);
    await page.getByRole('button', { name: 'Simulate shot' }).tap();
    await expect.poll(() => appShotCount(page)).toBe(1);
    const shot = await barShot(page, 0);
    await expect
      .poll(async () => (await barTimeline(page)).at(-1)?.carry ?? 0, { timeout: SCENE_TIMEOUT_MS })
      .toBeGreaterThan(shot.carry * 0.25);

    const messages = await sessionMessages(page);
    await joiner.evaluate(async () => {
      const service = '/src/services/socketService.ts';
      const { socketService } = await import(/* @vite-ignore */ service);
      socketService.disconnect();
      socketService.connect();
    });
    await expect.poll(() => sessionMessages(page)).toBeGreaterThan(messages);

    expect(await expectLandedAtLatestCarry(page)).toBe(shot.carry);
    const series = carriesOf(await barTimeline(page));
    expect(isAscending(series), `one count-up, no restart: ${series}`).toBe(true);
    expect(series.at(-1)).toBe(shot.carry);
    expect(await fillTexts(page), 'its sign was drawn once').toEqual([shot.sign]);
    expect(await sessionSwaps(page), 'a screen joining is not a new session').toEqual([]);
  });

  test('a shot waiting for the scene still flies after a screen joins', async ({ page, context }) => {
    let releaseScene!: () => void;
    const sceneHeld = new Promise<void>((resolve) => (releaseScene = resolve));
    await page.route('**/virtual-range/scene/createRangeScene.ts*', async (route) => {
      await sceneHeld;
      await route.continue();
    });
    await openApp(page);
    await recordSessionSwaps(page);
    await countSessionMessages(page);
    await rangeButton(page).tap();
    await expect(page.locator('.virtual-range__notice')).toBeVisible();
    await recordBar(page);

    await withControlSocket(async (socket) => {
      await simulateShot(socket);
    });
    await expect.poll(() => appShotCount(page)).toBe(1);
    const messages = await sessionMessages(page);
    const joiner = await context.newPage();
    await joiner.goto('/companion');
    await expect(joiner.locator('.companion__connection.is-connected')).toBeVisible();
    await expect.poll(() => sessionMessages(page)).toBeGreaterThan(messages);

    releaseScene();
    await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
    const carry = await expectLandedAtLatestCarry(page);
    const series = carriesOf(await barTimeline(page));
    const start = series.indexOf(0);
    expect(start, `it flew once the scene was up: ${series}`).toBeGreaterThan(-1);
    expect(isAscending(series.slice(start)), `one count-up: ${series}`).toBe(true);
    expect(series.at(-1)).toBe(carry);
    expect(await sessionSwaps(page)).toEqual([]);
  });
});

test.describe('the club face of demo shots', () => {
  test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });

  /** A zone word, or "uncertain" when the sigma leaves the zone open: never "—". */
  const ZONE_WORD = /^(Heel|Center|Toe|uncertain)$/;

  test('the impact point reaches the Live tile and All values → Club', async ({ page }) => {
    await withControlSocket((socket) => setDemo(socket, true));
    await page.addInitScript(() =>
      localStorage.setItem('openflight.kiosk-live-slots:v1', JSON.stringify(['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve']))
    );
    await openApp(page);
    await expect(band(page)).toBeVisible();
    await expect.poll(() => appShotCount(page), { timeout: SHOT_WAIT_MS }).toBeGreaterThanOrEqual(STARTERS);

    // Every demo shot carries the face data of a camera shot, method D.
    const faces = await withShotStore(page, (store) =>
      store.getState().shots.map((shot: Record<string, unknown>) => ({
        method: shot.impact_method,
        offset: shot.impact_offset_mm,
        f2p: shot.face_to_path_class,
      }))
    );
    for (const face of faces) {
      expect(face.method).toBe('D');
      expect(typeof face.offset).toBe('number');
      expect(['closed', 'neutral', 'open']).toContain(face.f2p);
    }

    // The Live tile: the face with a dot (a ring when the zone is open) over the zone word.
    const tile = page.locator('.kiosk-tile--impact');
    await expect(tile.locator('.kiosk-tile__value')).toHaveText(ZONE_WORD);
    await expect(tile.locator('svg.impact-face')).toHaveAttribute('data-mark', /^(dot|ring)$/);

    // All values → Club: the fourth card, its face and its word.
    await page.locator('.range-details-button').tap();
    await page.locator('.range-details-toolbar').getByRole('button', { name: 'Club', exact: true }).tap();
    const card = page.locator('.club-data__grid .metric-card[data-metric="cd_impact"]');
    await expect(card.locator('.metric-card__value')).toHaveText(ZONE_WORD);
    await expect(card.locator('svg.impact-face')).toHaveAttribute('data-mark', /^(dot|ring)$/);
  });
});

test.describe('at 800×400 with the band', () => {
  test.use({ viewport: { width: 800, height: 400 }, hasTouch: true });

  test('no Live tile value or hero label is clipped, even with the widest figures', async ({ page }) => {
    await withControlSocket((socket) => setDemo(socket, true));
    // The kiosk's tiles with spin among them (its default shows the curve instead).
    await page.addInitScript(() =>
      localStorage.setItem('openflight.kiosk-live-slots:v1', JSON.stringify(['carry', 'ball_speed', 'spin', 'launch_h', 'launch_v']))
    );
    await openApp(page);
    await expect(band(page)).toBeVisible();
    await expectLiveTilesFit(page);

    await freezeTilesOnWidestShot(page);
    await expect(page.locator('.kiosk-tile--spin .kiosk-tile__value')).toHaveText(WIDEST_LIVE_METRIC_VALUE);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

    expect(await clippedText(page, '.kiosk-tile__value-row')).toEqual([]);
    expect(await clippedText(page, '.kiosk-tile__title')).toEqual([]);
    expect(await overlapping(page, BAND, KIOSK_TILES)).toEqual([]);
  });
});

test.describe('the switch and the band', () => {
  test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });

  test('the menu switch and End turn the demo on, off and on again', async ({ page, context }) => {
    await openApp(page);
    await recordSessionSwaps(page);

    await inMenu(
      page,
      async (demoSwitch) => {
        await expect(demoSwitch).toHaveAttribute('aria-pressed', 'false');
        await demoSwitch.click();
        await expect(demoSwitch).toHaveAttribute('aria-pressed', 'true');
      },
      click
    );
    await expect(band(page)).toBeVisible();
    await expect.poll(() => appShotCount(page), { timeout: SHOT_WAIT_MS }).toBeGreaterThanOrEqual(STARTERS);

    const joiner = await context.newPage();
    await openApp(joiner);
    await expect(band(joiner)).toBeVisible();
    await inMenu(joiner, (demoSwitch) => expect(demoSwitch).toHaveAttribute('aria-pressed', 'true'), click);
    await joiner.close();

    await inMenu(
      page,
      async (demoSwitch) => {
        await expect(demoSwitch).toHaveAttribute('aria-pressed', 'true');
        await demoSwitch.click();
        await expect(demoSwitch).toHaveAttribute('aria-pressed', 'false');
      },
      click
    );
    await expect(band(page)).toHaveCount(0);
    await expect.poll(() => appShotCount(page)).toBe(0);

    await inMenu(
      page,
      async (demoSwitch) => {
        await demoSwitch.click();
        await expect(demoSwitch).toHaveAttribute('aria-pressed', 'true');
      },
      click
    );
    await expect(band(page)).toBeVisible();
    await expect.poll(() => appShotCount(page), { timeout: SHOT_WAIT_MS }).toBeGreaterThanOrEqual(STARTERS);

    await endButton(page).click();
    await expect(band(page)).toHaveCount(0);
    await inMenu(page, (demoSwitch) => expect(demoSwitch).toHaveAttribute('aria-pressed', 'false'), click);

    const demoStart = { demo: true, numbers: STARTER_NUMBERS, demoShots: STARTERS };
    const realAgain = { demo: false, numbers: [], demoShots: 0 };
    expect(await sessionSwaps(page), 'each start is a fresh demo from shot 1, each end the real session').toEqual([
      demoStart,
      realAgain,
      demoStart,
      realAgain,
    ]);
  });

  test('in swing-speed mode the switch is off limits, with its hint', async ({ page }) => {
    await openApp(page);
    // The server says so in demo_state; the mock backend here runs in ball mode, so the store is told directly.
    await page.evaluate(async () => {
      const store = '/src/stores/useSystemStore.ts';
      const { useSystemStore } = await import(/* @vite-ignore */ store);
      useSystemStore.getState().setDemoState({ active: false, unavailable: 'swing-speed' });
    });

    await inMenu(page, async (demoSwitch) => {
      await expect(demoSwitch).toBeDisabled();
      await expect(menu(page)).toContainText('Ball mode only');
    });
    await expect(band(page)).toHaveCount(0);
  });
});

test.describe('the TV display', () => {
  test.use({ viewport: { width: 1024, height: 600 }, hasTouch: true });

  test('shows the band without End, and its touches do not keep the demo alive', async ({ page }) => {
    const touches = countSentEvents(page, 'demo_touch');
    const switches = countSentEvents(page, 'set_demo_mode');
    await withControlSocket((socket) => setDemo(socket, true));

    await gotoApp(page);
    await expect(band(page)).toBeVisible();
    await page.getByRole('button', { name: 'Close Select club' }).tap();
    await expect.poll(touches, { message: 'a touch on the kiosk keeps the demo alive' }).toBe(1);

    await gotoApp(page, '/display');
    await expect(band(page)).toBeVisible();
    await expect(band(page)).toContainText(BAND_TEXT);
    await expect(band(page).getByRole('button')).toHaveCount(0);
    await expectBandInside(page);

    await page.locator('.display-mode').tap();
    // A socket sends in order: once this switch-on (a no-op, the demo runs) is out, a ping from the tap would be too.
    await page.evaluate(async () => {
      const service = '/src/services/socketService.ts';
      const { socketService } = await import(/* @vite-ignore */ service);
      socketService.setDemoMode(true);
    });
    await expect.poll(switches).toBe(1);
    expect(touches(), 'the TV display only shows').toBe(1);
  });
});

test.describe('a phone', () => {
  test.use({ hasTouch: true });

  test('shows the band while the demo runs, also when it joins mid-demo, and its End ends the demo everywhere', async ({
    page,
    browser,
  }, testInfo) => {
    await openApp(page);
    const phone = await browser.newContext({
      baseURL: testInfo.project.use.baseURL,
      viewport: PHONE_VIEWPORTS[2],
      isMobile: true,
      hasTouch: true,
    });
    try {
      const early = await phone.newPage();
      const touches = countSentEvents(early, 'demo_touch');
      await early.goto('/companion');
      await expect(early.locator('.companion__connection.is-connected')).toBeVisible();
      await expect(band(early)).toHaveCount(0);

      await startDemo(page);
      await expect(band(early)).toBeVisible();
      const late = await phone.newPage();
      await late.goto('/companion');

      for (const companion of [early, late]) {
        await expect(companion.locator('main.companion > :first-child')).toHaveClass(/demo-band/);
        await expect(band(companion)).toContainText(BAND_TEXT);
        // "Shot" and its number are joined by a no-break space (range.shot).
        await expect(companion.locator('.companion__shot-heading strong')).toHaveText(/^Shot\u00a0\d+$/);
        await expectBandInside(companion);
      }
      for (const companion of [early, late]) {
        await companion.getByRole('button', { name: 'Dispersion' }).tap();
        await expect(companion.locator('main.companion > :first-child')).toHaveClass(/demo-band/);
      }
      await expect.poll(touches, { message: 'a touch on a phone keeps the demo alive' }).toBe(1);

      await endButton(late).tap();

      for (const screen of [early, late, page]) await expect(band(screen)).toHaveCount(0);
      await expect.poll(() => appShotCount(page)).toBe(0);
    } finally {
      await phone.close();
    }
  });
});
