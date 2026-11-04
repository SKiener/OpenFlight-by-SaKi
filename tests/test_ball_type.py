"""Ball type per profile: "standard" or "range" picks the club-typical carry spin table.

A profile without a ball type flies the launcher's --typical-spin-table, so nothing
changes until a profile chooses. Measured spin, when trusted, still wins over any table.
"""

import json
from datetime import datetime
from unittest.mock import Mock

import pytest

from openflight import server
from openflight.ballistics import (
    CLUB_TYPICAL_SPIN_RPM,
    RANGE_BALL_SPIN_RPM,
    TYPICAL_SPIN_TABLES,
    resolve_launch,
    simulate,
)
from openflight.clubs import ClubType
from openflight.launch_monitor import SPIN_CONFIDENCE_HIGH, Shot
from openflight.profiles import (
    BALL_TYPE_SPIN_TABLES,
    DEFAULT_BALL_TYPE,
    ProfileStore,
    ball_type,
    ball_type_for_spin_table,
    clean_ball_type,
)
from openflight.session_logger import SessionLogger

pytestmark = pytest.mark.usefixtures("isolate_finalization")

INVALID_BALL_TYPES = ["Range", "tour", "range-ball", "", " range", None, True, 1, [], {}]


# -- profiles ----------------------------------------------------------------


def test_ball_types_map_to_the_two_spin_tables():
    assert BALL_TYPE_SPIN_TABLES == {"standard": "tour", "range": "range-ball"}
    assert set(BALL_TYPE_SPIN_TABLES.values()) == set(TYPICAL_SPIN_TABLES)
    assert DEFAULT_BALL_TYPE == "standard"


@pytest.mark.parametrize("value", ["standard", "range"])
def test_clean_keeps_known_ball_types(value):
    assert clean_ball_type(value) == value


@pytest.mark.parametrize("value", INVALID_BALL_TYPES)
def test_clean_rejects_everything_else(value):
    assert clean_ball_type(value) is None


@pytest.mark.parametrize(
    ("table", "expected"), [("tour", "standard"), ("range-ball", "range"), ("pro-v1", "standard")]
)
def test_launcher_table_maps_back_to_its_ball_type(table, expected):
    assert ball_type_for_spin_table(table) == expected


def test_unset_or_invalid_ball_type_reads_as_the_default():
    assert ball_type({}) == "standard"
    assert ball_type(None, "range") == "range"
    assert ball_type({"ball_type": "pro-v1"}, "range") == "range"
    assert ball_type({"ball_type": "standard"}, "range") == "standard"
    assert ball_type({"ball_type": "range"}) == "range"


@pytest.mark.parametrize("value", ["standard", "range"])
def test_setter_persists_and_keeps_other_settings(tmp_path, value):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    profile = store.get_active()
    assert store.set_driver_tee_height(profile.id, 0.05)
    assert store.set_ball_type(profile.id, value)
    assert ProfileStore(path).get_active().settings == {
        "driver_tee_height_m": 0.05,
        "ball_type": value,
    }


@pytest.mark.parametrize("value", INVALID_BALL_TYPES)
def test_setter_rejects_invalid_values_without_writing(tmp_path, value):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    original = path.read_bytes()
    assert not store.set_ball_type(store.get_active().id, value)
    assert path.read_bytes() == original
    assert "ball_type" not in store.get_active().settings


def test_setter_rejects_an_unknown_profile(tmp_path):
    assert not ProfileStore(tmp_path / "profiles.json").set_ball_type("ghost", "range")


def test_a_new_profile_stores_no_ball_type(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    assert "ball_type" not in store.add("Guest").settings


# -- server: socket and roster ------------------------------------------------


@pytest.fixture
def ball_server(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(server, "profile_store", store)
    monkeypatch.setattr(server.socketio, "emit", Mock())
    monkeypatch.setattr(server, "ballistics_enabled", True)
    monkeypatch.setattr(server, "typical_spin_scale", 1.0)
    monkeypatch.setattr(server, "typical_spin_table", "tour")
    return store


def _emitted_rosters():
    return [
        call.args[1] for call in server.socketio.emit.call_args_list if call.args[0] == "profiles"
    ]


def test_socket_handler_saves_and_emits_the_roster(ball_server):
    profile_id = ball_server.get_active().id
    assert server.handle_set_profile_ball_type(
        {"profile_id": profile_id, "ball_type": "range"}
    ) == {"ok": True}
    assert ProfileStore(ball_server._path).get_active().settings["ball_type"] == "range"
    roster = _emitted_rosters()[-1]
    assert roster["active_profile_id"] == profile_id
    assert roster["profiles"][0]["ball_type"] == "range"
    assert roster["profiles"][0]["settings"] == {"ball_type": "range"}


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"ball_type": "range"}, {"profile_id": "ghost", "ball_type": "range"}, "range"],
)
def test_socket_handler_rejects_and_still_emits(ball_server, payload):
    assert server.handle_set_profile_ball_type(payload) == {"ok": False}
    assert _emitted_rosters()[-1] == server._profiles_payload()


