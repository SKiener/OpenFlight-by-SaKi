import { io, type Socket } from 'socket.io-client';
import { useSystemStore } from '../stores/useSystemStore';
import { useShotStore } from '../stores/useShotStore';
import { useCameraStore, type CameraCaptureSettings, type CameraSettingsUpdate } from '../stores/useCameraStore';
import { useDebugStore } from '../stores/useDebugStore';
import {
  type Shot,
  type SessionState,
  type TriggerDiagnostic,
  type TriggerDiagnosticUpdate,
  type TriggerStatus,
} from '../types/shot';
import type { DebugReading, DemoState, RadarConfig, DebugShotLog, SimShotInfo, SimStatus } from '../types/socket';
import type { PowerStatus } from '../types/power';
import { getServerOrigin } from '../utils/serverOrigin';
import { handleShotMessage, handleShotUpdate, type ShotMessage, type ShotUpdateMessage } from './handleShotMessage';
import { shotAnnouncer, speaksOnThisDevice, speakTestCallout } from './shotAnnouncer';
import { setVoiceCalloutSender, useVoiceCalloutStore } from '../stores/useVoiceCalloutStore';
import { cancelSpeech } from '../utils/speech';
import { ingestSessionClub } from './sessionClubSync';
import { handleCameraReplayReady, type CameraReplayReadyMessage } from './cameraReplayReady';
import { remainingShotsAfterClear } from './sessionClear';
import { useProfileStore } from '../stores/useProfileStore';
import type { ProfilesSnapshot } from '../types/profile';
import { handleServerDisconnect, handleSocketConnectError } from '../stores/usePairingStore';
import type { PairingCodeClosedReason } from '../pairing/pairingApi';
import { OPS_RECOVERY_CODES, type OpsRecoveryResult } from '../types/socket';
import { clientClockPayload } from '../utils/clockOffset';
import { useWeatherStore } from '../stores/useWeatherStore';
import { useTiltCalibrationStore } from '../stores/useTiltCalibrationStore';
import type { TiltCalibrationEvent } from '../types/tiltCalibration';
import type { RangeLocation, WeatherAck, WeatherReport, WeatherState } from '../types/weather';
import { fetchCurrentWeather } from './openMeteo';
import { newGpsLocationAttempts, syncLocationFromGps, syncWeather, WEATHER_CHECK_INTERVAL_MS } from './weatherSync';
import type { WeatherFailureReport } from './weatherFailure';
import { isKioskPage } from '../utils/kioskHost';
import { getOpenflightNative, type OpenFlightNative } from '../native/openflightNative';
import { fetchRangeWeather, rangeLocationFromGps } from '../native/nativeWeather';
import { NativeForwarder } from '../native/nativeForwarder';

const SOCKET_URL = getServerOrigin();
const DELETE_SHOT_TIMEOUT_MS = 5000;
const WEATHER_ACK_TIMEOUT_MS = 6000;

class SocketService {
  private socket: Socket | null = null;
  private sessionClearedListeners = new Set<() => void>();
  private pairingCodeClosedListeners = new Set<(reason: PairingCodeClosedReason) => void>();
  private sessionShotsListeners = new Set<(shots: Shot[]) => void>();
  private deleteShotErrorListeners = new Set<() => void>();
  private weatherTimer: ReturnType<typeof setInterval> | null = null;
  /** The iOS companion's bridge (null in Safari and on the kiosk) and what it forwards to the Pi. */
  private native: OpenFlightNative | null = null;
  private nativeForwarder: NativeForwarder | null = null;
  private gpsLocationAttempts = newGpsLocationAttempts();

