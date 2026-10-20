"""Face DETECTION only (where are faces, how many). No recognition, no matching.

Engine 1: OpenCV YuNet (accurate, small ONNX model in models/, see README).
Engine 2: OpenCV Haar cascade (bundled with OpenCV, used when YuNet is missing).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
import numpy as np

from app.config import settings

log = logging.getLogger(__name__)


class FaceDetector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._yunet = None
        self._haar = None
        self.engine = "none"
        model = settings.models_dir / settings.yunet_model
        if model.is_file() and hasattr(cv2, "FaceDetectorYN"):
            try:
                self._yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7, 0.3, 5000)
                self.engine = "opencv-yunet"
            except cv2.error as e:
                log.warning("YuNet failed to load (%s); using Haar cascade", e)
        if self._yunet is None:
            data = getattr(getattr(cv2, "data", None), "haarcascades", "")
            path = data + "haarcascade_frontalface_default.xml"
            if data and Path(path).is_file():
                self._haar = cv2.CascadeClassifier(path)
                self.engine = "opencv-haar" if not self._haar.empty() else "none"
            else:
                log.warning("No face model: run scripts/download_models.py (YuNet) or use OpenCV 4.x (Haar)")

    def detect(self, bgr: np.ndarray) -> list[dict]:
        h, w = bgr.shape[:2]
        if self._yunet is not None:
            # YuNet works best around 640 px; scale boxes back afterwards.
            scale = min(1.0, 960 / max(h, w))
            img = cv2.resize(bgr, (int(w * scale), int(h * scale))) if scale < 1 else bgr
            with self._lock:
                self._yunet.setInputSize((img.shape[1], img.shape[0]))
                _, faces = self._yunet.detect(img)
            out = []
            for f in faces if faces is not None else []:
                x, y, fw, fh = (f[:4] / scale).tolist()
                out.append({"label": "face", "confidence": round(float(f[-1]), 3),
                            "bbox": _box(x, y, fw, fh, w, h)})
            return out
        if self._haar is not None and not self._haar.empty():
            gray = cv2.equalizeHist(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))
            min_side = max(24, int(min(h, w) * 0.04))
            rects = self._haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(min_side, min_side))
            return [{"label": "face", "confidence": None, "bbox": _box(x, y, fw, fh, w, h)}
                    for (x, y, fw, fh) in (rects if len(rects) else [])]
        return []


def _box(x, y, bw, bh, w, h) -> dict:
    x0, y0 = max(0, int(round(x))), max(0, int(round(y)))
    return {"x": x0, "y": y0, "width": int(min(w - x0, round(bw))), "height": int(min(h - y0, round(bh)))}
