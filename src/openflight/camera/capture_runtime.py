"""High-speed camera capture runtime for offline shot correlation."""

from __future__ import annotations

import json
import logging
import math
import queue
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import numpy as np

from openflight.camera.auto_exposure import (
    AutoExposureDecision,
    AutoExposurePolicy,
    ExposureObservation,
    measure_exposure,
    motion_blur_risk,
    nearly_black,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.tee_anchor import TeeAnchor
from openflight.camera.tee_zone import TeeZone
from openflight.camera.trigger_check import (
    check_ball_departure,
    exposure_timestamps_ns,
)
from openflight.camera.triggered_buffer import (
    CameraFrame,
    TriggeredCapture,
    TriggeredFrameBuffer,
    timing_summary,
    unpack_r8_frame,
    unpack_yuv420_y_plane,
)
from openflight.edge_timing import EdgeTimingLog, edge_ticks
from openflight.gpio_factory import ensure_lgpio_pin_factory

logger = logging.getLogger(__name__)

CameraCaptureStream = Literal["raw", "main-y"]

RASPBERRY_PI_DIST_PACKAGES = Path("/usr/lib/python3/dist-packages")
OV9281_VERTICAL_OFFSET_PATH = Path("/sys/module/ov9282/parameters/strip_y_offset")
AUTO_EXPOSURE_STARTUP_SETTLE_S = 0.3
# The startup calibration only sees the first second after app start (at the
# range: no ball yet, someone in front of the lens) and the light changes
# later (2026-10-06: the sun came out ten minutes in), so the lock is checked
# this often between shots.
AUTO_EXPOSURE_RETRY_S = 30.0
# Consecutive checks a ready lock must look too bright or too dark before it is
# re-measured.
AUTO_EXPOSURE_DRIFT_CHECKS = 2
# A flat frame ("no_image") is retried too: a sensor mode that recovers after a
# restart should not stay withheld for the rest of the session.
_RETRY_STATUSES = frozenset({"lighting_required", "no_image", "unavailable"})
# Blocked gate edges and saved trigger checks wait this long (count) for their
# partner: the gate decides within its deadline (<= 60 ms), the clip is saved
# after the post-trigger frames, so either may come first.
BLOCKED_EDGE_HISTORY = 16
# Both sides see the same edge timestamp (the edge source's float).
BLOCKED_EDGE_MATCH_S = 0.002
# Picamera2 request buffers. Each 640x400 request holds the unpacked raw frame
# (512 KB) plus the YUV420 main stream (384 KB), about 0.9 MB of CMA memory.
# Eight buffers (27.8 ms at 288 fps) ride out a short host stall; more only
# delay drops in a sustained shortfall and lengthen the worst frame delivery.
# A 640x200 request needs half the memory; eight span 14.9 ms at strip-560's
# delivered ~536 fps.
DEFAULT_BUFFER_COUNT = 8
BUFFER_COUNT_RANGE = (4, 32)
# Profile a withdrawn mode falls back to at startup.
FALLBACK_CAPTURE_MODE = {"id": "full-300", "width": 640, "height": 400, "fps": 300}
# Sensor sizes no longer offered: the patched 320x200 register set
# (crop-500/crop-600) delivers flat frames on the Pi, every pixel 16
# (range 2026-11-02, analysis/range-20261102/kamera/bericht-kamera.md section 1).
WITHDRAWN_CAPTURE_SIZES = frozenset({(320, 200)})


# Where each capture mode's saved frame sits in the full 640x400 frame, the
# canonical frame the lens calibration and the persisted camera marks use.
# Saved pixel (u, v) of a mode is canonical pixel (u + dx, v + dy + s) when the
# strip offset applies, s = strip_y_offset in output rows (positive = lower on
# the sensor, as the driver applies it: ``driver_strip_offset``), else
# (u + dx, v + dy). The runtime rotates a 180-degree mount's frames in software
# (``unpack_r8_frame``), which flips the window: there the saved pixel is
# canonical (u + W - w - dx, v + H - h - dy - s), 100 - s rows for the strip.
# Kept with drivers/ov9281/ov9282-high-speed.patch:
# every mode reads the sensor binned 2:1 (0x3814/0x3815) with the same ISP
# offset, so fx, fy and the normalized distortion hold and only the principal
# point moves; dy is the native Y_ADDR_START (0x3802/0x3803) halved, and
# ov9282_strip_y_offset() shifts the 640x200 strip by s but not 640x400.
#   640x400: window start (0, 0).
#   640x200: mode_640x200_regs 0x3802/0x3803 = 0x00c8, native row 200 -> dy = 100
#            (confirmed on 2026-11-02 frames: row shift 100, NCC 0.95-0.97).
# 640x100 and 320x200 are not mapped: their marks and tee zone are cleared or
# ignored on a switch, as before. 320x200 is no longer offered at all
# (``WITHDRAWN_CAPTURE_SIZES``).
CANONICAL_FRAME_SIZE = (640, 400)
CAPTURE_MODE_FRAME_OFFSETS: dict[tuple[int, int], tuple[int, int, bool]] = {
    (640, 400): (0, 0, False),
    (640, 200): (0, 100, True),
}
# The modes ov9282_strip_y_offset() moves by strip_y_offset, with its clamp in
# output rows: (800 - native crop height) / 4 for the strips, +/-75 for the
# 320x200 crop. Every other mode ignores the parameter. The 320x200 entry
# describes the driver, not an offer: a value it left in the parameter still
# moves the 640x200 strip.
DRIVER_STRIP_OFFSET_LIMITS: dict[tuple[int, int], int] = {
    (640, 200): 100,
    (640, 100): 150,
    (320, 200): 75,
}


def driver_strip_offset(width: int, height: int, strip_y_offset: int) -> int:
    """The sensor-window shift (output rows) the driver applies to a mode for a parameter value."""
    limit = DRIVER_STRIP_OFFSET_LIMITS.get((width, height))
    if limit is None:
        return 0
    return max(-limit, min(limit, int(strip_y_offset)))


def canonical_frame_offset(
    width, height, vertical_offset_px, *, rotate_180: bool = False
) -> tuple[int, int] | None:
    """(dx, dy) adding a mode's saved pixel to its canonical 640x400 pixel, None if unmapped.

    ``rotate_180``: the saved frames are rotated in software, so the window
    sits mirrored in the (equally rotated) canonical frame.
    """
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (width, height, vertical_offset_px)
    ):
        return None
    entry = CAPTURE_MODE_FRAME_OFFSETS.get((width, height))
    if entry is None:
        return None
    dx, dy, strip_offset_applies = entry
    dy += vertical_offset_px if strip_offset_applies else 0
    if rotate_180:
        canonical_width, canonical_height = CANONICAL_FRAME_SIZE
        return canonical_width - width - dx, canonical_height - height - dy
    return dx, dy


def mode_frame_shift(
    source: tuple, target: tuple, *, rotate_180: bool = False
) -> tuple[int, int] | None:
    """Pixel shift from one (width, height, vertical_offset_px) mode to another.

    A saved pixel (u, v) of ``source`` is (u + dx, v + dy) in ``target``, both
    saved with the mount's ``rotate_180``. The same mode is always (0, 0);
    otherwise both must be mapped, else None.
    """
    if tuple(source) == tuple(target):
        return 0, 0
    source_offset = canonical_frame_offset(*source, rotate_180=rotate_180)
    target_offset = canonical_frame_offset(*target, rotate_180=rotate_180)
    if source_offset is None or target_offset is None:
        return None
    return source_offset[0] - target_offset[0], source_offset[1] - target_offset[1]


def supported_capture_modes(sensor_modes: list[dict]) -> list[dict]:
    """Offer bounded R8 profiles only when the installed driver supports them.

    strip-560 is the 640x200 strip at the highest rate the patched driver
    allows (about 589 fps nominal from VTS/HTS); like every mode it delivers
    about 4.2 % below the request (2026-11-02: strip-500 479.1 fps, full-300
    287.9 fps). Mappings key on the geometry, so it shares strip-500's
    (``CAPTURE_MODE_FRAME_OFFSETS``, ``DRIVER_STRIP_OFFSET_LIMITS``).
    """
    profiles = [
        dict(FALLBACK_CAPTURE_MODE),
        {"id": "strip-500", "width": 640, "height": 200, "fps": 500},
        {"id": "strip-560", "width": 640, "height": 200, "fps": 560},
    ]
    return [
        profile
        for profile in profiles
        if any(
            tuple(mode.get("size", ())) == (profile["width"], profile["height"])
            and str(mode.get("format")) == "R8"
            and float(mode.get("fps", 0)) >= profile["fps"]
            for mode in sensor_modes
        )
    ]


