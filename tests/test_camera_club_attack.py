"""Tests for the arc-consistent camera attack angle and low point (club_attack)."""

import itertools
import json
import math
from dataclasses import asdict, replace
from types import SimpleNamespace

import numpy as np
import pytest

from openflight.camera import club_attack as ca
from openflight.camera.ball_flight import CameraBallGeometry, _CameraModel, _normalized
from openflight.camera.club_head_path import (
    HEAD_BEHIND_BALL_M,
    HeadObservation,
    HeadQuality,
    _fit_path,
    _lateral_on_plane,
)

FPS = 287.885
DT = 1.0 / FPS
FOCAL_PX = 576.48
WIDTH, HEIGHT = 640, 400
PITCH_DEG = -4.0
YAW_DEG = 0.3
# Legacy pinhole (no intrinsics, no roll): pixels = focal * offsets + image centre.
GEOMETRY = CameraBallGeometry(
    camera_height_m=0.182,
    radar_height_m=0.132,
    tee_range_m=1.5,
    ball_height_m=0.02135,
    camera_lateral_offset_m=-0.025,
    camera_forward_offset_m=0.007,
    image_width_px=WIDTH,
    image_height_px=HEIGHT,
)
OK = HeadQuality("ok", 60.0, 300, 30.0, 2.0, 0.0)
BLURRED = HeadQuality("blurred", 60.0, 300, 30.0, 9.0, 0.0)
# Exact (noise-free) arcs are fitted at the pixel noise floor, as in the aoa-arc study's
# noise-free runs: with the production ARC_PIXEL_SIGMA_PX (0.5) the swing-radius and
# plane priors pull a slow, off-prior arc (60 mph, R 2.0 m, plane 45 deg) by up to 0.19 deg.
EXACT_SIGMA_PX = ca.POSITION_NOISE_FLOOR_PX
IRON = {"speed_mph": 80.0, "path_deg": 0.0, "attack_deg": -6.0, "plane_deg": 56.0, "radius_m": 1.4}


def scene(pitch_deg=PITCH_DEG, yaw_deg=YAW_DEG, geometry=GEOMETRY):
    model = _CameraModel(
        FOCAL_PX,
        math.radians(pitch_deg),
        geometry.camera_origin - np.array([0.0, 0.0, geometry.radar_height_m]),
        math.radians(yaw_deg),
        direction_offset_deg=geometry.horizontal_offset_deg,
    )
    return SimpleNamespace(model=model, geometry=geometry)


def camera(pitch_deg=PITCH_DEG, yaw_deg=YAW_DEG, geometry=GEOMETRY):
    return ca.ArcCamera.from_scene(scene(pitch_deg, yaw_deg, geometry))


CAMERA = camera()


def frame_times(n_frames=6, last_s=-1.0e-3):
    return last_s - DT * np.arange(n_frames)[::-1]


def world_track(
    tau_s,
    *,
    speed_mph,
    path_deg,
    attack_deg,
    plane_deg,
    radius_m,
    contact_height_m=0.015,
    cam=CAMERA,
):
    contact = np.array([0.0, cam.contact_forward_m, contact_height_m])
    return ca.arc_points(
        contact,
        math.radians(path_deg),
        math.radians(attack_deg),
        math.radians(plane_deg),
        1.0 / radius_m,
        speed_mph / ca.MPH_PER_MS * tau_s,
    )


def offsets(tau_s, cam=CAMERA, **truth):
    return ca.world_to_normalized(world_track(tau_s, cam=cam, **truth), cam)


def to_pixels(image_x, image_z):
    return FOCAL_PX * image_x + WIDTH / 2.0, -FOCAL_PX * image_z + HEIGHT / 2.0


