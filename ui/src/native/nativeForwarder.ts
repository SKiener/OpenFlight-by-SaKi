import { clientClockPayload } from '../utils/clockOffset';
import { finiteNumber, NATIVE_EVENT, type OpenFlightNative } from './openflightNative';

/** A sensor snapshot goes to the Pi on connect and this often (design §11). */
export const SENSORS_INTERVAL_MS = 60_000;
/** Clock probes: a short burst on connect, then one this often. */
export const PROBE_BURST = 5;
export const PROBE_BURST_GAP_MS = 250;
export const PROBE_INTERVAL_MS = 30_000;
export const PROBE_ACK_TIMEOUT_MS = 3000;
/** Onsets detected while the socket is down wait for the reconnect, the newest this many. */
export const MAX_QUEUED_ONSETS = 50;

/** The page's authenticated Socket.IO connection, as the forwarder uses it. */
export interface ForwarderSocket {
  isConnected: () => boolean;
  emit: (event: string, payload: object) => void;
  emitWithAck: (event: string, payload: object, timeoutMs: number) => Promise<unknown>;
}

export interface ClockProbeResult {
  probe_seq: number;
  rtt_ms: number;
  /** Phone wall minus Pi wall at the probe's midpoint, in seconds. */
  offset_s: number;
}

interface ProbeAck {
  ok: true;
  probe_seq: number;
  pi_epoch_s: number;
  pi_monotonic_ns: number;
}

function isProbeAck(ack: unknown, probeSeq: number): ack is ProbeAck {
  const value = ack as Partial<ProbeAck> | null;
  return (
    !!value &&
    value.ok === true &&
    value.probe_seq === probeSeq &&
    finiteNumber(value.pi_epoch_s) &&
    value.pi_epoch_s > 0
  );
}

export interface ForwarderClocks {
  /** Phone wall time in ms (Date.now): the clock the native onsets are stamped on. */
  wallMs: () => number;
  /** A monotonic ms clock for the round trip (performance.now). */
  monotonicMs: () => number;
}

const defaultClocks: ForwarderClocks = { wallMs: () => Date.now(), monotonicMs: () => performance.now() };

/**
 * Forwards the iOS companion's events to the Pi over the page's socket (design §11). Log only on
 * the server: `phone_audio_onset` for each native `audio_onset`, `phone_sensors` on connect and
 * every minute, and two-way `phone_clock_probe`s that tie the phone clock to the Pi's. The kiosk
 * never creates one (it has no companion).
 */
export class NativeForwarder {
  private readonly queued: object[] = [];
  private sensorsTimer: ReturnType<typeof setInterval> | null = null;
  private probeTimer: ReturnType<typeof setInterval> | null = null;
  private burstTimers: ReturnType<typeof setTimeout>[] = [];
  private probeSeq = 0;
  private started = false;
  private readonly results: ClockProbeResult[] = [];

  private readonly native: OpenFlightNative;
  private readonly socket: ForwarderSocket;
  private readonly target: Pick<Window, 'addEventListener' | 'removeEventListener'>;
  private readonly clocks: ForwarderClocks;

  constructor(
    native: OpenFlightNative,
    socket: ForwarderSocket,
    target: Pick<Window, 'addEventListener' | 'removeEventListener'> = window,
    clocks: ForwarderClocks = defaultClocks
  ) {
    this.native = native;
    this.socket = socket;
    this.target = target;
    this.clocks = clocks;
  }

  start() {
    if (this.started) return;
    this.started = true;
    this.target.addEventListener(NATIVE_EVENT, this.onNativeEvent);
    this.sensorsTimer = setInterval(() => void this.sendSensors(), SENSORS_INTERVAL_MS);
    this.probeTimer = setInterval(() => void this.probe(), PROBE_INTERVAL_MS);
  }

