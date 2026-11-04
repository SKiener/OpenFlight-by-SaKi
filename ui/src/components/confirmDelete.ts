import { t } from '../i18n';

export type ConfirmPhase = 'idle' | 'confirming' | 'busy' | 'failed';

export const CONFIRM_MS = 4000;

export interface ConfirmDelete {
  tap(onConfirm: () => Promise<void> | void): void;
  dispose(): void;
}

export function createConfirmDelete(onPhase: (phase: ConfirmPhase) => void): ConfirmDelete {
  let phase: ConfirmPhase = 'idle';
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  const set = (next: ConfirmPhase): void => {
    if (disposed) return;
    phase = next;
    onPhase(next);
  };
  const holdThenReset = (next: ConfirmPhase): void => {
    clearTimeout(timer);
    set(next);
    timer = setTimeout(() => set('idle'), CONFIRM_MS);
  };

  return {
    tap(onConfirm) {
      if (disposed || phase === 'busy') return;
      if (phase !== 'confirming') {
        holdThenReset('confirming');
        return;
      }
      clearTimeout(timer);
      set('busy');
      new Promise<void>((resolve) => resolve(onConfirm())).then(
        () => set('idle'),
        () => holdThenReset('failed')
      );
    },
    dispose() {
      disposed = true;
      clearTimeout(timer);
    },
  };
}

/** The button text per phase; `texts` replaces the shot-delete wording (e.g. "Really reset?"). */
export function confirmDeleteText(
  phase: ConfirmPhase,
  label: string,
  texts: { confirm?: string; failed?: string } = {}
): string {
  if (phase === 'idle') return label;
  return phase === 'failed' ? (texts.failed ?? t('shots.deleteFailed')) : (texts.confirm ?? t('shots.confirmDelete'));
}
