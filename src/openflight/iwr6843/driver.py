"""IWR6843 serial driver — the host side of the L3-dump firmware contract.

Firmware v3+ speaks a SINGLE UART (the CP2105 Enhanced interface) at
1,041,667 baud for both CLI commands and the binary dump; the dump is framed
by its "ILD1" magic plus the header-declared length, so CLI echo and payload
can share the pipe. Hardware-validated 2026-07-13 at 100% of wire rate.

Gotchas baked in (each cost a debugging session):
- DTR/RTS must be held low on open (TI EVMs tie them to reset/boot mode).
- One serial handle only — two handles on one tty steal each other's bytes.
- The CP2105 can stall a stream for seconds (cp210x -110 control timeouts)
  and resume; the reader waits out gaps up to ``stall_tolerance_s``.

Firmware with the self-trigger (v1.0.3+) writes ``Triggered`` on this UART
whenever it froze its own ring. Every read path scans for that line and keeps
it (``take_notices``), also when it lands inside a command reply or ahead of a
dump, so a fire is never lost to a command that happened to run.
"""

from __future__ import annotations

import glob
import logging
import re
import threading
import time
from dataclasses import dataclass

import serial

from openflight.iwr6843.dump import HEADER, MAGIC, parse_header, payload_nbytes
from openflight.iwr6843.health import (
    BANNER_TEXT,
    ECHO_ONLY,
    ERROR,
    NOT_RECOGNIZED,
    NOT_RECOGNIZED_TEXT,
    OK,
    PROMPT,
    SILENT,
    CliReply,
    TiWedgedError,
    assess_capture,
    capture_warning,
    cause_for_reply,
    classify_reply,
    log_power_state,
    parse_stats,
    strip_prompts,
)
from openflight.iwr6843.self_trigger import TRIGGER_NOTICE, TriggerNotice, parse_trigger_cause

BAUD = 1_041_667
_PORT_GLOBS = ("/dev/ttyUSB*", "/dev/tty.SLAB_USBtoUART*")
_PROMPT = PROMPT.encode()
_TERMINATORS = tuple(
    marker.encode() for marker in ("Done", "Error", NOT_RECOGNIZED_TEXT, BANNER_TEXT)
)
_PROMPT_GRACE_S = 0.2
_SENSOR_START_WINDOW_S = 6.0
_SENSOR_START_LATE_S = 15.0
_CAPTURE_CHECK_S = 1.0
_DUMP_TRAILER = re.compile(rb"Done\r?\n\s*l3dump:/>\s*$")
_ERROR_GRACE_S = 0.5
# A healthy TI answers ``l3dump`` at once: all 26 complete wide dumps in the
# 2026-10-09 range-session logs took 7.09-7.10 s for 732,812 bytes, whose 8N1
# wire time at 1,041,667 baud is 7.035 s, so write-to-first-byte plus every
# gap stayed below 0.07 s. 3x that is 0.2 s; the window is never below 3 s and
# matches the 4 s CP2105 stall tolerance, because the bridge's multi-second
# stalls can also hold back the first byte. A silent CLI used to cost the whole
# 40 s read timeout.
_FIRST_BYTE_WINDOW_S = 4.0
_SLOW_CLOSE_S = 1.0
_NOTICE_TAIL_BYTES = len(TRIGGER_NOTICE) - 1
_NOTICE_LINE = re.compile(rb"Triggered\r?\n?")
# Commands whose handler takes a latched self-trigger freeze (l3_awaitFrozenRing,
# or sensorStart/Stop's latch clear): a notice read before their verdict is consumed.
_LATCH_CONSUMERS = ("l3dump", "l3release", "sensorStop", "sensorStart")
# l3release waits up to 250 ms for the freeze, then restarts the front end.
_RELEASE_WINDOW_S = 3.0
_TRIGGER_LOG_WINDOW_S = 2.0
_NOTICE_COMPLETE_S = 0.01

logger = logging.getLogger(__name__)


class DumpCancelled(Exception):
    """Firmware acknowledged a discarded capture and returned to its CLI."""


