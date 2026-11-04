import { describe, expect, it } from 'vitest';
import { reportsFlatFrame } from './flatFrame';

const READY = { auto_exposure: { status: 'ready' as const, frame_flat: false }, frame_flat: false };
const NO_IMAGE = { auto_exposure: { status: 'no_image' as const, frame_flat: true }, frame_flat: true };

describe('reportsFlatFrame', () => {
  it('takes the capture status until the first exposure-quality poll arrives', () => {
    expect(reportsFlatFrame(null, NO_IMAGE)).toBe(true);
    expect(reportsFlatFrame(undefined, { frame_flat: true })).toBe(true);
    expect(reportsFlatFrame(null, { auto_exposure: { status: 'no_image' } })).toBe(true);
    expect(reportsFlatFrame(null, READY)).toBe(false);
  });

  it('lets a fresh poll that sees a picture clear a flat capture-status snapshot', () => {
    // The capture settings are only re-sent on connect or after a settings change; the
    // backend's 30 s retry can recover a no_image lock without either.
    expect(reportsFlatFrame(READY, NO_IMAGE)).toBe(false);
    expect(reportsFlatFrame({ frame_flat: false }, { frame_flat: true })).toBe(false);
  });

  it('reports a flat frame the poll sees, by status or by either flag', () => {
    expect(reportsFlatFrame(NO_IMAGE, READY)).toBe(true);
    expect(reportsFlatFrame({ frame_flat: true, auto_exposure: { status: 'adjusting' } }, READY)).toBe(true);
    expect(reportsFlatFrame({ auto_exposure: { status: 'adjusting', frame_flat: true } }, READY)).toBe(true);
  });
});
