import { memo, useEffect, useRef, useState } from 'react';
import type { DebugReading, RadarConfig, DebugShotLog, OpsRecoveryResult } from '../types/socket';
import type { ClockOffsets, TriggerDiagnostic, TriggerStatus } from '../types/shot';
import { formatClockOffset, isClockOffsetShown } from '../utils/clockOffset';
import './DebugPanel.css';
import { useI18n } from '../i18n/useI18n';
import { t, getHtmlLang, type MessageKey } from '../i18n';

interface DebugPanelProps {
  enabled: boolean;
  readings: DebugReading[];
  shotLogs: DebugShotLog[];
  radarConfig: RadarConfig;
  radarConfigError?: 'busy' | 'failed' | null;
  mockMode: boolean;
  onToggle: () => void;
  onUpdateConfig: (config: Partial<RadarConfig>) => void;
  triggerDiagnostics: TriggerDiagnostic[];
  triggerStatus: TriggerStatus;
  connected: boolean;
  demoActive: boolean;
  onReinitializeRadar: () => Promise<OpsRecoveryResult>;
}

const REASON_DISPLAY: Record<string, MessageKey> = {
  accepted: 'debug.acceptedReason',
  no_response: 'debug.noResponse',
  parse_failed: 'debug.parseFailed',
  no_outbound_speed: 'debug.noOutbound',
  processing_failed: 'debug.processingFailed',
  no_ball_track: 'debug.noBallTrack',
  shot_validation_failed: 'debug.validationFailed',
  capture_timebase_mismatch: 'debug.timebase',
  ti_launch_implausible: 'debug.tiLaunchImplausible',
};

function formatReason(reason: string): string {
  return REASON_DISPLAY[reason] ? t(REASON_DISPLAY[reason]) : reason;
}

function formatTimeAgo(timestamp: string): string {
  const now = Date.now();
  const then = new Date(timestamp).getTime();
  const diffSec = Math.floor((now - then) / 1000);

  if (diffSec < 5) return t('debug.justNow');
  if (diffSec < 60) return t('debug.secondsAgo', { count: diffSec });
  if (diffSec < 3600) return t('debug.minutesAgo', { count: Math.floor(diffSec / 60) });
  return t('debug.hoursAgo', { count: Math.floor(diffSec / 3600) });
}

