"""Object + person detection.

Engine 1: Ultralytics YOLO (80 COCO classes: person, laptop, cell phone, car,
          chair, dining table, bottle, tv/monitor, book, backpack, handbag ...).
          The weights (yolov8n.pt, ~6 MB) download automatically on first use,
          or run scripts/download_models.py.
Engine 2: OpenCV HOG people detector (built in): persons only, when YOLO isn't installed.
"""

from __future__ import annotations

import logging
import os
import threading

import cv2

# Ultralytics otherwise runs `pip install` for missing extras at request time;
# a server must only use what requirements.txt installed.
os.environ.setdefault("YOLO_AUTOINSTALL", "false")
import numpy as np

from app.config import settings

log = logging.getLogger(__name__)

# Friendlier names for some COCO labels.
_RENAME = {"tv": "monitor/tv", "cell phone": "mobile phone", "dining table": "table"}


class ObjectDetector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._yolo = None
        self._hog = None
        self.engine = "none"
        self.load_error = ""
        try:
            from ultralytics import YOLO  # type: ignore

            weights = settings.models_dir / settings.yolo_model
            self._yolo = YOLO(str(weights) if weights.is_file() else settings.yolo_model)
            self.engine = f"ultralytics-{settings.yolo_model}"
        except Exception as e:  # noqa: BLE001 - not installed / no weights / offline
            self.load_error = f"YOLO unavailable ({type(e).__name__}: {e}); persons via OpenCV HOG, other objects skipped"
            log.warning(self.load_error)
            if hasattr(cv2, "HOGDescriptor_getDefaultPeopleDetector"):
                self._hog = cv2.HOGDescriptor()
                self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
                self.engine = "opencv-hog (persons only)"
            else:
                self.load_error = f"YOLO unavailable ({type(e).__name__}); no person/object detection (install ultralytics)"

    @property
    def full_objects(self) -> bool:
        return self._yolo is not None

    def detect(self, bgr: np.ndarray) -> list[dict]:
        h, w = bgr.shape[:2]
        if self._yolo is not None:
            with self._lock:
                result = self._yolo.predict(bgr, conf=settings.yolo_confidence, verbose=False)[0]
            out = []
            for box, cls, conf in zip(result.boxes.xyxy.tolist(), result.boxes.cls.tolist(), result.boxes.conf.tolist()):
                name = result.names[int(cls)]
                x1, y1, x2, y2 = box
                out.append({"label": _RENAME.get(name, name), "confidence": round(float(conf), 3),
                            "bbox": {"x": int(x1), "y": int(y1), "width": int(x2 - x1), "height": int(y2 - y1)}})
            return sorted(out, key=lambda d: -d["confidence"])
        if self._hog is not None:
            scale = min(1.0, 800 / max(h, w))
            img = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1 else bgr
            rects, weights = self._hog.detectMultiScale(img, winStride=(8, 8), padding=(8, 8), scale=1.05)
            boxes, scores = _nms(rects, weights)
            return [{"label": "person", "confidence": round(min(1.0, float(s) / 2), 3),
                     "bbox": {"x": int(x / scale), "y": int(y / scale), "width": int(bw / scale), "height": int(bh / scale)}}
                    for (x, y, bw, bh), s in zip(boxes, scores) if s > 0.5]
        return []


def _nms(rects, weights, thr: float = 0.4):
    if len(rects) == 0:
        return [], []
    boxes = [list(map(int, r)) for r in rects]
    scores = [float(s) for s in np.ravel(weights)]
    keep = cv2.dnn.NMSBoxes(boxes, scores, 0.0, thr)
    keep = np.ravel(keep).tolist() if len(keep) else []
    return [boxes[i] for i in keep], [scores[i] for i in keep]
