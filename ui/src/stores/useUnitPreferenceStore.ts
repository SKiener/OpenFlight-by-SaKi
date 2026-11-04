import { create } from 'zustand';
import { isUnitSystem, type UnitSystem } from '../utils/units';
import { readStorage, writeStorage } from '../utils/storage';

const STORAGE_KEY = 'openflight.unit-system';

function readStoredUnitSystem(): UnitSystem {
  const storedValue = readStorage(STORAGE_KEY);
  return isUnitSystem(storedValue) ? storedValue : 'imperial';
}

interface UnitPreferenceState {
  unitSystem: UnitSystem;
  setUnitSystem: (unitSystem: UnitSystem) => void;
}

export const useUnitPreferenceStore = create<UnitPreferenceState>((set) => ({
  unitSystem: readStoredUnitSystem(),
  setUnitSystem: (unitSystem) => {
    writeStorage(STORAGE_KEY, unitSystem);
    set({ unitSystem });
  },
}));