function formatTime(timestamp: string): string {
  return new Date(timestamp).toLocaleTimeString(getHtmlLang(), {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

interface SliderControlProps {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  unit?: string;
  disabled?: boolean;
  onChange: (value: number) => void;
}

function SliderControl({ label, value, min, max, step = 1, unit = '', disabled, onChange }: SliderControlProps) {
  const [localValue, setLocalValue] = useState(value);
  const [prevValue, setPrevValue] = useState(value);
  const [dragging, setDragging] = useState(false);

  if (prevValue !== value) {
    setPrevValue(value);
    if (!dragging) {
      setLocalValue(value);
    }
  }

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setDragging(true);
    setLocalValue(parseInt(e.target.value, 10));
  };

  const handleRelease = () => {
    setDragging(false);
    if (localValue !== value) {
      onChange(localValue);
    }
  };

  return (
    <div className={`slider-control ${disabled ? 'slider-control--disabled' : ''}`}>
      <div className="slider-control__header">
        <span className="slider-control__label">{label}</span>
        <span className="slider-control__value">
          {localValue}
          {unit}
        </span>
      </div>
      <input
        type="range"
        className="slider-control__input"
        min={min}
        max={max}
        step={step}
        value={localValue}
        disabled={disabled}
        onChange={handleChange}
        onMouseUp={handleRelease}
        onTouchEnd={handleRelease}
      />
      <div className="slider-control__range">
        <span>
          {min}
          {unit}
        </span>
        <span>
          {max}
          {unit}
        </span>
      </div>
    </div>
  );
}

interface TriggerRowProps {
  diag: TriggerDiagnostic;
}

export const TriggerRow = memo(function TriggerRow({ diag }: TriggerRowProps) {
  useI18n();
  return (
    <div className={`trigger-row ${diag.accepted ? 'trigger-row--accepted' : 'trigger-row--rejected'}`}>
      <div className="trigger-row__header">
        <span className="trigger-row__time">{formatTime(diag.timestamp)}</span>
        <span
          className={`trigger-row__badge ${diag.accepted ? 'trigger-row__badge--accepted' : 'trigger-row__badge--rejected'}`}
        >
          {diag.accepted ? t('debug.hit') : t('debug.miss')}
        </span>
      </div>
      <div className="trigger-row__details">
        <span className="trigger-row__reason">{formatReason(diag.reason)}</span>
        {diag.peak_outbound_mph > 0 && (
          <span className="trigger-row__speed">
            {t('debug.out')} {diag.peak_outbound_mph.toFixed(0)} mph
          </span>
        )}
        {diag.accepted && diag.ball_speed_mph && (
          <span className="trigger-row__ball-speed">{diag.ball_speed_mph.toFixed(0)} mph</span>
        )}
        {diag.iwr6843 && (
          <span className={`trigger-row__iwr trigger-row__iwr--${diag.iwr6843.state}`}>
            TI {diag.iwr6843.state.toUpperCase()}
            {diag.iwr6843.angle_deg !== undefined && ` ${diag.iwr6843.angle_deg.toFixed(1)}°`}
            {(diag.iwr6843.angle_deg === undefined || diag.iwr6843.state !== 'accepted') &&
              `: ${formatReason(diag.iwr6843.reason)}`}
          </span>
        )}
      </div>
    </div>
  );
});

function SystemStatus({ status }: { status: TriggerStatus }) {
  return (
    <div className="debug-panel__section">
      <h4>{t('header.statusMenu')}</h4>
      <div className="system-status">
        <div className="system-status__item">
          <span className="system-status__label">{t('camera.mode')}</span>
          <span className={`system-status__badge system-status__badge--${status.mode}`}>{status.mode}</span>
        </div>
        {status.trigger_type && (
          <div className="system-status__item">
            <span className="system-status__label">{t('debug.trigger')}</span>
            <span className="system-status__value">{status.trigger_type}</span>
          </div>
        )}
        <div className="system-status__item">
          <span className="system-status__label">Radar</span>
          <span
            className={`system-status__value ${status.radar_connected ? 'system-status__value--success' : 'system-status__value--error'}`}
          >
            {status.radar_connected ? t('header.connected') : t('header.disconnected')}
          </span>
        </div>
        <div className="system-status__item">
          <span className="system-status__label">{t('debug.triggers')}</span>
          <span className="system-status__value">
            <span className="system-status__counter">{status.triggers_total}</span>
            {status.triggers_total > 0 && (
              <>
                {' '}
                (<span className="system-status__counter--accepted">{status.triggers_accepted}</span>
                {' / '}
                <span className="system-status__counter--rejected">{status.triggers_rejected}</span>)
              </>
            )}
          </span>
        </div>
        <ClockOffsetLines offsets={status.clock_offset_s} />
      </div>
    </div>
  );
}

const CLOCK_OFFSET_LABELS = { phone: 'debug.clockOffset.phone', kiosk: 'debug.clockOffset.kiosk' } as const;

function ClockOffsetLines({ offsets }: { offsets?: ClockOffsets }) {
  if (!offsets) return null;
  return (
    <>
      {(['phone', 'kiosk'] as const).map((kind) => {
        const offset = offsets[kind];
        if (!isClockOffsetShown(offset)) return null;
        return (
          <div key={kind} className="system-status__item system-status__item--clock">
            <span className="system-status__value">
              {t(CLOCK_OFFSET_LABELS[kind], { offset: formatClockOffset(offset) })}
            </span>
          </div>
        );
      })}
    </>
  );
}

function LastTriggerCard({ diag }: { diag: TriggerDiagnostic | null }) {
  if (!diag) {
    return (
      <div className="debug-panel__section">
        <h4>{t('debug.lastTrigger')}</h4>
        <p className="debug-panel__empty">{t('debug.waitTrigger')}</p>
      </div>
    );
  }

  return (
    <div className="debug-panel__section">
      <h4>{t('debug.lastTrigger')}</h4>
      <div className={`last-trigger ${diag.accepted ? 'last-trigger--accepted' : 'last-trigger--rejected'}`}>
        <div className="last-trigger__header">
          <span
            className={`last-trigger__status ${diag.accepted ? 'last-trigger__status--accepted' : 'last-trigger__status--rejected'}`}
          >
            {diag.accepted ? t('debug.accepted') : t('debug.rejected')}
          </span>
          <span className="last-trigger__time">{formatTimeAgo(diag.timestamp)}</span>
        </div>

        <div className="last-trigger__reason">{formatReason(diag.reason)}</div>

        {diag.iwr6843 && (
          <div className={`last-trigger__iwr last-trigger__iwr--${diag.iwr6843.state}`}>
            <strong>
              TI {t('header.radar')}: {diag.iwr6843.state}
            </strong>
            {diag.iwr6843.angle_deg !== undefined &&
              ` ${t('debug.atAngle', { angle: diag.iwr6843.angle_deg.toFixed(1) })}`}
            {(diag.iwr6843.angle_deg === undefined || diag.iwr6843.state !== 'accepted') &&
              ` — ${formatReason(diag.iwr6843.reason)}`}
          </div>
        )}

        <div className="last-trigger__data">
          <div className="last-trigger__speeds">
            <div className="last-trigger__speed-row">
              <span className="last-trigger__speed-label">{t('debug.outbound')}</span>
              <span className="last-trigger__speed-value">
                {t('debug.readings', { count: diag.outbound_readings })}
                {diag.peak_outbound_mph > 0 && (
                  <>
                    {t('debug.peak')} <strong>{diag.peak_outbound_mph.toFixed(1)} mph</strong>
                  </>
                )}
              </span>
            </div>
            <div className="last-trigger__speed-row">
              <span className="last-trigger__speed-label">{t('debug.inbound')}</span>
              <span className="last-trigger__speed-value">
                {t('debug.readings', { count: diag.inbound_readings })}
                {diag.peak_inbound_mph > 0 && (
                  <>
                    {t('debug.peak')} <strong>{diag.peak_inbound_mph.toFixed(1)} mph</strong>
                  </>
                )}
              </span>
            </div>
          </div>

          <div className="last-trigger__meta">
            {diag.latency_ms !== null && (
              <span className="last-trigger__meta-item">
                {t('debug.latency')}: {diag.latency_ms.toFixed(0)}ms
              </span>
            )}
            {diag.response_bytes > 0 && (
              <span className="last-trigger__meta-item">
                {t('debug.data')}: {(diag.response_bytes / 1024).toFixed(1)}KB
              </span>
            )}
            <span className="last-trigger__meta-item">
              {t('debug.readingsLabel')}: {diag.total_readings}
            </span>
          </div>

          {diag.accepted && diag.ball_speed_mph && (
            <div className="last-trigger__shot-result">
              <div className="last-trigger__shot-item">
                <span className="last-trigger__shot-label">{t('shots.colBall')}</span>
                <span className="last-trigger__shot-value">{diag.ball_speed_mph.toFixed(1)} mph</span>
              </div>
              {diag.club_speed_mph && (
                <div className="last-trigger__shot-item">
                  <span className="last-trigger__shot-label">{t('shots.colClub')}</span>
                  <span className="last-trigger__shot-value">{diag.club_speed_mph.toFixed(1)} mph</span>
                </div>
              )}
              {diag.spin_rpm && (
                <div className="last-trigger__shot-item">
                  <span className="last-trigger__shot-label">{t('metric.spin')}</span>
                  <span className="last-trigger__shot-value">{diag.spin_rpm.toFixed(0)} rpm</span>
                </div>
              )}
              {diag.carry_yards && (
                <div className="last-trigger__shot-item">
                  <span className="last-trigger__shot-label">{t('metric.carry')}</span>
                  <span className="last-trigger__shot-value">{diag.carry_yards.toFixed(0)} yds</span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

type DebugTab = 'status' | 'history' | 'tuning';

export function DebugPanel({
  radarConfig,
  radarConfigError,
  mockMode,
  onUpdateConfig,
  triggerDiagnostics,
  triggerStatus,
  connected,
  demoActive,
  onReinitializeRadar,
}: DebugPanelProps) {
  useI18n();
  const [activeTab, setActiveTab] = useState<DebugTab>('status');
  const recoveryPending = useRef(false);
  const [recovering, setRecovering] = useState(false);
  const [recoveryResult, setRecoveryResult] = useState<OpsRecoveryResult | null>(null);
  // On a phone the result lands below the fold of a short scroller; bring it into view.
  const recoveryStatusRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (recoveryResult) recoveryStatusRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [recoveryResult]);
  const [configSnapshot, setConfigSnapshot] = useState({ value: radarConfig, revision: 0 });
  if (configSnapshot.value !== radarConfig) {
    setConfigSnapshot({ value: radarConfig, revision: configSnapshot.revision + 1 });
  }
  const recoverRadar = async () => {
    if (recoveryPending.current) return;
    recoveryPending.current = true;
    setRecovering(true);
    setRecoveryResult(null);
    try {
      setRecoveryResult(await onReinitializeRadar());
    } catch {
      setRecoveryResult({ ok: false, code: 'failed' });
    } finally {
      recoveryPending.current = false;
      setRecovering(false);
    }
  };
  const isRollingBuffer = triggerStatus.mode === 'rolling-buffer';
  const isSwingSpeed = triggerStatus.mode === 'swing-speed';
  const tuningDisabled = mockMode && !isSwingSpeed;
  const lastDiag = triggerDiagnostics.length > 0 ? triggerDiagnostics[triggerDiagnostics.length - 1] : null;

  // Show last 20 triggers, newest first
  const recentTriggers = [...triggerDiagnostics].reverse().slice(0, 20);

  return (
    <div className="debug-panel">
      <div className="debug-panel__header">
        <h3>{t('debug.title')}</h3>
      </div>

      <div className="debug-tabs">
        <button
          className={`debug-tabs__tab ${activeTab === 'status' ? 'debug-tabs__tab--active' : ''}`}
          onClick={() => setActiveTab('status')}
        >
          {t('debug.status')}
        </button>
        {isRollingBuffer && (
          <button
            className={`debug-tabs__tab ${activeTab === 'history' ? 'debug-tabs__tab--active' : ''}`}
            onClick={() => setActiveTab('history')}
          >
            {t('debug.history')}
          </button>
        )}
        <button
          className={`debug-tabs__tab ${activeTab === 'tuning' ? 'debug-tabs__tab--active' : ''}`}
          onClick={() => setActiveTab('tuning')}
          disabled={recovering}
        >
          {t('debug.tuning')}
        </button>
      </div>

      <div className="debug-panel__tab-content">
        {activeTab === 'status' && (
          <>
            {isRollingBuffer && triggerStatus.trigger_type === 'sound' && !mockMode && (
              <div className="debug-panel__section debug-panel__recovery">
                <button
                  className="debug-panel__recover-button"
                  type="button"
                  disabled={recovering || !connected || demoActive}
                  onClick={() => void recoverRadar()}
                >
                  {t(recovering ? 'debug.recovery.pending' : 'debug.recovery.button')}
                </button>
                {recoveryResult && (
                  <p role="status" ref={recoveryStatusRef}>
                    {t(`debug.recovery.${recoveryResult.code}`)}
                  </p>
                )}
                <p>{t('debug.recovery.hint')}</p>
              </div>
            )}
            <SystemStatus status={triggerStatus} />
            {isRollingBuffer && <LastTriggerCard diag={lastDiag} />}
            {!isRollingBuffer && triggerStatus.mode !== 'mock' && (
              <div className="debug-panel__section">
                <p className="debug-panel__hint">{t('debug.modeHint', { mode: triggerStatus.mode })}</p>
              </div>
            )}
          </>
        )}

        {activeTab === 'history' && isRollingBuffer && (
          <div className="debug-panel__section debug-panel__section--history">
            <h4>{t('debug.triggerHistory')}</h4>
            <div className="trigger-history">
              {recentTriggers.length === 0 ? (
                <p className="debug-panel__empty">{t('debug.noTriggers')}</p>
              ) : (
                recentTriggers.map((diag, index) => <TriggerRow key={`${diag.timestamp}-${index}`} diag={diag} />)
              )}
            </div>
          </div>
        )}

        {activeTab === 'tuning' && (
          <div className="debug-panel__section">
            <h4>{t('debug.radarTuning')}</h4>
            {radarConfigError && <p role="status">{t(`debug.tuningError.${radarConfigError}`)}</p>}
            {tuningDisabled && <p className="debug-panel__mock-warning">{t('debug.mockDisabled')}</p>}
            {mockMode && isSwingSpeed && <p className="debug-panel__mock-warning">{t('debug.mockSwing')}</p>}
            <div className="debug-panel__controls" key={configSnapshot.revision}>
              {isSwingSpeed ? (
                <>
                  <SliderControl
                    label={t('debug.lowerSpeed')}
                    value={radarConfig.min_speed}
                    min={30}
                    max={100}
                    unit=" mph"
                    disabled={false}
                    onChange={(v) => onUpdateConfig({ min_speed: v })}
                  />
                  <SliderControl
                    label={t('debug.upperSpeed')}
                    value={radarConfig.max_speed}
                    min={90}
                    max={170}
                    unit=" mph"
                    disabled={false}
                    onChange={(v) => onUpdateConfig({ max_speed: v })}
                  />
                </>
              ) : (
                <>
                  <SliderControl
                    label={t('debug.minSpeed')}
                    value={radarConfig.min_speed}
                    min={0}
                    max={50}
                    unit=" mph"
                    disabled={tuningDisabled}
                    onChange={(v) => onUpdateConfig({ min_speed: v })}
                  />
                  <SliderControl
                    label={t('debug.minMagnitude')}
                    value={radarConfig.min_magnitude}
                    min={0}
                    max={2000}
                    step={50}
                    disabled={tuningDisabled}
                    onChange={(v) => onUpdateConfig({ min_magnitude: v })}
                  />
                </>
              )}
              <SliderControl
                label={t('debug.txPower')}
                value={radarConfig.transmit_power}
                min={0}
                max={7}
                disabled={mockMode}
                onChange={(v) => onUpdateConfig({ transmit_power: v })}
              />
            </div>
            <p className="debug-panel__hint">{isSwingSpeed ? t('debug.speedHint') : t('debug.txHint')}</p>
          </div>
        )}
      </div>
    </div>
  );
}
