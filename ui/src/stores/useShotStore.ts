import { create } from 'zustand';
import type { Shot } from '../types/shot';

/** Duration to keep isNewShot true — covers the longest animation (shot-glow: 2s) */
const NEW_SHOT_DURATION_MS = 2500;
const SHOT_PROCESSING_TIMEOUT_MS = 30_000;
export type ShotProcessingPhase =
  'capturing' | 'calculating' | 'iwr_dump' | 'camera_processing' | 'hardware_enrichment';

interface ShotState {
  latestShot: Shot | null;
  shots: Shot[];
  isNewShot: boolean;
  shotProcessingPhase: ShotProcessingPhase | null;
  shotProcessingShotTimestamp: string | null;
  shotVersion: number;
  sessionGeneration: number;
  sessionDemo: boolean | null;
  startShotProcessing: (phase: ShotProcessingPhase, shotTimestamp?: string) => void;
  finishShotProcessing: () => void;
  addShot: (shot: Shot) => void;
  updateShot: (shot: Shot) => void;
  setShots: (shots: Shot[]) => void;
  clearShots: () => void;
  receiveSession: (shots: Shot[], demo: boolean) => void;
  clearSession: (remaining: Shot[]) => void;
}

/**
 * Demo rows may still be listed: the demo runs, or the store still holds its session. On switch-off
 * demo_state arrives before the real session_state, on switch-on it is the other way round.
 */
export const demoRowsMayShow = (demoActive: boolean, sessionDemo: boolean | null): boolean =>
  demoActive || sessionDemo === true;

type SessionPatch = (state: ShotState) => Partial<ShotState>;
const sameSession: SessionPatch = () => ({});
const newSession: SessionPatch = (state) => ({ sessionGeneration: state.sessionGeneration + 1 });

export const useShotStore = create<ShotState>((set) => {
  let timerRef: ReturnType<typeof setTimeout> | null = null;
  let processingTimerRef: ReturnType<typeof setTimeout> | null = null;

  const finishShotProcessing = () => {
    if (processingTimerRef) clearTimeout(processingTimerRef);
    processingTimerRef = null;
    set({ shotProcessingPhase: null, shotProcessingShotTimestamp: null });
  };

  const replaceShots = (newShots: Shot[], session: SessionPatch) => {
    finishShotProcessing();
    set((state) => ({
      shots: newShots,
      latestShot: newShots.length > 0 ? newShots[newShots.length - 1] : null,
      ...session(state),
    }));
  };

  const emptyShots = (session: SessionPatch) => {
    if (timerRef) clearTimeout(timerRef);
    if (processingTimerRef) clearTimeout(processingTimerRef);
    processingTimerRef = null;
    set((state) => ({
      latestShot: null,
      shots: [],
      isNewShot: false,
      shotProcessingPhase: null,
      shotProcessingShotTimestamp: null,
      ...session(state),
    }));
  };

  return {
    latestShot: null,
    shots: [],
    isNewShot: false,
    shotProcessingPhase: null,
    shotProcessingShotTimestamp: null,
    shotVersion: 0,
    sessionGeneration: 0,
    sessionDemo: null,
    startShotProcessing: (shotProcessingPhase, shotProcessingShotTimestamp) => {
      if (processingTimerRef) clearTimeout(processingTimerRef);
      set({ shotProcessingPhase, shotProcessingShotTimestamp: shotProcessingShotTimestamp ?? null });
      processingTimerRef = setTimeout(finishShotProcessing, SHOT_PROCESSING_TIMEOUT_MS);
    },
    finishShotProcessing,
    addShot: (shot) => {
      if (processingTimerRef) clearTimeout(processingTimerRef);
      processingTimerRef = null;
      set((state) => {
        const updated = [...state.shots, shot];
        const newShots = updated.length > 200 ? updated.slice(-200) : updated;
        return {
          latestShot: shot,
          shots: newShots,
          isNewShot: true,
          shotProcessingPhase: null,
          shotProcessingShotTimestamp: null,
          shotVersion: state.shotVersion + 1,
        };
      });

      if (timerRef) clearTimeout(timerRef);
      timerRef = setTimeout(() => {
        set({ isNewShot: false });
      }, NEW_SHOT_DURATION_MS);
    },
    updateShot: (shot) =>
      set((state) => {
        const index = state.shots.findIndex((existing) => existing.timestamp === shot.timestamp);
        if (index < 0) return state;

        const shots = [...state.shots];
        shots[index] = shot;
        const completesPendingShot = state.shotProcessingShotTimestamp === shot.timestamp;
        if (completesPendingShot && processingTimerRef) {
          clearTimeout(processingTimerRef);
          processingTimerRef = null;
        }
        return {
          shots,
          latestShot: state.latestShot?.timestamp === shot.timestamp ? shot : state.latestShot,
          shotProcessingPhase: completesPendingShot ? null : state.shotProcessingPhase,
          shotProcessingShotTimestamp: completesPendingShot ? null : state.shotProcessingShotTimestamp,
        };
      }),
    setShots: (newShots) => replaceShots(newShots, sameSession),
    clearShots: () => emptyShots(sameSession),
    // Every screen gets the session again whenever one connects; only the first and a flipped demo flag swap it.
    receiveSession: (newShots, demo) =>
      replaceShots(newShots, (state) =>
        state.sessionDemo === demo ? {} : { sessionDemo: demo, ...newSession(state) }
      ),
    clearSession: (remaining) => (remaining.length > 0 ? replaceShots(remaining, newSession) : emptyShots(newSession)),
  };
});
