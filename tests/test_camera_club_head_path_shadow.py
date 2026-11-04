"""The camera club-head path in shadow mode: worker job, budget, session log, nothing else."""

import json
import logging
import math
import os
import queue
import signal
import threading
import time
from dataclasses import fields as dataclass_fields, replace
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from test_camera_cv_worker import (
    _main_args,
    _RecordingWorker,
    _tiny_capture,
    _tracker_state,
    prepare_fusion,
)
from test_club_head_path import (
    CLUB_SPEED_MPH,
    GEOMETRY,
    Scenario,
    _departure_trigger_ns,
    _live_departure_time_zero,
    render_clip,
)

from openflight import server
from openflight.angle_reference import BallMark
from openflight.camera import club_head_path as club_head_path_module, cv_worker
from openflight.camera.ball_flight import (
    SPEED_OF_SOUND_M_S,
    BallLocatorDiagnostics,
    CameraBallEstimate,
    CameraTimeZero,
    _camera_model,
)
from openflight.camera.club_attack import AttackAngleResult
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.club_head_path import (
    ACOUSTIC_STAMP_DELAY_MS,
    CONTACT_SIGMA_S,
    ClubBall,
    ClubDataResult,
    ClubHeadPathResult,
    ContactShadow,
    HeadObservation,
    HeadQuality,
    ImpactLocation,
    estimate_club_data,
    estimate_club_head_path,
    mark_reference_ball,
    select_contact_time,
)
from openflight.camera.club_motion import ReferenceBall
from openflight.camera.cv_worker import (
    CameraCvDeadlinePassed,
    CameraCvJobError,
    CameraCvWorker,
    CameraCvWorkerError,
    CameraCvWorkerTimeout,
)
from openflight.camera.tee_zone import TeeZone
from openflight.clubs import ClubType
from openflight.dplane import curve_class
from openflight.gspro.codec import GSProCodec
from openflight.launch_monitor import CLUB_DATA_SHOT_FIELDS, Shot
from openflight.sim.types import PlayerUpdate

LOG_KEYS = {
    "accepted",
    "reason",
    "path_deg",
    "sigma_deg",
    "feature",
    "polarity",
    "n_frames",
    "fit_sigma_deg",
    "jackknife_sigma_deg",
    "feature_sigma_deg",
    "contact_sigma_deg",
    "feature_paths_deg",
    "frames",
    "t0_source",
    "t0_ms",
    "t0_sigma_ms",
    "t0_reason",
    "departure_fit_ms",
    "sound_bound_ms",
    "trigger_stamp_lag_ms",
    "edge_sound_bound_ms",
    "edge_bound_verdict",
    "edge_reference",
    "camera_departure_ms",
    "camera_departure_points",
    "camera_departure_rms_px",
    "camera_departure_span_frames",
    "departure_agreement_ms",
    "acoustic_ms",
    "acoustic_reference",
    "acoustic_delay_ms",
    "acoustic_delay_source",
    "acoustic_delay_n",
    "acoustic_kernel_ms",
    "t0_shadow_ms",
    "t0_shadow_source",
    "t0_shadow_reason",
    "t0_shadow_minus_t0_ms",
    "tee_control_n_teed",
    "tee_control_n_late",
    "tee_control_passes",
    "path_last_frame_gap_ms",
    "path_arc_status",
    "path_arc_deg",
    "path_arc_sigma_deg",
    "path_arc_radii_deg",
    "path_arc_window_deg",
    "path_noise_sigma",
    "segmentation",
    "segmentation_sigma_deg",
    "segmentation_paths_deg",
    "ball_source",
    "ball_x_px",
    "ball_y_px",
    "ball_d_px",
    "ball_anchor",
    "ball_refined",
    "ball_rejected",
    "elapsed_s",
    "stage_elapsed_s",
    "budget_s",
    "club_data_mode",
    "worker_restart_started",
    "attack",
    "impact",
    "impact_point",
    "ball_teed",
    "dplane",
    "dplane_reason",
}
ATTACK_LOG_KEYS = {
    "status",
    "attack_deg",
    "sigma_deg",
    "method",
    "n_frames",
    "feature",
    "radius_m",
    "plane_deg",
    "contact_height_mm",
    "rms_px",
    "arc_path_deg",
    "arc_path_sigma_deg",
    "arc_path_status",
    "arc_path_total_sigma_deg",
    "arc_path_stat_sigma_deg",
    "arc_path_jackknife_sigma_deg",
    "arc_path_feature_sigma_deg",
    "arc_path_contact_gain_deg_per_ms",
    "arc_path_speed_gain_deg_per_mph",
    "stat_sigma_deg",
    "jackknife_sigma_deg",
    "feature_sigma_deg",
    "contact_sigma_deg",
    "speed_sigma_deg",
    "contact_gain_deg_per_ms",
    "low_point_cm",
    "low_point_sigma_cm",
    "quadratic_attack_deg",
    "window_delta_deg",
    "feature_attacks_deg",
    "launch_vertical_deg",
    "launch_source",
    "face_weight",
    "dynamic_loft_deg",
    "dynamic_loft_sigma_deg",
    "spin_loft_deg",
    "spin_loft_sigma_deg",
}
TEE_RANGE_M = 1.524  # _tiny_capture's calibration
CLIP_TEE_RANGE_M = 1.5  # render_clip's contact times (test_club_head_path)
SOUND_BOUND_MS = -1e3 * TEE_RANGE_M / SPEED_OF_SOUND_M_S


@pytest.fixture(name="worker", scope="module")
def fixture_worker():
    worker = CameraCvWorker(timeout_s=60.0)
    yield worker
    worker.close()


def _start(worker: CameraCvWorker) -> None:
    """Run a trivial job so the worker process is up, as after a shot's ball flight."""
    worker.run(
        "ball_flight",
        {
            "frames": np.zeros((3, 4, 4), np.uint8),
            "timestamps_ns": np.arange(2),
            "trigger_ns": 0,
            "range_evidence": None,
            "geometry": None,
            "ops_ball_speed_mph": 1.0,
        },
    )


def _club_clip_job(path_deg: float = 6.0, shot_number: int = 12, scenario=None, **fields):
    """A synthetic club-head clip whose live time zero is its true contact, as queued.

    ``fields`` override the job's (club, start direction, ...); an 8-iron by default.
    ``departed_ball=True`` hands the job the clip's true ball as the ball flight's
    disappeared-ball locator would.
    """
    frames, timestamps, contact_s, ball = render_clip(scenario or Scenario(path_deg=path_deg))
    if fields.get("departed_ball") is True:
        fields["departed_ball"] = ball
    trigger_ns = _departure_trigger_ns(contact_s)
    time_zero = _live_departure_time_zero(trigger_ns, contact_s)
    job = server._ClubHeadPathShadowJob(
        shot_number=shot_number,
        shot_timestamp=100.0,
        contact=select_contact_time(time_zero, trigger_ns=trigger_ns, tee_range_m=CLIP_TEE_RANGE_M),
        kwargs={
            "frames": frames,
            "timestamps_ns": timestamps,
            "geometry": GEOMETRY,
            "club_speed_mph": CLUB_SPEED_MPH,
            "ball_tracker": ReferenceBallTracker(),
        },
        **{
            "club": ClubType.IRON_8,
            "club_key": "8i",
            "profile_id": "p1",
            "time_zero": time_zero,
            "trigger_ns": trigger_ns,
            "tee_range_m": CLIP_TEE_RANGE_M,
            **fields,
        },
    )
    return job, frames, timestamps


class _Entries(list):
    """The ``camera_club_head_path`` entries; ``club_data`` holds the ``club_data`` ones.

    ``updates`` holds the ``shot_updated`` entries (the shot as it was logged).
    """

    def __init__(self):
        super().__init__()
        self.club_data: list[dict] = []
        self.updates: list[dict] = []


def _record_entries(monkeypatch) -> _Entries:
    entries = _Entries()
    session = SimpleNamespace(
        log_camera_club_head_path=lambda **kwargs: entries.append(kwargs),
        log_club_data=lambda **kwargs: entries.club_data.append(kwargs),
        log_shot_update=lambda shot: entries.updates.append(shot.to_dict()),
    )
    monkeypatch.setattr(server, "get_session_logger", lambda: session)
    return entries


@pytest.fixture(name="club_data_state", autouse=True)
def fixture_club_data_state(monkeypatch):
    """Show mode, a right-handed golfer and a fresh session calibrator for every test."""
    monkeypatch.setattr(server, "club_data_mode", "show")
    monkeypatch.setattr(server, "golfer_handedness", "right")
    monkeypatch.setattr(server, "_sim_reported_handed", None)
    server._reset_club_data_state("test")
    yield
    server._reset_club_data_state("test")


def _club_data_result(path: ClubHeadPathResult, job=None) -> ClubDataResult:
    """A worker result with ``path`` and the job's preliminary contact."""
    job = job or _club_clip_job()[0]
    return ClubDataResult(job.contact, None, path, dict.fromkeys(server._CLUB_DATA_STAGES, 0.0))


@pytest.fixture(name="enrichment_queue", autouse=True)
def fixture_enrichment_queue(monkeypatch):
    """A private, empty shot-enrichment queue: the shadow checks it for waiting shots."""
    fresh = queue.Queue(maxsize=server._SHOT_ENRICHMENT_QUEUE_CAPACITY)
    monkeypatch.setattr(server, "shot_enrichment_queue", fresh)
    return fresh


def _waiting_shot():
    return (Shot(ball_speed_mph=101.0, timestamp=datetime.now()), "shot", None)


class _FakeWorker:
    """Stands in for a running worker; ``outcome`` is the result or the error to raise.

    ``on_run`` runs inside the job (e.g. a shot arriving meanwhile), ``on_start``
    inside ``start`` (e.g. a slow start); ``start_error`` makes ``start`` fail.
    """

    pid = 4242

    def __init__(self, outcome, *, on_run=None, on_start=None, start_error=None):
        self.outcome = outcome
        self.on_run = on_run
        self.on_start = on_start
        self.start_error = start_error
        self.calls = []
        self.starts = 0

    def run(self, job, kwargs, *, timeout_s=None):
        self.calls.append((job, kwargs, timeout_s))
        if self.on_run is not None:
            self.on_run()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome, {}

    def start(self, *, timeout_s=None):
        self.starts += 1
        if self.on_start is not None:
            self.on_start()
        if self.start_error is not None:
            raise self.start_error
        return 4343


def _join_restarts() -> None:
    """Wait for the shadow's background worker restarts to finish."""
    for thread in threading.enumerate():
        if thread.name == server._CAMERA_CV_RESTART_THREAD_NAME:
            thread.join(30.0)
            assert not thread.is_alive()


# --- The worker job --------------------------------------------------------------


def test_the_club_head_path_job_in_the_worker_equals_the_in_process_estimate(worker):
    job, frames, timestamps = _club_clip_job()
    kwargs = {
        **job.kwargs,
        "contact_time_s": job.contact.time_s,
        "contact_sigma_s": job.contact.sigma_s,
    }

    remote, state = worker.run("club_head_path", kwargs)
    local = estimate_club_head_path(
        frames,
        timestamps,
        contact_time_s=job.contact.time_s,
        geometry=GEOMETRY,
        club_speed_mph=CLUB_SPEED_MPH,
        ball_tracker=ReferenceBallTracker(),
        contact_sigma_s=CONTACT_SIGMA_S["departure_fit"],
    )

    assert remote == local
    assert remote.status == "accepted"
    assert remote.path_deg == pytest.approx(6.0, abs=0.8)
    # The worker moved its own copy of the tracker, not the caller's.
    assert len(state["ball_tracker"]._samples) == 1
    assert _tracker_state(job.kwargs["ball_tracker"]) == ([], [])


