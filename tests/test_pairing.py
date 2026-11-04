"""Tests for device pairing: local trust, the device secret, and one-time codes."""

import re
import stat
from urllib.parse import unquote

import pytest

from openflight import pairing
from openflight.pairing import (
    CLIENT_FAILURE_LIMIT,
    CLIENT_FAILURE_WINDOW_S,
    CODE_ALPHABET,
    CODE_FAILURE_LIMIT,
    CODE_LENGTH,
    CODE_TTL_S,
    DEFAULT_PAIRING_SECRET_PATH,
    LOOPBACK_ORIGIN_PATTERN,
    PAIRING_SECRET_PATH_ENV,
    DeviceSecret,
    PairingCodes,
    is_local_environ,
    normalize_code,
    origin_allowed,
    pairing_hosts,
    pairing_links,
    resolve_secret_path,
    wifi_join_qr_svg,
)


class FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestLocalTrust:
    """Only the Pi's own loopback clients are trusted without pairing."""

    @pytest.mark.parametrize("address", ["127.0.0.1", "127.0.1.1", "::1", "::ffff:127.0.0.1"])
    def test_loopback_peers_are_local(self, address):
        assert is_local_environ({"REMOTE_ADDR": address})

    @pytest.mark.parametrize(
        "address",
        ["192.168.1.20", "100.101.102.103", "fe80::1", "::ffff:192.168.1.20", "", None, "x"],
    )
    def test_other_peers_are_not_local(self, address):
        assert not is_local_environ({"REMOTE_ADDR": address})

    @pytest.mark.parametrize(
        "header",
        [
            "HTTP_X_FORWARDED_FOR",
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PROTO",
            "HTTP_FORWARDED",
            "HTTP_X_REAL_IP",
        ],
    )
    def test_loopback_through_a_proxy_is_not_local(self, header):
        assert not is_local_environ({"REMOTE_ADDR": "127.0.0.1", header: "192.168.1.20"})


class TestOriginCheck:
    """Browsers may only use the server from its own origin (or a local dev server)."""

    REMOTE = {
        "REMOTE_ADDR": "192.168.1.20",
        "HTTP_HOST": "openflight.local:8080",
        "wsgi.url_scheme": "http",
    }
    LOCAL = {"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1:8080", "wsgi.url_scheme": "http"}

    @pytest.mark.parametrize("origin", [None, ""])
    def test_clients_without_origin_are_allowed(self, origin):
        assert origin_allowed(origin, self.REMOTE)

    def test_same_origin_is_allowed_case_insensitively(self):
        assert origin_allowed("http://openflight.local:8080", self.REMOTE)
        assert origin_allowed("http://OpenFlight.local:8080", self.REMOTE)

    @pytest.mark.parametrize(
        "origin",
        [
            "https://evil.example",
            "http://openflight.local:5173",
            "https://openflight.local:8080",
            "http://127.0.0.1:5173",
            "null",
        ],
    )
    def test_cross_origin_browsers_are_rejected_for_remote_clients(self, origin):
        assert not origin_allowed(origin, self.REMOTE)

    @pytest.mark.parametrize(
        "origin", ["http://127.0.0.1:5173", "http://localhost:5173", "http://[::1]:5173"]
    )
    def test_local_dev_servers_may_call_a_local_backend(self, origin):
        assert origin_allowed(origin, self.LOCAL)

    @pytest.mark.parametrize("origin", ["https://evil.example", "file://", "null"])
    def test_foreign_pages_cannot_use_local_trust(self, origin):
        assert not origin_allowed(origin, self.LOCAL)

    def test_cors_pattern_only_matches_loopback_origins(self):
        for origin in ("http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:5173"):
            assert re.match(LOOPBACK_ORIGIN_PATTERN, origin)
        for origin in (
            "http://localhost.evil.example",
            "http://127.0.0.1.evil.example",
            "http://pi:5173",
        ):
            assert not re.match(LOOPBACK_ORIGIN_PATTERN, origin)


