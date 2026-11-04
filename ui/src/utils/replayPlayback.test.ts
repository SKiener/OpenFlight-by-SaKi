import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  REFUSED_AUTOMATIC_REPLAY_TIMEOUT_MS,
  scheduleRefusedAutomaticReturn,
  startReplayPlayback,
} from './replayPlayback';

const domError = (name: string) => Object.assign(new Error(name), { name });

describe('startReplayPlayback', () => {
  it('reports playing when the browser starts the video', async () => {
    const play = vi.fn().mockResolvedValue(undefined);
    await expect(startReplayPlayback({ play })).resolves.toBe('playing');
    expect(play).toHaveBeenCalledOnce();
  });

  it('reports blocked when the platform refuses playback without a tap (iOS Low Power Mode)', async () => {
    const play = vi.fn().mockRejectedValue(domError('NotAllowedError'));
    await expect(startReplayPlayback({ play })).resolves.toBe('blocked');
  });

  it('ignores a play request cancelled by a pause, seek, or source change', async () => {
    const play = vi.fn().mockRejectedValue(domError('AbortError'));
    await expect(startReplayPlayback({ play })).resolves.toBe('aborted');
  });

  it('reports other rejections as real playback failures', async () => {
    await expect(startReplayPlayback({ play: vi.fn().mockRejectedValue(domError('NotSupportedError')) })).resolves.toBe(
      'failed'
    );
    await expect(startReplayPlayback({ play: vi.fn().mockRejectedValue('boom') })).resolves.toBe('failed');
  });

  it('treats a synchronous throw like a rejection', async () => {
    const play = vi.fn(() => {
      throw domError('NotAllowedError');
    });
    await expect(startReplayPlayback({ play })).resolves.toBe('blocked');
  });

  it('copes with legacy browsers whose play() returns nothing', async () => {
    await expect(startReplayPlayback({ play: () => undefined as unknown as Promise<void> })).resolves.toBe('playing');
  });
});

describe('scheduleRefusedAutomaticReturn', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('returns a refused automatic replay to Live after 8 s without a tap', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    scheduleRefusedAutomaticReturn({ automatic: true, blocked: true, paused: true }, onClose);
    expect(REFUSED_AUTOMATIC_REPLAY_TIMEOUT_MS).toBe(8000);
    vi.advanceTimersByTime(7999);
    expect(onClose).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('cancels the return when the user taps play', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    const cancel = scheduleRefusedAutomaticReturn({ automatic: true, blocked: true, paused: true }, onClose);
    vi.advanceTimersByTime(5000);
    cancel?.();
    vi.advanceTimersByTime(60_000);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('never closes manual replays or replays that are playing', () => {
    vi.useFakeTimers();
    const onClose = vi.fn();
    expect(scheduleRefusedAutomaticReturn({ automatic: false, blocked: true, paused: true }, onClose)).toBeUndefined();
    expect(scheduleRefusedAutomaticReturn({ automatic: true, blocked: false, paused: true }, onClose)).toBeUndefined();
    expect(scheduleRefusedAutomaticReturn({ automatic: true, blocked: true, paused: false }, onClose)).toBeUndefined();
    vi.advanceTimersByTime(60_000);
    expect(onClose).not.toHaveBeenCalled();
  });
});
