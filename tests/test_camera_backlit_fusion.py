"""Backlight may allow ball analysis, not blanket approval of club metrics."""

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from test_camera_triggered_buffer import make_frame

from openflight import server
from openflight.camera.auto_exposure import nearly_black
from openflight.camera.capture_runtime import capture_exposure_status
from openflight.camera.triggered_buffer import TriggeredCapture
from openflight.launch_monitor import Shot


@pytest.mark.parametrize(
    "value,exposure,gain,ball_eligible",
    [
        (255, 100, 2.0, True),
        (255, 1000, 2.0, False),
        (255, 0, 2.0, False),
        (255, 100, 0.0, False),
        (255, 100, float("nan"), False),
        (0, 100, 2.0, False),
    ],
)
def test_backlit_permission_never_bypasses_shutter_metadata_or_darkness(
    value, exposure, gain, ball_eligible
):
    image = np.full((200, 320), value, np.uint8)
    # One pixel off the measured zones: a real frame is never one grey level (flat).
    image[0, 0] = 0 if value > 127 else 255
    frames = tuple(
        replace(
            make_frame(i),
            image=image,
            exposure_us=exposure,
            analogue_gain=gain,
        )
        for i in range(6)
    )
    result = capture_exposure_status(
        TriggeredCapture(
            frames=frames,
            pre_trigger_count=4,
            trigger_host_timestamp_ns=12_000_000,
        )
    )
    assert result["analysis_eligible"] is False
    assert result["ball_analysis_eligible"] is ball_eligible


@pytest.mark.parametrize("ball_eligible", [False, True])
def test_backlit_fusion_only_runs_ball_estimator(monkeypatch, ball_eligible):
    calls = []
    archive = {"frames": np.zeros((6, 200, 320), np.uint8)}
    monkeypatch.setattr(server, "_load_camera_capture_archive", lambda _: archive)
    monkeypatch.setattr(
        server, "_fuse_camera_ball_flight", lambda _s, _c, a, **_kw: calls.append(a)
    )
    monkeypatch.setattr(
        server,
        "_fuse_camera_club_delivery",
        lambda *_a, **_kw: pytest.fail("backlighting must not unlock club delivery"),
    )
    shot = Shot(
        ball_speed_mph=100.0,
        timestamp=datetime.now(),
        launch_angle_horizontal=2.0,
        launch_angle_horizontal_source="radar",
    )
    capture = SimpleNamespace(
        valid=True,
        metadata={
            "auto_exposure": {
                "analysis_eligible": False,
                "ball_analysis_eligible": ball_eligible,
            }
        },
    )
    server._fuse_camera_measurements(shot, capture)
    assert calls == ([archive] if ball_eligible else [])
    assert shot.experimental_fused_status == "rejected_lighting_quality"
    assert shot.launch_angle_horizontal == 2.0


@pytest.mark.parametrize(
    "auto_exposure,queued",
    [
        ({"analysis_eligible": True}, True),
        ({"analysis_eligible": False, "ball_analysis_eligible": True}, True),
        ({"analysis_eligible": False, "ball_analysis_eligible": False}, False),
    ],
    ids=["front_lit", "backlit_ball_only", "too_dark"],
)
def test_every_ball_eligible_clip_queues_the_shadow_club_head_path(
    monkeypatch, auto_exposure, queued
):
    job = object()
    monkeypatch.setattr(server, "_load_camera_capture_archive", lambda _: {})
    monkeypatch.setattr(server, "_fuse_camera_ball_flight", lambda *_args, **_kw: job)
    monkeypatch.setattr(server, "_fuse_camera_club_delivery", lambda *_args, **_kw: None)
    shot = Shot(ball_speed_mph=100.0, timestamp=datetime.now())
    capture = SimpleNamespace(valid=True, metadata={"auto_exposure": auto_exposure})

    assert server._fuse_camera_measurements(shot, capture) is (job if queued else None)


# --- the relaxed dark rule of the ball path (2026-11-02) -----------------------------------


