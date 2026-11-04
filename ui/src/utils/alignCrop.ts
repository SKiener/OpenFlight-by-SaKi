import { BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT } from './ballGuide';

/**
 * The part of the camera frame the Align picture shows, in percent of the frame: its top-left
 * corner and its size. The view box keeps the frame's own shape, so a crop of equal width and
 * height percentages works for every capture mode (640 x 400, 640 x 200, 320 x 200).
 */
export interface CropWindow {
  x: number;
  y: number;
  width: number;
  height: number;
}

/** A point in percent: of the frame, or of the view (the Align picture's box). */
export interface PctPoint {
  x: number;
  y: number;
}

/** The whole frame: the view is the frame. */
export const FULL_FRAME: CropWindow = { x: 0, y: 0, width: 100, height: 100 };

/** The Align close-up's magnification: the crop is half the frame's width and half its height. */
export const ALIGN_CROP_ZOOM = 2;

/** Where the anchor sits in the close-up, as a share of the window's height: low, with room ahead. */
export const ALIGN_CROP_ANCHOR_AT = 0.8;

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

/**
 * The crop for a zoom around an anchor (percent of the frame): centred on the anchor across, the
 * anchor at `anchorAt` of the window's height down, and moved back inside the frame at its edges.
 */
export function cropWindow(
  anchorX: number,
  anchorY: number,
  zoom: number = ALIGN_CROP_ZOOM,
  anchorAt: number = ALIGN_CROP_ANCHOR_AT
): CropWindow {
  const width = 100 / zoom;
  const height = 100 / zoom;
  return {
    x: clamp(anchorX - width / 2, 0, 100 - width),
    y: clamp(anchorY - anchorAt * height, 0, 100 - height),
    width,
    height,
  };
}

/**
 * The Align view's close-up: around the ball guide (the crosshair), the guide low in the crop so
 * the ground ahead of the ball, where the target is tapped, stays in it. With the guide at 50 % / 78 %
 * the crop covers x 25-75 % and y 38-88 % of the frame. It is fixed: it never follows a mark, so
 * the picture does not move under the finger.
 */
export const ALIGN_CROP: CropWindow = cropWindow(BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT);

/** A point in percent of the view to percent of the frame. */
export function viewToFrame(point: PctPoint, crop: CropWindow): PctPoint {
  return {
    x: crop.x + (point.x / 100) * crop.width,
    y: crop.y + (point.y / 100) * crop.height,
  };
}

/** A point in percent of the frame to percent of the view (outside 0-100 when the crop leaves it out). */
export function frameToView(point: PctPoint, crop: CropWindow): PctPoint {
  return {
    x: ((point.x - crop.x) / crop.width) * 100,
    y: ((point.y - crop.y) / crop.height) * 100,
  };
}

/**
 * Where the whole frame's box lies against the view box, in percent of the view: the picture and
 * every overlay laid out in frame percent go in this box, and the view clips what lies outside.
 */
export function cropSceneBox(crop: CropWindow): { left: string; top: string; width: string; height: string } {
  const origin = frameToView({ x: 0, y: 0 }, crop);
  return {
    left: `${origin.x}%`,
    top: `${origin.y}%`,
    width: `${(100 / crop.width) * 100}%`,
    height: `${(100 / crop.height) * 100}%`,
  };
}
