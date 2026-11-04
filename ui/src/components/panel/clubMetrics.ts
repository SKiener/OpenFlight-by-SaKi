import type { Shot } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { formatLengthCm, formatNumber, getLengthUnit } from '../../utils/units';
import { t } from '../../i18n';
import {
  buildLiveMetrics,
  calculatedAngleMetric,
  calculatedMetric,
  markEstimated,
  NO_VALUE,
  uncalibratedCaption,
  uncalibratedLabel,
  type LiveMetric,
} from './liveMetrics';
import { hasImpactPoint, impactZoneWord } from './impactPoint';

/**
 * The club page of "All metrics": always these five, in this order, so the grid never
 * reflows. Only what this hardware can deliver: the lofts, the face, face to path and the
 * spin axis are never shown (the server still logs them). The impact point shows as a
 * face graphic with its zone word (heel / centre / toe), never in mm.
 */
export const CLUB_METRIC_IDS = ['club_speed', 'cd_path', 'cd_aoa', 'cd_impact', 'cd_low_point'] as const;

/** The tiles that show a calculated value, each with the ≈ mark and "calculated" once it has one. */
export const CALCULATED_CLUB_METRIC_IDS = ['cd_path', 'cd_aoa', 'cd_impact', 'cd_low_point'] as const;

/** The tiles the club-data job fills: the calculated ones, never the radar's club speed. */
const PENDING_CLUB_METRIC_IDS: ReadonlySet<string> = new Set(CALCULATED_CLUB_METRIC_IDS);

/** An empty calculated tile reads "calculating" while the club data is still on its way. */
function pendingOr(shot: Shot, metric: LiveMetric): LiveMetric {
  if (metric.value !== NO_VALUE || shot.club_data_status !== 'pending') return metric;
  if (!PENDING_CLUB_METRIC_IDS.has(metric.id)) return metric;
  return { ...metric, subtext: t('clubData.pending') };
}

/**
 * The impact point card: its zone word as the value (ClubDataPanel draws the face above
 * it), fitted as a word, with ≈ and "calculated · uncalibrated" once there is an offset
 * (a per-club offset of a few mm is still unknown). No sigma: it would be millimetres.
 */
export function impactMetric(shot: Shot): LiveMetric {
  const shown = hasImpactPoint(shot);
  return {
    id: 'cd_impact',
    label: t('clubData.impact'),
    value: impactZoneWord(shot),
    kind: 'class',
    subtext: shown ? uncalibratedCaption() : undefined,
    estimated: markEstimated(shown),
    estimatedLabel: shown ? uncalibratedLabel() : undefined,
  };
}

/** Closer than this to the ball either way (cm), the low point reads "at the ball". */
const LOW_POINT_AT_BALL_CM = 0.5;

/**
 * The low point card: how far the club's arc bottoms out from the ball, without a sign,
 * one decimal in cm (in with yards). Its caption leads with the side ("ahead of the
 * ball", + toward the target; "at the ball" under 0.5 cm), then ±σ, then the attack
 * angle's "calculated · uncalibrated" (it is the bottom of the same fitted arc and
 * moves with the attack angle: see {@link uncalibratedLabel}). "—" without
 * one: the server withholds it for the driver and woods, and the camera may have none.
 */
export function lowPointMetric(shot: Shot, unitSystem: UnitSystem): LiveMetric {
  const label = t('clubData.lowPoint');
  const cm = shot.camera_low_point_cm;
  if (typeof cm !== 'number' || !Number.isFinite(cm)) {
    return calculatedMetric('cd_low_point', label, null, [], { uncalibrated: true });
  }
  const unit = getLengthUnit(unitSystem);
  const length = (valueCm: number) => formatLengthCm(valueCm, unitSystem);
  const side =
    Math.abs(cm) < LOW_POINT_AT_BALL_CM ? 'lowPointAt' : cm > 0 ? 'lowPointAhead' : 'lowPointBehind';
  const sigma = shot.camera_low_point_sigma_cm;
  const sigmaText =
    typeof sigma === 'number' && Number.isFinite(sigma)
      ? t('clubData.sigmaLength', { value: length(sigma), unit })
      : undefined;
  return calculatedMetric(
    'cd_low_point',
    label,
    { value: length(Math.abs(cm)), unit },
    [t(`clubData.${side}`), sigmaText],
    { uncalibrated: true }
  );
}

/**
 * The club page: club speed as measured (same tile as the ball page), then the
 * calculated camera path, attack angle, impact point and low point (smash stays on
 * the ball page). None of them has ground truth yet: "calculated · uncalibrated".
 */
export function buildClubMetrics(shot: Shot, unitSystem: UnitSystem): LiveMetric[] {
  const noSwing = { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 };
  const ball = buildLiveMetrics(shot, unitSystem, noSwing);
  const measured = (id: string) => ball.find((metric) => metric.id === id) as LiveMetric;
  const metrics: LiveMetric[] = [
    measured('club_speed'),
    calculatedAngleMetric(
      'cd_path',
      t('metric.clubPath'),
      shot.camera_club_path_deg,
      true,
      shot.camera_club_path_sigma_deg,
      { uncalibrated: true }
    ),
    calculatedAngleMetric(
      'cd_aoa',
      t('metric.clubAoa'),
      shot.camera_attack_angle_deg,
      true,
      shot.camera_attack_angle_sigma_deg,
      { uncalibrated: true }
    ),
    impactMetric(shot),
    lowPointMetric(shot, unitSystem),
  ];
  return metrics.map((metric) => pendingOr(shot, metric));
}

/**
 * The ShotsPanel's one-line summary of a shot's calculated club data, e.g.
 * "Path +2.1° · Attack -4.3° (uncalibrated)": one "(uncalibrated)" after the last
 * value covers both. Empty without either value.
 */
export function clubDataSummaryParts(shot: Shot): string[] {
  const angle = (value: number | null | undefined) =>
    typeof value === 'number' && Number.isFinite(value) ? `${value >= 0 ? '+' : ''}${formatNumber(value, 1)}°` : null;
  const parts: string[] = [];
  const path = angle(shot.camera_club_path_deg);
  if (path) parts.push(`${t('clubData.summaryPath')} ${path}`);
  const aoa = angle(shot.camera_attack_angle_deg);
  if (aoa) parts.push(`${t('clubData.summaryAoa')} ${aoa}`);
  if (parts.length > 0) parts[parts.length - 1] += ` (${t('impact.uncalibrated')})`;
  return parts;
}
