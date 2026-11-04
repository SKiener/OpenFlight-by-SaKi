"""Driver tee height: per-profile setting, per-shot ball height for the TI and camera."""

import json
import math
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pytest

from openflight import server
from openflight.camera.ball_flight import CameraBallGeometry, _CameraModel
from openflight.camera.club_head_path import _ground_forward
from openflight.clubs import ClubType
from openflight.iwr6843.calibration import Calibration
from openflight.iwr6843.runtime import IWR6843Runtime
from openflight.launch_monitor import Shot
from openflight.profiles import DEFAULT_DRIVER_TEE_HEIGHT_M, ProfileStore, driver_tee_height_m

BASE_M = 0.02135


# -- profile store -----------------------------------------------------------


def test_default_is_four_centimetres():
    assert DEFAULT_DRIVER_TEE_HEIGHT_M == 0.04
    assert driver_tee_height_m({}) == 0.04
    assert driver_tee_height_m(None) == 0.04


@pytest.mark.parametrize("value", [0, 0.0, 0.04, 0.055, 0.08])
def test_setter_persists_valid_heights_and_keeps_other_settings(tmp_path, value):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    profile = store.get_active()
    assert store.set_units(profile.id, "metric")
    assert store.set_driver_tee_height(profile.id, value)
    settings = ProfileStore(path).get_active().settings
    assert settings == {"unit_system": "metric", "driver_tee_height_m": float(value)}
    assert isinstance(settings["driver_tee_height_m"], float)


@pytest.mark.parametrize(
    "invalid", [-0.001, 0.0801, 1, math.nan, math.inf, -math.inf, "0.04", None, True, [], {}]
)
def test_setter_rejects_invalid_heights_without_writing(tmp_path, invalid):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    original = path.read_bytes()
    assert not store.set_driver_tee_height(store.get_active().id, invalid)
    assert path.read_bytes() == original
    assert "driver_tee_height_m" not in store.get_active().settings


def test_setter_rejects_an_unknown_profile(tmp_path):
    assert not ProfileStore(tmp_path / "profiles.json").set_driver_tee_height("ghost", 0.05)


@pytest.mark.parametrize("stored", [-0.01, 0.2, "0.05", None, True, math.nan])
def test_invalid_stored_value_reads_as_default(stored):
    assert driver_tee_height_m({"driver_tee_height_m": stored}) == 0.04


# -- server helpers ----------------------------------------------------------


@pytest.fixture
def tee_server(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(server, "profile_store", store)
    monkeypatch.setattr(server.socketio, "emit", Mock())
    monkeypatch.setattr(
        server,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=1.524, radar_height_m=0.15875, tee_ball_height_m=BASE_M
            )
        ),
    )
    return store


def _shot(club=ClubType.DRIVER, selection=None, profile_id=""):
    shot = Shot(
        ball_speed_mph=140.0,
        timestamp=datetime.now(),
        club_speed_mph=95.0,
        impact_timestamp=100.0,
        club=club,
        club_selection=selection,
    )
    shot.profile_id = profile_id
    return shot


def test_driver_shot_adds_the_default_tee(tee_server):
    shot = _shot(profile_id=tee_server.get_active().id)
    centre, tee = server._shot_ball_height(shot)
    assert tee == 0.04
    assert centre == pytest.approx(BASE_M + 0.04)


def test_driver_selection_without_profile_falls_back_to_active(tee_server):
    tee_server.set_driver_tee_height(tee_server.get_active().id, 0.055)
    assert server._shot_ball_height(_shot(selection="driver", profile_id="")) == (
        pytest.approx(BASE_M + 0.055),
        0.055,
    )
    assert server._shot_ball_height(_shot(profile_id="deleted")) == (
        pytest.approx(BASE_M + 0.055),
        0.055,
    )


def test_shot_profile_wins_over_the_active_one(tee_server):
    first = tee_server.get_active().id
    tee_server.set_driver_tee_height(first, 0.03)
    second = tee_server.add("Guest")
    tee_server.set_driver_tee_height(second.id, 0.06)
    assert tee_server.get_active().id == second.id
    assert server._shot_ball_height(_shot(profile_id=first)) == (pytest.approx(BASE_M + 0.03), 0.03)


@pytest.mark.parametrize(
    ("club", "selection"),
    [
        (ClubType.IRON_7, None),
        (ClubType.IRON_7, "7-iron"),
        (ClubType.DRIVER, "mini-driver"),
        (ClubType.WOOD_3, None),
        (ClubType.PW, "wedge-48"),
    ],
)
def test_other_clubs_keep_the_base_height(tee_server, club, selection):
    tee_server.set_driver_tee_height(tee_server.get_active().id, 0.06)
    centre, tee = server._shot_ball_height(_shot(club, selection, tee_server.get_active().id))
    assert centre is BASE_M or centre == BASE_M
    assert tee == 0.0


