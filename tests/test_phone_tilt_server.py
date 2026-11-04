"""Server side of the phone TI tilt calibration: REST, startup precedence, recovery."""

import json
import math
from types import SimpleNamespace

import pytest
from test_phone_tilt import body, position

from openflight import server as server_module
from openflight.inclinometer import OrientationSnapshot, SnapshotSelection
from openflight.iwr6843.calibration import Calibration
from openflight.launch_monitor import Shot
from openflight.pairing import DeviceSecret, PairingCodes
from openflight.phone_tilt import build_record, measure_phone_tilt, save_calibration

URL = "/api/calibration/iwr6843/phone-tilt"
PHONE_ADDR = "192.168.1.50"


def _selection(pitch):
    if pitch is None:
        return SnapshotSelection(snapshot=None, status="moving", age_s=0.1)
    return SnapshotSelection(
        snapshot=OrientationSnapshot(
            timestamp=1.0,
            x_g=0.0,
            y_g=0.0,
            z_g=1.0,
            gravity_g=1.0,
            raw_pitch_deg=pitch - 1.4,
            calibrated_pitch_deg=pitch,
            pitch_std_deg=0.05,
            sample_count=8,
        ),
        status="stable",
        age_s=0.1,
    )


class FakeInclinometer:
    def __init__(self, pitch=1.4, error=None):
        self.pitch = pitch
        self.error = error
        self.waits = []

    def wait_for_stable(self, timeout_s=2.0):
        self.waits.append(timeout_s)
        if self.error is not None:
            raise self.error
        return _selection(self.pitch)

    def snapshot_for_impact(self, _impact_timestamp):
        return _selection(self.pitch)


class FakeCaptureMonitor:
    def __init__(self, **_kwargs):
        self.port = "/dev/ttyUSB0"

    def start(self, *, armed=True):
        return None

    def stop(self):
        return None


@pytest.fixture(name="path")
def fixture_path(tmp_path, monkeypatch):
    path = tmp_path / "config" / "iwr6843_phone_orientation.json"
    monkeypatch.setattr(server_module, "PHONE_TILT_CALIBRATION_PATH", path)
    return path


@pytest.fixture(name="emitted")
def fixture_emitted(monkeypatch):
    events = []
    monkeypatch.setattr(
        server_module.socketio, "emit", lambda *args, **_kwargs: events.append(args)
    )
    return events


@pytest.fixture(name="config_changes")
def fixture_config_changes(monkeypatch, tmp_path):
    changes = []
    logger = SimpleNamespace(
        log_dir=tmp_path,
        log_config_change=lambda config, source="user": changes.append(
            (json.loads(json.dumps(config)), source)
        ),
    )
    monkeypatch.setattr(server_module, "get_session_logger", lambda: logger)
    return changes


