"""Settings from environment variables / .env (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env = BASE_DIR / ".env"
    if not env.is_file():
        return
    for raw in env.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0]                 # allow "KEY=value   # comment"
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    max_upload_mb: float = float(os.environ.get("MAX_UPLOAD_MB", "15"))
    max_pixels: int = int(os.environ.get("MAX_PIXELS", str(40_000_000)))   # decompression-bomb guard
    save_uploads: bool = _bool("SAVE_UPLOADS", False)                       # images are NOT kept by default
    upload_dir: Path = BASE_DIR / os.environ.get("UPLOAD_DIR", "uploads")
    models_dir: Path = BASE_DIR / os.environ.get("MODELS_DIR", "models")

    ocr_engine: str = os.environ.get("OCR_ENGINE", "easyocr")              # easyocr | none
    ocr_languages: tuple[str, ...] = tuple(
        x.strip() for x in os.environ.get("OCR_LANGUAGES", "en").split(",") if x.strip())
    ocr_min_confidence: float = float(os.environ.get("OCR_MIN_CONFIDENCE", "0.30"))

    yolo_model: str = os.environ.get("YOLO_MODEL", "yolov8n.pt")
    yolo_confidence: float = float(os.environ.get("YOLO_CONFIDENCE", "0.35"))
    yunet_model: str = os.environ.get("YUNET_MODEL", "face_detection_yunet_2023mar.onnx")

    include_gps: bool = _bool("INCLUDE_GPS", False)    # GPS is sensitive: only "present: yes/no" by default
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    allowed_formats: tuple[str, ...] = field(default=("JPEG", "PNG", "WEBP"))


settings = Settings()
