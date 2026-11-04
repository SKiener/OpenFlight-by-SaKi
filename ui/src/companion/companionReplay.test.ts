import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ReplayPreparationRequestError, type PreparedCameraReplay } from '../services/cameraReplay';
import { useReplayAvailabilityStore } from '../stores/useReplayAvailabilityStore';
import { planReplay, prepareReplayWithRetry, REPLAY_READY_GRACE_MS, resolveReplayVideo } from './companionReplay';

const prepared = (id: string): PreparedCameraReplay => ({
  id,
  frame_count: 99,
  trigger_frame: 73,
  playback_fps: 60,
  duration_seconds: 1.65,
  display_mirror_horizontal: false,
  videoUrl: `http://pi:8080/api/camera/replays/${id}/video`,
});

const noSleep = () => Promise.resolve();

beforeEach(() => {
  useReplayAvailabilityStore.setState({ readyVideoUrls: {}, liveSince: {} });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('planReplay', () => {
  it('plays an announced replay immediately', () => {
    expect(planReplay('a', { readyVideoUrls: { a: 'http://pi/a.mp4' }, liveSince: { a: 0 } }, 10)).toEqual({
      kind: 'ready',
      videoUrl: 'http://pi/a.mp4',
    });
  });

  it('waits for the background encode of a shot seen live', () => {
    expect(planReplay('a', { readyVideoUrls: {}, liveSince: { a: 1_000 } }, 6_000)).toEqual({
      kind: 'wait',
      remainingMs: REPLAY_READY_GRACE_MS - 5_000,
    });
  });

  it('prepares replays whose ready event was missed or is overdue', () => {
    expect(planReplay('a', { readyVideoUrls: {}, liveSince: {} }, 0)).toEqual({ kind: 'prepare' });
    expect(planReplay('a', { readyVideoUrls: {}, liveSince: { a: 0 } }, REPLAY_READY_GRACE_MS)).toEqual({
      kind: 'prepare',
    });
  });
});

describe('prepareReplayWithRetry', () => {
  it('backs off on 409 and 503 until the video is prepared', async () => {
    const prepare = vi
      .fn()
      .mockRejectedValueOnce(new ReplayPreparationRequestError('not ready', 409))
      .mockRejectedValueOnce(new ReplayPreparationRequestError('busy', 503))
      .mockResolvedValueOnce(prepared('a'));
    const sleep = vi.fn(noSleep);

    await expect(prepareReplayWithRetry('a', { prepare, sleep, delaysMs: [10, 20, 40] })).resolves.toEqual(
      prepared('a')
    );
    expect(prepare).toHaveBeenCalledTimes(3);
    expect(sleep.mock.calls.map(([ms]) => ms)).toEqual([10, 20]);
  });

  it('retries when the server could not be reached', async () => {
    const prepare = vi
      .fn()
      .mockRejectedValueOnce(new ReplayPreparationRequestError('offline'))
      .mockResolvedValueOnce(prepared('a'));

    await expect(prepareReplayWithRetry('a', { prepare, sleep: noSleep })).resolves.toEqual(prepared('a'));
  });

  it('does not retry a missing replay', async () => {
    const error = new ReplayPreparationRequestError('Camera replay was not found', 404);
    const prepare = vi.fn().mockRejectedValue(error);

    await expect(prepareReplayWithRetry('a', { prepare, sleep: noSleep })).rejects.toBe(error);
    expect(prepare).toHaveBeenCalledOnce();
  });

  it('gives up after the last backoff step', async () => {
    const prepare = vi.fn().mockRejectedValue(new ReplayPreparationRequestError('busy', 503));

    await expect(prepareReplayWithRetry('a', { prepare, sleep: noSleep, delaysMs: [1, 1] })).rejects.toThrow('busy');
    expect(prepare).toHaveBeenCalledTimes(3);
  });

  it('stops when aborted', async () => {
    const controller = new AbortController();
    const prepare = vi.fn().mockImplementation(async () => {
      controller.abort();
      throw new ReplayPreparationRequestError('busy', 503);
    });

    await expect(prepareReplayWithRetry('a', { prepare, sleep: noSleep, signal: controller.signal })).rejects.toThrow(
      'busy'
    );
    expect(prepare).toHaveBeenCalledOnce();
  });
});

describe('resolveReplayVideo', () => {
  it('uses the ready announcement without calling prepare', async () => {
    vi.useFakeTimers();
    useReplayAvailabilityStore.getState().markLive('a', 0);
    const prepare = vi.fn();

    const pending = resolveReplayVideo('a', { prepare, now: () => 1_000 });
    useReplayAvailabilityStore.getState().markReady('a', 'http://pi/a.mp4');

    await expect(pending).resolves.toBe('http://pi/a.mp4');
    expect(prepare).not.toHaveBeenCalled();
  });

  it('falls back to prepare when the live shot is never announced', async () => {
    vi.useFakeTimers();
    useReplayAvailabilityStore.getState().markLive('a', 0);
    const prepare = vi.fn().mockResolvedValue(prepared('a'));

    const pending = resolveReplayVideo('a', { prepare, now: () => 0 });
    await vi.advanceTimersByTimeAsync(REPLAY_READY_GRACE_MS);

    await expect(pending).resolves.toBe(prepared('a').videoUrl);
    expect(prepare).toHaveBeenCalledOnce();
    expect(useReplayAvailabilityStore.getState().readyVideoUrls.a).toBe(prepared('a').videoUrl);
  });

  it('prepares immediately when the client connected after the event', async () => {
    const prepare = vi.fn().mockResolvedValue(prepared('late'));

    await expect(resolveReplayVideo('late', { prepare })).resolves.toBe(prepared('late').videoUrl);
    expect(prepare).toHaveBeenCalledWith('late', undefined);
  });

  it('rejects when aborted while waiting for the announcement', async () => {
    vi.useFakeTimers();
    useReplayAvailabilityStore.getState().markLive('a', 0);
    const controller = new AbortController();
    const prepare = vi.fn();

    const pending = resolveReplayVideo('a', { prepare, now: () => 0, signal: controller.signal });
    controller.abort();

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
    expect(prepare).not.toHaveBeenCalled();
  });
});
