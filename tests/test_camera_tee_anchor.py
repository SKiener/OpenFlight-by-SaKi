"""Session tee anchor of the camera trigger gate (task A: a, b, d, e)."""

import logging

import numpy as np
import pytest

from openflight.camera.capture_runtime import CameraCaptureRuntime, CameraCaptureSettings
from openflight.camera.tee_anchor import CONFIRMATIONS_TO_BLOCK, TeeAnchor
from openflight.camera.tee_zone import TeeZone
from openflight.camera.trigger_check import AnchorBall

TEE = (368.0, 267.0)
TREE = (313.8, 169.0)
DIAMETER = 16.5
# Signed core-minus-ring contrast of the backlit range ball (2026-10-25: -43..-112).
CONTRAST = -60.0


def departed(x=TEE[0], y=TEE[1], source="hint", diameter=DIAMETER, contrast=CONTRAST) -> dict:
    """Stored trigger check of a clip whose ball left (x, y)."""
    return {
        "decision": "departed",
        "reason": "ball_left_spot",
        "ball_x": x,
        "ball_y": y,
        "ball_diameter_px": diameter,
        "ball_source": source,
        "departure_x": x,
        "departure_y": y,
        "departure_diameter_px": diameter,
        "departure_source": source,
        "departure_contrast": contrast,
        "exposure_gain": 200.0,
    }


def still_there(x=TEE[0], y=TEE[1], source="hint", **departure) -> dict:
    check = {
        "decision": "still_there",
        "reason": "ball_unchanged",
        "ball_x": x,
        "ball_y": y,
        "ball_diameter_px": DIAMETER,
        "ball_source": source,
    }
    check.update(departure)
    return check


def confirmed_anchor(x=TEE[0], y=TEE[1]) -> TeeAnchor:
    anchor = TeeAnchor()
    for shot in range(CONFIRMATIONS_TO_BLOCK):
        anchor.observe_shot(departed(x + shot, y), shot=shot + 1)
    return anchor


def test_static_blobs_and_unclear_checks_never_establish_the_anchor():
    """(a) The tree crown, still there on every check, is never an anchor."""
    anchor = TeeAnchor()
    for shot in range(10):
        event = anchor.observe_shot(still_there(*TREE, source="zone"), shot=shot)
        assert event["kind"] == "no_departure" and not event["contradicted"]
    anchor.observe_shot({"decision": "unclear", "reason": "low_contrast"}, shot=11)
    anchor.observe_shot(None, shot=12)
    assert anchor.hint() is None and anchor.gate_anchor() is None
    assert anchor.diagnostics()["state"] == "none"


def test_full_frame_departures_are_no_evidence():
    """(e) Only hint or zone searches may feed the anchor."""
    anchor = TeeAnchor()
    for shot in range(5):
        assert anchor.observe_shot(departed(source="full_frame"), shot=shot)["kind"] == (
            "no_departure"
        )
    assert anchor.hint() is None


def test_a_departure_establishes_the_hint_but_blocking_needs_three():
    """(a, b) One departure gives the check its hint; the gate waits for three."""
    anchor = TeeAnchor()
    event = anchor.observe_shot(departed(source="zone"), shot=1)
    assert event["kind"] == "established" and event["confirmations"] == 1
    assert anchor.hint().x == TEE[0] and anchor.gate_anchor() is None
    assert anchor.diagnostics()["state"] == "unconfirmed"

    assert anchor.observe_shot(departed(TEE[0] + 4, TEE[1] - 2), shot=2)["kind"] == "confirmed"
    assert anchor.gate_anchor() is None
    event = anchor.observe_shot(departed(TEE[0] - 3, TEE[1] + 1), shot=3)
    assert event["blocking_enabled"] and event["confirmations"] == 3
    gate = anchor.gate_anchor()
    assert gate is not None and gate.x == pytest.approx(TEE[0])
    diagnostics = anchor.diagnostics()
    assert diagnostics["state"] == "confirmed" and diagnostics["blocking_enabled"]


