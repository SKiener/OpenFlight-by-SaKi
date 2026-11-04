"""Heel/toe impact location and the ball that left, on synthetic rear-camera clips."""

import math
import pickle
from dataclasses import replace

import numpy as np
import pytest
from test_club_head_path import (
    CLUB_SPEED_MPH,
    GEOMETRY,
    HEIGHT,
    WIDTH,
    Scenario,
    _paint_disk,
    _paint_polygon,
    estimate,
    render_clip,
)

import openflight.camera.club_head_path as chp
from openflight.camera.club_head_path import (
    IMPACT_BALL_SIGMA_MM,
    IMPACT_EDGE_SIGMA_MM,
    ImpactLocation,
    _poly_intercept,
    locate_departed_ball,
)
from openflight.camera.club_motion import ReferenceBall

TOE_MM = 12.0


@pytest.fixture(name="toe_clip", scope="module")
def fixture_toe_clip():
    """A right-handed strike with the ball 12 mm toe-ward of the face centre."""
    return render_clip(Scenario(ball_lateral_m=TOE_MM / 1e3))


@pytest.fixture(name="arc_toe_clip", scope="module")
def fixture_arc_toe_clip():
    """The same strike on a swing arc with an out-to-in face drift (lateral speed at contact)."""
    return render_clip(Scenario(ball_lateral_m=TOE_MM / 1e3, attack_deg=-5.0, path_deg=3.0))


def _impact(clip, **overrides):
    frames, timestamps, contact, ball = clip
    kwargs = {"departed_ball": ball, "attack": False}
    kwargs.update(overrides)
    return estimate(frames, timestamps, contact, ball, **kwargs).impact


def test_a_right_handed_toe_strike_reads_toe(toe_clip):
    impact = _impact(toe_clip)

    assert impact.status == "accepted"
    assert impact.toe_mm == pytest.approx(TOE_MM, abs=2.0)
    assert impact.handedness == "right"
    assert impact.ball_reference == "given"
    assert impact.hosel_vote < 0.0  # the shaft leaves the head on its image-left side
    assert 50.0 <= impact.head_length_mm <= 95.0  # an iron-sized synthetic head


def test_a_mirrored_left_handed_toe_strike_reads_toe():
    clip = render_clip(Scenario(ball_lateral_m=-TOE_MM / 1e3, left_handed=True))

    impact = _impact(clip, handedness="left")

    assert impact.status == "accepted"
    assert impact.toe_mm == pytest.approx(TOE_MM, abs=2.0)
    assert impact.hosel_vote > 0.0


def test_a_wrong_handedness_setting_is_caught_by_the_hosel_side(toe_clip):
    impact = _impact(toe_clip, handedness="left")

    assert impact.status == "rejected_hosel_side_disagrees"
    assert impact.toe_mm == pytest.approx(-TOE_MM, abs=2.0)


def test_the_quadratic_intercept_is_exact_on_a_quadratic_track():
    tau_ms = np.array([-12.0, -8.5, -5.0, -1.5])
    values = 0.004 - 0.0013 * tau_ms + 0.00002 * tau_ms**2

    intercept, slope, sigma = _poly_intercept(tau_ms, values, 2, 1e-5)

    assert intercept == pytest.approx(0.004, abs=1e-12)
    assert slope == pytest.approx(-0.0013, abs=1e-12)
    assert sigma > 0.0  # the noise floor stays


def test_the_impact_uses_the_quadratic_and_logs_the_linear_variant(arc_toe_clip):
    impact = _impact(arc_toe_clip)

    assert impact.status == "accepted"
    assert set(impact.variants_mm) == {"linear_last3", "centroid_quadratic"}
    assert impact.toe_mm == pytest.approx(TOE_MM, abs=2.0)
    assert impact.toe_mm != impact.variants_mm["linear_last3"]


