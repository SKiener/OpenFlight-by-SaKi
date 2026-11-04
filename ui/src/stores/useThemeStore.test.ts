import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_STORAGE_KEY } from '../theme/theme';

function installBrowser(initial: Record<string, string> = {}, options: { failWrites?: boolean } = {}) {
  const store = { ...initial };
  const dataset: Record<string, string> = {};
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
  vi.stubGlobal('document', { documentElement: { dataset } });
  vi.stubGlobal('window', { localStorage });
  return { dataset, store };
}

describe('useThemeStore', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('defaults to sun and persists dark', async () => {
    const { dataset, store } = installBrowser();
    const { useThemeStore } = await import('./useThemeStore');

    expect(useThemeStore.getState().theme).toBe('sun');

    useThemeStore.getState().setTheme('dark');

    expect(useThemeStore.getState().theme).toBe('dark');
    expect(store[THEME_STORAGE_KEY]).toBe('dark');
    expect(store['openflight.theme']).toBeUndefined();
    expect(dataset.theme).toBe('dark');
  });

  it('hydrates from stored dark', async () => {
    installBrowser({ [THEME_STORAGE_KEY]: 'dark' });
    const { useThemeStore } = await import('./useThemeStore');
    expect(useThemeStore.getState().theme).toBe('dark');
  });

  it('starts on sun although the old key holds dark', async () => {
    installBrowser({ 'openflight.theme': 'dark' });
    const { useThemeStore } = await import('./useThemeStore');
    expect(useThemeStore.getState().theme).toBe('sun');
  });

  it('applies the theme for the session when storage rejects the write', async () => {
    const { dataset } = installBrowser({}, { failWrites: true });
    const { useThemeStore } = await import('./useThemeStore');

    expect(() => useThemeStore.getState().setTheme('dark')).not.toThrow();
    expect(useThemeStore.getState().theme).toBe('dark');
    expect(dataset.theme).toBe('dark');
  });
});
