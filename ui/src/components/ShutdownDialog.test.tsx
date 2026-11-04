import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { ShutdownDialog } from './ShutdownDialog';

const noop = () => {};

describe('ShutdownDialog', () => {
  it('replaces the confirmation controls with persistent shutdown feedback', () => {
    const html = renderToString(<ShutdownDialog state="pending" onConfirm={noop} onCancel={noop} />);

    expect(html).toContain('Shutting down OpenFlight…');
    expect(html).toContain('Safely stopping radar services');
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain('aria-label="Shutting down OpenFlight"');
    expect(html).not.toContain('>Shut Down</button>');
    expect(html).not.toContain('>Cancel</button>');
  });

  it('keeps retry and cancel controls available after a request failure', () => {
    const html = renderToString(<ShutdownDialog state="error" onConfirm={noop} onCancel={noop} />);

    expect(html).toContain('Could not start shutdown');
    expect(html).toContain('aria-labelledby="shutdown-dialog-title"');
    expect(html).toContain('>Try Again</button>');
    expect(html).toContain('>Cancel</button>');
  });
});
