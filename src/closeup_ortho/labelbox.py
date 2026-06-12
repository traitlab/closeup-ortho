"""Project Labelbox bounding-box annotations onto georeferenced photo footprints.

For each bounding box in the ndjson export, this module:
1. Looks up the matching footprint row (by filename) in a GeoPackage produced by
   :func:`closeup_ortho.orthorectify.orthorectify_folder`.
2. Reconstructs the same planar-projection affine transform from the footprint attributes
   (gsd_m, yaw, footprint centroid, and image pixel dimensions from the ndjson
   media_attributes).
3. Maps the four bbox corners from image-pixel space to projected coordinates.
4. Writes a new GeoPackage polygon layer (``labels``) with one row per bounding box.

The ``footprint_filename`` column in the output is the foreign key back to the
``filename`` column of the footprints layer.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon

from closeup_ortho.geometry import geotiff_affine

log = logging.getLogger("closeup_ortho")


def _bbox_polygon(
    top: float,
    left: float,
    height_px: float,
    width_px: float,
    img_w: int,
    img_h: int,
    x0: float,
    y0: float,
    gsd_m: float,
    yaw: float,
) -> Polygon:
    """Return the ground polygon for one Labelbox ImageBoundingBox.

    Labelbox bbox origin is the **top-left** of the image; ``top`` and ``left`` are
    pixel offsets from that origin. We push the four corners through the same affine
    used to write the GeoTIFF.
    """
    aff = geotiff_affine(x0, y0, gsd_m, img_w, img_h, yaw)
    corners_px = [
        (left, top),
        (left + width_px, top),
        (left + width_px, top + height_px),
        (left, top + height_px),
    ]
    return Polygon([aff * pt for pt in corners_px])


def project_labels(
    ndjson_path,
    footprints_gpkg,
    out_gpkg,
    footprints_layer: str = "footprints",
    out_layer: str = "labels",
) -> gpd.GeoDataFrame:
    """Project Labelbox bounding-box annotations to ground coordinates.

    Reads a Labelbox ndjson export and a footprints GeoPackage produced by
    :func:`closeup_ortho.orthorectify.orthorectify_folder`. For every bounding-box
    annotation whose photo filename matches a footprint, reprojects the bbox from image
    pixels to the footprint CRS and writes a ``labels`` layer to ``out_gpkg``.

    Parameters
    ----------
    ndjson_path : path
        Labelbox ndjson export file.
    footprints_gpkg : path
        GeoPackage with a footprints polygon layer (output of ``orthorectify_folder``).
    out_gpkg : path
        Destination GeoPackage; the labels are written to ``out_layer``.
    footprints_layer : str
        Name of the footprints layer inside ``footprints_gpkg``.
    out_layer : str
        Name of the labels layer to write inside ``out_gpkg``.

    Returns
    -------
    geopandas.GeoDataFrame
        The labels written, in the same CRS as the footprints.
    """
    ndjson_path = Path(ndjson_path)
    out_gpkg = Path(out_gpkg)

    footprints = gpd.read_file(footprints_gpkg, layer=footprints_layer)
    crs = footprints.crs
    # Index footprints by filename for O(1) lookup.
    fp_index = footprints.set_index("filename")

    records: list[dict] = []
    n_rows = n_matched = n_skipped = n_boxes = 0

    with open(ndjson_path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            n_rows += 1

            # Filename is the last segment of global_key (e.g. "folder/DJI_…tele.JPG").
            global_key: str = row["data_row"]["global_key"]
            filename = global_key.split("/")[-1]

            if filename not in fp_index.index:
                continue
            n_matched += 1

            fp = fp_index.loc[filename]
            # fp may be a Series (unique match) or a DataFrame (duplicate filenames).
            # Footprint filenames should be unique; take the first if not.
            if isinstance(fp, gpd.GeoDataFrame):
                fp = fp.iloc[0]

            x0 = fp.geometry.centroid.x
            y0 = fp.geometry.centroid.y
            gsd_m = float(fp["gsd_m"])
            yaw = float(fp["yaw"])
            img_w: int = row["media_attributes"]["width"]
            img_h: int = row["media_attributes"]["height"]

            datarow_id: str = row["data_row"]["id"]
            url: str = row["data_row"]["row_data"]
            dataset_name: str = row["data_row"]["details"].get("dataset_name", "")
            lb_created_by: str = row["data_row"]["details"].get("created_by", "")

            for proj_data in row["projects"].values():
                project_name: str = proj_data.get("name", "")
                workflow_status: str = proj_data.get("project_details", {}).get("workflow_status", "")

                for label in proj_data.get("labels", []):
                    label_id: str = label["id"]
                    label_created_by: str = label["label_details"].get("created_by", "")
                    label_created_at: str = label["label_details"].get("created_at", "")

                    for obj in label["annotations"].get("objects", []):
                        if obj.get("annotation_kind") != "ImageBoundingBox":
                            continue

                        bb = obj["bounding_box"]
                        try:
                            poly = _bbox_polygon(
                                top=bb["top"],
                                left=bb["left"],
                                height_px=bb["height"],
                                width_px=bb["width"],
                                img_w=img_w,
                                img_h=img_h,
                                x0=x0,
                                y0=y0,
                                gsd_m=gsd_m,
                                yaw=yaw,
                            )
                        except Exception as exc:
                            log.warning("SKIP bbox in %s: %s", filename, exc)
                            n_skipped += 1
                            continue

                        # Extract the taxon from the first radio classification.
                        taxon_name = taxon_value = None
                        for clf in obj.get("classifications", []):
                            ra = clf.get("radio_answer")
                            if ra:
                                taxon_name = ra.get("name")
                                taxon_value = ra.get("value")
                                break

                        records.append({
                            # Foreign key -> footprints layer
                            "footprint_filename": filename,
                            # Labelbox identifiers
                            "datarow_id": datarow_id,
                            "label_id": label_id,
                            "feature_id": obj.get("feature_id"),
                            # Source URL of the photo on the object store
                            "url": url,
                            # Annotation content
                            "object_name": obj.get("name"),
                            "object_value": obj.get("value"),
                            "taxon_name": taxon_name,
                            "taxon_value": taxon_value,
                            # Bbox in image pixels (preserved for reference)
                            "bbox_top_px": bb["top"],
                            "bbox_left_px": bb["left"],
                            "bbox_height_px": bb["height"],
                            "bbox_width_px": bb["width"],
                            # Provenance
                            "dataset_name": dataset_name,
                            "project_name": project_name,
                            "workflow_status": workflow_status,
                            "label_created_by": label_created_by,
                            "label_created_at": label_created_at,
                            "lb_created_by": lb_created_by,
                            "geometry": poly,
                        })
                        n_boxes += 1

    if not records:
        raise RuntimeError(
            "No bounding boxes projected. Check that the ndjson filenames overlap "
            "with the footprints layer."
        )

    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_gpkg, layer=out_layer, driver="GPKG")

    log.info(
        "Done: %d ndjson rows, %d matched footprints, %d bounding boxes written, "
        "%d skipped. Labels -> %s",
        n_rows, n_matched, n_boxes, n_skipped, out_gpkg,
    )
    return gdf
