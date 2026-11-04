import { useState } from 'react';
import { useI18n } from '../i18n/useI18n';
import { PanelAction } from '../components/panel/PanelAction';
import { revokeAllDevices } from './pairingApi';
import './pairing.css';

export type RevokeState = 'confirm' | 'pending' | 'done' | 'error';

interface RevokeDevicesViewProps {
  state: RevokeState;
  onConfirm: () => void;
  onClose: () => void;
}

export function RevokeDevicesView({ state, onConfirm, onClose }: RevokeDevicesViewProps) {
  const { t } = useI18n();
  const done = state === 'done';
  const pending = state === 'pending';

  return (
    <div className="clear-session-modal">
      <button
        type="button"
        className="clear-session-modal__scrim"
        aria-label={t('history.close')}
        onClick={onClose}
        disabled={pending}
      />
      <div
        className="clear-session-modal__dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="revoke-devices-title"
        aria-describedby={done ? undefined : 'revoke-devices-detail'}
      >
        <span id="revoke-devices-title" className="clear-session-modal__title">
          {done ? t('pairing.revokeDone') : t('pairing.revokeConfirm')}
        </span>
        {done ? null : (
          <p id="revoke-devices-detail" className="clear-session-dialog__detail">
            {t('pairing.revokeDetail')}
          </p>
        )}
        {state === 'error' ? (
          <p className="pair-dialog__notice" role="alert">
            {t('pairing.revokeError')}
          </p>
        ) : null}
        <div className="clear-session-modal__actions">
          {done ? (
            <PanelAction autoFocus onClick={onClose}>
              {t('history.close')}
            </PanelAction>
          ) : (
            <>
              <PanelAction variant="danger" onClick={onConfirm} disabled={pending}>
                {t('pairing.revokeAction')}
              </PanelAction>
              <PanelAction variant="secondary" autoFocus onClick={onClose} disabled={pending}>
                {t('shutdown.cancel')}
              </PanelAction>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function RevokeDevicesDialog({ onClose }: { onClose: () => void }) {
  const [state, setState] = useState<RevokeState>('confirm');

  const confirm = () => {
    setState('pending');
    revokeAllDevices().then(
      () => setState('done'),
      () => setState('error')
    );
  };

  return <RevokeDevicesView state={state} onConfirm={confirm} onClose={onClose} />;
}
