import { useMemo, useRef, useState, type ReactNode } from 'react';
import { getClubName } from '../../data/clubs';
import type { Shot } from '../../types/shot';
import {
  computeStats,
  computeSwingSpeedStats,
  filterShotsByProfile,
  getUniqueClubs,
  isBallSpeedUncertain,
  isSwingSpeedShot,
} from '../../types/shot';
import { useDragScroll } from '../../hooks/useDragScroll';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useI18n } from '../../i18n/useI18n';
import { formatNumber, formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit } from '../../utils/units';
import { useSharedFitFontSize } from '../../hooks/useFitFontSize';
import { MetricCard } from '../ui/MetricCard';
import { isTypicalSpinCarry, withTypicalCarryMark } from '../../utils/carryDisplay';
import { SegmentedControl } from '../ui/SegmentedControl';
import { PanelHeader } from './PanelHeader';
import { buildClubStatTiles } from './clubStatTiles';
import { withLabelBreaks } from './labelBreaks';

interface StatsPanelProps {
  shots: Shot[];
  activeClub: string;
  profileId: string;
  profileName: string;
  /** Pinned header control, e.g. Clear session. */
  headerAction?: ReactNode;
}

interface StatTile {
  id: string;
  label: string;
  value: string;
  unit?: string;
  title?: string;
}

/**
 * The club page of the statistics: session means of the calculated club data
 * for one club (`shots` is one profile's). Without a club ("All") it asks for one.
 */
export function StatsClubPage({ shots, club }: { shots: readonly Shot[]; club: string | null }) {
  const { locale, t } = useI18n();
  const tiles = useMemo(
    () => (club === null ? [] : buildClubStatTiles(shots, club)),
    // t() is stable; locale invalidates translated labels and formatted numbers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shots, club, locale]
  );
  const gridRef = useSharedFitFontSize(
    tiles.length > 0,
    tiles.map((tile) => `${tile.value}:${tile.unit ?? ''}`).join('|')
  );
  if (club === null) {
    return (
      <div className="panel__body--empty stats-panel__pick-club">
        <span className="panel__empty-title">{t('clubData.pickClub')}</span>
        <span className="panel__empty-detail">{t('clubData.pickClubDetail')}</span>
      </div>
    );
  }
  return (
    <div ref={gridRef} className={`stats-panel__grid stats-panel__grid--of-${tiles.length} stats-panel__grid--club`}>
      {tiles.map((tile) => (
        <MetricCard key={tile.id} metricId={tile.id} {...tile} labelPosition="above" />
      ))}
    </div>
  );
}

/**
 * The header's subtitle: the profile, and on the club page the ≈ legend, since
 * short club tiles drop their "calculated" captions (800×480, 320 px, landscape).
 */
export function StatsHeaderSubtitle({ profileName, clubPage }: { profileName: string; clubPage: boolean }) {
  const { t } = useI18n();
  if (!clubPage) return <>{profileName}</>;
  return (
    <>
      {profileName} ·{' '}
      <span className="stats-panel__legend">
        <span aria-hidden="true">≈</span> {t('metric.calculated')}
      </span>
    </>
  );
}

/**
 * Design doc 7a: session summary as a hairline tile grid, with the per-club
 * filter chips above the tiles. Six tiles for a ball-strike session (3x2 as
 * drawn), four for a swing-speed one. A ball-strike session has a Ball | Club
 * switch at the start of the chip row, outside the scrolling club strip so it
 * never scrolls away (on a portrait phone 500 px or taller it takes its own
 * row above the chips): the club page holds the session means of the calculated club data for
 * the selected club (with "All" it asks for one), and the header names the ≈ mark.
 */
