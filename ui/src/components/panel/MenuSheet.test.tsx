import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { DemoSection, MenuSheet } from './MenuSheet';
import { setActiveLocale } from '../../i18n';
import { useSystemStore } from '../../stores/useSystemStore';
import type { PowerStatus } from '../../types/power';
import type { MenuAreaId } from './menuAreas';

const ALL_CALLBACKS = {
  onOpenBag: () => {},
  onOpenHistory: () => {},
  onChangeView: () => {},
  onPairDevice: () => {},
  onRevokeDevices: () => {},
};

/** The start page, or with `area` that area's page (the app itself always opens on the start page). */
function renderMenu(area?: MenuAreaId, props: Partial<typeof ALL_CALLBACKS> = {}) {
  return renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} initialArea={area} {...props} />);
}

function renderDemo(overrides: Partial<Parameters<typeof DemoSection>[0]> = {}) {
  return renderToString(
    <DemoSection active={false} unavailable={false} connected onChange={() => {}} {...overrides} />
  );
}

/** The start page's area rows in order, as name and summary. */
function areaRows(html: string): Array<[string, string]> {
  return [
    ...html.matchAll(/menu-sheet__area-name">([^<]*)<\/span><span class="menu-sheet__area-summary">([^<]*)<\/span>/g),
  ].map(([, name, summary]) => [name, summary]);
}

/** Button labels (or aria-labels of empty buttons) in document order. */
function buttons(html: string): string[] {
  return [...html.matchAll(/<button([^>]*)>([\s\S]*?)<\/button>/g)].map(([, attributes, inner]) => {
    const text = inner
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
    return text || (/aria-label="([^"]*)"/.exec(attributes)?.[1] ?? '');
  });
}

describe('MenuSheet profiles', () => {
  it('does not manage profiles in the menu', () => {
    for (const html of [renderMenu(), renderMenu('display'), renderMenu('analysis', { onChangeView: () => {} })]) {
      expect(html).not.toContain('menu-sheet__section-title">Profile');
      expect(html).not.toContain('Add profile');
      expect(html).not.toContain('menu-sheet__input');
    }
  });
});

describe('MenuSheet theme', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('offers Sun (the default, selected) and Night under the theme section of Display', () => {
    const html = renderMenu('display');
    expect(html).toContain('menu-sheet__section-title">Theme');
    expect(html).toMatch(/aria-pressed="true"[^>]*>Sun<\/button>/);
    expect(html).toMatch(/aria-pressed="false"[^>]*>Night<\/button>/);
    expect(html).not.toMatch(/>Light<|>Dark</);
  });

  it.each([
    ['de', 'Darstellung', 'Sonne', 'Nacht'],
    ['es', 'Tema', 'Sol', 'Noche'],
  ] as const)('labels the themes in %s', (locale, title, sun, night) => {
    setActiveLocale(locale);
    const html = renderMenu('display');
    expect(html).toContain(`menu-sheet__section-title">${title}`);
    expect(html).toContain(`>${sun}</button>`);
    expect(html).toContain(`>${night}</button>`);
  });
});

describe('MenuSheet language', () => {
  it('offers English, German and Spanish as direct touch buttons instead of a native popup', () => {
    const html = renderMenu('display');

    expect(html).toContain('menu-sheet__section-title">Language');
    expect(html).toContain('aria-label="Language"');
    expect(html).not.toContain('<select');
    expect(html).toContain('aria-pressed="true">English</button>');
    for (const name of ['Deutsch', 'Español']) {
      expect(html).toContain(`aria-pressed="false">${name}</button>`);
    }
    expect(html).not.toContain('Français');
    expect(html).not.toContain('Português');
  });
});

describe('MenuSheet battery', () => {
  it('does not show battery in the menu, even when telemetry is present', () => {
    const powerStatus: PowerStatus = {
      available: true,
      provider: 'geekworm',
      state: 'on_battery',
      battery_percent: 64.2,
      battery_voltage_v: 3.81,
      external_power: false,
      updated_at: '2026-08-20T12:00:00+00:00',
      error: null,
    };
    useSystemStore.setState({ powerStatus });

    for (const html of [renderMenu(), renderMenu('system')]) {
      expect(html).not.toContain('menu-sheet__status-label">Battery');
      expect(html).not.toContain('power-status');
      expect(html).not.toContain('64%');
    }

    useSystemStore.setState({ powerStatus: null });
  });
});

