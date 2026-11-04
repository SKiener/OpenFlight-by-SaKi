"""Two-position phone tilt math, validation and persistence (phone_tilt.py)."""

import json
import math

import pytest

from openflight import phone_tilt
from openflight.phone_tilt import (
    CASE_TOP_TO_BORESIGHT_DEG,
    STANDARD_GRAVITY_MPS2 as G,
    PhoneTiltRejected,
    build_record,
    configured_tilt_deg,
    delete_calibration,
    load_calibration,
    measure_phone_tilt,
    pitch_deg,
    roll_deg,
    save_calibration,
)


def gravity(pitch=0.0, roll=0.0, g=G):
    """W3C accelerationIncludingGravity of a face-up phone, top raised by ``pitch``."""
    p, r = math.radians(pitch), math.radians(roll)
    return [g * math.sin(r), g * math.sin(p) * math.cos(r), g * math.cos(p) * math.cos(r)]


def position(pitch=0.0, roll=0.0, *, std=0.1, samples=60, g=G):
    return {"gravity": gravity(pitch, roll, g), "pitch_std_deg": std, "sample_count": samples}


def body(case_tilt=0.0, bias=0.0, *, case_roll=0.0, roll_bias=0.0, a=None, b=None, **extra):
    """A request for case tilt ``case_tilt`` measured by a phone with pitch bias ``bias``."""
    payload = {
        "position_a": a or position(case_tilt + bias, case_roll + roll_bias),
        "position_b": b or position(-case_tilt + bias, -case_roll + roll_bias),
        "measured_at": "2026-10-30T10:00:00Z",
    }
    payload.update(extra)
    return payload


def rejection(payload):
    with pytest.raises(PhoneTiltRejected) as caught:
        measure_phone_tilt(payload)
    return caught.value


# --- per-position angles -------------------------------------------------------


def test_flat_phone_reads_zero_pitch_and_roll():
    assert pitch_deg((0.0, 0.0, G)) == 0.0
    assert roll_deg((0.0, 0.0, G)) == 0.0


def test_raising_the_phone_top_is_positive_pitch():
    assert pitch_deg(tuple(gravity(pitch=2.0))) == pytest.approx(2.0)
    assert pitch_deg(tuple(gravity(pitch=-3.0))) == pytest.approx(-3.0)


def test_raising_the_right_edge_is_positive_roll():
    assert roll_deg(tuple(gravity(roll=1.5))) == pytest.approx(1.5)


# --- two-position result ---------------------------------------------------------


def test_level_case_gives_boresight_constant():
    result = measure_phone_tilt(body())
    assert result.case_tilt_deg == pytest.approx(0.0)
    assert result.radar_tilt_deg == pytest.approx(CASE_TOP_TO_BORESIGHT_DEG)
    assert CASE_TOP_TO_BORESIGHT_DEG == 10.0


def test_ball_side_up_two_degrees_is_plus_two():
    """Top end toward the ball raised +2 in A, so lowered 2 in B: theta = +2."""
    result = measure_phone_tilt(body(a=position(2.0), b=position(-2.0)))
    assert result.case_tilt_deg == pytest.approx(2.0)
    assert result.phone_bias_deg == pytest.approx(0.0)
    assert result.radar_tilt_deg == pytest.approx(12.0)


def test_ball_side_down_is_negative():
    result = measure_phone_tilt(body(case_tilt=-1.25))
    assert result.case_tilt_deg == pytest.approx(-1.25)
    assert result.radar_tilt_deg == pytest.approx(8.75)


def test_only_position_a_raised_splits_into_tilt_and_bias():
    result = measure_phone_tilt(body(a=position(2.0), b=position(0.0)))
    assert result.case_tilt_deg == pytest.approx(1.0)
    assert result.phone_bias_deg == pytest.approx(1.0)


@pytest.mark.parametrize("bias", [-2.5, 0.7, 2.9])
def test_phone_bias_cancels(bias):
    result = measure_phone_tilt(body(case_tilt=1.3, bias=bias))
    assert result.case_tilt_deg == pytest.approx(1.3)
    assert result.phone_bias_deg == pytest.approx(bias)


def test_side_roll_is_reported_and_its_bias_cancels():
    result = measure_phone_tilt(body(case_tilt=0.5, case_roll=1.0, roll_bias=0.4))
    assert result.case_roll_deg == pytest.approx(1.0, abs=1e-3)
    assert result.phone_roll_bias_deg == pytest.approx(0.4, abs=1e-3)
    assert result.case_tilt_deg == pytest.approx(0.5, abs=1e-3)


