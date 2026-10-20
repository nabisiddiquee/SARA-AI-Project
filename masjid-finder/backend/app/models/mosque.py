from __future__ import annotations

from pydantic import BaseModel, Field


class LatLng(BaseModel):
    latitude: float
    longitude: float


class Mosque(BaseModel):
    id: str = Field(description="Source-prefixed id, e.g. osm:way/123 or google:ChIJ...")
    name: str
    distance_meters: int
    latitude: float
    longitude: float
    address: str | None = Field(None, description="Only when the data source has it (never invented)")
    rating: float | None = None
    review_count: int | None = None
    open_now: bool | None = None
    phone: str | None = None
    website: str | None = None
    denomination: str | None = None
    jamaat_times: dict[str, str] | None = Field(
        None, description="Masjid-published Jamaat times, only if the source lists them (OSM service_times)")
    source: str = Field(description="openstreetmap | google_places")
    source_url: str | None = None
    directions: dict[str, str]


class NearbyResponse(BaseModel):
    location: LatLng
    radius_meters: int
    count: int
    source: str
    attribution: str
    mosques: list[Mosque]
    cached: bool = False


class AddressResponse(BaseModel):
    latitude: float
    longitude: float
    address: str | None
    approximate: bool = True
    attribution: str = "© OpenStreetMap contributors (Nominatim)"
