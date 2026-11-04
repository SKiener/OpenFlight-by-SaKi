import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getEmptyValidationEntry } from '../../stores/useValidationStore';
import type { Shot } from '../../types/shot';
import { ClubDataSummary, ShotsPanel, ValidationEditor } from './ShotsPanel';
import { setActiveLocale } from '../../i18n';
import { UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../../test-utils/shot';

// zustand renders the initial state on the server, so the demo flags are set through the hooks.
const { flags, withFlags } = vi.hoisted(() => ({
  flags: { system: { demoActive: false }, shot: { sessionDemo: null as boolean | null } },
  withFlags: <S,>(store: { getState: () => S }, override: Partial<S>) =>
    Object.assign(<T,>(select: (state: S) => T): T => select({ ...store.getState(), ...override }), store),
}));
vi.mock('../../stores/useSystemStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../stores/useSystemStore')>();
  return { ...actual, useSystemStore: withFlags(actual.useSystemStore, flags.system) };
});
vi.mock('../../stores/useShotStore', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../stores/useShotStore')>();
  return { ...actual, useShotStore: withFlags(actual.useShotStore, flags.shot) };
});

const css = readFileSync(fileURLToPath(new URL('./panel.css', import.meta.url)), 'utf8');

function text(html: string): string {
  return html.replace(/<!-- -->/g, '');
}

function makeShot(overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 92,
    club_speed_mph: 68,
    smash_factor: 1.35,
    estimated_carry_yards: 210,
    carry_range: [205, 215],
    club: 'driver',
    profile_id: 'james',
    profile_name: 'James',
    timestamp: '2026-08-19T10:00:00Z',
    peak_magnitude: 100,
    launch_angle_vertical: 13.4,
    launch_angle_horizontal: null,
    launch_angle_confidence: 0.8,
    angle_source: 'radar',
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: 2650,
    spin_confidence: 0.9,
    spin_quality: 'high',
    spin_source: 'measured',
    spin_method: null,
    carry_spin_adjusted: null,
    carry_spin_rpm: 2650,
    carry_spin_source: 'measured',
    ...overrides,
  };
}

const render = (shots: Shot[]) =>
  text(
    renderToString(
      <ShotsPanel shots={shots} profileId="james" profileName="James" onDeleteShot={() => {}} onReplayShot={() => {}} />
    )
  );

