from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.services import location_service

router = APIRouter(prefix="/api/location", tags=["location"])


@router.get("/search")
async def search(q: str = Query(..., min_length=2, max_length=200), limit: int = Query(5, ge=1, le=10)) -> dict:
    """Manual location: 'Delhi', 'Mumbai', 'Dubai', 'Mecca', a street, a postcode ..."""
    try:
        return {"query": q, "results": await location_service.search(q, limit),
                "attribution": "© OpenStreetMap contributors (Nominatim)"}
    except location_service.LocationError as e:
        raise HTTPException(e.status, str(e)) from e


@router.get("/reverse")
async def reverse(latitude: float = Query(..., ge=-90, le=90),
                  longitude: float = Query(..., ge=-180, le=180)) -> dict:
    """City / state / country for GPS coordinates."""
    try:
        return await location_service.reverse(latitude, longitude)
    except location_service.LocationError as e:
        # Still useful without a place name: timezone comes from local data.
        if e.status == 404:
            return {"latitude": latitude, "longitude": longitude, "display_name": None, "area": None,
                    "city": None, "state": None, "country": None, "country_code": None,
                    "timezone": location_service.timezone_at(latitude, longitude)}
        raise HTTPException(e.status, str(e)) from e