def test_still_there_at_the_anchor_on_an_ops_shot_disables_blocking(caplog):
    """(b) Logged, counted, exposed; the gate pulses until three new departures."""
    anchor = confirmed_anchor()
    with caplog.at_level(logging.WARNING, logger="openflight.camera.tee_anchor"):
        event = anchor.observe_shot(still_there(TEE[0] + 2, TEE[1]), shot=35)
    assert event["contradicted"] and not event["blocking_enabled"]
    assert "Tee anchor contradicted" in caplog.text and "shot 35" in caplog.text
    assert anchor.gate_anchor() is None
    assert anchor.hint() is not None
    diagnostics = anchor.diagnostics()
    assert diagnostics["state"] == "contradicted"
    assert diagnostics["contradictions"] == 1
    assert diagnostics["last_contradiction"]["shot"] == 35

    for shot in (36, 37):
        anchor.observe_shot(departed(), shot=shot)
        assert anchor.gate_anchor() is None
    anchor.observe_shot(departed(), shot=38)
    assert anchor.gate_anchor() is not None
    assert anchor.diagnostics()["state"] == "confirmed"
    assert anchor.diagnostics()["contradictions"] == 1


def test_still_there_away_from_the_anchor_is_no_contradiction():
    anchor = confirmed_anchor()
    event = anchor.observe_shot(still_there(*TREE, source="zone"), shot=9)
    assert not event["contradicted"]
    assert anchor.gate_anchor() is not None


def test_relocation_needs_three_agreeing_departures():
    """(a) A new tee spot replaces the anchor only after three departures there."""
    anchor = confirmed_anchor()
    new_tee = (TEE[0] + 60, TEE[1] + 20)
    first = anchor.observe_shot(departed(*new_tee), shot=10)
    assert first["kind"] == "relocation_pending"
    assert anchor.gate_anchor().x == pytest.approx(TEE[0] + 1)
    # A scattered departure restarts the count.
    anchor.observe_shot(departed(TEE[0] - 80, TEE[1] + 40), shot=11)
    anchor.observe_shot(departed(*new_tee), shot=12)
    assert anchor.observe_shot(departed(*new_tee), shot=13)["kind"] == "relocation_pending"
    event = anchor.observe_shot(departed(new_tee[0] + 2, new_tee[1]), shot=14)
    assert event["kind"] == "relocated" and event["blocking_enabled"]
    assert anchor.gate_anchor().x == pytest.approx(new_tee[0])
    assert anchor.diagnostics()["relocations"] == 1


def test_a_departure_at_the_anchor_cancels_a_pending_relocation():
    anchor = confirmed_anchor()
    anchor.observe_shot(departed(TEE[0] + 60, TEE[1]), shot=10)
    anchor.observe_shot(departed(), shot=11)
    assert anchor.diagnostics()["relocation_pending"] == 0


def test_tee_height_change_moves_the_anchor_with_confirmations():
    """Iron off the mat (row 267) to a teed driver (row 252): within the match radius."""
    anchor = confirmed_anchor()
    for shot in range(5):
        assert anchor.observe_shot(departed(370.0, 252.0), shot=20 + shot)["kind"] == "confirmed"
    assert anchor.gate_anchor().y == pytest.approx(252.0)


def test_reset_forgets_the_anchor_but_keeps_the_counters():
    anchor = confirmed_anchor()
    anchor.observe_shot(still_there(), shot=4)
    anchor.reset("capture geometry changed")
    diagnostics = anchor.diagnostics()
    assert diagnostics["state"] == "none" and diagnostics["contradictions"] == 1
    assert diagnostics["last_event"] == {"kind": "reset", "reason": "capture geometry changed"}


# --- runtime: camera restarts keep the anchor (d) --------------------------------


