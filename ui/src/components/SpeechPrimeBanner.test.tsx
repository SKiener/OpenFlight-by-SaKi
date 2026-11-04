import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installFakeBrowser } from '../test-utils/fakeBrowser';
import { fakeNative, fakeNativeSpeech } from '../test-utils/nativeBridge';

// Server rendering reads a zustand store's initial state; read the live one so
// the server's setting can be applied first (as in VoiceCalloutSection.test).
vi.mock('../stores/useVoiceCalloutStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../stores/useVoiceCalloutStore')>();
  const store = actual.useVoiceCalloutStore;
  const live = ((selector?: (state: ReturnType<typeof store.getState>) => unknown) =>
    selector ? selector(store.getState()) : store.getState()) as unknown as typeof store;
  return { ...actual, useVoiceCalloutStore: Object.assign(live, store) };
});

const PROMPT = 'Tap the screen once to turn on the callouts.';

async function setup({
  enabled = true,
  phone = true,
  speech = true,
  native,
}: { enabled?: boolean; phone?: boolean; speech?: boolean; native?: unknown } = {}) {
  const { synth } = installFakeBrowser({ phone, speech, native });
  const { useVoiceCalloutStore } = await import('../stores/useVoiceCalloutStore');
  useVoiceCalloutStore.getState().applyServerSettings({ enabled, metrics: ['carry'], language: 'en' });
  const { SpeechPrimeBanner } = await import('./SpeechPrimeBanner');
  const render = () => renderToString(<SpeechPrimeBanner />);
  return { synth, render };
}

describe('SpeechPrimeBanner', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('asks a phone for one tap while the callouts are on and speech is not primed', async () => {
    const { render } = await setup();

    const html = render();

    expect(html).toContain(PROMPT);
    expect(html).toContain('role="status"');
    expect(html).toContain('class="demo-band demo-band--view-only"');
    expect(html).not.toContain('<button');
  });

  it('goes away once the first tap primed speech', async () => {
    const { render, synth } = await setup();
    const { primeSpeech } = await import('../utils/speech');

    primeSpeech();
    expect(render(), 'still there until the engine confirms').toContain(PROMPT);
    synth.spoken[0].onstart?.();

    expect(render()).toBe('');
  });

  it('is not shown while the callouts are off', async () => {
    const { render } = await setup({ enabled: false });
    expect(render()).toBe('');
  });

  it('is never shown on the kiosk', async () => {
    const { render } = await setup({ phone: false });
    expect(render()).toBe('');
  });

  it('is not shown in the companion app, which speaks without a tap', async () => {
    const native = fakeNative({ capabilities: { speech: true }, speech: fakeNativeSpeech() });
    const { render } = await setup({ native });
    expect(render()).toBe('');
  });

  it('is not shown where the browser cannot speak at all', async () => {
    const { render } = await setup({ speech: false });
    expect(render()).toBe('');
  });

  it('is worded for each menu language', async () => {
    const { catalogs } = await import('../i18n');

    expect(catalogs.de['menu.voicePrime']).toBe('Tippe einmal aufs Display, um die Ansage zu aktivieren.');
    expect(catalogs.en['menu.voicePrime']).toBe(PROMPT);
    expect(catalogs.es['menu.voicePrime']).toBe('Toca la pantalla una vez para activar los avisos.');
  });
});