  /** `open` is the Socket.IO client (replaceable in tests). */
  connect(open: (url: string, options: { transports: string[] }) => Socket = io) {
    if (this.socket) return;

    this.socket = open(SOCKET_URL, {
      transports: ['websocket', 'polling'],
    });

    this.setupListeners();
    setVoiceCalloutSender((patch) => this.socket?.emit('set_voice_callouts', patch));
    // A paired phone keeps the range weather fresh; the kiosk has no internet and never fetches.
    if (!isKioskPage()) {
      this.weatherTimer = setInterval(() => this.syncWeather(), WEATHER_CHECK_INTERVAL_MS);
      this.native = getOpenflightNative();
      if (this.native) {
        this.nativeForwarder = new NativeForwarder(this.native, {
          isConnected: () => this.socket?.connected ?? false,
          emit: (event, payload) => this.socket?.emit(event, payload),
          emitWithAck: (event, payload, timeoutMs) =>
            this.socket
              ? this.socket.timeout(timeoutMs).emitWithAck(event, payload)
              : Promise.reject(new Error('closed')),
        });
        this.nativeForwarder.start();
      }
    }
  }

  disconnect() {
    if (this.weatherTimer !== null) {
      clearInterval(this.weatherTimer);
      this.weatherTimer = null;
    }
    this.nativeForwarder?.stop();
    this.nativeForwarder = null;
    this.native = null;
    this.gpsLocationAttempts = newGpsLocationAttempts();
    setVoiceCalloutSender(null);
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }

  private syncWeather() {
    const native = this.native;
    const connected = this.socket?.connected ?? false;
    const reportFailure = (failure: WeatherFailureReport) => this.reportWeatherFailure(failure);
    void syncWeather({
      isKiosk: isKioskPage(),
      connected,
      now: () => performance.now(),
      fetchWeather: (location) =>
        fetchRangeWeather(location, { native, browserFetch: (where) => fetchCurrentWeather(where) }),
      send: (report) => this.sendWeatherReport(report),
      reportFailure,
    });
    if (native) {
      void syncLocationFromGps({
        isKiosk: isKioskPage(),
        connected,
        now: () => performance.now(),
        attempts: this.gpsLocationAttempts,
        locate: () => rangeLocationFromGps(native),
        save: (location) => this.setWeatherLocation(location),
        reportFailure,
      });
    }
  }

  /**
   * Why a weather fetch or GPS location failed, for the session log: only the closed-set fields,
   * never error text, URLs or coordinates. Not queued while disconnected.
   */
  private reportWeatherFailure({ stage, reason, consecutive_failures, next_retry_s }: WeatherFailureReport) {
    if (!this.socket?.connected) return;
    this.socket.emit('weather_fetch_failed', { stage, reason, consecutive_failures, next_retry_s });
  }

