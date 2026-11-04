export type SpinQuality = 'high' | 'medium' | 'low' | 'experimental' | 'withheld';
export type ClubDataStatus = 'pending' | 'done' | 'skipped';
/** Heel/toe class of the impact point (club_data.impact_zone), right-handed. */
export type ImpactZone = 'heel' | 'center' | 'toe';
/** Which camera method gave the impact point: D measured across contact, E extrapolated (fallback). */
export type ImpactMethod = 'D' | 'E';
/** Curve shape of the drawn flight (dplane.CURVE_CLASSES), right-handed: draw and hook curve left. */
export type CurveClass = 'hook' | 'draw' | 'straight' | 'fade' | 'slice';

export interface CameraReplay {
  id: string;
  frame_count: number;
  trigger_frame: number;
  playback_fps: number;
  capture_fps?: number | null;
  duration_seconds: number;
  display_mirror_horizontal: boolean;
}

export type FlightPathPoint = [t: number, x: number, y: number, z: number];

/** A simulated flight: t in seconds, x downrange, y right, z up, all in yards. */
export interface FlightPath {
  unit: 'yd';
  points: FlightPathPoint[];
}

export interface Shot {
  mode?: 'rolling-buffer' | 'mock' | 'swing-speed';
  shot_number?: number | null;
  ball_speed_mph: number;
  /**
   * 'uncertain': the OPS ball-track guard kept a ball speed that reads like the
   * club head's trailing track (no replacement found). Its ball speed, smash and
   * carry tiles are marked estimated. Absent on older servers and saved shots.
   */
  ball_speed_status?: 'uncertain' | null;
  /** The guard rule that fired ('club_like_track', 'smash_implausible'), also for a replaced ball speed. */
  ball_speed_status_reason?: string | null;
  club_speed_mph: number | null;
  smash_factor: number | null;
  estimated_carry_yards: number;
  carry_range: [number, number];
  club: string;
  profile_id?: string;
  profile_name?: string;
  timestamp: string;
  impact_timestamp?: number | null;
  peak_magnitude: number | null;
  // Launch angle data (from K-LD7 radar (deprecated), camera, or estimation)
  launch_angle_vertical: number | null;
  launch_angle_horizontal: number | null;
  launch_angle_confidence: number | null;
  launch_angle_vertical_confidence?: number | null;
  launch_angle_horizontal_confidence?: number | null;
  launch_angle_vertical_source?: string | null;
  launch_angle_horizontal_source?: string | null;
  angle_source: 'radar' | 'camera' | 'estimated' | null;
  club_angle_deg: number | null;
  club_path_deg: number | null;
  experimental_attack_angle_deg?: number | null;
  experimental_attack_angle_status?: string | null;
  experimental_club_path_deg?: number | null;
  experimental_club_path_status?: string | null;
  // Chained camera club delivery: session log only, the server sends null here.
  experimental_fused_attack_angle_deg?: number | null;
  experimental_fused_club_path_deg?: number | null;
  experimental_fused_status?: string | null;
  experimental_fused_attack_angle_confidence?: 'high' | 'medium' | 'low' | 'withheld' | null;
  experimental_fused_club_path_confidence?: 'high' | 'medium' | 'low' | 'withheld' | null;
  experimental_camera_trace_deg?: number | null;
  experimental_aoa_offset_source?: string | null;
  iwr6843_horizontal_deg?: number | null;
  iwr6843_horizontal_confidence?: number | null;
  experimental_camera_horizontal_deg?: number | null;
  experimental_camera_horizontal_confidence?: number | null;
  experimental_camera_horizontal_status?: string | null;
  experimental_camera_iwr_delta_deg?: number | null;
  /** The axis the carry was flown with. Never shown; without one the carry notes read "Curve not considered". */
  spin_axis_deg: number | null;
  /**
   * The curve shape of that axis as a class word: set by the server from the same
   * calculated D-plane axis, so it always matches the drawn curve; null for woods
   * and without a calculated axis. Absent on older servers and saved shots.
   */
  curve_class?: CurveClass | null;
  /**
   * Calculated club data ("berechnet", camera). Each value is set by the server
   * only when its gate passed, so null means "not available"; the UI never
   * re-derives a gate. Absent on older servers and saved shots. The server also
   * sends lofts, face, face to path, the D-plane axis and impact_toe_mm (the
   * impact offset's older name; the UI reads impact_offset_mm below); this
   * hardware cannot deliver those, so the UI never reads them.
   */
  club_data_status?: ClubDataStatus | null;
  club_data_reason?: string | null;
  camera_club_path_deg?: number | null;
  camera_club_path_sigma_deg?: number | null;
  camera_attack_angle_deg?: number | null;
  camera_attack_angle_sigma_deg?: number | null;
  /**
   * Low point of the club's arc from the camera attack angle, cm, + ahead of the ball
   * (toward the target), with its 1-sigma ("berechnet", uncalibrated like the attack
   * angle). Withheld for the driver and woods; null when not measured.
   */
  camera_low_point_cm?: number | null;
  camera_low_point_sigma_cm?: number | null;
  /**
   * Impact point from the camera ("berechnet", not yet calibrated against a
   * Trackman iO: a fixed per-club offset of a few mm is still unknown). Heel/toe
   * offset in mm, + toward the toe (right-handed), with its 1-sigma; null when
   * neither method passed its gates. Shown as a face graphic with its zone word
   * (the kiosk's Treffpunkt tile and the club page of "All values"), never in mm
   * until a Trackman check.
   */
  impact_offset_mm?: number | null;
  impact_sigma_mm?: number | null;
  /** Ball centre above the head's lower edge, mm: only for a teed wood or driver, else null. */
  impact_height_mm?: number | null;
  impact_height_sigma_mm?: number | null;
  /**
   * Heel / center / toe (center: |offset| < 6 mm irons and wedges, < 8 mm woods and
   * driver, preliminary); null when the sigma leaves the zone ambiguous.
   */
  impact_zone?: ImpactZone | null;
  impact_method?: ImpactMethod | null;
  /** Image scale at the ball the millimetres were measured with (mm per pixel). */
  impact_scale_mm_per_px?: number | null;
  // Rolling buffer mode spin data
  spin_rpm: number | null;
  spin_confidence: number | null;
  spin_quality: SpinQuality | null;
  spin_source: 'measured' | 'calculated' | null;
  spin_method?: string | null;
  spin_multipath_fade_hz?: number | null;
  carry_spin_adjusted: number | null;
  /** Spin the carry was computed with: the measurement only when the server trusted it. */
  carry_spin_rpm?: number | null;
  carry_spin_source?: 'measured' | 'calculated' | 'club_typical' | 'speed_model' | null;
  /** Simulated landing offset from the target line, yards, + right (seen from behind the ball). */
  carry_lateral_yards?: number | null;
  /** Highest point of the simulated flight behind the carry, yards; null for a table carry. */
  apex_yards?: number | null;
  /** The simulated flight; null when none was simulated, absent on shots saved before it existed. */
  flight_path?: FlightPath | null;
  /** The bounces and roll after the touchdown, in the flight path's format; null when the shot has none. */
  roll_path?: FlightPath | null;
  /** Total minus carry; negative when the ball spins back. */
  roll_yards?: number | null;
  /** Where the ball rests, measured from the tee like carry. */
  total_yards?: number | null;
  demo?: boolean;
  swing_speed_duration_ms?: number;
  swing_speed_reading_count?: number;
  swing_speed_trigger_mph?: number;
  training_implement?: string;
  training_implement_label?: string;
  camera_replay?: CameraReplay | null;
}

