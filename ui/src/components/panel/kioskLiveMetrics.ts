import type { Shot } from '../../types/shot';
import { getCarryYards, getDisplayedSpin } from '../../types/shot';
import type { CameraCaptureSettings } from '../../stores/useCameraStore';
import type { LiveSlotId } from '../../stores/useLiveSlotsStore';
import {
  formatDistance,
  formatNumber,
  formatSpeed,
  getDistanceUnit,
  getSpeedUnit,
  type UnitSystem,
} from '../../utils/units';
import { isTypicalSpinCarry } from '../../utils/carryDisplay';
import { t } from '../../i18n';
import {
  buildLiveMetrics,
  formatSpinRpm,
  NO_VALUE,
  shownAttackAngle,
  shownClubPath,
  shownCurveClass,
  startDirectionUncalibrated,
  type LiveMetricOptions,
} from './liveMetrics';
import { impactFaceInput, impactZoneWord, type ImpactFaceInput } from './impactPoint';

/**
 * One tile of the kiosk's Live screen, read from 1.5–2.5 m: a short title with the
 * unit, a whole number and nothing else. The long notes (total, source, quality)
 * stay in "All values".
 */
export interface KioskTile {
  id: LiveSlotId;
  /** Short title: "Flugweite", "Richtung". */
  label: string;
  /** Shown in the title after the label (and the ≈ mark): "m", "km/h", "rpm". Angles carry "°" in the value. */
  unit?: string;
  value: string;
  /** Estimated, calculated or uncalibrated: the title shows ≈. */
  approx: boolean;
  /** 'class': the value is a word (Draw, Gerade). 'impact': the impact face over its zone word. */
  kind?: 'class' | 'impact';
  /** What the impact tile draws (kind 'impact'). */
  face?: ImpactFaceInput;
}

/** A whole number; -0 becomes 0 so a small negative never reads "-0". */
function whole(value: number): number {
  return Math.round(value) || 0;
}

/** A whole angle with "°": "16°", or "+3°"/"-4°" when `signed`; {@link NO_VALUE} without one. */
export function formatKioskAngle(value: number | null | undefined, signed = false): string {
  if (value == null || !Number.isFinite(value)) return NO_VALUE;
  const rounded = whole(value);
  return `${signed && rounded > 0 ? '+' : ''}${formatNumber(rounded, 0)}°`;
}

/**
 * The start direction as a side: "2° R", "3° L" (+ is right, as on the Live tile);
 * "0°" under half a degree either way.
 */
export function formatKioskDirection(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return NO_VALUE;
  if (Math.abs(value) < 0.5) return '0°';
  return `${formatNumber(whole(Math.abs(value)), 0)}° ${t(value > 0 ? 'kioskLive.right' : 'kioskLive.left')}`;
}

/** Whole rpm with the language's thousands separator ("10.000"). */
export function formatKioskSpin(rpm: number | null | undefined): string {
  return rpm == null || !Number.isFinite(rpm) ? NO_VALUE : formatSpinRpm(whole(rpm));
}

/** Smash keeps its two decimals ("1,45"). */
export function formatKioskSmash(smash: number | null | undefined): string {
  return smash == null || !Number.isFinite(smash) ? NO_VALUE : formatNumber(smash, 2);
}

/** The total distance, once the final ballistics gave one; null before (as the carry note's "Total —"). */
function totalYards(shot: Shot): number | null {
  const total = shot.total_yards;
  return shot.carry_spin_adjusted != null && total != null && Number.isFinite(total) ? total : null;
}

/** The camera measured the start direction (the server's camera sources all start with "camera"). */
export function isCameraDirection(shot: Shot): boolean {
  return (shot.launch_angle_horizontal_source ?? '').startsWith('camera');
}

/**
 * The start direction tile asks for a target: the camera measured it and no target line
 * is set, so it is measured from the camera axis instead of the line to the target.
 */
