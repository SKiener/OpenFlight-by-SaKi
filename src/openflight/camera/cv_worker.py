"""Heavy per-shot camera computer vision in one long-lived worker process.

The Picamera2 frame callback runs in the server process. While the previous
shot's camera estimators ran there too (8-10 s per shot on the Pi 5), the GIL
let frames reach the callback only every 4.4-5.5 ms instead of every 3.47 ms,
and every capture that overlapped them dropped frames. The pure estimators
therefore run here, in one worker process that is

* started lazily by the first job, from a fresh interpreter (``spawn``), never
  forked from the threaded server that owns Picamera2 and GPIO;
* reniced by +10 like the replay encoder, so the capture keeps the CPU;
* handed every input explicitly. A clip's frames travel through shared memory,
  everything else pickled through the pipe. On a Mac, pickling a 640x400 clip
  cost 26-28 ms (60 frames) and 46-111 ms (120 frames) with 5-16 ms of server
  CPU; shared memory takes 2 ms and 4-6 ms with 1-2 ms of server CPU.

``spawn`` re-imports the server's entry script in the worker under the
``__main__`` guard; that import has no side effects beyond module globals.

State stays in the server. A job receives copies of its stateful arguments
(``STATEFUL_ARGUMENTS``: the ``ReferenceBallTracker`` and the ball flight's
``GroundBallAnchor``) and returns them as the job left them; the server's
helpers adopt that state, so the result and the helper equal an in-process run.

``run_camera_cv`` runs a job in the server process when no worker is
configured (``--camera-cv-in-process``) and whenever the worker fails: a crash,
a failed start or an input or result that cannot cross the pipe logs a warning,
discards the worker so the next job starts a fresh one, and computes this shot
in-process, so the worker never costs a shot its camera result. An exception
the job itself raises is not a worker failure: the estimators are pure
functions of the inputs the worker got, so an in-process run would only raise
it again, after seconds of CV holding the server's GIL. It reaches the caller
as the job's own exception (its worker traceback as the cause), the server's
stateful arguments adopt the state the job left, exactly as after an
in-process run that raised, and the worker keeps running.

A shot job is bounded by the shot's own enrichment deadline
(``deadline_monotonic``), not by a fixed time from the job's start: a worker
that has not answered by then is replaced the same way, but the shot is not
recomputed, since the watchdog has already published it without camera data
and would drop a late result; ``CameraCvWorkerTimeout`` reaches the caller
instead. The deadline keeps running while a job waits for the worker, e.g. for
a start in another thread. Once it has passed, a job is neither sent to the
worker nor recomputed in-process (``CameraCvDeadlinePassed``), so a shot that is
already published never holds up the next one.

A further estimator joins by adding a job function and listing it in ``JOBS``.
A job may bring its own budget (``CameraCvWorker.run(timeout_s=...)``);
the shadow club-head path does, and the server calls the worker directly for
it, without the in-process fallback. ``CameraCvWorker.start`` brings a worker
up ahead of the next job, e.g. in idle time after a job past its budget.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing import shared_memory
from multiprocessing.reduction import ForkingPickler

import numpy as np

logger = logging.getLogger(__name__)

WORKER_NICE = 10
START_METHOD = "spawn"
STOP_TIMEOUT_S = 2.0
# Keyword arguments a job mutates. The worker sends them back as the job left
# them and the server's own objects adopt that state.
STATEFUL_ARGUMENTS = ("ball_tracker", "ground_anchor")
# Arrays at least this large (a clip's frames) travel through shared memory.
SHARED_MEMORY_MIN_BYTES = 1 << 20


def _ball_flight(**kwargs):
    from openflight.camera import ball_flight  # pylint: disable=import-outside-toplevel

    # Flicker compensation first, then the estimator (ball_flight.estimate_live_ball_flight).
    return ball_flight.estimate_live_ball_flight(**kwargs)


def _lowlight_ball_flight(**kwargs):
    from openflight.camera import lowlight  # pylint: disable=import-outside-toplevel

    return lowlight.estimate_lowlight_ball_flight(**kwargs)


def _club_delivery(**kwargs):
    from openflight.camera import club_delivery  # pylint: disable=import-outside-toplevel

    return club_delivery.estimate_chained_delivery(**kwargs)


def _club_head_path(**kwargs):
    from openflight.camera import club_head_path  # pylint: disable=import-outside-toplevel

    return club_head_path.estimate_club_head_path(**kwargs)


def _club_data(**kwargs):
    from openflight.camera import club_head_path  # pylint: disable=import-outside-toplevel

    return club_head_path.estimate_club_data(**kwargs)


# Looked up at call time, so a patched estimator is the one that runs. The server
# runs ``club_data``; ``club_head_path`` stays for the replay tools.
JOBS: dict[str, Callable[..., object]] = {
    "ball_flight": _ball_flight,
    # The low-light shadow start direction (camera.lowlight), after publication.
    "lowlight_ball_flight": _lowlight_ball_flight,
    "club_delivery": _club_delivery,
    "club_head_path": _club_head_path,
    "club_data": _club_data,
}


class CameraCvWorkerError(RuntimeError):
    """The worker process could not deliver a job's result."""


