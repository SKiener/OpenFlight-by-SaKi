import { isLocaleId, LOCALE_STORAGE_KEY, type LocaleId } from '../i18n';
import { useLocaleStore } from '../stores/useLocaleStore';

export const COMPANION_PATH = '/companion';
export const COMPANION_MANIFEST_URL = '/companion-manifest.json';
const COMPANION_ICON_URL = '/openflight-icon-black.png';

export function isCompanionPath(pathname: string): boolean {
  return pathname.replace(/\/$/, '') === COMPANION_PATH;
}

export function browserLocale(languages: readonly string[]): LocaleId | null {
  for (const language of languages) {
    const id = language.toLowerCase().split('-')[0];
    if (isLocaleId(id)) return id;
  }
  return null;
}

/** A phone has never chosen a language in the kiosk menu, so follow the device language. */
export function adoptBrowserLocale(storage: Pick<Storage, 'getItem'>, languages: readonly string[]): void {
  let stored: string | null = null;
  try {
    stored = storage.getItem(LOCALE_STORAGE_KEY);
  } catch {
    stored = null;
  }
  if (isLocaleId(stored)) return;
  const locale = browserLocale(languages);
  if (locale) useLocaleStore.getState().setLocale(locale);
}

function upsertHeadElement(doc: Document, tag: 'meta' | 'link', key: 'name' | 'rel', value: string) {
  let element = doc.head.querySelector<HTMLElement>(`${tag}[${key}="${value}"]`);
  if (!element) {
    element = doc.createElement(tag);
    element.setAttribute(key, value);
    doc.head.appendChild(element);
  }
  return element;
}

/** Home-screen metadata for the companion only; the kiosk shell stays a plain page. */
export function installCompanionHead(doc: Document): void {
  doc.title = 'OpenFlight Companion';
  upsertHeadElement(doc, 'link', 'rel', 'manifest').setAttribute('href', COMPANION_MANIFEST_URL);
  upsertHeadElement(doc, 'link', 'rel', 'apple-touch-icon').setAttribute('href', COMPANION_ICON_URL);
  const metas: Record<string, string> = {
    'apple-mobile-web-app-capable': 'yes',
    'mobile-web-app-capable': 'yes',
    'apple-mobile-web-app-status-bar-style': 'black-translucent',
    'apple-mobile-web-app-title': 'OpenFlight',
    'theme-color': '#000000',
  };
  for (const [name, content] of Object.entries(metas)) {
    upsertHeadElement(doc, 'meta', 'name', name).setAttribute('content', content);
  }
  upsertHeadElement(doc, 'meta', 'name', 'viewport').setAttribute(
    'content',
    'width=device-width, initial-scale=1.0, viewport-fit=cover'
  );
}
