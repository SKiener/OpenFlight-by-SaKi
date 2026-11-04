"""The low-light camera start direction in shadow: built after fusion, run after publication, log only.

The live ball gate and everything on the shot stay exactly as before; the shadow
writes one ``camera_lowlight_ball_flight`` session-log entry per clip.
"""

import copy
import queue
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from test_camera_club_head_path_shadow import _FakeWorker, _join_restarts, _waiting_shot
from test_camera_cv_worker import _main_args
from test_camera_departed_ball import (
    GEOMETRY,
    MPH_PER_MS,
    SPEED_MS,
    ZONE,
    _evidence,
    _render,
    _trigger_ns,
)
from test_camera_flicker import FLAT_OBSERVATION, FLOODLIGHT, _flickering

from openflight import server
from openflight.camera.ball_flight import BallLocatorDiagnostics, CameraBallEstimate
from openflight.camera.club_delivery import ReferenceBallTracker
from openflight.camera.cv_worker import (
    CameraCvJobError,
    CameraCvWorker,
    CameraCvWorkerError,
    CameraCvWorkerTimeout,
)
from openflight.camera.departed_ball import GroundBallAnchor
from openflight.camera.flicker import FlickerMeasurement
from openflight.camera.lowlight import LowLightBallFlight
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger

DAYLIGHT = {
    "status": "ready",
    "analysis_eligible": True,
    "motion_blur_risk": "low",
    "observation": {"status": "good", "p90": 180.0, "dark_pct": 0.0},
    "ball_analysis_eligible": True,
}
NEARLY_BLACK = {**FLOODLIGHT, "observation": {"status": "too_dark", "p90": 4.0, "dark_pct": 99.0}}
LIVE_FIELDS = (
    "experimental_camera_horizontal_deg",
    "experimental_camera_horizontal_confidence",
    "experimental_camera_horizontal_status",
    "experimental_camera_iwr_delta_deg",
    "experimental_camera_ball_locator",
    "experimental_camera_vertical_deg",
    "launch_angle_horizontal",
    "launch_angle_horizontal_source",
    "launch_angle_horizontal_confidence",
    "experimental_fused_status",
    "angle_reference",
)


@pytest.fixture(name="enrichment_queue", autouse=True)
def fixture_enrichment_queue(monkeypatch):
    fresh = queue.Queue(maxsize=server._SHOT_ENRICHMENT_QUEUE_CAPACITY)
    monkeypatch.setattr(server, "shot_enrichment_queue", fresh)
    return fresh


@pytest.fixture(name="entries")
def fixture_entries(monkeypatch):
    """The shadow entries the session log receives."""
    recorded = []
    session = SimpleNamespace(
        log_camera_lowlight_ball_flight=lambda **kwargs: recorded.append(kwargs),
        log_camera_club_head_path=lambda **_kwargs: None,
        log_club_data=lambda **_kwargs: None,
        log_shot_update=lambda _shot: None,
    )
    monkeypatch.setattr(server, "get_session_logger", lambda: session)
    return recorded


@pytest.fixture(name="session_errors", autouse=True)
def fixture_session_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(server, "log_session_error", lambda *args, **kwargs: errors.append(args))
    return errors


@pytest.fixture(name="worker", scope="module")
def fixture_worker():
    worker = CameraCvWorker(timeout_s=60.0)
    yield worker
    worker.close()


def _save_clip(path, frames, times, *, exposure_us=1250, gain=16.0):
    path.mkdir(parents=True, exist_ok=True)
    np.savez(
        path / "frames.npz",
        frames=frames,
        host_timestamp_ns=times,
        trigger_host_timestamp_ns=np.int64(_trigger_ns()),
        pre_trigger_count=np.int32(45),
        exposure_us=np.full(len(frames), exposure_us, dtype=np.int32),
        analogue_gain=np.full(len(frames), gain, dtype=np.float32),
    )
    return path


def _capture(path, auto_exposure):
    return SimpleNamespace(
        valid=True,
        path=path,
        metadata={"auto_exposure": copy.deepcopy(auto_exposure)},
        trigger_timestamp=None,
        error=None,
        sequence=1,
    )


