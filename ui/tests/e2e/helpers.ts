import { expect, type Locator, type Page } from '@playwright/test';
import { io, type Socket } from 'socket.io-client';
import { TARGET_KEY } from '../../src/virtual-range/aim';
import { GREENS } from '../../src/virtual-range/layout';
import { FLAG_CLOTH, FLAG_HEIGHT_M } from '../../src/virtual-range/scene/greens';
import { TARGET_LABEL_CANVAS } from '../../src/virtual-range/scene/labelLayout';

// Overridable so a second e2e run can use its own ports (the defaults are the CI ports).
const UI_URL = process.env.E2E_UI_URL ?? 'http://127.0.0.1:5173';
const SOCKET_URL = process.env.E2E_SOCKET_URL ?? 'http://127.0.0.1:8080';

function connectSocket(): Promise<Socket> {
  return new Promise((resolve, reject) => {
    const socket = io(SOCKET_URL, {
      transports: ['websocket', 'polling'],
    });

    const onConnect = () => {
      socket.off('connect_error', onError);
      resolve(socket);
    };

    const onError = (error: Error) => {
      socket.off('connect', onConnect);
      socket.close();
      reject(error);
    };

    socket.once('connect', onConnect);
    socket.once('connect_error', onError);
  });
}

export async function withControlSocket<T>(run: (socket: Socket) => Promise<T>): Promise<T> {
  const socket = await connectSocket();

  try {
    return await run(socket);
  } finally {
    socket.close();
  }
}

export async function setClub(socket: Socket, club: string) {
  socket.emit('set_club', { club });
  await waitForEvent(socket, 'club_changed');
}

export async function simulateShot(socket: Socket) {
  socket.emit('simulate_shot');
  return waitForEvent(socket, 'shot');
}

export async function waitForEvent<T>(socket: Socket, event: string, timeoutMs = 5000): Promise<T> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.off(event, onEvent);
      reject(new Error(`Timed out waiting for ${event}`));
    }, timeoutMs);

    const onEvent = (payload: T) => {
      clearTimeout(timeout);
      socket.off(event, onEvent);
      resolve(payload);
    };

    socket.on(event, onEvent);
  });
}

/** Roster + shot state the mock server hands back from a `profiles` snapshot. */
interface ProfilesSnapshot {
  profiles: Array<{ id: string; name: string; settings?: { unit_system?: string } }>;
  active_profile_id: string;
}

/**
 * Resets the shared session between tests: clears every profile's shots, then
 * switches back to the seeded default profile and removes every other profile
 * (the backend keeps state across connections, so profiles added by one test
 * would otherwise leak into the next and collide on name). Removal is refused
 * while a profile still has session rows, so shots must be cleared first.
 */
function demoStateIs(socket: Socket, active: boolean, timeoutMs = 5000): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      socket.off('demo_state', onState);
      reject(new Error(`Timed out waiting for demo_state active=${active}`));
    }, timeoutMs);
    const onState = (state: { active: boolean }) => {
      if (state.active !== active) return;
      clearTimeout(timeout);
      socket.off('demo_state', onState);
      resolve();
    };
    socket.on('demo_state', onState);
  });
}

export async function setDemo(socket: Socket, enabled: boolean): Promise<void> {
  const settled = demoStateIs(socket, enabled);
  socket.emit('set_demo_mode', { enabled });
  await settled;
}

export async function resetSession(socket: Socket) {
  const snapshotPromise = waitForEvent<ProfilesSnapshot>(socket, 'profiles');
  socket.emit('get_profiles');
  const { profiles } = await snapshotPromise;

  const defaultProfile = profiles.find((profile) => profile.name === 'Profile 1') ?? profiles[0];
  if (!defaultProfile) return;

  for (const profile of profiles) {
    const cleared = waitForEvent(socket, 'session_cleared');
    socket.emit('clear_session', { profile_id: profile.id });
    await cleared;
  }

  if (profiles.length > 1) {
    const activated = waitForEvent(socket, 'profiles');
    socket.emit('set_active_profile', { profile_id: defaultProfile.id });
    await activated;

    for (const profile of profiles) {
      if (profile.id === defaultProfile.id) continue;
      const removed = waitForEvent(socket, 'profiles');
      socket.emit('remove_profile', { profile_id: profile.id });
      await removed;
    }
  }
}

/**
 * Units are saved per profile on the server, so a test that switched them puts
 * them back. A profile that never chose units follows the browser's stored
 * choice (session-history.spec.ts relies on that), and the server cannot unset
 * them again, so such a profile is left alone.
 */
