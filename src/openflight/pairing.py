"""Device pairing for every client that is not the Pi's own kiosk.

A request is local when its TCP peer is loopback and it carries no proxy
forwarding headers; the kiosk (Electron on ``http://localhost:8080``) and the
start script are local. Every other client must present the persisted device
secret in an HttpOnly cookie, which it obtains by exchanging a short-lived
one-time code shown on the kiosk. Browsers keep a cookie for at most 400 days,
so every status check renews it; pairing lasts until the secret is rotated,
which unpairs all devices at once.

A reverse proxy or port forwarder on the Pi (for example ``tailscale serve``)
also connects from loopback. It must add ``X-Forwarded-For``; one that does not
would make every client it forwards local.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import math
import os
import secrets
import socket
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Mapping, Optional, Union
from urllib.parse import urlsplit

import segno
import segno.helpers

logger = logging.getLogger(__name__)

PAIRING_SECRET_PATH_ENV = "OPENFLIGHT_PAIRING_SECRET_PATH"
DEFAULT_PAIRING_SECRET_PATH = Path.home() / ".config" / "openflight" / "pairing-secret"
DEVICE_COOKIE_NAME = "openflight_device"
DEVICE_COOKIE_MAX_AGE_S = 400 * 24 * 60 * 60
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
CODE_TTL_S = 300
CLIENT_FAILURE_LIMIT = 5
CLIENT_FAILURE_WINDOW_S = 60
CODE_FAILURE_LIMIT = 10
LOOPBACK_ORIGIN_PATTERN = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

_MIN_SECRET_LENGTH = 32
_PROXY_ENVIRON_KEYS = ("HTTP_FORWARDED", "HTTP_X_REAL_IP")
_UNROUTED_PROBE_ADDRESS = ("192.0.2.1", 9)


def is_loopback_address(address: object) -> bool:
    """True for loopback IPv4/IPv6 addresses, including IPv4-mapped IPv6."""
    if not isinstance(address, str):
        return False
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    return (mapped or parsed).is_loopback


def is_local_environ(environ: Mapping[str, object]) -> bool:
    """Whether a WSGI request comes from the Pi itself rather than through a proxy."""
    if any(key.startswith("HTTP_X_FORWARDED_") or key in _PROXY_ENVIRON_KEYS for key in environ):
        return False
    return is_loopback_address(environ.get("REMOTE_ADDR"))


def origin_allowed(origin: Optional[str], environ: Mapping[str, object]) -> bool:
    """Allow non-browser clients, same-origin pages, and local dev servers on a local request.

    Used for Socket.IO connections and state-changing HTTP requests so that a
    foreign page cannot ride a paired browser's cookie or the kiosk's local trust.
    """
    if not origin:
        return True
    own_origin = f"{environ.get('wsgi.url_scheme', 'http')}://{environ.get('HTTP_HOST', '')}"
    if origin.lower() == own_origin.lower():
        return True
    return is_local_environ(environ) and _is_loopback_origin(origin)


def _is_loopback_origin(origin: str) -> bool:
    try:
        parts = urlsplit(origin)
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not hostname:
        return False
    return hostname == "localhost" or is_loopback_address(hostname)


def resolve_secret_path(path: Union[str, Path, None] = None) -> Path:
    """Constructor argument, then ``OPENFLIGHT_PAIRING_SECRET_PATH``, then the user default."""
    if path is not None and str(path).strip():
        return Path(path).expanduser()
    env_path = (os.environ.get(PAIRING_SECRET_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_PAIRING_SECRET_PATH


class DeviceSecret:
    """The secret every paired device presents. It is created on first use."""

    def __init__(self, path: Union[str, Path, None] = None):
        self._path = resolve_secret_path(path)
        self._lock = threading.Lock()
        self._value: Optional[str] = None

    @property
    def path(self) -> Path:
        """Where the secret is persisted (mode 0600)."""
        return self._path

    def value(self) -> str:
        """The current secret, loaded from disk or created and persisted."""
        with self._lock:
            if self._value is None:
                self._value = self._load() or self._write(secrets.token_urlsafe(32))
            return self._value

    def matches(self, candidate: object) -> bool:
        """Constant-time comparison of a presented cookie value."""
        if not isinstance(candidate, str) or not candidate or not candidate.isascii():
            return False
        return hmac.compare_digest(candidate, self.value())

    def rotate(self) -> str:
        """Persist a new secret; every device paired with the old one must pair again."""
        with self._lock:
            self._value = self._write(secrets.token_urlsafe(32))
            return self._value

    def _load(self) -> Optional[str]:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if len(text) < _MIN_SECRET_LENGTH or not text.isascii():
            logger.warning("[PAIRING] Replacing an unusable device secret in %s", self._path)
            return None
        return text

    def _write(self, value: str) -> str:
        """Atomically replace the file so a power cut never leaves a partial or old secret."""
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{self._path.name}-")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(f"{value}\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self._path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return value


def normalize_code(candidate: object) -> str:
    """Accept typed codes in any case, with spaces or a dash between the halves."""
    if not isinstance(candidate, str):
        return ""
    return "".join(character for character in candidate if character not in " -\t").upper()


@dataclass(frozen=True)
class IssuedCode:
    """A pairing code as shown on the kiosk."""

    code: str
    expires_in_s: int


@dataclass(frozen=True)
class ExchangeOutcome:
    """Result of one code exchange; ``code_closed`` means the active code is gone."""

    status: Literal["accepted", "invalid", "rate_limited"]
    retry_after_s: int = 0
    code_closed: bool = False


class PairingCodes:
    """At most one active one-time code, plus per-client and per-code attempt limits."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._code: Optional[str] = None
        self._expires_at = 0.0
        self._code_failures = 0
        self._client_failures: dict[str, deque[float]] = {}

    def issue(self) -> IssuedCode:
        """Create a new code; any previous code stops working."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        with self._lock:
            self._code = code
            self._expires_at = self._clock() + CODE_TTL_S
            self._code_failures = 0
        return IssuedCode(code=code, expires_in_s=CODE_TTL_S)

    def clear(self) -> None:
        """Invalidate the active code."""
        with self._lock:
            self._code = None

    def exchange(self, candidate: object, client: str) -> ExchangeOutcome:
        """Consume the active code if ``candidate`` matches and ``client`` is not rate limited."""
        now = self._clock()
        with self._lock:
            self._forget_failures_before(now - CLIENT_FAILURE_WINDOW_S)
            failures = self._client_failures.get(client)
            if failures is not None and len(failures) >= CLIENT_FAILURE_LIMIT:
                retry_after_s = math.ceil(failures[0] + CLIENT_FAILURE_WINDOW_S - now)
                return ExchangeOutcome("rate_limited", retry_after_s=max(retry_after_s, 1))
            if self._code is not None and now >= self._expires_at:
                self._code = None
            if self._code is not None and _codes_equal(normalize_code(candidate), self._code):
                self._code = None
                self._client_failures.pop(client, None)
                return ExchangeOutcome("accepted", code_closed=True)
            self._client_failures.setdefault(client, deque()).append(now)
            if self._code is None:
                return ExchangeOutcome("invalid")
            self._code_failures += 1
            if self._code_failures >= CODE_FAILURE_LIMIT:
                self._code = None
                return ExchangeOutcome("invalid", code_closed=True)
            return ExchangeOutcome("invalid")

    def _forget_failures_before(self, cutoff: float) -> None:
        for client in list(self._client_failures):
            failures = self._client_failures[client]
            while failures and failures[0] <= cutoff:
                failures.popleft()
            if not failures:
                del self._client_failures[client]


def _codes_equal(candidate: str, code: str) -> bool:
    return candidate.isascii() and hmac.compare_digest(candidate, code)


def pairing_hosts() -> list[str]:
    """Names a phone on the LAN can use for this Pi: its mDNS name, then its LAN IPv4."""
    hosts = []
    name = socket.gethostname().split(".", 1)[0].strip().lower()
    if name and name != "localhost":
        hosts.append(f"{name}.local")
    address = _primary_ipv4()
    if address:
        hosts.append(address)
    return hosts


def _primary_ipv4() -> Optional[str]:
    """Source address of the default route; connecting a UDP socket sends no packet."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_UNROUTED_PROBE_ADDRESS)
            address = probe.getsockname()[0]
    except OSError:
        return None
    if not address or address == "0.0.0.0" or is_loopback_address(address):
        return None
    return address


def _qr_svg_data_uri(qr: segno.QRCode) -> str:
    return qr.svg_data_uri(dark="#000", light="#fff", border=4, omitsize=True)


def pairing_links(code: str, port: Union[int, str]) -> list[dict]:
    """``/pair`` URLs with the code in the fragment (never sent to the server) and QR images."""
    links = []
    for host in pairing_hosts():
        url = f"http://{host}:{port}/pair#code={code}"
        qr_svg = _qr_svg_data_uri(segno.make(url, error="m"))
        links.append({"host": host, "url": url, "qr_svg": qr_svg})
    return links


def wifi_join_qr_svg(ssid: str, password: str) -> str:
    """QR image a phone camera scans to join a WPA network (``WIFI:`` payload, escaped)."""
    payload = segno.helpers.make_wifi_data(ssid=ssid, password=password, security="WPA")
    return _qr_svg_data_uri(segno.make_qr(payload, error="m"))
