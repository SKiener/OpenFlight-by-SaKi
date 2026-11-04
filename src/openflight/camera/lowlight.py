"""Low-light camera start direction, in shadow: relaxed ball gate plus flicker compensation.

Under floodlight (2026-10-30) the live camera start direction got 0/49: the
live ball gate (``capture_exposure_status``'s ``ball_analysis_eligible``)
blocks on ``motion_blur_risk == "high"`` (>= 800 us) and on ``too_dark``, and
the 100 Hz floodlight flicker made the disappeared-ball locator reject every
clip (``openflight.camera.flicker``). The offline replay
(analysis/lowlight-20261031) recovered 25/31 irons and 13/18 drivers with a
relaxed gate plus flicker compensation and no legacy fallback, and left
daylight unchanged.

This module is that path for the server's shadow log only; nothing here
reaches the shot, the screens or the simulators:

* ``lowlight_ball_gate`` admits a clip the live gate admits, and otherwise one
  that is merely dark: motion-blur risk is ignored (at 1250 us the ball smears
  only 7.5-9 px, about half a diameter) and the exposure only blocks when the
  hitting area is nearly black (p90 <= 12 grey and >= 90 % of it <= 12, the
  ``measure_exposure`` "nearly black" test). A flat clip (one grey level, no
  picture: ``frame_flat``) is never admitted. The club analysis gate is not
  touched.
* ``estimate_lowlight_ball_flight`` (the ``lowlight_ball_flight`` CV-worker job)
  compensates flicker when there is any (``flicker.compensate_ball_flicker``,
  the same step the live ball flight runs) and runs the live ball-flight
  estimator on the result. A clip admitted only by the relaxed gate runs the
  disappeared-ball locator without the legacy fallback: under floodlight the
  fallback's directions were +5 deg off, several with high confidence.

Since 2026-11-02 the live ball gate itself admits a merely dark clip
(``capture_exposure_status``'s ``ball_analysis_relaxed``) and runs it without
the legacy fallback too; the shadow treats such a clip as ``relaxed``, not
``live``, so it keeps running it the way it always did.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from openflight.camera.auto_exposure import (
    NEARLY_BLACK_DARK_PCT,
    NEARLY_BLACK_P90,
    nearly_black as _nearly_black_values,
)
from openflight.camera.flicker import (
    FLICKER_MIN_REL_STD,
    FlickerMeasurement,
    compensate_ball_flicker,
    flicker_roi_top,
)


@dataclass(frozen=True)
class LowLightGate:
    """Whether the low-light shadow path may run on a clip, and why.

    ``reason`` is ``live`` (the live ball gate admits the clip), ``relaxed`` (only
    the relaxed gate does), ``nearly_black``, ``unknown_controls`` (exposure or
    gain of a frame unknown) or ``no_exposure_metadata``.
    """

    eligible: bool
    reason: str

    @property
    def relaxed_only(self) -> bool:
        """Admitted by the relaxed gate alone: no legacy fallback for this clip."""
        return self.eligible and self.reason == "relaxed"


def known_controls(archive: dict) -> bool:
    """Every saved frame has a positive exposure and a finite positive gain.

    The same condition as ``capture_exposure_status``, read from the clip archive.
    """
    if "frames" not in archive or "exposure_us" not in archive or "analogue_gain" not in archive:
        return False
    exposure = np.asarray(archive["exposure_us"]).ravel()
    gain = np.asarray(archive["analogue_gain"], dtype=float).ravel()
    count = len(archive["frames"])
    if not count or len(exposure) != count or len(gain) != count:
        return False
    return bool(np.all(exposure > 0) and all(math.isfinite(g) and g > 0 for g in gain))


def nearly_black(observation: dict | None) -> bool:
    """The exposure observation shows a nearly black hitting area (or none at all).

    Not nearly black means p90 > 12 grey or fewer than 90 % of the area at or
    below 12. A missing p90 (no sample) or a missing dark share at a low p90
    counts as nearly black.
    """
    if not isinstance(observation, dict):
        return True
    return _nearly_black_values(observation.get("p90"), observation.get("dark_pct"))


def flat_frame(auto_exposure: dict | None) -> bool:
    """Whether the clip's saved exposure status reports a flat frame (no picture at all).

    ``frame_flat`` or a ``flat`` worst observation (``auto_exposure.measure_exposure``).
    """
    if not isinstance(auto_exposure, dict):
        return False
    observation = auto_exposure.get("observation")
    return auto_exposure.get("frame_flat") is True or (
        isinstance(observation, dict) and observation.get("status") == "flat"
    )


def relaxed_live_ball_gate(auto_exposure: dict | None) -> bool:
    """Whether the live gate admitted this clip only by its relaxed dark rule.

    ``auto_exposure`` is the clip's saved ``capture_exposure_status``; its
    ``ball_analysis_relaxed`` says so.
    """
    return isinstance(auto_exposure, dict) and auto_exposure.get("ball_analysis_relaxed") is True


def lowlight_ball_gate(
    auto_exposure: dict | None, *, live_ball_eligible: bool, controls_known: bool
) -> LowLightGate:
    """The relaxed ball gate of the shadow path; see the module docstring.

    ``auto_exposure`` is the clip's ``capture_exposure_status`` as saved in its
    metadata; ``live_ball_eligible`` whether the live shot path runs the ball
    flight on it; ``controls_known`` is ``known_controls`` of its archive. A
    clip the live gate admits by its own relaxed dark rule
    (``ball_analysis_relaxed``) is ``relaxed`` here as well: the shadow runs it
    without the legacy fallback, as before that rule went live. A flat clip
    (``flat_frame``: no picture, ``frame_flat``) is never admitted: at its one
    grey level (16 on 2026-11-02) it is not nearly black, so that test alone
    would let it through.
    """
    if live_ball_eligible and not relaxed_live_ball_gate(auto_exposure):
        return LowLightGate(True, "live")
    if not isinstance(auto_exposure, dict):
        return LowLightGate(False, "no_exposure_metadata")
    if not controls_known:
        return LowLightGate(False, "unknown_controls")
    if flat_frame(auto_exposure):
        return LowLightGate(False, "flat_frame")
    if nearly_black(auto_exposure.get("observation")):
        return LowLightGate(False, "nearly_black")
    return LowLightGate(True, "relaxed")


@dataclass(frozen=True)
class LowLightBallFlight:
    """The shadow ball flight of one clip: the estimate and the flicker step before it."""

    estimate: object  # camera.ball_flight.CameraBallEstimate
    flicker: FlickerMeasurement
    legacy_fallback: bool
    ball_locator: str
    elapsed_ms: float


def estimate_lowlight_ball_flight(
    *,
    frames: np.ndarray,
    relaxed_only: bool,
    ball_locator: str,
    tee_zone=None,
    flicker_min_rel_std: float = FLICKER_MIN_REL_STD,
    **ball_flight_kwargs,
) -> LowLightBallFlight:
    """Flicker-compensate the clip, then run ``estimate_camera_ball_flight`` on it.

    The flicker step is the live one (``flicker.compensate_ball_flicker``: the
    waveform below the hitting zone's top edge and in the ball window, from
    ``tee_zone`` and the ``ball_mark_px`` among the keyword arguments). ``relaxed_only``
    (the clip passed only the relaxed gate) forces the disappeared-ball locator
    without the legacy fallback; otherwise the live ``ball_locator`` runs with
    its fallback, exactly as live. The remaining keyword arguments go to the
    estimator unchanged (its ``ball_tracker`` and ``ground_anchor`` are updated
    in place: give it the shadow path's own).
    """
    from openflight.camera.ball_flight import (  # noqa: PLC0415  pylint: disable=import-outside-toplevel
        estimate_camera_ball_flight,
    )

    started = time.perf_counter()
    compensated, flicker = compensate_ball_flicker(
        frames,
        tee_zone=tee_zone,
        ball_mark_px=ball_flight_kwargs.get("ball_mark_px"),
        min_rel_std=flicker_min_rel_std,
    )
    locator = "disappeared" if relaxed_only else ball_locator
    estimate = estimate_camera_ball_flight(
        compensated,
        ball_locator=locator,
        legacy_fallback=not relaxed_only,
        tee_zone=tee_zone,
        **ball_flight_kwargs,
    )
    return LowLightBallFlight(
        estimate=estimate,
        flicker=flicker,
        legacy_fallback=not relaxed_only,
        ball_locator=locator,
        elapsed_ms=1e3 * (time.perf_counter() - started),
    )


__all__ = [
    "LowLightBallFlight",
    "LowLightGate",
    "estimate_lowlight_ball_flight",
    "flat_frame",
    "flicker_roi_top",
    "known_controls",
    "lowlight_ball_gate",
    "nearly_black",
    "relaxed_live_ball_gate",
    "NEARLY_BLACK_DARK_PCT",
    "NEARLY_BLACK_P90",
]
