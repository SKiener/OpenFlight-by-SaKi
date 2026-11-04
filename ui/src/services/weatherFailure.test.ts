import { describe, expect, it } from 'vitest';
import { nativeBridgeError } from '../test-utils/nativeBridge';
import { httpFailureReason, sendFailureReason, WeatherFailureError, weatherFailureReason } from './weatherFailure';

describe('weatherFailureReason', () => {
  it('keeps a reason known where the failure happened', () => {
    expect(weatherFailureReason(new WeatherFailureError('no_fix', 'No GPS fix'))).toBe('no_fix');
    expect(weatherFailureReason(new WeatherFailureError('http_429', 'Open-Meteo answered with an error'))).toBe(
      'http_429'
    );
  });

  it.each([
    ['timeout', 'cellular: cellular request timed out after 10 s; default: timed out', 'timeout'],
    ['network', 'cellular: no cellular path; default: The Internet connection appears to be offline.', 'offline'],
    ['http_error', 'Open-Meteo HTTP 503 via cellular', 'http_503'],
    ['http_error', 'Open-Meteo HTTP 404 via default', 'http_404'],
    ['http_error', 'Open-Meteo error: Parameter current must not be empty', 'invalid_response'],
    ['parse_error', 'current.temperature_2m missing or not a number', 'invalid_response'],
    ['permission_denied', 'location access is denied', 'no_permission'],
    ['unavailable', 'bridge is gone', 'unknown'],
    ['busy', 'busy', 'unknown'],
    ['internal', 'boom', 'unknown'],
    ['bad_request', 'latitude must be a number in -90…90', 'unknown'],
    ['toString', 'not a bridge code', 'unknown'],
  ] as const)('maps the native code %s (%s) to %s', (code, message, reason) => {
    expect(weatherFailureReason(nativeBridgeError(code, message))).toBe(reason);
  });

  it('counts an aborted or timed-out fetch as a timeout', () => {
    expect(weatherFailureReason(new DOMException('The operation was aborted.', 'AbortError'))).toBe('timeout');
    expect(weatherFailureReason(new DOMException('The operation timed out.', 'TimeoutError'))).toBe('timeout');
  });

  it('gives unknown for anything else, never the error text', () => {
    expect(weatherFailureReason(new TypeError('Load failed'))).toBe('unknown');
    expect(weatherFailureReason(new Error('https://api.open-meteo.com/v1/forecast?latitude=48.1'))).toBe('unknown');
    expect(weatherFailureReason('offline')).toBe('unknown');
    expect(weatherFailureReason(null)).toBe('unknown');
    expect(weatherFailureReason(undefined)).toBe('unknown');
    expect(weatherFailureReason({ name: 'OpenFlightNativeError', code: 7 })).toBe('unknown');
  });
});

describe('httpFailureReason', () => {
  it('takes a three-digit status only', () => {
    expect(httpFailureReason(503)).toBe('http_503');
    expect(httpFailureReason(100)).toBe('http_100');
    expect([0, 99, 1000, 503.5, Number.NaN, '503', undefined].map(httpFailureReason)).toEqual(Array(7).fill('unknown'));
  });
});

describe('sendFailureReason', () => {
  it("reads the report's ack", () => {
    expect(sendFailureReason('disconnected')).toBe('offline');
    expect(sendFailureReason('timeout')).toBe('timeout');
    expect(sendFailureReason('failed')).toBe('unknown');
    expect(sendFailureReason('stale')).toBe('rejected');
    expect(sendFailureReason('location_mismatch')).toBe('rejected');
  });
});