def _shot(number=7):
    shot = Shot(ball_speed_mph=SPEED_MS * MPH_PER_MS, timestamp=datetime.now())
    shot.shot_number = number
    shot.impact_timestamp = 1793366991.29
    shot.iwr6843_ball_range_evidence = _evidence()
    return shot


@pytest.fixture(name="scene")
def fixture_scene(monkeypatch):
    """Server globals of the synthetic mount (test_camera_departed_ball) and fresh shadow state."""
    monkeypatch.setattr(server, "camera_lowlight_shadow_enabled", True)
    monkeypatch.setattr(server, "camera_intrinsics", None)
    monkeypatch.setattr(server, "camera_cv_worker", None)
    monkeypatch.setattr(server, "camera_ball_flight_reference_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_reference_ball_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_ball_flight_ground_anchor", GroundBallAnchor())
    monkeypatch.setattr(server, "camera_lowlight_reference_tracker", ReferenceBallTracker())
    monkeypatch.setattr(server, "camera_lowlight_ground_anchor", GroundBallAnchor())
    monkeypatch.setattr(
        server,
        "camera_capture_runtime",
        SimpleNamespace(
            camera_analysis_eligible=True,
            active_tee_zone=lambda: ZONE,
            trigger_check_anchor=lambda: None,
        ),
    )
    monkeypatch.setattr(
        server,
        "iwr6843_runtime",
        SimpleNamespace(
            calibration=SimpleNamespace(
                tee_range_m=GEOMETRY.tee_range_m,
                radar_height_m=GEOMETRY.radar_height_m,
                tee_ball_height_m=GEOMETRY.ball_height_m,
            )
        ),
    )
    monkeypatch.setattr(
        server,
        "camera_capture_config",
        {
            "mount_height_m": GEOMETRY.camera_height_m,
            "width": GEOMETRY.image_width_px,
            "height": GEOMETRY.image_height_px,
            "ball_locator": "disappeared",
        },
    )
    monkeypatch.setattr(
        server, "_run_club_data_job", lambda *_a: (None, "skipped_test", None, False)
    )


def _floodlit_clip(tmp_path):
    frames, times = _render(mat=46, ball=230)
    return _capture(_save_clip(tmp_path / "flood", _flickering(frames), times), FLOODLIGHT)


def _live_fields(shot):
    return {name: copy.deepcopy(getattr(shot, name)) for name in LIVE_FIELDS}


# --- end to end in the worker -------------------------------------------------------------


def test_a_floodlit_clip_gets_a_shadow_direction_and_the_shot_stays_as_live(
    scene, tmp_path, worker, monkeypatch, entries
):  # pylint: disable=unused-argument
    capture = _floodlit_clip(tmp_path)
    shot = _shot()
    server._fuse_camera_measurements(shot, capture)
    live = _live_fields(shot)
    worker.start()
    monkeypatch.setattr(server, "camera_cv_worker", worker)

    job = server._lowlight_shadow_job(shot, capture)
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    assert live["experimental_camera_horizontal_status"] == "rejected_lighting_quality"
    assert live["experimental_camera_horizontal_deg"] is None
    assert _live_fields(shot) == live
    (entry,) = entries
    assert (entry["shot_number"], entry["shot_timestamp"]) == (7, shot.impact_timestamp)
    result = entry["result"]
    assert (result["status"], result["reason"], result["lowlight_gate"]) == (
        "done",
        "ran",
        "relaxed",
    )
    assert result["lowlight_camera_horizontal_deg"] == pytest.approx(4.0, abs=0.5)
    assert result["lowlight_camera_horizontal_confidence"] is not None
    assert result["lowlight_new_direction"] is True
    assert result["lowlight_minus_live_deg"] is None
    assert result["flicker_compensated"] is True
    assert result["flicker_std"] > 0.3
    assert result["lowlight_legacy_fallback"] is False
    assert result["lowlight_locator"]["locator"] == "disappeared"
    assert result["live_ball_analysis_eligible"] is False
    assert result["live_camera_horizontal_status"] == "rejected_lighting_quality"
    # The shadow saw the ball in its own anchor; the live ground anchor saw nothing. The
    # shot is a driver on its profile tee, so the ball feeds neither pose median.
    assert server.camera_lowlight_ground_anchor.last is not None
    assert server.camera_lowlight_ground_anchor.samples == 0
    assert server.camera_ball_flight_ground_anchor.last is None