def test_the_log_names_a_dropped_tracker_ball_beside_the_marks_ball():
    """2026-11-02 S1 irons: the tracker's tree-line blob gave way to the tapped mark."""
    job, _frames, _timestamps = _club_clip_job(
        scenario=Scenario(attack_deg=-4.0, path_deg=2.0, bright_head=True), departed_ball=True
    )
    result = estimate_club_data(
        **{**server._club_data_worker_kwargs(job, None), "ball_tracker": ReferenceBallTracker()}
    )
    ball = ClubBall(
        result.ball.ball,
        "refined",
        "mark",
        refined=True,
        rejected=("tracker_anchor_far_from_mark",),
    )

    payload = server._club_head_path_log_payload(job, replace(result, ball=ball), "accepted", 0.1)

    assert (payload["ball_anchor"], payload["ball_refined"], payload["ball_rejected"]) == (
        "mark",
        True,
        ["tracker_anchor_far_from_mark"],
    )
    assert json.loads(json.dumps(payload["ball_rejected"])) == ["tracker_anchor_far_from_mark"]


def test_a_job_budget_cuts_off_the_worker_and_the_next_job_restarts_it():
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        _start(worker)
        hung_pid = worker.pid
        os.kill(hung_pid, signal.SIGSTOP)

        with pytest.raises(CameraCvWorkerTimeout, match=r"no answer within 0\.5s"):
            worker.run("club_head_path", {}, timeout_s=0.5)

        assert worker.pid is None
        with pytest.raises(ProcessLookupError):
            os.kill(hung_pid, 0)
        _start(worker)
        assert worker.pid not in (None, hung_pid)
    finally:
        worker.close()


# --- The server's shadow runner ----------------------------------------------------


def test_the_shadow_logs_the_worker_result_with_its_contact_time(monkeypatch, worker):
    _start(worker)
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    entries = _record_entries(monkeypatch)
    # A bright head and ball on a dark mat: the dark-on-bright scene's sky edge, 1.5
    # ball diameters above the ball, out-contrasts the ball in refine_teed_ball.
    job, _frames, _timestamps = _club_clip_job(
        scenario=Scenario(attack_deg=-4.0, path_deg=2.0, bright_head=True), departed_ball=True
    )
    expected = estimate_club_data(
        **{**server._club_data_worker_kwargs(job, None), "ball_tracker": ReferenceBallTracker()}
    )

    server._run_camera_club_head_path_shadow(job)

    ((entry),) = entries
    assert (entry["shot_number"], entry["shot_timestamp"]) == (12, 100.0)
    result = entry["result"]
    assert set(result) == LOG_KEYS
    assert set(result["attack"]) == ATTACK_LOG_KEYS
    assert result["accepted"] is True
    assert result["reason"] == "accepted"
    path = expected.path
    assert result["path_deg"] == path.path_deg
    assert result["sigma_deg"] == path.sigma_deg
    assert result["contact_sigma_deg"] == path.contact_sigma_deg
    assert result["feature"] == "face_centre"
    assert result["n_frames"] == path.n_frames
    assert result["feature_paths_deg"] == path.feature_paths_deg
    assert [item["frame_index"] for item in result["frames"]] == [
        item.frame_index for item in path.observations
    ]
    assert [(item["contrast"], item["edge_width_px"]) for item in result["frames"]] == [
        (round(item.quality.contrast, 1), round(item.quality.edge_width_px, 1))
        for item in path.observations
    ]
    assert (result["segmentation"], result["ball_source"]) == ("polarity", "refined")
    assert (result["ball_x_px"], result["ball_y_px"], result["ball_d_px"]) == path.ball_px
    assert (result["ball_anchor"], result["ball_refined"], result["ball_rejected"]) == (
        "departed",
        True,
        None,
    )
    # The live departure fit sets the contact; no gate-free departure ran.
    assert result["t0_source"] == "departure_fit"
    assert result["t0_ms"] == pytest.approx(job.contact.t0_ms)
    assert result["t0_sigma_ms"] == pytest.approx(0.5)
    assert result["t0_reason"] == "departure_fit"
    assert result["camera_departure_ms"] is None and result["camera_departure_points"] is None
    assert result["acoustic_ms"] == pytest.approx(
        result["sound_bound_ms"] - ACOUSTIC_STAMP_DELAY_MS
    )
    assert (result["acoustic_reference"], result["acoustic_delay_source"]) == (
        "stamp",
        "default",
    )
    assert (result["acoustic_delay_n"], result["acoustic_kernel_ms"]) == (0, None)
    # A live departure fit is no acoustic contact: no shadow; a fresh session's tee control.
    assert expected.contact_shadow.reason == "not_acoustic"
    assert {key: result[key] for key in SHADOW_LOG_KEYS} == expected.contact_shadow.to_log()
    assert [result[key] for key in TEE_CONTROL_LOG_KEYS] == [0, 0, False]
    assert result["path_last_frame_gap_ms"] == path.last_frame_gap_ms is not None
    assert set(result["stage_elapsed_s"]) == {
        "departure",
        "path",
        "attack",
        "impact",
        "impact_point",
    }
    assert result["stage_elapsed_s"]["departure"] == 0.0
    assert result["budget_s"] == server._CAMERA_CLUB_DATA_BUDGET_S == 6.0
    assert result["club_data_mode"] == "show"
    assert 0.0 <= result["elapsed_s"] < 6.0
    assert result["attack"]["status"] == path.attack.status == "accepted"
    assert result["attack"]["attack_deg"] == path.attack.attack_deg
    assert result["impact"] == path.impact.to_dict()
    assert result["impact"]["status"] == "accepted"
    assert result["impact_point"] == path.impact_point.to_dict()
    assert result["impact_point"]["status"].startswith(("accepted", "rejected_"))
    # No start direction on this job: no D-plane face.
    assert (result["dplane"], result["dplane_reason"]) == (None, "no_launch_direction")
    ((club_entry),) = entries.club_data
    assert (club_entry["status"], club_entry["reason"], club_entry["mode"]) == (
        "done",
        "accepted",
        "show",
    )
    assert club_entry["fields"]["camera_club_path_deg"] == round(path.path_deg, 1)
    # The job's tracker copy is never adopted back.
    assert _tracker_state(job.kwargs["ball_tracker"]) == ([], [])


def test_the_shadow_asks_the_worker_with_the_budget_and_no_in_process_fallback(monkeypatch):
    fake = _FakeWorker(CameraCvWorkerError("worker died\nTraceback (most recent call last):"))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setitem(cv_worker.JOBS, "club_data", lambda **_kw: pytest.fail("in-process"))
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job(club=ClubType.DRIVER, teed=True, handedness="left")

    server._run_camera_club_head_path_shadow(job)

    ((name, kwargs, timeout_s),) = fake.calls
    assert name == "club_data"
    assert timeout_s == server._CAMERA_CLUB_DATA_BUDGET_S == 6.0
    assert kwargs["frames"] is job.kwargs["frames"]
    assert kwargs["time_zero"] is job.time_zero
    assert (kwargs["trigger_ns"], kwargs["tee_range_m"]) == (job.trigger_ns, CLIP_TEE_RANGE_M)
    assert (kwargs["club"], kwargs["teed"], kwargs["handedness"]) == (
        ClubType.DRIVER,
        True,
        "left",
    )
    # No session calibration yet: the default stamp delay.
    assert kwargs["acoustic_delay_ms"] is None
    assert "contact_time_s" not in kwargs
    result = entries[0]["result"]
    assert (result["accepted"], result["reason"]) == (False, "worker_error")
    assert result["error"] == "worker died"
    # The failure discarded the worker; it is started again while no shot waits.
    _join_restarts()
    assert (fake.starts, result["worker_restart_started"]) == (1, True)


def test_an_estimator_exception_is_logged_and_leaves_the_worker_running(monkeypatch):
    fake = _FakeWorker(
        CameraCvJobError("club_head_path raised ValueError: bad clip\nTraceback (most recent")
    )
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setitem(cv_worker.JOBS, "club_data", lambda **_kw: pytest.fail("in-process"))
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    server._run_camera_club_head_path_shadow(job)

    result = entries[0]["result"]
    assert (result["accepted"], result["reason"]) == (False, "job_error")
    assert result["error"] == "club_head_path raised ValueError: bad clip"
    # The worker was not discarded, so there is nothing to restart.
    _join_restarts()
    assert (fake.starts, result["worker_restart_started"]) == (0, None)


def test_a_shadow_past_its_budget_logs_a_timeout_and_continues(monkeypatch, caplog):
    fake = _FakeWorker(CameraCvWorkerTimeout("no answer within 3s"))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    with caplog.at_level(logging.INFO):
        server._run_camera_club_head_path_shadow(job)
        _join_restarts()

    result = entries[0]["result"]
    assert (result["accepted"], result["reason"]) == (False, "timeout")
    assert result["path_deg"] is None and result["frames"] == []
    assert result["t0_source"] == "departure_fit"
    assert "club_data exceeded its 6.0s budget" in caplog.text
    assert (fake.starts, result["worker_restart_started"]) == (1, True)
    assert "Worker restarted (pid 4343)" in caplog.text


def test_a_budget_spent_waiting_for_the_worker_restarts_nothing(monkeypatch, caplog):
    """The job never reached the worker (a start held it), so the worker was not cut off."""
    fake = _FakeWorker(
        CameraCvDeadlinePassed(
            "club_head_path: the deadline passed while waiting 3s for the worker"
        )
    )
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    with caplog.at_level(logging.INFO):
        server._run_camera_club_head_path_shadow(job)
        _join_restarts()

    result = entries[0]["result"]
    assert (result["accepted"], result["reason"]) == (False, "timeout")
    assert "budget ran out waiting for the worker" in caplog.text
    assert (fake.starts, result["worker_restart_started"]) == (0, None)


def test_the_restart_never_holds_up_the_enrichment_thread(monkeypatch, caplog):
    """The start (a fresh interpreter, seconds on a Pi) runs beside the drain loop."""
    release = threading.Event()
    fake = _FakeWorker(CameraCvWorkerTimeout("no answer within 3s"), on_start=release.wait)
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    with caplog.at_level(logging.INFO):
        started = time.monotonic()
        try:
            server._run_camera_club_head_path_shadow(job)
            returned_s = time.monotonic() - started
            logged_before_the_start_ended = list(entries)
        finally:
            release.set()
            _join_restarts()

    assert returned_s < 1.0
    ((entry),) = logged_before_the_start_ended
    assert entry["result"]["worker_restart_started"] is True
    assert fake.starts == 1
    assert "Worker restarted (pid 4343)" in caplog.text


def test_a_timeout_leaves_the_restart_to_a_shot_that_arrived_meanwhile(
    monkeypatch, enrichment_queue
):
    """A waiting shot's ball flight starts the worker anyway; starting it here delays that shot."""
    fake = _FakeWorker(
        CameraCvWorkerTimeout("no answer within 3s"),
        on_run=lambda: enrichment_queue.put_nowait(_waiting_shot()),
    )
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    server._run_camera_club_head_path_shadow(job)

    result = entries[0]["result"]
    assert result["reason"] == "timeout"
    _join_restarts()
    assert (fake.starts, result["worker_restart_started"]) == (0, False)


def test_a_failed_restart_is_logged_and_never_escapes(monkeypatch, caplog):
    fake = _FakeWorker(
        CameraCvWorkerTimeout("no answer within 3s"),
        start_error=CameraCvWorkerError("no answer within 20s"),
    )
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    with caplog.at_level(logging.WARNING):
        server._run_camera_club_head_path_shadow(job)
        _join_restarts()

    result = entries[0]["result"]
    # The start began and failed in the background; the server log says so.
    assert (result["reason"], result["worker_restart_started"]) == ("timeout", True)
    assert fake.starts == 1
    assert "Worker restart after the shadow club-head path failed" in caplog.text


def test_a_real_worker_past_the_budget_is_replaced_and_the_timeout_logged(monkeypatch):
    worker = CameraCvWorker(timeout_s=60.0)
    try:
        _start(worker)
        hung_pid = worker.pid
        os.kill(hung_pid, signal.SIGSTOP)
        monkeypatch.setattr(server, "camera_cv_worker", worker)
        monkeypatch.setattr(server, "_CAMERA_CLUB_DATA_BUDGET_S", 0.5)
        entries = _record_entries(monkeypatch)
        job, _frames, _timestamps = _club_clip_job()

        server._run_camera_club_head_path_shadow(job)

        result = entries[0]["result"]
        assert result["reason"] == "timeout"
        assert result["budget_s"] == 0.5
        assert result["elapsed_s"] < 5.0  # the job and its cut-off, not the restart
        with pytest.raises(ProcessLookupError):
            os.kill(hung_pid, 0)
        # Replaced right away, while idle: the next ball flight does not pay the start.
        assert result["worker_restart_started"] is True
        _join_restarts()
        assert worker.pid not in (None, hung_pid)
        os.kill(worker.pid, 0)
    finally:
        worker.close()


