"""Upload validation, decoding, basic info and image-quality measures (OpenCV + Pillow)."""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "application/octet-stream"}


class ImageValidationError(ValueError):
    """The upload is not a usable JPG / PNG / WEBP image."""


@dataclass
class LoadedImage:
    pil: Image.Image            # EXIF-orientation corrected, RGB
    bgr: np.ndarray             # same pixels for OpenCV
    raw_pil: Image.Image        # original (for EXIF)
    file_format: str
    file_size: int
    mode: str


def load_image(data: bytes, filename: str = "", content_type: str = "") -> LoadedImage:
    """Validate and decode an uploaded image. Raises ImageValidationError."""
    if not data:
        raise ImageValidationError("Empty file.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise ImageValidationError(f"File too large (max {settings.max_upload_mb:g} MB).")
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ImageValidationError(f"Unsupported file type '{ext}'. Use JPG, JPEG, PNG or WEBP.")
    if content_type and content_type.lower() not in ALLOWED_CONTENT_TYPES:
        raise ImageValidationError(f"Unsupported content type '{content_type}'.")

    Image.MAX_IMAGE_PIXELS = settings.max_pixels
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()                                   # detects truncated / corrupted files
        img = Image.open(io.BytesIO(data))
        img.load()
    except Image.DecompressionBombError as e:
        raise ImageValidationError("Image has too many pixels to process safely.") from e
    except (UnidentifiedImageError, OSError, SyntaxError) as e:
        # Pillow's message names internal objects (e.g. <_io.BytesIO ...>): keep it out of the API.
        raise ImageValidationError("Corrupted or unreadable image file.") from e

    fmt = (img.format or "").upper()
    if fmt not in settings.allowed_formats:
        raise ImageValidationError(f"Unsupported image format '{fmt or 'unknown'}'. Use JPG, PNG or WEBP.")

    upright = ImageOps.exif_transpose(img)               # respect camera rotation
    rgb = upright.convert("RGB")
    bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
    return LoadedImage(pil=rgb, bgr=bgr, raw_pil=img, file_format=fmt, file_size=len(data), mode=img.mode)


def channels_for(mode: str) -> int:
    return {"1": 1, "L": 1, "P": 1, "LA": 2, "RGB": 3, "YCbCr": 3, "LAB": 3, "HSV": 3,
            "RGBA": 4, "CMYK": 4, "I": 1, "F": 1}.get(mode, 3)


def orientation(width: int, height: int) -> str:
    if abs(width - height) <= 0.02 * max(width, height):
        return "square"
    return "landscape" if width > height else "portrait"


def quality_metrics(bgr: np.ndarray) -> dict:
    """Blur (Laplacian variance), brightness (mean), contrast (std), resolution."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    # Measure blur on a normalised size so big and small images compare fairly.
    scale = 1000 / max(h, w) if max(h, w) > 1000 else 1.0
    g = cv2.resize(gray, (int(w * scale), int(h * scale))) if scale != 1.0 else gray
    blur_var = float(cv2.Laplacian(g, cv2.CV_64F).var())
    brightness = float(gray.mean())
    contrast = float(gray.std())
    mp = w * h / 1_000_000

    return {
        "blur": "high" if blur_var < 60 else "medium" if blur_var < 150 else "low",
        "blur_score": round(blur_var, 1),
        "brightness": "dark" if brightness < 70 else "bright" if brightness > 190 else "normal",
        "brightness_value": round(brightness, 1),
        "contrast": "low" if contrast < 35 else "high" if contrast > 75 else "normal",
        "contrast_value": round(contrast, 1),
        "resolution": "low" if mp < 0.3 else "medium" if mp < 1.5 else "high",
        "megapixels": round(mp, 2),
    }


def ocr_readability(ocr_items: list[dict], ocr_ran: bool) -> str:
    if not ocr_ran:
        return "not_checked"
    if not ocr_items:
        return "no_text"
    avg = sum(i["confidence"] for i in ocr_items) / len(ocr_items)
    return "good" if avg >= 0.75 else "fair" if avg >= 0.5 else "poor"


def prepare_for_ocr(bgr: np.ndarray) -> np.ndarray:
    """Upscale small images and boost local contrast; OCR reads small text better."""
    h, w = bgr.shape[:2]
    if max(h, w) < 1000:
        f = 1000 / max(h, w)
        bgr = cv2.resize(bgr, (int(w * f), int(h * f)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
