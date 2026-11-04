import { useEffect, useRef, useState, type CSSProperties } from 'react';
import { useDragScroll } from '../hooks/useDragScroll';
import type { CameraAutoExposureStatus, CameraCaptureSettings, CameraSettingsUpdate } from '../stores/useCameraStore';
import { verticalViewTargets } from '../utils/cameraView';
import { getServerOrigin } from '../utils/serverOrigin';
import { BALL_GUIDE_CROSS_VARS, CAMERA_PREVIEW_URL } from '../utils/ballGuide';
import { BallGuideHairlines } from './BallGuideHairlines';
import { cssFrameAspect, frameAspect } from '../utils/frameAspect';
import { ALIGN_CROP, FULL_FRAME, cropSceneBox } from '../utils/alignCrop';
import { alignNote, alignTap, canTapMark, FIRST_ALIGN_STEP, type AlignStep } from '../utils/targetLine';
import { formatNumber } from '../utils/units';
import { reportsFlatFrame } from '../utils/flatFrame';
import { useI18n } from '../i18n/useI18n';
import { getHtmlLang, type MessageKey } from '../i18n';
import { CameraExposureControls } from './CameraExposureControls';
import { CameraModeControls } from './CameraModeControls';
import { withLabelBreaks } from './panel/labelBreaks';
import './CameraFeed.css';

interface CameraFeedProps {
  captureSettings: CameraCaptureSettings;
  captureSettingsError: string | null;
  onUpdateCaptureSettings: (settings: CameraSettingsUpdate) => void;
}

interface CaptureSettingsPanelProps {
  settings: CameraCaptureSettings;
  exposureQuality: ExposureQuality | null;
  error: string | null;
  onUpdate: (settings: CameraSettingsUpdate) => void;
  step: AlignStep;
  onSelectStep: (step: AlignStep) => void;
  onSetAnew: (step: AlignStep) => void;
  cropped: boolean;
  onToggleCrop: () => void;
}

const EXPOSURE_QUALITY_URL = `${getServerOrigin()}/api/camera/exposure-quality`;
const PREVIEW_REFRESH_MS = 5000;

type PreviewState = 'checking' | 'available' | 'unavailable';

interface ExposureQuality {
  sample_available: boolean;
  status: 'good' | 'too_dark' | 'too_bright' | 'marginal' | 'flat' | 'unavailable';
  recommendation?: 'brighter' | 'darker' | 'hold';
  message?: string;
  clipped_pct?: number;
  contrast?: number;
  auto_exposure?: CameraAutoExposureStatus;
  /** The live frame or the exposure lock shows no picture (one grey level). */
  frame_flat?: boolean;
}

/**
 * The active Align step. The view always opens on the ball, also with stored marks. A tap sets or
 * moves the active step's mark and keeps the step, so a second tap corrects the same mark; only
 * the step chips and "set anew" change the step. (Moving on to the target after each ball tap sent
 * the corrections meant for the ball to the target: range session 2026-11-02.)
 */
function useAlignStep(): [AlignStep, (step: AlignStep) => void] {
  return useState<AlignStep>(FIRST_ALIGN_STEP);
}