export async function resetUnits(socket: Socket, unitSystem = 'imperial') {
  const snapshotPromise = waitForEvent<ProfilesSnapshot>(socket, 'profiles');
  socket.emit('get_profiles');
  const { profiles, active_profile_id: profileId } = await snapshotPromise;
  const current = profiles.find((profile) => profile.id === profileId)?.settings?.unit_system;
  if (current === undefined || current === unitSystem) return;
  const result = (await socket
    .timeout(5000)
    .emitWithAck('set_profile_units', { profile_id: profileId, unit_system: unitSystem })) as { ok?: boolean };
  if (!result?.ok) throw new Error(`Could not reset units to ${unitSystem}`);
}

export async function gotoApp(page: Page, path = '/') {
  await page.goto(`${UI_URL}${path}`);
}

/**
 * Closes the kiosk's setup window ("Aim the camera at the ball") if it is open. It lies over the club
 * picker at start whenever the camera settings report a capture camera, so a kiosk test that fills the
 * camera store closes it first; the picker under it stays open.
 */
export async function dismissSetupReminder(page: Page): Promise<void> {
  const reminder = page.locator('.setup-reminder');
  if (!(await reminder.isVisible().catch(() => false))) return;
  await reminder.locator('.setup-reminder__close').click();
  await expect(reminder).toHaveCount(0);
}

/** A start-page row of the open device menu, by its area name ("Display", "Anzeige", ...). */
export function menuAreaRow(page: Page, name: string): Locator {
  return page
    .locator('.menu-sheet .menu-sheet__area')
    .filter({ has: page.locator('.menu-sheet__area-name').getByText(name, { exact: true }) });
}

/**
 * Open an area of the already open device menu: its sections replace the start page.
 * `tap` for touch contexts that tap rather than click.
 */
export async function openMenuArea(page: Page, name: string, { tap = false } = {}): Promise<void> {
  const row = menuAreaRow(page, name);
  await (tap ? row.tap() : row.click());
  await expect(page.locator('.menu-sheet__area-title')).toHaveText(name);
}

/** The area that holds a view button of the menu; quick buttons (clubs, history, pairing) sit on the start page. */
const MENU_ITEM_AREAS: Record<string, string> = {
  Schläge: 'Auswertung',
  Statistik: 'Auswertung',
  Profile: 'Auswertung',
  Diagnose: 'System',
  Shots: 'Analysis',
  Stats: 'Analysis',
  Profiles: 'Analysis',
  Debug: 'System',
  Golpes: 'Análisis',
  Estadísticas: 'Análisis',
  Perfiles: 'Análisis',
  Depuración: 'Sistema',
};

/** Press a button of the open device menu, through its area when it has one. */
export async function chooseMenuItem(page: Page, name: string, { tap = false } = {}): Promise<void> {
  const area = MENU_ITEM_AREAS[name];
  if (area) await openMenuArea(page, area, { tap });
  const button = page.locator('.menu-sheet').getByRole('button', { name, exact: true });
  await (tap ? button.tap() : button.click());
}

/** From an area page back to the menu's start page. */
export async function menuBack(page: Page, { tap = false } = {}): Promise<void> {
  const back = page.locator('.menu-sheet__back');
  await (tap ? back.tap() : back.click());
  await expect(page.locator('.menu-sheet__areas')).toBeVisible();
}

/** Production kiosk sizes the Live grid must fit. */
export const KIOSK_VIEWPORTS = [
  { width: 800, height: 400 },
  { width: 800, height: 480 },
  { width: 1024, height: 600 },
] as const;

/**
 * Phones in portrait, as Safari's visible area: iPhone SE with Display Zoom,
 * iPhone SE, 15, 15 Pro Max.
 */
export const PHONE_VIEWPORTS = [
  { width: 320, height: 460 },
  { width: 375, height: 548 },
  { width: 393, height: 659 },
  { width: 430, height: 739 },
] as const;

/**
 * Widest Live value we format (wedge spin with a thousands separator).
 * Applied after a real shot so units, estimated marks, and captions stay.
 */
export const WIDEST_LIVE_METRIC_VALUE = '10,000';

/** The Live panel's tiles; the Range layout's tiles are `.range-tile .metric-card`. */
export const LIVE_METRIC_CARDS = '.live-panel__grid .metric-card';

export async function liveMetricValueFontSizes(page: Page): Promise<number[]> {
  await page.evaluate(() => document.fonts.ready);
  return page
    .locator('.live-panel__grid .metric-card__value')
    .evaluateAll((values) => values.map((value) => parseFloat(getComputedStyle(value).fontSize)));
}