export function StatsPanel({ shots, activeClub, profileId, profileName, headerAction }: StatsPanelProps) {
  const { locale, t } = useI18n();
  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const hasShotsForActiveClub = profileShots.some((shot) => shot.club === activeClub);
  const [selectedClub, setSelectedClub] = useState<string | null>(hasShotsForActiveClub ? activeClub : null);
  const [prevActiveClub, setPrevActiveClub] = useState(activeClub);
  const [prevProfileId, setPrevProfileId] = useState(profileId);
  const [page, setPage] = useState<'ball' | 'club'>('ball');
  const chipRef = useRef<HTMLDivElement>(null);
  const chipScroll = useDragScroll(chipRef, 'x');

  // Update state during render when the prop changes, rather than in an effect.
  if (activeClub !== prevActiveClub || profileId !== prevProfileId) {
    setPrevActiveClub(activeClub);
    setPrevProfileId(profileId);
    setSelectedClub(hasShotsForActiveClub ? activeClub : null);
  }

  const { unitSystem } = useUnitPreference();
  const speedUnit = getSpeedUnit(unitSystem);
  const distanceUnit = getDistanceUnit(unitSystem);

  const availableClubs = useMemo(() => getUniqueClubs(profileShots), [profileShots]);
  const clubCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const shot of profileShots) {
      counts[shot.club] = (counts[shot.club] ?? 0) + 1;
    }
    return counts;
  }, [profileShots]);

  const filteredShots = useMemo(
    () => (selectedClub === null ? profileShots : profileShots.filter((shot) => shot.club === selectedClub)),
    [profileShots, selectedClub]
  );

  const stats = useMemo(() => computeStats(filteredShots), [filteredShots]);
  const swingStats = useMemo(() => computeSwingSpeedStats(filteredShots), [filteredShots]);
  const isSwingSpeedSession = filteredShots.length > 0 && filteredShots.every(isSwingSpeedShot);
  // One typical-spin carry in the average makes the average a guess at the spin too
  // (an uncertain ball speed is not in the average, computeStats).
  const typicalCarryAverage = useMemo(
    () => filteredShots.some((shot) => !isBallSpeedUncertain(shot) && isTypicalSpinCarry(shot)),
    [filteredShots]
  );

  const tiles: StatTile[] = useMemo(() => {
    if (isSwingSpeedSession) {
      return [
        { id: 'count', label: t('metric.swings'), value: String(swingStats.count) },
        {
          id: 'last',
          label: t('stats.last'),
          value: formatSpeed(swingStats.last_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
        {
          id: 'best',
          label: t('metric.best'),
          value: formatSpeed(swingStats.best_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
        {
          id: 'avg',
          label: t('metric.average'),
          value: formatSpeed(swingStats.avg_speed_mph, unitSystem, 1),
          unit: speedUnit,
        },
      ];
    }

    return [
      { id: 'count', label: t('stats.shots'), value: String(stats.shot_count) },
      {
        id: 'avg_ball',
        label: t('stats.avgBall'),
        value: formatSpeed(stats.avg_ball_speed, unitSystem, 1),
        unit: speedUnit,
      },
      {
        id: 'max_ball',
        label: t('stats.maxBall'),
        value: formatSpeed(stats.max_ball_speed, unitSystem, 1),
        unit: speedUnit,
      },
      {
        id: 'avg_carry',
        label: t('stats.avgCarry'),
        value: typicalCarryAverage
          ? withTypicalCarryMark(formatDistance(stats.avg_carry_est, unitSystem, 0))
          : formatDistance(stats.avg_carry_est, unitSystem, 0),
        unit: distanceUnit,
        title: typicalCarryAverage ? t('carry.typicalSpinNote') : undefined,
      },
      {
        id: 'avg_club',
        label: t('stats.avgClub'),
        value: stats.avg_club_speed === null ? '—' : formatSpeed(stats.avg_club_speed, unitSystem, 1),
        unit: stats.avg_club_speed === null ? undefined : speedUnit,
      },
      {
        id: 'avg_smash',
        label: t('stats.avgSmash'),
        value: stats.avg_smash_factor === null ? '—' : formatNumber(stats.avg_smash_factor, 2),
      },
    ];
    // t() is stable; locale invalidates translated labels and formatted numbers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isSwingSpeedSession, stats, swingStats, unitSystem, speedUnit, distanceUnit, typicalCarryAverage, locale, t]);

  const showSwitch = profileShots.length > 0 && !isSwingSpeedSession;
  const clubPage = showSwitch && page === 'club';
  const pageSwitch = showSwitch ? (
    <SegmentedControl
      ariaLabel={t('clubData.tabsAria')}
      value={page}
      onChange={setPage}
      options={[
        { id: 'ball', label: t('clubData.tabBall') },
        { id: 'club', label: t('clubData.tabClub') },
      ]}
    />
  ) : null;

  const clubFilters = (
    <div className="stats-panel__chips">
      {pageSwitch}
      <div className="stats-panel__club-filter" role="group" aria-label={t('stats.filterByClub')}>
        <button
          type="button"
          className={`panel-chip${selectedClub === null ? ' panel-chip--active' : ''}`}
          aria-pressed={selectedClub === null}
          onClick={() => setSelectedClub(null)}
        >
          {t('stats.all', { count: profileShots.length })}
        </button>
        <div
          className="panel-chips stats-panel__chip-scroll"
          ref={chipRef}
          onPointerDown={chipScroll.onPointerDown}
          onPointerMove={chipScroll.onPointerMove}
          onPointerUp={chipScroll.onPointerUp}
          onPointerCancel={chipScroll.onPointerCancel}
          onClickCapture={chipScroll.onClickCapture}
        >
          {availableClubs.map((club) => (
            <button
              key={club}
              type="button"
              className={`panel-chip${selectedClub === club ? ' panel-chip--active' : ''}`}
              aria-pressed={selectedClub === club}
              onClick={(event) => {
                setSelectedClub(club);
                // A chip picked at the strip's edge comes fully into view.
                event.currentTarget.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
              }}
            >
              {(locale === 'de' && club.includes('-') ? getClubName(club) : club).toUpperCase()} (
              {clubCounts[club] ?? 0})
            </button>
          ))}
        </div>
      </div>
    </div>
  );
  return (
    <div className="panel">
      <PanelHeader
        title={t('nav.stats')}
        subtitle={<StatsHeaderSubtitle profileName={profileName} clubPage={clubPage} />}
        actions={headerAction}
      />
      <div className="panel__body stats-panel">
        {profileShots.length > 0 ? clubFilters : null}
        {profileShots.length === 0 ? (
          <div className="panel__body--empty">
            <span className="panel__empty-title">{t('stats.noShots')}</span>
            <span className="panel__empty-detail">{t('stats.noShotsDetail')}</span>
          </div>
        ) : clubPage ? (
          <StatsClubPage shots={profileShots} club={selectedClub} />
        ) : (
          <div className={`stats-panel__grid stats-panel__grid--of-${tiles.length}`}>
            {tiles.map((tile) => (
              <MetricCard
                key={tile.id}
                label={withLabelBreaks(tile.label)}
                value={tile.value}
                unit={tile.unit}
                title={tile.title}
                labelPosition="above"
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