def test_the_impact_sigma_is_the_quadrature_of_its_terms(arc_toe_clip):
    impact = _impact(arc_toe_clip, contact_sigma_s=2e-3)

    assert impact.contact_sigma_mm == pytest.approx(
        abs(impact.lateral_speed_mm_per_ms) * 2.0, abs=0.01
    )
    assert impact.contact_sigma_mm > 1.0
    assert (
        (impact.edge_sigma_mm, impact.ball_sigma_mm)
        == (
            IMPACT_EDGE_SIGMA_MM,
            IMPACT_BALL_SIGMA_MM,
        )
        == (3.0, 0.5)
    )
    assert impact.sigma_mm == pytest.approx(
        math.sqrt(impact.fit_sigma_mm**2 + 3.0**2 + impact.contact_sigma_mm**2 + 0.5**2),
        abs=0.02,
    )


def test_a_three_frame_track_is_too_short(toe_clip):
    frames, timestamps, contact, ball = toe_clip
    result = estimate(frames, timestamps, contact, ball, departed_ball=ball, attack=False)
    usable = [item for item in result.observations if item.quality.ok]
    run = chp._PathRun(replace(result, observations=tuple(usable[-3:])))
    scene = chp._build_scene(
        frames.shape[1:],
        (timestamps / 1e9, contact),
        (ball, ball),
        GEOMETRY,
        CLUB_SPEED_MPH / chp.MPH_PER_MS,
    )

    impact = chp._impact_location(
        run,
        scene,
        club_speed_mph=CLUB_SPEED_MPH,
        contact_sigma_s=0.0,
        handedness="right",
        ball=(ball, "given"),
        head_length_median_mm=None,
    )

    assert impact.status == "rejected_short_track"


def test_a_long_gap_to_contact_is_not_extrapolated(toe_clip):
    """A contact 3.5 ms after the head's last frame leaves a 5 ms gap."""
    frames, timestamps, contact, ball = toe_clip

    impact = estimate(
        frames, timestamps, contact + 3.5e-3, ball, departed_ball=ball, attack=False
    ).impact

    assert impact.status == "rejected_extrapolation_gap"
    assert impact.extrapolation_gap_ms > 4.0


def test_a_whiff_far_from_the_face_is_implausible(toe_clip):
    frames, timestamps, contact, ball = toe_clip
    far = ReferenceBall(ball.x + 0.118 * 576.0 / 1.47, ball.y, ball.diameter_px, ball.area_px)

    impact = estimate(frames, timestamps, contact, ball, departed_ball=far, attack=False).impact

    assert impact.status == "rejected_implausible_offset"
    assert impact.toe_mm == pytest.approx(130.0, abs=8.0)


def test_a_head_length_off_the_clubs_median_is_rejected(toe_clip):
    impact = _impact(toe_clip, head_length_median_mm=50.0)

    assert impact.status == "rejected_head_length"
    assert _impact(toe_clip, head_length_median_mm=impact.head_length_mm).status == "accepted"


def test_a_head_length_outside_the_range_is_rejected(toe_clip, monkeypatch):
    """The synthetic head reads ~67 mm; a range starting at 70 mm stands in for a 30 mm head."""
    monkeypatch.setattr(chp, "IMPACT_HEAD_LENGTH_RANGE_MM", (70.0, 127.0))

    assert _impact(toe_clip).status == "rejected_head_length"


@pytest.mark.parametrize(
    ("head_length_mm", "median_mm", "passes"),
    [
        (104.0, None, True),  # a driver with woods segmentation, no session median yet
        (126.0, None, True),
        (128.0, None, False),  # past a driver's 127 mm (USGA 5 in)
        (45.0, None, False),
        (104.0, 98.0, True),  # inside the +/-20 % band around the median
        (104.0, 80.0, False),  # outside it, although inside the absolute range
        (126.0, 110.0, True),
        (130.0, 115.0, False),  # the band never lifts the absolute bound
    ],
)
def test_the_head_length_gate_takes_driver_heads_up_to_127_mm(head_length_mm, median_mm, passes):
    assert chp._head_length_passes(head_length_mm, median_mm) is passes
    assert chp.head_length_plausible(head_length_mm) is (50.0 <= head_length_mm <= 127.0)


def test_a_large_contact_sigma_fails_the_impact_sigma(arc_toe_clip):
    impact = _impact(arc_toe_clip, contact_sigma_s=10e-3)

    assert impact.status == "rejected_sigma_too_large"
    assert impact.sigma_mm > 10.0


