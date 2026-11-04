"""Spoken shot callouts: one server-held setting every screen follows (voice_callouts.py)."""

import json
import re
from pathlib import Path

import pytest

from openflight import server as server_module
from openflight.pairing import DeviceSecret, PairingCodes
from openflight.voice_callouts import (
    CALLOUT_LANGUAGES,
    CALLOUT_METRICS,
    DEFAULT_CALLOUT_METRICS,
    VOICE_CALLOUTS_PATH_ENV,
    VoiceCalloutStore,
    resolve_voice_callouts_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHOT_CALLOUT_TS = REPO_ROOT / "ui" / "src" / "services" / "shotCallout.ts"
DEFAULTS = {"enabled": False, "metrics": ["carry", "ball_speed"], "language": None}


@pytest.fixture(name="path")
def fixture_path(tmp_path):
    return tmp_path / "voice_callouts.json"


class TestStore:
    def test_starts_off_with_carry_and_ball_speed_and_no_language(self, path):
        assert VoiceCalloutStore(path).snapshot() == DEFAULTS
        assert not path.exists(), "reading the defaults writes nothing"

    def test_partial_changes_merge_and_persist(self, path):
        store = VoiceCalloutStore(path)

        assert store.update({"enabled": True}) == {**DEFAULTS, "enabled": True}
        assert store.update({"language": "es"})["language"] == "es"
        assert store.update({"metrics": ["spin", "carry"]}) == {
            "enabled": True,
            "metrics": ["carry", "spin"],
            "language": "es",
        }

        assert json.loads(path.read_text()) == {
            "enabled": True,
            "metrics": ["carry", "spin"],
            "language": "es",
        }
        assert VoiceCalloutStore(path).snapshot() == store.snapshot()

    def test_metrics_keep_the_spoken_order_without_duplicates_or_unknown_ids(self, path):
        store = VoiceCalloutStore(path)
        settings = store.update({"metrics": ["spin", "warp", "smash", "spin", 7]})
        assert settings["metrics"] == ["smash", "spin"]

    @pytest.mark.parametrize(
        "change",
        [
            None,
            "on",
            [],
            {},
            {"volume": 3},
            {"enabled": "yes"},
            {"enabled": 1},
            {"metrics": []},
            {"metrics": ["warp"]},
            {"metrics": "carry"},
            {"language": "fr"},
            {"language": None},
            {"enabled": True, "language": "it"},
        ],
    )
    def test_invalid_changes_are_refused_whole(self, path, change):
        store = VoiceCalloutStore(path)
        assert store.update(change) is None
        assert store.snapshot() == DEFAULTS
        assert not path.exists()

    def test_a_failed_save_changes_nothing(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("")
        store = VoiceCalloutStore(blocker / "voice_callouts.json")

        assert store.update({"enabled": True}) is None
        assert store.snapshot() == DEFAULTS

    @pytest.mark.parametrize(
        "content, expected",
        [
            ("{oops", DEFAULTS),
            ("null", DEFAULTS),
            ('"on"', DEFAULTS),
            (
                json.dumps({"enabled": "yes", "metrics": ["spin", "warp"], "language": "it"}),
                {**DEFAULTS, "metrics": ["spin"]},
            ),
            (json.dumps({"enabled": True, "metrics": []}), {**DEFAULTS, "enabled": True}),
            (json.dumps({"language": "de"}), {**DEFAULTS, "language": "de"}),
        ],
    )
    def test_a_damaged_file_falls_back_field_by_field(self, path, content, expected):
        path.write_text(content)
        assert VoiceCalloutStore(path).snapshot() == expected

    def test_path_comes_from_the_argument_then_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv(VOICE_CALLOUTS_PATH_ENV, str(tmp_path / "env.json"))
        assert resolve_voice_callouts_path(tmp_path / "arg.json") == tmp_path / "arg.json"
        assert resolve_voice_callouts_path(None) == tmp_path / "env.json"
        monkeypatch.delenv(VOICE_CALLOUTS_PATH_ENV)
        assert resolve_voice_callouts_path("  ").name == "voice_callouts.json"

    def test_ids_match_the_ui(self):
        source = SHOT_CALLOUT_TS.read_text(encoding="utf-8")
        metrics = re.search(r"export const CALLOUT_METRICS = \[([^\]]*)\]", source).group(1)
        defaults = re.search(r"DEFAULT_CALLOUT_METRICS[^=]*= \[([^\]]*)\]", source).group(1)
        languages = re.findall(r"\{ id: '(\w+)', nativeName:", source)

        assert tuple(re.findall(r"'(\w+)'", metrics)) == CALLOUT_METRICS
        assert tuple(re.findall(r"'(\w+)'", defaults)) == DEFAULT_CALLOUT_METRICS
        assert tuple(languages) == CALLOUT_LANGUAGES


@pytest.fixture(name="socket_client")
def fixture_socket_client(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server_module, "voice_callout_store", VoiceCalloutStore(tmp_path / "voice_callouts.json")
    )
    monkeypatch.setattr(server_module, "device_secret", DeviceSecret(tmp_path / "secret"))
    monkeypatch.setattr(server_module, "pairing_codes", PairingCodes())
    monkeypatch.setattr(server_module, "_paired_socket_sids", set())
    monkeypatch.setattr(server_module, "monitor", None)
    sio = server_module.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(sio, name, getattr(sio, name))
    monkeypatch.setattr(sio.eio, "async_handlers", sio.eio.async_handlers)
    clients = []

    def connect(http_client=None):
        client = server_module.socketio.test_client(
            server_module.app, flask_test_client=http_client or server_module.app.test_client()
        )
        clients.append(client)
        return client

    yield connect
    for client in clients:
        if client.is_connected():
            client.disconnect()


def paired_phone():
    code = server_module.app.test_client().post("/api/pairing/code").get_json()["code"]
    phone = server_module.app.test_client()
    phone.environ_base["REMOTE_ADDR"] = "192.168.1.50"
    assert phone.post("/api/pairing/exchange", json={"code": code}).status_code == 200
    return phone


def received(client, name):
    return [event["args"][0] for event in client.get_received() if event["name"] == name]


class TestSocket:
    def test_connect_sends_the_setting(self, socket_client):
        kiosk = socket_client()
        assert received(kiosk, "voice_callouts")[-1] == DEFAULTS

    def test_get_answers_with_the_setting(self, socket_client):
        kiosk = socket_client()
        kiosk.get_received()
        kiosk.emit("get_voice_callouts")
        assert received(kiosk, "voice_callouts") == [DEFAULTS]

    def test_kiosk_switches_on_and_the_phone_follows(self, socket_client):
        phone = socket_client(paired_phone())
        kiosk = socket_client()
        phone.get_received()

        assert kiosk.emit("set_voice_callouts", {"enabled": True}, callback=True) == {"ok": True}

        assert received(phone, "voice_callouts") == [{**DEFAULTS, "enabled": True}]
        assert received(kiosk, "voice_callouts")[-1]["enabled"] is True
        assert server_module.get_voice_callout_store().snapshot()["enabled"] is True

    def test_phone_changes_reach_the_kiosk(self, socket_client):
        phone = socket_client(paired_phone())
        kiosk = socket_client()
        kiosk.get_received()

        assert phone.emit(
            "set_voice_callouts", {"metrics": ["spin"], "language": "de"}, callback=True
        ) == {"ok": True}
        assert received(kiosk, "voice_callouts") == [
            {"enabled": False, "metrics": ["spin"], "language": "de"}
        ]

    def test_refused_change_resends_the_current_setting(self, socket_client):
        kiosk = socket_client()
        kiosk.get_received()

        assert kiosk.emit("set_voice_callouts", {"metrics": []}, callback=True) == {
            "ok": False,
            "error": "invalid_voice_callouts",
        }
        assert received(kiosk, "voice_callouts") == [DEFAULTS]

    def test_the_setting_survives_a_restart(self, socket_client, tmp_path):
        kiosk = socket_client()
        kiosk.emit("set_voice_callouts", {"enabled": True, "language": "en"}, callback=True)

        reloaded = VoiceCalloutStore(tmp_path / "voice_callouts.json")
        assert reloaded.snapshot() == {**DEFAULTS, "enabled": True, "language": "en"}

    def test_voice_test_reaches_every_screen(self, socket_client):
        phone = socket_client(paired_phone())
        kiosk = socket_client()
        phone.get_received()

        assert kiosk.emit("voice_test", callback=True) == {"ok": True}

        assert received(phone, "voice_test") == [{}]
        assert received(kiosk, "voice_test") == [{}]

    def test_an_unpaired_device_cannot_connect_to_change_it(self, socket_client):
        stranger = server_module.app.test_client()
        stranger.environ_base["REMOTE_ADDR"] = "192.168.1.77"
        client = socket_client(stranger)
        assert not client.is_connected()