export interface SessionStats {
  shot_count: number;
  avg_ball_speed: number;
  max_ball_speed: number;
  min_ball_speed: number;
  std_dev?: number;
  avg_club_speed: number | null;
  avg_smash_factor: number | null;
  avg_carry_est: number;
  // Rolling buffer mode spin stats
  avg_spin_rpm?: number | null;
  spin_detection_rate?: number;
  mode?: 'rolling-buffer' | 'demo';
}

export interface CalibrationStatus {
  horizontal_phase_reference: boolean;
}

export interface SessionState {
  stats: SessionStats;
  shots: Shot[];
  club?: string;
  calibration?: CalibrationStatus;
  demo_active?: boolean;
}

export interface TriggerDiagnostic {
  timestamp: string;
  trigger_type: string;
  accepted: boolean;
  reason: string;
  response_bytes: number;
  total_readings: number;
  outbound_readings: number;
  inbound_readings: number;
  peak_outbound_mph: number;
  peak_inbound_mph: number;
  all_outbound_speeds: number[];
  all_inbound_speeds: number[];
  peak_outbound_magnitude: number;
  peak_inbound_magnitude: number;
  latency_ms: number | null;
  // Present when accepted (shot created):
  ball_speed_mph?: number | null;
  club_speed_mph?: number | null;
  spin_rpm?: number | null;
  carry_yards?: number | null;
  iwr6843?: IWR6843Diagnostic;
}

