import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import type { TiltCalibrationStatus } from '../../types/tiltCalibration';
import {
  MENU_AREAS,
  MENU_START,
  backToMenuStart,
  calibrateSummary,
  calloutsSummary,
  displaySummary,
  liveTilesSummary,
  openMenuArea,
  systemSummary,
  visibleMenuAreas,
} from './menuAreas';

const TILT: TiltCalibrationStatus = {
  enabled: true,
  configured_iwr_tilt_deg: 3.2,
  tilt_source: 'cli',
  case_top_to_boresight_deg: 0,
  inclinometer_enabled: false,
  last_effective_iwr_tilt_deg: null,
  last_effective_at: null,
  calibration: null,
};

afterEach(() => {
  setActiveLocale('en');
});

describe('menu areas', () => {
  it('has six areas in the approved order', () => {
    expect(MENU_AREAS).toEqual(['display', 'liveTiles', 'callouts', 'calibrate', 'analysis', 'system']);
  });

  it('shows Live tiles on the kiosk only and Analysis only with the view callback', () => {
    expect(visibleMenuAreas({ kiosk: true, canChangeView: true })).toEqual([...MENU_AREAS]);
    expect(visibleMenuAreas({ kiosk: false, canChangeView: true })).toEqual([
      'display',
      'callouts',
      'calibrate',
      'analysis',
      'system',
    ]);
    expect(visibleMenuAreas({ kiosk: false, canChangeView: false })).toEqual([
      'display',
      'callouts',
      'calibrate',
      'system',
    ]);
  });
});

describe('menu navigation', () => {
  it('starts on the start page at the top', () => {
    expect(MENU_START).toEqual({ area: null, startScrollTop: 0 });
  });

  it('opens an area and goes back to where the start page was scrolled', () => {
    const opened = openMenuArea(MENU_START, 'calibrate', 240);
    expect(opened).toEqual({ area: 'calibrate', startScrollTop: 240 });

    const back = backToMenuStart(opened);
    expect(back).toEqual({ area: null, startScrollTop: 240 });

    // Another area from there remembers the new start-page position.
    expect(openMenuArea(back, 'system', 80)).toEqual({ area: 'system', startScrollTop: 80 });
  });

  it('keeps the start-page position when an area page is replaced by another', () => {
    const opened = openMenuArea(MENU_START, 'display', 120);
    expect(openMenuArea(opened, 'system', 0)).toEqual({ area: 'system', startScrollTop: 120 });
  });

  it('does not change the shared start state', () => {
    backToMenuStart(openMenuArea(MENU_START, 'display', 50));
    expect(MENU_START).toEqual({ area: null, startScrollTop: 0 });
  });
});

describe('menu area summaries', () => {
  it('sums up Display as speed, distance, theme and language', () => {
    expect(displaySummary('metric', 'sun', 'de')).toBe('km/h · m · Sun · Deutsch');
    expect(displaySummary('imperial', 'dark', 'en')).toBe('mph · yds · Night · English');
    expect(displaySummary('mph-meters', 'sun', 'es')).toBe('mph · m · Sun · Español');
    expect(displaySummary('kmh-yards', 'dark', 'en')).toBe('km/h · yds · Night · English');

    setActiveLocale('de');
    expect(displaySummary('metric', 'sun', 'de')).toBe('km/h · m · Sonne · Deutsch');
    expect(displaySummary('metric', 'dark', 'de')).toBe('km/h · m · Nacht · Deutsch');
  });

  it('names the two large Live tiles', () => {
    expect(liveTilesSummary(['carry', 'ball_speed', 'launch_v', 'launch_h', 'curve'])).toBe('Carry · Ball speed');
    setActiveLocale('de');
    expect(liveTilesSummary(['spin', 'club_speed', 'launch_v', 'launch_h', 'curve'])).toBe('Spin · Schlägertempo');
  });

  it('says whether callouts are on', () => {
    expect(calloutsSummary(true)).toBe('On');
    expect(calloutsSummary(false)).toBe('Off');
    setActiveLocale('de');
    expect(calloutsSummary(true)).toBe('An');
    expect(calloutsSummary(false)).toBe('Aus');
  });

  it('shows the radar tilt only once the server reported an active TI radar', () => {
    expect(calibrateSummary(null)).toBeNull();
    expect(calibrateSummary({ ...TILT, enabled: false })).toBeNull();
    expect(calibrateSummary({ ...TILT, configured_iwr_tilt_deg: null })).toBeNull();
    expect(calibrateSummary(TILT)).toBe('Tilt 3.2°');

    setActiveLocale('de');
    expect(calibrateSummary(TILT)).toBe('Neigung 3,2°');
  });

  it('prefers the last shot’s effective tilt with the inclinometer, like the Radar tilt section', () => {
    const status = { ...TILT, inclinometer_enabled: true, last_effective_iwr_tilt_deg: 4.75 };
    expect(calibrateSummary(status)).toBe('Tilt 4.8°');
  });

  it('mentions System only while the demo runs', () => {
    expect(systemSummary(false)).toBeNull();
    expect(systemSummary(true)).toBe('Demo on');
    setActiveLocale('de');
    expect(systemSummary(true)).toBe('Demo an');
  });
});
