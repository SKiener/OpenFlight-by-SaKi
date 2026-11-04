import { useEffect, useState, type CSSProperties } from 'react';
import type { CameraCaptureSettings } from '../stores/useCameraStore';
import { cssFrameAspect, frameAspect } from '../utils/frameAspect';
import { useI18n } from '../i18n/useI18n';
import { BALL_GUIDE_CROSS_VARS, CAMERA_PREVIEW_URL } from '../utils/ballGuide';
import { BallGuideHairlines } from './BallGuideHairlines';
import { PanelAction } from './panel/PanelAction';
import { setupMarkStates, startPreviewPolling, type SetupPreview } from './setupReminderState';
import './CameraFeed.css';
import './SetupReminder.css';

interface SetupReminderProps {
  captureSettings: CameraCaptureSettings;
  /** Close it and open the Align view (the club picker under it closes too). */
  onAlign: () => void;
  /** Close it; the club picker under it stays. The header's close button does the same. */
  onDone: () => void;
}

/**
 * The kiosk's setup window at start, above the club picker: the live camera picture with the Align view's
 * crosshair and the saved ball mark, what to do before the first shot, and whether ball and target are marked.
 */
export function SetupReminder({ captureSettings, onAlign, onDone }: SetupReminderProps) {
  const { t } = useI18n();
  const [preview, setPreview] = useState<SetupPreview>({ state: 'loading' });
  const [previewSize, setPreviewSize] = useState<[number, number] | null>(null);
  const aspect = cssFrameAspect(frameAspect(previewSize, [captureSettings.width, captureSettings.height]));
  const ball = captureSettings.ball_mark ?? null;
  const [ballState, targetState] = setupMarkStates(captureSettings);
  const title = t('setup.title');
  const align = t('range.align');

  useEffect(() => startPreviewPolling({ url: CAMERA_PREVIEW_URL, onChange: setPreview }), []);

  return (
    <div className="setup-reminder" role="dialog" aria-modal="true" aria-label={title}>
      <div className="setup-reminder__header">
        <div className="setup-reminder__heading">
          <span className="setup-reminder__eyebrow">{t('setup.eyebrow')}</span>
          <h2 className="setup-reminder__title">{title}</h2>
        </div>
        <button
          type="button"
          className="setup-reminder__close"
          onClick={onDone}
          aria-label={t('picker.close', { title })}
        >
          ✕
        </button>
      </div>
      <div className="setup-reminder__body">
        <div className="setup-reminder__picture">
          <div
            className="setup-reminder__frame"
            style={{ ...BALL_GUIDE_CROSS_VARS, '--setup-aspect': aspect } as CSSProperties}
          >
            {preview.state === 'available' ? (
              <>
                <img
                  src={preview.src}
                  alt={t('camera.preview')}
                  className="setup-reminder__image"
                  onLoad={(event) =>
                    setPreviewSize([event.currentTarget.naturalWidth, event.currentTarget.naturalHeight])
                  }
                />
                <BallGuideHairlines />
                <div className="camera-feed__center-cross" aria-hidden="true" />
                {ball && (
                  <div
                    className="camera-feed__mark camera-feed__mark--ball"
                    style={{ left: `${ball.preview_x_pct}%`, top: `${ball.preview_y_pct}%` }}
                    aria-hidden="true"
                  />
                )}
              </>
            ) : (
              <p className="setup-reminder__placeholder" role="status">
                {t(preview.state === 'loading' ? 'setup.loading' : 'setup.noPicture')}
              </p>
            )}
          </div>
        </div>
        <div className="setup-reminder__side">
          <ol className="setup-reminder__steps">
            <li>{t('setup.stepBall')}</li>
            <li>{t('setup.stepCamera')}</li>
            <li>{t('setup.stepMarks', { align })}</li>
          </ol>
          <p className="setup-reminder__marks">
            {t('setup.ballMark', { state: t(ballState) })} · {t('setup.targetMark', { state: t(targetState) })}
          </p>
          <div className="setup-reminder__actions">
            <PanelAction variant="secondary" onClick={onAlign}>
              {align}
            </PanelAction>
            <PanelAction variant="primary" onClick={onDone}>
              {t('setup.done')}
            </PanelAction>
          </div>
        </div>
      </div>
    </div>
  );
}