def _impact_area_frame(levels, *, height=400, width=640):
    """A frame whose impact area (rows 45-90 %, columns 20-80 %) holds ``levels`` by share.

    ``levels`` is ((grey, share), ...); the rest of the frame is mid grey.
    """
    image = np.full((height, width), 60, np.uint8)
    rows = slice(round(height * 0.45), round(height * 0.9))
    cols = slice(round(width * 0.2), round(width * 0.8))
    area = image[rows, cols]
    flat = np.concatenate(
        [np.full(round(share * area.size), grey, np.uint8) for grey, share in levels]
    )
    flat = np.resize(flat, area.size)
    image[rows, cols] = flat.reshape(area.shape)
    return image


# 2026-11-02 #3/#4 at the daylight lock 350 us / gain 6: p10 7, median 27-28, p90 73-74.
EDGE_DARK = ((7, 0.4), (30, 0.4), (74, 0.2))
NEAR_BLACK = ((4, 0.95), (10, 0.05))
GOOD = ((40, 0.3), (90, 0.4), (140, 0.3))


def _status(*images, exposure=350, gain=6.0):
    frames = tuple(
        replace(make_frame(i), image=image, exposure_us=exposure, analogue_gain=gain)
        for i, image in enumerate(images)
    )
    return capture_exposure_status(
        TriggeredCapture(frames=frames, pre_trigger_count=3, trigger_host_timestamp_ns=12_000_000)
    )


def test_an_impact_area_at_p90_74_is_eligible_for_the_ball_path_but_not_the_club_path():
    result = _status(*[_impact_area_frame(EDGE_DARK)] * 6)

    assert result["observation"]["status"] == "too_dark"
    assert result["observation"]["p90"] == pytest.approx(74.0)
    assert result["analysis_eligible"] is False
    assert result["ball_analysis_eligible"] is True
    assert result["ball_analysis_relaxed"] is True


def test_a_nearly_black_impact_area_is_eligible_for_neither_path():
    result = _status(*[_impact_area_frame(NEAR_BLACK)] * 6)

    assert result["observation"]["status"] == "too_dark"
    assert result["analysis_eligible"] is False
    assert result["ball_analysis_eligible"] is False
    assert result["ball_analysis_relaxed"] is False


def test_one_nearly_black_sampled_frame_blocks_the_relaxed_rule():
    images = [_impact_area_frame(EDGE_DARK)] * 6
    images[3] = _impact_area_frame(NEAR_BLACK)

    result = _status(*images)

    assert result["ball_analysis_eligible"] is False
    assert result["ball_analysis_relaxed"] is False


# 2026-11-02, 320x200 (crop-500/crop-600): every pixel 16, at the daylight lock.
FLAT_FRAME_P90 = 16.0
FLAT_FRAME = np.full((400, 640), int(FLAT_FRAME_P90), np.uint8)


def test_a_flat_clip_is_no_image_and_eligible_for_neither_path():
    """Grey 16 is too dark but not nearly black: only the flat check keeps it out."""
    assert not nearly_black(FLAT_FRAME_P90, 0.0)

    result = _status(*[FLAT_FRAME] * 6)

    assert result["observation"]["status"] == "flat"
    assert result["observation"]["p90"] == pytest.approx(FLAT_FRAME_P90)
    assert result["motion_blur_risk"] == "low"
    assert (result["status"], result["frame_flat"]) == ("no_image", True)
    assert result["analysis_eligible"] is False
    assert result["ball_analysis_eligible"] is False
    assert result["ball_analysis_relaxed"] is False


def test_one_flat_sampled_frame_blocks_the_relaxed_rule():
    images = [_impact_area_frame(EDGE_DARK)] * 6
    images[3] = FLAT_FRAME

    result = _status(*images)

    assert result["frame_flat"] is True
    assert result["ball_analysis_eligible"] is False
    assert result["ball_analysis_relaxed"] is False


def test_a_dark_clip_without_a_flat_frame_reports_no_flat_frame():
    result = _status(*[_impact_area_frame(EDGE_DARK)] * 6)

    assert result["frame_flat"] is False
    assert result["ball_analysis_relaxed"] is True