export async function overflowingLiveMetricValues(page: Page, cards = LIVE_METRIC_CARDS): Promise<string[]> {
  await page.evaluate(() => document.fonts.ready);

  return page.locator(cards).evaluateAll((cards) => {
    const slop = 2;
    const overflows: string[] = [];

    for (const card of cards) {
      const value = card.querySelector('.metric-card__value');
      const row = card.querySelector('.metric-card__value-row');
      if (!(value instanceof HTMLElement) || !(row instanceof HTMLElement)) {
        continue;
      }

      const cardRect = card.getBoundingClientRect();
      const rowRect = row.getBoundingClientRect();
      const label = card.querySelector('.metric-card__label');
      const meta = card.querySelector('.metric-card__meta');
      const range = document.createRange();
      range.selectNodeContents(value);
      const textRect = range.getBoundingClientRect();
      const unit = card.querySelector('.metric-card__unit');
      const unitOverlapsValue =
        unit instanceof HTMLElement &&
        (() => {
          const unitRange = document.createRange();
          unitRange.selectNodeContents(unit);
          return textRect.right > unitRange.getBoundingClientRect().left + slop;
        })();
      const unitOverflowsCard =
        unit instanceof HTMLElement &&
        (() => {
          const unitRange = document.createRange();
          unitRange.selectNodeContents(unit);
          const unitInk = unitRange.getBoundingClientRect();
          return (
            unitInk.right > cardRect.right + slop ||
            unitInk.left < cardRect.left - slop ||
            unit.scrollWidth > unit.clientWidth + slop
          );
        })();
      const valueOverflowsCard =
        textRect.right > cardRect.right + slop ||
        textRect.left < cardRect.left - slop ||
        textRect.bottom > cardRect.bottom + slop ||
        textRect.top < cardRect.top - slop;
      const rowOverflowsCard = rowRect.right > cardRect.right + slop || row.scrollWidth > row.clientWidth + slop;
      const overlapsLabel = label instanceof HTMLElement && rowRect.top < label.getBoundingClientRect().bottom - slop;
      // A caption row a layout hides with display: none has no box to run into.
      const overlapsMeta =
        meta instanceof HTMLElement &&
        meta.getClientRects().length > 0 &&
        rowRect.bottom > meta.getBoundingClientRect().top + slop;

      if (
        valueOverflowsCard ||
        rowOverflowsCard ||
        overlapsLabel ||
        overlapsMeta ||
        unitOverlapsValue ||
        unitOverflowsCard
      ) {
        const labelText = card.querySelector('.metric-card__label')?.textContent?.trim() ?? 'unknown';
        overflows.push(`${labelText} (${value.textContent?.trim() ?? ''})`);
      }
    }

    return overflows;
  });
}

export async function overflowingLiveMetricMetadata(page: Page, cards = LIVE_METRIC_CARDS): Promise<string[]> {
  await page.evaluate(() => document.fonts.ready);

  return page.locator(cards).evaluateAll((cards) => {
    const slop = 2;
    const overflows: string[] = [];

    for (const card of cards) {
      const cardRect = card.getBoundingClientRect();
      const meta = card.querySelector('.metric-card__meta');
      if (!(meta instanceof HTMLElement)) {
        continue;
      }

      const overflowingText = [...meta.querySelectorAll('.metric-card__confidence-label')].find((label) => {
        if (!(label instanceof HTMLElement)) {
          return false;
        }
        const range = document.createRange();
        range.selectNodeContents(label);
        const textRects = [...range.getClientRects()];
        return (
          label.scrollWidth > label.clientWidth + slop ||
          textRects.some(
            (rect) =>
              rect.left < cardRect.left - slop ||
              rect.right > cardRect.right + slop ||
              rect.top < cardRect.top - slop ||
              rect.bottom > cardRect.bottom + slop
          )
        );
      });

      if (overflowingText) {
        const labelText = card.querySelector('.metric-card__label')?.textContent?.trim() ?? 'unknown';
        overflows.push(`${labelText} (${overflowingText.textContent?.trim() ?? ''})`);
      }
    }

    return overflows;
  });
}

/**
 * Text under `selector` that is cut short or leaves its own box: an ellipsis,
 * a box narrower than its text, or text ink outside the element (by more than
 * `edge` px sideways, and vertically by an eighth of the font size: numbers
 * set at line-height 1 reach just past their box).
 */
