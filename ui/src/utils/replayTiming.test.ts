import { describe, expect, it } from 'vitest';
import { getReplayStartTime, getReplayPlaybackRate } from './replayTiming';

const replay = { trigger_frame: 44, playback_fps: 60, duration_seconds: 1 };

describe('real-world replay speed', () => {
  it('plays 300 fps recorded at 60 fps at 0.5 media speed for tenfold slow motion', () => {
    expect(getReplayPlaybackRate({ playback_fps: 60, capture_fps: 300 }, 0.1)).toBe(0.5);
  });
  it('uses measured rather than nominal capture speed', () => {
    expect(getReplayPlaybackRate({ playback_fps: 60, capture_fps: 288 }, 0.1)).toBeCloseTo(0.48);
    expect(getReplayPlaybackRate({ playback_fps: 60, capture_fps: 288 }, 1)).toBeCloseTo(4.8);
  });
  it.each([288, 479.5, 576, 600])('preserves twentyfold and fortyfold slowdown at %s fps', (capture_fps) => {
    for (const speed of [0.05, 0.025]) {
      const metadata = {
        ...replay,
        capture_fps,
        trigger_frame: Math.round(capture_fps * 0.15),
        duration_seconds: Math.ceil(capture_fps * 0.2) / 60,
      };
      const mediaRate = getReplayPlaybackRate(metadata, speed);
      expect((mediaRate * 60) / capture_fps).toBeCloseTo(speed);
      expect((metadata.trigger_frame / 60 - getReplayStartTime(metadata, speed)) / mediaRate).toBeCloseTo(1.5);
    }
  });
  it.each([undefined, null, 0, -1, Number.NaN])(
    'preserves legacy playback for unknown capture fps %s',
    (capture_fps) => {
      expect(getReplayPlaybackRate({ playback_fps: 60, capture_fps }, 0.1)).toBe(0.1);
    }
  );
});

describe('getReplayStartTime', () => {
  it('keeps 1.5 seconds before impact after correcting for the actual capture fps', () => {
    const measured = { ...replay, capture_fps: 288 };
    const start = getReplayStartTime(measured, 0.1);
    expect((44 / 60 - start) / getReplayPlaybackRate(measured, 0.1)).toBeCloseTo(1.5);
    expect(getReplayStartTime(measured, 1)).toBe(0);
  });
  it('leaves the entire clip available at normal playback speed', () => {
    expect(getReplayStartTime(replay, 1)).toBe(0);
  });

  it('skips about 5.8 seconds of waiting at 0.1×, keeping 1.5 seconds before impact', () => {
    const start = getReplayStartTime(replay, 0.1);
    expect(start).toBeCloseTo(0.5833333333);
    expect((44 / 60 - start) / 0.1).toBeCloseTo(1.5);
    expect(start / 0.1).toBeCloseTo(5.8333333333);
  });

  it('scales the lead-in for quarter speed instead of skipping a fixed number of frames', () => {
    expect(getReplayStartTime(replay, 0.25)).toBeCloseTo(44 / 60 - 0.375);
  });

  it('does not skip a lead-in that is already short enough', () => {
    expect(getReplayStartTime(replay, 0.5)).toBe(0);
    expect(getReplayStartTime({ ...replay, trigger_frame: 2 }, 0.1)).toBe(0);
  });

  it('uses each recording’s trigger and playback frame rate', () => {
    expect(getReplayStartTime({ trigger_frame: 73, playback_fps: 60, duration_seconds: 1.65 }, 0.1)).toBeCloseTo(
      73 / 60 - 0.15
    );
    expect(getReplayStartTime({ trigger_frame: 22, playback_fps: 30, duration_seconds: 1 }, 0.1)).toBeCloseTo(
      22 / 30 - 0.15
    );
  });

  it.each([
    { ...replay, playback_fps: 0 },
    { ...replay, trigger_frame: -1 },
    { ...replay, trigger_frame: 80 },
    { ...replay, trigger_frame: Number.NaN },
    { ...replay, duration_seconds: 0 },
    { ...replay, duration_seconds: Number.NaN },
  ])('falls back to the full clip for invalid timing: %j', (metadata) => {
    expect(getReplayStartTime(metadata, 0.1)).toBe(0);
  });
});