def runtime_with_confirmed_anchor(tmp_path, monkeypatch) -> CameraCaptureRuntime:
    runtime = CameraCaptureRuntime(output_dir=tmp_path, use_gpio_trigger=False)
    for shot in range(CONFIRMATIONS_TO_BLOCK):
        runtime._tee_anchor.observe_shot(departed(), shot=shot)
    monkeypatch.setattr(runtime, "start", lambda: setattr(runtime, "_running", True))
    monkeypatch.setattr(runtime, "stop", lambda: setattr(runtime, "_running", False))
    runtime._running = True
    return runtime


def test_exposure_restart_keeps_the_confirmed_anchor(tmp_path, monkeypatch):
    """(d) No one-clip re-seed after an exposure change: the anchor stays confirmed."""
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    before = runtime.trigger_check_anchor()
    runtime.update_exposure_mode(automatic=False, exposure_us=100, gain=1.0)
    runtime._reset_capture_state()
    assert runtime.trigger_check_anchor() == before
    assert runtime.tee_anchor_status()["state"] == "confirmed"


def test_capture_geometry_change_resets_the_anchor(tmp_path, monkeypatch):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    runtime._capture_modes = [{"id": "compact", "width": 320, "height": 200, "fps": 300.0}]
    runtime.update_capture_mode("compact")
    assert runtime.trigger_check_anchor() is None
    assert runtime.tee_anchor_status()["last_event"]["reason"] == "capture geometry changed"


def test_tee_zone_applies_to_its_image_geometry_only(tmp_path, monkeypatch, caplog):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)
    runtime.set_tee_zone(zone)
    assert runtime.active_tee_zone() == zone
    assert runtime.tee_anchor_status()["zone"]["y0"] == 209
    runtime._capture_modes = [{"id": "compact", "width": 320, "height": 200, "fps": 300.0}]
    runtime.update_capture_mode("compact")
    assert runtime.active_tee_zone() is None
    with caplog.at_level(logging.WARNING):
        runtime.set_tee_zone(zone)
    assert runtime.active_tee_zone() is None and "ignored at 320x200" in caplog.text


def test_tee_zone_follows_a_switch_into_the_strip_and_back(tmp_path, monkeypatch):
    """T3: the 640x400 zone holds in the 640x200 strip, 100 rows up and clipped."""
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)
    runtime.set_tee_zone(zone)
    runtime._capture_modes = [
        {"id": "full-300", "width": 640, "height": 400, "fps": 300.0},
        {"id": "strip-500", "width": 640, "height": 200, "fps": 500.0},
    ]

    runtime.update_capture_mode("strip-500")

    strip = runtime.active_tee_zone()
    assert (strip.x0, strip.y0, strip.x1, strip.y1) == (263, 109, 454, 200)
    assert (strip.tee_x, strip.tee_y) == (pytest.approx(357.4), pytest.approx(208.7))
    assert strip.diameter_px == zone.diameter_px
    assert (strip.image_width_px, strip.image_height_px) == (640, 200)
    assert runtime.tee_anchor_status()["zone"]["y1"] == 200
    # The anchor itself starts afresh in the new window.
    assert runtime.trigger_check_anchor() is None

    runtime.update_capture_mode("full-300")

    assert runtime.active_tee_zone() == zone


def test_tee_zone_holds_through_strip_500_strip_560_and_back(tmp_path, monkeypatch):
    """strip-560 is the same 640x200 window as strip-500: the zone (and anchor) carry over."""
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)
    runtime.set_tee_zone(zone)
    runtime._capture_modes = [
        {"id": "full-300", "width": 640, "height": 400, "fps": 300.0},
        {"id": "strip-500", "width": 640, "height": 200, "fps": 500.0},
        {"id": "strip-560", "width": 640, "height": 200, "fps": 560.0},
    ]

    runtime.update_capture_mode("strip-500")
    strip_500 = runtime.active_tee_zone()
    for shot in range(CONFIRMATIONS_TO_BLOCK):
        runtime._tee_anchor.observe_shot(departed(), shot=shot)
    strip_anchor = runtime.trigger_check_anchor()
    runtime.update_capture_mode("strip-560")

    strip_560 = runtime.active_tee_zone()
    assert strip_560 == strip_500
    assert (strip_560.x0, strip_560.y0, strip_560.x1, strip_560.y1) == (263, 109, 454, 200)
    assert runtime.tee_anchor_status()["zone"]["y1"] == 200
    # Same frame, so the anchor confirmed in strip-500 stays.
    assert strip_anchor is not None and runtime.trigger_check_anchor() == strip_anchor

    runtime.update_capture_mode("full-300")

    assert runtime.active_tee_zone() == zone
    assert runtime.trigger_check_anchor() is None


