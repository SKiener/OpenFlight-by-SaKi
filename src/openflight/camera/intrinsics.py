"""Calibrated camera lens model (pinhole + Brown-Conrady distortion).

Produced by ``scripts/hardware-test/calibrate_camera_intrinsics.py`` from
checkerboard views in the saved-frame orientation (after the runtime's
rotate/mirror), so it applies directly to saved capture pixels.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

INTRINSICS_VERSION = 1
UNDISTORT_ITERATIONS = 20


@dataclass(frozen=True)
class CameraIntrinsics:
    """Focal lengths, principal point and distortion for one capture mode."""

    fx: float
    fy: float
    cx: float
    cy: float
    dist_coeffs: tuple[float, ...]
    image_width_px: int
    image_height_px: int
    rotate_180: bool
    mirror_horizontal: bool
    scaler_crop: tuple[int, ...] | None
    rms_reprojection_px: float

    @property
    def focal_px(self) -> float:
        """Mean focal length used where a single pixel scale is needed."""
        return (self.fx + self.fy) / 2.0

    def undistort(
        self, x_px: float | np.ndarray, y_px: float | np.ndarray
    ) -> tuple[float | np.ndarray, float | np.ndarray]:
        """Pixel coordinates -> undistorted normalized image coordinates (y down)."""
        distorted_x = (np.asarray(x_px, dtype=float) - self.cx) / self.fx
        distorted_y = (np.asarray(y_px, dtype=float) - self.cy) / self.fy
        k1, k2, p1, p2, k3 = (*self.dist_coeffs, 0.0)[:5]
        x, y = distorted_x, distorted_y
        for _ in range(UNDISTORT_ITERATIONS):
            r2 = x * x + y * y
            radial = 1.0 + r2 * (k1 + r2 * (k2 + r2 * k3))
            delta_x = 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
            delta_y = p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
            x = (distorted_x - delta_x) / radial
            y = (distorted_y - delta_y) / radial
        if np.ndim(x_px) == 0 and np.ndim(y_px) == 0:
            return float(x), float(y)
        return x, y

    def project(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        """Undistorted normalized image coordinates (y down) -> saved-frame pixels.

        The forward Brown-Conrady model, the inverse of ``undistort``.
        """
        k1, k2, p1, p2, k3 = (*self.dist_coeffs, 0.0)[:5]
        x, y = float(x_norm), float(y_norm)
        r2 = x * x + y * y
        radial = 1.0 + r2 * (k1 + r2 * (k2 + r2 * k3))
        distorted_x = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        distorted_y = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        return self.fx * distorted_x + self.cx, self.fy * distorted_y + self.cy

    def mode_mismatch(self, settings: dict) -> str | None:
        """Describe why saved-frame settings differ from the calibrated mode."""
        crop = settings.get("scaler_crop")
        actual = {
            "width": settings.get("width"),
            "height": settings.get("height"),
            "rotate_180": bool(settings.get("rotate_180", False)),
            "mirror_horizontal": bool(settings.get("mirror_horizontal", False)),
            "scaler_crop": tuple(crop) if crop is not None else None,
        }
        expected = {
            "width": self.image_width_px,
            "height": self.image_height_px,
            "rotate_180": self.rotate_180,
            "mirror_horizontal": self.mirror_horizontal,
            "scaler_crop": self.scaler_crop,
        }
        for key, value in expected.items():
            if actual[key] != value:
                return f"{key}: calibrated {value!r}, capture {actual[key]!r}"
        return None


def for_mode(base: CameraIntrinsics, width, height, vertical_offset_px) -> CameraIntrinsics | None:
    """``base`` (calibrated at sensor window 0) carried into another capture mode.

    The modes share the 2:1 binning, so fx, fy and the distortion hold and only
    the principal point moves by the mode's frame shift
    (``capture_runtime.CAPTURE_MODE_FRAME_OFFSETS``): 640x200 at strip offset s
    has cy - 100 - s, cy - 100 + s for a 180-degree mount (``base.rotate_180``:
    rotated in software). The calibrated mode itself returns ``base``; a mode
    without a known mapping returns None. Orientation and ScalerCrop are not
    checked here: ``mode_mismatch`` of the result still compares them.
    """
    # capture_runtime imports this module (through tee_zone): import at call time.
    from openflight.camera.capture_runtime import mode_frame_shift  # noqa: PLC0415

    calibrated = (base.image_width_px, base.image_height_px, 0)
    if (width, height, vertical_offset_px) == calibrated:
        return base
    shift = mode_frame_shift(
        calibrated, (width, height, vertical_offset_px), rotate_180=base.rotate_180
    )
    if shift is None:
        return None
    return replace(
        base,
        cx=base.cx + shift[0],
        cy=base.cy + shift[1],
        image_width_px=width,
        image_height_px=height,
    )


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"camera intrinsics {name} must be a finite number")
    return float(value)


def load_camera_intrinsics(path: str | Path) -> CameraIntrinsics:
    """Load and validate a calibration JSON written by the calibration script."""
    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read camera intrinsics {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != INTRINSICS_VERSION:
        raise ValueError(f"camera intrinsics must be a version {INTRINSICS_VERSION} object")
    mode = payload.get("mode")
    if not isinstance(mode, dict):
        raise ValueError("camera intrinsics mode must describe the capture mode")
    width, height = mode.get("width"), mode.get("height")
    if not all(isinstance(value, int) and value > 0 for value in (width, height)):
        raise ValueError("camera intrinsics mode width/height must be positive integers")
    coefficients = payload.get("dist_coeffs")
    if not isinstance(coefficients, list) or len(coefficients) not in (4, 5):
        raise ValueError("camera intrinsics dist_coeffs must hold k1,k2,p1,p2[,k3]")
    dist_coeffs = tuple(_finite(item, "dist_coeffs") for item in coefficients)
    fx, fy, cx, cy = (_finite(payload.get(key), key) for key in ("fx", "fy", "cx", "cy"))
    if fx <= 0.0 or fy <= 0.0 or not (0.0 <= cx <= width and 0.0 <= cy <= height):
        raise ValueError("camera intrinsics focal lengths/principal point are implausible")
    crop = mode.get("scaler_crop")
    return CameraIntrinsics(
        fx=fx,
        fy=fy,
        cx=cx,
        cy=cy,
        dist_coeffs=dist_coeffs,
        image_width_px=width,
        image_height_px=height,
        rotate_180=bool(mode.get("rotate_180", False)),
        mirror_horizontal=bool(mode.get("mirror_horizontal", False)),
        scaler_crop=tuple(int(item) for item in crop) if crop is not None else None,
        rms_reprojection_px=_finite(payload.get("rms_reprojection_px"), "rms_reprojection_px"),
    )
