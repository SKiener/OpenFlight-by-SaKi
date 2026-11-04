import { useEffect, useState, type FormEvent } from 'react';
import { useI18n } from '../../i18n/useI18n';
import { searchLocations } from '../../services/openMeteo';
import { socketService } from '../../services/socketService';
import { useSystemStore } from '../../stores/useSystemStore';
import { useWeatherStore, type WeatherFetchStatus } from '../../stores/useWeatherStore';
import type { RangeLocation, WeatherAck, WeatherState } from '../../types/weather';
import { clientClockPayload } from '../../utils/clockOffset';
import { isKioskPage } from '../../utils/kioskHost';
import {
  freshReading,
  locationLabel,
  manualFormAutoOpen,
  parseDecimal,
  weatherStatusLine,
} from '../../utils/weatherFormat';

/** The shown age advances while the menu stays open. */
const AGE_TICK_MS = 30_000;

type Message = 'searchEmpty' | 'searchFailed' | 'invalidNumber' | 'saveFailed' | 'rejected' | 'rejectedRange' | null;

function messageKey(message: NonNullable<Message>) {
  return `weather.${message}` as const;
}

function ackMessage(ack: WeatherAck): Message {
  if (ack.ok) return null;
  if (ack.error === 'out_of_range') return 'rejectedRange';
  return ack.error === 'disconnected' || ack.error === 'timeout' ? 'saveFailed' : 'rejected';
}

function useNow(): number {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(performance.now()), AGE_TICK_MS);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** Menu block: the weather the carry flies with, the range location and, on the phone, how to set both. */
export function WeatherSection() {
  const { state, receivedAt, fetchStatus } = useWeatherStore();
  const connected = useSystemStore((s) => s.connected);
  const now = useNow();
  return (
    <WeatherSectionView
      state={state}
      receivedAt={receivedAt}
      now={Math.max(now, receivedAt)}
      fetchStatus={fetchStatus}
      connected={connected}
      kiosk={isKioskPage()}
    />
  );
}

export interface WeatherSectionViewProps {
  state: WeatherState | null;
  /** performance.now() when `state` arrived, and now on the same clock. */
  receivedAt: number;
  now: number;
  fetchStatus: WeatherFetchStatus;
  connected: boolean;
  /** The kiosk shows the weather only: the Pi has no internet, and typing happens on the phone. */
  kiosk: boolean;
}

export function WeatherSectionView({ state, receivedAt, now, fetchStatus, connected, kiosk }: WeatherSectionViewProps) {
  const { t } = useI18n();
  const location = state?.location ?? null;
  return (
    <section className="menu-sheet__section" aria-label={t('weather.title')}>
      <span className="menu-sheet__section-title">{t('weather.title')}</span>
      <span className="menu-sheet__sublabel" data-testid="weather-status">
        {weatherStatusLine(state, receivedAt, now)}
      </span>
      <span className="menu-sheet__note">
        {location ? t('weather.location', { name: locationLabel(location) }) : t('weather.noLocation')}
      </span>
      {kiosk ? (
        <span className="menu-sheet__note">{t('weather.phoneOnly')}</span>
      ) : (
        <PhoneWeatherControls
          hasLocation={location !== null}
          hasFreshReading={freshReading(state, receivedAt, now) !== null}
          fetchStatus={fetchStatus}
          connected={connected}
        />
      )}
      <span className="menu-sheet__note">{t('weather.explain')}</span>
    </section>
  );
}

function PhoneWeatherControls({
  hasLocation,
  hasFreshReading,
  fetchStatus,
  connected,
}: {
  hasLocation: boolean;
  hasFreshReading: boolean;
  fetchStatus: WeatherFetchStatus;
  connected: boolean;
}) {
  const { t } = useI18n();
  const [editingLocation, setEditingLocation] = useState(false);
  const [manualOpen, setManualOpen] = useState(false);
  // Opened by a failed fetch: stays mounted through the 'fetching' of a retry (see manualFormAutoOpen).
  const [autoOpen, setAutoOpen] = useState(false);
  const nextAutoOpen = manualFormAutoOpen(autoOpen, fetchStatus, hasFreshReading);
  if (nextAutoOpen !== autoOpen) setAutoOpen(nextAutoOpen);
  const showLocationEditor = !hasLocation || editingLocation;
  const showManual = manualOpen || nextAutoOpen;

  return (
    <>
      {fetchStatus === 'fetching' ? <span className="menu-sheet__note">{t('weather.fetching')}</span> : null}
      {showLocationEditor ? (
        <LocationEditor connected={connected} onSaved={() => setEditingLocation(false)} />
      ) : (
        <button type="button" className="panel-action panel-action--secondary" onClick={() => setEditingLocation(true)}>
          {t('weather.changeLocation')}
        </button>
      )}
      {showManual ? (
        <ManualWeatherForm
          failed={fetchStatus === 'failed'}
          connected={connected}
          onSaved={() => setManualOpen(false)}
        />
      ) : (
        <button type="button" className="panel-action panel-action--secondary" onClick={() => setManualOpen(true)}>
          {t('weather.enterManually')}
        </button>
      )}
    </>
  );
}

