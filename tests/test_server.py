"""Tests for server module."""

import argparse
import json
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight import server as server_module
from openflight.ballistics import TRACKMAN_FLAT_AIR_DENSITY
from openflight.camera.replay import ReplayNotFoundError, ReplayPreparationError
from openflight.clubs import ClubType
from openflight.clubs.bag import club_category
from openflight.dplane import CURVE_CLASSES, curve_class, dplane_spin_axis
from openflight.edge_timing import EdgeTimingLog
from openflight.iwr6843 import Calibration
from openflight.kld7.types import KLD7Angle
from openflight.launch_monitor import SPIN_CONFIDENCE_HIGH, Shot
from openflight.ops243 import UART_BAUD_COMMANDS
from openflight.power import PowerState
from openflight.server import (
    MockLaunchMonitor,
    MockSwingSpeedMonitor,
    estimate_launch_angle,
    on_shot_detected,
    radar_launch_is_plausible,
    shot_to_dict,
    swing_speed_to_dict,
    swing_speed_to_shot_dict,
)
from openflight.swing_speed import SwingSpeedEvent


def _wait_for_shot_finalization_idle(timeout_s: float = 2.0) -> None:
    """Wait until the asynchronous ordered finalizer has published every shot."""
    with server_module._shot_finalization_condition:
        idle = server_module._shot_finalization_condition.wait_for(
            lambda: (
                not server_module._shot_finalization_order
                and not server_module._shot_finalization_running
            ),
            timeout=timeout_s,
        )
    assert idle, "shot finalization coordinator did not become idle"


class TestCameraCaptureSettings:
    """Tests for live-safe Camera tab controls."""

    def test_shot_waits_four_seconds_for_its_camera_clip(self, monkeypatch):
        timeouts = []

        def capture_for_shot(impact_timestamp, timeout_s):
            timeouts.append((impact_timestamp, timeout_s))

        runtime = SimpleNamespace(
            capture_for_shot=capture_for_shot,
            note_accepted_shot=lambda *_args, **_kwargs: None,
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(server_module, "_snapshot_inclinometer_for_shot", lambda _shot: None)
        monkeypatch.setattr(server_module, "_process_iwr6843_angle", lambda _shot: None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "_attach_camera_replay", lambda *_args: None)
        monkeypatch.setattr(server_module, "_fuse_camera_measurements", lambda *_args, **_kw: None)
        shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
        shot.impact_timestamp = 100.0

        server_module._enrich_shot_from_optional_hardware(shot)

        assert timeouts == [(100.0, 4.0)]

    def test_camera_capture_entry_gets_the_edge_fields_of_its_trigger(self, monkeypatch):
        edge_timing = EdgeTimingLog()
        edge_timing.record(
            100.01,
            callback_timestamp=100.01,
            callback_monotonic_ns=5_000_000_000,
            ticks=None,
        )
        monkeypatch.setattr(server_module, "sound_edge_timing", edge_timing)
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=None,
            metadata={},
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            capture_for_shot=lambda _impact, timeout_s: capture,
            note_accepted_shot=lambda *_args, **_kwargs: None,
        )
        logged = []
        session = SimpleNamespace(
            stats={"shots_detected": 0},
            log_camera_capture=lambda **kwargs: logged.append(kwargs),
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(server_module, "_snapshot_inclinometer_for_shot", lambda _shot: None)
        monkeypatch.setattr(server_module, "_process_iwr6843_angle", lambda _shot: None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
        monkeypatch.setattr(server_module, "_attach_camera_replay", lambda *_args: None)
        monkeypatch.setattr(server_module, "_fuse_camera_measurements", lambda *_args, **_kw: None)
        monkeypatch.setattr(server_module, "_apply_dplane_spin_axis", lambda *_args: None)
        shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
        shot.impact_timestamp = 100.0

        server_module._enrich_shot_from_optional_hardware(shot)

        ((entry,),) = [(kwargs,) for kwargs in logged]
        assert entry["edge_fields"]["edge_callback_timestamp"] == 100.01
        assert entry["edge_fields"]["edge_callback_monotonic_ns"] == 5_000_000_000
        assert entry["edge_fields"]["edge_kernel_timestamp"] is None

    @pytest.mark.parametrize("demo", [False, True])
    def test_only_the_clip_of_an_ops_shot_teaches_the_tee_anchor(self, monkeypatch, demo):
        capture = SimpleNamespace(
            trigger_timestamp=100.01, path=None, metadata={}, error=None, valid=True, sequence=1
        )
        noted = []

        def note_accepted_shot(clip, *, shot_number=None):
            noted.append((clip, shot_number))
            clip.metadata["tee_anchor"] = {"kind": "established"}

        runtime = SimpleNamespace(
            capture_for_shot=lambda _impact, timeout_s: capture,
            note_accepted_shot=note_accepted_shot,
        )
        logged = []
        session = SimpleNamespace(
            stats={"shots_detected": 0},
            log_camera_capture=lambda **kwargs: logged.append(kwargs),
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(server_module, "_snapshot_inclinometer_for_shot", lambda _shot: None)
        monkeypatch.setattr(server_module, "_process_iwr6843_angle", lambda _shot: None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
        monkeypatch.setattr(server_module, "_attach_camera_replay", lambda *_args: None)
        monkeypatch.setattr(server_module, "_fuse_camera_measurements", lambda *_args, **_kw: None)
        monkeypatch.setattr(server_module, "_apply_dplane_spin_axis", lambda *_args: None)
        shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now(), demo=demo)
        shot.impact_timestamp = 100.0
        shot.shot_number = 7

        server_module._enrich_shot_from_optional_hardware(shot)

        if demo:
            assert noted == []
        else:
            assert noted == [(capture, 7)]
            assert logged[0]["metadata"]["tee_anchor"] == {"kind": "established"}

    @pytest.mark.parametrize("use_gpio_trigger", [True, False])
    def test_camera_owning_the_sound_input_fills_the_shared_edge_log(
        self, monkeypatch, tmp_path, use_gpio_trigger
    ):
        created = {}

        class FakeRuntime:
            def __init__(self, **kwargs):
                created.update(kwargs)
                self.settings = kwargs["settings"]

            def start(self):
                return None

            def set_ball_mark_source(self, provider):
                created["ball_mark_source"] = provider

        monkeypatch.setattr("openflight.camera.capture_runtime.CameraCaptureRuntime", FakeRuntime)
        for name in (
            "camera_capture_runtime",
            "camera_replay_manager",
            "camera_reference_ball_tracker",
            "camera_ball_flight_reference_tracker",
            "camera_cv_worker",
        ):
            monkeypatch.setattr(server_module, name, None)
        monkeypatch.setattr(server_module, "camera_capture_config", {"enabled": False})

        assert server_module.init_camera_capture(
            output_dir=tmp_path,
            gpio_pin=17,
            width=640,
            height=400,
            fps=600.0,
            pre_ms=100.0,
            post_ms=100.0,
            exposure_us=200,
            gain=4.0,
            stream="raw",
            rotate_180=False,
            mirror_horizontal=False,
            roll_correction_deg=0.0,
            scaler_crop=None,
            mount_height_m=0.3,
            lateral_offset_m=0.5,
            horizontal_offset_deg=0.0,
            use_gpio_trigger=use_gpio_trigger,
        )
        expected = server_module.sound_edge_timing if use_gpio_trigger else None
        assert created["edge_timing"] is expected
        # The trigger check reads the tapped ball mark the server holds.
        assert created["ball_mark_source"] is server_module._camera_ball_mark_px  # noqa: SLF001

    def test_exposure_quality_endpoint_uses_camera_runtime(self, monkeypatch):
        expected = {
            "sample_available": True,
            "status": "good",
            "recommendation": "hold",
        }
        auto_exposure = {
            "enabled": True,
            "status": "ready",
            "analysis_eligible": True,
        }
        runtime = SimpleNamespace(
            exposure_quality=lambda: dict(expected),
            auto_exposure_status=lambda: auto_exposure,
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)

        response = server_module.app.test_client().get("/api/camera/exposure-quality")

        assert response.status_code == 200
        assert response.get_json() == {
            **expected,
            "auto_exposure": auto_exposure,
            "frame_flat": False,
        }

    def test_update_applies_alignment_without_manual_exposure(self, monkeypatch):
        emitted = []

        class FakeRuntime:
            settings = SimpleNamespace(fps=600.0)
            capture_mode_status = staticmethod(lambda: {})

            @staticmethod
            def status():
                return {
                    "running": True,
                    "armed": True,
                    "buffered_frames": 90,
                    "required_pre_frames": 90,
                }

            @staticmethod
            def vertical_crop_status():
                return {
                    "raw_crop_adjustable": True,
                    "vertical_offset_px": -10,
                    "vertical_offset_min_px": -70,
                    "vertical_offset_max_px": 70,
                    "vertical_offset_step_px": 10,
                }

        config = {
            "enabled": True,
            "exposure_us": 500,
            "gain": 2.0,
            "width": 320,
            "height": 200,
        }
        monkeypatch.setattr(server_module, "camera_capture_runtime", FakeRuntime())
        monkeypatch.setattr(server_module, "camera_capture_config", config)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        server_module.handle_set_camera_capture_settings(
            {
                "alignment_x_pct": 47,
                "alignment_y_pct": 58,
            }
        )

        assert config["alignment_x_pct"] == 47.0
        assert config["alignment_y_pct"] == 58.0
        assert emitted[-1][0] == "camera_capture_settings"
        assert emitted[-1][1]["max_exposure_us"] == 1666
        assert emitted[-1][1]["raw_crop_adjustable"] is True
        assert emitted[-1][1]["vertical_offset_px"] == -10

    def test_update_rejects_manual_exposure_override(self, monkeypatch):
        emitted = []
        runtime = SimpleNamespace(
            settings=SimpleNamespace(fps=488.0),
            status=lambda: {"running": True, "armed": True},
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(
            server_module,
            "camera_capture_config",
            {"exposure_us": 500, "gain": 12.0},
        )
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        server_module.handle_set_camera_capture_settings({"exposure_us": 650})

        assert emitted == [
            (
                "camera_capture_settings_error",
                {"error": "Camera exposure and gain are managed automatically"},
            )
        ]

    def test_explicit_manual_exposure_mode_is_applied(self, monkeypatch):
        emitted, updates = [], []
        runtime = SimpleNamespace(
            settings=SimpleNamespace(fps=300.0, exposure_us=350, gain=4.0),
            capture_mode_status=lambda: {},
            status=lambda: {"running": True, "armed": True},
            vertical_crop_status=lambda: {"raw_crop_adjustable": False},
            update_exposure_mode=lambda **values: (
                updates.append(values)
                or {
                    "auto_exposure_enabled": False,
                    "exposure_us": 350,
                    "gain": 4.0,
                }
            ),
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(server_module, "camera_capture_config", {})
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *values: emitted.append(values))
        server_module.handle_set_camera_capture_settings(
            {
                "auto_exposure_enabled": False,
                "exposure_us": 350,
                "gain": 4.0,
            }
        )
        assert updates == [{"automatic": False, "exposure_us": 350, "gain": 4.0}]
        assert emitted[-1][0] == "camera_capture_settings"
        assert emitted[-1][1]["exposure_us"] == 350

    def test_update_moves_real_sensor_crop(self, monkeypatch):
        emitted = []
        moved = []

        class FakeRuntime:
            settings = SimpleNamespace(fps=450.0)
            capture_mode_status = staticmethod(lambda: {})

            @staticmethod
            def status():
                return {"running": True, "armed": True}

            @staticmethod
            def update_image_controls(**_kwargs):
                return {"exposure_us": 500, "gain": 15.0}

            @staticmethod
            def update_vertical_crop(offset_px):
                moved.append(offset_px)
                return {"vertical_offset_px": offset_px}

            @staticmethod
            def active_tee_zone():
                return None

            @staticmethod
            def vertical_crop_status():
                return {
                    "raw_crop_adjustable": True,
                    "vertical_offset_px": -20,
                    "vertical_offset_min_px": -70,
                    "vertical_offset_max_px": 70,
                    "vertical_offset_step_px": 10,
                }

        monkeypatch.setattr(server_module, "camera_capture_runtime", FakeRuntime())
        monkeypatch.setattr(
            server_module,
            "camera_capture_config",
            {"exposure_us": 500, "gain": 15.0},
        )
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        server_module.handle_set_camera_capture_settings({"vertical_offset_px": -20})

        assert moved == [-20]
        assert emitted[-1][1]["vertical_offset_px"] == -20

    def test_update_rejects_out_of_range_alignment(self, monkeypatch):
        emitted = []
        runtime = SimpleNamespace(
            settings=SimpleNamespace(fps=300.0),
            status=lambda: {"running": True, "armed": True},
            update_image_controls=lambda **_kwargs: pytest.fail("controls should not update"),
        )
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(
            server_module,
            "camera_capture_config",
            {"exposure_us": 500, "gain": 2.0},
        )
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        server_module.handle_set_camera_capture_settings({"alignment_x_pct": 101})

        assert emitted == [
            (
                "camera_capture_settings_error",
                {"error": "horizontal alignment must be between 0 and 100 percent"},
            )
        ]


class TestCameraReplayAPI:
    """Camera replay is prepared only through the explicit HTTP action."""

    def test_prepare_replay_returns_cached_video_url(self, monkeypatch, tmp_path):
        video = tmp_path / "replay.mp4"
        video.write_bytes(b"mp4")
        calls = []

        class FakeManager:
            @staticmethod
            def prepare(replay_id):
                calls.append(replay_id)
                return SimpleNamespace(
                    video_path=video,
                    payload={
                        "id": replay_id,
                        "frame_count": 99,
                        "trigger_frame": 73,
                        "playback_fps": 60,
                        "duration_seconds": 1.65,
                        "display_mirror_horizontal": True,
                    },
                )

        monkeypatch.setattr(server_module, "camera_replay_manager", FakeManager())

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 200
        assert calls == ["replay-123"]
        assert response.get_json()["video_url"] == ("/api/camera/replays/replay-123/video")
        assert response.get_json()["trigger_frame"] == 73
        assert response.get_json()["display_mirror_horizontal"] is True

    def test_prepare_replay_is_not_available_via_get(self):
        response = server_module.app.test_client().get("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 405

    @pytest.mark.parametrize(
        ("error", "status"),
        [
            (ReplayNotFoundError("missing"), 404),
            (ReplayPreparationError("broken capture"), 503),
        ],
    )
    def test_prepare_replay_reports_safe_errors(self, monkeypatch, error, status):
        manager = SimpleNamespace(prepare=lambda _replay_id: (_ for _ in ()).throw(error))
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == status
        assert response.get_json() == {"error": str(error)}

    def test_prepare_replay_catches_unexpected_errors_and_logs_them(self, monkeypatch):
        logged = []
        manager = SimpleNamespace(
            prepare=lambda _replay_id: (_ for _ in ()).throw(RuntimeError("private path"))
        )
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 500
        assert response.get_json() == {"error": "Camera replay could not be prepared"}
        assert logged[0][0] == "Camera replay preparation failed"
        assert logged[0][1]["context"] == {"replay_id": "replay-123"}

    def test_video_endpoint_supports_range_requests(self, monkeypatch, tmp_path):
        video = tmp_path / "replay.mp4"
        video.write_bytes(b"0123456789")
        manager = SimpleNamespace(video_path=lambda _replay_id: video)
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)

        response = server_module.app.test_client().get(
            "/api/camera/replays/replay-123/video",
            headers={"Range": "bytes=2-5"},
        )

        assert response.status_code == 206
        assert response.data == b"2345"
        assert response.mimetype == "video/mp4"

    def test_video_endpoint_reports_storage_error(self, monkeypatch):
        manager = SimpleNamespace(
            video_path=lambda _replay_id: (_ for _ in ()).throw(
                ReplayPreparationError("Camera replay storage is unavailable")
            )
        )
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)

        response = server_module.app.test_client().get("/api/camera/replays/replay-123/video")

        assert response.status_code == 503
        assert response.get_json() == {"error": "Camera replay storage is unavailable"}

    def test_matching_capture_registers_replay_without_preparing_it(self, monkeypatch, tmp_path):
        calls = []
        descriptor = {
            "id": "replay-123",
            "frame_count": 99,
            "trigger_frame": 73,
            "playback_fps": 60,
            "duration_seconds": 1.65,
            "display_mirror_horizontal": True,
        }

        class FakeManager:
            @staticmethod
            def register(path, metadata):
                calls.append((path, metadata))
                return descriptor

            @staticmethod
            def prepare(_replay_id):
                pytest.fail("matching a shot must not build the MP4")

        monkeypatch.setattr(server_module, "camera_replay_manager", FakeManager())
        shot = Shot(ball_speed_mph=100.0, timestamp=datetime.now())
        capture = SimpleNamespace(
            valid=True,
            path=tmp_path,
            metadata={"frame_count": 99, "pre_trigger_frames": 74},
        )

        server_module._attach_camera_replay(shot, capture)

        assert calls == [(tmp_path, capture.metadata)]
        assert shot.camera_replay == descriptor
        assert shot_to_dict(shot)["camera_replay"] == descriptor

    def test_deleting_shot_revokes_replay_access_without_deleting_capture(self, monkeypatch):
        replay_ids = []
        manager = SimpleNamespace(unregister=lambda replay_id: replay_ids.append(replay_id))
        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            camera_replay={"id": "replay-123"},
        )
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)
        monkeypatch.setattr(server_module, "monitor", SimpleNamespace(_shots=[shot]))

        assert server_module._delete_session_row(shot.timestamp.isoformat()) is True
        assert replay_ids == ["replay-123"]


class TestCameraReplayAutoPrepare:
    """Finalized camera shots are encoded in the background for companion screens."""

    @pytest.fixture(autouse=True)
    def _server_state(self, monkeypatch):
        self.emitted = []
        self.emit_lock = threading.Lock()
        self.ready = threading.Event()

        def emit(event, payload=None, **_kwargs):
            with self.emit_lock:
                self.emitted.append((event, payload))
            if event == "camera_replay_ready":
                self.ready.set()

        monkeypatch.setattr(server_module.socketio, "emit", emit)
        monkeypatch.setattr(server_module, "camera_capture_runtime", object())
        monkeypatch.setattr(server_module, "_CAMERA_REPLAY_IDLE_POLL_S", 0.01)
        monkeypatch.setattr(server_module, "_shot_processing_active", False)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "sim_connectors", [])
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        yield
        server_module.camera_replay_auto_prepare_queue.join()

    def _events(self):
        with self.emit_lock:
            return [event for event, _payload in self.emitted]

    @staticmethod
    def _shot(replay_id="replay-123", shot_number=7):
        return Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            mode="mock",
            shot_number=shot_number,
            camera_replay={"id": replay_id},
        )

    def _manager(self, monkeypatch, prepare):
        manager = SimpleNamespace(prepare=prepare)
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)
        return manager

    def test_ready_event_follows_final_shot_update(self, monkeypatch):
        prepared_after = []

        def prepare(replay_id):
            prepared_after.append(list(self._events()))
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)

        server_module._finalize_shot_detected(self._shot(), emit_event="shot_update")

        assert self.ready.wait(2.0)
        assert prepared_after == [["shot_update"]]
        assert self._events() == ["shot_update", "camera_replay_ready"]
        assert self.emitted[-1][1] == {
            "shot_number": 7,
            "replay_id": "replay-123",
            "video_url": "/api/camera/replays/replay-123/video",
        }

    def test_waits_while_a_new_shot_is_being_captured(self, monkeypatch):
        prepared = threading.Event()

        def prepare(replay_id):
            prepared.set()
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)
        server_module.on_shot_processing("capturing")

        server_module._queue_camera_replay_auto_prepare(self._shot())

        assert not prepared.wait(0.2)
        server_module.on_shot_processing("failed")
        assert self.ready.wait(2.0)
        assert prepared.is_set()

    def test_waits_until_pending_shot_finalization_is_published(self, monkeypatch):
        prepared = threading.Event()

        def prepare(replay_id):
            prepared.set()
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)
        with server_module._shot_finalization_condition:
            server_module._shot_finalization_order.append(99)
        try:
            server_module._queue_camera_replay_auto_prepare(self._shot())
            assert not prepared.wait(0.2)
        finally:
            with server_module._shot_finalization_condition:
                server_module._shot_finalization_order.remove(99)
        assert self.ready.wait(2.0)

    def test_cached_replay_is_announced_without_encoding(self, monkeypatch, tmp_path):
        from openflight.camera.replay import CameraReplayManager

        capture_dir = tmp_path / "camera_capture"
        capture_dir.mkdir()
        np.savez(capture_dir / "frames.npz", frames=np.zeros((4, 2, 2), dtype=np.uint8))
        (capture_dir / "replay.mp4").write_bytes(b"cached mp4")

        def runner(*_args, **_kwargs):
            pytest.fail("a cached replay must not be encoded again")

        manager = CameraReplayManager(tmp_path, runner=runner)
        replay = manager.register(capture_dir, {"frame_count": 4, "pre_trigger_frames": 2})
        monkeypatch.setattr(server_module, "camera_replay_manager", manager)

        server_module._queue_camera_replay_auto_prepare(self._shot(replay_id=replay["id"]))

        assert self.ready.wait(2.0)
        assert self.emitted[-1][1]["replay_id"] == replay["id"]

    @pytest.mark.parametrize(
        "error",
        [
            ReplayPreparationError("Camera replay encoding failed"),
            ReplayNotFoundError("Camera replay was not found"),
            RuntimeError("unexpected"),
        ],
    )
    def test_failed_preparation_logs_without_announcing(self, monkeypatch, error):
        logged = []
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda message, **kwargs: logged.append((message, kwargs)),
        )

        def prepare(_replay_id):
            raise error

        self._manager(monkeypatch, prepare)

        server_module._auto_prepare_camera_replay(7, "replay-123")

        assert "camera_replay_ready" not in self._events()
        if isinstance(error, ReplayNotFoundError):
            assert logged == []
        else:
            assert logged[0][0] == "Camera replay auto-preparation failed"
            assert logged[0][1]["context"] == {"replay_id": "replay-123", "shot_number": 7}

    def test_disabled_camera_capture_does_not_queue_work(self, monkeypatch):
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        self._manager(monkeypatch, lambda _replay_id: pytest.fail("camera is disabled"))

        server_module._queue_camera_replay_auto_prepare(self._shot())

        assert server_module.camera_replay_auto_prepare_queue.empty()

    def test_shot_without_replay_does_not_queue_work(self, monkeypatch):
        self._manager(monkeypatch, lambda _replay_id: pytest.fail("no replay attached"))
        shot = self._shot()
        shot.camera_replay = None

        server_module._queue_camera_replay_auto_prepare(shot)

        assert server_module.camera_replay_auto_prepare_queue.empty()

    def test_full_queue_drops_the_newest_request(self, monkeypatch):
        release = threading.Event()
        started = threading.Event()
        prepared = []

        def prepare(replay_id):
            started.set()
            release.wait(2.0)
            prepared.append(replay_id)
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)
        capacity = server_module._CAMERA_REPLAY_AUTO_PREPARE_CAPACITY
        server_module._queue_camera_replay_auto_prepare(self._shot(replay_id="busy"))
        assert started.wait(2.0)
        for index in range(capacity + 1):
            server_module._queue_camera_replay_auto_prepare(self._shot(replay_id=f"r{index}"))
        release.set()
        server_module.camera_replay_auto_prepare_queue.join()

        assert prepared == ["busy", *[f"r{index}" for index in range(capacity)]]

    @pytest.mark.parametrize(("platform", "expected"), [("linux", [10]), ("darwin", [])])
    def test_worker_lowers_only_its_own_linux_thread_priority(
        self, monkeypatch, platform, expected
    ):
        calls = []
        monkeypatch.setattr(server_module.sys, "platform", platform)
        monkeypatch.setattr(server_module.os, "nice", calls.append)

        server_module._lower_camera_replay_worker_priority()

        assert calls == expected

    def test_priority_errors_do_not_stop_the_worker(self, monkeypatch):
        def refuse(_increment):
            raise OSError("not permitted")

        monkeypatch.setattr(server_module.sys, "platform", "linux")
        monkeypatch.setattr(server_module.os, "nice", refuse)

        server_module._lower_camera_replay_worker_priority()

    @staticmethod
    def _ti_runtime(monkeypatch, dumping):
        capture_monitor = SimpleNamespace(dump_in_progress=dumping)
        runtime = SimpleNamespace(capture_monitor=capture_monitor)
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        return capture_monitor

    @pytest.mark.parametrize(("dumping", "expected"), [(None, False), (False, False), (True, True)])
    def test_capture_gate_includes_ti_dumps(self, monkeypatch, dumping, expected):
        if dumping is None:
            monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        else:
            self._ti_runtime(monkeypatch, dumping)

        assert server_module._shot_capture_in_progress() is expected

    def test_auto_prepare_waits_while_a_ti_dump_streams(self, monkeypatch):
        prepared = threading.Event()

        def prepare(replay_id):
            prepared.set()
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)
        capture_monitor = self._ti_runtime(monkeypatch, True)

        server_module._queue_camera_replay_auto_prepare(self._shot())

        assert not prepared.wait(0.2)
        capture_monitor.dump_in_progress = False
        assert self.ready.wait(2.0)

    def test_http_prepare_encodes_on_the_low_priority_worker(self, monkeypatch):
        threads = []

        def prepare(replay_id):
            threads.append(threading.current_thread().name)
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 200
        assert response.get_json() == {
            "id": "replay-123",
            "video_url": "/api/camera/replays/replay-123/video",
        }
        assert threads == ["camera-replay-prepare"]
        assert threading.current_thread().name not in threads

    def test_http_prepare_reports_busy_while_a_ti_dump_blocks_the_worker(self, monkeypatch):
        prepared = threading.Event()

        def prepare(replay_id):
            prepared.set()
            return SimpleNamespace(video_path=Path("/tmp/replay.mp4"), payload={"id": replay_id})

        self._manager(monkeypatch, prepare)
        capture_monitor = self._ti_runtime(monkeypatch, True)
        monkeypatch.setattr(server_module, "_CAMERA_REPLAY_REQUEST_TIMEOUT_S", 0.2)

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 503
        assert response.get_json() == {"error": "Camera replay is still being prepared"}
        assert not prepared.is_set()
        capture_monitor.dump_in_progress = False
        assert prepared.wait(2.0)

    def test_http_prepare_reports_busy_when_the_queue_stays_full(self, monkeypatch):
        self._manager(monkeypatch, lambda _replay_id: pytest.fail("queue was full"))
        self._ti_runtime(monkeypatch, True)
        monkeypatch.setattr(server_module, "_CAMERA_REPLAY_REQUEST_TIMEOUT_S", 0.05)
        full_queue = queue.Queue(maxsize=1)
        full_queue.put_nowait((None, "queued", None))
        monkeypatch.setattr(server_module, "camera_replay_auto_prepare_queue", full_queue)

        response = server_module.app.test_client().post("/api/camera/replays/replay-123/prepare")

        assert response.status_code == 503
        assert full_queue.get_nowait() == (None, "queued", None)
        full_queue.task_done()


