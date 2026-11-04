import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installFakeBrowser } from '../test-utils/fakeBrowser';
import { parseVoiceCalloutSettings, type VoiceCalloutPatch } from './useVoiceCalloutStore';

/** The per-device setting of earlier builds; now ignored. */
const LEGACY_STORAGE_KEY = 'openflight.voice-callouts:v1';

async function loadStore() {
  const module = await import('./useVoiceCalloutStore');
  const sent: VoiceCalloutPatch[] = [];
  module.setVoiceCalloutSender((patch) => sent.push(patch));
  return { store: module.useVoiceCalloutStore, sent, module };
}

describe('useVoiceCalloutStore', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('starts off, with carry and ball speed, in the browser language, until the server answers', async () => {
    installFakeBrowser({ languages: ['de-DE', 'en-US'] });
    const { store } = await loadStore();

    expect(store.getState()).toMatchObject({
      enabled: false,
      metrics: ['carry', 'ball_speed'],
      language: 'de',
      synced: false,
    });
  });

  it('ignores the old per-device setting, so a phone never switches the callouts on by itself', async () => {
    const { store: storage } = installFakeBrowser({
      phone: true,
      storage: { [LEGACY_STORAGE_KEY]: JSON.stringify({ enabled: true, metrics: ['spin'], language: 'es' }) },
    });
    const { store, sent } = await loadStore();

    expect(store.getState()).toMatchObject({ enabled: false, metrics: ['carry', 'ball_speed'], language: 'en' });
    store.getState().applyServerSettings({ enabled: false, metrics: ['carry', 'ball_speed'], language: null });
    expect(store.getState().enabled).toBe(false);
    expect(sent).toEqual([]);
    expect(storage[LEGACY_STORAGE_KEY], 'left alone, not rewritten').toBe(
      JSON.stringify({ enabled: true, metrics: ['spin'], language: 'es' })
    );
  });

  it('takes the server setting, in the browser language while none was picked', async () => {
    installFakeBrowser({ languages: ['es-ES'] });
    const { store } = await loadStore();

    store.getState().applyServerSettings({ enabled: true, metrics: ['spin', 'carry'], language: null });
    expect(store.getState()).toMatchObject({ enabled: true, metrics: ['carry', 'spin'], language: 'es', synced: true });

    store.getState().applyServerSettings({ enabled: true, metrics: ['spin'], language: 'de' });
    expect(store.getState()).toMatchObject({ metrics: ['spin'], language: 'de' });
  });

  it('shows a change at once and sends only what changed', async () => {
    installFakeBrowser();
    const { store, sent } = await loadStore();

    store.getState().setEnabled(true);
    store.getState().toggleMetric('spin');
    store.getState().setLanguage('es');

    expect(store.getState()).toMatchObject({ enabled: true, metrics: ['carry', 'ball_speed', 'spin'], language: 'es' });
    expect(sent).toEqual([{ enabled: true }, { metrics: ['carry', 'ball_speed', 'spin'] }, { language: 'es' }]);
  });

  it('lets the server have the final word over an optimistic change', async () => {
    installFakeBrowser();
    const { store } = await loadStore();

    store.getState().setEnabled(true);
    // The server refused (or another screen switched off meanwhile) and resends its setting.
    store.getState().applyServerSettings({ enabled: false, metrics: ['carry'], language: 'en' });

    expect(store.getState()).toMatchObject({ enabled: false, metrics: ['carry'] });
  });

  it('keeps metrics in the spoken order and never empty, and never sends an empty list', async () => {
    installFakeBrowser();
    const { store, sent } = await loadStore();

    store.getState().toggleMetric('smash');
    store.getState().toggleMetric('club_speed');
    expect(store.getState().metrics).toEqual(['carry', 'ball_speed', 'club_speed', 'smash']);

    for (const metric of ['carry', 'ball_speed', 'club_speed', 'smash'] as const) {
      store.getState().toggleMetric(metric);
    }
    expect(store.getState().metrics).toEqual(['smash']);
    expect(sent.every((patch) => patch.metrics === undefined || patch.metrics.length > 0)).toBe(true);
    expect(sent.at(-1)).toEqual({ metrics: ['smash'] });
  });

  it('still applies changes locally without a sender', async () => {
    installFakeBrowser();
    const { store, module } = await loadStore();
    module.setVoiceCalloutSender(null);

    expect(() => store.getState().setEnabled(true)).not.toThrow();
    expect(store.getState().enabled).toBe(true);
  });
});

describe('parseVoiceCalloutSettings', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the defaults for a missing or malformed payload', () => {
    vi.stubGlobal('navigator', { languages: ['es-MX'], language: 'es-MX' });
    const defaults = { enabled: false, metrics: ['carry', 'ball_speed'], language: 'es' };

    expect(parseVoiceCalloutSettings(undefined)).toEqual(defaults);
    expect(parseVoiceCalloutSettings(null)).toEqual(defaults);
    expect(parseVoiceCalloutSettings('on')).toEqual(defaults);
    expect(parseVoiceCalloutSettings(7)).toEqual(defaults);
  });

  it('repairs each field on its own', () => {
    vi.stubGlobal('navigator', { languages: ['en-GB'], language: 'en-GB' });

    expect(parseVoiceCalloutSettings({ enabled: 'yes', metrics: ['spin', 'warp', 'spin'], language: 'it' })).toEqual({
      enabled: false,
      metrics: ['spin'],
      language: 'en',
    });
    expect(parseVoiceCalloutSettings({ enabled: true, metrics: [] })).toEqual({
      enabled: true,
      metrics: ['carry', 'ball_speed'],
      language: 'en',
    });
  });

  it.each(['fr', 'pt'])('falls back to the browser language for the removed %s', (language) => {
    vi.stubGlobal('navigator', { languages: ['de-DE'], language: 'de-DE' });

    expect(parseVoiceCalloutSettings({ enabled: true, metrics: ['carry'], language }).language).toBe('de');
  });

  it('defaults to English without a navigator', () => {
    vi.stubGlobal('navigator', undefined);

    expect(parseVoiceCalloutSettings(null).language).toBe('en');
  });
});
