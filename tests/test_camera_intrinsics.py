"""Calibrated lens model: loading, undistortion and angle recovery."""

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from openflight.camera.ball_flight import (  # noqa: E402
    BallCandidate,
    CameraBallGeometry,
    _camera_model,
    _path_estimate,
)
from openflight.camera.club_motion import ReferenceBall  # noqa: E402
from openflight.camera.geometry import (  # noqa: E402
    deroll_normalized_offsets,
    normalized_image_offsets,
)
from openflight.camera.intrinsics import (  # noqa: E402
    CameraIntrinsics,
    for_mode,
    load_camera_intrinsics,
)

FX, FY, CX, CY = 472.0, 468.5, 331.4, 186.2
DIST = (-0.21, 0.08, 0.0015, -0.0008, -0.012)


def _intrinsics(**overrides) -> CameraIntrinsics:
    values = {
        "fx": FX,
        "fy": FY,
        "cx": CX,
        "cy": CY,
        "dist_coeffs": DIST,
        "image_width_px": 640,
        "image_height_px": 400,
        "rotate_180": False,
        "mirror_horizontal": False,
        "scaler_crop": None,
        "rms_reprojection_px": 0.25,
    }
    values.update(overrides)
    return CameraIntrinsics(**values)


def _payload(**overrides) -> dict:
    payload = {
        "version": 1,
        "fx": FX,
        "fy": FY,
        "cx": CX,
        "cy": CY,
        "dist_coeffs": list(DIST),
        "rms_reprojection_px": 0.25,
        "mode": {
            "width": 640,
            "height": 400,
            "rotate_180": False,
            "mirror_horizontal": True,
            "scaler_crop": None,
        },
    }
    payload.update(overrides)
    return payload


def test_loader_round_trips_calibration_json(tmp_path):
    path = tmp_path / "intrinsics.json"
    path.write_text(json.dumps(_payload()))

    loaded = load_camera_intrinsics(path)

    assert (loaded.fx, loaded.fy, loaded.cx, loaded.cy) == (FX, FY, CX, CY)
    assert loaded.dist_coeffs == DIST
    assert (loaded.image_width_px, loaded.image_height_px) == (640, 400)
    assert loaded.mirror_horizontal is True
    assert loaded.focal_px == pytest.approx((FX + FY) / 2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"fx": -1.0},
        {"fy": float("nan")},
        {"cx": 900.0},
        {"dist_coeffs": [0.1, 0.2, 0.0]},
        {"dist_coeffs": [0.1] * 8},
        {"mode": {"width": 640}},
        {"rms_reprojection_px": "low"},
    ],
)
def test_loader_rejects_invalid_calibration(tmp_path, overrides):
    path = tmp_path / "intrinsics.json"
    path.write_text(json.dumps(_payload(**overrides)))

    with pytest.raises(ValueError):
        load_camera_intrinsics(path)


def test_loader_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        load_camera_intrinsics(tmp_path / "missing.json")


def test_undistort_inverts_opencv_projection_across_the_frame():
    intrinsics = _intrinsics()
    normalized = np.array(
        [[x, y] for x in np.linspace(-0.65, 0.65, 7) for y in np.linspace(-0.4, 0.4, 5)]
    )
    camera_points = np.column_stack((normalized, np.ones(len(normalized))))
    pixels, _ = cv2.projectPoints(
        camera_points,
        np.zeros(3),
        np.zeros(3),
        np.array([[FX, 0.0, CX], [0.0, FY, CY], [0.0, 0.0, 1.0]]),
        np.asarray(DIST),
    )
    pixels = pixels.reshape(-1, 2)

    x, y = intrinsics.undistort(pixels[:, 0], pixels[:, 1])

    assert np.max(np.abs(x - normalized[:, 0])) < 1e-6
    assert np.max(np.abs(y - normalized[:, 1])) < 1e-6
    scalar = intrinsics.undistort(float(pixels[0, 0]), float(pixels[0, 1]))
    assert isinstance(scalar[0], float)


def test_mode_mismatch_names_the_differing_setting():
    intrinsics = _intrinsics()
    settings = {"width": 640, "height": 400, "rotate_180": False, "mirror_horizontal": False}

    assert intrinsics.mode_mismatch(settings) is None
    assert "height" in intrinsics.mode_mismatch({**settings, "height": 200})
    assert "mirror_horizontal" in intrinsics.mode_mismatch({**settings, "mirror_horizontal": True})
    assert "scaler_crop" in intrinsics.mode_mismatch({**settings, "scaler_crop": [0, 0, 1, 1]})


