import { useEffect, useRef, useState } from 'react';
import { useDragScroll } from '../hooks/useDragScroll';
import { useI18n } from '../i18n/useI18n';
import { getHtmlLang } from '../i18n';
import { getClubName } from '../data/clubs';
import { useUnitPreference } from '../state/useUnitPreference';
import { formatNumber, formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit } from '../utils/units';
import { getServerOrigin } from '../utils/serverOrigin';
import { deleteSavedShot } from '../services/historyApi';
import { PanelAction } from './panel/PanelAction';
import { formatSpinCell } from './panel/liveMetrics';
import { carryTooltip, markTypicalCarry } from '../utils/carryDisplay';
import type { CameraReplay, FlightPath } from '../types/shot';
import { savedSessionShots, type SavedRangeSession } from '../virtual-range/savedShots';
import { CameraReplayDialog } from './CameraReplayDialog';
import { ConfirmDeleteButton } from './ConfirmDeleteButton';
import { useCameraReplayController } from '../hooks/useCameraReplayController';
import './SessionHistoryDialog.css';

export interface SavedShot {
  shot_number: number | null;
  timestamp: string;
  club: string;
  /** The API sends "" for a text field the log lacks. */
  profile_id?: string;
  profile_name: string;
  mode?: string;
  ball_speed_mph: number;
  club_speed_mph: number | null;
  estimated_carry_yards: number | null;
  carry_spin_adjusted: number | null;
  carry_lateral_yards?: number | null;
  /** Peak height of the flight, yards; absent from servers and logs that predate it. */
  apex_yards?: number | null;
  /** Only with ?flight_path=1; null when the shot has none. */
  flight_path?: FlightPath | null;
  /** Only with ?flight_path=1, like flight_path. */
  roll_path?: FlightPath | null;
  roll_yards?: number | null;
  total_yards?: number | null;
  launch_angle_vertical: number | null;
  spin_rpm: number | null;
  /** Absent from servers that predate it; the API sends "" for a source the log lacks. */
  carry_spin_rpm?: number | null;
  carry_spin_source?: string | null;
  camera_replay?: CameraReplay;
}

export interface SavedSession {
  id: string;
  started_at: string;
  shot_count: number;
  clubs: string[];
  shots: SavedShot[];
}

type HistoryResponse = { sessions: Omit<SavedSession, 'shots'>[] } | SavedSession;

function dateLabel(value: string) {
  return new Date(value).toLocaleString(getHtmlLang(), { dateStyle: 'medium', timeStyle: 'short' });
}

interface HistoryResult {
  data?: HistoryResponse;
  error?: boolean;
}

export interface SessionHistoryViewProps {
  sessionId: string | null;
  current?: HistoryResult;
  onOpenSession(sessionId: string): void;
  onBack(): void;
  onRefresh(): void;
  onClose(): void;
  /** Offered only where the app has a range to show the session on. */
  onShowOnRange?(session: SavedRangeSession): void;
  onDeleteShot?(shot: SavedShot): Promise<void>;
  keepScroll?: boolean;
}

