"""The demo session: automatic demo shots held in memory, never stored or forwarded."""

import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .club_data import IMPACT_HEIGHT_CLUBS, impact_zone
from .clubs import ClubType
from .clubs.bag import club_category
from .clubs.physics import get_club_physics
from .dplane import (
    LAUNCH_DIRECTION_SIGMA_DEG,
    WOOD_CATEGORIES,
    dplane_face,
    face_weight_for_club,
    spin_loft_deg,
)
from .launch_monitor import Shot, summarize_shots
from .shot_generator import RandomSource, generate_shot

DEMO_STARTER_COUNT = 6
DEMO_STARTER_SPACING_S = 10.0
DEMO_INTERVAL_S = 20.0
DEMO_IDLE_TIMEOUT_S = 15 * 60.0

_EVERY_CLUB = tuple(club for club in ClubType if club is not ClubType.UNKNOWN)

# Demo club-face data (``demo_face_fields``), in the frames of the real club data:
# the heel/toe offset in mm (+ toe, right-handed) and the face height as the ball
# centre above the head's lower edge (``camera.impact_point``; the UI's ImpactFace y).
# A typical amateur spread, slightly toward the toe: (mean, sd) in mm per face kind.
DEMO_IMPACT_OFFSET_MM = {"woods": (2.0, 11.0), "irons": (1.5, 8.0)}
# Kept on the face: the UI's iron blade spans about -38..+39 mm, the fairway-wood
# face +-48 mm and the driver +-58 mm; these limits keep a ball mark inside them.
DEMO_IMPACT_MAX_ABS_MM = {"woods": 40.0, "irons": 30.0}
# Method D's typical sigma (its feature fallback is 3 mm), (mean, sd) and its clamp.
DEMO_IMPACT_SIGMA_MM = (3.0, 0.5)
DEMO_IMPACT_SIGMA_RANGE_MM = (2.0, 4.5)
# Face height on a teed wood or driver: about the middle of the face (the UI's driver
# face is 56 mm high, a fairway wood's 38 mm) +- a few mm, as (mean, sd, low, high).
_FAIRWAY_HEIGHT_MM = (19.0, 3.0, 9.0, 29.0)
DEMO_IMPACT_HEIGHT_MM = {
    ClubType.DRIVER: (28.0, 4.0, 14.0, 42.0),
    ClubType.WOOD_3: _FAIRWAY_HEIGHT_MM,
    ClubType.WOOD_5: _FAIRWAY_HEIGHT_MM,
    ClubType.WOOD_7: _FAIRWAY_HEIGHT_MM,
}
# Method D's height sigma: its frame term (1.03 mm) and model term (2 mm) in quadrature.
DEMO_IMPACT_HEIGHT_SIGMA_MM = 2.2
# The driver is always teed (as the server assumes); a fairway wood now and then.
DEMO_FAIRWAY_TEED_PROBABILITY = 0.25
# The sigmas a good camera shot carries into the D-plane face: a camera start direction
# (``camera_assisted_high``) and a camera club path well inside its 2 deg gate.
DEMO_LAUNCH_DIRECTION_SIGMA_DEG = LAUNCH_DIRECTION_SIGMA_DEG["camera_assisted_high"]
DEMO_CLUB_PATH_SIGMA_DEG = 1.0


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def demo_face_to_path_deg(shot: Shot) -> float:
    """The face to path that gives the shot's own spin axis under the D-plane model.

    The inverse of ``dplane.spin_axis_deg(f2p, spin loft)``, with the spin loft from the
    shot's launch angle and attack angle (``dplane.spin_loft_deg``), so its sign is the
    curve's: a draw (negative axis) comes out closed, a fade open. Without
    SPIN_AXIS_OFFSET_DEG: that provisional offset corrects the camera's measured face
    to path, a bias a simulated shot does not have (with it, a straight wedge would
    read closed and an open face would hardly ever occur).
    """
    k = face_weight_for_club(shot.club)
    loft = spin_loft_deg(shot.launch_angle_vertical, shot.club_angle_deg, k)
    sine = math.tan(math.radians(shot.spin_axis_deg)) * math.tan(math.radians(loft))
    return math.degrees(math.asin(_clamp(sine, -1.0, 1.0)))


