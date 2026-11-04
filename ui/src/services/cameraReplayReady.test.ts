import { beforeEach, describe, expect, it } from 'vitest';
import { useReplayAvailabilityStore } from '../stores/useReplayAvailabilityStore';
import { useShotStore } from '../stores/useShotStore';
import type { Shot } from '../types/shot';
import { handleCameraReplayReady, noteLiveReplay } from './cameraReplayReady';
import { handleShotMessage, handleShotUpdate } from './handleShotMessage';

const shot = {
  timestamp: '2026-10-04T12:00:00Z',
  ball_speed_mph: 150,
  camera_replay: { id: 'replay-1' },
} as Shot;
const stats = {} as Parameters<typeof handleShotMessage>[0]['stats'];

beforeEach(() => {
  useReplayAvailabilityStore.setState({ readyVideoUrls: {}, liveSince: {} });
  useShotStore.getState().clearShots();
});

describe('camera_replay_ready', () => {
  it('stores an absolute video URL for the announced replay', () => {
    handleCameraReplayReady({ shot_number: 3, replay_id: 'replay-1', video_url: '/api/camera/replays/replay-1/video' });

    expect(useReplayAvailabilityStore.getState().readyVideoUrls).toEqual({
      'replay-1': 'http://localhost:8080/api/camera/replays/replay-1/video',
    });
  });

  it('ignores malformed payloads', () => {
    handleCameraReplayReady({ shot_number: 3 } as never);

    expect(useReplayAvailabilityStore.getState().readyVideoUrls).toEqual({});
  });
});

describe('live replay tracking', () => {
  it('records the first time a replay is announced on shot and shot_update', () => {
    handleShotMessage({ shot: { ...shot, camera_replay: null }, stats });
    expect(useReplayAvailabilityStore.getState().liveSince).toEqual({});

    handleShotUpdate({ shot, stats });
    const first = useReplayAvailabilityStore.getState().liveSince['replay-1'];
    expect(first).toEqual(expect.any(Number));

    noteLiveReplay(shot, first + 5_000);
    expect(useReplayAvailabilityStore.getState().liveSince['replay-1']).toBe(first);
  });
});
