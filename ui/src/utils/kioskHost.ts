const KIOSK_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

// The kiosk loads the local server; phones and other browsers come over the network. A port is ignored.
export function isKioskHost(host: string): boolean {
  return KIOSK_HOSTS.has(host.replace(/:\d+$/, '').toLowerCase());
}

/** Whether this page is the Pi's own kiosk browser (also true without a window, e.g. server rendering). */
export function isKioskPage(): boolean {
  return typeof window === 'undefined' || isKioskHost(window.location.host);
}