def test_a_strip_zone_moves_with_the_strip_offset(tmp_path, monkeypatch):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    runtime.settings = CameraCaptureSettings(width=640, height=200)
    runtime._vertical_offset_px = 20
    zone = TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400)

    runtime.set_tee_zone(zone, vertical_offset_px=0)

    strip = runtime.active_tee_zone()
    assert (strip.y0, strip.y1, strip.tee_y) == (89, 200, pytest.approx(188.7))


def _runtime_reading_the_driver_window(tmp_path, monkeypatch, offset_px, **settings):
    """A runtime whose start() reads the sensor window as the real one does, sysfs at offset_px."""
    parameter = tmp_path / "strip_y_offset"
    parameter.write_text(f"{offset_px}\n", encoding="ascii")
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path,
        settings=CameraCaptureSettings(width=320, height=200, fps=500, **settings),
        use_gpio_trigger=False,
        vertical_offset_path=parameter,
    )

    def start():
        runtime._vertical_offset_px = runtime.vertical_crop_status()["vertical_offset_px"]
        runtime._running = True

    monkeypatch.setattr(runtime, "start", start)
    monkeypatch.setattr(runtime, "stop", lambda: setattr(runtime, "_running", False))
    runtime.start()
    runtime._capture_modes = [
        {"id": "crop-500", "width": 320, "height": 200, "fps": 500},
        {"id": "strip-500", "width": 640, "height": 200, "fps": 500},
    ]
    return runtime, parameter


def test_a_strip_offset_left_from_the_crop_moves_the_strip_zone_and_clip(tmp_path, monkeypatch):
    """Review: View down +30 in crop-500, then strip-500: the driver moves the strip 30 rows."""
    from openflight.camera.intrinsics import CameraIntrinsics, for_mode  # noqa: PLC0415
    from openflight.camera.triggered_buffer import CameraFrame, TriggeredCapture  # noqa: PLC0415

    runtime, parameter = _runtime_reading_the_driver_window(tmp_path, monkeypatch, 0)
    runtime.update_vertical_crop(30)
    runtime.update_capture_mode("strip-500")
    assert parameter.read_text(encoding="ascii").strip() == "30"

    runtime.set_tee_zone(
        TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400), vertical_offset_px=0
    )

    assert runtime.vertical_crop_status()["vertical_offset_px"] == 30
    assert not runtime.vertical_crop_status()["raw_crop_adjustable"]
    strip = runtime.active_tee_zone()
    assert (strip.y0, strip.y1, strip.tee_y) == (79, 200, pytest.approx(178.7))
    frame = CameraFrame(
        image=np.zeros((2, 3), dtype=np.uint8),
        sensor_timestamp_ns=0,
        host_timestamp_ns=100,
        exposure_us=1000,
        analogue_gain=4.0,
    )
    capture = TriggeredCapture(frames=(frame,), pre_trigger_count=1, trigger_host_timestamp_ns=100)
    settings = runtime._save_capture(1, 123.0, capture).metadata["settings"]
    assert settings["vertical_offset_px"] == 30
    base = CameraIntrinsics(
        575.7, 577.3, 347.8, 246.9, (0.0,) * 5, 640, 400, False, False, None, 0.4
    )
    strip_lens = for_mode(
        base, settings["width"], settings["height"], settings["vertical_offset_px"]
    )
    assert strip_lens.cy == pytest.approx(246.9 - 130.0)


