import { useState, type ComponentProps, type ReactNode } from 'react';
import { LivePanel } from '../panel/LivePanel';
import { withLabelBreaks } from '../panel/labelBreaks';
import { buildLiveMetrics, carrySubtext, hasCalculatedMetric, NO_VALUE, type LiveMetric } from '../panel/liveMetrics';
import { MetricCard } from '../ui/MetricCard';
import { CarryNotes } from '../ui/CarryNotes';
import { filterShotsByProfile, isSwingSpeedShot, latestDeletableShot } from '../../types/shot';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useDirectionCalibrated } from '../../stores/useSystemStore';
import { useI18n } from '../../i18n/useI18n';
import { getHtmlLang } from '../../i18n';
import { useSharedFitFontSize } from '../../hooks/useFitFontSize';
import { socketService } from '../../services/socketService';
import { ConfirmDeleteButton } from '../ConfirmDeleteButton';
import { SegmentedControl } from '../ui/SegmentedControl';
import { ClubDataPanel } from './ClubDataPanel';
import { approxLegend, carryNotes, isTypicalSpinCarry } from '../../utils/carryDisplay';
import { isKioskPage } from '../../utils/kioskHost';
import type { PanelView } from '../panel/views';
import { KioskLiveOverview } from './KioskLiveOverview';

export type RangeDetailsPage = 'ball' | 'club';

/**
 * The "All metrics" ≈ legend, on the club page and on a ball page with a
 * calculated tile (short tiles may drop their captions); null otherwise. Next
 * to a typical-spin carry (ball page only) it names both meanings of ≈.
 */
function rangeDetailsLegend(page: RangeDetailsPage, calculated: boolean, typicalSpin: boolean): string | null {
  if (page === 'club') return approxLegend({ calculated: true, typicalSpin: false });
  return calculated ? approxLegend({ calculated, typicalSpin }) : null;
}

/**
 * "All metrics" toolbar: back (arrow only on phones, its name kept for screen
 * readers), the Ball | Club page switch, then the ≈ legend (rangeDetailsLegend),
 * otherwise the profile.
 */
export function RangeDetailsToolbar({ page, onPage, onBack, profileName, headerAction, calculated = false, typicalSpin = false }: {
  page: RangeDetailsPage;
  onPage: (page: RangeDetailsPage) => void;
  onBack: () => void;
  profileName: string;
  headerAction?: ReactNode;
  /** The ball page shows a calculated value (camera path or attack angle). */
  calculated?: boolean;
  /** The ball page's carry was flown with the club-typical spin (its ≈ means that). */
  typicalSpin?: boolean;
}) {
  const { t } = useI18n();
  const legend = rangeDetailsLegend(page, calculated, typicalSpin);
  const both = page === 'ball' && calculated && typicalSpin;
  return <div className="range-toolbar range-details-toolbar">
    <button className="range-back" onClick={onBack}><span aria-hidden="true">←</span> <span className="range-back__text">{t('range.overview')}</span></button>
    <SegmentedControl ariaLabel={t('clubData.tabsAria')} value={page} onChange={onPage} options={[{ id: 'ball', label: t('clubData.tabBall') }, { id: 'club', label: t('clubData.tabClub') }]} />
    {legend ? <span className={`range-details-legend${both ? ' range-details-legend--both' : ''}`}><span aria-hidden="true">≈</span> {legend}</span> : <span>{profileName}</span>}
    {headerAction}
  </div>;
}

type RangeLivePanelProps = ComponentProps<typeof LivePanel> & {
  /** The Pi's own screen: the big-tile overview (KioskLiveOverview). Defaults to the page's host. */
  kiosk?: boolean;
  /** Switches the app's view (the kiosk's start direction tile opens Align). */
  onChangeView?: (view: PanelView) => void;
};

