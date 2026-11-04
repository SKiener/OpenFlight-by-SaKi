import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import type { Shot } from '../types/shot';
import { DispersionView, type DispersionViewProps } from './DispersionView';

const shot = (overrides: Partial<Shot>): Shot => ({
  ball_speed_mph: 120,
  club_speed_mph: 88,
  smash_factor: 1.36,
  estimated_carry_yards: 160,
  carry_range: [150, 170],
  club: '7-iron',
  timestamp: '2026-10-04T12:00:00Z',
  peak_magnitude: null,
  launch_angle_vertical: 18,
  launch_angle_horizontal: -1,
  launch_angle_confidence: 0.8,
  launch_angle_horizontal_confidence: 0.8,
  launch_angle_horizontal_source: 'radar',
  angle_source: 'radar',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 6500,
  spin_confidence: 0.9,
  spin_quality: 'high',
  spin_source: 'measured',
  carry_spin_adjusted: 155,
  carry_spin_source: 'measured',
  profile_id: 'p1',
  ...overrides,
});

const irons = [150, 155, 160, 152, 158].map((carry, index) =>
  shot({ timestamp: `2026-10-04T12:0${index}:00Z`, carry_spin_adjusted: carry, carry_lateral_yards: index - 2 })
);
const liveShots = [
  ...irons,
  shot({
    timestamp: '2026-10-04T12:06:00Z',
    club: 'wedge-54',
    carry_spin_adjusted: 90,
    launch_angle_horizontal: 2,
    carry_spin_source: 'club_typical',
  }),
  shot({
    timestamp: '2026-10-04T12:07:00Z',
    club: 'wedge-54',
    carry_spin_adjusted: 85,
    carry_lateral_yards: 1,
    launch_angle_horizontal_source: 'estimated',
    carry_spin_source: 'club_typical',
  }),
  shot({ timestamp: '2026-10-04T12:08:00Z', club: 'driver', launch_angle_horizontal: null }),
];

const props: DispersionViewProps = {
  liveShots,
  profileId: 'p1',
  bagClubIds: ['driver', '7-iron', 'wedge-54'],
  unitSystem: 'mph-meters',
  calibration: { horizontal_phase_reference: false },
  history: { status: 'idle', sessions: [], unavailable: [] },
  onLoadHistory: () => {},
  now: Date.parse('2026-10-04T13:00:00Z'),
};

const count = (html: string, pattern: RegExp) => html.match(pattern)?.length ?? 0;

afterEach(() => {
  setActiveLocale('en');
});

describe('DispersionView', () => {
  it('plots every shot with a direction, hollow where lateral is derived or the direction estimated', () => {
    const html = renderToString(<DispersionView {...props} />);

    expect(count(html, /class="dispersion-plot__point"/g)).toBe(7);
    expect(count(html, /data-hollow="true"/g)).toBe(2);
    expect(count(html, /class="dispersion-plot__ellipse"/g)).toBe(1);
    expect(count(html, /class="dispersion-plot__mean"/g)).toBe(2);
    expect(html).toContain('aria-label="7 Iron: carry 137 m, 2 m left"');
    expect(html).toContain('style="color:var(--dispersion-2)"');
    expect(html).toContain('Target');
  });

  it('labels estimated inputs honestly and counts what it cannot plot', () => {
    const html = renderToString(<DispersionView {...props} />);

    expect(html).toContain('Horizontal angle not calibrated – the lateral position may be systematically shifted');
    expect(html).toContain('Carry calculated with substitute spin (2 of 7 shots)');
    expect(html).toContain('1 without direction (not plotted)');
    expect(html).toContain('2σ ellipse ≈ 86 % of shots (2-D normal, from 5 shots)');
    expect(html).toContain('Hollow: lateral derived (carry × sin HLA) or direction estimated/uncertain');
    expect(renderToString(<DispersionView {...props} calibration={null} />)).toContain('not calibrated');
    expect(
      renderToString(<DispersionView {...props} calibration={{ horizontal_phase_reference: true }} />)
    ).not.toContain('not calibrated');
  });

  it('summarises clubs in bag order with mean ± SD in the profile units', () => {
    const html = renderToString(<DispersionView {...props} />);
    const order = [...html.matchAll(/<tbody data-club="([^"]+)"/g)].map((match) => match[1]);

    expect(order).toEqual(['wedge-54', '7-iron', 'driver']);
    expect(html).toContain('<strong>142</strong><small>± <!-- -->4</small>');
    expect(html).toContain('<strong>0</strong><small>± <!-- -->1</small>');
    expect(html).toContain('<strong>120.0</strong>');
    expect(html).toContain('<strong>-1.0</strong>');
    expect(html).toMatch(/Driver<span class="dispersion-table__count">n = <!-- -->0<\/span>/);
    expect(html).toContain('+1 not plotted');
    expect(html).toContain('<small>m</small>');
    expect(html).toContain('<small>mph</small>');
    expect(renderToString(<DispersionView {...props} unitSystem="imperial" />)).toContain('<strong>155</strong>');
  });

  it('offers the session, 7 and 30 day ranges and a pinned All chip', () => {
    const html = renderToString(<DispersionView {...props} />);

    expect(html).toMatch(/aria-pressed="true">This session</);
    expect(html).toContain('>7 days<');
    expect(html).toContain('>30 days<');
    expect(html).toMatch(/aria-pressed="true">All \(8\)</);
    expect(html.indexOf('All (8)')).toBeLessThan(html.indexOf('dispersion__chip-scroll'));
  });

  it('renders German copy', () => {
    setActiveLocale('de');

    const html = renderToString(<DispersionView {...props} />);

    expect(html).toContain('aria-label="Streuung"');
    expect(html).toContain('Aktuelle Session');
    expect(html).toContain('Seitenwinkel nicht kalibriert – die seitliche Lage kann systematisch verschoben sein');
    expect(html).toContain('Carry mit Ersatzspin berechnet (2 von 7 Schlägen)');
    expect(html).toContain('1 ohne Richtung (nicht dargestellt)');
    expect(html).toContain('Seitlich (m) · links − / rechts +');
  });

  it('explains an empty range instead of drawing an empty chart', () => {
    const html = renderToString(<DispersionView {...props} liveShots={[]} />);

    expect(html).toContain('No shots with a direction in this time range yet');
    expect(html).not.toContain('dispersion-plot');
  });
});