@pytest.mark.parametrize("value", INVALID_BALL_TYPES)
def test_socket_handler_rejects_invalid_ball_types(ball_server, value):
    profile_id = ball_server.get_active().id
    assert server.handle_set_profile_ball_type({"profile_id": profile_id, "ball_type": value}) == {
        "ok": False
    }
    assert "ball_type" not in ball_server.get_active().settings
    assert len(_emitted_rosters()) == 1


@pytest.mark.parametrize(("launcher", "effective"), [("tour", "standard"), ("range-ball", "range")])
def test_roster_shows_the_launcher_default_for_an_unset_profile(
    ball_server, monkeypatch, launcher, effective
):
    monkeypatch.setattr(server, "typical_spin_table", launcher)
    chosen = ball_server.add("Guest")
    ball_server.set_ball_type(chosen.id, "standard" if effective == "range" else "range")

    first, second = server._profiles_payload()["profiles"]

    assert first["ball_type"] == effective
    assert "ball_type" not in first["settings"]
    assert second["ball_type"] != effective


def test_roster_effective_value_never_reaches_the_disk(ball_server):
    server._emit_profiles()
    stored = json.loads(ball_server._path.read_text())
    assert all("ball_type" not in profile for profile in stored["profiles"])
    assert ball_server.snapshot()["profiles"][0].keys() == {"id", "name", "created_at", "settings"}


# -- server: the carry --------------------------------------------------------


def _typical_shot(club=ClubType.IRON_8, profile_id=""):
    # 0.59 is the OPS cap: never trusted, so carry runs on the club-typical spin.
    shot = Shot(
        ball_speed_mph=103.4,
        timestamp=datetime.now(),
        club=club,
        launch_angle_vertical=22.0,
        launch_angle_confidence=0.95,
        spin_rpm=4669.0,
        spin_confidence=0.59,
    )
    shot.profile_id = profile_id
    return shot


def _measured_shot(profile_id=""):
    shot = _typical_shot(ClubType.IRON_7, profile_id)
    shot.spin_confidence = SPIN_CONFIDENCE_HIGH
    shot.spin_source = "measured"
    return shot


def _finalized(shot):
    server._finalize_shot_detected(shot, emit_event="shot")
    return shot


@pytest.mark.parametrize(("launcher", "table"), [("tour", "tour"), ("range-ball", "range-ball")])
def test_unset_profile_flies_the_launcher_table(ball_server, monkeypatch, launcher, table):
    monkeypatch.setattr(server, "typical_spin_table", launcher)
    shot = _finalized(_typical_shot(profile_id=ball_server.get_active().id))

    assert shot.typical_spin_table == table
    assert shot.carry_spin_rpm == TYPICAL_SPIN_TABLES[table][ClubType.IRON_8]
    assert (
        shot.carry_spin_adjusted
        == simulate(
            resolve_launch(_typical_shot(), typical_spin_table=table),
            air_density=server.air_density_kg_m3,
        ).carry_yards
    )


@pytest.mark.parametrize("launcher", ["tour", "range-ball"])
def test_range_profile_flies_the_range_ball_table_whatever_the_launcher(
    ball_server, monkeypatch, launcher
):
    monkeypatch.setattr(server, "typical_spin_table", launcher)
    ball_server.set_ball_type(ball_server.get_active().id, "range")

    shot = _finalized(_typical_shot(profile_id=ball_server.get_active().id))

    assert shot.typical_spin_table == "range-ball"
    assert shot.carry_spin_rpm == RANGE_BALL_SPIN_RPM[ClubType.IRON_8] == 4260
    assert shot.carry_spin_source == "club_typical"


@pytest.mark.parametrize("launcher", ["tour", "range-ball"])
def test_standard_profile_flies_the_tour_table_whatever_the_launcher(
    ball_server, monkeypatch, launcher
):
    monkeypatch.setattr(server, "typical_spin_table", launcher)
    ball_server.set_ball_type(ball_server.get_active().id, "standard")

    shot = _finalized(_typical_shot(profile_id=ball_server.get_active().id))

    assert shot.typical_spin_table == "tour"
    assert shot.carry_spin_rpm == CLUB_TYPICAL_SPIN_RPM[ClubType.IRON_8] == 7500