@pytest.mark.parametrize("rotate_180", [False, True])
def test_strip_560_reads_the_driver_window_like_strip_500(tmp_path, monkeypatch, rotate_180):
    """The sysfs strip offset moves strip-560's zone and saved clip as it does strip-500's."""
    runtime, _parameter = _runtime_reading_the_driver_window(
        tmp_path, monkeypatch, 30, rotate_180=rotate_180
    )
    runtime._capture_modes.append({"id": "strip-560", "width": 640, "height": 200, "fps": 560})
    runtime.set_tee_zone(
        TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400), vertical_offset_px=0
    )
    runtime.update_capture_mode("strip-500")
    strip_500 = runtime.active_tee_zone()

    runtime.update_capture_mode("strip-560")

    assert runtime.settings.fps == 560
    assert runtime.vertical_crop_status()["vertical_offset_px"] == 30
    assert not runtime.vertical_crop_status()["raw_crop_adjustable"]
    strip = runtime.active_tee_zone()
    assert strip == strip_500
    expected = (139, 200, pytest.approx(238.7)) if rotate_180 else (79, 200, pytest.approx(178.7))
    assert (strip.y0, strip.y1, strip.tee_y) == expected


def test_a_rotated_mount_moves_the_strip_zone_the_other_way(tmp_path, monkeypatch):
    """Review: the 180-degree mount rotates saved frames in software, so +s is 100 - s rows."""
    runtime, _parameter = _runtime_reading_the_driver_window(
        tmp_path, monkeypatch, 30, rotate_180=True
    )
    runtime.update_capture_mode("strip-500")

    runtime.set_tee_zone(
        TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400), vertical_offset_px=0
    )

    strip = runtime.active_tee_zone()
    assert (strip.y0, strip.y1, strip.tee_y) == (139, 200, pytest.approx(238.7))


def test_a_zone_outside_the_strip_is_no_zone(tmp_path, monkeypatch):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    runtime.settings = CameraCaptureSettings(width=640, height=200)
    zone = TeeZone(263, 330, 454, 390, 357.4, 360.0, 16.5, 640, 400)

    runtime.set_tee_zone(zone)

    assert runtime.active_tee_zone() is None


def test_a_flip_drops_the_tee_zone(tmp_path, monkeypatch):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    runtime.set_tee_zone(TeeZone(263, 209, 454, 390, 357.4, 308.7, 16.5, 640, 400))

    runtime.settings = CameraCaptureSettings(rotate_180=True)

    assert runtime.active_tee_zone() is None


def test_status_exposes_the_anchor_diagnostics(tmp_path):
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path, settings=CameraCaptureSettings(), use_gpio_trigger=False
    )
    status = runtime.status()["tee_anchor"]
    assert status["state"] == "none" and status["zone"] is None
    assert status["confirmations_to_block"] == CONFIRMATIONS_TO_BLOCK


# --- spot contrast and blocked edges (review round 2) ------------------------------


def test_the_gate_anchor_carries_the_balls_normalised_contrast():
    """Departures keep their contrast, scaled to 100 us x gain 1: -60 at 200 -> -30."""
    anchor = TeeAnchor()
    anchor.observe_shot(departed(contrast=-60.0), shot=1)
    anchor.observe_shot(departed(contrast=-50.0), shot=2)
    anchor.observe_shot(departed(contrast=-70.0), shot=3)
    gate = anchor.gate_anchor()
    assert isinstance(gate, AnchorBall)
    assert gate.contrast == pytest.approx(-30.0)
    assert anchor.diagnostics()["anchor"]["contrast"] == pytest.approx(-30.0)


@pytest.mark.parametrize("missing", ["departure_contrast", "exposure_gain"])
def test_a_departure_without_contrast_or_exposure_is_no_evidence(missing):
    """The gate could not match such a spot, so it never places the anchor."""
    check = departed()
    del check[missing]
    anchor = TeeAnchor()
    assert anchor.observe_shot(check, shot=1)["kind"] == "no_departure"
    assert anchor.hint() is None


