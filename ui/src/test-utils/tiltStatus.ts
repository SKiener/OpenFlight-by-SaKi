import type { PhoneTiltRecord, TiltCalibrationStatus } from '../types/tiltCalibration';

/** The fixed "now" the tilt fixtures are dated against (3 days after the measurement). */
export const TILT_NOW_MS = Date.parse('2026-10-30T12:00:00Z');

/** A phone measurement: case front 1.24° higher, radar tilt 11.24°, LIS3DH 0.31° off. */
export function phoneTiltRecord(overrides: Partial<PhoneTiltRecord> = {}): PhoneTiltRecord {
  return {
    source: 'phone',
    convention: 'w3c',
    case_tilt_deg: 1.24,
    phone_bias_deg: 0.2,
    case_roll_deg: 0.1,
    phone_roll_bias_deg: -0.1,
    case_top_to_boresight_deg: 10,
    radar_tilt_deg: 11.24,
    configured_iwr_tilt_deg: 9.84,
    inclinometer_pitch_deg: 1.4,
    inclinometer_minus_phone_deg: 0.16,
    measured_at: '2026-10-27T11:00:00.000Z',
    applied_at: '2026-10-27T13:00:05+02:00',
    ...overrides,
  };
}

/** TI on, tilt from the phone calibration above, no shot since. */
export function tiltStatus(overrides: Partial<TiltCalibrationStatus> = {}): TiltCalibrationStatus {
  return {
    enabled: true,
    configured_iwr_tilt_deg: 9.84,
    tilt_source: 'phone',
    fallback_iwr_tilt_deg: 10.405,
    fallback_source: 'cli',
    case_top_to_boresight_deg: 10,
    inclinometer_enabled: true,
    last_effective_iwr_tilt_deg: null,
    last_effective_at: null,
    calibration: phoneTiltRecord(),
    ...overrides,
  };
}

/** TI on, the start script's fixed tilt, no phone calibration, no LIS3DH. */
export function cliTiltStatus(overrides: Partial<TiltCalibrationStatus> = {}): TiltCalibrationStatus {
  return tiltStatus({
    configured_iwr_tilt_deg: 10.405,
    tilt_source: 'cli',
    inclinometer_enabled: false,
    calibration: null,
    ...overrides,
  });
}
