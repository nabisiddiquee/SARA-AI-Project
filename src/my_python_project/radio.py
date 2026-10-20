"""FM / internet radio stations from the free Radio Browser directory.

https://www.radio-browser.info is a community-run, open list of radio streams.
Stations come with fresh stream URLs ("url_resolved"), so links don't go
stale like hard-coded ones. Results are cached for 10 minutes.

Voice: "radio chalao", "fm lagao", "radio mirchi chalao", "vividh bharati
sunao", "radio band karo".
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request

_SERVERS = ("https://de1.api.radio-browser.info", "https://de2.api.radio-browser.info",
            "https://fi1.api.radio-browser.info", "https://all.api.radio-browser.info")
_UA = {"User-Agent": "SaraOS/1.0 (voice assistant)"}
_TTL = 600
_cache: dict[tuple, tuple[float, list]] = {}
_lock = threading.Lock()

# Quick picks shown as buttons in the radio window.
QUICK = ["Radio Mirchi", "Vividh Bharati", "Red FM", "Big FM", "Radio City", "Bollywood", "Old Hindi", "Ghazal"]

# Stations the directory doesn't list (it has no ghazal station at all), shown
# first whenever the search matches one of their ``keys``.
_EXTRA = (
    {"id": "extra-best-of-ghazals", "name": "Best of Ghazals",
     "url": "https://stream.zeno.fm/yo0kzyzedittv", "hls": False, "codec": "MP3", "bitrate": 0,
     "favicon": "", "tags": ["ghazal", "hindi", "urdu"], "country": "IN", "language": "hindi",
     "votes": 0, "keys": ("ghazal", "ghazals", "gazal", "gazals", "best of ghazals", "गज़ल", "ग़ज़ल", "गजल")},
    {"id": "extra-gazal-radio-london", "name": "Gazal Radio London",
     "url": "https://streaming.webhostnepal.com/8018/stream", "hls": False, "codec": "AAC+", "bitrate": 0,
     "favicon": "", "tags": ["ghazal"], "country": "IN", "language": "hindi",
     "votes": 0, "keys": ("ghazal", "ghazals", "gazal", "gazals", "गज़ल", "ग़ज़ल", "गजल")},
)


def _extras(query: str) -> list[dict]:
    q = " ".join(query.lower().split())
    return [{k: v for k, v in s.items() if k != "keys"} for s in _EXTRA
            if q and (q in s["keys"] or q in s["name"].lower())]


_RADIO_WORD = re.compile(r"(?<![a-z])(?:radio|fm|f\.m\.|redio|rediyo)(?![a-z])|रेडियो|एफएम|एफ़एम")
_PLAY = re.compile(r"(?<![a-z])(?:chalao|chala|lagao|laga|sunao|suna|bajao|play|on|start|kholo|khol|open|tune)(?![a-z])"
                   r"|चलाओ|लगाओ|सुनाओ|बजाओ|खोलो")
_STOP = re.compile(r"(?<![a-z])(?:band|bandh|stop|off|roko|ruko|close|hatao)(?![a-z])|बंद|रोको")
_FILLER = re.compile(r"(?<![a-z])(?:radio|fm|f\.m\.|redio|rediyo|station|sara|please|pls|koi|ek|wala|wali|par|pe|ka|ki|ke|"
                     r"chalao|chala|lagao|laga|sunao|suna|bajao|play|on|start|kholo|khol|open|tune|karo|kar|do|"
                     r"mujhe|mere|liye|hindi|india)(?![a-z])|रेडियो|एफएम|एफ़एम|चलाओ|लगाओ|सुनाओ|बजाओ|खोलो")


def _get(path: str, params: dict) -> list:
    query = urllib.parse.urlencode(params)
    last = None
    for base in _SERVERS:
        try:
            req = urllib.request.Request(f"{base}{path}?{query}", headers=_UA)
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.loads(r.read().decode("utf-8"))
        except (OSError, ValueError) as e:
            last = e
    raise OSError(f"radio directory unreachable: {last}")


def _clean(s: dict) -> dict | None:
    url = (s.get("url_resolved") or s.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    tags = [t.strip() for t in (s.get("tags") or "").split(",") if t.strip()][:4]
    return {
        "id": s.get("stationuuid", ""),
        "name": re.sub(r"\s+", " ", (s.get("name") or "Radio").strip())[:60],
        "url": url,
        "hls": bool(s.get("hls")) or ".m3u8" in url.lower(),
        "codec": s.get("codec") or "",
        "bitrate": int(s.get("bitrate") or 0),
        "favicon": s.get("favicon") or "",
        "tags": tags,
        "country": s.get("countrycode") or "",
        "language": (s.get("language") or "").split(",")[0],
        "votes": int(s.get("votes") or 0),
    }


def stations(query: str = "", limit: int = 30) -> list[dict]:
    """Indian stations (top by votes), or a name/tag search."""
    query = (query or "").strip()[:60]
    key = (query.lower(), limit)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL:
            return hit[1]
    base = {"hidebroken": "true", "order": "votes", "reverse": "true", "limit": str(limit)}
    extra = _extras(query)
    try:
        found = _search(query, base)
    except OSError:
        if not extra:
            raise
        found = []          # directory down: the built-in stations still play
    out, seen = [], set()
    for c in extra + [c for c in map(_clean, found) if c]:
        if c["url"] not in seen and c["name"].lower() not in seen:
            seen.update({c["url"], c["name"].lower()})
            out.append(c)
    out = out[:limit]
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def _search(query: str, base: dict) -> list:
    if query:
        found = _get("/json/stations/search", {**base, "name": query, "countrycode": "IN"})
        if len(found) < 5:      # widen: tag search, then worldwide name search
            found += _get("/json/stations/search", {**base, "tag": query.lower(), "countrycode": "IN"})
        if len(found) < 5:
            found += _get("/json/stations/search", {**base, "name": query})
    else:
        found = _get("/json/stations/search", {**base, "countrycode": "IN"})
    return found


def parse_command(text: str) -> dict | None:
    """{"action": "play"|"stop"|"open", "query": str} for a radio command, else None."""
    t = " ".join(text.lower().split())
    if not _RADIO_WORD.search(t):
        return None
    if _STOP.search(t):
        return {"action": "stop", "query": ""}
    if not _PLAY.search(t):
        return None                      # "radio kya hota hai" is a question, not a command
    query = " ".join(_FILLER.sub(" ", t).split()).strip(" .,!?")
    # "red fm", "big fm", "fever fm": FM is part of the station name.
    m = re.search(r"(?<![a-z])([a-z]+)\s+fm(?![a-z])", t)
    if query and m and m.group(1) == query.split()[-1]:
        query += " fm"
    return {"action": "play" if query else "open", "query": query}
