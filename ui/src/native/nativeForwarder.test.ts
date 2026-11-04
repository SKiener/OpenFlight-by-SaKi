import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeNative, PHONE_EPOCH_MS, sensorSnapshot } from '../test-utils/nativeBridge';
import {
  MAX_QUEUED_ONSETS,
  NativeForwarder,
  PROBE_BURST,
  PROBE_BURST_GAP_MS,
  PROBE_INTERVAL_MS,
  SENSORS_INTERVAL_MS,
  type ForwarderSocket,
} from './nativeForwarder';
import { NATIVE_EVENT } from './openflightNative';

const PI_OFFSET_S = 5 * 86400;

const ONSET = {
  type: 'audio_onset',
  v: 1,
  capture_id: 'c1',
  seq: 1,
  onset_host_us: 812_345_678_901,
  onset_epoch_ms: PHONE_EPOCH_MS - 30,
  clock: { host_us: 812_345_700_000, epoch_ms: PHONE_EPOCH_MS - 9, bracket_us: 2 },
  epoch_ms: PHONE_EPOCH_MS - 5,
  tz_offset_min: 60,
  fat_cue: { detected: false },
};

interface Harness {
  socket: ForwarderSocket & { emit: ReturnType<typeof vi.fn>; emitWithAck: ReturnType<typeof vi.fn> };
  native: ReturnType<typeof fakeNative>;
  target: EventTarget;
  forwarder: NativeForwarder;
  setConnected: (value: boolean) => void;
  clock: { wall: number; mono: number };
}

function harness(rttMs = 8): Harness {
  let connected = true;
  const clock = { wall: PHONE_EPOCH_MS, mono: 1000 };
  const socket = {
    isConnected: () => connected,
    emit: vi.fn(),
    // The Pi stamps the probe half-way through the round trip.
    emitWithAck: vi.fn(async (_event: string, payload: { probe_seq: number; client_send_epoch_ms: number }) => {
      clock.mono += rttMs;
      clock.wall += rttMs;
      return {
        ok: true,
        probe_seq: payload.probe_seq,
        pi_epoch_s: (payload.client_send_epoch_ms + rttMs / 2) / 1000 - PI_OFFSET_S,
        pi_monotonic_ns: 42,
      };
    }),
  };
  const native = fakeNative();
  const target = new EventTarget();
  const forwarder = new NativeForwarder(native, socket, target, {
    wallMs: () => clock.wall,
    monotonicMs: () => clock.mono,
  });
  forwarder.start();
  return { socket, native, target, forwarder, setConnected: (value) => (connected = value), clock };
}

const dispatch = (target: EventTarget, detail: unknown) =>
  target.dispatchEvent(new CustomEvent(NATIVE_EVENT, { detail }));
const emitted = (socket: Harness['socket'], event: string) =>
  socket.emit.mock.calls.filter(([name]) => name === event).map(([, payload]) => payload);