export function RangeLivePanel({ kiosk = isKioskPage(), onChangeView, ...props }: RangeLivePanelProps) {
  const { t } = useI18n();
  const { unitSystem } = useUnitPreference();
  const directionCalibrated = useDirectionCalibrated();
  const [details, setDetails] = useState(false);
  const [page, setPage] = useState<RangeDetailsPage>('ball');
  const shots = filterShotsByProfile(props.shots, props.profileId);
  const shot = shots.at(-1) ?? null;
  const deletable = latestDeletableShot(props.shots, props.profileId);
  const metrics = shot ? buildLiveMetrics(shot, unitSystem, { count: 0, last_speed_mph: 0, best_speed_mph: 0, avg_speed_mph: 0 }, { directionCalibrated }) : [];
  const secondary = ['launch_v', 'launch_h', 'spin'];
  const carryNote = shot ? carrySubtext(shot, unitSystem) : '';
  const token = [...metrics.map((m) => `${m.id}:${m.label}:${m.value}:${m.unit}`), carryNote].join('|');
  const heroRef = useSharedFitFontSize(Boolean(shot) && !details && !kiosk, token);
  const secondaryRef = useSharedFitFontSize(Boolean(shot) && !details && !kiosk, token);
  const deleteLast = () => (deletable ? socketService.deleteShot(deletable.timestamp) : undefined);
  if (isSwingSpeedShot(shot)) return <LivePanel {...props} />;
  if (details) {
    const calculated = hasCalculatedMetric(metrics);
    const typicalSpin = shot !== null && isTypicalSpinCarry(shot);
    const legend = rangeDetailsLegend(page, calculated, typicalSpin);
    // The ball page's carry notes as text (a touch screen never shows the tile's tooltip).
    const notes = page === 'ball' && shot !== null && carryNotes(shot).length > 0;
    // Their short form (320 px portrait, landscape phones) leads with the ≈ legend of the
    // ball page, typical spin alone included: it never names the typical spin itself. Under
    // 360 px that one line replaces the footnote; on landscape phones the toolbar already
    // shows a calculated legend, so the line drops it (phone.css, phoneLandscape.css).
    const ballLegend = approxLegend({ calculated, typicalSpin });
    const shortLegend = ballLegend ? { text: ballLegend, className: calculated ? 'carry-notes__legend--in-toolbar' : undefined } : null;
    const footnoteClasses = ['range-details-footnote', page === 'ball' && calculated && typicalSpin ? 'range-details-footnote--both' : '', notes ? 'range-details-footnote--in-notes' : ''].filter(Boolean).join(' ');
    return <div className="range-details">
      <RangeDetailsToolbar page={page} onPage={setPage} onBack={() => setDetails(false)} profileName={props.profileName} headerAction={props.headerAction} calculated={calculated} typicalSpin={typicalSpin} />
      {page === 'ball' ? <LivePanel {...props} /> : <ClubDataPanel shots={props.shots} profileId={props.profileId} />}
      {notes && shot ? <CarryNotes shot={shot} className="range-details-carry-notes" withShortForm shortLegend={shortLegend} /> : null}
      {/* Phones under 360 px have no room for the legend beside back, switch and club: it moves under the grid.
          So does its two-meaning form on every portrait phone (phone.css). */}
      {legend ? <p className={footnoteClasses}><span aria-hidden="true">≈</span> {legend}</p> : null}
    </div>;
  }

  if (kiosk) {
    return <KioskLiveOverview shot={shot} shotNumber={shots.length} deletable={deletable} onDeleteLast={deleteLast}
      onDetails={() => setDetails(true)} headerAction={props.headerAction} clubLabel={props.clubLabel}
      onAlign={onChangeView ? () => onChangeView('camera') : undefined} />;
  }

  const card = (id: string, hero = false) => {
    const metric = metrics.find((m) => m.id === id) as LiveMetric;
    const source = hero
      ? metric.value === NO_VALUE ? t('range.unavailable') : id === 'carry' ? carryNote : t('range.opsMeasured')
      : metric.subtext ?? (metric.value === NO_VALUE ? t('range.unavailable') : undefined);
    return <div key={id} className={`range-tile range-tile--${id}`} data-metric={id}>
      <MetricCard {...metric} subtext={source} labelPosition="above" />
    </div>;
  };
  return <section className="range-live" aria-label={t('nav.live')}>
    <div className="range-toolbar">
      <div className="range-shot-heading"><h1>{shot ? t('range.shot', { n: shots.length }) : t('nav.live')}</h1><span>{props.profileName}{shot ? ` · ${new Date(shot.timestamp).toLocaleTimeString(getHtmlLang(), { hour: '2-digit', minute: '2-digit' })}` : ''}</span></div>
      <div className="range-toolbar-actions"><ConfirmDeleteButton key={deletable?.timestamp ?? ''} className="range-delete-last" label={t('shots.deleteLast')} disabled={!deletable} onConfirm={deleteLast} />{shot ? <button className="range-details-button" onClick={() => setDetails(true)}>{withLabelBreaks(t('range.allMetrics'))}</button> : null}{props.headerAction}</div>
    </div>
    {shot ? <>
      <div className="range-hero-grid" ref={heroRef}>{card('carry', true)}{card('ball_speed', true)}</div>
      <div className={`range-secondary-grid range-secondary-grid--of-${secondary.length}`} ref={secondaryRef}>{secondary.map((id) => card(id))}</div>
    </> : <div className="range-empty"><h2>{t('range.empty')}</h2><p>{t('live.readyDetail')}</p><span>{props.clubLabel}</span></div>}
  </section>;
}
