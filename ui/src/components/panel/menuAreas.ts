import { LOCALES, t, type LocaleId } from '../../i18n';
import type { MessageKey } from '../../i18n/useI18n';
import { LARGE_LIVE_SLOTS, type LiveSlotId } from '../../stores/useLiveSlotsStore';
import type { Theme } from '../../theme/theme';
import type { TiltCalibrationStatus } from '../../types/tiltCalibration';
import { shownTiltDeg, formatTiltDeg } from '../../utils/tiltFormat';
import { getDistanceUnit, getSpeedUnit, type UnitSystem } from '../../utils/units';

/**
 * The device menu has two levels: a start page with quick buttons and one row per area,
 * and an area page that shows that area's sections in the same sheet.
 */
export const MENU_AREAS = ['display', 'liveTiles', 'callouts', 'calibrate', 'analysis', 'system'] as const;
export type MenuAreaId = (typeof MENU_AREAS)[number];

export const MENU_AREA_TITLE: Record<MenuAreaId, MessageKey> = {
  display: 'menuAreas.display',
  liveTiles: 'menuAreas.liveTiles',
  callouts: 'menuAreas.callouts',
  calibrate: 'menuAreas.calibrate',
  analysis: 'menuAreas.analysis',
  system: 'menuAreas.system',
};

interface AreaAvailability {
  /** The Pi's own screen: only it has configurable Live tiles. */
  kiosk: boolean;
  /** Analysis holds nothing but the view buttons, so it needs the view callback. */
  canChangeView: boolean;
}

export function visibleMenuAreas({ kiosk, canChangeView }: AreaAvailability): MenuAreaId[] {
  return MENU_AREAS.filter((area) => (area === 'liveTiles' ? kiosk : area === 'analysis' ? canChangeView : true));
}

/** Where the sheet is: the start page (`area` null) or an area page, and where the start page was scrolled to. */
export interface MenuNav {
  area: MenuAreaId | null;
  startScrollTop: number;
}

/** Every opening of the menu starts here: the sheet mounts fresh, so nothing carries over from last time. */
export const MENU_START: MenuNav = { area: null, startScrollTop: 0 };

export function openMenuArea(nav: MenuNav, area: MenuAreaId, startScrollTop: number): MenuNav {
  return nav.area === null ? { area, startScrollTop } : { ...nav, area };
}

/** Back on the start page, which scrolls to where it was when the area opened. */
export function backToMenuStart(nav: MenuNav): MenuNav {
  return { ...nav, area: null };
}

const SEPARATOR = ' · ';

export function displaySummary(unitSystem: UnitSystem, theme: Theme, locale: LocaleId): string {
  const language = LOCALES.find((option) => option.id === locale)?.nativeName ?? locale;
  return [
    getSpeedUnit(unitSystem),
    getDistanceUnit(unitSystem),
    t(theme === 'dark' ? 'menu.themeNight' : 'menu.themeSun'),
    language,
  ].join(SEPARATOR);
}

/** The two large tiles: the values the kiosk shows biggest. */
export function liveTilesSummary(slots: readonly LiveSlotId[]): string {
  return slots
    .slice(0, LARGE_LIVE_SLOTS)
    .map((id) => t(`kioskLive.label.${id}`))
    .join(SEPARATOR);
}

export function calloutsSummary(enabled: boolean): string {
  return t(enabled ? 'menu.voiceOn' : 'menu.voiceOff');
}

/** The radar tilt the shots fly with, once the server reported an active TI radar; otherwise nothing. */
export function calibrateSummary(status: TiltCalibrationStatus | null): string | null {
  if (!status?.enabled) return null;
  const deg = shownTiltDeg(status);
  return deg === null || !Number.isFinite(deg) ? null : t('menuAreas.tilt', { deg: formatTiltDeg(deg) });
}

/** Only the demo is worth a glance here: while it runs, real shots are ignored. */
export function systemSummary(demoActive: boolean): string | null {
  return demoActive ? t('menuAreas.demoOn') : null;
}
