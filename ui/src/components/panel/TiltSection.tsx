import { useEffect, useMemo, useRef, useState } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { getOpenflightNative } from '../../native/openflightNative';
import { pickMotionSource } from '../../services/phoneMotion';
import { calibratePhoneTilt, resetPhoneTilt } from '../../services/phoneTiltApi';
import { socketService } from '../../services/socketService';
import { useSystemStore } from '../../stores/useSystemStore';
import { useTiltCalibrationStore } from '../../stores/useTiltCalibrationStore';
import type { PhoneTiltRequest, TiltCalibrationStatus } from '../../types/tiltCalibration';
import { isKioskPage } from '../../utils/kioskHost';
import { liveLabel } from '../../utils/phoneTilt';
import {
  formatTiltDeg,
  tiltDetailLines,
  tiltErrorMessage,
  tiltResultLine,
  tiltStatusLine,
} from '../../utils/tiltFormat';
import { ConfirmDeleteButton } from '../ConfirmDeleteButton';
import { ProgressIndicator } from '../ProgressIndicator';
import { createTiltFlow, type TiltFlow, type TiltFlowState } from './tiltFlow';

/** The "measured 3 days ago" text advances while the menu stays open. */
const AGE_TICK_MS = 30_000;

const IDLE: TiltFlowState = { step: 'idle', notice: null };

function useWallNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), AGE_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  return now;
}

function clientInfo(source: string): PhoneTiltRequest['client'] {
  const native = getOpenflightNative();
  return {
    motion_source: source,
    user_agent: typeof navigator === 'undefined' ? null : navigator.userAgent,
    app_version: native?.app_version ?? null,
  };
}

/** Menu block: the TI radar tilt, where it came from and, on the phone, the two-position measurement. */
export function TiltSection() {
  const status = useTiltCalibrationStore((s) => s.status);
  const connected = useSystemStore((s) => s.connected);
  const now = useWallNow();
  const kiosk = isKioskPage();
  const [flowState, setFlowState] = useState<TiltFlowState>(IDLE);
  const flowRef = useRef<TiltFlow | null>(null);
  // Stable callbacks into the flow that the effect below creates (and disposes when the menu closes).
  const actions = useMemo<TiltFlowActions>(
    () => ({
      begin: () => flowRef.current?.begin(),
      next: () => flowRef.current?.next(),
      confirm: () => flowRef.current?.confirm(),
      flip: () => flowRef.current?.flip(),
      measure: () => flowRef.current?.measure(),
      retry: () => flowRef.current?.retry(),
    }),
    []
  );

  useEffect(() => {
    // The last shot's effective tilt changes without an event; refresh it whenever the menu opens.
    socketService.requestTiltCalibration();
  }, []);

  useEffect(() => {
    if (kiosk) return undefined;
    const motion = pickMotionSource();
    const created = createTiltFlow({
      motion,
      calibrate: (request) => calibratePhoneTilt(request),
      onStatus: (next) => useTiltCalibrationStore.getState().applyStatus(next),
      onChange: setFlowState,
      clientInfo: () => clientInfo(motion.kind),
    });
    flowRef.current = created;
    return () => {
      created.dispose();
      flowRef.current = null;
    };
  }, [kiosk]);

  return (
    <TiltSectionView
      status={status}
      now={now}
      kiosk={kiosk}
      connected={connected}
      flow={flowState}
      actions={kiosk ? null : actions}
      onReset={async () => {
        useTiltCalibrationStore.getState().applyStatus(await resetPhoneTilt());
      }}
    />
  );
}

export type TiltFlowActions = Pick<TiltFlow, 'begin' | 'next' | 'confirm' | 'flip' | 'measure' | 'retry'>;

export interface TiltSectionViewProps {
  status: TiltCalibrationStatus | null;
  /** Date.now() for the "measured … ago" text. */
  now: number;
  /** The kiosk only shows the status: the phone lies on the device to measure. */
  kiosk: boolean;
  connected: boolean;
  flow: TiltFlowState;
  actions: TiltFlowActions | null;
  onReset: () => Promise<void>;
}

export function TiltSectionView({ status, now, kiosk, connected, flow, actions, onReset }: TiltSectionViewProps) {
  const { t } = useI18n();
  return (
    <section className="menu-sheet__section" aria-label={t('tilt.title')}>
      <span className="menu-sheet__section-title">{t('tilt.title')}</span>
      <span className="menu-sheet__sublabel" data-testid="tilt-status">
        {tiltStatusLine(status, now)}
      </span>
      {tiltDetailLines(status).map((line) => (
        <span key={line} className="menu-sheet__note">
          {line}
        </span>
      ))}
      {kiosk ? (
        <span className="menu-sheet__note">{t('tilt.kioskHint')}</span>
      ) : status?.enabled ? (
        <PhoneTiltSteps
          flow={flow}
          actions={actions}
          connected={connected}
          canReset={status.tilt_source === 'phone'}
          onReset={onReset}
        />
      ) : null}
    </section>
  );
}

