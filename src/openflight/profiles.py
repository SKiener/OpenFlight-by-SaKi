"""Persistent profile roster: the named contexts shots are attributed to.

A profile is deliberately untyped. It may denote a person ("Cormac") or a
place ("Home Range"), because both are things you want shots recorded
against. Later features attach data via the open ``settings`` dict, which
this module round-trips untouched and never interprets.

The store is the single source of truth for both the roster and which
profile is active; the UI mirrors it and holds no copy of its own.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .clubs.bag import clean_club_ids

logger = logging.getLogger(__name__)

DEFAULT_PROFILES_PATH = Path.home() / ".config" / "openflight" / "profiles.json"
PROFILES_PATH_ENV = "OPENFLIGHT_PROFILES_PATH"
DEFAULT_PROFILE_NAME = "Profile 1"
MAX_PROFILES = 12
MAX_NAME_LENGTH = 40
# Height of the ball's bottom above the mat on the rubber tee (driver only), in metres.
DEFAULT_DRIVER_TEE_HEIGHT_M = 0.04
MAX_DRIVER_TEE_HEIGHT_M = 0.08
# The ball a profile plays, and the club-typical spin table (ballistics.TYPICAL_SPIN_TABLES)
# its carry flies while no spin is measured: "standard" = the tour table, "range" = the
# range-ball table. A profile without a ball type plays the launcher's --typical-spin-table.
BALL_TYPE_SPIN_TABLES: Dict[str, str] = {"standard": "tour", "range": "range-ball"}
DEFAULT_BALL_TYPE = "standard"


def resolve_profiles_path(path: Union[str, Path, None] = None) -> Path:
    """Constructor argument, then ``OPENFLIGHT_PROFILES_PATH``, then the user default.

    Tests and CI point the env var at a temp file so they cannot create or
    rewrite ``~/.config/openflight/profiles.json``.
    """
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = (os.environ.get(PROFILES_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_PROFILES_PATH


def clean_profile_name(raw: Any) -> str:
    """Trim and cap a candidate name. Returns "" when unusable."""
    if raw is None:
        return ""
    return str(raw).strip()[:MAX_NAME_LENGTH]


def clean_driver_tee_height(raw: Any) -> Optional[float]:
    """A finite tee height in [0, MAX_DRIVER_TEE_HEIGHT_M] metres, else None."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or not 0.0 <= value <= MAX_DRIVER_TEE_HEIGHT_M:
        return None
    return value


def driver_tee_height_m(settings: Any) -> float:
    """A profile's stored driver tee height, the default when unset or invalid."""
    stored = settings.get("driver_tee_height_m") if isinstance(settings, dict) else None
    cleaned = clean_driver_tee_height(stored)
    return DEFAULT_DRIVER_TEE_HEIGHT_M if cleaned is None else cleaned


def clean_ball_type(raw: Any) -> Optional[str]:
    """A known ball type (``BALL_TYPE_SPIN_TABLES``), else None."""
    return raw if isinstance(raw, str) and raw in BALL_TYPE_SPIN_TABLES else None


def ball_type_for_spin_table(table: Any) -> str:
    """The ball type whose spin table is ``table`` (the launcher default's ball type).

    A table no ball type maps to reads as ``DEFAULT_BALL_TYPE``.
    """
    return next(
        (ball for ball, mapped in BALL_TYPE_SPIN_TABLES.items() if mapped == table),
        DEFAULT_BALL_TYPE,
    )


