import './ProgressIndicator.css';

interface ProgressIndicatorProps {
  variant: 'inline' | 'dialog';
  title: string;
  detail?: string;
}

export function ProgressIndicator({ variant, title, detail }: ProgressIndicatorProps) {
  return (
    <div className={`progress-indicator progress-indicator--${variant}`} role="status" aria-live="polite">
      <span className="progress-indicator__spinner" aria-hidden="true" />
      <span className="progress-indicator__copy">
        <strong className="progress-indicator__title">{title}</strong>
        {detail ? <span className="progress-indicator__detail">{detail}</span> : null}
      </span>
    </div>
  );
}
