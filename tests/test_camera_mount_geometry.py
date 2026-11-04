"""Camera mount geometry: fore/aft offset and yaw from the stationary ball."""

import json
import math
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.camera.ball_flight import (
    BallCandidate,
    CameraBallGeometry,
    _camera_model,
    _path_estimate,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.geometry import camera_to_world, reference_ball_pose

FOCAL_PX = 480.0


def test_reference_ball_pose_recovers_yaw_and_pitch():
    yaw, pitch = reference_ball_pose(
        -math.tan(math.radians(3.0)),
        0.0,
        camera_lateral_offset_m=0.0,
        camera_ball_forward_m=1.4,
        height_delta_m=-0.15,
    )

    assert math.degrees(yaw) == pytest.approx(3.0)
    assert pitch == pytest.approx(math.atan2(-0.15, 1.4))
    assert reference_ball_pose(
        -0.08 / 1.4, 0.0, camera_lateral_offset_m=0.08, camera_ball_forward_m=1.4, height_delta_m=0
    )[0] == pytest.approx(0.0, abs=1e-12)


def test_camera_to_world_without_yaw_is_the_legacy_pitch_rotation():
    rng = np.random.default_rng(3)
    lateral, vertical = rng.normal(size=(2, 20))
    pitch = -0.07

    world_lateral, world_forward, world_vertical = camera_to_world(
        lateral, 1.0, vertical, pitch_rad=pitch, yaw_rad=0.0
    )

    assert np.array_equal(world_lateral, lateral)
    assert np.array_equal(world_forward, math.cos(pitch) - vertical * math.sin(pitch))
    assert np.array_equal(world_vertical, math.sin(pitch) + vertical * math.cos(pitch))


def _world_to_pixel(point, *, origin, pitch, yaw, width=640, height=400):
    lateral_w, forward_w, up_w = point - origin
    lateral = math.cos(yaw) * lateral_w - math.sin(yaw) * forward_w
    level_forward = math.sin(yaw) * lateral_w + math.cos(yaw) * forward_w
    forward = math.cos(pitch) * level_forward + math.sin(pitch) * up_w
    up = -math.sin(pitch) * level_forward + math.cos(pitch) * up_w
    return width / 2 + FOCAL_PX * lateral / forward, height / 2 - FOCAL_PX * up / forward


def _mounted_shot(
    geometry, *, yaw_deg, render_geometry=None, horizontal_deg=4.0, vertical_deg=16.0
):
    yaw, pitch = math.radians(yaw_deg), math.radians(-5.0)
    origin = (render_geometry or geometry).camera_origin
    tee = np.array([0.0, geometry.ball_forward_m, geometry.ball_height_m])
    anchor_x, anchor_y = _world_to_pixel(tee, origin=origin, pitch=pitch, yaw=yaw)
    anchor = ReferenceBall(
        anchor_x,
        anchor_y,
        FOCAL_PX * geometry.ball_diameter_m / np.linalg.norm(tee - origin),
        150,
    )
    h, v = math.radians(horizontal_deg), math.radians(vertical_deg)
    velocity = 50.0 * np.array([math.cos(v) * math.sin(h), math.cos(v) * math.cos(h), math.sin(v)])
    times = np.arange(8) * 0.0035 + 0.004
    positions = tee + times[:, None] * velocity
    candidates = []
    for point in positions:
        x_px, y_px = _world_to_pixel(point, origin=origin, pitch=pitch, yaw=yaw)
        diameter = FOCAL_PX * geometry.ball_diameter_m / np.linalg.norm(point - origin)
        candidates.append(
            BallCandidate(
                x_px,
                y_px,
                max(5, round(math.pi * diameter**2 / 4)),
                round(diameter),
                round(diameter),
                0.75,
                0.9,
                220.0,
                150.0,
            )
        )
    radar_ranges = np.linalg.norm(positions - np.array([0.0, 0.0, geometry.radar_height_m]), axis=1)
    evidence = SimpleNamespace(
        track=SimpleNamespace(range_at=lambda t, _r: float(np.interp(t, times, radar_ranges))),
        geometry=SimpleNamespace(range_res_m=0.046875),
        impact_t_s=0.0,
    )
    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=np.asarray(times * 1e9, dtype=np.int64),
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=50.0 * 2.23694,
        iwr_vertical_deg=None,
        model=_camera_model(anchor, geometry),
        geometry=geometry,
        thresholds=(100, 12, 5),
    )
    assert result is not None
    return result[1]


