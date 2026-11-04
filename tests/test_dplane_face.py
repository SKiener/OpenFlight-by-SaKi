"""Calculated D-plane face data: face to target, face to path and spin-axis class."""

import json
import math

import pytest

import openflight.dplane as dpf
from openflight.clubs import ClubType
from openflight.dplane import MEAN_ATTACK_ANGLE_DEG, SPIN_AXIS_OFFSET_DEG, dplane_spin_axis

PW = "wedge-44"
# Start-direction sigma of a TI (IWR6843) launch direction against the camera on
# 2026-10-25 (residual sd 2.16 deg); not in LAUNCH_DIRECTION_SIGMA_DEG on purpose.
TI_LAUNCH_DIRECTION_SIGMA_DEG = 2.2


def face(club=PW, ld=-5.74, ld_sig=0.7, path=-2.87, path_sig=0.58, la=30.39, roll=0.0, **kw):
    return dpf.dplane_face(
        club,
        launch_direction_deg=ld,
        launch_direction_sigma_deg=ld_sig,
        club_path_deg=path,
        club_path_sigma_deg=path_sig,
        launch_vertical_deg=la,
        roll_sigma_deg=roll,
        **kw,
    )


def test_constants():
    assert dpf.FACE_WEIGHT_SIGMA == 0.05
    assert dpf.FACE_SHOW_MAX_SIGMA_DEG == 1.5
    assert dpf.CLASS_Z == 2.0
    assert dpf.CLASS_MAX_SIGMA_DEG == 3.0
    assert dpf.LAUNCH_VERTICAL_SIGMA_DEG == {"camera": 0.5, "radar": 1.5}
    assert dpf.PLACEHOLDER_ATTACK_SIGMA_DEG == 3.0
    assert dpf.ROLL_SIGMA_DEG_UNCALIBRATED == 0.7
    assert dpf.ROLL_SIGMA_DEG_CALIBRATED == 0.2
    assert dpf.ROLL_LD_OFFSET == 0.106
    assert dpf.F2P_CLASSES == ("closed", "neutral", "open")
    assert dpf.AXIS_CLASSES == ("draw", "straight", "fade")


def test_face_to_target_formula_and_worked_example():
    assert dpf.face_to_target_deg(-5.74, -2.87, 0.75) == pytest.approx(
        (-5.74 - 0.25 * -2.87) / 0.75
    )
    f = face()
    assert f.face_to_target_deg == pytest.approx(-6.7, abs=0.05)
    assert f.face_to_path_deg == pytest.approx(-3.8, abs=0.05)


def test_face_reduces_to_launch_direction_when_path_equals_launch_direction():
    assert dpf.face_to_target_deg(2.0, 2.0, 0.75) == pytest.approx(2.0)


@pytest.mark.parametrize("yaw", [-2.0, 0.7, 3.0])
def test_common_yaw_moves_face_one_to_one_and_cancels_in_f2p_and_axis(yaw):
    a, b = face(), face(ld=-5.74 + yaw, path=-2.87 + yaw)
    assert b.face_to_target_deg - a.face_to_target_deg == pytest.approx(yaw, abs=0.051)
    assert b.face_to_path_deg == a.face_to_path_deg
    assert b.spin_axis_deg == a.spin_axis_deg


def test_sigmas_match_the_analytic_propagation():
    k, sk = 0.75, dpf.FACE_WEIGHT_SIGMA
    f = face()
    f2p = (-5.74 + 2.87) / k
    f2p_sigma = math.hypot(math.hypot(0.7, 0.58) / k, abs(f2p) * sk / k)
    ftt_sigma = math.sqrt(
        (0.7 / k) ** 2 + ((1 - k) / k * 0.58) ** 2 + ((-5.74 + 2.87) / k**2 * sk) ** 2
    )
    assert f.face_to_path_sigma_deg == pytest.approx(f2p_sigma, abs=0.01)
    assert f.face_to_target_sigma_deg == pytest.approx(ftt_sigma, abs=0.01)


def test_roll_sigma_uses_tan_launch_plus_offset_and_widens_f2p_sigma():
    assert dpf.ld_per_roll_deg(28.0) == pytest.approx(math.tan(math.radians(28.0)) + 0.106)
    assert dpf.ld_per_roll_deg(None) == 0.0
    no_roll, roll = face(roll=0.0), face(roll=0.7)
    expected = math.hypot(no_roll.face_to_path_sigma_deg, 0.7 * dpf.ld_per_roll_deg(30.39) / 0.75)
    assert roll.face_to_path_sigma_deg == pytest.approx(expected, abs=0.02)
    assert roll.face_to_path_deg == no_roll.face_to_path_deg


def test_default_roll_sigma_is_the_uncalibrated_one():
    f = dpf.dplane_face(
        PW,
        launch_direction_deg=0.0,
        launch_direction_sigma_deg=0.7,
        club_path_deg=0.0,
        club_path_sigma_deg=1.0,
        launch_vertical_deg=30.0,
    )
    assert f.inputs["roll_sigma_deg"] == dpf.ROLL_SIGMA_DEG_UNCALIBRATED


