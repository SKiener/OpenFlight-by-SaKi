"""The spin replay tools pair each shot with its raw capture.

Since a ``no_ball_track`` rejection also writes a ``rolling_buffer_capture``
(with ``shot_number`` null and no ``shot_detected``), a positional
``zip(shots, captures)`` would shift every later pairing by one. The shared
session loader must drop those shot-less captures.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts" / "analysis"))

import experiment_spin_phase  # noqa: E402  pylint: disable=wrong-import-position
import experiment_spin_windows  # noqa: E402  pylint: disable=wrong-import-position
import replay_spin_candidates  # noqa: E402  pylint: disable=wrong-import-position
import replay_spin_dechirp  # noqa: E402  pylint: disable=wrong-import-position

TOOLS = [
    experiment_spin_windows,
    experiment_spin_phase,
    replay_spin_candidates,
    replay_spin_dechirp,
]


def _write_session(path: Path) -> None:
    entries = [
        {"type": "shot_detected", "data": {"shot_number": 1}},
        {"type": "rolling_buffer_capture", "shot_number": 1, "trigger_time": 1.0},
        # A no_ball_track rejection: raw capture kept, but no shot.
        {
            "type": "rolling_buffer_capture",
            "shot_number": None,
            "rejection_reason": "no_ball_track",
            "trigger_time": 2.0,
        },
        {"type": "shot_detected", "data": {"shot_number": 2}},
        {"type": "rolling_buffer_capture", "shot_number": 2, "trigger_time": 3.0},
    ]
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


@pytest.mark.parametrize("tool", TOOLS, ids=lambda module: module.__name__)
def test_rejected_capture_does_not_shift_the_shot_pairing(tool, tmp_path):
    session = tmp_path / "session.jsonl"
    _write_session(session)

    shots, captures = tool._load_session_entries(session)  # pylint: disable=protected-access

    pairs = [
        (shot["data"]["shot_number"], capture["shot_number"])
        for shot, capture in zip(shots, captures)
    ]
    assert pairs == [(1, 1), (2, 2)]
    assert [capture["trigger_time"] for capture in captures] == [1.0, 3.0]


def test_replay_tools_share_one_session_loader():
    loaders = {tool._load_session_entries for tool in TOOLS}  # pylint: disable=protected-access
    assert loaders == {experiment_spin_windows._load_session_entries}  # pylint: disable=protected-access
