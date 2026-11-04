import { create } from 'zustand';
import type { WeatherState } from '../types/weather';

/** The phone's own Open-Meteo fetch: never tried, running, or the last try failed. */
export type WeatherFetchStatus = 'idle' | 'fetching' | 'failed';

interface WeatherStoreState {
  state: WeatherState | null;
  /** performance.now() when `state` arrived: ages are counted on from there, not on the (Pi) wall clock. */
  receivedAt: number;
  fetchStatus: WeatherFetchStatus;
  /** performance.now() when the last failed fetch started, for the retry pause. */
  failedAt: number | null;
  /** performance.now() when the last successful fetch started, for the pause after a success. */
  succeededAt: number | null;
  /** Failed fetches in a row (each one doubles the retry pause); a successful one resets it. */
  consecutiveFailures: number;
  applyState: (state: WeatherState, receivedAt?: number) => void;
  setFetchStatus: (status: WeatherFetchStatus, at?: number) => void;
}

export const useWeatherStore = create<WeatherStoreState>((set) => ({
  state: null,
  receivedAt: 0,
  fetchStatus: 'idle',
  failedAt: null,
  succeededAt: null,
  consecutiveFailures: 0,
  applyState: (state, receivedAt = performance.now()) => set({ state, receivedAt }),
  setFetchStatus: (fetchStatus, at = performance.now()) =>
    set((current) =>
      fetchStatus === 'failed'
        ? { fetchStatus, failedAt: at, consecutiveFailures: current.consecutiveFailures + 1 }
        : fetchStatus === 'idle'
          ? // 'idle' after a fetch: it succeeded.
            { fetchStatus, succeededAt: at, consecutiveFailures: 0 }
          : { fetchStatus, consecutiveFailures: current.consecutiveFailures }
    ),
}));
