import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CameraReplayDialog } from './CameraReplayDialog';

const replay = {
  id: 'replay-123',
  frame_count: 99,
  trigger_frame: 73,
  playback_fps: 60,
  capture_fps: 288,
  duration_seconds: 1.65,
  display_mirror_horizontal: true,
};

describe('CameraReplayDialog', () => {
  it('does not offer unsupported sub-minimum media rates for legacy clips without capture timing', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={{ ...replay, capture_fps: undefined }}
        state={{ kind: 'ready', videoUrl: '/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );
    expect(html).toContain('>0.1×<');
    expect(html).not.toContain('>0.05×<');
    expect(html).not.toContain('>0.025×<');
  });
  it('starts automatic replays at one-tenth speed and indicates exactly two passes', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={replay}
        automatic
        state={{ kind: 'ready', videoUrl: '/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );
    expect(html).toContain('aria-pressed="true">0.1×');
    expect(html).toContain('Replay 1/2');
    expect(html).not.toContain('>Loop<');
  });
  it('shows an explicit preparation state before the manually requested MP4 is ready', () => {
    const html = renderToString(
      <CameraReplayDialog replay={replay} state={{ kind: 'preparing' }} onClose={() => {}} onRetry={() => {}} />
    );

    expect(html).toContain('Preparing replay');
    expect(html).not.toContain('<video');
  });

  it('renders a touch player and positions the impact marker from capture metadata', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={replay}
        state={{ kind: 'ready', videoUrl: 'http://localhost/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );

    expect(html).toContain('role="dialog"');
    expect(html).toContain('class="camera-replay__viewport"');
    expect(html).toContain('<video');
    expect(html).toContain('camera-replay__video--mirrored');
    expect(html).toContain('http://localhost/replay.mp4');
    expect(html).toContain('--replay-impact-position:74.48979591836735%');
    expect(html).toContain('aria-label="Impact"');
    expect(html).toContain('Restart replay');
    expect(html).toContain('Playback speed');
    expect(html).toContain('>1×<');
    expect(html).toContain('>0.5×<');
    expect(html).toContain('>0.25×<');
    expect(html).toContain('>0.1×<');
    expect(html).toContain('>0.05×<');
    expect(html).toContain('>0.025×<');
    expect(html).toContain('aria-pressed="false">Loop<');
  });

  it('asks for autoplay as an inline, muted video and shows no play prompt until playback is refused', () => {
    for (const automatic of [false, true]) {
      const html = renderToString(
        <CameraReplayDialog
          replay={replay}
          automatic={automatic}
          state={{ kind: 'ready', videoUrl: 'http://localhost/replay.mp4' }}
          onClose={() => {}}
          onRetry={() => {}}
        />
      );
      const video = html.match(/<video[^>]*>/)?.[0] ?? '';
      expect(video).toContain('autoPlay=""');
      expect(video).toContain('playsInline=""');
      expect(video).toContain('preload="auto"');
      expect(html).not.toContain('camera-replay__play-prompt');
    }
    const source = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.tsx', import.meta.url)), 'utf8');
    // React sets `muted` as a property only, so check the JSX itself.
    expect(source).toMatch(/<video[^>]*\n\s*muted\n/s);
  });

  it('keeps the play prompt over the frame and as large as a touch control', () => {
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');
    expect(css).toMatch(/\.camera-replay__play-prompt \{[^}]*position: absolute;[^}]*inset: 0;/s);
    expect(css).toMatch(/\.camera-replay__play-prompt-button \{[^}]*min-height: 56px/s);
  });

  it('fills Play and Retry with the block accent and its text colour, readable in sun and dark', () => {
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');
    const primary = css.match(/\.camera-replay__button--primary \{[^}]*\}/)?.[0] ?? '';
    expect(primary).toMatch(/background: var\(--color-accent-block\);/);
    expect(primary).toMatch(/color: var\(--color-accent-fg\);/);
  });

  it('offers retry and close after preparation fails', () => {
    const html = renderToString(
      <CameraReplayDialog replay={replay} state={{ kind: 'error' }} onClose={() => {}} onRetry={() => {}} />
    );

    expect(html).toContain('Could not prepare replay');
    expect(html).toContain('Try again');
    expect(html).toContain('Close replay');
  });

  it('gives playback failures their own retryable feedback', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={replay}
        state={{ kind: 'error', stage: 'playback' }}
        onClose={() => {}}
        onRetry={() => {}}
        onPlaybackError={() => {}}
      />
    );

    expect(html).toContain('Could not play replay');
    expect(html).toContain('Try again');
  });

  it('keeps every touch control at least 44 CSS pixels tall', () => {
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');

    expect(css).toMatch(/\.camera-replay__button \{[^}]*min-height: 44px/);
    expect(css).toMatch(/\.camera-replay__scrubber \{[^}]*min-height: 44px/);
  });

  it('keeps the scrubber and playback controls in a separate panel beside the video', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={replay}
        state={{ kind: 'ready', videoUrl: 'http://localhost/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');

    expect(html).toMatch(
      /class="camera-replay__body">.*class="camera-replay__stage">.*class="camera-replay__controls">/s
    );
    expect(css).toMatch(/\.camera-replay__body \{[^}]*grid-template-columns: minmax\(0, 1fr\) minmax\(280px, 32%\);/s);
    expect(css).toMatch(/\.camera-replay__stage \{[^}]*grid-area: stage;[^}]*display: grid;[^}]*place-items: center;/s);
    expect(css).toMatch(
      /\.camera-replay__viewport \{[^}]*position: relative;[^}]*width: min\(100% - 2px, var\(--replay-max-width, 720px\)\);[^}]*aspect-ratio: var\(--replay-aspect, 1\.6\);/s
    );
    expect(css).not.toMatch(/\.camera-replay__viewport \{[^}]*position: absolute/s);
    expect(css).toMatch(/\.camera-replay__controls \{[^}]*grid-area: controls;[^}]*border-left:/s);
  });

  it('mirrors only captures that request an operator-facing correction', () => {
    const unmirroredHtml = renderToString(
      <CameraReplayDialog
        replay={{ ...replay, display_mirror_horizontal: false }}
        state={{ kind: 'ready', videoUrl: 'http://localhost/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');

    expect(unmirroredHtml).not.toContain('camera-replay__video--mirrored');
    expect(css).toMatch(/\.camera-replay__video--mirrored \{[^}]*transform: scaleX\(-1\);/s);
  });

  it('sizes the viewport to the whole 16:10 frame and lets contain letterbox the video inside it', () => {
    const html = renderToString(
      <CameraReplayDialog
        replay={replay}
        state={{ kind: 'ready', videoUrl: 'http://localhost/replay.mp4' }}
        onClose={() => {}}
        onRetry={() => {}}
      />
    );
    const css = readFileSync(fileURLToPath(new URL('./CameraReplayDialog.css', import.meta.url)), 'utf8');

    // 640 x 400 until the video reports its own size.
    expect(html).toContain('style="--replay-aspect:1.6"');
    // The video resolves 100% against the viewport box, so object-fit works.
    expect(css).toMatch(/\.camera-replay__video \{[^}]*position: absolute;[^}]*inset: 0;[^}]*object-fit: contain;/s);
    // The viewport fits the stage in both directions; never a fixed 16:9 box.
    expect(css).toMatch(/\.camera-replay__stage \{[^}]*container-type: size;/s);
    expect(css).toMatch(
      /width: min\(100cqw - 2px, var\(--replay-max-width, 720px\), \(100cqh - 2px\) \* var\(--replay-aspect, 1\.6\)\);/
    );
    expect(css).toMatch(/\.camera-replay__viewport \{[^}]*box-sizing: content-box;/s);
    expect(css).not.toMatch(/aspect-ratio: 16 \/ 9/);
    // Stage above controls only in portrait; landscape phones keep them side by side.
    expect(css).toMatch(/@media \(max-width: 760px\) and \(orientation: portrait\)/);
    // A query for every phone (no orientation) may size buttons, never the layout.
    for (const [block] of css.matchAll(/@media \(max-width: 760px\) \{[^@]*/g)) {
      expect(block).not.toMatch(/camera-replay__(body|stage|controls)|grid-template/);
    }
  });
});
