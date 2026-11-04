/**
 * TI IWR6843 tilt calibration as `/api/calibration/iwr6843/phone-tilt` and the
 * `iwr6843_tilt_calibration` socket event report it (src/openflight/server.py
 * `_iwr6843_tilt_status`, src/openflight/phone_tilt.py `build_record`).
 */

/** Where the configured TI tilt came from: the phone measurement, `--iwr6843-tilt-deg`, or the calibration JSON. */
export type TiltSource = 'phone' | 'cli' | 'calibration_json';

/** The saved phone measurement as the server reports it (the raw request copy is left out). */
export interface PhoneTiltRecord {
  source: 'phone';
  convention: 'w3c' | 'inverted';
  /** Positive: the case's ball side is higher. */
  case_tilt_deg: number;
  phone_bias_deg: number;
  case_roll_deg: number;
  phone_roll_bias_deg: number;
  case_top_to_boresight_deg: number;
  radar_tilt_deg: number;
  configured_iwr_tilt_deg: number;
  inclinometer_pitch_deg: number | null;
  inclinometer_minus_phone_deg: number | null;
  /** Phone clock (ISO 8601) when position B was measured. */
  measured_at: string;
  /** Pi clock (ISO 8601) when the server applied it. */
  applied_at: string;
}

export interface TiltCalibrationStatus {
  enabled: boolean;
  configured_iwr_tilt_deg: number | null;
  tilt_source: TiltSource | null;
  fallback_iwr_tilt_deg?: number | null;
  fallback_source?: TiltSource | null;
  case_top_to_boresight_deg: number;
  inclinometer_enabled: boolean;
  /** The tilt the last TI shot used (configured plus the LIS3DH pitch). */
  last_effective_iwr_tilt_deg: number | null;
  last_effective_at: string | null;
  calibration: PhoneTiltRecord | null;
}

/** The socket event wraps the status with why it was sent. */
export interface TiltCalibrationEvent extends TiltCalibrationStatus {
  event: 'applied' | 'removed' | 'state';
}

/** How the phone reports `accelerationIncludingGravity`: as the W3C spec says, or every axis negated. */
export type GravityConvention = 'w3c' | 'inverted';

export type Vec3 = readonly [number, number, number];

export interface PhoneTiltPosition {
  /** Mean `accelerationIncludingGravity` in m/s², exactly as the phone reported it. */
  gravity: [number, number, number];
  pitch_std_deg: number;
  sample_count: number;
}

export interface PhoneTiltRequest {
  position_a: PhoneTiltPosition;
  position_b: PhoneTiltPosition;
  measured_at: string;
  convention: GravityConvention;
  client: Record<string, string | number | boolean | null>;
}
