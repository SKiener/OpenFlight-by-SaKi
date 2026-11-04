import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import type { Shot } from '../types/shot';
import { CompanionView, type CompanionViewProps } from './CompanionPage';
import { CompanionReplayPlayer } from './CompanionReplayPlayer';

const shot = (overrides: Partial<Shot>): Shot => ({
  ball_speed_mph: 150,
  club_speed_mph: 102,
  smash_factor: 1.47,
  estimated_carry_yards: 240,
  carry_range: [230, 250],
  club: 'driver',
  timestamp: '2026-10-04T12:00:00Z',
  peak_magnitude: null,
  launch_angle_vertical: 12.1,
  launch_angle_horizontal: -1.4,
  launch_angle_confidence: 0.8,
  angle_source: 'radar',
  club_angle_deg: null,
  club_path_deg: null,
  spin_axis_deg: null,
  spin_rpm: 2600,
  spin_confidence: 0.9,
  spin_quality: 'high',
  spin_source: 'measured',
  carry_spin_adjusted: 245,
  carry_spin_source: 'measured',
  profile_id: 'p1',
  ...overrides,
});

const replay = {
  id: 'r2',
  frame_count: 99,
  trigger_frame: 73,
  playback_fps: 60,
  duration_seconds: 1.65,
  display_mirror_horizontal: false,
};

const shots = [
  shot({ timestamp: '2026-10-04T12:00:00Z', camera_replay: null }),
  shot({
    timestamp: '2026-10-04T12:01:00Z',
    ball_speed_mph: 160,
    carry_spin_adjusted: 260,
    carry_spin_source: 'club_typical',
    camera_replay: replay,
  }),
  shot({ timestamp: '2026-10-04T12:02:00Z', profile_id: 'someone-else' }),
];

const props: CompanionViewProps = {
  connected: true,
  unitSystem: 'mph-meters',
  shots,
  activeProfileId: 'p1',
  phase: null,
  processingTimestamp: null,
  shotVersion: 0,
  demoActive: false,
};

afterEach(() => {
  setActiveLocale('en');
});

describe('CompanionView', () => {
  it('shows the latest shot of the active profile in the selected units with honest carry provenance', () => {
    const html = renderToString(<CompanionView {...props} />);

    expect(html).toContain('Shot\u00a02');
    expect(html).toContain('>≈\u00a0238<');
    expect(html).toContain('>160.0<');
    expect(html).toContain('>mph<');
    expect(html).toContain('with typical spin');
    expect(html.match(/companion__shot-row/g)).toHaveLength(2);
    expect(html).toMatch(/aria-pressed="true" aria-label="Show shot 2"/);
    for (const metric of ['carry', 'ball_speed', 'launch_v', 'launch_h', 'spin', 'club_speed', 'smash']) {
      expect(html).toContain(`data-metric="${metric}"`);
    }
  });

  it('shows preparation progress until the replay is announced', () => {
    const html = renderToString(<CompanionView {...props} />);

    expect(html).toContain('Preparing replay');
    expect(html).not.toContain('<video');
  });

  it('shows the processing state for the live shot', () => {
    const html = renderToString(<CompanionView {...props} phase="calculating" />);

    expect(html).toContain('Calculating metrics');
  });

  it('keeps waiting for camera enrichment instead of claiming there is no replay', () => {
    const pending = [shot({ timestamp: '2026-10-04T12:05:00Z', camera_replay: null })];
    const html = renderToString(
      <CompanionView {...props} shots={pending} phase="camera_processing" processingTimestamp="2026-10-04T12:05:00Z" />
    );

    expect(html).toContain('Preparing replay');
    expect(html).not.toContain('No camera replay');
  });

  it('says when a shot has no camera replay', () => {
    const html = renderToString(<CompanionView {...props} shots={[shots[0]]} />);

    expect(html).toContain('No camera replay for this shot');
  });

  it('renders German copy', () => {
    setActiveLocale('de');

    const html = renderToString(<CompanionView {...props} />);

    expect(html).toContain('Schläge dieser Session');
    expect(html).toContain('Nur Anzeige. Bedienung am Gerät.');
  });

  it('opens on Live with a Dispersion tab beside it', () => {
    const html = renderToString(<CompanionView {...props} />);

    expect(html).toContain('aria-label="Views"');
    expect(html).toMatch(/aria-pressed="true"><span>Live<\/span>/);
    expect(html).toMatch(/aria-pressed="false"><span>Dispersion<\/span>/);
    expect(html).not.toContain('class="dispersion"');

    setActiveLocale('de');
    expect(renderToString(<CompanionView {...props} />)).toContain('<span>Streuung</span>');
  });

  it('waits for the first shot', () => {
    expect(renderToString(<CompanionView {...props} shots={[]} />)).toContain('Waiting for the first shot');
  });

  it('shows the demo band with its way out above everything while the demo runs', () => {
    const html = renderToString(<CompanionView {...props} demoActive />);

    expect(html).toMatch(/^<main[^>]*><div class="demo-band"/);
    expect(html).toContain('DEMO – real shots are ignored');
    expect(html).toMatch(/>End<\/button>/);
    expect(renderToString(<CompanionView {...props} />)).not.toContain('demo-band');
  });
});

describe('CompanionReplayPlayer', () => {
  it('plays inline, muted and looping at 0.25× by default with the slow-motion choices', () => {
    const html = renderToString(
      <CompanionReplayPlayer replay={replay} videoUrl="http://pi:8080/r2.mp4" onPlaybackError={() => {}} />
    );

    expect(html).toContain('src="http://pi:8080/r2.mp4"');
    expect(html).toMatch(/<video[^>]*muted=""[^>]*playsInline=""[^>]*loop=""/);
    expect(html).toContain('aria-pressed="true">0.25×');
    for (const speed of ['1×', '0.5×', '0.1×']) expect(html).toContain(`aria-pressed="false">${speed}`);
    expect(html).toContain('aria-label="Scrub replay"');
  });
});
