"""Host side of the OPS243-A firmware 1.3.3 speed trigger (B2, ``--shot-trigger ops-speed*``).

Firmware 1.3.3 can trip its own rolling buffer: an outbound reading faster
than ``ST`` (mph) with at least ``SM`` magnitude freezes the buffer around the
trip (``S#n`` split) and drives J3 pin 1 HIGH "until the Rolling Buffer
sampling has completed" (vendor README). Pin 1 is wired to a Pi input (GPIO22,
header pin 15). No microphone edge and no HOST_INT pulse are needed.

Two shot-trigger modes, both off by default (``--shot-trigger sound`` stays):

* ``ops-speed-shadow`` (M3): the microphone still decides. A dump counts only
  with a microphone edge ``witness_window_ms`` after its pin edge; a
  microphone edge without a pin edge falls back to a HOST_INT pulse.
* ``ops-speed`` (M4): the pin edge drives TI, camera and the gate record; the
  microphone is only a witness.

This module holds what needs no server: the flags (``OpsSpeedConfig``), the
firmware gate (``speed_trigger_supported``), the pin input (``PinEdgeInput``,
built by ``make_pin_input`` / ``pin_button_factory``), the microphone witness
(``OpsSpeedWitness``) and the practice-swing verdict. The OPS commands live in
``ops243`` (``prepare_internal_speed_trigger``).

Timing defaults are model values, not measurements: the TI ``l3dump`` delay
(25 ms) and the club-speed anchor (28 ms after the pin edge) come from the
2026-10-03 shots at transmit power P0 and 30 ksps (analysis
trigger-b2-code-20261103, ``b2_timing.py``), while B2 runs at P3 and 20 ksps.
Recalibrate both in M3 from the logged mic-minus-pin delay (``matched``
witness records, ``delta_ms``).
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable

from openflight import ops243
from openflight.edge_timing import edge_ticks
from openflight.gpio_factory import ensure_lgpio_pin_factory
from openflight.ops243 import (
    SPEED_TRIGGER_MAGNITUDE_RANGE,
    SPEED_TRIGGER_THRESHOLD_RANGE_MPH,
    parse_firmware_version,
    speed_trigger_firmware_problem,
)

logger = logging.getLogger(__name__)

OPS_SPEED_SHADOW = "ops-speed-shadow"
OPS_SPEED = "ops-speed"
OPS_SPEED_SHOT_TRIGGERS = (OPS_SPEED_SHADOW, OPS_SPEED)

PIN_ACTIVE_LEVELS = ("high", "low")
PIN_PULLS = ("down", "up", "off")
# BCM inputs on the 40-pin header.
PIN_RANGE = (0, 27)
DEFAULT_PIN_ACTIVE = "high"
# 0 fans an edge out in the GPIO callback itself. A gpiozero bounce_time is
# never used: lgpio delays delivery by it (iwr6843/monitor.py _open_button).
DEFAULT_DEBOUNCE_MS = 0.0
MAX_DEBOUNCE_MS = 5.0
# Same as the TI monitor's 100 ms ringing filter.
DEFAULT_HOLDOFF_MS = 100.0
MAX_HOLDOFF_MS = 1000.0
DEFAULT_THRESHOLD_MPH = 25
# S#2 at 20 ksps: 12.8 ms before / 192.0 ms after the trip.
DEFAULT_PRE_TRIGGER_SEGMENTS = 2
# ops-speed only: l3dump this long after the pin edge (model value, see above).
DEFAULT_TI_DELAY_MS = 25.0
MAX_TI_DELAY_MS = 40.0
# Club-speed anchor this long after the pin edge (model value, see above).
DEFAULT_ANCHOR_MS = 28.0
MAX_ANCHOR_MS = 100.0
# Microphone edge minus pin edge that counts as the same swing.
DEFAULT_WITNESS_WINDOW_MS = (0.0, 60.0)
WITNESS_WINDOW_BOUNDS_MS = (-100.0, 200.0)
# ops-speed: re-arm the OPS once after this many consecutive microphone edges
# without a pin edge while the OPS was armed (stuck idle; 0 turns it off).
DEFAULT_STALL_MIC_EDGES = 3
MAX_STALL_MIC_EDGES = 50

OPS_STATE_ARMED = "armed"
OPS_STATE_BUSY = "busy"

WITNESS_KINDS = ("matched", "pin_without_mic", "mic_without_pin", "glitch")
# A microphone edge is decided this long after it: a pin edge 0-60 ms before
# it may still be on its way through the debounce (<= 5 ms) and the callbacks.
WITNESS_MIC_GRACE_MS = 10.0
# A pin edge waits for its release (HIGH width ~192 ms at S#2/20 ksps) at most
# this long before it is decided without one.
WITNESS_RELEASE_WAIT_MS = 1000.0
WITNESS_FLUSH_INTERVAL_S = 0.05
WITNESS_HISTORY = 64
MATCH_TOLERANCE_S = 1e-6

# Practice-swing rule (section 9 of the B2 brief): no club-to-ball jump in the
# OPS data and a smash below this. Logged always; rejects only with
# --ops-speed-reject-practice-swings, because on 197 real shots (19.10.,
# 30.10., 02.11.) it already drops 2.
PRACTICE_SWING_MAX_SMASH = 1.05
PRACTICE_SWING_VERDICTS = ("practice_swing", "shot", "unknown")


class OpsSpeedPinError(RuntimeError):
    """The pin-1 input cannot be used (active at idle, wrong polarity or pull)."""


def _check_number(name: str, value, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be {low:g}-{high:g}, got {value!r}")
    return float(value)


def _check_int(name: str, value, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer {low}-{high}, got {value!r}")
    return value


def parse_window_ms(text) -> tuple[float, float]:
    """``"0,60"`` (or a pair) -> ``(0.0, 60.0)``: microphone minus pin edge, in ms."""
    if isinstance(text, str):
        parts = text.split(",")
        if len(parts) != 2:
            raise ValueError(f"witness window {text!r}: expected LOW,HIGH in ms, e.g. 0,60")
        try:
            values = [float(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"witness window {text!r} is not two numbers") from exc
    else:
        values = list(text)
        if len(values) != 2:
            raise ValueError(f"witness window {text!r}: expected two values")
    bound_low, bound_high = WITNESS_WINDOW_BOUNDS_MS
    low = _check_number("witness window start", values[0], bound_low, bound_high)
    high = _check_number("witness window end", values[1], bound_low, bound_high)
    if low > high:
        raise ValueError(f"witness window start {low:g} ms is after its end {high:g} ms")
    return low, high


def resolve_pin_pull(active: str, pull: str | None = None) -> str:
    """The pull for ``active``: ``down`` for high, ``up`` for low, or the explicit one.

    ``off`` suits a push-pull driver. A pull towards the active level (high +
    up, low + down) would read the pin active at idle and is refused.
    """
    if active not in PIN_ACTIVE_LEVELS:
        raise ValueError(f"pin active level must be one of {PIN_ACTIVE_LEVELS}, got {active!r}")
    if pull is None:
        return "down" if active == "high" else "up"
    if pull not in PIN_PULLS:
        raise ValueError(f"pin pull must be one of {PIN_PULLS}, got {pull!r}")
    if (active, pull) in (("high", "up"), ("low", "down")):
        raise ValueError(
            f"pull {pull} with an active-{active} pin holds it active at idle; "
            f"use {'down' if active == 'high' else 'up'} or off"
        )
    return pull


def button_kwargs(active: str, pull: str | None = None) -> dict:
    """gpiozero ``Button`` keyword arguments for the polarity and pull (no bounce_time)."""
    pull = resolve_pin_pull(active, pull)
    if pull == "off":
        return {"pull_up": None, "active_state": active == "high", "bounce_time": None}
    return {"pull_up": pull == "up", "bounce_time": None}


@dataclass(frozen=True)
class OpsSpeedConfig:  # pylint: disable=too-many-instance-attributes
    """The ``--shot-trigger ops-speed*`` / ``--ops-speed-*`` flags, validated."""

    mode: str
    pin: int
    magnitude: int
    pin_active: str = DEFAULT_PIN_ACTIVE
    pin_pull: str | None = None
    debounce_ms: float = DEFAULT_DEBOUNCE_MS
    holdoff_ms: float = DEFAULT_HOLDOFF_MS
    threshold_mph: int = DEFAULT_THRESHOLD_MPH
    pre_trigger_segments: int = DEFAULT_PRE_TRIGGER_SEGMENTS
    ti_delay_ms: float = DEFAULT_TI_DELAY_MS
    anchor_ms: float = DEFAULT_ANCHOR_MS
    witness_window_ms: tuple[float, float] = DEFAULT_WITNESS_WINDOW_MS
    reject_practice_swings: bool = False
    stall_mic_edges: int = DEFAULT_STALL_MIC_EDGES

    def __post_init__(self) -> None:
        if self.mode not in OPS_SPEED_SHOT_TRIGGERS:
            raise ValueError(f"ops-speed mode must be one of {OPS_SPEED_SHOT_TRIGGERS}")
        _check_int("ops-speed pin", self.pin, *PIN_RANGE)
        _check_int("ops-speed magnitude", self.magnitude, *SPEED_TRIGGER_MAGNITUDE_RANGE)
        _check_int("ops-speed threshold", self.threshold_mph, *SPEED_TRIGGER_THRESHOLD_RANGE_MPH)
        _check_int("ops-speed pre-trigger", self.pre_trigger_segments, 0, 32)
        _check_int("ops-speed stall mic edges", self.stall_mic_edges, 0, MAX_STALL_MIC_EDGES)
        # Frozen: normalised values go in through object.__setattr__.
        normalised = {
            "pin_pull": resolve_pin_pull(self.pin_active, self.pin_pull),
            "debounce_ms": _check_number(
                "ops-speed debounce", self.debounce_ms, 0, MAX_DEBOUNCE_MS
            ),
            "holdoff_ms": _check_number("ops-speed hold-off", self.holdoff_ms, 0, MAX_HOLDOFF_MS),
            "ti_delay_ms": _check_number(
                "ops-speed TI delay", self.ti_delay_ms, 0, MAX_TI_DELAY_MS
            ),
            "anchor_ms": _check_number("ops-speed anchor", self.anchor_ms, 0, MAX_ANCHOR_MS),
            "witness_window_ms": parse_window_ms(self.witness_window_ms),
            "reject_practice_swings": bool(self.reject_practice_swings),
        }
        for name, value in normalised.items():
            object.__setattr__(self, name, value)

    @property
    def shadow(self) -> bool:
        """``ops-speed-shadow``: the microphone still decides."""
        return self.mode == OPS_SPEED_SHADOW

    @classmethod
    def from_args(cls, args) -> OpsSpeedConfig:
        """From the server's argparse namespace; unset (None) flags keep the defaults."""
        values = {
            "mode": args.shot_trigger,
            "pin": args.ops_speed_pin,
            "magnitude": args.ops_speed_magnitude,
        }
        optional = {
            "pin_active": "ops_speed_pin_active",
            "pin_pull": "ops_speed_pin_pull",
            "debounce_ms": "ops_speed_debounce_ms",
            "holdoff_ms": "ops_speed_holdoff_ms",
            "threshold_mph": "ops_speed_threshold_mph",
            "pre_trigger_segments": "ops_speed_pre_trigger",
            "ti_delay_ms": "ops_speed_ti_delay_ms",
            "anchor_ms": "ops_speed_anchor_ms",
            "witness_window_ms": "ops_speed_witness_window_ms",
            "reject_practice_swings": "ops_speed_reject_practice_swings",
            "stall_mic_edges": "ops_speed_stall_mic_edges",
        }
        for name, dest in optional.items():
            value = getattr(args, dest, None)
            if value is not None:
                values[name] = value
        return cls(**values)

    def as_dict(self) -> dict:
        """Session-start config form (``config.ops_speed``)."""
        fields = asdict(self)
        fields["witness_window_ms"] = list(self.witness_window_ms)
        fields["shadow"] = self.shadow
        return fields


