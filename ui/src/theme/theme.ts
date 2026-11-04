/**
 * 'sun' is the sunlight-readable default (black on white, the kiosk is used
 * outdoors and its backlight cannot be raised); 'dark' is the night look.
 */
export type Theme = 'sun' | 'dark';

export const DEFAULT_THEME: Theme = 'sun';

/**
 * v2: every device starts on 'sun' once. The old key ('openflight.theme',
 * 'dark' | 'light') is deliberately ignored, not migrated.
 */
export const THEME_STORAGE_KEY = 'openflight.theme.v2';

export function isTheme(value: unknown): value is Theme {
  return value === 'sun' || value === 'dark';
}

export function readStoredTheme(): Theme {
  if (typeof window === 'undefined') {
    return DEFAULT_THEME;
  }

  try {
    const storedValue = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isTheme(storedValue) ? storedValue : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

export function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') {
    return;
  }

  document.documentElement.dataset.theme = theme;
}