def test_a_daylight_clip_logs_the_shadow_next_to_the_live_direction(
    scene, tmp_path, worker, monkeypatch, entries
):  # pylint: disable=unused-argument
    frames, times = _render()
    capture = _capture(_save_clip(tmp_path / "day", frames, times, exposure_us=300), DAYLIGHT)
    shot = _shot()
    monkeypatch.setattr(server, "camera_cv_worker", worker)
    worker.start()
    server._fuse_camera_measurements(shot, capture)
    live = _live_fields(shot)

    server._run_camera_lowlight_shadow(
        server._lowlight_shadow_job(shot, capture), enrichment_on_time=True
    )

    result = entries[0]["result"]
    assert live["experimental_camera_horizontal_deg"] == pytest.approx(4.0, abs=0.5)
    assert _live_fields(shot) == live
    assert result["lowlight_gate"] == "live"
    assert result["flicker_compensated"] is False
    assert result["lowlight_legacy_fallback"] is True
    assert result["lowlight_new_direction"] is False
    assert result["live_camera_horizontal_deg"] == live["experimental_camera_horizontal_deg"]
    assert result["lowlight_minus_live_deg"] == pytest.approx(0.0, abs=1e-3)


def test_the_session_log_gets_its_own_entry_type(tmp_path):
    logger = SessionLogger(log_dir=tmp_path, location="t")
    logger.start_session(config={})

    logger.log_camera_lowlight_ball_flight(
        shot_number=3, shot_timestamp=1.5, result={"status": "done", "reason": "ran"}
    )
    logger.end_session()

    text = next(tmp_path.glob("*.jsonl")).read_text()
    assert '"type": "camera_lowlight_ball_flight"' in text
    assert '"shadow": true' in text


# --- building the job ----------------------------------------------------------------------


def test_the_job_reads_the_shot_and_never_changes_it(scene, tmp_path):  # pylint: disable=unused-argument
    capture = _floodlit_clip(tmp_path)
    shot = _shot()
    shot.experimental_camera_horizontal_status = "rejected_lighting_quality"
    before = copy.deepcopy(shot.to_dict())

    job = server._lowlight_shadow_job(shot, capture)

    assert shot.to_dict() == before
    assert job.skip_reason is None
    assert job.live_ball_analysis_eligible is False
    assert job.live_camera_horizontal_status == "rejected_lighting_quality"
    assert job.tee_zone is ZONE
    assert job.ops_ball_speed_mph == pytest.approx(SPEED_MS * MPH_PER_MS)
    assert job.geometry.tee_range_m == GEOMETRY.tee_range_m


@pytest.mark.parametrize("case", ["disabled", "mock", "no_capture", "invalid_capture"])
def test_no_job_without_a_valid_clip_or_with_the_shadow_off(scene, tmp_path, monkeypatch, case):  # pylint: disable=unused-argument
    capture = _floodlit_clip(tmp_path)
    shot = _shot()
    if case == "disabled":
        monkeypatch.setattr(server, "camera_lowlight_shadow_enabled", False)
    elif case == "mock":
        shot.mode = "mock"
    elif case == "no_capture":
        capture = None
    else:
        capture.valid = False

    assert server._lowlight_shadow_job(shot, capture) is None


def test_a_clip_without_tee_geometry_is_logged_as_skipped(scene, tmp_path, monkeypatch, entries):  # pylint: disable=unused-argument
    monkeypatch.setattr(server, "iwr6843_runtime", None)

    job = server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path))
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    assert job.skip_reason == "skipped_no_iwr_runtime"
    result = entries[0]["result"]
    assert (result["status"], result["reason"]) == ("skipped", "skipped_no_iwr_runtime")


