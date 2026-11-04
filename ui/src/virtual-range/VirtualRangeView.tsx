import {
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { ConfirmDeleteButton } from '../components/ConfirmDeleteButton';
import { EnrichmentProgress } from '../components/ShotProcessingArea';
import { getClubName } from '../data/clubs';
import { useI18n } from '../i18n/useI18n';
import { socketService } from '../services/socketService';
import { useUnitPreference } from '../state/useUnitPreference';
import { demoRowsMayShow, type ShotProcessingPhase } from '../stores/useShotStore';
import { latestDeletableShot, type Shot } from '../types/shot';
import { safeSessionStorage, safeStorage } from '../utils/storage';
import { formatSpeed, getDistanceUnit, getSpeedUnit, type UnitSystem } from '../utils/units';
import { withTypicalCarryMark } from '../utils/carryDisplay';
import { greenBearing, loadAims, loadTarget, NO_AIMS, saveAims, saveTarget } from './aim';
import { distanceValue } from './distance';
import {
  loadClubAverages,
  loadTraining,
  saveTraining,
  scoreShots,
  seeShots,
  startTarget,
  trainingOf,
  vanishedShots,
  withoutShots,
  withPlayer,
  withTarget,
  type TrainingState,
} from './game/training';
import { NO_INSETS, type RangeInsets } from './insets';
import { GREENS } from './layout';
import {
  createRangeController,
  LOADING_STATE,
  type OverlayRect,
  type RangeController,
  type RangeState,
  type SceneModule,
} from './rangeController';
import { demoFeed, startRangeDemo } from './rangeDemo';
import { isRangeDemo } from './rangeDemoSwitch';
import {
  hitMeanwhile,
  knownToday,
  LIVE_SOURCE,
  rangeNotice,
  rangeReadout,
  shotFeed,
  visibleRangeShots,
  type RangeAim,
  type RangeBar,
  type RangeSource,
  type ShotFeed,
} from './rangeViewModel';
import { TargetList } from './TargetList';
import { TrainingPanel } from './TrainingPanel';
import './virtualRange.css';

export type { RangeSource } from './rangeViewModel';

export interface VirtualRangeViewProps {
  shots: Shot[]; // live session shots (ignored for a saved source)
  club: string;
  profileId: string;
  sessionGeneration: number;
  source: RangeSource;
  processingPhase: ShotProcessingPhase | null; // names what the bar waits for while the newest shot is provisional
  demoActive: boolean;
  sessionDemo: boolean | null;
  // The app's chrome over the range: the scene centres its view between it. The bars here follow the
  // --virtual-range-inset-top/-bottom properties the app sets to the same px.
  insets?: RangeInsets;
  onBackToLive(): void;
  loadScene?: () => Promise<SceneModule>;
}

const NO_SHOTS: Shot[] = [];
const OVERLAYS =
  '.virtual-range__bar, .virtual-range__top, .virtual-range__targets, .virtual-range__training, .virtual-range__hint, .virtual-range__notice';
const loadRangeScene = (): Promise<SceneModule> => import('./scene/createRangeScene');

function Stat({
  kind,
  label,
  value,
  unit,
  title,
}: {
  kind: 'carry' | 'total' | 'target' | 'speed';
  label: string;
  value: string | null;
  unit: string;
  title?: string;
}): ReactElement {
  return (
    <div className={`virtual-range__stat virtual-range__stat--${kind}`} title={title}>
      <span className="virtual-range__label">{label}</span>
      <span className="virtual-range__value">
        {value ?? '—'}
        {value === null ? null : <small>{unit}</small>}
      </span>
    </div>
  );
}

function FlightBar({
  bar,
  unitSystem,
  children,
}: {
  bar: RangeBar;
  unitSystem: UnitSystem;
  children?: ReactNode;
}): ReactElement {
  const { t } = useI18n();
  const distance = (meters: number | null | undefined): string | null =>
    meters == null ? null : distanceValue(meters, unitSystem);
  const distanceUnit = getDistanceUnit(unitSystem);
  const carry = distance(bar.carryM);
  const carryValue = carry !== null && bar.typicalSpin ? withTypicalCarryMark(carry) : carry;
  return (
    <div className="virtual-range__bar">
      <strong className="virtual-range__club">{getClubName(bar.club)}</strong>
      <Stat
        kind="carry"
        label={t('metric.carry')}
        value={carryValue}
        unit={distanceUnit}
        title={
          [bar.typicalSpin ? t('carry.typicalSpinNote') : null, bar.curveIgnored ? t('carry.curveIgnored') : null]
            .filter(Boolean)
            .join(' · ') || undefined
        }
      />
      <Stat kind="total" label={t('virtualRange.total')} value={distance(bar.totalM)} unit={distanceUnit} />
      <Stat kind="target" label={t('virtualRange.toTarget')} value={distance(bar.targetM)} unit={distanceUnit} />
      <Stat
        kind="speed"
        label={t('metric.ballSpeed')}
        value={formatSpeed(bar.ballSpeedMph, unitSystem)}
        unit={getSpeedUnit(unitSystem)}
      />
      {children}
    </div>
  );
}

export function VirtualRangeView({
  shots,
  club,
  profileId,
  sessionGeneration,
  source,
  processingPhase,
  demoActive,
  sessionDemo,
  insets = NO_INSETS,
  onBackToLive,
  loadScene = loadRangeScene,
}: VirtualRangeViewProps): ReactElement {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const hostRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<RangeController | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const overlaysRef = useRef('');
  const [demo] = useState(() => typeof window !== 'undefined' && isRangeDemo(window.location.search));
  const [state, setState] = useState<RangeState>(LOADING_STATE);
  const [target, setTarget] = useState(() => loadTarget(safeSessionStorage));
  const [aims, setAims] = useState<ReadonlyMap<string, number>>(() => loadAims(safeSessionStorage));
  const todayIds = useMemo(
    () => visibleRangeShots(LIVE_SOURCE, shots, profileId).map((shot) => shot.id),
    [shots, profileId]
  );
  // Today's shots when a saved session opened: one hit after that takes the range back to today, unseen, so it flies.
  const view = { source, profileId, session: sessionGeneration };
  const [knownState, setKnownState] = useState(() => knownToday(null, view, todayIds));
  const known = knownToday(knownState, view, todayIds);
  if (known !== knownState) setKnownState(known);
  const hitWhileSaved = hitMeanwhile(known, todayIds);
  const saved = source.kind === 'saved' && !hitWhileSaved ? source : null;
  const liveShots = saved ? NO_SHOTS : shots;
  const liveProfileId = saved ? '' : profileId;
  const seen = hitWhileSaved ? known.ids : undefined;
  const feed = useMemo(
    () =>
      demo
        ? demoFeed()
        : shotFeed(saved ?? LIVE_SOURCE, liveShots, liveProfileId, { seen, session: sessionGeneration }),
    [demo, saved, liveShots, liveProfileId, seen, sessionGeneration]
  );

  const [trainings, setTrainings] = useState(() => loadTraining(safeStorage));
  const [heldTraining, setHeldTraining] = useState<{ profileId: string; shotId: string; before: TrainingState } | null>(
    null
  );
  const [startingTraining, setStartingTraining] = useState(false);
  const trainingStartRef = useRef<AbortController | null>(null);
  const [seenShots, setSeenShots] = useState(() => seeShots(null, feed, todayIds).seen);
  const serverDemo = demoRowsMayShow(demoActive, sessionDemo);
  const [serverDemoSeen, setServerDemoSeen] = useState(serverDemo);
  if (serverDemoSeen !== serverDemo) {
    setServerDemoSeen(serverDemo);
    if (!serverDemo) {
      setTrainings(loadTraining(safeStorage));
      setHeldTraining(null);
    }
  }
  const training = trainingOf(trainings, profileId);
  const setTraining = (next: TrainingState | null): void =>
    setTrainings((current) => withPlayer(current, profileId, next));
  const trainingOn = training !== null && !saved && !demo;
  const trainingTargetM = trainingOn ? training.targetM : null;

  const fixedAim = saved || demo;
  const shownTarget = fixedAim || trainingOn ? null : target;
  const bearing = shownTarget === null ? 0 : greenBearing(GREENS[shownTarget]);

  const backToToday = useEffectEvent(() => onBackToLive());
  useEffect(() => {
    if (hitWhileSaved) backToToday();
  }, [hitWhileSaved]);

  const [, setTargetsOpen] = useState(false);
  const chooseTarget = (next: number | null): void => {
    if (fixedAim || trainingOn) return;
    setTarget(next);
    saveTarget(safeSessionStorage, next);
  };

  const tapGreen = useEffectEvent((index: number) => chooseTarget(target === index ? null : index));

  const unseen = fixedAim ? [] : feed.shots.filter((shot) => !aims.has(shot.id));
  if (unseen.length > 0) setAims(new Map([...aims, ...unseen.map((shot) => [shot.id, bearing] as const)]));
  const shownAims = fixedAim ? NO_AIMS : aims;

  const [scoredFeed, setScoredFeed] = useState(feed);
  if (scoredFeed !== feed) {
    setScoredFeed(feed);
    const gone = new Set(vanishedShots(scoredFeed, feed));
    if (gone.size > 0) {
      setTrainings((current) =>
        Object.fromEntries(Object.entries(current).map(([player, state]) => [player, withoutShots(state, gone)]))
      );
      if (heldTraining && gone.has(heldTraining.shotId)) setHeldTraining(null);
    }
  }

  const sighting = seeShots(seenShots, feed);
  if (sighting.seen !== seenShots) {
    setSeenShots(sighting.seen);
    if (trainingOn && sighting.fresh.length > 0) {
      setTraining(scoreShots(training, feed.shots, sighting.fresh));
      setHeldTraining({ profileId, shotId: sighting.fresh[sighting.fresh.length - 1], before: training });
    }
  }

  useEffect(() => {
    saveAims(safeSessionStorage, aims);
  }, [aims]);

  useEffect(() => {
    if (!serverDemo) saveTraining(safeStorage, trainings);
  }, [trainings, serverDemo]);

  useEffect(() => () => trainingStartRef.current?.abort(), []);

  const startTraining = (): void => {
    if (startingTraining || training) return;
    const request = new AbortController();
    const player = profileId;
    trainingStartRef.current = request;
    setStartingTraining(true);
    void loadClubAverages(club, player, request.signal).then((averages) => {
      if (request.signal.aborted) return;
      trainingStartRef.current = null;
      setStartingTraining(false);
      setHeldTraining(null);
      setTrainings((current) => withPlayer(current, player, { targetM: startTarget(averages), recent: [] }));
    });
  };

  const endTraining = (): void => {
    setTraining(null);
    setHeldTraining(null);
  };

  const stepTraining = (deltaM: number): void => {
    setTrainings((current) => {
      const state = trainingOf(current, profileId);
      return state ? withPlayer(current, profileId, withTarget(state, state.targetM + deltaM)) : current;
    });
  };

  const showFeed = useEffectEvent((next: ShotFeed) => {
    controllerRef.current?.show(next, shownAims);
  });

  // Takes the unit and loader of the render that mounts it; a later unit goes through setUnitSystem.
  const startController = useEffectEvent((host: HTMLElement) => {
    const mountedAt = performance.now();
    return createRangeController({
      host,
      unitSystem,
      insets,
      // A step-down holds through reloads; a restarted kiosk measures at full quality again.
      storage: safeSessionStorage,
      loadScene,
      onState: setState,
      onScene: demo ? (scene, fly) => startRangeDemo(scene, fly, { mountedAt }) : undefined,
      onGreenTap: (index) => tapGreen(index),
    });
  });

  const measureOverlays = useEffectEvent(() => {
    const host = hostRef.current;
    const section = sectionRef.current;
    if (!host || !section) return;
    const origin = host.getBoundingClientRect();
    const rects = [...section.querySelectorAll(OVERLAYS)].map((element): OverlayRect => {
      const { left, top, right, bottom } = element.getBoundingClientRect();
      return [left - origin.left, top - origin.top, right - origin.left, bottom - origin.top];
    });
    const key = rects.flat().map(Math.round).join(',');
    if (key === overlaysRef.current) return;
    overlaysRef.current = key;
    controllerRef.current?.setOverlays(rects);
  });

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const controller = startController(host);
    controllerRef.current = controller;
    const resizeObserver = new ResizeObserver(() => {
      controller.resize();
      measureOverlays();
    });
    resizeObserver.observe(host);
    void document.fonts?.ready.then(() => measureOverlays());
    return () => {
      resizeObserver.disconnect();
      controller.dispose();
      controllerRef.current = null;
    };
  }, []);

  useLayoutEffect(() => {
    measureOverlays();
  });

  useLayoutEffect(() => {
    controllerRef.current?.setUnitSystem(unitSystem);
  }, [unitSystem]);

  useLayoutEffect(() => {
    controllerRef.current?.setInsets(insets);
  }, [insets]);

  useLayoutEffect(() => {
    controllerRef.current?.setAim(bearing);
    controllerRef.current?.setHighlight(shownTarget);
  }, [bearing, shownTarget]);

  useLayoutEffect(() => {
    controllerRef.current?.setTrainingTarget(trainingTargetM);
  }, [trainingTargetM]);

  useLayoutEffect(() => {
    showFeed(feed);
  }, [feed]);

  const aim: RangeAim = { selected: shownTarget, aims: shownAims, trainingTargetM };
  const flying = state.flight;
  const shownTraining =
    heldTraining?.profileId === profileId && flying?.shotId === heldTraining.shotId && flying.rested !== true
      ? heldTraining.before
      : training;
  const { bar, target: restTarget } = rangeReadout(feed.shots, state.flight, aim);
  const [restShotId, restX, restZ] = restTarget ? [restTarget.shotId, restTarget.x, restTarget.z] : [null, 0, 0];
  useLayoutEffect(() => {
    controllerRef.current?.setRestTarget(restShotId === null ? null : { x: restX, z: restZ });
  }, [restShotId, restX, restZ]);
  const latest = fixedAim ? null : latestDeletableShot(shots, profileId);
  const deletable = latest?.timestamp === restShotId ? latest : null;
  const notice = rangeNotice(state.status);
  return (
    <section className="virtual-range" ref={sectionRef}>
      <div className="virtual-range__host" ref={hostRef} />
      {notice ? (
        <p className="virtual-range__notice" role="status">
          {t(notice)}
        </p>
      ) : null}
      {saved ? (
        <div className="virtual-range__top">
          <span className="virtual-range__session">{t('virtualRange.saved', { date: saved.label })}</span>
          <button type="button" className="virtual-range__back" onClick={onBackToLive}>
            {t('virtualRange.backToLive')}
          </button>
        </div>
      ) : trainingOn ? (
        <TrainingPanel
          targetM={training.targetM}
          shown={shownTraining ?? training}
          unitSystem={unitSystem}
          onStep={stepTraining}
          onEnd={endTraining}
        />
      ) : demo ? null : (
        <div className="virtual-range__top virtual-range__top--live">
          <button type="button" className="virtual-range__mode" disabled={startingTraining} onClick={startTraining}>
            {t('virtualRange.training')}
          </button>
          <TargetList selected={target} unitSystem={unitSystem} onSelect={chooseTarget} onOpenChange={setTargetsOpen} />
        </div>
      )}
      {feed.holding ? (
        <div className="virtual-range__bar virtual-range__bar--processing">
          <EnrichmentProgress phase={processingPhase} />
        </div>
      ) : bar ? (
        <FlightBar bar={bar} unitSystem={unitSystem}>
          {fixedAim ? null : (
            <ConfirmDeleteButton
              key={deletable?.timestamp ?? ''}
              className="virtual-range__delete"
              label={t('shots.deleteShort')}
              ariaLabel={t('shots.deleteLast')}
              disabled={deletable === null}
              onConfirm={() => (deletable ? socketService.deleteShot(deletable.timestamp) : undefined)}
            />
          )}
        </FlightBar>
      ) : (
        <p className="virtual-range__hint">{t('virtualRange.empty')}</p>
      )}
    </section>
  );
}
