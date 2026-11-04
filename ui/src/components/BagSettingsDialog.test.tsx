import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it } from 'vitest';
import { BagSettingsDialog } from './BagSettingsDialog';
import { ALL_CLUBS, getBagClubIds, getClubName, toggleBagClub } from '../data/clubs';
import { clubSections } from './panel/pickerSections';
import { PickerOverlay } from './panel/PickerOverlay';
import { setActiveLocale } from '../i18n';
import { useSystemStore } from '../stores/useSystemStore';
import { useProfileStore } from '../stores/useProfileStore';
import { useUnitPreferenceStore } from '../stores/useUnitPreferenceStore';

const bag = ['wedge-58', 'wedge-54', 'wedge-48', 'wedge-44', '9-iron', '8-iron', '7-iron', '6-iron', '5-iron', '4-iron', '7-wood', '3-wood', 'mini-driver', 'driver'];
const profile = { id: 'golfer', name: 'Golfer', created_at: '', settings: { club_ids: bag } };

afterEach(() => { setActiveLocale('en'); useSystemStore.setState({ connected: false }); });

describe('personal bag', () => {
  it('uses server-saved mixed units on reconnect and preserves them on bag broadcasts', () => {
    useUnitPreferenceStore.setState({ unitSystem: 'metric' });
    useProfileStore.getState().applySnapshot({ profiles: [{ ...profile, settings: { ...profile.settings, unit_system: 'mph-meters' } }], active_profile_id: profile.id });
    expect(useUnitPreferenceStore.getState().unitSystem).toBe('mph-meters');
    useProfileStore.getState().applySnapshot({ profiles: [{ ...profile, settings: { ...profile.settings, unit_system: 'invalid' } }], active_profile_id: profile.id });
    expect(useUnitPreferenceStore.getState().unitSystem).toBe('mph-meters');
    useProfileStore.setState({ profiles: [], activeProfileId: '', loaded: false });
    useUnitPreferenceStore.setState({ unitSystem: 'imperial' });
  });
  it('filters the quick picker to the 14 selected clubs and removes empty groups', () => {
    const sections = clubSections(getBagClubIds(profile.settings));
    expect(sections.flatMap((section) => section.options.map((club) => club.id)).sort()).toEqual([...bag].sort());
    expect(sections.map((section) => section.name)).toEqual(['Wedges', 'Irons', 'Woods']);
    const html = renderToString(<PickerOverlay title="Clubs" sections={sections} selectedId="mini-driver" onSelect={() => {}} onClose={() => {}} />);
    expect(html).toContain('>Mini DR</span></button>');
    expect(html).not.toContain('>5W</span></button>');
    expect(html).not.toContain('Hybrids');
  });
  it('defaults old profiles to the catalogue and sanitizes invalid stored entries', () => {
    expect(getBagClubIds()).toHaveLength(ALL_CLUBS.length);
    expect(getBagClubIds({ club_ids: ['ghost', 12, 'driver', 'driver'] })).toEqual(['driver']);
    expect(getBagClubIds({ club_ids: [] })).toHaveLength(ALL_CLUBS.length);
  });
  it('can toggle clubs but never remove the last one or add an unknown id', () => {
    expect(toggleBagClub(['driver'], 'driver')).toEqual(['driver']);
    expect(toggleBagClub(['driver'], '7-iron')).toEqual(['driver', '7-iron']);
    expect(toggleBagClub(['driver', '7-iron'], 'driver')).toEqual(['7-iron']);
    expect(toggleBagClub(['driver'], 'bogus')).toEqual(['driver']);
  });
  it('shows selected state, saved profile, and the German settings entry', () => {
    setActiveLocale('de');
    useSystemStore.setState({ connected: true });
    const html = renderToString(<BagSettingsDialog profile={profile} onClose={() => {}} />);
    expect(html).toContain('Meine Schläger');
    expect(html).toContain('14 Schläger ausgewählt');
    expect(html).toContain('Golfer');
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(14);
    expect(html).toContain('Wedge 44°');
    expect(html).toContain('Mini Driver');
    expect(html).toContain('Auswahl speichern');
  });
  it('disables saving offline and keeps historical labels independent of bag settings', () => {
    const html = renderToString(<BagSettingsDialog profile={profile} onClose={() => {}} />);
    expect(html).toMatch(/disabled="">Save selection/);
    expect(html).toContain('Disconnected');
    expect(getClubName('pw')).toBe('Pitching Wedge');
    expect(getClubName('wedge-44')).toBe('Wedge 44°');
    expect(getClubName('mini-driver')).toBe('Mini Driver');
  });
});
