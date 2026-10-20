"""Distances and navigation links."""

from __future__ import annotations

import math
from urllib.parse import quote

EARTH_RADIUS_M = 6_371_008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (accurate to ~0.5% for these ranges)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def directions(lat: float, lon: float, name: str = "", place_id: str | None = None) -> dict[str, str]:
    """Navigation URLs to the mosque (destination = its coordinates).

    google: opens the Google Maps app on Android/iOS, the website elsewhere.
    apple:  Apple Maps (iPhone).
    osm:    OpenStreetMap routing in the browser.
    geo:    Android "geo:" intent (any installed maps app).
    """
    g = f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=walking"
    if place_id:
        g += f"&destination_place_id={quote(place_id)}"
    return {
        "google": g,
        "apple": f"https://maps.apple.com/?daddr={lat:.6f},{lon:.6f}&dirflg=w",
        "osm": f"https://www.openstreetmap.org/directions?engine=fossgis_osrm_foot&route=%3B{lat:.6f}%2C{lon:.6f}",
        "geo": f"geo:{lat:.6f},{lon:.6f}?q={lat:.6f},{lon:.6f}({quote(name or 'Masjid')})",
    }


def format_distance(m: float) -> str:
    return f"{round(m)} m" if m < 1000 else f"{m / 1000:.1f} km"
