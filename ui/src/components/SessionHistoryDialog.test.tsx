import type { ComponentProps } from 'react';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { makeSavedShot } from '../test-utils/savedShot';
import { SessionHistoryView, type SavedSession } from './SessionHistoryDialog';

const noop = () => {};

const SESSION: SavedSession = {
  id: 'session_20261007_181200_range',
  started_at: '2026-10-07T18:12:00+02:00',
  shot_count: 2,
  clubs: ['7-iron'],
  shots: [makeSavedShot({ shot_number: 1 }), makeSavedShot({ shot_number: 2 })],
};
const LIST = { sessions: [{ id: SESSION.id, started_at: SESSION.started_at, shot_count: 2, clubs: SESSION.clubs }] };

function render(props: Partial<ComponentProps<typeof SessionHistoryView>> = {}): string {
  return renderToString(
    <SessionHistoryView
      sessionId={SESSION.id}
      current={{ data: SESSION }}
      onOpenSession={noop}
      onBack={noop}
      onRefresh={noop}
      onClose={noop}
      onShowOnRange={noop}
      {...props}
    />
  );
}

const showOnRange = /<button[^>]*>Show on range<\/button>/;

afterEach(() => setActiveLocale('en'));

describe('SessionHistoryDialog', () => {
  it('offers the range only for an open session with shots', () => {
    const session = render();
    expect(session).toMatch(showOnRange);
    expect(session).toContain('<tbody>');

    const list = render({ sessionId: null, current: { data: LIST } });
    expect(list).toContain('session-history__session');
    expect(list).not.toContain('Show on range');

    const noShots = render({ current: { data: { ...SESSION, shot_count: 0, shots: [] } } });
    expect(noShots).toContain('<tbody></tbody>');
    expect(noShots).not.toContain('Show on range');

    expect(render({ current: undefined })).not.toContain('Show on range');
    expect(render({ current: { error: true } })).not.toContain('Show on range');
  });

  it('does not offer the range unless the app passes onShowOnRange, as off the kiosk', () => {
    const html = render({ onShowOnRange: undefined });

    expect(html).toContain('<tbody>');
    expect(html).toContain('>Back</button>');
    expect(html).not.toContain('Show on range');
  });

  it('speaks German', () => {
    setActiveLocale('de');

    expect(render()).toMatch(/<button[^>]*>Auf der Range zeigen<\/button>/);
  });

  it('offers a delete per row and says that deleting removes a shot everywhere', () => {
    const html = render({ onDeleteShot: async () => {} });

    expect(html.match(/class="confirm-delete session-history__delete"/g)).toHaveLength(2);
    expect(html).toContain('aria-label="Delete shot 2"');
    expect(render()).not.toContain('session-history__delete');
    expect(html).toContain('Deleting removes a shot everywhere, including the stats and club averages.');
    setActiveLocale('de');
    expect(render()).toContain('Löschen entfernt einen Schlag überall, auch aus Statistik und Schläger-Durchschnitt.');
  });
});
