import { useState } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { socketService } from '../../services/socketService';
import { useProfileStore } from '../../stores/useProfileStore';
import { useSystemStore } from '../../stores/useSystemStore';
import { activeBallType, type BallType } from '../../utils/ballType';
import { SegmentedControl } from '../ui/SegmentedControl';

/** Menu block ("Kalibrieren"): the active profile's ball type, saved on the device. */
export function BallTypeSection() {
  const activeProfileId = useProfileStore((state) => state.activeProfileId);
  const ballType = useProfileStore(activeBallType);
  const connected = useSystemStore((state) => state.connected);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);

  const change = async (next: BallType) => {
    if (saving || next === ballType) return;
    setSaving(true);
    setFailed(false);
    try {
      // The server answers after broadcasting the roster, so the store already holds the new value.
      await socketService.setProfileBallType(activeProfileId, next);
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <BallTypeSectionView
      ballType={ballType}
      disabled={saving || !connected || !activeProfileId}
      failed={failed}
      onChange={(next) => void change(next)}
    />
  );
}

export interface BallTypeSectionViewProps {
  /** The effective ball type (the profile's setting, else the launcher default). */
  ballType: BallType;
  disabled: boolean;
  failed: boolean;
  onChange: (ballType: BallType) => void;
}

export function BallTypeSectionView({ ballType, disabled, failed, onChange }: BallTypeSectionViewProps) {
  const { t } = useI18n();
  return (
    <fieldset
      className="menu-sheet__section"
      style={{ border: 0, padding: 0, margin: 0 }}
      aria-label={t('ballType.title')}
      disabled={disabled}
    >
      <span className="menu-sheet__section-title">{t('ballType.title')}</span>
      <SegmentedControl
        ariaLabel={t('ballType.title')}
        value={ballType}
        options={[
          { id: 'standard', label: t('ballType.standard') },
          { id: 'range', label: t('ballType.range') },
        ]}
        onChange={onChange}
      />
      <span className="menu-sheet__note">{t('ballType.hint')}</span>
      {failed ? <p role="alert">{t('ballType.error')}</p> : null}
    </fieldset>
  );
}
