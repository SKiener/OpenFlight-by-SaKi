"""Server enforcement of device pairing: HTTP guard, pairing API, and Socket.IO."""

import logging
import queue
from types import SimpleNamespace

import pytest

from openflight import pairing, server as server_module
from openflight.pairing import (
    CLIENT_FAILURE_LIMIT,
    CODE_FAILURE_LIMIT,
    DEVICE_COOKIE_MAX_AGE_S,
    DEVICE_COOKIE_NAME,
    DeviceSecret,
    PairingCodes,
    wifi_join_qr_svg,
)

PHONE_ADDR = "192.168.1.50"


@pytest.fixture(autouse=True)
def fresh_pairing_state(tmp_path, monkeypatch):
    """Each test starts with no paired devices, no code, and its own secret file."""
    monkeypatch.setattr(server_module, "device_secret", DeviceSecret(tmp_path / "pairing-secret"))
    monkeypatch.setattr(server_module, "pairing_codes", PairingCodes())
    monkeypatch.setattr(server_module, "_paired_socket_sids", set())
    monkeypatch.setattr(pairing, "pairing_hosts", lambda: ["openflight.local", "192.168.1.23"])
    monkeypatch.setattr(
        server_module, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path)
    )
    monkeypatch.setattr(server_module, "camera_replay_manager", None)


@pytest.fixture(name="shutdowns")
def fixture_shutdowns(monkeypatch):
    """Shutdown requests the route handed to its background thread."""
    calls = queue.Queue()
    monkeypatch.setattr(server_module, "_shutdown_process_after_delay", lambda: calls.put(True))
    return calls


def wait_for_shutdowns(calls, expected):
    for _ in range(expected):
        calls.get(timeout=2)
    assert calls.empty()


@pytest.fixture(name="emitted")
def fixture_emitted(monkeypatch):
    events = []
    monkeypatch.setattr(server_module.socketio, "emit", lambda *args, **kwargs: events.append(args))
    return events


def kiosk():
    return server_module.app.test_client()


def remote(addr=PHONE_ADDR):
    client = server_module.app.test_client()
    client.environ_base["REMOTE_ADDR"] = addr
    return client


def issue_code():
    response = kiosk().post("/api/pairing/code")
    assert response.status_code == 200
    return response.get_json()["code"]


def paired_phone(addr=PHONE_ADDR):
    phone = remote(addr)
    response = phone.post("/api/pairing/exchange", json={"code": issue_code()})
    assert response.status_code == 200
    return phone


PUBLIC_PAGES = ["/", "/display", "/companion", "/pair", "/pair/", "/?view=analysis"]
PROTECTED = [
    ("GET", "/api/session-history"),
    ("GET", "/api/session-history/session_1"),
    ("POST", "/api/shutdown"),
    ("GET", "/api/camera/preview.jpg"),
    ("GET", "/api/camera/exposure-quality"),
    ("POST", "/api/camera/replays/r1/prepare"),
    ("GET", "/api/camera/replays/r1/video"),
]
LOCAL_ONLY = ["/api/pairing/code", "/api/pairing/revoke"]


