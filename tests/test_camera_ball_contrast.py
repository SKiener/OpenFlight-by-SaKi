"""Pixel-level regressions for backlit ball silhouettes and lighting changes."""

import cv2
import numpy as np
import pytest
from test_camera_ball_flight import _synthetic_path

from openflight.camera.ball_flight import _candidates, _path_estimate, estimate_camera_ball_flight
from openflight.camera.club_motion import ReferenceBall

ANCHOR = ReferenceBall(x=320, y=270, diameter_px=16, area_px=200)
# The sound trigger follows impact by the 1.524 m acoustic path plus gate latency.
SOUND_TRIGGER_NS = 4_650_000
PARAMS = {"bright_threshold": 115, "difference_threshold": 18, "min_area": 5}


@pytest.mark.parametrize("background_value,ball_value", [(210, 35), (35, 210)])
def test_candidates_keep_real_intensity_and_local_contrast(background_value, ball_value):
    background = np.full((400, 640), background_value, np.uint8)
    frame = background.copy()
    cv2.circle(frame, (324, 235), 6, ball_value, -1)
    found = _candidates(frame, background, ANCHOR, **PARAMS)
    assert len(found) == 1
    assert (found[0].x, found[0].y) == pytest.approx((324, 235), abs=0.1)
    assert found[0].mean_intensity == pytest.approx(ball_value)
    assert found[0].local_contrast == pytest.approx(175.0)


@pytest.mark.parametrize("background_value,ball_value", [(210, 35), (35, 210)])
def test_reconstructed_ball_path_is_independent_of_contrast_polarity(background_value, ball_value):
    geometry, anchor, model, truth, timestamps, evidence, speed_ms = _synthetic_path()
    background = np.full((400, 640), background_value, np.uint8)
    candidates = []
    for point in truth:
        frame = background.copy()
        cv2.circle(frame, (round(point.x), round(point.y)), round(point.width / 2), ball_value, -1)
        found = _candidates(frame, background, anchor, **PARAMS)
        assert len(found) == 1
        candidates.append(found[0])
    result = _path_estimate(
        path=list(enumerate(candidates)),
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(115, 18, 5),
    )
    assert result is not None
    assert result[1].horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert result[1].vertical_deg == pytest.approx(20.0, abs=0.5)


@pytest.mark.parametrize("kind", ["static", "dimming", "brightening", "noise", "shaft"])
def test_no_ball_in_negative_controls(kind):
    background = np.full((400, 640), 150, np.uint8)
    frame = background.copy()
    if kind == "dimming":
        frame[:] = 60
    elif kind == "brightening":
        frame[:] = 240
    elif kind == "noise":
        frame = (
            frame.astype(int) + np.random.default_rng(123).integers(-5, 6, frame.shape)
        ).astype(np.uint8)
    elif kind == "shaft":
        cv2.rectangle(frame, (310, 200), (312, 245), 30, -1)
    assert _candidates(frame, background, ANCHOR, **PARAMS) == []


def test_blob_touching_the_resting_ball_is_not_a_ball_candidate():
    background = np.full((400, 640), 35, np.uint8)
    cv2.circle(background, (320, 270), 8, 210, -1)
    frame = background.copy()
    cv2.circle(frame, (334, 270), 6, 210, -1)
    cv2.circle(frame, (324, 235), 6, 210, -1)

    found = _candidates(frame, background, ANCHOR, **PARAMS)

    assert [(candidate.x, candidate.y) for candidate in found] == [pytest.approx((324, 235))]


def test_uniform_light_change_does_not_animate_stationary_ball_shapes():
    background = np.full((400, 640), 160, np.uint8)
    cv2.circle(background, (324, 235), 6, 40, -1)
    frame = (background.astype(np.int16) + 50).astype(np.uint8)
    assert _candidates(frame, background, ANCHOR, **PARAMS) == []


def test_temporal_change_without_local_contrast_is_not_a_ball():
    background = np.full((400, 640), 150, np.uint8)
    cv2.circle(background, (324, 235), 6, 35, -1)
    frame = np.full_like(background, 150)
    assert _candidates(frame, background, ANCHOR, **PARAMS) == []


