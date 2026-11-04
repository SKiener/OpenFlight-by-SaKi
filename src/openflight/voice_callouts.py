"""Spoken shot callouts: one setting for the whole session, held here.

The Pi has no speaker, so the kiosk never speaks; the phones at the bay do. The owner may
switch the callouts on from any screen (the kiosk menu or a phone), so the setting lives on
the server and every client follows its ``voice_callouts`` broadcast. Each phone decides
for itself whether it can speak (ui/src/services/shotAnnouncer.ts).

``language`` stays None until someone picks one: every phone then speaks in its own
browser language, as before.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_VOICE_CALLOUTS_PATH = Path.home() / ".config" / "openflight" / "voice_callouts.json"
VOICE_CALLOUTS_PATH_ENV = "OPENFLIGHT_VOICE_CALLOUTS_PATH"

# The spoken order and the voice languages, as CALLOUT_METRICS / CALLOUT_LANGUAGES in
# ui/src/services/shotCallout.ts (tests/test_voice_callouts.py keeps the two in step).
CALLOUT_METRICS = ("carry", "ball_speed", "club_speed", "smash", "launch_v", "spin")
DEFAULT_CALLOUT_METRICS = ("carry", "ball_speed")
CALLOUT_LANGUAGES = ("de", "en", "es")

_FIELDS = ("enabled", "metrics", "language")


def normalize_metrics(values: Any) -> Optional[List[str]]:
    """Known ids in the spoken order, without duplicates; None when not a list.

    Unknown ids are dropped, as the UI store does (a newer page may know more).
    """
    if not isinstance(values, list):
        return None
    chosen = {value for value in values if isinstance(value, str)}
    return [metric for metric in CALLOUT_METRICS if metric in chosen]


def resolve_voice_callouts_path(path: Union[str, Path, None] = None) -> Path:
    """Constructor argument, then ``OPENFLIGHT_VOICE_CALLOUTS_PATH``, then the user default."""
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = (os.environ.get(VOICE_CALLOUTS_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_VOICE_CALLOUTS_PATH


class VoiceCalloutStore:
    """The persisted callout setting: ``{enabled, metrics, language}``."""

    def __init__(self, path: Union[str, Path, None] = None):
        self._path = resolve_voice_callouts_path(path)
        self._lock = threading.Lock()
        self._enabled = False
        self._metrics: List[str] = list(DEFAULT_CALLOUT_METRICS)
        self._language: Optional[str] = None
        self._load()

    def snapshot(self) -> Dict[str, Any]:
        """The ``voice_callouts`` payload."""
        with self._lock:
            return {
                "enabled": self._enabled,
                "metrics": list(self._metrics),
                "language": self._language,
            }

    def update(self, raw: Any) -> Optional[Dict[str, Any]]:
        """Apply a partial change and persist it; None (nothing changed) when rejected.

        A change names at least one of ``enabled`` (bool), ``metrics`` (a list leaving at
        least one known id) and ``language`` (one of CALLOUT_LANGUAGES). Any invalid field
        rejects the whole change.
        """
        patch = self._parse_patch(raw)
        if patch is None:
            return None
        with self._lock:
            enabled = patch.get("enabled", self._enabled)
            metrics = patch.get("metrics", self._metrics)
            language = patch.get("language", self._language)
            payload = {"enabled": enabled, "metrics": list(metrics), "language": language}
            if not self._write(payload):
                return None
            self._enabled, self._metrics, self._language = enabled, list(metrics), language
            return payload

    @staticmethod
    def _parse_patch(raw: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        patch: Dict[str, Any] = {}
        if "enabled" in raw:
            if not isinstance(raw["enabled"], bool):
                return None
            patch["enabled"] = raw["enabled"]
        if "metrics" in raw:
            metrics = normalize_metrics(raw["metrics"])
            if not metrics:
                return None
            patch["metrics"] = metrics
        if "language" in raw:
            if raw["language"] not in CALLOUT_LANGUAGES:
                return None
            patch["language"] = raw["language"]
        return patch or None

    def _load(self) -> None:
        """Read the file field by field; anything missing or malformed keeps its default."""
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as error:
            logger.warning("[voice] could not read %s: %s", self._path, error)
            return
        if not isinstance(raw, dict):
            logger.warning("[voice] ignoring %s: not an object", self._path)
            return
        if isinstance(raw.get("enabled"), bool):
            self._enabled = raw["enabled"]
        metrics = normalize_metrics(raw.get("metrics"))
        if metrics:
            self._metrics = metrics
        if raw.get("language") in CALLOUT_LANGUAGES:
            self._language = raw["language"]

    def _write(self, payload: dict) -> bool:
        """Write atomically (temp file + rename). Never raises into a caller."""
        temp_path = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump({key: payload[key] for key in _FIELDS}, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path)
            return True
        except OSError as error:
            logger.error("[voice] could not save %s: %s", self._path, error)
            try:
                temp_path.unlink()
            except OSError:
                pass
            return False