@pytest.fixture(name="init_ti")
def fixture_init_ti(monkeypatch, tmp_path):
    """Run the real init_iwr6843 against a fake capture monitor; returns the kwargs."""
    monkeypatch.setattr("openflight.iwr6843.monitor.IWR6843CaptureMonitor", FakeCaptureMonitor)
    monkeypatch.setattr("openflight.iwr6843.monitor.tx_order_from_config", lambda _p: "normal")
    monkeypatch.setattr(server_module, "trigger_gate", None)
    monkeypatch.setattr(server_module, "camera_capture_runtime", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime_config", {"enabled": False})
    monkeypatch.setattr(server_module, "inclinometer_service", None)

    def load(_path):
        calibration = Calibration.identity()
        calibration.tilt_rad = math.radians(10.405)
        return calibration

    monkeypatch.setattr(Calibration, "load", load)
    kwargs = {
        "port": "/dev/ttyUSB0",
        "config_path": "snapshot.cfg",
        "calibration_path": "cal.json",
        "output_dir": tmp_path,
        "trigger_pin": 17,
        "tee_range_m": 1.5,
        "net_range_m": 4.6,
        "tx_order": "auto",
        "capture_timeout_s": 12.0,
        "tilt_deg": 10.0,
    }

    def run(**overrides):
        assert server_module.init_iwr6843(**{**kwargs, **overrides})
        return kwargs

    return run


def configured_tilt():
    return math.degrees(server_module.iwr6843_runtime.calibration.tilt_rad)


def kiosk():
    return server_module.app.test_client()


def saved_record(path, *, case_tilt=1.0, pitch=1.4):
    payload = body(case_tilt=case_tilt)
    record = build_record(
        payload, measure_phone_tilt(payload), inclinometer_pitch_deg=pitch, applied_at="t0"
    )
    save_calibration(record, path)
    return record


# --- startup precedence --------------------------------------------------------------


def test_startup_without_phone_file_uses_cli(path, init_ti, capsys):
    init_ti()
    assert configured_tilt() == pytest.approx(10.0)
    config = server_module.iwr6843_runtime_config
    assert config["tilt_source"] == "cli"
    assert config["tilt_fallback_deg"] == pytest.approx(10.0)
    assert config["phone_tilt_calibration"] is None
    assert "IWR6843 tilt: 10.000deg from cli" in capsys.readouterr().out
    assert not path.exists()


def test_startup_without_cli_or_phone_uses_calibration_json(path, init_ti):
    init_ti(tilt_deg=None)
    assert configured_tilt() == pytest.approx(10.405)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "calibration_json"
    assert server_module.iwr6843_runtime_config["tilt_fallback_source"] == "calibration_json"


def test_saved_phone_calibration_outranks_cli(path, init_ti, capsys):
    record = saved_record(path, case_tilt=1.0, pitch=1.4)
    init_ti()
    assert configured_tilt() == pytest.approx(9.6)
    config = server_module.iwr6843_runtime_config
    assert config["tilt_source"] == "phone"
    assert config["tilt_fallback_source"] == "cli"
    assert config["phone_tilt_calibration"]["radar_tilt_deg"] == record["radar_tilt_deg"]
    out = capsys.readouterr().out
    assert "from the phone calibration" in out and "ignoring cli 10.000deg" in out


def test_saved_phone_calibration_outranks_calibration_json(path, init_ti):
    saved_record(path)
    init_ti(tilt_deg=None)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "phone"
    assert server_module.iwr6843_runtime_config["tilt_fallback_deg"] == pytest.approx(10.405)


def test_invalid_phone_file_is_ignored(path, init_ti, caplog):
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    init_ti()
    assert configured_tilt() == pytest.approx(10.0)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "cli"
    assert "Ignoring invalid phone tilt calibration" in caplog.text


# --- recovery keeps the calibrated value ---------------------------------------------


class _Cycler:
    def __init__(self):
        self.cycles = 0

    def cycle(self):
        self.cycles += 1


def test_usb_power_cycle_recovery_keeps_the_phone_tilt(path, init_ti, monkeypatch):
    """Upstream silently fell back to the CLI tilt when init_iwr6843 re-ran."""
    from openflight.iwr6843 import usb_power

    saved_record(path, case_tilt=1.0, pitch=1.4)
    kwargs = init_ti()
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(usb_power, "wait_for_tty", lambda _port, **_k: True)
    cycler = _Cycler()

    assert server_module._recover_iwr6843_at_startup(cycler, "/dev/ttyUSB0", kwargs)
    assert cycler.cycles == 1
    assert kwargs["tilt_deg"] == 10.0
    assert configured_tilt() == pytest.approx(9.6)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "phone"


def test_live_applied_tilt_survives_reinit(path, init_ti, emitted, config_changes, monkeypatch):
    kwargs = init_ti()
    monkeypatch.setattr(server_module, "inclinometer_service", FakeInclinometer(pitch=0.5))
    response = kiosk().post(URL, json=body(case_tilt=2.0))
    assert response.status_code == 200
    assert configured_tilt() == pytest.approx(11.5)

    server_module.init_iwr6843(**kwargs)

    assert configured_tilt() == pytest.approx(11.5)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "phone"


# --- POST ------------------------------------------------------------------------------


def test_post_applies_persists_logs_and_emits(path, init_ti, emitted, config_changes, monkeypatch):
    init_ti()
    sensor = FakeInclinometer(pitch=1.4)
    monkeypatch.setattr(server_module, "inclinometer_service", sensor)

    response = kiosk().post(URL, json=body(case_tilt=1.0, bias=0.3, client={"device": "iPhone"}))

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "applied"
    assert data["event"] == "applied"
    assert data["tilt_source"] == "phone"
    assert data["configured_iwr_tilt_deg"] == pytest.approx(9.6)
    assert data["fallback_iwr_tilt_deg"] == pytest.approx(10.0)
    assert data["fallback_source"] == "cli"
    assert data["inclinometer_enabled"] is True
    calibration = data["calibration"]
    assert calibration["radar_tilt_deg"] == pytest.approx(11.0)
    assert calibration["case_tilt_deg"] == pytest.approx(1.0)
    assert calibration["phone_bias_deg"] == pytest.approx(0.3)
    assert calibration["inclinometer_pitch_deg"] == pytest.approx(1.4)
    assert calibration["inclinometer_minus_phone_deg"] == pytest.approx(0.4)
    assert calibration["convention"] == "w3c"
    assert calibration["client"] == {"device": "iPhone"}
    assert "request" not in calibration
    assert sensor.waits == [2.0]

    assert configured_tilt() == pytest.approx(9.6)
    saved = json.loads(path.read_text())
    assert saved["configured_iwr_tilt_deg"] == pytest.approx(9.6)
    assert saved["request"]["position_a"]["sample_count"] == 60

    [(config, source)] = config_changes
    assert source == "phone_tilt_calibration"
    assert config["iwr6843"]["tilt_source"] == "phone"
    assert config["iwr6843"]["tilt_deg"] == pytest.approx(9.6)
    [(name, payload)] = emitted
    assert name == "iwr6843_tilt_calibration"
    assert payload == {key: value for key, value in data.items() if key != "status"}


def test_post_without_inclinometer_configures_radar_tilt(path, init_ti, emitted, config_changes):
    init_ti()
    response = kiosk().post(URL, json=body(case_tilt=-0.75))
    assert response.status_code == 200
    data = response.get_json()
    assert data["configured_iwr_tilt_deg"] == pytest.approx(9.25)
    assert data["calibration"]["inclinometer_pitch_deg"] is None
    assert data["calibration"]["inclinometer_minus_phone_deg"] is None


def test_shot_after_calibration_uses_radar_tilt_plus_pitch_change(
    path, init_ti, emitted, config_changes, monkeypatch
):
    init_ti()
    monkeypatch.setattr(server_module, "inclinometer_service", FakeInclinometer(pitch=1.4))
    monkeypatch.setattr(server_module, "inclinometer_runtime_config", {"zero_offset_deg": 1.4})
    assert kiosk().post(URL, json=body(case_tilt=1.0)).status_code == 200

    server_module.inclinometer_service.pitch = 1.9  # case moved 0.5 deg up since
    shot = Shot(ball_speed_mph=120.0, timestamp=None, mode="rolling-buffer")
    server_module._snapshot_inclinometer_for_shot(shot)
    assert shot.inclinometer["effective_iwr_tilt_deg"] == pytest.approx(11.5)


def _assert_unchanged(path, emitted, config_changes, tilt=10.0):
    assert configured_tilt() == pytest.approx(tilt)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "cli"
    assert not path.exists()
    assert emitted == []
    assert config_changes == []


def test_unstable_inclinometer_is_409_and_changes_nothing(
    path, init_ti, emitted, config_changes, monkeypatch
):
    init_ti()
    monkeypatch.setattr(server_module, "inclinometer_service", FakeInclinometer(pitch=None))
    response = kiosk().post(URL, json=body())
    assert response.status_code == 409
    assert response.get_json()["error"] == "inclinometer_unstable"
    assert response.get_json()["inclinometer_status"] == "moving"
    _assert_unchanged(path, emitted, config_changes)


def test_failing_inclinometer_is_409(path, init_ti, emitted, config_changes, monkeypatch):
    init_ti()
    sensor = FakeInclinometer(error=OSError("i2c"))
    monkeypatch.setattr(server_module, "inclinometer_service", sensor)
    response = kiosk().post(URL, json=body())
    assert response.status_code == 409
    assert response.get_json()["error"] == "inclinometer_error"
    _assert_unchanged(path, emitted, config_changes)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (body(case_tilt=9.0), "case_tilt_out_of_range"),
        (body(a=position(0.0, samples=10)), "too_few_samples"),
        (body(bias=4.0), "phone_bias_out_of_range"),
        ({"nope": 1}, "invalid_payload"),
    ],
)
def test_invalid_measurement_is_422_and_changes_nothing(
    path, init_ti, emitted, config_changes, payload, reason
):
    init_ti()
    response = kiosk().post(URL, json=payload)
    assert response.status_code == 422
    assert response.get_json()["error"] == reason
    assert "message" in response.get_json()
    _assert_unchanged(path, emitted, config_changes)


