import type { ReactNode } from 'react';
import type { SpinQuality } from '../../types/shot';
import { t } from '../../i18n';
import { useI18n } from '../../i18n/useI18n';
import './MetricCard.css';

export interface MetricCardProps {
  value: string | number;
  unit?: string;
  label: string;
  subtext?: string;
  variant?: 'default' | 'emphasis';
  size?: 'standard' | 'hero';
  /**
   * Instrument-panel layouts (design doc 6a / 7a) lead with the label and put
   * the number underneath; the original card leads with the number.
   */
  labelPosition?: 'below' | 'above';
  /** Modeled value. Shown as an ≈ mark; measured values have no mark. */
  estimated?: boolean;
  /**
   * The ≈ mark's label; defaults to "estimated" (screen readers and tooltip only).
   * An override (e.g. "calculated" for a camera club path) stays hidden too: the
   * title row does not wrap, so a visible word belongs in the subtext instead.
   */
  estimatedLabel?: string;
  confidence?: SpinQuality | null;
  /** Override confidence copy while preserving its dot level. */
  confidenceLabel?: string;
  /** Renders the card as a button. Used by 6a's tap-a-tile-to-promote-it grid. */
  onClick?: () => void;
  /** Marks an interactive card as the currently promoted one (`aria-pressed`). */
  selected?: boolean;
  /** Metric id as `data-metric`, so a layout can style one tile (the phone's implement name). */
  metricId?: string;
  /** Tooltip for details a short tile has no room for (the carry's typical spin, the missing curve). */
  title?: string;
  /** 'class': the value is a word (Draw, Gerade), sized in its own fit group. */
  kind?: 'class';
  /** A picture over the value (the impact face over its zone word); label-above cards only. */
  graphic?: ReactNode;
}

export function EstimatedMark({ label }: { label?: string } = {}) {
  const text = label ?? t('metric.estimated');
  return (
    <span className="metric-card__estimated" title={text}>
      <svg viewBox="0 0 16 10" aria-hidden="true">
        <path d="M1 3.1c2.2-1.6 4.4 1.6 6.6 0s4.4-1.6 6.6 0" />
        <path d="M1 7.4c2.2-1.6 4.4 1.6 6.6 0s4.4-1.6 6.6 0" />
      </svg>
      <span className="metric-card__estimated-label">{text}</span>
    </span>
  );
}

export function MetricCard({
  value,
  unit,
  label,
  subtext,
  variant = 'default',
  size = 'standard',
  labelPosition = 'below',
  confidence,
  confidenceLabel,
  onClick,
  selected,
  estimated,
  estimatedLabel,
  metricId,
  title,
  kind,
  graphic,
}: MetricCardProps) {
  useI18n();
  const classes = ['metric-card', `metric-card--${variant}`, `metric-card--label-${labelPosition}`];
  if (size === 'hero') {
    classes.push('metric-card--hero');
  }
  if (onClick) {
    classes.push('metric-card--interactive');
  }
  if (selected) {
    classes.push('metric-card--selected');
  }
  if (kind === 'class') {
    classes.push('metric-card--class');
  }
  const withGraphic = graphic != null && labelPosition === 'above';
  if (withGraphic) {
    classes.push('metric-card--graphic');
  }

  const label_ = (
    <span className="metric-card__label">
      {label}
      {estimated ? <EstimatedMark label={estimatedLabel} /> : null}
    </span>
  );
  const meta = (
    <>
      {subtext ? <span className="metric-card__subtext metric-card__confidence-label">{subtext}</span> : null}
      {confidence ? (
        <div className={`metric-card__confidence metric-card__confidence--${confidence}`}>
          {confidence !== 'experimental' ? (
            <span className="metric-card__confidence-dots">
              <span className="dot filled" />
              <span className={`dot ${confidence === 'medium' || confidence === 'high' ? 'filled' : ''}`} />
              <span className={`dot ${confidence === 'high' ? 'filled' : ''}`} />
            </span>
          ) : null}
          <span className="metric-card__confidence-label">{confidenceLabel ?? t(`quality.${confidence}`)}</span>
        </div>
      ) : null}
    </>
  );
  const body = (
    <>
      {labelPosition === 'above' ? label_ : null}
      {withGraphic ? <div className="metric-card__graphic">{graphic}</div> : null}
      <div className="metric-card__value-row">
        <span className="metric-card__value">{value}</span>
        {unit ? <span className="metric-card__unit">{unit}</span> : null}
      </div>
      {labelPosition === 'below' ? label_ : null}
      {labelPosition === 'above' ? <div className="metric-card__meta">{meta}</div> : meta}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        className={classes.join(' ')}
        data-metric={metricId}
        title={title}
        aria-pressed={selected ?? false}
        onClick={onClick}
      >
        {body}
      </button>
    );
  }

  return (
    <div className={classes.join(' ')} data-metric={metricId} title={title}>
      {body}
    </div>
  );
}
