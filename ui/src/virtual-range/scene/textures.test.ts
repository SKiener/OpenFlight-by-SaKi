import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DISPLAY_FONT_PROBES, DISPLAY_FONT_STACK } from '../../theme/fonts';
import { createSign } from './textures';

interface FakeContext {
  font: string;
  fills: [text: string, font: string][];
}

/** Just enough of a 2D context for paintSign; it records the font of every fillText. */
function fakeContext(): FakeContext & Record<string, unknown> {
  const ctx: FakeContext & Record<string, unknown> = {
    font: '',
    fills: [],
    clearRect: () => {},
    beginPath: () => {},
    roundRect: () => {},
    fill: () => {},
    stroke: () => {},
    measureText: (text: string) => ({ width: text.length * 10 }),
    fillText(text: string) {
      ctx.fills.push([text, ctx.font]);
    },
  };
  return ctx;
}

let ctx: ReturnType<typeof fakeContext>;
let loaded: boolean;
let resolveLoad: () => void;
let fonts: { check: ReturnType<typeof vi.fn>; load: ReturnType<typeof vi.fn> };

beforeEach(() => {
  ctx = fakeContext();
  loaded = false;
  fonts = {
    check: vi.fn(() => loaded),
    load: vi.fn(
      () =>
        new Promise<FontFace[]>((resolve) => {
          resolveLoad = () => {
            loaded = true;
            resolve([]);
          };
        }),
    ),
  };
  vi.stubGlobal('document', {
    fonts,
    createElement: () => ({ width: 0, height: 0, getContext: () => ctx }),
  });
});

afterEach(() => vi.unstubAllGlobals());

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('range signs', () => {
  it('draw with the display face (a•space first, not Nexa)', () => {
    loaded = true;
    createSign(256, 112).draw('150 m', '#00ff78');
    expect(ctx.fills).toHaveLength(1);
    expect(ctx.fills[0][1]).toMatch(/^700 \d+px /);
    expect(ctx.fills[0][1].replace(/^700 \d+px /, '')).toBe(DISPLAY_FONT_STACK);
    expect(ctx.fills[0][1]).not.toContain('Nexa');
  });

  it('wait for a•space, the face they draw with', () => {
    createSign(256, 112).draw('150 m', '#00ff78');
    expect(fonts.check).toHaveBeenCalledWith(DISPLAY_FONT_PROBES[0]);
    expect(fonts.load).toHaveBeenCalledWith(DISPLAY_FONT_PROBES[0]);
    expect(DISPLAY_FONT_PROBES.every((probe) => probe.includes("'A-Space'"))).toBe(true);
  });

  it('do not wait when a•space is already loaded', () => {
    loaded = true;
    createSign(256, 112).draw('150 m', '#00ff78');
    expect(fonts.load).not.toHaveBeenCalled();
  });

  it('paint the latest label again once a•space has loaded', async () => {
    const sign = createSign(256, 112);
    const texture = (sign.sprite.material as { map: { version: number } }).map;
    sign.draw('100 m', '#00ff78');
    sign.draw('150 m', '#00ff78', true);
    expect(ctx.fills.map(([text]) => text)).toEqual(['100 m', '150 m']);
    // One wait per sign, however often it is drawn meanwhile.
    expect(fonts.load).toHaveBeenCalledTimes(1);
    const version = texture.version;

    resolveLoad();
    await flush();

    expect(ctx.fills.map(([text]) => text)).toEqual(['100 m', '150 m', '150 m']);
    expect(texture.version).toBeGreaterThan(version);
  });

  it('do not repaint again after the font is ready', async () => {
    const sign = createSign(256, 112);
    sign.draw('100 m', '#00ff78');
    resolveLoad();
    await flush();
    sign.draw('120 m', '#00ff78');
    await flush();
    expect(ctx.fills.map(([text]) => text)).toEqual(['100 m', '100 m', '120 m']);
    expect(fonts.load).toHaveBeenCalledTimes(1);
  });
});