class CameraCvWorkerTimeout(CameraCvWorkerError):
    """The worker did not answer before the job's deadline."""


class CameraCvDeadlinePassed(CameraCvWorkerTimeout):
    """The shot's deadline had passed before the job could run; it did not run."""


class CameraCvJobError(RuntimeError):
    """The job itself raised in the worker; the worker is fine and keeps running.

    ``error`` is the job's exception as unpickled here, None when it could not
    cross the pipe (the message then names it); ``state`` holds the stateful
    arguments as the job left them; the message carries the worker traceback.
    """

    def __init__(self, message: str, *, error: BaseException | None = None, state=None):
        super().__init__(message)
        self.error = error
        self.state = {} if state is None else state


@dataclass(frozen=True)
class _SharedArray:
    """An array the server copied into a shared-memory segment for one job."""

    name: str
    shape: tuple[int, ...]
    dtype: str


def _share_arrays(kwargs: dict, segments: list) -> dict:
    """Swap large arrays for shared-memory copies; the caller releases ``segments``."""
    shared = dict(kwargs)
    for key, value in kwargs.items():
        if (
            isinstance(value, np.ndarray)
            and not value.dtype.hasobject
            and value.nbytes >= SHARED_MEMORY_MIN_BYTES
        ):
            segment = shared_memory.SharedMemory(create=True, size=value.nbytes)
            segments.append(segment)
            view = np.ndarray(value.shape, value.dtype, buffer=segment.buf)
            view[...] = value
            del view
            shared[key] = _SharedArray(segment.name, value.shape, value.dtype.str)
    return shared


def _release(segments: list) -> None:
    for segment in segments:
        segment.close()
        try:
            segment.unlink()
        except FileNotFoundError:
            pass


def _unshare_arrays(kwargs: dict) -> dict:
    """The worker's private copies of the arrays the server put in shared memory."""
    private = dict(kwargs)
    for key, value in kwargs.items():
        if isinstance(value, _SharedArray):
            # Attaching registers the segment again with the resource tracker
            # this spawned process shares with the server, which unlinks it.
            segment = shared_memory.SharedMemory(name=value.name)
            try:
                view = np.ndarray(value.shape, np.dtype(value.dtype), buffer=segment.buf)
                private[key] = view.copy()
                del view
            finally:
                segment.close()
    return private


def _stateful(kwargs: dict) -> dict:
    return {name: kwargs[name] for name in STATEFUL_ARGUMENTS if kwargs.get(name) is not None}


def run_job(job: str, kwargs: dict) -> tuple[object, dict]:
    """Run one job here; return its result and its stateful arguments afterwards."""
    result = JOBS[job](**kwargs)
    return result, _stateful(kwargs)


