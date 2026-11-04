import { afterEach, describe, expect, it, vi } from 'vitest';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { readStorage, safeSessionStorage, writeStorage } from './storage';

describe('storage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads and writes localStorage', () => {
    const store: Record<string, string> = {};
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => store[key] ?? null,
        setItem: (key: string, value: string) => {
          store[key] = value;
        },
      },
    });

    expect(readStorage('k')).toBeNull();
    writeStorage('k', 'v');
    expect(store.k).toBe('v');
    expect(readStorage('k')).toBe('v');
  });

  it('returns null and ignores writes outside a browser', () => {
    expect(readStorage('k')).toBeNull();
    expect(() => writeStorage('k', 'v')).not.toThrow();
  });

  it('survives storage that is missing or throws', () => {
    vi.stubGlobal('window', {});
    expect(readStorage('k')).toBeNull();
    expect(() => writeStorage('k', 'v')).not.toThrow();

    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => {
          throw new Error('SecurityError');
        },
        setItem: () => {
          throw new Error('QuotaExceededError');
        },
      },
    });
    expect(readStorage('k')).toBeNull();
    expect(() => writeStorage('k', 'v')).not.toThrow();
  });
});

describe('safeSessionStorage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('reads and writes sessionStorage, not localStorage', () => {
    const session = fakeStorage();
    const local = fakeStorage({ k: 'from localStorage' });
    vi.stubGlobal('window', { sessionStorage: session.localStorage, localStorage: local.localStorage });

    expect(safeSessionStorage.getItem('k')).toBeNull();
    safeSessionStorage.setItem('k', 'v');
    expect(session.store.k).toBe('v');
    expect(safeSessionStorage.getItem('k')).toBe('v');
    expect(local.store.k).toBe('from localStorage');
  });

  it('returns null and ignores writes outside a browser, or when storage is missing or throws', () => {
    expect(safeSessionStorage.getItem('k')).toBeNull();
    expect(() => safeSessionStorage.setItem('k', 'v')).not.toThrow();

    vi.stubGlobal('window', {});
    expect(safeSessionStorage.getItem('k')).toBeNull();
    expect(() => safeSessionStorage.setItem('k', 'v')).not.toThrow();

    const failing = () => {
      throw new DOMException('blocked', 'SecurityError');
    };
    vi.stubGlobal('window', { sessionStorage: { getItem: failing, setItem: failing } });
    expect(safeSessionStorage.getItem('k')).toBeNull();
    expect(() => safeSessionStorage.setItem('k', 'v')).not.toThrow();
  });
});
