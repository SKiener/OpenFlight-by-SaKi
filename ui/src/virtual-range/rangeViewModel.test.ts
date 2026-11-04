import { describe, expect, it } from 'vitest';
import { makeShot } from '../test-utils/shot';
import type { FlightPathPoint, Shot } from '../types/shot';
import { greenBearing, NO_AIMS, rotateAboutTee } from './aim';
import { shownTotalM, YARD_M, type RangeShot } from './flightPath';
import { GREENS } from './layout';
import {
  gainedFlight,
  hitMeanwhile,
  knownToday,
  nextAnimation,
  rangeNotice,
  rangeReadout,
  shotFeed,
  visibleRangeShots,
  type RangeAim,
  type RangeSource,
} from './rangeViewModel';

const rangeBar = (...args: Parameters<typeof rangeReadout>) => rangeReadout(...args).bar;
const rangeTarget = (...args: Parameters<typeof rangeReadout>) => rangeReadout(...args).target;

const LIVE: RangeSource = { kind: 'live' };

const PATH: FlightPathPoint[] = [
  [0, 0, 0, 0],
  [1, 60, 2, 20],
  [2, 120, 3, 0],
];

const shot = (timestamp: string, profileId: string, overrides: Partial<Shot> = {}): Shot =>
  makeShot({ timestamp, profile_id: profileId, flight_path: { unit: 'yd', points: PATH }, ...overrides });

const rangeShot = (id: string, overrides: Partial<RangeShot> = {}): RangeShot => ({
  id,
  club: 'driver',
  ballSpeedMph: 150,
  carryM: 200,
  lateralM: 0,
  flight: null,
  ...overrides,
});

const a = rangeShot('a');
const b = rangeShot('b');
const ids = (shots: RangeShot[]): string[] => shots.map((s) => s.id);

describe('nextAnimation', () => {
  it('animates a shot that arrived while the scene was loading', () => {
    expect(nextAnimation([], [a], false, null)).toEqual({ animateId: null, pending: 'a' });
    expect(nextAnimation([], [a], true, 'a')).toEqual({ animateId: 'a', pending: null });
    // By the time the scene is ready the view has already seen 'a': the kept shot still flies.
    expect(nextAnimation(['a'], [a], true, 'a')).toEqual({ animateId: 'a', pending: null });
  });

  it('animates only a new newest shot', () => {
    expect(nextAnimation(['a'], [a, b], true, null)).toEqual({ animateId: 'b', pending: null });
    expect(nextAnimation(['a', 'b'], [a, b], true, null)).toEqual({ animateId: null, pending: null });
  });

  it('does not replay the newest shot when the one after it is deleted', () => {
    expect(nextAnimation(['a', 'b'], [a], true, null)).toEqual({ animateId: null, pending: null });
  });

  it('keeps a waiting shot until the scene is ready', () => {
    expect(nextAnimation(['a'], [a], false, 'a')).toEqual({ animateId: null, pending: 'a' });
  });

  it('waits for the newest of several shots that arrived while loading', () => {
    expect(nextAnimation(['a'], [a, b], false, 'a')).toEqual({ animateId: null, pending: 'b' });
    expect(nextAnimation(['a'], [a, b], true, 'a')).toEqual({ animateId: 'b', pending: null });
  });

  it('forgets a waiting shot that left the list', () => {
    expect(nextAnimation(['b'], [b], false, 'a')).toEqual({ animateId: null, pending: null });
    expect(nextAnimation(['b'], [b], true, 'a')).toEqual({ animateId: null, pending: null });
  });

  it('has nothing to animate without shots', () => {
    expect(nextAnimation([], [], true, null)).toEqual({ animateId: null, pending: null });
    expect(nextAnimation(['a'], [], false, 'a')).toEqual({ animateId: null, pending: null });
  });
});

