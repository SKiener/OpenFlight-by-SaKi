"""Tests for the D-plane spin-axis model."""

import math

import pytest

import openflight.dplane as dplane_module
from openflight.clubs import ClubType
from openflight.clubs.physics import SHOT_SIMULATION_DEFAULTS
from openflight.dplane import (
    CURVE_CLASSES,
    CURVE_STRAIGHT_MAX_DEG,
    CURVE_STRONG_MIN_DEG,
    FACE_WEIGHT_IRONS,
    FACE_WEIGHT_WOODS,
    MAX_SPIN_LOFT_DEG,
    MEAN_ATTACK_ANGLE_DEG,
    MIN_SPIN_LOFT_DEG,
    SPIN_AXIS_OFFSET_DEG,
    WOOD_CATEGORIES,
    CalculatedLoft,
    calculated_loft,
    curve_class,
    dplane_face,
    dplane_spin_axis,
    dynamic_loft_deg,
    face_to_path_deg,
    face_weight_for_club,
    spin_axis_deg,
    spin_loft_deg,
    spin_loft_unclamped_deg,
)

MEAN_ATTACK_DEG = SHOT_SIMULATION_DEFAULTS.angle_of_attack_mean_deg


def test_placeholder_attack_angle_is_minus_four_from_club_physics_table():
    assert MEAN_ATTACK_ANGLE_DEG == MEAN_ATTACK_DEG == -4.0


def _expected_axis(launch_direction, path, launch_vertical, attack, face_weight):
    """One face weight k for both components; the raw D-plane axis, without the offset."""
    f2p = (launch_direction - path) / face_weight
    spin_loft = min(
        max((launch_vertical - attack) / face_weight, MIN_SPIN_LOFT_DEG), MAX_SPIN_LOFT_DEG
    )
    return math.degrees(math.atan(math.sin(math.radians(f2p)) / math.tan(math.radians(spin_loft))))


def test_constants():
    assert FACE_WEIGHT_WOODS == 0.85
    assert FACE_WEIGHT_IRONS == 0.75
    assert (MIN_SPIN_LOFT_DEG, MAX_SPIN_LOFT_DEG) == (5.0, 70.0)


def test_separate_vertical_face_weight_is_gone():
    assert not hasattr(dplane_module, "VERTICAL_FACE_WEIGHT")


def test_wood_categories_are_driver_and_fairway_woods():
    assert WOOD_CATEGORIES == {ClubType.DRIVER, ClubType.WOOD_3, ClubType.WOOD_5, ClubType.WOOD_7}


@pytest.mark.parametrize(
    "club",
    [
        ClubType.DRIVER,
        ClubType.WOOD_3,
        ClubType.WOOD_5,
        ClubType.WOOD_7,
        "driver",
        "mini-driver",
        "3-wood",
    ],
)
def test_woods_use_wood_face_weight(club):
    assert face_weight_for_club(club) == FACE_WEIGHT_WOODS


@pytest.mark.parametrize(
    "club",
    [
        ClubType.HYBRID_3,
        ClubType.HYBRID_9,
        ClubType.IRON_2,
        ClubType.IRON_7,
        ClubType.PW,
        ClubType.GW,
        ClubType.SW,
        ClubType.LW,
        ClubType.UNKNOWN,
        "wedge-48",
        "5-hybrid",
        "not-a-club",
        None,
    ],
)
def test_hybrids_irons_wedges_and_unknown_use_iron_face_weight(club):
    assert face_weight_for_club(club) == FACE_WEIGHT_IRONS


def test_every_club_type_has_a_face_weight():
    for club in ClubType:
        assert face_weight_for_club(club) in (FACE_WEIGHT_WOODS, FACE_WEIGHT_IRONS)


def test_face_to_path_divides_by_face_weight():
    assert face_to_path_deg(-2.0, -4.0, 0.75) == pytest.approx(2.0 / 0.75)
    assert face_to_path_deg(1.0, 1.0, 0.85) == 0.0


def test_spin_loft_divides_launch_minus_attack_by_the_same_face_weight():
    assert spin_loft_deg(32.0, -5.0, 0.75) == pytest.approx(37.0 / 0.75)
    assert spin_loft_deg(32.0, -5.0, 0.75) == pytest.approx(49.333, abs=1e-3)
    assert spin_loft_deg(13.0, 2.0, 0.85) == pytest.approx(11.0 / 0.85)


