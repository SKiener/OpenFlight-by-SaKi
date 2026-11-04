"""The per-shot camera CV worker process: equivalence, failure fallback and wiring."""

import copy
import logging
import os
import signal
import threading
import time
from dataclasses import replace
from datetime import datetime
from multiprocessing import shared_memory
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from test_camera_ball_contrast import SOUND_TRIGGER_NS, _render_flight
from test_camera_club_delivery import _synthetic_capture
from test_club_head_path import (
    CLUB_SPEED_MPH as CLUB_CLIP_SPEED_MPH,
    GEOMETRY as CLUB_GEOMETRY,
    Scenario as ClubScenario,
    render_clip as render_club_clip,
)

from openflight import server
from openflight.camera import cv_worker
from openflight.camera.ball_flight import CameraBallGeometry, CameraTimeZero
from openflight.camera.club_delivery import CameraDeliveryGeometry, ReferenceBallTracker
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.cv_worker import (
    CameraCvDeadlinePassed,
    CameraCvJobError,
    CameraCvWorker,
    CameraCvWorkerError,
    CameraCvWorkerTimeout,
    run_camera_cv,
)
from openflight.clubs import ClubType
from openflight.iwr6843.club import ClubRangeEvidence
from openflight.iwr6843.lcmf import BallRangeEvidence
from openflight.iwr6843.tracking import BallTrack, Geometry
from openflight.launch_monitor import Shot

FLIGHT_TIMES_S = 0.007 + 0.0035 * np.arange(8)


def _flight_clip():
    """A backlit synthetic flight the real ball detector finds, with picklable IWR range."""
    frames, timestamps, geometry, _anchor, synthetic_evidence, speed_ms = _render_flight(210, 35)
    radar = Geometry(
        n_frames=10,
        chirps_per_frame=96,
        n_tx=3,
        n_rx=4,
        n_samples=256,
        frame_period_s=0.001,
        trigger_frame=2,
    )
    bins = [
        synthetic_evidence.track.range_at(t, radar.range_res_m) / radar.range_res_m
        for t in FLIGHT_TIMES_S
    ]
    slope, intercept = np.polyfit(FLIGHT_TIMES_S, bins, 1)
    evidence = BallRangeEvidence(
        track=BallTrack(
            speed_ms=speed_ms,
            slope_bins=float(slope),
            intercept_bins=float(intercept),
            rms_bins=0.1,
            n_inliers=8,
            t_first=0.0,
            t_last=0.03,
            low_confidence=False,
        ),
        geometry=radar,
        impact_t_s=0.0,
    )
    return frames, timestamps, geometry, evidence, speed_ms


def _delivery_geometry(geometry: CameraBallGeometry) -> CameraDeliveryGeometry:
    return CameraDeliveryGeometry(
        camera_height_m=geometry.camera_height_m,
        radar_height_m=geometry.radar_height_m,
        tee_range_m=geometry.tee_range_m,
        ball_height_m=geometry.ball_height_m,
        image_width_px=geometry.image_width_px,
        image_height_px=geometry.image_height_px,
    )


def _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, tracker):
    return {
        "frames": frames,
        "timestamps_ns": timestamps,
        "trigger_ns": SOUND_TRIGGER_NS,
        "range_evidence": evidence,
        "geometry": geometry,
        "ops_ball_speed_mph": speed_ms * 2.23694,
        "iwr_vertical_deg": 20.0,
        "ball_tracker": tracker,
    }


def _club_kwargs(frames, timestamps, geometry, tracker, trigger_index=19):
    return {
        "frames": frames,
        "host_timestamp_ns": timestamps,
        "trigger_index": trigger_index,
        "range_evidence": None,
        "geometry": _delivery_geometry(geometry),
        "ops_club_speed_mph": 80.0,
        "ball_tracker": tracker,
    }


# Beyond the tracker's consistency radius for the 15 px ball (max(40, 4 x 15) px).
RELOCATED_PX = 80


def _shots():
    """Seven shots that walk the reference-ball tracker through all of its state.

    The clip, a re-teed ball 6 px left, the clip again (three samples: the
    anchor), a ball 80 px left twice (a relocation candidate, then two: fewer
    than the three that move the anchor), the clip again (back on the anchor,
    candidates dropped), an empty mat (the anchor fallback).
    """
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    shifted = np.roll(frames, -6, axis=2)
    relocated = np.roll(frames, -RELOCATED_PX, axis=2)
    empty = np.full_like(frames, 210)
    return [
        (clip, timestamps, geometry, evidence, speed_ms)
        for clip in (frames, shifted, frames, relocated, relocated, frames, empty)
    ]


def _tracker_state(tracker: ReferenceBallTracker):
    return tracker._samples, tracker._relocation  # pylint: disable=protected-access


@pytest.fixture(scope="module")
def worker():
    worker = CameraCvWorker(timeout_s=60.0)
    yield worker
    worker.close()


