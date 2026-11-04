import { useReplayAvailabilityStore } from '../stores/useReplayAvailabilityStore';
import type { Shot } from '../types/shot';
import { getServerOrigin } from '../utils/serverOrigin';

export interface CameraReplayReadyMessage {
  shot_number: number | null;
  replay_id: string;
  video_url: string;
}

export function handleCameraReplayReady(data: CameraReplayReadyMessage) {
  if (typeof data?.replay_id !== 'string' || typeof data.video_url !== 'string') return;
  const videoUrl = new URL(data.video_url, `${getServerOrigin()}/`).toString();
  useReplayAvailabilityStore.getState().markReady(data.replay_id, videoUrl);
}

export function noteLiveReplay(shot: Shot, now = Date.now()) {
  if (shot.camera_replay?.id) {
    useReplayAvailabilityStore.getState().markLive(shot.camera_replay.id, now);
  }
}
