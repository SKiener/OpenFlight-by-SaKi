"""Hitting zone from the camera mount geometry and the calibrated lens (task A, c)."""

import math
from dataclasses import replace

import numpy as np
import pytest

from openflight.camera.intrinsics import CameraIntrinsics
from openflight.camera.tee_zone import GOLF_BALL_DIAMETER_M, TeeZone, tee_zone

# The lens the Pi used on 2026-10-25 (camera-intrinsics.json of 2026-10-04).
RANGE_LENS = CameraIntrinsics(
    fx=575.6873399350347,
    fy=577.2796462182525,
    cx=347.83452287071384,
    cy=246.92488611593205,
    dist_coeffs=(
        -0.4429218049772407,
        0.20397207911013926,
        -0.006967360091727496,
        -0.0005608074955810034,
        0.0,
    ),
    image_width_px=640,
    image_height_px=400,
    rotate_180=False,
    mirror_horizontal=False,
    scaler_crop=None,
    rms_reprojection_px=0.38,
)
# Session config of 2026-10-25: camera mount and TI tee geometry.
RANGE_MOUNT = {
    "camera_height_m": 0.182,
    "camera_lateral_offset_m": -0.025,
    "camera_forward_offset_m": 0.007,
    "tee_range_m": 1.5,
    "radar_height_m": 0.132,
    "ball_height_m": 0.02135,
}


def pinhole(**overrides) -> CameraIntrinsics:
    values = {
        "fx": 500.0,
        "fy": 500.0,
        "cx": 320.0,
        "cy": 200.0,
        "dist_coeffs": (0.0, 0.0, 0.0, 0.0, 0.0),
        "image_width_px": 640,
        "image_height_px": 400,
        "rotate_180": False,
        "mirror_horizontal": False,
        "scaler_crop": None,
        "rms_reprojection_px": 0.1,
    }
    values.update(overrides)
    return CameraIntrinsics(**values)


def test_project_inverts_undistort():
    for x_px, y_px in ((371.7, 267.2), (20.0, 15.0), (600.0, 390.0), (347.8, 246.9)):
        x_norm, y_norm = RANGE_LENS.undistort(x_px, y_px)
        assert RANGE_LENS.project(x_norm, y_norm) == pytest.approx((x_px, y_px), abs=1e-3)


def test_range_zone_holds_every_tee_spot_and_excludes_the_tree_line():
    """Real ball x 358-378 / y 251-269; the old anchor's tree crown sat at (313.8, 169)."""
    zone = tee_zone(RANGE_LENS, **RANGE_MOUNT)
    for x, y in ((358, 251), (378, 251), (358, 269), (378, 269), (371.7, 267.2)):
        assert zone.contains(x, y)
    assert not zone.contains(313.8, 169.0)
    # Below the horizon of the 4.1 deg down camera (row ~206) and the tree crown.
    assert zone.y0 > 246.9 - 577.3 * math.tan(math.radians(4.1)) > 169.0 + 15.0 / 2
    assert zone.diameter_px == pytest.approx(16.5, abs=0.3)
    assert (zone.image_width_px, zone.image_height_px) == (640, 400)


def test_level_pinhole_tee_matches_the_geometry():
    lens = pinhole()
    zone = tee_zone(lens, **RANGE_MOUNT)
    forward = math.sqrt(1.5**2 - (0.02135 - 0.132) ** 2) - 0.007
    assert zone.tee_x == pytest.approx(320.0 + 500.0 * 0.025 / forward)
    assert zone.tee_y == pytest.approx(200.0 + 500.0 * (0.182 - 0.02135) / forward)
    assert zone.diameter_px == pytest.approx(500.0 * GOLF_BALL_DIAMETER_M / forward)
    assert zone.contains(zone.tee_x, zone.tee_y)


def test_zone_margins_cover_pitch_yaw_and_tee_placement():
    lens = pinhole()
    zone = tee_zone(lens, **RANGE_MOUNT)
    forward = math.sqrt(1.5**2 - (0.02135 - 0.132) ** 2) - 0.007
    below = math.atan2(0.182 - 0.02135, forward)
    for pitch_down_deg in (-6.0, 0.0, 4.0, 8.0):
        y = 200.0 + 500.0 * math.tan(below - math.radians(pitch_down_deg))
        assert zone.y0 <= y <= zone.y1
    for yaw_deg in (-6.0, 6.0):
        x = 320.0 + 500.0 * math.tan(math.atan2(0.025, forward) - math.radians(yaw_deg))
        assert zone.x0 <= x <= zone.x1


def test_zone_stays_below_the_horizon_of_a_camera_pitched_down_like_at_the_range():
    """Below the horizon for > ~3.8 deg down-pitch; 2026-10-25 was 4.1 deg down."""
    lens = pinhole()
    zone = tee_zone(lens, **RANGE_MOUNT)
    for pitch_down_deg in (3.9, 4.1, 8.0):
        horizon_row = 200.0 - 500.0 * math.tan(math.radians(pitch_down_deg))
        assert zone.y0 > horizon_row
    assert zone.y0 < 200.0  # a level camera's zone reaches above its horizon (documented)


def test_mirrored_frames_flip_the_zone():
    lens = pinhole()
    plain = tee_zone(lens, **RANGE_MOUNT)
    mirrored = tee_zone(lens, **RANGE_MOUNT, horizontal_pixel_sign=-1.0)
    assert mirrored.tee_x == pytest.approx(2 * 320.0 - plain.tee_x)
    assert (mirrored.y0, mirrored.y1) == (plain.y0, plain.y1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tee_range_m": 0.1},
        {"camera_height_m": 0.01},
    ],
)
def test_implausible_geometry_gives_no_zone(overrides):
    assert tee_zone(pinhole(), **{**RANGE_MOUNT, **overrides}) is None


