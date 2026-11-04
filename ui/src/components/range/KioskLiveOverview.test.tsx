import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { makeShot } from '../../test-utils/shot';
import type { Shot } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import type { CameraCaptureSettings } from '../../stores/useCameraStore';
import type { LiveSlotId } from '../../stores/useLiveSlotsStore';
import { RangeLivePanel } from './RangeLivePanel';
import { KioskTileView } from './KioskLiveOverview';
import { LiveSlotsSection } from '../panel/LiveSlotsSection';

const preference = vi.hoisted(() => ({ unitSystem: 'metric' as UnitSystem }));
vi.mock('../../state/useUnitPreference', () => ({ useUnitPreference: () => preference }));
// Server rendering reads a zustand store's initial state, so the stores this screen reads are stubbed.
const stores = vi.hoisted(() => ({
  slots: null as LiveSlotId[] | null,
  captureSettings: { available: false } as CameraCaptureSettings,
  calibrated: false,
}));
vi.mock('../../stores/useLiveSlotsStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../stores/useLiveSlotsStore')>();
  const state = () => ({ slots: stores.slots ?? [...actual.DEFAULT_LIVE_SLOTS], setSlot: () => {}, reset: () => {} });
  return { ...actual, useLiveSlotsStore: (select: (s: ReturnType<typeof state>) => unknown) => select(state()) };
});
vi.mock('../../stores/useCameraStore', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../stores/useCameraStore')>()),
  useCameraStore: (select: (s: { captureSettings: CameraCaptureSettings }) => unknown) => select({ captureSettings: stores.captureSettings }),
}));
vi.mock('../../stores/useSystemStore', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../stores/useSystemStore')>()),
  useDirectionCalibrated: () => stores.calibrated,
}));

const shot = makeShot({
  ball_speed_mph: 120.2, carry_spin_adjusted: 166, total_yards: 174, launch_angle_vertical: 16.4,
  launch_angle_horizontal: 1.6, launch_angle_horizontal_source: 'radar', curve_class: 'draw', profile_id: 'p',
});
const props = { shots: [shot], profileId: 'p', profileName: 'Golfer', clubLabel: 'Driver' };
const render = (s: Shot = shot, extra: Partial<Parameters<typeof RangeLivePanel>[0]> = {}) =>
  renderToString(<RangeLivePanel {...props} shot={s} shots={[s]} kiosk {...extra} />).replace(/<!-- -->/g, '');
const titles = (html: string) =>
  [...html.matchAll(/<span class="kiosk-tile__title">([\s\S]*?)<\/span><span class="kiosk-tile__body">/g)].map((m) =>
    m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
  );
const values = (html: string) => [...html.matchAll(/class="kiosk-tile__value">([^<]*)</g)].map((m) => m[1]);
const target = { frame_x_px: 1, frame_y_px: 1, preview_x_pct: 50, preview_y_pct: 50, azimuth_deg: 0, elevation_deg: 0, set_at: 1 };

afterEach(() => {
  setActiveLocale('en');
  stores.slots = null;
  stores.captureSettings = { available: false };
  stores.calibrated = false;
});

