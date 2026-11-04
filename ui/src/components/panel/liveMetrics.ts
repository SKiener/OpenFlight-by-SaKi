import type { CurveClass, Shot, SpinFields, SpinQuality, SwingSpeedStats } from '../../types/shot';
import { getCarryYards, getDisplayedSpin, getSwingSpeedMph, isBallSpeedUncertain, isSwingSpeedShot } from '../../types/shot';
import type { UnitSystem } from '../../utils/units';
import { formatNumber, formatDistance, formatSpeed, getDistanceUnit, getSpeedUnit } from '../../utils/units';
import { getHtmlLang, t } from '../../i18n';
import { carryTooltip, isTypicalSpinCarry, markTypicalCarry, typicalCarryCaption } from '../../utils/carryDisplay';
import { isWoodClub } from '../../data/clubs';

/** Placeholder for a metric the current shot has no value for. */
export const NO_VALUE = '—';

export interface LiveMetric {
  /** Stable key. Persisted as the promoted-hero choice, so do not rename. */
  id: string;
  label: string;
  value: string;
  unit?: string;
  subtext?: string;
  /** True when the value is modeled rather than measured. Rendered as an icon. */
  estimated?: boolean;
  /** The icon's hidden label when it is not "estimated" (a calculated club value). */
  estimatedLabel?: string;
  confidence?: SpinQuality | null;
  /** Override confidence copy while preserving its dot level. */
  confidenceLabel?: string;
  /** Tooltip for details the tile has no room for. */
  title?: string;
  /** 'class': the value is a word (Draw, Gerade); sized on its own, not in the shared number fit. */
  kind?: 'class';
}

/**
 * Live table: eleven metrics, always the same eleven in the same canonical order,
 * so the grid never reflows between shots. Metrics the shot did not produce render
 * as {@link NO_VALUE} rather than disappearing. The selected metric is then
 * pinned to the top-left by {@link pinSelectedMetric}.
 */
export const LIVE_METRIC_COUNT = 11;

/** Metric count for a swing-speed session: a single 5-tile row. */
export const SWING_METRIC_COUNT = 5;

export function formatOptionalAngle(value: number | null, signed = false): string {
  if (value === null) return NO_VALUE;
  const prefix = signed && value >= 0 ? '+' : '';
  return `${prefix}${formatNumber(value, 1)}`;
}

export function angleUnit(value: number | null): string | undefined {
  return value === null ? undefined : '°';
}

/** Whole rpm with the language's thousands separator ("10,000", "10.000"). */
export function formatSpinRpm(rpm: number): string {
  return rpm.toLocaleString(getHtmlLang(), { maximumFractionDigits: 0 });
}

/** Marks a spin that was not measured where a table has no room for the tile's caption. */
const NOT_MEASURED_MARK = '≈';

/**
 * Spin for a table cell (shot list, session history): the value the Spin tile
 * shows, with ≈ in front when it was not measured, or {@link NO_VALUE}.
 */
export function formatSpinCell(shot: SpinFields): string {
  const spin = getDisplayedSpin(shot);
  if (!spin) return NO_VALUE;
  const rpm = formatSpinRpm(spin.rpm);
  return spin.source === 'measured' ? rpm : `${NOT_MEASURED_MARK}${rpm}`;
}

/**
 * The spin the carry used (see {@link getDisplayedSpin}). A measured spin keeps
 * its quality; a typical one is marked estimated and captioned "typical", with
 * no quality, since the radar's grade belongs to the reading it replaced.
 * Calculated spin stays as before: marked estimated, with the shot's quality.
 */
function buildSpinMetric(shot: Shot): LiveMetric {
  const spin = getDisplayedSpin(shot);
  const quality = spin !== null && spin.source !== 'typical' ? shot.spin_quality : null;

  return {
    id: 'spin',
    label: t('metric.spin'),
    value: spin === null ? NO_VALUE : formatSpinRpm(spin.rpm),
    unit: spin === null ? undefined : 'rpm',
    subtext: spin?.source === 'typical' ? t('metric.typical') : undefined,
    estimated: markEstimated(spin !== null && spin.source !== 'measured'),
    confidence: quality,
    confidenceLabel: quality === 'experimental' ? t('range.spinUnverified') : undefined,
  };
}

