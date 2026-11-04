"""Personal bags persist independently of historical shots and measurement categories."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openflight.clubs import ClubType
from openflight.clubs.bag import clean_club_ids, club_category
from openflight.launch_monitor import Shot
from openflight.profiles import ProfileStore
from openflight import server


EXAMPLE_BAG = ["wedge-58", "wedge-54", "wedge-48", "wedge-44", "9-iron", "8-iron",
              "7-iron", "6-iron", "5-iron", "4-iron", "7-wood", "3-wood", "mini-driver", "driver"]


def test_bag_survives_restart_and_preserves_other_profile_settings(tmp_path):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    profile = store.get_active()
    profile.settings["other"] = {"keep": True}
    assert store.set_clubs(profile.id, EXAMPLE_BAG)
    reloaded = ProfileStore(path).get_active()
    assert reloaded.settings == {"other": {"keep": True}, "club_ids": EXAMPLE_BAG}


@pytest.mark.parametrize("invalid", [[], None, "driver", ["unknown"], ["ghost"], [{}], [1], [True]])
def test_invalid_bag_does_not_modify_disk(tmp_path, invalid):
    path = tmp_path / "profiles.json"
    store = ProfileStore(path)
    original = path.read_bytes()
    assert not store.set_clubs(store.get_active().id, invalid)
    assert path.read_bytes() == original


def test_duplicate_ids_are_collapsed_and_unknown_profile_is_rejected(tmp_path):
    store = ProfileStore(tmp_path / "profiles.json")
    assert not store.set_clubs("missing", ["driver"])
    assert clean_club_ids(["driver", "wedge-58", "driver"]) == ["driver", "wedge-58"]


def test_save_failure_keeps_previous_bag(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    profile = store.get_active()
    assert store.set_clubs(profile.id, ["driver"])
    monkeypatch.setattr("openflight.profiles.os.replace", Mock(side_effect=OSError("disk full")))
    assert not store.set_clubs(profile.id, ["7-iron"])
    assert profile.settings["club_ids"] == ["driver"]
    assert ProfileStore(tmp_path / "profiles.json").get_active().settings["club_ids"] == ["driver"]


@pytest.mark.parametrize("selection,category", [("wedge-44", ClubType.PW), ("wedge-48", ClubType.GW),
    ("wedge-54", ClubType.SW), ("wedge-58", ClubType.LW), ("mini-driver", ClubType.DRIVER)])
def test_named_variant_keeps_category_but_distinct_shot_label(selection, category):
    assert club_category(selection) == category
    old = Shot(ball_speed_mph=100, timestamp=datetime.now(), club=category)
    new = Shot(ball_speed_mph=100, timestamp=datetime.now(), club=category, club_selection=selection)
    assert new.to_dict()["club"] == selection
    assert old.to_dict()["club"] == category.value
    assert new.estimated_carry_yards == old.estimated_carry_yards


@pytest.fixture
def bag_server(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / "profiles.json")
    monkeypatch.setattr(server, "profile_store", store)
    monitor = SimpleNamespace(_current_club=ClubType.DRIVER)
    def set_club(club, selection=None):
        monitor._current_club = club
        monitor._current_club_selection = selection or club.value
    monitor.set_club = set_club
    monkeypatch.setattr(server, "monitor", monitor)
    monkeypatch.setattr(server.socketio, "emit", Mock())
    return store, monitor


def test_save_switches_removed_active_club_and_broadcasts(bag_server):
    store, monitor = bag_server
    result = server.handle_set_profile_clubs({"profile_id": store.get_active().id, "club_ids": ["wedge-58"]})
    assert result == {"ok": True}
    assert monitor._current_club == ClubType.LW
    assert server._current_club_id() == "wedge-58"
    server.socketio.emit.assert_any_call("club_changed", {"club": "wedge-58"})
    server.socketio.emit.assert_any_call("profiles", server._profiles_payload())


def test_invalid_save_and_payloads_do_not_change_selection(bag_server):
    assert server.handle_set_profile_clubs(None) == {"ok": False}
    for payload in [None, {"club": {}}, {"club": "invalid"}]:
        server.handle_set_club(payload)
    assert server._current_club_id() == "driver"


def test_variant_selection_and_sim_category_reset(bag_server):
    _, monitor = bag_server
    server.handle_set_club({"club": "mini-driver"})
    assert monitor._current_club == ClubType.DRIVER
    assert server._current_club_id() == "mini-driver"
    monitor.set_club(ClubType.DRIVER)
    assert server._current_club_id() == "driver"


def test_switch_profile_reconciles_its_bag(bag_server):
    store, _ = bag_server
    first = store.get_active().id
    store.set_clubs(first, ["7-iron"])
    second = store.add("Guest")
    store.set_clubs(second.id, ["driver"])
    server.handle_set_active_profile({"profile_id": first})
    assert server._current_club_id() == "7-iron"
    server.handle_set_active_profile({"profile_id": second.id})
    assert server._current_club_id() == "driver"


@pytest.mark.parametrize("units", ["imperial", "metric", "mph-meters", "kmh-yards"])
def test_units_persist_with_bag_and_broadcast_to_other_clients(bag_server, units):
    store, _ = bag_server
    profile_id = store.get_active().id
    assert store.set_clubs(profile_id, EXAMPLE_BAG)
    assert server.handle_set_profile_units({"profile_id": profile_id, "unit_system": units}) == {"ok": True}
    assert store.get_active().settings["club_ids"] == EXAMPLE_BAG
    assert ProfileStore(store._path).get_active().settings["unit_system"] == units
    server.socketio.emit.assert_any_call("profiles", server._profiles_payload())


@pytest.mark.parametrize("invalid", [None, {}, [], "mph", 1, True])
def test_invalid_units_are_not_saved(bag_server, invalid):
    store, _ = bag_server
    assert not store.set_units(store.get_active().id, invalid)
    assert "unit_system" not in store.get_active().settings


def test_real_monitor_category_reset_does_not_relabel_an_existing_shot():
    from openflight.rolling_buffer.monitor import RollingBufferMonitor
    monitor = RollingBufferMonitor(port=None, trigger_type="sound")
    monitor.set_club(ClubType.DRIVER, selection="mini-driver")
    shot = Shot(ball_speed_mph=100, timestamp=datetime.now(), club=monitor._current_club,
                club_selection=monitor._current_club_selection)
    monitor.set_club(ClubType.IRON_7)
    assert monitor._current_club_selection == "7-iron"
    assert shot.to_dict()["club"] == "mini-driver"
