"""FastAPI app. Run:  uvicorn app.main:app --reload   ->  http://127.0.0.1:8000/docs"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.image_api import router
from app.config import settings
from app.services.image_analyzer import ImageAnalyzer

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("image-intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.models_dir.mkdir(parents=True, exist_ok=True)
    log.info("Loading models (first run may download them)...")
    app.state.analyzer = ImageAnalyzer()
    yield


app = FastAPI(
    title="Image Intelligence API",
    version="1.0.0",
    description="Extract visible, non-identifying information from an image as structured JSON. "
                "No face recognition and no social-media lookups.",
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"success": False, "error": exc.detail})


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"success": False, "error": "Send the image as multipart/form-data field 'file'."})


@app.exception_handler(Exception)
async def unexpected_error(_: Request, exc: Exception):
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"success": False, "error": "Internal error while analysing the image."})
