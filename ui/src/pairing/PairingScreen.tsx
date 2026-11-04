import { useEffect, useEffectEvent, useRef, useState, type FormEvent } from 'react';
import { useI18n } from '../i18n/useI18n';
import { adoptBrowserLocale } from '../companion/companionShell';
import { exchangePairingCode, isCompletePairingCode } from './pairingApi';
import { feedbackMessage, type PairingFeedback } from './pairingView';
import { exchangesInitialCode } from './companionHandoff';
import './pairing.css';

interface PairingScreenProps {
  /**
   * A code from a pairing QR link; exchanged once without typing, unless `appLink` is set.
   */
  initialCode?: string | null;
  /**
   * `openflight://` link to this page for the iOS companion app. When set (iOS browser outside
   * the app), the code is not used automatically: the app pairs with it itself, and pairing here
   * takes one tap on the pre-filled code.
   */
  appLink?: string | null;
  onPaired: () => void;
}

export function PairingScreen({ initialCode = null, appLink = null, onPaired }: PairingScreenProps) {
  const { t } = useI18n();
  const autoExchange = exchangesInitialCode(initialCode, appLink);
  const [code, setCode] = useState(initialCode ?? '');
  const [feedback, setFeedback] = useState<PairingFeedback>(autoExchange ? { kind: 'pending' } : { kind: 'idle' });
  const autoSubmitted = useRef(false);

  const exchange = async (value: string) => {
    const result = await exchangePairingCode(value);
    setFeedback(result);
    if (result.kind === 'paired') onPaired();
  };
  const exchangeInitialCode = useEffectEvent((value: string) => void exchange(value));

  useEffect(() => {
    adoptBrowserLocale(window.localStorage, navigator.languages ?? [navigator.language]);
  }, []);

  useEffect(() => {
    if (!autoExchange || !initialCode || autoSubmitted.current) return;
    autoSubmitted.current = true;
    exchangeInitialCode(initialCode);
  }, [autoExchange, initialCode]);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setFeedback({ kind: 'pending' });
    void exchange(code);
  };

  const busy = feedback.kind === 'pending' || feedback.kind === 'paired';
  const message = feedbackMessage(feedback);

  return (
    <main className="pairing-screen" aria-labelledby="pairing-title">
      <section className="pairing-screen__card">
        <h1 id="pairing-title">{t('pairing.title')}</h1>
        <p className="pairing-screen__intro">{t('pairing.intro')}</p>
        {appLink ? (
          <div className="pairing-screen__form">
            <button
              type="button"
              className="pairing-screen__submit"
              onClick={() => window.location.assign(appLink)}
              disabled={busy}
            >
              {t('pairing.openInApp')}
            </button>
            <p className="pairing-screen__intro">{t('pairing.openInAppHint')}</p>
          </div>
        ) : null}
        <form className="pairing-screen__form" onSubmit={handleSubmit}>
          <label htmlFor="pairing-code">{t('pairing.codeLabel')}</label>
          <input
            id="pairing-code"
            className="pairing-screen__input"
            value={code}
            onChange={(event) => setCode(event.target.value)}
            placeholder="XXXX-XXXX"
            autoComplete="one-time-code"
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            enterKeyHint="go"
            maxLength={12}
            disabled={busy}
          />
          <button
            type="submit"
            className={appLink ? 'pairing-screen__submit pairing-screen__submit--secondary' : 'pairing-screen__submit'}
            disabled={busy || !isCompletePairingCode(code)}
          >
            {feedback.kind === 'pending' ? t('pairing.pending') : t('pairing.submit')}
          </button>
        </form>
        {message ? (
          <p className={`pairing-screen__message pairing-screen__message--${message.role}`} role={message.role}>
            {t(message.key, message.vars)}
          </p>
        ) : null}
      </section>
    </main>
  );
}