def blocked_check(departure_x: float, departure_y: float = TEE[1]) -> dict:
    """Trigger check of a blocked edge: the anchor spot stayed, a zone spot left."""
    return {
        **still_there(),
        "departure_x": departure_x,
        "departure_y": departure_y,
        "departure_diameter_px": DIAMETER,
        "departure_source": "zone",
        "departure_contrast": CONTRAST,
        "exposure_gain": 200.0,
    }


def test_a_blocked_edge_whose_ball_left_elsewhere_contradicts_the_anchor(caplog):
    """Finding 2: a stale anchor must not keep blocking every strike from the new spot."""
    anchor = confirmed_anchor()
    with caplog.at_level(logging.WARNING, logger="openflight.camera.tee_anchor"):
        event = anchor.observe_blocked_edge(blocked_check(TEE[0] + 2 * DIAMETER), edge=1234.5)
    assert event["kind"] == "blocked_departure" and event["contradicted"]
    assert anchor.gate_anchor() is None
    assert "gate blocked edge 1234.500" in caplog.text
    diagnostics = anchor.diagnostics()
    assert diagnostics["state"] == "contradicted"
    assert diagnostics["blocked_contradictions"] == 1 and diagnostics["contradictions"] == 1
    assert diagnostics["last_contradiction"]["kind"] == "blocked_departure"
    assert diagnostics["last_contradiction"]["edge"] == 1234.5
    # Not an OPS shot: the spot is no relocation sample.
    assert diagnostics["relocation_pending"] == 0
    for shot in range(CONFIRMATIONS_TO_BLOCK):
        anchor.observe_shot(departed(), shot=40 + shot)
    assert anchor.gate_anchor() is not None


def test_blocked_edges_without_a_departure_elsewhere_keep_the_anchor():
    anchor = confirmed_anchor()
    assert anchor.observe_blocked_edge(still_there(), edge=1.0) is None
    assert anchor.observe_blocked_edge(blocked_check(TEE[0] + 3.0), edge=2.0) is None
    assert anchor.observe_blocked_edge(None, edge=3.0) is None
    assert anchor.gate_anchor() is not None
    assert anchor.diagnostics()["blocked_contradictions"] == 0
    assert TeeAnchor().observe_blocked_edge(blocked_check(TEE[0] + 40.0)) is None


@pytest.mark.parametrize("blocked_first", [True, False])
def test_runtime_pairs_a_blocked_edge_with_its_clip_in_either_order(
    tmp_path, monkeypatch, blocked_first
):
    """The gate decides within its deadline; the clip may be saved before or after."""
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)
    check = blocked_check(TEE[0] + 40.0)
    other = blocked_check(TEE[0] + 40.0)
    assert runtime.note_trigger_check(99.0, other) is None  # another edge's clip
    if blocked_first:
        assert runtime.note_blocked_edge(123.0) is None
        event = runtime.note_trigger_check(123.0, check)
    else:
        assert runtime.note_trigger_check(123.0, check) is None
        event = runtime.note_blocked_edge(123.0)
    assert event["kind"] == "blocked_departure"
    assert runtime.trigger_check_anchor() is None
    assert runtime.tee_anchor_status()["blocked_contradictions"] == 1
    # Each edge pairs once.
    assert runtime.note_blocked_edge(123.0) is None


def test_runtime_blocked_edge_update_never_raises(tmp_path, monkeypatch, caplog):
    runtime = runtime_with_confirmed_anchor(tmp_path, monkeypatch)

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime._tee_anchor, "observe_blocked_edge", explode)
    runtime.note_blocked_edge(5.0)
    assert runtime.note_trigger_check(5.0, blocked_check(TEE[0] + 40.0)) is None
    assert "blocked-edge update failed" in caplog.text


