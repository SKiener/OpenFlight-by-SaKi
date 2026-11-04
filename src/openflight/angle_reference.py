"""The camera as the launch-angle reference: session TI offsets and the tapped target line.

Vertical launch
    A camera ball-flight estimate that was accepted and carries a vertical
    launch angle sets the shot's ``launch_angle_vertical`` (source
    ``"camera"``). Every shot with both a plausible TI vertical and such a
    camera vertical adds TI minus camera to the session's vertical offset.
    Once that offset is valid, a TI-only shot is corrected by it (source
    ``"radar_calibrated"``), provided the corrected value still passes the
    club/speed plausibility guard the raw TI passed (``vertical_guard``, the
    server's ``_ti_vertical_launch_is_plausible``). A corrected value that fails
    it (2026-10-25 OF33: a topped driver read +0.08 deg, the +3.00 deg offset
    made it -2.92 deg and 0 yd of carry) is not shown: the shot keeps the TI as
    measured (source ``"radar"``) and logs the rejected value and the guard's
    rule. The uncorrected TI value is always kept in ``iwr6843_vertical_deg``.
    The TI read +2.3 deg high on 2026-10-25, but +0.7 deg on 2026-10-03 and +2.5 deg on 2026-10-05: the offset is not
    stable, so it is learned per session from the camera, never fixed.

Horizontal launch
    The same running median of the TI azimuth minus the camera start direction
    is kept and logged, but never applied: a TI-only shot keeps the fixed
    ``--iwr6843-azimuth-offset-deg`` (source ``"radar"``). The TI horizontal is
    compressed (TI = -1.84 + 0.288 x Toptracer on 2026-10-25), so TI minus
    camera is about -1.84 - 0.71 x the start direction: its median follows the
    directions the pairs happened to start in, not a setup constant. On
    2026-10-25 the +1.62 deg learned on drivers starting left moved the twelve
    TI-only 8-irons to an RMSE of 2.80 deg against 1.70 deg with the fixed
    offset (replay, validation only). The logged median stays a diagnostic.

Validity of an offset (constants set before looking at any data; nothing
here is fitted to Toptracer, which never enters the reference):

* ``MIN_PAIRS`` = 5 shots with both values (the orchestrator's brief);
* ``MAX_SPREAD_DEG`` = 1.0: the robust spread of the differences, 1.4826 x
  their median absolute deviation (a standard deviation for normal noise),
  must not exceed 1 deg (the orchestrator's brief);
* ``WINDOW_PAIRS`` = 20: the median runs over the last 20 pairs. A mount
  bumped mid-session is followed after 10 new pairs (half the window), and
  one outlier moves the median by at most one rank. A design choice, not
  derived from any session.

An offset is reset on session clear, profile switch and camera geometry
change (capture mode, sensor window); a new session (server start) starts
empty. The horizontal offset is also reset whenever the target line is set
or cleared, because its camera directions are then measured from another
line. Each shot's ball flight is stamped with the target it was measured
against (``angle_reference["measured_against"]``, set by the fusion when it
reads the target pixel). A shot whose target is no longer the current one (a
tap while the shot was being processed) adds no horizontal pair, and its log
names the target it was measured against.

Target line
    In the camera Align view the user taps the target he aims at (a flag,
    post or sign on the range). The tap is a position in the preview, which
    the capture runtime derolls by ``--camera-capture-roll-deg``; it is
    mapped back to saved-frame pixels (``preview_to_frame_px``), then
    undistorted with the active lens model and derolled again in normalized
    coordinates (``normalized_image_offsets``). Its azimuth in the camera
    frame is shown with the time it was set. The ball-flight estimator turns
    the same pixel into the target's azimuth in each shot's own camera pose
    and reports every camera start direction relative to it (it replaces
    ``--camera-capture-horizontal-offset-deg`` and the yaw from the teed ball,
    both of which cancel). Assumption: the target is far, so the parallax
    between the ball and the camera (about 1.5 m behind it and a few cm to
    the side) is negligible: 0.1 m of lateral offset is 0.19 deg at 30 m and
    0.06 deg at 100 m. Pick targets more than 30 m away.

    The target persists per device setup (``save_camera_marks``) and survives
    a restart. Its geometry key holds everything that maps a pixel to a
    direction: a mount part (orientation, scaler crop, roll, the calibrated
    lens model, mount offsets) and a capture-mode part (frame size, sensor
    window, ``MODE_GEOMETRY_KEYS``). A capture-mode or sensor-window change
    between modes with a known frame mapping
    (``capture_runtime.CAPTURE_MODE_FRAME_OFFSETS``: 640x400 and the 640x200
    strip) keeps the target's direction and re-projects its pixel through the
    new mode's lens model (``carry_camera_marks``); the server saves both
    marks in the canonical 640x400 frame, so a restart in another mapped mode
    restores them too. Another mount, or a mode without a known mapping,
    clears it (on load: discards it), as it always did.

Ball mark
    In the same view the user first taps the resting ball (``BallMark``): the
    tap is mapped to saved-frame pixels the same way, without the lens model
    (it is a pixel, not a direction). The camera ball flight then searches the
    resting ball only in a window around it (``camera.departed_ball``); the
    mark never enters the direction maths. It is saved in the same file as the
    target, under the same geometry key. On a capture-mode change it moves by
    the modes' frame shift (640x400 -> 640x200: y - 100 - strip offset, or
    y - 100 + strip offset for the software-rotated 180-degree mount); one
    that falls outside the new frame is dropped alone
    (``BALL_MARK_OUTSIDE_MODE``). Otherwise it is cleared with the target on a
    geometry change. The file is version 2 (``target_line`` and ``ball_mark``, either
    may be null); the version 1 file of older builds (the target alone) still
    loads.
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from openflight.camera.capture_runtime import CANONICAL_FRAME_SIZE, mode_frame_shift
from openflight.camera.geometry import deroll_normalized_offsets, normalized_image_offsets
from openflight.camera.intrinsics import CameraIntrinsics

logger = logging.getLogger(__name__)

DEFAULT_TARGET_LINE_PATH = Path.home() / ".config" / "openflight" / "camera-target-line.json"
TARGET_LINE_PATH_ENV = "OPENFLIGHT_CAMERA_TARGET_LINE_PATH"
MIN_PAIRS = 5
MAX_SPREAD_DEG = 1.0
WINDOW_PAIRS = 20
MAD_TO_SIGMA = 1.4826
# Version 1 held the target line alone; version 2 holds the target and the ball mark.
TARGET_LINE_VERSION = 1
CAMERA_MARKS_VERSION = 2
CAMERA_SOURCES = ("camera_assisted_experimental", "camera_only_experimental")
# The capture-mode part of a mark's geometry key; the rest is the mount part.
MODE_GEOMETRY_KEYS = ("width", "height", "vertical_offset_px")
# Event (and load reason) of a ball mark that lies outside the new capture mode's frame.
BALL_MARK_OUTSIDE_MODE = "ball_mark_outside_mode"
CALIBRATED_IMPLAUSIBLE_REASON = "calibrated_implausible"
# (angle_deg, shot) -> (plausible, details with the guard's "rule").
VerticalGuard = Callable[[float, object], tuple[bool, dict]]
# Confidence of a camera vertical by estimator tier: the numbers
# select_camera_assisted_horizontal gives the camera direction (0.75 high, 0.45
# experimental, 0.30 camera-only depth), without its IWR agreement condition:
# the IWR vertical is what this reference calibrates.
CAMERA_VERTICAL_CONFIDENCE = {"high": 0.75, "experimental": 0.45}
CAMERA_ONLY_VERTICAL_CONFIDENCE = 0.30


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


class SessionOffset:
    """Running median of radar minus camera over the session's recent pairs."""

    def __init__(self, axis: str, *, applied: bool = True):
        self.axis = axis
        # False: the offset is logged as a diagnostic, never applied to a shot.
        self.applied = applied
        self._differences: deque[float] = deque(maxlen=WINDOW_PAIRS)
        self._pairs_total = 0
        self._reset_reason: str | None = "session_start"

    def add(self, radar_deg: float, camera_deg: float) -> bool:
        """Add one pair; True when the offset became valid or invalid with it."""
        was_valid = self.valid
        self._differences.append(float(radar_deg) - float(camera_deg))
        self._pairs_total += 1
        return self.valid != was_valid

    def reset(self, reason: str) -> bool:
        """Forget every pair; True when there was anything to forget."""
        had_pairs = self._pairs_total > 0
        self._differences.clear()
        self._pairs_total = 0
        self._reset_reason = reason
        return had_pairs

    @property
    def offset_deg(self) -> float | None:
        """Median radar minus camera over the window, None without pairs."""
        return statistics.median(self._differences) if self._differences else None

    @property
    def spread_deg(self) -> float | None:
        """1.4826 x the median absolute deviation of the window, None without pairs."""
        offset = self.offset_deg
        if offset is None:
            return None
        return MAD_TO_SIGMA * statistics.median(abs(value - offset) for value in self._differences)

    @property
    def valid(self) -> bool:
        """At least MIN_PAIRS pairs whose robust spread is within MAX_SPREAD_DEG."""
        spread = self.spread_deg
        return (
            len(self._differences) >= MIN_PAIRS and spread is not None and spread <= MAX_SPREAD_DEG
        )

    def correct(self, radar_deg: float) -> float | None:
        """The radar angle minus the valid offset, None while the offset is not valid."""
        offset = self.offset_deg
        return float(radar_deg) - offset if self.valid and offset is not None else None

    def state(self) -> dict:
        """JSON-ready state for the session log."""
        return {
            "valid": self.valid,
            "applied": self.applied,
            "offset_deg": _rounded(self.offset_deg),
            "spread_deg": _rounded(self.spread_deg),
            "pairs": len(self._differences),
            "pairs_total": self._pairs_total,
            "window": WINDOW_PAIRS,
            "min_pairs": MIN_PAIRS,
            "max_spread_deg": MAX_SPREAD_DEG,
            "reset_reason": self._reset_reason,
        }


