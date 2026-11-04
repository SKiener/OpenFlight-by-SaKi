"""Phone-measured TI IWR6843 tilt: two-position math, validation and persistence.

The phone lies flat, screen up, on the case top face with its long side along
the shot direction, once with its top end toward the ball (position A) and once
rotated 180 degrees (position B). Each position reports an averaged gravity
vector in the W3C DeviceMotion device frame (x right, y toward the phone top,
z out of the screen; ``accelerationIncludingGravity`` of a phone lying face up
at rest reads about ``(0, 0, +9.81)`` m/s^2).

Per position ``pitch = atan2(a_y, a_z)``: positive when the phone's top end is
raised. With case tilt ``theta`` (positive = ball side higher = boresight up)
and the phone's own bias ``b``::

    pitch_A = +theta + b        pitch_B = -theta + b
    theta = (pitch_A - pitch_B) / 2       b = (pitch_A + pitch_B) / 2

so the phone's bias cancels. The same holds across the phone (``roll =
atan2(a_x, a_z)``, positive when the phone's right edge is raised):
``roll_A = +phi + r`` and ``roll_B = -phi + r``, where ``phi`` is the case side
roll and ``r`` the phone's own roll bias.

The pure functions here never touch the radar runtime; ``server.py`` applies
the result.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# CAD (OpenFlightCaseV3, rev L): the case top face is parallel to the case
# bottom and to the LIS3DH board, and the TI boresight sits +10.0 degrees above
# the top face (antenna plane at 80 degrees to the top face, leaning back).
# See analysis/vla-bias-20261030/referenzflaeche/bericht.md.
CASE_TOP_TO_BORESIGHT_DEG = 10.0

STANDARD_GRAVITY_MPS2 = 9.80665
SCHEMA_VERSION = 1

MIN_SAMPLE_COUNT = 30
MIN_GRAVITY_G = 0.9
MAX_GRAVITY_G = 1.1
MAX_PITCH_STD_DEG = 0.5
MIN_FLAT_RATIO = 0.9
MAX_CASE_TILT_DEG = 8.0
MAX_PHONE_BIAS_DEG = 3.0
MAX_PHONE_ROLL_BIAS_DEG = 3.0
MAX_CONFIGURED_TILT_DEG = 45.0

# "w3c": the vector is used as sent. "inverted": the client reports every
# axis with the opposite sign (seen on some iOS WebKit builds); the server
# negates the whole vector before any check.
CONVENTIONS = ("w3c", "inverted")
DEFAULT_CONVENTION = "w3c"

_MAX_CLIENT_KEYS = 8
_MAX_CLIENT_KEY_LEN = 40
_MAX_CLIENT_VALUE_LEN = 120
_MAX_MEASURED_AT_LEN = 64

PHONE_TILT_PATH_ENV = "OPENFLIGHT_IWR6843_PHONE_TILT_PATH"


def calibration_path() -> Path:
    """``OPENFLIGHT_IWR6843_PHONE_TILT_PATH``, else the user default file."""
    env_path = (os.environ.get(PHONE_TILT_PATH_ENV) or "").strip()
    if env_path:
        return Path(env_path).expanduser()
    return Path.home() / ".config" / "openflight" / "iwr6843_phone_orientation.json"


class PhoneTiltRejected(ValueError):
    """A phone tilt measurement failed validation; nothing may change."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        position: str | None = None,
        field: str | None = None,
        value: float | None = None,
    ):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.position = position
        self.field = field
        self.value = value

    def to_dict(self) -> dict:
        """JSON error body: ``error`` is the stable reason code."""
        return {
            "error": self.reason,
            "message": self.message,
            "position": self.position,
            "field": self.field,
            "value": None if self.value is None else round(self.value, 4),
        }


def pitch_deg(gravity: tuple[float, float, float]) -> float:
    """Phone pitch in degrees from a W3C gravity vector; top end raised is positive."""
    return math.degrees(math.atan2(gravity[1], gravity[2]))


def roll_deg(gravity: tuple[float, float, float]) -> float:
    """Phone roll in degrees from a W3C gravity vector; right edge raised is positive."""
    return math.degrees(math.atan2(gravity[0], gravity[2]))


