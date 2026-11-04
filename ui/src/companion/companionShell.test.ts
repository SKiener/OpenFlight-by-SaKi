import { afterEach, describe, expect, it } from 'vitest';
import { LOCALE_STORAGE_KEY, setActiveLocale } from '../i18n';
import { useLocaleStore } from '../stores/useLocaleStore';
import { adoptBrowserLocale, browserLocale, isCompanionPath } from './companionShell';

afterEach(() => {
  useLocaleStore.setState({ locale: 'en' });
  setActiveLocale('en');
});

const storage = (value: string | null) => ({ getItem: (key: string) => (key === LOCALE_STORAGE_KEY ? value : null) });

describe('companion shell', () => {
  it('matches the companion route with or without a trailing slash', () => {
    expect(isCompanionPath('/companion')).toBe(true);
    expect(isCompanionPath('/companion/')).toBe(true);
    expect(isCompanionPath('/display')).toBe(false);
    expect(isCompanionPath('/')).toBe(false);
  });

  it('maps browser languages to the first supported locale', () => {
    expect(browserLocale(['de-DE', 'en-US'])).toBe('de');
    expect(browserLocale(['ja-JP', 'es-MX'])).toBe('es');
    expect(browserLocale(['ja-JP'])).toBeNull();
  });

  it('skips French and Portuguese, which are no longer offered', () => {
    expect(browserLocale(['fr-FR', 'pt-BR'])).toBeNull();
    expect(browserLocale(['pt-BR', 'de-DE'])).toBe('de');
  });

  it('follows the device language until a language was chosen explicitly', () => {
    adoptBrowserLocale(storage(null), ['de-DE']);
    expect(useLocaleStore.getState().locale).toBe('de');

    useLocaleStore.setState({ locale: 'en' });
    adoptBrowserLocale(storage('es'), ['de-DE']);
    expect(useLocaleStore.getState().locale).toBe('en');
  });

  it('does not count a stored French or Portuguese as a choice, so the device language applies', () => {
    adoptBrowserLocale(storage('fr'), ['de-DE']);
    expect(useLocaleStore.getState().locale).toBe('de');

    useLocaleStore.setState({ locale: 'en' });
    adoptBrowserLocale(storage('pt'), ['es-ES']);
    expect(useLocaleStore.getState().locale).toBe('es');
  });

  it('keeps the default when storage is unavailable and the language is unsupported', () => {
    const blocked = {
      getItem: () => {
        throw new Error('blocked');
      },
    };
    adoptBrowserLocale(blocked, ['ja-JP']);
    expect(useLocaleStore.getState().locale).toBe('en');
  });
});
