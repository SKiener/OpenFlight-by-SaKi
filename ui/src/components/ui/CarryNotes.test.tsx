import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { makeShot } from '../../test-utils/shot';
import { CarryNotes } from './CarryNotes';

/** The span the server still sends for a typical-spin carry; no screen shows it. */
const SERVER_SPAN = { carry_span_min_yards: 120.4, carry_span_max_yards: 139.6 };
const TYPICAL_NO_AXIS = {
  carry_spin_source: 'club_typical' as const,
  ...SERVER_SPAN,
  spin_axis_deg: null,
};

afterEach(() => setActiveLocale('en'));

describe('CarryNotes', () => {
  it('prints the typical spin and the missing curve as one plain text line, not a tooltip', () => {
    setActiveLocale('de');
    const html = renderToString(<CarryNotes shot={makeShot(TYPICAL_NO_AXIS)} className="x" />);
    expect(html).toBe('<p class="carry-notes x">Geschätzt mit typischem Spin · Kurve nicht berücksichtigt</p>');
    expect(html).not.toContain('title=');
  });

  it('adds the short form beside the sentence for a layout to pick one', () => {
    setActiveLocale('de');
    const html = renderToString(<CarryNotes shot={makeShot(TYPICAL_NO_AXIS)} className="x" withShortForm />);
    expect(html).toBe(
      '<p class="carry-notes x"><span class="carry-notes__long">Geschätzt mit typischem Spin · Kurve nicht berücksichtigt</span><span class="carry-notes__short">Kurve nicht berücksichtigt</span></p>'
    );
  });

  it('leads the short form with the ≈ legend, which names the typical spin', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CarryNotes shot={makeShot(TYPICAL_NO_AXIS)} withShortForm shortLegend={{ text: 'mit typischem Spin' }} />
    );
    expect(html.replace(/<!-- -->/g, '')).toContain(
      '<span class="carry-notes__short"><span class="carry-notes__legend"><span aria-hidden="true">≈</span> mit typischem Spin · </span>Kurve nicht berücksichtigt</span>'
    );
  });

  it.each(['de', 'en', 'es'] as const)(
    '%s: shows no span for a typical-spin carry the server sent a span for',
    (locale) => {
      setActiveLocale(locale);
      const html = renderToString(
        <CarryNotes shot={makeShot(TYPICAL_NO_AXIS)} withShortForm shortLegend={{ text: 'legend' }} />
      );
      expect(html).not.toBe('');
      for (const fragment of ['110', '120', '128', '139', '140', '50–100', 'Span', 'Spanne', 'Rango']) {
        expect(html, fragment).not.toContain(fragment);
      }
    }
  );

  it('keeps the legend alone, with its layout class, when the short form has no other part', () => {
    setActiveLocale('en');
    const html = renderToString(
      <CarryNotes
        shot={makeShot({ carry_spin_source: 'club_typical', spin_axis_deg: 2 })}
        withShortForm
        shortLegend={{ text: 'calculated or with typical spin', className: 'carry-notes__legend--in-toolbar' }}
      />
    );
    expect(html.replace(/<!-- -->/g, '')).toContain(
      '<span class="carry-notes__short"><span class="carry-notes__legend carry-notes__legend--in-toolbar"><span aria-hidden="true">≈</span> calculated or with typical spin</span></span>'
    );
  });

  it('says only "Curve not considered" for a measured-spin carry without a spin axis', () => {
    const html = renderToString(<CarryNotes shot={makeShot({ spin_axis_deg: null })} />);
    expect(html).toBe('<p class="carry-notes">Curve not considered</p>');
  });

  it('renders nothing when the carry has a spin axis and measured spin', () => {
    expect(renderToString(<CarryNotes shot={makeShot({ spin_axis_deg: 2 })} />)).toBe('');
  });
});
