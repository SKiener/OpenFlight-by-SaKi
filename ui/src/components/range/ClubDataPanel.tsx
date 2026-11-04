import { useMemo, useRef } from 'react';
import type { Shot } from '../../types/shot';
import { filterShotsByProfile } from '../../types/shot';
import { useUnitPreference } from '../../state/useUnitPreference';
import { useI18n } from '../../i18n/useI18n';
import { useDragScroll } from '../../hooks/useDragScroll';
import { useSharedFitFontSize } from '../../hooks/useFitFontSize';
import { MetricCard } from '../ui/MetricCard';
import { buildClubMetrics } from '../panel/clubMetrics';
import { impactFaceInput } from '../panel/impactPoint';
import { ImpactFace } from '../ui/ImpactFace';

interface ClubDataPanelProps {
  shots: Shot[];
  profileId: string;
}

/**
 * "All metrics" → Club: club speed, path, attack angle, the impact point (the face
 * over its zone word) and the low point in the ball page's hairline grid
 * (live-panel__grid, so its kiosk/phone caption rules apply; 3 + 2 on the kiosk,
 * five in a row on a landscape phone, two by three on a portrait phone), static
 * order, no tile promotion. Values arrive after the shot (shot_update) and fill in
 * place.
 */
export function ClubDataPanel({ shots, profileId }: ClubDataPanelProps) {
  const { locale } = useI18n();
  const { unitSystem } = useUnitPreference();
  const profileShots = useMemo(() => filterShotsByProfile(shots, profileId), [shots, profileId]);
  const shot = profileShots.at(-1) ?? null;
  const metrics = useMemo(
    () => (shot ? buildClubMetrics(shot, unitSystem) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- locale re-translates the labels
    [shot, unitSystem, locale]
  );
  const gridRef = useSharedFitFontSize(
    metrics.length > 0,
    metrics.map((metric) => `${metric.value}:${metric.unit ?? ''}`).join('|')
  );
  // A portrait phone's cards may need more height than the panel has: the body scrolls by dragging (phone.css).
  const bodyRef = useRef<HTMLDivElement>(null);
  const dragScroll = useDragScroll(bodyRef);
  if (!shot) return null;
  return (
    <div className="panel club-data">
      <div
        ref={bodyRef}
        className="panel__body live-panel__body"
        onPointerDown={dragScroll.onPointerDown}
        onPointerMove={dragScroll.onPointerMove}
        onPointerUp={dragScroll.onPointerUp}
        onPointerCancel={dragScroll.onPointerCancel}
        onClickCapture={dragScroll.onClickCapture}
      >
        <div ref={gridRef} className={`live-panel__grid live-panel__grid--of-${metrics.length} club-data__grid`}>
          {metrics.map((metric) => (
            <MetricCard
              key={metric.id}
              metricId={metric.id}
              {...metric}
              labelPosition="above"
              graphic={metric.id === 'cd_impact' ? <ImpactFace {...impactFaceInput(shot)} /> : undefined}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
