export type ReplayPlaybackResult = 'playing' | 'blocked' | 'aborted' | 'failed';

function errorName(error: unknown): string | undefined {
  return error && typeof error === 'object' && 'name' in error && typeof error.name === 'string'
    ? error.name
    : undefined;
}

/**
 * Ask the video to play and say how it went, without throwing.
 *
 * - `blocked`: the platform needs a tap first (NotAllowedError). iOS refuses even muted
 *   autoplay in Low Power Mode or under thermal pressure, in Safari and in WKWebView alike;
 *   the `autoplay` attribute then fails silently, so the player must offer a play control.
 * - `aborted`: a pause, seek, or source change cancelled the request; nothing to report.
 * - `failed`: the media itself could not be played.
 */
export async function startReplayPlayback(video: Pick<HTMLMediaElement, 'play'>): Promise<ReplayPlaybackResult> {
  try {
    await video.play();
    return 'playing';
  } catch (error) {
    const name = errorName(error);
    if (name === 'NotAllowedError') return 'blocked';
    if (name === 'AbortError') return 'aborted';
    return 'failed';
  }
}

/** How long an automatic replay waits for a tap when autoplay was refused, then returns to Live. */
export const REFUSED_AUTOMATIC_REPLAY_TIMEOUT_MS = 8000;

/**
 * Close a refused automatic replay after REFUSED_AUTOMATIC_REPLAY_TIMEOUT_MS unless the user
 * starts it. Returns the cleanup, or undefined when nothing is waiting (manual replays stay
 * open until closed; a playing replay ends normally).
 */
export function scheduleRefusedAutomaticReturn(
  waiting: { automatic: boolean; blocked: boolean; paused: boolean },
  onClose: () => void
): (() => void) | undefined {
  if (!waiting.automatic || !waiting.blocked || !waiting.paused) return undefined;
  const timeout = setTimeout(onClose, REFUSED_AUTOMATIC_REPLAY_TIMEOUT_MS);
  return () => clearTimeout(timeout);
}
