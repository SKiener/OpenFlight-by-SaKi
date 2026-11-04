import { useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { getClubName } from '../../data/clubs';
import type { Shot } from '../../types/shot';
import { filterShotsByProfile, getCarryYards, getSwingSpeedMph, isSwingSpeedShot } from '../../types/shot';
import { useDragScroll } from '../../hooks/useDragScroll';
import { useUnitPreference } from '../../state/useUnitPreference';
import { demoRowsMayShow, useShotStore } from '../../stores/useShotStore';
import { useSystemStore } from '../../stores/useSystemStore';
import { getEmptyValidationEntry, useValidationStore, type ValidationEntry } from '../../stores/useValidationStore';
import { socketService } from '../../services/socketService';
import type { UnitSystem } from '../../utils/units';
import { formatNumber, formatDistance, formatSpeed } from '../../utils/units';
import { buildValidationCsv, comparatorDifference, downloadCsv } from '../../utils/validationCsv';
import { PanelHeader } from './PanelHeader';
import { PanelAction } from './PanelAction';
import { formatSpinCell } from './liveMetrics';
import { withLabelBreaks } from './labelBreaks';
import { carryTooltip, markTypicalCarry } from '../../utils/carryDisplay';
import { clubDataSummaryParts } from './clubMetrics';
import { useI18n } from '../../i18n/useI18n';
import { ConfirmDeleteButton } from '../ConfirmDeleteButton';
import { CarryNotes } from '../ui/CarryNotes';

interface ShotsPanelProps {
  shots: Shot[];
  profileId: string;
  profileName: string;
  clubLabel?: string;
  onDeleteShot: (timestamp: string) => Promise<void> | void;
  onReplayShot?: (shot: Shot) => void;
}

const COMPARATOR_DEVICES = ['Stack Radar', 'PRGR', 'TrackMan', 'Full Swing', 'Other'];

function optionalNumber(value: number | null | undefined, digits = 1, signed = false): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const prefix = signed && value >= 0 ? '+' : '';
  return `${prefix}${formatNumber(value, digits)}`;
}

/** The five numeric columns of a row, in the order design doc 7b draws them. */
function rowValues(shot: Shot, unitSystem: UnitSystem): string[] {
  if (isSwingSpeedShot(shot)) {
    // A swing-speed shot has no ball flight; reuse the columns for its own data
    // so the grid stays aligned with ball-strike rows.
    return [
      formatSpeed(getSwingSpeedMph(shot), unitSystem, 1),
      '—',
      optionalNumber(shot.swing_speed_reading_count, 0),
      optionalNumber(shot.swing_speed_trigger_mph),
      optionalNumber(shot.swing_speed_duration_ms, 0),
    ];
  }

  return [
    formatSpeed(shot.ball_speed_mph, unitSystem, 1),
    shot.club_speed_mph === null ? '—' : formatSpeed(shot.club_speed_mph, unitSystem, 1),
    optionalNumber(shot.launch_angle_vertical),
    formatSpinCell(shot),
    markTypicalCarry(shot, formatDistance(getCarryYards(shot), unitSystem, 0), { compact: true }),
  ];
}

/**
 * One wrapped line of the shot's calculated club data, marked ≈ "calculated".
 * Nothing when the shot has none (older servers, woods without a face, skipped).
 */
export function ClubDataSummary({ shot }: { shot: Shot }) {
  const { t } = useI18n();
  const parts = clubDataSummaryParts(shot);
  if (parts.length === 0) return null;
  return (
    <p className="shots-panel__club-data">
      <span className="shots-panel__club-data-mark">
        <span aria-hidden="true">≈</span> {t('metric.calculated')}:
      </span>{' '}
      {parts.join(' · ')}
    </p>
  );
}