export function SessionHistoryView({
  sessionId,
  current,
  onOpenSession,
  onBack,
  onRefresh,
  onClose,
  onShowOnRange,
  onDeleteShot,
  keepScroll = false,
}: SessionHistoryViewProps) {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const { activeReplay, openReplay, closeReplay, reportPlaybackError } = useCameraReplayController();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(scrollRef);
  const data = current?.data;
  const saved = data && 'shots' in data ? data : undefined;

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);

  useEffect(() => {
    if (!keepScroll && scrollRef.current) scrollRef.current.scrollTop = 0;
  }, [sessionId, current, keepScroll]);

  return (
    <dialog ref={dialogRef} className="session-history" onCancel={(event) => { if (activeReplay) { event.preventDefault(); closeReplay(); } else onClose(); }} aria-labelledby="history-title">
      <div className="session-history__header">
        <div>
          <h2 id="history-title">{t('history.title')}</h2>
          <p>{saved ? dateLabel(saved.started_at) : t('history.window')}</p>
        </div>
        <div className="session-history__actions">
          <PanelAction variant="secondary" onClick={() => window.location.assign('/?view=analysis')}>{t('analysis.title')}</PanelAction>
          {sessionId ? <PanelAction variant="secondary" onClick={onBack}>{t('history.back')}</PanelAction> : null}
          {saved && saved.shots.length > 0 && onShowOnRange ? (
            <PanelAction
              variant="secondary"
              onClick={() =>
                onShowOnRange({ label: dateLabel(saved.started_at), shots: savedSessionShots(saved.shots) })
              }
            >
              {t('history.showOnRange')}
            </PanelAction>
          ) : null}
          <PanelAction variant="secondary" onClick={onRefresh}>{t('history.refresh')}</PanelAction>
          <PanelAction onClick={onClose}>{t('history.close')}</PanelAction>
        </div>
      </div>
      <div className="session-history__scroll" ref={scrollRef} {...dragScroll} aria-busy={!current}>
        {!current ? <p role="status">{t('history.loading')}</p> : null}
        {current?.error ? <p role="alert">{t('history.error')}</p> : null}
        {data && 'sessions' in data ? (
          data.sessions.length ? data.sessions.map((session) => (
            <button className="session-history__session" key={session.id} onClick={() => onOpenSession(session.id)}>
              <span><strong>{dateLabel(session.started_at)}</strong><small>{session.clubs.map(getClubName).join(' · ')}</small></span>
              <span>{t('history.shots', { count: session.shot_count })} →</span>
            </button>
          )) : <p>{t('history.empty')}</p>
        ) : null}
        {saved ? (
          <table>
            <thead><tr>
              <th>#</th><th>{t('shots.colProfile')}</th>
              <th>{t('shots.colBall')}<small>{getSpeedUnit(unitSystem)}</small></th>
              <th>{t('shots.colClub')}<small>{getSpeedUnit(unitSystem)}</small></th>
              <th>{t('shots.colCarry')}<small>{getDistanceUnit(unitSystem)}</small></th>
              <th>{t('shots.colLaunch')}<small>°</small></th>
              <th>{t('shots.colSpin')}<small>rpm</small></th>
              <th>{t('replay.open')}</th>
              {onDeleteShot ? <th className="session-history__delete-cell" aria-label={t('shots.deleteShort')} /> : null}
            </tr></thead>
            <tbody>{saved.shots.map((shot, index) => (
              <tr key={`${shot.timestamp}:${index}`}>
                <td>{shot.shot_number ?? index + 1}</td>
                <td><strong>{getClubName(shot.club)}</strong><small>{shot.profile_name}</small></td>
                <td>{formatSpeed(shot.ball_speed_mph, unitSystem)}</td>
                <td>{shot.club_speed_mph === null ? '—' : formatSpeed(shot.club_speed_mph, unitSystem)}</td>
                <td title={carryTooltip(shot)}>{(shot.carry_spin_adjusted ?? shot.estimated_carry_yards) == null ? '—' : markTypicalCarry(shot, formatDistance((shot.carry_spin_adjusted ?? shot.estimated_carry_yards)!, unitSystem), { compact: true })}</td>
                <td>{shot.launch_angle_vertical == null ? '—' : formatNumber(shot.launch_angle_vertical, 1)}</td>
                <td>{formatSpinCell(shot)}</td>
                <td>{shot.camera_replay ? <button className="session-history__replay" onClick={() => openReplay(shot.camera_replay!)} aria-label={`${t('replay.open')} · ${t('range.shot', { n: shot.shot_number ?? index + 1 })}`}>▷ {t('replay.open')}</button> : <span aria-label={t('range.unavailable')}>—</span>}</td>
                {onDeleteShot ? <td className="session-history__delete-cell"><ConfirmDeleteButton className="session-history__delete" label={t('shots.deleteShort')} ariaLabel={t('shots.delete', { n: shot.shot_number ?? index + 1 })} onConfirm={() => onDeleteShot(shot)} /></td> : null}
              </tr>
            ))}</tbody>
          </table>
        ) : null}
      </div>
      <p className="session-history__note">{t('history.readOnly')}</p>
      {activeReplay ? <CameraReplayDialog key={activeReplay.replay.id} replay={activeReplay.replay} state={activeReplay.state} onClose={closeReplay} onRetry={() => openReplay(activeReplay.replay)} onPlaybackError={reportPlaybackError} /> : null}
    </dialog>
  );
}

type SessionHistoryDialogProps = Pick<SessionHistoryViewProps, 'onClose' | 'onShowOnRange'>;

export function SessionHistoryDialog({ onClose, onShowOnRange }: SessionHistoryDialogProps) {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [kept, setKept] = useState<string | null>(null);
  const [result, setResult] = useState<HistoryResult & { request: string }>();
  const requestKey = `${sessionId ?? 'list'}:${revision}`;
  const current = result?.request === requestKey || (kept !== null && result?.request === kept) ? result : undefined;
  // Only the range needs flight paths (~3 KB a shot); a boolean, as the App passes a new function every render.
  const withPaths = Boolean(onShowOnRange);

  useEffect(() => {
    const controller = new AbortController();
    const query = withPaths ? '?flight_path=1' : '';
    const path = sessionId ? `/api/session-history/${encodeURIComponent(sessionId)}${query}` : '/api/session-history';
    fetch(`${getServerOrigin()}${path}`, { signal: controller.signal, cache: 'no-store' })
      .then(async (response) => {
        if (!response.ok) throw new Error('History request failed');
        const value = await response.json() as HistoryResponse;
        if (!controller.signal.aborted) setResult({ request: requestKey, data: value });
      })
      .catch(() => {
        if (!controller.signal.aborted) setResult({ request: requestKey, error: true });
      });
    return () => controller.abort();
  }, [sessionId, requestKey, withPaths]);

  return (
    <SessionHistoryView
      sessionId={sessionId}
      current={current}
      onOpenSession={(id) => {
        setKept(null);
        setSessionId(id);
      }}
      onBack={() => {
        setKept(null);
        setSessionId(null);
      }}
      onRefresh={() => {
        setKept(null);
        setRevision((value) => value + 1);
      }}
      onClose={onClose}
      onShowOnRange={onShowOnRange}
      onDeleteShot={sessionId ? (shot) => deleteSavedShot(sessionId, shot.timestamp).then(() => {
        setKept(requestKey);
        setRevision((value) => value + 1);
      }) : undefined}
      keepScroll={kept !== null}
    />
  );
}
