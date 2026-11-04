import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { catalogs, getHtmlLang, isLocaleId, LOCALES, setActiveLocale, t } from './index';
import { en } from './en';

describe('i18n catalogs', () => {
  afterEach(() => {
    setActiveLocale('en');
  });

  it('ships English, German, and Spanish', () => {
    expect(LOCALES.map((locale) => locale.id)).toEqual(['en', 'de', 'es']);
  });

  it.each(['fr', 'pt'])('falls back to English for the removed %s, also in <html lang>', (id) => {
    expect(isLocaleId(id)).toBe(false);
    expect(setActiveLocale(id)).toBe('en');
    expect(t('nav.live')).toBe('Live');
    expect(getHtmlLang()).toBe('en');
  });

  // The inline script in index.html sets <html lang> before the bundle loads and cannot import LOCALES.
  it('lists the same <html lang> tags in the pre-paint script of index.html', () => {
    const html = readFileSync(fileURLToPath(new URL('../../index.html', import.meta.url)), 'utf8');
    const map = /var langs = \{([^}]*)\}/.exec(html)?.[1] ?? '';
    expect(map, 'langs map in index.html').not.toBe('');

    const tags = [...map.matchAll(/(\w+):\s*'([^']+)'/g)].map(([, id, tag]) => [id, tag]);
    expect(Object.fromEntries(tags)).toEqual(Object.fromEntries(LOCALES.map((locale) => [locale.id, locale.htmlLang])));
  });

  it('keeps every locale in lockstep with English keys', () => {
    const englishKeys = Object.keys(en).sort();
    expect(englishKeys.length).toBeGreaterThan(80);

    for (const [id, messages] of Object.entries(catalogs)) {
      expect(Object.keys(messages).sort(), id).toEqual(englishKeys);
    }
  });

  it('interpolates placeholders in the active locale', () => {
    expect(t('profiles.shots', { count: '3' })).toBe('3 shots');

    setActiveLocale('es');
    expect(t('nav.live')).toBe('En vivo');
    expect(t('profiles.shots', { count: '3' })).toBe('3 golpes');
  });

  it('falls back to English when a locale id is unknown', () => {
    setActiveLocale('xx');
    expect(t('nav.live')).toBe('Live');
  });

  it('uses clear German launch labels and translated hardware messages', () => {
    setActiveLocale('de');
    expect(t('metric.vLaunch')).toBe('Abflugwinkel');
    expect(t('metric.hLaunch')).toBe('Startrichtung');
    expect(t('camera.preview')).toBe('Kameravorschau');
    expect(t('debug.secondsAgo', { count: 12 })).toContain('12');
  });

  it('names typical spin in every language', () => {
    expect(
      Object.fromEntries(Object.entries(catalogs).map(([id, messages]) => [id, messages['metric.typical']]))
    ).toEqual({ en: 'typical', de: 'typisch', es: 'típico' });
  });

  it('preserves the interpolation variables in every translation', () => {
    const placeholders = (value: string) => (value.match(/\{\w+\}/g) ?? []).sort();
    for (const [locale, messages] of Object.entries(catalogs)) {
      for (const key of Object.keys(en) as (keyof typeof en)[]) {
        expect(placeholders(messages[key]), `${locale}: ${key}`).toEqual(placeholders(en[key]));
      }
    }
  });

  it('has the club-data keys in every language', () => {
    const keys = ['clubData.pickClub', 'impact.uncalibrated', 'metric.calculated'] as const;
    for (const [locale, messages] of Object.entries(catalogs)) {
      for (const key of keys) expect(messages[key], `${locale}: ${key}`).toBeTruthy();
    }
    expect(catalogs.de['clubData.pickClub']).toBe('Schläger wählen');
    expect(catalogs.de['impact.uncalibrated']).toBe('unkalibriert');
    expect(catalogs.es['clubData.pickClub']).toBe('Elige un palo');
  });

  // The owner chose to show the apex and the camera's low point (2026-11-01).
  it('has the apex and the low point with its three sides in every language', () => {
    const expected = {
      en: ['Apex', 'Apex', 'Low point', 'ahead of the ball', 'behind the ball', 'at the ball'],
      de: ['Apex', 'Apex', 'Tiefpunkt', 'vor dem Ball', 'hinter dem Ball', 'am Ball'],
      es: ['Apex', 'Apex', 'Punto bajo', 'delante de la bola', 'detrás de la bola', 'en la bola'],
    } as const;
    const keys = [
      'metric.apex',
      'kioskLive.label.apex',
      'clubData.lowPoint',
      'clubData.lowPointAhead',
      'clubData.lowPointBehind',
      'clubData.lowPointAt',
    ] as const;
    for (const [locale, messages] of Object.entries(catalogs)) {
      expect(keys.map((key) => (messages as Record<string, string>)[key]), locale).toEqual(
        expected[locale as keyof typeof expected]
      );
    }
  });

  // The hardware cannot deliver these: no screen may show them, so no catalog carries them.
  it.each(Object.keys(catalogs))('has no copy for the values this hardware cannot measure (%s)', (locale) => {
    const messages = catalogs[locale as keyof typeof catalogs] as Record<string, string>;
    const gone = Object.keys(messages).filter((key) =>
      /^(metric\.(spinAxis|spinLoft|dynamicLoft|faceAngle|faceToPath|lowPoint|strike)|stats\.(avgSpinAxis|avgFace|avgFaceToPath)|clubData\.(summaryAxis|summaryFace|withinSigma)|faceClass\.|strike\.|shape\.|impact\.(progress|shots|mean|scattered))/.test(
        key
      )
    );
    expect(gone).toEqual([]);
  });

  it('has the curve label and the five curve class words in every language', () => {
    const expected = {
      en: ['Curve', 'Hook', 'Draw', 'Straight', 'Fade', 'Slice', 'not for woods'],
      de: ['Kurve', 'Hook', 'Draw', 'Gerade', 'Fade', 'Slice', 'nicht bei Hölzern'],
      es: ['Curva', 'Hook', 'Draw', 'Recta', 'Fade', 'Slice', 'no en maderas'],
    } as const;
    const keys = [
      'metric.curve',
      'curve.hook',
      'curve.draw',
      'curve.straight',
      'curve.fade',
      'curve.slice',
      'clubData.notForWoods',
    ] as const;
    for (const [locale, messages] of Object.entries(catalogs)) {
      expect(
        keys.map((key) => messages[key]),
        locale
      ).toEqual(expected[locale as keyof typeof expected]);
    }
  });

  it('keeps the club-page tile titles within 16 characters', () => {
    const titles = ['metric.clubSpeed', 'metric.clubPath', 'metric.clubAoa', 'stats.avgPath', 'stats.avgAoa'] as const;
    for (const [locale, messages] of Object.entries(catalogs)) {
      for (const key of titles)
        expect(messages[key].length, `${locale}: ${key} "${messages[key]}"`).toBeLessThanOrEqual(16);
    }
  });
});

describe('shot headings', () => {
  // "Schlag 124" never breaks between the word and its number; "Ansicht:" may still take a line of its own.
  it.each(['en', 'de', 'es'] as const)('joins the word and the number with a no-break space in %s', (locale) => {
    const noBreakSpace = String.fromCharCode(0xa0);
    for (const key of ['range.shot', 'companion.viewingShot'] as const) {
      const message = catalogs[locale][key];
      expect(message.endsWith(`${noBreakSpace}{n}`), `${locale}: ${key} "${message}"`).toBe(true);
      expect(message.at(-5), `${locale}: ${key}`).toMatch(/\S/);
      expect(message.slice(0, -4), `${locale}: ${key}`).not.toContain(noBreakSpace);
    }
  });
});
