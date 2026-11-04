import { useI18n } from '../../i18n/useI18n';
import { socketService } from '../../services/socketService';
import { speaksOnThisDevice } from '../../services/shotAnnouncer';
import {
  CALLOUT_LANGUAGES,
  CALLOUT_METRIC_LABEL_KEYS,
  CALLOUT_METRICS,
  isCalloutLanguage,
} from '../../services/shotCallout';
import { useVoiceCalloutStore } from '../../stores/useVoiceCalloutStore';
import { cancelSpeech, isSpeechSupported, primeSpeech, speaksNatively } from '../../utils/speech';
import { SegmentedControl } from '../ui/SegmentedControl';

/**
 * Menu block for spoken shot callouts. The setting is shared by every screen
 * (the server holds it); the phones speak, the kiosk never does. On the kiosk
 * the block says so in its title, and its test button asks the phones to
 * speak. The value chips, voice language, test button and hint only show while
 * callouts are on.
 */
export function VoiceCalloutSection() {
  const { t } = useI18n();
  const { enabled, metrics, language, setEnabled, toggleMetric, setLanguage } = useVoiceCalloutStore();
  const speaksHere = speaksOnThisDevice();
  const title = speaksHere ? t('menu.voice') : t('menu.voiceKiosk');
  // Safari's conditions (screen on, in front, Silent mode off) do not apply in the app, which speaks natively.
  const hint = !speaksHere ? 'menu.voiceHintKiosk' : speaksNatively() ? 'menu.voiceHintApp' : 'menu.voiceHint';

  // The kiosk only switches the phones' callouts, so its own browser's speech does not matter.
  if (speaksHere && !isSpeechSupported()) {
    return (
      <section className="menu-sheet__section">
        <span className="menu-sheet__section-title">{title}</span>
        <span className="menu-sheet__note">{t('menu.voiceUnavailable')}</span>
      </section>
    );
  }

  const handleEnabled = (id: 'off' | 'on') => {
    if (id === 'on') {
      // Runs inside the tap, which is what unlocks speech on iOS.
      if (speaksHere) primeSpeech();
      setEnabled(true);
    } else {
      setEnabled(false);
      cancelSpeech();
    }
  };

  return (
    <section className="menu-sheet__section">
      <span className="menu-sheet__section-title">{title}</span>
      <SegmentedControl
        ariaLabel={title}
        value={enabled ? 'on' : 'off'}
        options={[
          { id: 'off', label: t('menu.voiceOff') },
          { id: 'on', label: t('menu.voiceOn') },
        ]}
        onChange={handleEnabled}
      />
      {enabled ? (
        <>
          <span className="menu-sheet__sublabel">{t('menu.voiceMetrics')}</span>
          <div className="menu-sheet__chips" role="group" aria-label={t('menu.voiceMetrics')}>
            {CALLOUT_METRICS.map((metric) => {
              const active = metrics.includes(metric);
              // The last chosen value cannot be removed (toggleMetric ignores it).
              const locked = active && metrics.length === 1;
              return (
                <button
                  key={metric}
                  type="button"
                  className={`menu-sheet__chip${active ? ' menu-sheet__chip--active' : ''}`}
                  aria-pressed={active}
                  aria-disabled={locked || undefined}
                  onClick={() => toggleMetric(metric)}
                >
                  {t(CALLOUT_METRIC_LABEL_KEYS[metric])}
                </button>
              );
            })}
          </div>
          <span className="menu-sheet__sublabel">{t('menu.voiceLanguage')}</span>
          <select
            className="menu-sheet__select"
            aria-label={t('menu.voiceLanguage')}
            value={language}
            onChange={(event) => {
              if (isCalloutLanguage(event.target.value)) setLanguage(event.target.value);
            }}
          >
            {CALLOUT_LANGUAGES.map((option) => (
              <option key={option.id} value={option.id}>
                {option.nativeName}
              </option>
            ))}
          </select>
          <button type="button" className="menu-sheet__action" onClick={() => socketService.testVoiceCallouts()}>
            {t('menu.voiceTest')}
          </button>
          <span className="menu-sheet__note">{t(hint)}</span>
        </>
      ) : null}
    </section>
  );
}
