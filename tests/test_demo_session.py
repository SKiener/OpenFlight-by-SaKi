"""The demo session: starter shots, one shot per interval, expiry, and its own bookkeeping."""

import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from random import Random

import pytest

from openflight import server
from openflight.club_data import IMPACT_HEIGHT_CLUBS, impact_zone
from openflight.clubs import ClubType
from openflight.clubs.physics import get_club_physics
from openflight.demo import (
    DEMO_IDLE_TIMEOUT_S,
    DEMO_IMPACT_HEIGHT_MM,
    DEMO_IMPACT_MAX_ABS_MM,
    DEMO_INTERVAL_S,
    DEMO_STARTER_COUNT,
    DEMO_STARTER_SPACING_S,
    DemoSession,
    demo_clubs,
    demo_face_fields,
    demo_face_to_path_deg,
)
from openflight.dplane import (
    CLASS_Z,
    F2P_CLASSES,
    WOOD_CATEGORIES,
    classify,
    curve_class,
    face_weight_for_club,
    spin_axis_deg,
    spin_loft_deg,
)
from openflight.shot_generator import generate_shot

WALL = datetime(2026, 10, 7, 12, 0, 0)
T0 = 100.0
PROFILE = {"profile_id": "p1", "profile_name": "Golfer"}


def _speed(club):
    return get_club_physics(club).average_ball_speed_mph


def _session(finalized=None, **options):
    def finalize(shot):
        if finalized is not None:
            finalized.append((shot.demo, shot.shot_number, shot.profile_id, shot.timestamp))

    return DemoSession(rng=Random(1), finalize=finalize, **options)


def _start(session, bag_ids=None, **overrides):
    return session.start(now_s=T0, wall=WALL, bag_ids=bag_ids, **{**PROFILE, **overrides})


def _due(session, after_s, **overrides):
    return session.due_shot(
        now_s=T0 + after_s, wall=WALL + timedelta(seconds=after_s), **{**PROFILE, **overrides}
    )


def test_the_constants_are_the_specified_ones():
    assert (DEMO_STARTER_COUNT, DEMO_STARTER_SPACING_S) == (6, 10.0)
    assert (DEMO_INTERVAL_S, DEMO_IDLE_TIMEOUT_S) == (20.0, 900.0)


def test_demo_clubs_spread_from_slowest_to_fastest():
    clubs = demo_clubs(None)

    assert clubs == [
        ClubType.LW,
        ClubType.IRON_9,
        ClubType.HYBRID_9,
        ClubType.IRON_4,
        ClubType.WOOD_7,
        ClubType.DRIVER,
    ]
    speeds = [_speed(club) for club in clubs]
    assert speeds == sorted(set(speeds))


def test_a_long_bag_keeps_the_slowest_and_the_fastest_club():
    bag = [
        "driver",
        "3-wood",
        "5-wood",
        "3-hybrid",
        "4-iron",
        "5-iron",
        "6-iron",
        "7-iron",
        "8-iron",
        "9-iron",
        "pw",
        "gw",
        "sw",
        "lw",
    ]

    clubs = demo_clubs(bag)

    assert (clubs[0], clubs[-1]) == (ClubType.LW, ClubType.DRIVER)
    assert len(set(clubs)) == DEMO_STARTER_COUNT
    speeds = [_speed(club) for club in clubs]
    assert speeds == sorted(set(speeds))


def test_a_short_bag_keeps_every_club():
    assert demo_clubs(["7-iron", "driver"]) == [ClubType.IRON_7, ClubType.DRIVER]
    assert demo_clubs(["driver", "7-iron"]) == [ClubType.IRON_7, ClubType.DRIVER]


def test_a_bag_of_exactly_six_clubs_is_used_whole():
    bag = ["driver", "3-wood", "7-iron", "9-iron", "pw", "lw"]

    assert demo_clubs(bag) == [
        ClubType.LW,
        ClubType.PW,
        ClubType.IRON_9,
        ClubType.IRON_7,
        ClubType.WOOD_3,
        ClubType.DRIVER,
    ]


def test_bag_variants_map_to_their_category_once():
    assert demo_clubs(["wedge-44", "mini-driver"]) == [ClubType.PW, ClubType.DRIVER]
    assert demo_clubs(["driver", "mini-driver", "7-iron"]) == [ClubType.IRON_7, ClubType.DRIVER]