def test_ball_path_rejects_low_contrast_even_with_plausible_shape_and_speed():
    from dataclasses import replace

    geometry, _anchor, model, candidates, timestamps, evidence, speed_ms = _synthetic_path()
    result = _path_estimate(
        path=[(i, replace(c, local_contrast=5.0)) for i, c in enumerate(candidates)],
        frame_indices=list(range(len(candidates))),
        timestamps_ns=timestamps,
        time_zero_s=0.0,
        range_evidence=evidence,
        ops_ball_speed_mph=speed_ms * 2.23694,
        iwr_vertical_deg=20.0,
        model=model,
        geometry=geometry,
        thresholds=(115, 18, 5),
    )
    assert result is None


def test_flight_rejects_reference_without_visible_local_contrast(monkeypatch):
    import openflight.camera.ball_flight as module

    geometry, anchor, _model, _candidates, _timestamps, _evidence, _speed = _synthetic_path()
    monkeypatch.setattr(module, "detect_reference_ball", lambda _frames: anchor)
    frames = np.full((30, 400, 640), 255, np.uint8)
    result = estimate_camera_ball_flight(
        frames,
        np.arange(30, dtype=np.int64) * 3_500_000,
        trigger_ns=70_000_000,
        range_evidence=None,
        geometry=geometry,
        ops_ball_speed_mph=100.0,
    )
    assert result.status == "rejected_reference_ball_contrast"


def _render_flight(background_value, ball_value, *, stationary_reference=False):
    geometry, anchor, _model, points, times, evidence, speed = _synthetic_path()
    pre = np.full((20, 400, 640), background_value, np.uint8)
    for frame in pre:
        cv2.circle(
            frame, (round(anchor.x), round(anchor.y)), round(anchor.diameter_px / 2), ball_value, -1
        )
    post = np.full((len(points), 400, 640), background_value, np.uint8)
    for frame, point in zip(post, points):
        if stationary_reference:
            cv2.circle(
                frame,
                (round(anchor.x), round(anchor.y)),
                round(anchor.diameter_px / 2),
                ball_value,
                -1,
            )
        cv2.circle(frame, (round(point.x), round(point.y)), round(point.width / 2), ball_value, -1)
    timestamps = np.concatenate((np.arange(-20, 0, dtype=np.int64) * 3_500_000, times))
    return np.concatenate((pre, post)), timestamps, geometry, anchor, evidence, speed


@pytest.mark.parametrize("background,ball", [(210, 35), (35, 210)])
def test_full_detector_reconstructs_backlit_and_bright_flight(monkeypatch, background, ball):
    import openflight.camera.ball_flight as module

    frames, times, geometry, anchor, evidence, speed = _render_flight(background, ball)
    monkeypatch.setattr(module, "detect_reference_ball", lambda _frames: anchor)
    result = estimate_camera_ball_flight(
        frames,
        times,
        trigger_ns=SOUND_TRIGGER_NS,
        range_evidence=evidence,
        geometry=geometry,
        ops_ball_speed_mph=speed * 2.23694,
    )
    assert result.confidence_tier != "withheld"
    assert result.horizontal_deg == pytest.approx(4.0, abs=0.5)
    assert result.vertical_deg == pytest.approx(20.0, abs=0.5)


def test_moving_decoy_is_rejected_when_reference_ball_never_leaves(monkeypatch):
    import openflight.camera.ball_flight as module

    frames, times, geometry, anchor, evidence, speed = _render_flight(
        210, 35, stationary_reference=True
    )
    monkeypatch.setattr(module, "detect_reference_ball", lambda _frames: anchor)
    result = estimate_camera_ball_flight(
        frames,
        times,
        trigger_ns=0,
        range_evidence=evidence,
        geometry=geometry,
        ops_ball_speed_mph=speed * 2.23694,
    )
    assert result.confidence_tier == "withheld"
    assert result.status == "rejected_no_ball_departure"