class TestHttpGuard:
    """Who may use which route."""

    @pytest.mark.parametrize("path", PUBLIC_PAGES)
    def test_unpaired_devices_get_the_app_shell(self, path):
        response = remote().get(path)

        assert response.status_code == 200
        assert b'<div id="root"></div>' in response.data

    def test_unpaired_devices_get_static_assets(self, tmp_path, monkeypatch):
        (tmp_path / "assets").mkdir()
        (tmp_path / "assets" / "index-abc.js").write_text("export {};\n", encoding="utf-8")
        (tmp_path / "companion-manifest.json").write_text("{}\n", encoding="utf-8")
        monkeypatch.setattr(server_module.app, "static_folder", str(tmp_path))

        assert remote().get("/assets/index-abc.js").status_code == 200
        assert remote().get("/companion-manifest.json").status_code == 200

    @pytest.mark.parametrize(("method", "path"), PROTECTED)
    def test_unpaired_devices_are_refused(self, method, path, shutdowns):
        response = remote().open(path, method=method)

        assert response.status_code == 401
        assert response.get_json() == {"error": "pairing_required"}
        assert shutdowns.empty()

    @pytest.mark.parametrize(("method", "path"), PROTECTED)
    def test_paired_devices_and_the_kiosk_have_full_access(self, method, path, shutdowns):
        phone = paired_phone()

        for client in (phone, kiosk()):
            assert client.open(path, method=method).status_code not in (401, 403)
        wait_for_shutdowns(shutdowns, 2 if path == "/api/shutdown" else 0)

    @pytest.mark.parametrize("path", LOCAL_ONLY)
    def test_pairing_administration_is_local_only(self, path):
        phone = paired_phone()

        for client in (phone, remote("192.168.1.77")):
            response = client.post(path)
            assert response.status_code == 403
            assert response.get_json() == {"error": "local_only"}

    @pytest.mark.parametrize(
        "header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Proto"]
    )
    def test_loopback_requests_through_a_proxy_need_pairing(self, header):
        response = kiosk().get("/api/session-history", headers={header: "203.0.113.9"})

        assert response.status_code == 401

    def test_wrong_cookie_is_refused(self):
        phone = remote()
        phone.set_cookie(DEVICE_COOKIE_NAME, "not-the-secret")

        assert phone.get("/api/session-history").status_code == 401

    def test_cross_site_pages_cannot_post(self, shutdowns):
        phone = paired_phone()

        refused = phone.post("/api/shutdown", headers={"Origin": "https://evil.example"})
        same_origin = phone.post("/api/shutdown", headers={"Origin": "http://localhost"})

        assert refused.status_code == 403
        assert refused.get_json() == {"error": "origin_not_allowed"}
        assert same_origin.status_code == 200
        wait_for_shutdowns(shutdowns, 1)

    def test_foreign_pages_cannot_use_the_kiosks_local_trust(self, shutdowns):
        refused = kiosk().post("/api/shutdown", headers={"Origin": "https://evil.example"})
        dev_server = kiosk().post("/api/shutdown", headers={"Origin": "http://127.0.0.1:5173"})

        assert refused.status_code == 403
        assert dev_server.status_code == 200
        wait_for_shutdowns(shutdowns, 1)

    def test_cors_is_limited_to_local_dev_servers(self):
        dev = kiosk().get("/api/session-history", headers={"Origin": "http://127.0.0.1:5173"})
        foreign = kiosk().get("/api/session-history", headers={"Origin": "https://evil.example"})

        assert dev.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"
        assert "Access-Control-Allow-Origin" not in foreign.headers


