import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from '../App';
import { setActiveLocale } from '../i18n';
import { setupReminderDe, setupReminderEn, setupReminderEs } from '../i18n/setupReminder';
import { useCameraStore, type CameraCaptureSettings } from '../stores/useCameraStore';
import { useSystemStore } from '../stores/useSystemStore';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT } from '../utils/ballGuide';
import { BallGuideHairlines } from './BallGuideHairlines';
import { SetupReminder } from './SetupReminder';
import {
  SETUP_PREVIEW_REFRESH_MS,
  cameraAvailable,
  closeSetupReminder,
  setupMarkStates,
  showSetupReminder,
  startPreviewPolling,
  type SetupPreview,
  type SetupReminderContext,
} from './setupReminderState';

const KIOSK = 'http://127.0.0.1:8080/';
const PHONE = 'http://openflight.local:8080/';

const CAMERA: CameraCaptureSettings = { available: true, enabled: true, running: true, width: 640, height: 400 };
const BALL = { frame_x_px: 191.5, frame_y_px: 279.5, preview_x_pct: 30, preview_y_pct: 70, set_at: 1_761_389_200 };
const TARGET = {
  frame_x_px: 412.3,
  frame_y_px: 150.1,
  preview_x_pct: 64.5,
  preview_y_pct: 37.6,
  azimuth_deg: 2.34,
  elevation_deg: 1.1,
  set_at: 1_761_389_280,
};

const openPage = (url: string): void => {
  const { localStorage } = fakeStorage({});
  vi.stubGlobal('window', { localStorage, location: new URL(url) });
};

/** React's SSR puts `<!-- -->` between adjacent text nodes; the user reads them as one line. */
const text = (html: string): string => html.replace(/<!-- -->/g, '');

describe('when the setup reminder shows', () => {
  const context: SetupReminderContext = {
    kiosk: true,
    displayRoute: false,
    rangeDemo: false,
    cameraAvailable: true,
    dismissed: false,
  };

  it('shows on the kiosk with a capture camera, until it is closed', () => {
    expect(showSetupReminder(context)).toBe(true);
    expect(showSetupReminder({ ...context, dismissed: true })).toBe(false);
  });

  it.each([
    ['a phone or another browser', { kiosk: false }],
    ['the /display route', { displayRoute: true }],
    ['the range demo', { rangeDemo: true }],
    ['no capture camera', { cameraAvailable: false }],
  ])('never shows on %s', (_name, change) => {
    expect(showSetupReminder({ ...context, ...change })).toBe(false);
  });

  it('takes the camera from the server settings: the capture runtime exists', () => {
    expect(cameraAvailable(CAMERA)).toBe(true);
    expect(cameraAvailable({ available: false, enabled: true })).toBe(false);
  });
});

describe('the setup reminder in the app', () => {
  /**
   * renderToString reads a store's server snapshot, its initial state (the same object the store keeps),
   * so the camera goes in there for a test and is put back after it.
   */
  const initial = useCameraStore.getInitialState();
  const original = initial.captureSettings;
  const serveCamera = (captureSettings: CameraCaptureSettings) => {
    initial.captureSettings = captureSettings;
  };

  beforeEach(() => {
    useSystemStore.setState({ serverClub: null });
    serveCamera(CAMERA);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    serveCamera(original);
  });

  it('lies above the club picker on the kiosk with a camera', () => {
    openPage(KIOSK);
    const html = renderToString(<App />);

    expect(html).toContain('class="setup-reminder"');
    expect(html).toContain('aria-label="Aim the camera at the ball"');
    // The picker is still open under it, and the reminder comes after it in the stacking order.
    expect(html).toContain('aria-label="Select club"');
    expect(html.indexOf('class="setup-reminder"')).toBeGreaterThan(html.indexOf('class="picker-overlay'));
  });

  it.each([
    ['a phone host', PHONE],
    ['the /display route', `${KIOSK}display`],
    ['the range demo', `${KIOSK}?rangeDemo=1`],
  ])('does not show on %s', (_name, url) => {
    openPage(url);
    expect(renderToString(<App />)).not.toContain('setup-reminder');
  });

  it('does not show without a capture camera', () => {
    serveCamera({ available: false });
    openPage(KIOSK);
    const html = renderToString(<App />);

    expect(html).not.toContain('setup-reminder');
    expect(html).toContain('aria-label="Select club"');
  });
});

