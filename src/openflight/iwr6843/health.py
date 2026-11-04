"""TI CLI reply classification and capture-health checks.

The IWR6843LEVM has no host path to NRST (CP2105 GPIO and modem lines are
unconnected on SWRR178), so the radar itself cannot be reset from software.
This module only *detects* the two observed wedge classes so the host can
abort, or recover, with a precise cause:

- class A, ``HWA_STALL``: the CLI answers, but the HWA chain produces no
  output frames; ``sensorStop`` reports "HWA shutdown boundary timed out" and
  every ``l3dump`` fails before its ILD1 magic.
- class B, ``SILENT_CLI``: a lifecycle command gets no Done/Error at all.

Only a USB power cycle recovers either class; a Pi reboot keeps USB powered.
By default that means a Pi shutdown or unplugging the TI cable. With the
opt-in ``--iwr6843-usb-power-cycle`` flag, a Pi 5 cuts USB power itself
(:mod:`openflight.iwr6843.usb_power`) at startup and mid-session.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

BANNER = "BANNER"
NOT_RECOGNIZED = "NOT_RECOGNIZED"
ERROR = "ERROR"
DONE = "DONE"
ECHO_ONLY = "ECHO_ONLY"
SILENT = "SILENT"
OTHER = "OTHER"
REPLY_KINDS = (BANNER, NOT_RECOGNIZED, ERROR, DONE, ECHO_ONLY, SILENT, OTHER)

HWA_TIMEOUT = "HWA_TIMEOUT"
HWA_TIMEOUT_TEXT = "HWA shutdown boundary timed out"

OK = "OK"
HWA_STALL = "HWA_STALL"
RF_STOPPED = "RF_STOPPED"
SILENT_CLI = "SILENT_CLI"
UNEXPECTED_REPLY = "UNEXPECTED_REPLY"

CAUSE_LABELS = {
    HWA_STALL: "HWA chain stalled",
    RF_STOPPED: "no RF frames",
    SILENT_CLI: "no reply to commands",
    UNEXPECTED_REPLY: "unexpected reply",
}

WEDGE_TAG = "[TI wedge: "
PROMPT = "l3dump:/>"
BANNER_TEXT = "hybrid-cadence L3 firmware"
NOT_RECOGNIZED_TEXT = "is not recognized as a CLI command"

HWA_STALL_SESSION_REASON = (
    "TI radar stopped delivering data (HWA stall) — shut the Pi down (not restart) "
    "after this session"
)
_TEXT_KINDS = (
    (BANNER_TEXT, BANNER),
    (NOT_RECOGNIZED_TEXT, NOT_RECOGNIZED),
    ("Error", ERROR),
    ("Done", DONE),
)
_CLASS_A_DUMP_MARKERS = ("freeze timed out", "bad magic", "ild1")
_STATS_PAIR = re.compile(r"(\w+)=(0x[0-9a-fA-F]+|-?\d+)(?=\s|$)")


@dataclass(frozen=True)
class CliReply:
    """One CLI exchange: what was sent, what came back, and how it reads."""

    command: str
    raw: str
    elapsed_s: float
    kind: str
    residue: str = ""

    @property
    def ok(self) -> bool:
        """Whether the firmware acknowledged the command with Done."""
        return self.kind == DONE

    @property
    def detail(self) -> str | None:
        """``HWA_TIMEOUT`` for the class-A sensorStop error, else None."""
        if self.kind == ERROR and HWA_TIMEOUT_TEXT in self.raw:
            return HWA_TIMEOUT
        return None


def classify_reply(command: str, raw: str) -> str:
    """Classify a CLI reply; earlier kinds in ``REPLY_KINDS`` take precedence."""
    for marker, kind in _TEXT_KINDS:
        if marker in raw:
            return kind
    lines = [line.strip() for line in raw.replace(PROMPT, "\n").splitlines()]
    lines = [line for line in lines if line]
    if lines and all(line == command.strip() for line in lines):
        return ECHO_ONLY
    if not lines:
        return SILENT
    return OTHER


def strip_prompts(text: str) -> str:
    """Remove CLI prompts and surrounding whitespace from leftover bytes."""
    return text.replace(PROMPT, "").strip()


def parse_stats(text: str) -> dict[str, int]:
    """Integer ``key=value`` pairs of a ``stats`` reply (hex values included)."""
    return {key: int(value, 0) for key, value in _STATS_PAIR.findall(text)}


def assess_capture(before: dict[str, int], after: dict[str, int]) -> str:
    """Judge whether RF frames and the HWA chain run at all.

    Only a frozen counter fails: no new frames is ``RF_STOPPED``; frames
    without a single new HWA output is ``HWA_STALL``. Firmware without an
    ``hwa_out`` counter is judged on frames alone. Slow counters are left to
    :func:`capture_warning`.
    """
    frames = after.get("frames", 0) - before.get("frames", 0)
    if frames <= 0:
        return RF_STOPPED
    if "hwa_out" in after and after["hwa_out"] - before.get("hwa_out", 0) <= 0:
        return HWA_STALL
    return OK


def capture_warning(
    before: dict[str, int], after: dict[str, int], min_frames: int = 10
) -> str | None:
    """Why a capture that passed :func:`assess_capture` still looks slow, else None.

    Fewer than ``min_frames`` new frames, or fewer than half as many new HWA
    outputs as frames, is worth a warning but not an abort.
    """
    frames = after.get("frames", 0) - before.get("frames", 0)
    if frames < min_frames:
        return f"only {frames} new RF frames"
    if "hwa_out" in after:
        hwa_out = after["hwa_out"] - before.get("hwa_out", 0)
        if 2 * hwa_out < frames:
            return f"only {hwa_out} new HWA outputs for {frames} new RF frames"
    return None


def cause_for_reply(reply: CliReply) -> str:
    """Wedge cause implied by a reply that was not a plain Done."""
    if reply.detail == HWA_TIMEOUT:
        return HWA_STALL
    if reply.kind in (SILENT, ECHO_ONLY):
        return SILENT_CLI
    return UNEXPECTED_REPLY


def is_class_a_dump_error(error: str | None) -> bool:
    """Whether a dump failure looks like the HWA stall (no ILD1 payload)."""
    normalized = (error or "").casefold()
    return any(marker in normalized for marker in _CLASS_A_DUMP_MARKERS)


class TiWedgedError(RuntimeError):
    """The TI firmware stopped working in a way only a power cycle fixes.

    The message keeps the historical markers ("did not acknowledge",
    "the firmware may be wedged (press RESET and retry)", and the firmware's
    own "HWA shutdown boundary timed out" text when it sent one) so log
    searches and older callers keep matching.
    """

    def __init__(self, cause: str, command: str, reply: CliReply | str | None = None):
        self.cause = cause
        self.command = command
        self.reply = reply
        raw = reply.raw if isinstance(reply, CliReply) else (reply or "")
        message = (
            f"IWR6843 did not acknowledge {command!r}; the firmware may be wedged "
            f"(press RESET and retry) {WEDGE_TAG}{self.label}]"
        )
        excerpt = raw.strip()[:300]
        if excerpt:
            message += f"; reply: {excerpt}"
        super().__init__(message)

    @property
    def label(self) -> str:
        """Short operator-facing name of the wedge cause."""
        return CAUSE_LABELS.get(self.cause, self.cause)


def wedge_label(error: object) -> str | None:
    """Wedge cause label from a TI error message, including pre-TiWedgedError texts."""
    text = str(error or "")
    start = text.find(WEDGE_TAG)
    end = text.find("]", start)
    if start >= 0 and end > start:
        return text[start + len(WEDGE_TAG) : end]
    normalized = text.casefold()
    if HWA_TIMEOUT_TEXT.casefold() in normalized:
        return CAUSE_LABELS[HWA_STALL]
    if "press reset and retry" in normalized or "firmware may be wedged" in normalized:
        return CAUSE_LABELS[SILENT_CLI]
    return None


def read_throttled(timeout_s: float = 1.0) -> str | None:
    """``vcgencmd get_throttled`` output, or None off a Pi or on any failure."""
    try:
        result = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or result.stderr).strip() or None


def log_power_state(log: logging.Logger) -> None:
    """Record the Pi's undervoltage/throttle flags next to a TI failure."""
    log.warning("[IWR6843] Pi power state at TI failure: %s", read_throttled() or "unavailable")


__all__ = [
    "CAUSE_LABELS",
    "CliReply",
    "TiWedgedError",
    "assess_capture",
    "classify_reply",
    "is_class_a_dump_error",
    "parse_stats",
    "wedge_label",
]
