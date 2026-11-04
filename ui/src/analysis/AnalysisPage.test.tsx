import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { AnalysisCarryCell, AnalysisCarryDetail } from './AnalysisPage';
import type { AnalysisShot } from './data';

const shot = (overrides: Partial<AnalysisShot> = {}): AnalysisShot => ({
  key: 's1:1',
  session_id: 's1',
  shot_number: 1,
  timestamp: '2026-10-25T12:00:00Z',
  club: '7-iron',
  profile_name: 'Golfer',
  ball_speed_mph: 115,
  club_speed_mph: null,
  estimated_carry_yards: 150,
  carry_spin_adjusted: 122.8,
  carry_spin_source: 'club_typical',
  launch_angle_vertical: 18,
  spin_rpm: null,
  ...overrides,
});

const text = (html: string) => html.replace(/<!-- -->/g, '').replace(/<[^>]+>/g, '|');

const cell = (row: AnalysisShot) => text(renderToString(<table><tbody><tr><AnalysisCarryCell shot={row} unitSystem="metric" /></tr></tbody></table>));
const detail = (row: AnalysisShot) => text(renderToString(<AnalysisCarryDetail shot={row} unitSystem="metric" />));

afterEach(() => setActiveLocale('en'));

describe('AnalysisCarryCell', () => {
  it('marks a typical-spin carry with ≈ and names the typical spin in its tooltip', () => {
    setActiveLocale('de');
    const html = renderToString(<table><tbody><tr><AnalysisCarryCell shot={shot()} unitSystem="metric" /></tr></tbody></table>);
    expect(text(html)).toContain('≈ 112,3');
    expect(html).toContain('title="Geschätzt mit typischem Spin');
  });

  it('leaves a measured or provisional carry unmarked', () => {
    expect(cell(shot({ carry_spin_source: 'measured' }))).not.toContain('≈');
    expect(cell(shot({ carry_spin_adjusted: null }))).not.toContain('≈');
  });
});

describe('AnalysisCarryDetail', () => {
  it('shows ≈ and "with typical spin" on a typical-spin carry', () => {
    const html = detail(shot());
    expect(html).toContain('≈ 112.3 m');
    expect(html).toContain('|with typical spin|');
  });

  it('is worded in German', () => {
    setActiveLocale('de');
    expect(detail(shot())).toContain('|mit typischem Spin|');
  });

  it('gives a measured carry neither', () => {
    const html = detail(shot({ carry_spin_source: 'measured' }));
    expect(html).toContain('|112.3 m|');
    expect(html).not.toContain('≈');
    expect(html).not.toContain('typical spin');
  });
});