@pytest.mark.parametrize(
    "worker,reason",
    [
        (None, "skipped_cv_in_process"),
        (SimpleNamespace(pid=None, run=lambda *_a, **_k: pytest.fail("started")), None),
    ],
    ids=["in_process", "not_running"],
)
def test_the_shadow_never_runs_in_the_server_or_starts_a_worker(monkeypatch, worker, reason):
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    monkeypatch.setitem(cv_worker.JOBS, "club_data", lambda **_kw: pytest.fail("in-process"))
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job()

    server._run_camera_club_head_path_shadow(job)

    result = entries[0]["result"]
    assert result["reason"] == (reason or "skipped_worker_not_running")
    assert result["accepted"] is False
    assert result["path_deg"] is None
    assert result["worker_restart_started"] is None


def test_log_mode_needs_a_session_log(monkeypatch):
    fake = _FakeWorker(None)
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setattr(server, "club_data_mode", "log")
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    job, _frames, _timestamps = _club_clip_job()

    server._run_camera_club_head_path_shadow(job)

    assert fake.calls == []


def test_the_logged_frames_carry_each_rated_head_contrast_and_edge_width(monkeypatch):
    """1 decimal, so a live log shows the margin to the contrast limit; null where unrated."""
    qualities = (
        HeadQuality("low_contrast", 11.96, 300, 30.0, 2.349, 0.0),
        HeadQuality("blurred", 21.04, 300, 30.0, math.inf, 0.0),
        HeadQuality("not_found", 0.0, 0, 0.0, math.inf, 0.0),
    )
    path = ClubHeadPathResult(
        status="rejected_head_quality",
        observations=tuple(
            HeadObservation(index, -1.0 - index, {}, quality, width_m=0.08)
            for index, quality in enumerate(qualities)
        ),
    )
    job, _frames, _timestamps = _club_clip_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_club_data_result(path, job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job)

    frames = entries[0]["result"]["frames"]
    assert [(item["quality"], item["contrast"], item["edge_width_px"]) for item in frames] == [
        ("low_contrast", 12.0, 2.3),
        ("blurred", 21.0, None),
        ("not_found", None, None),
    ]
    json.dumps(frames, allow_nan=False)


def test_show_mode_runs_without_a_session_log(monkeypatch):
    job, _frames, _timestamps = _club_clip_job()
    fake = _FakeWorker(_club_data_result(ClubHeadPathResult(status="rejected_test"), job))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert len(fake.calls) == 1
    assert (shot.club_data_status, shot.club_data_reason) == ("done", "rejected_test")


def test_a_failing_session_log_never_escapes_the_shadow(monkeypatch):
    def fail(**_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(server, "camera_cv_worker", None)
    monkeypatch.setattr(
        server, "get_session_logger", lambda: SimpleNamespace(log_camera_club_head_path=fail)
    )
    errors = []
    monkeypatch.setattr(server, "log_session_error", lambda *args, **kwargs: errors.append(kwargs))
    job, _frames, _timestamps = _club_clip_job()

    server._run_camera_club_head_path_shadow(job)

    assert errors[0]["context"] == {"stage": "club_head_path_shadow", "shot_number": 12}


# --- Wiring: inputs, timing, and nothing on the shot -------------------------------


def test_ball_flight_hands_the_shadow_its_clip_geometry_and_pre_shot_tracker(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    fake = _RecordingWorker(CameraBallEstimate(status="rejected_test"))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)
    shot.shot_number = 9
    shot.impact_timestamp = 123.4

    job = server._fuse_camera_ball_flight(shot, capture)

    ((_name, ball_kwargs),) = fake.calls
    assert job.skip_reason is None
    assert job.kwargs["geometry"] is ball_kwargs["geometry"]
    assert job.kwargs["frames"].shape == (8, 4, 4)
    np.testing.assert_array_equal(job.kwargs["timestamps_ns"], ball_kwargs["timestamps_ns"])
    assert job.kwargs["club_speed_mph"] == 85.0
    assert (job.shot_number, job.shot_timestamp) == (9, 123.4)
    # The live tracker took this shot's ball; the shadow resolves it from the state
    # the ball flight started from, so both read one pose.
    assert len(server.camera_ball_flight_reference_tracker._samples) == 1
    assert job.kwargs["ball_tracker"] is not server.camera_ball_flight_reference_tracker
    assert _tracker_state(job.kwargs["ball_tracker"]) == ([], [])
    # No time zero from a rejected estimate: the calibrated acoustic estimate, trigger
    # minus sound travel (tee 1.524 m) minus the default stamp delay.
    assert job.contact.reason == "no_camera_time_zero"
    assert job.contact.t0_ms == pytest.approx(SOUND_BOUND_MS - ACOUSTIC_STAMP_DELAY_MS)
    assert job.contact.time_s == pytest.approx(3e-9 + job.contact.t0_ms / 1e3)
    # This clip has no edge stamp: the bound stays at the trigger stamp.
    assert job.edge_reference is None
    assert job.contact.trigger_lag_ms is None


# The trigger stamp lags the sound edge; the edge log knows the edge's own stamps.


@pytest.mark.parametrize(
    "fields,expected",
    [
        (
            {"edge_callback_monotonic_ns": 1_000_000_000, "edge_callback_latency_ms": 0.4},
            (2.4, "kernel"),
        ),
        (
            {"edge_callback_monotonic_ns": 1_000_000_000, "edge_callback_latency_ms": None},
            (2.0, "callback"),
        ),
        ({"edge_callback_monotonic_ns": None, "edge_callback_latency_ms": None}, (None, None)),
        # An edge stamped after the trigger stamp is not this clip's edge.
        (
            {"edge_callback_monotonic_ns": 1_002_500_000, "edge_callback_latency_ms": None},
            (None, None),
        ),
    ],
    ids=["kernel", "callback_only", "unknown", "after_the_trigger"],
)
def test_the_trigger_stamp_lag_comes_from_the_edges_own_stamps(fields, expected):
    lag_ms, reference = server._trigger_stamp_lag(1_002_000_000, fields)

    assert reference == expected[1]
    assert lag_ms == (None if expected[0] is None else pytest.approx(expected[0]))


EDGE_TRIGGER_NS = 5_000_000_000


def _edge_capture(monkeypatch, tmp_path, *, callback_lag_ms, kernel_latency_ms):
    """A tiny clip triggered at EDGE_TRIGGER_NS and its edge in a real edge log.

    The edge callback ran ``callback_lag_ms`` before the trigger stamp, and the
    kernel stamped the line event ``kernel_latency_ms`` before the callback.
    """
    capture = _tiny_capture(monkeypatch, tmp_path)
    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=EDGE_TRIGGER_NS + np.arange(-4, 4, dtype=np.int64) * 3_470_000,
        trigger_host_timestamp_ns=np.int64(EDGE_TRIGGER_NS),
        pre_trigger_count=np.int32(4),
    )
    edge_wall = time.time()
    callback_ns = EDGE_TRIGGER_NS - round(callback_lag_ms * 1e6)
    edge_log = server.EdgeTimingLog()
    timing = edge_log.record(
        edge_wall,
        callback_timestamp=edge_wall,
        callback_monotonic_ns=callback_ns,
        ticks=callback_ns - round(kernel_latency_ms * 1e6),
    )
    assert timing.latency_ms == pytest.approx(kernel_latency_ms)  # ns ticks on CLOCK_MONOTONIC
    monkeypatch.setattr(server, "sound_edge_timing", edge_log)
    capture.trigger_timestamp = edge_wall
    return capture


def _departure_fit_estimate(departure_ms: float) -> CameraBallEstimate:
    """A ball flight whose live time zero is a departure fit ``departure_ms`` from the trigger."""
    return CameraBallEstimate(
        status="rejected_test",
        time_zero=CameraTimeZero(
            source="departure_fit",
            time_zero_ms=departure_ms,
            departure_fit_ms=departure_ms,
            acoustic_ms=SOUND_BOUND_MS,
            tee_patch_ms=-3.0,
            iwr_impact_t_s=None,
        ),
    )


def test_the_shadow_logs_a_departure_fit_after_the_bound_at_the_kernel_edge(monkeypatch, tmp_path):
    """Trigger stamp 3 ms behind the kernel edge: a fit 1 ms before the stamp's bound is
    2 ms after the edge bound. Logged as such; the stamp bound decides, so it is kept."""
    capture = _edge_capture(monkeypatch, tmp_path, callback_lag_ms=2.0, kernel_latency_ms=1.0)
    departure_ms = SOUND_BOUND_MS - 1.0
    monkeypatch.setattr(
        server, "camera_cv_worker", _RecordingWorker(_departure_fit_estimate(departure_ms))
    )
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)

    job = server._fuse_camera_ball_flight(shot, capture)

    assert shot.experimental_camera_time_zero["source"] == "departure_fit"
    assert job.edge_reference == "kernel"
    contact = job.contact
    assert (contact.source, contact.reason) == ("departure_fit", "departure_fit")
    assert contact.edge_bound_verdict == "departure_fit_after_edge_sound_bound"
    assert contact.trigger_lag_ms == pytest.approx(3.0)
    assert contact.edge_sound_bound_ms == pytest.approx(SOUND_BOUND_MS - 3.0)
    assert contact.t0_ms == pytest.approx(departure_ms)
    assert contact.time_s == pytest.approx(EDGE_TRIGGER_NS / 1e9 + departure_ms / 1e3)

    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(None))
    entries = _record_entries(monkeypatch)
    server._run_camera_club_head_path_shadow(replace(job, skip_reason="skipped_test"))
    result = entries[0]["result"]
    assert result["trigger_stamp_lag_ms"] == pytest.approx(3.0)
    assert result["edge_sound_bound_ms"] == pytest.approx(SOUND_BOUND_MS - 3.0)
    assert result["edge_reference"] == "kernel"
    assert result["edge_bound_verdict"] == "departure_fit_after_edge_sound_bound"
    assert (result["t0_source"], result["t0_reason"]) == ("departure_fit", "departure_fit")


def test_the_shadow_keeps_a_departure_fit_before_the_bound_at_the_edge(monkeypatch, tmp_path):
    capture = _edge_capture(monkeypatch, tmp_path, callback_lag_ms=2.0, kernel_latency_ms=1.0)
    departure_ms = SOUND_BOUND_MS - 3.4
    monkeypatch.setattr(
        server, "camera_cv_worker", _RecordingWorker(_departure_fit_estimate(departure_ms))
    )
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)

    contact = server._fuse_camera_ball_flight(shot, capture).contact

    assert (contact.source, contact.reason) == ("departure_fit", "departure_fit")
    assert contact.edge_bound_verdict == "departure_fit_before_edge_sound_bound"
    assert contact.t0_ms == pytest.approx(departure_ms)
    assert contact.sigma_s == CONTACT_SIGMA_S["departure_fit"]


def test_the_shadow_refuses_a_departure_fit_after_the_trigger_stamp_bound(monkeypatch, tmp_path):
    """The live window takes this fit (0.3 ms after the bound); the club path does not."""
    capture = _tiny_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server,
        "camera_cv_worker",
        _RecordingWorker(_departure_fit_estimate(SOUND_BOUND_MS + 0.3)),
    )
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)

    contact = server._fuse_camera_ball_flight(shot, capture).contact

    assert (contact.source, contact.reason) == (
        "acoustic_calibrated",
        "departure_fit_after_sound_bound",
    )
    assert contact.t0_ms == pytest.approx(SOUND_BOUND_MS - ACOUSTIC_STAMP_DELAY_MS)
    assert contact.departure_fit_ms == pytest.approx(SOUND_BOUND_MS + 0.3)


# Every ball-analysis-eligible clip gets an entry, also when the path cannot run.


def _time_out(_job, _kwargs, **_kw):
    raise CameraCvWorkerTimeout("no answer within 20s")


