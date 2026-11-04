import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { useDragScroll } from '../../hooks/useDragScroll';
import { LOCALES } from '../../i18n';
import { useI18n } from '../../i18n/useI18n';
import { useSystemStore } from '../../stores/useSystemStore';
import { useThemeStore } from '../../stores/useThemeStore';
import { useLocaleStore } from '../../stores/useLocaleStore';
import { useUnitPreference } from '../../state/useUnitPreference';
import { getDistanceUnit, getSpeedUnit, withDistanceUnit, withSpeedUnit, type UnitSystem } from '../../utils/units';
import { useProfileStore } from '../../stores/useProfileStore';
import { useLiveSlotsStore } from '../../stores/useLiveSlotsStore';
import { useTiltCalibrationStore } from '../../stores/useTiltCalibrationStore';
import { useVoiceCalloutStore } from '../../stores/useVoiceCalloutStore';
import { socketService } from '../../services/socketService';
import { SegmentedControl } from '../ui/SegmentedControl';
import { SimStatus } from '../SimStatus';
import { VoiceCalloutSection } from './VoiceCalloutSection';
import { TiltSection } from './TiltSection';
import { DriverTeeSection } from './DriverTeeSection';
import { BallTypeSection } from './BallTypeSection';
import { WeatherSection } from './WeatherSection';
import { LiveSlotsSection } from './LiveSlotsSection';
import { isKioskPage } from '../../utils/kioskHost';
import type { PanelView } from './views';
import {
  MENU_AREA_TITLE,
  MENU_START,
  backToMenuStart,
  calibrateSummary,
  calloutsSummary,
  displaySummary,
  liveTilesSummary,
  openMenuArea,
  systemSummary,
  visibleMenuAreas,
  type MenuAreaId,
  type MenuNav,
} from './menuAreas';

interface DemoSectionProps {
  active: boolean;
  unavailable: boolean;
  connected: boolean;
  onChange: (enabled: boolean) => void;
}

/** Offline, socket.io would queue the switch and start or end the demo on reconnect; so it waits for the server. */
export function DemoSection({ active, unavailable, connected, onChange }: DemoSectionProps) {
  const { t } = useI18n();
  return (
    <section className="menu-sheet__section">
      <span className="menu-sheet__section-title">{t('demo.title')}</span>
      <button
        type="button"
        className={`menu-sheet__chip${active ? ' menu-sheet__chip--active' : ''}`}
        aria-pressed={active}
        disabled={unavailable || !connected}
        onClick={() => onChange(!active)}
      >
        {t('demo.mode')}
      </button>
      <span className="menu-sheet__note">{t(unavailable ? 'demo.ballModeOnly' : 'demo.explain')}</span>
    </section>
  );
}

interface MenuSheetProps {
  onClose: () => void;
  onShutdown: () => void;
  onOpenHistory?: () => void;
  onOpenBag?: () => void;
  onChangeView?: (view: PanelView) => void;
  onPairDevice?: () => void;
  onRevokeDevices?: () => void;
  /** Open on this area page instead of the start page (server-rendered tests; the app always starts at the top). */
  initialArea?: MenuAreaId;
}

/**
 * The sheet behind the footer logo button (design doc 6a `menuOpen6`).
 *
 * Two levels in one sheet: the start page has the quick buttons (clubs, history, pairing),
 * one row per area with a short summary of its state, and Shut down at the bottom. A row
 * swaps the sheet's content for that area's sections under a back button. The sheet is
 * mounted only while open, so every opening starts on the start page.
 * Battery lives in the footer; socket connection on the panel header LED.
 */
