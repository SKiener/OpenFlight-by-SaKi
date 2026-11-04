import { useEffect, useState, type CSSProperties } from 'react';
import type { CameraCaptureSettings } from '../stores/useCameraStore';
import type { Shot } from '../types/shot';
import {
  computeSwingSpeedStats,
  getCarryYards,
  getDisplayedSpin,
  getSwingSpeedMph,
  isSwingSpeedShot,
} from '../types/shot';
import { useUnitPreference } from '../state/useUnitPreference';
import { formatNumber, formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit, type UnitSystem } from '../utils/units';
import { getServerOrigin } from '../utils/serverOrigin';
import { markTypicalCarry, typicalCarryCaption } from '../utils/carryDisplay';
import { cssFrameAspect, frameAspect } from '../utils/frameAspect';
import { DemoBand } from './DemoBand';
import { MetricCard } from './ui/MetricCard';
import { acceptedExperimental, formatSpinRpm, shownExperimentalStatus } from './panel/liveMetrics';
import type { MessageKey } from '../i18n';
import { useI18n } from '../i18n/useI18n';
import './DisplayMode.css';

type Translate = (key: MessageKey, vars?: Record<string, string | number>) => string;

interface DisplayModeProps {
  connected: boolean;
  captureSettings: CameraCaptureSettings;
  latestShot: Shot | null;
  shots: Shot[];
  demoActive: boolean;
}

interface DisplayMetric {
  label: string;
  value: string;
  unit?: string;
  detail?: string;
}

const CAMERA_PREVIEW_URL = `${getServerOrigin()}/api/camera/preview.jpg`;
const CAMERA_PREVIEW_REFRESH_MS = 5_000;
const RECENT_SHOT_COUNT = 5;

function formatOptionalNumber(value: number | null, digits = 1, prefixPositive = false): string {
  if (value === null) {
    return '--';
  }

  const prefix = prefixPositive && value > 0 ? '+' : '';
  return `${prefix}${formatNumber(value, digits)}`;
}

/** The spin the carry used: a measured or calculated one keeps its quality, a typical one says so. */
function buildSpinMetric(shot: Shot, t: Translate): DisplayMetric {
  const spin = getDisplayedSpin(shot);
  if (spin === null) {
    return { label: t('metric.spin'), value: '--' };
  }

  return {
    label: t('metric.spin'),
    value: formatSpinRpm(spin.rpm),
    unit: 'rpm',
    detail:
      spin.source === 'typical'
        ? t('metric.typical')
        : shot.spin_quality
          ? t(`quality.${shot.spin_quality}`)
          : undefined,
  };
}

function experimentalStatus(status: string | null | undefined, t: Translate): string {
  if (!status || status === 'candidate_available') return `${t('quality.experimental')} · ${t('quality.candidate')}`;
  return `${t('quality.experimental')} · ${status.startsWith('rejected_') ? t('quality.rejected', { reason: status.slice(9).replaceAll('_', ' ') }) : status.replaceAll('_', ' ')}`;
}

/**
 * A radar club path or attack angle's detail. Every shown value is measured but "not
 * calibrated" (no ground truth yet), an experimental one after its "experimental"; without
 * a value a rejected experimental status still says why.
 */
function clubAngleDetail(value: number | null, status: string | null, t: Translate): string | undefined {
  if (value == null) return status != null ? experimentalStatus(status, t) : undefined;
  return status != null ? `${t('quality.experimental')} · ${t('metric.uncalibrated')}` : t('metric.uncalibrated');
}

