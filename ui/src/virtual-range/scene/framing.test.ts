import { PerspectiveCamera, Vector3 } from 'three';
import { describe, expect, it } from 'vitest';
import { NO_INSETS, type RangeInsets } from '../insets';
import { chromeAreas, frameCamera } from './framing';

const WIDTH = 800;
const HEIGHT = 400;
const WITH_BAND: RangeInsets = { top: 104, bottom: 72 };

function camera(): PerspectiveCamera {
  const view = new PerspectiveCamera(46, 1, 0.5, 6000);
  view.position.set(0, 0, 10);
  view.lookAt(0, 0, 0);
  view.updateMatrixWorld();
  return view;
}

/** Where a point lands on a WIDTH × HEIGHT canvas. */
function onScreen(view: PerspectiveCamera, x: number, y: number, z: number): { x: number; y: number } {
  const ndc = new Vector3(x, y, z).project(view);
  return { x: ((ndc.x + 1) / 2) * WIDTH, y: ((1 - ndc.y) / 2) * HEIGHT };
}

describe('frameCamera', () => {
  it('fits the canvas and puts the middle of the view between the chrome', () => {
    const view = camera();
    frameCamera(view, WIDTH, HEIGHT, WITH_BAND);

    expect(view.aspect).toBe(2);
    expect(onScreen(view, 0, 0, 0).x).toBeCloseTo(WIDTH / 2, 6);
    expect(onScreen(view, 0, 0, 0).y).toBeCloseTo((104 + (HEIGHT - 72)) / 2, 6);
  });

  it('only moves the picture: sizes on screen stay as without chrome', () => {
    const framed = camera();
    const plain = camera();
    frameCamera(framed, WIDTH, HEIGHT, WITH_BAND);
    frameCamera(plain, WIDTH, HEIGHT, NO_INSETS);

    const height = (view: PerspectiveCamera) => onScreen(view, 0, 0, 0).y - onScreen(view, 0, 1, 0).y;
    expect(height(framed)).toBeCloseTo(height(plain), 6);
    expect(height(plain)).toBeCloseTo(HEIGHT / 2 / 10 / Math.tan((23 * Math.PI) / 180), 6);
  });

  it('centres the view on the canvas without chrome, also after there was some', () => {
    const view = camera();
    frameCamera(view, WIDTH, HEIGHT, NO_INSETS);
    expect(onScreen(view, 0, 0, 0).y).toBeCloseTo(HEIGHT / 2, 6);
    expect(view.view?.enabled ?? false).toBe(false);

    frameCamera(view, WIDTH, HEIGHT, WITH_BAND);
    frameCamera(view, WIDTH, HEIGHT, { top: 58, bottom: 58 });
    expect(onScreen(view, 0, 0, 0).y).toBeCloseTo(HEIGHT / 2, 6);
  });

  it('follows a canvas that changes size', () => {
    const view = camera();
    frameCamera(view, WIDTH, HEIGHT, WITH_BAND);
    frameCamera(view, 1024, 600, { top: 76, bottom: 90 });

    const ndc = new Vector3(0, 0, 0).project(view);
    expect(view.aspect).toBeCloseTo(1024 / 600, 9);
    expect(((1 - ndc.y) / 2) * 600).toBeCloseTo((76 + 600 - 90) / 2, 6);
  });

  it.each([
    [{ top: Number.NaN, bottom: 72 }, (HEIGHT - 72) / 2],
    [{ top: -20, bottom: 0 }, HEIGHT / 2],
    [{ top: Number.POSITIVE_INFINITY, bottom: 0 }, HEIGHT / 2],
  ])('counts chrome of %j as none at that edge', (insets, middle) => {
    const view = camera();
    frameCamera(view, WIDTH, HEIGHT, insets);
    expect(onScreen(view, 0, 0, 0).y).toBeCloseTo(middle, 6);
  });
});

describe('chromeAreas', () => {
  it('is the canvas strip under each bar', () => {
    expect(chromeAreas(WIDTH, HEIGHT, WITH_BAND)).toEqual([
      [0, 0, WIDTH, 104],
      [0, HEIGHT - 72, WIDTH, HEIGHT],
    ]);
  });

  it('leaves out an edge without chrome', () => {
    expect(chromeAreas(WIDTH, HEIGHT, NO_INSETS)).toEqual([]);
    expect(chromeAreas(WIDTH, HEIGHT, { top: 0, bottom: 72 })).toEqual([[0, HEIGHT - 72, WIDTH, HEIGHT]]);
    expect(chromeAreas(WIDTH, HEIGHT, { top: 58, bottom: Number.NaN })).toEqual([[0, 0, WIDTH, 58]]);
  });
});
