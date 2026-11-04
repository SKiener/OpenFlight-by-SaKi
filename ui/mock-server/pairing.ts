/**
 * Flask's device pairing for the companion E2E specs. Every test browser is on
 * loopback, so pairing stays off until POST /mock/pairing turns it on.
 */

import { randomInt, randomUUID } from 'node:crypto';
import type { Express } from 'express';
import type { Server } from 'socket.io';

const COOKIE_NAME = 'openflight_device';
const CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';
const CODE_TTL_S = 300;
const PLACEHOLDER_QR = `data:image/svg+xml,${encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1" fill="#fff"/></svg>'
)}`;

export function registerPairing(app: Express, io: Server): void {
  const state = { required: false, secret: randomUUID(), code: null as string | null };
  const isPaired = (cookieHeader: string | undefined) =>
    (cookieHeader ?? '').split(';').some((cookie) => cookie.trim() === `${COOKIE_NAME}=${state.secret}`);

  app.post('/mock/pairing', (req, res) => {
    state.required = req.body?.required === true;
    state.secret = randomUUID();
    state.code = null;
    res.json({ required: state.required });
  });

  app.get('/api/pairing/status', (req, res) => {
    const access = !state.required ? 'local' : isPaired(req.headers.cookie) ? 'paired' : 'unpaired';
    res.set('Cache-Control', 'no-store').json({ state: access });
  });

  app.post('/api/pairing/code', (req, res) => {
    state.code = Array.from({ length: 8 }, () => CODE_ALPHABET[randomInt(CODE_ALPHABET.length)]).join('');
    const origin = req.headers.origin ?? `http://${req.headers.host}`;
    res.json({
      code: state.code,
      expires_in_s: CODE_TTL_S,
      links: [{ host: new URL(origin).host, url: `${origin}/pair#code=${state.code}`, qr_svg: PLACEHOLDER_QR }],
    });
  });

  app.post('/api/pairing/exchange', (req, res) => {
    const code = String(req.body?.code ?? '')
      .replace(/[\s-]/g, '')
      .toUpperCase();
    if (!state.code || code !== state.code) {
      res.status(403).json({ error: 'invalid_code' });
      return;
    }
    state.code = null;
    io.emit('pairing_code_closed', { reason: 'paired' });
    res.cookie(COOKIE_NAME, state.secret, { httpOnly: true, sameSite: 'strict', path: '/' }).json({ state: 'paired' });
  });

  app.post('/api/pairing/revoke', (_req, res) => {
    state.secret = randomUUID();
    state.code = null;
    io.disconnectSockets();
    res.json({ status: 'revoked' });
  });

  io.use((socket, next) => {
    if (!state.required || isPaired(socket.handshake.headers.cookie)) next();
    else next(new Error('pairing_required'));
  });
}
