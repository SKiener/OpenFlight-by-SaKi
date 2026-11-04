import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import { MenuSheet } from './components/panel/MenuSheet';
import type { PanelView } from './components/panel/views';
import { DemoBand } from './components/DemoBand';
import { RangeFooter, RangeHeader } from './components/range/RangeChrome';
import { LAST_AREA_KEY } from './state/lastArea';
import { useSystemStore } from './stores/useSystemStore';
import { fakeStorage } from './test-utils/fakeBrowser';

const footer = (
  currentView: PanelView,
  open: { menuOpen?: boolean; historyOpen?: boolean; showRange?: boolean } = {}
): string =>
  renderToString(
    <RangeFooter
      currentView={currentView}
      onChangeView={() => {}}
      onOpenMenu={() => {}}
      menuOpen={open.menuOpen ?? false}
      onOpenHistory={() => {}}
      historyOpen={open.historyOpen ?? false}
      onReplay={() => {}}
      hasReplay={false}
      showRange={open.showRange ?? true}
    />
  );

const KIOSK = 'http://127.0.0.1:8080/';
const PHONE = 'http://openflight.local:8080/';

/** The page the app renders on: its address and what its localStorage holds. */
const openPage = (url: string, stored: Record<string, string> = {}): void => {
  const { localStorage } = fakeStorage(stored);
  vi.stubGlobal('window', { localStorage, location: new URL(url) });
};

const tabs = (html: string): Array<[label: string, pressed: string]> =>
  [...html.matchAll(/<button aria-pressed="(\w+)"[^>]*><svg[\s\S]*?<\/svg>([^<]+)<\/button>/g)].map(
    ([, pressed, label]) => [label, pressed]
  );

const pressedTabs = (html: string): string =>
  tabs(html)
    .filter(([, pressed]) => pressed === 'true')
    .map(([label]) => label)
    .join();

describe('App shell', () => {
  beforeEach(() => {
    useSystemStore.setState({ serverClub: null });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders the Range header and bottom navigation with a theme control', () => {
    const html = renderToString(<App />);

    expect(html).toContain('range-footer');
    expect(html).toContain('range-header');
    // Sun is the default, so the control offers the night look.
    expect(html).toContain('Switch to night mode');
    expect(html).toContain('aria-label="Open menu"');
    // The 6a chrome replaced the header entirely; nothing from it should survive.
    expect(html).not.toContain('class="header"');
    expect(html).not.toContain('header__secret-tap');
    expect(html).not.toContain('unit-toggle');
  });

  it('keeps five primary actions and extra panels reachable in the device menu', () => {
    openPage(KIOSK);
    const html = renderToString(<App />);

    for (const label of ['Live', 'Range', 'History', 'Align', 'Device']) expect(html).toContain(`>${label}<`);
    expect(tabs(html).map(([label]) => label)).toEqual(['Live', 'Range', 'History', 'Align']);
    // Two levels: Shots, Stats and Profiles sit in the Analysis area, Debug in System.
    const menu = (initialArea?: 'analysis' | 'system') =>
      renderToString(
        <MenuSheet onClose={() => {}} onShutdown={() => {}} onChangeView={() => {}} initialArea={initialArea} />
      );
    expect(menu()).toContain('>Analysis<');
    expect(menu()).toContain('>System<');
    for (const label of ['Shots', 'Stats', 'Profiles']) expect(menu('analysis')).toContain(`>${label}<`);
    expect(menu('system')).toContain('>Debug<');
  });

  it('marks the Live tab pressed and shows the Live panel', () => {
    const html = renderToString(<App />);
    const liveButton = html.match(/<button aria-pressed="true"[^>]*><svg[\s\S]*?<\/svg>Live<\/button>/)?.[0];

    expect(liveButton).toBeDefined();
    expect(liveButton).toContain('aria-pressed="true"');
    expect(html).toContain('range-live');
  });

  it('draws the Range button with a flag', () => {
    expect(footer('live')).toContain('<path d="M5 21V4m0 0h11l-2 4 2 4H5"></path></svg>Range</button>');
  });

  it('presses Range only while the range itself is showing', () => {
    expect(pressedTabs(footer('virtual-range'))).toBe('Range');
    expect(pressedTabs(footer('live'))).toBe('Live');
    // The device menu and the history sit over the view, so the view's button is not the pressed one.
    expect(pressedTabs(footer('virtual-range', { menuOpen: true }))).toBe('');
    expect(pressedTabs(footer('virtual-range', { historyOpen: true }))).toBe('History');
  });

  it('shows the range instead of the live tiles when it was the last area', () => {
    openPage(KIOSK, { [LAST_AREA_KEY]: 'virtual-range' });
    const html = renderToString(<App />);

    expect(html).toContain('class="virtual-range"');
    expect(html).not.toContain('range-live');
    expect(tabs(html)[1]).toEqual(['Range', 'true']);
  });

  it('opens the range for ?rangeDemo=1 with nothing stored, and nothing covers it', () => {
    openPage(`${KIOSK}?rangeDemo=1`);
    const html = renderToString(<App />);

    expect(html).toContain('class="virtual-range"');
    expect(tabs(html)[1]).toEqual(['Range', 'true']);
    expect(html).not.toContain('picker-overlay');
  });

  it('gives a phone or another browser no Range button', () => {
    openPage(PHONE);
    const html = renderToString(<App />);

    expect(tabs(html).map(([label]) => label)).toEqual(['Live', 'History', 'Align']);
    expect(html).not.toContain('>Range<');
    expect(footer('live', { showRange: false })).not.toContain('Range');
    expect(tabs(footer('live', { showRange: false })).map(([label]) => label)).toEqual(['Live', 'History', 'Align']);
  });

  it('opens Live off the kiosk even when the range was the last area', () => {
    openPage(PHONE, { [LAST_AREA_KEY]: 'virtual-range' });
    const html = renderToString(<App />);

    expect(html).toContain('range-live');
    expect(html).not.toContain('class="virtual-range"');
    expect(pressedTabs(html)).toBe('Live');
  });

  it('keeps ?rangeDemo=1 a measurement switch that opens the range on any host', () => {
    openPage(`${PHONE}?rangeDemo=1`);
    const html = renderToString(<App />);

    expect(html).toContain('class="virtual-range"');
    expect(html).not.toContain('>Range<');
  });

  it('opens on the club picker so the club is confirmed before the first shot', () => {
    const html = renderToString(<App />);

    expect(html).toContain('aria-label="Select club"');
    // Driver is the default and is pre-selected.
    expect(html).toMatch(/picker-overlay__option--selected[^>]*aria-pressed="true"[^>]*><span class="picker-overlay__label">DR</);
    // ...and it is dismissible, so skipping keeps the default.
    expect(html).toContain('aria-label="Close Select club"');
  });

  it('offers the club change action in the Live header', () => {
    const html = renderToString(<App />);

    expect(html).toContain('Change club');
    expect(html).not.toContain('panel-footer__action');
    expect(html).not.toContain('panel-action__value');
    expect(html).toMatch(/aria-label="Change club"[^>]*>Driver/);
  });

  it('keeps units and language accessible in the device menu', () => {
    // Both live in the Display area; its start-page row sums them up.
    expect(renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} />)).toContain(
      'menu-sheet__area-summary">mph · yds · Sun · English<'
    );
    const html = renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} initialArea="display" />);

    expect(html).toContain('aria-label="Speed"');
    expect(html).toContain('aria-label="Distance"');
    for (const label of ['mph', 'km/h', 'Meters', 'Yards']) expect(html).toContain(`>${label}<`);
    expect(html).toContain('>Deutsch</button>');
    expect(html).not.toContain('Shot 00');
    expect(html).not.toContain('panel-footer__count');
  });

  it('keeps shutdown behind the device menu and replay disabled without a capture', () => {
    const html = renderToString(<App />);

    expect(html).toContain('aria-label="Open menu"');
    expect(html).toMatch(/class="range-replay-button" disabled/);
    const menu = renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} />);
    expect(menu).toContain('menu-sheet__shutdown');
    expect(menu).toContain('Shut down');
  });

  it('does not ask to clear a session until the stats action is used', () => {
    const html = renderToString(<App />);

    expect(html).not.toContain("Clear Profile 1's session?");
    expect(html).not.toContain('Clear Profile 1&#x27;s session?');
    expect(html).not.toContain('clear-session-title');
  });
});

