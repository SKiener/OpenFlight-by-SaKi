import { describe, expect, it } from 'vitest';
import { feedbackMessage, isPairPath, pairDialogView } from './pairingView';
import type { IssuedPairingCode } from './pairingApi';

const issued: IssuedPairingCode = {
  code: 'AB2CD3EF',
  expiresAtMs: 300_000,
  links: [
    { host: 'openflight.local', url: 'http://openflight.local:8080/pair#code=AB2CD3EF', qrSvg: 'data:image/svg+xml,x' },
  ],
  wifi: null,
};

describe('pairing routes', () => {
  it('matches /pair with or without a trailing slash', () => {
    expect(isPairPath('/pair')).toBe(true);
    expect(isPairPath('/pair/')).toBe(true);
    expect(isPairPath('/companion')).toBe(false);
    expect(isPairPath('/pairing')).toBe(false);
  });
});

describe('pairing feedback', () => {
  it('announces success politely and problems as alerts', () => {
    expect(feedbackMessage({ kind: 'idle' })).toBeNull();
    expect(feedbackMessage({ kind: 'pending' })).toBeNull();
    expect(feedbackMessage({ kind: 'paired' })).toEqual({ key: 'pairing.success', role: 'status' });
    expect(feedbackMessage({ kind: 'invalid' })).toEqual({ key: 'pairing.invalid', role: 'alert' });
    expect(feedbackMessage({ kind: 'rate_limited', retryAfterS: 42 })).toEqual({
      key: 'pairing.rateLimited',
      vars: { seconds: 42 },
      role: 'alert',
    });
    expect(feedbackMessage({ kind: 'unavailable' })).toEqual({ key: 'pairing.unavailable', role: 'alert' });
  });
});

describe('kiosk pairing dialog', () => {
  it('shows the code with its remaining time until it expires', () => {
    expect(pairDialogView({ kind: 'ready', issued }, 1_000)).toEqual({ kind: 'code', issued, secondsLeft: 299 });
    expect(pairDialogView({ kind: 'ready', issued }, 300_000)).toEqual({
      kind: 'notice',
      key: 'pairing.expired',
      role: 'alert',
    });
  });

  it('reports a paired device, a locked code, and a failed request', () => {
    expect(pairDialogView({ kind: 'closed', reason: 'paired' }, 0)).toEqual({
      kind: 'notice',
      key: 'pairing.paired',
      role: 'status',
      detailKey: 'pairing.pairedDetail',
    });
    expect(pairDialogView({ kind: 'closed', reason: 'locked' }, 0)).toMatchObject({
      key: 'pairing.locked',
      role: 'alert',
    });
    expect(pairDialogView({ kind: 'error' }, 0)).toMatchObject({ key: 'pairing.issueError', role: 'alert' });
    expect(pairDialogView({ kind: 'loading' }, 0)).toEqual({ kind: 'loading' });
  });
});
