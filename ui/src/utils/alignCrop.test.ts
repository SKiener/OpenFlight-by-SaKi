import { describe, expect, it } from 'vitest';
import { BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT } from './ballGuide';
import { ALIGN_CROP, cropSceneBox, cropWindow, frameToView, FULL_FRAME, viewToFrame } from './alignCrop';

describe('the Align close-up window', () => {
  it('shows half the frame around the ball guide, the guide at 80 % of its height', () => {
    expect([BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT]).toEqual([50, 78]);
    expect(ALIGN_CROP).toEqual({ x: 25, y: 38, width: 50, height: 50 });
    expect(frameToView({ x: BALL_GUIDE_X_PCT, y: BALL_GUIDE_Y_PCT }, ALIGN_CROP)).toEqual({ x: 50, y: 80 });
  });

  it('holds the targets tapped at the range (x 54-69 %, y 42-54 % of the frame)', () => {
    for (const point of [
      { x: 54, y: 42 },
      { x: 69, y: 54 },
    ]) {
      const view = frameToView(point, ALIGN_CROP);
      expect(view.x).toBeGreaterThan(0);
      expect(view.x).toBeLessThan(100);
      expect(view.y).toBeGreaterThan(0);
      expect(view.y).toBeLessThan(100);
    }
  });

  it('stays inside the frame with a guide near the bottom and right edges', () => {
    expect(cropWindow(95, 97)).toEqual({ x: 50, y: 50, width: 50, height: 50 });
    expect(cropWindow(100, 100)).toEqual({ x: 50, y: 50, width: 50, height: 50 });
  });

  it('stays inside the frame with a guide near the top and left edges', () => {
    expect(cropWindow(5, 10)).toEqual({ x: 0, y: 0, width: 50, height: 50 });
  });

  it('takes the zoom and the anchor height', () => {
    expect(cropWindow(50, 50, 4, 0.5)).toEqual({ x: 37.5, y: 37.5, width: 25, height: 25 });
    expect(cropWindow(50, 78, 1)).toEqual(FULL_FRAME);
  });

  it('keeps the frame shape in every capture mode (640 x 400, 640 x 200, 320 x 200)', () => {
    for (const [width, height] of [
      [640, 400],
      [640, 200],
      [320, 200],
    ]) {
      const windowWidthPx = (ALIGN_CROP.width / 100) * width;
      const windowHeightPx = (ALIGN_CROP.height / 100) * height;
      expect(windowWidthPx / windowHeightPx).toBeCloseTo(width / height, 10);
      // The window centre in pixels: the same percent maps to each mode's own pixels.
      const centre = viewToFrame({ x: 50, y: 50 }, ALIGN_CROP);
      expect([(centre.x / 100) * width, (centre.y / 100) * height]).toEqual([0.5 * width, 0.63 * height]);
    }
  });
});

describe('view and frame percent', () => {
  it('maps the view centre to the window centre', () => {
    expect(viewToFrame({ x: 50, y: 50 }, ALIGN_CROP)).toEqual({ x: 50, y: 63 });
  });

  it('round-trips view -> frame -> view and frame -> view -> frame', () => {
    for (const point of [
      { x: 0, y: 0 },
      { x: 100, y: 100 },
      { x: 12.345, y: 87.65 },
      { x: 50, y: 80 },
    ]) {
      const there = frameToView(viewToFrame(point, ALIGN_CROP), ALIGN_CROP);
      expect(there.x).toBeCloseTo(point.x, 10);
      expect(there.y).toBeCloseTo(point.y, 10);
      const back = viewToFrame(frameToView(point, ALIGN_CROP), ALIGN_CROP);
      expect(back.x).toBeCloseTo(point.x, 10);
      expect(back.y).toBeCloseTo(point.y, 10);
    }
  });

  it('puts a frame point outside the window outside the view', () => {
    const corner = frameToView({ x: 0, y: 0 }, ALIGN_CROP);
    expect(corner).toEqual({ x: -50, y: -76 });
  });

  it('is the identity for the whole frame', () => {
    expect(viewToFrame({ x: 30, y: 70 }, FULL_FRAME)).toEqual({ x: 30, y: 70 });
    expect(frameToView({ x: 30, y: 70 }, FULL_FRAME)).toEqual({ x: 30, y: 70 });
  });
});

describe('the scene box', () => {
  it('lays the whole frame twice as large, shifted so the window fills the view', () => {
    expect(cropSceneBox(ALIGN_CROP)).toEqual({ left: '-50%', top: '-76%', width: '200%', height: '200%' });
  });

  it('is the view itself for the whole frame', () => {
    expect(cropSceneBox(FULL_FRAME)).toEqual({ left: '0%', top: '0%', width: '100%', height: '100%' });
  });
});
