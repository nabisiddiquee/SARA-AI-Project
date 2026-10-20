"""Latest news headlines via Google News RSS (free, no API key).

The LLM brain can't fetch current news, so Sara pulls real headlines here.
A topic (e.g. "cricket") searches Google News; without one it returns the top
headlines for India, in English or Hindi to match the user's language.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

_TOP_URL = "https://news.google.com/rss"
_SEARCH_URL = "https://news.google.com/rss/search"


def _edition(lang: str) -> dict:
    if lang == "hi":
        return {"hl": "hi", "gl": "IN", "ceid": "IN:hi"}
    return {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}


def headlines(topic: str = "", *, lang: str = "en", count: int = 5) -> list[dict]:
    """Recent headlines as ``{title, source, link, published}`` dicts.

    Google News titles end in " - Source"; that suffix is split off into
    ``source``. Raises on network/parse errors.
    """
    topic = (topic or "").strip()
    params = _edition(lang)
    if topic:
        url = _SEARCH_URL + "?" + urllib.parse.urlencode({"q": topic, **params})
    else:
        url = _TOP_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
        raw = resp.read()

    items = []
    for item in ElementTree.fromstring(raw).iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].rstrip()
        published = None
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").isoformat()
        except (TypeError, ValueError):
            pass
        items.append({
            "title": title, "source": source,
            "link": (item.findtext("link") or "").strip(), "published": published,
        })
        if len(items) >= count:
            break
    return items


def get_news(topic: str = "", *, lang: str = "en", count: int = 5) -> tuple[bool, str]:
    """Return ``(ok, message)`` with up to ``count`` recent headlines.

    Never raises — returns a friendly message on any failure.
    """
    topic = (topic or "").strip()
    try:
        titles = [
            f"{h['title']} - {h['source']}" if h["source"] else h["title"]
            for h in headlines(topic, lang=lang, count=count)
        ]
        if not titles:
            msg = (
                "अभी कोई खबर नहीं मिली।" if lang == "hi"
                else "I couldn't find any news right now."
            )
            return False, msg

        if lang == "hi":
            head = f"'{topic}' पर ताज़ा खबरें:" if topic else "आज की मुख्य खबरें:"
        else:
            head = f"Top news for {topic!r}:" if topic else "Today's top headlines:"
        body = "\n".join(f"{i}. {t}" for i, t in enumerate(titles, 1))
        return True, head + "\n" + body

    except Exception as e:  # noqa: BLE001
        msg = (
            f"अभी खबरें नहीं ला पाई: {e}" if lang == "hi"
            else f"I couldn't fetch the news right now: {e}"
        )
        return False, msg