def _terminator_end(buf: bytes | bytearray) -> int | None:
    """End offset of the first reply terminator in ``buf``, if any."""
    ends = [buf.find(token) + len(token) for token in _TERMINATORS if token in buf]
    return min(ends) if ends else None


def _expected_dump_size(buf: bytearray) -> int | None:
    """Dump length from its header once the magic and header arrived.

    Drops the CLI echo before the magic from ``buf`` as a side effect.
    """
    idx = buf.find(MAGIC)
    if idx < 0 or len(buf) - idx < HEADER.size:
        return None
    del buf[:idx]
    try:
        metadata = parse_header(buf)
        return metadata["header_nbytes"] + payload_nbytes(metadata, buf)
    except ValueError:
        return None


@dataclass
class _DumpReadStats:
    """UART behaviour of one dump read, for diagnosing short dumps."""

    start: float
    last: float = 0.0
    reads: int = 0
    max_in_waiting: int = 0
    max_gap_s: float = 0.0
    end: str = "timeout"

    def record(self, waiting: int, now: float) -> None:
        """Account for one non-empty read."""
        self.reads += 1
        self.max_in_waiting = max(self.max_in_waiting, waiting)
        self.max_gap_s = max(self.max_gap_s, now - self.last)
        self.last = now

    def log(self, nbytes: int, expected: int | None, now: float) -> None:
        """DEBUG for complete dumps, WARNING for short ones."""
        short = expected is None or nbytes < expected
        logger.log(
            logging.WARNING if short else logging.DEBUG,
            "[IWR6843] Dump read ended (%s): %d/%s bytes in %.2fs, %d reads, "
            "max in_waiting %d, max gap %.2fs",
            self.end,
            nbytes,
            "?" if expected is None else expected,
            now - self.start,
            self.reads,
            self.max_in_waiting,
            self.max_gap_s,
        )


def open_port(port: str, baud: int = BAUD, timeout: float = 0.3) -> serial.Serial:
    """DTR/RTS-safe serial open."""
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, baud, timeout
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