@pytest.mark.parametrize("bag", [None, [], ["nope"], ["nope", "unknown", ""]])
def test_unknown_ids_fall_back_to_every_club(bag):
    assert demo_clubs(bag) == demo_clubs(None)
    assert ClubType.UNKNOWN not in demo_clubs(bag)


def test_unknown_ids_in_a_real_bag_are_skipped():
    assert demo_clubs(["nope", "pw", "unknown", "driver"]) == [ClubType.PW, ClubType.DRIVER]


def test_start_gives_six_starter_shots_10_s_apart():
    finalized = []
    session = _session(finalized)

    shots = _start(session)

    assert len(shots) == 6
    assert len(finalized) == 6
    assert [shot.timestamp for shot in shots] == [
        WALL - timedelta(seconds=s) for s in range(50, -1, -10)
    ]
    assert [shot.club for shot in shots] == demo_clubs(None)
    assert all(shot.demo and shot.mode == "demo" for shot in shots)
    assert {(shot.profile_id, shot.profile_name) for shot in shots} == {("p1", "Golfer")}
    assert [shot.shot_number for shot in shots] == [1, 2, 3, 4, 5, 6]
    assert session.active
    assert session.shots() == shots


def test_every_shot_is_stamped_before_it_is_finalized():
    finalized = []
    session = _session(finalized)

    starters = _start(session)
    due = _due(session, 20)

    assert finalized == [
        (True, shot.shot_number, "p1", shot.timestamp) for shot in [*starters, due]
    ]


def test_a_short_bag_repeats_in_the_starter_set():
    session = _session()

    shots = _start(session, bag_ids=["pw", "driver"])

    assert [shot.club for shot in shots] == [ClubType.PW, ClubType.DRIVER] * 3


def test_switching_on_twice_adds_nothing():
    finalized = []
    session = _session(finalized)
    _start(session)

    assert _start(session) == []

    assert len(session.shots()) == 6
    assert len(finalized) == 6


