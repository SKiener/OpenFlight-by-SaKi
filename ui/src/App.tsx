import { useState, useEffect, useEffectEvent, useRef, type CSSProperties } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useSocket } from './hooks/useSocket';
import { useSystemStore } from './stores/useSystemStore';
import { useShotStore } from './stores/useShotStore';
import { useCameraStore } from './stores/useCameraStore';
import { useDebugStore } from './stores/useDebugStore';
import { useProfileStore } from './stores/useProfileStore';
import { useHeroMetricStore } from './stores/useHeroMetricStore';
import { usePairingStore } from './stores/usePairingStore';
import { useCameraReplayController } from './hooks/useCameraReplayController';
import { useChromeInsets } from './hooks/useChromeInsets';
import { useVoiceCalloutLifecycle } from './hooks/useVoiceCalloutLifecycle';
import { useDemoTouch } from './hooks/useDemoTouch';
import { subscribeToAutomaticReplays } from './services/automaticReplay';
import type { CameraReplay, Shot } from './types/shot';
import { socketService } from './services/socketService';
import { DebugPanel } from './components/DebugPanel';
import { DemoBand } from './components/DemoBand';
import { SpeechPrimeBanner } from './components/SpeechPrimeBanner';
import { BallSpeedUncertainNote } from './components/BallSpeedUncertainNote';
import { DisplayMode } from './components/DisplayMode';
import { SimShotBadges } from './components/SimShotBadges';
import { ShotProcessingArea } from './components/ShotProcessingArea';
import { ShutdownDialog, type ShutdownState } from './components/ShutdownDialog';
import { CameraReplayDialog } from './components/CameraReplayDialog';
import { SessionHistoryDialog } from './components/SessionHistoryDialog';
import { BagSettingsDialog } from './components/BagSettingsDialog';
import { SetupReminder } from './components/SetupReminder';
import {
  cameraAvailable,
  closeSetupReminder,
  showSetupReminder,
  type SetupReminderAction,
} from './components/setupReminderState';
import { PairDeviceDialog } from './pairing/PairDeviceDialog';
import { RevokeDevicesDialog } from './pairing/RevokeDevicesDialog';
import {
  CameraPanel,
  ProfileNameDialog,
  ProfilesPanel,
  ClearSessionDialog,
  SimulateBubble,
  MenuSheet,
  PanelHeader,
  PanelAction,
  PickerOverlay,
  ShotsPanel,
  StatsPanel,
  clubSections,
  trainingImplementSections,
  type PanelView,
} from './components/panel';
import { filterShotsByProfile } from './types/shot';
import type { Profile } from './types/profile';
import { getBagClubIds, getClubName } from './data/clubs';
import { getTrainingImplementLabel } from './data/trainingImplements';
import { useLaunchDaddy, LaunchDaddyOverlay } from './components/LaunchDaddy';
import { RangeLivePanel } from './components/range/RangeLivePanel';
import { RangeHeader, RangeFooter } from './components/range/RangeChrome';
import { VirtualRangeView } from './virtual-range/VirtualRangeView';
import { startArea, writeLastArea } from './state/lastArea';
import { isRangeDemo } from './virtual-range/rangeDemoSwitch';
import { LIVE_SOURCE, type RangeSource } from './virtual-range/rangeViewModel';
import type { SavedRangeSession } from './virtual-range/savedShots';
import { isKioskHost } from './utils/kioskHost';
import { safeStorage } from './utils/storage';

import { useI18n } from './i18n/useI18n';
import './components/panel/panel.css';
import './components/range/range.css';
// After range.css: portrait-only rules override the Range layout's base rules.
import './components/panel/phone.css';
import './components/panel/phoneLandscape.css';

const pageSearch = (): string => (typeof window === 'undefined' ? '' : window.location.search);
const pageHostname = (): string => (typeof window === 'undefined' ? '' : window.location.hostname);

