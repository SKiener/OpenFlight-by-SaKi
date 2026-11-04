import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Shot } from '../types/shot';
import { demoRowsMayShow, useShotStore } from './useShotStore';

const shot = {
  ball_speed_mph: 145,
  timestamp: '2026-08-12T12:00:00Z',
} as Shot;

describe('useShotStore processing lifecycle', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useShotStore.setState({
      latestShot: null,
      shots: [],
      isNewShot: false,
      shotProcessingPhase: null,
      shotProcessingShotTimestamp: null,
      shotVersion: 0,
    });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('keeps current shot data while marking the next shot as processing', () => {
    useShotStore.setState({ latestShot: shot, shots: [shot] });

    useShotStore.getState().startShotProcessing('capturing');

    expect(useShotStore.getState().shotProcessingPhase).toBe('capturing');
    expect(useShotStore.getState().latestShot).toBe(shot);
    expect(useShotStore.getState().shots).toEqual([shot]);
  });

  it('clears processing when the completed shot arrives', () => {
    useShotStore.getState().startShotProcessing('capturing');
    useShotStore.getState().startShotProcessing('calculating');

    useShotStore.getState().addShot(shot);

    expect(useShotStore.getState().shotProcessingPhase).toBeNull();
  });

  it('clears a stale processing state after the watchdog timeout', () => {
    useShotStore.getState().startShotProcessing('capturing');

    vi.advanceTimersByTime(30_000);

    expect(useShotStore.getState().shotProcessingPhase).toBeNull();
  });

  it('replaces a provisional shot without replaying new-shot effects', () => {
    const enriched = {
      ...shot,
      launch_angle_vertical: 17.4,
      launch_angle_vertical_source: 'radar',
    } as Shot;
    useShotStore.setState({
      latestShot: shot,
      shots: [shot],
      shotProcessingPhase: 'iwr_dump',
      shotProcessingShotTimestamp: shot.timestamp,
      isNewShot: true,
      shotVersion: 1,
    });

    useShotStore.getState().updateShot(enriched);

    expect(useShotStore.getState().shots).toEqual([enriched]);
    expect(useShotStore.getState().latestShot).toBe(enriched);
    expect(useShotStore.getState().shotProcessingPhase).toBeNull();
    expect(useShotStore.getState().shotProcessingShotTimestamp).toBeNull();
    expect(useShotStore.getState().isNewShot).toBe(true);
    expect(useShotStore.getState().shotVersion).toBe(1);
  });

  it('does not clear a newer IWR dump indicator when an older shot update arrives', () => {
    const newerShot = { ...shot, timestamp: '2026-08-12T12:01:00Z' } as Shot;
    useShotStore.setState({
      latestShot: newerShot,
      shots: [shot, newerShot],
      shotProcessingPhase: 'iwr_dump',
      shotProcessingShotTimestamp: newerShot.timestamp,
    });

    useShotStore.getState().updateShot({ ...shot, launch_angle_vertical: 17.4 } as Shot);

    expect(useShotStore.getState().shotProcessingPhase).toBe('iwr_dump');
    expect(useShotStore.getState().shotProcessingShotTimestamp).toBe(newerShot.timestamp);
  });

  it('patches an earlier shot with its late club data without touching the current shot', () => {
    const newerShot = { ...shot, timestamp: '2026-08-12T12:01:00Z' } as Shot;
    useShotStore.setState({ latestShot: newerShot, shots: [shot, newerShot], isNewShot: false, shotVersion: 2 });

    const withClubData = { ...shot, club_data_status: 'done', camera_club_path_deg: 1.2 } as Shot;
    useShotStore.getState().updateShot(withClubData);

    const state = useShotStore.getState();
    expect(state.shots).toEqual([withClubData, newerShot]);
    expect(state.latestShot).toBe(newerShot);
    expect(state.isNewShot).toBe(false);
    expect(state.shotVersion).toBe(2);
  });

  it('ignores enrichment for a shot that was already removed', () => {
    useShotStore.getState().updateShot(shot);

    expect(useShotStore.getState().shots).toEqual([]);
    expect(useShotStore.getState().latestShot).toBeNull();
  });
});

describe('useShotStore session generation', () => {
  const other = { ...shot, timestamp: '2026-08-12T12:01:00Z' } as Shot;
  const demoShot = { ...shot, timestamp: '2026-08-12T12:02:00Z' } as Shot;
  const generation = () => useShotStore.getState().sessionGeneration;

  beforeEach(() => {
    vi.useFakeTimers();
    useShotStore.getState().clearShots();
    useShotStore.setState(useShotStore.getInitialState(), true);
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
  });

  it('starts a new generation for the first session, a flipped demo flag and a clear, not for a resend', () => {
    const { receiveSession, clearSession } = useShotStore.getState();

    receiveSession([shot], false);
    expect(generation(), 'the first session of the page').toBe(1);
    receiveSession([shot, other], false);
    expect(generation(), 'the same session, resent').toBe(1);
    expect(useShotStore.getState().shots).toEqual([shot, other]);

    receiveSession([demoShot], true);
    expect(generation(), 'the demo started').toBe(2);
    receiveSession([demoShot], true);
    expect(generation()).toBe(2);
    receiveSession([shot, other], false);
    expect(generation(), 'the demo ended').toBe(3);

    clearSession([other]);
    expect(useShotStore.getState().shots).toEqual([other]);
    clearSession([]);
    expect(useShotStore.getState().shots).toEqual([]);
    expect(generation(), 'each clear').toBe(5);
  });

  it('keeps the generation for a shot, its update and a list set directly', () => {
    useShotStore.getState().addShot(shot);
    useShotStore.getState().updateShot({ ...shot, carry_spin_adjusted: 200 });
    useShotStore.getState().setShots([shot]);
    useShotStore.getState().clearShots();

    expect(generation()).toBe(0);
  });
});

describe('demoRowsMayShow', () => {
  it('holds from the demo switching on until the real session is back, in either message order', () => {
    expect(demoRowsMayShow(false, null), 'no session yet').toBe(false);
    expect(demoRowsMayShow(false, false), 'the real session').toBe(false);
    expect(demoRowsMayShow(true, false), 'demo_state before the demo session').toBe(true);
    expect(demoRowsMayShow(true, true), 'the demo session').toBe(true);
    expect(demoRowsMayShow(false, true), 'demo_state before the real session').toBe(true);
  });
});