export function MenuSheet({
  onClose,
  onShutdown,
  onOpenHistory,
  onOpenBag,
  onChangeView,
  onPairDevice,
  onRevokeDevices,
  initialArea,
}: MenuSheetProps) {
  const simStatuses = useSystemStore((state) => state.simStatuses);
  const { t } = useI18n();
  const { unitSystem, setUnitSystem } = useUnitPreference();
  const activeProfileId = useProfileStore((state) => state.activeProfileId);
  const connected = useSystemStore((state) => state.connected);
  const demoActive = useSystemStore((state) => state.demoActive);
  const demoUnavailable = useSystemStore((state) => state.demoUnavailable !== null);
  const liveSlots = useLiveSlotsStore((state) => state.slots);
  const voiceEnabled = useVoiceCalloutStore((state) => state.enabled);
  const tiltStatus = useTiltCalibrationStore((state) => state.status);
  const [savingUnits, setSavingUnits] = useState(false);
  const [unitsFailed, setUnitsFailed] = useState(false);
  const changeUnits = async (units: UnitSystem) => {
    if (savingUnits) return;
    setSavingUnits(true);
    setUnitsFailed(false);
    try {
      await socketService.setProfileUnits(activeProfileId, units);
      setUnitSystem(units);
    } catch {
      setUnitsFailed(true);
    } finally {
      setSavingUnits(false);
    }
  };
  const { theme, setTheme } = useThemeStore();
  const { locale, setLocale } = useLocaleStore();
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(scrollRef);
  const pairing = Boolean(onPairDevice && onRevokeDevices);
  const kiosk = isKioskPage();
  const areas = visibleMenuAreas({ kiosk, canChangeView: Boolean(onChangeView) });

  const [nav, setNav] = useState<MenuNav>(() => (initialArea ? { ...MENU_START, area: initialArea } : MENU_START));
  const area = nav.area !== null && areas.includes(nav.area) ? nav.area : null;
  // The row to focus again after Back, so a keyboard or screen reader user lands where they left.
  const lastAreaRef = useRef<MenuAreaId | null>(null);
  const backRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // The level-1 tilt summary shows the last shot's effective tilt, which changes without an event.
    socketService.requestTiltCalibration();
  }, []);

  useLayoutEffect(() => {
    const sheet = scrollRef.current;
    if (!sheet) return;
    if (area !== null) {
      sheet.scrollTop = 0;
      backRef.current?.focus({ preventScroll: true });
    } else if (lastAreaRef.current !== null) {
      sheet.scrollTop = nav.startScrollTop;
      sheet.querySelector<HTMLElement>(`[data-menu-area="${lastAreaRef.current}"]`)?.focus({ preventScroll: true });
    }
  }, [area, nav.startScrollTop]);

  const open = (next: MenuAreaId) => {
    lastAreaRef.current = next;
    setNav((current) => openMenuArea(current, next, scrollRef.current?.scrollTop ?? 0));
  };

  const summaries: Record<MenuAreaId, string | null> = {
    display: displaySummary(unitSystem, theme, locale),
    liveTiles: liveTilesSummary(liveSlots),
    callouts: calloutsSummary(voiceEnabled),
    calibrate: calibrateSummary(tiltStatus),
    analysis: null,
    system: systemSummary(demoActive),
  };

  const areaContent: Record<MenuAreaId, () => ReactNode> = {
    display: () => (
      <>
        <fieldset
          className="menu-sheet__section"
          style={{ border: 0, padding: 0, margin: 0 }}
          disabled={savingUnits || !connected || !activeProfileId}
        >
          <span className="menu-sheet__section-title">{t('units.speed')}</span>
          <SegmentedControl
            ariaLabel={t('units.speed')}
            value={getSpeedUnit(unitSystem)}
            options={[
              { id: 'mph', label: 'mph' },
              { id: 'km/h', label: 'km/h' },
            ]}
            onChange={(speed: 'mph' | 'km/h') => void changeUnits(withSpeedUnit(unitSystem, speed))}
          />
          <span className="menu-sheet__section-title">{t('units.distance')}</span>
          <SegmentedControl
            ariaLabel={t('units.distance')}
            value={getDistanceUnit(unitSystem)}
            options={[
              { id: 'm', label: t('units.meters') },
              { id: 'yds', label: 'Yards' },
            ]}
            onChange={(distance: 'm' | 'yds') => void changeUnits(withDistanceUnit(unitSystem, distance))}
          />
        </fieldset>
        {unitsFailed ? <p role="alert">{t('units.error')}</p> : null}

        <section className="menu-sheet__section">
          <span className="menu-sheet__section-title">{t('menu.theme')}</span>
          <SegmentedControl
            ariaLabel={t('menu.theme')}
            value={theme}
            options={[
              { id: 'sun', label: t('menu.themeSun') },
              { id: 'dark', label: t('menu.themeNight') },
            ]}
            onChange={setTheme}
          />
        </section>

        <section className="menu-sheet__section">
          <span className="menu-sheet__section-title">{t('menu.language')}</span>
          <div className="menu-sheet__languages" role="group" aria-label={t('menu.language')}>
            {LOCALES.map((option) => (
              <button
                key={option.id}
                type="button"
                className="panel-action panel-action--secondary"
                onClick={() => setLocale(option.id)}
                aria-pressed={locale === option.id}
              >
                {option.nativeName}
              </button>
            ))}
          </div>
        </section>
      </>
    ),
    // The kiosk's own Live tiles; phones keep their fixed layout (the row is kiosk-only).
    liveTiles: () => <LiveSlotsSection />,
    callouts: () => <VoiceCalloutSection />,
    calibrate: () => (
      <>
        <TiltSection />
        <DriverTeeSection />
        <BallTypeSection />
        <WeatherSection />
      </>
    ),
    analysis: () => (
      <section className="range-tools">
        {(['shots', 'stats', 'profiles'] as const).map((view) => (
          <button
            key={view}
            type="button"
            className="panel-action panel-action--secondary"
            onClick={() => onChangeView?.(view)}
          >
            {t(`nav.${view}`)}
          </button>
        ))}
      </section>
    ),
    system: () => (
      <>
        {onChangeView ? (
          <button type="button" className="panel-action panel-action--secondary" onClick={() => onChangeView('debug')}>
            {t('nav.debug')}
          </button>
        ) : null}

        <DemoSection
          active={demoActive}
          unavailable={demoUnavailable}
          connected={connected}
          onChange={(enabled) => socketService.setDemoMode(enabled)}
        />

        {Object.keys(simStatuses).length > 0 ? (
          <section className="menu-sheet__section">
            <div className="menu-sheet__status-row">
              <span className="menu-sheet__status-label">{t('menu.simulators')}</span>
              <SimStatus statuses={simStatuses} />
            </div>
          </section>
        ) : null}

        {pairing ? (
          <section className="menu-sheet__section">
            <span className="menu-sheet__section-title">{t('pairing.devices')}</span>
            <button type="button" className="panel-action panel-action--danger" onClick={onRevokeDevices}>
              {t('pairing.revokeAll')}
            </button>
          </section>
        ) : null}
      </>
    ),
  };

  return (
    <>
      <button type="button" className="panel-scrim" onClick={onClose} aria-label={t('menu.close')} />
      <div
        className="menu-sheet"
        ref={scrollRef}
        {...dragScroll}
        role="dialog"
        aria-modal="true"
        aria-label={t('menu.title')}
        data-menu-level={area === null ? 'start' : 'area'}
      >
        {area === null ? (
          <>
            <button className="panel-action panel-action--secondary" onClick={onClose}>
              {t('history.close')}
            </button>
            {onOpenBag ? (
              <button className="panel-action" onClick={onOpenBag}>
                {t('bag.title')}
              </button>
            ) : null}
            {onOpenHistory ? (
              <button className="panel-action panel-action--secondary" onClick={onOpenHistory}>
                {t('history.title')}
              </button>
            ) : null}
            {pairing ? (
              <button type="button" className="panel-action panel-action--secondary" onClick={onPairDevice}>
                {t('pairing.pairDevice')}
              </button>
            ) : null}

            <nav className="menu-sheet__areas" aria-label={t('menu.title')}>
              {areas.map((id) => (
                <button
                  key={id}
                  type="button"
                  className="menu-sheet__area"
                  data-menu-area={id}
                  onClick={() => open(id)}
                >
                  <span className="menu-sheet__area-name">{t(MENU_AREA_TITLE[id])}</span>
                  <span className="menu-sheet__area-summary">{summaries[id] ?? ''}</span>
                  <span className="menu-sheet__area-chevron" aria-hidden="true">
                    ›
                  </span>
                </button>
              ))}
            </nav>

            <button type="button" className="menu-sheet__shutdown" onClick={onShutdown}>
              {t('menu.shutdown')}
            </button>
          </>
        ) : (
          <>
            <div className="menu-sheet__area-header">
              <button ref={backRef} type="button" className="menu-sheet__back" onClick={() => setNav(backToMenuStart)}>
                {t('menuAreas.back')}
              </button>
              <h2 className="menu-sheet__area-title" id="menu-sheet-area-title">
                {t(MENU_AREA_TITLE[area])}
              </h2>
            </div>
            <div className="menu-sheet__area-page" role="group" aria-labelledby="menu-sheet-area-title">
              {areaContent[area]()}
            </div>
          </>
        )}
      </div>
    </>
  );
}
