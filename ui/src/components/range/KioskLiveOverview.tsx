import type { ReactNode } from 'react';
import type { Shot } from '../../types/shot';
import { useI18n } from '../../i18n/useI18n';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useDirectionCalibrated } from '../../stores/useSystemStore';
import { useCameraStore } from '../../stores/useCameraStore';
import { LARGE_LIVE_SLOTS, useLiveSlotsStore } from '../../stores/useLiveSlotsStore';
import { useSharedFitFontSize } from '../../hooks/useFitFontSize';
import { buildKioskTiles, needsTargetLine, type KioskTile } from '../panel/kioskLiveMetrics';
import { ConfirmDeleteButton } from '../ConfirmDeleteButton';
import { ImpactFace } from '../ui/ImpactFace';
import { RangePowerButton } from './RangeChrome';

/**
 * One kiosk tile: the title (short label, ≈, unit, and the "No target" badge) and the
 * value as the whole body. With `onAlign` the tile is a button that opens Align.
 */
export function KioskTileView({ tile, large, noTarget = false, onAlign }: {
  tile: KioskTile;
  large: boolean;
  /** The start direction was measured from the camera axis: no target line is set. */
  noTarget?: boolean;
  onAlign?: () => void;
}) {
  const { t } = useI18n();
  const classes = ['kiosk-tile', large ? 'kiosk-tile--large' : 'kiosk-tile--small', `kiosk-tile--${tile.id}`, tile.kind === 'class' ? 'kiosk-tile--class' : ''].filter(Boolean).join(' ');
  const body = <>
    <span className="kiosk-tile__title">
      {/* The spaces keep the words apart for screen readers and text copies; the flex gap spaces them on screen. */}
      <span className="kiosk-tile__label">{tile.label}</span>
      {tile.approx ? <>{' '}<span className="kiosk-tile__approx" title={t('kioskLive.approx')}>≈</span></> : null}
      {tile.unit ? <>{' '}<span className="kiosk-tile__unit">{tile.unit}</span></> : null}
      {noTarget ? <>{' '}<span className="kiosk-tile__badge">{t('kioskLive.noTarget')}</span></> : null}
    </span>
    <span className="kiosk-tile__body">
      {/* The impact tile: the face as large as the tile allows, its zone word under it. */}
      {tile.kind === 'impact' && tile.face ? <span className="kiosk-tile__face"><ImpactFace {...tile.face} /></span> : null}
      <span className="kiosk-tile__value-row"><span className="kiosk-tile__value">{tile.value}</span></span>
    </span>
  </>;
  if (noTarget && onAlign) {
    return <button type="button" className={`${classes} kiosk-tile--action`} data-metric={tile.id} title={t('kioskLive.noTargetHint')} onClick={onAlign}>{body}</button>;
  }
  return <div className={classes} data-metric={tile.id}>{body}</div>;
}

interface KioskLiveOverviewProps {
  shot: Shot | null;
  /** The shot's number in this profile's session. */
  shotNumber: number;
  deletable: Shot | null;
  onDeleteLast: () => Promise<void> | void;
  onDetails: () => void;
  /** The club picker. */
  headerAction?: ReactNode;
  clubLabel: string;
  /** Opens the Align view (a start direction without a target line). */
  onAlign?: () => void;
}

/**
 * The Pi's own Live screen, read from 1.5–2.5 m: one header row (shot number, delete,
 * all values, club, power) and five configurable tiles, two large over three small
 * (useLiveSlotsStore). Each value fills its tile; it shrinks only as far as its width demands.
 */
export function KioskLiveOverview({ shot, shotNumber, deletable, onDeleteLast, onDetails, headerAction, clubLabel, onAlign }: KioskLiveOverviewProps) {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const directionCalibrated = useDirectionCalibrated();
  const slots = useLiveSlotsStore((s) => s.slots);
  const targetLine = useCameraStore((s) => s.captureSettings.target_line);
  const tiles = shot ? buildKioskTiles(shot, slots, unitSystem, { directionCalibrated }) : [];
  const token = tiles.map((tile) => `${tile.id}:${tile.label}:${tile.value}:${tile.unit ?? ''}:${tile.approx}`).join('|');
  const gridRef = useSharedFitFontSize(shot !== null, token, 'each');
  const noTarget = shot !== null && needsTargetLine(shot, { target_line: targetLine });

  return <section className="range-live kiosk-live" aria-label={t('nav.live')}>
    <div className="kiosk-live__header">
      <h1>{shot ? t('range.shot', { n: shotNumber }) : t('nav.live')}</h1>
      <div className="kiosk-live__actions">
        <ConfirmDeleteButton key={deletable?.timestamp ?? ''} className="range-delete-last" label={t('kioskLive.delete')} ariaLabel={t('shots.deleteLast')} disabled={!deletable} onConfirm={onDeleteLast} />
        {shot ? <button className="range-details-button" onClick={onDetails}>{t('kioskLive.allValues')}</button> : null}
        {headerAction}
        <RangePowerButton />
      </div>
    </div>
    {shot ? <div className="kiosk-live__grid" ref={gridRef}>
      {tiles.map((tile, index) => <KioskTileView key={`${index}:${tile.id}`} tile={tile} large={index < LARGE_LIVE_SLOTS} noTarget={tile.id === 'launch_h' && noTarget} onAlign={onAlign} />)}
    </div> : <div className="range-empty"><h2>{t('range.empty')}</h2><p>{t('live.readyDetail')}</p><span>{clubLabel}</span></div>}
  </section>;
}
