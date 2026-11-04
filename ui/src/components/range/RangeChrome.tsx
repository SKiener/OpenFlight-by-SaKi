import { useEffect, useRef, useState, type ReactNode, type Ref } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { getHtmlLang } from '../../i18n';
import { useSystemStore } from '../../stores/useSystemStore';
import { useDebugStore } from '../../stores/useDebugStore';
import { useThemeStore } from '../../stores/useThemeStore';
import { PowerIndicator, PowerWarning } from '../PowerStatus';
import { StatusMenu } from '../panel/StatusMenu';
import type { PanelView } from '../panel/views';

const paths = {
  live: 'M3 13h4l3-8 4 14 3-6h4',
  flag: 'M5 21V4m0 0h11l-2 4 2 4H5',
  history: 'M3 5v6h6M3 11a9 9 0 1 1 2 7M12 7v5l3 2',
  aim: 'M12 2v5m0 10v5M2 12h5m10 0h5M12 5a7 7 0 1 0 0 14 7 7 0 1 0 0-14',
  settings: 'M3 7h18M3 17h18M8 4v6M16 14v6',
  play: 'm9 5 10 7-10 7Z',
  sun: 'M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2M12 8a4 4 0 1 0 0 8 4 4 0 1 0 0-8',
  moon: 'M20 15A9 9 0 0 1 9 4a9 9 0 1 0 11 11Z',
};
function Icon({ name }: { name: keyof typeof paths }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name]} /></svg>;
}

function PowerDialog({ onClose }: { onClose: () => void }) {
  const { t } = useI18n();
  const status = useSystemStore((s) => s.powerStatus);
  const connected = useSystemStore((s) => s.connected);
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const dialog = ref.current; dialog?.showModal(); return () => dialog?.close(); }, []);
  return <dialog className="range-power-dialog" ref={ref} onCancel={onClose} aria-labelledby="range-power-title">
    <header><h2 id="range-power-title">{t('range.power')}</h2><button onClick={onClose}>{t('history.close')}</button></header>
    {!connected ? <p role="alert">{t('range.offline')}</p> : null}
    {status?.available ? <><PowerIndicator status={status} /><dl>
      <dt>{t('range.voltage')}</dt><dd>{status.battery_voltage_v?.toLocaleString(getHtmlLang(), { minimumFractionDigits: 2, maximumFractionDigits: 2 }) ?? '—'} V</dd>
      <dt>{t('range.power')}</dt><dd>{status.external_power === null ? '—' : t(status.external_power ? 'power.pluggedIn' : 'power.onBattery')}</dd>
      <dt>{t('range.lastUpdate')}</dt><dd>{status.updated_at ? new Date(status.updated_at).toLocaleTimeString(getHtmlLang()) : '—'}</dd>
    </dl><p>{t('range.powerNote')}</p></> : <p>{t('power.unavailable')}</p>}
  </dialog>;
}

/** The status word of the header's connection button, and the kiosk power button's spoken status. */
function useConnectionStatus(): { online: boolean; label: string } {
  const { t } = useI18n();
  const connected = useSystemStore((s) => s.connected);
  const mock = useSystemStore((s) => s.mockMode);
  const radar = useDebugStore((s) => s.triggerStatus.radar_connected);
  return {
    online: connected && radar,
    label: t(!connected ? 'header.serverDisconnected' : mock ? 'range.mock' : radar ? 'range.opsConnected' : 'range.opsMissing'),
  };
}

/**
 * The kiosk's compact power button: a connection dot and the battery percentage. It
 * opens the power details; the status word is spoken, not shown.
 */
export function RangePowerButton() {
  const { t } = useI18n();
  const power = useSystemStore((s) => s.powerStatus);
  const connection = useConnectionStatus();
  const [open, setOpen] = useState(false);
  return <>
    <button className="range-power-button range-power-button--kiosk" onClick={() => setOpen(true)} aria-haspopup="dialog" aria-label={`${t('range.power')} · ${connection.label}`}>
      <i className={`range-power-button__dot ${connection.online ? 'online' : 'offline'}`} aria-hidden="true" />
      {power ? <PowerIndicator status={power} variant="chrome" /> : <span>—</span>}
    </button>
    {open ? <PowerDialog onClose={() => setOpen(false)} /> : null}
  </>;
}

