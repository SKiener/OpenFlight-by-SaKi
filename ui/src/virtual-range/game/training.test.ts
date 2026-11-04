import { afterEach, describe, expect, it, vi } from 'vitest';
import { fakeStorage } from '../../test-utils/fakeBrowser';
import { formatDistance } from '../../utils/units';
import { rotateAboutTee } from '../aim';
import { restingPoint, YARD_M, type RangeShot, type ScenePoint } from '../flightPath';
import { restOnRange } from '../target';
import {
  clampTarget,
  deviationAlongAim,
  loadClubAverages,
  loadTraining,
  NO_TRAINING,
  recordShot,
  runningFigures,
  saveTraining,
  scoreShots,
  seeShots,
  startTarget,
  TRAINING_KEY,
  trainingOf,
  withPlayer,
  withTarget,
  ZONES,
  zoneFor,
  type ClubAverages,
  vanishedShots,
  withoutShots,
  type TrainingState,
} from './training';

const averages = (overrides: Partial<ClubAverages> = {}): ClubAverages => ({
  club: '7-iron',
  total_yards: 160,
  total_count: 3,
  carry_yards: 150,
  carry_count: 3,
  ...overrides,
});

const path = (...points: [t: number, z: number][]): ScenePoint[] => points.map(([t, z]) => ({ t, x: 0, y: 0, z }));

const shot = (overrides: Partial<RangeShot>): RangeShot => ({
  id: 'a',
  club: '7-iron',
  ballSpeedMph: 110,
  carryM: 100,
  lateralM: 0,
  flight: path([0, 0], [2, -50], [4, -100]),
  ...overrides,
});

const fresh: TrainingState = { targetM: 100, recent: [] };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('zones', () => {
  it('sizes the zones in percent with a 1 m minimum', () => {
    expect(ZONES.map(({ zone, share, points }) => [zone, share, points])).toEqual([
      ['volltreffer', 0.03, 10],
      ['gut', 0.06, 5],
      ['getroffen', 0.1, 2],
    ]);
    for (const sign of [1, -1]) {
      expect(zoneFor(sign * 3, 100)).toBe('volltreffer');
      expect(zoneFor(sign * 3.01, 100)).toBe('gut');
      expect(zoneFor(sign * 6, 100)).toBe('gut');
      expect(zoneFor(sign * 6.01, 100)).toBe('getroffen');
      expect(zoneFor(sign * 10, 100)).toBe('getroffen');
      expect(zoneFor(sign * 10.01, 100)).toBe('outside');
    }
    expect(zoneFor(0, 100)).toBe('volltreffer');
    expect(zoneFor(1, 20)).toBe('volltreffer');
    expect(zoneFor(-1.01, 20)).toBe('gut');
    expect(zoneFor(1.2, 20)).toBe('gut');
    expect(zoneFor(2, 20)).toBe('getroffen');
    expect(zoneFor(2.01, 20)).toBe('outside');
    expect(zoneFor(1, 10)).toBe('volltreffer');
    expect(zoneFor(1.01, 10)).toBe('outside');
  });

  it('scores a spin-back shot by its rest point', () => {
    const spinBack = shot({ flight: path([0, 0], [2, -50], [4, -100]), roll: path([4, -100], [5, -97], [6, -96]) });

    const deviation = deviationAlongAim(restingPoint(spinBack), 100);

    expect(deviation).toBeCloseTo(-4, 12);
    expect(zoneFor(deviation, 100)).toBe('gut');
  });

  it('scores a shot without roll by its landing point', () => {
    const landed = shot({ flight: path([0, 0], [2, -50], [4, -104.5]), roll: null, totalM: 110 });

    const deviation = deviationAlongAim(restingPoint(landed), 100);

    expect(deviation).toBeCloseTo(4.5, 12);
    expect(zoneFor(deviation, 100)).toBe('gut');
    expect(deviationAlongAim({ x: 7, z: -88 }, 100)).toBeCloseTo(-12, 12);
  });
});

