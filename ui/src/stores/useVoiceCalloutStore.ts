import { create } from 'zustand';
import {
  CALLOUT_METRICS,
  DEFAULT_CALLOUT_METRICS,
  isCalloutLanguage,
  isCalloutMetricId,
  pickCalloutLanguage,
  type CalloutLanguage,
  type CalloutMetricId,
} from '../services/shotCallout';

/**
 * Spoken shot callouts. One setting for the whole session, held by the server
 * (src/openflight/voice_callouts.py): whichever screen switches them on, the
 * phones speak and the kiosk on the Pi (no speaker) stays silent. The earlier
 * per-device copy in localStorage (`openflight.voice-callouts:v1`) is ignored,
 * so an old phone setting never switches the callouts on by itself.
 */

export interface VoiceCalloutSettings {
  enabled: boolean;
  /** Values to speak, in {@link CALLOUT_METRICS} order. Never empty. */
  metrics: CalloutMetricId[];
  language: CalloutLanguage;
}

/** A change sent to the server (`set_voice_callouts`): only the fields that changed. */
export type VoiceCalloutPatch = Partial<VoiceCalloutSettings>;

function browserLanguages(): readonly string[] {
  if (typeof navigator === 'undefined') return [];
  if (Array.isArray(navigator.languages) && navigator.languages.length > 0) return navigator.languages;
  return typeof navigator.language === 'string' ? [navigator.language] : [];
}

export function defaultVoiceCalloutSettings(): VoiceCalloutSettings {
  return {
    enabled: false,
    metrics: [...DEFAULT_CALLOUT_METRICS],
    language: pickCalloutLanguage(browserLanguages()),
  };
}

/** Keep known ids, in canonical order, without duplicates. */
function normalizeMetrics(values: readonly unknown[]): CalloutMetricId[] {
  const chosen = new Set(values.filter(isCalloutMetricId));
  return CALLOUT_METRICS.filter((metric) => chosen.has(metric));
}

/**
 * Read the server's `voice_callouts` payload; anything missing or malformed
 * falls back field by field. The server keeps `language` null until someone
 * picks one, and then every device speaks in its own browser language.
 */
export function parseVoiceCalloutSettings(value: unknown): VoiceCalloutSettings {
  const defaults = defaultVoiceCalloutSettings();
  if (typeof value !== 'object' || value === null) return defaults;

  const { enabled, metrics, language } = value as Record<string, unknown>;
  const storedMetrics = Array.isArray(metrics) ? normalizeMetrics(metrics) : [];
  return {
    enabled: typeof enabled === 'boolean' ? enabled : defaults.enabled,
    metrics: storedMetrics.length > 0 ? storedMetrics : defaults.metrics,
    language: isCalloutLanguage(language) ? language : defaults.language,
  };
}

type Send = (patch: VoiceCalloutPatch) => void;

let send: Send | null = null;

/**
 * Where changes go: socketService registers its `set_voice_callouts` emit
 * (it imports the announcer, which reads this store, so it cannot be imported
 * here). Without one a change still applies locally.
 */
export function setVoiceCalloutSender(sender: Send | null): void {
  send = sender;
}

interface VoiceCalloutState extends VoiceCalloutSettings {
  /** The server's setting has arrived; until then this is the default (off). */
  synced: boolean;
  /** The server's `voice_callouts` broadcast: replaces the local values. */
  applyServerSettings: (value: unknown) => void;
  setEnabled: (enabled: boolean) => void;
  /** Add or remove a value. The last remaining value cannot be removed. */
  toggleMetric: (metric: CalloutMetricId) => void;
  setLanguage: (language: CalloutLanguage) => void;
}

export const useVoiceCalloutStore = create<VoiceCalloutState>((set, get) => {
  // Shown at once; the server's broadcast follows and is the final word.
  const update = (patch: VoiceCalloutPatch) => {
    set(patch);
    send?.(patch);
  };

  return {
    ...defaultVoiceCalloutSettings(),
    synced: false,
    applyServerSettings: (value) => set({ ...parseVoiceCalloutSettings(value), synced: true }),
    setEnabled: (enabled) => update({ enabled }),
    toggleMetric: (metric) => {
      const current = get().metrics;
      const next = current.includes(metric)
        ? current.filter((existing) => existing !== metric)
        : normalizeMetrics([...current, metric]);
      if (next.length === 0) return;
      update({ metrics: next });
    },
    setLanguage: (language) => update({ language }),
  };
});
