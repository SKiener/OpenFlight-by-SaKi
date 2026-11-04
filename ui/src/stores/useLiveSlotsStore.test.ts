import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { DEFAULT_LIVE_SLOTS, LIVE_SLOT_IDS, normalizeLiveSlots, withLiveSlot } from './useLiveSlotsStore';

const KEY = 'openflight.kiosk-live-slots:v1';

function installStorage(initial: Record<string, string> = {}, { failWrites = false } = {}) {
  const { store, localStorage } = fakeStorage(initial);
  const storage = failWrites
    ? {
        ...localStorage,
        setItem: () => {
          throw new Error('quota exceeded');
        },
      }
    : localStorage;
  vi.stubGlobal('window', { localStorage: storage });
  return store;
}

async function loadStore() {
  return (await import('./useLiveSlotsStore')).useLiveSlotsStore;
}

describe('LIVE_SLOT_IDS', () => {
  it('offers the apex after the total; the defaults stay as they were', () => {
    expect(LIVE_SLOT_IDS.slice(0, 3)).toEqual(['carry', 'total', 'apex']);
    expect(DEFAULT_LIVE_SLOTS).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
    expect(normalizeLiveSlots(['apex', 'ball_speed', 'launch_v', 'launch_h', 'curve'])[0]).toBe('apex');
  });
});

