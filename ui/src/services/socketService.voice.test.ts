import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Socket } from 'socket.io-client';
import { useShotStore } from '../stores/useShotStore';
import { useVoiceCalloutStore } from '../stores/useVoiceCalloutStore';
import { installFakeBrowser, spokenAloud, type FakeSynth } from '../test-utils/fakeBrowser';
import { socketService } from './socketService';

const page = vi.hoisted(() => ({ kiosk: false }));
vi.mock('../utils/kioskHost', () => ({ isKioskPage: () => page.kiosk, isKioskHost: () => page.kiosk }));

function fakeSocket() {
  const handlers = new Map<string, (data?: unknown) => void>();
  return {
    handlers,
    connected: true,
    on(event: string, handler: (data?: unknown) => void) {
      handlers.set(event, handler);
    },
    emit: vi.fn(),
    emitWithAck: vi.fn(async () => ({ ok: true })),
    timeout() {
      return { emitWithAck: this.emitWithAck };
    },
    close: vi.fn(),
    receive(event: string, data?: unknown) {
      const handler = handlers.get(event);
      if (!handler) throw new Error(`no listener for ${event}`);
      handler(data);
    },
  };
}

const ON = { enabled: true, metrics: ['carry'], language: 'en' };
const OFF = { ...ON, enabled: false };

let socket: ReturnType<typeof fakeSocket>;
let synth: FakeSynth;

function open({ kiosk }: { kiosk: boolean }) {
  page.kiosk = kiosk;
  ({ synth } = installFakeBrowser({ phone: !kiosk }));
  socket = fakeSocket();
  socketService.connect(() => socket as unknown as Socket);
  socket.receive('connect');
}

const emitted = (event: string) => socket.emit.mock.calls.filter(([name]) => name === event).map(([, p]) => p);

beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => {});
  useVoiceCalloutStore.setState(useVoiceCalloutStore.getInitialState(), true);
  useShotStore.setState(useShotStore.getInitialState(), true);
});

afterEach(() => {
  socketService.disconnect();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('socketService and the shared callout setting', () => {
  it('asks for the setting on connect and follows the broadcast', () => {
    open({ kiosk: false });
    expect(emitted('get_voice_callouts')).toHaveLength(1);

    socket.receive('voice_callouts', { enabled: true, metrics: ['spin', 'carry'], language: 'de' });

    expect(useVoiceCalloutStore.getState()).toMatchObject({
      enabled: true,
      metrics: ['carry', 'spin'],
      language: 'de',
      synced: true,
    });
  });

  it('sends each menu change to the server', () => {
    open({ kiosk: true });

    useVoiceCalloutStore.getState().setEnabled(true);
    useVoiceCalloutStore.getState().setLanguage('es');

    expect(emitted('set_voice_callouts')).toEqual([{ enabled: true }, { language: 'es' }]);
  });

  it('stops sending once disconnected', () => {
    open({ kiosk: false });
    socketService.disconnect();

    useVoiceCalloutStore.getState().setEnabled(true);

    expect(emitted('set_voice_callouts')).toEqual([]);
  });

  it('stops what a phone is saying when another screen switches the callouts off', () => {
    open({ kiosk: false });
    socket.receive('voice_callouts', ON);
    socket.receive('voice_callouts', ON);
    expect(synth.log).toEqual([]);

    socket.receive('voice_callouts', OFF);

    expect(synth.log).toEqual(['cancel']);
  });

  it('speaks the shots on a phone once the kiosk switched the callouts on', () => {
    open({ kiosk: false });
    socket.receive('voice_callouts', ON);

    socket.receive('shot', {
      shot: { mode: 'rolling-buffer', ball_speed_mph: 145, estimated_carry_yards: 180, timestamp: 't1' },
      stats: {},
    });

    expect(spokenAloud(synth)).toEqual([{ text: 'Carry 180 yards', lang: 'en-US' }]);
  });

  it('never speaks a shot on the kiosk', () => {
    open({ kiosk: true });
    socket.receive('voice_callouts', ON);

    socket.receive('shot', {
      shot: { mode: 'rolling-buffer', ball_speed_mph: 145, estimated_carry_yards: 180, timestamp: 't1' },
      stats: {},
    });

    expect(synth.spoken).toEqual([]);
  });
});

describe('the test button', () => {
  it('on the kiosk asks the phones instead of speaking', () => {
    open({ kiosk: true });
    socket.receive('voice_callouts', ON);

    socketService.testVoiceCallouts();

    expect(emitted('voice_test')).toHaveLength(1);
    expect(synth.spoken).toEqual([]);
  });

  it('on a phone speaks right there, without a round trip', () => {
    open({ kiosk: false });
    socket.receive('voice_callouts', ON);

    socketService.testVoiceCallouts();

    expect(emitted('voice_test')).toEqual([]);
    expect(spokenAloud(synth).map(({ text }) => text)).toEqual([expect.stringMatching(/^Carry \d+ yards$/)]);
  });

  it('a phone speaks the test callout the kiosk asked for', () => {
    open({ kiosk: false });
    socket.receive('voice_callouts', ON);

    socket.receive('voice_test', {});

    expect(spokenAloud(synth)).toEqual([{ text: 'Carry 165 yards', lang: 'en-US' }]);
  });

  it('a phone stays silent on a test while the callouts are off', () => {
    open({ kiosk: false });
    socket.receive('voice_callouts', OFF);

    socket.receive('voice_test', {});

    expect(synth.spoken).toEqual([]);
  });

  it('the kiosk ignores the test it sent out', () => {
    open({ kiosk: true });
    socket.receive('voice_callouts', ON);

    socket.receive('voice_test', {});

    expect(synth.spoken).toEqual([]);
  });
});