class TestShutdownCleanup:
    """Tests for UI/server shutdown hardware cleanup."""

    def test_shutdown_cleanup_continues_if_kld7_stop_fails(self, monkeypatch):
        """One hardware cleanup failure must not skip OPS rolling-buffer cleanup."""
        calls = []

        class FailingKLD7:
            def stop(self):
                calls.append("kld7_vertical.stop")
                raise RuntimeError("stale kld7 stream")

        class GoodKLD7:
            def stop(self):
                calls.append("kld7_horizontal.stop")

        monkeypatch.setattr(server_module, "kld7_vertical", FailingKLD7())
        monkeypatch.setattr(server_module, "kld7_horizontal", GoodKLD7())
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
        monkeypatch.setattr(server_module, "stop_monitor", lambda: calls.append("stop_monitor"))

        server_module._cleanup_hardware_for_shutdown()

        assert calls == [
            "kld7_vertical.stop",
            "kld7_horizontal.stop",
            "stop_monitor",
        ]

    def test_shutdown_cleanup_is_idempotent(self, monkeypatch):
        """Duplicate shutdown requests must not stop hardware twice."""
        calls = []

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
        monkeypatch.setattr(server_module, "stop_monitor", lambda: calls.append("monitor"))

        first_request_owned_cleanup = server_module._cleanup_hardware_for_shutdown()
        duplicate_request_owned_cleanup = server_module._cleanup_hardware_for_shutdown()

        assert calls == ["monitor"]
        assert first_request_owned_cleanup is True
        assert duplicate_request_owned_cleanup is False

    def test_shutdown_stops_iwr6843_before_ops_monitor(self, monkeypatch):
        calls = []
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(
            server_module,
            "iwr6843_runtime",
            SimpleNamespace(stop=lambda: calls.append("iwr6843")),
        )
        monkeypatch.setattr(server_module, "shutdown_cleanup_started", False)
        monkeypatch.setattr(server_module, "stop_monitor", lambda: calls.append("ops243"))

        server_module._cleanup_hardware_for_shutdown()

        assert calls == ["iwr6843", "ops243"]

    def test_shutdown_step_logs_elapsed_time(self, caplog):
        """Hardware logs should identify which cleanup step is slow in the field."""
        with caplog.at_level("INFO", logger="openflight.server"):
            server_module._run_shutdown_step("OPS monitor stop", lambda: None)

        assert "Shutdown step OPS monitor stop completed in" in caplog.text

    def test_duplicate_shutdown_thread_cannot_exit_during_owner_cleanup(self, monkeypatch):
        """Only the thread that owns hardware cleanup may terminate the process."""
        exit_codes = []
        monkeypatch.setattr(server_module, "_cleanup_hardware_for_shutdown", lambda: False)
        monkeypatch.setattr(server_module.os, "_exit", exit_codes.append)

        server_module._shutdown_process_after_delay(delay_s=0)

        assert exit_codes == []


def test_shot_processing_status_is_forwarded_to_ui(monkeypatch):
    """The monitor lifecycle should be exposed as a dedicated UI event."""
    emitted = []
    monkeypatch.setattr(
        server_module.socketio,
        "emit",
        lambda event, payload: emitted.append((event, payload)),
    )

    server_module.on_shot_processing("capturing")

    assert emitted == [("shot_processing", {"state": "capturing"})]


def test_power_status_is_forwarded_to_ui_and_session_log(monkeypatch):
    emitted = []
    logged = []
    status = server_module.PowerStatus(
        available=True,
        provider="geekworm",
        state=PowerState.ON_BATTERY,
        battery_percent=42.0,
        battery_voltage_v=3.72,
        external_power=False,
        updated_at="2026-08-15T12:00:00+00:00",
    )
    monkeypatch.setattr(
        server_module.socketio,
        "emit",
        lambda event, payload: emitted.append((event, payload)),
    )
    monkeypatch.setattr(
        server_module,
        "get_session_logger",
        lambda: SimpleNamespace(log_power_status=logged.append),
    )
    health = {"cpu_temp_c": 61.3, "throttle": {"raw": "0x0", "throttled_now": False}}
    monkeypatch.setattr(server_module, "pi_health", lambda: health)

    server_module._on_power_status(status)
    server_module._log_power_status(status)

    # The UI gets the battery only; the session log also gets the Pi's health.
    assert emitted == [("power_status", status.to_dict())]
    assert logged == [{**status.to_dict(), **health}]


def test_power_status_log_survives_a_pi_without_thermal_file_or_vcgencmd(monkeypatch, tmp_path):
    from openflight import pi_health

    logged = []

    def missing_command():
        raise FileNotFoundError("vcgencmd")

    monkeypatch.setattr(
        server_module,
        "pi_health",
        lambda: pi_health.pi_health(temp_path=tmp_path / "missing", run_vcgencmd=missing_command),
    )
    monkeypatch.setattr(
        server_module,
        "get_session_logger",
        lambda: SimpleNamespace(log_power_status=logged.append),
    )
    status = server_module.PowerStatus(
        available=False,
        provider="geekworm",
        state=PowerState.UNAVAILABLE,
        battery_percent=None,
        battery_voltage_v=None,
        external_power=None,
        updated_at="2026-11-02T13:00:00+00:00",
    )

    server_module._log_power_status(status)

    assert logged == [{**status.to_dict(), "cpu_temp_c": None, "throttle": None}]


class TestIWR6843ShotIntegration:
    """TI angle processing must enrich, never suppress, an OPS shot."""

    def test_init_iwr6843_has_no_host_freeze_delay(self, monkeypatch, tmp_path):
        """Production capture must always request the firmware-frozen boundary ring immediately."""
        captured = {}
        calibration = Calibration.identity()

        class FakeCaptureMonitor:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.port = "/dev/ttyUSB0"

            def start(self, *, armed=True):
                captured["armed"] = armed
                return None

            def stop(self):
                return None

        monkeypatch.setattr(Calibration, "load", lambda _path: calibration)
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.IWR6843CaptureMonitor",
            FakeCaptureMonitor,
        )
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.tx_order_from_config",
            lambda _path: "normal",
        )

        assert server_module.init_iwr6843(
            port="/dev/ttyUSB0",
            config_path="snapshot.cfg",
            calibration_path="cal.json",
            output_dir=tmp_path,
            trigger_pin=17,
            tee_range_m=1.575,
            net_range_m=4.6,
            tx_order="auto",
            capture_timeout_s=12.0,
        )

        assert "freeze_delay_s" not in captured
        assert captured["armed"] is False
        assert server_module.iwr6843_runtime.tdm_sign_policy == "positive"
        assert server_module.iwr6843_runtime_config["tdm_sign_policy"] == "positive"
        server_module.iwr6843_runtime = None

    def test_init_iwr6843_wires_horizontal_calibration_into_runtime(self, monkeypatch, tmp_path):
        """Horizontal calibration must reach IWR6843Runtime, not just be parsed.

        A flag that parses but never reaches the runtime silently reports every
        club path relative to boresight instead of the target line.
        """
        calibration = Calibration.identity()

        class FakeCaptureMonitor:
            def __init__(self, **kwargs):
                self.port = "/dev/ttyUSB0"

            def start(self, *, armed=True):
                return None

            def stop(self):
                return None

        monkeypatch.setattr(Calibration, "load", lambda _path: calibration)
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.IWR6843CaptureMonitor",
            FakeCaptureMonitor,
        )
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.tx_order_from_config",
            lambda _path: "normal",
        )

        assert server_module.init_iwr6843(
            port="/dev/ttyUSB0",
            config_path="snapshot.cfg",
            calibration_path="cal.json",
            output_dir=tmp_path,
            trigger_pin=17,
            tee_range_m=1.575,
            net_range_m=4.6,
            tx_order="auto",
            capture_timeout_s=12.0,
            azimuth_offset_deg=1.5,
            horizontal_phase_reference_rad=-0.5,
        )

        assert server_module.iwr6843_runtime.azimuth_offset_deg == 1.5
        assert server_module.iwr6843_runtime.horizontal_phase_reference_rad == -0.5
        assert server_module.iwr6843_runtime_config["azimuth_offset_deg"] == 1.5
        assert server_module.iwr6843_runtime_config["horizontal_phase_reference_rad"] == -0.5
        server_module.iwr6843_runtime = None

    def test_init_iwr6843_reports_sound_edges_to_the_ops_trigger(self, monkeypatch, tmp_path):
        """A TI-seen edge lets the OPS trigger re-arm a radar that sent no dump."""
        captured = {}

        class FakeCaptureMonitor:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.port = "/dev/ttyUSB0"

            def start(self, *, armed=True):
                return None

        camera_edges = []
        write_deferrals = []
        monkeypatch.setattr(
            server_module,
            "camera_capture_runtime",
            SimpleNamespace(
                notify_trigger=camera_edges.append,
                set_write_deferral=write_deferrals.append,
            ),
        )
        monkeypatch.setattr(Calibration, "load", lambda _path: Calibration.identity())
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.IWR6843CaptureMonitor",
            FakeCaptureMonitor,
        )
        monkeypatch.setattr(
            "openflight.iwr6843.monitor.tx_order_from_config",
            lambda _path: "normal",
        )
        assert server_module.init_iwr6843(
            port="/dev/ttyUSB0",
            config_path="snapshot.cfg",
            calibration_path="cal.json",
            output_dir=tmp_path,
            trigger_pin=17,
            tee_range_m=1.575,
            net_range_m=4.6,
            tx_order="auto",
            capture_timeout_s=12.0,
        )
        server_module.iwr6843_runtime = None

        ops_edges = []
        sound_trigger = SimpleNamespace(notify_trigger_edge=lambda: ops_edges.append(True))
        for current_monitor in (
            None,
            SimpleNamespace(),
            SimpleNamespace(trigger=SimpleNamespace()),
            SimpleNamespace(trigger=sound_trigger),
        ):
            monkeypatch.setattr(server_module, "monitor", current_monitor)
            for observer in captured["trigger_observers"]:
                observer(123.0)

        assert camera_edges == [123.0] * 4
        assert ops_edges == [True]
        assert len(write_deferrals) == 1

    def test_accepted_lcmf_angle_is_applied_to_existing_shot_contract(self, monkeypatch):
        emitted = []
        measurement = SimpleNamespace(
            accepted=True,
            angle_deg=17.42,
            n_snapshots=20,
            n_frames=6,
            component_std_deg=1.1,
            to_dict=lambda: {"estimator": "lcmf_v1", "launch_angle_deg": 17.42},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=Path("/tmp/test.l3dump"),
            raw=b"raw",
            dump_duration_s=7.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
            )
        )
        logged = []
        session = SimpleNamespace(
            stats={"shots_detected": 2},
            log_iwr6843_capture=lambda **kwargs: logged.append(kwargs),
        )
        edge_timing = EdgeTimingLog()
        edge_timing.record(100.01, callback_timestamp=100.01, callback_monotonic_ns=7, ticks=None)
        monkeypatch.setattr(server_module, "sound_edge_timing", edge_timing)
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )
        elapsed = server_module._process_iwr6843_angle(shot)

        assert elapsed is not None
        assert shot.launch_angle_vertical == pytest.approx(17.42)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.angle_source == "radar"
        assert shot.launch_angle_horizontal is None
        assert logged[0]["shot_number"] == 3
        assert logged[0]["ball_speed_mph"] == 100.0
        assert logged[0]["edge_fields"]["edge_callback_timestamp"] == 100.01
        assert logged[0]["edge_fields"]["edge_callback_monotonic_ns"] == 7
        assert logged[0]["measurement"]["estimator"] == "lcmf_v1"
        assert emitted == [
            (
                "trigger_diagnostic_update",
                {
                    "timestamp": shot.timestamp.isoformat(),
                    "iwr6843": {
                        "state": "accepted",
                        "reason": "accepted",
                        "angle_deg": 17.42,
                    },
                },
            )
        ]

    def test_iwr6843_horizontal_confidence_derived_from_coherence(self, monkeypatch):
        measurement = SimpleNamespace(
            accepted=True,
            angle_deg=18.5,
            horizontal_deg=2.25,
            horizontal_confidence=0.63,
            horizontal_status="hlcmf_v0_accepted",
            n_snapshots=18,
            n_frames=5,
            component_std_deg=1.4,
            to_dict=lambda: {
                "estimator": "lcmf_v1",
                "launch_angle_deg": 18.5,
                "horizontal_deg": 2.25,
                "horizontal_confidence": 0.63,
            },
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=Path("/tmp/test.l3dump"),
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)

        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.launch_angle_vertical == pytest.approx(18.5)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_horizontal == pytest.approx(2.25)
        assert shot.launch_angle_horizontal_source == "radar"
        # Confidence is now derived from HLCMF-v0 coherence (0.63 here), not
        # a hardcoded 0.95 -- see openflight.server.horizontal_confidence_from.
        assert shot.launch_angle_horizontal_confidence == pytest.approx(0.63)

    def test_debug_mode_exposes_rejected_club_candidates_without_promoting_them(self, monkeypatch):
        measurement = SimpleNamespace(
            accepted=False,
            status="rejected_track_quality",
            to_dict=lambda: {"status": "rejected_track_quality"},
        )
        club_path = SimpleNamespace(
            accepted=False,
            status="rejected_phase_span",
            path_deg=None,
            candidate_path_deg=5.8,
            candidate_path_status="candidate_available",
            candidate_attack_angle_deg=-4.9,
            attack_angle_status="candidate_available",
            to_dict=lambda: {
                "status": "rejected_phase_span",
                "candidate_path_deg": 5.8,
                "candidate_attack_angle_deg": -4.9,
            },
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=Path("/tmp/test.l3dump"),
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
                club_path=club_path,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "debug_mode", True)
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.club_path_deg is None
        assert shot.club_angle_deg is None
        assert shot.experimental_club_path_deg == pytest.approx(5.8)
        assert shot.experimental_club_path_status == "rejected_phase_span"
        assert shot.experimental_attack_angle_deg == pytest.approx(-4.9)
        assert shot.experimental_attack_angle_status == "candidate_available"

    def test_non_debug_mode_emits_experimental_club_candidates(self, monkeypatch):
        measurement = SimpleNamespace(
            accepted=False,
            status="rejected_track_quality",
            to_dict=lambda: {"status": "rejected_track_quality"},
        )
        club_path = SimpleNamespace(
            accepted=False,
            status="rejected_phase_span",
            path_deg=None,
            candidate_path_deg=5.8,
            candidate_path_status="candidate_noisy_fit",
            candidate_attack_angle_deg=-4.9,
            attack_angle_status="candidate_available",
            to_dict=lambda: {},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=None,
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
                club_path=club_path,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.club_path_deg is None
        assert shot.club_angle_deg is None
        assert shot.experimental_club_path_deg == pytest.approx(5.8)
        assert shot.experimental_club_path_status == "candidate_noisy_fit"
        assert shot.experimental_attack_angle_deg == pytest.approx(-4.9)
        assert shot.experimental_attack_angle_status == "candidate_available"

    def test_accepted_iwr_club_path_remains_experimental(self, monkeypatch):
        measurement = SimpleNamespace(
            accepted=False,
            status="rejected_track_quality",
            to_dict=lambda: {"status": "rejected_track_quality"},
        )
        club_path = SimpleNamespace(
            accepted=True,
            status="accepted",
            path_deg=0.0,
            confidence=0.82,
            n_frames=5,
            candidate_path_deg=2.6,
            candidate_path_status="candidate_available",
            candidate_attack_angle_deg=-4.1,
            attack_angle_status="candidate_available",
            to_dict=lambda: {"status": "accepted", "path_deg": 0.0},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=None,
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
                club_path=club_path,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.club_path_deg is None
        assert shot.club_angle_deg is None
        assert shot.experimental_club_path_deg == pytest.approx(0.0)
        assert shot.experimental_club_path_status == "accepted"
        assert shot.experimental_attack_angle_deg == pytest.approx(-4.1)
        assert shot.experimental_attack_angle_status == "candidate_available"

    def test_debug_mode_exposes_club_rejection_without_candidate(self, monkeypatch):
        measurement = SimpleNamespace(
            accepted=False,
            status="rejected_track_quality",
            to_dict=lambda: {"status": "rejected_track_quality"},
        )
        club_path = SimpleNamespace(
            accepted=False,
            status="rejected_no_club_track",
            path_deg=None,
            candidate_path_deg=None,
            candidate_path_status=None,
            candidate_attack_angle_deg=None,
            attack_angle_status=None,
            to_dict=lambda: {"status": "rejected_no_club_track"},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=None,
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
                club_path=club_path,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "debug_mode", True)
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            impact_timestamp=100.0,
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.experimental_club_path_deg is None
        assert shot.experimental_club_path_status == "rejected_no_club_track"
        assert shot.experimental_attack_angle_deg is None
        assert shot.experimental_attack_angle_status == "rejected_no_club_track"

    def test_horizontal_fallback_does_not_invent_measurement_for_lcmf_angle(self, monkeypatch):
        monkeypatch.setattr(server_module, "iwr6843_runtime", SimpleNamespace())
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
            launch_angle_vertical=17.42,
            launch_angle_vertical_source="radar",
            angle_source="radar",
        )

        server_module._ensure_user_facing_launch_angles(shot)

        assert shot.launch_angle_vertical == pytest.approx(17.42)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_confidence is None
        assert shot.launch_angle_vertical_confidence is None
        assert shot.launch_angle_horizontal is None
        assert shot.launch_angle_horizontal_source is None
        assert shot.launch_angle_horizontal_confidence is None

    @staticmethod
    def _shot_without_vertical_angle(spin_confidence):
        return Shot(
            ball_speed_mph=75.75821105273437,
            club_speed_mph=58.346041576904305,
            timestamp=datetime.now(),
            club=ClubType.GW,
            spin_rpm=2471.923828125,
            spin_confidence=spin_confidence,
        )

    def _expected_estimate(self, shot, spin_rpm):
        return server_module.estimate_launch_angle(
            shot.club,
            shot.ball_speed_mph,
            club_speed_mph=shot.club_speed_mph,
            spin_rpm=spin_rpm,
        )

    def test_estimated_angle_ignores_unconfirmed_spin_candidate(self, monkeypatch):
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        shot = self._shot_without_vertical_angle(0.5175857303004757)

        server_module._ensure_user_facing_launch_angles(shot)

        angle, confidence = self._expected_estimate(shot, None)
        assert shot.launch_angle_vertical == pytest.approx(angle)
        assert shot.launch_angle_vertical_confidence == pytest.approx(confidence)
        assert shot.launch_angle_vertical == pytest.approx(31.4, abs=0.05)
        assert shot.launch_angle_vertical_confidence == pytest.approx(0.35, abs=1e-6)
        assert shot.launch_angle_vertical_source == "estimated"

    def test_estimated_angle_uses_trusted_spin(self, monkeypatch):
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        shot = self._shot_without_vertical_angle(0.75)

        server_module._ensure_user_facing_launch_angles(shot)

        angle, confidence = self._expected_estimate(shot, 2471.923828125)
        assert shot.launch_angle_vertical == pytest.approx(angle)
        assert shot.launch_angle_vertical_confidence == pytest.approx(confidence)
        assert angle != pytest.approx(self._expected_estimate(shot, None)[0])

    def test_estimated_angle_ignores_spin_without_confidence(self, monkeypatch):
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        shot = self._shot_without_vertical_angle(None)

        server_module._ensure_user_facing_launch_angles(shot)

        angle, confidence = self._expected_estimate(shot, None)
        assert shot.launch_angle_vertical == pytest.approx(angle)
        assert shot.launch_angle_vertical_confidence == pytest.approx(confidence)

    def test_missing_ti_capture_preserves_ops_shot(self, monkeypatch):
        emitted = []
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(capture=None, measurement=None)
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert shot.ball_speed_mph == 100.0
        assert shot.launch_angle_vertical is None
        assert emitted == [
            (
                "trigger_diagnostic_update",
                {
                    "timestamp": shot.timestamp.isoformat(),
                    "iwr6843": {
                        "state": "error",
                        "reason": "no capture matched the OPS impact timestamp",
                    },
                },
            )
        ]

    def test_rejected_ti_measurement_updates_existing_trigger_row(self, monkeypatch):
        emitted = []
        measurement = SimpleNamespace(
            accepted=False,
            status="rejected_track_quality",
            to_dict=lambda: {"status": "rejected_track_quality"},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=Path("/tmp/test.l3dump"),
            raw=b"raw",
            dump_duration_s=4.5,
            error=None,
            valid=True,
            sequence=1,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
                club_path=None,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        server_module._process_iwr6843_angle(shot)

        assert emitted == [
            (
                "trigger_diagnostic_update",
                {
                    "timestamp": shot.timestamp.isoformat(),
                    "iwr6843": {
                        "state": "rejected",
                        "reason": "rejected_track_quality",
                    },
                },
            )
        ]


_RANGE_20261025_TI_FIXTURE = Path(__file__).parent / "fixtures" / "range_20261025_ti_launch.json"


class TestIWR6843LaunchPlausibility:
    """The K-LD7 launch guardrail also screens the IWR6843 vertical angle."""

    # Shot OF26 of the 2026-10-25 range session: 7-wood, channel two8 stuck at
    # the -5 deg grid edge, TI vertical -0.47 deg, displayed carry 0 m.
    OF26 = {
        "ball_speed_mph": 131.76226492236327,
        "club_speed_mph": 99.483564782959,
        "spin_rpm": 7470.703125,
        "spin_confidence": 0.4026717869953167,
        "ti_vertical_deg": -0.46811383175677956,
        "ti_horizontal_deg": -6.970304227184274,
    }

    @staticmethod
    def _install_runtime(monkeypatch, angle_deg, horizontal_deg=None):
        emitted = []
        measurement = SimpleNamespace(
            accepted=True,
            status="accepted",
            angle_deg=angle_deg,
            horizontal_deg=horizontal_deg,
            horizontal_confidence=1.0 if horizontal_deg is not None else None,
            horizontal_status="hlcmf_v1_accepted" if horizontal_deg is not None else None,
            n_snapshots=42,
            n_frames=12,
            component_std_deg=0.0,
            to_dict=lambda: {"estimator": "lcmf_v1", "launch_angle_deg": angle_deg},
        )
        capture = SimpleNamespace(
            trigger_timestamp=100.01,
            path=Path("/tmp/test.l3dump"),
            raw=b"raw",
            dump_duration_s=7.4,
            error=None,
            valid=True,
            sequence=62,
        )
        runtime = SimpleNamespace(
            process_shot=lambda **kwargs: SimpleNamespace(
                capture=capture,
                measurement=measurement,
            )
        )
        monkeypatch.setattr(server_module, "iwr6843_runtime", runtime)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        return emitted

    def _of26_shot(self, **overrides):
        values = {
            "ball_speed_mph": self.OF26["ball_speed_mph"],
            "club_speed_mph": self.OF26["club_speed_mph"],
            "spin_rpm": self.OF26["spin_rpm"],
            "spin_confidence": self.OF26["spin_confidence"],
            "timestamp": datetime.now(),
            "impact_timestamp": 100.0,
            "club": ClubType.WOOD_7,
            "club_selection": "7-wood",
        }
        values.update(overrides)
        return Shot(**values)

    def test_of26_negative_ti_angle_is_dropped_and_reason_stored(self, monkeypatch):
        emitted = self._install_runtime(
            monkeypatch, self.OF26["ti_vertical_deg"], self.OF26["ti_horizontal_deg"]
        )
        shot = self._of26_shot()

        server_module._process_iwr6843_angle(shot)

        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_vertical_source is None
        assert shot.launch_angle_vertical_confidence is None
        assert shot.angle_source is None
        assert shot.launch_angle_vertical_rejection_reason == "ti_launch_implausible"
        # The TX2 horizontal proxy has its own coherence gate and stays.
        assert shot.launch_angle_horizontal == pytest.approx(self.OF26["ti_horizontal_deg"])
        assert shot.launch_angle_horizontal_source == "radar"
        assert emitted == [
            (
                "trigger_diagnostic_update",
                {
                    "timestamp": shot.timestamp.isoformat(),
                    "iwr6843": {
                        "state": "rejected",
                        "reason": "ti_launch_implausible",
                        "angle_deg": -0.47,
                    },
                },
            )
        ]

    def test_of26_rejection_is_logged(self, monkeypatch, caplog):
        self._install_runtime(monkeypatch, self.OF26["ti_vertical_deg"])

        with caplog.at_level("WARNING"):
            server_module._process_iwr6843_angle(self._of26_shot())

        assert "IWR6843 vertical angle -0.47° rejected: ti_launch_implausible" in caplog.text
        # OF26 fails the 0-45 deg floor, not the delta window (11.0 < 21.2):
        # the line must name the rule that fired.
        assert "outside the 0-45° bounds" in caplog.text
        assert "delta > allowed" not in caplog.text

    def test_of26_guard_details_reach_the_session_log(self, monkeypatch):
        """Like the K-LD7 path, the TI guard verdict is replayable from the session log."""
        self._install_runtime(monkeypatch, self.OF26["ti_vertical_deg"])
        logged = []
        session_log = SimpleNamespace(
            log_iwr6843_capture=lambda **kwargs: logged.append(kwargs),
        )
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)
        monkeypatch.setattr(server_module, "_shot_number_for_log", lambda shot, log: 26)

        server_module._process_iwr6843_angle(self._of26_shot())

        assert len(logged) == 1
        guard = logged[0]["launch_guard"]
        assert guard["angle_deg"] == pytest.approx(self.OF26["ti_vertical_deg"])
        assert guard["plausible"] is False
        assert guard["reason"] == "ti_launch_implausible"
        assert guard["rule"] == "bounds_0_45"
        assert guard["skipped"] is False
        assert guard["expected_launch_deg"] == pytest.approx(10.5)
        assert guard["delta_deg"] == pytest.approx(11.0)
        assert guard["allowed_delta_deg"] is not None

    def test_plausible_ti_angle_logs_a_passing_guard(self, monkeypatch):
        self._install_runtime(monkeypatch, 12.0)
        logged = []
        session_log = SimpleNamespace(
            log_iwr6843_capture=lambda **kwargs: logged.append(kwargs),
        )
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)
        monkeypatch.setattr(server_module, "_shot_number_for_log", lambda shot, log: 26)
        shot = self._of26_shot()

        server_module._process_iwr6843_angle(shot)

        guard = logged[0]["launch_guard"]
        assert guard["plausible"] is True
        assert guard["reason"] is None
        assert shot.launch_angle_vertical == pytest.approx(12.0)
        assert shot.launch_angle_vertical_rejection_reason is None

    def test_rejection_above_expected_names_the_delta_rule(self, monkeypatch, caplog):
        self._install_runtime(monkeypatch, 60.0)

        with caplog.at_level("WARNING"):
            server_module._process_iwr6843_angle(self._of26_shot())

        assert "too far above the expected launch (delta > allowed)" in caplog.text
        assert "0-45° bounds" not in caplog.text

    def test_guard_details_name_the_rule(self):
        shot = self._of26_shot()
        below = server_module._ti_vertical_launch_is_plausible(-0.47, shot)
        above = server_module._ti_vertical_launch_is_plausible(31.0, shot)
        skipped = server_module._ti_vertical_launch_is_plausible(
            -0.47, self._of26_shot(club=ClubType.UNKNOWN, club_selection=None)
        )

        assert below[0] is False
        assert below[1]["rule"] == "bounds_0_45"
        assert above[0] is True
        assert above[1]["rule"] == "delta_above_expected"
        assert skipped[1]["rule"] is None

    def test_of26_falls_back_to_estimate_for_launch_speed_and_carry(self, monkeypatch):
        self._install_runtime(
            monkeypatch, self.OF26["ti_vertical_deg"], self.OF26["ti_horizontal_deg"]
        )
        # Session geometry: tee 1.5 m, ball 0.02135 m, radar 0.132 m.
        monkeypatch.setattr(server_module, "ball_speed_correction_enabled", True)
        monkeypatch.setattr(server_module, "ball_speed_correction_distance_ft", 1.5 * 3.28084)
        monkeypatch.setattr(
            server_module,
            "ball_speed_correction_ball_above_radar_ft",
            (0.02135 - 0.132) * 3.28084,
        )
        monkeypatch.setattr(server_module, "ballistics_enabled", True)
        monkeypatch.setattr(server_module, "calculated_spin_enabled", False)
        monkeypatch.setattr(server_module, "air_density_kg_m3", TRACKMAN_FLAT_AIR_DENSITY)
        shot = self._of26_shot()

        server_module._process_iwr6843_angle(shot)
        server_module._finalize_shot_metrics(shot)

        expected_launch, expected_conf = server_module.estimate_launch_angle(
            ClubType.WOOD_7,
            self.OF26["ball_speed_mph"],
            club_speed_mph=self.OF26["club_speed_mph"],
        )
        assert shot.launch_angle_vertical == pytest.approx(expected_launch)
        assert shot.launch_angle_vertical == pytest.approx(10.5)
        assert shot.launch_angle_vertical_source == "estimated"
        assert shot.launch_angle_vertical_confidence == pytest.approx(expected_conf)
        assert shot.angle_source == "estimated"
        assert shot.launch_angle_vertical_rejection_reason == "ti_launch_implausible"
        # The cosine correction now runs on the estimate, not on -0.47 deg.
        assert shot.ball_speed_raw_mph == pytest.approx(self.OF26["ball_speed_mph"])
        assert shot.ball_speed_mph == pytest.approx(
            server_module.correct_ball_speed(
                self.OF26["ball_speed_mph"],
                expected_launch,
                1.5 * 3.28084,
                (0.02135 - 0.132) * 3.28084,
            )
        )
        # The carry was 0 yd with the stuck angle; it now flies, still on
        # the club-typical spin (OPS spin is not trusted).
        assert shot.carry_model == "ballistic"
        assert shot.carry_spin_source == "club_typical"
        assert shot.carry_spin_adjusted == pytest.approx(198.0, abs=1.0)

    @pytest.mark.parametrize("angle_deg", [-0.5, -5.0, 45.0, 60.0])
    def test_out_of_window_ti_angles_are_rejected(self, monkeypatch, angle_deg):
        self._install_runtime(monkeypatch, angle_deg)
        shot = self._of26_shot()

        server_module._process_iwr6843_angle(shot)

        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_vertical_rejection_reason == "ti_launch_implausible"

    @pytest.mark.parametrize("angle_deg", [0.0, 3.3, 10.5, 20.6, 31.0])
    def test_plausible_ti_angles_are_applied_unchanged(self, monkeypatch, angle_deg):
        emitted = self._install_runtime(monkeypatch, angle_deg)
        shot = self._of26_shot()

        server_module._process_iwr6843_angle(shot)

        assert shot.launch_angle_vertical == pytest.approx(angle_deg)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.angle_source == "radar"
        assert shot.launch_angle_vertical_rejection_reason is None
        assert emitted[0][1]["iwr6843"]["state"] == "accepted"

    def test_unknown_club_skips_the_guard(self, monkeypatch):
        self._install_runtime(monkeypatch, -0.47)
        shot = self._of26_shot(club=ClubType.UNKNOWN, club_selection=None)

        server_module._process_iwr6843_angle(shot)

        assert shot.launch_angle_vertical == pytest.approx(-0.47)
        assert shot.launch_angle_vertical_rejection_reason is None

    def test_guard_ignores_untrusted_spin(self):
        untrusted = self._of26_shot(spin_rpm=2000.0, spin_confidence=0.59)
        no_spin = self._of26_shot(spin_rpm=None, spin_confidence=None)

        assert server_module._ti_vertical_launch_is_plausible(
            31.0, untrusted
        ) == server_module._ti_vertical_launch_is_plausible(31.0, no_spin)

    def test_guard_uses_trusted_spin(self, monkeypatch):
        # 31 deg sits right at the delta window: with trusted 2000 rpm the
        # expected launch drops (10.5 -> 9.5 deg) and the angle fails, with the
        # same spin untrusted (OPS cap 0.59) it passes.
        trusted = self._of26_shot(spin_rpm=2000.0, spin_confidence=SPIN_CONFIDENCE_HIGH)
        untrusted = self._of26_shot(spin_rpm=2000.0, spin_confidence=0.59)

        trusted_ok, trusted_details = server_module._ti_vertical_launch_is_plausible(31.0, trusted)
        untrusted_ok, untrusted_details = server_module._ti_vertical_launch_is_plausible(
            31.0, untrusted
        )

        assert trusted_ok is False
        assert untrusted_ok is True
        assert trusted_details["expected_launch_deg"] < untrusted_details["expected_launch_deg"]

        self._install_runtime(monkeypatch, 31.0)
        server_module._process_iwr6843_angle(trusted)
        assert trusted.launch_angle_vertical is None
        assert trusted.launch_angle_vertical_rejection_reason == "ti_launch_implausible"

    def test_rejection_reason_is_part_of_shot_schema(self):
        shot = self._of26_shot(launch_angle_vertical_rejection_reason="ti_launch_implausible")

        assert shot.to_dict()["launch_angle_vertical_rejection_reason"] == "ti_launch_implausible"
        assert (
            shot_to_dict(shot)["launch_angle_vertical_rejection_reason"] == "ti_launch_implausible"
        )
        assert self._of26_shot().to_dict()["launch_angle_vertical_rejection_reason"] is None

    def test_range_20261025_replay_only_of26_changes(self):
        """Replay every TI vertical angle of the 2026-10-25 range session."""
        shots = json.loads(_RANGE_20261025_TI_FIXTURE.read_text())["shots"]
        ti_measured = [row for row in shots if str(row["ti_status"]).startswith("accepted")]
        assert len(shots) == 73
        assert len(ti_measured) == 69
        # Every TI-accepted angle reached the shot as a radar angle in the session.
        assert {row["logged_vla_source"] for row in ti_measured} == {"radar"}

        rejected = []
        for row in ti_measured:
            shot = Shot(
                ball_speed_mph=row["ball_raw_mph"],
                club_speed_mph=row["club_mph"],
                spin_rpm=row["spin_rpm"],
                spin_confidence=row["spin_conf"],
                timestamp=datetime.now(),
                club=club_category(row["club"]),
            )
            plausible, _ = server_module._ti_vertical_launch_is_plausible(row["ti_vla"], shot)
            if not plausible:
                rejected.append((row["shot"], row["club"], row["ti_vla"]))

        assert rejected == [(26, "7-wood", -0.468)]


