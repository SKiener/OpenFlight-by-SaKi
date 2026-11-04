import type { MessageKey } from '../i18n';
import { secondsLeft, type ExchangeResult, type IssuedPairingCode, type PairingCodeClosedReason } from './pairingApi';

export const PAIR_PATH = '/pair';

export type PairingFeedback = { kind: 'idle' } | { kind: 'pending' } | ExchangeResult;

export interface FeedbackMessage {
  key: MessageKey;
  vars?: Record<string, number>;
  role: 'status' | 'alert';
}

export function isPairPath(pathname: string): boolean {
  return pathname.replace(/\/$/, '') === PAIR_PATH;
}

export function feedbackMessage(feedback: PairingFeedback): FeedbackMessage | null {
  switch (feedback.kind) {
    case 'paired':
      return { key: 'pairing.success', role: 'status' };
    case 'invalid':
      return { key: 'pairing.invalid', role: 'alert' };
    case 'rate_limited':
      return { key: 'pairing.rateLimited', vars: { seconds: feedback.retryAfterS }, role: 'alert' };
    case 'unavailable':
      return { key: 'pairing.unavailable', role: 'alert' };
    default:
      return null;
  }
}

export type PairDialogState =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'ready'; issued: IssuedPairingCode }
  | { kind: 'closed'; reason: PairingCodeClosedReason };

export type PairDialogView =
  | { kind: 'loading' }
  | { kind: 'notice'; key: MessageKey; role: 'status' | 'alert'; detailKey?: MessageKey }
  | { kind: 'code'; issued: IssuedPairingCode; secondsLeft: number };

/** What the kiosk's pairing dialog shows at `nowMs`; an expired code becomes a notice. */
export function pairDialogView(state: PairDialogState, nowMs: number): PairDialogView {
  switch (state.kind) {
    case 'loading':
      return state;
    case 'error':
      return { kind: 'notice', key: 'pairing.issueError', role: 'alert' };
    case 'closed':
      return state.reason === 'paired'
        ? { kind: 'notice', key: 'pairing.paired', role: 'status', detailKey: 'pairing.pairedDetail' }
        : { kind: 'notice', key: 'pairing.locked', role: 'alert' };
    case 'ready': {
      const remaining = secondsLeft(state.issued.expiresAtMs, nowMs);
      return remaining > 0
        ? { kind: 'code', issued: state.issued, secondsLeft: remaining }
        : { kind: 'notice', key: 'pairing.expired', role: 'alert' };
    }
  }
}