describe('the target', () => {
  it('starts at the average Total, else the carry, else 100 m', () => {
    expect(startTarget(averages())).toBe(Math.round(160 * YARD_M));
    expect(startTarget(averages({ total_count: 2 }))).toBe(Math.round(150 * YARD_M));
    expect(startTarget(averages({ total_yards: null, total_count: 0 }))).toBe(Math.round(150 * YARD_M));
    expect(startTarget(averages({ total_count: 2, carry_count: 2 }))).toBe(100);
    expect(startTarget(averages({ total_yards: null, total_count: 0, carry_yards: null, carry_count: 0 }))).toBe(100);
    expect(startTarget(null)).toBe(100);
    expect(startTarget(averages({ total_yards: 400 }))).toBe(300);
    expect(startTarget(averages({ total_yards: 5 }))).toBe(10);
  });

  it('stores the target in metres and reads it in either unit', () => {
    const storage = fakeStorage().localStorage;
    saveTraining(storage, { p1: withTarget(fresh, 137) });

    const target = loadTraining(storage).p1.targetM;

    expect(target).toBe(137);
    expect(formatDistance(target / YARD_M, 'metric')).toBe('137');
    expect(formatDistance(target / YARD_M, 'imperial')).toBe('150');
    expect(loadTraining(storage).p1.targetM).toBe(137);
  });

  it('clamps the target to 10–300 m in whole metres', () => {
    expect(clampTarget(137.4)).toBe(137);
    expect(clampTarget(9)).toBe(10);
    expect(clampTarget(301)).toBe(300);
    expect(clampTarget(Number.NaN)).toBe(100);
    expect(withTarget(fresh, 5)).toEqual({ targetM: 10, recent: [] });
  });
});

describe('the running figures', () => {
  it('keeps the last ten shots and their figures', () => {
    expect(runningFigures(fresh)).toEqual({ meanDeviationM: null, shareInside: null });

    let state = fresh;
    for (let i = 1; i <= 12; i++) state = recordShot(state, i % 2 === 0 ? i : -i);

    expect(state.targetM).toBe(100);
    expect(state.recent).toHaveLength(10);
    expect(state.recent[0]).toEqual({ deviationM: -3, zone: 'volltreffer' });
    expect(state.recent.at(-1)).toEqual({ deviationM: 12, zone: 'outside' });
    const figures = runningFigures(state);
    expect(figures.meanDeviationM).toBeCloseTo((-3 + 4 - 5 + 6 - 7 + 8 - 9 + 10 - 11 + 12) / 10, 12);
    expect(figures.shareInside).toBeCloseTo(0.8, 12);
    expect(recordShot(fresh, 2)).toEqual({ targetM: 100, recent: [{ deviationM: 2, zone: 'volltreffer' }] });
  });

  it('scores each shot against the target it was hit at', () => {
    const state = recordShot(withTarget(recordShot(fresh, 8), 50), 8);

    expect(state.recent.map(({ zone }) => zone)).toEqual(['getroffen', 'outside']);
  });
});

