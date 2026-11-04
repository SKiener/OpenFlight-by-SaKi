import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import type { ReactNode } from 'react';
import type { Shot } from '../../types/shot';
import { setActiveLocale } from '../../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../../test-utils/shot';
import { StatsClubPage, StatsHeaderSubtitle, StatsPanel } from './StatsPanel';

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 90,
    club_speed_mph: 67,
    smash_factor: 1.34,
    estimated_carry_yards: 200,
    carry_range: [195, 205],
    club: 'driver',
    profile_id: 'james',
    profile_name: 'James',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13,
    launch_angle_horizontal: 0,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: 2600,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: null,
    ...overrides,
  };
}

const render = (shots: Shot[], activeClub = 'driver', headerAction?: ReactNode) =>
  text(
    renderToString(
      <StatsPanel
        shots={shots}
        activeClub={activeClub}
        profileId="james"
        profileName="James"
        headerAction={headerAction}
      />
    )
  );

describe('StatsPanel', () => {
  it('shows an empty state before any shots', () => {
    const html = render([]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('stats-panel__grid');
    expect(html).not.toContain('All (0)');
    expect(html).not.toContain('Filter by club');
  });

  it('renders six tiles for a ball-strike session', () => {
    const html = render([makeShot(), makeShot({ ball_speed_mph: 96, timestamp: 'b' })]);

    expect(html).toContain('stats-panel__grid--of-6');
    for (const label of ['Shots', 'Avg ball', 'Max ball', 'Avg carry', 'Avg club', 'Avg smash']) {
      expect(html).toContain(`>${label}<`);
    }
    expect(html).toContain('metric-card__value">93.0<'); // avg of 90 and 96
    expect(html).toContain('metric-card__value">96.0<'); // max
  });

  it('marks the average carry with ≈ once a typical-spin carry is in it', () => {
    const measured = makeShot({ carry_spin_adjusted: 200, carry_spin_source: 'measured' });
    const typical = makeShot({ carry_spin_adjusted: 210, carry_spin_source: 'club_typical', timestamp: 'b' });

    expect(render([measured])).toContain('metric-card__value">200<');
    const html = render([measured, typical]);
    expect(html).toContain('metric-card__value">≈\u00a0205<');
    expect(html).toContain('title="Estimated with typical spin"');
  });

  it('counts an uncertain ball speed but leaves it out of the averages and their ≈', () => {
    const measured = makeShot({ carry_spin_adjusted: 200, carry_spin_source: 'measured' });
    const uncertain = makeShot({
      ball_speed_mph: 43.5,
      carry_spin_adjusted: 90,
      carry_spin_source: 'club_typical',
      ball_speed_status: 'uncertain',
      timestamp: 'b',
    });

    const html = render([measured, uncertain]);

    expect(html).toMatch(/>Shots<[\s\S]*?metric-card__value">2</);
    expect(html).toContain('metric-card__value">90.0<'); // avg and max ball: the measured shot only
    expect(html).not.toContain('43.5');
    expect(html).toContain('metric-card__value">200<');
    expect(html).not.toContain('title="Estimated with typical spin"');
  });

  it('keeps the tile count stable when club speed is missing', () => {
    // Six tiles either way, so the 3x2 grid never reflows.
    const html = render([makeShot({ club_speed_mph: null, smash_factor: null })]);

    expect(html).toContain('stats-panel__grid--of-6');
    expect(html).toContain('>Avg club<');
    expect(html).toContain('metric-card__value">—<');
  });

  it('renders four tiles for a swing-speed session', () => {
    const swing = makeShot({ mode: 'swing-speed', club: 'Swing Speed' });
    const html = render([swing], 'Swing Speed');

    expect(html).toContain('stats-panel__grid--of-4');
    for (const label of ['Swings', 'Last', 'Best', 'Average']) {
      expect(html).toContain(`>${label}<`);
    }
  });

  it('offers an All chip plus one per club, with counts', () => {
    const html = render([makeShot(), makeShot({ club: '7-iron', timestamp: 'b' })]);

    expect(html).toContain('All (2)');
    expect(html).toContain('DRIVER (1)');
    expect(html).toContain('7-IRON (1)');
  });

  it('puts club filters above the metric tiles, not in the header', () => {
    // Extra clubs used to live in panel-header__actions and clip the title.
    const html = render([
      makeShot(),
      makeShot({ club: '3-wood', timestamp: 'b' }),
      makeShot({ club: '5-iron', timestamp: 'c' }),
      makeShot({ club: 'pw', timestamp: 'd' }),
    ]);
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';

    expect(header).not.toContain('Filter by club');
    expect(html).toContain('stats-panel__chips');
    expect(html.indexOf('stats-panel__chips')).toBeLessThan(html.indexOf('stats-panel__grid'));
    expect(html).toContain('PW (1)');
  });

  it('places Clear session in the header', () => {
    const html = render(
      [],
      'driver',
      <button type="button" className="panel-action">
        Clear session
      </button>
    );
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';

    expect(header).toContain('Clear session');
  });

  it('keeps All fixed while the club chips scroll', () => {
    const html = render([
      makeShot(),
      makeShot({ club: '3-wood', timestamp: 'b' }),
      makeShot({ club: 'pw', timestamp: 'c' }),
    ]);
    // The strip ends where the chip row does (the page switch inside it has its own <div>).
    const scroller = html.match(/stats-panel__chip-scroll[\s\S]*?<\/div><\/div>/)?.[0] ?? '';

    expect(html.indexOf('All (3)')).toBeLessThan(html.indexOf('stats-panel__chip-scroll'));
    expect(scroller).not.toContain('All (');
    expect(scroller).toContain('DRIVER (1)');
    expect(scroller).toContain('PW (1)');
  });

  it('preselects the active club when it has shots', () => {
    const html = render([makeShot(), makeShot({ club: '7-iron', timestamp: 'b' })], '7-iron');
    const activeChip = html.match(/<button[^>]*panel-chip--active[^>]*>([^<]*)</)?.[1];

    expect(activeChip).toBe('7-IRON (1)');
    // Stats reflect the filter, not the whole session.
    expect(html).toContain('metric-card__value">1<');
  });

  it('falls back to All when the active club has no shots yet', () => {
    const html = render([makeShot()], 'sw');
    const activeChip = html.match(/<button[^>]*panel-chip--active[^>]*>([^<]*)</)?.[1];

    expect(activeChip).toBe('All (1)');
  });

  it('computes averages and club chips from the current profile only', () => {
    const html = render([
      makeShot({ ball_speed_mph: 90, timestamp: 'a' }),
      makeShot({ profile_id: 'alex', profile_name: 'Alex', ball_speed_mph: 150, club: '7-iron', timestamp: 'b' }),
    ]);

    expect(html).toContain('All (1)');
    expect(html).toContain('DRIVER (1)');
    expect(html).not.toContain('7-IRON');
    expect(html).toContain('metric-card__value">1<');
    expect(html).toContain('metric-card__value">90.0<');
    expect(html).not.toContain('metric-card__value">150.0<');
    expect(html).not.toContain('metric-card__value">120.0<');
  });

  it('shows the empty state when only other profiles have shots', () => {
    const html = render([makeShot({ profile_id: 'alex', profile_name: 'Alex' })]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('stats-panel__grid');
    expect(html).not.toContain('All (0)');
    expect(html).not.toContain('Filter by club');
  });

  it('paints chips and tiles on the same surface', () => {
    // Light theme made this look like a cream chip gutter over a white spreadsheet.
    expect(css).toMatch(/\.stats-panel \{[^}]*background: var\(--color-surface\)/);
    expect(css).not.toMatch(/\.stats-panel__grid \.metric-card--label-above \{[^}]*align-content: center/);
  });

  it('offers a Ball | Club switch first in the chip row, outside the club filter and its scrolling strip', () => {
    const html = render(
      [makeShot(), makeShot({ club: '7-iron', timestamp: 'b' })],
      'driver',
      <button type="button" className="panel-action">
        Clear session
      </button>
    );
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';
    // The header keeps its one line on phones.
    expect(header).not.toContain('Metrics page');
    const row = html.match(/stats-panel__chips[\s\S]*$/)?.[0] ?? '';
    expect(row).toContain('aria-label="Metrics page"');
    expect(row.indexOf('>Ball<')).toBeLessThan(row.indexOf('>Club<'));
    // Switch, then the club filter group (All fixed, then the scrolling strip).
    expect(row.indexOf('>Club<')).toBeLessThan(row.indexOf('stats-panel__club-filter'));
    expect(row.indexOf('stats-panel__club-filter')).toBeLessThan(row.indexOf('All (2)'));
    expect(html.indexOf('All (2)')).toBeLessThan(html.indexOf('stats-panel__chip-scroll'));
    const group = row.match(/stats-panel__club-filter" role="group" aria-label="Filter by club"[\s\S]*$/)?.[0] ?? '';
    expect(group).not.toBe('');
    expect(group).not.toContain('Metrics page');
    const strip = html.match(/stats-panel__chip-scroll[\s\S]*$/)?.[0] ?? '';
    expect(strip).not.toContain('Metrics page');
    expect(strip).toContain('DRIVER (1)');
    // The ball page stays the default and its header names the profile only.
    expect(html).toContain('stats-panel__grid--of-6');
    expect(html).not.toContain('stats-panel__grid--club');
    expect(header).not.toContain('stats-panel__legend');
  });

  it('gives the switch its own row on portrait phones with the height for it', () => {
    const phone = readFileSync(fileURLToPath(new URL('./phone.css', import.meta.url)), 'utf8');
    const narrow = phone.slice(phone.indexOf('@media (min-height: 500px)'));
    expect(narrow).toMatch(/\.stats-panel__chips \{[^}]*flex-wrap: wrap/);
    expect(narrow).toMatch(/\.stats-panel__club-filter \{[^}]*flex-basis: 100%/);
  });

  it('has no switch for a swing-speed session or before the first shot', () => {
    expect(render([makeShot({ mode: 'swing-speed', club: 'Swing Speed' })], 'Swing Speed')).not.toContain(
      'Metrics page'
    );
    expect(render([])).not.toContain('Metrics page');
  });
});

describe('StatsHeaderSubtitle', () => {
  afterEach(() => setActiveLocale('en'));

  it('names the ≈ mark on the club page, where short tiles drop "berechnet"', () => {
    setActiveLocale('de');
    expect(text(renderToString(<StatsHeaderSubtitle profileName="Golfer" clubPage />))).toBe(
      'Golfer · <span class="stats-panel__legend"><span aria-hidden="true">≈</span> berechnet</span>'
    );
    expect(text(renderToString(<StatsHeaderSubtitle profileName="Golfer" clubPage={false} />))).toBe('Golfer');
  });
});

describe('StatsClubPage', () => {
  afterEach(() => setActiveLocale('en'));

  const club = (overrides: Partial<Shot>, i: number) => makeShot({ club: '8-iron', timestamp: `t${i}`, ...overrides });

  it('asks for a club while "All" is selected', () => {
    setActiveLocale('de');
    const html = text(renderToString(<StatsClubPage shots={[club({}, 0)]} club={null} />));
    expect(html).toContain('Schläger wählen');
    expect(html).not.toContain('stats-panel__grid');
  });

  it('renders the path and attack-angle tiles in a grid of their own, side by side', () => {
    setActiveLocale('de');
    const shots = [-1, 1, 3].map((path, i) => club({ camera_club_path_deg: path }, i));
    const html = text(renderToString(<StatsClubPage shots={shots} club="8-iron" />));
    expect(html).toContain('stats-panel__grid stats-panel__grid--of-2 stats-panel__grid--club');
    expect(html.match(/metric-card__label/g)).toHaveLength(2);
    for (const label of ['Ø Pfad', 'Ø Eintreffwinkel']) {
      expect(html).toContain(`>${label}<`);
    }
    expect(html).toContain('metric-card__value">+1,0 ± 1,2<');
    expect(html).toContain('n=3 · berechnet');
    // Two tiles in one row (kiosk, landscape phones), one per row on a portrait phone: no empty cell.
    const phone = readFileSync(fileURLToPath(new URL('./phone.css', import.meta.url)), 'utf8');
    expect(css).toMatch(
      /\.stats-panel__grid\.stats-panel__grid--of-2 \{[^}]*grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);[^}]*grid-template-rows: minmax\(0, 1fr\)/
    );
    expect(phone).toMatch(
      /\.stats-panel__grid\.stats-panel__grid--of-2 \{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*grid-template-rows: repeat\(2, minmax\(0, 1fr\)\)/
    );
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows the face, face to path, the spin axis, the strike or the low point',
    (locale) => {
      setActiveLocale(locale);
      const shots = Array.from({ length: 9 }, (_, i) =>
        club({ ...UNMEASURABLE_CLUB_DATA, camera_club_path_deg: i / 2, camera_attack_angle_deg: -4 }, i)
      );
      const html = text(renderToString(<StatsClubPage shots={shots} club="8-iron" />));
      for (const word of UNMEASURABLE_COPY[locale]) expect(html, word).not.toContain(word);
      expect(html).not.toMatch(/12[.,]2|19[.,]1|cm|mm/);
    }
  );
});
