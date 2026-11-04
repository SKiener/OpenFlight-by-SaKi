import { create } from 'zustand';
import { fetchPairingAccess, PAIRING_REQUIRED_MESSAGE, type PairingAccess } from '../pairing/pairingApi';

/** `unknown` renders the app anyway: the server still enforces pairing. */
export type PairingGateState = PairingAccess | 'checking' | 'unknown';

interface PairingState {
  access: PairingGateState;
  setAccess: (access: PairingGateState) => void;
}

export const usePairingStore = create<PairingState>((set) => ({
  access: 'checking',
  setAccess: (access) => set({ access }),
}));

export async function refreshPairingAccess(): Promise<PairingGateState> {
  const access = (await fetchPairingAccess()) ?? 'unknown';
  usePairingStore.getState().setAccess(access);
  return access;
}

/** Socket.IO marks a refusal from the server's connect handler with `active === false`. */
export function handleSocketConnectError(error: Error, socketActive: boolean): void {
  if (!socketActive && error.message === PAIRING_REQUIRED_MESSAGE) {
    usePairingStore.getState().setAccess('unpaired');
  }
}

/** The server drops a device's socket when its pairing is revoked; otherwise reconnect. */
export async function handleServerDisconnect(reconnect: () => void): Promise<void> {
  if ((await refreshPairingAccess()) !== 'unpaired') reconnect();
}