class IWR6843Radar:
    """CLI + dump transport for the custom L3-dump firmware."""

    _clock = staticmethod(time.monotonic)
    _sleep = staticmethod(time.sleep)
    _exchanges: list[CliReply] | None = None
    # Notice stamps; tests replace them. Class defaults so a radar built with
    # __new__ (the scripted-CLI tests) works without __init__.
    _notice_monotonic_ns = staticmethod(time.monotonic_ns)
    _notice_wall_time = staticmethod(time.time)
    _notice_tail = b""
    _notices: tuple[TriggerNotice, ...] = ()

    def __init__(self, port: str | None = None, baud: int = BAUD):
        try:
            if port is None:
                port = self.detect_port(baud)
                if port is None:
                    raise RuntimeError("no IWR6843 CLI found — board on, flashed, single-port fw?")
            self.port = port
            self.baud = baud
            self.ser = open_port(port, baud)
        except Exception:
            log_power_state(logger)
            raise

    @staticmethod
    def detect_port(baud: int = BAUD) -> str | None:
        """First serial port whose CLI answers `help` with our commands."""
        candidates: list[str] = []
        for pattern in _PORT_GLOBS:
            candidates.extend(sorted(glob.glob(pattern)))
        for cand in candidates:
            try:
                ser = open_port(cand, baud)
            except (OSError, serial.SerialException):
                continue
            try:
                ser.reset_input_buffer()
                ser.write(b"help\n")
                resp = b""
                deadline = time.monotonic() + 1.5
                while time.monotonic() < deadline and b"sensorStart" not in resp:
                    resp += ser.read(512)
            finally:
                ser.close()
            if b"sensorStart" in resp:
                return cand
        return None

    def command(self, line: str, window: float = 1.5, *, late_s: float = 0.0) -> CliReply:
        """Send one CLI line and classify its reply.

        A reply ends at Done/Error/"not recognized" plus the prompt. With
        ``late_s``, a reply that is still only an echo (or nothing) after
        ``window`` is awaited that much longer without resending the command.
        """
        words = line.split(maxsplit=1)
        consumes = bool(words) and words[0] in _LATCH_CONSUMERS
        residue = self._take_residue(consumed=consumes)
        self.ser.write((line + "\n").encode())
        start = self._clock()
        buf = self._collect_reply(bytearray(), start + window)
        kind = classify_reply(line, buf.decode(errors="replace"))
        if late_s > 0.0 and kind in (ECHO_ONLY, SILENT):
            buf = self._collect_reply(buf, start + window + late_s)
        buf = self._strip_notices_from_reply(buf, consumed=consumes)
        raw = buf.decode(errors="replace")
        reply = CliReply(
            command=line,
            raw=raw,
            elapsed_s=self._clock() - start,
            kind=classify_reply(line, raw),
            residue=residue,
        )
        logger.debug(
            "[IWR6843] CLI %r -> %s in %.2fs: %r",
            line,
            reply.kind,
            reply.elapsed_s,
            raw[:200],
        )
        if not reply.ok:
            logger.warning(
                "[IWR6843] CLI %r replied %s after %.2fs: %r",
                line,
                reply.detail or reply.kind,
                reply.elapsed_s,
                raw[:200],
            )
        if self._exchanges is not None:
            self._exchanges.append(reply)
        return reply

    def cmd(self, line: str, window: float = 1.5) -> str:
        """Send one CLI line; return the raw response text."""
        return self.command(line, window).raw

    def _take_residue(self, *, consumed: bool = False) -> str:
        """Discard unread bytes before a command, warning about anything but prompts.

        A ``Triggered`` notice among them is kept (see ``take_notices``).
        """
        waiting = self.ser.in_waiting
        leftover = self.ser.read(waiting) if waiting else b""
        self.ser.reset_input_buffer()
        self._scan_for_notices(leftover, in_reply=True, consumed=consumed)
        self._notice_tail = b""
        leftover = _NOTICE_LINE.sub(b"", leftover)
        residue = strip_prompts(leftover.decode(errors="replace"))
        if residue:
            logger.warning("[IWR6843] Discarding unread TI output: %r", residue[:200])
        return residue

    def _read_available(self) -> bytes:
        waiting = self.ser.in_waiting
        return self.ser.read(waiting if waiting else 1)

    def _scan_for_notices(
        self, data: bytes | bytearray, *, in_reply: bool = False, consumed: bool = False
    ) -> int:
        """Record every ``Triggered`` in ``data``, also one split across reads."""
        if not data:
            return 0
        joined = self._notice_tail + bytes(data)
        count = joined.count(TRIGGER_NOTICE)
        last = joined.rfind(TRIGGER_NOTICE)
        rest = joined[last + len(TRIGGER_NOTICE) :] if last >= 0 else joined
        self._notice_tail = rest[-_NOTICE_TAIL_BYTES:]
        if count:
            stamp = TriggerNotice(
                monotonic_ns=self._notice_monotonic_ns(),
                wall_time=self._notice_wall_time(),
                in_reply=in_reply,
                consumed=consumed,
            )
            self._notices = (*self._notices, *([stamp] * count))
            if in_reply:
                logger.info(
                    "[IWR6843] Self-trigger notice read inside a CLI exchange%s",
                    " (that command takes its freeze)" if consumed else "",
                )
        return count

    def _strip_notices_from_reply(self, buf: bytearray, *, consumed: bool) -> bytearray:
        """Keep the notices of a reply and return the reply without them.

        Notices before the reply's verdict are consumed by a latch-taking
        command; any after it (the board rearmed and fired again) are not.
        """
        end = _terminator_end(buf)
        self._notice_tail = b""
        if end is None:
            self._scan_for_notices(buf, in_reply=True, consumed=consumed)
        else:
            self._scan_for_notices(buf[:end], in_reply=True, consumed=consumed)
            self._notice_tail = b""
            self._scan_for_notices(buf[end:], in_reply=True, consumed=False)
        self._notice_tail = b""
        if TRIGGER_NOTICE not in buf:
            return buf
        return bytearray(_NOTICE_LINE.sub(b"", bytes(buf)))

    def take_notices(self) -> tuple[TriggerNotice, ...]:
        """Self-trigger notices read since the last call, oldest first."""
        notices, self._notices = self._notices, ()
        return notices

    def poll_trigger_notice(self, *, block: bool) -> tuple[TriggerNotice, ...]:
        """Read what the idle CLI sent; return the notices read so far.

        ``block`` waits up to the port timeout for the first byte (``read``
        returns as soon as one arrives, so a notice is seen within about a
        millisecond); otherwise only bytes already waiting are read.
        """
        waiting = self.ser.in_waiting
        if waiting or block:
            data = self.ser.read(waiting if waiting else 1)
            if data:
                self._scan_for_notices(data)
                # A blocking read returns the first byte alone; the rest of a
                # notice follows within ~0.1 ms at 1 MBd, so finish the word.
                deadline = self._clock() + _NOTICE_COMPLETE_S
                while self._notice_head_pending() and self._clock() < deadline:
                    more = self.ser.read(self.ser.in_waiting or 1)
                    if not more:
                        break
                    data += more
                    self._scan_for_notices(more)
                logger.debug("[IWR6843] Idle CLI bytes: %r", data[:200])
        return self.take_notices()

    def _notice_head_pending(self) -> bool:
        """Whether the bytes read so far end in the start of a ``Triggered``."""
        tail = self._notice_tail
        return any(tail.endswith(TRIGGER_NOTICE[:size]) for size in range(1, len(TRIGGER_NOTICE)))

    def set_self_trigger(self, command: str) -> None:
        """Send one ``triggerCfg`` line; raise unless the firmware answered Done."""
        reply = self.command(command, 2.0)
        if reply.kind == NOT_RECOGNIZED:
            raise RuntimeError(
                "IWR6843 firmware has no triggerCfg: flash the self-trigger build "
                "(v1.0.3 sources with cb8fbff or later)"
            )
        self._require_done(reply)

    def release_frozen_ring(self) -> None:
        """``l3release``: take a self-trigger freeze without streaming it, then rearm.

        Raises TiWedgedError when the CLI gave no verdict, RuntimeError when it
        refused (nothing frozen and nothing running, or the rearm failed).
        """
        reply = self.command("l3release", _RELEASE_WINDOW_S)
        if reply.kind == NOT_RECOGNIZED:
            raise RuntimeError("IWR6843 firmware has no l3release; flash the self-trigger build")
        if reply.ok:
            return
        if reply.kind in (ECHO_ONLY, SILENT):
            raise TiWedgedError(cause_for_reply(reply), reply.command, reply)
        raise RuntimeError(
            f"IWR6843 did not release the frozen ring: {reply.raw.strip() or reply.kind}"
        )

    def trigger_cause(self) -> str | None:
        """Which rule fired the latched self-trigger (``triggerLog track``), or None."""
        reply = self.command("triggerLog track", _TRIGGER_LOG_WINDOW_S)
        if not reply.ok:
            return None
        return parse_trigger_cause(reply.raw)

    def _collect_reply(self, buf: bytearray, deadline: float) -> bytearray:
        """Read until a terminator and the prompt after it, or until ``deadline``."""
        settle_deadline = None
        while self._clock() < deadline:
            if settle_deadline is not None and self._clock() >= settle_deadline:
                break
            buf.extend(self._read_available())
            end = _terminator_end(buf)
            if end is None:
                continue
            if _PROMPT in buf[end:]:
                break
            if settle_deadline is None:
                settle_deadline = self._clock() + _PROMPT_GRACE_S
        return buf

    def _wait_for_prompt(self, deadline: float) -> bool:
        buf = bytearray()
        while self._clock() < deadline:
            buf.extend(self._read_available())
            if _PROMPT in buf:
                return True
        return False

    def resync(self, *, first_wait_s: float = 1.0, retry_s: float = 10.0) -> bool:
        """Bring the CLI back to an empty prompt before a lifecycle command.

        Sends a bare newline, which clears any half-typed command in the
        firmware's line buffer, then keeps nudging once a second.
        """
        start = self._clock()
        self._take_residue()
        self.ser.write(b"\n")
        if self._wait_for_prompt(start + first_wait_s):
            return True
        deadline = self._clock() + retry_s
        while self._clock() < deadline:
            self.ser.write(b"\n")
            if self._wait_for_prompt(min(deadline, self._clock() + 1.0)):
                logger.warning(
                    "[IWR6843] CLI prompt returned after %.1fs of resync",
                    self._clock() - start,
                )
                return True
        logger.warning("[IWR6843] CLI gave no prompt within %.1fs of resync", self._clock() - start)
        return False

    def drain_stale_output(
        self,
        *,
        max_wait_s: float = 10.0,
        initial_quiet_s: float = 0.25,
        stream_quiet_s: float = 4.25,
    ) -> int:
        """Drain an abandoned binary dump before sending configuration commands.

        If a host process exits during ``l3dump``, the firmware can still be
        writing the old payload through the CP2105. Commands sent into that
        stream are not safe to associate with their responses. Once bytes are
        observed, tolerate the bridge's known multi-second stalls before
        declaring the stream quiet.
        """
        drained = 0
        saw_data = False
        start = self._clock()
        last_data = start
        while self._clock() - start < max_wait_s:
            waiting = self.ser.in_waiting
            if waiting:
                chunk = self.ser.read(min(waiting, 4096))
                if chunk:
                    drained += len(chunk)
                    saw_data = True
                    last_data = self._clock()
                    continue
            quiet_s = stream_quiet_s if saw_data else initial_quiet_s
            if self._clock() - last_data >= quiet_s:
                break
            self._sleep(0.01)
        if drained:
            logger.warning(
                "[IWR6843] Drained %d stale UART bytes before configuration",
                drained,
            )
        return drained

    @staticmethod
    def _require_done(reply: CliReply) -> None:
        """Raise unless the reply is a Done; wedge signatures raise TiWedgedError."""
        if reply.ok:
            return
        if reply.kind == ERROR and reply.detail is None:
            raise RuntimeError(f"config rejected: {reply.command!r}: {reply.raw.strip()}")
        raise TiWedgedError(cause_for_reply(reply), reply.command, reply)

    def _sensor_stop(self, *, retry: bool = True) -> None:
        """sensorStop with one resync-and-retry for a lost or garbled command."""
        reply = self.command("sensorStop", 3.0)
        if reply.ok or reply.kind == ERROR or not retry:
            self._require_done(reply)
            return
        self.resync()
        retry = self.command("sensorStop", 3.0)
        self._require_done(retry)
        logger.warning("[IWR6843] sensorStop succeeded on retry after %s", reply.kind)

    def send_config(self, cfg_path: str) -> None:
        """Stop and flush old state, stream the cfg, then prove frames flow.

        The firmware's geometry guard rejects a cfg whose loops/samples don't
        match the flashed build — that surfaces here as RuntimeError. A wedged
        firmware raises TiWedgedError naming the cause.
        """
        started = self._clock()
        self._exchanges = []
        try:
            self._configure(cfg_path)
        except Exception:
            log_power_state(logger)
            raise
        finally:
            exchanges, self._exchanges = self._exchanges, None
        slowest = max(exchanges, key=lambda reply: reply.elapsed_s)
        logger.info(
            "[IWR6843] TI configured in %.1fs, %d commands, slowest %r %.2fs",
            self._clock() - started,
            len(exchanges),
            slowest.command,
            slowest.elapsed_s,
        )

    def _configure(self, cfg_path: str) -> CliReply:
        self.drain_stale_output()
        if not self.resync():
            logger.warning("[IWR6843] Sending sensorStop without a confirmed CLI prompt")
        self._sensor_stop()
        self._require_done(self.command("flushCfg", 1.5))
        with open(cfg_path, encoding="utf-8") as cfg:
            for rawline in cfg:
                line = rawline.strip()
                if not line or line.startswith("%"):
                    continue
                # The driver owns the lifecycle commands so every config gets
                # the required stop/flush ordering without sending duplicates.
                if line in {"sensorStop", "flushCfg"}:
                    continue
                if line.startswith("sensorStart"):
                    reply = self.command(line, _SENSOR_START_WINDOW_S, late_s=_SENSOR_START_LATE_S)
                    if reply.ok and reply.elapsed_s > _SENSOR_START_WINDOW_S:
                        logger.warning(
                            "[IWR6843] sensorStart acknowledged late, after %.1fs",
                            reply.elapsed_s,
                        )
                else:
                    reply = self.command(line, 1.5)
                self._require_done(reply)
        return self._verify_capture(self._wait_for_active())

    def _wait_for_active(self) -> CliReply:
        deadline = self._clock() + 6.0
        while True:
            health = self.command("stats", 2.0)
            self._require_done(health)
            if "active=1" in health.raw:
                return health
            if self._clock() >= deadline:
                raise RuntimeError(
                    f"IWR6843 did not enter active capture mode: {health.raw.strip()}"
                )
            self._sleep(0.1)

    def _verify_capture(self, baseline: CliReply) -> CliReply:
        """Require RF frames and HWA output to advance after ``active=1``.

        ``active=1`` is set before MMWave_start, so it does not prove capture.
        Re-sample every 250 ms for up to one second; only a counter that never
        moves aborts, a slow one is logged as a warning.
        """
        before = parse_stats(baseline.raw)
        started = self._clock()
        deadline = started + _CAPTURE_CHECK_S
        while True:
            self._sleep(0.25)
            after = self.command("stats", 2.0)
            self._require_done(after)
            after_stats = parse_stats(after.raw)
            verdict = assess_capture(before, after_stats)
            warning = capture_warning(before, after_stats)
            if (verdict == OK and warning is None) or self._clock() >= deadline:
                break
        logger.info(
            "[IWR6843] TI stats: %s; at active=1, %.2fs earlier: %s",
            " ".join(after.raw.split()),
            self._clock() - started,
            " ".join(baseline.raw.split()),
        )
        if verdict != OK:
            raise TiWedgedError(verdict, "sensorStart", after)
        if warning is not None:
            logger.warning("[IWR6843] TI capture looks slow after sensorStart: %s", warning)
        return after

    def read_dump(
        self,
        timeout_s: float = 40.0,
        stall_tolerance_s: float = 4.0,
        *,
        cancel_event: threading.Event | None = None,
        first_byte_s: float = _FIRST_BYTE_WINDOW_S,
    ) -> bytes:
        """Fire `l3dump` and return one complete dump (best effort on stalls).

        Syncs on the ILD1 magic past the CLI echo and sizes the read from the
        dump's own header, so any firmware geometry works. A CLI error before
        the magic raises at once; a short dump returns as soon as the
        firmware's closing ``Done`` prompt shows the payload is over. No byte
        at all within ``first_byte_s`` of the command ends the read empty.
        """
        # Self-trigger callers poll for notices right before this; the reset
        # drops what arrived since (microseconds).
        self._notice_tail = b""
        self.ser.reset_input_buffer()
        self.ser.write(b"l3dump\n")
        buf = bytearray()
        expected: int | None = None
        pre_scanned = 0
        cancel_sent = False
        read = _DumpReadStats(start=self._clock())
        read.last = read.start
        while self._clock() - read.start < timeout_s:
            # Send CAN only during a confirmed binary transfer, never to an idle CLI.
            if (
                cancel_event is not None
                and cancel_event.is_set()
                and not cancel_sent
                and expected is not None
                and len(buf) < expected
            ):
                self.ser.write(b"\x18")
                cancel_sent = True
            waiting = self.ser.in_waiting
            chunk = self.ser.read(waiting if waiting else 1)
            if chunk:
                read.record(waiting, self._clock())
                buf.extend(chunk)
            elif buf and self._clock() - read.last > stall_tolerance_s:
                read.end = "stall"
                break
            elif not buf and self._clock() - read.start > first_byte_s:
                read.end = "no first byte"
                break
            if cancel_sent:
                self._raise_on_cancel_ack(buf)
            if expected is None:
                if chunk:
                    # Only the CLI text before the magic: l3dump takes a fire seen there.
                    magic_at = buf.find(MAGIC)
                    stop = magic_at if magic_at >= 0 else len(buf)
                    if stop > pre_scanned:
                        self._scan_for_notices(buf[pre_scanned:stop], in_reply=True, consumed=True)
                        pre_scanned = stop
                self._raise_on_cli_error(buf)
                expected = _expected_dump_size(buf)
                if buf.startswith(MAGIC):
                    pre_scanned = 0
            if expected is not None and len(buf) >= expected:
                read.end = "complete"
                break
            trailer = _DUMP_TRAILER.search(buf, max(0, len(buf) - 64)) if chunk else None
            if trailer is not None:
                del buf[trailer.start() :]
                read.end = "trailer"
                break
        read.log(len(buf), expected, self._clock())
        if expected is None:
            return bytes(buf)

        payload = bytes(buf[:expected])
        if len(payload) == expected:
            # The binary payload can finish just before the CLI handler returns.
            # Wait for its trailing Done before another command can be consumed
            # by the firmware while it is still completing dump/restart work.
            elapsed = self._clock() - read.start
            trailer = self._wait_for_dump_cli_ready(
                buf[expected:], timeout_s=min(1.0, max(0.0, timeout_s - elapsed))
            )
            # l3dump restarts the ring before its Done: a notice here is a new fire.
            self._notice_tail = b""
            self._scan_for_notices(trailer, in_reply=True, consumed=False)
            self._notice_tail = b""
            if b"Error" in trailer:
                raise RuntimeError(
                    f"IWR6843 dump completed but firmware restart failed: "
                    f"{trailer.decode(errors='replace').strip()}"
                )
        return payload

    def _raise_on_cancel_ack(self, buf: bytearray) -> None:
        # Firmware writes eight bytes of its nine-byte ILDCANCEL literal.
        ack = buf.find(b"ILDCANCE")
        if ack < 0:
            return
        trailer = self._wait_for_dump_cli_ready(buf[ack + len(b"ILDCANCE") :], timeout_s=1.0)
        if not re.match(rb"L?\s*Done(?:\s|$)", trailer) or b"Error" in trailer:
            raise RuntimeError("TI cancellation did not confirm firmware restart")
        raise DumpCancelled("OPS rejected acoustic trigger; TI re-armed")

    def _raise_on_cli_error(self, buf: bytearray) -> None:
        """Fail fast when the firmware answers ``l3dump`` with an error line."""
        if MAGIC in buf or b"Error" not in buf:
            return
        reply = self._collect_reply(bytearray(buf), self._clock() + _ERROR_GRACE_S)
        text = " ".join(reply.decode(errors="replace").split())
        raise RuntimeError(f"IWR6843 dump refused before ILD1: {text}")

    def _wait_for_dump_cli_ready(self, initial: bytes, *, timeout_s: float) -> bytes:
        """Consume the dump handler's trailing response before reusing the CLI."""
        response = bytearray(initial)
        deadline = self._clock() + timeout_s
        while b"Done" not in response and b"Error" not in response:
            if self._clock() >= deadline:
                break
            waiting = self.ser.in_waiting
            chunk = self.ser.read(waiting if waiting else 1)
            if chunk:
                response.extend(chunk)
        return bytes(response)

    def stats(self) -> str:
        """Firmware health line (frames/wraps/active/calib/rf_faults)."""
        return self.cmd("stats", 2.0)

    def stop_sensor(self, *, retry: bool = True) -> None:
        """Stop capture and verify the firmware returned to its idle CLI state.

        ``retry=False`` (shutdown) sends one sensorStop without the resync
        ladder, so a silent board costs a single command window.
        """
        self._sensor_stop(retry=retry)
        health = self.command("stats", 2.0)
        self._require_done(health)
        if "active=0" not in health.raw:
            raise RuntimeError(f"IWR6843 remained active after sensorStop: {health.raw.strip()}")

    def close(self) -> None:
        """Release the serial port, warning when the bridge makes that slow."""
        started = self._clock()
        self.ser.close()
        elapsed = self._clock() - started
        if elapsed > _SLOW_CLOSE_S:
            logger.warning("[IWR6843] Closing the TI serial port took %.1fs", elapsed)

    def reopen(self) -> None:
        """Open the same port again, e.g. after a USB power cycle re-enumerated it.

        The old handle may belong to a vanished device, so a failure to close
        it is logged and ignored.
        """
        try:
            self.close()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("[IWR6843] Closing the stale TI serial handle failed", exc_info=True)
        self.ser = open_port(self.port, self.baud)
        logger.info("[IWR6843] Reopened TI serial port %s", self.port)

    def __enter__(self) -> "IWR6843Radar":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