export function clippedText(page: Page, selector: string, edge = 1): Promise<string[]> {
  return page.locator(selector).evaluateAll(
    (elements, edge) =>
      elements.flatMap((element) => {
        const box = element.getBoundingClientRect();
        if (box.width === 0) return [];
        const slack = Math.max(1, parseFloat(getComputedStyle(element).fontSize) / 8);
        const range = document.createRange();
        range.selectNodeContents(element);
        const outside = [...range.getClientRects()].some(
          (rect) =>
            rect.width > 0 &&
            (rect.left < box.left - edge ||
              rect.right > box.right + edge ||
              rect.top < box.top - slack ||
              rect.bottom > box.bottom + slack)
        );
        return element.scrollWidth > element.clientWidth + edge || outside ? [element.textContent?.trim() ?? ''] : [];
      }),
    edge
  );
}

/** Buttons under `selector` whose label leaves their padding (a squeezed button: its text runs to the border). */
export function labelsPastPadding(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((buttons) =>
    buttons.flatMap((button) => {
      const box = button.getBoundingClientRect();
      if (box.width === 0) return [];
      const style = getComputedStyle(button);
      const left = box.left + parseFloat(style.borderLeftWidth) + parseFloat(style.paddingLeft);
      const right = box.right - parseFloat(style.borderRightWidth) - parseFloat(style.paddingRight);
      const range = document.createRange();
      range.selectNodeContents(button);
      const ink = range.getBoundingClientRect();
      return ink.left < left - 1 || ink.right > right + 1
        ? [`${button.textContent?.trim()}: ${(left - ink.left).toFixed(1)} / ${(ink.right - right).toFixed(1)} px`]
        : [];
    })
  );
}

/** Text under `selector` that wraps inside a word ("Eise / n 7") rather than between words. */
export function brokenWords(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements.flatMap((element) => {
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      const broken: string[] = [];
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent ?? '';
        let previousTop: number | null = null;
        for (let index = 0; index < text.length; index += 1) {
          const range = document.createRange();
          range.setStart(node, index);
          range.setEnd(node, index + 1);
          const rect = range.getBoundingClientRect();
          if (rect.width === 0) continue;
          if (previousTop !== null && rect.top > previousTop + 1 && !/[\s-]/.test(text[index - 1])) broken.push(text);
          previousTop = rect.top;
        }
      }
      return broken;
    })
  );
}

/** Footer buttons whose icon or label leaves the button, or that a neighbour covers. */
export function crowdedFooter(page: Page): Promise<string[]> {
  return page.locator('.range-footer button').evaluateAll((buttons) =>
    buttons.flatMap((button) => {
      const box = button.getBoundingClientRect();
      const label = button.getAttribute('aria-label') ?? button.textContent?.trim();
      const problems: string[] = [];
      for (const part of button.querySelectorAll('svg, span, small')) {
        const rect = part.getBoundingClientRect();
        if (rect.width === 0) continue;
        if (rect.left < box.left - 1 || rect.right > box.right + 1)
          problems.push(`${label}: ${part.nodeName} leaves it`);
      }
      const range = document.createRange();
      range.selectNodeContents(button);
      const text = range.getBoundingClientRect();
      if (text.left < box.left - 1 || text.right > box.right + 1) problems.push(`${label}: text leaves it`);
      const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
      if (hit?.closest('button') !== button) problems.push(`${label}: covered`);
      return problems;
    })
  );
}

/** Controls under `selector` lower or narrower than a fingertip (44 px). */
export function smallTargets(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements
      .filter((element) => {
        const box = element.getBoundingClientRect();
        return box.width > 0 && (box.height < 44 || box.width < 44);
      })
      .map((element) => `${element.textContent?.trim() || element.getAttribute('aria-label')}`)
  );
}

export function outsideScreen(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements
      .filter((element) => {
        const box = element.getBoundingClientRect();
        return box.left < 0 || box.top < 0 || box.right > window.innerWidth || box.bottom > window.innerHeight;
      })
      .map((element) => element.getAttribute('aria-label') || element.textContent?.trim() || element.className)
  );
}

type FillTextSpy = Window & { __fillTexts?: string[]; __targetLabels?: string[] };

// From now on, records the text of every fillText on a sign, a target label's apart. Calling it again starts over.
export function spyOnFillText(page: Page): Promise<void> {
  return page.evaluate((label) => {
    const spy: FillTextSpy = window;
    if (spy.__fillTexts && spy.__targetLabels) {
      spy.__fillTexts.length = 0;
      spy.__targetLabels.length = 0;
      return;
    }
    const texts: string[] = [];
    const labels: string[] = [];
    spy.__fillTexts = texts;
    spy.__targetLabels = labels;
    const { fillText } = CanvasRenderingContext2D.prototype;
    CanvasRenderingContext2D.prototype.fillText = function (
      this: CanvasRenderingContext2D,
      text: string,
      ...position: [x: number, y: number, maxWidth?: number]
    ) {
      const { width, height } = this.canvas;
      (width === label.width && height === label.height ? labels : texts).push(text);
      fillText.call(this, text, ...position);
    };
  }, TARGET_LABEL_CANVAS);
}

