"""Server glue of the camera angle reference: shot pipeline, target tap, resets, startup."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from openflight import server as server_module
from openflight.angle_reference import (
    BALL_MARK_OUTSIDE_MODE,
    MIN_PAIRS,
    ball_mark_from_tap,
    canonical_geometry,
    load_camera_marks,
    resolve_target_line_path,
    save_camera_marks,
    target_line_from_tap,
)
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.departed_ball import GroundBallAnchor
from openflight.camera.intrinsics import CameraIntrinsics
from openflight.clubs import ClubType
from openflight.launch_monitor import Shot

INTRINSICS = CameraIntrinsics(
    fx=575.7,
    fy=577.3,
    cx=347.8,
    cy=246.9,
    dist_coeffs=(-0.2, 0.07, 0.001, -0.0005, -0.01),
    image_width_px=640,
    image_height_px=400,
    rotate_180=False,
    mirror_horizontal=False,
    scaler_crop=None,
    rms_reprojection_px=0.38,
)


class FakeSessionLog:
    def __init__(self):
        self.angle_entries = []
        self.config_changes = []

    def log_angle_reference(self, event, state, reason=None):
        self.angle_entries.append({"event": event, "reason": reason, **state})

    def log_config_change(self, config, source="user"):
        self.config_changes.append((config, source))


# crop-500 (320x200) stands for a capture mode without a known frame mapping.
MODE_SIZES = {
    "full-300": (640, 400),
    "strip-500": (640, 200),
    "strip-560": (640, 200),
    "crop-500": (320, 200),
}


class FakeRuntime:
    """The capture runtime as the settings handler and the target geometry read it."""

    def __init__(self):
        self.vertical_offset_px = 0
        self.width, self.height = 640, 400
        # A capture-mode change also checks the clip length for the departure locator.
        self.settings = SimpleNamespace(fps=300.0, post_ms=100.0, post_frames=30)

    def status(self):
        return {"running": True, "armed": True}

    def vertical_crop_status(self):
        return {"raw_crop_adjustable": True, "vertical_offset_px": self.vertical_offset_px}

    def capture_mode_status(self):
        return {"width": self.width, "height": self.height}

    def update_capture_mode(self, mode_id):
        self.width, self.height = MODE_SIZES[mode_id]
        return self.capture_mode_status()

    def active_tee_zone(self):
        return None

    def update_vertical_crop(self, offset_px):
        self.vertical_offset_px = offset_px
        return {"vertical_offset_px": offset_px}


@pytest.fixture(name="camera")
def fixture_camera(monkeypatch):
    """A running camera with the range lens and a capturing session log."""
    runtime = FakeRuntime()
    session_log = FakeSessionLog()
    emitted = []
    monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
    monkeypatch.setattr(server_module, "camera_intrinsics", INTRINSICS)
    monkeypatch.setattr(
        server_module,
        "camera_capture_config",
        {
            "enabled": True,
            "width": 640,
            "height": 400,
            "rotate_180": False,
            "mirror_horizontal": False,
            "scaler_crop": None,
            "roll_correction_deg": 0.0,
            "mount_height_m": 0.182,
            "lateral_offset_m": -0.025,
            "forward_offset_m": 0.007,
        },
    )
    monkeypatch.setattr(server_module, "get_session_logger", lambda: session_log)
    monkeypatch.setattr(
        server_module.socketio, "emit", lambda event, payload=None: emitted.append((event, payload))
    )
    return SimpleNamespace(runtime=runtime, log=session_log, emitted=emitted)


def _learn_offsets(pairs=MIN_PAIRS):
    for _ in range(pairs):
        shot = _camera_shot()
        server_module.angle_reference.apply(shot)


def _camera_shot(camera_vertical=18.0, ti_vertical=20.0) -> Shot:
    return Shot(
        ball_speed_mph=110.0,
        timestamp=datetime.now(),
        launch_angle_vertical=ti_vertical,
        launch_angle_vertical_source="radar",
        iwr6843_vertical_deg=ti_vertical,
        experimental_camera_vertical_deg=camera_vertical,
        experimental_camera_vertical_confidence=0.75,
        launch_angle_horizontal=1.0,
        launch_angle_horizontal_source="camera_assisted_experimental",
        experimental_camera_horizontal_deg=1.0,
        iwr6843_horizontal_deg=-6.0,
    )


def _last_settings(camera):
    return next(
        payload for event, payload in reversed(camera.emitted) if event == "camera_capture_settings"
    )


class TestTargetTap:
    def test_a_tap_sets_persists_and_reports_the_target(self, camera):
        _learn_offsets()

        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )

        target = server_module.angle_reference.target_line
        assert target is not None
        assert target.azimuth_deg > 0.0  # right of the principal point
        payload = _last_settings(camera)
        assert payload["target_line"]["azimuth_deg"] == pytest.approx(target.azimuth_deg, abs=1e-3)
        assert payload["target_line"]["preview_x_pct"] == 62.5
        assert payload["target_line_unavailable_reason"] is None
        saved, ball, reason = load_camera_marks(
            resolve_target_line_path(), server_module._camera_target_geometry()
        )
        assert reason is None and saved.to_dict() == target.to_dict()
        assert ball is None and payload["ball_mark"] is None
        # The camera directions are now measured from the target: the horizontal restarts.
        assert server_module.angle_reference.vertical.valid
        assert server_module.angle_reference.horizontal.state()["pairs"] == 0
        assert camera.log.angle_entries[-1]["event"] == "target_set"
        assert camera.log.angle_entries[-1]["target_line"]["azimuth_deg"] == pytest.approx(
            target.azimuth_deg, abs=1e-3
        )

    def test_a_null_tap_clears_the_target_and_its_file(self, camera):
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )

        server_module.handle_set_camera_capture_settings({"target_tap": None})

        assert server_module.angle_reference.target_line is None
        assert _last_settings(camera)["target_line"] is None
        assert not resolve_target_line_path().exists()
        assert camera.log.angle_entries[-1]["event"] == "target_cleared"

    def test_the_tap_honours_the_roll_and_the_mirror(self, camera, monkeypatch):
        mirrored = replace(INTRINSICS, mirror_horizontal=True)
        monkeypatch.setattr(server_module, "camera_intrinsics", mirrored)
        server_module.camera_capture_config.update(roll_correction_deg=1.35, mirror_horizontal=True)
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )

        expected = target_line_from_tap(
            62.5,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=1.35,
            horizontal_pixel_sign=-1.0,
            intrinsics=mirrored,
            geometry={},
        )
        assert expected.azimuth_deg < 0.0  # mirrored: right in the picture is target-left
        assert server_module.angle_reference.target_line.azimuth_deg == pytest.approx(
            expected.azimuth_deg
        )

    def test_without_the_lens_calibration_there_is_no_target(self, camera, monkeypatch):
        monkeypatch.setattr(server_module, "camera_intrinsics", None)

        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )

        assert server_module.angle_reference.target_line is None
        event, payload = camera.emitted[-1]
        assert event == "camera_capture_settings_error"
        assert "--camera-intrinsics" in payload["error"]
        assert (
            "--camera-intrinsics"
            in server_module._camera_capture_settings_payload()["target_line_unavailable_reason"]
        )

    def test_a_mode_without_a_lens_mapping_refuses_the_tap(self, camera):
        server_module.camera_capture_config.update(width=320, height=200)

        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )

        assert camera.emitted[-1][0] == "camera_capture_settings_error"
        assert "width" in camera.emitted[-1][1]["error"]

    def test_another_orientation_refuses_the_tap(self, camera):
        server_module.camera_capture_config.update(rotate_180=True)

        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )

        assert camera.emitted[-1][0] == "camera_capture_settings_error"
        assert "rotate_180" in camera.emitted[-1][1]["error"]

    def test_the_strip_mode_takes_a_target_in_the_direction_of_the_full_frame(self, camera):
        """2026-11-02: in 640x200 the target was locked ("height: calibrated 400, capture 200")."""
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )
        full = server_module.angle_reference.target_line
        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
        assert _last_settings(camera)["target_line_unavailable_reason"] is None

        # The same saved-frame pixel of the full frame, 100 rows higher in the strip.
        strip_y_pct = (full.frame_y_px - 100.0 + 0.5) / 200.0 * 100.0
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": strip_y_pct}}
        )

        strip = server_module.angle_reference.target_line
        assert camera.emitted[-1][0] == "camera_capture_settings"
        assert strip.frame_y_px == pytest.approx(full.frame_y_px - 100.0)
        assert strip.azimuth_deg == pytest.approx(full.azimuth_deg, abs=1e-9)
        assert strip.elevation_deg == pytest.approx(full.elevation_deg, abs=1e-9)

    @pytest.mark.parametrize(
        "data",
        [
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}, "alignment_x_pct": 40.0},
            {"target_tap": {"x_pct": 150.0, "y_pct": 40.0}},
            {"target_tap": [50.0, 40.0]},
        ],
    )
    def test_a_malformed_tap_is_refused(self, camera, data):
        server_module.handle_set_camera_capture_settings(data)

        assert server_module.angle_reference.target_line is None
        assert camera.emitted[-1][0] == "camera_capture_settings_error"

    @pytest.mark.parametrize("change", [{"capture_mode": "crop-500"}, {"rotate": True}])
    def test_a_geometry_change_clears_the_target_and_both_offsets(
        self, camera, change, monkeypatch
    ):
        monkeypatch.setattr(server_module, "_shot_finalization_order", [])
        monkeypatch.setattr(server_module, "_shot_finalization_running", False)
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )
        _learn_offsets()

        if change.pop("rotate", False):
            # Not a mode switch: no frame mapping carries a mark across a flip.
            before = server_module._camera_target_geometry()
            server_module.camera_capture_config.update(rotate_180=True)
            server_module._carry_camera_marks_over_geometry_change(before)
        else:
            server_module.handle_set_camera_capture_settings(change)

        assert server_module.angle_reference.target_line is None
        assert not resolve_target_line_path().exists()
        assert not server_module.angle_reference.vertical.valid
        assert not server_module.angle_reference.horizontal.valid
        events = [entry["event"] for entry in camera.log.angle_entries]
        assert events[-2:] == ["camera_geometry_changed", "offset_reset"]
        assert camera.log.angle_entries[-1]["reason"] == "camera_geometry_changed"

    def test_an_unchanged_sensor_window_keeps_the_target(self, camera):
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 50.0, "y_pct": 40.0}}
        )

        server_module.handle_set_camera_capture_settings({"vertical_offset_px": 0})

        assert server_module.angle_reference.target_line is not None


class TestBallTap:
    def test_a_tap_sets_persists_and_reports_the_ball_but_no_target(self, camera):
        _learn_offsets()

        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )

        ball = server_module.angle_reference.ball_mark
        assert ball.frame_px == (pytest.approx(367.5), pytest.approx(263.5))
        payload = _last_settings(camera)
        assert payload["ball_mark"]["frame_x_px"] == 367.5
        assert payload["ball_mark"]["preview_y_pct"] == 66.0
        assert "geometry" not in payload["ball_mark"]
        # The Live screen reads target_line: null still means "no target".
        assert payload["target_line"] is None
        target, saved, reason = load_camera_marks(
            resolve_target_line_path(), server_module._camera_target_geometry()
        )
        assert (target, reason) == (None, None)
        assert saved.to_dict() == ball.to_dict()
        # The ball mark is no direction: both offsets keep their pairs.
        assert server_module.angle_reference.vertical.valid
        assert server_module.angle_reference.horizontal.state()["pairs"] == MIN_PAIRS
        assert camera.log.angle_entries[-1]["event"] == "ball_set"
        assert camera.log.angle_entries[-1]["ball_mark"]["frame_y_px"] == 263.5

    def test_both_marks_share_the_file_and_clear_one_by_one(self, camera):
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )
        geometry = server_module._camera_target_geometry()
        target, ball, _reason = load_camera_marks(resolve_target_line_path(), geometry)
        assert target is not None and ball is not None

        server_module.handle_set_camera_capture_settings({"ball_tap": None})

        assert server_module.angle_reference.ball_mark is None
        assert _last_settings(camera)["ball_mark"] is None
        assert _last_settings(camera)["target_line"] is not None
        target, ball, _reason = load_camera_marks(resolve_target_line_path(), geometry)
        assert target is not None and ball is None
        assert camera.log.angle_entries[-1]["event"] == "ball_cleared"

        server_module.handle_set_camera_capture_settings({"target_tap": None})

        assert not resolve_target_line_path().exists()

    def test_clearing_the_target_keeps_the_ball(self, camera):
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )

        server_module.handle_set_camera_capture_settings({"target_tap": None})

        _target, ball, _reason = load_camera_marks(
            resolve_target_line_path(), server_module._camera_target_geometry()
        )
        assert ball is not None
        assert _last_settings(camera)["ball_mark"] is not None

    def test_the_ball_needs_no_lens_calibration(self, camera, monkeypatch):
        monkeypatch.setattr(server_module, "camera_intrinsics", None)

        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 50.0, "y_pct": 66.0}}
        )

        assert server_module.angle_reference.ball_mark is not None
        assert camera.emitted[-1][0] == "camera_capture_settings"

    def test_the_tap_honours_the_roll(self, camera):
        server_module.camera_capture_config.update(roll_correction_deg=1.35)

        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )

        expected = ball_mark_from_tap(
            57.5, 66.0, width=640, height=400, roll_correction_deg=1.35, geometry={}
        )
        assert server_module.angle_reference.ball_mark.frame_px == pytest.approx(expected.frame_px)

    @pytest.mark.parametrize("tap", [{"x_pct": 57.5, "y_pct": 66.0}, None])
    def test_setting_or_clearing_the_mark_restarts_the_ball_anchors(self, camera, monkeypatch, tap):
        trackers = ("camera_ball_flight_reference_tracker", "camera_lowlight_reference_tracker")
        anchors = ("camera_ball_flight_ground_anchor", "camera_lowlight_ground_anchor")
        old = {}
        for name in trackers:
            old[name] = ReferenceBallTracker()
            for _ in range(3):
                old[name].resolve(ReferenceBall(313.6, 169.3, 17.0, 227))
        for name in anchors:
            old[name] = GroundBallAnchor()
        club_tracker = ReferenceBallTracker()
        monkeypatch.setattr(server_module, "camera_reference_ball_tracker", club_tracker)
        for name, value in old.items():
            monkeypatch.setattr(server_module, name, value)

        server_module.handle_set_camera_capture_settings({"ball_tap": tap})

        for name in trackers:
            assert getattr(server_module, name) is not old[name]
            assert getattr(server_module, name).fallback() is None
        for name in anchors:
            assert getattr(server_module, name) is not old[name]
            assert getattr(server_module, name).samples == 0
        # The chained club delivery keeps its own tracker: it does not read the mark.
        assert server_module.camera_reference_ball_tracker is club_tracker

    @pytest.mark.parametrize(
        "data",
        [
            {"ball_tap": {"x_pct": 50.0, "y_pct": 66.0}, "alignment_x_pct": 40.0},
            {"ball_tap": {"x_pct": 50.0, "y_pct": -1.0}},
            {"ball_tap": "50,66"},
            {"ball_tap": {"x_pct": 50.0, "y_pct": 66.0}, "target_tap": None},
        ],
    )
    def test_a_malformed_tap_is_refused(self, camera, data):
        server_module.handle_set_camera_capture_settings(data)

        assert server_module.angle_reference.ball_mark is None
        assert camera.emitted[-1][0] == "camera_capture_settings_error"

    @pytest.mark.parametrize(
        "change", [{"capture_mode": "crop-500"}, {"capture_mode": "crop-500", "offset": 20}]
    )
    def test_a_geometry_change_clears_both_marks(self, camera, change, monkeypatch):
        monkeypatch.setattr(server_module, "_shot_finalization_order", [])
        monkeypatch.setattr(server_module, "_shot_finalization_running", False)
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )

        offset = change.pop("offset", None)
        server_module.handle_set_camera_capture_settings(change)
        if offset is not None:
            server_module.handle_set_camera_capture_settings({"vertical_offset_px": offset})

        assert server_module.angle_reference.ball_mark is None
        assert server_module.angle_reference.target_line is None
        assert not resolve_target_line_path().exists()
        assert _last_settings(camera)["ball_mark"] is None

    def test_a_geometry_change_clears_a_ball_without_a_target(self, camera):
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )

        server_module.handle_set_camera_capture_settings({"capture_mode": "crop-500"})

        assert server_module.angle_reference.ball_mark is None
        assert not resolve_target_line_path().exists()
        assert "camera_geometry_changed" in [entry["event"] for entry in camera.log.angle_entries]

    def test_the_ball_flight_and_its_low_light_shadow_get_the_mark(self, camera, monkeypatch):
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": 66.0}}
        )
        calibration = SimpleNamespace(tee_range_m=1.5, radar_height_m=0.15, tee_ball_height_m=0.02)
        monkeypatch.setattr(
            server_module, "iwr6843_runtime", SimpleNamespace(calibration=calibration)
        )
        camera.runtime.active_tee_zone = lambda: None
        monkeypatch.setattr(server_module, "camera_lowlight_shadow_enabled", True)
        monkeypatch.setattr(
            server_module,
            "_camera_analysis_eligibility",
            lambda _capture: (False, False, {"status": "too_dark"}),
        )
        shot = _camera_shot()
        shot.mode = "rolling-buffer"

        job = server_module._lowlight_shadow_job(shot, SimpleNamespace(valid=True, path="clip"))

        assert job.ball_mark_px == (pytest.approx(367.5), pytest.approx(263.5))
        archive = {
            "frames": np.zeros((4, 4, 4), dtype=np.uint8),
            "host_timestamp_ns": np.arange(4, dtype=np.int64),
            "trigger_host_timestamp_ns": np.int64(2),
        }
        kwargs = server_module._lowlight_worker_kwargs(
            job, archive, SimpleNamespace(relaxed_only=True)
        )
        assert kwargs["ball_mark_px"] == job.ball_mark_px
        assert server_module._camera_ball_mark_px() == job.ball_mark_px


class TestRestore:
    def _saved(self, geometry):
        target = target_line_from_tap(
            55.0,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            horizontal_pixel_sign=1.0,
            intrinsics=INTRINSICS,
            geometry=geometry,
            now=1_000.0,
        )
        save_camera_marks(resolve_target_line_path(), target, None)
        return target

    def test_a_target_of_this_setup_survives_a_restart(self, camera):
        saved = self._saved(server_module._camera_target_geometry())

        server_module.restore_camera_marks()

        assert server_module.angle_reference.target_line.to_dict() == saved.to_dict()
        assert server_module._camera_capture_settings_payload()["target_line"]["set_at"] == 1_000.0

    def test_a_target_of_another_roll_is_discarded(self, camera):
        self._saved({**server_module._camera_target_geometry(), "roll_correction_deg": 1.35})

        server_module.restore_camera_marks()

        assert server_module.angle_reference.target_line is None
        assert not resolve_target_line_path().exists()

    def test_without_a_camera_nothing_is_read(self, monkeypatch):
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        resolve_target_line_path().write_text("not json")

        server_module.restore_camera_marks()

        assert resolve_target_line_path().exists()
        assert server_module.angle_reference.target_line is None

    def _ball(self, geometry):
        return ball_mark_from_tap(
            57.5, 66.0, width=640, height=400, roll_correction_deg=0.0, geometry=geometry, now=900.0
        )

    def test_both_marks_of_this_setup_survive_a_restart(self, camera):
        geometry = server_module._camera_target_geometry()
        target = self._saved(geometry)
        ball = self._ball(geometry)
        save_camera_marks(resolve_target_line_path(), target, ball)

        server_module.restore_camera_marks()

        assert server_module.angle_reference.target_line.to_dict() == target.to_dict()
        assert server_module.angle_reference.ball_mark.to_dict() == ball.to_dict()
        assert server_module._camera_capture_settings_payload()["ball_mark"]["set_at"] == 900.0

    def test_a_ball_survives_a_restart_without_a_lens_calibration(self, camera, monkeypatch):
        monkeypatch.setattr(server_module, "camera_intrinsics", None)
        ball = self._ball(server_module._camera_target_geometry())
        save_camera_marks(resolve_target_line_path(), None, ball)

        server_module.restore_camera_marks()

        assert server_module.angle_reference.ball_mark.to_dict() == ball.to_dict()
        assert server_module.angle_reference.target_line is None

    def test_a_ball_of_an_unmapped_mode_is_discarded(self, camera):
        geometry = {**server_module._camera_target_geometry(), "width": 320, "height": 200}
        save_camera_marks(resolve_target_line_path(), None, self._ball(geometry))

        server_module.restore_camera_marks()

        assert server_module.angle_reference.ball_mark is None
        assert not resolve_target_line_path().exists()


class TestModeSwitch:
    """2026-11-02: four mode switches cleared both marks; now they carry over (T3)."""

    def _marks(self, ball_y_pct=66.0):
        server_module.handle_set_camera_capture_settings(
            {"ball_tap": {"x_pct": 57.5, "y_pct": ball_y_pct}}
        )
        server_module.handle_set_camera_capture_settings(
            {"target_tap": {"x_pct": 62.5, "y_pct": 40.0}}
        )
        return server_module.angle_reference.target_line, server_module.angle_reference.ball_mark

    def test_full_to_strip_and_back_keeps_the_direction_and_the_ball(self, camera):
        target, ball = self._marks()

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        strip_target = server_module.angle_reference.target_line
        strip_ball = server_module.angle_reference.ball_mark
        assert (strip_target.azimuth_deg, strip_target.elevation_deg) == (
            target.azimuth_deg,
            target.elevation_deg,
        )
        assert strip_target.frame_x_px == pytest.approx(target.frame_x_px, abs=1e-6)
        assert strip_target.frame_y_px == pytest.approx(target.frame_y_px - 100.0, abs=1e-6)
        assert strip_target.set_at == target.set_at
        assert strip_ball.frame_px == (pytest.approx(367.5), pytest.approx(163.5))
        assert strip_ball.preview_y_pct == pytest.approx((163.5 + 0.5) / 200.0 * 100.0)
        assert strip_ball.geometry["height"] == 200
        payload = _last_settings(camera)
        assert payload["ball_mark"]["frame_y_px"] == 163.5
        assert payload["target_line"]["azimuth_deg"] == pytest.approx(target.azimuth_deg, 1e-3)

        server_module.handle_set_camera_capture_settings({"capture_mode": "full-300"})

        back_target = server_module.angle_reference.target_line
        back_ball = server_module.angle_reference.ball_mark
        assert back_target.azimuth_deg == target.azimuth_deg
        assert back_target.elevation_deg == target.elevation_deg
        assert back_target.frame_px == (
            pytest.approx(target.frame_x_px, abs=1e-6),
            pytest.approx(target.frame_y_px, abs=1e-6),
        )
        assert back_ball.to_dict() == ball.to_dict()

    def test_the_club_job_anchors_on_the_ball_carried_into_the_strip(
        self, camera, monkeypatch, tmp_path
    ):
        """2026-11-02 S1 #6-#8: 640x200 irons without a departed ball use the carried mark."""
        from openflight.camera import ball_flight
        from openflight.camera.ball_flight import CameraBallEstimate

        self._marks()
        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
        np.savez(
            tmp_path / "frames.npz",
            frames=np.zeros((8, 4, 4), dtype=np.uint8),
            host_timestamp_ns=np.arange(8, dtype=np.int64),
            trigger_host_timestamp_ns=np.int64(3),
        )
        monkeypatch.setattr(
            ball_flight,
            "estimate_camera_ball_flight",
            lambda *_args, **_kwargs: CameraBallEstimate(status="rejected_test"),
        )
        monkeypatch.setattr(
            server_module,
            "iwr6843_runtime",
            SimpleNamespace(
                calibration=SimpleNamespace(
                    tee_range_m=1.5, radar_height_m=0.132, tee_ball_height_m=0.02135
                )
            ),
        )
        shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_8)

        job = server_module._fuse_camera_ball_flight(
            shot, SimpleNamespace(valid=True, path=tmp_path)
        )

        assert job.departed_ball is None
        assert (job.reference_ball.x, job.reference_ball.y) == (
            pytest.approx(367.5),
            pytest.approx(163.5),
        )

    def test_strip_500_to_strip_560_to_full_keeps_the_marks(self, camera):
        """strip-560 is the 640x200 strip at a higher rate: same window, same marks."""
        target, ball = self._marks()
        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
        strip_target = server_module.angle_reference.target_line
        strip_ball = server_module.angle_reference.ball_mark
        entries = len(camera.log.angle_entries)

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-560"})

        assert server_module.angle_reference.ball_mark.to_dict() == strip_ball.to_dict()
        assert server_module.angle_reference.target_line.to_dict() == strip_target.to_dict()
        assert server_module.angle_reference.ball_mark.frame_px == (
            pytest.approx(367.5),
            pytest.approx(163.5),
        )
        events = [entry["event"] for entry in camera.log.angle_entries[entries:]]
        assert "camera_geometry_changed" not in events
        assert BALL_MARK_OUTSIDE_MODE not in events
        assert _last_settings(camera)["ball_mark"]["frame_y_px"] == 163.5

        server_module.handle_set_camera_capture_settings({"capture_mode": "full-300"})

        back_target = server_module.angle_reference.target_line
        assert (back_target.azimuth_deg, back_target.elevation_deg) == (
            target.azimuth_deg,
            target.elevation_deg,
        )
        assert back_target.frame_px == (
            pytest.approx(target.frame_x_px, abs=1e-6),
            pytest.approx(target.frame_y_px, abs=1e-6),
        )
        assert server_module.angle_reference.ball_mark.to_dict() == ball.to_dict()

    def test_strip_560_drops_a_ball_outside_the_strip_like_strip_500(self, camera):
        target, _ball = self._marks(ball_y_pct=95.0)

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-560"})

        assert server_module.angle_reference.ball_mark is None
        assert server_module.angle_reference.target_line.azimuth_deg == target.azimuth_deg
        events = [entry["event"] for entry in camera.log.angle_entries]
        assert events[-2:] == [BALL_MARK_OUTSIDE_MODE, "camera_geometry_changed"]

    def test_the_marks_are_saved_in_the_full_frame(self, camera):
        target, ball = self._marks()

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        saved = json.loads(resolve_target_line_path().read_text())
        assert saved["ball_mark"]["geometry"]["height"] == 400
        assert saved["ball_mark"]["frame_y_px"] == pytest.approx(ball.frame_y_px)
        assert saved["target_line"]["frame_y_px"] == pytest.approx(target.frame_y_px, abs=1e-3)
        assert saved["target_line"]["geometry"] == canonical_geometry(target.geometry)

    def test_the_switch_restarts_the_offsets_and_logs_it(self, camera, monkeypatch):
        monkeypatch.setattr(server_module, "_shot_finalization_order", [])
        monkeypatch.setattr(server_module, "_shot_finalization_running", False)
        self._marks()
        _learn_offsets()

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        assert not server_module.angle_reference.vertical.valid
        events = [entry["event"] for entry in camera.log.angle_entries]
        assert events[-2:] == ["camera_geometry_changed", "offset_reset"]
        assert camera.log.angle_entries[-2]["ball_mark"]["frame_y_px"] == 163.5

    def test_a_ball_outside_the_strip_is_dropped_with_its_event(self, camera):
        target, _ball = self._marks(ball_y_pct=95.0)  # frame row 379.5, strip row 279.5

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        assert server_module.angle_reference.ball_mark is None
        assert server_module.angle_reference.target_line.azimuth_deg == target.azimuth_deg
        events = [entry["event"] for entry in camera.log.angle_entries]
        assert events[-2:] == [BALL_MARK_OUTSIDE_MODE, "camera_geometry_changed"]
        assert _last_settings(camera)["ball_mark"] is None
        saved = json.loads(resolve_target_line_path().read_text())
        assert saved["ball_mark"] is None and saved["target_line"] is not None

    def test_the_strip_offset_moves_the_marks(self, camera):
        target, _ball = self._marks()
        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        server_module.handle_set_camera_capture_settings({"vertical_offset_px": 20})

        assert server_module.angle_reference.ball_mark.frame_px == (
            pytest.approx(367.5),
            pytest.approx(143.5),
        )
        moved = server_module.angle_reference.target_line
        assert moved.frame_y_px == pytest.approx(target.frame_y_px - 120.0, abs=1e-6)
        assert moved.azimuth_deg == target.azimuth_deg

    def test_a_restart_in_the_strip_restores_the_full_frame_marks(self, camera):
        target, ball = self._marks()
        server_module.angle_reference.target_line = None
        server_module.angle_reference.ball_mark = None
        camera.runtime.update_capture_mode("strip-500")
        server_module.camera_capture_config.update(width=640, height=200)

        server_module.restore_camera_marks()

        restored = server_module.angle_reference.ball_mark
        assert restored.frame_px == (pytest.approx(ball.frame_x_px), pytest.approx(163.5))
        restored_target = server_module.angle_reference.target_line
        # The file keeps the direction to 1e-3 deg, about 0.01 px.
        assert restored_target.azimuth_deg == pytest.approx(target.azimuth_deg, abs=1e-3)
        assert restored_target.frame_x_px == pytest.approx(target.frame_x_px, abs=0.05)
        assert restored_target.frame_y_px == pytest.approx(target.frame_y_px - 100.0, abs=0.05)

    def test_the_settings_show_the_hitting_zone_of_the_new_mode(self, camera):
        zones = {200: {"y1": 200}, 400: {"y1": 390}}
        camera.runtime.active_tee_zone = lambda: SimpleNamespace(
            to_dict=lambda: zones[camera.runtime.height]
        )

        server_module.handle_set_camera_capture_settings({"capture_mode": "strip-500"})

        assert _last_settings(camera)["tee_zone"] == {"y1": 200}

    def test_the_strip_ball_mark_of_2026_11_02_restores_in_the_full_frame(self, camera):
        """The Pi saved the strip ball mark (350.9, 165.7) in 640x200 at 13:11:18."""
        strip_geometry = {**server_module._camera_target_geometry(), "height": 200}
        ball = ball_mark_from_tap(
            (350.9 + 0.5) / 640.0 * 100.0,
            (165.7 + 0.5) / 200.0 * 100.0,
            width=640,
            height=200,
            roll_correction_deg=0.0,
            geometry=strip_geometry,
            now=1_000.0,
        )
        save_camera_marks(resolve_target_line_path(), None, ball)

        server_module.restore_camera_marks()

        restored = server_module.angle_reference.ball_mark
        assert restored.frame_px == (pytest.approx(350.9), pytest.approx(265.7))
        # The 640x400 ball of the same session sat at (361, 274).
        assert abs(restored.frame_x_px - 361.0) <= 12.0 and abs(restored.frame_y_px - 274.0) <= 12.0


