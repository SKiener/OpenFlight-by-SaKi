import type { CameraAutoExposureStatus } from '../stores/useCameraStore';

/** One camera report that can name a flat frame: the exposure-quality poll or the capture status. */
export interface FlatFrameReport {
  auto_exposure?: Pick<CameraAutoExposureStatus, 'status' | 'frame_flat'> | null;
  frame_flat?: boolean | null;
}

/**
 * Whether the backend reports a flat frame (the sensor delivers one grey level, no picture): the
 * auto-exposure status `no_image`, or `frame_flat`. Shown as "No picture from the sensor" in the
 * lighting_required style, not as "Waiting".
 *
 * The polled exposure quality decides once it has arrived. The capture status (`snapshot`) counts
 * only until then: it is sent on connect and after a settings change, so a sensor whose no_image
 * lock the backend's retry recovers would otherwise keep the message until the next reconnect.
 */
export function reportsFlatFrame(
  live: FlatFrameReport | null | undefined,
  snapshot: FlatFrameReport | null | undefined
): boolean {
  const report = live ?? snapshot;
  return (
    report?.auto_exposure?.status === 'no_image' ||
    Boolean(report?.auto_exposure?.frame_flat) ||
    Boolean(report?.frame_flat)
  );
}
