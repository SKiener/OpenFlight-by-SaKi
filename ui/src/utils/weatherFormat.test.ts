import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { weatherState } from '../test-utils/weatherState';
import type { WeatherFetchStatus } from '../stores/useWeatherStore';
import {
  formatWeatherAge,
  freshReading,
  manualFormAutoOpen,
  parseDecimal,
  readingAgeS,
  weatherSourceLabel,
  weatherStatusLine,
} from './weatherFormat';

describe('weather status line', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the fresh reading in German with comma decimals', () => {
    setActiveLocale('de');
    expect(weatherStatusLine(weatherState(), 0, 0)).toBe('Wetter: 12 °C · 1008 hPa · ρ 1,23 · vor 5 min · Open-Meteo');
  });

  it('names the iOS companion sources: cellular fetch and barometer pressure', () => {
    setActiveLocale('de');
    const state = weatherState();
    state.reading = { ...state.reading!, transport: 'cellular', pressure_source: 'phone-barometer' };
    expect(weatherStatusLine(state, 0, 0)).toBe(
      'Wetter: 12 °C · 1008 hPa · ρ 1,23 · vor 5 min · Open-Meteo (Mobilfunk) · Barometer iPhone'
    );
    expect(weatherSourceLabel({ ...state.reading, pressure_source: 'open-meteo' })).toBe('Open-Meteo (Mobilfunk)');
    expect(weatherSourceLabel({ ...state.reading, transport: 'default' })).toBe('Open-Meteo · Barometer iPhone');
    expect(weatherSourceLabel({ ...state.reading, transport: 'browser', pressure_source: null })).toBe('Open-Meteo');
    setActiveLocale('en');
    expect(weatherSourceLabel(state.reading)).toBe('Open-Meteo (cellular) · iPhone barometer');
    setActiveLocale('es');
    expect(weatherSourceLabel(state.reading)).toBe('Open-Meteo (datos móviles) · barómetro del iPhone');
  });

  it('names a manual entry', () => {
    setActiveLocale('de');
    const state = weatherState();
    state.reading = { ...state.reading!, source: 'manual', age_s: 0 };
    expect(weatherStatusLine(state, 0, 0)).toBe('Wetter: 12 °C · 1008 hPa · ρ 1,23 · gerade eben · manuell');
  });

  it('shows the standard density without a reading', () => {
    setActiveLocale('de');
    expect(weatherStatusLine(weatherState({ reading: null }), 0, 0)).toBe('Wetter: Standard 1,184');
    expect(weatherStatusLine(null, 0, 0)).toBe('Wetter: Standard 1,184');
    setActiveLocale('en');
    expect(weatherStatusLine(null, 0, 0)).toBe('Weather: standard 1.184');
    setActiveLocale('es');
    expect(weatherStatusLine(null, 0, 0)).toBe('Tiempo: estándar 1,184');
  });

  it('falls back to the standard once the reading ages past the server limit', () => {
    const state = weatherState();
    // 300 s old on arrival, then 10500 s more = 3 h exactly: still fresh; one second later it is not.
    expect(freshReading(state, 1000, 1000 + 10_500_000)).not.toBeNull();
    expect(freshReading(state, 1000, 1000 + 10_501_000)).toBeNull();
    expect(weatherStatusLine(state, 1000, 1000 + 10_501_000)).toBe('Weather: standard 1.184');
  });

  it('ages on from the arrival', () => {
    expect(readingAgeS(weatherState().reading!, 1000, 61_000)).toBe(360);
    expect(readingAgeS(weatherState().reading!, 1000, 0)).toBe(300);
  });

  it('formats ages', () => {
    expect(formatWeatherAge(30)).toBe('just now');
    expect(formatWeatherAge(59 * 60)).toBe('59 min ago');
    expect(formatWeatherAge(125 * 60)).toBe('2 h 5 min ago');
    setActiveLocale('es');
    expect(formatWeatherAge(300)).toBe('hace 5 min');
  });
});

describe('parseDecimal', () => {
  it.each([
    ['12', 12],
    ['12,5', 12.5],
    [' 1008.4 ', 1008.4],
    ['-3', -3],
    ['−3,5', -3.5],
    ['+7', 7],
  ])('reads %s', (text, value) => {
    expect(parseDecimal(text)).toBe(value);
  });

  it.each(['', 'warm', '12,5,1', '1e3', '12.', '.5'])('refuses %s', (text) => {
    expect(parseDecimal(text)).toBeNull();
  });
});

describe('manualFormAutoOpen', () => {
  it('opens on a failed fetch without fresh weather and stays open through a retry', () => {
    let open = false;
    const steps: Array<[WeatherFetchStatus, boolean]> = [];
    for (const status of ['idle', 'fetching', 'failed', 'fetching', 'failed', 'fetching'] as const) {
      open = manualFormAutoOpen(open, status, false);
      steps.push([status, open]);
    }
    expect(steps).toEqual([
      ['idle', false],
      ['fetching', false],
      ['failed', true],
      ['fetching', true],
      ['failed', true],
      ['fetching', true],
    ]);
  });

  it('closes once weather is fresh, typed or fetched', () => {
    expect(manualFormAutoOpen(true, 'failed', true)).toBe(false);
    expect(manualFormAutoOpen(true, 'idle', true)).toBe(false);
  });

  it('does not open on a failure while weather is fresh', () => {
    expect(manualFormAutoOpen(false, 'failed', true)).toBe(false);
  });
});
