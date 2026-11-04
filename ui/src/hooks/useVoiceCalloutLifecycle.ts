import { useEffect } from 'react';
import { shotAnnouncer, speaksOnThisDevice } from '../services/shotAnnouncer';
import { useVoiceCalloutStore } from '../stores/useVoiceCalloutStore';
import { unlockAudioCue } from '../utils/audioCue';
import { cancelSpeech, primeSpeech } from '../utils/speech';

const GESTURE_EVENTS = ['touchend', 'click', 'keydown'] as const;

/**
 * Page-level wiring for the shot beep and spoken callouts.
 *
 * iOS Safari only plays Web Audio and speech after a tap has started them. A
 * tap fires touchend and click (pointerdown is not activation-triggering for
 * touch), so both are unlocked there, on every gesture until it takes.
 *
 * A hidden page cannot speak: iOS suspends it when the screen locks or Safari
 * leaves the foreground. Shots still waiting for final values are dropped then,
 * so they are not spoken late, and anything queued is cleared both ways (iOS
 * can leave the queue stuck after a suspension).
 *
 * The kiosk never speaks, so it never primes speech either.
 */
export function useVoiceCalloutLifecycle() {
  useEffect(() => {
    const onGesture = () => {
      unlockAudioCue();
      if (useVoiceCalloutStore.getState().enabled && speaksOnThisDevice()) primeSpeech();
    };
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') shotAnnouncer.reset();
      cancelSpeech();
    };

    GESTURE_EVENTS.forEach((name) => window.addEventListener(name, onGesture));
    document.addEventListener('visibilitychange', onVisibilityChange);

    return () => {
      GESTURE_EVENTS.forEach((name) => window.removeEventListener(name, onGesture));
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, []);
}
