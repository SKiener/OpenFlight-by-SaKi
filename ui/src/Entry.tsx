import { lazy, Suspense } from 'react';
import { isCompanionPath } from './companion/companionShell';
import { PairingGate, PairPage } from './pairing/PairingGate';
import { isPairPath } from './pairing/pairingView';

const App = lazy(() => import('./App'));
const AnalysisPage = lazy(() => import('./analysis/AnalysisPage'));
const CompanionPage = lazy(() => import('./companion/CompanionPage'));

function selectPage() {
  if (new URLSearchParams(window.location.search).get('view') === 'analysis') return <AnalysisPage />;
  if (isCompanionPath(window.location.pathname)) return <CompanionPage />;
  return <App />;
}

export function Entry() {
  if (isPairPath(window.location.pathname)) return <PairPage />;
  return (
    <PairingGate>
      <Suspense fallback={<p>OpenFlight…</p>}>{selectPage()}</Suspense>
    </PairingGate>
  );
}
