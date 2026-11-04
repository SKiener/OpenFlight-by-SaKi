import {
  Group,
  Object3D,
  PerspectiveCamera,
  Raycaster,
  Scene,
  Vector2,
  Vector3,
  type BufferGeometry,
  type WebGLRenderer,
} from 'three';
import type { UnitSystem } from '../../utils/units';
import { NO_AIMS, rotateAboutTee, type GroundPoint } from '../aim';
import { distanceLabel } from '../distance';
import {
  landingPoint,
  positionAt,
  sampleFlight,
  shownTotalM,
  usableFlight,
  usableRoll,
  type RangeShot,
  type ScenePoint,
} from '../flightPath';
import { NO_INSETS, type RangeInsets } from '../insets';
import { directDistanceM, restOnRange } from '../target';
import {
  CAMERA_TIMING,
  TEE_POSE,
  cameraPoseAt,
  flightSeconds,
  restView,
  rollSeconds,
  shotDuration,
  type CameraPose,
  type Vec3,
  type ViewBounds,
} from './cameraPath';
import { buildEnvironment } from './environment';
import { chromeAreas, coveredPx, frameCamera } from './framing';
import { buildGreens, turfHeightAt } from './greens';
import type { LabelBox } from './labelLayout';
import { FOG_COLOR } from './parts';
import { QualityMonitor, type QualityLevel } from './quality';
import { createRenderer, destroyRenderer, disposeResources } from './renderer';
import { createSignLayout, LANDING_SIGN_SIZE, TARGET_SIGN_SIZE } from './signs';
import { makeNoiseTexture, seededRandom } from './textures';
import { displayFontPending } from '../../theme/fonts';
import { buildFlightMarks, buildTracerGeometry, landingOf, onTurf, restOf, SAMPLE_STEP_S, shotDrawKey } from './tracer';
import { buildTrainingTarget } from './trainingTarget';

export interface FlightProgress {
  shotId: string;
  carryM: number;
  landed: boolean;
  rested?: boolean;
}

export interface RangeSceneOptions {
  unitSystem: UnitSystem; // sign texts in m or yd
  quality: QualityLevel;
  insets?: RangeInsets; // the app's chrome over the canvas; none by default
  aim?: number;
  onGreenTap?: (index: number) => void;
  onQualityChange(level: QualityLevel): void;
  onFlight(progress: FlightProgress): void; // at most 20 Hz in flight, and when landed and rested turn true
  // Once, then the scene draws nothing more: 'context-lost' when a lost context is not back within 3 s or is
  // lost again, or the error a frame threw.
  onFatalError(reason: unknown): void;
}

export interface RangeScene {
  setShots(shots: RangeShot[], animateId: string | null, aims?: ReadonlyMap<string, number>): void; // dots for all, faint tracers for the 2 before animateId
  setAim(bearing: number): void;
  setHighlight(index: number | null): void;
  setRestTarget(target: GroundPoint | null): void;
  setTrainingTarget(distanceM: number | null): void;
  setOverlays(rects: readonly LabelBox['rect'][]): void;
  setInsets(insets: RangeInsets): void; // the view centres between the chrome, signs keep off it
  resize(): void;
  stats(): {
    calls: number;
    triangles: number;
    programs: number;
    quality: QualityLevel;
    frames: number;
    animating: boolean;
  };
  frameTimes(): number[]; // ms per animated frame, the last 16,384, for diagnostics
  dispose(): void; // stops the loop, releases the WebGL context
}

const RANGE_SEED = 20261007;
const FAINT_FADES = [0.32, 0.16];
const WHOLE_FLIGHT_S = 1e4;
const PROGRESS_INTERVAL_MS = 50;
const IMPACT_S = 1.1;
const LANDING_ACCENT = '#ffd27a';
const TARGET_ACCENT = '#ffffff';
const TARGET_LABEL_LIFT_M = 0.15;
const FLAG_TOP_M = 4.2;
const TOTAL_SIGN_Y_M = 1.6;
const REST_CLEAR_TOP_REM = 4;
const REST_CLEAR_BOTTOM_REM = 4.75;
const REST_MARGIN_PX = 8;
const FRAME_LOG_SIZE = 1 << 14;
const TURN_MS = 500;
const TAP_SLOP_PX = 10;
const NEAR_FLAG_REM = 2.5;
export const RESTORE_TIMEOUT_MS = 3000;
const { preS, landingMoveS, landingHoldS, returnS } = CAMERA_TIMING;

