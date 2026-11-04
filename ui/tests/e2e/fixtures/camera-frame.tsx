import { createRoot } from 'react-dom/client';
import { CompanionReplayPlayer } from '../../../src/companion/CompanionReplayPlayer';
import '../../../src/companion/CompanionPage.css';
import type { CameraReplay } from '../../../src/types/shot';

/** The companion replay player in the companion page layout, with a 640 x 400 or 640 x 200 test clip. */
const params = new URLSearchParams(window.location.search);
const size = params.get('size') === '640x200' ? '640x200' : '640x400';

const replay: CameraReplay = {
  id: 'fixture-frame',
  frame_count: 10,
  trigger_frame: 5,
  playback_fps: 10,
  duration_seconds: 1,
  display_mirror_horizontal: false,
};

createRoot(document.getElementById('root')!).render(
  <main className="companion" aria-label="Companion">
    <header className="companion__header">
      <h1>OpenFlight</h1>
    </header>
    <section className="companion__replay">
      <CompanionReplayPlayer
        replay={replay}
        videoUrl={`/tests/e2e/fixtures/frame-${size}.webm`}
        onPlaybackError={() => {}}
      />
    </section>
  </main>
);