@pytest.mark.parametrize(
    "value,sigma,expected",
    [
        (4.0, 2.0, "open"),
        (3.99, 2.0, "neutral"),
        (-4.0, 2.0, "closed"),
        (-3.99, 2.0, "neutral"),
        (0.0, 0.5, "neutral"),
        (9.0, 3.01, None),
        (None, 1.0, None),
        (1.0, None, None),
        (float("nan"), 1.0, None),
        (1.0, float("nan"), None),
    ],
)
def test_classify_two_sigma_rule_and_sigma_ceiling(value, sigma, expected):
    assert dpf.classify(value, sigma, dpf.F2P_CLASSES) == expected


def test_face_shown_only_up_to_one_and_a_half_degrees():
    assert face(ld_sig=0.7).face_to_target_shown is True
    # A TI start direction (2.2 deg) with a path sigma of 1.2 gives an FTT sigma of
    # about 2.9 deg: the face is hidden and F2P gets no class.
    ti = face(ld_sig=TI_LAUNCH_DIRECTION_SIGMA_DEG, path_sig=1.2)
    assert ti.face_to_target_shown is False
    assert ti.face_to_path_class is None


def test_axis_sign_follows_f2p_and_matches_dplane_spin_axis():
    f = face()
    # The raw D-plane axis (without the calibration offset) follows F2P's sign.
    raw = f.spin_axis_deg - SPIN_AXIS_OFFSET_DEG
    assert math.copysign(1, raw) == math.copysign(1, f.face_to_path_deg)
    assert f.spin_axis_deg == dplane_spin_axis(PW, -5.74, -2.87, 30.39)
    assert f.inputs["attack_angle_deg"] == MEAN_ATTACK_ANGLE_DEG
    assert f.inputs["attack_angle_source"] == "placeholder"


def test_measured_attack_angle_replaces_placeholder_and_narrows_axis_sigma():
    placeholder = face()
    measured = face(attack_angle_deg=-6.0, attack_angle_sigma_deg=1.0)
    assert measured.inputs["attack_angle_source"] == "measured"
    assert measured.inputs["attack_angle_deg"] == -6.0
    assert measured.spin_axis_sigma_deg < placeholder.spin_axis_sigma_deg
    # The attack angle changes the raw axis's size, not its side; the offset is added to
    # both (raw -3.7 / -3.4 here, so the offset alone flips the shown sign of one).
    assert math.copysign(1, measured.spin_axis_deg - SPIN_AXIS_OFFSET_DEG) == math.copysign(
        1, placeholder.spin_axis_deg - SPIN_AXIS_OFFSET_DEG
    )


def test_face_axis_includes_the_offset_and_keeps_its_sigma(monkeypatch):
    shifted = face()
    monkeypatch.setattr(dpf, "SPIN_AXIS_OFFSET_DEG", 0.0)
    raw = face()
    assert SPIN_AXIS_OFFSET_DEG == 3.5
    assert shifted.spin_axis_deg - raw.spin_axis_deg == pytest.approx(3.5, abs=0.11)
    assert shifted.spin_axis_sigma_deg == raw.spin_axis_sigma_deg
    # Everything else is untouched by the axis offset.
    assert shifted.face_to_path_deg == raw.face_to_path_deg
    assert shifted.face_to_target_deg == raw.face_to_target_deg


def test_face_axis_class_uses_the_offset_axis():
    """A square face (LD = path) has a raw axis of 0: only the offset makes it a fade."""
    f = face(ld=-2.87, path=-2.87, ld_sig=0.7, path_sig=0.58, la=30.39)
    assert f.face_to_path_deg == 0.0
    assert f.spin_axis_deg == SPIN_AXIS_OFFSET_DEG
    assert f.spin_axis_class == "fade"  # 3.5 >= 2 * sigma 1.18
    assert f.spin_axis_class == dpf.classify(
        f.spin_axis_deg, f.spin_axis_sigma_deg, dpf.AXIS_CLASSES
    )
    assert f.spin_axis_deg == dplane_spin_axis(PW, -2.87, -2.87, 30.39)


def test_non_finite_attack_angle_falls_back_to_the_placeholder():
    f = face(attack_angle_deg=float("nan"), attack_angle_sigma_deg=1.0)
    assert f.inputs["attack_angle_source"] == "placeholder"
    assert f.spin_axis_deg == face().spin_axis_deg


@pytest.mark.parametrize(
    "club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7, "driver"]
)
def test_woods_get_face_and_f2p_with_wood_k_but_no_axis(club):
    f = face(club=club)
    assert f.status == "ok_no_axis_wood"
    assert f.face_weight == 0.85
    assert f.face_to_target_deg is not None and f.face_to_path_deg is not None
    assert f.spin_axis_deg is None and f.spin_axis_class is None and f.spin_axis_sigma_deg is None
    assert f.spin_loft_deg is None