def without_withdrawn_capture_mode(settings: CameraCaptureSettings) -> CameraCaptureSettings:
    """Move a stored withdrawn mode (320x200, crop-500/crop-600) to full-300, with a log line."""
    if (settings.width, settings.height) not in WITHDRAWN_CAPTURE_SIZES:
        return settings
    fallback = replace(
        settings,
        width=FALLBACK_CAPTURE_MODE["width"],
        height=FALLBACK_CAPTURE_MODE["height"],
        fps=float(FALLBACK_CAPTURE_MODE["fps"]),
    )
    logger.warning(
        "[CAMERA] Capture mode %dx%d %.0ffps is withdrawn (the 320x200 sensor mode delivers "
        "flat frames); starting %s (%dx%d %.0ffps) instead",
        settings.width,
        settings.height,
        settings.fps,
        FALLBACK_CAPTURE_MODE["id"],
        fallback.width,
        fallback.height,
        fallback.fps,
    )
    return fallback


def vertical_crop_limits(width: int, height: int) -> dict[str, int] | None:
    """Return safe output-pixel crop limits for a supported sensor mode."""
    if (width, height) == (320, 200):
        # Keep five pixels of margin around the driver's centered +/-75 limit.
        return {"min_px": -70, "max_px": 70, "step_px": 10}
    return None


def capture_exposure_status(capture: TriggeredCapture) -> dict:
    """Rate saved pixels without reusing startup state or changing controls.

    Sample the prebuffer, trigger boundary and postbuffer. Use the worst
    sampled observation so averaging cannot hide clipping after a light change.

    ``analysis_eligible`` (club analysis) needs a good or marginal exposure.
    ``ball_analysis_eligible`` (the ball flight) also takes a too-bright clip
    (backlit ball silhouettes), and a too-dark one unless a sampled frame is
    nearly black (``auto_exposure.nearly_black``: p90 <= 12 and >= 90 % of the
    impact area <= 12): the disappeared-ball locator's own relative gates decide
    there. ``ball_analysis_relaxed`` marks a clip only that dark rule admits; the
    shot path runs its ball flight without the legacy fallback and no club data.
    A clip with a flat sampled frame (``frame_flat``, status ``no_image``) is
    eligible for neither: it carries no picture, so the dark rule never applies.
    2026-11-02: the daylight lock (350 us / gain 6) sat on the ``p90 < 75`` edge,
    and #3/#4 (p90 74/73) lost their camera start direction to it.
    """
    count = len(capture.frames)
    indices = (
        sorted(
            set(np.linspace(0, count - 1, min(5, count), dtype=int).tolist())
            | {max(0, capture.pre_trigger_count - 1), min(count - 1, capture.pre_trigger_count)}
        )
        if count
        else []
    )
    observations = [measure_exposure(capture.frames[i].image) for i in indices]
    severity = {
        "good": 0,
        "marginal": 1,
        "too_dark": 2,
        "too_bright": 2,
        "flat": 3,
        "unavailable": 3,
    }
    observation = (
        max(observations, key=lambda item: severity[item.status])
        if observations
        else (measure_exposure(np.empty((0, 0))))
    )
    known_controls = bool(count) and all(
        frame.exposure_us > 0 and math.isfinite(frame.analogue_gain) and frame.analogue_gain > 0
        for frame in capture.frames
    )
    maximum_exposure = max((frame.exposure_us for frame in capture.frames), default=0)
    risk = motion_blur_risk(maximum_exposure)
    eligible = observation.acceptable and known_controls and risk != "high"
    if not known_controls:
        message = "Capture exposure metadata is unavailable; camera analysis withheld"
    elif observation.flat:
        message = "Capture frames are flat; the sensor mode delivered no image"
    elif risk == "high":
        message = "Capture shutter duration risks motion blur; use a shorter exposure"
    elif not observation.acceptable:
        message = "Capture lighting is unsuitable; recheck camera exposure before the next shot"
    else:
        message = "Saved capture exposure is usable"
    if eligible:
        status = "ready"
    elif observation.flat:
        status = "no_image"
    else:
        status = "lighting_required"
    result = AutoExposureDecision(
        status=status,
        analysis_eligible=eligible,
        message=message,
        observation=observation,
        motion_blur_risk=risk,
    ).to_dict()
    ball_gate_open = known_controls and risk != "high"
    # Backlit ball silhouettes still require local contrast/trajectory checks.
    ball_eligible = ball_gate_open and observation.status in {"good", "marginal", "too_bright"}
    # A flat frame is no image, not a dark one: at its one grey level (16 on
    # 2026-11-02, p90 > 12) it is not "nearly black", so the dark rule refuses
    # it by name. The worst observation is then "flat" or "unavailable" as well (both
    # outrank "too_dark"); this check does not lean on that ordering.
    relaxed = (
        ball_gate_open
        and observation.status == "too_dark"
        and not any(item.flat for item in observations)
        and not any(nearly_black(item.p90, item.dark_pct) for item in observations)
    )
    result.update(
        evaluation_source="capture_frames",
        sampled_frames=indices,
        maximum_exposure_us=maximum_exposure,
        frame_flat=observation.flat,
        ball_analysis_eligible=bool(ball_eligible or relaxed),
        ball_analysis_relaxed=bool(relaxed),
    )
    return result


@dataclass(frozen=True)
class CameraCaptureSettings:
    """Camera ring-buffer settings used for offline capture."""

    width: int = 640
    height: int = 400
    fps: float = 300.0
    pre_ms: float = 150.0
    post_ms: float = 50.0
    exposure_us: int = 1000
    gain: float = 4.0
    stream: CameraCaptureStream = "raw"
    rotate_180: bool = False
    mirror_horizontal: bool = False
    roll_correction_deg: float = 0.0
    scaler_crop: tuple[int, int, int, int] | None = None
    gpio_pin: int = 17
    match_tolerance_s: float = 0.05
    auto_exposure: bool = True
    auto_exposure_state_path: Path | None = None
    buffer_count: int = DEFAULT_BUFFER_COUNT

    def __post_init__(self) -> None:
        low, high = BUFFER_COUNT_RANGE
        if not low <= self.buffer_count <= high:
            raise ValueError(f"camera buffer count must be {low}-{high}, got {self.buffer_count}")

    @property
    def pre_frames(self) -> int:
        """Frames retained before the sound trigger."""
        return math.ceil(self.pre_ms * self.fps / 1000.0)

    @property
    def post_frames(self) -> int:
        """Frames retained after the sound trigger."""
        return math.ceil(self.post_ms * self.fps / 1000.0)


@dataclass(frozen=True)
class SavedCameraCapture:
    """A persisted camera capture and its timing metadata."""

    sequence: int
    trigger_timestamp: float
    completed_timestamp: float
    path: Path
    metadata: dict
    error: str | None = None

    @property
    def valid(self) -> bool:
        """Whether the capture was saved successfully."""
        return self.error is None