def observations(
    n_frames=6,
    last_s=-1.0e-3,
    *,
    truth_camera=CAMERA,
    contact_height_m=0.015,
    mirror=False,
    centroid_rise_m=0.0,
    noise_px=0.0,
    rng=None,
    **truth,
):
    """Head observations of an exact arc, seen by ``truth_camera``."""
    truth = {**IRON, **truth}
    tau = frame_times(n_frames, last_s)
    points = world_track(tau, contact_height_m=contact_height_m, cam=truth_camera, **truth)
    if mirror:
        points[:, 0] = -points[:, 0]
    features = {}
    # The centroid rises by centroid_rise_m over the track relative to the face centre.
    ramp = np.linspace(0.0, 1.0, n_frames)
    for name, rise in (("face_centre", 0.0), ("centroid", centroid_rise_m)):
        shifted = points.copy()
        shifted[:, 2] += rise * ramp
        px, py = to_pixels(*ca.world_to_normalized(shifted, truth_camera))
        if noise_px:
            px = px + rng.normal(0.0, noise_px, px.shape)
            py = py + rng.normal(0.0, noise_px, py.shape)
        features[name] = np.column_stack((px, py))
    return [
        HeadObservation(
            frame_index=index,
            t_rel_ms=float(tau[index] * 1000.0),
            features_px={name: tuple(map(float, xy[index])) for name, xy in features.items()},
            quality=OK,
        )
        for index in range(n_frames)
    ]


def estimate(obs, *, cam=CAMERA, geometry=GEOMETRY, **kwargs):
    kwargs.setdefault("club_speed_mph", IRON["speed_mph"])
    kwargs.setdefault("contact_sigma_s", 0.5e-3)
    return ca.estimate_attack_angle(obs, camera=cam, geometry=geometry, **kwargs)


def true_low_point_m(*, horizontal_offset_deg=0.0, cam=CAMERA, **truth):
    """Lowest point of the true circle by dense sampling, along the target line."""
    radius = truth["radius_m"]
    arc = np.linspace(-math.pi * radius, math.pi * radius, 400_001)
    contact = np.array([0.0, cam.contact_forward_m, 0.015])
    points = ca.arc_points(
        contact,
        math.radians(truth["path_deg"]),
        math.radians(truth["attack_deg"]),
        math.radians(truth["plane_deg"]),
        1.0 / radius,
        arc,
    )
    offset = points[int(np.argmin(points[:, 2]))] - contact
    rad = math.radians(horizontal_offset_deg)
    return float(-offset[0] * math.sin(rad) + offset[1] * math.cos(rad))


# ---------------------------------------------------------------- constants and camera


def test_constants():
    assert ca.SWING_RADIUS_PRIOR_M == 1.0
    assert ca.CURVATURE_PRIOR_SIGMA_PER_M == 0.3
    assert ca.PLANE_PRIOR_DEG == 60.0
    assert ca.PLANE_PRIOR_SIGMA_DEG == 20.0
    assert ca.ARC_PIXEL_SIGMA_PX == 0.5
    assert ca.POSITION_NOISE_FLOOR_PX == 0.3
    assert ca.MAX_AOA_SIGMA_DEG == 1.5
    assert ca.AOA_RANGE_DEG == (-15.0, 12.0)
    assert ca.RADIUS_RANGE_M == (0.5, 3.0)
    assert ca.PLANE_RANGE_DEG == (20.0, 85.0)
    assert ca.CONTACT_HEIGHT_RANGE_M == (-0.008, 0.050)
    assert ca.MAX_RMS_PX == 1.5
    assert ca.SPEED_SIGMA_MPH == 1.5
    assert ca.JACOBIAN_INFLATION == {4: 1.5, 5: 1.35, 6: 1.25}
    assert ca.JACKKNIFE_WHITE_NOISE_RATIO == {5: 1.58, 6: 1.39}
    assert ca.CONTACT_GAIN_STEP_S == 0.5e-3
    assert ca.SPEED_GAIN_STEP_MPH == 2.0
    assert ca.ATTACK_METHOD == "arc_v1"


