"""HTTP endpoints: POST /analyze-image, GET /health."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.models.response_model import AnalysisResponse, ErrorResponse
from app.utils.image_utils import ImageValidationError, load_image

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", tags=["system"])
def health(request: Request) -> dict:
    analyzer = request.app.state.analyzer
    return {"status": "ok", "engines": analyzer.engines()}


@router.post(
    "/analyze-image",
    response_model=AnalysisResponse,
    responses={400: {"model": ErrorResponse}, 413: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    tags=["analysis"],
    summary="Analyse one image (JPG / PNG / WEBP)",
    description=(
        "Extracts only what is visible: image info, person/face detection (no identification), "
        "OCR text with emails/phones/usernames/URLs, objects, QR codes, scene, quality and EXIF metadata. "
        "Faces are never matched against any database or social network."
    ),
)
async def analyze_image(request: Request, file: UploadFile = File(..., description="JPG, JPEG, PNG or WEBP")) -> dict:
    t0 = time.perf_counter()
    limit = int(settings.max_upload_mb * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File too large (max {settings.max_upload_mb:g} MB).")
    try:
        img = load_image(data, file.filename or "", file.content_type or "")
    except ImageValidationError as e:
        log.info("rejected upload %r: %s", file.filename, e)
        raise HTTPException(status_code=400, detail=str(e)) from e

    if settings.save_uploads:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}.{img.file_format.lower().replace('jpeg', 'jpg')}"
        (settings.upload_dir / name).write_bytes(data)

    analyzer = request.app.state.analyzer
    # The models are CPU-bound: run them off the event loop.
    result = await run_in_threadpool(analyzer.analyze, img)
    result["processing_ms"]["total"] = int((time.perf_counter() - t0) * 1000)
    log.info("analysed %r (%dx%d) in %d ms: persons=%d faces=%d text_lines=%d qr=%d",
             file.filename, result["image"]["width"], result["image"]["height"], result["processing_ms"]["total"],
             result["human_detection"]["person_count"], result["face_detection"]["face_count"],
             len(result["text"]), len(result["qr_codes"]))
    return result
