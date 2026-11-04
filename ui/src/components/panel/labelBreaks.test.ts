import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale, t } from '../../i18n';
import { SOFT_HYPHEN, withLabelBreaks } from './labelBreaks';

afterEach(() => setActiveLocale('en'));

describe('withLabelBreaks', () => {
  it.each([
    ['Schlägertempo', 'Schläger', 'tempo'],
    ['Abflugwinkel', 'Abflug', 'winkel'],
    ['Startrichtung', 'Start', 'richtung'],
    ['Schlägerpfad', 'Schläger', 'pfad'],
    ['Eintreffwinkel', 'Eintreff', 'winkel'],
    ['Alle Messwerte', 'Alle Mess', 'werte'],
    ['schlagbereit', 'schlag', 'bereit'],
  ])('lets %s break between its parts', (label, head, tail) => {
    expect(withLabelBreaks(label)).toBe(`${head}${SOFT_HYPHEN}${tail}`);
  });

  it('covers every long German title of the Live grid', () => {
    setActiveLocale('de');
    for (const key of ['metric.clubSpeed', 'metric.vLaunch', 'metric.hLaunch', 'metric.clubPath', 'metric.clubAoa'] as const) {
      expect(withLabelBreaks(t(key)), key).toContain(SOFT_HYPHEN);
    }
  });

  it('lets the Align chip break inside its long word', () => {
    setActiveLocale('de');
    expect(withLabelBreaks(t('camera.bufferArmed'))).toBe(`Aufnahme bleibt schlag${SOFT_HYPHEN}bereit`);
  });

  it('leaves other labels and English alone', () => {
    expect(withLabelBreaks('Smash')).toBe('Smash');
    expect(withLabelBreaks('Start direction')).toBe('Start direction');
    expect(withLabelBreaks('')).toBe('');
  });

  it('reads the same once the soft hyphens are dropped', () => {
    expect(withLabelBreaks('Ø Schlägertempo').replaceAll(SOFT_HYPHEN, '')).toBe('Ø Schlägertempo');
  });
});
