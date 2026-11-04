import { test, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dismissSetupReminder } from './helpers';

/**
 * The Range app on a phone, in German, with fixture data: /api is answered in
 * the page (camera frames are test cards with a coloured band on each edge),
 * and the stores are filled after the live socket is dropped.
 */

export type Size = '640x400' | '640x200';

const FRAME_VIDEO: Record<Size, Buffer> = {
  '640x400': readFileSync(fileURLToPath(new URL('./fixtures/frame-640x400.webm', import.meta.url))),
  '640x200': readFileSync(fileURLToPath(new URL('./fixtures/frame-640x200.webm', import.meta.url))),
};

/** A test card: red top, blue bottom, yellow left, magenta right, ball target at 50 % / 78 %. */
export function frameSvg(size: Size): string {
  const [width, height] = size.split('x').map(Number);
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">
    <rect width="${width}" height="${height}" fill="#404040"/>
    <rect width="${width}" height="20" fill="#f00"/>
    <rect y="${height - 20}" width="${width}" height="20" fill="#00f"/>
    <rect width="20" height="${height}" fill="#ff0"/>
    <rect x="${width - 20}" width="20" height="${height}" fill="#f0f"/>
    <circle cx="${width / 2}" cy="${height * 0.78}" r="8" fill="#fff"/>
  </svg>`;
}

export const REPLAY = {
  id: 'phone-camera',
  frame_count: 10,
  trigger_frame: 5,
  playback_fps: 10,
  capture_fps: 300,
  duration_seconds: 1,
  display_mirror_horizontal: true,
};
export const SHOT = {
  ball_speed_mph: 168.4,
  club_speed_mph: 113.8,
  smash_factor: 1.48,
  estimated_carry_yards: 287,
  carry_range: [273, 301],
  club: 'driver',
  timestamp: '2026-10-07T09:12:00Z',
  peak_magnitude: 100,
  launch_angle_vertical: 10.4,
  launch_angle_horizontal: -2,
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
  camera_replay: REPLAY,
};
export function camera(size: Size) {
  const [width, height] = size.split('x').map(Number);
  return {
    available: true,
    enabled: true,
    running: true,
    armed: true,
    width,
    height,
    fps: size === '640x400' ? 300 : 500,
    capture_mode: size === '640x400' ? 'full-300' : 'strip-500',
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
}

export interface Setup {
  /** The camera frame served as preview and replay. */
  frame?: Size;
  /** The capture settings' frame size, when it differs from the frame (just after a mode switch). */
  settings?: Size;
  shots?: object[];
  /**
   * Load the app from this origin. Defaults to a paired phone's ({@link phoneOrigin}): a phone's host
   * is not the kiosk's 127.0.0.1, whose own screen has the big kiosk Live layout. Pass the base URL for the kiosk.
   */
  origin?: string;
  /** Leave the start overlays (the kiosk's setup window, the club picker) open. */
  keepStartOverlays?: boolean;
  /** The app's language; German unless given. */
  locale?: 'de' | 'en' | 'es';
}

export async function routeApi(page: Page, { frame = '640x400' }: Setup) {
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/camera/preview.jpg*', (route) =>
    route.fulfill({ contentType: 'image/svg+xml', body: frameSvg(frame) })
  );
  await page.route('**/api/camera/replays/*/prepare', (route) =>
    route.fulfill({ json: { ...REPLAY, video_url: `/api/camera/replays/${REPLAY.id}/video` } })
  );
  await page.route('**/api/camera/replays/*/video*', (route) =>
    route.fulfill({ contentType: 'video/webm', body: FRAME_VIDEO[frame] })
  );
  await page.route('**/api/session-history', (route) =>
    route.fulfill({
      json: {
        days: 30,
        sessions: [{ id: 's1', started_at: '2026-10-04T10:00:00Z', shot_count: 1, clubs: ['driver'] }],
      },
    })
  );
  await page.route('**/api/session-history/*', (route) =>
    route.fulfill({
      json: {
        id: 's1',
        started_at: '2026-10-04T10:00:00Z',
        shot_count: 1,
        clubs: ['driver'],
        shots: [{ ...SHOT, shot_number: 1 }],
      },
    })
  );
}

/** A paired phone's view of the app: Chrome resolves *.localhost to this machine, and it is not a kiosk host. */
export const phoneOrigin = (baseURL: string) => baseURL.replace('127.0.0.1', 'phone.localhost');

/** The Range app in German with fixture stores (no live backend data). */
export async function openRange(page: Page, setup: Setup = {}) {
  await routeApi(page, setup);
  await page.addInitScript((locale) => localStorage.setItem('openflight.locale:v1', locale), setup.locale ?? 'de');
  await page.goto(`${setup.origin ?? phoneOrigin(test.info().project.use.baseURL ?? '')}/`);
  await page.locator('.range-app').waitFor();
  await page.evaluate(
    async ({ shots, settings }) => {
      const load = (path: string) => import(/* @vite-ignore */ `/src/${path}.ts`);
      (await load('services/socketService')).socketService.disconnect();
      (await load('stores/useProfileStore')).useProfileStore.getState().applySnapshot({
        profiles: [{ id: 'phone', name: 'Golfer', settings: { unit_system: 'metric' } }],
        active_profile_id: 'phone',
      });
      (await load('stores/useSystemStore')).useSystemStore.setState({ connected: true });
      (await load('stores/useCameraStore')).useCameraStore.getState().setCaptureSettings(settings);
      (await load('stores/useShotStore')).useShotStore.getState().setShots(shots);
      await document.fonts.ready;
    },
    { shots: setup.shots ?? [SHOT], settings: camera(setup.settings ?? setup.frame ?? '640x400') }
  );
  if (setup.keepStartOverlays) return;
  await dismissSetupReminder(page);
  const close = page.locator('.picker-overlay__close');
  if (await close.isVisible().catch(() => false)) await close.click();
}
