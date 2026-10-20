"""OCR: read every visible line of text (EasyOCR), with boxes and confidence.

EasyOCR downloads its detection + recognition models (~100 MB) on first use,
or ahead of time with scripts/download_models.py. Set OCR_ENGINE=none to skip.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from app.config import settings
from app.utils.image_utils import prepare_for_ocr

log = logging.getLogger(__name__)


class OCRService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reader = None
        self.engine = "none"
        self.load_error = ""
        if settings.ocr_engine.lower() == "none":
            self.load_error = "OCR disabled (OCR_ENGINE=none)"
            return
        try:
            import easyocr  # type: ignore

            self._reader = easyocr.Reader(list(settings.ocr_languages), gpu=False, verbose=False,
                                          model_storage_directory=str(settings.models_dir / "easyocr"))
            self.engine = "easyocr"
        except Exception as e:  # noqa: BLE001
            self.load_error = f"OCR unavailable ({type(e).__name__}: {e}); text extraction skipped"
            log.warning(self.load_error)

    @property
    def available(self) -> bool:
        return self._reader is not None

    def read(self, bgr: np.ndarray) -> list[dict]:
        """Lines of text, top-to-bottom: [{text, confidence, bbox, height}]."""
        if self._reader is None:
            return []
        prepared = prepare_for_ocr(bgr)
        fx = bgr.shape[1] / prepared.shape[1]
        fy = bgr.shape[0] / prepared.shape[0]
        with self._lock:
            raw = self._reader.readtext(prepared, detail=1, paragraph=False)
        words = []
        for pts, text, conf in raw:
            text = str(text).strip()
            if not text or float(conf) < settings.ocr_min_confidence:
                continue
            xs = [p[0] * fx for p in pts]
            ys = [p[1] * fy for p in pts]
            words.append({"text": text, "confidence": float(conf),
                          "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys)})
        return _group_lines(words)


def _group_lines(words: list[dict]) -> list[dict]:
    """Join word boxes that sit on the same line (vertical overlap), left to right."""
    words.sort(key=lambda w: (w["y0"] + w["y1"]) / 2)
    lines: list[list[dict]] = []
    for w in words:
        cy = (w["y0"] + w["y1"]) / 2
        for line in lines:
            ly0 = min(x["y0"] for x in line)
            ly1 = max(x["y1"] for x in line)
            if ly0 <= cy <= ly1 and abs((w["y1"] - w["y0"]) - (ly1 - ly0)) < 0.6 * (ly1 - ly0):
                line.append(w)
                break
        else:
            lines.append([w])
    out = []
    for line in lines:
        line.sort(key=lambda w: w["x0"])
        x0, y0 = min(w["x0"] for w in line), min(w["y0"] for w in line)
        x1, y1 = max(w["x1"] for w in line), max(w["y1"] for w in line)
        out.append({
            "text": " ".join(w["text"] for w in line),
            "confidence": round(sum(w["confidence"] for w in line) / len(line), 3),
            "bbox": {"x": int(x0), "y": int(y0), "width": int(x1 - x0), "height": int(y1 - y0)},
            "height": y1 - y0,
        })
    out.sort(key=lambda l: (l["bbox"]["y"], l["bbox"]["x"]))
    return out