@pytest.mark.parametrize(
    ("launch", "attack", "expected"),
    [
        (1.0, 5.0, MIN_SPIN_LOFT_DEG),
        (0.0, 0.0, MIN_SPIN_LOFT_DEG),
        (50.0, -12.0, MAX_SPIN_LOFT_DEG),
    ],
)
def test_spin_loft_is_clamped(launch, attack, expected):
    assert spin_loft_deg(launch, attack, FACE_WEIGHT_IRONS) == expected


def test_open_face_gives_positive_axis_and_closed_face_negative():
    assert spin_axis_deg(3.0, 20.0) > 0
    assert spin_axis_deg(-3.0, 20.0) < 0
    assert spin_axis_deg(0.0, 20.0) == 0.0
    assert spin_axis_deg(3.0, 20.0) == pytest.approx(-spin_axis_deg(-3.0, 20.0))


def test_spin_axis_formula():
    expected = math.degrees(math.atan(math.sin(math.radians(4.0)) / math.tan(math.radians(15.0))))
    assert spin_axis_deg(4.0, 15.0) == pytest.approx(expected)


def test_lower_spin_loft_tilts_axis_more_for_same_face_to_path():
    assert spin_axis_deg(2.0, 12.0) > spin_axis_deg(2.0, 40.0)


def test_worked_example_wedge_48_one_k():
    """LD -2, path -4, launch 32, placeholder AoA -4, k 0.75.

    F2P = 2 / 0.75 = 2.667 deg, spin loft = 36 / 0.75 = 48 deg,
    axis = atan(sin 2.667 / tan 48) = 2.40 deg (2.92 deg with the old kv 0.85);
    with the +3.5 deg calibration offset dplane_spin_axis gives 5.9.
    """
    k = face_weight_for_club("wedge-48")
    f2p = face_to_path_deg(-2.0, -4.0, k)
    loft = spin_loft_deg(32.0, -4.0, k)
    axis = spin_axis_deg(f2p, loft)

    assert k == 0.75
    assert f2p == pytest.approx(2.667, abs=1e-3)
    assert loft == pytest.approx(48.0)
    assert axis == pytest.approx(2.399, abs=1e-3)
    assert dplane_spin_axis("wedge-48", -2.0, -4.0, 32.0) == 5.9


def test_worked_example_7_iron_one_k():
    """LD +2, path 0, launch 18, AoA -4: F2P 2.667, spin loft 29.333, raw axis 4.73 (was 5.48).

    The +3.5 deg offset makes it 8.2, and the mirrored shot -4.73 + 3.5 = -1.2.
    """
    assert dplane_spin_axis(ClubType.IRON_7, 2.0, 0.0, 18.0) == 8.2
    assert dplane_spin_axis(ClubType.IRON_7, -2.0, 0.0, 18.0) == -1.2


def test_spin_axis_offset_constant():
    assert SPIN_AXIS_OFFSET_DEG == 3.5


def test_dplane_spin_axis_adds_the_offset_before_rounding():
    """A square face (LD = path) gives a raw axis of 0: the axis is the offset alone."""
    assert dplane_spin_axis(ClubType.IRON_7, -1.5, -1.5, 18.0) == SPIN_AXIS_OFFSET_DEG
    raw = _expected_axis(1.234, -0.5, 18.0, -4.0, FACE_WEIGHT_IRONS)
    assert dplane_spin_axis(ClubType.IRON_7, 1.234, -0.5, 18.0) == round(
        raw + SPIN_AXIS_OFFSET_DEG, 1
    )


def test_dplane_spin_axis_uses_placeholder_attack_angle_without_a_measured_one():
    axis = dplane_spin_axis("wedge-48", -2.0, -4.0, 32.0)
    expected = _expected_axis(-2.0, -4.0, 32.0, -4.0, FACE_WEIGHT_IRONS)
    assert axis == round(expected + SPIN_AXIS_OFFSET_DEG, 1)
    assert dplane_spin_axis("wedge-48", -2.0, -4.0, 32.0, None) == axis
    assert dplane_spin_axis("wedge-48", -2.0, -4.0, 32.0, attack_angle_deg=-4.0) == axis


def test_dplane_spin_axis_uses_measured_attack_angle_when_given():
    """LD +2, path 0, launch 18, measured AoA -8: spin loft 26 / 0.75 = 34.667, raw axis 3.8.

    With the +3.5 deg offset: 7.3.
    """
    axis = dplane_spin_axis(ClubType.IRON_7, 2.0, 0.0, 18.0, attack_angle_deg=-8.0)
    raw = _expected_axis(2.0, 0.0, 18.0, -8.0, FACE_WEIGHT_IRONS)
    assert axis == round(raw + SPIN_AXIS_OFFSET_DEG, 1)
    assert axis == 7.3
    assert axis != dplane_spin_axis(ClubType.IRON_7, 2.0, 0.0, 18.0)


