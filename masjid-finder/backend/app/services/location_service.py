"""Geocoding (place name -> coordinates) and reverse geocoding via Nominatim.

Nominatim's public server allows max 1 request/second and needs a real
User-Agent; we throttle globally and cache aggressively. Point NOMINATIM_URL
at a self-hosted instance (or swap this module) for heavy production traffic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import httpx
from timezonefinder import TimezoneFinder

from app.config import settings
from app.services.cache import TTLCache

log = logging.getLogger(__name__)

_cache = TTLCache(ttl=settings.cache_ttl_geocode, max_items=5000)
_lock = asyncio.Lock()
_last_call = 0.0
_tf: TimezoneFinder | None = None


class LocationError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


async def _nominatim(path: str, params: dict) -> object:
    global _last_call
    params = {**params, "format": "jsonv2", "accept-language": "en"}
    if settings.contact_email:
        params["email"] = settings.contact_email
    async with _lock:                                    # 1 request / second
        wait = 1.05 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=12, headers={"User-Agent": settings.user_agent}) as c:
                r = await c.get(f"{settings.nominatim_url}/{path}", params=params)
        except httpx.HTTPError as e:
            log.warning("nominatim %s failed: %s", path, e)
            raise LocationError("Location service is unreachable. Check your internet connection.") from e
        finally:
            _last_call = time.monotonic()
    if r.status_code == 429:
        raise LocationError("Location service is busy. Please try again in a moment.", 503)
    if r.status_code >= 400:
        raise LocationError(f"Location service error ({r.status_code}).")
    return r.json()


def _place(addr: dict) -> dict:
    city = (addr.get("city") or addr.get("town") or addr.get("village") or addr.get("municipality")
            or addr.get("city_district") or addr.get("county"))
    return {
        "area": addr.get("suburb") or addr.get("neighbourhood") or addr.get("quarter"),
        "city": city,
        "state": addr.get("state") or addr.get("region"),
        "country": addr.get("country"),
        "country_code": (addr.get("country_code") or "").upper() or None,
    }


def timezone_at(lat: float, lng: float) -> str:
    global _tf
    if _tf is None:
        _tf = TimezoneFinder(in_memory=True)
    return _tf.timezone_at(lat=lat, lng=lng) or _tf.closest_timezone_at(lat=lat, lng=lng) or "UTC"


def country_from_timezone(tz: str) -> str | None:
    """Rough country guess without a network call (used when no country is known)."""
    return {
        "Asia/Kolkata": "IN", "Asia/Calcutta": "IN", "Asia/Karachi": "PK", "Asia/Dhaka": "BD",
        "Asia/Kabul": "AF", "Asia/Riyadh": "SA", "Asia/Dubai": "AE", "Asia/Kuwait": "KW",
        "Asia/Qatar": "QA", "Asia/Bahrain": "BH", "Asia/Muscat": "OM", "Africa/Cairo": "EG",
        "Europe/Istanbul": "TR", "Asia/Tehran": "IR", "Asia/Singapore": "SG",
        "Asia/Kuala_Lumpur": "MY", "Asia/Jakarta": "ID", "Europe/Paris": "FR", "Europe/Moscow": "RU",
    }.get(tz) or ("US" if tz.startswith("America/") and tz not in ("America/Sao_Paulo",) else None)


async def reverse(lat: float, lng: float) -> dict:
    key = ("rev", round(lat, 4), round(lng, 4))
    hit = _cache.get(key)
    if hit:
        return hit
    data = await _nominatim("reverse", {"lat": f"{lat:.6f}", "lon": f"{lng:.6f}", "zoom": 18, "addressdetails": 1})
    if not isinstance(data, dict) or data.get("error"):
        raise LocationError("No address found for this location.", 404)
    out = {
        "latitude": lat, "longitude": lng,
        "display_name": data.get("display_name"),
        **_place(data.get("address") or {}),
        "timezone": timezone_at(lat, lng),
    }
    _cache.set(key, out)
    return out


async def search(query: str, limit: int = 5) -> list[dict]:
    q = " ".join(query.split())[:200]
    if len(q) < 2:
        raise LocationError("Type at least 2 characters.", 422)
    key = ("search", q.lower(), limit)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    data = await _nominatim("search", {"q": q, "limit": min(limit * 2, 20), "addressdetails": 1})
    items = data if isinstance(data, list) else []
    # Nominatim's order can put a tiny hamlet first ("Mecca" -> Italy); its
    # importance score puts the well-known place (Makkah, Saudi Arabia) on top.
    items.sort(key=lambda i: float(i.get("importance") or 0), reverse=True)
    results = []
    for item in items[:limit]:
        lat, lng = float(item["lat"]), float(item["lon"])
        results.append({
            "name": item.get("name") or q,
            "display_name": item.get("display_name"),
            "latitude": lat, "longitude": lng,
            "type": item.get("type"),
            **_place(item.get("address") or {}),
            "timezone": timezone_at(lat, lng),
        })
    _cache.set(key, results)
    return results


async def address_of(lat: float, lng: float) -> str | None:
    """Approximate street address for a point (used for mosques with no addr:* tags)."""
    try:
        data = await reverse(lat, lng)
    except LocationError:
        return None
    return data.get("display_name")


def today_in(tz: str):
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(tz)).date()
