import { useEffect } from 'react';
import { socketService } from '../services/socketService';
import { useSystemStore } from '../stores/useSystemStore';

const DEMO_TOUCH_INTERVAL_MS = 30_000;

export function watchDemoTouches(target: EventTarget, ping: () => void): () => void {
  let lastPing = -Infinity;
  const onPointerDown = () => {
    if (!useSystemStore.getState().demoActive) return;
    const now = performance.now();
    if (now - lastPing < DEMO_TOUCH_INTERVAL_MS) return;
    lastPing = now;
    ping();
  };
  // Capture: a handler that stops the event on its way up cannot hide the touch.
  target.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
  return () => target.removeEventListener('pointerdown', onPointerDown, { capture: true });
}

/** The server ends a demo after 15 minutes without a touch on any screen; this screen reports its touches. */
export function useDemoTouch(enabled: boolean): void {
  useEffect(() => (enabled ? watchDemoTouches(window, () => socketService.demoTouch()) : undefined), [enabled]);
}