class TestSessionErrorLogging:
    """Session JSONL should record shot-pipeline failures, not only Python logs."""

    def test_on_shot_detected_logs_kld7_processing_error(self, monkeypatch):
        logged_errors = []

        class FailingTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                raise RuntimeError("snapshot failed")

            def get_angle_for_shot(self, **kwargs):
                return None

            def get_club_angle(self, **kwargs):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", FailingTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda error, **kwargs: logged_errors.append((error, kwargs)),
        )
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=150.0,
            club_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
        )
        on_shot_detected(shot)
        _wait_for_shot_finalization_idle()

        assert logged_errors
        assert logged_errors[0][0] == "Angle/spin-axis post-processing failed"
        assert logged_errors[0][1]["component"] == "server"
        assert logged_errors[0][1]["context"]["stage"] == "angle_postprocessing"
        assert logged_errors[0][1]["exc"].__class__.__name__ == "RuntimeError"

    def test_set_radar_config_logs_failure_to_session(self, monkeypatch):
        logged_errors = []
        emitted = []

        class FailingRadar:
            def set_min_speed_filter(self, _value):
                raise ValueError("invalid speed")

        class StubMonitor:
            radar = FailingRadar()

        monkeypatch.setattr(server_module, "monitor", StubMonitor())
        monkeypatch.setattr(server_module, "mock_mode", False)
        monkeypatch.setattr(server_module, "radar_config", {"min_speed": 10})
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda error, **kwargs: logged_errors.append((error, kwargs)),
        )
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)

        server_module.handle_set_radar_config({"min_speed": 99})

        assert logged_errors
        assert logged_errors[0][0] == "Radar config update failed"
        assert logged_errors[0][1]["context"]["stage"] == "set_radar_config"
        assert emitted[-1][0] == "radar_config_error"

    def test_set_radar_config_logs_not_connected_to_session(self, monkeypatch):
        logged_errors = []

        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "mock_mode", True)
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda error, **kwargs: logged_errors.append((error, kwargs)),
        )
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        server_module.handle_set_radar_config({"min_speed": 99})

        assert logged_errors
        assert "not connected" in logged_errors[0][0]


class TestKLD7Initialization:
    """Tests for K-LD7 startup wiring."""

    def test_start_monitor_applies_cli_debug_mode(self, monkeypatch):
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)

        server_module.start_monitor(mock=True, trigger_type="sound", debug=True)

        assert server_module.debug_mode is True
        server_module.stop_monitor()

    def test_init_kld7_uses_stable_runtime_defaults(self, monkeypatch):
        import openflight.kld7 as kld7_package

        created = []

        class FakeKLD7Tracker:
            def __init__(self, **kwargs):
                self.port = kwargs["port"]
                self.kwargs = kwargs
                self.started = False
                created.append(self)

            def connect(self):
                return True

            def start(self):
                self.started = True

        monkeypatch.setattr(kld7_package, "KLD7Tracker", FakeKLD7Tracker)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)

        ok = server_module.init_kld7(
            port="/dev/test-kld7",
            orientation="horizontal",
            angle_offset_deg=1.5,
            base_freq=2,
        )

        assert ok is True
        assert created[0].started is True
        assert server_module.kld7_horizontal is created[0]
        assert created[0].kwargs == {
            "port": "/dev/test-kld7",
            "orientation": "horizontal",
            "angle_offset_deg": 1.5,
            "base_freq": 2,
            "buffer_seconds": 6.0,
            "vertical_estimator": "naive",
            "mount_tilt_deg": 18.0,
            "ball_distance_ft": 5.5,
            "vertical_flight_window_net_distance_ft": 10.0,
        }


class TestStaticRoutes:
    """Tests for frontend static routes."""

    def test_display_route_serves_react_app(self):
        """Direct refresh of /display should return the React app."""
        client = server_module.app.test_client()

        response = client.get("/display")

        assert response.status_code == 200
        assert b'<div id="root"></div>' in response.data

    def test_display_route_accepts_trailing_slash(self):
        """TV browsers may preserve a trailing slash on /display/."""
        client = server_module.app.test_client()

        response = client.get("/display/")

        assert response.status_code == 200
        assert b'<div id="root"></div>' in response.data

    @pytest.mark.parametrize("path", ["/companion", "/companion/"])
    def test_companion_route_serves_react_app(self, path):
        """Phones open /companion directly or from a home-screen bookmark."""
        response = server_module.app.test_client().get(path)

        assert response.status_code == 200
        assert b'<div id="root"></div>' in response.data

    def test_display_route_falls_back_when_dist_missing(self, monkeypatch, tmp_path):
        """Clean checkouts without ui/dist should still serve the React shell."""
        monkeypatch.setattr(server_module, "FRONTEND_DIST_DIR", tmp_path / "missing-dist")
        monkeypatch.setattr(server_module.app, "static_folder", str(tmp_path / "missing-dist"))

        client = server_module.app.test_client()
        response = client.get("/display")

        assert response.status_code == 200
        assert b'<div id="root"></div>' in response.data


