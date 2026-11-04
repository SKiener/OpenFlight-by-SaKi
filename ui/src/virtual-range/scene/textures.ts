import {
  CanvasTexture,
  DataTexture,
  LinearFilter,
  LinearMipmapLinearFilter,
  RGBAFormat,
  RepeatWrapping,
  SRGBColorSpace,
  Sprite,
  SpriteMaterial,
} from 'three';
import { DISPLAY_FONT_STACK, displayFontPending } from '../../theme/fonts';

export type Random = () => number;

export function seededRandom(seed: number): Random {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const between = (rand: Random, min: number, max: number): number => min + (max - min) * rand();

type Octave = [cells: number, amplitude: number];

function valueNoise(rand: Random, size: number, octaves: Octave[]): Float32Array {
  const grids = octaves.map(([cells]) => Float32Array.from({ length: cells * cells }, () => rand()));
  const out = new Float32Array(size * size);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let value = 0;
      for (let o = 0; o < octaves.length; o++) {
        const cells = octaves[o][0];
        const grid = grids[o];
        const fx = (x / size) * cells;
        const fy = (y / size) * cells;
        const x0 = Math.floor(fx);
        const y0 = Math.floor(fy);
        const sx = (fx - x0) ** 2 * (3 - 2 * (fx - x0));
        const sy = (fy - y0) ** 2 * (3 - 2 * (fy - y0));
        const row0 = (y0 % cells) * cells;
        const row1 = ((y0 + 1) % cells) * cells;
        const left = x0 % cells;
        const right = (x0 + 1) % cells;
        const top = grid[row0 + left] + (grid[row0 + right] - grid[row0 + left]) * sx;
        const bottom = grid[row1 + left] + (grid[row1 + right] - grid[row1 + left]) * sx;
        value += (top + (bottom - top) * sy) * octaves[o][1];
      }
      out[y * size + x] = value;
    }
  }
  return out;
}

/** Tileable noise: coarse patches in red, mid detail in green, fine grain in blue. */
export function makeNoiseTexture(rand: Random, size = 256): DataTexture {
  const red = valueNoise(rand, size, [
    [4, 0.45],
    [8, 0.3],
    [16, 0.15],
    [32, 0.1],
  ]);
  const green = valueNoise(rand, size, [
    [16, 0.5],
    [32, 0.3],
    [64, 0.2],
  ]);
  const blue = valueNoise(rand, size, [
    [64, 0.5],
    [128, 0.35],
    [256, 0.15],
  ]);
  const data = new Uint8Array(size * size * 4);
  for (let i = 0; i < size * size; i++) {
    data[i * 4] = red[i] * 255;
    data[i * 4 + 1] = green[i] * 255;
    data[i * 4 + 2] = blue[i] * 255;
    data[i * 4 + 3] = 255;
  }
  const texture = new DataTexture(data, size, size, RGBAFormat);
  texture.wrapS = texture.wrapT = RepeatWrapping;
  texture.magFilter = LinearFilter;
  texture.minFilter = LinearMipmapLinearFilter;
  texture.generateMipmaps = true;
  texture.needsUpdate = true;
  return texture;
}

export function radialTexture(stops: readonly [offset: number, css: string][]): CanvasTexture {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext('2d');
  if (ctx) {
    const gradient = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    for (const [offset, css] of stops) gradient.addColorStop(offset, css);
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 64, 64);
  }
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  return texture;
}

/** The display face (a•space), as for the numbers and headings on screen. */
const SIGN_FONT = DISPLAY_FONT_STACK;

/** `aspect` is height / width, as the probe used it. */
export interface Sign {
  readonly sprite: Sprite;
  readonly aspect: number;
  draw(text: string, accent: string, outlined?: boolean): void;
}

function paintSign(canvas: HTMLCanvasElement, text: string, accent: string, outlined: boolean): void {
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const { width, height } = canvas;
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = 'rgba(10, 16, 12, 0.78)';
  ctx.beginPath();
  ctx.roundRect(4, 4, width - 8, height - 8, 22);
  ctx.fill();
  ctx.fillStyle = accent;
  ctx.beginPath();
  ctx.roundRect(4, 4, 14, height - 8, 7);
  ctx.fill();
  if (outlined) {
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.roundRect(8, 8, width - 16, height - 16, 18);
    ctx.stroke();
  }

  const roomPx = width - 44;
  let fontPx = Math.round(height * 0.52);
  ctx.font = `700 ${fontPx}px ${SIGN_FONT}`;
  const textPx = ctx.measureText(text).width;
  if (textPx > roomPx) {
    fontPx = Math.floor((fontPx * roomPx) / textPx);
    ctx.font = `700 ${fontPx}px ${SIGN_FONT}`;
  }
  ctx.fillStyle = '#ffffff';
  ctx.textBaseline = 'middle';
  ctx.textAlign = 'center';
  ctx.fillText(text, width / 2 + 6, height / 2 + 2);
}

export function createSign(width: number, height: number): Sign {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const texture = new CanvasTexture(canvas);
  texture.colorSpace = SRGBColorSpace;
  texture.minFilter = LinearFilter;
  texture.generateMipmaps = false;
  const sprite = new Sprite(new SpriteMaterial({ map: texture, transparent: true, depthWrite: false, fog: false }));
  let last: [text: string, accent: string, outlined: boolean] | null = null;
  let awaitingFont = false;
  const paint = (): void => {
    if (!last) return;
    paintSign(canvas, ...last);
    texture.needsUpdate = true;
  };
  return {
    sprite,
    aspect: height / width,
    draw(text, accent, outlined = false) {
      last = [text, accent, outlined];
      paint();
      // Drawn before a•space loaded, the sign keeps the fallback face: paint
      // the latest label once more when it is ready (the scene renders on demand
      // and asks for that frame itself, see createRangeScene).
      if (awaitingFont) return;
      const pending = displayFontPending();
      if (!pending) return;
      awaitingFont = true;
      void pending.then(() => {
        awaitingFont = false;
        paint();
      });
    },
  };
}
