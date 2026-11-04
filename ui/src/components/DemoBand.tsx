import { useI18n } from '../i18n/useI18n';
import { socketService } from '../services/socketService';
import './DemoBand.css';

export function DemoBand({
  active,
  connected,
  viewOnly = false,
}: {
  active: boolean;
  connected: boolean;
  viewOnly?: boolean;
}) {
  const { t } = useI18n();
  if (!active) return null;
  return (
    <div className={`demo-band${viewOnly ? ' demo-band--view-only' : ''}`} role="status">
      <span className="demo-band__text">{t('demo.band')}</span>
      {viewOnly ? null : (
        <button
          type="button"
          className="demo-band__end"
          disabled={!connected}
          onClick={() => socketService.setDemoMode(false)}
        >
          {t('demo.end')}
        </button>
      )}
    </div>
  );
}
