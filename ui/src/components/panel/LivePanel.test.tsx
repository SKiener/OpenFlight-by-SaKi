import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../../test-utils/shot';
import type { Shot } from '../../types/shot';
import { LivePanel } from './LivePanel';

/** React SSR splits interpolated text with comment markers; drop them. */
function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 92,
    club_speed_mph: 68,
    smash_factor: 1.35,
    estimated_carry_yards: 210,
    carry_range: [205, 215],
    club: 'driver',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13.4,
    launch_angle_horizontal: -1.2,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: 2.1,
    club_path_deg: -0.6,
    spin_axis_deg: 3.4,
    spin_rpm: 2650,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: 214,
    carry_spin_rpm: 2650,
    carry_spin_source: 'measured',
    profile_id: 'james',
    profile_name: 'James',
    ...overrides,
  };
}

function render(
  shot: Shot | null,
  shots: Shot[] = shot ? [shot] : [],
  selectedMetricId: string | null = null,
  isNewShot = false
) {
  return text(
    renderToString(
      <LivePanel
        shot={shot}
        shots={shots}
        profileId="james"
        profileName="James"
        clubLabel="DR"
        selectedMetricId={selectedMetricId}
        onSelectMetric={() => {}}
        isNewShot={isNewShot}
      />
    )
  );
}

function tileLabels(html: string): string[] {
  return [...html.matchAll(/metric-card__label[^>]*>([^<]+)/g)].map((match) => match[1]);
}