def _raise_in_ball_flight(**_kwargs):
    raise RuntimeError("estimator bug")


@pytest.mark.parametrize(
    "case,reason",
    [
        ("no_frames_file", "skipped_missing_frames"),
        ("unloadable_frames", "skipped_missing_frames"),
        ("no_iwr_runtime", "skipped_no_iwr_runtime"),
        ("no_tee_geometry", "skipped_no_tee_geometry"),
        ("ball_flight_timeout", "skipped_ball_flight_timeout"),
        ("ball_flight_error", "skipped_ball_flight_error"),
        ("shot_past_deadline", "skipped_shot_past_deadline"),
    ],
)
def test_an_eligible_clip_the_path_cannot_run_on_still_gets_an_entry(
    monkeypatch, tmp_path, case, reason
):
    capture = _tiny_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server, "camera_cv_worker", _RecordingWorker(CameraBallEstimate(status="rejected_test"))
    )
    if case == "no_frames_file":
        (tmp_path / "frames.npz").unlink()
    elif case == "unloadable_frames":
        monkeypatch.setattr(server, "_load_camera_capture_archive", lambda _capture: None)
    elif case == "no_iwr_runtime":
        monkeypatch.setattr(server, "iwr6843_runtime", None)
    elif case == "no_tee_geometry":
        server.iwr6843_runtime.calibration.tee_range_m = None
    elif case == "ball_flight_timeout":
        monkeypatch.setattr(server, "camera_cv_worker", SimpleNamespace(pid=None, run=_time_out))
    elif case == "ball_flight_error":
        monkeypatch.setattr(server, "camera_cv_worker", None)
        monkeypatch.setitem(cv_worker.JOBS, "ball_flight", _raise_in_ball_flight)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club_speed_mph=85.0)
    shot.shot_number = 4
    shot.impact_timestamp = 55.5
    deadline = time.monotonic() - 1.0 if case == "shot_past_deadline" else None

    job = server._fuse_camera_ball_flight(shot, capture, deadline_monotonic=deadline)

    assert (job.skip_reason, job.contact, job.kwargs) == (reason, None, {})
    assert (job.shot_number, job.shot_timestamp) == (4, 55.5)
    if case == "shot_past_deadline":
        assert server.camera_cv_worker.calls == []  # never asked for a shot past its deadline
        assert shot.experimental_camera_horizontal_status != "error"
    fake = _FakeWorker(None)
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job)

    assert fake.calls == []
    ((entry),) = entries
    assert (entry["shot_number"], entry["shot_timestamp"]) == (4, 55.5)
    result = entry["result"]
    assert set(result) == LOG_KEYS
    assert (result["accepted"], result["reason"]) == (False, reason)
    for name in ("t0_source", "t0_ms", "t0_reason", "sound_bound_ms", "path_deg"):
        assert result[name] is None, name
    # A run that never happened: every attack key present, the impact and D-plane not run.
    assert set(result["attack"]) == ATTACK_LOG_KEYS
    assert (result["attack"]["status"], result["attack"]["attack_deg"]) == ("not_run", None)
    assert result["impact"] == {"status": "not_run"}
    assert result["impact_point"] == {"status": "not_run"}
    assert (result["dplane"], result["dplane_reason"]) == (None, "not_run")
    assert result["stage_elapsed_s"] == {}
    ((club_entry),) = entries.club_data
    assert (club_entry["status"], club_entry["reason"]) == ("skipped", reason)
    assert set(club_entry["fields"]) == set(server.CLUB_DATA_FIELDS)
    assert not any(club_entry["fields"].values())


@pytest.mark.parametrize(
    "capture",
    [None, SimpleNamespace(valid=False, path=None)],
    ids=["no_capture", "invalid_capture"],
)
def test_no_clip_gets_no_entry(monkeypatch, tmp_path, capture):
    _tiny_capture(monkeypatch, tmp_path)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now())

    assert server._fuse_camera_ball_flight(shot, capture) is None


def test_a_clip_not_eligible_for_ball_analysis_gets_no_entry(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    capture.metadata = {
        "auto_exposure": {"analysis_eligible": False, "ball_analysis_eligible": False}
    }
    monkeypatch.setattr(server, "camera_cv_worker", _RecordingWorker(None))
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now())

    assert server._fuse_camera_measurements(shot, capture) is None
    assert server.camera_cv_worker.calls == []


# The club data never feeds the D-plane spin axis, the carry or the simulators.


def _enrich_for_comparison(monkeypatch, tmp_path, worker, *, with_shadow: bool):
    """One fusion plus D-plane run of the same clip; returns the shot and its job."""
    clip_dir = tmp_path / ("with_shadow" if with_shadow else "without_shadow")
    clip_dir.mkdir()
    shot, capture = prepare_fusion(monkeypatch, clip_dir, worker)
    shot.club = ClubType.IRON_7
    shot.club_path_deg = 2.0  # a measured path, so the D-plane runs on the fused start
    # The synthetic clip's camera-assisted start carries confidence 0.45.
    monkeypatch.setattr(server, "SPIN_AXIS_MIN_CONFIDENCE", 0.4)
    if not with_shadow:
        monkeypatch.setattr(server, "_club_head_path_shadow_job", lambda *_a, **_kw: None)
    job = server._fuse_camera_measurements(shot, capture)
    server._apply_dplane_spin_axis(shot)
    return shot, job


def _without_club_data(data: dict) -> dict:
    return {key: value for key, value in data.items() if key not in CLUB_DATA_SHOT_FIELDS}


def _everything_downstream(shot: Shot):
    """Display payload and session-log shot without the club data, the D-plane, the sims."""
    return (
        _without_club_data(server.shot_to_dict(shot)),
        _without_club_data(shot.to_dict()),
        (shot.club_path_deg, shot.spin_axis_deg, shot.spin_axis_source),
        server.resolve_shot(shot, server.SimPlayerState()).as_values(),
    )


def test_the_club_data_changes_nothing_else_on_the_shot_the_dplane_or_the_sims(
    monkeypatch, tmp_path, worker
):
    _start(worker)
    shot, job = _enrich_for_comparison(monkeypatch, tmp_path, worker, with_shadow=True)
    reference, no_job = _enrich_for_comparison(monkeypatch, tmp_path, worker, with_shadow=False)
    assert no_job is None and job is not None and job.skip_reason is None
    assert shot.spin_axis_source == "calculated"  # the D-plane ran on this shot
    downstream = _everything_downstream(reference)

    # Building the job left the shot as a run without it does.
    assert _everything_downstream(shot) == downstream
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    # And running it changes nothing but the club data either.
    assert _everything_downstream(shot) == downstream
    assert shot.club_path_deg == 2.0
    assert shot.club_data_status == "done"
    ((entry),) = entries
    result = entry["result"]
    assert set(result) == LOG_KEYS
    assert result["accepted"] is False  # a ball-flight clip has no club head
    assert result["reason"].startswith("rejected_")
    assert shot.club_data_reason == result["reason"]
    assert result["sound_bound_ms"] == pytest.approx(
        -1e3 * server.iwr6843_runtime.calibration.tee_range_m / SPEED_OF_SOUND_M_S
    )
    assert result["departure_fit_ms"] == shot.experimental_camera_time_zero["departure_fit_ms"]


@pytest.mark.parametrize("use_worker", [False, True], ids=["in_process", "worker"])
def test_the_shadow_leaves_the_live_trackers_alone(monkeypatch, tmp_path, worker, use_worker):
    _start(worker)
    shot, capture = prepare_fusion(monkeypatch, tmp_path, worker if use_worker else None)
    job = server._fuse_camera_measurements(shot, capture)
    trackers = (
        _tracker_state(server.camera_ball_flight_reference_tracker),
        _tracker_state(server.camera_reference_ball_tracker),
    )
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job)

    assert len(entries) == 1
    assert (
        _tracker_state(server.camera_ball_flight_reference_tracker),
        _tracker_state(server.camera_reference_ball_tracker),
    ) == trackers


def test_enrichment_carries_the_fusions_shadow_job(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    monkeypatch.setattr(server, "_snapshot_inclinometer_for_shot", lambda _shot: None)
    monkeypatch.setattr(server, "_process_iwr6843_angle", lambda _shot: None)
    monkeypatch.setattr(server, "_attach_camera_replay", lambda *_args: None)
    monkeypatch.setattr(server, "_fuse_camera_measurements", lambda *_args, **_kw: sentinel)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())

    result = server._enrich_shot_from_optional_hardware(shot)

    assert result.camera_club_head_path_job is sentinel
    assert result == server._ShotEnrichmentResult()  # the job is not part of the comparison


# Timing: after this shot's hand-off, and never in the way of the next shot.


def test_the_shadow_runs_only_after_the_shot_is_handed_to_publication(monkeypatch):
    order = []
    job = object()
    monkeypatch.setattr(server, "_has_slow_shot_enrichment", lambda _shot: False)
    monkeypatch.setattr(
        server,
        "_enrich_shot_from_optional_hardware",
        lambda _shot: server._ShotEnrichmentResult(
            camera_capture_ms=12.0, camera_club_head_path_job=job
        ),
    )
    monkeypatch.setattr(
        server,
        "_queue_shot_finalization",
        lambda _shot, **kwargs: order.append(("published", kwargs["enrichment"])) or True,
    )
    monkeypatch.setattr(
        server,
        "_run_camera_club_head_path_shadow",
        lambda queued, **kwargs: order.append(("shadow", (queued, kwargs))),
    )

    server._finish_shot_detected(
        Shot(ball_speed_mph=120.0, timestamp=datetime.now()), emit_event="shot"
    )

    assert [step for step, _value in order] == ["published", "shadow"]
    published = order[0][1]
    assert published.camera_capture_ms == 12.0
    assert published.camera_club_head_path_job is None  # finalization never holds the clip
    queued, kwargs = order[1][1]
    assert queued is job
    assert kwargs["enrichment_on_time"] is True
    assert kwargs["source_shot"] is kwargs["final_shot"]  # no slow enrichment: one shot


def test_no_shadow_runs_without_a_job(monkeypatch):
    monkeypatch.setattr(server, "_has_slow_shot_enrichment", lambda _shot: False)
    monkeypatch.setattr(
        server, "_enrich_shot_from_optional_hardware", lambda _shot: server._ShotEnrichmentResult()
    )
    monkeypatch.setattr(server, "_queue_shot_finalization", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        server, "_run_camera_club_head_path_shadow", lambda *_a, **_kw: pytest.fail("shadow ran")
    )

    server._finish_shot_detected(
        Shot(ball_speed_mph=120.0, timestamp=datetime.now()), emit_event="shot"
    )


def _shadow_enrichment(monkeypatch, job):
    """Enrichment hands every shot a copy of ``job``; returns when each enrichment started."""
    started = []

    def enrich(shot):
        started.append((shot.shot_number, time.monotonic()))
        return server._ShotEnrichmentResult(
            camera_club_head_path_job=replace(job, shot_number=shot.shot_number)
        )

    monkeypatch.setattr(server, "_has_slow_shot_enrichment", lambda _shot: False)
    monkeypatch.setattr(server, "_enrich_shot_from_optional_hardware", enrich)
    return started


def test_the_shadow_does_not_start_while_the_next_shot_waits(monkeypatch, enrichment_queue):
    job, _frames, _timestamps = _club_clip_job(shot_number=1)
    _shadow_enrichment(monkeypatch, job)
    monkeypatch.setattr(server, "_queue_shot_finalization", lambda *_a, **_kw: True)
    fake = _FakeWorker(_club_data_result(ClubHeadPathResult(status="rejected_test"), job))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    enrichment_queue.put_nowait(_waiting_shot())
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    shot.shot_number = 1

    server._finish_shot_detected(shot, emit_event="shot")

    assert fake.calls == []
    result = entries[0]["result"]
    assert (result["accepted"], result["reason"]) == (False, "skipped_next_shot_queued")
    assert result["t0_source"] == "departure_fit"  # the entry stays complete