class TestShotToDict:
    """Tests for shot_to_dict conversion."""

    def test_basic_conversion(self):
        """Convert a basic shot to dict."""
        shot = Shot(
            ball_speed_mph=150.5,
            club_speed_mph=103.2,
            timestamp=datetime(2024, 1, 15, 10, 30, 0),
            club=ClubType.DRIVER,
        )

        raw = shot.to_dict()
        result = shot_to_dict(shot)

        assert set(result) == (set(raw) - {"mode", "readings", "readings_count"}) | {
            "carry_span_min_yards",
            "carry_span_max_yards",
        }
        assert result["ball_speed_mph"] == 150.5
        assert result["club_speed_mph"] == 103.2
        assert result["club"] == "driver"
        assert result["profile_name"] == ""
        assert result["timestamp"] == "2024-01-15T10:30:00"
        assert "estimated_carry_yards" in result
        assert "carry_range" in result
        assert len(result["carry_range"]) == 2

    def test_null_club_speed(self):
        """Shot without club speed should have null in dict."""
        shot = Shot(
            ball_speed_mph=150.0,
            timestamp=datetime.now(),
        )

        result = shot_to_dict(shot)

        assert result["club_speed_mph"] is None
        assert result["smash_factor"] is None

    def test_rounding(self):
        """Values should be rounded appropriately."""
        shot = Shot(
            ball_speed_mph=150.456,
            club_speed_mph=103.789,
            timestamp=datetime.now(),
        )

        result = shot_to_dict(shot)

        assert result["ball_speed_mph"] == 150.5  # 1 decimal
        assert result["club_speed_mph"] == 103.8  # 1 decimal
        assert result["smash_factor"] == 1.45  # 2 decimals

    def test_angle_source_field(self):
        """shot_to_dict should include angle_source."""
        shot = Shot(
            ball_speed_mph=150.0,
            timestamp=datetime.now(),
            launch_angle_vertical=12.5,
            launch_angle_confidence=0.8,
            launch_angle_vertical_confidence=0.8,
            launch_angle_vertical_source="radar",
            angle_source="radar",
        )
        result = shot_to_dict(shot)
        assert result["angle_source"] == "radar"
        assert result["launch_angle_vertical_confidence"] == 0.8
        assert result["launch_angle_vertical_source"] == "radar"
        assert result["launch_angle_horizontal_confidence"] is None
        assert result["launch_angle_horizontal_source"] is None

    def test_camera_assisted_horizontal_provenance_is_included(self):
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_horizontal=0.6,
            launch_angle_horizontal_confidence=0.75,
            launch_angle_horizontal_source="camera_assisted_experimental",
            iwr6843_horizontal_deg=17.9,
            iwr6843_horizontal_confidence=0.8,
            experimental_camera_horizontal_deg=0.6,
            experimental_camera_horizontal_confidence=0.75,
            experimental_camera_horizontal_status="camera_assisted_high",
            experimental_camera_iwr_delta_deg=-17.3,
        )

        result = shot_to_dict(shot)

        assert result["launch_angle_horizontal"] == 0.6
        assert result["launch_angle_horizontal_source"] == "camera_assisted_experimental"
        assert result["iwr6843_horizontal_deg"] == 17.9
        assert result["experimental_camera_horizontal_deg"] == 0.6
        assert result["experimental_camera_horizontal_status"] == "camera_assisted_high"
        assert result["experimental_camera_iwr_delta_deg"] == -17.3
        assert "iwr6843_ball_range_evidence" not in result

    def test_live_fusion_selects_camera_and_preserves_iwr(self, monkeypatch, tmp_path):
        import numpy as np

        from openflight.camera import ball_flight

        estimate_call = {}

        def fake_estimate(*_args, **kwargs):
            estimate_call.update(kwargs)
            return ball_flight.CameraBallEstimate(
                status="accepted",
                confidence_tier="high",
                horizontal_deg=0.6,
                support=20,
            )

        np.savez(
            tmp_path / "frames.npz",
            frames=np.zeros((8, 4, 4), dtype=np.uint8),
            host_timestamp_ns=np.arange(8, dtype=np.int64),
            trigger_host_timestamp_ns=np.int64(3),
        )
        monkeypatch.setattr(
            ball_flight,
            "estimate_camera_ball_flight",
            fake_estimate,
        )
        monkeypatch.setattr(
            server_module,
            "iwr6843_runtime",
            SimpleNamespace(
                calibration=SimpleNamespace(
                    tee_range_m=1.524,
                    radar_height_m=0.15875,
                    tee_ball_height_m=0.04,
                )
            ),
        )
        monkeypatch.setattr(
            server_module,
            "camera_capture_config",
            {
                "mount_height_m": 0.20955,
                "lateral_offset_m": 0.0762,
                "horizontal_offset_deg": -0.45,
                "roll_correction_deg": 2.8,
                "mirror_horizontal": True,
                "width": 640,
                "height": 400,
            },
        )
        ball_flight_tracker = object()
        monkeypatch.setattr(
            server_module,
            "camera_ball_flight_reference_tracker",
            ball_flight_tracker,
        )
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_horizontal=17.9,
            launch_angle_horizontal_confidence=0.8,
            launch_angle_horizontal_source="radar",
            iwr6843_horizontal_deg=17.9,
            iwr6843_horizontal_confidence=0.8,
            iwr6843_ball_range_evidence=object(),
        )
        capture = SimpleNamespace(valid=True, path=tmp_path)

        server_module._fuse_camera_ball_flight(shot, capture)

        assert shot.launch_angle_horizontal == 0.6
        assert shot.launch_angle_horizontal_source == "camera_assisted_experimental"
        assert shot.experimental_camera_horizontal_status == "camera_assisted_high"
        assert shot.iwr6843_horizontal_deg == 17.9
        assert estimate_call["geometry"].horizontal_offset_deg == -0.45
        assert estimate_call["geometry"].camera_lateral_offset_m == 0.0762
        assert estimate_call["geometry"].roll_correction_deg == 2.8
        assert estimate_call["geometry"].horizontal_pixel_sign == -1.0
        assert estimate_call["ball_tracker"] is ball_flight_tracker

    def test_live_fusion_without_camera_preserves_radar_horizontal(self):
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_horizontal=-2.0,
            launch_angle_horizontal_confidence=0.7,
            launch_angle_horizontal_source="radar",
            iwr6843_horizontal_deg=-2.0,
            iwr6843_horizontal_confidence=0.7,
        )

        server_module._fuse_camera_ball_flight(shot, None)

        assert shot.launch_angle_horizontal == -2.0
        assert shot.launch_angle_horizontal_confidence == 0.7
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.experimental_camera_horizontal_status == (
            "camera_withheld_fallback_iwr:rejected_no_camera_capture"
        )

    @pytest.mark.parametrize("angle", [-25.0, 25.0])
    def test_rejected_iwr_fallback_clears_the_previously_selected_direction(self, angle):
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_vertical=15.0,
            launch_angle_horizontal=angle,
            launch_angle_horizontal_confidence=0.9,
            launch_angle_horizontal_source="radar",
            iwr6843_horizontal_deg=angle,
            iwr6843_horizontal_confidence=0.9,
        )

        server_module._fuse_camera_ball_flight(shot, None)

        assert shot.experimental_camera_horizontal_status.startswith(
            "camera_withheld_iwr_implausible:"
        )
        assert shot.launch_angle_horizontal is None
        assert shot.launch_angle_horizontal_confidence is None
        assert shot.launch_angle_horizontal_source is None
        assert shot.iwr6843_horizontal_deg == angle
        assert shot.launch_angle_vertical == 15.0
        assert shot_to_dict(shot)["launch_angle_horizontal"] is None

    def test_live_camera_fusion_loads_capture_archive_once(self, monkeypatch, tmp_path):
        """Horizontal and club delivery should share one NPZ decode per shot."""
        np.savez(
            tmp_path / "frames.npz",
            frames=np.zeros((8, 4, 4), dtype=np.uint8),
            host_timestamp_ns=np.arange(8, dtype=np.int64),
            trigger_host_timestamp_ns=np.int64(3),
            pre_trigger_count=np.int32(4),
        )
        capture = SimpleNamespace(valid=True, path=tmp_path)
        real_load = np.load
        loads = []

        def counted_load(*args, **kwargs):
            loads.append(args[0])
            return real_load(*args, **kwargs)

        monkeypatch.setattr(np, "load", counted_load)
        fused_archives = []
        monkeypatch.setattr(
            server_module,
            "_fuse_camera_ball_flight",
            lambda _shot, _capture, archive, **_kw: fused_archives.append(archive),
        )
        monkeypatch.setattr(
            server_module,
            "_fuse_camera_club_delivery",
            lambda _shot, _capture, archive, **_kw: fused_archives.append(archive),
        )
        shot = Shot(ball_speed_mph=100.0, timestamp=datetime.now())

        server_module._fuse_camera_measurements(shot, capture)

        assert loads == [tmp_path / "frames.npz"]
        assert len(fused_archives) == 2
        assert fused_archives[0] is fused_archives[1]
        assert fused_archives[0]["frames"].shape == (8, 4, 4)

    def test_live_camera_fusion_withholds_dark_frames_and_preserves_iwr(self, monkeypatch):
        runtime = SimpleNamespace(camera_analysis_eligible=False)
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(
            server_module,
            "_load_camera_capture_archive",
            lambda _capture: pytest.fail("dark camera frames should not be decoded"),
        )
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_horizontal=-1.8,
            launch_angle_horizontal_confidence=0.8,
            launch_angle_horizontal_source="radar",
            iwr6843_horizontal_deg=-1.8,
            iwr6843_horizontal_confidence=0.8,
        )

        server_module._fuse_camera_measurements(shot, SimpleNamespace(valid=True))

        assert shot.launch_angle_horizontal == -1.8
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.experimental_camera_horizontal_status == "rejected_lighting_quality"
        assert shot.experimental_fused_status == "rejected_lighting_quality"
        assert shot.experimental_fused_attack_angle_deg is None
        assert shot.experimental_fused_club_path_deg is None

    def test_camera_fusion_uses_capture_time_exposure_state(self, monkeypatch):
        runtime = SimpleNamespace(camera_analysis_eligible=True)
        monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
        monkeypatch.setattr(
            server_module,
            "_load_camera_capture_archive",
            lambda _capture: pytest.fail("ineligible capture should not be decoded"),
        )
        shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now())
        capture = SimpleNamespace(
            valid=True,
            metadata={"auto_exposure": {"analysis_eligible": False}},
        )

        server_module._fuse_camera_measurements(shot, capture)

        assert shot.experimental_fused_status == "rejected_lighting_quality"

    def test_angle_source_none_by_default(self):
        """Shot without angle source should have None."""
        shot = Shot(
            ball_speed_mph=150.0,
            timestamp=datetime.now(),
        )
        result = shot_to_dict(shot)
        assert result["angle_source"] is None
        assert result["launch_angle_vertical_source"] is None
        assert result["launch_angle_horizontal_source"] is None

    CHAINED_DELIVERY = {
        "experimental_fused_attack_angle_deg": -4.2,
        "experimental_fused_attack_angle_confidence": "medium",
        "experimental_fused_club_path_deg": -19.7,
        "experimental_fused_club_path_confidence": "high",
        "experimental_fused_status": "approach_mixed",
    }

    def test_chained_camera_club_delivery_is_log_only(self):
        """The Lucas-Kanade estimator reads about -20 deg under backlight: log, never display."""
        shot = Shot(ball_speed_mph=105.0, timestamp=datetime.now(), **self.CHAINED_DELIVERY)

        result = shot_to_dict(shot)

        for name in self.CHAINED_DELIVERY:
            assert result[name] is None, name
        # The session log keeps every value.
        assert {name: shot.to_dict()[name] for name in self.CHAINED_DELIVERY} == (
            self.CHAINED_DELIVERY
        )

    @pytest.mark.parametrize(
        "path_status,attack_status,shown_path_status,shown_attack_status",
        [
            ("rejected_azimuth_fit", "candidate_out_of_bounds", "rejected_azimuth_fit", None),
            ("candidate_noisy_fit", "candidate_available", None, None),
            ("pending", "rejected_no_impact_frame", None, "rejected_no_impact_frame"),
            (None, None, None, None),
        ],
    )
    def test_unaccepted_ti_club_candidates_are_not_display_values(
        self, path_status, attack_status, shown_path_status, shown_attack_status
    ):
        shot = Shot(
            ball_speed_mph=105.0,
            timestamp=datetime.now(),
            experimental_club_path_deg=30.0,
            experimental_club_path_status=path_status,
            experimental_attack_angle_deg=-33.9,
            experimental_attack_angle_status=attack_status,
        )

        result = shot_to_dict(shot)

        assert result["experimental_club_path_deg"] is None
        assert result["experimental_attack_angle_deg"] is None
        # A rejection keeps its status-only tile; a candidate status under a withheld
        # value would caption the empty tile as if a value existed.
        assert result["experimental_club_path_status"] == shown_path_status
        assert result["experimental_attack_angle_status"] == shown_attack_status
        logged = shot.to_dict()
        assert logged["experimental_club_path_deg"] == 30.0
        assert logged["experimental_attack_angle_deg"] == -33.9
        assert logged["experimental_club_path_status"] == path_status
        assert logged["experimental_attack_angle_status"] == attack_status

    def test_accepted_ti_club_path_stays_a_display_value(self):
        shot = Shot(
            ball_speed_mph=105.0,
            timestamp=datetime.now(),
            experimental_club_path_deg=2.4,
            experimental_club_path_status="accepted",
            experimental_attack_angle_deg=-4.1,
            experimental_attack_angle_status="candidate_available",
            **self.CHAINED_DELIVERY,
        )

        result = shot_to_dict(shot)

        assert result["experimental_club_path_deg"] == 2.4
        assert result["experimental_club_path_status"] == "accepted"
        assert result["experimental_attack_angle_deg"] is None
        assert result["experimental_attack_angle_status"] is None
        assert result["experimental_fused_status"] is None

    def test_shot_to_dict_leaves_the_shot_alone(self):
        shot = Shot(
            ball_speed_mph=105.0,
            timestamp=datetime.now(),
            experimental_club_path_deg=30.0,
            experimental_club_path_status="rejected_azimuth_fit",
            **self.CHAINED_DELIVERY,
        )
        before = shot.to_dict()

        shot_to_dict(shot)

        assert shot.to_dict() == before

    def test_spin_diagnostics_included(self):
        """Rejected spin diagnostics should be present in UI payloads."""
        shot = Shot(
            ball_speed_mph=120.0,
            timestamp=datetime.now(),
            spin_snr=2.96,
            spin_peak_freq_hz=95.21484375,
            spin_candidates=[{"rank": 1, "rpm": 5713, "selected": True}],
            spin_phase_method="phase_residual",
            spin_phase_rpm=5713,
            spin_phase_snr=3.2,
            spin_phase_agreement_pct=2.1,
            spin_phase_confirmed=True,
            spin_rejection_reason="SNR too low (2.96, need 3.0)",
        )

        result = shot_to_dict(shot)

        assert result["spin_rpm"] is None
        assert result["spin_snr"] == 2.96
        assert result["spin_candidate_rpm"] == 5713
        assert result["spin_candidates"][0]["rpm"] == 5713
        assert result["spin_phase_method"] == "phase_residual"
        assert result["spin_phase_rpm"] == 5713
        assert result["spin_phase_snr"] == 3.2
        assert result["spin_phase_agreement_pct"] == 2.1
        assert result["spin_phase_confirmed"] is True
        assert result["spin_rejection_reason"] == "SNR too low (2.96, need 3.0)"


class TestSessionStateClub:
    """Connect snapshots must include the active club so a UI reload can restore it."""

    @staticmethod
    def _connect_session_state(monkeypatch, monitor):
        emitted = []
        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(server_module, "mock_mode", True)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "power_monitor", None)
        monkeypatch.setattr(server_module, "_emit_sim_snapshot", lambda: None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: emitted.append(args)
        )
        with server_module.app.test_request_context(environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            server_module.handle_connect()
        return next(data for name, data in emitted if name == "session_state")

    def test_connect_session_state_includes_current_club(self, monkeypatch):
        """Reload/dismiss keeps the server club, not a reset to driver."""
        monitor = MockLaunchMonitor()
        monitor.set_club(ClubType.IRON_7)

        payload = self._connect_session_state(monkeypatch, monitor)

        assert payload["club"] == "7-iron"

    def test_connect_session_state_defaults_to_driver(self, monkeypatch):
        """A fresh monitor with no set_club still reports driver."""
        payload = self._connect_session_state(monkeypatch, MockLaunchMonitor())

        assert payload["club"] == "driver"


class TestSwingSpeedMode:
    """Tests for swing speed training server helpers."""

    def test_swing_speed_to_dict(self):
        """Swing speed event payloads should be rounded and UI-friendly."""
        event = SwingSpeedEvent(
            peak_speed_mph=101.44,
            timestamp=datetime(2024, 1, 15, 10, 30, 0),
            duration_ms=347.8,
            reading_count=9,
            trigger_speed_mph=32.25,
            peak_magnitude=42,
        )

        result = swing_speed_to_dict(event)

        assert result == {
            "peak_speed_mph": 101.4,
            "timestamp": "2024-01-15T10:30:00",
            "duration_ms": 348,
            "reading_count": 9,
            "trigger_speed_mph": 32.2,
            "peak_magnitude": 42,
            "training_implement": "driver",
            "training_implement_label": "Driver",
            "profile_id": "",
            "profile_name": "",
            "unit": "mph",
            "mode": "swing-speed",
        }

    def test_swing_speed_to_shot_dict_supports_existing_ui(self):
        """Swing speed events should also map to the normal shot event shape."""
        event = SwingSpeedEvent(
            peak_speed_mph=101.44,
            timestamp=datetime(2024, 1, 15, 10, 30, 0),
            duration_ms=347.8,
            reading_count=9,
            trigger_speed_mph=32.25,
            peak_magnitude=42,
        )

        result = swing_speed_to_shot_dict(event)

        assert result["ball_speed_mph"] == 101.4
        assert result["club_speed_mph"] == 101.4
        assert result["club"] == "Driver"
        assert result["estimated_carry_yards"] == 0
        assert result["carry_range"] == [0, 0]
        assert result["mode"] == "swing-speed"
        assert result["swing_speed_reading_count"] == 9
        assert result["swing_speed_trigger_mph"] == 32.2
        assert result["training_implement"] == "driver"
        assert result["training_implement_label"] == "Driver"
        assert result["profile_name"] == ""

    def test_start_monitor_uses_swing_speed_monitor(self, monkeypatch):
        """Swing speed mode should start a club-only monitor and callback."""
        started = {}
        session = {}

        class FakeSwingSpeedMonitor:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.radar = SimpleNamespace(baud=57600)

            def connect(self):
                started["connected"] = True

            def get_radar_info(self):
                return {"Version": "test"}

            def start(self, event_callback=None, live_callback=None):
                started["event_callback"] = event_callback
                started["live_callback"] = live_callback

            def stop(self):
                started["stopped"] = True

            def disconnect(self):
                started["disconnected"] = True

        class FakeSessionLogger:
            def start_session(self, **kwargs):
                session.update(kwargs)

            def end_session(self):
                pass

            def log_connection(self, **kwargs):
                session["connection"] = kwargs

            def log_clock_sync(self, **kwargs):
                session["clock_sync"] = kwargs

        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: FakeSessionLogger())
        monkeypatch.setattr(
            "openflight.swing_speed.SwingSpeedMonitor",
            FakeSwingSpeedMonitor,
        )

        server_module.start_monitor(
            port="/dev/ops",
            swing_speed_mode=True,
            swing_speed_kwargs={
                "trigger_threshold_mph": 35.0,
                "max_speed_mph": 125.0,
                "min_readings": 4,
                "single_reading_peak_mph": 65.0,
                "num_reports": 8,
                "rejected_cooldown_ms": 50.0,
            },
        )

        assert server_module.monitor.kwargs == {
            "port": "/dev/ops",
            "trigger_threshold_mph": 35.0,
            "max_speed_mph": 125.0,
            "min_readings": 4,
            "single_reading_peak_mph": 65.0,
            "num_reports": 8,
            "rejected_cooldown_ms": 50.0,
        }
        assert started["connected"] is True
        assert started["event_callback"] is server_module.on_swing_speed_detected
        assert started["live_callback"] is server_module.on_live_reading
        assert session["mode"] == "swing-speed"
        assert session["trigger_type"] is None

        server_module.stop_monitor()

    def test_start_monitor_uses_mock_swing_speed_monitor(self, monkeypatch):
        """Mock swing speed mode should exercise the swing speed UI without hardware."""
        session = {}

        class FakeSessionLogger:
            def start_session(self, **kwargs):
                session.update(kwargs)

            def end_session(self):
                pass

        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: FakeSessionLogger())

        server_module.start_monitor(
            mock=True,
            swing_speed_mode=True,
            swing_speed_kwargs={
                "trigger_threshold_mph": 70.0,
                "max_speed_mph": 125.0,
                "min_readings": 5,
            },
        )

        assert isinstance(server_module.monitor, MockSwingSpeedMonitor)
        assert server_module.mock_mode is True
        assert server_module.mock_swing_speed_mode is True
        assert server_module.monitor.trigger_threshold_mph == 70.0
        assert server_module.monitor.max_speed_mph == 125.0
        assert server_module.monitor.min_readings == 5
        assert session["mode"] == "swing-speed"
        assert session["trigger_type"] is None

        server_module.stop_monitor()

    def test_mock_swing_speed_simulates_bounded_event(self):
        """Mock swing speed reps should respect configured lower and upper gates."""
        emitted = []
        monitor = MockSwingSpeedMonitor(
            trigger_threshold_mph=75.0,
            max_speed_mph=110.0,
            min_readings=5,
        )

        monitor.start(event_callback=emitted.append)
        event = monitor.simulate_shot()

        assert emitted == [event]
        assert 75.0 <= event.peak_speed_mph <= 110.0
        assert event.reading_count >= 5
        assert monitor.get_session_stats()["shot_count"] == 1

    def test_mock_swing_speed_stamps_training_implement(self):
        """Mock reps should use the selected training implement metadata."""
        monitor = MockSwingSpeedMonitor()

        assert (
            server_module.TRAINING_IMPLEMENT_LABELS["rypstick-3w-cw"]
            == "Rypstick 3 Weights + Counterweight"
        )

        monitor.set_training_implement("rypstick-3w-cw", "Rypstick 3 Weights + Counterweight")
        event = monitor.simulate_shot(peak_speed=95.0)
        shot = swing_speed_to_shot_dict(event)

        assert event.training_implement == "rypstick-3w-cw"
        assert event.training_implement_label == "Rypstick 3 Weights + Counterweight"
        assert shot["club"] == "Rypstick 3 Weights + Counterweight"

    def test_delete_session_row_removes_mock_swing_speed_event(self, monkeypatch):
        """Deleting a swing-speed UI row should remove the matching event."""
        monitor = MockSwingSpeedMonitor(
            trigger_threshold_mph=75.0,
            max_speed_mph=110.0,
            min_readings=5,
        )
        first = monitor.simulate_shot(peak_speed=95.0)
        second = monitor.simulate_shot(peak_speed=101.0)

        monkeypatch.setattr(server_module, "monitor", monitor)

        assert server_module._delete_session_row(first.timestamp.isoformat()) is True
        remaining = server_module._session_shots()

        assert len(remaining) == 1
        assert remaining[0]["timestamp"] == second.timestamp.isoformat()
        assert remaining[0]["club"] == "Driver"

    def test_set_radar_config_updates_swing_speed_gates(self, monkeypatch):
        """UI tuning should update live swing-speed lower and upper gates."""
        calls = []
        emitted = []

        class StubRadar:
            def set_min_speed_filter(self, value):
                calls.append(("min", value))

            def set_max_speed_filter(self, value):
                calls.append(("max", value))

        class StubSwingSpeedMonitor:
            radar = StubRadar()
            trigger_threshold_mph = 70.0
            max_speed_mph = 125.0

        monkeypatch.setattr(server_module, "monitor", StubSwingSpeedMonitor())
        monkeypatch.setattr(server_module, "mock_mode", False)
        monkeypatch.setattr(server_module, "radar_config", {"min_speed": 70, "max_speed": 125})
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "openflight.swing_speed.SwingSpeedMonitor",
            StubSwingSpeedMonitor,
        )

        server_module.handle_set_radar_config({"min_speed": 55, "max_speed": 115})

        assert calls == [("min", 55), ("max", 115)]
        assert server_module.monitor.trigger_threshold_mph == 55.0
        assert server_module.monitor.max_speed_mph == 115.0
        assert emitted[-1] == ("radar_config", {"min_speed": 55, "max_speed": 115})

    def test_set_radar_config_forwards_zero_max_speed_to_clear_the_filter(self, monkeypatch):
        """max_speed 0 must still reach the radar on the default launch path.

        AN-010-AD (p10) defines "R<0 resets to no limit", so 0 is how the UI
        clears a previously-set ceiling -- the default DebugPanel slider allows
        it (min=0). Skipping the command leaves the old ceiling active on the
        radar while radar_config reports 0, silently dropping fast shots.
        """
        calls = []

        class StubRadar:
            def set_min_speed_filter(self, value):
                calls.append(("min", value))

            def set_max_speed_filter(self, value):
                calls.append(("max", value))

        class StubMonitor:
            radar = StubRadar()

        monkeypatch.setattr(server_module, "monitor", StubMonitor())
        monkeypatch.setattr(server_module, "mock_mode", False)
        monkeypatch.setattr(server_module, "mock_swing_speed_mode", False)
        monkeypatch.setattr(server_module, "radar_config", {"min_speed": 10, "max_speed": 150})
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *_a, **_kw: None)
        monkeypatch.setattr(server_module, "log_session_error", lambda *_a, **_kw: None)

        server_module.handle_set_radar_config({"max_speed": 0})

        assert ("max", 0) in calls, (
            "R<0 is the documented reset-to-no-limit; suppressing it leaves the "
            f"previous ceiling active on the radar. Calls: {calls}"
        )
        assert server_module.radar_config["max_speed"] == 0


class TestProfileStamping:
    """Shot and swing-speed payloads carry profile id plus a name snapshot."""

    def test_shot_to_dict_emits_profile_fields(self):
        shot = Shot(
            ball_speed_mph=140.0,
            club_speed_mph=100.0,
            club=ClubType.DRIVER,
            timestamp=datetime(2026, 8, 27, 10, 0, 0),
        )
        shot.profile_id = "abc123"
        shot.profile_name = "Home Range"

        payload = shot_to_dict(shot)

        assert payload["profile_id"] == "abc123"
        assert payload["profile_name"] == "Home Range"
        assert "player_name" not in payload

    def test_unstamped_shot_has_empty_profile_fields(self):
        shot = Shot(
            ball_speed_mph=140.0,
            club_speed_mph=100.0,
            club=ClubType.DRIVER,
            timestamp=datetime(2026, 8, 27, 10, 0, 0),
        )

        assert shot.profile_id == ""
        assert shot.profile_name == ""

    def test_swing_speed_dicts_emit_profile_fields(self):
        event = SwingSpeedEvent(
            peak_speed_mph=101.4,
            timestamp=datetime(2026, 8, 27, 10, 0, 0),
            duration_ms=347.8,
            reading_count=9,
            trigger_speed_mph=32.2,
        )
        event.profile_id = "abc123"
        event.profile_name = "Home Range"

        event_payload = swing_speed_to_dict(event)
        shot_payload = swing_speed_to_shot_dict(event)

        assert event_payload["profile_id"] == "abc123"
        assert event_payload["profile_name"] == "Home Range"
        assert shot_payload["profile_id"] == "abc123"
        assert shot_payload["profile_name"] == "Home Range"
        assert "player_name" not in event_payload
        assert "player_name" not in shot_payload