function AlignMarksSection({
  settings,
  step,
  onSelectStep,
  onSetAnew,
  cropped,
  onToggleCrop,
}: Pick<CaptureSettingsPanelProps, 'settings' | 'step' | 'onSelectStep' | 'onSetAnew' | 'cropped' | 'onToggleCrop'>) {
  const { t } = useI18n();
  const ball = settings.ball_mark ?? null;
  const target = settings.target_line ?? null;
  return (
    <section className="camera-settings__section camera-settings__section--target">
      <div className="camera-settings__section-title">
        <span>{t('camera.target')}</span>
      </div>
      <AlignSteps
        step={step}
        onSelect={onSelectStep}
        cropped={cropped}
        onToggleCrop={onToggleCrop}
        placement="settings"
      />
      <dl className="camera-settings__marks">
        <div>
          <dt>{t('camera.markBall')}</dt>
          <dd>{t(ball ? 'camera.markSet' : 'camera.targetNone')}</dd>
        </div>
        <div>
          <dt>{t('camera.markTarget')}</dt>
          <dd>{t(target ? 'camera.markSet' : 'camera.targetNone')}</dd>
        </div>
      </dl>
      {target && (
        <div className="camera-settings__target-direction">
          {t(target.azimuth_deg >= 0 ? 'camera.targetRight' : 'camera.targetLeft', {
            angle: formatNumber(Math.abs(target.azimuth_deg), 1),
          })}
        </div>
      )}
      <p className="camera-settings__note">{t(alignNote(settings, step), { step: t('camera.stepTarget') })}</p>
      <div className="camera-settings__mark-actions">
        <button
          type="button"
          className="camera-settings__target-reset"
          disabled={settings.updating}
          onClick={() => onSetAnew('ball')}
        >
          {t('camera.ballReset')}
        </button>
        <button
          type="button"
          className="camera-settings__target-reset"
          disabled={settings.updating}
          onClick={() => onSetAnew('target')}
        >
          {t('camera.targetReset')}
        </button>
      </div>
    </section>
  );
}

/**
 * The two step chips, the active one the mark a tap sets, and beside them the button that switches
 * the picture between the close-up and the whole frame; it names the view it switches to. They sit
 * above the picture on the kiosk and at the top of the marks section on phones (CameraFeed.css),
 * where a row above the picture would take a third of its height in landscape; CSS shows one of the two.
 */
function AlignSteps({
  step,
  onSelect,
  cropped,
  onToggleCrop,
  placement,
}: {
  step: AlignStep;
  onSelect: (step: AlignStep) => void;
  cropped: boolean;
  onToggleCrop: () => void;
  placement: 'preview' | 'settings';
}) {
  const { t } = useI18n();
  const steps: [AlignStep, MessageKey][] = [
    ['ball', 'camera.stepBall'],
    ['target', 'camera.stepTarget'],
  ];
  return (
    <div className={`camera-feed__steps camera-feed__steps--${placement}`}>
      <div className="camera-feed__step-chips" role="group" aria-label={t('camera.alignSteps')}>
        {steps.map(([id, label]) => (
          <button
            key={id}
            type="button"
            className={`camera-feed__step${step === id ? ' camera-feed__step--active' : ''}`}
            aria-pressed={step === id}
            onClick={() => onSelect(id)}
          >
            {t(label)}
          </button>
        ))}
      </div>
      {/* data-spare holds the other label, laid in the same place unseen (CSS), so the button is as
       * wide as the longer of the two and a tap on it moves neither it nor the chips. */}
      <button
        type="button"
        className="camera-feed__view-toggle"
        data-spare={t(cropped ? 'camera.viewCrop' : 'camera.viewFull')}
        onClick={onToggleCrop}
      >
        <span className="camera-feed__view-toggle-label">{t(cropped ? 'camera.viewFull' : 'camera.viewCrop')}</span>
      </button>
    </div>
  );
}

const AUTO_EXPOSURE_LABELS: Record<CameraAutoExposureStatus['status'], MessageKey> = {
  ready: 'camera.ready',
  adjusting: 'camera.adjusting',
  lighting_required: 'camera.lightingRequired',
  no_image: 'camera.noImage',
  unavailable: 'camera.waiting',
};

/** Label for an auto-exposure status; a status this UI does not know yet falls back to "waiting". */
function autoExposureLabel(status: string | undefined): MessageKey {
  return AUTO_EXPOSURE_LABELS[status as CameraAutoExposureStatus['status']] ?? 'camera.waiting';
}

