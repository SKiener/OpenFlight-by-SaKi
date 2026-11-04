import { useEffect, useState } from 'react';
import { ReplayPreparationRequestError } from '../services/cameraReplay';
import { useReplayAvailabilityStore } from '../stores/useReplayAvailabilityStore';
import type { CameraReplay } from '../types/shot';
import { resolveReplayVideo } from './companionReplay';

export type CompanionReplayState =
  | { kind: 'none' }
  | { kind: 'pending' }
  | { kind: 'ready'; videoUrl: string }
  | { kind: 'error'; stage: 'preparation' | 'playback'; message?: string; status?: number };

interface Failure {
  replayId: string;
  attempt: number;
  stage: 'preparation' | 'playback';
  message?: string;
  status?: number;
}

export function useCompanionReplay(replay: CameraReplay | null | undefined) {
  const replayId = replay?.id ?? null;
  const videoUrl = useReplayAvailabilityStore((state) => (replayId ? state.readyVideoUrls[replayId] : undefined));
  const [attempt, setAttempt] = useState(0);
  const [failure, setFailure] = useState<Failure | null>(null);

  useEffect(() => {
    if (!replayId || videoUrl) return;
    const controller = new AbortController();
    resolveReplayVideo(replayId, { signal: controller.signal }).catch((error: unknown) => {
      if (controller.signal.aborted) return;
      setFailure({
        replayId,
        attempt,
        stage: 'preparation',
        message: error instanceof ReplayPreparationRequestError ? error.message : undefined,
        status: error instanceof ReplayPreparationRequestError ? error.status : undefined,
      });
    });
    return () => controller.abort();
  }, [replayId, videoUrl, attempt]);

  const currentFailure = failure && failure.replayId === replayId && failure.attempt === attempt ? failure : null;
  let state: CompanionReplayState;
  if (!replayId) state = { kind: 'none' };
  else if (currentFailure) state = { ...currentFailure, kind: 'error' };
  else if (videoUrl) state = { kind: 'ready', videoUrl };
  else state = { kind: 'pending' };

  return {
    state,
    retry: () => {
      if (replayId && currentFailure?.stage === 'playback') {
        useReplayAvailabilityStore.getState().forget(replayId);
      }
      setAttempt((value) => value + 1);
    },
    reportPlaybackError: () => {
      if (replayId) setFailure({ replayId, attempt, stage: 'playback' });
    },
  };
}
