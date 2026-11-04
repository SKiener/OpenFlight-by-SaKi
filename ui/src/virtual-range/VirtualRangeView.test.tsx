import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../i18n';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { makeShot } from '../test-utils/shot';
import type { UnitSystem } from '../utils/units';
import { AIMS_KEY, TARGET_KEY } from './aim';
import { TRAINING_KEY } from './game/training';
import { VirtualRangeView, type RangeSource, type VirtualRangeViewProps } from './VirtualRangeView';

// zustand renders the initial state on the server, so the unit is set through the hook.
const preference = vi.hoisted(() => ({ unitSystem: 'imperial' as UnitSystem }));
vi.mock('../state/useUnitPreference', () => ({ useUnitPreference: () => preference }));

const css = readFileSync(fileURLToPath(new URL('./virtualRange.css', import.meta.url)), 'utf8');

const text = (html: string): string => html.replace(/<!-- -->/g, '');

const eightIron = makeShot({
  timestamp: 'iron',
  profile_id: 'p1',
  club: '8-iron',
  ball_speed_mph: 102,
  carry_spin_adjusted: 133,
});
const driver = makeShot({
  timestamp: 'driver',
  profile_id: 'p1',
  club: 'driver',
  ball_speed_mph: 150,
  carry_spin_adjusted: 255,
});

const rolled = makeShot({
  timestamp: 'rolled',
  profile_id: 'p1',
  club: 'driver',
  ball_speed_mph: 150,
  carry_spin_adjusted: 240,
  flight_path: {
    unit: 'yd',
    points: [
      [0, 0, 0, 0],
      [3, 120, 0, 30],
      [6, 240, 0, 0],
    ],
  },
  roll_path: {
    unit: 'yd',
    points: [
      [6, 240, 0, 0],
      [9, 260, 2, 0],
    ],
  },
  total_yards: 260,
});

// The OPS numbers of a hardware shot: its carry stays provisional until the camera capture is processed.
const provisional = makeShot({
  timestamp: 'provisional',
  profile_id: 'p1',
  club: 'driver',
  ball_speed_mph: 141.3,
  estimated_carry_yards: 266,
  carry_spin_adjusted: null,
});
const processing = { processingPhase: 'camera_processing' } as const;

function render(overrides: Partial<VirtualRangeViewProps> = {}): string {
  return text(
    renderToString(
      <VirtualRangeView
        shots={[]}
        club="driver"
        profileId="p1"
        sessionGeneration={0}
        source={{ kind: 'live' }}
        processingPhase={null}
        demoActive={false}
        sessionDemo={null}
        onBackToLive={() => {}}
        {...overrides}
      />
    )
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  setActiveLocale('en');
  preference.unitSystem = 'imperial';
});

