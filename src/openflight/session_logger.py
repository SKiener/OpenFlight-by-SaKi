"""
Session logging for OpenFlight field testing.

Provides structured logging of all radar data, shots, and metrics
for analysis and debugging.
"""

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .launch_monitor import Shot

# Version of the session JSONL format itself. Bump on breaking changes to
# entry structure; additive changes (new fields, new entry types) do not
# require a bump. Consumed by offline analysis and (eventually) cloud sync.
SESSION_FORMAT_VERSION = 2


@dataclass
class SessionMetadata:
    """Metadata about a logging session."""

    session_id: str
    start_time: str
    radar_port: Optional[str]
    firmware_version: Optional[str]
    camera_enabled: bool
    camera_model: Optional[str]
    config: Dict[str, Any]
    mode: str  # "rolling-buffer" or "mock"
    trigger_type: Optional[str]  # For rolling-buffer mode: "sound" or "speed"
    # Globally unique session identity for cloud sync dedupe. The
    # timestamp-based session_id stays for filenames and display; this
    # UUID travels inside the data so renamed/copied session files keep
    # their identity (see docs/cloud-sync-design.md).
    session_uuid: str = ""
    format_version: int = SESSION_FORMAT_VERSION
    app_version: str = ""


class SessionLogger:
    """
    Comprehensive session logger for field testing.

    Creates structured log files with semantic naming:
    - session_YYYYMMDD_HHMMSS_<location>.jsonl - Main session log (JSON lines)
    - radar_raw_YYYYMMDD_HHMMSS.log - Raw radar serial data

    Log entry types:
    - session_start: Session metadata
    - session_end: Session summary
    - shot_detected: A shot was recorded
    - config_change: Radar configuration changed
    - error: Processing failures (component, context, optional exception metadata)
    """

    DEFAULT_LOG_DIR = Path.home() / "openflight_sessions"

    def __init__(
        self, log_dir: Optional[Path] = None, location: str = "range", enabled: bool = True
    ):
        """
        Initialize session logger.

        Args:
            log_dir: Directory for log files (default: ~/openflight_sessions)
            location: Location identifier for file naming (e.g., "range", "course", "home")
            enabled: Whether logging is enabled
        """
        self.log_dir = Path(log_dir) if log_dir else self.DEFAULT_LOG_DIR
        self.location = location
        self.enabled = enabled

        self._session_id: Optional[str] = None
        self._session_file: Optional[Any] = None
        self._session_path: Optional[Path] = None
        self._raw_path: Optional[Path] = None

        # Serializes all access to ``_session_file``. The log_* methods are
        # called concurrently from the OPS243 capture thread, the K-LD7
        # stream thread, and Flask-SocketIO handlers; without this lock,
        # large entries' writes interleave (corrupting the JSONL replay
        # corpus) and a write can race end_session() closing the file.
        self._write_lock = threading.Lock()

        # Counters for session summary
        self._stats = {
            "shots_detected": 0,
            "errors": 0,
        }

        # Setup Python logger for raw radar data
        self._raw_logger = logging.getLogger("ops243.raw")
        self._radar_logger = logging.getLogger("ops243")

    def start_session(
        self,
        radar_port: Optional[str] = None,
        firmware_version: Optional[str] = None,
        camera_enabled: bool = False,
        camera_model: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        mode: str = "rolling-buffer",
        trigger_type: Optional[str] = None,
    ) -> str:
        """
        Start a new logging session.

        Args:
            radar_port: Serial port for radar
            firmware_version: Radar firmware version
            camera_enabled: Whether camera is enabled
            camera_model: Camera/YOLO model being used
            config: Current radar configuration
            mode: Radar mode ("rolling-buffer" or "mock")
            trigger_type: Trigger strategy for rolling-buffer mode

        Returns:
            Session ID
        """
        if not self.enabled:
            return ""

        # Create log directory
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Generate session ID and filenames
        timestamp = datetime.now()
        self._session_id = timestamp.strftime("%Y%m%d_%H%M%S")

        # Semantic file naming: session_DATE_TIME_LOCATION.jsonl
        session_filename = f"session_{self._session_id}_{self.location}.jsonl"
        raw_filename = f"radar_raw_{self._session_id}.log"

        self._session_path = self.log_dir / session_filename
        self._raw_path = self.log_dir / raw_filename

        # Open log files
        self._session_file = open(self._session_path, "w")

        # Setup raw radar logging to file
        self._setup_raw_logging()

        # Reset stats
        self._stats = {k: 0 for k in self._stats}

        # Write session start entry
        metadata = SessionMetadata(
            session_id=self._session_id,
            start_time=timestamp.isoformat(),
            radar_port=radar_port,
            firmware_version=firmware_version,
            camera_enabled=camera_enabled,
            camera_model=camera_model,
            config=config or {},
            mode=mode,
            trigger_type=trigger_type,
            session_uuid=str(uuid.uuid4()),
            format_version=SESSION_FORMAT_VERSION,
            app_version=__version__,
        )

        self._write_entry("session_start", asdict(metadata))

        print(f"[SESSION] Started logging: {self._session_path}")
        print(f"[SESSION] Mode: {mode}" + (f" (trigger: {trigger_type})" if trigger_type else ""))
        print(f"[SESSION] Raw radar log: {self._raw_path}")

        return self._session_id

    def log_connection(
        self,
        device: str,
        port: str,
        baud: int = 0,
        firmware: str = None,
        radc_available: bool = None,
        **kwargs,
    ):
        """Log device connection details."""
        if not self.enabled:
            return
        entry = {
            "device": device,
            "port": port,
            "baud": baud,
        }
        if firmware:
            entry["firmware"] = firmware
        if radc_available is not None:
            entry["radc_available"] = radc_available
        entry.update(kwargs)
        self._write_entry("connection", entry)

    def log_clock_sync(self, device: str, port: str, summary: Dict[str, Any]):
        """Log an OPS clock-sync block (radar-clock -> host-epoch mapping).

        The ``summary`` comes from OPS243Radar.read_clock_sync and carries the
        per-read offsets plus the best offset/latency, so the radar's internal
        trigger_time can be converted to a host epoch in live capture and
        offline analysis.
        """
        if not self.enabled:
            return
        entry = {"device": device, "port": port}
        if summary:
            entry.update(summary)
        self._write_entry("ops_clock_sync", entry)

    def _setup_raw_logging(self):
        """Configure Python logging for raw radar data."""
        # Remove existing handlers
        for handler in self._raw_logger.handlers[:]:
            self._raw_logger.removeHandler(handler)
        for handler in self._radar_logger.handlers[:]:
            self._radar_logger.removeHandler(handler)

        # Add file handler for raw data
        file_handler = logging.FileHandler(self._raw_path)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s.%(msecs)03d - %(message)s", datefmt="%H:%M:%S")
        )

        self._raw_logger.addHandler(file_handler)
        self._raw_logger.setLevel(logging.DEBUG)

        self._radar_logger.addHandler(file_handler)
        self._radar_logger.setLevel(logging.DEBUG)

    def end_session(self):
        """End the current logging session and write summary."""
        if not self.enabled or not self._session_file:
            return

        # Calculate session duration
        end_time = datetime.now()

        # Write session end with summary
        summary = {
            "end_time": end_time.isoformat(),
            "stats": self._stats.copy(),
        }

        self._write_entry("session_end", summary)

        # Close the session file under the write lock so it cannot be
        # closed out from under a concurrent _write_entry call.
        with self._write_lock:
            if self._session_file:
                self._session_file.close()
                self._session_file = None

        # Remove logging handlers
        for handler in self._raw_logger.handlers[:]:
            handler.close()
            self._raw_logger.removeHandler(handler)
        for handler in self._radar_logger.handlers[:]:
            handler.close()
            self._radar_logger.removeHandler(handler)

        print(f"[SESSION] Ended. Total shots: {self._stats['shots_detected']}")
        print(f"[SESSION] Logs saved to: {self._session_path}")

    def _write_entry(self, entry_type: str, data: Dict[str, Any]):
        """Write a log entry to the session file.

        Serialized with ``_write_lock`` so concurrent log_* calls from
        different threads cannot interleave a partial line into the JSONL
        stream or write to a file that end_session() is closing. The entry
        is serialized outside the lock to keep the critical section to the
        write+flush.
        """
        if not self._session_file:
            return

        line = json.dumps({"ts": datetime.now().isoformat(), "type": entry_type, **data}) + "\n"

        with self._write_lock:
            if not self._session_file:
                return
            self._session_file.write(line)
            self._session_file.flush()

    def log_shot(
        self,
        shot: Shot,
        pipeline_ms: Optional[Dict] = None,
        log_only_fields: Optional[Dict[str, Any]] = None,
    ):
        """Log a shot using the canonical raw shot schema; a demo shot is never logged.

        ``log_only_fields`` are added to the entry as they are: values the session log
        keeps but the shot itself does not carry (the OPS spin session median).
        """
        if not self.enabled or shot.demo:
            return

        self._stats["shots_detected"] += 1
        data = shot.to_dict()
        if data["shot_number"] is None:
            data["shot_number"] = self._stats["shots_detected"]
        if pipeline_ms is not None:
            data["pipeline_ms"] = pipeline_ms
        if log_only_fields:
            data.update(log_only_fields)

        self._write_entry("shot_detected", data)

    def log_shot_update(self, shot: Shot) -> None:
        """Log a published shot again after it was flown again: entry type ``shot_updated``.

        The whole shot in the ``shot_detected`` schema, written when a calculated
        spin axis reached it after its ``shot_detected`` entry; join the two by
        ``shot_number`` and ``impact_timestamp``. Not a new shot: ``shots_detected``
        does not count it, and the saved-session history lays its flight over the
        shot's own entry. A demo shot is never logged.
        """
        if not self.enabled or shot.demo:
            return
        self._write_entry("shot_updated", shot.to_dict())

    def log_carry_variants(
        self,
        *,
        shot_number: Optional[int],
        shot_timestamp: Optional[float],
        variants: Optional[Dict[str, Any]],
        reason: Optional[str] = None,
        air_density_kg_m3: Optional[float] = None,
        elapsed_ms: Optional[float] = None,
    ) -> None:
        """Log the shadow carry variants of one ballistic carry: entry type ``carry_variants``.

        ``variants`` is ``ballistics.carry_variants`` (``{name: {spin_rpm, carry_m,
        apex_m}}``), or None with ``reason`` when its flights failed. Flown after the
        shot was published, so it follows the shot's ``shot_detected`` entry (and a
        ``shot_updated`` one when the shot was flown again); join by ``shot_number`` and
        ``shot_timestamp``, the last entry of a shot counts. Never reaches the shot.
        """
        if not self.enabled:
            return
        self._write_entry(
            "carry_variants",
            {
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "carry_variants": variants,
                "reason": reason,
                "air_density_kg_m3": air_density_kg_m3,
                "elapsed_ms": elapsed_ms,
            },
        )

    def log_shot_deleted(self, shot_timestamp: str) -> None:
        """Mark a logged shot as deleted; its shot_detected line stays in the file."""
        if not self.enabled:
            return
        self._write_entry("shot_deleted", {"shot_timestamp": shot_timestamp})

    def log_camera_capture(
        self,
        *,
        shot_number: int,
        shot_timestamp: Optional[float],
        trigger_timestamp: Optional[float],
        capture_path: Optional[str],
        metadata: Optional[Dict] = None,
        capture_error: Optional[str] = None,
        edge_fields: Optional[Dict[str, Any]] = None,
    ):
        """Log a high-speed camera clip saved for offline shot correlation.

        ``edge_fields`` (``EdgeTimingLog.fields_for`` of the trigger edge) add
        the kernel and callback stamps of the sound edge that froze the clip.
        """
        if not self.enabled:
            return

        self._write_entry(
            "camera_capture",
            {
                **(edge_fields or {}),
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "trigger_timestamp": trigger_timestamp,
                "trigger_delta_ms": (
                    (trigger_timestamp - shot_timestamp) * 1000.0
                    if shot_timestamp is not None and trigger_timestamp is not None
                    else None
                ),
                "capture_path": capture_path,
                "capture_error": capture_error,
                "metadata": metadata or {},
            },
        )

    def log_camera_club_head_path(
        self,
        *,
        shot_number: int,
        shot_timestamp: Optional[float],
        result: Dict[str, Any],
    ):
        """Log the camera club-data run of one shot (experimental): its full diagnostics.

        Computed after the shot was handed to publication, so it usually follows
        the shot's ``shot_detected`` entry; join the two by ``shot_number`` and
        ``shot_timestamp``. The gated values that reach the shot are logged
        separately (``log_club_data``); a shot whose spin axis they changed
        after its ``shot_detected`` entry gets a ``shot_updated`` entry
        (``log_shot_update``).
        """
        if not self.enabled:
            return

        self._write_entry(
            "camera_club_head_path",
            {
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "experimental": True,
                **result,
            },
        )

    def log_camera_lowlight_ball_flight(
        self,
        *,
        shot_number: int,
        shot_timestamp: Optional[float],
        result: Dict[str, Any],
    ):
        """Log the low-light shadow start direction of one shot (``camera.lowlight``).

        Shadow only: flicker compensation plus a relaxed ball gate, computed after
        the shot was handed to publication, so it usually follows the shot's
        ``shot_detected`` entry; join the two by ``shot_number`` and
        ``shot_timestamp``. Its ``lowlight_camera_horizontal_*`` fields never
        reach the shot (``launch_angle_horizontal``, curve, spin axis).
        """
        if not self.enabled:
            return

        self._write_entry(
            "camera_lowlight_ball_flight",
            {
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "experimental": True,
                "shadow": True,
                **result,
            },
        )

    def log_club_data(
        self,
        *,
        shot_number: int,
        shot_timestamp: Optional[float],
        status: str,
        reason: Optional[str],
        fields: Dict[str, Any],
        mode: str = "show",
    ):
        """Log the calculated club data of one shot: entry type ``club_data``.

        ``status`` is ``done`` or ``skipped`` (with ``reason``); ``fields`` are the
        calculated shot fields (``openflight.club_data.CLUB_DATA_FIELDS``), None
        where a gate failed. Written once per eligible clip. ``mode`` is the
        server's ``--club-data`` mode; the saved-session history merges only
        ``show`` entries into its shots.
        """
        if not self.enabled:
            return

        self._write_entry(
            "club_data",
            {
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "status": status,
                "reason": reason,
                "mode": mode,
                "fields": dict(fields),
            },
        )

    def log_kld7_buffer(
        self,
        shot_number: int,
        shot_timestamp: float,
        orientation: str,
        buffer_frames: list,
        ball_angle: Optional[Dict] = None,
        club_angle: Optional[Dict] = None,
    ):
        """Log K-LD7 frame timing alongside OPS243 shot data."""
        if not self.enabled:
            return

        radc_frame_count = sum(1 for frame in buffer_frames if frame.get("has_radc"))
        self._write_entry(
            "kld7_buffer",
            {
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "orientation": orientation,
                "frame_count": len(buffer_frames),
                "radc_frame_count": radc_frame_count,
                "frames": buffer_frames,
                "ball_angle": ball_angle,
                "club_angle": club_angle,
            },
        )

    def log_iwr6843_capture(
        self,
        *,
        shot_number: int,
        shot_timestamp: Optional[float],
        trigger_timestamp: Optional[float],
        capture_path: Optional[str],
        capture_bytes: int,
        dump_duration_s: Optional[float],
        capture_error: Optional[str],
        ball_speed_mph: float,
        measurement: Optional[Dict] = None,
        launch_guard: Optional[Dict[str, Any]] = None,
        club_path: Optional[Dict] = None,
        temperature_report: Optional[Dict[str, Any]] = None,
        match_context: Optional[Dict[str, Any]] = None,
        edge_fields: Optional[Dict[str, Any]] = None,
        ball_height_m: Optional[float] = None,
        tee_height_m: Optional[float] = None,
    ):
        """Log the TI raw-dump reference and complete LCMF evidence.

        ``match_context`` adds the out-of-window match fields
        (nearest_trigger_delta_ms, ti_busy_at_impact, ti_ready_after_impact_ms)
        and, under ``--shot-trigger ops-speed``, the edge-to-``l3dump`` timing
        (l3dump_target_ms, l3dump_actual_ms);
        keys whose value is None are left out of the record. ``edge_fields``
        (``EdgeTimingLog.fields_for`` of the trigger edge) add the kernel and
        callback stamps of the sound edge that started the dump.
        ``launch_guard`` is the club/speed plausibility verdict on an accepted
        LCMF vertical angle (rule, expected/allowed/delta, rejection reason);
        it is only written when the guard ran. ``ball_height_m`` (ball centre)
        and ``tee_height_m`` (its tee, 0 off the mat) are the shot's effective
        geometry for the TI and camera; written when given.
        """
        if not self.enabled:
            return
        self._write_entry(
            "iwr6843_capture",
            {
                **(edge_fields or {}),
                "shot_number": shot_number,
                "shot_timestamp": shot_timestamp,
                "trigger_timestamp": trigger_timestamp,
                "trigger_delta_ms": (
                    round((trigger_timestamp - shot_timestamp) * 1000.0, 3)
                    if trigger_timestamp is not None and shot_timestamp is not None
                    else None
                ),
                "capture_path": capture_path,
                "capture_bytes": capture_bytes,
                "dump_duration_s": dump_duration_s,
                "capture_error": capture_error,
                "ball_speed_source": "ops243",
                "ball_speed_mph": ball_speed_mph,
                **({"ball_height_m": ball_height_m} if ball_height_m is not None else {}),
                **({"tee_height_m": tee_height_m} if tee_height_m is not None else {}),
                "measurement": measurement,
                **({"launch_guard": launch_guard} if launch_guard is not None else {}),
                "club_path": club_path,
                "temperature_report": temperature_report,
                **{key: value for key, value in (match_context or {}).items() if value is not None},
            },
        )

    def log_config_change(self, config: Dict[str, Any], source: str = "user"):
        """Log a radar configuration change."""
        if not self.enabled:
            return

        self._write_entry(
            "config_change",
            {
                "config": config,
                "source": source,
            },
        )

    def log_sim_send(
        self,
        target: str,
        shot_number: int,
        provenance: Dict[str, str],
        values: Optional[Dict[str, Any]] = None,
    ):
        """Log a shot forwarded to a simulator connector."""
        if not self.enabled:
            return

        self._write_entry(
            "sim_send",
            {
                "target": target,
                "shot_number": shot_number,
                "provenance": provenance,
                "values": values or {},
            },
        )

    def log_sim_status(
        self,
        target: str,
        state: str,
        host: str = "",
        port: int = 0,
        message: str = "",
        attempt: int = 0,
        next_retry_in_s: float = 0.0,
    ):
        """Log a simulator connector connection-state change."""
        if not self.enabled:
            return

        self._write_entry(
            "sim_status",
            {
                "target": target,
                "state": state,
                "host": host,
                "port": port,
                "message": message,
                "attempt": attempt,
                "next_retry_in_s": next_retry_in_s,
            },
        )

    def log_sim_player(self, target: str, handed: str, club: str):
        """Log a player/club update pushed by a simulator."""
        if not self.enabled:
            return

        self._write_entry(
            "sim_player",
            {
                "target": target,
                "handed": handed,
                "club": club,
            },
        )

    def log_client_clock(self, reading: Dict[str, Any]) -> None:
        """Log a kiosk/phone clock report compared with the Pi clock (see client_clock.py)."""
        if not self.enabled:
            return
        self._write_entry("client_clock", reading)

    def log_angle_reference(
        self, event: str, state: Dict[str, Any], reason: Optional[str] = None
    ) -> None:
        """Log a change of the camera angle reference (``openflight.angle_reference``)."""
        if not self.enabled:
            return
        self._write_entry("angle_reference", {"event": event, "reason": reason, **state})

    def log_weather(self, reading: Dict[str, Any]) -> None:
        """Log an accepted weather update and its air density (see weather.py)."""
        if not self.enabled:
            return
        self._write_entry("weather", reading)

    def log_weather_fetch_failed(self, failure: Dict[str, Any]) -> None:
        """Log why the phone's weather fetch or GPS range location failed (see weather.py)."""
        if not self.enabled:
            return
        self._write_entry("weather_fetch_failed", failure)

    def log_phone_audio_onset(self, entry: Dict[str, Any]) -> None:
        """Log an impact onset from the iOS companion and its sound-edge match (log only)."""
        if not self.enabled:
            return
        self._write_entry("phone_audio_onset", entry)

    def log_ti_self_trigger(self, entry: Dict[str, Any]) -> None:
        """Log an IWR6843 self-trigger fire (``ti``) or shadow pairing (``ti-shadow``)."""
        if not self.enabled:
            return
        self._write_entry("ti_self_trigger", entry)

    def log_ops_speed_trigger(self, entry: Dict[str, Any]) -> None:
        """Log an OPS 1.3.3 speed-trigger event (``--shot-trigger ops-speed*``).

        ``kind``: a witness outcome (``matched``, ``pin_without_mic``,
        ``mic_without_pin``, ``glitch``), ``stall_rearm``, ``pin_silent``,
        ``refused`` or the session's ``summary``.
        """
        if not self.enabled:
            return
        self._write_entry("ops_speed_trigger", entry)

    def log_phone_sensors(self, entry: Dict[str, Any]) -> None:
        """Log a sensor snapshot from the iOS companion (log only)."""
        if not self.enabled:
            return
        self._write_entry("phone_sensors", entry)

    def log_weather_location(self, location: Dict[str, Any]) -> None:
        """Log a newly stored range location."""
        if not self.enabled:
            return
        self._write_entry("weather_location", location)

    def log_power_status(self, status: Dict[str, Any]) -> None:
        """Log a battery snapshot or power-state transition."""
        if not self.enabled:
            return
        self._write_entry("power_status", status)

    def log_trigger_event(
        self,
        trigger_type: str,
        accepted: bool,
        reason: str = "",
        response_bytes: int = 0,
        total_readings: int = 0,
        outbound_readings: int = 0,
        inbound_readings: int = 0,
        peak_outbound_mph: float = 0.0,
        peak_inbound_mph: float = 0.0,
        all_outbound_speeds: Optional[List[float]] = None,
        all_inbound_speeds: Optional[List[float]] = None,
        ball_speed_mph: Optional[float] = None,
        club_speed_mph: Optional[float] = None,
        spin_rpm: Optional[float] = None,
        carry_yards: Optional[float] = None,
        latency_ms: Optional[float] = None,
        gate_fields: Optional[Dict[str, Any]] = None,
        counts_as_trigger: bool = True,
    ):
        """Log the single enriched event for one physical trigger.

        ``gate_fields`` (Pi trigger gate only) are merged into the record.
        ``counts_as_trigger=False`` (a sound edge lost to OPS blind time, reason
        ``ops_busy``) keeps it out of the trigger totals; it is counted in
        ``triggers_ops_busy`` instead.
        """
        if not self.enabled:
            return

        if not counts_as_trigger:
            self._stats["triggers_ops_busy"] = self._stats.get("triggers_ops_busy", 0) + 1
        else:
            if "triggers_total" not in self._stats:
                self._stats["triggers_total"] = 0
                self._stats["triggers_accepted"] = 0
                self._stats["triggers_rejected"] = 0

            self._stats["triggers_total"] += 1
            if accepted:
                self._stats["triggers_accepted"] += 1
            else:
                self._stats["triggers_rejected"] += 1

        self._write_entry(
            "trigger_event",
            {
                "trigger_type": trigger_type,
                "accepted": accepted,
                "reason": reason,
                "response_bytes": response_bytes,
                "total_readings": total_readings,
                "outbound_readings": outbound_readings,
                "inbound_readings": inbound_readings,
                "peak_outbound_mph": peak_outbound_mph,
                "peak_inbound_mph": peak_inbound_mph,
                "all_outbound_speeds": all_outbound_speeds or [],
                "all_inbound_speeds": all_inbound_speeds or [],
                "ball_speed_mph": ball_speed_mph,
                "club_speed_mph": club_speed_mph,
                "spin_rpm": spin_rpm,
                "carry_yards": carry_yards,
                "latency_ms": latency_ms,
                **(gate_fields or {}),
            },
        )

    def log_rolling_buffer_capture(
        self,
        shot_number: Optional[int],
        sample_time: float,
        trigger_time: float,
        i_samples: List[int],
        q_samples: List[int],
        ball_speed_mph: Optional[float] = None,
        club_speed_mph: Optional[float] = None,
        ball_timestamp_ms: Optional[float] = None,
        club_timestamp_ms: Optional[float] = None,
        impact_timestamp_ms: Optional[float] = None,
        impact_source: Optional[str] = None,
        impact_reason: Optional[str] = None,
        impact_speed_delta_mph: Optional[float] = None,
        impact_transition_gap_ms: Optional[float] = None,
        impact_last_club_speed_mph: Optional[float] = None,
        impact_last_club_timestamp_ms: Optional[float] = None,
        impact_last_club_center_ms: Optional[float] = None,
        impact_first_ball_speed_mph: Optional[float] = None,
        impact_first_ball_timestamp_ms: Optional[float] = None,
        impact_first_ball_center_ms: Optional[float] = None,
        impact_min_transition_delta_mph: Optional[float] = None,
        trigger_latency_ms: Optional[float] = None,
        smash_factor: Optional[float] = None,
        spin_rpm: Optional[float] = None,
        spin_confidence: Optional[float] = None,
        spin_method: Optional[str] = None,
        spin_quality: Optional[str] = None,
        spin_multipath_fade_hz: Optional[float] = None,
        spin_snr: Optional[float] = None,
        spin_modulation_depth: Optional[float] = None,
        spin_peak_freq_hz: Optional[float] = None,
        spin_seam_cycles: Optional[float] = None,
        spin_at_lower_rail: Optional[bool] = None,
        spin_at_upper_rail: Optional[bool] = None,
        spin_candidates: Optional[List[Dict]] = None,
        spin_phase_method: Optional[str] = None,
        spin_phase_rpm: Optional[float] = None,
        spin_phase_snr: Optional[float] = None,
        spin_phase_agreement_pct: Optional[float] = None,
        spin_phase_confirmed: Optional[bool] = None,
        spin_rejection_reason: Optional[str] = None,
        first_byte_timestamp: Optional[float] = None,
        trigger_timestamp: Optional[float] = None,
        trigger_timestamp_source: Optional[str] = None,
        clock_sync_offset_s: Optional[float] = None,
        post_trigger_duration_ms: Optional[float] = None,
        club_line_end_ms: Optional[float] = None,
        club_speed_mode: Optional[str] = None,
        gate_fields: Optional[Dict[str, Any]] = None,
        rejection_reason: Optional[str] = None,
        sample_rate_hz: Optional[int] = None,
        ball_speed_status: Optional[str] = None,
        ball_speed_status_reason: Optional[str] = None,
        ball_speed_replaced_from_mph: Optional[float] = None,
        ball_guard: Optional[Dict[str, Any]] = None,
    ):
        """
        Log raw rolling buffer capture data for offline analysis.

        Args:
            shot_number: Shot number this capture belongs to; None for a
                capture the processor rejected (``rejection_reason``)
            sample_time: When sampling started (radar timestamp)
            trigger_time: When trigger fired (radar timestamp)
            i_samples: Raw I channel samples (4096 values)
            q_samples: Raw Q channel samples (4096 values)
            ball_speed_mph: Detected ball speed (if any)
            club_speed_mph: Detected club speed (if any)
            ball_timestamp_ms: Ball signal position in buffer (ms from start)
            club_timestamp_ms: Club signal position in buffer (ms from start)
            impact_timestamp_ms: Selected impact position in buffer (ms from start)
            impact_source: Source used for impact timing
            impact_reason: Fallback reason when source is not OPS transition
            impact_speed_delta_mph: Speed jump across club-to-ball transition
            impact_transition_gap_ms: Gap between transition frame centers
            impact_last_club_speed_mph: Last club-like frame speed
            impact_last_club_timestamp_ms: Last club-like frame start time
            impact_last_club_center_ms: Last club-like frame center time
            impact_first_ball_speed_mph: First ball-like frame speed
            impact_first_ball_timestamp_ms: First ball-like frame start time
            impact_first_ball_center_ms: First ball-like frame center time
            impact_min_transition_delta_mph: Minimum speed jump for transition
            trigger_latency_ms: Edge-to-S! latency (ms)
            smash_factor: Ball speed / club speed ratio
            spin_rpm: Detected spin rate in RPM
            spin_confidence: Confidence of spin detection (0-1)
            spin_method: Estimator that produced the spin candidate
            spin_quality: Quality assessment ("high", "medium", "low", "experimental")
            spin_multipath_fade_hz: Fitted two-ray fade frequency
            spin_snr: Signal-to-noise ratio of spin detection
            spin_modulation_depth: Envelope std/mean ratio (1-5% real seam,
                <0.5% noise floor, 0.5-1% suspicious)
            spin_peak_freq_hz: Frequency of the picked envelope-FFT peak
            spin_seam_cycles: Seam cycles in analysis window
            spin_at_lower_rail: True when peak landed at the bottom of
                the seam search range (envelope-DC leakage suspect)
            spin_at_upper_rail: True when peak landed at the top of the
                seam search range (bandpass-shoulder noise suspect)
            spin_candidates: Ranked envelope-FFT spin peaks for offline analysis
            spin_phase_method: Phase confirmation method, if attempted
            spin_phase_rpm: Phase-derived spin candidate, if available
            spin_phase_snr: Phase-derived candidate SNR
            spin_phase_agreement_pct: Envelope/phase agreement percentage
            spin_phase_confirmed: True when phase recovered a low-SNR spin
            spin_rejection_reason: Human-readable reason if spin was
                rejected (None on a clean accept)
            first_byte_timestamp: Host epoch timestamp when the first byte
                of the hardware-triggered rolling-buffer dump arrived
            trigger_timestamp: Host epoch timestamp of the inferred hardware trigger
            trigger_timestamp_source: Method used to infer trigger_timestamp
            clock_sync_offset_s: Host epoch minus OPS radar clock, when available
            post_trigger_duration_ms: Duration of the capture after trigger
            gate_fields: Pi trigger gate record (``gate_mode``, ``gate_decision``,
                ``sound_edge_delay_ms``, ``sound_edge_offset_ms``, ...), merged
                into the entry; club speed and impact anchor to the sound edge
            rejection_reason: Why the processor made no shot of this capture
                (e.g. ``no_ball_track``), None for a shot's capture
            sample_rate_hz: OPS sample rate of the capture; replays must use
                it (``logged_sample_rate_hz`` reads 30 ksps for older logs)
            ball_speed_status: "uncertain" when the ball-track guard flagged
                the ball speed, else None
            ball_speed_status_reason: Guard rule that fired (flagged or replaced)
            ball_speed_replaced_from_mph: Voted ball speed the guard replaced
            ball_guard: The guard's whole decision (``BallSpeedGuard.to_dict``),
                also when no rule fired; None without the anchored club
        """
        if not self.enabled:
            return

        trigger_offset_ms = (trigger_time - sample_time) * 1000
        impact_offset_from_trigger_ms = (
            impact_timestamp_ms - trigger_offset_ms if impact_timestamp_ms is not None else None
        )

        self._write_entry(
            "rolling_buffer_capture",
            {
                "shot_number": shot_number,
                "sample_time": sample_time,
                "trigger_time": trigger_time,
                "trigger_offset_ms": trigger_offset_ms,
                "sample_count": len(i_samples),
                "sample_rate_hz": sample_rate_hz,
                "i_samples": i_samples,
                "q_samples": q_samples,
                "ball_speed_mph": ball_speed_mph,
                "club_speed_mph": club_speed_mph,
                "ball_timestamp_ms": ball_timestamp_ms,
                "club_timestamp_ms": club_timestamp_ms,
                "impact_timestamp_ms": impact_timestamp_ms,
                "impact_offset_from_trigger_ms": impact_offset_from_trigger_ms,
                "impact_source": impact_source,
                "impact_reason": impact_reason,
                "impact_speed_delta_mph": impact_speed_delta_mph,
                "impact_transition_gap_ms": impact_transition_gap_ms,
                "impact_last_club_speed_mph": impact_last_club_speed_mph,
                "impact_last_club_timestamp_ms": impact_last_club_timestamp_ms,
                "impact_last_club_center_ms": impact_last_club_center_ms,
                "impact_first_ball_speed_mph": impact_first_ball_speed_mph,
                "impact_first_ball_timestamp_ms": impact_first_ball_timestamp_ms,
                "impact_first_ball_center_ms": impact_first_ball_center_ms,
                "impact_min_transition_delta_mph": impact_min_transition_delta_mph,
                "trigger_latency_ms": trigger_latency_ms,
                "first_byte_timestamp": first_byte_timestamp,
                "trigger_timestamp": trigger_timestamp,
                "trigger_timestamp_source": trigger_timestamp_source,
                "trigger_timestamp_from_first_byte": (
                    first_byte_timestamp - (post_trigger_duration_ms / 1000.0)
                    if first_byte_timestamp is not None and post_trigger_duration_ms is not None
                    else None
                ),
                "trigger_timestamp_delta_from_first_byte_ms": (
                    (trigger_timestamp - (first_byte_timestamp - post_trigger_duration_ms / 1000.0))
                    * 1000.0
                    if trigger_timestamp is not None
                    and first_byte_timestamp is not None
                    and post_trigger_duration_ms is not None
                    else None
                ),
                "clock_sync_offset_s": clock_sync_offset_s,
                "post_trigger_duration_ms": post_trigger_duration_ms,
                "club_line_end_ms": club_line_end_ms,
                "club_speed_mode": club_speed_mode,
                "smash_factor": smash_factor,
                "spin_rpm": spin_rpm,
                "spin_confidence": spin_confidence,
                "spin_method": spin_method,
                "spin_quality": spin_quality,
                "spin_multipath_fade_hz": spin_multipath_fade_hz,
                "spin_snr": spin_snr,
                "spin_modulation_depth": spin_modulation_depth,
                "spin_peak_freq_hz": spin_peak_freq_hz,
                "spin_candidate_rpm": (
                    round(spin_peak_freq_hz * 60) if spin_peak_freq_hz is not None else None
                ),
                "spin_seam_cycles": spin_seam_cycles,
                "spin_at_lower_rail": spin_at_lower_rail,
                "spin_at_upper_rail": spin_at_upper_rail,
                "spin_candidates": spin_candidates,
                "spin_phase_method": spin_phase_method,
                "spin_phase_rpm": spin_phase_rpm,
                "spin_phase_snr": spin_phase_snr,
                "spin_phase_agreement_pct": spin_phase_agreement_pct,
                "spin_phase_confirmed": spin_phase_confirmed,
                "spin_rejection_reason": spin_rejection_reason,
                "rejection_reason": rejection_reason,
                "ball_speed_status": ball_speed_status,
                "ball_speed_status_reason": ball_speed_status_reason,
                "ball_speed_replaced_from_mph": ball_speed_replaced_from_mph,
                "ball_guard": ball_guard,
                **(gate_fields or {}),
            },
        )

    def log_error(self, error: str, context: Optional[Dict] = None):
        """Log an error."""
        if not self.enabled:
            return

        self._stats["errors"] += 1

        self._write_entry(
            "error",
            {
                "error": error,
                "context": context or {},
            },
        )

    @property
    def session_path(self) -> Optional[Path]:
        """Get the current session log file path."""
        return self._session_path

    @property
    def current_path(self) -> Optional[Path]:
        """The session log file while it is open, else None."""
        with self._write_lock:
            return self._session_path if self._session_file else None

    @property
    def raw_path(self) -> Optional[Path]:
        """Get the current raw radar log file path."""
        return self._raw_path

    @property
    def session_id(self) -> Optional[str]:
        """Get the current session ID."""
        return self._session_id

    @property
    def stats(self) -> Dict[str, int]:
        """Get current session statistics."""
        return self._stats.copy()


# Global session logger instance
_session_logger: Optional[SessionLogger] = None


def get_session_logger() -> Optional[SessionLogger]:
    """Get the global session logger instance."""
    return _session_logger


def log_session_error(
    error: str,
    *,
    context: Optional[Dict[str, Any]] = None,
    component: Optional[str] = None,
    exc: Optional[BaseException] = None,
) -> None:
    """Write an error entry to the active session JSONL log, if any."""
    session = get_session_logger()
    if session is None:
        return

    ctx: Dict[str, Any] = dict(context or {})
    if component:
        ctx["component"] = component
    if exc is not None:
        ctx["exception_type"] = type(exc).__name__
        ctx["exception_message"] = str(exc)

    session.log_error(error, context=ctx)


def init_session_logger(
    log_dir: Optional[Path] = None, location: str = "range", enabled: bool = True
) -> SessionLogger:
    """
    Initialize and return the global session logger.

    Args:
        log_dir: Directory for log files
        location: Location identifier
        enabled: Whether logging is enabled

    Returns:
        SessionLogger instance
    """
    global _session_logger
    _session_logger = SessionLogger(log_dir=log_dir, location=location, enabled=enabled)
    return _session_logger