export const fillTexts = (page: Page): Promise<string[]> =>
  page.evaluate(() => (window as FillTextSpy).__fillTexts ?? []);

export const targetLabels = (page: Page): Promise<string[]> =>
  page.evaluate(() => (window as FillTextSpy).__targetLabels ?? []);

export const SCENE_TIMEOUT_MS = 20_000;
export const LANDED_TIMEOUT_MS = 30_000;

export const footerButton = (page: Page, name: string): Locator =>
  page.locator('.range-footer nav').getByRole('button', { name, exact: true });
export const rangeButton = (page: Page): Locator => footerButton(page, 'Range');
export const canvases = (page: Page): Locator => page.locator('.virtual-range__host canvas');

export async function openApp(page: Page, path = '/'): Promise<void> {
  await gotoApp(page, path);
  await page.getByRole('button', { name: 'Close Select club' }).tap();
}

export async function openRange(page: Page): Promise<void> {
  await openApp(page);
  await rangeButton(page).tap();
  await expect(rangeButton(page)).toHaveAttribute('aria-pressed', 'true');
  await expect(canvases(page)).toHaveCount(1, { timeout: SCENE_TIMEOUT_MS });
}

async function shotStoreInPage() {
  const path = '/src/stores/useShotStore.ts';
  const { useShotStore } = await import(/* @vite-ignore */ path);
  return useShotStore;
}

export async function withShotStore<R, Arg>(
  page: Page,
  run: (store: Awaited<ReturnType<typeof shotStoreInPage>>, arg: Arg) => R | Promise<R>,
  arg?: Arg
): Promise<R> {
  const store = await page.evaluateHandle(shotStoreInPage);
  const result = await store.evaluate(run as (on: unknown, arg: unknown) => R | Promise<R>, arg);
  await store.dispose();
  return result;
}

export function latestCarryYards(page: Page): Promise<number> {
  return withShotStore(page, async (store) => {
    const shotTypes = '/src/types/shot.ts';
    const { getCarryYards } = await import(/* @vite-ignore */ shotTypes);
    return Math.round(getCarryYards(store.getState().shots.at(-1)));
  });
}

export async function expectLandedAtLatestCarry(page: Page): Promise<number> {
  const carry = await latestCarryYards(page);
  await expect(page.locator('.virtual-range__stat--carry .virtual-range__value')).toHaveText(`${carry}yds`, {
    timeout: LANDED_TIMEOUT_MS,
  });
  return carry;
}

const OVERLAYS = [
  '.virtual-range__bar',
  '.virtual-range__club',
  '.virtual-range__value',
  '.virtual-range__hint',
  '.range-footer button',
  '.range-header button',
  '.simulate-bubble',
].join(', ');
const TAP_TARGETS = '.range-footer button, .range-header button, .simulate-bubble';

export async function expectOverlaysInside(page: Page): Promise<void> {
  expect(await outsideScreen(page, OVERLAYS)).toEqual([]);
  expect(await clippedText(page, OVERLAYS)).toEqual([]);
  expect(await smallTargets(page, TAP_TARGETS)).toEqual([]);
}

/** Controls under `selector` that a tap in their middle would not reach: something else lies on top. */
export function covered(page: Page, selector: string): Promise<string[]> {
  return page.locator(selector).evaluateAll((elements) =>
    elements
      .filter((element) => {
        const box = element.getBoundingClientRect();
        const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
        return !hit || !element.contains(hit);
      })
      .map((element) => element.getAttribute('aria-label') || element.textContent?.trim() || element.className)
  );
}

/** On the full-screen Range: the part of the screen between the top chrome (header, band) and the footer. */
export function freeArea(page: Page): Promise<{ top: number; bottom: number; width: number; height: number }> {
  return page.evaluate(() => ({
    top: document.querySelector('.range-app__top')!.getBoundingClientRect().bottom,
    bottom: document.querySelector('.range-footer')!.getBoundingClientRect().top,
    width: window.innerWidth,
    height: window.innerHeight,
  }));
}

