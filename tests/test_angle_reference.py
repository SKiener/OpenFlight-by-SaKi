"""The camera as the launch-angle reference: session offsets and the tapped target line."""

import json
import math
from dataclasses import replace
from datetime import datetime

import numpy as np
import pytest

from openflight.angle_reference import (
    BALL_MARK_OUTSIDE_MODE,
    CAMERA_ONLY_VERTICAL_CONFIDENCE,
    MAX_SPREAD_DEG,
    MIN_PAIRS,
    WINDOW_PAIRS,
    AngleReference,
    BallMark,
    SessionOffset,
    TargetLine,
    ball_mark_from_tap,
    camera_vertical_confidence,
    canonical_geometry,
    carry_camera_marks,
    direction_to_frame_px,
    frame_px_to_preview_pct,
    load_camera_marks,
    preview_to_frame_px,
    resolve_target_line_path,
    save_camera_marks,
    target_line_from_tap,
)
from openflight.camera.geometry import normalized_image_offsets
from openflight.camera.intrinsics import CameraIntrinsics, for_mode
from openflight.launch_monitor import Shot

# The 2026-10-25 range lens (terminal log): fx 575.7, fy 577.3, cx 347.8, cy 246.9.
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
GEOMETRY = {"width": 640, "height": 400, "roll_correction_deg": 0.0, "scaler_crop": (0, 0, 1, 1)}


def _offset(differences) -> SessionOffset:
    offset = SessionOffset("vertical")
    for difference in differences:
        offset.add(10.0 + difference, 10.0)
    return offset