class TestPairingApi:
    """Issuing, exchanging, and revoking."""

    def test_status_reports_local_paired_and_unpaired(self):
        assert kiosk().get("/api/pairing/status").get_json() == {"state": "local"}
        assert remote().get("/api/pairing/status").get_json() == {"state": "unpaired"}
        assert paired_phone().get("/api/pairing/status").get_json() == {"state": "paired"}

    def test_status_refreshes_a_paired_cookie(self):
        response = paired_phone().get("/api/pairing/status")

        assert response.headers["Cache-Control"] == "no-store"
        assert f"Max-Age={DEVICE_COOKIE_MAX_AGE_S}" in response.headers["Set-Cookie"]
        assert "Set-Cookie" not in remote().get("/api/pairing/status").headers

    def test_kiosk_gets_a_code_with_qr_links(self):
        response = kiosk().post("/api/pairing/code", base_url="http://localhost:8080")
        body = response.get_json()

        assert response.headers["Cache-Control"] == "no-store"
        assert len(body["code"]) == 8
        assert body["expires_in_s"] == 300
        assert [link["url"] for link in body["links"]] == [
            f"http://openflight.local:8080/pair#code={body['code']}",
            f"http://192.168.1.23:8080/pair#code={body['code']}",
        ]
        assert all(link["qr_svg"].startswith("data:image/svg+xml") for link in body["links"])
        assert body["wifi"] is None

    def test_code_shows_the_hotspot_name_and_password(self, monkeypatch):
        monkeypatch.setattr(server_module, "hotspot_ssid", "TestNet")
        monkeypatch.setattr(server_module, "hotspot_password", "test-pass-123")

        body = kiosk().post("/api/pairing/code").get_json()

        assert body["wifi"]["ssid"] == "TestNet"
        assert body["wifi"]["password"] == "test-pass-123"
        assert body["wifi"]["qr_svg"] == wifi_join_qr_svg("TestNet", "test-pass-123")
        assert body["wifi"]["qr_svg"].startswith("data:image/svg+xml")

    def test_code_shows_the_hotspot_password_without_a_name(self, monkeypatch):
        monkeypatch.setattr(server_module, "hotspot_ssid", None)
        monkeypatch.setattr(server_module, "hotspot_password", "test-pass-123")

        body = kiosk().post("/api/pairing/code").get_json()

        assert body["wifi"] == {"ssid": None, "password": "test-pass-123", "qr_svg": None}

    @pytest.mark.parametrize("password", [None, ""])
    def test_code_has_no_wifi_without_a_password(self, monkeypatch, password):
        monkeypatch.setattr(server_module, "hotspot_ssid", "TestNet")
        monkeypatch.setattr(server_module, "hotspot_password", password)

        assert kiosk().post("/api/pairing/code").get_json()["wifi"] is None

    def test_hotspot_password_is_never_logged(self, monkeypatch, caplog, capsys):
        monkeypatch.setattr(server_module, "hotspot_ssid", "TestNet")
        monkeypatch.setattr(server_module, "hotspot_password", "test-pass-123")
        caplog.set_level(logging.DEBUG)

        response = kiosk().post("/api/pairing/code")

        wifi = response.get_json()["wifi"]
        assert wifi["password"] == "test-pass-123"
        captured = capsys.readouterr()
        logged = caplog.text + captured.out + captured.err
        assert "test-pass-123" not in logged
        assert wifi["qr_svg"] not in logged
        assert "WIFI:" not in logged

    def test_hotspot_wifi_is_only_in_the_code_response(self, monkeypatch):
        monkeypatch.setattr(server_module, "hotspot_ssid", "TestNet")
        monkeypatch.setattr(server_module, "hotspot_password", "test-pass-123")

        for response in (kiosk().get("/api/pairing/status"), remote().post("/api/pairing/code")):
            assert b"test-pass-123" not in response.get_data()

    def test_exchange_sets_a_long_lived_strict_http_only_cookie(self, emitted):
        code = issue_code()

        response = remote().post("/api/pairing/exchange", json={"code": code.lower()})

        assert response.status_code == 200
        assert response.get_json() == {"state": "paired"}
        cookie = response.headers["Set-Cookie"]
        assert cookie.startswith(f"{DEVICE_COOKIE_NAME}={server_module.device_secret.value()};")
        for flag in ("HttpOnly", "SameSite=Strict", "Path=/", f"Max-Age={DEVICE_COOKIE_MAX_AGE_S}"):
            assert flag in cookie
        assert "Secure" not in cookie
        assert ("pairing_code_closed", {"reason": "paired"}) in emitted

    def test_exchange_over_https_marks_the_cookie_secure(self):
        code = issue_code()

        response = remote().post(
            "/api/pairing/exchange", json={"code": code}, base_url="https://openflight.local"
        )

        assert "Secure" in response.headers["Set-Cookie"]

    def test_code_is_single_use(self):
        code = issue_code()
        assert remote().post("/api/pairing/exchange", json={"code": code}).status_code == 200

        second = remote("192.168.1.77").post("/api/pairing/exchange", json={"code": code})

        assert second.status_code == 403
        assert second.get_json() == {"error": "invalid_code"}
        assert "Set-Cookie" not in second.headers

    @pytest.mark.parametrize("body", [{"json": {"code": "WRONG234"}}, {"json": {}}, {"data": "x"}])
    def test_wrong_or_malformed_codes_are_refused(self, body):
        issue_code()

        response = remote().post("/api/pairing/exchange", **body)

        assert response.status_code == 403
        assert response.get_json() == {"error": "invalid_code"}

    def test_repeated_wrong_codes_are_rate_limited(self):
        code = issue_code()
        phone = remote()
        for _ in range(CLIENT_FAILURE_LIMIT):
            phone.post("/api/pairing/exchange", json={"code": "WRONG234"})

        response = phone.post("/api/pairing/exchange", json={"code": code})

        assert response.status_code == 429
        assert response.get_json()["error"] == "too_many_attempts"
        assert int(response.headers["Retry-After"]) == response.get_json()["retry_after_s"] > 0
        assert "Set-Cookie" not in response.headers

    def test_code_is_locked_after_too_many_failures(self, emitted):
        code = issue_code()
        for index in range(CODE_FAILURE_LIMIT):
            remote(f"192.168.1.{100 + index}").post(
                "/api/pairing/exchange", json={"code": "WRONG234"}
            )

        response = remote().post("/api/pairing/exchange", json={"code": code})

        assert response.status_code == 403
        assert emitted.count(("pairing_code_closed", {"reason": "locked"})) == 1

    def test_unreadable_secret_fails_without_consuming_the_code(self, monkeypatch):
        code = issue_code()

        def unavailable():
            raise PermissionError("read-only file system")

        with monkeypatch.context() as patch:
            patch.setattr(server_module.device_secret, "value", unavailable)
            response = remote().post("/api/pairing/exchange", json={"code": code})

        assert response.status_code == 503
        assert response.get_json() == {"error": "pairing_unavailable"}
        assert remote().post("/api/pairing/exchange", json={"code": code}).status_code == 200

    def test_revoke_unpairs_every_device_and_drops_the_code(self):
        phone = paired_phone()
        other = paired_phone("192.168.1.77")
        code = issue_code()

        response = kiosk().post("/api/pairing/revoke")

        assert response.status_code == 200
        assert response.get_json() == {"status": "revoked"}
        for device in (phone, other):
            assert device.get("/api/session-history").status_code == 401
            assert device.get("/api/pairing/status").get_json() == {"state": "unpaired"}
        assert remote().post("/api/pairing/exchange", json={"code": code}).status_code == 403
        assert kiosk().get("/api/session-history").status_code == 200

    def test_failed_revoke_keeps_devices_paired(self, monkeypatch):
        phone = paired_phone()

        def read_only():
            raise PermissionError("read-only file system")

        monkeypatch.setattr(server_module.device_secret, "rotate", read_only)
        response = kiosk().post("/api/pairing/revoke")

        assert response.status_code == 503
        assert response.get_json() == {"error": "revoke_failed"}
        assert phone.get("/api/session-history").status_code == 200


