import type { ImpactZone, Shot } from '../../types/shot';
import { t } from '../../i18n';
import { NO_VALUE } from './liveMetrics';

/**
 * The camera's impact point as the screens show it: a schematic face with a dot
 * (ImpactFace) and a zone word. No millimetres anywhere: the offset is calculated
 * and not yet checked against a Trackman, so only its side is named.
 */
export interface ImpactFaceInput {
  /** The shot's club id: woods get a wood face, every other club an iron blade. */
  club: string | null;
  /** Heel/toe offset, + toward the toe (right-handed); null draws no dot. */
  offsetMm: number | null;
  /** Ball centre above the lower edge (teed woods and the driver); null puts the dot at mid-height. */
  heightMm: number | null;
  /** Heel / center / toe; null with an offset draws a ring: the sigma leaves the zone open. */
  zone: ImpactZone | null;
}

function finite(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

const ZONES: readonly ImpactZone[] = ['heel', 'center', 'toe'];

/** What the impact graphic draws for this shot. */
export function impactFaceInput(shot: Shot): ImpactFaceInput {
  const zone = shot.impact_zone;
  return {
    club: typeof shot.club === 'string' ? shot.club : null,
    offsetMm: finite(shot.impact_offset_mm),
    heightMm: finite(shot.impact_height_mm),
    zone: zone != null && ZONES.includes(zone) ? zone : null,
  };
}

/** The camera gave an impact point (an offset). */
export function hasImpactPoint(shot: Shot): boolean {
  return finite(shot.impact_offset_mm) !== null;
}

/**
 * The zone word under the graphic: "Ferse", "Mitte", "Spitze"; "unsicher" when the
 * sigma leaves the zone open; {@link NO_VALUE} without an offset.
 */
export function impactZoneWord(shot: Shot): string {
  const input = impactFaceInput(shot);
  if (input.offsetMm === null) return NO_VALUE;
  return input.zone === null ? t('clubData.zone.uncertain') : t(`clubData.zone.${input.zone}`);
}