def _adopt(kwargs: dict, state: dict) -> None:
    """Let the server's stateful arguments take over the state a worker job left."""
    for name, returned in state.items():
        kwargs[name].adopt(returned)


def _time_left_s(job: str, deadline_monotonic: float | None) -> float | None:
    """Seconds left until the shot's deadline, None without one.

    Raises CameraCvDeadlinePassed once the deadline has passed.
    """
    if deadline_monotonic is None:
        return None
    left_s = deadline_monotonic - time.monotonic()
    if left_s <= 0:
        raise CameraCvDeadlinePassed(f"{job}: the shot's deadline passed {-left_s:.2f}s ago")
    return left_s


def run_camera_cv(
    job: str,
    kwargs: dict,
    worker: CameraCvWorker | None,
    *,
    deadline_monotonic: float | None = None,
):
    """Run a job in the worker, or in this process without one or when it fails.

    ``deadline_monotonic`` is the shot's enrichment deadline (``time.monotonic``
    clock). With a worker, the job gets what is left of it instead of the
    worker's own timeout, and once it has passed the job does not run at all:
    CameraCvDeadlinePassed, a CameraCvWorkerTimeout. Without a worker the job
    runs in-process exactly as before the worker, deadline or not.

    Exceptions of the in-process run propagate, exactly as before the worker,
    and so does CameraCvWorkerTimeout. A job that raises in the worker raises
    the same exception here (CameraCvJobError when it cannot cross the pipe),
    without an in-process recompute.
    """
    if worker is not None:
        try:
            timeout_s = _time_left_s(job, deadline_monotonic)
        except CameraCvDeadlinePassed as late:
            logger.warning("[CAMERA-CV] %s; not sent to the worker", late)
            raise
        started = time.monotonic()
        try:
            result, state = worker.run(job, kwargs, timeout_s=timeout_s)
        except CameraCvJobError as raised:
            _adopt(kwargs, raised.state)
            logger.warning(
                "[CAMERA-CV] %s raised in worker pid %s; not recomputed in-process",
                job,
                worker.pid,
            )
            if raised.error is None:
                raise
            raise raised.error from raised
        except CameraCvDeadlinePassed as late:
            logger.warning("[CAMERA-CV] %s; not sent to the worker", late)
            raise
        except CameraCvWorkerTimeout as error:
            logger.warning(
                "[CAMERA-CV] %s: %s; worker replaced, shot past its deadline keeps no result",
                job,
                error,
            )
            raise
        except CameraCvWorkerError as error:
            try:
                _time_left_s(job, deadline_monotonic)
            except CameraCvDeadlinePassed as late:
                logger.warning(
                    "[CAMERA-CV] %s failed in the worker (%s); %s, not computed in-process",
                    job,
                    error,
                    late,
                )
                raise late from error
            logger.warning(
                "[CAMERA-CV] %s failed in the worker (%s); computing this shot in-process",
                job,
                error,
            )
        else:
            _adopt(kwargs, state)
            logger.info(
                "[CAMERA-CV] %s ran in worker pid %s in %.2fs",
                job,
                worker.pid,
                time.monotonic() - started,
            )
            return result
    return JOBS[job](**kwargs)