def test_worker_results_and_tracker_state_equal_the_in_process_run(worker):
    local_ball, local_club = ReferenceBallTracker(), ReferenceBallTracker()
    remote_ball, remote_club = ReferenceBallTracker(), ReferenceBallTracker()
    relocation_candidates = []

    for frames, timestamps, geometry, evidence, speed_ms in _shots():
        expected = run_camera_cv(
            "ball_flight",
            _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, local_ball),
            None,
        )
        actual = run_camera_cv(
            "ball_flight",
            _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, remote_ball),
            worker,
        )
        expected_club = run_camera_cv(
            "club_delivery", _club_kwargs(frames, timestamps, geometry, local_club), None
        )
        actual_club = run_camera_cv(
            "club_delivery", _club_kwargs(frames, timestamps, geometry, remote_club), worker
        )

        assert actual == expected
        assert actual_club == expected_club
        assert _tracker_state(remote_ball) == _tracker_state(local_ball)
        assert _tracker_state(remote_club) == _tracker_state(local_club)
        relocation_candidates.append(
            (len(local_ball._relocation), len(local_club._relocation))  # pylint: disable=protected-access
        )

    # The sequence exercised the full sweep, the re-teed ball, relocation candidates
    # carried from shot to shot, their drop back on the anchor and the anchor fallback.
    assert worker.pid is not None and worker.pid != os.getpid()
    assert relocation_candidates == [(0, 0)] * 3 + [(1, 1), (2, 2)] + [(0, 0)] * 2
    assert len(local_ball._samples) == 4  # pylint: disable=protected-access


def test_adopt_copies_both_tracker_lists_without_sharing_them():
    source = ReferenceBallTracker()
    outlier = ReferenceBall(300.0 + 2 * RELOCATED_PX, 250.0, 15.0, 150)
    for ball in (*(ReferenceBall(x_px, 250.0, 15.0, 150) for x_px in (300, 301, 299)), outlier):
        source.resolve(ball)
    samples, relocation = (list(items) for items in _tracker_state(source))
    assert (len(samples), len(relocation)) == (3, 1)

    adopted = ReferenceBallTracker()
    adopted.adopt(source)
    source.resolve(outlier)  # a second candidate, in the source only

    assert _tracker_state(adopted) == (samples, relocation)
    assert len(source._relocation) == 2  # pylint: disable=protected-access


def _club_sweep_kwargs(tracker, *, with_iwr_range: bool):
    """A synthetic club sweep the chained delivery measures, with or without IWR club range."""
    frames, timestamps = _synthetic_capture(club_velocity_px=(20.0, -6.0))
    radar = Geometry(
        n_frames=10,
        chirps_per_frame=96,
        n_tx=3,
        n_rx=4,
        n_samples=256,
        frame_period_s=0.001,
        trigger_frame=2,
    )
    speed_ms = 80.0 / 2.23694
    evidence = ClubRangeEvidence(
        track=BallTrack(
            speed_ms=speed_ms,
            slope_bins=speed_ms / radar.range_res_m,
            intercept_bins=1.524 / radar.range_res_m,
            rms_bins=0.1,
            n_inliers=8,
            t_first=-0.01,
            t_last=0.01,
            low_confidence=False,
        ),
        geometry=radar,
        impact_t_s=0.0,
    )
    return {
        "frames": frames,
        "host_timestamp_ns": timestamps,
        "trigger_index": 30,
        "range_evidence": evidence if with_iwr_range else None,
        "geometry": CameraDeliveryGeometry(
            camera_height_m=0.2032,
            radar_height_m=0.1524,
            tee_range_m=1.524,
            ball_height_m=0.04,
            image_width_px=640,
            image_height_px=400,
        ),
        "ops_club_speed_mph": 80.0,
        "ball_tracker": tracker,
    }


@pytest.mark.parametrize("with_iwr_range", [True, False], ids=["iwr_range", "camera_ops"])
def test_a_measured_club_delivery_crosses_the_worker_unchanged(worker, with_iwr_range):
    local, remote = ReferenceBallTracker(), ReferenceBallTracker()

    expected = run_camera_cv(
        "club_delivery", _club_sweep_kwargs(local, with_iwr_range=with_iwr_range), None
    )
    actual = run_camera_cv(
        "club_delivery", _club_sweep_kwargs(remote, with_iwr_range=with_iwr_range), worker
    )

    # A delivery with numbers, not a rejection, so path and attack angle cross the pipe.
    assert not expected.status.startswith("rejected"), expected.status
    assert expected.club_path_deg is not None and expected.attack_angle_deg is not None
    assert actual == expected
    assert (actual.status, actual.club_path_deg, actual.attack_angle_deg) == (
        expected.status,
        expected.club_path_deg,
        expected.attack_angle_deg,
    )
    assert _tracker_state(remote) == _tracker_state(local)


