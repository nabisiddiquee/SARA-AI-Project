"""QR codes and barcodes.

Engine 1: pyzbar (QR + EAN/UPC/Code128/... barcodes; the Windows wheel bundles zbar).
Engine 2: OpenCV QRCodeDetector (QR only, built in).
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

from app.services.text_parser import classify_qr

log = logging.getLogger(__name__)


class QRDetector:
    def __init__(self) -> None:
        self._pyzbar = None
        try:
            from pyzbar import pyzbar  # type: ignore

            self._pyzbar = pyzbar
            self.engine = "pyzbar"
        except Exception as e:  # noqa: BLE001 - missing zbar DLL etc.
            log.info("pyzbar unavailable (%s); using OpenCV QRCodeDetector", e)
            self.engine = "opencv-qrcode"
        self._cv = cv2.QRCodeDetector()

    def detect(self, bgr: np.ndarray) -> list[dict]:
        found: dict[str, dict] = {}
        if self._pyzbar is not None:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            for sym in self._pyzbar.decode(gray):
                value = sym.data.decode("utf-8", errors="replace")
                r = sym.rect
                found.setdefault(value, {"type": classify_qr(value), "value": value, "symbology": sym.type,
                                         "bbox": {"x": r.left, "y": r.top, "width": r.width, "height": r.height}})
        # OpenCV too (catches some QR codes zbar misses, and works without zbar).
        try:
            ok, values, points, _ = self._cv.detectAndDecodeMulti(bgr)
        except cv2.error:
            ok, values, points = False, [], None
        if ok:
            for value, pts in zip(values, points if points is not None else []):
                if not value or value in found:
                    continue
                xs, ys = pts[:, 0], pts[:, 1]
                found[value] = {"type": classify_qr(value), "value": value, "symbology": "QRCODE",
                                "bbox": {"x": int(xs.min()), "y": int(ys.min()),
                                         "width": int(xs.max() - xs.min()), "height": int(ys.max() - ys.min())}}
        return list(found.values())