def test_inverted_convention_negates_the_vector_first():
    plain = measure_phone_tilt(body(case_tilt=1.5, bias=0.3))
    flipped_body = body(case_tilt=1.5, bias=0.3, convention="inverted")
    for key in ("position_a", "position_b"):
        flipped_body[key]["gravity"] = [-v for v in flipped_body[key]["gravity"]]
    flipped = measure_phone_tilt(flipped_body)
    assert flipped.convention == "inverted"
    assert flipped.case_tilt_deg == pytest.approx(plain.case_tilt_deg)
    assert flipped.position_a.gravity_mps2 == pytest.approx(plain.position_a.gravity_mps2)


def test_default_convention_is_w3c():
    assert measure_phone_tilt(body()).convention == "w3c"


def test_inverted_vector_without_flag_is_rejected():
    payload = body()
    payload["position_a"]["gravity"] = [-v for v in payload["position_a"]["gravity"]]
    error = rejection(payload)
    assert error.reason == "gravity_sign_inverted"
    assert error.position == "a"


# --- validation ------------------------------------------------------------------


def test_minimum_samples_std_and_tilt_at_the_limits_pass():
    payload = body(case_tilt=7.99, bias=2.99)
    payload["position_a"].update(sample_count=30, pitch_std_deg=0.5)
    measure_phone_tilt(payload)


@pytest.mark.parametrize(
    ("change", "reason", "where"),
    [
        (lambda p: p["position_a"].update(sample_count=29), "too_few_samples", "a"),
        (lambda p: p["position_b"].update(pitch_std_deg=0.51), "phone_unstable", "b"),
        (
            lambda p: p["position_a"].update(gravity=gravity(g=1.15 * G)),
            "gravity_magnitude_out_of_range",
            "a",
        ),
        (
            lambda p: p["position_a"].update(gravity=gravity(g=0.85 * G)),
            "gravity_magnitude_out_of_range",
            "a",
        ),
        # Sent in g instead of m/s^2.
        (
            lambda p: p["position_b"].update(gravity=[0.0, 0.0, 1.0]),
            "gravity_magnitude_out_of_range",
            "b",
        ),
        # Upright against a wall (upstream's procedure) instead of flat.
        (lambda p: p["position_a"].update(gravity=[0.0, G, 0.0]), "phone_not_flat", "a"),
        (lambda p: p["position_a"].update(gravity=gravity(pitch=26.0)), "phone_not_flat", "a"),
    ],
)
def test_per_position_rejections(change, reason, where):
    payload = body()
    change(payload)
    error = rejection(payload)
    assert error.reason == reason
    assert error.position == where
    assert error.to_dict()["error"] == reason


def test_case_tilt_above_eight_degrees_is_rejected():
    error = rejection(body(case_tilt=8.5))
    assert error.reason == "case_tilt_out_of_range"
    assert error.value == pytest.approx(8.5)


def test_large_phone_bias_is_rejected():
    error = rejection(body(case_tilt=1.0, bias=3.5))
    assert error.reason == "phone_bias_out_of_range"


def test_phone_not_turned_on_a_tilted_case_shows_as_bias():
    """B taken in the same orientation as A on a 4 deg case: pitches equal, bias 4."""
    error = rejection(body(a=position(4.0), b=position(4.0)))
    assert error.reason == "phone_bias_out_of_range"


def test_side_rolls_that_do_not_mirror_are_rejected():
    error = rejection(body(a=position(0.0, roll=3.5), b=position(0.0, roll=3.5)))
    assert error.reason == "positions_not_opposite"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"position_a": position(), "measured_at": "x"},
        body(measured_at=""),
        body(measured_at=12),
        body(convention="android"),
        body(client="iPhone"),
        body(a={"gravity": [0, 0], "pitch_std_deg": 0.1, "sample_count": 60}),
        body(a={"gravity": [0, 0, "9.8"], "pitch_std_deg": 0.1, "sample_count": 60}),
        body(a={"gravity": [0, 0, float("nan")], "pitch_std_deg": 0.1, "sample_count": 60}),
        body(a={"gravity": gravity(), "pitch_std_deg": 0.1, "sample_count": True}),
        body(a={"gravity": gravity(), "pitch_std_deg": 0.1, "sample_count": 60.0}),
        body(a={"gravity": gravity(), "pitch_std_deg": -0.1, "sample_count": 60}),
        body(a={"gravity": gravity(), "sample_count": 60}),
    ],
)
def test_malformed_payloads_are_invalid(payload):
    assert rejection(payload).reason == "invalid_payload"


