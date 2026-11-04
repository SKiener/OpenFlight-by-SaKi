import { afterEach, describe, expect, it } from 'vitest';
import { setActiveLocale } from '../../i18n';
import { makeShot, UNMEASURABLE_CLUB_DATA, UNMEASURABLE_COPY } from '../../test-utils/shot';
import type { Shot } from '../../types/shot';
import { NO_VALUE, type LiveMetric } from './liveMetrics';
import { buildClubStatTiles, CLUB_STAT_IDS } from './clubStatTiles';

const byId = (tiles: LiveMetric[], id: string) => tiles.find((tile) => tile.id === id) as LiveMetric;

function shots(club: string, rows: Array<Partial<Shot>>): Shot[] {
  return rows.map((row, i) => makeShot({ club, timestamp: `${club}-${i}`, spin_axis_deg: null, ...row }));
}

afterEach(() => setActiveLocale('en'));

describe('buildClubStatTiles', () => {
  it('always returns the path and attack-angle tiles, in that order', () => {
    expect([...CLUB_STAT_IDS]).toEqual(['stats_cd_path', 'stats_cd_aoa']);
    expect(buildClubStatTiles([], '8-iron').map((tile) => tile.id)).toEqual([...CLUB_STAT_IDS]);
    expect(buildClubStatTiles([], 'driver').map((tile) => tile.id)).toEqual([...CLUB_STAT_IDS]);
  });

  it('de: means ± SEM from 3 shots, both captioned "n=… · berechnet · unkalibriert"', () => {
    setActiveLocale('de');
    const rows = [-1, 1, 3].map((v) => ({ camera_club_path_deg: v, camera_attack_angle_deg: v - 9 }));
    const tiles = buildClubStatTiles(shots('8-iron', rows), '8-iron');
    expect(byId(tiles, 'stats_cd_path')).toMatchObject({
      label: 'Ø Pfad',
      value: '+1,0 ± 1,2',
      unit: '°',
      subtext: 'n=3 · berechnet · unkalibriert',
      estimated: true,
      estimatedLabel: 'berechnet · unkalibriert',
    });
    expect(byId(tiles, 'stats_cd_aoa')).toMatchObject({
      value: '-8,0 ± 1,2',
      subtext: 'n=3 · berechnet · unkalibriert',
      estimatedLabel: 'berechnet · unkalibriert',
    });
    // Two shots are not enough.
    const two = buildClubStatTiles(shots('8-iron', rows.slice(0, 2)), '8-iron');
    expect(byId(two, 'stats_cd_path').value).toBe(NO_VALUE);
    expect(byId(two, 'stats_cd_path').estimated).toBeUndefined();
  });

  it.each([
    ['en', 'n=3 · calculated · uncalibrated'],
    ['es', 'n=3 · calculado · sin calibrar'],
  ] as const)('%s: captions the attack angle without a low point', (locale, caption) => {
    setActiveLocale(locale);
    const rows = [10, 12, 14].map((cm, i) => ({ camera_low_point_cm: cm, camera_attack_angle_deg: -8 + i }));
    expect(byId(buildClubStatTiles(shots('8-iron', rows), '8-iron'), 'stats_cd_aoa').subtext).toBe(caption);
  });

  it.each(['en', 'de', 'es'] as const)(
    '%s: never shows the face, face to path, the spin axis, the strike or the low point',
    (locale) => {
      setActiveLocale(locale);
      const rows = Array.from({ length: 9 }, (_, i) => ({
        ...UNMEASURABLE_CLUB_DATA,
        camera_club_path_deg: 2 + i / 10,
        camera_attack_angle_deg: -4 - i / 10,
      }));
      const all = JSON.stringify(buildClubStatTiles(shots('8-iron', rows), '8-iron'));
      for (const word of UNMEASURABLE_COPY[locale]) expect(all, word).not.toContain(word);
      expect(all).not.toMatch(/12[.,]2|19[.,]1|12[.,]3|cm|mm/);
    }
  );

  it('averages only the camera attack angle: a radar one never fills the tile, an empty tile stays unmarked', () => {
    const radar = shots('8-iron', [-3, -4, -5].map((v) => ({ club_angle_deg: v, camera_attack_angle_deg: null })));
    const empty = byId(buildClubStatTiles(radar, '8-iron'), 'stats_cd_aoa');
    expect(empty).toMatchObject({ value: NO_VALUE, subtext: undefined });
    expect(empty.estimated).toBeUndefined();
    expect(empty.estimatedLabel).toBeUndefined();
    const camera = shots('8-iron', [-9, -10, -11].map((v) => ({ club_angle_deg: -2, camera_attack_angle_deg: v })));
    expect(byId(buildClubStatTiles(camera, '8-iron'), 'stats_cd_aoa')).toMatchObject({
      value: '-10.0 ± 0.6',
      estimated: true,
      estimatedLabel: 'calculated · uncalibrated',
    });
  });

  it('counts only the selected club', () => {
    const mixed = [
      ...shots('8-iron', [{ camera_club_path_deg: 1 }, { camera_club_path_deg: 1 }]),
      ...shots('pw', [{ camera_club_path_deg: 9 }, { camera_club_path_deg: 9 }, { camera_club_path_deg: 9 }]),
    ];
    expect(byId(buildClubStatTiles(mixed, '8-iron'), 'stats_cd_path').value).toBe(NO_VALUE);
    expect(byId(buildClubStatTiles(mixed, 'pw'), 'stats_cd_path').value).toBe('+9.0 ± 0.0');
  });
});
