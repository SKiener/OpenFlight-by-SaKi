import { vi } from 'vitest';

/**
 * Test doubles for the browser APIs the voice callouts use. Vitest runs in
 * node here, so tests stub `window` (and `navigator`) by hand.
 */

export class FakeUtterance {
  text: string;
  lang = '';
  volume = 1;
  onstart: (() => void) | null = null;
  onend: (() => void) | null = null;

  constructor(text: string) {
    this.text = text;
  }
}

export class FakeSynth {
  speaking = false;
  pending = false;
  paused = false;
  spoken: FakeUtterance[] = [];
  /** Every call in order: 'speak:<text>', 'cancel', 'resume'. */
  log: string[] = [];

  speak(utterance: FakeUtterance) {
    this.spoken.push(utterance);
    this.log.push(`speak:${utterance.text}`);
  }

  cancel() {
    this.log.push('cancel');
    this.speaking = false;
    this.pending = false;
  }

  resume() {
    this.log.push('resume');
  }
}

/** Callouts said out loud (the silent unlock utterance is left out). */
export function spokenAloud(synth: FakeSynth): Array<{ text: string; lang: string }> {
  return synth.spoken.filter((utterance) => utterance.volume > 0).map(({ text, lang }) => ({ text, lang }));
}

export function fakeStorage(initial: Record<string, string> = {}) {
  const store: Record<string, string> = { ...initial };
  const localStorage = {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
  };
  return { store, localStorage };
}

interface FakeBrowserOptions {
  storage?: Record<string, string>;
  speech?: boolean;
  languages?: string[];
  /** A phone (or any other browser) on the Pi's network instead of the kiosk. */
  phone?: boolean;
  /** `window.openflightNative`, as inside the iOS companion app. */
  native?: unknown;
}

/** Where a paired phone loads the page from (the Pi's hotspot address). */
export const PHONE_PAGE_URL = 'http://10.42.0.1:8080/';

/**
 * Stub `window` (storage, location and, by default, speech) and `navigator`.
 * By default the location is the kiosk's own server: socketService reads it on import.
 */
export function installFakeBrowser({
  storage = {},
  speech = true,
  languages = ['en-US'],
  phone = false,
  native,
}: FakeBrowserOptions = {}) {
  const synth = new FakeSynth();
  const { store, localStorage } = fakeStorage(storage);
  vi.stubGlobal('window', {
    localStorage,
    location: new URL(phone ? PHONE_PAGE_URL : 'http://localhost:8080/'),
    ...(speech ? { speechSynthesis: synth, SpeechSynthesisUtterance: FakeUtterance } : {}),
    ...(native ? { openflightNative: native } : {}),
  });
  vi.stubGlobal('navigator', { languages, language: languages[0] });
  return { synth, store };
}

/** A browser with speech and empty storage; returns the synth to inspect. */
export function installFakeSpeech(): FakeSynth {
  return installFakeBrowser().synth;
}