def test_client_info_is_trimmed():
    payload = body(client={"user_agent": "x" * 500, "nested": {"a": 1}, "app": 3, "ok": True})
    client = measure_phone_tilt(payload).client
    assert len(client["user_agent"]) == 120
    assert "nested" not in client
    assert client["app"] == 3 and client["ok"] is True


# --- configured tilt and record ----------------------------------------------------


def test_configured_tilt_subtracts_the_inclinometer_pitch():
    assert configured_tilt_deg(12.0, 1.4) == pytest.approx(10.6)
    assert configured_tilt_deg(12.0, None) == pytest.approx(12.0)


def test_record_holds_cross_check_and_inputs():
    payload = body(case_tilt=1.0, bias=0.5)
    record = build_record(
        payload,
        measure_phone_tilt(payload),
        inclinometer_pitch_deg=1.4,
        applied_at="2026-10-30T11:00:00+01:00",
    )
    assert record["source"] == "phone"
    assert record["radar_tilt_deg"] == pytest.approx(11.0)
    assert record["configured_iwr_tilt_deg"] == pytest.approx(9.6)
    assert record["inclinometer_minus_phone_deg"] == pytest.approx(0.4)
    assert record["phone_bias_deg"] == pytest.approx(0.5)
    assert record["request"]["position_a"]["gravity"] == payload["position_a"]["gravity"]
    json.dumps(record)


def test_record_without_inclinometer_uses_radar_tilt():
    payload = body(case_tilt=-0.5)
    record = build_record(
        payload, measure_phone_tilt(payload), inclinometer_pitch_deg=None, applied_at="t"
    )
    assert record["configured_iwr_tilt_deg"] == pytest.approx(9.5)
    assert record["inclinometer_pitch_deg"] is None
    assert record["inclinometer_minus_phone_deg"] is None


# --- persistence --------------------------------------------------------------------


def _record(pitch=1.4):
    payload = body(case_tilt=1.0, bias=0.2, client={"device": "iPhone"})
    return build_record(
        payload, measure_phone_tilt(payload), inclinometer_pitch_deg=pitch, applied_at="t"
    )


def test_round_trip(tmp_path):
    path = tmp_path / "nested" / "phone.json"
    record = _record()
    save_calibration(record, path)
    assert load_calibration(path) == json.loads(json.dumps(record))
    assert [p.name for p in path.parent.iterdir()] == ["phone.json"]


def test_save_replaces_atomically(tmp_path, monkeypatch):
    path = tmp_path / "phone.json"
    save_calibration(_record(), path)
    before = path.read_text()

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(phone_tilt.os, "replace", fail)
    with pytest.raises(OSError):
        save_calibration(_record(pitch=0.0), path)
    assert path.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["phone.json"]


def test_missing_file_loads_as_none(tmp_path):
    assert load_calibration(tmp_path / "absent.json") is None


@pytest.mark.parametrize(
    "tamper",
    [
        lambda r: r.update(schema_version=2),
        lambda r: r.update(configured_iwr_tilt_deg=12.0),
        lambda r: r.update(configured_iwr_tilt_deg=60.0),
        lambda r: r["request"]["position_a"].update(sample_count=3),
        lambda r: r.pop("request"),
    ],
)
def test_inconsistent_files_are_rejected(tmp_path, tamper):
    record = _record()
    tamper(record)
    path = tmp_path / "phone.json"
    path.write_text(json.dumps(record))
    with pytest.raises(PhoneTiltRejected):
        load_calibration(path)


def test_corrupt_json_raises_value_error(tmp_path):
    path = tmp_path / "phone.json"
    path.write_text("{")
    with pytest.raises(ValueError):
        load_calibration(path)


def test_delete_reports_whether_a_file_existed(tmp_path):
    path = tmp_path / "phone.json"
    save_calibration(_record(), path)
    assert delete_calibration(path) is True
    assert delete_calibration(path) is False


def test_path_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(phone_tilt.PHONE_TILT_PATH_ENV, str(tmp_path / "x.json"))
    assert phone_tilt.calibration_path() == tmp_path / "x.json"
    monkeypatch.delenv(phone_tilt.PHONE_TILT_PATH_ENV)
    assert phone_tilt.calibration_path().name == "iwr6843_phone_orientation.json"