function launchAngleQuality(confidence: number | null): SpinQuality | null {
  if (confidence === null) return null;
  if (confidence >= 0.7) return 'high';
  if (confidence >= 0.4) return 'medium';
  return 'low';
}

export interface LiveMetricOptions {
  /** The TI start direction's phase zero is calibrated (the server's `calibration.horizontal_phase_reference`). */
  directionCalibrated?: boolean;
}

/**
 * The start direction's own grade. Until the TI phase zero is calibrated, a radar
 * direction is the raw bearing, so it is called "not calibrated" instead of graded:
 * its phase coherence reads high on almost every shot. Rows saved without a source
 * carry the radar's direction too, unless their angles were estimated.
 * "Not calibrated" is a caption qualifier ({@link startDirectionSubtext}), not a
 * pill: in the nowrap confidence row it ran past narrow details tiles.
 */
export function startDirectionUncalibrated(shot: Shot, calibrated: boolean): boolean {
  if (shot.launch_angle_horizontal === null) return false;
  const source = shot.launch_angle_horizontal_source;
  const fromRadar = source === 'radar' || (source == null && shot.angle_source !== 'estimated');
  return fromRadar && !calibrated;
}

function startDirectionQuality(shot: Shot, calibrated: boolean): Pick<LiveMetric, 'confidence' | 'confidenceLabel'> {
  if (shot.launch_angle_horizontal === null || startDirectionUncalibrated(shot, calibrated)) return { confidence: null };
  const source = shot.launch_angle_horizontal_source;
  return {
    confidence: launchAngleQuality(shot.launch_angle_horizontal_confidence ?? null),
    confidenceLabel: source === 'camera_assisted_experimental' ? t('quality.experimental') : undefined,
  };
}

/** The start direction's caption: "camera assisted" and/or "not calibrated", joined by " · ". */
function startDirectionSubtext(shot: Shot, calibrated: boolean): string | undefined {
  return (
    [
      shot.launch_angle_horizontal_source === 'camera_assisted_experimental' ? t('quality.cameraAssisted') : undefined,
      startDirectionUncalibrated(shot, calibrated) ? t('metric.uncalibrated') : undefined,
    ]
      .filter(Boolean)
      .join(' · ') || undefined
  );
}

export function markEstimated(isEstimated: boolean): true | undefined {
  return isEstimated ? true : undefined;
}

/**
 * An experimental club value is shown only once its estimator accepted it. Unaccepted TI
 * candidates and the chained camera estimate stay in the session log, never in a tile.
 */
export function acceptedExperimental(value: number | null | undefined, status: string | null | undefined): number | null {
  return value != null && status?.startsWith('accepted') ? value : null;
}

/**
 * The status an experimental club tile shows: an accepted value's, or why TI rejected one.
 * A candidate status under a withheld value would read as if a value existed.
 */
export function shownExperimentalStatus(value: number | null | undefined, status: string | null | undefined): string | null {
  if (acceptedExperimental(value, status) != null) return status ?? null;
  return status?.startsWith('rejected_') ? status : null;
}

function experimentalStatus(status: string | null | undefined): string {
  if (!status || status === 'candidate_available') return t('quality.candidate');
  if (status.startsWith('rejected_')) return t('quality.rejected', { reason: status.slice(9).replaceAll('_', ' ') });
  return status.replaceAll('_', ' ');
}

/** Caption of a calculated value: optional detail parts, then "calculated". */
export function calculatedCaption(...parts: Array<string | undefined>): string {
  return [...parts.filter(Boolean), t('metric.calculated')].join(' · ');
}

