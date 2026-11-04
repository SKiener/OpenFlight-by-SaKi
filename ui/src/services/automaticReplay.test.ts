import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { subscribeToAutomaticReplays } from './automaticReplay';
import { useShotStore } from '../stores/useShotStore';
import type { Shot } from '../types/shot';

const shot = { timestamp: '2026-10-01T12:00:00Z', camera_replay: { id: 'first' } } as Shot;
let unsubscribe: () => void;
const open = vi.fn();

beforeEach(() => {
  vi.useFakeTimers();
  open.mockClear();
  useShotStore.getState().clearShots();
  unsubscribe = subscribeToAutomaticReplays(open);
});
afterEach(() => {
  unsubscribe();
  vi.clearAllTimers();
  vi.useRealTimers();
});

describe('automatic shot replays', () => {
  it('opens a new captured shot exactly once', () => {
    useShotStore.getState().addShot(shot);
    useShotStore.getState().updateShot({ ...shot });
    vi.advanceTimersByTime(3000);
    expect(open).toHaveBeenCalledExactlyOnceWith(shot.camera_replay, shot);
  });
  it('waits for late camera enrichment without relying on the brief new-shot animation', () => {
    useShotStore.getState().addShot({ ...shot, camera_replay: null });
    vi.advanceTimersByTime(5000);
    expect(open).not.toHaveBeenCalled();
    useShotStore.getState().updateShot(shot);
    expect(open).toHaveBeenCalledOnce();
  });
  it('opens the enriched replay without changing its completed carry', () => {
    useShotStore.getState().addShot({ ...shot, camera_replay: null,
      estimated_carry_yards: 155.3, carry_spin_adjusted: null });
    expect(open).not.toHaveBeenCalled();
    const final = { ...shot, estimated_carry_yards: 160, carry_spin_adjusted: 146.4 };
    useShotStore.getState().updateShot(final);
    expect(open).toHaveBeenCalledExactlyOnceWith(final.camera_replay, final);
    vi.advanceTimersByTime(30000);
    expect(useShotStore.getState().latestShot?.carry_spin_adjusted).toBe(146.4);
    expect(useShotStore.getState().shots.at(-1)?.carry_spin_adjusted).toBe(146.4);
    expect(open).toHaveBeenCalledOnce();
  });
  it('does not replay historical shots on loading or reconnecting', () => {
    useShotStore.getState().setShots([shot]);
    useShotStore.getState().updateShot(shot);
    expect(open).not.toHaveBeenCalled();
  });
  it('does not replay an older shot when a newer shot is waiting', () => {
    useShotStore.getState().addShot({ ...shot, camera_replay: null });
    const next = { ...shot, timestamp: '2026-10-01T12:01:00Z' };
    useShotStore.getState().addShot({ ...next, camera_replay: null });
    useShotStore.getState().updateShot(shot);
    expect(open).not.toHaveBeenCalled();
    useShotStore.getState().updateShot(next);
    expect(open).toHaveBeenCalledExactlyOnceWith(next.camera_replay, next);
  });
  it('cancels pending replay when the session is cleared', () => {
    useShotStore.getState().addShot({ ...shot, camera_replay: null });
    useShotStore.getState().clearShots();
    useShotStore.getState().setShots([shot]);
    expect(open).not.toHaveBeenCalled();
  });
  it('ignores swing-speed sessions', () => {
    useShotStore.getState().addShot({ ...shot, mode: 'swing-speed' });
    expect(open).not.toHaveBeenCalled();
  });
});
