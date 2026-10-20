"""Live weather lookups via Open-Meteo (free, no API key required).

The LLM brain can't answer "what's the weather" because it has no real-time
data, so Sara handles weather intents here instead. We geocode the city name and
fetch the current conditions, returning a short bilingual sentence.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes -> (English, Hindi) descriptions.
_CODES: dict[int, tuple[str, str]] = {
    0: ("clear sky", "साफ़ आसमान"),
    1: ("mainly clear", "ज़्यादातर साफ़"),
    2: ("partly cloudy", "आंशिक रूप से बादल"),
    3: ("overcast", "बादल छाए हुए"),
    45: ("fog", "कोहरा"),
    48: ("freezing fog", "कोहरा"),
    51: ("light drizzle", "हल्की बूंदाबांदी"),
    53: ("drizzle", "बूंदाबांदी"),
    55: ("heavy drizzle", "तेज़ बूंदाबांदी"),
    56: ("freezing drizzle", "बर्फ़ीली बूंदाबांदी"),
    57: ("freezing drizzle", "बर्फ़ीली बूंदाबांदी"),
    61: ("light rain", "हल्की बारिश"),
    63: ("rain", "बारिश"),
    65: ("heavy rain", "तेज़ बारिश"),
    66: ("freezing rain", "बर्फ़ीली बारिश"),
    67: ("freezing rain", "बर्फ़ीली बारिश"),
    71: ("light snow", "हल्की बर्फ़बारी"),
    73: ("snow", "बर्फ़बारी"),
    75: ("heavy snow", "तेज़ बर्फ़बारी"),
    77: ("snow grains", "बर्फ़बारी"),
    80: ("rain showers", "बौछारें"),
    81: ("rain showers", "बौछारें"),
    82: ("violent rain showers", "तेज़ बौछारें"),
    85: ("snow showers", "बर्फ़ की बौछारें"),
    86: ("snow showers", "बर्फ़ की बौछारें"),
    95: ("a thunderstorm", "आंधी-तूफ़ान"),
    96: ("a thunderstorm with hail", "ओलों के साथ तूफ़ान"),
    99: ("a thunderstorm with hail", "ओलों के साथ तूफ़ान"),
}


# "Sabhi shahar ka mausam" / "all cities weather" -> these major cities.
MAJOR_CITIES = (
    "Delhi", "Mumbai", "Kolkata", "Chennai", "Bengaluru", "Hyderabad",
    "Patna", "Darbhanga", "Lucknow", "Jaipur", "Ahmedabad", "Pune",
)
_ALL_WORDS = re.compile(
    r"\b(all|sabhi|sab|saare|sare|har|every|major|bade|bde)\s+"
    r"(city|cities|shahar|shahron|shehar|sheher|shahro|jagah)\b"
    r"|सभी शहर|सब शहर|हर शहर",
    re.IGNORECASE,
)


def wants_all_cities(text: str) -> bool:
    return bool(_ALL_WORDS.search(text))


def split_cities(text: str) -> list[str]:
    """'delhi, mumbai aur patna' -> ["delhi", "mumbai", "patna"]."""
    parts = re.split(r",|\band\b|\baur\b|\bor\b|&|/|और", text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


def _get_json(url: str, params: dict) -> dict:
    full = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(full, timeout=15) as resp:  # noqa: S310
        return json.load(resp)


def geocode(name: str) -> dict | None:
    """Look up a place by name; return the top match dict (with latitude,
    longitude, name, timezone, ...) or ``None`` if nothing matched."""
    geo = _get_json(_GEO_URL, {"name": name, "count": 1, "language": "en"})
    results = geo.get("results") or []
    return results[0] if results else None


def find_place(name: str) -> dict | None:
    """Geocode ``name``; if the whole phrase fails, try its words.

    Handles phrases like "bihar darbhanga" or "darbhanga city": a word whose
    match lies in a region named by another word wins ("Darbhanga, Bihar").
    """
    loc = geocode(name)
    if loc:
        return loc
    words = [w for w in name.split() if len(w) > 2]
    hits = [(w, geocode(w)) for w in words]
    hits = [(w, h) for w, h in hits if h]
    for w, h in hits:
        region = (h.get("admin1") or "").lower()
        if any(o.lower() in region for o in words if o != w):
            return h
    return hits[0][1] if hits else None


def get_weather(city: str, *, lang: str = "en", all_cities: bool = False) -> tuple[bool, str]:
    """Return ``(ok, message)`` with the current weather for one or more cities.

    ``city`` may list several places ("delhi, mumbai aur patna"); with
    ``all_cities`` the major Indian cities are shown. With no city, the
    ``SARA_CITY`` default from ``.env`` is used. Never raises.
    """
    if all_cities:
        cities = list(MAJOR_CITIES)
    else:
        cities = split_cities((city or "").strip()) or [
            c for c in [os.environ.get("SARA_CITY", "").strip()] if c
        ]
    if not cities:
        msg = "कौन से शहर का मौसम बताऊँ?" if lang == "hi" else "Which city's weather would you like?"
        return False, msg
    if len(cities) == 1:
        return _weather_one(cities[0], lang=lang)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: _weather_one(c, lang=lang, short=True), cities[:15]))
    ok = any(r[0] for r in results)
    return ok, "\n".join(msg for _, msg in results)


def _weather_one(city: str, *, lang: str = "en", short: bool = False) -> tuple[bool, str]:
    try:
        loc = find_place(city)
        if not loc:
            msg = (
                f"माफ़ कीजिए, '{city}' नाम का शहर नहीं मिला।" if lang == "hi"
                else f"Sorry, I couldn't find a place called {city!r}."
            )
            return False, msg

        data = _get_json(
            _FORECAST_URL,
            {
                "latitude": loc["latitude"],
                "longitude": loc["longitude"],
                "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            },
        )
        cur = data.get("current") or {}
        temp = cur.get("temperature_2m")
        humidity = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        desc_en, desc_hi = _CODES.get(cur.get("weather_code"), ("changing conditions", "बदलता मौसम"))
        place = loc["name"]

        if short:
            desc = desc_hi if lang == "hi" else desc_en
            return True, f"{place}: {temp}°C, {desc}, {humidity}% RH, {wind} km/h"
        if lang == "hi":
            msg = (
                f"{place} में अभी {desc_hi} है। तापमान {temp}°C, नमी {humidity}%, "
                f"और हवा की रफ़्तार {wind} किमी/घंटा।"
            )
        else:
            msg = (
                f"It's currently {desc_en} in {place}. Temperature {temp}°C, "
                f"humidity {humidity}%, wind {wind} km/h."
            )
        return True, msg

    except Exception as e:  # noqa: BLE001
        msg = (
            f"अभी मौसम की जानकारी नहीं ला पाई: {e}" if lang == "hi"
            else f"I couldn't fetch the weather right now: {e}"
        )
        return False, msg
