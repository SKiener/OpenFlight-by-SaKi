"""Mode selection must reach the runtime and invalidate pixel anchors."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openflight import server


def setup_api(monkeypatch):
    update = {"capture_mode": "strip-500", "width": 640, "height": 200, "fps": 500}
    runtime = SimpleNamespace(
        settings=SimpleNamespace(fps=500, post_ms=100.0, post_frames=50),
        status=lambda: {"running": True},
        vertical_crop_status=lambda: {"raw_crop_adjustable": True},
        capture_mode_status=lambda: update,
        update_capture_mode=Mock(return_value=update),
        # The handler shows the hitting zone of the new mode (T3).
        active_tee_zone=lambda: None,
    )
    emitted = []
    monkeypatch.setattr(server, "camera_capture_runtime", runtime)
    monkeypatch.setattr(server, "camera_capture_config", {})
    monkeypatch.setattr(server, "get_session_logger", lambda: None)
    monkeypatch.setattr(server.socketio, "emit", lambda *args: emitted.append(args))
    monkeypatch.setattr(server, "camera_reference_ball_tracker", object())
    monkeypatch.setattr(server, "camera_ball_flight_reference_tracker", object())
    return runtime, emitted


def test_update_mode_refreshes_configuration_and_reference_trackers(monkeypatch):
    runtime, emitted = setup_api(monkeypatch)
    before = server.camera_reference_ball_tracker
    server.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
    runtime.update_capture_mode.assert_called_once_with("strip-500")
    assert emitted[-1][0] == "camera_capture_settings"
    assert emitted[-1][1]["height"] == 200
    assert server.camera_capture_config["fps"] == 500
    assert server.camera_reference_ball_tracker is not before


@pytest.mark.parametrize("extra", [{"vertical_offset_px": 10}, {"auto_exposure_enabled": True}])
def test_mixed_reconfiguration_is_rejected_before_hardware_changes(monkeypatch, extra):
    runtime, emitted = setup_api(monkeypatch)
    server.handle_set_camera_capture_settings({"capture_mode": "strip-500", **extra})
    runtime.update_capture_mode.assert_not_called()
    assert emitted[-1][0] == "camera_capture_settings_error"


def test_mode_switch_waits_for_shot_processing(monkeypatch):
    runtime, emitted = setup_api(monkeypatch)
    monkeypatch.setattr(server, "_shot_finalization_running", True)
    server.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
    runtime.update_capture_mode.assert_not_called()
    assert "shot is being processed" in emitted[-1][1]["error"]


def test_a_mode_switch_to_a_lower_frame_rate_warns_when_clips_get_too_short(monkeypatch, caplog):
    """The post-trigger milliseconds stay; 79 ms at 288 fps are 23 frames, the locator needs 29."""
    runtime, _emitted = setup_api(monkeypatch)
    runtime.settings = SimpleNamespace(fps=288.0, post_ms=79.0, post_frames=23)
    with caplog.at_level("WARNING", logger=server.logger.name):
        server.handle_set_camera_capture_settings({"capture_mode": "full-288"})
    warnings = [record.getMessage() for record in caplog.records if "[CAMERA]" in record.message]
    assert len(warnings) == 1
    assert "--camera-capture-post-ms 101 " in warnings[0]


def test_a_mode_switch_with_long_enough_clips_does_not_warn(monkeypatch, caplog):
    setup_api(monkeypatch)
    with caplog.at_level("WARNING", logger=server.logger.name):
        server.handle_set_camera_capture_settings({"capture_mode": "strip-500"})
    assert not [record for record in caplog.records if "[CAMERA]" in record.message]


def test_the_settings_event_is_registered_to_the_settings_handler():
    """A helper placed under the decorator would take over the event (3881f0e)."""
    handlers = server.socketio.server.handlers["/"]
    registered = handlers["set_camera_capture_settings"]
    assert (
        getattr(registered, "__wrapped__", registered) is server.handle_set_camera_capture_settings
    )
    misplaced = {
        event: handler.__name__
        for event, handler in handlers.items()
        if not handler.__name__.startswith("handle_")
    }
    assert not misplaced


def test_a_settings_event_from_a_client_gets_a_reply(monkeypatch):
    """Through the real Socket.IO dispatch: the UI waits for a reply to leave 'updating'."""
    monkeypatch.setattr(server, "camera_capture_runtime", None)
    sio = server.socketio.server
    monkeypatch.setattr(sio, "async_handlers", False)
    client = server.socketio.test_client(server.app, flask_test_client=server.app.test_client())
    try:
        client.get_received()
        client.emit("set_camera_capture_settings", {"alignment_x_pct": 40})
        names = [message["name"] for message in client.get_received()]
    finally:
        client.disconnect()
    assert names and names[-1] in {"camera_capture_settings", "camera_capture_settings_error"}