  private setupListeners() {
    if (!this.socket) return;

    this.socket.on('connect', () => {
      console.log('Connected to server');
      useSystemStore.getState().setConnected(true);
      this.socket?.emit('client_clock', clientClockPayload());
      this.socket?.emit('get_session');
      this.socket?.emit('get_trigger_status');
      this.socket?.emit('get_radar_config');
      this.socket?.emit('get_camera_capture_settings');
      this.socket?.emit('get_profiles');
      this.socket?.emit('get_iwr6843_tilt_calibration');
      this.socket?.emit('get_voice_callouts');
      this.nativeForwarder?.onConnect();
    });

    this.socket.on('disconnect', (reason) => {
      console.log('Disconnected from server');
      useSystemStore.getState().setConnected(false);
      useShotStore.getState().finishShotProcessing();
      if (useCameraStore.getState().captureSettings.updating)
        useCameraStore.getState().setCaptureSettingsError('Server disconnected');
      if (reason === 'io server disconnect') void handleServerDisconnect(() => this.socket?.connect());
    });

    this.socket.on('connect_error', (error: Error) => {
      handleSocketConnectError(error, this.socket?.active ?? false);
    });

    this.socket.on('pairing_code_closed', (data: { reason: PairingCodeClosedReason }) => {
      this.pairingCodeClosedListeners.forEach((listener) => listener(data.reason));
    });

    this.socket.on('shot_processing', (data: { state: 'capturing' | 'calculating' | 'failed' }) => {
      const shotStore = useShotStore.getState();
      if (data.state === 'failed') {
        shotStore.finishShotProcessing();
      } else {
        shotStore.startShotProcessing(data.state);
      }
    });

    this.socket.on('shot', (data: ShotMessage) => {
      handleShotMessage(data);
    });

    this.socket.on('shot_update', (data: ShotUpdateMessage) => {
      handleShotUpdate(data);
    });

    this.socket.on('camera_replay_ready', (data: CameraReplayReadyMessage) => {
      handleCameraReplayReady(data);
    });

    // Swing-speed mode also emits a normal `shot` event, handled above, so the
    // rep is already recorded. This listener is registered without a payload to
    // document that `swing_speed` is deliberately ignored here rather than
    // forgotten -- handling it too would double-count the rep.
    this.socket.on('swing_speed', () => {});

    this.socket.on('sim_status', (data: SimStatus) => {
      useSystemStore.getState().setSimStatus(data);
    });

    this.socket.on('power_status', (data: PowerStatus) => {
      useSystemStore.getState().setPowerStatus(data);
    });

    this.socket.on('sim_shot', (data: SimShotInfo) => {
      useSystemStore.getState().setLatestSimShot(data);
    });

    this.socket.on('sim_send_failed', (data: { target: string; reason: string }) => {
      console.warn(`Sim send failed (${data.target}): ${data.reason}`);
    });

    this.socket.on('sim_shot_dropped', (data: { reason: string }) => {
      console.warn(`Sim shot dropped: ${data.reason}`);
    });

    this.socket.on('club_changed', (data: { club: string }) => {
      ingestSessionClub(data.club);
    });

    this.socket.on('delete_shot_error', () => {
      this.deleteShotErrorListeners.forEach((listener) => listener());
    });

    this.socket.on('weather_state', (data: WeatherState) => {
      useWeatherStore.getState().applyState(data);
      this.syncWeather();
    });

    // One setting for every screen; each decides for itself whether it speaks.
    this.socket.on('voice_callouts', (data: unknown) => {
      const store = useVoiceCalloutStore.getState();
      const wasEnabled = store.enabled;
      store.applyServerSettings(data);
      // Switched off on another screen: stop what this one is still saying.
      if (wasEnabled && !useVoiceCalloutStore.getState().enabled) cancelSpeech();
    });

    // The kiosk's test button: the phones speak (the kiosk itself never does).
    this.socket.on('voice_test', () => {
      speakTestCallout();
    });

    this.socket.on('iwr6843_tilt_calibration', (data: TiltCalibrationEvent) => {
      useTiltCalibrationStore.getState().applyStatus(data);
    });

    this.socket.on('profiles', (data: ProfilesSnapshot) => {
      useProfileStore.getState().applySnapshot(data);
    });

    this.socket.on('demo_state', (data: DemoState) => {
      useSystemStore.getState().setDemoState(data);
    });

    this.socket.on('session_state', (data: SessionState & { mock_mode?: boolean; debug_mode?: boolean }) => {
      console.log('Session state received:', data);
      useShotStore.getState().receiveSession(data.shots, data.demo_active === true);
      shotAnnouncer.sync(data.shots);
      this.sessionShotsListeners.forEach((listener) => listener(data.shots));

      const systemStore = useSystemStore.getState();
      if (data.mock_mode !== undefined) {
        systemStore.setMockMode(data.mock_mode);
      }
      if (data.debug_mode !== undefined) {
        systemStore.setDebugMode(data.debug_mode);
      }
      if (data.demo_active !== undefined) {
        systemStore.setDemoActive(data.demo_active);
      }
      if (data.calibration) {
        systemStore.setCalibration(data.calibration);
      }
      ingestSessionClub(data.club);
    });

    this.socket.on('debug_toggled', (data: { enabled: boolean }) => {
      useSystemStore.getState().setDebugMode(data.enabled);
      if (!data.enabled) {
        useDebugStore.getState().clearDebugData();
      }
    });

    this.socket.on('debug_shot', (data: DebugShotLog) => {
      useDebugStore.getState().addDebugShotLog(data);
    });

    this.socket.on('debug_reading', (data: DebugReading) => {
      useDebugStore.getState().addDebugReading(data);
    });

    this.socket.on('radar_config', (data: RadarConfig) => {
      useDebugStore.getState().setRadarConfig(data);
    });
    this.socket.on('radar_config_error', (data?: { code?: string }) => {
      useDebugStore.getState().setRadarConfigError(data?.code === 'busy' ? 'busy' : 'failed');
    });

    this.socket.on('camera_capture_settings', (data: CameraCaptureSettings) => {
      useCameraStore.getState().setCaptureSettings(data);
    });

    this.socket.on('camera_capture_settings_error', (data: { error: string }) => {
      useCameraStore.getState().setCaptureSettingsError(data.error);
    });

    this.socket.on('session_cleared', (data?: { profile_id?: string; shots?: Shot[] }) => {
      const remaining = remainingShotsAfterClear(useShotStore.getState().shots, data);
      shotAnnouncer.sync(remaining);
      useShotStore.getState().clearSession(remaining);
      this.sessionClearedListeners.forEach((listener) => listener());
    });

    this.socket.on('trigger_diagnostic', (data: TriggerDiagnostic) => {
      const debugStore = useDebugStore.getState();
      debugStore.addTriggerDiagnostic(data);
      debugStore.updateTriggerStatusStats(data.accepted);
    });

    this.socket.on('trigger_diagnostic_update', (data: TriggerDiagnosticUpdate) => {
      useDebugStore.getState().updateTriggerDiagnostic(data);
    });

    this.socket.on('trigger_status', (data: TriggerStatus) => {
      useDebugStore.getState().setTriggerStatus(data);
    });

    this.socket.on(
      'cloud_upload_status',
      (data: { state: 'idle' | 'running' | 'complete' | 'error'; message: string }) => {
        useSystemStore.getState().setCloudUploadStatus(data.state, data.message);
      }
    );
  }

