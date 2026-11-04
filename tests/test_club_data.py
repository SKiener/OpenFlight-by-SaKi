"""Calculated club data: which shot fields each gate lets through."""

import json
import math
from dataclasses import replace

import pytest

from openflight.camera.club_attack import AttackAngleResult
from openflight.camera.club_head_path import (
    ClubDataResult,
    ClubHeadPathResult,
    ContactTime,
    ImpactLocation,
)
from openflight.camera.impact_point import ImpactPoint
from openflight.club_data import (
    CLUB_DATA_FIELDS,
    IMPACT_E_FALLBACK_SIGMA_MM,
    calculate_club_data,
    impact_zone,
)
from openflight.clubs import ClubType
from openflight.dplane import dplane_face

CAMERA_STATUS = "camera_assisted_high"
TI_FALLBACK_STATUS = "camera_withheld_fallback_iwr:rejected_line_fit_residual"


def _contact() -> ContactTime:
    return ContactTime(
        time_s=1.0,
        source="departure_fit",
        sigma_s=0.5e-3,
        t0_ms=-5.6,
        reason="departure_fit",
        departure_fit_ms=-5.6,
        sound_bound_ms=-4.373,
    )


def _result(
    *,
    path_status="accepted",
    path_deg=-2.87,
    path_sigma=0.58,
    attack_status="accepted",
    attack_deg=-8.4,
    attack_sigma=0.9,
    low_point_cm=11.73,
    impact_status="accepted",
    toe_mm=11.24,
    point_status="accepted",
    offset_mm=12.34,
    point=True,
) -> ClubDataResult:
    attack = AttackAngleResult(
        attack_status,
        attack_deg=attack_deg,
        sigma_deg=attack_sigma,
        low_point_cm=low_point_cm,
        low_point_sigma_cm=2.46,
    )
    impact = ImpactLocation(impact_status, toe_mm=toe_mm, sigma_mm=4.36)
    impact_point = (
        ImpactPoint(
            point_status,
            offset_mm=offset_mm,
            sigma_mm=2.66,
            method="interpolated",
            height_mm=31.24,
            height_sigma_mm=2.76,
            scale_mm_per_px=2.996,
            e_scale_ratio=1.155,
        )
        if point
        else None
    )
    path = ClubHeadPathResult(
        path_status,
        path_deg=path_deg,
        sigma_deg=path_sigma,
        attack=attack,
        impact=impact,
        impact_point=impact_point,
    )
    return ClubDataResult(_contact(), None, path, {})


def _calculate(result=None, **overrides):
    kwargs = {
        "club": ClubType.PW,
        "launch_direction_deg": -5.74,
        "launch_direction_status": CAMERA_STATUS,
        "launch_vertical_deg": 27.0,
        "camera_vertical_deg": 24.6,
    }
    kwargs.update(overrides)
    return calculate_club_data(_result() if result is None else result, **kwargs)


def test_the_field_set_is_pinned():
    assert CLUB_DATA_FIELDS == (
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
    )


IMPACT_FIELDS = {
    "impact_toe_mm",
    "impact_sigma_mm",
    "impact_offset_mm",
    "impact_height_mm",
    "impact_height_sigma_mm",
    "impact_zone",
    "impact_method",
    "impact_scale_mm_per_px",
}
HEIGHT_FIELDS = {"impact_height_mm", "impact_height_sigma_mm"}


def test_an_iron_with_everything_accepted_fills_every_field_rounded():
    calculated = _calculate()
    fields = calculated.fields

    assert set(fields) == set(CLUB_DATA_FIELDS)
    # The face height is a teed wood's or driver's only.
    assert {key for key in CLUB_DATA_FIELDS if fields[key] is None} == HEIGHT_FIELDS
    assert fields["camera_club_path_deg"] == -2.9
    assert fields["camera_club_path_sigma_deg"] == 0.6
    assert fields["camera_attack_angle_deg"] == -8.4
    assert (fields["camera_low_point_cm"], fields["camera_low_point_sigma_cm"]) == (11.7, 2.5)
    assert fields["impact_toe_mm"] == fields["impact_offset_mm"] == 12.3
    assert (fields["impact_method"], fields["impact_zone"]) == ("D", "toe")
    assert fields["impact_scale_mm_per_px"] == 3.0
    assert calculated.loft is not None and calculated.loft.launch_source == "camera"
    assert fields["spin_loft_deg"] == round(calculated.loft.spin_loft_deg, 1)
    assert calculated.face is not None and calculated.face.status == "ok"
    assert fields["face_to_path_class"] in ("closed", "neutral", "open")
    assert fields["spin_axis_class"] in ("draw", "straight", "fade")
    for key in CLUB_DATA_FIELDS:
        value = fields[key]
        if isinstance(value, float) and key != "impact_scale_mm_per_px":
            assert value == round(value, 1)
    json.dumps(fields)


