import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale } from '../i18n';
import type { CameraCaptureSettings } from '../stores/useCameraStore';
import { verticalViewTargets } from '../utils/cameraView';
import { CameraFeed } from './CameraFeed';

const captureSettings: CameraCaptureSettings = {
  available: true,
  enabled: true,
  running: true,
  armed: true,
  width: 320,
  height: 200,
  fps: 600,
  pre_ms: 150,
  post_ms: 50,
  pre_frames: 90,
  post_frames: 30,
  exposure_us: 500,
  max_exposure_us: 1666,
  gain: 2,
  rotate_180: true,
  alignment_x_pct: 48,
  alignment_y_pct: 55,
  raw_crop_adjustable: true,
  vertical_offset_px: -20,
  vertical_offset_min_px: -70,
  vertical_offset_max_px: 70,
  vertical_offset_step_px: 10,
  auto_exposure: {
    enabled: true,
    status: 'ready',
    analysis_eligible: true,
    message: 'Impact-area exposure and contrast look good',
    motion_blur_risk: 'low',
    exposure_us: 500,
    gain: 12,
    observation: {
      sample_available: true,
      status: 'good',
      median: 108,
      contrast: 94,
    },
  },
};

const BALL = { frame_x_px: 367.5, frame_y_px: 263.5, preview_x_pct: 57.5, preview_y_pct: 66, set_at: 1_761_389_200 };
const TARGET = {
  frame_x_px: 412.3,
  frame_y_px: 150.1,
  preview_x_pct: 64.5,
  preview_y_pct: 37.6,
  azimuth_deg: 2.34,
  elevation_deg: 1.1,
  set_at: 1_761_389_280,
};

/** The step chips marked pressed, by label. */
function pressedSteps(html: string): string[] {
  return [...html.matchAll(/class="camera-feed__step[^"]*" aria-pressed="true"[^>]*>([^<]*)</g)].map(
    (match) => match[1]
  );
}

