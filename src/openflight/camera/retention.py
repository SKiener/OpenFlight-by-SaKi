"""Opt-in retention for saved camera clips.

Every sound-trigger edge saves a clip of roughly 16 MB, and most of them are
false triggers. This module frees their heavy payloads while keeping
``metadata.json`` for later evaluation, under deliberately conservative rules:

* A clip that any session log references is a shot clip and is never touched.
  References are clip directory names anywhere in a session line (for example
  ``camera_capture.capture_path``) and ``camera_replay`` shots: a replay id is a
  per-process UUID, so a replay that cannot be tied to a clip name through its
  ``camera_capture`` line protects every clip within ``REPLAY_WINDOW_S`` of the
  shot.
* A session log that cannot be read, or a line mentioning the camera that does
  not parse, aborts the whole run: nothing is pruned.
* Clips younger than one hour (by name, metadata or file times), clips that
  are being encoded, and clips with an unknown layout are skipped.
* Only regular files inside directories named like
  ``camera_YYYYMMDD_HHMMSS_mmm_NNN`` directly under the clip root are deleted;
  symlinks are never followed.

Non-shot clips older than ``days`` lose their payload. If the clip filesystem
then has less than ``MIN_FREE_BYTES`` free, younger non-shot clips are pruned
oldest first until enough space is free.

Manual use::

    uv run python -m openflight.camera.retention --root DIR --sessions DIR --days 14 --dry-run
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import math
import os
import re
import shutil
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)

DEFAULT_DAYS = 14
MIN_FREE_BYTES = 10 * 1024**3
MIN_AGE_S = 3600.0
RUN_INTERVAL_S = 24 * 3600.0
FIRST_RUN_DELAY_S = 15 * 60.0
BUSY_RECHECK_S = 60.0
DRY_RUN_INFO_NAMES = 10
REPLAY_WINDOW_S = 60.0
METADATA_NAME = "metadata.json"
SESSION_GLOB = "session_*.jsonl"

CLIP_NAME = re.compile(r"camera_(\d{8}_\d{6})_(\d{3})_(\d{3,})")
_HEAVY_NAMES = frozenset({"frames.npz", "replay.mp4"})
_ENCODING_TEMP = re.compile(r"\.replay-[0-9a-f]{32}\.mp4")
_OWN_TEMP = re.compile(r"\.metadata-[0-9a-f]{32}\.tmp")

FreeSpace = Callable[[Path], int]
Busy = Callable[[], bool]
Wait = Callable[[float], bool]


class RetentionUncertain(RuntimeError):
    """The session logs cannot prove which clips are shot clips."""


@dataclass
class ShotReferences:
    """Clip names referenced by session logs plus times of unresolved replays."""

    names: set[str] = field(default_factory=set)
    replay_times: list[float] = field(default_factory=list)

    def protects(self, clip: CameraClip) -> bool:
        """Whether this clip may be needed by a saved shot."""
        if clip.name in self.names:
            return True
        return any(
            abs(clip_time - shot_time) <= REPLAY_WINDOW_S
            for clip_time in clip.times
            for shot_time in self.replay_times
        )


@dataclass
class CameraClip:
    """One clip directory with a known layout."""

    path: Path
    name: str
    times: list[float]
    newest_change: float
    heavy: list[Path]
    metadata: dict

    @property
    def timestamp(self) -> float:
        """The youngest of the clip's own timestamps (conservative age)."""
        return max(self.times)