def test_moving_the_sensor_window_drops_the_tee_zone(tmp_path, monkeypatch, caplog):
    """Finding 3: a vertical crop move shifts the image, so the old zone no longer applies."""
    parameter = tmp_path / "strip_y_offset"
    parameter.write_text("0\n", encoding="ascii")
    runtime = CameraCaptureRuntime(
        output_dir=tmp_path,
        settings=CameraCaptureSettings(width=320, height=200),
        use_gpio_trigger=False,
        vertical_offset_path=parameter,
    )
    monkeypatch.setattr(runtime, "start", lambda: setattr(runtime, "_running", True))
    monkeypatch.setattr(runtime, "stop", lambda: setattr(runtime, "_running", False))
    runtime._running = True
    zone = TeeZone(110, 95, 230, 180, 165.0, 135.0, 15.0, 320, 200)
    runtime.set_tee_zone(zone)
    assert runtime.active_tee_zone() == zone

    with caplog.at_level(logging.WARNING):
        runtime.update_vertical_crop(10)
    assert runtime.active_tee_zone() is None
    assert runtime.tee_anchor_status()["zone"] is None
    assert "Tee zone was computed for the sensor window at +0 px" in caplog.text

    # A zone set for the new window applies again.
    runtime.set_tee_zone(zone)
    assert runtime.active_tee_zone() == zone


def test_a_blocked_ball_that_left_beside_the_judged_spot_contradicts_the_anchor():
    """Two-spot lockout: the check judged a static spot 15 px beside the tee ball.

    The ball that left lies within the anchor's match radius, but more than
    half a diameter from the spot the check judged: the block was wrong.
    """
    anchor = confirmed_anchor()
    judged = (TEE[0] + 15.0, TEE[1])
    check = {**blocked_check(TEE[0]), "ball_x": judged[0], "ball_y": judged[1]}
    event = anchor.observe_blocked_edge(check, edge=7.0)
    assert event is not None and event["contradicted"]
    assert anchor.gate_anchor() is None


def test_a_blocked_ball_that_left_at_the_judged_spot_keeps_the_anchor(caplog):
    anchor = confirmed_anchor()
    judged = (TEE[0] + 15.0, TEE[1])
    check = {**blocked_check(judged[0] + 3.0), "ball_x": judged[0], "ball_y": judged[1]}
    with caplog.at_level(logging.INFO, logger="openflight.camera.tee_anchor"):
        assert anchor.observe_blocked_edge(check, edge=7.0) is None
    assert "left at 386,267, at the judged spot 383,267" in caplog.text
    assert anchor.gate_anchor() is not None


def test_a_blocked_check_without_a_hint_spot_compares_with_the_anchor(caplog):
    """No spot judged at the anchor (zone search): the anchor is the reference."""
    anchor = confirmed_anchor()
    near = {**blocked_check(TEE[0] + 10.0), "ball_source": "zone", "ball_x": 300.0}
    with caplog.at_level(logging.INFO, logger="openflight.camera.tee_anchor"):
        assert anchor.observe_blocked_edge(near, edge=8.0) is None
    # The log names the tee anchor, not the zone spot the check judged at x=300.
    assert "left at 378,267, at the tee anchor 369,267" in caplog.text
    assert "judged spot" not in caplog.text
    far = {**blocked_check(TEE[0] + 2 * DIAMETER), "ball_source": "zone", "ball_x": 300.0}
    assert anchor.observe_blocked_edge(far, edge=9.0)["contradicted"]


def test_a_ball_mark_departure_establishes_and_confirms_the_anchor():
    """The check's ball-mark search (``ball_source`` ``ball_mark``) is anchor evidence."""
    anchor = TeeAnchor()
    kinds = [
        anchor.observe_shot(departed(TEE[0] + shot, TEE[1], source="ball_mark"), shot=shot)["kind"]
        for shot in range(CONFIRMATIONS_TO_BLOCK)
    ]
    assert kinds == ["established"] + ["confirmed"] * (CONFIRMATIONS_TO_BLOCK - 1)
    assert anchor.gate_anchor() is not None