def test_non_json_body_is_422(path, init_ti, emitted, config_changes):
    init_ti()
    response = kiosk().post(URL, data="x", content_type="text/plain")
    assert response.status_code == 422
    assert response.get_json()["error"] == "invalid_payload"


def test_save_failure_is_500_and_changes_nothing(
    path, init_ti, emitted, config_changes, monkeypatch
):
    init_ti()

    def fail(*_args):
        raise OSError("read-only")

    monkeypatch.setattr(server_module, "save_phone_tilt_calibration", fail)
    response = kiosk().post(URL, json=body(case_tilt=1.0))
    assert response.status_code == 500
    assert response.get_json()["error"] == "save_failed"
    _assert_unchanged(path, emitted, config_changes)


def test_post_without_ti_is_409(path, monkeypatch, emitted):
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    response = kiosk().post(URL, json=body())
    assert response.status_code == 409
    assert response.get_json()["error"] == "iwr6843_not_enabled"
    assert not path.exists()


# --- DELETE ----------------------------------------------------------------------------


def test_delete_falls_back_to_cli_immediately(path, init_ti, emitted, config_changes, monkeypatch):
    saved_record(path, case_tilt=1.0, pitch=1.4)
    init_ti()
    assert configured_tilt() == pytest.approx(9.6)

    response = kiosk().delete(URL)

    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "removed" and data["removed"] is True
    assert data["tilt_source"] == "cli"
    assert data["configured_iwr_tilt_deg"] == pytest.approx(10.0)
    assert data["calibration"] is None
    assert configured_tilt() == pytest.approx(10.0)
    assert not path.exists()
    [(config, source)] = config_changes
    assert source == "phone_tilt_calibration_removed"
    assert config["iwr6843"]["tilt_source"] == "cli"
    [(name, payload)] = emitted
    assert name == "iwr6843_tilt_calibration" and payload["event"] == "removed"


