import { useI18n } from '../i18n/useI18n';
import { isSwingSpeedShot, type Shot } from '../types/shot';
import type { UnitSystem } from '../utils/units';
import { buildLiveMetrics, carrySourceLabel, NO_VALUE, type LiveMetric } from '../components/panel/liveMetrics';
import { MetricCard } from '../components/ui/MetricCard';
import { ProgressIndicator } from '../components/ProgressIndicator';
import { CompanionReplayPlayer } from './CompanionReplayPlayer';
import { useCompanionReplay } from './useCompanionReplay';
import { useDirectionCalibrated } from '../stores/useSystemStore';
import { isTypicalSpinCarry } from '../utils/carryDisplay';

const HERO_METRICS = ['carry', 'ball_speed'];
const DETAIL_METRICS = ['launch_v', 'launch_h', 'spin', 'club_speed', 'smash'];
const NO_SWING_STATS = { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 };

function pick(metrics: LiveMetric[], ids: string[]): LiveMetric[] {
  return ids.flatMap((id) => metrics.filter((metric) => metric.id === id));
}

/** The phone has room for a typical-spin carry's span, so that caption stays as built. */
function withCarrySource(metric: LiveMetric, shot: Shot): LiveMetric {
  if (metric.id !== 'carry' || metric.value === NO_VALUE || isTypicalSpinCarry(shot)) return metric;
  return { ...metric, subtext: carrySourceLabel(shot) ?? metric.subtext };
}

function MetricTiles({ className, metrics }: { className: string; metrics: LiveMetric[] }) {
  return (
    <div className={className}>
      {metrics.map((metric) => (
        <div key={metric.id} className="companion__tile" data-metric={metric.id}>
          <MetricCard {...metric} labelPosition="above" />
        </div>
      ))}
    </div>
  );
}

export function CompanionMetrics({ shot, unitSystem }: { shot: Shot; unitSystem: UnitSystem }) {
  useI18n();
  const directionCalibrated = useDirectionCalibrated();
  const metrics = buildLiveMetrics(shot, unitSystem, NO_SWING_STATS, { directionCalibrated }).map((metric) =>
    withCarrySource(metric, shot)
  );
  const swing = isSwingSpeedShot(shot);
  return (
    <>
      {swing ? null : <MetricTiles className="companion__hero" metrics={pick(metrics, HERO_METRICS)} />}
      <MetricTiles className="companion__details" metrics={swing ? metrics : pick(metrics, DETAIL_METRICS)} />
    </>
  );
}

export function CompanionReplay({ shot, awaitingCamera }: { shot: Shot; awaitingCamera: boolean }) {
  const { t } = useI18n();
  const { state, retry, reportPlaybackError } = useCompanionReplay(shot.camera_replay);

  if (state.kind === 'ready' && shot.camera_replay) {
    return (
      <CompanionReplayPlayer
        replay={shot.camera_replay}
        videoUrl={state.videoUrl}
        onPlaybackError={reportPlaybackError}
      />
    );
  }
  if (state.kind === 'error' && state.stage === 'preparation' && state.status === 404) {
    return <p className="companion-replay__note">{t('companion.replayGone')}</p>;
  }
  if (state.kind === 'error') {
    return (
      <div className="companion-replay__status" role="alert">
        <strong>{state.stage === 'playback' ? t('replay.playbackError') : t('replay.error')}</strong>
        <span>
          {state.message ?? (state.stage === 'playback' ? t('replay.playbackErrorDetail') : t('replay.errorDetail'))}
        </span>
        <button type="button" className="companion-button companion-button--primary" onClick={retry}>
          {t('replay.tryAgain')}
        </button>
      </div>
    );
  }
  if (shot.camera_replay || awaitingCamera) {
    return (
      <div className="companion-replay__status">
        <ProgressIndicator variant="inline" title={t('replay.preparing')} detail={t('replay.preparingDetail')} />
      </div>
    );
  }
  return <p className="companion-replay__note">{t('companion.noReplay')}</p>;
}
