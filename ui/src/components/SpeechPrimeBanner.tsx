import { useSyncExternalStore } from 'react';
import { useI18n } from '../i18n/useI18n';
import { speaksOnThisDevice } from '../services/shotAnnouncer';
import { useVoiceCalloutStore } from '../stores/useVoiceCalloutStore';
import { isSpeechPrimed, isSpeechSupported, subscribeSpeechPrimed } from '../utils/speech';
import './DemoBand.css';

/**
 * Safari only speaks after a tap on the page. The callouts may have been
 * switched on from another screen, so this page may never have had one: until
 * the first tap primes speech (useVoiceCalloutLifecycle), a band says so. It
 * blocks nothing; any tap, on the band or elsewhere, primes and hides it. The
 * companion app speaks natively and never shows it, nor does the kiosk.
 */
export function SpeechPrimeBanner() {
  const { t } = useI18n();
  const enabled = useVoiceCalloutStore((state) => state.enabled);
  const primed = useSyncExternalStore(subscribeSpeechPrimed, isSpeechPrimed, isSpeechPrimed);

  if (!enabled || primed || !speaksOnThisDevice() || !isSpeechSupported()) return null;
  return (
    <div className="demo-band demo-band--view-only" role="status">
      <span className="demo-band__text">{t('menu.voicePrime')}</span>
    </div>
  );
}