def test_a_job_that_cannot_be_built_costs_the_shot_nothing(scene, tmp_path, monkeypatch):  # pylint: disable=unused-argument
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_lowlight_shadow_job", broken)

    assert server._optional_lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path)) is None


# --- running it ----------------------------------------------------------------------------


def _estimate(deg=2.5, tier="high"):
    return CameraBallEstimate(
        "accepted",
        confidence_tier=tier,
        horizontal_deg=deg,
        vertical_deg=18.0,
        depth_source="iwr_range",
        locator=BallLocatorDiagnostics("disappeared", "located", snr=14.0),
    )


def _result(deg=2.5, tier="high"):
    return LowLightBallFlight(
        estimate=_estimate(deg, tier),
        flicker=FlickerMeasurement(True, "compensated", rel_std=0.36, elapsed_ms=110.0),
        legacy_fallback=False,
        ball_locator="disappeared",
        elapsed_ms=300.0,
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("next_shot_queued", "skipped_next_shot_queued"),
        ("past_deadline", "skipped_shot_past_deadline"),
        ("in_process", "skipped_cv_in_process"),
        ("worker_not_running", "skipped_worker_not_running"),
        ("nearly_black", "skipped_gate_nearly_black"),
        ("flat_frame", "skipped_gate_flat_frame"),
        ("unknown_controls", "skipped_gate_unknown_controls"),
        ("no_frames", "skipped_missing_frames"),
    ],
)
def test_the_shadow_skips_like_the_club_data_job(
    scene, tmp_path, monkeypatch, entries, enrichment_queue, case, reason
):  # pylint: disable=unused-argument,too-many-arguments
    capture = _floodlit_clip(tmp_path)
    fake = _FakeWorker(_result())
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    if case == "next_shot_queued":
        enrichment_queue.put_nowait(_waiting_shot())
    elif case == "in_process":
        monkeypatch.setattr(server, "camera_cv_worker", None)
    elif case == "worker_not_running":
        fake.pid = None
    elif case == "nearly_black":
        capture.metadata["auto_exposure"] = NEARLY_BLACK
    elif case == "flat_frame":
        capture.metadata["auto_exposure"] = {
            **FLOODLIGHT,
            "status": "no_image",
            "frame_flat": True,
            "observation": FLAT_OBSERVATION,
        }
    elif case == "unknown_controls":
        frames, times = _render(mat=46, ball=230)
        capture.path = _save_clip(tmp_path / "unknown", frames, times, gain=0.0)
    elif case == "no_frames":
        (capture.path / "frames.npz").unlink()

    job = server._lowlight_shadow_job(_shot(), capture)
    server._run_camera_lowlight_shadow(job, enrichment_on_time=case != "past_deadline")

    assert fake.calls == []
    result = entries[0]["result"]
    assert (result["status"], result["reason"]) == ("skipped", reason)
    assert "lowlight_camera_horizontal_deg" not in result


def test_the_worker_gets_the_shadow_state_and_the_server_adopts_it(
    scene, tmp_path, monkeypatch, entries
):  # pylint: disable=unused-argument
    learnt = GroundBallAnchor()
    learnt._samples = [object()]  # pylint: disable=protected-access

    class Worker(_FakeWorker):
        def run(self, job, kwargs, *, timeout_s=None):
            self.calls.append((job, kwargs, timeout_s))
            return _result(), {"ground_anchor": learnt}

    fake = Worker(None)
    monkeypatch.setattr(server, "camera_cv_worker", fake)

    job = server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path))
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    ((name, kwargs, timeout_s),) = fake.calls
    assert name == "lowlight_ball_flight"
    assert timeout_s == server._CAMERA_LOWLIGHT_BUDGET_S
    assert kwargs["relaxed_only"] is True
    assert kwargs["ground_anchor"] is server.camera_lowlight_ground_anchor
    assert kwargs["ball_tracker"] is server.camera_lowlight_reference_tracker
    assert kwargs["ground_anchor"] is not server.camera_ball_flight_ground_anchor
    assert server.camera_lowlight_ground_anchor.samples == 1
    result = entries[0]["result"]
    assert result["lowlight_camera_horizontal_deg"] == 2.5
    assert result["lowlight_camera_horizontal_status"] == "camera_assisted_high"
    assert result["lowlight_camera_horizontal_confidence"] == 0.75
    assert result["lowlight_camera_vertical_deg"] == 18.0
    assert result["flicker_std"] == 0.36


