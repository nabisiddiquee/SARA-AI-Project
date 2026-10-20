"""Safe EXIF metadata: only what is actually stored in the file.

GPS coordinates are sensitive, so by default only "gps_present" is reported;
set INCLUDE_GPS=true to return the coordinates too.
"""

from __future__ import annotations

from PIL import ExifTags, Image

from app.config import settings

_TAGS = {v: k for k, v in ExifTags.TAGS.items()}


def _clean(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", errors="ignore")
    if isinstance(v, str):
        v = v.replace("\x00", "").strip()
        return v or None
    return v


def _gps_decimal(gps: dict) -> dict | None:
    try:
        def conv(vals, ref):
            d, m, s = (float(x) for x in vals)
            dec = d + m / 60 + s / 3600
            return -dec if ref in ("S", "W") else dec
        return {"latitude": round(conv(gps[2], gps[1]), 6), "longitude": round(conv(gps[4], gps[3]), 6)}
    except Exception:  # noqa: BLE001
        return None


def extract(img: Image.Image) -> dict:
    out = {"exif_present": False, "gps_present": False, "gps": None}
    try:
        exif = img.getexif()
    except Exception:  # noqa: BLE001
        return out
    if not exif:
        return out
    out["exif_present"] = True
    sub = {}
    try:
        sub = exif.get_ifd(0x8769)          # Exif sub-IFD (capture date, lens)
    except Exception:  # noqa: BLE001
        pass
    get = lambda name: _clean(exif.get(_TAGS[name]) or sub.get(_TAGS[name]))  # noqa: E731
    out["camera_make"] = get("Make")
    out["camera_model"] = get("Model")
    out["capture_date"] = get("DateTimeOriginal") or get("DateTime")
    out["software"] = get("Software")
    out["lens_model"] = get("LensModel")
    orient = exif.get(_TAGS["Orientation"])
    out["exif_orientation"] = int(orient) if orient else None
    try:
        gps = exif.get_ifd(0x8825)
    except Exception:  # noqa: BLE001
        gps = {}
    if gps:
        out["gps_present"] = True
        if settings.include_gps:
            out["gps"] = _gps_decimal(gps)
    return out