def test_invalid_stored_value_uses_default(tee_server):
    tee_server.get_active().settings["driver_tee_height_m"] = "high"
    assert server._shot_ball_height(_shot(profile_id=tee_server.get_active().id))[1] == 0.04


def test_explicit_base_height(tee_server):
    assert server._shot_ball_height(_shot(ClubType.IRON_7), 0.03) == (0.03, 0.0)
    assert server._shot_ball_height(_shot(), 0.03) == (pytest.approx(0.07), 0.04)


def test_socket_handler_saves_and_emits(tee_server):
    profile_id = tee_server.get_active().id
    result = server.handle_set_profile_driver_tee(
        {"profile_id": profile_id, "driver_tee_height_m": 0.045}
    )
    assert result == {"ok": True}
    assert ProfileStore(tee_server._path).get_active().settings["driver_tee_height_m"] == 0.045
    server.socketio.emit.assert_any_call("profiles", server._profiles_payload())


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"driver_tee_height_m": 0.05}, {"profile_id": "x", "driver_tee_height_m": 0.05}],
)
def test_socket_handler_rejects_and_still_emits(tee_server, payload):
    assert server.handle_set_profile_driver_tee(payload) == {"ok": False}
    server.socketio.emit.assert_any_call("profiles", server._profiles_payload())


def test_socket_handler_rejects_out_of_range(tee_server):
    profile_id = tee_server.get_active().id
    assert server.handle_set_profile_driver_tee(
        {"profile_id": profile_id, "driver_tee_height_m": 0.09}
    ) == {"ok": False}
    assert "driver_tee_height_m" not in tee_server.get_active().settings


# -- TI runtime --------------------------------------------------------------


class _Capture:
    valid = True
    raw = b"x" * 32
    path = None
    error = None
    trigger_timestamp = 1.0
    dump_duration_s = 5.3
    sequence = 1


class _Monitor:
    def capture_for_shot(self, _ts, timeout_s):  # pylint: disable=unused-argument
        return _Capture()


def _runtime(calibration):
    return IWR6843Runtime(capture_monitor=_Monitor(), calibration=calibration, net_range_m=4.064)


@pytest.fixture
def _prepared():
    with patch(
        "openflight.iwr6843.runtime.prepare_lcmf_capture",
        return_value=SimpleNamespace(vertical=object()),
    ):
        yield


@pytest.mark.usefixtures("_prepared")
def test_process_shot_uses_a_calibration_copy_with_the_ball_height():
    calibration = Calibration.identity()
    calibration.tee_ball_height_m = BASE_M
    runtime = _runtime(calibration)
    seen = []

    def fake_lcmf(_raw, shot_calibration, **_kwargs):
        seen.append(shot_calibration)

    with patch("openflight.iwr6843.runtime.estimate_lcmf_v1", side_effect=fake_lcmf):
        runtime.process_shot(
            impact_timestamp=1.0, ball_speed_mph=140.0, club="driver", ball_height_m=0.06135
        )
        runtime.process_shot(
            impact_timestamp=1.0,
            ball_speed_mph=140.0,
            club="driver",
            tilt_deg=12.0,
            ball_height_m=0.07,
        )

    assert seen[0] is not calibration
    assert seen[0].tee_ball_height_m == 0.06135
    assert seen[0].tilt_rad == calibration.tilt_rad
    assert seen[1].tee_ball_height_m == 0.07
    assert math.degrees(seen[1].tilt_rad) == pytest.approx(12.0)
    assert runtime.calibration is calibration
    assert calibration.tee_ball_height_m == BASE_M
    assert calibration.tilt_rad == 0.0


@pytest.mark.usefixtures("_prepared")
def test_process_shot_without_ball_height_passes_the_runtime_calibration():
    calibration = Calibration.identity()
    runtime = _runtime(calibration)
    seen = []
    with patch(
        "openflight.iwr6843.runtime.estimate_lcmf_v1",
        side_effect=lambda _raw, cal, **_kw: seen.append(cal),
    ):
        runtime.process_shot(impact_timestamp=1.0, ball_speed_mph=100.0, club="7-iron")
    assert seen[0] is calibration