describe('MenuSheet demo', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('offers the demo switch in the System area', () => {
    expect(renderMenu()).not.toContain('Demo mode');
    const menu = renderMenu('system');

    expect(menu).toContain('menu-sheet__section-title">Demo</span>');
    expect(menu).toMatch(/aria-pressed="false"[^>]*>Demo mode<\/button>/);
    expect(menu).toContain('Shows every feature with automatic demo shots. Real shots are ignored.');
    expect(renderDemo({ active: true })).toMatch(/aria-pressed="true">Demo mode<\/button>/);
    expect(renderDemo()).toMatch(/aria-pressed="false">Demo mode<\/button>/);

    setActiveLocale('de');
    expect(renderDemo()).toContain('>Demo-Modus</button>');
    expect(renderDemo()).toContain(
      'Zeigt alle Funktionen mit automatischen Demo-Schlägen. Echte Schläge werden ignoriert.'
    );
  });

  it('disables the switch outside ball mode', () => {
    const swingSpeed = renderDemo({ unavailable: true });

    expect(swingSpeed).toMatch(/aria-pressed="false" disabled="">Demo mode<\/button>/);
    expect(swingSpeed).toContain('Ball mode only');
    expect(swingSpeed).not.toContain('Shows every feature');
    expect(renderDemo()).not.toContain('disabled');

    setActiveLocale('de');
    expect(renderDemo({ unavailable: true })).toContain('Nur im Ballmodus');
  });

  it('waits for the connection, which would otherwise send the switch later', () => {
    expect(renderDemo({ connected: false })).toMatch(/disabled="">Demo mode<\/button>/);
  });
});

