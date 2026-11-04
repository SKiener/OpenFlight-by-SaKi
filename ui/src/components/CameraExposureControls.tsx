import { useState } from 'react';
import { useI18n } from '../i18n/useI18n';
import type { CameraCaptureSettings } from '../stores/useCameraStore';

interface Props {
  settings: CameraCaptureSettings;
  onUpdate: (settings: Partial<CameraCaptureSettings>) => void;
}

export function CameraExposureControls({ settings, onUpdate }: Props) {
  const { t } = useI18n();
  const [draftExposure, setDraftExposure] = useState<number | null>(null);
  const [draftGain, setDraftGain] = useState<number | null>(null);
  const maxExposure = Math.min(1250, settings.max_exposure_us ?? 1250);
  const exposure = Math.max(50, Math.min(maxExposure, draftExposure ?? settings.exposure_us ?? 400));
  const gain = Math.max(1, Math.min(20, draftGain ?? settings.gain ?? 8));
  const enabled = settings.running === true && !settings.updating;
  const manual = (settings.auto_exposure?.enabled ?? settings.auto_exposure_enabled) === false;
  return (
    <div className="camera-settings__manual-controls">
      <strong>{t(manual ? 'camera.manualMode' : 'camera.automaticMode')}</strong>
      <label>
        <span>
          {t('camera.shutter')} <output>{exposure} µs</output>
        </span>
        <input
          type="range"
          aria-label={t('camera.shutter')}
          min={50}
          max={maxExposure}
          step={1}
          value={exposure}
          disabled={!enabled}
          onChange={(event) => setDraftExposure(Number(event.target.value))}
        />
      </label>
      <label>
        <span>
          {t('camera.gain')} <output>{gain.toFixed(1)}×</output>
        </span>
        <input
          type="range"
          aria-label={t('camera.gain')}
          min={1}
          max={20}
          step={0.5}
          value={gain}
          disabled={!enabled}
          onChange={(event) => setDraftGain(Number(event.target.value))}
        />
      </label>
      <button
        type="button"
        disabled={!enabled}
        onClick={() => onUpdate({ auto_exposure_enabled: false, exposure_us: exposure, gain })}
      >
        {t('camera.applyManual')}
      </button>
      <button
        type="button"
        disabled={!enabled}
        onClick={() => {
          setDraftExposure(null);
          setDraftGain(null);
          onUpdate({ auto_exposure_enabled: true });
        }}
      >
        {t('camera.recalibrate')}
      </button>
      <p className="camera-settings__note">{t('camera.manualNote')}</p>
      {exposure > 500 && (
        <p className="camera-settings__note camera-settings__blur-warning">{t('camera.manualBlurWarning')}</p>
      )}
    </div>
  );
}
