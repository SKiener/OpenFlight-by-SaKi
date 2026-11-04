/** Where the range is: searched once by name (Open-Meteo geocoding) or typed as lat/lon; stored on the Pi. */
export interface RangeLocation {
  name: string;
  latitude: number;
  longitude: number;
  country?: string;
  admin1?: string;
}

export type WeatherSource = 'open-meteo' | 'manual';
/** Where an Open-Meteo reading's pressure came from: Open-Meteo, or the iOS companion's barometer. */
export type PressureSource = 'open-meteo' | 'phone-barometer';
/** How the phone reached Open-Meteo: the companion's cellular path, its default route, or the page's fetch. */
export type WeatherTransport = 'cellular' | 'default' | 'browser';
export type DensitySource = WeatherSource | 'standard';

/** The latest accepted reading, as the server's `weather_state` sends it. */
export interface WeatherReadingState {
  source: WeatherSource;
  temperature_c: number;
  pressure_hpa: number;
  humidity_pct: number;
  humidity_assumed: boolean;
  wind_speed_ms: number | null;
  wind_direction_deg: number | null;
  air_density_kg_m3: number;
  /** Observation age when the server sent this state. */
  age_s: number;
  /** Open-Meteo readings only (null for manual ones, absent from older servers). */
  pressure_source?: PressureSource | null;
  transport?: WeatherTransport | null;
}

/** The server's `weather_state` event (src/openflight/weather.py WeatherStore.snapshot). */
export interface WeatherState {
  location: RangeLocation | null;
  reading: WeatherReadingState | null;
  density_kg_m3: number;
  density_source: DensitySource;
  standard_density_kg_m3: number;
  fetch_due: boolean;
  fresh_max_s: number;
  fetch_interval_s: number;
}

/** The `weather_report` payload; the clock fields come from clientClockPayload(). */
export interface WeatherReport {
  source: WeatherSource;
  temperature_c: number;
  pressure_hpa: number;
  humidity_pct?: number | null;
  wind_speed_ms?: number;
  wind_direction_deg?: number;
  observed_epoch_s?: number;
  latitude?: number;
  longitude?: number;
  /** Sent only from inside the iOS companion; Safari's report is unchanged. */
  transport?: WeatherTransport;
  pressure_source?: PressureSource;
  /** With a barometer pressure: Open-Meteo's surface pressure of the same fetch, and the barometer sample's age. */
  open_meteo_pressure_hpa?: number;
  phone_pressure_age_s?: number;
  epoch_ms: number;
  tz_offset_min: number;
}

export type WeatherAck = { ok: true } | { ok: false; error: string };