class TestEstimateLaunchAngle:
    """Tests for launch angle estimation from club type and ball speed."""

    def test_driver_average_speed(self):
        """Driver at average speed should return baseline launch angle."""
        angle, confidence = estimate_launch_angle(ClubType.DRIVER, 143)
        assert angle == 11.0
        assert confidence == 0.2

    def test_driver_fast_lowers_launch(self):
        """Faster than average ball speed should produce lower launch."""
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 160)
        assert angle < 11.0

    def test_driver_slow_raises_launch(self):
        """Slower than average ball speed should produce higher launch."""
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 120)
        assert angle > 11.0

    def test_wedge_high_launch(self):
        """Wedges should have high baseline launch angle."""
        angle, _ = estimate_launch_angle(ClubType.LW, 70)
        assert angle >= 30.0

    def test_floor_at_5_degrees(self):
        """Launch angle should never go below 5 degrees."""
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 300)
        assert angle >= 5.0

    def test_unknown_club(self):
        """Unknown club should still return a reasonable estimate."""
        angle, confidence = estimate_launch_angle(ClubType.UNKNOWN, 120)
        assert 5.0 <= angle <= 40.0
        assert confidence == 0.2

    def test_low_smash_lowers_launch(self):
        """Low smash factor (thin hit) should lower launch angle, clamped."""
        baseline, _ = estimate_launch_angle(ClubType.DRIVER, 143)
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=110)
        # smash = 143/110 = 1.30, well below optimal 1.48
        # Adjustment clamped to -3.0 degrees, so angle ≈ 11.0 - 3.0 = 8.0
        assert angle < baseline
        assert 7.0 <= angle <= 9.0

    def test_optimal_smash_no_change(self):
        """Optimal smash factor should not shift launch angle."""
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=96.6)
        # smash = 143/96.6 ≈ 1.48 (optimal for driver)
        assert angle == 11.0

    def test_smash_raises_confidence(self):
        """Providing club speed should raise confidence from 0.2 to 0.35."""
        _, conf = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=96.6)
        assert conf == 0.35

    def test_high_smash_raises_launch(self):
        """High smash factor should slightly raise launch angle."""
        baseline, _ = estimate_launch_angle(ClubType.DRIVER, 143)
        # smash = 143/90 ≈ 1.59, above optimal 1.48
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=90)
        assert angle > baseline
        assert angle <= baseline + 2.0  # capped at +2.0 degrees

    def test_iron_smash_adjustment(self):
        """Iron smash factor adjustment should lower angle for thin hit."""
        baseline, _ = estimate_launch_angle(ClubType.IRON_7, 100)
        # Low smash for 7-iron: smash = 100/80 = 1.25, below optimal ~1.34
        angle, _ = estimate_launch_angle(ClubType.IRON_7, 100, club_speed_mph=80)
        assert angle < baseline
        assert angle >= baseline - 3.0  # clamped

    def test_no_club_speed_unchanged(self):
        """Without club speed, behavior should be identical to current."""
        angle, conf = estimate_launch_angle(ClubType.DRIVER, 143)
        assert angle == 11.0
        assert conf == 0.2

    def test_zero_club_speed_ignored(self):
        """Zero club speed should be treated as no club speed."""
        angle, conf = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=0)
        assert angle == 11.0
        assert conf == 0.2

    def test_high_spin_raises_launch(self):
        """High spin should nudge launch angle up."""
        baseline, _ = estimate_launch_angle(ClubType.DRIVER, 143)
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 143, spin_rpm=4000)
        # 4000 rpm is above optimal ~2500 for driver at 143 mph
        assert angle > baseline

    def test_low_spin_lowers_launch(self):
        """Low spin should nudge launch angle down."""
        baseline, _ = estimate_launch_angle(ClubType.DRIVER, 143)
        angle, _ = estimate_launch_angle(ClubType.DRIVER, 143, spin_rpm=1000)
        assert angle < baseline

    def test_spin_with_smash_raises_confidence(self):
        """Providing both club speed and spin should raise confidence to 0.5."""
        _, conf = estimate_launch_angle(ClubType.DRIVER, 143, club_speed_mph=96.6, spin_rpm=2500)
        assert conf == 0.5

    def test_spin_alone_confidence(self):
        """Spin without club speed should raise confidence to 0.35."""
        _, conf = estimate_launch_angle(ClubType.DRIVER, 143, spin_rpm=2500)
        assert conf == 0.35


class TestMockLaunchMonitor:
    """Tests for MockLaunchMonitor."""

    def test_initial_state(self):
        """New mock monitor should have empty state."""
        monitor = MockLaunchMonitor()

        assert monitor._shots == []
        assert monitor._current_club == ClubType.DRIVER
        assert not monitor._running

    def test_connect_disconnect(self):
        """Connect and disconnect should work."""
        monitor = MockLaunchMonitor()

        assert monitor.connect() is True
        monitor.disconnect()
        assert not monitor._running

    def test_simulate_shot(self):
        """Simulating a shot should create a shot record."""
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()

        shot = monitor.simulate_shot(ball_speed=150.0)

        assert len(monitor._shots) == 1
        assert 140.0 <= shot.ball_speed_mph <= 160.0  # ±10 variance
        assert shot.club == ClubType.DRIVER
        assert shot.mode == "mock"
        assert shot.spin_rpm is not None and shot.spin_rpm >= 1000
        assert shot.launch_angle_vertical is not None and shot.launch_angle_vertical >= 5.0
        assert shot.launch_angle_horizontal is not None
        assert shot.launch_angle_confidence is not None

    def test_simulate_shot_with_callback(self):
        """Callback should be called when shot is simulated."""
        monitor = MockLaunchMonitor()
        received_shots = []

        def callback(shot):
            received_shots.append(shot)

        monitor.connect()
        monitor.start(shot_callback=callback)
        monitor.simulate_shot()

        assert len(received_shots) == 1

    def test_set_club(self):
        """Set club should affect future shots."""
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()

        monitor.set_club(ClubType.IRON_7)
        shot = monitor.simulate_shot()

        assert shot.club == ClubType.IRON_7

    def test_get_shots(self):
        """Get shots should return copy of shots list."""
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monitor.simulate_shot()
        monitor.simulate_shot()

        shots = monitor.get_shots()

        assert len(shots) == 2
        # Verify it's a copy
        shots.append(None)
        assert len(monitor._shots) == 2

    def test_session_stats_empty(self):
        """Empty session should return zero stats."""
        monitor = MockLaunchMonitor()

        stats = monitor.get_session_stats()

        assert stats["shot_count"] == 0
        assert stats["avg_ball_speed"] == 0

    def test_session_stats_with_shots(self):
        """Session stats should reflect shots taken."""
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monitor.simulate_shot(ball_speed=140.0)
        monitor.simulate_shot(ball_speed=150.0)
        monitor.simulate_shot(ball_speed=160.0)

        stats = monitor.get_session_stats()

        assert stats["shot_count"] == 3
        # Averages will vary due to ±10 variance, but should be in range
        assert 140 <= stats["avg_ball_speed"] <= 160
        assert stats["avg_club_speed"] is not None
        assert stats["avg_smash_factor"] is not None

    def test_clear_session(self):
        """Clear session should reset all shots."""
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monitor.simulate_shot()
        monitor.simulate_shot()

        monitor.clear_session()

        assert monitor._shots == []
        assert monitor.get_session_stats()["shot_count"] == 0


class TestProfileSocketHandlers:
    """Every mutation answers with the authoritative snapshot."""

    @pytest.fixture(name="store")
    def fixture_store(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(server_module, "profile_store", store)
        return store

    @pytest.fixture(name="emitted")
    def fixture_emitted(self, monkeypatch):
        captured = []
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: captured.append(args)
        )
        return captured

    @staticmethod
    def _last_snapshot(emitted):
        return next(payload for name, payload in reversed(emitted) if name == "profiles")

    def test_get_profiles_emits_snapshot(self, store, emitted):
        server_module.handle_get_profiles()

        snapshot = self._last_snapshot(emitted)
        assert snapshot["active_profile_id"] == store.get_active().id
        assert len(snapshot["profiles"]) == 1

    def test_add_profile_adds_and_broadcasts(self, store, emitted):
        server_module.handle_add_profile({"name": "Home Range"})

        snapshot = self._last_snapshot(emitted)
        assert [entry["name"] for entry in snapshot["profiles"]][-1] == "Home Range"
        assert snapshot["active_profile_id"] == store.list()[-1].id

    def test_add_profile_with_blank_name_broadcasts_unchanged_snapshot(self, store, emitted):
        server_module.handle_add_profile({"name": "   "})

        assert len(self._last_snapshot(emitted)["profiles"]) == 1

    def test_set_active_profile_switches(self, store, emitted):
        first = store.list()[0]
        store.add("Second")

        server_module.handle_set_active_profile({"profile_id": first.id})

        assert self._last_snapshot(emitted)["active_profile_id"] == first.id

    def test_set_active_profile_with_unknown_id_broadcasts_unchanged_snapshot(self, store, emitted):
        before = store.get_active().id

        server_module.handle_set_active_profile({"profile_id": "ghost"})

        assert self._last_snapshot(emitted)["active_profile_id"] == before

    def test_rename_profile_broadcasts_new_name(self, store, emitted):
        added = store.add("Rnage")

        server_module.handle_rename_profile({"profile_id": added.id, "name": "Range"})

        assert self._last_snapshot(emitted)["profiles"][-1]["name"] == "Range"

    def test_remove_profile_deletes_inactive(self, store, emitted):
        doomed = store.add("Doomed")
        store.add("Keeper")

        server_module.handle_remove_profile({"profile_id": doomed.id})

        names = [entry["name"] for entry in self._last_snapshot(emitted)["profiles"]]
        assert "Doomed" not in names

    def test_remove_profile_refuses_the_active_one(self, store, emitted):
        active = store.add("Active")

        server_module.handle_remove_profile({"profile_id": active.id})

        snapshot = self._last_snapshot(emitted)
        assert snapshot["active_profile_id"] == active.id
        assert len(snapshot["profiles"]) == 2

    def test_remove_profile_refuses_when_profile_has_shots(self, store, emitted, monkeypatch):
        doomed = store.add("Doomed")
        store.add("Keeper")
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        shot = monitor.simulate_shot()
        shot.profile_id = doomed.id
        monkeypatch.setattr(server_module, "monitor", monitor)

        server_module.handle_remove_profile({"profile_id": doomed.id})

        names = [entry["name"] for entry in self._last_snapshot(emitted)["profiles"]]
        assert "Doomed" in names
        assert [row.profile_id for row in monitor.get_shots()] == [doomed.id]

    def test_remove_profile_succeeds_after_session_rows_are_cleared(
        self, store, emitted, monkeypatch
    ):
        doomed = store.add("Doomed")
        store.add("Keeper")
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        shot = monitor.simulate_shot()
        shot.profile_id = doomed.id
        monkeypatch.setattr(server_module, "monitor", monitor)

        server_module.handle_clear_session({"profile_id": doomed.id})
        server_module.handle_remove_profile({"profile_id": doomed.id})

        names = [entry["name"] for entry in self._last_snapshot(emitted)["profiles"]]
        assert "Doomed" not in names
        assert monitor.get_shots() == []

    def test_remove_profile_refuses_when_profile_has_swing_speed_events(
        self, store, emitted, monkeypatch
    ):
        doomed = store.add("Doomed")
        store.add("Keeper")
        monitor = MockSwingSpeedMonitor()
        monitor.connect()
        monitor.start()
        event = SwingSpeedEvent(
            peak_speed_mph=100.0,
            timestamp=datetime(2026, 8, 27, 10, 0, 0),
            duration_ms=300.0,
            reading_count=8,
            trigger_speed_mph=32.0,
        )
        event.profile_id = doomed.id
        monitor._events[:] = [event]  # pylint: disable=protected-access
        monkeypatch.setattr(server_module, "monitor", monitor)

        server_module.handle_remove_profile({"profile_id": doomed.id})

        names = [entry["name"] for entry in self._last_snapshot(emitted)["profiles"]]
        assert "Doomed" in names
        assert [row.profile_id for row in monitor.get_events()] == [doomed.id]

    def test_handlers_tolerate_non_dict_payloads(self, store, emitted):
        server_module.handle_set_active_profile(None)
        server_module.handle_add_profile("not a dict")
        server_module.handle_rename_profile(None)
        server_module.handle_remove_profile(None)

        assert len(self._last_snapshot(emitted)["profiles"]) == 1

    def test_switching_active_profile_via_handler_is_seen_by_later_events(
        self, store, emitted, monkeypatch
    ):
        """A mutation handler must change what later events are stamped with.

        Regression guard for a gap a reviewer found: earlier coverage only
        called ``store.add()`` directly (which sets the new profile active as
        a side effect of the mutator) and never actually went through
        ``handle_set_active_profile``. This drives the real handler, then a
        real event callback, so it would catch either side caching a stale
        selection.
        """
        first = store.list()[0]
        second = store.add("Second")
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monkeypatch.setattr(server_module, "monitor", monitor)

        server_module.handle_set_active_profile({"profile_id": first.id})
        shot = monitor.simulate_shot(ball_speed=140.0)
        on_shot_detected(shot)

        assert shot.profile_id == first.id
        assert shot.profile_id != second.id


class TestShotProfileStamping:
    """Shots take their attribution from the active profile."""

    def test_shot_is_stamped_with_active_profile(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        active = store.add("Home Range")
        monkeypatch.setattr(server_module, "profile_store", store)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monkeypatch.setattr(server_module, "monitor", monitor)
        shot = monitor.simulate_shot(ball_speed=140.0)

        on_shot_detected(shot)

        assert shot.profile_id == active.id
        assert shot.profile_name == "Home Range"

    def test_swing_speed_event_is_stamped_with_active_profile(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        active = store.add("David")
        monkeypatch.setattr(server_module, "profile_store", store)
        emitted = []
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: emitted.append(args)
        )

        event = SwingSpeedEvent(
            peak_speed_mph=101.44,
            timestamp=datetime(2026, 8, 27, 10, 30, 0),
            duration_ms=347.8,
            reading_count=9,
            trigger_speed_mph=32.25,
        )
        server_module.on_swing_speed_detected(event)

        shot_payload = next(payload for name, payload in emitted if name == "shot")
        assert shot_payload["shot"]["profile_id"] == active.id
        assert shot_payload["shot"]["profile_name"] == "David"


class TestHandleClearSession:
    """Clear session removes only the active profile's rows, matched by id."""

    @pytest.fixture(name="store")
    def fixture_store(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(server_module, "profile_store", store)
        return store

    def test_removes_only_that_profiles_shots(self, store, monkeypatch):
        james = store.add("James")
        alex = store.add("Alex")
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        james_shot = monitor.simulate_shot(ball_speed=140.0)
        james_shot.profile_id = james.id
        james_shot.profile_name = "James"
        alex_shot = monitor.simulate_shot(ball_speed=150.0)
        alex_shot.profile_id = alex.id
        alex_shot.profile_name = "Alex"

        emitted = []
        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: emitted.append(args)
        )

        server_module.handle_clear_session({"profile_id": james.id})

        assert [shot.profile_name for shot in monitor.get_shots()] == ["Alex"]
        _event, payload = next(args for args in emitted if args[0] == "session_cleared")
        assert payload["profile_id"] == james.id
        assert [entry["profile_name"] for entry in payload["shots"]] == ["Alex"]

    def test_uses_active_profile_when_payload_omits_id(self, store, monkeypatch):
        alex = store.add("Alex")
        james = store.add("James")
        store.set_active(alex.id)
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        first = monitor.simulate_shot()
        first.profile_id = alex.id
        second = monitor.simulate_shot()
        second.profile_id = james.id

        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        server_module.handle_clear_session()

        assert [shot.profile_id for shot in monitor.get_shots()] == [james.id]

    def test_profiles_with_names_differing_only_in_case_do_not_collide(self, store, monkeypatch):
        """The old name-keyed code folded case and cleared both. Ids must not."""
        lower = store.add("james")
        upper = store.add("James")
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        lower_shot = monitor.simulate_shot()
        lower_shot.profile_id = lower.id
        lower_shot.profile_name = "james"
        upper_shot = monitor.simulate_shot()
        upper_shot.profile_id = upper.id
        upper_shot.profile_name = "James"

        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        server_module.handle_clear_session({"profile_id": lower.id})

        assert [shot.profile_name for shot in monitor.get_shots()] == ["James"]

    def test_unstamped_shots_belong_to_no_profile(self, store, monkeypatch):
        active = store.get_active()
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monitor.simulate_shot()

        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        server_module.handle_clear_session({"profile_id": active.id})

        assert len(monitor.get_shots()) == 1

    def test_emits_cleared_payload_without_monitor(self, store, monkeypatch):
        """UI still gets an ack so the confirm dialog can close."""
        active = store.get_active()
        emitted = []
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: emitted.append(args)
        )

        server_module.handle_clear_session({"profile_id": active.id})

        _event, payload = next(args for args in emitted if args[0] == "session_cleared")
        assert payload["profile_id"] == active.id

    def test_clears_only_that_profiles_swing_speed_events(self, store, monkeypatch):
        james = store.add("James")
        alex = store.add("Alex")
        monitor = MockSwingSpeedMonitor()
        monitor.connect()
        monitor.start()
        first = SwingSpeedEvent(
            peak_speed_mph=100.0,
            timestamp=datetime(2026, 8, 27, 10, 0, 0),
            duration_ms=300.0,
            reading_count=8,
            trigger_speed_mph=32.0,
        )
        first.profile_id = james.id
        second = SwingSpeedEvent(
            peak_speed_mph=105.0,
            timestamp=datetime(2026, 8, 27, 10, 1, 0),
            duration_ms=310.0,
            reading_count=8,
            trigger_speed_mph=32.0,
        )
        second.profile_id = alex.id
        monitor._events[:] = [first, second]  # pylint: disable=protected-access

        monkeypatch.setattr(server_module, "monitor", monitor)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        server_module.handle_clear_session({"profile_id": james.id})

        assert [
            event.profile_id
            for event in monitor._events  # pylint: disable=protected-access
        ] == [alex.id]


class TestSessionStatePayload:
    """session_state no longer carries a selection, so it cannot race."""

    def test_payload_has_no_selection_field(self, monkeypatch):
        monitor = MockLaunchMonitor()
        monitor.connect()
        monitor.start()
        monkeypatch.setattr(server_module, "monitor", monitor)

        payload = server_module._session_state_payload()  # pylint: disable=protected-access

        assert "player_name" not in payload
        assert "profile_id" not in payload
        assert "active_profile_id" not in payload

    @pytest.mark.parametrize("include_runtime_meta", [False, True])
    @pytest.mark.parametrize(
        "iwr_config,calibrated",
        [
            ({"enabled": False}, False),
            ({"enabled": False, "error": "no radar"}, False),
            ({"enabled": True, "horizontal_phase_reference_rad": None}, False),
            ({"enabled": True, "horizontal_phase_reference_rad": 0.0}, True),
            ({"enabled": True, "horizontal_phase_reference_rad": -1.2}, True),
        ],
    )
    def test_payload_reports_horizontal_aim_calibration(
        self, monkeypatch, include_runtime_meta, iwr_config, calibrated
    ):
        """Phones label lateral dispersion as unverified until aim is calibrated."""
        monkeypatch.setattr(server_module, "monitor", MockLaunchMonitor())
        monkeypatch.setattr(server_module, "iwr6843_runtime_config", iwr_config)

        payload = server_module._session_state_payload(  # pylint: disable=protected-access
            include_runtime_meta=include_runtime_meta
        )

        assert payload["calibration"] == {"horizontal_phase_reference": calibrated}


class TestRadarLaunchGuard:
    """Tests for club-and-speed sanity checks on radar launch angles."""

    SESSION_LOG_PATH = (
        Path(__file__).parent.parent / "session_logs" / "session_20260402_121507_range.jsonl"
    )

    def test_rejects_implausible_7iron_launch(self):
        """An obviously impossible 7-iron launch angle should be rejected."""
        plausible, details = radar_launch_is_plausible(
            radar_angle_deg=79.4,
            club=ClubType.IRON_7,
            ball_speed_mph=100.0,
        )

        assert plausible is False
        assert details["expected_launch_deg"] == pytest.approx(20.5)
        assert details["delta_deg"] > details["allowed_delta_deg"]

    def test_accepts_plausible_driver_launch(self):
        """A realistic driver launch angle should pass the sanity guard."""
        plausible, details = radar_launch_is_plausible(
            radar_angle_deg=17.8,
            club=ClubType.DRIVER,
            ball_speed_mph=97.9,
            club_speed_mph=66.0,
        )

        assert plausible is True
        assert details["delta_deg"] < details["allowed_delta_deg"]

    def test_accepts_low_iron_launch(self):
        """Thin/low iron shots are real and should not be replaced by estimates."""
        plausible, details = radar_launch_is_plausible(
            radar_angle_deg=6.9,
            club=ClubType.IRON_9,
            ball_speed_mph=54.8,
        )

        assert plausible is True
        assert details["delta_deg"] > details["allowed_delta_deg"]

    def test_flags_known_outliers_in_real_session_log(self):
        """Historic backyard session log should surface the same three driver outliers."""
        if not self.SESSION_LOG_PATH.exists():
            pytest.skip(f"Session log not found: {self.SESSION_LOG_PATH}")

        implausible_shots = []
        total_shots = 0

        with self.SESSION_LOG_PATH.open() as f:
            for line in f:
                entry = json.loads(line)
                if entry.get("type") != "shot_detected":
                    continue

                total_shots += 1
                plausible, _ = radar_launch_is_plausible(
                    radar_angle_deg=entry["launch_angle_vertical"],
                    club=ClubType(entry["club"]),
                    ball_speed_mph=entry["ball_speed_mph"],
                    club_speed_mph=entry.get("club_speed_mph"),
                    spin_rpm=entry.get("spin_rpm"),
                )
                if not plausible:
                    implausible_shots.append(entry["shot_number"])

        assert total_shots == 11
        assert implausible_shots == [3, 9, 11]


class TestKLD7BufferUnderfillWarning:
    """The buffer-underfill warning surfaces stream-rate problems in
    production logs without requiring a replay.
    """

    def test_full_buffer_does_not_warn(self, caplog):
        import logging

        from openflight.server import _warn_if_kld7_buffer_underfilled

        with caplog.at_level(logging.WARNING, logger="openflight.server"):
            # Expected ~204; full buffer should not warn.
            _warn_if_kld7_buffer_underfilled("vertical", 200)
        warns = [r for r in caplog.records if "underfilled" in r.message]
        assert not warns

    def test_underfilled_buffer_warns(self, caplog):
        import logging

        from openflight.server import _warn_if_kld7_buffer_underfilled

        with caplog.at_level(logging.WARNING, logger="openflight.server"):
            _warn_if_kld7_buffer_underfilled("vertical", 50)  # ~25%
        warns = [r for r in caplog.records if "underfilled" in r.message]
        assert warns, "Expected underfill WARNING but got none"
        assert "vertical" in warns[0].message
        assert "50/204" in warns[0].message or "50/" in warns[0].message

    def test_empty_buffer_does_not_warn(self, caplog):
        # frame_count=0 means snapshot wasn't taken or stream hadn't
        # started; not the underfill case we care about.
        import logging

        from openflight.server import _warn_if_kld7_buffer_underfilled

        with caplog.at_level(logging.WARNING, logger="openflight.server"):
            _warn_if_kld7_buffer_underfilled("horizontal", 0)
        warns = [r for r in caplog.records if "underfilled" in r.message]
        assert not warns


