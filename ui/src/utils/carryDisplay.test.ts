import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { makeShot } from '../test-utils/shot';
import {
  approxLegend,
  carryNotes,
  carryNotesShort,
  carryTooltip,
  isCurveIgnored,
  isTypicalSpinCarry,
  markTypicalCarry,
  typicalCarryCaption,
} from './carryDisplay';

const TYPICAL = { carry_spin_source: 'club_typical' as const, club: '7-iron', spin_axis_deg: 2 };
/** The span the server still sends for a typical-spin carry; no screen shows it. */
const SERVER_SPAN = { carry_span_min_yards: 140.2, carry_span_max_yards: 158.6 };
const SPAN = { ...TYPICAL, ...SERVER_SPAN };

afterEach(() => setActiveLocale('en'));

describe('isTypicalSpinCarry', () => {
  it('is true only for a final club_typical carry', () => {
    expect(isTypicalSpinCarry(makeShot(TYPICAL))).toBe(true);
    expect(isTypicalSpinCarry(makeShot({ ...TYPICAL, carry_spin_adjusted: null }))).toBe(false);
    for (const source of ['measured', 'calculated', 'speed_model', null] as const) {
      expect(isTypicalSpinCarry(makeShot({ carry_spin_source: source })), String(source)).toBe(false);
    }
  });
});

describe('markTypicalCarry', () => {
  it('puts ≈ and a no-break space in front of a typical-spin carry only', () => {
    expect(markTypicalCarry(makeShot(TYPICAL), '112')).toBe('≈ 112');
    expect(markTypicalCarry(makeShot(), '112')).toBe('112');
    expect(markTypicalCarry(makeShot(TYPICAL), '112', { compact: true })).toBe('≈112');
    expect(markTypicalCarry(makeShot(), '112', { compact: true })).toBe('112');
    expect(markTypicalCarry(makeShot({ carry_spin_source: 'speed_model' }), '112')).toBe('112');
  });
});

describe('no carry span on any screen', () => {
  it.each(['de', 'en', 'es'] as const)(
    '%s: a typical-spin carry with the server span shows no span in its caption, notes or tooltip',
    (locale) => {
      setActiveLocale(locale);
      const shot = makeShot({ ...SPAN, spin_axis_deg: null });
      const texts = [typicalCarryCaption(shot), ...carryNotes(shot), ...carryNotesShort(shot), carryTooltip(shot)].join(
        ' | '
      );
      for (const fragment of ['140', '141', '158', '159', '128', '145', '50–100', 'Span', 'Spanne', 'Rango', '(']) {
        expect(texts, fragment).not.toContain(fragment);
      }
      expect(carryNotes(shot)).toEqual(carryNotes(makeShot({ ...TYPICAL, spin_axis_deg: null })));
      expect(carryNotesShort(shot)).toEqual(carryNotesShort(makeShot({ ...TYPICAL, spin_axis_deg: null })));
    }
  );
});

describe('typicalCarryCaption', () => {
  it('names the typical spin, never a span', () => {
    expect(typicalCarryCaption(makeShot(SPAN))).toBe('with typical spin');
    expect(typicalCarryCaption(makeShot(TYPICAL))).toBe('with typical spin');
    expect(typicalCarryCaption(makeShot({ carry_spin_source: 'measured', ...SERVER_SPAN }))).toBeUndefined();
    expect(typicalCarryCaption(makeShot())).toBeUndefined();
  });

  it.each([
    ['de', 'mit typischem Spin'],
    ['es', 'con spin típico'],
  ] as const)('is worded in %s', (locale, caption) => {
    setActiveLocale(locale);
    expect(typicalCarryCaption(makeShot(TYPICAL))).toBe(caption);
  });
});

describe('isCurveIgnored', () => {
  it('holds exactly when the shot has no spin axis, whatever the club', () => {
    for (const club of ['7-iron', 'pw', 'driver', 'mini-driver', '3-wood', '7-wood', '3-hybrid', '9-hybrid']) {
      expect(isCurveIgnored(makeShot({ club, spin_axis_deg: null })), club).toBe(true);
      expect(isCurveIgnored(makeShot({ club, spin_axis_deg: 3 })), club).toBe(false);
      expect(isCurveIgnored(makeShot({ club, spin_axis_deg: -6.6 })), club).toBe(false);
    }
  });

  it('is not claimed before the final carry', () => {
    expect(isCurveIgnored(makeShot({ carry_spin_adjusted: null, spin_axis_deg: null }))).toBe(false);
  });
});

