import type { Shot } from '../../types/shot';
import { isSwingSpeedShot } from '../../types/shot';
import { t } from '../../i18n';
import { formatNumber } from '../../utils/units';
import { meanSem } from '../../utils/clubDataStats';
import { calculatedCaption, NO_VALUE, uncalibratedCaption, uncalibratedLabel, type LiveMetric } from './liveMetrics';

/**
 * The StatsPanel club page (2×1), in this order. Only what this hardware can deliver:
 * the face, face to path, the spin axis and the strike are never shown.
 */
export const CLUB_STAT_IDS = ['stats_cd_path', 'stats_cd_aoa'] as const;

function signed(value: number, digits = 1): string {
  return `${value >= 0 ? '+' : ''}${formatNumber(value, digits)}`;
}

/** "+0,7 ± 0,8" (unit °): a session mean with its SE. */
function meanValue(mean: number, se: number): string {
  return `${signed(mean)} ± ${formatNumber(se, 1)}`;
}

function calculatedTile(
  id: string,
  label: string,
  value: string,
  unit: string | undefined,
  caption: string
): LiveMetric {
  return { id, label, value, unit, subtext: caption, estimated: true, estimatedLabel: t('metric.calculated') };
}

function emptyTile(id: string, label: string, subtext?: string): LiveMetric {
  return { id, label, value: NO_VALUE, subtext };
}

function meanSemTile(
  id: string,
  label: string,
  values: Array<number | null | undefined>,
  { uncalibrated = false }: { uncalibrated?: boolean } = {}
): LiveMetric {
  const stat = meanSem(values);
  if (stat === null) return emptyTile(id, label);
  const caption = uncalibrated ? uncalibratedCaption : calculatedCaption;
  const tile = calculatedTile(
    id,
    label,
    meanValue(stat.mean, stat.sem),
    '°',
    caption(t('clubData.count', { count: stat.n }))
  );
  return uncalibrated ? { ...tile, estimatedLabel: uncalibratedLabel() } : tile;
}

function clubShots(shots: readonly Shot[], club: string): Shot[] {
  return shots.filter((shot) => shot.club === club && !isSwingSpeedShot(shot));
}

/**
 * The StatsPanel club page for one club of one profile: session means of the
 * calculated club data (plan T9, section 4). Path and attack angle: mean ± SEM
 * from 3 shots; both captions read "calculated · uncalibrated" (no ground truth yet).
 */
export function buildClubStatTiles(shots: readonly Shot[], club: string): LiveMetric[] {
  const own = clubShots(shots, club);
  return [
    meanSemTile(
      'stats_cd_path',
      t('stats.avgPath'),
      own.map((shot) => shot.camera_club_path_deg),
      { uncalibrated: true }
    ),
    meanSemTile(
      'stats_cd_aoa',
      t('stats.avgAoa'),
      own.map((shot) => shot.camera_attack_angle_deg),
      { uncalibrated: true }
    ),
  ];
}