def test_club_data_with_attack_and_impact_crosses_the_worker_unchanged(worker):
    """The server's club-data job: contact, departure, path, attack and impact all pickle."""
    # Bright on dark: the other scene's sky edge out-contrasts the ball in refine_teed_ball.
    frames, timestamps, contact_s, ball = render_club_clip(
        ClubScenario(attack_deg=-4.0, path_deg=2.0, bright_head=True)
    )
    trigger_ns = int(round((contact_s + 1.5 / 343.0 + 0.0012) * 1e9))
    departure_ms = 1e3 * (contact_s - trigger_ns / 1e9)

    def kwargs(tracker):
        return {
            "frames": frames,
            "timestamps_ns": timestamps,
            "time_zero": cv_worker_time_zero(departure_ms),
            "trigger_ns": trigger_ns,
            "tee_range_m": 1.5,
            "geometry": CLUB_GEOMETRY,
            "club_speed_mph": CLUB_CLIP_SPEED_MPH,
            "club": ClubType.IRON_8,
            "handedness": "right",
            "ball_tracker": tracker,
            "departed_ball": ball,
        }

    local, remote = ReferenceBallTracker(), ReferenceBallTracker()
    expected = run_camera_cv("club_data", kwargs(local), None)
    actual = run_camera_cv("club_data", kwargs(remote), worker)

    assert expected.path.status == "accepted"
    assert expected.path.attack is not None and expected.path.attack.accepted
    assert expected.path.impact is not None and expected.path.impact.accepted
    assert actual.contact == expected.contact
    assert actual.camera_departure == expected.camera_departure
    assert actual.path == expected.path
    assert set(actual.stage_elapsed_s) == {"departure", "path", "attack", "impact", "impact_point"}
    assert _tracker_state(remote) == _tracker_state(local)


def cv_worker_time_zero(departure_ms: float):
    """A live departure-fit time zero (ms from the trigger)."""
    return CameraTimeZero(
        source="departure_fit",
        time_zero_ms=departure_ms,
        departure_fit_ms=departure_ms,
        acoustic_ms=-4.373,
        tee_patch_ms=None,
        iwr_impact_t_s=None,
    )


def test_first_flight_shot_runs_the_full_sweep(worker):
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()

    result = run_camera_cv(
        "ball_flight",
        _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, ReferenceBallTracker()),
        worker,
    )

    assert result.status == "accepted"
    assert result.support == 27
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)


def prepare_fusion(monkeypatch, tmp_path: Path, worker_or_none):
    """Server globals and a saved synthetic flight clip; returns the shot and its capture."""
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    capture_dir = tmp_path / ("worker" if worker_or_none else "in_process")
    capture_dir.mkdir()
    np.savez(
        capture_dir / "frames.npz",
        frames=frames,
        host_timestamp_ns=timestamps,
        trigger_host_timestamp_ns=np.int64(SOUND_TRIGGER_NS),
        pre_trigger_count=np.int32(20),
    )
    monkeypatch.setattr(server, "camera_cv_worker", worker_or_none)
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    monkeypatch.setattr(server, "camera_intrinsics", None)
    monkeypatch.setattr(server, "camera_ball_flight_reference_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_reference_ball_tracker", ReferenceBallTracker())
    monkeypatch.setattr(
        server,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=geometry.tee_range_m,
                radar_height_m=geometry.radar_height_m,
                tee_ball_height_m=geometry.ball_height_m,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "camera_capture_config",
        {"mount_height_m": geometry.camera_height_m, "width": 640, "height": 400},
    )
    shot = Shot(
        ball_speed_mph=speed_ms * 2.23694,
        club_speed_mph=80.0,
        timestamp=datetime(2026, 10, 19, 12, 0, 0),
        launch_angle_vertical=20.0,
        launch_angle_horizontal=1.0,
        launch_angle_horizontal_confidence=0.8,
        launch_angle_horizontal_source="radar",
        iwr6843_horizontal_deg=1.0,
        iwr6843_horizontal_confidence=0.8,
        iwr6843_ball_range_evidence=evidence,
    )
    return shot, SimpleNamespace(valid=True, path=capture_dir)


def _fuse_one_shot(monkeypatch, tmp_path: Path, worker_or_none):
    shot, capture = prepare_fusion(monkeypatch, tmp_path, worker_or_none)
    server._fuse_camera_measurements(shot, capture)
    return (
        server.shot_to_dict(shot),
        _tracker_state(server.camera_ball_flight_reference_tracker),
        _tracker_state(server.camera_reference_ball_tracker),
    )


def test_server_fusion_through_the_worker_equals_the_in_process_fusion(
    monkeypatch, tmp_path, worker
):
    in_process = _fuse_one_shot(monkeypatch, tmp_path, None)
    through_worker = _fuse_one_shot(monkeypatch, tmp_path, worker)

    assert through_worker == in_process
    assert in_process[0]["launch_angle_horizontal_source"] == "camera_assisted_experimental"


class _KillsTheWorker(ReferenceBallTracker):
    """A normal tracker here; unpickling it ends the worker process at once."""

    def __reduce__(self):
        return (os._exit, (17,))


def _unreadable():
    raise RuntimeError("input could not be rebuilt")


class _UnreadableInTheWorker(ReferenceBallTracker):
    """A normal tracker here; unpickling it in the worker raises, so the job never starts."""

    def __reduce__(self):
        return (_unreadable, ())


class _RaisesAfterResolve(ReferenceBallTracker):
    """Takes the observation, then raises: an estimator bug after the tracker moved."""

    def resolve(self, candidate):
        super().resolve(candidate)
        raise RuntimeError("estimator bug")


class _TwoArgumentError(Exception):
    """Pickles, but cannot be rebuilt from its message alone."""

    def __init__(self, what, why):
        super().__init__(f"{what}: {why}")