def parse_scaler_crop(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse a Picamera2 ScalerCrop tuple."""
    if value is None:
        return None
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise ValueError("ScalerCrop must be X,Y,W,H")
    try:
        x, y, width, height = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError("ScalerCrop must contain integers") from exc
    if width <= 0 or height <= 0:
        raise ValueError("ScalerCrop width and height must be positive")
    return x, y, width, height


def _save_pgm(path: Path, image: np.ndarray) -> None:
    """Save a dependency-free grayscale preview."""
    with path.open("wb") as handle:
        handle.write(f"P5\n{image.shape[1]} {image.shape[0]}\n255\n".encode("ascii"))
        handle.write(image.tobytes())


def ensure_picamera2_import_path() -> bool:
    """Expose Raspberry Pi OS camera packages when running inside uv's venv."""
    path = str(RASPBERRY_PI_DIST_PACKAGES)
    if path in sys.path:
        return True
    if not RASPBERRY_PI_DIST_PACKAGES.exists():
        return False
    sys.path.append(path)
    return True


def _pop_near(entries: deque, timestamp: float):
    """Remove and return the first ``(timestamp, payload)`` entry of the same edge, else None."""
    for entry in entries:
        if abs(entry[0] - timestamp) <= BLOCKED_EDGE_MATCH_S:
            entries.remove(entry)
            return entry
    return None


class CameraCaptureRuntime:
    """Maintain a high-speed camera ring and save clips on sound-trigger edges.

    A clip's disk write can be deferred while another device streams a dump
    (``set_write_deferral``): concurrent writes during a TI dump correlate
    with lost USB bytes. At most ``MAX_DEFERRED_CLIPS`` clips are held back
    for at most ``MAX_WRITE_DEFERRAL_S``, and a shot waiting for its clip in
    ``capture_for_shot`` overrides the deferral.
    """

    MAX_DEFERRED_CLIPS = 3
    MAX_WRITE_DEFERRAL_S = 15.0

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        output_dir: str | Path,
        settings: CameraCaptureSettings | None = None,
        button_factory: Callable | None = None,
        use_gpio_trigger: bool = True,
        vertical_offset_path: str | Path = OV9281_VERTICAL_OFFSET_PATH,
        edge_timing: EdgeTimingLog | None = None,
        tee_anchor: TeeAnchor | None = None,
    ):
        self.output_dir = Path(output_dir).expanduser()
        self.settings = settings or CameraCaptureSettings()
        self._button_factory = button_factory
        self._use_gpio_trigger = use_gpio_trigger
        # Kernel vs callback time of the camera's own GPIO edges (shadow log);
        # only used when the camera owns the sound input (use_gpio_trigger).
        self.edge_timing = edge_timing
        self._vertical_offset_path = Path(vertical_offset_path)
        self._auto_exposure_state_path = (
            Path(self.settings.auto_exposure_state_path).expanduser()
            if self.settings.auto_exposure_state_path is not None
            else None
        )
        self._last_persisted_controls: tuple[int, float] | None = None
        self._restore_auto_exposure_controls()
        self._camera = None
        self._capture_modes: list[dict] = []
        self._button = None
        self._ring = TriggeredFrameBuffer(self.settings.pre_frames, self.settings.post_frames)
        self._running = False
        self._sequence = 0
        self._worker: threading.Thread | None = None
        self._ready: queue.Queue[TriggeredCapture | None] = queue.Queue()
        self._captures: list[SavedCameraCapture] = []
        self._condition = threading.Condition()
        self._trigger_epochs: queue.Queue[float] = queue.Queue()
        self._trigger_auto_exposure: queue.Queue[dict] = queue.Queue()
        self._camera_control_lock = threading.Lock()
        self._trigger_exposure_lock = threading.Lock()
        self._reconfigure_lock = threading.Lock()
        self._auto_exposure_policy = AutoExposurePolicy(fps=self.settings.fps)
        self._auto_exposure_stop = threading.Event()
        self._auto_exposure_lock = threading.Lock()
        self._auto_exposure_decision = AutoExposureDecision(
            status="unavailable",
            analysis_eligible=not self.settings.auto_exposure,
            message="Waiting for automatic exposure calibration",
            observation=ExposureObservation(
                sample_available=False,
                status="unavailable",
                recommendation="hold",
                message="Waiting for a camera frame",
            ),
            motion_blur_risk=motion_blur_risk(self.settings.exposure_us),
        )
        self._auto_exposure_last_check_epoch: float | None = None
        self._auto_exposure_last_adjustment_epoch: float | None = None
        self._auto_exposure_capture_deferred = False
        self._exposure_recalibrating = False
        self._exposure_watch_stop: threading.Event | None = None
        self._exposure_watch_thread: threading.Thread | None = None
        self._defer_writes_while: Callable[[], bool] | None = None
        self._flush_requested = threading.Event()
        # Injectable so offline replays drive the same anchor through this runtime.
        self._tee_anchor = tee_anchor if tee_anchor is not None else TeeAnchor()
        self._tee_zone: TeeZone | None = None
        self._tee_zone_key: tuple | None = None
        self._tee_zone_mode: tuple[int, int, int] | None = None
        self._ball_mark_source: Callable[[], tuple[float, float] | None] | None = None
        # Sensor window offset (OV9281 320x200 strip): part of the image geometry.
        self._vertical_offset_px = self.vertical_crop_status()["vertical_offset_px"]
        self._blocked_lock = threading.Lock()
        self._blocked_edges: deque[tuple[float, None]] = deque(maxlen=BLOCKED_EDGE_HISTORY)
        self._recent_checks: deque[tuple[float, dict]] = deque(maxlen=BLOCKED_EDGE_HISTORY)

    @property
    def running(self) -> bool:
        """Whether the camera delivers frames."""
        return self._running

    def live_frames_after(self, after_ns: int, timeout_s: float = 0.0) -> tuple[CameraFrame, ...]:
        """Live frames delivered after ``after_ns`` (monotonic ns), waiting up to ``timeout_s``."""
        if not self._running:
            return ()
        return self._ring.frames_after(after_ns, timeout_s)

    def trigger_check_anchor(self) -> ReferenceBall | None:
        """The confirmed session tee anchor the live gate may block on, else None."""
        return self._tee_anchor.gate_anchor()

    def _image_geometry(self) -> tuple:
        """What fixes saved-frame pixel coordinates (exposure and gain do not)."""
        settings = self.settings
        return (
            settings.width,
            settings.height,
            settings.scaler_crop,
            settings.rotate_180,
            settings.mirror_horizontal,
            self._vertical_offset_px,
        )

    def _mount_geometry(self) -> tuple:
        """The part of the image geometry a capture-mode switch never changes."""
        settings = self.settings
        return (settings.scaler_crop, settings.rotate_180, settings.mirror_horizontal)

    def _capture_mode_geometry(self) -> tuple[int, int, int]:
        """Width, height and sensor window of the current mode (``mode_frame_shift``)."""
        return (self.settings.width, self.settings.height, self._vertical_offset_px)

    def set_tee_zone(self, zone: TeeZone | None, *, vertical_offset_px: int | None = None) -> None:
        """Restrict the trigger check's ball search to the hitting zone of this mount.

        ``zone`` is in the pixels of its own ``image_width_px`` x
        ``image_height_px`` mode at ``vertical_offset_px`` (default: the current
        sensor window when that is the current size, else 0). It holds for this
        mount; ``active_tee_zone`` moves it into every mapped capture mode
        (``CAPTURE_MODE_FRAME_OFFSETS``), clipped to the frame. In an unmapped
        mode the check runs without it (and no anchor can form).
        """
        if zone is None:
            self._tee_zone = None
            self._tee_zone_key = None
            self._tee_zone_mode = None
            return
        size = (zone.image_width_px, zone.image_height_px)
        if vertical_offset_px is None:
            same_size = size == (self.settings.width, self.settings.height)
            vertical_offset_px = self._vertical_offset_px if same_size else 0
        self._tee_zone = zone
        self._tee_zone_key = self._mount_geometry()
        self._tee_zone_mode = (*size, int(vertical_offset_px))
        active = self.active_tee_zone()
        if active is None:
            logger.warning(
                "[CAMERA] Tee zone for %dx%d ignored at %dx%d (no known mapping)",
                zone.image_width_px,
                zone.image_height_px,
                self.settings.width,
                self.settings.height,
            )
            return
        logger.info(
            "[CAMERA] Tee zone x %d-%d, y %d-%d (geometric tee %.0f,%.0f, ball %.1f px)",
            active.x0,
            active.x1,
            active.y0,
            active.y1,
            active.tee_x,
            active.tee_y,
            active.diameter_px,
        )

    def active_tee_zone(self) -> TeeZone | None:
        """The hitting zone moved into the current capture mode, None when it has no mapping."""
        zone, mode = self._tee_zone, self._tee_zone_mode
        if zone is None or mode is None or self._tee_zone_key != self._mount_geometry():
            return None
        current = self._capture_mode_geometry()
        if mode == current:
            return zone
        shift = mode_frame_shift(mode, current, rotate_180=self.settings.rotate_180)
        if shift is None:
            return None
        return zone.shifted(*shift, self.settings.width, self.settings.height)

    def tee_anchor_status(self) -> dict:
        """Tee anchor diagnostics plus the active hitting zone."""
        zone = self.active_tee_zone()
        return {
            **self._tee_anchor.diagnostics(),
            "zone": zone.to_dict() if zone is not None else None,
        }

    def note_accepted_shot(self, capture: SavedCameraCapture | None, *, shot_number=None):
        """Teach the tee anchor from the clip of a shot the OPS accepted; never raises.

        The event is added to ``capture.metadata["tee_anchor"]`` (and so to the
        session log's camera_capture entry) and returned.
        """
        if capture is None or not capture.valid:
            return None
        try:
            event = self._tee_anchor.observe_shot(
                capture.metadata.get("trigger_check"), shot=shot_number
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[CAMERA] Tee anchor update failed: %s", exc, exc_info=True)
            return None
        capture.metadata["tee_anchor"] = event
        return event

    def note_blocked_edge(self, edge_timestamp: float) -> dict | None:
        """The camera gate kept this sound edge from the OPS; never raises.

        The edge's clip (``note_trigger_check``, before or after this call)
        then tells the tee anchor whether a ball left away from the anchor,
        which proves the block wrong (``TeeAnchor.observe_blocked_edge``).
        Returns the anchor event once both are known, else None.
        """
        edge = float(edge_timestamp)
        with self._blocked_lock:
            saved = _pop_near(self._recent_checks, edge)
            if saved is None:
                self._blocked_edges.append((edge, None))
                return None
        return self._learn_from_blocked_edge(edge, saved[1])

    def note_trigger_check(self, trigger_epoch: float, check: dict) -> dict | None:
        """Pair a saved clip's trigger check with a blocked edge (``note_blocked_edge``).

        Called for every saved clip; returns the anchor event when the clip's
        edge was blocked and its check proves the block wrong, else None.
        """
        with self._blocked_lock:
            if _pop_near(self._blocked_edges, trigger_epoch) is None:
                self._recent_checks.append((float(trigger_epoch), check))
                return None
        return self._learn_from_blocked_edge(float(trigger_epoch), check)

    def _learn_from_blocked_edge(self, edge: float, check: dict) -> dict | None:
        try:
            return self._tee_anchor.observe_blocked_edge(check, edge=edge)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[CAMERA] Tee anchor blocked-edge update failed: %s", exc, exc_info=True)
            return None

    def set_ball_mark_source(
        self, provider: Callable[[], tuple[float, float] | None] | None
    ) -> None:
        """Where the trigger check reads the tapped resting ball (frame px), None for no mark.

        ``provider`` is asked once per saved clip (the server's current ball
        mark in the current mode's pixels, which it moves into the new mode or
        clears on a geometry change, ``angle_reference.carry_camera_marks``);
        with a mark the check searches the ball only in the window around it
        (``trigger_check``).
        """
        self._ball_mark_source = provider

    def _ball_mark_px(self, sequence: int) -> tuple[float, float] | None:
        """The current ball mark for clip ``sequence``; None without one or on an error."""
        provider = self._ball_mark_source
        if provider is None:
            return None
        try:
            mark = provider()
            return None if mark is None else (float(mark[0]), float(mark[1]))
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[CAMERA] Trigger check #%d: ball mark unavailable: %s", sequence, exc)
            return None

    def set_write_deferral(self, predicate: Callable[[], bool] | None) -> None:
        """Hold clip disk writes back while ``predicate()`` is true."""
        self._defer_writes_while = predicate

    def _wait_for_write_slot(self, sequence: int) -> None:
        predicate = self._defer_writes_while
        if predicate is None:
            return
        started = time.monotonic()
        while self._running and time.monotonic() - started < self.MAX_WRITE_DEFERRAL_S:
            if self._flush_requested.is_set() or 1 + self._ready.qsize() > self.MAX_DEFERRED_CLIPS:
                break
            try:
                if not predicate():
                    break
            except Exception:  # pylint: disable=broad-except
                logger.warning("[CAMERA] Write deferral check failed", exc_info=True)
                break
            self._flush_requested.wait(0.02)
        waited = time.monotonic() - started
        if waited >= 0.02:
            logger.info("[CAMERA] Capture #%d write deferred %.2fs for a TI dump", sequence, waited)

    def start(self) -> None:
        """Start the camera and, optionally, the GPIO edge listener."""
        if self._running:
            return
        ensure_picamera2_import_path()
        try:
            from picamera2 import Picamera2  # pylint: disable=import-error,import-outside-toplevel
        except ImportError as exc:
            raise RuntimeError("picamera2 is required for --camera-capture") from exc

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._vertical_offset_px = self.vertical_crop_status()["vertical_offset_px"]
        self._camera = Picamera2()
        self._capture_modes = supported_capture_modes(getattr(self._camera, "sensor_modes", []))
        frame_duration_us = round(1_000_000 / self.settings.fps)
        config = self._camera.create_video_configuration(
            main={"size": (self.settings.width, self.settings.height), "format": "YUV420"},
            raw={"size": (self.settings.width, self.settings.height), "format": "R8"},
            controls={
                "AeEnable": False,
                "ExposureTime": self.settings.exposure_us,
                "AnalogueGain": self.settings.gain,
                "FrameDurationLimits": (frame_duration_us, frame_duration_us),
            },
            buffer_count=self.settings.buffer_count,
            display=None,
            encode=None,
        )
        try:
            self._camera.configure(config)
        except Exception:
            self._camera.close()
            self._camera = None
            raise
        if self.settings.scaler_crop is not None:
            self._camera.set_controls({"ScalerCrop": self.settings.scaler_crop})
        self._camera.post_callback = self._on_frame
        self._running = True
        self._worker = threading.Thread(
            target=self._save_loop,
            name="camera-capture-save",
            daemon=True,
        )
        self._worker.start()
        try:
            self._camera.start()
            self._wait_for_prebuffer()
            if self.settings.auto_exposure:
                self._start_auto_exposure()
                self._refill_locked_exposure_prebuffer()
                self._start_exposure_watch()
            if self._use_gpio_trigger:
                self._start_gpio_trigger()
        except Exception:
            self.stop()
            raise
        logger.info(
            "[CAMERA] Capture armed at %dx%d %.1ffps (%s, %d pre/%d post, %d buffers)",
            self.settings.width,
            self.settings.height,
            self.settings.fps,
            self.settings.stream,
            self.settings.pre_frames,
            self.settings.post_frames,
            self.settings.buffer_count,
        )

    def stop(self) -> None:
        """Stop camera capture and release hardware resources."""
        self._running = False
        self._auto_exposure_stop.set()
        if self._exposure_watch_stop is not None:
            # No join: _reconfigure_capture stops while holding the trigger
            # lock that a watch cycle may be waiting for.
            self._exposure_watch_stop.set()
        if self._button is not None:
            self._button.close()
            self._button = None
            if self.edge_timing is not None:
                self.edge_timing.log_summary(f"BCM{self.settings.gpio_pin} (camera trigger)")
        if self._camera is not None:
            try:
                self._camera.stop()
                self._camera.close()
            finally:
                self._camera = None
        self._ready.put(None)
        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None

    def capture_preview_jpeg(self, quality: int = 80) -> bytes | None:
        """Encode the latest rolling-buffer frame as a preview JPEG.

        Reusing the compact raw frame avoids a second capture request and
        remains reliable in high-FPS modes where the processed YUV companion
        stream may not produce usable pixels.
        """
        if not self._running or self._camera is None:
            return None
        try:
            import cv2  # pylint: disable=import-error,import-outside-toplevel

            frame = self._ring.latest_frame
            if frame is None:
                return None
            image = np.ascontiguousarray(frame.image)
            if abs(self.settings.roll_correction_deg) > 1e-6:
                height, width = image.shape
                transform = cv2.getRotationMatrix2D(
                    (width / 2.0, height / 2.0),
                    -self.settings.roll_correction_deg,
                    1.0,
                )
                image = cv2.warpAffine(
                    image,
                    transform,
                    (width, height),
                    flags=cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0,
                )
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
            return encoded.tobytes() if ok else None
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[CAMERA] Preview capture failed", exc_info=True)
            return None

    def exposure_quality(self) -> dict:
        """Rate exposure in the center-lower hitting zone of the latest frame."""
        frame = self._ring.latest_frame
        image = frame.image if frame is not None else np.asarray([])
        return measure_exposure(image).to_dict()

    @property
    def camera_analysis_eligible(self) -> bool:
        """Whether current lighting permits camera-derived shot metrics."""
        if not self.settings.auto_exposure:
            return self.auto_exposure_status()["analysis_eligible"]
        with self._auto_exposure_lock:
            return (
                self._auto_exposure_decision.analysis_eligible and not self._exposure_recalibrating
            )

    def auto_exposure_status(self) -> dict:
        """Return controller state for diagnostics and the operator UI."""
        if not self.settings.auto_exposure:
            frame = self._ring.latest_frame
            observation = measure_exposure(frame.image if frame is not None else np.asarray([]))
            risk = motion_blur_risk(self.settings.exposure_us)
            if observation.acceptable:
                status = "ready"
            elif observation.flat:
                status = "no_image"
            else:
                status = "lighting_required"
            return {
                **AutoExposureDecision(
                    status=status,
                    analysis_eligible=observation.acceptable and risk != "high",
                    message="Manual exposure; image quality is still checked",
                    observation=observation,
                    motion_blur_risk=risk,
                ).to_dict(),
                "enabled": False,
                "frame_flat": observation.flat,
                "exposure_us": self.settings.exposure_us,
                "gain": self.settings.gain,
            }
        with self._auto_exposure_lock:
            payload = self._auto_exposure_decision.to_dict()
            payload.update(
                {
                    "enabled": self.settings.auto_exposure,
                    "frame_flat": self._auto_exposure_decision.observation.flat,
                    "capture_deferred": self._auto_exposure_capture_deferred,
                    "last_check_timestamp": self._auto_exposure_last_check_epoch,
                    "last_adjustment_timestamp": self._auto_exposure_last_adjustment_epoch,
                    "recalibrating": self._exposure_recalibrating,
                }
            )
            if self._exposure_recalibrating:
                payload["analysis_eligible"] = False
        payload["exposure_us"] = self.settings.exposure_us
        payload["gain"] = self.settings.gain
        return payload

    def update_exposure_mode(self, *, automatic: bool, exposure_us=None, gain=None) -> dict:
        """Rearm with manual controls or rerun the existing automatic calibration."""
        if not isinstance(automatic, bool):
            raise ValueError("automatic exposure must be true or false")
        if automatic and (exposure_us is not None or gain is not None):
            raise ValueError("manual controls cannot be combined with automatic exposure")
        proposed = replace(self.settings, auto_exposure=automatic)
        if not automatic:
            exposure_us = self.settings.exposure_us if exposure_us is None else exposure_us
            gain = self.settings.gain if gain is None else gain
            limit = min(1250, round(1_000_000 / self.settings.fps) - 1)
            if (
                isinstance(exposure_us, bool)
                or not isinstance(exposure_us, int)
                or not 50 <= exposure_us <= limit
            ):
                raise ValueError(f"manual exposure must be between 50 and {limit} microseconds")
            if (
                isinstance(gain, bool)
                or not isinstance(gain, (int, float))
                or not math.isfinite(gain)
                or not 1 <= gain <= 20
            ):
                raise ValueError("manual gain must be between 1 and 20")
            proposed = replace(proposed, exposure_us=exposure_us, gain=float(gain))
        self._reconfigure_capture(proposed)
        return {
            "auto_exposure_enabled": self.settings.auto_exposure,
            "exposure_us": self.settings.exposure_us,
            "gain": self.settings.gain,
        }

    def _current_capture_mode_id(self) -> str | None:
        """The offered profile matching the running settings, None for a custom one."""
        return next(
            (
                mode["id"]
                for mode in self._capture_modes
                if (mode["width"], mode["height"], mode["fps"])
                == (self.settings.width, self.settings.height, self.settings.fps)
            ),
            None,
        )

    def capture_mode_status(self) -> dict:
        """Report selectable profiles, the actual ring-buffer timing and a flat image."""
        return {
            "capture_mode": self._current_capture_mode_id(),
            "capture_modes": self._capture_modes,
            "width": self.settings.width,
            "height": self.settings.height,
            "fps": self.settings.fps,
            "pre_frames": self.settings.pre_frames,
            "post_frames": self.settings.post_frames,
            "delivered_fps": self._ring.measured_fps,
            "frame_flat": self.auto_exposure_status()["frame_flat"],
        }

    def update_capture_mode(self, mode_id: str) -> dict:
        """Change sensor mode without altering mount geometry or buffer duration."""
        mode = next((mode for mode in self._capture_modes if mode["id"] == mode_id), None)
        if not isinstance(mode_id, str) or mode is None:
            raise ValueError("camera mode is not supported by the installed sensor driver")
        proposed = replace(
            self.settings, width=mode["width"], height=mode["height"], fps=mode["fps"]
        )
        if proposed != self.settings:
            self._reconfigure_capture(proposed)
        return self.capture_mode_status()

    def _reconfigure_capture(self, proposed: CameraCaptureSettings) -> None:
        """Serialize rearming and restore the previous mode if it fails."""
        with self._reconfigure_lock:
            with self._trigger_exposure_lock:
                if not self._running:
                    raise RuntimeError("camera capture is not running")
                if self._ring.capture_busy:
                    raise RuntimeError("camera is capturing a shot; try again after the replay")
                if self._ready.unfinished_tasks:
                    raise RuntimeError("camera is saving a shot; try again after the replay")
                previous = self.settings
                previous_geometry = self._image_geometry()
                self.stop()
            self.settings = proposed
            if self._image_geometry() != previous_geometry:
                self._tee_anchor.reset("capture geometry changed")
            self._reset_capture_state()
            try:
                self.start()
            except Exception:
                logger.exception("[CAMERA] Restart failed; restoring previous camera settings")
                self.stop()
                self.settings = previous
                self._reset_capture_state()
                self.start()
                raise

    def update_image_controls(self, *, exposure_us: int, gain: float) -> dict:
        """Apply exposure and gain without stopping the rolling buffer."""
        exposure_us = int(exposure_us)
        gain = float(gain)
        frame_period_us = round(1_000_000 / self.settings.fps)
        if exposure_us <= 0:
            raise ValueError("camera exposure must be positive")
        if exposure_us >= frame_period_us:
            raise ValueError(
                f"camera exposure must be shorter than the {frame_period_us}us frame period"
            )
        if gain <= 0:
            raise ValueError("camera gain must be positive")
        if not self._running or self._camera is None:
            raise RuntimeError("camera capture is not running")

        with self._camera_control_lock:
            self._camera.set_controls(
                {
                    "ExposureTime": exposure_us,
                    "AnalogueGain": gain,
                }
            )
        self.settings = replace(
            self.settings,
            exposure_us=exposure_us,
            gain=gain,
        )
        logger.info(
            "[CAMERA] Live controls updated: exposure=%dus gain=%.2f",
            exposure_us,
            gain,
        )
        return {"exposure_us": exposure_us, "gain": gain}

    def vertical_crop_status(self) -> dict:
        """Describe the live sensor window and the adjustment available to the UI.

        ``vertical_offset_px`` is the shift the driver applies to the current
        mode (``driver_strip_offset``): a value left in the parameter from the
        320x200 crop also moves the 640x200 strip, which offers no adjustment.
        """
        width, height = self.settings.width, self.settings.height
        limits = vertical_crop_limits(width, height)
        shifted = (width, height) in DRIVER_STRIP_OFFSET_LIMITS
        readable = shifted and self._vertical_offset_path.exists()
        offset = 0
        if readable:
            try:
                raw = int(self._vertical_offset_path.read_text(encoding="ascii").strip())
                offset = driver_strip_offset(width, height, raw)
            except (OSError, ValueError):
                logger.warning("[CAMERA] Could not read vertical crop parameter", exc_info=True)
                readable = False
        adjustable = limits is not None and readable
        payload = {
            "raw_crop_adjustable": adjustable,
            "vertical_offset_px": offset,
        }
        if limits is not None:
            payload.update(
                {
                    "vertical_offset_min_px": limits["min_px"],
                    "vertical_offset_max_px": limits["max_px"],
                    "vertical_offset_step_px": limits["step_px"],
                }
            )
        return payload

    def update_vertical_crop(self, offset_px: int) -> dict:
        """Move the hardware sensor window and restart the rolling capture."""
        limits = vertical_crop_limits(self.settings.width, self.settings.height)
        if limits is None:
            raise ValueError(
                f"vertical crop is unavailable for {self.settings.width}x{self.settings.height}"
            )
        offset_px = int(offset_px)
        if not limits["min_px"] <= offset_px <= limits["max_px"]:
            raise ValueError(
                f"vertical crop must be between {limits['min_px']} and {limits['max_px']} pixels"
            )
        if offset_px % limits["step_px"]:
            raise ValueError(f"vertical crop must use {limits['step_px']}-pixel steps")
        if not self._running:
            raise RuntimeError("camera capture is not running")

        with self._reconfigure_lock:
            status = self.vertical_crop_status()
            if not status["raw_crop_adjustable"]:
                raise RuntimeError(
                    "OV9281 vertical crop is unavailable; install the OpenFlight driver "
                    "and make strip_y_offset writable"
                )
            previous = int(status["vertical_offset_px"])
            if offset_px == previous:
                return status

            self.stop()
            try:
                self._vertical_offset_path.write_text(f"{offset_px}\n", encoding="ascii")
            except OSError as exc:
                self._reset_capture_state()
                self.start()
                raise RuntimeError(f"could not update OV9281 vertical crop: {exc}") from exc

            had_zone = self.active_tee_zone() is not None
            self._vertical_offset_px = offset_px
            self._tee_anchor.reset("sensor view moved")
            if had_zone and self.active_tee_zone() is None:
                logger.warning(
                    "[CAMERA] Tee zone was computed for the sensor window at %+d px; the trigger "
                    "check runs without it (and the camera gate cannot arm) until a zone is set "
                    "for %+d px",
                    previous,
                    offset_px,
                )
            self._reset_capture_state()
            try:
                self.start()
            except Exception:
                logger.exception("[CAMERA] Crop restart failed; restoring %+d px", previous)
                self._vertical_offset_path.write_text(f"{previous}\n", encoding="ascii")
                self._vertical_offset_px = previous
                self._reset_capture_state()
                self.start()
                raise
            logger.info("[CAMERA] Sensor view moved to %+d output pixels", offset_px)
            return self.vertical_crop_status()

    def _reset_capture_state(self) -> None:
        """Prepare one runtime instance to start again after a controlled stop."""
        self._ring = TriggeredFrameBuffer(self.settings.pre_frames, self.settings.post_frames)
        self._ready = queue.Queue()
        self._trigger_epochs = queue.Queue()
        self._trigger_auto_exposure = queue.Queue()
        self._auto_exposure_policy = AutoExposurePolicy(fps=self.settings.fps)
        self._last_persisted_controls = None
        # The tee anchor survives restarts (exposure changes): a fresh one would
        # need three new departures before the gate may block again.

    def status(self) -> dict:
        """Return lightweight state for the operator UI."""
        buffered_frames = self._ring.buffered_frames
        return {
            "running": self._running,
            "armed": self._running and buffered_frames >= self.settings.pre_frames,
            "buffered_frames": buffered_frames,
            "required_pre_frames": self.settings.pre_frames,
            "auto_exposure": self.auto_exposure_status(),
            "tee_anchor": self.tee_anchor_status(),
        }

    def notify_trigger(self, timestamp: float | None = None) -> bool:
        """Freeze the camera ring on a sound-trigger edge."""
        if not self._running:
            return False
        trigger_epoch = time.time() if timestamp is None else float(timestamp)
        with self._trigger_exposure_lock:
            if not self._running:
                return False
            accepted = self._ring.trigger(time.monotonic_ns())
            if not accepted:
                logger.debug("[CAMERA] Ignoring trigger edge while capture is busy")
                return False
            self._trigger_epochs.put(trigger_epoch)
            self._trigger_auto_exposure.put(self.auto_exposure_status())
            return True

    def capture_for_shot(
        self,
        impact_timestamp: float | None,
        *,
        timeout_s: float = 1.0,
    ) -> SavedCameraCapture | None:
        """Consume the saved capture nearest the OPS impact/trigger timestamp.

        Only clips whose edge lies within ``match_tolerance_s`` of impact match.
        While waiting, deferred clip writes are flushed.
        """
        deadline = time.monotonic() + timeout_s
        with self._condition:
            try:
                while True:
                    if impact_timestamp is None and self._captures:
                        return self._captures.pop(0)

                    if impact_timestamp is not None:
                        selected = self._nearest_capture_locked(impact_timestamp)
                        if selected is not None:
                            return selected

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return None
                    self._flush_requested.set()
                    self._condition.wait(timeout=remaining)
            finally:
                self._flush_requested.clear()

    def _nearest_capture_locked(self, impact_timestamp: float) -> SavedCameraCapture | None:
        tolerance = self.settings.match_tolerance_s
        cutoff = impact_timestamp - tolerance
        while self._captures and self._captures[0].trigger_timestamp < cutoff:
            stale = self._captures.pop(0)
            logger.warning(
                "[CAMERA] Discarding unmatched capture #%d (edge %.3f, shot %.3f)",
                stale.sequence,
                stale.trigger_timestamp,
                impact_timestamp,
            )
        matches = [
            capture
            for capture in self._captures
            if abs(capture.trigger_timestamp - impact_timestamp) <= tolerance
        ]
        if not matches:
            return None
        selected = min(
            matches, key=lambda capture: abs(capture.trigger_timestamp - impact_timestamp)
        )
        self._captures.remove(selected)
        return selected

    def _start_gpio_trigger(self) -> None:
        button_factory = self._button_factory
        if button_factory is None:
            ensure_lgpio_pin_factory()
            from gpiozero import Button  # pylint: disable=import-error,import-outside-toplevel

            button_factory = Button
        self._button = button_factory(
            self.settings.gpio_pin,
            pull_up=False,
            bounce_time=None,
        )
        self._button.when_pressed = self._on_gpio_edge

    def _on_gpio_edge(self) -> None:
        """The camera's own sound-input callback: ``notify_trigger`` plus the shadow edge log.

        The edge timestamp is the callback's wall time, read first as before;
        the kernel ticks and the monotonic stamp only go into ``edge_timing``.
        """
        callback_wall = time.time()
        edge_timing = self.edge_timing
        if edge_timing is not None:
            edge_timing.note(
                callback_wall,
                callback_timestamp=callback_wall,
                callback_monotonic_ns=time.monotonic_ns(),
                ticks=edge_ticks(self._button),
            )
        self.notify_trigger(callback_wall)
        if edge_timing is not None:
            edge_timing.flush()

    def _wait_for_prebuffer(self) -> None:
        deadline = time.monotonic() + 5.0
        while self._ring.buffered_frames < self.settings.pre_frames and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._ring.buffered_frames < self.settings.pre_frames:
            raise RuntimeError(
                "camera produced only "
                f"{self._ring.buffered_frames}/{self.settings.pre_frames} pre-trigger frames"
            )

    def _refill_locked_exposure_prebuffer(self) -> None:
        """Discard calibration frames and refill the ring at locked controls."""
        self._ring = TriggeredFrameBuffer(
            self.settings.pre_frames,
            self.settings.post_frames,
        )
        self._wait_for_prebuffer()

    def _start_auto_exposure(self) -> None:
        """Calibrate before arming shot capture, then lock the controls.

        The watch thread re-measures a lock that asks for light or no longer fits it.
        """
        self._auto_exposure_policy.reset()
        self._auto_exposure_stop.clear()
        self._auto_exposure_loop()

    def _auto_exposure_loop(
        self, stop: threading.Event | None = None, *, context: str = "Startup"
    ) -> None:
        """Converge startup controls and return without steady-state monitoring."""
        stop = stop or self._auto_exposure_stop
        while self._running and not stop.is_set() and not self._auto_exposure_stop.is_set():
            try:
                decision = self._run_auto_exposure_cycle(stop)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning("[CAMERA] %s exposure calibration failed", context, exc_info=True)
                return

            if decision is None:
                if stop.wait(0.1):
                    return
                continue
            if not decision.should_apply:
                logger.info(
                    "[CAMERA] %s exposure locked: %dus gain %.1f (%s)",
                    context,
                    self.settings.exposure_us,
                    self.settings.gain,
                    decision.status,
                )
                return
            if stop.wait(AUTO_EXPOSURE_STARTUP_SETTLE_S):
                return

    def _start_exposure_watch(self) -> None:
        """Watch the locked exposure in the background between shots."""
        stop = threading.Event()
        self._exposure_watch_stop = stop
        self._exposure_watch_thread = threading.Thread(
            target=self._exposure_watch_loop,
            args=(stop,),
            name="camera-exposure-watch",
            daemon=True,
        )
        self._exposure_watch_thread.start()

    def _exposure_watch_loop(self, stop: threading.Event) -> None:
        """Check every AUTO_EXPOSURE_RETRY_S until the camera stops."""
        unsuitable = 0
        while not stop.wait(AUTO_EXPOSURE_RETRY_S):
            if not self._running:
                return
            unsuitable = self._check_exposure(stop, unsuitable)

    def _check_exposure(self, stop: threading.Event, unsuitable: int) -> int:
        """One watch step; returns the new count of checks the light no longer fits.

        A lock that asks for light is retried at once. A ready lock is
        re-measured once the live impact area has been too bright or too dark
        for AUTO_EXPOSURE_DRIFT_CHECKS checks in a row, so a passer-by in front
        of the lens changes nothing. Checks pause while a shot is captured.
        """
        with self._auto_exposure_lock:
            status = self._auto_exposure_decision.status
        if not self.settings.auto_exposure or status not in {"ready", *_RETRY_STATUSES}:
            return 0
        if status in _RETRY_STATUSES:
            self.retry_exposure_calibration(stop)
            return 0
        if self._ring.capture_busy:
            return unsuitable
        frame = self._ring.latest_frame
        observation = measure_exposure(frame.image if frame is not None else np.asarray([]))
        count = unsuitable + 1 if observation.status in {"too_bright", "too_dark", "flat"} else 0
        if count < AUTO_EXPOSURE_DRIFT_CHECKS:
            return count
        self._remeasure_exposure(stop, f"the light changed ({observation.status})")
        return 0

    def retry_exposure_calibration(self, stop: threading.Event | None = None) -> bool:
        """Re-run the startup calibration while the locked result asks for light.

        Returns whether camera analysis is possible again.
        """
        with self._auto_exposure_lock:
            status = self._auto_exposure_decision.status
        if status not in _RETRY_STATUSES:
            return False
        reason = (
            "the last frame was flat" if status == "no_image" else "the last result asked for light"
        )
        return self._remeasure_exposure(stop or threading.Event(), reason)

    def _remeasure_exposure(self, stop: threading.Event, reason: str) -> bool:
        """Re-run the startup calibration between shots.

        Shots triggered meanwhile stay ineligible, and so do shots whose ring
        could still hold frames from before the new controls. Returns whether
        camera analysis is possible again.
        """
        with self._auto_exposure_lock:
            self._exposure_recalibrating = True
        try:
            logger.info("[CAMERA] Re-measuring exposure; %s", reason)
            self._auto_exposure_policy.reset()
            self._auto_exposure_loop(stop, context="Re-measured")
            with self._auto_exposure_lock:
                usable = self._auto_exposure_decision.analysis_eligible
            if usable:
                self._wait_for_fresh_frames(stop)
            return usable and not stop.is_set()
        finally:
            with self._auto_exposure_lock:
                self._exposure_recalibrating = False

    def _wait_for_fresh_frames(self, stop: threading.Event) -> None:
        """Let the ring turn over so no capture mixes frames from before the new controls."""
        frames = self.settings.pre_frames + self.settings.post_frames
        stop.wait(2.0 * frames / self.settings.fps)

    def _run_auto_exposure_cycle(
        self, stop: threading.Event | None = None
    ) -> AutoExposureDecision | None:
        """Measure one stable frame and apply the policy's requested control step."""
        with self._trigger_exposure_lock:
            # A stopped retry may get this lock only after a restart; it must
            # not touch the new camera.
            if not self._running or (stop is not None and stop.is_set()):
                return None
            if self._ring.capture_busy:
                with self._auto_exposure_lock:
                    self._auto_exposure_capture_deferred = True
                return None

            frame = self._ring.latest_frame
            observation = measure_exposure(
                frame.image if frame is not None else np.asarray([]),
            )
            decision = self._auto_exposure_policy.evaluate(
                observation,
                exposure_us=self.settings.exposure_us,
                gain=self.settings.gain,
            )
            if decision.should_apply:
                self.update_image_controls(
                    exposure_us=decision.target.exposure_us,
                    gain=decision.target.gain,
                )
                logger.info(
                    "[CAMERA] Auto exposure: %s -> %dus gain %.1f (%s)",
                    observation.status,
                    decision.target.exposure_us,
                    decision.target.gain,
                    decision.message,
                )
            elif decision.status == "lighting_required":
                logger.warning("[CAMERA] %s", decision.message)
            elif decision.status == "no_image":
                logger.warning(
                    "[CAMERA] Flat frame in mode %s (%dx%d %.0ffps, spread %s grey levels): "
                    "the sensor delivers no image, check the driver's register set for this "
                    "mode; camera analysis withheld",
                    self._current_capture_mode_id() or "custom",
                    self.settings.width,
                    self.settings.height,
                    self.settings.fps,
                    observation.frame_spread,
                )

            checked_at = time.time()
            with self._auto_exposure_lock:
                self._auto_exposure_decision = decision
                self._auto_exposure_capture_deferred = False
                self._auto_exposure_last_check_epoch = checked_at
                if decision.should_apply:
                    self._auto_exposure_last_adjustment_epoch = checked_at
        if decision.status == "ready" and observation.status == "good":
            self._persist_auto_exposure_controls()
        return decision

    def _restore_auto_exposure_controls(self) -> None:
        """Use the last good setting as the next startup seed for this mode."""
        path = self._auto_exposure_state_path
        if not self.settings.auto_exposure or path is None or not path.exists():
            return
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            same_mode = (
                saved.get("version") == 1
                and int(saved["width"]) == self.settings.width
                and int(saved["height"]) == self.settings.height
                and math.isclose(float(saved["fps"]), self.settings.fps, rel_tol=0.001)
            )
            exposure_us = int(saved["exposure_us"])
            gain = float(saved["gain"])
            frame_period_us = round(1_000_000 / self.settings.fps)
            if same_mode and 0 < exposure_us < frame_period_us and gain > 0:
                self.settings = replace(
                    self.settings,
                    exposure_us=exposure_us,
                    gain=gain,
                )
                self._last_persisted_controls = (exposure_us, gain)
                logger.info(
                    "[CAMERA] Restored auto exposure seed: %dus gain %.1f",
                    exposure_us,
                    gain,
                )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            logger.warning("[CAMERA] Ignoring invalid auto exposure state: %s", path)

    def _persist_auto_exposure_controls(self) -> None:
        """Atomically remember a validated setting without delaying capture."""
        path = self._auto_exposure_state_path
        controls = (self.settings.exposure_us, self.settings.gain)
        if path is None or controls == self._last_persisted_controls:
            return
        payload = {
            "version": 1,
            "width": self.settings.width,
            "height": self.settings.height,
            "fps": self.settings.fps,
            "exposure_us": controls[0],
            "gain": controls[1],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f"{path.suffix}.tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
            self._last_persisted_controls = controls
        except OSError:
            logger.warning("[CAMERA] Could not persist auto exposure state", exc_info=True)

    def _on_frame(self, request) -> None:
        try:
            metadata = request.get_metadata()
            if self.settings.stream == "main-y":
                image = unpack_yuv420_y_plane(
                    request.make_array("main"),
                    self.settings.width,
                    self.settings.height,
                    self.settings.rotate_180,
                    self.settings.mirror_horizontal,
                )
            else:
                image = unpack_r8_frame(
                    request.make_array("raw"),
                    self.settings.width,
                    self.settings.height,
                    self.settings.rotate_180,
                    self.settings.mirror_horizontal,
                )
            self._ring.add_frame(
                CameraFrame(
                    image=image,
                    sensor_timestamp_ns=int(metadata["SensorTimestamp"]),
                    host_timestamp_ns=time.monotonic_ns(),
                    exposure_us=int(metadata.get("ExposureTime", 0)),
                    analogue_gain=float(metadata.get("AnalogueGain", 0.0)),
                )
            )
            capture = self._ring.pop_capture()
            if capture is not None:
                self._ready.put(capture)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[CAMERA] Frame callback failed: %s", exc, exc_info=True)

    def _save_loop(self) -> None:
        while self._running:
            capture = self._ready.get()
            if capture is None:
                self._ready.task_done()
                break
            trigger_epoch = self._trigger_epochs.get() if not self._trigger_epochs.empty() else 0.0
            auto_exposure = (
                self._trigger_auto_exposure.get()
                if not self._trigger_auto_exposure.empty()
                else None
            )
            self._sequence += 1
            sequence = self._sequence
            self._wait_for_write_slot(sequence)
            try:
                saved = self._save_capture(
                    sequence,
                    trigger_epoch,
                    capture,
                    auto_exposure=auto_exposure,
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[CAMERA] Capture #%d save failed: %s", sequence, exc, exc_info=True)
                saved = SavedCameraCapture(
                    sequence=sequence,
                    trigger_timestamp=trigger_epoch,
                    completed_timestamp=time.time(),
                    path=self.output_dir,
                    metadata={},
                    error=str(exc),
                )
            with self._condition:
                self._captures.append(saved)
                self._condition.notify_all()
            self._ready.task_done()

    def _trigger_check(  # pylint: disable=too-many-arguments
        self,
        sequence: int,
        images: np.ndarray,
        host_ns: np.ndarray,
        sensor_ns: np.ndarray,
        trigger_ns: int,
        *,
        exposure_gain: np.ndarray | None = None,
    ) -> dict:
        """Observation-only camera verdict for this edge; never raises."""
        try:
            check = check_ball_departure(
                images,
                exposure_timestamps_ns(host_ns, sensor_ns),
                trigger_ns,
                self._tee_anchor.hint(),
                zone=self.active_tee_zone(),
                ball_mark_px=self._ball_mark_px(sequence),
                exposure_gain=exposure_gain,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[CAMERA] Trigger check #%d failed: %s", sequence, exc, exc_info=True)
            return {"decision": "unclear", "reason": "check_failed", "error": str(exc)}
        if check.decision == "departed":
            verdict = f"departed at +{check.first_departure_ms_after_edge:.0f} ms"
        else:
            verdict = f"{check.decision} ({check.reason})"
        logger.info(
            "[CAMERA] Trigger check #%d: %s (contrast %s, ball %s via %s, flicker %s, %.1f ms)",
            sequence,
            verdict,
            "-" if check.pre_contrast is None else f"{check.pre_contrast:.0f}",
            "-" if check.ball is None else f"{check.ball_x:.0f},{check.ball_y:.0f}",
            check.ball_source or "-",
            (check.flicker or {}).get("reason", "-"),
            check.compute_ms,
        )
        return check.to_dict()

    def _save_capture(
        self,
        sequence: int,
        trigger_epoch: float,
        capture: TriggeredCapture,
        *,
        auto_exposure: dict | None = None,
    ) -> SavedCameraCapture:
        timestamp = datetime.fromtimestamp(trigger_epoch or time.time()).strftime(
            "%Y%m%d_%H%M%S_%f"
        )[:-3]
        shot_dir = self.output_dir / f"camera_{timestamp}_{sequence:03d}"
        shot_dir.mkdir(parents=True, exist_ok=False)
        started = time.monotonic()

        images = np.stack([frame.image for frame in capture.frames])
        sensor_ns = np.asarray(
            [frame.sensor_timestamp_ns for frame in capture.frames],
            dtype=np.int64,
        )
        host_ns = np.asarray(
            [frame.host_timestamp_ns for frame in capture.frames],
            dtype=np.int64,
        )
        exposure_us = np.asarray([frame.exposure_us for frame in capture.frames], dtype=np.int32)
        gain = np.asarray([frame.analogue_gain for frame in capture.frames], dtype=np.float32)

        # These clips are consumed immediately by the live estimators. ZIP
        # compression delayed shot display by roughly a second on the Pi, so
        # favor fast sequential I/O over the modest storage reduction.
        np.savez(
            shot_dir / "frames.npz",
            frames=images,
            sensor_timestamp_ns=sensor_ns,
            host_timestamp_ns=host_ns,
            exposure_us=exposure_us,
            analogue_gain=gain,
            pre_trigger_count=np.int32(capture.pre_trigger_count),
            trigger_host_timestamp_ns=np.int64(capture.trigger_host_timestamp_ns),
            trigger_epoch_timestamp=np.float64(trigger_epoch),
        )

        for label, index in (
            ("first", 0),
            ("trigger", max(0, capture.pre_trigger_count - 1)),
            ("last", len(images) - 1),
        ):
            _save_pgm(shot_dir / f"{label}.pgm", images[index])

        summary = timing_summary(capture.frames)
        summary.update(
            {
                "sequence": sequence,
                "trigger_timestamp": trigger_epoch,
                "completed_timestamp": time.time(),
                "capture_path": str(shot_dir),
                "pre_trigger_frames": capture.pre_trigger_count,
                "post_trigger_frames": capture.post_trigger_count,
                "trigger_host_timestamp_ns": capture.trigger_host_timestamp_ns,
                "mean_brightness": float(images.mean()),
                "p99_brightness": float(np.percentile(images, 99)),
                "storage_format": "npz_uncompressed",
                "npz_bytes": (shot_dir / "frames.npz").stat().st_size,
                "save_time_ms": (time.monotonic() - started) * 1000.0,
                "settings": {
                    "width": self.settings.width,
                    "height": self.settings.height,
                    "fps": self.settings.fps,
                    "pre_ms": self.settings.pre_ms,
                    "post_ms": self.settings.post_ms,
                    "exposure_us": self.settings.exposure_us,
                    "gain": self.settings.gain,
                    # The sensor window: with width and height it places the clip's pixels.
                    "vertical_offset_px": self._vertical_offset_px,
                    "stream": self.settings.stream,
                    "rotate_180": self.settings.rotate_180,
                    "mirror_horizontal": self.settings.mirror_horizontal,
                    "roll_correction_deg": self.settings.roll_correction_deg,
                    "scaler_crop": self.settings.scaler_crop,
                    "auto_exposure": self.settings.auto_exposure,
                    "buffer_count": self.settings.buffer_count,
                },
                # Preserve the startup record for diagnosis, but never let
                # its stale verdict accept/reject a later shot's pixels.
                "startup_auto_exposure": auto_exposure or self.auto_exposure_status(),
                "auto_exposure": capture_exposure_status(capture),
            }
        )
        summary["trigger_check"] = self._trigger_check(
            sequence,
            images,
            host_ns,
            sensor_ns,
            capture.trigger_host_timestamp_ns,
            exposure_gain=exposure_us.astype(np.float64) * gain.astype(np.float64),
        )
        blocked_event = self.note_trigger_check(trigger_epoch, summary["trigger_check"])
        if blocked_event is not None:
            summary["tee_anchor"] = blocked_event
        (shot_dir / "metadata.json").write_text(json.dumps(summary, indent=2) + "\n")
        logger.info(
            "[CAMERA] Capture #%d saved: %d frames, %.1ffps, gaps=%d -> %s",
            sequence,
            summary["frame_count"],
            summary["delivered_fps"],
            summary["gap_count"],
            shot_dir,
        )
        return SavedCameraCapture(
            sequence=sequence,
            trigger_timestamp=trigger_epoch,
            completed_timestamp=summary["completed_timestamp"],
            path=shot_dir,
            metadata=summary,
        )
