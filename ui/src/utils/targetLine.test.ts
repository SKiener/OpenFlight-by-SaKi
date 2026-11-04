import { describe, expect, it } from 'vitest';
import type { CameraCaptureSettings } from '../stores/useCameraStore';
import { setActiveLocale, t } from '../i18n';
import { ALIGN_CROP } from './alignCrop';
import { alignNote, alignTap, alignTapUpdate, canTapMark, FIRST_ALIGN_STEP, tapPosition } from './targetLine';

const BOX = { left: 100, top: 50, width: 640, height: 400 };
const RUNNING: CameraCaptureSettings = { available: true, running: true, target_line_unavailable_reason: null };
const BALL = { frame_x_px: 367.5, frame_y_px: 263.5, preview_x_pct: 57.5, preview_y_pct: 66, set_at: 900 };
const TARGET = {
  frame_x_px: 412.3,
  frame_y_px: 150.1,
  preview_x_pct: 64.5,
  preview_y_pct: 37.6,
  azimuth_deg: 2.34,
  elevation_deg: 1.1,
  set_at: 980,
};

describe('tapPosition', () => {
  it('gives the tap in percent of the picture', () => {
    expect(tapPosition(100 + 400, 50 + 160, BOX)).toEqual({ x_pct: 62.5, y_pct: 40 });
  });

  it('keeps the corners on the picture', () => {
    expect(tapPosition(100, 50, BOX)).toEqual({ x_pct: 0, y_pct: 0 });
    expect(tapPosition(740, 450, BOX)).toEqual({ x_pct: 100, y_pct: 100 });
  });

  it('rounds to a thousandth of a percent', () => {
    expect(tapPosition(100 + 1, 50 + 1, { ...BOX, width: 3, height: 7 })).toEqual({ x_pct: 33.333, y_pct: 14.286 });
  });

  it('ignores a tap beside the picture (the phone status line under it)', () => {
    expect(tapPosition(99, 100, BOX)).toBeNull();
    expect(tapPosition(300, 451, BOX)).toBeNull();
  });

  it('ignores a picture without a size', () => {
    expect(tapPosition(100, 50, { ...BOX, width: 0 })).toBeNull();
    expect(tapPosition(100, 50, { ...BOX, height: Number.NaN })).toBeNull();
  });
});

describe('the Align steps', () => {
  it('sends a tap as the active step mark', () => {
    const tap = { x_pct: 57.5, y_pct: 66 };
    expect(alignTapUpdate('ball', tap)).toEqual({ ball_tap: tap });
    expect(alignTapUpdate('target', tap)).toEqual({ target_tap: tap });
  });

  it('opens on the ball, also with a stored ball mark and target', () => {
    expect(FIRST_ALIGN_STEP).toBe('ball');
  });

  it('lets the ball be tapped without the lens calibration the target needs', () => {
    const uncalibrated = { ...RUNNING, target_line_unavailable_reason: 'no --camera-intrinsics' };
    expect(canTapMark(uncalibrated, 'ball')).toBe(true);
    expect(canTapMark(uncalibrated, 'target')).toBe(false);
    expect(canTapMark(RUNNING, 'target')).toBe(true);
  });

  it('takes no tap while the camera is stopped or a change is on its way', () => {
    for (const step of ['ball', 'target'] as const) {
      expect(canTapMark({ ...RUNNING, running: false }, step)).toBe(false);
      expect(canTapMark({ ...RUNNING, updating: true }, step)).toBe(false);
    }
  });
});