class _RaisesUnportable(ReferenceBallTracker):
    def resolve(self, candidate):
        raise _TwoArgumentError("estimator", "bug")


def _record_shared_segments(monkeypatch) -> list[str]:
    names = []
    share = cv_worker._share_arrays

    def recording(kwargs, segments):
        shared = share(kwargs, segments)
        names.extend(segment.name for segment in segments)
        return shared

    monkeypatch.setattr(cv_worker, "_share_arrays", recording)
    return names


def _assert_released(names):
    for name in names:
        with pytest.raises(FileNotFoundError):
            shared_memory.SharedMemory(name=name)


def test_the_clip_travels_through_shared_memory_that_is_released_after_the_job(monkeypatch, worker):
    names = _record_shared_segments(monkeypatch)
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    kwargs = _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, None)

    assert run_camera_cv("ball_flight", kwargs, worker) == run_camera_cv(
        "ball_flight", kwargs, None
    )

    # Only the frames are large enough; timestamps and the rest are pickled.
    assert len(names) == 1
    _assert_released(names)


@pytest.mark.parametrize("tracker_type", [_KillsTheWorker, _UnreadableInTheWorker])
def test_a_failed_worker_falls_back_in_process_and_restarts(monkeypatch, caplog, tracker_type):
    names = _record_shared_segments(monkeypatch)
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    expected_tracker = ReferenceBallTracker()
    expected = run_camera_cv(
        "ball_flight",
        _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, expected_tracker),
        None,
    )
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        tracker = tracker_type()
        with caplog.at_level(logging.WARNING, logger=cv_worker.__name__):
            result = run_camera_cv(
                "ball_flight",
                _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, tracker),
                worker,
            )

        assert result == expected
        # Updated exactly once, by the in-process run.
        assert _tracker_state(tracker) == _tracker_state(expected_tracker)
        assert "computing this shot in-process" in caplog.text
        assert worker.pid is None
        _assert_released(names)

        again = run_camera_cv(
            "ball_flight",
            _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, None),
            worker,
        )
        assert again == expected
        assert worker.pid is not None
    finally:
        worker.close()


def test_a_hung_worker_is_replaced_and_the_late_shot_not_recomputed(monkeypatch, caplog):
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    kwargs = _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, None)
    expected = run_camera_cv("ball_flight", kwargs, None)
    # Generous for the starts, so only the hung job meets the short deadline.
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        assert run_camera_cv("ball_flight", kwargs, worker) == expected
        hung_pid = worker.pid
        os.kill(hung_pid, signal.SIGSTOP)
        in_process = []
        monkeypatch.setitem(cv_worker.JOBS, "ball_flight", lambda **_kw: in_process.append(1))
        deadline = time.monotonic() + 1.0
        with caplog.at_level(logging.WARNING, logger=cv_worker.__name__):
            with pytest.raises(CameraCvWorkerTimeout, match=r"no answer within 0\.9\d*s"):
                run_camera_cv("ball_flight", kwargs, worker, deadline_monotonic=deadline)

        # Cut off at the shot's deadline, not a fixed time after the job started.
        assert deadline <= time.monotonic() < deadline + 2 * cv_worker.STOP_TIMEOUT_S + 1.0
        # Past the shot deadline the watchdog has published the shot already.
        assert in_process == []
        assert "worker replaced" in caplog.text
        assert worker.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(hung_pid, 0)
        monkeypatch.undo()
        assert run_camera_cv("ball_flight", kwargs, worker) == expected
    finally:
        worker.close()


def test_a_shot_past_its_deadline_is_neither_sent_to_the_worker_nor_computed_in_process(
    monkeypatch, caplog, worker
):
    pid = worker.start()
    monkeypatch.setitem(cv_worker.JOBS, "ball_flight", lambda **_kw: pytest.fail("in-process"))

    with caplog.at_level(logging.WARNING, logger=cv_worker.__name__):
        with pytest.raises(CameraCvDeadlinePassed, match="deadline passed"):
            run_camera_cv("ball_flight", {}, worker, deadline_monotonic=time.monotonic() - 0.5)

    assert issubclass(CameraCvDeadlinePassed, CameraCvWorkerTimeout)
    assert "not sent to the worker" in caplog.text
    assert worker.pid == pid  # the healthy worker is kept


def test_a_shot_job_gets_what_is_left_of_the_shot_deadline():
    from openflight.camera.ball_flight import CameraBallEstimate

    fake = _RecordingWorker(CameraBallEstimate(status="rejected_test"))
    kwargs = {"ball_tracker": ReferenceBallTracker()}

    run_camera_cv("ball_flight", kwargs, fake, deadline_monotonic=time.monotonic() + 5.0)
    run_camera_cv("ball_flight", kwargs, fake)

    with_deadline, without_deadline = fake.timeouts
    assert 4.0 < with_deadline <= 5.0
    assert without_deadline is None  # the worker's own timeout


