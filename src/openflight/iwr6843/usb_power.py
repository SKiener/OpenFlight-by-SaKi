"""USB power cycle of the Raspberry Pi 5 ports, the only reset a wedged TI accepts.

The IWR6843LEVM has no host path to NRST (see :mod:`openflight.iwr6843.health`),
but a Pi 5 can cut VBUS on its USB ports with ``uhubctl``. The four ports are
ganged across hubs 2 and 4: VBUS drops only once both hubs are off, and any
"on" restores all four. Every USB device (the TI CP2105 bridge and the USB
touchscreen) therefore disconnects and re-enumerates about a second after
power returns.

The app runs as a normal user and calls ``sudo -n`` so sudo never prompts; a
sudoers drop-in must allow exactly the four ``uhubctl`` commands
(:meth:`UsbPowerCycler.permitted` checks that up front).
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
from typing import Callable

logger = logging.getLogger(__name__)

UHUBCTL = "/usr/sbin/uhubctl"
PI5_HUBS = ("2", "4")
DEFAULT_OFF_S = 3.0
DEFAULT_REENUMERATE_TIMEOUT_S = 15.0
AUTO_PORT_GLOB = "/dev/ttyUSB*"
AUTO_PORT_SETTLE_S = 1.0
POWER_OFF = 0
POWER_ON = 1
_STDERR_EXCERPT_CHARS = 200


class UsbPowerCycleError(RuntimeError):
    """A ``uhubctl`` command failed during a USB power cycle."""


class UsbPowerCycler:
    """Cut and restore VBUS on the Pi 5 USB hubs with ``uhubctl``.

    Power is always restored: the ON commands run even when an OFF command
    failed, timed out or raised, so USB (and the touchscreen) are never left
    unpowered by this class.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        *,
        hubs: tuple[str, ...] = PI5_HUBS,
        off_s: float = DEFAULT_OFF_S,
        use_sudo: bool = True,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        command_timeout_s: float = 10.0,
    ):
        self.hubs = tuple(hubs)
        self.off_s = off_s
        self.use_sudo = use_sudo
        self._runner = runner
        self._sleep = sleep
        self._clock = clock
        self.command_timeout_s = command_timeout_s

    def commands(self, action: int) -> list[list[str]]:
        """One ``uhubctl`` command per hub for ``action`` (0 = off, 1 = on)."""
        prefix = ["sudo", "-n"] if self.use_sudo else []
        return [[*prefix, UHUBCTL, "-l", hub, "-a", str(action)] for hub in self.hubs]

    def permitted(self) -> bool:
        """Whether sudo allows every ``uhubctl`` command without a password.

        Runs ``sudo -n -l <command>`` (exit 0 = allowed) for the four exact
        commands. Without sudo there is nothing to check.
        """
        if not self.use_sudo:
            return True
        for command in self.commands(POWER_OFF) + self.commands(POWER_ON):
            check = ["sudo", "-n", "-l", *command[2:]]
            try:
                result = self._runner(
                    check,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.command_timeout_s,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.debug("[IWR6843] %r failed", " ".join(check), exc_info=True)
                return False
            if result.returncode != 0:
                logger.debug(
                    "[IWR6843] sudo does not allow %r: %s",
                    " ".join(command[2:]),
                    (result.stderr or "").strip()[:_STDERR_EXCERPT_CHARS],
                )
                return False
        return True

    def cycle(self) -> float:
        """Power the hubs off, wait ``off_s``, power them on; return elapsed seconds.

        Raises :class:`UsbPowerCycleError` naming the first failing command
        once power has been restored.
        """
        started = self._clock()
        failures: list[str] = []
        logger.warning(
            "[IWR6843] Cutting USB power on hubs %s for %.1fs", ",".join(self.hubs), self.off_s
        )
        try:
            for command in self.commands(POWER_OFF):
                self._run(command, failures)
            self._sleep(self.off_s)
        finally:
            for command in self.commands(POWER_ON):
                self._run(command, failures)
        elapsed = self._clock() - started
        if failures:
            raise UsbPowerCycleError(f"USB power cycle failed: {failures[0]}")
        logger.info("[IWR6843] USB power restored after %.1fs", elapsed)
        return elapsed

    def _run(self, command: list[str], failures: list[str]) -> None:
        """Run one command, recording (never raising) any failure."""
        text = " ".join(command)
        try:
            result = self._runner(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.command_timeout_s,
            )
        except subprocess.TimeoutExpired:
            failure = f"{text!r} timed out after {self.command_timeout_s:.0f}s"
        except Exception as exc:  # pylint: disable=broad-exception-caught
            failure = f"{text!r} raised {type(exc).__name__}: {exc}"
        else:
            if result.returncode == 0:
                return
            stderr = (result.stderr or "").strip()[:_STDERR_EXCERPT_CHARS]
            failure = f"{text!r} exited {result.returncode}: {stderr}"
        logger.error("[IWR6843] %s", failure)
        failures.append(failure)


def wait_for_path(  # pylint: disable=too-many-arguments
    path: str,
    *,
    present: bool,
    timeout_s: float,
    poll_s: float = 0.1,
    exists: Callable[[str], bool] = os.path.exists,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until ``path`` exists (``present``) or is gone; False on timeout.

    The path is checked at least once, also with a zero timeout.
    """
    deadline = clock() + timeout_s
    while True:
        if exists(path) == present:
            return True
        if clock() >= deadline:
            return False
        sleep(poll_s)


def wait_for_tty(  # pylint: disable=too-many-arguments
    port: str | None,
    *,
    timeout_s: float = DEFAULT_REENUMERATE_TIMEOUT_S,
    settle_s: float = AUTO_PORT_SETTLE_S,
    exists: Callable[[str], bool] = os.path.exists,
    matches: Callable[[str], list[str]] = glob.glob,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for the TI tty after a power cycle; False on timeout.

    A path port is awaited as such. Without a port (auto-detect), any
    ``/dev/ttyUSB*`` is awaited, then ``settle_s`` lets the bridge finish
    enumerating its second interface. Other port names are not waited for.
    """
    if port is not None:
        if not port.startswith("/"):
            return True
        return wait_for_path(
            port, present=True, timeout_s=timeout_s, exists=exists, clock=clock, sleep=sleep
        )
    if not wait_for_path(
        AUTO_PORT_GLOB,
        present=True,
        timeout_s=timeout_s,
        exists=lambda pattern: bool(matches(pattern)),
        clock=clock,
        sleep=sleep,
    ):
        return False
    sleep(settle_s)
    return True


def usb_mounted_block_devices(
    *, mounts_path: str = "/proc/mounts", sys_class_block: str = "/sys/class/block"
) -> list[str]:
    """Mounted block devices that sit on USB (a power cycle would yank them)."""
    try:
        with open(mounts_path, encoding="utf-8") as handle:
            sources = [line.split(maxsplit=1)[0] for line in handle if line.strip()]
    except OSError:
        return []
    found = []
    for source in sources:
        if not source.startswith("/dev/"):
            continue
        name = os.path.basename(os.path.realpath(source))
        sysfs = os.path.join(sys_class_block, name)
        if os.path.exists(sysfs) and "/usb" in os.path.realpath(sysfs) and source not in found:
            found.append(source)
    return found


__all__ = [
    "DEFAULT_OFF_S",
    "AUTO_PORT_GLOB",
    "DEFAULT_REENUMERATE_TIMEOUT_S",
    "PI5_HUBS",
    "UHUBCTL",
    "UsbPowerCycleError",
    "UsbPowerCycler",
    "usb_mounted_block_devices",
    "wait_for_path",
    "wait_for_tty",
]
