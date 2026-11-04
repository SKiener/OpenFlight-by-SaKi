import { create } from 'zustand';
import { readStorage, writeStorage } from '../utils/storage';

/** The kiosk's Live tiles, saved on the kiosk itself (each phone keeps its own layout). */
export const LIVE_SLOTS_STORAGE_KEY = 'openflight.kiosk-live-slots:v1';

/** The values a kiosk Live tile can show, in the order the chooser lists them. */
export const LIVE_SLOT_IDS = [
  'carry',
  'total',
  'apex',
  'ball_speed',
  'club_speed',
  'smash',
  'launch_v',
  'launch_h',
  'curve',
  'spin',
  'club_path',
  'club_aoa',
  'impact',
] as const;

export type LiveSlotId = (typeof LIVE_SLOT_IDS)[number];

/** Two large tiles on top, three small ones below. */
export const LIVE_SLOT_COUNT = 5;
export const LARGE_LIVE_SLOTS = 2;

export const DEFAULT_LIVE_SLOTS: readonly LiveSlotId[] = ['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve'];

export function isLiveSlotId(value: unknown): value is LiveSlotId {
  return typeof value === 'string' && (LIVE_SLOT_IDS as readonly string[]).includes(value);
}

/**
 * Five distinct known ids from whatever was stored. A known id keeps its slot unless an
 * earlier slot already shows it; every other slot falls back to its default, or, when
 * that is taken, to the first free id in {@link LIVE_SLOT_IDS} order.
 */
export function normalizeLiveSlots(stored: unknown): LiveSlotId[] {
  const list = Array.isArray(stored) ? stored : [];
  const slots: Array<LiveSlotId | null> = [];
  for (let index = 0; index < LIVE_SLOT_COUNT; index += 1) {
    const id: unknown = list[index];
    slots.push(isLiveSlotId(id) && !slots.includes(id) ? id : null);
  }
  for (let index = 0; index < LIVE_SLOT_COUNT; index += 1) {
    if (slots[index] !== null) continue;
    const fallback = DEFAULT_LIVE_SLOTS[index];
    slots[index] = slots.includes(fallback) ? (LIVE_SLOT_IDS.find((id) => !slots.includes(id)) ?? fallback) : fallback;
  }
  return slots as LiveSlotId[];
}

/** The stored layout; the default when nothing (or no JSON) is stored. */
export function readLiveSlots(): LiveSlotId[] {
  const raw = readStorage(LIVE_SLOTS_STORAGE_KEY);
  if (raw === null) return [...DEFAULT_LIVE_SLOTS];
  try {
    return normalizeLiveSlots(JSON.parse(raw));
  } catch {
    return [...DEFAULT_LIVE_SLOTS];
  }
}

/**
 * `slots` with `id` in slot `index`. A value shown in another slot trades places with
 * the one it replaces, so no value is ever shown twice.
 */
export function withLiveSlot(slots: readonly LiveSlotId[], index: number, id: LiveSlotId): LiveSlotId[] {
  const next = [...slots];
  if (index < 0 || index >= next.length || !isLiveSlotId(id)) return next;
  const other = next.indexOf(id);
  if (other !== -1 && other !== index) next[other] = next[index];
  next[index] = id;
  return next;
}

interface LiveSlotsState {
  slots: LiveSlotId[];
  setSlot: (index: number, id: LiveSlotId) => void;
  reset: () => void;
}

function save(slots: LiveSlotId[]): LiveSlotId[] {
  writeStorage(LIVE_SLOTS_STORAGE_KEY, JSON.stringify(slots));
  return slots;
}

export const useLiveSlotsStore = create<LiveSlotsState>((set, get) => ({
  slots: readLiveSlots(),
  setSlot: (index, id) => set({ slots: save(withLiveSlot(get().slots, index, id)) }),
  reset: () => set({ slots: save([...DEFAULT_LIVE_SLOTS]) }),
}));