def speed_trigger_supported(
    version: str | None, *, off_commands: tuple[str, ...] | None = None
) -> tuple[bool, str | None]:
    """``(True, None)`` when firmware ``version`` may run the speed trigger, else ``(False, why)``.

    Refused: unknown or malformed version, below 1.3.3, outside the 1.3 train,
    or no known off command (``ops243.SPEED_TRIGGER_OFF_COMMANDS``, filled in
    after M1). ``off_commands`` overrides that constant (tests, M1).
    """
    problem = speed_trigger_firmware_problem(version)
    if problem is not None:
        return False, problem
    if off_commands is None:
        # Read at call time, so the constant can be filled in (or patched) later.
        off_commands = ops243.SPEED_TRIGGER_OFF_COMMANDS
    if not off_commands:
        return False, (
            "the speed-trigger off command is unknown (ops243.SPEED_TRIGGER_OFF_COMMANDS "
            "is empty until M1)"
        )
    return True, None


def pin_idle_message(pin: int, active: str, pull: str) -> str:
    """Refusal text for a pin that reads active while the OPS is idle."""
    return (
        f"BCM{pin} (J3 pin 1) reads active at idle (active {active}, pull {pull}): wrong "
        "polarity, wrong pull or a stuck pin"
    )


def _start_timer(delay_s: float, callback: Callable[[], None]):
    timer = threading.Timer(delay_s, callback)
    timer.daemon = True
    timer.start()
    return timer