@pytest.mark.usefixtures("_prepared")
def test_every_downstream_estimate_sees_the_ball_height_copy():
    from openflight.iwr6843.club import ClubPathResult  # noqa: PLC0415
    from openflight.iwr6843.lcmf import LCMFResult  # noqa: PLC0415

    calibration = Calibration.identity()
    runtime = _runtime(calibration)
    seen = {}
    baseline = LCMFResult(status="accepted", angle_deg=12.0, track_speed_mph=140.0)

    def ops_guided(_self, _raw, cal, **kwargs):
        seen["ops_guided"] = cal
        return kwargs["baseline"]

    def club_path(_raw, cal, **_kwargs):
        seen["club_path"] = cal
        return ClubPathResult(status="accepted", path_deg=1.0, confidence=0.8)

    with (
        patch("openflight.iwr6843.runtime.estimate_lcmf_v1", return_value=baseline),
        patch.object(IWR6843Runtime, "_ops_guided_measurement", ops_guided),
        patch.object(IWR6843Runtime, "_recover_impact_time", lambda *_a, **_kw: 0.01),
        patch("openflight.iwr6843.runtime.estimate_club_path", side_effect=club_path),
    ):
        runtime.process_shot(
            impact_timestamp=1.0,
            ball_speed_mph=140.0,
            club="driver",
            club_speed_mph=95.0,
            ball_height_m=0.06,
        )

    assert seen["ops_guided"].tee_ball_height_m == 0.06
    assert seen["club_path"].tee_ball_height_m == 0.06
    assert calibration.tee_ball_height_m == 0.04


# -- server wiring -----------------------------------------------------------


def _ti_runtime(seen):
    return SimpleNamespace(
        calibration=SimpleNamespace(
            tee_range_m=1.524, radar_height_m=0.15875, tee_ball_height_m=BASE_M, tilt_rad=0.2
        ),
        process_shot=lambda **kwargs: (
            seen.append(kwargs) or SimpleNamespace(capture=None, measurement=None)
        ),
    )


@pytest.mark.parametrize(
    ("club", "selection", "expected_height", "expected_tee"),
    [
        (ClubType.DRIVER, None, BASE_M + 0.04, 0.04),
        (ClubType.IRON_7, "7-iron", None, 0.0),
        (ClubType.DRIVER, "mini-driver", None, 0.0),
    ],
)
def test_process_iwr6843_angle_passes_the_shot_ball_height(
    tee_server, monkeypatch, club, selection, expected_height, expected_tee
):
    seen, logged = [], []
    monkeypatch.setattr(server, "iwr6843_runtime", _ti_runtime(seen))
    session = SimpleNamespace(
        stats={"shots_detected": 0}, log_iwr6843_capture=lambda **kw: logged.append(kw)
    )
    monkeypatch.setattr(server, "get_session_logger", lambda: session)
    monkeypatch.setattr(server, "_emit_iwr6843_trigger_status", lambda *_a, **_kw: None)
    server._process_iwr6843_angle(_shot(club, selection, tee_server.get_active().id))

    if expected_height is None:
        assert seen[0]["ball_height_m"] is None
        assert logged[0]["ball_height_m"] == BASE_M
    else:
        assert seen[0]["ball_height_m"] == pytest.approx(expected_height)
        assert logged[0]["ball_height_m"] == pytest.approx(expected_height)
    assert logged[0]["tee_height_m"] == expected_tee


@pytest.mark.parametrize(
    ("club", "selection", "expected_height", "expected_tee"),
    [
        (ClubType.DRIVER, None, BASE_M + 0.04, 0.04),
        (ClubType.IRON_7, None, BASE_M, 0.0),
        (ClubType.DRIVER, "mini-driver", BASE_M, 0.0),
    ],
)
def test_camera_geometries_get_the_shot_ball_height(
    tee_server, monkeypatch, tmp_path, club, selection, expected_height, expected_tee
):
    from openflight.camera import ball_flight, club_delivery  # noqa: PLC0415

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
        server, "camera_capture_config", {"mount_height_m": 0.20955, "width": 640, "height": 400}
    )
    capture = SimpleNamespace(valid=True, path=tmp_path, metadata={})
    shot = _shot(club, selection, tee_server.get_active().id)

    server._fuse_camera_ball_flight(shot, capture)
    server._fuse_camera_club_delivery(shot, capture)

    for name in ("ball", "club"):
        assert geometries[name].ball_height_m == pytest.approx(expected_height), name
        assert geometries[name].tee_height_m == expected_tee, name
    if expected_tee == 0.0:
        # Non-driver shots build exactly the geometry they did before.
        assert geometries["ball"].ball_height_m is BASE_M
        assert geometries["club"].ball_height_m is BASE_M


