import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { io, type Socket } from 'socket.io-client';
import { openMenuArea, waitForEvent } from './helpers';
import { phoneOrigin, routeApi } from './phoneFixture';

/**
 * The page inside the iOS companion: a fake `window.openflightNative` drives the weather source
 * (GPS location, native fetch, barometer pressure) and the forwarding of onsets, sensor snapshots
 * and clock probes. The page's socket is bridged to a real paired-phone socket on the mock
 * backend, so the server's validation and acks are exercised too. The kiosk never uses the bridge.
 */

const SOCKET_URL = process.env.E2E_SOCKET_URL ?? 'http://127.0.0.1:8080';
// A paired phone reaches the Pi over the hotspot; this marks the backend connection as not local.
const PHONE_ADDRESS = { 'X-Forwarded-For': '192.168.4.20' };
const GPS = { latitude: 48.13743, longitude: 11.57549 };
const BAROMETER_HPA = 957.4;
const OPEN_METEO_HPA = 960.2;

type Ack = { ok?: boolean; [key: string]: unknown };

/** A Socket.IO connection to the mock backend that the server treats as a paired phone. */
async function pairedPhoneSocket(request: APIRequestContext): Promise<Socket> {
  const { code } = await (await request.post(`${SOCKET_URL}/api/pairing/code`)).json();
  const exchange = await request.post(`${SOCKET_URL}/api/pairing/exchange`, {
    data: { code },
    headers: PHONE_ADDRESS,
  });
  expect(exchange.ok()).toBe(true);
  const cookie = /openflight_device=[^;]+/.exec(exchange.headers()['set-cookie'] ?? '')?.[0];
  expect(cookie).toBeTruthy();
  const socket = io(SOCKET_URL, { transports: ['websocket'], extraHeaders: { ...PHONE_ADDRESS, Cookie: cookie! } });
  await new Promise<void>((resolve, reject) => {
    socket.once('connect', () => resolve());
    socket.once('connect_error', reject);
  });
  return socket;
}

/** Defines `window.openflightNative` before any page script runs, as the companion's user script does. */
async function installFakeNative(page: Page) {
  await page.addInitScript(
    ({ gps, barometerHpa, openMeteoHpa }) => {
      const calls: Array<[string, unknown]> = [];
      const clock = () => ({ host_us: Math.round(performance.now() * 1000), epoch_ms: Date.now(), bracket_us: 2 });
      Object.defineProperty(window, '__nativeCalls', { value: calls });
      Object.defineProperty(window, 'openflightNative', {
        value: Object.freeze({
          version: 1,
          app_version: '0.1.0',
          platform: 'ios',
          capabilities: Object.freeze({
            barometer: true,
            absolute_altitude: false,
            location: true,
            weather: true,
            audio: true,
            keep_awake: true,
            debug_capture: true,
          }),
          getSensors: async (opts?: unknown) => {
            calls.push(['getSensors', opts ?? null]);
            return {
              type: 'sensors',
              epoch_ms: Date.now(),
              tz_offset_min: -new Date().getTimezoneOffset(),
              clock: clock(),
              barometer: { authorization: 'authorized', pressure_hpa: barometerHpa, age_s: 2 },
              absolute_altitude: null,
              location: { authorization: 'when_in_use', accuracy_authorization: 'full', ...gps, age_s: 3 },
              network: { wifi: true, cellular_available: true },
              audio: { state: 'running', capture_id: 'e2e-capture', last_seq: 0 },
              battery_pct: 80,
              battery_state: 'unplugged',
              keep_awake: true,
            };
          },
          fetchWeather: async (req: { latitude: number; longitude: number }) => {
            calls.push(['fetchWeather', req]);
            return {
              source: 'open-meteo',
              latitude: req.latitude,
              longitude: req.longitude,
              temperature_c: 11.5,
              pressure_hpa: openMeteoHpa,
              humidity_pct: 70,
              wind_speed_ms: 2.5,
              wind_direction_deg: 240,
              observed_epoch_s: Math.floor(Date.now() / 1000) - 300,
              epoch_ms: Date.now(),
              tz_offset_min: -new Date().getTimezoneOffset(),
              transport: 'cellular',
            };
          },
        }),
      });
    },
    { gps: GPS, barometerHpa: BAROMETER_HPA, openMeteoHpa: OPEN_METEO_HPA }
  );
}

/**
 * Swaps the page's socket for one relayed through `phone` (the page's own origin is not a Pi
 * origin here, so it cannot connect itself). Returns the acks the server gave the page's emits.
 */
