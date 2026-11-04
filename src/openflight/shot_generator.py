"""One generator for simulated shots, shared by the mock monitor and the demo."""

from datetime import datetime
from typing import Protocol, Sequence, TypeVar

from .clubs import ClubType
from .clubs.physics import (
    SHOT_SIMULATION_DEFAULTS,
    get_club_physics,
    get_club_simulation_profile,
)
from .launch_monitor import Shot

_T = TypeVar("_T")


class RandomSource(Protocol):
    """What a simulated shot draws from; the ``random`` module and ``random.Random`` fit."""

    def gauss(self, mu: float, sigma: float) -> float:
        """Draw from a normal distribution."""

    def uniform(self, a: float, b: float) -> float:
        """Draw evenly between a and b."""

    def choice(self, seq: Sequence[_T]) -> _T:
        """Pick one element."""


def generate_shot(
    club: ClubType,
    rng: RandomSource,
    *,
    ball_speed_mph: float | None = None,
    mode: str = "mock",
) -> Shot:
    """Draw one realistic, TrackMan-based shot for a club; carry and flight are not computed.

    The order of the draws is fixed: a seeded source keeps producing the same shots.
    """
    physics = get_club_physics(club)
    profile = get_club_simulation_profile(club)
    defaults = SHOT_SIMULATION_DEFAULTS

    if ball_speed_mph is None:
        ball_speed_mph = max(
            defaults.min_ball_speed_mph,
            min(
                defaults.max_ball_speed_mph,
                rng.gauss(
                    physics.average_ball_speed_mph,
                    profile.ball_speed_std_dev_mph,
                ),
            ),
        )

    smash_factor = profile.average_smash + rng.uniform(
        -defaults.smash_variation, defaults.smash_variation
    )
    club_speed = ball_speed_mph / smash_factor

    spin_rpm = max(
        defaults.min_spin_rpm,
        rng.gauss(profile.average_spin_rpm, profile.spin_std_dev_rpm),
    )

    launch_v = max(
        defaults.min_launch_deg,
        rng.gauss(physics.optimal_launch_deg, profile.launch_std_dev_deg),
    )
    launch_h = rng.gauss(0, defaults.horizontal_launch_std_dev_deg)
    launch_confidence = round(rng.uniform(defaults.confidence_min, defaults.confidence_max), 2)

    club_aoa = round(
        rng.gauss(
            defaults.angle_of_attack_mean_deg,
            defaults.angle_of_attack_std_dev_deg,
        ),
        1,
    )
    spin_confidence = rng.choice(defaults.spin_confidence_choices)
    club_path = round(
        rng.uniform(-defaults.club_path_max_abs_deg, defaults.club_path_max_abs_deg),
        1,
    )
    spin_axis = round(
        launch_h
        - rng.uniform(
            -defaults.spin_axis_error_max_abs_deg,
            defaults.spin_axis_error_max_abs_deg,
        ),
        1,
    )

    return Shot(
        ball_speed_mph=ball_speed_mph,
        club_speed_mph=club_speed,
        timestamp=datetime.now(),
        club=club,
        spin_rpm=spin_rpm,
        spin_confidence=spin_confidence,
        launch_angle_vertical=round(launch_v, 1),
        launch_angle_horizontal=round(launch_h, 1),
        launch_angle_confidence=launch_confidence,
        launch_angle_vertical_confidence=launch_confidence,
        launch_angle_horizontal_confidence=launch_confidence,
        launch_angle_vertical_source="mock",
        launch_angle_horizontal_source="mock",
        angle_source="mock",
        club_angle_deg=club_aoa,
        club_path_deg=club_path,
        spin_axis_deg=spin_axis,
        mode=mode,
    )
