"""Array and geometry calibration for the IWR6843 elevation array.

The checked-in reference was produced with a corner reflector at three
tape-measured positions. It is a validated starting point, not a universal
factory calibration. Conventions:

- Element corrections apply AFTER the physical-orientation flip (x8[::-1]).
- ``tilt_rad`` rotates measured angles into ground coordinates.
- ``range_bias_m`` subtracts from measured range (radar-face referenced).

Re-run array calibration whenever the board, enclosure, or antenna orientation
changes, and measure installation geometry whenever the mount moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

DEFAULT_CAL_PATH = "config/iwr6843_calibration_reference.json"


@dataclass
class Calibration:
    """Loaded calibration constants, ready to apply to snapshots."""

    elem_correction: np.ndarray  # complex, len n_virtual elements
    tilt_rad: float
    range_bias_m: float
    source: str = "unset"
    tee_range_m: float | None = None  # tape-measured launch point (slant)
    # Ball height at launch ABOVE THE FLOOR (ball on mat ~0.04 m). The old
    # field meant "above the radar plane" and silently anchored every tee
    # fit ~0.15 m high on a 0.152 m mount: -0.15/lever-arm of slope bias
    # (SW read -12 deg). Floor-referenced + radar height fixes the anchor.
    tee_ball_height_m: float = 0.04
    meta: dict = field(default_factory=dict)

    @property
    def radar_height_m(self) -> float:
        """Antenna center height above the floor (from the cal solve)."""
        return float(self.meta.get("radar_height_m", 0.152))

    @property
    def tee_anchor_h_m(self) -> float:
        """Tee anchor height in radar-plane coordinates (h=0 at radar)."""
        return self.tee_ball_height_m - self.radar_height_m

    @classmethod
    def load(cls, path: str = DEFAULT_CAL_PATH) -> "Calibration":
        """Load a cal JSON written by the corner-reflector solve."""
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        corr = np.exp(-1j * np.asarray(raw["elem_phase_rad"])) / np.asarray(raw["elem_gain"])
        return cls(
            elem_correction=corr,
            tilt_rad=float(np.radians(raw["tilt_deg"])),
            range_bias_m=float(raw["range_bias_const_m"]),
            source=path,
            meta=raw,
        )

    @classmethod
    def identity(cls, n_elements: int = 8) -> "Calibration":
        """No-op calibration (uncalibrated array, zero tilt/bias)."""
        return cls(
            elem_correction=np.ones(n_elements, dtype=complex),
            tilt_rad=0.0,
            range_bias_m=0.0,
            source="identity",
        )

    def apply(self, snapshot: np.ndarray) -> np.ndarray:
        """Correct a physical-order snapshot (post-flip) element-wise."""
        return snapshot * self.elem_correction

    def true_range(self, measured_m: float) -> float:
        """Bias-corrected range from a measured (apparent) range."""
        return measured_m - self.range_bias_m
