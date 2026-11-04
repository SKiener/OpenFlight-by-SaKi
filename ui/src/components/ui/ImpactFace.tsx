import { useId } from 'react';
import type { ImpactFaceInput } from '../panel/impactPoint';
import { IMPACT_DOT_RADIUS_MM, impactDotPosition, impactFaceKind, impactFaceShape, roundMm } from './impactFaceGeometry';
import './ImpactFace.css';

export interface ImpactFaceProps extends ImpactFaceInput {
  className?: string;
}

/**
 * Where the ball met the face (impactFaceGeometry): the schematic head with its centre
 * band and the impact dot, or a ring when the zone is open; no dot without an offset.
 * Decorative: the zone word under it says it, and no number is drawn.
 */
export function ImpactFace({ club, offsetMm, heightMm, zone, className }: ImpactFaceProps) {
  // useId's colons or guillemets are no use in url(#…); the gradients share the clip's unique stem.
  const clipId = `impact-face-${useId().replace(/[^\w-]/g, '')}`;
  const lineFadeId = `${clipId}-shaft`;
  const fillFadeId = `${clipId}-shaft-fill`;
  const shape = impactFaceShape(impactFaceKind(club));
  const dot = offsetMm === null ? null : impactDotPosition(shape, offsetMm, heightMm);
  const mark = dot === null ? 'none' : zone === null ? 'ring' : 'dot';
  const [, top, , span] = shape.viewBox.split(' ').map(Number);
  const half = shape.centreHalfWidth;
  const centre = dot ? { cx: roundMm(dot.x), cy: roundMm(-dot.y), r: IMPACT_DOT_RADIUS_MM } : null;
  const { shaft, hosel } = shape;
  // Along the shaft from the hosel top: full for the first quarter, then fading out.
  const fade = (id: string, stop: string) => (
    <linearGradient id={id} gradientUnits="userSpaceOnUse" x1={shaft.from.x} y1={shaft.from.y} x2={shaft.to.x} y2={shaft.to.y}>
      <stop offset="0" className={stop} />
      <stop offset="0.25" className={stop} />
      <stop offset="1" className={`${stop} ${stop}--end`} />
    </linearGradient>
  );
  return (
    <svg
      className={['impact-face', className].filter(Boolean).join(' ')}
      viewBox={shape.viewBox}
      preserveAspectRatio="xMidYMid meet"
      data-kind={shape.kind}
      data-mark={mark}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <clipPath id={clipId}>
          <path d={shape.face} />
        </clipPath>
        {fade(lineFadeId, 'impact-face__shaft-stop')}
        {fade(fillFadeId, 'impact-face__shaft-fill-stop')}
      </defs>
      <path className="impact-face__shaft-fill" fill={`url(#${fillFadeId})`} d={shaft.fill} />
      <path className="impact-face__shaft" stroke={`url(#${lineFadeId})`} d={shaft.lines} />
      {hosel ? <path className="impact-face__hosel-fill" d={hosel.fill} /> : null}
      {hosel ? <path className="impact-face__hosel-edge" d={hosel.edge} /> : null}
      <path className="impact-face__body" d={shape.body} />
      {shape.plate ? <path className="impact-face__plate" d={shape.plate} /> : null}
      <rect className="impact-face__band" clipPath={`url(#${clipId})`} x={-half} y={top} width={2 * half} height={span} />
      <path className="impact-face__grooves" clipPath={`url(#${clipId})`} d={shape.grooves} />
      <path className="impact-face__edge" d={shape.edge} />
      {centre && mark === 'dot' ? <circle className="impact-face__dot" {...centre} /> : null}
      {centre && mark === 'ring' ? (
        <g className="impact-face__ring">
          <circle className="impact-face__ring-edge" {...centre} />
          <circle className="impact-face__ring-line" {...centre} />
        </g>
      ) : null}
    </svg>
  );
}