describe('the kiosk Live overview', () => {
  it('shows the default slots: carry and ball speed large, launch, direction and curve small', () => {
    setActiveLocale('de');
    stores.calibrated = true;
    const html = render();
    expect([...html.matchAll(/class="kiosk-tile kiosk-tile--(large|small) kiosk-tile--(\w+)/g)].map((m) => `${m[1]}:${m[2]}`)).toEqual([
      'large:carry', 'large:ball_speed', 'small:launch_v', 'small:launch_h', 'small:curve',
    ]);
    expect(titles(html)).toEqual(['Flugweite m', 'Balltempo km/h', 'Abflug', 'Richtung', 'Kurve ≈']);
    expect(values(html)).toEqual(['152', '193', '16°', '2° R', 'Draw']);
  });

  it('leaves the long notes, quality dots and the profile to All values', () => {
    setActiveLocale('de');
    const html = render(makeShot({ ...shot, carry_spin_source: 'club_typical', carry_spin_rpm: 7500, spin_quality: 'high' }));
    for (const note of ['Total', 'Gemessen', 'OPS-Radar', 'nicht kalibriert', 'typischem Spin', 'metric-card__confidence', 'Golfer']) {
      expect(html, note).not.toContain(note);
    }
    expect(titles(html)[0]).toBe('Flugweite ≈ m');
  });

  it('puts ≈ in the title of an uncalibrated start direction', () => {
    setActiveLocale('de');
    expect(titles(render())[3]).toBe('Richtung ≈');
  });

  it('shows the stored slots', () => {
    stores.slots = ['club_speed', 'smash', 'spin', 'club_path', 'club_aoa'];
    const html = render(makeShot({ ...shot, spin_rpm: 10000, carry_spin_rpm: 10000, smash_factor: 1.448 }));
    // Path and attack angle have no ground truth yet: ≈ in their titles.
    expect(titles(html)).toEqual(['Club speed km/h', 'Smash', 'Spin rpm', 'Club path ≈', 'Attack angle ≈']);
    expect(values(html)).toEqual(['109', '1.45', '10,000', '-1°', '+2°']);
  });

  it('has one header row: shot number, delete, all values, the club and the power button', () => {
    setActiveLocale('de');
    const html = render(shot, { headerAction: <button className="panel-action">Driver</button> });
    const header = html.match(/<div class="kiosk-live__header">([\s\S]*?)<div class="kiosk-live__grid"/)?.[1] ?? '';
    expect(header).toContain('<h1>Schlag\u00a01</h1>');
    expect(header).toMatch(/aria-label="Letzten Schlag löschen"[^>]*>Löschen</);
    expect(header).toContain('>Alle Werte<');
    expect(header).toContain('>Driver<');
    expect(header).toContain('range-power-button--kiosk');
    for (const dropped of ['OpenFlight', 'range-theme', 'Simulation', 'Golfer']) expect(header, dropped).not.toContain(dropped);
  });

  it('keeps the swing-speed layout', () => {
    const swing = makeShot({ mode: 'swing-speed', profile_id: 'p' });
    expect(render(swing)).not.toContain('kiosk-live');
  });

  it('shows the phone overview where the page is not the kiosk', () => {
    expect(render(shot, { kiosk: false })).not.toContain('kiosk-live');
  });
});

describe('the impact tile', () => {
  const hit = makeShot({ ...shot, club: '7-iron', impact_offset_mm: 11, impact_sigma_mm: 3, impact_zone: 'toe', impact_method: 'D' });

  it('shows the face over the zone word in a large and in a small slot, titled with ≈', () => {
    setActiveLocale('de');
    for (const [slots, index, size] of [
      [['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve'], 0, 'large'],
      [['carry', 'ball_speed', 'launch_v', 'launch_h', 'impact'], 4, 'small'],
    ] as const) {
      stores.slots = [...slots];
      const html = render(hit);
      expect(titles(html)[index], size).toBe('Treffpunkt ≈');
      expect(values(html)[index], size).toBe('Spitze');
      const tile = html.match(/<div class="kiosk-tile kiosk-tile--(large|small) kiosk-tile--impact" data-metric="impact">([\s\S]*?)<\/span><\/span><\/div>/);
      expect(tile?.[1], size).toBe(size);
      // Face first, then the word, both inside the tile's body.
      expect(tile?.[2]).toMatch(/<span class="kiosk-tile__body"><span class="kiosk-tile__face"><svg class="impact-face" [^>]*data-kind="iron" data-mark="dot"[\s\S]*<\/svg><\/span><span class="kiosk-tile__value-row"><span class="kiosk-tile__value">Spitze/);
      expect(html.match(/<svg class="impact-face"/g)).toHaveLength(1);
    }
  });

  it('draws the driver face, the open-zone ring, and no dot without a value', () => {
    setActiveLocale('en');
    stores.slots = ['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve'];
    const driver = render(makeShot({ ...hit, club: 'driver', impact_offset_mm: -3, impact_height_mm: 30, impact_zone: 'center' }));
    expect(driver).toMatch(/data-kind="driver" data-mark="dot"/);
    expect(values(driver)[0]).toBe('Center');
    const open = render(makeShot({ ...hit, impact_zone: null }));
    expect(open).toContain('data-mark="ring"');
    expect(values(open)[0]).toBe('uncertain');
    const none = render(makeShot({ ...hit, impact_offset_mm: null, impact_zone: null }));
    expect(none).toContain('data-mark="none"');
    expect(values(none)[0]).toBe('—');
    expect(titles(none)[0]).toBe('Impact ≈');
  });

  it('keeps millimetres off the tile', () => {
    stores.slots = ['impact', 'ball_speed', 'launch_v', 'launch_h', 'curve'];
    expect(render(hit)).not.toMatch(/mm|±/);
  });

  it('sizes the word by the tile height in rem and leaves the face the rest', () => {
    const css = readFileSync(fileURLToPath(new URL('./range.css', import.meta.url)), 'utf8');
    expect(css).toMatch(/\.kiosk-tile--impact \.kiosk-tile__body \{[^}]*flex-direction: column/);
    expect(css).toMatch(/\.kiosk-tile__face \{[^}]*flex: 1 1 0; min-height: 0/);
    expect(css).toMatch(/\.kiosk-tile--impact \.kiosk-tile__value-row \{[^}]*justify-content: center;[^}]*font-size: clamp\(1rem, \d+cqb, 14rem\)/);
  });
});

