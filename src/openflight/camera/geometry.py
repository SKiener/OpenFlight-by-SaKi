"""Shared camera-coordinate geometry helpers."""

from __future__ import annotations

import math

import numpy as np

from openflight.camera.intrinsics import CameraIntrinsics


def deroll_normalized_offsets(
    horizontal: float | np.ndarray,
    vertical: float | np.ndarray,
    correction_deg: float,
) -> tuple[float | np.ndarray, float | np.ndarray]:
    """Remove clockwise image roll from normalized image-plane offsets.

    ``vertical`` is positive upward, unlike image-row coordinates. A positive
    correction therefore rotates the coordinate basis clockwise on screen.
    """
    angle = math.radians(correction_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    return (
        cosine * horizontal + sine * vertical,
        -sine * horizontal + cosine * vertical,
    )


def normalized_image_offsets(
    x_px: float | np.ndarray,
    y_px: float | np.ndarray,
    *,
    focal_px: float,
    image_width_px: int,
    image_height_px: int,
    horizontal_pixel_sign: float,
    roll_correction_deg: float,
    intrinsics: CameraIntrinsics | None = None,
) -> tuple[float | np.ndarray, float | np.ndarray]:
    """Saved-frame pixels -> derolled (target-right, upward) unit-plane offsets.

    Without intrinsics this is the legacy pinhole at the image centre with the
    reference-ball focal scale; with them, the calibrated lens model is used.
    """
    if intrinsics is None:
        image_x = horizontal_pixel_sign * (x_px - image_width_px / 2.0) / focal_px
        image_z = -(y_px - image_height_px / 2.0) / focal_px
    else:
        undistorted_x, undistorted_y = intrinsics.undistort(x_px, y_px)
        image_x = horizontal_pixel_sign * undistorted_x
        image_z = -undistorted_y
    return deroll_normalized_offsets(image_x, image_z, roll_correction_deg)


def reference_focal_px(
    ball_diameter_px: float,
    camera_ball_range_m: float,
    ball_diameter_m: float,
    intrinsics: CameraIntrinsics | None = None,
) -> float:
    """Calibrated focal length, else the stationary ball's apparent-size scale."""
    if intrinsics is not None:
        return intrinsics.focal_px
    return ball_diameter_px * camera_ball_range_m / ball_diameter_m


def reference_ball_pose(
    ball_x: float,
    ball_z: float,
    *,
    camera_lateral_offset_m: float,
    camera_ball_forward_m: float,
    height_delta_m: float,
) -> tuple[float, float]:
    """Camera yaw and pitch (rad) that put the teed ball where it is seen.

    Assumes the ball sits on the radar/enclosure centreline; ``ball_x``/``ball_z``
    are its derolled unit-plane offsets and ``height_delta_m`` is ball minus camera.
    """
    expected_azimuth = math.atan2(-camera_lateral_offset_m, camera_ball_forward_m)
    yaw_rad = expected_azimuth - math.atan2(ball_x, 1.0)
    pitch_rad = math.atan2(height_delta_m, camera_ball_forward_m) - math.atan2(ball_z, 1.0)
    return yaw_rad, pitch_rad


def camera_to_world(
    lateral: float | np.ndarray,
    forward: float | np.ndarray,
    vertical: float | np.ndarray,
    *,
    pitch_rad: float,
    yaw_rad: float,
) -> tuple[float | np.ndarray, float | np.ndarray, float | np.ndarray]:
    """Rotate camera-frame vectors to world (lateral, forward, vertical)."""
    level_forward = math.cos(pitch_rad) * forward - math.sin(pitch_rad) * vertical
    world_vertical = math.sin(pitch_rad) * forward + math.cos(pitch_rad) * vertical
    world_lateral = math.cos(yaw_rad) * lateral + math.sin(yaw_rad) * level_forward
    world_forward = -math.sin(yaw_rad) * lateral + math.cos(yaw_rad) * level_forward
    return world_lateral, world_forward, world_vertical