def test_arc_camera_from_scene_uses_the_club_path_pose_and_contact_point():
    cam = ca.ArcCamera.from_scene(scene())
    assert cam.focal_px == FOCAL_PX
    assert cam.pitch_rad == pytest.approx(math.radians(PITCH_DEG))
    assert cam.yaw_rad == pytest.approx(math.radians(YAW_DEG))
    np.testing.assert_array_equal(cam.camera_origin, GEOMETRY.camera_origin)
    assert cam.contact_forward_m == pytest.approx(GEOMETRY.ball_forward_m - HEAD_BEHIND_BALL_M)


def test_world_to_normalized_inverts_plane_ray_points():
    tau = frame_times()
    points = world_track(tau, **IRON)
    image_x, image_z = ca.world_to_normalized(points, CAMERA)
    back = ca.plane_ray_points(image_x, image_z, points[:, 1], CAMERA)
    np.testing.assert_allclose(back, points, atol=1e-12)


# ---------------------------------------------------------------- 1-3 exact arcs


@pytest.mark.parametrize(
    "speed_mph,radius_m,plane_deg,attack_deg,path_deg",
    list(itertools.product((60, 80, 100), (0.8, 1.4, 2.0), (45, 56, 75), (-8, -4, 3), (-5, 5))),
)
def test_exact_arc_grid_recovers_attack_path_and_radius(
    speed_mph, radius_m, plane_deg, attack_deg, path_deg
):
    truth = {
        "speed_mph": speed_mph,
        "path_deg": path_deg,
        "attack_deg": attack_deg,
        "plane_deg": plane_deg,
        "radius_m": radius_m,
    }
    tau = frame_times()
    image_x, image_z = offsets(tau, **truth)
    fit = ca.fit_attack_arc(
        tau,
        image_x,
        image_z,
        speed_ms=speed_mph / ca.MPH_PER_MS,
        camera=CAMERA,
        sigma_px=EXACT_SIGMA_PX,
    )
    assert fit.success
    assert abs(fit.attack_deg - attack_deg) < 0.1
    assert abs(fit.path_deg - path_deg) < 0.1
    assert fit.radius_m == pytest.approx(radius_m, rel=0.02)


@pytest.mark.parametrize("path_deg", (-4.0, 0.0, 4.0))
@pytest.mark.parametrize("radius_m", (1.0, 1.4))
def test_the_arc_path_of_an_exact_arc_is_within_a_tenth_of_a_degree(path_deg, radius_m):
    obs = observations(path_deg=path_deg, radius_m=radius_m)

    result = estimate(obs)
    alone = ca.estimate_arc_path(
        obs,
        camera=CAMERA,
        geometry=GEOMETRY,
        club_speed_mph=IRON["speed_mph"],
        contact_sigma_s=5e-4,
    )

    assert alone.status == result.arc_path_status == "accepted"
    assert alone.path_deg == pytest.approx(path_deg, abs=0.1)
    # The attack fit and the path-only estimate are the same measurement.
    assert (alone.path_deg, alone.sigma_deg) == (
        result.arc_path_deg,
        result.arc_path_total_sigma_deg,
    )
    assert alone.feature_sigma_deg == result.arc_path_feature_sigma_deg


def test_the_arc_path_sigma_is_the_quadrature_of_its_terms():
    obs = observations(centroid_rise_m=0.002, noise_px=0.3, rng=np.random.default_rng(3))

    arc = ca.estimate_arc_path(
        obs,
        camera=CAMERA,
        geometry=GEOMETRY,
        club_speed_mph=IRON["speed_mph"],
        contact_sigma_s=5e-4,
    )

    assert arc.sigma_deg == pytest.approx(
        math.sqrt(
            arc.stat_sigma_deg**2
            + arc.feature_sigma_deg**2
            + (arc.contact_gain_deg_per_ms * 0.5) ** 2
            + (arc.speed_gain_deg_per_mph * ca.SPEED_SIGMA_MPH) ** 2
        ),
        abs=0.02,
    )
    assert arc.jackknife_sigma_deg is not None


def test_the_arc_path_ignores_the_attack_angle_gates():
    """A steep attack angle fails the attack's own gate, not the path's."""
    result = estimate(observations(attack_deg=-20.0))

    assert result.status == "rejected_implausible_attack"
    assert result.arc_path_status == "accepted"
    assert result.arc_path_total_sigma_deg is not None


