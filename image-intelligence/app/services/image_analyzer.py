"""Runs the whole pipeline on one image and builds the JSON response.

    upload -> validate/decode (Pillow) -> OpenCV array
           -> quality (OpenCV) -> faces (OpenCV) -> objects + persons (YOLO / HOG)
           -> OCR (EasyOCR) -> QR/barcodes (pyzbar / OpenCV) -> regex parsing
           -> metadata (EXIF) -> scene rules -> JSON

Each stage is isolated: if one fails, the others still run and the failure is
reported in "warnings" instead of failing the whole request.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from app.services import metadata_service, scene_service, text_parser
from app.services.face_detector import FaceDetector
from app.services.object_detector import ObjectDetector
from app.services.ocr_service import OCRService
from app.services.qr_detector import QRDetector
from app.utils import image_utils

log = logging.getLogger(__name__)


class ImageAnalyzer:
    """Load the models once (at startup) and reuse them for every request."""

    def __init__(self) -> None:
        t = time.perf_counter()
        self.faces = FaceDetector()
        self.objects = ObjectDetector()
        self.ocr = OCRService()
        self.qr = QRDetector()
        log.info("Models ready in %.1fs: faces=%s objects=%s ocr=%s qr=%s", time.perf_counter() - t,
                 self.faces.engine, self.objects.engine, self.ocr.engine, self.qr.engine)

    def engines(self) -> dict[str, str]:
        return {"faces": self.faces.engine, "objects": self.objects.engine,
                "ocr": self.ocr.engine, "qr": self.qr.engine,
                "metadata": "pillow-exif", "preprocessing": "opencv", "text_parsing": "regex"}

    def analyze(self, img: image_utils.LoadedImage) -> dict:
        warnings: list[str] = []
        timings: dict[str, int] = {}

        @contextmanager
        def stage(name: str):
            t = time.perf_counter()
            try:
                yield
            except Exception as e:  # noqa: BLE001 - one stage must not break the others
                log.exception("stage %s failed", name)
                warnings.append(f"{name} failed: {type(e).__name__}: {e}")
            finally:
                timings[name] = int((time.perf_counter() - t) * 1000)

        bgr = img.bgr
        h, w = bgr.shape[:2]

        quality = {}
        with stage("quality"):
            quality = image_utils.quality_metrics(bgr)

        faces: list[dict] = []
        with stage("faces"):
            faces = self.faces.detect(bgr)

        detections: list[dict] = []
        with stage("objects"):
            detections = self.objects.detect(bgr)
        if self.objects.load_error:
            warnings.append(self.objects.load_error)
        persons = [d for d in detections if d["label"] == "person"]

        lines: list[dict] = []
        with stage("ocr"):
            lines = self.ocr.read(bgr)
        if not self.ocr.available:
            warnings.append(self.ocr.load_error or "OCR unavailable")

        qr_codes: list[dict] = []
        with stage("qr"):
            qr_codes = self.qr.detect(bgr)

        visible: dict = {}
        with stage("text_parsing"):
            visible = text_parser.parse(lines, [q["value"] for q in qr_codes])

        metadata = {"exif_present": False}
        with stage("metadata"):
            metadata = metadata_service.extract(img.raw_pil)

        has_contact = any(visible.get(k) for k in ("email", "phone", "username", "linkedin_url",
                                                   "instagram_url", "facebook_url", "website"))
        scene = {"setting": "unknown", "categories": [], "description": "unclassified scene", "evidence": []}
        with stage("scene"):
            scene = scene_service.analyze(bgr, [d["label"] for d in detections], lines, has_contact,
                                          len(persons), self.objects.full_objects)

        quality["ocr_readability"] = image_utils.ocr_readability(lines, self.ocr.available)
        exif_rot = bool(metadata.get("exif_orientation") and metadata["exif_orientation"] != 1)

        return {
            "success": True,
            "image": {
                "width": w, "height": h, "format": img.file_format, "channels": image_utils.channels_for(img.mode),
                "mode": img.mode, "file_size": img.file_size, "orientation": image_utils.orientation(w, h),
                "exif_rotation_applied": exif_rot,
            },
            "human_detection": {"person_present": bool(persons), "person_count": len(persons),
                                "persons": persons, "engine": self.objects.engine},
            "face_detection": {"face_present": bool(faces), "face_count": len(faces), "faces": faces,
                               "engine": self.faces.engine},
            "visible_information": visible,
            "text": [l["text"] for l in lines],
            "text_details": [{"text": l["text"], "confidence": l["confidence"], "bbox": l["bbox"]} for l in lines],
            "objects": detections,
            "qr_codes": qr_codes,
            "scene": scene,
            "quality": quality,
            "metadata": metadata,
            "engines": self.engines(),
            "warnings": warnings,
            "processing_ms": timings,
        }