describe('VirtualRangeView', () => {
  it('renders the loading state and the bar with the latest shot', () => {
    const html = render({ shots: [eightIron, driver] });

    expect(html).toContain('Loading the range…');
    expect(html).toContain('>Driver<');
    expect(html).toContain('>255<');
    expect(html).toContain('>150.0<');
    expect(html).toContain('>yds<');
    expect(html).toContain('>mph<');
    expect(html).not.toContain('8 Iron');
    expect(html).not.toContain('Hit a shot');
  });

  it('shows club, carry, Total, the distance to the target and ball speed, in that order, at rest', () => {
    const html = render({ shots: [eightIron, rolled] });

    expect(html).toMatch(
      /virtual-range__club">Driver<.*--carry.*>Carry<.*>240<.*--total.*>Total<.*>260<.*--target.*>To target<.*>12<.*--speed.*>Ball speed<.*>150\.0</
    );
  });

  it('shows no Total for a shot whose roll the range does not draw, but the distance to the nearest flag', () => {
    const html = render({ shots: [eightIron] });

    expect(html).toMatch(/--total.*>Total<.*virtual-range__value">—</);
    expect(html).toMatch(/--target.*>To target<.*virtual-range__value">11<small>yds</);
  });

  it("shows only the active profile's shots on the live range", () => {
    const other = makeShot({ timestamp: 'other', profile_id: 'p2', club: '7-iron' });

    const html = render({ shots: [eightIron, other] });

    expect(html).toContain('>8 Iron<');
    expect(html).not.toContain('7 Iron');
  });

  it('names a saved session and offers the way back', () => {
    const source: RangeSource = { kind: 'saved', label: 'Oct 7, 2026, 6:12 PM', shots: [eightIron] };

    const html = render({ source, shots: [driver] });

    expect(html).toContain('Saved session · Oct 7, 2026, 6:12 PM');
    expect(html).toMatch(/<button[^>]*>Back to today<\/button>/);
    expect(html).toContain('>8 Iron<');
    expect(html).not.toContain('>Driver<');
    expect(render()).not.toContain('Back to today');
    expect(render()).not.toContain('Saved session');
  });

  it('offers to delete the shown shot on the live feed only, not on a saved session or the demo', () => {
    const deleteButton =
      /<button type="button" class="confirm-delete virtual-range__delete" aria-label="Delete last shot">Del<\/button>/;

    expect(render({ shots: [eightIron, driver] })).toMatch(deleteButton);
    const source: RangeSource = { kind: 'saved', label: 'x', shots: [eightIron] };
    expect(render({ source, shots: [driver] })).not.toContain('Delete last shot');
    vi.stubGlobal('window', { location: { search: '?rangeDemo=1' } });
    expect(render({ shots: [driver] })).not.toContain('Delete last shot');
  });

  it('leaves the host of the 3D canvas empty, with the bars beside it', () => {
    const html = render({ shots: [eightIron] });

    expect(html).toContain('<div class="virtual-range__host"></div>');
    expect(html).toContain('<div class="virtual-range__bar">');
  });

  it("shows the Live view's processing state, not the provisional numbers, while the newest shot is processed", () => {
    const html = render({ shots: [eightIron, provisional], ...processing });

    expect(html).toContain('OPS metrics ready');
    expect(html).toContain('Processing camera capture…');
    expect(html).not.toContain('virtual-range__stat');
    expect(html).not.toContain('>266<');
    expect(html).not.toContain('8 Iron');
    expect(html).not.toContain('Hit a shot');
  });

  it('shows the processing state for a provisional newest shot the store has no phase for, as after a reload', () => {
    const html = render({ shots: [eightIron, provisional] });

    expect(html).toContain('OPS metrics ready');
    expect(html).not.toContain('Processing camera capture…');
    expect(html).not.toContain('virtual-range__stat');
    expect(html).not.toContain('>266<');
  });

  it('holds nothing back in a saved session', () => {
    const html = render({ source: { kind: 'saved', label: 'Oct 7', shots: [provisional] }, ...processing });

    expect(html).toContain('>266<');
    expect(html).not.toContain('OPS metrics ready');
  });

  it('shows the empty hint without shots', () => {
    const html = render();

    expect(html).toContain('<p class="virtual-range__hint">Hit a shot – it flies here.</p>');
    expect(html).not.toContain('virtual-range__bar');
  });

  it('shows the empty hint when the profile only has swing-speed reps', () => {
    const swing = makeShot({ timestamp: 'swing', profile_id: 'p1', mode: 'swing-speed' });

    expect(render({ shots: [swing] })).toContain('Hit a shot');
  });

  it('shows metres and km/h in the metric units', () => {
    preference.unitSystem = 'metric';
    const metric = makeShot({ timestamp: 'm', profile_id: 'p1', carry_spin_adjusted: 100, ball_speed_mph: 100 });

    const html = render({ shots: [metric] });

    expect(html).toContain('>91<');
    expect(html).toContain('>160.9<');
    expect(html).toContain('>m<');
    expect(html).toContain('>km/h<');
  });

  it('shows the demo shots instead of the live ones with ?rangeDemo=1', () => {
    vi.stubGlobal('window', { location: { search: '?rangeDemo=1' } });

    const html = render({ shots: [driver] });

    expect(html).toContain('>Wedge 54°<');
    expect(html).toContain('>70.0<');
    expect(html).not.toContain('>Driver<');
  });

  it('ignores a selected green and stored aims with ?rangeDemo=1, so every measurement starts alike', () => {
    const demoWith = (session: Record<string, string>) => {
      vi.stubGlobal('window', {
        location: { search: '?rangeDemo=1' },
        sessionStorage: fakeStorage(session).localStorage,
      });
      return render();
    };

    const plain = demoWith({});

    expect(plain).toMatch(/--target.*>To target<.*virtual-range__value">\d+<small>yds</);
    expect(demoWith({ [TARGET_KEY]: '4' })).toBe(plain);
    expect(demoWith({ [AIMS_KEY]: JSON.stringify([['demo-wedge', 0.4]]) })).toBe(plain);
  });

  it('speaks German', () => {
    setActiveLocale('de');
    const source: RangeSource = { kind: 'saved', label: '07.10.2026, 18:12', shots: [eightIron] };

    const saved = render({ source });
    const loading = render({ shots: [eightIron] });
    const empty = render();
    const held = render({ shots: [provisional], ...processing });

    expect(saved).toContain('Gespeicherte Session · 07.10.2026, 18:12');
    expect(saved).toContain('Zurück zu heute');
    expect(saved).toContain('>Eisen 8<');
    expect(saved).toContain('>102,0<');
    expect(loading).toContain('Range lädt …');
    expect(empty).toContain('Schlag einen Ball – er fliegt hier.');
    expect(held).toContain('OPS-Messwerte bereit');
    expect(held).toContain('Kameraaufnahme wird ausgewertet…');
    expect(loading).toMatch(/>Flugweite<.*>Total<.*>Zum Ziel<.*>Balltempo</);
  });
});

describe('VirtualRangeView in training', () => {
  const storeTraining = (byPlayer: object, session: Record<string, string> = {}) =>
    vi.stubGlobal('window', {
      location: { search: '' },
      localStorage: fakeStorage({ [TRAINING_KEY]: JSON.stringify(byPlayer) }).localStorage,
      sessionStorage: fakeStorage(session).localStorage,
    });
  const trained = (state: object, session: Record<string, string> = {}) => storeTraining({ p1: state }, session);
  const twoShots = {
    targetM: 137,
    recent: [
      { deviationM: 2, zone: 'volltreffer' },
      { deviationM: -4.04, zone: 'gut' },
    ],
  };

  it('offers training on the live range only, not on a saved session or the demo', () => {
    expect(render({ shots: [eightIron] })).toMatch(
      /<div class="virtual-range__top virtual-range__top--live"><button type="button" class="virtual-range__mode"[^>]*>Training<\/button>/
    );
    expect(render({ source: { kind: 'saved', label: 'Oct 7', shots: [eightIron] } })).not.toContain('>Training<');
    vi.stubGlobal('window', { location: { search: '?rangeDemo=1' } });
    expect(render()).not.toContain('>Training<');
  });

  it('shows the target, the last result and the running figures, with the steps and the way out', () => {
    trained(twoShots);
    preference.unitSystem = 'metric';

    const html = render({ shots: [eightIron] });

    expect(html).not.toContain('virtual-range__mode');
    expect(html).toContain('<div class="virtual-range__training">');
    expect(html).toMatch(/virtual-range__training-value">137<small>m<\/small>/);
    expect(html).toContain('4.0 m short · Good');
    expect(html).toContain('Avg 1.0 m short · 100% inside');
    for (const [step, label] of [
      ['−10', 'Target 10 m closer'],
      ['−', 'Target 1 m closer'],
      ['+', 'Target 1 m farther'],
      ['+10', 'Target 10 m farther'],
    ])
      expect(html).toContain(`aria-label="${label}">${step}</button>`);
    expect(html).toMatch(/<button type="button" class="virtual-range__training-end">End<\/button>/);
  });

  it("shows the active player's training: another player sees theirs, or none", () => {
    preference.unitSystem = 'metric';
    storeTraining({ p1: twoShots, p2: { targetM: 92, recent: [{ deviationM: 12, zone: 'outside' }] } });

    const anna = render({ profileId: 'p1', shots: [eightIron] });
    const ben = render({ profileId: 'p2' });
    const cleo = render({ profileId: 'p3' });

    expect(anna).toMatch(/virtual-range__training-value">137<small>m<\/small>/);
    expect(anna).toContain('4.0 m short · Good');
    expect(ben).toMatch(/virtual-range__training-value">92<small>m<\/small>/);
    expect(ben).toContain('12.0 m long · Miss');
    expect(ben).not.toContain('Good');
    expect(cleo).not.toContain('virtual-range__training');
    expect(cleo).toMatch(/<button type="button" class="virtual-range__mode"[^>]*>Training<\/button>/);
  });

  it('reads training stored in the earlier single-player format as none', () => {
    storeTraining(twoShots);

    const html = render({ shots: [eightIron] });

    expect(html).not.toContain('virtual-range__training');
    expect(html).toContain('>Training<');
  });

  it("keeps the player's training going while the server demo runs", () => {
    trained(twoShots);

    for (const demo of [
      { demoActive: true, sessionDemo: null },
      { demoActive: false, sessionDemo: true },
    ]) {
      const html = render({ shots: [eightIron], ...demo });
      expect(html).toContain('<div class="virtual-range__training">');
      expect(html).toContain('4.4 yds short · Good');
    }
  });

  it('reads a shot or a mean right on the target without short or long', () => {
    preference.unitSystem = 'metric';
    trained({ targetM: 100, recent: [{ deviationM: 0, zone: 'volltreffer' }] });

    expect(render()).toContain('<strong>0.0 m · Bullseye</strong>');
    expect(render()).toContain('Avg 0.0 m · 100% inside');
    trained({ targetM: 100, recent: [{ deviationM: 0.03, zone: 'volltreffer' }] });
    expect(render()).toContain('<strong>0.0 m · Bullseye</strong>');
    expect(render()).toContain('Avg 0.0 m · 100% inside');
    trained({
      targetM: 100,
      recent: [
        { deviationM: -2, zone: 'volltreffer' },
        { deviationM: 2, zone: 'volltreffer' },
      ],
    });
    expect(render()).toContain('Avg 0.0 m · 100% inside');
    setActiveLocale('de');
    expect(render()).toMatch(/Ø 0,0 m · 100\s%\sgetroffen</);
    trained({ targetM: 100, recent: [{ deviationM: 0, zone: 'volltreffer' }] });
    expect(render()).toContain('<strong>0,0 m · Volltreffer</strong>');
  });

  it('shows the target in the profile unit while it stays in metres', () => {
    trained(twoShots);

    expect(render()).toMatch(/virtual-range__training-value">150<small>yds<\/small>/);
  });

  it('asks for the first shot before any counted, and counts none of the shots already there', () => {
    trained({ targetM: 240, recent: [] });

    const html = render({ shots: [eightIron, rolled] });

    expect(html).toContain('Hit at the target');
    expect(html).not.toContain('short ·');
    expect(html).not.toContain('Avg');
  });

  it('measures to the training target straight ahead, not to a selected green, and leaves a saved session alone', () => {
    trained({ targetM: 240, recent: [] }, { [TARGET_KEY]: '4' });

    expect(render({ shots: [eightIron, rolled] })).toMatch(/--target.*>To target<.*virtual-range__value">3<small>yds</);
    const saved = render({ source: { kind: 'saved', label: 'Oct 7', shots: [eightIron] } });
    expect(saved).not.toContain('virtual-range__training');
    expect(saved).toContain('Saved session · Oct 7');
  });

  it('speaks German', () => {
    setActiveLocale('de');
    preference.unitSystem = 'metric';
    trained({ ...twoShots, recent: [...twoShots.recent, { deviationM: 30, zone: 'outside' }] });

    const html = render({ shots: [eightIron] });

    expect(html).toContain('30,0 m lang · Daneben');
    expect(html).toContain('Ø 9,3 m lang · 67 % getroffen');
    expect(html).toContain('>Beenden<');
    expect(html).toContain('aria-label="Ziel 10 m näher"');
    trained({ targetM: 100, recent: [{ deviationM: -1, zone: 'volltreffer' }] });
    expect(render()).toContain('1,0 m kurz · Volltreffer');
    trained({ targetM: 100, recent: [{ deviationM: 9, zone: 'getroffen' }] });
    expect(render()).toContain('9,0 m lang · Getroffen');
    trained({ targetM: 100, recent: [] });
    expect(render()).toContain('Schlag aufs Ziel');
    vi.unstubAllGlobals();
    expect(render({ shots: [eightIron] })).toContain('>Training<');
  });
});

describe('virtualRange.css', () => {
  it('keeps every overlay semi-transparent and free of backdrop blur', () => {
    expect(css).not.toContain('backdrop-filter');
    for (const overlay of ['bar', 'hint', 'notice', 'session', 'training'])
      expect(css, overlay).toMatch(new RegExp(`\\.virtual-range__${overlay}[^{]*\\{[^}]*background: var\\(--virtual-range-panel\\)`));
    // Dark: see-through over the scene; sun: near-opaque white, so black text never sits on the scene.
    expect(css).toMatch(/\.virtual-range \{[^}]*--virtual-range-panel: rgba\(8, 14, 10, 0\.66\)/);
    expect(css).toMatch(/\[data-theme='sun'\] \.virtual-range \{[^}]*--virtual-range-panel: rgba\(255, 255, 255, 0\.94\)/);
    expect(css).toMatch(/\[data-theme='sun'\] \.virtual-range \{[^}]*--virtual-range-text: #000/);
  });

  it('sets carry and Total large, and lets ball speed leave the bar first where it is short of room', () => {
    expect(css).toMatch(
      /\.virtual-range__stat--target \.virtual-range__value,\s*\.virtual-range__stat--speed \.virtual-range__value \{[^}]*font-size: [\d.]+rem/
    );
    expect(css).toMatch(/@media [^{]*\{[^@]*\.virtual-range__stat--speed \{\s*display: none;/);
  });

  it('sizes type in rem so it follows the kiosk screen', () => {
    expect(css).not.toMatch(/font-size:\s*[\d.]+px/);
    expect(css).toMatch(/font-size:\s*[\d.]+rem/);
  });

  it("keeps the hint in the bar's place above the footer, off the greens, and the notice mid-way between the bars", () => {
    expect(css).toMatch(
      /\.virtual-range__bar,\s*\.virtual-range__hint \{[^}]*bottom: calc\(var\(--virtual-range-inset-bottom, 0px\) \+ var\(--virtual-range-edge\)\);/
    );
    expect(css).toMatch(
      /\.virtual-range__notice \{[^}]*top: calc\(50% \+ \(var\(--virtual-range-inset-top, 0px\) - var\(--virtual-range-inset-bottom, 0px\)\) \/ 2\);/
    );
  });
});
