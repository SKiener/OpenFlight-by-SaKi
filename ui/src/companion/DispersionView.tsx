import { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { getBagClubIds, getClubName } from '../data/clubs';
import { useDragScroll } from '../hooks/useDragScroll';
import { getHtmlLang, type MessageKey } from '../i18n';
import { useI18n } from '../i18n/useI18n';
import { useProfileStore } from '../stores/useProfileStore';
import { useSystemStore } from '../stores/useSystemStore';
import type { CalibrationStatus, Shot } from '../types/shot';
import {
  convertDistanceFromYards,
  convertSpeedFromMph,
  formatDistance,
  formatNumber,
  getDistanceUnit,
  getSpeedUnit,
  type UnitSystem,
} from '../utils/units';
import { CompanionMetrics, CompanionReplay } from './CompanionShotDetail';
import { DispersionPlot } from './DispersionPlot';
import {
  classifyShot,
  clubColorSlot,
  collectShots,
  compareClubsByLength,
  DISPERSION_RANGES,
  ELLIPSE_SIGMA,
  ellipseCoverage,
  isPoint,
  MIN_ELLIPSE_SHOTS,
  summarizeClubs,
  type ClubSummary,
  type DirectionDoubt,
  type DispersionPoint,
  type DispersionRange,
} from './dispersion';
import { useDispersionHistoryStore, type DispersionHistory } from './useDispersionHistoryStore';
import { isCurveIgnored } from '../utils/carryDisplay';
import './DispersionView.css';

const RANGE_LABELS: Record<DispersionRange, MessageKey> = {
  session: 'dispersion.range.session',
  '7d': 'dispersion.range.7d',
  '30d': 'dispersion.range.30d',
};
const DOUBT_LABELS: Record<DirectionDoubt, MessageKey> = {
  estimated: 'dispersion.doubt.estimated',
  experimental: 'dispersion.doubt.experimental',
  low_confidence: 'dispersion.doubt.lowConfidence',
  unknown_source: 'dispersion.doubt.unknownSource',
};

export interface DispersionViewProps {
  liveShots: Shot[];
  profileId: string;
  bagClubIds: readonly string[];
  unitSystem: UnitSystem;
  calibration: CalibrationStatus | null;
  history: DispersionHistory;
  onLoadHistory: (refresh?: boolean) => void;
  now: number;
}

type Translate = ReturnType<typeof useI18n>['t'];

function signed(value: number, digits: number): string {
  const rounded = Number(value.toFixed(digits)) || 0;
  return `${rounded > 0 ? '+' : ''}${formatNumber(rounded, digits)}`;
}

function lateralText(yards: number, unitSystem: UnitSystem, t: Translate): string {
  const distance = formatDistance(Math.abs(yards), unitSystem, 0);
  if (Number(distance) === 0) return t('dispersion.onTarget');
  return t(yards > 0 ? 'dispersion.right' : 'dispersion.left', { value: `${distance} ${getDistanceUnit(unitSystem)}` });
}

interface StatCellProps {
  stat: ClubSummary['carry'];
  convert: (value: number) => number;
  digits: number;
  signedMean?: boolean;
}

function StatCell({ stat, convert, digits, signedMean = false }: StatCellProps) {
  if (stat.mean === null) return <td>—</td>;
  const mean = convert(stat.mean);
  return (
    <td>
      <strong>{signedMean ? signed(mean, digits) : formatNumber(mean, digits)}</strong>
      <small>± {stat.sd === null ? '—' : formatNumber(convert(stat.sd), digits)}</small>
    </td>
  );
}

function DispersionTable({
  summaries,
  unitSystem,
  colorOf,
}: {
  summaries: ClubSummary[];
  unitSystem: UnitSystem;
  colorOf: (club: string) => string;
}) {
  const { t } = useI18n();
  const distance = (yards: number) => convertDistanceFromYards(yards, unitSystem);
  const speed = (mph: number) => convertSpeedFromMph(mph, unitSystem);
  const degrees = (value: number) => value;
  return (
    <table className="dispersion-table">
      <caption>{t('dispersion.table')}</caption>
      <thead>
        <tr>
          <th scope="col">
            {t('dispersion.carry')} <small>{getDistanceUnit(unitSystem)}</small>
          </th>
          <th scope="col">
            {t('dispersion.lateral')} <small>{getDistanceUnit(unitSystem)}</small>
          </th>
          <th scope="col">
            {t('dispersion.ball')} <small>{getSpeedUnit(unitSystem)}</small>
          </th>
          <th scope="col">
            <abbr title={t('metric.vLaunch')}>VLA</abbr> <small>°</small>
          </th>
          <th scope="col">
            <abbr title={t('metric.hLaunch')}>HLA</abbr> <small>°</small>
          </th>
        </tr>
      </thead>
      {summaries.map((summary) => (
        <tbody key={summary.club} data-club={summary.club}>
          <tr>
            <th scope="rowgroup" colSpan={5}>
              <span className="dispersion__swatch" style={{ color: colorOf(summary.club) }} aria-hidden="true" />
              {getClubName(summary.club)}
              <span className="dispersion-table__count">n = {summary.points.length}</span>
              {summary.excluded > 0 ? (
                <span className="dispersion-table__count">
                  {t('dispersion.notPlotted', { count: summary.excluded })}
                </span>
              ) : null}
            </th>
          </tr>
          <tr>
            <StatCell stat={summary.carry} convert={distance} digits={0} />
            <StatCell stat={summary.lateral} convert={distance} digits={0} signedMean />
            <StatCell stat={summary.ballSpeed} convert={speed} digits={1} />
            <StatCell stat={summary.vla} convert={degrees} digits={1} />
            <StatCell stat={summary.hla} convert={degrees} digits={1} signedMean />
          </tr>
        </tbody>
      ))}
    </table>
  );
}

function DispersionShotDialog({
  point,
  unitSystem,
  onClose,
}: {
  point: DispersionPoint;
  unitSystem: UnitSystem;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const caveats = [
    point.lateralDerived ? t('dispersion.derived') : null,
    point.directionDoubt ? t(DOUBT_LABELS[point.directionDoubt]) : null,
    isCurveIgnored(point.shot) ? t('carry.curveIgnored') : null,
  ].filter((caveat) => caveat !== null);

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="dispersion-dialog"
      aria-label={t('dispersion.detail')}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      <header className="dispersion-dialog__header">
        <div>
          <h2>{getClubName(point.club)}</h2>
          <p>
            {new Date(point.shot.timestamp).toLocaleString(getHtmlLang(), { dateStyle: 'short', timeStyle: 'short' })}
          </p>
        </div>
        <button type="button" className="companion-button" onClick={onClose}>
          {t('history.close')}
        </button>
      </header>
      <p className="dispersion-dialog__lateral">
        {[t('dispersion.lateralLine', { value: lateralText(point.lateralYards, unitSystem, t) }), ...caveats].join(
          ' · '
        )}
      </p>
      <CompanionMetrics shot={point.shot} unitSystem={unitSystem} />
      <CompanionReplay shot={point.shot} awaitingCamera={false} />
    </dialog>
  );
}

export function DispersionView({
  liveShots,
  profileId,
  bagClubIds,
  unitSystem,
  calibration,
  history,
  onLoadHistory,
  now,
}: DispersionViewProps) {
  const { t } = useI18n();
  const [range, setRange] = useState<DispersionRange>('session');
  const [club, setClub] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const chipRef = useRef<HTMLDivElement>(null);
  const chipScroll = useDragScroll(chipRef, 'x');

  const entries = collectShots({ liveShots, sessions: history.sessions, profileId, range, now }).map(classifyShot);
  const clubs = [...new Set(entries.map((entry) => entry.club))].sort(compareClubsByLength);
  const activeClub = club !== null && clubs.includes(club) ? club : null;
  const visible = activeClub === null ? entries : entries.filter((entry) => entry.club === activeClub);
  const summaries = summarizeClubs(visible);
  const points = visible.filter(isPoint);
  const excluded = (reason: 'no_direction' | 'no_carry') =>
    visible.filter((entry) => !isPoint(entry) && entry.reason === reason).length;
  const substituteSpin = points.filter((point) => point.substituteSpin).length;
  const selected = points.find((point) => point.key === selectedKey) ?? null;
  const usesHistory = range !== 'session';
  const colorOf = (option: string) => {
    const slot = clubColorSlot(option, bagClubIds);
    return slot === null ? 'var(--dispersion-other)' : `var(--dispersion-${slot + 1})`;
  };
  const describe = (point: DispersionPoint) =>
    t('dispersion.point', {
      club: getClubName(point.club),
      carry: `${formatDistance(point.carryYards, unitSystem, 0)} ${getDistanceUnit(unitSystem)}`,
      lateral: lateralText(point.lateralYards, unitSystem, t),
    });
  const selectRange = (next: DispersionRange) => {
    setRange(next);
    if (next !== 'session') onLoadHistory();
  };
  const notes = [
    usesHistory && history.status === 'loading' ? t('dispersion.historyLoading') : null,
    usesHistory && history.unavailable.length > 0
      ? t('dispersion.historyPartial', { count: history.unavailable.length })
      : null,
    substituteSpin > 0 ? t('dispersion.substituteSpin', { count: substituteSpin, total: points.length }) : null,
    excluded('no_direction') > 0 ? t('dispersion.noDirection', { count: excluded('no_direction') }) : null,
    excluded('no_carry') > 0 ? t('dispersion.noCarry', { count: excluded('no_carry') }) : null,
  ].filter((note) => note !== null);

  return (
    <section className="dispersion" aria-label={t('companion.dispersion')}>
      <div className="dispersion__ranges" role="group" aria-label={t('dispersion.range')}>
        {DISPERSION_RANGES.map((option) => (
          <button
            key={option}
            type="button"
            className={`companion-button${range === option ? ' companion-button--active' : ''}`}
            aria-pressed={range === option}
            onClick={() => selectRange(option)}
          >
            {t(RANGE_LABELS[option])}
          </button>
        ))}
      </div>
      {clubs.length > 0 ? (
        <div className="dispersion__clubs" role="group" aria-label={t('dispersion.clubs')}>
          <button
            type="button"
            className={`companion-button${activeClub === null ? ' companion-button--active' : ''}`}
            aria-pressed={activeClub === null}
            onClick={() => setClub(null)}
          >
            {t('stats.all', { count: entries.length })}
          </button>
          <div ref={chipRef} className="dispersion__chip-scroll" {...chipScroll}>
            {clubs.map((option) => (
              <button
                key={option}
                type="button"
                className={`companion-button${activeClub === option ? ' companion-button--active' : ''}`}
                aria-pressed={activeClub === option}
                onClick={() => setClub(option)}
              >
                <span className="dispersion__swatch" style={{ color: colorOf(option) }} aria-hidden="true" />
                {getClubName(option)} ({entries.filter((entry) => entry.club === option).length})
              </button>
            ))}
          </div>
        </div>
      ) : null}
      <div className="dispersion__notes">
        {calibration?.horizontal_phase_reference ? null : (
          <p className="dispersion__warning" role="note">
            {t('dispersion.uncalibrated')}
          </p>
        )}
        {usesHistory && history.status === 'error' ? (
          <p className="dispersion__warning" role="alert">
            {t('dispersion.historyError')}{' '}
            <button type="button" className="companion-button" onClick={() => onLoadHistory(true)}>
              {t('history.refresh')}
            </button>
          </p>
        ) : null}
        {notes.map((note) => (
          <p key={note}>{note}</p>
        ))}
      </div>
      {points.length > 0 ? (
        <div className="dispersion__body">
          <figure className="dispersion__figure">
            <DispersionPlot
              points={points}
              summaries={summaries}
              unitSystem={unitSystem}
              colorOf={colorOf}
              nameOf={getClubName}
              describe={describe}
              onSelect={setSelectedKey}
            />
            <figcaption className="dispersion__legend">
              <ul>
                {summaries
                  .filter((summary) => summary.points.length > 0)
                  .map((summary) => (
                    <li key={summary.club}>
                      <span
                        className="dispersion__swatch"
                        style={{ color: colorOf(summary.club) }}
                        aria-hidden="true"
                      />
                      {getClubName(summary.club)}
                    </li>
                  ))}
              </ul>
              <ul>
                <li className="dispersion__key dispersion__key--filled">{t('dispersion.legendFilled')}</li>
                <li className="dispersion__key dispersion__key--hollow">{t('dispersion.legendHollow')}</li>
                <li className="dispersion__key dispersion__key--ellipse">
                  {t('dispersion.legendEllipse', {
                    percent: Math.round(ellipseCoverage(ELLIPSE_SIGMA) * 100),
                    min: MIN_ELLIPSE_SHOTS,
                  })}
                </li>
                <li className="dispersion__key dispersion__key--mean">{t('dispersion.legendMean')}</li>
              </ul>
            </figcaption>
          </figure>
          <DispersionTable summaries={summaries} unitSystem={unitSystem} colorOf={colorOf} />
        </div>
      ) : (
        <p className="dispersion__empty">{t('dispersion.empty')}</p>
      )}
      {selected ? (
        <DispersionShotDialog
          key={selected.key}
          point={selected}
          unitSystem={unitSystem}
          onClose={() => setSelectedKey(null)}
        />
      ) : null}
    </section>
  );
}

export function DispersionTab({
  liveShots,
  profileId,
  unitSystem,
}: {
  liveShots: Shot[];
  profileId: string;
  unitSystem: UnitSystem;
}) {
  const calibration = useSystemStore((state) => state.calibration);
  const settings = useProfileStore((state) => state.profiles.find((profile) => profile.id === profileId)?.settings);
  const { load, ...history } = useDispersionHistoryStore(
    useShallow((state) => ({
      status: state.status,
      sessions: state.sessions,
      unavailable: state.unavailable,
      load: state.load,
    }))
  );
  const [now] = useState(() => Date.now());
  return (
    <DispersionView
      liveShots={liveShots}
      profileId={profileId}
      bagClubIds={getBagClubIds(settings)}
      unitSystem={unitSystem}
      calibration={calibration}
      history={history}
      onLoadHistory={(refresh) => void load(refresh)}
      now={now}
    />
  );
}