describe('the full-screen range', () => {
  const appClasses = (html: string): string[] =>
    html.match(/<div class="(panel-app range-app[^"]*)"/)?.[1].split(/\s+/) ?? [];

  /** The top chrome's markup, up to its own closing tag. */
  function topChrome(html: string): string {
    const start = html.indexOf('<div class="range-app__top"');
    const tags = /<div\b|<\/div>/g;
    tags.lastIndex = Math.max(start, 0);
    let depth = 0;
    for (let tag = tags.exec(html); start >= 0 && tag; tag = tags.exec(html)) {
      depth += tag[0] === '</div>' ? -1 : 1;
      if (depth === 0) return html.slice(start, tags.lastIndex);
    }
    return '';
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('lays the chrome over the range, and over no other view', () => {
    openPage(KIOSK, { [LAST_AREA_KEY]: 'virtual-range' });
    expect(appClasses(renderToString(<App />))).toContain('range-app--full');

    openPage(`${PHONE}?rangeDemo=1`);
    expect(appClasses(renderToString(<App />))).toContain('range-app--full');

    openPage(KIOSK);
    expect(appClasses(renderToString(<App />))).not.toContain('range-app--full');
  });

  it('marks the kiosk, and only the kiosk, for its big Live screen', () => {
    openPage(KIOSK);
    const kiosk = renderToString(<App />);
    expect(appClasses(kiosk)).toContain('range-app--kiosk');
    expect(kiosk).toContain('kiosk-live');
    // The kiosk header drops the brand, the theme toggle and the status word.
    expect(kiosk).not.toContain('range-brand');
    expect(kiosk).not.toContain('range-theme');

    openPage(PHONE);
    const phone = renderToString(<App />);
    expect(appClasses(phone)).not.toContain('range-app--kiosk');
    expect(phone).not.toContain('kiosk-live');
    expect(phone).toContain('range-brand');
  });

  it('holds the header, then what the app puts under it, such as the demo band, in one top chrome', () => {
    for (const stored of [{ [LAST_AREA_KEY]: 'virtual-range' }, {}] as Record<string, string>[]) {
      openPage(KIOSK, stored);
      expect(topChrome(renderToString(<App />))).toMatch(/^<div class="range-app__top"><header class="range-header range-header--kiosk">/);
    }
    openPage(PHONE);
    expect(topChrome(renderToString(<App />))).toMatch(/^<div class="range-app__top"><header class="range-header">/);
    const top = topChrome(
      renderToString(
        <RangeHeader>
          <DemoBand active connected />
        </RangeHeader>
      )
    );

    expect(top.indexOf('class="demo-band"')).toBeGreaterThan(top.indexOf('</header>'));
  });
});