def configured_tilt_deg(radar_tilt_deg: float, inclinometer_pitch_deg: float | None) -> float:
    """The ``--iwr6843-tilt-deg`` equivalent that makes ``effective == radar_tilt`` now.

    The server adds the calibrated LIS3DH pitch to the configured tilt for every
    shot, so the configured value is the radar tilt minus the pitch at
    calibration time. Without an inclinometer the configured tilt is the radar
    tilt itself.
    """
    return radar_tilt_deg - (inclinometer_pitch_deg or 0.0)


def _number(value: Any, *, position: str | None, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PhoneTiltRejected(
            "invalid_payload", f"{field} must be a number", position=position, field=field
        )
    number = float(value)
    if not math.isfinite(number):
        raise PhoneTiltRejected(
            "invalid_payload", f"{field} must be finite", position=position, field=field
        )
    return number


@dataclass(frozen=True)
class PositionReading:
    """One validated phone position, gravity already in the W3C convention."""

    gravity_mps2: tuple[float, float, float]
    gravity_g: float
    pitch_deg: float
    roll_deg: float
    pitch_std_deg: float
    sample_count: int

    def to_dict(self) -> dict:
        """Rounded JSON form (gravity as used, after any convention flip)."""
        return {
            "gravity_mps2": [round(value, 5) for value in self.gravity_mps2],
            "gravity_g": round(self.gravity_g, 4),
            "pitch_deg": round(self.pitch_deg, 4),
            "roll_deg": round(self.roll_deg, 4),
            "pitch_std_deg": round(self.pitch_std_deg, 4),
            "sample_count": self.sample_count,
        }


def parse_position(payload: Any, position: str, convention: str) -> PositionReading:
    """Validate one position (``"a"`` or ``"b"``) of a calibration request."""
    if not isinstance(payload, dict):
        raise PhoneTiltRejected(
            "invalid_payload",
            f"position_{position} must be an object",
            position=position,
            field=f"position_{position}",
        )

    sample_count = payload.get("sample_count")
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise PhoneTiltRejected(
            "invalid_payload",
            "sample_count must be an integer",
            position=position,
            field="sample_count",
        )
    if sample_count < MIN_SAMPLE_COUNT:
        raise PhoneTiltRejected(
            "too_few_samples",
            f"at least {MIN_SAMPLE_COUNT} samples are required",
            position=position,
            field="sample_count",
            value=float(sample_count),
        )

    raw = payload.get("gravity")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise PhoneTiltRejected(
            "invalid_payload",
            "gravity must be [x, y, z] in m/s^2",
            position=position,
            field="gravity",
        )
    gravity = tuple(_number(value, position=position, field="gravity") for value in raw)
    if convention == "inverted":
        gravity = tuple(-value for value in gravity)

    pitch_std = _number(payload.get("pitch_std_deg"), position=position, field="pitch_std_deg")
    if pitch_std < 0.0:
        raise PhoneTiltRejected(
            "invalid_payload",
            "pitch_std_deg must not be negative",
            position=position,
            field="pitch_std_deg",
        )
    if pitch_std > MAX_PITCH_STD_DEG:
        raise PhoneTiltRejected(
            "phone_unstable",
            f"the phone moved (pitch std above {MAX_PITCH_STD_DEG} deg); keep it still",
            position=position,
            field="pitch_std_deg",
            value=pitch_std,
        )

    norm = math.sqrt(sum(value * value for value in gravity))
    gravity_g = norm / STANDARD_GRAVITY_MPS2
    if not MIN_GRAVITY_G <= gravity_g <= MAX_GRAVITY_G:
        raise PhoneTiltRejected(
            "gravity_magnitude_out_of_range",
            f"gravity must be {MIN_GRAVITY_G}-{MAX_GRAVITY_G} g (sent in m/s^2)",
            position=position,
            field="gravity",
            value=gravity_g,
        )

    flat_ratio = gravity[2] / norm
    if flat_ratio <= -MIN_FLAT_RATIO:
        raise PhoneTiltRejected(
            "gravity_sign_inverted",
            "z gravity is negative: the phone is face down or reports inverted signs",
            position=position,
            field="gravity",
            value=flat_ratio,
        )
    if flat_ratio < MIN_FLAT_RATIO:
        raise PhoneTiltRejected(
            "phone_not_flat",
            "lay the phone flat, screen up, on the case top",
            position=position,
            field="gravity",
            value=flat_ratio,
        )

    return PositionReading(
        gravity_mps2=gravity,
        gravity_g=gravity_g,
        pitch_deg=pitch_deg(gravity),
        roll_deg=roll_deg(gravity),
        pitch_std_deg=pitch_std,
        sample_count=sample_count,
    )


def _client_info(payload: Any) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise PhoneTiltRejected("invalid_payload", "client must be an object", field="client")
    info: dict = {}
    for key, value in payload.items():
        if len(info) >= _MAX_CLIENT_KEYS:
            break
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, str):
            info[key[:_MAX_CLIENT_KEY_LEN]] = value.strip()[:_MAX_CLIENT_VALUE_LEN]
        elif value is None or isinstance(value, bool):
            info[key[:_MAX_CLIENT_KEY_LEN]] = value
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            info[key[:_MAX_CLIENT_KEY_LEN]] = value
    return info


