import type { CameraReplay } from '../types/shot';

type ReplayTiming = Pick<CameraReplay, 'trigger_frame' | 'playback_fps' | 'duration_seconds' | 'capture_fps'>;

export function getReplayPlaybackRate(
  replay: { playback_fps: number; capture_fps?: number | null },
  speed: number
): number {
  const captureRate = replay.capture_fps;
  if (
    !captureRate ||
    !Number.isFinite(captureRate) ||
    captureRate <= 0 ||
    !Number.isFinite(replay.playback_fps) ||
    replay.playback_fps <= 0
  )
    return speed;
  return Math.min(16, Math.max(0.0625, (speed * captureRate) / replay.playback_fps));
}

export function getReplayStartTime(replay: ReplayTiming, playbackRate: number): number {
  const triggerTime = replay.trigger_frame / replay.playback_fps;
  if (
    !Number.isFinite(triggerTime) ||
    replay.playback_fps <= 0 ||
    triggerTime < 0 ||
    !Number.isFinite(replay.duration_seconds) ||
    triggerTime >= replay.duration_seconds ||
    !Number.isFinite(playbackRate) ||
    playbackRate <= 0 ||
    playbackRate >= 1
  )
    return 0;

  // Keep 1.5 seconds of viewing time before the trigger, not 1.5 seconds of video.
  return Math.max(0, triggerTime - 1.5 * getReplayPlaybackRate(replay, playbackRate));
}

export function formatReplayTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00.0';
  const minutes = Math.floor(seconds / 60);
  const remainder = (seconds % 60).toFixed(1).padStart(4, '0');
  return `${minutes}:${remainder}`;
}