  // Emitters
  onSessionCleared(listener: () => void) {
    this.sessionClearedListeners.add(listener);
    return () => {
      this.sessionClearedListeners.delete(listener);
    };
  }

  onPairingCodeClosed(listener: (reason: PairingCodeClosedReason) => void) {
    this.pairingCodeClosedListeners.add(listener);
    return () => {
      this.pairingCodeClosedListeners.delete(listener);
    };
  }

  clearSession(profileId: string) {
    this.socket?.emit('clear_session', { profile_id: profileId });
  }

  setActiveProfile(profileId: string) {
    this.socket?.emit('set_active_profile', { profile_id: profileId });
  }

  addProfile(name: string) {
    this.socket?.emit('add_profile', { name });
  }

  renameProfile(profileId: string, name: string) {
    this.socket?.emit('rename_profile', { profile_id: profileId, name });
  }

  removeProfile(profileId: string) {
    this.socket?.emit('remove_profile', { profile_id: profileId });
  }

  uploadCloud() {
    useSystemStore.getState().setCloudUploadStatus('running', 'Uploading...');
    this.socket?.emit('upload_cloud');
  }

  setClub(club: string) {
    this.socket?.emit('set_club', { club });
  }

  async setProfileClubs(profileId: string, clubIds: string[]): Promise<void> {
    if (!this.socket?.connected) throw new Error('Disconnected');
    const result = (await this.socket.timeout(6000).emitWithAck('set_profile_clubs', {
      profile_id: profileId,
      club_ids: clubIds,
    })) as { ok?: boolean };
    if (!result?.ok) throw new Error('Bag could not be saved');
  }

  async setProfileUnits(profileId: string, unitSystem: import('../utils/units').UnitSystem): Promise<void> {
    if (!this.socket?.connected) throw new Error('Disconnected');
    const result = (await this.socket.timeout(6000).emitWithAck('set_profile_units', {
      profile_id: profileId,
      unit_system: unitSystem,
    })) as { ok?: boolean };
    if (!result?.ok) throw new Error('Units could not be saved');
  }

  /** Store a profile's driver tee height (metres; the server validates 0–0.08). */
  async setProfileDriverTee(profileId: string, heightM: number): Promise<void> {
    if (!this.socket?.connected) throw new Error('Disconnected');
    const result = (await this.socket.timeout(6000).emitWithAck('set_profile_driver_tee', {
      profile_id: profileId,
      driver_tee_height_m: heightM,
    })) as { ok?: boolean };
    if (!result?.ok) throw new Error('Tee height could not be saved');
  }

