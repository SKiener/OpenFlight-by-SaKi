import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { catalogs, setActiveLocale } from '../i18n';
import { makeShot } from '../test-utils/shot';
import { BallSpeedUncertainNote } from './BallSpeedUncertainNote';

const UNCERTAIN = makeShot({ ball_speed_status: 'uncertain', ball_speed_status_reason: 'club_like_track' });

describe('BallSpeedUncertainNote', () => {
  afterEach(() => setActiveLocale('en'));

  it('shows the note in the TI alert band while the latest ball speed is uncertain', () => {
    const html = renderToString(<BallSpeedUncertainNote shot={UNCERTAIN} />);

    expect(html).toBe(
      '<div class="iwr-alert ball-speed-note" role="status"><div><span>Ball speed uncertain: the radar barely saw the ball.</span></div></div>'
    );
  });

  it.each([
    ['de', 'Ballgeschwindigkeit unsicher: Das Radar hat den Ball kaum gesehen.'],
    ['es', 'Velocidad de bola incierta: el radar apenas vio la bola.'],
  ])('speaks %s', (locale, text) => {
    setActiveLocale(locale);

    expect(renderToString(<BallSpeedUncertainNote shot={UNCERTAIN} />)).toContain(`<span>${text}</span>`);
  });

  it('has the note in every language', () => {
    expect(
      Object.fromEntries(Object.entries(catalogs).map(([id, messages]) => [id, messages['live.ballSpeedUncertainDetail']]))
    ).toEqual({
      en: 'Ball speed uncertain: the radar barely saw the ball.',
      de: 'Ballgeschwindigkeit unsicher: Das Radar hat den Ball kaum gesehen.',
      es: 'Velocidad de bola incierta: el radar apenas vio la bola.',
    });
  });

  it.each([
    ['no shot', null],
    ['a measured shot', makeShot()],
    // The guard found the ball and replaced the number: log only, no hint.
    ['a replaced ball speed', makeShot({ ball_speed_status: null, ball_speed_status_reason: 'club_like_track' })],
  ])('stays hidden for %s', (_name, shot) => {
    expect(renderToString(<BallSpeedUncertainNote shot={shot} />)).toBe('');
  });
});
