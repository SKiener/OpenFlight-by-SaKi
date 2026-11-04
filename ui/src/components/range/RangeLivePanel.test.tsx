import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, afterEach, vi } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY, unmeasurableClubPageCopy } from '../../test-utils/shot';
import type { Shot } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { RangeDetailsToolbar, RangeLivePanel } from './RangeLivePanel';
import { LivePanel } from '../panel/LivePanel';
import { SOFT_HYPHEN } from '../panel/labelBreaks';
import { buildLiveMetrics, hasCalculatedMetric } from '../panel/liveMetrics';
import { RangeFooter } from './RangeChrome';
import { ClubDataPanel } from './ClubDataPanel';

const preference = vi.hoisted(() => ({ unitSystem: 'imperial' as UnitSystem }));
vi.mock('../../state/useUnitPreference', () => ({ useUnitPreference: () => preference }));

const shot: Shot = {
  ball_speed_mph: 92, club_speed_mph: 68, smash_factor: 1.35,
  estimated_carry_yards: 150, carry_range: [145,155], club: '7iron',
  timestamp: '2026-10-01T10:00:00Z', peak_magnitude: 100,
  launch_angle_vertical: 19.2, launch_angle_horizontal: -1.2,
  launch_angle_confidence: .8, angle_source: 'radar', club_angle_deg: null,
  club_path_deg: null, spin_axis_deg: null, spin_rpm: 10000,
  spin_confidence: .9, spin_quality: 'high', spin_source: 'measured', spin_method: null,
  carry_spin_adjusted: 152, carry_spin_rpm: 10000, carry_spin_source: 'measured',
  profile_id: 'golfer', profile_name: 'Golfer',
};
// The range's 8-iron: OPS read 1,538 rpm at 39 %, so the carry flew the club's typical 7,500.
const typical: Shot = { ...shot, spin_rpm: 1538, spin_confidence: .39, spin_quality: 'low', spin_source: null,
  carry_spin_rpm: 7500, carry_spin_source: 'club_typical' };