@pytest.mark.parametrize(
    "kwargs,status",
    [
        ({"ld": None}, "no_launch_direction"),
        ({"ld_sig": None}, "no_launch_direction"),
        ({"ld": float("nan")}, "no_launch_direction"),
        ({"path": None}, "no_club_path"),
        ({"path_sig": None}, "no_club_path"),
        ({"club": None}, "no_club"),
        ({"la": None}, "ok_no_axis_no_launch_vertical"),
        ({"la": float("nan")}, "ok_no_axis_no_launch_vertical"),
        ({"la": float("inf")}, "ok_no_axis_no_launch_vertical"),
        ({"la": float("-inf")}, "ok_no_axis_no_launch_vertical"),
    ],
)
def test_missing_inputs_give_a_status_never_an_exception(kwargs, status):
    f = face(**kwargs)
    assert f.status == status
    if status != "ok_no_axis_no_launch_vertical":
        assert f.face_to_target_deg is None and f.face_to_path_class is None
    assert f.spin_axis_deg is None
    json.dumps(f.to_dict(), allow_nan=False)


@pytest.mark.parametrize("la", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_launch_vertical_is_treated_as_missing(la):
    missing = face(la=None)
    f = face(la=la)
    assert f.face_to_target_deg == missing.face_to_target_deg == pytest.approx(-6.7, abs=0.05)
    assert f.face_to_target_sigma_deg == missing.face_to_target_sigma_deg
    assert f.face_to_path_deg == missing.face_to_path_deg == pytest.approx(-3.8, abs=0.05)
    assert f.face_to_path_sigma_deg == missing.face_to_path_sigma_deg
    assert f.face_to_path_class == missing.face_to_path_class == "closed"
    assert f.inputs["launch_vertical_deg"] is None
    assert dpf.ld_per_roll_deg(la) == 0.0


def test_payload_keys_are_pinned_and_json_serialisable():
    d = face().to_dict()
    assert set(d) == {
        "status",
        "club_category",
        "face_weight",
        "face_to_target_deg",
        "face_to_target_sigma_deg",
        "face_to_target_shown",
        "face_to_path_deg",
        "face_to_path_sigma_deg",
        "face_to_path_class",
        "spin_loft_deg",
        "spin_axis_deg",
        "spin_axis_sigma_deg",
        "spin_axis_class",
        "inputs",
    }
    json.dumps(d)
    json.dumps(face(club=None).to_dict())


# Regression on four 2026-10-25 range shots (camera LD high tier, accepted camera path,
# TI vertical launch at the radar sigma 1.5, roll sigma 0.7): pinned to catch formula
# drift, not tuned to Toptracer.
@pytest.mark.parametrize(
    "shot,club,ld,path,path_sig,la,f2p,klass",
    [
        (2, PW, -5.74, -2.87, 0.58, 30.39, -3.8, "closed"),
        (7, PW, -1.54, -3.65, 0.69, 34.78, 2.8, "neutral"),
        (12, "8-iron", -0.20, 4.08, 0.87, 25.7, -5.7, "closed"),
        (67, "8-iron", 2.42, -1.14, 1.06, 27.9, 4.7, "open"),
    ],
)
def test_range_2026_10_25_regression(shot, club, ld, path, path_sig, la, f2p, klass):
    f = face(club=club, ld=ld, path=path, path_sig=path_sig, la=la, roll=0.7)
    assert f.face_to_path_deg == pytest.approx(f2p, abs=0.05)
    assert f.face_to_path_class == klass
    assert f.face_to_target_shown


@pytest.mark.parametrize(
    "status,sigma",
    [
        ("camera_assisted_high", 0.7),
        ("camera_assisted_high:accepted", 0.7),
        ("camera_only_experimental", 1.5),
        ("camera_withheld_fallback_iwr", None),
        ("camera_withheld_fallback_iwr:rejected_no_ball_departure", None),
        ("camera_withheld_iwr_implausible", None),
        (None, None),
        ("", None),
    ],
)
def test_launch_direction_sigma_from_fusion_status(status, sigma):
    assert dpf.launch_direction_sigma_deg(status) == sigma


def test_sigma_table_covers_every_camera_start_direction_and_never_the_ti_fallback():
    # HorizontalFusionDecision statuses whose selected start direction is the camera's
    # (ball_flight.select_camera_assisted_horizontal).
    camera = {
        "camera_only_experimental",
        "camera_assisted_high",
        "camera_assisted_experimental_agreement",
        "camera_experimental_disagreement",
        "camera_experimental_no_iwr",
    }
    assert set(dpf.LAUNCH_DIRECTION_SIGMA_DEG) == camera
    assert "camera_withheld_fallback_iwr" not in dpf.LAUNCH_DIRECTION_SIGMA_DEG