def _hold_the_worker_lock(worker, hold_s: float) -> threading.Thread:
    """A fake start in another thread (spawn + import) that holds the worker for hold_s."""
    held = threading.Event()

    def fake_start():
        with worker._lock:  # pylint: disable=protected-access
            held.set()
            time.sleep(hold_s)

    thread = threading.Thread(target=fake_start, daemon=True)
    thread.start()
    assert held.wait(5.0)
    return thread


def test_a_job_waiting_for_a_start_is_cut_off_at_its_own_deadline(monkeypatch):
    worker = CameraCvWorker(timeout_s=60.0)
    polls = []

    def poll(timeout):
        polls.append(timeout)
        time.sleep(timeout)
        return False  # a worker that never answers

    connection = SimpleNamespace(poll=poll)
    monkeypatch.setattr(worker, "_ensure_running", lambda _deadline: connection)
    monkeypatch.setattr(worker, "_send", lambda _connection, _message, _deadline: None)
    start = _hold_the_worker_lock(worker, 0.6)
    called = time.monotonic()

    with pytest.raises(CameraCvWorkerTimeout, match=r"no answer within 1s"):
        worker.run("ball_flight", {}, timeout_s=1.0)

    # The 0.6 s spent waiting for the start counts: cut off 1 s after the call.
    assert time.monotonic() - called < 1.0 + 0.2
    assert polls[0] <= 1.0 - 0.6 + 0.05
    start.join(5.0)
    worker.close()


def test_a_shot_whose_deadline_passes_waiting_for_a_start_is_not_sent(monkeypatch, caplog):
    worker = CameraCvWorker(timeout_s=60.0)
    alive = SimpleNamespace(pid=4242, is_alive=lambda: True)
    worker._process, worker._connection = alive, object()  # pylint: disable=protected-access
    monkeypatch.setattr(worker, "_send", lambda *_a: pytest.fail("sent to the worker"))
    monkeypatch.setitem(cv_worker.JOBS, "ball_flight", lambda **_kw: pytest.fail("in-process"))
    start = _hold_the_worker_lock(worker, 0.5)
    deadline = time.monotonic() + 0.2

    with caplog.at_level(logging.WARNING, logger=cv_worker.__name__):
        with pytest.raises(CameraCvDeadlinePassed, match="deadline passed while waiting"):
            run_camera_cv("ball_flight", {}, worker, deadline_monotonic=deadline)

    assert "not sent to the worker" in caplog.text
    assert "worker replaced" not in caplog.text
    assert worker.pid == 4242  # the worker the start brought up is kept
    start.join(5.0)
    worker._process = worker._connection = None  # pylint: disable=protected-access
    worker.close()


@pytest.mark.parametrize("past_deadline", [False, True], ids=["in_time", "past_deadline"])
def test_a_failed_worker_is_recomputed_in_process_only_before_the_deadline(
    monkeypatch, past_deadline
):
    def die(_job, _kwargs, *, timeout_s=None):
        time.sleep(0.1)
        raise CameraCvWorkerError("worker died")

    computed = []
    monkeypatch.setitem(
        cv_worker.JOBS, "ball_flight", lambda **_kw: computed.append(1) or "in-process"
    )
    deadline = time.monotonic() + (0.05 if past_deadline else 30.0)
    worker = SimpleNamespace(pid=None, run=die)

    if past_deadline:
        with pytest.raises(CameraCvDeadlinePassed):
            run_camera_cv("ball_flight", {}, worker, deadline_monotonic=deadline)
        assert computed == []
    else:
        assert run_camera_cv("ball_flight", {}, worker, deadline_monotonic=deadline) == (
            "in-process"
        )
        assert computed == [1]


def test_a_job_that_raises_in_the_worker_propagates_without_a_recompute(
    monkeypatch, caplog, worker
):
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    expected_tracker = _RaisesAfterResolve()
    with pytest.raises(RuntimeError, match="estimator bug"):
        run_camera_cv(
            "ball_flight",
            _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, expected_tracker),
            None,
        )
    pid = worker.start()
    monkeypatch.setitem(cv_worker.JOBS, "ball_flight", lambda **_kw: pytest.fail("recomputed"))
    tracker = _RaisesAfterResolve()

    with caplog.at_level(logging.WARNING, logger=cv_worker.__name__):
        with pytest.raises(RuntimeError, match="estimator bug") as raised:
            run_camera_cv(
                "ball_flight",
                _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, tracker),
                worker,
            )

    # The job's own exception, with the worker's traceback as its cause.
    assert type(raised.value) is RuntimeError
    cause = raised.value.__cause__
    assert isinstance(cause, CameraCvJobError)
    assert "ball_flight raised RuntimeError: estimator bug" in str(cause)
    assert "Traceback" in str(cause) and "in resolve" in str(cause)
    assert "not recomputed in-process" in caplog.text
    assert worker.pid == pid  # the worker was fine and keeps running
    # The tracker moved exactly as far as in the in-process run that raised.
    assert _tracker_state(tracker) == _tracker_state(expected_tracker)
    assert len(tracker._samples) == 1  # pylint: disable=protected-access