def test_the_face_uses_the_camera_launch_angle_and_the_measured_attack():
    calculated = _calculate()
    expected = dplane_face(
        ClubType.PW,
        launch_direction_deg=-5.74,
        launch_direction_sigma_deg=0.7,
        club_path_deg=-2.87,
        club_path_sigma_deg=0.58,
        launch_vertical_deg=24.6,
        launch_vertical_sigma_deg=0.5,
        attack_angle_deg=-8.4,
        attack_angle_sigma_deg=0.9,
        roll_sigma_deg=0.7,
    )

    assert calculated.face == expected
    assert calculated.fields["face_to_target_deg"] == round(expected.face_to_target_deg, 1)


def test_without_a_camera_launch_angle_the_face_takes_the_radar_one_and_no_lofts():
    calculated = _calculate(camera_vertical_deg=None)

    assert calculated.loft is None
    assert calculated.fields["dynamic_loft_deg"] is None
    assert calculated.fields["spin_loft_deg"] is None
    assert calculated.face.inputs["launch_vertical_deg"] == 27.0


def test_a_calibrated_roll_narrows_the_face_sigma():
    loose = _calculate().face
    tight = _calculate(roll_calibrated=True).face

    assert tight.inputs["roll_sigma_deg"] == 0.2
    assert tight.face_to_path_sigma_deg < loose.face_to_path_sigma_deg


@pytest.mark.parametrize(
    "result, empty",
    [
        (
            _result(path_status="rejected_sigma_too_large"),
            {
                "camera_club_path_deg",
                "camera_club_path_sigma_deg",
                "face_to_target_deg",
                "face_to_target_sigma_deg",
                "face_to_path_deg",
                "face_to_path_sigma_deg",
                "face_to_path_class",
                "calculated_spin_axis_deg",
                "calculated_spin_axis_sigma_deg",
                "spin_axis_class",
            },
        ),
        (
            _result(attack_status="rejected_contact_height"),
            {
                "camera_attack_angle_deg",
                "camera_attack_angle_sigma_deg",
                "camera_low_point_cm",
                "camera_low_point_sigma_cm",
                "dynamic_loft_deg",
                "dynamic_loft_sigma_deg",
                "spin_loft_deg",
                "spin_loft_sigma_deg",
            },
        ),
        (
            _result(impact_status="rejected_head_length", point_status="rejected_sigma_too_large"),
            IMPACT_FIELDS,
        ),
    ],
    ids=["path", "attack", "impact"],
)
def test_every_field_is_null_when_its_gate_fails(result, empty):
    fields = _calculate(result).fields

    assert {key for key, value in fields.items() if value is None} == empty | HEIGHT_FIELDS


def test_a_ti_start_direction_gives_no_face_to_target_and_no_class():
    calculated = _calculate(launch_direction_status=TI_FALLBACK_STATUS)

    assert calculated.face.status == "no_launch_direction"
    assert calculated.fields["face_to_target_deg"] is None
    assert calculated.fields["face_to_path_class"] is None
    assert calculated.fields["spin_axis_class"] is None
    assert calculated.fields["camera_club_path_deg"] == -2.9


def test_a_wide_start_direction_hides_only_the_face_to_target():
    """camera_only_experimental (1.5 deg) leaves sigma_FTT above the 1.5 deg limit."""
    calculated = _calculate(launch_direction_status="camera_only_experimental")

    assert not calculated.face.face_to_target_shown
    assert calculated.fields["face_to_target_deg"] is None
    assert calculated.fields["face_to_path_deg"] is not None