class TestKLD7PostShotCaptureDelay:
    """Live K-LD7 extraction should include post-impact frames."""

    def test_waits_until_post_shot_capture_time(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(server_module.time, "time", lambda: 1000.0)
        monkeypatch.setattr(server_module.time, "sleep", lambda delay: sleeps.append(delay))

        server_module._maybe_wait_for_kld7_post_shot_frames(1000.0)

        assert sleeps == [pytest.approx(0.18)]

    def test_does_not_wait_when_processing_is_already_past_capture_time(self, monkeypatch):
        sleeps = []
        monkeypatch.setattr(server_module.time, "time", lambda: 1000.2)
        monkeypatch.setattr(server_module.time, "sleep", lambda delay: sleeps.append(delay))

        server_module._maybe_wait_for_kld7_post_shot_frames(1000.0)

        assert sleeps == []


class TestOnShotDetected:
    """Tests for live shot processing in the server."""

    @pytest.fixture(autouse=True)
    def _reset_ordered_pipeline(self):
        server_module._reset_shot_sequence()
        yield
        _wait_for_shot_finalization_idle()

    class RecordingSessionLog:
        def __init__(self):
            self.stats = {"shots_detected": 0}
            self.shots = []
            self.rolling_buffer_captures = []
            self.iwr6843_captures = []
            self.camera_captures = []
            self.all_shots_logged = threading.Event()

        def log_shot(self, shot, pipeline_ms=None, log_only_fields=None):
            self.stats["shots_detected"] += 1
            shot_data = shot.to_dict()
            if shot_data["shot_number"] is None:
                shot_data["shot_number"] = self.stats["shots_detected"]
            if pipeline_ms is not None:
                shot_data["pipeline_ms"] = pipeline_ms
            self.shots.append(shot_data)
            if len(self.shots) == 4:
                self.all_shots_logged.set()

        def log_iwr6843_capture(self, **capture_data):
            self.iwr6843_captures.append(capture_data)

        def log_rolling_buffer_capture(self, **capture_data):
            self.rolling_buffer_captures.append(capture_data)

        def log_camera_capture(self, **capture_data):
            self.camera_captures.append(capture_data)

    @staticmethod
    def _shot(second: int = 0) -> Shot:
        return Shot(
            ball_speed_mph=150.0 + second,
            club_speed_mph=100.0,
            timestamp=datetime(2026, 8, 26, 12, 0, second),
            impact_timestamp=100.0 + second,
            club=ClubType.DRIVER,
        )

    @staticmethod
    def _enrichment_queue():
        return server_module.queue.Queue(maxsize=server_module._SHOT_ENRICHMENT_QUEUE_CAPACITY)

    def _record_finalization(self, monkeypatch):
        emitted = []
        session_log = self.RecordingSessionLog()
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        monkeypatch.setattr(server_module, "ball_speed_correction_enabled", False)
        monkeypatch.setattr(server_module, "calculated_spin_enabled", False)
        monkeypatch.setattr(server_module, "ballistics_enabled", False)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "sim_connectors", [])
        monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )
        return emitted, session_log

    @staticmethod
    def _final_update_payloads(emitted):
        return [
            payload
            for event, payload in emitted
            if event == "shot_update" and payload.get("enrichment", {}).get("status") != "skipped"
        ]

    @classmethod
    def _assert_finalized_once(cls, emitted, session_log):
        updates = cls._final_update_payloads(emitted)
        assert len(updates) == 1
        assert updates[0]["shot"]["launch_angle_vertical_source"] == "estimated"
        assert updates[0]["shot"]["carry_spin_adjusted"] > 0
        assert len(session_log.shots) == 1
        assert session_log.shots[0]["launch_angle_vertical_source"] == "estimated"
        assert session_log.shots[0]["carry_spin_adjusted"] > 0

    @staticmethod
    def _assert_finalization_coordinator_empty():
        with server_module._shot_finalization_lock:
            assert not server_module._shot_finalization_order
            assert not server_module._shot_finalization_registered
            assert not server_module._shot_finalization_ready
            assert server_module._shot_finalization_running is False

    @staticmethod
    def _wait_for_finalization_coordinator_idle(timeout_s: float = 2.0):
        _wait_for_shot_finalization_idle(timeout_s)

    def test_enrichment_queue_has_hard_capacity(self):
        assert server_module._SHOT_ENRICHMENT_QUEUE_CAPACITY > 0
        assert (
            server_module.shot_enrichment_queue.maxsize
            == server_module._SHOT_ENRICHMENT_QUEUE_CAPACITY
        )

    def test_emits_ops_metrics_before_iwr6843_dump_finishes(self, monkeypatch):
        """The seven-second TI UART dump must not hold the first UI update."""
        dump_started = threading.Event()
        release_dump = threading.Event()
        emitted = []
        background_threads = []

        class BlockingRuntime:
            @staticmethod
            def process_shot(**_kwargs):
                dump_started.set()
                assert release_dump.wait(5.0)
                return SimpleNamespace(capture=None, measurement=None, club_path=None)

        def start_background_task(target, *args, **kwargs):
            thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            background_threads.append(thread)
            thread.start()
            return thread

        monkeypatch.setattr(server_module, "iwr6843_runtime", BlockingRuntime())
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "start_background_task", start_background_task)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        shot = self._shot()
        callback = threading.Thread(target=on_shot_detected, args=(shot,), daemon=True)
        callback.start()
        assert dump_started.wait(2.0)
        try:
            callback.join(timeout=1.0)
            assert not callback.is_alive()
            assert emitted[0][0] == "shot"
            assert emitted[0][1]["shot"]["ball_speed_mph"] == 150.0
            assert emitted[0][1]["pending"] == {"iwr6843": True}
            assert all(event != "shot_update" for event, _payload in emitted)
        finally:
            release_dump.set()
            callback.join(timeout=2.0)
            for thread in background_threads:
                thread.join(timeout=2.0)

        self._wait_for_finalization_coordinator_idle()

        assert [event for event, _payload in emitted].count("shot") == 1
        updates = [payload for event, payload in emitted if event == "shot_update"]
        assert len(updates) == 1
        assert updates[0]["shot"]["timestamp"] == shot.timestamp.isoformat()

    def test_deferred_shots_are_fifo_on_one_worker(self, monkeypatch):
        processed = []
        worker_targets = []
        worker_token = object()
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(
            server_module,
            "_finish_shot_detected",
            lambda shot, **_kwargs: processed.append(shot.timestamp),
        )

        def capture_worker(target, *_args, **_kwargs):
            worker_targets.append(target)
            return worker_token

        monkeypatch.setattr(server_module.socketio, "start_background_task", capture_worker)
        shots = [self._shot(1), self._shot(2)]

        for shot in shots:
            server_module._defer_shot_enrichment(
                shot,
                emit_event="shot_update",
                initial_ui_ms=10.0,
            )

        assert worker_targets == [server_module._drain_shot_enrichment_queue]
        assert server_module.shot_enrichment_task is worker_token

        worker_targets[0]()

        assert processed == [shot.timestamp for shot in shots]
        assert enrichment_queue.empty()
        assert server_module.shot_enrichment_task is None

    def test_successful_worker_finalizes_and_persists_once(self, monkeypatch):
        emitted, session_log = self._record_finalization(monkeypatch)
        worker_targets = []
        enrichment_calls = []
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", object())
        monkeypatch.setattr(
            server_module.socketio,
            "start_background_task",
            lambda target, *_args, **_kwargs: worker_targets.append(target) or object(),
        )
        monkeypatch.setattr(
            server_module,
            "_enrich_shot_from_optional_hardware",
            lambda shot: (
                enrichment_calls.append(shot.timestamp) or server_module._ShotEnrichmentResult()
            ),
        )

        shot = self._shot()
        on_shot_detected(shot)
        worker_targets[0]()
        self._wait_for_finalization_coordinator_idle()

        assert enrichment_calls == [shot.timestamp]
        self._assert_finalized_once(emitted, session_log)

    def test_camera_only_enrichment_uses_provisional_then_final_events(self, monkeypatch):
        emitted = []
        worker_targets = []
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", object())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(
            server_module.socketio,
            "emit",
            lambda event, payload: emitted.append((event, payload)),
        )

        def capture_worker(target, *_args, **_kwargs):
            worker_targets.append(target)
            return object()

        monkeypatch.setattr(server_module.socketio, "start_background_task", capture_worker)
        monkeypatch.setattr(
            server_module,
            "_enrich_shot_from_optional_hardware",
            lambda _shot: server_module._ShotEnrichmentResult(),
        )

        on_shot_detected(self._shot())
        worker_targets[0]()
        self._wait_for_finalization_coordinator_idle()

        assert [event for event, _payload in emitted] == ["shot", "shot_update"]
        assert emitted[0][1]["pending"] == {"camera": True}

    def test_worker_start_failure_keeps_ops_result_usable(self, monkeypatch):
        emitted, session_log = self._record_finalization(monkeypatch)
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", object())
        monkeypatch.setattr(
            server_module.socketio,
            "start_background_task",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no worker")),
        )

        on_shot_detected(self._shot())
        self._wait_for_finalization_coordinator_idle()

        assert [event for event, _payload in emitted] == [
            "shot",
            "shot_update",
            "shot_update",
        ]
        assert emitted[1][1]["pending"] == {}
        assert emitted[1][1]["enrichment"] == {
            "status": "skipped",
            "reason": "worker_unavailable",
            "hardware": ["camera"],
        }
        self._assert_finalized_once(emitted, session_log)
        assert enrichment_queue.empty()

    def test_worker_enrichment_failure_keeps_ops_result_usable(self, monkeypatch):
        emitted, session_log = self._record_finalization(monkeypatch)
        worker_targets = []
        enrichment_calls = []
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", object())
        monkeypatch.setattr(
            server_module.socketio,
            "start_background_task",
            lambda target, *_args, **_kwargs: worker_targets.append(target) or object(),
        )
        monkeypatch.setattr(
            server_module,
            "_enrich_shot_from_optional_hardware",
            lambda shot: (
                enrichment_calls.append(shot.timestamp)
                or (_ for _ in ()).throw(RuntimeError("hardware failed"))
            ),
        )

        shot = self._shot()
        on_shot_detected(shot)
        worker_targets[0]()
        self._wait_for_finalization_coordinator_idle()

        assert enrichment_calls == [shot.timestamp]
        assert [event for event, _payload in emitted] == ["shot", "shot_update"]
        self._assert_finalized_once(emitted, session_log)

    def test_ordered_finalizer_failure_does_not_strand_later_shot(self, monkeypatch):
        attempts = []
        logged_errors = []
        shots = [self._shot(0), self._shot(1)]
        for shot_number, shot in enumerate(shots, start=1):
            shot.shot_number = shot_number
            server_module._register_shot_for_finalization(shot)

        def finalize(shot, **_kwargs):
            attempts.append(shot.shot_number)
            if shot.shot_number == 1:
                raise RuntimeError("final publish failed")

        monkeypatch.setattr(server_module, "_finalize_shot_detected", finalize)
        monkeypatch.setattr(
            server_module,
            "log_session_error",
            lambda *args, **kwargs: logged_errors.append((args, kwargs)),
        )

        server_module._queue_shot_finalization(
            shots[1],
            emit_event="shot_update",
            initial_ui_ms=10.0,
        )
        assert attempts == []

        server_module._queue_shot_finalization(
            shots[0],
            emit_event="shot_update",
            initial_ui_ms=10.0,
        )
        self._wait_for_finalization_coordinator_idle()

        assert attempts == [1, 2]
        assert len(logged_errors) == 1
        self._assert_finalization_coordinator_empty()

    def test_queue_overflow_preserves_shot_order_and_hardware_identity(self, monkeypatch):
        emitted, session_log = self._record_finalization(monkeypatch)
        dump_started = threading.Event()
        release_dump = threading.Event()
        all_initial_shots_emitted = threading.Event()
        background_threads = []
        simulator_shots = []
        enrichment_queue = self._enrichment_queue()
        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "_shot_sequence_number", 0, raising=False)

        class BlockingIWRRuntime:
            @staticmethod
            def process_shot(**kwargs):
                if kwargs["impact_timestamp"] == 100.0:
                    dump_started.set()
                    assert release_dump.wait(5.0)
                return SimpleNamespace(capture=None, measurement=None, club_path=None)

        class CameraRuntime:
            camera_analysis_eligible = False

            @staticmethod
            def capture_for_shot(impact_timestamp, timeout_s):
                del timeout_s
                return SimpleNamespace(
                    trigger_timestamp=impact_timestamp,
                    path=None,
                    metadata={},
                    error="test capture",
                    valid=False,
                    sequence=int(impact_timestamp),
                )

            @staticmethod
            def note_accepted_shot(capture, shot_number=None):
                del capture, shot_number

        def start_background_task(target, *args, **kwargs):
            thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            background_threads.append(thread)
            thread.start()
            return thread

        monkeypatch.setattr(server_module, "iwr6843_runtime", BlockingIWRRuntime())
        monkeypatch.setattr(server_module, "camera_capture_runtime", CameraRuntime())
        monkeypatch.setattr(server_module.socketio, "start_background_task", start_background_task)

        def record_emit(event, payload):
            emitted.append((event, payload))
            if event == "shot" and sum(name == "shot" for name, _payload in emitted) == 4:
                all_initial_shots_emitted.set()

        monkeypatch.setattr(server_module.socketio, "emit", record_emit)
        monkeypatch.setattr(
            server_module,
            "_forward_shot_to_simulators",
            lambda shot: simulator_shots.append((shot.shot_number, shot.impact_timestamp)),
        )

        shots = [self._shot(second) for second in range(4)]
        for shot_number, shot in enumerate(shots, start=1):
            shot.shot_number = shot_number
            session_log.log_rolling_buffer_capture(
                shot_number=shot_number,
                shot_timestamp=shot.impact_timestamp,
            )
        on_shot_detected(shots[0])
        assert dump_started.wait(2.0)

        on_shot_detected(shots[1])
        on_shot_detected(shots[2])
        assert enrichment_queue.full()

        overflow_callback = threading.Thread(
            target=on_shot_detected,
            args=(shots[3],),
            daemon=True,
        )
        overflow_callback.start()
        assert all_initial_shots_emitted.wait(2.0)
        try:
            overflow_callback.join(timeout=1.0)
            assert not overflow_callback.is_alive(), (
                "OPS callback must not wait for optional hardware"
            )
            assert not session_log.shots
        finally:
            release_dump.set()

        assert session_log.all_shots_logged.wait(5.0)
        overflow_callback.join(timeout=2.0)
        for thread in background_threads:
            thread.join(timeout=2.0)
        assert not overflow_callback.is_alive()
        assert all(not thread.is_alive() for thread in background_threads)

        expected = [(index, shot.impact_timestamp) for index, shot in enumerate(shots, start=1)]
        initial_shots = [payload["shot"] for event, payload in emitted if event == "shot"]
        skipped_updates = [
            payload
            for event, payload in emitted
            if event == "shot_update" and payload.get("enrichment", {}).get("status") == "skipped"
        ]
        final_shots = [payload["shot"] for payload in self._final_update_payloads(emitted)]
        assert [(row["shot_number"], row["impact_timestamp"]) for row in initial_shots] == expected
        assert len(skipped_updates) == 1
        assert skipped_updates[0]["shot"]["shot_number"] == 4
        assert skipped_updates[0]["pending"] == {}
        assert skipped_updates[0]["enrichment"] == {
            "status": "skipped",
            "reason": "queue_full",
            "hardware": ["iwr6843", "camera"],
        }
        assert [
            (row["shot_number"], row["impact_timestamp"]) for row in session_log.shots
        ] == expected
        assert [
            (row["shot_number"], row["shot_timestamp"])
            for row in session_log.rolling_buffer_captures
        ] == expected
        expected_enriched = expected[:3]
        assert [
            (row["shot_number"], row["shot_timestamp"]) for row in session_log.iwr6843_captures
        ] == expected_enriched
        assert [
            (row["shot_number"], row["shot_timestamp"]) for row in session_log.camera_captures
        ] == expected_enriched
        assert [(row["shot_number"], row["impact_timestamp"]) for row in final_shots] == expected
        assert simulator_shots == expected
        self._assert_finalization_coordinator_empty()

    def test_capacity_eviction_never_finalizes_on_ops_callback(self, monkeypatch):
        enrichment_queue = self._enrichment_queue()
        finalization_started = threading.Event()
        finalization_finished = threading.Event()
        release_finalization = threading.Event()
        worker_token = object()

        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", object())
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "_SHOT_ENRICHMENT_DEADLINE_S", 30.0)
        monkeypatch.setattr(
            server_module.socketio,
            "start_background_task",
            lambda *_args, **_kwargs: worker_token,
        )

        def block_finalization(*_args, **_kwargs):
            finalization_started.set()
            try:
                assert release_finalization.wait(5.0)
            finally:
                finalization_finished.set()

        monkeypatch.setattr(server_module, "_finalize_shot_detected", block_finalization)

        for second in range(server_module._SHOT_FINALIZATION_CAPACITY):
            on_shot_detected(self._shot(second))

        fifth_callback = threading.Thread(
            target=on_shot_detected,
            args=(self._shot(server_module._SHOT_FINALIZATION_CAPACITY),),
            daemon=True,
        )
        fifth_callback.start()
        assert finalization_started.wait(2.0)
        try:
            fifth_callback.join(timeout=1.0)
            assert not fifth_callback.is_alive(), (
                "capacity eviction must not run finalization on the OPS callback"
            )
        finally:
            release_finalization.set()
            fifth_callback.join(timeout=2.0)
            assert finalization_finished.wait(2.0)
            server_module._reset_shot_sequence()

    def test_stuck_head_deadline_bounds_coordinator_and_finalizes_ops_in_order(self, monkeypatch):
        emitted, session_log = self._record_finalization(monkeypatch)
        dump_started = threading.Event()
        release_dump = threading.Event()
        first_shot_logged = threading.Event()
        all_shots_logged = threading.Event()
        background_threads = []
        simulator_shots = []
        coordinator_sizes = []
        enrichment_queue = self._enrichment_queue()
        shot_count = 20
        shots = [self._shot(second) for second in range(shot_count)]

        monkeypatch.setattr(server_module, "shot_enrichment_queue", enrichment_queue)
        monkeypatch.setattr(server_module, "shot_enrichment_task", None)
        monkeypatch.setattr(server_module, "iwr6843_runtime", object())
        monkeypatch.setattr(server_module, "_SHOT_ENRICHMENT_DEADLINE_S", 0.05, raising=False)
        monkeypatch.setattr(server_module, "_SHOT_FINALIZATION_CAPACITY", 4, raising=False)

        original_log_shot = session_log.log_shot

        def log_shot(shot, pipeline_ms=None, log_only_fields=None):
            original_log_shot(shot, pipeline_ms, log_only_fields)
            if len(session_log.shots) == 1:
                first_shot_logged.set()
            if len(session_log.shots) == shot_count:
                all_shots_logged.set()

        session_log.log_shot = log_shot

        def block_first_enrichment(shot):
            if shot.shot_number == 1:
                dump_started.set()
                assert release_dump.wait(5.0)
            shot.launch_angle_vertical = 42.0
            shot.launch_angle_vertical_source = "iwr6843"
            return server_module._ShotEnrichmentResult()

        def start_background_task(target, *args, **kwargs):
            thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            background_threads.append(thread)
            thread.start()
            return thread

        monkeypatch.setattr(
            server_module,
            "_enrich_shot_from_optional_hardware",
            block_first_enrichment,
        )
        monkeypatch.setattr(server_module.socketio, "start_background_task", start_background_task)
        monkeypatch.setattr(
            server_module,
            "_forward_shot_to_simulators",
            lambda shot: simulator_shots.append(shot.shot_number),
        )

        on_shot_detected(shots[0])
        assert dump_started.wait(2.0)
        assert first_shot_logged.wait(2.0)
        assert not release_dump.is_set(), "deadline must not wait for hardware to return"

        def submit_remaining_shots():
            for shot in shots[1:]:
                on_shot_detected(shot)
                with server_module._shot_finalization_condition:
                    bounded = server_module._shot_finalization_condition.wait_for(
                        lambda: len(server_module._shot_finalization_order) <= 4,
                        timeout=1.0,
                    )
                    assert bounded
                    coordinator_sizes.append(
                        (
                            len(server_module._shot_finalization_order),
                            len(server_module._shot_finalization_registered),
                            len(server_module._shot_finalization_ready),
                        )
                    )

        callbacks = threading.Thread(target=submit_remaining_shots, daemon=True)
        callbacks.start()
        try:
            callbacks.join(timeout=5.0)
            assert not callbacks.is_alive(), "OPS callbacks must not wait for stuck hardware"
            assert max(size[0] for size in coordinator_sizes) <= 4
            assert max(size[1] for size in coordinator_sizes) <= 4
            assert max(size[2] for size in coordinator_sizes) <= 4
            assert all_shots_logged.wait(2.0)
            assert not release_dump.is_set(), "overflow must not wait for hardware to return"
        finally:
            release_dump.set()

        callbacks.join(timeout=2.0)
        for thread in background_threads:
            thread.join(timeout=2.0)

        final_shots = [payload["shot"] for payload in self._final_update_payloads(emitted)]
        assert [row["shot_number"] for row in final_shots] == list(range(1, shot_count + 1))
        assert [row["shot_number"] for row in session_log.shots] == list(range(1, shot_count + 1))
        assert all(row["launch_angle_vertical_source"] == "estimated" for row in final_shots)
        assert all(row["carry_spin_adjusted"] > 0 for row in final_shots)
        assert all(shot.launch_angle_vertical_source == "estimated" for shot in shots)
        assert simulator_shots == list(range(1, shot_count + 1))
        self._assert_finalization_coordinator_empty()

    def test_kld7_uses_shot_impact_timestamp(self, monkeypatch):
        """K-LD7 selection should be anchored to the OPS243 impact timestamp."""
        calls = []

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                calls.append(("ball", shot_timestamp))
                return KLD7Angle(vertical_deg=12.0, confidence=0.8, num_frames=2)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                calls.append(("club", shot_timestamp))
                return None

            def reset(self):
                calls.append(("reset", None))

        emitted = []
        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.socketio, "emit", lambda *args, **kwargs: emitted.append((args, kwargs))
        )

        shot = Shot(
            ball_speed_mph=150.0,
            club_speed_mph=100.0,
            timestamp=datetime.now(),
            impact_timestamp=1234.5,
            club=ClubType.DRIVER,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert ("ball", 1234.5) in calls
        assert ("club", 1234.5) in calls
        assert emitted

    def test_implausible_kld7_angle_falls_back_to_estimate(self, monkeypatch):
        """Radar angles that conflict with club+speed should not override the estimate."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=79.4, confidence=0.58, num_frames=1)

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.angle_source == "estimated"
        assert shot.launch_angle_vertical == pytest.approx(20.5)
        assert shot.launch_angle_horizontal == pytest.approx(0.0)

    def test_low_valid_vertical_kld7_angle_beats_high_estimate(self, monkeypatch):
        """A low measured iron launch should not be replaced by a high fallback estimate."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=10.7, confidence=0.89, num_frames=6)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=52.94928729492188,
            club_speed_mph=40.32291878613282,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_vertical == pytest.approx(10.7)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_confidence == pytest.approx(0.89)
        assert shot.angle_source == "radar"

    def test_lane_disagreement_vertical_radar_shown_as_marginal_confidence(self, monkeypatch):
        """Weak vertical radar candidates should not override the launch model."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=10.7, confidence=0.72, num_frames=6)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=52.94928729492188,
            club_speed_mph=40.32291878613282,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        # Lane disagreement no longer silently replaces the measurement:
        # shown as radar with single-dot (marginal) confidence
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_vertical == pytest.approx(10.7)
        assert shot.launch_angle_vertical_confidence < 0.4

    def test_low_confidence_vertical_kld7_angle_soft_accepts_when_estimator_aligned(
        self, monkeypatch
    ):
        """A marginal vertical radar candidate can win when it agrees with the shot model."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(
                self,
                shot_timestamp=None,
                ball_speed_mph=None,
                impact_timestamp=None,
                **kwargs,
            ):
                return KLD7Angle(
                    vertical_deg=19.9,
                    confidence=0.69,
                    num_frames=10,
                    radc_selection={
                        "estimator": "geometry",
                        "selection_path": "geometry_primary",
                        "selected_frame_indices": [39, 40],
                        "selected_t_ms": [21.1, 56.3],
                        "selected_bin_errors": [19, 2],
                        "geom_fit_rmse_deg": 0.64,
                    },
                )

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=107.8,
            club_speed_mph=76.2,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_vertical == pytest.approx(19.9)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_confidence == pytest.approx(0.69)
        assert shot.angle_source == "radar"
        assert logged_buffers[0]["ball_angle"]["selection_reason"] == "soft_accept"
        assert logged_buffers[0]["ball_angle"]["radc_selection"] == {
            "estimator": "geometry",
            "selection_path": "geometry_primary",
            "selected_frame_indices": [39, 40],
            "selected_t_ms": [21.1, 56.3],
            "selected_bin_errors": [19, 2],
            "geom_fit_rmse_deg": 0.64,
        }

    def test_near_threshold_vertical_kld7_angle_displays_as_low_confidence_radar(self, monkeypatch):
        """A plausible near-threshold radar candidate should show instead of estimate."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(
                self,
                shot_timestamp=None,
                ball_speed_mph=None,
                impact_timestamp=None,
                **kwargs,
            ):
                return KLD7Angle(
                    vertical_deg=19.9,
                    confidence=0.67,
                    num_frames=1,
                    radc_selection={
                        "estimator": "geometry_single_frame",
                        "selection_path": "geometry_single_frame",
                        "selected_frame_indices": [40],
                        "selected_t_ms": [79.4],
                        "selected_bin_errors": [5],
                    },
                )

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []
        logged_shots = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                logged_shots.append(kwargs)

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=100.9,
            club_speed_mph=67.7,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_vertical == pytest.approx(19.9)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_confidence == pytest.approx(0.67)
        assert shot.angle_source == "radar"
        assert logged_shots[0]["shot"].launch_angle_vertical == pytest.approx(19.9)
        assert logged_shots[0]["shot"].launch_angle_vertical_source == "radar"
        assert logged_shots[0]["shot"].angle_source == "radar"
        assert logged_buffers[0]["ball_angle"]["selection_reason"] == "low_confidence_accept"
        assert logged_buffers[0]["ball_angle"]["acceptance_path"] == "low_confidence"
        assert logged_buffers[0]["ball_angle"]["radc_selection"] == {
            "estimator": "geometry_single_frame",
            "selection_path": "geometry_single_frame",
            "selected_frame_indices": [40],
            "selected_t_ms": [79.4],
            "selected_bin_errors": [5],
        }

    def test_low_confidence_vertical_kld7_angle_rejects_estimator_outlier(self, monkeypatch):
        """Soft acceptance should not admit high-angle lane picks from the same session."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(self, shot_timestamp=None, ball_speed_mph=None, **kwargs):
                return KLD7Angle(vertical_deg=27.8, confidence=0.75, num_frames=32)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=117.2,
            club_speed_mph=87.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )
        expected_launch, _ = estimate_launch_angle(
            shot.club,
            shot.ball_speed_mph,
            club_speed_mph=shot.club_speed_mph,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        # Marginal accept: shown as radar with single-dot confidence
        # instead of silently replaced by the club estimate
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_vertical != pytest.approx(expected_launch)
        assert shot.launch_angle_vertical_confidence < 0.4
        assert (
            logged_buffers[0]["ball_angle"]["selection_reason"]
            == "marginal_accept:estimator_delta_too_large"
        )

    def test_vertical_estimate_preserves_radar_horizontal(self, monkeypatch):
        """Vertical fallback should not erase a horizontal radar measurement."""

        class StubHorizontalTracker:
            orientation = "horizontal"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(horizontal_deg=1.5, confidence=0.68, num_frames=3)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", StubHorizontalTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.angle_source == "estimated"
        assert shot.launch_angle_vertical == pytest.approx(20.5)
        assert shot.launch_angle_horizontal == pytest.approx(1.5)
        assert shot.launch_angle_vertical_source == "estimated"
        assert shot.launch_angle_horizontal_source == "radar"

    def test_low_confidence_horizontal_radar_falls_back_to_neutral(self, monkeypatch):
        """Very low-confidence horizontal K-LD7 angles should not overwrite neutral fallback."""

        class StubHorizontalTracker:
            orientation = "horizontal"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(horizontal_deg=-8.1, confidence=0.31, num_frames=19)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", StubHorizontalTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=95.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_horizontal == pytest.approx(0.0)
        assert shot.launch_angle_horizontal_source == "estimated"
        assert shot.angle_source == "estimated"

    def test_low_confidence_horizontal_radar_soft_accepts_near_target_line(self, monkeypatch):
        """Marginal horizontal candidates can win when they stay near centerline."""

        class StubHorizontalTracker:
            orientation = "horizontal"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(self, shot_timestamp=None, ball_speed_mph=None, **kwargs):
                return KLD7Angle(horizontal_deg=-2.2, confidence=0.34, num_frames=8)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", StubHorizontalTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=95.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_horizontal == pytest.approx(-2.2)
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.launch_angle_horizontal_confidence == pytest.approx(0.34)
        assert logged_buffers[0]["ball_angle"]["selection_reason"] == "soft_accept"

    def test_low_confidence_horizontal_radar_rejects_wide_soft_lane(self, monkeypatch):
        """Soft horizontal acceptance should not admit wider marginal candidates."""

        class StubHorizontalTracker:
            orientation = "horizontal"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(self, shot_timestamp=None, ball_speed_mph=None, **kwargs):
                return KLD7Angle(horizontal_deg=-8.1, confidence=0.34, num_frames=8)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", StubHorizontalTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=95.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_horizontal == pytest.approx(0.0)
        assert shot.launch_angle_horizontal_source == "estimated"
        assert shot.angle_source == "estimated"
        assert logged_buffers[0]["ball_angle"]["selection_reason"] == "outside_soft_lane"

    def test_weak_near_limit_horizontal_radar_falls_back_to_neutral(self, monkeypatch):
        """Near-wall horizontal readings need stronger evidence than centerline readings."""

        class StubHorizontalTracker:
            orientation = "horizontal"

            def snapshot_buffer(self):
                return [{"timestamp": 1234.5, "has_radc": True}]

            def get_angle_for_shot(self, shot_timestamp=None, ball_speed_mph=None, **kwargs):
                return KLD7Angle(horizontal_deg=13.9, confidence=0.66, num_frames=2)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        logged_buffers = []

        class StubSessionLogger:
            @property
            def stats(self):
                return {"shots_detected": 0}

            def log_kld7_buffer(self, **kwargs):
                logged_buffers.append(kwargs)

            def log_shot(self, **kwargs):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", StubHorizontalTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: StubSessionLogger())
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=108.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.launch_angle_horizontal == pytest.approx(0.0)
        assert shot.launch_angle_horizontal_source == "estimated"
        assert logged_buffers[0]["ball_angle"]["selection_reason"] == "weak_near_limit"

    def test_vertical_radar_gets_neutral_horizontal_fallback(self, monkeypatch):
        """A good vertical radar angle should still emit a horizontal value."""

        class StubVerticalTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=18.7, confidence=0.8, num_frames=2)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                return None

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubVerticalTracker())
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=82.5,
            club_speed_mph=57.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.angle_source == "radar"
        assert shot.launch_angle_vertical == pytest.approx(18.7)
        assert shot.launch_angle_horizontal == pytest.approx(0.0)
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_horizontal_source == "estimated"

    def test_mock_shot_missing_angles_gets_fallback_values(self, monkeypatch):
        """Even malformed/manual mock shots should emit user-facing angles."""
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=100.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
            mode="mock",
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.angle_source == "estimated"
        assert shot.launch_angle_vertical == pytest.approx(20.5)
        assert shot.launch_angle_horizontal == pytest.approx(0.0)

    def test_implausible_club_aoa_is_rejected(self, monkeypatch):
        """A +31° club AoA is physically impossible and should be discarded."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=15.0, confidence=0.7, num_frames=2)

            def get_club_angle(self, club_speed_mph=None, shot_timestamp=None):
                # Radar reports -31° vertical → server negates to +31° AoA
                return KLD7Angle(vertical_deg=-31.0, confidence=0.7, num_frames=2)

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=115.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.club_angle_deg is None, (
            f"AoA of +31° should be rejected, got {shot.club_angle_deg}"
        )

    def test_plausible_kld7_angle_remains_radar_source(self, monkeypatch):
        """Plausible radar angles should continue to override the estimate."""

        class StubTracker:
            orientation = "vertical"

            def snapshot_buffer(self):
                return []

            def get_angle_for_shot(
                self, shot_timestamp=None, ball_speed_mph=None, impact_timestamp=None, **kwargs
            ):
                return KLD7Angle(vertical_deg=18.7, confidence=0.8, num_frames=2)

            def reset(self):
                return None

        monkeypatch.setattr(server_module, "kld7_vertical", StubTracker())
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

        shot = Shot(
            ball_speed_mph=82.5,
            club_speed_mph=57.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
        )

        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

        assert shot.angle_source == "radar"
        assert shot.launch_angle_vertical == pytest.approx(18.7)
        assert shot.launch_angle_horizontal == pytest.approx(0.0)

    def _spin_axis_shot(
        self, *, horizontal_confidence, launch_angle_vertical=18.0, club=ClubType.IRON_7
    ):
        """A shot with launch angle and club path already resolved, isolating
        the spin-axis gate itself rather than whatever radar path fed it."""
        return Shot(
            ball_speed_mph=120.0,
            club_speed_mph=85.0,
            timestamp=datetime.now(),
            impact_timestamp=1234.5,
            club=club,
            launch_angle_vertical=launch_angle_vertical,
            launch_angle_horizontal=3.2,
            launch_angle_horizontal_confidence=horizontal_confidence,
            launch_angle_horizontal_source="camera_assisted_experimental",
            club_path_deg=-1.5,
        )

    def _run_with_no_radar_hardware(self, monkeypatch, shot):
        monkeypatch.setattr(server_module, "iwr6843_runtime", None)
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)
        on_shot_detected(shot)
        self._wait_for_finalization_coordinator_idle()

    def test_spin_axis_emitted_when_horizontal_confidence_clears_gate(self, monkeypatch):
        shot = self._spin_axis_shot(horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE)

        self._run_with_no_radar_hardware(monkeypatch, shot)

        # F2P 4.7 / 0.75 = 6.267, spin loft (18 + 4) / 0.75 = 29.333: raw axis 11.0,
        # 14.5 with the 3.5 deg calibration offset.
        assert shot.spin_axis_deg == dplane_spin_axis(ClubType.IRON_7, 3.2, -1.5, 18.0)
        assert shot.spin_axis_deg == 14.5
        assert shot.spin_axis_source == "calculated"
        assert shot.to_dict()["spin_axis_source"] == "calculated"
        # The class comes from the same axis as the drawn curve: 14.5 is a slice.
        assert shot.curve_class == "slice"
        assert shot.to_dict()["curve_class"] == "slice"
        assert server_module.shot_to_dict(shot)["curve_class"] == "slice"

    @pytest.mark.parametrize(
        "club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7]
    )
    def test_spin_axis_withheld_for_woods(self, monkeypatch, club):
        shot = self._spin_axis_shot(
            horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE, club=club
        )

        self._run_with_no_radar_hardware(monkeypatch, shot)

        assert shot.spin_axis_deg is None
        assert shot.spin_axis_source is None
        assert shot.curve_class is None
        assert server_module.shot_to_dict(shot)["curve_class"] is None

    def test_spin_axis_uses_the_camera_fused_start_direction(self, monkeypatch):
        """The D-plane runs after camera fusion: an accepted camera start direction,
        not the earlier TI one, reaches the axis."""
        shot = self._spin_axis_shot(horizontal_confidence=0.9)
        seen_by_fusion = []

        def fake_fusion(fused_shot, _camera_capture, **_kw):
            seen_by_fusion.append(fused_shot.spin_axis_deg)
            fused_shot.launch_angle_horizontal = -0.5
            fused_shot.launch_angle_horizontal_confidence = 0.8
            fused_shot.launch_angle_horizontal_source = "camera_assisted_experimental"

        monkeypatch.setattr(server_module, "_fuse_camera_measurements", fake_fusion)

        self._run_with_no_radar_hardware(monkeypatch, shot)

        assert seen_by_fusion == [None]
        # Camera LD -0.5 vs path -1.5: F2P 1.333, spin loft 29.333, raw axis 2.4, 5.9 with
        # the 3.5 deg offset (TI 3.2 gives 14.5).
        assert shot.spin_axis_deg == dplane_spin_axis(ClubType.IRON_7, -0.5, -1.5, 18.0)
        assert shot.spin_axis_deg == 5.9
        assert shot.spin_axis_source == "calculated"
        assert shot.curve_class == "fade"  # from the fused axis, not the TI one (slice)

    def test_spin_axis_gate_applies_to_the_fused_start_direction(self, monkeypatch):
        """A low-confidence camera direction replacing a confident TI one withholds the axis."""
        shot = self._spin_axis_shot(horizontal_confidence=0.9)

        def fake_fusion(fused_shot, _camera_capture, **_kw):
            fused_shot.launch_angle_horizontal = -0.5
            fused_shot.launch_angle_horizontal_confidence = (
                server_module.SPIN_AXIS_MIN_CONFIDENCE - 0.01
            )
            fused_shot.launch_angle_horizontal_source = "camera_assisted_experimental"

        monkeypatch.setattr(server_module, "_fuse_camera_measurements", fake_fusion)

        self._run_with_no_radar_hardware(monkeypatch, shot)

        assert shot.spin_axis_deg is None
        assert shot.spin_axis_source is None
        assert shot.curve_class is None

    def test_spin_axis_withheld_without_vertical_launch(self, monkeypatch):
        shot = self._spin_axis_shot(
            horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE,
            launch_angle_vertical=None,
        )

        self._run_with_no_radar_hardware(monkeypatch, shot)

        assert shot.spin_axis_deg is None

    def test_spin_axis_withheld_when_horizontal_confidence_below_gate(self, monkeypatch):
        """Regression guard: spin axis must not appear the moment club path
        is non-null -- only once the horizontal leg is trustworthy enough."""
        shot = self._spin_axis_shot(
            horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE - 0.01
        )

        self._run_with_no_radar_hardware(monkeypatch, shot)

        assert shot.spin_axis_deg is None
        assert shot.curve_class is None

    @pytest.mark.parametrize(
        ("launch_direction", "expected"),
        [
            # Axes include the 3.5 deg calibration offset.
            (-8.0, "hook"),  # axis -11.5
            (-5.0, "draw"),  # -4.7
            (-3.0, "straight"),  # -0.1
            (-1.5, "fade"),  # 3.5 (a square face: the offset alone)
            (0.5, "slice"),  # 8.2
            (3.2, "slice"),  # 14.5
        ],
    )
    def test_curve_class_is_set_from_the_axis_it_draws(self, launch_direction, expected):
        """Every calculated axis gets the class of exactly that axis (path -1.5, 7 iron)."""
        shot = self._spin_axis_shot(horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE)
        shot.launch_angle_horizontal = launch_direction

        server_module._apply_dplane_spin_axis(shot)

        assert shot.spin_axis_source == "calculated"
        assert shot.spin_axis_deg == dplane_spin_axis(ClubType.IRON_7, launch_direction, -1.5, 18.0)
        assert shot.curve_class == curve_class(shot.spin_axis_deg) == expected
        assert shot.curve_class in CURVE_CLASSES

    @pytest.mark.parametrize(
        "change",
        [
            {"club": ClubType.DRIVER},
            {"club_path_deg": None},
            {"launch_angle_horizontal": None},
            {"launch_angle_vertical": None},
            {"launch_angle_horizontal_confidence": 0.0},
            {"launch_angle_horizontal_source": "radar"},
            {"launch_angle_horizontal_source": None},
        ],
    )
    def test_curve_class_stays_none_without_a_calculated_axis(self, change):
        shot = self._spin_axis_shot(horizontal_confidence=server_module.SPIN_AXIS_MIN_CONFIDENCE)
        for name, value in change.items():
            setattr(shot, name, value)

        server_module._apply_dplane_spin_axis(shot)

        assert shot.spin_axis_deg is None
        assert shot.spin_axis_source is None
        assert shot.curve_class is None

    def test_curve_class_is_none_for_an_axis_that_is_not_calculated(self):
        """Only a "calculated" axis gets a class; a mock or other axis keeps None."""
        shot = Shot(
            ball_speed_mph=120.0,
            club_speed_mph=85.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
            spin_axis_deg=-12.0,
        )

        assert shot.spin_axis_source is None
        assert shot.curve_class is None
        assert shot.to_dict()["curve_class"] is None


class TestBallisticsConfiguration:
    """The physics model is preferred unless an operator explicitly opts out."""

    @pytest.mark.parametrize(
        ("arguments", "expected"),
        [
            ([], True),
            (["--ballistics"], True),
            (["--no-ballistics"], False),
        ],
    )
    def test_cli_ballistics_preference(self, arguments, expected):
        parser = argparse.ArgumentParser()
        server_module._add_ballistics_arguments(parser)

        assert parser.parse_args(arguments).ballistics is expected

    def test_runtime_default_enables_ballistics(self):
        assert server_module.ballistics_enabled is True


class TestAirDensityConfiguration:
    """Site air density feeds the ballistic model; the default is the validated one."""

    def test_cli_defaults_to_validated_density(self):
        parser = argparse.ArgumentParser()
        server_module._add_air_density_arguments(parser)

        assert parser.parse_args([]).air_density == TRACKMAN_FLAT_AIR_DENSITY

    def test_cli_accepts_site_density(self):
        parser = argparse.ArgumentParser()
        server_module._add_air_density_arguments(parser)

        assert parser.parse_args(["--air-density", "1.05"]).air_density == pytest.approx(1.05)

    @pytest.mark.parametrize("value", ["0.5", "0.89", "1.41", "2", "nan", "inf", "dense"])
    def test_cli_rejects_implausible_density(self, value):
        parser = argparse.ArgumentParser()
        server_module._add_air_density_arguments(parser)

        with pytest.raises(SystemExit):
            parser.parse_args(["--air-density", value])

    def test_main_applies_cli_density(self, monkeypatch, tmp_path):
        for name in (
            "air_density_kg_m3",
            "typical_spin_scale",
            "ballistics_enabled",
            "battery_provider",
            "profile_store",
            "ball_speed_correction_enabled",
            "ball_speed_correction_distance_ft",
            "ball_speed_correction_ball_above_radar_ft",
            "calculated_spin_enabled",
            "_VERTICAL_RADAR_GATE_BYPASS",
            "demo_session",
        ):
            monkeypatch.setattr(server_module, name, getattr(server_module, name))
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "openflight-server",
                "--no-logging",
                "--mock",
                "--air-density",
                "1.1",
                "--profiles-path",
                str(tmp_path / "profiles.json"),
            ],
        )
        monkeypatch.setattr(server_module, "init_session_logger", lambda **_kwargs: None)
        monkeypatch.setattr(server_module, "start_monitor", lambda **_kwargs: None)
        monkeypatch.setattr(server_module.socketio, "run", lambda *_args, **_kwargs: None)
        monkeypatch.setattr(server_module, "_cleanup_hardware_for_shutdown", lambda: None)

        server_module.main()

        assert server_module.air_density_kg_m3 == pytest.approx(1.1)


class TestBatteryConfiguration:
    """Battery monitoring is explicitly enabled with a supported provider."""

    def test_cli_accepts_geekworm_provider(self):
        parser = argparse.ArgumentParser()
        server_module._add_battery_arguments(parser)

        assert parser.parse_args(["--battery", "geekworm"]).battery == "geekworm"

    def test_cli_is_disabled_by_default(self):
        parser = argparse.ArgumentParser()
        server_module._add_battery_arguments(parser)

        assert parser.parse_args([]).battery is None

    def test_cli_rejects_unknown_provider(self):
        parser = argparse.ArgumentParser()
        server_module._add_battery_arguments(parser)

        with pytest.raises(SystemExit):
            parser.parse_args(["--battery", "unknown"])


def test_kld7_device_symlinks_supply_stable_defaults(tmp_path):
    (tmp_path / "kld7_vertical").touch()
    (tmp_path / "kld7_horizontal").touch()
    args = argparse.Namespace(
        kld7=True,
        kld7_port=None,
        kld7_horizontal=False,
        kld7_horizontal_port=None,
    )

    server_module._apply_kld7_device_defaults(args, tmp_path)

    assert args.kld7_port == str(tmp_path / "kld7_vertical")
    assert args.kld7_horizontal is True
    assert args.kld7_horizontal_port == str(tmp_path / "kld7_horizontal")


class TestCarryComputation:
    """Tests for the ballistic carry path in on_shot_detected."""

    def _patch_environment(self, monkeypatch):
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(server_module, "monitor", None)
        monkeypatch.setattr(server_module, "debug_mode", False)
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: None)

    def test_carry_uses_ballistic_simulator_when_launch_angle_present(self, monkeypatch):
        """A shot with a vertical launch angle should get carry from the physics sim."""
        self._patch_environment(monkeypatch)
        monkeypatch.setattr(server_module, "ballistics_enabled", True)

        captured = {}

        from openflight import ballistics as ballistics_module

        real_simulate = ballistics_module.simulate

        def spying_simulate(conditions, *args, **kwargs):
            captured["conditions"] = conditions
            return real_simulate(conditions, *args, **kwargs)

        monkeypatch.setattr(server_module, "simulate", spying_simulate)

        shot = Shot(
            ball_speed_mph=165.0,
            club_speed_mph=112.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
            launch_angle_vertical=11.0,
            launch_angle_confidence=0.8,
            spin_rpm=2700,
            spin_confidence=0.85,
            angle_source="radar",
        )

        on_shot_detected(shot)
        _wait_for_shot_finalization_idle()

        assert "conditions" in captured, "simulate() should have been called"
        assert captured["conditions"].spin_source == "measured"
        assert shot.carry_spin_adjusted is not None
        assert 250 < shot.carry_spin_adjusted < 300

    def test_carry_falls_back_to_table_when_resolve_returns_none(self, monkeypatch):
        """When resolve_launch returns None, the table path should compute carry."""
        self._patch_environment(monkeypatch)

        monkeypatch.setattr(server_module, "resolve_launch", lambda shot, **_kwargs: None)

        def fail_simulate(*args, **kwargs):
            raise AssertionError("simulate() must not be called when resolve_launch is None")

        monkeypatch.setattr(server_module, "simulate", fail_simulate)

        shot = Shot(
            ball_speed_mph=150.0,
            club_speed_mph=105.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
            launch_angle_vertical=12.0,
            spin_rpm=2700,
            spin_confidence=0.85,
            angle_source="radar",
        )

        on_shot_detected(shot)
        _wait_for_shot_finalization_idle()

        assert shot.carry_spin_adjusted is not None
        assert shot.carry_spin_adjusted > 0

    def test_carry_skips_ballistic_when_ballistics_disabled(self, monkeypatch):
        """When ballistics_enabled is False, the simulator must not run even
        if a valid launch angle is present — carry falls through to the
        table estimator. Operators can request this with `--no-ballistics`."""
        self._patch_environment(monkeypatch)
        monkeypatch.setattr(server_module, "ballistics_enabled", False)

        def fail_resolve(*args, **kwargs):
            raise AssertionError("resolve_launch must not run when ballistics disabled")

        def fail_simulate(*args, **kwargs):
            raise AssertionError("simulate() must not run when ballistics disabled")

        monkeypatch.setattr(server_module, "resolve_launch", fail_resolve)
        monkeypatch.setattr(server_module, "simulate", fail_simulate)

        shot = Shot(
            ball_speed_mph=165.0,
            club_speed_mph=112.0,
            timestamp=datetime.now(),
            club=ClubType.DRIVER,
            launch_angle_vertical=11.0,
            launch_angle_confidence=0.8,
            spin_rpm=2700,
            spin_confidence=0.85,
            angle_source="radar",
        )

        on_shot_detected(shot)
        _wait_for_shot_finalization_idle()

        assert shot.carry_spin_adjusted is not None
        assert shot.carry_spin_adjusted > 0


class TestApplyCalculatedSpin:
    """Tests for the --calculated-spin shot rewrite."""

    def _shot(self, la=18.0, la_source="radar", ball_speed=115.0, spin=6800.0):
        return Shot(
            ball_speed_mph=ball_speed,
            timestamp=datetime.now(),
            club=ClubType.IRON_7,
            launch_angle_vertical=la,
            launch_angle_vertical_source=la_source,
            spin_rpm=spin,
            spin_confidence=0.3,
            spin_rejection_reason="SNR too low",
        )

    def test_rewrites_spin_when_launch_angle_measured(self):
        shot = self._shot()
        assert server_module._apply_calculated_spin(shot) is True
        # 170 * 115 * sin(18deg)^1.2 ~= 4800 rpm
        assert 4500 < shot.spin_rpm < 5100
        assert shot.spin_rpm_measured == 6800.0
        assert shot.spin_source == "calculated"
        assert shot.spin_confidence == pytest.approx(0.7)
        assert shot.spin_rejection_reason is None

    def test_untouched_when_launch_angle_estimated(self):
        shot = self._shot(la_source="estimated")
        assert server_module._apply_calculated_spin(shot) is False
        assert shot.spin_rpm == 6800.0
        assert shot.spin_source is None

    def test_untouched_when_no_launch_angle(self):
        shot = self._shot(la=None)
        assert server_module._apply_calculated_spin(shot) is False
        assert shot.spin_rpm == 6800.0

    def test_untouched_when_launch_angle_outside_model_range(self):
        shot = self._shot(la=1.0)
        assert server_module._apply_calculated_spin(shot) is False
        assert shot.spin_rpm == 6800.0

    def test_camera_launch_angle_accepted(self):
        shot = self._shot(la_source="camera")
        assert server_module._apply_calculated_spin(shot) is True
        assert shot.spin_source == "calculated"


class TestVerticalGateBypass:
    """--kld7-vertical-raw: show the radar angle for every candidate."""

    def _shot(self):
        return SimpleNamespace(
            club=ClubType.IRON_7, ball_speed_mph=110.0, club_speed_mph=86.0, spin_rpm=None
        )

    def test_default_marginal_accepts_out_of_lane_reading(self):
        # 0.6 deg for a 7-iron is outside the soft lane. It clears the hard
        # physics guard, so it is shown as a low-confidence (marginal) radar
        # reading rather than silently replaced by the club estimate.
        angle = KLD7Angle(vertical_deg=0.6, confidence=0.65, num_frames=1)
        accepted, details = server_module._select_vertical_radar_launch(angle, self._shot())
        assert accepted is True
        assert details["selection_reason"] == "marginal_accept:outside_soft_lane"
        assert details["acceptance_path"] == "marginal"

    def test_bypass_accepts_anything_with_a_candidate(self, monkeypatch):
        monkeypatch.setattr(server_module, "_VERTICAL_RADAR_GATE_BYPASS", True)
        angle = KLD7Angle(vertical_deg=0.6, confidence=0.65, num_frames=1)
        accepted, details = server_module._select_vertical_radar_launch(angle, self._shot())
        assert accepted is True
        assert details["selection_reason"] == "gate_bypassed"
        assert details["acceptance_path"] == "bypass"

    def test_bypass_still_needs_a_candidate(self, monkeypatch):
        monkeypatch.setattr(server_module, "_VERTICAL_RADAR_GATE_BYPASS", True)
        assert server_module._select_vertical_radar_launch(None, self._shot())[0] is False
        no_angle = KLD7Angle(vertical_deg=None, confidence=0.9, num_frames=2)
        assert server_module._select_vertical_radar_launch(no_angle, self._shot())[0] is False


class TestClubPathOwnershipGuard:
    """Two producers can write shot.club_path_deg (IWR6843 and the deprecated
    horizontal K-LD7). _process_iwr6843_angle runs first in on_shot_detected,
    so if both hardware paths were allowed to start, the deprecated radar
    would silently overwrite the IWR6843 value with no error and no log.
    The CLI must make that combination impossible to start, matching the
    existing --iwr6843/--kld7 (vertical) guard."""

    def test_iwr6843_and_kld7_horizontal_cannot_both_own_club_path(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["openflight-server", "--iwr6843", "--kld7-horizontal"])

        with pytest.raises(SystemExit) as exc_info:
            server_module.main()

        # code=2 pins this to argparse's parser.error(), not the unrelated
        # SystemExit(1) that main() raises further down when IWR6843
        # hardware init fails in a test environment -- that failure would
        # otherwise make this test pass whether or not the guard exists.
        # The message pins it to *this* guard, not one of the other
        # parser.error() calls in the same validation block.
        assert exc_info.value.code == 2
        assert "cannot both own club path" in capsys.readouterr().err

    def test_iwr6843_and_kld7_vertical_cannot_both_own_launch_angle(self, monkeypatch, capsys):
        """Existing guard this one is modeled on -- pinned so a refactor of
        the argparse validation block can't quietly drop either check."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["openflight-server", "--iwr6843", "--kld7", "--kld7-mount-tilt", "0"],
        )

        with pytest.raises(SystemExit) as exc_info:
            server_module.main()

        assert exc_info.value.code == 2
        assert "cannot both own launch angle" in capsys.readouterr().err


