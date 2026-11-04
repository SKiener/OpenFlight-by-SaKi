import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useI18n } from '../i18n/useI18n';
import { convertDistanceFromYards, formatNumber, getDistanceUnit, type UnitSystem } from '../utils/units';
import {
  equalAspectViewport,
  isHollow,
  niceStep,
  plotBounds,
  scaleEllipse,
  tickValues,
  type ClubSummary,
  type DispersionPoint,
} from './dispersion';

const MARGIN = { top: 28, right: 12, bottom: 44, left: 44 };
const FALLBACK_SIZE = { width: 358, height: 440 };
const GRID_SPACING_PX = 56;
const MIN_SPAN = 20;

interface DispersionPlotProps {
  points: readonly DispersionPoint[];
  summaries: readonly ClubSummary[];
  unitSystem: UnitSystem;
  colorOf: (club: string) => string;
  nameOf: (club: string) => string;
  describe: (point: DispersionPoint) => string;
  onSelect: (key: string) => void;
}

export function DispersionPlot({
  points,
  summaries,
  unitSystem,
  colorOf,
  nameOf,
  describe,
  onSelect,
}: DispersionPlotProps) {
  const { t } = useI18n();
  const frameRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState(FALLBACK_SIZE);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ width: Math.floor(width), height: Math.floor(height) });
    });
    observer.observe(frame);
    return () => observer.disconnect();
  }, []);

  const factor = convertDistanceFromYards(1, unitSystem);
  const marks = points.map((point) => ({ point, x: point.lateralYards * factor, y: point.carryYards * factor }));
  const ellipses = summaries.flatMap(({ club, ellipse }) =>
    ellipse ? [{ club, ellipse: scaleEllipse(ellipse, factor) }] : []
  );
  const means = summaries
    .filter((summary) => summary.carry.n >= 2)
    .map((summary) => ({ club: summary.club, x: summary.lateral.mean! * factor, y: summary.carry.mean! * factor }));
  const width = size.width - MARGIN.left - MARGIN.right;
  const height = size.height - MARGIN.top - MARGIN.bottom;
  const view = equalAspectViewport(
    plotBounds(
      marks,
      ellipses.map((entry) => entry.ellipse)
    ),
    width,
    height,
    MIN_SPAN
  );
  const px = (x: number) => MARGIN.left + (x - view.xMin) * view.pxPerUnit;
  const py = (y: number) => MARGIN.top + (view.yMax - y) * view.pxPerUnit;
  const step = niceStep(GRID_SPACING_PX / view.pxPerUnit);
  const unit = getDistanceUnit(unitSystem);
  const activate = (key: string) => (event: KeyboardEvent) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onSelect(key);
  };

  return (
    <div ref={frameRef} className="dispersion-plot">
      <svg width={size.width} height={size.height} role="group" aria-label={t('dispersion.chart')}>
        <g className="dispersion-plot__grid dispersion-plot__grid-x" aria-hidden="true">
          {tickValues(view.xMin, view.xMax, step).map((x) => (
            <g key={x}>
              <line x1={px(x)} x2={px(x)} y1={MARGIN.top} y2={MARGIN.top + height} />
              <text x={px(x)} y={MARGIN.top + height + 16} textAnchor="middle">
                {formatNumber(x, 0)}
              </text>
            </g>
          ))}
        </g>
        <g className="dispersion-plot__grid dispersion-plot__grid-y" aria-hidden="true">
          {tickValues(view.yMin, view.yMax, step).map((y) => (
            <g key={y}>
              <line x1={MARGIN.left} x2={MARGIN.left + width} y1={py(y)} y2={py(y)} />
              <text x={MARGIN.left - 6} y={py(y) + 4} textAnchor="end">
                {formatNumber(y, 0)}
              </text>
            </g>
          ))}
        </g>
        <g className="dispersion-plot__target" aria-hidden="true">
          <line x1={px(0)} x2={px(0)} y1={MARGIN.top} y2={MARGIN.top + height} />
          <text x={px(0)} y={MARGIN.top - 8} textAnchor="middle">
            {t('dispersion.target')}
          </text>
        </g>
        {ellipses.map(({ club, ellipse }) => (
          <ellipse
            key={club}
            className="dispersion-plot__ellipse"
            style={{ color: colorOf(club) }}
            cx={px(ellipse.cx)}
            cy={py(ellipse.cy)}
            rx={ellipse.semiMajor * view.pxPerUnit}
            ry={ellipse.semiMinor * view.pxPerUnit}
            transform={`rotate(${(-ellipse.angleRad * 180) / Math.PI} ${px(ellipse.cx)} ${py(ellipse.cy)})`}
          />
        ))}
        {marks.map(({ point, x, y }) => (
          <g
            key={point.key}
            className="dispersion-plot__point"
            style={{ color: colorOf(point.club) }}
            role="button"
            tabIndex={0}
            aria-label={describe(point)}
            data-hollow={isHollow(point)}
            onClick={() => onSelect(point.key)}
            onKeyDown={activate(point.key)}
          >
            <circle className="dispersion-plot__dot" cx={px(x)} cy={py(y)} r={5} />
            <circle className="dispersion-plot__hit" cx={px(x)} cy={py(y)} r={16} />
          </g>
        ))}
        {means.map(({ club, x, y }) => (
          <g key={club} className="dispersion-plot__mean" style={{ color: colorOf(club) }} aria-hidden="true">
            <path className="is-halo" d={`M${px(x) - 7} ${py(y)}h14M${px(x)} ${py(y) - 7}v14`} />
            <path d={`M${px(x) - 7} ${py(y)}h14M${px(x)} ${py(y) - 7}v14`} />
            <text x={px(x) + 10} y={py(y) - 10}>
              {nameOf(club)}
            </text>
          </g>
        ))}
        <text className="dispersion-plot__axis" x={MARGIN.left} y={14}>
          {t('dispersion.axisCarry', { unit })}
        </text>
        <text className="dispersion-plot__axis" x={MARGIN.left + width / 2} y={size.height - 6} textAnchor="middle">
          {t('dispersion.axisLateral', { unit })}
        </text>
      </svg>
    </div>
  );
}
