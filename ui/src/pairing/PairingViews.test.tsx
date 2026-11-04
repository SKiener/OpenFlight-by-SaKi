import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { PairingScreen } from './PairingScreen';
import { PairDeviceView, type PairQrMode } from './PairDeviceDialog';
import { RevokeDevicesView } from './RevokeDevicesDialog';
import type { IssuedPairingCode } from './pairingApi';
import type { PairDialogState } from './pairingView';

const issued: IssuedPairingCode = {
  code: 'AB2CD3EF',
  expiresAtMs: 300_000,
  links: [
    {
      host: 'openflight.local',
      url: 'http://openflight.local:8080/pair#code=AB2CD3EF',
      qrSvg: 'data:image/svg+xml,qr1',
    },
    { host: '192.168.1.23', url: 'http://192.168.1.23:8080/pair#code=AB2CD3EF', qrSvg: 'data:image/svg+xml,qr2' },
  ],
  wifi: null,
};

const noop = () => {};

function renderDialog(state: PairDialogState, hostIndex = 0, nowMs = 1_000, qrMode: PairQrMode = 'pair') {
  return renderToString(
    <PairDeviceView
      state={state}
      nowMs={nowMs}
      hostIndex={hostIndex}
      qrMode={qrMode}
      onSelectHost={noop}
      onSelectQrMode={noop}
      onNewCode={noop}
      onClose={noop}
    />
  );
}

afterEach(() => {
  setActiveLocale('en');
});

describe('PairingScreen', () => {
  it('asks for the code with a keyboard suited to one-time codes', () => {
    const html = renderToString(<PairingScreen onPaired={noop} />);

    expect(html).toContain('Pair this device');
    expect(html).toContain('autoComplete="one-time-code"');
    expect(html).toContain('autoCapitalize="characters"');
    expect(html).toContain('spellCheck="false"');
    expect(html).toMatch(/<button type="submit" class="pairing-screen__submit" disabled="">Pair<\/button>/);
  });

  it('exchanges a code from a QR link without typing', () => {
    const html = renderToString(<PairingScreen initialCode="AB2CD3EF" onPaired={noop} />);

    expect(html).toContain('value="AB2CD3EF"');
    expect(html).toContain('Pairing…');
    expect(html).toMatch(/<input[^>]*disabled=""/);
  });

  it('in an iOS browser, offers the app first and pairs here only on a tap', () => {
    const appLink = 'openflight://open?url=http%3A%2F%2Fopenflight.local%3A8080%2Fpair%23code%3DAB2CD3EF';
    const html = renderToString(<PairingScreen initialCode="AB2CD3EF" appLink={appLink} onPaired={noop} />);

    expect(html).toMatch(/<button type="button" class="pairing-screen__submit">Open in the OpenFlight app<\/button>/);
    expect(html).toContain('For microphone and tilt measurement');
    // No automatic exchange: the code waits, pre-filled, behind an enabled secondary button.
    expect(html).not.toContain('Pairing…');
    expect(html).toContain('value="AB2CD3EF"');
    expect(html).not.toMatch(/<input[^>]*disabled=""/);
    expect(html).toMatch(
      /<button type="submit" class="pairing-screen__submit pairing-screen__submit--secondary">Pair<\/button>/
    );
    expect(html.indexOf('Open in the OpenFlight app')).toBeLessThan(html.indexOf('>Pair<'));
  });

  it('shows no app button elsewhere', () => {
    expect(renderToString(<PairingScreen initialCode="AB2CD3EF" onPaired={noop} />)).not.toContain('OpenFlight app');
    expect(renderToString(<PairingScreen onPaired={noop} />)).not.toContain('OpenFlight app');
  });

  it('offers the app in German and Spanish', () => {
    setActiveLocale('de');
    let html = renderToString(
      <PairingScreen initialCode="AB2CD3EF" appLink="openflight://open?url=x" onPaired={noop} />
    );
    expect(html).toContain('In der OpenFlight-App öffnen');
    expect(html).toContain('Für Mikrofon und Neigungsmessung');

    setActiveLocale('es');
    html = renderToString(<PairingScreen initialCode="AB2CD3EF" appLink="openflight://open?url=x" onPaired={noop} />);
    expect(html).toContain('Abrir en la app OpenFlight');
    expect(html).toContain('Para micrófono y medición de inclinación');
  });

  it('speaks German', () => {
    setActiveLocale('de');

    const html = renderToString(<PairingScreen onPaired={noop} />);

    expect(html).toContain('Gerät koppeln');
    expect(html).toContain('Kopplungscode');
  });
});