BASE = CameraBallGeometry(
    camera_height_m=0.20955,
    radar_height_m=0.15875,
    tee_range_m=1.5,
    ball_height_m=0.02135,
    camera_lateral_offset_m=0.06,
)


@pytest.mark.parametrize("yaw_deg", [-3.0, 2.5])
def test_yaw_from_reference_ball_removes_camera_aim_error(yaw_deg):
    derived = _mounted_shot(replace(BASE, yaw_from_reference_ball=True), yaw_deg=yaw_deg)
    legacy = _mounted_shot(BASE, yaw_deg=yaw_deg)

    assert derived.horizontal_deg == pytest.approx(4.0, abs=0.1)
    assert derived.vertical_deg == pytest.approx(16.0, abs=0.2)
    assert abs(legacy.horizontal_deg - 4.0) > 0.6 * abs(yaw_deg)


def test_forward_camera_offset_is_part_of_the_mount_geometry():
    forward = replace(BASE, camera_forward_offset_m=0.12)

    estimate = _mounted_shot(forward, yaw_deg=0.0)
    ignored = _mounted_shot(BASE, yaw_deg=0.0, render_geometry=forward)

    assert forward.camera_origin.tolist() == [0.06, 0.12, 0.20955]
    assert forward.camera_ball_forward_m == pytest.approx(BASE.ball_forward_m - 0.12)
    assert estimate.horizontal_deg == pytest.approx(4.0, abs=0.1)
    assert estimate.vertical_deg == pytest.approx(16.0, abs=0.2)
    assert abs(ignored.vertical_deg - 16.0) > 0.3


def test_default_geometry_keeps_lateral_ball_offset_out_of_yaw():
    assert BASE.camera_forward_offset_m == 0.0
    assert BASE.yaw_from_reference_ball is False
    assert _camera_model(ReferenceBall(250.0, 260.0, 14.0, 150), BASE).yaw_rad == 0.0


def test_server_plumbs_mount_flags(monkeypatch, tmp_path):
    from openflight import server

    init_calls = []
    monkeypatch.setattr(
        "sys.argv",
        [
            "openflight-server",
            "--no-logging",
            "--camera-capture",
            "--camera-capture-forward-offset-m",
            "-0.05",
            "--camera-capture-yaw-from-ball",
            "--startup-status-file",
            str(tmp_path / "status.json"),
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "init_camera_capture", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)

    server.main()

    assert init_calls[0]["forward_offset_m"] == -0.05
    assert init_calls[0]["yaw_from_ball"] is True
    assert init_calls[0]["mount_height_m"] == 0.20955


@pytest.mark.parametrize(
    ("config", "forward", "yaw"),
    [({}, 0.0, False), ({"forward_offset_m": 0.1, "yaw_from_ball": True}, 0.1, True)],
)
def test_fusion_passes_mount_geometry(monkeypatch, tmp_path, config, forward, yaw):
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
    (tmp_path / "metadata.json").write_text(json.dumps({}))
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
        {"mount_height_m": 0.20955, "width": 640, "height": 400, **config},
    )
    capture = SimpleNamespace(valid=True, path=tmp_path, metadata={})
    shot = Shot(ball_speed_mph=110.0, timestamp=None, club_speed_mph=85.0)

    server._fuse_camera_ball_flight(shot, capture)
    server._fuse_camera_club_delivery(shot, capture)

    assert geometries["ball"].camera_forward_offset_m == forward
    assert geometries["club"].camera_forward_offset_m == forward
    assert geometries["ball"].yaw_from_reference_ball is yaw
