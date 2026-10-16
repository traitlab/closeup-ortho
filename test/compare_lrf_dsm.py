"""Compare camera-to-ground distance from the M4E laser rangefinder (LRF) vs the DSM.

For each close-up photo, compute the distance used to size the footprint two ways:

* ``dsm_dist``  — the pipeline's method: ``AbsoluteAltitude - max(DSM in auto buffer)``
  (also ``dsm_center_dist`` from the single DSM pixel under the camera);
* ``lrf_dist``  — ``drone-dji:LRFTargetDistance``, the M4E laser range along the camera
  axis (≈ vertical distance for a nadir gimbal).

Footprint width/height and GSD scale linearly with distance, so ``lrf_dist / dsm_dist - 1``
is directly the relative footprint-size error between the two methods.

Only ``LRFTargetDistance`` is used: ``LRFTargetAbsAlt`` / ``LRFTargetLat/Lon`` are derived
from the drone's on-board position and are *not* updated by the PPK shift script.

Read-only: inputs are never modified. Usage::

    python test/compare_lrf_dsm.py --out /path/to/lrf_vs_dsm.csv
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd
from PIL import ExifTags, Image

from closeup_ortho.dsm import DSM
from closeup_ortho.geometry import compute_footprint
from closeup_ortho.metadata import camera_of
from closeup_ortho.orthorectify import _auto_buffer_radius

PHOTO_GLOB = "/mnt/nfs/lefodata/upload/tmp/plaisance"
MISSION_PATTERN = "20260924_pnp*_wpt*_m4e"
DSM_PATH = (
    "/mnt/nfs/conrad/labolaliberte_data/metashape/2026/20260924_pnpperras_p1/"
    "20260924_pnpperras_p1_dsm.cog.tif"
)

# DJI writes XMP as attributes; exiftool-rewritten files (e.g. after the PPK shift) use
# elements. Accept both.
_ATTR_RE = re.compile(rb'drone-dji:(\w+)\s*=\s*"([^"]*)"')
_ELEM_RE = re.compile(rb"<drone-dji:(\w+)>([^<]*)</drone-dji:\1>")


def read_xmp(path: Path) -> dict[str, str]:
    with open(path, "rb") as fh:
        head = fh.read(262144)
    out = {m.group(1).decode(): m.group(2).decode() for m in _ATTR_RE.finditer(head)}
    out.update({m.group(1).decode(): m.group(2).decode() for m in _ELEM_RE.finditer(head)})
    return out


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return np.nan


def process_photo(path: Path, dsm: DSM) -> dict | None:
    x = read_xmp(path)
    with Image.open(path) as im:
        w, h = im.size
        fl35 = im.getexif().get_ifd(ExifTags.IFD.Exif).get(0xA405)
    fl35 = float(fl35) if fl35 is not None else None
    cam = camera_of(path, x.get("ImageSource"), fl35)
    lat, lon, alt = f(x.get("GpsLatitude")), f(x.get("GpsLongitude")), f(x.get("AbsoluteAltitude"))
    if np.isnan([lat, lon, alt]).any() or cam is None:
        return None
    calib = f(x.get("CalibratedFocalLength"))
    calib = None if np.isnan(calib) else calib

    rec = {
        "mission": path.parts[-3] if len(path.parts) >= 3 else "",
        "filename": path.name,
        "camera": cam,
        "lat": lat,
        "lon": lon,
        "abs_alt": alt,
        "pitch": f(x.get("GimbalPitchDegree")),
        "lrf_status": x.get("LRFStatus"),
        "lrf_dist": f(x.get("LRFTargetDistance")),
        "lrf_target_abs_alt": f(x.get("LRFTargetAbsAlt")),
        "rel_alt": f(x.get("RelativeAltitude")),
    }

    x0, y0 = dsm.lonlat_to_xy(lon, lat)
    center = dsm.max_elevation(x0, y0, 0.0)
    rec["dsm_center_elev"] = center
    if center is None:
        return rec
    seed = compute_footprint(alt - center, w, h, fl35, calib, cam)
    radius = _auto_buffer_radius(seed.width_m, seed.height_m)
    dsm_max = dsm.max_elevation(x0, y0, radius)
    rec.update(
        buffer_radius_m=radius,
        dsm_max_elev=dsm_max,
        dsm_center_dist=alt - center,
        dsm_dist=alt - dsm_max,
        # Ground elevation implied by the laser, comparable with the DSM values.
        lrf_ground_elev=alt - rec["lrf_dist"],
        footprint_w_dsm=compute_footprint(alt - dsm_max, w, h, fl35, calib, cam).width_m,
        footprint_w_lrf=(
            compute_footprint(rec["lrf_dist"], w, h, fl35, calib, cam).width_m
            if rec["lrf_dist"] > 0 else np.nan
        ),
    )
    return rec


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    d = df.dropna(subset=["dsm_dist", "lrf_dist"]).copy()
    d["diff_m"] = d["lrf_dist"] - d["dsm_dist"]
    d["diff_center_m"] = d["lrf_dist"] - d["dsm_center_dist"]
    d["rel_pct"] = 100 * (d["lrf_dist"] / d["dsm_dist"] - 1)
    g = d.groupby(["mission", "camera"])
    return pd.DataFrame({
        "n": g.size(),
        "dsm_dist_med": g["dsm_dist"].median(),
        "lrf_dist_med": g["lrf_dist"].median(),
        "diff_med_m": g["diff_m"].median(),
        "diff_mad_m": g["diff_m"].apply(lambda s: (s - s.median()).abs().median()),
        "diff_p5_m": g["diff_m"].quantile(0.05),
        "diff_p95_m": g["diff_m"].quantile(0.95),
        "diff_ctr_med_m": g["diff_center_m"].median(),
        "rel_med_pct": g["rel_pct"].median(),
        "rel_absmax_pct": g["rel_pct"].apply(lambda s: s.abs().max()),
    }).round(3)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--photo-root", default=PHOTO_GLOB)
    ap.add_argument("--pattern", default=MISSION_PATTERN)
    ap.add_argument("--dsm", default=DSM_PATH)
    ap.add_argument("--out", required=True, help="Per-photo CSV output")
    args = ap.parse_args()

    missions = sorted(Path(args.photo_root).glob(args.pattern))
    print(f"{len(missions)} missions: {[m.name for m in missions]}")
    rows = []
    with DSM(args.dsm) as dsm:
        for m in missions:
            for p in sorted(m.rglob("*.JPG")):
                r = process_photo(p, dsm)
                if r is not None:
                    r["mission"] = m.name
                    rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(args.out, index=False)
    print(f"{len(df)} photos -> {args.out}")
    print("\nLRFStatus counts:\n", df.groupby(["mission", "lrf_status"], dropna=False).size())
    print("\nNo DSM under camera:", df["dsm_center_elev"].isna().sum())
    pd.set_option("display.width", 250)
    print("\nlrf_dist - dsm_dist (m); rel_pct = footprint-size change using LRF instead of DSM\n")
    print(summarize(df).to_string())


if __name__ == "__main__":
    main()
