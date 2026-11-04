import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useDispersionHistoryStore } from './useDispersionHistoryStore';

const session = (id: string) => ({ id, started_at: '2026-10-03T10:00:00', shot_count: 0, clubs: [], shots: [] });

function stubHistory(ids: string[], status = 200) {
  const fetcher = vi.fn(async (url: string) => {
    if (status !== 200) return new Response('', { status });
    if (url.endsWith('/api/session-history')) return Response.json({ sessions: ids.map((id) => ({ id })) });
    return Response.json(session(url.split('/').at(-1)!));
  });
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

beforeEach(() => {
  useDispersionHistoryStore.setState({ status: 'idle', sessions: [], unavailable: [] });
});

afterEach(() => vi.unstubAllGlobals());

describe('dispersion history cache', () => {
  it('loads saved sessions lazily once and only refetches on refresh', async () => {
    const fetcher = stubHistory(['one']);
    const { load } = useDispersionHistoryStore.getState();

    const first = load();
    expect(useDispersionHistoryStore.getState().status).toBe('loading');
    await Promise.all([first, load()]);
    expect(useDispersionHistoryStore.getState()).toMatchObject({ status: 'ready', unavailable: [] });
    expect(useDispersionHistoryStore.getState().sessions.map((entry) => entry.id)).toEqual(['one']);
    expect(fetcher).toHaveBeenCalledTimes(2);

    await load();
    expect(fetcher).toHaveBeenCalledTimes(2);

    stubHistory(['one', 'two']);
    await load(true);
    expect(useDispersionHistoryStore.getState().sessions.map((entry) => entry.id)).toEqual(['one', 'two']);
  });

  it('reports a failed listing and keeps what was already shown', async () => {
    stubHistory(['one']);
    await useDispersionHistoryStore.getState().load();

    stubHistory([], 503);
    await useDispersionHistoryStore.getState().load(true);

    expect(useDispersionHistoryStore.getState().status).toBe('error');
    expect(useDispersionHistoryStore.getState().sessions).toHaveLength(1);

    stubHistory(['one']);
    await useDispersionHistoryStore.getState().load();
    expect(useDispersionHistoryStore.getState().status).toBe('ready');
  });
});