def test_lowlight_job_geometry_gets_the_shot_ball_height(tee_server, monkeypatch):
    monkeypatch.setattr(
        server, "camera_capture_config", {"mount_height_m": 0.20955, "width": 640, "height": 400}
    )
    capture = SimpleNamespace(valid=True, path=None, metadata={})
    geometry = server._camera_ball_geometry(
        capture,
        server.iwr6843_runtime.calibration,
        target_px=None,
        shot=_shot(profile_id=tee_server.get_active().id),
    )
    assert geometry.ball_height_m == pytest.approx(BASE_M + 0.04)
    assert geometry.tee_height_m == 0.04


# -- camera ground map -------------------------------------------------------


def _geometry(ball_height_m, tee_height_m=0.0):
    return CameraBallGeometry(
        camera_height_m=0.20955,
        radar_height_m=0.15875,
        tee_range_m=1.524,
        ball_height_m=ball_height_m,
        tee_height_m=tee_height_m,
    )


_MODEL = _CameraModel(
    focal_px=520.0, pitch_rad=math.radians(-6.0), radar_from_camera=np.zeros(3), yaw_rad=0.0
)
_ROI = (200, 150, 440, 400)


def test_ground_map_is_unchanged_without_a_tee():
    geometry = _geometry(BASE_M)
    assert geometry.tee_height_m == 0.0
    # The pre-tee expression, verbatim: the ball's bottom is the floor.
    drop = geometry.ball_height_m - geometry.ball_diameter_m / 2.0 - geometry.camera_height_m
    ground = _ground_forward(_ROI, _MODEL, geometry)
    from openflight.camera.ball_flight import _normalized  # noqa: PLC0415
    from openflight.camera.geometry import camera_to_world  # noqa: PLC0415

    ys, xs = np.mgrid[_ROI[1] : _ROI[3], _ROI[0] : _ROI[2]]
    image_x, image_z = _normalized(
        xs.ravel().astype(float),
        ys.ravel().astype(float),
        focal_px=_MODEL.focal_px,
        geometry=geometry,
    )
    _lat, forward, vertical = camera_to_world(
        image_x, 1.0, image_z, pitch_rad=_MODEL.pitch_rad, yaw_rad=_MODEL.yaw_rad
    )
    descending = vertical < 0.0
    scale = np.divide(drop, vertical, out=np.full(vertical.shape, np.inf), where=descending)
    expected = np.where(descending, geometry.camera_forward_offset_m + scale * forward, np.inf)
    np.testing.assert_array_equal(ground, expected.reshape(ys.shape))


def test_ground_map_of_a_teed_ball_stays_on_the_floor():
    mat = _ground_forward(_ROI, _MODEL, _geometry(BASE_M))
    teed = _ground_forward(_ROI, _MODEL, _geometry(BASE_M + 0.05, tee_height_m=0.05))
    untreated = _ground_forward(_ROI, _MODEL, _geometry(BASE_M + 0.05))
    finite = np.isfinite(mat)
    assert finite.any()
    np.testing.assert_array_equal(np.isfinite(teed), finite)
    np.testing.assert_allclose(teed[finite], mat[finite], rtol=1e-12)
    # Without tee_height_m the plane would rise to the ball's bottom (shorter rays).
    assert np.all(untreated[finite] < mat[finite])


# -- session log -------------------------------------------------------------


@pytest.mark.parametrize(("height", "tee"), [(BASE_M + 0.04, 0.04), (BASE_M, 0.0)])
def test_capture_log_records_the_effective_ball_height(tmp_path, height, tee):
    from openflight.session_logger import SessionLogger  # noqa: PLC0415

    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session(mode="rolling-buffer", trigger_type="sound")
    logger.log_iwr6843_capture(
        shot_number=1,
        shot_timestamp=100.0,
        trigger_timestamp=100.01,
        capture_path=None,
        capture_bytes=0,
        dump_duration_s=None,
        capture_error=None,
        ball_speed_mph=140.0,
        ball_height_m=height,
        tee_height_m=tee,
    )
    entry = json.loads(logger.session_path.read_text().strip().split("\n")[-1])
    assert entry["ball_height_m"] == height
    assert entry["tee_height_m"] == tee


# -- camera pose source ------------------------------------------------------


def test_ground_anchor_pose_uses_the_mat_height_on_a_teed_shot():
    from openflight.camera.ball_flight import _pose_geometry  # noqa: PLC0415

    teed = _geometry(BASE_M + 0.04, tee_height_m=0.04)
    mat = _pose_geometry(teed, "ground_anchor")
    assert mat.ball_height_m == pytest.approx(BASE_M)
    assert mat.tee_height_m == 0.0
    # This shot's own (teed) ball keeps the effective height.
    for source in ("teed_ball", "this_ball"):
        assert _pose_geometry(teed, source) is teed
    # Untee'd shots are untouched whatever the pose source.
    flat = _geometry(BASE_M)
    assert _pose_geometry(flat, "ground_anchor") is flat
