import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Shot } from '../types/shot';
import { FINAL_VALUES_TIMEOUT_MS, ShotAnnouncer } from './shotAnnouncer';
import type { CalloutConfig } from './shotCallout';

function makeShot(timestamp: string, overrides: Partial<Shot> = {}): Shot {
  return {
    ball_speed_mph: 140,
    club_speed_mph: 100,
    smash_factor: 1.4,
    estimated_carry_yards: 200,
    carry_range: [195, 205],
    club: 'driver',
    timestamp,
    peak_magnitude: 100,
    launch_angle_vertical: 11,
    launch_angle_horizontal: null,
    launch_angle_confidence: null,
    angle_source: 'estimated',
    club_angle_deg: null,
    club_path_deg: null,
    spin_axis_deg: null,
    spin_rpm: null,
    spin_confidence: null,
    spin_quality: null,
    spin_source: null,
    carry_spin_adjusted: null,
    ...overrides,
  };
}

/** A final shot from the server: finalization always sets carry_spin_adjusted. */
function finalShot(timestamp: string, carry: number): Shot {
  return makeShot(timestamp, { carry_spin_adjusted: carry });
}

function setup(initial: Partial<CalloutConfig & { enabled: boolean }> = {}) {
  const config: CalloutConfig & { enabled: boolean } = {
    enabled: true,
    metrics: ['carry'],
    language: 'en',
    unitSystem: 'imperial',
    ...initial,
  };
  const spoken: Array<{ text: string; lang: string }> = [];
  const announcer = new ShotAnnouncer({
    speak: (text, lang) => spoken.push({ text, lang }),
    getConfig: () => config,
  });
  return { announcer, config, spoken, texts: () => spoken.map((entry) => entry.text) };
}

const PENDING = { iwr6843: true, camera: true };
const SKIPPED = { pending: {}, enrichment: { status: 'skipped' } };

