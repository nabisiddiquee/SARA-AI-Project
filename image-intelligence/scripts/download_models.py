"""Download all model files ahead of time (so the first request isn't slow).

    python scripts/download_models.py

* YuNet face detector (OpenCV Zoo, ~230 KB)  -> models/face_detection_yunet_2023mar.onnx
* YOLOv8n weights (Ultralytics, ~6 MB)         -> models/yolov8n.pt          (if ultralytics is installed)
* EasyOCR detection + recognition models       -> models/easyocr/            (if easyocr is installed)
"""

from __future__ import annotations

import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import settings  # noqa: E402

YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
             "face_detection_yunet_2023mar.onnx")


def main() -> None:
    models = settings.models_dir
    models.mkdir(parents=True, exist_ok=True)

    target = models / settings.yunet_model
    if target.is_file():
        print(f"[ok] YuNet already present: {target}")
    else:
        print("[..] Downloading YuNet face detector...")
        urllib.request.urlretrieve(YUNET_URL, target)
        print(f"[ok] {target} ({target.stat().st_size // 1024} KB)")

    try:
        from ultralytics import YOLO  # type: ignore

        weights = models / settings.yolo_model
        if weights.is_file():
            print(f"[ok] YOLO weights already present: {weights}")
        else:
            print(f"[..] Downloading {settings.yolo_model}...")
            YOLO(settings.yolo_model)                      # downloads into the working directory
            src = Path(settings.yolo_model)
            if src.is_file():
                shutil.move(str(src), weights)
            print(f"[ok] {weights}")
    except ImportError:
        print("[skip] ultralytics not installed (pip install ultralytics)")

    try:
        import easyocr  # type: ignore

        print(f"[..] Preparing EasyOCR models for {settings.ocr_languages}...")
        easyocr.Reader(list(settings.ocr_languages), gpu=False,
                       model_storage_directory=str(models / "easyocr"))
        print(f"[ok] EasyOCR models in {models / 'easyocr'}")
    except ImportError:
        print("[skip] easyocr not installed (pip install easyocr)")


if __name__ == "__main__":
    main()