class CameraCvWorker:
    """One long-lived, low-priority process that runs ``JOBS`` one at a time."""

    def __init__(self, *, timeout_s: float, nice: int = WORKER_NICE):
        self._timeout_s = timeout_s
        # The running job's timeout (the worker's, or the job's own budget).
        self._job_timeout_s = timeout_s
        self._nice = nice
        self._context = multiprocessing.get_context(START_METHOD)
        self._lock = threading.Lock()
        self._process = None
        self._connection = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        """The running worker's process id, None before the first job or after a failure."""
        process = self._process
        return process.pid if process is not None and process.is_alive() else None

    def run(self, job: str, kwargs: dict, *, timeout_s: float | None = None) -> tuple[object, dict]:
        """Run one job in the worker; return its result and its stateful arguments.

        Raises CameraCvJobError when the job itself raised (the worker keeps
        running), CameraCvWorkerError (and discards the worker) when the worker
        cannot start, dies or a job's input or result cannot cross, and
        CameraCvWorkerTimeout when
        starting plus running the job exceeds the timeout: ``timeout_s`` when
        given (a job's own budget, or what is left of the shot's deadline),
        else the worker's. The timeout counts from this call, including any
        wait for another call that holds the worker (a ``start`` in idle time
        spawns and imports for seconds); when that wait alone uses it up, the
        job is not sent and the worker is kept (CameraCvDeadlinePassed).
        """
        budget_s = self._timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget_s
        with self._lock:
            if self._closed:
                raise CameraCvWorkerError("the worker is shut down")
            if time.monotonic() >= deadline:
                raise CameraCvDeadlinePassed(
                    f"{job}: the deadline passed while waiting {budget_s:g}s for the worker"
                )
            segments: list = []
            self._job_timeout_s = budget_s
            try:
                connection = self._ensure_running(deadline)
                self._send(connection, (job, _share_arrays(kwargs, segments)), deadline)
                if not connection.poll(max(0.0, deadline - time.monotonic())):
                    raise self._timed_out()
                status, payload = connection.recv()
            except CameraCvWorkerError:
                self._discard()
                raise
            except Exception as error:  # pylint: disable=broad-exception-caught
                self._discard()
                raise CameraCvWorkerError(f"{type(error).__name__}: {error}") from error
            finally:
                # After the reply, or after a failed worker is gone.
                _release(segments)
            if status == "raised":
                message, error, state = payload
                raise CameraCvJobError(message, error=error, state=state)
            if status != "ok":
                self._discard()
                raise CameraCvWorkerError(payload)
            return payload

    def start(self, *, timeout_s: float | None = None) -> int:
        """Start the worker now unless it runs, so the next job finds it ready; return its pid.

        For idle time, e.g. right after a job past its budget discarded the worker.
        Raises CameraCvWorkerError (and discards the worker) when it cannot start
        within ``timeout_s`` (default: the worker's timeout).
        """
        with self._lock:
            if self._closed:
                raise CameraCvWorkerError("the worker is shut down")
            self._job_timeout_s = self._timeout_s if timeout_s is None else timeout_s
            try:
                self._ensure_running(time.monotonic() + self._job_timeout_s)
            except CameraCvWorkerError:
                self._discard()
                raise
            except Exception as error:  # pylint: disable=broad-exception-caught
                self._discard()
                raise CameraCvWorkerError(f"{type(error).__name__}: {error}") from error
            return self._process.pid

    def close(self) -> None:
        """Stop the worker; a job still in flight is cut off."""
        self._closed = True
        # pylint: disable-next=consider-using-with  # released in the finally below
        if not self._lock.acquire(timeout=0.5):
            # A job holds the lock: kill the process so the waiting call returns.
            self._kill(self._process)
            return
        try:
            connection, process = self._connection, self._process
            if connection is not None and process is not None and process.is_alive():
                try:
                    connection.send(None)
                    process.join(STOP_TIMEOUT_S)
                except (OSError, ValueError):
                    pass
            self._discard()
        finally:
            self._lock.release()

    def _send(self, connection, message, deadline: float) -> None:
        """Send within the job deadline; a worker that stops reading would block forever."""
        errors: list[BaseException] = []

        def send() -> None:
            try:
                connection.send(message)
            except BaseException as error:  # pylint: disable=broad-exception-caught
                errors.append(error)

        sender = threading.Thread(target=send, name="camera-cv-send", daemon=True)
        sender.start()
        sender.join(max(0.0, deadline - time.monotonic()))
        if sender.is_alive():
            # Killing the reader breaks the pipe, which ends the blocked write
            # before the connection (and its descriptor number) is released.
            self._kill(self._process)
            sender.join(STOP_TIMEOUT_S)
            raise self._timed_out()
        if errors:
            raise errors[0]

    def _timed_out(self) -> CameraCvWorkerTimeout:
        return CameraCvWorkerTimeout(f"no answer within {self._job_timeout_s:g}s")

    def _ensure_running(self, deadline: float):
        process = self._process
        if process is not None and process.is_alive() and self._connection is not None:
            return self._connection
        self._discard()
        started = time.monotonic()
        parent, child = self._context.Pipe(duplex=True)
        process = self._context.Process(
            target=_worker_main,
            args=(child, self._nice),
            name="camera-cv-worker",
            daemon=True,
        )
        process.start()
        child.close()
        self._process, self._connection = process, parent
        if not parent.poll(max(0.0, deadline - time.monotonic())):
            raise self._timed_out()
        status, _pid = parent.recv()
        if status != "ready":
            raise CameraCvWorkerError(f"worker sent {status!r} instead of ready")
        logger.info(
            "[CAMERA-CV] Worker pid %s started in %.2fs (%s, nice +%d)",
            process.pid,
            time.monotonic() - started,
            START_METHOD,
            self._nice,
        )
        return parent

    def _discard(self) -> None:
        connection, process = self._connection, self._process
        self._connection = self._process = None
        self._kill(process)
        if connection is not None:
            connection.close()

    @staticmethod
    def _kill(process) -> None:
        if process is None:
            return
        if process.is_alive():
            process.terminate()
            process.join(STOP_TIMEOUT_S)
        if process.is_alive():
            process.kill()
            process.join(STOP_TIMEOUT_S)