def test_delete_falls_back_to_calibration_json(path, init_ti, emitted, config_changes):
    saved_record(path)
    init_ti(tilt_deg=None)
    kiosk().delete(URL)
    assert configured_tilt() == pytest.approx(10.405)
    assert server_module.iwr6843_runtime_config["tilt_source"] == "calibration_json"


def test_delete_without_saved_file_is_idempotent(path, init_ti, emitted, config_changes):
    init_ti()
    response = kiosk().delete(URL)
    assert response.status_code == 200
    assert response.get_json()["removed"] is False
    assert configured_tilt() == pytest.approx(10.0)


def test_delete_without_ti_still_removes_the_file(path, emitted, config_changes, monkeypatch):
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    saved_record(path)
    response = kiosk().delete(URL)
    assert response.status_code == 200
    assert response.get_json()["removed"] is True
    assert not path.exists()
    assert config_changes == []


# --- GET and socket ----------------------------------------------------------------------


def test_get_status_reports_source_and_last_effective_tilt(path, init_ti, monkeypatch):
    saved_record(path, case_tilt=1.0, pitch=1.4)
    init_ti()
    first = kiosk().get(URL)
    assert first.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    data = first.get_json()
    assert data["enabled"] is True
    assert data["tilt_source"] == "phone"
    assert data["configured_iwr_tilt_deg"] == pytest.approx(9.6)
    assert data["case_top_to_boresight_deg"] == 10.0
    assert data["last_effective_iwr_tilt_deg"] is None
    assert data["calibration"]["radar_tilt_deg"] == pytest.approx(11.0)
    assert data["calibration"]["measured_at"] == "2026-10-30T10:00:00Z"

    server_module._record_effective_iwr_tilt(11.2)
    data = kiosk().get(URL).get_json()
    assert data["last_effective_iwr_tilt_deg"] == pytest.approx(11.2)
    assert data["last_effective_at"]

    server_module._record_effective_iwr_tilt(None)
    assert kiosk().get(URL).get_json()["last_effective_iwr_tilt_deg"] == pytest.approx(9.6)