@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_7])
def test_a_wood_gives_a_face_to_path_class_but_no_axis_and_no_lofts(club):
    calculated = _calculate(_result(attack_status="withheld_tee_height_unknown"), club=club)
    fields = calculated.fields

    assert calculated.face.status == "ok_no_axis_wood"
    assert fields["face_to_target_deg"] is not None
    assert calculated.face.face_to_path_class is not None
    assert fields["face_to_path_class"] == calculated.face.face_to_path_class
    assert fields["calculated_spin_axis_deg"] is None
    assert fields["spin_axis_class"] is None
    assert fields["dynamic_loft_deg"] is None and fields["spin_loft_deg"] is None
    assert fields["camera_attack_angle_deg"] is None
    assert fields["camera_low_point_cm"] is None


def test_an_accepted_wood_attack_still_gives_no_low_point_or_loft():
    fields = _calculate(club=ClubType.DRIVER).fields

    assert fields["camera_low_point_cm"] is None
    assert fields["dynamic_loft_deg"] is None


def test_a_left_handed_golfer_gets_no_dplane_face():
    calculated = _calculate(handedness="left")

    assert calculated.face is None
    assert calculated.dplane_reason == "left_handed_unsupported"
    assert calculated.fields["face_to_path_class"] is None
    assert calculated.fields["camera_club_path_deg"] == -2.9
    assert calculated.fields["impact_toe_mm"] == 12.3


@pytest.mark.parametrize(
    "overrides, reason",
    [
        ({"result": None}, "not_run"),
        ({"launch_direction_deg": None}, "no_launch_direction"),
        ({"launch_direction_deg": math.nan}, "no_launch_direction"),
    ],
)
def test_the_dplane_reason_names_what_was_missing(overrides, reason):
    result = overrides.pop("result", _result())
    calculated = calculate_club_data(
        result,
        club=ClubType.PW,
        launch_direction_deg=overrides.get("launch_direction_deg", -5.74),
        launch_direction_status=CAMERA_STATUS,
        launch_vertical_deg=27.0,
        camera_vertical_deg=24.6,
    )

    assert calculated.face is None
    assert calculated.dplane_reason == reason


def test_a_rejected_path_names_itself_as_the_dplane_reason():
    assert _calculate(_result(path_status="rejected_short_track")).dplane_reason == (
        "path_not_accepted"
    )


def test_no_result_gives_every_field_null():
    calculated = calculate_club_data(
        None,
        club=ClubType.PW,
        launch_direction_deg=-5.74,
        launch_direction_status=CAMERA_STATUS,
        launch_vertical_deg=27.0,
        camera_vertical_deg=24.6,
    )

    assert all(value is None for value in calculated.fields.values())


def test_a_broken_result_never_raises():
    calculated = calculate_club_data(
        object(),
        club="pw",
        launch_direction_deg=-5.74,
        launch_direction_status=CAMERA_STATUS,
        launch_vertical_deg=27.0,
        camera_vertical_deg=24.6,
    )

    assert calculated.dplane_reason == "error"
    assert set(calculated.fields) == set(CLUB_DATA_FIELDS)
    assert all(value is None for value in calculated.fields.values())


def test_a_club_id_string_is_accepted():
    assert _calculate(club="8-iron").fields["spin_axis_class"] is not None


# --- The impact point: D primary, E fallback, height, zone -------------------------------


def test_the_impact_point_is_d_when_it_passed_and_never_a_mean_with_e():
    fields = _calculate(_result(offset_mm=12.34, toe_mm=-3.0)).fields

    assert fields["impact_method"] == "D"
    assert (fields["impact_offset_mm"], fields["impact_sigma_mm"]) == (12.3, 2.7)
    assert fields["impact_scale_mm_per_px"] == 3.0


