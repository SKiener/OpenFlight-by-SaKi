"""
WebSocket server for OpenFlight UI.

Provides real-time shot data to the web frontend via Flask-SocketIO.
"""

import copy
import json
import logging
import math
import os
import queue
import random
import statistics
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from flask import Flask, Response, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from flask_socketio import ConnectionRefusedError as SocketConnectionRefusedError, SocketIO

from .angle_reference import (
    BALL_MARK_OUTSIDE_MODE,
    AngleReference,
    ball_mark_from_tap,
    camera_vertical_confidence,
    canonical_geometry,
    carry_camera_marks,
    load_camera_marks,
    resolve_target_line_path,
    save_camera_marks,
    target_line_from_tap,
)
from .ballistics import (
    DEFAULT_TYPICAL_SPIN_TABLE,
    M_TO_YD,
    RANGE_BALL_DRAG_SLOPE_RANGE,
    TRACKMAN_FLAT_AIR_DENSITY,
    TYPICAL_SPIN_SCALE_RANGE,
    TYPICAL_SPIN_TABLES,
    LaunchConditions,
    Trajectory,
    carry_span_yards,
    carry_variants,
    flight_path,
    resolve_launch,
    roll_path,
    simulate,
    touchdown_of,
)
from .camera.intrinsics import for_mode as intrinsics_for_mode
from .client_clock import (
    CLOCK_OFFSET_WARN_S,
    ClientClockTracker,
    format_clock_offset,
    local_utc_offset_min,
    parse_client_clock,
)
from .club_data import CLUB_DATA_FIELDS, calculate_club_data
from .clubs import ClubType
from .clubs.bag import SELECTABLE_CLUB_IDS, clean_club_ids, club_category
from .clubs.physics import get_club_physics
from .demo import DEMO_INTERVAL_S, DemoSession
from .dplane import (
    MEAN_ATTACK_ANGLE_DEG,
    curve_class,
    dplane_spin_axis,
    face_to_path_deg,
    face_weight_for_club,
    spin_loft_deg,
)
from .edge_timing import EdgeTimingLog
from .launch_monitor import (
    CLUB_DATA_SHOT_FIELDS,
    SPIN_CONFIDENCE_HIGH,
    SPIN_CONFIDENCE_RELIABLE,
    Shot,
    summarize_shots,
    trusted_spin_rpm,
)
from .ops243 import (
    UART_BAUD_COMMANDS,
    Direction,
    OPS243Radar,
    SpeedReading,
    is_uart_port,
    set_show_raw_readings,
)
from .ops_spin_median import OpsSpinSessionMedian, log_fields as ops_spin_median_log_fields
from .pairing import (
    DEVICE_COOKIE_MAX_AGE_S,
    DEVICE_COOKIE_NAME,
    LOOPBACK_ORIGIN_PATTERN,
    DeviceSecret,
    PairingCodes,
    is_local_environ,
    origin_allowed,
    pairing_links,
    wifi_join_qr_svg,
)
from .phone_events import (
    PhoneClockProbes,
    PhoneEventRejected,
    onset_log_entry,
    parse_audio_onset,
    parse_probe,
    parse_sensors,
)
from .phone_tilt import (
    CASE_TOP_TO_BORESIGHT_DEG,
    PhoneTiltRejected,
    build_record as build_phone_tilt_record,
    calibration_path as default_phone_tilt_path,
    delete_calibration as delete_phone_tilt_calibration,
    load_calibration as load_phone_tilt_calibration,
    measure_phone_tilt,
    save_calibration as save_phone_tilt_calibration,
)
from .pi_health import pi_health
from .power import SUPPORTED_BATTERY_PROVIDERS, PowerMonitor, PowerStatus
from .profiles import (
    BALL_TYPE_SPIN_TABLES,
    Profile,
    ProfileStore,
    ball_type,
    ball_type_for_spin_table,
    clean_ball_type,
    driver_tee_height_m,
)
from .rolling_buffer.monitor import estimate_carry_with_spin, get_optimal_spin_for_ball_speed
from .rolling_buffer.processor import RollingBufferProcessor
from .rollout import OutOfRangeError, simulate_rollout
from .session_history import (
    HISTORY_DAYS,
    append_shot_deleted,
    club_averages,
    find_saved_shot,
    get_session as read_saved_session,
    list_sessions,
)
from .session_logger import (
    SessionLogger,
    get_session_logger,
    init_session_logger,
    log_session_error,
)
from .shot_generator import generate_shot
from .sim import (
    IncompleteShotError,
    PlayerState as SimPlayerState,
    PlayerUpdate,
    ShotAck,
    SimError,
    build_connectors,
    initial_shot_counter,
    load_sim_config,
    resolve_shot,
)
from .speed_correction import correct_ball_speed
from .spin_estimate import calculated_spin_rpm
from .startup_status import StartupStatusReporter, configured_startup_components
from .swing_speed import SwingSpeedEvent
from .voice_callouts import VoiceCalloutStore
from .weather import (
    AIR_DENSITY_RANGE_KG_M3,
    AirDensityChoice,
    FetchFailureRateGate,
    WeatherRejected,
    WeatherStore,
    parse_weather_fetch_failure,
    parse_weather_report,
)

# Configure logging
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = REPO_ROOT / "ui" / "dist"
FRONTEND_SOURCE_DIR = REPO_ROOT / "ui"


app = Flask(__name__, static_folder=str(FRONTEND_DIST_DIR), static_url_path="")
CORS(app, origins=[LOOPBACK_ORIGIN_PATTERN])
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Global state
monitor = None
_ops_control_lock = threading.Lock()
power_monitor: Optional[PowerMonitor] = None
battery_provider: str | None = None
hotspot_ssid: str | None = None
hotspot_password: str | None = None
mock_mode: bool = False
debug_mode: bool = False
mock_swing_speed_mode: bool = False
debug_log_file = None
debug_log_path: Optional[Path] = None
# Created lazily so importing the server (in tests, in tooling) never writes
# to the real config directory.
profile_store: Optional[ProfileStore] = None


def get_profile_store() -> ProfileStore:
    """The profile roster. Single source of truth for the active selection."""
    global profile_store  # pylint: disable=global-statement
    if profile_store is None:
        profile_store = ProfileStore()
    return profile_store


# Range location (persisted) and the latest weather report; created lazily like the roster.
weather_store: Optional[WeatherStore] = None
# At most one weather_fetch_failed log entry per 5 s, whichever phone sends it.
weather_fetch_failure_gate = FetchFailureRateGate()


def get_weather_store() -> WeatherStore:
    """The range location and the current weather reading."""
    global weather_store  # pylint: disable=global-statement
    if weather_store is None:
        weather_store = WeatherStore()
    return weather_store


# Spoken shot callouts, shared by every screen (persisted); created lazily like the roster.
voice_callout_store: Optional[VoiceCalloutStore] = None


def get_voice_callout_store() -> VoiceCalloutStore:
    """The session-wide callout setting the phones speak by."""
    global voice_callout_store  # pylint: disable=global-statement
    if voice_callout_store is None:
        voice_callout_store = VoiceCalloutStore()
    return voice_callout_store


def _air_density_for_shot() -> AirDensityChoice:
    """Fresh weather's density, else the fixed --air-density."""
    return get_weather_store().density(air_density_kg_m3)


device_secret: Optional[DeviceSecret] = None
_device_secret_lock = threading.Lock()
pairing_codes = PairingCodes()
client_clocks = ClientClockTracker()
phone_clock_probes = PhoneClockProbes()
_pairing_lock = threading.Lock()
_paired_socket_sids: set[str] = set()


def get_device_secret() -> DeviceSecret:
    """The secret paired devices present, created lazily like the profile store."""
    global device_secret  # pylint: disable=global-statement
    with _device_secret_lock:
        if device_secret is None:
            device_secret = DeviceSecret()
        return device_secret


TRAINING_IMPLEMENT_LABELS = {
    "driver": "Driver",
    "superspeed-light": "SuperSpeed Light",
    "superspeed-medium": "SuperSpeed Medium",
    "superspeed-heavy": "SuperSpeed Heavy",
    "speed-stick-light": "SuperSpeed Light",
    "speed-stick-medium": "SuperSpeed Medium",
    "speed-stick-heavy": "SuperSpeed Heavy",
    "stack": "Stack",
    "stack-0g": "Stack 0g",
    "stack-60g": "Stack 60g",
    "stack-100g": "Stack 100g",
    "stack-120g": "Stack 120g",
    "stack-160g": "Stack 160g",
    "stack-180g": "Stack 180g",
    "stack-200g": "Stack 200g",
    "stack-220g": "Stack 220g",
    "stack-240g": "Stack 240g",
    "stack-260g": "Stack 260g",
    "stack-280g": "Stack 280g",
    "stack-300g": "Stack 300g",
    "rypstick": "Rypstick",
    "rypstick-0w": "Rypstick 0 Weights",
    "rypstick-1w": "Rypstick 1 Weight",
    "rypstick-2w": "Rypstick 2 Weights",
    "rypstick-3w": "Rypstick 3 Weights",
    "rypstick-3w-cw": "Rypstick 3 Weights + Counterweight",
    "custom": "Custom",
}

# K-LD7 angle radars (vertical = launch angle, horizontal = club path)
kld7_vertical = None
kld7_horizontal = None

# TI IWR6843 L3 rolling-buffer capture + LCMF-v1 launch angle.
iwr6843_runtime = None
iwr6843_runtime_config: dict = {"enabled": False}
# Saved phone tilt calibration (see phone_tilt.py); it outranks --iwr6843-tilt-deg.
# None: OPENFLIGHT_IWR6843_PHONE_TILT_PATH, else ~/.config/openflight/.
PHONE_TILT_CALIBRATION_PATH: Optional[Path] = None
# Serialises applying/removing the phone tilt (file + runtime swap together).
_iwr6843_tilt_lock = threading.Lock()
# Tilt the newest TI shot was processed with: {"tilt_deg": float, "at": iso}.
_last_effective_iwr_tilt: dict | None = None
camera_capture_runtime = None
camera_capture_config: dict = {"enabled": False}
trigger_gate = None
trigger_gate_listener = None
trigger_gate_config: dict = {"enabled": False}
# OPS243-A firmware 1.3.3 speed trigger (--shot-trigger ops-speed*): its settings
# (OpsSpeedConfig), the pin-1/microphone witness, the input the TI monitor or the
# gate listener does not own (ops-speed: the microphone; shadow: pin 1) and the
# session-start record ("requested" only once the mode was asked for).
ops_speed_settings = None
ops_speed_witness = None
ops_speed_listener = None
ops_speed_runtime_config: dict = {"enabled": False}
# Kernel vs callback time of every sound edge (shadow log); filled by whichever
# input owns the sound pin (TI monitor or trigger-gate listener).
sound_edge_timing = EdgeTimingLog()
camera_replay_manager = None
_history_replay_init_lock = threading.Lock()
camera_reference_ball_tracker = None
camera_ball_flight_reference_tracker = None
# Untee'd balls the disappeared-ball locator found: the ball flight's camera pose.
camera_ball_flight_ground_anchor = None
# Low-light shadow start direction (camera.lowlight): logged after publication, never
# on the shot. --no-camera-lowlight-shadow turns it off. Its own tee tracker and
# ground anchor, so it never moves the live camera pose.
camera_lowlight_shadow_enabled = True
camera_lowlight_reference_tracker = None
camera_lowlight_ground_anchor = None
# Low-priority process for the per-shot camera CV; None runs it in this process
# (no camera, or --camera-cv-in-process).
camera_cv_worker = None
# Optional calibrated lens model (--camera-intrinsics); None keeps the legacy model.
camera_intrinsics = None
# The camera as the launch-angle reference: session TI offsets and the tapped
# target line (openflight.angle_reference). The target persists per device setup.
angle_reference = AngleReference()
# Calculated club data ("berechnet", openflight.club_data): "show" puts the gated values
# on the shot and updates the screens; "log" only writes the session log (--club-data).
CLUB_DATA_MODES = ("show", "log")
club_data_mode: str = "show"
# The golfer's handedness for the club data (--golfer-handedness); a simulator that
# reports one (GSPro "Handed") overrides it.
golfer_handedness: str = "right"
_sim_reported_handed: str | None = None
# Session state of the club data, reset on session clear, profile switch and a new
# session: the acoustic-delay calibrator (teed shots) and the impacts' plausible head
# lengths per (profile, club) of E and of the impact point D (separately: D reads
# them across contact at the ball's depth), whose medians the impact gates use from
# 5 on. D's scale is the regulation ball at the known range: no session ball size.
_club_data_state_lock = threading.Lock()
_club_data_calibrator = None
_club_head_length_mm: dict[tuple[str, str], list[float]] = {}
_impact_point_head_length_mm: dict[tuple[str, str], list[float]] = {}
_CLUB_HEAD_LENGTH_MIN_SAMPLES = 5
# How long a club-data result waits for a running finalization to finish copying the
# shot, so it never interleaves with that copy.
_CLUB_DATA_FINALIZATION_WAIT_S = 2.0

# Optional LIS3DH enclosure orientation used to compensate TI mount tilt.
inclinometer_service = None
inclinometer_runtime_config: dict = {"enabled": False}

# Ballistic model toggle. Shot carry comes from the physics simulator whenever
# a vertical launch angle is available. Operators can explicitly disable it;
# missing launch inputs always fall back to the legacy table estimator.
ballistics_enabled: bool = True
# Fixed fallback density (--air-density). A fresh phone weather report replaces it for
# live shots (weather.py); without one, or once it is older than 3 h, this one flies.
air_density_kg_m3: float = TRACKMAN_FLAT_AIR_DENSITY
# Factor on the club-typical spin of irons and wedges that feeds the ballistic
# carry when no trusted spin was measured (--typical-spin-scale). Other clubs
# and the simulator export spin are not scaled.
typical_spin_scale: float = 1.0
# Which club-typical spin table feeds that carry (--typical-spin-table): "tour"
# (TrackMan PGA Tour averages) or "range-ball" (this user's Toptracer range-ball spin).
# The device default: a profile's ball type ("ball_type" setting) overrides it for the
# shots attributed to that profile (_shot_typical_spin_table).
typical_spin_table: str = DEFAULT_TYPICAL_SPIN_TABLE
# Unvalidated range-ball drag of every ballistic flight (--range-ball-drag-slope);
# 0.0 = off, today's model.
range_ball_drag_slope: float = 0.0

# Simulator connectors (optional). Populated in main() from config/sim.json +
# CLI flags; shots fan out to every connected connector. Player/club state is
# shared across all of them.
sim_connectors: List = []
# Seed the shot counter from the clock so ShotNumber strictly increases across
# server restarts. Some sims (e.g. OpenGolfSim's Developer API) reject any
# ShotNumber <= the highest they've seen, and that counter persists across
# reconnects — a per-run reset to 1 would get every shot dropped. Uses epoch
# *seconds* (see initial_shot_counter): epoch millis overflow GSPro's 32-bit
# ShotNumber field and every shot comes back 501 "Bad format".
sim_player_state = SimPlayerState(shot_counter=initial_shot_counter())

shutdown_lock = threading.Lock()
shutdown_cleanup_started = False
# One active hardware job plus two waiting shots is enough for normal golf
# cadence without allowing a stuck peripheral to consume memory indefinitely.
_SHOT_ENRICHMENT_QUEUE_CAPACITY = 2
_SHOT_ENRICHMENT_DEADLINE_S = 20.0
# A shot's camera-CV jobs get what is left of that shot's enrichment deadline; a
# worker that has not answered by then is replaced (the watchdog has published
# the shot without camera data already), and a job whose shot is past it does not
# run. This is the worker's own timeout for a start and for a job without a shot
# deadline, and the budget all of a shot's camera jobs share when it has none.
_CAMERA_CV_WORKER_TIMEOUT_S = _SHOT_ENRICHMENT_DEADLINE_S
# Budget of the camera club-data job (contact time, gate-free departure, path, arc
# attack angle and impact) in the CV worker (plan D13). The club-head path alone took
# 0.20-0.32 s per clip on a Mac; the full job is about 5 s on a Pi 5 (about 5x slower
# than the Mac), so 6 s. Its stage timings are logged; revisit after the Pi timing (A6).
# A job past it is cut off and logged as a timeout, and the worker is started again at
# once, in the background, while no shot waits. The shot was published before it
# started; it does not start while another shot waits, but a shot arriving while it
# runs waits for it: up to the budget plus the cut-off. The restart does not hold the
# enrichment thread; a shot that needs the worker while it starts waits only for the
# rest of the start.
_CAMERA_CLUB_DATA_BUDGET_S = 6.0
# Budget of the low-light shadow start direction (flicker compensation plus one ball
# flight) in the CV worker, after publication and after the club-data job. It took
# 0.33-0.42 s per clip on a Mac (median; max 0.82 s; analysis/lowlight-20261031/
# shadow/results.json), so about 2 s (max 4 s) on a Pi 5 at the usual 5x. Like the
# club-data job it does not start while a shot waits, and a job past it is cut off
# (the worker restarts in the background while no shot waits).
_CAMERA_LOWLIGHT_BUDGET_S = 5.0
# Active hardware work, the two queued jobs, and one overflow result. If more
# shots arrive, fail the oldest optional enrichment closed so OPS finalization
# can keep advancing without retaining an unbounded number of full Shot objects.
_SHOT_FINALIZATION_CAPACITY = _SHOT_ENRICHMENT_QUEUE_CAPACITY + 2
shot_enrichment_queue: queue.Queue[tuple[Shot, str, float | None]] = queue.Queue(
    maxsize=_SHOT_ENRICHMENT_QUEUE_CAPACITY
)
shot_enrichment_task = None
shot_enrichment_task_lock = threading.Lock()
_shot_sequence_number = 0
_shot_sequence_lock = threading.Lock()
_shot_callback_lock = threading.Lock()
_shot_finalization_lock = threading.Lock()
_shot_finalization_condition = threading.Condition(_shot_finalization_lock)
_shot_finalization_order: deque[int] = deque()
_shot_finalization_registered: dict[int, "_RegisteredShotFinalization"] = {}
_shot_finalization_ready: dict[int, "_PendingShotFinalization"] = {}
_shot_finalization_running = False
_shot_finalization_worker: threading.Thread | None = None
# Set from the monitor's "capturing"/"calculating" lifecycle until the shot is
# registered for finalization; guarded by _shot_finalization_condition.
_shot_processing_active = False
# Background MP4 preparation for companion screens. Encoding is CPU heavy on a
# Pi, so it waits for capture/finalization to go idle and runs at low priority.
_CAMERA_REPLAY_AUTO_PREPARE_CAPACITY = 4
_CAMERA_REPLAY_WORKER_NICE = 10
_CAMERA_REPLAY_IDLE_POLL_S = 0.25
_CAMERA_REPLAY_REQUEST_TIMEOUT_S = 60.0
# (shot_number, replay_id, result): result is set for HTTP requests, None for auto jobs.
camera_replay_auto_prepare_queue: queue.Queue[tuple[int | None, str, Future | None]] = queue.Queue(
    maxsize=_CAMERA_REPLAY_AUTO_PREPARE_CAPACITY
)
_camera_replay_auto_prepare_worker: threading.Thread | None = None
_camera_replay_auto_prepare_lock = threading.Lock()


@dataclass(frozen=True)
class _ClubHeadPathShadowJob:  # pylint: disable=too-many-instance-attributes
    """Inputs of one shot's camera club-data job, run after publication.

    ``kwargs`` are the clip, the launch-direction geometry, the club speed and a
    copy of the ball tracker as the ball-flight run saw it. ``contact`` is the
    preliminary contact time (without the camera departure the job adds), logged
    when the job does not run. ``skip_reason`` marks a ball-analysis-eligible
    clip the job cannot run on (no tee geometry, no frames, a failed ball
    flight); it still gets its log entries, so the log has one per eligible
    clip. ``edge_reference`` names the edge stamp the trigger lag came from
    (``kernel`` or ``callback``).

    The start direction, its fusion status and the launch angle are the shot's
    after ``_fuse_camera_measurements``; ``camera_vertical_deg`` is the same
    clip's camera launch angle (None without an accepted camera estimate),
    ``departed_ball`` the ball the disappeared-ball locator found and
    ``confirmed_tee`` the session's confirmed tee anchor. Without a departed ball
    ``reference_ball`` is the tapped ball mark the ball flight searched around
    (``club_head_path.mark_reference_ball``), and ``tee_zone`` the hitting zone
    of the clip's capture mode; both bound the club ball
    (``club_head_path.locate_club_ball``). ``source_shot`` is the stored shot
    and ``final_shot`` the enriched copy finalization copies into it.
    """

    shot_number: int | None
    shot_timestamp: float | None
    contact: object | None  # camera.club_head_path.ContactTime
    kwargs: dict = field(default_factory=dict, repr=False)
    skip_reason: str | None = None
    edge_reference: str | None = None
    club: ClubType | None = None
    club_key: str = ""
    profile_id: str = ""
    teed: bool = False
    time_zero: object | None = None  # camera.ball_flight.CameraTimeZero
    trigger_ns: int | None = None
    tee_range_m: float | None = None
    trigger_lag_ms: float | None = None
    camera_vertical_deg: float | None = None
    launch_direction_deg: float | None = None
    launch_direction_status: str | None = None
    launch_vertical_deg: float | None = None
    roll_calibrated: bool = False
    handedness: str = "right"
    head_length_median_mm: float | None = None
    departed_ball: object | None = None  # camera.club_motion.ReferenceBall
    confirmed_tee: object | None = None  # camera.club_motion.ReferenceBall
    reference_ball: object | None = None  # camera.club_motion.ReferenceBall
    tee_zone: object | None = None  # camera.tee_zone.TeeZone
    # The impact point (camera.impact_point): the shot's ball speed (its launch angles
    # are the ones above) blanks the departing ball; the ball flight's locator saw a
    # tee under the ball (``ball_teed``); the session median of its head lengths.
    ball_speed_mph: float | None = None
    ball_teed: bool | None = None
    impact_point_head_length_median_mm: float | None = None
    source_shot: Shot | None = field(default=None, repr=False, compare=False)
    final_shot: Shot | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _LowLightShadowJob:  # pylint: disable=too-many-instance-attributes
    """Inputs of one shot's low-light shadow start direction, run after publication.

    Captured right after the live camera fusion: the clip, whether the live
    ball gate admitted it and the live result (for the comparison in the log),
    and the ball flight's inputs as the live path had them. The frames are
    loaded only when the job runs. ``skip_reason`` marks a clip it cannot run on.
    """

    shot_number: int | None
    shot_timestamp: float | None
    camera_capture: object = field(default=None, repr=False, compare=False)
    skip_reason: str | None = None
    live_ball_analysis_eligible: bool = False
    auto_exposure: dict | None = field(default=None, repr=False)
    live_camera_horizontal_deg: float | None = None
    live_camera_horizontal_confidence: float | None = None
    live_camera_horizontal_status: str | None = None
    geometry: object | None = field(default=None, repr=False)  # CameraBallGeometry
    ball_locator: str = "disappeared"
    range_evidence: object | None = field(default=None, repr=False)
    ops_ball_speed_mph: float | None = None
    iwr_vertical_deg: float | None = None
    iwr_horizontal_deg: float | None = None
    iwr_horizontal_confidence: float | None = None
    tee_zone: object | None = field(default=None, repr=False)  # camera.tee_zone.TeeZone
    # The tapped resting ball (saved-frame pixels), read when the live fusion ran.
    ball_mark_px: tuple[float, float] | None = None


@dataclass(frozen=True)
class _ShotEnrichmentResult:
    """Optional hardware outputs needed by required shot finalization."""

    iwr6843_ms: float | None = None
    kld7_ms: float | None = None
    camera_capture_ms: float | None = None
    # Not needed by finalization: run once the shot is queued for publication.
    camera_club_head_path_job: _ClubHeadPathShadowJob | None = field(
        default=None, repr=False, compare=False
    )
    camera_lowlight_job: _LowLightShadowJob | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class _CarryFlight:
    """What a ballistic carry was flown with: the input of its carry span and variants."""

    conditions: LaunchConditions
    # The live density (fresh weather, else --air-density), never the export's fixed one.
    air_density_kg_m3: float
    # --range-ball-drag-slope at the flight (0.0 = off).
    range_ball_drag_slope: float = 0.0


@dataclass(frozen=True)
class _PendingShotFinalization:
    """A completed enrichment waiting for its detection-order publication turn."""

    shot: Shot
    emit_event: str
    initial_ui_ms: float | None
    enrichment: _ShotEnrichmentResult


@dataclass(frozen=True)
class _RegisteredShotFinalization:
    """OPS-only fallback retained until optional enrichment reaches its deadline."""

    shot: Shot
    emit_event: str
    initial_ui_ms: float | None
    deadline_monotonic: float | None


def _assign_shot_number(shot: Shot) -> None:
    """Give a shot one stable session identity before any async work starts."""
    global _shot_sequence_number  # pylint: disable=global-statement

    with _shot_sequence_lock:
        if shot.shot_number is None:
            _shot_sequence_number += 1
            shot.shot_number = _shot_sequence_number
        else:
            _shot_sequence_number = max(_shot_sequence_number, shot.shot_number)


def _reset_shot_sequence() -> None:
    """Start shot identities at one for a newly started logging session."""
    global _shot_sequence_number  # pylint: disable=global-statement
    global _shot_finalization_running  # pylint: disable=global-statement

    with _shot_sequence_lock:
        _shot_sequence_number = 0
    with _shot_finalization_condition:
        _shot_finalization_order.clear()
        _shot_finalization_registered.clear()
        _shot_finalization_ready.clear()
        _shot_finalization_running = False
        _shot_finalization_condition.notify_all()


def _register_shot_for_finalization(
    shot: Shot,
    *,
    emit_event: str = "shot_update",
    initial_ui_ms: float | None = None,
    needs_watchdog: bool = False,
) -> None:
    """Record callback order and the bounded OPS-only fallback for a shot."""
    if shot.shot_number is None:
        raise ValueError("shot must have a stable number before registration")
    with _shot_finalization_condition:
        if shot.shot_number in _shot_finalization_registered:
            raise ValueError(f"shot #{shot.shot_number} is already registered")
        global _shot_processing_active  # pylint: disable=global-statement
        _shot_processing_active = False
        _shot_finalization_order.append(shot.shot_number)
        _shot_finalization_registered[shot.shot_number] = _RegisteredShotFinalization(
            shot=shot,
            emit_event=emit_event,
            initial_ui_ms=initial_ui_ms,
            deadline_monotonic=(
                time.monotonic() + _SHOT_ENRICHMENT_DEADLINE_S if needs_watchdog else None
            ),
        )
        _ensure_shot_finalization_worker_locked()
        _shot_finalization_condition.notify_all()


def _shot_enrichment_deadline(shot_number: int | None) -> float | None:
    """The registered shot's enrichment deadline (``time.monotonic``), None without one."""
    with _shot_finalization_condition:
        registered = _shot_finalization_registered.get(shot_number)
    return None if registered is None else registered.deadline_monotonic


def _ensure_shot_finalization_worker_locked() -> None:
    """Start the exclusive finalization worker; caller holds the condition lock."""
    global _shot_finalization_worker  # pylint: disable=global-statement

    if _shot_finalization_worker is not None and _shot_finalization_worker.is_alive():
        return
    _shot_finalization_worker = threading.Thread(
        target=_shot_finalization_worker_loop,
        name="shot-finalization",
        daemon=True,
    )
    _shot_finalization_worker.start()


def _shot_finalization_worker_loop() -> None:
    """Exclusively finalize ready, overdue, or capacity-evicted shots in order."""
    global _shot_finalization_running  # pylint: disable=global-statement
    global _shot_finalization_worker  # pylint: disable=global-statement

    current_thread = threading.current_thread()
    try:
        while True:
            with _shot_finalization_condition:
                while True:
                    if not _shot_finalization_order:
                        _shot_finalization_running = False
                        if _shot_finalization_worker is current_thread:
                            _shot_finalization_worker = None
                        _shot_finalization_condition.notify_all()
                        return

                    next_shot_number = _shot_finalization_order[0]
                    registered = _shot_finalization_registered[next_shot_number]
                    pending = _shot_finalization_ready.get(next_shot_number)
                    deadline_expired = (
                        registered.deadline_monotonic is not None
                        and time.monotonic() >= registered.deadline_monotonic
                    )
                    over_capacity = len(_shot_finalization_order) > _SHOT_FINALIZATION_CAPACITY
                    if pending is not None or deadline_expired or over_capacity:
                        _shot_finalization_order.popleft()
                        del _shot_finalization_registered[next_shot_number]
                        _shot_finalization_ready.pop(next_shot_number, None)
                        _shot_finalization_running = True
                        _shot_finalization_condition.notify_all()
                        break

                    _shot_finalization_running = False
                    wait_timeout_s = None
                    if registered.deadline_monotonic is not None:
                        wait_timeout_s = max(
                            0.0,
                            registered.deadline_monotonic - time.monotonic(),
                        )
                    _shot_finalization_condition.wait(timeout=wait_timeout_s)

            if pending is None:
                reason = "deadline" if deadline_expired else "coordinator capacity"
                logger.warning(
                    "[SERVER] Shot #%d optional enrichment exceeded %s; finalizing OPS-only",
                    next_shot_number,
                    reason,
                )
                pending = _PendingShotFinalization(
                    shot=registered.shot,
                    emit_event=registered.emit_event,
                    initial_ui_ms=registered.initial_ui_ms,
                    enrichment=_ShotEnrichmentResult(),
                )
            elif pending.shot is not registered.shot:
                for shot_field in fields(Shot):
                    setattr(
                        registered.shot,
                        shot_field.name,
                        getattr(pending.shot, shot_field.name),
                    )
                pending = replace(pending, shot=registered.shot)

            try:
                _finalize_shot_detected(
                    pending.shot,
                    emit_event=pending.emit_event,
                    initial_ui_ms=pending.initial_ui_ms,
                    enrichment=pending.enrichment,
                )
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error(
                    "[SERVER] Ordered shot finalization failed: %s",
                    error,
                    exc_info=True,
                )
                log_session_error(
                    "Ordered shot finalization failed",
                    component="server",
                    context={
                        "stage": "ordered_finalization",
                        "shot_number": pending.shot.shot_number,
                        "ball_speed_mph": pending.shot.ball_speed_mph,
                    },
                    exc=error,
                )
            finally:
                with _shot_finalization_condition:
                    _shot_finalization_running = False
                    _shot_finalization_condition.notify_all()
    finally:
        with _shot_finalization_condition:
            _shot_finalization_running = False
            if _shot_finalization_worker is current_thread:
                _shot_finalization_worker = None
            _shot_finalization_condition.notify_all()


def _shot_number_for_log(shot: Shot, session_log) -> int:
    """Use detection identity, retaining compatibility for direct helper calls."""
    if shot.shot_number is not None:
        return shot.shot_number
    return session_log.stats.get("shots_detected", 0) + 1


def _run_shutdown_step(name: str, callback) -> None:
    """Run one shutdown step without preventing later hardware cleanup."""
    started = time.monotonic()
    try:
        callback()
    except Exception:
        logger.warning("[SERVER] Shutdown cleanup failed during %s", name, exc_info=True)
    finally:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("[SERVER] Shutdown step %s completed in %.1fms", name, elapsed_ms)


def _stop_ops_speed_inputs() -> None:
    """Release the speed trigger's second input, then decide the witness's pending edges."""
    if ops_speed_listener is not None:
        ops_speed_listener.stop()
    if ops_speed_witness is not None:
        ops_speed_witness.stop()


def _cleanup_hardware_for_shutdown() -> bool:
    """Stop hardware resources and report whether this caller owned cleanup."""
    global shutdown_cleanup_started  # pylint: disable=global-statement

    with shutdown_lock:
        if shutdown_cleanup_started:
            logger.info("[SERVER] Shutdown cleanup already started")
            return False
        shutdown_cleanup_started = True

    if kld7_vertical:
        _run_shutdown_step("K-LD7 vertical stop", kld7_vertical.stop)
    if kld7_horizontal:
        _run_shutdown_step("K-LD7 horizontal stop", kld7_horizontal.stop)
    if inclinometer_service:
        _run_shutdown_step("inclinometer stop", inclinometer_service.stop)
    if trigger_gate_listener:
        _run_shutdown_step("trigger gate input stop", trigger_gate_listener.stop)
    if ops_speed_listener or ops_speed_witness:
        _run_shutdown_step("OPS speed-trigger input and witness stop", _stop_ops_speed_inputs)
    if iwr6843_runtime:
        _run_shutdown_step("IWR6843 stop", iwr6843_runtime.stop)
    if trigger_gate:
        _run_shutdown_step("trigger gate stop", trigger_gate.stop)
    if power_monitor:
        _run_shutdown_step("battery monitor stop", power_monitor.stop)
    if camera_capture_runtime:
        _run_shutdown_step("camera capture stop", camera_capture_runtime.stop)
    if camera_cv_worker is not None:
        _run_shutdown_step("camera CV worker stop", camera_cv_worker.close)

    _run_shutdown_step("launch monitor stop", stop_monitor)

    for connector in sim_connectors:
        _run_shutdown_step(f"simulator connector stop ({connector.name})", connector.stop)

    return True


def _shutdown_process_after_delay(delay_s: float = 0.5) -> None:
    """Give the HTTP/WebSocket response time to flush, then clean up and exit."""
    time.sleep(delay_s)
    if not _cleanup_hardware_for_shutdown():
        return
    logger.info("[SERVER] Goodbye")
    os._exit(0)


# Max smash factor adjustment in degrees (clamped to prevent floor-dependence)
_MAX_SMASH_ADJ_LOW = -3.0  # max degrees to subtract for thin/toe hits
_MAX_SMASH_ADJ_HIGH = 2.0  # max degrees to add for high-face hits

# Degrees of launch angle change per 0.01 smash factor unit
_SMASH_DEG_PER_HUNDREDTH_LOW = 0.4  # below optimal (thin hits penalized more)
_SMASH_DEG_PER_HUNDREDTH_HIGH = 0.2  # above optimal

# Spin rate adjustment: degrees per 500 rpm deviation from optimal
_SPIN_DEG_PER_500RPM = 0.3
# Max spin adjustment in degrees (clamped like smash)
_MAX_SPIN_ADJ = 2.0

# Radar launch sanity guard. These windows are intentionally wide:
# they are meant to catch obvious K-LD7 false positives, not to micromanage
# normal shot-to-shot variation or mishits.
_RADAR_SANITY_LOW_CONF_BONUS_DEG = 5.0


def _react_app_dir() -> Path:
    """Return the best available directory containing the React index file."""
    candidates = [
        Path(app.static_folder) if app.static_folder else FRONTEND_DIST_DIR,
        FRONTEND_DIST_DIR,
        FRONTEND_SOURCE_DIR,
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return candidates[0]


def estimate_launch_angle(
    club: ClubType,
    ball_speed_mph: float,
    club_speed_mph: Optional[float] = None,
    spin_rpm: Optional[float] = None,
) -> tuple:
    """
    Estimate launch angle from club type, ball speed, and optional smash/spin data.

    Uses TrackMan averages as baseline, then adjusts for:
    - Ball speed deviation from club average
    - Smash factor deviation from optimal (if club_speed provided)
    - Spin rate deviation from optimal (if spin_rpm provided)

    Returns (vertical_angle, confidence).
    """
    physics = get_club_physics(club)

    # Slower than average → higher launch, faster → lower launch
    speed_delta = ball_speed_mph - physics.average_ball_speed_mph
    adjustment = -speed_delta * physics.launch_deg_per_mph

    confidence = 0.2

    # Smash factor adjustment: compare actual smash to optimal for this club
    if club_speed_mph is not None and club_speed_mph > 0:
        smash_factor = ball_speed_mph / club_speed_mph
        smash_delta = smash_factor - physics.optimal_smash

        if smash_delta < 0:
            smash_adj = max(_MAX_SMASH_ADJ_LOW, smash_delta * 100 * _SMASH_DEG_PER_HUNDREDTH_LOW)
        else:
            smash_adj = min(_MAX_SMASH_ADJ_HIGH, smash_delta * 100 * _SMASH_DEG_PER_HUNDREDTH_HIGH)
        adjustment += smash_adj

        confidence = 0.35

    # Spin rate adjustment: compare actual spin to optimal for this club/speed
    if spin_rpm is not None and spin_rpm > 0:
        optimal_spin = get_optimal_spin_for_ball_speed(ball_speed_mph, club)
        spin_delta = spin_rpm - optimal_spin
        spin_adj = (spin_delta / 500.0) * _SPIN_DEG_PER_500RPM
        spin_adj = max(-_MAX_SPIN_ADJ, min(_MAX_SPIN_ADJ, spin_adj))
        adjustment += spin_adj

        if confidence >= 0.35:
            confidence = 0.5
        else:
            confidence = 0.35

    launch_angle = max(5.0, round(physics.optimal_launch_deg + adjustment, 1))

    return (launch_angle, confidence)


def _radar_launch_base_delta_deg(club: ClubType) -> float:
    """Return a conservative club-family window for radar launch sanity checks."""
    if club in {ClubType.PW, ClubType.GW, ClubType.SW, ClubType.LW}:
        return 22.0
    if club in {ClubType.IRON_6, ClubType.IRON_7, ClubType.IRON_8, ClubType.IRON_9}:
        return 20.0
    return 18.0


def radar_launch_is_plausible(
    radar_angle_deg: Optional[float],
    club: ClubType,
    ball_speed_mph: float,
    club_speed_mph: Optional[float] = None,
    spin_rpm: Optional[float] = None,
) -> tuple[bool, dict]:
    """Check whether a radar launch angle is plausible for the shot profile.

    This is a wide guardrail meant to reject only obvious radar outliers. When
    the selected club is unknown or the angle is missing, we skip the guard.
    """
    if radar_angle_deg is None or club in {None, ClubType.UNKNOWN} or ball_speed_mph <= 0:
        return True, {
            "skipped": True,
            "rule": None,
            "expected_launch_deg": None,
            "allowed_delta_deg": None,
            "delta_deg": None,
        }

    expected_launch_deg, estimate_conf = estimate_launch_angle(
        club,
        ball_speed_mph,
        club_speed_mph=club_speed_mph,
        spin_rpm=spin_rpm,
    )
    allowed_delta_deg = (
        _radar_launch_base_delta_deg(club)
        + (1.0 - estimate_conf) * _RADAR_SANITY_LOW_CONF_BONUS_DEG
    )
    delta_deg = abs(radar_angle_deg - expected_launch_deg)
    # "rule" names the check that decided the verdict, so a rejection log can
    # say why: angles at or below the expected launch only have to lie within
    # the 0-45 deg bounds, angles above it must stay within allowed_delta_deg.
    if radar_angle_deg <= expected_launch_deg:
        rule = "bounds_0_45"
        plausible = 0.0 <= radar_angle_deg <= 45.0
    else:
        rule = "delta_above_expected"
        plausible = delta_deg <= allowed_delta_deg

    return plausible, {
        "skipped": False,
        "rule": rule,
        "expected_launch_deg": round(expected_launch_deg, 1),
        "allowed_delta_deg": round(allowed_delta_deg, 1),
        "delta_deg": round(delta_deg, 1),
    }


TI_LAUNCH_IMPLAUSIBLE_REASON = "ti_launch_implausible"
_TI_GUARD_RULE_TEXT = {
    "bounds_0_45": "outside the 0-45° bounds",
    "delta_above_expected": "too far above the expected launch (delta > allowed)",
}


def _ti_vertical_launch_is_plausible(angle_deg: Optional[float], shot: Shot) -> tuple[bool, dict]:
    """Apply the radar launch guardrail to an accepted IWR6843 vertical angle.

    The TI angle is checked against the same club/speed profile as the K-LD7
    candidates, before it feeds the launch angle, the ball-speed cosine
    correction and the carry. Only trusted measured spin shapes the expected
    launch; an untrusted spin candidate is ignored like everywhere else.
    """
    return radar_launch_is_plausible(
        radar_angle_deg=angle_deg,
        club=shot.club,
        ball_speed_mph=shot.ball_speed_mph,
        club_speed_mph=shot.club_speed_mph,
        spin_rpm=trusted_spin_rpm(shot),
    )


def _vertical_soft_launch_lane_deg(club: ClubType) -> tuple[float, float]:
    """Return broad club-family lanes for low-confidence vertical radar candidates."""
    if club == ClubType.DRIVER:
        return (4.0, 22.0)
    if club in {ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7}:
        return (5.0, 24.0)
    if club in {ClubType.HYBRID_3, ClubType.HYBRID_5, ClubType.HYBRID_7, ClubType.HYBRID_9}:
        return (6.0, 26.0)
    if club in {ClubType.IRON_2, ClubType.IRON_3, ClubType.IRON_4, ClubType.IRON_5}:
        return (5.0, 25.0)
    if club in {ClubType.IRON_6, ClubType.IRON_7, ClubType.IRON_8, ClubType.IRON_9}:
        return (7.0, 28.0)
    if club in {ClubType.PW, ClubType.GW, ClubType.SW, ClubType.LW}:
        return (10.0, 45.0)
    return (5.0, 35.0)


def _select_vertical_radar_launch(kld7_angle, shot: Shot) -> tuple[bool, dict]:
    """Decide whether a vertical K-LD7 candidate should set the shot launch angle.

    High-confidence candidates keep the existing production behavior. Marginal
    candidates get a stricter second pass: they must agree with the launch
    estimator, sit inside a club-family lane, and avoid very long frame spans
    that often indicate clutter rather than the ball transit. Very low
    confidence candidates are still rejected, but near-threshold candidates
    that pass every other guard are allowed through so the UI can show them as
    low-confidence radar measurements instead of silently replacing them with
    the launch estimator.
    """
    details = {
        "accepted": False,
        "selection_reason": "no_candidate",
        "acceptance_path": None,
        "strict_min_confidence": _MIN_VERTICAL_RADAR_CONFIDENCE,
        "soft_min_confidence": _MIN_VERTICAL_SOFT_RADAR_CONFIDENCE,
        "low_confidence_min_confidence": _MIN_VERTICAL_LOW_CONFIDENCE_RADAR_CONFIDENCE,
        "soft_allowed_delta_deg": _VERTICAL_SOFT_ESTIMATE_DELTA_DEG,
        "soft_max_frame_count": _VERTICAL_SOFT_MAX_FRAME_COUNT,
    }
    if not kld7_angle or kld7_angle.vertical_deg is None:
        return False, details

    if _VERTICAL_RADAR_GATE_BYPASS:
        details["accepted"] = True
        details["selection_reason"] = "gate_bypassed"
        details["acceptance_path"] = "bypass"
        return True, details

    radar_angle_deg = kld7_angle.vertical_deg
    plausible, guard_details = radar_launch_is_plausible(
        radar_angle_deg=radar_angle_deg,
        club=shot.club,
        ball_speed_mph=shot.ball_speed_mph,
        club_speed_mph=shot.club_speed_mph,
        spin_rpm=shot.spin_rpm,
    )
    details.update(guard_details)
    if not plausible:
        details["selection_reason"] = "implausible_launch"
        return False, details

    if kld7_angle.confidence >= _MIN_VERTICAL_RADAR_CONFIDENCE:
        details["accepted"] = True
        details["selection_reason"] = "strict_accept"
        details["acceptance_path"] = "strict"
        return True, details

    if kld7_angle.confidence < _MIN_VERTICAL_LOW_CONFIDENCE_RADAR_CONFIDENCE:
        details["selection_reason"] = "low_confidence"
        return False, details

    if guard_details.get("skipped"):
        details["selection_reason"] = "soft_guard_unavailable"
        return False, details

    lane_min, lane_max = _vertical_soft_launch_lane_deg(shot.club)
    details["soft_lane_min_deg"] = lane_min
    details["soft_lane_max_deg"] = lane_max
    if radar_angle_deg < lane_min or radar_angle_deg > lane_max:
        details["accepted"] = True
        details["selection_reason"] = "marginal_accept:outside_soft_lane"
        details["acceptance_path"] = "marginal"
        return True, details

    delta_deg = guard_details.get("delta_deg")
    if delta_deg is None or delta_deg > _VERTICAL_SOFT_ESTIMATE_DELTA_DEG:
        details["accepted"] = True
        details["selection_reason"] = "marginal_accept:estimator_delta_too_large"
        details["acceptance_path"] = "marginal"
        return True, details

    if kld7_angle.num_frames <= 0:
        details["selection_reason"] = "no_candidate_frames"
        return False, details

    if (
        kld7_angle.num_frames > _VERTICAL_SOFT_MAX_FRAME_COUNT
        and delta_deg > _VERTICAL_SOFT_TIGHT_DELTA_FOR_LONG_FRAME_DEG
    ):
        details["accepted"] = True
        details["selection_reason"] = "marginal_accept:suspicious_frame_span"
        details["acceptance_path"] = "marginal"
        return True, details

    details["accepted"] = True
    if kld7_angle.confidence >= _MIN_VERTICAL_SOFT_RADAR_CONFIDENCE:
        details["selection_reason"] = "soft_accept"
        details["acceptance_path"] = "soft"
    else:
        details["selection_reason"] = "low_confidence_accept"
        details["acceptance_path"] = "low_confidence"
    return True, details


def _select_horizontal_radar_launch(kld7_angle, horizontal_limit: float) -> tuple[bool, dict]:
    """Decide whether a horizontal K-LD7 candidate should set the shot angle."""
    soft_limit = min(_HORIZONTAL_SOFT_ANGLE_LIMIT_DEG, max(horizontal_limit, 0.0))
    details = {
        "accepted": False,
        "selection_reason": "no_candidate",
        "acceptance_path": None,
        "horizontal_limit_deg": horizontal_limit,
        "strict_min_confidence": _MIN_HORIZONTAL_RADAR_CONFIDENCE,
        "soft_min_confidence": _MIN_HORIZONTAL_SOFT_RADAR_CONFIDENCE,
        "soft_angle_limit_deg": soft_limit,
        "soft_max_frame_count": _HORIZONTAL_SOFT_MAX_FRAME_COUNT,
        "near_limit_min_confidence": _HORIZONTAL_NEAR_LIMIT_MIN_CONFIDENCE,
        "near_limit_max_frame_count": _HORIZONTAL_NEAR_LIMIT_MAX_FRAMES,
    }
    if not kld7_angle or kld7_angle.horizontal_deg is None:
        return False, details

    abs_angle = abs(kld7_angle.horizontal_deg)
    if abs_angle > horizontal_limit:
        details["selection_reason"] = "outside_horizontal_limit"
        return False, details

    near_limit_angle = horizontal_limit * _HORIZONTAL_NEAR_LIMIT_FRACTION
    if (
        abs_angle >= near_limit_angle
        and kld7_angle.num_frames <= _HORIZONTAL_NEAR_LIMIT_MAX_FRAMES
        and kld7_angle.confidence < _HORIZONTAL_NEAR_LIMIT_MIN_CONFIDENCE
    ):
        details["selection_reason"] = "weak_near_limit"
        details["near_limit_angle_deg"] = round(near_limit_angle, 1)
        return False, details

    if kld7_angle.confidence >= _MIN_HORIZONTAL_RADAR_CONFIDENCE:
        details["accepted"] = True
        details["selection_reason"] = "strict_accept"
        details["acceptance_path"] = "strict"
        return True, details

    if kld7_angle.confidence < _MIN_HORIZONTAL_SOFT_RADAR_CONFIDENCE:
        details["selection_reason"] = "low_confidence"
        return False, details

    if abs_angle > soft_limit:
        details["selection_reason"] = "outside_soft_lane"
        return False, details

    if kld7_angle.num_frames <= 0:
        details["selection_reason"] = "no_candidate_frames"
        return False, details

    if kld7_angle.num_frames > _HORIZONTAL_SOFT_MAX_FRAME_COUNT:
        details["selection_reason"] = "suspicious_frame_span"
        return False, details

    details["accepted"] = True
    details["selection_reason"] = "soft_accept"
    details["acceptance_path"] = "soft"
    return True, details


def _ensure_user_facing_launch_angles(shot: Shot) -> None:
    """Provide a vertical estimate without inventing a horizontal measurement."""
    estimated: tuple[float, float] | None = None

    if shot.launch_angle_vertical is None:
        estimated = estimate_launch_angle(
            shot.club,
            shot.ball_speed_mph,
            club_speed_mph=shot.club_speed_mph,
            spin_rpm=trusted_spin_rpm(shot),
        )
        shot.launch_angle_vertical = estimated[0]
        shot.launch_angle_confidence = estimated[1]
        shot.launch_angle_vertical_confidence = estimated[1]
        shot.launch_angle_vertical_source = "estimated"
        shot.angle_source = "estimated"
        logger.info(
            "[SERVER] Angle source: estimated (%.1f°, conf=%.0f%%)",
            estimated[0],
            estimated[1] * 100,
        )

    if shot.launch_angle_horizontal is None:
        camera_capture_enabled = bool(camera_capture_config.get("enabled"))
        if iwr6843_runtime is not None or camera_capture_enabled:
            logger.info("[SERVER] Horizontal angle unavailable; no measured trajectory")
            return
        # Preserve the legacy neutral estimate for installations that have no
        # horizontal-capable TI/camera pipeline at all.
        shot.launch_angle_horizontal = 0.0
        if shot.launch_angle_horizontal_confidence is None:
            if estimated is None:
                estimated = estimate_launch_angle(
                    shot.club,
                    shot.ball_speed_mph,
                    club_speed_mph=shot.club_speed_mph,
                    spin_rpm=shot.spin_rpm,
                )
            shot.launch_angle_horizontal_confidence = estimated[1]
        shot.launch_angle_horizontal_source = "estimated"
        if shot.angle_source is None:
            shot.angle_source = "estimated"
        logger.info("[SERVER] Horizontal angle source: neutral estimate (0.0°)")


# K-LD7 produces ~34 RADC frames/sec at 3 Mbaud. With buffer_seconds=6
# the steady-state buffer is ~204 frames. If the snapshot at shot time
# is dramatically less than that, the radar's stream rate dropped.
# Surface as WARN so cabling/USB issues are visible without a replay.
_KLD7_FRAME_HZ = 34.0
_KLD7_BUFFER_SECONDS = 6.0
_KLD7_BUFFER_UNDERFILL_FRAC = 0.5
_KLD7_POST_SHOT_CAPTURE_DELAY_S = 0.18
_MIN_VERTICAL_RADAR_CONFIDENCE = 0.80
_MIN_VERTICAL_SOFT_RADAR_CONFIDENCE = 0.68
_MIN_VERTICAL_LOW_CONFIDENCE_RADAR_CONFIDENCE = 0.65
# Test-mode escape hatch (--kld7-vertical-raw): when True,
# _select_vertical_radar_launch accepts ANY vertical radar candidate and skips
# every guard, so the UI shows the radar's launch angle for every shot the
# estimator produces. Default False leaves production behavior unchanged.
_VERTICAL_RADAR_GATE_BYPASS = False
# Display confidence for radar measurements that pass the physics guard but
# trip a soft consistency guard (club lane / estimator delta / frame span):
# shown as radar with a single confidence dot (<0.4) instead of silently
# being replaced by the club estimate.
_VERTICAL_MARGINAL_DISPLAY_CONFIDENCE = 0.38
_VERTICAL_SOFT_ESTIMATE_DELTA_DEG = 4.5
_VERTICAL_SOFT_MAX_FRAME_COUNT = 40
_VERTICAL_SOFT_TIGHT_DELTA_FOR_LONG_FRAME_DEG = 2.0
_MIN_HORIZONTAL_RADAR_CONFIDENCE = 0.40
_MIN_HORIZONTAL_SOFT_RADAR_CONFIDENCE = 0.30
_HORIZONTAL_SOFT_ANGLE_LIMIT_DEG = 5.0
_HORIZONTAL_SOFT_MAX_FRAME_COUNT = 40
_HORIZONTAL_NEAR_LIMIT_FRACTION = 0.80
_HORIZONTAL_NEAR_LIMIT_MAX_FRAMES = 2
_HORIZONTAL_NEAR_LIMIT_MIN_CONFIDENCE = 0.80


def _maybe_wait_for_kld7_post_shot_frames(shot_timestamp: float) -> None:
    """Let the K-LD7 stream collect post-impact RADC frames for extraction.

    TrackMan test logs showed immediate snapshots ending at or just before
    the OPS impact timestamp, leaving the configured post-shot extraction
    window empty. Wait only until the bounded target time; if processing is
    already past that point, this adds no latency.

    ``shot_timestamp`` is wall-clock, so the remaining delay is read off the
    wall clock once and capped at the configured delay: a backward NTP step
    between impact and now must not turn this into an hours-long sleep.
    ``time.sleep`` is a duration and immune to steps during the wait.
    """
    target_time = shot_timestamp + _KLD7_POST_SHOT_CAPTURE_DELAY_S
    delay_s = min(target_time - time.time(), _KLD7_POST_SHOT_CAPTURE_DELAY_S)
    if delay_s <= 0:
        return
    logger.info(
        "[SERVER] Waiting %.0fms for post-impact K-LD7 RADC frames",
        delay_s * 1000.0,
    )
    time.sleep(delay_s)


def _warn_if_kld7_buffer_underfilled(orientation: str, frame_count: int) -> None:
    """Log a WARNING when the K-LD7 ring-buffer snapshot is far below
    the expected steady-state size at shot time.
    """
    expected = int(_KLD7_FRAME_HZ * _KLD7_BUFFER_SECONDS)
    if expected <= 0 or frame_count <= 0:
        return
    if frame_count < expected * _KLD7_BUFFER_UNDERFILL_FRAC:
        logger.warning(
            "[SERVER] K-LD7 %s buffer underfilled: %d/%d frames (%.0f%%) — "
            "stream rate dropped, check USB cabling and contention.",
            orientation,
            frame_count,
            expected,
            100.0 * frame_count / expected,
        )


def _kld7_angle_log_payload(
    angle,
    axis_field: str,
    selection_details: Optional[dict] = None,
) -> Optional[dict]:
    """Build the compact K-LD7 angle payload used in session logs."""
    if angle is None:
        return None

    payload = {
        axis_field: getattr(angle, axis_field),
        "confidence": angle.confidence,
        "detection_class": angle.detection_class,
        "magnitude": angle.magnitude,
        "num_frames": angle.num_frames,
        "frames_examined": angle.frames_examined,
        "frames_available": angle.frames_available,
        "frames_ignored_stale": angle.frames_ignored_stale,
    }
    radc_selection = getattr(angle, "radc_selection", None)
    if radc_selection:
        payload["radc_selection"] = radc_selection
    if selection_details:
        payload.update(selection_details)
    return payload


def _session_start_config() -> dict:
    """Return hardware configuration recorded at session start."""
    config = radar_config.copy()
    config["iwr6843"] = dict(iwr6843_runtime_config)
    config["camera_capture"] = dict(camera_capture_config)
    config["inclinometer"] = dict(inclinometer_runtime_config)
    config["angle_reference"] = angle_reference.state()
    # The scale only feeds the ballistic carry model; with --no-ballistics it
    # has no effect, so record None instead of a scale that changed nothing.
    # It applies to irons and wedges only (TYPICAL_SPIN_SCALED_CLUBS).
    config["typical_spin_scale"] = typical_spin_scale if ballistics_enabled else None
    config["typical_spin_scale_clubs"] = "irons_wedges" if ballistics_enabled else None
    config["typical_spin_table"] = typical_spin_table if ballistics_enabled else None
    config["range_ball_drag_slope"] = range_ball_drag_slope if ballistics_enabled else None
    if trigger_gate_config.get("enabled"):
        config["trigger_gate"] = dict(trigger_gate_config)
    radar = getattr(monitor, "radar", None)
    if ops_speed_runtime_config.get("requested"):
        # --shot-trigger ops-speed*: on (flags, firmware, raw ??) or why it was refused.
        config["ops_speed"] = {
            key: value for key, value in ops_speed_runtime_config.items() if key != "requested"
        }
        if ops_speed_runtime_config.get("enabled") and isinstance(radar, OPS243Radar):
            config["ops_speed"]["speed_trigger_setup"] = radar.last_speed_trigger_setup
    if isinstance(radar, OPS243Radar):
        # --ops-fast-rearm: which re-arm timings this session's OPS ran with.
        config["ops_rearm"] = {"fast": radar.fast_rearm, **radar.rearm_timing()}
    processor = getattr(monitor, "processor", None)
    if isinstance(processor, RollingBufferProcessor):
        # --sample-rate / --sound-pre-trigger: the buffer's time base. Each
        # rolling_buffer_capture also carries its own sample_rate_hz.
        config["ops_buffer"] = {
            "sample_rate_ksps": processor.SAMPLE_RATE / 1000,
            "pre_trigger_segments": getattr(
                getattr(monitor, "trigger", None), "pre_trigger_segments", None
            ),
            "doppler_unfolding": processor.doppler_unfolding,
            "max_outbound_mph": round(processor.max_outbound_speed_mph, 1),
        }
    config["power"] = {
        "enabled": battery_provider is not None,
        "provider": battery_provider,
    }
    return config


ball_speed_correction_enabled = False
ball_speed_correction_distance_ft = 5.5
ball_speed_correction_ball_above_radar_ft = -4.0 / 12.0
calculated_spin_enabled = False


# The chained camera (Lucas-Kanade) club delivery stays in the session log but is
# no display value: under backlight it tracks static texture and reads a constant
# path of about -20 deg (13 of 13 range shots). Its status would otherwise stand in
# a tile for a value that is not shown, and hide an accepted TI path.
_LOG_ONLY_CHAINED_DELIVERY_FIELDS = (
    "experimental_fused_attack_angle_deg",
    "experimental_fused_club_path_deg",
    "experimental_fused_status",
    "experimental_fused_attack_angle_confidence",
    "experimental_fused_club_path_confidence",
)
# TI club values the payload shows only when TI accepted them; an unaccepted
# candidate (path sd 17.5 deg, attack angles -23 to -56 deg) stays in the session log.
# With the value withheld only a rejection status stays: a candidate status would
# caption the empty tile as if a value existed.
_ACCEPTED_ONLY_TI_CLUB_FIELDS = (
    ("experimental_club_path_deg", "experimental_club_path_status"),
    ("experimental_attack_angle_deg", "experimental_attack_angle_status"),
)


def _accepted_status(status: object) -> bool:
    """Whether an experimental estimator's status means it accepted its value."""
    return isinstance(status, str) and status.startswith("accepted")


def _rejected_status(status: object) -> bool:
    """Whether an experimental estimator's status names why it rejected its value."""
    return isinstance(status, str) and status.startswith("rejected_")


# Calculated club-data numbers (``openflight.club_data``), shown to 0.1 (the impact
# scale to 0.01 mm/px). They are only set where their gate passed, so nothing is
# withheld here.
_CLUB_DATA_TEXT_FIELDS = (
    "club_data_status",
    "club_data_reason",
    "face_to_path_class",
    "spin_axis_class",
    "impact_zone",
    "impact_method",
)
_CLUB_DATA_ROUNDED_FIELDS = tuple(
    name
    for name in CLUB_DATA_SHOT_FIELDS
    if name not in (*_CLUB_DATA_TEXT_FIELDS, "impact_scale_mm_per_px")
)


def shot_to_dict(shot: Shot) -> dict:
    """Return the UI shot schema with display-oriented rounding.

    Unvalidated club values are withheld here, not on the shot, so the session
    log (``Shot.to_dict``) keeps them for offline analysis. The calculated club
    data is gated where it is set and only rounded here.
    """
    data = shot.to_dict()
    # The carry span is computed after the shot is logged (see _start_carry_span), so it
    # is a UI field only; Shot.to_dict, the session log schema, leaves it out.
    data["carry_span_min_yards"] = shot.carry_span_min_yards
    data["carry_span_max_yards"] = shot.carry_span_max_yards
    for log_only_field in ("mode", "readings", "readings_count"):
        data.pop(log_only_field)
    for chained_field in _LOG_ONLY_CHAINED_DELIVERY_FIELDS:
        data[chained_field] = None
    for value_field, status_field in _ACCEPTED_ONLY_TI_CLUB_FIELDS:
        if not _accepted_status(data[status_field]):
            data[value_field] = None
            if not _rejected_status(data[status_field]):
                data[status_field] = None
    for rounded_field, digits in {
        "ball_speed_mph": 1,
        "ball_speed_raw_mph": 1,
        "club_speed_mph": 1,
        "smash_factor": 2,
        "estimated_carry_yards": None,
        "spin_rpm": None,
        "spin_rpm_measured": None,
        "spin_confidence": 2,
        "spin_multipath_fade_hz": 2,
        "spin_snr": 2,
        "spin_modulation_depth": 4,
        "spin_peak_freq_hz": 2,
        "spin_candidate_rpm": None,
        "spin_seam_cycles": 2,
        "spin_phase_rpm": None,
        "spin_phase_snr": 2,
        "spin_phase_agreement_pct": 1,
        "carry_spin_adjusted": None,
        "carry_spin_rpm": None,
        "carry_lateral_yards": None,
        "apex_yards": 1,
        "carry_span_min_yards": None,
        "carry_span_max_yards": None,
        "roll_yards": None,
        "total_yards": None,
        **dict.fromkeys(_CLUB_DATA_ROUNDED_FIELDS, 1),
        "impact_scale_mm_per_px": 2,
    }.items():
        if data[rounded_field] is not None:
            data[rounded_field] = (
                round(data[rounded_field], digits)
                if digits is not None
                else round(data[rounded_field])
            )
    data["carry_range"] = [round(value) for value in data["carry_range"]]
    return data


@app.route("/")
def index():
    """Serve the React app."""
    return send_from_directory(_react_app_dir(), "index.html")


@app.route("/display", strict_slashes=False)
def display():
    """Serve the React app for TV display mode."""
    return send_from_directory(_react_app_dir(), "index.html")


@app.route("/companion", strict_slashes=False)
def companion():
    """Serve the React app for the read-only phone/tablet companion view."""
    return send_from_directory(_react_app_dir(), "index.html")


@app.route("/pair", strict_slashes=False)
def pair_page():
    """Serve the React app for the device pairing page that pairing QR codes open."""
    return send_from_directory(_react_app_dir(), "index.html")


@app.route("/<path:path>")
def static_files(path):
    """Serve static files."""
    return send_from_directory(app.static_folder, path)


@app.route("/api/session-history")
def api_session_history():
    """List saved sessions independently of the active shot monitor."""
    session_log = get_session_logger()
    log_dir = session_log.log_dir if session_log else SessionLogger.DEFAULT_LOG_DIR
    response = jsonify({"days": HISTORY_DAYS, "sessions": list_sessions(log_dir)})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/session-history/<session_id>")
def api_saved_session(session_id):
    """Read compact historical metrics without mutating active shots.

    Each shot's simulated flight path and roll path are left out unless the request asks for
    them with ``?flight_path=1`` (the analysis page reads every session and has no use for
    the paths). Roll and Total come with every shot.
    """
    global camera_replay_manager  # pylint: disable=global-statement
    session_log = get_session_logger()
    log_dir = session_log.log_dir if session_log else SessionLogger.DEFAULT_LOG_DIR
    try:
        # Saved clips remain usable even if today's camera failed to initialize.
        if camera_replay_manager is None:
            from .camera.replay import CameraReplayManager

            with _history_replay_init_lock:
                if camera_replay_manager is None:
                    camera_replay_manager = CameraReplayManager(log_dir)
        response = jsonify(
            read_saved_session(
                log_dir,
                session_id,
                replay_manager=camera_replay_manager,
                flight_paths=request.args.get("flight_path") == "1",
            )
        )
    except (OSError, ValueError):
        return jsonify({"error": "Saved session not available"}), 404
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/session-history/<session_id>/shots/<shot_timestamp>", methods=["DELETE"])
def api_delete_saved_shot(session_id, shot_timestamp):
    """Delete one shot of a saved session by appending a shot_deleted line to its log."""
    session_log = get_session_logger()
    log_dir = session_log.log_dir if session_log else SessionLogger.DEFAULT_LOG_DIR
    try:
        path, already_deleted = find_saved_shot(log_dir, session_id, shot_timestamp)
    except FileNotFoundError:
        return jsonify({"error": "Shot not found"}), 404
    except ValueError:
        return jsonify({"error": "Invalid session or shot"}), 400
    if already_deleted:
        return "", 204
    if session_log is not None and path == session_log.current_path:
        with _demo_lock:
            session_log.log_shot_deleted(shot_timestamp)
            _delete_session_row(shot_timestamp)
            socketio.emit("session_state", _session_state_payload())
    else:
        append_shot_deleted(path, shot_timestamp)
    return "", 204


@app.route("/api/club-averages")
def api_club_averages():
    """Average Total and carry of one club over the saved sessions of the history window."""
    club = request.args.get("club", "")
    if not club:
        return jsonify({"error": "club is required"}), 400
    session_log = get_session_logger()
    log_dir = session_log.log_dir if session_log else SessionLogger.DEFAULT_LOG_DIR
    profile_id = request.args.get("profile") or None
    response = jsonify(club_averages(log_dir, club, profile_id=profile_id))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/shutdown", methods=["POST"])
def api_shutdown():
    """Cleanly shut down the server via REST API."""
    logger.info("[SERVER] Shutdown requested via REST API")
    threading.Thread(target=_shutdown_process_after_delay, daemon=True).start()
    return {"status": "shutting_down"}, 200


IWR6843_TILT_EVENT = "iwr6843_tilt_calibration"


def _public_phone_tilt_record(record: dict | None) -> dict | None:
    """The calibration record as clients see it (without the raw request copy)."""
    if record is None:
        return None
    return {key: value for key, value in record.items() if key != "request"}


def _iwr6843_tilt_status() -> dict:
    """Configured TI tilt, where it came from, and the phone calibration behind it."""
    enabled = iwr6843_runtime is not None
    record = iwr6843_runtime_config.get("phone_tilt_calibration") if enabled else None
    if not enabled:
        try:
            record = load_phone_tilt_calibration(_phone_tilt_path())
        except (OSError, ValueError):
            record = None
    last = _last_effective_iwr_tilt or {}
    return {
        "enabled": enabled,
        "configured_iwr_tilt_deg": (
            round(math.degrees(iwr6843_runtime.calibration.tilt_rad), 4) if enabled else None
        ),
        "tilt_source": iwr6843_runtime_config.get("tilt_source") if enabled else None,
        "fallback_iwr_tilt_deg": iwr6843_runtime_config.get("tilt_fallback_deg"),
        "fallback_source": iwr6843_runtime_config.get("tilt_fallback_source"),
        "case_top_to_boresight_deg": CASE_TOP_TO_BORESIGHT_DEG,
        "inclinometer_enabled": inclinometer_service is not None,
        "last_effective_iwr_tilt_deg": last.get("tilt_deg"),
        "last_effective_at": last.get("at"),
        "calibration": _public_phone_tilt_record(record),
    }


def _apply_iwr6843_tilt(tilt_deg: float, source: str, record: dict | None) -> None:
    """Swap in a new configured tilt; callers hold ``_iwr6843_tilt_lock``.

    The runtime's calibration is replaced as a whole, so a shot in flight sees
    either the old or the new tilt, never a partial update.
    """
    iwr6843_runtime.calibration = replace(
        iwr6843_runtime.calibration, tilt_rad=math.radians(tilt_deg)
    )
    iwr6843_runtime_config.update(
        {"tilt_deg": tilt_deg, "tilt_source": source, "phone_tilt_calibration": record}
    )


def _log_iwr6843_tilt_change(source: str) -> None:
    session_logger = get_session_logger()
    if session_logger:
        session_logger.log_config_change({"iwr6843": dict(iwr6843_runtime_config)}, source=source)


def _emit_iwr6843_tilt_state(event: str) -> dict:
    payload = {"event": event, **_iwr6843_tilt_status()}
    socketio.emit(IWR6843_TILT_EVENT, payload)
    return payload


def _tilt_error(code: str, message: str, status: int, **extra):
    return {"error": code, "message": message, **extra}, status


def apply_iwr6843_phone_tilt(payload):  # pylint: disable=too-many-return-statements
    """Validate a two-position phone measurement, then persist and apply it.

    Nothing changes unless every check passes and the file is written.
    """
    if iwr6843_runtime is None:
        return _tilt_error("iwr6843_not_enabled", "TI IWR6843 radar is not enabled", 409)
    try:
        measurement = measure_phone_tilt(payload)
    except PhoneTiltRejected as rejection:
        return rejection.to_dict(), 422

    inclinometer_pitch_deg = None
    if inclinometer_service is not None:
        try:
            selection = inclinometer_service.wait_for_stable(timeout_s=2.0)
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.warning("[SERVER] LIS3DH failed during phone tilt calibration: %s", error)
            return _tilt_error(
                "inclinometer_error", "Could not read the enclosure sensor; try again", 409
            )
        if selection.snapshot is None:
            return _tilt_error(
                "inclinometer_unstable",
                "The enclosure sensor is not stable; keep the rig still and try again",
                409,
                inclinometer_status=selection.status,
            )
        inclinometer_pitch_deg = float(selection.snapshot.calibrated_pitch_deg)

    record = build_phone_tilt_record(
        payload,
        measurement,
        inclinometer_pitch_deg=inclinometer_pitch_deg,
        applied_at=datetime.now().astimezone().isoformat(),
    )
    configured = record["configured_iwr_tilt_deg"]
    if abs(configured) > 45.0:
        return _tilt_error(
            "configured_tilt_out_of_range",
            "Derived TI tilt relative to the enclosure is outside -45..45 deg",
            422,
            value=configured,
        )

    with _iwr6843_tilt_lock:
        try:
            save_phone_tilt_calibration(record, _phone_tilt_path())
        except OSError as error:
            logger.warning("[SERVER] Could not save the phone tilt calibration: %s", error)
            return _tilt_error("save_failed", "OpenFlight could not save the calibration", 500)
        _apply_iwr6843_tilt(configured, "phone", record)

    _log_iwr6843_tilt_change("phone_tilt_calibration")
    logger.info(
        "[SERVER] Applied phone tilt calibration: case tilt %+.3fdeg, radar tilt %.3fdeg, "
        "LIS3DH pitch %s, configured TI tilt %.3fdeg (phone bias %+.3fdeg)",
        measurement.case_tilt_deg,
        measurement.radar_tilt_deg,
        "off" if inclinometer_pitch_deg is None else f"{inclinometer_pitch_deg:+.3f}deg",
        configured,
        measurement.phone_bias_deg,
    )
    state = _emit_iwr6843_tilt_state("applied")
    return {"status": "applied", **state}, 200


def remove_iwr6843_phone_tilt():
    """Delete the saved phone calibration and fall back to the CLI/JSON tilt now."""
    with _iwr6843_tilt_lock:
        try:
            removed = delete_phone_tilt_calibration(_phone_tilt_path())
        except OSError as error:
            logger.warning("[SERVER] Could not delete the phone tilt calibration: %s", error)
            return _tilt_error("delete_failed", "OpenFlight could not delete the calibration", 500)
        if iwr6843_runtime is not None:
            _apply_iwr6843_tilt(
                float(iwr6843_runtime_config["tilt_fallback_deg"]),
                iwr6843_runtime_config["tilt_fallback_source"],
                None,
            )
    if iwr6843_runtime is not None:
        _log_iwr6843_tilt_change("phone_tilt_calibration_removed")
        now = (
            f"{math.degrees(iwr6843_runtime.calibration.tilt_rad):.3f}deg "
            f"from {iwr6843_runtime_config.get('tilt_source')}"
        )
    else:
        now = "not enabled"
    logger.info(
        "[SERVER] Phone tilt calibration %s; TI tilt now %s",
        "removed" if removed else "was not saved",
        now,
    )
    state = _emit_iwr6843_tilt_state("removed")
    return {"status": "removed", "removed": removed, **state}, 200


@app.route("/api/calibration/iwr6843/phone-tilt", methods=["GET", "POST", "DELETE"])
def api_iwr6843_phone_tilt():
    """Read, apply or remove the phone-measured TI tilt (kiosk or paired device only)."""
    if request.method == "GET":
        response = jsonify(_iwr6843_tilt_status())
        response.headers["Cache-Control"] = "no-store"
        return response
    if request.method == "DELETE":
        return remove_iwr6843_phone_tilt()
    return apply_iwr6843_phone_tilt(request.get_json(silent=True))


def _warn_if_phone_tilt_inclinometer_mismatch(inclinometer_active: bool) -> None:
    """Warn when the phone calibration was taken with a different LIS3DH state.

    Measured with the LIS3DH, the configured tilt excludes the case pitch the
    LIS3DH adds per shot; measured without it, the tilt already includes it.
    Switching ``--inclinometer`` afterwards double-counts or drops that pitch.
    """
    if iwr6843_runtime_config.get("tilt_source") != "phone":
        return
    record = iwr6843_runtime_config.get("phone_tilt_calibration") or {}
    measured_with = record.get("inclinometer_pitch_deg") is not None
    if measured_with != inclinometer_active:
        message = (
            "WARNING: the phone tilt calibration was measured "
            f"{'with' if measured_with else 'without'} the inclinometer but it is now "
            f"{'active' if inclinometer_active else 'off'}; recalibrate the TI tilt"
        )
        logger.warning("[SERVER] %s", message)
        print(message)


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PUBLIC_ENDPOINTS = frozenset(
    {
        "index",
        "display",
        "companion",
        "pair_page",
        "static",
        "static_files",
        "pairing_status",
        "pairing_exchange",
    }
)
_LOCAL_ONLY_ENDPOINTS = frozenset({"pairing_code", "pairing_revoke"})


@app.before_request
def require_paired_device():
    """Serve the app shell to anyone; everything else needs the kiosk or a paired device."""
    if request.method not in _SAFE_METHODS and not origin_allowed(
        request.headers.get("Origin"), request.environ
    ):
        return jsonify({"error": "origin_not_allowed"}), 403
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    local = is_local_environ(request.environ)
    if request.endpoint in _LOCAL_ONLY_ENDPOINTS:
        return None if local else (jsonify({"error": "local_only"}), 403)
    if local or _request_is_paired():
        return None
    return jsonify({"error": "pairing_required"}), 401


def _request_is_paired() -> bool:
    """Whether the request carries the current device secret as its cookie."""
    try:
        return get_device_secret().matches(request.cookies.get(DEVICE_COOKIE_NAME))
    except OSError as error:
        logger.error("[PAIRING] Device secret unavailable: %s", error)
        return False


def _set_device_cookie(response: Response, secret: str) -> None:
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        secret,
        max_age=DEVICE_COOKIE_MAX_AGE_S,
        path="/",
        secure=request.is_secure,
        httponly=True,
        samesite="Strict",
    )


@app.route("/api/pairing/status")
def pairing_status():
    """Tell the UI whether to show the pairing screen, refreshing a paired cookie."""
    if is_local_environ(request.environ):
        response = jsonify({"state": "local"})
    elif _request_is_paired():
        response = jsonify({"state": "paired"})
        _set_device_cookie(response, get_device_secret().value())
    else:
        response = jsonify({"state": "unpaired"})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/pairing/exchange", methods=["POST"])
def pairing_exchange():
    """Trade a one-time code shown on the kiosk for the device cookie."""
    try:
        secret = get_device_secret().value()
    except OSError as error:
        logger.error("[PAIRING] Device secret unavailable: %s", error)
        return jsonify({"error": "pairing_unavailable"}), 503
    payload = request.get_json(silent=True)
    code = payload.get("code") if isinstance(payload, dict) else None
    outcome = pairing_codes.exchange(code, request.remote_addr or "")
    if outcome.status == "rate_limited":
        response = jsonify({"error": "too_many_attempts", "retry_after_s": outcome.retry_after_s})
        response.headers["Retry-After"] = str(outcome.retry_after_s)
        return response, 429
    if outcome.status != "accepted":
        if outcome.code_closed:
            socketio.emit("pairing_code_closed", {"reason": "locked"})
        return jsonify({"error": "invalid_code"}), 403
    logger.info("[PAIRING] Paired a device at %s", request.remote_addr)
    socketio.emit("pairing_code_closed", {"reason": "paired"})
    response = jsonify({"state": "paired"})
    _set_device_cookie(response, secret)
    return response


@app.route("/api/pairing/code", methods=["POST"])
def pairing_code():
    """Issue a new one-time code, with QR links, for the kiosk's pairing dialog."""
    issued = pairing_codes.issue()
    response = jsonify(
        {
            "code": issued.code,
            "expires_in_s": issued.expires_in_s,
            "links": pairing_links(issued.code, request.environ.get("SERVER_PORT", "8080")),
            "wifi": (
                {
                    "ssid": hotspot_ssid or None,
                    "password": hotspot_password,
                    "qr_svg": (
                        wifi_join_qr_svg(hotspot_ssid, hotspot_password) if hotspot_ssid else None
                    ),
                }
                if hotspot_password
                else None
            ),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/pairing/revoke", methods=["POST"])
def pairing_revoke():
    """Unpair every device: rotate the secret and drop their live connections."""
    try:
        with _pairing_lock:
            get_device_secret().rotate()
            sids = list(_paired_socket_sids)
            _paired_socket_sids.clear()
    except OSError as error:
        logger.error("[PAIRING] Could not rotate the device secret: %s", error)
        return jsonify({"error": "revoke_failed"}), 503
    pairing_codes.clear()
    for sid in sids:
        socketio.server.disconnect(sid, namespace="/")
    logger.info("[PAIRING] Unpaired all devices and closed %d connection(s)", len(sids))
    return jsonify({"status": "revoked"})


def init_camera_capture(
    *,
    output_dir: str | Path,
    gpio_pin: int,
    width: int,
    height: int,
    fps: float,
    pre_ms: float,
    post_ms: float,
    exposure_us: int,
    gain: float,
    stream: str,
    rotate_180: bool,
    mirror_horizontal: bool,
    roll_correction_deg: float,
    scaler_crop: tuple[int, int, int, int] | None,
    mount_height_m: float,
    lateral_offset_m: float,
    horizontal_offset_deg: float,
    use_gpio_trigger: bool,
    forward_offset_m: float = 0.0,
    yaw_from_ball: bool = False,
    buffer_count: int | None = None,
    cv_in_process: bool = False,
    ball_locator: str = "disappeared",
) -> bool:
    """Initialize passive high-speed camera capture for offline alignment."""
    global camera_capture_runtime, camera_capture_config  # pylint: disable=global-statement
    global camera_replay_manager  # pylint: disable=global-statement
    global camera_reference_ball_tracker  # pylint: disable=global-statement
    global camera_ball_flight_reference_tracker  # pylint: disable=global-statement
    global camera_ball_flight_ground_anchor  # pylint: disable=global-statement
    global camera_lowlight_reference_tracker, camera_lowlight_ground_anchor
    global camera_cv_worker  # pylint: disable=global-statement
    try:
        from .camera.capture_runtime import (
            DEFAULT_BUFFER_COUNT,
            CameraCaptureRuntime,
            CameraCaptureSettings,
            without_withdrawn_capture_mode,
        )

        settings = CameraCaptureSettings(
            width=width,
            height=height,
            fps=fps,
            pre_ms=pre_ms,
            post_ms=post_ms,
            exposure_us=exposure_us,
            gain=gain,
            stream=stream,
            rotate_180=rotate_180,
            mirror_horizontal=mirror_horizontal,
            roll_correction_deg=roll_correction_deg,
            scaler_crop=scaler_crop,
            gpio_pin=gpio_pin,
            auto_exposure_state_path=(
                Path.home() / ".config" / "openflight" / "camera-exposure.json"
            ),
            buffer_count=DEFAULT_BUFFER_COUNT if buffer_count is None else buffer_count,
        )
        # A stored crop-500/crop-600 (320x200) starts as full-300: that sensor mode
        # delivers flat frames on the Pi.
        settings = without_withdrawn_capture_mode(settings)
        camera_capture_runtime = CameraCaptureRuntime(
            output_dir=output_dir,
            settings=settings,
            use_gpio_trigger=use_gpio_trigger,
            edge_timing=sound_edge_timing if use_gpio_trigger else None,
        )
        camera_capture_runtime.start()
        # The trigger check searches the tapped ball's window, as the ball flight does.
        camera_capture_runtime.set_ball_mark_source(_camera_ball_mark_px)
        settings = camera_capture_runtime.settings
        from .camera.replay import CameraReplayManager

        camera_replay_manager = CameraReplayManager(output_dir)
        from openflight.camera.club_delivery import (  # noqa: PLC0415
            ReferenceBallTracker,
        )
        from openflight.camera.departed_ball import (  # noqa: PLC0415
            GroundBallAnchor,
        )

        _warn_if_clips_too_short_for_locator(settings, ball_locator)
        camera_reference_ball_tracker = ReferenceBallTracker()
        camera_ball_flight_reference_tracker = ReferenceBallTracker()
        camera_ball_flight_ground_anchor = GroundBallAnchor()
        camera_lowlight_reference_tracker = ReferenceBallTracker()
        camera_lowlight_ground_anchor = GroundBallAnchor()
        if not cv_in_process:
            from .camera.cv_worker import CameraCvWorker  # noqa: PLC0415

            camera_cv_worker = CameraCvWorker(timeout_s=_CAMERA_CV_WORKER_TIMEOUT_S)
        camera_capture_config = {
            "enabled": True,
            "output_dir": str(Path(output_dir).expanduser()),
            "gpio_pin_bcm": gpio_pin,
            "trigger_source": "gpio" if use_gpio_trigger else "iwr6843_fanout",
            "width": settings.width,
            "height": settings.height,
            "fps": settings.fps,
            "pre_ms": settings.pre_ms,
            "post_ms": settings.post_ms,
            "pre_frames": settings.pre_frames,
            "post_frames": settings.post_frames,
            "exposure_us": settings.exposure_us,
            "gain": settings.gain,
            "auto_exposure_enabled": settings.auto_exposure,
            "stream": settings.stream,
            "rotate_180": settings.rotate_180,
            "mirror_horizontal": settings.mirror_horizontal,
            "roll_correction_deg": settings.roll_correction_deg,
            "scaler_crop": settings.scaler_crop,
            "mount_height_m": mount_height_m,
            "lateral_offset_m": lateral_offset_m,
            "forward_offset_m": forward_offset_m,
            "yaw_from_ball": yaw_from_ball,
            "horizontal_offset_deg": horizontal_offset_deg,
            "ball_locator": ball_locator,
            "alignment_x_pct": 50.0,
            "alignment_y_pct": 50.0,
        }
        logger.info("[SERVER] Camera capture initialized: %s", camera_capture_config)
        _prestart_camera_cv_worker_for_lowlight_shadow(camera_cv_worker)
        return True
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera capture initialization failed: %s", error, exc_info=True)
        log_session_error(
            "Camera capture initialization failed",
            component="camera_capture",
            context={"output_dir": str(output_dir)},
            exc=error,
        )
        camera_capture_runtime = None
        camera_replay_manager = None
        camera_reference_ball_tracker = None
        camera_ball_flight_reference_tracker = None
        camera_ball_flight_ground_anchor = None
        camera_lowlight_reference_tracker = None
        camera_lowlight_ground_anchor = None
        camera_cv_worker = None
        camera_capture_config = {"enabled": False, "error": str(error)}
        return False


def configure_camera_tee_zone():
    """Give the camera trigger check the hitting zone of this mount (``camera.tee_zone``).

    Needs the lens calibration (``--camera-intrinsics``) and the TI tee
    geometry. The zone is computed in the calibrated mode's frame; the capture
    runtime moves it into every capture mode with a known frame mapping
    (``CameraCaptureRuntime.active_tee_zone``). Without a zone for the current
    mode the check keeps the full-frame search, and the tee anchor (so the
    camera gate) never forms. Returns the zone of the current mode.
    """
    if camera_capture_runtime is None:
        return None
    from .camera.tee_zone import tee_zone  # noqa: PLC0415

    zone, reason = None, None
    calibration = iwr6843_runtime.calibration if iwr6843_runtime is not None else None
    # Orientation and ScalerCrop must match; the capture mode need not.
    mismatch = (
        camera_intrinsics.mode_mismatch(
            {
                **camera_capture_config,
                "width": camera_intrinsics.image_width_px,
                "height": camera_intrinsics.image_height_px,
            }
        )
        if camera_intrinsics is not None
        else None
    )
    if camera_intrinsics is None:
        reason = "no --camera-intrinsics"
    elif mismatch is not None:
        reason = f"intrinsics mode differs ({mismatch})"
    elif calibration is None or calibration.tee_range_m is None:
        reason = "no TI tee geometry"
    else:
        # Base (mat) ball height on purpose: a startup search window whose pitch margin
        # (tee_zone.PITCH_MARGIN_DEG) covers a teed driver's few cm (~3 deg at 1.5 m).
        zone = tee_zone(
            camera_intrinsics,
            camera_height_m=float(camera_capture_config["mount_height_m"]),
            camera_lateral_offset_m=float(camera_capture_config.get("lateral_offset_m", 0.0)),
            camera_forward_offset_m=float(camera_capture_config.get("forward_offset_m", 0.0)),
            tee_range_m=float(calibration.tee_range_m),
            radar_height_m=calibration.radar_height_m,
            ball_height_m=calibration.tee_ball_height_m,
            horizontal_pixel_sign=-1.0 if camera_capture_config.get("mirror_horizontal") else 1.0,
        )
        reason = None if zone is not None else "implausible tee geometry"
    # The zone is in the calibrated frame, at sensor window 0.
    camera_capture_runtime.set_tee_zone(zone, vertical_offset_px=0)
    active = _refresh_camera_tee_zone_config()
    if zone is not None and active is None:
        reason = (
            f"intrinsics mode differs (no frame mapping from "
            f"{zone.image_width_px}x{zone.image_height_px} to "
            f"{camera_capture_config.get('width')}x{camera_capture_config.get('height')})"
        )
    if active is None:
        logger.warning(
            "[SERVER] Camera tee zone unavailable (%s): the trigger check searches the full "
            "frame and the camera gate never blocks",
            reason,
        )
    return active


def _refresh_camera_tee_zone_config():
    """Show the capture runtime's hitting zone of the current mode in the camera config."""
    zone = camera_capture_runtime.active_tee_zone() if camera_capture_runtime is not None else None
    camera_capture_config["tee_zone"] = zone.to_dict() if zone is not None else None
    return zone


def _notify_ops_trigger_edge(_edge_timestamp: float) -> None:
    """Let the OPS sound trigger re-arm a radar that missed a TI-seen edge."""
    trigger = getattr(monitor, "trigger", None)
    if hasattr(trigger, "notify_trigger_edge"):
        trigger.notify_trigger_edge()


def _reject_ti_for_ops_rejection(first_byte_timestamp: float | None, reason: str) -> None:
    """Release the TI ring frozen by an OPS dump that produced no shot."""
    if iwr6843_runtime is None:
        return
    if iwr6843_runtime.capture_monitor.reject_trigger(first_byte_timestamp):
        logger.info("[SERVER] OPS rejected trigger (%s); TI dump cancelled", reason)


def _notify_ti_ops_dump_started(first_byte_timestamp: float) -> None:
    """Tell the TI monitor that an OPS dump is streaming."""
    if iwr6843_runtime is not None:
        iwr6843_runtime.capture_monitor.notify_ops_dump_started(first_byte_timestamp)


def _notify_ti_ops_dump_finished() -> None:
    """Tell the TI monitor that the OPS dump is over and the re-arm starts."""
    if iwr6843_runtime is not None:
        iwr6843_runtime.capture_monitor.notify_ops_dump_finished()


def _release_ti_edge(edge_timestamp: float) -> bool:
    """Release the TI dump of a sound edge the trigger gate kept from the OPS."""
    if iwr6843_runtime is None:
        return False
    return iwr6843_runtime.capture_monitor.reject_edge(edge_timestamp)


def _log_ops_speed_trigger(entry: dict) -> None:
    """Write one ``ops_speed_trigger`` session-log entry (witness outcome, re-arm, summary)."""
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_ops_speed_trigger"):
        mode = ops_speed_settings.mode if ops_speed_settings is not None else None
        session_log.log_ops_speed_trigger({"mode": mode, **entry})


def _ops_speed_ops_state(edge_timestamp: float | None = None) -> str:
    """The witness's OPS state at a microphone edge: blind (busy) or armed.

    The OPS counts as blind from a trip or pulse before the edge on
    (``TriggerGate.ops_busy_now``), not only once that dump's first byte came.
    """
    from .ops_speed import OPS_STATE_ARMED, OPS_STATE_BUSY

    gate = trigger_gate
    busy = gate is not None and gate.ops_busy_now(edge_timestamp)
    return OPS_STATE_BUSY if busy else OPS_STATE_ARMED


def _on_ops_speed_stall(stall: dict) -> None:
    """``ops-speed``: microphone edges without a speed trip while armed; re-arm the OPS once."""
    trigger = getattr(monitor, "trigger", None)
    requested = hasattr(trigger, "request_rearm")
    if requested:
        trigger.request_rearm()
    _log_ops_speed_trigger({**stall, "kind": "stall_rearm", "rearm_requested": requested})


def _ops_speed_shadow_pin_edge(edge_timestamp: float) -> None:
    """Shadow: a pin-1 edge for the gate, then for the witness unless it was a HOST_INT echo."""
    gate = trigger_gate
    recorded = gate.note_ops_speed_edge(edge_timestamp) if gate is not None else True
    witness = ops_speed_witness
    if recorded and witness is not None:
        witness.note_pin_edge(edge_timestamp)


def _ops_speed_mic_edge(edge_timestamp: float) -> None:
    """A microphone edge for the witness; in the shadow the gate says whether it pulsed.

    The OPS state is taken at the edge, so the shadow's own fallback pulse
    for it (sent just before, by the gate observer) does not make it busy.
    """
    witness = ops_speed_witness
    if witness is None:
        return
    gate = trigger_gate
    fallback = gate.ops_speed_fallback_for(edge_timestamp) if gate is not None else None
    witness.note_mic_edge(
        edge_timestamp,
        ops_state=_ops_speed_ops_state(edge_timestamp),
        fallback_pulse=fallback,
    )


def _log_ops_speed_refusal() -> None:
    """Write the preflight's refusal (if any) into the session that just started."""
    reason = ops_speed_runtime_config.get("refused")
    if reason:
        _log_ops_speed_trigger(
            {
                "kind": "refused",
                "mode": ops_speed_runtime_config.get("mode"),
                "reason": reason,
                "firmware": ops_speed_runtime_config.get("firmware"),
            }
        )


def _arm_ops_speed_inputs() -> None:
    """Accept edges on the speed trigger's second input and start the witness's flushes."""
    if ops_speed_listener is not None:
        ops_speed_listener.arm()
    if ops_speed_witness is not None:
        ops_speed_witness.start()


def _ops_speed_summary_entry() -> dict:
    """The session's ``ops_speed_trigger`` summary: witness counts and delays, gate, re-arms."""
    witness = ops_speed_witness
    witness.flush()
    gate_summary = trigger_gate.summary() if trigger_gate is not None else {}
    trigger = getattr(monitor, "trigger", None)
    return {
        "kind": "summary",
        **witness.summary(),
        "gate": gate_summary.get("ops_speed"),
        "stall_rearms_requested": getattr(trigger, "stall_rearms_requested", None),
    }


def init_ops_speed_witness(config):
    """Create the pin-1 / microphone witness for ``config`` (an ``OpsSpeedConfig``).

    Its outcomes become ``ops_speed_trigger`` entries. In ``ops-speed`` its
    stall rule re-arms an OPS stuck idle (``--ops-speed-stall-mic-edges``); the
    shadow keeps HOST_INT fallback pulses, which feed the OPS stall watch anyway.
    """
    global ops_speed_settings, ops_speed_witness  # pylint: disable=global-statement
    from .ops_speed import OPS_SPEED, OpsSpeedWitness

    stall = config.mode == OPS_SPEED
    ops_speed_settings = config
    ops_speed_witness = OpsSpeedWitness(
        _log_ops_speed_trigger,
        window_ms=config.witness_window_ms,
        ops_state=_ops_speed_ops_state,
        stall_mic_edges=config.stall_mic_edges if stall else 0,
        on_stall=_on_ops_speed_stall if stall else None,
    )
    return ops_speed_witness


def _ops_speed_pin_factory(config):
    """``button_factory`` for J3 pin 1 that reports its releases and glitches to the witness."""
    from .ops_speed import pin_button_factory

    witness = ops_speed_witness
    return pin_button_factory(
        config.pin,
        active=config.pin_active,
        pull=config.pin_pull,
        debounce_ms=config.debounce_ms,
        holdoff_ms=config.holdoff_ms,
        on_release=witness.note_pin_release if witness is not None else None,
        on_glitch=witness.note_pin_glitch if witness is not None else None,
    )


def _ops_speed_edge_input(config, sound_pin: int):
    """The speed trigger's primary input: ``(pin, button_factory, witness observer)``.

    ``ops-speed``: pin 1 drives TI, camera and gate. Shadow: the microphone
    does, as in sound mode. The primary input's owner (TI monitor or gate
    listener) appends the witness observer after the gate and the camera.
    """
    from .ops_speed import OPS_SPEED

    if config.mode == OPS_SPEED:
        witness = ops_speed_witness
        observer = witness.note_pin_edge if witness is not None else None
        return config.pin, _ops_speed_pin_factory(config), observer
    return sound_pin, None, _ops_speed_mic_edge


def init_ops_speed_listener(config, *, sound_pin: int) -> bool:
    """Own the speed trigger's second input: the microphone witness, or pin 1 in the shadow.

    ``ops-speed``: the microphone only feeds the witness (never the OPS stall
    watch, or neighbour noise would force re-arms). Shadow: pin 1 records the
    speed trips in the gate and feeds the witness (not a pin edge right after a
    HOST_INT fallback pulse: that is the pulse's own dump), and freezes the
    camera ring when it is free. Call after the gate and the TI monitor;
    ``start_monitor`` arms it.
    """
    global ops_speed_listener  # pylint: disable=global-statement
    from .ops_speed import OPS_SPEED
    from .trigger_gate import EdgeListener

    if config.mode == OPS_SPEED:
        listener = EdgeListener(pin=sound_pin, observers=[_ops_speed_mic_edge])
        role = "microphone witness"
    else:
        observers = [_ops_speed_shadow_pin_edge]
        if camera_capture_runtime is not None:
            observers.append(camera_capture_runtime.notify_trigger)
        listener = EdgeListener(
            pin=config.pin, observers=observers, button_factory=_ops_speed_pin_factory(config)
        )
        role = "J3 pin 1"
    try:
        listener.start()
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("[SERVER] OPS speed-trigger input (%s) failed: %s", role, error, exc_info=True)
        log_session_error(
            "OPS speed-trigger input failed",
            component="ops_speed",
            context={"pin": listener.pin, "role": role},
            exc=error,
        )
        return False
    ops_speed_listener = listener
    logger.info("[SERVER] OPS speed trigger: BCM%d is the %s", listener.pin, role)
    return True


def init_trigger_gate(  # pylint: disable=too-many-arguments
    *,
    pin: int,
    mode: str,
    pulse_ms: float,
    deadline_ms: float,
    edge_pin: int,
    own_edge_pin: bool,
    split_warning: str | None = None,
    edge_source: str = "sound",
    ti_anchor=None,
    ops_speed=None,
    sample_rate_ksps: float = 30.0,
) -> bool:
    """Drive OPS HOST_INT from ``pin``; without a TI monitor also own the sound-gate input.

    Call after camera capture and before the TI monitor, which fans its edges
    out to the gate first. ``edge_source="ti"`` (``--shot-trigger ti``): the
    edges are the TI's self-trigger notices, anchored by ``ti_anchor``.
    ``ops_speed`` (an ``OpsSpeedConfig``, ``--shot-trigger ops-speed*``): the
    gate records the OPS speed trips; in ``ops-speed`` the listener owns pin 1
    instead of the microphone (``init_ops_speed_listener`` owns the other input).
    ``sample_rate_ksps`` sets the speed-trigger dump's post-trigger span, from
    the trip on which the gate counts the OPS blind for the witness.
    """
    global trigger_gate, trigger_gate_listener, trigger_gate_config  # pylint: disable=global-statement
    from .ops_speed import OPS_SPEED, OPS_SPEED_SHADOW
    from .trigger_gate import EdgeListener, TriggerGate, post_trigger_span_ms

    ops_speed_kwargs = {}
    if ops_speed is not None:
        edge_source = "ops-speed" if ops_speed.mode == OPS_SPEED else "sound"
        ops_speed_kwargs = {
            "ops_speed_shadow": ops_speed.mode == OPS_SPEED_SHADOW,
            "ops_speed_anchor_ms": ops_speed.anchor_ms,
            "ops_speed_window_ms": ops_speed.witness_window_ms,
            "ops_speed_post_trigger_ms": post_trigger_span_ms(
                ops_speed.pre_trigger_segments, sample_rate_ksps
            ),
        }
    gate = TriggerGate(
        pin=pin,
        mode=mode,
        pulse_ms=pulse_ms,
        deadline_ms=deadline_ms,
        camera=camera_capture_runtime,
        on_pulse=lambda: _notify_ops_trigger_edge(time.time()),
        release_edge=_release_ti_edge,
        edge_timing=sound_edge_timing,
        edge_source=edge_source,
        ti_anchor=ti_anchor,
        **ops_speed_kwargs,
    )
    try:
        gate.start()
        trigger_gate = gate
        if own_edge_pin:
            observers = [gate.on_edge]
            if camera_capture_runtime is not None:
                observers.append(camera_capture_runtime.notify_trigger)
                camera_capture_config["trigger_source"] = "trigger_gate_fanout"
            listener_kwargs = {"pin": edge_pin}
            if ops_speed is not None:
                listener_pin, factory, observer = _ops_speed_edge_input(ops_speed, edge_pin)
                if observer is not None:
                    observers.append(observer)
                listener_kwargs = {"pin": listener_pin, "button_factory": factory}
            listener = EdgeListener(
                observers=observers, edge_timing=sound_edge_timing, **listener_kwargs
            )
            listener.start()
            trigger_gate_listener = listener
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("[SERVER] Trigger gate failed to start: %s", error, exc_info=True)
        log_session_error(
            "Trigger gate initialization failed",
            component="trigger_gate",
            context={"pin": pin, "mode": mode},
            exc=error,
        )
        trigger_gate_config = {"enabled": False, "error": str(error)}
        return False
    if split_warning:
        logger.warning("[GATE] %s", split_warning)
    if mode == "camera" and camera_capture_runtime is None:
        logger.warning("[GATE] Camera capture is unavailable; camera mode pulses every edge")
    trigger_gate_config = {
        "enabled": True,
        "host_int_pin_bcm": pin,
        "edge_pin_bcm": edge_pin if edge_source != "ops-speed" else ops_speed.pin,
        "edge_source": (
            "iwr6843_self_trigger"
            if edge_source == "ti"
            else ("trigger_gate" if own_edge_pin else "iwr6843_fanout")
        ),
        "mode": mode,
        "pulse_ms": pulse_ms,
        "deadline_ms": (
            deadline_ms if mode == "camera" or camera_capture_runtime is not None else None
        ),
        "camera_shadow": mode == "passthrough" and camera_capture_runtime is not None,
        "split_warning": split_warning,
        "ti_anchor": gate.ti_anchor.as_dict() if gate.ti_anchor is not None else None,
    }
    if ops_speed is not None:
        trigger_gate_config["ops_speed"] = {
            "mode": gate.ops_speed_mode,
            "anchor_ms": gate.ops_speed_anchor_ms,
            "window_ms": list(gate.ops_speed_window_ms),
            "post_trigger_ms": gate.ops_speed_post_trigger_ms,
        }
    return True


def _log_ti_self_trigger(fields: dict) -> None:
    """Write one ``ti_self_trigger`` session-log entry (fire, or a shadow pairing)."""
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_ti_self_trigger"):
        session_log.log_ti_self_trigger(fields)


def _on_ti_fire(fields: dict) -> None:
    """``--shot-trigger ti``: anchor the gate record by the rule that fired, then log it."""
    entry = dict(fields)
    if trigger_gate is not None:
        trigger_gate.note_ti_cause(fields["edge_timestamp"], fields.get("cause"))
        if trigger_gate.ti_anchor is not None:
            entry["anchor_lead_ms"] = trigger_gate.ti_anchor.lead_ms(fields.get("cause"))
    _log_ti_self_trigger(entry)


def _iwr6843_self_trigger_config(
    *,
    config_path: str,
    tee_range_m: float,
    offset_m: float | None,
    snr: float | None,
    explicit_bin: int | None,
):
    """The board's ``triggerCfg``: tee + 30 mm replay convention (see self_trigger)."""
    from .iwr6843.self_trigger import (
        DEFAULT_SELF_TRIGGER_OFFSET_M,
        DEFAULT_SELF_TRIGGER_SNR,
        SelfTriggerConfig,
        check_first_window_bin,
        self_trigger_bin,
    )

    if explicit_bin is not None:
        tee_bin = check_first_window_bin(
            explicit_bin, config_path, f"--iwr6843-self-trigger-bin {explicit_bin}"
        )
    else:
        tee_bin = self_trigger_bin(
            tee_range_m,
            config_path,
            offset_m=DEFAULT_SELF_TRIGGER_OFFSET_M if offset_m is None else offset_m,
        )
    return SelfTriggerConfig(tee_bin=tee_bin, snr=DEFAULT_SELF_TRIGGER_SNR if snr is None else snr)


def _ops_speed_ti_input(ops_speed, trigger_pin: int) -> tuple:
    """The TI monitor's input: ``(gpio_pin, button_factory, extra observers, l3dump delay s)``.

    Without the speed trigger: the microphone pin, as always. ``ops-speed``:
    pin 1, the witness as last observer and ``l3dump`` the TI delay after the
    pin edge. Shadow: the microphone, with the witness's microphone observer.
    """
    from .ops_speed import OPS_SPEED

    if ops_speed is None:
        return trigger_pin, None, [], 0.0
    gpio_pin, button_factory, observer = _ops_speed_edge_input(ops_speed, trigger_pin)
    observers = [observer] if observer is not None else []
    if ops_speed.mode != OPS_SPEED:
        return gpio_pin, button_factory, observers, 0.0
    if camera_capture_runtime is not None:
        camera_capture_config["trigger_source"] = "ops_speed_pin"
    return gpio_pin, button_factory, observers, ops_speed.ti_delay_ms / 1000.0


def _ti_edge_pin(args, ops_speed) -> int:
    """The BCM pin the TI monitor listens on: pin 1 in ``ops-speed``, else the microphone."""
    from .ops_speed import OPS_SPEED

    if ops_speed is not None and ops_speed.mode == OPS_SPEED:
        return ops_speed.pin
    return args.iwr6843_trigger_pin


def _freeze_delay_text(l3dump_delay_s: float) -> str:
    """Log suffix for a delayed TI ring freeze (``ops-speed``); empty without one."""
    if l3dump_delay_s <= 0.0:
        return ""
    return f" {l3dump_delay_s * 1000.0:g} ms after the pin-1 edge"


def init_iwr6843(
    *,
    port: str | None,
    config_path: str,
    calibration_path: str,
    output_dir: str | Path,
    trigger_pin: int,
    tee_range_m: float,
    net_range_m: float | None,
    tx_order: str,
    capture_timeout_s: float,
    tilt_deg: float | None = None,
    radar_height_m: float | None = None,
    ball_height_m: float = 0.04,
    azimuth_offset_deg: float = 0.0,
    horizontal_phase_reference_rad: float | None = None,
    save_dumps: bool = False,
    horizontal_trajectory_fit: bool = False,
    tee_lateral_m: float | None = None,
    usb_power_cycler=None,
    recovery_guard=None,
    shot_trigger: str = "sound",
    self_trigger_offset_m: float | None = None,
    self_trigger_snr: float | None = None,
    self_trigger_bin: int | None = None,
    shadow_holdoff_ms: float = 0.0,
    ops_speed=None,
) -> bool:
    """Initialize GPIO- or self-triggered TI capture and the frozen LCMF-v1 estimator.

    A ``usb_power_cycler`` enables mid-session recovery of a wedged TI; the
    ``recovery_guard`` can veto each recovery (see the monitor).
    ``shot_trigger`` ``ti``/``ti-shadow`` arms the board's self-trigger
    (:mod:`openflight.iwr6843.self_trigger`). The OPS speed-trigger modes run
    the TI as in ``sound`` (no self-trigger); with ``ops_speed`` in
    ``ops-speed`` it listens on pin 1 and sends ``l3dump`` the TI delay after
    the pin edge (``--ops-speed-ti-delay-ms``).
    """
    global iwr6843_runtime, iwr6843_runtime_config  # pylint: disable=global-statement
    try:
        from .iwr6843 import Calibration
        from .iwr6843.lcmf import DEFAULT_TEE_LATERAL_M
        from .iwr6843.monitor import IWR6843CaptureMonitor, tx_order_from_config
        from .iwr6843.runtime import IWR6843Runtime
        from .iwr6843.self_trigger import TiShadowMatcher, trigger_ball_bin
        from .ops_speed import OPS_SPEED_SHOT_TRIGGERS

        configured_order = tx_order_from_config(config_path)
        resolved_order = configured_order if tx_order == "auto" else tx_order
        if resolved_order != configured_order:
            raise ValueError(
                f"--iwr6843-tx-order {resolved_order} conflicts with "
                f"{Path(config_path).name} ({configured_order})"
            )

        calibration = Calibration.load(calibration_path)
        calibration.tee_range_m = tee_range_m
        calibration.tee_ball_height_m = ball_height_m
        # Re-read on every init, so the USB power-cycle retry keeps the phone value.
        tilt = _resolve_iwr_mount_tilt(math.degrees(calibration.tilt_rad), cli_tilt_deg=tilt_deg)
        calibration.tilt_rad = math.radians(tilt["tilt_deg"])
        if radar_height_m is not None:
            calibration.meta["radar_height_m"] = radar_height_m

        # The speed-trigger modes keep the TI in sound mode: no TI self-trigger config.
        ti_shot_trigger = "sound" if shot_trigger in OPS_SPEED_SHOT_TRIGGERS else shot_trigger
        self_trigger = None
        if ti_shot_trigger != "sound":
            self_trigger = _iwr6843_self_trigger_config(
                config_path=config_path,
                tee_range_m=tee_range_m,
                offset_m=self_trigger_offset_m,
                snr=self_trigger_snr,
                explicit_bin=self_trigger_bin,
            )
        shadow_matcher = (
            TiShadowMatcher(lambda fields: _log_ti_self_trigger({"mode": "ti-shadow", **fields}))
            if shot_trigger == "ti-shadow"
            else None
        )
        trigger_observers = []
        if trigger_gate is not None:
            trigger_observers.append(trigger_gate.on_edge)
        if camera_capture_runtime is not None:
            trigger_observers.append(camera_capture_runtime.notify_trigger)
            if shot_trigger == "ti":
                camera_capture_config["trigger_source"] = "iwr6843_self_trigger"
        if trigger_gate is None:
            trigger_observers.append(_notify_ops_trigger_edge)
        gpio_pin, button_factory, ops_speed_observers, l3dump_delay_s = _ops_speed_ti_input(
            ops_speed, trigger_pin
        )
        trigger_observers.extend(ops_speed_observers)
        capture_monitor = IWR6843CaptureMonitor(
            config_path=config_path,
            output_dir=output_dir,
            port=port,
            gpio_pin=gpio_pin,
            button_factory=button_factory,
            save_dumps=save_dumps,
            cancel_false_triggers=True,
            trigger_observers=trigger_observers,
            usb_power_cycler=usb_power_cycler,
            recovery_guard=recovery_guard,
            edge_timing=sound_edge_timing,
            shot_trigger=ti_shot_trigger,
            self_trigger=self_trigger,
            shadow_matcher=shadow_matcher,
            shadow_holdoff_s=shadow_holdoff_ms / 1000.0,
            fire_observers=[_on_ti_fire] if shot_trigger == "ti" else None,
            l3dump_delay_s=l3dump_delay_s,
        )
        # OPS initialization can pulse the shared sound gate. Configure TI now,
        # but do not accept edges until the OPS trigger path is fully running.
        capture_monitor.start(armed=False)
        if camera_capture_runtime is not None:
            camera_capture_runtime.set_write_deferral(lambda: capture_monitor.dump_in_progress)
        iwr6843_runtime = IWR6843Runtime(
            capture_monitor=capture_monitor,
            calibration=calibration,
            net_range_m=net_range_m,
            tx_order=resolved_order,
            capture_timeout_s=capture_timeout_s,
            azimuth_offset_deg=azimuth_offset_deg,
            horizontal_phase_reference_rad=horizontal_phase_reference_rad,
            horizontal_trajectory_fit=horizontal_trajectory_fit,
            tee_lateral_m=(DEFAULT_TEE_LATERAL_M if tee_lateral_m is None else tee_lateral_m),
            # The supported normal-TX profiles have a measured positive TDM
            # registration. Auto sign selection can choose the mirror solution
            # in multipath and collapse the eight-element vertical channel.
            tdm_sign_policy="positive",
        )
        iwr6843_runtime_config = {
            "enabled": True,
            "estimator": "lcmf_v1",
            "port": capture_monitor.port,
            "config": str(config_path),
            "calibration": str(calibration_path),
            "trigger_pin_bcm": gpio_pin if shot_trigger != "ti" else None,
            "shot_trigger": shot_trigger,
            "self_trigger": (
                {
                    "command": self_trigger.command,
                    "tee_bin": self_trigger.tee_bin,
                    "snr": self_trigger.snr,
                    "offset_m": self_trigger_offset_m,
                    "explicit_bin": self_trigger_bin,
                    # The ball bin the trigger bin is counted from: --iwr6843-tee-m
                    # + 30 mm, the replay/upstream tape convention (not the LCMF slant).
                    "ball_bin": trigger_ball_bin(tee_range_m),
                    "shadow_holdoff_ms": shadow_holdoff_ms if shot_trigger == "ti-shadow" else None,
                    "shadow_window_ms": (
                        list(shadow_matcher.window_ms) if shadow_matcher is not None else None
                    ),
                }
                if self_trigger is not None
                else None
            ),
            "tee_slant_range_m": tee_range_m,
            "net_range_m": net_range_m,
            "tx_order": resolved_order,
            "tdm_sign_policy": iwr6843_runtime.tdm_sign_policy,
            "tilt_deg": math.degrees(calibration.tilt_rad),
            "tilt_source": tilt["tilt_source"],
            "tilt_fallback_deg": tilt["fallback_deg"],
            "tilt_fallback_source": tilt["fallback_source"],
            "phone_tilt_calibration": tilt["record"],
            "radar_height_m": calibration.radar_height_m,
            "ball_height_m": calibration.tee_ball_height_m,
            "azimuth_offset_deg": azimuth_offset_deg,
            "horizontal_phase_reference_rad": horizontal_phase_reference_rad,
            "capture_timeout_s": capture_timeout_s,
            # ops-speed: l3dump (the ring freeze) this long after the pin-1 edge.
            "freeze_delay_ms": l3dump_delay_s * 1000.0,
            "raw_dump_saved": save_dumps,
            "output_dir": str(Path(output_dir).expanduser()),
        }
        if horizontal_trajectory_fit:
            iwr6843_runtime_config["horizontal_trajectory_fit"] = True
            iwr6843_runtime_config["tee_lateral_m"] = iwr6843_runtime.tee_lateral_m
            if horizontal_phase_reference_rad is None:
                logger.warning(
                    "[SERVER] IWR6843 horizontal trajectory fit is on without "
                    "--iwr6843-horizontal-phase-reference-rad; launch direction "
                    "is biased until the TX2 phase zero is calibrated"
                )
        logger.info(
            "[SERVER] IWR6843 initialized (port=%s, %s, estimator=LCMF-v1, %s)",
            capture_monitor.port,
            f"BCM{gpio_pin}" if shot_trigger != "ti" else "no microphone pin",
            (
                f"firmware boundary freeze{_freeze_delay_text(l3dump_delay_s)}"
                if self_trigger is None
                else f"{shot_trigger} self-trigger {self_trigger.command!r}"
            ),
        )
        return True
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] IWR6843 initialization failed: %s", error, exc_info=True)
        log_session_error(
            "IWR6843 initialization failed",
            component="iwr6843",
            context={"config": config_path, "port": port or "auto"},
            exc=error,
        )
        iwr6843_runtime = None
        iwr6843_runtime_config = {"enabled": False, "error": str(error)}
        return False


def _phone_tilt_path() -> Path:
    """Where the phone tilt calibration is saved."""
    return PHONE_TILT_CALIBRATION_PATH or default_phone_tilt_path()


def _resolve_iwr_mount_tilt(json_tilt_deg: float, *, cli_tilt_deg: float | None) -> dict:
    """Pick the configured TI tilt: saved phone calibration > CLI > calibration JSON.

    The launcher always passes ``--iwr6843-tilt-deg``, so the phone value has to
    outrank it to have any effect. An unreadable or inconsistent phone file is
    ignored with a warning. Returns ``tilt_deg``, ``tilt_source``, the fallback
    used when the phone calibration is removed, and the phone ``record``.
    """
    if cli_tilt_deg is not None:
        fallback_deg, fallback_source = float(cli_tilt_deg), "cli"
    else:
        fallback_deg, fallback_source = float(json_tilt_deg), "calibration_json"
    try:
        record = load_phone_tilt_calibration(_phone_tilt_path())
    except (OSError, ValueError) as error:
        logger.warning(
            "[SERVER] Ignoring invalid phone tilt calibration %s: %s",
            _phone_tilt_path(),
            error,
        )
        record = None
    if record is not None:
        tilt_deg, tilt_source = float(record["configured_iwr_tilt_deg"]), "phone"
    else:
        tilt_deg, tilt_source = fallback_deg, fallback_source
    if tilt_source == "phone":
        message = (
            f"IWR6843 tilt: {tilt_deg:.3f}deg from the phone calibration of "
            f"{record.get('applied_at')} (radar tilt {record.get('radar_tilt_deg')}deg); "
            f"ignoring {fallback_source} {fallback_deg:.3f}deg"
        )
    else:
        message = f"IWR6843 tilt: {tilt_deg:.3f}deg from {tilt_source}"
    logger.info("[SERVER] %s", message)
    print(message)
    return {
        "tilt_deg": tilt_deg,
        "tilt_source": tilt_source,
        "fallback_deg": fallback_deg,
        "fallback_source": fallback_source,
        "record": record,
    }


def _iwr6843_startup_recovery(error: object) -> str:
    """Translate a known TI initialization failure into operator guidance.

    A wedged TI firmware survives a Pi restart because USB stays powered;
    only a shutdown or unplugging the TI cable power-cycles the radar.
    """
    from .iwr6843.health import wedge_label  # pylint: disable=import-outside-toplevel

    cause = wedge_label(error)
    if cause is not None:
        return (
            f"TI radar is not responding ({cause}). Shut the Pi down completely "
            "(not restart) and power it on again, or unplug the TI USB cable for 5 s. "
            "Then start OpenFlight."
        )
    return "Check the TI radar USB and power connections, then relaunch OpenFlight."


def _iwr6843_error_warrants_power_cycle(error: object) -> bool:
    """Whether a TI init failure looks like a wedge a USB power cycle can clear."""
    from .iwr6843.health import wedge_label  # pylint: disable=import-outside-toplevel

    return wedge_label(error) is not None or "no IWR6843 CLI found" in str(error or "")


def _make_iwr6843_recovery_guard(*, kld7: bool):
    """Veto a mid-session USB power cycle that would take other radars down.

    The Pi 5 ports are ganged, so a power cycle drops every USB device. The
    guard runs right before each recovery and reads the OPS port that is
    actually connected (it is auto-detected when ``--port`` is absent).
    """

    def guard() -> str | None:
        if kld7:
            return "K-LD7 radars are on USB"
        current = monitor
        if current is None:
            return "OPS radar not connected yet"
        if mock_mode:
            return None
        port = getattr(getattr(current, "radar", None), "port", None)
        if port is None:
            return "OPS radar not connected yet"
        if not is_uart_port(port):
            return f"OPS radar on {port} is not a GPIO UART"
        return None

    return guard


def _iwr6843_usb_power_cycler():
    """The cycler for ``--iwr6843-usb-power-cycle``, or None when unsafe or not allowed."""
    from .iwr6843.usb_power import (  # pylint: disable=import-outside-toplevel
        UsbPowerCycler,
        usb_mounted_block_devices,
    )

    cycler = UsbPowerCycler()
    if not cycler.permitted():
        logger.warning(
            "[SERVER] --iwr6843-usb-power-cycle is set but sudo does not allow uhubctl "
            "without a password; see docs/iwr6843/troubleshooting.md"
        )
        return None
    usb_storage = usb_mounted_block_devices()
    if usb_storage:
        logger.warning(
            "[SERVER] --iwr6843-usb-power-cycle disabled: USB storage is mounted (%s)",
            ", ".join(usb_storage),
        )
        return None
    return cycler


def _recover_iwr6843_at_startup(cycler, port: str | None, init_kwargs: dict) -> bool:
    """Power-cycle USB once and retry TI initialization; never raises."""
    from .iwr6843.usb_power import (  # pylint: disable=import-outside-toplevel
        DEFAULT_REENUMERATE_TIMEOUT_S,
        wait_for_tty,
    )

    try:
        cycler.cycle()
        if not wait_for_tty(port):
            raise RuntimeError(
                f"TI port {port or '/dev/ttyUSB*'} did not return within "
                f"{DEFAULT_REENUMERATE_TIMEOUT_S:.0f}s after the USB power cycle"
            )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.error("[SERVER] TI USB power cycle at startup failed", exc_info=True)
        return False
    if not init_iwr6843(**init_kwargs):
        logger.error(
            "[SERVER] TI still failed after a USB power cycle: %s",
            iwr6843_runtime_config.get("error"),
        )
        return False
    logger.info("[SERVER] TI recovered by USB power cycle at startup")
    return True


def init_inclinometer(*, zero_offset_deg: float, bus_number: int = 1, address: int = 0x18) -> bool:
    """Start the optional LIS3DH service without risking radar availability."""
    global inclinometer_service  # pylint: disable=global-statement
    global inclinometer_runtime_config  # pylint: disable=global-statement

    service = None
    try:
        from .inclinometer import LIS3DH, InclinometerService

        service = InclinometerService(
            LIS3DH(bus_number=bus_number, address=address),
            zero_offset_deg=zero_offset_deg,
        )
        service.start()
        startup = service.wait_for_stable(timeout_s=2.0)
        inclinometer_service = service
        inclinometer_runtime_config = {
            "enabled": True,
            "sensor": "lis3dh",
            "i2c_bus": bus_number,
            "i2c_address": f"0x{address:02x}",
            "sample_hz": service.sample_hz,
            "zero_offset_deg": zero_offset_deg,
            "startup": startup.to_dict(),
        }
        if startup.snapshot is None:
            logger.warning(
                "[SERVER] LIS3DH initialized but has no stable startup reading (%s)",
                startup.status,
            )
            print(f"Inclinometer enabled, waiting for a stable reading ({startup.status})")
            return True

        snapshot = startup.snapshot
        print(
            "Inclinometer enabled "
            f"(raw pitch {snapshot.raw_pitch_deg:+.2f}deg, "
            f"calibrated {snapshot.calibrated_pitch_deg:+.2f}deg)"
        )
        if iwr6843_runtime is not None:
            configured_tilt = math.degrees(iwr6843_runtime.calibration.tilt_rad)
            effective_tilt = configured_tilt + snapshot.calibrated_pitch_deg
            print(
                f"IWR6843 tilt: configured {configured_tilt:.2f}deg, "
                f"effective {effective_tilt:.2f}deg"
            )
        return True
    except Exception as error:  # pylint: disable=broad-exception-caught
        if service is not None:
            try:
                service.stop()
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("Failed to close LIS3DH after initialization error", exc_info=True)
        logger.warning("[SERVER] Inclinometer initialization failed: %s", error, exc_info=True)
        log_session_error(
            "Inclinometer initialization failed",
            component="inclinometer",
            context={"i2c_bus": bus_number, "i2c_address": f"0x{address:02x}"},
            exc=error,
        )
        inclinometer_service = None
        inclinometer_runtime_config = {
            "enabled": False,
            "requested": True,
            "sensor": "lis3dh",
            "i2c_bus": bus_number,
            "i2c_address": f"0x{address:02x}",
            "zero_offset_deg": zero_offset_deg,
            "error": str(error),
        }
        return False


def init_kld7(
    port=None,
    orientation="vertical",
    angle_offset_deg=0.0,
    base_freq=0,
    mount_tilt_deg=18.0,
    ball_distance_ft=5.5,
    vertical_flight_window_net_distance_ft=10.0,
) -> bool:
    """Initialize a single K-LD7 angle radar tracker.

    Returns True if the tracker connected and started successfully.
    Sets the appropriate global (kld7_vertical or kld7_horizontal).
    """
    global kld7_vertical, kld7_horizontal  # pylint: disable=global-statement
    try:
        from openflight.kld7 import KLD7Tracker

        tracker = KLD7Tracker(
            port=port,
            orientation=orientation,
            angle_offset_deg=angle_offset_deg,
            base_freq=base_freq,
            buffer_seconds=6.0,
            vertical_estimator="two_ray" if orientation == "vertical" else "naive",
            mount_tilt_deg=mount_tilt_deg,
            ball_distance_ft=ball_distance_ft,
            vertical_flight_window_net_distance_ft=vertical_flight_window_net_distance_ft,
        )
        if tracker.connect():
            tracker.start()
            logger.info(
                "[SERVER] K-LD7 %s initialized (port=%s, offset=%.1f°, RBFR=%d)",
                orientation,
                port or "auto",
                angle_offset_deg,
                base_freq,
            )
            session_log = get_session_logger()
            if session_log:
                session_log.log_connection(
                    device="kld7_%s" % orientation,
                    port=tracker.port or "auto",
                    baud=3000000,
                    radc_available=True,
                    base_freq=base_freq,
                )
            if orientation == "vertical":
                kld7_vertical = tracker
            else:
                kld7_horizontal = tracker
            return True
        else:
            return False
    except Exception as e:
        logger.warning("[SERVER] K-LD7 %s initialization failed: %s", orientation, e, exc_info=True)
        log_session_error(
            "K-LD7 initialization failed",
            component="kld7",
            context={"orientation": orientation},
            exc=e,
        )
        return False


@app.route("/api/camera/preview.jpg")
def camera_capture_preview():
    """Single still from the capture runtime's concurrent main stream.

    Served from the processed YUV stream while the raw rolling buffer keeps
    running, so shots are never missed while the camera tab polls this.
    """
    if camera_capture_runtime is None:
        return "Camera capture not enabled", 404
    jpeg = camera_capture_runtime.capture_preview_jpeg()
    if jpeg is None:
        return "Camera not running", 503
    return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/api/camera/exposure-quality")
def camera_capture_exposure_quality():
    """Automatic exposure state derived from the latest impact-zone pixels."""
    if camera_capture_runtime is None:
        return jsonify({"sample_available": False, "status": "unavailable"}), 404
    quality = camera_capture_runtime.exposure_quality()
    quality["auto_exposure"] = camera_capture_runtime.auto_exposure_status()
    # The live frame or the exposure lock shows no picture (one grey level).
    quality["frame_flat"] = quality.get("status") == "flat" or bool(
        quality["auto_exposure"].get("frame_flat")
    )
    return jsonify(quality)


@app.route("/api/camera/replays/<replay_id>/prepare", methods=["GET", "POST"])
def prepare_camera_replay(replay_id: str):  # pylint: disable=too-many-return-statements
    """Create or reuse the cached MP4 on the low-priority replay worker."""
    from .camera.replay import ReplayNotFoundError, ReplayPreparationError

    if request.method != "POST":
        return jsonify({"error": "Use POST to prepare a camera replay"}), 405
    if camera_replay_manager is None:
        return jsonify({"error": "Camera replay was not found"}), 404
    result: Future = Future()
    deadline = time.monotonic() + _CAMERA_REPLAY_REQUEST_TIMEOUT_S
    try:
        _submit_camera_replay_prepare((None, replay_id, result), _CAMERA_REPLAY_REQUEST_TIMEOUT_S)
        prepared = result.result(timeout=max(0.0, deadline - time.monotonic()))
    except (queue.Full, FutureTimeoutError):
        logger.warning("[CAMERA] Replay %s was not prepared within the request timeout", replay_id)
        return jsonify({"error": "Camera replay is still being prepared"}), 503
    except ReplayNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except ReplayPreparationError as error:
        logger.warning("[CAMERA] Could not prepare replay %s: %s", replay_id, error)
        log_session_error(
            "Camera replay preparation failed",
            component="camera_capture",
            context={"replay_id": replay_id},
            exc=error,
        )
        return jsonify({"error": str(error)}), 503
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.exception("[CAMERA] Unexpected replay preparation failure for %s", replay_id)
        log_session_error(
            "Camera replay preparation failed",
            component="camera_capture",
            context={"replay_id": replay_id},
            exc=error,
        )
        return jsonify({"error": "Camera replay could not be prepared"}), 500

    payload = dict(prepared.payload)
    payload["video_url"] = _camera_replay_video_url(replay_id)
    return jsonify(payload)


def _camera_replay_video_url(replay_id: str) -> str:
    return f"/api/camera/replays/{replay_id}/video"


@app.route("/api/camera/replays/<replay_id>/video")
def camera_replay_video(replay_id: str):  # pylint: disable=too-many-return-statements
    """Stream a previously prepared replay with HTTP range support."""
    from .camera.replay import (
        ReplayNotFoundError,
        ReplayNotReadyError,
        ReplayPreparationError,
    )

    if camera_replay_manager is None:
        return jsonify({"error": "Camera replay was not found"}), 404
    try:
        video_path = camera_replay_manager.video_path(replay_id)
    except ReplayNotFoundError as error:
        return jsonify({"error": str(error)}), 404
    except ReplayNotReadyError as error:
        return jsonify({"error": str(error)}), 409
    except ReplayPreparationError as error:
        logger.warning("[CAMERA] Could not read replay %s: %s", replay_id, error)
        return jsonify({"error": str(error)}), 503
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.exception("[CAMERA] Unexpected replay lookup failure for %s", replay_id)
        log_session_error(
            "Camera replay lookup failed",
            component="camera_capture",
            context={"replay_id": replay_id},
            exc=error,
        )
        return jsonify({"error": "Camera replay video is unavailable"}), 500
    try:
        return send_file(video_path, mimetype="video/mp4", conditional=True, etag=True)
    except OSError as error:
        logger.warning("[CAMERA] Could not stream replay %s: %s", replay_id, error)
        return jsonify({"error": "Camera replay video is unavailable"}), 503
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.exception("[CAMERA] Unexpected replay streaming failure for %s", replay_id)
        log_session_error(
            "Camera replay streaming failed",
            component="camera_capture",
            context={"replay_id": replay_id},
            exc=error,
        )
        return jsonify({"error": "Camera replay video is unavailable"}), 500


def _log_angle_reference(event: str, reason: str | None = None) -> None:
    """Write the camera angle reference's state to the session log (``angle_reference``)."""
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_angle_reference"):
        session_log.log_angle_reference(event, angle_reference.state(), reason=reason)


def _apply_angle_reference(shot: Shot) -> None:
    """Select the shot's launch angles against the camera; never risks the shot."""
    try:
        # A corrected TI vertical must pass the guard the raw TI passed.
        changed = angle_reference.apply(shot, vertical_guard=_ti_vertical_launch_is_plausible)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera angle reference failed: %s", error, exc_info=True)
        log_session_error(
            "Camera angle reference failed",
            component="server",
            context={"stage": "angle_reference", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )
        return
    reference = shot.angle_reference or {}
    for axis in ("vertical", "horizontal"):
        details = reference.get(axis) or {}
        offset = details.get("offset") or {}
        logger.info(
            "[SERVER] Angle reference %s: selected=%s radar=%s camera=%s corrected=%s "
            "offset=%s spread=%s pairs=%s valid=%s",
            axis,
            details.get("selected"),
            details.get("radar_deg"),
            details.get("camera_deg"),
            details.get("corrected_deg"),
            offset.get("offset_deg"),
            offset.get("spread_deg"),
            offset.get("pairs"),
            offset.get("valid"),
        )
    if changed:
        _log_angle_reference("offset_validity_changed")


def _reset_angle_reference(reason: str, *, axes: tuple[str, ...] = ("vertical", "horizontal")):
    """Forget the session offsets of ``axes`` and log it when there was anything to forget."""
    if angle_reference.reset(reason, axes=axes):
        logger.info("[SERVER] Angle reference reset (%s): %s", reason, ", ".join(axes))
        _log_angle_reference("offset_reset", reason)


def _camera_target_geometry() -> dict | None:
    """Everything that maps a preview pixel to a direction: the target line's geometry key.

    Its capture-mode part (``angle_reference.MODE_GEOMETRY_KEYS``: width,
    height, sensor window) selects the lens model ``intrinsics.for_mode``
    derives from the calibration in its mount part (``intrinsics``, as
    calibrated), so a mode switch keeps the mount part and both marks carry over.
    """
    if camera_capture_runtime is None:
        return None
    config = camera_capture_config
    intrinsics = camera_intrinsics
    scaler_crop = config.get("scaler_crop")
    return {
        "width": config.get("width"),
        "height": config.get("height"),
        "rotate_180": bool(config.get("rotate_180", False)),
        "mirror_horizontal": bool(config.get("mirror_horizontal", False)),
        "scaler_crop": list(scaler_crop) if scaler_crop is not None else None,
        "vertical_offset_px": camera_capture_runtime.vertical_crop_status().get(
            "vertical_offset_px"
        ),
        "roll_correction_deg": float(config.get("roll_correction_deg", 0.0)),
        "mount_height_m": camera_capture_config.get("mount_height_m"),
        "lateral_offset_m": camera_capture_config.get("lateral_offset_m"),
        "forward_offset_m": camera_capture_config.get("forward_offset_m"),
        "intrinsics": (
            None
            if intrinsics is None
            else {
                "fx": intrinsics.fx,
                "fy": intrinsics.fy,
                "cx": intrinsics.cx,
                "cy": intrinsics.cy,
                "dist_coeffs": list(intrinsics.dist_coeffs),
            }
        ),
    }


def _camera_target_unavailable_reason() -> str | None:
    """Why a target cannot be tapped now, None when it can."""
    if camera_capture_runtime is None:
        return "High-speed camera capture is not running"
    if camera_intrinsics is None:
        return "Tapping a target needs the lens calibration (--camera-intrinsics)"
    _intrinsics, mismatch = _mode_intrinsics(camera_capture_config, _camera_vertical_offset_px())
    if mismatch is not None:
        return f"The lens calibration does not match this camera mode ({mismatch})"
    return None


def _camera_vertical_offset_px() -> int:
    """The live sensor window (output rows), 0 without a camera."""
    if camera_capture_runtime is None:
        return 0
    return camera_capture_runtime.vertical_crop_status().get("vertical_offset_px", 0)


def _mode_intrinsics(settings: dict, vertical_offset_px) -> tuple:
    """The lens calibration carried into a capture mode, or (None, why it does not apply).

    ``intrinsics.for_mode`` moves the principal point into every mode with a
    known frame mapping; ``mode_mismatch`` still refuses another orientation or
    ScalerCrop, and an unmapped mode reports its size difference. (None, None)
    without a calibration.
    """
    if camera_intrinsics is None:
        return None, None
    derived = intrinsics_for_mode(
        camera_intrinsics, settings.get("width"), settings.get("height"), vertical_offset_px
    )
    if derived is None:
        mismatch = camera_intrinsics.mode_mismatch(settings)
        return None, mismatch or f"no frame mapping for sensor window {vertical_offset_px!r}"
    mismatch = derived.mode_mismatch(settings)
    return (None, mismatch) if mismatch is not None else (derived, None)


def _active_camera_intrinsics():
    """The lens model of the live capture mode, None when it has none."""
    return _mode_intrinsics(camera_capture_config, _camera_vertical_offset_px())[0]


def _save_camera_marks() -> None:
    """Persist the target line and the ball mark together (one file, see angle_reference).

    Saved in the canonical 640x400 frame when the live mode has a known
    mapping, so a restart in another mapped mode restores them; else in the
    live mode's own pixels.
    """
    target, ball = angle_reference.target_line, angle_reference.ball_mark
    geometry = _camera_target_geometry()
    if geometry is not None and (target is not None or ball is not None):
        canonical = canonical_geometry(geometry)
        canonical_intrinsics = (
            intrinsics_for_mode(camera_intrinsics, canonical["width"], canonical["height"], 0)
            if camera_intrinsics is not None
            else None
        )
        carried = carry_camera_marks(target, ball, canonical, canonical_intrinsics)
        if carried.discarded is None and not carried.ball_outside:
            target, ball = carried.target, carried.ball
    save_camera_marks(resolve_target_line_path(), target, ball)


def _store_camera_target_line(target, event: str) -> None:
    """Set (or with None clear) the target line, persist it and restart the horizontal offset."""
    angle_reference.target_line = target
    _save_camera_marks()
    if target is None:
        logger.info("[SERVER] Camera target line cleared (%s)", event)
    else:
        logger.info(
            "[SERVER] Camera target line set: azimuth %+.2f deg, elevation %+.2f deg "
            "(frame %.1f, %.1f px)",
            target.azimuth_deg,
            target.elevation_deg,
            target.frame_x_px,
            target.frame_y_px,
        )
    # Its camera directions are measured from another line from now on.
    angle_reference.reset(event, axes=("horizontal",))
    _log_angle_reference(event)


def _set_camera_target_from_tap(tap) -> None:
    """Handle ``target_tap`` of ``set_camera_capture_settings``: a preview position or None."""
    if tap is None:
        _store_camera_target_line(None, "target_cleared")
        return
    if not isinstance(tap, dict):
        raise ValueError("The target must be an object with x_pct and y_pct")
    reason = _camera_target_unavailable_reason()
    if reason is not None:
        raise ValueError(reason)
    config = camera_capture_config
    target = target_line_from_tap(
        tap.get("x_pct"),
        tap.get("y_pct"),
        width=int(config["width"]),
        height=int(config["height"]),
        roll_correction_deg=float(config.get("roll_correction_deg", 0.0)),
        horizontal_pixel_sign=-1.0 if config.get("mirror_horizontal") else 1.0,
        intrinsics=_active_camera_intrinsics(),
        geometry=_camera_target_geometry(),
    )
    _store_camera_target_line(target, "target_set")


def _reset_camera_ball_anchors() -> None:
    """Forget where the ball flight and its low-light shadow saw the resting ball.

    Their tee trackers and ground anchors (the camera pose) start afresh, so no
    ball seen before (another sensor window, another ball mark) moves them.
    """
    global camera_ball_flight_reference_tracker  # pylint: disable=global-statement
    global camera_ball_flight_ground_anchor  # pylint: disable=global-statement
    global camera_lowlight_reference_tracker, camera_lowlight_ground_anchor
    from .camera.club_delivery import ReferenceBallTracker  # noqa: PLC0415
    from .camera.departed_ball import GroundBallAnchor  # noqa: PLC0415

    camera_ball_flight_reference_tracker = ReferenceBallTracker()
    camera_ball_flight_ground_anchor = GroundBallAnchor()
    camera_lowlight_reference_tracker = ReferenceBallTracker()
    camera_lowlight_ground_anchor = GroundBallAnchor()


def _store_camera_ball_mark(ball, event: str) -> None:
    """Set (or with None clear) the ball mark, persist it and restart the ball anchors."""
    angle_reference.ball_mark = ball
    _save_camera_marks()
    if ball is None:
        logger.info("[SERVER] Camera ball mark cleared (%s)", event)
    else:
        logger.info(
            "[SERVER] Camera ball mark set: frame %.1f, %.1f px", ball.frame_x_px, ball.frame_y_px
        )
    # Balls seen away from the new mark must not keep the pose or the search there.
    _reset_camera_ball_anchors()
    _log_angle_reference(event)


def _set_camera_ball_from_tap(tap) -> None:
    """Handle ``ball_tap`` of ``set_camera_capture_settings``: a preview position or None.

    Unlike the target, the ball mark is a pixel, not a direction: it needs no
    lens calibration.
    """
    if tap is None:
        _store_camera_ball_mark(None, "ball_cleared")
        return
    if not isinstance(tap, dict):
        raise ValueError("The ball must be an object with x_pct and y_pct")
    config = camera_capture_config
    ball = ball_mark_from_tap(
        tap.get("x_pct"),
        tap.get("y_pct"),
        width=int(config["width"]),
        height=int(config["height"]),
        roll_correction_deg=float(config.get("roll_correction_deg", 0.0)),
        geometry=_camera_target_geometry(),
    )
    _store_camera_ball_mark(ball, "ball_set")


def _camera_ball_mark_px() -> tuple[float, float] | None:
    """The tapped resting ball in saved-frame pixels for the ball flight, if one is set."""
    ball = angle_reference.ball_mark
    return ball.frame_px if ball is not None else None


def _carry_camera_marks_over_geometry_change(previous_geometry: dict | None) -> None:
    """A capture-mode or sensor-window change carries both marks over, else clears them.

    Between modes with a known frame mapping the target keeps its direction and
    the ball mark its spot (``angle_reference.carry_camera_marks``); a ball mark
    outside the new frame is dropped alone (``ball_mark_outside_mode``). Any
    other change clears both. Both offsets restart either way; the caller
    restarts the ball anchors with the new geometry.
    """
    geometry = _camera_target_geometry()
    if previous_geometry is None or geometry == previous_geometry:
        return
    target, ball = angle_reference.target_line, angle_reference.ball_mark
    if target is not None or ball is not None:
        carried = carry_camera_marks(target, ball, geometry, _active_camera_intrinsics())
        if carried.discarded is not None:
            if ball is not None:
                angle_reference.ball_mark = None
                logger.info("[SERVER] Camera ball mark cleared (camera_geometry_changed)")
            _store_camera_target_line(None, "camera_geometry_changed")
        else:
            angle_reference.target_line = carried.target
            angle_reference.ball_mark = carried.ball
            _save_camera_marks()
            _log_carried_camera_marks(carried, ball, geometry)
            _log_angle_reference("camera_geometry_changed")
    _reset_angle_reference("camera_geometry_changed")


def _log_carried_camera_marks(carried, previous_ball, geometry: dict) -> None:
    """Log where the marks went in the new capture mode."""
    size = f"{geometry.get('width')}x{geometry.get('height')}"
    if carried.target is not None:
        logger.info(
            "[SERVER] Camera target line kept in %s: azimuth %+.2f deg, frame %.1f, %.1f px",
            size,
            carried.target.azimuth_deg,
            carried.target.frame_x_px,
            carried.target.frame_y_px,
        )
    if carried.ball is not None:
        logger.info(
            "[SERVER] Camera ball mark moved into %s: frame %.1f, %.1f px",
            size,
            carried.ball.frame_x_px,
            carried.ball.frame_y_px,
        )
    elif carried.ball_outside:
        logger.info(
            "[SERVER] Camera ball mark cleared (%s): frame %.1f, %.1f px lies outside %s",
            BALL_MARK_OUTSIDE_MODE,
            previous_ball.frame_x_px,
            previous_ball.frame_y_px,
            size,
        )
        _log_angle_reference(BALL_MARK_OUTSIDE_MODE)


def restore_camera_marks() -> None:
    """Load the marks saved for this device setup at startup; stale ones are discarded.

    The ball mark needs only the frame geometry; the target also the lens
    calibration of this mode. Marks saved in another capture mode are carried
    into this one where the frame mapping is known.
    """
    geometry = _camera_target_geometry()
    if geometry is None:
        return
    target, ball, reason = load_camera_marks(
        resolve_target_line_path(), geometry, _active_camera_intrinsics()
    )
    if reason == BALL_MARK_OUTSIDE_MODE:
        logger.warning(
            "[SERVER] Saved camera ball mark not applied (%s): it lies outside %sx%s",
            reason,
            geometry.get("width"),
            geometry.get("height"),
        )
    elif reason is not None:
        logger.warning("[SERVER] Saved camera target line and ball mark discarded: %s", reason)
    if ball is not None:
        angle_reference.ball_mark = ball
        logger.info(
            "[SERVER] Camera ball mark restored: frame %.1f, %.1f px, set %s",
            ball.frame_x_px,
            ball.frame_y_px,
            datetime.fromtimestamp(ball.set_at).isoformat(timespec="seconds"),
        )
    if target is None:
        return
    if _camera_target_unavailable_reason() is not None:
        logger.warning(
            "[SERVER] Saved camera target line not applied: %s",
            _camera_target_unavailable_reason(),
        )
        return
    angle_reference.target_line = target
    logger.info(
        "[SERVER] Camera target line restored: azimuth %+.2f deg, set %s",
        target.azimuth_deg,
        datetime.fromtimestamp(target.set_at).isoformat(timespec="seconds"),
    )


def _camera_capture_settings_payload() -> dict:
    """Return camera controls and capture state for the Camera tab."""
    payload = dict(camera_capture_config)
    payload["available"] = camera_capture_runtime is not None
    payload.setdefault("alignment_x_pct", 50.0)
    payload.setdefault("alignment_y_pct", 50.0)
    target = angle_reference.target_line
    payload["target_line"] = target.to_log() if target is not None else None
    payload["target_line_unavailable_reason"] = _camera_target_unavailable_reason()
    ball = angle_reference.ball_mark
    payload["ball_mark"] = ball.to_log() if ball is not None else None
    if camera_capture_runtime is not None:
        payload.update(camera_capture_runtime.status())
        payload.update(camera_capture_runtime.vertical_crop_status())
        payload.update(camera_capture_runtime.capture_mode_status())
        payload["exposure_us"] = getattr(
            camera_capture_runtime.settings,
            "exposure_us",
            payload.get("exposure_us"),
        )
        payload["gain"] = getattr(
            camera_capture_runtime.settings,
            "gain",
            payload.get("gain"),
        )
        frame_period_us = round(1_000_000 / camera_capture_runtime.settings.fps)
        payload["max_exposure_us"] = frame_period_us - 1
    return payload


@socketio.on("get_camera_capture_settings")
def handle_get_camera_capture_settings():
    """Send current high-speed capture settings to the requesting UI."""
    socketio.emit("camera_capture_settings", _camera_capture_settings_payload())


def _warn_if_clips_too_short_for_locator(settings, ball_locator: str) -> None:
    """Warn when every clip ends before the disappeared-ball locator's late window.

    Runs at camera start and after a capture-mode change, which keeps the
    post-trigger milliseconds but changes the frame rate.
    """
    from openflight.camera.departed_ball import (  # noqa: PLC0415
        MIN_CLIP_POST_FRAMES,
        min_post_ms,
    )

    if ball_locator == "disappeared" and settings.post_frames < MIN_CLIP_POST_FRAMES:
        logger.warning(
            "[CAMERA] %d post-trigger frames (%.0f ms at %.0f fps) are too few for the "
            "disappeared-ball locator (needs %d, the trigger frame lands up to a few frames "
            "late); shots fall back to the legacy locator. Use --camera-capture-post-ms %d "
            "or more.",
            settings.post_frames,
            settings.post_ms,
            settings.fps,
            MIN_CLIP_POST_FRAMES,
            min_post_ms(settings.fps),
        )


@socketio.on("set_camera_capture_settings")
def handle_set_camera_capture_settings(data):
    """Apply live-safe camera controls and alignment-guide position."""
    global camera_reference_ball_tracker  # pylint: disable=global-statement
    if camera_capture_runtime is None:
        socketio.emit(
            "camera_capture_settings_error",
            {"error": "High-speed camera capture is not running"},
        )
        return
    if not isinstance(data, dict):
        socketio.emit(
            "camera_capture_settings_error",
            {"error": "Camera settings must be an object"},
        )
        return

    try:
        if "target_tap" in data:
            if len(data) != 1:
                raise ValueError("Set the target separately from other controls")
            _set_camera_target_from_tap(data["target_tap"])
            socketio.emit("camera_capture_settings", _camera_capture_settings_payload())
            return
        if "ball_tap" in data:
            if len(data) != 1:
                raise ValueError("Set the ball separately from other controls")
            _set_camera_ball_from_tap(data["ball_tap"])
            socketio.emit("camera_capture_settings", _camera_capture_settings_payload())
            return
        if "capture_mode" in data:
            if len(data) != 1:
                raise ValueError("Change camera mode separately from other controls")
            with _shot_finalization_condition:
                if _shot_finalization_order or _shot_finalization_running:
                    raise RuntimeError(
                        "A shot is being processed; wait before changing camera mode"
                    )
        if ("exposure_us" in data or "gain" in data) and "auto_exposure_enabled" not in data:
            raise ValueError("Camera exposure and gain are managed automatically")
        alignment_x_pct = float(
            data.get("alignment_x_pct", camera_capture_config.get("alignment_x_pct", 50.0))
        )
        alignment_y_pct = float(
            data.get("alignment_y_pct", camera_capture_config.get("alignment_y_pct", 50.0))
        )
        if not 0.0 <= alignment_x_pct <= 100.0:
            raise ValueError("horizontal alignment must be between 0 and 100 percent")
        if not 0.0 <= alignment_y_pct <= 100.0:
            raise ValueError("vertical alignment must be between 0 and 100 percent")

        crop_update = {}
        exposure_update = {}
        mode_update = {}
        geometry_before = (
            _camera_target_geometry()
            if "capture_mode" in data or "vertical_offset_px" in data
            else None
        )
        if "capture_mode" in data:
            mode_update = camera_capture_runtime.update_capture_mode(data["capture_mode"])
            from .camera.ball_flight import DEFAULT_BALL_LOCATOR  # noqa: PLC0415

            _warn_if_clips_too_short_for_locator(
                camera_capture_runtime.settings,
                camera_capture_config.get("ball_locator", DEFAULT_BALL_LOCATOR),
            )
        if "capture_mode" in data or "vertical_offset_px" in data:
            from .camera.club_delivery import ReferenceBallTracker

            camera_reference_ball_tracker = ReferenceBallTracker()
            _reset_camera_ball_anchors()
        if "auto_exposure_enabled" in data:
            if "vertical_offset_px" in data:
                raise ValueError("Change exposure and sensor crop separately")
            exposure_update = camera_capture_runtime.update_exposure_mode(
                automatic=data["auto_exposure_enabled"],
                exposure_us=data.get("exposure_us"),
                gain=data.get("gain"),
            )
        if "vertical_offset_px" in data:
            crop_update = camera_capture_runtime.update_vertical_crop(
                int(data["vertical_offset_px"])
            )

        camera_capture_config.update(
            {
                **crop_update,
                **exposure_update,
                **mode_update,
                "alignment_x_pct": alignment_x_pct,
                "alignment_y_pct": alignment_y_pct,
            }
        )
        if geometry_before is not None:
            _refresh_camera_tee_zone_config()
        _carry_camera_marks_over_geometry_change(geometry_before)
        session_log = get_session_logger()
        if session_log:
            session_log.log_config_change(
                {"camera_capture": dict(camera_capture_config)},
                source="camera_ui",
            )
        socketio.emit("camera_capture_settings", _camera_capture_settings_payload())
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        logger.warning("[SERVER] Camera settings update rejected: %s", error)
        socketio.emit("camera_capture_settings_error", {"error": str(error)})


def start_debug_logging():
    """Start logging raw readings to a file."""
    global debug_log_file, debug_log_path  # pylint: disable=global-statement

    # Create logs directory
    log_dir = Path.home() / "openflight_logs"
    log_dir.mkdir(exist_ok=True)

    # Create timestamped log file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    debug_log_path = log_dir / f"debug_{timestamp}.jsonl"
    debug_log_file = open(debug_log_path, "w")  # pylint: disable=consider-using-with

    # Enable radar raw logging
    radar_logger = logging.getLogger("ops243")
    radar_raw_logger = logging.getLogger("ops243.raw")
    radar_logger.setLevel(logging.DEBUG)
    radar_raw_logger.setLevel(logging.DEBUG)

    # Add file handler for raw radar data
    raw_log_path = log_dir / f"radar_raw_{timestamp}.log"
    file_handler = logging.FileHandler(raw_log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    radar_raw_logger.addHandler(file_handler)
    radar_logger.addHandler(file_handler)

    print(f"Debug logging to: {debug_log_path}")
    print(f"Raw radar logging to: {raw_log_path}")
    return str(debug_log_path)


def stop_debug_logging():
    """Stop logging and close the file."""
    global debug_log_file, debug_log_path  # pylint: disable=global-statement

    if debug_log_file:
        debug_log_file.close()
        debug_log_file = None
        print(f"Debug log saved: {debug_log_path}")


def log_debug_reading(reading: SpeedReading):
    """Log a raw reading to the debug file."""
    if debug_log_file:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "type": "reading",
            "speed": reading.speed,
            "direction": reading.direction.value,
            "magnitude": reading.magnitude,
            "unit": reading.unit,
        }
        debug_log_file.write(json.dumps(entry) + "\n")
        debug_log_file.flush()

        # Also print to console for immediate feedback
        print(
            f"[RADAR] {reading.speed:.1f} mph {reading.direction.value} (mag={reading.magnitude})"
        )


def on_live_reading(reading: SpeedReading):
    """Callback for live radar readings - used in debug mode."""
    # Log ALL readings first (before filtering) so we can debug direction issues
    if debug_mode:
        log_debug_reading(reading)

        # Emit ALL readings to UI debug panel (including inbound)
        socketio.emit(
            "debug_reading",
            {
                "speed": reading.speed,
                "direction": reading.direction.value,
                "magnitude": reading.magnitude,
                "timestamp": datetime.now().isoformat(),
                "filtered": reading.direction != Direction.OUTBOUND,
            },
        )

    # Filter out inbound readings for shot detection
    # Note: shot filtering happens in launch_monitor.py but we also filter here
    # for any UI purposes that need only outbound readings
    if reading.direction != Direction.OUTBOUND:
        return


def _get_trigger_status() -> dict:
    """Build trigger status payload for the UI."""
    from .rolling_buffer import RollingBufferMonitor  # pylint: disable=import-outside-toplevel

    is_rolling_buffer = isinstance(monitor, RollingBufferMonitor)
    is_swing_speed = _in_swing_speed_mode()
    session_logger = get_session_logger()
    stats = session_logger.stats if session_logger else {}

    if is_swing_speed:
        mode = "swing-speed"
    elif mock_mode:
        mode = "mock"
    else:
        mode = "rolling-buffer"
    trigger_type = None
    radar_port = None

    if is_rolling_buffer:
        trigger_type = monitor.trigger_type
    if is_rolling_buffer or is_swing_speed:
        if hasattr(monitor, "radar") and hasattr(monitor.radar, "port"):
            radar_port = monitor.radar.port

    return {
        "mode": mode,
        "trigger_type": trigger_type,
        "radar_connected": monitor is not None and not mock_mode,
        "radar_port": radar_port,
        "triggers_total": stats.get("triggers_total", 0),
        "triggers_accepted": stats.get("triggers_accepted", 0),
        "triggers_rejected": stats.get("triggers_rejected", 0),
        "clock_offset_s": client_clocks.status(),
    }


def _current_club_id() -> str:
    """Club id the kiosk should restore after a reload."""
    if monitor is None:
        return ClubType.DRIVER.value
    club = getattr(monitor, "_current_club", None)
    if club is None:
        return ClubType.DRIVER.value
    return getattr(monitor, "_current_club_selection", None) or (
        club.value if hasattr(club, "value") else str(club)
    )


def _session_state_payload(*, include_runtime_meta: bool = False) -> dict:
    """Build the session_state event the UI applies on connect and refresh.

    While the demo runs, its shots and stats stand in for the real session. The caller
    holds _demo_lock, so the payload cannot cross a demo switch on its way out.
    """
    demo_active = demo_session.active
    if demo_active:
        stats = demo_session.stats()
    else:
        stats = monitor.get_session_stats() if monitor else {}
    payload = {
        "stats": stats,
        "shots": _session_shots(),
        "club": _current_club_id(),
        "calibration": _calibration_status(),
        "demo_active": demo_active,
    }
    if include_runtime_meta:
        payload.update(
            {
                "mock_mode": mock_mode,
                "debug_mode": debug_mode,
            }
        )
    return payload


def _calibration_status() -> dict:
    """Which aim calibrations are configured, so clients can label unverified data."""
    return {
        "horizontal_phase_reference": (
            iwr6843_runtime_config.get("horizontal_phase_reference_rad") is not None
        ),
    }


def _session_shots() -> list[dict]:
    """Return current session rows in the UI's shot-shaped payload format."""
    if demo_session.active:
        return [shot_to_dict(shot) for shot in demo_session.shots()]
    if not monitor:
        return []
    if _in_swing_speed_mode():
        return [swing_speed_to_shot_dict(event) for event in monitor.get_events()]
    return [shot_to_dict(shot) for shot in monitor.get_shots()]


def _unregister_camera_replay(shot: Shot) -> None:
    """Revoke live replay access while preserving the session artifacts."""
    replay = getattr(shot, "camera_replay", None)
    replay_id = replay.get("id") if isinstance(replay, dict) else None
    if not replay_id or camera_replay_manager is None:
        return
    try:
        camera_replay_manager.unregister(replay_id)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[CAMERA] Could not unregister replay %s: %s", replay_id, error)


def _delete_session_row(timestamp: str) -> bool:
    """Delete one shot or swing-speed rep by UI timestamp."""
    if not monitor or not timestamp:
        return False

    if _in_swing_speed_mode():
        events = getattr(monitor, "_events", None)
        if events is not None:
            for index, event in enumerate(events):
                if event.timestamp.isoformat() == timestamp:
                    del events[index]
                    return True
        return False

    shots = getattr(monitor, "_shots", None)
    if shots is None:
        return False
    for index, shot in enumerate(shots):
        if shot.timestamp.isoformat() == timestamp:
            _unregister_camera_replay(shot)
            del shots[index]
            return True
    return False


def _emit_sim_snapshot() -> None:
    """Emit the current status of every configured simulator connector.

    The UI builds its connector buttons from ``sim_status`` events, which
    otherwise only fire on connection-state *changes* (see _sim_on_status). A
    client that connects or refreshes after a connector already reached its
    state would miss those events and show no button — the intermittent
    "sometimes the sim status shows, sometimes it doesn't". Replaying a snapshot
    on connect guarantees a button for every enabled connector (sim_connectors
    holds only the enabled ones) carrying its live state.
    """
    for connector in sim_connectors:
        socketio.emit(
            "sim_status",
            {
                "target": connector.name,
                "state": connector.state.value,
                "host": connector.host,
                "port": connector.port,
            },
        )


def _on_power_status(status: PowerStatus) -> None:
    """Publish one battery reading to connected UI clients."""
    socketio.emit("power_status", status.to_dict())


def _log_power_status(status: PowerStatus) -> None:
    """Write throttled battery telemetry into the active session log."""
    session_log = get_session_logger()
    if session_log:
        # CPU temperature and throttle flags ride along: nothing else logs them.
        session_log.log_power_status({**status.to_dict(), **pi_health()})


def start_power_monitor(provider: str) -> None:
    """Start optional battery monitoring without blocking server startup."""
    global power_monitor  # pylint: disable=global-statement
    power_monitor = PowerMonitor(
        provider=provider,
        on_status=_on_power_status,
        on_log=_log_power_status,
    )
    power_monitor.start()
    logger.info("[POWER] Battery monitoring enabled with provider=%s", provider)


def _admit_socket() -> bool:
    """Admit the kiosk and paired devices; remember paired ones so revoking can drop them."""
    if is_local_environ(request.environ):
        return True
    with _pairing_lock:
        if not _request_is_paired():
            return False
        _paired_socket_sids.add(request.sid)
    return True


@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    # Not via cors_allowed_origins: python-engineio < 4.12 passes that callable no environ.
    if not origin_allowed(request.headers.get("Origin"), request.environ):
        raise SocketConnectionRefusedError("origin_not_allowed")
    if not _admit_socket():
        raise SocketConnectionRefusedError("pairing_required")
    print("Client connected")
    _emit_sim_snapshot()
    _emit_profiles()
    _emit_weather_state()
    _emit_voice_callouts()
    if power_monitor and power_monitor.status:
        socketio.emit("power_status", power_monitor.status.to_dict())
    if monitor:
        _reconcile_bag_club()
        with _demo_lock:
            socketio.emit("demo_state", _demo_state_payload())
            socketio.emit("session_state", _session_state_payload(include_runtime_meta=True))
        socketio.emit("trigger_status", _get_trigger_status())


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    with _pairing_lock:
        _paired_socket_sids.discard(request.sid)
    print("Client disconnected")


def _socket_client_kind() -> str:
    """'phone' for a paired device, 'kiosk' for the Pi's own browser."""
    with _pairing_lock:
        if request.sid in _paired_socket_sids:
            return "phone"
    return "kiosk" if is_local_environ(request.environ) else "phone"


@socketio.on("client_clock")
def handle_client_clock(data):
    """Record how far the Pi clock is from the client's; never changes the system time."""
    server_epoch = time.time()
    reading = parse_client_clock(
        data,
        client=_socket_client_kind(),
        server_epoch=server_epoch,
        server_tz_offset_min=local_utc_offset_min(server_epoch),
    )
    if reading is None:
        return
    session_log = get_session_logger()
    session_id = getattr(session_log, "session_id", None)
    if session_log is not None and hasattr(session_log, "log_client_clock"):
        session_log.log_client_clock(reading.to_log())
    if client_clocks.record(reading, session_id):
        offset = format_clock_offset(reading.offset_s)
        logger.warning(
            "[CLOCK] Pi clock (UTC) differs from %s by %s (more than %.0f s); "
            "correct session timestamps as described in docs/reference/session-log.md "
            "(client_clock)",
            reading.client,
            offset,
            CLOCK_OFFSET_WARN_S,
        )
    socketio.emit("trigger_status", _get_trigger_status())


def _emit_weather_state() -> None:
    """Broadcast the range location, the current weather and the density shots fly with.

    Like ``profiles``: every client gets it, so a stale one self-heals on the next change.
    """
    socketio.emit("weather_state", get_weather_store().snapshot(air_density_kg_m3))


@socketio.on("get_weather")
def handle_get_weather():
    """Send the weather state (broadcast, as get_profiles does)."""
    _emit_weather_state()


def _emit_voice_callouts() -> None:
    """Broadcast the callout setting; like ``profiles``, every client gets it."""
    socketio.emit("voice_callouts", get_voice_callout_store().snapshot())


@socketio.on("get_voice_callouts")
def handle_get_voice_callouts():
    """Send the callout setting (broadcast, as get_weather does)."""
    _emit_voice_callouts()


@socketio.on("set_voice_callouts")
def handle_set_voice_callouts(data=None):
    """Change the callout setting from any screen; every screen follows the broadcast.

    A refused change is answered with the current setting too, so the screen that sent it
    drops its optimistic value.
    """
    settings = get_voice_callout_store().update(data)
    _emit_voice_callouts()
    if settings is None:
        return {"ok": False, "error": "invalid_voice_callouts"}
    logger.info(
        "[VOICE] Callouts %s: %s (%s)",
        "on" if settings["enabled"] else "off",
        ", ".join(settings["metrics"]),
        settings["language"] or "browser language",
    )
    return {"ok": True}


@socketio.on("voice_test")
def handle_voice_test():
    """The kiosk's test button: the phones speak the test callout (the Pi has no speaker)."""
    socketio.emit("voice_test", {})
    return {"ok": True}


@socketio.on("get_iwr6843_tilt_calibration")
def handle_get_iwr6843_tilt_calibration():
    """Send the TI tilt calibration state (broadcast, as get_weather does)."""
    _emit_iwr6843_tilt_state("state")


@socketio.on("set_weather_location")
def handle_set_weather_location(data=None):
    """Store the range location (searched by name on the phone, or typed as lat/lon)."""
    location = get_weather_store().set_location(data)
    if location is None:
        return {"ok": False, "error": "invalid_location"}
    logger.info(
        "[WEATHER] Range location set: %s (%.5f, %.5f)",
        location.name,
        location.latitude,
        location.longitude,
    )
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_weather_location"):
        session_log.log_weather_location(location.to_dict())
    _emit_weather_state()
    return {"ok": True}


@socketio.on("weather_report")
def handle_weather_report(data=None):
    """Accept the phone's Open-Meteo fetch or a typed-in reading; never fetched here."""
    server_epoch = time.time()
    store = get_weather_store()
    clock = parse_client_clock(
        data,
        client=_socket_client_kind(),
        server_epoch=server_epoch,
        server_tz_offset_min=local_utc_offset_min(server_epoch),
    )
    try:
        reading = parse_weather_report(
            data, clock=clock, location=store.location, received_monotonic=store.now()
        )
    except WeatherRejected as rejected:
        logger.warning("[WEATHER] Report rejected (%s): %s", rejected.code, rejected)
        return {"ok": False, "error": rejected.code}
    store.record(reading)
    logger.info(
        "[WEATHER] %s: %.1f °C, %.1f hPa, %.0f %% RH%s -> air %.4f kg/m³ (%.0f s old)",
        reading.source,
        reading.temperature_c,
        reading.pressure_hpa,
        reading.humidity_pct,
        " (assumed)" if reading.humidity_assumed else "",
        reading.air_density_kg_m3,
        reading.age_at_receipt_s,
    )
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_weather"):
        session_log.log_weather(reading.to_log())
    _emit_weather_state()
    return {"ok": True}


@socketio.on("weather_fetch_failed")
def handle_weather_fetch_failed(data=None):
    """Log why the phone's weather fetch or GPS range location failed (log only, no broadcast).

    The phone classifies the reason; only the closed-set fields are kept. A malformed report,
    and any report less than 5 s after the last logged one, is ignored.
    """
    failure = parse_weather_fetch_failure(data)
    if failure is None:
        return {"ok": False, "error": "malformed"}
    if not weather_fetch_failure_gate.admit():
        return {"ok": False, "error": "rate_limited"}
    failure["client"] = _socket_client_kind()
    logger.info(
        "[WEATHER] %s: %s failed (%s), %d in a row, next try in %d s",
        failure["client"],
        failure["stage"],
        failure["reason"],
        failure["consecutive_failures"],
        failure["next_retry_s"],
    )
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_weather_fetch_failed"):
        session_log.log_weather_fetch_failed(failure)
    return {"ok": True}


def _phone_clock_estimate():
    """The best phone-minus-Pi offset: a two-way probe, else the phone's client_clock."""
    return phone_clock_probes.estimate(time.monotonic(), client_clocks.status().get("phone"))


def _phone_event_rejected(event: str, rejected: PhoneEventRejected) -> dict:
    logger.info("[PHONE] %s rejected (%s): %s", event, rejected.code, rejected)
    return {"ok": False, "error": rejected.code}


@socketio.on("phone_audio_onset")
def handle_phone_audio_onset(data=None):
    """Log an iOS-companion impact onset matched to the nearest sound edge (log only).

    Nothing here feeds the shot, the carry, the club data or the trigger.
    """
    if _socket_client_kind() != "phone":
        return {"ok": False, "error": "phone_only"}
    server_epoch = time.time()
    server_monotonic_ns = time.monotonic_ns()
    try:
        onset = parse_audio_onset(
            data, server_epoch=server_epoch, server_tz_offset_min=local_utc_offset_min(server_epoch)
        )
    except PhoneEventRejected as rejected:
        return _phone_event_rejected("phone_audio_onset", rejected)
    entry = onset_log_entry(
        onset,
        estimate=_phone_clock_estimate(),
        edges=sound_edge_timing.history(),
        server_monotonic_ns=server_monotonic_ns,
    )
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_phone_audio_onset"):
        session_log.log_phone_audio_onset(entry)
    return {
        "ok": True,
        "edge_match": entry["edge_match"],
        "edge_residual_ms": entry["edge_residual_ms"],
        "clock_source": entry["clock_source"],
    }


@socketio.on("phone_sensors")
def handle_phone_sensors(data=None):
    """Log an iOS-companion sensor snapshot (log only)."""
    if _socket_client_kind() != "phone":
        return {"ok": False, "error": "phone_only"}
    server_epoch = time.time()
    try:
        snapshot, clock = parse_sensors(
            data, server_epoch=server_epoch, server_tz_offset_min=local_utc_offset_min(server_epoch)
        )
    except PhoneEventRejected as rejected:
        return _phone_event_rejected("phone_sensors", rejected)
    entry = {
        **snapshot,
        "server_epoch": clock.server_epoch,
        "client_clock_offset_s": clock.offset_s,
        **_phone_clock_estimate().to_log(),
    }
    session_log = get_session_logger()
    if session_log is not None and hasattr(session_log, "log_phone_sensors"):
        session_log.log_phone_sensors(entry)
    return {"ok": True}


@socketio.on("phone_clock_probe")
def handle_phone_clock_probe(data=None):
    """Two-way clock probe: echo the Pi's wall and monotonic time; take the phone's round trip.

    A payload may carry a new probe (``probe_seq``, ``client_send_epoch_ms``), the
    ``report`` of an answered one (its round trip), or both. Never changes the system time.
    """
    if _socket_client_kind() != "phone":
        return {"ok": False, "error": "phone_only"}
    pi_monotonic_ns = time.monotonic_ns()
    pi_epoch_s = time.time()
    try:
        probe, report = parse_probe(data)
    except PhoneEventRejected as rejected:
        return _phone_event_rejected("phone_clock_probe", rejected)
    sid = request.sid
    if report is not None:
        phone_clock_probes.report(sid, *report)
    if probe is None:
        return {"ok": True}
    return phone_clock_probes.answer(
        sid, probe[0], probe[1], pi_epoch_s=pi_epoch_s, pi_monotonic_ns=pi_monotonic_ns
    )


@socketio.on("get_trigger_status")
def handle_get_trigger_status():
    """Get current trigger/mode status for debug UI."""
    socketio.emit("trigger_status", _get_trigger_status())


@socketio.on("set_club")
def handle_set_club(data):
    """Handle club selection change."""
    club_name = _payload_dict(data).get("club", "driver")
    if isinstance(club_name, str) and club_name in SELECTABLE_CLUB_IDS:
        _select_bag_club(club_name)


def _select_bag_club(selection: str) -> None:
    if monitor:
        category = club_category(selection)
        if selection == category.value:
            monitor.set_club(category)
        else:
            monitor.set_club(category, selection=selection)
    socketio.emit("club_changed", {"club": selection})


def _reconcile_bag_club() -> None:
    clubs = clean_club_ids(get_profile_store().get_active().settings.get("club_ids"))
    if clubs and _current_club_id() not in clubs:
        _select_bag_club(clubs[0])


def _payload_dict(data) -> dict:
    """Normalize a socket payload to a dict, ignoring anything else."""
    return data if isinstance(data, dict) else {}


def _emit_profiles() -> None:
    """Broadcast the authoritative roster + selection.

    Sent after every mutation, including rejected ones, so a stale client
    self-heals on the next round trip instead of needing an error event.
    """
    socketio.emit("profiles", _profiles_payload())


def _profiles_payload() -> dict:
    """The roster snapshot, each profile with its effective ``ball_type``.

    The effective ball type is the profile's own setting, else the ball type of the
    launcher's --typical-spin-table; ``settings`` keeps only what is stored.
    """
    payload = get_profile_store().snapshot()
    for profile in payload["profiles"]:
        profile["ball_type"] = _effective_ball_type(profile["settings"])
    return payload


def _effective_ball_type(settings) -> str:
    """A profile's ball type, the launcher default's ball type when it stores none."""
    return ball_type(settings, ball_type_for_spin_table(typical_spin_table))


@socketio.on("get_profiles")
def handle_get_profiles():
    """Send the roster to a client that asked for it."""
    _emit_profiles()


def _reset_angle_reference_on_profile_switch(previous_id: str) -> None:
    """A different active profile starts the session offsets afresh."""
    if get_profile_store().get_active().id != previous_id:
        _reset_angle_reference("profile_switch")


@socketio.on("set_active_profile")
def handle_set_active_profile(data=None):
    """Change which profile shots are attributed to."""
    store = get_profile_store()
    previous_id = store.get_active().id
    store.set_active(_payload_dict(data).get("profile_id"))
    _reset_angle_reference_on_profile_switch(previous_id)
    if store.get_active().id != previous_id:
        # Re-selecting the active profile (tapping its own card) keeps the session.
        ops_spin_session_median.reset()
        _reset_club_data_state("profile switch")
    _reconcile_bag_club()
    _emit_profiles()


@socketio.on("set_profile_clubs")
def handle_set_profile_clubs(data=None):
    """Save a bag on the device and acknowledge disk-persistence failures."""
    payload = _payload_dict(data)
    saved = get_profile_store().set_clubs(payload.get("profile_id"), payload.get("club_ids"))
    if saved:
        _reconcile_bag_club()
    _emit_profiles()
    return {"ok": saved}


@socketio.on("set_profile_units")
def handle_set_profile_units(data=None):
    """Synchronize speed/distance preferences across clients and restarts."""
    payload = _payload_dict(data)
    saved = get_profile_store().set_units(payload.get("profile_id"), payload.get("unit_system"))
    _emit_profiles()
    return {"ok": saved}


@socketio.on("set_profile_driver_tee")
def handle_set_profile_driver_tee(data=None):
    """Save a profile's driver tee height (ball bottom above the mat, metres)."""
    payload = _payload_dict(data)
    saved = get_profile_store().set_driver_tee_height(
        payload.get("profile_id"), payload.get("driver_tee_height_m")
    )
    _emit_profiles()
    return {"ok": saved}


@socketio.on("set_profile_ball_type")
def handle_set_profile_ball_type(data=None):
    """Save a profile's ball type ("standard" or "range"); its next shot flies that table."""
    payload = _payload_dict(data)
    saved = get_profile_store().set_ball_type(payload.get("profile_id"), payload.get("ball_type"))
    _emit_profiles()
    return {"ok": saved}


@socketio.on("add_profile")
def handle_add_profile(data=None):
    """Add a profile and make it active."""
    store = get_profile_store()
    previous_id = store.get_active().id
    added = store.add(_payload_dict(data).get("name"))
    _reset_angle_reference_on_profile_switch(previous_id)
    if added is not None:
        # A rejected name leaves the active profile, and so its session, in place.
        ops_spin_session_median.reset()
        _reset_club_data_state("profile switch")
    _emit_profiles()


@socketio.on("rename_profile")
def handle_rename_profile(data=None):
    """Rename a profile. Its shots keep their id and stay attached."""
    payload = _payload_dict(data)
    get_profile_store().rename(payload.get("profile_id"), payload.get("name"))
    _emit_profiles()


@socketio.on("remove_profile")
def handle_remove_profile(data=None):
    """Delete a profile. Refused for the active, the last, or one with session rows."""
    profile_id = str(_payload_dict(data).get("profile_id") or "").strip()
    if profile_id and _profile_has_session_rows(profile_id):
        _emit_profiles()
        return
    get_profile_store().remove(profile_id)
    _emit_profiles()


@socketio.on("set_training_implement")
def handle_set_training_implement(data):
    """Handle swing speed training implement selection."""
    implement = data.get("implement", "driver") if isinstance(data, dict) else "driver"
    label = TRAINING_IMPLEMENT_LABELS.get(implement)
    if not label:
        socketio.emit("training_implement_error", {"error": "Unknown training implement"})
        return

    if monitor and hasattr(monitor, "set_training_implement"):
        monitor.set_training_implement(implement, label)
    socketio.emit(
        "training_implement_changed",
        {"implement": implement, "label": label},
    )


def _profile_has_session_rows(profile_id: str) -> bool:
    """True when the live session still has rows stamped with this profile."""
    if not monitor or not profile_id:
        return False

    if _in_swing_speed_mode():
        return any(getattr(event, "profile_id", "") == profile_id for event in monitor.get_events())

    if hasattr(monitor, "get_shots"):
        return any(getattr(shot, "profile_id", "") == profile_id for shot in monitor.get_shots())
    return False


def _clear_profile_rows(profile_id: str) -> None:
    """Remove one profile's shots or swing-speed reps from the active monitor.

    Matching is exact on the id. The old name-keyed code folded case, so two
    profiles whose names differed only in case cleared each other.
    """
    if not monitor or not profile_id:
        return

    if _in_swing_speed_mode():
        events = getattr(monitor, "_events", None)
        if events is not None:
            events[:] = [
                event for event in events if getattr(event, "profile_id", "") != profile_id
            ]
        return

    shots = getattr(monitor, "_shots", None)
    if shots is not None:
        removed = [shot for shot in shots if getattr(shot, "profile_id", "") == profile_id]
        shots[:] = [shot for shot in shots if getattr(shot, "profile_id", "") != profile_id]
        for shot in removed:
            _unregister_camera_replay(shot)
        return

    if hasattr(monitor, "clear_session"):
        monitor.clear_session()


@socketio.on("clear_session")
def handle_clear_session(data=None):
    """Clear recorded rows for one profile only."""
    raw_id = _payload_dict(data).get("profile_id")
    profile_id = str(raw_id).strip() if raw_id else get_profile_store().get_active().id
    with _demo_lock:
        if demo_session.active:
            demo_session.clear(profile_id)
        else:
            _clear_profile_rows(profile_id)
            _reset_angle_reference("session_cleared")
            ops_spin_session_median.reset()
            _reset_club_data_state("session clear")
        socketio.emit(
            "session_cleared",
            {"profile_id": profile_id, "shots": _session_shots()},
        )


@socketio.on("upload_cloud")
def handle_upload_cloud():
    """Manually trigger upload of completed session logs."""
    threading.Thread(target=_run_cloud_push_for_ui, daemon=True).start()


@socketio.on("get_session")
def handle_get_session():
    """Get current session data."""
    if monitor:
        with _demo_lock:
            socketio.emit("session_state", _session_state_payload())


@socketio.on("delete_shot")
def handle_delete_shot(data):
    """Delete one recorded shot or swing-speed rep from the current session."""
    timestamp = data.get("timestamp") if isinstance(data, dict) else None
    with _demo_lock:
        if demo_session.active:
            deleted = demo_session.delete(timestamp)
        else:
            deleted = _delete_session_row(timestamp)
            if deleted:
                ops_spin_session_median.forget(timestamp)
            session_log = get_session_logger() if deleted else None
            if session_log is not None:
                session_log.log_shot_deleted(timestamp)

        if not deleted:
            socketio.emit("delete_shot_error", {"error": "Shot not found"})
            return

        socketio.emit("session_state", _session_state_payload())


@socketio.on("simulate_shot")
def handle_simulate_shot():
    """Simulate a shot (only works in mock mode)."""
    if monitor and isinstance(monitor, (MockLaunchMonitor, MockSwingSpeedMonitor)):
        monitor.simulate_shot()


@socketio.on("toggle_debug")
def handle_toggle_debug():
    """Toggle debug mode on/off."""
    global debug_mode  # pylint: disable=global-statement

    debug_mode = not debug_mode

    if debug_mode:
        log_path = start_debug_logging()
        socketio.emit("debug_toggled", {"enabled": True, "log_path": log_path})
        print("Debug mode ENABLED")
    else:
        stop_debug_logging()
        socketio.emit("debug_toggled", {"enabled": False})
        print("Debug mode DISABLED")


@socketio.on("get_debug_status")
def handle_get_debug_status():
    """Get current debug mode status."""
    socketio.emit(
        "debug_status",
        {
            "enabled": debug_mode,
            "log_path": str(debug_log_path) if debug_log_path else None,
        },
    )


# Radar tuning state
radar_config = {
    "min_speed": 10,
    "max_speed": 220,
    "min_magnitude": 0,
    "transmit_power": 0,
}


@socketio.on("get_radar_config")
def handle_get_radar_config():
    """Get current radar configuration."""
    socketio.emit("radar_config", radar_config)


@socketio.on("reinitialize_radar")
def handle_reinitialize_radar():
    """Manually recover OPS without restarting the session or other sensors."""
    from .rolling_buffer import RollingBufferMonitor

    if not _ops_control_lock.acquire(blocking=False):
        return {"ok": False, "code": "busy"}
    try:
        with shutdown_lock:
            if shutdown_cleanup_started:
                return {"ok": False, "code": "shutdown"}
            if mock_mode or not isinstance(monitor, RollingBufferMonitor):
                return {"ok": False, "code": "unsupported"}
            if monitor.trigger_type not in ("sound", "ops-speed"):
                return {"ok": False, "code": "unsupported"}
            if demo_session.active:
                return {"ok": False, "code": "demo"}
            if _shot_capture_in_progress():
                return {"ok": False, "code": "busy"}
            monitor.reinitialize_radar()
            return {"ok": True, "code": "commands_sent"}
    except Exception:
        logger.exception("[SERVER] Manual OPS recovery failed")
        return {"ok": False, "code": "failed"}
    finally:
        _ops_control_lock.release()


@socketio.on("set_radar_config")
def handle_set_radar_config(data):
    """Serialize tuning against manual recovery."""
    if not _ops_control_lock.acquire(blocking=False):
        socketio.emit("radar_config", radar_config, to=request.sid)
        socketio.emit("radar_config_error", {"code": "busy"}, to=request.sid)
        return
    try:
        _apply_radar_config(data)
    finally:
        _ops_control_lock.release()


def _apply_radar_config(data):
    """Update radar configuration."""
    global radar_config  # pylint: disable=global-statement

    if not monitor or (mock_mode and not mock_swing_speed_mode):
        log_session_error(
            "Radar config update rejected: radar not connected",
            component="server",
            context={"stage": "set_radar_config", "mock_mode": mock_mode},
        )
        socketio.emit("radar_config_error", {"error": "Radar not connected"})
        return

    try:
        is_swing_speed = _in_swing_speed_mode()

        # Update min speed filter
        if "min_speed" in data:
            new_min = int(data["min_speed"])
            monitor.radar.set_min_speed_filter(new_min)
            if is_swing_speed:
                monitor.trigger_threshold_mph = float(new_min)
            radar_config["min_speed"] = new_min
            print(f"Set min speed filter: {new_min} mph")

        # Update max speed filter. 0 must still be forwarded: AN-010-AD (p10)
        # defines "R<0 resets to no limit", so it is how the UI clears a
        # previously-set ceiling. Swallowing it would leave the old ceiling
        # active on the radar while radar_config claimed no limit.
        if "max_speed" in data:
            new_max = int(data["max_speed"])
            monitor.radar.set_max_speed_filter(new_max)
            if is_swing_speed:
                monitor.max_speed_mph = None if new_max <= 0 else float(new_max)
            radar_config["max_speed"] = new_max
            print(f"Set max speed filter: {new_max} mph")

        # Update magnitude filter
        if "min_magnitude" in data:
            new_mag = int(data["min_magnitude"])
            monitor.radar.set_magnitude_filter(min_mag=new_mag)
            radar_config["min_magnitude"] = new_mag
            print(f"Set min magnitude filter: {new_mag}")

        # Update transmit power (0=max, 7=min)
        if "transmit_power" in data:
            new_power = int(data["transmit_power"])
            if 0 <= new_power <= 7:
                monitor.radar.set_transmit_power(new_power)
                radar_config["transmit_power"] = new_power
                print(f"Set transmit power: {new_power}")

        # Log config change
        session_logger = get_session_logger()
        if session_logger:
            session_logger.log_config_change(radar_config.copy(), source="user")

        # Legacy debug logging
        if debug_mode and debug_log_file:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "type": "config_change",
                "config": radar_config.copy(),
            }
            debug_log_file.write(json.dumps(entry) + "\n")
            debug_log_file.flush()

        socketio.emit("radar_config", radar_config)

    except Exception as e:
        logger.warning("[SERVER] Error setting radar config: %s", e, exc_info=True)
        log_session_error(
            "Radar config update failed",
            component="server",
            context={"stage": "set_radar_config", "requested": data},
            exc=e,
        )
        socketio.emit("radar_config_error", {"error": str(e)})


@socketio.on("shutdown")
def handle_shutdown():
    """Cleanly shut down the server and all hardware."""
    logger.info("[SERVER] Shutdown requested from UI (WebSocket)")
    socketio.emit("shutdown_ack", {"message": "Shutting down..."})
    threading.Thread(target=_shutdown_process_after_delay, daemon=True).start()


def on_shot_processing(state: str) -> None:
    """Forward the rolling-buffer processing lifecycle to the UI."""
    global _shot_processing_active  # pylint: disable=global-statement

    with _shot_finalization_condition:
        _shot_processing_active = state in ("capturing", "calculating")
    _emit_real_shot_event("shot_processing", {"state": state})


def _clear_shot_processing() -> None:
    """End the processing state of a shot that is dropped instead of registered."""
    global _shot_processing_active  # pylint: disable=global-statement

    with _shot_finalization_condition:
        _shot_processing_active = False


def _ensure_standard_air_carry(shot: Shot) -> None:
    """Fly the export carry at the fixed --air-density if finalization skipped it.

    _finalize_shot_metrics only flies it while a simulator is connected; a simulator that
    connected after that must still get the fixed-density carry, never the weather one.
    """
    if shot.carry_model != "ballistic" or shot.carry_standard_air_yards is not None:
        return
    conditions = resolve_launch(
        shot,
        typical_spin_scale=typical_spin_scale,
        typical_spin_table=shot.typical_spin_table or _shot_typical_spin_table(shot),
    )
    if conditions is not None:
        shot.carry_standard_air_yards = simulate(
            conditions,
            air_density=air_density_kg_m3,
            range_ball_drag_slope=range_ball_drag_slope,
        ).carry_yards


def _forward_shot_to_simulators(shot: Shot) -> None:
    """Resolve a shot once and fan it out to every connected simulator.

    The shot pipeline is untouched: this is called after the UI emit and never
    raises into it. A shot number is only allocated when at least one connector
    is connected, so the sequence doesn't drift while sims are offline. A demo
    shot is never sent.
    """
    if shot.demo or not any(c.is_connected() for c in sim_connectors):
        return
    _ensure_standard_air_carry(shot)
    try:
        resolved = resolve_shot(shot, sim_player_state)
    except IncompleteShotError as e:
        logger.warning("[sim] shot not sendable: %s", e)
        socketio.emit("sim_shot_dropped", {"reason": str(e)})
        return

    values = resolved.as_values()
    for connector in sim_connectors:
        if not connector.is_connected():
            continue
        try:
            # Sends are synchronous on this thread, in connector order. Local sims
            # (the only target today) ack in microseconds; if a remote or laggy sim
            # is ever added, move this behind a per-connector send queue so one slow
            # sim can't stall delivery to the others (PR #115 review #2).
            connector.send_shot(resolved)
        except OSError as e:
            logger.warning("[sim] %s send failed: %s", connector.name, e)
            socketio.emit("sim_send_failed", {"target": connector.name, "reason": str(e)})
            continue
        sl = get_session_logger()
        if sl:
            sl.log_sim_send(
                target=connector.name,
                shot_number=resolved.shot_number,
                provenance=resolved.provenance,
                values=values,
            )
        socketio.emit(
            "sim_shot",
            {
                "target": connector.name,
                "shot_number": resolved.shot_number,
                "fields": connector.codec.fields_for_target(),
                "values": values,
                "provenance": resolved.provenance,
            },
        )
        if debug_mode:
            measured = sum(1 for p in resolved.provenance.values() if p == "measured")
            calculated = sum(1 for p in resolved.provenance.values() if p == "calculated")
            estimated = len(resolved.provenance) - measured - calculated
            logger.info(
                "[sim] → %s shot #%d: ball=%.1f vla=%.1f hla=%.1f spin=%.0f axis=%.1f "
                "carry=%.1f (%dM/%dC/%dE)",
                connector.name,
                resolved.shot_number,
                resolved.ball_speed_mph,
                resolved.vla,
                resolved.hla,
                resolved.total_spin_rpm,
                resolved.spin_axis_deg,
                resolved.carry_yards,
                measured,
                calculated,
                estimated,
            )


def _sim_on_status(target: str, event) -> None:
    """Relay a connector status change to the UI and session log."""
    state = event.state.value
    if state == "connected":
        logger.info("[sim] %s connected (%s:%s)", target, event.host, event.port)
    elif state == "reconnecting":
        logger.info(
            "[sim] %s reconnecting — attempt %s, retry in %.0fs",
            target,
            event.attempt,
            event.next_retry_in_s,
        )
    elif state == "error":
        logger.warning("[sim] %s error: %s", target, event.message)
    elif debug_mode:
        logger.info("[sim] %s %s", target, state)
    socketio.emit(
        "sim_status",
        {
            "target": target,
            "state": event.state.value,
            "host": event.host,
            "port": event.port,
            "attempt": event.attempt,
            "next_retry_in_s": event.next_retry_in_s,
            "message": event.message,
        },
    )
    sl = get_session_logger()
    if sl:
        sl.log_sim_status(
            target=target,
            state=event.state.value,
            host=event.host,
            port=event.port,
            message=event.message,
            attempt=event.attempt,
            next_retry_in_s=event.next_retry_in_s,
        )


def _sim_on_inbound(target: str, event) -> None:
    """Apply an inbound simulator event (player/club update, error, ack)."""
    if isinstance(event, PlayerUpdate):
        sim_player_state.apply(event)
        _note_sim_handedness(event.handed)
        club_value = sim_player_state.club.value
        logger.info("[sim] ← %s player update: club=%s", target, club_value)
        socketio.emit(
            "sim_player",
            {"target": target, "handed": sim_player_state.handed, "club": club_value},
        )
        sl = get_session_logger()
        if sl:
            sl.log_sim_player(target=target, handed=sim_player_state.handed, club=club_value)
        # The monitor owns current-club state for shot tagging and carry/spin
        # model selection; keep it in sync with the sim's canonical club.
        if monitor is not None:
            try:
                monitor.set_club(sim_player_state.club)
            except Exception:  # pylint: disable=broad-except
                logger.exception("[sim] monitor.set_club failed")
        socketio.emit("club_changed", {"club": club_value})
    elif isinstance(event, SimError):
        logger.warning("[sim] ← %s error: %s", target, event.message)
        socketio.emit("sim_status", {"target": target, "state": "error", "message": event.message})
    elif isinstance(event, ShotAck):
        if not event.ok:
            logger.info("[sim] ← %s rejected shot %s: %s", target, event.shot_number, event.message)
        elif debug_mode:
            logger.info("[sim] ← %s ack: shot %s ok", target, event.shot_number)


def _apply_calculated_spin(shot: Shot) -> bool:
    """Replace radar-measured spin with the kinematic estimate.

    The 24 GHz OPS return carries no usable spin line (see
    spin_estimate.py), so when the vertical launch angle was actually
    measured (radar/camera, not the club-table estimate), spin_rpm is
    rewritten with 170*v*sin(LA)^1.2. The displaced measured value is
    kept in spin_rpm_measured for offline scoring. Returns True when
    the shot was rewritten.
    """
    if shot.launch_angle_vertical is None:
        return False
    if shot.launch_angle_vertical_source not in ("radar", "camera"):
        return False
    spin_calc = calculated_spin_rpm(shot.ball_speed_mph, shot.launch_angle_vertical)
    if spin_calc is None:
        return False
    shot.spin_rpm_measured = shot.spin_rpm
    shot.spin_rpm = spin_calc
    shot.spin_confidence = SPIN_CONFIDENCE_HIGH
    shot.spin_source = "calculated"
    shot.spin_rejection_reason = None
    logger.info(
        "[SERVER] Calculated spin: %.0f rpm (v=%.1f mph, LA=%.1f deg, measured was %s)",
        spin_calc,
        shot.ball_speed_mph,
        shot.launch_angle_vertical,
        "%.0f rpm" % shot.spin_rpm_measured if shot.spin_rpm_measured else "none",
    )
    return True


# Confidence floor/ceiling for measured angles. A measured angle always beats
# the table fallback (0.5), but only a corroborated, tightly-agreeing estimate
# earns the top of the range.
ANGLE_CONFIDENCE_FLOOR = 0.55
ANGLE_CONFIDENCE_CEILING = 0.95
VERTICAL_SPREAD_FULL_CONFIDENCE_DEG = 2.0
VERTICAL_SPREAD_ZERO_CONFIDENCE_DEG = 10.0

# Spin axis is a difference of two experimental estimates (horizontal launch
# minus club path); it should not appear the moment club path becomes
# non-null, only once the weaker of the two legs (horizontal launch) clears
# this bar.
SPIN_AXIS_MIN_CONFIDENCE = 0.6


def vertical_confidence(measurement) -> float:
    """Vertical launch confidence from channel agreement and corroboration.

    ``component_std_deg`` measures cross-channel disagreement, which only
    means something when two channels were actually compared. When
    ``single_channel`` is set, that comparison never happened: the value is
    either ``None`` (no second component existed) or describes a
    disagreement that was already resolved by discarding the bad channel --
    reusing it as a spread penalty would double-count a fault this function
    already derates via SINGLE_CHANNEL_CONFIDENCE_FACTOR, and np.std of the
    one surviving value is spuriously 0.0 ("perfect agreement") in the
    discarded-channel case. So single-channel results never derive their
    agreement score from component_std_deg at all -- both single-channel
    states (no second channel, or one caught and discarded) get the same
    fixed "one channel, no corroboration" score before the derating factor
    is applied, which is the honest answer: in both cases we have exactly
    one channel's word for it.
    """
    single_channel = getattr(measurement, "single_channel", False)
    spread = getattr(measurement, "component_std_deg", None)
    if single_channel or spread is None:
        # The `spread is None` half of this is defensive only: reachable
        # today solely via single_channel (handled above), kept in case a
        # future corroborated estimator omits component_std_deg.
        score = 0.5
    else:
        span = VERTICAL_SPREAD_ZERO_CONFIDENCE_DEG - VERTICAL_SPREAD_FULL_CONFIDENCE_DEG
        score = 1.0 - (float(spread) - VERTICAL_SPREAD_FULL_CONFIDENCE_DEG) / span
        score = min(1.0, max(0.0, score))
    if single_channel:
        from .iwr6843.lcmf import (  # pylint: disable=import-outside-toplevel
            SINGLE_CHANNEL_CONFIDENCE_FACTOR,
        )

        score *= SINGLE_CHANNEL_CONFIDENCE_FACTOR
    span = ANGLE_CONFIDENCE_CEILING - ANGLE_CONFIDENCE_FLOOR
    return round(ANGLE_CONFIDENCE_FLOOR + span * score, 3)


def horizontal_confidence_from(coherence: float | None) -> float:
    """Horizontal launch confidence from HLCMF-v0 coherence."""
    if coherence is None:
        return 0.0
    return round(min(ANGLE_CONFIDENCE_CEILING, max(0.0, float(coherence))), 3)


def _snapshot_inclinometer_for_shot(shot: Shot) -> None:
    """Attach the stable pre-impact enclosure orientation used by this shot."""
    if inclinometer_service is None or shot.mode == "mock":
        return

    impact_timestamp = shot.impact_timestamp or time.time()
    selection = inclinometer_service.snapshot_for_impact(impact_timestamp)
    data = selection.to_dict()
    data["zero_offset_deg"] = inclinometer_runtime_config.get("zero_offset_deg", 0.0)
    snapshot = selection.snapshot
    if snapshot is not None and iwr6843_runtime is not None:
        configured_tilt = math.degrees(iwr6843_runtime.calibration.tilt_rad)
        effective_tilt = configured_tilt + snapshot.calibrated_pitch_deg
        data.update(
            {
                "applied": True,
                "configured_iwr_tilt_deg": round(configured_tilt, 3),
                "effective_iwr_tilt_deg": round(effective_tilt, 3),
            }
        )
        logger.info(
            "[SERVER] Inclinometer pitch: raw %+.2fdeg, calibrated %+.2fdeg, "
            "IWR tilt %.2fdeg (age %.0fms)",
            snapshot.raw_pitch_deg,
            snapshot.calibrated_pitch_deg,
            effective_tilt,
            (selection.age_s or 0.0) * 1000.0,
        )
    else:
        data["applied"] = False
        logger.warning("[SERVER] Inclinometer correction not applied: %s", selection.status)
    shot.inclinometer = data


def _ti_launch_guard_log_payload(
    angle_deg: Optional[float], plausible: bool, details: dict
) -> dict:
    """Build the session-log record of the TI vertical launch guard verdict."""
    return {
        "angle_deg": angle_deg,
        "plausible": plausible,
        "reason": None if plausible else TI_LAUNCH_IMPLAUSIBLE_REASON,
        **details,
    }


def _apply_iwr6843_accepted_measurement(
    shot: Shot,
    measurement,
    vertical_guard: tuple[bool, dict] | None = None,
) -> None:
    """Apply an LCMF-v1 result that passed the TI's own quality gates.

    The vertical angle must also pass the club/speed launch guardrail. An
    implausible one is treated like a missing TI angle: the vertical stays
    unset (the launch estimator fills it later, provenance "estimated") and the
    reason is kept on the shot. The TX2 horizontal proxy has its own coherence
    gate and is applied either way. ``vertical_guard`` is the guard verdict
    already computed (and session-logged) by the caller.
    """
    if vertical_guard is None:
        vertical_guard = _ti_vertical_launch_is_plausible(measurement.angle_deg, shot)
    vertical_plausible, guard_details = vertical_guard
    if vertical_plausible:
        shot.launch_angle_vertical = measurement.angle_deg
        # Kept uncorrected: the camera reference may replace or correct the shown value.
        shot.iwr6843_vertical_deg = measurement.angle_deg
        # Device-level provenance is retained in iwr6843_capture. The
        # public Shot contract uses "radar" for all measured radar angles.
        shot.launch_angle_vertical_source = "radar"
        shot.launch_angle_vertical_confidence = vertical_confidence(measurement)
        shot.launch_angle_confidence = shot.launch_angle_vertical_confidence
        shot.angle_source = "radar"
    else:
        shot.launch_angle_vertical_rejection_reason = TI_LAUNCH_IMPLAUSIBLE_REASON
    horizontal_deg = getattr(measurement, "horizontal_deg", None)
    horizontal_confidence = getattr(measurement, "horizontal_confidence", None)
    horizontal_status = getattr(measurement, "horizontal_status", None)
    if horizontal_deg is not None:
        shot.iwr6843_horizontal_deg = horizontal_deg
        shot.iwr6843_horizontal_confidence = horizontal_confidence_from(horizontal_confidence)
        shot.launch_angle_horizontal = horizontal_deg
        shot.launch_angle_horizontal_confidence = shot.iwr6843_horizontal_confidence
        shot.launch_angle_horizontal_source = "radar"
        logger.info(
            "[SERVER] IWR6843 TX2 horizontal proxy: %.2f° (coherence %.0f%%, status=%s)",
            horizontal_deg,
            (horizontal_confidence or 0.0) * 100,
            horizontal_status,
        )
    if not vertical_plausible:
        logger.warning(
            "[SERVER] IWR6843 vertical angle %.2f° rejected: %s, %s "
            "(expected=%s°, allowed=%s°, delta=%s°)",
            measurement.angle_deg,
            TI_LAUNCH_IMPLAUSIBLE_REASON,
            _TI_GUARD_RULE_TEXT.get(guard_details.get("rule"), guard_details.get("rule")),
            guard_details.get("expected_launch_deg"),
            guard_details.get("allowed_delta_deg"),
            guard_details.get("delta_deg"),
        )
        _emit_iwr6843_trigger_status(
            shot,
            state="rejected",
            reason=TI_LAUNCH_IMPLAUSIBLE_REASON,
            angle_deg=measurement.angle_deg,
        )
        return
    logger.info(
        "[SERVER] IWR6843 LCMF-v1 launch: %.2f° (%d snapshots/%d frames, component std %.2f°)",
        measurement.angle_deg,
        measurement.n_snapshots,
        measurement.n_frames,
        measurement.component_std_deg,
    )
    _emit_iwr6843_trigger_status(
        shot,
        state="accepted",
        reason="accepted",
        angle_deg=measurement.angle_deg,
    )


def _record_effective_iwr_tilt(shot_tilt_deg: float | None) -> None:
    """Remember the tilt a TI shot uses (configured when no inclinometer value applied).

    Status bookkeeping only: it never raises into the shot path.
    """
    global _last_effective_iwr_tilt  # pylint: disable=global-statement
    if shot_tilt_deg is not None:
        tilt_deg = float(shot_tilt_deg)
    else:
        calibration = getattr(iwr6843_runtime, "calibration", None)
        if calibration is None:
            return
        tilt_deg = math.degrees(calibration.tilt_rad)
    _last_effective_iwr_tilt = {
        "tilt_deg": round(tilt_deg, 4),
        "at": datetime.now().astimezone().isoformat(),
    }


def _shot_ball_height(shot: Shot, base_m: float | None = None) -> tuple[float, float]:
    """(ball-centre height, tee height) above the floor for this shot's geometry, in m.

    ``base_m`` is the ball centre off the mat (``--iwr6843-ball-height-m``),
    by default the TI calibration's. Only the ``driver`` club id is teed: it
    adds its profile's ``driver_tee_height_m`` (the shot's attributed profile,
    else the active one). Every other club returns ``(base_m, 0.0)`` unchanged.
    """
    if base_m is None:
        calibration = getattr(iwr6843_runtime, "calibration", None)
        # init_iwr6843's own default when there is no calibration to read.
        base_m = float(getattr(calibration, "tee_ball_height_m", 0.04))
    if _club_key(shot) != ClubType.DRIVER.value:
        return base_m, 0.0
    tee_m = driver_tee_height_m(_shot_profile(shot).settings)
    return base_m + tee_m, tee_m


def _shot_profile(shot: Shot) -> Profile:
    """The shot's attributed profile, else the active one (a deleted or unset id)."""
    store = get_profile_store()
    return store.get(getattr(shot, "profile_id", "")) or store.get_active()


def _shot_typical_spin_table(shot: Shot) -> str:
    """The club-typical spin table of this shot's carry: its profile's ball type.

    A profile without a ball type flies the launcher's --typical-spin-table itself.
    """
    stored = clean_ball_type(_shot_profile(shot).settings.get("ball_type"))
    return typical_spin_table if stored is None else BALL_TYPE_SPIN_TABLES[stored]


def _process_iwr6843_angle(shot: Shot) -> float | None:
    """Apply a correlated LCMF-v1 result without risking the OPS shot."""
    if iwr6843_runtime is None or shot.mode == "mock":
        return None

    started = time.perf_counter()
    try:
        shot_tilt_deg = (
            shot.inclinometer.get("effective_iwr_tilt_deg")
            if shot.inclinometer and shot.inclinometer.get("applied")
            else None
        )
        _record_effective_iwr_tilt(shot_tilt_deg)
        ball_height_m, tee_height_m = _shot_ball_height(shot)
        shot_result = iwr6843_runtime.process_shot(
            impact_timestamp=shot.impact_timestamp,
            ball_speed_mph=shot.ball_speed_mph,
            club=shot.club.value,
            club_speed_mph=shot.club_speed_mph,
            tilt_deg=shot_tilt_deg,
            # A teed driver only; every other club keeps the calibration's own height.
            ball_height_m=ball_height_m if tee_height_m else None,
        )
        capture = shot_result.capture
        measurement = shot_result.measurement
        club_path = getattr(shot_result, "club_path", None)
        vertical_guard = None
        if measurement is not None:
            shot.iwr6843_ball_range_evidence = getattr(measurement, "range_evidence", None)
            if measurement.accepted:
                vertical_guard = _ti_vertical_launch_is_plausible(measurement.angle_deg, shot)
        session_log = get_session_logger()
        if session_log:
            session_log.log_iwr6843_capture(
                shot_number=_shot_number_for_log(shot, session_log),
                shot_timestamp=shot.impact_timestamp,
                trigger_timestamp=(capture.trigger_timestamp if capture is not None else None),
                capture_path=(str(capture.path) if capture and capture.path else None),
                capture_bytes=(len(capture.raw) if capture and capture.raw else 0),
                edge_fields=sound_edge_timing.fields_for(
                    capture.trigger_timestamp if capture is not None else None
                ),
                dump_duration_s=(capture.dump_duration_s if capture is not None else None),
                capture_error=(
                    capture.error
                    if capture is not None
                    else "no capture matched the OPS impact timestamp"
                ),
                ball_speed_mph=shot.ball_speed_mph,
                ball_height_m=ball_height_m,
                tee_height_m=tee_height_m,
                measurement=(measurement.to_dict() if measurement is not None else None),
                launch_guard=(
                    _ti_launch_guard_log_payload(measurement.angle_deg, *vertical_guard)
                    if vertical_guard is not None
                    else None
                ),
                club_path=(club_path.to_dict() if club_path is not None else None),
                temperature_report=(
                    getattr(capture, "temperature_report", None) if capture is not None else None
                ),
                match_context={
                    key: getattr(capture, key, None)
                    for key in (
                        "nearest_trigger_delta_ms",
                        "ti_busy_at_impact",
                        "ti_ready_after_impact_ms",
                        # ops-speed: the edge-to-l3dump delay (None, so left out, otherwise).
                        "l3dump_target_ms",
                        "l3dump_actual_ms",
                    )
                },
            )

        if capture is None:
            logger.warning("[SERVER] IWR6843 capture timed out; preserving OPS shot")
            _emit_iwr6843_trigger_status(
                shot,
                state="error",
                reason="no capture matched the OPS impact timestamp",
            )
        elif not capture.valid:
            logger.warning(
                "[SERVER] IWR6843 capture #%d failed: %s; preserving OPS shot",
                capture.sequence,
                capture.error,
            )
            _emit_iwr6843_trigger_status(
                shot,
                state="error",
                reason=capture.error or "invalid IWR6843 capture",
            )
        elif measurement is None:
            logger.warning("[SERVER] IWR6843 capture had no LCMF measurement")
            _emit_iwr6843_trigger_status(
                shot,
                state="rejected",
                reason="no LCMF measurement",
            )
        elif measurement.accepted:
            _apply_iwr6843_accepted_measurement(shot, measurement, vertical_guard)
        else:
            logger.warning(
                "[SERVER] IWR6843 LCMF-v1 withheld angle: %s",
                measurement.status,
            )
            _emit_iwr6843_trigger_status(
                shot,
                state="rejected",
                reason=measurement.status,
            )

        # IWR club path/AoA remain experimental even when their internal
        # quality gates accept them. Publish them through the normal UI path,
        # but never populate the canonical club fields or silently label them
        # as production radar measurements.
        if club_path is not None:
            shot.iwr6843_club_range_evidence = getattr(club_path, "range_evidence", None)
            accepted_path = club_path.path_deg if club_path.accepted else None
            candidate_path = (
                accepted_path
                if accepted_path is not None
                else getattr(club_path, "candidate_path_deg", None)
            )
            candidate_attack = getattr(club_path, "candidate_attack_angle_deg", None)
            candidate_path_status = getattr(club_path, "candidate_path_status", None)
            shot.experimental_club_path_status = (
                club_path.status
                if accepted_path is not None
                else (
                    candidate_path_status
                    if candidate_path_status not in (None, "candidate_available")
                    else club_path.status
                )
            )
            shot.experimental_attack_angle_status = (
                getattr(club_path, "attack_angle_status", None) or club_path.status
            )
            if candidate_path is not None:
                shot.experimental_club_path_deg = round(candidate_path, 1)
            if candidate_attack is not None:
                shot.experimental_attack_angle_deg = round(candidate_attack, 1)
            if accepted_path is not None:
                logger.info(
                    "[SERVER] Experimental IWR6843 club path: %.2f° (confidence %.2f, %d frames)",
                    accepted_path,
                    club_path.confidence or 0.0,
                    club_path.n_frames,
                )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] IWR6843 processing error: %s", error, exc_info=True)
        log_session_error(
            "IWR6843 shot processing failed",
            component="server",
            context={
                "stage": "iwr6843",
                "ball_speed_mph": shot.ball_speed_mph,
                "club": shot.club.value,
            },
            exc=error,
        )
        _emit_iwr6843_trigger_status(shot, state="error", reason=str(error))
    return (time.perf_counter() - started) * 1000.0


def _emit_iwr6843_trigger_status(
    shot: Shot,
    *,
    state: str,
    reason: str,
    angle_deg: float | None = None,
) -> None:
    """Enrich the existing OPS trigger row with the correlated TI result."""
    iwr_status = {"state": state, "reason": reason}
    if angle_deg is not None:
        iwr_status["angle_deg"] = round(angle_deg, 2)
    _emit_real_shot_event(
        "trigger_diagnostic_update",
        {
            "timestamp": shot.timestamp.isoformat(),
            "iwr6843": iwr_status,
        },
    )


_CAMERA_ARCHIVE_UNSET = object()
_CAMERA_SENSOR_MAX_LAG_NS = 50_000_000


def _load_camera_capture_archive(camera_capture) -> dict[str, object] | None:
    """Load one camera clip into memory for all per-shot estimators."""
    if camera_capture is None or not camera_capture.valid or not camera_capture.path:
        return None
    frames_path = Path(camera_capture.path) / "frames.npz"
    if not frames_path.exists():
        return None

    import numpy as np  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    try:
        with np.load(frames_path) as archive:
            return {name: archive[name] for name in archive.files}
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera capture archive could not be loaded: %s", error)
        return None


def _capture_horizontal_pixel_sign(camera_capture) -> float:
    """Image handedness of the saved frames, not of the current live settings."""
    metadata = getattr(camera_capture, "metadata", None)
    settings = metadata.get("settings") if isinstance(metadata, dict) else None
    mirrored = (
        settings["mirror_horizontal"]
        if isinstance(settings, dict) and isinstance(settings.get("mirror_horizontal"), bool)
        else camera_capture_config.get("mirror_horizontal")
    )
    return -1.0 if mirrored else 1.0


def _camera_frame_timestamps(archive):
    """Sensor exposure times when they share the trigger's monotonic clock.

    Host receive times lag exposure by ~4.6 ms with sub-ms jitter; older or
    foreign-clock captures keep the host times.
    """
    import numpy as np  # noqa: PLC0415  pylint: disable=import-outside-toplevel

    host = np.asarray(archive["host_timestamp_ns"], dtype=np.int64)
    if "sensor_timestamp_ns" not in archive:
        return host
    sensor = np.asarray(archive["sensor_timestamp_ns"], dtype=np.int64)
    if sensor.shape != host.shape or np.any(np.diff(sensor) <= 0):
        return host
    if not 0 <= np.median(host - sensor) <= _CAMERA_SENSOR_MAX_LAG_NS:
        return host
    return sensor


def _camera_time_zero_diagnostics(estimate, shot: Shot) -> dict | None:
    """Camera time-zero candidates plus the OPS impact, all in ms from the trigger."""
    if estimate.time_zero is None:
        return None
    ops_impact_ms = None
    trigger_s, impact_s = shot.impact_timestamp, shot.impact_timestamp_kld7
    # Equal stamps mean the OPS found no club-to-ball transition and used its trigger.
    if trigger_s is not None and impact_s is not None and impact_s != trigger_s:
        ops_impact_ms = 1e3 * (impact_s - trigger_s)
    return {**asdict(estimate.time_zero), "ops_impact_ms": ops_impact_ms}


def _capture_intrinsics(camera_capture):
    """Calibrated lens model carried into the clip's capture mode, None when it has none.

    The clip's ``metadata.settings`` name its mode; a clip saved before they
    held the sensor window was at window 0. Without settings the live mode applies.
    """
    if camera_intrinsics is None:
        return None
    metadata = getattr(camera_capture, "metadata", None)
    settings = metadata.get("settings") if isinstance(metadata, dict) else None
    if isinstance(settings, dict):
        vertical_offset_px = settings.get("vertical_offset_px", 0)
    else:
        settings, vertical_offset_px = camera_capture_config, _camera_vertical_offset_px()
    intrinsics, mismatch = _mode_intrinsics(settings, vertical_offset_px)
    if mismatch is not None:
        logger.warning("[SERVER] Camera intrinsics not applied, mode differs (%s)", mismatch)
        return None
    return intrinsics


# Status of a camera estimate whose shot was past its enrichment deadline before
# the job could run; the job did not run.
_CAMERA_CV_PAST_DEADLINE = "rejected_shot_past_deadline"


def _run_shot_camera_cv(job: str, kwargs: dict, *, deadline_monotonic, past_deadline):
    """One of a shot's camera jobs (``run_camera_cv``); ``past_deadline`` if it did not run."""
    from openflight.camera.cv_worker import (  # noqa: PLC0415
        CameraCvDeadlinePassed,
        run_camera_cv,
    )

    try:
        return run_camera_cv(job, kwargs, camera_cv_worker, deadline_monotonic=deadline_monotonic)
    except CameraCvDeadlinePassed:
        return past_deadline


def _fuse_camera_club_delivery(
    shot: Shot,
    camera_capture,
    camera_archive=_CAMERA_ARCHIVE_UNSET,
    *,
    deadline_monotonic: float | None = None,
) -> None:
    """Impact-centered camera + IWR depth club delivery, experimentally.

    ``deadline_monotonic`` is the shot's enrichment deadline: past it the worker
    is not asked (``rejected_shot_past_deadline``); see ``run_camera_cv``.
    """
    try:
        from openflight.camera.club_delivery import (  # noqa: PLC0415
            CameraDeliveryGeometry,
            ChainedDelivery,
        )

        fused = ChainedDelivery(status="rejected_no_camera_capture")
        if camera_capture is not None and camera_capture.valid and camera_capture.path:
            frames_path = Path(camera_capture.path) / "frames.npz"
            if frames_path.exists():
                archive = (
                    _load_camera_capture_archive(camera_capture)
                    if camera_archive is _CAMERA_ARCHIVE_UNSET
                    else camera_archive
                )
                if archive is None:
                    fused = ChainedDelivery(status="rejected_missing_camera_frames")
                else:
                    trigger_index = (
                        int(archive["pre_trigger_count"]) - 1
                        if "pre_trigger_count" in archive
                        else None
                    )
                    if iwr6843_runtime is None:
                        fused = ChainedDelivery(status="rejected_no_iwr_runtime")
                    else:
                        calibration = iwr6843_runtime.calibration
                        if calibration.tee_range_m is None:
                            fused = ChainedDelivery(status="rejected_missing_tee_geometry")
                        else:
                            ball_height_m, tee_height_m = _shot_ball_height(
                                shot, calibration.tee_ball_height_m
                            )
                            geometry = CameraDeliveryGeometry(
                                camera_height_m=float(camera_capture_config["mount_height_m"]),
                                radar_height_m=calibration.radar_height_m,
                                tee_range_m=float(calibration.tee_range_m),
                                ball_height_m=ball_height_m,
                                tee_height_m=tee_height_m,
                                camera_lateral_offset_m=float(
                                    camera_capture_config.get("lateral_offset_m", 0.0)
                                ),
                                camera_forward_offset_m=float(
                                    camera_capture_config.get("forward_offset_m", 0.0)
                                ),
                                image_width_px=int(camera_capture_config["width"]),
                                image_height_px=int(camera_capture_config["height"]),
                                horizontal_pixel_sign=_capture_horizontal_pixel_sign(
                                    camera_capture
                                ),
                                roll_correction_deg=float(
                                    camera_capture_config.get("roll_correction_deg", 0.0)
                                ),
                                intrinsics=_capture_intrinsics(camera_capture),
                            )
                            fused = _run_shot_camera_cv(
                                "club_delivery",
                                {
                                    "frames": archive["frames"],
                                    "host_timestamp_ns": _camera_frame_timestamps(archive),
                                    "trigger_index": trigger_index,
                                    "range_evidence": shot.iwr6843_club_range_evidence,
                                    "geometry": geometry,
                                    "ops_club_speed_mph": shot.club_speed_mph,
                                    "ball_tracker": camera_reference_ball_tracker,
                                },
                                deadline_monotonic=deadline_monotonic,
                                past_deadline=ChainedDelivery(status=_CAMERA_CV_PAST_DEADLINE),
                            )
            else:
                fused = ChainedDelivery(status="rejected_missing_camera_frames")
        shot.experimental_fused_attack_angle_deg = fused.attack_angle_deg
        shot.experimental_fused_club_path_deg = fused.club_path_deg
        shot.experimental_fused_status = fused.status
        shot.experimental_fused_attack_angle_confidence = fused.attack_confidence_tier
        shot.experimental_fused_club_path_confidence = fused.path_confidence_tier
        shot.experimental_camera_trace_deg = None
        shot.experimental_aoa_offset_source = "none_chained_3d"
        logger.info(
            "[SERVER] Camera/IWR chained club delivery: AoA %s path %s "
            "(status=%s, features=%d, speed_ratio=%s, velocity_mad=%s mph, "
            "path_windows=%d, path_mad=%s deg, impact_frame=%s)",
            fused.attack_angle_deg,
            fused.club_path_deg,
            fused.status,
            fused.n_features,
            fused.speed_ratio_ops,
            fused.velocity_mad_mph,
            fused.path_window_count,
            fused.path_window_mad_deg,
            fused.impact_frame,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        shot.experimental_fused_status = "error"
        logger.warning("[SERVER] Camera club-delivery fusion error: %s", error, exc_info=True)
        log_session_error(
            "Camera club-delivery fusion failed",
            component="camera_capture",
            context={"stage": "club_delivery_fusion", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )


def _trigger_stamp_lag(trigger_ns: int, edge_fields: dict) -> tuple[float | None, str | None]:
    """How long the clip's trigger stamp followed its sound edge, and which edge stamp says so.

    The camera stamps the trigger with ``time.monotonic_ns()`` after it took its
    exposure lock, behind the edge callback and any observer before it. The edge
    log (``EdgeTimingLog.fields_for``) has the callback's own monotonic stamp,
    read before any observer ran, and, once the kernel clock is decided, the
    callback latency: the kernel line-event time is the callback stamp minus it.
    Returns ``(lag_ms, "kernel" | "callback")``, or ``(None, None)`` when the edge
    is unknown or its stamp follows the trigger stamp (a mismatched edge).
    """
    callback_ns = edge_fields.get("edge_callback_monotonic_ns")
    if callback_ns is None:
        return None, None
    latency_ms = edge_fields.get("edge_callback_latency_ms")
    edge_ns = callback_ns - (0.0 if latency_ms is None else latency_ms * 1e6)
    lag_ms = (trigger_ns - edge_ns) / 1e6
    if not math.isfinite(lag_ms) or lag_ms < 0:
        return None, None
    return lag_ms, "callback" if latency_ms is None else "kernel"


def _club_data_calibrator_instance():
    """The session's acoustic-delay calibrator (``camera.club_head_path``), created on first use."""
    global _club_data_calibrator  # pylint: disable=global-statement
    from openflight.camera.club_head_path import (  # noqa: PLC0415
        AcousticDelayCalibrator,
    )

    with _club_data_state_lock:
        if _club_data_calibrator is None:
            _club_data_calibrator = AcousticDelayCalibrator()
        return _club_data_calibrator


def _reset_club_data_state(reason: str) -> None:
    """Forget the session's acoustic-delay calibration (with its tee control) and head lengths."""
    global _club_data_calibrator  # pylint: disable=global-statement
    with _club_data_state_lock:
        had_state = (
            _club_data_calibrator is not None
            or bool(_club_head_length_mm)
            or bool(_impact_point_head_length_mm)
        )
        _club_data_calibrator = None
        _club_head_length_mm.clear()
        _impact_point_head_length_mm.clear()
    if had_state:
        logger.info("[CLUB-DATA] Session state reset (%s)", reason)


def _club_key(shot: Shot) -> str:
    """The club a shot's head length belongs to: the bag selection, else the club type."""
    return shot.club_selection or shot.club.value


def _club_head_length_median_mm(
    profile_id: str, club_key: str, lengths_by_club: dict | None = None
) -> float | None:
    """Median plausible head length of this profile's club, once there are 5.

    ``lengths_by_club`` is E's (default) or the impact point's session state.
    """
    with _club_data_state_lock:
        source = _club_head_length_mm if lengths_by_club is None else lengths_by_club
        lengths = list(source.get((profile_id, club_key), ()))
    if len(lengths) < _CLUB_HEAD_LENGTH_MIN_SAMPLES:
        return None
    return float(statistics.median(lengths))


def _note_sim_handedness(handed: str | None) -> None:
    """Remember the handedness a simulator reported (GSPro ``Handed``: RH or LH)."""
    global _sim_reported_handed  # pylint: disable=global-statement
    if handed is not None:
        _sim_reported_handed = str(handed).strip().upper()


def _golfer_handedness() -> str:
    """``right`` or ``left``: the simulator's report when it sent one, else the flag."""
    reported = _sim_reported_handed
    if reported == "LH":
        return "left"
    if reported == "RH":
        return "right"
    return golfer_handedness


def _accepted_camera_vertical_deg(estimate) -> float | None:
    """The camera launch angle of an accepted camera ball-flight estimate, else None."""
    if estimate is None or not str(getattr(estimate, "status", "")).startswith("accepted"):
        return None
    vertical = getattr(estimate, "vertical_deg", None)
    return float(vertical) if vertical is not None and math.isfinite(vertical) else None


def _departed_reference_ball(estimate):
    """The ball the disappeared-ball locator found in this clip (a ReferenceBall), else None."""
    from openflight.camera.club_motion import ReferenceBall  # noqa: PLC0415

    locator = getattr(estimate, "locator", None)
    if (
        locator is None
        or locator.locator != "disappeared"
        or locator.ball_x is None
        or locator.ball_y is None
        or not locator.ball_diameter_px
    ):
        return None
    diameter = float(locator.ball_diameter_px)
    return ReferenceBall(
        float(locator.ball_x),
        float(locator.ball_y),
        diameter,
        int(round(math.pi * (diameter / 2.0) ** 2)),
    )


def _departed_ball_teed(estimate) -> bool | None:
    """Whether the disappeared-ball locator saw a tee under this clip's ball, else None."""
    locator = getattr(estimate, "locator", None)
    if locator is None or locator.locator != "disappeared" or locator.teed is None:
        return None
    return bool(locator.teed)


def _confirmed_tee_reference():
    """The session's confirmed tee anchor as a plain ReferenceBall, else None."""
    from openflight.camera.club_motion import ReferenceBall  # noqa: PLC0415

    gate_anchor = getattr(camera_capture_runtime, "trigger_check_anchor", None)
    anchor = gate_anchor() if gate_anchor is not None else None
    if anchor is None:
        return None
    return ReferenceBall(
        float(anchor.x), float(anchor.y), float(anchor.diameter_px), int(anchor.area_px)
    )


def _optional_club_data_input(name: str, compute):
    """One optional club-data input, None when it cannot be read; never raises.

    The job is built inside the ball-flight fusion, so a failure here must not
    cost the shot its camera start direction.
    """
    try:
        return compute()
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[CLUB-DATA] %s unavailable: %s", name, error)
        return None


def _club_head_path_shadow_job(  # pylint: disable=too-many-arguments
    shot: Shot,
    archive: dict,
    *,
    geometry,
    ball_tracker,
    time_zero,
    edge_fields: dict,
    estimate=None,
    ball_mark_px: tuple[float, float] | None = None,
    tee_zone=None,
) -> _ClubHeadPathShadowJob:
    """The club-data job's inputs: this clip, geometry, live time zero and the ball flight.

    ``edge_fields`` are the clip's sound-edge stamps; they move the sound bound
    from the trigger stamp to the edge (``select_contact_time``). ``estimate`` is
    this clip's camera ball-flight estimate (camera launch angle, departed ball).
    ``ball_mark_px`` and ``tee_zone`` are the tapped ball mark and the hitting
    zone the ball flight ran with: without a departed ball the mark is the
    club ball's anchor (2026-11-02 S1: the tracker put irons' balls on the tree
    line).
    """
    from openflight.camera.club_head_path import (  # noqa: PLC0415
        mark_reference_ball,
        select_contact_time,
    )

    trigger_ns = int(archive["trigger_host_timestamp_ns"])
    trigger_lag_ms, edge_reference = _trigger_stamp_lag(trigger_ns, edge_fields)
    teed = shot.club is ClubType.DRIVER
    club_key = _club_key(shot)
    departed_ball = _optional_club_data_input(
        "departed ball", lambda: _departed_reference_ball(estimate)
    )
    return _ClubHeadPathShadowJob(
        shot_number=shot.shot_number,
        shot_timestamp=shot.impact_timestamp,
        contact=select_contact_time(
            time_zero,
            trigger_ns=trigger_ns,
            tee_range_m=geometry.tee_range_m,
            trigger_lag_ms=trigger_lag_ms,
            lag_reference=edge_reference,
            teed=teed,
            acoustic_delay_ms=_club_data_calibrator_instance().session_delay_ms(),
        ),
        kwargs={
            "frames": archive["frames"],
            "timestamps_ns": _camera_frame_timestamps(archive),
            "geometry": geometry,
            "club_speed_mph": shot.club_speed_mph,
            "ball_tracker": ball_tracker,
        },
        edge_reference=edge_reference,
        club=shot.club,
        club_key=club_key,
        profile_id=shot.profile_id,
        teed=teed,
        time_zero=time_zero,
        trigger_ns=trigger_ns,
        tee_range_m=float(geometry.tee_range_m),
        trigger_lag_ms=trigger_lag_ms,
        camera_vertical_deg=_optional_club_data_input(
            "camera launch angle", lambda: _accepted_camera_vertical_deg(estimate)
        ),
        handedness=_golfer_handedness(),
        head_length_median_mm=_club_head_length_median_mm(shot.profile_id, club_key),
        departed_ball=departed_ball,
        confirmed_tee=_optional_club_data_input("confirmed tee", _confirmed_tee_reference),
        reference_ball=(
            None
            if departed_ball is not None
            else _optional_club_data_input(
                "ball mark", lambda: mark_reference_ball(ball_mark_px, geometry)
            )
        ),
        tee_zone=tee_zone,
        ball_speed_mph=shot.ball_speed_mph,
        ball_teed=_optional_club_data_input("ball tee", lambda: _departed_ball_teed(estimate)),
        impact_point_head_length_median_mm=_club_head_length_median_mm(
            shot.profile_id, club_key, _impact_point_head_length_mm
        ),
    )


def _club_head_path_shadow_skip(shot: Shot, reason: str) -> _ClubHeadPathShadowJob:
    """An eligible clip the club-data job cannot run on: its log entries, no job."""
    return _ClubHeadPathShadowJob(
        shot_number=shot.shot_number,
        shot_timestamp=shot.impact_timestamp,
        contact=None,
        skip_reason=reason,
    )


def _camera_target_px(shot: Shot) -> tuple[float, float] | None:
    """The tapped target in saved-frame pixels for the ball-flight geometry, if one is set.

    Stamps the shot with that target (``angle_reference["measured_against"]``,
    None without one), read once with the pixel: a tap while the ball flight
    runs must not relabel this shot or pair it into the new target's offset.
    """
    target = angle_reference.target_line
    shot.angle_reference = {"measured_against": target.to_log() if target is not None else None}
    return target.frame_px if target is not None else None


def _note_camera_vertical(shot: Shot, estimate) -> None:
    """Keep an accepted camera vertical and the target azimuth of this shot's pose.

    Only an accepted estimate counts: a direction the line fit withheld still
    carries the consensus vertical, which is not a measurement then.
    """
    if estimate.status.startswith("accepted") and estimate.vertical_deg is not None:
        shot.experimental_camera_vertical_deg = estimate.vertical_deg
        shot.experimental_camera_vertical_confidence = camera_vertical_confidence(
            estimate.confidence_tier, estimate.depth_source
        )
    if estimate.target_azimuth_deg is not None:
        shot.angle_reference = {
            **(shot.angle_reference or {}),
            "target_line": {"shot_azimuth_deg": round(estimate.target_azimuth_deg, 3)},
        }


def _camera_horizontal_fields(estimate, decision) -> tuple[float | None, float | None, str]:
    """(camera start direction, its confidence, its status) of one ball-flight decision."""
    return (
        decision.camera_horizontal_deg,
        (
            decision.confidence
            if decision.source in ("camera_assisted_experimental", "camera_only_experimental")
            else None
        ),
        (
            decision.status
            if estimate.confidence_tier != "withheld"
            else f"{decision.status}:{estimate.status}"
        ),
    )


def _camera_ball_geometry(camera_capture, calibration, *, target_px, shot: Shot):
    """The ball flight's ``CameraBallGeometry`` of one clip under the session config.

    The ball height is the shot's own (a teed driver sits higher, see ``_shot_ball_height``).
    """
    from openflight.camera.ball_flight import CameraBallGeometry  # noqa: PLC0415

    ball_height_m, tee_height_m = _shot_ball_height(shot, calibration.tee_ball_height_m)
    return CameraBallGeometry(
        camera_height_m=float(camera_capture_config["mount_height_m"]),
        radar_height_m=calibration.radar_height_m,
        tee_range_m=float(calibration.tee_range_m),
        ball_height_m=ball_height_m,
        tee_height_m=tee_height_m,
        camera_lateral_offset_m=float(camera_capture_config.get("lateral_offset_m", 0.0)),
        camera_forward_offset_m=float(camera_capture_config.get("forward_offset_m", 0.0)),
        yaw_from_reference_ball=bool(camera_capture_config.get("yaw_from_ball", False)),
        horizontal_offset_deg=float(camera_capture_config.get("horizontal_offset_deg", 0.0)),
        roll_correction_deg=float(camera_capture_config.get("roll_correction_deg", 0.0)),
        horizontal_pixel_sign=_capture_horizontal_pixel_sign(camera_capture),
        image_width_px=int(camera_capture_config["width"]),
        image_height_px=int(camera_capture_config["height"]),
        intrinsics=_capture_intrinsics(camera_capture),
        target_px=target_px,
    )


def _fuse_camera_ball_flight(
    shot: Shot,
    camera_capture,
    camera_archive=_CAMERA_ARCHIVE_UNSET,
    *,
    deadline_monotonic: float | None = None,
    relaxed_only: bool = False,
) -> _ClubHeadPathShadowJob | None:
    """Select experimental camera horizontal while preserving IWR fallback.

    Returns the camera club-data job for this clip when the ball-flight
    estimator ran; for a valid clip it could not run on (no frames, no IWR
    runtime or tee geometry, a ball flight that raised or timed out, a shot
    already past ``deadline_monotonic``), a skip marker naming why; None
    without a valid clip. ``relaxed_only`` (the clip passed the ball gate only
    by its relaxed dark rule, ``_camera_ball_relaxed_only``) runs the
    disappeared-ball locator without the legacy fallback, as the low-light
    shadow does: the fallback's fixed thresholds are not for a dark clip.
    The worker job compensates mains flicker first
    (``ball_flight.estimate_live_ball_flight``); the archive's frames stay as saved.
    """
    shadow_job = None
    # Set once the clip is loaded: a failure from there on still logs a skip.
    clip_loaded = False
    try:
        from openflight.camera.ball_flight import (  # noqa: PLC0415
            DEFAULT_BALL_LOCATOR,
            CameraBallEstimate,
            select_camera_assisted_horizontal,
        )

        estimate = CameraBallEstimate(status="rejected_no_camera_capture")
        if camera_capture is not None and camera_capture.valid and camera_capture.path:
            frames_path = Path(camera_capture.path) / "frames.npz"
            if not frames_path.exists():
                estimate = CameraBallEstimate(status="rejected_missing_camera_frames")
                shadow_job = _club_head_path_shadow_skip(shot, "skipped_missing_frames")
            elif iwr6843_runtime is None:
                estimate = CameraBallEstimate(status="rejected_no_iwr_runtime")
                shadow_job = _club_head_path_shadow_skip(shot, "skipped_no_iwr_runtime")
            else:
                calibration = iwr6843_runtime.calibration
                if calibration.tee_range_m is None:
                    estimate = CameraBallEstimate(status="rejected_missing_tee_geometry")
                    shadow_job = _club_head_path_shadow_skip(shot, "skipped_no_tee_geometry")
                else:
                    archive = (
                        _load_camera_capture_archive(camera_capture)
                        if camera_archive is _CAMERA_ARCHIVE_UNSET
                        else camera_archive
                    )
                    if archive is None:
                        estimate = CameraBallEstimate(status="rejected_missing_camera_frames")
                        shadow_job = _club_head_path_shadow_skip(shot, "skipped_missing_frames")
                    else:
                        clip_loaded = True
                        trigger_ns = int(archive["trigger_host_timestamp_ns"])
                        geometry = _camera_ball_geometry(
                            camera_capture,
                            calibration,
                            target_px=_camera_target_px(shot),
                            shot=shot,
                        )
                        # The club-data job resolves the teed ball from the
                        # tracker as this shot's ball flight found it, so both read
                        # one camera pose and yaw frame and the live tracker sees
                        # each shot once.
                        tracker_before_shot = copy.deepcopy(camera_ball_flight_reference_tracker)
                        # Read before the ball flight, while the edge log still holds it.
                        edge_fields = sound_edge_timing.fields_for(
                            getattr(camera_capture, "trigger_timestamp", None)
                        )
                        # Read once: the club-data job bounds its ball by the same
                        # mark and zone the ball flight searched.
                        ball_mark_px = _camera_ball_mark_px()
                        tee_zone = (
                            camera_capture_runtime.active_tee_zone()
                            if camera_capture_runtime is not None
                            else None
                        )
                        estimate = _run_shot_camera_cv(
                            "ball_flight",
                            {
                                "frames": archive["frames"],
                                "timestamps_ns": _camera_frame_timestamps(archive),
                                "trigger_ns": trigger_ns,
                                "range_evidence": shot.iwr6843_ball_range_evidence,
                                "geometry": geometry,
                                "ops_ball_speed_mph": shot.ball_speed_raw_mph
                                or shot.ball_speed_mph,
                                "iwr_vertical_deg": shot.launch_angle_vertical,
                                "ball_tracker": camera_ball_flight_reference_tracker,
                                "ball_locator": (
                                    "disappeared"
                                    if relaxed_only
                                    else camera_capture_config.get(
                                        "ball_locator", DEFAULT_BALL_LOCATOR
                                    )
                                ),
                                "legacy_fallback": not relaxed_only,
                                "ground_anchor": camera_ball_flight_ground_anchor,
                                "tee_zone": tee_zone,
                                "ball_mark_px": ball_mark_px,
                            },
                            deadline_monotonic=deadline_monotonic,
                            past_deadline=CameraBallEstimate(status=_CAMERA_CV_PAST_DEADLINE),
                        )
                        shadow_job = (
                            _club_head_path_shadow_skip(shot, "skipped_shot_past_deadline")
                            if estimate.status == _CAMERA_CV_PAST_DEADLINE
                            else _club_head_path_shadow_job(
                                shot,
                                archive,
                                geometry=geometry,
                                ball_tracker=tracker_before_shot,
                                time_zero=estimate.time_zero,
                                edge_fields=edge_fields,
                                estimate=estimate,
                                ball_mark_px=ball_mark_px,
                                tee_zone=tee_zone,
                            )
                        )

        _note_camera_vertical(shot, estimate)
        decision = select_camera_assisted_horizontal(
            estimate,
            iwr_horizontal_deg=shot.iwr6843_horizontal_deg,
            iwr_confidence=shot.iwr6843_horizontal_confidence,
        )
        (
            shot.experimental_camera_horizontal_deg,
            shot.experimental_camera_horizontal_confidence,
            shot.experimental_camera_horizontal_status,
        ) = _camera_horizontal_fields(estimate, decision)
        shot.experimental_camera_iwr_delta_deg = decision.camera_iwr_delta_deg
        shot.experimental_camera_time_zero = _camera_time_zero_diagnostics(estimate, shot)
        shot.experimental_camera_ball_locator = (
            estimate.locator.to_dict() if estimate.locator is not None else None
        )
        if decision.selected_deg is not None:
            shot.launch_angle_horizontal = decision.selected_deg
            shot.launch_angle_horizontal_confidence = decision.confidence
            shot.launch_angle_horizontal_source = decision.source
        elif decision.status == "camera_withheld_iwr_implausible":
            shot.launch_angle_horizontal = None
            shot.launch_angle_horizontal_confidence = None
            shot.launch_angle_horizontal_source = None
        locator = estimate.locator
        logger.info(
            "[SERVER] Camera-assisted horizontal: selected=%s camera=%s IWR=%s "
            "delta=%s status=%s support=%d/27 locator=%s (%s) %s ms",
            decision.selected_deg,
            decision.camera_horizontal_deg,
            decision.iwr_horizontal_deg,
            decision.camera_iwr_delta_deg,
            decision.status,
            estimate.support,
            None if locator is None else locator.locator,
            None if locator is None else locator.reason,
            (None if locator is None or locator.elapsed_ms is None else round(locator.elapsed_ms)),
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        if shadow_job is None and clip_loaded:
            from openflight.camera.cv_worker import CameraCvWorkerTimeout  # noqa: PLC0415

            shadow_job = _club_head_path_shadow_skip(
                shot,
                "skipped_ball_flight_timeout"
                if isinstance(error, CameraCvWorkerTimeout)
                else "skipped_ball_flight_error",
            )
        shot.experimental_camera_horizontal_status = "error"
        logger.warning("[SERVER] Camera ball-flight fusion error: %s", error, exc_info=True)
        log_session_error(
            "Camera ball-flight fusion failed",
            component="camera_capture",
            context={"stage": "ball_flight_fusion", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )
    return shadow_job


def _camera_analysis_eligibility(camera_capture) -> tuple[bool, bool, dict | None]:
    """(club analysis, ball analysis eligible, the clip's saved exposure status) of one clip.

    The saved ``capture_exposure_status`` decides; a clip without one falls back
    to the runtime's current verdict (club and ball alike).
    """
    captured_auto_exposure = (
        camera_capture.metadata.get("auto_exposure")
        if camera_capture is not None
        and isinstance(getattr(camera_capture, "metadata", None), dict)
        else None
    )
    analysis_eligible = (
        bool(captured_auto_exposure.get("analysis_eligible"))
        if isinstance(captured_auto_exposure, dict)
        else (
            camera_capture_runtime.camera_analysis_eligible
            if camera_capture_runtime is not None
            else True
        )
    )
    ball_analysis_eligible = analysis_eligible or (
        isinstance(captured_auto_exposure, dict)
        and captured_auto_exposure.get("ball_analysis_eligible") is True
    )
    return (
        analysis_eligible,
        ball_analysis_eligible,
        captured_auto_exposure if isinstance(captured_auto_exposure, dict) else None,
    )


def _camera_ball_relaxed_only(camera_capture) -> bool:
    """Whether only the relaxed dark rule of the ball gate admits this clip.

    The saved ``capture_exposure_status`` says so (``ball_analysis_relaxed``)
    for a clip the club analysis does not take; see ``camera.lowlight``.
    """
    from openflight.camera.lowlight import relaxed_live_ball_gate  # noqa: PLC0415

    analysis_eligible, ball_analysis_eligible, auto_exposure = _camera_analysis_eligibility(
        camera_capture
    )
    return (
        ball_analysis_eligible and not analysis_eligible and relaxed_live_ball_gate(auto_exposure)
    )


def _fuse_camera_measurements(
    shot: Shot, camera_capture, *, deadline_monotonic: float | None = None
) -> _ClubHeadPathShadowJob | None:
    """Decode one camera clip and share it across all live estimators.

    ``deadline_monotonic`` is the shot's enrichment deadline; both estimators
    share it, so a ball flight cut off at it leaves no time to start another
    worker for the club delivery. Without one they share
    ``_CAMERA_CV_WORKER_TIMEOUT_S`` from now.

    Returns the camera club-data job of a ball-analysis-eligible clip, which
    runs once the shot is queued for publication, else None. A clip the ball
    gate admits only by its relaxed dark rule gets its ball flight (without the
    legacy fallback) and no club-data job: the club data keep the gate they had.
    """
    analysis_eligible, ball_analysis_eligible, _auto_exposure = _camera_analysis_eligibility(
        camera_capture
    )
    if not analysis_eligible:
        shot.experimental_camera_horizontal_status = "rejected_lighting_quality"
        shot.experimental_camera_horizontal_deg = None
        shot.experimental_camera_horizontal_confidence = None
        shot.experimental_camera_iwr_delta_deg = None
        shot.experimental_fused_attack_angle_deg = None
        shot.experimental_fused_club_path_deg = None
        shot.experimental_fused_status = "rejected_lighting_quality"
        shot.experimental_fused_attack_angle_confidence = "withheld"
        shot.experimental_fused_club_path_confidence = "withheld"
        logger.warning(
            "[SERVER] Camera club analysis withheld for lighting quality; ball analysis=%s",
            ball_analysis_eligible,
        )
        if not ball_analysis_eligible:
            return None
    if deadline_monotonic is None:
        deadline_monotonic = time.monotonic() + _CAMERA_CV_WORKER_TIMEOUT_S
    relaxed_only = _camera_ball_relaxed_only(camera_capture)
    camera_archive = _load_camera_capture_archive(camera_capture)
    shadow_job = _fuse_camera_ball_flight(
        shot,
        camera_capture,
        camera_archive,
        deadline_monotonic=deadline_monotonic,
        relaxed_only=relaxed_only,
    )
    if relaxed_only:
        logger.info(
            "[SERVER] Camera ball flight on a dark clip (relaxed ball gate): no legacy "
            "fallback, no club data"
        )
        shadow_job = None
    if analysis_eligible:
        _fuse_camera_club_delivery(
            shot, camera_capture, camera_archive, deadline_monotonic=deadline_monotonic
        )
    return shadow_job


def _optional_lowlight_shadow_job(shot: Shot, camera_capture) -> _LowLightShadowJob | None:
    """``_lowlight_shadow_job``, None when it cannot be built: never costs the shot anything."""
    try:
        return _lowlight_shadow_job(shot, camera_capture)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera low-light shadow job not built: %s", error, exc_info=True)
        return None


def _lowlight_shadow_job(shot: Shot, camera_capture) -> _LowLightShadowJob | None:
    """The low-light shadow job of one shot's clip, built after the live camera fusion.

    None when the shadow is off or there is no valid clip; a job with a
    ``skip_reason`` for a valid clip it cannot run on (no IWR runtime or tee
    geometry), so every clip gets one ``camera_lowlight_ball_flight`` entry. Reads
    the shot, never changes it.
    """
    if (
        not camera_lowlight_shadow_enabled
        or shot.mode == "mock"
        or camera_capture is None
        or not camera_capture.valid
        or not camera_capture.path
    ):
        return None
    from openflight.camera.ball_flight import DEFAULT_BALL_LOCATOR  # noqa: PLC0415

    _analysis, live_ball_eligible, auto_exposure = _camera_analysis_eligibility(camera_capture)
    common = {
        "shot_number": shot.shot_number,
        "shot_timestamp": shot.impact_timestamp,
        "camera_capture": camera_capture,
        "live_ball_analysis_eligible": bool(live_ball_eligible),
        "auto_exposure": auto_exposure,
        "live_camera_horizontal_deg": shot.experimental_camera_horizontal_deg,
        "live_camera_horizontal_confidence": shot.experimental_camera_horizontal_confidence,
        "live_camera_horizontal_status": shot.experimental_camera_horizontal_status,
    }
    if iwr6843_runtime is None:
        return _LowLightShadowJob(**common, skip_reason="skipped_no_iwr_runtime")
    calibration = iwr6843_runtime.calibration
    if calibration.tee_range_m is None:
        return _LowLightShadowJob(**common, skip_reason="skipped_no_tee_geometry")
    target = angle_reference.target_line
    return _LowLightShadowJob(
        **common,
        geometry=_camera_ball_geometry(
            camera_capture,
            calibration,
            target_px=target.frame_px if target is not None else None,
            shot=shot,
        ),
        ball_locator=camera_capture_config.get("ball_locator", DEFAULT_BALL_LOCATOR),
        range_evidence=shot.iwr6843_ball_range_evidence,
        ops_ball_speed_mph=shot.ball_speed_raw_mph or shot.ball_speed_mph,
        iwr_vertical_deg=shot.launch_angle_vertical,
        iwr_horizontal_deg=shot.iwr6843_horizontal_deg,
        iwr_horizontal_confidence=shot.iwr6843_horizontal_confidence,
        tee_zone=(
            camera_capture_runtime.active_tee_zone() if camera_capture_runtime is not None else None
        ),
        ball_mark_px=_camera_ball_mark_px(),
    )


def _lowlight_worker_kwargs(job: _LowLightShadowJob, archive: dict, gate) -> dict:
    """The ``lowlight_ball_flight`` worker job's arguments (``camera.lowlight``)."""
    return {
        "frames": archive["frames"],
        "timestamps_ns": _camera_frame_timestamps(archive),
        "trigger_ns": int(archive["trigger_host_timestamp_ns"]),
        "relaxed_only": gate.relaxed_only,
        "ball_locator": job.ball_locator,
        "range_evidence": job.range_evidence,
        "geometry": job.geometry,
        "ops_ball_speed_mph": job.ops_ball_speed_mph,
        "iwr_vertical_deg": job.iwr_vertical_deg,
        "ball_tracker": camera_lowlight_reference_tracker,
        "ground_anchor": camera_lowlight_ground_anchor,
        "tee_zone": job.tee_zone,
        "ball_mark_px": job.ball_mark_px,
    }


def _lowlight_result_fields(job: _LowLightShadowJob, result) -> dict:
    """The shadow start direction of one run, the flicker step and the live comparison."""
    from openflight.camera.ball_flight import select_camera_assisted_horizontal  # noqa: PLC0415

    estimate = result.estimate
    decision = select_camera_assisted_horizontal(
        estimate,
        iwr_horizontal_deg=job.iwr_horizontal_deg,
        iwr_confidence=job.iwr_horizontal_confidence,
    )
    deg, confidence, status = _camera_horizontal_fields(estimate, decision)
    live_deg = job.live_camera_horizontal_deg
    return {
        "lowlight_camera_horizontal_deg": deg,
        "lowlight_camera_horizontal_confidence": confidence,
        "lowlight_camera_horizontal_status": status,
        "lowlight_camera_iwr_delta_deg": decision.camera_iwr_delta_deg,
        "lowlight_camera_vertical_deg": _accepted_camera_vertical_deg(estimate),
        "lowlight_estimate_status": estimate.status,
        "lowlight_ball_locator": result.ball_locator,
        "lowlight_legacy_fallback": result.legacy_fallback,
        "lowlight_locator": estimate.locator.to_dict() if estimate.locator is not None else None,
        "flicker_std": result.flicker.rel_std,
        "flicker_compensated": result.flicker.compensated,
        "flicker": result.flicker.to_dict(),
        "lowlight_job_ms": round(result.elapsed_ms, 1),
        "lowlight_new_direction": live_deg is None and deg is not None,
        "lowlight_minus_live_deg": (
            round(deg - live_deg, 3) if deg is not None and live_deg is not None else None
        ),
    }


def _lowlight_shadow_run(
    job: _LowLightShadowJob, worker
) -> tuple[str, str, dict, str | None, bool]:
    """Load the clip, gate it and run the worker job.

    Returns (status, reason, log fields, error text, whether the worker was
    discarded). The worker's tracker and ground anchor state is adopted by the
    shadow path's own.
    """
    from openflight.camera.lowlight import known_controls, lowlight_ball_gate  # noqa: PLC0415

    archive = _load_camera_capture_archive(job.camera_capture)
    if archive is None or "trigger_host_timestamp_ns" not in archive:
        return "skipped", "skipped_missing_frames", {"lowlight_gate": None}, None, False
    gate = lowlight_ball_gate(
        job.auto_exposure,
        live_ball_eligible=job.live_ball_analysis_eligible,
        controls_known=known_controls(archive),
    )
    logged: dict = {"lowlight_gate": gate.reason}
    if not gate.eligible:
        return "skipped", f"skipped_gate_{gate.reason}", logged, None, False
    kwargs = _lowlight_worker_kwargs(job, archive, gate)
    result, state, reason, error_text, discarded = _run_after_publication_job(
        "lowlight_ball_flight",
        kwargs,
        worker,
        budget_s=_CAMERA_LOWLIGHT_BUDGET_S,
        dropped="low-light shadow direction",
    )
    for name, returned in state.items():
        if kwargs.get(name) is not None:
            kwargs[name].adopt(returned)
    if result is None:
        return "skipped", reason, logged, error_text, discarded
    logged.update(_lowlight_result_fields(job, result))
    return "done", "ran", logged, None, False


def _run_camera_lowlight_shadow(job: _LowLightShadowJob, *, enrichment_on_time: bool) -> None:
    """The low-light shadow start direction of one shot, after its publication: log only.

    Runs after the club-data job, under the same rules
    (``_after_publication_skip_reason``): never while another shot waits, never
    for a shot past its deadline, only in a running CV worker and within
    ``_CAMERA_LOWLIGHT_BUDGET_S``. Loads the clip, applies the relaxed ball gate
    (``camera.lowlight.lowlight_ball_gate``) and runs the ``lowlight_ball_flight``
    job on the shadow path's own tracker and ground anchor. Writes one
    ``camera_lowlight_ball_flight`` session-log entry per clip, ``done`` or
    ``skipped`` with the reason; nothing reaches the shot, the screens or the
    simulators. Never raises.
    """
    try:
        session_log = get_session_logger()
        if session_log is None:
            return
        started = time.monotonic()
        worker = camera_cv_worker
        status, logged, error_text, discarded = "skipped", {"lowlight_gate": None}, None, False
        reason = _after_publication_skip_reason(
            job.skip_reason, worker, enrichment_on_time=enrichment_on_time
        )
        if reason is None:
            status, reason, logged, error_text, discarded = _lowlight_shadow_run(job, worker)
        if discarded:
            _restart_camera_cv_worker_when_idle(worker)
        elif reason == "skipped_worker_not_running":
            # Crashed or stopped and no ball-eligible clip to start it lazily:
            # start it in idle time for the next shot, never inside this one.
            _restart_camera_cv_worker_when_idle(worker, _CAMERA_CV_FOR_LOWLIGHT_SHADOW)
        entry = {
            "status": status,
            "reason": reason,
            **logged,
            "live_ball_analysis_eligible": job.live_ball_analysis_eligible,
            "live_camera_horizontal_deg": job.live_camera_horizontal_deg,
            "live_camera_horizontal_confidence": job.live_camera_horizontal_confidence,
            "live_camera_horizontal_status": job.live_camera_horizontal_status,
            "elapsed_s": round(time.monotonic() - started, 3),
        }
        if error_text is not None:
            entry["error"] = error_text
        session_log.log_camera_lowlight_ball_flight(
            shot_number=job.shot_number, shot_timestamp=job.shot_timestamp, result=entry
        )
        logger.info(
            "[SERVER] Camera low-light shadow: %s %s gate=%s camera=%s (live %s) flicker=%s/%s "
            "in %.2fs",
            status,
            reason,
            logged.get("lowlight_gate"),
            logged.get("lowlight_camera_horizontal_deg"),
            job.live_camera_horizontal_deg,
            logged.get("flicker_std"),
            logged.get("flicker_compensated"),
            entry["elapsed_s"],
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera low-light shadow error: %s", error, exc_info=True)
        log_session_error(
            "Camera low-light shadow failed",
            component="camera_capture",
            context={"stage": "lowlight_shadow", "shot_number": job.shot_number},
            exc=error,
        )


# Keys of the ``attack`` log section (plan section 3b): the arc fit, its sigma terms,
# the diagnostics and the lofts. Every key is present, None/0/{} when it did not run.
_ATTACK_LOG_FIT_KEYS = (
    "status",
    "attack_deg",
    "sigma_deg",
    "method",
    "n_frames",
    "feature",
    "radius_m",
    "plane_deg",
    "contact_height_mm",
    "rms_px",
    "arc_path_deg",
    "arc_path_sigma_deg",
    "arc_path_status",
    "arc_path_total_sigma_deg",
    "arc_path_stat_sigma_deg",
    "arc_path_jackknife_sigma_deg",
    "arc_path_feature_sigma_deg",
    "arc_path_contact_gain_deg_per_ms",
    "arc_path_speed_gain_deg_per_mph",
    "stat_sigma_deg",
    "jackknife_sigma_deg",
    "feature_sigma_deg",
    "contact_sigma_deg",
    "speed_sigma_deg",
    "contact_gain_deg_per_ms",
    "low_point_cm",
    "low_point_sigma_cm",
    "quadratic_attack_deg",
    "window_delta_deg",
    "feature_attacks_deg",
)
_ATTACK_LOG_LOFT_KEYS = (
    "dynamic_loft_deg",
    "dynamic_loft_sigma_deg",
    "spin_loft_deg",
    "spin_loft_sigma_deg",
)
_CLUB_DATA_STAGES = ("departure", "path", "attack", "impact", "impact_point")


def _attack_log_payload(attack, loft, camera_vertical_deg: float | None) -> dict:
    """The ``attack`` log section: the arc fit (``club_attack``) and the lofts from it."""
    payload = dict.fromkeys(_ATTACK_LOG_FIT_KEYS)
    payload.update(status="not_run", n_frames=0, feature_attacks_deg={})
    if attack is not None:
        payload.update({key: getattr(attack, key) for key in _ATTACK_LOG_FIT_KEYS})
        payload["feature_attacks_deg"] = dict(attack.feature_attacks_deg)
    payload["launch_vertical_deg"] = camera_vertical_deg
    payload["launch_source"] = (
        loft.launch_source
        if loft is not None
        else ("camera" if camera_vertical_deg is not None else None)
    )
    payload["face_weight"] = None if loft is None else loft.face_weight
    for key in _ATTACK_LOG_LOFT_KEYS:
        payload[key] = None if loft is None else getattr(loft, key)
    return payload


def _head_quality_log_fields(quality) -> dict:
    """One frame's rated head ``contrast`` and ``edge_width_px``, 1 decimal.

    ``quality`` is a ``camera.club_head_path.HeadQuality``; a value is None where
    no head was rated (``not_found``) or it is not finite (an edge-less head).
    """
    rated = quality.status != "not_found"
    return {
        name: round(value, 1) if rated and math.isfinite(value) else None
        for name, value in (
            ("contrast", quality.contrast),
            ("edge_width_px", quality.edge_width_px),
        )
    }


def _club_head_path_log_payload(  # pylint: disable=too-many-arguments
    job: _ClubHeadPathShadowJob,
    result,
    reason: str,
    elapsed_s: float,
    worker_restart_started: bool | None = None,
    *,
    calculated=None,
    acoustic_delay_n: int | None = None,
    tee_control: tuple[int, int, bool] | None = None,
) -> dict:
    """Session-log fields of one club-data run; ``result`` is None when it did not run.

    ``result`` is the worker's ``camera.club_head_path.ClubDataResult`` and
    ``calculated`` the ``club_data.CalculatedClubData`` gated from it (its lofts
    and D-plane face). The contact fields come from the run's contact time, else
    from the job's preliminary one; they are None for a skipped clip without one.
    ``worker_restart_started`` is None unless the job cut off or lost the worker:
    then whether a background start of it began (its outcome goes to the log).
    ``acoustic_delay_n`` is how many teed shots the session's acoustic-delay
    calibration had when the job started, ``tee_control`` the state of its tee
    control then (``TeeControl.state``: teed departures, late ones, passes).
    The ``t0_shadow_*`` fields (the run's ``ContactShadow``) and
    ``path_last_frame_gap_ms`` are None for a clip the job did not run on, the
    ``path_arc_*`` fields (logged only) empty with status ``not_run``, and
    ``path_noise_sigma`` (logged only) None.
    """
    path = None if result is None else result.path
    contact = job.contact if result is None else result.contact
    departure = None if result is None else result.camera_departure
    payload = {
        "accepted": bool(path is not None and path.accepted),
        "reason": reason,
        "path_deg": None,
        "sigma_deg": None,
        "feature": None,
        "polarity": None,
        "n_frames": 0,
        "fit_sigma_deg": None,
        "jackknife_sigma_deg": None,
        "feature_sigma_deg": None,
        "contact_sigma_deg": None,
        "feature_paths_deg": {},
        "frames": [],
        "t0_source": None,
        "t0_ms": None,
        "t0_sigma_ms": None,
        "t0_reason": None,
        "departure_fit_ms": None,
        "sound_bound_ms": None,
        "trigger_stamp_lag_ms": None,
        "edge_sound_bound_ms": None,
        "edge_bound_verdict": None,
        "edge_reference": job.edge_reference,
        "camera_departure_ms": None,
        "camera_departure_points": None if departure is None else departure.n_points,
        "camera_departure_rms_px": None if departure is None else departure.rms_px,
        "camera_departure_span_frames": None if departure is None else departure.span_frames,
        "departure_agreement_ms": None,
        "acoustic_ms": None,
        "acoustic_reference": None,
        "acoustic_delay_ms": None,
        "acoustic_delay_source": None,
        "acoustic_delay_n": acoustic_delay_n,
        "acoustic_kernel_ms": None,
        "t0_shadow_ms": None,
        "t0_shadow_source": None,
        "t0_shadow_reason": None,
        "t0_shadow_minus_t0_ms": None,
        "tee_control_n_teed": None if tee_control is None else tee_control[0],
        "tee_control_n_late": None if tee_control is None else tee_control[1],
        "tee_control_passes": None if tee_control is None else tee_control[2],
        "path_last_frame_gap_ms": None if path is None else path.last_frame_gap_ms,
        "path_arc_status": "not_run" if path is None else path.path_arc_status,
        "path_arc_deg": None if path is None else path.path_arc_deg,
        "path_arc_sigma_deg": None if path is None else path.path_arc_sigma_deg,
        "path_arc_radii_deg": {} if path is None else dict(path.path_arc_radii_deg),
        "path_arc_window_deg": {} if path is None else dict(path.path_arc_window_deg),
        "path_noise_sigma": None if path is None else path.noise_sigma,
        "segmentation": None,
        "segmentation_sigma_deg": None,
        "segmentation_paths_deg": {},
        "ball_source": None,
        "ball_x_px": None,
        "ball_y_px": None,
        "ball_d_px": None,
        "ball_anchor": None,
        "ball_refined": None,
        "ball_rejected": None,
        "elapsed_s": round(elapsed_s, 3),
        "stage_elapsed_s": (
            {}
            if result is None
            else {
                stage: round(float(result.stage_elapsed_s.get(stage, 0.0)), 3)
                for stage in _CLUB_DATA_STAGES
            }
        ),
        "budget_s": _CAMERA_CLUB_DATA_BUDGET_S,
        "club_data_mode": club_data_mode,
        "worker_restart_started": worker_restart_started,
        "attack": _attack_log_payload(
            None if path is None else path.attack,
            None if calculated is None else calculated.loft,
            job.camera_vertical_deg,
        ),
        "impact": (
            path.impact.to_dict()
            if path is not None and path.impact is not None
            else {"status": "not_run"}
        ),
        "impact_point": (
            path.impact_point.to_dict()
            if path is not None and getattr(path, "impact_point", None) is not None
            else {"status": "not_run"}
        ),
        "ball_teed": job.ball_teed,
        "dplane": (
            None if calculated is None or calculated.face is None else calculated.face.to_dict()
        ),
        "dplane_reason": ("not_run" if calculated is None else calculated.dplane_reason),
    }
    club_ball = None if result is None else getattr(result, "ball", None)
    if club_ball is not None:
        payload.update(club_ball.to_log())
    contact_shadow = None if result is None else getattr(result, "contact_shadow", None)
    if contact_shadow is not None:
        payload.update(contact_shadow.to_log())
    if contact is not None:
        payload.update(
            t0_source=contact.source,
            t0_ms=contact.t0_ms,
            t0_sigma_ms=contact.sigma_s * 1e3,
            t0_reason=contact.reason,
            departure_fit_ms=contact.departure_fit_ms,
            sound_bound_ms=contact.sound_bound_ms,
            trigger_stamp_lag_ms=contact.trigger_lag_ms,
            edge_sound_bound_ms=contact.edge_sound_bound_ms,
            edge_bound_verdict=contact.edge_bound_verdict,
            camera_departure_ms=contact.camera_departure_ms,
            departure_agreement_ms=contact.departure_agreement_ms,
            acoustic_ms=contact.acoustic_ms,
            acoustic_reference=contact.acoustic_reference,
            acoustic_delay_ms=contact.acoustic_delay_ms,
            acoustic_delay_source=contact.acoustic_delay_source,
            acoustic_kernel_ms=contact.acoustic_kernel_ms,
        )
    if path is not None:
        ball_px = path.ball_px
        payload.update(
            path_deg=path.path_deg,
            sigma_deg=path.sigma_deg,
            feature=path.feature,
            polarity=path.polarity,
            n_frames=path.n_frames,
            fit_sigma_deg=path.fit_sigma_deg,
            jackknife_sigma_deg=path.jackknife_sigma_deg,
            feature_sigma_deg=path.feature_sigma_deg,
            contact_sigma_deg=path.contact_sigma_deg,
            feature_paths_deg=dict(path.feature_paths_deg),
            frames=[
                {
                    "frame_index": item.frame_index,
                    "t_rel_ms": item.t_rel_ms,
                    "quality": item.quality.status,
                    **_head_quality_log_fields(item.quality),
                    "width_m": item.width_m,
                    "residual_mm": item.residual_mm,
                }
                for item in path.observations
            ],
            segmentation=path.segmentation,
            segmentation_sigma_deg=path.segmentation_sigma_deg,
            segmentation_paths_deg=dict(path.segmentation_paths_deg),
            ball_source=path.ball_source,
            ball_x_px=None if ball_px is None else ball_px[0],
            ball_y_px=None if ball_px is None else ball_px[1],
            ball_d_px=None if ball_px is None else ball_px[2],
        )
    return payload


def _club_head_path_shadow_skip_reason(
    job: _ClubHeadPathShadowJob, worker, *, enrichment_on_time: bool
) -> str | None:
    """Why the club-data job does not ask the worker for this clip, or None.

    It runs on the shot-enrichment thread, so it would hold up the next shot's
    enrichment: it never starts while a shot waits, nor for a shot that missed its
    enrichment deadline (a backlog). Nor without a running worker (it never runs
    in the server process, and never pays a worker start itself).
    """
    return _after_publication_skip_reason(
        job.skip_reason, worker, enrichment_on_time=enrichment_on_time
    )


def _after_publication_skip_reason(
    skip_reason: str | None, worker, *, enrichment_on_time: bool
) -> str | None:
    """Why a CV-worker job after publication (club data, low-light shadow) does not run, or None.

    ``skip_reason`` is the job's own (set when it was built); see
    ``_club_head_path_shadow_skip_reason`` for the rest.
    """
    if skip_reason is not None:
        return skip_reason
    if not enrichment_on_time:
        return "skipped_shot_past_deadline"
    if not shot_enrichment_queue.empty():
        return "skipped_next_shot_queued"
    if worker is None:
        return "skipped_cv_in_process"
    if worker.pid is None:
        return "skipped_worker_not_running"
    return None


_CAMERA_CV_RESTART_THREAD_NAME = "camera-cv-restart"
_CAMERA_CV_AFTER_CLUB_HEAD_PATH = "after the shadow club-head path"


def _restart_camera_cv_worker_when_idle(
    worker, why: str = _CAMERA_CV_AFTER_CLUB_HEAD_PATH
) -> threading.Thread | None:
    """Start the CV worker again after the shadow job cut it off or lost it.

    Only while no shot waits: the next ball flight then finds a running worker
    instead of paying the start inside its own deadline. A waiting shot's ball
    flight starts it anyway, so starting it here would only delay that shot.
    The start (a fresh interpreter importing the server, numpy, scipy and cv2)
    runs in its own thread, so the enrichment thread is free at once: a shot
    that needs the worker meanwhile waits for the rest of the start on the
    worker's lock, a shot that does not need it does not wait. Returns that
    thread, None when a shot waits.
    """
    if not shot_enrichment_queue.empty():
        return None
    return _start_camera_cv_worker_in_background(worker, why)


def _start_camera_cv_worker_in_background(
    worker, why: str, *, action: str = "restart"
) -> threading.Thread:
    """Run ``_start_camera_cv_worker`` in its own thread and return that thread."""
    thread = threading.Thread(
        target=_start_camera_cv_worker,
        args=(worker, why),
        kwargs={"action": action},
        name=_CAMERA_CV_RESTART_THREAD_NAME,
        daemon=True,
    )
    thread.start()
    return thread


def _start_camera_cv_worker(
    worker, why: str = _CAMERA_CV_AFTER_CLUB_HEAD_PATH, *, action: str = "restart"
) -> None:
    """The background (re)start: start the worker and log how it went. Never raises."""
    started = time.monotonic()
    try:
        pid = worker.start()
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[CAMERA-CV] Worker %s %s failed after %.2fs: %s",
            action,
            why,
            time.monotonic() - started,
            error,
        )
        return
    logger.info(
        "[CAMERA-CV] Worker %sed (pid %s) in %.2fs %s",
        action,
        pid,
        time.monotonic() - started,
        why,
    )


_CAMERA_CV_FOR_LOWLIGHT_SHADOW = "for the low-light shadow"


def _prestart_camera_cv_worker_for_lowlight_shadow(worker) -> threading.Thread | None:
    """Start the CV worker with the camera capture when the low-light shadow is on.

    The worker otherwise starts lazily with the first ball-eligible clip; in full
    darkness no clip is eligible, so the shadow (which never pays a worker start
    itself) skipped every shot as ``skipped_worker_not_running`` (range
    2026-11-02, session 2). This is a one-time cost at startup, in the
    background, not per shot. Returns the start thread, None when it is not
    needed.
    """
    if worker is None or not camera_lowlight_shadow_enabled:
        return None
    return _start_camera_cv_worker_in_background(
        worker, _CAMERA_CV_FOR_LOWLIGHT_SHADOW, action="start"
    )


def _club_data_worker_kwargs(job: _ClubHeadPathShadowJob, acoustic_delay_ms: float | None) -> dict:
    """The ``club_data`` worker job's arguments (``club_head_path.estimate_club_data``)."""
    return {
        **job.kwargs,
        "time_zero": job.time_zero,
        "trigger_ns": job.trigger_ns,
        "tee_range_m": job.tee_range_m,
        "trigger_lag_ms": job.trigger_lag_ms,
        "lag_reference": job.edge_reference,
        "teed": job.teed,
        "acoustic_delay_ms": acoustic_delay_ms,
        "club": job.club,
        "handedness": job.handedness,
        "departed_ball": job.departed_ball,
        "confirmed_tee": job.confirmed_tee,
        "reference_ball": job.reference_ball,
        "tee_zone": job.tee_zone,
        "head_length_median_mm": job.head_length_median_mm,
        "ball_speed_mph": job.ball_speed_mph,
        "launch_vertical_deg": job.launch_vertical_deg,
        "launch_horizontal_deg": job.launch_direction_deg,
        "impact_point_head_length_median_mm": job.impact_point_head_length_median_mm,
    }


def _run_club_data_job(
    job: _ClubHeadPathShadowJob, worker, acoustic_delay_ms: float | None
) -> tuple[object | None, str, str | None, bool]:
    """Run the ``club_data`` job in the worker within its budget.

    Returns (result, reason, error text, whether the worker was discarded); the
    result is None when the job failed, and the reason is then why.
    """
    result, _state, reason, error_text, discarded = _run_after_publication_job(
        "club_data",
        _club_data_worker_kwargs(job, acoustic_delay_ms),
        worker,
        budget_s=_CAMERA_CLUB_DATA_BUDGET_S,
        dropped="club data",
    )
    if result is None:
        return None, reason, error_text, discarded
    return result, result.path.status, None, False


def _run_after_publication_job(  # pylint: disable=too-many-arguments
    name: str, kwargs: dict, worker, *, budget_s: float, dropped: str
) -> tuple[object | None, dict, str | None, str | None, bool]:
    """Run one job after publication in the worker within ``budget_s``, never in-process.

    Returns (result, stateful arguments as the job left them, failure reason,
    error text, whether the worker was discarded). On failure the result is
    None and the reason is ``timeout``, ``job_error`` or ``worker_error``;
    ``dropped`` names what is lost in the warning.
    """
    from openflight.camera.cv_worker import (  # noqa: PLC0415
        CameraCvDeadlinePassed,
        CameraCvJobError,
        CameraCvWorkerError,
        CameraCvWorkerTimeout,
    )

    try:
        result, state = worker.run(name, kwargs, timeout_s=budget_s)
    except CameraCvDeadlinePassed:
        # The budget ran out while another call held the worker: the job never
        # reached it and the worker was not cut off.
        logger.warning(
            "[CAMERA-CV] %s's %.1fs budget ran out waiting for the worker; not sent, %s dropped",
            name,
            budget_s,
            dropped,
        )
        return None, {}, "timeout", None, False
    except CameraCvWorkerTimeout:
        logger.warning(
            "[CAMERA-CV] %s exceeded its %.1fs budget; worker cut off, %s dropped",
            name,
            budget_s,
            dropped,
        )
        return None, {}, "timeout", None, True
    except (CameraCvJobError, CameraCvWorkerError) as error:
        # The estimator raising leaves the worker running; a worker failure does not.
        reason = "job_error" if isinstance(error, CameraCvJobError) else "worker_error"
        error_text = str(error).splitlines()[0] if str(error) else type(error).__name__
        logger.warning(
            "[CAMERA-CV] %s failed in the worker (%s); %s dropped", name, error_text, dropped
        )
        state = error.state if isinstance(error, CameraCvJobError) else {}
        return None, state, reason, error_text, reason == "worker_error"
    return result, state, None, None, False


def _learn_club_data_session(job: _ClubHeadPathShadowJob, result) -> None:
    """Feed one run into the session state: the acoustic delay, its tee control and the head lengths.

    The head-length medians (E and the impact point D each) learn from every
    measured head length inside the absolute plausible range, whatever the
    impact's status: learning only from accepted impacts kept them from ever
    seeing values the band would have let in. A ball anchored on the tapped
    mark (``ClubBall.anchor`` ``mark``) feeds neither median: that clip's ball
    is the refine's find around the tap, not one the ball flight or the session
    tracker saw, and the medians gate every later shot's impact of that club,
    departed balls too. It still reaches the acoustic delay and its tee
    control, which learn only from teed shots, and a teed shot never anchors on
    the mark.
    """
    from .camera.club_head_path import head_length_plausible  # noqa: PLC0415
    from .camera.impact_point import (  # noqa: PLC0415
        head_length_plausible as point_head_length_plausible,
    )

    _club_data_calibrator_instance().add_contact(result.contact, teed=job.teed)
    key = (job.profile_id, job.club_key)
    impact = result.path.impact
    point = getattr(result.path, "impact_point", None)
    on_mark = getattr(getattr(result, "ball", None), "anchor", None) == "mark"
    with _club_data_state_lock:
        if on_mark:
            return
        if impact is not None and head_length_plausible(impact.head_length_mm):
            _club_head_length_mm.setdefault(key, []).append(float(impact.head_length_mm))
        if point is None:
            return
        if point_head_length_plausible(point.head_length_mm):
            _impact_point_head_length_mm.setdefault(key, []).append(float(point.head_length_mm))


def _publish_club_data(  # pylint: disable=too-many-arguments
    job: _ClubHeadPathShadowJob,
    *,
    source_shot: Shot | None,
    final_shot: Shot | None,
    shot_fields: dict,
    status: str,
    reason: str | None,
) -> None:
    """Put one shot's club data on its shots; a published shot gets one ``shot_update``.

    ``final_shot`` is the enriched copy finalization copies into the stored
    ``source_shot`` (the same object without slow enrichment), so both get the
    fields: before finalization the final event carries them, after it the
    stored shot is re-sent with ``pending: {}``. A running finalization is
    waited for (up to ``_CLUB_DATA_FINALIZATION_WAIT_S``), so its copy never
    interleaves with these fields. A deleted or cleared shot, or one from an
    earlier session, gets no emit; while the demo runs nothing is emitted.

    The camera D-plane axis (``_camera_spin_axis``) becomes the shot's one spin
    axis and curve class (``_set_calculated_spin_axis``), replacing a K-LD7 one.
    Before finalization the final flight, session-log entry and simulator export
    carry it; after it the stored shot is flown again (``_fly_shot``), the session
    log gets a ``shot_updated`` entry with the re-flown shot, the carry span is
    flown again and the ``shot_update`` carries the new flight. A simulator that
    already got the shot is not sent it again. Never raises.
    """
    try:
        shots = [shot for shot in (final_shot, source_shot) if shot is not None]
        if not shots:
            return
        values = {**shot_fields, "club_data_status": status, "club_data_reason": reason}
        axis = None
        with _shot_finalization_condition:
            registered = _shot_finalization_registered.get(job.shot_number)
            finalized = registered is None or registered.shot is not source_shot
            if finalized:
                wait_until = time.monotonic() + _CLUB_DATA_FINALIZATION_WAIT_S
                while _shot_finalization_running:
                    remaining_s = wait_until - time.monotonic()
                    if remaining_s <= 0.0:
                        break
                    _shot_finalization_condition.wait(remaining_s)
            for shot in shots:
                for name, value in values.items():
                    setattr(shot, name, value)
            # The stored shot is the one finalization published; before it, the copy.
            axis = _camera_spin_axis(
                source_shot if finalized and source_shot is not None else shots[0], values
            )
            if axis is not None:
                for shot in shots:
                    _set_calculated_spin_axis(shot, axis)
        if not finalized or source_shot is None or monitor is None:
            return
        # A deleted or cleared shot, or one of an earlier session, is not flown again.
        if not any(shot is source_shot for shot in monitor.get_shots()):
            return
        if axis is not None:
            _refly_published_shot(source_shot)
        _emit_real_shot_event(
            "shot_update",
            {
                "shot": shot_to_dict(source_shot),
                "stats": monitor.get_session_stats(),
                "pending": {},
            },
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Club data publish failed: %s", error, exc_info=True)
        log_session_error(
            "Club data publish failed",
            component="camera_capture",
            context={"stage": "club_data_publish", "shot_number": job.shot_number},
            exc=error,
        )


def _refly_published_shot(shot: Shot) -> None:
    """Fly a published shot again after its spin axis changed; log it and its span.

    The session log's ``shot_updated`` entry holds the re-flown shot, which the
    saved-session history lays over its ``shot_detected`` entry; the carry span
    then follows as its own ``shot_update`` (``_start_carry_span``) and the carry
    variants as a second ``carry_variants`` entry (``_start_carry_variants``).
    """
    flight = _fly_shot(shot)
    logger.log(
        _shot_log_level(shot),
        "[SERVER] Spin axis: %+.1f° calculated (camera D-plane, %s), shot #%s re-flown: "
        "carry %.0f yds, lateral %s",
        shot.spin_axis_deg,
        shot.curve_class,
        shot.shot_number,
        shot.carry_spin_adjusted if shot.carry_spin_adjusted is not None else float("nan"),
        "n/a" if shot.carry_lateral_yards is None else f"{shot.carry_lateral_yards:+.1f} yds",
    )
    if not shot.demo:
        session_log = get_session_logger()
        if session_log is not None:
            session_log.log_shot_update(shot)
    _start_carry_span(shot, flight)
    _start_carry_variants(shot, flight)


def _run_camera_club_head_path_shadow(  # pylint: disable=too-many-locals
    job: _ClubHeadPathShadowJob,
    *,
    enrichment_on_time: bool = True,
    source_shot: Shot | None = None,
    final_shot: Shot | None = None,
) -> None:
    """Camera club data of one shot, after its publication: log, gate and publish.

    Runs the ``club_data`` job (contact time, gate-free departure, path, arc
    attack angle and impact) only in a running CV worker, within
    ``_CAMERA_CLUB_DATA_BUDGET_S`` and without the in-process fallback: in the
    server process it would hold the GIL the frame callback needs and could not
    be cut off. ``enrichment_on_time`` is whether this shot's enrichment reached
    finalization before its deadline; see ``_club_head_path_shadow_skip_reason``
    for when it does not start. A shot arriving while it runs waits for it, up
    to the budget plus the worker's cut-off.

    Every eligible clip gets a ``camera_club_head_path`` and a ``club_data``
    session-log entry. In ``--club-data show`` the gated values
    (``club_data.calculate_club_data``) and the status go onto ``source_shot`` /
    ``final_shot`` (``_publish_club_data``): ``done`` when the job ran, else
    ``skipped`` with the reason, in a ``finally`` so no shot stays ``pending``.
    In ``log`` mode nothing reaches the shot. Never ``club_path_deg``; in ``show``
    mode an iron's camera D-plane axis becomes the shot's spin axis, curve class
    and flight (``_publish_club_data``). Never raises.
    """
    mode = club_data_mode
    source_shot = source_shot if source_shot is not None else job.source_shot
    final_shot = final_shot if final_shot is not None else job.final_shot
    status, status_reason, shot_fields = "skipped", "error", {}
    try:
        session_log = get_session_logger()
        if session_log is None and mode != "show":
            status_reason = "skipped_no_session_log"
            return
        worker = camera_cv_worker
        result = calculated = error_text = worker_restart_started = None
        worker_discarded = False
        calibrator = _club_data_calibrator_instance()
        _delay_ms, _delay_source, delay_n = calibrator.delay()
        tee_control = calibrator.tee_control.state()
        started = time.monotonic()
        reason = _club_head_path_shadow_skip_reason(
            job, worker, enrichment_on_time=enrichment_on_time
        )
        if reason is None:
            result, reason, error_text, worker_discarded = _run_club_data_job(
                job, worker, calibrator.session_delay_ms()
            )
        elapsed_s = time.monotonic() - started
        if worker_discarded:
            worker_restart_started = _restart_camera_cv_worker_when_idle(worker) is not None
        status_reason = reason
        if result is not None:
            _learn_club_data_session(job, result)
            calculated = calculate_club_data(
                result,
                club=job.club,
                launch_direction_deg=job.launch_direction_deg,
                launch_direction_status=job.launch_direction_status,
                launch_vertical_deg=job.launch_vertical_deg,
                camera_vertical_deg=job.camera_vertical_deg,
                roll_calibrated=job.roll_calibrated,
                handedness=job.handedness,
                teed=bool(job.teed or job.ball_teed),
            )
            status, shot_fields = "done", dict(calculated.fields)
        payload = _club_head_path_log_payload(
            job,
            result,
            reason,
            elapsed_s,
            worker_restart_started,
            calculated=calculated,
            acoustic_delay_n=delay_n,
            tee_control=tee_control,
        )
        if error_text is not None:
            payload["error"] = error_text
        if session_log is not None:
            session_log.log_camera_club_head_path(
                shot_number=job.shot_number,
                shot_timestamp=job.shot_timestamp,
                result=payload,
            )
            session_log.log_club_data(
                shot_number=job.shot_number,
                shot_timestamp=job.shot_timestamp,
                status=status,
                reason=status_reason,
                fields=shot_fields or dict.fromkeys(CLUB_DATA_FIELDS),
                mode=mode,
            )
        logger.info(
            "[SERVER] Camera club data (%s): %s %s path=%s attack=%s impact=%s "
            "impact_point=%s t0=%s %s ms (%s) in %.2fs",
            mode,
            status,
            reason,
            payload["path_deg"],
            payload["attack"]["status"],
            payload["impact"]["status"],
            payload["impact_point"]["status"],
            payload["t0_source"],
            "n/a" if payload["t0_ms"] is None else f"{payload['t0_ms']:+.2f}",
            payload["t0_reason"],
            payload["elapsed_s"],
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera club data error: %s", error, exc_info=True)
        log_session_error(
            "Camera club data failed",
            component="camera_capture",
            context={"stage": "club_head_path_shadow", "shot_number": job.shot_number},
            exc=error,
        )
    finally:
        if mode == "show":
            _publish_club_data(
                job,
                source_shot=source_shot,
                final_shot=final_shot,
                shot_fields=shot_fields,
                status=status,
                reason=status_reason,
            )


def _attach_camera_replay(shot: Shot, camera_capture) -> None:
    """Expose a matched raw clip without doing any video conversion."""
    if (
        shot.demo
        or camera_replay_manager is None
        or camera_capture is None
        or not camera_capture.valid
        or not camera_capture.path
    ):
        return
    try:
        shot.camera_replay = camera_replay_manager.register(
            camera_capture.path,
            camera_capture.metadata,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[CAMERA] Replay registration failed: %s", error)
        log_session_error(
            "Camera replay registration failed",
            component="camera_capture",
            context={"stage": "replay_registration"},
            exc=error,
        )


def _shot_capture_in_progress() -> bool:
    """Whether a shot is being captured, enriched, or published, or a TI dump streams."""
    with _shot_finalization_condition:
        busy = (
            _shot_processing_active or bool(_shot_finalization_order) or _shot_finalization_running
        )
    return busy or _ti_dump_in_progress()


def _ti_dump_in_progress() -> bool:
    """Whether the TI radar is streaming a raw dump over its USB UART."""
    runtime = iwr6843_runtime
    return runtime is not None and bool(runtime.capture_monitor.dump_in_progress)


def _lower_camera_replay_worker_priority() -> None:
    """Renice the calling thread; FFmpeg children inherit its priority."""
    # Linux applies nice per thread. Elsewhere it would renice the whole server.
    if not sys.platform.startswith("linux"):
        return
    try:
        os.nice(_CAMERA_REPLAY_WORKER_NICE)
    except OSError as error:
        logger.warning("[CAMERA] Could not lower replay worker priority: %s", error)


def _queue_camera_replay_auto_prepare(shot: Shot) -> None:
    """Queue a published shot's replay for background MP4 preparation."""
    replay = shot.camera_replay
    if shot.demo or camera_capture_runtime is None or camera_replay_manager is None:
        return
    if not isinstance(replay, dict) or not isinstance(replay.get("id"), str):
        return
    try:
        _submit_camera_replay_prepare((shot.shot_number, replay["id"], None))
    except queue.Full:
        logger.warning(
            "[CAMERA] Replay preparation queue is full; skipping shot #%s", shot.shot_number
        )


def _submit_camera_replay_prepare(
    job: tuple[int | None, str, Future | None], timeout: float | None = None
) -> None:
    """Queue a replay job (waiting up to timeout for room) and ensure the worker runs."""
    global _camera_replay_auto_prepare_worker  # pylint: disable=global-statement

    if timeout is None:
        camera_replay_auto_prepare_queue.put_nowait(job)
    else:
        camera_replay_auto_prepare_queue.put(job, timeout=timeout)
    with _camera_replay_auto_prepare_lock:
        worker = _camera_replay_auto_prepare_worker
        if worker is None or not worker.is_alive():
            _camera_replay_auto_prepare_worker = threading.Thread(
                target=_camera_replay_auto_prepare_loop,
                name="camera-replay-prepare",
                daemon=True,
            )
            _camera_replay_auto_prepare_worker.start()


def _camera_replay_auto_prepare_loop() -> None:
    """Prepare queued replays one at a time whenever no shot is in flight."""
    _lower_camera_replay_worker_priority()
    while True:
        shot_number, replay_id, result = camera_replay_auto_prepare_queue.get()
        try:
            while _shot_capture_in_progress():
                time.sleep(_CAMERA_REPLAY_IDLE_POLL_S)
            if result is None:
                _auto_prepare_camera_replay(shot_number, replay_id)
            else:
                _prepare_requested_camera_replay(replay_id, result)
        finally:
            camera_replay_auto_prepare_queue.task_done()


def _prepare_requested_camera_replay(replay_id: str, result: Future) -> None:
    """Build or reuse the cached MP4 for an HTTP request waiting on result."""
    from .camera.replay import ReplayNotFoundError

    if not result.set_running_or_notify_cancel():
        return
    manager = camera_replay_manager
    try:
        if manager is None:
            raise ReplayNotFoundError("Camera replay was not found")
        result.set_result(manager.prepare(replay_id))
    except Exception as error:  # pylint: disable=broad-exception-caught
        result.set_exception(error)


def _auto_prepare_camera_replay(shot_number: int | None, replay_id: str) -> None:
    """Build or reuse the cached MP4, then tell clients it can be streamed."""
    from .camera.replay import ReplayNotFoundError

    manager = camera_replay_manager
    if manager is None:
        return
    try:
        manager.prepare(replay_id)
    except ReplayNotFoundError:
        logger.info("[CAMERA] Replay %s was removed before background preparation", replay_id)
        return
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[CAMERA] Background replay preparation failed for %s: %s", replay_id, error)
        log_session_error(
            "Camera replay auto-preparation failed",
            component="camera_capture",
            context={"replay_id": replay_id, "shot_number": shot_number},
            exc=error,
        )
        return
    socketio.emit(
        "camera_replay_ready",
        {
            "shot_number": shot_number,
            "replay_id": replay_id,
            "video_url": _camera_replay_video_url(replay_id),
        },
    )


def _set_calculated_spin_axis(shot: Shot, axis: float) -> None:
    """Make ``axis`` the shot's one spin axis: the axis it flies with and its curve class.

    From the same axis, so the curve tile and the drawn curve always agree. The only
    writer of a calculated ``spin_axis_deg``; the carry follows when the shot is flown
    (``_fly_shot``).
    """
    shot.spin_axis_deg = axis
    shot.spin_axis_source = "calculated"
    shot.curve_class = curve_class(axis)


def _camera_spin_axis(shot: Shot, shot_fields: dict) -> Optional[float]:
    """The camera D-plane axis of a shot's club data, when it may be the shot's axis.

    ``calculated_spin_axis_deg`` of ``club_data.calculate_club_data`` (``dplane_face``
    with the camera club path, the measured camera attack angle when accepted and the
    camera launch angle when there is one): set only for an iron with an accepted path
    and a camera start direction. The shot's own start direction must be the camera's
    too, the rule of ``_apply_dplane_spin_axis``. None otherwise.
    """
    axis = shot_fields.get("calculated_spin_axis_deg")
    if isinstance(axis, bool) or not isinstance(axis, (int, float)) or not math.isfinite(axis):
        return None
    if not (shot.launch_angle_horizontal_source or "").startswith("camera"):
        return None
    return float(axis)


def _apply_dplane_spin_axis(shot: Shot) -> None:
    """Set a calculated D-plane spin axis from start direction, club path and launch angle.

    The K-LD7 axis: only a build with a K-LD7 (the only source of ``club_path_deg``)
    gets one, and only as a fallback. The camera club data's axis, when it comes, wins
    and replaces it (``_publish_club_data``), so a shot never carries two axes.

    Runs after camera fusion, so the start direction is the camera's when fusion accepted
    it and the TI's otherwise. Both start direction and club path are experimental; only
    trust their difference once the weaker leg (start direction) clears a confidence
    floor, rather than emitting it the moment club path is non-null. The attack angle is
    the placeholder in dplane. Woods get no axis (dplane_spin_axis returns None).
    """
    try:
        if (
            shot.launch_angle_horizontal is None
            or shot.club_path_deg is None
            or shot.launch_angle_vertical is None
            or (shot.launch_angle_horizontal_confidence or 0.0) < SPIN_AXIS_MIN_CONFIDENCE
            # Only a camera start direction: the TI one (slope ~0.3 against Toptracer) would
            # draw a curve from noise, and SPIN_AXIS_OFFSET_DEG was fitted on camera shots.
            or not (shot.launch_angle_horizontal_source or "").startswith("camera")
        ):
            return
        axis = dplane_spin_axis(
            shot.club,
            shot.launch_angle_horizontal,
            shot.club_path_deg,
            shot.launch_angle_vertical,
        )
        if axis is None:
            return
        _set_calculated_spin_axis(shot, axis)
        face_weight = face_weight_for_club(shot.club)
        logger.info(
            "[SERVER] Spin axis: %+.1f° calculated (D-plane F2P=%+.1f°, spin loft=%.1f°, "
            "start direction %s)",
            axis,
            face_to_path_deg(shot.launch_angle_horizontal, shot.club_path_deg, face_weight),
            spin_loft_deg(shot.launch_angle_vertical, MEAN_ATTACK_ANGLE_DEG, face_weight),
            shot.launch_angle_horizontal_source,
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Spin-axis post-processing error: %s", e, exc_info=True)
        log_session_error(
            "Spin-axis post-processing failed",
            component="server",
            context={
                "stage": "spin_axis_postprocessing",
                "ball_speed_mph": shot.ball_speed_mph,
                "club": shot.club.value,
            },
            exc=e,
        )


def _enrich_shot_from_optional_hardware(shot: Shot) -> _ShotEnrichmentResult:
    """Mutate a shot with available radar/camera measurements and timings."""

    # Snapshot orientation before IWR capture can block, and select only data
    # timestamped before impact so impact vibration cannot bias the geometry.
    _snapshot_inclinometer_for_shot(shot)
    iwr6843_ms = _process_iwr6843_angle(shot)
    kld7_ms = None
    camera_capture_ms = None
    # Process K-LD7 angle radars (vertical = launch angle, horizontal = club path)
    try:
        if shot.mode != "mock":
            kld7_start = time.perf_counter()
            shot_ts = shot.impact_timestamp or time.time()
            session_log = get_session_logger()
            if kld7_vertical or kld7_horizontal:
                _maybe_wait_for_kld7_post_shot_frames(shot_ts)

            # --- Vertical K-LD7 (launch angle) ---
            if kld7_vertical:
                raw_buffer = kld7_vertical.snapshot_buffer()
                _warn_if_kld7_buffer_underfilled("vertical", len(raw_buffer))
                kld7_angle = kld7_vertical.get_angle_for_shot(
                    shot_timestamp=shot_ts,
                    ball_speed_mph=shot.ball_speed_mph,
                    impact_timestamp=shot.impact_timestamp_kld7,
                    club=shot.club,
                )
                vertical_selection_details = None
                if kld7_angle and kld7_angle.vertical_deg is not None:
                    accepted, vertical_selection_details = _select_vertical_radar_launch(
                        kld7_angle, shot
                    )
                    selection_reason = vertical_selection_details["selection_reason"]
                    if not accepted:
                        logger.warning(
                            "[SERVER] Vertical angle %.1f° rejected: %s "
                            "(expected=%s°, delta=%s°, conf=%.0f%%)",
                            kld7_angle.vertical_deg,
                            selection_reason,
                            vertical_selection_details.get("expected_launch_deg"),
                            vertical_selection_details.get("delta_deg"),
                            kld7_angle.confidence * 100,
                        )
                    else:
                        accepted_conf = kld7_angle.confidence
                        if vertical_selection_details.get("acceptance_path") == "marginal":
                            accepted_conf = min(
                                accepted_conf, _VERTICAL_MARGINAL_DISPLAY_CONFIDENCE
                            )
                        shot.launch_angle_vertical = kld7_angle.vertical_deg
                        shot.launch_angle_confidence = accepted_conf
                        shot.launch_angle_vertical_confidence = accepted_conf
                        shot.launch_angle_vertical_source = "radar"
                        shot.angle_source = "radar"
                        logger.info(
                            "[SERVER] Vertical angle: %.1f° (conf=%.0f%%, %d frames, %s)",
                            kld7_angle.vertical_deg,
                            kld7_angle.confidence * 100,
                            kld7_angle.num_frames,
                            selection_reason,
                        )
                # Club angle of attack (same RADC buffer, club speed from OPS).
                # Compute BEFORE logging the buffer so the log entry can
                # include club_angle alongside ball_angle for offline analysis.
                club_angle_v = None
                if shot.club_speed_mph:
                    club_angle_v = kld7_vertical.get_club_angle(
                        club_speed_mph=shot.club_speed_mph,
                        shot_timestamp=shot_ts,
                    )
                    if club_angle_v and club_angle_v.vertical_deg is not None:
                        # Negate: the radar sees where the club IS (above center = positive),
                        # but AoA is the club's attack direction (descending = negative).
                        candidate_aoa = -club_angle_v.vertical_deg
                        # Reject physically impossible AoA values.
                        # Real AoA ranges from ~-15° (steep iron) to ~+8° (ascending driver).
                        if -15.0 <= candidate_aoa <= 8.0:
                            shot.club_angle_deg = candidate_aoa
                            logger.info(
                                "[SERVER] Club AoA: %.1f° (conf=%.0f%%)",
                                shot.club_angle_deg,
                                club_angle_v.confidence * 100,
                            )
                        else:
                            logger.warning(
                                "[SERVER] Club AoA rejected: %.1f° outside plausible range",
                                candidate_aoa,
                            )

                if session_log and raw_buffer:
                    session_log.log_kld7_buffer(
                        shot_number=_shot_number_for_log(shot, session_log),
                        shot_timestamp=shot_ts,
                        orientation="vertical",
                        buffer_frames=raw_buffer,
                        ball_angle=_kld7_angle_log_payload(
                            kld7_angle,
                            "vertical_deg",
                            selection_details=vertical_selection_details,
                        ),
                        club_angle=_kld7_angle_log_payload(club_angle_v, "vertical_deg"),
                    )

                kld7_vertical.reset()

            # --- Horizontal K-LD7 (club path / aim direction) ---
            if kld7_horizontal:
                raw_buffer_h = kld7_horizontal.snapshot_buffer()
                _warn_if_kld7_buffer_underfilled("horizontal", len(raw_buffer_h))
                kld7_angle_h = kld7_horizontal.get_angle_for_shot(
                    shot_timestamp=shot_ts,
                    ball_speed_mph=shot.ball_speed_mph,
                )
                horizontal_selection_details = None
                if kld7_angle_h and kld7_angle_h.horizontal_deg is not None:
                    horizontal_limit = 15.0
                    accepted_h, horizontal_selection_details = _select_horizontal_radar_launch(
                        kld7_angle_h, horizontal_limit
                    )
                    selection_reason_h = horizontal_selection_details["selection_reason"]
                    if accepted_h:
                        shot.launch_angle_horizontal = kld7_angle_h.horizontal_deg
                        shot.launch_angle_horizontal_confidence = kld7_angle_h.confidence
                        shot.launch_angle_horizontal_source = "radar"
                        if shot.angle_source is None:
                            shot.angle_source = "radar"
                        if shot.launch_angle_confidence is None:
                            shot.launch_angle_confidence = kld7_angle_h.confidence
                        logger.info(
                            "[SERVER] Horizontal angle: %.1f° (conf=%.0f%%, %d frames, %s)",
                            kld7_angle_h.horizontal_deg,
                            kld7_angle_h.confidence * 100,
                            kld7_angle_h.num_frames,
                            selection_reason_h,
                        )
                    else:
                        logger.warning(
                            "[SERVER] Horizontal angle %.1f° rejected: %s "
                            "(limit=±%.0f°, conf=%.0f%%)",
                            kld7_angle_h.horizontal_deg,
                            selection_reason_h,
                            horizontal_limit,
                            kld7_angle_h.confidence * 100,
                        )
                # Club path (same RADC buffer, club speed from OPS).
                # Compute BEFORE logging the buffer so the log entry can
                # include club_angle alongside ball_angle for offline analysis.
                club_angle_h = None
                if shot.club_speed_mph:
                    club_angle_h = kld7_horizontal.get_club_angle(
                        club_speed_mph=shot.club_speed_mph,
                        shot_timestamp=shot_ts,
                    )
                    if club_angle_h and club_angle_h.horizontal_deg is not None:
                        shot.club_path_deg = club_angle_h.horizontal_deg
                        logger.info(
                            "[SERVER] Club path: %.1f° (conf=%.0f%%)",
                            club_angle_h.horizontal_deg,
                            club_angle_h.confidence * 100,
                        )

                if session_log and raw_buffer_h:
                    session_log.log_kld7_buffer(
                        shot_number=_shot_number_for_log(shot, session_log),
                        shot_timestamp=shot_ts,
                        orientation="horizontal",
                        buffer_frames=raw_buffer_h,
                        ball_angle=_kld7_angle_log_payload(
                            kld7_angle_h,
                            "horizontal_deg",
                            selection_details=horizontal_selection_details,
                        ),
                        club_angle=_kld7_angle_log_payload(club_angle_h, "horizontal_deg"),
                    )

                kld7_horizontal.reset()

            if kld7_vertical or kld7_horizontal:
                kld7_ms = (time.perf_counter() - kld7_start) * 1000
                logger.info("[SERVER] K-LD7 processing: %.1fms", kld7_ms)
    except Exception as e:
        logger.warning("[SERVER] Angle/spin-axis post-processing error: %s", e, exc_info=True)
        log_session_error(
            "Angle/spin-axis post-processing failed",
            component="server",
            context={
                "stage": "angle_postprocessing",
                "ball_speed_mph": shot.ball_speed_mph,
                "club": shot.club.value,
            },
            exc=e,
        )

    camera_capture = None
    try:
        if camera_capture_runtime is not None and shot.mode != "mock":
            camera_capture_start = time.perf_counter()
            camera_capture = camera_capture_runtime.capture_for_shot(
                shot.impact_timestamp,
                timeout_s=4.0,
            )
            camera_capture_ms = (time.perf_counter() - camera_capture_start) * 1000.0
            if not shot.demo:
                # Only clips of OPS-accepted shots teach the camera gate's tee anchor.
                camera_capture_runtime.note_accepted_shot(
                    camera_capture, shot_number=shot.shot_number
                )
            session_log = get_session_logger()
            if session_log:
                shot_number = _shot_number_for_log(shot, session_log)
                if camera_capture is not None:
                    session_log.log_camera_capture(
                        shot_number=shot_number,
                        shot_timestamp=shot.impact_timestamp,
                        trigger_timestamp=camera_capture.trigger_timestamp,
                        capture_path=str(camera_capture.path) if camera_capture.path else None,
                        metadata=camera_capture.metadata,
                        capture_error=camera_capture.error,
                        edge_fields=sound_edge_timing.fields_for(camera_capture.trigger_timestamp),
                    )
                    if camera_capture.valid:
                        logger.info(
                            "[SERVER] Camera capture #%d matched -> %s",
                            camera_capture.sequence,
                            camera_capture.path,
                        )
                    else:
                        logger.warning(
                            "[SERVER] Camera capture #%d failed: %s",
                            camera_capture.sequence,
                            camera_capture.error,
                        )
                else:
                    session_log.log_camera_capture(
                        shot_number=shot_number,
                        shot_timestamp=shot.impact_timestamp,
                        trigger_timestamp=None,
                        capture_path=None,
                        capture_error="no_matching_camera_capture",
                    )
                    logger.warning("[SERVER] No camera capture matched this shot")
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Camera capture matching error: %s", error, exc_info=True)
        log_session_error(
            "Camera capture matching failed",
            component="camera_capture",
            context={"stage": "camera_capture_match", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )

    _attach_camera_replay(shot, camera_capture)

    club_head_path_job = None
    lowlight_job = None
    if shot.mode != "mock":
        club_head_path_job = _fuse_camera_measurements(
            shot,
            camera_capture,
            deadline_monotonic=_shot_enrichment_deadline(shot.shot_number),
        )
        lowlight_job = _optional_lowlight_shadow_job(shot, camera_capture)
        # After camera fusion: the camera vertical wins, a TI-only shot gets the
        # session's camera-referenced offset once it is valid.
        _apply_angle_reference(shot)
        # After camera fusion, so the D-plane sees the fused start direction.
        _apply_dplane_spin_axis(shot)
        if isinstance(club_head_path_job, _ClubHeadPathShadowJob):
            # The club data's D-plane face uses the fused start direction too.
            club_head_path_job = replace(
                club_head_path_job,
                launch_direction_deg=shot.launch_angle_horizontal,
                launch_direction_status=shot.experimental_camera_horizontal_status,
                launch_vertical_deg=shot.launch_angle_vertical,
            )

    return _ShotEnrichmentResult(
        iwr6843_ms=iwr6843_ms,
        kld7_ms=kld7_ms,
        camera_capture_ms=camera_capture_ms,
        camera_club_head_path_job=club_head_path_job,
        camera_lowlight_job=lowlight_job,
    )


def _shot_log_level(shot: Shot) -> int:
    """INFO for a real shot; a demo shot logs at DEBUG, so the INFO log stays about real shots."""
    return logging.DEBUG if shot.demo else logging.INFO


def _roll_out(trajectory: Trajectory) -> tuple[dict, float]:
    """The roll path and the Total in yards, rolled out from the flight's touchdown.

    Raises OutOfRangeError for a touchdown outside the roll model's range and ValueError
    for every other failure, a flight without touchdown state or roll path included.
    """
    touchdown = touchdown_of(trajectory)
    if touchdown is None:
        raise ValueError("the flight has no touchdown state")
    rollout = simulate_rollout(touchdown)
    path = roll_path(trajectory, rollout)
    if path is None:
        raise ValueError("the roll-out gives no roll path")
    return path, math.hypot(rollout.rest_x_m, rollout.rest_y_m) * M_TO_YD


def _clear_roll(shot: Shot) -> None:
    """No roll: roll_path, total_yards and roll_yards are None."""
    shot.roll_path = shot.total_yards = shot.roll_yards = None


def _attach_roll(shot: Shot, trajectory: Trajectory) -> None:
    """Set roll_path, total_yards and roll_yards from the flight behind the carry.

    All three are None for a shot without a flight_path and when the roll-out fails. The
    roll is optional: no error in it, expected or not, may cost the shot its carry, its log
    entry or its emit. A touchdown outside the model's range, above all one faster than
    40 m/s, is a documented limit, not a failure: it logs "No roll" at the shot's log level,
    not a warning.
    """
    _clear_roll(shot)
    if shot.flight_path is None:
        return
    try:
        path, total_yards = _roll_out(trajectory)
    except OutOfRangeError as exc:
        logger.log(_shot_log_level(shot), "[SERVER] No roll: %s", exc)
        return
    except ValueError as exc:
        logger.warning("[SERVER] Roll-out failed: %s", exc)
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Roll-out failed: %s", exc, exc_info=True)
        return
    shot.roll_path = path
    shot.total_yards = total_yards
    shot.roll_yards = total_yards - trajectory.carry_yards


def _finalize_shot_metrics(shot: Shot) -> Optional[_CarryFlight]:
    """Complete a shot's launch angles, ball speed, spin, carry, flight path and roll in place.

    Nothing is written to the session log, emitted or forwarded, so this also finalizes
    shots that are not published. Returns the launch conditions and the air density the
    ballistic carry was flown with (None for a table carry), the input of the carry span.
    """
    # Always provide user-facing launch angles. Radar/camera measurements win;
    # rejected or missing axes fall back to conservative estimates.
    _ensure_user_facing_launch_angles(shot)

    # Ball-speed cosine correction: the OPS reads the radial component of
    # a ball departing at the launch angle. Applied AFTER the K-LD7 (which
    # must anchor to the radial speed) and BEFORE carry/ballistics.
    if ball_speed_correction_enabled and not shot.demo and shot.launch_angle_vertical is not None:
        raw_speed = shot.ball_speed_mph
        shot.ball_speed_raw_mph = raw_speed
        shot.ball_speed_mph = correct_ball_speed(
            raw_speed,
            shot.launch_angle_vertical,
            ball_speed_correction_distance_ft,
            ball_speed_correction_ball_above_radar_ft,
        )
        logger.info(
            "[SERVER] Ball speed cosine correction: %.1f -> %.1f mph (LA %.1f)",
            raw_speed,
            shot.ball_speed_mph,
            shot.launch_angle_vertical,
        )

    # Calculated spin runs AFTER the cosine correction (the model is
    # calibrated on true ball speed) and BEFORE carry/ballistics.
    if calculated_spin_enabled:
        _apply_calculated_spin(shot)

    return _fly_shot(shot)


def _fly_shot(shot: Shot) -> Optional[_CarryFlight]:
    """Fly a shot's carry, flight path and roll from its finished launch values, in place.

    The carry part of ``_finalize_shot_metrics``, also run again when a calculated spin
    axis reaches a shot after its publication (``_publish_club_data``): the same launch
    values give the same flight, so a re-flown shot equals one finalized with that axis.
    Returns what ``_finalize_shot_metrics`` returns.
    """
    # The span belongs to the carry computed below; it is attached afterwards.
    shot.carry_span_min_yards = shot.carry_span_max_yards = None
    # Compute carry. Prefer the physics simulator (drag + Magnus, RK4) when
    # ballistics is enabled and a vertical launch angle is available; fall
    # back to the table estimator otherwise (either ballistics disabled or
    # angle missing → resolve_launch returns None). Simulated (mock) shots take
    # the same path so they fly the same physics. This is the only place that
    # writes carry_spin_adjusted, flight_path, apex_yards and the roll.
    # The spin table is the shot profile's ball type, fixed by the shot's first flight:
    # a re-flown shot keeps it even if the profile's ball type changed in between.
    # The shot names it only when a club-typical spin from it was flown; a trusted
    # measured spin leaves it unused (None). A re-flight keeps the spin and its
    # confidence (only the spin axis changes), so it uses a table exactly when the
    # first flight did.
    table = (
        (shot.typical_spin_table or _shot_typical_spin_table(shot)) if ballistics_enabled else None
    )
    conditions = (
        resolve_launch(shot, typical_spin_scale=typical_spin_scale, typical_spin_table=table)
        if table is not None
        else None
    )
    shot.typical_spin_table = (
        table if conditions is not None and conditions.spin_source == "club_typical" else None
    )
    flight: Optional[_CarryFlight] = None
    if conditions is not None:
        density = _air_density_for_shot()
        flight = _CarryFlight(conditions, density.kg_m3, range_ball_drag_slope)
        trajectory = simulate(
            conditions, air_density=density.kg_m3, range_ball_drag_slope=range_ball_drag_slope
        )
        shot.carry_air_density_kg_m3 = round(density.kg_m3, 4)
        shot.carry_air_density_source = density.source
        shot.carry_weather_age_s = (
            None if density.weather_age_s is None else round(density.weather_age_s, 1)
        )
        shot.carry_spin_adjusted = trajectory.carry_yards
        # The simulator export keeps the fixed density: only the live carry follows the weather.
        # Flying it a second time only pays off when a simulator can receive it; otherwise
        # _forward_shot_to_simulators fills it in if a simulator connects in the meantime.
        if density.kg_m3 == air_density_kg_m3:
            shot.carry_standard_air_yards = trajectory.carry_yards
        elif any(c.is_connected() for c in sim_connectors):
            shot.carry_standard_air_yards = simulate(
                conditions,
                air_density=air_density_kg_m3,
                range_ball_drag_slope=range_ball_drag_slope,
            ).carry_yards
        else:
            shot.carry_standard_air_yards = None
        # resolve_launch flies an unknown direction straight; that is not an offset.
        shot.carry_lateral_yards = (
            trajectory.lateral_yards if shot.launch_angle_horizontal is not None else None
        )
        # The drawn flight is the simulation behind the carry, so it ends on it.
        shot.flight_path = flight_path(trajectory)
        shot.apex_yards = trajectory.apex_yards
        shot.carry_model = "ballistic"
        shot.carry_spin_rpm = conditions.spin_rpm
        shot.carry_spin_source = (
            "calculated"
            if conditions.spin_source == "measured" and shot.spin_source == "calculated"
            else conditions.spin_source
        )
        logger.log(
            _shot_log_level(shot),
            "[SERVER] Ballistic carry: %.0f yds (spin: %.0f rpm, source: %s; air %.3f kg/m³, %s)",
            shot.carry_spin_adjusted,
            conditions.spin_rpm,
            conditions.spin_source,
            density.kg_m3,
            density.source,
        )
        _attach_roll(shot, trajectory)
    else:
        has_reliable_spin = (
            shot.spin_rpm
            and shot.spin_rpm > 0
            and shot.spin_confidence is not None
            and shot.spin_confidence >= SPIN_CONFIDENCE_RELIABLE
        )
        spin_for_carry = (
            shot.spin_rpm
            if has_reliable_spin
            else get_optimal_spin_for_ball_speed(shot.ball_speed_mph, shot.club)
        )
        shot.carry_spin_adjusted = estimate_carry_with_spin(
            shot.ball_speed_mph,
            spin_for_carry,
            shot.club,
            club_speed_mph=shot.club_speed_mph,
        )
        shot.carry_model = "table"
        # The table carry has no air density in it.
        shot.carry_air_density_kg_m3 = None
        shot.carry_air_density_source = None
        shot.carry_weather_age_s = None
        shot.carry_standard_air_yards = None
        shot.flight_path = None  # a table carry has no simulated flight to draw
        shot.apex_yards = None  # nor a peak height
        _clear_roll(shot)
        shot.carry_spin_rpm = spin_for_carry
        shot.carry_spin_source = (
            ("calculated" if shot.spin_source == "calculated" else "measured")
            if has_reliable_spin
            else "speed_model"
        )
        reason = "ballistics disabled" if not ballistics_enabled else "no launch angle"
        logger.log(
            _shot_log_level(shot),
            "[SERVER] Table carry (%s): %.0f yds (spin: %.0f rpm%s)",
            reason,
            shot.carry_spin_adjusted,
            spin_for_carry,
            "" if shot.spin_rpm and shot.spin_rpm > 0 else " avg",
        )

    return flight


# One worker for the carry span: four extra flights take about 35-50 ms on a Mac and
# several times that on the Pi, too slow for the shot path. The span is flown after the
# shot event and reaches the screens as a shot_update.
_carry_span_executor: ThreadPoolExecutor | None = None
_carry_span_executor_lock = threading.Lock()


def _carry_span_worker() -> ThreadPoolExecutor:
    """The single carry-span worker, started on first use."""
    global _carry_span_executor  # pylint: disable=global-statement

    with _carry_span_executor_lock:
        if _carry_span_executor is None:
            _carry_span_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="carry-span"
            )
        return _carry_span_executor


def _submit_carry_span(job) -> None:
    """Run a carry-span job on the span worker, in submission order."""
    _carry_span_worker().submit(job)


def _attach_carry_span(
    shot: Shot,
    flight: Optional[_CarryFlight],
    carry: Optional[float],
) -> bool:
    """Set the shot's carry span from the conditions and air its carry was flown with.

    Only a club_typical ballistic carry gets a span (ballistics.carry_span_yards). It
    flies at the carry's own density (the weather's while fresh), so it contains that
    carry. `carry` is the carry that flight gave; a shot whose carry is no longer that one
    (finalized again meanwhile) keeps no span. Returns whether it was set. Never raises:
    the span is optional, a failure costs only the span.
    """
    if flight is None or flight.conditions.spin_source != "club_typical" or carry is None:
        return False
    try:
        span = carry_span_yards(
            flight.conditions,
            air_density=flight.air_density_kg_m3,
            full_spin_carry_yards=carry,
            range_ball_drag_slope=flight.range_ball_drag_slope,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Carry span failed: %s", exc, exc_info=True)
        return False
    if span is None or shot.carry_spin_adjusted != carry:
        return False
    shot.carry_span_min_yards, shot.carry_span_max_yards = span
    return True


def _start_carry_span(shot: Shot, flight: Optional[_CarryFlight]) -> None:
    """Fly a published live shot's carry span in the background, then send a shot_update."""
    if shot.demo or flight is None or flight.conditions.spin_source != "club_typical":
        return
    carry = shot.carry_spin_adjusted

    def job() -> None:
        started = time.perf_counter()
        if not _attach_carry_span(shot, flight, carry):
            return
        logger.log(
            _shot_log_level(shot),
            "[SERVER] Carry span (typical spin x0.5-1.0): %.0f-%.0f yds in %.0f ms",
            shot.carry_span_min_yards,
            shot.carry_span_max_yards,
            (time.perf_counter() - started) * 1000.0,
        )
        try:
            _emit_real_shot_event(
                "shot_update",
                {
                    "shot": shot_to_dict(shot),
                    "stats": monitor.get_session_stats() if monitor else {},
                },
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[SERVER] Failed to emit the carry span: %s", exc, exc_info=True)

    try:
        _submit_carry_span(job)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Carry span not started: %s", exc)


def _submit_carry_variants(job) -> None:
    """Run a carry-variants job on the span worker, after the spans submitted before it."""
    _carry_span_worker().submit(job)


def _start_carry_variants(shot: Shot, flight: Optional[_CarryFlight]) -> None:
    """Fly a published live shot's carry variants in the background: session log only.

    ``ballistics.carry_variants`` flies the shot's launch with each spin table and
    range-ball drag slope of ``CARRY_VARIANTS`` at the carry's own density. Nothing
    reaches the shot, the screens or the simulators: the result is one ``carry_variants``
    session-log entry, ``null`` with a ``reason`` when the flights fail. Runs on the
    carry-span worker after the shot event, so it never delays the shot or its span.
    Skipped for demo shots, table carries and without a session log; an entry whose
    session ended or changed meanwhile is dropped. Never raises.
    """
    if shot.demo or flight is None:
        return
    session_log = get_session_logger()
    if session_log is None or not getattr(session_log, "enabled", True):
        return
    session_id = getattr(session_log, "session_id", None)
    shot_number, shot_timestamp, club = shot.shot_number, shot.impact_timestamp, shot.club

    def write(variants: Optional[dict], reason: Optional[str], elapsed_ms: Optional[float]):
        if getattr(session_log, "session_id", None) != session_id:
            return
        session_log.log_carry_variants(
            shot_number=shot_number,
            shot_timestamp=shot_timestamp,
            variants=variants,
            reason=reason,
            air_density_kg_m3=round(flight.air_density_kg_m3, 4),
            elapsed_ms=elapsed_ms,
        )

    def job() -> None:
        started = time.perf_counter()
        variants, reason = None, None
        try:
            variants = carry_variants(flight.conditions, club, flight.air_density_kg_m3)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[SERVER] Carry variants failed: %s", exc, exc_info=True)
            reason = f"failed: {type(exc).__name__}: {exc}"
        try:
            write(variants, reason, round((time.perf_counter() - started) * 1000.0, 1))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("[SERVER] Carry variants not logged: %s", exc, exc_info=True)

    try:
        _submit_carry_variants(job)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Carry variants not started: %s", exc)
        try:
            write(None, f"not_started: {type(exc).__name__}: {exc}", None)
        except Exception as log_exc:  # pylint: disable=broad-exception-caught
            logger.warning("[SERVER] Carry variants not logged: %s", log_exc)


def _finalize_demo_shot_metrics(shot: Shot) -> None:
    """Finalize a demo shot, span included: it becomes visible only once finalized."""
    flight = _finalize_shot_metrics(shot)
    _attach_carry_span(shot, flight, shot.carry_spin_adjusted)


# OPS spin median of the earlier shots per club, for the session log only.
ops_spin_session_median = OpsSpinSessionMedian()


def _finalize_shot_detected(
    shot: Shot,
    *,
    emit_event: str,
    initial_ui_ms: float | None = None,
    enrichment: _ShotEnrichmentResult | None = None,
) -> None:
    """Apply required fallbacks, persist once, and publish the final shot."""
    enrichment = enrichment or _ShotEnrichmentResult()
    iwr6843_ms = enrichment.iwr6843_ms
    kld7_ms = enrichment.kld7_ms
    camera_capture_ms = enrichment.camera_capture_ms

    carry_flight = _finalize_shot_metrics(shot)
    log_only_fields = (
        None if shot.demo else ops_spin_median_log_fields(*ops_spin_session_median.observe(shot))
    )

    if shot.spin_rejection_reason:
        logger.info(
            "[SERVER] Spin unavailable: %s (snr=%s, candidate=%s rpm)",
            shot.spin_rejection_reason,
            "%.2f" % shot.spin_snr if shot.spin_snr is not None else "N/A",
            "%.0f" % (shot.spin_peak_freq_hz * 60) if shot.spin_peak_freq_hz is not None else "N/A",
        )

    # Log shot with all data (radar + spin + camera) in one entry
    try:
        session_log = get_session_logger()
        if session_log:
            session_log.log_shot(
                shot=shot,
                pipeline_ms={
                    "initial_ui": (round(initial_ui_ms, 1) if initial_ui_ms is not None else None),
                    "iwr6843": (round(iwr6843_ms, 1) if iwr6843_ms is not None else None),
                    "kld7": round(kld7_ms, 1) if kld7_ms is not None else None,
                    "camera_capture": (
                        round(camera_capture_ms, 1) if camera_capture_ms is not None else None
                    ),
                },
                log_only_fields=log_only_fields,
            )
    except Exception as e:
        logger.warning("[SERVER] Failed to log shot: %s", e, exc_info=True)
        log_session_error(
            "Session shot logging failed",
            component="server",
            context={"stage": "session_log_shot", "ball_speed_mph": shot.ball_speed_mph},
            exc=e,
        )

    # Emit shot with launch angle data included
    try:
        shot_data = shot_to_dict(shot)
        stats = monitor.get_session_stats() if monitor else {}
        _emit_real_shot_event(emit_event, {"shot": shot_data, "stats": stats})

        # Log shot info
        angle_str = ""
        if shot.launch_angle_vertical is not None:
            angle_str = ", Launch: %.1f°" % shot.launch_angle_vertical
        logger.log(
            _shot_log_level(shot),
            "[SERVER] Shot: ball=%.1f mph, carry=%.0f yds%s",
            shot.ball_speed_mph,
            shot.estimated_carry_yards,
            angle_str,
        )
    except Exception as e:
        logger.error("[SERVER] Failed to emit shot: %s", e, exc_info=True)
        log_session_error(
            "WebSocket shot emit failed",
            component="server",
            context={"stage": f"emit_{emit_event}", "ball_speed_mph": shot.ball_speed_mph},
            exc=e,
        )
        return

    _queue_camera_replay_auto_prepare(shot)

    # Forward to simulator connectors (optional)
    _forward_shot_to_simulators(shot)

    # Debug logging (optional)
    if debug_mode and not shot.demo:
        try:
            debug_log_entry = {
                "type": "shot",
                "timestamp": datetime.now().isoformat(),
                "radar": {
                    "ball_speed_mph": shot_data["ball_speed_mph"],
                    "club_speed_mph": shot_data["club_speed_mph"],
                    "smash_factor": shot_data["smash_factor"],
                    "peak_magnitude": shot_data["peak_magnitude"],
                },
                "club": shot_data["club"],
            }

            if debug_log_file:
                debug_log_file.write(json.dumps(debug_log_entry) + "\n")
                debug_log_file.flush()

            socketio.emit("debug_shot", debug_log_entry)
        except Exception as e:
            print(f"[WARN] Debug logging error: {e}")

    _start_carry_span(shot, carry_flight)
    _start_carry_variants(shot, carry_flight)


def _finish_shot_detected(
    shot: Shot,
    *,
    emit_event: str,
    initial_ui_ms: float | None = None,
) -> None:
    """Attempt optional enrichment, then always perform required finalization."""
    # Optional hardware may return after the coordinator has timed out this
    # shot. Mutate a shallow dataclass copy so a late result cannot change the
    # already-finalized OPS object retained by the monitor.
    enriched_shot = replace(shot) if _has_slow_shot_enrichment(shot) else shot
    enrichment = _ShotEnrichmentResult()
    try:
        enrichment = _enrich_shot_from_optional_hardware(enriched_shot)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("[SERVER] Deferred shot enrichment failed: %s", error, exc_info=True)
        log_session_error(
            "Deferred shot enrichment failed",
            component="server",
            context={"stage": "deferred_enrichment", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )

    shadow_job = enrichment.camera_club_head_path_job
    if (
        club_data_mode == "show"
        and isinstance(shadow_job, _ClubHeadPathShadowJob)
        and shadow_job.skip_reason is None
    ):
        # The screens show the club data as coming; the club-data job ends it.
        enriched_shot.club_data_status = "pending"
    lowlight_job = enrichment.camera_lowlight_job
    enrichment_on_time = _queue_shot_finalization(
        shot,
        emit_event=emit_event,
        initial_ui_ms=initial_ui_ms,
        enrichment=replace(enrichment, camera_club_head_path_job=None, camera_lowlight_job=None),
        final_shot=enriched_shot,
    )
    # After the hand-off, so it never delays this shot's result; it skips itself
    # when the next shot waits or this one was late, and a result it gets after
    # the shot was published goes out as one more shot_update.
    if shadow_job is not None:
        _run_camera_club_head_path_shadow(
            shadow_job,
            enrichment_on_time=bool(enrichment_on_time),
            source_shot=shot,
            final_shot=enriched_shot,
        )
    # Log only, after the club data and under the same rules (camera.lowlight).
    if isinstance(lowlight_job, _LowLightShadowJob):
        _run_camera_lowlight_shadow(lowlight_job, enrichment_on_time=bool(enrichment_on_time))


def _queue_shot_finalization(
    shot: Shot,
    *,
    emit_event: str,
    initial_ui_ms: float | None,
    enrichment: _ShotEnrichmentResult | None = None,
    final_shot: Shot | None = None,
) -> bool:
    """Record a ready shot for the exclusive finalization worker.

    Returns False when the shot was finalized without it already (past its
    deadline or over capacity) or is already waiting.
    """
    return _queue_ordered_shot_finalization(
        _PendingShotFinalization(
            shot=final_shot if final_shot is not None else shot,
            emit_event=emit_event,
            initial_ui_ms=initial_ui_ms,
            enrichment=(enrichment if enrichment is not None else _ShotEnrichmentResult()),
        ),
        source_shot=shot,
    )


def _queue_ordered_shot_finalization(
    pending: _PendingShotFinalization,
    *,
    source_shot: Shot,
) -> bool:
    """Make a completed shot visible to the ordered finalization worker.

    Returns whether it did; False for a late enrichment of a finalized shot.
    """

    shot_number = pending.shot.shot_number
    if shot_number is None:
        raise ValueError("shot must have a stable number before finalization")

    with _shot_finalization_condition:
        registered = _shot_finalization_registered.get(shot_number)
        if registered is None or registered.shot is not source_shot:
            logger.info(
                "[SERVER] Ignoring late enrichment for finalized shot #%d",
                shot_number,
            )
            return False
        if shot_number in _shot_finalization_ready:
            logger.warning("[SERVER] Shot #%d is already waiting for finalization", shot_number)
            return False
        _shot_finalization_ready[shot_number] = pending
        _ensure_shot_finalization_worker_locked()
        _shot_finalization_condition.notify_all()
        return True


def _emit_initial_ops_shot(shot: Shot) -> bool:
    """Publish immediately available OPS metrics before slow enrichments."""
    try:
        shot_data = shot_to_dict(shot)
        stats = monitor.get_session_stats() if monitor else {}
        pending = {}
        if iwr6843_runtime is not None:
            pending["iwr6843"] = True
        if camera_capture_runtime is not None:
            pending["camera"] = True
        _emit_real_shot_event(
            "shot",
            {
                "shot": shot_data,
                "stats": stats,
                "pending": pending,
            },
        )
        # True also when the demo held the emit back: the row comes back with the real
        # session, so the final event must update it, not append a second one.
        return True
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.error("[SERVER] Failed to emit initial OPS shot: %s", error, exc_info=True)
        log_session_error(
            "Initial WebSocket shot emit failed",
            component="server",
            context={"stage": "emit_initial_shot", "ball_speed_mph": shot.ball_speed_mph},
            exc=error,
        )
        return False


def _emit_ops_enrichment_skipped(shot: Shot, *, reason: str) -> None:
    """Immediately clear provisional hardware progress when admission fails."""
    skipped_hardware = []
    if iwr6843_runtime is not None:
        skipped_hardware.append("iwr6843")
    if camera_capture_runtime is not None:
        skipped_hardware.append("camera")
    try:
        _emit_real_shot_event(
            "shot_update",
            {
                "shot": shot_to_dict(shot),
                "stats": monitor.get_session_stats() if monitor else {},
                "pending": {},
                "enrichment": {
                    "status": "skipped",
                    "reason": reason,
                    "hardware": skipped_hardware,
                },
            },
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[SERVER] Failed to clear skipped shot enrichment status: %s",
            error,
            exc_info=True,
        )


def _has_slow_shot_enrichment(shot: Shot) -> bool:
    """Whether optional hardware can add seconds to this shot callback."""
    return shot.mode != "mock" and (
        iwr6843_runtime is not None or camera_capture_runtime is not None
    )


def _drain_shot_enrichment_queue() -> None:
    """Finish deferred shots in detection order, one hardware consumer at a time."""
    global shot_enrichment_task  # pylint: disable=global-statement

    while True:
        try:
            shot, emit_event, initial_ui_ms = shot_enrichment_queue.get_nowait()
        except queue.Empty:
            with shot_enrichment_task_lock:
                if shot_enrichment_queue.empty():
                    shot_enrichment_task = None
                    return
            continue

        try:
            _finish_shot_detected(
                shot,
                emit_event=emit_event,
                initial_ui_ms=initial_ui_ms,
            )
        except Exception as error:  # pylint: disable=broad-exception-caught
            logger.error("[SERVER] Deferred shot finalization failed: %s", error, exc_info=True)
            log_session_error(
                "Deferred shot finalization failed",
                component="server",
                context={"stage": "deferred_finalization", "ball_speed_mph": shot.ball_speed_mph},
                exc=error,
            )
        finally:
            shot_enrichment_queue.task_done()


def _defer_shot_enrichment(
    shot: Shot,
    *,
    emit_event: str,
    initial_ui_ms: float | None,
) -> None:
    """Queue optional hardware work without blocking the OPS capture callback."""
    global shot_enrichment_task  # pylint: disable=global-statement

    with shot_enrichment_task_lock:
        shot_enrichment_queue.put_nowait((shot, emit_event, initial_ui_ms))
        if shot_enrichment_task is not None:
            return
        try:
            # Reserve the slot while the task starts. The worker needs the same
            # lock before clearing it, which closes the fast-finish race.
            shot_enrichment_task = True
            task = socketio.start_background_task(_drain_shot_enrichment_queue)
            shot_enrichment_task = task
        except Exception:
            shot_enrichment_task = None
            queued_shot, _event, _latency = shot_enrichment_queue.get_nowait()
            shot_enrichment_queue.task_done()
            if queued_shot is not shot:
                raise RuntimeError("shot enrichment queue lost FIFO ordering") from None
            raise


def on_shot_detected(shot: Shot) -> None:
    """Serialize detection order before publishing or queueing a shot."""
    with _shot_callback_lock:
        _handle_shot_detected(shot)


def _handle_shot_detected(shot: Shot) -> None:
    """Publish OPS metrics promptly, then enrich optional hardware data.

    While the demo runs, a real shot is dropped instead, also from the monitor's session.
    """
    with _demo_lock:
        if demo_session.active:
            _delete_session_row(shot.timestamp.isoformat())
            _clear_shot_processing()
            logger.info("[DEMO] real shot ignored (%.1f mph)", shot.ball_speed_mph)
            return
    _assign_shot_number(shot)
    active_profile = get_profile_store().get_active()
    shot.profile_id = active_profile.id
    shot.profile_name = active_profile.name
    logger.info("[SERVER] Shot callback: %.1f mph", shot.ball_speed_mph)

    if not _has_slow_shot_enrichment(shot):
        _register_shot_for_finalization(
            shot,
            emit_event="shot",
            initial_ui_ms=None,
        )
        _finish_shot_detected(shot, emit_event="shot")
        return

    emitted = _emit_initial_ops_shot(shot)
    initial_ui_ms = None
    if emitted and shot.impact_timestamp is not None:
        initial_ui_ms = max(0.0, (time.time() - shot.impact_timestamp) * 1000.0)
        logger.info(
            "[SERVER] Initial OPS metrics emitted %.0fms after impact; "
            "hardware enrichment continues in background",
            initial_ui_ms,
        )
    final_event = "shot_update" if emitted else "shot"
    _register_shot_for_finalization(
        shot,
        emit_event=final_event,
        initial_ui_ms=initial_ui_ms,
        needs_watchdog=True,
    )
    try:
        _defer_shot_enrichment(
            shot,
            emit_event=final_event,
            initial_ui_ms=initial_ui_ms,
        )
    except queue.Full:
        logger.warning(
            "[SERVER] Shot enrichment queue is full (%d waiting); "
            "skipping optional hardware for shot #%d",
            _SHOT_ENRICHMENT_QUEUE_CAPACITY,
            shot.shot_number,
        )
        if emitted:
            _emit_ops_enrichment_skipped(shot, reason="queue_full")
        _queue_shot_finalization(
            shot,
            emit_event=final_event,
            initial_ui_ms=initial_ui_ms,
        )
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning(
            "[SERVER] Could not defer shot enrichment: %s",
            error,
            exc_info=True,
        )
        if emitted:
            _emit_ops_enrichment_skipped(shot, reason="worker_unavailable")
        _queue_shot_finalization(
            shot,
            emit_event=final_event,
            initial_ui_ms=initial_ui_ms,
        )


demo_session = DemoSession(rng=random.Random(), finalize=_finalize_demo_shot_metrics)
# Demo-dependent emits are decided and sent under this lock, in step with the switch,
# so no screen gets a real shot in the demo or a demo shot in the real session.
_demo_lock = threading.RLock()
_DEMO_TICK_S = 1.0
_demo_ticker: threading.Thread | None = None


def _emit_real_shot_event(event: str, payload: dict) -> None:
    """Emit a real shot or its progress; while the demo runs, the screens show only the demo."""
    with _demo_lock:
        if not demo_session.active:
            socketio.emit(event, payload)


def _in_swing_speed_mode() -> bool:
    """Whether the monitor counts club-only swing-speed reps instead of ball shots."""
    from .swing_speed import SwingSpeedMonitor  # pylint: disable=import-outside-toplevel

    return isinstance(monitor, (SwingSpeedMonitor, MockSwingSpeedMonitor))


def _demo_state_payload() -> dict:
    """The demo_state event. The demo shows ball shots, so swing-speed mode has none."""
    if _in_swing_speed_mode():
        return {"active": False, "unavailable": "swing-speed"}
    return {"active": demo_session.active}


def _announce_demo(*, swapped: bool) -> None:
    """Tell every client whether the demo runs and, after a swap, which session to show."""
    socketio.emit("demo_state", _demo_state_payload())
    if swapped:
        socketio.emit("session_state", _session_state_payload())


def _start_demo() -> None:
    """Start the demo on the active profile's bag; every client swaps to the demo session."""
    profile = get_profile_store().get_active()
    with _demo_lock:
        starters = []
        if not _in_swing_speed_mode():
            starters = demo_session.start(
                now_s=time.monotonic(),
                wall=datetime.now(),
                bag_ids=profile.settings.get("club_ids"),
                profile_id=profile.id,
                profile_name=profile.name,
            )
        _announce_demo(swapped=bool(starters))
        if starters:
            _ensure_demo_ticker()
    if starters:
        logger.info("[DEMO] started: %s", ", ".join(shot.club.value for shot in starters))


def _end_demo() -> None:
    """End the demo, by the switch or the idle timeout; every client gets the real session."""
    with _demo_lock:
        stopped = demo_session.stop()
        _announce_demo(swapped=stopped)
    if stopped:
        logger.info("[DEMO] ended")


def _ensure_demo_ticker() -> None:
    """Start the ticker unless it runs. The caller holds _demo_lock."""
    global _demo_ticker  # pylint: disable=global-statement

    if _demo_ticker is None or not _demo_ticker.is_alive():
        _demo_ticker = threading.Thread(target=_run_demo_ticker, name="demo-ticker", daemon=True)
        _demo_ticker.start()


def _run_demo_ticker() -> None:
    """Tick every _DEMO_TICK_S while the demo runs; the thread ends with the demo."""
    global _demo_ticker  # pylint: disable=global-statement

    while True:
        time.sleep(_DEMO_TICK_S)
        with _demo_lock:
            if not demo_session.active:
                _demo_ticker = None
                return
            try:
                _demo_tick(time.monotonic())
            except Exception as error:  # pylint: disable=broad-exception-caught
                logger.error("[DEMO] Tick failed: %s", error, exc_info=True)


def _demo_tick(now_s: float) -> None:
    """End an idle demo the way the switch does, else publish the next shot once it is due.

    The caller holds _demo_lock, which _end_demo takes as well, so no demo shot can be
    emitted after the screens got the real session back.
    """
    if demo_session.expired(now_s):
        logger.info("[DEMO] idle timeout")
        _end_demo()
        return
    profile = get_profile_store().get_active()
    shot = demo_session.due_shot(
        now_s=now_s, wall=datetime.now(), profile_id=profile.id, profile_name=profile.name
    )
    if shot is not None:
        socketio.emit("shot", {"shot": shot_to_dict(shot), "stats": demo_session.stats()})


@socketio.on("set_demo_mode")
def handle_set_demo_mode(data=None):
    """The menu switch and the band's End button; a payload without a bool changes nothing."""
    enabled = _payload_dict(data).get("enabled")
    if enabled is True:
        _start_demo()
    elif enabled is False:
        _end_demo()


@socketio.on("demo_touch")
def handle_demo_touch():
    """A touch on any screen keeps the demo from ending idle."""
    with _demo_lock:
        demo_session.touch(time.monotonic())


def swing_speed_to_dict(event: SwingSpeedEvent) -> dict:
    """Convert a swing speed training event to a UI payload."""
    return {
        "peak_speed_mph": round(event.peak_speed_mph, 1),
        "timestamp": event.timestamp.isoformat(),
        "duration_ms": round(event.duration_ms),
        "reading_count": event.reading_count,
        "trigger_speed_mph": round(event.trigger_speed_mph, 1),
        "peak_magnitude": event.peak_magnitude,
        "training_implement": event.training_implement,
        "training_implement_label": event.training_implement_label,
        "profile_id": event.profile_id,
        "profile_name": event.profile_name,
        "unit": event.unit,
        "mode": event.mode,
    }


def swing_speed_to_shot_dict(event: SwingSpeedEvent) -> dict:
    """Convert a swing speed event to the existing shot UI shape."""
    peak_speed = round(event.peak_speed_mph, 1)
    return {
        "ball_speed_mph": peak_speed,
        "ball_speed_raw_mph": None,
        "club_speed_mph": peak_speed,
        "smash_factor": None,
        "estimated_carry_yards": 0,
        "carry_range": [0, 0],
        "club": event.training_implement_label,
        "profile_id": event.profile_id,
        "profile_name": event.profile_name,
        "timestamp": event.timestamp.isoformat(),
        "peak_magnitude": event.peak_magnitude,
        "launch_angle_vertical": None,
        "launch_angle_horizontal": None,
        "launch_angle_confidence": None,
        "launch_angle_vertical_confidence": None,
        "launch_angle_horizontal_confidence": None,
        "launch_angle_vertical_source": None,
        "launch_angle_horizontal_source": None,
        "angle_source": None,
        "club_angle_deg": None,
        "club_path_deg": None,
        "spin_axis_deg": None,
        "spin_axis_source": None,
        "curve_class": None,
        "spin_rpm": None,
        "spin_rpm_measured": None,
        "spin_source": None,
        "spin_confidence": None,
        "spin_quality": None,
        "spin_snr": None,
        "spin_modulation_depth": None,
        "spin_peak_freq_hz": None,
        "spin_peak_freq_rpm": None,
        "spin_seam_cycles": None,
        "spin_at_lower_rail": None,
        "spin_at_upper_rail": None,
        "spin_candidates": None,
        "spin_phase_method": None,
        "spin_phase_rpm": None,
        "spin_phase_snr": None,
        "spin_phase_agreement_pct": None,
        "spin_phase_confirmed": None,
        "spin_rejection_reason": None,
        "carry_spin_adjusted": None,
        "apex_yards": None,
        "mode": event.mode,
        "swing_speed_duration_ms": round(event.duration_ms),
        "swing_speed_reading_count": event.reading_count,
        "swing_speed_trigger_mph": round(event.trigger_speed_mph, 1),
        "training_implement": event.training_implement,
        "training_implement_label": event.training_implement_label,
    }


def on_swing_speed_detected(event: SwingSpeedEvent):
    """Handle swing speed training reps and emit them to connected clients."""
    active_profile = get_profile_store().get_active()
    event.profile_id = active_profile.id
    event.profile_name = active_profile.name
    event_data = swing_speed_to_dict(event)
    shot_data = swing_speed_to_shot_dict(event)
    stats = monitor.get_session_stats() if monitor else {}
    socketio.emit("swing_speed", {"event": event_data, "stats": stats})
    socketio.emit("shot", {"shot": shot_data, "stats": stats})
    logger.info(
        "[SERVER] Swing speed event emitted: peak=%.1f mph, readings=%d",
        event.peak_speed_mph,
        event.reading_count,
    )


def start_monitor(
    port: Optional[str] = None,
    mock: bool = False,
    trigger_type: str = "sound",
    debug: bool = False,
    trigger_kwargs: Optional[dict] = None,
    sample_rate_ksps: int = 30,
    swing_speed_mode: bool = False,
    swing_speed_kwargs: Optional[dict] = None,
    ops_baud: Optional[int] = None,
    ops_fast_rearm: bool = False,
):
    """
    Start the monitor in launch monitor or swing speed mode.

    Args:
        port: Serial port for radar
        mock: Run in mock mode without radar
        trigger_type: Trigger strategy (sound or speed)
        debug: Enable verbose debug output
        ops_baud: Target UART baud when the OPS243 is on the GPIO header
        ops_fast_rearm: Re-arm the OPS with 50 ms drain gap and settles
    """
    global monitor, mock_mode, mock_swing_speed_mode, debug_mode, radar_config

    # Stop any existing monitor first
    if monitor is not None:
        print("[MONITOR] Stopping existing monitor before starting new one")
        stop_monitor()

    mock_mode = mock
    mock_swing_speed_mode = bool(mock and swing_speed_mode)
    debug_mode = debug
    if mock and swing_speed_mode:
        monitor = MockSwingSpeedMonitor(**(swing_speed_kwargs or {}))
        print("[MODE] Mock swing speed training mode")
    elif mock:
        # Mock mode for testing without radar
        monitor = MockLaunchMonitor()
    elif swing_speed_mode:
        from .swing_speed import SwingSpeedMonitor

        monitor = SwingSpeedMonitor(
            port=port,
            **(swing_speed_kwargs or {}),
        )
        print("[MODE] Swing speed training mode")
    else:
        from .rolling_buffer import RollingBufferMonitor

        monitor = RollingBufferMonitor(
            port=port,
            trigger_type=trigger_type,
            sample_rate_ksps=sample_rate_ksps,
            ops_baud=ops_baud,
            ops_fast_rearm=ops_fast_rearm,
            **(trigger_kwargs or {}),
        )
        print(
            "[MODE] Rolling buffer mode "
            f"(trigger: {trigger_type}, sample_rate: {sample_rate_ksps}ksps)"
        )

    monitor.connect()
    _reset_shot_sequence()
    ops_spin_session_median.reset()
    _reset_club_data_state("new session")

    if swing_speed_mode:
        swing_config = swing_speed_kwargs or {}
        radar_config = {
            **radar_config,
            "min_speed": int(swing_config.get("trigger_threshold_mph", 30)),
            "max_speed": int(swing_config.get("max_speed_mph") or 0),
        }

    logger.info(
        "[SERVER] Starting monitor: mode=%s, trigger=%s, sample_rate=%dksps",
        "swing-speed" if swing_speed_mode else ("mock" if mock else "rolling-buffer"),
        trigger_type,
        sample_rate_ksps,
    )

    # Start session logging
    session_logger = get_session_logger()
    if session_logger:
        radar_info = monitor.get_radar_info() if not mock else {}
        session_logger.start_session(
            radar_port=port if not mock else "mock",
            firmware_version=radar_info.get("Version"),
            camera_enabled=camera_capture_runtime is not None,
            camera_model="capture" if camera_capture_runtime is not None else None,
            config=_session_start_config(),
            mode="swing-speed" if swing_speed_mode else ("mock" if mock else "rolling-buffer"),
            trigger_type=None if swing_speed_mode or mock else trigger_type,
        )
        _log_ops_speed_refusal()
        if not mock and radar_info:
            session_logger.log_connection(
                device="ops243",
                port=port or "auto",
                baud=getattr(monitor.radar, "baud", 0) if hasattr(monitor, "radar") else 0,
                firmware=radar_info.get("Version"),
            )
            # Capture the OPS radar-clock -> host-epoch offset once at startup
            # before the trigger loop runs. Sound-triggered captures use this to
            # anchor K-LD7 correlation to the OPS trigger_time instead of the
            # USB first-byte arrival time.
            radar = getattr(monitor, "radar", None)
            if not swing_speed_mode and radar is not None and hasattr(radar, "read_clock_sync"):
                try:
                    clock_sync = radar.read_clock_sync()
                    session_logger.log_clock_sync(
                        device="ops243",
                        port=port or "auto",
                        summary=clock_sync,
                    )
                except Exception:  # pylint: disable=broad-except
                    # Instrumentation must never break startup.
                    logger.warning("[SERVER] OPS clock sync read failed", exc_info=True)
        if not mock and iwr6843_runtime is not None:
            session_logger.log_connection(
                device="iwr6843",
                port=iwr6843_runtime.capture_monitor.port,
                baud=getattr(iwr6843_runtime.capture_monitor.radar, "baud", 1_041_667),
                firmware="custom-l3-dump",
                estimator="lcmf_v1",
                trigger_pin_bcm=iwr6843_runtime_config.get("trigger_pin_bcm"),
            )
        if not mock and inclinometer_service is not None:
            session_logger.log_connection(
                device="lis3dh",
                port=f"i2c-{inclinometer_runtime_config.get('i2c_bus', 1)}",
                baud=0,
                address=inclinometer_runtime_config.get("i2c_address", "0x18"),
                sample_hz=inclinometer_runtime_config.get("sample_hz", 10.0),
            )

    if swing_speed_mode:
        monitor.start(
            event_callback=on_swing_speed_detected,
            live_callback=on_live_reading,
        )
    elif not mock:

        def on_trigger_diagnostic(data: dict):
            """Forward trigger diagnostics to connected UI clients."""
            if (
                iwr6843_runtime is not None
                and data.get("accepted") is False
                and data.get("reason") == "no_outbound_speed"
            ):
                iwr6843_runtime.capture_monitor.reject_trigger(
                    data.get("capture_first_byte_timestamp")
                )
            socketio.emit("trigger_diagnostic", data)

        if trigger_gate is not None:
            monitor.set_trigger_gate(trigger_gate)
        if hasattr(monitor, "set_ops_speed_witness"):
            monitor.set_ops_speed_witness(ops_speed_witness)
        monitor.start(  # pylint: disable=unexpected-keyword-arg
            shot_callback=on_shot_detected,
            live_callback=on_live_reading,
            diagnostic_callback=on_trigger_diagnostic,
            processing_callback=on_shot_processing,
            trigger_rejected_callback=_reject_ti_for_ops_rejection,
            ops_dump_started_callback=_notify_ti_ops_dump_started,
            ops_dump_finished_callback=_notify_ti_ops_dump_finished,
        )
        if iwr6843_runtime is not None:
            if getattr(monitor, "trigger_type", None) in ("sound", "ops-speed"):
                iwr6843_runtime.capture_monitor.enable_ops_dump_gating()
            iwr6843_runtime.capture_monitor.arm()
        if trigger_gate_listener is not None:
            trigger_gate_listener.arm()
        _arm_ops_speed_inputs()
    else:
        monitor.start(shot_callback=on_shot_detected, live_callback=on_live_reading)


def _cloud_raw_uploads_enabled() -> bool:
    """True when this Pi has opted in to raw cloud uploads. Never raises."""
    try:
        from .cloud.config import load_config

        config = load_config()
        return bool(config and config.is_active() and config.upload_raw)
    except Exception:  # pylint: disable=broad-exception-caught
        return False


def _fire_cloud_push(session_logger):
    """Best-effort, non-blocking cloud push on session end.

    Fully guarded: the uploader is opt-in and must never delay or break the
    shot/session path. The systemd timer is the safety net if this no-ops.
    """
    try:
        from .cloud.config import load_config
        from .cloud.trigger import fire_push_async

        config = load_config()
        if config is None or not config.is_active():
            return
        log_dir = getattr(session_logger, "log_dir", None)
        if log_dir is not None:
            fire_push_async(config, log_dir=log_dir)
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _run_cloud_push_for_ui():
    """Run a manual cloud push and report the result to connected UI clients."""
    socketio.emit("cloud_upload_status", {"state": "running", "message": "Uploading..."})
    try:
        from .cloud import commands
        from .cloud.client import CloudClient
        from .cloud.config import CloudConfig, load_config

        config = load_config() or CloudConfig()
        session_logger = get_session_logger()
        log_dir = getattr(session_logger, "log_dir", None)
        if log_dir is None:
            log_dir = session_logger.DEFAULT_LOG_DIR if session_logger else None
        if log_dir is None:
            log_dir = Path.home() / "openflight_sessions"

        messages = []
        summary = commands.cmd_push(
            config,
            Path(log_dir),
            CloudClient(config.endpoint, token=config.device_token or None),
            out=messages.append,
        )

        if summary.get("needs_relink"):
            state = "error"
            message = "Cloud token rejected. Re-link this Pi."
        elif summary.get("skipped") == "inactive":
            state = "error"
            message = "Cloud uploader is not linked."
        elif summary.get("offline"):
            state = "error"
            message = "Cloud unreachable."
        elif summary.get("uploaded", 0) > 0:
            state = "complete"
            message = f"Uploaded {summary['uploaded']} session(s)."
        elif messages:
            state = "complete"
            message = messages[-1]
        else:
            state = "complete"
            message = "Nothing to upload."

        socketio.emit(
            "cloud_upload_status",
            {"state": state, "message": message, "summary": summary},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("[SERVER] Manual cloud upload failed: %s", exc, exc_info=True)
        socketio.emit(
            "cloud_upload_status",
            {"state": "error", "message": str(exc)},
        )


def stop_monitor():
    """Stop the launch monitor."""
    global monitor, mock_swing_speed_mode  # pylint: disable=global-statement

    # End session logging
    session_logger = get_session_logger()
    if session_logger:
        if ops_speed_witness is not None:
            _log_ops_speed_trigger(_ops_speed_summary_entry())
        session_logger.end_session()
        _fire_cloud_push(session_logger)

    if monitor:
        monitor.stop()
        monitor.disconnect()
        monitor = None
    mock_swing_speed_mode = False


class MockLaunchMonitor:
    """Mock launch monitor for UI development without radar hardware."""

    def __init__(self):
        """Initialize mock monitor."""
        self._shots: List[Shot] = []
        self._running = False
        self._shot_callback = None
        self._current_club = ClubType.DRIVER
        self._current_club_selection = ClubType.DRIVER.value

    def connect(self):
        """Connect to mock radar (no-op)."""
        return True

    def disconnect(self):
        """Disconnect from mock radar."""
        self.stop()

    def start(self, shot_callback=None, live_callback=None):  # pylint: disable=unused-argument
        """Start mock monitoring."""
        self._shot_callback = shot_callback
        self._running = True
        print("Mock monitor started - simulate shots via WebSocket")

    def stop(self):
        """Stop mock monitoring."""
        self._running = False

    def simulate_shot(self, ball_speed: float = None):
        """Simulate a shot for testing using realistic TrackMan-based values."""
        shot = generate_shot(self._current_club, random, ball_speed_mph=ball_speed)
        shot.club_selection = self._current_club_selection

        self._shots.append(shot)

        if self._shot_callback:
            self._shot_callback(shot)

        return shot

    def get_shots(self) -> List[Shot]:
        """Get all recorded shots."""
        return self._shots.copy()

    def get_session_stats(self) -> dict:
        """Get session statistics."""
        return summarize_shots(self._shots, mode="mock")

    def clear_session(self):
        """Clear all recorded shots."""
        self._shots = []

    def set_club(self, club: ClubType, selection: Optional[str] = None):
        """Set the current club for future shots."""
        self._current_club = club
        self._current_club_selection = selection or club.value


class _MockSwingRadar:
    """Tiny radar facade so UI tuning can exercise the swing speed controls."""

    port = "mock"
    baud = 0

    def set_min_speed_filter(self, value):  # pylint: disable=unused-argument
        """Accept mock lower speed updates."""

    def set_max_speed_filter(self, value):  # pylint: disable=unused-argument
        """Accept mock upper speed updates."""

    def set_magnitude_filter(self, min_mag=0, max_mag=0):  # pylint: disable=unused-argument
        """Accept mock magnitude updates."""

    def set_transmit_power(self, level):  # pylint: disable=unused-argument
        """Accept mock transmit power updates."""


class MockSwingSpeedMonitor:
    """Mock swing speed monitor for UI development without OPS hardware."""

    def __init__(
        self,
        trigger_threshold_mph: float = 30.0,
        max_speed_mph: Optional[float] = 130.0,
        min_readings: int = 3,
        single_reading_peak_mph: float = 60.0,
        **kwargs,  # pylint: disable=unused-argument
    ):
        self.trigger_threshold_mph = float(trigger_threshold_mph)
        self.max_speed_mph = None if max_speed_mph is None else float(max_speed_mph)
        self.min_readings = int(min_readings)
        self.single_reading_peak_mph = float(single_reading_peak_mph)
        self.radar = _MockSwingRadar()
        self._events: List[SwingSpeedEvent] = []
        self._running = False
        self._event_callback = None
        self.training_implement = "driver"
        self.training_implement_label = "Driver"

    def connect(self):
        """Connect to mock radar."""
        return True

    def disconnect(self):
        """Disconnect from mock radar."""
        self.stop()

    def start(self, event_callback=None, live_callback=None):  # pylint: disable=unused-argument
        """Start mock swing speed monitoring."""
        self._event_callback = event_callback
        self._running = True
        print("Mock swing speed monitor started - simulate swings via WebSocket")

    def stop(self):
        """Stop mock monitoring."""
        self._running = False

    def get_radar_info(self):
        """Return mock radar metadata."""
        return {"Version": "mock-swing-speed"}

    def simulate_shot(self, peak_speed: float = None):
        """Simulate a club-only swing speed rep."""
        lower = max(20.0, float(self.trigger_threshold_mph))
        upper = float(self.max_speed_mph) if self.max_speed_mph is not None else 130.0
        upper = max(lower + 1.0, upper)

        if peak_speed is None:
            center = min(max(lower + 35.0, 95.0), upper - 4.0)
            peak_speed = random.gauss(center, 6.0)

        peak_speed = max(lower, min(upper, float(peak_speed)))
        trigger_speed = max(lower, min(peak_speed, peak_speed - random.uniform(12.0, 24.0)))
        reading_count = random.randint(max(1, self.min_readings), max(self.min_readings + 3, 8))

        event = SwingSpeedEvent(
            peak_speed_mph=peak_speed,
            timestamp=datetime.now(),
            duration_ms=random.uniform(850.0, 1800.0),
            reading_count=reading_count,
            trigger_speed_mph=trigger_speed,
            peak_magnitude=random.uniform(80.0, 450.0),
            training_implement=self.training_implement,
            training_implement_label=self.training_implement_label,
        )
        self._events.append(event)

        if self._event_callback:
            self._event_callback(event)

        return event

    def get_shots(self) -> List[Shot]:
        """Swing speed mode has no ball-flight shots."""
        return []

    def get_events(self) -> List[SwingSpeedEvent]:
        """Get all simulated swing speed reps."""
        return list(self._events)

    def get_session_stats(self) -> dict:
        """Get swing speed session statistics."""
        if not self._events:
            return {
                "shot_count": 0,
                "avg_ball_speed": 0,
                "max_ball_speed": 0,
                "min_ball_speed": 0,
                "avg_club_speed": None,
                "avg_smash_factor": None,
                "avg_carry_est": 0,
            }

        speeds = [event.peak_speed_mph for event in self._events]
        return {
            "shot_count": len(self._events),
            "avg_ball_speed": statistics.mean(speeds),
            "max_ball_speed": max(speeds),
            "min_ball_speed": min(speeds),
            "std_dev": statistics.stdev(speeds) if len(speeds) > 1 else 0,
            "avg_club_speed": statistics.mean(speeds),
            "avg_smash_factor": None,
            "avg_carry_est": 0,
        }

    def clear_session(self):
        """Clear all simulated swing speed reps."""
        self._events = []

    def set_club(self, club: ClubType, selection=None):  # pylint: disable=unused-argument
        """Accept club changes for API compatibility."""

    def set_training_implement(self, implement: str, label: str):
        """Set the training implement stamped onto future mock reps."""
        self.training_implement = implement
        self.training_implement_label = label


def _add_ballistics_arguments(parser):
    """Add the preferred ballistic carry model and its explicit opt-out."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--ballistics",
        action="store_true",
        dest="ballistics",
        help=(
            "Use the physics-based carry simulator (drag + Magnus, RK4). "
            "This is the default; shots without a vertical launch angle "
            "fall back to the legacy table estimator."
        ),
    )
    group.add_argument(
        "--no-ballistics",
        action="store_false",
        dest="ballistics",
        help="Disable the physics simulator and use the legacy carry table for all shots.",
    )
    parser.set_defaults(ballistics=True)


def _air_density_arg(value: str) -> float:
    import argparse  # pylint: disable=import-outside-toplevel

    try:
        density = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    low, high = AIR_DENSITY_RANGE_KG_M3
    if not low <= density <= high:
        raise argparse.ArgumentTypeError(f"must be between {low} and {high} kg/m³")
    return density


def _add_air_density_arguments(parser):
    """Add the site air density used by the ballistic carry model."""
    parser.add_argument(
        "--air-density",
        type=_air_density_arg,
        default=TRACKMAN_FLAT_AIR_DENSITY,
        help=(
            "Fallback site air density in kg/m³ for ballistic carry, used while no "
            "phone or manual weather reading is at most 3 h old; the simulator export "
            "always uses it "
            f"(default: {TRACKMAN_FLAT_AIR_DENSITY}, TrackMan 'Flat' sea level 77 °F, "
            "the density the model was validated at)"
        ),
    )


def _typical_spin_scale_arg(value: str) -> float:
    import argparse  # pylint: disable=import-outside-toplevel

    try:
        scale = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    low, high = TYPICAL_SPIN_SCALE_RANGE
    if not low <= scale <= high:
        raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
    return scale


def _add_typical_spin_scale_arguments(parser):
    """Add the factor on the club-typical spin used for carry."""
    parser.add_argument(
        "--typical-spin-scale",
        type=_typical_spin_scale_arg,
        default=1.0,
        help=(
            "Multiply the club-typical spin (TrackMan PGA Tour averages) of irons and "
            "wedges that the ballistic carry uses when no trusted spin was measured, "
            "and that the UI shows as typical spin (default: 1.0, allowed "
            f"{TYPICAL_SPIN_SCALE_RANGE[0]}-{TYPICAL_SPIN_SCALE_RANGE[1]}). Driver, "
            "woods, hybrids and unknown clubs keep the table spin; trusted measured "
            "spin and the simulator (GSPro etc.) export spin are unchanged."
        ),
    )
    parser.add_argument(
        "--typical-spin-table",
        choices=sorted(TYPICAL_SPIN_TABLES),
        default=DEFAULT_TYPICAL_SPIN_TABLE,
        help=(
            "club-typical spin used for the ballistic carry when no trusted spin was "
            "measured: tour = TrackMan PGA Tour averages, range-ball = this user's "
            f"Toptracer range-ball spin (default: {DEFAULT_TYPICAL_SPIN_TABLE}). "
            "--typical-spin-scale applies to either table. This is the device default: "
            "a profile's ball type set in the app (standard = tour, range = range-ball) "
            "overrides it for that profile's shots."
        ),
    )


def _range_ball_drag_slope_arg(value: str) -> float:
    import argparse  # pylint: disable=import-outside-toplevel

    try:
        slope = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    low, high = RANGE_BALL_DRAG_SLOPE_RANGE
    if not low <= slope <= high:
        raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
    return slope


def _add_range_ball_drag_arguments(parser):
    """Add the unvalidated range-ball drag of the ballistic carry."""
    parser.add_argument(
        "--range-ball-drag-slope",
        type=_range_ball_drag_slope_arg,
        default=0.0,
        help=(
            "UNVALIDATED range-ball drag: above Reynolds number 1.2e5 the drag "
            "coefficient of every ballistic flight grows by SLOPE per 1e5, so fast "
            "drives fly shorter and wedges are unchanged (default: 0.0 = off, allowed "
            f"{RANGE_BALL_DRAG_SLOPE_RANGE[0]}-{RANGE_BALL_DRAG_SLOPE_RANGE[1]}; the "
            "Toptracer study's fit is 0.3546, which failed its peak/hang-time check)."
        ),
    )


def _add_battery_arguments(parser):
    """Add explicit battery-provider selection."""
    parser.add_argument(
        "--battery",
        choices=SUPPORTED_BATTERY_PROVIDERS,
        default=None,
        help="Show battery and external-power status using the selected provider",
    )


def _add_trigger_gate_arguments(parser):
    """Add the Pi-driven OPS HOST_INT trigger (off unless --ops-trigger-pin is given)."""
    from .trigger_gate import DEFAULT_DEADLINE_MS, DEFAULT_PULSE_MS, GATE_MODES

    parser.add_argument(
        "--ops-trigger-pin",
        type=int,
        default=None,
        help=(
            "BCM GPIO wired to OPS HOST_INT (J3 pin 3) through 1 kOhm, e.g. 27. The Pi then "
            "triggers the OPS on each sound edge; only after the GATE-to-HOST_INT wire is "
            "removed. Default: off, the microphone drives HOST_INT"
        ),
    )
    parser.add_argument(
        "--ops-trigger-mode",
        choices=GATE_MODES,
        default=None,
        help=(
            "passthrough (default): pulse on every sound edge. camera: pulse only when the "
            "camera sees the ball leave the tee or cannot tell; needs --camera-capture and "
            "a longer --sound-pre-trigger"
        ),
    )
    parser.add_argument(
        "--ops-trigger-pulse-ms",
        type=float,
        default=DEFAULT_PULSE_MS,
        help=f"HOST_INT pulse length (default: {DEFAULT_PULSE_MS:.0f} ms)",
    )
    parser.add_argument(
        "--ops-trigger-deadline-ms",
        type=float,
        default=DEFAULT_DEADLINE_MS,
        help=(
            "Camera mode: latest pulse after the sound edge; unclear means pulse "
            f"(default: {DEFAULT_DEADLINE_MS:.0f} ms)"
        ),
    )


def _add_shot_trigger_arguments(parser):
    """``--shot-trigger`` and the IWR6843 self-trigger tuning (see iwr6843.self_trigger)."""
    from .iwr6843.self_trigger import (
        DEFAULT_SELF_TRIGGER_OFFSET_M,
        DEFAULT_SELF_TRIGGER_SNR,
        SHADOW_HOLDOFF_MAX_MS,
        SHOT_TRIGGERS,
    )
    from .ops_speed import OPS_SPEED_SHOT_TRIGGERS
    from .trigger_gate import DEFAULT_TI_PRE_TRIGGER_SEGMENTS

    parser.add_argument(
        "--shot-trigger",
        choices=(*SHOT_TRIGGERS, *OPS_SPEED_SHOT_TRIGGERS),
        default="sound",
        help=(
            "What starts a shot. sound (default): the microphone edge, as before. ti: the "
            "IWR6843 self-trigger, no microphone (needs --iwr6843, --ops-trigger-pin and the "
            "self-trigger firmware). ti-shadow: the microphone drives everything; the TI "
            "self-trigger runs alongside and is logged as ti_self_trigger entries. "
            "ops-speed-shadow / ops-speed: the OPS243-A firmware 1.3.3 speed trigger on J3 "
            "pin 1 (see --ops-speed-pin); shadow: the microphone still decides, ops-speed: "
            "pin 1 drives everything and the microphone is a witness. Refused (back to "
            "sound) below firmware 1.3.3"
        ),
    )
    parser.add_argument(
        "--ti-pre-trigger",
        type=int,
        default=None,
        help=(
            "--shot-trigger ti: OPS pre-trigger segments S#n, replacing --sound-pre-trigger "
            f"(default: {DEFAULT_TI_PRE_TRIGGER_SEGMENTS} = 57.6 ms before / 147.2 ms after "
            "HOST_INT at 20 ksps)"
        ),
    )
    parser.add_argument(
        "--ti-fire-latency-ms",
        default=None,
        help=(
            "--shot-trigger ti: board fire minus impact per firing rule, RULE=MS,... with "
            "RULE crossing, end, leave, unknown (they place the club-speed anchor) or worst "
            "(the slowest fire, sizes the --ti-pre-trigger check). Default, for offset 0: "
            "crossing=-1.7,end=1.57,leave=-0.48,unknown=-0.57,worst=9.52; for "
            "--iwr6843-self-trigger-offset-m 0.2 use crossing=2.88,end=3.9,leave=1.19,"
            "unknown=2.91,worst=18.45"
        ),
    )
    parser.add_argument(
        "--ti-shadow-holdoff-ms",
        type=float,
        default=None,
        help=(
            "--shot-trigger ti-shadow: wait up to this long after a microphone edge for a "
            "TI fire before the sound-path l3dump freezes the ring (default: 0 = as today; "
            f"max {SHADOW_HOLDOFF_MAX_MS:.0f}). Longer measures late fires but shifts the "
            "sound-path TI ring later"
        ),
    )
    parser.add_argument(
        "--iwr6843-self-trigger-offset-m",
        type=float,
        default=None,
        help=(
            "Self-trigger: move the watched bin this far downrange of 2 bins short of the "
            f"ball (default: {DEFAULT_SELF_TRIGGER_OFFSET_M:g} m; upstream's kiosk uses 0.2 "
            "against waggle fires; +/-1 m)"
        ),
    )
    parser.add_argument(
        "--iwr6843-self-trigger-snr",
        type=float,
        default=None,
        help=(
            "Self-trigger: club target threshold over the board's noise floor "
            f"(default: {DEFAULT_SELF_TRIGGER_SNR:g}, minimum 1)"
        ),
    )
    parser.add_argument(
        "--iwr6843-self-trigger-bin",
        type=int,
        default=None,
        help=(
            "Self-trigger: explicit global range bin for triggerCfg instead of the one "
            "computed from --iwr6843-tee-m + 30 mm (the convention the firmware replays used)"
        ),
    )


def _add_ops_speed_arguments(parser):
    """The ``--ops-speed-*`` flags of ``--shot-trigger ops-speed*`` (see openflight.ops_speed)."""
    from .ops_speed import (
        DEFAULT_ANCHOR_MS,
        DEFAULT_DEBOUNCE_MS,
        DEFAULT_HOLDOFF_MS,
        DEFAULT_PRE_TRIGGER_SEGMENTS,
        DEFAULT_STALL_MIC_EDGES,
        DEFAULT_THRESHOLD_MPH,
        DEFAULT_TI_DELAY_MS,
        DEFAULT_WITNESS_WINDOW_MS,
        MAX_DEBOUNCE_MS,
        MAX_TI_DELAY_MS,
        PIN_ACTIVE_LEVELS,
        PIN_PULLS,
    )

    window = ",".join(f"{value:g}" for value in DEFAULT_WITNESS_WINDOW_MS)
    parser.add_argument(
        "--ops-speed-pin",
        type=int,
        default=None,
        help=(
            "--shot-trigger ops-speed*: BCM GPIO wired to OPS J3 pin 1 (the speed-trigger "
            "output), e.g. 22 (header pin 15, GND on pin 14). Measure its level first"
        ),
    )
    parser.add_argument(
        "--ops-speed-pin-active",
        choices=PIN_ACTIVE_LEVELS,
        default=None,
        help="Level of J3 pin 1 while the speed trigger holds it (default: high)",
    )
    parser.add_argument(
        "--ops-speed-pin-pull",
        choices=PIN_PULLS,
        default=None,
        help="Pull on the pin-1 input (default: down for high, up for low; off for push-pull)",
    )
    parser.add_argument(
        "--ops-speed-debounce-ms",
        type=float,
        default=None,
        help=(
            "Pin 1 must still be active this long before its edge is fanned out, with its "
            f"original time (default: {DEFAULT_DEBOUNCE_MS:g}; max {MAX_DEBOUNCE_MS:g})"
        ),
    )
    parser.add_argument(
        "--ops-speed-holdoff-ms",
        type=float,
        default=None,
        help=f"Ignore pin-1 edges this long after one (default: {DEFAULT_HOLDOFF_MS:g} ms)",
    )
    parser.add_argument(
        "--ops-speed-threshold-mph",
        type=int,
        default=None,
        help=(
            "Outbound speed that trips the OPS buffer, sent as ST-<mph> "
            f"(default: {DEFAULT_THRESHOLD_MPH})"
        ),
    )
    parser.add_argument(
        "--ops-speed-magnitude",
        type=int,
        default=None,
        help="Speed-trigger magnitude gate SMnn, 1-2000 (required; only the device can tell it)",
    )
    parser.add_argument(
        "--ops-speed-pre-trigger",
        type=int,
        default=None,
        help=(
            "OPS split S#n around the speed trip, replacing --sound-pre-trigger "
            f"(default: {DEFAULT_PRE_TRIGGER_SEGMENTS} = 12.8 ms before / 192.0 ms after at "
            "20 ksps)"
        ),
    )
    parser.add_argument(
        "--ops-speed-ti-delay-ms",
        type=float,
        default=None,
        help=(
            f"--shot-trigger ops-speed: TI l3dump this long after the pin-1 edge (default: "
            f"{DEFAULT_TI_DELAY_MS:g}; 0-{MAX_TI_DELAY_MS:g}). A model value: recalibrate in M3 "
            "from the logged mic-minus-pin delay"
        ),
    )
    parser.add_argument(
        "--ops-speed-anchor-ms",
        type=float,
        default=None,
        help=(
            f"Club-speed anchor this long after the speed trip (default: {DEFAULT_ANCHOR_MS:g}). "
            "A model value: recalibrate in M3 from the logged mic-minus-pin delay"
        ),
    )
    parser.add_argument(
        "--ops-speed-witness-window-ms",
        default=None,
        help=(
            "Microphone minus pin-1 edge, LOW,HIGH in ms, that counts as the same swing "
            f"(default: {window})"
        ),
    )
    parser.add_argument(
        "--ops-speed-reject-practice-swings",
        action="store_true",
        help=(
            "Reject captures the practice-swing rule calls practice_swing (no club-to-ball "
            "jump and smash < 1.05). Off by default: on 197 real shots it already drops 2, "
            "against the M3 criterion of dropping none; the verdict is always logged"
        ),
    )
    parser.add_argument(
        "--ops-speed-stall-mic-edges",
        type=int,
        default=None,
        help=(
            "--shot-trigger ops-speed: re-arm the OPS once after this many microphone edges "
            "in a row without a speed trip while it was armed (stuck idle; default: "
            f"{DEFAULT_STALL_MIC_EDGES}, 0 = off)"
        ),
    )


# (flag, args attribute) of every --ops-speed-* option; set means "given".
_OPS_SPEED_FLAGS = (
    ("--ops-speed-pin", "ops_speed_pin"),
    ("--ops-speed-pin-active", "ops_speed_pin_active"),
    ("--ops-speed-pin-pull", "ops_speed_pin_pull"),
    ("--ops-speed-debounce-ms", "ops_speed_debounce_ms"),
    ("--ops-speed-holdoff-ms", "ops_speed_holdoff_ms"),
    ("--ops-speed-threshold-mph", "ops_speed_threshold_mph"),
    ("--ops-speed-magnitude", "ops_speed_magnitude"),
    ("--ops-speed-pre-trigger", "ops_speed_pre_trigger"),
    ("--ops-speed-ti-delay-ms", "ops_speed_ti_delay_ms"),
    ("--ops-speed-anchor-ms", "ops_speed_anchor_ms"),
    ("--ops-speed-witness-window-ms", "ops_speed_witness_window_ms"),
    ("--ops-speed-reject-practice-swings", "ops_speed_reject_practice_swings"),
    ("--ops-speed-stall-mic-edges", "ops_speed_stall_mic_edges"),
)
# Flags that only mean something with pin 1 driving the TI and the stall rule.
_OPS_SPEED_ONLY_FLAGS = ("--ops-speed-ti-delay-ms", "--ops-speed-stall-mic-edges")


def _validate_ops_speed_args(parser, args) -> str | None:
    """Refuse speed-trigger options that cannot work; set the S#n split; return a warning.

    ``args.ops_speed_config`` gets the ``OpsSpeedConfig`` and
    ``args.ops_speed_sound_pre_trigger`` the sound split, restored when the
    preflight refuses the speed trigger.
    """
    from .ops_speed import OPS_SPEED_SHADOW, OPS_SPEED_SHOT_TRIGGERS, OpsSpeedConfig
    from .trigger_gate import FULL_EDGE_HISTORY_MS, ops_speed_split_check, pre_trigger_ms

    mode = getattr(args, "shot_trigger", "sound")
    given = [
        flag
        for flag, name in _OPS_SPEED_FLAGS
        if getattr(args, name, None) is not None and getattr(args, name) is not False
    ]
    args.ops_speed_config = None
    if mode not in OPS_SPEED_SHOT_TRIGGERS:
        if given:
            parser.error(f"{given[0]} needs --shot-trigger ops-speed or ops-speed-shadow")
        return None
    _refuse_ops_speed_setup(parser, args, mode, given)
    try:
        config = OpsSpeedConfig.from_args(args)
    except ValueError as exc:
        parser.error(f"--shot-trigger {mode}: {exc}")
    split = config.pre_trigger_segments
    problem = ops_speed_split_check(split, config.anchor_ms, args.sample_rate)
    if problem is not None and problem[0] == "error":
        parser.error(f"--shot-trigger {mode}: {problem[1]}")
    if split != args.sound_pre_trigger:
        print(
            f"--shot-trigger {mode}: OPS split S#{split} (--ops-speed-pre-trigger) replaces "
            f"--sound-pre-trigger {args.sound_pre_trigger}"
        )
    args.ops_speed_config = config
    args.ops_speed_sound_pre_trigger = args.sound_pre_trigger
    args.sound_pre_trigger = split
    warnings = [problem[1]] if problem is not None else []
    if mode == OPS_SPEED_SHADOW and pre_trigger_ms(split, args.sample_rate) < FULL_EDGE_HISTORY_MS:
        warnings.append(
            f"S#{split} keeps {pre_trigger_ms(split, args.sample_rate):.1f} ms before HOST_INT: "
            "a fallback dump (microphone edge without a speed trip) has less club approach "
            f"than the club-speed rule's {FULL_EDGE_HISTORY_MS:.1f} ms"
        )
    return "; ".join(warnings) or None


def _refuse_ops_speed_setup(parser, args, mode: str, given: list[str]) -> None:
    """Refuse a speed-trigger mode the rest of the command line cannot run."""
    from .ops_speed import OPS_SPEED_SHADOW

    if mode == OPS_SPEED_SHADOW:
        for flag in _OPS_SPEED_ONLY_FLAGS:
            if flag in given:
                parser.error(f"{flag} needs --shot-trigger ops-speed")
    if args.mock or args.swing_speed or args.trigger != "sound":
        parser.error(
            f"--shot-trigger {mode} needs the OPS rolling buffer (not --mock, --swing-speed "
            "or --trigger speed)"
        )
    if args.ops_trigger_pin is None:
        parser.error(
            f"--shot-trigger {mode} needs --ops-trigger-pin: HOST_INT must be driven LOW by "
            "the Pi (the shadow also pulses it for a microphone edge without a speed trip)"
        )
    if args.ops_trigger_mode == "camera":
        parser.error(
            f"--shot-trigger {mode} needs --ops-trigger-mode passthrough: the gate cannot "
            "block a dump the OPS started itself"
        )
    if args.ops_speed_pin is None:
        parser.error(f"--shot-trigger {mode} needs --ops-speed-pin (the BCM GPIO on J3 pin 1)")
    if args.ops_speed_magnitude is None:
        parser.error(f"--shot-trigger {mode} needs --ops-speed-magnitude (SMnn, from M1)")
    if args.ops_speed_pin in (args.iwr6843_trigger_pin, args.ops_trigger_pin):
        parser.error(
            "--ops-speed-pin must differ from the sound-gate input "
            f"BCM{args.iwr6843_trigger_pin} and --ops-trigger-pin BCM{args.ops_trigger_pin}"
        )


OPS_SPEED_REFUSED_TEXT = "speed trigger refused, staying on the microphone (--shot-trigger sound)"


def _ops_speed_preflight(args, *, radar_factory=None, pin_check=None) -> str | None:
    """Refuse the speed trigger before any GPIO or OPS setup when it cannot run.

    Connects the OPS once (the UART baud negotiation, or a ``?V`` probe over
    USB, reads the firmware version), checks it (1.3.3 or later on the 1.3
    train, a known off command) and then the idle level of pin 1. On a
    refusal: one ERROR line, ``--shot-trigger sound`` with the sound split
    restored, and ``config.ops_speed`` records why; the rest of the sound setup
    (BCM17, BCM27 passthrough) then runs unchanged. Returns the reason, None
    when the speed trigger may run (or was not asked for).
    """
    global ops_speed_runtime_config  # pylint: disable=global-statement
    from .ops_speed import OPS_SPEED_SHOT_TRIGGERS, pin_idle_problem, speed_trigger_supported

    config = getattr(args, "ops_speed_config", None)
    if args.shot_trigger not in OPS_SPEED_SHOT_TRIGGERS or config is None:
        return None
    radar_kwargs = {} if args.ops_baud is None else {"uart_baud": args.ops_baud}
    radar = (radar_factory or OPS243Radar)(port=args.port, **radar_kwargs)
    version = None
    reason = None
    try:
        radar.connect(probe_firmware=True)
        version = radar.firmware_version
    except Exception as exc:  # pylint: disable=broad-exception-caught
        reason = f"OPS firmware check failed ({exc})"
    finally:
        try:
            radar.disconnect()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[OPS-SPEED] Preflight disconnect failed", exc_info=True)
    if reason is None:
        _, reason = speed_trigger_supported(version)
    pin_idle = None
    if reason is None:
        reason = (pin_check or pin_idle_problem)(
            config.pin, active=config.pin_active, pull=config.pin_pull
        )
        pin_idle = "inactive" if reason is None else None
    ops_speed_runtime_config = {
        "requested": True,
        "enabled": reason is None,
        "mode": config.mode,
        "firmware": version,
        "pin_idle": pin_idle,
        "flags": config.as_dict(),
    }
    if reason is None:
        logger.info(
            "[OPS-SPEED] %s: firmware %s, BCM%d (J3 pin 1) inactive at idle",
            config.mode,
            version,
            config.pin,
        )
        return None
    ops_speed_runtime_config["refused"] = reason
    logger.error("[OPS-SPEED] %s: %s", reason, OPS_SPEED_REFUSED_TEXT)
    print(f"[OPS-SPEED] {reason}: {OPS_SPEED_REFUSED_TEXT}")
    args.shot_trigger = "sound"
    args.sound_pre_trigger = args.ops_speed_sound_pre_trigger
    args.ops_speed_config = None
    return reason


def _validate_shot_trigger_args(parser, args) -> str | None:
    """Refuse shot-trigger options that cannot work; resolve the ti split; return a warning."""
    from .iwr6843.self_trigger import (
        MAX_SELF_TRIGGER_OFFSET_M,
        SHADOW_HOLDOFF_MAX_MS,
        TiAnchorModel,
        parse_fire_latency,
    )
    from .ops_speed import OPS_SPEED_SHOT_TRIGGERS
    from .trigger_gate import DEFAULT_TI_PRE_TRIGGER_SEGMENTS, ti_split_check

    mode = args.shot_trigger
    if mode != "ti":
        for flag, value in (
            ("--ti-pre-trigger", args.ti_pre_trigger),
            ("--ti-fire-latency-ms", args.ti_fire_latency_ms),
        ):
            if value is not None:
                parser.error(f"{flag} needs --shot-trigger ti")
    if mode != "ti-shadow" and args.ti_shadow_holdoff_ms is not None:
        parser.error("--ti-shadow-holdoff-ms needs --shot-trigger ti-shadow")
    tuning = (
        ("--iwr6843-self-trigger-offset-m", args.iwr6843_self_trigger_offset_m),
        ("--iwr6843-self-trigger-snr", args.iwr6843_self_trigger_snr),
        ("--iwr6843-self-trigger-bin", args.iwr6843_self_trigger_bin),
    )
    if mode == "sound" or mode in OPS_SPEED_SHOT_TRIGGERS:
        # The speed-trigger modes run the TI as in sound (_validate_ops_speed_args).
        for flag, value in tuning:
            if value is not None:
                parser.error(f"{flag} needs --shot-trigger ti or ti-shadow")
        return None
    if not args.iwr6843:
        parser.error(f"--shot-trigger {mode} needs --iwr6843")
    if args.mock or args.swing_speed or args.trigger != "sound":
        parser.error(
            f"--shot-trigger {mode} needs the OPS sound trigger (not --mock, --swing-speed "
            "or --trigger speed)"
        )
    offset = args.iwr6843_self_trigger_offset_m
    if offset is not None and (
        not math.isfinite(offset) or abs(offset) > MAX_SELF_TRIGGER_OFFSET_M
    ):
        parser.error(
            f"--iwr6843-self-trigger-offset-m must be within +/-{MAX_SELF_TRIGGER_OFFSET_M:g} m"
        )
    snr = args.iwr6843_self_trigger_snr
    if snr is not None and (not math.isfinite(snr) or snr < 1.0):
        parser.error("--iwr6843-self-trigger-snr must be at least 1")
    if args.iwr6843_self_trigger_bin is not None and args.iwr6843_self_trigger_bin < 0:
        parser.error("--iwr6843-self-trigger-bin must be >= 0")
    if mode == "ti-shadow":
        holdoff = 0.0 if args.ti_shadow_holdoff_ms is None else args.ti_shadow_holdoff_ms
        if not 0.0 <= holdoff <= SHADOW_HOLDOFF_MAX_MS:
            parser.error(f"--ti-shadow-holdoff-ms must be 0-{SHADOW_HOLDOFF_MAX_MS:g}")
        args.ti_shadow_holdoff_ms = holdoff
        return None
    if args.ops_trigger_pin is None:
        parser.error(
            "--shot-trigger ti needs --ops-trigger-pin: without a microphone only the Pi "
            "can pulse OPS HOST_INT"
        )
    try:
        latencies = parse_fire_latency(args.ti_fire_latency_ms or "")
    except ValueError as exc:
        parser.error(f"--ti-fire-latency-ms: {exc}")
    args.ti_anchor = TiAnchorModel(fire_latency_ms=latencies)
    split = DEFAULT_TI_PRE_TRIGGER_SEGMENTS if args.ti_pre_trigger is None else args.ti_pre_trigger
    if not 0 <= split <= 32:
        parser.error("--ti-pre-trigger must be 0-32")
    if split != args.sound_pre_trigger:
        print(
            f"--shot-trigger ti: OPS split S#{split} (--ti-pre-trigger) replaces "
            f"--sound-pre-trigger {args.sound_pre_trigger}"
        )
    args.ti_pre_trigger = split
    args.sound_pre_trigger = split
    deadline = args.ops_trigger_deadline_ms if args.ops_trigger_mode == "camera" else 0.0
    problem = ti_split_check(
        split,
        max_lead_ms=args.ti_anchor.max_lead_ms,
        deadline_ms=deadline,
        sample_rate_ksps=args.sample_rate,
    )
    if problem is None:
        return None
    level, message = problem
    if level == "error":
        parser.error(f"--shot-trigger ti: {message}")
    return message


def _validate_trigger_gate_args(parser, args) -> str | None:
    """Refuse trigger-gate options that cannot work; return a short-split warning."""
    from .trigger_gate import DEADLINE_MS_RANGE, PULSE_MS_RANGE, camera_split_check

    if args.ops_trigger_pin is None:
        if args.ops_trigger_mode is not None:
            parser.error("--ops-trigger-mode needs --ops-trigger-pin")
        return None
    args.ops_trigger_mode = args.ops_trigger_mode or "passthrough"
    if args.mock or args.swing_speed or args.trigger != "sound":
        parser.error(
            "--ops-trigger-pin needs the OPS sound trigger (not --mock, --swing-speed "
            "or --trigger speed)"
        )
    if not 0 <= args.ops_trigger_pin <= 27 or args.ops_trigger_pin == args.iwr6843_trigger_pin:
        parser.error(
            "--ops-trigger-pin must be a BCM GPIO 0-27 other than the sound-gate input "
            f"BCM{args.iwr6843_trigger_pin}"
        )
    if not PULSE_MS_RANGE[0] <= args.ops_trigger_pulse_ms <= PULSE_MS_RANGE[1]:
        parser.error(f"--ops-trigger-pulse-ms must be {PULSE_MS_RANGE[0]:g}-{PULSE_MS_RANGE[1]:g}")
    if not DEADLINE_MS_RANGE[0] <= args.ops_trigger_deadline_ms <= DEADLINE_MS_RANGE[1]:
        parser.error(
            f"--ops-trigger-deadline-ms must be {DEADLINE_MS_RANGE[0]:g}-{DEADLINE_MS_RANGE[1]:g}"
        )
    if args.ops_trigger_mode != "camera":
        return None
    if not args.camera_capture:
        parser.error("--ops-trigger-mode camera needs --camera-capture")
    if getattr(args, "shot_trigger", "sound") == "ti":
        # ti_split_check (_validate_shot_trigger_args) covers the deadline and the TI lead.
        return None
    if getattr(args, "shot_trigger", "sound") in ("ops-speed", "ops-speed-shadow"):
        # _validate_ops_speed_args refuses camera mode for the speed trigger.
        return None
    problem = camera_split_check(
        args.sound_pre_trigger, args.ops_trigger_deadline_ms, args.sample_rate
    )
    if problem is None:
        return None
    level, message = problem
    if level == "error":
        parser.error(f"--ops-trigger-mode camera: {message}")
    return message


def _apply_kld7_device_defaults(args, dev_root: Path = Path("/dev")) -> None:
    """Preserve kiosk symlink discovery while keeping CLI policy in the server."""
    vertical = dev_root / "kld7_vertical"
    horizontal = dev_root / "kld7_horizontal"
    if args.kld7 and args.kld7_port is None and vertical.exists():
        args.kld7_port = str(vertical)
    if args.kld7 and horizontal.exists():
        args.kld7_horizontal = True
    if args.kld7_horizontal and args.kld7_horizontal_port is None and horizontal.exists():
        args.kld7_horizontal_port = str(horizontal)


def main():
    """Run the server."""
    import argparse  # pylint: disable=import-outside-toplevel

    from .camera.retention import add_retention_arguments, start_retention_from_args

    parser = argparse.ArgumentParser(description="OpenFlight UI Server")
    parser.add_argument("--port", "-p", help="Serial port for radar")
    parser.add_argument(
        "--ops-baud",
        type=int,
        default=None,
        help=(
            "Target UART baud for the OPS243 on the GPIO header "
            f"(default {OPS243Radar.DEFAULT_UART_BAUD}). Only meaningful when "
            "--port is a UART device such as /dev/ttyAMA0; drop to 115200 if "
            "230400 proves unreliable on your board."
        ),
    )
    parser.add_argument(
        "--ops-fast-rearm",
        action="store_true",
        help=(
            "Re-arm the OPS243 after each dump with a 50 ms drain gap and 50 ms "
            "command settles instead of 200/100/100/150 ms (about 0.25 s less "
            "blind time per dump; the re-arm returns 0.35 s sooner, and the "
            "radar is armed from the final PA write). Off by default until "
            "validated on the hardware."
        ),
    )
    parser.add_argument("--mock", "-m", action="store_true", help="Run in mock mode without radar")
    parser.add_argument(
        "--mock-swing-speed",
        action="store_true",
        help="Run swing speed training mode with simulated reps and no OPS radar",
    )
    parser.add_argument(
        "--demo-interval-s",
        type=float,
        default=DEMO_INTERVAL_S,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    parser.add_argument(
        "--web-port", type=int, default=8080, help="Web server port (default: 8080)"
    )
    parser.add_argument(
        "--startup-status-file",
        default=None,
        help="Write structured initialization progress for the optional kiosk splash",
    )
    parser.add_argument(
        "--debug", "-d", action="store_true", help="Enable verbose FFT/CFAR debug output"
    )
    parser.add_argument(
        "--radar-log", action="store_true", help="Log raw radar data to console (Python logging)"
    )
    parser.add_argument(
        "--show-raw", action="store_true", help="Show raw radar readings in console (signed values)"
    )
    parser.add_argument(
        "--camera-capture",
        action="store_true",
        help="Enable high-speed camera rolling-buffer capture and replay",
    )
    parser.add_argument("--camera-capture-width", type=int, default=640)
    parser.add_argument("--camera-capture-height", type=int, default=400)
    parser.add_argument("--camera-capture-fps", type=float, default=300.0)
    parser.add_argument("--camera-capture-pre-ms", type=float, default=150.0)
    parser.add_argument("--camera-capture-post-ms", type=float, default=50.0)
    parser.add_argument(
        "--camera-capture-exposure-us",
        type=int,
        default=1000,
        help="Exposure seed used by the one-time startup calibration.",
    )
    parser.add_argument(
        "--camera-capture-gain",
        type=float,
        default=4.0,
        help="Analogue-gain seed used by the one-time startup calibration.",
    )
    parser.add_argument(
        "--camera-capture-mount-height-m",
        type=float,
        default=0.20955,
        help="Camera optical-center height above the hitting surface (default: 8.25 in).",
    )
    parser.add_argument(
        "--camera-capture-horizontal-offset-deg",
        type=float,
        default=0.0,
        help=(
            "Measured camera target-line correction added to horizontal launch angles; "
            "a target tapped in the Align view replaces it."
        ),
    )
    parser.add_argument(
        "--camera-capture-lateral-offset-m",
        type=float,
        default=0.0,
        help=(
            "Camera optical-center lateral position relative to radar center in meters; "
            "positive is target-right when looking downrange."
        ),
    )
    parser.add_argument(
        "--camera-capture-forward-offset-m",
        type=float,
        default=0.0,
        help=(
            "Camera optical-center position downrange of the radar center in meters "
            "(negative when the camera sits behind the radar)."
        ),
    )
    parser.add_argument(
        "--camera-capture-yaw-from-ball",
        action="store_true",
        help=(
            "Derive camera yaw from the teed ball for horizontal launch; the ball must sit "
            "on the radar centreline. Use with --camera-intrinsics."
        ),
    )
    parser.add_argument(
        "--camera-capture-roll-deg",
        type=float,
        default=0.0,
        help=(
            "Clockwise image-roll correction applied to camera preview and geometry "
            "without modifying saved raw frames. Set a measured roll (e.g. the plumb-line "
            "+1.35) together with a target tapped in the Align view: roll alone leaves the "
            "camera yaw uncorrected, and a roll change discards the saved target, so tap "
            "it again after restarting (docs/camera/README.md)."
        ),
    )
    parser.add_argument(
        "--camera-intrinsics",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Lens calibration JSON from scripts/hardware-test/calibrate_camera_intrinsics.py; "
            "replaces the ball-size focal estimate and image-centre principal point."
        ),
    )
    parser.add_argument(
        "--camera-capture-stream",
        choices=("raw", "main-y"),
        default="raw",
        help="Camera stream to persist (raw preserves OV9281 R8 detail; main-y is smaller).",
    )
    parser.add_argument(
        "--camera-capture-scaler-crop",
        default=None,
        help="Optional Picamera2 ScalerCrop as X,Y,W,H.",
    )
    parser.add_argument(
        "--camera-capture-rotate-180",
        action="store_true",
        help="Rotate saved camera frames 180 degrees.",
    )
    parser.add_argument(
        "--camera-capture-mirror-horizontal",
        action="store_true",
        help="Mirror saved frames left-to-right after mount rotation.",
    )
    from .camera.capture_runtime import (  # pylint: disable=import-outside-toplevel
        BUFFER_COUNT_RANGE,
        DEFAULT_BUFFER_COUNT,
    )

    parser.add_argument(
        "--camera-buffer-count",
        type=int,
        default=DEFAULT_BUFFER_COUNT,
        help=(
            f"Picamera2 request buffers, {BUFFER_COUNT_RANGE[0]}-{BUFFER_COUNT_RANGE[1]} "
            f"(default {DEFAULT_BUFFER_COUNT}, 27.8 ms at 288 fps). Each 640x400 buffer "
            "takes about 0.9 MB of CMA memory (512 KB raw + 384 KB YUV420); check the "
            "CMA pool (/proc/meminfo CmaFree) before raising it."
        ),
    )
    from .camera.ball_flight import (  # pylint: disable=import-outside-toplevel
        BALL_LOCATORS,
        DEFAULT_BALL_LOCATOR,
    )

    parser.add_argument(
        "--camera-ball-locator",
        choices=BALL_LOCATORS,
        default=DEFAULT_BALL_LOCATOR,
        help=(
            "How the camera start direction finds the resting ball: 'disappeared' (default) "
            "takes the spot that vanished at the strike inside the hitting zone, with "
            "clip-relative contrast gates, the pose from untee'd balls, the OPS motion gate "
            "and the OPS line-fit check; 'legacy' is the earlier blob detector and tracker "
            "(rollback)."
        ),
    )
    parser.add_argument(
        "--club-data",
        choices=CLUB_DATA_MODES,
        default="show",
        help=(
            "Calculated club data from the camera (path, attack angle, lofts, face, "
            "impact; 'berechnet'): 'show' (default) puts the gated values on the shot "
            "and updates the screens, 'log' only writes them to the session log. "
            "Never part of the carry, the spin axis or the simulator export."
        ),
    )
    parser.add_argument(
        "--golfer-handedness",
        choices=("right", "left"),
        default="right",
        help=(
            "Golfer handedness for the calculated club data (default right); a "
            "simulator that reports one (GSPro) overrides it. Left-handed golfers "
            "get no calculated face yet."
        ),
    )
    parser.add_argument(
        "--camera-cv-in-process",
        action="store_true",
        help=(
            "Run the per-shot camera computer vision in the server process, as before "
            "(rollback). By default it runs in one low-priority worker process, so it "
            "cannot slow the camera frame callback."
        ),
    )
    parser.add_argument(
        "--no-camera-lowlight-shadow",
        action="store_false",
        dest="camera_lowlight_shadow",
        help=(
            "Turn off the low-light camera start direction in shadow (flicker "
            "compensation and a relaxed ball gate, run in the CV worker after each "
            "shot is published and only written to the session log). On by default; "
            "it never changes a shot."
        ),
    )
    parser.add_argument(
        "--session-location",
        "-l",
        default="range",
        help="Location identifier for session logs (e.g., 'range', 'course', 'home')",
    )
    parser.add_argument(
        "--log-dir", help="Directory for session logs (default: ~/openflight_sessions)"
    )
    parser.add_argument(
        "--profiles-path",
        default=None,
        help=(
            "Path to profiles.json (default: OPENFLIGHT_PROFILES_PATH or "
            "~/.config/openflight/profiles.json)"
        ),
    )
    parser.add_argument(
        "--weather-path",
        default=None,
        help=(
            "Path to weather.json, the stored range location (default: "
            "OPENFLIGHT_WEATHER_PATH or ~/.config/openflight/weather.json)"
        ),
    )
    parser.add_argument(
        "--voice-callouts-path",
        default=None,
        help=(
            "Path to voice_callouts.json, the spoken shot callout setting (default: "
            "OPENFLIGHT_VOICE_CALLOUTS_PATH or ~/.config/openflight/voice_callouts.json)"
        ),
    )
    parser.add_argument("--no-logging", action="store_true", help="Disable session logging")
    parser.add_argument(
        "--hotspot-ssid",
        default=None,
        help="Wi-Fi name of this device's hotspot, shown on the pairing dialog",
    )
    parser.add_argument(
        "--hotspot-password",
        default=None,
        help="Wi-Fi password of this device's hotspot, shown on the pairing dialog",
    )
    _add_battery_arguments(parser)
    add_retention_arguments(parser)
    parser.add_argument(
        "--sim",
        action="store_true",
        help="Enable simulator connectors from config/sim.json (GSPro / OpenGolfSim / PAR-TEE). "
        "Off by default.",
    )
    _add_ballistics_arguments(parser)
    _add_air_density_arguments(parser)
    _add_typical_spin_scale_arguments(parser)
    _add_range_ball_drag_arguments(parser)
    parser.add_argument(
        "--trigger",
        choices=["sound", "speed"],
        default="sound",
        help="Trigger strategy (default: sound)",
    )
    parser.add_argument(
        "--swing-speed",
        action="store_true",
        help="Run club-only swing speed training mode (no impact or ball required)",
    )
    parser.add_argument(
        "--swing-speed-threshold",
        type=float,
        default=30.0,
        help="Outbound speed threshold that starts a swing speed rep (default: 30 mph)",
    )
    parser.add_argument(
        "--swing-speed-max",
        type=float,
        default=130.0,
        help="Maximum plausible swing speed accepted from OPS reports; use 0 to disable (default: 130 mph)",
    )
    parser.add_argument(
        "--swing-speed-min-readings",
        type=int,
        default=3,
        help="Minimum qualifying radar readings required to count a swing speed rep (default: 3)",
    )
    parser.add_argument(
        "--swing-speed-single-peak",
        type=float,
        default=60.0,
        help="Peak speed that can count as a swing from one radar reading (default: 60 mph)",
    )
    parser.add_argument(
        "--swing-speed-num-reports",
        type=int,
        default=8,
        help="Number of OPS speed candidates to report per sample cycle (default: 8)",
    )
    parser.add_argument(
        "--swing-speed-end-ms",
        type=float,
        default=1000.0,
        help="Milliseconds below threshold before ending a swing speed rep (default: 1000)",
    )
    parser.add_argument(
        "--swing-speed-cooldown-ms",
        type=float,
        default=750.0,
        help="Cooldown after a swing speed rep before accepting another (default: 750)",
    )
    parser.add_argument(
        "--swing-speed-rejected-cooldown-ms",
        type=float,
        default=100.0,
        help="Cooldown after an ignored short motion before re-arming (default: 100)",
    )
    parser.add_argument(
        "--sound-pre-trigger",
        type=int,
        default=16,
        help=(
            "Pre-trigger segments S#n, 0-32 "
            "(default: 16 = 50/50 split; a segment is 128 samples: "
            "4.27ms at 30ksps, 6.4ms at 20ksps)"
        ),
    )
    _add_trigger_gate_arguments(parser)
    _add_shot_trigger_arguments(parser)
    _add_ops_speed_arguments(parser)
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=30,
        help=(
            "Radar sample rate in ksps (default: 30 = 136.5ms buffer, unambiguous "
            "to 208mph). Lower = longer buffer: 20=204.8ms, 25=164ms, 27=152ms. "
            "Below 30 a ball faster than the rate's fold speed (20: 139mph) is "
            "unfolded up to 200mph"
        ),
    )
    parser.add_argument(
        "--iwr6843",
        action="store_true",
        help="Enable TI IWR6843 L3 capture and LCMF-v1 vertical launch angle",
    )
    parser.add_argument(
        "--inclinometer",
        action="store_true",
        help="Enable LIS3DH enclosure pitch compensation for IWR6843 tilt",
    )
    parser.add_argument(
        "--inclinometer-zero-offset",
        type=float,
        default=0.0,
        help="Degrees added to raw LIS3DH pitch (default: 0)",
    )
    parser.add_argument(
        "--iwr6843-port", default=None, help="TI serial port (auto-detect by default)"
    )
    parser.add_argument(
        "--iwr6843-usb-power-cycle",
        action="store_true",
        default=False,
        help=(
            "Recover a wedged TI radar by cutting USB power (Pi 5; needs sudo rights for "
            "/usr/sbin/uhubctl; the touchscreen restarts too)"
        ),
    )
    parser.add_argument(
        "--iwr6843-config",
        default="config/iwr6843_l3dump_wide_24f3ms_53bin_iq16.cfg",
        help="TI RF config matching the flashed L3 firmware",
    )
    parser.add_argument(
        "--iwr6843-cal",
        default="config/iwr6843_calibration_reference.json",
        help="TI complex array/range calibration JSON",
    )
    parser.add_argument(
        "--iwr6843-trigger-pin",
        type=int,
        default=17,
        help="BCM GPIO receiving the shared sound-trigger edge (default: 17)",
    )
    parser.add_argument(
        "--iwr6843-tee-m",
        type=float,
        default=1.575,
        help="Antenna-center to tee slant range in metres (default: 1.575)",
    )
    parser.add_argument(
        "--iwr6843-net-m",
        type=float,
        default=4.6,
        help="Antenna-center to net range in metres (default: 4.6)",
    )
    parser.add_argument(
        "--iwr6843-tilt-deg",
        type=float,
        default=None,
        help=(
            "Override mount tilt from the TI calibration JSON; a saved phone tilt "
            "calibration (~/.config/openflight/iwr6843_phone_orientation.json) outranks it"
        ),
    )
    parser.add_argument(
        "--iwr6843-radar-height-m",
        type=float,
        default=None,
        help="Override antenna-center height from the TI calibration JSON",
    )
    parser.add_argument(
        "--iwr6843-ball-height-m",
        type=float,
        default=0.040,
        help="Ball-center height above the floor/mat (default: 0.040)",
    )
    parser.add_argument(
        "--iwr6843-tx-order",
        choices=("auto", "normal", "reversed"),
        default="auto",
        help="TI TDM chirp order; auto reads the chirp masks from the cfg",
    )
    parser.add_argument(
        "--iwr6843-capture-timeout",
        type=float,
        default=16.0,
        help=(
            "Maximum seconds an OPS shot waits for its TI UART dump "
            "(default: 16). A 25-frame ring is 763,200 bytes, which takes "
            "7.4 s at the saturated 1,041,667 baud link."
        ),
    )
    parser.add_argument(
        "--iwr6843-output-dir",
        default=None,
        help=(
            "Raw TI dump directory, used when --debug or cloud raw uploads "
            "(openflight-cloud raw on) are enabled (default: <session-log-dir>/iwr6843)"
        ),
    )
    parser.add_argument(
        "--iwr6843-azimuth-offset-deg",
        type=float,
        default=0.0,
        help=(
            "Azimuth of the radar boresight relative to the target line, in degrees. "
            "Positive means boresight points right of the target line. Added to "
            "measured horizontal launch and club path; 0 reports both relative "
            "to boresight."
        ),
    )
    parser.add_argument(
        "--iwr6843-horizontal-phase-reference-rad",
        type=float,
        default=None,
        help=(
            "Static target-line phase measured by horizontal aim calibration. "
            "Subtracted from the TX2 horizontal proxy before angle conversion."
        ),
    )
    parser.add_argument(
        "--iwr6843-horizontal-trajectory-fit",
        action="store_true",
        help=(
            "Report horizontal launch from the lateral ball path anchored at the "
            "tee instead of the radar bearing. Default off; needs a calibrated "
            "--iwr6843-horizontal-phase-reference-rad."
        ),
    )
    parser.add_argument(
        "--iwr6843-tee-lateral-m",
        type=float,
        default=None,
        help=(
            "Signed lateral ball position relative to the TI antenna centre, "
            "positive = right seen from behind the radar (default: -0.072). "
            "Used only by --iwr6843-horizontal-trajectory-fit."
        ),
    )
    parser.add_argument(
        "--kld7",
        action="store_true",
        help="[DEPRECATED] Enable K-LD7 vertical angle radar (launch angle)",
    )
    parser.add_argument(
        "--kld7-port",
        default=None,
        help="K-LD7 vertical serial port (auto-detect if not specified)",
    )
    parser.add_argument(
        "--kld7-angle-offset",
        type=float,
        default=1.5,
        help=(
            "K-LD7 vertical boresight offset in degrees. Not user-measurable "
            "without a corner reflector; 1.5 is the calibrated default for the "
            "standard mount (default: 1.5)"
        ),
    )
    parser.add_argument(
        "--calculated-spin",
        action="store_true",
        help=(
            "Replace radar-measured spin with the kinematic estimate "
            "(170*v*sin(LA)^1.2) when the launch angle was measured. The 24 GHz "
            "OPS return carries no usable spin line (see "
            "src/openflight/spin_estimate.py); the measured value is kept in "
            "spin_rpm_measured for offline scoring"
        ),
    )
    parser.add_argument(
        "--kld7-mount-tilt",
        type=float,
        default=os.getenv("KLD7_MOUNT_TILT"),
        help=(
            "K-LD7 vertical radar mount tilt in degrees. REQUIRED with --kld7 — "
            "measure it with a phone inclinometer against the radar face; there is "
            "no default because a wrong tilt silently corrupts the launch angle"
        ),
    )
    parser.add_argument(
        "--kld7-ball-distance",
        type=float,
        default=5.0,
        help="Radar-to-tee distance in feet (default: 5.0)",
    )
    parser.add_argument(
        "--net-distance",
        dest="net_distance",
        type=float,
        default=10.0,
        help=(
            "Ball-to-net/screen distance in feet (two_ray). For nets beyond the "
            "~11ft FSK range wrap, far-flight frames are de-aliased and kept "
            "instead of dropped (default: 10.0; nets at/inside the wrap are "
            "unaffected)."
        ),
    )
    parser.add_argument(
        "--kld7-radar-height-inches",
        dest="kld7_radar_height_inches",
        type=float,
        default=4.0,
        help=(
            "K-LD7 radar height above the ball in inches, used by the ball-speed "
            "cosine correction geometry (default: 4.0)"
        ),
    )
    parser.add_argument(
        "--kld7-vertical-raw",
        dest="kld7_vertical_raw",
        action="store_true",
        help=(
            "TEST MODE: show the raw vertical launch angle for every shot the "
            "estimator produces, bypassing all display guardrails (plausibility, "
            "soft-lane, estimator-agreement, confidence floor). Default off."
        ),
    )
    parser.add_argument(
        "--kld7-horizontal",
        action="store_true",
        help="[DEPRECATED] Enable K-LD7 horizontal angle radar (club path)",
    )
    parser.add_argument("--kld7-horizontal-port", default=None, help="K-LD7 horizontal serial port")
    parser.add_argument(
        "--kld7-horizontal-offset",
        type=float,
        default=0.0,
        help="K-LD7 horizontal angle offset in degrees (default: 0.0)",
    )
    args = parser.parse_args()
    _apply_kld7_device_defaults(args)

    # Mount tilt cannot be defaulted safely (a wrong value silently biases the
    # launch angle), so require it whenever the K-LD7 radars are enabled.
    if args.kld7 and args.kld7_mount_tilt is None:
        parser.error("--kld7-mount-tilt is required when --kld7 is passed")
    if args.mock_swing_speed:
        args.mock = True
        args.swing_speed = True
    elif args.swing_speed and args.mock:
        parser.error(
            "--swing-speed requires real OPS243 radar hardware; use --mock-swing-speed for UI testing"
        )

    if not 0 < args.demo_interval_s < math.inf:
        parser.error("--demo-interval-s must be a positive, finite number of seconds")
    trigger_gate_split_warning = _validate_trigger_gate_args(parser, args)
    if trigger_gate_split_warning:
        print(f"WARNING: --ops-trigger-mode camera: {trigger_gate_split_warning}")
    ti_split_warning = _validate_shot_trigger_args(parser, args)
    if ti_split_warning:
        print(f"WARNING: --shot-trigger ti: {ti_split_warning}")
        trigger_gate_split_warning = ti_split_warning
    ops_speed_split_warning = _validate_ops_speed_args(parser, args)
    if ops_speed_split_warning:
        print(f"WARNING: --shot-trigger {args.shot_trigger}: {ops_speed_split_warning}")
        trigger_gate_split_warning = ops_speed_split_warning
    if args.iwr6843 and args.kld7:
        parser.error("--iwr6843 and vertical --kld7 cannot both own launch angle")
    if args.iwr6843 and args.kld7_horizontal:
        parser.error("--iwr6843 and horizontal --kld7 cannot both own club path")
    if args.inclinometer and not args.iwr6843:
        parser.error("--inclinometer requires --iwr6843")
    if args.iwr6843 and args.mock:
        parser.error("--iwr6843 cannot be used with --mock")
    if args.camera_capture and args.mock:
        parser.error("--camera-capture cannot be used with --mock")
    if args.iwr6843 and (args.iwr6843_tee_m <= 0 or args.iwr6843_net_m <= 0):
        parser.error("--iwr6843-tee-m and --iwr6843-net-m must be positive")
    if args.iwr6843_tee_lateral_m is not None and abs(args.iwr6843_tee_lateral_m) >= 1.0:
        parser.error("--iwr6843-tee-lateral-m must be within +/-1 m")
    if args.camera_capture and (
        args.camera_capture_width <= 0
        or args.camera_capture_height <= 0
        or args.camera_capture_fps <= 0
        or args.camera_capture_pre_ms <= 0
        or args.camera_capture_post_ms <= 0
        or args.camera_capture_exposure_us <= 0
        or args.camera_capture_gain <= 0
        or args.camera_capture_mount_height_m <= 0
    ):
        parser.error("--camera-capture dimensions, timing, exposure, and gain must be positive")
    if not BUFFER_COUNT_RANGE[0] <= args.camera_buffer_count <= BUFFER_COUNT_RANGE[1]:
        parser.error(
            f"--camera-buffer-count must be {BUFFER_COUNT_RANGE[0]}-{BUFFER_COUNT_RANGE[1]}"
        )
    camera_capture_scaler_crop = None
    if args.camera_capture_scaler_crop:
        try:
            from .camera.capture_runtime import parse_scaler_crop

            camera_capture_scaler_crop = parse_scaler_crop(args.camera_capture_scaler_crop)
        except ValueError as exc:
            parser.error(f"--camera-capture-scaler-crop: {exc}")
    global club_data_mode, golfer_handedness  # pylint: disable=global-statement
    global camera_lowlight_shadow_enabled  # pylint: disable=global-statement
    club_data_mode = args.club_data
    camera_lowlight_shadow_enabled = args.camera_lowlight_shadow
    golfer_handedness = args.golfer_handedness
    if args.camera_intrinsics is not None:
        from .camera.intrinsics import load_camera_intrinsics

        try:
            loaded_intrinsics = load_camera_intrinsics(args.camera_intrinsics)
        except ValueError as exc:
            parser.error(f"--camera-intrinsics: {exc}")
        if (
            intrinsics_for_mode(
                loaded_intrinsics, args.camera_capture_width, args.camera_capture_height, 0
            )
            is None
        ):
            parser.error(
                "--camera-intrinsics was calibrated for "
                f"{loaded_intrinsics.image_width_px}x{loaded_intrinsics.image_height_px}, "
                f"camera captures {args.camera_capture_width}x{args.camera_capture_height}"
            )
        global camera_intrinsics  # pylint: disable=global-statement
        camera_intrinsics = loaded_intrinsics
        print(
            f"Camera intrinsics: fx={camera_intrinsics.fx:.1f} fy={camera_intrinsics.fy:.1f} "
            f"cx={camera_intrinsics.cx:.1f} cy={camera_intrinsics.cy:.1f} "
            f"(RMS {camera_intrinsics.rms_reprojection_px:.2f} px)"
        )
    # The radar can only be moved to a rate it has an API command for, so an
    # unsupported value is refused by the hardware and leaves the link at
    # whatever answered -- a silent slow link, which presents as an
    # unresponsive app rather than a bad flag. Fail at the CLI instead.
    if args.ops_baud is not None and args.ops_baud not in UART_BAUD_COMMANDS:
        supported = ", ".join(str(b) for b in sorted(UART_BAUD_COMMANDS))
        parser.error(f"--ops-baud must be one of {supported} (got {args.ops_baud})")
    global ballistics_enabled
    global battery_provider
    global hotspot_ssid, hotspot_password  # pylint: disable=global-statement
    global profile_store
    global ball_speed_correction_enabled
    global ball_speed_correction_distance_ft
    global ball_speed_correction_ball_above_radar_ft
    # Cosine correction rides on whichever vertical radar supplies launch.
    # LCMF itself always receives the original OPS radial speed first.
    ball_speed_correction_enabled = args.kld7 or args.iwr6843
    ball_speed_correction_distance_ft = args.kld7_ball_distance
    ball_speed_correction_ball_above_radar_ft = -args.kld7_radar_height_inches / 12.0
    global _VERTICAL_RADAR_GATE_BYPASS
    _VERTICAL_RADAR_GATE_BYPASS = args.kld7_vertical_raw
    global calculated_spin_enabled
    calculated_spin_enabled = args.calculated_spin
    ballistics_enabled = args.ballistics
    global air_density_kg_m3
    air_density_kg_m3 = args.air_density
    global typical_spin_scale
    typical_spin_scale = args.typical_spin_scale
    global typical_spin_table
    typical_spin_table = args.typical_spin_table
    global range_ball_drag_slope
    range_ball_drag_slope = args.range_ball_drag_slope
    battery_provider = args.battery
    hotspot_ssid = args.hotspot_ssid
    hotspot_password = args.hotspot_password
    profile_store = ProfileStore(args.profiles_path)
    global weather_store  # pylint: disable=global-statement
    weather_store = WeatherStore(args.weather_path)
    global voice_callout_store  # pylint: disable=global-statement
    voice_callout_store = VoiceCalloutStore(args.voice_callouts_path)
    global demo_session  # pylint: disable=global-statement
    demo_session = DemoSession(
        rng=random.Random(), finalize=_finalize_demo_shot_metrics, interval_s=args.demo_interval_s
    )
    startup_status = StartupStatusReporter(
        args.startup_status_file,
        configured_startup_components(
            mock=args.mock,
            camera=args.camera_capture,
            iwr6843=args.iwr6843,
            inclinometer=args.inclinometer,
            kld7=args.kld7,
            kld7_horizontal=args.kld7_horizontal,
            battery=bool(args.battery),
            simulators=args.sim,
        ),
    )
    startup_status.start("server", "Preparing OpenFlight server")

    # Configure logging - always show INFO and above for openflight modules
    # This ensures trigger events and important messages are visible
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    # Set rolling buffer logger to INFO so trigger events are visible
    logging.getLogger("openflight.rolling_buffer").setLevel(logging.INFO)
    logging.getLogger("openflight.rolling_buffer.trigger").setLevel(logging.INFO)
    logging.getLogger("openflight.rolling_buffer.monitor").setLevel(logging.INFO)

    print("=" * 50)
    print("  OpenFlight UI Server")
    print("=" * 50)
    print()

    # Initialize session logger (enabled for both real and mock modes)
    if not args.no_logging:
        log_dir = Path(args.log_dir) if args.log_dir else None
        init_session_logger(log_dir=log_dir, location=args.session_location, enabled=True)
        print(f"Session logging enabled (location: {args.session_location})")
    else:
        init_session_logger(enabled=False)
        print("Session logging DISABLED")

    try:
        get_device_secret().value()
    except OSError as error:
        logger.error(
            "[PAIRING] Device secret unavailable; only this Pi's kiosk can connect: %s", error
        )

    if ballistics_enabled:
        print(
            "Ballistic carry model: ENABLED (simulator + drag/Magnus, "
            f"air density {air_density_kg_m3:.3f} kg/m³ without fresh weather, "
            f"typical spin table {typical_spin_table}, "
            f"typical spin x{typical_spin_scale:g} for irons/wedges, "
            + (
                f"range-ball drag slope {range_ball_drag_slope:g} (UNVALIDATED))"
                if range_ball_drag_slope
                else "range-ball drag off)"
            )
        )
    else:
        print("Ballistic carry model: DISABLED (table fallback for all shots)")
        if typical_spin_scale != 1.0:
            print(
                f"WARNING: typical spin scale x{typical_spin_scale:g} ignored: "
                "the table fallback uses its speed-model spin"
            )
        if typical_spin_table != DEFAULT_TYPICAL_SPIN_TABLE:
            print(
                f"WARNING: typical spin table {typical_spin_table} ignored: "
                "the table fallback uses its speed-model spin"
            )
        if range_ball_drag_slope:
            print(
                f"WARNING: range-ball drag slope {range_ball_drag_slope:g} ignored: "
                "the table fallback has no flight model"
            )

    # Configure radar logging if requested
    if args.radar_log:
        logging.basicConfig(
            level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        radar_logger = logging.getLogger("ops243")
        radar_raw_logger = logging.getLogger("ops243.raw")
        radar_logger.setLevel(logging.DEBUG)
        radar_raw_logger.setLevel(logging.DEBUG)
        print("Radar raw logging ENABLED - all readings will be logged")

    # Enable raw reading console output if requested
    if args.show_raw:
        set_show_raw_readings(True)
        print("Raw radar readings display ENABLED - signed speed values will be shown")

    ops_speed_refusal = _ops_speed_preflight(args)
    if ops_speed_refusal:
        startup_status.start("ops", "OPS speed trigger refused; using the microphone")
        if trigger_gate_split_warning == ops_speed_split_warning:
            trigger_gate_split_warning = None  # it was about the speed-trigger split
    ops_speed = getattr(args, "ops_speed_config", None)
    if ops_speed is not None:
        init_ops_speed_witness(ops_speed)

    # Start the monitor
    # Build trigger-specific kwargs (pre_trigger_segments always passed)
    trigger_kwargs = {"pre_trigger_segments": args.sound_pre_trigger}
    if ops_speed is not None:
        trigger_kwargs["config"] = ops_speed
    swing_speed_kwargs = {
        "trigger_threshold_mph": args.swing_speed_threshold,
        "max_speed_mph": None if args.swing_speed_max <= 0 else args.swing_speed_max,
        "min_readings": args.swing_speed_min_readings,
        "single_reading_peak_mph": args.swing_speed_single_peak,
        "num_reports": args.swing_speed_num_reports,
        "end_quiet_ms": args.swing_speed_end_ms,
        "cooldown_ms": args.swing_speed_cooldown_ms,
        "rejected_cooldown_ms": args.swing_speed_rejected_cooldown_ms,
    }

    if args.camera_capture:
        startup_status.start("camera", "Connecting high-speed camera")
        camera_capture_base = (
            Path(args.log_dir).expanduser() if args.log_dir else Path.home() / "openflight_sessions"
        )
        camera_capture_output_dir = camera_capture_base / args.session_location / "camera"
        if not init_camera_capture(
            output_dir=camera_capture_output_dir,
            gpio_pin=args.iwr6843_trigger_pin,
            width=args.camera_capture_width,
            height=args.camera_capture_height,
            fps=args.camera_capture_fps,
            pre_ms=args.camera_capture_pre_ms,
            post_ms=args.camera_capture_post_ms,
            exposure_us=args.camera_capture_exposure_us,
            gain=args.camera_capture_gain,
            mount_height_m=args.camera_capture_mount_height_m,
            lateral_offset_m=args.camera_capture_lateral_offset_m,
            forward_offset_m=args.camera_capture_forward_offset_m,
            yaw_from_ball=args.camera_capture_yaw_from_ball,
            horizontal_offset_deg=args.camera_capture_horizontal_offset_deg,
            roll_correction_deg=args.camera_capture_roll_deg,
            stream=args.camera_capture_stream,
            rotate_180=args.camera_capture_rotate_180,
            mirror_horizontal=args.camera_capture_mirror_horizontal,
            scaler_crop=camera_capture_scaler_crop,
            use_gpio_trigger=not args.iwr6843 and args.ops_trigger_pin is None,
            buffer_count=args.camera_buffer_count,
            cv_in_process=args.camera_cv_in_process,
            ball_locator=args.camera_ball_locator,
        ):
            print("Camera capture unavailable - running without high-speed camera capture")
            startup_status.skip("camera", "High-speed camera unavailable; continuing")
        else:
            print(f"Camera capture enabled: {camera_capture_output_dir}")
            startup_status.ready("camera", "High-speed camera connected")

    retention_base = (
        Path(args.log_dir).expanduser() if args.log_dir else Path.home() / "openflight_sessions"
    )
    start_retention_from_args(
        args,
        retention_base / args.session_location / "camera",
        retention_base,
        busy=_shot_capture_in_progress,
    )

    if args.ops_trigger_pin is not None:
        if not init_trigger_gate(
            pin=args.ops_trigger_pin,
            mode=args.ops_trigger_mode,
            pulse_ms=args.ops_trigger_pulse_ms,
            deadline_ms=args.ops_trigger_deadline_ms,
            edge_pin=args.iwr6843_trigger_pin,
            own_edge_pin=not args.iwr6843,
            split_warning=trigger_gate_split_warning,
            edge_source="ti" if args.shot_trigger == "ti" else "sound",
            ti_anchor=getattr(args, "ti_anchor", None),
            ops_speed=ops_speed,
            sample_rate_ksps=args.sample_rate,
        ):
            print("ERROR: --ops-trigger-pin requested but the trigger gate failed. Exiting.")
            _cleanup_hardware_for_shutdown()
            sys.exit(1)
        print(
            f"OPS trigger gate: BCM{args.ops_trigger_pin} -> HOST_INT, {args.ops_trigger_mode} mode"
        )

    if args.iwr6843:
        startup_status.start("ti", "Connecting TI radar")
        iwr_output_dir = (
            Path(args.iwr6843_output_dir).expanduser()
            if args.iwr6843_output_dir
            else (
                Path(args.log_dir).expanduser()
                if args.log_dir
                else Path.home() / "openflight_sessions"
            )
            / "iwr6843"
        )
        usb_power_cycler = _iwr6843_usb_power_cycler() if args.iwr6843_usb_power_cycle else None
        iwr6843_init_kwargs = {
            "port": args.iwr6843_port,
            "config_path": args.iwr6843_config,
            "calibration_path": args.iwr6843_cal,
            "output_dir": iwr_output_dir,
            "trigger_pin": args.iwr6843_trigger_pin,
            "tee_range_m": args.iwr6843_tee_m,
            "net_range_m": args.iwr6843_net_m,
            "tx_order": args.iwr6843_tx_order,
            "capture_timeout_s": args.iwr6843_capture_timeout,
            "tilt_deg": args.iwr6843_tilt_deg,
            "radar_height_m": args.iwr6843_radar_height_m,
            "ball_height_m": args.iwr6843_ball_height_m,
            "azimuth_offset_deg": args.iwr6843_azimuth_offset_deg,
            "horizontal_phase_reference_rad": args.iwr6843_horizontal_phase_reference_rad,
            # Raw cloud uploads need the dumps on disk to send them.
            "save_dumps": args.debug or _cloud_raw_uploads_enabled(),
            "horizontal_trajectory_fit": args.iwr6843_horizontal_trajectory_fit,
            "tee_lateral_m": args.iwr6843_tee_lateral_m,
            "usb_power_cycler": usb_power_cycler,
            "shot_trigger": args.shot_trigger,
            "self_trigger_offset_m": args.iwr6843_self_trigger_offset_m,
            "self_trigger_snr": args.iwr6843_self_trigger_snr,
            "self_trigger_bin": args.iwr6843_self_trigger_bin,
            "shadow_holdoff_ms": args.ti_shadow_holdoff_ms or 0.0,
            "ops_speed": ops_speed,
            "recovery_guard": (
                _make_iwr6843_recovery_guard(kld7=args.kld7 or args.kld7_horizontal)
                if usb_power_cycler is not None
                else None
            ),
        }
        iwr6843_ready = init_iwr6843(**iwr6843_init_kwargs)
        if (
            not iwr6843_ready
            and usb_power_cycler is not None
            and _iwr6843_error_warrants_power_cycle(iwr6843_runtime_config.get("error"))
        ):
            startup_status.start("ti", "TI radar not responding – restarting its USB power")
            iwr6843_ready = _recover_iwr6843_at_startup(
                usb_power_cycler, args.iwr6843_port, iwr6843_init_kwargs
            )
        if iwr6843_ready:
            calibration = iwr6843_runtime.calibration
            ball_speed_correction_distance_ft = args.iwr6843_tee_m * 3.28084
            # Base (mat) ball height: a teed driver changes this cosine by <= 0.2 %.
            ball_speed_correction_ball_above_radar_ft = (
                calibration.tee_ball_height_m - calibration.radar_height_m
            ) * 3.28084
            print(
                "IWR6843 enabled (LCMF-v1 launch angle, "
                + (
                    f"BCM{_ti_edge_pin(args, ops_speed)}"
                    if args.shot_trigger != "ti"
                    else "self-trigger, no microphone"
                )
                + f", {iwr6843_runtime.tx_order} TX order"
                + (
                    f", {args.shot_trigger} {iwr6843_runtime_config['self_trigger']['command']!r}"
                    if iwr6843_runtime_config.get("self_trigger")
                    else ""
                )
                + (
                    f", l3dump {iwr6843_runtime_config['freeze_delay_ms']:g} ms after pin 1"
                    if iwr6843_runtime_config.get("freeze_delay_ms")
                    else ""
                )
                + ")"
            )
            if args.debug:
                print(f"IWR6843 raw dumps enabled: {iwr_output_dir}")
            startup_status.ready("ti", "TI radar connected")
        else:
            startup_status.error(
                "ti",
                "TI radar failed to initialize",
                _iwr6843_startup_recovery(iwr6843_runtime_config.get("error")),
            )
            print("ERROR: IWR6843 requested but failed to initialize. Exiting.")
            _cleanup_hardware_for_shutdown()
            sys.exit(1)

    if ops_speed is not None:
        if not init_ops_speed_listener(ops_speed, sound_pin=args.iwr6843_trigger_pin):
            print(
                f"ERROR: --shot-trigger {args.shot_trigger} input on its second pin failed. "
                "Exiting."
            )
            _cleanup_hardware_for_shutdown()
            sys.exit(1)
        print(
            f"OPS speed trigger: {ops_speed.mode}, J3 pin 1 on BCM{ops_speed.pin}, "
            f"ST-{ops_speed.threshold_mph} SM{ops_speed.magnitude} "
            f"S#{ops_speed.pre_trigger_segments}"
        )

    configure_camera_tee_zone()
    restore_camera_marks()

    if args.inclinometer:
        startup_status.start("inclinometer", "Connecting inclinometer")
        if not init_inclinometer(zero_offset_deg=args.inclinometer_zero_offset):
            print("WARNING: Inclinometer unavailable; continuing with configured IWR6843 tilt")
            startup_status.skip("inclinometer", "Inclinometer unavailable; continuing")
        else:
            startup_status.ready("inclinometer", "Inclinometer connected")
    if args.iwr6843:
        _warn_if_phone_tilt_inclinometer_mismatch(inclinometer_service is not None)

    # Initialize K-LD7 angle radars (if enabled)
    if args.kld7:
        startup_status.start("kld7_vertical", "Connecting K-LD7 launch radar")
        if init_kld7(
            port=args.kld7_port,
            orientation="vertical",
            angle_offset_deg=args.kld7_angle_offset,
            base_freq=0,
            mount_tilt_deg=args.kld7_mount_tilt,
            ball_distance_ft=args.kld7_ball_distance,
            vertical_flight_window_net_distance_ft=args.net_distance,
        ):
            offset_str = (
                f", offset: {args.kld7_angle_offset:+.1f}°" if args.kld7_angle_offset else ""
            )
            print(f"K-LD7 vertical radar enabled (launch angle{offset_str})")
            startup_status.ready("kld7_vertical", "K-LD7 launch radar connected")
        else:
            startup_status.error(
                "kld7_vertical",
                "K-LD7 launch radar failed to connect",
                "Check the K-LD7 USB connection and power, then relaunch OpenFlight.",
            )
            print("ERROR: K-LD7 vertical requested but failed to connect. Exiting.")
            _cleanup_hardware_for_shutdown()
            sys.exit(1)

    if args.kld7_horizontal:
        startup_status.start("kld7_horizontal", "Connecting K-LD7 path radar")
        if init_kld7(
            port=args.kld7_horizontal_port,
            orientation="horizontal",
            angle_offset_deg=args.kld7_horizontal_offset,
            base_freq=2,
        ):
            offset_str = (
                f", offset: {args.kld7_horizontal_offset:+.1f}°"
                if args.kld7_horizontal_offset
                else ""
            )
            print(f"K-LD7 horizontal radar enabled (club path{offset_str})")
            startup_status.ready("kld7_horizontal", "K-LD7 path radar connected")
        else:
            startup_status.error(
                "kld7_horizontal",
                "K-LD7 path radar failed to connect",
                "Check the K-LD7 USB connection and power, then relaunch OpenFlight.",
            )
            print("ERROR: K-LD7 horizontal requested but failed to connect. Exiting.")
            _cleanup_hardware_for_shutdown()
            sys.exit(1)

    monitor_component = "monitor" if args.mock else "ops"
    monitor_label = "shot simulator" if args.mock else "OPS radar"
    startup_status.start(monitor_component, f"Starting {monitor_label}")
    try:
        start_monitor(
            port=args.port,
            mock=args.mock,
            trigger_type="ops-speed" if ops_speed is not None else args.trigger,
            debug=args.debug,
            trigger_kwargs=trigger_kwargs,
            sample_rate_ksps=args.sample_rate,
            swing_speed_mode=args.swing_speed,
            swing_speed_kwargs=swing_speed_kwargs,
            ops_baud=args.ops_baud,
            ops_fast_rearm=args.ops_fast_rearm,
        )
    except Exception:
        monitor_recovery = (
            "Relaunch OpenFlight and check the terminal log."
            if args.mock
            else "Check the OPS radar USB and power connections, then relaunch OpenFlight."
        )
        startup_status.error(
            monitor_component,
            f"{'Shot simulator' if args.mock else 'OPS radar'} failed to initialize",
            monitor_recovery,
        )
        _cleanup_hardware_for_shutdown()
        raise
    startup_status.ready(
        monitor_component,
        f"{monitor_label.capitalize()} ready"
        + (" (speed trigger refused: microphone trigger)" if ops_speed_refusal else ""),
    )

    if battery_provider:
        startup_status.start("battery", "Starting power monitor")
        start_power_monitor(battery_provider)
        print(f"Battery monitoring: ENABLED ({battery_provider})")
        startup_status.ready("battery", "Power monitor ready")

    # Simulator connectors (off unless --sim). Started after the monitor exists
    # so inbound club updates can call monitor.set_club().
    global sim_connectors  # pylint: disable=global-statement
    if args.sim:
        startup_status.start("simulators", "Connecting golf simulators")
    sim_cfgs = load_sim_config() if args.sim else []
    sim_connectors = build_connectors(
        sim_cfgs, on_status=_sim_on_status, on_inbound=_sim_on_inbound
    )
    for connector in sim_connectors:
        connector.start()
        print(f"Simulator connector enabled: {connector.name} -> {connector.host}:{connector.port}")
    if args.sim and not sim_connectors:
        print("Simulator connectors enabled (--sim) but none are enabled in config/sim.json")
        startup_status.skip("simulators", "No simulator connections are configured")
    elif args.sim:
        startup_status.ready("simulators", "Simulator connections started")

    if args.mock:
        print("Running in MOCK mode - no radar required")
        print("Simulate shots via WebSocket or API")
    if args.swing_speed:
        print("Running in SWING SPEED mode - no ball impact trigger required")

    print(f"Server starting at http://{args.host}:{args.web_port}")
    print()
    startup_status.start("server", "Starting OpenFlight server")

    try:
        # Note: Flask debug mode (reloader) is disabled to prevent duplicate processes
        # fighting over the serial port. OpenFlight --debug enables verbose logging only.
        socketio.run(
            app, host=args.host, port=args.web_port, debug=False, allow_unsafe_werkzeug=True
        )
    finally:
        _cleanup_hardware_for_shutdown()


if __name__ == "__main__":
    main()