@dataclass
class RetentionReport:
    """Outcome of one retention run."""

    dry_run: bool
    scanned: int = 0
    shot_clips: int = 0
    skipped: int = 0
    pruned: int = 0
    bytes_freed: int = 0
    errors: int = 0
    free_bytes: int | None = None
    aborted: str | None = None
    pruned_names: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One log line."""
        if self.aborted:
            return f"aborted, nothing pruned: {self.aborted}"
        verb = "would prune" if self.dry_run else "pruned"
        free = "unknown" if self.free_bytes is None else f"{self.free_bytes / 1024**3:.1f} GB"
        return (
            f"{self.scanned} clips scanned, {self.shot_clips} shot clips kept, "
            f"{self.skipped} skipped, {verb} {self.pruned} "
            f"({self.bytes_freed / 1024**2:.1f} MB), {self.errors} errors, free space {free}"
            + (" [dry run]" if self.dry_run else "")
        )


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _key(shot_number: object, timestamp: object) -> str:
    return json.dumps([shot_number, timestamp], sort_keys=True, default=str)


def _shot_time(entry: dict, outer: dict) -> float | None:
    """Epoch seconds of a shot, from the most precise field available."""
    for source in (entry, outer):
        if _finite(source.get("impact_timestamp")):
            return float(source["impact_timestamp"])
    for source in (entry, outer):
        for name in ("timestamp", "ts"):
            value = source.get(name)
            if isinstance(value, str):
                try:
                    return datetime.fromisoformat(value).timestamp()
                except ValueError:
                    continue
    return None


def _replay_holders(value: object):
    """Every dict (at any depth) that carries a non-empty ``camera_replay``."""
    if isinstance(value, dict):
        replay = value.get("camera_replay")
        if isinstance(replay, dict) and replay:
            yield value
        for child in value.values():
            yield from _replay_holders(child)
    elif isinstance(value, list):
        for child in value:
            yield from _replay_holders(child)


def _capture_names(capture_path: str) -> set[str]:
    names = {match.group(0) for match in CLIP_NAME.finditer(capture_path)}
    try:
        resolved = Path(capture_path).resolve().name
    except (OSError, RuntimeError, ValueError):
        resolved = ""
    if CLIP_NAME.fullmatch(resolved):
        names.add(resolved)
    return names


def _scan_session(path: Path, refs: ShotReferences) -> None:
    """Add one session log's clip references; RetentionUncertain if it is unreadable."""
    captures: dict[str, set[str]] = {}
    replays: list[tuple[str, float | None]] = []
    try:
        with path.open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                if b"camera" not in raw:
                    continue
                try:
                    text = raw.decode("utf-8")
                    entry = json.loads(text)
                except (UnicodeDecodeError, ValueError) as error:
                    raise RetentionUncertain(
                        f"{path} line {number} mentions the camera but does not parse"
                    ) from error
                refs.names.update(match.group(0) for match in CLIP_NAME.finditer(text))
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "camera_capture" and isinstance(
                    entry.get("capture_path"), str
                ):
                    names = _capture_names(entry["capture_path"])
                    refs.names.update(names)
                    key = _key(entry.get("shot_number"), entry.get("shot_timestamp"))
                    captures.setdefault(key, set()).update(names)
                for holder in _replay_holders(entry):
                    key = _key(holder.get("shot_number"), holder.get("impact_timestamp"))
                    replays.append((key, _shot_time(holder, entry)))
    except OSError as error:
        raise RetentionUncertain(f"{path} cannot be read: {error}") from error
    for key, shot_time in replays:
        if captures.get(key):
            continue
        if shot_time is None:
            raise RetentionUncertain(f"{path} has a camera replay without clip or time")
        refs.replay_times.append(shot_time)


def collect_shot_references(sessions_dir: Path, *, clip_root: Path | None = None) -> ShotReferences:
    """Scan every ``session_*.jsonl`` below ``sessions_dir`` (symlinked dirs not followed)."""
    if not sessions_dir.is_dir():
        raise RetentionUncertain(f"session directory {sessions_dir} is not available")
    skip = os.path.realpath(clip_root) if clip_root is not None else None
    refs = ShotReferences()

    def _walk_error(error: OSError) -> None:
        raise RetentionUncertain(f"cannot list {error.filename}: {error}") from error

    for directory, subdirs, files in os.walk(sessions_dir, onerror=_walk_error):
        subdirs[:] = [
            name for name in subdirs if os.path.realpath(os.path.join(directory, name)) != skip
        ]
        for name in sorted(files):
            if fnmatch.fnmatchcase(name, SESSION_GLOB):
                _scan_session(Path(directory) / name, refs)
    return refs


def _clip_name_time(name: str) -> float | None:
    match = CLIP_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        started = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return started.timestamp() + int(match.group(2)) / 1000.0


