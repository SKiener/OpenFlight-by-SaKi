import { t } from '../i18n';

/**
 * Honest carry: a carry flown with the club-typical spin (the radar spin was not
 * trusted) is a guess at the spin, so every screen marks it with ≈ and names the
 * typical spin. The server also sends a carry span over 50–100 % of that spin
 * (`carry_span_min_yards`/`carry_span_max_yards`); no screen shows it, as no
 * launch monitor does, so the UI does not read it.
 */

/** Marks a carry flown with typical spin. A no-break space keeps it on the number's line. */
export const TYPICAL_CARRY_MARK = '≈';

/** The carry fields these helpers read; live shots and saved history rows both have them. */
export interface CarryFields {
  carry_spin_adjusted?: number | null;
  carry_spin_source?: string | null;
  spin_axis_deg?: number | null;
}

const isFiniteNumber = (value: number | null | undefined): value is number =>
  typeof value === 'number' && Number.isFinite(value);

/** The final carry was flown with the club-typical spin. */
export function isTypicalSpinCarry(shot: CarryFields): boolean {
  return isFiniteNumber(shot.carry_spin_adjusted) && shot.carry_spin_source === 'club_typical';
}

/** A formatted carry with the ≈ mark in front ("≈ 112"). */
export function withTypicalCarryMark(formatted: string): string {
  return `${TYPICAL_CARRY_MARK}\u00a0${formatted}`;
}

/**
 * `formatted` with the ≈ mark in front when the carry was flown with typical spin.
 * `compact` drops the space ("≈112") for narrow table columns, like the spin cell's "≈7,500".
 */
export function markTypicalCarry(shot: CarryFields, formatted: string, { compact = false } = {}): string {
  if (!isTypicalSpinCarry(shot)) return formatted;
  return compact ? `${TYPICAL_CARRY_MARK}${formatted}` : withTypicalCarryMark(formatted);
}

/** The caption of a typical-spin carry, "with typical spin"; undefined for any other carry. */
export function typicalCarryCaption(shot: CarryFields): string | undefined {
  return isTypicalSpinCarry(shot) ? t('carry.typicalSpin') : undefined;
}

/**
 * The carry's offline comes from the start direction alone: the shot has no spin
 * axis. The server flies the spin axis for every club that has one (live drivers
 * and woods get none from the D-plane, so they arrive without one).
 */
export function isCurveIgnored(shot: CarryFields): boolean {
  if (!isFiniteNumber(shot.carry_spin_adjusted)) return false;
  return !isFiniteNumber(shot.spin_axis_deg);
}

/**
 * The carry's notes: "Estimated with typical spin" and "Curve not considered".
 * Empty when neither applies.
 */
export function carryNotes(shot: CarryFields): string[] {
  return [
    isTypicalSpinCarry(shot) ? t('carry.typicalSpinNote') : undefined,
    isCurveIgnored(shot) ? t('carry.curveIgnored') : undefined,
  ].filter((part): part is string => part !== undefined);
}

/**
 * The short form of {@link carryNotes} for the Range details sheet on short
 * phones: only "Curve not considered". It never names the typical spin: the ≈
 * legend leading that line (or, on landscape phones, the toolbar's) does
 * ({@link approxLegend}). Empty when the curve was considered.
 */
export function carryNotesShort(shot: CarryFields): string[] {
  return isCurveIgnored(shot) ? [t('carry.curveIgnored')] : [];
}

/**
 * Tooltip for a carry: its {@link carryNotes} on one line. Short tiles show only
 * the ≈ mark and keep these details here; a touch screen never shows a tooltip,
 * so the shot detail and the Range details sheet also print them as text
 * (CarryNotes). Undefined when neither applies.
 */
export function carryTooltip(shot: CarryFields): string | undefined {
  const parts = carryNotes(shot);
  return parts.length > 0 ? parts.join(' · ') : undefined;
}

/**
 * The text of a single ≈ legend: what the ≈ marks in its view mean. "calculated"
 * for calculated values alone, "with typical spin" for a typical-spin carry alone,
 * both when both are visible ("calculated or with typical spin"); null for neither.
 */
export function approxLegend({
  calculated,
  typicalSpin,
}: {
  calculated: boolean;
  typicalSpin: boolean;
}): string | null {
  if (calculated && typicalSpin) return t('metric.calculatedOrTypicalSpin');
  if (calculated) return t('metric.calculated');
  if (typicalSpin) return t('carry.typicalSpin');
  return null;
}
