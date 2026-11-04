import { useState } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { LARGE_LIVE_SLOTS, LIVE_SLOT_IDS, useLiveSlotsStore, type LiveSlotId } from '../../stores/useLiveSlotsStore';
import './LiveSlotsSection.css';

/**
 * The kiosk's "Live display" menu section: its five Live tiles as a small map (two
 * large over three small). Tapping one lists every value it can show; choosing one
 * saves it on the kiosk. "Default" restores carry, ball speed, launch, direction, curve.
 */
export function LiveSlotsSection() {
  const { t } = useI18n();
  const slots = useLiveSlotsStore((s) => s.slots);
  const setSlot = useLiveSlotsStore((s) => s.setSlot);
  const reset = useLiveSlotsStore((s) => s.reset);
  const [editing, setEditing] = useState<number | null>(null);
  const slotName = (index: number) =>
    index < LARGE_LIVE_SLOTS
      ? t('kioskLive.slotLarge', { n: index + 1 })
      : t('kioskLive.slotSmall', { n: index - LARGE_LIVE_SLOTS + 1 });
  const label = (id: LiveSlotId) => t(`kioskLive.label.${id}`);
  const choose = (id: LiveSlotId) => {
    if (editing === null) return;
    setSlot(editing, id);
    setEditing(null);
  };

  return (
    <section className="menu-sheet__section live-slots" aria-labelledby="live-slots-title">
      <span className="menu-sheet__section-title" id="live-slots-title">
        {t('kioskLive.slotsTitle')}
      </span>
      <div className="live-slots__map">
        {slots.map((id, index) => (
          <button
            key={index}
            type="button"
            className={`live-slots__slot live-slots__slot--${index < LARGE_LIVE_SLOTS ? 'large' : 'small'}`}
            aria-pressed={editing === index}
            aria-label={`${slotName(index)}: ${label(id)}`}
            onClick={() => setEditing(editing === index ? null : index)}
          >
            {label(id)}
          </button>
        ))}
      </div>
      {editing !== null ? (
        <div
          className="live-slots__options"
          role="group"
          aria-label={t('kioskLive.choose', { slot: slotName(editing) })}
        >
          {LIVE_SLOT_IDS.map((id) => (
            <button
              key={id}
              type="button"
              className="live-slots__option"
              aria-pressed={slots[editing] === id}
              onClick={() => choose(id)}
            >
              {label(id)}
            </button>
          ))}
        </div>
      ) : null}
      <button
        type="button"
        className="panel-action panel-action--secondary live-slots__reset"
        onClick={() => {
          reset();
          setEditing(null);
        }}
      >
        {t('kioskLive.reset')}
      </button>
    </section>
  );
}