def test_shot_profile_wins_over_the_active_one(ball_server):
    first = ball_server.get_active().id
    ball_server.set_ball_type(first, "range")
    second = ball_server.add("Guest")
    ball_server.set_ball_type(second.id, "standard")
    assert ball_server.get_active().id == second.id

    assert server._shot_typical_spin_table(_typical_shot(profile_id=first)) == "range-ball"
    assert server._shot_typical_spin_table(_typical_shot(profile_id=second.id)) == "tour"


@pytest.mark.parametrize("profile_id", ["", "deleted"])
def test_shot_without_a_known_profile_uses_the_active_one(ball_server, profile_id):
    ball_server.set_ball_type(ball_server.get_active().id, "range")
    assert server._shot_typical_spin_table(_typical_shot(profile_id=profile_id)) == "range-ball"


def test_invalid_stored_ball_type_flies_the_launcher_table(ball_server, monkeypatch):
    monkeypatch.setattr(server, "typical_spin_table", "range-ball")
    ball_server.get_active().settings["ball_type"] = "pro-v1"
    assert server._shot_typical_spin_table(_typical_shot()) == "range-ball"
    assert server._profiles_payload()["profiles"][0]["ball_type"] == "range"


@pytest.mark.parametrize("value", ["standard", "range"])
def test_trusted_measured_spin_wins_over_the_ball_type(ball_server, value):
    profile_id = ball_server.get_active().id
    ball_server.set_ball_type(profile_id, value)

    shot = _finalized(_measured_shot(profile_id))

    assert shot.carry_spin_source == "measured"
    assert shot.carry_spin_rpm == 4669.0
    assert (
        shot.carry_spin_adjusted
        == simulate(
            resolve_launch(_measured_shot()), air_density=server.air_density_kg_m3
        ).carry_yards
    )
    # No table was flown, so the shot names none.
    assert shot.typical_spin_table is None
    assert shot.to_dict()["typical_spin_table"] is None


def test_measured_spin_reflight_keeps_naming_no_table(ball_server):
    profile_id = ball_server.get_active().id
    shot = _finalized(_measured_shot(profile_id))
    carry = shot.carry_spin_adjusted
    ball_server.set_ball_type(profile_id, "range")

    server._fly_shot(shot)

    assert shot.typical_spin_table is None
    assert (shot.carry_spin_source, shot.carry_spin_adjusted) == ("measured", carry)


def test_calculated_spin_names_no_table(ball_server):
    shot = _measured_shot(ball_server.get_active().id)
    shot.spin_source = "calculated"
    shot = _finalized(shot)

    assert shot.carry_spin_source == "calculated"
    assert shot.typical_spin_table is None


def test_profile_switch_takes_effect_for_the_next_shot(ball_server):
    profile_id = ball_server.get_active().id
    before = _finalized(_typical_shot(profile_id=profile_id))
    assert server.handle_set_profile_ball_type({"profile_id": profile_id, "ball_type": "range"})[
        "ok"
    ]
    after = _finalized(_typical_shot(profile_id=profile_id))

    assert (before.typical_spin_table, before.carry_spin_rpm) == ("tour", 7500)
    assert (after.typical_spin_table, after.carry_spin_rpm) == ("range-ball", 4260)

    # A re-flight of the earlier shot (a late spin axis) keeps the table it first flew.
    carry = before.carry_spin_adjusted
    server._fly_shot(before)
    assert (before.typical_spin_table, before.carry_spin_rpm) == ("tour", 7500)
    assert before.carry_spin_adjusted == carry


def test_standard_air_carry_flies_the_shot_profile_table(ball_server):
    ball_server.set_ball_type(ball_server.get_active().id, "range")
    shot = _typical_shot(ClubType.PW, ball_server.get_active().id)
    shot.carry_model = "ballistic"

    server._ensure_standard_air_carry(shot)

    assert (
        shot.carry_standard_air_yards
        == simulate(
            resolve_launch(_typical_shot(ClubType.PW), typical_spin_table="range-ball"),
            air_density=server.air_density_kg_m3,
        ).carry_yards
    )


def test_standard_air_carry_keeps_the_table_the_shot_flew(ball_server):
    ball_server.set_ball_type(ball_server.get_active().id, "range")
    shot = _typical_shot(ClubType.PW, ball_server.get_active().id)
    shot.carry_model = "ballistic"
    shot.typical_spin_table = "tour"

    server._ensure_standard_air_carry(shot)

    assert (
        shot.carry_standard_air_yards
        == simulate(
            resolve_launch(_typical_shot(ClubType.PW)), air_density=server.air_density_kg_m3
        ).carry_yards
    )


