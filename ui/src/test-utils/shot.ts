import type { Shot } from '../types/shot';

export function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 92,
    club_speed_mph: 68,
    smash_factor: 1.35,
    estimated_carry_yards: 210,
    carry_range: [205, 215],
    club: 'driver',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13.4,
    launch_angle_horizontal: -1.2,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: 2.1,
    club_path_deg: -0.6,
    spin_axis_deg: 3.4,
    spin_rpm: 2650,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: 214,
    carry_spin_rpm: 2650,
    carry_spin_source: 'measured',
    ...overrides,
  };
}

/**
 * What the server still computes, logs and sends, but no screen may show: this
 * hardware cannot measure it (the lofts, the face, face to path, the D-plane spin
 * axis, and the heel/toe impact under its old name impact_toe_mm). Spread it into a
 * shot to check a screen leaves it out. The low point is in here too: it shows on the
 * club page only ({@link LOW_POINT_COPY}). The impact point itself (impact_offset_mm)
 * shows only on the club page and the kiosk's impact tile: see {@link IMPACT_COPY}.
 */
export const UNMEASURABLE_CLUB_DATA = {
  spin_axis_source: 'calculated',
  camera_low_point_cm: 12.3,
  camera_low_point_sigma_cm: 2.1,
  dynamic_loft_deg: 37.7,
  dynamic_loft_sigma_deg: 0.7,
  spin_loft_deg: 52.7,
  spin_loft_sigma_deg: 1.5,
  face_to_target_deg: -12.2,
  face_to_target_sigma_deg: 0.96,
  face_to_path_deg: -19.1,
  face_to_path_sigma_deg: 1.6,
  face_to_path_class: 'closed',
  calculated_spin_axis_deg: -1.2,
  calculated_spin_axis_sigma_deg: 1.9,
  spin_axis_class: 'draw',
  impact_toe_mm: -9,
  impact_sigma_mm: 4.5,
} as const;

/** The titles and words those values had on screen, per locale; no screen may render them. */
export const UNMEASURABLE_COPY = {
  en: [
    'Spin axis',
    'Spin loft',
    'Dyn. loft',
    'Face angle',
    'Face to path',
    'Low point',
    'Avg face',
    'Closed',
    'Heel',
    'Draw',
  ],
  de: [
    'Spinachse',
    'Spin-Loft',
    'Dyn. Loft',
    'Schlagfläche',
    'Fläche zu Pfad',
    'Treffpunkt',
    'Tiefpunkt',
    'Achse',
    'Geschlossen',
    'Ferse',
    'Draw',
  ],
  es: [
    'Eje de spin',
    'Loft dinámico',
    'Spin loft',
    'Ángulo de cara',
    'Cara a path',
    'Punto bajo',
    'Media cara',
    'Media eje spin',
    'Cerrada',
    'Talón',
    'Draw',
  ],
} as const;

/**
 * The words of {@link UNMEASURABLE_COPY} the impact point now uses (its title and the
 * heel zone word): allowed on the club page and the kiosk's impact tile, still on no
 * other screen.
 */
export const IMPACT_COPY = {
  en: ['Heel'],
  de: ['Treffpunkt', 'Ferse'],
  es: ['Talón'],
} as const;

/**
 * The low point's title: the club page shows it (decided 2026-11-01), no
 * other screen does.
 */
export const LOW_POINT_COPY = {
  en: ['Low point'],
  de: ['Tiefpunkt'],
  es: ['Punto bajo'],
} as const;

/** {@link UNMEASURABLE_COPY} for the club page: everything but the impact point's and the low point's own words. */
export function unmeasurableClubPageCopy(locale: keyof typeof UNMEASURABLE_COPY): string[] {
  const allowed: readonly string[] = [...IMPACT_COPY[locale], ...LOW_POINT_COPY[locale]];
  return UNMEASURABLE_COPY[locale].filter((word) => !allowed.includes(word));
}