function AppContent() {
  const { t } = useI18n();
  const { shutdown } = useSocket();
  const { connected, mockMode, debugMode, demoActive, latestSimShots, serverClub } = useSystemStore(
    useShallow((state) => ({
      connected: state.connected,
      mockMode: state.mockMode,
      debugMode: state.debugMode,
      demoActive: state.demoActive,
      latestSimShots: state.latestSimShots,
      serverClub: state.serverClub,
    }))
  );
  const { latestShot, shots, isNewShot, shotProcessingPhase, shotVersion, sessionGeneration, sessionDemo } =
    useShotStore(
      useShallow((state) => ({
        latestShot: state.latestShot,
        shots: state.shots,
        isNewShot: state.isNewShot,
        shotProcessingPhase: state.shotProcessingPhase,
        shotVersion: state.shotVersion,
        sessionGeneration: state.sessionGeneration,
        sessionDemo: state.sessionDemo,
      }))
    );
  const { captureSettings, captureSettingsError } = useCameraStore(
    useShallow((state) => ({
      captureSettings: state.captureSettings,
      captureSettingsError: state.captureSettingsError,
    }))
  );
  const { profiles, activeProfileId, profilesLoaded } = useProfileStore(
    useShallow((state) => ({
      profiles: state.profiles,
      activeProfileId: state.activeProfileId,
      profilesLoaded: state.loaded,
    }))
  );
  const activeProfile = profiles.find((profile) => profile.id === activeProfileId) ?? null;
  const activeProfileName = activeProfile?.name ?? '';
  const { heroMetricId, setHeroMetricId } = useHeroMetricStore(
    useShallow((state) => ({ heroMetricId: state.heroMetricId, setHeroMetricId: state.setHeroMetricId }))
  );
  const {
    debugReadings,
    debugShotLogs,
    radarConfig,
    radarConfigError,
    triggerDiagnostics,
    triggerStatus,
    iwr6843Alert,
    dismissIWR6843Alert,
  } = useDebugStore(
    useShallow((state) => ({
      debugReadings: state.debugReadings,
      debugShotLogs: state.debugShotLogs,
      radarConfig: state.radarConfig,
      radarConfigError: state.radarConfigError,
      triggerDiagnostics: state.triggerDiagnostics,
      triggerStatus: state.triggerStatus,
      iwr6843Alert: state.iwr6843Alert,
      dismissIWR6843Alert: state.dismissIWR6843Alert,
    }))
  );

  const [currentView, setCurrentView] = useState<PanelView>(() => startArea(safeStorage, pageSearch(), pageHostname()));
  const rangeDemo = isRangeDemo(pageSearch());
  // The page's host is known at the first render, which already picks the start area and the footer. The
  // server's answer (isLocalKiosk below, from pairing) arrives later, so it only gates the menu's pairing items.
  // The same host check gives the kiosk its on-screen keyboard; a phone types with its own.
  const showRange = isKioskHost(pageHostname());
  const [rangeSource, setRangeSource] = useState<RangeSource>(LIVE_SOURCE);
  // Leaving the range ends a saved session there: the next visit opens today's shots.
  if (currentView !== 'virtual-range' && rangeSource.kind !== 'live') setRangeSource(LIVE_SOURCE);
  const [selectedClub, setSelectedClub] = useState('driver');
  const [selectedTrainingImplement, setSelectedTrainingImplement] = useState('driver');
  const [menuOpen, setMenuOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [bagOpen, setBagOpen] = useState(false);
  const [showShutdown, setShowShutdown] = useState(false);
  const [shutdownState, setShutdownState] = useState<ShutdownState>('confirm');
  // Open on every app load so the user confirms their club before the first
  // shot; dismissing keeps the default. The /display route returns early below,
  // so this never appears in the passive TV view. The range demo starts without it, so nothing covers the picture.
  const [pickerOpen, setPickerOpen] = useState(!rangeDemo);
  // The kiosk's setup window lies above the picker at start when a capture camera runs: aim the camera at
  // the ball, then mark ball and target. Once per page load; closing it is not remembered.
  const [setupDismissed, setSetupDismissed] = useState(false);
  const [profileDialog, setProfileDialog] = useState<{ mode: 'add' | 'rename'; target: Profile | null } | null>(null);
  const [profileDialogName, setProfileDialogName] = useState('');
  const [clearSessionOpen, setClearSessionOpen] = useState(false);
  const [pairDialogOpen, setPairDialogOpen] = useState(false);
  const [revokeDialogOpen, setRevokeDialogOpen] = useState(false);
  const isLocalKiosk = usePairingStore((state) => state.access === 'local');
  const { activeReplay, openReplay, closeReplay, reportPlaybackError } = useCameraReplayController();
  // The range takes the whole screen; header, band and footer lie over it, so it is told how much they cover.
  const fullRange = currentView === 'virtual-range';
  const topChromeRef = useRef<HTMLDivElement>(null);
  const footerRef = useRef<HTMLElement>(null);
  const rangeInsets = useChromeInsets(topChromeRef, footerRef, fullRange);

  // Reflect a server-pushed club change (e.g. the club changed in the connected
  // simulator) locally without echoing back. Done during render (React's "adjust
  // state when an input changes" pattern) rather than in an effect.
  const [appliedServerClub, setAppliedServerClub] = useState<string | null>(null);
  if (serverClub && serverClub !== appliedServerClub) {
    setAppliedServerClub(serverClub);
    setSelectedClub(serverClub);
  }

  const { isLaunchDaddyMode, isExploding, triggerExplosion } = useLaunchDaddy();
  const isDisplayRoute = typeof window !== 'undefined' && window.location.pathname.replace(/\/$/, '') === '/display';
  const isSwingSpeedMode = triggerStatus.mode === 'swing-speed';
  const setupOpen = showSetupReminder({
    kiosk: showRange,
    displayRoute: isDisplayRoute,
    rangeDemo,
    cameraAvailable: cameraAvailable(captureSettings),
    dismissed: setupDismissed,
  });
  const handleCloseSetup = (action: SetupReminderAction) => {
    const outcome = closeSetupReminder(action, pickerOpen);
    setSetupDismissed(true);
    setPickerOpen(outcome.pickerOpen);
    if (outcome.view) setCurrentView(outcome.view);
  };
  const onAutomaticReplay = useEffectEvent((replay: CameraReplay, shot: Shot) => {
    if (
      isDisplayRoute ||
      setupOpen ||
      currentView !== 'live' ||
      menuOpen ||
      historyOpen ||
      bagOpen ||
      showShutdown ||
      pickerOpen ||
      profileDialog ||
      clearSessionOpen ||
      pairDialogOpen ||
      revokeDialogOpen ||
      shot.profile_id !== activeProfileId ||
      (activeReplay && !activeReplay.automatic)
    )
      return;
    openReplay(replay, true);
  });
  useEffect(() => subscribeToAutomaticReplays((replay, shot) => onAutomaticReplay(replay, shot)), []);
  const activeImplementLabel = isSwingSpeedMode
    ? getTrainingImplementLabel(selectedTrainingImplement)
    : getClubName(selectedClub);

  useEffect(() => {
    return socketService.onSessionCleared(() => {
      setClearSessionOpen(false);
      // The range just shows its empty hint; any other view goes back to Live.
      setCurrentView((view) => (view === 'virtual-range' ? view : 'live'));
    });
  }, []);

  useEffect(() => {
    if (!rangeDemo) writeLastArea(safeStorage, currentView);
  }, [currentView, rangeDemo]);

  // Trigger explosion when a new shot is detected in Launch Daddy mode
  useEffect(() => {
    if (isNewShot && isLaunchDaddyMode) {
      triggerExplosion();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- shotVersion triggers the effect; isNewShot is only a guard
  }, [shotVersion, isLaunchDaddyMode, triggerExplosion]);

  // Unlocks the shot beep and speech on the first tap (iOS needs a gesture).
  useVoiceCalloutLifecycle();
  useDemoTouch(!isDisplayRoute);

  const handleSelectProfile = (profileId: string) => {
    socketService.setActiveProfile(profileId);
    setCurrentView('live');
  };

  const handleRemoveProfile = (profileId: string) => {
    // The server refuses to remove the active profile; don't offer it either.
    if (profileId === activeProfileId) return;
    socketService.removeProfile(profileId);
  };

  const openAddProfile = () => {
    setProfileDialog({ mode: 'add', target: null });
    setProfileDialogName('');
  };

  const openRenameProfile = (profile: Profile) => {
    setProfileDialog({ mode: 'rename', target: profile });
    setProfileDialogName(profile.name);
  };

  const closeProfileDialog = () => {
    setProfileDialog(null);
    setProfileDialogName('');
  };

  const handleConfirmProfileDialog = () => {
    const name = profileDialogName.trim();
    if (!name || !profileDialog) return;
    if (profileDialog.mode === 'add') {
      socketService.addProfile(name);
    } else if (profileDialog.target) {
      socketService.renameProfile(profileDialog.target.id, name);
    }
    closeProfileDialog();
  };

  const handlePickerSelect = (id: string) => {
    if (isSwingSpeedMode) {
      setSelectedTrainingImplement(id);
      socketService.setTrainingImplement(id);
    } else {
      setSelectedClub(id);
      socketService.setClub(id);
    }
    setPickerOpen(false);
  };

  const handleShutdown = async () => {
    setShutdownState('pending');
    try {
      await shutdown();
    } catch {
      setShutdownState('error');
    }
  };

  const closeShutdown = () => {
    setShowShutdown(false);
    setShutdownState('confirm');
  };

  const showOnRange = (session: SavedRangeSession) => {
    setHistoryOpen(false);
    setRangeSource({ kind: 'saved', ...session });
    setCurrentView('virtual-range');
  };

  const profileShots = filterShotsByProfile(shots, activeProfileId);
  const profileLatestShot = profileShots[profileShots.length - 1] ?? null;
  const profileIsNewShot = Boolean(
    isNewShot && latestShot && profileLatestShot && latestShot.timestamp === profileLatestShot.timestamp
  );

  if (isDisplayRoute) {
    return (
      <DisplayMode
        connected={connected}
        captureSettings={captureSettings}
        latestShot={latestShot}
        shots={shots}
        demoActive={demoActive}
      />
    );
  }

  const changeClubAction = (
    <PanelAction
      aria-label={isSwingSpeedMode ? t('app.changeImplement') : t('app.changeClub')}
      onClick={() => setPickerOpen(true)}
    >
      {activeImplementLabel} <span aria-hidden="true">⌄</span>
    </PanelAction>
  );
  const latestReplay = profileLatestShot?.camera_replay;

  const liveHeaderActions = changeClubAction;

  const addProfileAction = <PanelAction onClick={openAddProfile}>{t('menu.addProfile')}</PanelAction>;

  const clearSessionAction = (
    <PanelAction variant="danger" onClick={() => setClearSessionOpen(true)}>
      {t('app.clearSession')}
    </PanelAction>
  );

  const debugRecordAction = (
    <PanelAction variant="secondary" onClick={() => socketService.toggleDebug()}>
      {debugMode ? t('app.stopRecording') : t('app.record')}
    </PanelAction>
  );

  const insetProperties = fullRange
    ? ({
        '--virtual-range-inset-top': `${rangeInsets.top}px`,
        '--virtual-range-inset-bottom': `${rangeInsets.bottom}px`,
      } as CSSProperties)
    : undefined;

  return (
    <div
      className={`panel-app range-app${showRange ? ' range-app--kiosk' : ''}${fullRange ? ' range-app--full' : ''} ${isLaunchDaddyMode ? 'app--launch-daddy' : ''} ${isExploding ? 'app--exploding' : ''}`}
      style={insetProperties}
    >
      <LaunchDaddyOverlay />
      <RangeHeader ref={topChromeRef} kiosk={showRange}>
        <DemoBand active={demoActive} connected={connected} />
        <SpeechPrimeBanner />

        {iwr6843Alert && (
          <div className="iwr-alert" role="alert">
            <div>
              <strong>{t('live.tiRadarFailed')}</strong>
              <span>{t('live.tiRadarDetail', { reason: iwr6843Alert.reason })}</span>
            </div>
            <button type="button" onClick={dismissIWR6843Alert} aria-label={t('live.dismissAlert')}>
              {t('live.dismiss')}
            </button>
          </div>
        )}
        <BallSpeedUncertainNote shot={profileLatestShot} />
      </RangeHeader>

      {showShutdown ? (
        <ShutdownDialog state={shutdownState} onConfirm={handleShutdown} onCancel={closeShutdown} />
      ) : null}

      {activeReplay ? (
        <CameraReplayDialog
          key={activeReplay.replay.id}
          replay={activeReplay.replay}
          state={activeReplay.state}
          automatic={activeReplay.automatic}
          onClose={closeReplay}
          onRetry={() => openReplay(activeReplay.replay, activeReplay.automatic)}
          onPlaybackError={reportPlaybackError}
        />
      ) : null}

      <main className="panel-app__main">
        {currentView === 'live' && (
          <>
            <ShotProcessingArea phase={shotProcessingPhase}>
              <RangeLivePanel
                shot={profileLatestShot}
                shots={shots}
                profileId={activeProfileId}
                profileName={activeProfileName}
                clubLabel={activeImplementLabel}
                activeTrainingImplement={isSwingSpeedMode ? selectedTrainingImplement : undefined}
                selectedMetricId={heroMetricId}
                onSelectMetric={setHeroMetricId}
                isNewShot={profileIsNewShot}
                headerAction={liveHeaderActions}
                kiosk={showRange}
                onChangeView={setCurrentView}
              />
            </ShotProcessingArea>
            {debugMode && <SimShotBadges latestSimShots={latestSimShots} />}
          </>
        )}
        {currentView === 'virtual-range' && (
          <VirtualRangeView
            shots={shots}
            club={selectedClub}
            profileId={activeProfileId}
            sessionGeneration={sessionGeneration}
            source={rangeSource}
            processingPhase={shotProcessingPhase}
            demoActive={demoActive}
            sessionDemo={sessionDemo}
            insets={rangeInsets}
            onBackToLive={() => setRangeSource(LIVE_SOURCE)}
          />
        )}
        {currentView === 'profiles' && (
          <ProfilesPanel
            profiles={profiles}
            activeProfileId={activeProfileId}
            shots={shots}
            loaded={profilesLoaded}
            demoActive={demoActive}
            sessionDemo={sessionDemo}
            onSelectProfile={handleSelectProfile}
            onRenameProfile={openRenameProfile}
            onRemoveProfile={handleRemoveProfile}
            headerAction={addProfileAction}
          />
        )}
        {currentView === 'stats' && (
          <StatsPanel
            shots={shots}
            activeClub={selectedClub}
            profileId={activeProfileId}
            profileName={activeProfileName}
            headerAction={clearSessionAction}
          />
        )}
        {currentView === 'shots' && (
          <ShotsPanel
            shots={shots}
            profileId={activeProfileId}
            profileName={activeProfileName}
            clubLabel={activeImplementLabel}
            onDeleteShot={(timestamp) => socketService.deleteShot(timestamp)}
            onReplayShot={(shot) => {
              if (shot.camera_replay) openReplay(shot.camera_replay);
            }}
          />
        )}
        {currentView === 'camera' && (
          <CameraPanel
            captureSettings={captureSettings}
            captureSettingsError={captureSettingsError}
            onUpdateCaptureSettings={(settings) => socketService.setCameraCaptureSettings(settings)}
          />
        )}
        {currentView === 'debug' && (
          <div className="panel">
            <PanelHeader
              title={t('nav.debug')}
              subtitle={debugMode ? t('app.debugRecording') : t('app.debugIdle')}
              actions={debugRecordAction}
            />
            <div className="panel__body panel-app__debug">
              <DebugPanel
                enabled={debugMode}
                readings={debugReadings}
                shotLogs={debugShotLogs}
                radarConfig={radarConfig}
                radarConfigError={radarConfigError}
                mockMode={mockMode}
                onToggle={() => socketService.toggleDebug()}
                onUpdateConfig={(config) => socketService.setRadarConfig(config)}
                triggerDiagnostics={triggerDiagnostics}
                triggerStatus={triggerStatus}
                connected={connected}
                demoActive={demoActive}
                onReinitializeRadar={() => socketService.reinitializeRadar()}
              />
            </div>
          </div>
        )}
        {mockMode && (currentView === 'live' || currentView === 'virtual-range') ? (
          <SimulateBubble
            label={isSwingSpeedMode ? t('app.simulateSwing') : t('app.simulateShot')}
            onSimulate={() => socketService.simulateShot()}
            className={currentView === 'virtual-range' ? 'simulate-bubble--range' : undefined}
          />
        ) : null}
      </main>

      {/*
       * Overlays sit outside <main> so they cover the footer too, matching how
       * 6a draws them over the whole card.
       */}
      {menuOpen ? (
        <MenuSheet
          onOpenBag={() => {
            setMenuOpen(false);
            setBagOpen(true);
          }}
          onChangeView={(view) => {
            setMenuOpen(false);
            setCurrentView(view);
          }}
          onOpenHistory={() => {
            setMenuOpen(false);
            setHistoryOpen(true);
          }}
          onClose={() => setMenuOpen(false)}
          onShutdown={() => {
            setMenuOpen(false);
            setShutdownState('confirm');
            setShowShutdown(true);
          }}
          onPairDevice={
            isLocalKiosk
              ? () => {
                  setMenuOpen(false);
                  setPairDialogOpen(true);
                }
              : undefined
          }
          onRevokeDevices={
            isLocalKiosk
              ? () => {
                  setMenuOpen(false);
                  setRevokeDialogOpen(true);
                }
              : undefined
          }
        />
      ) : null}

      {pairDialogOpen ? <PairDeviceDialog onClose={() => setPairDialogOpen(false)} /> : null}
      {revokeDialogOpen ? <RevokeDevicesDialog onClose={() => setRevokeDialogOpen(false)} /> : null}

      {historyOpen ? (
        <SessionHistoryDialog
          onClose={() => setHistoryOpen(false)}
          onShowOnRange={showRange ? showOnRange : undefined}
        />
      ) : null}
      {bagOpen && activeProfile ? (
        <BagSettingsDialog key={activeProfile.id} profile={activeProfile} onClose={() => setBagOpen(false)} />
      ) : null}

      {profileDialog ? (
        <ProfileNameDialog
          mode={profileDialog.mode}
          name={profileDialogName}
          onChange={setProfileDialogName}
          onConfirm={handleConfirmProfileDialog}
          onCancel={closeProfileDialog}
          onScreenKeyboard={showRange}
        />
      ) : null}

      {clearSessionOpen ? (
        <ClearSessionDialog
          profileName={activeProfileName}
          onConfirm={() => socketService.clearSession(activeProfileId)}
          onCancel={() => setClearSessionOpen(false)}
        />
      ) : null}

      {pickerOpen ? (
        <PickerOverlay
          title={isSwingSpeedMode ? t('app.selectImplement') : t('app.selectClub')}
          selectedId={isSwingSpeedMode ? selectedTrainingImplement : selectedClub}
          sections={
            isSwingSpeedMode ? trainingImplementSections() : clubSections(getBagClubIds(activeProfile?.settings))
          }
          onSelect={handlePickerSelect}
          onClose={() => setPickerOpen(false)}
          wide={isSwingSpeedMode}
        />
      ) : null}

      {setupOpen ? (
        <SetupReminder
          captureSettings={captureSettings}
          onAlign={() => handleCloseSetup('align')}
          onDone={() => handleCloseSetup('done')}
        />
      ) : null}

      <RangeFooter
        ref={footerRef}
        currentView={currentView}
        onChangeView={(view) => {
          setMenuOpen(false);
          setHistoryOpen(false);
          setCurrentView(view);
        }}
        onOpenMenu={() => setMenuOpen((open) => !open)}
        menuOpen={menuOpen}
        onOpenHistory={() => {
          setMenuOpen(false);
          setHistoryOpen(true);
        }}
        historyOpen={historyOpen}
        hasReplay={Boolean(latestReplay)}
        onReplay={() => {
          if (latestReplay) openReplay(latestReplay);
        }}
        showRange={showRange}
      />
    </div>
  );
}

function App() {
  return <AppContent />;
}

export default App;
