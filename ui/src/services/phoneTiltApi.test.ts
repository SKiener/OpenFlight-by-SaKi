import { describe, expect, it, vi } from 'vitest';
import { useTiltCalibrationStore } from '../stores/useTiltCalibrationStore';
import { tiltStatus } from '../test-utils/tiltStatus';
import type { PhoneTiltRequest } from '../types/tiltCalibration';
import { calibratePhoneTilt, resetPhoneTilt } from './phoneTiltApi';

const request: PhoneTiltRequest = {
  position_a: { gravity: [0, 0.2, 9.8], pitch_std_deg: 0.05, sample_count: 150 },
  position_b: { gravity: [0, -0.1, 9.8], pitch_std_deg: 0.04, sample_count: 150 },
  measured_at: '2026-10-30T12:00:00.000Z',
  convention: 'w3c',
  client: { motion_source: 'native' },
};

function respond(status: number, body: unknown) {
  return vi.fn(() =>
    Promise.resolve(
      new Response(typeof body === 'string' ? body : JSON.stringify(body), {
        status,
        headers: { 'Content-Type': 'application/json' },
      })
    )
  );
}

describe('phone tilt REST client', () => {
  it('POSTs the measurement as JSON and returns the new status', async () => {
    const fetcher = respond(200, { status: 'applied', event: 'applied', ...tiltStatus() });
    const result = await calibratePhoneTilt(request, fetcher as unknown as typeof fetch);
    expect(result.ok).toBe(true);
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/calibration\/iwr6843\/phone-tilt$/);
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual(request);
  });

  it('returns the reason code of a 422, 409 or 401', async () => {
    for (const [status, error] of [
      [422, 'phone_bias_out_of_range'],
      [409, 'inclinometer_unstable'],
      [401, 'pairing_required'],
    ] as const) {
      const result = await calibratePhoneTilt(
        request,
        respond(status, { error, message: 'x' }) as unknown as typeof fetch
      );
      expect(result).toEqual({ ok: false, error });
    }
    const html = await calibratePhoneTilt(request, respond(502, '<html>') as unknown as typeof fetch);
    expect(html).toEqual({ ok: false, error: 'http_502' });
  });

  it('reports a failed request as network', async () => {
    const fetcher = vi.fn(() => Promise.reject(new TypeError('Failed to fetch')));
    expect(await calibratePhoneTilt(request, fetcher as unknown as typeof fetch)).toEqual({
      ok: false,
      error: 'network',
    });
  });

  it('DELETEs to reset and throws when the server refuses', async () => {
    const fetcher = respond(200, {
      status: 'removed',
      removed: true,
      ...tiltStatus({ tilt_source: 'cli', calibration: null }),
    });
    const status = await resetPhoneTilt(fetcher as unknown as typeof fetch);
    expect((fetcher.mock.calls[0] as unknown as [string, RequestInit])[1].method).toBe('DELETE');
    expect(status.tilt_source).toBe('cli');
    await expect(resetPhoneTilt(respond(500, { error: 'delete_failed' }) as unknown as typeof fetch)).rejects.toThrow(
      'delete_failed'
    );
  });
});

describe('tilt calibration store', () => {
  it('keeps the status without the event and reply wrappers', () => {
    useTiltCalibrationStore.getState().applyStatus({ event: 'applied', ...tiltStatus() });
    expect(useTiltCalibrationStore.getState().status).toEqual(tiltStatus());
    useTiltCalibrationStore
      .getState()
      .applyStatus({ ...tiltStatus({ tilt_source: 'cli' }), status: 'removed' } as unknown as ReturnType<
        typeof tiltStatus
      >);
    expect(useTiltCalibrationStore.getState().status).toEqual(tiltStatus({ tilt_source: 'cli' }));
  });
});
