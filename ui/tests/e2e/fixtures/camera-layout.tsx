import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { CameraPanel } from '../../../src/components/panel/CameraPanel';
import { useLocaleStore } from '../../../src/stores/useLocaleStore';
import type { CameraCaptureSettings, CameraSettingsUpdate } from '../../../src/stores/useCameraStore';
import '../../../src/index.css';
import '../../../src/components/panel/panel.css';
import '../../../src/components/range/range.css';

useLocaleStore.getState().setLocale('de');
document.documentElement.dataset.theme = 'dark';
const nativeFetch = window.fetch;
window.fetch = async (input, init) => {
  if (String(input).includes('/api/camera/preview.jpg'))
    return new Response(
      new Blob(
        [
          '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400"><rect width="640" height="400" fill="#666"/><path d="M0 0L640 400M640 0L0 400" stroke="white"/><text x="200" y="150" fill="white">640 × 400 TEST</text></svg>',
        ],
        { type: 'image/svg+xml' }
      )
    );
  if (String(input).includes('/api/camera/exposure-quality'))
    return Response.json({ sample_available: false, status: 'unavailable' });
  return nativeFetch(input, init);
};

/** The Align marks as the server answers a tap (a made-up lens: 0.1 deg per percent off centre). */
function withMarks(previous: CameraCaptureSettings, update: CameraSettingsUpdate): CameraCaptureSettings | null {
  const width = previous.width ?? 640;
  const height = previous.height ?? 400;
  const setAt = Date.now() / 1000;
  if ('ball_tap' in update) {
    const tap = update.ball_tap;
    return {
      ...previous,
      ball_mark: tap
        ? {
            frame_x_px: (tap.x_pct / 100) * width - 0.5,
            frame_y_px: (tap.y_pct / 100) * height - 0.5,
            preview_x_pct: tap.x_pct,
            preview_y_pct: tap.y_pct,
            set_at: setAt,
          }
        : null,
    };
  }
  if ('target_tap' in update) {
    const tap = update.target_tap;
    return {
      ...previous,
      target_line: tap
        ? {
            frame_x_px: (tap.x_pct / 100) * width - 0.5,
            frame_y_px: (tap.y_pct / 100) * height - 0.5,
            preview_x_pct: tap.x_pct,
            preview_y_pct: tap.y_pct,
            azimuth_deg: (tap.x_pct - 50) * 0.1,
            elevation_deg: (50 - tap.y_pct) * 0.1,
            set_at: setAt,
          }
        : null,
    };
  }
  return null;
}

function Fixture() {
  const [settings, setSettings] = useState<CameraCaptureSettings>({
    available: true,
    enabled: true,
    running: true,
    armed: true,
    width: 640,
    height: 400,
    fps: 300,
    capture_mode: 'full-300',
    capture_modes: [
      { id: 'full-300', width: 640, height: 400, fps: 300 },
      { id: 'strip-500', width: 640, height: 200, fps: 500 },
      { id: 'strip-560', width: 640, height: 200, fps: 560 },
    ],
    exposure_us: 400,
    gain: 8,
    raw_crop_adjustable: false,
    pre_ms: 150,
    post_ms: 50,
    pre_frames: 45,
    post_frames: 15,
    vertical_offset_px: 0,
    vertical_offset_min_px: -70,
    vertical_offset_max_px: 70,
    vertical_offset_step_px: 10,
  });
  return (
    <div className="panel-app range-app">
      <header className="range-header">
        <b>OpenFlight · Kameratest</b>
      </header>
      <main className="panel-app__main">
        <CameraPanel
          captureSettings={settings}
          captureSettingsError={null}
          onUpdateCaptureSettings={(update) =>
            setSettings((previous) => {
              const marked = withMarks(previous, update);
              if (marked) return marked;
              const mode = previous.capture_modes?.find((item) => item.id === update.capture_mode);
              return {
                ...previous,
                ...update,
                ...(mode
                  ? {
                      width: mode.width,
                      height: mode.height,
                      fps: mode.fps,
                      raw_crop_adjustable: mode.width === 320,
                    }
                  : {}),
              };
            })
          }
        />
      </main>
      <footer className="range-footer" style={{ height: 82 }}>
        <button
          onClick={() =>
            setSettings((previous) => ({ ...previous, width: 320, height: 200, raw_crop_adjustable: true }))
          }
        >
          Testmodus 320 × 200
        </button>
      </footer>
    </div>
  );
}
createRoot(document.getElementById('root')!).render(<Fixture />);
