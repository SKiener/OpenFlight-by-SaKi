import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  codeFromHash,
  exchangePairingCode,
  fetchPairingAccess,
  formatCountdown,
  formatPairingCode,
  isCompletePairingCode,
  issuePairingCode,
  normalizePairingCode,
  revokeAllDevices,
  secondsLeft,
  typedPairingAddress,
} from './pairingApi';

afterEach(() => {
  vi.unstubAllGlobals();
});

function respond(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (body === undefined) throw new SyntaxError('no body');
      return body;
    },
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('pairing codes', () => {
  it('normalizes typed codes and shows them in two groups of four', () => {
    expect(normalizePairingCode(' ab2c-d3ef ')).toBe('AB2CD3EF');
    expect(formatPairingCode('AB2CD3EF')).toBe('AB2C-D3EF');
    expect(formatPairingCode('SHORT')).toBe('SHORT');
  });

  it('only treats eight characters as a complete code so typos do not cost attempts', () => {
    expect(isCompletePairingCode('ab2c-d3e')).toBe(false);
    expect(isCompletePairingCode('ab2c-d3ef')).toBe(true);
  });

  it('reads the code from the QR link fragment', () => {
    expect(codeFromHash('#code=ab2cd3ef')).toBe('AB2CD3EF');
    expect(codeFromHash('#code=')).toBeNull();
    expect(codeFromHash('')).toBeNull();
  });

  it('counts down whole seconds and never below zero', () => {
    expect(secondsLeft(10_000, 0)).toBe(10);
    expect(secondsLeft(10_000, 9_001)).toBe(1);
    expect(secondsLeft(10_000, 12_000)).toBe(0);
    expect(formatCountdown(299)).toBe('4:59');
    expect(formatCountdown(5)).toBe('0:05');
  });

  it('turns a QR link into the address to type by hand', () => {
    expect(typedPairingAddress('http://openflight.local:8080/pair#code=AB2CD3EF')).toBe('openflight.local:8080/pair');
  });
});

describe('pairing status', () => {
  it.each(['local', 'paired', 'unpaired'])('reports %s from the server', async (state) => {
    const fetchMock = respond(200, { state });

    await expect(fetchPairingAccess()).resolves.toBe(state);
    expect(fetchMock).toHaveBeenCalledWith('/api/pairing/status', { cache: 'no-store' });
  });

  it('is unknown when the server is unreachable or answers nonsense', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(fetchPairingAccess()).resolves.toBeNull();

    respond(200, { state: 'admin' });
    await expect(fetchPairingAccess()).resolves.toBeNull();

    respond(404, undefined);
    await expect(fetchPairingAccess()).resolves.toBeNull();
  });
});

describe('code exchange', () => {
  it('posts the normalized code as JSON', async () => {
    const fetchMock = respond(200, { state: 'paired' });

    await expect(exchangePairingCode('ab2c-d3ef')).resolves.toEqual({ kind: 'paired' });
    expect(fetchMock).toHaveBeenCalledWith('/api/pairing/exchange', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: 'AB2CD3EF' }),
    });
  });

  it('distinguishes wrong codes, rate limiting, and an unreachable server', async () => {
    respond(403, { error: 'invalid_code' });
    await expect(exchangePairingCode('AB2CD3EF')).resolves.toEqual({ kind: 'invalid' });

    respond(429, { error: 'too_many_attempts', retry_after_s: 42 });
    await expect(exchangePairingCode('AB2CD3EF')).resolves.toEqual({ kind: 'rate_limited', retryAfterS: 42 });

    respond(429, undefined);
    await expect(exchangePairingCode('AB2CD3EF')).resolves.toEqual({ kind: 'rate_limited', retryAfterS: 60 });

    respond(503, { error: 'pairing_unavailable' });
    await expect(exchangePairingCode('AB2CD3EF')).resolves.toEqual({ kind: 'unavailable' });

    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')));
    await expect(exchangePairingCode('AB2CD3EF')).resolves.toEqual({ kind: 'unavailable' });
  });
});

