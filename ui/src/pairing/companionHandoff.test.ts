import { describe, expect, it } from 'vitest';
import { companionAppLink, exchangesInitialCode, offersCompanionApp } from './companionHandoff';

const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Mobile/15E148 Safari/604.1';
const IPHONE_CHROME =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/140.0 Mobile/15E148 Safari/604.1';
const COMPANION_APP =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) OpenFlightCompanion/0.1.0';
const IPAD_DESKTOP_MODE =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Safari/605.1.15';
const ANDROID_CHROME =
  'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36';
const DESKTOP_CHROME =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';
const KIOSK_CHROMIUM =
  'Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/140.0 Safari/537.36';

const BRIDGE = { version: 1, getSensors: () => {}, fetchWeather: () => {}, capabilities: {} };

describe('offersCompanionApp', () => {
  it('offers the app in iOS browsers', () => {
    expect(offersCompanionApp({ userAgent: IPHONE_SAFARI, platform: 'iPhone', maxTouchPoints: 5 })).toBe(true);
    expect(offersCompanionApp({ userAgent: IPHONE_CHROME, platform: 'iPhone', maxTouchPoints: 5 })).toBe(true);
    expect(offersCompanionApp({ userAgent: IPAD_DESKTOP_MODE, platform: 'MacIntel', maxTouchPoints: 5 })).toBe(true);
  });

  it('does not offer it inside the app', () => {
    expect(offersCompanionApp({ userAgent: COMPANION_APP, platform: 'iPhone', maxTouchPoints: 5 })).toBe(false);
    expect(
      offersCompanionApp({ userAgent: IPHONE_SAFARI, platform: 'iPhone', maxTouchPoints: 5, openflightNative: BRIDGE })
    ).toBe(false);
    expect(
      offersCompanionApp({
        userAgent: IPHONE_SAFARI,
        platform: 'iPhone',
        maxTouchPoints: 5,
        openflightNative: { version: 2 },
      })
    ).toBe(false);
  });

  it('does not offer it on Android, desktops, Macs or the kiosk', () => {
    expect(offersCompanionApp({ userAgent: ANDROID_CHROME, platform: 'Linux armv8l', maxTouchPoints: 5 })).toBe(false);
    expect(offersCompanionApp({ userAgent: DESKTOP_CHROME, platform: 'Win32', maxTouchPoints: 0 })).toBe(false);
    expect(offersCompanionApp({ userAgent: IPAD_DESKTOP_MODE, platform: 'MacIntel', maxTouchPoints: 0 })).toBe(false);
    expect(offersCompanionApp({ userAgent: KIOSK_CHROMIUM, platform: 'Linux aarch64', maxTouchPoints: 10 })).toBe(
      false
    );
    expect(offersCompanionApp({ userAgent: '' })).toBe(false);
  });
});

describe('companionAppLink', () => {
  it('passes the whole page URL, code fragment included, as one encoded parameter', () => {
    const page = 'http://openflight.local:8080/pair#code=AB2CD3EF';
    const link = companionAppLink(page);

    expect(link).toBe('openflight://open?url=http%3A%2F%2Fopenflight.local%3A8080%2Fpair%23code%3DAB2CD3EF');
    expect(link).not.toContain('#');
    expect(new URL(link).searchParams.get('url')).toBe(page);
  });

  it('keeps query strings and other characters inside the parameter', () => {
    const page = 'http://192.168.4.1:8080/pair?x=1&y=a b#code=AB2CD3EF';
    const link = companionAppLink(page);

    expect([...new URL(link).searchParams.keys()]).toEqual(['url']);
    expect(new URL(link).searchParams.get('url')).toBe(page);
  });
});

describe('exchangesInitialCode', () => {
  it('uses a QR code right away unless the app is offered', () => {
    expect(exchangesInitialCode('AB2CD3EF', null)).toBe(true);
    expect(exchangesInitialCode('AB2CD3EF', 'openflight://open?url=x')).toBe(false);
    expect(exchangesInitialCode(null, null)).toBe(false);
    expect(exchangesInitialCode(null, 'openflight://open?url=x')).toBe(false);
  });
});
