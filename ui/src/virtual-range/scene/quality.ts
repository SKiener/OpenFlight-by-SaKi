export type QualityLevel = 0 | 1 | 2;

export const QUALITY_STORAGE_KEY = 'openflight.virtual-range.quality:v1';

const WINDOW_MS = 2000;
const MIN_WINDOW_FRAMES = 10;
const SLOW_FRAME_MS = 25;

function median(values: readonly number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = sorted.length >> 1;
  return sorted.length % 2 === 1 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export class QualityMonitor {
  private currentLevel: QualityLevel;
  private frames: number[] = [];
  private windowMs = 0;

  constructor(level: QualityLevel) {
    this.currentLevel = level;
  }

  get level(): QualityLevel {
    return this.currentLevel;
  }

  // Rendered time, not wall clock (the scene idles between flights); the frame floor stops one hitch closing a window.
  record(frameMs: number): QualityLevel | null {
    if (this.currentLevel === 2 || !Number.isFinite(frameMs) || frameMs <= 0) return null;
    this.frames.push(frameMs);
    this.windowMs += frameMs;
    if (this.windowMs < WINDOW_MS || this.frames.length < MIN_WINDOW_FRAMES) return null;

    const tooSlow = median(this.frames) > SLOW_FRAME_MS;
    this.frames = [];
    this.windowMs = 0;
    if (!tooSlow) return null;
    this.currentLevel = this.currentLevel === 0 ? 1 : 2;
    return this.currentLevel;
  }
}

export function loadQuality(storage: Pick<Storage, 'getItem'> | null): QualityLevel {
  try {
    const stored = storage?.getItem(QUALITY_STORAGE_KEY);
    return stored === '1' ? 1 : stored === '2' ? 2 : 0;
  } catch {
    return 0;
  }
}

export function saveQuality(storage: Pick<Storage, 'setItem'> | null, level: QualityLevel): void {
  try {
    storage?.setItem(QUALITY_STORAGE_KEY, String(level));
  } catch {
    // Quota or blocked storage: the level then lasts only as long as this scene.
  }
}