/** Shows or hides the demo band through the store, without a demo session swapping the shots. */
export function showDemoBand(page: Page, shown: boolean): Promise<void> {
  return page.evaluate(async (shown) => {
    const path = '/src/stores/useSystemStore.ts';
    const { useSystemStore } = await import(/* @vite-ignore */ path);
    useSystemStore.setState({ demoActive: shown });
  }, shown);
}

/**
 * From now on, keeps the camera the range scene draws with in `window.__rangeCamera`: its sign layout places
 * every distance sign through `Vector3.project(camera)` each frame. Install it before the Range opens.
 */
export function spyOnRangeCamera(page: Page): Promise<void> {
  return page.evaluate(async () => {
    const path = '/src/virtual-range/scene/textures.ts';
    const { createSign } = await import(/* @vite-ignore */ path);
    // A sprite's position is a Vector3 of the very three.js module the scene draws with.
    const vector = Object.getPrototypeOf(createSign(4, 4).sprite.position);
    const { project } = vector;
    vector.project = function (this: unknown, camera: { isPerspectiveCamera?: boolean }) {
      if (camera.isPerspectiveCamera) (window as unknown as { __rangeCamera: unknown }).__rangeCamera = camera;
      return project.call(this, camera);
    };
  });
}

/** Each element under `selector` whose box overlaps one under `others`, as "class × class". */
export function overlapping(page: Page, selector: string, others: string): Promise<string[]> {
  return page.evaluate(
    ([selector, others]) => {
      const boxes = (query: string) =>
        [...document.querySelectorAll(query)].map((element) => ({ element, box: element.getBoundingClientRect() }));
      return boxes(selector).flatMap(({ element, box }) =>
        boxes(others)
          .filter(
            ({ box: other }) =>
              box.left < other.right && box.right > other.left && box.top < other.bottom && box.bottom > other.top
          )
          .map((other) => `${element.className} × ${other.element.className}`)
      );
    },
    [selector, others] as const
  );
}

export { expect };

export interface MockedSession {
  summary: { id: string; started_at: string; shot_count: number; clubs: string[] };
  shots: object[];
}

export const savedShot = (timestamp: string, overrides: Record<string, unknown>): Record<string, unknown> => ({
  timestamp,
  profile_name: 'Golfer',
  club_speed_mph: null,
  launch_angle_vertical: 18,
  spin_rpm: 6000,
  flight_path: null,
  ...overrides,
});

export async function showSavedSession(page: Page, index: number): Promise<string> {
  await footerButton(page, 'History').tap();
  await page.locator('.session-history__session').nth(index).tap();
  const showOnRange = page.getByRole('button', { name: 'Show on range', exact: true });
  await expect(showOnRange).toBeVisible();
  const date = await page.locator('.session-history__header p').innerText();
  await showOnRange.tap();
  await expect(page.locator('.session-history')).toHaveCount(0);
  return date;
}

export async function mockSessionHistory(page: Page, sessions: MockedSession[]): Promise<string[]> {
  const sessionUrls: string[] = [];
  await page.route('**/api/session-history', (route) =>
    route.fulfill({ json: { days: 30, sessions: sessions.map(({ summary }) => summary) } })
  );
  await page.route('**/api/session-history/*', (route) => {
    const url = route.request().url();
    sessionUrls.push(url);
    const id = new URL(url).pathname.split('/').pop();
    const found = sessions.find(({ summary }) => summary.id === id);
    return route.fulfill(
      found ? { json: { ...found.summary, shots: found.shots } } : { status: 404, json: { error: 'Missing' } }
    );
  });
  return sessionUrls;
}

export interface Point {
  x: number;
  y: number;
  z: number;
}

interface WorldVector extends Point {
  applyMatrix4(matrix: unknown): WorldVector;
  setFromMatrixPosition(matrix: unknown): WorldVector;
  clone(): WorldVector;
  project(camera: unknown): WorldVector;
  distanceTo(other: Point): number;
}

interface WorldCamera {
  fov: number;
  position: WorldVector;
}

interface SceneObject {
  isGroup?: boolean;
  isScene?: boolean;
  isInstancedMesh?: boolean;
  isSprite?: boolean;
  name?: string;
  position: Point;
  scale?: Point;
  center?: { x: number; y: number };
  visible: boolean;
  count?: number;
  rotation: { y: number };
  matrix: object;
  matrixWorld: object;
  children: SceneObject[];
  geometry?: { type: string; parameters?: Record<string, number> };
  renderOrder?: number;
  getMatrixAt?(index: number, matrix: object): void;
}

export interface WorldFrame {
  at: number;
  turn: number;
  ball: boolean;
}

