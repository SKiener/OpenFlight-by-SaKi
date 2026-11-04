import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { applyTheme, isTheme, readStoredTheme, THEME_STORAGE_KEY } from './theme';

function installBrowser(initial: Record<string, string> = {}) {
  const store = { ...initial };
  const dataset: Record<string, string> = {};
  const localStorage = {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
  };
  vi.stubGlobal('localStorage', localStorage);
  vi.stubGlobal('document', { documentElement: { dataset } });
  vi.stubGlobal('window', { localStorage });
  return { dataset, store };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('theme helpers', () => {
  it('accepts only sun and dark', () => {
    expect(isTheme('sun')).toBe(true);
    expect(isTheme('dark')).toBe(true);
    expect(isTheme('light')).toBe(false);
    expect(isTheme('solar')).toBe(false);
    expect(isTheme(null)).toBe(false);
  });

  it('stores under a v2 key', () => {
    expect(THEME_STORAGE_KEY).toBe('openflight.theme.v2');
  });

  it('defaults to sun when storage is missing or invalid', () => {
    installBrowser();
    expect(readStoredTheme()).toBe('sun');
    installBrowser({ [THEME_STORAGE_KEY]: 'nope' });
    expect(readStoredTheme()).toBe('sun');
    installBrowser({ [THEME_STORAGE_KEY]: 'light' });
    expect(readStoredTheme()).toBe('sun');
  });

  it('ignores the old key, so every device starts on sun once', () => {
    installBrowser({ 'openflight.theme': 'dark' });
    expect(readStoredTheme()).toBe('sun');
  });

  it('reads a stored dark theme', () => {
    installBrowser({ [THEME_STORAGE_KEY]: 'dark' });
    expect(readStoredTheme()).toBe('dark');
  });

  it('writes data-theme on the document element', () => {
    const { dataset } = installBrowser();
    applyTheme('sun');
    expect(dataset.theme).toBe('sun');
    applyTheme('dark');
    expect(dataset.theme).toBe('dark');
  });

  it('index.html applies the same key and default before the bundle loads', () => {
    const html = readFileSync(fileURLToPath(new URL('../../index.html', import.meta.url)), 'utf8');
    expect(html).toContain('data-theme="sun"');
    expect(html).toContain(`localStorage.getItem('${THEME_STORAGE_KEY}')`);
    expect(html).not.toMatch(/'light'/);
  });
});
