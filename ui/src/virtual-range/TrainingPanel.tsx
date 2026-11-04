import type { ReactElement } from 'react';
import { getHtmlLang } from '../i18n';
import { useI18n } from '../i18n/useI18n';
import { getDistanceUnit, type UnitSystem } from '../utils/units';
import { distanceLabel, distanceValue } from './distance';
import { runningFigures, type TrainingState } from './game/training';

export interface TrainingPanelProps {
  targetM: number;
  shown: TrainingState;
  unitSystem: UnitSystem;
  onStep(deltaM: number): void;
  onEnd(): void;
}

const STEPS = [
  { deltaM: -10, text: '−10' },
  { deltaM: -1, text: '−' },
  { deltaM: 1, text: '+' },
  { deltaM: 10, text: '+10' },
] as const;

export function TrainingPanel({ targetM, shown, unitSystem, onStep, onEnd }: TrainingPanelProps): ReactElement {
  const { t } = useI18n();
  const unit = getDistanceUnit(unitSystem);
  const lengthOff = (deviationM: number): string => {
    const distance = distanceLabel(Math.abs(deviationM), unitSystem, 1);
    if (distance === distanceLabel(0, unitSystem, 1)) return distance;
    return t(deviationM < 0 ? 'virtualRange.short' : 'virtualRange.long', { distance });
  };
  const last = shown.recent.at(-1);
  const { meanDeviationM, shareInside } = runningFigures(shown);
  const share =
    shareInside === null
      ? null
      : new Intl.NumberFormat(getHtmlLang(), { style: 'percent', maximumFractionDigits: 0 }).format(shareInside);
  const stepButton = ({ deltaM, text }: (typeof STEPS)[number]): ReactElement => (
    <button
      key={deltaM}
      type="button"
      className="virtual-range__training-step"
      aria-label={t(deltaM < 0 ? 'virtualRange.trainingCloser' : 'virtualRange.trainingFarther', {
        step: `${Math.abs(deltaM)} m`,
      })}
      onClick={() => onStep(deltaM)}
    >
      {text}
    </button>
  );
  return (
    <div className="virtual-range__training">
      <div className="virtual-range__training-target">
        {STEPS.slice(0, 2).map(stepButton)}
        <span className="virtual-range__training-goal">
          <span className="virtual-range__label">{t('virtualRange.trainingTarget')}</span>
          <span className="virtual-range__training-value">
            {distanceValue(targetM, unitSystem)}
            <small>{unit}</small>
          </span>
        </span>
        {STEPS.slice(2).map(stepButton)}
      </div>
      <p className="virtual-range__training-result" aria-live="polite">
        <strong>
          {last
            ? `${lengthOff(last.deviationM)} · ${t(`virtualRange.zone.${last.zone}`)}`
            : t('virtualRange.trainingReady')}
        </strong>
        {meanDeviationM === null || share === null ? null : (
          <span>
            {`${t('virtualRange.trainingMean', { deviation: lengthOff(meanDeviationM) })} · ${t(
              'virtualRange.trainingInside',
              { share }
            )}`}
          </span>
        )}
      </p>
      <button type="button" className="virtual-range__training-end" onClick={onEnd}>
        {t('virtualRange.trainingEnd')}
      </button>
    </div>
  );
}