function CaptureSettingsPanel({
  settings,
  exposureQuality,
  error,
  onUpdate,
  step,
  onSelectStep,
  onSetAnew,
  cropped,
  onToggleCrop,
}: CaptureSettingsPanelProps) {
  const { locale, t } = useI18n();
  const scrollRef = useRef<HTMLElement>(null);
  const dragScroll = useDragScroll(scrollRef);
  const verticalOffset = settings.vertical_offset_px ?? 0;
  const verticalMin = settings.vertical_offset_min_px ?? verticalOffset;
  const verticalMax = settings.vertical_offset_max_px ?? verticalOffset;
  const verticalStep = settings.vertical_offset_step_px ?? 10;
  const viewTargets = verticalViewTargets(verticalOffset, verticalStep, settings.rotate_180 ?? false);
  const viewUpOffset = viewTargets.up;
  const viewDownOffset = viewTargets.down;
  const autoExposure = exposureQuality?.auto_exposure ?? settings.auto_exposure;
  const exposureObservation = autoExposure?.observation;
  const flatFrame = reportsFlatFrame(exposureQuality, settings);
  const exposureStatus = flatFrame
    ? t('camera.noImage')
    : autoExposure?.capture_deferred
      ? t('camera.capturing')
      : t(autoExposureLabel(autoExposure?.status));

  return (
    <aside className="camera-settings" ref={scrollRef} {...dragScroll} aria-label={t('camera.setup')}>
      <div className="camera-settings__heading">
        <div>
          <span className="camera-settings__eyebrow">{t('camera.controls')}</span>
          <h3>{t('camera.setup')}</h3>
        </div>
        <span className={`camera-settings__armed ${settings.armed ? 'camera-settings__armed--ready' : ''}`}>
          {settings.armed ? t('camera.armed') : settings.running ? t('camera.filling') : t('camera.offline')}
        </span>
      </div>

      <div className="camera-settings__controls">
        <AlignMarksSection
          settings={settings}
          step={step}
          onSelectStep={onSelectStep}
          onSetAnew={onSetAnew}
          cropped={cropped}
          onToggleCrop={onToggleCrop}
        />

        <section className="camera-settings__section">
          <div className="camera-settings__section-title">
            <span>{t('camera.sensorView')}</span>
            <small>{settings.width && settings.height ? `${settings.width} × ${settings.height}` : '—'}</small>
          </div>
          <CameraModeControls settings={settings} onUpdate={onUpdate} />
          {settings.raw_crop_adjustable && (
            <div className="camera-settings__view-controls">
              <button
                type="button"
                disabled={
                  settings.updating ||
                  !settings.running ||
                  !settings.raw_crop_adjustable ||
                  viewUpOffset < verticalMin ||
                  viewUpOffset > verticalMax
                }
                onClick={() => onUpdate({ vertical_offset_px: viewUpOffset })}
              >
                {t('camera.viewUp')}
              </button>
              <output>{`${verticalOffset > 0 ? '+' : ''}${verticalOffset} px`}</output>
              <button
                type="button"
                disabled={
                  settings.updating ||
                  !settings.running ||
                  !settings.raw_crop_adjustable ||
                  viewDownOffset < verticalMin ||
                  viewDownOffset > verticalMax
                }
                onClick={() => onUpdate({ vertical_offset_px: viewDownOffset })}
              >
                {t('camera.viewDown')}
              </button>
            </div>
          )}
          <p className="camera-settings__note">
            {t(settings.raw_crop_adjustable ? 'camera.moveNote' : 'camera.fixedViewNote')}
          </p>
        </section>

        <section className="camera-settings__section">
          <div className="camera-settings__section-title">
            <span>{t('camera.exposureControls')}</span>
            <small>{t('camera.startupCalibration')}</small>
          </div>
          <CameraExposureControls settings={settings} onUpdate={onUpdate} />
          <div
            className={`camera-settings__auto-status camera-settings__auto-status--${
              flatFrame ? 'no_image' : (autoExposure?.status ?? 'unavailable')
            }`}
          >
            <div>
              <strong>{exposureStatus}</strong>
              <span title={autoExposure?.message}>
                {locale === 'de' ? exposureStatus : (autoExposure?.message ?? t('camera.firstCheck'))}
              </span>
            </div>
            <span className="camera-settings__auto-dot" aria-hidden="true" />
          </div>
          <dl className="camera-settings__exposure-metrics">
            <div>
              <dt>{t('camera.shutter')}</dt>
              <dd>{autoExposure?.exposure_us ?? settings.exposure_us ?? '—'} µs</dd>
            </div>
            <div>
              <dt>{t('camera.gain')}</dt>
              <dd>{autoExposure?.gain ?? settings.gain ?? '—'}×</dd>
            </div>
            <div>
              <dt>{t('camera.median')}</dt>
              <dd>{exposureObservation?.median ?? '—'}</dd>
            </div>
            <div>
              <dt>{t('camera.contrast')}</dt>
              <dd>{exposureObservation?.contrast ?? exposureQuality?.contrast ?? '—'}</dd>
            </div>
          </dl>
          <div
            className={`camera-settings__analysis-state ${autoExposure?.analysis_eligible ? 'camera-settings__analysis-state--ready' : ''}`}
          >
            <strong>{autoExposure?.analysis_eligible ? t('camera.analysisActive') : t('camera.radarFallback')}</strong>
            <span>{autoExposure?.analysis_eligible ? t('camera.analysisDetail') : t('camera.fallbackDetail')}</span>
          </div>
          <p className="camera-settings__note">
            {t('camera.blurRisk')}:{' '}
            <strong>
              {autoExposure?.motion_blur_risk === 'low'
                ? t('quality.low')
                : autoExposure?.motion_blur_risk === 'high'
                  ? t('quality.high')
                  : autoExposure?.motion_blur_risk === 'elevated'
                    ? t('quality.elevated')
                    : t('camera.checking')}
            </strong>
            . {t('camera.lightNote')}
          </p>
        </section>

        <section className="camera-settings__section">
          <div className="camera-settings__section-title">
            <span>{t('camera.guide')}</span>
            <small>{t('camera.fixedGuide')}</small>
          </div>
          <div className="camera-settings__ball-guide-position">{t('camera.guidePosition')}</div>
          <p className="camera-settings__note">{t('camera.guideNote')}</p>
        </section>

        <section className="camera-settings__section camera-settings__section--summary">
          <div className="camera-settings__section-title">
            <span>{t('camera.capture')}</span>
            <small>{t('camera.sessionSetting')}</small>
          </div>
          <dl className="camera-settings__summary">
            <div>
              <dt>{t('camera.mode')}</dt>
              <dd>{settings.width && settings.height ? `${settings.width} × ${settings.height}` : '—'}</dd>
            </div>
            <div>
              <dt>{t('camera.requested')}</dt>
              <dd>{settings.fps ? `${settings.fps.toFixed(0)} fps` : '—'}</dd>
            </div>
            <div>
              <dt>{t('camera.delivered')}</dt>
              <dd>{settings.delivered_fps ? `${settings.delivered_fps.toFixed(1)} fps` : '—'}</dd>
            </div>
            <div>
              <dt>{t('camera.buffer')}</dt>
              <dd>
                {settings.pre_ms !== undefined && settings.post_ms !== undefined
                  ? `${settings.pre_ms.toFixed(0)} / ${settings.post_ms.toFixed(0)} ms`
                  : '—'}
              </dd>
            </div>
            <div>
              <dt>{t('camera.frames')}</dt>
              <dd>
                {settings.pre_frames !== undefined && settings.post_frames !== undefined
                  ? `${settings.pre_frames} + ${settings.post_frames}`
                  : '—'}
              </dd>
            </div>
          </dl>
        </section>

        {error && <p className="camera-settings__error">{error}</p>}
      </div>
    </aside>
  );
}

