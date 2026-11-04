"""Raspberry Pi CPU temperature and throttle flags for the session log.

Neither was logged anywhere and the Pi's journal does not persist (range
2026-11-02), so a hot or under-powered Pi could not be told apart afterwards.
Both reads are cheap (one sysfs file, one ``vcgencmd`` call) and never raise:
a missing file or command, or output that cannot be parsed, gives None.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# thermal_zone1 is disabled on this Pi (dmesg); zone 0 is the CPU.
CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
VCGENCMD_THROTTLED = ("vcgencmd", "get_throttled")
VCGENCMD_TIMEOUT_S = 1.0

# ``vcgencmd get_throttled`` bits: the low bits hold now, the bits from 16 on
# hold whether it happened since boot.
_THROTTLE_BITS = {
    "under_voltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "under_voltage_ever": 16,
    "freq_capped_ever": 17,
    "throttled_ever": 18,
}


def read_cpu_temp_c(path: Path = CPU_TEMP_PATH) -> float | None:
    """CPU temperature in degrees Celsius, None when it cannot be read."""
    try:
        return round(int(path.read_text(encoding="ascii").strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def parse_throttled(output: str) -> dict[str, Any] | None:
    """Decode ``throttled=0x50005`` into the raw value and named flags, None if malformed."""
    _key, separator, value = output.strip().partition("=")
    if not separator:
        return None
    try:
        bits = int(value.strip(), 16)
    except ValueError:
        return None
    flags: dict[str, Any] = {"raw": hex(bits)}
    flags.update({name: bool(bits >> bit & 1) for name, bit in _THROTTLE_BITS.items()})
    return flags


def _run_vcgencmd() -> str:
    return subprocess.run(
        VCGENCMD_THROTTLED,
        capture_output=True,
        check=True,
        text=True,
        timeout=VCGENCMD_TIMEOUT_S,
    ).stdout


def read_throttled(run: Callable[[], str] = _run_vcgencmd) -> dict[str, Any] | None:
    """The Pi's throttle flags, None when ``vcgencmd`` is missing or fails."""
    try:
        return parse_throttled(run())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def pi_health(
    *,
    temp_path: Path = CPU_TEMP_PATH,
    run_vcgencmd: Callable[[], str] = _run_vcgencmd,
) -> dict[str, Any]:
    """``cpu_temp_c`` and ``throttle`` for one ``power_status`` log entry. Never raises."""
    try:
        return {
            "cpu_temp_c": read_cpu_temp_c(temp_path),
            "throttle": read_throttled(run_vcgencmd),
        }
    except Exception:  # pylint: disable=broad-exception-caught
        logger.debug("[POWER] Pi health read failed", exc_info=True)
        return {"cpu_temp_c": None, "throttle": None}
