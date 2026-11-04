import { useI18n } from '../i18n/useI18n';
import type { CameraCaptureSettings } from '../stores/useCameraStore';

interface Props {
  settings: CameraCaptureSettings;
  onUpdate: (settings: Partial<CameraCaptureSettings>) => void;
}

export function CameraModeControls({ settings, onUpdate }: Props) {
  const { t } = useI18n();
  const modes = settings.capture_modes ?? [];
  if (!modes.length) return <p className="camera-settings__note">{t('camera.modesUnavailable')}</p>;
  return (
    <div className="camera-settings__mode-controls">
      <div role="group" aria-label={t('camera.modeSelection')}>
        {modes.map((mode) => (
          <button
            type="button"
            key={mode.id}
            aria-pressed={settings.capture_mode === mode.id}
            disabled={!settings.running || settings.updating}
            onClick={() => onUpdate({ capture_mode: mode.id })}
          >
            <strong>
              {mode.width} × {mode.height}
            </strong>
            <span>{mode.fps} fps</span>
          </button>
        ))}
      </div>
      {settings.updating && <p role="status">{t('camera.modeChanging')}</p>}
      <p className="camera-settings__note">{t('camera.modeNote')}</p>
    </div>
  );
}
