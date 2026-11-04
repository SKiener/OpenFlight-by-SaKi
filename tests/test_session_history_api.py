"""History is read-only and remains separate from the current live session."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import quote

from openflight import server


def test_history_routes_do_not_modify_live_session_or_files(tmp_path, monkeypatch):
    now = datetime.now().astimezone()
    session_id = f"session_{now:%Y%m%d_%H%M%S}_range"
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text(
        json.dumps({"type": "session_start", "start_time": now.isoformat()})
        + "\n"
        + json.dumps(
            {
                "type": "shot_detected",
                "timestamp": now.isoformat(),
                "ball_speed_mph": 110,
                "shot_number": 1,
            }
        )
        + "\n"
    )
    original = path.read_bytes()
    live_shots = [object()]
    monkeypatch.setattr(server, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path))
    monkeypatch.setattr(server, "monitor", SimpleNamespace(shots=live_shots))
    client = server.app.test_client()
    response = client.get("/api/session-history")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["days"] == 30
    assert response.json["sessions"][0]["id"] == session_id
    detail = client.get(f"/api/session-history/{session_id}")
    assert detail.status_code == 200
    assert detail.json["shots"][0]["ball_speed_mph"] == 110
    assert client.get("/api/session-history/not-a-session").status_code == 404
    assert client.post("/api/session-history").status_code == 405
    assert client.delete(f"/api/session-history/{session_id}").status_code == 405
    assert server.monitor.shots is live_shots
    assert path.read_bytes() == original


def test_history_video_uses_existing_replay_api_after_restart(tmp_path, monkeypatch):
    import numpy as np

    from openflight.camera.replay import CameraReplayManager

    now = datetime.now().astimezone()
    session_id = f"session_{now:%Y%m%d_%H%M%S}_range"
    capture = tmp_path / "camera" / "clip"
    capture.mkdir(parents=True)
    np.savez(capture / "frames.npz", frames=np.zeros((6, 4, 6), dtype=np.uint8))
    entries = [
        {"type": "session_start", "start_time": now.isoformat()},
        {"type": "camera_capture", "shot_number": 1, "shot_timestamp": 100.0,
         "capture_path": str(capture), "capture_error": None,
         "metadata": {"frame_count": 6, "pre_trigger_frames": 4}},
        {"type": "shot_detected", "shot_number": 1, "impact_timestamp": 100.0,
         "timestamp": now.isoformat(), "ball_speed_mph": 103},
    ]
    (tmp_path / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    monkeypatch.setattr(server, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path))
    # Camera disconnected today: historical files should still be discoverable.
    monkeypatch.setattr(server, "camera_replay_manager", None)
    client = server.app.test_client()
    payload = client.get(f"/api/session-history/{session_id}").json
    replay_id = payload["shots"][0]["camera_replay"]["id"]
    assert "capture_path" not in json.dumps(payload)
    assert not (capture / "replay.mp4").exists()
    assert client.get(f"/api/camera/replays/{replay_id}/video").status_code == 409
    (capture / "replay.mp4").write_bytes(b"cached test mp4")
    prepared = client.post(f"/api/camera/replays/{replay_id}/prepare")
    assert prepared.status_code == 200
    # The streamed response keeps the mp4 open until it is closed.
    with client.get(prepared.json["video_url"]) as video:
        assert video.data == b"cached test mp4"
        assert video.content_type == "video/mp4"
    monkeypatch.setattr(server, "camera_replay_manager", CameraReplayManager(tmp_path))
    assert client.post(f"/api/camera/replays/{replay_id}/prepare").status_code == 404
    restored = client.get(f"/api/session-history/{session_id}").json
    new_id = restored["shots"][0]["camera_replay"]["id"]
    assert new_id != replay_id
    assert client.post(f"/api/camera/replays/{new_id}/prepare").status_code == 200


def test_history_detail_returns_flight_paths_only_on_request(tmp_path, monkeypatch):
    now = datetime.now().astimezone()
    session_id = f"session_{now:%Y%m%d_%H%M%S}_range"
    flight_path = {"unit": "yd", "points": [[0.0, 0.0, 0.0, 0.0], [5.2, 135.6, 7.66, 0.0]]}
    entries = [
        {"type": "session_start", "start_time": now.isoformat()},
        {
            "type": "shot_detected",
            "shot_number": 1,
            "timestamp": now.isoformat(),
            "ball_speed_mph": 110,
            "flight_path": flight_path,
        },
    ]
    (tmp_path / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries))
    monkeypatch.setattr(server, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path))
    monkeypatch.setattr(server, "camera_replay_manager", None)
    client = server.app.test_client()
    url = f"/api/session-history/{session_id}"

    assert "flight_path" not in client.get(url).json["shots"][0]
    for not_one in ("0", "true", ""):
        assert "flight_path" not in client.get(f"{url}?flight_path={not_one}").json["shots"][0]
    asked = client.get(f"{url}?flight_path=1")
    assert asked.status_code == 200
    assert asked.json["shots"][0]["flight_path"] == flight_path
    # Asking once changes nothing for the next plain request, nor for the session listing.
    assert "flight_path" not in client.get(url).json["shots"][0]
    listing = client.get("/api/session-history").json
    assert client.get("/api/session-history?flight_path=1").json == listing


def test_club_averages_endpoint(tmp_path, monkeypatch):
    now = datetime.now().astimezone()
    entries = [
        {"type": "session_start", "start_time": now.isoformat()},
        {
            "type": "shot_detected",
            "timestamp": now.isoformat(),
            "ball_speed_mph": 110,
            "club": "7iron",
            "carry_spin_adjusted": 150,
            "total_yards": 160.0,
        },
    ]
    (tmp_path / f"session_{now:%Y%m%d_%H%M%S}_range.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries)
    )
    monkeypatch.setattr(server, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path))
    client = server.app.test_client()
    response = client.get("/api/club-averages?club=7iron")
    assert response.status_code == 200
    assert response.json == {
        "club": "7iron",
        "total_yards": 160.0,
        "total_count": 1,
        "carry_yards": 150,
        "carry_count": 1,
    }
    for query in ("", "?club="):
        missing = client.get(f"/api/club-averages{query}")
        assert missing.status_code == 400
        assert "error" in missing.json


def test_club_averages_endpoint_passes_the_profile(tmp_path, monkeypatch):
    asked = []

    def averages(log_dir, club, *, profile_id=None):
        asked.append((log_dir, club, profile_id))
        return {"club": club}

    monkeypatch.setattr(server, "club_averages", averages)
    monkeypatch.setattr(server, "get_session_logger", lambda: SimpleNamespace(log_dir=tmp_path))
    client = server.app.test_client()
    for query, profile_id in (("&profile=anna%20k", "anna k"), ("&profile=", None), ("", None)):
        response = client.get(f"/api/club-averages?club=7iron{query}")
        assert response.status_code == 200
        assert asked.pop() == (tmp_path, "7iron", profile_id)


def test_delete_saved_shot_endpoint(tmp_path, monkeypatch):
    now = datetime.now().astimezone()
    session_id = f"session_{now:%Y%m%d_%H%M%S}_range"
    stamps = [now.isoformat(), (now + timedelta(seconds=1)).isoformat()]
    entries = [{"type": "session_start", "start_time": now.isoformat()}] + [
        {"type": "shot_detected", "timestamp": stamp, "ball_speed_mph": 110, "shot_number": n}
        for n, stamp in enumerate(stamps, start=1)
    ]
    path = tmp_path / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries))
    monkeypatch.setattr(
        server,
        "get_session_logger",
        lambda: SimpleNamespace(log_dir=tmp_path, current_path=None),
    )
    client = server.app.test_client()
    url = f"/api/session-history/{session_id}/shots/{quote(stamps[0], safe='')}"
    first = client.delete(url)
    assert first.status_code == 204
    assert client.delete(url).status_code == 204
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["shot_timestamp"] for line in lines if line["type"] == "shot_deleted"] == [
        stamps[0]
    ]
    detail = client.get(f"/api/session-history/{session_id}").json
    assert [shot["timestamp"] for shot in detail["shots"]] == [stamps[1]]
    unknown = quote((now + timedelta(seconds=9)).isoformat(), safe="")
    assert client.delete(f"/api/session-history/{session_id}/shots/{unknown}").status_code == 404
    missing = f"session_{now:%Y%m%d_%H%M%S}_other"
    assert client.delete(f"/api/session-history/{missing}/shots/x").status_code == 404
    assert client.delete("/api/session-history/not-a-session/shots/x").status_code == 400
