import type { MessageKey } from '../i18n';
import type { CameraCaptureSettings, CameraSettingsUpdate, CameraTargetTap } from '../stores/useCameraStore';
import { FULL_FRAME, viewToFrame, type CropWindow } from './alignCrop';

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** The two marks of the Align view: the resting ball and the target. The chips choose which a tap sets. */
export type AlignStep = 'ball' | 'target';

/**
 * The step the Align view opens on: always the ball, also with stored marks. Opening on the target
 * once a ball was stored sent the first taps at the range to the target when a mark restored from
 * home was off (range session 2026-11-02).
 */
export const FIRST_ALIGN_STEP: AlignStep = 'ball';

/**
 * Where a tap landed on the Align picture, in percent of the camera frame, or null off the picture.
 *
 * The stream box always has the picture's own shape (CameraFeed.css) and shows `crop` of the
 * frame (the close-up, or the whole frame), so a tap in percent of the box maps through the crop
 * to the frame: the server gets the same percentages for the same scene point in either view.
 */
export function tapPosition(
  clientX: number,
  clientY: number,
  box: Box,
  crop: CropWindow = FULL_FRAME
): CameraTargetTap | null {
  if (!(box.width > 0 && box.height > 0)) return null;
  const x = ((clientX - box.left) / box.width) * 100;
  const y = ((clientY - box.top) / box.height) * 100;
  if (!(x >= 0 && x <= 100 && y >= 0 && y <= 100)) return null;
  const frame = viewToFrame({ x, y }, crop);
  return { x_pct: Math.round(frame.x * 1000) / 1000, y_pct: Math.round(frame.y * 1000) / 1000 };
}

/** What a tap on the picture sends in this step: it sets that mark, or moves it when it is set. */
export function alignTapUpdate(step: AlignStep, tap: CameraTargetTap): CameraSettingsUpdate {
  return step === 'ball' ? { ball_tap: tap } : { target_tap: tap };
}

/**
 * What a tap on the Align picture sends in this step, or null when it sends nothing (off the picture,
 * or the step cannot take a mark now). The step itself never changes on a tap.
 */
export function alignTap(
  settings: CameraCaptureSettings,
  step: AlignStep,
  clientX: number,
  clientY: number,
  box: Box,
  crop: CropWindow
): CameraSettingsUpdate | null {
  if (!canTapMark(settings, step)) return null;
  const tap = tapPosition(clientX, clientY, box, crop);
  return tap ? alignTapUpdate(step, tap) : null;
}

/**
 * The note under the marks: what the next tap does in this step, or why it cannot set the target.
 * With the ball set it also names the target chip to choose next (the `step` value).
 */
export function alignNote(settings: CameraCaptureSettings, step: AlignStep): MessageKey {
  if (step === 'ball') return settings.ball_mark ? 'camera.ballRetap' : 'camera.ballHint';
  if (settings.target_line_unavailable_reason) return 'camera.targetUnavailable';
  return settings.target_line ? 'camera.targetRetap' : 'camera.targetHint';
}

/**
 * Whether a tap may set this step's mark now. The ball is a pixel and only needs the running
 * camera; the target is a direction and also needs the server's consent (lens calibration).
 */
export function canTapMark(settings: CameraCaptureSettings, step: AlignStep): boolean {
  if (!settings.running || settings.updating) return false;
  return step === 'ball' || !settings.target_line_unavailable_reason;
}