beforeEach(() => {
  vi.useFakeTimers();
  vi.spyOn(console, 'debug').mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('NativeForwarder: audio onsets', () => {
  it('forwards each native onset as phone_audio_onset with the send time', () => {
    const { socket, target, clock, forwarder } = harness();
    clock.wall = PHONE_EPOCH_MS + 20;
    dispatch(target, ONSET);
    const [payload] = emitted(socket, 'phone_audio_onset');
    expect(payload).toMatchObject({ ...ONSET, epoch_ms: PHONE_EPOCH_MS + 20 });
    expect(payload).toHaveProperty('tz_offset_min');
    forwarder.stop();
  });

  it('ignores other native events', () => {
    const { socket, target, forwarder } = harness();
    dispatch(target, { type: 'audio_status', state: 'running' });
    dispatch(target, null);
    dispatch(target, 'audio_onset');
    expect(socket.emit).not.toHaveBeenCalled();
    forwarder.stop();
  });

  it('keeps onsets while disconnected and sends them, oldest first, on reconnect', () => {
    const { socket, target, forwarder, setConnected } = harness();
    setConnected(false);
    for (let seq = 1; seq <= MAX_QUEUED_ONSETS + 2; seq += 1) dispatch(target, { ...ONSET, seq });
    expect(socket.emit).not.toHaveBeenCalled();
    setConnected(true);
    forwarder.onConnect();
    const seqs = emitted(socket, 'phone_audio_onset').map((payload) => (payload as { seq: number }).seq);
    expect(seqs).toHaveLength(MAX_QUEUED_ONSETS);
    expect(seqs[0]).toBe(3);
    expect(seqs.at(-1)).toBe(MAX_QUEUED_ONSETS + 2);
    forwarder.stop();
  });

  it('stops listening when stopped', () => {
    const { socket, target, forwarder } = harness();
    forwarder.stop();
    dispatch(target, ONSET);
    expect(socket.emit).not.toHaveBeenCalled();
  });
});

describe('NativeForwarder: sensors', () => {
  it('sends a snapshot on connect and every minute', async () => {
    const { socket, native, forwarder } = harness();
    forwarder.onConnect();
    await vi.advanceTimersByTimeAsync(0);
    expect(emitted(socket, 'phone_sensors')).toHaveLength(1);
    expect(emitted(socket, 'phone_sensors')[0]).toMatchObject({
      type: 'sensors',
      barometer: sensorSnapshot().barometer,
      epoch_ms: PHONE_EPOCH_MS,
    });
    // The snapshot never asks for a location fix (no location prompt from the forwarder).
    expect(native.getSensors).toHaveBeenCalledWith();
    await vi.advanceTimersByTimeAsync(SENSORS_INTERVAL_MS);
    expect(emitted(socket, 'phone_sensors')).toHaveLength(2);
    forwarder.stop();
  });

  it('skips the snapshot while disconnected and survives a failing bridge', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { socket, native, forwarder, setConnected } = harness();
    setConnected(false);
    await forwarder.sendSensors();
    expect(native.getSensors).not.toHaveBeenCalled();
    setConnected(true);
    native.getSensors.mockRejectedValueOnce(new Error('busy'));
    await forwarder.sendSensors();
    expect(emitted(socket, 'phone_sensors')).toHaveLength(0);
    forwarder.stop();
  });
});

describe('NativeForwarder: clock probes', () => {
  it('measures the round trip, reports it and estimates the offset', async () => {
    const { socket, forwarder } = harness(8);
    const result = await forwarder.probe();
    expect(result).toEqual({ probe_seq: 1, rtt_ms: 8, offset_s: expect.closeTo(PI_OFFSET_S, 6) });
    const [, probe] = socket.emitWithAck.mock.calls[0];
    expect(probe).toMatchObject({ probe_seq: 1, client_send_epoch_ms: PHONE_EPOCH_MS, epoch_ms: PHONE_EPOCH_MS });
    expect(emitted(socket, 'phone_clock_probe')).toEqual([
      expect.objectContaining({ report: { probe_seq: 1, client_send_epoch_ms: PHONE_EPOCH_MS, rtt_ms: 8 } }),
    ]);
    forwarder.stop();
  });

  it('runs a burst on connect, then one probe every 30 s, and keeps the best', async () => {
    const { socket, forwarder } = harness(5);
    forwarder.onConnect();
    await vi.advanceTimersByTimeAsync((PROBE_BURST - 1) * PROBE_BURST_GAP_MS);
    expect(socket.emitWithAck).toHaveBeenCalledTimes(PROBE_BURST);
    await vi.advanceTimersByTimeAsync(PROBE_INTERVAL_MS);
    expect(socket.emitWithAck).toHaveBeenCalledTimes(PROBE_BURST + 1);
    expect(forwarder.bestProbe()?.rtt_ms).toBe(5);
    forwarder.stop();
    await vi.advanceTimersByTimeAsync(PROBE_INTERVAL_MS * 2);
    expect(socket.emitWithAck).toHaveBeenCalledTimes(PROBE_BURST + 1);
  });

  it('drops a probe the Pi did not answer or answered for another number', async () => {
    const { socket, forwarder } = harness();
    socket.emitWithAck.mockRejectedValueOnce(new Error('timeout'));
    expect(await forwarder.probe()).toBeNull();
    socket.emitWithAck.mockResolvedValueOnce({ ok: true, probe_seq: 99, pi_epoch_s: 1, pi_monotonic_ns: 1 });
    expect(await forwarder.probe()).toBeNull();
    socket.emitWithAck.mockResolvedValueOnce({ ok: false, error: 'phone_only' });
    expect(await forwarder.probe()).toBeNull();
    expect(emitted(socket, 'phone_clock_probe')).toHaveLength(0);
    expect(forwarder.bestProbe()).toBeNull();
    forwarder.stop();
  });

  it('does not probe while disconnected', async () => {
    const { socket, forwarder, setConnected } = harness();
    setConnected(false);
    expect(await forwarder.probe()).toBeNull();
    expect(socket.emitWithAck).not.toHaveBeenCalled();
    forwarder.stop();
  });
});