/**
 * The ≈ label of a calculated value without ground truth: "calculated · uncalibrated".
 * The camera's club path, attack angle and low point have none yet. On 2026-10-25
 * (daylight) the camera's iron and wedge attack angle read 7.4° steeper than
 * Toptracer on average (TT's own re-computation moves it only 0.5°). The frames do
 * show the head still descending steeply just before the ball, so how much of that
 * gap is the camera's is open (analysis/range-20261102/schlaegerdaten-replay/
 * 40-eisen-aoa/bericht.md). The low point (median 23 cm ahead of the ball) is the
 * bottom of the fitted arc, below the mat that stops the head. Every screen that
 * shows one of them marks it with ≈: this label, its caption, the shot list's own
 * "≈ calculated … (uncalibrated)" line, or the kiosk's bare ≈.
 */
export function uncalibratedLabel(): string {
  return `${t('metric.calculated')} · ${t('impact.uncalibrated')}`;
}

/** Caption of such a value: optional detail parts, then "calculated · uncalibrated". */
export function uncalibratedCaption(...parts: Array<string | undefined>): string {
  return [...parts.filter(Boolean), uncalibratedLabel()].join(' · ');
}

/** "±0,9°" for a finite sigma, else undefined. */
export function sigmaDetail(sigma: number | null | undefined): string | undefined {
  return typeof sigma === 'number' && Number.isFinite(sigma)
    ? t('clubData.sigma', { value: formatNumber(sigma, 1) })
    : undefined;
}

/**
 * A calculated tile from its formatted value: ≈ labelled "calculated", and the
 * caption "detail · … · calculated". `uncalibrated` (no ground truth yet) makes
 * both "calculated · uncalibrated". Without a value (null): {@link NO_VALUE}, no
 * caption and no mark.
 */
export function calculatedMetric(
  id: string,
  label: string,
  shown: { value: string; unit?: string } | null,
  details: ReadonlyArray<string | undefined>,
  { uncalibrated = false }: { uncalibrated?: boolean } = {},
): LiveMetric {
  const caption = uncalibrated ? uncalibratedCaption : calculatedCaption;
  return {
    id,
    label,
    value: shown === null ? NO_VALUE : shown.value,
    unit: shown?.unit,
    subtext: shown === null ? undefined : caption(...details),
    estimated: markEstimated(shown !== null),
    estimatedLabel: shown === null ? undefined : uncalibrated ? uncalibratedLabel() : t('metric.calculated'),
  };
}

/**
 * A calculated angle tile: the number, ≈ labelled "calculated", and the caption
 * "±σ° · calculated" (just "calculated" without a sigma). `uncalibrated` (no
 * ground truth yet) makes both "calculated · uncalibrated".
 */
export function calculatedAngleMetric(
  id: string,
  label: string,
  value: number | null | undefined,
  signed: boolean,
  sigma?: number | null,
  options: { uncalibrated?: boolean } = {},
): LiveMetric {
  const shown = typeof value === 'number' && Number.isFinite(value) ? value : null;
  return calculatedMetric(
    id,
    label,
    shown === null ? null : { value: formatOptionalAngle(shown, signed), unit: angleUnit(shown) },
    [sigmaDetail(sigma)],
    options,
  );
}

/**
 * True when a tile of these shows a calculated value (its ≈ mark is labelled
 * "calculated", or "calculated · uncalibrated").
 */
export function hasCalculatedMetric(metrics: readonly LiveMetric[]): boolean {
  const labels = new Set([t('metric.calculated'), uncalibratedLabel()]);
  return metrics.some((metric) => metric.estimated === true && labels.has(metric.estimatedLabel ?? ''));
}

const CURVE_CLASSES: readonly CurveClass[] = ['hook', 'draw', 'straight', 'fade', 'slice'];

/** The server's curve class, when it is one of the five words; else null. */
export function shownCurveClass(shot: Shot): CurveClass | null {
  const cls = shot.curve_class;
  return typeof cls === 'string' && CURVE_CLASSES.includes(cls) ? cls : null;
}