def test_a_waiting_shot_is_not_held_up_by_the_shadow(monkeypatch, enrichment_queue):
    """Two queued shots on the one enrichment thread: the first shot's shadow skips itself,
    so the second starts at once; the last shot's shadow runs (here 0.5 s)."""
    job, _frames, _timestamps = _club_clip_job()
    started = _shadow_enrichment(monkeypatch, job)
    monkeypatch.setattr(server, "_queue_shot_finalization", lambda *_a, **_kw: True)
    fake = _FakeWorker(
        _club_data_result(ClubHeadPathResult(status="rejected_test"), job),
        on_run=lambda: time.sleep(0.5),
    )
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setattr(server, "shot_enrichment_task", True)
    entries = _record_entries(monkeypatch)
    for number in (1, 2):
        shot = Shot(ball_speed_mph=120.0 + number, timestamp=datetime.now())
        shot.shot_number = number
        enrichment_queue.put_nowait((shot, "shot", None))

    server._drain_shot_enrichment_queue()

    ((first, first_at), (second, second_at)) = started
    assert (first, second) == (1, 2)
    assert second_at - first_at < 0.25
    assert [(entry["shot_number"], entry["result"]["reason"]) for entry in entries] == [
        (1, "skipped_next_shot_queued"),
        (2, "rejected_test"),
    ]
    assert len(fake.calls) == 1
    assert server.shot_enrichment_task is None


def test_a_shot_that_missed_its_deadline_gets_no_shadow_run(monkeypatch):
    """Its enrichment came back after the watchdog published it: a backlog, so no job."""
    job, _frames, _timestamps = _club_clip_job(shot_number=31)
    _shadow_enrichment(monkeypatch, job)
    fake = _FakeWorker(_club_data_result(ClubHeadPathResult(status="rejected_test"), job))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    entries = _record_entries(monkeypatch)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    shot.shot_number = 31  # never registered: finalized already, so the enrichment is late

    server._finish_shot_detected(shot, emit_event="shot_update")

    assert fake.calls == []
    assert entries[0]["result"]["reason"] == "skipped_shot_past_deadline"


def test_finalization_reports_whether_it_took_the_enrichment(monkeypatch):
    finalized = threading.Event()
    monkeypatch.setattr(server, "_finalize_shot_detected", lambda *_a, **_kw: finalized.set())
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    shot.shot_number = 77
    try:
        server._register_shot_for_finalization(shot)

        assert server._queue_shot_finalization(shot, emit_event="shot", initial_ui_ms=None)
        assert finalized.wait(5.0)
        # The same shot again: finalized, so a further enrichment is late.
        assert not server._queue_shot_finalization(shot, emit_event="shot", initial_ui_ms=None)
    finally:
        server._reset_shot_sequence()


# --- Club data on the shot ("berechnet", --club-data show) ------------------------


def _accepted_club_data(job) -> ClubDataResult:
    """A worker result whose path, arc attack angle and impact all passed their gates."""
    path = ClubHeadPathResult(
        status="accepted",
        path_deg=2.04,
        sigma_deg=0.93,
        n_frames=5,
        attack=AttackAngleResult(
            status="accepted",
            attack_deg=-4.26,
            sigma_deg=0.81,
            low_point_cm=11.94,
            low_point_sigma_cm=2.04,
        ),
        impact=ImpactLocation(status="accepted", toe_mm=6.04, sigma_mm=4.46, head_length_mm=74.0),
    )
    return _club_data_result(path, job)


def _face_job(shot_number: int = 7, **fields):
    """An iron job with a camera start direction and launch angle: every gate can pass."""
    return _club_clip_job(
        shot_number=shot_number,
        **{
            "launch_direction_deg": -1.5,
            "launch_direction_status": "camera_assisted_high",
            "launch_vertical_deg": 24.3,
            "camera_vertical_deg": 24.0,
            **fields,
        },
    )[0]


@pytest.fixture(name="emitted")
def fixture_emitted(monkeypatch):
    captured = []
    monkeypatch.setattr(server.socketio, "emit", lambda *args, **_kwargs: captured.append(args))
    return captured


def _shot_updates(emitted) -> list[dict]:
    return [payload for name, *rest in emitted if name == "shot_update" for payload in rest]


def _stored_shot(monkeypatch, shot_number: int = 7) -> Shot:
    """A published shot the monitor holds; finalization already ran (not registered)."""
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now(), club=ClubType.IRON_8)
    shot.shot_number = shot_number
    stored = [shot]
    monkeypatch.setattr(
        server,
        "monitor",
        SimpleNamespace(
            get_shots=lambda: list(stored), get_session_stats=lambda: {"count": len(stored)}
        ),
    )
    shot.stored = stored  # the test may delete the row
    return shot


def test_the_job_carries_the_club_teed_and_the_camera_launch_angle(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    estimate = CameraBallEstimate(
        status="accepted",
        vertical_deg=14.25,
        locator=BallLocatorDiagnostics(
            "disappeared", "ok", ball_x=310.0, ball_y=270.5, ball_diameter_px=16.0
        ),
    )
    monkeypatch.setattr(server, "camera_cv_worker", _RecordingWorker(estimate))
    shot = Shot(
        ball_speed_mph=150.0,
        timestamp=datetime.now(),
        club_speed_mph=100.0,
        club=ClubType.DRIVER,
    )
    shot.shot_number = 5
    shot.profile_id = "p2"

    job = server._fuse_camera_ball_flight(shot, capture)

    assert (job.club, job.teed, job.handedness) == (ClubType.DRIVER, True, "right")
    assert (job.profile_id, job.club_key) == ("p2", ClubType.DRIVER.value)
    assert job.camera_vertical_deg == 14.25
    assert job.departed_ball == ReferenceBall(310.0, 270.5, 16.0, 201)
    assert (job.trigger_ns, job.tee_range_m) == (3, TEE_RANGE_M)
    assert job.time_zero is None and job.head_length_median_mm is None
    assert job.confirmed_tee is None  # no camera runtime, no session tee


def test_a_rejected_ball_flight_gives_the_job_no_camera_launch_angle(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    estimate = CameraBallEstimate(status="rejected_test", vertical_deg=14.25)
    monkeypatch.setattr(server, "camera_cv_worker", _RecordingWorker(estimate))
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_8)

    job = server._fuse_camera_ball_flight(shot, capture)

    assert (job.camera_vertical_deg, job.departed_ball, job.teed) == (None, None, False)


@pytest.mark.parametrize(
    ("departed", "mark_px"),
    [(False, (360.8, 288.1)), (True, (360.8, 288.1)), (False, None)],
    ids=["mark", "departed", "no_mark"],
)
def test_the_job_carries_the_ball_mark_only_without_a_departed_ball(
    monkeypatch, tmp_path, departed, mark_px
):
    """2026-11-02 S1: irons without a departed ball anchor on the tapped ball, not the tracker."""
    capture = _tiny_capture(monkeypatch, tmp_path)
    locator = (
        BallLocatorDiagnostics(
            "disappeared", "ok", ball_x=358.9, ball_y=273.7, ball_diameter_px=16.2
        )
        if departed
        else None
    )
    worker = _RecordingWorker(
        CameraBallEstimate(status="accepted", vertical_deg=21.0, locator=locator)
    )
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)
    monkeypatch.setattr(
        server, "camera_capture_runtime", SimpleNamespace(active_tee_zone=lambda: zone)
    )
    # The lens model gives the mark its ball size.
    monkeypatch.setattr(server, "_capture_intrinsics", lambda _capture: GEOMETRY.intrinsics)
    mark = None if mark_px is None else BallMark(*mark_px, 56.4, 72.0, 1_000.0, {})
    monkeypatch.setattr(server.angle_reference, "ball_mark", mark)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_8)

    job = server._fuse_camera_ball_flight(shot, capture)

    flight = dict(worker.calls)["ball_flight"]
    assert (flight["ball_mark_px"], flight["tee_zone"]) == (mark_px, zone)
    assert job.tee_zone is zone
    if departed:
        assert job.departed_ball is not None and job.reference_ball is None
    elif mark_px is None:
        assert job.departed_ball is None and job.reference_ball is None
    else:
        expected = mark_reference_ball(mark_px, job.kwargs["geometry"])
        assert job.departed_ball is None
        assert job.reference_ball == expected
        assert (expected.x, expected.y) == mark_px and expected.diameter_px > 0.0
    kwargs = server._club_data_worker_kwargs(job, None)
    assert (kwargs["reference_ball"], kwargs["tee_zone"]) == (job.reference_ball, zone)


def test_a_ball_mark_that_cannot_be_read_costs_the_job_nothing(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server, "camera_cv_worker", _RecordingWorker(CameraBallEstimate(status="rejected_test"))
    )
    monkeypatch.setattr(
        server.angle_reference, "ball_mark", BallMark(360.8, 288.1, 56.4, 72.0, 1.0, {})
    )

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(club_head_path_module, "mark_reference_ball", boom)
    shot = Shot(ball_speed_mph=110.0, timestamp=datetime.now(), club=ClubType.IRON_8)

    job = server._fuse_camera_ball_flight(shot, capture)

    assert job is not None and job.reference_ball is None


def test_enrichment_hands_the_job_the_fused_start_direction(monkeypatch):
    job = server._ClubHeadPathShadowJob(shot_number=3, shot_timestamp=1.0, contact=None)

    def fuse(shot, *_args, **_kwargs):
        shot.launch_angle_horizontal = -1.5
        shot.experimental_camera_horizontal_status = "camera_assisted_high"
        shot.launch_angle_vertical = 17.0
        return job

    monkeypatch.setattr(server, "camera_capture_runtime", None)
    monkeypatch.setattr(server, "_snapshot_inclinometer_for_shot", lambda _shot: None)
    monkeypatch.setattr(server, "_process_iwr6843_angle", lambda _shot: None)
    monkeypatch.setattr(server, "_attach_camera_replay", lambda *_args: None)
    monkeypatch.setattr(server, "_fuse_camera_measurements", fuse)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())

    carried = server._enrich_shot_from_optional_hardware(shot).camera_club_head_path_job

    assert (
        carried.launch_direction_deg,
        carried.launch_direction_status,
        carried.launch_vertical_deg,
    ) == (-1.5, "camera_assisted_high", 17.0)


@pytest.mark.parametrize("live_fit", [True, False], ids=["live_fit", "no_live_fit"])
def test_the_worker_job_runs_the_departure_only_without_a_live_fit(monkeypatch, live_fit):
    departures = []

    def departure(*_args, **_kwargs):
        departures.append(1)

    monkeypatch.setattr(club_head_path_module, "estimate_departure_contact", departure)
    in_process = SimpleNamespace(
        pid=1,
        run=lambda name, kwargs, timeout_s=None: (cv_worker.JOBS[name](**kwargs), {}),
    )
    monkeypatch.setattr(server, "camera_cv_worker", in_process)
    entries = _record_entries(monkeypatch)
    job, _frames, _timestamps = _club_clip_job(departed_ball=True)
    if not live_fit:
        job = replace(job, time_zero=None)

    server._run_camera_club_head_path_shadow(job)

    result = entries[0]["result"]
    assert departures == ([] if live_fit else [1])
    assert result["t0_source"] == ("departure_fit" if live_fit else "acoustic_calibrated")
    if live_fit:
        assert result["stage_elapsed_s"]["departure"] == 0.0
    else:
        assert result["t0_reason"] == "no_camera_departure"


