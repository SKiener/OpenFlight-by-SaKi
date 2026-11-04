import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import { Entry } from './Entry';
import { applyTheme, readStoredTheme } from './theme/theme';
import { useLocaleStore } from './stores/useLocaleStore';
import { startScreenWakeLock } from './services/screenWakeLock';

applyTheme(readStoredTheme());
useLocaleStore.getState();
// Phones in a secure context keep the screen on; no-op over plain http, in the app and kiosk.
startScreenWakeLock();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Entry />
  </StrictMode>
);
