import { renderToString } from 'react-dom/server';
import { describe, it, expect, vi } from 'vitest';
import { CameraExposureControls } from './CameraExposureControls';

describe('CameraExposureControls', () => {
  it('offers bounded controls and an explicit manual apply action', () => {
    const html = renderToString(
      <CameraExposureControls
        settings={{ available: true, running: true, exposure_us: 400, gain: 8, max_exposure_us: 999 }}
        onUpdate={vi.fn()}
      />
    );
    expect(html).toContain('max="999"');
    expect(html).toContain('value="400"');
    expect(html).toContain('Apply manual settings');
    expect(html).toContain('Measure automatically again');
  });
  it('warns against long exposures and disables controls when offline', () => {
    const html = renderToString(
      <CameraExposureControls settings={{ available: false, exposure_us: 1000 }} onUpdate={vi.fn()} />
    );
    expect(html).toContain('motion blur increases');
    expect(html).toContain('disabled=""');
  });
});