def test_zone_is_clipped_to_the_image():
    zone = tee_zone(pinhole(fx=2000.0, fy=2000.0), **RANGE_MOUNT)
    assert 0 <= zone.x0 < zone.x1 <= 640 and 0 <= zone.y0 < zone.y1 <= 400
    assert np.isfinite(zone.tee_x)
    assert zone.to_dict()["diameter_px"] == round(zone.diameter_px, 1)


def test_a_shifted_zone_is_clipped_to_the_new_frame():
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)

    strip = zone.shifted(0, -100, 640, 200)

    assert (strip.x0, strip.y0, strip.x1, strip.y1) == (263, 109, 454, 200)
    assert (strip.tee_x, strip.tee_y, strip.diameter_px) == (357.4, pytest.approx(208.7), 16.5)
    assert (strip.image_width_px, strip.image_height_px) == (640, 200)
    assert zone.shifted(0, -100, 640, 200).shifted(0, 100, 640, 400).y1 == 300  # clipped once
    assert zone.shifted(0, -389, 640, 200) is None  # one row left


def test_the_range_zone_shifted_into_the_strip_is_the_strip_lens_zone():
    """Shifting the 640x400 zone equals computing it with the strip's lens model."""
    from openflight.camera.intrinsics import for_mode

    full = tee_zone(RANGE_LENS, **RANGE_MOUNT)
    strip_lens = for_mode(RANGE_LENS, 640, 200, 0)

    computed = tee_zone(strip_lens, **RANGE_MOUNT)

    assert full.shifted(0, -100, 640, 200) == replace(computed, tee_y=pytest.approx(computed.tee_y))


# --- server wiring -----------------------------------------------------------------


class _ZoneRecorder:
    """The capture runtime's zone handling, mode mapping included."""

    def __init__(self, config):
        self.zones = []
        self.config = config

    def set_tee_zone(self, zone, *, vertical_offset_px=None):
        assert vertical_offset_px == 0  # the server computes it in the calibrated frame
        self.zones.append(zone)

    def active_tee_zone(self):
        from openflight.camera.capture_runtime import mode_frame_shift

        zone = self.zones[-1]
        if zone is None:
            return None
        size = (self.config["width"], self.config["height"])
        shift = mode_frame_shift((zone.image_width_px, zone.image_height_px, 0), (*size, 0))
        return None if shift is None else zone.shifted(*shift, *size)


RANGE_CAMERA_CONFIG = {
    "width": 640,
    "height": 400,
    "rotate_180": False,
    "mirror_horizontal": False,
    "scaler_crop": None,
    "mount_height_m": 0.182,
    "lateral_offset_m": -0.025,
    "forward_offset_m": 0.007,
}


def _configure(monkeypatch, *, intrinsics=RANGE_LENS, calibration="range", config=None):
    from types import SimpleNamespace

    from openflight import server as server_module

    config = dict(RANGE_CAMERA_CONFIG if config is None else config)
    runtime = _ZoneRecorder(config)
    if calibration == "range":
        calibration = SimpleNamespace(
            tee_range_m=1.5, radar_height_m=0.132, tee_ball_height_m=0.02135
        )
    ti = None if calibration is None else SimpleNamespace(calibration=calibration)
    monkeypatch.setattr(server_module, "camera_capture_runtime", runtime)
    monkeypatch.setattr(server_module, "camera_intrinsics", intrinsics)
    monkeypatch.setattr(server_module, "iwr6843_runtime", ti)
    monkeypatch.setattr(server_module, "camera_capture_config", config)
    return server_module.configure_camera_tee_zone(), runtime, config


def test_server_gives_the_camera_the_range_zone(monkeypatch):
    zone, runtime, config = _configure(monkeypatch)
    assert runtime.zones == [zone]
    assert zone.contains(371.7, 267.2)
    assert config["tee_zone"]["y0"] == zone.y0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"intrinsics": None}, "no --camera-intrinsics"),
        ({"calibration": None}, "no TI tee geometry"),
        ({"intrinsics": replace(RANGE_LENS, rotate_180=True)}, "intrinsics mode differs"),
    ],
)
def test_server_without_lens_or_tee_geometry_sets_no_zone(monkeypatch, caplog, kwargs, reason):
    zone, runtime, config = _configure(monkeypatch, **kwargs)
    assert zone is None and runtime.zones == [None] and config["tee_zone"] is None
    assert reason in caplog.text and "camera gate never blocks" in caplog.text


def test_server_gives_the_strip_the_full_frame_zone(monkeypatch):
    """T3: in 640x200 the zone was refused ("intrinsics mode differs"); now it is moved."""
    full, _runtime, _config = _configure(monkeypatch)

    zone, runtime, config = _configure(
        monkeypatch, config={**RANGE_CAMERA_CONFIG, "width": 640, "height": 200}
    )

    assert runtime.zones[-1].image_height_px == 400
    assert zone == full.shifted(0, -100, 640, 200)
    assert config["tee_zone"]["image_height_px"] == 200


def test_server_keeps_the_zone_for_later_in_an_unmapped_mode(monkeypatch, caplog):
    zone, runtime, config = _configure(
        monkeypatch, config={**RANGE_CAMERA_CONFIG, "width": 320, "height": 200}
    )

    assert zone is None and config["tee_zone"] is None
    assert runtime.zones[-1] is not None  # applies again after a switch to a mapped mode
    assert "intrinsics mode differs" in caplog.text and "320x200" in caplog.text