@pytest.mark.parametrize("strip_offset", [0, 20, -30])
def test_for_mode_moves_the_principal_point_into_the_strip(strip_offset):
    base = _intrinsics()

    strip = for_mode(base, 640, 200, strip_offset)

    assert strip.cy == pytest.approx(CY - 100.0 - strip_offset)
    assert (strip.fx, strip.fy, strip.cx, strip.dist_coeffs) == (FX, FY, CX, DIST)
    assert (strip.image_width_px, strip.image_height_px) == (640, 200)
    assert strip.rms_reprojection_px == base.rms_reprojection_px
    assert strip.mode_mismatch({"width": 640, "height": 200}) is None


@pytest.mark.parametrize("strip_offset", [0, 30, -20])
def test_for_mode_moves_a_rotated_mounts_principal_point_by_100_minus_s(strip_offset):
    """The runtime rotates saved frames in software: the strip offset counts the other way."""
    base = _intrinsics(rotate_180=True)

    strip = for_mode(base, 640, 200, strip_offset)

    assert strip.cy == pytest.approx(CY - 100.0 + strip_offset)
    assert strip.cx == pytest.approx(CX)
    assert strip.mode_mismatch({"width": 640, "height": 200, "rotate_180": True}) is None


def test_for_mode_keeps_the_calibrated_mode_and_ignores_the_offset_there():
    base = _intrinsics()

    assert for_mode(base, 640, 400, 0) is base
    # The driver applies strip_y_offset to the strip modes only.
    assert for_mode(base, 640, 400, 20).cy == CY


@pytest.mark.parametrize(
    ("width", "height", "offset"),
    [(640, 100, 0), (320, 200, 0), (1280, 800, 0), (640, 200, None), (640, 200, 1.5)],
)
def test_for_mode_has_no_model_for_an_unmapped_mode(width, height, offset):
    assert for_mode(_intrinsics(), width, height, offset) is None


@pytest.mark.parametrize("strip_offset", [0, 20])
def test_a_direction_projects_into_the_strip_100_rows_higher(strip_offset):
    base = _intrinsics()
    strip = for_mode(base, 640, 200, strip_offset)

    for x_norm, y_norm in [(0.0, 0.0), (0.3, -0.1), (-0.45, 0.2), (0.12, 0.05)]:
        full_x, full_y = base.project(x_norm, y_norm)
        strip_x, strip_y = strip.project(x_norm, y_norm)
        assert strip_x - full_x == pytest.approx(0.0, abs=1e-6)
        assert full_y - strip_y == pytest.approx(100.0 + strip_offset, abs=1e-6)


def test_offsets_without_intrinsics_are_bitwise_the_legacy_pinhole():
    rng = np.random.default_rng(7)
    x_px = rng.uniform(0, 640, 50)
    y_px = rng.uniform(0, 400, 50)
    for sign, roll in ((1.0, 0.0), (-1.0, 2.8)):
        legacy = deroll_normalized_offsets(
            sign * (x_px - 640 / 2.0) / 517.3,
            -(y_px - 400 / 2.0) / 517.3,
            roll,
        )

        offsets = normalized_image_offsets(
            x_px,
            y_px,
            focal_px=517.3,
            image_width_px=640,
            image_height_px=400,
            horizontal_pixel_sign=sign,
            roll_correction_deg=roll,
        )

        assert np.array_equal(offsets[0], legacy[0])
        assert np.array_equal(offsets[1], legacy[1])


def _render(points_world, *, camera_origin, pitch_rad, mirror):
    """World points (lateral, forward, up) -> distorted saved-frame pixels."""
    vector = points_world - camera_origin
    forward = math.cos(pitch_rad) * vector[:, 1] + math.sin(pitch_rad) * vector[:, 2]
    up = -math.sin(pitch_rad) * vector[:, 1] + math.cos(pitch_rad) * vector[:, 2]
    camera_points = np.column_stack((vector[:, 0], -up, forward))
    pixels, _ = cv2.projectPoints(
        camera_points,
        np.zeros(3),
        np.zeros(3),
        np.array([[FX, 0.0, CX], [0.0, FY, CY], [0.0, 0.0, 1.0]]),
        np.asarray(DIST),
    )
    pixels = pixels.reshape(-1, 2)
    if mirror:
        # The calibration is taken on saved (already mirrored) frames.
        pixels[:, 0] = 640 - 1 - pixels[:, 0]
    return pixels, np.linalg.norm(vector, axis=1)


