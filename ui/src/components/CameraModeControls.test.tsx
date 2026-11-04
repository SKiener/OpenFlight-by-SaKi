import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { setActiveLocale, t } from '../i18n';
import { CameraModeControls } from './CameraModeControls';
import { useCameraStore } from '../stores/useCameraStore';

const settings = {
  available: true,
  running: true,
  capture_mode: 'full-300',
  capture_modes: [
    { id: 'full-300', width: 640, height: 400, fps: 300 },
    { id: 'strip-500', width: 640, height: 200, fps: 500 },
    { id: 'strip-560', width: 640, height: 200, fps: 560 },
  ],
};

describe('camera mode selection', () => {
  afterEach(() => setActiveLocale('en'));
  it('shows only server-supported profiles and explains the test setting', () => {
    setActiveLocale('de');
    const html = renderToString(<CameraModeControls settings={settings} onUpdate={vi.fn()} />);
    expect(html).toContain('Auflösung und Bildrate');
    expect(html).toContain('500<!-- --> fps');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('bis zum App-Neustart');
    expect(html).not.toContain('1000');
  });
  it('labels both 640 × 200 strips by their frame rate', () => {
    const html = renderToString(<CameraModeControls settings={settings} onUpdate={vi.fn()} />);
    const plain = html.replace(/<!-- -->/g, '');
    const labels = [...plain.matchAll(/<strong>([^<]*)<\/strong><span>(\d+) fps<\/span>/g)].map(
      ([, size, fps]) => `${size} ${fps}`
    );
    expect(labels).toEqual(['640 × 400 300', '640 × 200 500', '640 × 200 560']);
  });
  it.each(['en', 'de'] as const)('describes only the offered modes in %s, no 320 × 200 crop', (locale) => {
    setActiveLocale(locale);
    const html = renderToString(<CameraModeControls settings={settings} onUpdate={vi.fn()} />);
    const note = html.match(/<p class="camera-settings__note">([^<]*)<\/p>/)?.[1] ?? '';
    expect(note).toContain('560');
    expect(note).not.toContain('320');
    expect(t('camera.fixedViewNote')).not.toContain('320');
  });
  it('disables changes while rearming', () => {
    const html = renderToString(<CameraModeControls settings={{ ...settings, updating: true }} onUpdate={vi.fn()} />);
    expect(html.match(/disabled=""/g)).toHaveLength(3);
    expect(html).toContain('Rearming camera');
  });
  it('does not invent modes for unavailable hardware', () => {
    expect(renderToString(<CameraModeControls settings={{ available: false }} onUpdate={vi.fn()} />)).not.toContain(
      '<button'
    );
  });
  it('unlocks the UI after both a server error and a settings response', () => {
    const store = useCameraStore.getState();
    store.setCaptureSettings(settings);
    store.beginUpdate();
    expect(useCameraStore.getState().captureSettings.updating).toBe(true);
    store.setCaptureSettingsError('Busy');
    expect(useCameraStore.getState().captureSettings.updating).toBe(false);
    store.beginUpdate();
    store.setCaptureSettings(settings);
    expect(useCameraStore.getState().captureSettings.updating).toBeFalsy();
  });
});