interface RangeHeaderProps {
  ref?: Ref<HTMLDivElement>;
  children?: ReactNode; // more top chrome under the header, e.g. the demo band
  /**
   * The Pi's own screen: one compact row with only the power button (the Live overview
   * hides it and carries that button in its own row, range.css). No brand, theme or status word.
   */
  kiosk?: boolean;
}

/** The top chrome: header, offline line and `children`; its dialogs and warnings lie outside it, over everything. */
export function RangeHeader({ ref, children, kiosk = false }: RangeHeaderProps) {
  const { t } = useI18n();
  const { theme, setTheme } = useThemeStore();
  const connected = useSystemStore((s) => s.connected);
  const power = useSystemStore((s) => s.powerStatus);
  const radar = useDebugStore((s) => s.triggerStatus.radar_connected);
  const connection = useConnectionStatus();
  const [statusOpen, setStatusOpen] = useState(false);
  const [powerOpen, setPowerOpen] = useState(false);
  const warning = connected && power?.available && !power.external_power && (power.state === 'low' || power.state === 'critical') ? power.state : null;
  return <>
    <div className="range-app__top" ref={ref}>
      {kiosk ? <header className="range-header range-header--kiosk"><RangePowerButton /></header> : <header className="range-header">
        <div className="range-brand">OpenFlight<span>Launch Monitor</span></div>
        <div className="range-header-actions">
          <button className="range-theme" onClick={() => setTheme(theme === 'dark' ? 'sun' : 'dark')} aria-label={t(theme === 'dark' ? 'range.toSun' : 'range.toNight')}><Icon name={theme === 'dark' ? 'sun' : 'moon'} /></button>
          <button className="range-connection" onClick={() => setStatusOpen(true)} aria-haspopup="dialog"><i className={connection.online ? 'online' : 'offline'} />{connection.label}</button>
          <button className="range-power-button" onClick={() => setPowerOpen(true)} aria-label={t('range.power')}>{power ? <PowerIndicator status={power} variant="chrome" /> : <span>{t('range.power')} —</span>}</button>
        </div>
      </header>}
      {!connected ? <div className="range-disconnected" role="status">{t('range.offline')}</div> : null}
      {children}
    </div>
    {statusOpen ? <StatusMenu connected={connected} radarConnected={radar} onClose={() => setStatusOpen(false)} /> : null}
    {powerOpen ? <PowerDialog onClose={() => setPowerOpen(false)} /> : null}
    {warning && power?.battery_percent != null ? <PowerWarning key={warning} level={warning} percentage={power.battery_percent} /> : null}
  </>;
}

interface RangeFooterProps {
  ref?: Ref<HTMLElement>;
  currentView: PanelView;
  onChangeView: (view: PanelView) => void;
  onOpenMenu: () => void;
  menuOpen: boolean;
  onOpenHistory: () => void;
  historyOpen: boolean;
  onReplay: () => void;
  hasReplay: boolean;
  showRange: boolean;
}
export function RangeFooter({ ref, ...props }: RangeFooterProps) {
  const { t } = useI18n();
  const shows = (view: PanelView) => props.currentView === view && !props.menuOpen && !props.historyOpen;
  return <footer className="range-footer" ref={ref}><nav aria-label={t('nav.panels')}>
    <button aria-pressed={shows('live')} onClick={() => props.onChangeView('live')}><Icon name="live" />{t('nav.live')}</button>
    {props.showRange ? <button aria-pressed={shows('virtual-range')} onClick={() => props.onChangeView('virtual-range')}><Icon name="flag" />{t('virtualRange.tab')}</button> : null}
    <button aria-pressed={props.historyOpen} onClick={props.onOpenHistory}><Icon name="history" />{t('range.history')}</button>
    <button aria-pressed={shows('camera')} onClick={() => props.onChangeView('camera')}><Icon name="aim" />{t('range.align')}</button>
    <button aria-expanded={props.menuOpen} aria-label={t('nav.openMenu')} onClick={props.onOpenMenu}><Icon name="settings" />{t('range.device')}</button>
  </nav><button className="range-replay-button" onClick={props.onReplay} disabled={!props.hasReplay}><Icon name="play" />{t('replay.open')}<small>0,1×</small></button></footer>;
}