class TestCaptureIntrinsics:
    def _capture(self, **settings):
        base = {
            "width": 640,
            "height": 400,
            "rotate_180": False,
            "mirror_horizontal": False,
            "scaler_crop": None,
        }
        return SimpleNamespace(metadata={"settings": {**base, **settings}})

    def test_a_strip_clip_gets_the_lens_model_of_its_mode(self, camera, caplog):
        with caplog.at_level("WARNING"):
            intrinsics = server_module._capture_intrinsics(self._capture(height=200))

        assert intrinsics.cy == pytest.approx(INTRINSICS.cy - 100.0)
        assert (intrinsics.fx, intrinsics.fy, intrinsics.cx) == (
            INTRINSICS.fx,
            INTRINSICS.fy,
            INTRINSICS.cx,
        )
        assert "Camera intrinsics not applied" not in caplog.text

    def test_the_clip_sensor_window_moves_the_principal_point(self, camera):
        intrinsics = server_module._capture_intrinsics(
            self._capture(height=200, vertical_offset_px=-30)
        )

        assert intrinsics.cy == pytest.approx(INTRINSICS.cy - 70.0)

    def test_the_full_frame_clip_keeps_the_calibration(self, camera):
        assert server_module._capture_intrinsics(self._capture()) is INTRINSICS

    @pytest.mark.parametrize(
        ("settings", "reason"),
        [({"width": 320, "height": 200}, "width"), ({"rotate_180": True}, "rotate_180")],
    )
    def test_an_unmapped_or_flipped_clip_gets_none(self, camera, caplog, settings, reason):
        with caplog.at_level("WARNING"):
            assert server_module._capture_intrinsics(self._capture(**settings)) is None

        assert "Camera intrinsics not applied" in caplog.text and reason in caplog.text


