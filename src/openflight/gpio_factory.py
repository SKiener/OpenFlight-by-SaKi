"""Explicit gpiozero pin-factory selection for the Raspberry Pi 5.

gpiozero 2.0.1.post2 — the latest release as of 2026-07 — cannot auto-detect
a pin factory on a Pi 5. Its ``pins/lgpio.py`` contains::

    if chip is None:
        chip = 4 if (self._get_revision() & 0xff0) >> 4 == 0x17 \
            and os.path.exists('/dev/gpiochip4') else 0

and the module never imports ``os``. Revision type ``0x17`` is the Pi 5, and
``and`` short-circuits, so a Pi 4 never touches ``os`` and works fine. On a
Pi 5 this raises ``NameError``, gpiozero swallows it as a
``PinFactoryFallback`` warning, and the remaining backends fail for their own
reasons: RPi.GPIO isn't installed (and doesn't support the Pi 5), pigpio
isn't installed, and the native factory can't open ``/dev/gpiomem``, which
doesn't exist on a Pi 5. The result is a fatal
``BadPinFactory: Unable to load any default pin factory!``.

Setting ``GPIOZERO_PIN_FACTORY=lgpio`` does not help — that forces the same
``LGPIOFactory()`` with ``chip=None`` and hits the same ``NameError``, merely
unswallowed.

The ``if chip is None`` guard is the way out: supplying the chip explicitly
skips the broken line, so this module owns chip selection instead of
gpiozero. Remove it once upstream ships a release with the missing import.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Escape hatch for a Pi whose header GPIOs are not on the chip we pick —
# kernel updates have renumbered the RP1 banks before.
GPIO_CHIP_ENV = "OPENFLIGHT_GPIO_CHIP"

# The Pi 5 exposes the 40-pin header through the RP1 as gpiochip4; earlier
# models use gpiochip0. This mirrors gpiozero's own intent, minus the bug.
_PI5_HEADER_CHIP = 4


def detect_gpio_chip() -> int:
    """Return the gpiochip number carrying the 40-pin header.

    Raises:
        ValueError: if ``OPENFLIGHT_GPIO_CHIP`` is set but not an integer.
            A typo here would otherwise claim a line on the wrong chip and
            silently never see the trigger edge.
    """
    override = os.environ.get(GPIO_CHIP_ENV)
    if override:
        try:
            return int(override)
        except ValueError as exc:
            raise ValueError(
                f"{GPIO_CHIP_ENV}={override!r} is not an integer gpiochip number"
            ) from exc

    if os.path.exists(f"/dev/gpiochip{_PI5_HEADER_CHIP}"):
        return _PI5_HEADER_CHIP
    return 0


def _load_gpiozero() -> Tuple[type, type]:
    """Import gpiozero's Device and lgpio factory. Separated for testing."""
    from gpiozero import Device  # pylint: disable=import-outside-toplevel
    from gpiozero.pins.lgpio import (  # pylint: disable=import-outside-toplevel
        LGPIOFactory,
    )

    return Device, LGPIOFactory


def ensure_lgpio_pin_factory(chip: Optional[int] = None):
    """Install an lgpio pin factory with an explicit chip, once per process.

    Call this before constructing the first gpiozero device. Idempotent: if a
    factory is already installed (by an earlier call or by gpiozero itself)
    it is left alone, so the two trigger paths can both call it safely.

    Args:
        chip: gpiochip number to open. Defaults to ``detect_gpio_chip()``.

    Returns:
        The active pin factory.

    Raises:
        RuntimeError: if gpiozero or lgpio is missing, or the chip cannot be
            opened — both of which are actionable setup problems rather than
            something to fall back from.
    """
    try:
        device_cls, factory_cls = _load_gpiozero()
    except ImportError as exc:
        raise RuntimeError(
            f"GPIO support unavailable ({exc}). Install the Linux extras with "
            "'uv sync' on the Pi; gpiozero and lgpio are declared in pyproject.toml."
        ) from exc

    if device_cls.pin_factory is not None:
        return device_cls.pin_factory

    if chip is None:
        chip = detect_gpio_chip()

    try:
        # Passing chip explicitly is load-bearing: chip=None takes gpiozero's
        # broken Pi 5 auto-detection path. See the module docstring.
        factory = factory_cls(chip=chip)
    except Exception as exc:  # pylint: disable=broad-except
        raise RuntimeError(
            f"Could not open /dev/gpiochip{chip} ({exc}). Check that the user is "
            f"in the 'gpio' group, then override with {GPIO_CHIP_ENV} if this Pi "
            "exposes the 40-pin header on a different chip."
        ) from exc

    device_cls.pin_factory = factory
    logger.info("[GPIO] Pin factory: lgpio on /dev/gpiochip%d", chip)
    return factory
