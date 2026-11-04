import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../i18n/useI18n';
import { getHtmlLang } from '../i18n';
import { useLocaleStore } from '../stores/useLocaleStore';
import { useThemeStore } from '../stores/useThemeStore';
import { useUnitPreference } from '../state/useUnitPreference';
import { useDragScroll } from '../hooks/useDragScroll';
import { getClubName } from '../data/clubs';
import { convertDistanceFromYards, formatDistance, formatNumber, formatSpeed, getDistanceUnit, getSpeedUnit, withDistanceUnit, withSpeedUnit, type UnitSystem } from '../utils/units';
import { carryTooltip, isTypicalSpinCarry, markTypicalCarry } from '../utils/carryDisplay';
import { CameraReplayDialog } from '../components/CameraReplayDialog';
import { useCameraReplayController } from '../hooks/useCameraReplayController';
import { prepareCameraReplay } from '../services/cameraReplay';
import { AnalysisChart } from './AnalysisChart';
import { loadSessions } from './load';
import { carry, clubStatistics, confirmedSpin, emptyFilters, filterShots, finite, flattenSessions, profileKey, shotsCsv, statistics, statsCsv, type AnalysisShot, type Filters } from './data';
import '../components/range/range.css';
import './analysis.css';

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** The shot table's carry cell: ≈ on a typical-spin carry, the typical spin in its tooltip. */
export function AnalysisCarryCell({ shot, unitSystem }: { shot: AnalysisShot; unitSystem: UnitSystem }) {
  const { t } = useI18n();
  const value = carry(shot);
  return <td title={carryTooltip(shot)}>{finite(value) ? markTypicalCarry(shot, formatDistance(value, unitSystem, 1)) : '—'}<small>{finite(shot.carry_spin_adjusted) ? t('analysis.final') : t('analysis.provisional')}</small></td>;
}

/** The detail pane's carry: ≈ and "with typical spin" on a typical-spin carry. */
export function AnalysisCarryDetail({ shot, unitSystem }: { shot: AnalysisShot; unitSystem: UnitSystem }) {
  const { t } = useI18n();
  const value = carry(shot);
  const formatted = finite(value) ? `${formatDistance(value, unitSystem, 1)} ${getDistanceUnit(unitSystem)}` : '—';
  return <div title={carryTooltip(shot)}><dt>Carry</dt><dd>{finite(value) ? markTypicalCarry(shot, formatted) : formatted}<small>{finite(shot.carry_spin_adjusted) ? t('analysis.final') : t('analysis.provisional')}</small>{isTypicalSpinCarry(shot) ? <small>{t('carry.typicalSpin')}</small> : null}<small>{t(shot.carry_spin_source === 'measured' ? 'analysis.measured' : shot.carry_spin_source ? 'analysis.unconfirmed' : 'analysis.unknownSource')}</small></dd></div>;
}

