import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import type { SimShotInfo } from '../types/socket';
import { SimShotBadges } from './SimShotBadges';

const gsproShot: SimShotInfo = {
  target: 'gspro',
  shot_number: 14,
  fields: ['ball_speed', 'vla', 'club_path'],
  values: { ball_speed: 142, vla: 12.4, club_path: 0 },
  provenance: { ball_speed: 'measured', vla: 'measured', club_path: 'estimated' },
};

describe('SimShotBadges', () => {
  it('renders nothing with no sim shots', () => {
    expect(renderToString(<SimShotBadges latestSimShots={{}} />)).toBe('');
  });

  it('renders per-field values and measured/estimated badges', () => {
    const html = renderToString(<SimShotBadges latestSimShots={{ gspro: gsproShot }} />);
    expect(html).toContain('Sent to GSPro');
    expect(html).toContain('#14');
    expect(html).toContain('sim-shot-badges__badge--measured');
    expect(html).toContain('sim-shot-badges__badge--estimated');
    // 2 measured / 1 estimated
    expect(html).toContain('2 measured / 1 estimated');
  });

  it.each(['en', 'de', 'es'] as const)('hides the spin axis and side spin we cannot measure in %s', (locale) => {
    setActiveLocale(locale);
    try {
      const dplane: SimShotInfo = {
        target: 'gspro',
        shot_number: 15,
        fields: ['ball_speed', 'spin_axis', 'side_spin'],
        values: { ball_speed: 120, spin_axis: 2.4, side_spin: 293 },
        provenance: { ball_speed: 'measured', spin_axis: 'calculated', side_spin: 'calculated' },
      };
      const html = renderToString(<SimShotBadges latestSimShots={{ gspro: dplane }} />);
      expect(html).not.toContain('Axis');
      expect(html).not.toContain('Sidespin');
      expect(html).not.toContain('2.4');
      expect(html).not.toContain('293');
      expect(html).not.toContain('sim-shot-badges__badge--calculated');
      expect(html.split('sim-shot-badges__item').length - 1).toBe(1);
    } finally {
      setActiveLocale('en');
    }
  });

  it('renders a card per connected simulator', () => {
    const ogs: SimShotInfo = {
      target: 'opengolfsim',
      shot_number: 3,
      fields: ['ball_speed', 'spin_axis'],
      values: { ball_speed: 130, spin_axis: -2 },
      provenance: { ball_speed: 'measured', spin_axis: 'estimated' },
    };
    const partee: SimShotInfo = { ...gsproShot, target: 'partee', shot_number: 4 };
    const html = renderToString(<SimShotBadges latestSimShots={{ gspro: gsproShot, opengolfsim: ogs, partee }} />);
    expect(html).toContain('Sent to GSPro');
    expect(html).toContain('Sent to OpenGolfSim');
    expect(html).toContain('Sent to PAR-TEE');
  });
});
