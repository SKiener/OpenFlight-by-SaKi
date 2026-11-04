import { afterEach, describe, expect, it, vi } from 'vitest';
import { deleteSavedShot } from './historyApi';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('deleteSavedShot', () => {
  it('deletes a saved shot with an encoded timestamp', async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetch);

    await expect(
      deleteSavedShot('session_20261009_101500_range', '2026-10-09T10:15:00+02:00')
    ).resolves.toBeUndefined();
    expect(fetch).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(
        /\/api\/session-history\/session_20261009_101500_range\/shots\/2026-10-09T10%3A15%3A00%2B02%3A00$/
      ),
      { method: 'DELETE', cache: 'no-store' }
    );

    for (const status of [404, 400, 500, 200]) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => new Response(status === 200 ? '{}' : null, { status }))
      );
      await expect(deleteSavedShot('s', 't'), `status ${status}`).rejects.toThrow();
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('offline')))
    );
    await expect(deleteSavedShot('s', 't')).rejects.toThrow('offline');
  });
});
