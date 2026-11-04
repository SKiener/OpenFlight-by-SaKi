import { getServerOrigin } from '../utils/serverOrigin';

export async function deleteSavedShot(sessionId: string, shotTimestamp: string): Promise<void> {
  const url = `${getServerOrigin()}/api/session-history/${encodeURIComponent(sessionId)}/shots/${encodeURIComponent(shotTimestamp)}`;
  const response = await fetch(url, { method: 'DELETE', cache: 'no-store' });
  if (response.status !== 204) throw new Error(`Deleting the shot failed (${response.status})`);
}