def demo_face_fields(shot: Shot, rng: RandomSource) -> dict:
    """Club-face data for a demo shot, keyed and framed as the camera club data sets it.

    Face to path from the shot's own spin axis (``demo_face_to_path_deg``); its value,
    sigma and class come from ``dplane.dplane_face`` with camera sigmas, fed the start
    direction that the shot's club path and that face to path imply. The impact point is
    drawn from an amateur spread, its zone from ``club_data.impact_zone``, method "D",
    no scale; the face height only on a teed wood or driver. The draws come in a fixed
    order, so a seeded source repeats the same data.
    """
    club = shot.club
    face = dplane_face(
        club,
        launch_direction_deg=shot.club_path_deg
        + face_weight_for_club(club) * demo_face_to_path_deg(shot),
        launch_direction_sigma_deg=DEMO_LAUNCH_DIRECTION_SIGMA_DEG,
        club_path_deg=shot.club_path_deg,
        club_path_sigma_deg=DEMO_CLUB_PATH_SIGMA_DEG,
        launch_vertical_deg=shot.launch_angle_vertical,
        attack_angle_deg=shot.club_angle_deg,
    )

    group = "woods" if club in WOOD_CATEGORIES else "irons"
    mean, spread = DEMO_IMPACT_OFFSET_MM[group]
    limit = DEMO_IMPACT_MAX_ABS_MM[group]
    offset = round(_clamp(rng.gauss(mean, spread), -limit, limit), 1)
    sigma = round(_clamp(rng.gauss(*DEMO_IMPACT_SIGMA_MM), *DEMO_IMPACT_SIGMA_RANGE_MM), 1)

    height = height_sigma = None
    if club in IMPACT_HEIGHT_CLUBS:
        teed = club is ClubType.DRIVER or rng.uniform(0.0, 1.0) < DEMO_FAIRWAY_TEED_PROBABILITY
        if teed:
            middle, spread, low, high = DEMO_IMPACT_HEIGHT_MM[club]
            height = round(_clamp(rng.gauss(middle, spread), low, high), 1)
            height_sigma = DEMO_IMPACT_HEIGHT_SIGMA_MM

    return {
        "face_to_path_deg": round(face.face_to_path_deg, 1),
        "face_to_path_sigma_deg": round(face.face_to_path_sigma_deg, 1),
        "face_to_path_class": face.face_to_path_class,
        "impact_offset_mm": offset,
        "impact_sigma_mm": sigma,
        "impact_toe_mm": offset,
        "impact_height_mm": height,
        "impact_height_sigma_mm": height_sigma,
        "impact_zone": impact_zone(offset, sigma, club),
        "impact_method": "D",
        "impact_scale_mm_per_px": None,
    }


def _bag_club(selection: str) -> ClubType | None:
    try:
        club = club_category(selection)
    except (TypeError, ValueError):
        return None
    return None if club is ClubType.UNKNOWN else club


def demo_clubs(bag_ids: Sequence[str] | None) -> list[ClubType]:
    """The clubs a demo rotates through, from the slowest to the fastest ball speed.

    A bag id that resolves to no club is skipped; an empty or unset bag means every club.
    A bag with more than DEMO_STARTER_COUNT clubs is thinned out evenly, keeping both ends.
    """
    bag = [club for club in map(_bag_club, bag_ids or ()) if club is not None]
    ordered = sorted(
        dict.fromkeys(bag or _EVERY_CLUB),
        key=lambda club: get_club_physics(club).average_ball_speed_mph,
    )
    if len(ordered) <= DEMO_STARTER_COUNT:
        return ordered
    last = len(ordered) - 1
    return [ordered[round(i * last / (DEMO_STARTER_COUNT - 1))] for i in range(DEMO_STARTER_COUNT)]


@dataclass
class _Run:
    """One switched-on demo: its clubs, shots and clocks.

    ``made`` counts every shot made, deleted or failed ones included, so it picks the next
    club and the next shot number, and a number never repeats within the run.
    """

    clubs: list[ClubType]
    next_due_s: float
    last_touch_s: float
    shots: list[Shot] = field(default_factory=list)
    made: int = 0


