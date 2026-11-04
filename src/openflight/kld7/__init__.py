"""K-LD7 angle radar integration module.

.. deprecated::
    The K-LD7 angle radars are deprecated — OpenFlight has moved to a more
    capable radar chip for angle measurement. This module is kept for
    existing builds but will not receive further development.
"""

import warnings

from .tracker import KLD7Tracker
from .types import KLD7Angle, KLD7Frame

warnings.warn(
    "The K-LD7 angle radar is deprecated; OpenFlight has moved to a more "
    "capable radar chip. K-LD7 support is kept for existing builds only.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["KLD7Angle", "KLD7Frame", "KLD7Tracker"]