def _distorted_shot(*, horizontal_deg, vertical_deg, mirror=False):
    geometry = CameraBallGeometry(
        camera_height_m=0.20955,
        radar_height_m=0.15875,
        tee_range_m=1.524,
        ball_height_m=0.02135,
        horizontal_pixel_sign=-1.0 if mirror else 1.0,
    )
    pitch_rad = math.radians(-6.0)
    camera_origin = geometry.camera_origin
    tee = np.array([0.0, geometry.ball_forward_m, geometry.ball_height_m])
    speed_ms = 55.0
    horizontal = math.radians(horizontal_deg)
    vertical = math.radians(vertical_deg)
    velocity = speed_ms * np.array(
        [
            math.cos(vertical) * math.sin(horizontal),
            math.cos(vertical) * math.cos(horizontal),
            math.sin(vertical),
        ]
    )
    times = np.arange(8, dtype=float) * 0.00347 + 0.004
    positions = tee + times[:, None] * velocity
    anchor_pixels, anchor_ranges = _render(
        tee[None], camera_origin=camera_origin, pitch_rad=pitch_rad, mirror=mirror
    )
    anchor_px, anchor_range = anchor_pixels[0], anchor_ranges[0]
    # A blurred silhouette measures ~15% wide, as the legacy model assumes it does not.
    anchor = ReferenceBall(
        x=float(anchor_px[0]),
        y=float(anchor_px[1]),
        diameter_px=1.15 * FX * geometry.ball_diameter_m / anchor_range,
        area_px=200,
    )
    pixels, ranges = _render(
        positions, camera_origin=camera_origin, pitch_rad=pitch_rad, mirror=mirror
    )
    candidates = []
    for (x_px, y_px), camera_range in zip(pixels, ranges, strict=True):
        diameter = FX * geometry.ball_diameter_m / camera_range
        candidates.append(
            BallCandidate(
                x=float(x_px),
                y=float(y_px),
                area=max(5, round(math.pi * (diameter / 2) ** 2)),
                width=max(2, round(diameter)),
                height=max(2, round(diameter)),
                fill=0.75,
                circularity=0.9,
                mean_intensity=220.0,
                local_contrast=150.0,
            )
        )
    radar_ranges = np.linalg.norm(positions - np.array([0.0, 0.0, geometry.radar_height_m]), axis=1)
    evidence = SimpleNamespace(
        track=SimpleNamespace(
            range_at=lambda time_s, _res: float(np.interp(time_s, times, radar_ranges))
        ),
        geometry=SimpleNamespace(range_res_m=0.046875),
        impact_t_s=0.0,
    )
    return geometry, anchor, candidates, np.asarray(times * 1e9, dtype=np.int64), evidence, speed_ms


def _estimate(geometry, anchor, candidates, timestamps, evidence, speed_ms):
    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=None,
        model=_camera_model(anchor, geometry),
        geometry=geometry,
        thresholds=(100, 12, 5),
    )
    assert result is not None
    return result[1]


@pytest.mark.parametrize(
    ("horizontal_deg", "vertical_deg", "mirror"),
    [(6.0, 14.0, False), (-8.0, 22.0, False), (5.0, 18.0, True)],
)
def test_calibrated_lens_recovers_launch_angles_through_distortion(
    horizontal_deg, vertical_deg, mirror
):
    geometry, anchor, candidates, timestamps, evidence, speed = _distorted_shot(
        horizontal_deg=horizontal_deg, vertical_deg=vertical_deg, mirror=mirror
    )
    intrinsics = _intrinsics(mirror_horizontal=mirror)
    if mirror:
        # Mirroring moves the principal point and flips tangential p2.
        k1, k2, p1, p2, k3 = DIST
        intrinsics = replace(intrinsics, cx=640 - 1 - CX, dist_coeffs=(k1, k2, p1, -p2, k3))

    calibrated = _estimate(
        replace(geometry, intrinsics=intrinsics), anchor, candidates, timestamps, evidence, speed
    )
    legacy = _estimate(geometry, anchor, candidates, timestamps, evidence, speed)

    assert calibrated.horizontal_deg == pytest.approx(horizontal_deg, abs=0.15)
    assert calibrated.vertical_deg == pytest.approx(vertical_deg, abs=0.2)
    # The image-centre, ball-size-focal model is measurably biased here.
    legacy_error = max(
        abs(legacy.horizontal_deg - horizontal_deg), abs(legacy.vertical_deg - vertical_deg)
    )
    assert legacy_error > 1.0