def _lower_priority(increment: int) -> None:
    """Renice the whole worker, including threads that imports already started.

    Linux applies nice per thread, so each thread of this process is reniced.
    """
    if increment <= 0:
        return
    try:
        if sys.platform.startswith("linux"):
            for name in os.listdir("/proc/self/task"):
                tid = int(name)
                os.setpriority(
                    os.PRIO_PROCESS, tid, os.getpriority(os.PRIO_PROCESS, tid) + increment
                )
        else:
            os.nice(increment)
    except OSError as error:
        logger.warning("[CAMERA-CV] Could not lower worker priority: %s", error)


def _portable(error: BaseException) -> BaseException | None:
    """``error`` if it survives a pickle round trip (as it must to reach the server), else None."""
    try:
        ForkingPickler.loads(ForkingPickler.dumps(error))
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    return error


def _job_reply(job: str, kwargs: dict) -> tuple[str, object]:
    """``("ok", (result, state))``, or ``("raised", ...)`` when the job itself raised."""
    try:
        return "ok", run_job(job, kwargs)
    except Exception as error:  # pylint: disable=broad-exception-caught
        message = f"{job} raised {type(error).__name__}: {error}\n{traceback.format_exc()}"
        return "raised", (message, _portable(error), _stateful(kwargs))


def _worker_main(connection, nice: int) -> None:
    """Serve jobs until the server sends None or closes the pipe."""
    # Ctrl-C reaches the whole process group; the server decides when to stop.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _lower_priority(nice)
    connection.send(("ready", os.getpid()))
    while True:
        try:
            raw = connection.recv_bytes()
        except (EOFError, OSError):
            return
        job = "job"
        try:
            message = ForkingPickler.loads(raw)
            if message is None:
                return
            job, kwargs = message
            kwargs = _unshare_arrays(kwargs)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # The input did not arrive: a transport failure, not the job's.
            reply = (
                "error",
                f"{job} input could not be read: {type(error).__name__}: {error}\n"
                f"{traceback.format_exc()}",
            )
        else:
            reply = _job_reply(job, kwargs)
        try:
            connection.send(reply)
        except Exception as error:  # pylint: disable=broad-exception-caught
            connection.send(("error", f"{job} result could not be sent: {error!r}"))
