from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.models.mosque import AddressResponse, LatLng, Mosque, NearbyResponse
from app.services import location_service, mosque_service

router = APIRouter(prefix="/api/mosques", tags=["mosques"])

ALLOWED_RADII = (1000, 3000, 5000, 10000, 20000)


@router.get("/nearby", response_model=NearbyResponse)
async def nearby(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
    radius: int = Query(5000, ge=100, le=20000, description="Metres; the app offers 1, 3, 5, 10, 20 km"),
) -> NearbyResponse:
    try:
        mosques, source, attribution, cached = await mosque_service.nearby(latitude, longitude, radius)
    except mosque_service.MosqueSourceError as e:
        raise HTTPException(e.status, str(e)) from e
    return NearbyResponse(
        location=LatLng(latitude=latitude, longitude=longitude),
        radius_meters=radius, count=len(mosques), source=source, attribution=attribution,
        mosques=[Mosque(**m) for m in mosques], cached=cached,
    )


@router.get("/address", response_model=AddressResponse)
async def address(
    latitude: float = Query(..., ge=-90, le=90),
    longitude: float = Query(..., ge=-180, le=180),
) -> AddressResponse:
    """Approximate address (reverse geocoded) for a masjid whose source has no address."""
    return AddressResponse(latitude=latitude, longitude=longitude,
                           address=await location_service.address_of(latitude, longitude))