def test_the_arc_path_keeps_the_fit_gates():
    result = estimate(observations(radius_m=4.0))

    assert result.status == result.arc_path_status == "rejected_implausible_radius"
    assert result.arc_path_total_sigma_deg is None


def test_the_arc_path_estimate_never_raises():
    kwargs = {
        "camera": CAMERA,
        "geometry": GEOMETRY,
        "club_speed_mph": IRON["speed_mph"],
        "contact_sigma_s": 5e-4,
    }
    short = observations(n_frames=3)
    missing = [replace(item, features_px={}) for item in observations()]

    assert ca.estimate_arc_path(short, **kwargs) == ca.ArcPath("rejected_short_track")
    assert ca.estimate_arc_path(missing, **kwargs) == ca.ArcPath("rejected_fit_failed")


def test_the_arc_path_windows_leave_out_the_last_and_the_first_frame():
    kwargs = {"camera": CAMERA, "geometry": GEOMETRY, "club_speed_mph": IRON["speed_mph"]}

    windows = ca.arc_path_windows(observations(path_deg=2.0), **kwargs)

    assert set(windows) == {"drop_last", "drop_first"}
    assert windows["drop_last"] == pytest.approx(2.0, abs=0.1)
    assert windows["drop_first"] == pytest.approx(2.0, abs=0.1)
    assert ca.arc_path_windows(observations(n_frames=4), **kwargs) == {}
    missing = [replace(item, features_px={}) for item in observations()]
    assert ca.arc_path_windows(missing, **kwargs) == {}


def test_quadratic_attack_is_steep_where_the_arc_fit_is_exact():
    truth = {"speed_mph": 80.0, "path_deg": 0.0, "attack_deg": -4.0, "plane_deg": 60.0}
    tau = frame_times(6, last_s=-2.5e-3)
    image_x, image_z = offsets(tau, radius_m=1.0, **truth)
    speed_ms = 80.0 / ca.MPH_PER_MS
    quadratic = ca.quadratic_attack_deg(tau, image_x, image_z, speed_ms=speed_ms, camera=CAMERA)
    fit = ca.fit_attack_arc(
        tau, image_x, image_z, speed_ms=speed_ms, camera=CAMERA, sigma_px=EXACT_SIGMA_PX
    )
    assert quadratic.attack_deg <= -4.0 - 2.5
    assert abs(fit.attack_deg + 4.0) < 0.05


def test_radius_off_the_prior_still_recovers_the_attack_angle():
    truth = {**IRON, "radius_m": 1.5, "speed_mph": 75.0, "attack_deg": -4.0, "plane_deg": 60.0}
    tau = frame_times()
    image_x, image_z = offsets(tau, **truth)
    fit = ca.fit_attack_arc(
        tau,
        image_x,
        image_z,
        speed_ms=75.0 / ca.MPH_PER_MS,
        camera=CAMERA,
        sigma_px=EXACT_SIGMA_PX,
        radius_prior_m=1.0,
    )
    assert abs(fit.attack_deg + 4.0) < 0.15
    assert fit.radius_m == pytest.approx(1.5, rel=0.05)


# ---------------------------------------------------------------- 4 contact gain


def test_contact_gain_and_contact_sigma():
    result = estimate(observations(radius_m=1.4), contact_sigma_s=0.5e-3)
    assert 0.8 <= result.contact_gain_deg_per_ms <= 1.4
    assert result.contact_sigma_deg == pytest.approx(
        abs(result.contact_gain_deg_per_ms) * 0.5e-3 * 1e3, abs=0.01
    )
    doubled = estimate(observations(radius_m=1.4), contact_sigma_s=1.0e-3)
    assert doubled.contact_sigma_deg == pytest.approx(2 * result.contact_sigma_deg, abs=0.01)


# ---------------------------------------------------------------- 5 sigma calibration


