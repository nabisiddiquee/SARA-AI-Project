"""Info cards for the camera panel, built from what the user types.

The input is a link, a name or a question. Nothing here looks at faces: the
card shows only what was typed and public data about it.

* GitHub link        -> GitHub's public REST API (name, bio, company, repos...)
* LinkedIn link      -> LinkedIn refuses apps (HTTP 999), so we return the
                        handle + link and the page lets the user fill details in
* Other link         -> the page's Open Graph preview (title, text, image)
* Name               -> Wikipedia summary, if there is one
* Question           -> Sara's AI answer
Every result carries a Google search link the user can open in a new tab.
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124 Safari/537.36 SaraOS/1.0")
_TIMEOUT = 8

_PLATFORMS = {
    "linkedin.com": "LinkedIn", "github.com": "GitHub", "instagram.com": "Instagram",
    "facebook.com": "Facebook", "x.com": "X", "twitter.com": "X",
    "youtube.com": "YouTube", "medium.com": "Medium",
}


def _get(url: str, accept: str = "text/html") -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Accept": accept, "Accept-Language": "en-IN,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return r.status, r.read(600_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (OSError, ValueError):
        return 0, ""


def _google(q: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)


def _platform(host: str) -> str:
    host = host.lower().removeprefix("www.").removeprefix("m.")
    for dom, name in _PLATFORMS.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host


def _handle(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] in ("in", "company", "pub", "user", "c") and len(parts) > 1:
        return parts[1]
    return parts[0].lstrip("@") if parts else ""


def _meta(page: str) -> dict[str, str]:
    """Open Graph / Twitter / description tags from an HTML page."""
    out: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        key = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)', tag, re.I)
        val = re.search(r'content\s*=\s*["\']([^"\']*)', tag, re.I)
        if key and val:
            out.setdefault(key.group(1).lower(), html.unescape(val.group(1)).strip())
    title = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if title:
        out.setdefault("title", html.unescape(title.group(1)).strip())
    return out


def _github(handle: str, url: str) -> dict:
    status, body = _get("https://api.github.com/users/" + urllib.parse.quote(handle),
                        "application/vnd.github+json")
    if status != 200:
        return {}
    d = json.loads(body)
    facts = [(k, v) for k, v in (
        ("Company", d.get("company")), ("Location", d.get("location")),
        ("Website", d.get("blog")), ("Public repos", d.get("public_repos")),
        ("Followers", d.get("followers")),
        ("Joined", (d.get("created_at") or "")[:10]),
    ) if v not in (None, "")]
    return {
        "kind": "profile", "platform": "GitHub", "handle": handle, "url": url,
        "name": d.get("name") or handle, "headline": d.get("bio") or "",
        "image": d.get("avatar_url") or "", "facts": facts, "source": "GitHub API",
    }


def _lookup_url(raw: str) -> dict:
    url = raw if re.match(r"https?://", raw, re.I) else "https://" + raw
    parts = urllib.parse.urlparse(url)
    platform, handle = _platform(parts.netloc), _handle(parts.path)
    base = {
        "kind": "profile", "platform": platform, "handle": handle, "url": url,
        "name": "", "headline": "", "image": "", "facts": [],
        "google": _google(f"{handle} {platform}" if handle else url),
    }
    if platform == "GitHub" and handle:
        gh = _github(handle, url)
        if gh:
            return {**base, **gh}
    status, page = _get(url)
    m = _meta(page) if status == 200 else {}
    if not m.get("og:title") and not m.get("title"):
        # LinkedIn (999), Instagram/Facebook login walls, etc.
        return {**base, "blocked": True, "source": platform,
                "notice": f"{platform} doesn't share profile details with apps. "
                          "Open the profile, then add the details here once."}
    return {
        **base,
        "name": m.get("og:title") or m.get("twitter:title") or m.get("title", ""),
        "headline": m.get("og:description") or m.get("description") or m.get("twitter:description", ""),
        "image": m.get("og:image") or m.get("twitter:image", ""),
        "facts": [("Site", m.get("og:site_name") or platform)],
        "source": "Page preview",
    }


def _lookup_name(name: str) -> dict:
    base = {"kind": "name", "name": name, "headline": "", "image": "", "facts": [],
            "url": "", "google": _google(name), "source": "Wikipedia"}
    status, body = _get("https://en.wikipedia.org/w/api.php?action=opensearch&limit=1&format=json&search="
                        + urllib.parse.quote(name), "application/json")
    try:
        title = json.loads(body)[1][0] if status == 200 else ""
    except (ValueError, IndexError):
        title = ""
    if not title:
        return {**base, "notice": "No public summary found. Try Search on Google."}
    status, body = _get("https://en.wikipedia.org/api/rest_v1/page/summary/"
                        + urllib.parse.quote(title.replace(" ", "_")), "application/json")
    if status != 200:
        return {**base, "notice": "No public summary found. Try Search on Google."}
    d = json.loads(body)
    return {
        **base, "name": d.get("title") or name, "headline": d.get("description") or "",
        "summary": d.get("extract") or "",
        "image": (d.get("thumbnail") or {}).get("source", ""),
        "url": ((d.get("content_urls") or {}).get("desktop") or {}).get("page", ""),
    }


def _is_url(text: str) -> bool:
    return bool(re.match(r"(https?://)?([\w-]+\.)+[a-z]{2,}(/\S*)?$", text, re.I)) and " " not in text


def _is_question(text: str) -> bool:
    words = text.split()
    starts = ("what", "who", "how", "why", "when", "where", "which", "is", "are", "can",
              "kya", "kaise", "kaun", "kyu", "kyun", "kab", "kahan", "tell", "explain")
    return text.endswith("?") or len(words) > 5 or (words and words[0].lower() in starts)


def lookup(text: str, lang: str = "en") -> dict:
    """Build an info card for a link, a name or a question. Never raises."""
    text = (text or "").strip()[:300]
    if not text:
        return {"ok": False, "error": "Type a link, a name or a question."}
    try:
        if _is_url(text):
            card = _lookup_url(text)
        elif _is_question(text):
            from .ai import ask_ai

            res = ask_ai(text, lang=lang)
            card = {"kind": "answer", "name": text, "summary": res.text, "ok_ai": res.ok,
                    "google": _google(text), "source": "Sara AI", "facts": []}
        else:
            card = _lookup_name(text)
        return {"ok": True, "query": text, "card": card}
    except Exception:  # noqa: BLE001
        return {"ok": True, "query": text, "card": {
            "kind": "name", "name": text, "facts": [], "google": _google(text),
            "notice": "Couldn't fetch details right now. Try Search on Google.",
        }}