@pytest.mark.parametrize(
    "point_status", ["rejected_sigma_too_large", "rejected_no_head_before_contact", "error"]
)
def test_e_is_the_fallback_at_the_ball_scale_with_its_sigma_widened(point_status):
    fields = _calculate(_result(point_status=point_status, toe_mm=-40.0)).fields

    assert fields["impact_method"] == "E"
    assert fields["impact_offset_mm"] == round(1.155 * -40.0, 1)
    assert fields["impact_toe_mm"] == fields["impact_offset_mm"]
    assert fields["impact_sigma_mm"] == round(
        math.hypot(1.155 * 4.36, IMPACT_E_FALLBACK_SIGMA_MM), 1
    )
    assert fields["impact_scale_mm_per_px"] == 3.0
    assert fields["impact_zone"] == "heel"
    assert fields["impact_height_mm"] is None  # E measures no height


def test_e_without_an_impact_point_keeps_its_own_scale():
    fields = _calculate(_result(point=False, toe_mm=11.24)).fields

    assert fields["impact_method"] == "E"
    assert fields["impact_offset_mm"] == 11.2
    assert fields["impact_scale_mm_per_px"] is None
    assert fields["impact_sigma_mm"] == round(math.hypot(4.36, IMPACT_E_FALLBACK_SIGMA_MM), 1)


def test_neither_method_gives_no_impact_point():
    fields = _calculate(
        _result(point_status="rejected_head_length", impact_status="rejected_short_track")
    ).fields

    assert all(fields[key] is None for key in IMPACT_FIELDS)


@pytest.mark.parametrize(
    "club, teed, shown",
    [
        (ClubType.DRIVER, True, True),
        (ClubType.DRIVER, False, False),
        (ClubType.WOOD_7, True, True),
        (ClubType.WOOD_3, False, False),
        (ClubType.HYBRID_5, True, False),
        (ClubType.PW, True, False),
        (ClubType.IRON_8, False, False),
    ],
)
def test_the_face_height_only_for_a_teed_wood_or_driver(club, teed, shown):
    fields = _calculate(club=club, teed=teed).fields

    if shown:
        assert (fields["impact_height_mm"], fields["impact_height_sigma_mm"]) == (31.2, 2.8)
    else:
        assert fields["impact_height_mm"] is None and fields["impact_height_sigma_mm"] is None
    assert fields["impact_offset_mm"] == 12.3


@pytest.mark.parametrize(
    "offset, sigma, club, zone",
    [
        (0.0, 2.6, ClubType.IRON_8, "center"),
        (7.0, 0.5, ClubType.IRON_8, "toe"),  # irons: center is |offset| < 6 mm
        (7.0, 0.5, ClubType.DRIVER, "center"),  # woods and driver: < 8 mm
        (-7.0, 0.5, ClubType.PW, "heel"),
        (-7.0, 0.5, ClubType.WOOD_3, "center"),
        (7.0, 0.5, ClubType.HYBRID_5, "center"),
        (7.0, 0.5, None, "toe"),  # an unknown club takes the irons width
        (7.0, 0.5, "7-wood", "center"),
        (6.0, 2.6, ClubType.IRON_8, None),  # on the edge: 50/50
        (8.0, 5.3, ClubType.DRIVER, None),
        (5.5, 5.0, ClubType.DRIVER, "center"),  # P(center) 0.69
        (6.0, 5.0, ClubType.DRIVER, None),  # 0.65
        (10.0, 5.0, ClubType.DRIVER, None),  # P(toe) 0.66
        (10.5, 5.0, ClubType.DRIVER, "toe"),  # 0.69
        (0.0, 10.0, ClubType.IRON_8, None),  # a sigma wider than the zone
        (-46.0, 15.0, ClubType.DRIVER, "heel"),  # an extreme hit keeps its zone
        (None, 2.0, ClubType.IRON_8, None),
        (3.0, None, ClubType.IRON_8, None),
        (3.0, 0.0, ClubType.IRON_8, None),
        (math.nan, 2.0, ClubType.IRON_8, None),
    ],
)
def test_impact_zone(offset, sigma, club, zone):
    assert impact_zone(offset, sigma, club) == zone


def test_a_wide_d_sigma_leaves_the_zone_open_but_shows_the_offset():
    result = _result(offset_mm=6.2)
    point = result.path.impact_point
    wide = ClubDataResult(
        result.contact,
        None,
        replace(result.path, impact_point=replace(point, sigma_mm=4.0)),
        {},
    )

    fields = _calculate(wide).fields

    assert fields["impact_offset_mm"] == 6.2
    assert fields["impact_zone"] is None
