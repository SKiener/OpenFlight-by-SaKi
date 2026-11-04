/**
 * localStorage (and sessionStorage) that never throws. Storage can be missing
 * (outside a browser) or blocked (Safari with all cookies blocked throws a
 * SecurityError on access); a setting then simply applies for this session only.
 */
type BrowserStorage = 'localStorage' | 'sessionStorage';

function read(storage: BrowserStorage, key: string): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return window[storage].getItem(key);
  } catch {
    return null;
  }
}

function write(storage: BrowserStorage, key: string, value: string): void {
  if (typeof window === 'undefined') return;
  try {
    window[storage].setItem(key, value);
  } catch {
    // Quota, private mode, or blocked storage: keep the in-memory value.
  }
}

export function readStorage(key: string): string | null {
  return read('localStorage', key);
}

export function writeStorage(key: string, value: string): void {
  write('localStorage', key, value);
}

export const safeStorage = { getItem: readStorage, setItem: writeStorage };

/** sessionStorage: a value survives a reload, not a restart of the browser. */
export const safeSessionStorage = {
  getItem: (key: string): string | null => read('sessionStorage', key),
  setItem: (key: string, value: string): void => write('sessionStorage', key, value),
};
