import { useEffect, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { socketService } from '../services/socketService';
import { useShotStore, type ShotProcessingPhase } from '../stores/useShotStore';
import { useSystemStore } from '../stores/useSystemStore';
import { useProfileStore } from '../stores/useProfileStore';
import { useUnitPreference } from '../state/useUnitPreference';
import { useDragScroll } from '../hooks/useDragScroll';
import { useDemoTouch } from '../hooks/useDemoTouch';
import { useVoiceCalloutLifecycle } from '../hooks/useVoiceCalloutLifecycle';
import { useI18n } from '../i18n/useI18n';
import { getClubName } from '../data/clubs';
import { filterShotsByProfile, type Shot } from '../types/shot';
import { formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit, type UnitSystem } from '../utils/units';
import { carryTooltip, markTypicalCarry } from '../utils/carryDisplay';
import { DemoBand } from '../components/DemoBand';
import { SpeechPrimeBanner } from '../components/SpeechPrimeBanner';
import { ShotProcessingArea } from '../components/ShotProcessingArea';
import { TabBar } from '../components/ui/TabBar';
import { CompanionMetrics, CompanionReplay } from './CompanionShotDetail';
import { DispersionTab } from './DispersionView';
import { adoptBrowserLocale, installCompanionHead } from './companionShell';
import './CompanionPage.css';

type CompanionTab = 'live' | 'dispersion';

export interface CompanionViewProps {
  connected: boolean;
  unitSystem: UnitSystem;
  shots: Shot[];
  activeProfileId: string;
  phase: ShotProcessingPhase | null;
  processingTimestamp: string | null;
  shotVersion: number;
  demoActive: boolean;
}

export default function CompanionPage() {
  const { unitSystem } = useUnitPreference();
  const connected = useSystemStore((state) => state.connected);
  const demoActive = useSystemStore((state) => state.demoActive);
  const shotState = useShotStore(
    useShallow((state) => ({
      shots: state.shots,
      phase: state.shotProcessingPhase,
      processingTimestamp: state.shotProcessingShotTimestamp,
      shotVersion: state.shotVersion,
    }))
  );
  const activeProfileId = useProfileStore((state) => state.activeProfileId);

  useEffect(() => {
    installCompanionHead(document);
    adoptBrowserLocale(window.localStorage, navigator.languages ?? [navigator.language]);
    // Read-only client: snapshots (get_*) plus the demo band's End and touch pings; no other socket commands here.
    socketService.connect();
  }, []);
  useDemoTouch(true);
  // Callouts switched on from any screen speak here too; Safari needs a tap first.
  useVoiceCalloutLifecycle();

  return (
    <CompanionView
      connected={connected}
      unitSystem={unitSystem}
      activeProfileId={activeProfileId}
      demoActive={demoActive}
      {...shotState}
    />
  );
}

export function CompanionView({
  connected,
  unitSystem,
  shots,
  activeProfileId,
  phase,
  processingTimestamp,
  shotVersion,
  demoActive,
}: CompanionViewProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState<CompanionTab>('live');
  const [selectedTimestamp, setSelectedTimestamp] = useState<string | null>(null);
  const [followedVersion, setFollowedVersion] = useState(shotVersion);
  const listRef = useRef<HTMLOListElement>(null);
  const dragScroll = useDragScroll(listRef);

  // A new live shot always takes over the screen, even while browsing older shots.
  if (shotVersion !== followedVersion) {
    setFollowedVersion(shotVersion);
    setSelectedTimestamp(null);
  }

  const sessionShots = activeProfileId ? filterShotsByProfile(shots, activeProfileId) : shots;
  const latest = sessionShots.at(-1) ?? null;
  const shot = sessionShots.find((candidate) => candidate.timestamp === selectedTimestamp) ?? latest;
  const isLive = shot === latest;
  const shotNumber = shot ? sessionShots.indexOf(shot) + 1 : 0;

  const header = (
    <>
      <DemoBand active={demoActive} connected={connected} />
      <SpeechPrimeBanner />
      <header className="companion__header">
        <div>
          <h1>{t('companion.title')}</h1>
          <span className={`companion__connection ${connected ? 'is-connected' : ''}`} role="status">
            {connected ? t('companion.connected') : t('companion.offline')}
          </span>
        </div>
        {shot ? (
          <div className="companion__shot-heading">
            <strong>
              {isLive ? t('range.shot', { n: shotNumber }) : t('companion.viewingShot', { n: shotNumber })}
            </strong>
            <span>{getClubName(shot.club)}</span>
          </div>
        ) : null}
      </header>
      <TabBar
        value={tab}
        options={[
          { id: 'live', label: t('nav.live') },
          { id: 'dispersion', label: t('companion.dispersion') },
        ]}
        onChange={setTab}
        className="companion__tabs"
        ariaLabel={t('companion.views')}
      />
    </>
  );
  const footnote = <p className="companion__footnote">{t('companion.readOnly')}</p>;

  if (tab === 'dispersion') {
    return (
      <main className="companion" aria-label={t('companion.title')}>
        {header}
        <DispersionTab liveShots={sessionShots} profileId={activeProfileId} unitSystem={unitSystem} />
        {footnote}
      </main>
    );
  }

  return (
    <main className="companion" aria-label={t('companion.title')}>
      {header}

      <section className="companion__live" aria-label={t('nav.live')}>
        <ShotProcessingArea phase={isLive ? phase : null}>
          {shot ? (
            <>
              {!isLive ? (
                <button
                  type="button"
                  className="companion-button companion__back"
                  onClick={() => setSelectedTimestamp(null)}
                >
                  {t('companion.backToLive')}
                </button>
              ) : null}
              <CompanionMetrics shot={shot} unitSystem={unitSystem} />
            </>
          ) : (
            <div className="companion__empty">
              <h2>{t('companion.empty')}</h2>
              <p>{t('companion.emptyDetail')}</p>
            </div>
          )}
        </ShotProcessingArea>
      </section>

      {shot ? (
        <section className="companion__replay" aria-label={t('replay.title')}>
          <CompanionReplay key={shot.timestamp} shot={shot} awaitingCamera={processingTimestamp === shot.timestamp} />
        </section>
      ) : null}

      <section className="companion__shots" aria-label={t('companion.shotList')}>
        <h2>{t('companion.shotList')}</h2>
        <ol
          ref={listRef}
          className="companion__shot-list"
          onPointerDown={dragScroll.onPointerDown}
          onPointerMove={dragScroll.onPointerMove}
          onPointerUp={dragScroll.onPointerUp}
          onPointerCancel={dragScroll.onPointerCancel}
          onClickCapture={dragScroll.onClickCapture}
        >
          {sessionShots
            .map((entry, index) => ({ entry, number: index + 1 }))
            .reverse()
            .map(({ entry, number }) => (
              <li key={entry.timestamp}>
                <button
                  type="button"
                  className={`companion__shot-row ${entry === shot ? 'is-selected' : ''}`}
                  aria-pressed={entry === shot}
                  aria-label={t('companion.selectShot', { n: number })}
                  onClick={() => setSelectedTimestamp(entry === latest ? null : entry.timestamp)}
                >
                  <span className="companion__shot-number">#{number}</span>
                  <span>{getClubName(entry.club)}</span>
                  <span title={carryTooltip(entry)}>
                    {markTypicalCarry(
                      entry,
                      formatDistance(entry.carry_spin_adjusted ?? entry.estimated_carry_yards, unitSystem, 0),
                      { compact: true }
                    )}{' '}
                    {getDistanceUnit(unitSystem)}
                  </span>
                  <span>
                    {formatSpeed(entry.ball_speed_mph, unitSystem, 1)} {getSpeedUnit(unitSystem)}
                  </span>
                  <span className="companion__shot-replay" aria-hidden="true">
                    {entry.camera_replay ? '▶' : ''}
                  </span>
                </button>
              </li>
            ))}
        </ol>
      </section>
      {footnote}
    </main>
  );
}