/**
 * The curve tile: the class word of the server's `curve_class` (set from the same
 * calculated axis the flight is drawn with), never the axis number, and never
 * re-derived from `spin_axis_deg`. "—" without one; woods get no class by design.
 */
export function curveMetric(shot: Shot): LiveMetric {
  const id = 'curve';
  const label = t('metric.curve');
  const cls = shownCurveClass(shot);
  if (cls === null) {
    return { id, label, value: NO_VALUE, subtext: isWoodClub(shot.club) ? t('clubData.notForWoods') : undefined };
  }
  return {
    id,
    label,
    value: t(`curve.${cls}`),
    kind: 'class',
    subtext: calculatedCaption(),
    estimated: true,
    estimatedLabel: t('metric.calculated'),
  };
}

/** The radar's attack angle, or an accepted experimental one; null without either. */
export function radarAttackAngle(shot: Shot): number | null {
  return shot.club_angle_deg ?? acceptedExperimental(shot.experimental_attack_angle_deg, shot.experimental_attack_angle_status);
}

/** The radar's club path, or an accepted experimental one; null without either. */
export function radarClubPath(shot: Shot): number | null {
  return shot.club_path_deg ?? acceptedExperimental(shot.experimental_club_path_deg, shot.experimental_club_path_status);
}

/** The attack angle a tile shows: the camera's when the radar has no attack angle of its own (as on the Live tile). */
export function shownAttackAngle(shot: Shot): number | null {
  return shot.club_angle_deg == null && shot.camera_attack_angle_deg != null ? shot.camera_attack_angle_deg : radarAttackAngle(shot);
}

/** The club path a tile shows: the camera's when the radar has no club path of its own (as on the Live tile). */
export function shownClubPath(shot: Shot): number | null {
  return shot.club_path_deg == null && shot.camera_club_path_deg != null ? shot.camera_club_path_deg : radarClubPath(shot);
}

/**
 * A radar club path or attack angle tile (the radar's own, or an accepted experimental
 * one). It is measured, not calculated, so it gets no ≈; it has no ground truth yet
 * either, so a value is captioned "not calibrated". An experimental value keeps its
 * quality dot; without a value a rejected experimental status still says why.
 */
function radarAngleMetric(id: string, label: string, value: number | null, experimentalStatusValue: string | null): LiveMetric {
  const experimental = experimentalStatusValue != null;
  return {
    id,
    label,
    value: formatOptionalAngle(value, true),
    unit: angleUnit(value),
    subtext:
      value !== null ? t('metric.uncalibrated') : experimental ? experimentalStatus(experimentalStatusValue) : undefined,
    confidence: experimental ? 'experimental' : null,
  };
}

/**
 * The apex: the top of the simulated flight behind the carry, a model value like the
 * carry, in its distance unit as a whole number. A typical-spin carry's apex reads
 * like that carry ("≈ 28", "with typical spin"); any other one is ≈ "calculated".
 * {@link NO_VALUE} without one (a table carry, an older saved shot).
 */
export function apexMetric(shot: Shot, unitSystem: UnitSystem): LiveMetric {
  const id = 'apex';
  const label = t('metric.apex');
  const apex = shot.apex_yards;
  if (typeof apex !== 'number' || !Number.isFinite(apex)) return { id, label, value: NO_VALUE };
  const typical = isTypicalSpinCarry(shot);
  return {
    id,
    label,
    value: markTypicalCarry(shot, formatDistance(apex, unitSystem, 0)),
    unit: getDistanceUnit(unitSystem),
    subtext: typical ? t('carry.typicalSpin') : t('metric.calculated'),
    estimated: markEstimated(!typical),
    estimatedLabel: typical ? undefined : t('metric.calculated'),
  };
}

