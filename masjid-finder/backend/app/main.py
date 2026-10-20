"""Masjid Finder API.

Run:  uvicorn app.main:app --reload --port 8000   (from backend/)
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import location_routes, mosque_routes, prayer_routes
from app.config import ROOT_DIR, settings

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("masjid-finder")
logging.getLogger("httpx").setLevel(logging.WARNING)

app = FastAPI(title="Masjid Finder API", version="1.0.0",
              description="Nearby masjids (OpenStreetMap / Google Places) and calculated prayer times.")
app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                   allow_methods=["GET"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(mosque_routes.router)
app.include_router(prayer_routes.router)
app.include_router(location_routes.router)


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "request"
    return JSONResponse(status_code=422, content={"detail": f"Invalid {field}: {first.get('msg', 'bad value')}"})


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Something went wrong. Please try again."})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "google_places": bool(settings.google_maps_api_key)}


# Serve the built React app (frontend/dist) from the same origin in production.
_DIST = ROOT_DIR / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        file = (_DIST / path).resolve()
        if path and file.is_file() and _DIST.resolve() in file.parents:
            return FileResponse(file)
        return FileResponse(_DIST / "index.html")
