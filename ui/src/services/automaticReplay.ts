import { useShotStore } from '../stores/useShotStore';
import { isSwingSpeedShot, type CameraReplay, type Shot } from '../types/shot';

export function subscribeToAutomaticReplays(onReplay: (replay: CameraReplay, shot: Shot) => void) {
  let pendingTimestamp: string | null = null;
  return useShotStore.subscribe((state, previous) => {
    if (state.shotVersion !== previous.shotVersion) {
      pendingTimestamp = !isSwingSpeedShot(state.latestShot) ? (state.latestShot?.timestamp ?? null) : null;
    }
    const shot = state.latestShot;
    if (!shot || shot.timestamp !== pendingTimestamp) {
      pendingTimestamp = null;
      return;
    }
    if (shot.camera_replay) {
      pendingTimestamp = null;
      onReplay(shot.camera_replay, shot);
    }
  });
}