describe('the "No target" badge', () => {
  const camera = makeShot({ ...shot, launch_angle_horizontal_source: 'camera_assisted_experimental' });

  it('marks the start direction measured by the camera without a target line, and opens Align', () => {
    setActiveLocale('de');
    stores.captureSettings = { available: true, target_line: null };
    const html = render(camera, { onChangeView: () => {} });
    expect(html).toMatch(/<button type="button" class="kiosk-tile kiosk-tile--small kiosk-tile--launch_h kiosk-tile--action"[^>]*>/);
    expect(titles(html)[3]).toBe('Richtung Ziel fehlt');
    expect(html.match(/kiosk-tile__badge/g)).toHaveLength(1);
  });

  it.each([['en', 'No target'], ['es', 'Sin objetivo']] as const)('%s: %s', (locale, text) => {
    setActiveLocale(locale);
    stores.captureSettings = { available: true, target_line: null };
    expect(render(camera)).toContain(`<span class="kiosk-tile__badge">${text}</span>`);
  });

  it('stays away with a target, for a radar direction, and where no slot shows the direction', () => {
    stores.captureSettings = { available: true, target_line: target };
    expect(render(camera)).not.toContain('kiosk-tile__badge');
    stores.captureSettings = { available: true, target_line: null };
    expect(render()).not.toContain('kiosk-tile__badge');
    stores.slots = ['carry', 'ball_speed', 'launch_v', 'spin', 'curve'];
    expect(render(camera)).not.toContain('kiosk-tile__badge');
  });

  it('is a plain tile, not a button, without a way to open Align', () => {
    const html = renderToString(<KioskTileView tile={{ id: 'launch_h', label: 'Direction', value: '2° R', approx: false }} large={false} noTarget />);
    expect(html).toMatch(/^<div class="kiosk-tile/);
    expect(html).toContain('No target');
  });
});

describe('the Live display menu section', () => {
  it('maps the five slots, two large over three small, with a Default button', () => {
    setActiveLocale('de');
    stores.slots = ['club_speed', 'smash', 'spin', 'club_path', 'club_aoa'];
    const html = renderToString(<LiveSlotsSection />).replace(/<!-- -->/g, '');
    expect(html).toContain('>Live-Anzeige<');
    expect([...html.matchAll(/live-slots__slot--(large|small)" aria-pressed="false" aria-label="([^"]+)"/g)].map((m) => `${m[1]}|${m[2]}`)).toEqual([
      'large|Großes Feld 1: Schlägertempo',
      'large|Großes Feld 2: Smash',
      'small|Kleines Feld 1: Spin',
      'small|Kleines Feld 2: Schlägerpfad',
      'small|Kleines Feld 3: Eintreffwinkel',
    ]);
    expect(html).toContain('>Standard<');
    expect(html).not.toContain('live-slots__options');
  });

  it('gives its rows a 56 px touch height', () => {
    const css = readFileSync(fileURLToPath(new URL('../panel/LiveSlotsSection.css', import.meta.url)), 'utf8');
    expect(css).toMatch(/\.live-slots \.live-slots__map \.live-slots__slot,\s*\.live-slots \.live-slots__options \.live-slots__option \{[^}]*min-height: 56px/);
  });
});

describe('kiosk styles', () => {
  const css = readFileSync(fileURLToPath(new URL('./range.css', import.meta.url)), 'utf8');

  it('scopes the header and footer changes to the kiosk', () => {
    for (const rule of css.match(/^[^\n{]*(range-header--kiosk|range-footer)[^\n{]*\{[^}]*\.9167rem/gm) ?? []) {
      expect(rule).toContain('.range-app--kiosk');
    }
    expect(css).toMatch(/\.range-app--kiosk:has\(\.kiosk-live\) \.range-header--kiosk \{ display: none; \}/);
  });

  it('sizes the value by the tile height and keeps type in rem', () => {
    expect(css).toMatch(/\.kiosk-tile__value-row \{[^}]*font-size: clamp\(1rem, 100cqb, 14rem\)/);
    const kiosk = css.slice(css.indexOf('range-app--kiosk'));
    expect(kiosk).not.toMatch(/font-size:\s*\d+px/);
  });
});