export function ValidationEditor({
  shot,
  entry,
  onUpdate,
  disabled,
}: {
  shot: Shot;
  entry: ValidationEntry;
  onUpdate: (timestamp: string, patch: Partial<ValidationEntry>) => void;
  disabled: boolean;
}) {
  const { t } = useI18n();
  const difference = comparatorDifference(shot, entry);

  return (
    <div className="shots-panel__validation">
      <label className="shots-panel__field">
        <span>{t('shots.device')}</span>
        <select
          value={entry.comparatorDevice}
          disabled={disabled}
          onChange={(event) => onUpdate(shot.timestamp, { comparatorDevice: event.target.value })}
        >
          <option value="">{t('shots.device')}</option>
          {COMPARATOR_DEVICES.map((device) => (
            <option key={device} value={device}>
              {device}
            </option>
          ))}
        </select>
      </label>
      <label className="shots-panel__field">
        <span>{t('shots.comparator')}</span>
        <input
          type="number"
          inputMode="decimal"
          min="0"
          step="0.1"
          placeholder="mph"
          value={entry.comparatorSpeed}
          disabled={disabled}
          onChange={(event) => onUpdate(shot.timestamp, { comparatorSpeed: event.target.value })}
        />
      </label>
      <div className="shots-panel__field shots-panel__field--diff">
        <span>{t('shots.diff')}</span>
        <strong>{difference === null ? '—' : `${difference >= 0 ? '+' : ''}${formatNumber(difference, 1)}`}</strong>
      </div>
      <label className="shots-panel__field shots-panel__field--notes">
        <span>{t('shots.notes')}</span>
        <input
          type="text"
          placeholder={t('shots.notesPlaceholder')}
          value={entry.notes}
          disabled={disabled}
          onChange={(event) => onUpdate(shot.timestamp, { notes: event.target.value })}
        />
      </label>
    </div>
  );
}

/**
 * Design doc 7b: session log as a hairline table with Upload / Export in the
 * header.
 *
 * 7b has no room for the per-shot validation fields the old ShotList showed
 * inline, so a row expands on tap to reveal them — the mockup's own "make the
 * shot rows tappable to open shot detail" follow-up.
 */
