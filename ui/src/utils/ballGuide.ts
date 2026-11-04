import { getServerOrigin } from './serverOrigin';

/** The capture camera's still, refreshed by the Align view and the kiosk's setup reminder. */
export const CAMERA_PREVIEW_URL = `${getServerOrigin()}/api/camera/preview.jpg`;

/** Where the ball should lie in the camera picture, in percent of the frame: the Align view's crosshair. */
export const BALL_GUIDE_X_PCT = 50;
export const BALL_GUIDE_Y_PCT = 78;

/** The CSS variables `.camera-feed__center-cross` reads to sit on the ball guide. */
export const BALL_GUIDE_CROSS_VARS = {
  '--camera-crosshair-x': `${BALL_GUIDE_X_PCT}%`,
  '--camera-crosshair-y': `${BALL_GUIDE_Y_PCT}%`,
};
