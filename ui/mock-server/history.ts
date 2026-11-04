/**
 * Saved sessions for the companion's 7/30-day dispersion, aged relative to mock start.
 */

import type { CameraReplay } from '../src/types/shot.js';

const DAY_MS = 86_400_000;
const CLUBS: Record<string, { speed: number; launch: number; spin: number }> = {
  'wedge-54': { speed: 78, launch: 30, spin: 9000 },
  '7-iron': { speed: 112, launch: 17, spin: 6500 },
  driver: { speed: 150, launch: 12, spin: 2700 },
};

type Sample = [club: string, carryYards: number, horizontalDeg: number | null, extra?: Record<string, unknown>];

function replay(id: string): CameraReplay {
  return {
    id,
    frame_count: 99,
    trigger_frame: 73,
    playback_fps: 60,
    duration_seconds: 1.65,
    display_mirror_horizontal: true,
  };
}

function savedSession(daysAgo: number, samples: Sample[]) {
  const start = Date.now() - daysAgo * DAY_MS;
  const shots = samples.map(([club, carry, horizontal, extra], index) => {
    const { speed, launch, spin } = CLUBS[club]!;
    return {
      shot_number: index + 1,
      timestamp: new Date(start + index * 60_000).toISOString(),
      club,
      profile_id: 'mock-profile-1',
      profile_name: 'Profile 1',
      mode: 'rolling-buffer',
      ball_speed_mph: speed,
      club_speed_mph: null,
      estimated_carry_yards: carry,
      carry_spin_adjusted: carry,
      carry_spin_source: 'measured',
      carry_lateral_yards: horizontal === null ? null : Math.round(carry * Math.sin((horizontal * Math.PI) / 180)),
      launch_angle_vertical: launch,
      launch_angle_horizontal: horizontal,
      launch_angle_horizontal_confidence: horizontal === null ? null : 0.8,
      launch_angle_horizontal_source: horizontal === null ? '' : 'radar',
      spin_rpm: spin,
      spin_quality: 'high',
      spin_confidence: 0.9,
      spin_source: 'measured',
      ...extra,
    };
  });
  return {
    id: `session_mock_${daysAgo}d`,
    started_at: new Date(start).toISOString(),
    shot_count: shots.length,
    clubs: [...new Set(shots.map((shot) => shot.club))],
    shots,
  };
}

export const MOCK_HISTORY = [
  savedSession(3, [
    ['7-iron', 152, -2.5, { camera_replay: replay('mock-history-7i') }],
    ['7-iron', 158, 1.0],
    ['7-iron', 148, -4.0, { carry_spin_source: 'club_typical' }],
    ['7-iron', 161, 2.0],
    ['7-iron', 155, -0.5],
    ['7-iron', 150, 3.0, { carry_lateral_yards: null }],
    ['7-iron', 149, null],
    ['wedge-54', 88, 1.5, { carry_spin_source: 'club_typical' }],
    ['wedge-54', 92, -1.0],
  ]),
  savedSession(12, [
    ['driver', 228, -3.0],
    ['driver', 241, 2.5],
    ['driver', 235, -1.0],
    ['driver', 246, 4.0],
    ['driver', 232, 0.5],
    ['driver', 250, -9.0, { camera_replay: replay('expired-driver') }],
  ]),
];
