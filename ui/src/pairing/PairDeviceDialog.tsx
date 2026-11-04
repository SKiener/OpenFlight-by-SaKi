import { useCallback, useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n/useI18n';
import { socketService } from '../services/socketService';
import { PanelAction } from '../components/panel/PanelAction';
import { ProgressIndicator } from '../components/ProgressIndicator';
import { SegmentedControl } from '../components/ui/SegmentedControl';
import { formatCountdown, formatPairingCode, issuePairingCode, typedPairingAddress } from './pairingApi';
import { pairDialogView, type PairDialogState } from './pairingView';
import './pairing.css';

export type PairQrMode = 'pair' | 'wifi';

interface PairDeviceViewProps {
  state: PairDialogState;
  nowMs: number;
  hostIndex: number;
  qrMode: PairQrMode;
  onSelectHost: (index: number) => void;
  onSelectQrMode: (mode: PairQrMode) => void;
  onNewCode: () => void;
  onClose: () => void;
}

export function PairDeviceView({
  state,
  nowMs,
  hostIndex,
  qrMode,
  onSelectHost,
  onSelectQrMode,
  onNewCode,
  onClose,
}: PairDeviceViewProps) {
  const { t } = useI18n();
  const view = pairDialogView(state, nowMs);
  const links = view.kind === 'code' ? view.issued.links : [];
  const link = links[Math.min(hostIndex, links.length - 1)];
  const wifi = view.kind === 'code' ? view.issued.wifi : null;
  const wifiQr = wifi?.qrSvg && wifi.ssid !== null ? { src: wifi.qrSvg, ssid: wifi.ssid } : null;
  const showWifiQr = wifiQr !== null && qrMode === 'wifi';
  const qr = showWifiQr
    ? { src: wifiQr.src, alt: t('pairing.wifiQrAlt', { ssid: wifiQr.ssid }) }
    : link
      ? { src: link.qrSvg, alt: t('pairing.qrAlt', { host: link.host }) }
      : null;

  return (
    <div className="pair-dialog">
      <button type="button" className="pair-dialog__scrim" aria-label={t('history.close')} onClick={onClose} />
      <div className="pair-dialog__panel" role="dialog" aria-modal="true" aria-labelledby="pair-dialog-title">
        <h2 id="pair-dialog-title">{t('pairing.pairDevice')}</h2>
        {view.kind === 'loading' ? <ProgressIndicator variant="inline" title={t('pairing.loading')} /> : null}
        {view.kind === 'notice' ? (
          <div className="pair-dialog__details">
            <p className="pair-dialog__notice" role={view.role}>
              {t(view.key)}
            </p>
            {view.detailKey ? <p>{t(view.detailKey)}</p> : null}
          </div>
        ) : null}
        {view.kind === 'code' ? (
          <div className="pair-dialog__content">
            {qr || wifiQr ? (
              <div className={`pair-dialog__qr-column${wifiQr ? ' pair-dialog__qr-column--toggle' : ''}`}>
                {qr ? <img className="pair-dialog__qr" src={qr.src} alt={qr.alt} /> : null}
                {wifiQr ? (
                  <SegmentedControl<PairQrMode>
                    ariaLabel={t('pairing.qrModeLabel')}
                    value={showWifiQr ? 'wifi' : 'pair'}
                    options={[
                      { id: 'pair', label: t('pairing.qrModePair') },
                      { id: 'wifi', label: t('pairing.qrModeWifi') },
                    ]}
                    onChange={onSelectQrMode}
                  />
                ) : null}
              </div>
            ) : null}
            <div className="pair-dialog__details">
              <output className="pair-dialog__code" aria-label={t('pairing.codeLabel')}>
                {formatPairingCode(view.issued.code)}
              </output>
              <span className="pair-dialog__countdown">
                {t('pairing.validFor', { time: formatCountdown(view.secondsLeft) })}
              </span>
              {showWifiQr ? (
                <p>{t('pairing.wifiScan', { ssid: wifiQr.ssid })}</p>
              ) : link ? (
                <p>{t('pairing.scanOrType', { address: typedPairingAddress(link.url) })}</p>
              ) : null}
              {view.issued.wifi ? (
                <div className="pair-dialog__wifi">
                  <span className="pair-dialog__wifi-title">{t('pairing.wifiTitle')}</span>
                  <dl>
                    {view.issued.wifi.ssid !== null ? (
                      <div>
                        <dt>{t('pairing.wifiName')}</dt>
                        <dd>{view.issued.wifi.ssid}</dd>
                      </div>
                    ) : null}
                    <div>
                      <dt>{t('pairing.wifiPassword')}</dt>
                      <dd className="pair-dialog__wifi-password">{view.issued.wifi.password}</dd>
                    </div>
                  </dl>
                </div>
              ) : null}
              {links.length > 1 && !showWifiQr ? (
                <SegmentedControl
                  ariaLabel={t('pairing.address')}
                  value={String(Math.min(hostIndex, links.length - 1))}
                  options={links.map((option, index) => ({ id: String(index), label: option.host }))}
                  onChange={(id) => onSelectHost(Number(id))}
                />
              ) : null}
            </div>
          </div>
        ) : null}
        <div className="pair-dialog__actions">
          {view.kind !== 'loading' ? (
            <PanelAction variant="secondary" onClick={onNewCode}>
              {t('pairing.newCode')}
            </PanelAction>
          ) : null}
          <PanelAction autoFocus onClick={onClose}>
            {t('history.close')}
          </PanelAction>
        </div>
      </div>
    </div>
  );
}

export function PairDeviceDialog({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<PairDialogState>({ kind: 'loading' });
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [hostIndex, setHostIndex] = useState(0);
  const [qrMode, setQrMode] = useState<PairQrMode>('pair');
  const latestRequest = useRef(0);

  const issue = useCallback(() => {
    const request = ++latestRequest.current;
    issuePairingCode(Date.now()).then(
      (issued) => {
        if (request !== latestRequest.current) return;
        setNowMs(Date.now());
        setQrMode('pair');
        setState({ kind: 'ready', issued });
      },
      () => {
        if (request === latestRequest.current) setState({ kind: 'error' });
      }
    );
  }, []);

  useEffect(() => {
    if (latestRequest.current === 0) issue();
  }, [issue]);

  useEffect(() => {
    if (state.kind !== 'ready') return;
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [state.kind]);

  useEffect(
    () =>
      socketService.onPairingCodeClosed((reason) =>
        setState((current) => (current.kind === 'ready' ? { kind: 'closed', reason } : current))
      ),
    []
  );

  const newCode = () => {
    setState({ kind: 'loading' });
    issue();
  };

  return (
    <PairDeviceView
      state={state}
      nowMs={nowMs}
      hostIndex={hostIndex}
      qrMode={qrMode}
      onSelectHost={setHostIndex}
      onSelectQrMode={setQrMode}
      onNewCode={newCode}
      onClose={onClose}
    />
  );
}