describe('MenuSheet device pairing', () => {
  const pairing = { onPairDevice: () => {}, onRevokeDevices: () => {} };

  it('offers pairing and signing out all devices only when the kiosk passes the actions', () => {
    expect(renderMenu()).not.toContain('Pair device');
    expect(renderMenu('system')).not.toContain('Sign out all devices');

    expect(renderMenu(undefined, pairing)).toContain('>Pair device</button>');
    const system = renderMenu('system', pairing);
    expect(system).toContain('menu-sheet__section-title">Devices');
    expect(system).toContain('>Sign out all devices</button>');
  });

  it('needs both pairing actions, as before', () => {
    expect(renderMenu(undefined, { onPairDevice: () => {} })).not.toContain('Pair device');
    expect(renderMenu('system', { onRevokeDevices: () => {} })).not.toContain('Sign out all devices');
  });

  it('puts pairing among the quick buttons before the areas, and signing out in System', () => {
    const html = renderMenu(undefined, ALL_CALLBACKS);

    expect(html.split('>Pair device</button>')).toHaveLength(2);
    expect(html.indexOf('>Pair device</button>')).toBeLessThan(html.indexOf('menu-sheet__areas'));
    expect(html).not.toContain('Sign out all devices');
    expect(renderMenu('system', ALL_CALLBACKS)).toMatch(/panel-action--danger">Sign out all devices</);
  });
});

describe('MenuSheet start page', () => {
  afterEach(() => {
    setActiveLocale('en');
    vi.unstubAllGlobals();
  });

  it('keeps the dialog and its label on both levels', () => {
    expect(renderMenu()).toMatch(/role="dialog" aria-modal="true" aria-label="Menu"/);
    expect(renderMenu('system')).toMatch(/role="dialog" aria-modal="true" aria-label="Menu"/);
  });

  it('lists Close, the quick buttons, six area rows, then Shut down at the very bottom', () => {
    const html = renderMenu(undefined, ALL_CALLBACKS);

    expect(buttons(html)).toEqual([
      'Close menu',
      'Close',
      'My clubs',
      'Session history',
      'Pair device',
      'Display mph · yds · Sun · English ›',
      'Live tiles Carry · Ball speed ›',
      'Callouts Off ›',
      'Calibrate ›',
      'Analysis ›',
      'System ›',
      'Shut down',
    ]);
    expect(html).toMatch(/class="menu-sheet__shutdown">Shut down<\/button><\/div>$/);
  });

  it('shows each quick button only with its callback', () => {
    const labels = buttons(renderMenu());
    for (const label of ['My clubs', 'Session history', 'Pair device']) expect(labels).not.toContain(label);
    expect(buttons(renderMenu(undefined, { onOpenBag: () => {} }))).toContain('My clubs');
    expect(buttons(renderMenu(undefined, { onOpenHistory: () => {} }))).toContain('Session history');
  });

  it('keeps every setting off the start page', () => {
    const html = renderMenu(undefined, ALL_CALLBACKS);

    for (const hidden of [
      'aria-label="Speed"',
      'aria-label="Theme"',
      'Live display',
      'Voice callouts',
      'Radar tilt',
      'Demo mode',
      '>Shots<',
      '>Debug<',
      'Sign out all devices',
    ]) {
      expect(html, hidden).not.toContain(hidden);
    }
  });

  it('sums up each area on its row', () => {
    expect(areaRows(renderMenu(undefined, ALL_CALLBACKS))).toEqual([
      ['Display', 'mph · yds · Sun · English'],
      ['Live tiles', 'Carry · Ball speed'],
      ['Callouts', 'Off'],
      ['Calibrate', ''],
      ['Analysis', ''],
      ['System', ''],
    ]);

    // Server rendering reads the stores' initial state, so the language stays the locale store's English here
    // (menuAreas.test.ts covers the summary for every language).
    setActiveLocale('de');
    expect(areaRows(renderMenu(undefined, ALL_CALLBACKS))).toEqual([
      ['Anzeige', 'mph · yds · Sonne · English'],
      ['Live-Kacheln', 'Flugweite · Balltempo'],
      ['Ansage', 'Aus'],
      ['Kalibrieren', ''],
      ['Auswertung', ''],
      ['System', ''],
    ]);
  });

  it('shows Live tiles on the kiosk only', () => {
    vi.stubGlobal('window', { location: { host: 'phone.local:5173' } });
    expect(areaRows(renderMenu(undefined, ALL_CALLBACKS)).map(([name]) => name)).toEqual([
      'Display',
      'Callouts',
      'Calibrate',
      'Analysis',
      'System',
    ]);
    // A page this screen does not have falls back to the start page.
    expect(renderMenu('liveTiles', ALL_CALLBACKS)).toContain('data-menu-level="start"');

    vi.stubGlobal('window', { location: { host: '127.0.0.1:8080' } });
    expect(areaRows(renderMenu(undefined, ALL_CALLBACKS)).map(([name]) => name)).toContain('Live tiles');
  });

  it('leaves Analysis out without the view callback, which is all it holds', () => {
    expect(areaRows(renderMenu()).map(([name]) => name)).toEqual([
      'Display',
      'Live tiles',
      'Callouts',
      'Calibrate',
      'System',
    ]);
  });

  it('starts on the start page: the app mounts a new sheet on every opening', () => {
    const html = renderMenu(undefined, ALL_CALLBACKS);
    expect(html).toContain('data-menu-level="start"');
    expect(html).not.toContain('menu-sheet__back');
  });
});

describe('MenuSheet area pages', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it.each([
    [
      'display',
      'Display',
      ['aria-label="Speed"', 'aria-label="Distance"', 'aria-label="Theme"', 'aria-label="Language"'],
    ],
    ['liveTiles', 'Live tiles', ['Live display', 'Large tile 1: Carry']],
    ['callouts', 'Callouts', ['Callouts (on the phone)']],
    [
      'calibrate',
      'Calibrate',
      ['Radar tilt', 'aria-label="Driver tee height"', 'aria-label="Ball type"', 'aria-label="Weather"'],
    ],
    ['analysis', 'Analysis', ['>Shots<', '>Stats<', '>Profiles<']],
    ['system', 'System', ['>Debug<', 'Demo mode', 'Sign out all devices']],
  ] as const)('%s: Back and the title over its sections, in place of the start page', (area, title, content) => {
    const html = renderMenu(area, ALL_CALLBACKS);

    expect(html).toContain('data-menu-level="area"');
    expect(html).toContain('class="menu-sheet__back">‹ Back</button>');
    expect(html).toContain(`id="menu-sheet-area-title">${title}</h2>`);
    for (const part of content) expect(html, part).toContain(part);
    expect(html).not.toContain('menu-sheet__areas');
    expect(html).not.toContain('menu-sheet__shutdown');
    expect(html).not.toContain('>Close</button>');
  });

  it('puts Back before the title, and the radar tilt before the weather', () => {
    const html = renderMenu('calibrate');
    expect(html.indexOf('menu-sheet__back')).toBeLessThan(html.indexOf('menu-sheet__area-title'));
    expect(html.indexOf('Radar tilt')).toBeLessThan(html.indexOf('aria-label="Weather"'));
  });

  it('puts the driver tee height between the radar tilt and the weather', () => {
    const html = renderMenu('calibrate');
    const tee = html.indexOf('aria-label="Driver tee height"');
    expect(html.indexOf('Radar tilt')).toBeLessThan(tee);
    expect(tee).toBeLessThan(html.indexOf('aria-label="Weather"'));
  });

  it('puts the ball type right after the driver tee height, before the weather', () => {
    const html = renderMenu('calibrate');
    const tee = html.indexOf('aria-label="Driver tee height"');
    const ball = html.indexOf('aria-label="Ball type"');
    expect(tee).toBeLessThan(ball);
    expect(ball).toBeLessThan(html.indexOf('aria-label="Weather"'));
    // Nothing between the tee section's end and the ball type section.
    expect(html.slice(tee, ball).match(/<section|<fieldset/g)).toHaveLength(1);
  });

  it.each([
    ['de', '‹ Zurück', 'Kalibrieren'],
    ['es', '‹ Atrás', 'Calibrar'],
  ] as const)('labels Back and the title in %s', (locale, back, title) => {
    setActiveLocale(locale);
    const html = renderMenu('calibrate');
    expect(html).toContain(`>${back}</button>`);
    expect(html).toContain(`>${title}</h2>`);
  });

  it('keeps Debug in System and the views in Analysis', () => {
    expect(renderMenu('analysis', ALL_CALLBACKS)).not.toContain('>Debug<');
    expect(renderMenu('system', ALL_CALLBACKS)).not.toContain('>Shots<');
  });
});