function PhoneTiltSteps({
  flow,
  actions,
  connected,
  canReset,
  onReset,
}: {
  flow: TiltFlowState;
  actions: TiltFlowActions | null;
  connected: boolean;
  canReset: boolean;
  onReset: () => Promise<void>;
}) {
  const { t } = useI18n();
  const disabled = actions === null;

  switch (flow.step) {
    case 'idle':
      return (
        <>
          {flow.notice?.kind === 'applied' ? (
            <>
              <span className="menu-sheet__sublabel" role="status">
                {tiltResultLine(flow.notice.caseTiltDeg, flow.notice.radarTiltDeg)}
              </span>
              <span className="menu-sheet__note">{t('tilt.applied')}</span>
            </>
          ) : null}
          {flow.notice?.kind === 'denied' ? (
            <>
              <span className="menu-sheet__sublabel" role="status">
                {t('tilt.motionDenied')}
              </span>
              <span className="menu-sheet__note">{t('tilt.motionDeniedHelp')}</span>
            </>
          ) : null}
          {flow.notice?.kind === 'unsupported' ? (
            <span className="menu-sheet__note" role="status">
              {t('tilt.motionUnsupported')}
            </span>
          ) : null}
          <button
            type="button"
            className="panel-action panel-action--primary"
            disabled={disabled || !connected}
            onClick={() => actions?.begin()}
          >
            {t('tilt.calibrate')}
          </button>
          {canReset ? (
            <ConfirmDeleteButton
              className="panel-action panel-action--secondary"
              label={t('tilt.reset')}
              confirmLabel={t('tilt.confirmReset')}
              failedLabel={t('tilt.resetFailed')}
              disabled={!connected}
              onConfirm={onReset}
            />
          ) : null}
        </>
      );
    case 'intro':
      return (
        <>
          <span className="menu-sheet__note">{t('tilt.intro')}</span>
          <button
            type="button"
            className="panel-action panel-action--primary"
            disabled={disabled || flow.requesting}
            onClick={() => actions?.next()}
          >
            {t('tilt.next')}
          </button>
        </>
      );
    case 'live': {
      const pitch = flow.pitchDeg;
      return (
        <>
          {pitch === null ? (
            <span className="menu-sheet__note">{t('tilt.waitingForMotion')}</span>
          ) : (
            <span className="menu-sheet__sublabel" data-testid="tilt-live">
              {t(
                liveLabel(pitch) === 'higher'
                  ? 'tilt.liveHigher'
                  : liveLabel(pitch) === 'lower'
                    ? 'tilt.liveLower'
                    : 'tilt.liveLevel',
                { value: formatTiltDeg(Math.abs(pitch)) }
              )}
            </span>
          )}
          <span className="menu-sheet__note">{t('tilt.liveInstruction')}</span>
          <button
            type="button"
            className="panel-action panel-action--primary"
            disabled={disabled || pitch === null}
            onClick={() => actions?.confirm()}
          >
            {t('tilt.correct')}
          </button>
          <button
            type="button"
            className="panel-action panel-action--secondary"
            disabled={disabled || pitch === null}
            onClick={() => actions?.flip()}
          >
            {t('tilt.wrongWay')}
          </button>
        </>
      );
    }
    case 'ready':
      return (
        <>
          {flow.position === 'b' ? <span className="menu-sheet__note">{t('tilt.rotate')}</span> : null}
          <button
            type="button"
            className="panel-action panel-action--primary"
            disabled={disabled}
            onClick={() => actions?.measure()}
          >
            {t(flow.position === 'a' ? 'tilt.measureA' : 'tilt.measureB')}
          </button>
        </>
      );
    case 'measuring':
      return (
        <>
          {flow.unstable ? (
            <span className="menu-sheet__sublabel" role="status">
              {t('tilt.unstable')}
            </span>
          ) : null}
          <ProgressIndicator
            variant="inline"
            title={t('tilt.measuring', { n: flow.position === 'a' ? 1 : 2 })}
            detail={t('tilt.remaining', { seconds: flow.remainingS })}
          />
        </>
      );
    case 'submitting':
      return <ProgressIndicator variant="inline" title={t('tilt.submitting')} />;
    case 'failed':
      return (
        <>
          <span className="menu-sheet__sublabel" role="alert">
            {tiltErrorMessage(flow.code)}
          </span>
          <button
            type="button"
            className="panel-action panel-action--primary"
            disabled={disabled}
            onClick={() => actions?.retry()}
          >
            {t('tilt.again')}
          </button>
        </>
      );
  }
}