@dataclass(frozen=True)
class TargetLine:
    """A tapped target: saved-frame pixel, preview position, camera-frame direction."""

    frame_x_px: float
    frame_y_px: float
    preview_x_pct: float
    preview_y_pct: float
    azimuth_deg: float
    elevation_deg: float
    set_at: float
    geometry: dict

    @property
    def frame_px(self) -> tuple[float, float]:
        """The target in saved-frame pixels, as the ball-flight estimator reads it."""
        return (self.frame_x_px, self.frame_y_px)

    def to_dict(self) -> dict:
        """JSON-ready form, as persisted and shown in the Align view."""
        data = asdict(self)
        for key in (
            "frame_x_px",
            "frame_y_px",
            "preview_x_pct",
            "preview_y_pct",
            "azimuth_deg",
            "elevation_deg",
        ):
            data[key] = round(data[key], 3)
        return data

    def to_log(self) -> dict:
        """The session-log form: the direction, where it was tapped and when."""
        data = self.to_dict()
        del data["geometry"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> TargetLine:
        """Rebuild a persisted target; raises ValueError for anything malformed."""
        values, geometry = _persisted_fields(
            data,
            (
                "frame_x_px",
                "frame_y_px",
                "preview_x_pct",
                "preview_y_pct",
                "azimuth_deg",
                "elevation_deg",
                "set_at",
            ),
            "target line",
        )
        return cls(**values, geometry=geometry)


@dataclass(frozen=True)
class BallMark:
    """The tapped resting ball: saved-frame pixel, preview position and when it was set."""

    frame_x_px: float
    frame_y_px: float
    preview_x_pct: float
    preview_y_pct: float
    set_at: float
    geometry: dict

    @property
    def frame_px(self) -> tuple[float, float]:
        """The ball in saved-frame pixels, where the ball flight searches the resting ball."""
        return (self.frame_x_px, self.frame_y_px)

    def to_dict(self) -> dict:
        """JSON-ready form, as persisted."""
        data = asdict(self)
        for key in ("frame_x_px", "frame_y_px", "preview_x_pct", "preview_y_pct"):
            data[key] = round(data[key], 3)
        return data

    def to_log(self) -> dict:
        """The form the Align view and the session log get: where it was tapped and when."""
        data = self.to_dict()
        del data["geometry"]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> BallMark:
        """Rebuild a persisted ball mark; raises ValueError for anything malformed."""
        values, geometry = _persisted_fields(
            data,
            ("frame_x_px", "frame_y_px", "preview_x_pct", "preview_y_pct", "set_at"),
            "ball mark",
        )
        return cls(**values, geometry=geometry)


def _persisted_fields(data, keys: tuple[str, ...], what: str) -> tuple[dict, dict]:
    """The finite numbers ``keys`` and the geometry key of a persisted mark, or ValueError."""
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be an object")
    try:
        values = {key: float(data[key]) for key in keys}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{what} is malformed: {exc}") from exc
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError(f"{what} holds a non-finite number")
    geometry = data.get("geometry")
    if not isinstance(geometry, dict):
        raise ValueError(f"{what} has no geometry key")
    return values, geometry


def preview_to_frame_px(
    x_pct: float,
    y_pct: float,
    *,
    width: int,
    height: int,
    roll_correction_deg: float,
) -> tuple[float, float]:
    """Preview position (percent of the picture) -> saved-frame pixel coordinates.

    The preview is the saved frame rotated by ``-roll_correction_deg`` about the
    image centre (``CameraCaptureRuntime.capture_preview_jpeg``, OpenCV's
    ``getRotationMatrix2D`` convention); this undoes that rotation. Pixel
    centres sit at integer coordinates, as in the detectors' centroids.
    """
    preview_x = x_pct / 100.0 * width - 0.5
    preview_y = y_pct / 100.0 * height - 0.5
    if abs(roll_correction_deg) <= 1e-6:
        return preview_x, preview_y
    angle = math.radians(-roll_correction_deg)
    cosine, sine = math.cos(angle), math.sin(angle)
    centre_x, centre_y = width / 2.0, height / 2.0
    dx, dy = preview_x - centre_x, preview_y - centre_y
    return (
        cosine * dx - sine * dy + centre_x,
        sine * dx + cosine * dy + centre_y,
    )


def frame_px_to_preview_pct(
    x_px: float,
    y_px: float,
    *,
    width: int,
    height: int,
    roll_correction_deg: float,
) -> tuple[float, float]:
    """Saved-frame pixel -> preview position in percent, the inverse of ``preview_to_frame_px``."""
    preview_x, preview_y = float(x_px), float(y_px)
    if abs(roll_correction_deg) > 1e-6:
        angle = math.radians(roll_correction_deg)
        cosine, sine = math.cos(angle), math.sin(angle)
        centre_x, centre_y = width / 2.0, height / 2.0
        dx, dy = preview_x - centre_x, preview_y - centre_y
        preview_x = cosine * dx - sine * dy + centre_x
        preview_y = sine * dx + cosine * dy + centre_y
    return (preview_x + 0.5) / width * 100.0, (preview_y + 0.5) / height * 100.0


def direction_to_frame_px(  # pylint: disable=too-many-arguments
    azimuth_deg: float,
    elevation_deg: float,
    *,
    intrinsics: CameraIntrinsics,
    horizontal_pixel_sign: float,
    roll_correction_deg: float,
) -> tuple[float, float]:
    """A target's camera-frame direction -> saved-frame pixel, the inverse of the tap mapping.

    Undoes ``target_line_from_tap``: the azimuth and elevation give the derolled
    unit-plane offsets, which are rolled back and projected through the lens
    (``CameraIntrinsics.project``).
    """
    image_x = math.tan(math.radians(azimuth_deg))
    image_z = math.tan(math.radians(elevation_deg)) * math.hypot(image_x, 1.0)
    rolled_x, rolled_z = deroll_normalized_offsets(image_x, image_z, -roll_correction_deg)
    return intrinsics.project(float(rolled_x) / horizontal_pixel_sign, -float(rolled_z))


def target_line_from_tap(  # pylint: disable=too-many-arguments
    x_pct: float,
    y_pct: float,
    *,
    width: int,
    height: int,
    roll_correction_deg: float,
    horizontal_pixel_sign: float,
    intrinsics: CameraIntrinsics,
    geometry: dict,
    now: float | None = None,
) -> TargetLine:
    """The target line through a tapped preview position (see the module docstring)."""
    _check_tap(x_pct, y_pct, "target")
    frame_x, frame_y = preview_to_frame_px(
        float(x_pct),
        float(y_pct),
        width=width,
        height=height,
        roll_correction_deg=roll_correction_deg,
    )
    image_x, image_z = normalized_image_offsets(
        frame_x,
        frame_y,
        focal_px=intrinsics.focal_px,
        image_width_px=width,
        image_height_px=height,
        horizontal_pixel_sign=horizontal_pixel_sign,
        roll_correction_deg=roll_correction_deg,
        intrinsics=intrinsics,
    )
    return TargetLine(
        frame_x_px=float(frame_x),
        frame_y_px=float(frame_y),
        preview_x_pct=float(x_pct),
        preview_y_pct=float(y_pct),
        azimuth_deg=math.degrees(math.atan2(float(image_x), 1.0)),
        elevation_deg=math.degrees(math.atan2(float(image_z), math.hypot(float(image_x), 1.0))),
        set_at=time.time() if now is None else float(now),
        geometry=_json_ready(geometry),
    )


def ball_mark_from_tap(  # pylint: disable=too-many-arguments
    x_pct: float,
    y_pct: float,
    *,
    width: int,
    height: int,
    roll_correction_deg: float,
    geometry: dict,
    now: float | None = None,
) -> BallMark:
    """The resting ball under a tapped preview position, in saved-frame pixels."""
    _check_tap(x_pct, y_pct, "ball")
    frame_x, frame_y = preview_to_frame_px(
        float(x_pct),
        float(y_pct),
        width=width,
        height=height,
        roll_correction_deg=roll_correction_deg,
    )
    return BallMark(
        frame_x_px=float(frame_x),
        frame_y_px=float(frame_y),
        preview_x_pct=float(x_pct),
        preview_y_pct=float(y_pct),
        set_at=time.time() if now is None else float(now),
        geometry=_json_ready(geometry),
    )


def _check_tap(x_pct, y_pct, what: str) -> None:
    """A tap is two numbers within the picture (0 to 100 percent); ValueError otherwise."""
    for name, value in (("x_pct", x_pct), ("y_pct", y_pct)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{what} {name} must be a number")
        if not math.isfinite(value) or not 0.0 <= value <= 100.0:
            raise ValueError(f"{what} {name} must be between 0 and 100 percent")


def _json_ready(value: dict) -> dict:
    """The geometry key as it reads back from JSON (tuples become lists)."""
    return json.loads(json.dumps(value, sort_keys=True))


def resolve_target_line_path() -> Path:
    """``OPENFLIGHT_CAMERA_TARGET_LINE_PATH``, else the user default.

    Tests point the env var at a temp file so they cannot touch
    ``~/.config/openflight/camera-target-line.json``.
    """
    env_path = (os.environ.get(TARGET_LINE_PATH_ENV) or "").strip()
    return Path(env_path).expanduser() if env_path else DEFAULT_TARGET_LINE_PATH


def save_camera_marks(path: Path, target: TargetLine | None, ball: BallMark | None) -> bool:
    """Persist both marks for this device setup; False (and a warning) when it fails.

    Written to a temporary file that then replaces the old one. Without either
    mark the file is deleted.
    """
    if target is None and ball is None:
        delete_camera_marks(path)
        return True
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        payload = {
            "version": CAMERA_MARKS_VERSION,
            "target_line": target.to_dict() if target is not None else None,
            "ball_mark": ball.to_dict() if ball is not None else None,
        }
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
        return True
    except OSError:
        logger.warning("[CAMERA] Could not save the target line to %s", path, exc_info=True)
        return False


def delete_camera_marks(path: Path) -> None:
    """Forget the persisted marks; a missing file is fine."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("[CAMERA] Could not delete the target line %s", path, exc_info=True)


def _parse_camera_marks(payload) -> tuple[TargetLine | None, BallMark | None]:
    """Both marks of a version 1 or 2 file; ValueError for anything malformed."""
    if not isinstance(payload, dict):
        raise ValueError("the target line file must hold an object")
    version = payload.get("version")
    if version == TARGET_LINE_VERSION:
        return TargetLine.from_dict(payload), None
    if version != CAMERA_MARKS_VERSION:
        raise ValueError(
            f"the target line file must be version {TARGET_LINE_VERSION} or {CAMERA_MARKS_VERSION}"
        )
    target, ball = payload.get("target_line"), payload.get("ball_mark")
    return (
        TargetLine.from_dict(target) if target is not None else None,
        BallMark.from_dict(ball) if ball is not None else None,
    )


def _split_geometry(geometry: dict) -> tuple[dict, tuple]:
    """A geometry key's mount part and its (width, height, vertical_offset_px) mode part."""
    mount = {key: value for key, value in geometry.items() if key not in MODE_GEOMETRY_KEYS}
    return mount, tuple(geometry.get(key) for key in MODE_GEOMETRY_KEYS)


def canonical_geometry(geometry: dict) -> dict:
    """The geometry key of the same mount in the canonical 640x400 frame (sensor window 0)."""
    width, height = CANONICAL_FRAME_SIZE
    return _json_ready({**geometry, "width": width, "height": height, "vertical_offset_px": 0})


@dataclass(frozen=True)
class CarriedMarks:
    """Both marks moved into another geometry key, and what could not be moved.

    ``discarded`` names why both marks were dropped (another mount, or a mode
    without a known mapping); ``ball_outside`` is True when only the ball mark
    was dropped because it lies outside the new frame.
    """

    target: TargetLine | None
    ball: BallMark | None
    discarded: str | None = None
    ball_outside: bool = False


def _carry_target(
    target: TargetLine, geometry: dict, intrinsics: CameraIntrinsics | None
) -> TargetLine | None:
    """The same target direction in ``geometry``'s mode; None without a lens model for it."""
    if intrinsics is None:
        return None
    width, height = int(geometry["width"]), int(geometry["height"])
    roll = float(geometry.get("roll_correction_deg", 0.0))
    frame_x, frame_y = direction_to_frame_px(
        target.azimuth_deg,
        target.elevation_deg,
        intrinsics=intrinsics,
        horizontal_pixel_sign=-1.0 if geometry.get("mirror_horizontal") else 1.0,
        roll_correction_deg=roll,
    )
    preview_x, preview_y = frame_px_to_preview_pct(
        frame_x, frame_y, width=width, height=height, roll_correction_deg=roll
    )
    return replace(
        target,
        frame_x_px=float(frame_x),
        frame_y_px=float(frame_y),
        preview_x_pct=float(preview_x),
        preview_y_pct=float(preview_y),
        geometry=geometry,
    )


def _carry_ball(ball: BallMark, shift: tuple[int, int], geometry: dict) -> BallMark | None:
    """The same ball pixel in ``geometry``'s mode; None when it falls outside that frame."""
    width, height = int(geometry["width"]), int(geometry["height"])
    frame_x, frame_y = ball.frame_x_px + shift[0], ball.frame_y_px + shift[1]
    # Pixel centres sit at integer coordinates: the frame spans -0.5 to size - 0.5.
    if not (-0.5 <= frame_x <= width - 0.5 and -0.5 <= frame_y <= height - 0.5):
        return None
    preview_x, preview_y = frame_px_to_preview_pct(
        frame_x,
        frame_y,
        width=width,
        height=height,
        roll_correction_deg=float(geometry.get("roll_correction_deg", 0.0)),
    )
    return replace(
        ball,
        frame_x_px=float(frame_x),
        frame_y_px=float(frame_y),
        preview_x_pct=float(preview_x),
        preview_y_pct=float(preview_y),
        geometry=geometry,
    )


def carry_camera_marks(
    target: TargetLine | None,
    ball: BallMark | None,
    geometry: dict,
    intrinsics: CameraIntrinsics | None,
) -> CarriedMarks:
    """Move both marks into ``geometry`` (another capture mode of the same mount).

    ``intrinsics`` is the lens model of ``geometry``'s mode (``intrinsics.for_mode``).
    A mark already under ``geometry`` stays as it is. The target keeps its
    direction (azimuth, elevation) and gets the pixel it projects to; the ball
    mark is a pixel and moves by the modes' frame shift
    (``capture_runtime.mode_frame_shift``). Another mount, a mode without a
    known mapping or a target without a lens model drop both marks
    (``discarded``); a ball mark outside the new frame drops alone.
    """
    geometry = _json_ready(geometry)
    mount, mode = _split_geometry(geometry)
    carried: dict[str, TargetLine | BallMark | None] = {"target": target, "ball": ball}
    ball_outside = False
    for name, mark in (("target", target), ("ball", ball)):
        if mark is None or mark.geometry == geometry:
            continue
        mark_mount, mark_mode = _split_geometry(mark.geometry)
        shift = (
            mode_frame_shift(mark_mode, mode, rotate_180=bool(mount.get("rotate_180")))
            if mark_mount == mount
            else None
        )
        if shift is None:
            return CarriedMarks(None, None, discarded="camera geometry changed")
        if name == "target":
            carried["target"] = _carry_target(mark, geometry, intrinsics)
            if carried["target"] is None:
                return CarriedMarks(None, None, discarded="no lens calibration for this mode")
        else:
            carried["ball"] = _carry_ball(mark, shift, geometry)
            ball_outside = carried["ball"] is None
    return CarriedMarks(carried["target"], carried["ball"], ball_outside=ball_outside)


def load_camera_marks(
    path: Path, geometry: dict, intrinsics: CameraIntrinsics | None = None
) -> tuple[TargetLine | None, BallMark | None, str | None]:
    """The persisted target and ball mark carried into this geometry, and why there are none.

    The marks are saved in the canonical 640x400 frame where the capture mode
    has a known mapping (else in their own mode) and are carried into
    ``geometry`` by ``carry_camera_marks``; ``intrinsics`` is the lens model of
    ``geometry``'s mode. The reason is None when nothing was saved. An
    unreadable or malformed file, or one holding a mark of another mount or of
    a mode without a known mapping, is reported and deleted, with both marks. A
    ball mark outside this mode's frame is dropped alone, with the reason
    ``BALL_MARK_OUTSIDE_MODE``; the file keeps it for the other modes.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None, None
    except (OSError, json.JSONDecodeError) as exc:
        delete_camera_marks(path)
        return None, None, f"unreadable: {exc}"
    try:
        target, ball = _parse_camera_marks(payload)
    except ValueError as exc:
        delete_camera_marks(path)
        return None, None, str(exc)
    carried = carry_camera_marks(target, ball, geometry, intrinsics)
    if carried.discarded is not None:
        delete_camera_marks(path)
        return None, None, carried.discarded
    return carried.target, carried.ball, BALL_MARK_OUTSIDE_MODE if carried.ball_outside else None


class AngleReference:
    """Session state of the camera angle reference: two offsets, the target line, the ball mark."""

    def __init__(self):
        self.vertical = SessionOffset("vertical")
        # Logged only: an offset cannot correct the compressed TI horizontal.
        self.horizontal = SessionOffset("horizontal", applied=False)
        self.target_line: TargetLine | None = None
        # Where the camera searches the resting ball; never part of the angle maths here.
        self.ball_mark: BallMark | None = None
        self._lock = threading.Lock()

    def reset(self, reason: str, *, axes: tuple[str, ...] = ("vertical", "horizontal")) -> bool:
        """Forget the pairs of ``axes``; True when any offset had pairs."""
        with self._lock:
            changed = False
            for axis in axes:
                changed = getattr(self, axis).reset(reason) or changed
            return changed

    def state(self) -> dict:
        """JSON-ready state for a session-log entry."""
        with self._lock:
            return {
                "vertical": self.vertical.state(),
                "horizontal": self.horizontal.state(),
                "target_line": self.target_line.to_log() if self.target_line else None,
                "ball_mark": self.ball_mark.to_log() if self.ball_mark else None,
            }

    def apply(self, shot, *, vertical_guard: VerticalGuard | None = None) -> bool:
        """Select the shot's launch angles against the camera; True if an offset's validity changed.

        Reads ``iwr6843_vertical_deg``, ``experimental_camera_vertical_deg`` (and
        its confidence), ``iwr6843_horizontal_deg``, the camera start direction
        the fusion selected and the target its ball flight was measured against;
        writes the launch angles it decides and ``shot.angle_reference``.
        ``vertical_guard(angle_deg, shot) -> (plausible, details)`` checks a
        corrected TI vertical before it is shown. A demo shot is left alone.
        """
        if getattr(shot, "demo", False):
            return False
        with self._lock:
            previous = shot.angle_reference or {}
            current = self.target_line.to_log() if self.target_line else None
            stamped = "measured_against" in previous
            measured = previous.get("measured_against") if stamped else current
            target_changed = stamped and _target_set_at(measured) != _target_set_at(current)
            vertical, vertical_changed = self._apply_vertical(shot, vertical_guard)
            horizontal, horizontal_changed = self._apply_horizontal(shot, target_changed)
            target = dict(measured) if isinstance(measured, dict) else None
            if target is not None and isinstance(previous.get("target_line"), dict):
                target.update(previous["target_line"])
            shot.angle_reference = {
                "vertical": vertical,
                "horizontal": horizontal,
                "target_line": target,
            }
            return vertical_changed or horizontal_changed

    def _apply_vertical(self, shot, guard: VerticalGuard | None) -> tuple[dict, bool]:
        radar = shot.iwr6843_vertical_deg
        camera = shot.experimental_camera_vertical_deg
        changed = False
        if radar is not None and camera is not None:
            changed = self.vertical.add(radar, camera)
        corrected = None
        rejected = None
        if camera is not None:
            confidence = shot.experimental_camera_vertical_confidence
            shot.launch_angle_vertical = camera
            shot.launch_angle_vertical_source = "camera"
            shot.launch_angle_vertical_confidence = confidence
            shot.launch_angle_confidence = confidence
            shot.angle_source = "camera"
        elif radar is not None and shot.launch_angle_vertical_source == "radar":
            corrected = self.vertical.correct(radar)
            plausible, details = (
                (True, {}) if corrected is None or guard is None else guard(corrected, shot)
            )
            if corrected is not None and plausible:
                shot.launch_angle_vertical = corrected
                shot.launch_angle_vertical_source = "radar_calibrated"
            elif corrected is not None:
                # The raw TI passed the same guard: keep it as measured.
                rejected = {
                    "reason": CALIBRATED_IMPLAUSIBLE_REASON,
                    "rule": (details or {}).get("rule"),
                }
        return {
            "selected": shot.launch_angle_vertical_source,
            "radar_deg": _rounded(radar),
            "camera_deg": _rounded(camera),
            "corrected_deg": _rounded(corrected),
            "calibration_rejected": rejected,
            "offset": self.vertical.state(),
        }, changed

    def _apply_horizontal(self, shot, target_changed: bool) -> tuple[dict, bool]:
        """Pair the TI azimuth with the camera direction; the shown direction is left alone."""
        radar = shot.iwr6843_horizontal_deg
        camera = (
            shot.experimental_camera_horizontal_deg
            if shot.launch_angle_horizontal_source in CAMERA_SOURCES
            else None
        )
        changed = False
        # A direction measured from another target than the current one would
        # put a pair of the old frame into the new offset.
        if radar is not None and camera is not None and not target_changed:
            changed = self.horizontal.add(radar, camera)
        return {
            "selected": shot.launch_angle_horizontal_source,
            "radar_deg": _rounded(radar),
            "camera_deg": _rounded(camera),
            "corrected_deg": None,
            "target_changed": target_changed,
            "offset": self.horizontal.state(),
        }, changed


def _target_set_at(target: dict | None) -> float | None:
    """When a logged target was set; None for no target."""
    return target.get("set_at") if isinstance(target, dict) else None


def camera_vertical_confidence(confidence_tier: str, depth_source: str | None) -> float | None:
    """Confidence of an accepted camera vertical (see CAMERA_VERTICAL_CONFIDENCE)."""
    if depth_source == "camera_size":
        return CAMERA_ONLY_VERTICAL_CONFIDENCE
    return CAMERA_VERTICAL_CONFIDENCE.get(confidence_tier)


__all__ = [
    "BALL_MARK_OUTSIDE_MODE",
    "CALIBRATED_IMPLAUSIBLE_REASON",
    "CAMERA_ONLY_VERTICAL_CONFIDENCE",
    "CAMERA_SOURCES",
    "CAMERA_VERTICAL_CONFIDENCE",
    "MAX_SPREAD_DEG",
    "MIN_PAIRS",
    "MODE_GEOMETRY_KEYS",
    "WINDOW_PAIRS",
    "AngleReference",
    "BallMark",
    "CarriedMarks",
    "SessionOffset",
    "TargetLine",
    "VerticalGuard",
    "ball_mark_from_tap",
    "camera_vertical_confidence",
    "canonical_geometry",
    "carry_camera_marks",
    "delete_camera_marks",
    "direction_to_frame_px",
    "frame_px_to_preview_pct",
    "load_camera_marks",
    "preview_to_frame_px",
    "resolve_target_line_path",
    "save_camera_marks",
    "target_line_from_tap",
]
