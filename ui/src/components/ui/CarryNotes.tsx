import { useI18n } from '../../i18n/useI18n';
import { carryNotes, carryNotesShort, type CarryFields } from '../../utils/carryDisplay';

/** The ≈ legend leading the short form, e.g. "≈ with typical spin". */
export interface CarryNotesLegend {
  /** The legend's words after the ≈ mark (approxLegend). */
  text: string;
  /** Extra class on the legend, for a layout that already shows it elsewhere. */
  className?: string;
}

/**
 * The carry's typical spin and "Curve not considered" as a plain text
 * line. A touch kiosk never shows a title tooltip, so the views a player taps
 * into (the shot detail, the Range details sheet) print the notes instead.
 * `withShortForm` adds the short form beside the sentence, for a layout to show
 * one of them (`carry-notes__long`, `carry-notes__short`). The short form leads
 * with `shortLegend` ("≈ with typical spin · Curve not considered"): it names
 * the typical spin only through that legend. Renders nothing when the carry has
 * no notes.
 */
export function CarryNotes({
  shot,
  className,
  withShortForm = false,
  shortLegend = null,
}: {
  shot: CarryFields;
  className?: string;
  withShortForm?: boolean;
  shortLegend?: CarryNotesLegend | null;
}) {
  useI18n();
  const notes = carryNotes(shot);
  if (notes.length === 0) return null;
  const classes = ['carry-notes', className].filter(Boolean).join(' ');
  if (!withShortForm) return <p className={classes}>{notes.join(' · ')}</p>;
  const short = carryNotesShort(shot).join(' · ');
  return (
    <p className={classes}>
      <span className="carry-notes__long">{notes.join(' · ')}</span>
      <span className="carry-notes__short">
        {shortLegend ? (
          <span className={['carry-notes__legend', shortLegend.className].filter(Boolean).join(' ')}>
            <span aria-hidden="true">≈</span> {shortLegend.text}
            {short ? ' · ' : ''}
          </span>
        ) : null}
        {short}
      </span>
    </p>
  );
}
