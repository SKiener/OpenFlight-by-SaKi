import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../i18n';
import { DemoBand } from './DemoBand';

const css = readFileSync(fileURLToPath(new URL('./DemoBand.css', import.meta.url)), 'utf8');

afterEach(() => {
  setActiveLocale('en');
});

describe('DemoBand', () => {
  it('shows the band with its way out while the demo runs', () => {
    const html = renderToString(<DemoBand active connected />);

    expect(html).toContain('DEMO – real shots are ignored');
    expect(html).toMatch(/<button type="button" class="demo-band__end">End<\/button>/);
    expect(renderToString(<DemoBand active={false} connected />)).toBe('');
  });

  it('holds End while offline, which socket.io would send on reconnect, maybe into a new demo', () => {
    expect(renderToString(<DemoBand active connected={false} />)).toMatch(/disabled="">End<\/button>/);
  });

  it('speaks German, and English in Spanish', () => {
    setActiveLocale('de');
    const german = renderToString(<DemoBand active connected />);
    setActiveLocale('es');
    const spanish = renderToString(<DemoBand active connected />);

    expect(german).toContain('DEMO – echte Schläge werden ignoriert');
    expect(german).toMatch(/>Beenden<\/button>/);
    expect(spanish).toContain('DEMO – real shots are ignored');
    expect(spanish).toMatch(/>End<\/button>/);
  });

  it('shows only the text on a view-only page', () => {
    const html = renderToString(<DemoBand active connected viewOnly />);

    expect(html).toContain('DEMO – real shots are ignored');
    expect(html).toContain('demo-band--view-only');
    expect(html).not.toContain('<button');
    expect(renderToString(<DemoBand active={false} connected viewOnly />)).toBe('');
  });
});

describe('DemoBand.css', () => {
  it('keeps the band semi-transparent and free of backdrop blur', () => {
    const alpha = /\.demo-band \{[^}]*background: rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/.exec(css)?.[1];

    expect(css).not.toContain('backdrop-filter');
    expect(Number(alpha), `background alpha ${alpha}`).toBeGreaterThan(0);
    expect(Number(alpha), `background alpha ${alpha}`).toBeLessThan(1);
  });

  it('sizes type in rem so it follows the kiosk screen', () => {
    expect(css).not.toMatch(/font-size:\s*[\d.]+px/);
    expect(css).toMatch(/font-size:\s*[\d.]+rem/);
  });

  it('greys out an End that waits for the connection, its word still readable at full opacity', () => {
    const disabled = /\.demo-band \.demo-band__end:disabled \{([^}]*)\}/.exec(css)?.[1] ?? '';

    expect(disabled, 'no amber fill').toMatch(/background: transparent;/);
    expect(disabled).toMatch(/color: var\(--color-text-muted\);/);
    expect(disabled, "over the kiosk's dimmed disabled buttons").toMatch(/opacity: 1;/);
  });
});
