import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

describe('unit preferences', () => {
  it.each(['imperial', 'metric', 'mph-meters', 'kmh-yards'])('restores %s after reload', async (value) => {
    vi.resetModules();
    vi.stubGlobal('window', { localStorage: { getItem: () => value, setItem: vi.fn() } });
    const { useUnitPreferenceStore } = await import('./useUnitPreferenceStore');
    expect(useUnitPreferenceStore.getState().unitSystem).toBe(value);
  });
  it('saves a mixed choice so a restart keeps it', async () => {
    const setItem = vi.fn();
    vi.stubGlobal('window', { localStorage: { getItem: () => null, setItem } });
    const { useUnitPreferenceStore } = await import('./useUnitPreferenceStore');
    useUnitPreferenceStore.getState().setUnitSystem('mph-meters');
    expect(setItem).toHaveBeenCalledWith('openflight.unit-system', 'mph-meters');
    expect(useUnitPreferenceStore.getState().unitSystem).toBe('mph-meters');
  });
});

describe('useUnitPreferenceStore', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('hydrates and persists the unit system', async () => {
    const store: Record<string, string> = { 'openflight.unit-system': 'metric' };
    vi.stubGlobal('window', {
      localStorage: {
        getItem: (key: string) => store[key] ?? null,
        setItem: (key: string, value: string) => {
          store[key] = value;
        },
      },
    });
    const { useUnitPreferenceStore } = await import('./useUnitPreferenceStore');

    expect(useUnitPreferenceStore.getState().unitSystem).toBe('metric');
    useUnitPreferenceStore.getState().setUnitSystem('imperial');
    expect(store['openflight.unit-system']).toBe('imperial');
  });

  it('falls back to imperial and keeps working when storage is unavailable', async () => {
    vi.stubGlobal('window', {});
    const { useUnitPreferenceStore } = await import('./useUnitPreferenceStore');

    expect(useUnitPreferenceStore.getState().unitSystem).toBe('imperial');
    expect(() => useUnitPreferenceStore.getState().setUnitSystem('metric')).not.toThrow();
    expect(useUnitPreferenceStore.getState().unitSystem).toBe('metric');
  });

  it('keeps a mixed choice for the session when storage is blocked', async () => {
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
    const { useUnitPreferenceStore } = await import('./useUnitPreferenceStore');

    expect(useUnitPreferenceStore.getState().unitSystem).toBe('imperial');
    expect(() => useUnitPreferenceStore.getState().setUnitSystem('kmh-yards')).not.toThrow();
    expect(useUnitPreferenceStore.getState().unitSystem).toBe('kmh-yards');
  });
});
