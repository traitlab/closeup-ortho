# closeup-ortho

Orthorectify drone close-up photos and associated labels, if available.

## What it does

`closeup-ortho` performs a deliberately **simple planar projection** (not full
orthorectification) of DJI nadir close-up photos. Each photo is placed on the ground as a
rectangle whose:

- **position** comes from the photo's RTK GPS (WGS84 lat/lon),
- **heading** comes from the gimbal yaw,
- **size** comes from the camera-to-ground distance, computed as
  `AbsoluteAltitude − max(DSM elevation in a small buffer below the camera)`, combined with
  the lens geometry (calibrated focal length for the wide camera, `FocalLengthIn35mmFilm`
  for the tele camera).

It assumes the photos are taken straight down (gimbal pitch ≈ −90°) and that the DSM is in
**ellipsoidal** heights, matching the drone's `AbsoluteAltitude`.

Two commands are provided:

1. **`orthorectify`** — projects photos onto a DSM. Outputs a GeoPackage polygon layer
   (`footprints`) and optionally georeferenced COG GeoTIFFs.
2. **`labels`** — reprojects Labelbox bounding-box annotations (from an ndjson export) into
   the same ground CRS, linked back to the footprints layer.

All outputs use the **CRS of the input DSM**.

## Install

```bash
python -m venv .venv
# Windows
.venv\Scripts\python -m pip install -e .
# macOS/Linux
.venv/bin/python -m pip install -e .
```

## Usage

### 1 — Project photos onto the DSM

Command line (tele photos by default):

```bash
closeup-ortho orthorectify \
  --photos   data/examples/enearehab1/closeup/20260127_enearehab1_wpt169_m3e \
  --dsm      data/examples/enearehab1/dsm/20260225_enearehab1_10m169_m3e_aoi.tif \
  --out      out/examples/enearehab1/footprints.gpkg \
  --geotiffs out/examples/enearehab1/tiff
```

Options:
- `--lenses tele` (default), `wide`, `tele,wide`, or `all`
- `--geotiffs DIR` also writes projected COG GeoTIFFs
- `--buffer-radius M` overrides the auto DSM sampling buffer (meters)
- `--no-recursive` to scan only the top folder

Python:

```python
from closeup_ortho import orthorectify_folder

gdf = orthorectify_folder(
    photo_dir="data/examples/enearehab1/closeup/20260127_enearehab1_wpt169_m3e",
    dsm_path="data/examples/enearehab1/dsm/20260225_enearehab1_10m169_m3e_aoi.tif",
    out_gpkg="out/examples/enearehab1/footprints.gpkg",
    lenses={"tele"},
    write_geotiffs=True,
    geotiff_dir="out/examples/enearehab1/tiff",
)
```

### 2 — Project Labelbox annotations

Command line:

```bash
closeup-ortho labels \
  --ndjson     "data/examples/enearehab1/labelbox/2025_wa_roberge - 6_12_2026.ndjson" \
  --footprints out/examples/enearehab1/footprints.gpkg \
  --out        out/examples/enearehab1/labels.gpkg
```

Python:

```python
from closeup_ortho import project_labels

gdf = project_labels(
    ndjson_path="data/examples/enearehab1/labelbox/2025_wa_roberge - 6_12_2026.ndjson",
    footprints_gpkg="out/examples/enearehab1/footprints.gpkg",
    out_gpkg="out/examples/enearehab1/labels.gpkg",
)
```

The output layer contains one polygon per bounding box. Join it back to footprints via the
`footprint_filename` → `filename` columns.

## Footprint layer attributes (`footprints`)

| column | meaning |
|---|---|
| `path` | full path to the photo |
| `filename` | filename with extension |
| `directory` | parent directory of the photo |
| `camera` | `tele` or `wide` |
| `longitude`, `latitude` | camera position, WGS84 |
| `abs_alt_m` | AbsoluteAltitude (ellipsoidal), meters |
| `dist_m` | camera-to-ground distance used for projection, meters |
| `gsd_m` | ground sample distance, meters/pixel |
| `footprint_w_m`, `footprint_h_m` | footprint dimensions on the ground, meters |
| `yaw`, `pitch`, `roll` | gimbal angles, degrees |
| `nadir_ok` | gimbal within nadir tolerance |
| `focal_source` | `calib` (wide CalibratedFocalLength) or `fl35` (FocalLengthIn35mmFilm) |
| `timestamp` | capture time |

## Label layer attributes (`labels`)

| column | meaning |
|---|---|
| `footprint_filename` | foreign key → `filename` in the footprints layer |
| `datarow_id` | Labelbox data row ID |
| `label_id` | Labelbox label ID |
| `feature_id` | Labelbox feature ID for this bounding box |
| `url` | full URL of the source JPEG on the object store |
| `object_name` | annotation class name (e.g. `Plants`) |
| `object_value` | annotation class value (e.g. `plants`) |
| `taxon_name` | taxon name from the radio classification |
| `taxon_value` | taxon ID (PlantNet ID) |
| `bbox_top_px`, `bbox_left_px`, `bbox_height_px`, `bbox_width_px` | original bbox in image pixels |
| `dataset_name` | Labelbox dataset name |
| `project_name` | Labelbox project name |
| `workflow_status` | Labelbox workflow status (e.g. `IN_REVIEW`, `TO_LABEL`) |
| `label_created_by` | annotator email |
| `label_created_at` | annotation timestamp |
| `lb_created_by` | data row creator email |

## Not yet implemented

- Using the Matrice 4 Enterprise laser rangefinder for the distance term (currently
  DSM-derived).
- Handling photos that fall outside the DSM extent (currently skipped with a warning).
