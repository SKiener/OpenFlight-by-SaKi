"""The mock monitor and the demo draw their shots from one generator."""

import random
from random import Random

import pytest

from openflight.clubs import ClubType
from openflight.clubs.physics import SHOT_SIMULATION_DEFAULTS, get_club_simulation_profile
from openflight.server import MockLaunchMonitor
from openflight.shot_generator import generate_shot

# Recorded from MockLaunchMonitor.simulate_shot before it moved into the generator
# (CPython 3.11.3, random.seed(1234)); a reordered draw changes every value after it.
GOLDEN_DRIVER = {
    "ball_speed_mph": 155.65063570312685,
    "club_speed_mph": 109.57843766763072,
    "smash_factor": 1.4204494882035152,
    "spin_rpm": 2609.7770976997253,
    "spin_confidence": 0.7,
    "launch_angle_vertical": 15.0,
    "launch_angle_horizontal": -2.5,
    "launch_angle_confidence": 0.76,
    "launch_angle_vertical_confidence": 0.76,
    "launch_angle_horizontal_confidence": 0.76,
    "club_angle_deg": -4.5,
    "club_path_deg": -2.6,
    "spin_axis_deg": 2.2,
    "launch_angle_vertical_source": "mock",
    "launch_angle_horizontal_source": "mock",
    "angle_source": "mock",
    "mode": "mock",
    "club_selection": "driver",
}
GOLDEN_IRON_7 = {
    "ball_speed_mph": 107.37953749349067,
    "club_speed_mph": 86.5650222073963,
    "smash_factor": 1.2404494882035153,
    "spin_rpm": 5864.665646549588,
    "spin_confidence": 0.7,
    "launch_angle_vertical": 25.5,
    "launch_angle_horizontal": -2.5,
    "launch_angle_confidence": 0.76,
    "launch_angle_vertical_confidence": 0.76,
    "launch_angle_horizontal_confidence": 0.76,
    "club_angle_deg": -4.5,
    "club_path_deg": -2.6,
    "spin_axis_deg": 2.2,
    "launch_angle_vertical_source": "mock",
    "launch_angle_horizontal_source": "mock",
    "angle_source": "mock",
    "mode": "mock",
    "club_selection": "7-iron",
}


@pytest.fixture(name="seeded_random")
def fixture_seeded_random():
    state = random.getstate()
    random.seed(1234)
    yield
    random.setstate(state)


@pytest.mark.parametrize(
    "club, expected",
    [
        pytest.param(None, GOLDEN_DRIVER, id="default-club"),
        pytest.param(ClubType.IRON_7, GOLDEN_IRON_7, id="iron-7"),
    ],
)
def test_mock_monitor_values_are_unchanged(seeded_random, club, expected):
    del seeded_random
    monitor = MockLaunchMonitor()
    if club is not None:
        monitor.set_club(club)

    shot = monitor.simulate_shot()

    actual = {name: getattr(shot, name) for name in expected}
    numeric = {name for name, value in expected.items() if isinstance(value, (int, float))}
    # pytest.approx rejects strings, so labels compare exactly and only numbers use a tolerance.
    assert {name: actual[name] for name in numeric} == pytest.approx(
        {name: expected[name] for name in numeric}, rel=1e-9
    )
    assert {name: actual[name] for name in expected.keys() - numeric} == {
        name: expected[name] for name in expected.keys() - numeric
    }


def _fields(shot):
    return {name: value for name, value in shot.to_dict().items() if name != "timestamp"}


def test_generator_is_deterministic_per_random_source():
    first = generate_shot(ClubType.IRON_7, Random(7))
    second = generate_shot(ClubType.IRON_7, Random(7))
    other = generate_shot(ClubType.IRON_7, Random(8))

    assert _fields(first) == _fields(second)
    assert _fields(first) != _fields(other)


@pytest.mark.parametrize("club", list(ClubType), ids=lambda club: club.value)
def test_generated_values_stay_within_the_simulation_limits(club):
    limits = SHOT_SIMULATION_DEFAULTS
    rng = Random(2026)

    for _ in range(500):
        shot = generate_shot(club, rng)

        assert shot.club is club
        assert limits.min_ball_speed_mph <= shot.ball_speed_mph <= limits.max_ball_speed_mph
        assert shot.spin_rpm >= limits.min_spin_rpm
        assert shot.launch_angle_vertical >= limits.min_launch_deg


def test_a_given_ball_speed_is_kept():
    shot = generate_shot(ClubType.IRON_7, Random(1), ball_speed_mph=95.0)
    average_smash = get_club_simulation_profile(ClubType.IRON_7).average_smash

    assert shot.ball_speed_mph == 95.0
    assert shot.smash_factor == pytest.approx(
        average_smash, abs=SHOT_SIMULATION_DEFAULTS.smash_variation
    )


def test_generator_stamps_the_mode():
    assert generate_shot(ClubType.PW, Random(1)).mode == "mock"
    assert generate_shot(ClubType.PW, Random(1), mode="demo").mode == "demo"


# Only the demo session adds face data (demo.demo_face_fields); mock shots show "—" there.
_FACE_DATA_FIELDS = (
    "face_to_path_deg",
    "face_to_path_sigma_deg",
    "face_to_path_class",
    "impact_offset_mm",
    "impact_sigma_mm",
    "impact_toe_mm",
    "impact_height_mm",
    "impact_height_sigma_mm",
    "impact_zone",
    "impact_method",
    "impact_scale_mm_per_px",
)


@pytest.mark.parametrize("mode", ["mock", "demo"])
@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.IRON_7], ids=lambda club: club.value)
def test_the_generator_itself_adds_no_face_data(club, mode):
    payload = generate_shot(club, Random(3), mode=mode).to_dict()

    assert {name: payload[name] for name in _FACE_DATA_FIELDS} == dict.fromkeys(_FACE_DATA_FIELDS)


def test_mock_monitor_shots_carry_no_face_data(seeded_random):
    del seeded_random
    monitor = MockLaunchMonitor()
    monitor.set_club(ClubType.DRIVER)

    payload = monitor.simulate_shot().to_dict()

    assert {name: payload[name] for name in _FACE_DATA_FIELDS} == dict.fromkeys(_FACE_DATA_FIELDS)