@dataclass(frozen=True)
class PhoneTiltMeasurement:  # pylint: disable=too-many-instance-attributes
    """A validated two-position measurement and the radar tilt it implies."""

    position_a: PositionReading
    position_b: PositionReading
    convention: str
    case_tilt_deg: float
    phone_bias_deg: float
    case_roll_deg: float
    phone_roll_bias_deg: float
    case_top_to_boresight_deg: float
    radar_tilt_deg: float
    measured_at: str
    client: dict

    def to_dict(self) -> dict:
        """Rounded JSON form."""
        return {
            "convention": self.convention,
            "case_tilt_deg": round(self.case_tilt_deg, 4),
            "phone_bias_deg": round(self.phone_bias_deg, 4),
            "case_roll_deg": round(self.case_roll_deg, 4),
            "phone_roll_bias_deg": round(self.phone_roll_bias_deg, 4),
            "case_top_to_boresight_deg": self.case_top_to_boresight_deg,
            "radar_tilt_deg": round(self.radar_tilt_deg, 4),
            "measured_at": self.measured_at,
            "client": dict(self.client),
            "position_a": self.position_a.to_dict(),
            "position_b": self.position_b.to_dict(),
        }


def measure_phone_tilt(
    payload: Any, *, case_top_to_boresight_deg: float = CASE_TOP_TO_BORESIGHT_DEG
) -> PhoneTiltMeasurement:
    """Validate a POST body and compute case tilt and radar tilt.

    Raises ``PhoneTiltRejected`` with a stable ``reason`` code on any problem.
    """
    if not isinstance(payload, dict):
        raise PhoneTiltRejected("invalid_payload", "JSON body must be an object")

    convention = payload.get("convention", DEFAULT_CONVENTION)
    if convention not in CONVENTIONS:
        raise PhoneTiltRejected(
            "invalid_payload",
            f"convention must be one of {', '.join(CONVENTIONS)}",
            field="convention",
        )

    measured_at = payload.get("measured_at")
    if not isinstance(measured_at, str) or not measured_at.strip():
        raise PhoneTiltRejected(
            "invalid_payload", "measured_at must be an ISO-8601 timestamp", field="measured_at"
        )
    client = _client_info(payload.get("client"))

    position_a = parse_position(payload.get("position_a"), "a", convention)
    position_b = parse_position(payload.get("position_b"), "b", convention)

    case_tilt = (position_a.pitch_deg - position_b.pitch_deg) / 2.0
    phone_bias = (position_a.pitch_deg + position_b.pitch_deg) / 2.0
    case_roll = (position_a.roll_deg - position_b.roll_deg) / 2.0
    phone_roll_bias = (position_a.roll_deg + position_b.roll_deg) / 2.0

    if abs(phone_bias) > MAX_PHONE_BIAS_DEG:
        raise PhoneTiltRejected(
            "phone_bias_out_of_range",
            "A and B pitches do not mirror each other; turn the phone exactly 180 deg "
            "between the positions",
            field="phone_bias_deg",
            value=phone_bias,
        )
    if abs(phone_roll_bias) > MAX_PHONE_ROLL_BIAS_DEG:
        raise PhoneTiltRejected(
            "positions_not_opposite",
            "A and B side rolls do not mirror each other; keep the phone's long side "
            "along the shot direction and turn it exactly 180 deg",
            field="phone_roll_bias_deg",
            value=phone_roll_bias,
        )
    if abs(case_tilt) > MAX_CASE_TILT_DEG:
        raise PhoneTiltRejected(
            "case_tilt_out_of_range",
            f"case tilt is above {MAX_CASE_TILT_DEG} deg; level the tripod first",
            field="case_tilt_deg",
            value=case_tilt,
        )

    return PhoneTiltMeasurement(
        position_a=position_a,
        position_b=position_b,
        convention=convention,
        case_tilt_deg=case_tilt,
        phone_bias_deg=phone_bias,
        case_roll_deg=case_roll,
        phone_roll_bias_deg=phone_roll_bias,
        case_top_to_boresight_deg=float(case_top_to_boresight_deg),
        radar_tilt_deg=float(case_top_to_boresight_deg) + case_tilt,
        measured_at=measured_at.strip()[:_MAX_MEASURED_AT_LEN],
        client=client,
    )