describe('gainedFlight', () => {
  const flight = [
    { t: 0, x: 0, y: 0, z: 0 },
    { t: 1, x: 0, y: 5, z: -50 },
  ];
  const landingPoint = rangeShot('a');
  const flying = rangeShot('a', { flight });

  it('names the newest shot once the server adds its flight path to the landing point', () => {
    expect(gainedFlight([landingPoint], [flying])).toBe('a');
    expect(gainedFlight([rangeShot('x'), landingPoint], [rangeShot('x'), flying])).toBe('a');
  });

  it('is silent when the flight was there before, or never comes', () => {
    expect(gainedFlight([flying], [flying])).toBeNull();
    expect(gainedFlight([landingPoint], [landingPoint])).toBeNull();
    expect(gainedFlight([flying], [landingPoint])).toBeNull();
  });

  it('is silent for a shot that is new, or no longer the newest', () => {
    expect(gainedFlight([], [flying])).toBeNull();
    expect(gainedFlight([landingPoint, b], [flying, b])).toBeNull();
    expect(gainedFlight([landingPoint], [])).toBeNull();
  });
});

describe('visibleRangeShots', () => {
  const shots = [shot('1', 'p1'), shot('2', 'p2'), shot('3', 'p1')];

  it("switches to the new profile's shots without replaying one", () => {
    expect(ids(visibleRangeShots(LIVE, shots, 'p1'))).toEqual(['1', '3']);
    expect(ids(visibleRangeShots(LIVE, shots, 'p2'))).toEqual(['2']);

    const feed = shotFeed(LIVE, shots, 'p2');
    expect(ids(feed.shots)).toEqual(['2']);
    expect(nextAnimation(feed.seenOnReplace, feed.shots, true, null)).toEqual({ animateId: null, pending: null });
    expect(nextAnimation(feed.seenOnReplace, feed.shots, false, '3')).toEqual({ animateId: null, pending: null });
  });

  it('shows every shot of a saved session', () => {
    const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [shot('1', 'p1'), shot('2', 'p2')] };

    expect(ids(visibleRangeShots(saved, [shot('9', 'p1')], 'p1'))).toEqual(['1', '2']);
  });

  it('shows a shot without a flight path as a landing point', () => {
    const noPath = shot('x', 'p1', { flight_path: null, carry_spin_adjusted: 133, carry_lateral_yards: 4 });

    const [landing] = visibleRangeShots(LIVE, [noPath], 'p1');

    expect(landing).toMatchObject({ id: 'x', flight: null });
    expect(landing.carryM).toBeCloseTo(133 * YARD_M, 9);
    expect(landing.lateralM).toBeCloseTo(4 * YARD_M, 9);
    expect(rangeBar([landing], null)?.carryM).toBeCloseTo(133 * YARD_M, 9);
    expect(rangeBar([landing], { shotId: 'x', carryM: landing.carryM, landed: true })?.carryM).toBeCloseTo(
      133 * YARD_M,
      9
    );
  });

  it('converts a shot object once: the same object gives the same range shot, a replaced one a new one', () => {
    const first = shot('1', 'p1');
    const [converted] = visibleRangeShots(LIVE, [first], 'p1');

    expect(visibleRangeShots(LIVE, [first, shot('2', 'p1')], 'p1')[0]).toBe(converted);
    expect(visibleRangeShots({ kind: 'saved', label: 'Oct 7', shots: [first] }, [], '')[0]).toBe(converted);
    expect(visibleRangeShots(LIVE, [{ ...first, carry_spin_adjusted: 150 }], 'p1')[0]).not.toBe(converted);
  });

  it('leaves out swing-speed reps and shows nothing without a profile', () => {
    const swing = shot('s', 'p1', { mode: 'swing-speed' });

    expect(ids(visibleRangeShots(LIVE, [swing, shot('1', 'p1')], 'p1'))).toEqual(['1']);
    expect(visibleRangeShots(LIVE, shots, '')).toEqual([]);
  });
});

