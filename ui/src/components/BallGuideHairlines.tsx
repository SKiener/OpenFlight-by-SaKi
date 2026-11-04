import type { ReactNode } from 'react';
import { BALL_GUIDE_X_PCT, BALL_GUIDE_Y_PCT } from '../utils/ballGuide';

/**
 * The two hairlines through the ball guide, laid over the picture's box (CameraFeed.css). Further
 * strokes in the same 0-100 frame (the Align view's target line) go in as children.
 */
export function BallGuideHairlines({ children }: { children?: ReactNode }) {
  return (
    <svg className="camera-feed__crosshair" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <line
        x1={BALL_GUIDE_X_PCT}
        y1="0"
        x2={BALL_GUIDE_X_PCT}
        y2="100"
        vectorEffect="non-scaling-stroke"
        className="camera-feed__hairline"
      />
      <line
        x1="0"
        y1={BALL_GUIDE_Y_PCT}
        x2="100"
        y2={BALL_GUIDE_Y_PCT}
        vectorEffect="non-scaling-stroke"
        className="camera-feed__hairline"
      />
      {children}
    </svg>
  );
}