describe('the shots that count', () => {
  const feed = (origin: string | object, ids: string[], seenOnReplace = ids) => ({
    shots: ids.map((id) => ({ id })),
    origin,
    seenOnReplace,
  });

  it('counts none of the shots already there when the range opens', () => {
    const first = seeShots(null, feed('0/p1', ['a', 'b']));

    expect(first.fresh).toEqual([]);
    expect([...first.seen.ids]).toEqual(['a', 'b']);
    const processing = seeShots(null, feed('0/p1', ['a']), ['a', 'p']);
    expect(seeShots(processing.seen, feed('0/p1', ['a', 'p'])).fresh).toEqual([]);
  });

  it('counts each shot that arrives once, and keeps the same record while nothing arrives', () => {
    const { seen } = seeShots(null, feed('0/p1', ['a']));

    const next = seeShots(seen, feed('0/p1', ['a', 'b', 'c']));

    expect(next.fresh).toEqual(['b', 'c']);
    expect(seeShots(next.seen, feed('0/p1', ['a', 'b', 'c']))).toEqual({ seen: next.seen, fresh: [] });
    expect(seeShots(next.seen, feed('0/p1', ['a', 'b', 'c'])).seen).toBe(next.seen);
  });

  it("counts none of another profile's or session's shots, but the ones hit while a saved session was open", () => {
    const { seen } = seeShots(null, feed('0/p1', ['a']));
    const saved = { kind: 'saved' };

    expect(seeShots(seen, feed('0/p2', ['x', 'y'])).fresh).toEqual([]);
    const onSaved = seeShots(seen, feed(saved, ['old'], []));
    expect(seeShots(onSaved.seen, feed('0/p1', ['a', 'b'], ['a'])).fresh).toEqual(['b']);
  });

  it('scores the new shots by their rest point along the aim', () => {
    const shots = [
      shot({ id: 'a', flight: path([0, 0], [2, -50], [4, -96]) }),
      shot({ id: 'b', flight: path([0, 0], [2, -50], [4, -104]) }),
    ];

    const state = scoreShots(fresh, shots, ['b', 'a']);

    expect(state.recent).toEqual([
      { deviationM: 4, zone: 'gut', shotId: 'b' },
      { deviationM: -4, zone: 'gut', shotId: 'a' },
    ]);
    expect(scoreShots(fresh, shots, [])).toBe(fresh);
    expect(scoreShots(fresh, shots, ['gone'])).toBe(fresh);
  });

  it('the bearing does not change the score: a shot is measured along the aim it was hit with', () => {
    const shots = [shot({ id: 'a', flight: path([0, 0], [2, -50], [4, -96]) })];

    const scored = scoreShots(fresh, shots, ['a']).recent[0].deviationM;

    expect(scored).toBe(-4);
    for (const bearing of [0, 0.3, -0.2, Math.PI / 2]) {
      const rest = restOnRange(shots[0], bearing);
      const aim = rotateAboutTee({ x: 0, z: -1 }, bearing);
      expect(rest.x * aim.x + rest.z * aim.z - fresh.targetM, `bearing ${bearing}`).toBeCloseTo(scored, 9);
    }
  });
});

describe('training per player', () => {
  const anna = recordShot(withTarget(fresh, 137), -2);
  const ben = withTarget(fresh, 92);

  it("holds each player's training, and none for a player who has not started one", () => {
    const byPlayer = withPlayer(withPlayer(NO_TRAINING, 'anna', anna), 'ben', ben);

    expect(byPlayer).toEqual({ anna, ben });
    expect(trainingOf(byPlayer, 'anna')).toBe(anna);
    expect(trainingOf(byPlayer, 'ben')).toBe(ben);
    expect(trainingOf(byPlayer, 'cleo')).toBeNull();
    expect(trainingOf(byPlayer, 'toString')).toBeNull();
    expect(trainingOf(NO_TRAINING, '')).toBeNull();
  });

  it("replaces or ends only the given player's training", () => {
    const byPlayer = { anna, ben };
    const moved = withTarget(anna, 150);

    expect(withPlayer(byPlayer, 'anna', moved)).toEqual({ anna: moved, ben });
    expect(withPlayer(byPlayer, 'anna', null)).toEqual({ ben });
    expect(withPlayer(byPlayer, 'cleo', null)).toEqual(byPlayer);
    expect(byPlayer).toEqual({ anna, ben });
  });
});

describe('storage', () => {
  it('survives a reload', () => {
    const { localStorage, store } = fakeStorage();
    const state = recordShot(recordShot(withTarget(fresh, 142), -4.25), 1.5);

    saveTraining(localStorage, { p1: state, p2: fresh });

    expect(Object.keys(store)).toEqual([TRAINING_KEY]);
    expect(TRAINING_KEY).toBe('openflight.virtual-range.training:v1');
    expect(loadTraining(localStorage)).toEqual({ p1: state, p2: fresh });
    saveTraining(localStorage, NO_TRAINING);
    expect(loadTraining(localStorage)).toEqual({});
  });

  it('reads nothing from missing, broken or foreign state', () => {
    const load = (value: string) => loadTraining(fakeStorage({ [TRAINING_KEY]: value }).localStorage);

    expect(loadTraining(null)).toEqual({});
    expect(loadTraining(fakeStorage().localStorage)).toEqual({});
    expect(load('{')).toEqual({});
    expect(load('null')).toEqual({});
    expect(load('[]')).toEqual({});
    expect(load('{"p1":{"targetM":"100","recent":[]}}')).toEqual({});
    expect(load('{"p1":{"targetM":100}}')).toEqual({});
    expect(
      load(
        '{"p1":{"targetM":400,"recent":[{"deviationM":2,"zone":"gut"},{"deviationM":null,"zone":"gut"},{"zone":"x"}]},"p2":7}'
      )
    ).toEqual({ p1: { targetM: 300, recent: [{ deviationM: 2, zone: 'gut' }] } });
    expect(
      loadTraining({
        getItem: () => {
          throw new Error('blocked');
        },
        setItem: () => {},
      })
    ).toEqual({});
  });

  it('reads the earlier single-player format as no training', () => {
    const load = (value: string) => loadTraining(fakeStorage({ [TRAINING_KEY]: value }).localStorage);

    expect(load('{"targetM":137,"recent":[{"deviationM":2,"zone":"volltreffer"}]}')).toEqual({});
    expect(load('{"targetM":137,"recent":[]}')).toEqual({});
  });
});

