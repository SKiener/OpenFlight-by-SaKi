import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fakeStorage } from '../test-utils/fakeBrowser';
import { fakeRangeScene } from '../test-utils/fakeRangeScene';
import { makeShot } from '../test-utils/shot';
import { useShotStore } from '../stores/useShotStore';
import type { Shot } from '../types/shot';
import {
  createRangeController,
  type RangeControllerOptions,
  type RangeState,
  type SceneModule,
} from './rangeController';
import { NO_INSETS } from './insets';
import { shotFeed, type RangeSource, type ShotFeed } from './rangeViewModel';
import type { RangeSceneOptions } from './scene/createRangeScene';
import { QUALITY_STORAGE_KEY } from './scene/quality';

const LIVE: RangeSource = { kind: 'live' };

const live = (profileId: string, ...ids: string[]): ShotFeed =>
  shotFeed(
    LIVE,
    ids.map((id) => makeShot({ timestamp: id, profile_id: profileId })),
    profileId
  );

const flightShot = (id: string) =>
  makeShot({
    timestamp: id,
    profile_id: 'p1',
    flight_path: {
      unit: 'yd',
      points: [
        [0, 0, 0, 0],
        [1, 60, 2, 20],
        [2, 120, 3, 0],
      ],
    },
  });

const flying = (...ids: string[]): ShotFeed => shotFeed(LIVE, ids.map(flightShot), 'p1');

// The OPS emit of a hardware shot: no path and a provisional carry, its camera or IWR data still on the way.
const provisional = (id: string) =>
  makeShot({
    timestamp: id,
    profile_id: 'p1',
    carry_spin_adjusted: null,
    estimated_carry_yards: 180,
    flight_path: null,
  });

const session = (n: number, ...shots: Array<string | Shot>): ShotFeed =>
  shotFeed(
    LIVE,
    shots.map((entry) => (typeof entry === 'string' ? flightShot(entry) : entry)),
    'p1',
    { session: n }
  );

const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

const animated = (scene: ReturnType<typeof fakeRangeScene>) => scene.setShots.mock.calls.map(([, id]) => id);

const webglMissing = (createRangeScene: ReturnType<typeof rig>['createRangeScene']): void => {
  createRangeScene.mockImplementation(() => {
    throw new Error('webgl-unavailable');
  });
};

type SceneHook = NonNullable<RangeControllerOptions['onScene']>;

function rig(overrides: Partial<RangeControllerOptions> = {}) {
  const scenes = [fakeRangeScene()];
  const sceneOptions: RangeSceneOptions[] = [];
  const createRangeScene = vi.fn<SceneModule['createRangeScene']>((_host, options) => {
    sceneOptions.push(options);
    scenes[sceneOptions.length - 1] ??= fakeRangeScene();
    return scenes[sceneOptions.length - 1];
  });
  let loaded!: () => void;
  const loading = new Promise<SceneModule>((resolve) => {
    loaded = () => resolve({ createRangeScene } as unknown as SceneModule);
  });
  const states: RangeState[] = [];
  const host = {} as HTMLElement;
  const controller = createRangeController({
    host,
    unitSystem: 'imperial',
    loadScene: () => loading,
    storage: null,
    onState: (state) => states.push(state),
    ...overrides,
  });
  return {
    controller,
    scene: scenes[0],
    scenes,
    sceneOptions,
    createRangeScene,
    states,
    host,
    last: () => states.at(-1),
    ready: async () => {
      loaded();
      await settle();
    },
  };
}