class TestSessionOffset:
    def test_needs_min_pairs_before_it_is_valid(self):
        offset = _offset([2.0] * (MIN_PAIRS - 1))
        assert not offset.valid
        assert offset.correct(15.0) is None
        assert offset.add(12.0, 10.0) is True  # the fifth pair makes it valid
        assert offset.valid
        assert offset.correct(15.0) == pytest.approx(13.0)

    def test_is_the_median_of_radar_minus_camera(self):
        offset = _offset([1.8, 2.0, 2.2, 2.4, 9.0])
        assert offset.offset_deg == pytest.approx(2.2)
        assert offset.valid  # one outlier moves the median one rank, not the spread gate

    def test_a_wide_spread_is_not_valid(self):
        offset = _offset([0.0, 1.0, 2.0, 3.0, 4.0])
        assert offset.spread_deg > MAX_SPREAD_DEG
        assert not offset.valid
        assert offset.correct(15.0) is None

    def test_spread_is_the_scaled_median_absolute_deviation(self):
        offset = _offset([1.0, 1.5, 2.0, 2.5, 3.0])
        assert offset.spread_deg == pytest.approx(1.4826 * 0.5)

    def test_runs_over_the_last_window_pairs(self):
        offset = _offset([5.0] * WINDOW_PAIRS)
        for _ in range(WINDOW_PAIRS // 2 + 1):
            offset.add(12.0, 10.0)
        assert offset.offset_deg == pytest.approx(2.0)
        state = offset.state()
        assert state["pairs"] == WINDOW_PAIRS
        assert state["pairs_total"] == WINDOW_PAIRS + WINDOW_PAIRS // 2 + 1

    def test_reset_forgets_every_pair_and_names_why(self):
        offset = _offset([2.0] * MIN_PAIRS)
        assert offset.reset("profile_switch") is True
        assert not offset.valid
        assert offset.state()["reset_reason"] == "profile_switch"
        assert offset.state()["offset_deg"] is None
        assert offset.reset("again") is False

    def test_state_is_json_ready(self):
        state = _offset([2.0] * MIN_PAIRS).state()
        assert json.loads(json.dumps(state)) == state
        assert state["valid"] is True
        assert state["offset_deg"] == 2.0
        assert state["min_pairs"] == MIN_PAIRS
        assert state["max_spread_deg"] == MAX_SPREAD_DEG
        assert state["window"] == WINDOW_PAIRS


class TestPreviewToFramePx:
    def test_without_roll_is_the_pixel_under_the_tap(self):
        assert preview_to_frame_px(50.0, 25.0, width=640, height=400, roll_correction_deg=0.0) == (
            pytest.approx(319.5),
            pytest.approx(99.5),
        )

    @pytest.mark.parametrize("roll", [1.35, -2.0])
    def test_undoes_the_preview_deroll(self, roll):
        cv2 = pytest.importorskip("cv2")
        frame = np.zeros((400, 640), dtype=np.uint8)
        frame[100, 500] = 255
        transform = cv2.getRotationMatrix2D((320.0, 200.0), -roll, 1.0)
        preview = cv2.warpAffine(frame, transform, (640, 400), flags=cv2.INTER_LINEAR)
        rows, columns = np.nonzero(preview)
        weights = preview[rows, columns].astype(float)
        x_px = float((columns * weights).sum() / weights.sum())
        y_px = float((rows * weights).sum() / weights.sum())

        frame_x, frame_y = preview_to_frame_px(
            100.0 * (x_px + 0.5) / 640,
            100.0 * (y_px + 0.5) / 400,
            width=640,
            height=400,
            roll_correction_deg=roll,
        )

        assert frame_x == pytest.approx(500.0, abs=0.05)
        assert frame_y == pytest.approx(100.0, abs=0.05)


class TestTargetLineFromTap:
    def _tap(self, x_pct, y_pct, **overrides):
        values = {
            "width": 640,
            "height": 400,
            "roll_correction_deg": 0.0,
            "horizontal_pixel_sign": 1.0,
            "intrinsics": INTRINSICS,
            "geometry": GEOMETRY,
            "now": 1_000.0,
        }
        values.update(overrides)
        return target_line_from_tap(x_pct, y_pct, **values)

    def test_the_principal_point_is_straight_ahead(self):
        target = self._tap(100.0 * (INTRINSICS.cx + 0.5) / 640, 100.0 * (INTRINSICS.cy + 0.5) / 400)
        assert target.azimuth_deg == pytest.approx(0.0, abs=1e-6)
        assert target.elevation_deg == pytest.approx(0.0, abs=1e-6)

    def test_right_of_the_principal_point_is_target_right_and_undistorted(self):
        x_px = INTRINSICS.cx + 150.0
        target = self._tap(100.0 * (x_px + 0.5) / 640, 100.0 * (INTRINSICS.cy + 0.5) / 400)
        undistorted_x, _ = INTRINSICS.undistort(x_px, INTRINSICS.cy)
        assert target.azimuth_deg == pytest.approx(math.degrees(math.atan(undistorted_x)))
        # Barrel distortion: the undistorted direction lies farther out than the pinhole one.
        assert target.azimuth_deg > math.degrees(math.atan(150.0 / INTRINSICS.fx))

    def test_a_mirrored_capture_flips_the_side(self):
        x_pct = 100.0 * (INTRINSICS.cx + 150.5) / 640
        y_pct = 100.0 * (INTRINSICS.cy + 0.5) / 400
        plain = self._tap(x_pct, y_pct)
        mirrored = self._tap(x_pct, y_pct, horizontal_pixel_sign=-1.0)
        assert mirrored.azimuth_deg == pytest.approx(-plain.azimuth_deg)

    def test_applies_roll_in_normalized_coordinates(self):
        target = self._tap(80.0, 30.0, roll_correction_deg=1.35)
        frame_x, frame_y = preview_to_frame_px(
            80.0, 30.0, width=640, height=400, roll_correction_deg=1.35
        )
        image_x, image_z = normalized_image_offsets(
            frame_x,
            frame_y,
            focal_px=INTRINSICS.focal_px,
            image_width_px=640,
            image_height_px=400,
            horizontal_pixel_sign=1.0,
            roll_correction_deg=1.35,
            intrinsics=INTRINSICS,
        )
        assert target.frame_px == (pytest.approx(frame_x), pytest.approx(frame_y))
        assert target.azimuth_deg == pytest.approx(math.degrees(math.atan(image_x)))
        assert target.elevation_deg == pytest.approx(
            math.degrees(math.atan2(image_z, math.hypot(image_x, 1.0)))
        )

    def test_keeps_the_tap_the_time_and_a_json_geometry_key(self):
        target = self._tap(62.5, 40.0)
        assert (target.preview_x_pct, target.preview_y_pct) == (62.5, 40.0)
        assert target.set_at == 1_000.0
        assert target.geometry["scaler_crop"] == [0, 0, 1, 1]

    @pytest.mark.parametrize(
        ("x_pct", "y_pct"),
        [(-1.0, 50.0), (50.0, 100.5), (float("nan"), 50.0), (True, 50.0), ("50", 50.0), (None, 5)],
    )
    def test_rejects_a_position_off_the_picture(self, x_pct, y_pct):
        with pytest.raises(ValueError, match="target"):
            self._tap(x_pct, y_pct)


class TestBallMarkFromTap:
    def _tap(self, x_pct, y_pct, roll=0.0):
        return ball_mark_from_tap(
            x_pct,
            y_pct,
            width=640,
            height=400,
            roll_correction_deg=roll,
            geometry=GEOMETRY,
            now=1_000.0,
        )

    def test_is_the_saved_frame_pixel_under_the_tap(self):
        ball = self._tap(57.5, 66.0)

        assert ball.frame_px == (pytest.approx(367.5), pytest.approx(263.5))
        assert (ball.preview_x_pct, ball.preview_y_pct, ball.set_at) == (57.5, 66.0, 1_000.0)
        assert ball.geometry["scaler_crop"] == [0, 0, 1, 1]

    def test_undoes_the_preview_deroll_like_the_target(self):
        ball = self._tap(57.5, 66.0, roll=1.35)

        assert ball.frame_px == pytest.approx(
            preview_to_frame_px(57.5, 66.0, width=640, height=400, roll_correction_deg=1.35)
        )

    def test_the_log_form_has_no_geometry_and_is_json_ready(self):
        log = self._tap(57.5, 66.0).to_log()

        assert "geometry" not in log
        assert json.loads(json.dumps(log)) == log
        assert log["frame_x_px"] == 367.5

    @pytest.mark.parametrize(
        ("x_pct", "y_pct"),
        [(-1.0, 50.0), (50.0, 100.5), (float("inf"), 50.0), (False, 50.0), ("50", 50.0)],
    )
    def test_rejects_a_position_off_the_picture(self, x_pct, y_pct):
        with pytest.raises(ValueError, match="ball"):
            self._tap(x_pct, y_pct)


class TestTargetLinePersistence:
    def _target(self, geometry=None) -> TargetLine:
        return target_line_from_tap(
            62.5,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            horizontal_pixel_sign=1.0,
            intrinsics=INTRINSICS,
            geometry=geometry or GEOMETRY,
            now=1_000.0,
        )

    def _ball(self, geometry=None) -> BallMark:
        return ball_mark_from_tap(
            50.0,
            66.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            geometry=geometry or GEOMETRY,
            now=900.0,
        )

    def test_round_trips_for_the_same_geometry(self, tmp_path):
        path = tmp_path / "nested" / "target.json"
        target = self._target()
        assert save_camera_marks(path, target, None) is True

        loaded, ball, reason = load_camera_marks(path, GEOMETRY)

        assert reason is None
        assert ball is None
        assert loaded.to_dict() == target.to_dict()
        assert not path.with_suffix(".json.tmp").exists()

    def test_both_marks_round_trip_in_one_version_2_file(self, tmp_path):
        path = tmp_path / "target.json"
        target, ball = self._target(), self._ball()
        save_camera_marks(path, target, ball)

        loaded_target, loaded_ball, reason = load_camera_marks(path, GEOMETRY)

        assert json.loads(path.read_text())["version"] == 2
        assert reason is None
        assert loaded_target.to_dict() == target.to_dict()
        assert loaded_ball.to_dict() == ball.to_dict()

    def test_a_ball_mark_alone_is_kept_with_no_target(self, tmp_path):
        path = tmp_path / "target.json"
        save_camera_marks(path, None, self._ball())

        target, ball, reason = load_camera_marks(path, GEOMETRY)

        assert (target, reason) == (None, None)
        assert ball.frame_px == (pytest.approx(319.5), pytest.approx(263.5))
        assert json.loads(path.read_text())["target_line"] is None

    def test_no_mark_deletes_the_file(self, tmp_path):
        path = tmp_path / "target.json"
        save_camera_marks(path, self._target(), self._ball())

        assert save_camera_marks(path, None, None) is True

        assert not path.exists()

    def test_the_version_1_file_of_an_older_build_still_loads(self, tmp_path):
        path = tmp_path / "target.json"
        target = self._target()
        path.write_text(json.dumps({"version": 1, **target.to_dict()}))

        loaded, ball, reason = load_camera_marks(path, GEOMETRY)

        assert reason is None
        assert ball is None
        assert loaded.to_dict() == target.to_dict()

    def test_no_saved_target_is_no_reason(self, tmp_path):
        assert load_camera_marks(tmp_path / "missing.json", GEOMETRY) == (None, None, None)

    @pytest.mark.parametrize("which", ["target", "ball"])
    def test_a_mark_of_another_geometry_discards_both(self, tmp_path, which):
        path = tmp_path / "target.json"
        other = {**GEOMETRY, "roll_correction_deg": 1.35}
        save_camera_marks(
            path,
            self._target(other if which == "target" else None),
            self._ball(other if which == "ball" else None),
        )

        target, ball, reason = load_camera_marks(path, GEOMETRY)

        assert (target, ball) == (None, None)
        assert reason == "camera geometry changed"
        assert not path.exists()

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            json.dumps({"version": 99}),
            json.dumps({"version": 1, "azimuth_deg": "x"}),
            json.dumps({"version": 2, "target_line": None, "ball_mark": [1, 2]}),
            json.dumps({"version": 2, "target_line": {"frame_x_px": 1.0}, "ball_mark": None}),
            json.dumps([1, 2]),
        ],
    )
    def test_a_damaged_file_is_discarded(self, tmp_path, content):
        path = tmp_path / "target.json"
        path.write_text(content)

        target, ball, reason = load_camera_marks(path, GEOMETRY)

        assert (target, ball) == (None, None)
        assert reason
        assert not path.exists()

    def test_a_non_finite_value_is_discarded(self, tmp_path):
        path = tmp_path / "target.json"
        payload = {"version": 1, **self._target().to_dict(), "azimuth_deg": float("inf")}
        path.write_text(json.dumps(payload))
        assert load_camera_marks(path, GEOMETRY)[0] is None
        ball = {**self._ball().to_dict(), "frame_y_px": float("nan")}
        path.write_text(json.dumps({"version": 2, "target_line": None, "ball_mark": ball}))
        assert load_camera_marks(path, GEOMETRY)[1] is None

    def test_the_path_follows_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENFLIGHT_CAMERA_TARGET_LINE_PATH", str(tmp_path / "t.json"))
        assert resolve_target_line_path() == tmp_path / "t.json"
        monkeypatch.delenv("OPENFLIGHT_CAMERA_TARGET_LINE_PATH")
        assert resolve_target_line_path().name == "camera-target-line.json"