class TestDeviceSecret:
    """One persisted secret; rotating it unpairs every device."""

    def test_first_use_creates_a_private_long_secret(self, tmp_path):
        path = tmp_path / "config" / "pairing-secret"

        value = DeviceSecret(path).value()

        assert len(value) >= 43
        assert re.fullmatch(r"[A-Za-z0-9_-]+", value)
        assert path.read_text(encoding="utf-8").strip() == value
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_secret_survives_a_restart(self, tmp_path):
        path = tmp_path / "pairing-secret"
        value = DeviceSecret(path).value()

        assert DeviceSecret(path).value() == value

    def test_matches_only_the_exact_secret(self, tmp_path):
        secret = DeviceSecret(tmp_path / "pairing-secret")
        value = secret.value()

        assert secret.matches(value)
        assert not secret.matches(value[:-1])
        assert not secret.matches(value + "x")
        assert not secret.matches("")
        assert not secret.matches(None)
        assert not secret.matches(42)
        assert not secret.matches("ä" * len(value))

    def test_anonymous_checks_do_not_create_the_file(self, tmp_path):
        path = tmp_path / "pairing-secret"

        assert not DeviceSecret(path).matches(None)
        assert not path.exists()

    def test_rotation_replaces_the_secret_on_disk(self, tmp_path):
        path = tmp_path / "pairing-secret"
        secret = DeviceSecret(path)
        old = secret.value()

        new = secret.rotate()

        assert new != old
        assert not secret.matches(old)
        assert secret.matches(new)
        assert DeviceSecret(path).value() == new
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert [entry.name for entry in tmp_path.iterdir()] == ["pairing-secret"]

    def test_unusable_file_is_replaced(self, tmp_path):
        path = tmp_path / "pairing-secret"
        path.write_text("short\n", encoding="utf-8")

        value = DeviceSecret(path).value()

        assert value != "short"
        assert path.read_text(encoding="utf-8").strip() == value

    def test_path_comes_from_argument_then_environment_then_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(PAIRING_SECRET_PATH_ENV, str(tmp_path / "env-secret"))
        assert resolve_secret_path(tmp_path / "arg-secret") == tmp_path / "arg-secret"
        assert resolve_secret_path() == tmp_path / "env-secret"

        monkeypatch.delenv(PAIRING_SECRET_PATH_ENV)
        assert resolve_secret_path() == DEFAULT_PAIRING_SECRET_PATH
        assert DEFAULT_PAIRING_SECRET_PATH.parent.name == "openflight"


class TestPairingCodes:
    """Short-lived, single-use codes with brute-force limits."""

    def make(self):
        clock = FakeClock()
        return PairingCodes(clock=clock), clock

    def test_code_is_short_and_unambiguous(self):
        codes, _ = self.make()

        issued = codes.issue()

        assert len(issued.code) == CODE_LENGTH == 8
        assert set(issued.code) <= set(CODE_ALPHABET)
        assert not set("01IOL") & set(CODE_ALPHABET)
        assert issued.expires_in_s == CODE_TTL_S == 300

    def test_code_is_accepted_once(self):
        codes, _ = self.make()
        code = codes.issue().code

        first = codes.exchange(code, "phone")
        second = codes.exchange(code, "phone")

        assert first.status == "accepted"
        assert first.code_closed
        assert second.status == "invalid"

    @pytest.mark.parametrize("typed", ["{c}", "{lower}", "{c4}-{rest}", " {c4} {rest} "])
    def test_typed_code_is_normalized(self, typed):
        codes, _ = self.make()
        code = codes.issue().code
        entry = typed.format(c=code, lower=code.lower(), c4=code[:4], rest=code[4:])

        assert codes.exchange(entry, "phone").status == "accepted"

    def test_normalize_code_ignores_non_strings(self):
        assert normalize_code(None) == ""
        assert normalize_code(12345678) == ""
        assert normalize_code("ab-cd ef") == "ABCDEF"

    def test_code_expires(self):
        codes, clock = self.make()
        code = codes.issue().code

        clock.advance(CODE_TTL_S)

        assert codes.exchange(code, "phone").status == "invalid"

    def test_new_code_replaces_the_previous_one(self):
        codes, _ = self.make()
        old = codes.issue().code
        new = codes.issue().code

        assert codes.exchange(old, "phone").status == "invalid"
        assert codes.exchange(new, "phone").status == "accepted"

    def test_clear_invalidates_the_code(self):
        codes, _ = self.make()
        code = codes.issue().code

        codes.clear()

        assert codes.exchange(code, "phone").status == "invalid"

    @pytest.mark.parametrize("candidate", [None, 42, "", "ÄÖÜÄÖÜÄÖ", "ABCDEFGHJ"])
    def test_malformed_candidates_are_invalid(self, candidate):
        codes, _ = self.make()
        codes.issue()

        assert codes.exchange(candidate, "phone").status == "invalid"

    def test_client_is_rate_limited_after_repeated_wrong_codes(self):
        codes, clock = self.make()
        code = codes.issue().code
        for _ in range(CLIENT_FAILURE_LIMIT):
            assert codes.exchange("WRONGXXX", "attacker").status == "invalid"

        limited = codes.exchange(code, "attacker")

        assert limited.status == "rate_limited"
        assert 0 < limited.retry_after_s <= CLIENT_FAILURE_WINDOW_S
        assert codes.exchange(code, "phone").status == "accepted"

    def test_rate_limit_lifts_after_the_window(self):
        codes, clock = self.make()
        codes.issue()
        for _ in range(CLIENT_FAILURE_LIMIT):
            codes.exchange("WRONGXXX", "phone")
        clock.advance(CLIENT_FAILURE_WINDOW_S)
        code = codes.issue().code

        assert codes.exchange(code, "phone").status == "accepted"

    def test_success_forgets_earlier_failures(self):
        codes, _ = self.make()
        code = codes.issue().code
        for _ in range(CLIENT_FAILURE_LIMIT - 1):
            codes.exchange("WRONGXXX", "phone")
        assert codes.exchange(code, "phone").status == "accepted"
        code = codes.issue().code

        for _ in range(CLIENT_FAILURE_LIMIT - 1):
            assert codes.exchange("WRONGXXX", "phone").status == "invalid"
        assert codes.exchange(code, "phone").status == "accepted"

    def test_code_is_locked_after_too_many_failures_from_any_client(self):
        codes, _ = self.make()
        code = codes.issue().code
        outcomes = [
            codes.exchange("WRONGXXX", f"client-{index}") for index in range(CODE_FAILURE_LIMIT)
        ]

        assert [outcome.code_closed for outcome in outcomes] == [False] * (
            CODE_FAILURE_LIMIT - 1
        ) + [True]
        assert codes.exchange(code, "phone").status == "invalid"

    def test_failures_without_an_active_code_do_not_lock_the_next_code(self):
        codes, _ = self.make()
        for index in range(CODE_FAILURE_LIMIT):
            codes.exchange("WRONGXXX", f"client-{index}")

        code = codes.issue().code

        assert codes.exchange(code, "phone").status == "accepted"


