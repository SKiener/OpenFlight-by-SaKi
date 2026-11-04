import type { PanelView } from '../components/panel/views';
import { isKioskHost } from '../utils/kioskHost';
import { isRangeDemo } from '../virtual-range/rangeDemoSwitch';

export const LAST_AREA_KEY = 'openflight.last-area:v1';

export function readLastArea(storage: Pick<Storage, 'getItem'>, hostname: string): PanelView {
  return isKioskHost(hostname) && storage.getItem(LAST_AREA_KEY) === 'virtual-range' ? 'virtual-range' : 'live';
}

export function writeLastArea(storage: Pick<Storage, 'setItem'>, view: PanelView): void {
  storage.setItem(LAST_AREA_KEY, view === 'virtual-range' ? 'virtual-range' : 'live');
}

// The Pi's render test loads ?rangeDemo=1 in a fresh profile and needs the range open.
export function startArea(storage: Pick<Storage, 'getItem'>, search: string, hostname: string): PanelView {
  return isRangeDemo(search) ? 'virtual-range' : readLastArea(storage, hostname);
}
