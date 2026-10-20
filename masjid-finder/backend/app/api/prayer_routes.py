from __future__ import annotations

from datetime import date as Date

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.models.prayer import PrayerTimesResponse
from app.services import location_service, prayer_service

router = APIRouter(prefix="/api", tags=["prayer"])


@router.get("/prayer-times", response_model=PrayerTimesResponse)
def prayer_times(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    date: Date | None = Query(None, description="YYYY-MM-DD; default = today at that location"),
    method: str = Query(settings.prayer_method,
                        description="auto | " + " | ".join(prayer_service.METHODS)),
    asr: str = Query(settings.asr_method, description="auto | standard | hanafi"),
    country: str | None = Query(None, min_length=2, max_length=2,
                                description="ISO country code; improves 'auto' method choice"),
    timezone: str | None = Query(None, description="IANA zone; default = looked up from coordinates"),
) -> PrayerTimesResponse:
    tz = timezone or location_service.timezone_at(latitude, longitude)
    try:
        day = date or location_service.today_in(tz)
    except Exception as e:
        raise HTTPException(422, f"Unknown timezone: {tz}") from e
    m, a = prayer_service.resolve_method(method, asr, country or location_service.country_from_timezone(tz))
    return PrayerTimesResponse(**prayer_service.prayer_times(latitude, longitude, day, tz, m, a))


@router.get("/prayer-methods")
def methods() -> dict:
    return {"methods": {k: v.name for k, v in prayer_service.METHODS.items()},
            "asr": ["standard", "hanafi"], "default": settings.prayer_method}
