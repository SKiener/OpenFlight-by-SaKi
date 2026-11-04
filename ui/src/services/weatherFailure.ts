/**
 * Why the phone's weather upkeep failed, for the `weather_fetch_failed` event (session log only).
 * The reason is classified here, from a closed set the server checks (src/openflight/weather.py):
 * no error text, URLs or coordinates ever leave the phone.
 */

/** `location`: the GPS range location; `fetch`: the Open-Meteo fetch; `send`: the report to the Pi. */
export type WeatherFailureStage = 'location' | 'fetch' | 'send';

export type WeatherFailureReason =
  | 'offline'
  | 'timeout'
  | `http_${number}`
  | 'invalid_response'
  | 'rejected'
  | 'no_permission'
  | 'no_fix'
  | 'save_failed'
  | 'unknown';

/** The `weather_fetch_failed` payload. */
export interface WeatherFailureReport {
  stage: WeatherFailureStage;
  reason: WeatherFailureReason;
  consecutive_failures: number;
  next_retry_s: number;
}

/** A failure whose reason is known where it happens (the page fetch, the GPS fix, the server's answer). */
export class WeatherFailureError extends Error {
  readonly reason: WeatherFailureReason;

  constructor(reason: WeatherFailureReason, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'WeatherFailureError';
    this.reason = reason;
  }
}

/** `http_<status>` for a three-digit HTTP status, else 'unknown'. */
export function httpFailureReason(status: unknown): WeatherFailureReason {
  return typeof status === 'number' && Number.isInteger(status) && status >= 100 && status <= 999
    ? `http_${status}`
    : 'unknown';
}

/**
 * The companion's bridge rejects with an `OpenFlightNativeError` carrying `.code`
 * (ios/OpenFlightCompanion/Web/openflight-native.js, design §7.2). `network` means both
 * transports failed (WeatherService.swift); `http_error` is mapped on its own below.
 */
const NATIVE_CODE_REASONS: Readonly<Record<string, WeatherFailureReason>> = {
  timeout: 'timeout',
  network: 'offline',
  parse_error: 'invalid_response',
  permission_denied: 'no_permission',
};

/** The closed-set reason of a fetch, GPS or send error; 'unknown' for anything not recognised. */
export function weatherFailureReason(error: unknown): WeatherFailureReason {
  if (error instanceof WeatherFailureError) return error.reason;
  if (typeof error !== 'object' || error === null) return 'unknown';
  const { name, code, message } = error as { name?: unknown; code?: unknown; message?: unknown };
  if (name === 'OpenFlightNativeError' && typeof code === 'string') {
    if (code === 'http_error') {
      // "Open-Meteo HTTP 503 via cellular": only the status is kept. Without one, Open-Meteo
      // answered 2xx with an error body.
      const status = typeof message === 'string' ? /\bHTTP (\d{3})\b/.exec(message)?.[1] : undefined;
      return status ? httpFailureReason(Number(status)) : 'invalid_response';
    }
    return Object.hasOwn(NATIVE_CODE_REASONS, code) ? NATIVE_CODE_REASONS[code] : 'unknown';
  }
  if (name === 'AbortError' || name === 'TimeoutError') return 'timeout';
  return 'unknown';
}

/** What the server's answer to a `weather_report` means (socketService.emitWeather's ack). */
export function sendFailureReason(ackError: string): WeatherFailureReason {
  if (ackError === 'disconnected') return 'offline';
  if (ackError === 'timeout') return 'timeout';
  // No well-formed answer at all (an older server without the handler).
  if (ackError === 'failed') return 'unknown';
  return 'rejected';
}
