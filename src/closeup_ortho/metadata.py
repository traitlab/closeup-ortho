"""Read the EXIF + DJI XMP metadata needed for planar projection.

DJI stores most of what we need in an XMP packet (``drone-dji`` namespace) embedded
tens of kilobytes into the JPEG, plus a couple of standard EXIF tags. Pillow's
``getxmp`` requires ``defusedxml`` and silently returns nothing without it, so we parse
the XMP attributes directly from the file bytes with a small regex. The two values that
live in EXIF instead of XMP (``FocalLengthIn35mmFilm`` and the capture timestamp) and the
pixel dimensions are read with Pillow.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import ExifTags, Image

# How far into the file to scan for the XMP packet. DJI writes it after the thumbnail,
# observed around 32-40 KB in; 256 KB is a generous safety margin without reading whole
# multi-megabyte files.
_XMP_SCAN_BYTES = 262144

_ATTR_RE = re.compile(rb'drone-dji:(\w+)\s*=\s*"([^"]*)"')


@dataclass
class PhotoMeta:
    """Metadata for one close-up photo, in the units used downstream."""

    path: Path
    camera: str  # "tele", "wide" or "med"
    latitude: float  # WGS84 decimal degrees
    longitude: float  # WGS84 decimal degrees
    abs_alt_m: float  # AbsoluteAltitude, WGS84 ellipsoidal, meters
    yaw: float  # GimbalYawDegree, clockwise from North
    pitch: float  # GimbalPitchDegree (~ -90 for nadir)
    roll: float  # GimbalRollDegree (~ 0)
    width_px: int
    height_px: int
    fl35_mm: float | None  # FocalLengthIn35mmFilm
    calib_focal_px: float | None  # drone-dji:CalibratedFocalLength (wide only)
    timestamp: str | None  # EXIF DateTimeOriginal


def _read_xmp_attrs(path: Path) -> dict[str, str]:
    """Return the ``drone-dji`` XMP attributes of a JPEG as a flat string dict."""
    with open(path, "rb") as fh:
        head = fh.read(_XMP_SCAN_BYTES)
    return {m.group(1).decode(): m.group(2).decode() for m in _ATTR_RE.finditer(head)}


def _to_float(value: str | None) -> float | None:
    """Parse a DJI numeric string (often prefixed with ``+``) to float."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def camera_of(path: Path, image_source: str | None, fl35_mm: float | None = None) -> str | None:
    """Classify a photo as ``"tele"``, ``"wide"`` or ``"med"``.

    Trust order: DJI filename suffix (``tele``/``zoom`` = tele, ``wide``, ``med``), then
    the 35mm-equivalent focal length (~24 wide / 70 med / 160+ tele), then the XMP
    ``ImageSource`` — least trusted, since the M4E stamps ``ZoomCamera`` on all three of
    its cameras and individual frames can carry a wrong ``ImageSource``.
    """
    stem = path.stem.lower()
    if stem.endswith("tele") or stem.endswith("zoom"):
        return "tele"
    if stem.endswith("wide"):
        return "wide"
    if stem.endswith("med"):
        return "med"
    if fl35_mm:
        if fl35_mm <= 35:
            return "wide"
        return "med" if fl35_mm < 100 else "tele"
    if image_source == "ZoomCamera":
        return "tele"
    if image_source == "WideCamera":
        return "wide"
    return None


def read_position(path: Path) -> tuple[float, float, float] | None:
    """Read only ``(latitude, longitude, abs_alt_m)`` from a photo's XMP.

    Cheap (no Pillow, only the XMP head scan); used to borrow the RTK position of a
    wide frame for its tele/med siblings. Returns ``None`` if any field is missing.
    """
    xmp = _read_xmp_attrs(Path(path))
    lat = _to_float(xmp.get("GpsLatitude"))
    lon = _to_float(xmp.get("GpsLongitude"))
    abs_alt = _to_float(xmp.get("AbsoluteAltitude"))
    if None in (lat, lon, abs_alt):
        return None
    return lat, lon, abs_alt


def read_photo_meta(path: Path) -> PhotoMeta | None:
    """Read all projection-relevant metadata from one photo.

    Returns ``None`` if the file lacks the GPS/altitude/gimbal fields required for
    projection (e.g. a non-DJI image or a corrupt file).
    """
    path = Path(path)
    xmp = _read_xmp_attrs(path)

    lat = _to_float(xmp.get("GpsLatitude"))
    lon = _to_float(xmp.get("GpsLongitude"))
    abs_alt = _to_float(xmp.get("AbsoluteAltitude"))
    yaw = _to_float(xmp.get("GimbalYawDegree"))
    pitch = _to_float(xmp.get("GimbalPitchDegree"))
    roll = _to_float(xmp.get("GimbalRollDegree"))
    if None in (lat, lon, abs_alt, yaw, pitch, roll):
        return None

    calib_focal = _to_float(xmp.get("CalibratedFocalLength"))

    with Image.open(path) as im:
        width_px, height_px = im.size
        exif = im.getexif()
        exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
    fl35 = exif_ifd.get(0xA405)  # FocalLengthIn35mmFilm
    fl35_mm = float(fl35) if fl35 is not None else None
    timestamp = exif_ifd.get(0x9003)  # DateTimeOriginal

    camera = camera_of(path, xmp.get("ImageSource"), fl35_mm)
    if camera is None:
        return None

    return PhotoMeta(
        path=path,
        camera=camera,
        latitude=lat,
        longitude=lon,
        abs_alt_m=abs_alt,
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        width_px=int(width_px),
        height_px=int(height_px),
        fl35_mm=fl35_mm,
        calib_focal_px=calib_focal,
        timestamp=timestamp,
    )