export type IWR6843DiagnosticState = 'accepted' | 'rejected' | 'error';

export interface IWR6843Diagnostic {
  state: IWR6843DiagnosticState;
  reason: string;
  angle_deg?: number;
}

export interface TriggerDiagnosticUpdate {
  timestamp: string;
  iwr6843: IWR6843Diagnostic;
}

export interface TriggerStatus {
  mode: 'rolling-buffer' | 'mock' | 'swing-speed';
  trigger_type: string | null;
  radar_connected: boolean;
  radar_port: string | null;
  triggers_total: number;
  triggers_accepted: number;
  triggers_rejected: number;
  /** Latest client-minus-Pi clock offset in seconds per client kind (null until reported). */
  clock_offset_s?: ClockOffsets;
}

export interface ClockOffsets {
  kiosk: number | null;
  phone: number | null;
}

export interface SwingSpeedStats {
  count: number;
  last_speed_mph: number;
  best_speed_mph: number;
  avg_speed_mph: number;
}

export interface SwingSpeedStatsFilter {
  profileId?: string | null;
  trainingImplement?: string | null;
  club?: string | null;
}

export function isSwingSpeedShot(shot: Shot | null): boolean {
  return shot?.mode === 'swing-speed' || shot?.club === 'Swing Speed';
}

export function getSwingSpeedMph(shot: Shot): number {
  return shot.club_speed_mph ?? shot.ball_speed_mph;
}

/**
 * The OPS ball-track guard kept a ball speed that reads like the club head's
 * trailing track: the radar barely saw the ball. Ball speed, smash and carry are
 * then marked estimated.
 */
export function isBallSpeedUncertain(shot: Pick<Shot, 'ball_speed_status'>): boolean {
  return shot.ball_speed_status === 'uncertain';
}

/** Carry the UI shows: the spin-adjusted value when the server has one, else the estimate. */
export function getCarryYards(shot: Shot): number {
  return shot.carry_spin_adjusted ?? shot.estimated_carry_yards;
}

/** The fields {@link getDisplayedSpin} reads. Saved history shots have them too (a missing source is ""). */
export interface SpinFields {
  spin_rpm?: number | null;
  carry_spin_rpm?: number | null;
  carry_spin_source?: string | null;
}

/**
 * Where a shown spin comes from: `measured` by the radar and trusted for the
 * carry, `calculated` from ball speed and launch angle, or `typical` for the
 * club or ball speed because the measurement was not trusted.
 */
export type DisplayedSpinSource = 'measured' | 'calculated' | 'typical';

