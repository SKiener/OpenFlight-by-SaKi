import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSystemStore } from '../stores/useSystemStore';
import { watchDemoTouches } from './useDemoTouch';

describe('watchDemoTouches', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    useSystemStore.setState({ demoActive: false });
  });

  it('pings at most every 30 s and only during the demo', () => {
    const screen = new EventTarget();
    const ping = vi.fn();
    const stop = watchDemoTouches(screen, ping);
    const touch = () => screen.dispatchEvent(new Event('pointerdown'));

    touch();
    expect(ping, 'no demo').not.toHaveBeenCalled();

    useSystemStore.setState({ demoActive: true });
    touch();
    touch();
    expect(ping).toHaveBeenCalledOnce();

    vi.advanceTimersByTime(29_999);
    touch();
    expect(ping).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(1);
    touch();
    expect(ping).toHaveBeenCalledTimes(2);

    useSystemStore.setState({ demoActive: false });
    vi.advanceTimersByTime(30_000);
    touch();
    expect(ping, 'the demo ended').toHaveBeenCalledTimes(2);

    stop();
    useSystemStore.setState({ demoActive: true });
    touch();
    expect(ping, 'no longer watching').toHaveBeenCalledTimes(2);
  });
});
