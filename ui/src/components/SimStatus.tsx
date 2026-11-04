import { useI18n } from '../i18n/useI18n';
import { t, type MessageKey } from '../i18n';
import type { SimStatus as SimStatusData } from '../types/socket';
import './SimStatus.css';

const DISPLAY_NAMES: Record<string, string> = {
  gspro: 'GSPro',
  opengolfsim: 'OpenGolfSim',
  partee: 'PAR-TEE',
};

const STATE_LABELS: Record<SimStatusData['state'], MessageKey> = {
  connected: 'sim.connected',
  connecting: 'sim.connecting',
  reconnecting: 'sim.reconnecting',
  disabled: 'sim.off',
  stopped: 'sim.disconnected',
  error: 'sim.error',
};

// Map a connection state to a visual severity bucket.
function severity(state: SimStatusData['state']): 'ok' | 'warn' | 'error' | 'off' {
  switch (state) {
    case 'connected':
      return 'ok';
    case 'connecting':
    case 'reconnecting':
      return 'warn';
    case 'error':
      return 'error';
    default:
      return 'off';
  }
}

function pillTitle(s: SimStatusData): string {
  const where = s.host ? `${s.host}:${s.port ?? ''}` : '';
  if (s.state === 'reconnecting' && s.next_retry_in_s) {
    return `${where} — ${t('sim.retry', { seconds: s.next_retry_in_s, attempt: s.attempt ?? '' })}`.trim();
  }
  if (s.state === 'error' && s.message) {
    return `${where} — ${s.message}`.trim();
  }
  return where || t(STATE_LABELS[s.state]);
}

interface SimStatusProps {
  statuses: Record<string, SimStatusData>;
}

export function SimStatus({ statuses }: SimStatusProps) {
  const { t } = useI18n();
  const entries = Object.values(statuses);
  if (entries.length === 0) {
    return null; // No simulator connectors configured.
  }
  return (
    <div className="sim-status" role="group" aria-label={t('sim.connectors')}>
      {entries.map((s) => (
        <div key={s.target} className={`sim-status__pill sim-status__pill--${severity(s.state)}`} title={pillTitle(s)}>
          <span className="sim-status__dot" />
          <span className="sim-status__name">{DISPLAY_NAMES[s.target] ?? s.target}</span>
          <span className="sim-status__state">{t(STATE_LABELS[s.state])}</span>
        </div>
      ))}
    </div>
  );
}
