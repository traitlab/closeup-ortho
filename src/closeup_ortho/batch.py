"""Batch runner: project several close-up missions, each onto its own DSM, into one GeoPackage.

The link between a close-up ("wpt") mission and the photogrammetry mission whose DSM it
should be projected onto is made manually: the input is a list of
``closeup_mission,photogrammetry_mission`` pairs (one per line, ``#`` comments and blank
lines allowed). Paths are resolved from two root folders using the lab layout:

- photos: ``<photos_root>/<yyyy>/<closeup_mission>/`` (year taken from the mission name)
- DSM:    ``<dsm_root>/<yyyy>/<pg_mission>/<pg_mission>_dsm.cog.tif`` (falls back to
  ``_dsm.tif`` when no COG exists)

All footprints go into a single ``footprints`` layer with ``mission``,
``photogrammetry_mission`` and ``dsm`` columns. The Labelbox label projection
reconstructs meter-based affines from the footprint attributes, so footprints must stay
in the projected CRS of their DSM: if the missions' DSMs disagree on CRS, this raises
instead of silently reprojecting.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd

from closeup_ortho.orthorectify import project_folder

log = logging.getLogger("closeup_ortho")

DEFAULT_PHOTOS_ROOT = Path("/mnt/nfs/lefodata/data/drone_missions")
DEFAULT_DSM_ROOT = Path("/mnt/nfs/conrad/labolaliberte_data/metashape")


def read_mission_pairs(path) -> list[tuple[str, str]]:
    """Parse a missions file into ``(closeup_mission, photogrammetry_mission)`` pairs."""
    pairs: list[tuple[str, str]] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.replace(",", " ").split()]
        if len(parts) != 2:
            raise ValueError(
                f"{path}:{lineno}: expected 'closeup_mission,photogrammetry_mission', got {raw!r}"
            )
        pairs.append((parts[0], parts[1]))
    if not pairs:
        raise ValueError(f"{path}: no mission pairs found.")
    return pairs


def _mission_year(mission: str) -> str:
    """Extract the year from a ``yyyymmdd_...`` mission name."""
    year = mission[:4]
    if not (len(mission) > 8 and mission[:8].isdigit()):
        raise ValueError(f"Mission name {mission!r} does not start with a yyyymmdd date.")
    return year


def resolve_photo_dir(mission: str, photos_root) -> Path:
    photo_dir = Path(photos_root) / _mission_year(mission) / mission
    if not photo_dir.is_dir():
        raise FileNotFoundError(f"Photo folder not found: {photo_dir}")
    return photo_dir


def resolve_dsm(pg_mission: str, dsm_root) -> Path:
    """Return the mission DSM, preferring the COG variant."""
    mission_dir = Path(dsm_root) / _mission_year(pg_mission) / pg_mission
    candidates = [
        mission_dir / f"{pg_mission}_dsm.cog.tif",
        mission_dir / f"{pg_mission}_dsm.tif",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "DSM not found for mission "
        f"{pg_mission!r}; tried: " + ", ".join(str(c) for c in candidates)
    )


def orthorectify_missions(
    pairs,
    out_gpkg,
    photos_root=DEFAULT_PHOTOS_ROOT,
    dsm_root=DEFAULT_DSM_ROOT,
    lenses=None,
    write_geotiffs: bool = False,
    geotiff_dir=None,
    buffer_radius_m: float | None = None,
    recursive: bool = True,
) -> gpd.GeoDataFrame:
    """Project every mission in ``pairs`` and write one combined footprints layer.

    Parameters
    ----------
    pairs : list[tuple[str, str]]
        ``(closeup_mission, photogrammetry_mission)`` name pairs (see
        :func:`read_mission_pairs`).
    out_gpkg : path
        Output GeoPackage; all footprints go to a single ``footprints`` layer.
    photos_root, dsm_root : path
        Roots of the drone-mission photo tree and the Metashape product tree.
    lenses, write_geotiffs, geotiff_dir, buffer_radius_m, recursive
        Same as :func:`closeup_ortho.orthorectify.orthorectify_folder`. GeoTIFFs are
        written under ``geotiff_dir/<closeup_mission>/``.

    Returns
    -------
    geopandas.GeoDataFrame
        The combined footprints, in the (shared) DSM CRS.
    """
    out_gpkg = Path(out_gpkg)
    if write_geotiffs and geotiff_dir is None:
        raise ValueError("geotiff_dir is required when write_geotiffs is True.")

    # Resolve every mission up front so a typo fails fast, before hours of processing.
    resolved = []
    for mission, pg_mission in pairs:
        photo_dir = resolve_photo_dir(mission, photos_root)
        dsm_path = resolve_dsm(pg_mission, dsm_root)
        resolved.append((mission, pg_mission, photo_dir, dsm_path))

    frames: list[gpd.GeoDataFrame] = []
    crs_by_mission: dict[str, object] = {}
    for mission, pg_mission, photo_dir, dsm_path in resolved:
        log.info("=== %s (DSM: %s) ===", mission, dsm_path.name)
        records, crs = project_folder(
            photo_dir,
            dsm_path,
            lenses=lenses,
            write_geotiffs=write_geotiffs,
            geotiff_dir=Path(geotiff_dir) / mission if write_geotiffs else None,
            buffer_radius_m=buffer_radius_m,
            recursive=recursive,
        )
        if not records:
            log.warning("%s: no photos projected; mission left out of the output.", mission)
            continue
        gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
        gdf.insert(0, "mission", mission)
        gdf.insert(1, "photogrammetry_mission", pg_mission)
        gdf.insert(2, "dsm", str(dsm_path))
        frames.append(gdf)
        crs_by_mission[mission] = crs

    if not frames:
        raise RuntimeError("No photos were successfully projected in any mission.")

    crs_set = {str(c) for c in crs_by_mission.values()}
    if len(crs_set) > 1:
        detail = ", ".join(f"{m}: {c}" for m, c in crs_by_mission.items())
        raise ValueError(
            "Missions have different DSM CRSs and cannot share one footprints layer "
            f"({detail}). Run them in separate GeoPackages, or group missions by CRS."
        )

    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), geometry="geometry", crs=frames[0].crs
    )
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    combined.to_file(out_gpkg, layer="footprints", driver="GPKG")
    log.info(
        "Done: %d footprints from %d mission(s) -> %s", len(combined), len(frames), out_gpkg
    )
    return combined