function buildBallStrikeMetrics(shot: Shot, unitSystem: UnitSystem, options: LiveMetricOptions): LiveMetric[] {
  const speedUnit = getSpeedUnit(unitSystem);
  const carry = getCarryYards(shot);
  const angleConfidence = launchAngleQuality(shot.launch_angle_confidence);
  const angleEstimated = shot.angle_source === 'estimated';
  const attackAngle = radarAttackAngle(shot);
  const attackStatus = shownExperimentalStatus(shot.experimental_attack_angle_deg, shot.experimental_attack_angle_status);
  const attackIsExperimental = shot.club_angle_deg === null && attackStatus != null;
  const clubPath = radarClubPath(shot);
  const clubPathStatus = shownExperimentalStatus(shot.experimental_club_path_deg, shot.experimental_club_path_status);
  const clubPathIsExperimental = shot.club_path_deg === null && clubPathStatus != null;
  const ballSpeedUncertain = isBallSpeedUncertain(shot);

  return [
    {
      id: 'ball_speed',
      label: t('metric.ballSpeed'),
      value: formatSpeed(shot.ball_speed_mph, unitSystem, 1),
      unit: speedUnit,
      estimated: markEstimated(ballSpeedUncertain),
    },
    {
      id: 'carry',
      label: t('metric.carry'),
      value: markTypicalCarry(shot, formatDistance(carry, unitSystem, 0)),
      unit: getDistanceUnit(unitSystem),
      // A typical-spin carry names its spin: "with typical spin".
      subtext:
        shot.carry_spin_adjusted === null ? undefined : (typicalCarryCaption(shot) ?? t('metric.spinAdjusted')),
      estimated: markEstimated(shot.carry_spin_adjusted === null || ballSpeedUncertain),
      title: carryTooltip(shot),
    },
    apexMetric(shot, unitSystem),
    {
      id: 'club_speed',
      label: t('metric.clubSpeed'),
      value: shot.club_speed_mph === null ? NO_VALUE : formatSpeed(shot.club_speed_mph, unitSystem, 1),
      unit: shot.club_speed_mph === null ? undefined : speedUnit,
    },
    {
      id: 'smash',
      label: t('metric.smash'),
      value: shot.smash_factor === null ? NO_VALUE : formatNumber(shot.smash_factor, 2),
      estimated: markEstimated(shot.smash_factor !== null && ballSpeedUncertain),
    },
    {
      id: 'launch_v',
      label: t('metric.vLaunch'),
      value: formatOptionalAngle(shot.launch_angle_vertical),
      unit: angleUnit(shot.launch_angle_vertical),
      estimated: markEstimated(shot.launch_angle_vertical !== null && angleEstimated),
      confidence: shot.launch_angle_vertical === null ? null : angleConfidence,
    },
    {
      id: 'launch_h',
      label: t('metric.hLaunch'),
      value: formatOptionalAngle(shot.launch_angle_horizontal, true),
      unit: angleUnit(shot.launch_angle_horizontal),
      subtext: startDirectionSubtext(shot, options.directionCalibrated === true),
      estimated: markEstimated(shot.launch_angle_horizontal !== null && angleEstimated),
      ...startDirectionQuality(shot, options.directionCalibrated === true),
    },
    buildSpinMetric(shot),
    curveMetric(shot),
    shot.club_path_deg == null && shot.camera_club_path_deg != null
      ? calculatedAngleMetric('club_path', t('metric.clubPath'), shot.camera_club_path_deg, true, shot.camera_club_path_sigma_deg, { uncalibrated: true })
      : radarAngleMetric('club_path', t('metric.clubPath'), clubPath, clubPathIsExperimental ? clubPathStatus : null),
    shot.club_angle_deg == null && shot.camera_attack_angle_deg != null
      ? calculatedAngleMetric('club_aoa', t('metric.clubAoa'), shot.camera_attack_angle_deg, true, shot.camera_attack_angle_sigma_deg, { uncalibrated: true })
      : radarAngleMetric('club_aoa', t('metric.clubAoa'), attackAngle, attackIsExperimental ? attackStatus : null),
  ];
}