def test_an_exception_that_cannot_cross_the_pipe_arrives_as_a_job_error(worker):
    frames, timestamps, geometry, evidence, speed_ms = _flight_clip()
    pid = worker.start()

    with pytest.raises(CameraCvJobError, match="_TwoArgumentError: estimator: bug") as raised:
        run_camera_cv(
            "ball_flight",
            _ball_kwargs(frames, timestamps, geometry, evidence, speed_ms, _RaisesUnportable()),
            worker,
        )

    assert raised.value.error is None
    assert worker.pid == pid


def test_an_unknown_job_raises_its_key_error_and_keeps_the_worker(worker):
    pid = worker.start()

    with pytest.raises(KeyError, match="no_such_job"):
        run_camera_cv("no_such_job", {}, worker)

    assert worker.pid == pid


def test_close_stops_the_worker_and_refuses_further_jobs():
    worker = CameraCvWorker(timeout_s=60.0)
    kwargs = {"frames": np.zeros((3, 4, 4), np.uint8), "timestamps_ns": np.arange(2)}
    kwargs.update(trigger_ns=0, range_evidence=None, geometry=None, ops_ball_speed_mph=1.0)
    result, _state = worker.run("ball_flight", kwargs)
    assert result.status == "rejected_invalid_camera_frames"
    pid = worker.pid

    worker.close()

    assert worker.pid is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(CameraCvWorkerError, match="shut down"):
        worker.run("ball_flight", kwargs)


def test_start_brings_the_worker_up_once_and_the_next_job_reuses_it():
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        pid = worker.start()

        assert worker.pid == pid
        assert worker.start() == pid  # already running: nothing restarted
        kwargs = {"frames": np.zeros((3, 4, 4), np.uint8), "timestamps_ns": np.arange(2)}
        kwargs.update(trigger_ns=0, range_evidence=None, geometry=None, ops_ball_speed_mph=1.0)
        worker.run("ball_flight", kwargs)
        assert worker.pid == pid
    finally:
        worker.close()
    with pytest.raises(CameraCvWorkerError, match="shut down"):
        worker.start()


def test_a_start_that_does_not_finish_in_time_discards_the_worker():
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        # A fresh interpreter cannot report ready within a millisecond.
        with pytest.raises(CameraCvWorkerTimeout, match=r"no answer within 0\.001s"):
            worker.start(timeout_s=0.001)
        assert worker.pid is None
        assert worker.start() is not None  # the next start succeeds
    finally:
        worker.close()


def test_the_worker_runs_reniced(worker):
    if not hasattr(os, "getpriority"):
        pytest.skip("no process priorities on this platform")
    run_camera_cv(
        "ball_flight",
        {
            "frames": np.zeros((3, 4, 4), np.uint8),
            "timestamps_ns": np.arange(2),
            "trigger_ns": 0,
            "range_evidence": None,
            "geometry": None,
            "ops_ball_speed_mph": 1.0,
        },
        worker,
    )

    assert os.getpriority(os.PRIO_PROCESS, worker.pid) == min(
        19, os.getpriority(os.PRIO_PROCESS, 0) + cv_worker.WORKER_NICE
    )


class _RecordingWorker:
    """Stands in for the process: records the job, returns a tracker copy it moved."""

    pid = 4242

    def __init__(self, result):
        self.result = result
        self.calls = []
        self.timeouts = []

    def run(self, job, kwargs, *, timeout_s=None):
        self.calls.append((job, kwargs))
        self.timeouts.append(timeout_s)
        tracker = copy.deepcopy(kwargs["ball_tracker"])
        tracker.resolve(ReferenceBall(300.0, 250.0, 15.0, 150))
        return self.result, {"ball_tracker": tracker}


def _tiny_capture(monkeypatch, tmp_path):
    """Server globals for a fusion run on an 8-frame 4x4 clip; returns the capture."""
    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=np.arange(8, dtype=np.int64),
        trigger_host_timestamp_ns=np.int64(3),
        pre_trigger_count=np.int32(4),
    )
    monkeypatch.setattr(server, "camera_ball_flight_reference_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_reference_ball_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    monkeypatch.setattr(
        server,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=1.524, radar_height_m=0.15875, tee_ball_height_m=0.04
            )
        ),
    )
    monkeypatch.setattr(
        server, "camera_capture_config", {"mount_height_m": 0.20955, "width": 4, "height": 4}
    )
    return SimpleNamespace(valid=True, path=tmp_path)


def test_server_fusion_sends_both_estimators_to_the_configured_worker(monkeypatch, tmp_path):
    from openflight.camera.ball_flight import CameraBallEstimate
    from openflight.camera.club_delivery import ChainedDelivery

    capture = _tiny_capture(monkeypatch, tmp_path)
    jobs = []
    for job, result in (
        ("ball_flight", CameraBallEstimate(status="rejected_test")),
        ("club_delivery", ChainedDelivery(status="rejected_test")),
    ):
        fake = _RecordingWorker(result)
        monkeypatch.setattr(server, "camera_cv_worker", fake)
        shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)
        if job == "ball_flight":
            server._fuse_camera_ball_flight(shot, capture)
        else:
            server._fuse_camera_club_delivery(shot, capture)
        jobs.extend(name for name, _kwargs in fake.calls)
        assert fake.calls[0][1]["frames"].shape == (8, 4, 4)

    assert jobs == ["ball_flight", "club_delivery"]
    # The live trackers adopted what the worker's copies observed.
    assert len(server.camera_ball_flight_reference_tracker._samples) == 1
    assert len(server.camera_reference_ball_tracker._samples) == 1


