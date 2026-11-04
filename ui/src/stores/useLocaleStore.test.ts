import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LOCALE_STORAGE_KEY } from '../i18n';

function installBrowser(initial: Record<string, string> = {}, options: { failWrites?: boolean; lang?: string } = {}) {
  const store = { ...initial };
  const documentElement = { lang: options.lang ?? 'en' };
  const localStorage = {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      if (options.failWrites) {
        throw new Error('quota exceeded');
      }
      store[key] = value;
    },
  };
  vi.stubGlobal('localStorage', localStorage);
  vi.stubGlobal('document', { documentElement });
  vi.stubGlobal('window', { localStorage });
  return { documentElement, store };
}

describe('useLocaleStore', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('defaults to English and persists Spanish', async () => {
    const { documentElement, store } = installBrowser();
    const { useLocaleStore } = await import('./useLocaleStore');
    const { t } = await import('../i18n');

    expect(useLocaleStore.getState().locale).toBe('en');
    expect(t('nav.live')).toBe('Live');

    useLocaleStore.getState().setLocale('es');

    expect(useLocaleStore.getState().locale).toBe('es');
    expect(store[LOCALE_STORAGE_KEY]).toBe('es');
    expect(documentElement.lang).toBe('es');
    expect(t('nav.live')).toBe('En vivo');
  });

  it('hydrates from stored Spanish', async () => {
    const { documentElement } = installBrowser({ [LOCALE_STORAGE_KEY]: 'es' });
    const { useLocaleStore } = await import('./useLocaleStore');
    const { t } = await import('../i18n');

    expect(useLocaleStore.getState().locale).toBe('es');
    expect(documentElement.lang).toBe('es');
    expect(t('nav.live')).toBe('En vivo');
  });

  it.each([
    ['fr', 'fr'],
    ['pt', 'pt-BR'],
  ])('loads English, and resets <html lang>, when the removed %s was stored', async (stored, staleLang) => {
    // The seeded lang is what an older index.html would have set, so 'en' below is the store's doing.
    const { documentElement } = installBrowser({ [LOCALE_STORAGE_KEY]: stored }, { lang: staleLang });
    const { useLocaleStore } = await import('./useLocaleStore');
    const { t } = await import('../i18n');

    expect(useLocaleStore.getState().locale).toBe('en');
    expect(documentElement.lang).toBe('en');
    expect(t('nav.live')).toBe('Live');
  });

  it('persists German and restores it on the next load', async () => {
    const { documentElement, store } = installBrowser();
    const { useLocaleStore } = await import('./useLocaleStore');
    useLocaleStore.getState().setLocale('de');
    expect(store[LOCALE_STORAGE_KEY]).toBe('de');
    expect(documentElement.lang).toBe('de-DE');

    vi.resetModules();
    const restored = await import('./useLocaleStore');
    const { t } = await import('../i18n');
    expect(restored.useLocaleStore.getState().locale).toBe('de');
    expect(t('metric.hLaunch')).toBe('Startrichtung');
  });

  it('applies the locale for the session when storage rejects the write', async () => {
    const { documentElement } = installBrowser({}, { failWrites: true });
    const { useLocaleStore } = await import('./useLocaleStore');

    expect(() => useLocaleStore.getState().setLocale('es')).not.toThrow();
    expect(useLocaleStore.getState().locale).toBe('es');
    expect(documentElement.lang).toBe('es');
  });
});
