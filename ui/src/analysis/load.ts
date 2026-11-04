import { getServerOrigin } from '../utils/serverOrigin';
import type { SavedSession } from './data';

export async function loadSessions(signal: AbortSignal) {
  const get = async (path: string) => {
    const response = await fetch(`${getServerOrigin()}${path}`, { signal, cache: 'no-store' });
    if (!response.ok) throw new Error(`History: ${response.status}`);
    return response.json();
  };
  const listing = await get('/api/session-history') as { sessions: Omit<SavedSession, 'shots'>[] };
  if (!Array.isArray(listing.sessions)) throw new Error('Invalid session list');
  const sessions: SavedSession[] = [];
  const unavailable: string[] = [];
  const queue = [...listing.sessions];
  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, async () => {
    while (queue.length && !signal.aborted) {
      const summary = queue.shift()!;
      try {
        const session = await get(`/api/session-history/${encodeURIComponent(summary.id)}`) as SavedSession;
        if (session.id !== summary.id || !Array.isArray(session.shots)) throw new Error('Invalid session');
        sessions.push(session);
      } catch (error) {
        if (signal.aborted) throw error;
        unavailable.push(summary.id);
      }
    }
  }));
  return { sessions: sessions.sort((a, b) => b.started_at.localeCompare(a.started_at)), unavailable };
}