def _run_server_main(monkeypatch, tmp_path, *extra_args):
    from openflight import server

    init_calls = []
    monkeypatch.setattr(server, "camera_intrinsics", None)
    monkeypatch.setattr(
        "sys.argv",
        [
            "openflight-server",
            "--no-logging",
            "--camera-capture",
            "--startup-status-file",
            str(tmp_path / "status.json"),
            *extra_args,
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "init_camera_capture", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    server.main()
    return server, init_calls


def test_server_without_intrinsics_flag_keeps_the_legacy_model(monkeypatch, tmp_path):
    server, init_calls = _run_server_main(monkeypatch, tmp_path)

    assert server.camera_intrinsics is None
    assert len(init_calls) == 1


def test_server_loads_intrinsics_flag(monkeypatch, tmp_path):
    path = tmp_path / "intrinsics.json"
    path.write_text(json.dumps(_payload()))

    server, _calls = _run_server_main(monkeypatch, tmp_path, "--camera-intrinsics", str(path))

    assert server.camera_intrinsics.fx == FX
    assert server.camera_intrinsics.mirror_horizontal is True


def test_server_starts_in_the_strip_with_the_full_frame_calibration(monkeypatch, tmp_path):
    path = tmp_path / "intrinsics.json"
    path.write_text(json.dumps(_payload()))

    server, _calls = _run_server_main(
        monkeypatch,
        tmp_path,
        "--camera-intrinsics",
        str(path),
        "--camera-capture-width",
        "640",
        "--camera-capture-height",
        "200",
    )

    assert server.camera_intrinsics.image_height_px == 400


@pytest.mark.parametrize("width", [None, "320"])
def test_server_rejects_unusable_intrinsics(monkeypatch, tmp_path, width):
    path = tmp_path / "intrinsics.json"
    path.write_text(json.dumps(_payload() if width else {"version": 1}))
    extra = ("--camera-capture-width", width, "--camera-capture-height", "200") if width else ()

    with pytest.raises(SystemExit):
        _run_server_main(monkeypatch, tmp_path, "--camera-intrinsics", str(path), *extra)


@pytest.mark.parametrize(
    ("saved_settings", "applied"),
    [
        ({"width": 640, "height": 400, "mirror_horizontal": False}, True),
        ({"width": 640, "height": 400, "mirror_horizontal": True}, False),
        ({"width": 320, "height": 200, "mirror_horizontal": False}, False),
        ({"width": 640, "height": 200, "mirror_horizontal": False}, "strip"),
        (None, True),
    ],
)
def test_fusion_applies_intrinsics_only_in_the_calibrated_mode(
    monkeypatch, tmp_path, saved_settings, applied
):
    from openflight import server
    from openflight.camera import ball_flight, club_delivery
    from openflight.launch_monitor import Shot

    geometries = {}

    def record(name, result):
        def fake(*_args, **kwargs):
            geometries[name] = kwargs["geometry"]
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
        server,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=1.524, radar_height_m=0.15875, tee_ball_height_m=0.04
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "camera_capture_config",
        {"mount_height_m": 0.20955, "width": 640, "height": 400, "mirror_horizontal": False},
    )
    intrinsics = _intrinsics()
    monkeypatch.setattr(server, "camera_intrinsics", intrinsics)
    metadata = {"settings": saved_settings} if saved_settings is not None else {}
    capture = SimpleNamespace(valid=True, path=tmp_path, metadata=metadata)
    shot = Shot(ball_speed_mph=110.0, timestamp=None, club_speed_mph=85.0)

    server._fuse_camera_ball_flight(shot, capture)
    server._fuse_camera_club_delivery(shot, capture)

    if applied == "strip":
        # The strip gets the calibration carried into its mode (T3), not None.
        assert geometries["ball"].intrinsics == for_mode(intrinsics, 640, 200, 0)
        assert geometries["club"].intrinsics == for_mode(intrinsics, 640, 200, 0)
        return
    expected = intrinsics if applied else None
    assert geometries["ball"].intrinsics is expected
    assert geometries["club"].intrinsics is expected