@pytest.mark.parametrize(
    ("outcome", "reason", "restart"),
    [
        (CameraCvWorkerTimeout("no answer within 5s"), "timeout", True),
        (CameraCvWorkerError("worker died"), "worker_error", True),
        (CameraCvJobError("lowlight_ball_flight raised ValueError: x"), "job_error", False),
    ],
)
def test_a_failed_job_is_logged_and_never_raises(
    scene, tmp_path, monkeypatch, entries, session_errors, outcome, reason, restart
):  # pylint: disable=unused-argument,too-many-arguments
    restarts = []
    monkeypatch.setattr(server, "camera_cv_worker", _FakeWorker(outcome))
    monkeypatch.setattr(server, "_restart_camera_cv_worker_when_idle", restarts.append)

    job = server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path))
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)
    _join_restarts()

    result = entries[0]["result"]
    assert (result["status"], result["reason"]) == ("skipped", reason)
    assert bool(restarts) is restart
    assert session_errors == []


def test_an_unexpected_error_is_reported_not_raised(
    scene, tmp_path, monkeypatch, entries, session_errors
):  # pylint: disable=unused-argument
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(server, "_after_publication_skip_reason", broken)
    job = server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path))

    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    assert entries == []
    assert session_errors and session_errors[0][0] == "Camera low-light shadow failed"


def test_no_session_log_no_work(scene, tmp_path, monkeypatch):  # pylint: disable=unused-argument
    fake = _FakeWorker(_result())
    monkeypatch.setattr(server, "camera_cv_worker", fake)
    monkeypatch.setattr(server, "get_session_logger", lambda: None)

    job = server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path))
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    assert fake.calls == []


# --- after publication ---------------------------------------------------------------------


def test_the_shadow_runs_after_the_hand_off_and_after_the_club_data(monkeypatch):
    order = []
    job = server._LowLightShadowJob(shot_number=1, shot_timestamp=1.0)
    club_job = server._club_head_path_shadow_skip(_shot(), "skipped_no_tee_geometry")
    handed = []

    monkeypatch.setattr(
        server,
        "_enrich_shot_from_optional_hardware",
        lambda _shot: server._ShotEnrichmentResult(
            camera_club_head_path_job=club_job, camera_lowlight_job=job
        ),
    )

    def queue_finalization(_shot, **kwargs):
        handed.append(kwargs["enrichment"])
        order.append("queued")
        return True

    monkeypatch.setattr(server, "_queue_shot_finalization", queue_finalization)
    monkeypatch.setattr(
        server, "_run_camera_club_head_path_shadow", lambda *_a, **_k: order.append("club_data")
    )
    monkeypatch.setattr(
        server,
        "_run_camera_lowlight_shadow",
        lambda queued, **kwargs: order.append(("lowlight", queued, kwargs["enrichment_on_time"])),
    )

    server._finish_shot_detected(_shot(), emit_event="shot")

    assert order == ["queued", "club_data", ("lowlight", job, True)]
    assert handed[0].camera_lowlight_job is None
    assert handed[0].camera_club_head_path_job is None