class TestOpsBaudValidation:
    """The radar can only move to a rate it has an ``In`` API command for, so an
    unsupported --ops-baud is refused by the hardware and leaves the link at
    whatever answered. That presents as an unresponsive app -- a 40KB dump takes
    ~21s at 19,200 against ~1.8s at 230,400 -- rather than as a bad flag, and 0
    or a negative value reaches pyserial directly. Its sibling geometry flags
    already validate via parser.error; this one did not."""

    @pytest.mark.parametrize("bad", ["0", "-1", "250000", "9601"])
    def test_unsupported_ops_baud_is_refused_at_the_cli(self, monkeypatch, capsys, bad):
        monkeypatch.setattr(sys, "argv", ["openflight-server", "--ops-baud", bad])

        with pytest.raises(SystemExit) as exc_info:
            server_module.main()

        # code=2 pins this to parser.error() rather than a later SystemExit(1)
        # from hardware init failing in a test environment, which would make
        # this pass whether or not the guard exists.
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "--ops-baud must be one of" in err
        # The message must name the valid rates; "invalid value" alone leaves
        # the operator guessing which of five the radar accepts.
        assert "230400" in err and "9600" in err

    @pytest.mark.parametrize("good", [9600, 19200, 57600, 115200, 230400])
    def test_every_api_supported_baud_is_accepted(self, good):
        """The guard must admit exactly the rates the radar has a command for --
        a stricter check would reject a legitimate fallback to 115200, which the
        flag's own help text tells operators to use."""
        assert good in UART_BAUD_COMMANDS


