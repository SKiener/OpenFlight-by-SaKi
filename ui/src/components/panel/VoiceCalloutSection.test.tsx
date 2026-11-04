import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installFakeBrowser } from '../../test-utils/fakeBrowser';
import { fakeNative, fakeNativeSpeech } from '../../test-utils/nativeBridge';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

// Server rendering reads a zustand store's initial state, and the setting now
// arrives from the server after load. The hook is swapped for one that reads
// the live state, so a test can apply the server's setting before rendering.
vi.mock('../../stores/useVoiceCalloutStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../stores/useVoiceCalloutStore')>();
  const store = actual.useVoiceCalloutStore;
  const live = ((selector?: (state: ReturnType<typeof store.getState>) => unknown) =>
    selector ? selector(store.getState()) : store.getState()) as unknown as typeof store;
  return { ...actual, useVoiceCalloutStore: Object.assign(live, store) };
});

async function installBrowser({
  speech = true,
  enabled = false,
  metrics = ['carry', 'ball_speed'],
  phone = true,
  native,
}: { speech?: boolean; enabled?: boolean; metrics?: string[]; phone?: boolean; native?: unknown } = {}) {
  installFakeBrowser({ speech, phone, native, languages: ['de-DE'] });
  const { useVoiceCalloutStore } = await import('../../stores/useVoiceCalloutStore');
  useVoiceCalloutStore.getState().applyServerSettings({ enabled, metrics, language: 'de' });
}

async function render() {
  const { VoiceCalloutSection } = await import('./VoiceCalloutSection');
  return renderToString(<VoiceCalloutSection />).replace(/<!-- -->/g, '');
}

