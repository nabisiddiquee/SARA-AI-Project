"""Place name for a GPS position (reverse geocoding).

The IP-based location from /netinfo only knows the internet provider's hub
city, which is often wrong (e.g. "Ghaziabad" while you are in Noida). When the
browser shares a precise position, this turns it into a real address using
OpenStreetMap's Nominatim service.

Nominatim's usage policy asks for an identifying User-Agent and at most one
request per second, so results are cached by position (~10 m grid) and
requests are spaced out.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

_UA = "SaraOS/1.0 (local voice assistant)"
_URL = "https://nominatim.openstreetmap.org/reverse"
_cache: dict[tuple[float, float], dict] = {}
_lock = threading.Lock()
_last_call = 0.0
# The dashboard's latest precise position, so "meri location kya hai" can be
# answered with the same place the HUD shows.
_last_place: dict | None = None


def _first(addr: dict, *keys: str) -> str:
    for k in keys:
        v = (addr.get(k) or "").strip()
        if v:
            return v
    return ""


def reverse(lat: float, lon: float) -> dict:
    """Address for (lat, lon). Never raises; ``ok`` is False on failure."""
    global _last_call, _last_place
    key = (round(lat, 4), round(lon, 4))
    with _lock:
        if key in _cache:
            _last_place = _cache[key]
            return _cache[key]
        wait = 1.1 - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    query = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "zoom": 18, "addressdetails": 1, "accept-language": "en",
    })
    try:
        req = urllib.request.Request(f"{_URL}?{query}", headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
    except (OSError, ValueError):
        return {"ok": False}
    addr = d.get("address") or {}
    city = _first(addr, "city", "town", "village", "municipality", "state_district", "county")
    area = _first(addr, "suburb", "neighbourhood", "quarter", "city_district", "hamlet")
    result = {
        "ok": True,
        "landmark": (d.get("name") or "").strip(),
        "area": area,
        "city": city,
        "district": _first(addr, "state_district", "county"),
        "state": _first(addr, "state"),
        "postcode": _first(addr, "postcode"),
        "country": _first(addr, "country"),
        "country_code": _first(addr, "country_code").upper(),
        "display": (d.get("display_name") or "").strip(),
    }
    # Short label for the HUD: "Chilla, Noida" (area + city).
    result["short"] = ", ".join(x for x in (area, city) if x) or result["display"].split(",")[0]
    with _lock:
        _cache[key] = result
        _last_place = result
    return result


# "meri location kya hai", "main kahan hoon", "where am i", "live location batao".
_LOC_WORDS = ("location", "loc", "lokeshan", "lokesan", "address", "lokation",
              "लोकेशन", "पता", "लोकेसन")
_SELF_WORDS = ("my", "meri", "mera", "mere", "mai", "main", "hum", "hamari",
               "current", "live", "abhi", "मेरी", "मेरा", "मैं", "लाइव", "अभी")
_WHERE_PHRASES = ("where am i", "main kahan hoon", "main kaha hoon", "mai kaha hu",
                  "main kahan hu", "mai kahan hu", "mai kaha hoon", "hum kahan hai",
                  "hum kaha hai", "मैं कहाँ हूँ", "मैं कहां हूं", "मैं कहा हूँ")


def _words(text: str) -> list[str]:
    return text.lower().replace("?", " ").replace(",", " ").split()


def is_location_question(text: str) -> bool:
    low = " ".join(_words(text))
    if any(p in low for p in _WHERE_PHRASES):
        return True
    words = set(_words(text))
    return bool(words & set(_LOC_WORDS)) and bool(words & set(_SELF_WORDS))


def location_reply(*, hi: bool, ip_info: dict | None = None) -> tuple[bool, str]:
    """Spoken answer for "where am I": the dashboard's GPS place when known,
    else the approximate city from the internet connection (``ip_info``)."""
    p = _last_place
    if p and p.get("ok"):
        parts = [p.get("area"), p.get("city"), p.get("state"), p.get("postcode"), p.get("country")]
        place = ", ".join(dict.fromkeys(x for x in parts if x))
        return True, (f"आप अभी {place} में हैं।" if hi else f"You are at {place} right now.")
    ip = ip_info or {}
    place = ", ".join(dict.fromkeys(x for x in (ip.get("city"), ip.get("region"), ip.get("country")) if x))
    if place:
        return True, (f"आप लगभग {place} में हैं। यह इंटरनेट कनेक्शन से अंदाज़ा है; सही जगह के लिए डैशबोर्ड को लोकेशन की अनुमति दीजिए।"
                      if hi else f"You are around {place}. That is estimated from your internet connection; "
                      "allow location access in the dashboard for the exact place.")
    return False, ("माफ़ कीजिए, अभी आपकी लोकेशन पता नहीं चल पाई।" if hi
                   else "Sorry, I could not find your location right now.")