def test_no_club_speed_gives_no_impact(toe_clip):
    assert _impact(toe_clip, club_speed_mph=None).status == "rejected_no_club_speed"


def test_no_confirmed_ball_gives_no_impact(toe_clip):
    """The clip ends at contact, so nothing can be seen leaving, and no ball is given."""
    impact = _impact(toe_clip, departed_ball=None)

    assert impact.status == "rejected_ball_not_confirmed"


def test_the_impact_is_accepted_while_the_path_fails_its_sigma(arc_toe_clip):
    frames, timestamps, contact, ball = arc_toe_clip

    result = estimate(
        frames, timestamps, contact, ball, departed_ball=ball, contact_sigma_s=5e-3, attack=False
    )

    assert result.status == "rejected_sigma_too_large"
    assert result.impact.status == "accepted"


def test_the_impact_location_pickles_and_serialises(toe_clip):
    impact = _impact(toe_clip)

    assert pickle.loads(pickle.dumps(impact)) == impact
    data = impact.to_dict()
    assert data["status"] == "accepted"
    assert data["ball_px"] == list(impact.ball_px)
    assert set(data) == {
        "status",
        "toe_mm",
        "sigma_mm",
        "fit_sigma_mm",
        "edge_sigma_mm",
        "contact_sigma_mm",
        "ball_sigma_mm",
        "feature",
        "head_length_mm",
        "extrapolation_gap_ms",
        "lateral_speed_mm_per_ms",
        "ball_reference",
        "ball_px",
        "ball_shift_mm",
        "handedness",
        "hosel_vote",
        "variants_mm",
    }
    assert ImpactLocation("accepted").accepted


# --- the ball that left ------------------------------------------------------------------


def _departing_clip(*, ball_value: float | None = None, tee: bool = False, decoy_dx=None):
    """A clip long enough for ``departed_ball`` (45 frames after contact); the ball leaves."""
    frames, timestamps, contact, ball = render_clip(
        Scenario(ball_departs=True, extra_frames=40, seed=7)
    )
    frames = frames.astype(np.float64)
    contact_index = int(np.searchsorted(timestamps / 1e9, contact))
    if ball_value is not None:
        # The ball 20 grey levels off the mat (105) before it leaves.
        disk = np.zeros((HEIGHT, WIDTH))
        _paint_disk(disk, np.array([ball.x, ball.y]), ball.diameter_px / 2.0, 1.0)
        frames[:contact_index] += disk * (ball_value - frames[:contact_index]) * (disk > 0)
    if tee:
        stem = np.zeros((HEIGHT, WIDTH))
        top = ball.y + ball.diameter_px / 2.0
        _paint_polygon(
            stem,
            np.array(
                [
                    [ball.x - 3, top],
                    [ball.x + 3, top],
                    [ball.x + 3, top + 14],
                    [ball.x - 3, top + 14],
                ]
            ),
            1.0,
        )
        frames = frames * (1.0 - stem) + 45.0 * stem
    if decoy_dx is not None:
        decoy = np.zeros((HEIGHT, WIDTH))
        _paint_disk(decoy, np.array([ball.x + decoy_dx, ball.y]), ball.diameter_px / 2.0, 1.0)
        frames = frames * (1.0 - decoy) + 30.0 * decoy
    return np.clip(np.round(frames), 0, 255).astype(np.uint8), timestamps, contact, ball


def test_the_ball_that_left_is_found_where_it_lay():
    frames, timestamps, contact, ball = _departing_clip()

    found = locate_departed_ball(
        frames,
        timestamps,
        contact_time_s=contact,
        references=(ball,),
        expected_diameter_px=ball.diameter_px,
    )

    assert found is not None
    assert math.hypot(found.x - ball.x, found.y - ball.y) <= 0.3
    assert found.reference_index == 0


def test_a_ball_that_stays_is_not_confirmed():
    frames, timestamps, contact, ball = render_clip(Scenario(extra_frames=40, seed=7))

    assert (
        locate_departed_ball(
            frames,
            timestamps,
            contact_time_s=contact,
            references=(ball,),
            expected_diameter_px=ball.diameter_px,
        )
        is None
    )


