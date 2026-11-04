import { afterEach, describe, expect, it, vi } from 'vitest';
import { loadSessions } from './load';

afterEach(() => vi.unstubAllGlobals());
describe('analysis loading', () => {
  it('surfaces unavailable sessions instead of silently exporting incomplete data', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('session-history')) return Response.json({ sessions: [{ id: 'one' }, { id: 'two' }] });
      if (url.endsWith('one')) return Response.json({ id: 'one', shots: [], started_at: '2026-10-03' });
      return new Response('', { status: 404 });
    }));
    const result = await loadSessions(new AbortController().signal);
    expect(result.sessions.map((session) => session.id)).toEqual(['one']);
    expect(result.unavailable).toEqual(['two']);
  });
  it('does not turn a failed listing into an empty history', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })));
    await expect(loadSessions(new AbortController().signal)).rejects.toThrow('503');
  });
  it('rejects malformed responses and mismatched session IDs', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => Response.json(url.endsWith('session-history') ? { sessions: [{ id: 'one' }] } : { id: 'other', shots: [] })));
    expect((await loadSessions(new AbortController().signal)).unavailable).toEqual(['one']);
  });
  it('caps concurrent detail requests at three', async () => {
    let active = 0;
    let maximum = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.endsWith('session-history')) return Response.json({ sessions: Array.from({ length: 8 }, (_, index) => ({ id: String(index) })) });
      maximum = Math.max(maximum, ++active);
      await new Promise((resolve) => setTimeout(resolve, 2));
      active--;
      return Response.json({ id: url.split('/').at(-1), started_at: '2026-10-03', shots: [] });
    }));
    expect((await loadSessions(new AbortController().signal)).sessions).toHaveLength(8);
    expect(maximum).toBe(3);
  });
  it('passes cancellation to every fetch and stops the queue', async () => {
    const controller = new AbortController();
    const fetcher = vi.fn(async (_url: string, options: RequestInit) => {
      expect(options.signal).toBe(controller.signal);
      controller.abort();
      throw new DOMException('Aborted', 'AbortError');
    });
    vi.stubGlobal('fetch', fetcher);
    await expect(loadSessions(controller.signal)).rejects.toThrow('Aborted');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
