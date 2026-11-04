import {
  prepareCameraReplay,
  ReplayPreparationRequestError,
  type PreparedCameraReplay,
} from '../services/cameraReplay';
import { useReplayAvailabilityStore } from '../stores/useReplayAvailabilityStore';

/** How long to wait for `camera_replay_ready` before asking the server directly. */
export const REPLAY_READY_GRACE_MS = 45_000;
export const PREPARE_RETRY_DELAYS_MS: readonly number[] = [1_000, 2_000, 4_000, 8_000];
export const COMPANION_PLAYBACK_SPEEDS = [1, 0.5, 0.25, 0.1] as const;
export type CompanionPlaybackSpeed = (typeof COMPANION_PLAYBACK_SPEEDS)[number];
export const DEFAULT_PLAYBACK_SPEED: CompanionPlaybackSpeed = 0.25;

export type ReplayPlan =
  { kind: 'ready'; videoUrl: string } | { kind: 'wait'; remainingMs: number } | { kind: 'prepare' };

interface ReplayAvailability {
  readyVideoUrls: Record<string, string>;
  liveSince: Record<string, number>;
}

/** Use an announced video, wait for a live shot's background encode, or prepare explicitly (late connect). */
export function planReplay(replayId: string, availability: ReplayAvailability, now: number): ReplayPlan {
  const videoUrl = availability.readyVideoUrls[replayId];
  if (videoUrl) return { kind: 'ready', videoUrl };
  const liveSince = availability.liveSince[replayId];
  if (liveSince !== undefined) {
    const remainingMs = liveSince + REPLAY_READY_GRACE_MS - now;
    if (remainingMs > 0) return { kind: 'wait', remainingMs };
  }
  return { kind: 'prepare' };
}

export function isRetryablePrepareError(error: unknown): boolean {
  if (!(error instanceof ReplayPreparationRequestError)) return false;
  return error.status === undefined || error.status === 409 || error.status === 503;
}

function abortError(): Error {
  const error = new Error('Replay preparation aborted');
  error.name = 'AbortError';
  return error;
}

export function waitFor(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(timer);
      reject(abortError());
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

interface PrepareWithRetryOptions {
  signal?: AbortSignal;
  prepare?: (replayId: string, signal?: AbortSignal) => Promise<PreparedCameraReplay>;
  delaysMs?: readonly number[];
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

/** POST prepare, backing off while the server is busy (409/503) or unreachable. */
export async function prepareReplayWithRetry(
  replayId: string,
  {
    signal,
    prepare = prepareCameraReplay,
    delaysMs = PREPARE_RETRY_DELAYS_MS,
    sleep = waitFor,
  }: PrepareWithRetryOptions = {}
): Promise<PreparedCameraReplay> {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await prepare(replayId, signal);
    } catch (error) {
      if (signal?.aborted || attempt >= delaysMs.length || !isRetryablePrepareError(error)) throw error;
    }
    await sleep(delaysMs[attempt], signal);
  }
}

function waitForAnnouncement(replayId: string, timeoutMs: number, signal?: AbortSignal): Promise<string | null> {
  return new Promise((resolve, reject) => {
    const finish = (videoUrl: string | null) => {
      clearTimeout(timer);
      unsubscribe();
      signal?.removeEventListener('abort', onAbort);
      resolve(videoUrl);
    };
    const onAbort = () => {
      clearTimeout(timer);
      unsubscribe();
      reject(abortError());
    };
    const timer = setTimeout(() => finish(null), timeoutMs);
    const unsubscribe = useReplayAvailabilityStore.subscribe((state) => {
      const videoUrl = state.readyVideoUrls[replayId];
      if (videoUrl) finish(videoUrl);
    });
    if (signal?.aborted) onAbort();
    else signal?.addEventListener('abort', onAbort, { once: true });
  });
}

interface ResolveReplayOptions extends PrepareWithRetryOptions {
  now?: () => number;
}

export async function resolveReplayVideo(replayId: string, options: ResolveReplayOptions = {}): Promise<string> {
  const { now = Date.now, ...prepareOptions } = options;
  const plan = planReplay(replayId, useReplayAvailabilityStore.getState(), now());
  if (plan.kind === 'ready') return plan.videoUrl;
  if (plan.kind === 'wait') {
    const announced = await waitForAnnouncement(replayId, plan.remainingMs, options.signal);
    if (announced) return announced;
  }
  const prepared = await prepareReplayWithRetry(replayId, prepareOptions);
  useReplayAvailabilityStore.getState().markReady(replayId, prepared.videoUrl);
  return prepared.videoUrl;
}