def test_a_tapped_target_leaves_face_to_path_and_the_axis_class_unchanged(monkeypatch):
    """Path and start direction share the target-line frame, so F2P does not move."""
    in_process = SimpleNamespace(
        pid=1,
        run=lambda name, kwargs, timeout_s=None: (cv_worker.JOBS[name](**kwargs), {}),
    )
    monkeypatch.setattr(server, "camera_cv_worker", in_process)
    entries = _record_entries(monkeypatch)
    targeted = replace(
        GEOMETRY, target_px=(GEOMETRY.intrinsics.cx + 25.0, GEOMETRY.intrinsics.cy - 10.0)
    )
    # The scene of the in-worker test, whose path the gates accept.
    scenario = Scenario(attack_deg=-4.0, path_deg=2.0, bright_head=True)
    plain_job = _face_job(shot_number=1, scenario=scenario, departed_ball=True)
    ball = plain_job.departed_ball
    offset_deg = _camera_model(ball, targeted).direction_offset_deg
    assert abs(offset_deg) > 3.0
    # The ball flight measured the same start direction from the target line.
    aimed_job = _face_job(
        shot_number=2,
        scenario=scenario,
        departed_ball=True,
        launch_direction_deg=plain_job.launch_direction_deg + offset_deg,
    )
    aimed_job = replace(aimed_job, kwargs={**aimed_job.kwargs, "geometry": targeted})

    server._run_camera_club_head_path_shadow(plain_job)
    server._run_camera_club_head_path_shadow(aimed_job)

    plain, aimed = (entry["result"] for entry in entries)
    assert plain["accepted"] and aimed["accepted"]
    assert aimed["path_deg"] == pytest.approx(plain["path_deg"] + offset_deg, abs=0.011)
    assert plain["dplane"] is not None and aimed["dplane"] is not None
    assert aimed["dplane"]["face_to_path_deg"] == pytest.approx(
        plain["dplane"]["face_to_path_deg"], abs=0.02
    )
    plain_fields, aimed_fields = (entry["fields"] for entry in entries.club_data)
    assert aimed_fields["face_to_path_class"] == plain_fields["face_to_path_class"]
    assert aimed_fields["spin_axis_class"] == plain_fields["spin_axis_class"]


def test_an_accepted_result_after_publication_sends_one_shot_update(monkeypatch, emitted):
    shot = _stored_shot(monkeypatch)
    shot.club_data_status = "pending"
    before = shot.to_dict()
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    ((update),) = _shot_updates(emitted)
    assert update["pending"] == {}
    assert update["stats"] == {"count": 1}
    data = update["shot"]
    assert (data["club_data_status"], data["club_data_reason"]) == ("done", "accepted")
    assert (data["camera_club_path_deg"], data["camera_club_path_sigma_deg"]) == (2.0, 0.9)
    assert (data["camera_attack_angle_deg"], data["camera_attack_angle_sigma_deg"]) == (-4.3, 0.8)
    assert (data["camera_low_point_cm"], data["camera_low_point_sigma_cm"]) == (11.9, 2.0)
    assert data["dynamic_loft_deg"] is not None and data["spin_loft_deg"] is not None
    assert data["face_to_target_deg"] is not None
    assert data["face_to_path_class"] in ("closed", "neutral", "open")
    assert data["spin_axis_class"] in ("draw", "straight", "fade")
    # E alone (no impact point measured across contact): its sigma widened by 4.9 mm.
    assert (data["impact_toe_mm"], data["impact_sigma_mm"]) == (6.0, 6.6)
    assert (data["impact_offset_mm"], data["impact_method"]) == (6.0, "E")
    # Only the club data changed on the stored shot.
    after = shot.to_dict()
    assert {key for key in after if after[key] != before[key]} <= set(CLUB_DATA_SHOT_FIELDS)


def test_a_result_before_finalization_rides_on_the_final_event(monkeypatch, emitted):
    source = _stored_shot(monkeypatch, shot_number=8)
    final = replace(source)  # the enriched copy finalization copies into the stored shot
    final.club_data_status = "pending"
    finalized, published = threading.Event(), []

    def finalize(shot, **_kwargs):
        published.append(server.shot_to_dict(shot))
        finalized.set()

    monkeypatch.setattr(server, "_finalize_shot_detected", finalize)
    job = _face_job(shot_number=8)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)
    try:
        server._register_shot_for_finalization(source, needs_watchdog=True)

        server._run_camera_club_head_path_shadow(job, source_shot=source, final_shot=final)

        assert _shot_updates(emitted) == []  # not published yet: no extra emit
        assert server._queue_shot_finalization(
            source, emit_event="shot_update", initial_ui_ms=None, final_shot=final
        )
        assert finalized.wait(5.0)
    finally:
        server._reset_shot_sequence()
    ((data),) = published
    assert (data["club_data_status"], data["camera_club_path_deg"]) == ("done", 2.0)
    assert (source.camera_club_path_deg, final.camera_club_path_deg) == (2.0, 2.0)
    assert _shot_updates(emitted) == []


def _raise(*_args, **_kwargs):
    raise RuntimeError("bug")


@pytest.mark.parametrize(
    "case,reason",
    [
        ("next_shot_queued", "skipped_next_shot_queued"),
        ("past_deadline", "skipped_shot_past_deadline"),
        ("timeout", "timeout"),
        ("worker_error", "worker_error"),
        ("exception", "error"),
        ("no_tee_geometry", "skipped_no_tee_geometry"),
    ],
)
def test_every_unrun_job_ends_skipped_with_its_reason(
    monkeypatch, emitted, enrichment_queue, case, reason
):
    shot = _stored_shot(monkeypatch)
    shot.club_data_status = "pending"
    job = _face_job()
    outcome = _accepted_club_data(job)
    if case == "next_shot_queued":
        enrichment_queue.put_nowait(_waiting_shot())
    elif case == "timeout":
        outcome = CameraCvWorkerTimeout("no answer within 6s")
    elif case == "worker_error":
        outcome = CameraCvWorkerError("worker died")
    elif case == "exception":
        monkeypatch.setattr(server, "_club_head_path_shadow_skip_reason", _raise)
    elif case == "no_tee_geometry":
        job = server._club_head_path_shadow_skip(shot, "skipped_no_tee_geometry")
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(outcome))
    monkeypatch.setattr(server, "_restart_camera_cv_worker_when_idle", lambda _worker: None)
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(
        job,
        enrichment_on_time=case != "past_deadline",
        source_shot=shot,
        final_shot=shot,
    )

    assert (shot.club_data_status, shot.club_data_reason) == ("skipped", reason)
    assert shot.camera_club_path_deg is None
    ((update),) = _shot_updates(emitted)
    assert update["shot"]["club_data_status"] == "skipped"


def test_log_mode_sets_nothing_on_the_shot_and_emits_nothing(monkeypatch, emitted):
    monkeypatch.setattr(server, "club_data_mode", "log")
    shot = _stored_shot(monkeypatch)
    before = shot.to_dict()
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert shot.to_dict() == before
    assert emitted == []
    ((entry),) = entries
    assert entry["result"]["club_data_mode"] == "log"
    assert entry["result"]["dplane"]["status"] == "ok"
    ((club_entry),) = entries.club_data
    assert (club_entry["mode"], club_entry["status"]) == ("log", "done")
    assert club_entry["fields"]["camera_club_path_deg"] == 2.0


@pytest.mark.parametrize(
    "mode,skip_reason,expected",
    [("show", None, "pending"), ("log", None, None), ("show", "skipped_test", None)],
    ids=["show", "log", "skip_job"],
)
def test_the_club_data_is_pending_only_for_a_job_that_runs_in_show_mode(
    monkeypatch, mode, skip_reason, expected
):
    monkeypatch.setattr(server, "club_data_mode", mode)
    job = server._ClubHeadPathShadowJob(
        shot_number=1, shot_timestamp=1.0, contact=None, skip_reason=skip_reason
    )
    _shadow_enrichment(monkeypatch, job)
    handed_over = []
    monkeypatch.setattr(
        server,
        "_queue_shot_finalization",
        lambda _shot, **kwargs: handed_over.append(kwargs["final_shot"].club_data_status) or True,
    )
    monkeypatch.setattr(server, "_run_camera_club_head_path_shadow", lambda *_a, **_kw: None)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now())
    shot.shot_number = 1

    server._finish_shot_detected(shot, emit_event="shot")

    assert handed_over == [expected]


def test_the_demo_gets_no_club_data_emit(monkeypatch, emitted):
    shot = _stored_shot(monkeypatch)
    monkeypatch.setattr(server, "demo_session", SimpleNamespace(active=True))
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert emitted == []
    assert shot.club_data_status == "done"


def test_a_deleted_or_cleared_shot_gets_no_emit(monkeypatch, emitted):
    shot = _stored_shot(monkeypatch)
    shot.stored.clear()
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert emitted == []


def _export_snapshot(shot: Shot):
    """What the club data must never move: spin axis, club path, carry and the sim export."""
    resolved = server.resolve_shot(shot, server.SimPlayerState())
    return (
        shot.spin_axis_deg,
        shot.spin_axis_source,
        shot.club_path_deg,
        shot.carry_lateral_yards,
        shot.estimated_carry_yards,
        resolved.as_values(),
        GSProCodec().build_shot(resolved),
    )


def test_without_a_camera_start_direction_the_axis_carry_and_gspro_are_byte_identical(
    monkeypatch, emitted
):
    """A shot whose start direction is not the camera's keeps its axis (here a K-LD7 one)."""
    shot = _stored_shot(monkeypatch)
    shot.club_speed_mph = 85.0
    shot.launch_angle_vertical = 24.3
    shot.launch_angle_horizontal = -1.5
    shot.club_path_deg = 2.0
    shot.spin_axis_deg = -3.0
    shot.spin_axis_source = "calculated"
    server._finalize_shot_metrics(shot)
    without = replace(shot)
    before = _export_snapshot(shot)
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert shot.calculated_spin_axis_deg is not None and shot.camera_club_path_deg == 2.0
    assert _export_snapshot(shot) == before
    # Finalizing again with the club data on the shot gives the same flight and export.
    server._finalize_shot_metrics(shot)
    server._finalize_shot_metrics(without)
    assert _export_snapshot(shot) == _export_snapshot(without)


# --- The camera D-plane axis as the shot's one spin axis (--club-data show) -------------


def _camera_launch(shot: Shot, source: str = "camera_assisted_experimental") -> Shot:
    """The launch of the ``_face_job`` iron: camera start direction, flown ballistically."""
    shot.club_speed_mph = 85.0
    shot.launch_angle_vertical = 24.3
    shot.launch_angle_horizontal = -1.5
    shot.launch_angle_horizontal_confidence = 0.45
    shot.launch_angle_horizontal_source = source
    return shot


_FLOWN_KEYS = (
    "spin_axis_deg",
    "spin_axis_source",
    "curve_class",
    "carry_spin_adjusted",
    "carry_lateral_yards",
    "apex_yards",
    "flight_path",
    "roll_path",
    "total_yards",
    "estimated_carry_yards",
)


def _flown_payload(data: dict) -> tuple:
    """Everything the range draws and the shot list shows of a shot's flight."""
    return tuple(data[key] for key in _FLOWN_KEYS)


def _flown(shot: Shot) -> tuple:
    return _flown_payload(server.shot_to_dict(shot))


@pytest.fixture(name="spans")
def fixture_spans(monkeypatch):
    """The carry spans started, as (shot, flight); none is flown."""
    started = []
    monkeypatch.setattr(server, "_start_carry_span", lambda shot, flight: started.append(flight))
    return started


def test_an_irons_camera_axis_becomes_its_one_axis_curve_and_flight(monkeypatch, emitted, spans):
    shot = _camera_launch(_stored_shot(monkeypatch))
    server._finalize_shot_metrics(shot)  # published straight: no axis yet
    assert (shot.spin_axis_deg, shot.curve_class) == (None, None)
    straight = _flown(shot)
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    axis = shot.calculated_spin_axis_deg
    assert axis is not None and curve_class(axis) != "straight"
    assert (shot.spin_axis_deg, shot.spin_axis_source) == (axis, "calculated")
    assert shot.curve_class == curve_class(axis)
    # Flown exactly as a shot finalized with that axis (the K-LD7 path's timing).
    reference = _camera_launch(Shot(ball_speed_mph=120.0, timestamp=shot.timestamp))
    reference.club = shot.club
    reference.spin_axis_deg, reference.spin_axis_source = axis, "calculated"
    reference.curve_class = curve_class(axis)
    server._finalize_shot_metrics(reference)
    assert _flown(shot) == _flown(reference)
    assert shot.carry_lateral_yards != straight[4]
    # One shot_update with the new flight, one shot_updated entry, the span flown again.
    ((update),) = _shot_updates(emitted)
    assert _flown_payload(update["shot"]) == _flown(shot)
    ((logged),) = entries.updates
    assert (logged["shot_number"], logged["curve_class"], logged["spin_axis_deg"]) == (
        7,
        shot.curve_class,
        axis,
    )
    assert logged["flight_path"] == shot.to_dict()["flight_path"]
    ((flight),) = spans
    assert flight is not None and flight.conditions.spin_axis_deg == axis