@pytest.mark.parametrize("n_frames", [4, 5, 6])
def test_statistical_sigma_is_calibrated_under_white_noise(n_frames):
    rng = np.random.default_rng(3)
    speed_ms = IRON["speed_mph"] / ca.MPH_PER_MS
    pulls = []
    for _ in range(300):
        tau = frame_times(n_frames, last_s=-rng.uniform(0.2e-3, DT + 0.2e-3))
        image_x, image_z = offsets(tau, **IRON)
        image_x = image_x + rng.normal(0.0, 0.5 / FOCAL_PX, image_x.shape)
        image_z = image_z + rng.normal(0.0, 0.5 / FOCAL_PX, image_z.shape)
        fit = ca.fit_attack_arc(tau, image_x, image_z, speed_ms=speed_ms, camera=CAMERA)
        refits = ca.jackknife_fits(tau, image_x, image_z, speed_ms=speed_ms, camera=CAMERA)
        jackknife = ca._jackknife_spread([item.attack_deg for item in refits]) if refits else None
        stat = ca.statistical_sigma(fit.stat_sigma_deg, jackknife, n_frames)
        pulls.append((fit.attack_deg - IRON["attack_deg"]) / stat)
    assert 0.85 <= float(np.std(pulls)) <= 1.2


def test_statistical_sigma_takes_the_larger_term():
    assert ca.statistical_sigma(1.0, None, 4) == pytest.approx(1.5)
    assert ca.statistical_sigma(1.0, 0.1, 6) == pytest.approx(1.25)
    assert ca.statistical_sigma(1.0, 3.0, 5) == pytest.approx(3.0 / 1.58)
    assert ca.statistical_sigma(1.0, None, 9) == pytest.approx(1.25)


# ---------------------------------------------------------------- 6 pitch


def test_pitch_error_moves_attack_and_contact_height():
    obs = observations(contact_height_m=0.015)
    base = estimate(obs)
    pitched = estimate(obs, cam=camera(pitch_deg=PITCH_DEG + 1.0))
    assert base.accepted
    assert 0.95 <= pitched.attack_deg - base.attack_deg <= 1.05
    assert pitched.contact_height_mm - base.contact_height_mm == pytest.approx(25.0, abs=3.0)


def test_wrong_anchor_pitch_is_rejected_by_contact_height():
    obs = observations(contact_height_m=0.012)
    assert estimate(obs).accepted
    result = estimate(obs, cam=camera(pitch_deg=PITCH_DEG - 1.26))
    assert result.status == "rejected_contact_height"
    assert result.contact_height_mm < -8.0


# ---------------------------------------------------------------- 7 gates


@pytest.mark.parametrize("speed", [None, 0.0, -5.0, float("nan"), float("inf")])
def test_gate_no_club_speed(speed):
    assert estimate(observations(), club_speed_mph=speed).status == "rejected_no_club_speed"


def test_gate_short_track_counts_usable_frames_only():
    assert estimate(observations(n_frames=3)).status == "rejected_short_track"
    obs = observations(n_frames=5)
    obs[1] = replace(obs[1], quality=BLURRED)
    obs[3] = replace(obs[3], quality=BLURRED)
    result = estimate(obs)
    assert result.status == "rejected_short_track"
    assert result.n_frames == 3
    assert estimate([]).status == "rejected_short_track"


def test_gate_fit_failed_on_non_finite_positions():
    obs = observations()
    obs[2] = replace(obs[2], features_px={**obs[2].features_px, "face_centre": (math.nan, 1.0)})
    assert estimate(obs).status == "rejected_fit_failed"


def test_gate_fit_failed_on_a_missing_feature():
    obs = observations()
    obs[0] = replace(obs[0], features_px={"face_centre": obs[0].features_px["face_centre"]})
    assert estimate(obs).status == "rejected_fit_failed"


def test_gate_residual():
    obs = observations()
    x, y = obs[2].features_px["face_centre"]
    obs[2] = replace(obs[2], features_px={**obs[2].features_px, "face_centre": (x + 6.0, y - 6.0)})
    result = estimate(obs)
    assert result.status == "rejected_residual"
    assert result.rms_px > ca.MAX_RMS_PX


