"""Hitting zone: where the teed ball can appear in the saved camera frames.

The camera trigger check searches for the ball only here. The zone is the
geometric tee projected through the calibrated lens: the ball sits
``tee_range_m`` (slant, from the TI antenna centre) ahead on the radar
centreline at ``ball_height_m``, the camera at its configured mount offsets.
The camera's pitch and yaw are not configured (the camera sits on a rocker, and
``--camera-capture-yaw-from-ball`` derives yaw from the ball), so the zone is
the bounding box of the ball centre over every combination of these margins,
plus half the largest ball diameter and ``PIXEL_MARGIN_PX``:

* pitch from ``PITCH_MARGIN_DEG[0]`` (up) to ``PITCH_MARGIN_DEG[1]`` (down)
  around a level camera; the 2026-10-25 and 2026-10-09 range setups were
  ~4 deg down.
* yaw +-``YAW_MARGIN_DEG`` (2026-10-25: ~1 deg).
* tee placement +-``RANGE_MARGIN_M`` along the line and +-``LATERAL_MARGIN_M``
  across it.

The ball sits ~6 deg below the horizon (camera ~0.16 m above it, 1.5 m away).
With 8 deg of down-pitch margin the zone's top edge is therefore below the
horizon of any camera pitched down by more than ~3.8 deg; a level camera's zone
reaches ~3.8 deg above its horizon, the price of not knowing the pitch. On
2026-10-25 (4.1 deg down) the zone starts at row 209, below the horizon (~206)
and the tree crown the old anchor locked onto (169), and 42 px above the
highest ball (teed driver, row 251); see ``tests/test_camera_tee_zone.py``.
The trigger check judges centres only from ``RING_OUTER_FRAC`` (1.4) ball
diameters below the zone top (``trigger_check._zone_box``, rows 233+ on
2026-10-25), so the mat ring it compares stays inside the zone as well.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import asdict, dataclass, replace

from openflight.camera.intrinsics import CameraIntrinsics

GOLF_BALL_DIAMETER_M = 0.04267
PITCH_MARGIN_DEG = (-6.0, 8.0)
YAW_MARGIN_DEG = 6.0
RANGE_MARGIN_M = 0.15
LATERAL_MARGIN_M = 0.05
PIXEL_MARGIN_PX = 4


@dataclass(frozen=True)
class TeeZone:
    """Search box ``[x0, x1) x [y0, y1)`` plus the nominal tee and ball size in pixels."""

    x0: int
    y0: int
    x1: int
    y1: int
    tee_x: float
    tee_y: float
    diameter_px: float
    image_width_px: int
    image_height_px: int

    def contains(self, x: float, y: float) -> bool:
        """Whether a ball centre at (x, y) lies inside the zone."""
        return self.x0 <= x < self.x1 and self.y0 <= y < self.y1

    def to_dict(self) -> dict:
        """JSON-ready form."""
        data = asdict(self)
        for key in ("tee_x", "tee_y", "diameter_px"):
            data[key] = round(data[key], 1)
        return data

    def shifted(self, dx: int, dy: int, width: int, height: int) -> TeeZone | None:
        """This zone in another capture mode: moved by (dx, dy), clipped to ``width`` x ``height``.

        The ball diameter is the same (the modes share the lens scale); the
        nominal tee moves with the box and may lie outside the new frame. None
        when less than ``tee_zone``'s 2 x 2 pixels of the box remain.
        """
        x0, x1 = max(0, self.x0 + dx), min(width, self.x1 + dx)
        y0, y1 = max(0, self.y0 + dy), min(height, self.y1 + dy)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        return replace(
            self,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            tee_x=self.tee_x + dx,
            tee_y=self.tee_y + dy,
            image_width_px=width,
            image_height_px=height,
        )


def _project(  # pylint: disable=too-many-arguments
    intrinsics: CameraIntrinsics,
    lateral_m: float,
    forward_m: float,
    up_m: float,
    *,
    pitch_down_deg: float,
    yaw_deg: float,
    horizontal_pixel_sign: float,
) -> tuple[float, float, float]:
    """Camera-relative point (target-right, downrange, up) -> pixel x, y and depth."""
    pitch = math.radians(pitch_down_deg)
    yaw = math.radians(yaw_deg)
    # Yaw turns the optical axis target-right; pitch tilts it down.
    right = math.cos(yaw) * lateral_m - math.sin(yaw) * forward_m
    ahead = math.sin(yaw) * lateral_m + math.cos(yaw) * forward_m
    down = math.cos(pitch) * -up_m - math.sin(pitch) * ahead
    depth = math.sin(pitch) * -up_m + math.cos(pitch) * ahead
    x_px, y_px = intrinsics.project(horizontal_pixel_sign * right / depth, down / depth)
    return x_px, y_px, depth


def tee_zone(  # pylint: disable=too-many-arguments,too-many-locals
    intrinsics: CameraIntrinsics,
    *,
    camera_height_m: float,
    camera_lateral_offset_m: float,
    camera_forward_offset_m: float,
    tee_range_m: float,
    radar_height_m: float,
    ball_height_m: float,
    horizontal_pixel_sign: float = 1.0,
) -> TeeZone | None:
    """The hitting zone for this mount, or None when the tee geometry is implausible.

    Offsets follow ``CameraDeliveryGeometry``: the camera's optical centre
    relative to the radar centre, positive target-right and downrange.
    """
    vertical = ball_height_m - radar_height_m
    if not tee_range_m > abs(vertical) + RANGE_MARGIN_M or camera_height_m <= ball_height_m:
        return None
    width, height = intrinsics.image_width_px, intrinsics.image_height_px

    def ball_at(range_m, lateral_m, pitch_down_deg, yaw_deg):
        forward = math.sqrt(range_m**2 - vertical**2) - camera_forward_offset_m
        return _project(
            intrinsics,
            lateral_m - camera_lateral_offset_m,
            forward,
            ball_height_m - camera_height_m,
            pitch_down_deg=pitch_down_deg,
            yaw_deg=yaw_deg,
            horizontal_pixel_sign=horizontal_pixel_sign,
        )

    corners = [
        ball_at(tee_range_m + range_offset, lateral, pitch, yaw)
        for range_offset, lateral, pitch, yaw in itertools.product(
            (-RANGE_MARGIN_M, RANGE_MARGIN_M),
            (-LATERAL_MARGIN_M, LATERAL_MARGIN_M),
            PITCH_MARGIN_DEG,
            (-YAW_MARGIN_DEG, YAW_MARGIN_DEG),
        )
    ]
    if min(depth for _x, _y, depth in corners) <= 0.0:
        return None
    largest_diameter = max(
        intrinsics.focal_px * GOLF_BALL_DIAMETER_M / depth for *_xy, depth in corners
    )
    margin = largest_diameter / 2.0 + PIXEL_MARGIN_PX
    x0 = max(0, math.floor(min(x for x, _y, _d in corners) - margin))
    x1 = min(width, math.ceil(max(x for x, _y, _d in corners) + margin) + 1)
    y0 = max(0, math.floor(min(y for _x, y, _d in corners) - margin))
    y1 = min(height, math.ceil(max(y for _x, y, _d in corners) + margin) + 1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    tee_x, tee_y, depth = ball_at(tee_range_m, 0.0, 0.0, 0.0)
    return TeeZone(
        x0=x0,
        y0=y0,
        x1=x1,
        y1=y1,
        tee_x=tee_x,
        tee_y=tee_y,
        diameter_px=intrinsics.focal_px * GOLF_BALL_DIAMETER_M / depth,
        image_width_px=width,
        image_height_px=height,
    )
