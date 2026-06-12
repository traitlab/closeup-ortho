"""Footprint and georeferencing geometry for a simple nadir planar projection.

All functions assume a perfect-nadir camera (gimbal pointing straight down). The only
free parameters are the camera-to-ground distance ``dist`` (meters), the lens, and the
gimbal yaw (heading, clockwise from North).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from affine import Affine
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, box

# Diagonal of a 36 x 24 mm "full frame", used to interpret FocalLengthIn35mmFilm, which
# DJI defines on the diagonal.
_DIAG35_MM = math.hypot(36.0, 24.0)  # 43.26661...


@dataclass
class Footprint:
    """Result of sizing a photo's ground footprint at a given distance."""

    gsd_m: float  # ground sample distance, meters / pixel
    width_m: float  # ground footprint width  (along image columns)
    height_m: float  # ground footprint height (along image rows)
    focal_source: str  # "calib" or "fl35"


def focal_length_px(
    width_px: int,
    height_px: int,
    fl35_mm: float | None,
    calib_focal_px: float | None,
    camera: str,
) -> tuple[float, str]:
    """Return the focal length in pixels and which source was used.

    Wide photos carry a photogrammetric ``CalibratedFocalLength`` (pixels) which is the
    most accurate; tele photos do not, so we derive the focal length from
    ``FocalLengthIn35mmFilm`` interpreted on the sensor diagonal (square pixels assumed,
    which holds for these cameras).
    """
    if camera == "wide" and calib_focal_px:
        return calib_focal_px, "calib"
    if fl35_mm:
        diag_px = math.hypot(width_px, height_px)
        return diag_px * fl35_mm / _DIAG35_MM, "fl35"
    raise ValueError("No focal length available (neither CalibratedFocalLength nor FL35).")


def compute_footprint(
    dist_m: float,
    width_px: int,
    height_px: int,
    fl35_mm: float | None,
    calib_focal_px: float | None,
    camera: str,
) -> Footprint:
    """Size the ground footprint of a nadir photo at camera-to-ground distance ``dist_m``.

    With square pixels the ground sample distance is identical on both axes, so the
    footprint dimensions follow directly from the pixel counts (which already encode the
    aspect ratio).
    """
    f_px, source = focal_length_px(width_px, height_px, fl35_mm, calib_focal_px, camera)
    gsd = dist_m / f_px
    return Footprint(
        gsd_m=gsd,
        width_m=gsd * width_px,
        height_m=gsd * height_px,
        focal_source=source,
    )


def footprint_polygon(x0: float, y0: float, width_m: float, height_m: float, yaw_deg: float) -> Polygon:
    """Build the ground footprint rectangle centered at ``(x0, y0)`` in the DSM CRS.

    ``yaw_deg`` is the gimbal heading (clockwise from North). Shapely rotation is
    counter-clockwise-positive, so we pass ``-yaw_deg``.
    """
    rect = box(-width_m / 2.0, -height_m / 2.0, width_m / 2.0, height_m / 2.0)
    rect = rotate(rect, -yaw_deg, origin=(0.0, 0.0))
    return translate(rect, xoff=x0, yoff=y0)


def geotiff_affine(
    x0: float,
    y0: float,
    gsd_m: float,
    width_px: int,
    height_px: int,
    yaw_deg: float,
) -> Affine:
    """Affine transform mapping image (col, row) -> projected (easting, northing).

    Conventions: image column increases East-ish, row increases downward; northing
    increases upward (hence the negative ``e`` term, the standard north-up flip); yaw is
    clockwise from North. Verified by pushing the image corners through the matrix.
    """
    g = gsd_m
    cx = width_px / 2.0
    cy = height_px / 2.0
    p = math.radians(yaw_deg)
    cs = math.cos(p)
    sn = math.sin(p)

    a = g * cs
    b = -g * sn
    c = x0 - cx * g * cs + cy * g * sn
    d = -g * sn
    e = -g * cs
    f = y0 + cx * g * sn + cy * g * cs
    return Affine(a, b, c, d, e, f)