describe('shotFeed', () => {
  const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [shot('1', 'p1'), shot('2', 'p1')] };

  it("counts a profile's shots as seen", () => {
    const feed = shotFeed(LIVE, [shot('1', 'p1'), shot('2', 'p1')], 'p1');

    expect(feed.seenOnReplace).toEqual(['1', '2']);
  });

  it('opens a saved session with nothing seen, so its newest shot flies once', () => {
    const feed = shotFeed(saved, [], 'p1');

    expect(feed.origin).toBe(saved);
    expect(feed.seenOnReplace).toEqual([]);
    expect(nextAnimation(feed.seenOnReplace, feed.shots, true, null)).toEqual({ animateId: '2', pending: null });
  });

  it('counts only the shots given as seen when today replaces a saved session, so one hit meanwhile flies', () => {
    const feed = shotFeed(LIVE, [shot('1', 'p1'), shot('2', 'p1')], 'p1', { seen: new Set(['1']) });

    expect(ids(feed.shots)).toEqual(['1', '2']);
    expect(feed.seenOnReplace).toEqual(['1']);
    expect(nextAnimation(feed.seenOnReplace, feed.shots, true, null)).toEqual({ animateId: '2', pending: null });
  });

  describe('a provisional live shot (no final carry yet)', () => {
    const provisional = (id: string) =>
      shot(id, 'p1', { carry_spin_adjusted: null, estimated_carry_yards: 180, flight_path: null });
    const today = [shot('1', 'p1'), provisional('2')];

    it('stays off the range and unseen, whatever the store marks as processing', () => {
      const feed = shotFeed(LIVE, today, 'p1');

      expect(ids(feed.shots)).toEqual(['1']);
      expect(feed.seenOnReplace).toEqual(['1']);
      expect(feed.holding).toBe(true);
    });

    it('comes onto the range once its own update brings the final carry', () => {
      const feed = shotFeed(LIVE, [today[0], { ...today[1], carry_spin_adjusted: 152 }], 'p1');

      expect(ids(feed.shots)).toEqual(['1', '2']);
      expect(feed.holding).toBe(false);
    });

    it('keeps an older provisional shot off the range while a newer one is held too', () => {
      const feed = shotFeed(LIVE, [shot('1', 'p1'), provisional('2'), provisional('3')], 'p1');

      expect(ids(feed.shots)).toEqual(['1']);
      expect(feed.holding).toBe(true);
    });

    it('holds the bar only while the newest shot is provisional', () => {
      const feed = shotFeed(LIVE, [provisional('1'), shot('2', 'p1')], 'p1');

      expect(ids(feed.shots)).toEqual(['2']);
      expect(feed.holding).toBe(false);
    });

    it('holds nothing back for another profile or in a saved session', () => {
      const otherProfile = shotFeed(LIVE, today, 'p2');
      const savedToday = shotFeed({ kind: 'saved', label: 'Oct 7', shots: today }, [], '');

      expect(otherProfile.holding).toBe(false);
      expect(ids(savedToday.shots)).toEqual(['1', '2']);
      expect(savedToday.holding).toBe(false);
    });
  });
});

describe('knownToday and hitMeanwhile', () => {
  const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [shot('s1', 'p1')] };
  const opened = knownToday(null, { source: saved, profileId: 'p1', session: 1 }, ['1', '2']);

  it('take the range back to today for a shot hit while a saved session is shown', () => {
    expect(hitMeanwhile(opened, ['1', '2'])).toBe(false);
    expect(hitMeanwhile(opened, ['1', '2', '3'])).toBe(true);
  });

  it('keep what today held while the same session stays open, so the hit stays one', () => {
    const later = knownToday(opened, { source: saved, profileId: 'p1', session: 1 }, ['1', '2', '3']);

    expect(later).toBe(opened);
    expect(hitMeanwhile(later, ['1', '2', '3'])).toBe(true);
  });

  it('take today in again for another session or profile, and never count a hit on today itself', () => {
    const other: RangeSource = { kind: 'saved', label: 'Oct 6', shots: [] };

    expect(
      hitMeanwhile(knownToday(opened, { source: other, profileId: 'p1', session: 1 }, ['1', '2', '3']), ['1', '2', '3'])
    ).toBe(false);
    expect(hitMeanwhile(knownToday(opened, { source: saved, profileId: 'p2', session: 1 }, ['9']), ['9'])).toBe(false);
    expect(hitMeanwhile(knownToday(null, { source: LIVE, profileId: 'p1', session: 1 }, []), ['1'])).toBe(false);
  });
});

