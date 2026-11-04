import type { WeatherState } from '../types/weather';

/** A weather_state with a 5 min old Open-Meteo reading at a stored range. */
export function weatherState(overrides: Partial<WeatherState> = {}): WeatherState {
  return {
    location: { name: 'Golfpark', latitude: 48.1, longitude: 11.5, country: 'Deutschland', admin1: 'Bayern' },
    reading: {
      source: 'open-meteo',
      temperature_c: 12.2,
      pressure_hpa: 1008.4,
      humidity_pct: 70,
      humidity_assumed: false,
      wind_speed_ms: 3.2,
      wind_direction_deg: 250,
      air_density_kg_m3: 1.2318,
      age_s: 300,
    },
    density_kg_m3: 1.2318,
    density_source: 'open-meteo',
    standard_density_kg_m3: 1.184,
    fetch_due: false,
    fresh_max_s: 10800,
    fetch_interval_s: 1800,
    ...overrides,
  };
}
