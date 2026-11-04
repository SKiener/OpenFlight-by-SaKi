/**
 * Width / height of a camera frame, as one number for CSS.
 *
 * Every camera view (Align preview, replay, /display, companion) sizes its
 * box to the frame's own shape so the whole sensor frame shows and overlays
 * map to the picture. The sensor is 640 x 400 (16:10) in the full mode and
 * 640 x 200 in the strip mode.
 */
export const DEFAULT_FRAME_ASPECT = 640 / 400;

function validSize(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0;
}

/** The aspect of the first valid width/height pair, else the 16:10 default. */
export function frameAspect(
  ...sizes: Array<[number | null | undefined, number | null | undefined] | null | undefined>
) {
  for (const size of sizes) {
    if (!size) continue;
    const [width, height] = size;
    if (validSize(width) && validSize(height)) return width / height;
  }
  return DEFAULT_FRAME_ASPECT;
}

/** The aspect as a CSS number, rounded so style strings stay stable. */
export function cssFrameAspect(aspect: number): string {
  return String(Math.round(aspect * 10000) / 10000);
}
