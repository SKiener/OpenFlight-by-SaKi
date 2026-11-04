"""Camera time base: sensor timestamps and the camera time zero for IWR range lookup."""

import json
import math
from datetime import datetime
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from test_camera_ball_flight import _project_world_point, _synthetic_path

import openflight.camera.ball_flight as ball_flight_module
from openflight import server
from openflight.camera.ball_flight import (
    DEPARTURE_FIT_MAX_RMS_PX,
    DEPARTURE_FIT_MAX_SPAN_FRAMES,
    DEPARTURE_MAX_LAG_S,
    DEPARTURE_MAX_LEAD_S,
    FLIGHT_WINDOW_FRAMES,
    SPEED_OF_SOUND_M_S,
    BallCandidate,
    CameraBallEstimate,
    CameraTimeZero,
    DepartureFit,
    _fit_departure,
    _post_impact_frames,
    _select_time_zero,
    estimate_camera_ball_flight,
)
from openflight.launch_monitor import Shot
from openflight.session_logger import SessionLogger

PERIOD_NS = 3_474_000
HALF_FRAME_MS = PERIOD_NS / 2e6
FIRST_FLIGHT_FRAME = 30


def _shot_motion(geometry, speed_ms):
    tee = np.array([0.0, geometry.ball_forward_m, geometry.ball_height_m])
    horizontal, vertical = math.radians(4.0), math.radians(20.0)
    velocity = speed_ms * np.array(
        [
            math.cos(vertical) * math.sin(horizontal),
            math.cos(vertical) * math.cos(horizontal),
            math.sin(vertical),
        ]
    )
    return tee, velocity


def _flight_frames(occluded_frames: int = 0, flight_phase: float = 0.5):
    """Impact at sensor time 0, ``flight_phase`` frame periods before the first flight frame."""
    geometry, anchor, _model, _points, _times, evidence, speed_ms = _synthetic_path()
    tee, velocity = _shot_motion(geometry, speed_ms)
    first_exposure_ns = round(flight_phase * PERIOD_NS)
    sensor_ns = (np.arange(-FIRST_FLIGHT_FRAME, 12) * PERIOD_NS + first_exposure_ns).astype(
        np.int64
    )
    camera_origin = geometry.camera_origin
    frames = np.full((len(sensor_ns), 400, 640), 35, np.uint8)
    for frame, time_ns in zip(frames, sensor_ns, strict=True):
        point = tee + velocity * max(time_ns, 0) / 1e9
        x_px, y_px = _project_world_point(
            point,
            camera_height_m=geometry.camera_height_m,
            focal_px=480.0,
            pitch_rad=0.0,
            width=640,
            height=400,
        )
        diameter = 480.0 * geometry.ball_diameter_m / np.linalg.norm(point - camera_origin)
        cv2.circle(frame, (round(x_px), round(y_px)), round(diameter / 2), 210, -1)
        if 0 < time_ns < occluded_frames * PERIOD_NS:
            # A club head as bright as the ball keeps the tee patch "present".
            cv2.circle(
                frame, (round(anchor.x), round(anchor.y)), round(anchor.diameter_px), 210, -1
            )
    radar = np.array([0.0, 0.0, geometry.radar_height_m])
    evidence.track.range_at = lambda time_s, _res: float(
        np.linalg.norm(tee + velocity * max(time_s, 0.0) - radar)
    )
    return frames, sensor_ns, geometry, anchor, evidence, speed_ms