def test_a_worker_timeout_marks_the_late_shot_as_an_error_without_recomputing(
    monkeypatch, tmp_path
):
    capture = _tiny_capture(monkeypatch, tmp_path)

    def time_out(_job, _kwargs, *, timeout_s=None):
        raise CameraCvWorkerTimeout("no answer within 20s")

    monkeypatch.setattr(server, "camera_cv_worker", SimpleNamespace(pid=None, run=time_out))
    monkeypatch.setitem(cv_worker.JOBS, "ball_flight", lambda **_kw: pytest.fail("recomputed"))
    monkeypatch.setitem(cv_worker.JOBS, "club_delivery", lambda **_kw: pytest.fail("recomputed"))
    shot = Shot(
        ball_speed_mph=110.0,
        timestamp=datetime.now(),
        club_speed_mph=85.0,
        launch_angle_horizontal=2.0,
        launch_angle_horizontal_source="radar",
    )

    server._fuse_camera_ball_flight(shot, capture)
    server._fuse_camera_club_delivery(shot, capture)

    assert shot.experimental_camera_horizontal_status == "error"
    assert shot.experimental_fused_status == "error"
    assert shot.launch_angle_horizontal == 2.0


@pytest.mark.parametrize("with_deadline", [True, False], ids=["shot_deadline", "shared_budget"])
def test_a_ball_flight_cut_off_at_the_deadline_sends_no_club_delivery_job(
    monkeypatch, tmp_path, with_deadline
):
    capture = _tiny_capture(monkeypatch, tmp_path)
    jobs = []

    def hang_until_the_deadline(job, _kwargs, *, timeout_s=None):
        jobs.append(job)
        time.sleep(timeout_s)
        raise CameraCvWorkerTimeout(f"no answer within {timeout_s:g}s")

    monkeypatch.setattr(
        server, "camera_cv_worker", SimpleNamespace(pid=None, run=hang_until_the_deadline)
    )
    monkeypatch.setitem(cv_worker.JOBS, "club_delivery", lambda **_kw: pytest.fail("recomputed"))
    # Without a shot deadline, both jobs share one worker timeout from the fusion's start.
    monkeypatch.setattr(server, "_CAMERA_CV_WORKER_TIMEOUT_S", 0.3)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)
    started = time.monotonic()

    server._fuse_camera_measurements(
        shot, capture, deadline_monotonic=started + 0.3 if with_deadline else None
    )

    assert jobs == ["ball_flight"]
    assert time.monotonic() - started < 2.0
    assert shot.experimental_camera_horizontal_status == "error"
    assert shot.experimental_fused_status == "rejected_shot_past_deadline"


def test_enrichment_hands_camera_fusion_the_registered_shot_deadline(monkeypatch):
    deadlines = []
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    monkeypatch.setattr(server, "_snapshot_inclinometer_for_shot", lambda _shot: None)
    monkeypatch.setattr(server, "_process_iwr6843_angle", lambda _shot: None)
    monkeypatch.setattr(server, "_attach_camera_replay", lambda *_args: None)
    monkeypatch.setattr(
        server,
        "_fuse_camera_measurements",
        lambda _shot, _capture, **kwargs: deadlines.append(kwargs["deadline_monotonic"]),
    )
    registered = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    registered.shot_number = 9001
    deadline = time.monotonic() + 12.0
    monkeypatch.setitem(
        server._shot_finalization_registered,
        9001,
        server._RegisteredShotFinalization(
            shot=registered, emit_event="shot", initial_ui_ms=None, deadline_monotonic=deadline
        ),
    )
    unregistered = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    unregistered.shot_number = 9002

    # Enrichment works on a copy of the registered shot; the number links the two.
    server._enrich_shot_from_optional_hardware(replace(registered))
    server._enrich_shot_from_optional_hardware(unregistered)

    assert deadlines == [deadline, None]


class _FakeCameraRuntime:
    def __init__(self, **kwargs):
        self.settings = kwargs["settings"]

    def start(self):
        return None

    def set_ball_mark_source(self, provider):
        self.ball_mark_source = provider


def _init_camera(monkeypatch, tmp_path, **kwargs):
    monkeypatch.setattr(
        "openflight.camera.capture_runtime.CameraCaptureRuntime", _FakeCameraRuntime
    )
    for name in (
        "camera_capture_runtime",
        "camera_replay_manager",
        "camera_reference_ball_tracker",
        "camera_ball_flight_reference_tracker",
        "camera_ball_flight_ground_anchor",
        "camera_cv_worker",
    ):
        monkeypatch.setattr(server, name, None)
    monkeypatch.setattr(server, "camera_capture_config", {"enabled": False})
    kwargs = {"post_ms": 50.0, **kwargs}
    assert server.init_camera_capture(
        output_dir=tmp_path,
        gpio_pin=17,
        width=640,
        height=400,
        fps=288.0,
        pre_ms=150.0,
        exposure_us=200,
        gain=2.0,
        stream="raw",
        rotate_180=False,
        mirror_horizontal=False,
        roll_correction_deg=0.0,
        scaler_crop=None,
        mount_height_m=0.2,
        lateral_offset_m=0.0,
        horizontal_offset_deg=0.0,
        use_gpio_trigger=False,
        **kwargs,
    )


