"""Command-line interface for closeup-ortho."""

from __future__ import annotations

import argparse
import logging
import sys

from closeup_ortho.batch import (
    DEFAULT_DSM_ROOT,
    DEFAULT_PHOTOS_ROOT,
    orthorectify_missions,
    read_mission_pairs,
)
from closeup_ortho.labelbox import project_labels
from closeup_ortho.orthorectify import orthorectify_folder


def _parse_lenses(value: str) -> set[str]:
    value = value.lower()
    if value == "all":
        return {"tele", "wide"}
    return {part.strip() for part in value.split(",") if part.strip()}


def _cmd_orthorectify(args: argparse.Namespace) -> int:
    gdf = orthorectify_folder(
        photo_dir=args.photos,
        dsm_path=args.dsm,
        out_gpkg=args.out,
        lenses=args.lenses,
        write_geotiffs=args.geotiffs is not None,
        geotiff_dir=args.geotiffs,
        buffer_radius_m=args.buffer_radius,
        recursive=not args.no_recursive,
    )
    print(f"Wrote {len(gdf)} footprints to {args.out} (CRS: {gdf.crs})")
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    pairs = read_mission_pairs(args.missions)
    gdf = orthorectify_missions(
        pairs,
        out_gpkg=args.out,
        photos_root=args.photos_root,
        dsm_root=args.dsm_root,
        lenses=args.lenses,
        write_geotiffs=args.geotiffs is not None,
        geotiff_dir=args.geotiffs,
        buffer_radius_m=args.buffer_radius,
        recursive=not args.no_recursive,
    )
    n_missions = gdf["mission"].nunique()
    print(f"Wrote {len(gdf)} footprints from {n_missions} mission(s) to {args.out} (CRS: {gdf.crs})")
    return 0


def _cmd_labels(args: argparse.Namespace) -> int:
    gdf = project_labels(
        ndjson_path=args.ndjson,
        footprints_gpkg=args.footprints,
        out_gpkg=args.out,
        footprints_layer=args.footprints_layer,
        out_layer=args.out_layer,
    )
    print(f"Wrote {len(gdf)} label polygons to {args.out} (CRS: {gdf.crs})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="closeup-ortho",
        description="Simple planar projection of DJI nadir close-up photos onto a DSM.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    # ── orthorectify subcommand ──────────────────────────────────────────────
    p_ortho = sub.add_parser("orthorectify", help="Project photos onto a DSM.")
    p_ortho.add_argument("--photos", required=True, help="Folder of close-up JPEGs.")
    p_ortho.add_argument("--dsm", required=True, help="DSM raster (defines the output CRS).")
    p_ortho.add_argument("--out", required=True, help="Output GeoPackage (footprints layer).")
    p_ortho.add_argument(
        "--lenses",
        type=_parse_lenses,
        default={"tele"},
        help="Lenses to process: 'tele' (default), 'wide', 'tele,wide', or 'all'.",
    )
    p_ortho.add_argument("--geotiffs", default=None, help="Also write GeoTIFFs to this folder.")
    p_ortho.add_argument(
        "--buffer-radius",
        type=float,
        default=None,
        help="Override DSM sampling buffer radius in meters (default: auto).",
    )
    p_ortho.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into subfolders when scanning for photos.",
    )

    # ── batch subcommand ─────────────────────────────────────────────────────
    p_batch = sub.add_parser(
        "batch",
        help="Project several missions (each onto its own DSM) into one GeoPackage.",
    )
    p_batch.add_argument(
        "--missions", required=True,
        help="Text file of 'closeup_mission,photogrammetry_mission' pairs, one per line "
             "('#' comments allowed).",
    )
    p_batch.add_argument("--out", required=True, help="Output GeoPackage (single footprints layer).")
    p_batch.add_argument(
        "--photos-root", default=DEFAULT_PHOTOS_ROOT,
        help=f"Root of the drone-mission photo tree (default: {DEFAULT_PHOTOS_ROOT}).",
    )
    p_batch.add_argument(
        "--dsm-root", default=DEFAULT_DSM_ROOT,
        help=f"Root of the Metashape product tree (default: {DEFAULT_DSM_ROOT}).",
    )
    p_batch.add_argument(
        "--lenses",
        type=_parse_lenses,
        default={"tele"},
        help="Lenses to process: 'tele' (default), 'wide', 'tele,wide', or 'all'.",
    )
    p_batch.add_argument(
        "--geotiffs", default=None,
        help="Also write GeoTIFFs, under <this folder>/<closeup_mission>/.",
    )
    p_batch.add_argument(
        "--buffer-radius",
        type=float,
        default=None,
        help="Override DSM sampling buffer radius in meters (default: auto).",
    )
    p_batch.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not recurse into subfolders when scanning for photos.",
    )

    # ── labels subcommand ────────────────────────────────────────────────────
    p_labels = sub.add_parser(
        "labels",
        help="Project Labelbox bounding-box annotations onto footprints.",
    )
    p_labels.add_argument("--ndjson", required=True, help="Labelbox ndjson export file.")
    p_labels.add_argument(
        "--footprints", required=True,
        help="Footprints GeoPackage produced by the orthorectify command.",
    )
    p_labels.add_argument("--out", required=True, help="Output GeoPackage (labels layer).")
    p_labels.add_argument(
        "--footprints-layer", default="footprints",
        help="Layer name inside the footprints GeoPackage (default: footprints).",
    )
    p_labels.add_argument(
        "--out-layer", default="labels",
        help="Layer name to write inside the output GeoPackage (default: labels).",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.command == "orthorectify":
        return _cmd_orthorectify(args)
    if args.command == "batch":
        return _cmd_batch(args)
    if args.command == "labels":
        return _cmd_labels(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