def _estimate(
    monkeypatch,
    trigger_latency_ms,
    *,
    occluded_frames=0,
    with_iwr=True,
    n_frames=None,
    ball_at_rest=False,
    flight_phase=0.5,
    club_beside_ball=False,
):
    frames, sensor_ns, geometry, anchor, evidence, speed_ms = _flight_frames(
        occluded_frames, flight_phase
    )
    if ball_at_rest:
        frames = np.repeat(frames[:1], len(frames), axis=0)
    if club_beside_ball:
        club = (round(anchor.x + 0.7 * anchor.diameter_px), round(anchor.y))
        cv2.circle(frames[FIRST_FLIGHT_FRAME - 1], club, 5, 210, -1)
    monkeypatch.setattr(ball_flight_module, "detect_reference_ball", lambda _frames: anchor)
    result = estimate_camera_ball_flight(
        frames[:n_frames],
        sensor_ns[:n_frames],
        trigger_ns=round(trigger_latency_ms * 1e6),
        range_evidence=evidence if with_iwr else None,
        geometry=geometry,
        ops_ball_speed_mph=speed_ms * 2.23694,
    )
    return result, geometry, speed_ms


# The sound trigger follows impact by the 1.524 m acoustic path (4.44 ms) plus latency.
@pytest.mark.parametrize("trigger_latency_ms", [4.65, 6.5, 8.0])
def test_range_lookup_is_anchored_at_the_fitted_departure_not_the_trigger(
    monkeypatch, trigger_latency_ms
):
    result, _geometry, speed_ms = _estimate(monkeypatch, trigger_latency_ms)

    assert result.depth_source == "iwr_range"
    assert result.time_zero.source == "departure_fit"
    assert result.time_zero.time_zero_ms == pytest.approx(-trigger_latency_ms, abs=HALF_FRAME_MS)
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.3)
    assert result.vertical_deg == pytest.approx(20.0, abs=0.3)
    assert result.speed_mph == pytest.approx(speed_ms * 2.23694, rel=0.03)


def test_late_tee_patch_departure_is_recovered_by_the_departure_fit(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 4.65, occluded_frames=2)

    assert result.time_zero.tee_patch_ms > 0.0
    assert result.time_zero.source == "departure_fit"
    assert result.time_zero.time_zero_ms == pytest.approx(-4.65, abs=HALF_FRAME_MS)
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.3)
    assert result.vertical_deg == pytest.approx(20.0, abs=0.3)


def test_departure_after_its_own_sound_falls_back_to_the_acoustic_estimate(monkeypatch):
    result, geometry, _speed_ms = _estimate(monkeypatch, 0.0)

    acoustic_ms = -1e3 * geometry.tee_range_m / SPEED_OF_SOUND_M_S
    assert result.time_zero.departure_fit_ms == pytest.approx(0.0, abs=HALF_FRAME_MS)
    assert result.time_zero.source == "acoustic"
    assert result.time_zero.time_zero_ms == pytest.approx(acoustic_ms)
    assert result.time_zero.acoustic_ms == pytest.approx(acoustic_ms)


def test_time_zero_diagnostics_report_every_candidate(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 4.65)

    time_zero = result.time_zero
    assert time_zero.time_zero_ms == time_zero.departure_fit_ms
    assert time_zero.tee_patch_ms == pytest.approx(-4.65)
    assert time_zero.iwr_impact_t_s == 0.0
    assert time_zero.departure_fit_points == 6
    assert 0.0 <= time_zero.departure_fit_rms_px <= DEPARTURE_FIT_MAX_RMS_PX
    assert 0.0 < time_zero.departure_fit_span_frames <= DEPARTURE_FIT_MAX_SPAN_FRAMES
    assert 2 * time_zero.departure_fit_support >= time_zero.selected_paths > 0


def test_camera_only_estimate_still_reports_its_time_zero(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 4.65, with_iwr=False)

    assert result.depth_source == "camera_size"
    assert result.time_zero.source == "departure_fit"
    assert result.time_zero.iwr_impact_t_s is None


def test_first_flight_frame_between_acoustic_estimate_and_trigger_is_tracked(monkeypatch):
    """First flight exposure 3.13 ms after impact, acoustic estimate 2.06 ms, trigger 6.5 ms."""
    result, _geometry, _speed_ms = _estimate(monkeypatch, 6.5, flight_phase=0.9)

    assert result.confidence_tier != "withheld"
    assert result.first_frame == FIRST_FLIGHT_FRAME
    assert result.time_zero.time_zero_ms == pytest.approx(-6.5, abs=HALF_FRAME_MS)
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.3)
    assert result.vertical_deg == pytest.approx(20.0, abs=0.3)


