import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { useShotStore } from '../stores/useShotStore';
import { useSystemStore } from '../stores/useSystemStore';
import { makeShot } from '../test-utils/shot';
import type { Shot } from '../types/shot';
import { socketService } from './socketService';

const socket = vi.hoisted(() => {
  const handlers = new Map<string, (data?: unknown) => void>();
  return {
    handlers,
    on(event: string, handler: (data?: unknown) => void) {
      handlers.set(event, handler);
    },
    emit: vi.fn(),
    close: vi.fn(),
  };
});

vi.mock('socket.io-client', () => ({ io: () => socket }));

const receive = (event: string, data?: unknown): void => {
  const handler = socket.handlers.get(event);
  if (!handler) throw new Error(`no listener for ${event}`);
  handler(data);
};

const generation = () => useShotStore.getState().sessionGeneration;

beforeAll(() => {
  socketService.connect();
});

beforeEach(() => {
  vi.spyOn(console, 'log').mockImplementation(() => {});
  socket.emit.mockClear();
  useShotStore.setState(useShotStore.getInitialState(), true);
});

afterEach(() => {
  vi.restoreAllMocks();
  useSystemStore.setState({ demoActive: false, demoUnavailable: null });
});

const real = [makeShot({ timestamp: 'r1', profile_id: 'p1' }), makeShot({ timestamp: 'r2', profile_id: 'p1' })];
const demo = [makeShot({ timestamp: 'd1', profile_id: 'p1' }), makeShot({ timestamp: 'd2', profile_id: 'p1' })];
const sessionState = (shots: Shot[], demoActive?: boolean) =>
  receive('session_state', { shots, stats: {}, ...(demoActive === undefined ? {} : { demo_active: demoActive }) });

describe('socketService and the demo', () => {
  it('keeps the demo state the server sends', () => {
    receive('demo_state', { active: true });
    expect(useSystemStore.getState()).toMatchObject({ demoActive: true, demoUnavailable: null });

    receive('demo_state', { active: false, unavailable: 'swing-speed' });
    expect(useSystemStore.getState()).toMatchObject({ demoActive: false, demoUnavailable: 'swing-speed' });
  });

  it('swaps the session once per demo start and end, however often the server resends it', () => {
    sessionState(real, false);
    expect(generation(), 'the first session of the page').toBe(1);

    receive('demo_state', { active: false });
    sessionState(real, false);
    sessionState(real.slice(1), false);
    expect(generation(), 'another screen connected, asked for the session, deleted a shot').toBe(1);
    expect(useShotStore.getState().shots).toEqual(real.slice(1));

    receive('demo_state', { active: true });
    expect(generation(), 'demo_state comes first and swaps nothing').toBe(1);
    sessionState(demo, true);
    expect(generation(), 'the demo started').toBe(2);
    expect(useSystemStore.getState().demoActive).toBe(true);

    receive('demo_state', { active: true });
    sessionState(demo, true);
    expect(generation(), 'a second switch-on, another screen connecting').toBe(2);

    receive('demo_state', { active: false });
    sessionState(real, false);
    expect(generation(), 'the demo ended').toBe(3);
    expect(useShotStore.getState().shots).toEqual(real);
    expect(useSystemStore.getState().demoActive).toBe(false);
  });

  it('takes the first session of a page that connects mid-demo as a new one, once', () => {
    receive('demo_state', { active: true });
    sessionState(demo, true);
    sessionState(demo, true);

    expect(generation()).toBe(1);
    expect(useSystemStore.getState().demoActive).toBe(true);
  });

  it('keeps one session with a server that sends no demo flag', () => {
    useSystemStore.setState({ demoActive: true });
    sessionState(real);
    sessionState(real);

    expect(generation()).toBe(1);
    expect(useSystemStore.getState().demoActive, 'nothing said about the demo').toBe(true);
  });

  it('starts a new session when the server clears one', () => {
    sessionState(real, false);

    receive('session_cleared', { profile_id: 'p1', shots: [makeShot({ timestamp: 'other', profile_id: 'p2' })] });
    receive('session_cleared', { profile_id: 'p2', shots: [] });

    expect(generation()).toBe(3);
  });

  it('sends the switch, the way out and the touch pings', () => {
    socketService.setDemoMode(true);
    socketService.setDemoMode(false);
    socketService.demoTouch();

    expect(socket.emit.mock.calls).toEqual([
      ['set_demo_mode', { enabled: true }],
      ['set_demo_mode', { enabled: false }],
      ['demo_touch'],
    ]);
  });
});

describe('deleting a shot over the socket', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('settles on the next session without the shot', async () => {
    const deleted = socketService.deleteShot('r2');
    let done = false;
    void deleted.then(() => (done = true));

    expect(socket.emit).toHaveBeenCalledExactlyOnceWith('delete_shot', { timestamp: 'r2' });
    sessionState(real, false);
    await Promise.resolve();
    expect(done, 'a session that still has the shot').toBe(false);

    sessionState(real.slice(0, 1), false);
    await expect(deleted).resolves.toBeUndefined();
    expect(useShotStore.getState().shots).toEqual(real.slice(0, 1));
  });

  it('fails on delete_shot_error or after 5 s, once', async () => {
    const refused = socketService.deleteShot('gone');
    receive('delete_shot_error', { error: 'Shot not found' });
    await expect(refused).rejects.toThrow();

    vi.useFakeTimers();
    const silent = socketService.deleteShot('r1');
    const outcome = expect(silent).rejects.toThrow();
    vi.advanceTimersByTime(4999);
    sessionState(real, false);
    vi.advanceTimersByTime(1);
    await outcome;

    sessionState([], false);
    receive('delete_shot_error', { error: 'Shot not found' });
  });
});

describe('socketService and the client clock', () => {
  it('sends the client time once on every connection, before asking for the session', () => {
    vi.useFakeTimers({ now: 1_761_384_000_000, toFake: ['Date'] });
    try {
      receive('connect');
    } finally {
      vi.useRealTimers();
    }
    const events = socket.emit.mock.calls.map((call) => call[0]);
    expect(events.filter((event) => event === 'client_clock')).toHaveLength(1);
    expect(events.indexOf('client_clock')).toBeLessThan(events.indexOf('get_session'));
    const [, payload] = socket.emit.mock.calls.find((call) => call[0] === 'client_clock')!;
    expect(payload).toEqual({
      epoch_ms: 1_761_384_000_000,
      tz_offset_min: -new Date(1_761_384_000_000).getTimezoneOffset(),
    });
  });
});