describe('ShotAnnouncer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('speaks a shot without pending hardware at once', () => {
    const { announcer, spoken } = setup({ language: 'de', unitSystem: 'metric' });

    announcer.handleShot({ shot: makeShot('a') });

    expect(spoken).toEqual([{ text: 'Carry 183 Meter', lang: 'de-DE' }]);
  });

  it('treats an empty or all-false pending map as final', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: {} });
    announcer.handleShot({ shot: makeShot('b', { estimated_carry_yards: 210 }), pending: { iwr6843: false } });

    expect(texts()).toEqual(['Carry 200 yards', 'Carry 210 yards']);
  });

  it('waits for the final values when the TI radar or camera is still working', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    expect(texts()).toEqual([]);

    announcer.handleShotUpdate({ shot: finalShot('a', 214) });
    expect(texts()).toEqual(['Carry 214 yards']);

    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);
    expect(texts()).toEqual(['Carry 214 yards']);
  });

  it('keeps waiting through a "skipped" notice and speaks the final that follows it', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: { iwr6843: true } });
    announcer.handleShotUpdate({ shot: makeShot('a', { estimated_carry_yards: 230 }), ...SKIPPED });
    expect(texts()).toEqual([]);

    announcer.handleShotUpdate({ shot: finalShot('a', 214) });
    expect(texts()).toEqual(['Carry 214 yards']);
  });

  it('keeps waiting through an update that is still pending, and the fallback speaks its values', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShotUpdate({ shot: makeShot('a', { estimated_carry_yards: 207 }), pending: { camera: true } });
    expect(texts()).toEqual([]);

    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);
    expect(texts()).toEqual(['Carry 207 yards']);
  });

  it('falls back to the provisional values when the final ones never come, and ignores a late final', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS - 1);
    expect(texts()).toEqual([]);

    vi.advanceTimersByTime(1);
    expect(texts()).toEqual(['Carry 200 yards']);

    announcer.handleShotUpdate({ shot: finalShot('a', 214) });
    expect(texts()).toEqual(['Carry 200 yards']);
  });

  it('speaks every shot of a fast run, in shot order, each with its final values', () => {
    const { announcer, texts } = setup();

    // The hardware finishes one shot at a time, so later shots arrive before
    // earlier finals: a(pending) b(pending) final a, c(pending) final b, final c.
    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('a', 201) });
    announcer.handleShot({ shot: makeShot('c'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('b', 202) });
    announcer.handleShotUpdate({ shot: finalShot('c', 203) });

    expect(texts()).toEqual(['Carry 201 yards', 'Carry 202 yards', 'Carry 203 yards']);
  });

  it('gives every waiting shot its own fallback timer', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    vi.advanceTimersByTime(5_000);
    announcer.handleShot({ shot: makeShot('b', { estimated_carry_yards: 150 }), pending: PENDING });

    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS - 5_000);
    expect(texts()).toEqual(['Carry 200 yards']);
    vi.advanceTimersByTime(5_000);
    expect(texts()).toEqual(['Carry 200 yards', 'Carry 150 yards']);
  });

  it('keeps every shot of a long backlog, so a late final is still spoken', () => {
    const { announcer, texts } = setup();

    for (let index = 0; index < 6; index += 1) {
      announcer.handleShot({ shot: makeShot(`s${index}`), pending: PENDING });
    }
    for (let index = 0; index < 6; index += 1) {
      announcer.handleShotUpdate({ shot: finalShot(`s${index}`, 190 + index) });
    }

    expect(texts()).toEqual([190, 191, 192, 193, 194, 195].map((carry) => `Carry ${carry} yards`));
  });

  it('speaks a shot once even if its final values arrive twice', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('a', 214) });
    announcer.handleShotUpdate({ shot: finalShot('a', 215) });

    expect(texts()).toEqual(['Carry 214 yards']);
  });

  it('does not repeat a shot when the same shot event arrives again', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a') });
    announcer.handleShot({ shot: makeShot('a') });
    announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
    announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('b', 201) });

    expect(texts()).toEqual(['Carry 200 yards', 'Carry 201 yards']);
  });

  it('ignores updates for shots it is not waiting for', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('z', 201) });

    expect(texts()).toEqual([]);
  });

  describe('sync with the server list', () => {
    it('drops a waiting shot that was deleted or cleared', () => {
      const { announcer, texts } = setup();

      announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
      announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
      announcer.sync([makeShot('b')]);
      announcer.handleShotUpdate({ shot: finalShot('a', 201) });
      vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);

      expect(texts()).toEqual(['Carry 200 yards']);
    });

    it('speaks a shot whose final values arrived while the socket was down', () => {
      const { announcer, texts } = setup();

      announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
      announcer.sync([finalShot('a', 214)]);
      announcer.handleShotUpdate({ shot: finalShot('a', 214) });
      vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);

      expect(texts()).toEqual(['Carry 214 yards']);
    });

    it('keeps waiting for a shot that is not final yet, with the newer values', () => {
      const { announcer, texts } = setup();

      announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
      announcer.sync([makeShot('a', { estimated_carry_yards: 205 })]);
      expect(texts()).toEqual([]);

      vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);
      expect(texts()).toEqual(['Carry 205 yards']);
    });
  });

  it('forgets every waiting shot on reset', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
    announcer.reset();
    announcer.handleShotUpdate({ shot: finalShot('a', 201) });
    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);

    expect(texts()).toEqual([]);
  });

  it('stays silent while disabled', () => {
    const { announcer, texts } = setup({ enabled: false });

    announcer.handleShot({ shot: makeShot('a') });
    announcer.handleShot({ shot: makeShot('b'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('b', 201) });
    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);

    expect(texts()).toEqual([]);
  });

  it('stays silent when callouts are turned off while a shot waits', () => {
    const { announcer, config, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    config.enabled = false;
    announcer.handleShotUpdate({ shot: finalShot('a', 201) });

    expect(texts()).toEqual([]);
  });

  it('reads the settings when it speaks, not when the shot arrived', () => {
    const { announcer, config, spoken } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    config.language = 'de';
    config.unitSystem = 'metric';
    config.metrics = ['carry', 'ball_speed'];
    announcer.handleShotUpdate({ shot: makeShot('a') });

    expect(spoken).toEqual([{ text: 'Carry 183 Meter, Ballgeschwindigkeit 225', lang: 'de-DE' }]);
  });

  it('stays silent for a shot without the chosen values and still speaks the next one', () => {
    const { announcer, texts } = setup({ metrics: ['spin'] });

    announcer.handleShot({ shot: makeShot('a') });
    announcer.handleShot({
      shot: makeShot('b', { spin_rpm: 2500, carry_spin_rpm: 2500, carry_spin_source: 'measured' }),
    });

    expect(texts()).toEqual(['Spin 2500']);
  });

  it('waits longer than the server takes to give up on the hardware', () => {
    const server = readFileSync(fileURLToPath(new URL('../../../src/openflight/server.py', import.meta.url)), 'utf8');
    const deadline = /^_SHOT_ENRICHMENT_DEADLINE_S = ([\d.]+)$/m.exec(server);

    expect(deadline).not.toBeNull();
    expect(FINAL_VALUES_TIMEOUT_MS).toBeGreaterThanOrEqual(Number(deadline?.[1]) * 1000 + 2000);
  });

  it('never speaks again for a club-data shot_update after the announcement', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a'), pending: PENDING });
    announcer.handleShotUpdate({ shot: finalShot('a', 201), pending: {} });
    expect(texts()).toEqual(['Carry 201 yards']);

    // The calculated club data arrives a few seconds later on the same event.
    announcer.handleShotUpdate({
      shot: { ...finalShot('a', 201), club_data_status: 'done', camera_club_path_deg: 1.2 },
      pending: {},
    });
    vi.advanceTimersByTime(FINAL_VALUES_TIMEOUT_MS);
    expect(texts()).toEqual(['Carry 201 yards']);
  });

  it('does not hold back a shot whose club data is still pending: pending {} is final', () => {
    const { announcer, texts } = setup();

    announcer.handleShot({ shot: makeShot('a', { club_data_status: 'pending' }), pending: {} });

    expect(texts()).toEqual(['Carry 200 yards']);
  });
});
