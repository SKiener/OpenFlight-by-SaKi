"""IWR6843 angle-radar integration: capture, calibration, and shot processing.

OpenFlight combines the custom IWR6843 rolling capture with OPS243 shot timing
and speed measurements. The host pipeline decodes complex range snapshots and
estimates vertical and horizontal launch direction.
"""

from openflight.iwr6843.calibration import Calibration
from openflight.iwr6843.driver import IWR6843Radar
from openflight.iwr6843.lcmf import LCMFResult, estimate_lcmf_v1
from openflight.iwr6843.shot import ShotMeasurement, process_dump
from openflight.iwr6843.tracking import BallTrack, Geometry
from openflight.iwr6843.trajectory import TrajectoryFit

__all__ = [
    "BallTrack",
    "Calibration",
    "Geometry",
    "IWR6843Radar",
    "LCMFResult",
    "ShotMeasurement",
    "TrajectoryFit",
    "estimate_lcmf_v1",
    "process_dump",
]