@pytest.mark.parametrize("radius_m", [0.4, 4.0])
def test_gate_implausible_radius(radius_m):
    assert estimate(observations(radius_m=radius_m)).status == "rejected_implausible_radius"


@pytest.mark.parametrize("plane_deg", [10.0, 95.0])
def test_gate_implausible_plane(plane_deg):
    assert estimate(observations(plane_deg=plane_deg)).status == "rejected_implausible_plane"


@pytest.mark.parametrize("height_m", [-0.02, 0.08])
def test_gate_contact_height(height_m):
    assert estimate(observations(contact_height_m=height_m)).status == "rejected_contact_height"


@pytest.mark.parametrize("attack_deg", [-18.0, 14.0])
def test_gate_implausible_attack(attack_deg):
    assert estimate(observations(attack_deg=attack_deg)).status == "rejected_implausible_attack"


def test_gate_sigma_too_large():
    result = estimate(observations(n_frames=4), contact_sigma_s=3.0e-3)
    assert result.status == "rejected_sigma_too_large"
    assert result.sigma_deg > ca.MAX_AOA_SIGMA_DEG


def test_woods_are_withheld_without_a_known_tee_height_and_skip_the_contact_height_gate():
    assert estimate(observations(), wood=True).status == "withheld_tee_height_unknown"
    teed = estimate(observations(contact_height_m=0.08), wood=True)
    assert teed.status == "withheld_tee_height_unknown"
    assert estimate(observations(), wood=True, tee_height_known=True).status == "accepted"
    rejected = estimate(observations(attack_deg=-18.0), wood=True)
    assert rejected.status == "rejected_implausible_attack"


def test_accepted_result_carries_every_diagnostic_and_serialises():
    result = estimate(observations(centroid_rise_m=0.002))
    assert result.accepted
    assert result.method == "arc_v1"
    assert result.n_frames == 6
    assert result.feature == "face_centre"
    for name in (
        "attack_deg",
        "sigma_deg",
        "radius_m",
        "plane_deg",
        "contact_height_mm",
        "rms_px",
        "arc_path_deg",
        "arc_path_sigma_deg",
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
    ):
        assert isinstance(getattr(result, name), float), name
    assert result.feature_sigma_deg > 0.0
    assert set(result.feature_attacks_deg) == {"face_centre", "centroid"}
    expected_sigma = math.sqrt(
        result.stat_sigma_deg**2
        + result.feature_sigma_deg**2
        + result.contact_sigma_deg**2
        + result.speed_sigma_deg**2
    )
    assert result.sigma_deg == pytest.approx(expected_sigma, abs=0.02)
    json.dumps(asdict(result))


def test_centroid_feature_uses_face_centre_as_its_alternative():
    result = estimate(observations(centroid_rise_m=0.002), feature="centroid")
    assert result.feature == "centroid"
    assert set(result.feature_attacks_deg) == {"centroid", "face_centre"}


def test_arc_path_carries_the_horizontal_offset_like_the_club_path():
    obs = observations(path_deg=2.0)
    plain = estimate(obs)
    geometry = replace(GEOMETRY, horizontal_offset_deg=1.5)
    shifted = estimate(obs, cam=camera(geometry=geometry), geometry=geometry)
    assert shifted.arc_path_deg == pytest.approx(plain.arc_path_deg + 1.5, abs=0.011)
    assert shifted.attack_deg == plain.attack_deg


def test_arc_path_and_low_point_follow_the_scene_direction_offset():
    # A tapped target sets the scene's direction offset (-target azimuth) while the
    # geometry keeps its horizontal offset: the arc path and the low point follow the
    # scene, the frame of the ball's start direction.
    truth = {**IRON, "path_deg": 4.0}
    obs = observations(**truth)
    geometry = replace(GEOMETRY, horizontal_offset_deg=1.5)
    model = scene(geometry=geometry).model
    aimed_scene = SimpleNamespace(
        model=model._replace(direction_offset_deg=-3.0), geometry=geometry
    )
    plain = estimate(obs)
    aimed = estimate(obs, cam=ca.ArcCamera.from_scene(aimed_scene), geometry=geometry)
    assert aimed.arc_path_deg == pytest.approx(plain.arc_path_deg - 3.0, abs=0.011)
    assert aimed.attack_deg == plain.attack_deg
    expected = true_low_point_m(horizontal_offset_deg=-3.0, **truth)
    assert abs(aimed.low_point_cm / 100.0 - expected) < 0.005