function buildMetrics(shot: Shot | null, unitSystem: UnitSystem, t: Translate): DisplayMetric[] {
  if (!shot) {
    return [
      { label: t('display.ballSpeed'), value: '--', unit: getSpeedUnit(unitSystem) },
      { label: t('metric.carry'), value: '--', unit: getDistanceUnit(unitSystem) },
      { label: t('display.clubSpeed'), value: '--', unit: getSpeedUnit(unitSystem) },
      { label: t('metric.smash'), value: '--' },
      { label: t('display.launch'), value: '--', unit: 'deg' },
      { label: t('metric.spin'), value: '--', unit: 'rpm' },
      { label: t('display.clubPath'), value: '--', unit: 'deg' },
      { label: t('metric.clubAoa'), value: '--', unit: 'deg' },
      { label: t('metric.hLaunch'), value: '--', unit: 'deg' },
    ];
  }

  const carryYards = getCarryYards(shot);
  const clubPath =
    shot.club_path_deg ?? acceptedExperimental(shot.experimental_club_path_deg, shot.experimental_club_path_status);
  const attackAngle =
    shot.club_angle_deg ??
    acceptedExperimental(shot.experimental_attack_angle_deg, shot.experimental_attack_angle_status);
  const clubPathStatus = shownExperimentalStatus(shot.experimental_club_path_deg, shot.experimental_club_path_status);
  const attackStatus = shownExperimentalStatus(shot.experimental_attack_angle_deg, shot.experimental_attack_angle_status);

  return [
    {
      label: t('display.ballSpeed'),
      value: formatSpeed(shot.ball_speed_mph, unitSystem, 1),
      unit: getSpeedUnit(unitSystem),
    },
    {
      label: t('metric.carry'),
      value: markTypicalCarry(shot, formatDistance(carryYards, unitSystem, 0)),
      unit: getDistanceUnit(unitSystem),
      detail: shot.carry_spin_adjusted
        ? (typicalCarryCaption(shot) ?? t('metric.spinAdjusted'))
        : undefined,
    },
    {
      label: t('display.clubSpeed'),
      value: shot.club_speed_mph === null ? '--' : formatSpeed(shot.club_speed_mph, unitSystem, 1),
      unit: shot.club_speed_mph === null ? undefined : getSpeedUnit(unitSystem),
    },
    {
      label: t('metric.smash'),
      value: shot.smash_factor === null ? '--' : formatNumber(shot.smash_factor, 2),
    },
    {
      label: t('display.launch'),
      value: formatOptionalNumber(shot.launch_angle_vertical),
      unit: shot.launch_angle_vertical === null ? undefined : 'deg',
      detail: shot.angle_source ?? undefined,
    },
    buildSpinMetric(shot, t),
    {
      label: t('display.clubPath'),
      value: formatOptionalNumber(clubPath, 1, true),
      unit: clubPath == null ? undefined : 'deg',
      detail: clubAngleDetail(clubPath, shot.club_path_deg == null ? clubPathStatus : null, t),
    },
    {
      label: t('metric.clubAoa'),
      value: formatOptionalNumber(attackAngle),
      unit: attackAngle == null ? undefined : 'deg',
      detail: clubAngleDetail(attackAngle, shot.club_angle_deg == null ? attackStatus : null, t),
    },
    {
      label: t('metric.hLaunch'),
      value: formatOptionalNumber(shot.launch_angle_horizontal, 1, true),
      unit: shot.launch_angle_horizontal === null ? undefined : 'deg',
      detail:
        shot.launch_angle_horizontal_source === 'camera_assisted_experimental'
          ? `${t('quality.cameraAssisted')} (${t('quality.experimental')})`
          : undefined,
    },
  ];
}

function toMetricCard(metric: DisplayMetric, featured = false) {
  return (
    <MetricCard
      key={metric.label}
      value={metric.value}
      unit={metric.unit}
      label={metric.label}
      subtext={metric.detail}
      variant={featured ? 'emphasis' : 'default'}
    />
  );
}