describe('loadClubAverages', () => {
  const signal = new AbortController().signal;

  it("asks the server for the player's averages with the club", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify(averages({ club: 'wedge-54' }))));
    vi.stubGlobal('fetch', fetch);

    await expect(loadClubAverages('wedge-54', 'anna k&co', signal)).resolves.toEqual(averages({ club: 'wedge-54' }));
    expect(fetch).toHaveBeenCalledExactlyOnceWith(
      expect.stringMatching(/\/api\/club-averages\?club=wedge-54&profile=anna%20k%26co$/),
      { signal, cache: 'no-store' }
    );
    await loadClubAverages('wedge-54', '', signal);
    expect(fetch).toHaveBeenLastCalledWith(expect.stringMatching(/\?club=wedge-54$/), { signal, cache: 'no-store' });
  });

  it('gives null when the server fails or answers something else', async () => {
    for (const answer of [
      () => Promise.reject(new Error('offline')),
      async () => new Response('{}', { status: 500 }),
      async () => new Response('not json'),
      async () => new Response(JSON.stringify({ club: 'driver', total_yards: '200' })),
    ]) {
      vi.stubGlobal('fetch', vi.fn(answer));
      await expect(loadClubAverages('driver', 'p1', signal)).resolves.toBeNull();
    }
  });
});

describe('deleted shots', () => {
  const feed = (origin: string | object, ids: string[]) => ({
    shots: ids.map((id) => ({ id })),
    origin,
    seenOnReplace: ids,
  });

  it('drops only results of shots that vanish from the same feed', () => {
    const state = recordShot(recordShot(recordShot(fresh, 1, 'a'), 2, 'b'), 3);
    const shots = [shot({ id: 'c', flight: path([0, 0], [2, -50], [4, -98]) })];

    expect(scoreShots(fresh, shots, ['c']).recent).toEqual([{ deviationM: -2, zone: 'volltreffer', shotId: 'c' }]);
    expect(withoutShots(state, new Set(['b'])).recent.map(({ deviationM }) => deviationM)).toEqual([1, 3]);
    expect(withoutShots(state, new Set(['x'])), 'nothing to drop').toBe(state);
    expect(withoutShots(state, new Set(['b'])).targetM).toBe(100);

    expect(vanishedShots(feed('0/p1', ['a', 'b']), feed('0/p1', ['a']))).toEqual(['b']);
    expect(vanishedShots(feed('0/p1', ['a', 'b']), feed('0/p1', ['a', 'b', 'c']))).toEqual([]);
    expect(vanishedShots(feed('0/p1', ['a', 'b']), feed('0/p2', ['x'])), 'another profile').toEqual([]);
    expect(vanishedShots(feed('0/p1', ['a', 'b']), feed('1/p1', [])), 'a new session').toEqual([]);
    expect(vanishedShots(null, feed('0/p1', ['a']))).toEqual([]);
  });

  it('keeps the shot ids through a reload', () => {
    const { localStorage } = fakeStorage();
    const state = recordShot(recordShot(fresh, 1, 'a'), 2);

    saveTraining(localStorage, { p1: state });

    expect(loadTraining(localStorage)).toEqual({ p1: state });
    expect(loadTraining(localStorage).p1.recent[0].shotId).toBe('a');
    expect('shotId' in loadTraining(localStorage).p1.recent[1]).toBe(false);
  });
});