describe('a session swap', () => {
  const real = [shot('r1', 'p1'), shot('r2', 'p1')];
  const demo = ['d1', 'd2', 'd3', 'd4', 'd5', 'd6'].map((id) => shot(id, 'p1'));
  const demoIds = demo.map((s) => s.timestamp);
  const nextDemoShot = shot('d7', 'p1');
  const today = (shots: Shot[], session: number) => shotFeed(LIVE, shots, 'p1', { session });

  it('a session swap animates nothing', () => {
    const before = today(real, 1);
    const started = today(demo, 2);
    const ended = today(real, 3);

    expect(started.origin, 'a new origin replaces the list').not.toBe(before.origin);
    expect(ended.origin).not.toBe(started.origin);
    for (const swapped of [started, ended]) {
      expect(swapped.seenOnReplace, 'every swapped-in shot counts as seen').toEqual(ids(swapped.shots));
      expect(nextAnimation(swapped.seenOnReplace, swapped.shots, true, null)).toEqual({
        animateId: null,
        pending: null,
      });
    }
  });

  it('flies the next shot after it as a live hit', () => {
    const started = today(demo, 2);
    const next = today([...demo, nextDemoShot], 2);

    expect(next.origin).toBe(started.origin);
    expect(nextAnimation(ids(started.shots), next.shots, true, null)).toEqual({ animateId: 'd7', pending: null });
  });

  it('names the profile and the session as the origin of live shots', () => {
    const origin = (profileId: string, session: number) => shotFeed(LIVE, [], profileId, { session }).origin;

    expect(origin('p1', 1)).toBe(origin('p1', 1));
    expect(origin('p2', 1)).not.toBe(origin('p1', 1));
    expect(origin('p1', 2)).not.toBe(origin('p1', 1));
  });

  it('leaves no hold behind: a real shot in processing does not hold the demo', () => {
    const processing = shot('r3', 'p1', { carry_spin_adjusted: null, flight_path: null });

    expect(today([...real, processing], 1).holding).toBe(true);
    expect(today(demo, 2).holding).toBe(false);
  });

  it('keeps a saved session open; the next shot after it takes the range back to today and flies', () => {
    const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [shot('s1', 'p1')] };
    const opened = knownToday(null, { source: saved, profileId: 'p1', session: 1 }, ids(today(real, 1).shots));
    const swapped = knownToday(opened, { source: saved, profileId: 'p1', session: 2 }, demoIds);

    expect(hitMeanwhile(swapped, demoIds)).toBe(false);
    expect(hitMeanwhile(swapped, [...demoIds, 'd7'])).toBe(true);

    const back = shotFeed(LIVE, [...demo, nextDemoShot], 'p1', { seen: swapped.ids, session: 2 });
    expect(nextAnimation(back.seenOnReplace, back.shots, true, null)).toEqual({ animateId: 'd7', pending: null });
  });
});