class TestModeMapping:
    """T3: a capture-mode switch carries both marks (640x400 <-> 640x200 strip)."""

    MOUNT = {
        "rotate_180": False,
        "mirror_horizontal": False,
        "scaler_crop": None,
        "roll_correction_deg": 0.0,
        "mount_height_m": 0.182,
        "intrinsics": {"fx": 575.7},
    }
    FULL = {**MOUNT, "width": 640, "height": 400, "vertical_offset_px": 0}
    STRIP = {**MOUNT, "width": 640, "height": 200, "vertical_offset_px": 0}

    def _target(self, geometry=None, roll=0.0, sign=1.0, intrinsics=INTRINSICS):
        geometry = geometry or self.FULL
        return target_line_from_tap(
            62.5,
            40.0,
            width=geometry["width"],
            height=geometry["height"],
            roll_correction_deg=roll,
            horizontal_pixel_sign=sign,
            intrinsics=intrinsics,
            geometry=geometry,
            now=1_000.0,
        )

    def _ball(self, y_pct=66.0, geometry=None):
        geometry = geometry or self.FULL
        return ball_mark_from_tap(
            57.5,
            y_pct,
            width=geometry["width"],
            height=geometry["height"],
            roll_correction_deg=float(geometry["roll_correction_deg"]),
            geometry=geometry,
            now=900.0,
        )

    @pytest.mark.parametrize("roll", [0.0, 1.35, -2.0])
    def test_the_preview_position_inverts_the_tap_mapping(self, roll):
        frame = preview_to_frame_px(62.5, 33.0, width=640, height=200, roll_correction_deg=roll)

        assert frame_px_to_preview_pct(*frame, width=640, height=200, roll_correction_deg=roll) == (
            pytest.approx(62.5),
            pytest.approx(33.0),
        )

    @pytest.mark.parametrize(("roll", "sign"), [(0.0, 1.0), (1.35, 1.0), (-2.0, -1.0)])
    def test_a_direction_projects_back_onto_its_tapped_pixel(self, roll, sign):
        target = self._target(roll=roll, sign=sign)

        frame = direction_to_frame_px(
            target.azimuth_deg,
            target.elevation_deg,
            intrinsics=INTRINSICS,
            horizontal_pixel_sign=sign,
            roll_correction_deg=roll,
        )

        assert frame == (
            pytest.approx(target.frame_x_px, abs=1e-6),
            pytest.approx(target.frame_y_px, abs=1e-6),
        )

    def test_full_to_strip_and_back_keeps_the_target_and_the_ball(self):
        target, ball = self._target(), self._ball()
        strip_lens = for_mode(INTRINSICS, 640, 200, 0)

        strip = carry_camera_marks(target, ball, self.STRIP, strip_lens)
        back = carry_camera_marks(strip.target, strip.ball, self.FULL, INTRINSICS)

        assert strip.discarded is None and not strip.ball_outside
        assert strip.target.azimuth_deg == target.azimuth_deg
        assert strip.target.elevation_deg == target.elevation_deg
        assert strip.target.frame_y_px == pytest.approx(target.frame_y_px - 100.0, abs=1e-6)
        assert strip.target.frame_x_px == pytest.approx(target.frame_x_px, abs=1e-6)
        assert strip.target.geometry == self.STRIP
        assert strip.ball.frame_px == (pytest.approx(367.5), pytest.approx(163.5))
        assert strip.ball.preview_y_pct == pytest.approx(82.0)
        assert back.ball.to_dict() == ball.to_dict()
        assert back.target.to_dict() == target.to_dict()

    def test_the_strip_offset_moves_the_ball_with_the_window(self):
        lower = {**self.STRIP, "vertical_offset_px": 20}

        carried = carry_camera_marks(None, self._ball(), lower, None)

        assert carried.ball.frame_px == (pytest.approx(367.5), pytest.approx(143.5))

    def test_a_rotated_mount_moves_the_ball_and_target_by_100_minus_s(self):
        """Review: the 180-degree mount's strip at offset +30 sits 70 rows into the full frame."""
        rotated = {**self.MOUNT, "rotate_180": True}
        full = {**rotated, "width": 640, "height": 400, "vertical_offset_px": 0}
        lower = {**rotated, "width": 640, "height": 200, "vertical_offset_px": 30}
        lens = replace(INTRINSICS, rotate_180=True)
        target = self._target(geometry=full, intrinsics=lens)

        carried = carry_camera_marks(
            target, self._ball(geometry=full), lower, for_mode(lens, 640, 200, 30)
        )

        assert carried.ball.frame_px == (pytest.approx(367.5), pytest.approx(193.5))
        assert carried.target.frame_y_px == pytest.approx(target.frame_y_px - 70.0, abs=1e-6)
        assert carried.target.azimuth_deg == target.azimuth_deg

    def test_the_strip_ball_mark_of_2026_11_02_lies_on_the_full_frame_ball(self):
        strip_ball = replace_ball(self._ball(geometry=self.STRIP), 350.9, 165.7)

        full = carry_camera_marks(None, strip_ball, self.FULL, None).ball

        assert full.frame_px == (pytest.approx(350.9), pytest.approx(265.7))
        # 640x400 ball of the session: (361, 274); the strip balls 172-176 -> 272-276.
        assert abs(full.frame_x_px - 361.0) <= 12.0 and abs(full.frame_y_px - 274.0) <= 12.0

    def test_a_ball_outside_the_strip_is_dropped_alone(self):
        target = self._target()

        carried = carry_camera_marks(
            target, self._ball(y_pct=95.0), self.STRIP, for_mode(INTRINSICS, 640, 200, 0)
        )

        assert carried.ball is None and carried.ball_outside
        assert carried.discarded is None
        assert carried.target.azimuth_deg == target.azimuth_deg

    @pytest.mark.parametrize(
        "geometry",
        [
            {**MOUNT, "width": 320, "height": 200, "vertical_offset_px": 0},
            {**MOUNT, "width": 640, "height": 100, "vertical_offset_px": 0},
            {**STRIP, "roll_correction_deg": 1.35},
        ],
    )
    def test_an_unmapped_mode_or_another_mount_drops_both(self, geometry):
        carried = carry_camera_marks(self._target(), self._ball(), geometry, INTRINSICS)

        assert (carried.target, carried.ball) == (None, None)
        assert carried.discarded == "camera geometry changed"

    def test_a_target_without_a_lens_model_for_the_mode_drops_both(self):
        carried = carry_camera_marks(self._target(), self._ball(), self.STRIP, None)

        assert (carried.target, carried.ball) == (None, None)
        assert carried.discarded

    def test_marks_of_the_same_geometry_stay_as_they_are(self):
        target, ball = self._target(), self._ball()

        carried = carry_camera_marks(target, ball, self.FULL, None)

        assert carried.target is target and carried.ball is ball

    def test_the_canonical_geometry_is_the_full_frame_at_window_zero(self):
        assert canonical_geometry({**self.STRIP, "vertical_offset_px": 20}) == self.FULL

    def test_a_file_of_the_full_frame_loads_into_the_strip(self, tmp_path):
        """The file saved before T3 (640x400) is already canonical: it loads in every mode."""
        path = tmp_path / "marks.json"
        target, ball = self._target(), self._ball()
        save_camera_marks(path, target, ball)

        loaded_target, loaded_ball, reason = load_camera_marks(
            path, self.STRIP, for_mode(INTRINSICS, 640, 200, 0)
        )

        assert reason is None
        assert loaded_ball.frame_px == (pytest.approx(367.5), pytest.approx(163.5))
        assert loaded_target.azimuth_deg == pytest.approx(target.azimuth_deg, abs=1e-3)
        assert loaded_target.frame_y_px == pytest.approx(target.frame_y_px - 100.0, abs=0.05)

    def test_a_ball_outside_this_mode_loads_without_it_and_keeps_the_file(self, tmp_path):
        path = tmp_path / "marks.json"
        save_camera_marks(path, self._target(), self._ball(y_pct=95.0))

        target, ball, reason = load_camera_marks(
            path, self.STRIP, for_mode(INTRINSICS, 640, 200, 0)
        )

        assert reason == BALL_MARK_OUTSIDE_MODE
        assert ball is None and target is not None
        assert json.loads(path.read_text())["ball_mark"] is not None

    def test_a_file_of_an_unmapped_mode_is_discarded_elsewhere(self, tmp_path):
        path = tmp_path / "marks.json"
        crop = {**self.MOUNT, "width": 320, "height": 200, "vertical_offset_px": 0}
        save_camera_marks(path, None, self._ball(geometry=crop))

        assert load_camera_marks(path, self.FULL) == (None, None, "camera geometry changed")
        assert not path.exists()


