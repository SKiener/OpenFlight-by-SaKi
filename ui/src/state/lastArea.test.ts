import { afterEach, describe, expect, it, vi } from 'vitest';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { safeStorage } from '../utils/storage';
import { LAST_AREA_KEY, readLastArea, startArea, writeLastArea } from './lastArea';

const holding = (value: string | null) => ({ getItem: () => value });
const KIOSK = '127.0.0.1';
const PHONE = 'openflight.local';

// Blocked site data: touching window.localStorage itself throws.
const blockStorage = () =>
  vi.stubGlobal('window', {
    get localStorage(): never {
      throw new Error('SecurityError');
    },
  });

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('last area', () => {
  it('reopens the range and nothing else', () => {
    expect(readLastArea(holding('virtual-range'), KIOSK)).toBe('virtual-range');
    expect(readLastArea(holding('camera'), KIOSK)).toBe('live');
    expect(readLastArea(holding('live'), KIOSK)).toBe('live');
    expect(readLastArea(holding(null), KIOSK)).toBe('live');

    blockStorage();
    expect(readLastArea(safeStorage, KIOSK)).toBe('live');
  });

  it('never reopens the range off the kiosk', () => {
    for (const host of [PHONE, '192.168.1.20:8080', 'device.example.ts.net', '']) {
      expect(readLastArea(holding('virtual-range'), host), host).toBe('live');
    }
    expect(readLastArea(holding('virtual-range'), 'localhost:5173')).toBe('virtual-range');
  });

  it('stores live for every view but the range, so only a range that was last reopens', () => {
    const { store, localStorage } = fakeStorage();

    for (const view of ['live', 'camera', 'stats', 'shots', 'profiles', 'debug'] as const) {
      writeLastArea(localStorage, 'virtual-range');
      expect(store[LAST_AREA_KEY], 'the range').toBe('virtual-range');
      expect(readLastArea(localStorage, KIOSK), 'the range').toBe('virtual-range');

      writeLastArea(localStorage, view);
      expect(store[LAST_AREA_KEY], view).toBe('live');
      expect(readLastArea(localStorage, KIOSK), view).toBe('live');
    }
  });

  it('keeps working when the storage is blocked', () => {
    blockStorage();

    expect(() => writeLastArea(safeStorage, 'virtual-range')).not.toThrow();
  });

  it('reads back what it wrote through safeStorage', () => {
    const { localStorage } = fakeStorage();
    vi.stubGlobal('window', { localStorage });

    writeLastArea(safeStorage, 'virtual-range');
    expect(readLastArea(safeStorage, KIOSK)).toBe('virtual-range');
    writeLastArea(safeStorage, 'live');
    expect(readLastArea(safeStorage, KIOSK)).toBe('live');
  });
});

describe('start area', () => {
  it('opens the range for ?rangeDemo=1 whatever is stored, on any host: it is a measurement switch', () => {
    expect(startArea(holding(null), '?rangeDemo=1', KIOSK)).toBe('virtual-range');
    expect(startArea(holding('live'), '?rangeDemo=1', KIOSK)).toBe('virtual-range');
    expect(startArea(holding('camera'), '?x=1&rangeDemo=1', KIOSK)).toBe('virtual-range');
    expect(startArea(holding(null), '?rangeDemo=1', PHONE)).toBe('virtual-range');
  });

  it('otherwise reopens the stored area, on the kiosk only', () => {
    expect(startArea(holding(null), '', KIOSK)).toBe('live');
    expect(startArea(holding('virtual-range'), '', KIOSK)).toBe('virtual-range');
    expect(startArea(holding('virtual-range'), '?rangeDemo=0', KIOSK)).toBe('virtual-range');
    expect(startArea(holding(null), '?rangeDemo=0', KIOSK)).toBe('live');
    expect(startArea(holding('virtual-range'), '', PHONE)).toBe('live');
    expect(startArea(holding('virtual-range'), '?rangeDemo=0', PHONE)).toBe('live');
  });
});