  stop() {
    if (!this.started) return;
    this.started = false;
    this.target.removeEventListener(NATIVE_EVENT, this.onNativeEvent);
    if (this.sensorsTimer !== null) clearInterval(this.sensorsTimer);
    if (this.probeTimer !== null) clearInterval(this.probeTimer);
    this.sensorsTimer = this.probeTimer = null;
    this.clearBurst();
    this.queued.length = 0;
  }

  /** The socket (re)connected: send what waited, a sensor snapshot and a probe burst. */
  onConnect() {
    if (!this.started) return;
    for (const payload of this.queued.splice(0)) this.socket.emit('phone_audio_onset', payload);
    void this.sendSensors();
    this.clearBurst();
    for (let index = 0; index < PROBE_BURST; index += 1) {
      this.burstTimers.push(setTimeout(() => void this.probe(), index * PROBE_BURST_GAP_MS));
    }
  }

  /** The probe with the smallest round trip so far (the server keeps its own estimate). */
  bestProbe(): ClockProbeResult | null {
    return this.results.reduce<ClockProbeResult | null>(
      (best, result) => (best === null || result.rtt_ms < best.rtt_ms ? result : best),
      null
    );
  }

  private clearBurst() {
    this.burstTimers.forEach((timer) => clearTimeout(timer));
    this.burstTimers = [];
  }

  private readonly onNativeEvent = (event: Event) => {
    const detail = (event as CustomEvent<unknown>).detail as { type?: unknown } | null;
    if (!detail || typeof detail !== 'object' || detail.type !== 'audio_onset') return;
    // The clock fields say when the message is sent (design §7.1); the onset keeps its own times.
    const payload = { ...detail, ...clientClockPayload(new Date(this.clocks.wallMs())) };
    if (this.socket.isConnected()) {
      this.socket.emit('phone_audio_onset', payload);
      return;
    }
    this.queued.push(payload);
    if (this.queued.length > MAX_QUEUED_ONSETS) this.queued.shift();
  };

  async sendSensors(): Promise<void> {
    if (!this.started || !this.socket.isConnected()) return;
    try {
      const snapshot = await this.native.getSensors();
      if (!this.started || !this.socket.isConnected()) return;
      this.socket.emit('phone_sensors', { ...snapshot, ...clientClockPayload(new Date(this.clocks.wallMs())) });
    } catch (error) {
      console.warn('Native sensor snapshot failed:', error);
    }
  }

  /** One NTP-style probe: the Pi echoes its clocks, the page reports the round trip it measured. */
  async probe(): Promise<ClockProbeResult | null> {
    if (!this.started || !this.socket.isConnected()) return null;
    this.probeSeq += 1;
    const probeSeq = this.probeSeq;
    const sendWallMs = this.clocks.wallMs();
    const sentAt = this.clocks.monotonicMs();
    let ack: unknown;
    try {
      ack = await this.socket.emitWithAck(
        'phone_clock_probe',
        { probe_seq: probeSeq, client_send_epoch_ms: sendWallMs, ...clientClockPayload(new Date(sendWallMs)) },
        PROBE_ACK_TIMEOUT_MS
      );
    } catch {
      return null;
    }
    const rttMs = this.clocks.monotonicMs() - sentAt;
    if (!isProbeAck(ack, probeSeq) || !(rttMs >= 0)) return null;
    const result = {
      probe_seq: probeSeq,
      rtt_ms: Math.round(rttMs * 1000) / 1000,
      offset_s: (sendWallMs + rttMs / 2) / 1000 - ack.pi_epoch_s,
    };
    this.results.push(result);
    if (this.results.length > 20) this.results.shift();
    console.debug(
      `[clock probe] #${probeSeq}: round trip ${result.rtt_ms.toFixed(1)} ms, phone − Pi ${result.offset_s.toFixed(3)} s`
    );
    if (this.socket.isConnected()) {
      this.socket.emit('phone_clock_probe', {
        report: { probe_seq: probeSeq, client_send_epoch_ms: sendWallMs, rtt_ms: result.rtt_ms },
        ...clientClockPayload(new Date(this.clocks.wallMs())),
      });
    }
    return result;
  }
}