class TestPairingLinks:
    """QR links point phones at the Pi's mDNS name and LAN address."""

    def test_hosts_are_the_mdns_name_then_the_lan_address(self, monkeypatch):
        monkeypatch.setattr(pairing.socket, "gethostname", lambda: "OpenFlight")
        monkeypatch.setattr(pairing, "_primary_ipv4", lambda: "192.168.1.23")

        assert pairing_hosts() == ["openflight.local", "192.168.1.23"]

    def test_hosts_skip_unusable_names_and_addresses(self, monkeypatch):
        monkeypatch.setattr(pairing.socket, "gethostname", lambda: "localhost")
        monkeypatch.setattr(pairing, "_primary_ipv4", lambda: None)

        assert pairing_hosts() == []

    def test_fqdn_hostname_uses_its_short_name(self, monkeypatch):
        monkeypatch.setattr(pairing.socket, "gethostname", lambda: "openflight.lan")
        monkeypatch.setattr(pairing, "_primary_ipv4", lambda: None)

        assert pairing_hosts() == ["openflight.local"]

    def test_primary_address_is_none_without_a_route(self, monkeypatch):
        class NoRoute:
            def __init__(self, *_args):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def connect(self, _address):
                raise OSError("Network is unreachable")

        monkeypatch.setattr(pairing.socket, "socket", NoRoute)

        assert pairing._primary_ipv4() is None

    def test_links_carry_the_code_in_the_fragment_and_a_qr_image(self, monkeypatch):
        monkeypatch.setattr(pairing, "pairing_hosts", lambda: ["openflight.local", "192.168.1.23"])

        links = pairing_links("ABCD2345", 8080)

        assert [link["url"] for link in links] == [
            "http://openflight.local:8080/pair#code=ABCD2345",
            "http://192.168.1.23:8080/pair#code=ABCD2345",
        ]
        assert [link["host"] for link in links] == ["openflight.local", "192.168.1.23"]
        for link in links:
            assert link["qr_svg"].startswith("data:image/svg+xml")
            assert "viewBox" in unquote(link["qr_svg"])


class TestWifiJoinQr:
    """The hotspot join code is a standard escaped ``WIFI:`` payload rendered as a QR image."""

    def test_payload_escapes_special_characters(self, monkeypatch):
        payloads = []
        make_qr = pairing.segno.make_qr

        def spy(content, **kwargs):
            payloads.append(content)
            return make_qr(content, **kwargs)

        monkeypatch.setattr(pairing.segno, "make_qr", spy)

        wifi_join_qr_svg("Net;1", 'p:a"ss,\\')

        assert payloads == [pairing.segno.helpers.make_wifi_data("Net;1", 'p:a"ss,\\', "WPA")]
        assert payloads[0].startswith(r"WIFI:T:WPA;S:Net\;1;P:p\:a\"ss")
        assert payloads[0].endswith(";;")

    def test_renders_an_svg_data_uri(self):
        qr_svg = wifi_join_qr_svg("TestNet", "e2e-dummy-pass-1234")

        assert qr_svg.startswith("data:image/svg+xml")
        assert "viewBox" in unquote(qr_svg)
        assert "e2e-dummy-pass-1234" not in unquote(qr_svg)
