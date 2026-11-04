import { getOpenflightNative, type NativeSpeech } from '../native/openflightNative';

/**
 * Thin wrapper around the browser's speech synthesis (Web Speech API), or the
 * iOS companion's native speech when the page runs inside the app (that one
 * needs no priming and ignores the ring/silent switch).
 *
 * iOS Safari refuses to speak until the page has called `speak()` once while
 * handling a tap or click; after that it may speak at any time. Call
 * {@link primeSpeech} from such a handler. The utterance only sets `lang`, so
 * the device's own default voice for that language is used (iOS also ships
 * novelty voices that a "first matching voice" pick would land on).
 */

/** WebKit before Safari 26.4 can drop an utterance queued in the same task as `cancel()` (bug 191745). */
export const CANCEL_SETTLE_MS = 120;

interface SpeechGlobals {
  speechSynthesis?: SpeechSynthesis;
  SpeechSynthesisUtterance?: typeof SpeechSynthesisUtterance;
}

interface Speech {
  synth: SpeechSynthesis;
  Utterance: typeof SpeechSynthesisUtterance;
}

function getSpeech(): Speech | null {
  if (typeof window === 'undefined') return null;
  const globals = window as unknown as SpeechGlobals;
  if (!globals.speechSynthesis || typeof globals.SpeechSynthesisUtterance !== 'function') {
    return null;
  }
  return { synth: globals.speechSynthesis, Utterance: globals.SpeechSynthesisUtterance };
}

/** The companion app's speech, when this page runs in an app build that has it. */
function getNativeSpeech(): NativeSpeech | null {
  const native = getOpenflightNative();
  const speech = native?.capabilities.speech === true ? native.speech : undefined;
  return speech && typeof speech.speak === 'function' && typeof speech.cancel === 'function' ? speech : null;
}

/** Whether this page speaks through the companion app: no priming tap, and the silent switch does not mute it. */
export function speaksNatively(): boolean {
  return getNativeSpeech() !== null;
}

export function isSpeechSupported(): boolean {
  return getNativeSpeech() !== null || getSpeech() !== null;
}

let primed = false;
let pendingSpeak: ReturnType<typeof setTimeout> | null = null;
const primedListeners = new Set<() => void>();

/** Whether speech may start without a tap: Web Speech after {@link primeSpeech} took, native speech always. */
export function isSpeechPrimed(): boolean {
  return primed || getNativeSpeech() !== null;
}

/** Called once when Web Speech becomes primed; returns the unsubscribe. */
export function subscribeSpeechPrimed(listener: () => void): () => void {
  primedListeners.add(listener);
  return () => {
    primedListeners.delete(listener);
  };
}

function ignoreNativeFailure(): void {
  // A callout the app refused (a bad argument, a page from another origin) is skipped, as a
  // Web Speech utterance that never starts is.
}

/**
 * Unlock speech for this page. Must run inside a tap, click, or key handler;
 * a silent utterance is enough. It counts as done only once the engine
 * starts or finishes that utterance: WebKit drops a call made outside a
 * gesture without any event, so until then every gesture tries again.
 */
export function primeSpeech(): void {
  if (primed || getNativeSpeech()) return;
  const speech = getSpeech();
  if (!speech) return;

  const utterance = new speech.Utterance(' ');
  utterance.volume = 0;
  const confirm = () => {
    if (primed) return;
    primed = true;
    primedListeners.forEach((listener) => listener());
  };
  utterance.onstart = confirm;
  utterance.onend = confirm;
  speech.synth.speak(utterance);
}

function speakNow(speech: Speech, text: string, lang: string): void {
  const utterance = new speech.Utterance(text);
  utterance.lang = lang;
  speech.synth.speak(utterance);
  // A queue the OS paused (a call, Siri) accepts utterances but never speaks
  // them. resume() only acts on a current, paused utterance, so it is safe
  // to call every time, and only works after speak().
  speech.synth.resume();
}

/**
 * Speak `text` in `lang` (BCP 47). By default it queues behind anything
 * still being spoken; `interrupt` cuts that off first.
 */
export function speak(text: string, lang: string, { interrupt = false }: { interrupt?: boolean } = {}): void {
  const native = getNativeSpeech();
  if (native) {
    native.speak({ text, lang, interrupt }).catch(ignoreNativeFailure);
    return;
  }
  const speech = getSpeech();
  if (!speech) return;
  if (!interrupt) {
    speakNow(speech, text, lang);
    return;
  }

  if (pendingSpeak !== null) {
    clearTimeout(pendingSpeak);
    pendingSpeak = null;
  }
  const { synth } = speech;
  if (synth.speaking || synth.pending) {
    synth.cancel();
    pendingSpeak = setTimeout(() => {
      pendingSpeak = null;
      speakNow(speech, text, lang);
    }, CANCEL_SETTLE_MS);
    return;
  }
  speakNow(speech, text, lang);
}

/** Stop speaking now and drop anything queued. */
export function cancelSpeech(): void {
  if (pendingSpeak !== null) {
    clearTimeout(pendingSpeak);
    pendingSpeak = null;
  }
  const native = getNativeSpeech();
  if (native) {
    native.cancel().catch(ignoreNativeFailure);
    return;
  }
  getSpeech()?.synth.cancel();
}