describe('rangeBar', () => {
  const driver = rangeShot('d', { club: 'driver', carryM: 220, ballSpeedMph: 160 });
  const iron = rangeShot('i', { club: '8-iron', carryM: 120, ballSpeedMph: 100 });
  const IRON_TO_FLAG_M = Math.hypot(9, 5);

  it('has nothing to show without shots', () => {
    expect(rangeBar([], null)).toBeNull();
  });

  it('shows the latest shot at rest', () => {
    const bar = rangeBar([driver, iron], null);

    expect(bar).toEqual({ club: '8-iron', carryM: 120, ballSpeedMph: 100, totalM: null, targetM: expect.any(Number) });
    expect(bar?.targetM).toBeCloseTo(IRON_TO_FLAG_M, 12);
  });

  it('counts the carry of the shot in flight and shows its carry once it landed', () => {
    const flying = { shotId: 'd', carryM: 87, landed: false };

    expect(rangeBar([driver, iron], flying)).toEqual({
      club: 'driver',
      carryM: 87,
      ballSpeedMph: 160,
      totalM: null,
      targetM: null,
    });
    expect(rangeBar([driver, iron], { ...flying, carryM: 220, landed: true })?.carryM).toBe(220);
  });

  it('follows the shot itself after the landing, e.g. when the server updates its carry', () => {
    const updated = { ...driver, carryM: 214 };

    expect(rangeBar([updated], { shotId: 'd', carryM: 220, landed: true })?.carryM).toBe(214);
  });

  it('falls back to the latest shot when the shot in flight left the list', () => {
    const bar = rangeBar([iron], { shotId: 'd', carryM: 87, landed: false });

    expect(bar).toEqual({ club: '8-iron', carryM: 120, ballSpeedMph: 100, totalM: null, targetM: expect.any(Number) });
  });

  const flight = [
    { t: 0, x: 0, y: 0, z: 0 },
    { t: 3, x: 0, y: 30, z: -110 },
    { t: 6, x: 0, y: 0, z: -220 },
  ];
  const roll = [
    { t: 6, x: 0, y: 0, z: -220 },
    { t: 9, x: 2, y: 0, z: -251 },
  ];
  const rolling = rangeShot('r', { club: 'driver', carryM: 220, ballSpeedMph: 160, totalM: 251, flight, roll });
  const touchdown = { shotId: 'r', carryM: 220, landed: true };
  const rested = { ...touchdown, rested: true };

  describe('the Total', () => {
    it('counts the Total once the ball rests', () => {
      expect(rangeBar([rolling], rested)?.totalM).toBe(251);
    });

    it('holds the Total back while the ball flies or rolls', () => {
      expect(rangeBar([rolling], { shotId: 'r', carryM: 87, landed: false })?.totalM).toBeNull();
      expect(rangeBar([rolling], touchdown)?.totalM).toBeNull();
      expect(rangeBar([rolling], { ...touchdown, rested: false })?.totalM).toBeNull();
    });

    it('shows the Total of a shot at rest, and of the latest shot when the one in flight left the list', () => {
      expect(rangeBar([rolling], null)?.totalM).toBe(251);
      expect(rangeBar([rolling], { shotId: 'gone', carryM: 87, landed: false })?.totalM).toBe(251);
    });

    it('follows the shot itself once rested, e.g. when the server updates its Total', () => {
      expect(rangeBar([{ ...rolling, totalM: 248 }], rested)?.totalM).toBe(248);
    });

    it.each([
      ['a missing', undefined],
      ['a null', null],
      ['a non-finite', Number.NaN],
    ])('has no Total for %s Total', (_name, totalM) => {
      expect(rangeBar([{ ...rolling, totalM }], rested)?.totalM).toBeNull();
    });

    it.each([
      ['no roll', { roll: undefined }],
      ['a roll of one point', { roll: roll.slice(0, 1) }],
      ['a roll after no flight', { flight: null }],
    ])('has no Total for a shot with %s, as the range draws no roll for it', (_name, change) => {
      expect(rangeBar([{ ...rolling, ...change }], rested)?.totalM).toBeNull();
      expect(shownTotalM({ ...rolling, ...change })).toBeNull();
    });

    it('shows the Total by the same rule as the sign at the rest point', () => {
      expect(shownTotalM(rolling)).toBe(251);
    });
  });

  describe('the distance to the target', () => {
    const aimed = (selected: number | null, bearing = 0): RangeAim => ({
      selected,
      aims: new Map([['r', bearing]]),
    });
    const at230 = GREENS.findIndex(({ distanceM }) => distanceM === 230);

    it('measures from the rest point to the nearest flag without a selection, the 230 m one here', () => {
      expect(rangeBar([rolling], rested)?.targetM).toBeCloseTo(Math.hypot(8, 21), 12);
    });

    it('measures to the selected green, from the rest point turned by the bearing the shot was hit with', () => {
      const bearing = greenBearing(GREENS[at230]);
      const rest = rotateAboutTee({ x: 2, z: -251 }, bearing);
      const flag = { x: GREENS[at230].sideM, z: -GREENS[at230].distanceM };

      expect(rangeBar([rolling], rested, aimed(at230, bearing))?.targetM).toBeCloseTo(
        Math.hypot(rest.x - flag.x, rest.z - flag.z),
        12
      );
      expect(rangeBar([rolling], rested, aimed(0))?.targetM).toBeCloseTo(Math.hypot(2 + 9, 251 - 50), 12);
    });

    it('measures to the training target', () => {
      const training: RangeAim = { selected: null, aims: NO_AIMS, trainingTargetM: 240 };

      expect(rangeBar([rolling], rested, training)?.targetM).toBeCloseTo(Math.hypot(2, 11), 12);
    });

    it('measures from the landing point of a shot whose roll is not drawn', () => {
      expect(rangeBar([{ ...rolling, roll: null }], rested)?.targetM).toBeCloseTo(Math.hypot(10, 10), 12);
    });

    it('holds the distance back while the ball flies or rolls', () => {
      expect(rangeBar([rolling], { shotId: 'r', carryM: 87, landed: false })?.targetM).toBeNull();
      expect(rangeBar([rolling], touchdown)?.targetM).toBeNull();
    });
  });

  describe('rangeTarget', () => {
    it("names the shown shot's target for the scene, also while the ball still flies", () => {
      const flying = { shotId: 'r', carryM: 87, landed: false };

      expect(rangeTarget([iron, rolling], flying)).toEqual({ shotId: 'r', x: 10, z: -230, kind: 'nearest' });
      expect(rangeTarget([iron, rolling], null)).toEqual({ shotId: 'r', x: 10, z: -230, kind: 'nearest' });
      expect(rangeTarget([rolling, iron], null)).toEqual({ shotId: 'i', x: 9, z: -125, kind: 'nearest' });
      expect(rangeTarget([rolling], flying, { selected: 2, aims: NO_AIMS })).toEqual({
        shotId: 'r',
        x: -5,
        z: -100,
        kind: 'selected',
      });
    });

    it('has no target without shots', () => {
      expect(rangeTarget([], null)).toBeNull();
    });

    it('reads the bar and the target off the same shown shot', () => {
      const flying = { shotId: 'r', carryM: 87, landed: false };

      expect(rangeReadout([rolling, iron], flying)).toEqual({
        bar: rangeBar([rolling, iron], flying),
        target: { shotId: 'r', x: 10, z: -230, kind: 'nearest' },
      });
      expect(rangeReadout([], null)).toEqual({ bar: null, target: null });
    });
  });
});

describe('rangeBar carry provenance', () => {
  it('passes the typical spin and the missing curve to the bar', () => {
    expect(rangeBar([rangeShot('a', { typicalSpin: true, curveIgnored: true })], null)).toMatchObject({
      typicalSpin: true,
      curveIgnored: true,
    });
  });

  it('adds neither flag for a measured, curved carry', () => {
    const bar = rangeBar([rangeShot('a')], null)!;
    expect('typicalSpin' in bar).toBe(false);
    expect('curveIgnored' in bar).toBe(false);
  });
});

describe('rangeNotice', () => {
  it('names what the picture is waiting for or missing, and nothing once it runs', () => {
    expect(rangeNotice('loading')).toBe('virtualRange.loading');
    expect(rangeNotice('unavailable')).toBe('virtualRange.noWebgl');
    expect(rangeNotice('ready')).toBeNull();
  });
});
