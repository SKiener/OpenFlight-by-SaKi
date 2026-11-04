/**
 * Hand-off from the pairing page in an iOS browser to the OpenFlight iOS companion app
 * (ios/OpenFlightCompanion/App/CompanionDeepLink.swift). The app has its own cookie store, so it
 * pairs with the code itself: on iOS outside the app the page therefore does not use the
 * one-time code automatically, and offers "open in the app" next to pairing here.
 */

/** The custom scheme link the companion handles: `openflight://open?url=<page>`. */
export function companionAppLink(pageUrl: string): string {
  return `openflight://open?url=${encodeURIComponent(pageUrl)}`;
}

/** `applicationNameForUserAgent` of the companion's web view (CompanionServices.userAgentToken). */
const COMPANION_UA_TOKEN = 'OpenFlightCompanion/';

export interface BrowserEnv {
  userAgent: string;
  platform?: string;
  maxTouchPoints?: number;
  /** `window.openflightNative`, present only inside the companion app. */
  openflightNative?: unknown;
}

function isIOS(env: BrowserEnv): boolean {
  if (/\b(iPhone|iPad|iPod)\b/.test(env.userAgent)) return true;
  // iPadOS asks for desktop pages with a Mac user agent; a Mac has no touch points.
  return env.platform === 'MacIntel' && (env.maxTouchPoints ?? 0) > 1;
}

/** True inside the OpenFlight iOS companion app's web view. */
export function isCompanionApp(env: BrowserEnv): boolean {
  // Any bridge object counts, whatever its protocol version.
  return env.userAgent.includes(COMPANION_UA_TOKEN) || env.openflightNative != null;
}

/** True in an iOS browser outside the companion app: there the app link is offered. */
export function offersCompanionApp(env: BrowserEnv): boolean {
  return isIOS(env) && !isCompanionApp(env);
}

export function currentBrowserEnv(): BrowserEnv {
  if (typeof navigator === 'undefined') return { userAgent: '' };
  return {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    maxTouchPoints: navigator.maxTouchPoints,
    openflightNative: typeof window === 'undefined' ? undefined : window.openflightNative,
  };
}

/** Whether the pairing screen uses its QR code right away; on iOS the user picks app or browser first. */
export function exchangesInitialCode(initialCode: string | null, appLink: string | null): boolean {
  return Boolean(initialCode) && !appLink;
}