function buildSwingSpeedMetrics(shot: Shot, stats: SwingSpeedStats, unitSystem: UnitSystem): LiveMetric[] {
  const speedUnit = getSpeedUnit(unitSystem);

  return [
    {
      id: 'swing_last',
      label: t('metric.lastSwing'),
      value: formatSpeed(getSwingSpeedMph(shot), unitSystem, 1),
      unit: speedUnit,
    },
    {
      id: 'swing_best',
      label: t('metric.best'),
      value: formatSpeed(stats.best_speed_mph, unitSystem, 1),
      unit: speedUnit,
      subtext: t('metric.profileImplement'),
    },
    {
      id: 'swing_avg',
      label: t('metric.average'),
      value: formatSpeed(stats.avg_speed_mph, unitSystem, 1),
      unit: speedUnit,
      subtext: t('metric.swingsCount', { count: stats.count }),
    },
    {
      id: 'swing_count',
      label: t('metric.swings'),
      value: String(stats.count),
      subtext:
        shot.swing_speed_reading_count === undefined
          ? undefined
          : t('metric.readingsCount', { count: shot.swing_speed_reading_count }),
    },
    {
      id: 'swing_implement',
      label: t('metric.implement'),
      value: shot.training_implement_label ?? shot.club,
      subtext:
        shot.swing_speed_trigger_mph === undefined
          ? undefined
          : t('metric.trigger', {
              speed: formatSpeed(shot.swing_speed_trigger_mph, unitSystem, 1),
              unit: speedUnit,
            }),
    },
  ];
}

/**
 * Carry provenance: provisional before final ballistics, "with typical spin" for a
 * club-typical carry, estimated while any other spin is unconfirmed.
 */
export function carrySourceLabel(shot: Shot): string | undefined {
  if (shot.carry_spin_adjusted == null) return t('range.carryProvisional');
  if (shot.carry_spin_source === 'club_typical') return t('carry.typicalSpin');
  if (shot.carry_spin_source && shot.carry_spin_source !== 'measured') return t('range.carrySpinEstimated');
  return undefined;
}

export function carrySubtext(shot: Shot, unitSystem: UnitSystem): string {
  const parts: string[] = [];
  const totalYards = shot.total_yards;
  if (shot.carry_spin_adjusted != null) {
    const distance =
      totalYards != null && Number.isFinite(totalYards)
        ? `${formatDistance(totalYards, unitSystem, 0)} ${getDistanceUnit(unitSystem)}`
        : NO_VALUE;
    parts.push(t('range.total', { distance }));
  }
  const source = carrySourceLabel(shot);
  if (source) parts.push(source);
  return parts.join(' · ');
}

/**
 * Build the fixed metric list for the Live panel. Returns {@link LIVE_METRIC_COUNT}
 * entries for a ball-strike shot and {@link SWING_METRIC_COUNT} for a swing-speed
 * one; the two sets share no ids, so a selected-metric choice never leaks across
 * modes.
 */
export function buildLiveMetrics(
  shot: Shot,
  unitSystem: UnitSystem,
  swingStats: SwingSpeedStats,
  options: LiveMetricOptions = {}
): LiveMetric[] {
  return isSwingSpeedShot(shot)
    ? buildSwingSpeedMetrics(shot, swingStats, unitSystem)
    : buildBallStrikeMetrics(shot, unitSystem, options);
}

/**
 * Put the selected metric first (top-left of the table) and keep the rest in
 * canonical order. Falls back to the first metric when `selectedId` is absent
 * from this list — which is the normal case right after switching between
 * ball-strike and swing-speed modes.
 */
export function pinSelectedMetric(metrics: LiveMetric[], selectedId: string | null): LiveMetric[] {
  if (metrics.length === 0) {
    return [];
  }

  const selectedIndex = metrics.findIndex((metric) => metric.id === selectedId);
  const index = selectedIndex === -1 ? 0 : selectedIndex;

  return [metrics[index], ...metrics.filter((_, i) => i !== index)];
}
