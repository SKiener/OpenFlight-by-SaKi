import type { PhoneTiltRequest, TiltCalibrationStatus } from '../types/tiltCalibration';
import { getServerOrigin } from '../utils/serverOrigin';

const PATH = '/api/calibration/iwr6843/phone-tilt';

export type CalibrateResult = { ok: true; status: TiltCalibrationStatus } | { ok: false; error: string };

function url(): string {
  return `${getServerOrigin()}${PATH}`;
}

async function errorCode(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: unknown };
    if (typeof body?.error === 'string' && body.error) return body.error;
  } catch {
    // Not JSON: fall through to the HTTP status.
  }
  return `http_${response.status}`;
}

/** POST a two-position measurement; resolves with the server's reason code when it refuses. */
export async function calibratePhoneTilt(
  request: PhoneTiltRequest,
  fetcher: typeof fetch = fetch
): Promise<CalibrateResult> {
  let response: Response;
  try {
    response = await fetcher(url(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      cache: 'no-store',
    });
  } catch {
    return { ok: false, error: 'network' };
  }
  if (!response.ok) return { ok: false, error: await errorCode(response) };
  try {
    return { ok: true, status: (await response.json()) as TiltCalibrationStatus };
  } catch {
    return { ok: false, error: 'invalid_response' };
  }
}

/** DELETE the phone calibration; throws when the server did not confirm (for the two-step confirm). */
export async function resetPhoneTilt(fetcher: typeof fetch = fetch): Promise<TiltCalibrationStatus> {
  const response = await fetcher(url(), { method: 'DELETE', cache: 'no-store' });
  if (!response.ok) throw new Error(`Resetting the tilt calibration failed (${await errorCode(response)})`);
  return (await response.json()) as TiltCalibrationStatus;
}
