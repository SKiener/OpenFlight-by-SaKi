import { readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * Guards for what the browser tests cannot see: Playwright's Chromium has no
 * Safari toolbar (100vh equals 100dvh there) and the test machine is online.
 */
function source(path: string): string {
  return readFileSync(fileURLToPath(new URL(path, import.meta.url)), 'utf8');
}

describe('phone styles', () => {
  it('sizes the app shell to the dynamic viewport, so the footer stays above the Safari toolbar', () => {
    expect(source('../components/panel/panel.css')).toMatch(
      /\.panel-app \{[^}]*height: 100dvh;[^}]*max-height: 100dvh;/
    );
    expect(source('../App.css')).toMatch(/\.app \{[^}]*height: 100dvh;/);
    expect(source('../index.css')).toMatch(/body \{[^}]*min-height: 100dvh;/);
  });

  it('uses the system font the Pi and a phone share instead of loading one from the internet', () => {
    const srcDir = fileURLToPath(new URL('..', import.meta.url));
    const stylesheets = readdirSync(srcDir, { recursive: true, encoding: 'utf8' }).filter((file) =>
      file.endsWith('.css')
    );
    expect(stylesheets.length).toBeGreaterThan(5);
    for (const file of stylesheets) {
      expect(source(`../${file}`), file).not.toMatch(/fonts\.googleapis\.com|@import\s+['"]?https?:|url\(['"]?https?:/);
    }
    expect(source('../index.css')).toMatch(/--font-body: 'Nexa', 'A-Space', Arial, 'Liberation Sans', sans-serif;/);
    expect(source('../index.css')).toMatch(/--font-display: 'A-Space', Arial, 'Liberation Sans', sans-serif;/);
    // No font file ships with this repository, and none comes from the internet.
    expect(source('../index.css')).not.toMatch(/@font-face/);
  });

  it('reaches only portrait phones, never the landscape kiosk', () => {
    const phone = source('../components/panel/phone.css');
    const layoutQueries = [...phone.matchAll(/@media ([^{]+)\{/g)].map((match) => match[1].trim());
    for (const query of layoutQueries.filter((query) => query.includes('max-width'))) {
      expect(query).toMatch(/^\(orientation: portrait\) and \(max-width: \d+px\)$/);
    }
  });

  it('reaches only landscape phones, never a kiosk (800 x 400 and larger, at most 2 : 1)', () => {
    const landscape = source('../components/panel/phoneLandscape.css');
    const queries = [...landscape.matchAll(/@media ([^{]+)\{/g)].map((match) => match[1].trim());
    expect(queries.length).toBeGreaterThan(0);
    for (const query of queries.flatMap((list) => list.split(',').map((part) => part.trim()))) {
      // A big phone as a home-screen app (932 x 430): up to 440 high, but wider than any kiosk.
      const wide = query.match(
        /^\(orientation: landscape\) and \(max-height: (\d+)px\) and \(min-aspect-ratio: (\d+)\/(\d+)\)$/
      );
      if (wide) {
        const [, height, width, per] = wide.map(Number);
        expect(height, query).toBeLessThanOrEqual(440);
        expect(width / per, query).toBeGreaterThan(800 / 400);
        continue;
      }
      const match = query.match(/^\(orientation: landscape\) and \(max-(width|height): (\d+)px\)$/);
      expect(match, query).not.toBeNull();
      const [, side, limit] = match!;
      expect(Number(limit), query).toBeLessThanOrEqual(side === 'width' ? 760 : 399);
    }
  });

  it('lays the app out under the notch (viewport-fit=cover) and keeps every edge inside the safe area', () => {
    expect(source('../../index.html')).toMatch(/name="viewport" content="[^"]*viewport-fit=cover/);
    const range = source('../components/range/range.css');
    // The side margins clear a landscape notch; the top a status bar.
    expect(range).toMatch(
      /\.range-app \{ padding: env\(safe-area-inset-top, 0px\) max\(clamp\(12px, 2vw, 20px\), env\(safe-area-inset-right, 0px\)\)/
    );
    // The footer clears the home indicator in every orientation, not only in portrait.
    expect(range).toMatch(/\.range-footer \{[^}]*padding: 10px 0 max\(12px, env\(safe-area-inset-bottom, 0px\)\)/);
    expect(range).toMatch(/\.range-footer \{ padding: 7px 0 max\(9px, env\(safe-area-inset-bottom, 0px\)\)/);
    // Full-screen layers stay inside the safe area.
    for (const file of ['../components/SessionHistoryDialog.css', '../components/BagSettingsDialog.css']) {
      expect(source(file), file).toMatch(/inset: env\(safe-area-inset-top, 0px\) env\(safe-area-inset-right, 0px\)/);
    }
    expect(source('../components/CameraReplayDialog.css')).toMatch(/env\(safe-area-inset-bottom, 0px\)/);
    expect(source('../components/panel/panel.css')).toMatch(/\.picker-overlay \{[^}]*inset: env\(safe-area-inset-top/);
  });

  it('keeps type at the usable width under the notch and stops iOS enlarging it on rotation', () => {
    const index = source('../index.css');
    expect(index).toMatch(
      /\(100vw - env\(safe-area-inset-left, 0px\) - env\(safe-area-inset-right, 0px\)\) \* 0\.0234/
    );
    expect(index).toMatch(/-webkit-text-size-adjust: 100%;/);
  });

  it('keeps analysis fields at 16 px on touch screens, so iOS does not zoom in on focus', () => {
    expect(source('../analysis/analysis.css')).toMatch(
      /@media \(hover: none\) and \(pointer: coarse\) \{\s*\.analysis-page select, \.analysis-page input \{ font-size: max\(16px, \.84rem\); \}/
    );
  });

  it('lets the pairing code field shrink to a 320 px phone', () => {
    expect(source('../pairing/pairing.css')).toMatch(/\.pairing-screen__input \{[^}]*width: 100%;[^}]*min-width: 0;/);
  });
});