describe('CameraFeed', () => {
  afterEach(() => setActiveLocale('en'));

  it('explains the fixed full view without offering unsupported crop changes', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed
        captureSettings={{ ...captureSettings, width: 640, height: 400, fps: 300, raw_crop_adjustable: false }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('640 × 400');
    expect(html).toContain('In diesem Aufnahmemodus ist der Bildausschnitt fest');
    expect(html).not.toContain('echter 320 × 200 Ausschnitt');
    expect(html).not.toContain('Nach oben');
    expect(html).not.toContain('Nach unten');
  });

  it('does not ask for more light when the automatic check reports overexposure', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          auto_exposure: {
            ...captureSettings.auto_exposure!,
            status: 'lighting_required',
            analysis_eligible: false,
            observation: { sample_available: true, status: 'too_bright' },
          },
        }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('Beleuchtung prüfen');
    expect(html).not.toContain('Mehr Licht nötig');
  });

  const flatFrame: CameraCaptureSettings = {
    ...captureSettings,
    auto_exposure: {
      ...captureSettings.auto_exposure!,
      status: 'no_image',
      analysis_eligible: false,
      frame_flat: true,
      observation: { sample_available: true, status: 'flat' },
    },
  };

  it.each([
    ['en', 'No picture from the sensor'],
    ['de', 'Kein Bild vom Sensor'],
    ['es', 'Sin imagen del sensor'],
  ] as const)('names a flat frame in %s instead of waiting, in the lighting warning', (locale, label) => {
    setActiveLocale(locale);
    const html = renderToString(
      <CameraFeed captureSettings={flatFrame} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    expect(html).toContain(`<strong>${label}</strong>`);
    expect(html).toContain('camera-settings__auto-status--no_image');
    expect(html).not.toContain('<strong></strong>');
  });

  it('names a flat frame the capture status reports, in the header badge and the settings', () => {
    const html = renderToString(
      <CameraFeed
        captureSettings={{ ...captureSettings, frame_flat: true }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('camera-feed__exposure-quality--no_image');
    expect(html.replace(/<!-- -->/g, '')).toMatch(
      /camera-feed__exposure-quality--no_image"[^>]*>Auto exposure: No picture from the sensor</
    );
    expect(html).toContain('<strong>No picture from the sensor</strong>');
    expect(html).toContain('camera-settings__auto-status--no_image');
  });

  it('keeps the usual labels without a flat frame', () => {
    const html = renderToString(
      <CameraFeed captureSettings={captureSettings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    expect(html).not.toContain('No picture from the sensor');
    expect(html).not.toContain('--no_image');
    expect(html).toContain('<strong>Ready</strong>');
  });

  it('falls back to the waiting label for an auto-exposure status the UI does not know', () => {
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          auto_exposure: {
            ...captureSettings.auto_exposure!,
            status: 'some_future_status' as never,
          },
        }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('<strong>Waiting</strong>');
    expect(html).not.toContain('<strong></strong>');
  });

  it('translates the camera controls and keeps raw diagnostic errors available', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          auto_exposure: { ...captureSettings.auto_exposure!, motion_blur_risk: 'elevated' },
        }}
        captureSettingsError="camera_device_busy"
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('Kamera einstellen');
    expect(html).toContain('Automatisch neu messen');
    expect(html).toContain('Nach oben');
    expect(html).toContain('Nach unten');
    expect(html).toContain('erhöht');
    expect(html).toContain('camera_device_busy');
  });
  it('maps view direction through the 180-degree mount rotation', () => {
    expect(verticalViewTargets(0, 10, true)).toEqual({ up: 10, down: -10 });
    expect(verticalViewTargets(0, 10, false)).toEqual({ up: -10, down: 10 });
  });

  it('renders the dominant preview workspace and operator settings', () => {
    const html = renderToString(
      <CameraFeed captureSettings={captureSettings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );

    expect(html).toContain('camera-feed__workspace');
    expect(html).toContain('Camera setup');
    expect(html).toContain('Measure automatically again');
    expect(html).toContain('calibrates once at startup');
    expect(html).not.toContain('checks every 5 seconds');
    expect(html).toContain('Auto exposure');
    expect(html).toContain('camera-feed__exposure-quality');
    expect(html).toContain('Camera analysis active');
    expect(html).toContain('Impact median');
    expect(html).toContain('Motion blur risk');
    expect(html).toContain('500<!-- --> µs');
    expect(html).not.toContain('Environment profile');
    expect(html).not.toContain('Darker');
    expect(html).not.toContain('Brighter');
    expect(html).toContain('Ball placement guide');
    expect(html).toContain('50% across · 78% down');
    expect(html).toContain('type="range"');
    expect(html).toContain('Apply manual settings');
    expect(html).not.toContain('Apply alignment guide');
    expect(html).toContain('Sensor view');
    expect(html).toContain('View up');
    expect(html).toContain('View down');
    expect(html).toContain('-20 px');
    expect(html).toContain('320 × 200');
    expect(html).toContain('600 fps');
    expect(html).toContain('Armed');
  });

  it('opens the settings with both marks unset and asks for the ball first', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed captureSettings={captureSettings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    const marks = html.indexOf('camera-settings__section--target');
    expect(marks).toBeGreaterThan(-1);
    // The marks are the panel's first section, above the sensor view.
    expect(marks).toBeLessThan(html.indexOf('Bildausschnitt'));
    expect(html).toContain('Ziellinie');
    expect(html).toContain('<dt>Ball</dt><dd>nicht gesetzt</dd>');
    expect(html).toContain('<dt>Ziel</dt><dd>nicht gesetzt</dd>');
    expect(html).toContain('Tippe auf den Ball.');
    expect(html).toContain('Ball neu');
    expect(html).toContain('Ziel neu');
    expect(html).not.toContain('Kameraachse');
  });

  it('shows both marks set and the target right of the camera axis', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          ball_mark: BALL,
          target_line: TARGET,
          target_line_unavailable_reason: null,
        }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(html).toContain('<dt>Ball</dt><dd>gesetzt</dd>');
    expect(html).toContain('<dt>Ziel</dt><dd>gesetzt</dd>');
    expect(html).toContain('Ziel 2,3° rechts der Kameraachse');
  });

  it('opens on the ball step with a stored ball mark and target, and names the target chip', () => {
    setActiveLocale('de');
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          ball_mark: BALL,
          target_line: TARGET,
          target_line_unavailable_reason: null,
        }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );
    expect(pressedSteps(html)).toEqual(['1 Ball']);
    expect(html).toContain('Nochmal tippen verschiebt die Ballmarke. Danach „2 Ziel“ wählen.');
    expect(html).not.toContain('Nochmal tippen verschiebt das Ziel.');
  });

  it('names a target left of the camera axis in English and Spanish', () => {
    const settings: CameraCaptureSettings = {
      ...captureSettings,
      ball_mark: BALL,
      target_line: { ...TARGET, preview_x_pct: 31.3, azimuth_deg: -1.25 },
    };
    const english = renderToString(
      <CameraFeed captureSettings={settings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    expect(english).toContain('Target 1.3° left of the camera axis');
    expect(english).toContain('New ball');
    expect(english).toContain('New target');
    setActiveLocale('es');
    const spanish = renderToString(
      <CameraFeed captureSettings={settings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    expect(spanish).toContain('Objetivo a 1,3° a la izquierda del eje de la cámara');
    expect(spanish).toContain('Bola nueva');
    expect(spanish).toContain('Objetivo nuevo');
  });

  it('says why a target cannot be tapped once the ball is set', () => {
    const settings = {
      ...captureSettings,
      target_line_unavailable_reason: 'Tapping a target needs the lens calibration (--camera-intrinsics)',
    };
    const ballStep = renderToString(
      <CameraFeed captureSettings={settings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
    );
    // The ball needs no lens calibration: the ball step still asks for it (alignNote covers the target step).
    expect(ballStep).toContain('Tap the ball.');
    expect(ballStep).not.toContain('Tapping a target needs the lens calibration');
  });

  it('opens on the close-up every time: the view button offers the whole picture', () => {
    const render = () =>
      renderToString(
        <CameraFeed captureSettings={captureSettings} captureSettingsError={null} onUpdateCaptureSettings={vi.fn()} />
      );
    // The button shows the view it switches to; the other label (data-spare) only sizes it, unseen.
    const toggle = (html: string) => {
      const match =
        /class="camera-feed__view-toggle" data-spare="([^"]*)"[^>]*><span class="camera-feed__view-toggle-label">([^<]*)</.exec(
          html
        );
      return match && { shown: match[2], spare: match[1] };
    };
    // Each render is a fresh open (the Align view mounts anew): the choice is not kept.
    for (const [locale, full, crop] of [
      ['de', 'Ganzes Bild', 'Ausschnitt'],
      ['en', 'Full picture', 'Close-up'],
      ['es', 'Imagen completa', 'Primer plano'],
      ['de', 'Ganzes Bild', 'Ausschnitt'],
    ]) {
      setActiveLocale(locale);
      expect(toggle(render()), locale).toEqual({ shown: full, spare: crop });
    }
  });

  it('explains lighting failure without disabling capture', () => {
    const html = renderToString(
      <CameraFeed
        captureSettings={{
          ...captureSettings,
          auto_exposure: {
            ...captureSettings.auto_exposure!,
            status: 'lighting_required',
            analysis_eligible: false,
            message: 'Camera lighting is insufficient; add or redirect light toward the ball',
          },
        }}
        captureSettingsError={null}
        onUpdateCaptureSettings={vi.fn()}
      />
    );

    expect(html).toContain('Lighting needed');
    expect(html).toContain('Radar fallback active');
    expect(html).toContain('Preview and raw clips continue recording');
    expect(html).toContain('Armed');
  });
});
