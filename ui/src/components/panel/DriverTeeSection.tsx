import { useState } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { socketService } from '../../services/socketService';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useProfileStore } from '../../stores/useProfileStore';
import { useSystemStore } from '../../stores/useSystemStore';
import { MAX_DRIVER_TEE_HEIGHT_M, activeDriverTeeHeightM, formatDriverTee, stepDriverTee } from '../../utils/driverTee';
import type { UnitSystem } from '../../utils/units';

/** Menu block ("Kalibrieren"): the active profile's driver tee height, saved on the device. */
export function DriverTeeSection() {
  const activeProfileId = useProfileStore((state) => state.activeProfileId);
  const heightM = useProfileStore(activeDriverTeeHeightM);
  const connected = useSystemStore((state) => state.connected);
  const { unitSystem } = useUnitPreference();
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  const change = async (heightM: number) => {
    if (saving) return;
    setSaving(true);
    setFailed(false);
    try {
      // The server answers after broadcasting the roster, so the store already holds the new value.
      await socketService.setProfileDriverTee(activeProfileId, heightM);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <DriverTeeSectionView
      heightM={heightM}
      unitSystem={unitSystem}
      disabled={saving || !connected || !activeProfileId}
      failed={failed}
      onChange={(next) => void change(next)}
    />
  );
}

export interface DriverTeeSectionViewProps {
  heightM: number;
  unitSystem: UnitSystem;
  disabled: boolean;
  failed: boolean;
  /** The next height in metres (already stepped and clamped). */
  onChange: (heightM: number) => void;
}

export function DriverTeeSectionView({ heightM, unitSystem, disabled, failed, onChange }: DriverTeeSectionViewProps) {
  const { t } = useI18n();
  return (
    <section className="menu-sheet__section" aria-label={t('driverTee.title')}>
      <span className="menu-sheet__section-title">{t('driverTee.title')}</span>
      <div className="menu-sheet__status-row">
        <button
          type="button"
          className="panel-action panel-action--secondary"
          aria-label={t('driverTee.lower')}
          disabled={disabled || heightM <= 0}
          onClick={() => onChange(stepDriverTee(heightM, -1))}
        >
          −
        </button>
        <span className="menu-sheet__status-value" data-testid="driver-tee-value" aria-live="polite">
          {formatDriverTee(heightM, unitSystem)}
        </span>
        <button
          type="button"
          className="panel-action panel-action--secondary"
          aria-label={t('driverTee.higher')}
          disabled={disabled || heightM >= MAX_DRIVER_TEE_HEIGHT_M}
          onClick={() => onChange(stepDriverTee(heightM, 1))}
        >
          +
        </button>
      </div>
      <span className="menu-sheet__note">{t('driverTee.hint')}</span>
      {failed ? <p role="alert">{t('driverTee.error')}</p> : null}
    </section>
  );
}