describe('a tap through the close-up', () => {
  // The stream box at 640 x 400 px on screen, showing the close-up window (x 25-75 %, y 38-88 %).
  const VIEW = { left: 100, top: 50, width: 640, height: 400 };

  it('sends frame percent: the view centre is the window centre', () => {
    expect(tapPosition(100 + 320, 50 + 200, VIEW, ALIGN_CROP)).toEqual({ x_pct: 50, y_pct: 63 });
  });

  it('maps the view corners to the window corners', () => {
    expect(tapPosition(100, 50, VIEW, ALIGN_CROP)).toEqual({ x_pct: 25, y_pct: 38 });
    expect(tapPosition(740, 450, VIEW, ALIGN_CROP)).toEqual({ x_pct: 75, y_pct: 88 });
  });

  it('sends the same frame point as a tap on the whole picture', () => {
    // The full-frame tap at 62.5 % / 40 % of a 640 x 400 box, the same scene point in the close-up.
    const full = tapPosition(100 + 400, 50 + 160, VIEW);
    const viewX = ((62.5 - 25) / 50) * 640;
    const viewY = ((40 - 38) / 50) * 400;
    expect(tapPosition(100 + viewX, 50 + viewY, VIEW, ALIGN_CROP)).toEqual(full);
  });

  // The tap's payload per step through the close-up. That the step itself stays put after a tap
  // (no jump to the target) lives in CameraFeed's state, which these DOM-less tests cannot click:
  // e2e camera-target.spec.ts pins it ("a tap keeps the step", "a second ball tap stays on the ball").
  it('sends each tap to the step it is given: ball taps move the ball, target taps the target', () => {
    const settings = { ...RUNNING, ball_mark: BALL, target_line: TARGET };
    const first = alignTap(settings, 'ball', 100 + 320, 50 + 200, VIEW, ALIGN_CROP);
    const second = alignTap(settings, 'ball', 100 + 352, 50 + 216, VIEW, ALIGN_CROP);
    expect(first).toEqual({ ball_tap: { x_pct: 50, y_pct: 63 } });
    expect(second).toEqual({ ball_tap: { x_pct: 52.5, y_pct: 65 } });
    expect(alignTap(settings, 'target', 100 + 448, 50 + 40, VIEW, ALIGN_CROP)).toEqual({
      target_tap: { x_pct: 60, y_pct: 43 },
    });
  });

  it('sends nothing off the picture or when the step cannot take a mark', () => {
    expect(alignTap(RUNNING, 'ball', 99, 100, VIEW, ALIGN_CROP)).toBeNull();
    expect(alignTap({ ...RUNNING, updating: true }, 'ball', 420, 250, VIEW, ALIGN_CROP)).toBeNull();
    const uncalibrated = { ...RUNNING, target_line_unavailable_reason: 'no --camera-intrinsics' };
    expect(alignTap(uncalibrated, 'target', 420, 250, VIEW, ALIGN_CROP)).toBeNull();
  });
});

describe('the note under the marks', () => {
  const note = (settings: CameraCaptureSettings, step: 'ball' | 'target') =>
    t(alignNote(settings, step), { step: t('camera.stepTarget') });

  it('says what the next tap does in each step and state', () => {
    setActiveLocale('de');
    try {
      expect(note(RUNNING, 'ball')).toBe('Tippe auf den Ball.');
      expect(note({ ...RUNNING, ball_mark: BALL }, 'ball')).toBe(
        'Nochmal tippen verschiebt die Ballmarke. Danach „2 Ziel“ wählen.'
      );
      expect(note({ ...RUNNING, ball_mark: BALL }, 'target')).toBe('Tippe auf das Ziel.');
      expect(note({ ...RUNNING, ball_mark: BALL, target_line: TARGET }, 'target')).toBe(
        'Nochmal tippen verschiebt das Ziel.'
      );
    } finally {
      setActiveLocale('en');
    }
    expect(note(RUNNING, 'ball')).toBe('Tap the ball.');
    expect(note({ ...RUNNING, ball_mark: BALL }, 'ball')).toBe(
      'Tap again to move the ball mark. Then choose “2 Target”.'
    );
    expect(note(RUNNING, 'target')).toBe('Tap the target.');
    expect(note({ ...RUNNING, target_line: TARGET }, 'target')).toBe('Tap again to move the target.');
  });

  it('keeps saying why a target cannot be tapped', () => {
    const uncalibrated = { ...RUNNING, ball_mark: BALL, target_line_unavailable_reason: 'no --camera-intrinsics' };
    expect(note(uncalibrated, 'target')).toBe('Tapping a target needs the lens calibration of this camera mode.');
    // The ball needs no lens calibration: its step still asks for it.
    expect(note({ ...uncalibrated, ball_mark: null }, 'ball')).toBe('Tap the ball.');
  });
});
