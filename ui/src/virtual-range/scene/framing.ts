import type { PerspectiveCamera } from 'three';
import type { RangeInsets } from '../insets';
import type { LabelBox } from './labelLayout';

export const coveredPx = (value: number): number => (Number.isFinite(value) && value > 0 ? value : 0);

/**
 * Fits the camera to a `width` × `height` canvas and moves the picture so the middle of the view is the middle of
 * the part the chrome leaves free: flight and landing sit there, not under a bar. Sizes on screen stay the same.
 */
export function frameCamera(camera: PerspectiveCamera, width: number, height: number, insets: RangeInsets): void {
  camera.aspect = width / height;
  const shiftDown = (coveredPx(insets.top) - coveredPx(insets.bottom)) / 2;
  if (shiftDown === 0) camera.clearViewOffset();
  else camera.setViewOffset(width, height, 0, -shiftDown, width, height);
}

/** The strips of the canvas under the chrome, in canvas px. */
export function chromeAreas(width: number, height: number, insets: RangeInsets): LabelBox['rect'][] {
  const top = coveredPx(insets.top);
  const bottom = coveredPx(insets.bottom);
  const areas: LabelBox['rect'][] = [];
  if (top > 0) areas.push([0, 0, width, top]);
  if (bottom > 0) areas.push([0, height - bottom, width, height]);
  return areas;
}