export default function AnalysisPage() {
  const { t, locale } = useI18n();
  const { setLocale } = useLocaleStore();
  const { theme, setTheme } = useThemeStore();
  const { unitSystem, setUnitSystem } = useUnitPreference();
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<{ revision: number; data?: Awaited<ReturnType<typeof loadSessions>>; error?: boolean }>();
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const [sort, setSort] = useState('chronological');
  const [page, setPage] = useState(0);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [videoStatus, setVideoStatus] = useState<{ busy: boolean; error?: boolean }>({ busy: false });
  const downloadRequest = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLElement>(null);
  const details = useRef<HTMLElement>(null);
  const drag = useDragScroll(scroller);
  const { activeReplay, openReplay, closeReplay, reportPlaybackError } = useCameraReplayController();
  const current = result?.revision === revision ? result : undefined;
  const allShots = useMemo(() => flattenSessions(current?.data?.sessions ?? []), [current]);
  const shots = useMemo(() => filterShots(allShots, filters), [allShots, filters]);
  const stats = useMemo(() => statistics(shots), [shots]);
  const groups = useMemo(() => clubStatistics(shots), [shots]);
  const selected = shots.find((shot) => shot.key === selectedKey);
  const sorted = useMemo(() => [...shots].sort((a, b) => {
    if (sort === 'newest') return new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime();
    if (sort === 'longest') return (carry(b) ?? -Infinity) - (carry(a) ?? -Infinity);
    if (sort === 'fastest') return b.ball_speed_mph - a.ball_speed_mph;
    return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime();
  }), [shots, sort]);
  const pages = Math.max(1, Math.ceil(sorted.length / 50));
  const currentPage = Math.min(page, pages - 1);
  const distance = (value: number | null | undefined, digits = 1) => finite(value) ? `${formatDistance(value, unitSystem, digits)} ${getDistanceUnit(unitSystem)}` : '—';
  const speed = (value: number | null | undefined) => finite(value) ? `${formatSpeed(value, unitSystem)} ${getSpeedUnit(unitSystem)}` : '—';
  const date = (value: string) => new Date(value).toLocaleString(getHtmlLang(), { dateStyle: 'short', timeStyle: 'short' });
  const changeFilter = (key: keyof Filters, value: string) => { setFilters((previous) => ({ ...previous, [key]: value })); setPage(0); setSelectedKey(null); };

  useEffect(() => {
    const controller = new AbortController();
    void loadSessions(controller.signal).then((data) => {
      if (!controller.signal.aborted) setResult({ revision, data });
    }).catch(() => { if (!controller.signal.aborted) setResult({ revision, error: true }); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => () => downloadRequest.current?.abort(), []);
  useEffect(() => {
    if (!activeReplay) return;
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.preventDefault(); closeReplay(); } };
    window.addEventListener('keydown', escape);
    return () => window.removeEventListener('keydown', escape);
  }, [activeReplay, closeReplay]);
  useEffect(() => { if (selectedKey) details.current?.scrollIntoView({ block: 'nearest' }); }, [selectedKey]);

  const downloadVideo = async (shot: AnalysisShot) => {
    if (!shot.camera_replay || videoStatus.busy) return;
    const controller = new AbortController();
    downloadRequest.current = controller;
    setVideoStatus({ busy: true });
    try {
      const video = await prepareCameraReplay(shot.camera_replay.id, controller.signal);
      const response = await fetch(video.videoUrl, { signal: controller.signal });
      if (!response.ok) throw new Error('Video unavailable');
      const blob = await response.blob();
      if (!controller.signal.aborted) {
        download(blob, `openflight-${shot.session_id.replace(/[^\w-]/g, '')}-shot-${shot.shot_number ?? 0}.mp4`);
        setVideoStatus({ busy: false });
      }
    } catch { if (!controller.signal.aborted) setVideoStatus({ busy: false, error: true }); }
  };
  const exportData = (kind: 'shots' | 'stats' | 'json') => {
    const content = kind === 'json' ? JSON.stringify({ schema_version: 1, exported_at: new Date().toISOString(), visible_days: 30, filters,
      unavailable_sessions: current?.data?.unavailable ?? [], display_units: unitSystem,
      canonical_units: { speed: 'mph', distance: 'yards', angles: 'degrees', spin: 'rpm' },
      notes: 'Carry is estimated. Experimental/calc spin is excluded from confirmed-spin statistics. Raw fields retain canonical units. Videos download separately.',
      statistics_by_club: groups, shots }, null, 2) : kind === 'shots' ? shotsCsv(shots, unitSystem) : statsCsv(shots, unitSystem);
    download(new Blob([content], { type: kind === 'json' ? 'application/json' : 'text/csv;charset=utf-8' }), `openflight-${kind}-${new Date().toISOString().slice(0, 10)}.${kind === 'json' ? 'json' : 'csv'}`);
  };

  return <div className="analysis-page">
    <header className="analysis-header">
      <div><strong>OpenFlight</strong><h1>{t('analysis.title')}</h1></div>
      <nav aria-label={t('analysis.title')}>
        <a href="/">← {t('analysis.back')}</a>
        <button onClick={() => setLocale(locale === 'de' ? 'en' : 'de')} aria-label="Deutsch / English">{locale === 'de' ? 'EN' : 'DE'}</button>
        <button onClick={() => setTheme(theme === 'dark' ? 'sun' : 'dark')} aria-label={t('analysis.theme')}>{theme === 'dark' ? '☀' : '☾'}</button>
      </nav>
    </header>
    <main ref={scroller} {...drag} className="analysis-scroll" aria-busy={!current}>
      <div className="analysis-intro"><div><p>{t('analysis.subtitle')}</p><small>{t('analysis.local')}</small></div>
        <button onClick={() => setRevision((value) => value + 1)} disabled={!current}>{t('history.refresh')}</button></div>
      {!current ? <p role="status">{t('history.loading')}</p> : null}
      {current?.error ? <p role="alert">{t('history.error')}</p> : null}
      {!!current?.data?.unavailable.length && <p className="analysis-warning" role="alert">{t('analysis.partial', { n: current.data.unavailable.length })}</p>}
      {current?.data ? <>
        <section className="analysis-filters analysis-card" aria-label={t('analysis.reset')}>
          <label>{t('analysis.session')}<select value={filters.session} onChange={(event) => changeFilter('session', event.target.value)}><option value="">{t('analysis.all')}</option>{current.data.sessions.map((session) => <option key={session.id} value={session.id}>{date(session.started_at)} · {session.shot_count}</option>)}</select></label>
          <label>{t('analysis.club')}<select value={filters.club} onChange={(event) => changeFilter('club', event.target.value)}><option value="">{t('analysis.all')}</option>{[...new Set(allShots.map((shot) => shot.club))].map((club) => <option key={club} value={club}>{getClubName(club) || '—'}</option>)}</select></label>
          <label>{t('analysis.profile')}<select value={filters.profile} onChange={(event) => changeFilter('profile', event.target.value)}><option value="">{t('analysis.all')}</option>{[...new Map(allShots.filter((shot) => profileKey(shot)).map((shot) => [profileKey(shot), shot.profile_name || profileKey(shot)])).entries()].map(([id, name]) => <option key={id} value={id}>{name}</option>)}</select></label>
          <label>{t('analysis.from')}<input type="date" value={filters.from} onChange={(event) => changeFilter('from', event.target.value)} /></label>
          <label>{t('analysis.to')}<input type="date" value={filters.to} onChange={(event) => changeFilter('to', event.target.value)} /></label>
          <label>{t('units.speed')}<select value={getSpeedUnit(unitSystem)} onChange={(event) => setUnitSystem(withSpeedUnit(unitSystem, event.target.value as 'mph' | 'km/h'))}><option>km/h</option><option>mph</option></select></label>
          <label>{t('units.distance')}<select value={getDistanceUnit(unitSystem)} onChange={(event) => setUnitSystem(withDistanceUnit(unitSystem, event.target.value as 'm' | 'yds'))}><option>m</option><option>yds</option></select></label>
          <button onClick={() => { setFilters(emptyFilters); setPage(0); }}>{t('analysis.reset')}</button>
        </section>
        {filters.from && filters.to && filters.from > filters.to ? <p role="alert">{t('analysis.dateError')}</p> : null}
        <div className="analysis-kpis">
          <section className="analysis-card"><h2>{t('analysis.shots')}</h2><strong data-testid="shot-count">{stats.count}</strong><small>{t('analysis.videos', { n: stats.videos })}</small></section>
          <section className="analysis-card"><h2>{t('analysis.meanCarry')}</h2><strong data-testid="mean-carry">{distance(stats.carry.mean)}</strong><small>{t('analysis.median')}: {distance(stats.carry.median)} · n={stats.carry.n}</small></section>
          <section className="analysis-card"><h2>{t('analysis.speed')}</h2><strong>{speed(stats.speed.mean)}</strong><small>{t('analysis.sample', { n: stats.speed.n })}</small></section>
          <section className="analysis-card"><h2>{t('analysis.spread')}</h2><strong>{distance(stats.carry.sd)}</strong><small>{t('analysis.sample', { n: stats.carry.n })}</small></section>
        </div>
        <p className="analysis-note">{t('analysis.chartNote')}</p>
        {shots.length ? <div className="analysis-charts">
          <AnalysisChart title={t('analysis.trend')} unit={getDistanceUnit(unitSystem)} kind="line" labels={shots.map((shot) => `${date(shot.timestamp)} · #${shot.shot_number ?? '—'}`)} values={shots.map((shot) => { const value = carry(shot); return value == null ? null : convertDistanceFromYards(value, unitSystem); })} />
          <AnalysisChart title={t('analysis.comparison')} unit={getDistanceUnit(unitSystem)} kind="bar" labels={groups.map((group) => `${getClubName(group.club)} (n=${group.carry.n})`)} values={groups.map((group) => group.carry.mean == null ? null : convertDistanceFromYards(group.carry.mean, unitSystem))} />
        </div> : <p role="status">{t('analysis.empty')}</p>}
        <section className="analysis-card analysis-exports"><h2>{t('analysis.downloads')}</h2><div className="analysis-actions">
          <button disabled={!shots.length} onClick={() => exportData('shots')}>{t('analysis.shotsCsv')}</button>
          <button disabled={!shots.length} onClick={() => exportData('stats')}>{t('analysis.statsCsv')}</button>
          <button disabled={!shots.length} onClick={() => exportData('json')}>{t('analysis.json')}</button>
        </div><p>{t('analysis.qualityNote')}</p></section>
        <section className="analysis-card analysis-shots">
          <div className="analysis-table-header"><h2>{t('analysis.shots')} ({shots.length})</h2><label>{t('analysis.sort')}<select value={sort} onChange={(event) => { setSort(event.target.value); setPage(0); }}>{(['chronological', 'newest', 'longest', 'fastest'] as const).map((key) => <option key={key} value={key}>{t(`analysis.${key}`)}</option>)}</select></label></div>
          <table><thead><tr><th>{t('analysis.session')} / #</th><th>{t('analysis.club')}</th><th>Carry <small>{getDistanceUnit(unitSystem)}</small></th><th>{t('analysis.speed')} <small>{getSpeedUnit(unitSystem)}</small></th><th>{t('analysis.detail')}</th></tr></thead>
            <tbody>{sorted.slice(currentPage * 50, currentPage * 50 + 50).map((shot) => <tr key={shot.key} aria-selected={shot.key === selectedKey}>
              <td><strong>#{shot.shot_number ?? '—'}</strong><small>{date(shot.timestamp)}</small></td><td>{getClubName(shot.club)}<small>{shot.profile_name}</small></td>
              <AnalysisCarryCell shot={shot} unitSystem={unitSystem} />
              <td>{formatSpeed(shot.ball_speed_mph, unitSystem)}</td>
              <td><button onClick={() => setSelectedKey(shot.key)} aria-label={`${t('analysis.detail')} #${shot.shot_number}`} title={t('analysis.detail')}><span className="analysis-detail-label">{shot.camera_replay ? '▷ ' : ''}{t('analysis.detail')}</span><span className="analysis-detail-icon" aria-hidden="true">▷</span></button></td>
            </tr>)}</tbody></table>
          <div className="analysis-pagination"><button disabled={!currentPage} onClick={() => setPage(currentPage - 1)}>{t('analysis.previous')}</button><span>{t('analysis.page', { page: currentPage + 1, pages })}</span><button disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>{t('analysis.next')}</button></div>
        </section>
        {selected ? <section className="analysis-card analysis-detail" ref={details} aria-label={t('analysis.detail')}>
          <h2>{t('analysis.detail')} #{selected.shot_number} · {getClubName(selected.club)}</h2><p>{date(selected.timestamp)} · {selected.profile_name}</p>
          <dl>
            <AnalysisCarryDetail shot={selected} unitSystem={unitSystem} />
            <div><dt>{t('analysis.speed')}</dt><dd>{speed(selected.ball_speed_mph)}</dd></div>
            <div><dt>{t('analysis.launch')}</dt><dd>{finite(selected.launch_angle_vertical) ? `${formatNumber(selected.launch_angle_vertical, 1)}°` : '—'}<small>{selected.launch_angle_vertical_source || '—'}</small></dd></div>
            <div><dt>{t('analysis.direction')}</dt><dd>{finite(selected.launch_angle_horizontal) ? `${formatNumber(selected.launch_angle_horizontal, 1)}°` : '—'}<small>{selected.launch_angle_horizontal_source || '—'}</small></dd></div>
            <div><dt>{t('analysis.spin')}</dt><dd>{confirmedSpin(selected) == null ? t('analysis.noSpin') : `${formatNumber(confirmedSpin(selected)!, 0)} rpm`}</dd></div>
            {confirmedSpin(selected) == null && finite(selected.spin_rpm) ? <div><dt>{t('analysis.candidate')}</dt><dd>{formatNumber(selected.spin_rpm, 0)} rpm<small>{selected.spin_quality || '—'} · {finite(selected.spin_confidence) ? `${formatNumber(selected.spin_confidence * 100, 0)}%` : '—'}</small></dd></div> : null}
          </dl>
          {selected.camera_replay ? <div className="analysis-actions"><button onClick={() => openReplay(selected.camera_replay!)}>▷ {t('replay.open')}</button><button disabled={videoStatus.busy} onClick={() => void downloadVideo(selected)}>{videoStatus.busy ? t('analysis.videoBusy') : t('analysis.downloadVideo')}</button></div> : <p>{t('analysis.noVideo')}</p>}
          {videoStatus.error ? <p role="alert">{t('analysis.videoError')}</p> : null}
          <p className="analysis-note">{t('analysis.videoNote')}</p>
        </section> : null}
      </> : null}
    </main>
    {activeReplay ? <CameraReplayDialog key={activeReplay.replay.id} replay={activeReplay.replay} state={activeReplay.state} onClose={closeReplay} onRetry={() => openReplay(activeReplay.replay)} onPlaybackError={reportPlaybackError} /> : null}
  </div>;
}