def inspect_clip(path: Path) -> tuple[CameraClip | None, str]:  # pylint: disable=too-many-return-statements
    """A clip with a known layout, or None and the reason it must be left alone."""
    name_time = _clip_name_time(path.name)
    if name_time is None:
        return None, "unparseable clip name"
    heavy: list[Path] = []
    newest = path.lstat().st_mtime
    has_metadata = False
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                return None, f"unexpected entry {entry.name}"
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            if entry.name == METADATA_NAME:
                has_metadata = True
            elif _ENCODING_TEMP.fullmatch(entry.name):
                return None, "replay is being encoded"
            elif (
                entry.name in _HEAVY_NAMES
                or entry.name.endswith(".pgm")
                or _OWN_TEMP.fullmatch(entry.name)
            ):
                heavy.append(Path(entry.path))
            else:
                return None, f"unknown file {entry.name}"
    if not has_metadata:
        return None, "no metadata.json"
    try:
        metadata = json.loads((path / METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return None, f"unreadable metadata.json ({error})"
    if not isinstance(metadata, dict):
        return None, "metadata.json is not an object"
    times = [name_time] + [
        float(metadata[key])
        for key in ("trigger_timestamp", "completed_timestamp")
        if _finite(metadata.get(key))
    ]
    return (
        CameraClip(
            path=path,
            name=path.name,
            times=times,
            newest_change=newest,
            heavy=sorted(heavy),
            metadata=metadata,
        ),
        "",
    )


def _payload_bytes(clip: CameraClip) -> int:
    total = 0
    for path in clip.heavy:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def prune_clip(clip: CameraClip, *, reason: str, now: float) -> int:
    """Delete the clip's payload files, annotate ``metadata.json``; bytes freed."""
    if os.path.islink(clip.path) or not clip.path.is_dir():
        raise OSError(f"{clip.path} is no longer a clip directory")
    freed = 0
    removed = []
    for path in clip.heavy:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or path.parent != clip.path:
            continue
        os.unlink(path)
        freed += info.st_size
        removed.append(path.name)

    metadata_path = clip.path / METADATA_NAME
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            metadata = dict(clip.metadata)
    except (OSError, ValueError):
        metadata = dict(clip.metadata)
    previous = metadata.get("pruned_bytes")
    metadata["pruned_at"] = datetime.fromtimestamp(now).astimezone().isoformat()
    metadata["pruned_bytes"] = freed + (previous if _finite(previous) else 0)
    metadata["pruned_files"] = sorted(set(metadata.get("pruned_files") or []) | set(removed))
    metadata["pruned_reason"] = reason
    temporary = clip.path / f".metadata-{uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, metadata_path)
    finally:
        temporary.unlink(missing_ok=True)
    return freed


def _list_clips(root: Path, report: RetentionReport, now: float, refs: ShotReferences):
    """Prunable clips: old enough, known layout, not referenced, with a payload."""
    candidates: list[CameraClip] = []
    with os.scandir(root) as entries:
        directories = sorted(
            entry.path
            for entry in entries
            if CLIP_NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False)
        )
    for directory in directories:
        report.scanned += 1
        try:
            clip, why = inspect_clip(Path(directory))
        except OSError as error:
            clip, why = None, str(error)
        if clip is None:
            report.skipped += 1
            logger.debug("[RETENTION] Keeping %s: %s", directory, why)
            continue
        if refs.protects(clip):
            report.shot_clips += 1
        elif now - clip.timestamp < MIN_AGE_S or now - clip.newest_change < MIN_AGE_S:
            report.skipped += 1
        elif clip.heavy:
            candidates.append(clip)
    return sorted(candidates, key=lambda clip: (clip.timestamp, clip.name))


def _apply(clip: CameraClip, reason: str, report: RetentionReport, now: float) -> int:
    size = _payload_bytes(clip)
    if report.dry_run:
        level = logging.INFO if report.pruned < DRY_RUN_INFO_NAMES else logging.DEBUG
        logger.log(
            level, "[RETENTION] Would prune %s (%s, %.1f MB)", clip.name, reason, size / 1024**2
        )
        freed = size
    else:
        try:
            freed = prune_clip(clip, reason=reason, now=now)
        except OSError as error:
            report.errors += 1
            logger.warning("[RETENTION] Could not prune %s: %s", clip.path, error)
            return 0
        logger.debug("[RETENTION] Pruned %s (%s, %d bytes)", clip.name, reason, freed)
    report.pruned += 1
    report.bytes_freed += freed
    report.pruned_names.append(clip.name)
    return freed


def run_retention(  # pylint: disable=too-many-arguments
    root: Path | str,
    sessions_dir: Path | str,
    *,
    days: int = DEFAULT_DAYS,
    dry_run: bool = False,
    min_free_bytes: int = MIN_FREE_BYTES,
    now: float | None = None,
    free_space: FreeSpace = _disk_free,
) -> RetentionReport:
    """One retention pass over ``root``; see the module docstring for the rules."""
    if days < 1:
        raise ValueError("retention days must be at least 1")
    root = Path(root).expanduser()
    sessions_dir = Path(sessions_dir).expanduser()
    now = time.time() if now is None else now
    report = RetentionReport(dry_run=dry_run)
    if not root.is_dir():
        report.aborted = f"clip root {root} does not exist"
        return report
    try:
        refs = collect_shot_references(sessions_dir, clip_root=root)
        candidates = _list_clips(root, report, now, refs)
    except (RetentionUncertain, OSError) as error:
        report.aborted = str(error)
        logger.warning("[RETENTION] Nothing pruned: %s", error)
        return report

    remaining = []
    for clip in candidates:
        if now - clip.timestamp >= days * 86400.0:
            _apply(clip, f"older than {days} days", report, now)
        else:
            remaining.append(clip)

    try:
        free = free_space(root) + (report.bytes_freed if dry_run else 0)
    except OSError as error:
        logger.warning("[RETENTION] Free space unknown, low-disk guard skipped: %s", error)
        return report
    for clip in remaining:
        if free >= min_free_bytes:
            break
        freed = _apply(clip, "low disk space", report, now)
        try:
            free = free + freed if dry_run else free_space(root)
        except OSError:
            break
    report.free_bytes = free
    if free < min_free_bytes:
        logger.warning(
            "[RETENTION] Only %.1f GB free on the clip filesystem after pruning "
            "(target %.1f GB); shot clips are never pruned",
            free / 1024**3,
            min_free_bytes / 1024**3,
        )
    return report


def _lower_priority() -> None:
    """Lower this thread's CPU priority; on Linux ``nice`` applies per thread."""
    if not sys.platform.startswith("linux"):
        return
    try:
        os.nice(10)
    except OSError:
        pass


def _is_busy(busy: Busy | None) -> bool:
    """The busy predicate's answer; a failing predicate does not block retention."""
    if busy is None:
        return False
    try:
        return bool(busy())
    except Exception:  # pylint: disable=broad-exception-caught
        logger.warning("[RETENTION] Busy check failed; running anyway", exc_info=True)
        return False


def _run_logged(root: Path | str, sessions_dir: Path | str, days: int, dry_run: bool) -> None:
    try:
        report = run_retention(root, sessions_dir, days=days, dry_run=dry_run)
        logger.info("[RETENTION] Camera clips: %s", report.summary())
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("[RETENTION] Camera clip retention failed")


def start_retention_thread(  # pylint: disable=too-many-arguments
    root: Path | str,
    sessions_dir: Path | str,
    *,
    days: int,
    dry_run: bool = False,
    interval_s: float = RUN_INTERVAL_S,
    first_delay_s: float = FIRST_RUN_DELAY_S,
    busy: Busy | None = None,
    busy_recheck_s: float = BUSY_RECHECK_S,
    stop_event: threading.Event | None = None,
    wait: Wait | None = None,
) -> threading.Thread:
    """Run retention ``first_delay_s`` after start, then every ``interval_s``.

    The low-priority daemon thread postpones a due run while ``busy()`` is
    true and asks again every ``busy_recheck_s``. ``wait(seconds)`` returns
    True to stop; it defaults to waiting on ``stop_event``.
    """
    stop = stop_event or threading.Event()
    wait = wait or stop.wait

    def _loop() -> None:
        _lower_priority()
        delay = first_delay_s
        while not wait(delay):
            if _is_busy(busy):
                delay = busy_recheck_s
                continue
            _run_logged(root, sessions_dir, days, dry_run)
            delay = interval_s

    thread = threading.Thread(target=_loop, name="camera-retention", daemon=True)
    thread.start()
    return thread


def _positive_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a whole number of days") from error
    if days < 1:
        raise argparse.ArgumentTypeError("must be at least 1 day")
    return days


def add_retention_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the opt-in server options for camera clip retention."""
    parser.add_argument(
        "--camera-retention-days",
        type=_positive_days,
        default=None,
        metavar="N",
        help=(
            "Prune the video payload of camera clips no shot references once they are "
            "older than N days (metadata.json is kept). Default: off"
        ),
    )
    parser.add_argument(
        "--camera-retention-dry-run",
        action="store_true",
        help="With --camera-retention-days, only log what would be pruned",
    )


def start_retention_from_args(
    args: argparse.Namespace,
    root: Path | str,
    sessions_dir: Path | str,
    *,
    busy: Busy | None = None,
) -> threading.Thread | None:
    """Start the retention thread when the server options enable it."""
    days = getattr(args, "camera_retention_days", None)
    if days is None:
        if getattr(args, "camera_retention_dry_run", False):
            logger.warning("[RETENTION] --camera-retention-dry-run needs --camera-retention-days")
        return None
    return start_retention_thread(
        root, sessions_dir, days=days, dry_run=bool(args.camera_retention_dry_run), busy=busy
    )


def main(argv: list[str] | None = None) -> int:
    """Manual retention run; exit status 1 when the run was aborted."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, type=Path, help="Camera clip directory")
    parser.add_argument("--sessions", required=True, type=Path, help="Session log directory")
    parser.add_argument("--days", type=_positive_days, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run_retention(args.root, args.sessions, days=args.days, dry_run=args.dry_run)
    print(report.summary())
    return 1 if report.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