describe('ShotsPanel', () => {
  it('shows an empty state before any shots', () => {
    const html = render([]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('shots-panel__row-main');
    // Nothing to upload or export yet.
    expect(html).toContain('disabled=""');
  });

  it('makes Export CSV the primary header action', () => {
    const html = render([makeShot()]);
    const header = html.match(/<header class="panel-header">[\s\S]*?<\/header>/)?.[0] ?? '';
    const exportButton = header.match(/<button[^>]*>[\s\S]*?Export CSV[\s\S]*?<\/button>/)?.[0];
    const uploadButton = header.match(/<button[^>]*>[\s\S]*?Upload[\s\S]*?<\/button>/)?.[0];

    expect(exportButton).toContain('panel-action--primary');
    expect(uploadButton).toContain('panel-action--secondary');
  });

  it('exposes the shot rows as a drag-scrollable region', () => {
    const html = render([makeShot()]);

    expect(html).toContain('shots-panel__rows');
    expect(html).toContain('aria-label="Recorded shots"');
    expect(html).toContain('role="region"');
  });

  it('renders the seven columns the mockup draws', () => {
    const html = render([makeShot()]);

    for (const column of ['Shot', 'Profile', 'Ball', 'Club', 'Launch', 'Spin', 'Carry']) {
      expect(html).toContain(`>${column}<`);
    }
  });

  it('numbers rows newest-first', () => {
    const html = render([makeShot({ timestamp: 'a' }), makeShot({ timestamp: 'b' }), makeShot({ timestamp: 'c' })]);
    const indexes = [...html.matchAll(/shots-panel__index">(\d+)</g)].map((m) => m[1]);

    expect(indexes).toEqual(['3', '2', '1']);
  });

  it('renders each metric column for a ball-strike shot', () => {
    const html = render([makeShot()]);

    expect(html).toContain('>92.0<');
    expect(html).toContain('>68.0<');
    expect(html).toContain('>13.4<');
    expect(html).toContain('>2,650<');
    expect(html).toContain('>210<');
    expect(html).toContain('shots-panel__value--accent');
  });

  it('lists the carry the live tile shows: spin-adjusted, else the estimate', () => {
    const html = render([makeShot({ estimated_carry_yards: 210, carry_spin_adjusted: 198 })]);

    expect(html).toContain('>198<');
    expect(html).not.toContain('>210<');
  });

  it('marks a typical-spin carry with ≈ and shows no span, not even in the tooltip', () => {
    const html = render([
      makeShot({
        club: '7-iron',
        spin_axis_deg: 2,
        carry_spin_adjusted: 150,
        carry_spin_source: 'club_typical',
        // The span the server still sends; no screen shows it.
        ...{ carry_span_min_yards: 141, carry_span_max_yards: 160 },
      }),
    ]);

    expect(html).toContain('title="Estimated with typical spin">≈150<');
    expect(html).not.toContain('141');
    expect(html).not.toContain('160');
  });

  it('leaves a measured carry unmarked', () => {
    const html = render([makeShot({ club: '7-iron', spin_axis_deg: 2, carry_spin_adjusted: 150 })]);

    expect(html).toContain('shots-panel__value--accent">150<');
    expect(html).not.toContain('≈150');
  });

  it('renders placeholders rather than blanks for missing values', () => {
    const html = render([
      makeShot({ club_speed_mph: null, launch_angle_vertical: null, spin_rpm: null, carry_spin_rpm: null }),
    ]);

    expect(html.match(/shots-panel__value">—</g)).toHaveLength(3);
  });

  it('shows the spin the carry used, with ≈ when it was not measured', () => {
    const typical = makeShot({
      spin_rpm: 1538,
      spin_confidence: 0.39,
      spin_quality: 'low',
      spin_source: null,
      carry_spin_rpm: 7500,
      carry_spin_source: 'club_typical',
    });
    const html = render([typical]);

    expect(html).toContain('shots-panel__value">≈7,500<');
    expect(html).not.toContain('1,538');
  });

  it('shows a dash, never the raw spin, for a shot without carry spin', () => {
    const html = render([makeShot({ spin_rpm: 6000, carry_spin_rpm: undefined, carry_spin_source: undefined })]);

    expect(html).not.toContain('6,000');
    expect(html.match(/shots-panel__value">—</g)).toHaveLength(1);
  });

  it('reports how many shots have a comparator speed', () => {
    const html = render([makeShot({ timestamp: 'a' }), makeShot({ timestamp: 'b' })]);

    expect(html).toContain('2 recorded · 0/2 validated');
  });

  it('keeps rows collapsed until tapped, so the validation editor stays out of the grid', () => {
    const html = render([makeShot()]);

    expect(html).toContain('aria-expanded="false"');
    expect(html).not.toContain('shots-panel__validation');
  });

  it('shows a delete control per row', () => {
    const html = render([makeShot({ timestamp: 'a' }), makeShot({ timestamp: 'b' })]);

    expect(html.match(/shots-panel__delete/g)).toHaveLength(2);
    expect(html).toContain('aria-label="Delete shot 2"');
  });

  it('shows replay only for shots backed by a camera capture', () => {
    const html = render([
      makeShot({ timestamp: 'a' }),
      makeShot({
        timestamp: 'b',
        camera_replay: {
          id: 'replay-123',
          frame_count: 99,
          trigger_frame: 73,
          playback_fps: 60,
          duration_seconds: 1.65,
          display_mirror_horizontal: true,
        },
      }),
    ]);

    expect(html.match(/shots-panel__replay/g)).toHaveLength(1);
    expect(html).toContain('aria-label="Replay shot 2"');
  });

  it('labels a swing-speed row with its implement', () => {
    const html = render([
      makeShot({
        mode: 'swing-speed',
        club: 'Swing Speed',
        training_implement_label: 'Stack 100g',
        swing_speed_reading_count: 7,
        swing_speed_trigger_mph: 60,
        swing_speed_duration_ms: 120,
      }),
    ]);

    expect(html).toContain('>Stack 100g<');
    expect(html).toContain('>7<');
    expect(html).toContain('>120<');
  });

  it("lists only the current profile's shots", () => {
    const html = render([
      makeShot({ timestamp: 'a', ball_speed_mph: 92 }),
      makeShot({ profile_id: 'alex', profile_name: 'Alex', timestamp: 'b', ball_speed_mph: 140 }),
    ]);
    const indexes = [...html.matchAll(/shots-panel__index">(\d+)</g)].map((m) => m[1]);

    expect(html).toContain('>92.0<');
    expect(html).not.toContain('>140.0<');
    expect(html).not.toContain('>Alex<');
    expect(html).toContain('1 recorded · 0/1 validated');
    expect(indexes).toEqual(['1']);
  });

  it('shows the empty state when only other profiles have shots', () => {
    const html = render([makeShot({ profile_id: 'alex', profile_name: 'Alex' })]);

    expect(html).toContain('No shots yet');
    expect(html).not.toContain('shots-panel__row-main');
  });

  it('paints the list on the same surface as the header', () => {
    expect(render([makeShot()])).toContain('panel shots-panel');
    expect(css).toMatch(/\.panel\.shots-panel \{[^}]*background: var\(--color-surface\)/);
  });
});

describe('ShotsPanel during the demo', () => {
  afterEach(() => {
    flags.system.demoActive = false;
    flags.shot.sessionDemo = null;
  });

  const exportButton = (html: string) => html.match(/<button[^>]*>Export CSV<\/button>/)?.[0] ?? '';

  it('exports nothing while the demo runs, and the real session again after it', () => {
    flags.system.demoActive = true;
    expect(exportButton(render([makeShot()]))).toContain('disabled=""');

    flags.system.demoActive = false;
    expect(exportButton(render([makeShot()]))).not.toContain('disabled');
  });

  it('waits for the real session: on switch-off demo_state comes first, the demo rows stay until it', () => {
    flags.shot.sessionDemo = true;
    expect(exportButton(render([makeShot()]))).toContain('disabled=""');

    flags.shot.sessionDemo = false;
    expect(exportButton(render([makeShot()]))).not.toContain('disabled');
  });

  it('locks the comparator fields, so no demo row reaches localStorage', () => {
    const editor = (disabled: boolean) =>
      renderToString(
        <ValidationEditor shot={makeShot()} entry={getEmptyValidationEntry()} onUpdate={() => {}} disabled={disabled} />
      );

    expect(editor(true).match(/<(select|input)[^>]* disabled=""/g)).toHaveLength(3);
    expect(editor(false)).not.toContain('disabled');
  });
});

describe('ClubDataSummary (expanded row)', () => {
  afterEach(() => setActiveLocale('en'));

  it.each([
    ['de', 'Pfad +2,1° · Eintreffw. -4,3° (unkalibriert)'],
    ['en', 'Path +2.1° · Attack -4.3° (uncalibrated)'],
    ['es', 'Path +2,1° · Ataque -4,3° (sin calibrar)'],
  ] as const)(
    '%s: one wrapped ≈ line with path and attack only, never the face, the axis or the impact',
    (locale, line) => {
      setActiveLocale(locale);
      const html = text(
        renderToString(
          <ClubDataSummary
            shot={makeShot({
              ...UNMEASURABLE_CLUB_DATA,
              club: '8-iron',
              camera_club_path_deg: 2.1,
              camera_attack_angle_deg: -4.3,
            })}
          />
        )
      );
      expect(html).toContain('class="shots-panel__club-data"');
      expect(html).toContain(line);
      for (const word of UNMEASURABLE_COPY[locale]) expect(html, word).not.toContain(word);
      expect(html).not.toMatch(/Fläche|Face|Cara|12[.,]2|19[.,]1|mm/);
      expect(css).toMatch(/\.shots-panel__club-data \{[^}]*overflow-wrap: anywhere/);
    }
  );

  it('renders nothing for a shot with only the club data no screen shows', () => {
    expect(renderToString(<ClubDataSummary shot={makeShot({ ...UNMEASURABLE_CLUB_DATA })} />)).toBe('');
  });

  it('renders nothing for a shot without calculated club data', () => {
    expect(renderToString(<ClubDataSummary shot={makeShot()} />)).toBe('');
  });
});