def test_flight_frame_before_the_acoustic_estimate_is_not_tracked(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 8.0, flight_phase=0.9)

    assert result.confidence_tier != "withheld"
    assert result.first_frame == FIRST_FLIGHT_FRAME + 1


def test_club_beside_the_resting_ball_never_starts_the_track(monkeypatch):
    """A trigger at impact opens the window on the last frame before it."""
    result, _geometry, _speed_ms = _estimate(monkeypatch, 0.0, club_beside_ball=True)

    assert result.first_frame >= FIRST_FLIGHT_FRAME


def test_static_ball_clip_is_still_rejected(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 4.65, ball_at_rest=True)

    assert result.status == "rejected_no_ball_departure"
    assert result.confidence_tier == "withheld"


def test_clip_ending_three_frames_after_the_acoustic_estimate_is_rejected(monkeypatch):
    result, _geometry, _speed_ms = _estimate(monkeypatch, 4.65, n_frames=FIRST_FLIGHT_FRAME + 3)

    assert result.status == "rejected_insufficient_post_trigger_frames"


CLIP_NS = np.arange(20, dtype=np.int64) * PERIOD_NS + 225_000_000_000


def _exposure_s(frame):
    return int(CLIP_NS[frame]) / 1e9


@pytest.mark.parametrize(
    ("acoustic_s", "first", "stop"),
    [
        (_exposure_s(0) - 1.0, 0, FLIGHT_WINDOW_FRAMES),
        (_exposure_s(3), 3, 3 + FLIGHT_WINDOW_FRAMES),
        (_exposure_s(3) + 1e-6, 4, 4 + FLIGHT_WINDOW_FRAMES),
        (_exposure_s(17), 17, 20),
        (_exposure_s(19) + 1e-6, 20, 20),
    ],
    ids=["before_clip", "at_an_exposure", "between_exposures", "near_clip_end", "after_clip"],
)
def test_post_impact_frames_stay_inside_the_clip(acoustic_s, first, stop):
    assert _post_impact_frames(CLIP_NS, acoustic_s) == list(range(first, stop))


def _track(departure_s: float, occluded_frames: int, noise_px: float = 0.2):
    """Image track of the synthetic shot; the first frames after departure are hidden."""
    geometry, anchor, model, _points, _times, _evidence, speed_ms = _synthetic_path()
    tee, velocity = _shot_motion(geometry, speed_ms)
    noise = np.random.default_rng(7).normal(0.0, noise_px, (16, 2))
    timestamps_ns = np.arange(16, dtype=np.int64) * PERIOD_NS
    path = []
    for frame, time_ns in enumerate(timestamps_ns):
        flight_s = time_ns / 1e9 - departure_s
        if flight_s <= occluded_frames * PERIOD_NS / 1e9:
            continue
        point = tee + velocity * flight_s
        x_px, y_px = _project_world_point(
            point,
            camera_height_m=geometry.camera_height_m,
            focal_px=480.0,
            pitch_rad=0.0,
            width=640,
            height=400,
        )
        candidate = BallCandidate(
            x=x_px + noise[frame, 0],
            y=y_px + noise[frame, 1],
            area=100,
            width=10,
            height=10,
            fill=0.8,
            circularity=0.9,
            mean_intensity=210.0,
        )
        path.append((frame, candidate))
    return path, timestamps_ns, anchor, model, geometry


def _fit(path, timestamps_ns, anchor, model, geometry):
    return _fit_departure(path, timestamps_ns, anchor=anchor, model=model, geometry=geometry)


