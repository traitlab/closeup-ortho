"""DSM access: open once, transform camera coordinates, sample max elevation in a buffer."""

from __future__ import annotations

import math

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window


class DSM:
    """Wraps an open DSM raster and the WGS84 -> DSM-CRS transformer.

    Open once and reuse across all photos; the dataset handle and transformer are the
    expensive bits.
    """

    def __init__(self, path):
        self._ds = rasterio.open(path)
        self.crs = self._ds.crs
        self.bounds = self._ds.bounds
        self.nodata = self._ds.nodata
        self.res_x, self.res_y = abs(self._ds.res[0]), abs(self._ds.res[1])
        # Drone GPS is WGS84 geographic (EPSG:4326); reproject into the DSM's own CRS.
        self._tf = Transformer.from_crs("EPSG:4326", self.crs, always_xy=True)

    def close(self) -> None:
        self._ds.close()

    def __enter__(self) -> "DSM":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def lonlat_to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        """Project WGS84 lon/lat to the DSM CRS (easting, northing)."""
        x, y = self._tf.transform(lon, lat)
        return x, y

    def contains(self, x: float, y: float) -> bool:
        b = self.bounds
        return b.left <= x <= b.right and b.bottom <= y <= b.top

    def max_elevation(self, x: float, y: float, radius_m: float) -> float | None:
        """Max elevation within ``radius_m`` of ``(x, y)``, ignoring nodata.

        A square window covering the radius is read (cheap and sufficient given the small
        buffers used here). Returns ``None`` if the point is outside the raster or the
        window holds only nodata. Pass ``radius_m == 0`` to sample the single pixel.
        """
        if not self.contains(x, y):
            return None

        row, col = self._ds.index(x, y)
        r_px = max(0, math.ceil(radius_m / self.res_x))
        full = Window(0, 0, self._ds.width, self._ds.height)
        win = Window(col - r_px, row - r_px, 2 * r_px + 1, 2 * r_px + 1).intersection(full)
        if win.width <= 0 or win.height <= 0:
            return None

        arr = self._ds.read(1, window=win).astype("float64")
        if self.nodata is not None:
            arr[arr == self.nodata] = np.nan
        if np.all(np.isnan(arr)):
            return None
        return float(np.nanmax(arr))