class DemoSession:
    """A demo held in memory: starter shots, then one shot per interval, until it ends.

    Time comes in as arguments, so a test drives it without sleeping. Every method holds
    one lock, and a shot is visible only once finalized. ``finalize`` runs under that lock,
    which is not re-entrant, so it must not call back into the session.
    """

    def __init__(
        self,
        *,
        rng: RandomSource,
        finalize: Callable[[Shot], None],
        interval_s: float = DEMO_INTERVAL_S,
        idle_timeout_s: float = DEMO_IDLE_TIMEOUT_S,
    ) -> None:
        self._rng = rng
        self._finalize = finalize
        self._interval_s = interval_s
        self._idle_timeout_s = idle_timeout_s
        self._lock = threading.Lock()
        self._run: _Run | None = None

    @property
    def active(self) -> bool:
        """Whether the demo is on."""
        with self._lock:
            return self._run is not None

    def start(
        self,
        *,
        now_s: float,
        wall: datetime,
        bag_ids: Sequence[str] | None,
        profile_id: str,
        profile_name: str,
    ) -> list[Shot]:
        """Switch the demo on and return its starter shots; [] when it is already on.

        The starter shots end at ``wall``. If finalizing one fails, the demo stays off.
        """
        with self._lock:
            if self._run is not None:
                return []
            run = _Run(
                clubs=demo_clubs(bag_ids),
                next_due_s=now_s + self._interval_s,
                last_touch_s=now_s,
            )
            for index in range(DEMO_STARTER_COUNT):
                age_s = (DEMO_STARTER_COUNT - 1 - index) * DEMO_STARTER_SPACING_S
                self._add_shot(run, wall - timedelta(seconds=age_s), profile_id, profile_name)
            self._run = run
            return list(run.shots)

    def stop(self) -> bool:
        """Switch the demo off and forget its shots; False when it was not on."""
        with self._lock:
            was_on = self._run is not None
            self._run = None
            return was_on

    def touch(self, now_s: float) -> None:
        """Note a touch on any screen, which postpones the idle expiry."""
        with self._lock:
            if self._run is not None:
                self._run.last_touch_s = now_s

    def due_shot(
        self, *, now_s: float, wall: datetime, profile_id: str, profile_name: str
    ) -> Shot | None:
        """The next demo shot once its time has come, else None.

        A tick that comes late yields one shot, not the ones it missed. The schedule moves
        on before the shot is made, so a failing finalize costs one shot, not a retry loop.
        """
        with self._lock:
            run = self._run
            if run is None or now_s < run.next_due_s:
                return None
            run.next_due_s += self._interval_s
            if run.next_due_s <= now_s:
                run.next_due_s = now_s + self._interval_s
            return self._add_shot(run, wall, profile_id, profile_name)

    def expired(self, now_s: float) -> bool:
        """Whether the demo has run unattended for the idle timeout."""
        with self._lock:
            return self._run is not None and now_s - self._run.last_touch_s >= self._idle_timeout_s

    def shots(self) -> list[Shot]:
        """The demo shots, oldest first, as a new list."""
        with self._lock:
            return list(self._run.shots) if self._run is not None else []

    def stats(self) -> dict:
        """The session summary of the demo shots."""
        with self._lock:
            return summarize_shots(self._run.shots if self._run is not None else [], mode="demo")

    def delete(self, timestamp: str) -> bool:
        """Remove the shot whose ``Shot.to_dict()`` timestamp this is; False if none matches."""
        with self._lock:
            shots = self._run.shots if self._run is not None else []
            for index, shot in enumerate(shots):
                if shot.timestamp.isoformat() == timestamp:
                    del shots[index]
                    return True
            return False

    def clear(self, profile_id: str) -> None:
        """Remove the shots of one profile; the other profiles' shots stay."""
        with self._lock:
            if self._run is not None:
                self._run.shots[:] = [
                    shot for shot in self._run.shots if shot.profile_id != profile_id
                ]

    def _add_shot(self, run: _Run, timestamp: datetime, profile_id: str, profile_name: str) -> Shot:
        """Make the run's next shot, finalize it and add it. The caller holds the lock."""
        club = run.clubs[run.made % len(run.clubs)]
        shot = generate_shot(club, self._rng, mode="demo")
        for name, value in demo_face_fields(shot, self._rng).items():
            setattr(shot, name, value)
        shot.demo = True
        shot.shot_number = run.made + 1
        run.made += 1
        shot.timestamp = timestamp
        shot.profile_id = profile_id
        shot.profile_name = profile_name
        self._finalize(shot)
        run.shots.append(shot)
        return shot