@pytest.mark.usefixtures("isolate_finalization")
class TestBallisticCarryPrecedence:
    """Finalization is the single writer of carry_spin_adjusted.

    The simulator owns carry whenever it can run; the spin table owns it
    otherwise. Anything already on the shot is replaced either way.
    """

    @pytest.fixture(autouse=True)
    def _isolate_finalization(self):
        server_module._reset_shot_sequence()
        yield
        _wait_for_shot_finalization_idle()

    @staticmethod
    def _shot(*, launch_angle: float | None, prefilled_carry: float | None) -> Shot:
        # Field report, 2026-09-23: a 7-iron where the kiosk showed the 119 yd
        # spin-table number while the simulator and a commercial launch
        # monitor both landed near 143 yd.
        return Shot(
            ball_speed_mph=104.2,
            club_speed_mph=83.7,
            timestamp=datetime(2026, 9, 23, 12, 0, 0),
            impact_timestamp=100.0,
            club=ClubType.IRON_7,
            spin_rpm=5164.0,
            spin_confidence=0.9,
            launch_angle_vertical=launch_angle,
            launch_angle_confidence=0.9 if launch_angle is not None else None,
            carry_spin_adjusted=prefilled_carry,
            mode="rolling-buffer",
        )

    def test_simulator_overrides_prefilled_table_carry(self, monkeypatch):
        monkeypatch.setattr(server_module, "ballistics_enabled", True)
        shot = self._shot(launch_angle=19.1, prefilled_carry=119.2)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        expected = server_module.simulate(
            server_module.resolve_launch(shot), air_density=server_module.air_density_kg_m3
        ).carry_yards
        assert shot.carry_spin_adjusted == pytest.approx(expected)
        assert shot.carry_spin_adjusted != pytest.approx(119.2)
        assert shot.carry_spin_adjusted > 135.0

    def test_table_fallback_replaces_prefilled_carry_when_ballistics_disabled(self, monkeypatch):
        monkeypatch.setattr(server_module, "ballistics_enabled", False)
        shot = self._shot(launch_angle=19.1, prefilled_carry=999.0)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        expected = server_module.estimate_carry_with_spin(
            104.2, 5164.0, ClubType.IRON_7, club_speed_mph=83.7
        )
        assert shot.carry_spin_adjusted == pytest.approx(expected)

    def test_table_fallback_replaces_prefilled_carry_without_launch_angle(self, monkeypatch):
        monkeypatch.setattr(server_module, "ballistics_enabled", True)
        monkeypatch.setattr(server_module, "_ensure_user_facing_launch_angles", lambda _shot: None)
        shot = self._shot(launch_angle=None, prefilled_carry=999.0)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        assert shot.carry_spin_adjusted is not None
        assert shot.carry_spin_adjusted != pytest.approx(999.0)
        assert 0 < shot.carry_spin_adjusted < 200

    @pytest.mark.parametrize(
        ("offset_from_floor", "uses_measured_spin"),
        [(0.0, True), (-0.01, False)],
    )
    def test_table_fallback_spin_gate_matches_monitor_reliability(
        self, monkeypatch, offset_from_floor, uses_measured_spin
    ):
        """The fallback trusts measured spin on the same floor SpinResult.is_reliable uses."""
        from openflight.launch_monitor import SPIN_CONFIDENCE_RELIABLE
        from openflight.rolling_buffer.types import SpinResult

        confidence = SPIN_CONFIDENCE_RELIABLE + offset_from_floor
        assert (
            SpinResult(spin_rpm=5164, confidence=confidence, snr=10.0, quality="medium").is_reliable
            is uses_measured_spin
        )

        monkeypatch.setattr(server_module, "ballistics_enabled", False)
        shot = self._shot(launch_angle=19.1, prefilled_carry=None)
        shot.spin_confidence = confidence

        server_module._finalize_shot_detected(shot, emit_event="shot")

        measured = server_module.estimate_carry_with_spin(
            104.2, 5164.0, ClubType.IRON_7, club_speed_mph=83.7
        )
        optimal = server_module.estimate_carry_with_spin(
            104.2,
            server_module.get_optimal_spin_for_ball_speed(104.2, ClubType.IRON_7),
            ClubType.IRON_7,
            club_speed_mph=83.7,
        )
        assert measured != pytest.approx(optimal)
        expected = measured if uses_measured_spin else optimal
        assert shot.carry_spin_adjusted == pytest.approx(expected)

    def test_table_fallback_fills_empty_carry(self, monkeypatch):
        monkeypatch.setattr(server_module, "ballistics_enabled", False)
        shot = self._shot(launch_angle=19.1, prefilled_carry=None)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        assert shot.carry_spin_adjusted is not None
        assert shot.carry_spin_adjusted > 0

    def test_simulator_carry_reaches_sim_connectors(self, monkeypatch):
        monkeypatch.setattr(server_module, "ballistics_enabled", True)
        forwarded = []
        monkeypatch.setattr(server_module, "_forward_shot_to_simulators", forwarded.append)
        shot = self._shot(launch_angle=19.1, prefilled_carry=None)

        server_module._finalize_shot_detected(shot, emit_event="shot")

        assert forwarded == [shot]
        resolved = server_module.resolve_shot(forwarded[0], server_module.SimPlayerState())
        assert resolved.carry_yards == pytest.approx(shot.carry_spin_adjusted)
        assert resolved.carry_yards > 135.0


@pytest.mark.parametrize(
    ("live_mirror", "capture_metadata", "expected_sign"),
    [
        (True, {"settings": {"mirror_horizontal": False}}, 1.0),
        (False, {"settings": {"mirror_horizontal": True}}, -1.0),
        (True, {}, -1.0),
        (False, None, 1.0),
    ],
)
def test_camera_fusion_uses_the_mirror_setting_recorded_with_the_capture(
    monkeypatch, tmp_path, live_mirror, capture_metadata, expected_sign
):
    from openflight.camera import ball_flight, club_delivery

    calls = {}

    def record(name, result):
        def fake(*_args, **kwargs):
            calls[name] = kwargs["geometry"]
            return result

        return fake

    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=np.arange(8, dtype=np.int64),
        trigger_host_timestamp_ns=np.int64(3),
        pre_trigger_count=np.int32(4),
    )
    monkeypatch.setattr(
        ball_flight,
        "estimate_camera_ball_flight",
        record("ball", ball_flight.CameraBallEstimate(status="rejected_test")),
    )
    monkeypatch.setattr(
        club_delivery,
        "estimate_chained_delivery",
        record("club", club_delivery.ChainedDelivery(status="rejected_test")),
    )
    monkeypatch.setattr(
        server_module,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=1.524, radar_height_m=0.15875, tee_ball_height_m=0.04
            )
        ),
    )
    monkeypatch.setattr(
        server_module,
        "camera_capture_config",
        {"mount_height_m": 0.20955, "mirror_horizontal": live_mirror, "width": 4, "height": 4},
    )
    capture = SimpleNamespace(valid=True, path=tmp_path, metadata=capture_metadata)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)

    server_module._fuse_camera_ball_flight(shot, capture)
    server_module._fuse_camera_club_delivery(shot, capture)

    assert calls["ball"].horizontal_pixel_sign == expected_sign
    assert calls["club"].horizontal_pixel_sign == expected_sign


class TestIWR6843HorizontalTrajectoryFit:
    """The bearing-to-direction fit is opt-in and announces a missing zero."""

    @staticmethod
    def _init(monkeypatch, tmp_path, **kwargs):
        class FakeCaptureMonitor:
            def __init__(self, **_kwargs):
                self.port = "/dev/ttyUSB0"

            def start(self, *, armed=True):
                return None

            def stop(self):
                return None

        monkeypatch.setattr(Calibration, "load", lambda _path: Calibration.identity())
        monkeypatch.setattr("openflight.iwr6843.monitor.IWR6843CaptureMonitor", FakeCaptureMonitor)
        monkeypatch.setattr("openflight.iwr6843.monitor.tx_order_from_config", lambda _p: "normal")
        assert server_module.init_iwr6843(
            port="/dev/ttyUSB0",
            config_path="snapshot.cfg",
            calibration_path="cal.json",
            output_dir=tmp_path,
            trigger_pin=17,
            tee_range_m=1.5,
            net_range_m=100.0,
            tx_order="auto",
            capture_timeout_s=12.0,
            **kwargs,
        )
        runtime, config = server_module.iwr6843_runtime, server_module.iwr6843_runtime_config
        server_module.iwr6843_runtime = None
        return runtime, config

    def test_default_leaves_runtime_and_logged_config_unchanged(self, monkeypatch, tmp_path):
        runtime, config = self._init(monkeypatch, tmp_path)

        assert runtime.horizontal_trajectory_fit is False
        assert "horizontal_trajectory_fit" not in config
        assert "tee_lateral_m" not in config

    def test_enabled_fit_reaches_runtime_and_config(self, monkeypatch, tmp_path):
        runtime, config = self._init(
            monkeypatch,
            tmp_path,
            horizontal_phase_reference_rad=-0.37,
            horizontal_trajectory_fit=True,
            tee_lateral_m=0.05,
        )

        assert runtime.horizontal_trajectory_fit is True
        assert runtime.tee_lateral_m == pytest.approx(0.05)
        assert config["horizontal_trajectory_fit"] is True
        assert config["tee_lateral_m"] == pytest.approx(0.05)

    def test_enabled_fit_without_phase_reference_warns(self, monkeypatch, tmp_path, caplog):
        with caplog.at_level("WARNING"):
            self._init(monkeypatch, tmp_path, horizontal_trajectory_fit=True)

        assert "horizontal-phase-reference-rad" in caplog.text


# --- Calculated club data on the shot ("berechnet") -------------------------------------

CLUB_DATA_KEYS = {
    "club_data_status",
    "club_data_reason",
    "camera_club_path_deg",
    "camera_club_path_sigma_deg",
    "camera_attack_angle_deg",
    "camera_attack_angle_sigma_deg",
    "camera_low_point_cm",
    "camera_low_point_sigma_cm",
    "dynamic_loft_deg",
    "dynamic_loft_sigma_deg",
    "spin_loft_deg",
    "spin_loft_sigma_deg",
    "face_to_target_deg",
    "face_to_target_sigma_deg",
    "face_to_path_deg",
    "face_to_path_sigma_deg",
    "face_to_path_class",
    "calculated_spin_axis_deg",
    "calculated_spin_axis_sigma_deg",
    "spin_axis_class",
    "impact_toe_mm",
    "impact_sigma_mm",
    "impact_offset_mm",
    "impact_height_mm",
    "impact_height_sigma_mm",
    "impact_zone",
    "impact_method",
    "impact_scale_mm_per_px",
}
CLUB_DATA_TEXT_KEYS = {
    "club_data_status",
    "club_data_reason",
    "face_to_path_class",
    "spin_axis_class",
    "impact_zone",
    "impact_method",
}


def test_a_shot_carries_every_club_data_key_unset_by_default():
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())

    logged, shown = shot.to_dict(), shot_to_dict(shot)

    assert CLUB_DATA_KEYS <= set(logged)
    assert CLUB_DATA_KEYS <= set(shown)
    assert all(logged[key] is None and shown[key] is None for key in CLUB_DATA_KEYS)


def test_shot_to_dict_rounds_the_club_data_to_a_tenth_and_withholds_nothing():
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    numbers = sorted(CLUB_DATA_KEYS - CLUB_DATA_TEXT_KEYS)
    for offset, key in enumerate(numbers):
        setattr(shot, key, -3.14159 + offset)
    shot.club_data_status, shot.club_data_reason = "done", "accepted"
    shot.face_to_path_class, shot.spin_axis_class = "closed", "draw"
    shot.impact_zone, shot.impact_method = "toe", "D"

    shown = shot_to_dict(shot)

    for offset, key in enumerate(numbers):
        # The impact scale (mm per pixel, about 3) keeps two decimals.
        digits = 2 if key == "impact_scale_mm_per_px" else 1
        assert shown[key] == round(-3.14159 + offset, digits)
        assert shot.to_dict()[key] == -3.14159 + offset  # the log keeps the full value
    assert (shown["club_data_status"], shown["club_data_reason"]) == ("done", "accepted")
    assert (shown["face_to_path_class"], shown["spin_axis_class"]) == ("closed", "draw")
    assert (shown["impact_zone"], shown["impact_method"]) == ("toe", "D")


def test_session_state_carries_the_club_data_once_it_is_on_the_stored_shot(monkeypatch):
    monitor = MockLaunchMonitor()
    monitor.connect()
    monitor.start()
    shot = monitor.simulate_shot()
    monkeypatch.setattr(server_module, "monitor", monitor)
    assert server_module._session_state_payload()["shots"][0]["club_data_status"] is None

    shot.club_data_status = "done"
    shot.camera_club_path_deg = 2.04
    shot.face_to_path_class = "closed"

    (row,) = server_module._session_state_payload()["shots"]
    assert (row["club_data_status"], row["camera_club_path_deg"]) == ("done", 2.0)
    assert row["face_to_path_class"] == "closed"


def _club_data_session_state() -> None:
    """Give the session a calibrator and head lengths, as after a few club-data runs."""
    server_module._reset_club_data_state("test")
    server_module._club_data_calibrator_instance().add(-4.4, -5.6, teed=True)
    server_module._club_head_length_mm[("p1", "8i")] = [74.0] * 5


def _club_data_state_is_fresh() -> bool:
    calibrator = server_module._club_data_calibrator
    return (calibrator is None or calibrator.delay()[2] == 0) and not (
        server_module._club_head_length_mm
    )


class TestClubDataSessionState:
    """The acoustic-delay calibration and the head lengths belong to one session."""

    @pytest.fixture(name="store")
    def fixture_store(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(server_module, "profile_store", store)
        monkeypatch.setattr(server_module.socketio, "emit", lambda *_args, **_kwargs: None)
        yield store
        server_module._reset_club_data_state("test")

    def test_a_profile_switch_drops_it(self, store):
        first = store.list()[0]
        store.add("Second")
        _club_data_session_state()

        server_module.handle_set_active_profile({"profile_id": first.id})

        assert _club_data_state_is_fresh()

    def test_reselecting_the_active_profile_keeps_it(self, store):
        active = store.get_active().id
        _club_data_session_state()
        calibrator = server_module._club_data_calibrator

        server_module.handle_set_active_profile({"profile_id": active})

        assert server_module._club_data_calibrator is calibrator
        assert calibrator.delay()[2] == 1
        assert server_module._club_head_length_mm == {("p1", "8i"): [74.0] * 5}

    def test_an_unknown_profile_id_keeps_it(self, store):
        _club_data_session_state()
        calibrator = server_module._club_data_calibrator

        server_module.handle_set_active_profile({"profile_id": "no-such-profile"})

        assert server_module._club_data_calibrator is calibrator
        assert server_module._club_head_length_mm == {("p1", "8i"): [74.0] * 5}

    def test_a_rejected_new_profile_keeps_it(self, store):
        _club_data_session_state()
        calibrator = server_module._club_data_calibrator

        server_module.handle_add_profile({"name": "   "})

        assert server_module._club_data_calibrator is calibrator
        assert server_module._club_head_length_mm == {("p1", "8i"): [74.0] * 5}

    def test_a_new_profile_drops_it(self, store):
        _club_data_session_state()

        server_module.handle_add_profile({"name": "Range"})

        assert _club_data_state_is_fresh()

    def test_a_session_clear_drops_it(self, store, monkeypatch):
        monitor = MockLaunchMonitor()
        monitor.connect()
        monkeypatch.setattr(server_module, "monitor", monitor)
        _club_data_session_state()

        server_module.handle_clear_session({"profile_id": store.get_active().id})

        assert _club_data_state_is_fresh()

    def test_a_new_session_drops_it(self, monkeypatch):
        _club_data_session_state()
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(server_module, "monitor", None)
        try:
            server_module.start_monitor(mock=True)
        finally:
            server_module.stop_monitor()

        assert _club_data_state_is_fresh()
