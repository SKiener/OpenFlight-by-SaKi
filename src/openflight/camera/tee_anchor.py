"""Session tee anchor for the camera trigger gate.

The live gate (``openflight.trigger_gate``) blocks a sound edge when the ball at
this anchor is still there, so a wrong anchor blocks real strikes. On
2026-10-25 the old anchor (any blob the check found) slid onto a dark tree
crown on the horizon and stayed there for 33 minutes; a live gate would have
blocked 45 of 69 strikes. The rules now:

* Only a spot seen leaving on a shot the OPS accepted
  (``TriggerCheck.departure_*``, found by the hint, ball-mark or hitting-zone
  search, see ``openflight.camera.trigger_check``) establishes, confirms or
  moves the anchor. Static blobs, clips without an accepted shot and full-frame
  detections never do.
* The first such departure establishes the anchor (the observation check
  searches there next), each departure within ``match_radius_px`` confirms
  it, and ``RELOCATION_SAMPLES`` agreeing departures elsewhere move it.
* ``gate_anchor`` is offered to the gate only once the anchor has
  ``CONFIRMATIONS_TO_BLOCK`` confirmations. An OPS-accepted shot whose check
  saw the ball at the anchor ``still_there`` proves the anchor wrong (or the
  shot a phantom): it is logged, counted in ``diagnostics`` and the
  confirmations restart at zero, so the gate pulses every edge until three new
  departures confirm the anchor.
* Each departure keeps its signed contrast (``TriggerCheck.departure_contrast``
  at ``exposure_gain``, normalised so exposure changes keep it valid); the
  gate anchor (an ``AnchorBall``) carries their median, and the live gate
  judges only a spot that matches it. A departure without contrast or
  exposure is no evidence.
* A sound edge the gate blocked never reaches the OPS, so it cannot confirm
  or move the anchor. But if its clip shows a ball leaving away from the spot
  the check judged at the anchor (``observe_blocked_edge``), the block was
  wrong and the anchor stale: the confirmations restart at zero exactly as for
  an OPS contradiction, so a stale anchor costs one strike, not every strike
  from the new spot. The departure is compared with the judged spot, not the
  anchor: a static spot inside the match radius (a spare ball 15 px beside the
  tee) otherwise kept every blocked strike "at the anchor".
* Camera restarts (exposure changes) keep the anchor; ``reset`` is for image
  geometry changes only.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Mapping

import numpy as np

from openflight.camera.club_motion import ReferenceBall
from openflight.camera.trigger_check import (
    ANCHOR_SOURCES,
    AnchorBall,
    TriggerCheck,
    match_radius_px,
    normalised_contrast,
)

logger = logging.getLogger(__name__)

CONFIRMATIONS_TO_BLOCK = 3
RELOCATION_SAMPLES = 3
# The anchor is the median of the latest departures, so a change of tee height
# (iron off the mat vs. a teed driver, 15 px on 2026-10-25) moves it within a
# few shots.
ANCHOR_SAMPLES = 5
SIZE_RATIO_RANGE = (0.65, 1.55)


def _spot(fields: Mapping, prefix: str) -> ReferenceBall | None:
    x, y = fields.get(f"{prefix}_x"), fields.get(f"{prefix}_y")
    diameter = fields.get(f"{prefix}_diameter_px")
    if x is None or y is None or not diameter:
        return None
    return ReferenceBall(
        float(x), float(y), float(diameter), round(math.pi * (float(diameter) / 2.0) ** 2)
    )


def _departure(fields: Mapping) -> AnchorBall | None:
    """The spot seen leaving (``ANCHOR_SOURCES``) with its normalised contrast, else None."""
    spot = _spot(fields, "departure") if fields.get("departure_source") in ANCHOR_SOURCES else None
    raw = fields.get("departure_contrast")
    exposure_gain = fields.get("exposure_gain")
    if spot is None or raw is None or exposure_gain is None:
        return None
    contrast = normalised_contrast(float(raw), float(exposure_gain))
    if contrast is None:
        return None
    return AnchorBall(spot.x, spot.y, spot.diameter_px, spot.area_px, contrast=contrast)


def _median(samples: list[AnchorBall]) -> AnchorBall | None:
    if not samples:
        return None
    return AnchorBall(
        x=float(np.median([ball.x for ball in samples])),
        y=float(np.median([ball.y for ball in samples])),
        diameter_px=float(np.median([ball.diameter_px for ball in samples])),
        area_px=int(round(np.median([ball.area_px for ball in samples]))),
        contrast=float(np.median([ball.contrast for ball in samples])),
    )


def agrees(spot: ReferenceBall, anchor: ReferenceBall) -> bool:
    """Whether ``spot`` is the ball at ``anchor`` (re-teed within the match radius)."""
    distance = math.hypot(spot.x - anchor.x, spot.y - anchor.y)
    ratio = spot.diameter_px / anchor.diameter_px
    return (
        distance <= match_radius_px(anchor.diameter_px)
        and SIZE_RATIO_RANGE[0] <= ratio <= SIZE_RATIO_RANGE[1]
    )


def _point(ball: ReferenceBall | None) -> dict | None:
    if ball is None:
        return None
    point = {
        "x": round(ball.x, 1),
        "y": round(ball.y, 1),
        "diameter_px": round(ball.diameter_px, 1),
    }
    if isinstance(ball, AnchorBall) and ball.contrast is not None:
        point["contrast"] = round(ball.contrast, 1)
    return point


class TeeAnchor:  # pylint: disable=too-many-instance-attributes
    """Thread-safe session tee anchor fed by OPS-accepted shots (see the module docstring)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: list[AnchorBall] = []
        self._relocation: list[AnchorBall] = []
        self._confirmations = 0
        self._contradicted = False
        self._contradictions = 0
        self._blocked_contradictions = 0
        self._relocations = 0
        self._last_event: dict | None = None
        self._last_contradiction: dict | None = None

    def hint(self) -> AnchorBall | None:
        """Where the observation check searches first: the anchor, confirmed or not."""
        with self._lock:
            return _median(self._samples)

    def gate_anchor(self) -> AnchorBall | None:
        """The anchor (with its ball's contrast) the live gate may block on, else None."""
        with self._lock:
            if self._confirmations < CONFIRMATIONS_TO_BLOCK:
                return None
            return _median(self._samples)

    def observe_shot(self, check: TriggerCheck | Mapping | None, *, shot=None) -> dict:
        """Learn from the trigger check of a clip the OPS accepted as a shot.

        Returns the event: ``kind`` is ``established``, ``confirmed``,
        ``relocation_pending``, ``relocated`` or ``no_departure``;
        ``contradicted`` is True when the ball stayed at the anchor.
        """
        fields = check.to_dict() if isinstance(check, TriggerCheck) else dict(check or {})
        with self._lock:
            event = self._observe_locked(fields, shot)
            self._last_event = event
        self._log(event)
        return event

    def _observe_locked(self, fields: dict, shot) -> dict:
        anchor = _median(self._samples)
        located = _spot(fields, "ball")
        event = {
            "shot": shot,
            "decision": fields.get("decision"),
            "reason": fields.get("reason"),
            "contradicted": False,
        }
        if (
            anchor is not None
            and fields.get("decision") == "still_there"
            and fields.get("ball_source") == "hint"
            and located is not None
            and agrees(located, anchor)
        ):
            self._contradict(
                {"kind": "ops_shot_still_there", "shot": shot, "reason": fields.get("reason")},
                anchor,
            )
            event["contradicted"] = True
        departure = _departure(fields)
        event["kind"] = "no_departure" if departure is None else self._learn(departure, anchor)
        if self._confirmations >= CONFIRMATIONS_TO_BLOCK:
            self._contradicted = False
        event.update(
            departure=_point(departure),
            anchor=_point(_median(self._samples)),
            confirmations=self._confirmations,
            blocking_enabled=self._confirmations >= CONFIRMATIONS_TO_BLOCK,
        )
        return event

    def _contradict(self, details: dict, anchor: AnchorBall) -> None:
        """The anchor was proven wrong: blocking stops until it is confirmed again."""
        self._confirmations = 0
        self._contradicted = True
        self._contradictions += 1
        self._last_contradiction = {**details, **_point(anchor)}

    def observe_blocked_edge(
        self, check: TriggerCheck | Mapping | None, *, edge=None
    ) -> dict | None:
        """Learn from the trigger check of a sound edge the camera gate blocked.

        Returns the event when the clip shows a ball leaving away from the
        spot the check judged (``ball_*`` with ``ball_source`` ``hint``: more
        than half a ball diameter from it), or away from the anchor when the
        check judged no spot there: the block was wrong and the anchor is
        contradicted (see the module docstring). A departure at the judged
        spot itself (the ball left after the gate decided) keeps the anchor;
        None. The spot is no relocation sample: only OPS-accepted shots move
        the anchor.
        """
        fields = check.to_dict() if isinstance(check, TriggerCheck) else dict(check or {})
        departure = _departure(fields)
        judged = _spot(fields, "ball") if fields.get("ball_source") == "hint" else None
        with self._lock:
            anchor = _median(self._samples)
            if anchor is None or departure is None:
                return None
            if judged is not None:
                at_spot = math.hypot(departure.x - judged.x, departure.y - judged.y) <= (
                    judged.diameter_px / 2.0
                )
            else:
                at_spot = agrees(departure, anchor)
            if at_spot:
                spot = anchor if judged is None else judged
                logger.info(
                    "[CAMERA] Blocked edge %s: the ball left at %.0f,%.0f, at the %s %.0f,%.0f, "
                    "after the gate decided (anchor kept)",
                    "?" if edge is None else f"{edge:.3f}",
                    departure.x,
                    departure.y,
                    "tee anchor" if judged is None else "judged spot",
                    spot.x,
                    spot.y,
                )
                return None
            self._blocked_contradictions += 1
            self._contradict({"kind": "blocked_departure", "edge": edge}, anchor)
            event = {
                "kind": "blocked_departure",
                "edge": edge,
                "decision": fields.get("decision"),
                "reason": fields.get("reason"),
                "contradicted": True,
                "departure": _point(departure),
                "anchor": _point(anchor),
                "confirmations": 0,
                "blocking_enabled": False,
            }
            self._last_event = event
        logger.warning(
            "[CAMERA] Tee anchor contradicted: the gate blocked edge %s but a ball left at "
            "%.0f,%.0f, away from the anchor %.0f,%.0f; the gate pulses every edge until %d "
            "OPS shots confirm or move the anchor",
            "?" if edge is None else f"{edge:.3f}",
            departure.x,
            departure.y,
            anchor.x,
            anchor.y,
            CONFIRMATIONS_TO_BLOCK,
        )
        return event

    def _learn(self, departure: AnchorBall, anchor: AnchorBall | None) -> str:
        if anchor is None:
            self._samples = [departure]
            self._confirmations = 1
            return "established"
        if agrees(departure, anchor):
            self._samples = (self._samples + [departure])[-ANCHOR_SAMPLES:]
            self._relocation = []
            self._confirmations += 1
            return "confirmed"
        pending = _median(self._relocation)
        if pending is not None and not agrees(departure, pending):
            self._relocation = []
        self._relocation.append(departure)
        if len(self._relocation) < RELOCATION_SAMPLES:
            return "relocation_pending"
        self._samples = self._relocation[-ANCHOR_SAMPLES:]
        self._confirmations = len(self._relocation)
        self._relocation = []
        self._relocations += 1
        return "relocated"

    @staticmethod
    def _log(event: dict) -> None:
        shot = "?" if event["shot"] is None else event["shot"]
        anchor = event["anchor"]
        where = "-" if anchor is None else f"{anchor['x']:.0f},{anchor['y']:.0f}"
        if event["contradicted"]:
            logger.warning(
                "[CAMERA] Tee anchor contradicted: OPS shot %s left the ball %s at %s (%s); "
                "the gate pulses every edge until %d departures confirm the anchor",
                shot,
                event["decision"],
                where,
                event["reason"],
                CONFIRMATIONS_TO_BLOCK,
            )
        if event["kind"] == "no_departure":
            logger.info(
                "[CAMERA] Tee anchor: shot %s showed no departing ball (%s/%s)",
                shot,
                event["decision"],
                event["reason"],
            )
            return
        departure = event["departure"]
        logger.info(
            "[CAMERA] Tee anchor %s by shot %s (ball left %.0f,%.0f): anchor %s, "
            "%d confirmation(s), gate blocking %s",
            event["kind"].replace("_", " "),
            shot,
            departure["x"],
            departure["y"],
            where,
            event["confirmations"],
            "enabled" if event["blocking_enabled"] else "disabled",
        )

    def reset(self, reason: str) -> None:
        """Forget the anchor (image geometry changed); the counters stay for the session."""
        with self._lock:
            had_anchor = bool(self._samples)
            self._samples, self._relocation = [], []
            self._confirmations = 0
            self._contradicted = False
            self._last_event = {"kind": "reset", "reason": reason}
        if had_anchor:
            logger.info("[CAMERA] Tee anchor reset: %s", reason)

    def diagnostics(self) -> dict:
        """JSON-ready state for the camera status and the session log."""
        with self._lock:
            anchor = _median(self._samples)
            if anchor is None:
                state = "none"
            elif self._confirmations >= CONFIRMATIONS_TO_BLOCK:
                state = "confirmed"
            elif self._contradicted:
                state = "contradicted"
            else:
                state = "unconfirmed"
            return {
                "state": state,
                "anchor": _point(anchor),
                "confirmations": self._confirmations,
                "confirmations_to_block": CONFIRMATIONS_TO_BLOCK,
                "blocking_enabled": self._confirmations >= CONFIRMATIONS_TO_BLOCK,
                "contradictions": self._contradictions,
                "blocked_contradictions": self._blocked_contradictions,
                "last_contradiction": self._last_contradiction,
                "relocations": self._relocations,
                "relocation_pending": len(self._relocation),
                "last_event": self._last_event,
            }