async function bridgePageSocket(page: Page, phone: Socket): Promise<Array<[string, Ack]>> {
  const acks: Array<[string, Ack]> = [];
  await page.exposeFunction('__phoneEmit', async (event: string, args: unknown[]) => {
    const ack = (await phone
      .timeout(5000)
      .emitWithAck(event, ...args)
      .catch(() => null)) as Ack | null;
    if (ack) acks.push([event, ack]);
    return ack;
  });
  phone.onAny((event: string, data: unknown) => {
    void page
      .evaluate(
        ([name, payload]) =>
          (window as unknown as { __fakeSocket?: { receive: (e: string, d: unknown) => void } }).__fakeSocket?.receive(
            name as string,
            payload
          ),
        [event, data] as const
      )
      .catch(() => {});
  });
  await page.evaluate(async () => {
    const { socketService } = await import(/* @vite-ignore */ '/src/services/socketService.ts');
    socketService.disconnect();
    const handlers = new Map<string, (data?: unknown) => void>();
    const relay = (window as unknown as { __phoneEmit: (e: string, args: unknown[]) => Promise<unknown> }).__phoneEmit;
    // Events without a payload stay without one (the server's handlers take no argument).
    const args = (payload: unknown) => (payload === undefined ? [] : [payload]);
    const fake = {
      connected: true,
      active: true,
      on: (event: string, handler: (data?: unknown) => void) => void handlers.set(event, handler),
      emit: (event: string, payload?: unknown) => void relay(event, args(payload)),
      timeout: () => ({ emitWithAck: (event: string, payload?: unknown) => relay(event, args(payload)) }),
      close: () => {},
      connect: () => {},
      receive: (event: string, data: unknown) => handlers.get(event)?.(data),
    };
    Object.assign(window, { __fakeSocket: fake });
    socketService.connect(() => fake);
    fake.receive('connect');
  });
  return acks;
}

const nativeCalls = (page: Page) =>
  page.evaluate(() => (window as unknown as { __nativeCalls: Array<[string, unknown]> }).__nativeCalls);

test.describe('the page inside the iOS companion', () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true });

  test('fills the location from GPS, flies the barometer pressure and forwards onsets, sensors and probes', async ({
    page,
    request,
    baseURL,
  }) => {
    await installFakeNative(page);
    await routeApi(page, {});
    await page.addInitScript(() => localStorage.setItem('openflight.locale:v1', 'de'));
    await page.goto(`${phoneOrigin(baseURL!)}/`);
    await page.locator('.range-app').waitFor();

    const phone = await pairedPhoneSocket(request);
    try {
      const acks = await bridgePageSocket(page, phone);
      const firstState = waitForEvent<{ location: unknown }>(phone, 'weather_state');
      phone.emit('get_weather');
      // A fresh backend (one weather file per e2e run) has no range location yet.
      expect((await firstState).location).toBeNull();

      // GPS → range location → native fetch with the barometer pressure.
      const flown = await new Promise<{ reading: Record<string, unknown>; location: Record<string, unknown> }>(
        (resolve) => {
          const onState = (state: { reading?: Record<string, unknown> | null; location: Record<string, unknown> }) => {
            if (state.reading?.pressure_source !== 'phone-barometer') return;
            phone.off('weather_state', onState);
            resolve(state as never);
          };
          phone.on('weather_state', onState);
        }
      );
      expect(flown.location).toMatchObject(GPS);
      expect(flown.reading).toMatchObject({
        source: 'open-meteo',
        pressure_hpa: BAROMETER_HPA,
        transport: 'cellular',
        temperature_c: 11.5,
      });
      const calls = await nativeCalls(page);
      expect(calls).toContainEqual(['getSensors', { location_max_age_s: 600, timeout_ms: 15_000 }]);
      expect(calls).toContainEqual(['fetchWeather', GPS]);

      // The source is labelled in the menu.
      const close = page.locator('.picker-overlay__close');
      if (await close.isVisible().catch(() => false)) await close.click();
      await page.getByRole('button', { name: 'Menü öffnen' }).click();
      await openMenuArea(page, 'Kalibrieren');
      await expect(page.getByTestId('weather-status')).toContainText('Open-Meteo (Mobilfunk) · Barometer iPhone');

      // Sensor snapshots and clock probes reach the server and are accepted.
      await expect.poll(() => acks.some(([event, ack]) => event === 'phone_sensors' && ack.ok === true)).toBe(true);
      await expect
        .poll(() => acks.filter(([event, ack]) => event === 'phone_clock_probe' && ack.pi_epoch_s).length)
        .toBeGreaterThan(0);

      // A native onset is forwarded, mapped with the probe clock and logged (no sound edges in mock mode).
      await page.evaluate(() => {
        const now = Date.now();
        window.dispatchEvent(
          new CustomEvent('openflight-native', {
            detail: {
              type: 'audio_onset',
              v: 1,
              capture_id: 'e2e-capture',
              seq: 1,
              onset_host_us: Math.round(performance.now() * 1000) - 30_000,
              onset_epoch_ms: now - 30,
              clock: { host_us: Math.round(performance.now() * 1000), epoch_ms: now, bracket_us: 2 },
              epoch_ms: now,
              tz_offset_min: -new Date().getTimezoneOffset(),
              detection: { snr_db: 30, peak_dbfs: -6, clipped: false },
              features: { decay_ms_20db: 7.5 },
              fat_cue: { detected: false },
              detector_version: 'onset-v1',
            },
          })
        );
      });
      await expect
        .poll(() => acks.find(([event]) => event === 'phone_audio_onset')?.[1])
        .toMatchObject({ ok: true, edge_match: 'none', clock_source: 'probe' });
    } finally {
      phone.close();
    }
  });
});

test('the kiosk never uses the companion bridge', async ({ page }) => {
  await installFakeNative(page);
  await page.goto('/');
  await page.locator('.range-app').waitFor();
  await page.waitForTimeout(1500);
  expect(await nativeCalls(page)).toEqual([]);
});
