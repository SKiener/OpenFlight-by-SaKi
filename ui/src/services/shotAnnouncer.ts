import type { Shot } from '../types/shot';
import { useShotStore } from '../stores/useShotStore';
import { useUnitPreferenceStore } from '../stores/useUnitPreferenceStore';
import { useVoiceCalloutStore } from '../stores/useVoiceCalloutStore';
import { isKioskPage } from '../utils/kioskHost';
import { speak } from '../utils/speech';
import { buildShotCallout, getSpeechLang, testCalloutText, type CalloutConfig } from './shotCallout';

/**
 * Decides WHEN a shot is spoken. With the TI radar or the camera attached the
 * server sends a shot in steps: `shot` with provisional OPS values and
 * `pending` flags, sometimes a `shot_update` that only says enrichment was
 * skipped, then a `shot_update` with the final values (launch angle, and the
 * carry that depends on it). The callout waits for the final values so it
 * never contradicts the screen.
 *
 * Every shot is spoken. The hardware works on one shot at a time and the
 * server finalizes in detection order, so at a fast cadence several shots wait
 * at once; their callouts follow in shot order, queued by the speech engine.
 */

/**
 * Speak the provisional values if the final ones never arrive. The server
 * always finalizes within its 20 s enrichment deadline
 * (`_SHOT_ENRICHMENT_DEADLINE_S` in server.py); this only covers a lost event.
 */
export const FINAL_VALUES_TIMEOUT_MS = 22_000;

/** Recently spoken shots, so a repeated event never speaks a shot twice. */
const SPOKEN_MEMORY = 16;

type PendingFlags = Record<string, boolean | undefined> | undefined;

export interface AnnouncerShotMessage {
  shot: Shot;
  pending?: PendingFlags;
  /** Present when the server skipped hardware enrichment; the final values still follow. */
  enrichment?: { status: string };
}

export interface ShotAnnouncerDeps {
  speak: (text: string, lang: string) => void;
  /** Read on every callout, so menu changes apply to the next shot. */
  getConfig: () => CalloutConfig & { enabled: boolean };
}

function hasPending(pending: PendingFlags): boolean {
  return pending !== undefined && Object.values(pending).some(Boolean);
}

/**
 * Whether a shot from `session_state` already has its final values. For a
 * live shot, `_finalize_shot_detected` in server.py is the only place that
 * writes `carry_spin_adjusted`, and it always writes it.
 */
function isFinalized(shot: Shot): boolean {
  return shot.carry_spin_adjusted !== null && shot.carry_spin_adjusted !== undefined;
}

export class ShotAnnouncer {
  /**
   * Shots waiting for their final values, oldest first. No cap: the server
   * finalizes every shot, and each entry expires after its own timer anyway.
   */
  private readonly waiting = new Map<string, { shot: Shot; timer: ReturnType<typeof setTimeout> }>();
  private readonly spoken: string[] = [];
  private readonly deps: ShotAnnouncerDeps;

  constructor(deps: ShotAnnouncerDeps) {
    this.deps = deps;
  }

  /** A new shot (`shot` event). */
  handleShot({ shot, pending }: AnnouncerShotMessage): void {
    if (!this.deps.getConfig().enabled) return;
    if (this.waiting.has(shot.timestamp) || this.spoken.includes(shot.timestamp)) return;

    if (hasPending(pending)) {
      this.wait(shot);
      return;
    }
    this.announce(shot);
  }

  /** Late values for a shot (`shot_update` event). */
  handleShotUpdate({ shot, pending, enrichment }: AnnouncerShotMessage): void {
    const entry = this.waiting.get(shot.timestamp);
    if (!entry) return;

    if (hasPending(pending) || enrichment?.status === 'skipped') {
      entry.shot = shot;
      return;
    }
    this.drop(shot.timestamp);
    this.announce(shot);
  }

  /**
   * Line the waiting shots up with the server's list (`session_state` after a
   * reconnect or a deleted shot, `session_cleared`): drop the ones that are
   * gone, speak the ones whose final values arrived while the socket was down.
   */
  sync(shots: readonly Shot[]): void {
    const current = new Map(shots.map((shot) => [shot.timestamp, shot]));
    for (const [timestamp, entry] of [...this.waiting]) {
      const shot = current.get(timestamp);
      if (!shot) {
        this.drop(timestamp);
      } else if (isFinalized(shot)) {
        this.drop(timestamp);
        this.announce(shot);
      } else {
        entry.shot = shot;
      }
    }
  }

  /** Forget every waiting shot (the page went to the background). */
  reset(): void {
    for (const timestamp of [...this.waiting.keys()]) this.drop(timestamp);
  }

  private wait(shot: Shot): void {
    const timer = setTimeout(() => {
      const entry = this.waiting.get(shot.timestamp);
      this.waiting.delete(shot.timestamp);
      if (entry) this.announce(entry.shot);
    }, FINAL_VALUES_TIMEOUT_MS);
    this.waiting.set(shot.timestamp, { shot, timer });
  }

  private drop(timestamp: string): void {
    const entry = this.waiting.get(timestamp);
    if (entry) clearTimeout(entry.timer);
    this.waiting.delete(timestamp);
  }

  private announce(shot: Shot): void {
    if (this.spoken.includes(shot.timestamp)) return;
    const config = this.deps.getConfig();
    if (!config.enabled) return;

    const text = buildShotCallout(shot, config);
    if (text === null) return;
    this.spoken.push(shot.timestamp);
    if (this.spoken.length > SPOKEN_MEMORY) this.spoken.shift();
    this.deps.speak(text, getSpeechLang(config.language));
  }
}

/**
 * Whether this screen speaks. The setting is shared by every screen, but the
 * Pi has no speaker: its kiosk never speaks, the phones (and any other
 * browser) do.
 */
export function speaksOnThisDevice(): boolean {
  return !isKioskPage();
}

/** The setting as this screen applies it: never enabled on the kiosk. */
export function currentCalloutConfig(): CalloutConfig & { enabled: boolean } {
  const { enabled, metrics, language } = useVoiceCalloutStore.getState();
  return {
    enabled: enabled && speaksOnThisDevice(),
    metrics,
    language,
    unitSystem: useUnitPreferenceStore.getState().unitSystem,
  };
}

/**
 * The menu's test callout, spoken on this screen and cutting off anything
 * still speaking. Does nothing on the kiosk or while callouts are off.
 */
export function speakTestCallout(): void {
  const config = currentCalloutConfig();
  if (!config.enabled) return;
  const text = testCalloutText(useShotStore.getState().latestShot, config);
  if (text !== null) speak(text, getSpeechLang(config.language), { interrupt: true });
}

export const shotAnnouncer = new ShotAnnouncer({ speak, getConfig: currentCalloutConfig });
