import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../i18n';
import { PairPage } from './PairingGate';

const QR_URL = 'http://openflight.local:8080/pair#code=AB2CD3EF';
const IPHONE_SAFARI =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/27.0 Mobile/15E148 Safari/604.1';
const ANDROID_CHROME =
  'Mozilla/5.0 (Linux; Android 16; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Mobile Safari/537.36';
const DESKTOP_CHROME =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36';

function renderPairPage(navigatorStub: Partial<Navigator>, openflightNative?: unknown) {
  const url = new URL(QR_URL);
  vi.stubGlobal('navigator', { languages: ['en'], language: 'en', ...navigatorStub });
  vi.stubGlobal('window', {
    location: { href: url.href, hash: url.hash, pathname: url.pathname },
    openflightNative,
  });
  return renderToString(<PairPage />);
}

afterEach(() => {
  vi.unstubAllGlobals();
  setActiveLocale('en');
});

describe('PairPage', () => {
  it('in iOS Safari, offers the app and does not use the code', () => {
    const html = renderPairPage({ userAgent: IPHONE_SAFARI, platform: 'iPhone', maxTouchPoints: 5 });

    expect(html).toContain('Open in the OpenFlight app');
    expect(html).not.toContain('Pairing…');
    expect(html).toMatch(/pairing-screen__submit--secondary">Pair</);
  });

  it.each([
    ['Android', { userAgent: ANDROID_CHROME, platform: 'Linux armv8l', maxTouchPoints: 5 }, undefined],
    ['a desktop', { userAgent: DESKTOP_CHROME, platform: 'Win32', maxTouchPoints: 0 }, undefined],
    [
      'the companion app',
      { userAgent: `${IPHONE_SAFARI} OpenFlightCompanion/0.1.0`, platform: 'iPhone', maxTouchPoints: 5 },
      { version: 1 },
    ],
  ])('on %s, uses the code right away as before', (_name, navigatorStub, bridge) => {
    const html = renderPairPage(navigatorStub, bridge);

    expect(html).not.toContain('OpenFlight app');
    expect(html).toContain('Pairing…');
    expect(html).toContain('value="AB2CD3EF"');
  });
});