@pytest.fixture(name="socket_client")
def fixture_socket_client(monkeypatch):
    """Flask-SocketIO test clients; restores the server hooks the test client patches."""
    server = server_module.socketio.server
    for name in ("_send_packet", "_send_eio_packet", "async_handlers"):
        monkeypatch.setattr(server, name, getattr(server, name))
    monkeypatch.setattr(server.eio, "async_handlers", server.eio.async_handlers)
    clients = []

    def connect(http_client=None, headers=None):
        client = server_module.socketio.test_client(
            server_module.app, flask_test_client=http_client or kiosk(), headers=headers
        )
        clients.append(client)
        return client

    yield connect
    for client in clients:
        if client.is_connected():
            client.disconnect()


class TestSocketIo:
    """Live updates reach only the kiosk and paired devices."""

    def test_kiosk_connects(self, socket_client):
        assert socket_client().is_connected()

    def test_unpaired_device_is_refused(self, socket_client):
        client = socket_client(remote())

        assert not client.is_connected()
        assert server_module._paired_socket_sids == set()

    def test_forwarded_loopback_connection_is_refused(self, socket_client):
        assert not socket_client(headers={"X-Forwarded-For": "203.0.113.9"}).is_connected()

    def test_paired_device_connects(self, socket_client):
        client = socket_client(paired_phone())

        assert client.is_connected()
        assert len(server_module._paired_socket_sids) == 1

    def test_revoke_disconnects_paired_devices_but_not_the_kiosk(self, socket_client):
        phone = paired_phone()
        phone_socket = socket_client(phone)
        kiosk_socket = socket_client()

        assert kiosk().post("/api/pairing/revoke").status_code == 200

        assert not phone_socket.is_connected()
        assert kiosk_socket.is_connected()
        assert server_module._paired_socket_sids == set()
        assert not socket_client(phone).is_connected()

    def test_disconnect_forgets_the_paired_socket(self, socket_client):
        client = socket_client(paired_phone())

        client.disconnect()

        assert server_module._paired_socket_sids == set()

    @pytest.mark.parametrize(
        ("client", "origin", "accepted"),
        [
            ("kiosk", "http://localhost", True),
            ("kiosk", "http://127.0.0.1:5173", True),
            ("kiosk", "https://evil.example", False),
            ("phone", "http://localhost", True),
            ("phone", "http://127.0.0.1:5173", False),
            ("phone", "https://evil.example", False),
        ],
    )
    def test_connection_origin_must_be_the_app_itself(
        self, socket_client, client, origin, accepted
    ):
        http = kiosk() if client == "kiosk" else paired_phone()

        connection = socket_client(http, headers={"Origin": origin})

        assert connection.is_connected() is accepted