def replace_ball(ball: BallMark, x_px: float, y_px: float) -> BallMark:
    """The same mark at another saved-frame pixel."""
    return BallMark(
        frame_x_px=x_px,
        frame_y_px=y_px,
        preview_x_pct=ball.preview_x_pct,
        preview_y_pct=ball.preview_y_pct,
        set_at=ball.set_at,
        geometry=ball.geometry,
    )


def _shot(**overrides) -> Shot:
    values = {"ball_speed_mph": 110.0, "timestamp": datetime(2026, 10, 25, 12, 0)}
    values.update(overrides)
    return Shot(**values)


def _ti_only(vertical=20.0, horizontal=-5.0) -> Shot:
    return _shot(
        launch_angle_vertical=vertical,
        launch_angle_vertical_source="radar",
        launch_angle_vertical_confidence=0.8,
        launch_angle_confidence=0.8,
        angle_source="radar",
        iwr6843_vertical_deg=vertical,
        launch_angle_horizontal=horizontal,
        launch_angle_horizontal_source="radar",
        launch_angle_horizontal_confidence=0.6,
        iwr6843_horizontal_deg=horizontal,
    )


def _with_camera(vertical=18.0, horizontal=2.0, ti_vertical=20.0, ti_horizontal=-5.0) -> Shot:
    shot = _ti_only(ti_vertical, ti_horizontal)
    shot.experimental_camera_vertical_deg = vertical
    shot.experimental_camera_vertical_confidence = 0.75
    shot.experimental_camera_horizontal_deg = horizontal
    shot.launch_angle_horizontal = horizontal
    shot.launch_angle_horizontal_source = "camera_assisted_experimental"
    shot.launch_angle_horizontal_confidence = 0.75
    return shot


