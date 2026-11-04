import type { UnitSystem } from '../utils/units';
import { NO_AIMS, type GroundPoint } from './aim';
import { NO_INSETS, type RangeInsets } from './insets';
import { gainedFlight, nextAnimation, type RangeStatus, type ShotFeed } from './rangeViewModel';
import type { FlightProgress, RangeScene } from './scene/createRangeScene';
import type { LabelBox } from './scene/labelLayout';
import { loadQuality, saveQuality } from './scene/quality';

export type SceneModule = typeof import('./scene/createRangeScene');
export type OverlayRect = LabelBox['rect'];

export interface RangeState {
  status: RangeStatus;
  flight: FlightProgress | null;
}

export const LOADING_STATE: RangeState = { status: 'loading', flight: null };

export interface RangeControllerOptions {
  host: HTMLElement;
  unitSystem: UnitSystem;
  insets?: RangeInsets;
  loadScene: () => Promise<SceneModule>;
  storage: Pick<Storage, 'getItem' | 'setItem'> | null;
  onState(state: RangeState): void;
  onScene?(scene: RangeScene, fly: (shotId: string) => void): () => void;
  onGreenTap?(index: number): void;
}

export interface RangeController {
  show(feed: ShotFeed, aims?: ReadonlyMap<string, number>): void;
  setAim(bearing: number): void;
  setHighlight(index: number | null): void;
  setRestTarget(target: GroundPoint | null): void;
  setTrainingTarget(distanceM: number | null): void;
  setOverlays(rects: readonly OverlayRect[]): void;
  setUnitSystem(unitSystem: UnitSystem): void;
  setInsets(insets: RangeInsets): void;
  resize(): void;
  dispose(): void;
}

const idsOf = ({ shots }: ShotFeed): string[] => shots.map((shot) => shot.id);

export function createRangeController(options: RangeControllerOptions): RangeController {
  const { host, storage } = options;
  let unitSystem = options.unitSystem;
  let insets = options.insets ?? NO_INSETS;
  let state = LOADING_STATE;
  let sceneModule: SceneModule | null = null;
  let scene: RangeScene | null = null;
  let stopScene: (() => void) | null = null;
  let feed: ShotFeed | null = null;
  let aims = NO_AIMS;
  let aim = 0;
  let highlight: number | null = null;
  let restTarget: GroundPoint | null = null;
  let trainingTarget: number | null = null;
  let overlays: readonly OverlayRect[] = [];
  let pending: string | null = null;
  let disposed = false;

  function publish(next: RangeState): void {
    state = next;
    options.onState(next);
  }

  function releaseScene(): void {
    stopScene?.();
    stopScene = null;
    scene?.dispose();
    scene = null;
  }

  function fail(reason: unknown): void {
    if (disposed) return;
    console.warn('Virtual range: no 3D', reason);
    releaseScene();
    publish({ status: 'unavailable', flight: null });
  }

  function play(animateId: string | null): void {
    if (!scene || !feed) return;
    try {
      scene.setShots(feed.shots, animateId, aims);
    } catch (error) {
      fail(error);
      return;
    }
    if (animateId === null) return;
    const shot = feed.shots.find((candidate) => candidate.id === animateId);
    publish({ status: 'ready', flight: shot?.flight ? { shotId: animateId, carryM: 0, landed: false } : null });
  }

  function apply(previousIds: readonly string[]): void {
    if (!feed || state.status === 'unavailable') return;
    const plan = nextAnimation(previousIds, feed.shots, scene !== null, pending);
    pending = plan.pending;
    play(plan.animateId);
  }

  function show(next: ShotFeed, nextAims = NO_AIMS): void {
    if (disposed) return;
    const previous = feed;
    feed = next;
    aims = nextAims;
    const counting = state.flight;
    if (counting && !next.shots.some((shot) => shot.id === counting.shotId)) publish({ ...state, flight: null });
    if (previous === null || previous.origin !== next.origin) {
      pending = null; // what waited for the scene belonged to the old list
      apply(next.seenOnReplace);
      return;
    }
    const upgraded = gainedFlight(previous.shots, next.shots);
    apply(idsOf(previous).filter((id) => id !== upgraded));
  }

  function build({ createRangeScene }: SceneModule): void {
    try {
      scene = createRangeScene(host, {
        unitSystem,
        insets,
        aim,
        onGreenTap: (index) => options.onGreenTap?.(index),
        quality: loadQuality(storage),
        onQualityChange: (level) => saveQuality(storage, level),
        onFlight: (progress) => {
          if (scene) publish({ status: 'ready', flight: progress });
        },
        onFatalError: fail,
      });
      scene.setHighlight(highlight);
      scene.setRestTarget(restTarget);
      scene.setTrainingTarget(trainingTarget);
      scene.setOverlays(overlays);
    } catch (error) {
      fail(error);
      return;
    }
    publish({ status: 'ready', flight: null });
    apply(feed ? idsOf(feed) : []);
    if (scene) stopScene = options.onScene?.(scene, play) ?? null;
  }

  function onLoaded(module: SceneModule): void {
    if (disposed) return;
    sceneModule = module;
    build(module);
  }

  function setUnitSystem(next: UnitSystem): void {
    if (disposed || next === unitSystem) return;
    unitSystem = next;
    if (!scene || !sceneModule) return;
    releaseScene();
    build(sceneModule);
  }

  options.loadScene().then(onLoaded).catch(fail);

  return {
    show,
    setUnitSystem,
    setAim(bearing) {
      aim = bearing;
      scene?.setAim(bearing);
    },
    setHighlight(index) {
      highlight = index;
      scene?.setHighlight(index);
    },
    setRestTarget(target) {
      restTarget = target;
      scene?.setRestTarget(target);
    },
    setTrainingTarget(distanceM) {
      trainingTarget = distanceM;
      scene?.setTrainingTarget(distanceM);
    },
    setOverlays(rects) {
      overlays = rects;
      scene?.setOverlays(rects);
    },
    setInsets(next) {
      insets = next;
      scene?.setInsets(next);
    },
    resize() {
      scene?.resize();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      releaseScene();
    },
  };
}