def test_axis_barely_depends_on_k_with_one_k():
    """Same k top and bottom: k 0.75 vs 0.85 moves this iron axis by under 0.2 deg.

    Uses the production functions, so a separate vertical weight in spin_loft_deg
    (k fixed at 0.85 below the line) would break it: the two axes would then
    differ by about 0.6 deg.
    """
    low, high = (
        spin_axis_deg(face_to_path_deg(2.0, 0.0, k), spin_loft_deg(18.0, -4.0, k))
        for k in (0.75, 0.85)
    )
    assert abs(low - high) < 0.2


@pytest.mark.parametrize(
    "club",
    sorted(WOOD_CATEGORIES, key=lambda c: c.value)
    + ["driver", "mini-driver", "3-wood", "5-wood", "7-wood"],
)
def test_dplane_spin_axis_is_none_for_woods(club):
    assert dplane_spin_axis(club, 2.0, 0.0, 13.0) is None
    assert dplane_spin_axis(club, 2.0, 0.0, 13.0, attack_angle_deg=2.0) is None


@pytest.mark.parametrize(
    "club", [ClubType.HYBRID_3, ClubType.IRON_4, ClubType.PW, ClubType.UNKNOWN]
)
def test_dplane_spin_axis_still_computed_for_hybrids_irons_wedges_and_unknown(club):
    assert dplane_spin_axis(club, 2.0, 0.0, 18.0) == 8.2  # raw 4.7 + 3.5 offset


def test_dplane_spin_axis_rounds_to_tenth():
    axis = dplane_spin_axis(ClubType.IRON_7, 1.234, -0.5, 18.0)
    assert axis == round(axis, 1)


@pytest.mark.parametrize(
    "args",
    [
        (ClubType.IRON_7, None, 0.0, 18.0),
        (ClubType.IRON_7, 2.0, None, 18.0),
        (ClubType.IRON_7, 2.0, 0.0, None),
        (None, 2.0, 0.0, 18.0),
    ],
)
def test_dplane_spin_axis_none_when_any_input_missing(args):
    assert dplane_spin_axis(*args) is None


def test_curve_class_constants():
    assert CURVE_CLASSES == ("hook", "draw", "straight", "fade", "slice")
    assert CURVE_STRAIGHT_MAX_DEG == 2.5
    assert CURVE_STRONG_MIN_DEG == 8.0


@pytest.mark.parametrize(
    ("axis", "expected"),
    [
        (0.0, "straight"),
        (-0.0, "straight"),
        (2.4, "straight"),
        (-2.4, "straight"),
        (2.4999, "straight"),
        (-2.4999, "straight"),
        (2.5, "fade"),
        (-2.5, "draw"),
        (5.0, "fade"),
        (-5.0, "draw"),
        (7.9, "fade"),
        (-7.9, "draw"),
        (7.9999, "fade"),
        (-7.9999, "draw"),
        (8.0, "slice"),
        (-8.0, "hook"),
        (25.0, "slice"),
        (-25.0, "hook"),
        (3, "fade"),
        (-9, "hook"),
    ],
)
def test_curve_class_bands_and_boundaries(axis, expected):
    assert curve_class(axis) == expected
    assert curve_class(axis) in CURVE_CLASSES


@pytest.mark.parametrize("axis", [None, math.nan, math.inf, -math.inf, True, False, "3.0"])
def test_curve_class_none_without_a_finite_axis(axis):
    assert curve_class(axis) is None


def test_curve_class_of_a_dplane_axis_follows_its_sign():
    # F2P +-8 deg: raw axes +-13.9, with the offset 17.4 and -10.4.
    open_face = dplane_spin_axis(ClubType.IRON_7, 4.0, -2.0, 18.0)
    closed_face = dplane_spin_axis(ClubType.IRON_7, -4.0, 2.0, 18.0)
    assert open_face > 0 > closed_face
    assert curve_class(open_face) in ("fade", "slice")
    assert curve_class(closed_face) in ("draw", "hook")


