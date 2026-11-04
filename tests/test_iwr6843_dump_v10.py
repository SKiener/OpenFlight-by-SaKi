"""Dump version 10 (firmware v1.0.3+: version 7 plus the board's clutter map).

The synthetic headers below follow upstream ``firmware/iwr6843/dump_format.h``:
the clutter report (12 bytes) and its three columns sit between the
temperature report and the per-frame descriptors.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from openflight.iwr6843.driver import _expected_dump_size
from openflight.iwr6843.dump import (
    CLUTTER_MAP_MAX_BINS,
    CLUTTER_REPORT,
    DUMP_VERSION_CLUTTER,
    HEADER,
    SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
    TEMP_REPORT,
    TEMP_REPORT_KEYS,
    TIMED_FRAME_DESCRIPTOR,
    pack_dump,
    parse_dump,
    parse_header,
    payload_nbytes,
    project_tx_pair,
)

N_FRAMES = 4
N_TX = 3
N_LOOPS = 2
N_RX = 4
N_BINS = 8
TEMPERATURE = {key: 30 + index for index, key in enumerate(TEMP_REPORT_KEYS)}
CLUTTER = {
    "first_bin": 28,
    "count": 3,
    "updates": 517,
    "avg": (1.5, 2.25, 3.0),
    "dev": (0.5, 0.25, 0.125),
    "seen": (8, 9, 255),
}


def _cube(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = (N_FRAMES, N_TX * N_LOOPS, N_RX, N_BINS)
    return np.round(rng.normal(0, 300, shape)) + 1j * np.round(rng.normal(0, 300, shape))


def _timed_kwargs() -> dict:
    return {
        "n_tx": N_TX,
        "trigger_frame": 1,
        "frame_period_us": 3000,
        "sample_fmt": SAMPLE_RANGE_FFT_IQ16_VARIABLE_TIMED,
        "range_bin_starts": (20, 20, 32, 32),
        "range_bin_counts": (N_BINS, N_BINS, 6, N_BINS),
        "frame_time_offsets_us": (0, 3000, 6000, 9000),
        "temperature_report": TEMPERATURE,
    }


def _v7(cube: np.ndarray) -> bytes:
    return pack_dump(cube, version=7, **_timed_kwargs())


def _v10(cube: np.ndarray, *, clutter: dict = CLUTTER, window: str = "none") -> bytes:
    return pack_dump(
        cube,
        version=DUMP_VERSION_CLUTTER,
        clutter_map=clutter,
        range_window=window,
        **_timed_kwargs(),
    )


def _clutter_nbytes(count: int) -> int:
    return CLUTTER_REPORT.size + count * (4 + 4 + 1)


def test_v10_is_v7_with_the_clutter_block_after_the_temperature_report():
    cube = _cube()
    v7, v10 = _v7(cube), _v10(cube)
    prefix = HEADER.size + TEMP_REPORT.size
    block = _clutter_nbytes(CLUTTER["count"])

    assert len(v10) == len(v7) + block
    # Header bytes differ only in the version field; the temperature report is identical.
    assert v10[:4] == v7[:4] and v10[6 : HEADER.size] == v7[6 : HEADER.size]
    assert struct.unpack_from("<H", v10, 4)[0] == DUMP_VERSION_CLUTTER
    assert v10[HEADER.size : prefix] == v7[HEADER.size : prefix]
    # Descriptors and payload follow the clutter block unchanged.
    assert v10[prefix + block :] == v7[prefix:]
    first_bin, count, updates, window = CLUTTER_REPORT.unpack_from(v10, prefix)
    assert (first_bin, count, updates, window) == (28, 3, 517, 0)


def test_v10_header_parses_clutter_map_and_range_window():
    meta = parse_header(_v10(_cube(), window="hann"))

    assert meta["version"] == DUMP_VERSION_CLUTTER
    assert meta["temperature_report"] == TEMPERATURE
    assert meta["range_window"] == "hann"
    assert meta["clutter_map"]["first_bin"] == 28
    assert meta["clutter_map"]["updates"] == 517
    assert meta["clutter_map"]["avg"] == pytest.approx(CLUTTER["avg"])
    assert meta["clutter_map"]["dev"] == pytest.approx(CLUTTER["dev"])
    assert meta["clutter_map"]["seen"] == CLUTTER["seen"]
    assert meta["header_nbytes"] == HEADER.size + TEMP_REPORT.size + _clutter_nbytes(3)


def test_v10_payload_equals_v7_payload():
    cube = _cube(3)
    meta7, cube7 = parse_dump(_v7(cube))
    meta10, cube10 = parse_dump(_v10(cube))

    np.testing.assert_array_equal(cube10, cube7)
    for key in ("range_bin_starts", "range_bin_counts", "frame_time_offsets_us", "n_frames"):
        assert meta10[key] == meta7[key]
    assert "clutter_map" not in meta7 and "range_window" not in meta7
    assert meta7["header_nbytes"] == HEADER.size + TEMP_REPORT.size


def test_v10_size_from_header_matches_the_wire_length():
    raw = _v10(_cube())
    meta = parse_header(raw)
    assert meta["header_nbytes"] + payload_nbytes(meta, raw) == len(raw)
    # The driver sizes the read from the header behind the CLI echo.
    buf = bytearray(b"l3dump\r\n" + raw)
    assert _expected_dump_size(buf) == len(raw)
    assert bytes(buf) == raw


def test_v10_with_an_empty_clutter_map():
    empty = {"first_bin": 0, "count": 0, "updates": 0, "avg": (), "dev": (), "seen": ()}
    cube = _cube()
    raw = _v10(cube, clutter=empty)
    meta, parsed = parse_dump(raw)
    assert meta["clutter_map"]["count"] == 0
    assert len(raw) == len(_v7(cube)) + CLUTTER_REPORT.size
    np.testing.assert_array_equal(parsed, parse_dump(_v7(cube))[1])


def test_v10_survives_tx_projection():
    raw = _v10(_cube(5), window="hann")
    projected = project_tx_pair(raw, (0, 2))
    meta = parse_header(projected)

    assert meta["version"] == DUMP_VERSION_CLUTTER
    assert meta["n_tx"] == 2
    assert meta["range_window"] == "hann"
    assert meta["clutter_map"]["seen"] == CLUTTER["seen"]
    _, cube = parse_dump(projected)
    assert cube.shape[1] == 2 * N_LOOPS


def test_v7_and_older_versions_still_parse():
    cube = _cube()
    assert parse_header(_v7(cube))["version"] == 7
    raw3 = pack_dump(np.zeros((2, 4, 4, 8), dtype=complex), n_tx=2, version=3)
    assert parse_header(raw3)["version"] == 3


@pytest.mark.parametrize("version", [8, 9, 11])
def test_unsupported_versions_are_refused(version):
    raw = bytearray(_v7(_cube()))
    raw[4:6] = version.to_bytes(2, "little")
    with pytest.raises(ValueError, match="unsupported dump version"):
        parse_header(bytes(raw))


def test_short_clutter_extension_is_refused():
    raw = _v10(_cube())
    cut = HEADER.size + TEMP_REPORT.size + CLUTTER_REPORT.size + 5
    with pytest.raises(ValueError, match="short clutter map extension"):
        parse_header(raw[:cut])
    with pytest.raises(ValueError, match="short clutter map extension"):
        parse_header(raw[: HEADER.size + TEMP_REPORT.size + 4])


def test_bad_clutter_report_fields_are_refused():
    raw = bytearray(_v10(_cube()))
    at = HEADER.size + TEMP_REPORT.size
    bad_window = bytearray(raw)
    bad_window[at + 8] = 7
    with pytest.raises(ValueError, match="unknown range window"):
        parse_header(bytes(bad_window))
    too_many = bytearray(raw)
    struct.pack_into("<H", too_many, at + 2, CLUTTER_MAP_MAX_BINS + 1)
    with pytest.raises(ValueError, match="invalid clutter map"):
        parse_header(bytes(too_many))


def test_packing_rules_for_the_clutter_block():
    cube = _cube()
    with pytest.raises(ValueError, match="requires a clutter map"):
        pack_dump(cube, version=DUMP_VERSION_CLUTTER, **_timed_kwargs())
    with pytest.raises(ValueError, match="require dump version 10"):
        pack_dump(cube, version=7, clutter_map=CLUTTER, **_timed_kwargs())
    with pytest.raises(ValueError, match="unknown range window"):
        _v10(cube, window="blackman")
    with pytest.raises(ValueError, match="dump version 5\\+ requires a temperature report"):
        kwargs = _timed_kwargs()
        kwargs.pop("temperature_report")
        pack_dump(cube, version=DUMP_VERSION_CLUTTER, clutter_map=CLUTTER, **kwargs)


def test_descriptor_offset_moves_past_the_clutter_block():
    raw = _v10(_cube())
    meta = parse_header(raw)
    starts = [
        TIMED_FRAME_DESCRIPTOR.unpack_from(raw, meta["header_nbytes"] + 4 * frame)[0]
        for frame in range(N_FRAMES)
    ]
    assert starts == [20, 20, 32, 32]