def test_the_enrichment_builds_the_job_after_the_live_fusion(scene, tmp_path, monkeypatch):  # pylint: disable=unused-argument
    capture = _floodlit_clip(tmp_path)
    seen = {}
    monkeypatch.setattr(server, "_process_iwr6843_angle", lambda _shot: None)
    monkeypatch.setattr(server, "_snapshot_inclinometer_for_shot", lambda _shot: None)
    monkeypatch.setattr(server, "kld7_vertical", None)
    monkeypatch.setattr(server, "kld7_horizontal", None)
    monkeypatch.setattr(
        server,
        "camera_capture_runtime",
        SimpleNamespace(
            capture_for_shot=lambda *_a, **_k: capture,
            note_accepted_shot=lambda *_a, **_k: None,
            active_tee_zone=lambda: ZONE,
            trigger_check_anchor=lambda: None,
            camera_analysis_eligible=True,
        ),
    )
    original = server._lowlight_shadow_job

    def spy(shot, clip):
        seen["status"] = shot.experimental_camera_horizontal_status
        return original(shot, clip)

    monkeypatch.setattr(server, "_lowlight_shadow_job", spy)
    monkeypatch.setattr(server, "_fuse_camera_measurements", _mark_live)
    monkeypatch.setattr(server, "get_session_logger", lambda: None)

    enrichment = server._enrich_shot_from_optional_hardware(_shot())

    assert seen["status"] == "fused"
    assert isinstance(enrichment.camera_lowlight_job, server._LowLightShadowJob)


def _mark_live(shot, _capture, **_kwargs):
    shot.experimental_camera_horizontal_status = "fused"


def test_the_shadow_is_on_by_default():
    assert server.camera_lowlight_shadow_enabled is True
    assert server._CAMERA_LOWLIGHT_BUDGET_S > 0


@pytest.mark.parametrize(
    ("extra", "expected"), [((), True), (("--no-camera-lowlight-shadow",), False)]
)
def test_cli_turns_the_shadow_off(monkeypatch, tmp_path, extra, expected):
    monkeypatch.setattr(server, "camera_lowlight_shadow_enabled", "unset")

    _main_args(monkeypatch, tmp_path, *extra)

    assert server.camera_lowlight_shadow_enabled is expected


def test_replace_keeps_the_job_frozen():
    job = server._LowLightShadowJob(shot_number=1, shot_timestamp=1.0)

    assert replace(job, skip_reason="x").skip_reason == "x"
    with pytest.raises(AttributeError):
        job.skip_reason = "y"


# --- the worker starts with the camera (range 2026-11-02, session 2) ------------------------
#
# Under the floodlight no clip was ball-eligible, so the lazily started worker never
# ran and all 10 shadow jobs were skipped_worker_not_running.


class _LazyWorker(_FakeWorker):
    """Not running until ``start``, like ``CameraCvWorker``; ``crash`` ends the process."""

    pid = None

    def start(self, *, timeout_s=None):
        pid = super().start(timeout_s=timeout_s)
        self.pid = pid
        return pid

    def crash(self):
        self.pid = None


def _init_camera(monkeypatch, tmp_path, worker, *, shadow=True):
    """``init_camera_capture`` with a fake camera and ``worker`` as the CV worker."""
    from openflight.camera import capture_runtime, cv_worker  # noqa: PLC0415

    class FakeRuntime:
        def __init__(self, *, settings, **_kwargs):
            self.settings = settings

        def start(self):
            pass

        def set_ball_mark_source(self, _provider):
            pass

    monkeypatch.setattr(capture_runtime, "CameraCaptureRuntime", FakeRuntime)
    monkeypatch.setattr(cv_worker, "CameraCvWorker", lambda **_kwargs: worker)
    monkeypatch.setattr(server, "camera_lowlight_shadow_enabled", shadow)
    monkeypatch.setattr(server, "camera_replay_manager", server.camera_replay_manager)
    config, runtime = server.camera_capture_config, server.camera_capture_runtime
    assert server.init_camera_capture(
        output_dir=tmp_path / "clips",
        gpio_pin=17,
        width=640,
        height=400,
        fps=300.0,
        pre_ms=150.0,
        post_ms=100.0,
        exposure_us=1250,
        gain=16.0,
        stream="raw",
        rotate_180=False,
        mirror_horizontal=False,
        roll_correction_deg=0.0,
        scaler_crop=None,
        mount_height_m=GEOMETRY.camera_height_m,
        lateral_offset_m=0.0,
        horizontal_offset_deg=0.0,
        use_gpio_trigger=False,
    )
    _join_restarts()
    assert server.camera_cv_worker is worker
    # The shadow job reads the synthetic mount of the scene, not the fake camera.
    monkeypatch.setattr(server, "camera_capture_config", config)
    monkeypatch.setattr(server, "camera_capture_runtime", runtime)