describe('VoiceCalloutSection', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('says so when the phone browser cannot speak', async () => {
    await installBrowser({ speech: false });
    const html = await render();

    expect(html).toContain('Voice callouts');
    expect(html).toContain('This browser cannot speak');
    expect(html).not.toContain('segmented-control');
  });

  it('only shows the on/off switch while off', async () => {
    await installBrowser();
    const html = await render();

    expect(html).toContain('aria-label="Voice callouts"');
    expect(html).toMatch(/aria-pressed="true"[^>]*>Off</);
    expect(html).not.toContain('Values to announce');
    expect(html).not.toContain('Test voice');
  });

  it('shows labelled values, the voice language, the test button and the iPhone hint while on', async () => {
    await installBrowser({ enabled: true });
    const html = await render();

    expect(html).toMatch(/aria-pressed="true"[^>]*>On</);
    expect(html).toContain('<span class="menu-sheet__sublabel">Values to announce</span>');
    expect(html).toContain('aria-label="Values to announce"');
    expect(html).toMatch(/menu-sheet__chip menu-sheet__chip--active" aria-pressed="true">Carry</);
    expect(html).toMatch(/menu-sheet__chip menu-sheet__chip--active" aria-pressed="true">Ball speed</);
    expect(html).toMatch(/menu-sheet__chip" aria-pressed="false">Spin</);
    expect(html).not.toContain('aria-disabled');
    expect(html).toContain('<span class="menu-sheet__sublabel">Voice language</span>');
    expect(html).toContain('<option value="de" selected="">Deutsch</option>');
    expect(html).toContain('>Español</option>');
    expect(html).not.toContain('Français');
    expect(html).not.toContain('Português');
    expect(html).toContain('Test voice');
    expect(html).toContain('iPhone: keep the screen on, Safari in front and Silent mode off.');
  });

  it('marks the last chosen value as locked', async () => {
    await installBrowser({ enabled: true, metrics: ['spin'] });
    const html = await render();

    expect(html).toMatch(/aria-pressed="true" aria-disabled="true">Spin</);
    expect(html.match(/aria-disabled/g)).toHaveLength(1);
  });

  it('on the kiosk, says the phone speaks and offers the same controls', async () => {
    await installBrowser({ phone: false, enabled: true });
    const html = await render();

    expect(html).toContain('menu-sheet__section-title">Callouts (on the phone)</span>');
    expect(html).toContain('aria-label="Callouts (on the phone)"');
    expect(html).toMatch(/aria-pressed="true"[^>]*>On</);
    expect(html).toContain('aria-label="Values to announce"');
    expect(html).toContain('aria-label="Voice language"');
    expect(html).toContain('Test voice');
    expect(html).not.toContain('>Voice callouts<');
    expect(html).toContain('The phone speaks while OpenFlight is open on it. In Safari: Silent mode off.');
    expect(html).not.toContain('iPhone: keep the screen on');
  });

  it('in the companion app, drops the Safari hint: the app speaks while it is open', async () => {
    await installBrowser({
      enabled: true,
      speech: false,
      native: fakeNative({ capabilities: { speech: true }, speech: fakeNativeSpeech() }),
    });
    const html = await render();

    expect(html).toContain('menu-sheet__section-title">Voice callouts</span>');
    expect(html).toContain('Test voice');
    expect(html).toContain('The app speaks while it is open in the foreground.');
    expect(html).not.toContain('Safari');
  });

  it('words the app and kiosk hints in every menu language', async () => {
    await installBrowser();
    const { catalogs } = await import('../../i18n');

    expect(catalogs.de['menu.voiceHintApp']).toBe('Die App spricht, solange sie im Vordergrund offen ist.');
    expect(catalogs.de['menu.voiceHintKiosk']).toBe(
      'Es spricht das Handy, solange OpenFlight dort offen ist. In Safari: Stummmodus aus.'
    );
    expect(catalogs.es['menu.voiceHintApp']).toBe('La app habla mientras está abierta en primer plano.');
    expect(catalogs.es['menu.voiceHintKiosk']).toContain('En Safari');
  });

  it('on the kiosk, needs no speech of its own', async () => {
    await installBrowser({ phone: false, speech: false });
    const html = await render();

    expect(html).toContain('Callouts (on the phone)');
    expect(html).toContain('segmented-control');
    expect(html).not.toContain('This browser cannot speak');
  });

  it('is titled in the menu language on the kiosk', async () => {
    await installBrowser({ phone: false });
    const { t } = await import('../../i18n');
    const { catalogs } = await import('../../i18n');

    expect(catalogs.de['menu.voiceKiosk']).toBe('Ansage (auf dem Handy)');
    expect(catalogs.es['menu.voiceKiosk']).toBe('Avisos (en el móvil)');
    expect(t('menu.voiceKiosk')).toBe('Callouts (on the phone)');
  });

  it('is part of the menu sheet', async () => {
    await installBrowser();
    const { MenuSheet } = await import('./MenuSheet');
    const start = renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} />);
    const area = renderToString(<MenuSheet onClose={() => {}} onShutdown={() => {}} initialArea="callouts" />);

    // The start page names the Callouts area and its state; the section itself is on the area page.
    expect(start).toMatch(/menu-sheet__area-name">Callouts<\/span><span class="menu-sheet__area-summary">Off</);
    expect(start).not.toContain('menu-sheet__section-title">Voice callouts');
    expect(area).toContain('menu-sheet__area-title" id="menu-sheet-area-title">Callouts<');
    expect(area).toContain('menu-sheet__section-title">Voice callouts');
  });

  it('lets a finger drag scroll the taller menu, and a phone pan it natively', () => {
    expect(css).toMatch(/\.menu-sheet,\s*\.menu-sheet button\s*\{\s*touch-action: none;/);
    expect(css).toMatch(
      /@media \(hover: none\) and \(pointer: coarse\) \{\s*\.menu-sheet,\s*\.menu-sheet button\s*\{\s*touch-action: pan-y;/
    );
    expect(css).toMatch(/\.menu-sheet \{\s*overscroll-behavior: contain;\s*user-select: none;/);
  });
});
