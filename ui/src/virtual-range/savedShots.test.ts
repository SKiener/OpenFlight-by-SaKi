import { describe, expect, it } from 'vitest';
import type { SavedShot } from '../components/SessionHistoryDialog';
import { makeSavedShot } from '../test-utils/savedShot';
import type { CameraReplay, FlightPath } from '../types/shot';
import { toRangeShot, YARD_M } from './flightPath';
import { visibleRangeShots, type RangeSource } from './rangeViewModel';
import { savedSessionShots, savedShotToShot } from './savedShots';

const PATH: FlightPath = {
  unit: 'yd',
  points: [
    [0, 0, 0, 0],
    [2, 70, 2, 28],
    [4, 133, 4, 0],
  ],
};

const ROLL: FlightPath = {
  unit: 'yd',
  points: [
    [4, 133, 4, 0],
    [4.6, 138, 4.2, 0.3],
    [5.4, 142, 4.4, 0],
  ],
};

const REPLAY: CameraReplay = {
  id: 'replay-1',
  frame_count: 99,
  trigger_frame: 73,
  playback_fps: 60,
  duration_seconds: 1.65,
  display_mirror_horizontal: false,
};

const SAVED = makeSavedShot();

describe('savedShotToShot', () => {
  it('keeps carry, lateral offset and flight path of a saved shot', () => {
    const shot = savedShotToShot({ ...SAVED, carry_spin_adjusted: 133, carry_lateral_yards: 4, flight_path: PATH });

    expect(toRangeShot(shot)).toMatchObject({ carryM: 121.6152, lateralM: 3.6576 });
    expect(toRangeShot(shot)!.flight).toHaveLength(PATH.points.length);
  });

  it('keeps the apex of a saved shot; an older log has none', () => {
    expect(savedShotToShot({ ...SAVED, apex_yards: 30.2 }).apex_yards).toBe(30.2);
    expect(savedShotToShot({ ...SAVED }).apex_yards).toBeUndefined();
  });

  it('keeps a saved shot without a flight path as a landing point', () => {
    for (const flight_path of [null, undefined]) {
      const shot = savedShotToShot({ ...SAVED, carry_spin_adjusted: 133, carry_lateral_yards: 4, flight_path });

      expect(toRangeShot(shot), `flight_path ${flight_path}`).toMatchObject({
        flight: null,
        carryM: 121.6152,
        lateralM: 3.6576,
      });
    }
  });

  it('keeps the roll in saved shots', () => {
    const saved = { ...SAVED, flight_path: PATH, roll_path: ROLL, roll_yards: 9, total_yards: 142 };
    const shot = savedShotToShot(saved);

    expect(shot).toMatchObject({ roll_path: ROLL, roll_yards: 9, total_yards: 142 });
    expect(toRangeShot(shot)).toMatchObject({ totalM: 142 * YARD_M });
    expect(toRangeShot(shot)!.roll).toHaveLength(ROLL.points.length);
  });

  it('keeps a saved shot that rolled nowhere as a shot without roll', () => {
    const saved = { ...SAVED, flight_path: PATH, roll_path: null, roll_yards: null, total_yards: null };

    expect(toRangeShot(savedShotToShot(saved))).toMatchObject({ roll: null, totalM: null });
  });

  it('converts a saved shot from before the roll as it was converted then', () => {
    const range = toRangeShot(savedShotToShot({ ...SAVED, flight_path: PATH }))!;

    expect('roll' in range).toBe(false);
    expect('totalM' in range).toBe(false);
  });

  it('lands on the estimated carry when there is no spin-adjusted one, and on the target line without an offset', () => {
    const shot = savedShotToShot({ ...SAVED, carry_spin_adjusted: null, estimated_carry_yards: 140 });

    expect(toRangeShot(shot)?.carryM).toBeCloseTo(128.016, 9);
    expect(toRangeShot(shot)?.lateralM).toBe(0);
    expect(toRangeShot(savedShotToShot({ ...SAVED, carry_lateral_yards: null }))?.lateralM).toBe(0);
  });

  it('copies the saved fields and leaves the rest empty', () => {
    const saved: SavedShot = {
      ...SAVED,
      carry_lateral_yards: 4,
      flight_path: PATH,
      carry_spin_rpm: 6000,
      carry_spin_source: 'measured',
      camera_replay: REPLAY,
    };

    expect(savedShotToShot(saved)).toEqual({
      shot_number: 3,
      timestamp: '2026-10-07T18:12:04.512345',
      club: '7-iron',
      profile_id: 'p1',
      profile_name: 'Golfer',
      mode: 'rolling-buffer',
      ball_speed_mph: 112.4,
      club_speed_mph: 84.1,
      estimated_carry_yards: 140,
      carry_spin_adjusted: 125,
      carry_lateral_yards: 4,
      flight_path: PATH,
      launch_angle_vertical: 17.2,
      spin_rpm: 6200,
      carry_spin_rpm: 6000,
      carry_spin_source: 'measured',
      camera_replay: REPLAY,
      smash_factor: null,
      carry_range: [140, 140],
      peak_magnitude: null,
      launch_angle_horizontal: null,
      launch_angle_confidence: null,
      angle_source: null,
      club_angle_deg: null,
      club_path_deg: null,
      spin_axis_deg: null,
      spin_confidence: null,
      spin_quality: null,
      spin_source: null,
    });
  });

  it('reads the empty text the API sends for a missing mode, profile and spin source as absent', () => {
    const shot = savedShotToShot({ ...SAVED, mode: '', profile_id: '', carry_spin_source: '' });

    expect(shot.mode).toBeUndefined();
    expect(shot.profile_id).toBeUndefined();
    expect(shot.carry_spin_source).toBeNull();
    expect(toRangeShot(shot)).not.toBeNull();
  });

  it.each([
    ['a swing-speed mode', { mode: 'swing-speed' as const }],
    ['the club of a log that has no mode', { mode: '' as const, club: 'Swing Speed' }],
  ])('does not turn a swing-speed rep (%s) into a range shot', (_name, overrides) => {
    expect(toRangeShot(savedShotToShot({ ...SAVED, ...overrides }))).toBeNull();
  });
});