// The phone's overview; the kiosk's own one has its tests in KioskLiveOverview.test.tsx.
const props = { shot, shots: [shot], profileId: 'golfer', profileName: 'Golfer', clubLabel: 'Eisen 7', kiosk: false };
const render = (s: Shot) => renderToString(<RangeLivePanel {...props} shot={s} shots={[s]} />);
const secondaryTiles = (html: string) =>
  [...(html.match(/class="range-secondary-grid[\s\S]*$/)?.[0] ?? '').matchAll(/data-metric="(\w+)"/g)].map((m) => m[1]);
afterEach(() => {
  setActiveLocale('en');
  preference.unitSystem = 'imperial';
});
describe('Range live overview', () => {
  it('shows real carry and ball speed as two heroes with an all-metrics control', () => {
    const html = renderToString(<RangeLivePanel {...props} />);
    expect(html.match(/data-metric=/g)).toHaveLength(5);
    expect(html).toContain('data-metric="carry"');
    expect(html).toContain('data-metric="ball_speed"');
    expect(html).toContain('>152<');
    expect(html).toContain('>92.0<');
    expect(html).toContain('All metrics');
    expect(html).toContain('>Total —<');
    expect(html).toContain('10,000');
    expect(html).not.toContain('Not available');
  });
  it('splits the small row into three tiles, never a spin axis', () => {
    const html = render(shot);
    expect(secondaryTiles(html)).toEqual(['launch_v', 'launch_h', 'spin']);
    expect(html).toContain('range-secondary-grid range-secondary-grid--of-3');
    expect(html).not.toContain('data-metric="spin_axis"');
    for (const axis of [Number.NaN, 3.4, -4, 0]) {
      expect(secondaryTiles(render({ ...shot, spin_axis_deg: axis }))).toEqual(['launch_v', 'launch_h', 'spin']);
    }
    // Not after the session's first calculated axis either (no sticky fourth tile).
    const first = { ...shot, ...UNMEASURABLE_CLUB_DATA, spin_axis_deg: 2.5, timestamp: '2026-10-01T09:59:00Z' };
    expect(secondaryTiles(renderToString(<RangeLivePanel {...props} shots={[first, shot]} />))).toEqual([
      'launch_v',
      'launch_h',
      'spin',
    ]);
  });
  it.each(['en', 'de', 'es'] as const)(
    '%s: the overview never shows the spin axis or any other value this hardware cannot measure',
    (locale) => {
      setActiveLocale(locale);
      const html = render({ ...shot, ...UNMEASURABLE_CLUB_DATA, spin_axis_deg: 3.4 }).replace(/<!-- -->/g, '');
      for (const word of [...UNMEASURABLE_COPY[locale], 'Fade', 'Neutral', 'Neutra']) {
        expect(html, word).not.toContain(word);
      }
    }
  );
  it('gives three tiles three equal columns on the kiosk and lets spin span the phone row', () => {
    const css = (name: string) => readFileSync(fileURLToPath(new URL(name, import.meta.url)), 'utf8');
    expect(css('./range.css')).toMatch(/\.range-secondary-grid \{ grid-template-columns: repeat\(3,minmax\(0,1fr\)\); \}/);
    const phone = css('../panel/phone.css');
    expect(phone).toMatch(/\.range-secondary-grid \{\s*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);/);
    expect(phone).toMatch(/\.range-secondary-grid--of-3 > :last-child \{\s*grid-column: 1 \/ -1;\s*\}/);
  });
  it.each([
    ['en', '>7,500<', 'typical', '1,538'],
    ['de', '>7.500<', 'typisch', '1.538'],
  ] as const)('shows the typical spin the carry used instead of an unreliable reading in %s', (locale, value, label, raw) => {
    setActiveLocale(locale);
    const html = render(typical);
    expect(html).toContain(value);
    expect(html).toContain(`>${label}<`);
    expect(html).not.toContain(raw);
  });
  it('shows the no-value dash, never the raw spin, for a shot without carry spin', () => {
    const older: Shot = { ...shot, spin_rpm: 6000, carry_spin_rpm: undefined, carry_spin_source: undefined };
    const html = render(older);
    expect(html).not.toContain('6,000');
    expect(html).toMatch(/data-metric="spin"[\s\S]*?>—<[\s\S]*?Not available/);
  });
  it('says Not available for a missing small value', () => {
    expect(render({ ...shot, launch_angle_horizontal: null })).toContain('Not available');
  });
  it('does not leak another profile’s latest measurement', () => {
    const other = { ...shot, profile_id: 'other', ball_speed_mph: 250 };
    const html = renderToString(<RangeLivePanel {...props} shot={other} shots={[shot,other]} />);
    expect(html).toContain('>92.0<');
    expect(html).not.toContain('>250.0<');
    expect(html).toContain('Shot\u00a01');
  });
  it('shows the spin the carry flew, marked typical, instead of an unconfirmed reading', () => {
    setActiveLocale('de');
    const diagnostic = { ...shot, spin_rpm: 4669, spin_quality: 'experimental' as const,
      spin_confidence: .59, spin_source: null, carry_spin_rpm: 7500, carry_spin_source: 'club_typical' as const,
      carry_model: 'ballistic' as const };
    const html = renderToString(<RangeLivePanel {...props} shot={diagnostic} shots={[diagnostic]} />);
    expect(html).toContain('Total — · mit typischem Spin');
    expect(html).toContain('>≈\u00a0152<');
    expect(html).toContain('>7.500<');
    expect(html).toContain('>typisch<');
    expect(html).not.toContain('4.669');
    expect(html).not.toContain('Unbestätigt');
    expect(html).not.toContain('Standardspin');
    expect(html).not.toContain('Spin gemessen');
  });
  it.each([
    ['de', 'Vorläufige Carry-Schätzung'],
    ['en', 'Provisional carry estimate'],
  ] as const)('labels initial carry as provisional in %s without changing the number', (locale, label) => {
    setActiveLocale(locale);
    const initial = { ...shot, carry_spin_adjusted: null, launch_angle_vertical: null,
      launch_angle_horizontal: null, camera_replay: null };
    const html = renderToString(<RangeLivePanel {...props} shot={initial} shots={[initial]} />);
    expect(html).toContain(label);
    expect(html).toContain('>150<');
    expect(html).not.toContain('>152<');
  });
  it('keeps completed zero carry final, rather than replacing it with a provisional estimate', () => {
    const final = { ...shot, carry_spin_adjusted: 0, carry_spin_source: 'measured' as const };
    const html = renderToString(<RangeLivePanel {...props} shot={final} shots={[final]} />);
    expect(html).toContain('>Total —<');
    expect(html).not.toContain('Provisional');
    expect(html).not.toContain('>150<');
    expect(html).toContain('>0<');
  });
  it.each(['speed_model', 'calculated'] as const)(
    'labels completed carry with %s spin as an estimate', (carry_spin_source) => {
      const final = { ...shot, carry_spin_source };
      const html = renderToString(<RangeLivePanel {...props} shot={final} shots={[final]} />);
      expect(html).toContain('Estimated · spin unconfirmed');
      expect(html).toContain('>152<');
      expect(html).not.toContain('Provisional');
    });
  it('marks a club_typical carry with ≈ and names the typical spin', () => {
    const final = { ...shot, carry_spin_source: 'club_typical' as const };
    const html = renderToString(<RangeLivePanel {...props} shot={final} shots={[final]} />);
    expect(html).toContain('>≈\u00a0152<');
    expect(html).toContain('>Total — · with typical spin<');
    expect(html).not.toContain('Estimated · spin unconfirmed');
    expect(html).not.toContain('Provisional');
  });
  it('shows no span on the hero tile or in its tooltip', () => {
    const final = { ...shot, carry_spin_source: 'club_typical' as const,
      carry_span_min_yards: 140.2, carry_span_max_yards: 158.6 };
    const html = renderToString(<RangeLivePanel {...props} shot={final} shots={[final]} />);
    const hero = html.match(/data-metric="carry"[\s\S]*?data-metric="ball_speed"/)![0];
    expect(hero).toContain('>Total — · with typical spin<');
    expect(hero).toContain('title="Estimated with typical spin · Curve not considered"');
    expect(html).not.toMatch(/140|159|50–100/);
  });
  it('shows no span in all metrics', () => {
    const final = { ...shot, carry_spin_source: 'club_typical' as const,
      carry_span_min_yards: 140.2, carry_span_max_yards: 158.6 };
    const html = renderToString(<LivePanel {...props} shot={final} shots={[final]} />);
    expect(html).toContain('>≈\u00a0152<');
    expect(html).toContain('>with typical spin<');
    expect(html).not.toMatch(/140|159|50–100/);
  });
  it('shows the Total under the carry, not under the ball speed', () => {
    const html = render({ ...shot, total_yards: 171 });
    expect(html).toMatch(/data-metric="carry"[\s\S]*?>Total 171 yds<[\s\S]*?data-metric="ball_speed"/);
    expect(html.match(/Total/g)).toHaveLength(1);
    expect(html).not.toContain('Total —');
  });
  it('keeps the spin note next to the Total', () => {
    expect(render({ ...typical, total_yards: 171 })).toContain('>Total 171 yds · with typical spin<');
  });
  it('leaves the Total out while the carry is provisional', () => {
    const html = render({ ...shot, carry_spin_adjusted: null, total_yards: 171 });
    expect(html).toContain('Provisional carry estimate');
    expect(html).not.toContain('Total');
  });
  it('gives the Total in the units of the tiles', () => {
    preference.unitSystem = 'metric';
    const html = render({ ...shot, total_yards: 171 });
    expect(html).toContain('>139<');
    expect(html).toContain('>Total 156 m<');
  });
  it('shows no made-up metrics when the active profile has no shots', () => {
    const html = renderToString(<RangeLivePanel {...props} profileId="nobody" />);
    expect(html).toContain('range-empty');
    expect(html).not.toContain('data-metric');
  });
  it('preserves the five-metric continuous swing mode', () => {
    const swing: Shot = { ...shot, mode: 'swing-speed', club: 'Swing Speed' };
    const html = renderToString(<RangeLivePanel {...props} shot={swing} shots={[swing]} />);
    expect(html).toContain('live-panel__grid--of-5');
    expect(html).not.toContain('range-hero-grid');
  });
  it('translates new copy and numbers into German', () => {
    setActiveLocale('de');
    const html = render({ ...shot, launch_angle_horizontal: null });
    // "Messwerte" carries a soft hyphen (labelBreaks.ts) for the narrowest phones.
    expect(html).toContain(`Alle Mess${SOFT_HYPHEN}werte`);
    expect(html).toContain('>Total —<');
    expect(html).toContain('Schlag\u00a01');
    expect(html).toContain('10.000');
    expect(html).toContain('Nicht verfügbar');
  });
  it('only enables replay with a capture and marks the history navigation state', () => {
    const base = { currentView: 'live' as const, onChangeView: () => {}, onOpenMenu: () => {}, menuOpen: false, onOpenHistory: () => {}, historyOpen: true, onReplay: () => {}, showRange: false };
    const off = renderToString(<RangeFooter {...base} hasReplay={false} />);
    const on = renderToString(<RangeFooter {...base} hasReplay />);
    expect(off).toMatch(/class="range-replay-button" disabled/);
    expect(on).not.toMatch(/class="range-replay-button" disabled/);
    expect(off).toMatch(/aria-pressed="true"[^>]*><svg[\s\S]*?<\/svg>History<\/button>/);
  });
});
describe('deleting the last shot from the Live view', () => {
  it('offers it next to all metrics, and disables it while the newest shot is processed or there is none', () => {
    setActiveLocale('de');
    const enabled = renderToString(<RangeLivePanel {...props} />);
    expect(enabled).toMatch(/<button type="button" class="confirm-delete range-delete-last">Letzten Schlag löschen<\/button><button class="range-details-button">/);
    const provisional = { ...shot, timestamp: '2026-10-01T10:01:00Z', carry_spin_adjusted: null };
    expect(renderToString(<RangeLivePanel {...props} shot={provisional} shots={[shot, provisional]} />)).toMatch(/class="confirm-delete range-delete-last" disabled="">/);
    expect(renderToString(<RangeLivePanel {...props} shot={null} shots={[]} />)).toMatch(/class="confirm-delete range-delete-last" disabled="">/);
  });
});

describe('All metrics toolbar (Ball | Club)', () => {
  const toolbar = (page: 'ball' | 'club') =>
    renderToString(<RangeDetailsToolbar page={page} onPage={() => {}} onBack={() => {}} profileName="Golfer" />);

  it('offers the page switch, the ball page naming the profile', () => {
    const html = toolbar('ball');
    expect(html).toContain('aria-label="Metrics page"');
    expect(html).toMatch(/segmented-control__button segmented-control__button--active"[^>]*>Ball</);
    expect(html).toContain('>Club<');
    expect(html).toContain('>Golfer<');
    expect(html).not.toContain('range-details-legend');
  });

  it('names the ≈ mark on the club page instead of the profile', () => {
    setActiveLocale('de');
    const html = toolbar('club').replace(/<!-- -->/g, '');
    expect(html).toContain('<span class="range-details-legend"><span aria-hidden="true">≈</span> berechnet</span>');
    expect(html).toMatch(/segmented-control__button--active"[^>]*>Schläger</);
    expect(html).not.toContain('>Golfer<');
  });

  it('names the ≈ mark on a ball page with a calculated tile too', () => {
    setActiveLocale('de');
    const html = renderToString(
      <RangeDetailsToolbar page="ball" onPage={() => {}} onBack={() => {}} profileName="Golfer" calculated />
    ).replace(/<!-- -->/g, '');
    expect(html).toContain('<span class="range-details-legend"><span aria-hidden="true">≈</span> berechnet</span>');
    expect(html).not.toContain('>Golfer<');
  });

  it.each([
    ['de', 'berechnet bzw. mit typischem Spin'],
    ['en', 'calculated or with typical spin'],
    ['es', 'calculado o con spin típico'],
  ] as const)('%s: next to a typical-spin carry the ball-page legend names both meanings of ≈', (locale, both) => {
    setActiveLocale(locale);
    const legend = (page: 'ball' | 'club', calculated: boolean, typicalSpin: boolean) =>
      renderToString(
        <RangeDetailsToolbar page={page} onPage={() => {}} onBack={() => {}} profileName="Golfer" calculated={calculated} typicalSpin={typicalSpin} />
      )
        .replace(/<!-- -->/g, '')
        .match(/<span class="range-details-legend[^"]*"><span aria-hidden="true">≈<\/span> ([^<]*)<\/span>/)?.[1] ?? null;
    const calculated = { de: 'berechnet', en: 'calculated', es: 'calculado' }[locale];
    expect(legend('ball', true, true)).toBe(both);
    expect(legend('ball', true, false)).toBe(calculated);
    // The club page shows no carry: only "calculated".
    expect(legend('club', false, true)).toBe(calculated);
    // Without a calculated tile the ball page keeps the profile (the carry names its typical spin itself).
    expect(legend('ball', false, true)).toBeNull();
  });

  it.each([
    ['a camera club path', { club_path_deg: null, camera_club_path_deg: -16.9, camera_club_path_sigma_deg: 1.9 }, true],
    ['a camera attack angle', { club_angle_deg: null, camera_attack_angle_deg: -4.1 }, true],
    // Measured, only not calibrated: no "≈ calculated" legend on a radar-only shot.
    ['a radar club path and attack angle', { club_path_deg: 2.6, club_angle_deg: -4.3 }, false],
    ['an accepted experimental club path', { experimental_club_path_deg: 1.2, experimental_club_path_status: 'accepted' }, false],
    // Calculated, but never shown: they no longer make the ball page name the ≈ mark.
    ['a camera spin-axis class', { spin_axis_class: 'straight' }, false],
    ['a K-LD7 calculated axis', { spin_axis_deg: 3.4, spin_axis_source: 'calculated' }, false],
    ['only estimated values (carry, typical spin)', { carry_spin_adjusted: null }, false],
  ] as const)('ball page legend with %s: %s', (_name, overrides, expected) => {
    const metrics = buildLiveMetrics({ ...shot, ...overrides } as Shot, 'metric', { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 });
    expect(hasCalculatedMetric(metrics)).toBe(expected);
  });

  it('keeps the back button\'s accessible name while phones show only its arrow', () => {
    setActiveLocale('de');
    const html = toolbar('ball').replace(/<!-- -->/g, '');
    expect(html).toMatch(/<button class="range-back"><span aria-hidden="true">←<\/span> <span class="range-back__text">Übersicht<\/span><\/button>/);
    const phone = readFileSync(fileURLToPath(new URL('../panel/phone.css', import.meta.url)), 'utf8');
    // Visually hidden, not display: none, so screen readers still read it.
    expect(phone).toMatch(/\.range-back__text \{[^}]*clip: rect\(0, 0, 0, 0\)/);
    expect(phone).not.toMatch(/\.range-back__text \{[^}]*display: none/);
  });

  it('has no switch in a swing-speed session', () => {
    const swing: Shot = { ...shot, mode: 'swing-speed', club: 'Swing Speed' };
    const html = renderToString(<RangeLivePanel {...props} shot={swing} shots={[swing]} />);
    expect(html).not.toContain('Metrics page');
    expect(html).not.toContain('range-details-button');
  });
});

describe('All metrics club page (ClubDataPanel)', () => {
  const iron: Shot = {
    ...shot,
    ...UNMEASURABLE_CLUB_DATA,
    club: '8-iron',
    camera_club_path_deg: 6.9,
    camera_club_path_sigma_deg: 1.2,
    camera_attack_angle_deg: -10.4,
    camera_attack_angle_sigma_deg: 0.8,
  } as Shot;

  it('shows club speed, path, attack angle, the impact point and the low point in a five-card grid', () => {
    const html = renderToString(<ClubDataPanel shots={[iron]} profileId="golfer" />);
    expect(html).toContain('live-panel__grid live-panel__grid--of-5 club-data__grid');
    expect([...html.matchAll(/data-metric="(\w+)"/g)].map((m) => m[1])).toEqual([
      'club_speed',
      'cd_path',
      'cd_aoa',
      'cd_impact',
      'cd_low_point',
    ]);
    const css = (name: string) => readFileSync(fileURLToPath(new URL(name, import.meta.url)), 'utf8');
    // Five in a row on landscape phones (as swing speed), 3 + 2 on the kiosk, two by three on a portrait phone.
    expect(css('../panel/panel.css')).toMatch(/\n\.live-panel__grid--of-5 \{[^}]*grid-template-rows: 1fr/);
    expect(css('../panel/panel.css')).toMatch(
      /\.club-data__grid\.live-panel__grid--of-5 \{[^}]*grid-template-columns: repeat\(3, 1fr\);[^}]*grid-template-rows: repeat\(2, 1fr\)/
    );
    expect(css('../panel/phone.css')).toMatch(
      /\.club-data__grid\.live-panel__grid--of-5 \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);[^}]*grid-template-rows: repeat\(3, minmax\(4\.75rem, 1fr\)\)/
    );
    // The kiosk: the impact card takes row 2's free cell (no filler left), its face left and word right.
    const panel = css('../panel/panel.css');
    const kioskStart = panel.indexOf(
      '@media (orientation: landscape) and (min-width: 800px) and (min-height: 400px) and (max-aspect-ratio: 21/10) {'
    );
    expect(kioskStart).toBeGreaterThan(-1);
    const kiosk = panel.slice(kioskStart, panel.indexOf('\n}\n', kioskStart));
    expect(kiosk).toMatch(/\.club-data__grid \.metric-card\[data-metric='cd_impact'\] \{\s*grid-column: span 2;/);
    expect(panel).not.toContain('.club-data__grid.live-panel__grid--of-5::after');
    expect(kiosk).toMatch(
      /\.club-data__grid \.metric-card\.metric-card--label-above\.metric-card--graphic \{[^}]*display: grid;[^}]*grid-template-columns: minmax\(0, 11fr\) minmax\(0, 9fr\);/
    );
    expect(kiosk).toMatch(/\.club-data__grid \.metric-card--graphic \.metric-card__label \{[^}]*grid-column: 1 \/ -1;/);
    expect(kiosk).toMatch(
      /\.club-data__grid \.metric-card--graphic \.metric-card__graphic \{[^}]*grid-column: 1;[^}]*grid-row: 2 \/ -1;[^}]*height: auto;/
    );
    expect(kiosk).toMatch(/\.club-data__grid \.metric-card--graphic \.metric-card__value-row \{[^}]*grid-column: 2;/);
    expect(kiosk).toMatch(/\.club-data__grid \.metric-card--graphic \.metric-card__meta \{[^}]*grid-column: 2;/);
    // Phones keep the stacked card: none of this outside the kiosk query.
    expect(panel.replace(kiosk, '')).not.toContain("[data-metric='cd_impact'] {");
    for (const phone of ['../panel/phone.css', '../panel/phoneLandscape.css']) {
      expect(css(phone), phone).not.toContain('metric-card--graphic');
    }
  });

  it.each([
    // The unit preference defaults to yards here: 12.3 cm ± 2.1 = 4.8 in ± 0.8.
    ['de', 'Tiefpunkt', '4,8', 'in', 'vor dem Ball · ±0,8 in · berechnet · unkalibriert'],
    ['en', 'Low point', '4.8', 'in', 'ahead of the ball · ±0.8 in · calculated · uncalibrated'],
    ['es', 'Punto bajo', '4,8', 'in', 'delante de la bola · ±0,8 in · calculado · sin calibrar'],
  ] as const)('%s: the 5th card shows the low point, its side, ±σ and ≈', (locale, label, value, unit, caption) => {
    setActiveLocale(locale);
    const page = renderToString(<ClubDataPanel shots={[iron]} profileId="golfer" />).replace(/<!-- -->/g, '');
    const html = page.slice(page.indexOf('data-metric="cd_low_point"'));
    expect(html).toMatch(new RegExp(`^data-metric="cd_low_point"><span class="metric-card__label">${label}<span class="metric-card__estimated"`));
    expect(html).toContain(`<span class="metric-card__value">${value}</span><span class="metric-card__unit">${unit}</span>`);
    expect(html).toContain(caption);
  });

  it('reads "—" in the 5th card for a driver', () => {
    const driver = { ...iron, club: 'driver', camera_low_point_cm: null, camera_low_point_sigma_cm: null } as Shot;
    const page = renderToString(<ClubDataPanel shots={[driver]} profileId="golfer" />).replace(/<!-- -->/g, '');
    const html = page.slice(page.indexOf('data-metric="cd_low_point"'));
    expect(html).toContain('<span class="metric-card__value">—</span>');
    expect(html).not.toContain('metric-card__estimated');
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows a loft, the face, face to path or the spin axis',
    (locale) => {
      setActiveLocale(locale);
      const page = renderToString(<ClubDataPanel shots={[iron]} profileId="golfer" />).replace(/<!-- -->/g, '');
      // The low point has its own card (above); no other card names it or its centimetres.
      const html = page.slice(0, page.indexOf('data-metric="cd_low_point"'));
      for (const word of unmeasurableClubPageCopy(locale)) expect(html, word).not.toContain(word);
      // The impact graphic's path coordinates are drawing, not copy (it holds no text): left out of the number check.
      expect(html.replace(/<svg class="impact-face[\s\S]*?<\/svg>/g, '')).not.toMatch(/37[.,]7|52[.,]7|12[.,]2|19[.,]1|12[.,]3|>cm<|mm/);
      // impact_toe_mm (the old name) draws no dot.
      expect(html).toContain('data-mark="none"');
    }
  );

  const card = (html: string) =>
    html.slice(html.indexOf('data-metric="cd_impact"'), html.indexOf('data-metric="cd_low_point"'));

  it.each([
    ['de', 'toe', 9, 'Spitze'],
    ['de', 'heel', -11, 'Ferse'],
    ['en', 'center', 2, 'Center'],
    ['es', null, 4, 'incierto'],
  ] as const)('%s: the 4th card draws the face and names the zone %s', (locale, zone, offset, word) => {
    setActiveLocale(locale);
    const hit = { ...iron, impact_offset_mm: offset, impact_sigma_mm: 3, impact_zone: zone, impact_method: 'D' } as Shot;
    const page = renderToString(<ClubDataPanel shots={[hit]} profileId="golfer" />).replace(/<!-- -->/g, '');
    expect(page).toContain('metric-card--class metric-card--graphic" data-metric="cd_impact"');
    const html = card(page);
    // The title and its ≈ "calculated" mark.
    expect(html).toMatch(/^data-metric="cd_impact"><span class="metric-card__label">(Treffpunkt|Impact|Impacto)<span class="metric-card__estimated"/);
    expect(html).toMatch(/<div class="metric-card__graphic"><svg class="impact-face" [^>]*data-kind="iron" data-mark="(dot|ring)"/);
    expect(html).toContain(`<span class="metric-card__value">${word}</span>`);
    expect(html).not.toMatch(/mm|±/);
  });

  it('reads "calculating" under an empty face while the club data is pending', () => {
    setActiveLocale('de');
    const pending = { ...iron, camera_club_path_deg: null, camera_attack_angle_deg: null, club_data_status: 'pending' } as Shot;
    const html = card(renderToString(<ClubDataPanel shots={[pending]} profileId="golfer" />).replace(/<!-- -->/g, ''));
    expect(html).toContain('data-mark="none"');
    expect(html).toContain('<span class="metric-card__value">—</span>');
    expect(html).toContain('wird berechnet');
    expect(html).not.toContain('metric-card__estimated');
  });
});