describe('normalizeLiveSlots', () => {
  it('keeps five distinct known ids as they are', () => {
    const slots = ['club_speed', 'smash', 'spin', 'club_path', 'club_aoa'];
    expect(normalizeLiveSlots(slots)).toEqual(slots);
  });

  it('keeps the impact tile in a large or a small slot; it is chosen last and never a default', () => {
    expect(LIVE_SLOT_IDS.at(-1)).toBe('impact');
    expect(DEFAULT_LIVE_SLOTS).not.toContain('impact');
    expect(normalizeLiveSlots(['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve'])).toEqual([
      'impact',
      'ball_speed',
      'launch_v',
      'launch_h',
      'curve',
    ]);
    expect(normalizeLiveSlots(['carry', 'ball_speed', 'launch_v', 'launch_h', 'impact'])[4]).toBe('impact');
    expect(normalizeLiveSlots(['impact', 'impact'])).toEqual(['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
    expect(withLiveSlot([...DEFAULT_LIVE_SLOTS], 1, 'impact')).toEqual(['carry', 'impact', 'launch_v', 'launch_h', 'curve']);
  });

  it('gives the default for anything but a list', () => {
    for (const stored of [null, undefined, 'carry', 42, { 0: 'carry' }]) {
      expect(normalizeLiveSlots(stored)).toEqual([...DEFAULT_LIVE_SLOTS]);
    }
  });

  it('falls back per slot for an unknown id', () => {
    expect(normalizeLiveSlots(['spin', 'nonsense', 'smash', 7, 'total'])).toEqual([
      'spin',
      'ball_speed',
      'smash',
      'launch_h',
      'total',
    ]);
  });

  it('fills a short list from the defaults and drops extra entries', () => {
    expect(normalizeLiveSlots(['spin'])).toEqual(['spin', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
    expect(normalizeLiveSlots(['spin', 'smash', 'total', 'curve', 'carry', 'ball_speed'])).toEqual([
      'spin',
      'smash',
      'total',
      'curve',
      'carry',
    ]);
  });

  it('keeps the first of a duplicate and gives the later slot its default', () => {
    expect(normalizeLiveSlots(['spin', 'spin', 'launch_v', 'launch_h', 'curve'])).toEqual([
      'spin',
      'ball_speed',
      'launch_v',
      'launch_h',
      'curve',
    ]);
  });

  it('takes the first free id when the slot’s default is already shown', () => {
    // Slot 1's default (ball_speed) is in slot 0; the first free id in chooser order is carry.
    expect(normalizeLiveSlots(['ball_speed', 'ball_speed', 'launch_v', 'launch_h', 'curve'])).toEqual([
      'ball_speed',
      'carry',
      'launch_v',
      'launch_h',
      'curve',
    ]);
    // A known id later in the list keeps its slot; an earlier unknown one does not take it.
    expect(normalizeLiveSlots(['x', 'carry', 'launch_v', 'launch_h', 'curve'])).toEqual([
      'total',
      'carry',
      'launch_v',
      'launch_h',
      'curve',
    ]);
  });

  it('always yields five distinct known ids', () => {
    const stored = ['curve', 'curve', 'curve', 'curve', 'curve'];
    const slots = normalizeLiveSlots(stored);
    expect(slots).toHaveLength(5);
    expect(new Set(slots).size).toBe(5);
    expect(slots.every((id) => (LIVE_SLOT_IDS as readonly string[]).includes(id))).toBe(true);
  });
});

describe('withLiveSlot', () => {
  it('puts the value in the slot', () => {
    expect(withLiveSlot(DEFAULT_LIVE_SLOTS, 4, 'spin')).toEqual([
      'carry',
      'ball_speed',
      'launch_v',
      'launch_h',
      'spin',
    ]);
  });

  it('trades places with the slot that already shows the value', () => {
    expect(withLiveSlot(DEFAULT_LIVE_SLOTS, 0, 'curve')).toEqual([
      'curve',
      'ball_speed',
      'launch_v',
      'launch_h',
      'carry',
    ]);
  });

  it('ignores a slot out of range and an unknown id', () => {
    expect(withLiveSlot(DEFAULT_LIVE_SLOTS, 5, 'spin')).toEqual([...DEFAULT_LIVE_SLOTS]);
    expect(withLiveSlot(DEFAULT_LIVE_SLOTS, -1, 'spin')).toEqual([...DEFAULT_LIVE_SLOTS]);
    expect(withLiveSlot(DEFAULT_LIVE_SLOTS, 0, 'nope' as never)).toEqual([...DEFAULT_LIVE_SLOTS]);
  });
});

describe('useLiveSlotsStore', () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('starts with carry and ball speed large, launch, direction and curve small', async () => {
    installStorage();
    expect((await loadStore()).getState().slots).toEqual(['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
  });

  it('restores a stored layout, and validates it', async () => {
    installStorage({ [KEY]: JSON.stringify(['club_speed', 'smash', 'spin', 'club_path', 'club_aoa']) });
    expect((await loadStore()).getState().slots).toEqual(['club_speed', 'smash', 'spin', 'club_path', 'club_aoa']);

    vi.resetModules();
    installStorage({ [KEY]: JSON.stringify(['spin', 'spin', 'bogus', 'launch_h', 'curve']) });
    expect((await loadStore()).getState().slots).toEqual(['spin', 'ball_speed', 'launch_v', 'launch_h', 'curve']);
  });

  it('falls back to the default for stored text that is not JSON', async () => {
    installStorage({ [KEY]: '{not json' });
    expect((await loadStore()).getState().slots).toEqual([...DEFAULT_LIVE_SLOTS]);
  });

  it('saves a choice and the default', async () => {
    const store = installStorage();
    const useLiveSlotsStore = await loadStore();
    useLiveSlotsStore.getState().setSlot(2, 'spin');
    expect(useLiveSlotsStore.getState().slots).toEqual(['carry', 'ball_speed', 'spin', 'launch_h', 'curve']);
    expect(JSON.parse(store[KEY])).toEqual(['carry', 'ball_speed', 'spin', 'launch_h', 'curve']);

    useLiveSlotsStore.getState().reset();
    expect(useLiveSlotsStore.getState().slots).toEqual([...DEFAULT_LIVE_SLOTS]);
    expect(JSON.parse(store[KEY])).toEqual([...DEFAULT_LIVE_SLOTS]);
  });

  it('keeps a choice for the session when storage refuses it', async () => {
    installStorage({}, { failWrites: true });
    const useLiveSlotsStore = await loadStore();
    useLiveSlotsStore.getState().setSlot(0, 'total');
    expect(useLiveSlotsStore.getState().slots[0]).toBe('total');
  });
});