export interface DisplayedSpin {
  rpm: number;
  source: DisplayedSpinSource;
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/**
 * Spin the UI shows: the measured spin only when the server trusted it for the
 * carry (`carry_spin_source` "measured"), otherwise the spin the carry was
 * computed with. Never the raw radar spin on its own: a shot without carry
 * spin (swing speed, mock, older saved shots) has none.
 */
export function getDisplayedSpin(shot: SpinFields): DisplayedSpin | null {
  if (shot.carry_spin_source === 'measured') {
    // carry_spin_rpm is the same measurement, as the carry used it.
    const rpm = isFiniteNumber(shot.spin_rpm) ? shot.spin_rpm : shot.carry_spin_rpm;
    return isFiniteNumber(rpm) ? { rpm, source: 'measured' } : null;
  }
  if (!isFiniteNumber(shot.carry_spin_rpm)) return null;
  return {
    rpm: shot.carry_spin_rpm,
    source: shot.carry_spin_source === 'calculated' ? 'calculated' : 'typical',
  };
}

export function filterShotsByProfile(shots: Shot[], profileId: string): Shot[] {
  if (!profileId) return [];
  return shots.filter((shot) => shot.profile_id === profileId);
}

export function latestDeletableShot(shots: Shot[], profileId: string): Shot | null {
  const newest = filterShotsByProfile(shots, profileId).at(-1);
  return newest && newest.carry_spin_adjusted != null ? newest : null;
}

export function excludeShotsByProfile(shots: Shot[], profileId: string): Shot[] {
  if (!profileId) return shots;
  return shots.filter((shot) => shot.profile_id !== profileId);
}

function normalizeToken(value: string | null | undefined): string {
  return (value?.trim() || '').toLowerCase();
}

export function filterSwingSpeedShots(shots: Shot[], filter: SwingSpeedStatsFilter = {}): Shot[] {
  const scoped = filter.profileId ? filterShotsByProfile(shots, filter.profileId) : shots;
  const trainingImplement = normalizeToken(filter.trainingImplement);
  const club = normalizeToken(filter.club);

  return scoped.filter((shot) => {
    if (!isSwingSpeedShot(shot)) {
      return false;
    }

    if (trainingImplement) {
      const shotImplement = normalizeToken(shot.training_implement);
      const shotImplementLabel = normalizeToken(shot.training_implement_label);
      const shotClub = normalizeToken(shot.club);

      return (
        shotImplement === trainingImplement ||
        shotImplementLabel === trainingImplement ||
        shotClub === trainingImplement
      );
    }

    if (club && normalizeToken(shot.club) !== club) {
      return false;
    }

    return true;
  });
}

export function computeSwingSpeedStats(shots: Shot[], filter: SwingSpeedStatsFilter = {}): SwingSpeedStats {
  const swingSpeeds = filterSwingSpeedShots(shots, filter).map(getSwingSpeedMph);

  if (swingSpeeds.length === 0) {
    return {
      count: 0,
      last_speed_mph: 0,
      best_speed_mph: 0,
      avg_speed_mph: 0,
    };
  }

  const total = swingSpeeds.reduce((sum, speed) => sum + speed, 0);

  return {
    count: swingSpeeds.length,
    last_speed_mph: swingSpeeds[swingSpeeds.length - 1],
    best_speed_mph: Math.max(...swingSpeeds),
    avg_speed_mph: total / swingSpeeds.length,
  };
}

/**
 * Compute session stats from an array of shots. `shot_count` counts every shot;
 * the means and extremes leave out an uncertain ball speed ({@link isBallSpeedUncertain}),
 * as the server's session summary does.
 */
export function computeStats(allShots: Shot[]): SessionStats {
  const shots = allShots.filter((shot) => !isBallSpeedUncertain(shot));
  if (shots.length === 0) {
    return {
      shot_count: allShots.length,
      avg_ball_speed: 0,
      max_ball_speed: 0,
      min_ball_speed: 0,
      avg_club_speed: null,
      avg_smash_factor: null,
      avg_carry_est: 0,
    };
  }

  const ballSpeeds = shots.map((s) => s.ball_speed_mph);
  const clubSpeeds = shots.map((s) => s.club_speed_mph).filter((v): v is number => v !== null);
  const smashFactors = shots.map((s) => s.smash_factor).filter((v): v is number => v !== null);
  const carries = shots.map(getCarryYards);

  const mean = (arr: number[]) => arr.reduce((a, b) => a + b, 0) / arr.length;
  const stdDev = (arr: number[]) => {
    if (arr.length < 2) return 0;
    const m = mean(arr);
    return Math.sqrt(arr.reduce((sum, x) => sum + (x - m) ** 2, 0) / (arr.length - 1));
  };

  return {
    shot_count: allShots.length,
    avg_ball_speed: mean(ballSpeeds),
    max_ball_speed: Math.max(...ballSpeeds),
    min_ball_speed: Math.min(...ballSpeeds),
    std_dev: stdDev(ballSpeeds),
    avg_club_speed: clubSpeeds.length > 0 ? mean(clubSpeeds) : null,
    avg_smash_factor: smashFactors.length > 0 ? mean(smashFactors) : null,
    avg_carry_est: mean(carries),
  };
}

/**
 * Get unique clubs from shots array.
 */
export function getUniqueClubs(shots: Shot[]): string[] {
  const clubs = new Set(shots.map((s) => s.club));
  return Array.from(clubs);
}
