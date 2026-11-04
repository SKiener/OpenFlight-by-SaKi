import { describe, expect, it } from 'vitest';
import type { Vec3 } from '../types/tiltCalibration';
import {
  applyConvention,
  liveLabel,
  looksInverted,
  meanVector,
  pitchDeg,
  pitchStdDeg,
  recentSamples,
  smoothedPitchDeg,
  summarizePosition,
  type MotionSample,
} from './phoneTilt';

const G = 9.80665;

/** A W3C vector of a phone lying face up with its top end raised by `deg`. */
function tilted(deg: number, scale = 1): Vec3 {
  const rad = (deg * Math.PI) / 180;
  return [0, G * Math.sin(rad) * scale, G * Math.cos(rad) * scale];
}

function samples(vectors: Vec3[], stepMs = 20): MotionSample[] {
  return vectors.map((gravity, index) => ({ gravity, t: index * stepMs }));
}

describe('phone tilt math', () => {
  it('reads pitch as atan2(a_y, a_z): top end raised is positive', () => {
    expect(pitchDeg([0, 0, G])).toBe(0);
    expect(pitchDeg(tilted(2.5))).toBeCloseTo(2.5, 10);
    expect(pitchDeg(tilted(-1.2))).toBeCloseTo(-1.2, 10);
  });

  it('negates every axis for the inverted convention only', () => {
    expect(applyConvention([1, -2, 3], 'inverted')).toEqual([-1, 2, -3]);
    expect(applyConvention([1, -2, 3], 'w3c')).toEqual([1, -2, 3]);
    const inverted = applyConvention(tilted(1.5), 'inverted');
    expect(pitchDeg(applyConvention(inverted, 'inverted'))).toBeCloseTo(1.5, 10);
  });

  it('averages vectors component-wise and has no mean of nothing', () => {
    expect(meanVector([])).toBeNull();
    expect(
      meanVector([
        [1, 2, 3],
        [3, 4, 5],
      ])
    ).toEqual([2, 3, 4]);
  });

  it('measures the pitch spread as a population standard deviation', () => {
    expect(pitchStdDeg([tilted(1)], 'w3c')).toBe(0);
    expect(pitchStdDeg([tilted(1), tilted(1), tilted(1)], 'w3c')).toBeCloseTo(0, 10);
    // Pitches 0° and 2°: mean 1°, deviation 1°.
    expect(pitchStdDeg([tilted(0), tilted(2)], 'w3c')).toBeCloseTo(1, 6);
  });

  it('computes the spread after the convention flip, so an inverted phone does not wrap around 180°', () => {
    const inverted = [tilted(0.1), tilted(-0.1)].map((vector) => applyConvention(vector, 'inverted'));
    expect(pitchStdDeg(inverted, 'inverted')).toBeCloseTo(0.1, 6);
    expect(pitchStdDeg(inverted, 'w3c')).toBeGreaterThan(100);
  });

  it('labels the live pitch higher, lower or level (rounds to 0.0°)', () => {
    expect(liveLabel(0.3)).toBe('higher');
    expect(liveLabel(-0.3)).toBe('lower');
    expect(liveLabel(0.04)).toBe('level');
    expect(liveLabel(-0.04)).toBe('level');
    expect(liveLabel(0.05)).toBe('higher');
  });

  it('smooths the live value over the last 0.5 s only', () => {
    const history = [
      ...samples([tilted(10), tilted(10)], 100),
      ...[1, 2, 3, 4, 5, 6].map((step) => ({ gravity: tilted(2), t: 700 + step * 100 })),
    ];
    expect(recentSamples(history).every((sample) => sample.t >= 800)).toBe(true);
    expect(smoothedPitchDeg(history, 'w3c')).toBeCloseTo(2, 6);
    expect(smoothedPitchDeg([], 'w3c')).toBeNull();
  });

  it('spots a flat phone that reports its axes negated', () => {
    expect(looksInverted([0, 0, -G])).toBe(true);
    expect(looksInverted(applyConvention(tilted(3), 'inverted'))).toBe(true);
    expect(looksInverted([0, 0, G])).toBe(false);
    // On edge (z near 0) is not "inverted", it is just not flat.
    expect(looksInverted([0, G, 0])).toBe(false);
    expect(looksInverted([0, 0, 0])).toBe(false);
  });

  it('summarizes a position as the reported mean, the pitch std and the count', () => {
    expect(summarizePosition([], 'w3c')).toBeNull();
    const position = summarizePosition(samples([tilted(1), tilted(3)]), 'w3c');
    expect(position?.sample_count).toBe(2);
    expect(position?.pitch_std_deg).toBeCloseTo(1, 6);
    expect(pitchDeg(position!.gravity)).toBeCloseTo(2, 2);

    // The mean is sent as reported; the server applies the convention.
    const inverted = summarizePosition(samples([applyConvention(tilted(1), 'inverted')]), 'inverted');
    expect(inverted?.gravity[2]).toBeLessThan(0);
  });
});