def test_two_clients_switching_on_at_once_start_one_demo():
    finalized = []

    def slow_finalize(shot):
        finalized.append(shot)
        time.sleep(0.002)  # long enough for the other starts to run if nothing held them back

    session = DemoSession(rng=Random(1), finalize=slow_finalize)
    gate = threading.Barrier(8)
    results = []

    def switch_on():
        gate.wait()
        results.append(_start(session))

    threads = [threading.Thread(target=switch_on) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(len(result) for result in results) == [0] * 7 + [6]
    assert len(finalized) == 6
    assert len(session.shots()) == 6


def test_a_failing_finalize_leaves_the_demo_off():
    def broken(_shot):
        raise RuntimeError("no ballistics")

    session = DemoSession(rng=Random(1), finalize=broken)

    with pytest.raises(RuntimeError):
        _start(session)

    assert not session.active
    assert session.shots() == []
    assert session.stop() is False


def test_switching_off_twice_is_harmless():
    session = _session()
    _start(session)

    assert session.stop() is True
    assert session.stop() is False

    assert not session.active
    assert session.shots() == []
    assert session.stats()["shot_count"] == 0


def test_a_new_demo_starts_fresh_after_the_old_one_ended():
    session = _session()
    _start(session)
    _due(session, 20)
    session.stop()

    shots = _start(session)

    assert [shot.shot_number for shot in shots] == [1, 2, 3, 4, 5, 6]
    assert len(session.shots()) == 6


def test_next_shot_every_interval():
    session = _session()
    starters = _start(session)

    assert _due(session, 19.9) is None
    first = _due(session, 20)
    assert _due(session, 20) is None
    assert _due(session, 39.9) is None
    second = _due(session, 40)

    assert (first.shot_number, second.shot_number) == (7, 8)
    assert (first.club, second.club) == (starters[0].club, starters[1].club)
    assert (first.timestamp, second.timestamp) == (
        WALL + timedelta(seconds=20),
        WALL + timedelta(seconds=40),
    )
    assert first.demo and first.mode == "demo"
    assert session.shots() == [*starters, first, second]


def test_a_short_bag_keeps_rotating_after_the_starter_set():
    session = _session()
    _start(session, bag_ids=["pw", "driver", "7-iron"])

    clubs = [_due(session, 20 * n).club for n in range(1, 5)]

    assert clubs == [ClubType.PW, ClubType.IRON_7, ClubType.DRIVER, ClubType.PW]


def test_a_late_tick_does_not_make_up_for_missed_shots():
    session = _session()
    _start(session)

    assert _due(session, 95) is not None
    assert _due(session, 95) is None
    assert _due(session, 114.9) is None
    assert _due(session, 115) is not None


def test_later_shots_carry_the_profile_active_then():
    session = _session()
    _start(session)

    shot = _due(session, 20, profile_id="p2", profile_name="Guest")

    assert (shot.profile_id, shot.profile_name) == ("p2", "Guest")


def test_a_failing_finalize_skips_one_shot_but_not_the_schedule():
    calls = []

    def sometimes_broken(shot):
        calls.append(shot)
        if len(calls) == 7:
            raise RuntimeError("no ballistics")

    session = DemoSession(rng=Random(1), finalize=sometimes_broken)
    _start(session)

    with pytest.raises(RuntimeError):
        _due(session, 20)

    assert len(session.shots()) == 6
    assert _due(session, 21) is None
    assert _due(session, 40) is not None


def test_nothing_is_due_before_the_demo_starts():
    session = _session()

    assert _due(session, 20) is None
    assert session.expired(T0 + 10_000) is False
    session.touch(T0)
    assert not session.active


def test_expires_after_15_minutes_without_touch():
    session = _session()
    _start(session)

    assert session.expired(T0 + 899) is False
    assert session.expired(T0 + 900) is True


def test_a_touch_extends_the_demo():
    session = _session()
    _start(session)

    session.touch(T0 + 600)

    assert session.expired(T0 + 1200) is False
    assert session.expired(T0 + 1500) is True


def test_idle_timeout_and_interval_are_adjustable():
    session = _session(interval_s=2.0, idle_timeout_s=5.0)
    _start(session)

    assert _due(session, 1.9) is None
    assert _due(session, 2) is not None
    assert session.expired(T0 + 4.9) is False
    assert session.expired(T0 + 5) is True


def test_delete_and_clear_act_on_demo_shots():
    session = _session()
    starters = _start(session)
    guest = _due(session, 20, profile_id="p2", profile_name="Guest")

    timestamp = starters[2].to_dict()["timestamp"]
    assert session.delete(timestamp) is True
    assert session.delete(timestamp) is False
    assert session.delete("2026-01-01T00:00:00") is False
    assert starters[2] not in session.shots()

    session.clear("p1")

    assert session.shots() == [guest]
    assert session.active
    assert _due(session, 40).shot_number == 8


def test_delete_and_clear_do_nothing_without_a_demo():
    session = _session()

    assert session.delete(WALL.isoformat()) is False
    session.clear("p1")
    assert session.shots() == []


def test_shots_returns_a_copy():
    session = _session()
    _start(session)

    session.shots().clear()

    assert len(session.shots()) == 6


def test_stats_summarize_the_demo_shots():
    session = _session()
    starters = _start(session)

    stats = session.stats()

    assert stats["shot_count"] == 6
    assert stats["mode"] == "demo"
    assert stats["max_ball_speed"] == max(shot.ball_speed_mph for shot in starters)


def test_the_real_metrics_give_every_starter_shot_a_carry_and_a_flight_path(monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "ball_speed_correction_enabled", False)
    monkeypatch.setattr(server, "calculated_spin_enabled", False)
    session = DemoSession(rng=Random(2026), finalize=server._finalize_shot_metrics)

    shots = _start(session)

    assert len(shots) == 6
    for shot in shots:
        assert shot.carry_spin_adjusted is not None, shot.club
        assert shot.flight_path is not None, shot.club


# --- Club-face data of the demo shots -------------------------------------------------


def _many_demo_shots(seed=7, count=400, bag_ids=None):
    """A demo's starters and then one shot a second, ``count`` in all."""
    session = DemoSession(rng=Random(seed), finalize=lambda shot: None, interval_s=1.0)
    shots = _start(session, bag_ids=bag_ids)
    for second in range(1, count - len(shots) + 1):
        shots.append(_due(session, second))
    return shots


def _face(shot):
    names = ("face_to_path_deg", "face_to_path_class", "impact_offset_mm", "impact_sigma_mm")
    return {name: getattr(shot, name) for name in (*names, "impact_zone", "impact_height_mm")}


def test_every_demo_shot_carries_face_to_path_and_an_impact_point():
    for shot in _many_demo_shots():
        payload = shot.to_dict()
        for name in ("face_to_path_deg", "face_to_path_sigma_deg", "impact_offset_mm"):
            assert isinstance(payload[name], float), (shot.club, name)
        assert payload["face_to_path_class"] in F2P_CLASSES
        assert 2.0 <= payload["impact_sigma_mm"] <= 4.5
        assert payload["impact_toe_mm"] == payload["impact_offset_mm"]
        assert payload["impact_method"] == "D"
        assert payload["impact_scale_mm_per_px"] is None


def test_demo_offsets_stay_on_the_face_and_zones_come_from_impact_zone():
    for shot in _many_demo_shots():
        limit = DEMO_IMPACT_MAX_ABS_MM["woods" if shot.club in WOOD_CATEGORIES else "irons"]
        assert -limit <= shot.impact_offset_mm <= limit, shot.club
        assert shot.impact_zone == impact_zone(
            shot.impact_offset_mm, shot.impact_sigma_mm, shot.club
        )


def test_a_few_hundred_demo_shots_show_every_zone_and_now_and_then_none():
    zones = Counter(shot.impact_zone for shot in _many_demo_shots())

    assert {"heel", "center", "toe"} <= zones.keys()
    assert 0 < zones[None] < zones["center"]


def test_only_teed_woods_and_the_driver_get_a_face_height():
    shots = _many_demo_shots()
    with_height = [shot for shot in shots if shot.impact_height_mm is not None]

    assert {shot.club for shot in with_height} <= IMPACT_HEIGHT_CLUBS
    assert all(shot.impact_height_sigma_mm is not None for shot in with_height)
    assert all(
        shot.impact_height_sigma_mm is None for shot in shots if shot.impact_height_mm is None
    )
    drivers = [shot for shot in shots if shot.club is ClubType.DRIVER]
    assert drivers and all(shot.impact_height_mm is not None for shot in drivers)
    for shot in with_height:
        _mean, _sd, low, high = DEMO_IMPACT_HEIGHT_MM[shot.club]
        assert low <= shot.impact_height_mm <= high


def test_driver_heights_sit_around_the_middle_of_the_face():
    shots = _many_demo_shots(bag_ids=["driver"])
    heights = [shot.impact_height_mm for shot in shots]

    assert None not in heights
    # The UI's driver face is 56 mm high (impactFaceGeometry.ts), y up from the lower edge.
    assert 24.0 < sum(heights) / len(heights) < 32.0


def test_face_to_path_follows_the_curve():
    shots = _many_demo_shots()
    pairs = Counter((curve_class(shot.spin_axis_deg), shot.face_to_path_class) for shot in shots)

    for shot in shots:
        curve = curve_class(shot.spin_axis_deg)
        if curve in ("draw", "hook"):
            assert shot.face_to_path_deg < 0 and shot.face_to_path_class != "open"
        if curve in ("fade", "slice"):
            assert shot.face_to_path_deg > 0 and shot.face_to_path_class != "closed"
    assert pairs[("draw", "closed")] > 0
    assert pairs[("fade", "open")] > 0


def test_face_to_path_inverts_the_dplane_axis_and_classes_on_the_real_thresholds():
    for shot in _many_demo_shots(count=100):
        expected = demo_face_to_path_deg(shot)
        loft = spin_loft_deg(
            shot.launch_angle_vertical, shot.club_angle_deg, face_weight_for_club(shot.club)
        )

        # The face to path gives back the shot's own spin axis under the D-plane model.
        assert spin_axis_deg(expected, loft) == pytest.approx(shot.spin_axis_deg, abs=1e-6)
        assert shot.face_to_path_deg == pytest.approx(expected, abs=0.051)
        # The class is classify's, apart from values within rounding of the 2-sigma line.
        if abs(abs(expected) - CLASS_Z * shot.face_to_path_sigma_deg) > 0.2:
            assert shot.face_to_path_class == classify(
                expected, shot.face_to_path_sigma_deg, F2P_CLASSES
            )


def test_a_strong_draw_reads_closed():
    shot = generate_shot(ClubType.IRON_7, Random(1), mode="demo")
    shot.spin_axis_deg = -8.0

    fields = demo_face_fields(shot, Random(1))

    assert fields["face_to_path_deg"] < 0
    assert fields["face_to_path_class"] == "closed"


def test_seeded_demos_repeat_their_face_data():
    first = [_face(shot) for shot in _many_demo_shots(seed=11, count=50)]
    second = [_face(shot) for shot in _many_demo_shots(seed=11, count=50)]
    other = [_face(shot) for shot in _many_demo_shots(seed=12, count=50)]

    assert first == second
    assert first != other
