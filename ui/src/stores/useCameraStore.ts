import { create } from 'zustand';

export interface CameraCaptureSettings {
  available: boolean;
  enabled?: boolean;
  running?: boolean;
  armed?: boolean;
  width?: number;
  height?: number;
  fps?: number;
  delivered_fps?: number | null;
  capture_mode?: string | null;
  capture_modes?: { id: string; width: number; height: number; fps: number }[];
  updating?: boolean;
  pre_ms?: number;
  post_ms?: number;
  pre_frames?: number;
  post_frames?: number;
  buffered_frames?: number;
  required_pre_frames?: number;
  exposure_us?: number;
  max_exposure_us?: number;
  gain?: number;
  stream?: string;
  rotate_180?: boolean;
  mirror_horizontal?: boolean;
  roll_correction_deg?: number;
  alignment_x_pct?: number;
  alignment_y_pct?: number;
  raw_crop_adjustable?: boolean;
  vertical_offset_px?: number;
  vertical_offset_min_px?: number;
  vertical_offset_max_px?: number;
  vertical_offset_step_px?: number;
  auto_exposure?: CameraAutoExposureStatus;
  auto_exposure_enabled?: boolean;
  /** The capture status: the live frame or the exposure lock shows no picture (one grey level). */
  frame_flat?: boolean;
  /** The tapped target the start directions are measured from; null when none is set. */
  target_line?: CameraTargetLine | null;
  /** Why a target cannot be tapped now (no lens calibration for this mode, ...); null when it can. */
  target_line_unavailable_reason?: string | null;
  /** The tapped resting ball the camera searches around; null when none is set. */
  ball_mark?: CameraBallMark | null;
}

/** The server's tapped resting ball (`openflight.angle_reference.BallMark.to_log`). */
export interface CameraBallMark {
  frame_x_px: number;
  frame_y_px: number;
  /** Where it was tapped, in percent of the preview picture. */
  preview_x_pct: number;
  preview_y_pct: number;
  /** Epoch seconds. */
  set_at: number;
}

/** The server's tapped target (`openflight.angle_reference.TargetLine.to_log`). */
export interface CameraTargetLine {
  frame_x_px: number;
  frame_y_px: number;
  /** Where it was tapped, in percent of the preview picture. */
  preview_x_pct: number;
  preview_y_pct: number;
  /** Direction in the camera frame, + is target-right. */
  azimuth_deg: number;
  elevation_deg: number;
  /** Epoch seconds. */
  set_at: number;
}

/** A tap on the preview picture, in percent of its content box. */
export interface CameraTargetTap {
  x_pct: number;
  y_pct: number;
}

/** What the Camera tab may send: settings, `target_tap` or `ball_tap` (null clears that mark). */
export type CameraSettingsUpdate = Partial<CameraCaptureSettings> & {
  target_tap?: CameraTargetTap | null;
  ball_tap?: CameraTargetTap | null;
};

export interface CameraAutoExposureStatus {
  enabled: boolean;
  status: 'ready' | 'adjusting' | 'lighting_required' | 'no_image' | 'unavailable';
  analysis_eligible: boolean;
  /** True when the last frame had no usable spread (sensor delivered a flat image). */
  frame_flat?: boolean;
  message: string;
  motion_blur_risk: 'low' | 'elevated' | 'high';
  capture_deferred?: boolean;
  exposure_us?: number;
  gain?: number;
  observation?: {
    sample_available: boolean;
    status: 'good' | 'too_dark' | 'too_bright' | 'marginal' | 'flat' | 'unavailable';
    recommendation?: 'brighter' | 'darker' | 'hold';
    message?: string;
    median?: number | null;
    p90?: number | null;
    contrast?: number | null;
    clipped_pct?: number | null;
    dark_pct?: number | null;
  };
}

interface CameraState {
  captureSettings: CameraCaptureSettings;
  captureSettingsError: string | null;
  setCaptureSettings: (settings: CameraCaptureSettings) => void;
  setCaptureSettingsError: (error: string | null) => void;
  beginUpdate: () => void;
}

export const useCameraStore = create<CameraState>((set) => ({
  captureSettings: { available: false },
  captureSettingsError: null,
  setCaptureSettings: (settings) => set({ captureSettings: settings, captureSettingsError: null }),
  setCaptureSettingsError: (error) =>
    set((state) => ({
      captureSettingsError: error,
      captureSettings: { ...state.captureSettings, updating: false },
    })),
  beginUpdate: () =>
    set((state) => ({
      captureSettingsError: null,
      captureSettings: { ...state.captureSettings, updating: true },
    })),
}));
