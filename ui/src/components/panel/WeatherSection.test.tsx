import { afterEach, describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { setActiveLocale } from '../../i18n';
import { weatherState } from '../../test-utils/weatherState';
import { WeatherSectionView, type WeatherSectionViewProps } from './WeatherSection';

function render(overrides: Partial<WeatherSectionViewProps> = {}) {
  return renderToString(
    <WeatherSectionView
      state={weatherState()}
      receivedAt={0}
      now={0}
      fetchStatus="idle"
      connected
      kiosk={false}
      {...overrides}
    />
  ).replace(/<!-- -->/g, '');
}

describe('WeatherSection', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows source and age of fresh weather and the stored range', () => {
    setActiveLocale('de');
    const html = render();
    expect(html).toContain('Wetter: 12 °C · 1008 hPa · ρ 1,23 · vor 5 min · Open-Meteo');
    expect(html).toContain('Range: Golfpark, Bayern, Deutschland');
    expect(html).toContain('Standort ändern');
    expect(html).toContain('Wetter von Hand eingeben');
    // The location search is behind "change location" once a range is stored.
    expect(html).not.toContain('Range suchen');
  });

  it('opens the search on the phone while no location is stored', () => {
    const html = render({ state: weatherState({ location: null, reading: null }) });
    expect(html).toContain('Weather: standard 1.184');
    expect(html).toContain('No range location stored');
    expect(html).toContain('aria-label="Find the range"');
    expect(html).toContain('aria-label="Latitude"');
    expect(html).toContain('aria-label="Longitude"');
  });

  it('offers the manual entry after a failed fetch', () => {
    const html = render({ state: weatherState({ reading: null }), fetchStatus: 'failed' });
    expect(html).toContain('The phone could not load the weather (no internet?). Enter it by hand:');
    expect(html).toContain('Temperature (°C)');
    expect(html).toContain('Air pressure (hPa)');
    expect(html).toContain('Humidity (%, optional)');
    expect(html).toContain('not the sea-level value');
  });

  it('keeps the form closed after a failed fetch when a manual entry is fresh', () => {
    const state = weatherState();
    state.reading = { ...state.reading!, source: 'manual' };
    const html = render({ state, fetchStatus: 'failed' });
    expect(html).toContain('· manual');
    expect(html).not.toContain('Temperature (°C)');
  });

  it('says when it is fetching', () => {
    expect(render({ fetchStatus: 'fetching' })).toContain('Loading weather…');
  });

  it('shows only the weather on the kiosk', () => {
    const html = render({ kiosk: true, state: weatherState({ location: null, reading: null }) });
    expect(html).toContain('Weather: standard 1.184');
    expect(html).toContain('Range location and weather are set on the paired phone.');
    expect(html).not.toContain('<input');
    expect(html).not.toContain('Enter weather by hand');
  });

  it('shows the standard once the reading is older than 3 h', () => {
    const html = render({ now: 10_501_000 });
    expect(html).toContain('Weather: standard 1.184');
  });
});