/**
 * Camera tab.
 *
 * When the high-speed capture runtime is active (--camera-capture), shows a
 * still refreshed every 5 s from the concurrent preview stream. The raw
 * rolling buffer keeps running, so shots are never missed while viewing.
 */
export function CameraFeed({ captureSettings, captureSettingsError, onUpdateCaptureSettings }: CameraFeedProps) {
  const { locale, t } = useI18n();
  const [previewState, setPreviewState] = useState<PreviewState>('checking');
  const [previewSrc, setPreviewSrc] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [exposureQuality, setExposureQuality] = useState<ExposureQuality | null>(null);
  // The picture's own size once it has loaded: right away after a mode switch
  // (640 x 400 <-> 640 x 200), while the settings may describe the next frame.
  const [previewSize, setPreviewSize] = useState<[number, number] | null>(null);
  const aspectRatio = frameAspect(previewSize, [captureSettings.width, captureSettings.height]);
  const aspect = cssFrameAspect(aspectRatio);
  const crosshairStyle = {
    ...BALL_GUIDE_CROSS_VARS,
    '--camera-aspect': aspect,
    '--camera-aspect-ratio': aspect,
  } as CSSProperties;
  const target = captureSettings.target_line ?? null;
  const ball = captureSettings.ball_mark ?? null;
  const [step, setStep] = useAlignStep();
  const tappable = canTapMark(captureSettings, step);
  // The close-up on every open (not persisted); the button beside the step chips shows the whole frame.
  const [cropped, setCropped] = useState(true);
  const toggleCrop = () => setCropped((value) => !value);
  const view = cropped ? ALIGN_CROP : FULL_FRAME;
  const flatFrame = reportsFlatFrame(exposureQuality, captureSettings);
  const setAnew = (next: AlignStep) => {
    setStep(next);
    if (next === 'ball' && ball) onUpdateCaptureSettings({ ball_tap: null });
    if (next === 'target' && target) onUpdateCaptureSettings({ target_tap: null });
  };

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    let timer: ReturnType<typeof setInterval> | null = null;

    const refresh = async () => {
      try {
        const response = await fetch(`${CAMERA_PREVIEW_URL}?t=${Date.now()}`, { cache: 'no-store' });
        if (cancelled) return;
        if (response.status === 404) {
          setPreviewState('unavailable');
          if (timer) clearInterval(timer);
          return;
        }
        if (!response.ok) return;
        const blob = await response.blob();
        if (cancelled) return;
        const nextUrl = URL.createObjectURL(blob);
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = nextUrl;
        setPreviewSrc(nextUrl);
        setLastUpdated(new Date());
        setPreviewState('available');
        const qualityResponse = await fetch(`${EXPOSURE_QUALITY_URL}?t=${Date.now()}`, { cache: 'no-store' });
        if (qualityResponse.ok && !cancelled) {
          setExposureQuality(await qualityResponse.json());
        }
      } catch {
        // Keep the last frame and retry after a transient network failure.
      }
    };

    refresh();
    timer = setInterval(refresh, PREVIEW_REFRESH_MS);
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, []);

  if (previewState === 'available' || previewState === 'checking') {
    return (
      <div className="camera-feed camera-feed--capture">
        <div className="camera-feed__header">
          <div>
            <span className="camera-feed__eyebrow">{t('camera.highSpeed')}</span>
            <h2 className="camera-feed__title">{t('camera.alignment')}</h2>
          </div>
          <div className="camera-feed__header-status">
            <span
              className={`camera-feed__exposure-quality camera-feed__exposure-quality--${
                flatFrame ? 'no_image' : (exposureQuality?.status ?? 'checking')
              }`}
              title={locale === 'de' ? t('camera.analyzing') : (exposureQuality?.message ?? t('camera.analyzing'))}
            >
              {t('camera.autoExposureShort')}:{' '}
              {flatFrame
                ? t('camera.noImage')
                : exposureQuality?.auto_exposure
                  ? t(autoExposureLabel(exposureQuality.auto_exposure.status))
                  : t('camera.checking')}
            </span>
            {lastUpdated && (
              <span className="camera-feed__timestamp">
                {t('camera.previewTime', { time: lastUpdated.toLocaleTimeString(getHtmlLang()) })}
              </span>
            )}
          </div>
        </div>
        <div className="camera-feed__workspace">
          <div
            className={`camera-feed__preview-column${aspectRatio >= 2 ? ' camera-feed__preview-column--wide-frame' : ''}`}
          >
            {previewSrc ? (
              <>
                <AlignSteps
                  step={step}
                  onSelect={setStep}
                  cropped={cropped}
                  onToggleCrop={toggleCrop}
                  placement="preview"
                />
                <div className="camera-feed__preview-frame">
                  <div
                    className={`camera-feed__stream${tappable ? ' camera-feed__stream--tappable' : ''}${
                      cropped ? ' camera-feed__stream--cropped' : ''
                    }`}
                    style={crosshairStyle}
                    onClick={(event) => {
                      const update = alignTap(
                        captureSettings,
                        step,
                        event.clientX,
                        event.clientY,
                        event.currentTarget.getBoundingClientRect(),
                        view
                      );
                      if (update) onUpdateCaptureSettings(update);
                    }}
                  >
                    {/* The view clips the frame's box (the scene) to the window shown; the picture and
                     * every overlay are laid out in frame percent in the scene. */}
                    <div className="camera-feed__view">
                      <div className="camera-feed__scene" style={cropSceneBox(view)}>
                        <img
                          src={previewSrc}
                          alt={t('camera.preview')}
                          className="camera-feed__video"
                          onLoad={(event) =>
                            setPreviewSize([event.currentTarget.naturalWidth, event.currentTarget.naturalHeight])
                          }
                        />
                        <BallGuideHairlines>
                          {ball && target && (
                            <g className="camera-feed__target-line">
                              {['camera-feed__target-line-outline', 'camera-feed__target-line-stroke'].map((name) => (
                                <line
                                  key={name}
                                  x1={ball.preview_x_pct}
                                  y1={ball.preview_y_pct}
                                  x2={target.preview_x_pct}
                                  y2={target.preview_y_pct}
                                  vectorEffect="non-scaling-stroke"
                                  className={name}
                                />
                              ))}
                            </g>
                          )}
                        </BallGuideHairlines>
                        <div className="camera-feed__center-cross" aria-hidden="true" />
                        {ball && (
                          <div
                            className="camera-feed__mark camera-feed__mark--ball"
                            style={{ left: `${ball.preview_x_pct}%`, top: `${ball.preview_y_pct}%` }}
                            aria-hidden="true"
                          />
                        )}
                        {target && (
                          <div
                            className="camera-feed__mark camera-feed__mark--target"
                            style={{ left: `${target.preview_x_pct}%`, top: `${target.preview_y_pct}%` }}
                            aria-hidden="true"
                          />
                        )}
                      </div>
                    </div>
                    {/* Only in the whole picture: in the close-up the chip sat on the zone ahead of the
                     * ball where the targets are tapped (x 54-69 %, y 42-54 % of the frame). The
                     * header badge keeps showing the exposure state. */}
                    {!cropped && (
                      <div className="camera-feed__overlay">
                        <div className="camera-feed__status">{withLabelBreaks(t('camera.bufferArmed'))}</div>
                      </div>
                    )}
                  </div>
                </div>
              </>
            ) : (
              <div className="camera-feed__message camera-feed__message--preview">
                <h3>{t('camera.waitingTitle')}</h3>
                <p>{t('camera.waitingDetail')}</p>
              </div>
            )}
          </div>
          <CaptureSettingsPanel
            settings={captureSettings}
            exposureQuality={exposureQuality}
            error={captureSettingsError}
            onUpdate={onUpdateCaptureSettings}
            step={step}
            onSelectStep={setStep}
            onSetAnew={setAnew}
            cropped={cropped}
            onToggleCrop={toggleCrop}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="camera-feed camera-feed--unavailable">
      <div className="camera-feed__message">
        <h3>{t('camera.unavailable')}</h3>
        <p>{t('camera.enableHint')}</p>
      </div>
    </div>
  );
}
