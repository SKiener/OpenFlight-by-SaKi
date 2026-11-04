import type { CameraCaptureSettings } from '../stores/useCameraStore';
import type { PanelView } from './panel/views';
import type { MessageKey } from '../i18n';

/** The reminder's picture refreshes once a second while it is open (the Align view: every 5 s). */
export const SETUP_PREVIEW_REFRESH_MS = 1000;

export interface SetupReminderContext {
  /** The page is the Pi's own kiosk browser (`isKioskHost`), as for the Range layout. */
  kiosk: boolean;
  displayRoute: boolean;
  rangeDemo: boolean;
  /** The server runs a capture camera: what the Align view's picture needs. */
  cameraAvailable: boolean;
  /** Closed once already on this page load; the reminder is not persisted. */
  dismissed: boolean;
}

/** Whether the kiosk shows the setup reminder now: once per page load, only with a capture camera. */
export function showSetupReminder(context: SetupReminderContext): boolean {
  return context.kiosk && !context.displayRoute && !context.rangeDemo && context.cameraAvailable && !context.dismissed;
}

/** Whether the server reports a capture camera (the runtime exists), as the Align view relies on. */
export function cameraAvailable(settings: CameraCaptureSettings): boolean {
  return settings.available === true;
}

export type SetupReminderAction = 'done' | 'align';

export interface SetupReminderOutcome {
  /** The club picker under the reminder stays open, or closes with it. */
  pickerOpen: boolean;
  /** The view to switch to, or null to keep the current one. */
  view: PanelView | null;
}

/**
 * What closing the reminder does. "Done" (and the header's close button) leaves the club picker under it;
 * "Align" closes the picker too and opens the Align view.
 */
export function closeSetupReminder(action: SetupReminderAction, pickerOpen: boolean): SetupReminderOutcome {
  if (action === 'align') return { pickerOpen: false, view: 'camera' };
  return { pickerOpen, view: null };
}

/** The status line's two parts: whether the ball mark and the target are set. */
export function setupMarkStates(settings: CameraCaptureSettings): [ball: MessageKey, target: MessageKey] {
  return [
    settings.ball_mark ? 'setup.markSet' : 'setup.markMissing',
    settings.target_line ? 'setup.markSet' : 'setup.markMissing',
  ];
}

export type SetupPreview = { state: 'loading' } | { state: 'unavailable' } | { state: 'available'; src: string };

export interface PreviewPollingOptions {
  url: string;
  intervalMs?: number;
  fetchImpl?: typeof fetch;
  createObjectUrl?: (blob: Blob) => string;
  revokeObjectUrl?: (url: string) => void;
  now?: () => number;
  onChange: (preview: SetupPreview) => void;
}

/**
 * Polls the camera still until the returned stop function runs. A 404 or another failed answer shows
 * "No camera picture"; a network error before the first frame does too, after it the last frame stays.
 * Polling continues either way, so the picture comes back when the camera does. One request at a time:
 * a slow answer skips the ticks it overlaps. Stopping revokes the last frame's object URL.
 */
export function startPreviewPolling({
  url,
  intervalMs = SETUP_PREVIEW_REFRESH_MS,
  fetchImpl = (...args) => fetch(...args),
  createObjectUrl = (blob) => URL.createObjectURL(blob),
  revokeObjectUrl = (objectUrl) => URL.revokeObjectURL(objectUrl),
  now = Date.now,
  onChange,
}: PreviewPollingOptions): () => void {
  let stopped = false;
  let inFlight = false;
  let objectUrl: string | null = null;

  const dropFrame = () => {
    if (objectUrl) revokeObjectUrl(objectUrl);
    objectUrl = null;
  };

  const refresh = async () => {
    if (stopped || inFlight) return;
    inFlight = true;
    try {
      const response = await fetchImpl(`${url}?t=${now()}`, { cache: 'no-store' });
      if (stopped) return;
      if (!response.ok) {
        dropFrame();
        onChange({ state: 'unavailable' });
        return;
      }
      const blob = await response.blob();
      if (stopped) return;
      const next = createObjectUrl(blob);
      dropFrame();
      objectUrl = next;
      onChange({ state: 'available', src: next });
    } catch {
      if (!stopped && !objectUrl) onChange({ state: 'unavailable' });
    } finally {
      inFlight = false;
    }
  };

  void refresh();
  const timer = setInterval(() => void refresh(), intervalMs);
  return () => {
    stopped = true;
    clearInterval(timer);
    dropFrame();
  };
}