class TestResets:
    @pytest.fixture(name="store")
    def fixture_store(self, tmp_path, monkeypatch):
        from openflight.profiles import ProfileStore

        store = ProfileStore(tmp_path / "profiles.json")
        monkeypatch.setattr(server_module, "profile_store", store)
        monkeypatch.setattr(server_module, "monitor", None)
        return store

    def test_a_profile_switch_starts_the_offsets_afresh(self, camera, store):
        first = store.get_active()
        store.add("Second")
        _learn_offsets()

        server_module.handle_set_active_profile({"profile_id": first.id})

        assert not server_module.angle_reference.vertical.valid
        assert camera.log.angle_entries[-1]["reason"] == "profile_switch"

    def test_choosing_the_active_profile_again_keeps_them(self, camera, store):
        _learn_offsets()

        server_module.handle_set_active_profile({"profile_id": store.get_active().id})

        assert server_module.angle_reference.vertical.valid

    def test_a_new_profile_starts_them_afresh(self, camera, store):
        _learn_offsets()

        server_module.handle_add_profile({"name": "Guest"})

        assert not server_module.angle_reference.horizontal.valid

    def test_clearing_the_session_starts_them_afresh(self, camera, store):
        _learn_offsets()

        server_module.handle_clear_session()

        assert not server_module.angle_reference.vertical.valid
        assert camera.log.angle_entries[-1]["reason"] == "session_cleared"

    def test_the_session_start_records_the_reference(self, camera):
        _learn_offsets()

        config = server_module._session_start_config()

        assert config["angle_reference"]["vertical"]["valid"] is True
        assert config["angle_reference"]["target_line"] is None