  /** Store a profile's ball type; its next shot's club-typical spin comes from that ball's table. */
  async setProfileBallType(profileId: string, ballType: import('../utils/ballType').BallType): Promise<void> {
    if (!this.socket?.connected) throw new Error('Disconnected');
    const result = (await this.socket.timeout(6000).emitWithAck('set_profile_ball_type', {
      profile_id: profileId,
      ball_type: ballType,
    })) as { ok?: boolean };
    if (!result?.ok) throw new Error('Ball type could not be saved');
  }

  /** Store the range location on the Pi; resolves false when the server refused it or did not answer. */
  async setWeatherLocation(location: RangeLocation): Promise<boolean> {
    return (await this.emitWeather('set_weather_location', location)).ok;
  }

  /** Send a fetched or typed-in weather reading; the server validates it and answers why when it refuses. */
  sendWeatherReport(report: WeatherReport): Promise<WeatherAck> {
    return this.emitWeather('weather_report', report);
  }

  private async emitWeather(event: string, payload: object): Promise<WeatherAck> {
    if (!this.socket?.connected) return { ok: false, error: 'disconnected' };
    try {
      const result = (await this.socket.timeout(WEATHER_ACK_TIMEOUT_MS).emitWithAck(event, payload)) as WeatherAck;
      return result && result.ok === true ? { ok: true } : { ok: false, error: result?.ok === false ? result.error : 'failed' };
    } catch {
      return { ok: false, error: 'timeout' };
    }
  }

  /**
   * The menu's test button. A phone speaks the test callout itself; the kiosk
   * has no speaker, so it asks the phones to (each speaks on `voice_test`).
   */
  testVoiceCallouts() {
    if (speaksOnThisDevice()) speakTestCallout();
    else this.socket?.emit('voice_test');
  }

  /** Ask for the TI tilt calibration status (answered by `iwr6843_tilt_calibration`). */
  requestTiltCalibration() {
    this.socket?.emit('get_iwr6843_tilt_calibration');
  }

  setTrainingImplement(implement: string) {
    this.socket?.emit('set_training_implement', { implement });
  }

  simulateShot() {
    this.socket?.emit('simulate_shot');
  }

  setDemoMode(enabled: boolean) {
    this.socket?.emit('set_demo_mode', { enabled });
  }

  demoTouch() {
    this.socket?.emit('demo_touch');
  }

  deleteShot(timestamp: string): Promise<void> {
    return new Promise((resolve, reject) => {
      const settle = (finish: () => void): void => {
        clearTimeout(timer);
        this.sessionShotsListeners.delete(onShots);
        this.deleteShotErrorListeners.delete(onError);
        finish();
      };
      const onShots = (shots: Shot[]): void => {
        if (!shots.some((shot) => shot.timestamp === timestamp)) settle(resolve);
      };
      const onError = (): void => settle(() => reject(new Error('The server did not delete the shot')));
      const timer = setTimeout(
        () => settle(() => reject(new Error('No answer to the delete'))),
        DELETE_SHOT_TIMEOUT_MS
      );
      this.sessionShotsListeners.add(onShots);
      this.deleteShotErrorListeners.add(onError);
      this.socket?.emit('delete_shot', { timestamp });
    });
  }

  toggleDebug() {
    this.socket?.emit('toggle_debug');
  }

  setRadarConfig(config: Partial<RadarConfig>) {
    this.socket?.emit('set_radar_config', config);
  }

  async reinitializeRadar(): Promise<OpsRecoveryResult> {
    if (!this.socket?.connected) return { ok: false, code: 'disconnected' };
    try {
      const result = await this.socket.timeout(30000).emitWithAck('reinitialize_radar');
      if (result && typeof result.ok === 'boolean' && OPS_RECOVERY_CODES.includes(result.code)
          && result.ok === (result.code === 'commands_sent')) return result;
      return { ok: false, code: 'failed' };
    } catch {
      return { ok: false, code: 'timeout' };
    }
  }

  setCameraCaptureSettings(settings: CameraSettingsUpdate) {
    if (!this.socket?.connected) {
      useCameraStore.getState().setCaptureSettingsError('Server disconnected');
      return;
    }
    useCameraStore.getState().beginUpdate();
    this.socket?.emit('set_camera_capture_settings', settings);
  }
}

export const socketService = new SocketService();
