import { vi } from 'vitest';
import type { RangeScene } from '../virtual-range/scene/createRangeScene';

export function fakeRangeScene() {
  const reading: ReturnType<RangeScene['stats']> = {
    calls: 0,
    triangles: 0,
    programs: 0,
    quality: 0,
    frames: 0,
    animating: false,
  };
  const frameLog: number[] = [];
  return {
    reading,
    frameLog,
    setShots: vi.fn<RangeScene['setShots']>(),
    setAim: vi.fn<RangeScene['setAim']>(),
    setHighlight: vi.fn<RangeScene['setHighlight']>(),
    setRestTarget: vi.fn<RangeScene['setRestTarget']>(),
    setTrainingTarget: vi.fn<RangeScene['setTrainingTarget']>(),
    setOverlays: vi.fn<RangeScene['setOverlays']>(),
    setInsets: vi.fn<RangeScene['setInsets']>(),
    resize: vi.fn<RangeScene['resize']>(),
    dispose: vi.fn<RangeScene['dispose']>(),
    stats: vi.fn(() => ({ ...reading })),
    frameTimes: vi.fn(() => [...frameLog]),
  };
}