describe('closing the setup reminder', () => {
  it('"Done" keeps the club picker under it open and the view as it is', () => {
    expect(closeSetupReminder('done', true)).toEqual({ pickerOpen: true, view: null });
  });

  it('"Align" closes the club picker too and opens the Align view', () => {
    expect(closeSetupReminder('align', true)).toEqual({ pickerOpen: false, view: 'camera' });
  });

  it('wires the header close button and "Done" to onDone, "Align" to onAlign', () => {
    const html = renderToString(<SetupReminder captureSettings={CAMERA} onAlign={() => {}} onDone={() => {}} />);

    expect(html).toContain('aria-label="Close Aim the camera at the ball"');
    expect(html).toMatch(/panel-action--secondary"[^>]*>Align</);
    expect(html).toMatch(/panel-action--primary"[^>]*>Done</);
  });
});

describe('the setup reminder content', () => {
  afterEach(() => setActiveLocale('en'));

  const render = (settings: CameraCaptureSettings) =>
    text(renderToString(<SetupReminder captureSettings={settings} onAlign={() => {}} onDone={() => {}} />));

  it.each([
    [{}, 'Ball mark: missing · Target: missing'],
    [{ ball_mark: BALL }, 'Ball mark: set · Target: missing'],
    [{ target_line: TARGET }, 'Ball mark: missing · Target: set'],
    [{ ball_mark: BALL, target_line: TARGET }, 'Ball mark: set · Target: set'],
  ])('reports the marks %#: %s', (marks, line) => {
    expect(render({ ...CAMERA, ...marks })).toContain(`<p class="setup-reminder__marks">${line}</p>`);
  });

  it('maps the marks to their message keys', () => {
    expect(setupMarkStates({ ...CAMERA, ball_mark: BALL, target_line: null })).toEqual([
      'setup.markSet',
      'setup.markMissing',
    ]);
  });

  it('lists the three steps in German and waits for the first frame', () => {
    setActiveLocale('de');
    const html = render({ ...CAMERA, ball_mark: BALL });

    expect(html).toContain('>Setup<');
    expect(html).toContain('>Kamera auf den Ball einstellen<');
    expect(html).toContain('<li>Ball auf die Abschlagposition legen.</li>');
    expect(html).toContain('<li>Kamera einstellen, bis der Ball im Fadenkreuz liegt.</li>');
    expect(html).toContain('<li>Ball und Ziel unter „Ausrichten“ antippen.</li>');
    expect(html).toContain('Ballmarke: gesetzt · Ziel: fehlt');
    expect(html).toMatch(/>Ausrichten<\/button>/);
    expect(html).toMatch(/>Fertig<\/button>/);
    expect(html).toContain('Kamerabild lädt …');
  });

  it('draws the Align view crosshair from the shared ball guide', () => {
    const html = renderToString(<BallGuideHairlines />);

    expect([BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT]).toEqual([50, 78]);
    expect(html).toContain('x1="50" y1="0" x2="50" y2="100"');
    expect(html).toContain('x1="0" y1="78" x2="100" y2="78"');
  });
});

describe('the setup reminder picture', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  const frame = () => new Response(new Blob(['jpeg']), { status: 200 });

  function poll(fetchImpl: typeof fetch) {
    const changes: SetupPreview[] = [];
    const revoked: string[] = [];
    let next = 0;
    const stop = startPreviewPolling({
      url: '/api/camera/preview.jpg',
      fetchImpl,
      createObjectUrl: () => `blob:${(next += 1)}`,
      revokeObjectUrl: (url) => revoked.push(url),
      now: () => 7,
      onChange: (preview) => changes.push(preview),
    });
    return { changes, revoked, stop };
  }

  it('refreshes once a second while open and stops when it closes', async () => {
    const fetchImpl = vi.fn(async () => frame());
    const { changes, revoked, stop } = poll(fetchImpl);
    await vi.advanceTimersByTimeAsync(0);

    expect(SETUP_PREVIEW_REFRESH_MS).toBe(1000);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledWith('/api/camera/preview.jpg?t=7', { cache: 'no-store' });
    expect(changes).toEqual([{ state: 'available', src: 'blob:1' }]);

    await vi.advanceTimersByTimeAsync(2000);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    // Each new frame frees the one before.
    expect(revoked).toEqual(['blob:1', 'blob:2']);

    stop();
    expect(vi.getTimerCount()).toBe(0);
    expect(revoked).toEqual(['blob:1', 'blob:2', 'blob:3']);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it('says "No camera picture" on a 404 and keeps polling for the camera to come back', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValue(frame());
    const { changes, stop } = poll(fetchImpl);
    await vi.advanceTimersByTimeAsync(0);
    expect(changes).toEqual([{ state: 'unavailable' }]);

    await vi.advanceTimersByTimeAsync(1000);
    expect(changes.at(-1)).toEqual({ state: 'available', src: 'blob:1' });
    stop();
  });

  it('says "No camera picture" on a network error before the first frame, and keeps a frame it has', async () => {
    const fetchImpl = vi
      .fn<typeof fetch>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(frame())
      .mockRejectedValue(new Error('offline'));
    const { changes, stop } = poll(fetchImpl);
    await vi.advanceTimersByTimeAsync(2000);

    expect(changes).toEqual([{ state: 'unavailable' }, { state: 'available', src: 'blob:1' }]);
    stop();
  });

  it('asks again only once an answer is in', async () => {
    let answer: (response: Response) => void = () => {};
    const fetchImpl = vi.fn(() => new Promise<Response>((resolve) => (answer = resolve)));
    const { stop } = poll(fetchImpl);
    await vi.advanceTimersByTimeAsync(3000);
    expect(fetchImpl).toHaveBeenCalledTimes(1);

    answer(frame());
    await vi.advanceTimersByTimeAsync(1000);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    stop();
  });
});

describe('setup reminder copy', () => {
  it('has every key in German and Spanish', () => {
    const keys = Object.keys(setupReminderEn).sort();
    expect(Object.keys(setupReminderDe).sort()).toEqual(keys);
    expect(Object.keys(setupReminderEs).sort()).toEqual(keys);
    for (const catalog of [setupReminderEn, setupReminderDe, setupReminderEs])
      for (const value of Object.values(catalog)) expect(value.trim()).not.toBe('');
  });
});