def test_dynamic_and_spin_loft_worked_example():
    assert dynamic_loft_deg(24.0, -8.0, 0.75) == pytest.approx(34.667, abs=5e-4)
    assert spin_loft_unclamped_deg(24.0, -8.0, 0.75) == pytest.approx(42.667, abs=5e-4)
    loft = calculated_loft(
        ClubType.IRON_8, 24.0, -8.0, attack_sigma_deg=1.0, launch_source="camera"
    )
    assert isinstance(loft, CalculatedLoft)
    assert loft.dynamic_loft_deg == pytest.approx(34.667, abs=5e-4)
    assert loft.spin_loft_deg == pytest.approx(42.667, abs=5e-4)
    assert loft.face_weight == 0.75
    assert loft.launch_source == "camera"


def test_calculated_loft_sigmas():
    # Camera launch sigma 0.5, attack sigma 1.0, k 0.75.
    loft = calculated_loft(
        ClubType.IRON_8, 24.0, -8.0, attack_sigma_deg=1.0, launch_source="camera"
    )
    assert loft.dynamic_loft_sigma_deg == pytest.approx(0.745, abs=5e-4)
    assert loft.spin_loft_sigma_deg == pytest.approx(1.491, abs=5e-4)


@pytest.mark.parametrize(
    "club,launch,attack,sigma,source",
    [
        (ClubType.DRIVER, 14.0, 2.0, 1.0, "camera"),
        (ClubType.WOOD_3, 12.0, -1.0, 1.0, "camera"),
        ("7-wood", 15.0, -2.0, 1.0, "camera"),
        (ClubType.IRON_8, 24.0, -8.0, 1.0, "radar"),
        (ClubType.IRON_8, 24.0, -8.0, 1.0, None),
        (ClubType.IRON_8, None, -8.0, 1.0, "camera"),
        (ClubType.IRON_8, 24.0, None, 1.0, "camera"),
        (ClubType.IRON_8, 24.0, -8.0, None, "camera"),
        (ClubType.IRON_8, float("nan"), -8.0, 1.0, "camera"),
        (None, 24.0, -8.0, 1.0, "camera"),
        # Spin loft outside [5, 70]: 3 deg and 75 deg.
        (ClubType.IRON_8, 0.25, -2.0, 1.0, "camera"),
        (ClubType.LW, 50.25, -6.0, 1.0, "camera"),
    ],
)
def test_calculated_loft_is_none_without_its_inputs(club, launch, attack, sigma, source):
    assert (
        calculated_loft(club, launch, attack, attack_sigma_deg=sigma, launch_source=source) is None
    )


def test_calculated_loft_keeps_the_range_edges():
    low = calculated_loft(ClubType.IRON_8, 1.75, -2.0, attack_sigma_deg=1.0, launch_source="camera")
    assert low is not None and low.spin_loft_deg == pytest.approx(MIN_SPIN_LOFT_DEG)
    high = calculated_loft(ClubType.LW, 46.5, -6.0, attack_sigma_deg=1.0, launch_source="camera")
    assert high is not None and high.spin_loft_deg == pytest.approx(MAX_SPIN_LOFT_DEG)


@pytest.mark.parametrize("offset", [-3.0, 1.5, 4.0])
def test_common_offset_in_launch_and_attack_leaves_spin_loft_unchanged(offset):
    base = calculated_loft(
        ClubType.IRON_8, 24.0, -8.0, attack_sigma_deg=1.0, launch_source="camera"
    )
    moved = calculated_loft(
        ClubType.IRON_8, 24.0 + offset, -8.0 + offset, attack_sigma_deg=1.0, launch_source="camera"
    )
    assert moved.spin_loft_deg == pytest.approx(base.spin_loft_deg, abs=1e-9)
    assert moved.dynamic_loft_deg == pytest.approx(base.dynamic_loft_deg + offset, abs=1e-9)


@pytest.mark.parametrize("launch,attack", [(24.0, -8.0), (30.4, -9.2), (18.0, -2.5)])
def test_dplane_spin_loft_equals_calculated_loft_in_range(launch, attack):
    loft = calculated_loft(
        ClubType.IRON_8, launch, attack, attack_sigma_deg=1.0, launch_source="camera"
    )
    face = dplane_face(
        ClubType.IRON_8,
        launch_direction_deg=1.0,
        launch_direction_sigma_deg=0.7,
        club_path_deg=0.0,
        club_path_sigma_deg=1.0,
        launch_vertical_deg=launch,
        attack_angle_deg=attack,
        attack_angle_sigma_deg=1.0,
    )
    assert loft.spin_loft_deg == pytest.approx(spin_loft_deg(launch, attack, 0.75), abs=1e-12)
    assert face.spin_loft_deg == round(loft.spin_loft_deg, 1)
