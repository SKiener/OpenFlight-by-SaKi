import { useEffect, useRef, useState } from 'react';
import { ALL_CLUBS, CLUBS_BY_TYPE, getBagClubIds, getClubName, toggleBagClub } from '../data/clubs';
import { useDragScroll } from '../hooks/useDragScroll';
import { clubGroupLabel } from '../i18n';
import { useI18n } from '../i18n/useI18n';
import { socketService } from '../services/socketService';
import { useSystemStore } from '../stores/useSystemStore';
import type { Profile } from '../types/profile';
import { PanelAction } from './panel/PanelAction';
import './BagSettingsDialog.css';

export function BagSettingsDialog({ profile, onClose }: { profile: Profile; onClose: () => void }) {
  const { t } = useI18n();
  const connected = useSystemStore((state) => state.connected);
  const [selected, setSelected] = useState(() => getBagClubIds(profile.settings));
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(scrollRef);

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    return () => dialog?.close();
  }, []);

  const save = async () => {
    setSaving(true);
    setFailed(false);
    try {
      await socketService.setProfileClubs(profile.id, selected);
      onClose();
    } catch {
      setFailed(true);
    } finally {
      setSaving(false);
    }
  };

  return <dialog ref={dialogRef} className="bag-settings" aria-labelledby="bag-title"
    onCancel={(event) => { if (saving) event.preventDefault(); else onClose(); }}>
    <header className="bag-settings__header">
      <div><h2 id="bag-title">{t('bag.title')}</h2><p>{t('bag.count', { count: selected.length })}</p></div>
      <PanelAction variant="secondary" disabled={saving} onClick={onClose}>{t('shutdown.cancel')}</PanelAction>
    </header>
    <div className="bag-settings__scroll" ref={scrollRef} {...dragScroll}>
      <p>{t('bag.description', { name: profile.name })}</p>
      {Object.entries(CLUBS_BY_TYPE).map(([group, clubs]) => <section key={group} aria-label={clubGroupLabel(group)}>
        <h3>{clubGroupLabel(group)}</h3>
        <div className="bag-settings__grid">
          {clubs.map((club) => <button type="button" key={club.id} aria-pressed={selected.includes(club.id)}
            disabled={saving || (selected.length === 1 && selected.includes(club.id))}
            onClick={() => { setSelected((ids) => toggleBagClub(ids, club.id)); setFailed(false); }}>
            <span aria-hidden="true">{selected.includes(club.id) ? '✓' : '+'}</span>{getClubName(club.id)}
          </button>)}
        </div>
      </section>)}
      <p className="bag-settings__note">{t('bag.models')}</p>
    </div>
    <footer className="bag-settings__footer">
      <p role={failed ? 'alert' : 'status'}>{failed ? t('bag.error') : !connected ? t('range.offline') : t('bag.note')}</p>
      <PanelAction disabled={saving || !connected || selected.length < 1 || selected.length > ALL_CLUBS.length} onClick={() => void save()}>
        {t(saving ? 'bag.saving' : 'bag.save')}
      </PanelAction>
    </footer>
  </dialog>;
}