describe('carryTooltip', () => {
  it('names the typical spin and the missing curve', () => {
    expect(carryTooltip(makeShot({ ...SPAN, spin_axis_deg: null }))).toBe(
      'Estimated with typical spin · Curve not considered'
    );
    expect(carryTooltip(makeShot(TYPICAL))).toBe('Estimated with typical spin');
    expect(carryTooltip(makeShot({ club: 'driver', spin_axis_deg: null }))).toBe('Curve not considered');
    expect(carryTooltip(makeShot({ club: 'driver' }))).toBeUndefined();
    expect(carryTooltip(makeShot({ club: '5-hybrid', spin_axis_deg: -4.4 }))).toBeUndefined();
    expect(carryTooltip(makeShot({ club: '7-iron' }))).toBeUndefined();
  });

  it('is worded in German', () => {
    setActiveLocale('de');
    expect(carryTooltip(makeShot({ ...SPAN, club: 'driver', spin_axis_deg: null }))).toBe(
      'Geschätzt mit typischem Spin · Kurve nicht berücksichtigt'
    );
  });
});

describe('carryNotes', () => {
  it('lists the typical spin and the missing curve as separate notes', () => {
    expect(carryNotes(makeShot({ ...SPAN, spin_axis_deg: null }))).toEqual([
      'Estimated with typical spin',
      'Curve not considered',
    ]);
    expect(carryNotes(makeShot(SPAN))).toEqual(['Estimated with typical spin']);
    expect(carryNotes(makeShot({ club: 'driver', spin_axis_deg: null }))).toEqual(['Curve not considered']);
    expect(carryNotes(makeShot({ club: '5-hybrid', spin_axis_deg: -4.4 }))).toEqual([]);
    expect(carryNotes(makeShot({ spin_axis_deg: null, carry_spin_adjusted: null }))).toEqual([]);
  });

  it('is worded in Spanish', () => {
    setActiveLocale('es');
    expect(carryNotes(makeShot({ ...SPAN, spin_axis_deg: null }))).toEqual([
      'Estimado con spin típico',
      'Curva no considerada',
    ]);
  });
});

describe('approxLegend', () => {
  it.each([
    ['de', 'berechnet bzw. mit typischem Spin', 'berechnet', 'mit typischem Spin'],
    ['en', 'calculated or with typical spin', 'calculated', 'with typical spin'],
    ['es', 'calculado o con spin típico', 'calculado', 'con spin típico'],
  ] as const)(
    '%s: names both meanings of ≈ when both are visible, else only the visible one',
    (locale, both, calculated, typical) => {
      setActiveLocale(locale);
      expect(approxLegend({ calculated: true, typicalSpin: true })).toBe(both);
      expect(approxLegend({ calculated: true, typicalSpin: false })).toBe(calculated);
      expect(approxLegend({ calculated: false, typicalSpin: true })).toBe(typical);
      expect(approxLegend({ calculated: false, typicalSpin: false })).toBeNull();
    }
  );
});

describe('carryNotesShort', () => {
  it.each([
    ['de', 'Kurve nicht berücksichtigt'],
    ['en', 'Curve not considered'],
    ['es', 'Curva no considerada'],
  ] as const)('%s: the missing curve in a few words', (locale, text) => {
    setActiveLocale(locale);
    expect(carryNotesShort(makeShot({ ...SPAN, spin_axis_deg: null })).join(' · ')).toBe(text);
  });

  it('keeps only the missing curve, never the typical spin (the ≈ legend leading the line names it)', () => {
    expect(carryNotesShort(makeShot({ ...SPAN }))).toEqual([]);
    expect(carryNotesShort(makeShot(TYPICAL))).toEqual([]);
    expect(carryNotesShort(makeShot({ ...TYPICAL, spin_axis_deg: null }))).toEqual(['Curve not considered']);
    expect(carryNotesShort(makeShot({ club: 'driver', spin_axis_deg: null }))).toEqual(['Curve not considered']);
    expect(carryNotesShort(makeShot({ club: '7-iron' }))).toEqual([]);
  });
});
