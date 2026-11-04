import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ProgressIndicator } from './ProgressIndicator';

describe('ProgressIndicator', () => {
  it('renders shot processing as an inline status that does not cover metrics', () => {
    const html = renderToString(
      <ProgressIndicator variant="inline" title="Shot captured" detail="Calculating metrics…" />
    );

    expect(html).toContain('progress-indicator--inline');
    expect(html).toContain('Shot captured');
    expect(html).toContain('Calculating metrics…');
    expect(html).not.toContain('shutdown-overlay');
  });

  it('renders an accessible live status', () => {
    const html = renderToString(<ProgressIndicator variant="dialog" title="Shutting down OpenFlight…" />);

    expect(html).toContain('role="status"');
    expect(html).toContain('aria-live="polite"');
  });
});