def test_the_camera_axis_replaces_a_k_ld7_axis(monkeypatch, emitted, spans):
    shot = _camera_launch(_stored_shot(monkeypatch))
    shot.club_path_deg = -6.0  # a K-LD7 path: the placeholder-AoA axis is a fade
    shot.launch_angle_horizontal_confidence = 0.75  # camera_assisted_high clears the floor
    server._apply_dplane_spin_axis(shot)
    kld7_axis = shot.spin_axis_deg
    assert kld7_axis is not None
    server._finalize_shot_metrics(shot)
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    axis = shot.calculated_spin_axis_deg
    assert axis is not None and axis != kld7_axis
    assert (shot.spin_axis_deg, shot.curve_class) == (axis, curve_class(axis))
    assert len(entries.updates) == 1 and len(_shot_updates(emitted)) == 1


def test_a_k_ld7_axis_stays_when_the_camera_gives_none(monkeypatch, emitted, spans):
    shot = _camera_launch(_stored_shot(monkeypatch))
    shot.club_path_deg = -6.0
    shot.launch_angle_horizontal_confidence = 0.75
    server._apply_dplane_spin_axis(shot)
    assert shot.spin_axis_deg is not None
    server._finalize_shot_metrics(shot)
    before = _flown(shot)
    job = _face_job()
    rejected = ClubHeadPathResult(status="rejected_sigma_too_large", path_deg=2.0, sigma_deg=4.0)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_club_data_result(rejected, job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert shot.calculated_spin_axis_deg is None
    assert _flown(shot) == before
    assert entries.updates == [] and spans == []
    assert len(_shot_updates(emitted)) == 1  # the club data status still goes out


@pytest.mark.parametrize(
    "case",
    ["wood", "ti_start_direction", "shot_direction_not_camera", "log_mode"],
)
def test_no_axis_reaches_the_shot_without_an_iron_camera_axis_in_show_mode(
    monkeypatch, emitted, spans, case
):
    shot = _camera_launch(
        _stored_shot(monkeypatch),
        source="radar" if case == "shot_direction_not_camera" else "camera_assisted_experimental",
    )
    job = _face_job()
    if case == "wood":
        shot.club = ClubType.DRIVER
        job = _face_job(club=ClubType.DRIVER, club_key="dr")
    elif case == "ti_start_direction":
        job = _face_job(launch_direction_status="camera_withheld_fallback_iwr")
    elif case == "log_mode":
        monkeypatch.setattr(server, "club_data_mode", "log")
    server._finalize_shot_metrics(shot)
    before = _flown(shot)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert _flown(shot) == before
    assert (shot.spin_axis_deg, shot.curve_class) == (None, None)
    assert entries.updates == [] and spans == []
    if case == "shot_direction_not_camera":
        assert shot.calculated_spin_axis_deg is not None  # the club data still shows it
    elif case == "log_mode":
        assert emitted == [] and shot.calculated_spin_axis_deg is None
    else:
        assert shot.calculated_spin_axis_deg is None


def test_a_deleted_shot_is_not_flown_again_or_logged(monkeypatch, emitted, spans):
    shot = _camera_launch(_stored_shot(monkeypatch))
    server._finalize_shot_metrics(shot)
    before = _flown(shot)
    shot.stored.clear()
    job = _face_job()
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert _flown(shot)[3:] == before[3:]  # the carry and the flight stay as published
    assert entries.updates == [] and spans == [] and emitted == []


def test_a_camera_axis_before_finalization_flies_with_the_final_shot(monkeypatch, emitted, spans):
    source = _camera_launch(_stored_shot(monkeypatch, shot_number=8))
    final = replace(source)
    final.club_data_status = "pending"
    finalized, published = threading.Event(), []

    def finalize(shot, **_kwargs):
        server._finalize_shot_metrics(shot)
        published.append(server.shot_to_dict(shot))
        finalized.set()

    monkeypatch.setattr(server, "_finalize_shot_detected", finalize)
    job = _face_job(shot_number=8)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)
    try:
        server._register_shot_for_finalization(source, needs_watchdog=True)

        server._run_camera_club_head_path_shadow(job, source_shot=source, final_shot=final)

        assert server._queue_shot_finalization(
            source, emit_event="shot_update", initial_ui_ms=None, final_shot=final
        )
        assert finalized.wait(5.0)
    finally:
        server._reset_shot_sequence()
    ((data),) = published
    axis = data["calculated_spin_axis_deg"]
    assert axis is not None
    assert (data["spin_axis_deg"], data["curve_class"]) == (axis, curve_class(axis))
    assert _flown(source) == _flown_payload(data)
    # The final event and its shot_detected entry carry it: no re-flight, no extra emit.
    assert entries.updates == [] and spans == [] and _shot_updates(emitted) == []


def test_a_teed_shot_calibrates_the_acoustic_delay_and_impacts_collect_head_lengths(
    monkeypatch,
):
    job = _face_job(teed=True, club_key="8i", profile_id="p1")
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    for _ in range(4):
        server._run_camera_club_head_path_shadow(job)
    assert server._club_head_length_median_mm("p1", "8i") is None
    server._run_camera_club_head_path_shadow(job)

    # The live departure fit lies within 5 ms before the stamp bound: five teed samples.
    delay_ms, source, count = server._club_data_calibrator_instance().delay()
    assert (source, count) == ("session", 5)
    assert delay_ms == pytest.approx(job.contact.sound_bound_ms - job.contact.t0_ms)
    assert server._club_head_length_median_mm("p1", "8i") == 74.0
    assert server._club_head_length_median_mm("p1", "PW") is None
    # The next job carries the calibrated delay and the median.
    fake = _FakeWorker(_accepted_club_data(job))
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    server._run_camera_club_head_path_shadow(job)
    assert fake.calls[0][1]["acoustic_delay_ms"] == pytest.approx(delay_ms)


def test_the_head_length_median_learns_from_every_plausible_head_length(monkeypatch):
    """Driver heads read 91-104 mm: a rejected impact still teaches the median its length.

    Learning only from accepted impacts kept the median below the old 95 mm cap; a
    length outside the absolute range (here 130 mm, past a driver's 127 mm) never counts.
    """
    job = _face_job(club_key="Dr", profile_id="p1")
    _record_entries(monkeypatch)

    def run(status: str, head_length_mm: float) -> None:
        impact = ImpactLocation(
            status=status, toe_mm=-8.0, sigma_mm=4.0, head_length_mm=head_length_mm
        )
        result = _accepted_club_data(job)
        result = replace(result, path=replace(result.path, impact=impact))
        monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(result))
        server._run_camera_club_head_path_shadow(job)

    for length in (98.0, 101.0, 104.0):
        run("rejected_implausible_offset", length)
    run("rejected_head_length", 130.0)
    run("accepted", 96.0)
    assert server._club_head_length_median_mm("p1", "Dr") is None
    run("rejected_sigma_too_large", 100.0)

    assert server._club_head_length_median_mm("p1", "Dr") == 100.0


def test_a_mat_shot_never_calibrates_the_acoustic_delay(monkeypatch):
    job = _face_job(teed=False)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    _record_entries(monkeypatch)

    for _ in range(5):
        server._run_camera_club_head_path_shadow(job)

    assert server._club_data_calibrator_instance().delay()[1:] == ("default", 0)


SHADOW_LOG_KEYS = ("t0_shadow_ms", "t0_shadow_source", "t0_shadow_reason", "t0_shadow_minus_t0_ms")
TEE_CONTROL_LOG_KEYS = ("tee_control_n_teed", "tee_control_n_late", "tee_control_passes")


def _late_teed_contact(job, after_bound_ms: float):
    """The job's contact had its live fit lain ``after_bound_ms`` after the stamp sound bound."""
    late_ms = job.contact.sound_bound_ms + after_bound_ms
    time_zero = replace(job.time_zero, time_zero_ms=late_ms, departure_fit_ms=late_ms)
    contact = select_contact_time(
        time_zero, trigger_ns=job.trigger_ns, tee_range_m=job.tee_range_m, teed=True
    )
    assert contact.reason == "departure_fit_after_sound_bound"
    return contact


def test_the_entry_logs_the_shadow_contact_and_publishes_the_used_one(monkeypatch):
    """A shadow candidate reaches the log only: the t0 fields and the shot's club data
    are the ones of the same result without it."""
    job = _face_job()
    shadow = ContactShadow(
        "accepted", "live_departure_fit", t0_ms=-3.41, sigma_s=0.5e-3, minus_t0_ms=2.16
    )
    entries = _record_entries(monkeypatch)
    plain = _accepted_club_data(job)
    for result in (plain, replace(plain, contact_shadow=shadow)):
        monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(result))
        server._run_camera_club_head_path_shadow(job)

    without, with_shadow = (entry["result"] for entry in entries)
    assert set(with_shadow) == LOG_KEYS
    assert {key: with_shadow[key] for key in SHADOW_LOG_KEYS} == {
        "t0_shadow_ms": -3.41,
        "t0_shadow_source": "live_departure_fit",
        "t0_shadow_reason": "accepted",
        "t0_shadow_minus_t0_ms": 2.16,
    }
    assert all(without[key] is None for key in SHADOW_LOG_KEYS)
    skip = {"elapsed_s", *SHADOW_LOG_KEYS, *TEE_CONTROL_LOG_KEYS}
    assert {k: v for k, v in with_shadow.items() if k not in skip} == {
        k: v for k, v in without.items() if k not in skip
    }
    assert with_shadow["t0_ms"] == pytest.approx(job.contact.t0_ms)
    first, second = entries.club_data
    assert first["fields"] == second["fields"]


def test_a_skipped_clip_logs_no_shadow_and_no_path_gap_but_the_tee_control(monkeypatch):
    job = _face_job()
    entries = _record_entries(monkeypatch)
    monkeypatch.setattr(server, "camera_cv_worker", None)

    server._run_camera_club_head_path_shadow(job)

    ((entry),) = entries
    result = entry["result"]
    assert set(result) == LOG_KEYS
    assert result["reason"].startswith("skipped_")
    assert all(result[key] is None for key in SHADOW_LOG_KEYS)
    assert result["path_last_frame_gap_ms"] is None
    assert (result["path_arc_status"], result["path_arc_deg"]) == ("not_run", None)
    assert result["path_noise_sigma"] is None
    assert result["path_arc_radii_deg"] == result["path_arc_window_deg"] == {}
    assert [result[key] for key in TEE_CONTROL_LOG_KEYS] == [0, 0, False]


def test_the_entry_logs_the_path_gap_of_the_last_usable_frame(monkeypatch):
    job = _face_job()
    quality = HeadQuality("ok", 40.0, 300, 20.0, 2.0, 0.0)
    observations = tuple(
        HeadObservation(i, t_ms, {}, quality if ok else replace(quality, status="low_contrast"))
        for i, (t_ms, ok) in enumerate(((-6.9, True), (-3.47, True), (-0.01, False)))
    )
    result = _accepted_club_data(job)
    result = replace(result, path=replace(result.path, observations=observations))
    entries = _record_entries(monkeypatch)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(result))

    server._run_camera_club_head_path_shadow(job)

    assert entries[0]["result"]["path_last_frame_gap_ms"] == 3.47


def test_the_entry_logs_the_arc_path_and_nothing_calculated_reads_it(monkeypatch):
    job = _face_job()
    plain = _accepted_club_data(job)
    arc = {
        "path_arc_status": "accepted",
        "path_arc_deg": 9.87,
        "path_arc_sigma_deg": 1.2,
        "path_arc_radii_deg": {"0.25": 9.0, "0.30": 9.87, "0.35": 10.5},
        "path_arc_window_deg": {"drop_last": 9.5, "drop_first": 10.1},
    }
    entries = _record_entries(monkeypatch)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(plain))
    server._run_camera_club_head_path_shadow(job)
    monkeypatch.setattr(
        server, "camera_cv_worker", _FakeWorker(replace(plain, path=replace(plain.path, **arc)))
    )
    server._run_camera_club_head_path_shadow(job)

    assert entries[0]["result"]["path_noise_sigma"] is None
    without, with_arc = (entry["result"] for entry in entries)
    assert {key: with_arc[key] for key in arc} == arc
    assert with_arc["path_deg"] == without["path_deg"]
    assert without["path_arc_status"] == "not_run"


