import { create } from 'zustand';
import type { SavedSession } from '../analysis/data';
import { loadSessions } from '../analysis/load';

export interface DispersionHistory {
  status: 'idle' | 'loading' | 'ready' | 'error';
  sessions: SavedSession[];
  unavailable: string[];
}

interface DispersionHistoryState extends DispersionHistory {
  /** Fetch the 30-day history once per page; `refresh` refetches and keeps the shown data on failure. */
  load: (refresh?: boolean) => Promise<void>;
}

export const useDispersionHistoryStore = create<DispersionHistoryState>((set, get) => ({
  status: 'idle',
  sessions: [],
  unavailable: [],
  load: async (refresh = false) => {
    const { status } = get();
    if (status === 'loading' || (status === 'ready' && !refresh)) return;
    set({ status: 'loading' });
    try {
      const { sessions, unavailable } = await loadSessions(new AbortController().signal);
      set({ status: 'ready', sessions, unavailable });
    } catch {
      set({ status: 'error' });
    }
  },
}));
