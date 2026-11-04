"""Personal bag identifiers without changing radar/physics club categories."""

from .types import ClubType


CLUB_VARIANTS = {
    "wedge-44": ClubType.PW,
    "wedge-48": ClubType.GW,
    "wedge-54": ClubType.SW,
    "wedge-58": ClubType.LW,
    "mini-driver": ClubType.DRIVER,
}
SELECTABLE_CLUB_IDS = frozenset(
    [club.value for club in ClubType if club != ClubType.UNKNOWN] + list(CLUB_VARIANTS)
)


def club_category(selection: str) -> ClubType:
    """Resolve an exact bag label to the existing generic measurement category."""
    return CLUB_VARIANTS[selection] if selection in CLUB_VARIANTS else ClubType(selection)


def clean_club_ids(value):
    """Reject invalid/empty bags, preserving user order and removing duplicates."""
    if not isinstance(value, list) or not 1 <= len(value) <= len(SELECTABLE_CLUB_IDS):
        return None
    if any(not isinstance(item, str) or item not in SELECTABLE_CLUB_IDS for item in value):
        return None
    return list(dict.fromkeys(value))
