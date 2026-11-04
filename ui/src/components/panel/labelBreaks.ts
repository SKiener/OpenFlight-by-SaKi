/**
 * Where a long German compound title may break, with a hyphen, in a tile too
 * narrow for it on one line. a•space is wide: "SCHLÄGERTEMPO" needs about
 * 160 px in a 5 × 2 kiosk tile that has 138 px at the 12 px caption floor.
 * Browsers hyphenate German only with a downloaded dictionary, which an
 * offline Pi does not have, so the break points are spelled out here as soft
 * hyphens (invisible unless the word breaks there).
 */
const COMPOUNDS: readonly (readonly [head: string, tail: string])[] = [
  ['Abflug', 'winkel'],
  ['Ball', 'tempo'],
  ['Eintreff', 'winkel'],
  ['Flug', 'weite'],
  ['Mess', 'werte'],
  ['Schläger', 'pfad'],
  ['Schläger', 'tempo'],
  ['Start', 'richtung'],
  // The Align chip "Aufnahme bleibt schlagbereit" beside a landscape phone's picture (81 px).
  ['schlag', 'bereit'],
];

export const SOFT_HYPHEN = '­';

/** The label with a soft hyphen at each known compound's joint; other text is unchanged. */
export function withLabelBreaks(label: string): string {
  let out = label;
  for (const [head, tail] of COMPOUNDS) {
    out = out.split(head + tail).join(head + SOFT_HYPHEN + tail);
  }
  return out;
}
