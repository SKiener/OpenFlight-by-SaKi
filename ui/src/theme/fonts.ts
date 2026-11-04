/**
 * The font stacks for canvas text: the body face (Nexa) for Chart.js, the
 * display face (a•space) for the 3D range signs. A canvas cannot read
 * `var(--font-body)` or `var(--font-display)`, so these mirror index.css;
 * fonts.test.ts keeps them in sync.
 */
export const BODY_FONT_STACK = "'Nexa', 'A-Space', Arial, 'Liberation Sans', sans-serif";
export const DISPLAY_FONT_STACK = "'A-Space', Arial, 'Liberation Sans', sans-serif";

/**
 * What `document.fonts` is asked about: the faces a canvas draws with, never
 * the fallback. Chart.js draws its ticks at normal weight (Nexa Light) and its
 * tooltip titles bold (Nexa Bold).
 */
export const BODY_FONT_PROBES: readonly string[] = ["400 1em 'Nexa'", "700 1em 'Nexa'"];
/** The signs draw bold, which a•space synthesizes from its Regular face. */
export const DISPLAY_FONT_PROBES: readonly string[] = ["700 1em 'A-Space'"];

type FontSet = Pick<FontFaceSet, 'check' | 'load'>;

function documentFonts(): FontSet | undefined {
  return typeof document === 'undefined' ? undefined : document.fonts;
}

/**
 * A canvas draws only with a face that has already loaded; until then it
 * falls back to the next face in its stack and keeps that drawing. Returns
 * null when every probed face is ready (or the browser has no font loading
 * API), else a promise that settles once they are, so the caller can draw
 * again. Never rejects.
 */
export function fontPending(probes: readonly string[], fonts: FontSet | undefined = documentFonts()): Promise<void> | null {
  if (!fonts) return null;
  try {
    const missing = probes.filter((probe) => !fonts.check(probe));
    if (missing.length === 0) return null;
    // Each load settles on its own, so a failed face does not cut the wait for another one short.
    return Promise.all(missing.map((probe) => fonts.load(probe).catch(() => []))).then(() => undefined);
  } catch {
    return null;
  }
}

/** Nexa, for canvases that draw text with BODY_FONT_STACK (Chart.js). */
export function bodyFontPending(fonts: FontSet | undefined = documentFonts()): Promise<void> | null {
  return fontPending(BODY_FONT_PROBES, fonts);
}

/** a•space, for canvases that draw with DISPLAY_FONT_STACK (the 3D range signs). */
export function displayFontPending(fonts: FontSet | undefined = documentFonts()): Promise<void> | null {
  return fontPending(DISPLAY_FONT_PROBES, fonts);
}