def test_simulator_late_export_carry_uses_the_profile_table(ball_server, monkeypatch):
    """A simulator connecting after the shot gets the export carry of the shot's table."""
    ball_server.set_ball_type(ball_server.get_active().id, "range")
    monkeypatch.setattr(server, "_air_density_for_shot", lambda: _Density(1.10))
    shot = _finalized(_typical_shot(profile_id=ball_server.get_active().id))
    assert shot.carry_standard_air_yards is None

    server._ensure_standard_air_carry(shot)

    assert (
        shot.carry_standard_air_yards
        == simulate(
            resolve_launch(_typical_shot(), typical_spin_table="range-ball"),
            air_density=server.air_density_kg_m3,
        ).carry_yards
    )


class _Density:
    def __init__(self, kg_m3):
        self.kg_m3 = kg_m3
        self.source = "manual"
        self.weather_age_s = None


def test_table_carry_has_no_spin_table(ball_server, monkeypatch):
    monkeypatch.setattr(server, "ballistics_enabled", False)
    shot = _finalized(_typical_shot(profile_id=ball_server.get_active().id))
    assert shot.carry_model != "ballistic"
    assert shot.typical_spin_table is None
    assert shot.to_dict()["typical_spin_table"] is None


def test_no_launch_angle_has_no_spin_table(ball_server):
    shot = _typical_shot(profile_id=ball_server.get_active().id)
    shot.launch_angle_vertical = None
    server._fly_shot(shot)
    assert shot.typical_spin_table is None


# -- session log ----------------------------------------------------------------


def test_shot_detected_logs_the_table_and_session_start_keeps_the_launcher_default(
    ball_server, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "typical_spin_table", "tour")
    logger = SessionLogger(log_dir=tmp_path / "logs", enabled=True)
    logger.start_session(mode="mock", trigger_type="sound", config=server._session_start_config())
    monkeypatch.setattr(server, "get_session_logger", lambda: logger)
    profile_id = ball_server.get_active().id

    _finalized(_typical_shot(profile_id=profile_id))
    ball_server.set_ball_type(profile_id, "range")
    _finalized(_typical_shot(profile_id=profile_id))

    entries = [json.loads(line) for line in logger.session_path.read_text().splitlines()]
    start = next(entry for entry in entries if entry["type"] == "session_start")
    detected = [entry for entry in entries if entry["type"] == "shot_detected"]
    assert start["config"]["typical_spin_table"] == "tour"
    assert [entry["typical_spin_table"] for entry in detected] == ["tour", "range-ball"]
    assert [entry["carry_spin_rpm"] for entry in detected] == [7500, 4260]


def test_shot_detected_logs_no_table_for_a_measured_spin(ball_server, monkeypatch, tmp_path):
    logger = SessionLogger(log_dir=tmp_path / "logs", enabled=True)
    logger.start_session(mode="mock", trigger_type="sound", config=server._session_start_config())
    monkeypatch.setattr(server, "get_session_logger", lambda: logger)

    _finalized(_measured_shot(ball_server.get_active().id))

    entries = [json.loads(line) for line in logger.session_path.read_text().splitlines()]
    detected = [entry for entry in entries if entry["type"] == "shot_detected"]
    assert [entry["carry_spin_source"] for entry in detected] == ["measured"]
    assert [entry["typical_spin_table"] for entry in detected] == [None]


# -- equivalence with the old launcher flag ----------------------------------------


@pytest.mark.parametrize("club", [ClubType.DRIVER, ClubType.WOOD_3, ClubType.IRON_7, ClubType.PW])
@pytest.mark.parametrize(("value", "table"), [("range", "range-ball"), ("standard", "tour")])
def test_profile_ball_type_flies_the_old_launcher_flag_carry(
    ball_server, monkeypatch, club, value, table
):
    ball_server.set_ball_type(ball_server.get_active().id, value)
    via_profile = _finalized(_typical_shot(club, ball_server.get_active().id))

    monkeypatch.setattr(
        server, "profile_store", ProfileStore(ball_server._path.with_name("u.json"))
    )
    monkeypatch.setattr(server, "typical_spin_table", table)
    via_flag = _finalized(_typical_shot(club))

    for name in ("carry_spin_adjusted", "carry_spin_rpm", "apex_yards", "carry_lateral_yards"):
        assert getattr(via_profile, name) == getattr(via_flag, name), name
    assert via_profile.flight_path == via_flag.flight_path
