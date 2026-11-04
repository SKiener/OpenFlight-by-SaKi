import { create } from 'zustand';

interface ReplayAvailabilityState {
  /** Absolute MP4 URL per replay id, once the server says the video exists. */
  readyVideoUrls: Record<string, string>;
  /** When a replay was first announced live on this connection (ms epoch). */
  liveSince: Record<string, number>;
  markReady: (replayId: string, videoUrl: string) => void;
  markLive: (replayId: string, at: number) => void;
  /** Drop a replay so the next request asks the server again. */
  forget: (replayId: string) => void;
}

export const useReplayAvailabilityStore = create<ReplayAvailabilityState>((set) => ({
  readyVideoUrls: {},
  liveSince: {},
  markReady: (replayId, videoUrl) =>
    set((state) =>
      state.readyVideoUrls[replayId] === videoUrl
        ? state
        : { readyVideoUrls: { ...state.readyVideoUrls, [replayId]: videoUrl } }
    ),
  markLive: (replayId, at) =>
    set((state) => (replayId in state.liveSince ? state : { liveSince: { ...state.liveSince, [replayId]: at } })),
  forget: (replayId) =>
    set((state) => {
      const readyVideoUrls = { ...state.readyVideoUrls };
      const liveSince = { ...state.liveSince };
      delete readyVideoUrls[replayId];
      delete liveSince[replayId];
      return { readyVideoUrls, liveSince };
    }),
}));
