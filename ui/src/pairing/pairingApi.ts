export type PairingAccess = 'local' | 'paired' | 'unpaired';

export type PairingCodeClosedReason = 'paired' | 'locked';

export type ExchangeResult =
  { kind: 'paired' } | { kind: 'invalid' } | { kind: 'rate_limited'; retryAfterS: number } | { kind: 'unavailable' };

export interface PairingLink {
  host: string;
  url: string;
  qrSvg: string;
}

export interface PairingWifi {
  ssid: string | null;
  password: string;
  qrSvg: string | null;
}

export interface IssuedPairingCode {
  code: string;
  expiresAtMs: number;
  links: PairingLink[];
  wifi: PairingWifi | null;
}

export const PAIRING_REQUIRED_MESSAGE = 'pairing_required';
export const PAIRING_CODE_LENGTH = 8;
const DEFAULT_RETRY_AFTER_S = 60;

// Same-origin paths: the Vite dev proxy then keeps the device cookie on the page's host.
const STATUS_URL = '/api/pairing/status';
const EXCHANGE_URL = '/api/pairing/exchange';
const CODE_URL = '/api/pairing/code';
const REVOKE_URL = '/api/pairing/revoke';

export function normalizePairingCode(input: string): string {
  return input.replace(/[\s-]/g, '').toUpperCase();
}

export function formatPairingCode(code: string): string {
  return code.length === PAIRING_CODE_LENGTH ? `${code.slice(0, 4)}-${code.slice(4)}` : code;
}

export function isCompletePairingCode(input: string): boolean {
  return normalizePairingCode(input).length === PAIRING_CODE_LENGTH;
}

export function codeFromHash(hash: string): string | null {
  const code = new URLSearchParams(hash.replace(/^#/, '')).get('code');
  return code ? normalizePairingCode(code) : null;
}

export function secondsLeft(expiresAtMs: number, nowMs: number): number {
  return Math.max(0, Math.ceil((expiresAtMs - nowMs) / 1000));
}

export function formatCountdown(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

/** `http://openflight.local:8080/pair#code=…` → `openflight.local:8080/pair` for typing by hand. */
export function typedPairingAddress(url: string): string {
  return url.replace(/^https?:\/\//, '').replace(/#.*$/, '');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object';
}

export async function fetchPairingAccess(): Promise<PairingAccess | null> {
  try {
    const response = await fetch(STATUS_URL, { cache: 'no-store' });
    if (!response.ok) return null;
    const body: unknown = await response.json();
    const state = isRecord(body) ? body.state : null;
    return state === 'local' || state === 'paired' || state === 'unpaired' ? state : null;
  } catch {
    return null;
  }
}

export async function exchangePairingCode(code: string): Promise<ExchangeResult> {
  let response: Response;
  try {
    response = await fetch(EXCHANGE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: normalizePairingCode(code) }),
    });
  } catch {
    return { kind: 'unavailable' };
  }
  if (response.ok) return { kind: 'paired' };
  const body: unknown = await response.json().catch(() => null);
  const error = isRecord(body) ? body.error : null;
  if (response.status === 429) {
    const retryAfterS = Number(isRecord(body) ? body.retry_after_s : NaN);
    return {
      kind: 'rate_limited',
      retryAfterS: Number.isFinite(retryAfterS) && retryAfterS > 0 ? Math.ceil(retryAfterS) : DEFAULT_RETRY_AFTER_S,
    };
  }
  return error === 'invalid_code' ? { kind: 'invalid' } : { kind: 'unavailable' };
}

function parseLink(value: unknown): PairingLink | null {
  if (!isRecord(value)) return null;
  const { host, url, qr_svg: qrSvg } = value;
  if (typeof host !== 'string' || typeof url !== 'string' || typeof qrSvg !== 'string') return null;
  if (!qrSvg.startsWith('data:image/svg+xml')) return null;
  return { host, url, qrSvg };
}

function parseWifi(value: unknown): PairingWifi | null {
  if (!isRecord(value)) return null;
  const { ssid, password, qr_svg: qrSvg } = value;
  if (typeof password !== 'string' || password === '') return null;
  return {
    ssid: typeof ssid === 'string' && ssid !== '' ? ssid : null,
    password,
    qrSvg: typeof qrSvg === 'string' && qrSvg.startsWith('data:image/svg+xml') ? qrSvg : null,
  };
}

export async function issuePairingCode(nowMs: number): Promise<IssuedPairingCode> {
  const response = await fetch(CODE_URL, { method: 'POST', cache: 'no-store' });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || !isRecord(body)) throw new Error(`Pairing code request failed (${response.status})`);
  const { code, expires_in_s: expiresInS, links, wifi } = body;
  if (typeof code !== 'string' || typeof expiresInS !== 'number' || !Array.isArray(links)) {
    throw new Error('Pairing code response was incomplete');
  }
  return {
    code,
    expiresAtMs: nowMs + expiresInS * 1000,
    links: links.map(parseLink).filter((link): link is PairingLink => link !== null),
    wifi: parseWifi(wifi),
  };
}

export async function revokeAllDevices(): Promise<void> {
  const response = await fetch(REVOKE_URL, { method: 'POST' });
  if (!response.ok) throw new Error(`Revoking devices failed (${response.status})`);
}