export function ShotsPanel({ shots, profileId, profileName, clubLabel, onDeleteShot, onReplayShot }: ShotsPanelProps) {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const { entries, updateEntry, removeEntry } = useValidationStore();
  const [expanded, setExpanded] = useState<string | null>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(listRef);
  const { cloudUploadState, cloudUploadMessage, demoActive } = useSystemStore(
    useShallow((state) => ({
      cloudUploadState: state.cloudUploadState,
      cloudUploadMessage: state.cloudUploadMessage,
      demoActive: state.demoActive,
    }))
  );
  const sessionDemo = useShotStore((state) => state.sessionDemo);
  const demoRows = demoRowsMayShow(demoActive, sessionDemo);

  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const visibleShots = useMemo(() => [...profileShots].reverse(), [profileShots]);
  const validatedCount = useMemo(
    () => profileShots.filter((shot) => entries[shot.timestamp]?.comparatorSpeed).length,
    [entries, profileShots]
  );

  const handleExport = () => {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    downloadCsv(`openflight-validation-${stamp}.csv`, buildValidationCsv(profileShots, entries));
  };

  const handleDelete = async (timestamp: string) => {
    await onDeleteShot(timestamp);
    removeEntry(timestamp);
  };

  const header = (
    <PanelHeader
      title={t('nav.shots')}
      subtitle={
        profileShots.length === 0
          ? profileName
          : cloudUploadMessage ||
            t('shots.recorded', {
              count: profileShots.length,
              validated: validatedCount,
              total: profileShots.length,
            })
      }
      club={clubLabel}
      actions={
        <>
          <PanelAction
            variant="secondary"
            disabled={cloudUploadState === 'running' || profileShots.length === 0}
            onClick={() => socketService.uploadCloud()}
          >
            {cloudUploadState === 'running' ? t('shots.uploading') : t('shots.uploadCloud')}
          </PanelAction>
          <PanelAction variant="primary" disabled={demoRows || profileShots.length === 0} onClick={handleExport}>
            {t('shots.exportCsv')}
          </PanelAction>
        </>
      }
    />
  );

  if (profileShots.length === 0) {
    return (
      <div className="panel shots-panel">
        {header}
        <div className="panel__body panel__body--empty">
          <span className="panel__empty-title">{t('shots.noShots')}</span>
          <span className="panel__empty-detail">{t('shots.noShotsDetail')}</span>
        </div>
      </div>
    );
  }

  return (
    <div className="panel shots-panel">
      {header}
      <div className="shots-panel__columns" role="presentation">
        <span>{t('shots.colShot')}</span>
        <span>{t('shots.colProfile')}</span>
        <span className="shots-panel__num">{t('shots.colBall')}</span>
        <span className="shots-panel__num">{t('shots.colClub')}</span>
        <span className="shots-panel__num">{t('shots.colLaunch')}</span>
        <span className="shots-panel__num">{t('shots.colSpin')}</span>
        <span className="shots-panel__num">{withLabelBreaks(t('shots.colCarry'))}</span>
        <span />
      </div>
      <div
        className="panel__body shots-panel__rows"
        role="region"
        aria-label={t('shots.recordedAria')}
        ref={listRef}
        onPointerDown={dragScroll.onPointerDown}
        onPointerMove={dragScroll.onPointerMove}
        onPointerUp={dragScroll.onPointerUp}
        onPointerCancel={dragScroll.onPointerCancel}
        onClickCapture={dragScroll.onClickCapture}
      >
        {visibleShots.map((shot, index) => {
          const shotNumber = profileShots.length - index;
          const entry = entries[shot.timestamp] ?? getEmptyValidationEntry();
          const isOpen = expanded === shot.timestamp;
          const [ball, club, launch, spin, carry] = rowValues(shot, unitSystem);

          return (
            <div className="shots-panel__row-group" key={shot.timestamp}>
              <div className="shots-panel__row">
                <button
                  type="button"
                  className="shots-panel__row-main"
                  aria-expanded={isOpen}
                  onClick={() => setExpanded(isOpen ? null : shot.timestamp)}
                >
                  <span className="shots-panel__index">{shotNumber}</span>
                  <span className="shots-panel__profile">
                    <span className="shots-panel__profile-name">{profileName}</span>
                    <span className="shots-panel__profile-club">
                      {shot.training_implement_label ?? getClubName(shot.club)}
                    </span>
                  </span>
                  <span className="shots-panel__num shots-panel__value">{ball}</span>
                  <span className="shots-panel__num shots-panel__value">{club}</span>
                  <span className="shots-panel__num shots-panel__value">{launch}</span>
                  <span className="shots-panel__num shots-panel__value">{spin}</span>
                  <span
                    className="shots-panel__num shots-panel__value shots-panel__value--accent"
                    title={isSwingSpeedShot(shot) ? undefined : carryTooltip(shot)}
                  >
                    {carry}
                  </span>
                </button>
                <div className="shots-panel__actions">
                  {shot.camera_replay && onReplayShot ? (
                    <button
                      type="button"
                      className="shots-panel__replay"
                      aria-label={t('replay.shot', { n: shotNumber })}
                      onClick={() => onReplayShot(shot)}
                    >
                      <span aria-hidden="true">▶</span>
                    </button>
                  ) : null}
                  <ConfirmDeleteButton
                    className="shots-panel__delete"
                    label={t('shots.deleteShort')}
                    ariaLabel={t('shots.delete', { n: shotNumber })}
                    onConfirm={() => handleDelete(shot.timestamp)}
                  />
                </div>
              </div>
              {isOpen ? (
                <>
                  {isSwingSpeedShot(shot) ? null : (
                    <>
                      <CarryNotes shot={shot} className="shots-panel__carry-notes" />
                      <ClubDataSummary shot={shot} />
                    </>
                  )}
                  <ValidationEditor shot={shot} entry={entry} onUpdate={updateEntry} disabled={demoRows} />
                </>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