describe('LivePanel', () => {
  afterEach(() => setActiveLocale('en'));

  it('sizes live metric numbers in rem that follow the screen', () => {
    const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');
    const rootCss = readFileSync(fileURLToPath(new URL('../../index.css', import.meta.url)), 'utf8');

    expect(rootCss).toMatch(/html \{[^}]*font-size: clamp\([^)]*vw[^)]*150%/);
    expect(css).toMatch(
      /\.live-panel__grid \.metric-card__value-row \{[^}]*font-size: clamp\([\d.]+rem, min\([^)]*vw[^)]*vh[^)]*cqi[^)]*cqb\), [\d.]+rem\)/
    );
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value \{[^}]*font-size: 1em/);
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value \{[^}]*flex-shrink: 0/);
    // Units scale with the number but never drop below the 12 px (0.75rem) caption floor of a tight 5 × 2 tile.
    expect(css).toMatch(/\.live-panel__grid \.metric-card__unit \{[^}]*font-size: max\(0\.75rem, 0\.28em\)/);
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value \{[^}]*font-weight: 800/);
    expect(rootCss).toContain("'Liberation Sans'");
    expect(rootCss).not.toContain('fonts.googleapis.com');
    // a•space digits have wide side bearings: slightly tight tracking and a slim gap before the unit.
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value \{[^}]*letter-spacing: -0\.01em/);
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value-row \{[^}]*gap: 0\.04em/);
    expect(css).toMatch(/\.live-panel__grid \.metric-card__value-row \{[^}]*overflow: hidden/);
    expect(css).not.toMatch(/\.live-panel__grid \.metric-card__value \{[^}]*font-size: clamp\(\d+px/);
  });

  it('lays the eleven tiles out 4 × 3 on the kiosk, 5 × 3 on a landscape phone and 2 × 6 on a portrait one, none spanning', () => {
    const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');
    const phoneCss = readFileSync(fileURLToPath(new URL('./phone.css', import.meta.url)), 'utf8');
    expect(css).toMatch(
      /\.live-panel__grid \{[^}]*grid-template-columns: repeat\(5, 1fr\);[^}]*grid-template-rows: 1fr 1fr/
    );
    expect(css).toMatch(/\n\.live-panel__grid--of-11 \{[^}]*grid-template-rows: repeat\(3, 1fr\)/);
    const kiosk = css.match(
      /@media \(orientation: landscape\) and \(min-width: 800px\) and \(min-height: 400px\) and \(max-aspect-ratio: 21\/10\) \{([\s\S]*?)\n\}/
    )?.[1];
    expect(kiosk).toMatch(
      /\.live-panel__grid--of-11 \{[^}]*grid-template-columns: repeat\(4, 1fr\);[^}]*grid-template-rows: repeat\(3, 1fr\)/
    );
    expect(phoneCss).toMatch(
      /\.live-panel__grid \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);[^}]*grid-template-rows: repeat\(5, minmax\(0, 1fr\)\)/
    );
    expect(phoneCss).toMatch(/\.live-panel__grid--of-11 \{[^}]*grid-template-rows: repeat\(6, minmax\(4\.75rem, 1fr\)\)/);
    expect(phoneCss).toMatch(/\.range-details \.live-panel__body \{[^}]*overflow-y: auto;[^}]*touch-action: none/);
    // Landscape phones: rows keep room for a two-line title and the number; where three do not fit,
    // the tiles scroll inside the panel and own the gesture (drag scrolls, a tap selects).
    const landscapeCss = readFileSync(fileURLToPath(new URL('./phoneLandscape.css', import.meta.url)), 'utf8');
    expect(landscapeCss).toMatch(/\.range-details \.live-panel__grid--of-11 \{[^}]*grid-template-rows: repeat\(3, minmax\(3rem, 1fr\)\)/);
    expect(landscapeCss).toMatch(/\.range-details \.live-panel__body \{[^}]*overflow-y: auto;[^}]*touch-action: none/);
    expect(landscapeCss).toMatch(/\.range-details \.live-panel__grid--of-11 \.metric-card \{[^}]*touch-action: none/);
    expect(css).not.toContain('live-panel__grid--of-9');
    expect(css).not.toMatch(/\.live-panel__grid[^{]*:last-child \{[^}]*grid-column: span/);
    expect(phoneCss).not.toMatch(/\.live-panel__grid[^{]*:last-child \{[^}]*grid-column: span/);
  });

  it('sizes the curve class word in its own, lower fit group on kiosk and phone', () => {
    const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');
    const phoneCss = readFileSync(fileURLToPath(new URL('./phone.css', import.meta.url)), 'utf8');
    const cardCss = readFileSync(fileURLToPath(new URL('../ui/MetricCard.css', import.meta.url)), 'utf8');
    expect(css).toMatch(/\.live-panel__grid \.metric-card--class \.metric-card__value-row \{[^}]*font-size: clamp\(/);
    expect(phoneCss).toMatch(
      /\.live-panel__grid \.metric-card--class \.metric-card__value-row \{[^}]*font-size: clamp\(/
    );
    expect(cardCss).toMatch(/\.metric-card--class \.metric-card__value \{[^}]*font-variant-numeric: normal/);
  });

  it('shares one live number size across tiles instead of shrinking per card', () => {
    const liveSrc = readFileSync(fileURLToPath(new URL('./LivePanel.tsx', import.meta.url)), 'utf8');
    const cardSrc = readFileSync(fileURLToPath(new URL('../ui/MetricCard.tsx', import.meta.url)), 'utf8');

    expect(liveSrc).toContain('useSharedFitFontSize');
    expect(cardSrc).not.toContain('useFitFontSize');
    expect(cardSrc).not.toContain('useSharedFitFontSize');
  });

  it('shows the ready state before the first shot', () => {
    const html = render(null);

    expect(html).toContain('Ready');
    expect(html).toContain('live-panel__empty-title');
    expect(html).not.toContain('live-panel__spotlight');
    expect(html).not.toContain('metric-card--interactive');
  });

  it('renders all eleven metrics in a table with no hero slot', () => {
    const html = render(makeShot());

    expect(html).not.toContain('live-panel__hero');
    expect(html).toContain('live-panel__grid--of-11');
    expect(html.match(/metric-card--interactive/g)).toHaveLength(11);
    expect(html).toContain('>Attack angle<');
    expect(tileLabels(html)[0]).toBe('Ball speed');
    // The apex follows the carry it belongs to.
    expect(tileLabels(html).slice(1, 4)).toEqual(['Carry', 'Apex', 'Club speed']);
  });

  it('pins the selected metric to the top left and marks its title selected', () => {
    const html = render(makeShot(), undefined, 'spin');

    expect(tileLabels(html)[0]).toBe('Spin');
    expect(html).toContain('metric-card--selected');
    expect(html).toMatch(/metric-card--selected[\s\S]*?>Spin</);
    expect(html).toContain('>Ball speed<');
    expect(html.match(/metric-card--interactive/g)).toHaveLength(11);
  });

  it('falls back to ball speed when the selected metric is not in this set', () => {
    const html = render(makeShot(), undefined, 'swing_best');

    expect(tileLabels(html)[0]).toBe('Ball speed');
    expect(html).toMatch(/metric-card--selected[\s\S]*?>Ball speed</);
  });

  it('keeps every metric visible after a new shot', () => {
    const html = render(makeShot(), undefined, 'spin', true);

    expect(html).not.toContain('live-panel__spotlight');
    expect(html).toContain('live-panel__grid--of-11');
    expect(html.match(/metric-card--interactive/g)).toHaveLength(11);
    expect(html).toContain('shot-flash');
  });

  it('does not show the spotlight for a restored session shot', () => {
    const html = render(makeShot(), undefined, null, false);

    expect(html).not.toContain('live-panel__spotlight');
    expect(html).not.toContain('shot-flash');
  });

  it('shows the typical spin the carry used, marked typical', () => {
    const html = render(
      makeShot({
        spin_rpm: 1538,
        spin_confidence: 0.39,
        spin_quality: 'low',
        spin_source: null,
        carry_spin_rpm: 7500,
        carry_spin_source: 'club_typical',
      })
    );

    expect(html).toContain('>7,500<');
    expect(html).toContain('>typical<');
    expect(html).not.toContain('1,538');
  });

  it('marks estimated tiles with an icon instead of provenance copy', () => {
    const html = render(makeShot({ angle_source: 'estimated', spin_source: 'calculated' }));

    expect(html).toContain('metric-card__estimated');
    expect(html).not.toContain('>estimated<');
    expect(html).not.toContain('>radar<');
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never renders the spin axis, a loft, the face, face to path, the strike or the low point',
    (locale) => {
      setActiveLocale(locale);
      const html = render(
        makeShot({ ...UNMEASURABLE_CLUB_DATA, club: '7-iron', spin_axis_deg: 2.4, camera_club_path_deg: 1.1 })
      );
      expect(html).toContain('live-panel__grid--of-11');
      expect(html).not.toContain('data-metric="spin_axis"');
      for (const word of [...UNMEASURABLE_COPY[locale], 'Fade']) expect(html, word).not.toContain(word);
      expect(html).not.toMatch(/12[.,]2|19[.,]1|>12<|mm/);
      // The curve tile never shows the axis (2[.,]4) nor derives a class from it.
      const curveTile = html.match(/<button[^>]*data-metric="curve"[\s\S]*?<\/button>/)?.[0] ?? '';
      expect(curveTile).toContain('>—<');
      expect(curveTile).not.toMatch(/2[.,]4/);
    }
  );

  it.each([
    ['en', 'Curve', 'Draw', 'calculated'],
    ['de', 'Kurve', 'Gerade', 'berechnet'],
    ['es', 'Curva', 'Recta', 'calculado'],
  ] as const)('%s: renders the curve class word in its tile, never the axis number', (locale, label, word, caption) => {
    setActiveLocale(locale);
    const html = render(
      makeShot({
        club: '7-iron',
        spin_axis_deg: locale === 'en' ? -5.3 : 1.7,
        spin_axis_source: 'calculated',
        curve_class: locale === 'en' ? 'draw' : 'straight',
      })
    );
    const curveTile = html.match(/<button[^>]*data-metric="curve"[\s\S]*?<\/button>/)?.[0] ?? '';
    expect(curveTile).toContain('metric-card--class');
    expect(curveTile).toContain(`>${label}`);
    expect(curveTile).toContain(`>${word}<`);
    expect(curveTile).toContain(caption);
    expect(curveTile).not.toMatch(/5[.,]3|1[.,]7/);
    expect(html).toContain('live-panel__grid--of-11');
  });

  it.each([
    ['en', 'not for woods'],
    ['de', 'nicht bei Hölzern'],
    ['es', 'no en maderas'],
  ] as const)('%s: renders — in the curve tile for a wood', (locale, caption) => {
    setActiveLocale(locale);
    const html = render(makeShot({ club: 'driver', spin_axis_deg: null, curve_class: null }));
    const curveTile = html.match(/<button[^>]*data-metric="curve"[\s\S]*?<\/button>/)?.[0] ?? '';
    expect(curveTile).toContain('>—<');
    expect(curveTile).toContain(caption);
    expect(curveTile).not.toContain('metric-card--class');
  });

  it('makes every tile a pressable button so it can be selected', () => {
    const html = render(makeShot());

    expect(html.match(/<button[^>]*metric-card--interactive/g)).toHaveLength(11);
    expect(html).toContain('aria-pressed="true"');
  });

  it('shows the profile and club in the header', () => {
    const html = render(makeShot(), [makeShot(), makeShot(), makeShot()]);

    expect(html).toContain('panel-header__subtitle">James<');
    expect(html).toContain('panel-header__club">DR<');
    expect(html).not.toContain('mph / yds');
    expect(html).not.toContain('Shot 03');
  });

  it('places Change club in the header actions', () => {
    const html = text(
      renderToString(
        <LivePanel
          shot={null}
          shots={[]}
          profileId="james"
          profileName="James"
          clubLabel="DR"
          headerAction={
            <button type="button" className="panel-action">
              Change club
            </button>
          }
        />
      )
    );
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';

    expect(header).toContain('Change club');
    expect(header).toContain('panel-action');
  });

  it("shows the current profile's last shot, not the previous profile's", () => {
    const james = makeShot({ ball_speed_mph: 90, timestamp: 'a' });
    const alex = makeShot({ profile_id: 'alex', profile_name: 'Alex', ball_speed_mph: 150, timestamp: 'b' });
    const html = render(alex, [james, alex]);

    expect(html).toContain('>90.0<');
    expect(html).not.toContain('>150.0<');
    expect(html).not.toContain('Ready');
  });

  it('returns to ready when the current profile has no shots', () => {
    const alex = makeShot({ profile_id: 'alex', profile_name: 'Alex', ball_speed_mph: 150, timestamp: 'b' });
    const html = render(alex, [alex]);

    expect(html).toContain('Ready');
    expect(html).not.toContain('metric-card--interactive');
  });

  it('renders the five-tile grid for a swing-speed shot', () => {
    const swing = makeShot({
      mode: 'swing-speed',
      training_implement: 'stack-100g',
      training_implement_label: 'Stack 100g',
      profile_id: 'james',
      profile_name: 'James',
    });
    const html = text(
      renderToString(
        <LivePanel
          shot={swing}
          shots={[swing]}
          profileId="james"
          profileName="James"
          clubLabel="Stack 100g"
          activeTrainingImplement="stack-100g"
          onSelectMetric={() => {}}
        />
      )
    );

    expect(html).not.toContain('live-panel__hero');
    expect(html).toContain('live-panel__grid--of-5');
    expect(html.match(/metric-card--interactive/g)).toHaveLength(5);
    expect(tileLabels(html)[0]).toBe('Last swing');
  });
});