interface Tracer {
  key: string;
  samples: ScenePoint[];
  geometry: BufferGeometry;
  drawn: ScenePoint[];
  rollSampled: boolean;
  roll: ScenePoint[] | null;
  drawnRoll: ScenePoint[] | null;
}

interface Focus {
  shot: RangeShot;
  bearing: number;
  tracer: Tracer | null;
  flightS: number;
  rollS: number;
  endS: number;
  landing: Vec3;
  restView: CameraPose | null;
  rest: Vec3;
}

type Phase = 'idle' | 'flight' | 'report-landing';

const turned = (points: ScenePoint[], bearing: number): ScenePoint[] =>
  bearing === 0 ? points : points.map((point) => ({ t: point.t, y: point.y, ...rotateAboutTee(point, bearing) }));

const rootRemPx = (): number => parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;

export function createRangeScene(host: HTMLElement, options: RangeSceneOptions): RangeScene {
  let antialiased = options.quality === 0;
  let renderer = createRenderer(antialiased);
  const scene = new Scene();
  const camera = new PerspectiveCamera(46, 1, 0.5, 6000);
  const world = new Group();
  let environment: ReturnType<typeof buildEnvironment>;
  let greens: ReturnType<typeof buildGreens>;
  let marks: ReturnType<typeof buildFlightMarks>;
  let signs: ReturnType<typeof createSignLayout>;
  let trainingTarget: ReturnType<typeof buildTrainingTarget>;

  const monitor = new QualityMonitor(options.quality);
  const lookTarget = new Vector3();
  const ballPoint = new Vector3();
  const ballSeen = new Vector3();
  const raycaster = new Raycaster();
  const pointer = new Vector2();
  const frameLog = new Float64Array(FRAME_LOG_SIZE);
  let framesLogged = 0;
  let frames = 0;
  let width = 1;
  let height = 1;
  let frameRequest = 0;
  let lastFrameMs: number | null = null;
  let disposed = false;
  let contextLost = false;
  let losses = 0;
  let fatal = false;
  let restoreTimer = 0;
  let pendingStep: QualityLevel | null = null;
  let quality = options.quality;
  let insets = options.insets ?? NO_INSETS;
  let chrome: LabelBox['rect'][] = [];
  let overlays: LabelBox['rect'][] = [];
  let reserved: LabelBox['rect'][] = [];
  let restBounds: ViewBounds = { up: 1, down: 1, side: 1 };
  let restTarget: GroundPoint | null = null;
  let targetLabel: string | null = null;
  let targetShown = false;

  let shots: RangeShot[] = [];
  let tracers = new Map<string, Tracer>();
  let focus: Focus | null = null;
  let phase: Phase = 'idle';
  let startMs: number | null = null;
  let lastProgressMs = -Infinity;
  let landedSent = false;
  let restedSent = false;
  let signKey: string | null = null;
  let pendingProgress: FlightProgress | null = null;
  let pendingQuality: QualityLevel | null = null;
  let aims = NO_AIMS;
  let aim = options.aim ?? 0;
  let turnFrom = aim;
  let turnStartMs: number | null = null;
  let press: { id: number; x: number; y: number } | null = null;

  function applySize(): void {
    width = Math.max(1, host.clientWidth);
    height = Math.max(1, host.clientHeight);
    renderer.setSize(width, height, false);
    frameView();
  }

  // Without setSize: a canvas given its width again is cleared until the next frame.
  function frameView(): void {
    frameCamera(camera, width, height, insets);
    chrome = chromeAreas(width, height, insets);
    reserved = [...chrome, ...overlays];
    restBounds = boundsAtRest();
    refreshRestView();
  }

  function boundsAtRest(): ViewBounds {
    const remPx = rootRemPx();
    const pxPerUnit = height / 2 / Math.tan((camera.fov * Math.PI) / 360);
    const top = coveredPx(insets.top) + REST_CLEAR_TOP_REM * remPx + REST_MARGIN_PX;
    const bottom = height - coveredPx(insets.bottom) - REST_CLEAR_BOTTOM_REM * remPx - REST_MARGIN_PX;
    const axisY = (height + coveredPx(insets.top) - coveredPx(insets.bottom)) / 2;
    const tangent = (px: number) => Math.max(0.01, px / pxPerUnit);
    return {
      up: tangent(axisY - top - LANDING_SIGN_SIZE * marks.landingSign.aspect * pxPerUnit),
      down: tangent(bottom - axisY - TARGET_SIGN_SIZE * marks.targetSign.aspect * pxPerUnit),
      side: tangent(width / 2 - REST_MARGIN_PX - (LANDING_SIGN_SIZE / 2) * pxPerUnit),
    };
  }

  function restViewOf({ shot, bearing }: Focus): CameraPose {
    const rest = restOf(shot);
    const points: Vec3[] = [rest, { ...rest, y: TOTAL_SIGN_Y_M }];
    if (restTarget) {
      const flag = rotateAboutTee(restTarget, -bearing);
      points.push(
        { x: flag.x, y: 0, z: flag.z },
        { x: flag.x, y: FLAG_TOP_M, z: flag.z },
        { x: (rest.x + flag.x) / 2, y: TARGET_LABEL_LIFT_M, z: (rest.z + flag.z) / 2 }
      );
    }
    return restView(points, restBounds, [landingOf(shot)]);
  }

  const restViewHeld = (): boolean => phase === 'flight' && restedSent;

  function refreshRestView(): void {
    if (focus?.tracer && !restViewHeld()) focus.restView = restViewOf(focus);
  }

  function placeTargetLine(): void {
    if (!focus || !restTarget) return;
    const { rest } = focus;
    marks.targetLine.place(rest, restTarget);
    const label = distanceLabel(directDistanceM(rest, restTarget), options.unitSystem);
    if (label !== targetLabel) marks.targetSign.draw(label, TARGET_ACCENT);
    targetLabel = label;
    const x = (rest.x + restTarget.x) / 2;
    const z = (rest.z + restTarget.z) / 2;
    marks.targetSign.sprite.position.set(x, turfHeightAt(x, z) + TARGET_LABEL_LIFT_M, z);
  }

  function attachRenderer(): void {
    renderer.domElement.addEventListener('webglcontextlost', onContextLost);
    renderer.domElement.addEventListener('webglcontextrestored', onContextRestored);
    renderer.domElement.addEventListener('pointerdown', onPointerDown);
    renderer.domElement.addEventListener('pointermove', onPointerMove);
    renderer.domElement.addEventListener('pointerup', onPointerUp);
    renderer.domElement.addEventListener('pointercancel', onPointerCancel);
    applySize();
    renderer.compile(scene, camera);
  }

  function detachRenderer(): void {
    renderer.domElement.removeEventListener('webglcontextlost', onContextLost);
    renderer.domElement.removeEventListener('webglcontextrestored', onContextRestored);
    renderer.domElement.removeEventListener('pointerdown', onPointerDown);
    renderer.domElement.removeEventListener('pointermove', onPointerMove);
    renderer.domElement.removeEventListener('pointerup', onPointerUp);
    renderer.domElement.removeEventListener('pointercancel', onPointerCancel);
  }

  function onPointerDown(event: PointerEvent): void {
    press = event.isPrimary && event.button === 0 ? { id: event.pointerId, x: event.clientX, y: event.clientY } : null;
  }

  function dragged(event: PointerEvent): boolean {
    return (
      press === null ||
      event.pointerId !== press.id ||
      Math.hypot(event.clientX - press.x, event.clientY - press.y) > TAP_SLOP_PX
    );
  }

  function onPointerMove(event: PointerEvent): void {
    if (press?.id === event.pointerId && dragged(event)) press = null;
  }

  function onPointerUp(event: PointerEvent): void {
    const tapped = !dragged(event);
    press = null;
    if (tapped) pickGreen(event.clientX, event.clientY);
  }

  function onPointerCancel(): void {
    press = null;
  }

  function pickGreen(clientX: number, clientY: number): void {
    if (!options.onGreenTap || fatal) return;
    const box = renderer.domElement.getBoundingClientRect();
    pointer.set(((clientX - box.left) / box.width) * 2 - 1, 1 - ((clientY - box.top) / box.height) * 2);
    raycaster.setFromCamera(pointer, camera);
    const remPx = rootRemPx();
    const index =
      greens.pick(raycaster) ??
      greens.nearest(camera, clientX - box.left, clientY - box.top, box.width, box.height, NEAR_FLAG_REM * remPx);
    if (index !== null) options.onGreenTap(index);
  }

  function onContextLost(event: Event): void {
    event.preventDefault();
    contextLost = true;
    cancelFrame();
    losses += 1;
    if (losses > 1) giveUp('context-lost');
    else restoreTimer = window.setTimeout(() => giveUp('context-lost'), RESTORE_TIMEOUT_MS);
  }

  function giveUp(reason: unknown): void {
    clearRestoreTimer();
    if (fatal) return;
    fatal = true;
    phase = 'idle';
    options.onFatalError(reason);
  }

  function clearRestoreTimer(): void {
    if (restoreTimer !== 0) window.clearTimeout(restoreTimer);
    restoreTimer = 0;
  }

  function onContextRestored(): void {
    clearRestoreTimer();
    if (fatal) return;
    contextLost = false;
    // three rebuilt its GL state on restore; the clear colour lives in that state.
    renderer.setClearColor(FOG_COLOR);
    renderer.compile(scene, camera);
    requestFrame();
  }

  function requestFrame(): void {
    if (frameRequest === 0 && !disposed && !contextLost && !fatal) frameRequest = requestAnimationFrame(onFrame);
  }

  function cancelFrame(): void {
    if (frameRequest !== 0) cancelAnimationFrame(frameRequest);
    frameRequest = 0;
    lastFrameMs = null;
  }

  function dropAntialiasing(): void {
    let next: WebGLRenderer;
    try {
      next = createRenderer(false);
    } catch {
      return; // No second context to switch to: keep drawing with the antialiased one.
    }
    detachRenderer();
    renderer.domElement.replaceWith(next.domElement);
    disposeResources(scene); // through the old renderer; the new one uploads everything again
    destroyRenderer(renderer);
    renderer = next;
    antialiased = false;
    attachRenderer();
  }

  // Applied once the flight is over: a new renderer compiles for a few hundred ms on the Pi, a visible freeze mid-air.
  function applyQualityStep(level: QualityLevel): void {
    if (level >= 1 && antialiased) dropAntialiasing();
    if (level === 2) environment.setVegetationHalved(true);
    quality = level;
    pendingQuality = level;
  }

  function recordFrame(frameMs: number): void {
    frameLog[framesLogged % FRAME_LOG_SIZE] = frameMs;
    framesLogged += 1;
    if (pendingStep === null) pendingStep = monitor.record(frameMs);
  }

  const bearingOf = (shot: RangeShot): number => aims.get(shot.id) ?? 0;
  const drawKey = (shot: RangeShot): string => `${shotDrawKey(shot)}|${bearingOf(shot)}`;

  function newTracer(shot: RangeShot, key: string): Tracer | null {
    const flight = usableFlight(shot);
    if (!flight) return null;
    const samples = sampleFlight(flight, SAMPLE_STEP_S);
    const drawn = turned(samples, bearingOf(shot));
    return {
      key,
      samples,
      drawn,
      geometry: buildTracerGeometry(drawn),
      rollSampled: false,
      roll: null,
      drawnRoll: null,
    };
  }

  function sampleRoll(tracer: Tracer, shot: RangeShot): void {
    const usable = usableRoll(shot);
    tracer.roll = usable ? sampleFlight(usable, SAMPLE_STEP_S) : null;
    tracer.drawnRoll = tracer.roll && turned(tracer.roll, bearingOf(shot));
    tracer.rollSampled = true;
  }

  function tracerFor(shot: RangeShot | undefined, focused: boolean): Tracer | null {
    if (!shot) return null;
    const key = drawKey(shot);
    const cached = tracers.get(shot.id);
    const tracer = cached?.key === key ? cached : newTracer(shot, key);
    if (tracer && focused && !tracer.rollSampled) sampleRoll(tracer, shot);
    return tracer;
  }

  function setShots(nextShots: RangeShot[], animateId: string | null, nextAims = NO_AIMS): void {
    if (disposed) return;
    shots = nextShots.slice();
    aims = nextAims;
    const requested = animateId === null ? undefined : shots.find((shot) => shot.id === animateId);
    // Without a new flight, one in progress goes on; otherwise the newest shot is shown at rest.
    const flying = phase === 'idle' ? undefined : shots.find((shot) => shot.id === focus?.shot.id);
    const focusShot = requested ?? flying ?? shots.at(-1);
    const index = focusShot ? shots.indexOf(focusShot) : -1;

    const shown = focusShot
      ? [focusShot, shots[index - 1], shots[index - 2]].map((shot, slot) => tracerFor(shot, slot === 0))
      : [];
    const next = new Map<string, Tracer>();
    shown.forEach((tracer, slot) => {
      if (tracer) next.set(shots[index - slot].id, tracer);
    });
    for (const [id, tracer] of tracers) if (next.get(id) !== tracer) tracer.geometry.dispose();
    tracers = next;
    marks.tracers.forEach((slot, i) => {
      slot.show(shown[i]?.geometry ?? null, i === 0 ? 1 : FAINT_FADES[i - 1]);
      slot.growTo(WHOLE_FLIGHT_S);
    });

    const heldView = flying && restViewHeld() ? (focus?.restView ?? null) : null;
    if (!focusShot) {
      focus = null;
    } else {
      const tracer = shown[0] ?? null;
      const flightS = tracer ? flightSeconds(tracer.samples) : 0;
      const rollS = rollSeconds(tracer?.roll);
      const bearing = bearingOf(focusShot);
      focus = {
        shot: focusShot,
        bearing,
        tracer,
        flightS,
        rollS,
        endS: shotDuration(flightS, rollS),
        landing: onTurf(rotateAboutTee(landingPoint(focusShot), bearing)),
        rest: onTurf(restOnRange(focusShot, bearing)),
        restView: heldView,
      };
    }
    if (requested && focus) {
      phase = focus.tracer ? 'flight' : 'report-landing';
      startMs = null;
      landedSent = false;
      restedSent = false;
      lastProgressMs = -Infinity;
      if (focus.tracer) finishTurn();
    } else if (!flying) {
      phase = 'idle';
    }
    marks.rollLine.set(focus?.tracer?.drawnRoll ?? null);
    refreshRestView();
    placeTargetLine();

    const others = shots.filter((shot) => shot !== focusShot).map((shot) => onTurf(restOnRange(shot, bearingOf(shot))));
    marks.dots.place(focus ? [...others, focus.rest] : others);
    const focusKey = focus ? drawKey(focus.shot) : null;
    if (focus && focusKey !== signKey) {
      const label = distanceLabel(shownTotalM(focus.shot) ?? focus.shot.carryM, options.unitSystem);
      marks.landingSign.draw(label, LANDING_ACCENT);
      marks.landingSign.sprite.position.set(focus.rest.x, TOTAL_SIGN_Y_M, focus.rest.z);
      signKey = focusKey;
    }
    requestFrame();
  }

  /** Moves the focus on to `now`: starts its clock, reports its landing, ends its flight. Seconds into it while it flies. */
  function advanceFlight(now: number): number | null {
    if (!focus) return null;
    if (phase === 'report-landing') {
      queueProgress(focus, focus.shot.carryM, true, true);
      phase = 'idle';
    }
    if (phase !== 'flight') return null;
    if (startMs === null) startMs = now;
    const t = Math.max(0, (now - startMs) / 1000);
    const flownS = t - preS;
    if (flownS >= focus.flightS && !landedSent) queueProgress(focus, focus.shot.carryM, true, focus.rollS === 0);
    if (flownS >= focus.flightS + focus.rollS && !restedSent) queueProgress(focus, focus.shot.carryM, true, true);
    if (t < focus.endS) return t;
    phase = 'idle';
    return null;
  }

  function queueProgress(shown: Focus, carryM: number, landed: boolean, rested: boolean): void {
    pendingProgress = { shotId: shown.shot.id, carryM, landed, rested };
    if (landed) landedSent = true;
    if (rested) restedSent = true;
  }

  function placeCamera({ position: p, target: t }: CameraPose, turn = 0): void {
    camera.position.set(p.x, p.y, p.z).applyAxisAngle(Object3D.DEFAULT_UP, -turn);
    camera.lookAt(lookTarget.set(t.x, t.y, t.z).applyAxisAngle(Object3D.DEFAULT_UP, -turn));
    environment.sky.position.copy(camera.position);
  }

  function showAtRest(): boolean {
    placeCamera(TEE_POSE);
    marks.setBall(null, 0);
    marks.setBallShadow(null, 0);
    marks.setImpact(null, 0);
    marks.setLandingMark(focus?.tracer?.drawnRoll ? focus.landing : null);
    marks.rollLine.show(Infinity);
    marks.tracers[0].growTo(WHOLE_FLIGHT_S);
    marks.dots.showLast(true);
    showTargetLine(focus !== null);
    return false;
  }

  function showTargetLine(atRest: boolean): void {
    targetShown = atRest && restTarget !== null;
    marks.targetLine.show(targetShown);
  }

  /** Also reports the progress in flight; returns whether the landing sign shows. */
  function showFlight(shown: Focus, { samples, roll, drawn, drawnRoll }: Tracer, t: number, now: number): boolean {
    const { shot, flightS, rollS, landing } = shown;
    const first = drawn[0];
    placeCamera(cameraPoseAt(samples, t, roll, shown.restView), shown.bearing - world.rotation.y);
    const flownS = t - preS;
    const sinceLanding = flownS - flightS;
    const sinceRest = sinceLanding - rollS;
    const ball =
      drawnRoll && sinceLanding > 0
        ? positionAt(drawnRoll, drawnRoll[0].t + sinceLanding)
        : positionAt(drawn, first.t + Math.min(Math.max(flownS, 0), flightS));
    const groundY = turfHeightAt(ball.x, ball.z);
    ballPoint.set(ball.x, Math.max(ball.y, groundY), ball.z);
    marks.tracers[0].growTo(flownS);
    const ballShown = flownS > 0 && sinceRest < landingMoveS + landingHoldS;
    ballSeen.copy(ballPoint).applyAxisAngle(Object3D.DEFAULT_UP, world.rotation.y);
    marks.setBall(ballShown ? ballPoint : null, camera.position.distanceTo(ballSeen));
    const inAir = flownS > 0 && sinceLanding < 0;
    marks.setBallShadow(inAir ? ballPoint : null, groundY);
    if (inAir && now - lastProgressMs >= PROGRESS_INTERVAL_MS) {
      queueProgress(shown, Math.min(Math.hypot(ball.x - first.x, ball.z - first.z), shot.carryM), false, false);
      lastProgressMs = now;
    }
    const impactShown = sinceLanding > 0 && sinceLanding < IMPACT_S;
    marks.setImpact(impactShown ? landing : null, sinceLanding / IMPACT_S);
    marks.setLandingMark(drawnRoll && sinceLanding > 0 ? landing : null);
    marks.rollLine.show(sinceLanding > 0 ? sinceLanding : null);
    showTargetLine(sinceRest > 0);
    marks.dots.showLast(sinceRest > 0);
    return sinceRest > 0 && sinceRest < landingMoveS + landingHoldS + returnS * 0.5;
  }

  function onFrame(now: number): void {
    frameRequest = 0;
    if (disposed || contextLost) return;
    try {
      drawFrame(now);
    } catch (error) {
      giveUp(error);
      return;
    }
    notify();
  }

  function drawFrame(now: number): void {
    if (phase === 'flight' && lastFrameMs !== null) recordFrame(now - lastFrameMs);
    const t = advanceFlight(now);
    if (phase !== 'flight' && pendingStep !== null) {
      applyQualityStep(pendingStep);
      pendingStep = null;
    }
    const turning = turnWorld(now);
    greens.waveFlags(now / 1000);
    const landingShown = t !== null && focus?.tracer ? showFlight(focus, focus.tracer, t, now) : showAtRest();
    world.updateMatrixWorld();
    signs.layout(camera, width, height, landingShown, targetShown, reserved);
    renderer.render(scene, camera);
    frames += 1;
    if (phase === 'flight') {
      lastFrameMs = now;
      requestFrame();
    } else {
      lastFrameMs = null;
      if (turning) requestFrame();
    }
  }

  // rotation.y = b turns every point as rotateAboutTee(point, -b) does.
  function turnWorld(now: number): boolean {
    if (phase === 'flight' || world.rotation.y === aim) return false;
    if (turnStartMs === null) {
      turnStartMs = now;
      turnFrom = world.rotation.y;
    }
    const k = Math.min(1, (now - turnStartMs) / TURN_MS);
    world.rotation.y = k === 1 ? aim : turnFrom + (aim - turnFrom) * k * k * (3 - 2 * k);
    if (k === 1) turnStartMs = null;
    return k < 1;
  }

  function finishTurn(): void {
    world.rotation.y = aim;
    turnStartMs = null;
  }

  // Callbacks run last, so one that calls back into the scene finds it in a consistent state.
  function notify(): void {
    const progress = pendingProgress;
    const level = pendingQuality;
    pendingProgress = null;
    pendingQuality = null;
    if (level !== null) options.onQualityChange(level);
    if (progress) options.onFlight(progress);
  }

  try {
    const rand = seededRandom(RANGE_SEED);
    const noise = makeNoiseTexture(rand);
    environment = buildEnvironment(scene, rand, noise, options.quality === 2);
    world.add(...scene.children.filter((child) => child !== environment.sky));
    world.rotation.y = aim;
    scene.add(world);
    greens = buildGreens(world, rand, options.unitSystem);
    marks = buildFlightMarks(world, 1 + FAINT_FADES.length);
    trainingTarget = buildTrainingTarget(world);
    signs = createSignLayout(greens.signs, marks.landingSign, marks.targetSign);
    host.prepend(renderer.domElement);
    attachRenderer();
  } catch (error) {
    detachRenderer();
    disposeResources(scene);
    destroyRenderer(renderer);
    throw error;
  }
  placeCamera(TEE_POSE);
  requestFrame();
  // Signs painted before a•space loaded paint again once it is ready (textures.ts); show that frame.
  void displayFontPending()?.then(() => requestFrame());

  return {
    setShots,
    setAim(bearing) {
      if (disposed || bearing === aim) return;
      aim = bearing;
      turnStartMs = null;
      requestFrame();
    },
    setHighlight(index) {
      if (disposed) return;
      greens.highlight(index);
      requestFrame();
    },
    setRestTarget(target) {
      if (disposed) return;
      restTarget = target && { x: target.x, z: target.z };
      refreshRestView();
      placeTargetLine();
      requestFrame();
    },
    setTrainingTarget(distanceM) {
      if (disposed) return;
      trainingTarget.place(distanceM);
      requestFrame();
    },
    setOverlays(rects) {
      if (disposed) return;
      overlays = rects.map(([x0, y0, x1, y1]): LabelBox['rect'] => [x0, y0, x1, y1]);
      reserved = [...chrome, ...overlays];
      requestFrame();
    },
    setInsets(next) {
      if (disposed) return;
      insets = next;
      frameView();
      requestFrame();
    },
    resize() {
      if (disposed) return;
      applySize();
      requestFrame();
    },
    stats() {
      const { calls, triangles } = renderer.info.render;
      const programs = renderer.info.programs?.length ?? 0;
      return { calls, triangles, programs, quality, frames, animating: phase === 'flight' || world.rotation.y !== aim };
    },
    frameTimes() {
      const count = Math.min(framesLogged, FRAME_LOG_SIZE);
      return Array.from({ length: count }, (_, i) => frameLog[(framesLogged - count + i) % FRAME_LOG_SIZE]);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      phase = 'idle';
      cancelFrame();
      clearRestoreTimer();
      detachRenderer();
      disposeResources(scene);
      destroyRenderer(renderer);
    },
  };
}
