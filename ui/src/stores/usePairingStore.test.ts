import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  handleServerDisconnect,
  handleSocketConnectError,
  refreshPairingAccess,
  usePairingStore,
} from './usePairingStore';

function serverSays(state: string | null) {
  vi.stubGlobal(
    'fetch',
    state === null
      ? vi.fn().mockRejectedValue(new TypeError('offline'))
      : vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ state }) })
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  usePairingStore.setState({ access: 'checking' });
});

describe('pairing access', () => {
  it('starts as checking and follows the server status', async () => {
    expect(usePairingStore.getState().access).toBe('checking');

    serverSays('unpaired');
    await expect(refreshPairingAccess()).resolves.toBe('unpaired');
    expect(usePairingStore.getState().access).toBe('unpaired');
  });

  it('is unknown when the status cannot be read, so the app still renders', async () => {
    serverSays(null);

    await refreshPairingAccess();

    expect(usePairingStore.getState().access).toBe('unknown');
  });

  it('shows pairing when the server refuses the socket for missing pairing', () => {
    usePairingStore.setState({ access: 'paired' });

    handleSocketConnectError(new Error('xhr poll error'), true);
    expect(usePairingStore.getState().access).toBe('paired');

    handleSocketConnectError(new Error('pairing_required'), false);
    expect(usePairingStore.getState().access).toBe('unpaired');
  });

  it('reconnects after a server disconnect only while the device is still allowed', async () => {
    const reconnect = vi.fn();

    serverSays('paired');
    await handleServerDisconnect(reconnect);
    expect(reconnect).toHaveBeenCalledTimes(1);

    serverSays('unpaired');
    await handleServerDisconnect(reconnect);
    expect(reconnect).toHaveBeenCalledTimes(1);
    expect(usePairingStore.getState().access).toBe('unpaired');
  });
});