function LocationEditor({ connected, onSaved }: { connected: boolean; onSaved: () => void }) {
  const { t, locale } = useI18n();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<RangeLocation[]>([]);
  const [searching, setSearching] = useState(false);
  const [latitude, setLatitude] = useState('');
  const [longitude, setLongitude] = useState('');
  const [message, setMessage] = useState<Message>(null);

  const save = async (location: RangeLocation) => {
    setMessage(null);
    if (await socketService.setWeatherLocation(location)) {
      setResults([]);
      onSaved();
    } else {
      setMessage('saveFailed');
    }
  };

  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!query.trim() || searching) return;
    setSearching(true);
    setMessage(null);
    try {
      const found = await searchLocations(query, locale);
      setResults(found);
      if (found.length === 0) setMessage('searchEmpty');
    } catch {
      setResults([]);
      setMessage('searchFailed');
    } finally {
      setSearching(false);
    }
  };

  const saveCoordinates = (event: FormEvent) => {
    event.preventDefault();
    const lat = parseDecimal(latitude);
    const lon = parseDecimal(longitude);
    if (lat === null || lon === null || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      setMessage('invalidNumber');
      return;
    }
    void save({ name: '', latitude: lat, longitude: lon });
  };

  return (
    <>
      <form className="menu-sheet__add-row" onSubmit={(event) => void search(event)}>
        <input
          className="menu-sheet__input"
          type="search"
          aria-label={t('weather.searchLabel')}
          placeholder={t('weather.searchPlaceholder')}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          enterKeyHint="search"
          autoComplete="off"
        />
        <button type="submit" className="menu-sheet__chip" disabled={searching || !query.trim()}>
          {searching ? t('weather.searching') : t('weather.search')}
        </button>
      </form>
      {results.length > 0 ? (
        <div className="menu-sheet__chips" role="group" aria-label={t('weather.searchLabel')}>
          {results.map((result) => (
            <button
              key={`${result.latitude},${result.longitude}`}
              type="button"
              className="menu-sheet__chip"
              disabled={!connected}
              onClick={() => void save(result)}
            >
              {locationLabel(result)}
            </button>
          ))}
        </div>
      ) : null}
      <span className="menu-sheet__sublabel">{t('weather.coordinates')}</span>
      <form className="menu-sheet__add-row" onSubmit={saveCoordinates}>
        <input
          className="menu-sheet__input"
          aria-label={t('weather.latitude')}
          placeholder={t('weather.latitude')}
          value={latitude}
          onChange={(event) => setLatitude(event.target.value)}
          autoComplete="off"
        />
        <input
          className="menu-sheet__input"
          aria-label={t('weather.longitude')}
          placeholder={t('weather.longitude')}
          value={longitude}
          onChange={(event) => setLongitude(event.target.value)}
          autoComplete="off"
        />
        <button type="submit" className="menu-sheet__chip" disabled={!connected}>
          {t('weather.saveCoordinates')}
        </button>
      </form>
      {message ? (
        <span className="menu-sheet__note" role="status">
          {t(messageKey(message))}
        </span>
      ) : null}
    </>
  );
}

function ManualWeatherForm({
  failed,
  connected,
  onSaved,
}: {
  failed: boolean;
  connected: boolean;
  onSaved: () => void;
}) {
  const { t } = useI18n();
  const [temperature, setTemperature] = useState('');
  const [pressure, setPressure] = useState('');
  const [humidity, setHumidity] = useState('');
  const [message, setMessage] = useState<Message>(null);
  const [saving, setSaving] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const temperatureC = parseDecimal(temperature);
    const pressureHpa = parseDecimal(pressure);
    const humidityPct = humidity.trim() ? parseDecimal(humidity) : null;
    if (temperatureC === null || pressureHpa === null || (humidity.trim() && humidityPct === null)) {
      setMessage('invalidNumber');
      return;
    }
    setSaving(true);
    setMessage(null);
    const ack = await socketService.sendWeatherReport({
      source: 'manual',
      temperature_c: temperatureC,
      pressure_hpa: pressureHpa,
      humidity_pct: humidityPct,
      ...clientClockPayload(),
    });
    setSaving(false);
    setMessage(ackMessage(ack));
    if (ack.ok) onSaved();
  };

  return (
    <form className="menu-sheet__section" onSubmit={(event) => void submit(event)}>
      {failed ? <span className="menu-sheet__note">{t('weather.fetchFailed')}</span> : null}
      <label className="menu-sheet__section">
        <span className="menu-sheet__sublabel">{t('weather.temperature')}</span>
        <input
          className="menu-sheet__input"
          value={temperature}
          onChange={(event) => setTemperature(event.target.value)}
          autoComplete="off"
        />
      </label>
      <label className="menu-sheet__section">
        <span className="menu-sheet__sublabel">{t('weather.pressure')}</span>
        <input
          className="menu-sheet__input"
          inputMode="decimal"
          value={pressure}
          onChange={(event) => setPressure(event.target.value)}
          autoComplete="off"
        />
      </label>
      <span className="menu-sheet__note">{t('weather.pressureHint')}</span>
      <label className="menu-sheet__section">
        <span className="menu-sheet__sublabel">{t('weather.humidity')}</span>
        <input
          className="menu-sheet__input"
          inputMode="decimal"
          value={humidity}
          onChange={(event) => setHumidity(event.target.value)}
          autoComplete="off"
        />
      </label>
      <button type="submit" className="panel-action panel-action--secondary" disabled={saving || !connected}>
        {t('weather.saveManual')}
      </button>
      {message ? (
        <span className="menu-sheet__note" role="status">
          {t(messageKey(message))}
        </span>
      ) : null}
    </form>
  );
}