export interface RangeWorldSpy {
  scene: SceneObject | null;
  frames: WorldFrame[];
  turn(): number;
  highlighted(): boolean;
  signShown(point: Point): boolean;
  onScreen(point: Point): { x: number; y: number };
  dots(): { x: number; z: number }[];
  target(): { line: boolean; label: boolean };
  marks(): { flag: Point | null; zones: boolean };
  signRects(): { name: string; left: number; top: number; right: number; bottom: number }[];
}

declare global {
  interface Window {
    __rangeWorld?: RangeWorldSpy;
  }
}

export const clothOf = (index: number): Point => ({
  x: GREENS[index].sideM + FLAG_CLOTH.offsetM,
  y: FLAG_HEIGHT_M - FLAG_CLOTH.dropM,
  z: -GREENS[index].distanceM,
});

export function spyOnRangeWorld(page: Page): Promise<void> {
  return page.evaluate(async () => {
    const path = '/src/virtual-range/scene/textures.ts';
    const { createSign } = await import(/* @vite-ignore */ path);
    const probe = createSign(4, 4).sprite;
    const Vector = probe.position.constructor as new (x?: number, y?: number, z?: number) => WorldVector;
    const Matrix = probe.matrix.constructor as new () => object;
    const object3d = Object.getPrototypeOf(Object.getPrototypeOf(probe));
    const { updateMatrixWorld } = object3d;
    const camera = () => (window as unknown as { __rangeCamera: WorldCamera }).__rangeCamera;
    const world = (): SceneObject => spy.scene!.children.find((child) => child.isGroup)!;
    const named = (name: string) => world().children.find((child) => child.name === name);
    const spy: RangeWorldSpy = {
      scene: null,
      frames: [],
      turn: () => world().rotation.y,
      highlighted: () =>
        world().children.some((child) => child.geometry?.parameters?.innerRadius === 1 && child.visible),
      signShown: ({ x, z }) =>
        world().children.some(
          (child) =>
            child.isSprite &&
            child.visible &&
            child.position.x === x &&
            child.position.y === 5 &&
            child.position.z === z
        ),
      onScreen({ x, y, z }) {
        const ndc = new Vector(x, y, z).applyMatrix4(world().matrixWorld).project(camera());
        const canvas = document.querySelector('.virtual-range__host canvas')!.getBoundingClientRect();
        return { x: canvas.left + ((ndc.x + 1) / 2) * canvas.width, y: canvas.top + ((1 - ndc.y) / 2) * canvas.height };
      },
      dots() {
        const dots = world().children.find(
          (child) => child.isInstancedMesh && child.geometry?.type === 'CircleGeometry'
        )!;
        const matrix = new Matrix();
        return Array.from({ length: dots.count ?? 0 }, (_, i) => {
          dots.getMatrixAt!(i, matrix);
          const { x, z } = new Vector().setFromMatrixPosition(matrix).applyMatrix4(world().matrixWorld);
          return { x, z };
        });
      },
      target() {
        const shown = (found: SceneObject | undefined) => found?.visible === true;
        return {
          line: shown(named('target-line')),
          label: shown(world().children.find((child) => child.isSprite && child.renderOrder === 8)),
        };
      },
      marks() {
        const flag = named('training-flag');
        return {
          flag: flag?.visible ? { x: flag.position.x, y: flag.position.y, z: flag.position.z } : null,
          zones: named('training-zones')?.visible === true,
        };
      },
      signRects() {
        const { fov, position } = camera();
        const canvas = document.querySelector('.virtual-range__host canvas')!.getBoundingClientRect();
        const pxPerUnit = canvas.height / 2 / Math.tan((fov * Math.PI) / 360);
        const signs = world().children.filter((child) => child.isSprite && child.visible && child.renderOrder !== 7);
        return signs.map(({ matrixWorld, scale, center }) => {
          const at = new Vector().setFromMatrixPosition(matrixWorld);
          const distance = position.distanceTo(at);
          const ndc = at.clone().project(camera());
          const x = canvas.left + ((ndc.x + 1) / 2) * canvas.width;
          const y = canvas.top + ((1 - ndc.y) / 2) * canvas.height;
          const w = (scale!.x / distance) * pxPerUnit;
          const h = (scale!.y / distance) * pxPerUnit;
          const name = [at.x, at.y, at.z].map(Math.round).join(',');
          return {
            name,
            left: x - center!.x * w,
            top: y - (1 - center!.y) * h,
            right: x + (1 - center!.x) * w,
            bottom: y + center!.y * h,
          };
        });
      },
    };
    window.__rangeWorld = spy;
    object3d.updateMatrixWorld = function (this: SceneObject, force?: boolean) {
      if (this.isScene) {
        spy.scene = this;
        const ball = world().children.find((child) => child.geometry?.type === 'SphereGeometry');
        spy.frames.push({ at: performance.now(), turn: world().rotation.y, ball: ball?.visible === true });
      }
      return updateMatrixWorld.call(this, force);
    };
  });
}

