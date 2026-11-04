import { useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n/useI18n';
import { confirmDeleteText, createConfirmDelete, type ConfirmDelete, type ConfirmPhase } from './confirmDelete';
import './ConfirmDeleteButton.css';

export interface ConfirmDeleteButtonProps {
  label: string;
  onConfirm: () => Promise<void> | void;
  disabled?: boolean;
  className?: string;
  ariaLabel?: string;
  /** Second-tap and failure texts; default to the shot-delete wording. */
  confirmLabel?: string;
  failedLabel?: string;
}

export function ConfirmDeleteButton({
  label,
  onConfirm,
  disabled = false,
  className,
  ariaLabel,
  confirmLabel,
  failedLabel,
}: ConfirmDeleteButtonProps) {
  useI18n();
  const [phase, setPhase] = useState<ConfirmPhase>('idle');
  const controlRef = useRef<ConfirmDelete | null>(null);

  useEffect(() => {
    const control = createConfirmDelete(setPhase);
    controlRef.current = control;
    return () => {
      control.dispose();
      controlRef.current = null;
    };
  }, []);

  const classes = ['confirm-delete', phase === 'idle' ? '' : `confirm-delete--${phase}`, className ?? '']
    .filter(Boolean)
    .join(' ');
  return (
    <button
      type="button"
      className={classes}
      disabled={disabled || phase === 'busy'}
      aria-label={phase === 'idle' ? ariaLabel : undefined}
      onClick={() => controlRef.current?.tap(onConfirm)}
    >
      {confirmDeleteText(phase, label, { confirm: confirmLabel, failed: failedLabel })}
    </button>
  );
}
