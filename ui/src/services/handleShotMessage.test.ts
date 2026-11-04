import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Shot } from '../types/shot';
import { installFakeBrowser, spokenAloud } from '../test-utils/fakeBrowser';

const ballShot = {
  mode: 'rolling-buffer',
  ball_speed_mph: 145,
  club: '7i',
  timestamp: '2026-08-23T12:00:00Z',
} as Shot;

class FakeAudioNode {
  connect() {
    return this;
  }
}

class FakeOscillator extends FakeAudioNode {
  type = 'sine';
  frequency = { setValueAtTime() {} };
  started = false;

  start() {
    this.started = true;
  }

  stop() {}
}

class FakeGain extends FakeAudioNode {
  gain = {
    setValueAtTime() {},
    exponentialRampToValueAtTime() {},
  };
}

class FakeAudioContext {
  state = 'running';
  currentTime = 0;
  destination = {};
  oscillators: FakeOscillator[] = [];

  resume() {
    return Promise.resolve();
  }

  createOscillator() {
    const oscillator = new FakeOscillator();
    this.oscillators.push(oscillator);
    return oscillator;
  }

  createGain() {
    return new FakeGain();
  }
}

describe('handleShotMessage', () => {
  afterEach(() => {
    vi.resetModules();
    vi.unstubAllGlobals();
  });

  it('plays a beep when a live ball shot is received', async () => {
    const context = new FakeAudioContext();
    vi.stubGlobal('window', {
      AudioContext: function AudioContext() {
        return context;
      },
    } as unknown as Window & typeof globalThis);

    const { handleShotMessage } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');
    useShotStore.setState({
      latestShot: null,
      shots: [],
      isNewShot: false,
      shotProcessingPhase: null,
      shotVersion: 0,
    });

    handleShotMessage({
      shot: ballShot,
      stats: {
        shot_count: 1,
        avg_ball_speed: 145,
        max_ball_speed: 145,
        min_ball_speed: 145,
        avg_club_speed: null,
        avg_smash_factor: null,
        avg_carry_est: 0,
      },
    });

    expect(useShotStore.getState().latestShot).toEqual(ballShot);
    expect(context.oscillators.some((oscillator) => oscillator.started)).toBe(true);
  });

  it('updates late hardware metrics without replaying the shot cue', async () => {
    const context = new FakeAudioContext();
    vi.stubGlobal('window', {
      AudioContext: function AudioContext() {
        return context;
      },
    } as unknown as Window & typeof globalThis);

    const { handleShotUpdate } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');
    useShotStore.setState({ latestShot: ballShot, shots: [ballShot], shotVersion: 1 });
    const enriched = { ...ballShot, launch_angle_vertical: 17.4 } as Shot;

    handleShotUpdate({
      shot: enriched,
      stats: {
        shot_count: 1,
        avg_ball_speed: 145,
        max_ball_speed: 145,
        min_ball_speed: 145,
        avg_club_speed: null,
        avg_smash_factor: null,
        avg_carry_est: 0,
      },
    });

    expect(useShotStore.getState().latestShot).toEqual(enriched);
    expect(context.oscillators.some((oscillator) => oscillator.started)).toBe(false);
  });

  it('keeps IWR dump feedback active after provisional OPS metrics arrive', async () => {
    vi.stubGlobal('window', {} as Window & typeof globalThis);

    const { handleShotMessage } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');
    useShotStore.setState({
      latestShot: null,
      shots: [],
      shotProcessingPhase: 'calculating',
      shotProcessingShotTimestamp: null,
    });

    handleShotMessage({
      shot: ballShot,
      stats: {
        shot_count: 1,
        avg_ball_speed: 145,
        max_ball_speed: 145,
        min_ball_speed: 145,
        avg_club_speed: null,
        avg_smash_factor: null,
        avg_carry_est: 0,
      },
      pending: { iwr6843: true },
    });

    expect(useShotStore.getState().latestShot).toEqual(ballShot);
    expect(useShotStore.getState().shotProcessingPhase).toBe('iwr_dump');
    expect(useShotStore.getState().shotProcessingShotTimestamp).toBe(ballShot.timestamp);
  });

  it('keeps camera processing feedback active after provisional OPS metrics arrive', async () => {
    vi.stubGlobal('window', {} as Window & typeof globalThis);

    const { handleShotMessage } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');

    handleShotMessage({
      shot: ballShot,
      stats: {
        shot_count: 1,
        avg_ball_speed: 145,
        max_ball_speed: 145,
        min_ball_speed: 145,
        avg_club_speed: null,
        avg_smash_factor: null,
        avg_carry_est: 0,
      },
      pending: { camera: true },
    });

    expect(useShotStore.getState().shotProcessingPhase).toBe('camera_processing');
    expect(useShotStore.getState().shotProcessingShotTimestamp).toBe(ballShot.timestamp);
  });

  it('clears hardware feedback immediately when enrichment is skipped', async () => {
    vi.stubGlobal('window', {} as Window & typeof globalThis);

    const { handleShotMessage, handleShotUpdate } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');
    const stats = {
      shot_count: 1,
      avg_ball_speed: 145,
      max_ball_speed: 145,
      min_ball_speed: 145,
      avg_club_speed: null,
      avg_smash_factor: null,
      avg_carry_est: 0,
    };

    handleShotMessage({ shot: ballShot, stats, pending: { iwr6843: true } });
    handleShotUpdate({
      shot: ballShot,
      stats,
      pending: {},
      enrichment: {
        status: 'skipped',
        reason: 'queue_full',
        hardware: ['iwr6843'],
      },
    });

    const state = useShotStore.getState();
    expect(state.shotProcessingPhase).toBeNull();
    expect(state.shotProcessingShotTimestamp).toBeNull();
    expect(state.shots).toEqual([ballShot]);
  });

  describe('voice callouts', () => {
    const stats = {
      shot_count: 1,
      avg_ball_speed: 145,
      max_ball_speed: 145,
      min_ball_speed: 145,
      avg_club_speed: null,
      avg_smash_factor: null,
      avg_carry_est: 0,
    };
    const fullShot = {
      ...ballShot,
      club_speed_mph: 104,
      smash_factor: 1.39,
      estimated_carry_yards: 180,
      carry_spin_adjusted: null,
      launch_angle_vertical: 14,
      spin_rpm: 5100,
    } as Shot;

    /** A phone page with the server's setting already received. */
    async function seeded(settings: object, { phone = true } = {}) {
      const { synth } = installFakeBrowser({ phone });
      const { useVoiceCalloutStore } = await import('../stores/useVoiceCalloutStore');
      useVoiceCalloutStore.getState().applyServerSettings(settings);
      return synth;
    }

    it('speaks a shot once its hardware values are final, not at the skipped notice', async () => {
      const synth = await seeded({ enabled: true, metrics: ['carry', 'ball_speed'], language: 'de' });
      const { handleShotMessage, handleShotUpdate } = await import('./handleShotMessage');

      handleShotMessage({ shot: fullShot, stats, pending: { iwr6843: true, camera: true } });
      handleShotUpdate({
        shot: fullShot,
        stats,
        pending: {},
        enrichment: { status: 'skipped', reason: 'queue_full', hardware: ['iwr6843'] },
      });
      expect(spokenAloud(synth)).toEqual([]);

      handleShotUpdate({ shot: { ...fullShot, carry_spin_adjusted: 186 }, stats });
      expect(spokenAloud(synth)).toEqual([{ text: 'Carry 186 Yards, Ballgeschwindigkeit 145', lang: 'de-DE' }]);
    });

    it('speaks a shot without pending hardware right away', async () => {
      const synth = await seeded({ enabled: true, metrics: ['carry'], language: 'en' });
      const { handleShotMessage } = await import('./handleShotMessage');

      handleShotMessage({ shot: fullShot, stats });

      expect(spokenAloud(synth)).toEqual([{ text: 'Carry 180 yards', lang: 'en-US' }]);
    });

    it('stays silent while callouts are off', async () => {
      const synth = await seeded({ enabled: false, metrics: ['carry'], language: 'en' });
      const { handleShotMessage } = await import('./handleShotMessage');
      const { useVoiceCalloutStore } = await import('../stores/useVoiceCalloutStore');

      handleShotMessage({ shot: fullShot, stats });

      // The seeded setting was read (not a default that happens to be off).
      expect(useVoiceCalloutStore.getState().metrics).toEqual(['carry']);
      expect(useVoiceCalloutStore.getState().enabled).toBe(false);
      expect(spokenAloud(synth)).toEqual([]);
    });

    it('never speaks on the kiosk, even with callouts on', async () => {
      const synth = await seeded({ enabled: true, metrics: ['carry'], language: 'en' }, { phone: false });
      const { handleShotMessage, handleShotUpdate } = await import('./handleShotMessage');

      handleShotMessage({ shot: fullShot, stats });
      handleShotMessage({ shot: { ...fullShot, timestamp: 'k2' }, stats, pending: { iwr6843: true } });
      handleShotUpdate({ shot: { ...fullShot, timestamp: 'k2', carry_spin_adjusted: 186 }, stats });

      expect(synth.spoken).toEqual([]);
    });
  });

  it('takes a later camera spin axis: the curve tile, the range flight and the carry note follow', async () => {
    vi.stubGlobal('window', {} as Window & typeof globalThis);

    const { handleShotUpdate } = await import('./handleShotMessage');
    const { useShotStore } = await import('../stores/useShotStore');
    const { curveMetric } = await import('../components/panel/liveMetrics');
    const { carryNotes } = await import('../utils/carryDisplay');
    const { shotFeed } = await import('../virtual-range/rangeViewModel');
    const { t } = await import('../i18n');
    const stats = {
      shot_count: 1,
      avg_ball_speed: 120,
      max_ball_speed: 120,
      min_ball_speed: 120,
      avg_club_speed: null,
      avg_smash_factor: null,
      avg_carry_est: 0,
    };
    const published = {
      ...ballShot,
      club: '8-iron',
      profile_id: 'p1',
      carry_spin_adjusted: 140,
      carry_lateral_yards: -4.1,
      spin_axis_deg: null,
      curve_class: null,
      flight_path: { unit: 'yd', points: [[0, 0, 0, 0], [5.2, 140, -4.1, 0]] },
    } as unknown as Shot;
    useShotStore.setState({ latestShot: published, shots: [published], shotVersion: 1 });
    const straight = shotFeed({ kind: 'live' }, useShotStore.getState().shots, 'p1').shots[0];
    expect(curveMetric(published).value).toBe('—');
    expect(carryNotes(published)).toContain(t('carry.curveIgnored'));

    // The club data's shot_update: the camera axis, its class and the shot flown again.
    const curved = {
      ...published,
      spin_axis_deg: -4.2,
      spin_axis_source: 'calculated',
      curve_class: 'draw',
      carry_spin_adjusted: 138.5,
      carry_lateral_yards: -9.6,
      flight_path: { unit: 'yd', points: [[0, 0, 0, 0], [5.1, 138.5, -9.6, 0]] },
    } as unknown as Shot;
    handleShotUpdate({ shot: curved, stats, pending: {} });

    const latest = useShotStore.getState().latestShot as Shot;
    expect(curveMetric(latest).value).toBe(t('curve.draw'));
    expect(carryNotes(latest)).not.toContain(t('carry.curveIgnored'));
    const range = shotFeed({ kind: 'live' }, useShotStore.getState().shots, 'p1').shots;
    expect(range).toHaveLength(1);
    expect(range[0].id).toBe(straight.id);
    expect(range[0].lateralM).not.toBe(straight.lateralM);
    expect(range[0].flight?.at(-1)).not.toEqual(straight.flight?.at(-1));
  });
});