@dataclass(frozen=True)
class _Rise:
    wall_time: float
    monotonic_ns: int
    ticks: float | None


class PinEdgeInput:  # pylint: disable=too-many-instance-attributes
    """J3 pin 1 on a Pi input, with the surface the TI monitor and EdgeListener use.

    The consumer sets ``when_pressed``; it is called with the wall time of the
    active edge, read in the GPIO callback, also when the edge is delivered
    ``debounce_ms`` later. ``_last_changed`` holds that edge's kernel ticks
    while ``when_pressed`` runs (``edge_timing.edge_ticks``).

    * hold-off: an edge within ``holdoff_ms`` of the last accepted one is
      dropped (counted);
    * debounce: with ``debounce_ms > 0`` an edge is fanned out only if the pin
      is still active after that time; a release before it is a glitch,
      reported to ``when_glitch(rise, release, high_ms)``;
    * release: ``when_released(rise, release, high_ms)`` reports the HIGH
      width of an accepted edge.

    Thread safe: device callbacks arrive on the GPIO thread, confirmations on
    a timer thread. The consumer callbacks run outside the lock.
    """

    bounce_time = None

    def __init__(  # pylint: disable=too-many-arguments
        self,
        device,
        *,
        pin: int,
        debounce_ms: float = DEFAULT_DEBOUNCE_MS,
        holdoff_ms: float = DEFAULT_HOLDOFF_MS,
        on_release: Callable[[float, float, float], None] | None = None,
        on_glitch: Callable[[float, float, float], None] | None = None,
        schedule: Callable[[float, Callable[[], None]], object] | None = None,
        wall_time: Callable[[], float] = time.time,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.pin = pin
        self.debounce_ms = _check_number("debounce", debounce_ms, 0, MAX_DEBOUNCE_MS)
        self.holdoff_ms = _check_number("hold-off", holdoff_ms, 0, MAX_HOLDOFF_MS)
        # Set by the consumer (TI monitor, EdgeListener); called with the edge's wall time.
        self.when_pressed = None
        self.when_released = on_release
        self.when_glitch = on_glitch
        self.closed = False
        self._device = device
        self._schedule = schedule or _start_timer
        self._wall_time = wall_time
        self._monotonic_ns = monotonic_ns
        self._lock = threading.Lock()
        self._pending: _Rise | None = None
        self._pending_timer = None
        self._open: _Rise | None = None
        self._last_accepted_ns: int | None = None
        self._dispatch_ticks: float | None = None
        self._counts = {
            "accepted": 0,
            "holdoff_dropped": 0,
            "glitches": 0,
            "releases": 0,
            "stray_releases": 0,
            "missed_releases": 0,
        }
        device.when_pressed = self._on_device_pressed
        device.when_released = self._on_device_released

    @property
    def is_active(self) -> bool:
        """The pin's logical level (after the active-state mapping)."""
        return bool(self._device.is_active)

    @property
    def _last_changed(self) -> float | None:
        """Kernel ticks of the edge being dispatched to ``when_pressed``."""
        return self._dispatch_ticks

    def stats(self) -> dict:
        """Edge counts so far."""
        with self._lock:
            return dict(self._counts)

    def _on_device_pressed(self) -> None:
        rise = _Rise(self._wall_time(), self._monotonic_ns(), edge_ticks(self._device))
        with self._lock:
            if self.closed or self._pending is not None:
                # The debounce of the last rise still runs: not a new edge.
                return
            if self._open is not None:
                # Its release never reached us; never let that silence the pin.
                self._open = None
                self._counts["missed_releases"] += 1
            holdoff_ns = self.holdoff_ms * 1e6
            if (
                self._last_accepted_ns is not None
                and rise.monotonic_ns - self._last_accepted_ns < holdoff_ns
            ):
                self._counts["holdoff_dropped"] += 1
                return
            if self.debounce_ms > 0:
                self._pending = rise
                self._pending_timer = self._schedule(self.debounce_ms / 1000.0, self._confirm)
                return
            self._accept_locked(rise)
        self._deliver(rise)

    def _accept_locked(self, rise: _Rise) -> None:
        self._open = rise
        self._last_accepted_ns = rise.monotonic_ns
        self._counts["accepted"] += 1

    def _confirm(self) -> None:
        with self._lock:
            rise = self._pending
            if rise is None or self.closed:
                return
            self._pending = None
            self._pending_timer = None
            still_active = bool(self._device.is_active)
            if still_active:
                self._accept_locked(rise)
            else:
                self._counts["glitches"] += 1
        if still_active:
            self._deliver(rise)
        else:
            # Released without a callback reaching us first: still a glitch.
            self._report(self.when_glitch, rise, self._wall_time(), self._monotonic_ns())

    def _deliver(self, rise: _Rise) -> None:
        callback = self.when_pressed
        if callback is None:
            return
        self._dispatch_ticks = rise.ticks
        callback(rise.wall_time)  # pylint: disable=not-callable  # set by the consumer

    def _on_device_released(self) -> None:
        release_wall = self._wall_time()
        release_ns = self._monotonic_ns()
        with self._lock:
            if self.closed:
                return
            if self._pending is not None:
                rise, self._pending = self._pending, None
                timer, self._pending_timer = self._pending_timer, None
                if timer is not None:
                    timer.cancel()
                self._counts["glitches"] += 1
                callback = self.when_glitch
            elif self._open is not None:
                rise, self._open = self._open, None
                self._counts["releases"] += 1
                callback = self.when_released
            else:
                self._counts["stray_releases"] += 1
                return
        self._report(callback, rise, release_wall, release_ns)

    @staticmethod
    def _report(callback, rise: _Rise, release_wall: float, release_ns: int) -> None:
        if callback is None:
            return
        high_ms = (release_ns - rise.monotonic_ns) / 1e6
        try:
            callback(rise.wall_time, release_wall, high_ms)
        except Exception:
            logger.warning("[OPS-SPEED] Pin release callback failed", exc_info=True)

    def close(self) -> None:
        """Release the GPIO line; idempotent."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            timer, self._pending_timer = self._pending_timer, None
            self._pending = None
        if timer is not None:
            timer.cancel()
        self._device.when_pressed = None
        self._device.when_released = None
        self._device.close()


def make_pin_input(  # pylint: disable=too-many-arguments
    pin: int,
    *,
    active: str = DEFAULT_PIN_ACTIVE,
    pull: str | None = None,
    debounce_ms: float = DEFAULT_DEBOUNCE_MS,
    holdoff_ms: float = DEFAULT_HOLDOFF_MS,
    factory: Callable | None = None,
    on_release: Callable[[float, float, float], None] | None = None,
    on_glitch: Callable[[float, float, float], None] | None = None,
    schedule: Callable[[float, Callable[[], None]], object] | None = None,
) -> PinEdgeInput:
    """Claim BCM ``pin`` for pin 1 and check its idle level.

    ``factory`` builds the device like gpiozero's ``Button`` (default: lgpio
    via ``ensure_lgpio_pin_factory``), with ``bounce_time=None``. Raises
    OpsSpeedPinError, with the line released again, when the pin reads
    active at idle.
    """
    _check_int("pin", pin, *PIN_RANGE)
    pull = resolve_pin_pull(active, pull)
    if factory is None:
        # Must precede the first gpiozero device: on a Pi 5 gpiozero's own
        # auto-detection fails outright. See gpio_factory.
        ensure_lgpio_pin_factory()
        from gpiozero import Button  # pylint: disable=import-error

        factory = Button
    device = factory(pin, **button_kwargs(active, pull))
    try:
        pin_input = PinEdgeInput(
            device,
            pin=pin,
            debounce_ms=debounce_ms,
            holdoff_ms=holdoff_ms,
            on_release=on_release,
            on_glitch=on_glitch,
            schedule=schedule,
        )
    except Exception:
        device.close()
        raise
    if pin_input.is_active:
        pin_input.close()
        raise OpsSpeedPinError(pin_idle_message(pin, active, pull))
    logger.info(
        "[OPS-SPEED] Pin 1 on BCM%d (active %s, pull %s, debounce %g ms, hold-off %g ms)",
        pin,
        active,
        pull,
        pin_input.debounce_ms,
        pin_input.holdoff_ms,
    )
    return pin_input


def pin_button_factory(pin: int, **kwargs) -> Callable[..., PinEdgeInput]:
    """A ``button_factory`` for the TI monitor or ``EdgeListener`` that builds pin-1 inputs.

    They call ``factory(pin, pull_up=False, bounce_time=None)``; both keywords
    are ignored (polarity and pull come from ``kwargs``, as for
    ``make_pin_input``). Each call claims the pin afresh, so a consumer that
    restarts gets a new input.
    """

    def factory(gpio_pin: int, pull_up=False, bounce_time=None) -> PinEdgeInput:
        del pull_up, bounce_time  # the microphone defaults; pin 1 has its own
        if gpio_pin != pin:
            raise ValueError(f"pin-1 factory built for BCM{pin}, asked for BCM{gpio_pin}")
        return make_pin_input(pin, **kwargs)

    return factory


def pin_idle_problem(
    pin: int,
    *,
    active: str = DEFAULT_PIN_ACTIVE,
    pull: str | None = None,
    factory: Callable | None = None,
) -> str | None:
    """Claim the pin once, read its idle level, release it: a refusal reason or None."""
    try:
        make_pin_input(pin, active=active, pull=pull, factory=factory).close()
    except OpsSpeedPinError as exc:
        return str(exc)
    except Exception as exc:  # GPIO missing, line busy, bad polarity/pull: all refusals
        return f"BCM{pin} (J3 pin 1) could not be read: {exc}"
    return None


@dataclass
class _WitnessPin:
    wall_time: float
    monotonic_ns: int
    high_ms: float | None = None
    release_timestamp: float | None = None
    mic: _WitnessMic | None = None
    kind: str | None = None


@dataclass
class _WitnessMic:
    wall_time: float
    monotonic_ns: int
    ops_state: str | None
    fallback_pulse: bool | None = None
    done: bool = False
    pin: _WitnessPin | None = None


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    deciles = statistics.quantiles(ordered, n=10) if len(ordered) > 1 else [ordered[0]] * 9
    return {
        "n": len(ordered),
        "median": round(statistics.median(ordered), 3),
        "p10": round(deciles[0], 3),
        "p90": round(deciles[-1], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


class OpsSpeedWitness:  # pylint: disable=too-many-instance-attributes
    """Pair pin-1 edges with microphone edges on the monotonic clock.

    ``sink(fields)`` receives one dict per outcome, ``kind``:

    * ``matched``: a microphone edge ``window_ms`` (microphone minus pin)
      after a pin edge; ``delta_ms`` is that delay (what M3 recalibrates the
      TI delay and the anchor from), ``pin_high_ms`` the HIGH width;
    * ``pin_without_mic``: a neighbour, a practice swing, or a shot the
      microphone missed;
    * ``mic_without_pin``: ``ops_state`` ``armed`` is a would-be B2 miss,
      ``busy`` an edge while the OPS was blind; ``fallback_pulse`` says
      whether shadow mode pulsed HOST_INT for it;
    * ``glitch``: pin 1 released before the debounce confirmed it.

    A microphone edge is decided ``WITNESS_MIC_GRACE_MS`` after it, a pin edge
    once its window has closed and it was released (or
    ``WITNESS_RELEASE_WAIT_MS`` passed). ``note_*`` only record (they run on
    GPIO callback threads); ``flush`` decides and calls the sink, from
    ``start``'s thread, from the caller, or from the lookups.

    Stuck-idle OPS (``ops-speed``): after ``stall_mic_edges`` consecutive
    ``mic_without_pin`` records with ``ops_state`` ``armed`` the witness calls
    ``on_stall(fields)`` once and counts again from zero; any pin edge resets
    the count.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        sink: Callable[[dict], None],
        *,
        window_ms: tuple[float, float] = DEFAULT_WITNESS_WINDOW_MS,
        ops_state: Callable[[], str] | None = None,
        stall_mic_edges: int = DEFAULT_STALL_MIC_EDGES,
        on_stall: Callable[[dict], None] | None = None,
        wall_time: Callable[[], float] = time.time,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ):
        low, high = parse_window_ms(window_ms)
        self.window_ms = (low, high)
        self._low_ns = round(low * 1e6)
        self._high_ns = round(high * 1e6)
        # A pin edge up to -low ms after the microphone edge still pairs with it.
        self._mic_grace_ns = round((max(0.0, -low) + WITNESS_MIC_GRACE_MS) * 1e6)
        self._release_wait_ns = round(WITNESS_RELEASE_WAIT_MS * 1e6)
        self._sink = sink
        self._ops_state = ops_state
        self._stall_mic_edges = _check_int(
            "stall mic edges", stall_mic_edges, 0, MAX_STALL_MIC_EDGES
        )
        self._on_stall = on_stall
        self._wall_time = wall_time
        self._now_ns = monotonic_ns
        self._lock = threading.Lock()
        self._pins: list[_WitnessPin] = []
        self._mics: list[_WitnessMic] = []
        self._glitches: list[dict] = []
        self._decided_pins: deque[_WitnessPin] = deque(maxlen=WITNESS_HISTORY)
        self._armed_streak = 0
        self._counts = dict.fromkeys(WITNESS_KINDS, 0)
        self._counts.update(mic_without_pin_armed=0, mic_without_pin_busy=0, stall=0)
        self._deltas_ms: list[float] = []
        self._high_ms: list[float] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- recording (GPIO callback threads) ---------------------------------

    def _monotonic_for(self, wall_time: float) -> int:
        return self._now_ns() - round((self._wall_time() - wall_time) * 1e9)

    def note_pin_edge(self, wall_time: float, monotonic_ns: int | None = None) -> None:
        """A pin-1 edge was fanned out (usable directly as an edge observer)."""
        mono = self._monotonic_for(wall_time) if monotonic_ns is None else int(monotonic_ns)
        with self._lock:
            self._pins.append(_WitnessPin(float(wall_time), mono))
            self._armed_streak = 0

    def note_pin_release(self, rise_timestamp: float, release_timestamp: float, high_ms: float):
        """Pin 1 fell again: the HIGH width of the edge stamped ``rise_timestamp``."""
        with self._lock:
            pin = self._find_pin_locked(rise_timestamp)
            if pin is not None:
                pin.high_ms = round(float(high_ms), 3)
                pin.release_timestamp = float(release_timestamp)

    def note_pin_glitch(self, rise_timestamp: float, release_timestamp: float, high_ms: float):
        """Pin 1 fell before the debounce confirmed it; never fanned out."""
        with self._lock:
            self._glitches.append(
                {
                    "kind": "glitch",
                    "pin_timestamp": float(rise_timestamp),
                    "release_timestamp": float(release_timestamp),
                    "pin_high_ms": round(float(high_ms), 3),
                    **self._window_fields(),
                }
            )

    def note_mic_edge(
        self,
        wall_time: float,
        *,
        ops_state: str | None = None,
        fallback_pulse: bool | None = None,
        monotonic_ns: int | None = None,
    ) -> None:
        """A microphone edge passed the ringing filter (usable directly as an edge observer).

        ``ops_state`` defaults to the ``ops_state`` provider's answer.
        """
        if ops_state is None and self._ops_state is not None:
            try:
                ops_state = self._ops_state()
            except Exception:
                logger.warning("[OPS-SPEED] OPS state provider failed", exc_info=True)
        mono = self._monotonic_for(wall_time) if monotonic_ns is None else int(monotonic_ns)
        with self._lock:
            self._mics.append(_WitnessMic(float(wall_time), mono, ops_state, fallback_pulse))

    def note_fallback_pulse(self, wall_time: float, pulsed: bool = True) -> None:
        """Shadow mode pulsed HOST_INT (or not) for the microphone edge ``wall_time``."""
        with self._lock:
            for mic in self._mics:
                if abs(mic.wall_time - wall_time) <= MATCH_TOLERANCE_S:
                    mic.fallback_pulse = bool(pulsed)

    # --- deciding -----------------------------------------------------------

    def flush(self, now_ns: int | None = None, *, everything: bool = False) -> None:
        """Decide every edge whose wait is over and hand the outcomes to the sink."""
        now = self._now_ns() if now_ns is None else now_ns
        emitted: list[dict] = []
        stalls: list[dict] = []
        with self._lock:
            emitted.extend(self._glitches)
            self._glitches = []
            for mic in self._mics:
                if mic.done or (not everything and now < mic.monotonic_ns + self._mic_grace_ns):
                    continue
                mic.done = True
                if self._claim_pin_locked(mic) is None:
                    fields = self._mic_fields_locked(mic)
                    emitted.append(fields)
                    stall = self._count_streak_locked(mic)
                    if stall is not None:
                        stalls.append(stall)
            keep: list[_WitnessPin] = []
            for pin in self._pins:
                if everything or self._pin_decidable(pin, now):
                    emitted.append(self._decide_pin_locked(pin))
                else:
                    keep.append(pin)
            self._pins = keep
            horizon_ns = self._high_ns + self._release_wait_ns
            self._mics = [
                mic for mic in self._mics if not mic.done or now - mic.monotonic_ns < horizon_ns
            ]
            for fields in emitted:
                self._counts[fields["kind"]] += 1
            self._counts["stall"] += len(stalls)
        for fields in emitted:
            self._emit(self._sink, fields)
        for stall in stalls:
            logger.warning(
                "[OPS-SPEED] %d microphone edges in a row without a pin edge while the OPS "
                "was armed: OPS stuck idle?",
                stall["mic_edges"],
            )
            if self._on_stall is not None:
                self._emit(self._on_stall, stall)

    @staticmethod
    def _emit(callback: Callable[[dict], None], fields: dict) -> None:
        try:
            callback(fields)
        except Exception:
            logger.warning("[OPS-SPEED] Witness sink failed", exc_info=True)

    def _pin_decidable(self, pin: _WitnessPin, now: int) -> bool:
        if now < pin.monotonic_ns + max(self._high_ns, 0) + self._mic_grace_ns:
            return False
        return pin.high_ms is not None or now >= pin.monotonic_ns + self._release_wait_ns

    def _claim_pin_locked(self, mic: _WitnessMic) -> _WitnessPin | None:
        candidates = [
            pin
            for pin in self._pins
            if pin.mic is None
            and self._low_ns <= mic.monotonic_ns - pin.monotonic_ns <= self._high_ns
        ]
        if not candidates:
            return None
        pin = min(candidates, key=lambda item: abs(mic.monotonic_ns - item.monotonic_ns))
        pin.mic = mic
        mic.pin = pin
        return pin

    def _count_streak_locked(self, mic: _WitnessMic) -> dict | None:
        if mic.ops_state != OPS_STATE_ARMED:
            self._armed_streak = 0
            return None
        self._armed_streak += 1
        if not self._stall_mic_edges or self._armed_streak < self._stall_mic_edges:
            return None
        self._armed_streak = 0
        return {
            "kind": "stall",
            "mic_edges": self._stall_mic_edges,
            "mic_timestamp": mic.wall_time,
            "mic_monotonic_ns": mic.monotonic_ns,
        }

    def _decide_pin_locked(self, pin: _WitnessPin) -> dict:
        mic = pin.mic
        pin.kind = "matched" if mic is not None else "pin_without_mic"
        fields = {
            "kind": pin.kind,
            "pin_timestamp": pin.wall_time,
            "pin_monotonic_ns": pin.monotonic_ns,
            "pin_high_ms": pin.high_ms,
            "pin_released": pin.high_ms is not None,
            **self._window_fields(),
        }
        if pin.high_ms is not None:
            self._high_ms.append(pin.high_ms)
        if mic is not None:
            delta_ms = self._delta_ms(mic, pin)
            self._deltas_ms.append(delta_ms)
            fields.update(
                mic_timestamp=mic.wall_time,
                mic_monotonic_ns=mic.monotonic_ns,
                delta_ms=delta_ms,
                ops_state=mic.ops_state,
            )
        else:
            fields["nearest_mic_delta_ms"] = self._nearest_mic_ms_locked(pin)
        self._decided_pins.append(pin)
        return fields

    def _mic_fields_locked(self, mic: _WitnessMic) -> dict:
        if mic.ops_state == OPS_STATE_ARMED:
            self._counts["mic_without_pin_armed"] += 1
        elif mic.ops_state == OPS_STATE_BUSY:
            self._counts["mic_without_pin_busy"] += 1
        return {
            "kind": "mic_without_pin",
            "mic_timestamp": mic.wall_time,
            "mic_monotonic_ns": mic.monotonic_ns,
            "ops_state": mic.ops_state,
            "fallback_pulse": mic.fallback_pulse,
            "nearest_pin_delta_ms": self._nearest_pin_ms_locked(mic),
            **self._window_fields(),
        }

    @staticmethod
    def _delta_ms(mic: _WitnessMic, pin: _WitnessPin) -> float:
        return round((mic.monotonic_ns - pin.monotonic_ns) / 1e6, 3)

    def _nearest_pin_ms_locked(self, mic: _WitnessMic) -> float | None:
        pins = [*self._pins, *self._decided_pins]
        if not pins:
            return None
        nearest = min(pins, key=lambda pin: abs(mic.monotonic_ns - pin.monotonic_ns))
        return self._delta_ms(mic, nearest)

    def _nearest_mic_ms_locked(self, pin: _WitnessPin) -> float | None:
        if not self._mics:
            return None
        nearest = min(self._mics, key=lambda mic: abs(mic.monotonic_ns - pin.monotonic_ns))
        return self._delta_ms(nearest, pin)

    def _window_fields(self) -> dict:
        return {"window_ms": list(self.window_ms)}

    def _find_pin_locked(self, pin_timestamp: float) -> _WitnessPin | None:
        for pin in (*self._pins, *reversed(self._decided_pins)):
            if abs(pin.wall_time - pin_timestamp) <= MATCH_TOLERANCE_S:
                return pin
        return None

    # --- lookups and summary ------------------------------------------------

    def mic_delta_ms(self, pin_timestamp: float) -> float | None:
        """Microphone minus pin edge (ms) for the pin edge stamped ``pin_timestamp``, if paired."""
        self.flush()
        with self._lock:
            pin = self._find_pin_locked(pin_timestamp)
            if pin is None or pin.mic is None:
                return None
            return self._delta_ms(pin.mic, pin)

    def pin_fields(self, pin_timestamp: float) -> dict:
        """Per-capture log fields for the dump that pin edge started."""
        self.flush()
        with self._lock:
            pin = self._find_pin_locked(pin_timestamp)
            if pin is None:
                return {
                    "ops_speed_witness": None,
                    "ops_speed_mic_delta_ms": None,
                    "ops_speed_pin_high_ms": None,
                }
            return {
                "ops_speed_witness": pin.kind or ("matched" if pin.mic else "pending"),
                "ops_speed_mic_delta_ms": (
                    self._delta_ms(pin.mic, pin) if pin.mic is not None else None
                ),
                "ops_speed_pin_high_ms": pin.high_ms,
            }

    def summary(self) -> dict:
        """Counts per outcome, the matched delays and the HIGH widths so far."""
        with self._lock:
            return {
                **self._counts,
                "pending_pins": len(self._pins),
                "pending_mics": sum(1 for mic in self._mics if not mic.done),
                "delta_ms": _stats(self._deltas_ms),
                "pin_high_ms": _stats(self._high_ms),
                **self._window_fields(),
            }

    def start(self, interval_s: float = WITNESS_FLUSH_INTERVAL_S) -> None:
        """Flush every ``interval_s`` on a daemon thread until ``stop``."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(interval_s,), name="ops-speed-witness", daemon=True
        )
        self._thread.start()

    def _run(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.flush()
            except Exception:
                logger.warning("[OPS-SPEED] Witness flush failed", exc_info=True)

    def stop(self) -> None:
        """Stop the flush thread and decide everything still pending."""
        thread, self._thread = self._thread, None
        if thread is not None:
            self._stop.set()
            thread.join(timeout=1.0)
        self.flush(everything=True)


def practice_swing_verdict(processed, *, max_smash: float = PRACTICE_SWING_MAX_SMASH) -> dict:
    """Practice-swing rule features and verdict for one processed ops-speed capture.

    ``practice_swing``: the OPS data show no club-to-ball jump (impact source
    not ``ops_transition``) and the smash is below ``max_smash``; ``unknown``
    without a club speed (or without a processed capture); else ``shot``.
    ``post_anchor_repeat_count`` counts outbound timeline readings at or after
    the club-speed anchor within the ball-track tolerance of the ball speed
    (a ball repeats its speed, a club alone does not; logged, not used).
    """
    # Imported here: rolling_buffer's package import pulls in its monitor,
    # which may import this module.
    from openflight.rolling_buffer.processor import RollingBufferProcessor

    if processed is None:
        return {"verdict": "unknown", "reason": "no_processed_capture"}
    impact = getattr(processed, "impact", None)
    impact_source = getattr(impact, "source", None)
    club = processed.club_speed_mph
    smash = processed.smash_factor
    repeat_count = None
    capture = getattr(processed, "capture", None)
    timeline = getattr(processed, "timeline", None)
    if capture is not None and timeline is not None:
        anchor_ms = capture.sound_edge_offset_ms
        tolerance = RollingBufferProcessor.BALL_TRACK_REPEAT_TOLERANCE_MPH
        repeat_count = sum(
            1
            for reading in timeline.readings
            if reading.is_outbound
            and reading.timestamp_ms >= anchor_ms
            and abs(reading.speed_mph - processed.ball_speed_mph) <= tolerance
        )
    if club is None or club <= 0:
        verdict = "unknown"
    elif impact_source != "ops_transition" and smash is not None and smash < max_smash:
        verdict = "practice_swing"
    else:
        verdict = "shot"
    return {
        "verdict": verdict,
        "impact_source": impact_source,
        "impact_speed_delta_mph": getattr(impact, "speed_delta_mph", None),
        "smash_factor": None if smash is None else round(smash, 4),
        "ball_speed_mph": processed.ball_speed_mph,
        "club_speed_mph": club,
        "post_anchor_repeat_count": repeat_count,
        "rejection_reason": processed.rejection_reason,
        "max_smash": max_smash,
    }


__all__ = [
    "DEFAULT_ANCHOR_MS",
    "DEFAULT_DEBOUNCE_MS",
    "DEFAULT_HOLDOFF_MS",
    "DEFAULT_PIN_ACTIVE",
    "DEFAULT_PRE_TRIGGER_SEGMENTS",
    "DEFAULT_STALL_MIC_EDGES",
    "DEFAULT_THRESHOLD_MPH",
    "DEFAULT_TI_DELAY_MS",
    "DEFAULT_WITNESS_WINDOW_MS",
    "OPS_SPEED",
    "OPS_SPEED_SHADOW",
    "OPS_SPEED_SHOT_TRIGGERS",
    "OPS_STATE_ARMED",
    "OPS_STATE_BUSY",
    "OpsSpeedConfig",
    "OpsSpeedPinError",
    "OpsSpeedWitness",
    "PIN_ACTIVE_LEVELS",
    "PIN_PULLS",
    "PRACTICE_SWING_MAX_SMASH",
    "PinEdgeInput",
    "button_kwargs",
    "make_pin_input",
    "parse_firmware_version",
    "parse_window_ms",
    "pin_button_factory",
    "pin_idle_message",
    "pin_idle_problem",
    "practice_swing_verdict",
    "resolve_pin_pull",
    "speed_trigger_supported",
]