export const worldTurn = (page: Page): Promise<number> => page.evaluate(() => window.__rangeWorld!.turn());

export const greenHighlighted = (page: Page): Promise<boolean> =>
  page.evaluate(() => window.__rangeWorld!.highlighted());

export const onScreen = (page: Page, point: Point): Promise<{ x: number; y: number }> =>
  page.evaluate((point) => window.__rangeWorld!.onScreen(point), point);

export const sceneAndCameraSeen = (page: Page): Promise<boolean> =>
  page.evaluate(() => Boolean(window.__rangeWorld!.scene && (window as { __rangeCamera?: unknown }).__rangeCamera));

export async function settled(page: Page): Promise<void> {
  await expect
    .poll(() => page.evaluate(() => performance.now() - (window.__rangeWorld!.frames.at(-1)?.at ?? 0)), {
      timeout: 30_000,
    })
    .toBeGreaterThan(600);
}

export const storedTarget = (page: Page): Promise<unknown> =>
  page.evaluate((key) => JSON.parse(sessionStorage.getItem(key) ?? 'null'), TARGET_KEY);

export function signLabel(page: Page, distanceM: number, unitSystem = 'imperial'): Promise<string> {
  return page.evaluate(
    async ([distanceM, unitSystem]) => {
      const path = '/src/virtual-range/distance.ts';
      const { distanceLabel } = await import(/* @vite-ignore */ path);
      return distanceLabel(distanceM, unitSystem) as string;
    },
    [distanceM, unitSystem] as const
  );
}

export async function hitAShot(page: Page): Promise<void> {
  await withControlSocket(async (socket) => {
    const arrived = waitForEvent(socket, 'shot');
    await page.getByRole('button', { name: 'Schlag simulieren' }).tap();
    await arrived;
  });
}

export async function serveClubAverages(page: Page, averages: Record<string, unknown>): Promise<string[]> {
  const asked: string[] = [];
  await page.route('**/api/club-averages?**', (route) => {
    const query = new URL(route.request().url()).searchParams;
    asked.push(`${query.get('club')}/${query.get('profile')}`);
    return route.fulfill({ json: averages, headers: { 'access-control-allow-origin': '*' } });
  });
  return asked;
}

export const activeProfileId = (page: Page): Promise<string> =>
  page.evaluate(async () => {
    const path = '/src/stores/useProfileStore.ts';
    const { useProfileStore } = await import(/* @vite-ignore */ path);
    return useProfileStore.getState().activeProfileId as string;
  });

/**
 * A metric tile's label, number row and caption, each a line of its own: no
 * part squeezed below its text, every part inside the tile's border, in order
 * from top to bottom.
 */
export function crowdedTileParts(page: Page, tiles: string): Promise<string[]> {
  return page.locator(tiles).evaluateAll((cards) =>
    cards.flatMap((card) => {
      const style = getComputedStyle(card);
      const box = card.getBoundingClientRect();
      const top = box.top + parseFloat(style.borderTopWidth) - 1;
      const bottom = box.bottom - parseFloat(style.borderBottomWidth) + 1;
      const name = card.querySelector('.metric-card__label')?.textContent?.trim();
      const parts = ['.metric-card__label', '.metric-card__value-row', '.metric-card__meta']
        .map((selector) => card.querySelector(selector))
        .filter((part): part is HTMLElement => part instanceof HTMLElement && part.getBoundingClientRect().height > 0);
      const problems: string[] = [];
      let previousBottom = top;
      for (const part of parts) {
        const rect = part.getBoundingClientRect();
        // Number rows have line-height 1, so the digits' ink always reaches past them by a pixel or two.
        const squeezed = !part.matches('.metric-card__value-row') && part.scrollHeight > part.clientHeight + 1;
        if (squeezed) problems.push(`${name}: ${part.className} squeezed`);
        if (rect.top < previousBottom - 1) problems.push(`${name}: ${part.className} overlaps the part above`);
        if (rect.bottom > bottom) problems.push(`${name}: ${part.className} below the tile`);
        previousBottom = rect.bottom;
      }
      return problems;
    })
  );
}