export function needsTargetLine(shot: Shot, settings: Pick<CameraCaptureSettings, 'target_line'>): boolean {
  return isCameraDirection(shot) && settings.target_line === null;
}

/**
 * The kiosk tiles for `ids`. ≈ follows the Live tiles' own marks (estimated or
 * calculated values, an uncertain ball speed with its smash and carry; the apex is
 * always one), plus a typical-spin carry and total (their ≈ on every screen)
 * and an uncalibrated radar start direction, club path or attack angle (their "not
 * calibrated" caption).
 */
export function buildKioskTiles(
  shot: Shot,
  ids: readonly LiveSlotId[],
  unitSystem: UnitSystem,
  options: LiveMetricOptions = {}
): KioskTile[] {
  const metrics = buildLiveMetrics(
    shot,
    unitSystem,
    { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 },
    options
  );
  const estimated = (id: string) => metrics.find((metric) => metric.id === id)?.estimated === true;
  const speedUnit = getSpeedUnit(unitSystem);
  const distanceUnit = getDistanceUnit(unitSystem);
  const typicalSpin = isTypicalSpinCarry(shot);

  const tile = (id: LiveSlotId): Omit<KioskTile, 'id' | 'label'> => {
    switch (id) {
      case 'carry':
        return {
          value: formatDistance(getCarryYards(shot), unitSystem, 0),
          unit: distanceUnit,
          approx: estimated('carry') || typicalSpin,
        };
      case 'total': {
        const total = totalYards(shot);
        return {
          value: total === null ? NO_VALUE : formatDistance(total, unitSystem, 0),
          unit: distanceUnit,
          approx: total !== null && typicalSpin,
        };
      }
      // A model value, ≈ as on its "All values" tile (typical spin or calculated).
      case 'apex': {
        const apex = shot.apex_yards;
        const shown = typeof apex === 'number' && Number.isFinite(apex);
        return { value: shown ? formatDistance(apex, unitSystem, 0) : NO_VALUE, unit: distanceUnit, approx: shown };
      }
      case 'ball_speed':
        return { value: formatSpeed(shot.ball_speed_mph, unitSystem, 0), unit: speedUnit, approx: estimated('ball_speed') };
      case 'club_speed':
        return {
          value: shot.club_speed_mph === null ? NO_VALUE : formatSpeed(shot.club_speed_mph, unitSystem, 0),
          unit: speedUnit,
          approx: false,
        };
      case 'smash':
        return { value: formatKioskSmash(shot.smash_factor), approx: estimated('smash') };
      case 'launch_v':
        return { value: formatKioskAngle(shot.launch_angle_vertical), approx: estimated('launch_v') };
      case 'launch_h':
        return {
          value: formatKioskDirection(shot.launch_angle_horizontal),
          approx: estimated('launch_h') || startDirectionUncalibrated(shot, options.directionCalibrated === true),
        };
      case 'curve': {
        const cls = shownCurveClass(shot);
        return cls === null
          ? { value: NO_VALUE, approx: false }
          : { value: t(`curve.${cls}`), kind: 'class', approx: estimated('curve') };
      }
      case 'spin':
        return { value: formatKioskSpin(getDisplayedSpin(shot)?.rpm), unit: 'rpm', approx: estimated('spin') };
      // Every shown path and attack angle is uncalibrated: the camera's calculated one (≈ on
      // its Live tile) and the radar's "not calibrated" one, as the start direction.
      case 'club_path': {
        const path = shownClubPath(shot);
        return { value: formatKioskAngle(path, true), approx: path != null };
      }
      case 'club_aoa': {
        const attack = shownAttackAngle(shot);
        return { value: formatKioskAngle(attack, true), approx: attack != null };
      }
      // A calculated camera value without ground truth yet: always ≈, never millimetres.
      case 'impact':
        return { value: impactZoneWord(shot), kind: 'impact', face: impactFaceInput(shot), approx: true };
    }
  };

  return ids.map((id) => ({ id, label: t(`kioskLive.label.${id}`), ...tile(id) }));
}