describe('kiosk pairing administration', () => {
  it('issues a code with its expiry and only well-formed QR links', async () => {
    const fetchMock = respond(200, {
      code: 'AB2CD3EF',
      expires_in_s: 300,
      links: [
        {
          host: 'openflight.local',
          url: 'http://openflight.local:8080/pair#code=AB2CD3EF',
          qr_svg: 'data:image/svg+xml,<svg/>',
        },
        { host: 'evil', url: 'http://x', qr_svg: 'javascript:alert(1)' },
        'nonsense',
      ],
    });

    const issued = await issuePairingCode(1_000);

    expect(fetchMock).toHaveBeenCalledWith('/api/pairing/code', { method: 'POST', cache: 'no-store' });
    expect(issued).toEqual({
      code: 'AB2CD3EF',
      expiresAtMs: 301_000,
      links: [
        {
          host: 'openflight.local',
          url: 'http://openflight.local:8080/pair#code=AB2CD3EF',
          qrSvg: 'data:image/svg+xml,<svg/>',
        },
      ],
      wifi: null,
    });
  });

  it('reads the launch monitor Wi-Fi only when it has a password', async () => {
    const withWifi = async (wifi: unknown) => {
      respond(200, { code: 'AB2CD3EF', expires_in_s: 300, links: [], wifi });
      return (await issuePairingCode(0)).wifi;
    };

    expect(await withWifi({ ssid: 'TestNet', password: 'test-pass-123' })).toEqual({
      ssid: 'TestNet',
      password: 'test-pass-123',
      qrSvg: null,
    });
    expect(await withWifi({ ssid: null, password: 'test-pass-123' })).toEqual({
      ssid: null,
      password: 'test-pass-123',
      qrSvg: null,
    });
    expect(await withWifi({ ssid: 42, password: 'test-pass-123' })).toEqual({
      ssid: null,
      password: 'test-pass-123',
      qrSvg: null,
    });
    expect(await withWifi({ password: 'test-pass-123' })).toEqual({
      ssid: null,
      password: 'test-pass-123',
      qrSvg: null,
    });
    expect(await withWifi(undefined)).toBeNull();
    expect(await withWifi(null)).toBeNull();
    expect(await withWifi('TestNet')).toBeNull();
    expect(await withWifi({ ssid: 'TestNet' })).toBeNull();
    expect(await withWifi({ ssid: 'TestNet', password: '' })).toBeNull();
    expect(await withWifi({ ssid: 'TestNet', password: 12345678 })).toBeNull();
  });

  it('reads the Wi-Fi join QR code only when it is an SVG data URI', async () => {
    const qrSvgFor = async (qrSvg: unknown) => {
      respond(200, {
        code: 'AB2CD3EF',
        expires_in_s: 300,
        links: [],
        wifi: { ssid: 'TestNet', password: 'test-pass-123', qr_svg: qrSvg },
      });
      return (await issuePairingCode(0)).wifi?.qrSvg;
    };

    expect(await qrSvgFor('data:image/svg+xml,<svg/>')).toBe('data:image/svg+xml,<svg/>');
    expect(await qrSvgFor(undefined)).toBeNull();
    expect(await qrSvgFor(null)).toBeNull();
    expect(await qrSvgFor(42)).toBeNull();
    expect(await qrSvgFor({ svg: 'data:image/svg+xml,<svg/>' })).toBeNull();
    expect(await qrSvgFor('')).toBeNull();
    expect(await qrSvgFor('javascript:alert(1)')).toBeNull();
    expect(await qrSvgFor('data:image/png;base64,AAAA')).toBeNull();
  });

  it('fails loudly when the code cannot be issued', async () => {
    respond(403, { error: 'local_only' });
    await expect(issuePairingCode(0)).rejects.toThrow('403');

    respond(200, { code: 'AB2CD3EF' });
    await expect(issuePairingCode(0)).rejects.toThrow('incomplete');
  });

  it('revokes every device or reports the failure', async () => {
    const fetchMock = respond(200, { status: 'revoked' });
    await expect(revokeAllDevices()).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith('/api/pairing/revoke', { method: 'POST' });

    respond(503, { error: 'revoke_failed' });
    await expect(revokeAllDevices()).rejects.toThrow('503');
  });
});