# ---------------------------------------------------------------- 8 left-handed


def test_mirrored_left_handed_track_gives_the_same_attack_angle():
    right = estimate(observations(path_deg=3.0))
    left = estimate(observations(path_deg=3.0, mirror=True), left_handed=True)
    assert right.accepted and left.accepted
    assert abs(left.attack_deg - right.attack_deg) < 0.05
    assert left.plane_deg == pytest.approx(180.0 - right.plane_deg, abs=0.5)
    assert left.low_point_cm == pytest.approx(right.low_point_cm, abs=0.1)


def test_left_handed_track_with_the_right_handed_setting_fails_the_plane_gate():
    status = estimate(observations(path_deg=3.0, mirror=True)).status
    assert status == "rejected_implausible_plane"


# ---------------------------------------------------------------- 9 quadratic baseline


def test_quadratic_lateral_slope_equals_the_club_path_fit():
    obs = observations(path_deg=4.0)
    tau = np.asarray([item.t_rel_ms / 1000.0 for item in obs])
    points = np.asarray([item.features_px["face_centre"] for item in obs])
    speed_ms = IRON["speed_mph"] / ca.MPH_PER_MS
    image_x, image_z = _normalized(
        points[:, 0], points[:, 1], focal_px=CAMERA.focal_px, geometry=GEOMETRY
    )
    quadratic = ca.quadratic_attack_deg(
        tau, np.asarray(image_x), np.asarray(image_z), speed_ms=speed_ms, camera=CAMERA
    )
    model = scene().model
    forward = GEOMETRY.ball_forward_m - HEAD_BEHIND_BALL_M + speed_ms * tau
    lateral = _lateral_on_plane(points, forward, model=model, geometry=GEOMETRY)
    path = _fit_path(tau, lateral, forward_speed_ms=speed_ms, noise_floor_m=1e-4).path_deg
    assert quadratic.path_deg == pytest.approx(path, abs=1e-9)
    assert estimate(obs).quadratic_attack_deg == pytest.approx(quadratic.attack_deg, abs=0.006)


# ---------------------------------------------------------------- 10 window delta


def test_window_delta_never_changes_the_status(monkeypatch):
    rng = np.random.default_rng(11)
    obs = observations(noise_px=0.5, rng=rng)
    base = estimate(obs)
    monkeypatch.setattr(ca, "WINDOW_FRAMES", 5)
    other = estimate(obs)
    assert base.window_delta_deg != other.window_delta_deg
    assert replace(base, window_delta_deg=None) == replace(other, window_delta_deg=None)


def test_window_delta_needs_more_frames_than_the_window():
    four = estimate(observations(n_frames=4))
    assert four.window_delta_deg is None
    assert four.accepted
    assert estimate(observations(n_frames=5)).window_delta_deg is not None


# ---------------------------------------------------------------- low point (R4)


@pytest.mark.parametrize(
    "speed_mph,radius_m,plane_deg,attack_deg,path_deg",
    [
        (60.0, 0.8, 45.0, -8.0, -5.0),
        (80.0, 1.0, 60.0, -4.0, 0.0),
        (80.0, 1.4, 56.0, -6.0, 3.0),
        (100.0, 2.0, 75.0, -2.0, 5.0),
        (95.0, 1.3, 48.0, 3.0, -2.0),
    ],
)
def test_low_point_matches_the_exact_arc(speed_mph, radius_m, plane_deg, attack_deg, path_deg):
    truth = {
        "speed_mph": speed_mph,
        "path_deg": path_deg,
        "attack_deg": attack_deg,
        "plane_deg": plane_deg,
        "radius_m": radius_m,
    }
    tau = frame_times()
    image_x, image_z = offsets(tau, **truth)
    fit = ca.fit_attack_arc(
        tau,
        image_x,
        image_z,
        speed_ms=speed_mph / ca.MPH_PER_MS,
        camera=CAMERA,
        sigma_px=EXACT_SIGMA_PX,
    )
    assert abs(fit.low_point_m - true_low_point_m(**truth)) < 0.005