def build_record(
    payload: dict,
    measurement: PhoneTiltMeasurement,
    *,
    inclinometer_pitch_deg: float | None,
    applied_at: str,
) -> dict:
    """The saved and reported calibration record.

    ``request`` keeps the validated inputs exactly as sent so a load can
    recompute and verify the result.
    """
    configured = configured_tilt_deg(measurement.radar_tilt_deg, inclinometer_pitch_deg)
    record = {
        "schema_version": SCHEMA_VERSION,
        "source": "phone",
        "configured_iwr_tilt_deg": round(configured, 4),
        "inclinometer_pitch_deg": (
            None if inclinometer_pitch_deg is None else round(inclinometer_pitch_deg, 4)
        ),
        "inclinometer_minus_phone_deg": (
            None
            if inclinometer_pitch_deg is None
            else round(inclinometer_pitch_deg - measurement.case_tilt_deg, 4)
        ),
        "applied_at": applied_at,
        "request": {
            "convention": measurement.convention,
            "measured_at": measurement.measured_at,
            "client": dict(measurement.client),
            "position_a": _request_position(payload["position_a"]),
            "position_b": _request_position(payload["position_b"]),
        },
    }
    record.update(measurement.to_dict())
    return record


def _request_position(position: dict) -> dict:
    return {
        "gravity": [float(value) for value in position["gravity"]],
        "pitch_std_deg": float(position["pitch_std_deg"]),
        "sample_count": int(position["sample_count"]),
    }


def save_calibration(record: dict, path: Path) -> None:
    """Atomically write a calibration record (temp file, fsync, rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(record, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def load_calibration(path: Path) -> dict | None:
    """Load and re-verify a saved record; None when there is no file.

    Raises ``OSError``, ``json.JSONDecodeError`` or ``PhoneTiltRejected`` for an
    unreadable, malformed or inconsistent file.
    """
    path = Path(path)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        record = json.load(handle)
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise PhoneTiltRejected("invalid_saved_calibration", "schema_version must be 1")
    configured = _number(record.get("configured_iwr_tilt_deg"), position=None, field="configured")
    if abs(configured) > MAX_CONFIGURED_TILT_DEG:
        raise PhoneTiltRejected(
            "invalid_saved_calibration", "saved configured tilt is out of range"
        )
    boresight = _number(
        record.get("case_top_to_boresight_deg"), position=None, field="case_top_to_boresight_deg"
    )
    pitch = record.get("inclinometer_pitch_deg")
    if pitch is not None:
        pitch = _number(pitch, position=None, field="inclinometer_pitch_deg")
    measurement = measure_phone_tilt(record.get("request"), case_top_to_boresight_deg=boresight)
    if abs(configured_tilt_deg(measurement.radar_tilt_deg, pitch) - configured) > 1e-3:
        raise PhoneTiltRejected(
            "invalid_saved_calibration", "saved configured tilt does not match its measurement"
        )
    record["configured_iwr_tilt_deg"] = configured
    return record


def delete_calibration(path: Path) -> bool:
    """Remove the saved record; True when a file was removed."""
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "CASE_TOP_TO_BORESIGHT_DEG",
    "CONVENTIONS",
    "PHONE_TILT_PATH_ENV",
    "PhoneTiltMeasurement",
    "PhoneTiltRejected",
    "PositionReading",
    "build_record",
    "calibration_path",
    "configured_tilt_deg",
    "delete_calibration",
    "load_calibration",
    "measure_phone_tilt",
    "parse_position",
    "pitch_deg",
    "roll_deg",
    "save_calibration",
]
