import { afterEach, describe, expect, it, vi } from 'vitest';
import { socketService } from './socketService';
import { useDebugStore } from '../stores/useDebugStore';

const socket = vi.hoisted(() => ({ connected: true, on: vi.fn(), emit: vi.fn(), close: vi.fn(),
  timeout: vi.fn(), emitWithAck: vi.fn() }));
vi.mock('socket.io-client', () => ({ io: () => socket }));
afterEach(() => { socketService.disconnect(); vi.clearAllMocks(); socket.connected = true; });

describe('recovery socket acknowledgement', () => {
  it('reports disconnection without sending anything', async () => {
    expect(await socketService.reinitializeRadar()).toEqual({ ok: false, code: 'disconnected' });
    expect(socket.emitWithAck).not.toHaveBeenCalled();
  });
  it('waits for server completion and preserves its error code', async () => {
    socketService.connect();
    socket.timeout.mockReturnValue(socket);
    socket.emitWithAck.mockResolvedValue({ ok: false, code: 'busy' });
    expect(await socketService.reinitializeRadar()).toEqual({ ok: false, code: 'busy' });
    expect(socket.timeout).toHaveBeenCalledWith(30000);
    expect(socket.emitWithAck).toHaveBeenCalledWith('reinitialize_radar');
  });
  it('handles timeouts and malformed responses without claiming success', async () => {
    socketService.connect();
    socket.timeout.mockReturnValue(socket);
    socket.emitWithAck.mockRejectedValueOnce(new Error('timeout'));
    expect(await socketService.reinitializeRadar()).toEqual({ ok: false, code: 'timeout' });
    socket.emitWithAck.mockResolvedValueOnce(undefined);
    expect(await socketService.reinitializeRadar()).toEqual({ ok: false, code: 'failed' });
  });
  it('keeps an authoritative tuning snapshot and exposes a rejected change', () => {
    socketService.connect();
    const receive = (name: string, data: unknown) => {
      const listener = socket.on.mock.calls.find(([event]) => event === name)?.[1];
      expect(listener).toBeTypeOf('function');
      listener(data);
    };
    const config = { min_speed: 10, max_speed: 220, min_magnitude: 0, transmit_power: 3 };
    receive('radar_config', config);
    receive('radar_config_error', { code: 'busy' });
    expect(useDebugStore.getState().radarConfig).toEqual(config);
    expect(useDebugStore.getState().radarConfigError).toBe('busy');
    receive('radar_config', { ...config, transmit_power: 4 });
    expect(useDebugStore.getState().radarConfigError).toBeNull();
  });
});
