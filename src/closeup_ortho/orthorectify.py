"""Orchestration: scan a photo folder, project each photo, write footprints + GeoTIFFs.

This implements a deliberately simple *planar* projection (not full orthorectification):
each nadir photo is placed on the ground as a rectangle whose size comes from the
camera-to-ground distance (absolute ellipsoidal altitude minus the max DSM elevation in a
small buffer below the camera) and whose heading comes from the gimbal yaw.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from PIL import Image

from closeup_ortho.dsm import DSM
from closeup_ortho.geometry import compute_footprint, footprint_polygon, geotiff_affine
from closeup_ortho.metadata import PhotoMeta, camera_of, read_photo_meta

log = logging.getLogger("closeup_ortho")

VALID_LENSES = {"tele", "wide"}

# Nadir tolerance (degrees of gimbal pitch/roll deviation from straight-down).
_NADIR_WARN_DEG = 0.5
_NADIR_SKIP_DEG = 3.0

# Auto buffer target diameter (meters); clamped to the footprint and to <= 1 m.
_BUFFER_TARGET_DIAM_M = 0.625
_BUFFER_MAX_DIAM_M = 1.0


def _iter_photos(photo_dir: Path, recursive: bool):
    pattern = "**/*" if recursive else "*"
    for p in sorted(photo_dir.glob(pattern)):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"):
            yield p


def _auto_buffer_radius(footprint_w_m: float, footprint_h_m: float) -> float:
    """Buffer radius (m): ~50-75 cm diameter, never larger than the footprint, <= 1 m."""
    diam = min(_BUFFER_TARGET_DIAM_M, footprint_w_m, footprint_h_m, _BUFFER_MAX_DIAM_M)
    return diam / 2.0


def _project_one(meta: PhotoMeta, dsm: DSM, buffer_radius_m: float | None) -> dict | None:
    """Run the planar projection for one photo. Returns a record dict, or ``None`` to skip."""
    name = meta.path.name

    # Nadir gate.
    pitch_dev = abs(meta.pitch + 90.0)
    roll_dev = abs(meta.roll)
    dev = max(pitch_dev, roll_dev)
    if dev > _NADIR_SKIP_DEG:
        log.warning("SKIP %s: gimbal not nadir (pitch=%.2f, roll=%.2f)", name, meta.pitch, meta.roll)
        return None
    nadir_ok = dev <= _NADIR_WARN_DEG
    if not nadir_ok:
        log.warning("%s: gimbal off-nadir by %.2f deg (proceeding)", name, dev)

    x0, y0 = dsm.lonlat_to_xy(meta.longitude, meta.latitude)
    if not dsm.contains(x0, y0):
        log.warning("SKIP %s: camera position is outside the DSM extent", name)
        return None

    # Seed distance from the centre pixel, size the footprint, derive the buffer, then
    # resample the max elevation within that buffer (one refinement pass).
    seed_elev = dsm.max_elevation(x0, y0, 0.0)
    if seed_elev is None:
        log.warning("SKIP %s: only nodata under camera in the DSM", name)
        return None
    seed_dist = meta.abs_alt_m - seed_elev
    if seed_dist <= 0:
        log.warning("SKIP %s: non-positive distance (abs_alt=%.2f, dsm=%.2f)", name, meta.abs_alt_m, seed_elev)
        return None
    seed_fp = compute_footprint(
        seed_dist, meta.width_px, meta.height_px, meta.fl35_mm, meta.calib_focal_px, meta.camera
    )

    radius = buffer_radius_m if buffer_radius_m is not None else _auto_buffer_radius(seed_fp.width_m, seed_fp.height_m)
    max_elev = dsm.max_elevation(x0, y0, radius)
    if max_elev is None:
        log.warning("SKIP %s: only nodata in DSM buffer", name)
        return None
    dist = meta.abs_alt_m - max_elev
    if dist <= 0:
        log.warning("SKIP %s: non-positive distance after buffer (abs_alt=%.2f, dsm_max=%.2f)", name, meta.abs_alt_m, max_elev)
        return None

    fp = compute_footprint(
        dist, meta.width_px, meta.height_px, meta.fl35_mm, meta.calib_focal_px, meta.camera
    )
    poly = footprint_polygon(x0, y0, fp.width_m, fp.height_m, meta.yaw)

    return {
        "record": {
            "path": str(meta.path),
            "filename": name,
            "directory": str(meta.path.parent),
            "camera": meta.camera,
            "longitude": meta.longitude,
            "latitude": meta.latitude,
            "abs_alt_m": meta.abs_alt_m,
            "dist_m": dist,
            "gsd_m": fp.gsd_m,
            "footprint_w_m": fp.width_m,
            "footprint_h_m": fp.height_m,
            "yaw": meta.yaw,
            "pitch": meta.pitch,
            "roll": meta.roll,
            "nadir_ok": nadir_ok,
            "focal_source": fp.focal_source,
            "timestamp": meta.timestamp,
            "geometry": poly,
        },
        "x0": x0,
        "y0": y0,
        "gsd_m": fp.gsd_m,
        "meta": meta,
    }


def _write_geotiff(meta: PhotoMeta, x0: float, y0: float, gsd_m: float, crs, out_path: Path) -> None:
    """Write the photo as a georeferenced (rotated) GeoTIFF in the DSM CRS."""
    with Image.open(meta.path) as im:
        rgb = np.asarray(im.convert("RGB"))  # (H, W, 3)
    bands = np.transpose(rgb, (2, 0, 1))  # (3, H, W)
    transform = geotiff_affine(x0, y0, gsd_m, meta.width_px, meta.height_px, meta.yaw)
    profile = dict(
        driver="COG",
        height=meta.height_px,
        width=meta.width_px,
        count=3,
        dtype="uint8",
        crs=crs,
        transform=transform,
        compress="DEFLATE",
        BIGTIFF="IF_SAFER",
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(bands)


def orthorectify_folder(
    photo_dir,
    dsm_path,
    out_gpkg,
    lenses=None,
    write_geotiffs: bool = False,
    geotiff_dir=None,
    buffer_radius_m: float | None = None,
    recursive: bool = True,
) -> gpd.GeoDataFrame:
    """Planar-project close-up photos in ``photo_dir`` onto ``dsm_path``.

    Parameters
    ----------
    photo_dir : path
        Folder of DJI close-up JPEGs (searched recursively by default).
    dsm_path : path
        DSM raster (any rasterio-readable format). Its CRS is used for all output.
    out_gpkg : path
        Output GeoPackage; footprints are written to a ``footprints`` layer.
    lenses : set[str] | None
        Which lenses to process, a subset of ``{"tele", "wide"}``. Defaults to
        ``{"tele"}``.
    write_geotiffs : bool
        If true, also write each projected photo as a GeoTIFF.
    geotiff_dir : path | None
        Destination for GeoTIFFs (required when ``write_geotiffs`` is true).
    buffer_radius_m : float | None
        Override the auto-computed DSM sampling buffer radius (meters).
    recursive : bool
        Recurse into subfolders when scanning for photos.

    Returns
    -------
    geopandas.GeoDataFrame
        The footprints written, in the DSM CRS.
    """
    photo_dir = Path(photo_dir)
    out_gpkg = Path(out_gpkg)
    lenses = set(lenses) if lenses else {"tele"}
    bad = lenses - VALID_LENSES
    if bad:
        raise ValueError(f"Invalid lenses {bad}; choose from {VALID_LENSES}.")
    if write_geotiffs and geotiff_dir is None:
        raise ValueError("geotiff_dir is required when write_geotiffs is True.")
    geotiff_dir = Path(geotiff_dir) if geotiff_dir else None

    n_seen = n_processed = n_skipped = 0
    records: list[dict] = []

    with DSM(dsm_path) as dsm:
        log.info("DSM CRS: %s | resolution: %.4f m", dsm.crs, dsm.res_x)
        for path in _iter_photos(photo_dir, recursive):
            # Cheap lens pre-filter on the filename before parsing full metadata.
            quick_cam = camera_of(path, None)
            if quick_cam is not None and quick_cam not in lenses:
                continue
            n_seen += 1

            meta = read_photo_meta(path)
            if meta is None:
                log.warning("SKIP %s: missing required DJI metadata", path.name)
                n_skipped += 1
                continue
            if meta.camera not in lenses:
                continue

            result = _project_one(meta, dsm, buffer_radius_m)
            if result is None:
                n_skipped += 1
                continue

            records.append(result["record"])
            n_processed += 1

            if write_geotiffs:
                out_tif = geotiff_dir / (path.stem + ".tif")
                _write_geotiff(meta, result["x0"], result["y0"], result["gsd_m"], dsm.crs, out_tif)

        crs = dsm.crs

    if not records:
        raise RuntimeError("No photos were successfully projected; check lens filter and DSM coverage.")

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_gpkg, layer="footprints", driver="GPKG")

    log.info("Done: %d processed, %d skipped (%d candidate photos). Footprints -> %s",
             n_processed, n_skipped, n_seen, out_gpkg)
    return gdf