def test_camera_init_prepares_a_worker_without_starting_it(monkeypatch, tmp_path):
    _init_camera(monkeypatch, tmp_path)

    assert isinstance(server.camera_cv_worker, CameraCvWorker)
    assert server.camera_cv_worker.pid is None
    assert server.camera_capture_runtime.settings.buffer_count == 8


def test_camera_cv_in_process_flag_keeps_the_cv_in_the_server(monkeypatch, tmp_path):
    _init_camera(monkeypatch, tmp_path, cv_in_process=True, buffer_count=24)

    assert server.camera_cv_worker is None
    assert server.camera_capture_runtime.settings.buffer_count == 24


def test_shutdown_stops_the_camera_cv_worker(monkeypatch):
    closed = []
    monkeypatch.setattr(server, "shutdown_cleanup_started", False)
    for name in (
        "kld7_vertical",
        "kld7_horizontal",
        "inclinometer_service",
        "trigger_gate_listener",
        "iwr6843_runtime",
        "trigger_gate",
        "power_monitor",
        "camera_capture_runtime",
    ):
        monkeypatch.setattr(server, name, None)
    monkeypatch.setattr(server, "sim_connectors", [])
    monkeypatch.setattr(server, "stop_monitor", lambda: None)
    monkeypatch.setattr(server, "camera_cv_worker", SimpleNamespace(close=lambda: closed.append(1)))

    assert server._cleanup_hardware_for_shutdown()

    assert closed == [1]


def _main_args(monkeypatch, tmp_path, *extra):
    init_calls = []
    monkeypatch.setattr(
        "sys.argv",
        [
            "openflight-server",
            "--no-logging",
            "--camera-capture",
            "--startup-status-file",
            str(tmp_path / "status.json"),
            *extra,
        ],
    )
    monkeypatch.setattr(server, "init_session_logger", lambda **_kwargs: None)
    monkeypatch.setattr(server, "init_camera_capture", lambda **kwargs: init_calls.append(kwargs))
    monkeypatch.setattr(server, "start_monitor", lambda **_kwargs: None)
    monkeypatch.setattr(server.socketio, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_cleanup_hardware_for_shutdown", lambda: None)
    server.main()
    return init_calls[0]


def test_cli_defaults_keep_todays_buffers_and_use_the_worker(monkeypatch, tmp_path):
    kwargs = _main_args(monkeypatch, tmp_path)

    assert kwargs["buffer_count"] == 8
    assert kwargs["cv_in_process"] is False


def test_cli_passes_buffer_count_and_in_process_rollback(monkeypatch, tmp_path):
    kwargs = _main_args(
        monkeypatch, tmp_path, "--camera-buffer-count", "24", "--camera-cv-in-process"
    )

    assert kwargs["buffer_count"] == 24
    assert kwargs["cv_in_process"] is True


@pytest.mark.parametrize("count", ["3", "33"])
def test_cli_rejects_buffer_counts_outside_4_to_32(monkeypatch, tmp_path, capsys, count):
    with pytest.raises(SystemExit):
        _main_args(monkeypatch, tmp_path, "--camera-buffer-count", count)

    assert "--camera-buffer-count must be 4-32" in capsys.readouterr().err


REAL_CLIPS = os.environ.get("OPENFLIGHT_CAMERA_CLIP_DIR")


@pytest.mark.skipif(
    not REAL_CLIPS, reason="set OPENFLIGHT_CAMERA_CLIP_DIR to a folder of saved camera clips"
)
def test_real_clips_give_identical_results_through_the_worker(worker):
    clips = sorted(Path(REAL_CLIPS).glob("**/frames.npz"))[:8]
    assert clips
    geometry = CameraBallGeometry(
        camera_height_m=0.182,
        radar_height_m=0.132,
        tee_range_m=1.5,
        ball_height_m=0.02135,
        image_width_px=640,
        image_height_px=400,
    )
    trackers = {path: ReferenceBallTracker() for path in ("local", "remote")}
    for clip in clips:
        with np.load(clip) as archive:
            frames = archive["frames"]
            timestamps = archive["sensor_timestamp_ns"]
            trigger_ns = int(archive["trigger_host_timestamp_ns"])
            trigger_index = int(archive["pre_trigger_count"]) - 1
        results = {}
        for path, use in (("local", None), ("remote", worker)):
            kwargs = _ball_kwargs(frames, timestamps, geometry, None, 30.0, trackers[path])
            kwargs["trigger_ns"] = trigger_ns
            results[path] = (
                run_camera_cv("ball_flight", kwargs, use),
                run_camera_cv(
                    "club_delivery",
                    _club_kwargs(frames, timestamps, geometry, trackers[path], trigger_index),
                    use,
                ),
            )
        assert results["remote"] == results["local"], clip
        assert _tracker_state(trackers["remote"]) == _tracker_state(trackers["local"])