beforeEach(() => {
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('createRangeController', () => {
  describe('the scene', () => {
    it('is built in the host with the units and the saved quality once the module has loaded', async () => {
      const { localStorage } = fakeStorage({ [QUALITY_STORAGE_KEY]: '1' });
      const { createRangeScene, ready, host, last } = rig({ unitSystem: 'metric', storage: localStorage });

      expect(createRangeScene).not.toHaveBeenCalled();
      await ready();

      expect(createRangeScene).toHaveBeenCalledOnce();
      expect(createRangeScene).toHaveBeenCalledWith(
        host,
        expect.objectContaining({ unitSystem: 'metric', quality: 1 })
      );
      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('starts at full quality without a saved level or with storage that throws', async () => {
      const blocked = {
        getItem: (): never => {
          throw new DOMException('blocked', 'SecurityError');
        },
        setItem: (): never => {
          throw new DOMException('blocked', 'SecurityError');
        },
      };
      for (const storage of [null, blocked]) {
        const { sceneOptions, ready, createRangeScene } = rig({ storage });
        await ready();
        expect(createRangeScene.mock.calls[0][1].quality).toBe(0);
        expect(() => sceneOptions[0].onQualityChange(2)).not.toThrow();
      }
    });

    it('saves the quality the scene steps down to', async () => {
      const { store, localStorage } = fakeStorage();
      const { sceneOptions, ready } = rig({ storage: localStorage });
      await ready();

      sceneOptions[0].onQualityChange(2);

      expect(store[QUALITY_STORAGE_KEY]).toBe('2');
    });

    it('hands the new scene to the caller after it shows the shots', async () => {
      const onScene = vi.fn(() => () => {});
      const { controller, scene, ready } = rig({ onScene });
      controller.show(live('p1', 'a'));
      await ready();

      expect(onScene).toHaveBeenCalledOnce();
      expect(onScene).toHaveBeenCalledWith(scene, expect.any(Function));
      expect(onScene.mock.invocationCallOrder[0]).toBeGreaterThan(scene.setShots.mock.invocationCallOrder[0]);
    });

    it('is resized on request', async () => {
      const { controller, scene, ready } = rig();
      controller.resize();
      await ready();
      controller.resize();

      expect(scene.resize).toHaveBeenCalledOnce();
    });

    it('is built clear of the chrome the view names last, none by default', async () => {
      const plain = rig();
      await plain.ready();
      expect(plain.sceneOptions[0].insets).toEqual(NO_INSETS);

      const { controller, sceneOptions, scene, ready } = rig({ insets: { top: 76, bottom: 90 } });
      controller.setInsets({ top: 122, bottom: 90 });
      await ready();

      expect(sceneOptions[0].insets).toEqual({ top: 122, bottom: 90 });
      expect(scene.setInsets).not.toHaveBeenCalled();
    });

    it('hands chrome that changes to the scene, and to the one a unit change builds', async () => {
      const { controller, scenes, sceneOptions, ready } = rig({ insets: { top: 76, bottom: 90 } });
      await ready();

      controller.setInsets({ top: 122, bottom: 90 });
      controller.setUnitSystem('metric');

      expect(scenes[0].setInsets).toHaveBeenCalledExactlyOnceWith({ top: 122, bottom: 90 });
      expect(sceneOptions[1].insets).toEqual({ top: 122, bottom: 90 });
    });

    it('leaves a disposed scene alone when the chrome changes', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.dispose();

      controller.setInsets({ top: 58, bottom: 72 });

      expect(scene.setInsets).not.toHaveBeenCalled();
    });
  });

  describe('shots', () => {
    it('animates a shot that arrived while the scene was loading', async () => {
      const { controller, scene, ready, last } = rig();
      controller.show(live('p1'));
      controller.show(flying('a'));
      expect(scene.setShots).not.toHaveBeenCalled();

      await ready();

      expect(scene.setShots).toHaveBeenCalledOnce();
      expect(scene.setShots.mock.calls[0][0].map((shot) => shot.id)).toEqual(['a']);
      expect(scene.setShots.mock.calls[0][1]).toBe('a');
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 0, landed: false } });
    });

    it('flies only the newest of several shots that arrived while loading', async () => {
      const { controller, scene, ready } = rig();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));
      controller.show(live('p1', 'a', 'b'));

      await ready();

      expect(scene.setShots).toHaveBeenCalledOnce();
      expect(scene.setShots.mock.calls[0][1]).toBe('b');
    });

    it('shows the shots of a live range at rest when it opens', async () => {
      const { controller, scene, ready, last } = rig();
      controller.show(live('p1', 'a', 'b'));

      await ready();

      expect(scene.setShots).toHaveBeenCalledOnce();
      expect(scene.setShots.mock.calls[0][1]).toBeNull();
      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('flies a new newest shot, once', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1'));

      controller.show(live('p1', 'a'));
      controller.show(live('p1', 'a', 'b'));
      controller.show(live('p1', 'a', 'b'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'a', 'b', null]);
    });

    it('does not fly a shot again that the server updates', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));

      controller.show(live('p1', 'a'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'a', null]);
    });

    it('flies a shot again when its flight path arrives after its landing point', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));
      expect(last(), 'a landing point has nothing to count').toEqual({ status: 'ready', flight: null });

      controller.show(flying('a'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'a', 'a']);
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 0, landed: false } });
    });

    it('starts no count for a landing point and drops the count of the shot before it', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(flying('a'));
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 0, landed: false } });

      controller.show(shotFeed(LIVE, [flightShot('a'), makeShot({ timestamp: 'b', profile_id: 'p1' })], 'p1'));

      expect(scene.setShots.mock.lastCall?.[1]).toBe('b');
      expect(last(), 'the bar shows b at its carry, not a and not 0').toEqual({ status: 'ready', flight: null });
    });

    it('does not fly a shot that already has its flight path again', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(shotFeed(LIVE, [flightShot('a')], 'p1'));

      controller.show(shotFeed(LIVE, [flightShot('a')], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'a', null]);
    });

    it('flies a shot once when its flight path arrives while the scene is loading', async () => {
      const { controller, scene, ready } = rig();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));
      controller.show(shotFeed(LIVE, [flightShot('a')], 'p1'));

      await ready();

      expect(scene.setShots).toHaveBeenCalledOnce();
      expect(scene.setShots.mock.calls[0][1]).toBe('a');
    });

    it('does not fly an older shot that gets its flight path after a newer one arrived', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));
      controller.show(live('p1', 'a', 'b'));

      controller.show(shotFeed(LIVE, [flightShot('a'), makeShot({ timestamp: 'b', profile_id: 'p1' })], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'a', 'b', null]);
    });

    it('holds back a provisional shot and flies it once with its final numbers', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      controller.show(live('p1'));

      controller.show(shotFeed(LIVE, [provisional('b')], 'p1'));
      expect(scene.setShots.mock.lastCall?.[0], 'nothing of b reaches the scene').toEqual([]);
      expect(last()).toEqual({ status: 'ready', flight: null });

      controller.show(shotFeed(LIVE, [flightShot('b')], 'p1'));
      controller.show(shotFeed(LIVE, [flightShot('b')], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, null, 'b', null]);
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'b', carryM: 0, landed: false } });
    });

    it('holds overlapping provisional shots until their own updates, then flies each once', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1'));

      // Shot 2's OPS numbers come before shot 1's update, so the store's processing marker moves on to shot 2.
      controller.show(shotFeed(LIVE, [provisional('1')], 'p1'));
      controller.show(shotFeed(LIVE, [provisional('1'), provisional('2')], 'p1'));
      expect(
        scene.setShots.mock.calls.flatMap(([shots]) => shots),
        'no provisional dot'
      ).toEqual([]);

      controller.show(shotFeed(LIVE, [flightShot('1'), provisional('2')], 'p1'));
      controller.show(shotFeed(LIVE, [flightShot('1'), flightShot('2')], 'p1'));
      controller.show(shotFeed(LIVE, [flightShot('1'), flightShot('2')], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, null, null, '1', '2', null]);
    });

    it('does not count a shot held when the range opened as seen: it lands once its final carry is in', async () => {
      const { controller, scene, ready } = rig();
      const tableCarry = makeShot({ timestamp: 'b', profile_id: 'p1', carry_spin_adjusted: 152, flight_path: null });
      controller.show(shotFeed(LIVE, [flightShot('a'), provisional('b')], 'p1'));
      await ready();

      controller.show(shotFeed(LIVE, [flightShot('a'), tableCarry], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 'b']);
    });

    it("switches to the new profile's shots without flying one", async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      controller.show(live('p1', 'a'));
      const before = last();

      controller.show(live('p2', 'c', 'd'));

      const [shots, animateId] = scene.setShots.mock.lastCall ?? [];
      expect(shots?.map((shot) => shot.id)).toEqual(['c', 'd']);
      expect(animateId).toBeNull();
      expect(last()).toBe(before);
    });

    it("drops the old profile's waiting shot when the profile changes while loading", async () => {
      const { controller, scene, ready } = rig();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));
      controller.show(live('p2', 'c'));

      await ready();

      expect(scene.setShots).toHaveBeenCalledOnce();
      expect(scene.setShots.mock.calls[0][1]).toBeNull();
    });

    it('flies the newest shot of a saved session once, when it opens', async () => {
      const saved: RangeSource = {
        kind: 'saved',
        label: 'Oct 7',
        shots: ['s1', 's2'].map((id) => makeShot({ timestamp: id })),
      };
      const { controller, scene, ready } = rig();
      controller.show(shotFeed(saved, [], 'p1'));

      await ready();
      controller.show(shotFeed(saved, [], 'p1'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual(['s2', null]);
    });

    it('flies a saved session again when another one opens, and shows today at rest on the way back', async () => {
      const session = (...ids: string[]): RangeSource => ({
        kind: 'saved',
        label: 'x',
        shots: ids.map((id) => makeShot({ timestamp: id })),
      });
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(live('p1', 'a'));

      controller.show(shotFeed(session('s1'), [], 'p1'));
      controller.show(shotFeed(session('t1'), [], 'p1'));
      controller.show(live('p1', 'a'));

      expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 's1', 't1', null]);
    });

    describe('a shot hit while a saved session is shown', () => {
      const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [makeShot({ timestamp: 's1' })] };

      async function savedSessionShown() {
        const rigged = rig();
        await rigged.ready();
        rigged.controller.show(shotFeed(LIVE, [flightShot('a')], 'p1'));
        rigged.controller.show(shotFeed(saved, [], ''));
        return rigged;
      }

      it('flies once the range is back on today', async () => {
        const { controller, scene } = await savedSessionShown();
        const today = [flightShot('a'), flightShot('b')];

        controller.show(shotFeed(LIVE, today, 'p1', { seen: new Set(['a']) }));
        controller.show(shotFeed(LIVE, today, 'p1'));

        expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 's1', 'b', null]);
      });

      it('waits while it is provisional, then flies once', async () => {
        const { controller, scene } = await savedSessionShown();
        const held = [flightShot('a'), provisional('b')];

        controller.show(shotFeed(LIVE, held, 'p1', { seen: new Set(['a']) }));
        controller.show(shotFeed(LIVE, held, 'p1'));
        controller.show(shotFeed(LIVE, [flightShot('a'), flightShot('b')], 'p1'));

        expect(scene.setShots.mock.calls.map(([, animateId]) => animateId)).toEqual([null, 's1', null, null, 'b']);
      });
    });
  });

  describe('a session swap', () => {
    it('shows the swapped-in shots at rest both ways, then flies the next shot', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      controller.show(session(1, 'r1'));
      controller.show(session(1, 'r1', 'r2'));

      controller.show(session(2, 'd1', 'd2', 'd3', 'd4', 'd5', 'd6'));
      controller.show(session(3, 'r1', 'r2'));
      expect(last(), 'no flight counts after the swaps').toEqual({ status: 'ready', flight: null });

      controller.show(session(3, 'r1', 'r2', 'r3'));
      expect(animated(scene)).toEqual([null, 'r2', null, null, 'r3']);
    });

    it('drops the count of a flight whose shot the swap took away, also when the shot comes back', async () => {
      const { controller, sceneOptions, ready, last } = rig();
      await ready();
      controller.show(session(1, 'r1'));
      controller.show(session(1, 'r1', 'r2'));
      sceneOptions[0].onFlight({ shotId: 'r2', carryM: 87, landed: false });

      controller.show(session(2, 'd1', 'd2'));
      expect(last(), 'the demo started mid-flight').toEqual({ status: 'ready', flight: null });

      controller.show(session(3, 'r1', 'r2'));
      expect(last(), 'the demo ended before its first flight').toEqual({ status: 'ready', flight: null });
    });

    it('drops the count of a flight whose shot was deleted, and keeps it while the shot stays', async () => {
      const { controller, sceneOptions, ready, last } = rig();
      await ready();
      controller.show(session(1, 'r1'));
      controller.show(session(1, 'r1', 'r2'));
      sceneOptions[0].onFlight({ shotId: 'r2', carryM: 87, landed: false });

      controller.show(session(1, 'r1', 'r2'));
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'r2', carryM: 87, landed: false } });

      controller.show(session(1, 'r1'));
      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('does not animate the previous shot after a delete', async () => {
      const { controller, scene, sceneOptions, ready, last } = rig();
      await ready();
      controller.show(session(1, 'r1'));
      controller.show(session(1, 'r1', 'r2'));
      sceneOptions[0].onFlight({ shotId: 'r2', carryM: 120, landed: true, rested: true });
      scene.setShots.mockClear();

      controller.show(session(1, 'r1'));

      expect(animated(scene), 'r1 drawn at rest').toEqual([null]);
      expect(scene.setShots.mock.calls[0][0].map((shot) => shot.id)).toEqual(['r1']);
      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('drops a shot that waited for the scene, even when the new list has it too', async () => {
      const { controller, scene, ready } = rig();
      controller.show(session(1, 'a'));
      controller.show(session(1, 'a', 'b'));
      controller.show(session(2, 'a', 'b'));

      await ready();

      expect(animated(scene)).toEqual([null]);
    });

    it('lands a shot held through the swap at rest when it comes back final', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(session(1, 'r1', provisional('r2')));

      controller.show(session(2, 'd1', 'd2'));
      controller.show(session(3, 'r1', 'r2'));

      expect(animated(scene)).toEqual([null, null, null]);
    });

    it('flies a shot still held after the swap once its final numbers come', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(session(1, 'r1', provisional('r2')));

      controller.show(session(2, 'd1', 'd2'));
      controller.show(session(3, 'r1', provisional('r2')));
      controller.show(session(3, 'r1', 'r2'));

      expect(animated(scene)).toEqual([null, null, null, 'r2']);
    });

    it('flies the next shot after a swap from a saved session, once the range is back on today', async () => {
      const saved: RangeSource = { kind: 'saved', label: 'Oct 7', shots: [makeShot({ timestamp: 's1' })] };
      const { controller, scene, ready } = rig();
      await ready();
      controller.show(session(1, 'r1'));
      controller.show(shotFeed(saved, [], ''));

      const demo = ['d1', 'd2', 'd3'].map(flightShot);
      controller.show(shotFeed(LIVE, demo, 'p1', { seen: new Set(['d1', 'd2']), session: 2 }));
      controller.show(session(2, ...demo));

      expect(animated(scene)).toEqual([null, 's1', 'd3', null]);
    });
  });

  describe('sessions from the server, through the shot store', () => {
    // The feed VirtualRangeView builds from the store.
    const fromStore = (): ShotFeed => {
      const { shots, sessionGeneration } = useShotStore.getState();
      return shotFeed(LIVE, shots, 'p1', { session: sessionGeneration });
    };
    const sessionState = (ids: string[], demo = false) =>
      useShotStore.getState().receiveSession(ids.map(flightShot), demo);
    const hit = (id: string) => useShotStore.getState().addShot(flightShot(id));

    beforeEach(() => {
      useShotStore.setState(useShotStore.getInitialState(), true);
    });

    afterEach(() => {
      useShotStore.getState().clearShots();
    });

    it('lets a flight go on when another screen makes the server resend the session', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      sessionState(['a']);
      controller.show(fromStore());
      hit('b');
      controller.show(fromStore());
      const flight = last();

      sessionState(['a', 'b']);
      controller.show(fromStore());

      expect(last(), 'the count goes on').toBe(flight);
      expect(animated(scene)).toEqual([null, 'b', null]);
      expect(
        scene.setShots.mock.lastCall?.[0].map((shot) => shot.id),
        'b stays in the scene'
      ).toEqual(['a', 'b']);
    });

    it('keeps a shot waiting for the scene when the server resends the session', async () => {
      const { controller, scene, ready } = rig();
      sessionState(['a']);
      controller.show(fromStore());
      hit('b');
      controller.show(fromStore());

      sessionState(['a', 'b']);
      controller.show(fromStore());
      await ready();

      expect(animated(scene)).toEqual(['b']);
    });

    it('shows the first session of a page at rest, also when it connects mid-demo', async () => {
      for (const demo of [false, true]) {
        useShotStore.setState(useShotStore.getInitialState(), true);
        const { controller, scene, ready } = rig();
        controller.show(fromStore());

        sessionState(['s1', 's2', 's3'], demo);
        controller.show(fromStore());
        await ready();

        expect(animated(scene), `demo: ${demo}`).toEqual([null]);
      }
    });

    it('swaps in the demo and the real session at rest, and flies the next demo shot', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      sessionState(['r1', 'r2']);
      controller.show(fromStore());

      sessionState(['d1', 'd2'], true);
      controller.show(fromStore());
      sessionState(['d1', 'd2'], true);
      controller.show(fromStore());
      hit('d3');
      controller.show(fromStore());
      sessionState(['r1', 'r2'], false);
      controller.show(fromStore());

      expect(animated(scene)).toEqual([null, null, null, 'd3', null]);
    });
  });

  describe('units', () => {
    it('rebuilds the scene in the new unit and shows the shots again at rest', async () => {
      const { controller, scenes, createRangeScene, host, ready, last } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));

      controller.setUnitSystem('metric');

      expect(scenes[0].dispose).toHaveBeenCalledOnce();
      expect(createRangeScene).toHaveBeenCalledTimes(2);
      expect(createRangeScene).toHaveBeenLastCalledWith(host, expect.objectContaining({ unitSystem: 'metric' }));
      const [shots, animateId] = scenes[1].setShots.mock.lastCall ?? [];
      expect(shots?.map((shot) => shot.id)).toEqual(['a']);
      expect(animateId).toBeNull();
      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('keeps the scene when the unit stays the same', async () => {
      const { controller, scene, createRangeScene, ready } = rig({ unitSystem: 'metric' });
      await ready();

      controller.setUnitSystem('metric');

      expect(createRangeScene).toHaveBeenCalledOnce();
      expect(scene.dispose).not.toHaveBeenCalled();
    });

    it('builds the first scene in the new unit when the unit changes while loading', async () => {
      const { controller, createRangeScene, ready } = rig();
      controller.setUnitSystem('metric');

      await ready();

      expect(createRangeScene).toHaveBeenCalledOnce();
      expect(createRangeScene.mock.calls[0][1].unitSystem).toBe('metric');
    });

    it("starts the caller's work on the new scene and stops it on the old one first", async () => {
      const stops = [vi.fn(), vi.fn()];
      const onScene = vi.fn(() => stops[onScene.mock.calls.length - 1]);
      const { controller, scenes, ready } = rig({ onScene });
      await ready();

      controller.setUnitSystem('metric');

      expect(onScene.mock.calls).toEqual([
        [scenes[0], expect.any(Function)],
        [scenes[1], expect.any(Function)],
      ]);
      expect(stops[0]).toHaveBeenCalledOnce();
      expect(stops[0].mock.invocationCallOrder[0]).toBeLessThan(scenes[0].dispose.mock.invocationCallOrder[0]);
      expect(stops[1]).not.toHaveBeenCalled();
    });

    it('says 3D is unavailable when the new scene cannot be built', async () => {
      const { controller, createRangeScene, ready, last } = rig();
      await ready();
      webglMissing(createRangeScene);

      controller.setUnitSystem('metric');

      expect(last()).toEqual({ status: 'unavailable', flight: null });
    });

    it('leaves a scene that was given up, or disposed, alone', async () => {
      const first = rig();
      await first.ready();
      first.sceneOptions[0].onFatalError('context-lost');
      first.controller.setUnitSystem('metric');
      expect(first.createRangeScene).toHaveBeenCalledOnce();

      const second = rig();
      await second.ready();
      second.controller.dispose();
      second.controller.setUnitSystem('metric');
      expect(second.createRangeScene).toHaveBeenCalledOnce();
    });
  });

  describe('aim', () => {
    it('builds the scene turned to the aim and the highlight set before it loaded', async () => {
      const { controller, scene, sceneOptions, ready } = rig();
      controller.setAim(0.2);
      controller.setHighlight(4);
      await ready();

      expect(sceneOptions[0].aim).toBe(0.2);
      expect(scene.setAim).not.toHaveBeenCalled();
      expect(scene.setHighlight).toHaveBeenCalledExactlyOnceWith(4);
    });

    it('hands a later aim and highlight to the scene, and both to the one a unit change builds', async () => {
      const { controller, scenes, sceneOptions, ready } = rig();
      await ready();

      controller.setAim(-0.1);
      controller.setHighlight(2);
      controller.setUnitSystem('metric');

      expect(scenes[0].setAim).toHaveBeenCalledExactlyOnceWith(-0.1);
      expect(scenes[0].setHighlight).toHaveBeenLastCalledWith(2);
      expect(sceneOptions[1].aim).toBe(-0.1);
      expect(scenes[1].setHighlight).toHaveBeenLastCalledWith(2);
    });

    it('draws the shots at the aims shown with them', async () => {
      const { controller, scene, ready } = rig();
      const aims = new Map([['a', 0.1]]);
      controller.show(flying('a'), aims);
      await ready();

      expect(scene.setShots.mock.lastCall?.[2]).toBe(aims);
      controller.show(flying('a', 'b'), aims);
      expect(scene.setShots.mock.lastCall?.[1]).toBe('b');
      expect(scene.setShots.mock.lastCall?.[2]).toBe(aims);
    });

    it('passes a green tap on to the caller', async () => {
      const onGreenTap = vi.fn();
      const { sceneOptions, ready } = rig({ onGreenTap });
      await ready();

      sceneOptions[0].onGreenTap?.(3);

      expect(onGreenTap).toHaveBeenCalledExactlyOnceWith(3);
    });

    it('leaves a disposed scene alone', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.dispose();

      controller.setAim(0.3);
      controller.setHighlight(1);

      expect(scene.setAim).not.toHaveBeenCalled();
      expect(scene.setHighlight).toHaveBeenCalledOnce();
    });
  });

  describe('the target at rest', () => {
    const target = { x: -8, z: -150 };

    it('hands the target to the scene, also one named before the scene loaded', async () => {
      const { controller, scene, ready } = rig();
      controller.setRestTarget(target);
      await ready();

      expect(scene.setRestTarget).toHaveBeenCalledExactlyOnceWith(target);
      controller.setRestTarget(null);
      expect(scene.setRestTarget).toHaveBeenLastCalledWith(null);
    });

    it('names the target before the shots, so a shot that flies on load frames it at rest from the start', async () => {
      const { controller, scene, ready } = rig();
      controller.show(flying('a'));
      controller.setRestTarget(target);
      await ready();

      expect(scene.setRestTarget.mock.invocationCallOrder[0]).toBeLessThan(scene.setShots.mock.invocationCallOrder[0]);
    });

    it('hands the same target in metres to the scene a unit change builds, which labels it anew', async () => {
      const { controller, scenes, sceneOptions, ready } = rig();
      await ready();
      controller.show(live('p1', 'a'));
      controller.setRestTarget(target);

      controller.setUnitSystem('metric');

      expect(sceneOptions[1].unitSystem).toBe('metric');
      expect(scenes[1].setRestTarget).toHaveBeenCalledExactlyOnceWith(target);
    });

    it('leaves a disposed scene alone', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.dispose();

      controller.setRestTarget(target);

      expect(scene.setRestTarget).toHaveBeenCalledExactlyOnceWith(null);
    });
  });

  describe('the training target', () => {
    it('hands the distance to the scene, also one named before the scene loaded, and to the one a unit change builds', async () => {
      const { controller, scenes, ready } = rig();
      controller.setTrainingTarget(137);
      await ready();

      expect(scenes[0].setTrainingTarget).toHaveBeenCalledExactlyOnceWith(137);
      controller.setTrainingTarget(null);
      expect(scenes[0].setTrainingTarget).toHaveBeenLastCalledWith(null);
      controller.setTrainingTarget(90);
      controller.setUnitSystem('metric');
      expect(scenes[1].setTrainingTarget).toHaveBeenCalledExactlyOnceWith(90);
    });

    it('leaves a disposed scene alone', async () => {
      const { controller, scene, ready } = rig();
      await ready();
      controller.dispose();

      controller.setTrainingTarget(120);

      expect(scene.setTrainingTarget).toHaveBeenCalledExactlyOnceWith(null);
    });
  });

  describe("the range's own bars", () => {
    const bar: [number, number, number, number] = [12, 500, 1012, 560];

    it('keeps signs off them, in the scene of now, of later and of a unit change', async () => {
      const { controller, scenes, ready } = rig();
      controller.setOverlays([bar]);
      await ready();

      expect(scenes[0].setOverlays).toHaveBeenCalledExactlyOnceWith([bar]);
      controller.setOverlays([]);
      expect(scenes[0].setOverlays).toHaveBeenLastCalledWith([]);
      controller.setOverlays([bar]);
      controller.setUnitSystem('metric');
      expect(scenes[1].setOverlays).toHaveBeenCalledExactlyOnceWith([bar]);
    });
  });

  describe('flying a shot on request', () => {
    async function withFlyer(feed: ShotFeed) {
      const onScene = vi.fn<SceneHook>(() => () => {});
      const rigged = rig({ onScene });
      rigged.controller.show(feed);
      await rigged.ready();
      return { ...rigged, fly: onScene.mock.calls[0][1] };
    }

    it('flies a shot of the shown list and starts its count', async () => {
      const { scene, fly, last } = await withFlyer(flying('a', 'b'));

      fly('a');

      const [shots, animateId] = scene.setShots.mock.lastCall ?? [];
      expect(shots?.map((shot) => shot.id)).toEqual(['a', 'b']);
      expect(animateId).toBe('a');
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 0, landed: false } });
    });

    it('starts no count for a landing point', async () => {
      const { controller, fly, last } = await withFlyer(flying('a'));
      fly('a');
      controller.show(shotFeed(LIVE, [flightShot('a'), makeShot({ timestamp: 'b', profile_id: 'p1' })], 'p1'));

      fly('b');

      expect(last()).toEqual({ status: 'ready', flight: null });
    });

    it('does nothing once the scene is given up', async () => {
      const { scene, sceneOptions, fly, last } = await withFlyer(flying('a'));
      sceneOptions[0].onFatalError('context-lost');
      const calls = scene.setShots.mock.calls.length;

      fly('a');

      expect(scene.setShots).toHaveBeenCalledTimes(calls);
      expect(last()).toEqual({ status: 'unavailable', flight: null });
    });

    it('treats an error from the scene as a loss of 3D', async () => {
      const { scene, fly, last } = await withFlyer(flying('a'));
      scene.setShots.mockImplementation(() => {
        throw new RangeError('a flight needs at least 2 points');
      });

      expect(() => fly('a')).not.toThrow();

      expect(scene.dispose).toHaveBeenCalledOnce();
      expect(last()).toEqual({ status: 'unavailable', flight: null });
    });
  });

  describe('the flight', () => {
    it('reports the carry counting up and the landing', async () => {
      const { controller, sceneOptions, ready, last } = rig();
      await ready();
      controller.show(live('p1'));
      controller.show(live('p1', 'a'));

      sceneOptions[0].onFlight({ shotId: 'a', carryM: 40, landed: false });
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 40, landed: false } });

      sceneOptions[0].onFlight({ shotId: 'a', carryM: 120, landed: true });
      expect(last()).toEqual({ status: 'ready', flight: { shotId: 'a', carryM: 120, landed: true } });
    });
  });

  describe('without 3D', () => {
    it('says so when WebGL cannot start, and keeps taking shots', async () => {
      const { controller, scene, createRangeScene, ready, last, states } = rig();
      webglMissing(createRangeScene);
      controller.show(live('p1', 'a'));

      await ready();
      controller.show(live('p1', 'a', 'b'));

      expect(last()).toEqual({ status: 'unavailable', flight: null });
      expect(states).toHaveLength(1);
      expect(scene.setShots).not.toHaveBeenCalled();
    });

    it('says so when the 3D module cannot be loaded', async () => {
      const { last } = rig({
        loadScene: () => Promise.reject(new Error('Failed to fetch dynamically imported module')),
      });
      await settle();

      expect(last()).toEqual({ status: 'unavailable', flight: null });
    });

    it('does not start the caller on a scene that failed', async () => {
      const onScene = vi.fn(() => () => {});
      const { createRangeScene, ready } = rig({ onScene });
      webglMissing(createRangeScene);

      await ready();

      expect(onScene).not.toHaveBeenCalled();
    });

    it('gives the scene up after a fatal error and keeps taking shots', async () => {
      const { controller, scene, sceneOptions, ready, last } = rig();
      await ready();
      controller.show(live('p1', 'a'));
      sceneOptions[0].onFlight({ shotId: 'a', carryM: 40, landed: false });

      sceneOptions[0].onFatalError('context-lost');
      controller.show(live('p1', 'a', 'b'));

      expect(scene.dispose).toHaveBeenCalledOnce();
      expect(last()).toEqual({ status: 'unavailable', flight: null });
      expect(scene.setShots).toHaveBeenCalledTimes(1);
    });

    it('treats an error from the scene as a loss of 3D', async () => {
      const { controller, scene, ready, last } = rig();
      await ready();
      scene.setShots.mockImplementation(() => {
        throw new RangeError('a flight needs at least 2 points');
      });

      expect(() => controller.show(live('p1', 'a'))).not.toThrow();

      expect(scene.dispose).toHaveBeenCalledOnce();
      expect(last()).toEqual({ status: 'unavailable', flight: null });
    });

    it('logs why 3D is missing', async () => {
      const { createRangeScene, ready } = rig();
      webglMissing(createRangeScene);

      await ready();

      expect(console.warn).toHaveBeenCalledOnce();
    });

    it('says so, with the error, when a frame of the scene threw', async () => {
      const { scene, sceneOptions, ready, last } = rig();
      await ready();
      const error = new Error('draw failed');

      sceneOptions[0].onFatalError(error);

      expect(scene.dispose).toHaveBeenCalledOnce();
      expect(last()).toEqual({ status: 'unavailable', flight: null });
      expect(console.warn).toHaveBeenCalledWith('Virtual range: no 3D', error);
    });
  });

  describe('dispose', () => {
    it('never builds a scene when it comes before the module has loaded', async () => {
      const { controller, createRangeScene, ready, states } = rig();

      controller.dispose();
      await ready();

      expect(createRangeScene).not.toHaveBeenCalled();
      expect(states).toEqual([]);
    });

    it('releases the scene once and ignores everything after', async () => {
      const { controller, scene, sceneOptions, ready, states } = rig();
      await ready();
      const published = states.length;

      controller.dispose();
      controller.dispose();
      controller.show(live('p1', 'a'));
      controller.resize();
      sceneOptions[0].onFlight({ shotId: 'a', carryM: 10, landed: false });
      sceneOptions[0].onFatalError('context-lost');

      expect(scene.dispose).toHaveBeenCalledOnce();
      expect(scene.setShots).not.toHaveBeenCalled();
      expect(scene.resize).not.toHaveBeenCalled();
      expect(states).toHaveLength(published);
    });

    it("stops the caller's work on the scene before the scene goes", async () => {
      const stop = vi.fn();
      const { controller, scene, ready } = rig({ onScene: () => stop });
      await ready();

      controller.dispose();

      expect(stop).toHaveBeenCalledOnce();
      expect(stop.mock.invocationCallOrder[0]).toBeLessThan(scene.dispose.mock.invocationCallOrder[0]);
    });

    it("stops the caller's work when the scene is given up", async () => {
      const stop = vi.fn();
      const { scene, sceneOptions, ready } = rig({ onScene: () => stop });
      await ready();

      sceneOptions[0].onFatalError('context-lost');

      expect(stop).toHaveBeenCalledOnce();
      expect(stop.mock.invocationCallOrder[0]).toBeLessThan(scene.dispose.mock.invocationCallOrder[0]);
    });
  });
});