@pytest.mark.parametrize("departure_ms", [0.0, 1.3, 2.9])
@pytest.mark.parametrize("occluded_frames", [0, 1, 2, 3])
def test_departure_fit_recovers_a_departure_hidden_behind_the_club(occluded_frames, departure_ms):
    fit = _fit(*_track(departure_ms / 1e3, occluded_frames))

    assert fit is not None
    assert fit.time_s == pytest.approx(departure_ms / 1e3, abs=HALF_FRAME_MS / 1e3)
    assert fit.n_points == 6
    assert fit.rms_px <= DEPARTURE_FIT_MAX_RMS_PX
    assert occluded_frames < fit.span_frames <= DEPARTURE_FIT_MAX_SPAN_FRAMES


def test_departure_fit_rejects_too_few_separated_points():
    path, timestamps_ns, anchor, model, geometry = _track(0.0, 0)

    assert _fit(path[-3:], timestamps_ns, anchor, model, geometry) is None


def test_departure_fit_rejects_a_track_that_is_not_one_receding_ball():
    path, timestamps_ns, anchor, model, geometry = _track(0.0, 0, noise_px=0.0)
    frames = [frame for frame, _candidate in path]
    candidates = [candidate for _frame, candidate in path]
    swapped = list(zip(frames, [candidates[0], candidates[2], candidates[1], *candidates[3:]]))
    stalled = list(zip(frames, [*candidates[:3], candidates[2], *candidates[4:]]))

    assert _fit(swapped, timestamps_ns, anchor, model, geometry) is None
    assert _fit(stalled, timestamps_ns, anchor, model, geometry) is None


TRIGGER_S = 225.0
ACOUSTIC_S = TRIGGER_S - 1.5 / SPEED_OF_SOUND_M_S


def _departure(time_s):
    return DepartureFit(time_s=time_s, n_points=6, rms_px=0.1, span_frames=2.5)


@pytest.mark.parametrize(
    "offset_s", [-0.99 * DEPARTURE_MAX_LEAD_S, -1e-3, 0.0, 0.99 * DEPARTURE_MAX_LAG_S]
)
def test_departure_fit_inside_the_acoustic_window_is_time_zero(offset_s):
    fit = _departure(ACOUSTIC_S + offset_s)

    assert _select_time_zero(fit, trigger_s=TRIGGER_S, acoustic_s=ACOUSTIC_S) == (
        ACOUSTIC_S + offset_s,
        "departure_fit",
    )


@pytest.mark.parametrize(
    "fit",
    [
        None,
        _departure(ACOUSTIC_S - 1.01 * DEPARTURE_MAX_LEAD_S),
        _departure(ACOUSTIC_S + 1.01 * DEPARTURE_MAX_LAG_S),
        _departure(TRIGGER_S + 1e-3),
    ],
    ids=["no_reliable_fit", "too_early", "after_its_sound", "after_trigger"],
)
def test_time_zero_falls_back_to_the_acoustic_estimate(fit):
    assert _select_time_zero(fit, trigger_s=TRIGGER_S, acoustic_s=ACOUSTIC_S) == (
        ACOUSTIC_S,
        "acoustic",
    )


def test_time_zero_never_follows_the_trigger():
    near_acoustic_s = TRIGGER_S - 0.3 / SPEED_OF_SOUND_M_S

    zero_s, source = _select_time_zero(
        _departure(TRIGGER_S + 1e-4), trigger_s=TRIGGER_S, acoustic_s=near_acoustic_s
    )

    assert (zero_s, source) == (near_acoustic_s, "acoustic")
    assert zero_s < TRIGGER_S


TIME_ZERO = CameraTimeZero(
    source="departure_fit",
    time_zero_ms=-5.0,
    departure_fit_ms=-5.0,
    acoustic_ms=-4.37,
    tee_patch_ms=2.1,
    iwr_impact_t_s=0.0121,
    departure_fit_points=6,
    departure_fit_rms_px=0.12,
    departure_fit_span_frames=2.6,
    departure_fit_support=27,
    selected_paths=27,
)