def test_the_entry_logs_the_background_noise(monkeypatch):
    job = _face_job()
    result = _accepted_club_data(job)
    result = replace(result, path=replace(result.path, noise_sigma=3.456))
    entries = _record_entries(monkeypatch)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(result))

    server._run_camera_club_head_path_shadow(job)

    assert entries[0]["result"]["path_noise_sigma"] == 3.456


def test_the_tee_control_counts_teed_departures_and_logs_its_state_at_the_start(monkeypatch):
    """Each entry logs the control as its job found it; from five teed fits on, at most
    10 % late, it passes. A mat shot never counts; a session reset forgets it."""
    job = _face_job(teed=True)
    late_job = replace(job, contact=_late_teed_contact(job, 0.5))
    mat_job = replace(job, teed=False, contact=_late_teed_contact(job, 0.5))
    entries = _record_entries(monkeypatch)

    def run(the_job):
        monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(the_job)))
        server._run_camera_club_head_path_shadow(the_job)
        return [entries[-1]["result"][key] for key in TEE_CONTROL_LOG_KEYS]

    assert [run(job) for _ in range(5)] == [[n, 0, False] for n in range(5)]
    assert run(mat_job) == [5, 0, True]
    assert run(late_job) == [5, 0, True]
    assert server._club_data_calibrator_instance().tee_control.state() == (6, 1, False)
    assert [run(job) for _ in range(4)] == [[n, 1, False] for n in range(6, 10)]
    assert run(job) == [10, 1, True]
    # The late fit was no delay sample; the mat shot nothing at all.
    assert server._club_data_calibrator_instance().delay()[2] == 10

    server._reset_club_data_state("session clear")

    assert run(job) == [0, 0, False]


@pytest.mark.parametrize(
    ("failure", "reason", "extra_keys"),
    [
        (CameraCvWorkerTimeout("no answer within 3s"), "timeout", set()),
        (CameraCvJobError("club_head_path raised ValueError: bad clip"), "job_error", {"error"}),
    ],
)
def test_a_failed_job_logs_the_new_keys_and_leaves_the_tee_control(
    monkeypatch, failure, reason, extra_keys
):
    """A timeout or a job error logs the shadow and the path gap as null and the tee control
    as the job found it, and learns nothing: a late driver fit would have counted as late."""
    job = _face_job(teed=True)
    late_job = replace(job, contact=_late_teed_contact(job, 0.5))
    calibrator = server._club_data_calibrator_instance()
    for _ in range(2):
        assert calibrator.add_contact(job.contact, teed=True)
    found = calibrator.tee_control.state()
    delay_n = calibrator.delay()[2]
    assert found == (2, 0, False)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(failure))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(late_job)
    _join_restarts()

    result = entries[0]["result"]
    # All 8 new keys are present: the shadow and the path gap null, the tee control
    # as the job found it (it is logged for skipped clips too).
    assert set(result) == LOG_KEYS | extra_keys
    assert (result["accepted"], result["reason"]) == (False, reason)
    null_keys = (*SHADOW_LOG_KEYS, "path_last_frame_gap_ms")
    assert {key: result[key] for key in null_keys} == dict.fromkeys(null_keys)
    assert [result[key] for key in TEE_CONTROL_LOG_KEYS] == [2, 0, False]
    assert server._club_data_calibrator_instance() is calibrator
    assert calibrator.tee_control.state() == found
    assert calibrator.delay()[2] == delay_n


def test_a_teed_clip_on_the_mark_counts_for_the_tee_control(monkeypatch):
    """A mark-anchored ball feeds no median but its contact counts like any other."""
    job = _face_job(teed=True)
    job = replace(job, contact=_late_teed_contact(job, 0.5))
    ball = ClubBall(ReferenceBall(360.3, 176.6, 16.4, 211), "refined", "mark", refined=True)
    monkeypatch.setattr(
        server, "camera_cv_worker", _FakeWorker(replace(_accepted_club_data(job), ball=ball))
    )
    _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job)

    assert server._club_data_calibrator_instance().tee_control.state() == (1, 1, False)


def test_the_handedness_defaults_to_right_and_a_simulator_overrides_it(monkeypatch, emitted):
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    monkeypatch.setattr(server, "monitor", None)
    monkeypatch.setattr(server, "sim_player_state", server.SimPlayerState())
    assert server._golfer_handedness() == "right"

    server._sim_on_inbound("gspro", PlayerUpdate(handed="LH"))
    assert server._golfer_handedness() == "left"

    server._sim_on_inbound("gspro", PlayerUpdate(club=None))
    assert server._golfer_handedness() == "left"  # an update without a hand keeps it
    server._sim_on_inbound("gspro", PlayerUpdate(handed="RH"))
    monkeypatch.setattr(server, "golfer_handedness", "left")
    assert server._golfer_handedness() == "right"


def test_a_left_handed_golfer_gets_no_face(monkeypatch, emitted):
    shot = _stored_shot(monkeypatch)
    job = _face_job(handedness="left")
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_accepted_club_data(job)))
    entries = _record_entries(monkeypatch)

    server._run_camera_club_head_path_shadow(job, source_shot=shot, final_shot=shot)

    assert shot.camera_club_path_deg == 2.0
    assert shot.face_to_path_class is None and shot.face_to_target_deg is None
    assert entries[0]["result"]["dplane"] is None
    assert entries[0]["result"]["dplane_reason"] == "left_handed_unsupported"


@pytest.mark.parametrize(
    "extra,expected",
    [
        ((), ("show", "right")),
        (("--club-data", "log", "--golfer-handedness", "left"), ("log", "left")),
    ],
    ids=["defaults", "log_left"],
)
def test_cli_sets_the_club_data_mode_and_the_handedness(monkeypatch, tmp_path, extra, expected):
    monkeypatch.setattr(server, "club_data_mode", "unset")
    monkeypatch.setattr(server, "golfer_handedness", "unset")

    _main_args(monkeypatch, tmp_path, *extra)

    assert (server.club_data_mode, server.golfer_handedness) == expected


# --- The impact point (camera.impact_point, method D) through the server ----------------


def _impact_point(**fields):
    from openflight.camera.impact_point import ImpactPoint  # noqa: PLC0415

    values = {
        "offset_mm": 9.46,
        "sigma_mm": 2.71,
        "method": "interpolated",
        "height_mm": 33.4,
        "height_sigma_mm": 2.76,
        "head_length_mm": 80.2,
        "scale_mm_per_px": 2.594,
        "scale_source": "geometry",
        "ball_diameter_px": 16.4,
        "e_scale_ratio": 1.0,
    }
    values.update(fields)
    return ImpactPoint(values.pop("status", "accepted"), **values)


def _club_data_with_point(job, **fields) -> ClubDataResult:
    result = _accepted_club_data(job)
    return replace(result, path=replace(result.path, impact_point=_impact_point(**fields)))


def test_the_job_carries_the_impact_point_inputs(monkeypatch, tmp_path):
    capture = _tiny_capture(monkeypatch, tmp_path)
    estimate = CameraBallEstimate(
        status="accepted",
        vertical_deg=14.25,
        locator=BallLocatorDiagnostics(
            "disappeared", "ok", ball_x=310.0, ball_y=270.5, ball_diameter_px=16.0, teed=True
        ),
    )
    monkeypatch.setattr(server, "camera_cv_worker", _RecordingWorker(estimate))
    shot = Shot(
        ball_speed_mph=131.5, timestamp=datetime.now(), club_speed_mph=95.0, club=ClubType.WOOD_3
    )
    shot.shot_number, shot.profile_id = 6, "p2"

    job = server._fuse_camera_ball_flight(shot, capture)

    assert (job.ball_speed_mph, job.ball_teed, job.teed) == (131.5, True, False)
    assert job.impact_point_head_length_median_mm is None
    assert not hasattr(job, "ball_diameter_median_px")  # the scale is the geometry's


def test_the_worker_job_hands_the_impact_point_its_inputs():
    job = _face_job(ball_speed_mph=101.2, impact_point_head_length_median_mm=80.0)

    kwargs = server._club_data_worker_kwargs(job, None)

    assert kwargs["ball_speed_mph"] == 101.2
    assert (kwargs["launch_vertical_deg"], kwargs["launch_horizontal_deg"]) == (24.3, -1.5)
    assert kwargs["impact_point_head_length_median_mm"] == 80.0
    assert "ball_diameter_median_px" not in kwargs


def test_the_impact_point_learns_its_own_head_lengths_and_no_ball_size(monkeypatch):
    job = _face_job(club_key="8i", profile_id="p1")
    _record_entries(monkeypatch)

    def run(**fields) -> None:
        monkeypatch.setattr(
            server, "camera_cv_worker", _FakeWorker(_club_data_with_point(job, **fields))
        )
        server._run_camera_club_head_path_shadow(job)

    point_lengths = server._impact_point_head_length_mm
    run(head_length_mm=79.0)
    run(head_length_mm=81.0)
    run(status="rejected_sigma_too_large", head_length_mm=80.0)
    # A length outside the range never counts.
    run(head_length_mm=140.0)
    assert server._club_head_length_median_mm("p1", "8i", point_lengths) is None
    run(head_length_mm=82.0)
    run(head_length_mm=78.0)

    assert server._club_head_length_median_mm("p1", "8i", point_lengths) == 80.0
    # E's head lengths are their own state (74 mm on every run here).
    assert server._club_head_length_median_mm("p1", "8i") == 74.0

    server._reset_club_data_state("test")
    assert not server._impact_point_head_length_mm


def test_the_server_keeps_no_session_ball_size():
    """Owner decision 2026-11-03: the ball has the standard size, nothing learns it."""
    for name in ("_ball_diameter_px", "_ball_diameter_median_px", "_BALL_DIAMETER_MIN_SAMPLES"):
        assert not hasattr(server, name), name
    assert "ball_diameter_median_px" not in {
        f.name for f in dataclass_fields(server._ClubHeadPathShadowJob)
    }


def test_a_ball_on_the_mark_never_feeds_the_session_medians(monkeypatch):
    """Its ball is the refine's find around the tap: its head lengths teach no median."""
    job = _face_job(club_key="6i", profile_id="p1")
    _record_entries(monkeypatch)

    def run(anchor: str, head_length_mm: float) -> None:
        result = _club_data_with_point(job, head_length_mm=head_length_mm)
        ball = ClubBall(ReferenceBall(360.3, 176.6, 16.4, 211), "refined", anchor, refined=True)
        monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(replace(result, ball=ball)))
        server._run_camera_club_head_path_shadow(job)

    run("departed", 70.0)
    run("mark", 95.0)
    run("tracker", 71.0)
    run("mark", 96.0)
    run("departed", 72.0)

    assert server._impact_point_head_length_mm[("p1", "6i")] == [70.0, 71.0, 72.0]
    assert len(server._club_head_length_mm[("p1", "6i")]) == 3


@pytest.mark.parametrize(
    ("club", "teed", "ball_teed", "height"),
    [
        (ClubType.DRIVER, True, None, 33.4),
        (ClubType.WOOD_3, False, True, 33.4),
        (ClubType.WOOD_3, False, False, None),
        (ClubType.IRON_8, False, True, None),
    ],
)
def test_the_face_height_reaches_the_shot_for_a_teed_wood_only(
    monkeypatch, club, teed, ball_teed, height
):
    job = _face_job(club=club, teed=teed, ball_teed=ball_teed)
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(_club_data_with_point(job)))
    entries = _record_entries(monkeypatch)
    shot = Shot(ball_speed_mph=120.0, timestamp=datetime.now(), club=club)

    server._run_camera_club_head_path_shadow(job, source_shot=shot)

    assert (shot.impact_offset_mm, shot.impact_toe_mm, shot.impact_method) == (9.5, 9.5, "D")
    assert shot.impact_height_mm == height
    assert shot.impact_scale_mm_per_px == 2.59
    ((club_entry),) = entries.club_data
    assert club_entry["fields"]["impact_height_mm"] == height
    ((entry),) = entries
    assert entry["result"]["impact_point"]["offset_mm"] == 9.46
    assert entry["result"]["ball_teed"] is ball_teed