@pytest.mark.parametrize("horizontal_offset_deg", [-2.0, 2.5])
def test_low_point_is_measured_along_the_target_line(horizontal_offset_deg):
    truth = {**IRON, "path_deg": 4.0}
    tau = frame_times()
    image_x, image_z = offsets(tau, **truth)
    fit = ca.fit_attack_arc(
        tau,
        image_x,
        image_z,
        speed_ms=IRON["speed_mph"] / ca.MPH_PER_MS,
        camera=CAMERA,
        sigma_px=EXACT_SIGMA_PX,
        horizontal_offset_deg=horizontal_offset_deg,
    )
    expected = true_low_point_m(horizontal_offset_deg=horizontal_offset_deg, **truth)
    assert abs(fit.low_point_m - expected) < 0.005


def test_low_point_formula_on_known_geometry():
    # Level path, attack 0: the low point is the contact point itself.
    np.testing.assert_allclose(
        ca.low_point_offset_m(0.0, 0.0, math.radians(60.0), 1.0), 0.0, atol=1e-12
    )
    # Vertical swing plane (plane 90 deg), R 1 m, attack -5 deg: the lowest point lies
    # R * sin(5 deg) ahead and R * (1 - cos(5 deg)) below.
    offset = ca.low_point_offset_m(0.0, math.radians(-5.0), math.radians(90.0), 1.0)
    assert offset[1] == pytest.approx(math.sin(math.radians(5.0)), abs=1e-12)
    assert offset[2] == pytest.approx(-(1.0 - math.cos(math.radians(5.0))), abs=1e-12)
    assert offset[0] == pytest.approx(0.0, abs=1e-12)


def test_low_point_sign_ahead_for_a_descending_blow_behind_for_an_ascending_one():
    down = estimate(observations(attack_deg=-6.0))
    up = estimate(observations(attack_deg=3.0), wood=True)
    assert down.low_point_cm > 0.0
    assert up.low_point_cm < 0.0
    assert down.low_point_cm == pytest.approx(true_low_point_m(**IRON) * 100.0, abs=0.5)


def test_low_point_sigma_combines_its_terms_and_grows_with_the_contact_sigma():
    obs = observations(centroid_rise_m=0.002)
    tight = estimate(obs, contact_sigma_s=0.0)
    loose = estimate(obs, contact_sigma_s=2.0e-3)
    assert 0.0 < tight.low_point_sigma_cm < loose.low_point_sigma_cm


def test_low_point_jacobian_sigma_is_calibrated_like_the_attack_angle():
    rng = np.random.default_rng(3)
    speed_ms = IRON["speed_mph"] / ca.MPH_PER_MS
    truth_m = true_low_point_m(**IRON)
    pulls = []
    for _ in range(150):
        tau = frame_times(6, last_s=-rng.uniform(0.2e-3, DT + 0.2e-3))
        image_x, image_z = offsets(tau, **IRON)
        image_x = image_x + rng.normal(0.0, 0.5 / FOCAL_PX, image_x.shape)
        image_z = image_z + rng.normal(0.0, 0.5 / FOCAL_PX, image_z.shape)
        fit = ca.fit_attack_arc(tau, image_x, image_z, speed_ms=speed_ms, camera=CAMERA)
        sigma = ca.statistical_sigma(fit.low_point_stat_sigma_m, None, 6)
        pulls.append((fit.low_point_m - truth_m) / sigma)
    assert 0.75 <= float(np.std(pulls)) <= 1.3