def ball_type(settings: Any, default_ball_type: str = DEFAULT_BALL_TYPE) -> str:
    """A profile's stored ball type, ``default_ball_type`` when unset or invalid."""
    stored = settings.get("ball_type") if isinstance(settings, dict) else None
    cleaned = clean_ball_type(stored)
    return default_ball_type if cleaned is None else cleaned


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Profile:
    """One named context that shots are attributed to."""

    id: str
    name: str
    created_at: str
    settings: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Wire/disk representation.

        ``settings`` is shallow-copied so external callers can't mutate store-owned
        state (and bypass the store's lock / persistence path).
        """
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "settings": dict(self.settings),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["Profile"]:
        """Parse one stored record, or None when it is unusable."""
        if not isinstance(raw, dict):
            return None
        profile_id = str(raw.get("id") or "").strip()
        name = clean_profile_name(raw.get("name"))
        if not profile_id or not name:
            return None
        settings = raw.get("settings")
        return cls(
            id=profile_id,
            name=name,
            created_at=str(raw.get("created_at") or _utc_now_iso()),
            settings=settings if isinstance(settings, dict) else {},
        )


class ProfileStore:
    """Load, mutate, and atomically persist the profile roster.

    Mutators return a falsy value and change nothing when they would break
    an invariant: at least one profile always exists, and
    ``active_profile_id`` always names a live profile.
    """

    def __init__(self, path: Union[str, Path, None] = None):
        self._path = resolve_profiles_path(path)
        # One kiosk, one writer -- an in-process lock is enough; no file locking.
        self._lock = threading.Lock()
        self._profiles: List[Profile] = []
        self._active_id: str = ""
        self._load()

    # -- reads ---------------------------------------------------------

    def list(self) -> List[Profile]:  # pylint: disable=redefined-builtin
        """All profiles, in insertion order."""
        return list(self._profiles)

    def get_active(self) -> Profile:
        """The active profile. Always present."""
        for profile in self._profiles:
            if profile.id == self._active_id:
                return profile
        return self._profiles[0]

    def get(self, profile_id: Any) -> Optional[Profile]:
        """The profile with this id, or None."""
        return self._find(profile_id)

    def snapshot(self) -> dict:
        """The authoritative payload broadcast on the socket."""
        with self._lock:
            return self._payload()

    # -- mutations -----------------------------------------------------

    def add(self, name: Any) -> Optional[Profile]:
        """Append a profile and make it active. None when rejected."""
        cleaned = clean_profile_name(name)
        if not cleaned or len(self._profiles) >= MAX_PROFILES:
            return None

        with self._lock:
            profile = Profile(id=uuid.uuid4().hex, name=cleaned, created_at=_utc_now_iso())
            self._profiles.append(profile)
            self._active_id = profile.id
        self.save()
        return profile

    def rename(self, profile_id: Any, name: Any) -> bool:
        """Change a profile's name. Its id and shots are unaffected."""
        cleaned = clean_profile_name(name)
        if not cleaned:
            return False

        with self._lock:
            profile = self._find(profile_id)
            if profile is None:
                return False
            profile.name = cleaned
        self.save()
        return True

    def remove(self, profile_id: Any) -> bool:
        """Delete a profile. Refused for the active or the last one."""
        with self._lock:
            profile = self._find(profile_id)
            if profile is None or profile.id == self._active_id or len(self._profiles) <= 1:
                return False
            self._profiles.remove(profile)
        self.save()
        return True

    def set_active(self, profile_id: Any) -> bool:
        """Change the active profile. Refused for an unknown id."""
        with self._lock:
            profile = self._find(profile_id)
            if profile is None:
                return False
            self._active_id = profile.id
        self.save()
        return True

    def set_clubs(self, profile_id: Any, club_ids: Any) -> bool:
        """Persist a personal bag without changing other settings or shot history."""
        cleaned = clean_club_ids(club_ids)
        if cleaned is None:
            return False
        return self._set_setting(profile_id, "club_ids", cleaned)

    def set_units(self, profile_id: Any, unit_system: Any) -> bool:
        """Store independently selected speed/distance units for this profile."""
        if unit_system not in ("imperial", "metric", "mph-meters", "kmh-yards"):
            return False
        return self._set_setting(profile_id, "unit_system", unit_system)

    def set_driver_tee_height(self, profile_id: Any, value: Any) -> bool:
        """Store the driver's tee height (ball bottom above the mat, metres)."""
        cleaned = clean_driver_tee_height(value)
        if cleaned is None:
            return False
        return self._set_setting(profile_id, "driver_tee_height_m", cleaned)

    def set_ball_type(self, profile_id: Any, value: Any) -> bool:
        """Store the ball type ("standard" or "range") the profile's carries fly."""
        cleaned = clean_ball_type(value)
        if cleaned is None:
            return False
        return self._set_setting(profile_id, "ball_type", cleaned)

    def _set_setting(self, profile_id: Any, key: str, value: Any) -> bool:
        with self._lock:
            profile = self._find(profile_id)
            if profile is None:
                return False
            previous = profile.settings
            profile.settings = {**previous, key: value}
            if not self._write_payload(self._payload()):
                profile.settings = previous
                return False
        return True

    # -- persistence ---------------------------------------------------

    def save(self) -> None:
        """Write the roster atomically. Never raises into a caller."""
        with self._lock:
            payload = self._payload()
            self._write_payload(payload)

    def _write_payload(self, payload: dict) -> bool:
        temp_path = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            return True
        except OSError as error:
            logger.error("[profiles] could not save profiles to %s: %s", self._path, error)
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False

    def _payload(self) -> dict:
        """The disk/wire representation of the whole roster."""
        return {
            "profiles": [profile.to_dict() for profile in self._profiles],
            "active_profile_id": self.get_active().id,
        }

    def _find(self, profile_id: Any) -> Optional[Profile]:
        wanted = str(profile_id or "").strip()
        if not wanted:
            return None
        return next((profile for profile in self._profiles if profile.id == wanted), None)

    def _load(self) -> None:
        """Read the roster, seeding a default when absent or unusable."""
        raw: Any = None
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            raw = None
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[profiles] could not read %s: %s", self._path, error)
            raw = None

        entries = raw.get("profiles") if isinstance(raw, dict) else None
        parsed = (
            [Profile.from_dict(entry) for entry in entries] if isinstance(entries, list) else []
        )
        self._profiles = [profile for profile in parsed if profile is not None][:MAX_PROFILES]

        if not self._profiles:
            self._profiles = [
                Profile(id=uuid.uuid4().hex, name=DEFAULT_PROFILE_NAME, created_at=_utc_now_iso())
            ]
            self._active_id = self._profiles[0].id
            self.save()
            return

        stored_active = str(raw.get("active_profile_id") or "") if isinstance(raw, dict) else ""
        known = {profile.id for profile in self._profiles}
        self._active_id = stored_active if stored_active in known else self._profiles[0].id
