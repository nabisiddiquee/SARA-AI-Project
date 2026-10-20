"""Nearby masjids from real place data.

Primary source: OpenStreetMap via the Overpass API (free, no key, worldwide,
every mapped mosque within the radius). If GOOGLE_MAPS_API_KEY is set, Google
Places API (New) is queried too and its results (with rating, reviews,
open-now, phone, website) are merged with OSM's, de-duplicated by distance.

Only fields the source actually has are returned. Nothing is guessed.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings
from app.services import maps_service
from app.services.cache import TTLCache

log = logging.getLogger(__name__)

_cache = TTLCache(ttl=settings.cache_ttl_mosques, max_items=2000)
OSM_ATTRIBUTION = "Map data © OpenStreetMap contributors (ODbL)"
GOOGLE_ATTRIBUTION = "Place details © Google"
_GOOGLE_URL = "https://places.googleapis.com/v1/places:searchNearby"
_GOOGLE_FIELDS = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.location",
    "places.rating", "places.userRatingCount", "places.currentOpeningHours.openNow",
    "places.nationalPhoneNumber", "places.internationalPhoneNumber", "places.websiteUri",
    "places.googleMapsUri", "places.businessStatus",
])
_DUPLICATE_METERS = 80        # an OSM and a Google result this close are the same masjid


class MosqueSourceError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


# --- OpenStreetMap ----------------------------------------------------------------
def _osm_address(t: dict) -> str | None:
    if t.get("addr:full"):
        return t["addr:full"]
    street = " ".join(x for x in (t.get("addr:housenumber"), t.get("addr:street")) if x)
    parts = [t.get("addr:place") if not street else street, t.get("addr:suburb"),
             t.get("addr:city"), t.get("addr:postcode")]
    parts = [p for p in parts if p]
    return ", ".join(parts) or None


def _osm_name(t: dict) -> str:
    return t.get("name") or t.get("name:en") or t.get("official_name") or "Masjid (name not listed)"


async def _overpass(lat: float, lng: float, radius: int) -> list[dict]:
    query = (f'[out:json][timeout:25];'
             f'nwr["amenity"="place_of_worship"]["religion"="muslim"](around:{radius},{lat:.6f},{lng:.6f});'
             f'out center tags;')
    last_error = "Map data service is unreachable."
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": settings.user_agent}) as client:
        for url in settings.overpass_urls:
            try:
                r = await client.post(url, data={"data": query})
            except httpx.HTTPError as e:
                log.warning("overpass %s failed: %s", url, e)
                continue
            if r.status_code == 200:
                return r.json().get("elements", [])
            last_error = ("Map data service is busy. Please try again in a minute."
                          if r.status_code in (429, 504) else f"Map data service error ({r.status_code}).")
            log.warning("overpass %s -> %s", url, r.status_code)
    raise MosqueSourceError(last_error, 503)


def _from_osm(el: dict, lat: float, lng: float) -> dict | None:
    t = el.get("tags") or {}
    c = el.get("center") or el
    if "lat" not in c or "lon" not in c:
        return None
    mlat, mlng = float(c["lat"]), float(c["lon"])
    name = _osm_name(t)
    jamaat = {"schedule": t["service_times"]} if t.get("service_times") else None
    return {
        "id": f"osm:{el['type']}/{el['id']}",
        "name": name,
        "distance_meters": round(maps_service.haversine_m(lat, lng, mlat, mlng)),
        "latitude": mlat, "longitude": mlng,
        "address": _osm_address(t),
        "phone": t.get("phone") or t.get("contact:phone"),
        "website": t.get("website") or t.get("contact:website"),
        "denomination": t.get("denomination"),
        "jamaat_times": jamaat,
        "source": "openstreetmap",
        "source_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
        "directions": maps_service.directions(mlat, mlng, name),
    }


# --- Google Places (optional) -----------------------------------------------------
async def _google(lat: float, lng: float, radius: int) -> list[dict]:
    body = {
        "includedTypes": ["mosque"],
        "maxResultCount": 20,
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": float(radius)}},
    }
    headers = {"X-Goog-Api-Key": settings.google_maps_api_key, "X-Goog-FieldMask": _GOOGLE_FIELDS}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(_GOOGLE_URL, json=body, headers=headers)
    if r.status_code != 200:
        # Never log the key; Google's error message doesn't contain it.
        raise MosqueSourceError(f"Google Places error ({r.status_code}): {r.text[:200]}")
    out = []
    for p in r.json().get("places", []):
        if p.get("businessStatus") == "CLOSED_PERMANENTLY":
            continue
        loc = p.get("location") or {}
        if "latitude" not in loc:
            continue
        mlat, mlng = loc["latitude"], loc["longitude"]
        name = (p.get("displayName") or {}).get("text") or "Masjid (name not listed)"
        out.append({
            "id": f"google:{p['id']}",
            "name": name,
            "distance_meters": round(maps_service.haversine_m(lat, lng, mlat, mlng)),
            "latitude": mlat, "longitude": mlng,
            "address": p.get("formattedAddress"),
            "rating": p.get("rating"),
            "review_count": p.get("userRatingCount"),
            "open_now": (p.get("currentOpeningHours") or {}).get("openNow"),
            "phone": p.get("nationalPhoneNumber") or p.get("internationalPhoneNumber"),
            "website": p.get("websiteUri"),
            "source": "google_places",
            "source_url": p.get("googleMapsUri"),
            "directions": maps_service.directions(mlat, mlng, name, place_id=p["id"]),
        })
    return out


def _merge(google: list[dict], osm: list[dict]) -> list[dict]:
    """Google entries win (richer); OSM fills gaps (address/website/Jamaat) and adds unmatched mosques."""
    merged = list(google)
    for o in osm:
        twin = next((g for g in google if maps_service.haversine_m(
            g["latitude"], g["longitude"], o["latitude"], o["longitude"]) <= _DUPLICATE_METERS), None)
        if twin is None:
            merged.append(o)
            continue
        for field in ("address", "phone", "website", "denomination", "jamaat_times"):
            if not twin.get(field) and o.get(field):
                twin[field] = o[field]
    return merged


async def nearby(lat: float, lng: float, radius: int) -> tuple[list[dict], str, str, bool]:
    """Returns (mosques sorted by distance, source, attribution, cached)."""
    key = (round(lat, 4), round(lng, 4), radius, bool(settings.google_maps_api_key))
    hit = _cache.get(key)
    if hit:
        return (*hit, True)

    use_google = bool(settings.google_maps_api_key)
    tasks = [_overpass(lat, lng, radius)] + ([_google(lat, lng, radius)] if use_google else [])
    results = await asyncio.gather(*tasks, return_exceptions=True)
    osm_raw, google = results[0], (results[1] if use_google else [])

    if isinstance(google, Exception):
        log.warning("google places failed, using OSM only: %s", google)
        google = []
    if isinstance(osm_raw, Exception):
        if not google:                     # both failed -> real error, no fake data
            raise osm_raw if isinstance(osm_raw, MosqueSourceError) else MosqueSourceError(str(osm_raw))
        osm_raw = []

    osm = [m for m in (_from_osm(el, lat, lng) for el in osm_raw) if m]
    mosques = _merge(google, osm) if google else osm
    mosques = [m for m in mosques if m["distance_meters"] <= radius]
    mosques.sort(key=lambda m: m["distance_meters"])

    source = "google_places+openstreetmap" if google else "openstreetmap"
    attribution = f"{GOOGLE_ATTRIBUTION} · {OSM_ATTRIBUTION}" if google else OSM_ATTRIBUTION
    _cache.set(key, (mosques, source, attribution))
    return mosques, source, attribution, False