describe('PairDeviceView', () => {
  it('shows a large QR code, the typed code, the time left, and where to type it', () => {
    const html = renderDialog({ kind: 'ready', issued });

    expect(html).toContain('src="data:image/svg+xml,qr1"');
    expect(html).toContain('alt="QR code to pair via openflight.local"');
    expect(html).toContain('AB2C-D3EF');
    expect(html).toContain('Valid for 4:59');
    expect(html).toContain('openflight.local:8080/pair and enter the code');
    expect(html).not.toContain('#code=');
    expect(html).toContain('aria-pressed="true">openflight.local</button>');
    expect(html).toContain('aria-pressed="false">192.168.1.23</button>');
  });

  it('switches the QR code to the LAN address', () => {
    const html = renderDialog({ kind: 'ready', issued }, 1);

    expect(html).toContain('src="data:image/svg+xml,qr2"');
    expect(html).toContain('192.168.1.23:8080/pair and enter the code');
  });

  it('shows the launch monitor Wi-Fi name and password next to the QR code', () => {
    const html = renderDialog({
      kind: 'ready',
      issued: { ...issued, wifi: { ssid: 'TestNet', password: 'test-pass-123', qrSvg: null } },
    });

    expect(html).toContain('class="pair-dialog__wifi"');
    expect(html).toContain('Launch monitor Wi-Fi');
    expect(html).toContain('<dt>Network</dt><dd>TestNet</dd>');
    expect(html).toContain('<dt>Password</dt><dd class="pair-dialog__wifi-password">test-pass-123</dd>');
    expect(html.indexOf('pair-dialog__wifi')).toBeGreaterThan(html.indexOf('enter the code'));
    expect(html.indexOf('pair-dialog__wifi')).toBeLessThan(html.indexOf('aria-pressed'));
  });

  it('shows only the password when the Wi-Fi name is unknown, in German too', () => {
    setActiveLocale('de');

    const html = renderDialog({
      kind: 'ready',
      issued: { ...issued, wifi: { ssid: null, password: 'test-pass-123', qrSvg: null } },
    });

    expect(html).toContain('WLAN des Launch Monitors');
    expect(html).toContain('<dt>Passwort</dt>');
    expect(html).not.toContain('<dt>WLAN</dt>');
  });

  it('offers no QR code toggle without a Wi-Fi join code', () => {
    for (const wifi of [null, { ssid: 'TestNet', password: 'test-pass-123', qrSvg: null }]) {
      const html = renderDialog({ kind: 'ready', issued: { ...issued, wifi } }, 0, 1_000, 'wifi');

      expect(html).not.toContain('aria-label="QR code"');
      expect(html).not.toContain('>Wi-Fi</button>');
      expect(html).toContain('src="data:image/svg+xml,qr1"');
      expect(html).toContain('openflight.local:8080/pair and enter the code');
    }
  });

  describe('with a Wi-Fi join code', () => {
    const withWifiQr: PairDialogState = {
      kind: 'ready',
      issued: { ...issued, wifi: { ssid: 'TestNet', password: 'test-pass-123', qrSvg: 'data:image/svg+xml,wifi' } },
    };

    function expectCodeAndWifiDetails(html: string) {
      expect(html).toContain('AB2C-D3EF');
      expect(html).toContain('Valid for 4:59');
      expect(html).toContain('<dt>Network</dt><dd>TestNet</dd>');
      expect(html).toContain('<dd class="pair-dialog__wifi-password">test-pass-123</dd>');
    }

    it('shows the pairing QR code with a toggle underneath by default', () => {
      const html = renderDialog(withWifiQr);

      expect(html).toContain('src="data:image/svg+xml,qr1"');
      expect(html).not.toContain('data:image/svg+xml,wifi');
      expect(html).toContain('role="group" aria-label="QR code"');
      expect(html).toContain('aria-pressed="true">Pair</button>');
      expect(html).toContain('aria-pressed="false">Wi-Fi</button>');
      expect(html.indexOf('aria-label="QR code"')).toBeGreaterThan(html.indexOf('class="pair-dialog__qr"'));
      expect(html.indexOf('aria-label="QR code"')).toBeLessThan(html.indexOf('pair-dialog__details'));
      expect(html).toContain('openflight.local:8080/pair and enter the code');
      expect(html).toContain('aria-label="Address"');
      expect(html).toContain('aria-pressed="true">openflight.local</button>');
      expectCodeAndWifiDetails(html);
    });

    it('switches the QR code to joining the Wi-Fi and hides the address choice', () => {
      const html = renderDialog(withWifiQr, 1, 1_000, 'wifi');

      expect(html).toContain('src="data:image/svg+xml,wifi"');
      expect(html).toContain('alt="QR code to join the TestNet Wi-Fi"');
      expect(html).not.toContain('data:image/svg+xml,qr2');
      expect(html).toContain('aria-pressed="false">Pair</button>');
      expect(html).toContain('aria-pressed="true">Wi-Fi</button>');
      expect(html).toContain('Scan with the phone camera to join the &quot;TestNet&quot; Wi-Fi.');
      expect(html).not.toContain('enter the code');
      expect(html).not.toContain('aria-label="Address"');
      expect(html).not.toContain('192.168.1.23</button>');
      expectCodeAndWifiDetails(html);
    });

    it('speaks German', () => {
      setActiveLocale('de');

      const html = renderDialog(withWifiQr, 0, 1_000, 'wifi');

      expect(html).toContain('role="group" aria-label="QR-Code"');
      expect(html).toContain('>Koppeln</button>');
      expect(html).toContain('>WLAN</button>');
      expect(html).toContain('alt="QR-Code zum Beitreten des WLANs TestNet"');
      expect(html).toContain('Mit der Handykamera scannen, um dem WLAN „TestNet“ beizutreten.');
    });
  });

  it('leaves out the Wi-Fi block when no hotspot password is configured', () => {
    const html = renderDialog({ kind: 'ready', issued });

    expect(html).not.toContain('pair-dialog__wifi');
    expect(html).not.toContain('Launch monitor Wi-Fi');
  });

  it('offers a new code once the code expired, was used, or was locked', () => {
    for (const [state, text] of [
      [{ kind: 'ready', issued }, 'This code has expired.'],
      [{ kind: 'closed', reason: 'paired' }, 'Device paired'],
      [{ kind: 'closed', reason: 'locked' }, 'This code no longer works.'],
      [{ kind: 'error' }, 'Could not create a pairing code.'],
    ] as const) {
      const html = renderDialog(state, 0, 400_000);
      expect(html).toContain(text);
      expect(html).not.toContain('<img');
      expect(html).toContain('New code');
    }
  });

  it('shows progress while the code is created', () => {
    const html = renderDialog({ kind: 'loading' });

    expect(html).toContain('Creating a code…');
    expect(html).not.toContain('New code');
  });
});