@pytest.mark.parametrize("decoy_dx", [50.0, 60.0, 70.0])
def test_a_reference_on_a_decoy_still_finds_the_ball_that_left(decoy_dx):
    """A static spare ball beside the tee never disappears. (The decoys sit on the side away
    from the synthetic leg, whose steady drift would change the image inside the box.)"""
    frames, timestamps, contact, ball = _departing_clip(decoy_dx=decoy_dx)
    decoy = ReferenceBall(ball.x + decoy_dx, ball.y, ball.diameter_px, ball.area_px)

    found = locate_departed_ball(
        frames,
        timestamps,
        contact_time_s=contact,
        references=(decoy,),
        expected_diameter_px=ball.diameter_px,
    )

    assert found is not None
    assert math.hypot(found.x - ball.x, found.y - ball.y) <= 0.5
    assert found.shift_px > 40.0


def test_a_teed_ball_with_a_still_tee_is_found():
    frames, timestamps, contact, ball = _departing_clip(tee=True)

    found = locate_departed_ball(
        frames,
        timestamps,
        contact_time_s=contact,
        references=(ball,),
        expected_diameter_px=ball.diameter_px,
    )

    assert found is not None
    assert math.hypot(found.x - ball.x, found.y - ball.y) <= 0.5


def test_a_ball_twenty_grey_levels_off_the_mat_is_found():
    frames, timestamps, contact, ball = _departing_clip(ball_value=85.0)

    found = locate_departed_ball(
        frames,
        timestamps,
        contact_time_s=contact,
        references=(ball,),
        expected_diameter_px=ball.diameter_px,
    )

    assert found is not None
    assert math.hypot(found.x - ball.x, found.y - ball.y) <= 0.5


def test_the_strongest_reference_wins_and_names_itself():
    frames, timestamps, contact, ball = _departing_clip()
    far_tee = ReferenceBall(ball.x + 250.0, ball.y, ball.diameter_px, ball.area_px)

    found = locate_departed_ball(
        frames,
        timestamps,
        contact_time_s=contact,
        references=(far_tee, None, ball),
        expected_diameter_px=ball.diameter_px,
    )

    assert found.reference_index == 2


@pytest.mark.parametrize(
    "frames, timestamps",
    [
        (np.zeros((0, 4, 4), np.uint8), np.zeros(0, np.int64)),
        (np.zeros((3, 4, 4), np.uint8), np.zeros(2, np.int64)),
        (np.zeros((4, 4), np.uint8), np.zeros(4, np.int64)),
    ],
)
def test_locating_never_raises_on_empty_input(frames, timestamps):
    ball = ReferenceBall(2.0, 2.0, 16.0, 200)

    assert (
        locate_departed_ball(
            frames, timestamps, contact_time_s=0.0, references=(ball,), expected_diameter_px=16.0
        )
        is None
    )


def test_locating_without_after_frames_never_raises():
    frames, timestamps, contact, ball = _departing_clip()
    end = int(np.searchsorted(timestamps / 1e9, contact)) + 2

    assert (
        locate_departed_ball(
            frames[:end],
            timestamps[:end],
            contact_time_s=contact,
            references=(ball,),
            expected_diameter_px=ball.diameter_px,
        )
        is None
    )


def test_the_impact_finds_its_ball_near_the_club_ball_or_the_tee():
    frames, timestamps, contact, ball = _departing_clip()

    near_ball = estimate(frames, timestamps, contact, ball, attack=False).impact
    tee = ReferenceBall(ball.x + 0.4, ball.y - 0.3, ball.diameter_px, ball.area_px)
    with_tee = estimate(frames, timestamps, contact, ball, attack=False, confirmed_tee=tee).impact

    assert near_ball.status == "accepted"
    assert near_ball.ball_reference == "ball"
    assert near_ball.toe_mm == pytest.approx(0.0, abs=2.0)
    assert near_ball.ball_shift_mm == pytest.approx(0.0, abs=0.5)
    assert with_tee.status == "accepted"
    assert with_tee.ball_reference in ("ball", "tee")