def test_the_worker_runs_after_capture_start_and_the_dark_shadow_is_not_skipped(
    scene, tmp_path, monkeypatch, entries
):  # pylint: disable=unused-argument
    worker = _LazyWorker(_result())
    _init_camera(monkeypatch, tmp_path, worker)

    assert worker.starts == 1
    assert worker.pid == 4343
    capture = _floodlit_clip(tmp_path)
    shot = _shot()
    server._fuse_camera_measurements(shot, capture)
    assert capture.metadata["auto_exposure"]["ball_analysis_eligible"] is False
    job = server._lowlight_shadow_job(shot, capture)
    server._run_camera_lowlight_shadow(job, enrichment_on_time=True)

    result = entries[0]["result"]
    assert result["reason"] != "skipped_worker_not_running"
    assert (result["status"], result["reason"]) == ("done", "ran")
    assert [call[0] for call in worker.calls] == ["lowlight_ball_flight"]
    assert worker.starts == 1  # the shot paid no start


def test_the_worker_start_is_logged(scene, tmp_path, monkeypatch, caplog):
    # pylint: disable=unused-argument
    with caplog.at_level("INFO", logger=server.logger.name):
        _init_camera(monkeypatch, tmp_path, _LazyWorker(_result()))

    assert "Worker started (pid 4343)" in caplog.text
    assert "for the low-light shadow" in caplog.text


def test_without_the_shadow_the_worker_still_starts_lazily(scene, tmp_path, monkeypatch):
    # pylint: disable=unused-argument
    worker = _LazyWorker(_result())
    _init_camera(monkeypatch, tmp_path, worker, shadow=False)

    assert worker.starts == 0
    assert worker.pid is None


def test_a_failed_startup_start_never_breaks_the_camera(scene, tmp_path, monkeypatch, caplog):
    # pylint: disable=unused-argument
    worker = _LazyWorker(_result(), start_error=CameraCvWorkerError("spawn failed"))
    with caplog.at_level("WARNING", logger=server.logger.name):
        _init_camera(monkeypatch, tmp_path, worker)

    # _init_camera asserted that init_camera_capture still returned True.
    assert server.camera_cv_worker is worker
    assert "Worker start for the low-light shadow failed" in caplog.text


def test_a_crashed_worker_is_restarted_for_the_next_shot_not_inside_this_one(
    scene, tmp_path, monkeypatch, entries
):  # pylint: disable=unused-argument
    worker = _LazyWorker(_result())
    _init_camera(monkeypatch, tmp_path, worker)
    worker.crash()
    capture = _floodlit_clip(tmp_path)

    server._run_camera_lowlight_shadow(
        server._lowlight_shadow_job(_shot(7), capture), enrichment_on_time=True
    )
    _join_restarts()

    assert entries[0]["result"]["reason"] == "skipped_worker_not_running"
    assert worker.calls == []
    assert (worker.starts, worker.pid) == (2, 4343)
    server._run_camera_lowlight_shadow(
        server._lowlight_shadow_job(_shot(8), capture), enrichment_on_time=True
    )
    assert (entries[1]["result"]["status"], entries[1]["result"]["reason"]) == ("done", "ran")


def test_a_stopped_worker_is_not_restarted_while_a_shot_waits(
    scene, tmp_path, monkeypatch, entries, enrichment_queue
):  # pylint: disable=unused-argument
    worker = _LazyWorker(_result())
    _init_camera(monkeypatch, tmp_path, worker)
    worker.crash()
    enrichment_queue.put_nowait(_waiting_shot())

    server._run_camera_lowlight_shadow(
        server._lowlight_shadow_job(_shot(), _floodlit_clip(tmp_path)), enrichment_on_time=True
    )
    _join_restarts()

    assert entries[0]["result"]["reason"] == "skipped_next_shot_queued"
    assert worker.starts == 1