describe('RevokeDevicesView', () => {
  it('asks before signing out every device and keeps this display signed in', () => {
    const html = renderToString(<RevokeDevicesView state="confirm" onConfirm={noop} onClose={noop} />);

    expect(html).toContain('Sign out all devices?');
    expect(html).toContain('This display stays signed in.');
    expect(html).toContain('Sign out all');
    expect(html).toContain('Cancel');
  });

  it('disables the actions while signing out and reports the outcome', () => {
    expect(renderToString(<RevokeDevicesView state="pending" onConfirm={noop} onClose={noop} />)).toMatch(
      /disabled="">Sign out all<\/button>/
    );
    expect(renderToString(<RevokeDevicesView state="error" onConfirm={noop} onClose={noop} />)).toContain(
      'Could not sign out the devices.'
    );
    const done = renderToString(<RevokeDevicesView state="done" onConfirm={noop} onClose={noop} />);
    expect(done).toContain('All devices are signed out.');
    expect(done).not.toContain('Sign out all</button>');
  });

  it('uses the German owner wording', () => {
    setActiveLocale('de');

    const html = renderToString(<RevokeDevicesView state="confirm" onConfirm={noop} onClose={noop} />);

    expect(html).toContain('Alle Geräte abmelden?');
    expect(html).toContain('Alle abmelden');
  });
});