class TestShotPipeline:
    def test_the_plausible_ti_vertical_is_kept_uncorrected(self):
        measurement = SimpleNamespace(
            angle_deg=17.42, n_snapshots=12, n_frames=4, component_std_deg=0.8, single_channel=False
        )
        shot = Shot(
            ball_speed_mph=100.0,
            club_speed_mph=80.0,
            timestamp=datetime.now(),
            club=ClubType.IRON_9,
        )

        server_module._apply_iwr6843_accepted_measurement(shot, measurement)

        assert shot.iwr6843_vertical_deg == 17.42
        assert shot.launch_angle_vertical == 17.42

    def test_an_implausible_ti_vertical_is_not_kept(self):
        measurement = SimpleNamespace(
            angle_deg=-0.47, n_snapshots=12, n_frames=4, component_std_deg=0.8, single_channel=False
        )
        shot = Shot(
            ball_speed_mph=140.0,
            club_speed_mph=95.0,
            timestamp=datetime.now(),
            club=ClubType.WOOD_7,
        )

        server_module._apply_iwr6843_accepted_measurement(shot, measurement)

        assert shot.iwr6843_vertical_deg is None
        assert shot.launch_angle_vertical_rejection_reason == "ti_launch_implausible"

    def _fuse(self, monkeypatch, tmp_path, estimate):
        from openflight.camera import ball_flight

        calls = {}

        def fake_estimate(*_args, **kwargs):
            calls.update(kwargs)
            return estimate

        np.savez(
            tmp_path / "frames.npz",
            frames=np.zeros((8, 4, 4), dtype=np.uint8),
            host_timestamp_ns=np.arange(8, dtype=np.int64),
            trigger_host_timestamp_ns=np.int64(3),
        )
        monkeypatch.setattr(ball_flight, "estimate_camera_ball_flight", fake_estimate)
        monkeypatch.setattr(
            server_module,
            "iwr6843_runtime",
            SimpleNamespace(
                calibration=SimpleNamespace(
                    tee_range_m=1.5, radar_height_m=0.132, tee_ball_height_m=0.02135
                )
            ),
        )
        monkeypatch.setattr(
            server_module,
            "camera_capture_config",
            {"mount_height_m": 0.182, "width": 640, "height": 400},
        )
        shot = Shot(
            ball_speed_mph=110.0,
            timestamp=datetime.now(),
            launch_angle_vertical=20.0,
            launch_angle_vertical_source="radar",
            iwr6843_vertical_deg=20.0,
            iwr6843_ball_range_evidence=object(),
        )
        server_module._fuse_camera_ball_flight(shot, SimpleNamespace(valid=True, path=tmp_path))
        return shot, calls

    def test_an_accepted_camera_vertical_is_kept_for_the_reference(self, monkeypatch, tmp_path):
        from openflight.camera.ball_flight import CameraBallEstimate

        shot, calls = self._fuse(
            monkeypatch,
            tmp_path,
            CameraBallEstimate(
                status="accepted",
                confidence_tier="high",
                horizontal_deg=0.6,
                vertical_deg=18.2,
                depth_source="iwr_range",
                support=20,
            ),
        )

        assert shot.experimental_camera_vertical_deg == 18.2
        assert shot.experimental_camera_vertical_confidence == 0.75
        assert calls["geometry"].target_px is None
        # Stamped: this ball flight was measured without a target.
        assert shot.angle_reference == {"measured_against": None}
        # The fusion itself leaves the vertical to the reference step.
        assert shot.launch_angle_vertical == 20.0

    def test_a_withheld_direction_keeps_no_camera_vertical(self, monkeypatch, tmp_path):
        from openflight.camera.ball_flight import CameraBallEstimate

        shot, _calls = self._fuse(
            monkeypatch,
            tmp_path,
            CameraBallEstimate(
                status="rejected_line_fit_residual",
                confidence_tier="withheld",
                vertical_deg=18.2,
                support=20,
            ),
        )

        assert shot.experimental_camera_vertical_deg is None

    def test_the_tapped_target_goes_into_the_geometry_and_the_shot(self, monkeypatch, tmp_path):
        from openflight.camera.ball_flight import CameraBallEstimate

        server_module.angle_reference.target_line = target_line_from_tap(
            55.0,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            horizontal_pixel_sign=1.0,
            intrinsics=INTRINSICS,
            geometry={},
        )
        shot, calls = self._fuse(
            monkeypatch,
            tmp_path,
            CameraBallEstimate(
                status="accepted",
                confidence_tier="experimental",
                horizontal_deg=-1.2,
                vertical_deg=15.0,
                depth_source="iwr_range",
                support=5,
                target_azimuth_deg=2.3456,
            ),
        )

        target = server_module.angle_reference.target_line
        assert calls["geometry"].target_px == target.frame_px
        assert shot.angle_reference == {
            "measured_against": target.to_log(),
            "target_line": {"shot_azimuth_deg": 2.346},
        }
        assert shot.experimental_camera_vertical_confidence == 0.45

    def test_the_reference_step_selects_the_camera_and_logs_a_validity_change(self, monkeypatch):
        log = FakeSessionLog()
        monkeypatch.setattr(server_module, "get_session_logger", lambda: log)
        for _ in range(MIN_PAIRS - 1):
            server_module._apply_angle_reference(_camera_shot())
        assert log.angle_entries == []

        shot = _camera_shot()
        server_module._apply_angle_reference(shot)

        assert shot.launch_angle_vertical == 18.0
        assert shot.launch_angle_vertical_source == "camera"
        assert [entry["event"] for entry in log.angle_entries] == ["offset_validity_changed"]
        assert log.angle_entries[0]["vertical"]["offset_deg"] == 2.0
        entry = shot.to_dict()
        assert entry["iwr6843_vertical_deg"] == 20.0
        assert entry["experimental_camera_vertical_deg"] == 18.0
        assert entry["angle_reference"]["vertical"]["offset"]["valid"] is True
        assert json.loads(json.dumps(server_module.shot_to_dict(shot)))["angle_reference"]

    def test_a_failing_reference_never_costs_the_shot(self, monkeypatch):
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        monkeypatch.setattr(
            server_module.angle_reference,
            "apply",
            lambda _shot, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        shot = _camera_shot()

        server_module._apply_angle_reference(shot)

        assert shot.launch_angle_vertical == 20.0

    def test_a_calibrated_vertical_must_pass_the_ti_guard(self, monkeypatch):
        # 2026-10-25 OF33: a topped driver read +0.08 deg on the TI; the session
        # offset of +3.00 deg would show -2.92 deg, outside the 0-45 deg bounds.
        monkeypatch.setattr(server_module, "get_session_logger", lambda: None)
        for _ in range(MIN_PAIRS):
            server_module._apply_angle_reference(_camera_shot(camera_vertical=17.0))
        shot = Shot(
            ball_speed_mph=135.4,
            club_speed_mph=104.9,
            club=ClubType.DRIVER,
            timestamp=datetime.now(),
            launch_angle_vertical=0.08,
            launch_angle_vertical_source="radar",
            iwr6843_vertical_deg=0.08,
        )
        assert server_module._ti_vertical_launch_is_plausible(0.08, shot)[0]

        server_module._apply_angle_reference(shot)

        vertical = shot.angle_reference["vertical"]
        assert shot.launch_angle_vertical == 0.08
        assert shot.launch_angle_vertical_source == "radar"
        assert vertical["corrected_deg"] == pytest.approx(-2.92)
        assert vertical["calibration_rejected"] == {
            "reason": "calibrated_implausible",
            "rule": "bounds_0_45",
        }

    def test_enrichment_applies_the_reference_after_the_camera(self, monkeypatch):
        order = []
        monkeypatch.setattr(server_module, "_snapshot_inclinometer_for_shot", lambda _shot: None)
        monkeypatch.setattr(server_module, "_process_iwr6843_angle", lambda _shot: None)
        monkeypatch.setattr(server_module, "camera_capture_runtime", None)
        monkeypatch.setattr(server_module, "kld7_vertical", None)
        monkeypatch.setattr(server_module, "kld7_horizontal", None)
        monkeypatch.setattr(
            server_module,
            "_fuse_camera_measurements",
            lambda *_args, **_kwargs: order.append("camera"),
        )
        monkeypatch.setattr(
            server_module, "_apply_angle_reference", lambda _shot: order.append("reference")
        )
        monkeypatch.setattr(
            server_module, "_apply_dplane_spin_axis", lambda _shot: order.append("dplane")
        )
        shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), mode="rolling-buffer")

        server_module._enrich_shot_from_optional_hardware(shot)

        assert order == ["camera", "reference", "dplane"]


def test_session_logger_writes_the_angle_reference_entry(tmp_path):
    from openflight.session_logger import SessionLogger

    session = SessionLogger(log_dir=tmp_path, location="test")
    session.start_session(radar_port="mock", config={})
    session.log_angle_reference("target_set", server_module.angle_reference.state(), reason=None)
    session.end_session()

    entries = [
        json.loads(line) for line in Path(session.session_path).read_text().splitlines() if line
    ]
    entry = next(item for item in entries if item["type"] == "angle_reference")
    assert entry["event"] == "target_set"
    assert entry["vertical"]["valid"] is False
    assert entry["target_line"] is None