def test_get_without_ti_shows_the_saved_file(path, monkeypatch):
    monkeypatch.setattr(server_module, "iwr6843_runtime", None)
    monkeypatch.setattr(server_module, "iwr6843_runtime_config", {"enabled": False})
    saved_record(path)
    data = kiosk().get(URL).get_json()
    assert data["enabled"] is False
    assert data["configured_iwr_tilt_deg"] is None
    assert data["tilt_source"] is None
    assert data["calibration"]["radar_tilt_deg"] == pytest.approx(11.0)


def test_socket_request_broadcasts_the_state(path, init_ti, emitted):
    init_ti()
    server_module.handle_get_iwr6843_tilt_calibration()
    [(name, payload)] = emitted
    assert name == "iwr6843_tilt_calibration"
    assert payload["event"] == "state"
    assert payload["tilt_source"] == "cli"


# --- auth --------------------------------------------------------------------------------


@pytest.fixture(name="pairing_state")
def fixture_pairing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module, "device_secret", DeviceSecret(tmp_path / "secret"))
    monkeypatch.setattr(server_module, "pairing_codes", PairingCodes())
    monkeypatch.setattr(server_module, "_paired_socket_sids", set())


def _remote():
    client = server_module.app.test_client()
    client.environ_base["REMOTE_ADDR"] = PHONE_ADDR
    return client


@pytest.mark.parametrize("method", ["get", "post", "delete"])
def test_unpaired_remote_device_is_refused(path, init_ti, pairing_state, emitted, method):
    saved_record(path)
    init_ti()
    response = getattr(_remote(), method)(URL, json=body(case_tilt=2.0))
    assert response.status_code == 401
    assert response.get_json()["error"] == "pairing_required"
    assert configured_tilt() == pytest.approx(9.6)
    assert path.exists()


def test_paired_device_can_calibrate(path, init_ti, pairing_state, emitted, config_changes):
    init_ti()
    code = kiosk().post("/api/pairing/code").get_json()["code"]
    phone = _remote()
    assert phone.post("/api/pairing/exchange", json={"code": code}).status_code == 200
    response = phone.post(URL, json=body(case_tilt=2.0))
    assert response.status_code == 200
    assert configured_tilt() == pytest.approx(12.0)


def test_foreign_origin_is_refused(path, init_ti, emitted):
    init_ti()
    response = kiosk().post(URL, json=body(), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert configured_tilt() == pytest.approx(10.0)


# --- inclinometer state mismatch -----------------------------------------------------------


@pytest.mark.parametrize(
    ("pitch", "active", "warns"),
    [(1.4, True, False), (None, False, False), (1.4, False, True), (None, True, True)],
)
def test_mismatch_warning(path, init_ti, capsys, pitch, active, warns):
    saved_record(path, pitch=pitch)
    init_ti()
    capsys.readouterr()
    server_module._warn_if_phone_tilt_inclinometer_mismatch(active)
    assert ("recalibrate the TI tilt" in capsys.readouterr().out) is warns