describe('savedSessionShots', () => {
  const at = (second: number, overrides: Partial<SavedShot> = {}): SavedShot =>
    makeSavedShot({ shot_number: second, timestamp: `2026-10-07T18:12:0${second}.000000`, ...overrides });
  const sourceOf = (saved: SavedShot[]): Extract<RangeSource, { kind: 'saved' }> => ({
    kind: 'saved',
    label: 'Oct 7',
    shots: savedSessionShots(saved),
  });

  it('keeps the history order, oldest first, so the range flies the last shot of the session', () => {
    const source = sourceOf([at(1), at(2), at(3)]);

    expect(source.shots.map((shot) => shot.shot_number)).toEqual([1, 2, 3]);
    expect(visibleRangeShots(source, [], '').at(-1)?.id).toBe(at(3).timestamp);
  });

  it('shows every shot whatever its profile, and lets swing-speed reps fall out', () => {
    const source = sourceOf([
      at(1, { profile_id: 'p1' }),
      at(2, { profile_id: 'p2' }),
      at(3, { profile_id: 'p3', mode: 'swing-speed' }),
    ]);

    expect(visibleRangeShots(source, [], 'p9').map((shot) => shot.id)).toEqual([at(1).timestamp, at(2).timestamp]);
  });

  it('keeps a shot that has only one of the two carries', () => {
    const shots = savedSessionShots([at(1, { carry_spin_adjusted: null }), at(2, { estimated_carry_yards: null })]);

    expect(shots.map((shot) => shot.shot_number)).toEqual([1, 2]);
  });

  it('leaves out a shot that has no carry at all, since nothing can land', () => {
    const shots = savedSessionShots([at(1), at(2, { carry_spin_adjusted: null, estimated_carry_yards: null }), at(3)]);

    expect(shots.map((shot) => shot.shot_number)).toEqual([1, 3]);
  });

  it('has nothing for an empty session', () => {
    expect(savedSessionShots([])).toEqual([]);
  });
});