export function DisplayMode({ connected, captureSettings, latestShot, shots, demoActive }: DisplayModeProps) {
  const { t } = useI18n();
  const [failedCameraKey, setFailedCameraKey] = useState<string | null>(null);
  const [cameraRefresh, setCameraRefresh] = useState(0);
  const [previewSize, setPreviewSize] = useState<[number, number] | null>(null);
  const cameraAvailable = Boolean(captureSettings.available && captureSettings.running);
  useEffect(() => {
    if (!cameraAvailable) return undefined;
    const timer = window.setInterval(() => {
      setFailedCameraKey(null);
      setCameraRefresh((value) => value + 1);
    }, CAMERA_PREVIEW_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [cameraAvailable]);
  const { unitSystem } = useUnitPreference();
  const isSwingSpeedSession = latestShot ? isSwingSpeedShot(latestShot) : false;
  const swingStats = computeSwingSpeedStats(shots);
  const metrics = isSwingSpeedSession
    ? [
        {
          label: t('display.lastSwing'),
          value: formatSpeed(swingStats.last_speed_mph, unitSystem, 1),
          unit: getSpeedUnit(unitSystem),
        },
        {
          label: t('metric.best'),
          value: formatSpeed(swingStats.best_speed_mph, unitSystem, 1),
          unit: getSpeedUnit(unitSystem),
          detail: t('display.thisSession'),
        },
        {
          label: t('metric.average'),
          value: formatSpeed(swingStats.avg_speed_mph, unitSystem, 1),
          unit: getSpeedUnit(unitSystem),
        },
        { label: t('metric.swings'), value: String(swingStats.count) },
      ]
    : buildMetrics(latestShot, unitSystem, t);
  const recentShots = shots.slice(-RECENT_SHOT_COUNT).reverse();
  const cameraAspect = cssFrameAspect(frameAspect(previewSize, [captureSettings.width, captureSettings.height]));
  const cameraKey = `${captureSettings.available}-${captureSettings.running}`;
  const cameraError = failedCameraKey === cameraKey;

  return (
    <main className="display-mode">
      <DemoBand active={demoActive} connected={connected} viewOnly />
      <section className="display-mode__hero" aria-label={t('display.tvAria')}>
        <div className="display-mode__camera" style={{ '--camera-aspect-ratio': cameraAspect } as CSSProperties}>
          {!cameraAvailable || cameraError ? (
            <div className="display-mode__camera-placeholder">
              <span>{t('display.streamUnavailable')}</span>
            </div>
          ) : (
            <img
              src={`${CAMERA_PREVIEW_URL}?refresh=${cameraRefresh}`}
              alt={t('display.streamAlt')}
              className="display-mode__camera-image"
              onError={() => setFailedCameraKey(cameraKey)}
              onLoad={(event) => {
                setFailedCameraKey(null);
                setPreviewSize([event.currentTarget.naturalWidth, event.currentTarget.naturalHeight]);
              }}
            />
          )}
          <div className="display-mode__status-row">
            <span
              className={`display-mode__status ${connected ? 'display-mode__status--online' : 'display-mode__status--offline'}`}
            >
              {connected ? t('display.socketOn') : t('display.socketOff')}
            </span>
            <span
              className={`display-mode__status ${cameraAvailable && !cameraError ? 'display-mode__status--online' : 'display-mode__status--offline'}`}
            >
              {cameraAvailable && !cameraError ? t('display.streamActive') : t('display.cameraUnavailable')}
            </span>
          </div>
        </div>

        <div className="display-mode__shot-panel">
          <div className="display-mode__eyebrow">{t('display.eyebrow')}</div>
          <h1 className="display-mode__title">
            {isSwingSpeedSession ? t('display.swingSpeed') : latestShot ? latestShot.club : t('display.ready')}
          </h1>
          <div className="display-mode__primary-grid">
            {toMetricCard(metrics[0], true)}
            {toMetricCard(metrics[1], true)}
          </div>
          <div className="display-mode__metrics-grid">{metrics.slice(2).map((metric) => toMetricCard(metric))}</div>
        </div>
      </section>

      <section className="display-mode__recent" aria-label={t('display.recentAria')}>
        {recentShots.length === 0 ? (
          <div className="display-mode__empty-strip">{t('display.recentEmpty')}</div>
        ) : (
          recentShots.map((shot, index) => (
            <div className="display-shot-chip" key={shot.timestamp}>
              <span className="display-shot-chip__number">#{shots.length - index}</span>
              <span className="display-shot-chip__club">
                {isSwingSpeedShot(shot) ? (shot.training_implement_label ?? shot.club) : shot.club}
              </span>
              <span className="display-shot-chip__stat">
                {formatSpeed(isSwingSpeedShot(shot) ? getSwingSpeedMph(shot) : shot.ball_speed_mph, unitSystem, 0)}{' '}
                {getSpeedUnit(unitSystem)}
              </span>
              {!isSwingSpeedShot(shot) && (
                <span className="display-shot-chip__stat">
                  {markTypicalCarry(shot, formatDistance(getCarryYards(shot), unitSystem, 0))}{' '}
                  {getDistanceUnit(unitSystem)}
                </span>
              )}
            </div>
          ))
        )}
      </section>
    </main>
  );
}
