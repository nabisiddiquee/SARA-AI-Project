"""Web commands from the dashboard.

``parse`` turns a command ("youtube par arijit singh chalao", "delhi ka map",
"google best phones 2026") into a request: YouTube videos play in the
dashboard's player (found with ``youtube_search``); Google, maps, Wikipedia
and websites open in a browser tab. It also recognises "close screen" and
"full screen" for the dashboard's big panel.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

# --- URLs ------------------------------------------------------------------


def google_url(query: str = "") -> str:
    if not query:
        return "https://www.google.com/"
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": query})


def maps_url(place: str) -> str:
    return "https://www.google.com/maps/search/?" + urllib.parse.urlencode({"api": "1", "query": place})


def wikipedia_url(topic: str, lang: str = "en") -> str:
    host = "hi.wikipedia.org" if lang == "hi" else "en.wikipedia.org"
    return f"https://{host}/w/index.php?" + urllib.parse.urlencode({"search": topic, "go": "Go"})


# --- YouTube search (no API key: reads the results page's initial data) ----


def youtube_search(query: str, limit: int = 8) -> list[dict]:
    """Top YouTube results as ``{video_id, title, channel}`` dicts."""
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        html = r.read().decode("utf-8", "replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not m:
        return []
    results: list[dict] = []

    def walk(node) -> None:
        if len(results) >= limit:
            return
        if isinstance(node, dict):
            vr = node.get("videoRenderer")
            if isinstance(vr, dict) and vr.get("videoId"):
                runs = (vr.get("title") or {}).get("runs") or [{}]
                owner = (vr.get("ownerText") or {}).get("runs") or [{}]
                results.append({
                    "video_id": vr["videoId"],
                    "title": runs[0].get("text", ""),
                    "channel": owner[0].get("text", ""),
                })
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(m.group(1)))
    return results


# --- Command parsing ---------------------------------------------------------

_YT_VERBS = r"(?:chalao|chala do|chalado|lagao|laga do|bajao|baja do|play karo|play kar do|dikhao|sunao|चलाओ|लगाओ|बजाओ|दिखाओ|सुनाओ)"
_YT_ON = r"(?:youtube|you tube|यूट्यूब)\s+(?:par|pe|pr|mein|me|on|पर|पे|में)"

_YOUTUBE_PLAY = [
    rf"^(?:play|search)\s+(.+?)\s+(?:on|in)\s+(?:youtube|you tube)$",
    rf"^(?:youtube|you tube)\s+(?:play|search)\s+(.+)$",
    rf"^{_YT_ON}\s+(.+?)(?:\s+{_YT_VERBS})?$",
    rf"^(.+?)\s+{_YT_ON}\s+{_YT_VERBS}$",
]
_YOUTUBE_OPEN = (
    "open youtube", "youtube open", "youtube open karo", "youtube kholo", "youtube khol do",
    "start youtube", "youtube chalao", "youtube", "यूट्यूब खोलो", "यूट्यूब",
)
_SEARCH = [
    r"^(?:google|गूगल)\s+(?:par|pe|pr|पर|पे)\s+(.+?)(?:\s+(?:search karo|search kro|search kar do|dhundo|khojo|सर्च करो|खोजो))?$",
    r"^(?:google search|google|search for|search)\s+(.+)$",
    r"^(.+?)\s+(?:google\s+)?(?:search karo|search kro|search kar do|google karo|dhundo|khojo|सर्च करो|खोजो|ढूंढो)$",
]
_GOOGLE_OPEN = ("open google", "google kholo", "google open karo", "गूगल खोलो")
_MAP = [
    r"^(?:show\s+)?(?:the\s+)?(?:map|maps)\s+(?:of\s+|for\s+)?(.+)$",
    r"^(?:show|open|find)\s+(.+?)\s+on\s+(?:the\s+)?maps?$",
    r"^(?:location of|directions to)\s+(.+)$",
    r"^(.+?)\s+(?:ka|ki|का|की)\s+(?:map|maps|location|naksha|नक्शा|मैप|लोकेशन)(?:\s+(?:dikhao|batao|kholo|दिखाओ|बताओ))?$",
]
_WIKI = [
    r"^(?:wikipedia|wiki|विकिपीडिया)\s+(?:par\s+|pe\s+|on\s+|पर\s+)?(.+?)(?:\s+(?:dikhao|kholo|batao|दिखाओ))?$",
    r"^(?:open\s+|show\s+)?(.+?)\s+(?:on|in)\s+wikipedia$",
    r"^(.+?)\s+(?:wikipedia|wiki|विकिपीडिया)\s+(?:par|pe|पर)?\s*(?:dikhao|kholo|batao|दिखाओ)?$",
]
_SITE = [
    r"^(?:open|visit|go to|kholo)\s+((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)$",
    r"^((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?)\s+(?:kholo|open karo|khol do|खोलो)$",
]
_CLOSE = (
    "close screen", "close the screen", "close window", "close stage", "close browser",
    "close youtube", "close google", "close map", "close maps", "close video", "close wikipedia",
    "back to camera", "screen band karo", "screen band", "window band karo", "youtube band karo",
    "video band karo", "map band karo", "स्क्रीन बंद करो", "यूट्यूब बंद करो",
)
_FULL = ("full screen", "fullscreen", "maximize", "maximise", "bada karo", "screen badi karo",
         "screen bada karo", "फुल स्क्रीन", "बड़ा करो")
_EXIT_FULL = ("exit full screen", "exit fullscreen", "minimize", "minimise", "chhota karo",
              "screen chhoti karo", "normal screen", "restore screen", "छोटा करो")


def _norm(text: str) -> str:
    t = " ".join(text.lower().strip().split())
    return re.sub(r"[?.!।]+$", "", t).strip()


def _first(patterns, t: str) -> str | None:
    for p in patterns:
        m = re.match(p, t)
        if m and m.group(1).strip():
            return m.group(1).strip()
    return None


def parse(text: str) -> dict | None:
    """The panel action a command asks for, or None.

    Returns one of ``{"action": "close"}``, ``{"action": "maximize"}``,
    ``{"action": "restore"}``, ``{"action": "youtube_play", "query"}``,
    or ``{"action": "web", "kind", "title", "url"}``.
    """
    t = _norm(text)
    if not t:
        return None
    if t in _EXIT_FULL:
        return {"action": "restore"}
    if t in _FULL:
        return {"action": "maximize"}
    if t in _CLOSE:
        return {"action": "close"}
    if t in _YOUTUBE_OPEN:
        return {"action": "web", "kind": "YOUTUBE", "title": "YouTube", "url": "https://www.youtube.com/"}
    q = _first(_YOUTUBE_PLAY, t)
    if q:
        return {"action": "youtube_play", "query": q}
    if t in _GOOGLE_OPEN:
        return {"action": "web", "kind": "GOOGLE", "title": "Google", "url": google_url()}
    place = _first(_MAP, t)
    if place:
        return {"action": "web", "kind": "MAP", "title": place.title(), "url": maps_url(place)}
    topic = _first(_WIKI, t)
    if topic:
        lang = "hi" if re.search(r"[ऀ-ॿ]", topic) else "en"
        return {"action": "web", "kind": "WIKI", "title": topic.title(), "url": wikipedia_url(topic, lang)}
    site = _first(_SITE, t)
    if site:
        url = site if site.startswith("http") else "https://" + site
        return {"action": "web", "kind": "WEB", "title": site, "url": url}
    q = _first(_SEARCH, t)
    if q:
        return {"action": "web", "kind": "GOOGLE", "title": q, "url": google_url(q)}
    return None
