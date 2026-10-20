"""Reader view for news stories, so the dashboard can show them in its own
panel instead of opening a new tab (most news sites refuse to be framed).

``read(link)`` takes a Google News article link (as found in the RSS feed),
resolves it to the publisher's URL, downloads the page and extracts the
headline, lead image, publish time and body paragraphs. Standard library
only; results are cached.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
}
_GN_PREFIX = "https://news.google.com/"
_CACHE_MAX = 60
_cache: dict[str, dict] = {}      # Google News link -> reader data
_resolved: dict[str, str] = {}    # Google News link -> publisher URL


def _remember(store: dict, key, value) -> None:
    if len(store) >= _CACHE_MAX:
        store.pop(next(iter(store)))
    store[key] = value


def _get(url: str, *, data: bytes | None = None, headers: dict | None = None) -> tuple[str, str]:
    """(final_url, text) for ``url``."""
    req = urllib.request.Request(url, data=data, headers={**_HEADERS, **(headers or {})})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        raw = r.read(3_000_000)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.geturl(), raw.decode(charset, "replace")


def resolve(link: str) -> str:
    """The publisher URL behind a Google News ``/articles/<id>`` link.

    Google no longer redirects these; the article page carries a signature
    that its own ``batchexecute`` endpoint exchanges for the real URL.
    Cached, since Google rate-limits repeated lookups.
    """
    if link in _resolved:
        return _resolved[link]
    m = re.search(r"/articles/([^?/#]+)", link)
    if not m:
        return link
    aid = m.group(1)
    _, html = _get(f"https://news.google.com/articles/{aid}")
    sg = re.search(r'data-n-a-sg="([^"]+)"', html)
    ts = re.search(r'data-n-a-ts="([^"]+)"', html)
    if not (sg and ts):
        raise ValueError("could not resolve the Google News link")
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                             None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
             aid, int(ts.group(1)), sg.group(1)]
    freq = json.dumps([[["Fbv4je", json.dumps(inner), None, "generic"]]])
    _, body = _get(
        "https://news.google.com/_/DotsSplashUi/data/batchexecute",
        data=urllib.parse.urlencode({"f.req": freq}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
    )
    m = re.search(r'\[\\"garturlres\\",\\"(.*?)\\"', body)
    if not m:
        raise ValueError("could not resolve the Google News link")
    url = m.group(1).encode().decode("unicode_escape")
    _remember(_resolved, link, url)
    return url


class _Extractor(HTMLParser):
    """Collects <meta> tags, JSON-LD blocks and the text of <p> elements,
    skipping page chrome."""

    _SKIP = {"script", "style", "noscript", "nav", "footer", "aside", "form",
             "header", "figcaption", "button", "svg", "iframe", "select"}
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.title = ""
        self.paras: list[tuple[bool, str]] = []  # (inside <article>, text)
        self._skip = 0
        self._article = 0
        self._in_p = False
        self._in_title = False
        self._buf: list[str] = []
        self.ld: list[str] = []   # <script type="application/ld+json"> bodies
        self._in_ld = False

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID:
            if tag == "meta":
                a = dict(attrs)
                key = (a.get("property") or a.get("name") or "").lower()
                if key and a.get("content") and key not in self.meta:
                    self.meta[key] = a["content"]
            return
        if tag == "script" and (dict(attrs).get("type") or "").lower() == "application/ld+json":
            self._in_ld = True
            self.ld.append("")
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "article":
            self._article += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "p" and not self._skip:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_ld = False
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag == "article" and self._article:
            self._article -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            self._in_p = False
            text = " ".join("".join(self._buf).split())
            if text:
                self.paras.append((self._article > 0, text))

    def handle_data(self, data):
        if self._in_ld:
            self.ld[-1] += data
        elif self._in_title:
            self.title += data
        elif self._in_p and not self._skip:
            self._buf.append(data)


_JUNK = re.compile(
    r"(subscribe|sign up|sign in|log ?in|download (the )?app|follow us|cookie|"
    r"advertisement|also read|read more|click here|all rights reserved|copyright|"
    r"whatsapp channel|telegram|newsletter|subscription|premium stor|unlock (these|this)|"
    r"e-?paper|google news|सब्सक्राइब|फॉलो करें|यह भी पढ़ें|ये भी पढ़ें)", re.I)


def _body(paras: list[tuple[bool, str]]) -> list[str]:
    # Prefer paragraphs inside <article> when the page marks its story up.
    inside = [t for a, t in paras if a]
    chosen = inside if sum(len(t) for t in inside) > 400 else [t for _, t in paras]
    out, seen = [], set()
    for t in chosen:
        if len(t) < 40 or t in seen:
            continue
        if len(t) < 300 and _JUNK.search(t):
            continue
        seen.add(t)
        out.append(t)
    return out[:40]


def _ld_body(blocks: list[str]) -> list[str]:
    """Paragraphs from a JSON-LD ``articleBody`` (sites that render their text
    with scripts often still publish it here)."""
    best = ""

    def walk(node) -> None:
        nonlocal best
        if isinstance(node, dict):
            body = node.get("articleBody")
            if isinstance(body, str) and len(body) > len(best):
                best = body
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for raw in blocks:
        try:
            walk(json.loads(raw))
        except ValueError:
            continue
    text = unescape(re.sub(r"<[^>]+>", " ", best)).strip()
    if not text:
        return []
    parts = [" ".join(p.split()) for p in re.split(r"\n\s*\n|\n", text)]
    parts = [p for p in parts if p]
    if len(parts) == 1:  # one long block: regroup every ~3 sentences
        sentences = re.split(r"(?<=[.!?।])\s+", parts[0])
        parts = [" ".join(sentences[i:i + 3]) for i in range(0, len(sentences), 3)]
    return _body([(True, p) for p in parts])


def read(link: str) -> dict:
    """Reader data for a Google News article link. Raises on failure."""
    if not link.startswith(_GN_PREFIX):
        raise ValueError("only Google News article links are supported")
    if link in _cache:
        return _cache[link]
    url = resolve(link)
    final_url, html = _get(url)
    p = _Extractor()
    p.feed(html)
    m = p.meta
    image = m.get("og:image") or m.get("twitter:image") or ""
    paragraphs = _body(p.paras)
    if len(paragraphs) < 3:
        from_ld = _ld_body(p.ld)
        if sum(map(len, from_ld)) > sum(map(len, paragraphs)):
            paragraphs = from_ld
    data = {
        "url": final_url,
        "title": unescape(m.get("og:title") or m.get("twitter:title") or p.title).strip(),
        "site": m.get("og:site_name") or urllib.parse.urlsplit(final_url).netloc,
        "description": unescape(m.get("og:description") or m.get("description") or "").strip(),
        "image": urllib.parse.urljoin(final_url, image) if image else "",
        "published": m.get("article:published_time") or "",
        "paragraphs": paragraphs,
    }
    _remember(_cache, link, data)
    return data