class TestAngleReferenceApply:
    def test_an_accepted_camera_vertical_wins_and_pairs_with_the_ti(self):
        reference = AngleReference()
        shot = _with_camera()

        assert reference.apply(shot) is False

        assert shot.launch_angle_vertical == 18.0
        assert shot.launch_angle_vertical_source == "camera"
        assert shot.launch_angle_vertical_confidence == 0.75
        assert shot.launch_angle_confidence == 0.75
        assert shot.angle_source == "camera"
        assert shot.iwr6843_vertical_deg == 20.0
        assert shot.angle_reference["vertical"]["selected"] == "camera"
        assert shot.angle_reference["vertical"]["offset"]["pairs"] == 1
        assert shot.angle_reference["horizontal"]["offset"]["offset_deg"] == -7.0

    def test_a_ti_only_shot_is_corrected_once_the_offset_is_valid(self):
        reference = AngleReference()
        changed = [reference.apply(_with_camera(vertical=18.0 + 0.1 * i)) for i in range(MIN_PAIRS)]
        assert changed == [False] * (MIN_PAIRS - 1) + [True]
        shot = _ti_only(vertical=21.0, horizontal=-4.0)

        reference.apply(shot)

        offset = shot.angle_reference["vertical"]["offset"]["offset_deg"]
        assert offset == pytest.approx(1.8)
        assert shot.launch_angle_vertical == pytest.approx(21.0 - 1.8)
        assert shot.launch_angle_vertical_source == "radar_calibrated"
        assert shot.launch_angle_vertical_confidence == 0.8
        assert shot.angle_source == "radar"
        assert shot.iwr6843_vertical_deg == 21.0
        assert shot.angle_reference["vertical"]["corrected_deg"] == pytest.approx(19.2)
        assert shot.angle_reference["vertical"]["calibration_rejected"] is None

    def test_the_horizontal_offset_is_logged_but_never_applied(self):
        # TI - camera of a compressed TI depends on where the pairs started, so a
        # learned median would move the fallback by the session's shot mix
        # (2026-10-25: RMSE 2.80 against 1.70 with the fixed offset).
        reference = AngleReference()
        for _ in range(MIN_PAIRS):
            reference.apply(_with_camera())
        assert reference.horizontal.valid
        shot = _ti_only(vertical=21.0, horizontal=-4.0)

        reference.apply(shot)

        horizontal = shot.angle_reference["horizontal"]
        assert shot.launch_angle_horizontal == -4.0
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.launch_angle_horizontal_confidence == 0.6
        assert horizontal["selected"] == "radar"
        assert horizontal["corrected_deg"] is None
        assert horizontal["offset"]["valid"] is True
        assert horizontal["offset"]["applied"] is False

    def test_a_calibrated_vertical_that_fails_the_guard_keeps_the_ti(self):
        # 2026-10-25 OF33: a topped driver, TI +0.08 deg, offset +3.00 deg.
        reference = AngleReference()
        for _ in range(MIN_PAIRS):
            reference.apply(_with_camera(vertical=17.0, ti_vertical=20.0))
        shot = _ti_only(vertical=0.08)
        checked = []

        def guard(angle_deg, guarded_shot):
            checked.append((angle_deg, guarded_shot))
            return angle_deg >= 0.0, {"rule": "bounds_0_45"}

        reference.apply(shot, vertical_guard=guard)

        vertical = shot.angle_reference["vertical"]
        assert checked == [(pytest.approx(-2.92), shot)]
        assert shot.launch_angle_vertical == 0.08
        assert shot.launch_angle_vertical_source == "radar"
        assert vertical["selected"] == "radar"
        assert vertical["corrected_deg"] == pytest.approx(-2.92)
        assert vertical["calibration_rejected"] == {
            "reason": "calibrated_implausible",
            "rule": "bounds_0_45",
        }

    def test_a_calibrated_vertical_that_passes_the_guard_is_shown(self):
        reference = AngleReference()
        for _ in range(MIN_PAIRS):
            reference.apply(_with_camera(vertical=17.0, ti_vertical=20.0))
        shot = _ti_only(vertical=12.0)

        reference.apply(shot, vertical_guard=lambda angle, _shot: (True, {"rule": None}))

        assert shot.launch_angle_vertical == pytest.approx(9.0)
        assert shot.launch_angle_vertical_source == "radar_calibrated"
        assert shot.angle_reference["vertical"]["calibration_rejected"] is None

    def test_a_ti_only_shot_stays_radar_until_the_offset_is_valid(self):
        reference = AngleReference()
        for _ in range(MIN_PAIRS - 1):
            reference.apply(_with_camera())
        shot = _ti_only()

        reference.apply(shot)

        assert shot.launch_angle_vertical == 20.0
        assert shot.launch_angle_vertical_source == "radar"
        assert shot.launch_angle_horizontal_source == "radar"
        assert shot.angle_reference["vertical"]["corrected_deg"] is None

    def test_a_noisy_session_never_corrects(self):
        reference = AngleReference()
        for camera in (14.0, 16.0, 18.0, 20.0, 22.0, 24.0):
            reference.apply(_with_camera(vertical=camera))
        shot = _ti_only()

        reference.apply(shot)

        assert shot.launch_angle_vertical_source == "radar"
        assert not shot.angle_reference["vertical"]["offset"]["valid"]

    def test_a_shot_without_a_plausible_ti_vertical_is_left_to_the_estimate(self):
        reference = AngleReference()
        for _ in range(MIN_PAIRS):
            reference.apply(_with_camera())
        shot = _shot(launch_angle_vertical_rejection_reason="ti_launch_implausible")

        reference.apply(shot)

        assert shot.launch_angle_vertical is None
        assert shot.launch_angle_vertical_source is None

    def test_a_camera_vertical_without_a_ti_value_wins_without_a_pair(self):
        reference = AngleReference()
        shot = _shot(
            experimental_camera_vertical_deg=12.0, experimental_camera_vertical_confidence=0.3
        )

        reference.apply(shot)

        assert shot.launch_angle_vertical == 12.0
        assert shot.launch_angle_vertical_source == "camera"
        assert shot.angle_reference["vertical"]["offset"]["pairs"] == 0

    def test_a_withheld_camera_direction_does_not_pair(self):
        reference = AngleReference()
        shot = _ti_only()
        shot.experimental_camera_horizontal_deg = 3.0  # logged, but the fusion fell back to TI

        reference.apply(shot)

        assert shot.angle_reference["horizontal"]["camera_deg"] is None
        assert shot.angle_reference["horizontal"]["offset"]["pairs"] == 0

    def test_a_demo_shot_is_left_alone(self):
        reference = AngleReference()
        shot = _with_camera()
        shot.demo = True

        assert reference.apply(shot) is False

        assert shot.launch_angle_vertical_source == "radar"
        assert shot.angle_reference is None
        assert reference.vertical.state()["pairs"] == 0

    def test_reset_of_one_axis_keeps_the_other(self):
        reference = AngleReference()
        for _ in range(MIN_PAIRS):
            reference.apply(_with_camera())

        assert reference.reset("target_set", axes=("horizontal",)) is True

        assert reference.vertical.valid
        assert not reference.horizontal.valid
        assert reference.reset("again", axes=("horizontal",)) is False

    def test_the_shot_logs_the_target_line_with_its_own_azimuth(self):
        reference = AngleReference()
        reference.target_line = target_line_from_tap(
            62.5,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            horizontal_pixel_sign=1.0,
            intrinsics=INTRINSICS,
            geometry=GEOMETRY,
            now=1_000.0,
        )
        shot = _with_camera()
        shot.angle_reference = {"target_line": {"shot_azimuth_deg": 4.321}}

        reference.apply(shot)

        logged = shot.angle_reference["target_line"]
        assert logged["shot_azimuth_deg"] == 4.321
        assert logged["azimuth_deg"] == reference.target_line.to_log()["azimuth_deg"]
        assert "geometry" not in logged
        assert reference.state()["target_line"]["set_at"] == 1_000.0

    @staticmethod
    def _target(x_pct: float, now: float) -> TargetLine:
        return target_line_from_tap(
            x_pct,
            40.0,
            width=640,
            height=400,
            roll_correction_deg=0.0,
            horizontal_pixel_sign=1.0,
            intrinsics=INTRINSICS,
            geometry=GEOMETRY,
            now=now,
        )

    def test_a_target_tapped_during_the_shot_keeps_its_pair_out(self):
        # The shot's ball flight ran against target A; B was tapped meanwhile.
        reference = AngleReference()
        target_a = self._target(45.0, 1_000.0)
        reference.target_line = target_a
        shot = _with_camera()
        shot.angle_reference = {
            "measured_against": target_a.to_log(),
            "target_line": {"shot_azimuth_deg": -2.0},
        }
        reference.target_line = self._target(70.0, 2_000.0)
        reference.reset("target_set", axes=("horizontal",))

        reference.apply(shot)

        logged = shot.angle_reference["target_line"]
        assert logged["set_at"] == 1_000.0
        assert logged["azimuth_deg"] == target_a.to_log()["azimuth_deg"]
        assert logged["shot_azimuth_deg"] == -2.0
        horizontal = shot.angle_reference["horizontal"]
        assert horizontal["target_changed"] is True
        assert horizontal["camera_deg"] == 2.0
        assert horizontal["offset"]["pairs"] == 0
        # The vertical does not depend on the target: its pair still counts.
        assert shot.angle_reference["vertical"]["offset"]["pairs"] == 1
        assert "measured_against" not in shot.angle_reference

    def test_a_shot_measured_before_the_first_target_keeps_its_pair_out(self):
        reference = AngleReference()
        shot = _with_camera()
        shot.angle_reference = {"measured_against": None}
        reference.target_line = self._target(70.0, 2_000.0)

        reference.apply(shot)

        assert shot.angle_reference["target_line"] is None
        assert shot.angle_reference["horizontal"]["target_changed"] is True
        assert shot.angle_reference["horizontal"]["offset"]["pairs"] == 0

    def test_a_shot_measured_against_the_current_target_pairs(self):
        reference = AngleReference()
        target = self._target(45.0, 1_000.0)
        reference.target_line = target
        shot = _with_camera()
        shot.angle_reference = {
            "measured_against": target.to_log(),
            "target_line": {"shot_azimuth_deg": -2.0},
        }

        reference.apply(shot)

        assert shot.angle_reference["horizontal"]["target_changed"] is False
        assert shot.angle_reference["horizontal"]["offset"]["pairs"] == 1
        assert shot.angle_reference["target_line"]["set_at"] == 1_000.0
        assert shot.angle_reference["target_line"]["shot_azimuth_deg"] == -2.0

    def test_the_state_is_json_ready(self):
        reference = AngleReference()
        reference.apply(_with_camera())
        state = reference.state()
        assert json.loads(json.dumps(state)) == state
        assert state["target_line"] is None
        assert state["ball_mark"] is None
        reference.ball_mark = ball_mark_from_tap(
            50.0, 66.0, width=640, height=400, roll_correction_deg=0.0, geometry=GEOMETRY
        )
        assert reference.state()["ball_mark"]["preview_y_pct"] == 66.0


def test_camera_vertical_confidence_follows_the_estimator_tier():
    assert camera_vertical_confidence("high", "iwr_range") == 0.75
    assert camera_vertical_confidence("experimental", "iwr_range") == 0.45
    assert (
        camera_vertical_confidence("experimental", "camera_size") == CAMERA_ONLY_VERTICAL_CONFIDENCE
    )
    assert camera_vertical_confidence("withheld", "iwr_range") is None