def _fuse(monkeypatch, tmp_path, *, ops_impact_offset_s):
    monkeypatch.setattr(
        ball_flight_module,
        "estimate_camera_ball_flight",
        lambda *_args, **_kwargs: CameraBallEstimate(
            status="accepted", confidence_tier="high", horizontal_deg=0.6, time_zero=TIME_ZERO
        ),
    )
    calibration = SimpleNamespace(tee_range_m=1.5, radar_height_m=0.15, tee_ball_height_m=0.02)
    monkeypatch.setattr(server, "iwr6843_runtime", SimpleNamespace(calibration=calibration))
    monkeypatch.setattr(
        server, "camera_capture_config", {"mount_height_m": 0.21, "width": 640, "height": 400}
    )
    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=np.arange(8, dtype=np.int64),
        trigger_host_timestamp_ns=np.int64(3),
    )
    shot = Shot(
        ball_speed_mph=86.0,
        timestamp=datetime.now(),
        impact_timestamp=1791030836.22971,
        impact_timestamp_kld7=1791030836.22971 + ops_impact_offset_s,
    )
    server._fuse_camera_ball_flight(shot, SimpleNamespace(valid=True, path=tmp_path))
    return shot


def test_live_fusion_logs_time_zero_with_the_ops_impact(monkeypatch, tmp_path):
    shot = _fuse(monkeypatch, tmp_path, ops_impact_offset_s=-0.00507)
    logger = SessionLogger(log_dir=tmp_path, enabled=True)
    logger.start_session()
    logger.log_shot(shot)
    entry = json.loads(logger.session_path.read_text().strip().split("\n")[-1])

    expected = {**vars(TIME_ZERO), "ops_impact_ms": pytest.approx(-5.07, abs=1e-3)}
    assert shot.experimental_camera_time_zero == expected
    assert entry["experimental_camera_time_zero"] == expected


def test_ops_impact_is_omitted_when_the_ops_fell_back_to_its_trigger(monkeypatch, tmp_path):
    shot = _fuse(monkeypatch, tmp_path, ops_impact_offset_s=0.0)

    assert shot.experimental_camera_time_zero == {**vars(TIME_ZERO), "ops_impact_ms": None}


def test_shot_without_camera_time_zero_serializes_none():
    shot = Shot(ball_speed_mph=86.0, timestamp=datetime.now())

    assert shot.to_dict()["experimental_camera_time_zero"] is None


def _archive(sensor_ns, host_ns):
    archive = {"host_timestamp_ns": np.asarray(host_ns, dtype=np.int64)}
    if sensor_ns is not None:
        archive["sensor_timestamp_ns"] = np.asarray(sensor_ns, dtype=np.int64)
    return archive


def test_frame_timestamps_prefer_sensor_exposure_times():
    sensor = np.arange(60, dtype=np.int64) * PERIOD_NS + 225_000_000_000
    jitter = np.random.default_rng(1).integers(-300_000, 300_000, 60)
    host = sensor + 4_650_000 + jitter

    assert np.array_equal(server._camera_frame_timestamps(_archive(sensor, host)), sensor)


@pytest.mark.parametrize(
    "sensor",
    [
        None,
        np.arange(59, dtype=np.int64) * PERIOD_NS,
        np.r_[np.arange(30), np.arange(30)].astype(np.int64) * PERIOD_NS,
        np.arange(60, dtype=np.int64) * PERIOD_NS + 225_000_000_000 - 3_000_000_000,
    ],
    ids=["missing", "length", "not_increasing", "other_clock"],
)
def test_frame_timestamps_fall_back_to_host_when_sensor_times_are_unusable(sensor):
    host = np.arange(60, dtype=np.int64) * PERIOD_NS + 225_000_000_000

    assert np.array_equal(server._camera_frame_timestamps(_archive(sensor, host)), host)
