import { useEffect, useState, type ReactNode } from 'react';
import { refreshPairingAccess, usePairingStore } from '../stores/usePairingStore';
import { PairingScreen } from './PairingScreen';
import { codeFromHash } from './pairingApi';
import { companionAppLink, currentBrowserEnv, offersCompanionApp } from './companionHandoff';

/** Shows the app to the kiosk and paired devices, and the code entry to everyone else. */
export function PairingGate({ children }: { children: ReactNode }) {
  const access = usePairingStore((state) => state.access);

  useEffect(() => {
    void refreshPairingAccess();
  }, []);

  if (access === 'checking') return <p>OpenFlight…</p>;
  if (access === 'unpaired') return <PairingScreen onPaired={() => window.location.reload()} />;
  return children;
}

/**
 * Target of the kiosk's QR code: pairs with the code from the link, then opens
 * the app, which phones show in their portrait layout (/companion stays reachable).
 */
export function PairPage() {
  const [initialCode] = useState(() => codeFromHash(window.location.hash));
  // The full QR link, code fragment included, read before the fragment is removed below.
  const [appLink] = useState(() =>
    offersCompanionApp(currentBrowserEnv()) ? companionAppLink(window.location.href) : null
  );

  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname);
  }, []);

  return <PairingScreen initialCode={initialCode} appLink={appLink} onPaired={() => window.location.replace('/')} />;
}
