import { getActiveLocale } from '../i18n';

export interface Club {
  id: string;
  /** Compact picker tile, e.g. "7i". */
  label: string;
  /** Prose name for the header, e.g. "7 Iron". */
  name: string;
}

// Clubs grouped by type. Object insertion order is preserved and drives the
// display order in both the ClubPicker dropdown and the ClubSelectScreen.
export const CLUBS_BY_TYPE: Record<string, Club[]> = {
  Wedges: [
    { id: 'wedge-58', label: '58°', name: 'Wedge 58°' },
    { id: 'wedge-54', label: '54°', name: 'Wedge 54°' },
    { id: 'wedge-48', label: '48°', name: 'Wedge 48°' },
    { id: 'wedge-44', label: '44°', name: 'Wedge 44°' },
  ],
  Irons: [
    { id: '2-iron', label: '2i', name: '2 Iron' },
    { id: '3-iron', label: '3i', name: '3 Iron' },
    { id: '4-iron', label: '4i', name: '4 Iron' },
    { id: '5-iron', label: '5i', name: '5 Iron' },
    { id: '6-iron', label: '6i', name: '6 Iron' },
    { id: '7-iron', label: '7i', name: '7 Iron' },
    { id: '8-iron', label: '8i', name: '8 Iron' },
    { id: '9-iron', label: '9i', name: '9 Iron' },
    { id: 'pw', label: 'PW', name: 'Pitching Wedge' },
    { id: 'gw', label: 'GW', name: 'Gap Wedge' },
    { id: 'sw', label: 'SW', name: 'Sand Wedge' },
    { id: 'lw', label: 'LW', name: 'Lob Wedge' },
  ],
  Hybrids: [
    { id: '3-hybrid', label: '3H', name: '3 Hybrid' },
    { id: '5-hybrid', label: '5H', name: '5 Hybrid' },
    { id: '7-hybrid', label: '7H', name: '7 Hybrid' },
    { id: '9-hybrid', label: '9H', name: '9 Hybrid' },
  ],
  Woods: [
    { id: 'driver', label: 'DR', name: 'Driver' },
    { id: 'mini-driver', label: 'Mini DR', name: 'Mini Driver' },
    { id: '3-wood', label: '3W', name: '3 Wood' },
    { id: '5-wood', label: '5W', name: '5 Wood' },
    { id: '7-wood', label: '7W', name: '7 Wood' },
  ],
};

export const ALL_CLUBS: Club[] = Object.values(CLUBS_BY_TYPE).flat();

/** Driver, mini driver and fairway woods: dplane.WOOD_CATEGORIES, no D-plane spin axis and no curve class. */
export function isWoodClub(clubId: string): boolean {
  return CLUBS_BY_TYPE.Woods.some((club) => club.id === clubId);
}

export function getBagClubIds(settings?: Record<string, unknown>): string[] {
  const stored = settings?.club_ids;
  if (Array.isArray(stored)) {
    const valid = [...new Set(stored.filter((id): id is string =>
      typeof id === 'string' && ALL_CLUBS.some((club) => club.id === id)))];
    if (valid.length) return valid;
  }
  return ALL_CLUBS.map((club) => club.id);
}

export function toggleBagClub(ids: string[], id: string): string[] {
  if (!ALL_CLUBS.some((club) => club.id === id)) return ids;
  return ids.includes(id) ? (ids.length > 1 ? ids.filter((club) => club !== id) : ids) : [...ids, id];
}

export function getClubName(clubId: string): string {
  if (getActiveLocale() === 'de') {
    const club = ALL_CLUBS.find((entry) => entry.id === clubId);
    const match = club?.id.match(/^(\d+)-(iron|wood|hybrid)$/);
    if (match) {
      const type = { iron: 'Eisen', wood: 'Holz', hybrid: 'Hybrid' }[match[2]];
      return `${type} ${match[1]}`;
    }
  }
  return ALL_CLUBS.find((club) => club.id === clubId)?.name ?? clubId;
}