def test_the_relaxed_rule_keeps_the_motion_blur_gate():
    result = _status(*[_impact_area_frame(EDGE_DARK)] * 6, exposure=1250, gain=16.0)

    assert result["motion_blur_risk"] == "high"
    assert result["ball_analysis_eligible"] is False
    assert result["ball_analysis_relaxed"] is False


def test_a_well_lit_clip_is_not_relaxed():
    result = _status(*[_impact_area_frame(GOOD)] * 6)

    assert result["observation"]["status"] in {"good", "marginal"}
    assert result["analysis_eligible"] is True
    assert result["ball_analysis_eligible"] is True
    assert result["ball_analysis_relaxed"] is False


RELAXED = {
    "analysis_eligible": False,
    "ball_analysis_eligible": True,
    "ball_analysis_relaxed": True,
}


def test_a_relaxed_clip_runs_only_the_ball_flight_without_the_legacy_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_load_camera_capture_archive", lambda _: {})
    monkeypatch.setattr(
        server,
        "_fuse_camera_ball_flight",
        lambda _s, _c, _a, **kwargs: calls.append(kwargs) or object(),
    )
    monkeypatch.setattr(
        server,
        "_fuse_camera_club_delivery",
        lambda *_a, **_kw: pytest.fail("the relaxed ball gate must not unlock club delivery"),
    )
    shot = Shot(ball_speed_mph=100.0, timestamp=datetime.now())
    capture = SimpleNamespace(valid=True, metadata={"auto_exposure": dict(RELAXED)})

    job = server._fuse_camera_measurements(shot, capture)

    assert calls and calls[0]["relaxed_only"] is True
    # The club-data job keeps the gate it always had: no job for a merely dark clip.
    assert job is None
    assert shot.experimental_fused_status == "rejected_lighting_quality"


@pytest.mark.parametrize(
    ("auto_exposure", "relaxed"),
    [
        (RELAXED, True),
        ({"analysis_eligible": False, "ball_analysis_eligible": True}, False),
        ({"analysis_eligible": True, "ball_analysis_eligible": True}, False),
        ({"analysis_eligible": True, "ball_analysis_relaxed": True}, False),
        (None, False),
    ],
    ids=["relaxed", "backlit", "front_lit", "club_eligible_wins", "no_metadata"],
)
def test_only_a_clip_the_relaxed_rule_alone_admits_counts_as_relaxed(auto_exposure, relaxed):
    capture = SimpleNamespace(valid=True, metadata={"auto_exposure": auto_exposure})

    assert server._camera_ball_relaxed_only(capture) is relaxed


def test_the_relaxed_ball_flight_runs_the_disappeared_locator_without_fallback(
    monkeypatch, tmp_path
):
    seen = {}

    def fake_run(job, kwargs, **_kw):
        seen.update(kwargs, job=job)
        from openflight.camera.ball_flight import CameraBallEstimate  # noqa: PLC0415

        return CameraBallEstimate(status="rejected_no_departed_ball")

    monkeypatch.setattr(server, "_run_shot_camera_cv", fake_run)
    calibration = SimpleNamespace(tee_range_m=1.5, radar_height_m=0.15, tee_ball_height_m=0.02)
    monkeypatch.setattr(server, "iwr6843_runtime", SimpleNamespace(calibration=calibration))
    monkeypatch.setattr(
        server,
        "camera_capture_config",
        {"mount_height_m": 0.21, "width": 640, "height": 400, "ball_locator": "legacy"},
    )
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    np.savez(
        tmp_path / "frames.npz",
        frames=np.zeros((8, 4, 4), dtype=np.uint8),
        host_timestamp_ns=np.arange(8, dtype=np.int64),
        trigger_host_timestamp_ns=np.int64(3),
    )
    shot = Shot(ball_speed_mph=86.0, timestamp=datetime.now())
    capture = SimpleNamespace(valid=True, path=tmp_path)

    server._fuse_camera_ball_flight(shot, capture, relaxed_only=True)
    relaxed = dict(seen)
    server._fuse_camera_ball_flight(shot, capture)

    assert relaxed["job"] == "ball_flight"
    assert (relaxed["ball_locator"], relaxed["legacy_fallback"]) == ("disappeared", False)
    assert (seen["ball_locator"], seen["legacy_fallback"]) == ("legacy", True)
